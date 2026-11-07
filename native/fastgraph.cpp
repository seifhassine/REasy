#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <algorithm>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <deque>
#include <intrin.h>
#include <map>
#include <memory>
#include <new>
#include <string>
#include <type_traits>
#include <unordered_map>
#include <unordered_set>
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
    Ref(Ref &&other) noexcept : object_(other.release()) {}
    Ref &operator=(Ref &&other) noexcept {
        if (this != &other) {
            Py_XDECREF(object_);
            object_ = other.release();
        }
        return *this;
    }
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

struct Configuration {
    PyTypeObject *object_type = nullptr;
    PyTypeObject *array_type = nullptr;
    PyObject *invalid = nullptr;
    PyObject *semantic_text = nullptr;
    PyObject *route_tables = nullptr;
    PyObject *graph_keys = nullptr;
    PyObject *stage = nullptr;
    PyObject *contract_fingerprint = nullptr;
    PyObject *nodes = nullptr;
    PyObject *links = nullptr;
    PyObject *items = nullptr;
    PyObject *encode = nullptr;
    PyObject *utf8 = nullptr;
} config;

void require_configured() {
    if (!config.object_type) {
        PyErr_SetString(PyExc_RuntimeError, "fastgraph.configure() has not run");
        throw Fail();
    }
}

Py_ssize_t size_of(PyObject *object) {
    Py_ssize_t size = PyObject_Size(object);
    if (size < 0) throw Fail();
    return size;
}

Py_ssize_t text_length(PyObject *text) {
    return PyUnicode_CheckExact(text) ? PyUnicode_GET_LENGTH(text) : size_of(text);
}

bool container(PyObject *value) { return PyDict_Check(value) || PyList_Check(value); }

enum class Scalar { invalid, valid, container };

Scalar scalar_state(PyObject *value) {
    if (value == Py_None || PyLong_Check(value)) return Scalar::valid;
    if (PyUnicode_Check(value)) return text_length(value) <= 4096 ? Scalar::valid : Scalar::invalid;
    if (PyFloat_Check(value)) return std::isfinite(PyFloat_AS_DOUBLE(value)) ? Scalar::valid : Scalar::invalid;
    return container(value) ? Scalar::container : Scalar::invalid;
}

bool equal(PyObject *left, PyObject *right) {
    int result = PyObject_RichCompareBool(left, right, Py_EQ);
    check(result);
    return result;
}

bool contains(PyObject *collection, PyObject *key) {
    int result = PySequence_Contains(collection, key);
    check(result);
    return result;
}

void unpack_pair(PyObject *item, Ref &first, Ref &second) {
    if (PyTuple_CheckExact(item) && PyTuple_GET_SIZE(item) == 2) {
        first = Ref(Py_NewRef(PyTuple_GET_ITEM(item, 0)));
        second = Ref(Py_NewRef(PyTuple_GET_ITEM(item, 1)));
        return;
    }
    Ref iterator(check(PyObject_GetIter(item)));
    Ref parts[2];
    for (Py_ssize_t index = 0; index < 2; ++index) {
        parts[index] = Ref(PyIter_Next(iterator.get()));
        if (!parts[index].get()) {
            if (PyErr_Occurred()) throw Fail();
            PyErr_Format(PyExc_ValueError, "not enough values to unpack (expected 2, got %zd)", index);
            throw Fail();
        }
    }
    Ref extra(PyIter_Next(iterator.get()));
    if (extra.get()) {
        PyErr_SetString(PyExc_ValueError, "too many values to unpack (expected 2)");
        throw Fail();
    }
    if (PyErr_Occurred()) throw Fail();
    first = std::move(parts[0]);
    second = std::move(parts[1]);
}

template <typename Visit> void each_entry(PyObject *entries, bool mapping, Visit visit) {
    if (mapping ? PyDict_CheckExact(entries) : PyList_CheckExact(entries)) {
        if (mapping) {
            Py_ssize_t position = 0;
            PyObject *key, *value;
            while (PyDict_Next(entries, &position, &key, &value)) {
                Ref held_key(Py_NewRef(key)), held_value(Py_NewRef(value));
                if (!visit(held_key.get(), Py_ssize_t(0), held_value.get())) return;
            }
        } else {
            for (Py_ssize_t index = 0; index < PyList_GET_SIZE(entries); ++index) {
                Ref held(Py_NewRef(PyList_GET_ITEM(entries, index)));
                if (!visit(nullptr, index, held.get())) return;
            }
        }
        return;
    }
    Ref iterable(mapping ? check(PyObject_CallMethodNoArgs(entries, config.items)) : Py_NewRef(entries));
    Ref iterator(check(PyObject_GetIter(iterable.get())));
    for (Py_ssize_t index = 0;; ++index) {
        Ref item(PyIter_Next(iterator.get()));
        if (!item.get()) {
            if (PyErr_Occurred()) throw Fail();
            return;
        }
        if (mapping) {
            Ref key, value;
            unpack_pair(item.get(), key, value);
            if (!visit(key.get(), Py_ssize_t(0), value.get())) return;
        } else if (!visit(nullptr, index, item.get())) {
            return;
        }
    }
}

PyObject *interned(const char *text);

PyObject *made(PyTypeObject *type) {
    static PyObject *empty = check(PyTuple_New(0));
    return check(type->tp_new(type, empty, nullptr));
}

struct Pending {
    Ref item;
    Py_ssize_t limit;
    Ref parent;
    Ref key;
    Py_ssize_t index;
    bool leaving;
};

struct PairHash {
    size_t operator()(const std::pair<PyObject *, Py_ssize_t> &pair) const {
        return std::hash<void *>()(pair.first) * 31 + std::hash<Py_ssize_t>()(pair.second);
    }
};

class PropertyCheck {
public:
    PropertyCheck(PyObject *memo, PyObject *validated, Py_ssize_t max_nodes, Py_ssize_t max_links)
        : memo_(memo), validated_(validated), max_nodes_(max_nodes), max_links_(max_links) {}

    PyObject *run(PyObject *value) {
        active_.clear();
        completed_.clear();
        added_.clear();
        pending_.clear();
        try {
            PyObject *result = walk(value);
            if (result == config.invalid) rollback();
            return result;
        } catch (...) {
            PyObject *type, *error, *trace;
            PyErr_Fetch(&type, &error, &trace);
            rollback();
            PyErr_Restore(type, error, trace);
            throw;
        }
    }

private:
    PyObject *memo_;
    PyObject *validated_;
    Py_ssize_t max_nodes_, max_links_;
    std::unordered_set<PyObject *> active_;
    std::unordered_set<std::pair<PyObject *, Py_ssize_t>, PairHash> completed_;
    std::vector<PyObject *> added_;
    std::vector<Pending> pending_;

    static PyObject *identity(PyObject *item) { return check(PyLong_FromVoidPtr(item)); }

    PyObject *validated_key(PyObject *item, Py_ssize_t limit) {
        Ref address(identity(item));
        Ref size(check(PyLong_FromSsize_t(limit)));
        return check(PyTuple_Pack(2, address.get(), size.get()));
    }

    void rollback() {
        for (PyObject *item : added_) {
            Ref key(PyLong_FromVoidPtr(item));
            if (!key.get() || PyDict_DelItem(memo_, key.get()) < 0) PyErr_Clear();
        }
        added_.clear();
    }

    void push(PyObject *item, Py_ssize_t limit, PyObject *parent, PyObject *key, Py_ssize_t index, bool leaving) {
        pending_.push_back(Pending{Ref(Py_NewRef(item)), limit, Ref(Py_XNewRef(parent)), Ref(Py_XNewRef(key)), index,
                                   leaving});
    }

    bool embedded(PyObject *item, bool exact) {
        if (exact) {
            return PyDict_GET_SIZE(item) == 4 && contains(item, config.stage)
                && contains(item, config.contract_fingerprint) && contains(item, config.nodes)
                && contains(item, config.links);
        }
        Ref keys(check(PySet_New(item)));
        return equal(keys.get(), config.graph_keys);
    }

    PyObject *walk(PyObject *value) {
        Ref result(Py_NewRef(value));
        push(value, 2048, nullptr, nullptr, 0, false);
        while (!pending_.empty()) {
            Pending entry = std::move(pending_.back());
            pending_.pop_back();
            PyObject *item = entry.item.get();
            if (entry.leaving) {
                active_.erase(item);
                if (validated_) {
                    Ref key(validated_key(item, entry.limit));
                    check(PyDict_SetItem(validated_, key.get(), item));
                } else if (!memo_) {
                    completed_.insert({item, entry.limit});
                }
                continue;
            }
            bool mapping = PyDict_Check(item);
            if (size_of(item) > (mapping ? 64 : entry.limit) || active_.count(item)) return Py_NewRef(config.invalid);
            Ref owned;
            if (PyObject_TypeCheck(item, config.object_type) || PyObject_TypeCheck(item, config.array_type)) {
                owned = Ref(Py_NewRef(item));
            } else if (memo_ && [&] {
                           Ref key(identity(item));
                           PyObject *found;
                           check(PyDict_GetItemRef(memo_, key.get(), &found));
                           owned = Ref(found);
                           return found != nullptr;
                       }()) {
            } else if (!memo_ && [&] {
                           if (!validated_) return completed_.count({item, entry.limit}) != 0;
                           Ref key(validated_key(item, entry.limit));
                           return contains(validated_, key.get());
                       }()) {
                continue;
            } else {
                bool exact = mapping ? PyDict_CheckExact(item) : PyList_CheckExact(item);
                Ref entries(Py_NewRef(item));
                if (memo_) {
                    if (!exact) {
                        Ref listed(mapping ? check(PyObject_CallMethodNoArgs(item, config.items)) : Py_NewRef(item));
                        entries = Ref(check(PySequence_List(listed.get())));
                    }
                    // _Object(entries) / _Array(entries): dict.__init__ merges, list.__init__ extends.
                    owned = Ref(made(mapping ? config.object_type : config.array_type));
                    if (!mapping) check(PyList_SetSlice(owned.get(), 0, 0, entries.get()));
                    else if (exact) check(PyDict_Merge(owned.get(), entries.get(), 1));
                    else check(PyDict_MergeFromSeq2(owned.get(), entries.get(), 1));
                    Ref key(identity(item));
                    check(PyDict_SetItem(memo_, key.get(), owned.get()));
                    added_.push_back(item);
                }
                active_.insert(item);
                push(item, entry.limit, nullptr, nullptr, 0, true);
                bool graph = mapping && embedded(item, exact);
                // A listed mapping's entries are (key, value) pairs; its items were read already.
                bool listed_pairs = mapping && memo_ && !exact;
                bool invalid = false;
                auto visit = [&](PyObject *child_key, Py_ssize_t index, PyObject *child) {
                    if (mapping) {
                        if (!PyUnicode_Check(child_key) || text_length(child_key) > 256) {
                            invalid = true;
                            return false;
                        }
                        if (contains(config.semantic_text, child_key) && PyUnicode_Check(child)) {
                            Ref encoded(PyUnicode_CheckExact(child) ? check(PyUnicode_AsUTF8String(child))
                                        : check(PyObject_CallMethodOneArg(child, config.encode, config.utf8)));
                            if (size_of(encoded.get()) <= 2 * 1024 * 1024) return true;
                        }
                    }
                    Scalar state = scalar_state(child);
                    if (state == Scalar::container) {
                        Py_ssize_t limit = 2048;
                        if (mapping) {
                            if (contains(config.route_tables, child_key)) limit = 65535;
                            else if (graph && equal(child_key, config.nodes)) limit = max_nodes_;
                            else if (graph && equal(child_key, config.links)) limit = max_links_;
                        }
                        push(child, limit, owned.get(), child_key, index, false);
                    } else if (state == Scalar::invalid) {
                        invalid = true;
                        return false;
                    }
                    return true;
                };
                if (listed_pairs) {
                    for (Py_ssize_t position = 0; position < PyList_GET_SIZE(entries.get()); ++position) {
                        Ref key, child;
                        unpack_pair(PyList_GET_ITEM(entries.get(), position), key, child);
                        if (!visit(key.get(), 0, child.get())) break;
                    }
                } else {
                    each_entry(entries.get(), mapping, visit);
                }
                if (invalid) return Py_NewRef(config.invalid);
            }
            if (memo_) {
                PyObject *parent = entry.parent.get();
                if (!parent) {
                    result = std::move(owned);
                } else if (Py_TYPE(parent) == config.object_type) {
                    check(PyDict_SetItem(parent, entry.key.get(), owned.get()));
                } else {
                    check(PyList_SetItem(parent, entry.index, owned.release()));
                }
            }
        }
        return result.release();
    }
};

PyObject *checked_properties(PyObject *value, PyObject *memo, PyObject *validated, Py_ssize_t max_nodes,
                             Py_ssize_t max_links) {
    require_configured();
    if (!container(value)) return Py_NewRef(scalar_state(value) == Scalar::valid ? value : config.invalid);
    if ((memo && !PyDict_CheckExact(memo)) || (validated && !PyDict_CheckExact(validated))) {
        PyErr_SetString(PyExc_TypeError, "memo and validated must be dicts or None");
        throw Fail();
    }
    return PropertyCheck(memo, validated, max_nodes, max_links).run(value);
}

PyObject *copy_properties(PyObject *value, PyObject *memo) {
    require_configured();
    if (!PyDict_CheckExact(memo)) {
        PyErr_SetString(PyExc_TypeError, "memo must be a dict");
        throw Fail();
    }
    struct Copy {
        Ref original, copied;
        bool ordinary;
    };
    std::vector<Copy> pending;
    auto clone = [&](PyObject *item) -> PyObject * {
        if (!container(item)) return Py_NewRef(item);
        Ref key(check(PyLong_FromVoidPtr(item)));
        PyObject *found;
        check(PyDict_GetItemRef(memo, key.get(), &found));
        if (found) return found;
        PyTypeObject *type = Py_TYPE(item);
        bool ordinary = type == &PyDict_Type || type == &PyList_Type || type == config.object_type
            || type == config.array_type;
        Ref result;
        if (PyDict_Check(item)) {
            result = Ref(check(PyDict_New()));
            if (ordinary) check(PyDict_Merge(result.get(), item, 1));
        } else {
            result = Ref(check(ordinary ? PySequence_List(item) : PyList_New(0)));
        }
        check(PyDict_SetItem(memo, key.get(), result.get()));
        pending.push_back(Copy{Ref(Py_NewRef(item)), Ref(Py_NewRef(result.get())), ordinary});
        return result.release();
    };
    Ref result(clone(value));
    while (!pending.empty()) {
        Copy entry = std::move(pending.back());
        pending.pop_back();
        PyObject *original = entry.original.get(), *copied = entry.copied.get();
        bool mapping = PyDict_Check(original);
        auto visit = [&](PyObject *key, Py_ssize_t index, PyObject *item) {
            if (mapping) {
                if (!entry.ordinary || container(item)) {
                    Ref copy(clone(item));
                    check(PyDict_SetItem(copied, key, copy.get()));
                }
            } else if (!entry.ordinary) {
                Ref copy(clone(item));
                check(PyList_Append(copied, copy.get()));
            } else if (container(item)) {
                check(PyList_SetItem(copied, index, clone(item)));
            }
            return true;
        };
        if (entry.ordinary) {
            // dict, list, _Object and _Array keep the built-in iteration.
            if (mapping) {
                Py_ssize_t position = 0;
                PyObject *key, *item;
                while (PyDict_Next(original, &position, &key, &item)) {
                    Ref held_key(Py_NewRef(key)), held_item(Py_NewRef(item));
                    visit(held_key.get(), 0, held_item.get());
                }
            } else {
                for (Py_ssize_t index = 0; index < PyList_GET_SIZE(original); ++index) {
                    Ref held(Py_NewRef(PyList_GET_ITEM(original, index)));
                    visit(nullptr, index, held.get());
                }
            }
        } else {
            each_entry(original, mapping, visit);
        }
    }
    return result.release();
}

void replace(PyObject *&slot, PyObject *value) {
    Py_XSETREF(slot, Py_NewRef(value));
}

PyObject *interned(const char *text) { return check(PyUnicode_InternFromString(text)); }

PyObject *py_configure(PyObject *, PyObject *arguments) {
    PyObject *object_type, *array_type, *invalid, *semantic_text, *route_tables, *graph_keys;
    if (!PyArg_ParseTuple(arguments, "O!O!OO!O!O!:configure", &PyType_Type, &object_type, &PyType_Type, &array_type,
                          &invalid, &PyFrozenSet_Type, &semantic_text, &PyFrozenSet_Type, &route_tables,
                          &PyFrozenSet_Type, &graph_keys)) {
        return nullptr;
    }
    return guarded([&] {
        if (!PyType_IsSubtype(reinterpret_cast<PyTypeObject *>(object_type), &PyDict_Type)
            || !PyType_IsSubtype(reinterpret_cast<PyTypeObject *>(array_type), &PyList_Type)) {
            PyErr_SetString(PyExc_TypeError, "configure() needs a dict and a list subclass");
            throw Fail();
        }
        Py_XSETREF(config.object_type, reinterpret_cast<PyTypeObject *>(Py_NewRef(object_type)));
        Py_XSETREF(config.array_type, reinterpret_cast<PyTypeObject *>(Py_NewRef(array_type)));
        replace(config.invalid, invalid);
        replace(config.semantic_text, semantic_text);
        replace(config.route_tables, route_tables);
        replace(config.graph_keys, graph_keys);
        if (!config.items) {
            config.stage = interned("stage");
            config.contract_fingerprint = interned("contract_fingerprint");
            config.nodes = interned("nodes");
            config.links = interned("links");
            config.items = interned("items");
            config.encode = interned("encode");
            config.utf8 = interned("utf-8");
        }
        Py_RETURN_NONE;
    });
}

