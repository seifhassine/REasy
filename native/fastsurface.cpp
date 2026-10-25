#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <algorithm>
#include <cstdint>
#include <new>
#include <unordered_map>
#include <utility>
#include <vector>

namespace {

struct Fail {};

class Ref {
public:
    Ref() = default;
    explicit Ref(PyObject *object) : object_(object) {}
    Ref(const Ref &) = delete;
    Ref &operator=(const Ref &) = delete;
    ~Ref() { Py_XDECREF(object_); }
    PyObject *get() const { return object_; }
    PyObject *release() { PyObject *object = object_; object_ = nullptr; return object; }
private:
    PyObject *object_ = nullptr;
};

PyObject *check(PyObject *object) {
    if (!object) throw Fail();
    return object;
}

void check(int status) {
    if (status < 0) throw Fail();
}

template <typename Body> PyObject *guarded(Body body) {
    try {
        return body();
    } catch (const Fail &) {
        return nullptr;
    } catch (const std::bad_alloc &) {
        PyErr_NoMemory();
        return nullptr;
    }
}

PyObject *interned(const char *text) { return check(PyUnicode_InternFromString(text)); }

class Fields {
public:
    Fields(PyObject *type, PyObject *const *names, int count) : count_(count) {
        if (!PyType_Check(type)) {
            PyErr_SetString(PyExc_TypeError, "the value type must be a class");
            throw Fail();
        }
        type_ = reinterpret_cast<PyTypeObject *>(type);
        for (int index = 0; index < count; ++index) {
            PyObject *descriptor = _PyType_LookupRef(type_, names[index]);
            PyMemberDef *member = descriptor && Py_IS_TYPE(descriptor, &PyMemberDescr_Type)
                ? reinterpret_cast<PyMemberDescrObject *>(descriptor)->d_member : nullptr;
            bool usable = member && member->type == Py_T_OBJECT_EX;
            if (usable) offsets_[index] = member->offset;
            Py_XDECREF(descriptor);
            if (!usable) {
                PyErr_Format(PyExc_TypeError, "%R has no slot %R", type, names[index]);
                throw Fail();
            }
            names_[index] = names[index];
        }
    }
    PyObject *get(PyObject *object, int index) const {
        if (Py_TYPE(object) != type_) {
            PyErr_Format(PyExc_TypeError, "flow values must be %s, not %.200s", type_->tp_name, Py_TYPE(object)->tp_name);
            throw Fail();
        }
        PyObject *value = *reinterpret_cast<PyObject **>(reinterpret_cast<char *>(object) + offsets_[index]);
        if (!value) {
            PyErr_Format(PyExc_AttributeError, "'%.200s' object has no attribute %R", type_->tp_name, names_[index]);
            throw Fail();
        }
        return value;
    }
    bool complete(PyObject *object) const {
        if (Py_TYPE(object) != type_) return false;
        for (int index = 0; index < count_; ++index)
            if (!*reinterpret_cast<PyObject **>(reinterpret_cast<char *>(object) + offsets_[index])) return false;
        return true;
    }
    PyObject *make(PyObject *const *items) const {
        PyObject *object = check(type_->tp_alloc(type_, 0));
        for (int index = 0; index < count_; ++index)
            *reinterpret_cast<PyObject **>(reinterpret_cast<char *>(object) + offsets_[index]) = Py_NewRef(items[index]);
        return object;
    }
private:
    PyTypeObject *type_;
    int count_;
    Py_ssize_t offsets_[6] = {};
    PyObject *names_[6] = {};
};

class Numbering {
public:
    explicit Numbering(int64_t first = 0) : first_(first) { slots_.resize(64); }
    int64_t of(const std::vector<int64_t> &key) {
        uint64_t hash = 0x9E3779B97F4A7C15ull ^ key.size();
        for (int64_t item : key) {
            hash = (hash ^ static_cast<uint64_t>(item)) * 0xBF58476D1CE4E5B9ull;
            hash ^= hash >> 31;
        }
        size_t mask = slots_.size() - 1;
        for (size_t at = hash & mask;; at = (at + 1) & mask) {
            Slot &slot = slots_[at];
            if (!slot.length) {
                int64_t number = first_ + static_cast<int64_t>(count_);
                slot = {hash, keys_.size(), key.size(), number};
                keys_.insert(keys_.end(), key.begin(), key.end());
                if (++count_ * 2 > slots_.size()) grow();
                return number;
            }
            if (slot.hash == hash && slot.length == key.size()
                && std::equal(key.begin(), key.end(), keys_.begin() + static_cast<std::ptrdiff_t>(slot.offset)))
                return slot.number;
        }
    }
    size_t size() const { return count_; }
    void clear() {
        std::fill(slots_.begin(), slots_.end(), Slot());
        keys_.clear();
        count_ = 0;
    }
private:
    struct Slot { uint64_t hash = 0; size_t offset = 0, length = 0; int64_t number = 0; };
    void grow() {
        std::vector<Slot> old(slots_.size() * 2);
        old.swap(slots_);
        size_t mask = slots_.size() - 1;
        for (const Slot &slot : old) {
            if (!slot.length) continue;
            size_t at = slot.hash & mask;
            while (slots_[at].length) at = (at + 1) & mask;
            slots_[at] = slot;
        }
    }
    std::vector<Slot> slots_;
    std::vector<int64_t> keys_;
    size_t count_ = 0;
    int64_t first_;
};

struct PairHash {
    size_t operator()(const std::pair<PyObject *, PyObject *> &pair) const noexcept {
        uint64_t hash = reinterpret_cast<uintptr_t>(pair.first) * 0x9E3779B97F4A7C15ull;
        return static_cast<size_t>((hash ^ reinterpret_cast<uintptr_t>(pair.second)) * 0xBF58476D1CE4E5B9ull);
    }
};

enum Kind { PURE, SOURCE, OTHER };

class Graph {
public:
    Graph(PyObject *values, PyObject *value_type) {
        static PyObject *const names[] = {interned("kind"), interned("operands"), interned("controls"),
                                          interned("signature")};
        static PyObject *const pure = interned("pure"), *const source = interned("source");
        Fields fields(value_type, names, 4);
        Ref sequence(check(PySequence_Fast(values, "flow values must be a sequence")));
        Py_ssize_t count = PySequence_Fast_GET_SIZE(sequence.get());
        kinds_.reserve(count);
        identity_.reserve(count);
        begin_.reserve(count + 1);
        operands_end_.reserve(count);
        Ref identities(check(PyDict_New()));
        std::unordered_map<std::pair<PyObject *, PyObject *>, int64_t, PairHash> seen;
        begin_.push_back(0);
        for (Py_ssize_t index = 0; index < count; ++index) {
            PyObject *value = PySequence_Fast_GET_ITEM(sequence.get(), index);
            PyObject *kind = fields.get(value, 0), *signature = fields.get(value, 3);
            kinds_.push_back(same(kind, pure) ? PURE : same(kind, source) ? SOURCE : OTHER);
            auto found = seen.find({kind, signature});
            if (found == seen.end()) {
                Ref pair(check(PyTuple_Pack(2, kind, signature)));
                Ref next(check(PyLong_FromSsize_t(PyDict_GET_SIZE(identities.get()))));
                PyObject *number = check(PyDict_SetDefault(identities.get(), pair.get(), next.get()));
                found = seen.emplace(std::make_pair(kind, signature), PyLong_AsLongLong(number)).first;
            }
            identity_.push_back(found->second);
            read_indices(fields.get(value, 1), count, dependencies_);
            operands_end_.push_back(dependencies_.size());
            read_indices(fields.get(value, 2), count, dependencies_);
            begin_.push_back(dependencies_.size());
        }
        size_t size = kinds_.size();
        reached_.assign(size, 0);
        member_.assign(size, 0);
        region_.assign(size, 0);
        frontier_.assign(size, 0);
    }