PyObject *optional(PyObject *value) { return value == Py_None ? nullptr : value; }

PyObject *py_checked_properties(PyObject *, PyObject *const *arguments, Py_ssize_t count) {
    if (count != 5) {
        PyErr_SetString(PyExc_TypeError, "checked_properties(value, memo, validated, max_nodes, max_links)");
        return nullptr;
    }
    Py_ssize_t max_nodes = PyLong_AsSsize_t(arguments[3]);
    if (max_nodes == -1 && PyErr_Occurred()) return nullptr;
    Py_ssize_t max_links = PyLong_AsSsize_t(arguments[4]);
    if (max_links == -1 && PyErr_Occurred()) return nullptr;
    return guarded([&] {
        return checked_properties(arguments[0], optional(arguments[1]), optional(arguments[2]), max_nodes, max_links);
    });
}

[[noreturn]] void graph_error(PyObject *error, PyObject *message) {
    Ref held(message);
    if (message) PyErr_SetObject(error, message);
    throw Fail();
}

PyObject *pair_array(PyObject *first, PyObject *second) {
    Ref array(made(config.array_type));
    check(PyList_Append(array.get(), first));
    check(PyList_Append(array.get(), second));
    return array.release();
}

class Slots {
public:
    Slots(PyObject *type, const char *const *names, int count) : count_(count) {
        if (!PyType_Check(type)) {
            PyErr_SetString(PyExc_TypeError, "a node or link class is needed");
            throw Fail();
        }
        type_ = reinterpret_cast<PyTypeObject *>(type);
        for (int index = 0; index < count; ++index) {
            Ref name(interned(names[index]));
            PyObject *descriptor = _PyType_LookupRef(type_, name.get());
            PyMemberDef *member = descriptor && Py_IS_TYPE(descriptor, &PyMemberDescr_Type)
                ? reinterpret_cast<PyMemberDescrObject *>(descriptor)->d_member : nullptr;
            bool usable = member && member->type == Py_T_OBJECT_EX;
            if (usable) offsets_[index] = member->offset;
            Py_XDECREF(descriptor);
            if (!usable) {
                PyErr_Format(PyExc_TypeError, "%R has no slot %s", type, names[index]);
                throw Fail();
            }
            names_[index] = names[index];
        }
    }
    bool is(PyObject *object) const { return Py_TYPE(object) == type_; }
    PyObject *peek(PyObject *object, int index) const {
        return *reinterpret_cast<PyObject **>(reinterpret_cast<char *>(object) + offsets_[index]);
    }
    PyObject *get(PyObject *object, int index) const {
        PyObject *value = peek(object, index);
        if (!value) {
            PyErr_Format(PyExc_AttributeError, "'%.200s' object has no attribute '%s'", type_->tp_name, names_[index]);
            throw Fail();
        }
        return value;
    }
    // type(*items) for a dataclass whose __init__ only sets the fields.
    PyObject *make(PyObject *const *items) const {
        PyObject *object = check(type_->tp_alloc(type_, 0));
        for (int index = 0; index < count_; ++index)
            *reinterpret_cast<PyObject **>(reinterpret_cast<char *>(object) + offsets_[index]) = Py_NewRef(items[index]);
        return object;
    }
private:
    PyTypeObject *type_ = nullptr;
    int count_;
    Py_ssize_t offsets_[5] = {};
    const char *names_[5] = {};
};

const char *const node_fields[] = {"node_id", "kind", "x", "y", "properties"};
const char *const link_fields[] = {"source_node", "source_pin", "target_node", "target_pin"};

// kind == kind.strip().lower() for an exact str.
bool normalized(PyObject *kind) {
    if (PyUnicode_IS_ASCII(kind)) {
        Py_ssize_t size = PyUnicode_GET_LENGTH(kind);
        const Py_UCS1 *data = PyUnicode_1BYTE_DATA(kind);
        auto space = [](Py_UCS1 c) { return c == ' ' || (c >= 0x09 && c <= 0x0d) || (c >= 0x1c && c <= 0x1f); };
        if (size && (space(data[0]) || space(data[size - 1]))) return false;
        for (Py_ssize_t index = 0; index < size; ++index)
            if (data[index] >= 'A' && data[index] <= 'Z') return false;
        return true;
    }
    Ref stripped(check(PyObject_CallMethod(kind, "strip", nullptr)));
    Ref lowered(check(PyObject_CallMethod(stripped.get(), "lower", nullptr)));
    return equal(kind, lowered.get());
}

PyObject *snapshot_graph(PyObject *nodes, PyObject *links, PyObject *node_type, PyObject *link_type,
                         PyObject *node_kinds, PyObject *extension, PyObject *error, Py_ssize_t max_nodes,
                         Py_ssize_t max_links) {
    require_configured();
    if (!PyList_CheckExact(nodes) || !PyList_CheckExact(links)) {
        PyErr_SetString(PyExc_TypeError, "snapshot_graph needs node and link lists");
        throw Fail();
    }
    static PyObject *keys[6];
    if (!keys[0]) {
        const char *names[] = {"id", "kind", "position", "properties", "source", "target"};
        for (int index = 0; index < 6; ++index) keys[index] = interned(names[index]);
    }
    Slots node_slots(node_type, node_fields, 5), link_slots(link_type, link_fields, 4);
    Ref copied(check(PyDict_New())), identities(check(PySet_New(nullptr)));
    Ref owned_nodes(check(PyList_New(0))), node_documents(check(PyList_New(0)));
    Ref owned_links(check(PyList_New(0))), link_documents(check(PyList_New(0)));
    PropertyCheck freeze(copied.get(), nullptr, max_nodes, max_links);
    for (Py_ssize_t index = 0; index < PyList_GET_SIZE(nodes); ++index) {
        Ref source(Py_NewRef(PyList_GET_ITEM(nodes, index)));
        if (!node_slots.is(source.get())) Py_RETURN_NONE;
        PyObject *node_id = node_slots.get(source.get(), 0);
        if (!PyUnicode_CheckExact(node_id) || !PyUnicode_GET_LENGTH(node_id)) Py_RETURN_NONE;
        PyObject *kind = node_slots.get(source.get(), 1);
        if (!PyUnicode_CheckExact(kind) || !normalized(kind)) Py_RETURN_NONE;
        PyObject *x = node_slots.get(source.get(), 2), *y = node_slots.get(source.get(), 3);
        if (!PyFloat_CheckExact(x) || !PyFloat_CheckExact(y)) Py_RETURN_NONE;
        PyObject *properties = node_slots.get(source.get(), 4);
        if (Py_TYPE(properties) != &PyDict_Type && Py_TYPE(properties) != config.object_type) Py_RETURN_NONE;
        Ref held_id(Py_NewRef(node_id)), held_kind(Py_NewRef(kind)), held_x(Py_NewRef(x)), held_y(Py_NewRef(y));
        Ref held_properties(Py_NewRef(properties));
        if (PyUnicode_GET_LENGTH(kind) > 4096) {
            graph_error(error, PyUnicode_FromString(
                "Compiler properties must be finite JSON within their declared size limits."));
        }
        Ref frozen(freeze.run(properties));
        if (frozen.get() == config.invalid) {
            graph_error(error, PyUnicode_FromString(
                "Compiler properties must be finite JSON within their declared size limits."));
        }
        if (!std::isfinite(PyFloat_AS_DOUBLE(x)) || !std::isfinite(PyFloat_AS_DOUBLE(y))) {
            graph_error(error, PyUnicode_FromString("shader graph node position must be finite"));
        }
        if (!contains(node_kinds, kind)) {
            Ref found(check(PyObject_CallOneArg(extension, kind)));
            if (found.get() == Py_None) {
                graph_error(error, PyUnicode_FromFormat("unknown shader graph node kind %R", kind));
            }
        }
        if (PyUnicode_GET_LENGTH(node_id) > 256 || contains(identities.get(), node_id)) {
            graph_error(error, PyUnicode_FromFormat("duplicate or empty shader graph node id %R", node_id));
        }
        Ref values(made(&PyDict_Type));
        check(PyDict_Merge(values.get(), frozen.get(), 1));
        PyObject *items[] = {node_id, kind, x, y, values.get()};
        Ref node(node_slots.make(items));
        check(PySet_Add(identities.get(), node_id));
        check(PyList_Append(owned_nodes.get(), node.get()));
        Ref document(made(config.object_type));
        Ref position(pair_array(x, y));
        check(PyDict_SetItem(document.get(), keys[0], node_id));
        check(PyDict_SetItem(document.get(), keys[1], kind));
        check(PyDict_SetItem(document.get(), keys[2], position.get()));
        check(PyDict_SetItem(document.get(), keys[3], frozen.get()));
        check(PyList_Append(node_documents.get(), document.get()));
    }
    for (Py_ssize_t index = 0; index < PyList_GET_SIZE(links); ++index) {
        Ref source(Py_NewRef(PyList_GET_ITEM(links, index)));
        if (!link_slots.is(source.get())) Py_RETURN_NONE;
        PyObject *fields[4];
        for (int part = 0; part < 4; ++part) fields[part] = link_slots.get(source.get(), part);
        for (PyObject *part : fields)
            if (!PyUnicode_CheckExact(part)) Py_RETURN_NONE;
        for (PyObject *part : fields) {
            if (!PyUnicode_GET_LENGTH(part) || PyUnicode_GET_LENGTH(part) > 256) {
                graph_error(error, PyUnicode_FromString("shader graph link endpoint is empty or too long"));
            }
        }
        check(PyList_Append(owned_links.get(), source.get()));
        Ref document(made(config.object_type));
        Ref from(pair_array(fields[0], fields[1])), to(pair_array(fields[2], fields[3]));
        check(PyDict_SetItem(document.get(), keys[4], from.get()));
        check(PyDict_SetItem(document.get(), keys[5], to.get()));
        check(PyList_Append(link_documents.get(), document.get()));
    }
    return check(PyTuple_Pack(4, owned_nodes.get(), node_documents.get(), owned_links.get(), link_documents.get()));
}

PyObject *py_snapshot_graph(PyObject *, PyObject *const *arguments, Py_ssize_t count) {
    if (count != 9) {
        PyErr_SetString(PyExc_TypeError, "snapshot_graph(nodes, links, node_type, link_type, node_kinds, extension, "
                                         "error, max_nodes, max_links)");
        return nullptr;
    }
    Py_ssize_t max_nodes = PyLong_AsSsize_t(arguments[7]);
    if (max_nodes == -1 && PyErr_Occurred()) return nullptr;
    Py_ssize_t max_links = PyLong_AsSsize_t(arguments[8]);
    if (max_links == -1 && PyErr_Occurred()) return nullptr;
    return guarded([&] {
        return snapshot_graph(arguments[0], arguments[1], arguments[2], arguments[3], arguments[4], arguments[5],
                              arguments[6], max_nodes, max_links);
    });
}

bool truth(PyObject *value) {
    int result = PyObject_IsTrue(value);
    check(result);
    return result;
}

bool same(PyObject *left, PyObject *right) {
    if (PyUnicode_CheckExact(left) && PyUnicode_CheckExact(right)) return PyUnicode_Equal(left, right);
    Ref result(check(PyObject_RichCompare(left, right, Py_EQ)));
    return truth(result.get());
}

PyObject *attribute(PyObject *object, PyObject *name) { return check(PyObject_GetAttr(object, name)); }

PyObject *member(PyObject *dict, PyObject *key) {
    PyObject *value;
    int found = PyDict_GetItemRef(dict, key, &value);
    check(found);
    return value;
}

template <typename Visit> void each(PyObject *items, Visit visit) {
    auto step = [&](PyObject *item) {
        if constexpr (std::is_void_v<decltype(visit(item))>) {
            visit(item);
            return true;
        } else {
            return bool(visit(item));
        }
    };
    if (PyList_CheckExact(items) || PyTuple_CheckExact(items)) {
        for (Py_ssize_t index = 0; index < Py_SIZE(items); ++index) {
            Ref item(Py_NewRef(PySequence_Fast_GET_ITEM(items, index)));
            if (!step(item.get())) return;
        }
        return;
    }
    Ref iterator(check(PyObject_GetIter(items)));
    for (;;) {
        Ref item(PyIter_Next(iterator.get()));
        if (!item.get()) {
            if (PyErr_Occurred()) throw Fail();
            return;
        }
        if (!step(item.get())) return;
    }
}

struct GraphNames {
    PyObject *kind, *properties, *node_id, *pin_id, *boundary, *value_type, *name, *label, *direction, *required,
        *source_node, *source_pin, *target_node, *target_pin, *function_input, *function_output, *input, *output,
        *get, *empty, *sum, *error, *code_node, *code_interface, *code_nodes, *code_link, *code_pin, *code_input,
        *code_type, *code_cycle, *interface, *duplicate, *dangling, *missing, *multiple, *cycle, *x, *y, *pop, *node,
        *pin, *links, *append_node, *connect, *connected_targets, *incoming_links, *add, *local_kinds, *nodes,
        *graph, *branch, *repeat, *unassigned_value, *material_surface, *value, *condition, *execution, *outputs,
        *stage, *contract_fingerprint, *id, *position, *source, *target, *recovered_asset_digest, *code_fork, *fork,
        *node_keys, *link_keys;
};

const GraphNames &graph_names() {
    static const GraphNames names = [] {
        Ref builtins(check(PyImport_ImportModule("builtins")));
        return GraphNames{
            interned("kind"), interned("properties"), interned("node_id"), interned("pin_id"),
            interned("semantic_reconstruction_boundary"), interned("value_type"), interned("name"), interned("label"),
            interned("direction"), interned("required"), interned("source_node"), interned("source_pin"),
            interned("target_node"), interned("target_pin"), interned("function_input"), interned("function_output"),
            interned("input"), interned("output"), interned("get"), interned(""),
            check(PyObject_GetAttrString(builtins.get(), "sum")), interned("error"), interned("function-node"),
            interned("function-interface"), interned("function-nodes"), interned("function-link"),
            interned("function-pin"), interned("function-input"), interned("function-type"),
            interned("function-cycle"), interned("Function input/output nodes do not match the published interface."),
            interned("Function node IDs are not unique."), interned("Function has a dangling connection."),
            interned("Function connection uses a missing pin."), interned("A function input has multiple connections."),
            interned("Material functions cannot contain loops."), interned("x"), interned("y"), interned("pop"),
            interned("node"), interned("pin"), interned("links"), interned("append_node"), interned("connect"),
            interned("_connected_targets"), interned("_incoming_links"), interned("add"), [] {
                Ref kinds(check(PyTuple_Pack(2, interned("constant"), interned("unassigned_value"))));
                return check(PyFrozenSet_New(kinds.get()));
            }(), interned("nodes"), interned("graph"), interned("branch"), interned("repeat"),
            interned("unassigned_value"), interned("material_surface"), interned("value"), interned("condition"),
            interned("execution"), interned("outputs"), interned("stage"), interned("contract_fingerprint"),
            interned("id"), interned("position"), interned("source"), interned("target"),
            interned("recovered_asset_digest"), interned("execution-fork"),
            interned("Connect execution through one ordered continuation; this live operation has multiple "
                     "successors."), [] {
                Ref keys(check(PyTuple_Pack(4, interned("id"), interned("kind"), interned("position"),
                                            interned("properties"))));
                return check(PyFrozenSet_New(keys.get()));
            }(), [] {
                Ref keys(check(PyTuple_Pack(2, interned("source"), interned("target"))));
                return check(PyFrozenSet_New(keys.get()));
            }()};
    }();
    return names;
}

PyObject *mapping_get(PyObject *mapping, PyObject *key, PyObject *fallback) {
    if (PyDict_CheckExact(mapping)) {
        PyObject *value = member(mapping, key);
        return value ? value : Py_NewRef(fallback);
    }
    return check(PyObject_CallMethodObjArgs(mapping, graph_names().get, key, fallback, nullptr));
}

bool same_key(PyObject *stored, PyObject *key) {
    if (PyUnicode_CheckExact(stored) && PyUnicode_CheckExact(key)) return PyUnicode_Equal(stored, key);
    return equal(stored, key);
}

// The caller holds the keys.
class FlatMap {
public:
    Py_ssize_t size() const { return used_; }

    Py_ssize_t find(uint64_t tag, PyObject *key) {
        Py_hash_t hash = hashed(key);
        return entries_.empty() ? -1 : entries_[locate(tag, key, hash)].number;
    }

    Py_ssize_t add(uint64_t tag, PyObject *key) {
        Py_hash_t hash = hashed(key);
        if (2 * size_t(used_ + 1) > entries_.size()) grow();
        Entry &entry = entries_[locate(tag, key, hash)];
        if (entry.number < 0) entry = Entry{tag, hash, key, used_++};
        return entry.number;
    }

private:
    struct Entry {
        uint64_t tag;
        Py_hash_t hash;
        PyObject *key;
        Py_ssize_t number = -1;
    };

    static Py_hash_t hashed(PyObject *key) {
        Py_hash_t hash = key ? PyObject_Hash(key) : 0;
        if (hash == -1) throw Fail();
        return hash;
    }

    static size_t mixed(uint64_t tag, Py_hash_t hash) {
        uint64_t value = tag * 0x9E3779B97F4A7C15ull ^ uint64_t(hash);
        value = (value ^ value >> 30) * 0xBF58476D1CE4E5B9ull;
        value = (value ^ value >> 27) * 0x94D049BB133111EBull;
        return size_t(value ^ value >> 31);
    }