    PyObject *signatures() {
        sign();
        Ref result(check(PyList_New(static_cast<Py_ssize_t>(signatures_.size()))));
        for (size_t index = 0; index < signatures_.size(); ++index)
            PyList_SET_ITEM(result.get(), static_cast<Py_ssize_t>(index), check(PyLong_FromLongLong(signatures_[index])));
        return result.release();
    }

    PyObject *packing(PyObject *roots_object, Py_ssize_t width) {
        sign();
        std::vector<int64_t> roots;
        read_indices(roots_object, static_cast<Py_ssize_t>(kinds_.size()), roots);
        std::vector<int64_t> queue(roots);
        std::vector<std::pair<int64_t, int64_t>> classes;
        ++generation_;
        for (size_t head = 0; head < queue.size(); ++head) {
            int64_t current = queue[head];
            if (reached_[current] == generation_) continue;
            reached_[current] = generation_;
            if (std::find(roots.begin(), roots.end(), current) == roots.end() && kinds_[current] != SOURCE)
                classes.emplace_back(signatures_[current], current);
            if (kinds_[current] == PURE) queue.insert(queue.end(), operand(current, 0), operand(current, 1));
        }
        std::stable_sort(classes.begin(), classes.end(),
                         [](const auto &left, const auto &right) { return left.first < right.first; });
        bool found = false;
        std::vector<int64_t> best_region, best_frontier, region, frontier, members;
        for (size_t start = 0, end; start < classes.size(); start = end) {
            members.clear();
            for (end = start; end < classes.size() && classes[end].first == classes[start].first; ++end)
                members.push_back(classes[end].second);
            if (!separated(roots, members, region, frontier)) continue;
            if (frontier.empty() || static_cast<Py_ssize_t>(frontier.size()) > width) continue;
            if (!found || region.size() > best_region.size()) {
                best_region.swap(region);
                best_frontier.swap(frontier);
                found = true;
            }
        }
        Ref filled(check(PySet_New(nullptr)));
        for (int64_t item : best_region) {
            Ref number(check(PyLong_FromLongLong(item)));
            check(PySet_Add(filled.get(), number.get()));
        }
        Ref tail(check(PyFrozenSet_New(best_region.empty() ? nullptr : filled.get())));
        std::sort(best_frontier.begin(), best_frontier.end());
        Ref edge(numbers(best_frontier));
        return check(PyTuple_Pack(2, tail.get(), edge.get()));
    }