    size_t locate(uint64_t tag, PyObject *key, Py_hash_t hash) {
        size_t mask = entries_.size() - 1;
        for (size_t at = mixed(tag, hash) & mask;; at = (at + 1) & mask) {
            const Entry &entry = entries_[at];
            if (entry.number < 0 || (entry.tag == tag && entry.hash == hash
                                     && (entry.key == key || (key && entry.key && same_key(entry.key, key)))))
                return at;
        }
    }

    void grow() {
        std::vector<Entry> previous(std::max<size_t>(16, 2 * entries_.size()));
        previous.swap(entries_);
        size_t mask = entries_.size() - 1;
        for (const Entry &entry : previous) {
            if (entry.number < 0) continue;
            size_t at = mixed(entry.tag, entry.hash) & mask;
            while (entries_[at].number >= 0) at = (at + 1) & mask;
            entries_[at] = entry;
        }
    }

    std::vector<Entry> entries_;
    Py_ssize_t used_ = 0;
};

using Edges = std::vector<std::pair<Py_ssize_t, Py_ssize_t>>;

struct Adjacency {
    std::vector<Py_ssize_t> offsets, ends;

    Adjacency(Py_ssize_t count, const Edges &edges, bool reversed) : offsets(size_t(count) + 1), ends(edges.size()) {
        for (const auto &[from, to] : edges) ++offsets[size_t(reversed ? to : from) + 1];
        for (size_t index = 1; index < offsets.size(); ++index) offsets[index] += offsets[index - 1];
        std::vector<Py_ssize_t> cursor(offsets.begin(), offsets.end() - 1);
        for (const auto &[from, to] : edges) ends[size_t(cursor[size_t(reversed ? to : from)]++)] = reversed ? from : to;
    }

    std::vector<char> reach(std::vector<Py_ssize_t> pending) const {
        std::vector<char> reached(offsets.size() - 1);
        for (Py_ssize_t index : pending) reached[size_t(index)] = 1;
        while (!pending.empty()) {
            Py_ssize_t index = pending.back();
            pending.pop_back();
            for (Py_ssize_t at = offsets[size_t(index)]; at < offsets[size_t(index) + 1]; ++at) {
                Py_ssize_t end = ends[size_t(at)];
                if (!reached[size_t(end)]) {
                    reached[size_t(end)] = 1;
                    pending.push_back(end);
                }
            }
        }
        return reached;
    }
};

class Fields {
public:
    Fields(const char *const *names, int count) : raw_(names), count_(count) {
        for (int index = 0; index < count; ++index) names_[index] = Ref(interned(names[index]));
    }

    Ref operator()(PyObject *object, int index) {
        if (reinterpret_cast<PyObject *>(Py_TYPE(object)) != type_.get()) learn(Py_TYPE(object));
        PyObject *value = slots_ ? slots_->peek(object, index) : nullptr;
        return Ref(value ? Py_NewRef(value) : attribute(object, names_[index].get()));
    }

private:
    void learn(PyTypeObject *type) {
        type_ = Ref(Py_NewRef(reinterpret_cast<PyObject *>(type)));
        slots_.reset();
        if (type->tp_getattro != PyObject_GenericGetAttr) return;
        try {
            slots_ = std::make_unique<Slots>(type_.get(), raw_, count_);
        } catch (const Fail &) {
            PyErr_Clear();
        }
    }

    const char *const *raw_;
    int count_;
    Ref names_[5];
    Ref type_;
    std::unique_ptr<Slots> slots_;
};

const char *const pin_fields[] = {"direction", "name", "value_type", "required", "label"};

struct GraphRows {
    struct Node {
        Ref object, id, kind, properties;
    };
    struct Link {
        Ref source_id, source_pin, target_id, target_pin;
        Py_ssize_t source = -1, target = -1;
    };
    struct Member {
        size_t row, first_row;
        Ref pins;
        Py_ssize_t first = -1, count = 0;
        bool generic = false;
    };
    struct Pin {
        Ref object, direction, name;
    };

    GraphRows(PyObject *nodes, PyObject *links, PyObject *known, PyObject *provider, bool contract, bool record)
        : known_(known), provider_(provider), contract_(contract), record_(record) {
        each(nodes, [&](PyObject *node) {
            this->nodes.push_back(Node{Ref(Py_NewRef(node)), node_field(node, 0), node_field(node, 1),
                                       node_field(node, 4)});
        });
        each(links, [&](PyObject *link) {
            this->links.push_back(Link{link_field(link, 0), link_field(link, 1), link_field(link, 2),
                                       link_field(link, 3)});
        });
    }

    Py_ssize_t count() const { return Py_ssize_t(members.size()); }
    const Node &node_of(Py_ssize_t number) const { return nodes[members[size_t(number)].row]; }
    // The id the numbering keeps for a node: its first node's.
    PyObject *identity(Py_ssize_t number) const { return nodes[members[size_t(number)].first_row].id.get(); }
    Py_ssize_t find(PyObject *id) { return ids_.find(0, id); }

    void index() {
        if (indexed_) return;
        indexed_ = true;
        for (size_t row = 0; row < nodes.size(); ++row) {
            Py_ssize_t before = ids_.size(), number = ids_.add(0, nodes[row].id.get());
            if (number == before) members.push_back(Member{row, row});
            else members[size_t(number)].row = row;
        }
    }

    void resolve() {
        if (resolved_) return;
        resolved_ = true;
        index();
        for (Link &link : links) {
            link.source = find(link.source_id.get());
            link.target = find(link.target_id.get());
        }
    }

    PyObject *pins(Py_ssize_t number) {
        Member &entry = members[size_t(number)];
        if (entry.pins.get()) return entry.pins.get();
        const Node &node = nodes[entry.row];
        if (known_) entry.pins = Ref(member(known_, node.id.get()));
        if (!entry.pins.get()) {
            if (!provider_) {
                Ref key(check(PyTuple_Pack(1, node.id.get())));
                PyErr_SetObject(PyExc_KeyError, key.get());
                throw Fail();
            }
            PyObject *arguments[] = {node.object.get(), Py_None};
            entry.pins = Ref(check(PyObject_Vectorcall(provider_, arguments, contract_ ? 2 : 1, nullptr)));
            if (record_) check(PyDict_SetItem(known_, node.id.get(), entry.pins.get()));
        }
        return entry.pins.get();
    }

    void read_pins(Py_ssize_t number) {
        if (members[size_t(number)].first >= 0) return;
        PyObject *ports = pins(number);
        Member &entry = members[size_t(number)];
        entry.first = Py_ssize_t(pin_rows.size());
        each(ports, [&](PyObject *pin) {
            pin_rows.push_back(Pin{Ref(Py_NewRef(pin)), pin_field(pin, 0), pin_field(pin, 1)});
            entry.generic = entry.generic || !PyUnicode_CheckExact(pin_rows.back().direction.get())
                || !PyUnicode_CheckExact(pin_rows.back().name.get());
        });
        entry.count = Py_ssize_t(pin_rows.size()) - entry.first;
        if (entry.generic) return;
        const GraphNames &n = graph_names();
        for (Py_ssize_t at = entry.first; at < entry.first + entry.count; ++at) {
            PyObject *direction = pin_rows[size_t(at)].direction.get();
            uint64_t side = PyUnicode_Equal(direction, n.input) ? 0 : PyUnicode_Equal(direction, n.output) ? 1 : 2;
            if (side < 2 && pin_table_.add(uint64_t(number) << 1 | side, pin_rows[size_t(at)].name.get())
                                == Py_ssize_t(pin_positions_.size()))
                pin_positions_.push_back(at);
        }
    }

    Py_ssize_t pin(Py_ssize_t number, PyObject *direction, PyObject *name) {
        read_pins(number);
        const Member &entry = members[size_t(number)];
        if (!entry.generic && PyUnicode_CheckExact(name)) {
            Py_ssize_t found = pin_table_.find(uint64_t(number) << 1 | (direction == graph_names().input ? 0 : 1), name);
            return found < 0 ? -1 : pin_positions_[size_t(found)];
        }
        for (Py_ssize_t at = entry.first; at < entry.first + entry.count; ++at)
            if (same(pin_rows[size_t(at)].direction.get(), direction) && same(pin_rows[size_t(at)].name.get(), name))
                return at;
        return -1;
    }

    Fields node_field{node_fields, 5}, link_field{link_fields, 4}, pin_field{pin_fields, 5};
    std::vector<Node> nodes;
    std::vector<Link> links;
    std::vector<Member> members;
    std::vector<Pin> pin_rows;

private:
    PyObject *known_, *provider_;
    bool contract_, record_, indexed_ = false, resolved_ = false;
    FlatMap ids_, pin_table_;
    std::vector<Py_ssize_t> pin_positions_;
};

void execution_forks(GraphRows &graph, PyObject *helpers, PyObject *issues) {
    const GraphNames &n = graph_names();
    PyObject *kinds = PyTuple_GET_ITEM(helpers, 0), *type = PyTuple_GET_ITEM(helpers, 1);
    bool relevant = false;
    for (const auto &node : graph.nodes) {
        relevant = contains(kinds, node.kind.get());
        if (!relevant) {
            Ref value_type(mapping_get(node.properties.get(), n.value_type, Py_None));
            relevant = same(value_type.get(), type);
        }
        if (relevant) break;
    }
    for (size_t index = 0; !relevant && index < graph.links.size(); ++index)
        relevant = same(graph.links[index].target_pin.get(), n.execution);
    if (!relevant) return;
    auto terminal = [&](PyObject *kind) {
        return same(kind, n.output) || same(kind, n.function_output) || same(kind, n.material_surface);
    };
    graph.resolve();
    Py_ssize_t count = graph.count();
    Edges edges;
    for (const auto &link : graph.links)
        if (link.source >= 0 && link.target >= 0) edges.emplace_back(link.source, link.target);
    std::vector<Py_ssize_t> seeds;
    for (const auto &node : graph.nodes)
        if (terminal(node.kind.get())) seeds.push_back(graph.find(node.id.get()));
    std::vector<char> live = Adjacency(count, edges, true).reach(std::move(seeds));
    std::vector<Ref> executions(static_cast<size_t>(count));
    std::vector<std::vector<Py_ssize_t>> successors(static_cast<size_t>(count));
    std::vector<Py_ssize_t> order;
    for (const auto &link : graph.links) {
        if (link.source < 0 || !live[size_t(link.source)] || link.target < 0 || !live[size_t(link.target)]) continue;
        Ref &names = executions[size_t(link.source)];
        if (!names.get()) {
            names = Ref(check(PySet_New(nullptr)));
            each(graph.pins(link.source), [&](PyObject *pin) {
                Ref direction(graph.pin_field(pin, 0));
                if (!same(direction.get(), n.output)) return;
                Ref pin_type(graph.pin_field(pin, 2));
                if (!same(pin_type.get(), type)) return;
                Ref name(graph.pin_field(pin, 1));
                check(PySet_Add(names.get(), name.get()));
            });
        }
        if (!contains(names.get(), link.source_pin.get())) continue;
        if (successors[size_t(link.source)].empty()) order.push_back(link.source);
        successors[size_t(link.source)].push_back(link.target);
    }
    for (Py_ssize_t source : order) {
        Py_ssize_t continuations = 0;
        bool reaches_output = false;
        for (Py_ssize_t target : successors[size_t(source)]) {
            if (terminal(graph.node_of(target).kind.get())) reaches_output = true;
            else ++continuations;
        }
        if (continuations + reaches_output <= 1) continue;
        PyObject *arguments[] = {n.error, n.code_fork, n.fork, graph.node_of(source).id.get()};
        Ref issue(check(PyObject_Vectorcall(PyTuple_GET_ITEM(helpers, 2), arguments, 4, nullptr)));
        check(PyList_Append(issues, issue.get()));
    }
}

// sum(field == value for field in fields), fields(visit) visiting each: bools are counted; from the first other term
// on, builtins.sum adds the rest to the count so far.
template <typename Each> PyObject *count_matches(Each fields, PyObject *value) {
    Py_ssize_t count = 0;
    Ref terms;
    fields([&](PyObject *field) {
        if (!terms.get() && PyUnicode_CheckExact(field) && PyUnicode_CheckExact(value)) {
            count += PyUnicode_Equal(field, value);
            return;
        }
        Ref term(check(PyObject_RichCompare(field, value, Py_EQ)));
        if (!terms.get() && PyBool_Check(term.get())) {
            count += term.get() == Py_True;
            return;
        }
        if (!terms.get()) {
            Ref total(check(PyLong_FromSsize_t(count)));
            terms = Ref(check(PyList_New(0)));
            check(PyList_Append(terms.get(), total.get()));
        }
        check(PyList_Append(terms.get(), term.get()));
    });
    return check(terms.get() ? PyObject_CallOneArg(graph_names().sum, terms.get()) : PyLong_FromSsize_t(count));
}

class Largest {
public:
    void offer(Ref item) {
        if (best_.get()) {
            int greater = PyObject_RichCompareBool(item.get(), best_.get(), Py_GT);
            check(greater);
            if (!greater) return;
        }
        best_ = std::move(item);
    }
    PyObject *get() const { return best_.get(); }
private:
    Ref best_;
};

class FunctionCheck {
public:
    FunctionCheck(PyObject *known, PyObject *helpers) : names_(graph_names()), known_(known) {
        if (!PyTuple_CheckExact(helpers) || PyTuple_GET_SIZE(helpers) != 7 || !PyDict_Check(known)) {
            PyErr_SetString(PyExc_TypeError, "known: dict; helpers: (outside kinds, node_title, "
                                             "node_property_diagnostics, connection_compatible, diagnostic type, "
                                             "node_pins, (execution kinds, execution type, diagnostic type))");
            throw Fail();
        }
        outside_ = PyTuple_GET_ITEM(helpers, 0);
        title_ = PyTuple_GET_ITEM(helpers, 1);
        property_issues_ = PyTuple_GET_ITEM(helpers, 2);
        compatible_ = PyTuple_GET_ITEM(helpers, 3);
        diagnostic_ = PyTuple_GET_ITEM(helpers, 4);
        node_pins_ = PyTuple_GET_ITEM(helpers, 5);
        execution_ = PyTuple_GET_ITEM(helpers, 6);
        issues_ = Ref(check(PyList_New(0)));
    }

    PyObject *run(PyObject *nodes, PyObject *links, PyObject *inputs, PyObject *outputs) {
        const GraphNames &n = names_;
        GraphRows graph(nodes, links, known_, node_pins_, true, true);
        for (const auto &node : graph.nodes) {
            bool outside = contains(outside_, node.kind.get());
            if (!outside) {
                Ref boundary(mapping_get(node.properties.get(), n.boundary, Py_False));
                outside = truth(boundary.get());
            }
            if (outside) {
                Ref title(titled(node.object.get()));
                Ref message(check(PyUnicode_FromFormat("%U must remain outside a reusable function.", title.get())));
                add(n.code_node, message.get(), node.id.get());
            }
        }
        Ref input_nodes(by_pin(graph, n.function_input)), output_nodes(by_pin(graph, n.function_output));
        Ref expected_inputs(pin_ids(inputs)), expected_outputs(pin_ids(outputs));
        if (keys_differ(input_nodes.get(), expected_inputs.get())
            || keys_differ(output_nodes.get(), expected_outputs.get())
            || miscounted(graph, input_nodes.get(), n.function_input)
            || miscounted(graph, output_nodes.get(), n.function_output)) {
            add(n.code_interface, n.interface, nullptr);
        }
        port_types(inputs, input_nodes.get());
        port_types(outputs, output_nodes.get());
        for (const auto &node : graph.nodes) {
            Ref found(check(PyObject_CallOneArg(property_issues_, node.object.get())));
            each(found.get(), [&](PyObject *issue) { check(PyList_Append(issues_.get(), issue)); });
        }
        graph.index();
        if (graph.count() != size_of(nodes)) add(n.code_nodes, n.duplicate, nullptr);
        graph.resolve();
        FlatMap incoming, pairs;
        Edges edges;
        for (const auto &link : graph.links) {
            if (link.source < 0 || link.target < 0) {
                add(n.code_link, n.dangling, nullptr);
                continue;
            }
            Py_ssize_t source_pin = graph.pin(link.source, n.output, link.source_pin.get());
            Py_ssize_t target_pin = graph.pin(link.target, n.input, link.target_pin.get());
            if (source_pin < 0 || target_pin < 0) {
                add(n.code_pin, n.missing, nullptr);
                continue;
            }
            Py_ssize_t marked = incoming.size();
            if (incoming.add(uint64_t(target_pin), nullptr) != marked) add(n.code_input, n.multiple, nullptr);
            Ref source_type(graph.pin_field(graph.pin_rows[size_t(source_pin)].object.get(), 2));
            Ref target_type(graph.pin_field(graph.pin_rows[size_t(target_pin)].object.get(), 2));
            if (!compatible(source_type.get(), target_type.get())) {
                Ref left(formatted(source_type.get())), right(formatted(target_type.get()));
                Ref message(check(PyUnicode_FromFormat("%U cannot connect to %U.", left.get(), right.get())));
                add(n.code_type, message.get(), graph.node_of(link.target).id.get());
            }
            Py_ssize_t seen = pairs.size();
            if (pairs.add(uint64_t(link.source) << 32 | uint64_t(link.target), nullptr) == seen)
                edges.emplace_back(link.source, link.target);
        }
        Py_ssize_t count = graph.count();
        Adjacency successors(count, edges, false);
        std::vector<Py_ssize_t> indegree(static_cast<size_t>(count)), pending;
        for (const auto &edge : edges) ++indegree[size_t(edge.second)];
        for (Py_ssize_t index = 0; index < count; ++index)
            if (!indegree[size_t(index)]) pending.push_back(index);
        Py_ssize_t visited = 0;
        while (!pending.empty()) {
            Py_ssize_t index = pending.back();
            pending.pop_back();
            ++visited;
            for (Py_ssize_t at = successors.offsets[size_t(index)]; at < successors.offsets[size_t(index) + 1]; ++at)
                if (!--indegree[size_t(successors.ends[size_t(at)])]) pending.push_back(successors.ends[size_t(at)]);
        }
        if (visited != count) add(n.code_cycle, n.cycle, nullptr);
        Py_ssize_t cursor = 0;
        PyObject *key, *value;
        while (PyDict_Next(output_nodes.get(), &cursor, &key, &value)) {
            Ref node(Py_NewRef(value));
            Ref node_id(graph.node_field(node.get(), 0));
            Py_ssize_t index = graph.find(node_id.get());
            if (index < 0) {
                Ref missing(check(PyTuple_Pack(1, node_id.get())));
                PyErr_SetObject(PyExc_KeyError, missing.get());
                throw Fail();
            }
            pending.push_back(index);
        }
        std::vector<char> live = Adjacency(count, edges, true).reach(std::move(pending));
        for (Py_ssize_t index = 0; index < count; ++index) {
            if (!live[size_t(index)]) continue;
            graph.read_pins(index);
            Py_ssize_t first = graph.members[size_t(index)].first, end = first + graph.members[size_t(index)].count;
            for (Py_ssize_t at = first; at < end; ++at) {
                Ref pin(Py_NewRef(graph.pin_rows[size_t(at)].object.get()));
                if (!same(graph.pin_rows[size_t(at)].direction.get(), n.input)) continue;
                Ref required(graph.pin_field(pin.get(), 3));
                if (!truth(required.get())) continue;
                Py_ssize_t linked = graph.pin(index, n.input, graph.pin_rows[size_t(at)].name.get());
                if (linked >= 0 && incoming.find(uint64_t(linked), nullptr) >= 0) continue;
                Ref title(titled(graph.node_of(index).object.get()));
                Ref label(graph.pin_field(pin.get(), 4));
                Ref text(formatted(label.get()));
                Ref message(check(PyUnicode_FromFormat("%U needs %U.", title.get(), text.get())));
                add(n.code_input, message.get(), graph.identity(index));
            }
        }
        execution_forks(graph, execution_, issues_.get());
        return issues_.release();
    }

private:
    void add(PyObject *code, PyObject *message, PyObject *node_id) {
        PyObject *arguments[] = {names_.error, code, message, node_id};
        Ref issue(check(PyObject_Vectorcall(diagnostic_, arguments, node_id ? 4 : 3, nullptr)));
        check(PyList_Append(issues_.get(), issue.get()));
    }