    PyObject *reaches(PyObject *root_sets, PyObject *marked_object) {
        Py_ssize_t size = static_cast<Py_ssize_t>(kinds_.size());
        std::vector<int64_t> marked, roots, found;
        read_indices(marked_object, size, marked);
        uint32_t tag = ++generation_;
        for (int64_t item : marked) member_[item] = tag;
        Ref sets(check(PySequence_Fast(root_sets, "root sets must be a sequence")));
        Ref result(check(PyList_New(PySequence_Fast_GET_SIZE(sets.get()))));
        for (Py_ssize_t index = 0; index < PySequence_Fast_GET_SIZE(sets.get()); ++index) {
            roots.clear();
            read_indices(PySequence_Fast_GET_ITEM(sets.get(), index), size, roots);
            found.clear();
            walk(roots, [&](int64_t value) {
                if (member_[value] == tag) found.push_back(value);
            });
            PyList_SET_ITEM(result.get(), index, numbers(found));
        }
        return result.release();
    }

    PyObject *frontier(PyObject *position_object, PyObject *attributes_object) {
        size_t size = kinds_.size();
        std::vector<int64_t> position, attributes, roots;
        read_indices(position_object, static_cast<Py_ssize_t>(size), position);
        read_indices(attributes_object, static_cast<Py_ssize_t>(size), attributes);
        std::vector<size_t> count(size, 0);
        for (int64_t root : position) {
            roots.assign(1, root);
            walk(roots, [&](int64_t value) { ++count[value]; });
        }
        size_t lanes = position.size();
        std::vector<size_t> start(size + 1, 0);
        for (size_t value = 0; value < size; ++value)
            for (const int64_t *item = operand(value, 0); item != operand(value, 1); ++item) ++start[*item + 1];
        for (size_t value = 0; value < size; ++value) start[value + 1] += start[value];
        std::vector<int64_t> consumers(start[size]);
        std::vector<size_t> next(start.begin(), start.end() - 1);
        for (size_t value = 0; value < size; ++value)
            for (const int64_t *item = operand(value, 0); item != operand(value, 1); ++item)
                consumers[next[*item]++] = static_cast<int64_t>(value);
        std::vector<char> reaching(size, 0);
        std::vector<int64_t> pending;
        for (int64_t item : attributes)
            if (!reaching[item]) {
                reaching[item] = 1;
                pending.push_back(item);
            }
        while (!pending.empty()) {
            int64_t current = pending.back();
            pending.pop_back();
            for (size_t k = start[current]; k < start[current + 1]; ++k)
                if (!reaching[consumers[k]]) {
                    reaching[consumers[k]] = 1;
                    pending.push_back(consumers[k]);
                }
        }
        std::vector<int64_t> found, taken;
        Ref consumed(check(PyList_New(0)));
        for (size_t value = 0; value < size; ++value) {
            if (count[value] != lanes || !reaching[value]) continue;
            taken.clear();
            for (size_t k = start[value]; k < start[value + 1]; ++k)
                if (count[consumers[k]] && count[consumers[k]] < lanes) taken.push_back(consumers[k]);
            if (taken.empty()) continue;
            taken.erase(std::unique(taken.begin(), taken.end()), taken.end());
            found.push_back(static_cast<int64_t>(value));
            Ref item(numbers(taken));
            check(PyList_Append(consumed.get(), item.get()));
        }
        Ref edge(numbers(found)), lists(check(PyList_AsTuple(consumed.get())));
        return check(PyTuple_Pack(2, edge.get(), lists.get()));
    }

    PyObject *tail_shape(PyObject *frontier_object, PyObject *roots_object) {
        std::vector<int64_t> frontier, roots;
        read_indices(frontier_object, static_cast<Py_ssize_t>(kinds_.size()), frontier);
        read_indices(roots_object, static_cast<Py_ssize_t>(kinds_.size()), roots);
        std::unordered_map<int64_t, int64_t> shapes;
        for (int64_t item : frontier) shapes[item] = 0;
        Numbering shaped(1);
        std::vector<int64_t> pending(roots), missing, key;
        while (!pending.empty()) {
            int64_t current = pending.back();
            if (shapes.count(current)) {
                pending.pop_back();
                continue;
            }
            missing.clear();
            for (const int64_t *item = operand(current, 0); item != operand(current, 1); ++item)
                if (!shapes.count(*item)) missing.push_back(*item);
            if (!missing.empty()) {
                pending.insert(pending.end(), missing.begin(), missing.end());
                continue;
            }
            pending.pop_back();
            key.assign(1, identity_[current]);
            for (const int64_t *item = operand(current, 0); item != operand(current, 1); ++item)
                key.push_back(shapes[*item]);
            std::sort(key.begin() + 1, key.end());
            shapes[current] = shaped.of(key);
        }
        std::vector<int64_t> result;
        for (int64_t root : roots) result.push_back(shapes[root]);
        return numbers(result);
    }

private:
    static bool same(PyObject *kind, PyObject *text) {
        if (kind == text) return true;
        int equal = PyObject_RichCompareBool(kind, text, Py_EQ);
        check(equal);
        return equal != 0;
    }

    template <typename Visit> void walk(const std::vector<int64_t> &roots, Visit visit) {
        uint32_t seen = ++generation_;
        std::vector<int64_t> &pending = pending_;
        pending.assign(roots.begin(), roots.end());
        while (!pending.empty()) {
            int64_t current = pending.back();
            pending.pop_back();
            if (reached_[current] == seen) continue;
            reached_[current] = seen;
            visit(current);
            pending.insert(pending.end(), operand(current, 0), operand(current, 1));
        }
    }

    const int64_t *operand(int64_t value, int end) const {
        return dependencies_.data() + (end ? operands_end_[value] : begin_[value]);
    }
    const int64_t *controls_end(int64_t value) const { return dependencies_.data() + begin_[value + 1]; }

    static void read_indices(PyObject *items, Py_ssize_t count, std::vector<int64_t> &result) {
        Ref sequence(check(PySequence_Fast(items, "flow value indices must be a sequence")));
        for (Py_ssize_t index = 0; index < PySequence_Fast_GET_SIZE(sequence.get()); ++index) {
            Py_ssize_t item = PyLong_AsSsize_t(PySequence_Fast_GET_ITEM(sequence.get(), index));
            if (item == -1 && PyErr_Occurred()) throw Fail();
            if (item < 0 || item >= count) {
                PyErr_SetString(PyExc_IndexError, "flow value index out of range");
                throw Fail();
            }
            result.push_back(item);
        }
    }

    static PyObject *numbers(const std::vector<int64_t> &items) {
        Ref result(check(PyTuple_New(static_cast<Py_ssize_t>(items.size()))));
        for (size_t index = 0; index < items.size(); ++index)
            PyTuple_SET_ITEM(result.get(), static_cast<Py_ssize_t>(index), check(PyLong_FromLongLong(items[index])));
        return result.release();
    }

    void components(std::vector<int64_t> &members, std::vector<size_t> &ends) const {
        size_t size = kinds_.size();
        std::vector<int64_t> order(size, -1), low(size, 0), stack;
        std::vector<char> on_stack(size, 0);
        std::vector<std::pair<int64_t, size_t>> work;
        int64_t counter = 0;
        for (size_t root = 0; root < size; ++root) {
            if (order[root] >= 0) continue;
            order[root] = low[root] = counter++;
            stack.push_back(static_cast<int64_t>(root));
            on_stack[root] = 1;
            work.emplace_back(static_cast<int64_t>(root), begin_[root]);
            while (!work.empty()) {
                int64_t node = work.back().first;
                size_t next = work.back().second;
                if (next < begin_[node + 1]) {
                    work.back().second = next + 1;
                    int64_t child = dependencies_[next];
                    if (order[child] < 0) {
                        order[child] = low[child] = counter++;
                        stack.push_back(child);
                        on_stack[child] = 1;
                        work.emplace_back(child, begin_[child]);
                    } else if (on_stack[child]) {
                        low[node] = std::min(low[node], order[child]);
                    }
                    continue;
                }
                work.pop_back();
                if (!work.empty()) low[work.back().first] = std::min(low[work.back().first], low[node]);
                if (low[node] == order[node]) {
                    size_t start = members.size();
                    for (;;) {
                        int64_t item = stack.back();
                        stack.pop_back();
                        on_stack[item] = 0;
                        members.push_back(item);
                        if (item == node) break;
                    }
                    std::sort(members.begin() + static_cast<std::ptrdiff_t>(start), members.end());
                    ends.push_back(members.size());
                }
            }
        }
    }