    PyObject *formatted(PyObject *value) { return check(PyObject_Format(value, names_.empty)); }

    PyObject *titled(PyObject *node) {
        Ref title(check(PyObject_CallOneArg(title_, node)));
        return formatted(title.get());
    }

    PyObject *by_pin(const GraphRows &graph, PyObject *kind) {
        Ref table(check(PyDict_New()));
        for (const auto &node : graph.nodes) {
            if (!same(node.kind.get(), kind)) continue;
            Ref pin_id(mapping_get(node.properties.get(), names_.pin_id, names_.empty));
            Ref key(check(PyObject_Str(pin_id.get())));
            check(PyDict_SetItem(table.get(), key.get(), node.object.get()));
        }
        return table.release();
    }

    PyObject *pin_ids(PyObject *ports) {
        Ref result(check(PySet_New(nullptr)));
        each(ports, [&](PyObject *port) {
            Ref pin_id(attribute(port, names_.pin_id));
            check(PySet_Add(result.get(), pin_id.get()));
        });
        return result.release();
    }

    bool keys_differ(PyObject *table, PyObject *expected) {
        Ref keys(check(PySet_New(table)));
        Ref result(check(PyObject_RichCompare(keys.get(), expected, Py_NE)));
        return truth(result.get());
    }

    bool miscounted(const GraphRows &graph, PyObject *table, PyObject *kind) {
        Ref total(count_matches([&](auto visit) { for (const auto &node : graph.nodes) visit(node.kind.get()); }, kind));
        Ref size(check(PyLong_FromSsize_t(PyDict_GET_SIZE(table))));
        Ref result(check(PyObject_RichCompare(size.get(), total.get(), Py_NE)));
        return truth(result.get());
    }

    void port_types(PyObject *ports, PyObject *table) {
        each(ports, [&](PyObject *port) {
            Ref pin_id(attribute(port, names_.pin_id));
            Ref node(member(table, pin_id.get()));
            if (!node.get()) return;
            Ref properties(attribute(node.get(), names_.properties));
            Ref value_type(mapping_get(properties.get(), names_.value_type, Py_None));
            Ref declared(attribute(port, names_.value_type));
            Ref differs(check(PyObject_RichCompare(value_type.get(), declared.get(), Py_NE)));
            if (!truth(differs.get())) return;
            Ref name(attribute(port, names_.name));
            Ref text(formatted(name.get()));
            Ref message(check(PyUnicode_FromFormat("%U's node type differs from its published type.", text.get())));
            Ref node_id(attribute(node.get(), names_.node_id));
            add(names_.code_interface, message.get(), node_id.get());
        });
    }

    // connection_compatible(source, target), which accepts equal types first.
    bool compatible(PyObject *source, PyObject *target) {
        if (PyUnicode_CheckExact(source) && PyUnicode_CheckExact(target) && PyUnicode_Equal(source, target)) return true;
        PyObject *arguments[] = {source, target};
        Ref result(check(PyObject_Vectorcall(compatible_, arguments, 2, nullptr)));
        return truth(result.get());
    }

    const GraphNames &names_;
    PyObject *known_, *outside_, *title_, *property_issues_, *compatible_, *diagnostic_, *node_pins_, *execution_;
    Ref issues_;
};

PyObject *py_function_diagnostics(PyObject *, PyObject *const *arguments, Py_ssize_t count) {
    if (count != 6) {
        PyErr_SetString(PyExc_TypeError, "function_diagnostics(nodes, links, inputs, outputs, known, helpers)");
        return nullptr;
    }
    return guarded([&] {
        return FunctionCheck(arguments[4], arguments[5]).run(arguments[0], arguments[1], arguments[2], arguments[3]);
    });
}

PyObject *py_execution_diagnostics(PyObject *, PyObject *const *arguments, Py_ssize_t count) {
    if (count != 4) {
        PyErr_SetString(PyExc_TypeError, "execution_diagnostics(nodes, links, pins, helpers)");
        return nullptr;
    }
    return guarded([&] {
        Ref issues(check(PyList_New(0)));
        GraphRows graph(arguments[0], arguments[1], nullptr, arguments[2], false, false);
        execution_forks(graph, arguments[3], issues.get());
        return check(PyList_AsTuple(issues.get()));
    });
}

PyObject *arrange_graph(PyObject *nodes, PyObject *links, PyObject *pins, PyObject *width_provider, PyObject *error) {
    const GraphNames &n = graph_names();
    bool table = PyDict_CheckExact(pins);
    GraphRows graph(nodes, links, table ? pins : nullptr, table ? nullptr : pins, false, false);
    graph.index();
    Py_ssize_t count = graph.count();
    FlatMap pairs;
    Edges edges;
    for (const auto &link : graph.links) {
        Py_ssize_t from = graph.find(link.source_id.get());
        Py_ssize_t to = from < 0 ? -1 : graph.find(link.target_id.get());
        if (to < 0) {
            PyErr_SetString(error, "Cannot arrange a graph with dangling connections.");
            throw Fail();
        }
        Py_ssize_t seen = pairs.size();
        if (pairs.add(uint64_t(from) << 32 | uint64_t(to), nullptr) == seen) edges.emplace_back(from, to);
    }
    Adjacency children(count, edges, false);
    std::vector<Py_ssize_t> remaining(static_cast<size_t>(count)), rank(static_cast<size_t>(count)), ready;
    for (const auto &edge : edges) ++remaining[size_t(edge.second)];
    for (Py_ssize_t index = 0; index < count; ++index)
        if (!remaining[size_t(index)]) ready.push_back(index);
    for (size_t head = 0; head < ready.size(); ++head) {
        Py_ssize_t index = ready[head];
        for (Py_ssize_t at = children.offsets[size_t(index)]; at < children.offsets[size_t(index) + 1]; ++at) {
            Py_ssize_t child = children.ends[size_t(at)];
            rank[size_t(child)] = std::max(rank[size_t(child)], rank[size_t(index)] + 1);
            if (!--remaining[size_t(child)]) ready.push_back(child);
        }
    }
    if (Py_ssize_t(ready.size()) != count) {
        PyErr_SetString(error, "Cannot arrange a cyclic value graph.");
        throw Fail();
    }
    Ref one(check(PyLong_FromLong(1))), ninety(check(PyFloat_FromDouble(90.0)));
    Ref twenty_five(check(PyFloat_FromDouble(25.0))), two(check(PyFloat_FromDouble(2.0)));
    Ref seventy(check(PyFloat_FromDouble(70.0)));
    std::map<Py_ssize_t, std::vector<std::pair<PyObject *, Ref>>> columns;
    for (Py_ssize_t index = 0; index < count; ++index) {
        PyObject *ports = graph.pins(index);
        auto directions = [&](auto visit) {
            each(ports, [&](PyObject *pin) {
                Ref direction(graph.pin_field(pin, 0));
                visit(direction.get());
            });
        };
        Ref inputs(count_matches(directions, n.input)), outputs(count_matches(directions, n.output));
        Largest rows;
        rows.offer(Ref(Py_NewRef(one.get())));
        rows.offer(std::move(inputs));
        rows.offer(std::move(outputs));
        Ref scaled(check(PyNumber_Multiply(twenty_five.get(), rows.get())));
        columns[rank[size_t(index)]].emplace_back(graph.node_of(index).object.get(),
                                                  Ref(check(PyNumber_Add(ninety.get(), scaled.get()))));
    }
    bool fixed = !PyCallable_Check(width_provider);
    Ref x(check(PyFloat_FromDouble(0.0)));
    for (auto &entry : columns) {
        auto &column = entry.second;
        Ref heights(check(PyList_New(0)));
        for (auto &[node, height] : column) check(PyList_Append(heights.get(), height.get()));
        Ref total(check(PyObject_CallOneArg(n.sum, heights.get())));
        Ref negative(check(PyNumber_Negative(total.get())));
        Ref y(check(PyNumber_TrueDivide(negative.get(), two.get())));
        for (auto &[node, height] : column) {
            check(PyObject_SetAttr(node, n.x, x.get()));
            check(PyObject_SetAttr(node, n.y, y.get()));
            y = Ref(check(PyNumber_InPlaceAdd(y.get(), height.get())));
        }
        Largest widest;
        if (fixed) widest.offer(Ref(Py_NewRef(width_provider)));
        else for (auto &[node, height] : column) widest.offer(Ref(check(PyObject_CallOneArg(width_provider, node))));
        Ref step(check(PyNumber_Add(widest.get(), seventy.get())));
        x = Ref(check(PyNumber_InPlaceAdd(x.get(), step.get())));
    }
    Py_RETURN_NONE;
}

PyObject *py_arrange_graph(PyObject *, PyObject *const *arguments, Py_ssize_t count) {
    if (count != 5) {
        PyErr_SetString(PyExc_TypeError, "arrange_graph(nodes, links, pins, width_provider, error)");
        return nullptr;
    }
    return guarded([&] { return arrange_graph(arguments[0], arguments[1], arguments[2], arguments[3], arguments[4]); });
}

class CanonicalJson {
public:
    CanonicalJson() {
        static PyObject *items_name = interned("items");
        static PyObject *dict_items = check(_PyType_LookupRef(&PyDict_Type, items_name));
        items_name_ = items_name;
        dict_items_ = dict_items;
        out_.reserve(1 << 14);
    }

    PyObject *run(PyObject *value) {
        write(value);
        return bytes();
    }

    PyObject *graph(PyObject *graph, PyObject *fields, PyObject *asset_digest) {
        const GraphNames &n = graph_names();
        if (PyDict_Check(graph)) {
            Ref result(check(PyObject_CallOneArg(reinterpret_cast<PyObject *>(&PyDict_Type), graph)));
            Ref nodes(check(PyList_New(0))), source(check(PyObject_GetItem(graph, n.nodes)));
            each(source.get(), [&](PyObject *node) {
                Ref kind(check(PyObject_GetItem(node, n.kind)));
                Ref properties(region_properties(kind.get(), fields, asset_digest, [&] {
                    return check(PyObject_GetItem(node, n.properties));
                }));
                if (!properties.get()) {
                    check(PyList_Append(nodes.get(), node));
                    return;
                }
                Ref copy(check(PyDict_New()));
                check(PyDict_Update(copy.get(), node));
                check(PyDict_SetItem(copy.get(), n.properties, properties.get()));
                check(PyList_Append(nodes.get(), copy.get()));
            });
            check(PyDict_SetItem(result.get(), n.nodes, nodes.get()));
            document(result.get());
            return bytes();
        }
        Ref stage(attribute(graph, n.stage)), fingerprint(attribute(graph, n.contract_fingerprint));
        Ref nodes(attribute(graph, n.nodes)), links(attribute(graph, n.links));
        out_ += "{\"contract_fingerprint\":";
        write(fingerprint.get());
        out_ += ",\"links\":[";
        const char *separator = "";
        Fields link_field(link_fields, 4), node_field(node_fields, 5);
        each(links.get(), [&](PyObject *link) {
            Ref source_node(link_field(link, 0)), source_pin(link_field(link, 1));
            Ref target_node(link_field(link, 2)), target_pin(link_field(link, 3));
            out_ += separator;
            separator = ",";
            out_ += "{\"source\":[";
            write(source_node.get());
            out_ += ',';
            write(source_pin.get());
            out_ += "],\"target\":[";
            write(target_node.get());
            out_ += ',';
            write(target_pin.get());
            out_ += "]}";
        });
        out_ += "],\"nodes\":[";
        separator = "";
        each(nodes.get(), [&](PyObject *node) {
            Ref node_id(node_field(node, 0)), kind(node_field(node, 1)), x(node_field(node, 2));
            Ref y(node_field(node, 3)), properties(node_field(node, 4));
            Ref replaced(region_properties(kind.get(), fields, asset_digest, [&] { return Py_NewRef(properties.get()); }));
            out_ += separator;
            separator = ",";
            out_ += "{\"id\":";
            write(node_id.get());
            out_ += ",\"kind\":";
            write(kind.get());
            out_ += ",\"position\":[";
            write(x.get());
            out_ += ',';
            write(y.get());
            out_ += "],\"properties\":";
            write(replaced.get() ? replaced.get() : properties.get());
            out_ += '}';
        });
        out_ += "],\"stage\":";
        write(stage.get());
        out_ += '}';
        return bytes();
    }

private:
    PyObject *bytes() { return check(PyBytes_FromStringAndSize(out_.data(), Py_ssize_t(out_.size()))); }

    template <typename Read> PyObject *region_properties(PyObject *kind, PyObject *fields, PyObject *asset_digest, Read read) {
        PyObject *names;
        int found = PyDict_GetItemRef(fields, kind, &names);
        check(found);
        Ref held(names);
        if (!found || !truth(names)) return nullptr;
        Ref source(read());
        Ref properties(check(PyObject_CallOneArg(reinterpret_cast<PyObject *>(&PyDict_Type), source.get())));
        each(names, [&](PyObject *name) {
            Ref value(check(PyObject_GetItem(properties.get(), name)));
            Ref digest(check(PyObject_CallOneArg(asset_digest, value.get())));
            Ref wrapped(check(PyDict_New()));
            check(PyDict_SetItem(wrapped.get(), graph_names().recovered_asset_digest, digest.get()));
            check(PyObject_SetItem(properties.get(), name, wrapped.get()));
        });
        return properties.release();
    }

    using Items = std::vector<std::pair<PyObject *, PyObject *>>;

    class Nested {
    public:
        Nested(CanonicalJson &owner, PyObject *value) : owner_(owner), value_(value) {
            if (Py_EnterRecursiveCall(" while encoding a JSON object")) throw Fail();
        }
        ~Nested() {
            if (marked_) owner_.active_.pop_back();
            Py_LeaveRecursiveCall();
        }
        void mark() {
            auto &active = owner_.active_;
            if (std::find(active.begin(), active.end(), value_) != active.end()) {
                PyErr_SetString(PyExc_ValueError, "Circular reference detected");
                throw Fail();
            }
            active.push_back(value_);
            marked_ = true;
        }
    private:
        CanonicalJson &owner_;
        PyObject *value_;
        bool marked_ = false;
    };

    class Level {
    public:
        explicit Level(CanonicalJson &owner) : owner_(owner) {
            if (owner_.pool_.size() <= owner_.depth_) owner_.pool_.emplace_back();
            items_ = &owner_.pool_[owner_.depth_++];
            items_->clear();
        }
        ~Level() {
            for (auto &[key, value] : *items_) {
                Py_DECREF(key);
                Py_DECREF(value);
            }
            items_->clear();
            --owner_.depth_;
        }
        Items &items() { return *items_; }
    private:
        CanonicalJson &owner_;
        Items *items_;
    };

    void write(PyObject *value) {
        if (value == Py_None) {
            out_ += "null";
        } else if (value == Py_True) {
            out_ += "true";
        } else if (value == Py_False) {
            out_ += "false";
        } else if (PyUnicode_Check(value)) {
            text(value);
        } else if (PyLong_Check(value)) {
            integer(value);
        } else if (PyFloat_Check(value)) {
            real(PyFloat_AS_DOUBLE(value));
        } else if (PyList_Check(value) || PyTuple_Check(value)) {
            array(value);
        } else if (PyDict_Check(value)) {
            object(value);
        } else {
            Ref name(check(PyType_GetName(Py_TYPE(value))));
            PyErr_Format(PyExc_TypeError, "Object of type %U is not JSON serializable", name.get());
            throw Fail();
        }
    }

    void escape(Py_UCS4 c) {
        static const char hex[] = "0123456789abcdef";
        auto unicode = [&](Py_UCS4 unit) {
            out_ += 'u';
            for (int shift = 12; shift >= 0; shift -= 4) out_ += hex[(unit >> shift) & 0xf];
        };
        out_ += '\\';
        switch (c) {
        case '\\': case '"': out_ += char(c); return;
        case '\b': out_ += 'b'; return;
        case '\f': out_ += 'f'; return;
        case '\n': out_ += 'n'; return;
        case '\r': out_ += 'r'; return;
        case '\t': out_ += 't'; return;
        }
        if (c >= 0x10000) {
            unicode(Py_UNICODE_HIGH_SURROGATE(c));
            out_ += '\\';
            c = Py_UNICODE_LOW_SURROGATE(c);
        }
        unicode(c);
    }

    static bool plain(Py_UCS4 c) { return c >= ' ' && c < 0x7f && c != '\\' && c != '"'; }

    void text(PyObject *value) {
        out_ += '"';
        Py_ssize_t size = PyUnicode_GET_LENGTH(value);
        if (PyUnicode_IS_ASCII(value)) {
            const char *data = reinterpret_cast<const char *>(PyUnicode_1BYTE_DATA(value));
            Py_ssize_t start = 0;
            for (Py_ssize_t index = 0; index < size; ++index) {
                if (plain(static_cast<unsigned char>(data[index]))) continue;
                out_.append(data + start, size_t(index - start));
                escape(static_cast<unsigned char>(data[index]));
                start = index + 1;
            }
            out_.append(data + start, size_t(size - start));
        } else {
            int kind = PyUnicode_KIND(value);
            const void *data = PyUnicode_DATA(value);
            for (Py_ssize_t index = 0; index < size; ++index) {
                Py_UCS4 c = PyUnicode_READ(kind, data, index);
                if (plain(c)) out_ += char(c);
                else escape(c);
            }
        }
        out_ += '"';
    }

    void digits(PyObject *repr) {
        Py_ssize_t size;
        const char *data = PyUnicode_AsUTF8AndSize(repr, &size);
        if (!data) throw Fail();
        out_.append(data, size_t(size));
    }

    void integer(PyObject *value) {
        int overflow;
        long long number = PyLong_AsLongLongAndOverflow(value, &overflow);
        if (number == -1 && PyErr_Occurred()) throw Fail();
        if (overflow) {
            Ref repr(check(PyLong_Type.tp_repr(value)));
            digits(repr.get());
            return;
        }
        char buffer[24];
        out_.append(buffer, std::to_chars(buffer, buffer + sizeof buffer, number).ptr);
    }

    // float.__repr__: fixed notation for decimal exponents -4 to 15, else d.ddde±XX.
    void real(double number) {
        if (!std::isfinite(number)) {
            out_ += number > 0 ? "Infinity" : number < 0 ? "-Infinity" : "NaN";
            return;
        }
        char buffer[32];
        const char *end = std::to_chars(buffer, buffer + sizeof buffer, number, std::chars_format::scientific).ptr;
        const char *cursor = buffer;
        if (*cursor == '-') {
            out_ += '-';
            ++cursor;
        }
        char figures[24];
        int count = 0;
        for (; *cursor != 'e'; ++cursor)
            if (*cursor != '.') figures[count++] = *cursor;
        int exponent = 0;
        std::from_chars(cursor + 1 + (cursor[1] == '+'), end, exponent);
        int point = exponent + 1;
        if (point <= -4 || point > 16) {
            out_ += figures[0];
            if (count > 1) {
                out_ += '.';
                out_.append(figures + 1, size_t(count - 1));
            }
            char tail[8];
            out_.append(tail, size_t(std::snprintf(tail, sizeof tail, "e%+.02d", exponent)));
        } else if (point <= 0) {
            out_ += "0.";
            out_.append(size_t(-point), '0');
            out_.append(figures, size_t(count));
        } else if (point < count) {
            out_.append(figures, size_t(point));
            out_ += '.';
            out_.append(figures + point, size_t(count - point));
        } else {
            out_.append(figures, size_t(count));
            out_.append(size_t(point - count), '0');
            out_ += ".0";
        }
    }

    void array(PyObject *value) {
        Nested nested(*this, value);
        if (!PySequence_Fast_GET_SIZE(value)) {
            out_ += "[]";
            return;
        }
        nested.mark();
        out_ += '[';
        for (Py_ssize_t index = 0; index < PySequence_Fast_GET_SIZE(value); ++index) {
            if (index) out_ += ',';
            Ref item(Py_NewRef(PySequence_Fast_GET_ITEM(value, index)));
            write(item.get());
        }
        out_ += ']';
    }

    void entry(PyObject *key, PyObject *value, bool first) {
        if (!first) out_ += ',';
        if (PyUnicode_Check(key)) {
            text(key);
        } else {
            std::string saved;
            saved.swap(out_);
            if (PyFloat_Check(key)) {
                real(PyFloat_AS_DOUBLE(key));
            } else if (key == Py_True || key == Py_False || key == Py_None) {
                write(key);
            } else if (PyLong_Check(key)) {
                Ref repr(check(PyLong_Type.tp_repr(key)));
                digits(repr.get());
            } else {
                out_.swap(saved);
                PyErr_Format(PyExc_TypeError, "keys must be str, int, float, bool or None, not %.100s",
                             Py_TYPE(key)->tp_name);
                throw Fail();
            }
            Ref converted(check(PyUnicode_FromStringAndSize(out_.data(), Py_ssize_t(out_.size()))));
            out_.swap(saved);
            text(converted.get());
        }
        out_ += ':';
        write(value);
    }

    // items() gives the dict's own items in order: exact dicts, and subclasses reading attributes the usual way, without
    // instance dicts, that keep dict.items.
    bool own_items(PyObject *value) {
        PyTypeObject *type = Py_TYPE(value);
        if (type == &PyDict_Type || type == own_type_) return true;
        if (type->tp_dictoffset || type->tp_getattro != PyObject_GenericGetAttr) return false;
        PyObject *found = _PyType_LookupRef(type, items_name_);
        Py_XDECREF(found);
        if (found != dict_items_) return false;
        own_type_ = type;
        return true;
    }

    void document(PyObject *value) {
        const GraphNames &n = graph_names();
        Level level(*this);
        Items &items = level.items();
        Py_ssize_t cursor = 0;
        PyObject *key, *item;
        while (PyDict_Next(value, &cursor, &key, &item)) {
            if (!PyUnicode_CheckExact(key)) return object(value);
            items.emplace_back(Py_NewRef(key), Py_NewRef(item));
        }
        std::sort(items.begin(), items.end(),
                  [](const auto &left, const auto &right) { return text_less(left.first, right.first); });
        out_ += '{';
        for (size_t index = 0; index < items.size(); ++index) {
            auto [name, rows] = items[index];
            if (index) out_ += ',';
            text(name);
            out_ += ':';
            bool nodes = PyUnicode_Equal(name, n.nodes), links = !nodes && PyUnicode_Equal(name, n.links);
            if ((!nodes && !links) || !(PyList_CheckExact(rows) || Py_TYPE(rows) == config.array_type)) {
                write(rows);
                continue;
            }
            out_ += '[';
            for (Py_ssize_t position = 0; position < PyList_GET_SIZE(rows); ++position) {
                if (position) out_ += ',';
                Ref row(Py_NewRef(PyList_GET_ITEM(rows, position)));
                if (nodes) this->row(row.get(), {n.id, n.kind, n.position, n.properties});
                else this->row(row.get(), {n.source, n.target});
            }
            out_ += ']';
        }
        out_ += '}';
    }

    // A dict holding exactly names (in sorted order) written field by field; anything else as write() writes it.
    void row(PyObject *value, std::initializer_list<PyObject *> names) {
        if (!(PyDict_CheckExact(value) || Py_TYPE(value) == config.object_type)
            || PyDict_GET_SIZE(value) != Py_ssize_t(names.size()))
            return write(value);
        std::vector<Ref> fields;
        for (PyObject *name : names) {
            fields.emplace_back(member(value, name));
            if (!fields.back().get()) return write(value);
        }
        out_ += '{';
        size_t index = 0;
        for (PyObject *name : names) {
            if (index) out_ += ',';
            text(name);
            out_ += ':';
            write(fields[index++].get());
        }
        out_ += '}';
    }

    static bool text_less(PyObject *left, PyObject *right) {
        if (PyUnicode_IS_ASCII(left) && PyUnicode_IS_ASCII(right)) {
            Py_ssize_t a = PyUnicode_GET_LENGTH(left), b = PyUnicode_GET_LENGTH(right);
            int order = std::memcmp(PyUnicode_1BYTE_DATA(left), PyUnicode_1BYTE_DATA(right), size_t(std::min(a, b)));
            return order < 0 || (order == 0 && a < b);
        }
        return PyUnicode_Compare(left, right) < 0;
    }

    void object(PyObject *value) {
        Nested nested(*this, value);
        if (!PyDict_GET_SIZE(value)) {
            out_ += "{}";
            return;
        }
        nested.mark();
        out_ += '{';
        if (own_items(value)) {
            Level level(*this);
            Items &items = level.items();
            bool text_keys = true;
            Py_ssize_t cursor = 0;
            PyObject *key, *item;
            while (PyDict_Next(value, &cursor, &key, &item)) {
                items.emplace_back(Py_NewRef(key), Py_NewRef(item));
                text_keys = text_keys && PyUnicode_CheckExact(key);
            }
            if (text_keys) {
                std::sort(items.begin(), items.end(),
                          [](const auto &left, const auto &right) { return text_less(left.first, right.first); });
                for (size_t index = 0; index < items.size(); ++index)
                    entry(items[index].first, items[index].second, index == 0);
                out_ += '}';
                return;
            }
        }
        Ref items(check(PyMapping_Items(value)));
        check(PyList_Sort(items.get()));
        for (Py_ssize_t index = 0; index < PyList_GET_SIZE(items.get()); ++index) {
            PyObject *item = PyList_GET_ITEM(items.get(), index);
            if (!PyTuple_Check(item) || PyTuple_GET_SIZE(item) != 2) {
                PyErr_SetString(PyExc_ValueError, "items must return 2-tuples");
                throw Fail();
            }
            entry(PyTuple_GET_ITEM(item, 0), PyTuple_GET_ITEM(item, 1), index == 0);
        }
        out_ += '}';
    }

    PyObject *items_name_;
    PyObject *dict_items_;
    PyTypeObject *own_type_ = nullptr;
    std::string out_;
    std::vector<PyObject *> active_;
    std::deque<Items> pool_;
    size_t depth_ = 0;
};

PyObject *py_canonical_json(PyObject *, PyObject *value) {
    return guarded([&] { return CanonicalJson().run(value); });
}

PyObject *py_graph_json(PyObject *, PyObject *const *arguments, Py_ssize_t count) {
    if (count != 3 || !PyDict_Check(arguments[1])) {
        PyErr_SetString(PyExc_TypeError, "graph_json(graph, fields: dict, asset_digest)");
        return nullptr;
    }
    return guarded([&] { return CanonicalJson().graph(arguments[0], arguments[1], arguments[2]); });
}

bool matches_graph(PyObject *graph, PyObject *document, PyObject *types) {
    const GraphNames &n = graph_names();
    require_configured();
    auto dict = [](PyObject *value) { return PyDict_CheckExact(value) || Py_TYPE(value) == config.object_type; };
    // set(mapping) == expected, whose members are names.
    auto keys_are = [&](PyObject *mapping, PyObject *expected, std::initializer_list<PyObject *> names) {
        if (dict(mapping)) {
            if (PyDict_GET_SIZE(mapping) != Py_ssize_t(names.size())) return false;
            for (PyObject *name : names) {
                int found = PyDict_Contains(mapping, name);
                check(found);
                if (!found) return false;
            }
            return true;
        }
        Ref keys(check(PySet_New(mapping)));
        Ref differs(check(PyObject_RichCompare(keys.get(), expected, Py_NE)));
        return !truth(differs.get());
    };
    auto key_order_differs = [&](PyObject *left, PyObject *right) {
        if (!dict(left) || !dict(right)) {
            Ref first(check(PySequence_Tuple(left))), second(check(PySequence_Tuple(right)));
            Ref result(check(PyObject_RichCompare(first.get(), second.get(), Py_NE)));
            return truth(result.get());
        }
        Py_ssize_t left_cursor = 0, right_cursor = 0;
        PyObject *left_key, *right_key, *ignored;
        while (PyDict_Next(left, &left_cursor, &left_key, &ignored) && PyDict_Next(right, &right_cursor, &right_key, &ignored)) {
            Ref held_left(Py_NewRef(left_key)), held_right(Py_NewRef(right_key));
            int equal = PyObject_RichCompareBool(held_left.get(), held_right.get(), Py_EQ);
            check(equal);
            if (!equal) return true;
        }
        return PyDict_GET_SIZE(left) != PyDict_GET_SIZE(right);
    };
    auto equal = [](PyObject *left, PyObject *right) {
        Ref result(check(PyObject_RichCompare(left, right, Py_EQ)));
        return truth(result.get());
    };
    auto differ = [](PyObject *left, PyObject *right) {
        Ref result(check(PyObject_RichCompare(left, right, Py_NE)));
        return truth(result.get());
    };
    auto item = [](PyObject *container, Py_ssize_t index) {
        if ((PyList_CheckExact(container) || Py_TYPE(container) == config.array_type) && index < PyList_GET_SIZE(container))
            return Ref(Py_NewRef(PyList_GET_ITEM(container, index)));
        Ref key(check(PyLong_FromSsize_t(index)));
        return Ref(check(PyObject_GetItem(container, key.get())));
    };
    auto exact = [&](PyObject *object, PyObject *name, auto test) {
        Ref value(attribute(object, name));
        return test(value.get()) ? std::move(value) : Ref();
    };
    auto is_list = [](PyObject *value) { return PyList_CheckExact(value) != 0; };
    auto is_text = [](PyObject *value) { return PyUnicode_CheckExact(value) != 0; };
    if (!keys_are(document, config.graph_keys, {n.stage, n.contract_fingerprint, n.nodes, n.links})) return false;
    if (Py_TYPE(graph) != reinterpret_cast<PyTypeObject *>(PyTuple_GET_ITEM(types, 0))) return false;
    Ref nodes(exact(graph, n.nodes, is_list)), links;
    if (!nodes.get() || !(links = exact(graph, n.links, is_list)).get()) return false;
    Ref stage(exact(graph, n.stage, is_text)), fingerprint;
    if (!stage.get() || !(fingerprint = exact(graph, n.contract_fingerprint, is_text)).get()) return false;
    Ref declared_stage(check(PyObject_GetItem(document, n.stage)));
    if (differ(stage.get(), declared_stage.get())) return false;
    Ref declared_fingerprint(check(PyObject_GetItem(document, n.contract_fingerprint)));
    if (differ(fingerprint.get(), declared_fingerprint.get())) return false;
    Ref declared_nodes(check(PyObject_GetItem(document, n.nodes)));
    if (PyList_GET_SIZE(nodes.get()) != size_of(declared_nodes.get())) return false;
    Ref declared_links(check(PyObject_GetItem(document, n.links)));
    if (PyList_GET_SIZE(links.get()) != size_of(declared_links.get())) return false;
    Ref rows(check(PyObject_GetIter(declared_nodes.get())));
    for (Py_ssize_t index = 0; index < PyList_GET_SIZE(nodes.get()); ++index) {
        Ref node(Py_NewRef(PyList_GET_ITEM(nodes.get(), index)));
        Ref declared(PyIter_Next(rows.get()));
        if (!declared.get()) {
            if (PyErr_Occurred()) throw Fail();
            break;
        }
        Ref properties(attribute(node.get(), n.properties));
        Ref expected(check(PyObject_GetItem(declared.get(), n.properties)));
        if (!keys_are(declared.get(), n.node_keys, {n.id, n.kind, n.position, n.properties})) return false;
        if (Py_TYPE(node.get()) != reinterpret_cast<PyTypeObject *>(PyTuple_GET_ITEM(types, 1))) return false;
        Ref node_id(exact(node.get(), n.node_id, is_text)), kind;
        if (!node_id.get() || !(kind = exact(node.get(), n.kind, is_text)).get()) return false;
        Ref declared_id(check(PyObject_GetItem(declared.get(), n.id)));
        if (differ(node_id.get(), declared_id.get())) return false;
        Ref declared_kind(check(PyObject_GetItem(declared.get(), n.kind)));
        if (differ(kind.get(), declared_kind.get())) return false;
        Ref x(attribute(node.get(), n.x)), position(check(PyObject_GetItem(declared.get(), n.position)));
        if (x.get() != item(position.get(), 0).get()) return false;
        Ref y(attribute(node.get(), n.y));
        if (y.get() != item(position.get(), 1).get()) return false;
        int known = PySequence_Contains(PyTuple_GET_ITEM(types, 3), reinterpret_cast<PyObject *>(Py_TYPE(properties.get())));
        check(known);
        if (!known || !PyDict_Check(properties.get())) return false;
        if (key_order_differs(properties.get(), expected.get())) return false;
        Py_ssize_t cursor = 0;
        PyObject *key, *value;
        while (PyDict_Next(properties.get(), &cursor, &key, &value)) {
            Ref held_key(Py_NewRef(key)), held_value(Py_NewRef(value));
            Ref declared_value(check(PyObject_GetItem(expected.get(), held_key.get())));
            if (declared_value.get() != held_value.get()) return false;
        }
    }
    rows = Ref(check(PyObject_GetIter(declared_links.get())));
    for (Py_ssize_t index = 0; index < PyList_GET_SIZE(links.get()); ++index) {
        Ref link(Py_NewRef(PyList_GET_ITEM(links.get(), index)));
        Ref row(PyIter_Next(rows.get()));
        if (!row.get()) {
            if (PyErr_Occurred()) throw Fail();
            break;
        }
        if (!keys_are(row.get(), n.link_keys, {n.source, n.target})) return false;
        if (Py_TYPE(link.get()) != reinterpret_cast<PyTypeObject *>(PyTuple_GET_ITEM(types, 2))) return false;
        Ref source_node(attribute(link.get(), n.source_node)), source_pin(attribute(link.get(), n.source_pin));
        Ref target_node(attribute(link.get(), n.target_node)), target_pin(attribute(link.get(), n.target_pin));
        if (!is_text(source_node.get()) || !is_text(source_pin.get()) || !is_text(target_node.get())
            || !is_text(target_pin.get()))
            return false;
        Ref source(check(PyObject_GetItem(row.get(), n.source)));
        if (!equal(source_node.get(), item(source.get(), 0).get()) || !equal(source_pin.get(), item(source.get(), 1).get()))
            return false;
        Ref target(check(PyObject_GetItem(row.get(), n.target)));
        if (!equal(target_node.get(), item(target.get(), 0).get()) || !equal(target_pin.get(), item(target.get(), 1).get()))
            return false;
    }
    return true;
}