    template <typename Inner> void key_of(std::vector<int64_t> &key, int64_t tag, int64_t value, Inner inner) const {
        key.assign({tag, identity_[value], static_cast<int64_t>(operands_end_[value] - begin_[value])});
        for (const int64_t *item = operand(value, 0); item != operand(value, 1); ++item) key.push_back(inner(*item));
        size_t controls = key.size();
        for (const int64_t *item = operand(value, 1); item != controls_end(value); ++item) key.push_back(inner(*item));
        std::sort(key.begin() + 3, key.begin() + static_cast<std::ptrdiff_t>(controls));
        std::sort(key.begin() + static_cast<std::ptrdiff_t>(controls), key.end());
    }

    void sign() {
        if (signed_) return;
        size_t size = kinds_.size();
        signatures_.assign(size, -1);
        std::vector<int64_t> colour(size, 0), updated, members, key;
        std::vector<char> member(size, 0);
        std::vector<size_t> ends;
        members.reserve(size);
        components(members, ends);
        Numbering interned, local;
        auto outer = [&](int64_t item) { return signatures_[item]; };
        auto inner = [&](int64_t item) { return member[item] ? -1 - colour[item] : signatures_[item]; };
        for (size_t k = 0, start = 0; k < ends.size(); start = ends[k++]) {
            const int64_t *first = members.data() + start, *last = members.data() + ends[k];
            if (last - first == 1 && std::find(operand(*first, 0), controls_end(*first), *first) == controls_end(*first)) {
                key_of(key, 0, *first, outer);
                signatures_[*first] = interned.of(key);
                continue;
            }
            for (const int64_t *index = first; index != last; ++index) {
                member[*index] = 1;
                colour[*index] = 0;
            }
            size_t colours = 1;
            updated.resize(static_cast<size_t>(last - first));
            for (;;) {
                local.clear();
                for (const int64_t *index = first; index != last; ++index) {
                    key_of(key, 0, *index, inner);
                    updated[static_cast<size_t>(index - first)] = local.of(key);
                }
                if (local.size() == colours) break;
                colours = local.size();
                for (const int64_t *index = first; index != last; ++index) colour[*index] = updated[static_cast<size_t>(index - first)];
            }
            for (const int64_t *index = first; index != last; ++index) {
                key_of(key, 1, *index, inner);
                signatures_[*index] = interned.of(key);
            }
            for (const int64_t *index = first; index != last; ++index) member[*index] = 0;
        }
        signed_ = true;
    }

    bool separated(const std::vector<int64_t> &roots, const std::vector<int64_t> &members, std::vector<int64_t> &region,
                   std::vector<int64_t> &frontier) {
        ++generation_;
        for (int64_t item : members) member_[item] = generation_;
        region.clear();
        frontier.clear();
        std::vector<int64_t> &pending = pending_;
        pending.assign(roots.begin(), roots.end());
        while (!pending.empty()) {
            int64_t current = pending.back();
            pending.pop_back();
            if (member_[current] == generation_) {
                if (frontier_[current] != generation_) {
                    frontier_[current] = generation_;
                    frontier.push_back(current);
                }
                continue;
            }
            if (region_[current] == generation_) continue;
            if (kinds_[current] != PURE) return false;
            region_[current] = generation_;
            region.push_back(current);
            pending.insert(pending.end(), operand(current, 0), operand(current, 1));
        }
        return true;
    }