PyObject *py_matches_graph(PyObject *, PyObject *const *arguments, Py_ssize_t count) {
    if (count != 3 || !PyTuple_CheckExact(arguments[2]) || PyTuple_GET_SIZE(arguments[2]) != 4) {
        PyErr_SetString(PyExc_TypeError, "matches_graph(graph, document, (graph, node, link, property types))");
        return nullptr;
    }
    return guarded([&] { return Py_NewRef(matches_graph(arguments[0], arguments[1], arguments[2]) ? Py_True : Py_False); });
}

PyObject *discard_links(PyObject *links, PyObject *identities, PyObject *table, Py_ssize_t start) {
    const GraphNames &n = graph_names();
    Ref kept(check(PyList_New(0))), tail(check(PySequence_GetSlice(links, start, PY_SSIZE_T_MAX)));
    each(tail.get(), [&](PyObject *link) {
        Ref source(attribute(link, n.source_node));
        bool touching = contains(identities, source.get());
        if (!touching) {
            Ref target(attribute(link, n.target_node));
            touching = contains(identities, target.get());
        }
        if (!touching) {
            check(PyList_Append(kept.get(), link));
            return;
        }
        if (table == Py_None) return;
        Ref target(attribute(link, n.target_node)), pin(attribute(link, n.target_pin));
        Ref key(check(PyTuple_Pack(2, target.get(), pin.get())));
        if (PyDict_CheckExact(table)) {
            check(PyDict_Pop(table, key.get(), nullptr));
        } else {
            Ref popped(check(PyObject_CallMethodObjArgs(table, n.pop, key.get(), Py_None, nullptr)));
        }
    });
    check(PySequence_SetSlice(links, start, PY_SSIZE_T_MAX, kept.get()));
    return check(PyLong_FromSsize_t(size_of(tail.get()) - PyList_GET_SIZE(kept.get())));
}

PyObject *py_discard_links(PyObject *, PyObject *const *arguments, Py_ssize_t count) {
    if (count != 4) {
        PyErr_SetString(PyExc_TypeError, "discard_links(links, identities, table, start)");
        return nullptr;
    }
    Py_ssize_t start = PyLong_AsSsize_t(arguments[3]);
    if (start == -1 && PyErr_Occurred()) return nullptr;
    return guarded([&] { return discard_links(arguments[0], arguments[1], arguments[2], start); });
}

class SlotsCache {
public:
    SlotsCache(const char *const *names, int count) : names_(names), count_(count) {}
    const Slots &of(PyObject *type) {
        if (type_ != type) {
            slots_ = std::make_unique<Slots>(type, names_, count_);
            Py_XSETREF(type_, Py_NewRef(type));
        }
        return *slots_;
    }
private:
    const char *const *names_;
    int count_;
    PyObject *type_ = nullptr;
    std::unique_ptr<Slots> slots_;
};

void add_to(PyObject *collection, PyObject *item) {
    if (PySet_Check(collection)) {
        check(PySet_Add(collection, item));
    } else {
        Ref ignored(check(PyObject_CallMethodOneArg(collection, graph_names().add, item)));
    }
}

void append_to(PyObject *list, PyObject *item) {
    if (PyList_CheckExact(list)) {
        check(PyList_Append(list, item));
    } else {
        Ref ignored(check(PyObject_CallMethod(list, "append", "O", item)));
    }
}

PyObject *optional_attribute(PyObject *owner, PyObject *name) {
    PyObject *found;
    if (PyObject_GetOptionalAttr(owner, name, &found) < 0) throw Fail();
    return found ? found : Py_NewRef(Py_None);
}

PyObject *connect_link(PyObject *graph, PyObject *const *ends, PyObject *max_links, PyObject *link_type,
                       PyObject *error, PyObject *base_connect) {
    static SlotsCache link_slots(link_fields, 4);
    const GraphNames &n = graph_names();
    Ref links(attribute(graph, n.links));
    Ref size(check(PyLong_FromSsize_t(size_of(links.get()))));
    Ref full(check(PyObject_RichCompare(size.get(), max_links, Py_GE)));
    if (truth(full.get())) {
        Ref message(check(PyUnicode_FromFormat("shader graph is limited to %S links", max_links)));
        PyErr_SetObject(error, message.get());
        throw Fail();
    }
    if (same(ends[0], ends[2])) {
        PyErr_SetString(error, "a node cannot connect to itself");
        throw Fail();
    }
    Ref targets(optional_attribute(graph, n.connected_targets));
    if (targets.get() == Py_None) {
        targets = Ref(check(PySet_New(nullptr)));
        each(links.get(), [&](PyObject *link) {
            Ref target(attribute(link, n.target_node)), pin(attribute(link, n.target_pin));
            Ref key(check(PyTuple_Pack(2, target.get(), pin.get())));
            check(PySet_Add(targets.get(), key.get()));
        });
        check(PyObject_SetAttr(graph, n.connected_targets, targets.get()));
    }
    Ref key(check(PyTuple_Pack(2, ends[2], ends[3])));
    Ref link;
    if (contains(targets.get(), key.get())) {
        PyObject *arguments[] = {graph, ends[0], ends[1], ends[2], ends[3]};
        link = Ref(check(PyObject_Vectorcall(base_connect, arguments, 5, nullptr)));
    } else {
        Ref parts[4];
        for (int index = 0; index < 4; ++index) parts[index] = Ref(check(PyObject_Str(ends[index])));
        PyObject *fields[] = {parts[0].get(), parts[1].get(), parts[2].get(), parts[3].get()};
        link = Ref(link_slots.of(link_type).make(fields));
        Ref target(attribute(link.get(), n.target_node)), pin(attribute(link.get(), n.target_pin));
        Ref added(check(PyTuple_Pack(2, target.get(), pin.get())));
        add_to(targets.get(), added.get());
        append_to(links.get(), link.get());
    }
    Ref table(optional_attribute(graph, n.incoming_links));
    if (table.get() != Py_None) {
        Ref target(attribute(link.get(), n.target_node)), pin(attribute(link.get(), n.target_pin));
        Ref entry(check(PyTuple_Pack(2, target.get(), pin.get())));
        check(PyObject_SetItem(table.get(), entry.get(), link.get()));
    }
    return link.release();
}

PyObject *py_connect_link(PyObject *, PyObject *const *arguments, Py_ssize_t count) {
    if (count != 9) {
        PyErr_SetString(PyExc_TypeError, "connect_link(graph, source_node, source_pin, target_node, target_pin, "
                                         "max_links, link_type, error, base_connect)");
        return nullptr;
    }
    return guarded([&] {
        return connect_link(arguments[0], arguments + 1, arguments[5], arguments[6], arguments[7], arguments[8]);
    });
}

class RegionCopy {
public:
    RegionCopy(PyObject *const *arguments)
        : names_(graph_names()), graph_(arguments[1]), nodes_(arguments[2]), links_(arguments[3]),
          region_(arguments[4]), copied_(arguments[5]), capture_(arguments[6]), ports_(arguments[7]),
          shared_(arguments[8]) {
        PyObject *helpers = arguments[9];
        if (!PyTuple_CheckExact(helpers) || PyTuple_GET_SIZE(helpers) != 5) {
            PyErr_SetString(PyExc_TypeError, "helpers: (node_pins, deepcopy, replace, value type, node type)");
            throw Fail();
        }
        node_pins_ = PyTuple_GET_ITEM(helpers, 0);
        deepcopy_ = PyTuple_GET_ITEM(helpers, 1);
        replace_ = PyTuple_GET_ITEM(helpers, 2);
        value_type_ = PyTuple_GET_ITEM(helpers, 3);
        node_type_ = PyTuple_GET_ITEM(helpers, 4);
    }

    PyObject *run(PyObject *value) {
        const GraphNames &n = names_;
        Ref start(attribute(value, n.node));
        if (!contains(region_, start.get())) return external(value);
        std::vector<Ref> pending;
        pending.emplace_back(Py_NewRef(start.get()));
        while (!pending.empty()) {
            Ref identity(std::move(pending.back()));
            pending.pop_back();
            if (contains(copied_, identity.get())) continue;
            add_to(copied_, identity.get());
            Ref node(check(PyObject_GetItem(nodes_, identity.get())));
            Ref copy(contains(shared_, identity.get()) ? check(PyObject_CallOneArg(deepcopy_, node.get()))
                                                       : replaced(node.get()));
            Ref appended(check(PyObject_CallMethodOneArg(graph_, n.append_node, copy.get())));
            Ref candidates(pins(identity.get()));
            each(candidates.get(), [&](PyObject *pin) {
                Ref direction(attribute(pin, n.direction));
                if (!same(direction.get(), n.input)) return;
                Ref name(attribute(pin, n.name));
                Ref key(check(PyTuple_Pack(2, identity.get(), name.get())));
                Ref link(lookup(links_, key.get()));
                if (link.get() == Py_None) return;
                Ref source_node(attribute(link.get(), n.source_node));
                Ref source;
                if (contains(region_, source_node.get())) {
                    Ref source_pin(attribute(link.get(), n.source_pin));
                    source = Ref(check(PyTuple_Pack(2, source_node.get(), source_pin.get())));
                    pending.emplace_back(Py_NewRef(source_node.get()));
                } else {
                    Ref found(output_type(source_node.get(), link.get()));
                    Ref source_pin(attribute(link.get(), n.source_pin));
                    PyObject *fields[] = {source_node.get(), source_pin.get(), found.get()};
                    Ref outside(check(PyObject_Vectorcall(value_type_, fields, 3, nullptr)));
                    source = Ref(external(outside.get()));
                }
                connect(source.get(), identity.get(), name.get());
            });
        }
        Ref node(attribute(value, n.node)), pin(attribute(value, n.pin));
        return check(PyTuple_Pack(2, node.get(), pin.get()));
    }

private:
    PyObject *lookup(PyObject *mapping, PyObject *key) {
        if (PyDict_CheckExact(mapping)) {
            PyObject *found = member(mapping, key);
            return found ? found : Py_NewRef(Py_None);
        }
        return check(PyObject_CallMethodObjArgs(mapping, names_.get, key, nullptr));
    }

    PyObject *replaced(PyObject *node) {
        static SlotsCache slots(node_fields, 5);
        if (Py_TYPE(node) != reinterpret_cast<PyTypeObject *>(node_type_))
            return check(PyObject_CallOneArg(replace_, node));
        const Slots &fields = slots.of(node_type_);
        PyObject *values[5];
        for (int index = 0; index < 5; ++index) values[index] = fields.get(node, index);
        return fields.make(values);
    }

    PyObject *pins(PyObject *identity) {
        if (!contains(ports_, identity)) {
            Ref node(check(PyObject_GetItem(nodes_, identity)));
            PyObject *arguments[] = {node.get(), Py_None};
            Ref found(check(PyObject_Vectorcall(node_pins_, arguments, 2, nullptr)));
            check(PyObject_SetItem(ports_, identity, found.get()));
        }
        return check(PyObject_GetItem(ports_, identity));
    }

    PyObject *output_type(PyObject *source, PyObject *link) {
        const GraphNames &n = names_;
        Ref candidates(pins(source));
        PyObject *found = nullptr;
        each(candidates.get(), [&](PyObject *candidate) {
            Ref direction(attribute(candidate, n.direction));
            if (!same(direction.get(), n.output)) return true;
            Ref name(attribute(candidate, n.name)), wanted(attribute(link, n.source_pin));
            if (!same(name.get(), wanted.get())) return true;
            found = attribute(candidate, n.value_type);
            return false;
        });
        if (!found) {
            PyErr_SetNone(PyExc_StopIteration);
            throw Fail();
        }
        return found;
    }

    PyObject *external(PyObject *value) {
        const GraphNames &n = names_;
        Ref identity(attribute(value, n.node));
        Ref node(check(PyObject_GetItem(nodes_, identity.get())));
        Ref kind(attribute(node.get(), n.kind));
        if (!contains(n.local_kinds, kind.get())) return check(PyObject_CallOneArg(capture_, value));
        if (!contains(copied_, identity.get())) {
            add_to(copied_, identity.get());
            Ref original(check(PyObject_GetItem(nodes_, identity.get())));
            Ref copy(check(PyObject_CallOneArg(deepcopy_, original.get())));
            Ref appended(check(PyObject_CallMethodOneArg(graph_, n.append_node, copy.get())));
            Ref recorded(pins(identity.get()));
        }
        Ref pin(attribute(value, n.pin));
        return check(PyTuple_Pack(2, identity.get(), pin.get()));
    }

    void connect(PyObject *source, PyObject *target, PyObject *pin) {
        if (PyTuple_CheckExact(source) && PyTuple_GET_SIZE(source) == 2) {
            PyObject *arguments[] = {graph_, PyTuple_GET_ITEM(source, 0), PyTuple_GET_ITEM(source, 1), target, pin};
            Ref ignored(check(PyObject_VectorcallMethod(names_.connect, arguments, 5, nullptr)));
            return;
        }
        Ref parts(check(PySequence_Tuple(source)));
        Ref tail(check(PyTuple_Pack(2, target, pin)));
        Ref arguments(check(PySequence_Concat(parts.get(), tail.get())));
        Ref method(attribute(graph_, names_.connect));
        Ref ignored(check(PyObject_Call(method.get(), arguments.get(), nullptr)));
    }

    const GraphNames &names_;
    PyObject *graph_, *nodes_, *links_, *region_, *copied_, *capture_, *ports_, *shared_;
    PyObject *node_pins_, *deepcopy_, *replace_, *value_type_, *node_type_;
};

PyObject *py_copy_region_value(PyObject *, PyObject *const *arguments, Py_ssize_t count) {
    if (count != 10) {
        PyErr_SetString(PyExc_TypeError, "copy_region_value(value, graph, nodes, links, region, copied, capture, "
                                         "ports, shared, helpers)");
        return nullptr;
    }
    return guarded([&] { return RegionCopy(arguments).run(arguments[0]); });
}

void unpack(PyObject *item, Ref *parts, Py_ssize_t count) {
    Ref iterator(check(PyObject_GetIter(item)));
    for (Py_ssize_t index = 0; index < count; ++index) {
        parts[index] = Ref(PyIter_Next(iterator.get()));
        if (!parts[index].get()) {
            if (PyErr_Occurred()) throw Fail();
            PyErr_Format(PyExc_ValueError, "not enough values to unpack (expected %zd, got %zd)", count, index);
            throw Fail();
        }
    }
    Ref extra(PyIter_Next(iterator.get()));
    if (extra.get()) {
        PyErr_Format(PyExc_ValueError, "too many values to unpack (expected %zd)", count);
        throw Fail();
    }
    if (PyErr_Occurred()) throw Fail();
}

class RegionPlan {
public:
    RegionPlan(PyObject *branch_arms, PyObject *loop_body, PyObject *link_type = nullptr, PyObject *replace = nullptr)
        : n_(graph_names()), branch_arms_(branch_arms), loop_body_(loop_body), link_type_(link_type), replace_(replace) {
        value_ = text(n_.value);
        condition_ = text(n_.condition);
    }

    PyObject *plan(PyObject *graph, PyObject *keep_inputs) {
        Body &root = make_body(graph);
        prepare(root);
        std::vector<int> keep;
        each(keep_inputs, [&](PyObject *pin) { keep.push_back(text(pin)); });
        entries_ = Ref(check(PyList_New(0)));
        trim(root, true, {}, unique(std::move(keep)));
        return entries_.release();
    }

    PyObject *passthroughs(PyObject *node, PyObject *pins) {
        Region &region = make_region(node, kind_of(node));
        for (Body *body : region.bodies) prepare(*body);
        prepare_states(region);
        Ref aliases(check(PyList_New(0)));
        each(pins, [&](PyObject *pin) {
            int alias = alias_input(region, text(pin));
            check(PyList_Append(aliases.get(), alias < 0 ? Py_None : texts_[alias].get()));
        });
        return check(PyTuple_Pack(2, region.states.get(), aliases.get()));
    }

private:
    enum Kind : uint8_t { plain, input, output, branch, repeat, unassigned, terminal };
    struct Region;
    struct Link {
        int pin, node, source_pin, value;
    };
    struct GraphLink {
        int target, pin, value;  // target -1 when no node has the id; value -1 until read
    };
    struct Value {
        uint64_t key;
        int next;  // the node's previous value
        int alias = -1, source = -1, children = -1, count = 0;
        int64_t bits = -1;  // offset in the body's arena
        uint32_t seen = 0;
    };
    struct Body {
        Ref graph, list, links;              // the graph's lists as tuples
        FlatMap indices;                     // node id -> index
        std::vector<Ref> held;               // the ids, by index
        std::vector<PyObject *> ids, nodes;  // per index, nodes holding the last node with the id
        std::vector<Kind> kinds;
        std::vector<int> list_index;         // per graph node
        std::vector<Kind> list_kinds;
        std::vector<GraphLink> graph_links;
        std::vector<Ref> unknown;            // ids no node has; their indices are ~position
        std::vector<std::vector<Link>> incoming;
        std::unordered_map<int, int> outputs;  // pin -> node
        std::vector<int> output_pins;          // first-insertion order
        std::vector<Region *> regions, region_order;
        bool prepared = false;
        size_t words = 0;
        std::unordered_map<int, int> bits;
        std::vector<int> bit_pins;
        std::vector<int> node_values, unknown_values;  // the last value of each node and of each ~node
        std::vector<Value> values;
        std::vector<int> children;
        std::vector<uint64_t> arena;
        uint32_t epoch = 0;
    };
    struct Region {
        Ref node, assets, declared, continuation, states;
        std::vector<Body *> bodies;
        std::unordered_map<int, int> updates;  // dict(states)
        std::map<std::vector<int>, std::pair<std::vector<int>, std::vector<int>>> required;
        std::unordered_map<int, Ref> output_types;
    };
    using Requirement = std::pair<std::vector<int>, std::vector<int>>;

    static uint64_t key(int node, int pin) { return uint64_t(uint32_t(node)) << 32 | uint32_t(pin); }
    static int node_of(uint64_t key) { return int(uint32_t(key >> 32)); }
    static int pin_of(uint64_t key) { return int(uint32_t(key)); }

    static std::vector<int> unique(std::vector<int> items) {
        std::sort(items.begin(), items.end());
        items.erase(std::unique(items.begin(), items.end()), items.end());
        return items;
    }

    [[noreturn]] static void missing(PyObject *key) {
        PyErr_SetObject(PyExc_KeyError, key);
        throw Fail();
    }

    int text(PyObject *string) {
        Py_ssize_t before = text_index_.size(), found = text_index_.add(0, string);
        if (found == before) texts_.emplace_back(Py_NewRef(string));
        return int(found);
    }

    Kind kind_of(PyObject *node) {
        Ref kind(node_field_(node, 1));
        if (same(kind.get(), n_.function_input)) return input;
        if (same(kind.get(), n_.function_output)) return output;
        if (same(kind.get(), n_.branch)) return branch;
        if (same(kind.get(), n_.repeat)) return repeat;
        if (same(kind.get(), n_.unassigned_value)) return unassigned;
        if (same(kind.get(), n_.output) || same(kind.get(), n_.material_surface)) return terminal;
        return plain;
    }

    int index_of(Body &body, PyObject *node_id, bool add_unknown) {
        Py_ssize_t found = body.indices.find(0, node_id);
        if (found >= 0) return int(found);
        if (!add_unknown) missing(node_id);
        body.unknown.emplace_back(Py_NewRef(node_id));
        body.unknown_values.push_back(-1);
        return ~int(body.unknown.size() - 1);
    }

    PyObject *node_object(Body &body, int node) {
        if (node < 0) missing(body.unknown[size_t(~node)].get());
        return body.nodes[size_t(node)];
    }

    PyObject *node_id(Body &body, int node) {
        return node < 0 ? body.unknown[size_t(~node)].get() : body.ids[size_t(node)];
    }

    int number(Body &body, uint64_t key) {
        int node = node_of(key);
        int &last = node < 0 ? body.unknown_values[size_t(~node)] : body.node_values[size_t(node)];
        for (int value = last; value >= 0; value = body.values[size_t(value)].next)
            if (body.values[size_t(value)].key == key) return value;
        body.values.push_back(Value{key, last});
        last = int(body.values.size() - 1);
        return last;
    }

    Body &make_body(PyObject *graph) {
        bodies_.push_back(std::make_unique<Body>());
        Body &body = *bodies_.back();
        body.graph = Ref(Py_NewRef(graph));
        Ref nodes(attribute(graph, n_.nodes));
        body.list = Ref(check(PySequence_Tuple(nodes.get())));
        for (Py_ssize_t position = 0; position < PyTuple_GET_SIZE(body.list.get()); ++position) {
            PyObject *node = PyTuple_GET_ITEM(body.list.get(), position);
            Ref identity(node_field_(node, 0));
            Kind kind = kind_of(node);
            Py_ssize_t before = body.indices.size();
            int index = int(body.indices.add(0, identity.get()));
            if (index < before) {
                body.nodes[size_t(index)] = node;
                body.kinds[size_t(index)] = kind;
            } else {
                body.ids.push_back(identity.get());
                body.held.push_back(std::move(identity));
                body.nodes.push_back(node);
                body.kinds.push_back(kind);
            }
            body.list_index.push_back(index);
            body.list_kinds.push_back(kind);
        }
        body.incoming.resize(body.nodes.size());
        body.regions.resize(body.nodes.size());
        body.node_values.assign(body.nodes.size(), -1);
        body.words = (size_t(std::count(body.kinds.begin(), body.kinds.end(), input)) + 63) / 64;
        Ref links(attribute(graph, n_.links));
        body.links = Ref(check(PySequence_Tuple(links.get())));
        for (Py_ssize_t position = 0; position < PyTuple_GET_SIZE(body.links.get()); ++position) {
            PyObject *link = PyTuple_GET_ITEM(body.links.get(), position);
            Ref source(link_field_(link, 0)), source_pin(link_field_(link, 1));
            Ref target(link_field_(link, 2)), pin(link_field_(link, 3));
            int target_index = int(body.indices.find(0, target.get()));
            if (target_index < 0) {
                body.graph_links.push_back({-1, -1, -1});
                continue;
            }
            Link entry{text(pin.get()), index_of(body, source.get(), true), text(source_pin.get()), 0};
            entry.value = number(body, key(entry.node, entry.source_pin));
            auto &incoming = body.incoming[size_t(target_index)];
            auto existing = std::find_if(incoming.begin(), incoming.end(),
                                         [&](const Link &item) { return item.pin == entry.pin; });
            if (existing == incoming.end()) incoming.push_back(entry);
            else *existing = entry;
            body.graph_links.push_back({target_index, entry.pin, entry.value});
        }
        for (Py_ssize_t position = 0; position < PyTuple_GET_SIZE(body.list.get()); ++position) {
            if (body.list_kinds[size_t(position)] != output) continue;
            Ref properties(node_field_(PyTuple_GET_ITEM(body.list.get(), position), 4));
            Ref pin(check(PyObject_GetItem(properties.get(), n_.pin_id)));
            int id = text(pin.get()), target = body.list_index[size_t(position)];
            if (body.outputs.insert_or_assign(id, target).second) body.output_pins.push_back(id);
        }
        return body;
    }

    Region &make_region(PyObject *node, Kind kind) {
        regions_.push_back(std::make_unique<Region>());
        Region &region = *regions_.back();
        region.node = Ref(Py_NewRef(node));
        if (kind == repeat) {
            Ref read(check(PyObject_CallOneArg(loop_body_, node)));
            Ref parts[3];
            unpack(read.get(), parts, 3);
            region.assets = Ref(check(PyTuple_Pack(1, parts[0].get())));
            region.declared = std::move(parts[1]);
            region.continuation = std::move(parts[2]);
        } else {
            region.assets = Ref(check(PyObject_CallOneArg(branch_arms_, node)));
            region.declared = Ref(check(PyTuple_New(0)));
            region.continuation = Ref(Py_NewRef(Py_None));
        }
        region.states = Ref(check(PyTuple_New(0)));
        each(region.assets.get(), [&](PyObject *asset) {
            Ref graph(attribute(asset, n_.graph));
            region.bodies.push_back(&make_body(graph.get()));
        });
        return region;
    }

    Region &region_of(Body &body, int node) {
        Region *region = body.regions[size_t(node)];
        if (!region) {
            region = &make_region(body.nodes[size_t(node)], body.kinds[size_t(node)]);
            for (Body *child : region->bodies) prepare(*child);
            prepare_states(*region);
            body.regions[size_t(node)] = region;
            body.region_order.push_back(region);
        }
        return *region;
    }

    void prepare(Body &root) {
        if (root.prepared) return;
        std::vector<std::pair<Body *, bool>> pending{{&root, false}};
        while (!pending.empty()) {
            auto [body, ready] = pending.back();
            pending.pop_back();
            if (body->prepared) continue;
            if (!ready) {
                pending.emplace_back(body, true);
                for (size_t position = 0; position < body->list_kinds.size(); ++position) {
                    Kind kind = body->list_kinds[position];
                    if (kind != branch && kind != repeat) continue;
                    size_t index = size_t(body->list_index[position]);
                    if (!body->regions[index]) {
                        body->regions[index] = &make_region(PyTuple_GET_ITEM(body->list.get(), Py_ssize_t(position)), kind);
                        body->region_order.push_back(body->regions[index]);
                    }
                    for (Body *child : body->regions[index]->bodies) pending.emplace_back(child, false);
                }
            } else {
                for (Region *region : body->region_order) prepare_states(*region);
                body->prepared = true;
                for (int pin : body->output_pins) {
                    output_alias(*body, pin);
                    inputs(*body, number(*body, key(body->outputs.at(pin), value_)));
                }
            }
        }
    }

    void prepare_states(Region &region) {
        if (!truth(region.declared.get())) return;
        if (region.bodies.empty()) {
            PyErr_SetString(PyExc_IndexError, "tuple index out of range");
            throw Fail();
        }
        Body &body = *region.bodies[0];
        Ref kept(check(PyList_New(0)));
        region.updates.clear();
        each(region.declared.get(), [&](PyObject *pair) {
            Ref parts[2];
            unpack(pair, parts, 2);
            int alias = output_alias(body, text(parts[1].get()));
            Ref differs(check(PyObject_RichCompare(alias < 0 ? Py_None : texts_[alias].get(), parts[0].get(), Py_NE)));
            if (!truth(differs.get())) return;
            Ref state(check(PyTuple_Pack(2, parts[0].get(), parts[1].get())));
            check(PyList_Append(kept.get(), state.get()));
            region.updates[text(parts[0].get())] = text(parts[1].get());
        });
        region.states = Ref(check(PyList_AsTuple(kept.get())));
    }

    int output_node(Body &body, int pin) {
        auto found = body.outputs.find(pin);
        if (found == body.outputs.end()) missing(texts_[pin].get());
        return found->second;
    }

    int output_alias(Body &body, int pin) {
        int source = node_of(body.values[size_t(canonical(body, number(body, key(output_node(body, pin), value_))))].key);
        PyObject *node = node_object(body, source);
        if (body.kinds[size_t(source)] != input) return -1;
        Ref properties(node_field_(node, 4));
        Ref pin_id(check(PyObject_GetItem(properties.get(), n_.pin_id)));
        return text(pin_id.get());
    }

    int canonical(Body &body, int value) {
        prepare(body);
        std::vector<int> trail;
        while (body.values[size_t(value)].alias < 0) {
            trail.push_back(value);
            uint64_t current = body.values[size_t(value)].key;
            int node = node_of(current);
            node_object(body, node);
            Kind kind = body.kinds[size_t(node)];
            int pin;
            if (kind == output) {
                pin = value_;
            } else if (kind == branch || kind == repeat) {
                pin = alias_input(region_of(body, node), pin_of(current));
                if (pin < 0) break;
            } else {
                break;
            }
            const auto &incoming = body.incoming[size_t(node)];
            auto link = std::find_if(incoming.begin(), incoming.end(), [&](const Link &item) { return item.pin == pin; });
            if (link == incoming.end()) missing(texts_[pin].get());
            value = link->value;
        }
        int alias = body.values[size_t(value)].alias;
        int result = alias < 0 ? value : alias;
        for (int item : trail) body.values[size_t(item)].alias = result;
        return result;
    }

    int alias_input(Region &region, int pin) {
        if (region.bodies.empty()) {
            PyErr_SetString(PyExc_IndexError, "list index out of range");
            throw Fail();
        }
        int source = output_alias(*region.bodies[0], pin);
        bool agree = true;
        for (size_t index = 1; index < region.bodies.size(); ++index)
            agree &= output_alias(*region.bodies[index], pin) == source;
        if (source < 0 || !agree) return -1;
        return region.updates.count(source) ? -1 : source;
    }

    PyObject *output_type(Region &region, int pin) {
        auto found = region.output_types.find(pin);
        if (found != region.output_types.end()) return found->second.get();
        Ref first(check(PySequence_GetItem(region.assets.get(), 0)));
        Ref outputs(attribute(first.get(), n_.outputs));
        Ref result;
        each(outputs.get(), [&](PyObject *candidate) {
            Ref pin_id(attribute(candidate, n_.pin_id));
            if (!same(pin_id.get(), texts_[pin].get())) return true;
            result = Ref(attribute(candidate, n_.value_type));
            return false;
        });
        if (!result.get()) {
            PyErr_SetNone(PyExc_StopIteration);
            throw Fail();
        }
        return region.output_types.emplace(pin, std::move(result)).first->second.get();
    }

    int value_source(Body &body, int value) {
        if (body.values[size_t(value)].source >= 0) return body.values[size_t(value)].source;
        int start = value;
        for (;;) {
            uint64_t current = body.values[size_t(value)].key;
            int node = node_of(current);
            node_object(body, node);
            if (body.kinds[size_t(node)] != branch) break;
            Region &region = region_of(body, node);
            if (same(output_type(region, pin_of(current)), n_.execution)) break;
            int source = alias_input(region, pin_of(current));
            if (source < 0) break;
            const auto &incoming = body.incoming[size_t(node)];
            if (incoming.empty()) missing(body.ids[size_t(node)]);
            auto link = std::find_if(incoming.begin(), incoming.end(),
                                     [&](const Link &item) { return item.pin == source; });
            if (link == incoming.end()) missing(texts_[source].get());
            value = link->value;
        }
        body.values[size_t(start)].source = value;
        return value;
    }

    void read_children(Body &body, int value) {
        if (body.values[size_t(value)].children >= 0) return;
        uint64_t current = body.values[size_t(value)].key;
        int node = node_of(current);
        node_object(body, node);
        Kind kind = body.kinds[size_t(node)];
        const std::vector<int> *needed = nullptr;  // sorted
        if (kind == branch || kind == repeat) needed = &required(region_of(body, node), {pin_of(current)}).first;
        std::vector<int> result;
        for (const Link &link : body.incoming[size_t(node)]) {
            if (needed && !(kind == branch && link.pin == condition_) &&
                !std::binary_search(needed->begin(), needed->end(), link.pin))
                continue;
            result.push_back(value_source(body, link.value));
        }
        body.values[size_t(value)].children = int(body.children.size());
        body.values[size_t(value)].count = int(result.size());
        body.children.insert(body.children.end(), result.begin(), result.end());
    }

    int bit_of(Body &body, int pin) {
        auto found = body.bits.emplace(pin, int(body.bit_pins.size()));
        if (found.second) body.bit_pins.push_back(pin);
        return found.first->second;
    }

    size_t inputs(Body &body, int value) {
        prepare(body);
        if (body.values[size_t(value)].bits >= 0) return size_t(body.values[size_t(value)].bits);
        std::vector<std::pair<int, bool>> pending{{value, false}};
        while (!pending.empty()) {
            auto [current, ready] = pending.back();
            pending.pop_back();
            if (body.values[size_t(current)].bits >= 0) continue;
            int node = node_of(body.values[size_t(current)].key);
            PyObject *object = node_object(body, node);
            if (body.kinds[size_t(node)] == input) {
                Ref properties(node_field_(object, 4));
                Ref pin_id(check(PyObject_GetItem(properties.get(), n_.pin_id)));
                int bit = bit_of(body, text(pin_id.get()));
                size_t offset = body.arena.size();
                body.arena.resize(offset + body.words);
                body.arena[offset + size_t(bit) / 64] |= uint64_t(1) << (bit % 64);
                body.values[size_t(current)].bits = int64_t(offset);
                continue;
            }
            read_children(body, current);
            int begin = body.values[size_t(current)].children, count = body.values[size_t(current)].count;
            if (ready) {
                size_t offset = body.arena.size();
                body.arena.resize(offset + body.words);
                for (int index = begin; index < begin + count; ++index) {
                    size_t other = size_t(body.values[size_t(body.children[size_t(index)])].bits);
                    for (size_t word = 0; word < body.words; ++word) body.arena[offset + word] |= body.arena[other + word];
                }
                body.values[size_t(current)].bits = int64_t(offset);
            } else {
                pending.emplace_back(current, true);
                for (int index = begin; index < begin + count; ++index) {
                    int child = body.children[size_t(index)];
                    if (body.values[size_t(child)].bits < 0) pending.emplace_back(child, false);
                }
            }
        }
        return size_t(body.values[size_t(value)].bits);
    }