    std::vector<Kind> kinds_;
    std::vector<int64_t> identity_, dependencies_, signatures_, pending_;
    std::vector<size_t> begin_, operands_end_;
    std::vector<uint32_t> reached_, member_, region_, frontier_;
    uint32_t generation_ = 0;
    bool signed_ = false;
};

struct GraphObject {
    PyObject_HEAD
    Graph *graph;
};

Graph &graph_of(PyObject *self) { return *reinterpret_cast<GraphObject *>(self)->graph; }

PyObject *graph_new(PyTypeObject *type, PyObject *arguments, PyObject *keywords) {
    PyObject *values, *value_type;
    static const char *names[] = {"values", "value_type", nullptr};
    if (!PyArg_ParseTupleAndKeywords(arguments, keywords, "OO:Graph", const_cast<char **>(names), &values, &value_type))
        return nullptr;
    PyObject *self = reinterpret_cast<allocfunc>(PyType_GetSlot(type, Py_tp_alloc))(type, 0);
    if (!self) return nullptr;
    reinterpret_cast<GraphObject *>(self)->graph = nullptr;
    PyObject *made = guarded([&] {
        reinterpret_cast<GraphObject *>(self)->graph = new Graph(values, value_type);
        return self;
    });
    if (!made) Py_DECREF(self);
    return made;
}

void graph_dealloc(PyObject *self) {
    PyTypeObject *type = Py_TYPE(self);
    delete reinterpret_cast<GraphObject *>(self)->graph;
    reinterpret_cast<freefunc>(PyType_GetSlot(type, Py_tp_free))(self);
    Py_DECREF(type);
}

PyObject *graph_signatures(PyObject *self, PyObject *) {
    return guarded([&] { return graph_of(self).signatures(); });
}

PyObject *graph_packing(PyObject *self, PyObject *arguments) {
    PyObject *roots;
    Py_ssize_t width;
    if (!PyArg_ParseTuple(arguments, "On:packing", &roots, &width)) return nullptr;
    return guarded([&] { return graph_of(self).packing(roots, width); });
}

PyObject *graph_tail_shape(PyObject *self, PyObject *arguments) {
    PyObject *frontier, *roots;
    if (!PyArg_ParseTuple(arguments, "OO:tail_shape", &frontier, &roots)) return nullptr;
    return guarded([&] { return graph_of(self).tail_shape(frontier, roots); });
}

PyObject *graph_reaches(PyObject *self, PyObject *arguments) {
    PyObject *root_sets, *marked;
    if (!PyArg_ParseTuple(arguments, "OO:reaches", &root_sets, &marked)) return nullptr;
    return guarded([&] { return graph_of(self).reaches(root_sets, marked); });
}

PyObject *graph_frontier(PyObject *self, PyObject *arguments) {
    PyObject *position, *attributes;
    if (!PyArg_ParseTuple(arguments, "OO:frontier", &position, &attributes)) return nullptr;
    return guarded([&] { return graph_of(self).frontier(position, attributes); });
}

PyMethodDef graph_methods[] = {
    {"signatures", graph_signatures, METH_NOARGS, "signatures() -> list: surface_flow.parallel_signatures"},
    {"packing", graph_packing, METH_VARARGS,
     "packing(roots, width) -> (tail, frontier): the widest packing of the roots' values whose frontier holds 1 to\n"
     "width values"},
    {"reaches", graph_reaches, METH_VARARGS,
     "reaches(root_sets, marked) -> list: for each set of roots, the marked values it reaches through operands"},
    {"frontier", graph_frontier, METH_VARARGS,
     "frontier(position, attributes) -> (values, consumers): surface_data._projection's frontier"},
    {"tail_shape", graph_tail_shape, METH_VARARGS,
     "tail_shape(frontier, roots) -> tuple: the roots' steps from the frontier, numbered by shape"},
    {nullptr, nullptr, 0, nullptr},
};

PyType_Slot graph_slots[] = {
    {Py_tp_new, reinterpret_cast<void *>(graph_new)},
    {Py_tp_dealloc, reinterpret_cast<void *>(graph_dealloc)},
    {Py_tp_methods, graph_methods},
    {Py_tp_doc, const_cast<char *>(
        "Graph(values, value_type)\n"
        "A native flow's values (each a ``value_type``: kind, operands, controls, signature), read once.")},
    {0, nullptr},
};

PyType_Spec graph_spec = {"fastsurface.Graph", sizeof(GraphObject), 0, Py_TPFLAGS_DEFAULT, graph_slots};

PyObject *graph_error() {
    static PyObject *type = nullptr;
    if (!type) {
        Ref module(check(PyImport_ImportModule("file_handlers.sdf.shader_graph")));
        type = check(PyObject_GetAttrString(module.get(), "ShaderGraphError"));
    }
    return type;
}

[[noreturn]] void raise_graph(const char *message) {
    PyErr_SetString(graph_error(), message);
    throw Fail();
}

class Attribute {
public:
    Attribute(PyObject *type, PyObject *name) : name_(name) {
        if (!PyType_Check(type)) return;
        type_ = reinterpret_cast<PyTypeObject *>(type);
        PyObject *descriptor = _PyType_LookupRef(type_, name);
        if (descriptor && Py_IS_TYPE(descriptor, &PyMemberDescr_Type)
            && reinterpret_cast<PyMemberDescrObject *>(descriptor)->d_member->type == Py_T_OBJECT_EX)
            offset_ = reinterpret_cast<PyMemberDescrObject *>(descriptor)->d_member->offset;
        Py_XDECREF(descriptor);
    }
    PyObject *get(PyObject *object) const {
        if (offset_ >= 0 && Py_TYPE(object) == type_) {
            PyObject *value = *reinterpret_cast<PyObject **>(reinterpret_cast<char *>(object) + offset_);
            if (value) return Py_NewRef(value);
        }
        return check(PyObject_GetAttr(object, name_));
    }
private:
    PyTypeObject *type_ = nullptr;
    PyObject *name_;
    Py_ssize_t offset_ = -1;
};

PyObject *unpacked(PyObject *iterable) {
    PyObject *list = PySequence_List(iterable);
    if (!list && PyErr_ExceptionMatches(PyExc_TypeError) && !Py_TYPE(iterable)->tp_iter && !PySequence_Check(iterable)) {
        PyErr_Clear();
        PyErr_Format(PyExc_TypeError, "Value after * must be an iterable, not %.200s", Py_TYPE(iterable)->tp_name);
    }
    return check(list);
}

bool within(PyObject *item, PyObject *zero, PyObject *limit, Py_ssize_t count) {
    if (PyLong_CheckExact(item)) {
        int overflow = 0;
        long long value = PyLong_AsLongLongAndOverflow(item, &overflow);
        if (value == -1 && PyErr_Occurred()) throw Fail();
        return !overflow && value >= 0 && value < count;
    }
    int low = PyObject_RichCompareBool(zero, item, Py_LE);
    check(low);
    if (!low) return false;
    int high = PyObject_RichCompareBool(item, limit, Py_LT);
    check(high);
    return high != 0;
}

PyObject *check_flow(PyObject *values, PyObject *stores, PyObject *discards, PyObject *value_type, PyObject *store_type) {
    static PyObject *const pure = interned("pure");
    static PyObject *kinds = nullptr;
    if (!kinds) {
        Ref names(check(Py_BuildValue("(sss)", "pure", "source", "join")));
        kinds = check(PyFrozenSet_New(names.get()));
    }
    static PyObject *const names[] = {interned("kind"), interned("operands"), interned("controls"), interned("value")};
    Attribute kind(value_type, names[0]), operands(value_type, names[1]), controls(value_type, names[2]),
        stored(store_type, names[3]);
    Py_ssize_t count = PyObject_Length(values);
    if (count < 0) throw Fail();
    Ref zero(check(PyLong_FromLong(0))), limit(check(PyLong_FromSsize_t(count)));
    Ref iterator(check(PyObject_GetIter(values)));
    while (PyObject *item = PyIter_Next(iterator.get())) {
        Ref value(item);
        Ref value_kind(kind.get(value.get()));
        int known = PySet_Contains(kinds, value_kind.get());
        check(known);
        if (!known) raise_graph("Native dataflow has an unknown value kind.");
        int arithmetic = PyObject_RichCompareBool(value_kind.get(), pure, Py_EQ);
        check(arithmetic);
        if (arithmetic) {
            Ref inputs(operands.get(value.get()));
            int any = PyObject_IsTrue(inputs.get());
            check(any);
            if (!any) raise_graph("Native dataflow has arithmetic without operands.");
        }
        Ref first(operands.get(value.get()));
        Ref read_operands(unpacked(first.get()));
        Ref second(controls.get(value.get()));
        Ref read_controls(unpacked(second.get()));
        for (PyObject *list : {read_operands.get(), read_controls.get()})
            for (Py_ssize_t index = 0; index < PyList_GET_SIZE(list); ++index)
                if (!within(PyList_GET_ITEM(list, index), zero.get(), limit.get(), count))
                    raise_graph("Native dataflow references an undefined value.");
    }
    if (PyErr_Occurred()) throw Fail();
    Ref all_stores(unpacked(stores)), all_discards(unpacked(discards));
    for (PyObject *list : {all_stores.get(), all_discards.get()})
        for (Py_ssize_t index = 0; index < PyList_GET_SIZE(list); ++index) {
            Ref value(stored.get(PyList_GET_ITEM(list, index)));
            if (value.get() != Py_None && !within(value.get(), zero.get(), limit.get(), count))
                raise_graph("Native dataflow stores an undefined value.");
        }
    Py_RETURN_NONE;
}

PyObject *py_check_flow(PyObject *, PyObject *arguments) {
    PyObject *values, *stores, *discards, *value_type, *store_type;
    if (!PyArg_ParseTuple(arguments, "OOOOO:check_flow", &values, &stores, &discards, &value_type, &store_type))
        return nullptr;
    return guarded([&] { return check_flow(values, stores, discards, value_type, store_type); });
}

PyObject *const *column_names() {
    static PyObject *const names[] = {interned("kind"), interned("operands"), interned("controls"),
                                      interned("signature"), interned("scalar"), interned("origin")};
    return names;
}

PyObject *value_columns(PyObject *values, PyObject *value_type) {
    Fields fields(value_type, column_names(), 6);
    if (!PyTuple_CheckExact(values)) Py_RETURN_NONE;
    Py_ssize_t count = PyTuple_GET_SIZE(values);
    PyObject *const *items = PySequence_Fast_ITEMS(values);
    for (Py_ssize_t index = 0; index < count; ++index)
        if (!fields.complete(items[index])) Py_RETURN_NONE;
    std::vector<PyObject *> sorted(items, items + count);
    std::sort(sorted.begin(), sorted.end());
    if (std::adjacent_find(sorted.begin(), sorted.end()) != sorted.end()) Py_RETURN_NONE;
    Ref columns(check(PyTuple_New(6)));
    for (int field = 0; field < 6; ++field) {
        PyObject *column = check(PyTuple_New(count));
        PyTuple_SET_ITEM(columns.get(), field, column);
        for (Py_ssize_t index = 0; index < count; ++index)
            PyTuple_SET_ITEM(column, index, Py_NewRef(fields.get(items[index], field)));
    }
    return columns.release();
}

PyObject *flow_values(PyObject *columns, PyObject *value_type) {
    Fields fields(value_type, column_names(), 6);
    if (!PyTuple_CheckExact(columns) || PyTuple_GET_SIZE(columns) != 6) {
        PyErr_SetString(PyExc_TypeError, "flow values travel as six columns");
        throw Fail();
    }
    PyObject *const *column[6];
    Py_ssize_t count = 0;
    for (int field = 0; field < 6; ++field) {
        PyObject *items = PyTuple_GET_ITEM(columns, field);
        if (!PyTuple_CheckExact(items) || (field && PyTuple_GET_SIZE(items) != count)) {
            PyErr_SetString(PyExc_TypeError, "flow value columns must be tuples of one length");
            throw Fail();
        }
        count = PyTuple_GET_SIZE(items);
        column[field] = PySequence_Fast_ITEMS(items);
    }
    Ref values(check(PyTuple_New(count)));
    for (Py_ssize_t index = 0; index < count; ++index) {
        PyObject *row[6];
        for (int field = 0; field < 6; ++field) row[field] = column[field][index];
        PyTuple_SET_ITEM(values.get(), index, fields.make(row));
    }
    return values.release();
}

PyObject *py_value_columns(PyObject *, PyObject *arguments) {
    PyObject *values, *value_type;
    if (!PyArg_ParseTuple(arguments, "OO:value_columns", &values, &value_type)) return nullptr;
    return guarded([&] { return value_columns(values, value_type); });
}

PyObject *py_flow_values(PyObject *, PyObject *arguments) {
    PyObject *columns, *value_type;
    if (!PyArg_ParseTuple(arguments, "OO:flow_values", &columns, &value_type)) return nullptr;
    return guarded([&] { return flow_values(columns, value_type); });
}

PyMethodDef methods[] = {
    {"check_flow", py_check_flow, METH_VARARGS,
     "check_flow(values, stores, discards, value_type, store_type): NativeFlow's checks, raising its errors"},
    {"value_columns", py_value_columns, METH_VARARGS,
     "value_columns(values, value_type) -> tuple | None: the values' kinds, operands, controls, signatures, scalars\n"
     "and origins, a tuple each; None unless the values are a tuple of distinct value_type objects with every field set"},
    {"flow_values", py_flow_values, METH_VARARGS,
     "flow_values(columns, value_type) -> tuple: value_type objects with value_columns' fields"},
    {nullptr, nullptr, 0, nullptr},
};

PyModuleDef module = {PyModuleDef_HEAD_INIT, "fastsurface", nullptr, -1, methods};

}

PyMODINIT_FUNC PyInit_fastsurface() {
    PyObject *made = PyModule_Create(&module);
    PyObject *graph = made ? PyType_FromSpec(&graph_spec) : nullptr;
    if (graph && PyModule_AddObject(made, "Graph", graph) == 0) return made;
    Py_XDECREF(graph);
    Py_XDECREF(made);
    return nullptr;
}