    std::vector<int> output_inputs(Body &body, const std::vector<int> &pins) {
        std::vector<uint64_t> all(body.words);
        for (int pin : pins) {
            size_t offset = inputs(body, number(body, key(output_node(body, pin), value_)));
            for (size_t word = 0; word < all.size(); ++word) all[word] |= body.arena[offset + word];
        }
        std::vector<int> result;
        for (size_t word = 0; word < all.size(); ++word) {
            for (uint64_t rest = all[word]; rest; rest &= rest - 1) {
                unsigned long bit;
                _BitScanForward64(&bit, rest);
                result.push_back(body.bit_pins[word * 64 + bit]);
            }
        }
        return unique(std::move(result));
    }

    const Requirement &required(Region &region, std::vector<int> outputs) {
        outputs = unique(std::move(outputs));
        auto found = region.required.find(outputs);
        if (found != region.required.end()) return found->second;
        std::vector<int> wanted = outputs;
        if (region.continuation.get() != Py_None) wanted.push_back(text(region.continuation.get()));
        wanted = unique(std::move(wanted));
        std::vector<int> inputs;
        for (Body *body : region.bodies) {
            auto read = output_inputs(*body, wanted);
            inputs.insert(inputs.end(), read.begin(), read.end());
        }
        inputs = unique(std::move(inputs));
        std::vector<int> pending = inputs;
        while (!pending.empty()) {
            int source = pending.back();
            pending.pop_back();
            auto update = region.updates.find(source);
            if (update == region.updates.end() || std::binary_search(wanted.begin(), wanted.end(), update->second))
                continue;
            wanted.insert(std::upper_bound(wanted.begin(), wanted.end(), update->second), update->second);
            for (int pin : output_inputs(*region.bodies[0], {update->second})) {
                auto place = std::lower_bound(inputs.begin(), inputs.end(), pin);
                if (place != inputs.end() && *place == pin) continue;
                inputs.insert(place, pin);
                pending.push_back(pin);
            }
        }
        return region.required.emplace(std::move(outputs), Requirement(std::move(inputs), std::move(wanted)))
            .first->second;
    }

    std::vector<std::pair<int, std::vector<int>>> live(Body &body, const std::vector<int> *outputs) {
        std::vector<int> pending;
        if (outputs) {
            for (int pin : *outputs) pending.push_back(number(body, key(output_node(body, pin), value_)));
        } else {
            for (size_t position = 0; position < body.list_kinds.size(); ++position) {
                Kind kind = body.list_kinds[position];
                if (kind == output || kind == terminal)
                    pending.push_back(number(body, key(body.list_index[position], value_)));
            }
        }
        uint32_t epoch = ++body.epoch;
        std::vector<std::pair<int, int>> reached;
        while (!pending.empty()) {
            int current = pending.back();
            pending.pop_back();
            if (body.values[size_t(current)].seen == epoch) continue;
            body.values[size_t(current)].seen = epoch;
            reached.emplace_back(node_of(body.values[size_t(current)].key), pin_of(body.values[size_t(current)].key));
            read_children(body, current);
            const Value &item = body.values[size_t(current)];
            pending.insert(pending.end(), body.children.begin() + item.children,
                           body.children.begin() + item.children + item.count);
        }
        std::stable_sort(reached.begin(), reached.end(), [](const auto &left, const auto &right) {
            return left.first < right.first;
        });
        std::vector<std::pair<int, std::vector<int>>> used;
        for (auto [node, pin] : reached) {
            if (used.empty() || used.back().first != node) used.emplace_back(node, std::vector<int>());
            used.back().second.push_back(pin);
        }
        return used;
    }

    PyObject *texts(const std::vector<int> &ids, bool frozen) {
        Ref items(check(frozen ? PyFrozenSet_New(nullptr) : PySet_New(nullptr)));
        for (int id : ids) check(PySet_Add(items.get(), texts_[size_t(id)].get()));
        return items.release();
    }

    Py_ssize_t trim(Body &body, bool all, const std::vector<int> &outputs, const std::vector<int> &keep) {
        auto request = std::make_tuple(&body, all, outputs, keep);
        auto done = done_.find(request);
        if (done != done_.end()) return done->second;
        auto used = live(body, all ? nullptr : &outputs);
        size_t count = body.nodes.size();
        std::vector<char> live_nodes(count), kept_nodes(count), named(count);
        std::vector<Ref> regions(count), unassigned(count);
        std::vector<std::vector<int>> allowed(count), empty(count);
        for (auto &[node, pins] : used) {
            live_nodes[size_t(node)] = 1;
            Kind kind = body.kinds[size_t(node)];
            if (kind != branch && kind != repeat) continue;
            Region &region = region_of(body, node);
            Requirement requirement = required(region, pins);
            Ref children(check(PyList_New(0)));
            for (Body *child : region.bodies) {
                Ref index(check(PyLong_FromSsize_t(trim(*child, false, requirement.second, requirement.first))));
                check(PyList_Append(children.get(), index.get()));
            }
            Ref inputs(texts(requirement.first, true)), results(texts(requirement.second, true));
            Ref child_tuple(check(PyList_AsTuple(children.get())));
            regions[size_t(node)] = Ref(check(PyTuple_Pack(7, region.node.get(), region.assets.get(),
                region.states.get(), region.continuation.get(), inputs.get(), results.get(), child_tuple.get())));
            std::vector<int> open;
            for (int pin : pins) {
                const auto &read = required(region, {pin}).first;
                open.insert(open.end(), read.begin(), read.end());
            }
            if (kind == branch) open.push_back(condition_);
            allowed[size_t(node)] = unique(std::move(open));
            if (kind != branch) continue;
            std::vector<int> pins_empty;
            for (const Link &link : body.incoming[size_t(node)]) {
                if (link.pin == condition_) continue;
                int source = node_of(body.values[size_t(value_source(body, link.value))].key);
                node_object(body, source);
                if (body.kinds[size_t(source)] == Kind::unassigned) pins_empty.push_back(link.pin);
            }
            empty[size_t(node)] = unique(std::move(pins_empty));
            unassigned[size_t(node)] = Ref(texts(empty[size_t(node)], false));
        }
        Py_ssize_t link_count = PyTuple_GET_SIZE(body.links.get());
        std::vector<int> sources(static_cast<size_t>(link_count));
        for (Py_ssize_t position = 0; position < link_count; ++position) {
            GraphLink &link = body.graph_links[size_t(position)];
            if (link.value < 0) {
                PyObject *object = PyTuple_GET_ITEM(body.links.get(), position);
                Ref node(link_field_(object, 0)), pin(link_field_(object, 1));
                int source = index_of(body, node.get(), true);
                link.value = number(body, key(source, text(pin.get())));
            }
            sources[size_t(position)] = value_source(body, link.value);
        }
        Ref kept(check(PyList_New(0))), region_items(check(PyList_New(0))), replaced(check(PyList_New(0)));
        Ref failure(Py_NewRef(Py_None)), keep_set(texts(keep, true)), nothing(check(PyTuple_New(0)));
        std::vector<int> kept_indices;
        for (Py_ssize_t position = 0; position < PyTuple_GET_SIZE(body.list.get()); ++position) {
            PyObject *node = PyTuple_GET_ITEM(body.list.get(), position);
            size_t index = size_t(body.list_index[size_t(position)]);
            Kind kind = body.list_kinds[size_t(position)];
            if (!live_nodes[index]) {
                if (kind != input) continue;
                int found = -1;
                if (PyObject *properties = PyObject_GetAttr(node, n_.properties)) {
                    if (PyObject *pin_id = PyObject_GetItem(properties, n_.pin_id)) {
                        found = PySequence_Contains(keep_set.get(), pin_id);
                        Py_DECREF(pin_id);
                    }
                    Py_DECREF(properties);
                }
                if (found < 0) {
                    failure = Ref(PyErr_GetRaisedException());
                    break;
                }
                if (!found) continue;
            }
            check(PyList_Append(kept.get(), node));
            kept_nodes[index] = 1;
            kept_indices.push_back(int(index));
            if (kind != branch && kind != repeat) continue;
            if (!regions[index].get()) {
                Ref identity(attribute(node, n_.node_id));
                failure = Ref(check(PyObject_CallOneArg(PyExc_KeyError, identity.get())));
                break;
            }
            Ref item(check(PyTuple_Pack(4, body.ids[index], regions[index].get(),
                unassigned[index].get() ? unassigned[index].get() : nothing.get(), body.nodes[index])));
            check(PyList_Append(region_items.get(), item.get()));
            named[index] = 1;
        }
        Ref links(check(PyList_New(0)));
        if (failure.get() == Py_None) {
            static SlotsCache link_slots(link_fields, 4);
            for (Py_ssize_t position = 0; position < link_count; ++position) {
                PyObject *object = PyTuple_GET_ITEM(body.links.get(), position);
                const GraphLink &link = body.graph_links[size_t(position)];
                int source = sources[size_t(position)];
                uint64_t read = body.values[size_t(source)].key;
                bool exact = Py_TYPE(object) == reinterpret_cast<PyTypeObject *>(link_type_);
                Ref rewired;
                if (exact && source == link.value) {
                    rewired = Ref(Py_NewRef(object));
                } else {
                    PyObject *node = node_id(body, node_of(read)), *pin = texts_[size_t(pin_of(read))].get();
                    if (exact) {
                        const Slots &fields = link_slots.of(link_type_);
                        PyObject *items[] = {node, pin, fields.get(object, 2), fields.get(object, 3)};
                        rewired = Ref(fields.make(items));
                    } else {
                        Ref arguments(check(PyTuple_Pack(1, object)));
                        Ref changes(check(Py_BuildValue("{sOsO}", "source_node", node, "source_pin", pin)));
                        rewired = Ref(check(PyObject_Call(replace_, arguments.get(), changes.get())));
                    }
                }
                if (link.target < 0 || !kept_nodes[size_t(node_of(read))] || !kept_nodes[size_t(link.target)]) continue;
                const auto &open = allowed[size_t(link.target)], &closed = empty[size_t(link.target)];
                if (regions[size_t(link.target)].get() && !std::binary_search(open.begin(), open.end(), link.pin)) continue;
                if (std::binary_search(closed.begin(), closed.end(), link.pin)) continue;
                check(PyList_Append(links.get(), rewired.get()));
            }
            for (size_t position = 0; position < kept_indices.size(); ++position) {
                if (!named[size_t(kept_indices[position])]) continue;
                Ref item(check(Py_BuildValue("(nO)", Py_ssize_t(position), body.ids[size_t(kept_indices[position])])));
                check(PyList_Append(replaced.get(), item.get()));
            }
        }
        Ref entry(check(PyTuple_Pack(6, body.graph.get(), kept.get(), links.get(), region_items.get(),
                                     replaced.get(), failure.get())));
        check(PyList_Append(entries_.get(), entry.get()));
        Py_ssize_t index = PyList_GET_SIZE(entries_.get()) - 1;
        done_.emplace(request, index);
        return index;
    }

    const GraphNames &n_;
    PyObject *branch_arms_, *loop_body_, *link_type_, *replace_;
    Fields node_field_{node_fields, 5}, link_field_{link_fields, 4};
    FlatMap text_index_;
    Ref entries_;
    std::vector<Ref> texts_;
    int value_ = 0, condition_ = 0;
    std::vector<std::unique_ptr<Body>> bodies_;
    std::vector<std::unique_ptr<Region>> regions_;
    std::map<std::tuple<Body *, bool, std::vector<int>, std::vector<int>>, Py_ssize_t> done_;
};

PyObject *py_region_plan(PyObject *, PyObject *const *arguments, Py_ssize_t count) {
    if (count != 6) {
        PyErr_SetString(PyExc_TypeError, "region_plan(graph, keep_inputs, branch_arms, loop_body, link_type, replace)");
        return nullptr;
    }
    return guarded([&] {
        return RegionPlan(arguments[2], arguments[3], arguments[4], arguments[5]).plan(arguments[0], arguments[1]);
    });
}

PyObject *py_region_passthroughs(PyObject *, PyObject *const *arguments, Py_ssize_t count) {
    if (count != 4) {
        PyErr_SetString(PyExc_TypeError, "region_passthroughs(node, pins, branch_arms, loop_body)");
        return nullptr;
    }
    return guarded([&] { return RegionPlan(arguments[2], arguments[3]).passthroughs(arguments[0], arguments[1]); });
}

PyObject *py_copy_properties(PyObject *, PyObject *const *arguments, Py_ssize_t count) {
    if (count != 2) {
        PyErr_SetString(PyExc_TypeError, "copy_properties(value, memo)");
        return nullptr;
    }
    return guarded([&] { return copy_properties(arguments[0], arguments[1]); });
}

PyMethodDef methods[] = {
    {"configure", py_configure, METH_VARARGS,
     "configure(object_type, array_type, invalid, semantic_text, route_tables, graph_keys): graph_snapshot's types"},
    {"checked_properties", reinterpret_cast<PyCFunction>(reinterpret_cast<void (*)()>(py_checked_properties)),
     METH_FASTCALL,
     "checked_properties(value, memo, validated, max_nodes, max_links) -> object: graph_snapshot._checked_properties"},
    {"copy_properties", reinterpret_cast<PyCFunction>(reinterpret_cast<void (*)()>(py_copy_properties)),
     METH_FASTCALL, "copy_properties(value, memo) -> object: value with each dict and list in it (frozen _Object and "
     "_Array too) copied as a plain dict or list, one copy per object through memo"},
    {"snapshot_graph", reinterpret_cast<PyCFunction>(reinterpret_cast<void (*)()>(py_snapshot_graph)), METH_FASTCALL,
     "snapshot_graph(nodes, links, node_type, link_type, node_kinds, extension, error, max_nodes, max_links)"
     " -> (nodes, node documents, links, link documents) | None: checked_graph_snapshot's loops"},
    {"function_diagnostics", reinterpret_cast<PyCFunction>(reinterpret_cast<void (*)()>(py_function_diagnostics)),
     METH_FASTCALL, "function_diagnostics(nodes, links, inputs, outputs, known, helpers) -> list: the node, "
     "interface, link, cycle, required-input and execution-fork checks of material_function._validate_material_function; "
     "the pins it finds are added to known"},
    {"execution_diagnostics", reinterpret_cast<PyCFunction>(reinterpret_cast<void (*)()>(py_execution_diagnostics)),
     METH_FASTCALL, "execution_diagnostics(nodes, links, pins, (execution kinds, execution type, diagnostic type)) -> "
     "tuple: shader_graph_execution.execution_diagnostics"},
    {"graph_json", reinterpret_cast<PyCFunction>(reinterpret_cast<void (*)()>(py_graph_json)), METH_FASTCALL,
     "graph_json(graph, fields, asset_digest) -> bytes: canonical_json of graph.to_dict() (or of a graph document) with "
     "each region node's asset fields replaced by {'recovered_asset_digest': asset_digest(value)}"},
    {"matches_graph", reinterpret_cast<PyCFunction>(reinterpret_cast<void (*)()>(py_matches_graph)), METH_FASTCALL,
     "matches_graph(graph, document, (graph, node, link, property types)) -> bool: "
     "material_function._matches_compiler_graph"},
    {"arrange_graph", reinterpret_cast<PyCFunction>(reinterpret_cast<void (*)()>(py_arrange_graph)), METH_FASTCALL,
     "arrange_graph(nodes, links, pins, width_provider, error): shader_graph_layout.arrange_shader_graph's layout; "
     "pins: callable or dict by node id, width_provider: callable or width"},
    {"canonical_json", py_canonical_json, METH_O,
     "canonical_json(value) -> bytes: json.dumps(value, sort_keys=True, separators=(',', ':')).encode()"},
    {"discard_links", reinterpret_cast<PyCFunction>(reinterpret_cast<void (*)()>(py_discard_links)), METH_FASTCALL,
     "discard_links(links, identities, table, start) -> removed: vanilla_function._RecoveryGraph.discard_links"},
    {"connect_link", reinterpret_cast<PyCFunction>(reinterpret_cast<void (*)()>(py_connect_link)), METH_FASTCALL,
     "connect_link(graph, source_node, source_pin, target_node, target_pin, max_links, link_type, error, "
     "base_connect) -> link: vanilla_function._RecoveryGraph.connect"},
    {"copy_region_value", reinterpret_cast<PyCFunction>(reinterpret_cast<void (*)()>(py_copy_region_value)),
     METH_FASTCALL, "copy_region_value(value, graph, nodes, links, region, copied, capture, ports, shared, helpers) "
     "-> (node, pin): vanilla_function._copy_region_value"},
    {"region_plan", reinterpret_cast<PyCFunction>(reinterpret_cast<void (*)()>(py_region_plan)), METH_FASTCALL,
     "region_plan(graph, keep_inputs, branch_arms, loop_body, link_type, replace) -> [(graph, kept, links, regions, "
     "replaced, failure), ...]: region_dependencies' trimmed bodies, children first"},
    {"region_passthroughs", reinterpret_cast<PyCFunction>(reinterpret_cast<void (*)()>(py_region_passthroughs)),
     METH_FASTCALL, "region_passthroughs(node, pins, branch_arms, loop_body) -> (states, inputs or None per pin)"},
    {nullptr, nullptr, 0, nullptr},
};

PyModuleDef module = {PyModuleDef_HEAD_INIT, "fastgraph", nullptr, -1, methods};

}

PyMODINIT_FUNC PyInit_fastgraph() { return PyModule_Create(&module); }
