#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <algorithm>
#include <climits>
#include <cstdint>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace {

using U = char32_t;
using S = std::u32string;
using V = std::u32string_view;
constexpr size_t NPOS = static_cast<size_t>(-1);

struct VHash {
    size_t operator()(V text) const noexcept {
        uint64_t hash = 0x9E3779B97F4A7C15ull ^ text.size();
        size_t index = 0;
        for (; index + 1 < text.size(); index += 2) {
            hash = (hash ^ (uint64_t(text[index]) << 32 | text[index + 1])) * 0xBF58476D1CE4E5B9ull;
            hash ^= hash >> 31;
        }
        if (index < text.size()) {
            hash = (hash ^ text[index]) * 0xBF58476D1CE4E5B9ull;
            hash ^= hash >> 31;
        }
        return static_cast<size_t>(hash);
    }
};
template <typename T> using VMap = std::unordered_map<V, T, VHash>;
using VSet = std::unordered_set<V, VHash>;

class Arena {
public:
    V keep(V text) {
        if (text.size() > left_) {
            size_t size = std::max<size_t>(text.size(), 16384);
            blocks_.emplace_back(new U[size]);
            next_ = blocks_.back().get();
            left_ = size;
        }
        U *start = next_;
        std::copy(text.begin(), text.end(), start);
        next_ += text.size();
        left_ -= text.size();
        return V(start, text.size());
    }
private:
    std::vector<std::unique_ptr<U[]>> blocks_;
    U *next_ = nullptr;
    size_t left_ = 0;
};

struct Fail {};

class Ref {
public:
    Ref() = default;
    explicit Ref(PyObject *object) : object_(object) {}
    Ref(const Ref &) = delete;
    Ref &operator=(const Ref &) = delete;
    Ref(Ref &&other) noexcept : object_(other.object_) { other.object_ = nullptr; }
    Ref &operator=(Ref &&other) noexcept { std::swap(object_, other.object_); return *this; }
    ~Ref() { Py_XDECREF(object_); }
    PyObject *get() const { return object_; }
    PyObject *release() { PyObject *object = object_; object_ = nullptr; return object; }
    explicit operator bool() const { return object_ != nullptr; }
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

[[noreturn]] void raise_index() {
    PyErr_SetString(PyExc_IndexError, "list index out of range");
    throw Fail();
}

PyObject *interned(const char *text) {
    PyObject *object = PyUnicode_InternFromString(text);
    return check(object);
}

struct Constants {
    PyObject *pure, *source, *join, *float_, *bool_, *integer, *empty, *output, *patch, *float32, *uint32, *four;
    PyObject *instruction, *label, *function, *module;
    PyObject *value_fields[6], *record_fields[4];
};
Constants &constants() {
    static Constants made;
    static bool ready = false;
    if (!ready) {
        made.pure = interned("pure"); made.source = interned("source"); made.join = interned("join");
        made.float_ = interned("float"); made.bool_ = interned("bool"); made.integer = interned("integer");
        made.empty = interned(""); made.output = interned("output"); made.patch = interned("patch");
        made.float32 = interned("float32"); made.uint32 = interned("uint32");
        made.four = check(PyLong_FromLong(4));
        made.instruction = interned("instruction"); made.label = interned("label");
        made.function = interned("function"); made.module = interned("module");
        const char *value_fields[] = {"kind", "operands", "controls", "signature", "scalar", "origin"};
        for (int index = 0; index < 6; ++index) made.value_fields[index] = interned(value_fields[index]);
        const char *record_fields[] = {"kind", "text", "opcode", "result"};
        for (int index = 0; index < 4; ++index) made.record_fields[index] = interned(record_fields[index]);
        ready = true;
    }
    return made;
}

bool space(U c) { return Py_UNICODE_ISSPACE(c); }
bool word(U c) {
    if (c < 128) return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_';
    return Py_UNICODE_ISALNUM(c);
}
bool decimal(U c) { return c < 128 ? c >= '0' && c <= '9' : Py_UNICODE_ISDECIMAL(c) != 0; }
bool alpha(U c) { return c < 128 ? (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') : Py_UNICODE_ISALPHA(c) != 0; }
bool ascii_digit(U c) { return c >= '0' && c <= '9'; }
bool hex_digit(U c) { return ascii_digit(c) || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F'); }
bool name_char(U c) {
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || ascii_digit(c) || c == '-' || c == '$' || c == '.' || c == '_';
}
bool storage_char(U c) { return word(c) || c == '-' || c == '.' || c == '$'; }

template <typename T> struct Uninitialized : std::allocator<T> {
    template <typename O> struct rebind { using other = Uninitialized<O>; };
    Uninitialized() = default;
    template <typename O> Uninitialized(const Uninitialized<O> &) noexcept {}
    template <typename O> void construct(O *place) noexcept { ::new (static_cast<void *>(place)) O; }
    template <typename O, typename... Arguments> void construct(O *place, Arguments &&...arguments) {
        ::new (static_cast<void *>(place)) O(std::forward<Arguments>(arguments)...);
    }
};
using Buffer = std::vector<U, Uninitialized<U>>;

template <typename Text> void append(Text &buffer, PyObject *object) {
    if (!PyUnicode_Check(object)) {
        PyErr_Format(PyExc_TypeError, "DXIL text must be str, not %.200s", Py_TYPE(object)->tp_name);
        throw Fail();
    }
    Py_ssize_t size = PyUnicode_GET_LENGTH(object);
    size_t start = buffer.size();
    buffer.resize(start + static_cast<size_t>(size));
    U *out = buffer.data() + start;
    int kind = PyUnicode_KIND(object);
    const void *data = PyUnicode_DATA(object);
    if (kind == PyUnicode_1BYTE_KIND) {
        const Py_UCS1 *chars = static_cast<const Py_UCS1 *>(data);
        for (Py_ssize_t index = 0; index < size; ++index) out[index] = chars[index];
    } else if (kind == PyUnicode_2BYTE_KIND) {
        const Py_UCS2 *chars = static_cast<const Py_UCS2 *>(data);
        for (Py_ssize_t index = 0; index < size; ++index) out[index] = chars[index];
    } else {
        const Py_UCS4 *chars = static_cast<const Py_UCS4 *>(data);
        for (Py_ssize_t index = 0; index < size; ++index) out[index] = chars[index];
    }
}

S read(PyObject *object) {
    S result;
    append(result, object);
    return result;
}

PyObject *make(V text) {
    U widest = 0;
    for (U c : text) widest = std::max(widest, c);
    if (widest < 128) {
        PyObject *object = check(PyUnicode_New(static_cast<Py_ssize_t>(text.size()), 127));
        Py_UCS1 *out = PyUnicode_1BYTE_DATA(object);
        for (size_t index = 0; index < text.size(); ++index) out[index] = static_cast<Py_UCS1>(text[index]);
        return object;
    }
    return check(PyUnicode_FromKindAndData(PyUnicode_4BYTE_KIND, text.data(), static_cast<Py_ssize_t>(text.size())));
}

class Raw {
public:
    explicit Raw(PyObject *object) {
        if (!PyUnicode_Check(object)) {
            PyErr_Format(PyExc_TypeError, "DXIL text must be str, not %.200s", Py_TYPE(object)->tp_name);
            throw Fail();
        }
        kind_ = PyUnicode_KIND(object);
        data_ = PyUnicode_DATA(object);
        size_ = PyUnicode_GET_LENGTH(object);
    }
    Py_ssize_t size() const { return size_; }
    U operator[](Py_ssize_t index) const { return PyUnicode_READ(kind_, data_, index); }
    bool starts(const char *prefix) const {
        for (Py_ssize_t index = 0; prefix[index]; ++index)
            if (index >= size_ || (*this)[index] != static_cast<U>(static_cast<unsigned char>(prefix[index]))) return false;
        return true;
    }
    Py_ssize_t skip_space() const {
        Py_ssize_t index = 0;
        while (index < size_ && space((*this)[index])) ++index;
        return index;
    }
    bool contains(const char *needle) const {
        if (kind_ == PyUnicode_1BYTE_KIND)
            return std::string_view(static_cast<const char *>(data_), size_).find(needle) != std::string_view::npos;
        Py_ssize_t length = static_cast<Py_ssize_t>(std::char_traits<char>::length(needle));
        for (Py_ssize_t start = 0; start + length <= size_; ++start) {
            Py_ssize_t index = 0;
            while (index < length && (*this)[start + index] == static_cast<U>(static_cast<unsigned char>(needle[index])))
                ++index;
            if (index == length) return true;
        }
        return false;
    }
private:
    int kind_;
    const void *data_;
    Py_ssize_t size_;
};

class Texts {
public:
    void reserve(size_t characters, size_t lines) {
        buffer_.reserve(characters);
        spans_.reserve(lines);
        origins_.reserve(lines);
    }
    void add(PyObject *object, size_t origin) {
        size_t start = buffer_.size();
        append(buffer_, object);
        spans_.emplace_back(start, buffer_.size() - start);
        origins_.push_back(origin);
    }
    void add(V text, size_t origin) {
        size_t start = buffer_.size();
        buffer_.insert(buffer_.end(), text.begin(), text.end());
        spans_.emplace_back(start, text.size());
        origins_.push_back(origin);
    }
    size_t size() const { return spans_.size(); }
    V operator[](size_t index) const { return V(buffer_.data() + spans_[index].first, spans_[index].second); }
    size_t origin(size_t index) const { return origins_[index]; }
private:
    Buffer buffer_;
    std::vector<std::pair<size_t, size_t>> spans_;
    std::vector<size_t> origins_;
};

template <typename Keep> Texts read_lines(PyObject *lines, Keep keep) {
    Texts result;
    if (PyList_CheckExact(lines) || PyTuple_CheckExact(lines)) {
        Py_ssize_t size = PySequence_Fast_GET_SIZE(lines);
        size_t characters = 0;
        for (Py_ssize_t index = 0; index < size; ++index) {
            PyObject *line = PySequence_Fast_GET_ITEM(lines, index);
            if (PyUnicode_Check(line)) characters += static_cast<size_t>(PyUnicode_GET_LENGTH(line));
        }
        result.reserve(characters, static_cast<size_t>(size));
        for (Py_ssize_t index = 0; index < size; ++index) {
            PyObject *line = PySequence_Fast_GET_ITEM(lines, index);
            if (keep(Raw(line))) result.add(line, static_cast<size_t>(index));
        }
        return result;
    }
    Ref iterator(check(PyObject_GetIter(lines)));
    size_t index = 0;
    while (PyObject *item = PyIter_Next(iterator.get())) {
        Ref owned(item);
        if (keep(Raw(item))) result.add(item, index);
        ++index;
    }
    if (PyErr_Occurred()) throw Fail();
    return result;
}
Texts read_lines(PyObject *lines) { return read_lines(lines, [](const Raw &) { return true; }); }

bool metadata_line(const Raw &line) {
    Py_ssize_t start = line.skip_space();
    return start < line.size() && line[start] == '!';
}
bool type_line(const Raw &line) { return line.size() && line[0] == '%'; }
bool payload_line(const Raw &line) {
    return line.contains("@dx.op.getMeshPayload.") || line.contains("@dx.op.dispatchMesh.");
}
bool access_line(const Raw &line) { return line.starts("attributes #") || line.starts("declare"); }
bool span_line(const Raw &line) {
    if (line.starts("define")) return true;
    Py_ssize_t index = line.skip_space();
    if (index >= line.size() || line[index] != '}') return false;
    while (++index < line.size()) if (!space(line[index])) return false;
    return true;
}

class Strings {
public:
    ~Strings() { for (auto &item : map_) Py_DECREF(item.second); }
    PyObject *get(V text) {
        auto found = map_.find(text);
        if (found != map_.end()) return found->second;
        V kept = arena_.keep(text);
        PyObject *object = make(kept);
        map_.emplace(kept, object);
        return object;
    }
    PyObject *take(V text) {
        PyObject *object = get(text);
        Py_INCREF(object);
        return object;
    }
private:
    VMap<PyObject *> map_;
    Arena arena_;
};

V strip(V text) {
    size_t start = 0, end = text.size();
    while (start < end && space(text[start])) ++start;
    while (end > start && space(text[end - 1])) --end;
    return text.substr(start, end - start);
}
V lstrip(V text) {
    size_t start = 0;
    while (start < text.size() && space(text[start])) ++start;
    return text.substr(start);
}
V rstrip(V text) {
    size_t end = text.size();
    while (end > 0 && space(text[end - 1])) --end;
    return text.substr(0, end);
}
bool starts(V text, const char *prefix) {
    size_t index = 0;
    for (; prefix[index]; ++index)
        if (index >= text.size() || text[index] != static_cast<U>(static_cast<unsigned char>(prefix[index]))) return false;
    return true;
}
bool at(V text, size_t position, const char *literal) { return position <= text.size() && starts(text.substr(position), literal); }
bool same(V text, const char *literal) {
    size_t index = 0;
    for (; literal[index]; ++index)
        if (index >= text.size() || text[index] != static_cast<U>(static_cast<unsigned char>(literal[index]))) return false;
    return index == text.size();
}
size_t find(V text, const char *needle, size_t from = 0) {
    size_t length = std::char_traits<char>::length(needle);
    U first = static_cast<U>(static_cast<unsigned char>(needle[0]));
    for (size_t index = from; index + length <= text.size(); ++index)
        if (text[index] == first && at(text, index, needle)) return index;
    return NPOS;
}
bool contains(V text, const char *needle) { return find(text, needle) != NPOS; }
size_t find_char(V text, U c, size_t from = 0) {
    for (size_t index = from; index < text.size(); ++index) if (text[index] == c) return index;
    return NPOS;
}
size_t rfind_char(V text, U c) {
    for (size_t index = text.size(); index > 0; --index) if (text[index - 1] == c) return index - 1;
    return NPOS;
}
std::vector<V> split_space(V text) {
    std::vector<V> parts;
    size_t index = 0;
    while (index < text.size()) {
        while (index < text.size() && space(text[index])) ++index;
        size_t start = index;
        while (index < text.size() && !space(text[index])) ++index;
        if (index > start) parts.push_back(text.substr(start, index - start));
    }
    return parts;
}
std::vector<V> split_on(V text, U separator) {
    std::vector<V> parts;
    size_t start = 0;
    for (size_t index = 0; index <= text.size(); ++index)
        if (index == text.size() || text[index] == separator) {
            parts.push_back(text.substr(start, index - start));
            start = index + 1;
        }
    return parts;
}
bool is_alpha(V text) {
    if (text.empty()) return false;
    for (U c : text) if (!alpha(c)) return false;
    return true;
}

PyObject *to_int(V text) {
    Ref object(make(text));
    return check(PyLong_FromUnicodeObject(object.get(), 10));
}

class Slots {
public:
    Slots(PyObject *type, PyObject *const *names, int count)
        : type_(reinterpret_cast<PyTypeObject *>(type)), count_(count) {
        for (int index = 0; index < count; ++index) {
            PyObject *descriptor = _PyType_LookupRef(type_, names[index]);
            PyMemberDef *member = descriptor && Py_IS_TYPE(descriptor, &PyMemberDescr_Type)
                ? reinterpret_cast<PyMemberDescrObject *>(descriptor)->d_member : nullptr;
            bool usable = member && member->type == Py_T_OBJECT_EX && !(member->flags & Py_READONLY);
            if (usable) offsets_[index] = member->offset;
            Py_XDECREF(descriptor);
            if (!usable) {
                PyErr_Format(PyExc_TypeError, "%R has no slot %R", type, names[index]);
                throw Fail();
            }
        }
    }
    PyObject *make(PyObject *const *values) const {
        PyObject *object = check(type_->tp_alloc(type_, 0));
        for (int index = 0; index < count_; ++index)
            *reinterpret_cast<PyObject **>(reinterpret_cast<char *>(object) + offsets_[index]) = Py_NewRef(values[index]);
        return object;
    }
private:
    PyTypeObject *type_;
    int count_;
    Py_ssize_t offsets_[8] = {};
};

size_t llvm_name(V text, size_t i) {
    size_t n = text.size();
    if (i < n && text[i] == '"') {
        size_t j = i + 1;
        while (j < n) {
            U c = text[j];
            if (c == '"') return j + 1;
            if (c == '\\') {
                if (j + 1 < n && text[j + 1] != '\n') { j += 2; continue; }
                return NPOS;
            }
            ++j;
        }
        return NPOS;
    }
    size_t j = i;
    while (j < n && name_char(text[j])) ++j;
    return j > i ? j : NPOS;
}

struct Span { size_t start = NPOS, end = NPOS; bool found() const { return start != NPOS; } };

template <typename Take> void ssa_each(V text, Take take) {
    size_t i = 0;
    while (i < text.size()) {
        if (text[i] == '%') {
            size_t end = llvm_name(text, i + 1);
            if (end != NPOS) { take(i, end); i = end; continue; }
        }
        ++i;
    }
}

struct Call { size_t start = NPOS, name_start = 0, name_end = 0, end = 0; bool found() const { return start != NPOS; } };
Call call_search(V text) {
    Call call;
    for (size_t i = 0; i < text.size(); ++i) {
        if (text[i] != '@') continue;
        size_t end = llvm_name(text, i + 1);
        if (end != NPOS && end < text.size() && text[end] == '(') {
            call.start = i; call.name_start = i + 1; call.name_end = end; call.end = end + 1;
            return call;
        }
    }
    return call;
}

struct Definition { Span result; size_t end = NPOS; bool found() const { return end != NPOS; } };
Definition value_match(V text) {
    Definition definition;
    size_t i = 0;
    while (i < text.size() && space(text[i])) ++i;
    if (i >= text.size() || text[i] != '%') return definition;
    size_t name_end = llvm_name(text, i + 1);
    if (name_end == NPOS) return definition;
    size_t j = name_end;
    while (j < text.size() && space(text[j])) ++j;
    if (j >= text.size() || text[j] != '=') return definition;
    definition.result = {i, name_end};
    definition.end = j + 1;
    return definition;
}

Span keyword_symbol(V text, size_t keyword) {
    if (keyword < text.size() && word(text[keyword])) return {};
    for (size_t k = keyword; k < text.size(); ++k) {
        if (text[k] == '@') {
            size_t end = llvm_name(text, k + 1);
            if (end != NPOS && end < text.size() && text[end] == '(') return {k + 1, end};
        }
        if (text[k] == '\n') break;
    }
    return {};
}

template <typename Take> void targets_each(V text, Take take) {
    size_t i = 0;
    while (i + 7 <= text.size()) {
        if (text[i] == 'l' && at(text, i, "label %") && i + 7 < text.size() && name_char(text[i + 7])) {
            size_t end = i + 7;
            while (end < text.size() && name_char(text[end])) ++end;
            take(i, i + 6, end);
            i = end;
            continue;
        }
        ++i;
    }
}

bool boundary(V text, size_t low, size_t high, size_t position) {
    bool before = position > low && word(text[position - 1]);
    bool after = position < high && word(text[position]);
    return before != after;
}

const char *const SCALAR_TYPES[] = {"half", "float", "double", "i1", "i8", "i16", "i32", "i64"};
const char *scalar_search(V text, size_t low, size_t high) {
    if (low > high) return nullptr;
    V bounded = text.substr(0, high);
    for (size_t p = low; p < high; ++p) {
        U c = text[p];
        if ((c != 'h' && c != 'f' && c != 'd' && c != 'i') || !boundary(text, low, high, p)) continue;
        for (const char *type : SCALAR_TYPES) {
            size_t length = std::char_traits<char>::length(type);
            if (at(bounded, p, type) && boundary(text, low, high, p + length)) return type;
        }
    }
    return nullptr;
}

PyObject *scalar_kind(const char *type) {
    Constants &made = constants();
    if (!type) return made.empty;
    std::string_view name(type);
    if (name == "half" || name == "float" || name == "double") return made.float_;
    return name == "i1" ? made.bool_ : made.integer;
}

const char *const ARGUMENT_TYPES[] = {"i1", "i8", "i16", "i32", "i64", "half", "float", "double"};
void literal_arguments(V text, size_t low, std::vector<Span> &values) {
    values.clear();
    size_t high = text.size(), p = low;
    while (p < high) {
        size_t value_end = NPOS, value_start = 0;
        U c = text[p];
        if ((c == 'i' || c == 'h' || c == 'f' || c == 'd') && boundary(text, low, high, p)) {
            for (const char *type : ARGUMENT_TYPES) {
                size_t length = std::char_traits<char>::length(type);
                if (!at(text, p, type) || p + length >= high || text[p + length] != ' ') continue;
                size_t v = p + length + 1, q = v;
                if (q < high && text[q] == '-') ++q;
                if (q < high && ascii_digit(text[q])) {
                    ++q;
                    while (q < high && (hex_digit(text[q]) || text[q] == 'x' || text[q] == 'X' || text[q] == '.'
                                        || text[q] == '+' || text[q] == '-')) ++q;
                    value_start = v; value_end = q;
                } else {
                    for (const char *word_value : {"true", "false", "undef"})
                        if (at(text, v, word_value)) {
                            value_start = v; value_end = v + std::char_traits<char>::length(word_value);
                            break;
                        }
                }
                if (value_end != NPOS) break;
            }
        }
        if (value_end != NPOS) {
            values.push_back({value_start, value_end});
            p = value_end;
        } else {
            ++p;
        }
    }
}

void number_literals(V text, std::vector<Span> &values) {
    values.clear();
    size_t p = 0, n = text.size();
    auto number_at = [&](size_t q) -> size_t {
        if (q + 2 < n && text[q] == '0' && text[q + 1] == 'x' && hex_digit(text[q + 2])) {
            size_t end = q + 2;
            while (end < n && hex_digit(text[end])) ++end;
            return end;
        }
        if (q < n && ascii_digit(text[q])) {
            size_t end = q + 1;
            while (end < n && (ascii_digit(text[end]) || text[end] == '.' || text[end] == 'e' || text[end] == '+'
                               || text[end] == '-')) ++end;
            return end;
        }
        return NPOS;
    };
    while (p < n) {
        size_t end = NPOS;
        U c = text[p];
        if ((c == '-' || ascii_digit(c)) && (p == 0 || !(text[p - 1] == '%' || text[p - 1] == '.' || word(text[p - 1])))) {
            if (c == '-') end = number_at(p + 1);
            if (end == NPOS) end = number_at(p);
        }
        if (end != NPOS) {
            values.push_back({p, end});
            p = end;
        } else {
            ++p;
        }
    }
}

struct Incoming { Span value, label; };
std::vector<Incoming> incoming_each(V text) {
    std::vector<Incoming> found;
    size_t n = text.size(), p = 0;
    while (p < n) {
        bool matched = false;
        if (text[p] == '[') {
            size_t spaces = p + 1;
            while (spaces < n && space(text[spaces])) ++spaces;
            for (size_t k = spaces + 1; k-- > p + 1 && !matched;) {
                for (size_t end = k + 1; end <= n && text[end - 1] != ',' && text[end - 1] != ']'; ++end) {
                    size_t q = end;
                    while (q < n && space(text[q])) ++q;
                    if (q >= n || text[q] != ',') continue;
                    ++q;
                    while (q < n && space(text[q])) ++q;
                    if (q >= n || text[q] != '%' || q + 1 >= n || !name_char(text[q + 1])) continue;
                    size_t label_end = q + 1;
                    while (label_end < n && name_char(text[label_end])) ++label_end;
                    size_t close = label_end;
                    while (close < n && space(text[close])) ++close;
                    if (close >= n || text[close] != ']') continue;
                    found.push_back({{k, end}, {q, label_end}});
                    p = close + 1;
                    matched = true;
                    break;
                }
            }
        }
        if (!matched) ++p;
    }
    return found;
}

std::vector<V> metadata_parts(V text) {
    if (!(starts(text, "!{") && text.back() == '}')) raise_graph("DXIL entry metadata is not a tuple.");
    V body = text.substr(2, text.size() - 3);
    bool nested = false;
    for (U c : body)
        if (c == '"' || c == '{' || c == '}' || c == '(' || c == ')' || c == '[' || c == ']') { nested = true; break; }
    std::vector<V> parts;
    if (!nested) {
        std::vector<V> pieces = split_on(body, ',');
        if (strip(pieces.back()).empty()) pieces.pop_back();
        for (V piece : pieces) parts.push_back(strip(piece));
        return parts;
    }
    size_t start = 0;
    long depth = 0;
    bool quoted = false, escaped = false;
    for (size_t index = 0; index < body.size(); ++index) {
        U c = body[index];
        if (escaped) escaped = false;
        else if (c == '\\' && quoted) escaped = true;
        else if (c == '"') quoted = !quoted;
        else if (!quoted) {
            if (c == '{' || c == '(' || c == '[') ++depth;
            else if (c == '}' || c == ')' || c == ']') --depth;
            else if (c == ',' && depth == 0) {
                parts.push_back(strip(body.substr(start, index - start)));
                start = index + 1;
            }
        }
    }
    if (quoted || depth) raise_graph("DXIL entry metadata is unbalanced.");
    V tail = strip(body.substr(start));
    if (!tail.empty()) parts.push_back(tail);
    return parts;
}

PyObject *operands_tuple(V text, Strings &strings) {
    std::vector<V> parts = metadata_parts(text);
    Ref tuple(check(PyTuple_New(static_cast<Py_ssize_t>(parts.size()))));
    for (size_t index = 0; index < parts.size(); ++index)
        PyTuple_SET_ITEM(tuple.get(), static_cast<Py_ssize_t>(index), strings.take(parts[index]));
    return tuple.release();
}

void add_definition(V line, PyObject *result, Strings &strings) {
    if (find_char(line, '!') == NPOS || !starts(lstrip(line), "!")) return;
    V text = strip(line);
    size_t j = 1;
    while (j < text.size() && (word(text[j]) || text[j] == '.')) ++j;
    if (j == 1 || !at(text, j, " = !{") || text.size() < j + 6 || text.back() != '}') return;
    for (size_t k = j + 5; k + 1 < text.size(); ++k) if (text[k] == '\n') return;
    Ref operands(operands_tuple(text.substr(j + 3), strings));
    check(PyDict_SetItem(result, strings.get(text.substr(0, j)), operands.get()));
}

PyObject *definitions_of(const Texts &lines, Strings &strings) {
    Ref result(check(PyDict_New()));
    for (size_t index = 0; index < lines.size(); ++index) add_definition(lines[index], result.get(), strings);
    return result.release();
}

PyObject *spans_of(const Texts &lines, Strings &strings) {
    Ref result(check(PyDict_New()));
    size_t index = 0;
    while (index < lines.size()) {
        V line = lines[index];
        Span name = starts(line, "define") ? keyword_symbol(line, 6) : Span{};
        if (!name.found()) { ++index; continue; }
        V symbol = line.substr(name.start, name.end - name.start);
        if (symbol.size() >= 2 && symbol.front() == '"' && symbol.back() == '"') symbol = symbol.substr(1, symbol.size() - 2);
        size_t stop = index + 1;
        while (stop < lines.size() && !same(strip(lines[stop]), "}")) ++stop;
        PyObject *key = strings.get(symbol);
        int known = PyDict_Contains(result.get(), key);
        check(known);
        if (stop >= lines.size() || known) raise_graph("DXIL carrier contains an invalid function table.");
        Ref span(check(Py_BuildValue("(nn)", static_cast<Py_ssize_t>(lines.origin(index)),
                                     static_cast<Py_ssize_t>(lines.origin(stop) + 1))));
        check(PyDict_SetItem(result.get(), key, span.get()));
        index = stop + 1;
    }
    return result.release();
}

enum Access { EFFECT = 0, READONLY = 1, READNONE = 2 };
const char *const ACCESS_NAMES[] = {"effect", "readonly", "readnone"};

struct Accesses {
    std::vector<std::pair<V, Access>> order;
    VMap<size_t> position;
    Access get(V name, Access fallback) const {
        auto found = position.find(name);
        return found == position.end() ? fallback : order[found->second].second;
    }
};

template <typename Lines> Accesses access_of(const Lines &lines) {
    Ref groups(check(PyDict_New()));
    for (size_t index = 0; index < lines.size(); ++index) {
        V line = lines[index];
        if (!starts(line, "attributes #")) continue;
        size_t j = 12;
        while (j < line.size() && decimal(line[j])) ++j;
        if (j == 12 || !at(line, j, " = ") || line.size() <= j + 3) continue;
        V rest = line.substr(j + 3);
        size_t newline = find_char(rest, '\n');
        if (newline != NPOS) {
            if (newline + 1 != rest.size() || newline == 0) continue;
            rest = rest.substr(0, newline);
        }
        Ref number(to_int(line.substr(12, j - 12)));
        Ref text(make(rest));
        check(PyDict_SetItem(groups.get(), number.get(), text.get()));
    }
    Accesses result;
    for (size_t index = 0; index < lines.size(); ++index) {
        V line = lines[index];
        Span name = starts(line, "declare") ? keyword_symbol(line, 7) : Span{};
        if (!name.found()) continue;
        Access access = EFFECT;
        size_t end = line.size();
        while (end > 0 && space(line[end - 1])) --end;
        size_t digits = end;
        while (digits > 0 && decimal(line[digits - 1])) --digits;
        if (digits < end && digits > 0 && line[digits - 1] == '#') {
            Ref number(to_int(line.substr(digits, end - digits)));
            PyObject *text = PyDict_GetItemWithError(groups.get(), number.get());
            if (!text && PyErr_Occurred()) throw Fail();
            if (text) {
                S value = read(text);
                access = contains(value, "readnone") ? READNONE : contains(value, "readonly") ? READONLY : EFFECT;
            }
        }
        V key = line.substr(name.start, name.end - name.start);
        auto found = result.position.find(key);
        if (found == result.position.end()) {
            result.position.emplace(key, result.order.size());
            result.order.emplace_back(key, access);
        } else {
            result.order[found->second].second = access;
        }
    }
    return result;
}

struct Statement { size_t block; size_t line; V text; };

struct Statements {
    std::vector<Statement> rows;
    std::vector<V> blocks;
    std::vector<Ref> block_objects;
    Arena owned;
    VMap<size_t> block_index;
    size_t block(V name) {
        auto found = block_index.find(name);
        if (found != block_index.end()) return found->second;
        blocks.push_back(name);
        block_index.emplace(name, blocks.size() - 1);
        return blocks.size() - 1;
    }
};

void statements_of(const Texts &lines, Statements &result) {
    size_t block = result.block(result.owned.keep(U"%0"));
    std::vector<V> pending;
    size_t start = 0;
    long depth = 0;
    for (size_t index = 0; index < lines.size(); ++index) {
        V stripped = strip(lines[index]);
        if (pending.empty()) {
            if (starts(stripped, "; <label>:") && stripped.size() > 10 && decimal(stripped[10])) {
                size_t end = 10;
                while (end < stripped.size() && decimal(stripped[end])) ++end;
                block = result.block(result.owned.keep(U"%" + S(stripped.substr(10, end - 10))));
                continue;
            }
            size_t end = 0;
            while (end < stripped.size() && name_char(stripped[end])) ++end;
            if (end > 0 && end < stripped.size() && stripped[end] == ':') {
                block = result.block(result.owned.keep(U"%" + S(stripped.substr(0, end))));
                continue;
            }
            if (stripped.empty() || stripped.front() == ';') continue;
            start = index;
        }
        V text = stripped;
        if (!starts(stripped, "[")) {
            size_t comment = find_char(stripped, ';');
            if (comment != NPOS) text = stripped.substr(0, comment);
        }
        pending.push_back(text);
        for (U c : text) depth += c == '[' ? 1 : c == ']' ? -1 : 0;
        if (depth <= 0) {
            V joined = pending[0];
            if (pending.size() > 1) {
                S built;
                for (size_t part = 0; part < pending.size(); ++part) {
                    if (part) built += U' ';
                    built += pending[part];
                }
                joined = result.owned.keep(built);
            }
            result.rows.push_back({block, start, joined});
            pending.clear();
            depth = 0;
        }
    }
    if (!pending.empty()) raise_graph("DXIL entry function ends inside a statement.");
}

void read_statements(PyObject *statements, Statements &result, Buffer &buffer) {
    struct Raw { size_t name, text_start, text_size, line; };
    std::vector<Raw> raw;
    std::vector<std::pair<size_t, size_t>> names;
    std::vector<Ref> objects;
    PyObject *last = nullptr;
    if (PyList_CheckExact(statements)) {
        size_t characters = 0;
        for (Py_ssize_t index = 0; index < PyList_GET_SIZE(statements); ++index) {
            PyObject *row = PyList_GET_ITEM(statements, index);
            if (PyTuple_CheckExact(row) && PyTuple_GET_SIZE(row) == 3 && PyUnicode_Check(PyTuple_GET_ITEM(row, 2)))
                characters += static_cast<size_t>(PyUnicode_GET_LENGTH(PyTuple_GET_ITEM(row, 2)));
        }
        buffer.reserve(characters + 64);
        raw.reserve(static_cast<size_t>(PyList_GET_SIZE(statements)));
        objects.reserve(static_cast<size_t>(PyList_GET_SIZE(statements)));
    }
    Ref iterator(check(PyObject_GetIter(statements)));
    while (PyObject *row = PyIter_Next(iterator.get())) {
        Ref owned(row);
        Ref items(PyTuple_CheckExact(row) ? Py_NewRef(row) : check(PySequence_Tuple(row)));
        Py_ssize_t size = PyTuple_GET_SIZE(items.get());
        if (size != 3) {
            if (size > 3) PyErr_SetString(PyExc_ValueError, "too many values to unpack (expected 3)");
            else PyErr_Format(PyExc_ValueError, "not enough values to unpack (expected 3, got %zd)", size);
            throw Fail();
        }
        PyObject *block = PyTuple_GET_ITEM(items.get(), 0);
        if (block != last) {
            size_t start = buffer.size();
            append(buffer, block);
            names.emplace_back(start, buffer.size() - start);
            last = block;
        }
        Raw item;
        item.name = names.size() - 1;
        Py_ssize_t line = PyLong_AsSsize_t(PyTuple_GET_ITEM(items.get(), 1));
        if (line == -1 && PyErr_Occurred()) throw Fail();
        item.line = static_cast<size_t>(line);
        item.text_start = buffer.size();
        append(buffer, PyTuple_GET_ITEM(items.get(), 2));
        item.text_size = buffer.size() - item.text_start;
        raw.push_back(item);
        objects.emplace_back(Py_NewRef(block));
    }
    if (PyErr_Occurred()) throw Fail();
    std::vector<size_t> blocks(names.size());
    for (size_t index = 0; index < names.size(); ++index)
        blocks[index] = result.block(V(buffer.data() + names[index].first, names[index].second));
    for (size_t index = 0; index < raw.size(); ++index) {
        result.rows.push_back({blocks[raw[index].name], raw[index].line,
                               V(buffer.data() + raw[index].text_start, raw[index].text_size)});
        result.block_objects.push_back(std::move(objects[index]));
    }
}

struct Control {
    std::vector<V> blocks;
    VMap<size_t> position;
    std::vector<std::vector<V>> successors;
    std::vector<std::pair<V, V>> conditions;
    VMap<size_t> condition_position;
    std::vector<std::vector<uint64_t>> dominators;
    const V *condition(V block) const {
        auto found = condition_position.find(block);
        return found == condition_position.end() ? nullptr : &conditions[found->second].second;
    }
};

bool has_bit(const std::vector<uint64_t> &bits, size_t index) { return bits[index / 64] >> (index % 64) & 1; }

Control control_of(const Statements &statements) {
    Control control;
    for (const Statement &statement : statements.rows) {
        V name = statements.blocks[statement.block];
        auto found = control.position.find(name);
        size_t block;
        if (found == control.position.end()) {
            block = control.blocks.size();
            control.position.emplace(name, block);
            control.blocks.push_back(name);
            control.successors.emplace_back();
        } else {
            block = found->second;
        }
        V text = statement.text;
        if (!(starts(text, "br ") || starts(text, "switch "))) continue;
        targets_each(text, [&](size_t, size_t group, size_t end) {
            V target = text.substr(group, end - group);
            auto &list = control.successors[block];
            for (V item : list) if (item == target) return;
            list.push_back(target);
        });
        if (starts(text, "switch ") || starts(text, "br i1 ")) {
            size_t comma = find_char(text, ',');
            V head = comma == NPOS ? text : text.substr(0, comma);
            Span first;
            ssa_each(head, [&](size_t start, size_t end) { if (!first.found()) first = {start, end}; });
            if (!first.found()) raise_index();
            V condition = head.substr(first.start, first.end - first.start);
            auto known = control.condition_position.find(name);
            if (known == control.condition_position.end()) {
                control.condition_position.emplace(name, control.conditions.size());
                control.conditions.emplace_back(name, condition);
            } else {
                control.conditions[known->second].second = condition;
            }
        }
    }
    size_t count = control.blocks.size(), words = (count + 63) / 64;
    std::vector<std::vector<size_t>> predecessors(count);
    for (size_t source = 0; source < count; ++source)
        for (V target : control.successors[source]) {
            auto found = control.position.find(target);
            if (found != control.position.end()) predecessors[found->second].push_back(source);
        }
    std::vector<uint64_t> full(words, ~uint64_t(0));
    if (count % 64) full.back() = (uint64_t(1) << (count % 64)) - 1;
    control.dominators.assign(count, full);
    if (count) {
        std::fill(control.dominators[0].begin(), control.dominators[0].end(), 0);
        control.dominators[0][0] = 1;
    }
    std::vector<uint64_t> shared(words);
    for (bool changed = true; changed;) {
        changed = false;
        for (size_t block = 1; block < count; ++block) {
            if (predecessors[block].empty()) {
                std::fill(shared.begin(), shared.end(), 0);
            } else {
                shared = full;
                for (size_t predecessor : predecessors[block])
                    for (size_t w = 0; w < words; ++w) shared[w] &= control.dominators[predecessor][w];
            }
            shared[block / 64] |= uint64_t(1) << (block % 64);
            if (shared != control.dominators[block]) {
                control.dominators[block] = shared;
                changed = true;
            }
        }
    }
    return control;
}

PyObject *control_object(const Control &control, Strings &strings) {
    size_t count = control.blocks.size();
    Ref blocks(check(PyList_New(static_cast<Py_ssize_t>(count))));
    Ref successors(check(PyDict_New())), conditions(check(PyDict_New())), dominators(check(PyDict_New()));
    for (size_t block = 0; block < count; ++block) {
        PyObject *name = strings.take(control.blocks[block]);
        PyList_SET_ITEM(blocks.get(), static_cast<Py_ssize_t>(block), name);
        Ref targets(check(PySet_New(nullptr)));
        for (V target : control.successors[block]) check(PySet_Add(targets.get(), strings.get(target)));
        check(PyDict_SetItem(successors.get(), name, targets.get()));
    }
    for (const auto &item : control.conditions)
        check(PyDict_SetItem(conditions.get(), strings.get(item.first), strings.get(item.second)));
    for (size_t block = 0; block < count; ++block) {
        Ref members(check(PySet_New(nullptr)));
        for (size_t member = 0; member < count; ++member)
            if (has_bit(control.dominators[block], member)) check(PySet_Add(members.get(), strings.get(control.blocks[member])));
        check(PyDict_SetItem(dominators.get(), strings.get(control.blocks[block]), members.get()));
    }
    return check(PyTuple_Pack(4, blocks.get(), successors.get(), conditions.get(), dominators.get()));
}

bool branches_of(const Control &control, size_t block, std::vector<size_t> &result, std::vector<char> &seen,
                 std::vector<size_t> &pending, std::vector<uint64_t> &strict) {
    result.clear();
    size_t count = control.blocks.size();
    strict = control.dominators[block];
    strict[block / 64] &= ~(uint64_t(1) << (block % 64));
    size_t dominator = NPOS;
    for (size_t item = 0; item < count; ++item)
        if (has_bit(strict, item) && control.dominators[item] == strict) { dominator = item; break; }
    if (dominator == NPOS) return true;
    seen.assign(count, 0);
    pending.assign(1, dominator);
    while (!pending.empty()) {
        size_t current = pending.back();
        pending.pop_back();
        if (seen[current] || current == block) continue;
        seen[current] = 1;
        for (V target : control.successors[current]) {
            auto found = control.position.find(target);
            if (found == control.position.end()) return false;
            pending.push_back(found->second);
        }
    }
    for (size_t item = 0; item < count; ++item) if (seen[item]) result.push_back(item);
    return true;
}

size_t type_match(V line) {
    if (line.empty() || line[0] != '%') return NPOS;
    auto rest = [&](size_t end) {
        size_t q = end;
        while (q < line.size() && space(line[q])) ++q;
        if (q >= line.size() || line[q] != '=') return false;
        ++q;
        while (q < line.size() && space(line[q])) ++q;
        if (!at(line, q, "type")) return false;
        return q + 4 >= line.size() || !word(line[q + 4]);
    };
    if (line.size() > 1 && line[1] == '"') {
        size_t end = llvm_name(line, 1);
        if (end != NPOS && rest(end)) return end;
    }
    size_t end = 1;
    while (end < line.size() && line[end] != '=' && !space(line[end])) ++end;
    return end > 1 && rest(end) ? end : NPOS;
}

V memory_root(V text) {
    for (size_t i = 0; i < text.size(); ++i) {
        if (text[i] != '@') continue;
        size_t end = llvm_name(text, i + 1);
        if (end == NPOS) continue;
        V found = text.substr(i, end - i);
        return starts(found, "@dx.op.") ? V() : found;
    }
    return V();
}

void operands_of(V text, const VSet &types, Arena &owned, std::vector<V> &result) {
    result.clear();
    V view = text;
    bool targets = false;
    targets_each(text, [&](size_t, size_t, size_t) { targets = true; });
    if (targets) {
        S reduced;
        size_t last = 0;
        targets_each(text, [&](size_t start, size_t, size_t end) {
            reduced.append(text.substr(last, start - last));
            last = end;
        });
        reduced.append(text.substr(last));
        view = owned.keep(reduced);
    }
    Call call = call_search(view);
    if (call.found()) view = view.substr(call.end);
    ssa_each(view, [&](size_t start, size_t end) {
        V token = view.substr(start, end - start);
        if (!types.count(token)) result.push_back(token);
    });
}

V before_last_paren(V text) {
    size_t close = rfind_char(text, ')');
    return close == NPOS ? text : text.substr(0, close);
}

V last_token(V text) {
    std::vector<V> parts = split_space(text);
    if (parts.empty()) raise_index();
    return parts.back();
}

bool small_operation(V text, long long &value) {
    size_t start = 0;
    while (start < text.size() && text[start] == '-') ++start;
    if (start == text.size()) return false;
    for (size_t index = start; index < text.size(); ++index) if (!ascii_digit(text[index])) return false;
    bool negative = start > 0;
    unsigned long long magnitude = 0;
    for (size_t index = start; index < text.size(); ++index) {
        if (magnitude > (~0ull - 9) / 10) { value = negative ? LLONG_MIN : LLONG_MAX; return true; }
        magnitude = magnitude * 10 + (text[index] - '0');
    }
    if (magnitude > static_cast<unsigned long long>(LLONG_MAX)) value = negative ? LLONG_MIN : LLONG_MAX;
    else value = negative ? -static_cast<long long>(magnitude) : static_cast<long long>(magnitude);
    return true;
}

enum Kind { PURE = 0, SOURCE = 1, JOIN = 2 };

struct Value {
    Kind kind;
    PyObject *signature;
    PyObject *scalar;
    bool has_origin;
    V origin;
    bool memory_read;
};

struct Waiting {
    size_t position;
    Kind kind;
    size_t operands, operand_count, controls, control_count;
    V body;
};

template <typename T> struct Ordered {
    std::vector<std::pair<V, T>> items;
    VMap<size_t> position;
    T *find(V key) {
        auto found = position.find(key);
        return found == position.end() ? nullptr : &items[found->second].second;
    }
    size_t slot(V key) {
        auto found = position.find(key);
        if (found != position.end()) return found->second;
        position.emplace(key, items.size());
        items.emplace_back(key, T());
        return items.size() - 1;
    }
    T &at(V key) { return items[slot(key)].second; }
    void reserve(size_t count) {
        items.reserve(count);
        position.reserve(count);
    }
};

struct Sample { PyObject *texture = nullptr, *site = nullptr; bool float_texel = true; };

bool one_of_words(V text, std::initializer_list<const char *> words) {
    for (const char *item : words) if (same(text, item)) return true;
    return false;
}

size_t words_after_first(V text, V (&words)[3]) {
    size_t count = 0, index = 0;
    bool first = true;
    while (index < text.size() && count < 3) {
        while (index < text.size() && space(text[index])) ++index;
        size_t start = index;
        while (index < text.size() && !space(text[index])) ++index;
        if (index == start) continue;
        if (first) first = false;
        else words[count++] = text.substr(start, index - start);
    }
    return count;
}

class Signatures {
public:
    template <typename Build> PyObject *get(V key, Build build) {
        auto found = map_.find(key);
        if (found != map_.end()) return found->second;
        held_.emplace_back(build());
        map_.emplace(arena_.keep(key), held_.back().get());
        return held_.back().get();
    }
private:
    VMap<PyObject *> map_;
    std::vector<Ref> held_;
    Arena arena_;
};

void keyed(S &key, V part) {
    key += static_cast<U>(part.size());
    key += part;
}

PyObject *flow(PyObject *lines_object, Py_ssize_t begin, PyObject *statements_object, PyObject *outputs,
               PyObject *handles_object, PyObject *value_type, PyObject *branches_callable) {
    Constants &made = constants();
    if (!PyDict_Check(handles_object) || !PyDict_Check(outputs)) {
        PyErr_SetString(PyExc_TypeError, "flow needs dicts of outputs and handles");
        throw Fail();
    }
    Slots slots(value_type, made.value_fields, 6);
    Strings strings;
    Arena owned;
    Texts lines = read_lines(lines_object, [](const Raw &line) { return type_line(line) || access_line(line); });
    VSet types;
    for (size_t index = 0; index < lines.size(); ++index) {
        V line = lines[index];
        size_t end = type_match(line);
        if (end != NPOS) types.insert(line.substr(0, end));
    }
    Accesses access = access_of(lines);
    Statements statements;
    Buffer statement_buffer;
    read_statements(statements_object, statements, statement_buffer);
    Control control = control_of(statements);
    S handle_buffer;
    std::vector<std::pair<size_t, size_t>> handle_spans;
    std::vector<PyObject *> handle_values;
    {
        Py_ssize_t position = 0;
        PyObject *key, *value;
        while (PyDict_Next(handles_object, &position, &key, &value)) {
            size_t start = handle_buffer.size();
            append(handle_buffer, key);
            handle_spans.emplace_back(start, handle_buffer.size() - start);
            handle_values.push_back(value);
        }
    }
    VMap<PyObject *> handles;
    for (size_t index = 0; index < handle_spans.size(); ++index)
        handles.emplace(V(handle_buffer.data() + handle_spans[index].first, handle_spans[index].second), handle_values[index]);

    size_t rows = statements.rows.size();
    std::vector<Value> values;
    std::vector<Waiting> pending;
    std::vector<V> pool;
    values.reserve(rows);
    pending.reserve(rows);
    VMap<size_t> index;
    index.reserve(rows);
    std::vector<std::pair<bool, V>> index_order;
    bool none_indexed = false;
    size_t none_position = 0;
    VMap<V> roots;
    VMap<std::vector<V>> addresses;
    Ordered<std::vector<V>> memory;
    Ordered<V> bodies;
    bodies.reserve(rows);
    Ordered<Sample> samples;
    std::vector<Ref> sample_refs;
    Signatures signatures;
    S key;
    std::vector<V> arguments;
    std::vector<Span> literal;
    Ref stores(check(PyList_New(0))), texture_reads(check(PyList_New(0))), resource_stores(check(PyList_New(0)));
    Ref discards(check(PyList_New(0))), exits(check(PyList_New(0)));
    Ref empty_tuple(check(PyTuple_New(0)));
    std::vector<size_t> between, branch_pending;
    std::vector<char> branch_seen;
    std::vector<uint64_t> branch_strict;

    for (size_t si = 0; si < rows; ++si) {
        const Statement &statement = statements.rows[si];
        V text = statement.text;
        Definition definition = value_match(text);
        bool has_result = definition.found();
        V result = has_result ? text.substr(definition.result.start, definition.result.end - definition.result.start) : V();
        V body = has_result ? strip(text.substr(definition.end)) : text;
        size_t first_space = find_char(body, ' ');
        V opcode = first_space == NPOS ? body : body.substr(0, first_space);
        Ref site_object;
        auto site = [&]() -> PyObject * {
            if (!site_object) {
                Ref line(check(PyLong_FromSsize_t(begin + 1 + static_cast<Py_ssize_t>(statement.line))));
                site_object = Ref(check(PyTuple_Pack(2, line.get(), statements.block_objects[si].get())));
            }
            return site_object.get();
        };
        if (same(opcode, "call")) {
            Call callee = call_search(body);
            if (!callee.found()) raise_graph("DXIL call has no callee.");
            V name = body.substr(callee.name_start, callee.name_end - callee.name_start);
            literal_arguments(body, callee.end, literal);
            long long operation = 0;
            bool has_operation = starts(name, "dx.op.") && !literal.empty()
                && small_operation(body.substr(literal[0].start, literal[0].end - literal[0].start), operation);
            operands_of(body, types, owned, arguments);
            if (starts(name, "dx.op.storeOutput") || starts(name, "dx.op.storePatchConstant")) {
                PyObject *scope = has_operation && operation == 5 ? made.output : has_operation && operation == 106 ? made.patch : nullptr;
                std::vector<V> tail = split_on(before_last_paren(body.substr(callee.end)), ',');
                if (!scope || tail.size() != 5) raise_graph("DXIL output store is malformed.");
                Ref identity(to_int(last_token(tail[1])));
                Ref row(to_int(last_token(tail[2])));
                Ref lane(to_int(last_token(tail[3])));
                Ref output_key(check(PyTuple_Pack(3, scope, identity.get(), row.get())));
                PyObject *semantic = PyDict_GetItemWithError(outputs, output_key.get());
                if (!semantic) {
                    if (PyErr_Occurred()) throw Fail();
                    raise_graph("DXIL output store has no signature row.");
                }
                V stored = last_token(tail[4]);
                PyObject *where = site();
                Ref store_site(check(PyTuple_Pack(3, PyTuple_GET_ITEM(where, 0), PyTuple_GET_ITEM(where, 1), made.four)));
                Ref entry(check(PyTuple_Pack(5, semantic, scope, lane.get(), strings.get(stored), store_site.get())));
                check(PyList_Append(stores.get(), entry.get()));
                continue;
            }
            if (has_operation && (operation == 140 || operation == 69 || operation == 67)) {
                std::vector<V> items = split_on(before_last_paren(body.substr(callee.end)), ',');
                Ref list(check(PyList_New(0)));
                for (V item : items) check(PyList_Append(list.get(), strings.get(last_token(item))));
                Ref number(check(PyLong_FromLongLong(operation)));
                Ref entry(check(PyTuple_Pack(4, number.get(), strings.get(name), list.get(), site())));
                check(PyList_Append(resource_stores.get(), entry.get()));
                continue;
            }
            if (has_operation && operation == 82) {
                V condition;
                if (!arguments.empty()) {
                    condition = arguments[0];
                } else {
                    size_t space_at = rfind_char(body, ' ');
                    V last = space_at == NPOS ? body : body.substr(space_at + 1);
                    size_t end = last.size();
                    while (end > 0 && last[end - 1] == ')') --end;
                    condition = last.substr(0, end);
                }
                Ref entry(check(PyTuple_Pack(2, strings.get(condition), site())));
                check(PyList_Append(discards.get(), entry.get()));
                continue;
            }
            if (!has_result) continue;
            if (starts(name, "dx.op.sample") || starts(name, "dx.op.textureLoad") || starts(name, "dx.op.textureGather")) {
                PyObject *texture = nullptr;
                for (V item : arguments) {
                    auto found = handles.find(item);
                    if (found != handles.end()) { texture = found->second; break; }
                }
                if (texture) {
                    bool float_texel = true;
                    for (size_t p = find(body, "%dx.types.ResRet."); p != NPOS; p = find(body, "%dx.types.ResRet.", p + 1)) {
                        size_t q = p + 17;
                        if (q + 1 < body.size() && (body[q] == 'f' || body[q] == 'i') && decimal(body[q + 1])) {
                            float_texel = body[q] == 'f';
                            break;
                        }
                    }
                    Sample &sample = samples.at(result);
                    sample_refs.emplace_back(Py_NewRef(texture));
                    sample_refs.emplace_back(Py_NewRef(site()));
                    sample = {texture, site(), float_texel};
                }
            }
            const char *scalar = scalar_search(body, 4, callee.start);
            Kind kind = access.get(name, EFFECT) == READNONE && !arguments.empty() ? PURE : SOURCE;
            size_t family_size = kind == PURE ? literal.size() : std::min<size_t>(literal.size(), 1);
            key.clear();
            key += U'c';
            keyed(key, name);
            for (size_t k = 0; k < family_size; ++k) keyed(key, body.substr(literal[k].start, literal[k].end - literal[k].start));
            PyObject *signature = signatures.get(key, [&] {
                Ref family(check(PyTuple_New(static_cast<Py_ssize_t>(family_size))));
                for (size_t k = 0; k < family_size; ++k)
                    PyTuple_SET_ITEM(family.get(), static_cast<Py_ssize_t>(k),
                                     strings.take(body.substr(literal[k].start, literal[k].end - literal[k].start)));
                return check(PyTuple_Pack(2, strings.get(name), family.get()));
            });
            pending.push_back({values.size(), kind, pool.size(), arguments.size(), 0, 0, body});
            pool.insert(pool.end(), arguments.begin(), arguments.end());
            bool memory_read = one_of_words(name, {"load", "atomicrmw", "cmpxchg"});
            values.push_back({kind, signature, scalar_kind(scalar), true, result, memory_read});
        } else if (same(opcode, "phi")) {
            size_t start = pool.size();
            for (const Incoming &item : incoming_each(body)) {
                V value = body.substr(item.value.start, item.value.end - item.value.start);
                if (value[0] == '%') pool.push_back(value);
            }
            size_t operand_count = pool.size() - start;
            size_t block = control.position.at(statements.blocks[statement.block]);
            if (!branches_of(control, block, between, branch_seen, branch_pending, branch_strict)) {
                Ref name(make(statements.blocks[statement.block]));
                Ref answer(check(PyObject_CallOneArg(branches_callable, name.get())));
                Ref iterator(check(PyObject_GetIter(answer.get())));
                while (PyObject *item = PyIter_Next(iterator.get())) {
                    Ref owned_item(item);
                    V branch = owned.keep(read(item));
                    if (const V *condition = control.condition(branch)) pool.push_back(*condition);
                }
                if (PyErr_Occurred()) throw Fail();
            } else {
                for (size_t item : between)
                    if (const V *condition = control.condition(control.blocks[item])) pool.push_back(*condition);
            }
            const char *scalar = scalar_search(body, 0, body.size());
            pending.push_back({values.size(), JOIN, start, operand_count, start + operand_count,
                               pool.size() - start - operand_count, body});
            values.push_back({JOIN, empty_tuple.get(), scalar_kind(scalar), has_result, result, false});
        } else if (has_result) {
            bodies.at(result) = body;
            operands_of(body, types, owned, arguments);
            V scalar_text = body;
            size_t to = find(body, " to ");
            if (to != NPOS) scalar_text = body.substr(to + 4);
            if (same(opcode, "select")) {
                size_t comma = find_char(body, ',');
                if (comma == NPOS) raise_index();
                scalar_text = body.substr(comma + 1);
            } else if (same(opcode, "extractvalue")) {
                scalar_text = V();
                for (size_t p = find_char(body, '.'); p != NPOS; p = find_char(body, '.', p + 1)) {
                    size_t q = p + 1;
                    if (q >= body.size() || (body[q] != 'f' && body[q] != 'i')) continue;
                    size_t end = q + 1;
                    while (end < body.size() && decimal(body[end])) ++end;
                    if (end == q + 1 || (end < body.size() && word(body[end]))) continue;
                    scalar_text = body[q] == 'f' ? V(U"float") : V(U"i32");
                    break;
                }
            }
            bool compare = same(opcode, "fcmp") || same(opcode, "icmp");
            const char *scalar = compare ? "i1" : scalar_search(scalar_text, 0, scalar_text.size());
            if (same(opcode, "alloca") || same(opcode, "getelementptr")) {
                const V *base = nullptr;
                for (const V &item : arguments) if (roots.count(item)) { base = &item; break; }
                V root = base ? roots[*base] : memory_root(body);
                if (!base && root.empty()) root = result;
                std::vector<V> address;
                if (base) {
                    auto known = addresses.find(*base);
                    if (known != addresses.end()) address = known->second;
                }
                for (V item : arguments) if (!base || item != *base) address.push_back(item);
                roots[result] = root;
                addresses[result] = std::move(address);
                continue;
            }
            if (same(opcode, "extractvalue") && !arguments.empty()) {
                if (Sample *sample = samples.find(arguments[0])) {
                    size_t comma = rfind_char(body, ',');
                    if (comma == NPOS) raise_index();
                    Ref lane(to_int(body.substr(comma + 1)));
                    int below = PyObject_RichCompareBool(lane.get(), made.four, Py_LT);
                    check(below);
                    if (below) {
                        Ref entry(check(PyTuple_Pack(4, sample->texture, lane.get(), strings.get(result), sample->site)));
                        check(PyList_Append(texture_reads.get(), entry.get()));
                    }
                }
            }
            bool memory_opcode = one_of_words(opcode, {"load", "atomicrmw", "cmpxchg"});
            bool sourced = false;
            if (same(opcode, "extractvalue") && !arguments.empty()) {
                auto known = index.find(arguments[0]);
                sourced = known != index.end() && values[known->second].kind == SOURCE;
            }
            Kind kind = memory_opcode || sourced || arguments.empty() ? SOURCE : PURE;
            if (kind == SOURCE && same(opcode, "extractvalue")) literal.clear();
            else number_literals(body, literal);
            V words[3];
            size_t word_count = words_after_first(body, words);
            V flags[3];
            size_t flag_count = 0;
            for (size_t k = 0; k < word_count; ++k) if (is_alpha(words[k])) flags[flag_count++] = words[k];
            key.clear();
            key += U'v';
            key += static_cast<U>(flag_count);
            keyed(key, opcode);
            for (size_t k = 0; k < flag_count; ++k) keyed(key, flags[k]);
            for (const Span &number : literal) keyed(key, body.substr(number.start, number.end - number.start));
            PyObject *signature = signatures.get(key, [&] {
                Ref flag_tuple(check(PyTuple_New(static_cast<Py_ssize_t>(flag_count))));
                for (size_t k = 0; k < flag_count; ++k)
                    PyTuple_SET_ITEM(flag_tuple.get(), static_cast<Py_ssize_t>(k), strings.take(flags[k]));
                Ref literal_tuple(check(PyTuple_New(static_cast<Py_ssize_t>(literal.size()))));
                for (size_t k = 0; k < literal.size(); ++k)
                    PyTuple_SET_ITEM(literal_tuple.get(), static_cast<Py_ssize_t>(k),
                                     strings.take(body.substr(literal[k].start, literal[k].end - literal[k].start)));
                return check(PyTuple_Pack(3, strings.get(opcode), flag_tuple.get(), literal_tuple.get()));
            });
            pending.push_back({values.size(), kind, pool.size(), arguments.size(), 0, 0, body});
            pool.insert(pool.end(), arguments.begin(), arguments.end());
            values.push_back({kind, signature, scalar_kind(scalar), true, result, memory_opcode});
        } else {
            if (same(opcode, "ret")) check(PyList_Append(exits.get(), site()));
            if (one_of_words(opcode, {"store", "atomicrmw", "cmpxchg"})) {
                operands_of(body, types, owned, arguments);
                const V *pointer = nullptr;
                for (const V &item : arguments) if (roots.count(item)) { pointer = &item; break; }
                V root = pointer ? roots[*pointer] : memory_root(body);
                std::vector<V> &stored = memory.at(root);
                for (V item : arguments) if (!pointer || item != *pointer) stored.push_back(item);
            }
            continue;
        }
        bool known = has_result ? index.count(result) != 0 : none_indexed;
        if (known) {
            Ref name(has_result ? make(result) : Py_NewRef(Py_None));
            PyErr_Format(graph_error(), "DXIL redefines %S.", name.get());
            throw Fail();
        }
        if (has_result) index.emplace(result, values.size() - 1);
        else { none_indexed = true; none_position = values.size() - 1; }
        index_order.emplace_back(has_result, result);
    }

    std::vector<Ref> numbers(values.size());
    auto number = [&](size_t value) -> PyObject * {
        if (!numbers[value]) numbers[value] = Ref(check(PyLong_FromSize_t(value)));
        return numbers[value].get();
    };
    std::vector<V> operands;
    std::vector<size_t> found;
    auto numbered = [&](const V *items, size_t count) {
        found.clear();
        for (size_t k = 0; k < count; ++k) {
            auto known = index.find(items[k]);
            if (known == index.end()) continue;
            if (std::find(found.begin(), found.end(), known->second) == found.end()) found.push_back(known->second);
        }
        Ref tuple(check(PyTuple_New(static_cast<Py_ssize_t>(found.size()))));
        for (size_t k = 0; k < found.size(); ++k)
            PyTuple_SET_ITEM(tuple.get(), static_cast<Py_ssize_t>(k), Py_NewRef(number(found[k])));
        return tuple;
    };
    Ref resolved(check(PyList_New(static_cast<Py_ssize_t>(pending.size()))));
    for (size_t position = 0; position < pending.size(); ++position) {
        const Waiting &waiting = pending[position];
        const Value &value = values[waiting.position];
        const V *items = pool.data() + waiting.operands;
        size_t count = waiting.operand_count;
        if (value.memory_read) {
            const V *pointer = nullptr;
            for (size_t k = 0; k < count; ++k) if (roots.count(items[k])) { pointer = &items[k]; break; }
            V root = pointer ? roots[*pointer] : memory_root(waiting.body);
            operands.clear();
            for (size_t k = 0; k < count; ++k) if (!pointer || items[k] != *pointer) operands.push_back(items[k]);
            if (pointer) {
                auto known = addresses.find(*pointer);
                if (known != addresses.end()) operands.insert(operands.end(), known->second.begin(), known->second.end());
            }
            if (std::vector<V> *stored = memory.find(root)) operands.insert(operands.end(), stored->begin(), stored->end());
            items = operands.data();
            count = operands.size();
        }
        Ref references = numbered(items, count);
        Ref controls = numbered(pool.data() + waiting.controls, waiting.control_count);
        Kind kind = PyTuple_GET_SIZE(references.get()) || waiting.kind != PURE ? waiting.kind : SOURCE;
        PyObject *kind_object = kind == PURE ? made.pure : kind == SOURCE ? made.source : made.join;
        PyObject *origin = value.has_origin ? strings.get(value.origin) : Py_None;
        PyObject *fields[6] = {kind_object, references.get(), controls.get(), value.signature, value.scalar, origin};
        PyList_SET_ITEM(resolved.get(), static_cast<Py_ssize_t>(position), slots.make(fields));
    }

    Ref index_object(check(PyDict_New()));
    for (const auto &item : index_order)
        check(PyDict_SetItem(index_object.get(), item.first ? strings.get(item.second) : Py_None,
                             number(item.first ? index[item.second] : none_position)));
    Ref samples_object(check(PyDict_New()));
    for (const auto &item : samples.items) {
        Ref entry(check(PyTuple_Pack(3, item.second.texture, item.second.site,
                                     item.second.float_texel ? made.float32 : made.uint32)));
        check(PyDict_SetItem(samples_object.get(), strings.get(item.first), entry.get()));
    }
    Ref bodies_object(check(PyDict_New()));
    if (PyList_GET_SIZE(resource_stores.get()))
        for (const auto &item : bodies.items)
            check(PyDict_SetItem(bodies_object.get(), strings.get(item.first), strings.get(item.second)));
    return check(PyTuple_Pack(9, resolved.get(), stores.get(), texture_reads.get(), resource_stores.get(),
                              samples_object.get(), discards.get(), exits.get(), index_object.get(), bodies_object.get()));
}

PyObject *material_sites(PyObject *lines_object, PyObject *statements_object, Py_ssize_t base, PyObject *wanted) {
    Strings strings;
    Statements statements;
    Buffer buffer;
    read_statements(statements_object, statements, buffer);
    Ordered<V> bodies;
    std::vector<size_t> sites;
    bodies.reserve(statements.rows.size());
    for (size_t index = 0; index < statements.rows.size(); ++index) {
        V text = statements.rows[index].text;
        Definition definition = value_match(text);
        if (!definition.found()) continue;
        size_t slot = bodies.slot(text.substr(definition.result.start, definition.result.end - definition.result.start));
        bodies.items[slot].second = strip(text.substr(definition.end));
        if (slot == sites.size()) sites.push_back(index);
        else sites[slot] = index;
    }
    Control control = control_of(statements);
    Texts lines = read_lines(lines_object, access_line);
    Accesses access = access_of(lines);
    VSet read_only;
    Ref uav(check(PyLong_FromLong(1 << 12)));
    const char *prefix = "@dx.op.annotateHandle(i32 216, %dx.types.Handle ";
    const char *properties = ", %dx.types.ResourceProperties { i32 ";
    for (const auto &item : bodies.items) {
        V body = item.second;
        for (size_t p = find(body, prefix); p != NPOS; p = find(body, prefix, p + 1)) {
            size_t q = p + std::char_traits<char>::length(prefix), comma = find_char(body, ',', q);
            if (comma == NPOS || comma == q || !at(body, comma, properties)) continue;
            size_t number = comma + std::char_traits<char>::length(properties), digits = number;
            if (digits < body.size() && body[digits] == '-') ++digits;
            size_t end = digits;
            while (end < body.size() && decimal(body[end])) ++end;
            if (end == digits || end >= body.size() || body[end] != ',') continue;
            Ref value(to_int(body.substr(number, end - number)));
            Ref masked(check(PyNumber_And(value.get(), uav.get())));
            int set = PyObject_IsTrue(masked.get());
            check(set);
            if (!set) read_only.insert(item.first);
            break;
        }
    }
    auto pure = [&](V body) {
        for (const char *memory : {"phi ", "load ", "store ", "alloca ", "atomicrmw ", "cmpxchg ", "fence "})
            if (starts(body, memory)) return false;
        bool call = starts(body, "call ") || starts(body, "tail call ");
        Call called = call ? call_search(body) : Call{};
        if (!called.found()) return !call;
        Access kind = access.get(body.substr(called.name_start, called.name_end - called.name_start), EFFECT);
        if (kind == READNONE) return true;
        if (kind != READONLY) return false;
        bool all = true;
        ssa_each(body, [&](size_t start, size_t end) {
            if (!all) return;
            V token = body.substr(start, end - start);
            V *defined = bodies.find(token);
            V text = defined ? *defined : V();
            if ((starts(text, "call %dx.types.Handle") || starts(text, "tail call %dx.types.Handle")) && !read_only.count(token))
                all = false;
        });
        return all;
    };
    Arena arena;
    std::vector<V> keys(bodies.items.size());
    VMap<V> numbered;
    numbered.reserve(bodies.items.size());
    S shape;
    for (size_t current = 0; current < bodies.items.size(); ++current) {
        V body = bodies.items[current].second;
        if (!pure(body)) {
            keys[current] = bodies.items[current].first;
            continue;
        }
        shape.clear();
        size_t last = 0;
        ssa_each(body, [&](size_t start, size_t end) {
            shape.append(body.substr(last, start - last));
            V token = body.substr(start, end - start);
            auto found = bodies.position.find(token);
            shape.append(found != bodies.position.end() && found->second < current ? keys[found->second] : token);
            last = end;
        });
        shape.append(body.substr(last));
        auto known = numbered.find(shape);
        if (known == numbered.end()) {
            std::string label = "#" + std::to_string(numbered.size());
            known = numbered.emplace(arena.keep(shape), arena.keep(S(label.begin(), label.end()))).first;
        }
        keys[current] = known->second;
    }
    Ref result(check(PyDict_New()));
    Ref iterator(check(PyObject_GetIter(wanted)));
    while (PyObject *item = PyIter_Next(iterator.get())) {
        Ref address(item);
        if (!PyUnicode_Check(item)) continue;
        S name = read(item);
        auto found = bodies.position.find(name);
        if (found == bodies.position.end()) continue;
        size_t site = sites[found->second];
        const Statement &row = statements.rows[site];
        size_t block = control.position.at(statements.blocks[row.block]);
        Ref dominated(check(PyFrozenSet_New(nullptr)));
        for (size_t other = 0; other < control.blocks.size(); ++other)
            if (has_bit(control.dominators[other], block))
                check(PySet_Add(dominated.get(), strings.get(control.blocks[other])));
        Ref line(check(PyLong_FromSsize_t(base + static_cast<Py_ssize_t>(row.line))));
        Ref entry(check(PyTuple_Pack(4, strings.get(keys[found->second]), statements.block_objects[site].get(),
                                     line.get(), dominated.get())));
        check(PyDict_SetItem(result.get(), item, entry.get()));
    }
    if (PyErr_Occurred()) throw Fail();
    return result.release();
}

class Words {
public:
    explicit Words(PyObject *set) {
        std::vector<std::pair<size_t, size_t>> spans;
        Ref iterator(check(PyObject_GetIter(set)));
        while (PyObject *item = PyIter_Next(iterator.get())) {
            Ref owned(item);
            size_t start = buffer_.size();
            append(buffer_, item);
            spans.emplace_back(start, buffer_.size() - start);
        }
        if (PyErr_Occurred()) throw Fail();
        for (const auto &span : spans) words_.insert(V(buffer_.data() + span.first, span.second));
    }
    bool has(V word) const { return words_.count(word) != 0; }
private:
    S buffer_;
    VSet words_;
};

bool split_first(V text, V &first, V &rest, bool &has_rest) {
    size_t start = 0;
    while (start < text.size() && space(text[start])) ++start;
    if (start == text.size()) return false;
    size_t end = start;
    while (end < text.size() && !space(text[end])) ++end;
    first = text.substr(start, end - start);
    size_t next = end;
    while (next < text.size() && space(text[next])) ++next;
    has_rest = next < text.size();
    rest = has_rest ? text.substr(next) : V();
    return true;
}

void opcode_of(V body, const Words &prefixes, const Words &flags, S &opcode) {
    V first, rest;
    bool has_rest = false;
    if (!split_first(body, first, rest, has_rest)) raise_graph("Recovered DXIL instruction is empty.");
    bool tokens = true;
    if (prefixes.has(first)) tokens = has_rest && split_first(V(rest), first, rest, has_rest);
    while (tokens && flags.has(first)) tokens = has_rest && split_first(V(rest), first, rest, has_rest);
    if (!tokens) raise_graph("Recovered DXIL instruction has no opcode.");
    for (U c : first)
        if (c >= 128) {
            Ref token(make(first));
            Ref folded(check(PyObject_CallMethod(token.get(), "casefold", nullptr)));
            opcode = read(folded.get());
            return;
        }
    opcode.assign(first);
    for (U &c : opcode) if (c >= 'A' && c <= 'Z') c += 32;
}

V strip_comment(V line) {
    if (find_char(line, '"') == NPOS) {
        size_t comment = find_char(line, ';');
        return rstrip(comment == NPOS ? line : line.substr(0, comment));
    }
    bool quoted = false, escaped = false;
    for (size_t index = 0; index < line.size(); ++index) {
        U c = line[index];
        if (escaped) { escaped = false; continue; }
        if (c == '\\' && quoted) { escaped = true; continue; }
        if (c == '"') { quoted = !quoted; continue; }
        if (c == ';' && !quoted) return rstrip(line.substr(0, index));
    }
    return rstrip(line);
}

long bracket_delta(V text) {
    long result = 0;
    if (find_char(text, '"') == NPOS) {
        for (U c : text) result += c == '[' ? 1 : c == ']' ? -1 : 0;
        return result;
    }
    bool quoted = false, escaped = false;
    for (U c : text) {
        if (escaped) { escaped = false; continue; }
        if (c == '\\' && quoted) escaped = true;
        else if (c == '"') quoted = !quoted;
        else if (!quoted && c == '[') ++result;
        else if (!quoted && c == ']') --result;
    }
    return result;
}

bool recovery_define(V line) {
    if (!starts(line, "define") || line.size() < 7 || !space(line[6])) return false;
    for (size_t k = 7; k < line.size(); ++k) if (line[k] == '\n') return false;
    size_t end = line.size();
    while (end > 0 && space(line[end - 1])) --end;
    return end > 0 && line[end - 1] == '{' && end - 1 >= 8;
}

bool recovery_label(V line) {
    if (line.empty() || line.back() != ':') return false;
    V name = line.substr(0, line.size() - 1);
    if (name.empty()) return false;
    U c = name[0];
    if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c == '$' || c == '.' || c == '_') {
        for (size_t k = 1; k < name.size(); ++k) if (!name_char(name[k])) return false;
        return true;
    }
    for (U d : name) if (!decimal(d)) return false;
    return true;
}

bool global_match(V line) {
    if (line.empty() || line[0] != '@') return false;
    auto rest = [&](size_t end) {
        size_t q = end;
        while (q < line.size() && space(line[q])) ++q;
        return q < line.size() && line[q] == '=';
    };
    if (line.size() > 1 && line[1] == '"') {
        size_t end = llvm_name(line, 1);
        if (end != NPOS && rest(end)) return true;
    }
    size_t end = 1;
    while (end < line.size() && line[end] != '=' && !space(line[end])) ++end;
    return end > 1 && rest(end);
}

bool module_line(V line) {
    if (starts(line, "target datalayout =") || starts(line, "target triple =") || starts(line, "source_filename ="))
        return true;
    if (type_match(line) != NPOS || global_match(line)) return true;
    if (starts(line, "declare") && line.size() > 7 && space(line[7])) return true;
    if (starts(line, "attributes") && line.size() > 10 && space(line[10])) {
        size_t q = 10;
        while (q < line.size() && space(line[q])) ++q;
        if (q < line.size() && line[q] == '#') {
            size_t digits = q + 1, end = digits;
            while (end < line.size() && decimal(line[end])) ++end;
            if (end > digits) {
                while (end < line.size() && space(line[end])) ++end;
                if (end < line.size() && line[end] == '=') return true;
            }
        }
    }
    if (starts(line, "!")) {
        for (int pass = 0; pass < 2; ++pass) {
            size_t end = 1;
            while (end < line.size() && (pass == 0 ? name_char(line[end]) : decimal(line[end]))) ++end;
            if (end == 1) continue;
            while (end < line.size() && space(line[end])) ++end;
            if (end < line.size() && line[end] == '=') return true;
        }
    }
    return false;
}

class Records {
public:
    Records(PyObject *type, PyObject *executable, PyObject *prefixes, PyObject *flags)
        : slots_(type, constants().record_fields, 4), list_(check(PyList_New(0))), executable_(executable),
          prefixes_(prefixes), flags_(flags) {}
    void add(PyObject *kind, V text) {
        valid_text(text);
        Ref value(make(text));
        Constants &made = constants();
        append(kind, value.get(), made.empty, made.empty);
    }
    void instruction(V line) {
        V body = line, result;
        if (!line.empty() && line[0] == '%') {
            size_t j = 1;
            while (j < line.size() && line[j] != '=' && !space(line[j])) ++j;
            size_t k = j;
            while (k < line.size() && space(line[k])) ++k;
            if (j > 1 && k < line.size() && line[k] == '=') {
                size_t start = k + 1, spaces = start;
                while (spaces < line.size() && space(line[spaces])) ++spaces;
                size_t body_start = spaces < line.size() ? spaces : (line.size() > start ? line.size() - 1 : NPOS);
                if (body_start != NPOS && find_char(line.substr(body_start), '\n') == NPOS) {
                    result = line.substr(0, j);
                    body = line.substr(body_start);
                }
            }
        }
        opcode_of(body, prefixes_, flags_, opcode_);
        valid_text(line);
        PyObject *opcode = opcodes_.get(opcode_);
        if (!executable_.has(opcode_)) {
            Ref shown(check(PyObject_Repr(opcode)));
            PyErr_Format(graph_error(), "Recovered DXIL opcode %U is not understood.", shown.get());
            throw Fail();
        }
        Ref text(make(line)), result_object(make(result));
        append(constants().instruction, text.get(), opcode, result_object.get());
    }
    PyObject *release() { return list_.release(); }
private:
    static void valid_text(V text) {
        if (text.empty() || text.size() > 2 * 1024 * 1024) raise_graph("Recovered DXIL record is invalid.");
        for (U c : text) if (c == 0 || c == '\r' || c == '\n') raise_graph("Recovered DXIL record is invalid.");
    }
    void append(PyObject *kind, PyObject *text, PyObject *opcode, PyObject *result) {
        PyObject *values[4] = {kind, text, opcode, result};
        Ref record(slots_.make(values));
        check(PyList_Append(list_.get(), record.get()));
    }
    Slots slots_;
    Ref list_;
    Words executable_, prefixes_, flags_;
    Strings opcodes_;
    S opcode_;
};

PyObject *recovery_records(PyObject *assembly, PyObject *record_type, PyObject *executable, PyObject *prefixes,
                           PyObject *flags) {
    Constants &made = constants();
    Ref line_list(check(PyUnicode_Splitlines(assembly, 0)));
    Texts lines = read_lines(line_list.get());
    Records records(record_type, executable, prefixes, flags);
    bool in_function = false;
    S pending;
    long pending_brackets = 0;
    for (size_t index = 0; index < lines.size(); ++index) {
        V source = lines[index];
        if (in_function && pending.empty() && starts(lstrip(source), ";")) {
            size_t q = 0;
            while (q < source.size() && space(source[q])) ++q;
            ++q;
            while (q < source.size() && space(source[q])) ++q;
            if (at(source, q, "<label>:")) {
                size_t digits = q + 8, end = digits;
                while (end < source.size() && decimal(source[end])) ++end;
                if (end > digits && (end == source.size() || !word(source[end]))) {
                    records.add(made.label, S(source.substr(digits, end - digits)) + U":");
                    continue;
                }
            }
        }
        V line = strip(strip_comment(source));
        if (line.empty()) continue;
        if (!pending.empty()) {
            pending += U' ';
            pending += line;
            pending_brackets += bracket_delta(line);
            if (pending_brackets < 0) raise_graph("Recovered DXIL instruction brackets are unbalanced.");
            if (pending_brackets == 0) {
                records.instruction(pending);
                pending.clear();
            }
            continue;
        }
        if (starts(line, "define") && recovery_define(line)) {
            if (in_function) raise_graph("Recovered DXIL contains nested functions.");
            records.add(made.function, line);
            in_function = true;
            continue;
        }
        if (in_function && same(line, "}")) {
            records.add(made.function, line);
            in_function = false;
            continue;
        }
        if (in_function) {
            if (line.back() == ':' && recovery_label(line)) {
                records.add(made.label, line);
                continue;
            }
            pending_brackets = bracket_delta(line);
            if (pending_brackets < 0) raise_graph("Recovered DXIL instruction brackets are unbalanced.");
            if (pending_brackets) pending = S(line);
            else records.instruction(line);
            continue;
        }
        if (!module_line(line)) {
            Ref shown(make(line.substr(0, std::min<size_t>(line.size(), 120))));
            PyErr_Format(graph_error(), "Recovered DXIL module statement is not understood: %U", shown.get());
            throw Fail();
        }
        records.add(made.module, line);
    }
    if (!pending.empty()) raise_graph("Recovered DXIL instruction is incomplete.");
    return records.release();
}

PyObject *entry_tables_of(const Texts &lines) {
    Strings strings;
    Ref definitions(definitions_of(lines, strings));
    Ref types(check(PyDict_New()));
    Ref payloads(check(PySet_New(nullptr)));
    for (size_t index = 0; index < lines.size(); ++index) {
        V line = lines[index];
        if (starts(line, "%")) {
            size_t j = 1;
            while (j < line.size() && storage_char(line[j])) ++j;
            if (j > 1 && at(line, j, " = type ") && line.size() > j + 8 && find_char(line.substr(j + 8), '\n') == NPOS)
                check(PyDict_SetItem(types.get(), strings.get(line.substr(0, j)), strings.get(line.substr(j + 8))));
        }
    }
    for (size_t index = 0; index < lines.size(); ++index) {
        V line = lines[index];
        if (contains(line, "@dx.op.getMeshPayload.")) {
            for (size_t p = 0; p < line.size(); ++p) {
                size_t q = at(line, p, "call ") ? p + 5 : at(line, p, "declare ") ? p + 8 : NPOS;
                if (q == NPOS || q >= line.size() || line[q] != '%') continue;
                size_t end = q + 1;
                while (end < line.size() && storage_char(line[end])) ++end;
                if (end > q + 1 && at(line, end, "* @dx.op.getMeshPayload.")) {
                    check(PySet_Add(payloads.get(), strings.get(line.substr(q, end - q))));
                    break;
                }
            }
        }
        if (contains(line, "@dx.op.dispatchMesh.")) {
            bool done = false;
            for (size_t p = find(line, "@dx.op.dispatchMesh."); p != NPOS && !done; p = find(line, "@dx.op.dispatchMesh.", p + 1)) {
                size_t q = p + 20, open = find_char(line, '(', q);
                if (open == NPOS || open == q) continue;
                for (size_t k = open + 1; k <= line.size(); ++k) {
                    if (k > open + 1 && line[k - 1] == '\n') break;
                    if (!at(line, k, ", %")) continue;
                    size_t start = k + 3, end = start;
                    while (end < line.size() && storage_char(line[end])) ++end;
                    if (end > start && at(line, end, " addrspace(3)*")) {
                        check(PySet_Add(payloads.get(), strings.get(line.substr(k + 2, end - (k + 2)))));
                        done = true;
                        break;
                    }
                }
            }
        }
    }
    return check(PyTuple_Pack(3, definitions.get(), types.get(), payloads.get()));
}

PyObject *entry_tables(PyObject *lines_object) {
    Texts lines = read_lines(lines_object, [](const Raw &line) {
        return metadata_line(line) || type_line(line) || payload_line(line);
    });
    return entry_tables_of(lines);
}

enum Origin {
    NONE, MATERIAL_HANDLE, INVALID_HANDLE, MATERIAL_REGISTER_ADDRESS, MATERIAL_CBV_INDEX, MESHLET_BYTES,
    MESHLET_REGISTER, RECORD_BYTES, RECORD_REGISTER, RECORD_WORD, SPEEDTREE_EXTENSION_BYTES,
    SPEEDTREE_EXTENSION_REGISTER, SPEEDTREE_PARAMETERS_BYTES, SPEEDTREE_PARAMETERS_REGISTER, SPEEDTREE_WIND_BYTES,
    RENDERER_CBV_INDEX, RENDERER_CBV_DESCRIPTOR, SPLINE_EXTENSION_BYTES, MATERIAL_INDIRECTION_RECORD,
};
enum Storage { HEAP = 0, STRUCTURED = 1, PAGED = 2 };

size_t after(V text, size_t i, const char *literal) {
    return at(text, i, literal) ? i + std::char_traits<char>::length(literal) : NPOS;
}
size_t plain_value(V text, size_t i) {
    if (i >= text.size() || text[i] != '%') return NPOS;
    size_t end = i + 1;
    while (end < text.size() && name_char(text[end])) ++end;
    return end > i + 1 ? end : NPOS;
}
size_t decimals(V text, size_t i) {
    size_t end = i;
    while (end < text.size() && decimal(text[end])) ++end;
    return end != i ? end : NPOS;
}
size_t integer_at(V text, size_t i) {
    size_t end = decimals(text, after(text, i, "-"));
    return end != NPOS ? end : decimals(text, i);
}
size_t operand_at(V text, size_t i) {
    size_t end = plain_value(text, i);
    return end != NPOS ? end : integer_at(text, i);
}
size_t run_before(V text, size_t i, U stop, U also = U(0xFFFFFFFF)) {
    if (i >= text.size()) return NPOS;
    size_t end = i;
    while (end < text.size() && text[end] != stop && text[end] != also) ++end;
    return end != i ? end : NPOS;
}
size_t wrap_flags(V text, size_t i) {
    while (at(text, i, " nuw") || at(text, i, " nsw")) i += 4;
    return i;
}

bool is_integer(V text) { return integer_at(text, 0) == text.size(); }
bool all_decimal(V text) { return decimals(text, 0) == text.size(); }
bool all_digit(V text) {
    for (U c : text) if (!(c < 128 ? ascii_digit(c) : Py_UNICODE_ISDIGIT(c) != 0)) return false;
    return !text.empty();
}

using Ops = std::vector<size_t>;

struct Groups {
    Span item[6];
    void set(int index, size_t start, size_t end) { item[index].start = start; item[index].end = end; }
    bool has(int index) const { return item[index].found(); }
    V of(V text, int index) const { return text.substr(item[index].start, item[index].end - item[index].start); }
};

template <typename Rest> bool search_from(V text, const Ops &ops, const char *head, Groups &groups, Rest rest) {
    size_t length = std::char_traits<char>::length(head);
    for (size_t p : ops) {
        if (!at(text, p, head)) continue;
        groups = Groups();
        if (rest(p + length)) return true;
    }
    return false;
}

void operation_positions(V text, Ops &ops) {
    ops.clear();
    for (size_t p = find(text, "@dx.op."); p != NPOS; p = find(text, "@dx.op.", p + 1)) ops.push_back(p);
}

bool assignment(V stripped, V &result, V &body) {
    size_t name_end = plain_value(stripped, 0), start = name_end;
    while (start < stripped.size() && space(stripped[start])) ++start;
    start = after(stripped, start, "=");
    while (start < stripped.size() && space(stripped[start])) ++start;
    if (start >= stripped.size() || find_char(stripped, '\n', start) != NPOS) return false;
    result = stripped.substr(0, name_end);
    body = stripped.substr(start);
    return true;
}

bool input_search(V text, const Ops &ops, Groups &groups) {
    return search_from(text, ops, "@dx.op.loadInput.i32(i32 4, i32 ", groups, [&](size_t q) {
        size_t a = decimals(text, q), b0 = after(text, a, ", i32 "), b = decimals(text, b0);
        size_t c0 = after(text, b, ", i8 "), c = decimals(text, c0);
        if (after(text, c, ",") == NPOS) return false;
        groups.set(0, q, a); groups.set(1, b0, b); groups.set(2, c0, c);
        return true;
    });
}

bool legacy_search(V text, const Ops &ops, Groups &groups) {
    return search_from(text, ops, "@dx.op.createHandle(i32 57, i8 ", groups, [&](size_t q) {
        size_t a = decimals(text, q), b0 = after(text, a, ", i32 "), b = decimals(text, b0);
        if (after(text, b, ",") == NPOS) return false;
        groups.set(0, q, a); groups.set(1, b0, b);
        return true;
    });
}

bool binding_search(V text, const Ops &ops, Groups &groups) {
    return search_from(text, ops, "@dx.op.createHandleFromBinding(i32 217, %dx.types.ResBind ", groups, [&](size_t q) {
        if (size_t zero = after(text, q, "zeroinitializer"); zero != NPOS) {
            groups.set(0, q, zero);
            return true;
        }
        size_t a0 = after(text, q, "{ i32 "), a = decimals(text, a0), u0 = after(text, a, ", i32 ");
        size_t u = decimals(text, u0);
        if (u == NPOS) u = after(text, u0, "-1");
        size_t s0 = after(text, u, ", i32 "), s = decimals(text, s0), k0 = after(text, s, ", i8 ");
        size_t k = decimals(text, k0);
        if (after(text, k, " }") == NPOS) return false;
        groups.set(1, a0, a); groups.set(2, s0, s); groups.set(3, k0, k);
        return true;
    });
}

bool annotate_search(V text, const Ops &ops, Groups &groups) {
    return search_from(text, ops, "@dx.op.annotateHandle(i32 216, %dx.types.Handle ", groups, [&](size_t q) {
        size_t a = plain_value(text, q), k0 = after(text, a, ", %dx.types.ResourceProperties { i32 ");
        size_t k = decimals(text, k0), s0 = after(text, k, ", i32 "), s = decimals(text, s0);
        if (after(text, s, " }") == NPOS) return false;
        groups.set(0, q, a); groups.set(1, k0, k); groups.set(2, s0, s);
        return true;
    });
}

bool heap_search(V text, const Ops &ops, Groups &groups) {
    return search_from(text, ops, "@dx.op.createHandleFromHeap(i32 218, i32 ", groups, [&](size_t q) {
        size_t a = plain_value(text, q), b = after(text, a, ", i1 false, i1 ");
        if (after(text, b, "false)") == NPOS && after(text, b, "true)") == NPOS) return false;
        groups.set(0, q, a);
        return true;
    });
}

bool add_match(V text, Groups &groups) {
    size_t q = after(text, wrap_flags(text, after(text, 0, "add")), " i32 ");
    size_t comma = run_before(text, q, ','), r = after(text, comma, ", ");
    if (r == NPOS || r >= text.size() || find_char(text, '\n', r) != NPOS) return false;
    groups.set(0, q, comma); groups.set(1, r, text.size());
    return true;
}

bool divide_match(V text, Groups &groups) {
    if (text.empty() || (text[0] != 's' && text[0] != 'u')) return false;
    size_t q = after(text, 1, "div i32 "), comma = run_before(text, q, ',');
    if (after(text, comma, ", 16") != text.size()) return false;
    groups.set(0, q, comma);
    return true;
}

bool shift_match(V text, Groups &groups) {
    size_t head = after(text, 0, "lshr");
    if (head == NPOS) head = after(text, 0, "shl");
    size_t q = after(text, wrap_flags(text, head), " i32 ");
    size_t comma = run_before(text, q, ','), n = after(text, comma, ", ");
    if (decimals(text, n) != text.size()) return false;
    groups.set(0, q, comma); groups.set(1, n, text.size());
    return true;
}

bool select_match(V text, Groups &groups) {
    size_t comma = run_before(text, after(text, 0, "select i1 "), ',');
    size_t a0 = after(text, comma, ", i32 "), a = operand_at(text, a0), b0 = after(text, a, ", i32 ");
    if (operand_at(text, b0) != text.size()) return false;
    groups.set(0, a0, a); groups.set(1, b0, text.size());
    return true;
}

bool wave_min_search(V text, const Ops &ops, Groups &groups) {
    return search_from(text, ops, "@dx.op.waveActiveOp.i32(i32 119, i32 ", groups, [&](size_t q) {
        size_t comma = run_before(text, q, ',');
        if (after(text, comma, ", i8 2, i8 1)") == NPOS) return false;
        groups.set(0, q, comma);
        return true;
    });
}

bool wave_first_search(V text, const Ops &ops, Groups &groups) {
    return search_from(text, ops, "@dx.op.waveReadLaneFirst.i32(i32 118, i32 ", groups, [&](size_t q) {
        size_t a = plain_value(text, q);
        if (after(text, a, ")") == NPOS) return false;
        groups.set(0, q, a);
        return true;
    });
}

bool raw_load_search(V text, const Ops &ops, Groups &groups) {
    return search_from(text, ops, "@dx.op.", groups, [&](size_t q) {
        size_t t = after(text, q, "rawBufferLoad.");
        if (t == NPOS) t = after(text, q, "bufferLoad.");
        size_t o = after(text, t, "i32(i32 ");
        if (o == NPOS) o = after(text, t, "f32(i32 ");
        size_t h = after(text, o, "139");
        if (h == NPOS) h = after(text, o, "68");
        size_t a0 = after(text, h, ", %dx.types.Handle "), a = plain_value(text, a0);
        size_t e0 = after(text, a, ", i32 "), e = run_before(text, e0, ','), f0 = after(text, e, ", i32 ");
        size_t f = run_before(text, f0, ',', ')');
        if (f == NPOS || f == text.size()) return false;
        groups.set(0, t, t + 3); groups.set(1, a0, a); groups.set(2, e0, e); groups.set(3, f0, f);
        if (text[f] == ')') return true;
        size_t m0 = after(text, f, ", i8 "), m = decimals(text, m0), l0 = after(text, m, ", i32 ");
        size_t l = decimals(text, l0);
        if (after(text, l, ")") == NPOS) return false;
        groups.set(4, m0, m); groups.set(5, l0, l);
        return true;
    });
}

bool cb_load_search(V text, const Ops &ops, Groups &groups) {
    return search_from(text, ops, "@dx.op.cbufferLoadLegacy.", groups, [&](size_t q) {
        size_t a0 = after(text, q, "i32(i32 59, %dx.types.Handle ");
        if (a0 == NPOS) a0 = after(text, q, "f32(i32 59, %dx.types.Handle ");
        size_t a = plain_value(text, a0), r0 = after(text, a, ", i32 "), r = run_before(text, r0, ')');
        if (after(text, r, ")") == NPOS) return false;
        groups.set(0, q, q + 3); groups.set(1, a0, a); groups.set(2, r0, r);
        return true;
    });
}

bool extract_match(V text, Groups &groups) {
    size_t q = after(text, 0, "extractvalue %dx.types.ResRet.");
    if (q == NPOS) q = after(text, 0, "extractvalue %dx.types.CBufRet.");
    size_t a0 = after(text, q, "i32 ");
    if (a0 == NPOS) a0 = after(text, q, "f32 ");
    size_t a = plain_value(text, a0), c = after(text, a, ", ");
    if (c == NPOS || c + 1 != text.size() || text[c] < '0' || text[c] > '3') return false;
    groups.set(0, q, q + 3); groups.set(1, a0, a); groups.set(2, c, c + 1);
    return true;
}

PyObject *bound_resource(V body, const Ops &ops, Groups &groups, PyObject *by_id, PyObject *by_binding,
                         PyObject *classes) {
    auto resource_class = [&](PyObject *kind) {
        PyObject *found = PyDict_GetItemWithError(classes, kind);
        if (!found && PyErr_Occurred()) throw Fail();
        return found ? found : constants().empty;
    };
    PyObject *table;
    Ref key;
    if (legacy_search(body, ops, groups)) {
        Ref kind(to_int(groups.of(body, 0))), id(to_int(groups.of(body, 1)));
        key = Ref(check(PyTuple_Pack(2, resource_class(kind.get()), id.get())));
        table = by_id;
    } else if (binding_search(body, ops, groups)) {
        Ref lower, space_number, kind;
        if (groups.has(0)) {
            Ref zero(check(PyLong_FromLong(0)));
            key = Ref(check(PyTuple_Pack(3, resource_class(zero.get()), zero.get(), zero.get())));
        } else {
            lower = Ref(to_int(groups.of(body, 1)));
            space_number = Ref(to_int(groups.of(body, 2)));
            kind = Ref(to_int(groups.of(body, 3)));
            key = Ref(check(PyTuple_Pack(3, resource_class(kind.get()), space_number.get(), lower.get())));
        }
        table = by_binding;
    } else {
        return nullptr;
    }
    PyObject *found = PyDict_GetItemWithError(table, key.get());
    if (!found && PyErr_Occurred()) throw Fail();
    return found;
}

void incoming_values(V text, std::vector<V> &values) {
    values.clear();
    for (size_t p = 0; p < text.size();) {
        size_t a0 = after(text, p, "[ "), a = operand_at(text, a0);
        size_t end = after(text, plain_value(text, after(text, a, ", ")), " ]");
        if (end == NPOS) {
            ++p;
            continue;
        }
        values.push_back(text.substr(a0, a - a0));
        p = end;
    }
}

void plain_values(V text, std::vector<V> &values) {
    for (size_t p = 0; p < text.size();) {
        size_t end = plain_value(text, p);
        if (end == NPOS) {
            ++p;
            continue;
        }
        values.push_back(text.substr(p, end - p));
        p = end;
    }
}

V operand_of(V text, const char *head, bool flags, const char *tail) {
    size_t q = after(text, 0, head);
    size_t a0 = after(text, flags ? wrap_flags(text, q) : q, " i32 "), a = plain_value(text, a0);
    return after(text, a, tail) == text.size() ? text.substr(a0, a - a0) : V();
}

bool integer_operation(V body) {
    for (const char *name : {"add", "sub", "mul", "shl", "lshr", "ashr", "and", "or", "xor", "select", "phi"}) {
        size_t end = after(body, 0, name);
        if (end != NPOS && (end == body.size() || !word(body[end]))) return true;
    }
    return false;
}

long long small(PyObject *number) {
    int overflow = 0;
    long long value = PyLong_AsLongLongAndOverflow(number, &overflow);
    if (value == -1 && PyErr_Occurred()) throw Fail();
    return overflow ? LLONG_MIN : value;
}
long long small(V digits) {
    long long value = 0;
    for (U c : digits) {
        int digit = c < 128 ? static_cast<int>(c - '0') : Py_UNICODE_TODECIMAL(c);
        value = value > (LLONG_MAX - digit) / 10 ? LLONG_MAX : value * 10 + digit;
    }
    return value;
}
PyObject *plus(PyObject *number, long long amount) {
    Ref other(check(PyLong_FromLongLong(amount)));
    return check(PyNumber_Add(number, other.get()));
}
PyObject *times(PyObject *number, long long amount) {
    Ref other(check(PyLong_FromLongLong(amount)));
    return check(PyNumber_Multiply(number, other.get()));
}
PyObject *masked(PyObject *number, long long mask) {
    Ref other(check(PyLong_FromLongLong(mask)));
    return check(PyNumber_And(number, other.get()));
}
bool misaligned(PyObject *number, long long divisor) {
    Ref other(check(PyLong_FromLongLong(divisor)));
    Ref rest(check(PyNumber_Remainder(number, other.get())));
    return small(rest.get()) != 0;
}
bool equal(PyObject *left, PyObject *right) {
    int result = PyObject_RichCompareBool(left, right, Py_EQ);
    check(result);
    return result != 0;
}

struct Fact {
    Ref row;
    PyObject *name;
    bool cbv_table, cbv_element, shared, paged, paged_element, shared_element, redirect, byte_address, root;
    std::vector<PyObject *> record_offsets;
    std::vector<std::pair<PyObject *, bool>> instance_words;
};

class RecordReader {
public:
    RecordReader(PyObject *describe, PyObject *by_id, PyObject *by_binding, PyObject *classes, PyObject *inputs,
                 bool spline_entry, bool speedtree_entry, bool wind_arena, PyObject *candidates)
        : describe_(describe), by_id_(by_id), by_binding_(by_binding), classes_(classes), inputs_(inputs),
          spline_entry_(spline_entry), speedtree_entry_(speedtree_entry), wind_arena_(wind_arena),
          candidates_(candidates), zero_(check(PyLong_FromLong(0))) {}

    PyObject *read(PyObject *lines_object, PyObject *word_type, PyObject *access_type) {
        static PyObject *const word_fields[] = {interned("byte_offset"), interned("value"), interned("scalar_type"),
                                                interned("line_index"), interned("load_line_index")};
        static PyObject *const access_fields[] = {interned("storage"), interned("resource_name"), interned("address"),
                                                  interned("words"), interned("constant_buffer_size")};
        Slots word_slots(word_type, word_fields, 5), access_slots(access_type, access_fields, 5);
        Texts lines = read_lines(lines_object, [](const Raw &line) {
            Py_ssize_t start = line.skip_space();
            return start < line.size() && line[start] == '%';
        });
        Groups groups;
        std::vector<V> values;
        for (size_t row = 0; row < lines.size(); ++row) {
            V result, body;
            if (!assignment(strip(lines[row]), result, body)) continue;
            size_t line = lines.origin(row);
            if (candidates_ != Py_None && (starts(body, "add ") || contains(body, "Handle"))) {
                Ref text(make(body));
                Ref pair(check(PyTuple_Pack(2, strings_.get(result), text.get())));
                check(PyList_Append(candidates_, pair.get()));
            }
            Ops &ops = ops_;
            operation_positions(body, ops);
            if (integer_operation(body)) integers_[result] = body;
            else if (ops.empty() && !starts(body, "extractvalue ") && !starts(body, "sdiv ") && !starts(body, "udiv ")) continue;
            if (starts(body, "lshr ") || starts(body, "and ") || starts(body, "sub ") || starts(body, "shl "))
                expressions_[result] = strip(body.substr(0, find_char(body, ';')));
            PyObject *resource = nullptr;
            if (!ops.empty() && input_search(body, ops, groups)) {
                Ref signature(to_int(groups.of(body, 0))), place(to_int(groups.of(body, 1)));
                Ref location(check(PyTuple_Pack(2, signature.get(), place.get())));
                int known = PySet_Contains(inputs_, location.get());
                check(known);
                if (known && small(groups.of(body, 2)) == 0) set(result, MATERIAL_HANDLE);
            } else if (!ops.empty() && (resource = bound_resource(body, ops, groups, by_id_, by_binding_, classes_))) {
                handles_[result] = resource;
            } else if (!ops.empty() && annotate_search(body, ops, groups)) {
                V base = groups.of(body, 0);
                if (auto handle = handles_.find(base); handle != handles_.end()) {
                    PyObject *bound = handle->second;
                    handles_[result] = bound;
                }
                if (auto heap = heaps_.find(base); heap != heaps_.end() && small(groups.of(body, 1)) == 13) {
                    V descriptor = heap->second;
                    Ref size(to_int(groups.of(body, 2)));
                    Ref size_text(check(PyObject_Str(size.get())));
                    S digits;
                    append(digits, size_text.get());
                    dynamic_[result] = {descriptor, std::move(size), arena_.keep(digits)};
                }
            } else if (!ops.empty() && heap_search(body, ops, groups)) {
                V value = groups.of(body, 0);
                if (descriptors_.count(value)) heaps_[result] = value;
            } else if (starts(body, "add ") && add_match(body, groups)) {
                add(result, groups.of(body, 0), groups.of(body, 1));
            } else if ((starts(body, "sdiv ") || starts(body, "udiv ")) && divide_match(body, groups)) {
                if (origin_of(groups.of(body, 0)) == MESHLET_BYTES) set(result, MESHLET_REGISTER);
            } else if ((starts(body, "lshr ") || starts(body, "shl ")) && shift_match(body, groups)) {
                if (starts(body, "lshr")) shift(result, groups.of(body, 0), small(groups.of(body, 1)));
            } else if (starts(body, "select ") && select_match(body, groups)) {
                values.assign({groups.of(body, 0), groups.of(body, 1)});
                if (Origin kind = merged_origin(values)) set(result, kind);
            } else if (!ops.empty() && wave_min_search(body, ops, groups)) {
                V value = groups.of(body, 0);
                if (same(value, "-1")) set(result, INVALID_HANDLE);
                else if (Origin kind = origin_of(value)) set(result, kind);
            } else if (!ops.empty() && wave_first_search(body, ops, groups)) {
                if (Origin kind = origin_of(groups.of(body, 0))) set(result, kind);
            } else if (starts(body, "phi i32 ")) {
                incoming_values(body, values);
                if (!values.empty())
                    if (Origin kind = merged_origin(values)) set(result, kind);
            } else if (!ops.empty() && raw_load_search(body, ops, groups)) {
                raw_load(result, body, groups, line);
            } else if (!ops.empty() && cb_load_search(body, ops, groups)) {
                cb_load(result, body, groups, line);
            } else if (starts(body, "extractvalue ") && extract_match(body, groups)) {
                extract(result, body, groups, line, word_slots);
            }
        }
        return finish(access_slots);
    }

private:
    struct Key { Storage storage; PyObject *name; V base; Ref size; };
    struct Load { size_t key; Ref offset; size_t line; };
    struct Dynamic { V descriptor; Ref size; V size_text; };
    struct Reads { std::vector<Ref> words; std::vector<V> values; };

    Origin origin_of(V value) const {
        auto found = origins_.find(value);
        return found == origins_.end() ? NONE : found->second;
    }
    void set(V value, Origin kind) { origins_[value] = kind; }

    std::pair<V, PyObject *> index(V value) const {
        auto found = affine_.find(value);
        return found == affine_.end() ? std::pair<V, PyObject *>(value, zero_.get())
                                      : std::pair<V, PyObject *>(found->second.first, found->second.second.get());
    }
    V expression(V value) const {
        auto found = expressions_.find(value);
        return found == expressions_.end() ? V() : found->second;
    }

    Origin merged_origin(const std::vector<V> &values) const {
        Origin kinds[2] = {NONE, NONE};
        size_t count = 0;
        for (V value : values) {
            Origin kind = same(value, "-1") ? INVALID_HANDLE : origin_of(value);
            if (count && (kind == kinds[0] || (count == 2 && kind == kinds[1]))) continue;
            if (count == 2) return NONE;
            kinds[count++] = kind;
        }
        auto pair = [&](Origin a, Origin b) {
            return count == 2 && ((kinds[0] == a && kinds[1] == b) || (kinds[0] == b && kinds[1] == a));
        };
        if (pair(MATERIAL_HANDLE, INVALID_HANDLE)) return MATERIAL_HANDLE;
        if (count == 1) return kinds[0];
        if (pair(RECORD_BYTES, RECORD_WORD)) return RECORD_BYTES;
        return NONE;
    }

    const Fact &fact_of(PyObject *resource) {
        auto found = facts_.find(resource);
        if (found != facts_.end()) return found->second;
        Fact fact;
        fact.row = Ref(check(PyObject_CallOneArg(describe_, resource)));
        PyObject *row = fact.row.get();
        if (!PyTuple_Check(row) || PyTuple_GET_SIZE(row) != 12) {
            PyErr_SetString(PyExc_TypeError, "record_accesses' describe gives 12 facts");
            throw Fail();
        }
        auto flag = [&](Py_ssize_t index) {
            int value = PyObject_IsTrue(PyTuple_GET_ITEM(row, index));
            check(value);
            return value != 0;
        };
        fact.name = PyTuple_GET_ITEM(row, 0);
        fact.cbv_table = flag(1); fact.cbv_element = flag(2); fact.shared = flag(3); fact.paged = flag(4);
        fact.paged_element = flag(5); fact.shared_element = flag(6); fact.redirect = flag(7);
        fact.byte_address = flag(8); fact.root = flag(9);
        PyObject *records = PyTuple_GET_ITEM(row, 10), *instances = PyTuple_GET_ITEM(row, 11);
        if (!PyTuple_Check(records) || !PyTuple_Check(instances)) {
            PyErr_SetString(PyExc_TypeError, "record_accesses' describe gives tuples of words");
            throw Fail();
        }
        for (Py_ssize_t index = 0; index < PyTuple_GET_SIZE(records); ++index)
            fact.record_offsets.push_back(PyTuple_GET_ITEM(records, index));
        for (Py_ssize_t index = 0; index < PyTuple_GET_SIZE(instances); ++index) {
            PyObject *pair = PyTuple_GET_ITEM(instances, index);
            if (!PyTuple_Check(pair) || PyTuple_GET_SIZE(pair) != 2) {
                PyErr_SetString(PyExc_TypeError, "record_accesses' instance words are (offset, material) pairs");
                throw Fail();
            }
            int material = PyObject_IsTrue(PyTuple_GET_ITEM(pair, 1));
            check(material);
            fact.instance_words.emplace_back(PyTuple_GET_ITEM(pair, 0), material != 0);
        }
        return facts_.emplace(resource, std::move(fact)).first->second;
    }

    void add(V result, V left, V right) {
        if (is_integer(left)) std::swap(left, right);
        if (is_integer(right)) {
            auto [base, offset] = index(left);
            Ref number(to_int(right));
            Ref sum(check(PyNumber_Add(offset, number.get())));
            affine_[result] = {base, std::move(sum)};
            if (Origin kind = origin_of(left)) set(result, kind);
            return;
        }
        Origin a = origin_of(left), b = origin_of(right);
        for (Origin kind : {MESHLET_BYTES, MESHLET_REGISTER, RECORD_BYTES, RECORD_REGISTER})
            if (kind == a || kind == b) {
                set(result, kind);
                return;
            }
    }

    void shift(V result, V source, long long amount) {
        Origin kind = origin_of(source);
        if (kind == MATERIAL_HANDLE && (amount == 4 || amount == 8)) {
            set(result, amount == 4 ? MATERIAL_REGISTER_ADDRESS : MATERIAL_CBV_INDEX);
        } else if (kind == MESHLET_BYTES && amount == 4) {
            set(result, MESHLET_REGISTER);
        } else if (kind == RECORD_BYTES && amount == 4) {
            set(result, RECORD_REGISTER);
        } else if (kind == SPEEDTREE_EXTENSION_BYTES && amount == 4) {
            set(result, SPEEDTREE_EXTENSION_REGISTER);
        } else if (kind == SPEEDTREE_PARAMETERS_BYTES && amount == 4) {
            set(result, SPEEDTREE_PARAMETERS_REGISTER);
            Ref offset(Py_NewRef(index(source).second));
            parameter_offsets_[result] = std::move(offset);
        } else if (kind == SPEEDTREE_WIND_BYTES && amount == 8) {
            set(result, RENDERER_CBV_INDEX);
        }
    }

    size_t key_of(Storage storage, PyObject *name, V base, PyObject *size, V size_text) {
        key_text_.assign(1, static_cast<U>(storage));
        key_text_ += static_cast<U>(base.size());
        key_text_ += base;
        key_text_ += size_text;
        auto found = key_index_.find(key_text_);
        if (found != key_index_.end()) return found->second;
        keys_.push_back({storage, name, arena_.keep(base), Ref(Py_NewRef(size))});
        key_index_.emplace(arena_.keep(key_text_), keys_.size() - 1);
        return keys_.size() - 1;
    }

    void raw_load(V result, V body, const Groups &groups, size_t line) {
        auto handle = handles_.find(groups.of(body, 1));
        if (handle == handles_.end()) return;
        PyObject *resource = handle->second;
        const Fact &fact = fact_of(resource);
        V element = groups.of(body, 2), element_offset = groups.of(body, 3);
        bool single = !groups.has(4) || same(groups.of(body, 4), "1");
        if (all_decimal(element_offset))
            named_loads_[result] = {resource, Ref(to_int(element_offset))};
        else if (fact.redirect && fact.byte_address && same(element_offset, "undef") && single)
            named_loads_[result] = {resource, Ref(Py_NewRef(zero_.get()))};
        if (fact.cbv_table) {
            if (!fact.cbv_element) raise_graph("Material CBV table has an unexpected element type.");
            if (!same(element_offset, "0") || !single)
                raise_graph("Material CBV selection has an unsupported table element access.");
            table_loads_[result] = element;
            table_indices_.push_back(element);
        } else if (fact.shared) {
            if (fact.paged) {
                if (!fact.paged_element) raise_graph("Paged shared buffer has an unexpected element type.");
                auto [base, offset] = paged_address(element, element_offset);
                loads_[result] = {key_of(PAGED, fact.name, base, zero_.get(), U"0"), std::move(offset), line};
                return;
            }
            if (!fact.shared_element) raise_graph("Material shared buffer has an unexpected element type.");
            if (!all_decimal(element_offset)) raise_graph("Material load has a nonconstant element offset.");
            auto [base, register_offset] = index(element);
            Ref scaled(times(register_offset, 16)), number(to_int(element_offset));
            Ref byte_offset(check(PyNumber_Add(scaled.get(), number.get())));
            if (auto parameter = parameter_offsets_.find(base); parameter != parameter_offsets_.end())
                byte_offset = Ref(check(PyNumber_Add(byte_offset.get(), parameter->second.get())));
            if (small(byte_offset.get()) < 0 || misaligned(byte_offset.get(), 4))
                raise_graph("Material load has an invalid byte offset.");
            loads_[result] = {key_of(STRUCTURED, fact.name, base, zero_.get(), U"0"), std::move(byte_offset), line};
        }
    }

    std::pair<V, Ref> paged_address(V element, V element_offset) {
        auto [page_value, page_delta] = index(element);
        V address = operand_of(expression(page_value), "lshr", true, ", 8");
        if (address.empty()) raise_graph("Paged shared-buffer load has no proven page address.");
        auto [base, byte_offset] = index(address);
        if (all_decimal(element_offset)) {
            if (origin_of(base) != MATERIAL_HANDLE || misaligned(byte_offset, 256))
                raise_graph("Constant page offset requires a page-aligned material allocation.");
            Ref pages(times(page_delta, 256)), number(to_int(element_offset));
            Ref sum(check(PyNumber_Add(byte_offset, pages.get())));
            return {base, Ref(check(PyNumber_Add(sum.get(), number.get())))};
        }
        V offset_text = expression(element_offset);
        V lanes = operand_of(offset_text, "and", false, ", 255");
        bool whole = !lanes.empty();
        if (!whole) lanes = operand_of(offset_text, "and", false, ", 240");
        if (!lanes.empty()) {
            auto [lanes_base, lanes_offset] = index(lanes);
            Ref apart(check(PyNumber_Subtract(lanes_offset, byte_offset)));
            if (lanes_base == base && !misaligned(apart.get(), 256) && small(page_delta) == 0)
                return {base, whole ? Ref(Py_NewRef(byte_offset)) : Ref(masked(byte_offset, -16))};
        }
        V aligned = operand_of(offset_text, "and", false, ", -16");
        V remainder = expression(aligned);
        size_t a0 = after(remainder, 0, "sub i32 "), a = plain_value(remainder, a0), b0 = after(remainder, a, ", ");
        if (!aligned.empty() && plain_value(remainder, b0) == remainder.size()) {
            V page_bytes = operand_of(expression(remainder.substr(b0)), "shl", true, ", 8");
            if (!page_bytes.empty() && remainder.substr(a0, a - a0) == address && page_bytes == page_value
                && small(page_delta) == 0)
                return {base, Ref(masked(byte_offset, -16))};
        }
        raise_graph("Paged shared-buffer load has no proven page/remainder address.");
    }

    void cb_load(V result, V body, const Groups &groups, size_t line) {
        V handle = groups.of(body, 1), register_text = groups.of(body, 2);
        if (same(groups.of(body, 0), "i32") && same(register_text, "0"))
            if (auto bound = handles_.find(handle); bound != handles_.end() && fact_of(bound->second).root)
                root_handle_loads_.insert(result);
        auto dynamic = dynamic_.find(handle);
        if (dynamic == dynamic_.end()) return;
        const Dynamic &table = dynamic->second;
        if (origin_of(table.descriptor) == RENDERER_CBV_DESCRIPTOR) return;
        if (!all_digit(register_text)) raise_graph("Material constant-buffer indexing is dynamic.");
        static PyObject *const table_name = interned("BindlessConstantBufferViewTbl");
        Ref number(to_int(register_text));
        size_t key = key_of(HEAP, table_name, table.descriptor, table.size.get(), table.size_text);
        loads_[result] = {key, Ref(times(number.get(), 16)), line};
    }

    void extract(V result, V body, const Groups &groups, size_t line, const Slots &word_slots) {
        V scalar = groups.of(body, 0), loaded = groups.of(body, 1);
        long long component = groups.of(body, 2)[0] - '0';
        bool i32 = same(scalar, "i32");
        if (i32 && component == 0 && root_handle_loads_.count(loaded)) set(result, MATERIAL_HANDLE);
        if (auto named = named_loads_.find(loaded); named != named_loads_.end()) {
            const Fact &fact = fact_of(named->second.first);
            if (fact.redirect && component == 0) set(result, MATERIAL_HANDLE);
            if (!fact.record_offsets.empty() || !fact.instance_words.empty()) {
                Ref place(plus(named->second.second.get(), component * 4));
                for (PyObject *offset : fact.record_offsets)
                    if (equal(offset, place.get())) {
                        set(result, RECORD_BYTES);
                        break;
                    }
                for (const auto &[offset, material] : fact.instance_words) {
                    if (!equal(offset, place.get())) continue;
                    if (material) set(result, MESHLET_BYTES);
                    else if (speedtree_entry_ && i32) set(result, SPEEDTREE_EXTENSION_BYTES);
                    else if (spline_entry_ && i32) set(result, SPLINE_EXTENSION_BYTES);
                }
            }
        }
        if (auto table = table_loads_.find(loaded); table != table_loads_.end() && component == 0) {
            V selected = table->second;
            descriptors_.insert(result);
            if (origin_of(selected) == RENDERER_CBV_INDEX) set(result, RENDERER_CBV_DESCRIPTOR);
        }
        auto found = loads_.find(loaded);
        if (found == loads_.end()) return;
        size_t key = found->second.key, load_line = found->second.line;
        Ref offset(plus(found->second.offset.get(), component * 4));
        long long at_offset = small(offset.get());
        V base = keys_[key].base;
        Origin kind = origin_of(base);
        if (kind == RECORD_REGISTER || kind == RECORD_BYTES) set(result, RECORD_WORD);
        kind = origin_of(base);
        if ((kind == SPEEDTREE_EXTENSION_REGISTER || kind == SPEEDTREE_EXTENSION_BYTES) && at_offset == 0)
            set(result, SPEEDTREE_PARAMETERS_BYTES);
        kind = origin_of(base);
        if ((kind == SPEEDTREE_PARAMETERS_BYTES || kind == SPEEDTREE_PARAMETERS_REGISTER)
            && (at_offset == 16 || at_offset == 20) && wind_arena_)
            set(result, SPEEDTREE_WIND_BYTES);
        kind = origin_of(base);
        if (kind == SPLINE_EXTENSION_BYTES && (at_offset == 0 || at_offset == 4 || at_offset == 8))
            set(result, RECORD_BYTES);
        kind = origin_of(base);
        if ((kind == MESHLET_REGISTER || kind == MESHLET_BYTES) && (at_offset == 0 || at_offset == 8))
            set(result, MATERIAL_HANDLE);
        PyObject *size = keys_[key].size.get();
        if (small(size) != 0) {
            Ref end(plus(offset.get(), 4));
            int beyond = PyObject_RichCompareBool(end.get(), size, Py_GT);
            check(beyond);
            if (beyond) raise_graph("Material word exceeds its declared constant buffer.");
        }
        Ref line_number(check(PyLong_FromSize_t(line))), load_line_number(check(PyLong_FromSize_t(load_line)));
        PyObject *fields[] = {offset.get(), strings_.get(result), strings_.get(scalar), line_number.get(),
                              load_line_number.get()};
        if (reads_.size() <= key) reads_.resize(key + 1);
        Reads &reads = reads_[key];
        if (reads.words.empty()) key_order_.push_back(key);
        reads.words.emplace_back(word_slots.make(fields));
        reads.values.push_back(result);
    }

    PyObject *finish(const Slots &access_slots) {
        VMap<size_t> records;
        for (size_t key : key_order_)
            for (V value : reads_[key].values) records[value] = key;
        VSet visited;
        std::vector<V> pending(table_indices_);
        while (!pending.empty()) {
            V value = pending.back();
            pending.pop_back();
            if (!visited.insert(value).second) continue;
            if (auto record = records.find(value); record != records.end()) {
                const Key &key = keys_[record->second];
                Origin kind = origin_of(key.base);
                if (key.storage != HEAP && kind != MATERIAL_REGISTER_ADDRESS && kind != MATERIAL_HANDLE)
                    set(key.base, MATERIAL_INDIRECTION_RECORD);
            } else if (auto body = integers_.find(value); body != integers_.end()) {
                plain_values(body->second, pending);
            }
        }
        S unresolved;
        for (size_t index : key_order_) {
            const Key &key = keys_[index];
            if (key.storage == HEAP) continue;
            switch (origin_of(key.base)) {
            case MATERIAL_REGISTER_ADDRESS: case MESHLET_REGISTER: case MATERIAL_INDIRECTION_RECORD:
            case RECORD_REGISTER: case SPEEDTREE_EXTENSION_REGISTER: case MATERIAL_HANDLE: case MESHLET_BYTES:
            case RECORD_BYTES: case SPEEDTREE_EXTENSION_BYTES: case SPLINE_EXTENSION_BYTES:
            case SPEEDTREE_PARAMETERS_BYTES: case SPEEDTREE_PARAMETERS_REGISTER:
                continue;
            default:
                unresolved += unresolved.empty() ? U"Shared-buffer access has no proven material/renderer address origin: "
                                                 : U", ";
                unresolved += key.base;
            }
        }
        if (!unresolved.empty()) {
            Ref message(make(unresolved));
            PyErr_SetObject(graph_error(), message.get());
            throw Fail();
        }
        static PyObject *const storage_names[] = {interned("descriptor-heap"), interned("structured-buffer"),
                                                  interned("paged-structured-buffer")};
        Ref result(check(PyList_New(0)));
        for (size_t index : key_order_) {
            const Key &key = keys_[index];
            Origin kind = origin_of(key.base);
            if (key.storage != HEAP && kind != MATERIAL_REGISTER_ADDRESS && !(key.storage == PAGED && kind == MATERIAL_HANDLE))
                continue;
            const std::vector<Ref> &words = reads_[index].words;
            Ref tuple(check(PyTuple_New(static_cast<Py_ssize_t>(words.size()))));
            for (size_t k = 0; k < words.size(); ++k)
                PyTuple_SET_ITEM(tuple.get(), static_cast<Py_ssize_t>(k), Py_NewRef(words[k].get()));
            PyObject *fields[] = {storage_names[key.storage], key.name, strings_.get(key.base), tuple.get(), key.size.get()};
            Ref access(access_slots.make(fields));
            check(PyList_Append(result.get(), access.get()));
        }
        return check(PyList_AsTuple(result.get()));
    }

    PyObject *describe_, *by_id_, *by_binding_, *classes_, *inputs_;
    bool spline_entry_, speedtree_entry_, wind_arena_;
    PyObject *candidates_;
    Ref zero_;
    Arena arena_;
    Strings strings_;
    std::unordered_map<PyObject *, Fact> facts_;
    VMap<PyObject *> handles_;
    VMap<V> table_loads_, heaps_, expressions_, integers_;
    std::vector<V> table_indices_;
    VSet descriptors_, root_handle_loads_;
    VMap<Dynamic> dynamic_;
    VMap<std::pair<V, Ref>> affine_;
    VMap<Ref> parameter_offsets_;
    VMap<Load> loads_;
    std::vector<Key> keys_;
    VMap<size_t> key_index_;
    S key_text_;
    std::vector<Reads> reads_;
    std::vector<size_t> key_order_;
    VMap<Origin> origins_;
    VMap<std::pair<PyObject *, Ref>> named_loads_;
    Ops ops_;
};

size_t word_value(V text, size_t i) {
    if (i >= text.size() || text[i] != '%') return NPOS;
    size_t end = i + 1;
    while (end < text.size() && (word(text[end]) || text[end] == '.')) ++end;
    return end > i + 1 ? end : NPOS;
}
size_t word_run(V text, size_t i) {
    if (i >= text.size()) return NPOS;
    size_t end = i;
    while (end < text.size() && word(text[end])) ++end;
    return end != i ? end : NPOS;
}

bool assigned_line(const Raw &line) {
    Py_ssize_t start = line.skip_space();
    return start < line.size() && line[start] == '%';
}

bool named_line(V line, V &result, V &body) {
    size_t start = 0;
    while (start < line.size() && space(line[start])) ++start;
    size_t end = word_value(line, start), rest = after(line, end, " = ");
    if (rest == NPOS) return false;
    size_t stop = find_char(line, '\n', rest);
    result = line.substr(start, end - start);
    body = line.substr(rest, stop == NPOS ? NPOS : stop - rest);
    return true;
}

bool attribute_search(V text, const Ops &ops, Groups &groups) {
    return search_from(text, ops, "@dx.op.loadInput.", groups, [&](size_t q) {
        size_t a0 = after(text, word_run(text, q), "(i32 4, i32 "), a = decimals(text, a0);
        size_t b0 = after(text, a, ", i32 "), b = decimals(text, b0), c0 = after(text, b, ", i8 "), c = decimals(text, c0);
        if (c == NPOS) return false;
        groups.set(0, a0, a); groups.set(1, b0, b); groups.set(2, c0, c);
        return true;
    });
}

bool heap_kind_search(V text, const Ops &ops, Groups &groups) {
    return search_from(text, ops, "@dx.op.createHandleFromHeap(i32 218, ", groups, [&](size_t q) {
        size_t f = after(text, run_before(text, q, ','), ", i1 "), e = after(text, f, "false");
        if (e == NPOS) e = after(text, f, "true");
        if (e == NPOS) return false;
        groups.set(0, f, e);
        return true;
    });
}

bool annotated_search(V text, const Ops &ops, Groups &groups) {
    return search_from(text, ops, "@dx.op.annotateHandle(i32 216, %dx.types.Handle ", groups, [&](size_t q) {
        size_t a = word_value(text, q);
        if (a == NPOS) return false;
        groups.set(0, q, a);
        return true;
    });
}

bool row_search(V text, const Ops &ops, Groups &groups) {
    return search_from(text, ops, "@dx.op.cbufferLoadLegacy.", groups, [&](size_t q) {
        size_t a0 = after(text, word_run(text, q), "(i32 59, %dx.types.Handle "), a = word_value(text, a0);
        size_t r0 = after(text, a, ", i32 "), r = decimals(text, r0);
        if (after(text, r, ")") == NPOS) return false;
        groups.set(0, a0, a); groups.set(1, r0, r);
        return true;
    });
}

bool lane_search(V text, Groups &groups) {
    const char *head = "extractvalue %dx.types.CBufRet.";
    size_t length = std::char_traits<char>::length(head);
    for (size_t p = find(text, head); p != NPOS; p = find(text, head, p + 1)) {
        size_t a0 = after(text, word_run(text, p + length), " "), a = word_value(text, a0);
        size_t l0 = after(text, a, ", "), l = decimals(text, l0);
        if (l == NPOS) continue;
        groups = Groups();
        groups.set(0, a0, a); groups.set(1, l0, l);
        return true;
    }
    return false;
}

bool texture_index_search(V text, Groups &groups) {
    for (size_t p = find(text, ", i32 "); p != NPOS; p = find(text, ", i32 ", p + 1)) {
        size_t a = plain_value(text, p + 6), b = after(text, a, ", i1 ");
        if (after(text, b, "true)") == NPOS && after(text, b, "false)") == NPOS) continue;
        groups = Groups();
        groups.set(0, p + 6, a);
        return true;
    }
    return false;
}

void word_values(V text, std::vector<V> &values) {
    values.clear();
    for (size_t p = 0; p < text.size();) {
        size_t end = word_value(text, p);
        if (end == NPOS) {
            ++p;
            continue;
        }
        values.push_back(text.substr(p, end - p));
        p = end;
    }
}

PyObject *as_dict(const Ordered<Ref> &items, Strings &strings) {
    Ref result(check(PyDict_New()));
    for (const auto &[key, value] : items.items) check(PyDict_SetItem(result.get(), strings.get(key), value.get()));
    return result.release();
}

PyObject *value_names(PyObject *lines_object, PyObject *inputs, PyObject *by_id, PyObject *by_binding,
                      PyObject *classes, PyObject *sampler_heap, PyObject *resource_heap) {
    static PyObject *const attribute = interned("attribute"), *const data = interned("data");
    Texts lines = read_lines(lines_object, assigned_line);
    Strings strings;
    Ordered<std::pair<bool, Ref>> names;
    VMap<PyObject *> handles;
    Ops ops;
    Groups groups;
    std::vector<V> values;
    for (size_t row = 0; row < lines.size(); ++row) {
        V result, body;
        if (!named_line(lines[row], result, body)) continue;
        operation_positions(body, ops);
        PyObject *resource = nullptr;
        if (!ops.empty() && attribute_search(body, ops, groups)) {
            Ref signature(to_int(groups.of(body, 0))), place(to_int(groups.of(body, 1)));
            Ref key(check(PyTuple_Pack(2, signature.get(), place.get())));
            PyObject *name = PyDict_GetItemWithError(inputs, key.get());
            if (!name && PyErr_Occurred()) throw Fail();
            if (name) {
                Ref component(to_int(groups.of(body, 2)));
                names.at(result) = {false, Ref(check(PyTuple_Pack(3, attribute, name, component.get())))};
            }
            continue;
        }
        if (!ops.empty() && (resource = bound_resource(body, ops, groups, by_id, by_binding, classes))) {
            handles[result] = resource;
            continue;
        }
        if (!ops.empty() && heap_kind_search(body, ops, groups)) {
            handles[result] = same(groups.of(body, 0), "true") ? sampler_heap : resource_heap;
            continue;
        }
        if (!ops.empty() && annotated_search(body, ops, groups)) {
            if (auto found = handles.find(groups.of(body, 0)); found != handles.end()) {
                PyObject *name = found->second;
                handles[result] = name;
            }
            continue;
        }
        if (starts(body, "call")) {
            word_values(body, values);
            PyObject *handle = nullptr;
            for (V value : values)
                if (auto found = handles.find(value); found != handles.end()) {
                    handle = found->second;
                    break;
                }
            if (handle) {
                names.at(result) = {true, Ref(check(PyTuple_Pack(2, data, handle)))};
                continue;
            }
        }
        if (starts(body, "extractvalue")) {
            word_values(body, values);
            for (V value : values) {
                const auto *found = names.find(value);
                if (!found || !found->first) continue;
                Ref source(Py_NewRef(found->second.get()));
                names.at(result) = {true, std::move(source)};
                break;
            }
        }
    }
    Ref result(check(PyDict_New()));
    for (const auto &[key, value] : names.items) check(PyDict_SetItem(result.get(), strings.get(key), value.second.get()));
    return result.release();
}

PyObject *constant_words(PyObject *lines_object) {
    Texts lines = read_lines(lines_object, assigned_line);
    Strings strings;
    VMap<std::pair<V, Ref>> rows;
    Ordered<Ref> words;
    Ops ops;
    Groups groups;
    for (size_t row = 0; row < lines.size(); ++row) {
        V result, body;
        if (!named_line(lines[row], result, body)) continue;
        operation_positions(body, ops);
        if (!ops.empty() && row_search(body, ops, groups)) {
            rows[result] = {groups.of(body, 0), Ref(to_int(groups.of(body, 1)))};
        } else if (lane_search(body, groups)) {
            auto found = rows.find(groups.of(body, 0));
            if (found == rows.end()) continue;
            Ref lane(to_int(groups.of(body, 1)));
            Ref word(check(PyTuple_Pack(3, strings.get(found->second.first), found->second.second.get(), lane.get())));
            words.at(result) = std::move(word);
        }
    }
    return as_dict(words, strings);
}

PyObject *texture_handles(PyObject *lines_object, PyObject *sources_object, PyObject *by_id, PyObject *by_binding,
                          PyObject *classes, PyObject *describe) {
    struct Fact { Ref row; bool wanted, single; PyObject *name; };
    Texts lines = read_lines(lines_object, [](const Raw &line) { return line.contains("add ") || line.contains("Handle"); });
    Strings strings;
    Arena kept;
    VMap<PyObject *> sources;
    {
        Py_ssize_t position = 0;
        PyObject *key, *value;
        S text;
        while (PyDict_Next(sources_object, &position, &key, &value)) {
            text.clear();
            append(text, key);
            sources[kept.keep(text)] = value;
        }
    }
    std::unordered_map<PyObject *, Fact> facts;
    auto fact_of = [&](PyObject *resource) -> const Fact & {
        auto found = facts.find(resource);
        if (found != facts.end()) return found->second;
        Fact fact;
        fact.row = Ref(check(PyObject_CallOneArg(describe, resource)));
        if (!PyTuple_Check(fact.row.get()) || PyTuple_GET_SIZE(fact.row.get()) != 3) {
            PyErr_SetString(PyExc_TypeError, "texture_handles' describe gives (wanted, one resource, name)");
            throw Fail();
        }
        int wanted = PyObject_IsTrue(PyTuple_GET_ITEM(fact.row.get(), 0));
        check(wanted);
        int single = PyObject_IsTrue(PyTuple_GET_ITEM(fact.row.get(), 1));
        check(single);
        fact.wanted = wanted != 0;
        fact.single = single != 0;
        fact.name = PyTuple_GET_ITEM(fact.row.get(), 2);
        return facts.emplace(resource, std::move(fact)).first->second;
    };
    auto offset_name = [](PyObject *offset) { return Ref(check(PyUnicode_FromFormat("@%S", offset))); };
    Ordered<Ref> result;
    Ops ops;
    Groups groups;
    for (size_t row = 0; row < lines.size(); ++row) {
        V stripped = strip(lines[row]), value, body;
        if (!starts(stripped, "%") || !assignment(stripped, value, body)) continue;
        operation_positions(body, ops);
        if (starts(body, "add ") && add_match(body, groups)) {
            V left = groups.of(body, 0), right = groups.of(body, 1);
            bool zero_right = same(right, "0"), zero_left = same(left, "0");
            if (!zero_right && !zero_left) continue;
            auto found = sources.find(zero_right ? left : right);
            if (found != sources.end()) {
                PyObject *offset = found->second;
                sources[kept.keep(value)] = offset;
            }
        } else if (!ops.empty() && heap_search(body, ops, groups)) {
            auto found = sources.find(groups.of(body, 0));
            if (found != sources.end()) result.at(value) = offset_name(found->second);
        } else if (!ops.empty() && annotate_search(body, ops, groups)) {
            if (Ref *known = result.find(groups.of(body, 0))) {
                Ref copy(Py_NewRef(known->get()));
                result.at(value) = std::move(copy);
            }
        } else {
            PyObject *resource = ops.empty() ? nullptr : bound_resource(body, ops, groups, by_id, by_binding, classes);
            if (!resource) continue;
            const Fact &fact = fact_of(resource);
            if (!fact.wanted) continue;
            if (texture_index_search(body, groups)) {
                auto found = sources.find(groups.of(body, 0));
                if (found != sources.end()) {
                    result.at(value) = offset_name(found->second);
                    continue;
                }
            }
            if (fact.single) result.at(value) = Ref(Py_NewRef(fact.name));
        }
    }
    return as_dict(result, strings);
}

bool line_break(U c) {
    if (c >= 0x20) return c == 0x85 || c == 0x2028 || c == 0x2029;
    return c == '\n' || c == '\r' || c == 0x0B || c == 0x0C || c == 0x1C || c == 0x1D || c == 0x1E;
}
void split_rows(V text, std::vector<V> &rows) {
    rows.clear();
    size_t start = 0;
    for (size_t index = 0; index < text.size(); ++index) {
        U c = text[index];
        if (!line_break(c)) continue;
        rows.push_back(text.substr(start, index - start));
        if (c == '\r' && index + 1 < text.size() && text[index + 1] == '\n') ++index;
        start = index + 1;
    }
    if (start < text.size()) rows.push_back(text.substr(start));
}

struct Rows {
    Buffer text;
    std::vector<V> rows;
    explicit Rows(PyObject *object) {
        append(text, object);
        split_rows(V(text.data(), text.size()), rows);
    }
};

PyObject *joined(const std::vector<V> &rows) {
    size_t size = rows.size();
    U widest = '\n';
    for (V row : rows) {
        size += row.size();
        for (U c : row) widest = std::max(widest, c);
    }
    PyObject *result = check(PyUnicode_New(static_cast<Py_ssize_t>(size), widest < 128 ? 127 : widest));
    int kind = PyUnicode_KIND(result);
    void *data = PyUnicode_DATA(result);
    Py_ssize_t position = 0;
    for (V row : rows) {
        if (kind == PyUnicode_1BYTE_KIND) {
            Py_UCS1 *out = static_cast<Py_UCS1 *>(data) + position;
            for (size_t index = 0; index < row.size(); ++index) out[index] = static_cast<Py_UCS1>(row[index]);
            position += static_cast<Py_ssize_t>(row.size());
        } else {
            for (U c : row) PyUnicode_WRITE(kind, data, position++, c);
        }
        PyUnicode_WRITE(kind, data, position++, '\n');
    }
    return result;
}

PyObject *row_list(const std::vector<V> &rows) {
    Ref result(check(PyList_New(static_cast<Py_ssize_t>(rows.size()))));
    for (size_t index = 0; index < rows.size(); ++index)
        PyList_SET_ITEM(result.get(), static_cast<Py_ssize_t>(index), make(rows[index]));
    return result.release();
}

void read_rows(PyObject *lines, std::vector<V> &rows, Arena &arena) {
    rows.clear();
    S text;
    Ref iterator(check(PyObject_GetIter(lines)));
    while (PyObject *item = PyIter_Next(iterator.get())) {
        Ref owned(item);
        text.clear();
        append(text, item);
        rows.push_back(arena.keep(text));
    }
    if (PyErr_Occurred()) throw Fail();
}

V kept(Arena &arena, const S &text) { return arena.keep(V(text)); }

Ref attribute(PyObject *object, const char *name) { return Ref(check(PyObject_GetAttrString(object, name))); }
bool truth(PyObject *object) {
    int value = PyObject_IsTrue(object);
    check(value);
    return value != 0;
}
bool compare(PyObject *a, PyObject *b, int op) {
    Ref result(check(PyObject_RichCompare(a, b, op)));
    return truth(result.get());
}
Ref integer_of(PyObject *value) { return Ref(check(PyNumber_Long(value))); }
Ref item_of(PyObject *sequence, Py_ssize_t index) { return Ref(check(PySequence_GetItem(sequence, index))); }
Ref integer(long value) { return Ref(check(PyLong_FromLong(value))); }
Ref string(const char *text) { return Ref(check(PyUnicode_FromString(text))); }
void append_formatted(S &out, PyObject *value) {
    Ref text(check(PyObject_Format(value, constants().empty)));
    append(out, text.get());
}
void append_str(S &out, PyObject *value) {
    Ref text(check(PyObject_Str(value)));
    append(out, text.get());
}
void append_literal(S &out, const char *text) {
    for (size_t index = 0; text[index]; ++index) out.push_back(static_cast<U>(static_cast<unsigned char>(text[index])));
}
void append_joined(S &out, PyObject *items) {
    static PyObject *const separator = interned(", ");
    Ref text(check(PyUnicode_Join(separator, items)));
    append(out, text.get());
}
[[noreturn]] void raise_graph_object(PyObject *message) {
    PyErr_SetObject(graph_error(), message);
    throw Fail();
}
template <typename... Shown> [[noreturn]] void raise_graph_format(const char *format, Shown... shown) {
    Ref message(check(PyUnicode_FromFormat(format, shown...)));
    raise_graph_object(message.get());
}
PyObject *unpacked(PyObject *value, Py_ssize_t expected) {
    PyObject *parts = check(PySequence_Tuple(value));
    Py_ssize_t size = PyTuple_GET_SIZE(parts);
    if (size != expected) {
        Py_DECREF(parts);
        if (size > expected) PyErr_Format(PyExc_ValueError, "too many values to unpack (expected %zd)", expected);
        else PyErr_Format(PyExc_ValueError, "not enough values to unpack (expected %zd, got %zd)", expected, size);
        throw Fail();
    }
    return parts;
}

V integer_text(V digits, Arena &arena) {
    bool plain = true;
    for (U c : digits)
        if (c < '0' || c > '9') {
            plain = false;
            break;
        }
    size_t start = 0;
    if (plain) {
        while (start + 1 < digits.size() && digits[start] == '0') ++start;
        return digits.substr(start);
    }
    S value;
    value.reserve(digits.size());
    for (U c : digits) value.push_back(static_cast<U>('0' + Py_UNICODE_TODECIMAL(c)));
    while (start + 1 < value.size() && value[start] == '0') ++start;
    return arena.keep(V(value).substr(start));
}
bool integer_less(V a, V b) { return a.size() != b.size() ? a.size() < b.size() : a < b; }
void increment(S &value) {
    for (size_t index = value.size(); index-- > 0;) {
        if (value[index] != '9') {
            ++value[index];
            return;
        }
        value[index] = '0';
    }
    value.insert(value.begin(), U('1'));
}
S integer_after(const std::vector<V> &values) {
    if (values.empty()) return S(U"0");
    V largest = values.front();
    for (V value : values)
        if (integer_less(largest, value)) largest = value;
    S result(largest);
    increment(result);
    return result;
}
template <typename T> std::vector<V> integer_keys(const VMap<T> &map) {
    std::vector<V> keys;
    keys.reserve(map.size());
    for (const auto &item : map) keys.push_back(item.first);
    return keys;
}
void sort_integers(std::vector<V> &values) { std::sort(values.begin(), values.end(), integer_less); }

Span define_symbol(V line) { return starts(line, "define") ? keyword_symbol(line, 6) : Span{}; }
Span declare_symbol(V line) { return starts(line, "declare") ? keyword_symbol(line, 7) : Span{}; }

size_t symbol_end(V line, U sigil, bool type) {
    if (line.empty() || line[0] != sigil) return NPOS;
    auto rest = [&](size_t end) {
        size_t j = end;
        while (j < line.size() && space(line[j])) ++j;
        if (j >= line.size() || line[j] != '=') return false;
        if (!type) return true;
        ++j;
        while (j < line.size() && space(line[j])) ++j;
        return at(line, j, "type") && (j + 4 >= line.size() || !word(line[j + 4]));
    };
    if (line.size() > 1 && line[1] == '"') {
        size_t j = 2;
        while (j < line.size()) {
            U c = line[j];
            if (c == '"') {
                if (rest(j + 1)) return j + 1;
                break;
            }
            if (c == '\\') {
                if (j + 1 < line.size() && line[j + 1] != '\n') {
                    j += 2;
                    continue;
                }
                break;
            }
            ++j;
        }
    }
    size_t j = 1;
    while (j < line.size() && !space(line[j]) && line[j] != '=') ++j;
    return j > 1 && rest(j) ? j : NPOS;
}
bool module_symbol(V line) {
    return symbol_end(line, '@', false) != NPOS || define_symbol(line).found() || declare_symbol(line).found();
}

bool metadata_number(V line, V &digits) {
    if (line.size() < 2 || line[0] != '!') return false;
    size_t j = 1;
    while (j < line.size() && decimal(line[j])) ++j;
    if (j == 1) return false;
    size_t k = j;
    while (k < line.size() && space(line[k])) ++k;
    if (k >= line.size() || line[k] != '=') return false;
    digits = line.substr(1, j - 1);
    return true;
}

template <typename Take> void module_each(V text, Take take) {
    size_t i = 0;
    while (i < text.size()) {
        if (text[i] == '@') {
            size_t end = llvm_name(text, i + 1);
            if (end != NPOS) {
                take(text.substr(i, end - i));
                i = end;
                continue;
            }
        }
        ++i;
    }
}

template <typename Take> void calls_each(V text, Take take) {
    size_t i = 0;
    while (i < text.size()) {
        if (text[i] == '@') {
            size_t end = llvm_name(text, i + 1);
            if (end != NPOS && end < text.size() && text[end] == '(') {
                take(text.substr(i + 1, end - i - 1));
                i = end + 1;
                continue;
            }
        }
        ++i;
    }
}

template <typename Take> void numbered_each(V text, U mark, Take take) {
    size_t i = 0;
    while (i < text.size()) {
        if (text[i] == mark) {
            size_t end = i + 1;
            while (end < text.size() && decimal(text[end])) ++end;
            if (end > i + 1) {
                take(i, i + 1, end);
                i = end;
                continue;
            }
        }
        ++i;
    }
}

template <typename Renamed> V numbers_renamed(V line, U mark, Arena &arena, Renamed renamed) {
    if (find_char(line, mark) == NPOS) return line;
    S out;
    size_t last = 0;
    bool changed = false;
    numbered_each(line, mark, [&](size_t position, size_t start, size_t end) {
        out.append(line.substr(last, position - last));
        out.push_back(mark);
        renamed(out, line.substr(start, end - start));
        last = end;
        changed = true;
    });
    if (!changed) return line;
    out.append(line.substr(last));
    return arena.keep(V(out));
}

bool declaration_attribute(V line, size_t &start, V &digits) {
    size_t end = line.size();
    while (end > 0 && space(line[end - 1])) --end;
    size_t first = end;
    while (first > 0 && decimal(line[first - 1])) --first;
    if (first == end || first == 0 || line[first - 1] != '#') return false;
    size_t hash = first - 1, before = hash;
    while (before > 0 && space(line[before - 1])) --before;
    if (before == hash) return false;
    start = before;
    digits = line.substr(first, end - first);
    return true;
}

template <typename Take> void instruction_metadata_each(V text, Take take) {
    auto name = [](U c) {
        return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || ascii_digit(c) || c == '_' || c == '.' || c == '-';
    };
    size_t i = 0;
    while (i < text.size()) {
        if (text[i] == '!') {
            size_t j = i + 1;
            while (j < text.size() && name(text[j])) ++j;
            if (j > i + 1) {
                size_t k = j;
                while (k < text.size() && space(text[k])) ++k;
                if (k > j && k < text.size() && text[k] == '!') {
                    size_t end = k + 1;
                    while (end < text.size() && decimal(text[end])) ++end;
                    if (end > k + 1) {
                        take(text.substr(k + 1, end - k - 1));
                        i = end;
                        continue;
                    }
                }
            }
        }
        ++i;
    }
}

struct FunctionSpan { V name; size_t start, stop; };
struct Spans {
    std::vector<FunctionSpan> items;
    VMap<size_t> where;
    const FunctionSpan *find(V name) const {
        auto found = where.find(name);
        return found == where.end() ? nullptr : &items[found->second];
    }
};
Spans spans_in(const std::vector<V> &rows) {
    Spans spans;
    size_t index = 0;
    while (index < rows.size()) {
        V line = rows[index];
        Span name = define_symbol(line);
        if (!name.found()) {
            ++index;
            continue;
        }
        V symbol = line.substr(name.start, name.end - name.start);
        if (symbol.size() >= 2 && symbol.front() == '"' && symbol.back() == '"') symbol = symbol.substr(1, symbol.size() - 2);
        size_t stop = index + 1;
        while (stop < rows.size() && !same(strip(rows[stop]), "}")) ++stop;
        if (stop >= rows.size() || spans.where.count(symbol)) raise_graph("DXIL carrier contains an invalid function table.");
        spans.where.emplace(symbol, spans.items.size());
        spans.items.push_back({symbol, index, stop + 1});
        index = stop + 1;
    }
    return spans;
}

struct Tables {
    Ordered<V> define, declare, type, global;
    VMap<V> metadata;
    VMap<V> attributes;
};
void tables_of(const std::vector<V> &rows, Tables &tables, Arena &arena) {
    for (V line : rows) {
        if (line.empty()) continue;
        U first = line[0];
        if (first == 'd') {
            bool define = starts(line, "define");
            Span name = define ? keyword_symbol(line, 6) : declare_symbol(line);
            if (name.found()) (define ? tables.define : tables.declare).at(line.substr(name.start, name.end - name.start)) = line;
        } else if (first == '%' || first == '@') {
            size_t end = symbol_end(line, first, first == '%');
            if (end != NPOS) (first == '%' ? tables.type : tables.global).at(line.substr(0, end)) = line;
        } else if (first == '!') {
            V digits;
            if (metadata_number(line, digits)) tables.metadata[integer_text(digits, arena)] = line;
        } else if (first == 'a' && starts(line, "attributes #")) {
            size_t j = 12;
            while (j < line.size() && decimal(line[j])) ++j;
            if (j == 12 || !at(line, j, " = ") || line.size() <= j + 3) continue;
            tables.attributes[integer_text(line.substr(12, j - 12), arena)] = line.substr(j + 3);
        }
    }
}

struct InputOperation { size_t row_start, row_end, element_start, element_end, end; bool tail; };
bool input_operation(V text, size_t p, InputOperation &match) {
    size_t q = after(text, p, "@dx.op."), name = NPOS;
    for (const char *operation : {"loadInput", "evalCentroid", "evalSampleIndex", "evalSnapped"})
        if ((name = after(text, q, operation)) != NPOS) break;
    name = after(text, name, ".");
    if (name == NPOS) return false;
    size_t words = name;
    while (words < text.size() && word(text[words])) ++words;
    if (words == name) return false;
    size_t row_start = after(text, decimals(text, after(text, words, "(i32 ")), ", i32 ");
    size_t row_end = decimals(text, row_start);
    size_t element_start = after(text, row_end, ", i32 ");
    size_t element_end = decimals(text, element_start);
    if (element_end == NPOS) return false;
    match = {row_start, row_end, element_start, element_end, element_end, false};
    size_t tail = after(text, decimals(text, after(text, element_end, ", i8 ")), ", i32 ");
    if (tail != NPOS) {
        size_t end = tail;
        while (end < text.size() && (word(text[end]) || text[end] == '%' || text[end] == '.' || text[end] == '-')) ++end;
        if (end > tail) {
            match.end = end;
            match.tail = true;
        }
    }
    return true;
}

PyObject *module_re() {
    static PyObject *module = nullptr;
    if (!module) module = check(PyImport_ImportModule("re"));
    return module;
}

void carrier_inputs(std::vector<V> &rows, PyObject *mapping, Arena &arena) {
    static PyObject *const tail_pattern = interned("i32 [\\w%.-]+$");
    for (V &line : rows) {
        if (!contains(line, "@dx.op.")) continue;
        S out;
        size_t last = 0;
        bool changed = false;
        for (size_t p = find(line, "@dx.op."); p != NPOS;) {
            InputOperation match;
            if (!input_operation(line, p, match)) {
                p = find(line, "@dx.op.", p + 1);
                continue;
            }
            Ref first(to_int(line.substr(match.row_start, match.row_end - match.row_start)));
            Ref second(to_int(line.substr(match.element_start, match.element_end - match.element_start)));
            Ref location(check(PyTuple_Pack(2, first.get(), second.get())));
            int known = PySequence_Contains(mapping, location.get());
            check(known);
            if (!known) raise_graph("Carrier input operation has no native semantic row.");
            Ref value(check(PyObject_GetItem(mapping, location.get())));
            Ref parts(unpacked(value.get(), 3));
            PyObject *identity = PyTuple_GET_ITEM(parts.get(), 0), *row = PyTuple_GET_ITEM(parts.get(), 1);
            PyObject *element = PyTuple_GET_ITEM(parts.get(), 2);
            V head = line.substr(p, match.row_start - p);
            V tail = match.tail ? line.substr(match.element_end, match.end - match.element_end) : V();
            S rewritten(tail);
            if (element != Py_None) {
                if (tail.empty() || !contains(head, "loadInput"))
                    raise_graph("Indexed primitive input must be a native LoadInput operation");
                S replacement(U"i32 ");
                append_formatted(replacement, element);
                if (find_char(V(replacement), '\\') != NPOS) {
                    Ref template_text(make(V(replacement))), text(make(tail));
                    Ref result(check(PyObject_CallMethod(module_re(), "sub", "OOO", tail_pattern, template_text.get(), text.get())));
                    rewritten.clear();
                    append(rewritten, result.get());
                } else {
                    rewritten = S(tail.substr(0, tail.rfind(U"i32 "))) + replacement;
                }
            }
            out.append(line.substr(last, p - last));
            out.append(head);
            append_formatted(out, identity);
            append_literal(out, ", i32 ");
            append_formatted(out, row);
            out.append(rewritten);
            last = match.end;
            changed = true;
            p = find(line, "@dx.op.", match.end);
        }
        if (!changed) continue;
        out.append(line.substr(last));
        line = arena.keep(V(out));
    }
}

size_t spaces1(V text, size_t i) {
    if (i == NPOS || i >= text.size() || !space(text[i])) return NPOS;
    while (i < text.size() && space(text[i])) ++i;
    return i;
}
size_t spaces0(V text, size_t i) {
    if (i == NPOS) return NPOS;
    while (i < text.size() && space(text[i])) ++i;
    return i;
}
size_t handle_suffix(V text, size_t i) {
    size_t j = spaces1(text, after(text, spaces1(text, after(text, i, ",")), "i1"));
    size_t end = after(text, j, "true");
    if (end == NPOS) end = after(text, j, "false");
    return after(text, end, ")");
}
bool assigned(V line, size_t &name_end, size_t &after_equals) {
    if (line.empty() || line[0] != '%') return false;
    size_t j = 1;
    while (j < line.size() && !space(line[j]) && line[j] != '=') ++j;
    if (j == 1) return false;
    size_t k = spaces0(line, j);
    if (k >= line.size() || line[k] != '=') return false;
    name_end = j;
    after_equals = k + 1;
    return true;
}
size_t properties_size(V text, size_t i) {
    size_t j = spaces1(text, after(text, i, "%dx.types.ResourceProperties"));
    j = spaces1(text, after(text, spaces0(text, after(text, j, "{")), "i32"));
    j = spaces1(text, after(text, decimals(text, j), ","));
    return spaces1(text, after(text, j, "i32"));
}

struct Reserved {
    const std::vector<V> &body;
    VSet names;
    bool read = false;
    VSet &get() {
        if (!read) {
            for (V line : body) ssa_each(line, [&](size_t start, size_t end) { names.insert(line.substr(start, end - start)); });
            read = true;
        }
        return names;
    }
};

struct ResourceTables {
    std::unordered_map<PyObject *, Ref> by_id, by_binding;
    PyObject *get(PyObject *contract, bool binding) {
        auto &cache = binding ? by_binding : by_id;
        auto found = cache.find(contract);
        if (found != cache.end()) return found->second.get();
        Ref table = build(contract, binding);
        PyObject *result = table.get();
        cache.emplace(contract, std::move(table));
        return result;
    }
    static Ref build(PyObject *contract, bool by_binding) {
        Ref result(check(PyDict_New()));
        Ref resources(attribute(contract, "resources"));
        Ref iterator(check(PyObject_GetIter(resources.get())));
        while (PyObject *item = PyIter_Next(iterator.get())) {
            Ref owned(item);
            Ref register_class(attribute(item, "register_class"));
            Ref key;
            if (by_binding) {
                Ref space_value(attribute(item, "register_space"));
                Ref space(integer_of(space_value.get()));
                Ref point_value(attribute(item, "bind_point"));
                Ref point(integer_of(point_value.get()));
                key = Ref(check(PyTuple_Pack(3, register_class.get(), space.get(), point.get())));
            } else {
                Ref id_value(attribute(item, "resource_id"));
                Ref id(integer_of(id_value.get()));
                key = Ref(check(PyTuple_Pack(2, register_class.get(), id.get())));
            }
            check(PyDict_SetItem(result.get(), key.get(), item));
        }
        if (PyErr_Occurred()) throw Fail();
        return result;
    }
};

struct Handles {
    Handles(PyObject *source, PyObject *candidate, PyObject *overrides, PyObject *classes, PyObject *numbers,
            ResourceTables &tables)
        : source(source), candidate(candidate), overrides(overrides), classes(classes), numbers(numbers), tables(tables) {}
    PyObject *source, *candidate, *overrides, *classes, *numbers;
    ResourceTables &tables;
    PyObject *source_by_id = nullptr, *source_by_binding = nullptr, *candidate_by_binding = nullptr;

    static PyObject *lookup(PyObject *table, PyObject *key) {
        PyObject *found = PyDict_GetItemWithError(table, key);
        if (!found && PyErr_Occurred()) throw Fail();
        return found;
    }
    Ref override_key(PyObject *resource, PyObject *key) {
        static PyObject *const constant_buffer = interned("constant-buffer"), *const b = interned("b");
        Ref sampleable(attribute(resource, "sampleable"));
        if (truth(sampleable.get())) return Ref(check(PyObject_CallMethod(overrides, "get", "OO", key, key)));
        Ref input_type(attribute(resource, "input_type"));
        if (compare(input_type.get(), constant_buffer, Py_EQ)) {
            Ref first(item_of(key, 0)), second(item_of(key, 1));
            Ref prefixed(check(PyTuple_Pack(3, b, first.get(), second.get())));
            return Ref(check(PyObject_CallMethod(overrides, "get", "OO", prefixed.get(), key)));
        }
        return Ref(Py_NewRef(key));
    }
    PyObject *target(PyObject *register_class, PyObject *target_key) {
        Ref space_value(item_of(target_key, 0));
        Ref space(integer_of(space_value.get()));
        Ref point_value(item_of(target_key, 1));
        Ref point(integer_of(point_value.get()));
        Ref key(check(PyTuple_Pack(3, register_class, space.get(), point.get())));
        PyObject *found = lookup(candidate_by_binding, key.get());
        if (!found) {
            Ref shown_point(item_of(target_key, 1)), shown_space(item_of(target_key, 0));
            Ref a(check(PyObject_Format(register_class, constants().empty)));
            Ref b(check(PyObject_Format(shown_point.get(), constants().empty)));
            Ref c(check(PyObject_Format(shown_space.get(), constants().empty)));
            raise_graph_format("Rebuilt DXIL is missing %U%U space %U.", a.get(), b.get(), c.get());
        }
        return found;
    }
    Ref class_number(PyObject *resource) {
        Ref register_class(attribute(resource, "register_class"));
        return Ref(check(PyObject_GetItem(numbers, register_class.get())));
    }
};

V remapped_index(PyObject *source, PyObject *target, V index, Reserved &reserved, std::vector<V> &emitted, Arena &arena) {
    static PyObject *const all = check(PyLong_FromUnsignedLong(0xFFFFFFFFul)), *const zero = check(PyLong_FromLong(0));
    static PyObject *const one = check(PyLong_FromLong(1));
    auto extent = [&](PyObject *resource) {
        Ref value(attribute(resource, "bind_count"));
        return compare(value.get(), all, Py_EQ) ? Ref(Py_NewRef(zero)) : std::move(value);
    };
    Ref source_count = extent(source), target_count = extent(target);
    auto place = [&](PyObject *resource) {
        Ref space(attribute(resource, "register_space")), point(attribute(resource, "bind_point"));
        return Ref(check(PyTuple_Pack(2, space.get(), point.get())));
    };
    Ref source_place = place(source), target_place = place(target);
    if (compare(source_place.get(), target_place.get(), Py_NE)) {
        auto shape = [&](PyObject *resource, PyObject *count) {
            Ref a(attribute(resource, "input_type")), b(attribute(resource, "return_type"));
            Ref c(attribute(resource, "dimension")), d(attribute(resource, "sample_count"));
            return Ref(check(PyTuple_Pack(5, a.get(), b.get(), c.get(), d.get(), count)));
        };
        Ref source_shape = shape(source, source_count.get()), target_shape = shape(target, target_count.get());
        if (compare(source_shape.get(), target_shape.get(), Py_NE)) {
            Ref source_name(attribute(source, "name")), target_name(attribute(target, "name"));
            Ref a(check(PyObject_Format(source_name.get(), constants().empty)));
            Ref b(check(PyObject_Format(target_name.get(), constants().empty)));
            raise_graph_format("Resource remap %U -> %U changes its native type or array extent.", a.get(), b.get());
        }
    }
    if (compare(source_count.get(), one, Py_EQ)) {
        Ref point(attribute(target, "bind_point"));
        S out;
        append_str(out, point.get());
        return kept(arena, out);
    }
    Ref target_point(attribute(target, "bind_point")), source_point(attribute(source, "bind_point"));
    Ref offset(check(PyNumber_Subtract(target_point.get(), source_point.get())));
    if (!truth(offset.get())) return index;
    V stripped = strip(index);
    if (is_integer(stripped)) {
        Ref value(to_int(stripped));
        Ref sum(check(PyNumber_Add(value.get(), offset.get())));
        Ref masked(check(PyNumber_And(sum.get(), all)));
        S out;
        append_str(out, masked.get());
        return kept(arena, out);
    }
    VSet &names = reserved.get();
    S name(U"%reasy.resource_index");
    for (size_t serial = 1; names.count(V(name)); ++serial) {
        name = U"%reasy.resource_index_";
        for (char c : std::to_string(serial)) name.push_back(static_cast<U>(c));
    }
    V kept_name = kept(arena, name);
    names.insert(kept_name);
    S instruction(U"  ");
    instruction += kept_name;
    append_literal(instruction, " = add i32 ");
    instruction += stripped;
    append_literal(instruction, ", ");
    append_formatted(instruction, offset.get());
    emitted.push_back(kept(arena, instruction));
    return kept_name;
}

void legacy_handles(std::vector<V> &body, Handles &handles, Arena &arena) {
    handles.source_by_id = handles.tables.get(handles.source, false);
    handles.candidate_by_binding = handles.tables.get(handles.candidate, true);
    Reserved reserved{body};
    std::vector<V> result;
    result.reserve(body.size());
    for (V line : body) {
        if (!contains(line, "@dx.op.createHandle(")) {
            result.push_back(line);
            continue;
        }
        S out;
        size_t last = 0;
        bool changed = false;
        for (size_t p = find(line, "@dx.op.createHandle("); p != NPOS;) {
            size_t j = spaces1(line, after(line, after(line, spaces1(line, after(line, p, "@dx.op.createHandle(i32")), "57"), ","));
            size_t class_start = spaces1(line, after(line, j, "i8"));
            size_t class_end = decimals(line, class_start);
            size_t id_start = spaces1(line, after(line, spaces1(line, after(line, class_end, ",")), "i32"));
            size_t id_end = decimals(line, id_start);
            size_t index_start = spaces1(line, after(line, spaces1(line, after(line, id_end, ",")), "i32"));
            size_t index_end = run_before(line, index_start, ',');
            size_t end = handle_suffix(line, index_end);
            if (end == NPOS) {
                p = find(line, "@dx.op.createHandle(", p + 1);
                continue;
            }
            Ref class_value(to_int(line.substr(class_start, class_end - class_start)));
            PyObject *register_class = Handles::lookup(handles.classes, class_value.get());
            PyObject *class_key = register_class && truth(register_class) ? register_class : constants().empty;
            Ref id(to_int(line.substr(id_start, id_end - id_start)));
            Ref key(check(PyTuple_Pack(2, class_key, id.get())));
            PyObject *source = Handles::lookup(handles.source_by_id, key.get());
            if (!source) raise_graph("Recovered DXIL references an unknown resource range.");
            Ref keep_source(Py_NewRef(source));
            Ref space_value(attribute(source, "register_space"));
            Ref space(integer_of(space_value.get()));
            Ref point_value(attribute(source, "bind_point"));
            Ref point(integer_of(point_value.get()));
            Ref source_key(check(PyTuple_Pack(2, space.get(), point.get())));
            Ref target_key = handles.override_key(source, source_key.get());
            Ref source_class(attribute(source, "register_class"));
            PyObject *target = handles.target(source_class.get(), target_key.get());
            Ref keep_target(Py_NewRef(target));
            V index = remapped_index(source, target, line.substr(index_start, index_end - index_start), reserved, result, arena);
            Ref number = handles.class_number(target);
            Ref id_object(attribute(target, "resource_id"));
            Ref target_id(integer_of(id_object.get()));
            out.append(line.substr(last, class_start - last));
            append_str(out, number.get());
            append_literal(out, ", i32 ");
            append_str(out, target_id.get());
            append_literal(out, ", i32 ");
            out.append(index);
            out.append(line.substr(index_end, end - index_end));
            last = end;
            changed = true;
            p = find(line, "@dx.op.createHandle(", end);
        }
        if (!changed) {
            result.push_back(line);
            continue;
        }
        out.append(line.substr(last));
        result.push_back(kept(arena, out));
    }
    body.swap(result);
}

void modern_handles(std::vector<V> &body, Handles &handles, Arena &arena) {
    static PyObject *const constant_buffer = interned("constant-buffer");
    static PyObject *const all = check(PyLong_FromUnsignedLong(0xFFFFFFFFul)), *const zero = check(PyLong_FromLong(0));
    handles.source_by_binding = handles.tables.get(handles.source, true);
    handles.candidate_by_binding = handles.tables.get(handles.candidate, true);
    VMap<PyObject *> handle_resources;
    std::vector<Ref> held;
    std::vector<V> result;
    result.reserve(body.size());
    Reserved reserved{body};
    for (V line : body) {
        if (contains(line, "@dx.op.createHandleFromBinding(")) {
            V text = strip(line);
            size_t name_end = 0, after_equals = 0, binding = NPOS;
            if (assigned(text, name_end, after_equals)) {
                size_t j = spaces1(text, after(text, spaces0(text, after_equals), "call"));
                j = spaces1(text, after(text, j, "%dx.types.Handle"));
                j = spaces1(text, after(text, j, "@dx.op.createHandleFromBinding(i32"));
                j = spaces1(text, after(text, after(text, j, "217"), ","));
                binding = spaces1(text, after(text, j, "%dx.types.ResBind"));
            }
            size_t lower_start = NPOS, lower_end = NPOS, space_start = NPOS, space_end = NPOS, class_start = NPOS;
            size_t class_end = NPOS, middle = after(text, binding, "zeroinitializer");
            if (binding != NPOS && middle == NPOS) {
                lower_start = spaces1(text, after(text, spaces0(text, after(text, binding, "{")), "i32"));
                lower_end = decimals(text, lower_start);
                size_t upper = spaces1(text, after(text, spaces0(text, after(text, lower_end, ",")), "i32"));
                size_t upper_end = decimals(text, upper);
                if (upper_end == NPOS) upper_end = after(text, upper, "-1");
                space_start = spaces1(text, after(text, spaces0(text, after(text, upper_end, ",")), "i32"));
                space_end = decimals(text, space_start);
                class_start = spaces1(text, after(text, spaces0(text, after(text, space_end, ",")), "i8"));
                class_end = decimals(text, class_start);
                middle = after(text, spaces0(text, class_end), "}");
            }
            size_t index_start = spaces1(text, after(text, spaces1(text, after(text, middle, ",")), "i32"));
            size_t index_end = run_before(text, index_start, ',');
            if (handle_suffix(text, index_end) != NPOS) {
                Ref lower = lower_start == NPOS ? integer(0) : Ref(to_int(text.substr(lower_start, lower_end - lower_start)));
                Ref space = lower_start == NPOS ? integer(0) : Ref(to_int(text.substr(space_start, space_end - space_start)));
                Ref number = lower_start == NPOS ? integer(0) : Ref(to_int(text.substr(class_start, class_end - class_start)));
                Ref register_class(check(PyObject_CallMethod(handles.classes, "get", "OO", number.get(), constants().empty)));
                Ref key(check(PyTuple_Pack(3, register_class.get(), space.get(), lower.get())));
                PyObject *source = Handles::lookup(handles.source_by_binding, key.get());
                if (!source) raise_graph("Recovered DXIL uses an unknown resource binding.");
                held.emplace_back(Py_NewRef(source));
                Ref source_key(check(PyTuple_Pack(2, space.get(), lower.get())));
                Ref target_key = handles.override_key(source, source_key.get());
                PyObject *target = handles.target(register_class.get(), target_key.get());
                held.emplace_back(Py_NewRef(target));
                Ref count_value(attribute(target, "bind_count"));
                Ref count(integer_of(count_value.get()));
                Ref upper;
                if (compare(count.get(), zero, Py_EQ) || compare(count.get(), all, Py_EQ)) {
                    upper = integer(-1);
                } else {
                    Ref point_value(attribute(target, "bind_point")), extent_value(attribute(target, "bind_count"));
                    Ref point(integer_of(point_value.get())), extent(integer_of(extent_value.get()));
                    Ref sum(check(PyNumber_Add(point.get(), extent.get())));
                    Ref last_one = integer(1);
                    upper = Ref(check(PyNumber_Subtract(sum.get(), last_one.get())));
                }
                bool zero_binding = false;
                {
                    Ref point_value(attribute(target, "bind_point"));
                    Ref point(integer_of(point_value.get()));
                    if (compare(point.get(), zero, Py_EQ) && compare(upper.get(), zero, Py_EQ)) {
                        Ref space_value(attribute(target, "register_space"));
                        Ref target_space(integer_of(space_value.get()));
                        if (compare(target_space.get(), zero, Py_EQ)) {
                            Ref target_number = handles.class_number(target);
                            zero_binding = compare(target_number.get(), zero, Py_EQ);
                        }
                    }
                }
                S binding_text;
                if (zero_binding) {
                    append_literal(binding_text, "zeroinitializer");
                } else {
                    Ref point_value(attribute(target, "bind_point"));
                    Ref point(integer_of(point_value.get()));
                    Ref space_value(attribute(target, "register_space"));
                    Ref target_space(integer_of(space_value.get()));
                    Ref target_number = handles.class_number(target);
                    append_literal(binding_text, "{ i32 ");
                    append_formatted(binding_text, point.get());
                    append_literal(binding_text, ", i32 ");
                    append_formatted(binding_text, upper.get());
                    append_literal(binding_text, ", i32 ");
                    append_formatted(binding_text, target_space.get());
                    append_literal(binding_text, ", i8 ");
                    append_formatted(binding_text, target_number.get());
                    append_literal(binding_text, " }");
                }
                V index = remapped_index(source, target, text.substr(index_start, index_end - index_start), reserved, result, arena);
                S out(text.substr(0, binding));
                out += binding_text;
                out.append(text.substr(middle, index_start - middle));
                out.append(index);
                out.append(text.substr(index_end));
                line = kept(arena, out);
                handle_resources[text.substr(0, name_end)] = target;
            }
        }
        if (contains(line, "@dx.op.annotateHandle(")) {
            V text = strip(line);
            size_t name_end = 0, after_equals = 0, base_start = NPOS, base_end = NPOS, end = NPOS;
            if (assigned(text, name_end, after_equals)) {
                size_t j = spaces1(text, after(text, spaces0(text, after_equals), "call"));
                j = spaces1(text, after(text, j, "%dx.types.Handle"));
                j = spaces1(text, after(text, j, "@dx.op.annotateHandle(i32"));
                j = spaces1(text, after(text, after(text, j, "216"), ","));
                base_start = spaces1(text, after(text, j, "%dx.types.Handle"));
                if (base_start != NPOS && base_start < text.size() && text[base_start] == '%') {
                    base_end = run_before(text, base_start + 1, ',');
                    size_t size_start = properties_size(text, spaces1(text, after(text, base_end, ",")));
                    end = after(text, spaces0(text, decimals(text, size_start)), "}");
                }
            }
            auto found = end == NPOS ? handle_resources.end() : handle_resources.find(text.substr(base_start, base_end - base_start));
            if (found != handle_resources.end()) {
                PyObject *target = found->second;
                handle_resources[text.substr(0, name_end)] = target;
                Ref input_type(attribute(target, "input_type"));
                if (compare(input_type.get(), constant_buffer, Py_EQ)) {
                    Ref buffers(attribute(handles.candidate, "constant_buffers"));
                    Ref iterator(check(PyObject_GetIter(buffers.get())));
                    Ref buffer;
                    while (PyObject *item = PyIter_Next(iterator.get())) {
                        Ref owned(item);
                        Ref a_value(attribute(item, "bind_point"));
                        Ref a(integer_of(a_value.get()));
                        Ref b_value(attribute(target, "bind_point"));
                        Ref b(integer_of(b_value.get()));
                        if (!compare(a.get(), b.get(), Py_EQ)) continue;
                        Ref c_value(attribute(item, "register_space"));
                        Ref c(integer_of(c_value.get()));
                        Ref d_value(attribute(target, "register_space"));
                        Ref d(integer_of(d_value.get()));
                        if (!compare(c.get(), d.get(), Py_EQ)) continue;
                        buffer = std::move(owned);
                        break;
                    }
                    if (PyErr_Occurred()) throw Fail();
                    if (!buffer) {
                        Ref name(attribute(target, "name"));
                        Ref shown(check(PyObject_Format(name.get(), constants().empty)));
                        raise_graph_format("Rebuilt DXIL has no layout for constant buffer %U.", shown.get());
                    }
                    Ref size_value(attribute(buffer.get(), "byte_size"));
                    Ref size(integer_of(size_value.get()));
                    for (size_t p = find(line, "%dx.types.ResourceProperties"); p != NPOS;
                         p = find(line, "%dx.types.ResourceProperties", p + 1)) {
                        size_t digits = properties_size(line, p), digits_end = decimals(line, digits);
                        if (after(line, spaces0(line, digits_end), "}") == NPOS) continue;
                        S out(line.substr(0, digits));
                        append_str(out, size.get());
                        out.append(line.substr(digits_end));
                        line = kept(arena, out);
                        break;
                    }
                }
            }
        }
        result.push_back(line);
    }
    body.swap(result);
}

struct Extraction {
    PyObject *resource;
    bool dynamic;
    V row, lane, scalar, value;
    long long size;
};
struct ConstantResource {
    Ref binding;
    Ref point, space;
    long long space_value = 0, point_value = 0;
    bool constant_buffer = false;
};

class ConstantReads {
public:
    ConstantReads(PyObject *contract, PyObject *classes, ResourceTables &tables)
        : contract_(contract), classes_(classes), tables_(tables) {}

    template <typename Dynamic, typename Extracted>
    void read(const std::vector<V> &body, Dynamic dynamic, Extracted extracted) {
        by_id_ = tables_.get(contract_, false);
        by_binding_ = tables_.get(contract_, true);
        VMap<PyObject *> handles;
        VMap<std::pair<PyObject *, std::pair<bool, V>>> loads;
        for (V source_line : body) {
            V line = strip(source_line);
            size_t name_end = 0, after_equals = 0;
            if (!assigned(line, name_end, after_equals)) continue;
            bool legacy_op = false, modern_op = false, annotate_op = false;
            for (size_t p = find_char(line, '@', after_equals); p != NPOS; p = find_char(line, '@', p + 1)) {
                if (!at(line, p, "@dx.op.")) continue;
                V rest = line.substr(p + 7);
                legacy_op |= starts(rest, "createHandle(");
                modern_op |= starts(rest, "createHandleFromBinding(");
                annotate_op |= starts(rest, "annotateHandle(");
            }
            bool load_op = true, extract_op = true;
            if (legacy_op) {
                bool matched = false;
                PyObject *resource = legacy(line, after_equals, matched);
                if (matched) {
                    if (resource) handles[line.substr(0, name_end)] = resource;
                    continue;
                }
            }
            if (modern_op) {
                bool matched = false;
                PyObject *resource = modern(line, after_equals, matched);
                if (matched) {
                    if (resource) handles[line.substr(0, name_end)] = resource;
                    continue;
                }
            }
            if (annotate_op) {
                V base;
                if (annotation(line, after_equals, base)) {
                    auto found = handles.find(base);
                    if (found != handles.end()) {
                        PyObject *resource = found->second;
                        handles[line.substr(0, name_end)] = resource;
                    }
                    continue;
                }
            }
            if (load_op) {
                V handle, row;
                if (load(line, after_equals, handle, row)) {
                    auto found = handles.find(handle);
                    if (found != handles.end() && resource(found->second).constant_buffer) {
                        V stripped = strip(row);
                        bool is_dynamic = !all_decimal(stripped);
                        loads[line.substr(0, name_end)] = {found->second, {is_dynamic, stripped}};
                        if (is_dynamic) dynamic(resource(found->second));
                    }
                    continue;
                }
            }
            if (extract_op) {
                V load_name, scalar, lane;
                if (!extract(line, after_equals, load_name, scalar, lane)) continue;
                auto found = loads.find(load_name);
                if (found == loads.end()) continue;
                long long bits = scalar[1] == '1' ? 16 : scalar[1] == '3' ? 32 : 64;
                extracted(Extraction{found->second.first, found->second.second.first, found->second.second.second, lane,
                                     scalar, line.substr(0, name_end), bits / 8});
            }
        }
    }
    const ConstantResource &resource(PyObject *object) {
        auto found = resources_.find(object);
        if (found != resources_.end()) return found->second;
        static PyObject *const constant_buffer = interned("constant-buffer");
        ConstantResource item;
        Ref input_type(attribute(object, "input_type"));
        item.constant_buffer = compare(input_type.get(), constant_buffer, Py_EQ);
        Ref space_value(attribute(object, "register_space")), point_value(attribute(object, "bind_point"));
        item.space = integer_of(space_value.get());
        item.point = integer_of(point_value.get());
        item.binding = Ref(check(PyTuple_Pack(2, item.space.get(), item.point.get())));
        item.space_value = PyLong_AsLongLong(item.space.get());
        item.point_value = PyLong_AsLongLong(item.point.get());
        if (PyErr_Occurred()) throw Fail();
        held_.emplace_back(Py_NewRef(object));
        return resources_.emplace(object, std::move(item)).first->second;
    }

private:
    PyObject *class_of(PyObject *number) {
        PyObject *found = Handles::lookup(classes_, number);
        return found ? found : constants().empty;
    }
    PyObject *legacy(V line, size_t after_equals, bool &matched) {
        for (size_t p = line.size(); p-- > after_equals;) {
            if (line[p] != '@' || !at(line, p, "@dx.op.createHandle(")) continue;
            size_t j = spaces1(line, after(line, after(line, spaces1(line, after(line, p, "@dx.op.createHandle(i32")), "57"), ","));
            size_t class_start = spaces1(line, after(line, j, "i8"));
            size_t class_end = decimals(line, class_start);
            size_t id_start = spaces1(line, after(line, spaces1(line, after(line, class_end, ",")), "i32"));
            size_t id_end = decimals(line, id_start);
            if (after(line, id_end, ",") == NPOS) continue;
            matched = true;
            Ref number(to_int(line.substr(class_start, class_end - class_start)));
            Ref id(to_int(line.substr(id_start, id_end - id_start)));
            Ref key(check(PyTuple_Pack(2, class_of(number.get()), id.get())));
            return Handles::lookup(by_id_, key.get());
        }
        return nullptr;
    }
    PyObject *modern(V line, size_t after_equals, bool &matched) {
        for (size_t p = line.size(); p-- > after_equals;) {
            if (line[p] != '@' || !at(line, p, "@dx.op.createHandleFromBinding(")) continue;
            size_t j = spaces1(line, after(line, p, "@dx.op.createHandleFromBinding(i32"));
            j = spaces1(line, after(line, after(line, j, "217"), ","));
            size_t binding = spaces1(line, after(line, j, "%dx.types.ResBind"));
            if (binding == NPOS) continue;
            Ref lower, space, number;
            if (after(line, binding, "zeroinitializer") != NPOS) {
                lower = integer(0);
                space = integer(0);
                number = integer(0);
            } else {
                size_t lower_start = spaces1(line, after(line, spaces0(line, after(line, binding, "{")), "i32"));
                size_t lower_end = decimals(line, lower_start);
                size_t upper = spaces1(line, after(line, spaces0(line, after(line, lower_end, ",")), "i32"));
                size_t space_start = spaces1(line, after(line, spaces0(line, after(line, decimals(line, upper), ",")), "i32"));
                size_t space_end = decimals(line, space_start);
                size_t class_start = spaces1(line, after(line, spaces0(line, after(line, space_end, ",")), "i8"));
                size_t class_end = decimals(line, class_start);
                if (after(line, spaces0(line, class_end), "}") == NPOS) continue;
                lower = Ref(to_int(line.substr(lower_start, lower_end - lower_start)));
                space = Ref(to_int(line.substr(space_start, space_end - space_start)));
                number = Ref(to_int(line.substr(class_start, class_end - class_start)));
            }
            matched = true;
            Ref key(check(PyTuple_Pack(3, class_of(number.get()), space.get(), lower.get())));
            return Handles::lookup(by_binding_, key.get());
        }
        return nullptr;
    }
    static bool annotation(V line, size_t after_equals, V &base) {
        for (size_t p = line.size(); p-- > after_equals;) {
            if (line[p] != '@' || !at(line, p, "@dx.op.annotateHandle(")) continue;
            size_t j = run_before(line, p + 22, ',');
            j = spaces1(line, after(line, spaces1(line, after(line, j, ",")), "%dx.types.Handle"));
            if (j == NPOS || j >= line.size() || line[j] != '%') continue;
            size_t end = run_before(line, j + 1, ',');
            if (after(line, end, ",") == NPOS) continue;
            base = line.substr(j, end - j);
            return true;
        }
        return false;
    }
    static size_t scalar_type(V line, size_t i) {
        if (i >= line.size() || (line[i] != 'f' && line[i] != 'i')) return NPOS;
        for (const char *width : {"16", "32", "64"})
            if (at(line, i + 1, width)) return i + 3;
        return NPOS;
    }
    static bool load(V line, size_t after_equals, V &handle, V &row) {
        size_t j = spaces1(line, after(line, spaces0(line, after_equals), "call"));
        j = spaces1(line, scalar_type(line, after(line, j, "%dx.types.CBufRet.")));
        j = after(line, scalar_type(line, after(line, j, "@dx.op.cbufferLoadLegacy.")), "(i32");
        j = spaces1(line, after(line, after(line, spaces1(line, j), "59"), ","));
        j = spaces1(line, after(line, j, "%dx.types.Handle"));
        if (j == NPOS || j >= line.size() || line[j] != '%') return false;
        size_t handle_end = run_before(line, j + 1, ',');
        size_t start = spaces1(line, after(line, spaces1(line, after(line, handle_end, ",")), "i32"));
        size_t end = run_before(line, start, ')');
        if (after(line, end, ")") == NPOS) return false;
        handle = line.substr(j, handle_end - j);
        row = line.substr(start, end - start);
        return true;
    }
    static bool extract(V line, size_t after_equals, V &load_name, V &scalar, V &lane) {
        size_t j = after(line, spaces1(line, after(line, spaces0(line, after_equals), "extractvalue")), "%dx.types.CBufRet.");
        size_t scalar_end = scalar_type(line, j);
        size_t name = spaces1(line, scalar_end);
        if (name == NPOS || name >= line.size() || line[name] != '%') return false;
        size_t name_end = run_before(line, name + 1, ',');
        size_t digits = spaces1(line, after(line, name_end, ","));
        size_t digits_end = decimals(line, digits);
        if (digits_end == NPOS) return false;
        scalar = line.substr(j, scalar_end - j);
        load_name = line.substr(name, name_end - name);
        lane = line.substr(digits, digits_end - digits);
        return true;
    }

    PyObject *contract_, *classes_;
    ResourceTables &tables_;
    PyObject *by_id_ = nullptr, *by_binding_ = nullptr;
    std::unordered_map<PyObject *, ConstantResource> resources_;
    std::vector<Ref> held_;
};

Ref constant_word(PyObject *binding, PyObject *offset, PyObject *space) {
    static PyObject *const zero = check(PyLong_FromLong(0));
    for (PyObject *value : {binding, offset, space})
        if (compare(value, zero, Py_LT)) raise_graph("Constant word has an invalid binding, offset or register space");
    if (truth(space)) return Ref(check(PyTuple_Pack(3, binding, offset, space)));
    return Ref(check(PyTuple_Pack(2, binding, offset)));
}

PyObject *constant_extracts(PyObject *lines_object, PyObject *contract, PyObject *classes, PyObject *dynamic_bindings,
                            PyObject *dynamic_extracts, PyObject *read_ranges) {
    Texts texts = read_lines(lines_object);
    std::vector<V> body;
    body.reserve(texts.size());
    for (size_t index = 0; index < texts.size(); ++index) body.push_back(texts[index]);
    Strings strings;
    Ref result(check(PyDict_New()));
    ResourceTables tables;
    ConstantReads reads(contract, classes, tables);
    static PyObject *const sixteen = check(PyLong_FromLong(16));
    auto append_to = [](PyObject *table, PyObject *key, PyObject *item) {
        PyObject *list = PyDict_GetItemWithError(table, key);
        if (!list && PyErr_Occurred()) throw Fail();
        if (!list) {
            Ref made(check(PyList_New(0)));
            check(PyDict_SetItem(table, key, made.get()));
            list = made.get();
        }
        check(PyList_Append(list, item));
    };
    reads.read(
        body,
        [&](const ConstantResource &item) {
            if (dynamic_bindings != Py_None) check(PySet_Add(dynamic_bindings, item.binding.get()));
        },
        [&](const Extraction &read) {
            const ConstantResource &item = reads.resource(read.resource);
            Ref size(check(PyLong_FromLongLong(read.size)));
            Ref lane(to_int(read.lane));
            if (read_ranges != Py_None) {
                Ref offset;
                if (read.dynamic) {
                    offset = Ref(Py_NewRef(Py_None));
                } else {
                    Ref row(to_int(read.row));
                    Ref rows(check(PyNumber_Multiply(row.get(), sixteen)));
                    Ref lanes(check(PyNumber_Multiply(lane.get(), size.get())));
                    offset = Ref(check(PyNumber_Add(rows.get(), lanes.get())));
                }
                Ref entry(check(PyTuple_Pack(3, offset.get(), size.get(), strings.get(read.value))));
                append_to(read_ranges, item.binding.get(), entry.get());
            }
            if (read.size != 4) return;
            if (read.dynamic) {
                if (dynamic_extracts == Py_None) return;
                Ref key = constant_word(item.point.get(), lane.get(), item.space.get());
                Ref entry(check(PyTuple_Pack(3, strings.get(read.value), strings.get(read.row), strings.get(read.scalar))));
                append_to(dynamic_extracts, key.get(), entry.get());
                return;
            }
            Ref row(to_int(read.row));
            Ref rows(check(PyNumber_Multiply(row.get(), sixteen)));
            Ref four(check(PyLong_FromLong(4)));
            Ref lanes(check(PyNumber_Multiply(lane.get(), four.get())));
            Ref offset(check(PyNumber_Add(rows.get(), lanes.get())));
            Ref key = constant_word(item.point.get(), offset.get(), item.space.get());
            append_to(result.get(), key.get(), strings.get(read.value));
        });
    return result.release();
}

template <typename T> const T &python_item(const std::vector<T> &items, long long index) {
    long long size = static_cast<long long>(items.size());
    if (index < 0) index += size;
    if (index < 0 || index >= size) raise_index();
    return items[static_cast<size_t>(index)];
}
long long plan_integer(PyObject *value) {
    long long result = PyLong_AsLongLong(value);
    if (result == -1 && PyErr_Occurred()) throw Fail();
    return result;
}
S text_of(PyObject *value) { return read(value); }

template <typename T> struct Numbered {
    std::vector<std::pair<long long, T>> items;
    std::unordered_map<long long, size_t> where;
    T &at(long long key) {
        auto [found, added] = where.emplace(key, items.size());
        if (added) items.emplace_back(key, T());
        return items[found->second].second;
    }
    T *find(long long key) {
        auto found = where.find(key);
        return found == where.end() ? nullptr : &items[found->second].second;
    }
};

V renamed_text(V text, const VMap<V> &names, Arena &arena) {
    if (names.empty()) return text;
    S out;
    size_t last = 0;
    bool changed = false;
    ssa_each(text, [&](size_t start, size_t end) {
        auto found = names.find(text.substr(start, end - start));
        if (found == names.end()) return;
        out.append(text.substr(last, start - last));
        out += found->second;
        last = end;
        changed = true;
    });
    if (!changed) return text;
    out.append(text.substr(last));
    return arena.keep(V(out));
}
V renamed_uses(V line, const VMap<V> &names, Arena &arena) {
    Definition definition = value_match(line);
    size_t head = definition.found() ? definition.end : 0;
    V rest = line.substr(head), body = renamed_text(rest, names, arena);
    if (body.data() == rest.data() && body.size() == rest.size()) return line;
    S out(line.substr(0, head));
    out += body;
    return arena.keep(V(out));
}
V defined_name(V line, const Definition &definition) {
    return line.substr(definition.result.start, definition.result.end - definition.result.start);
}

bool stored_value(V line, V &value) {
    auto closes = [&](size_t at_paren) {
        size_t j = spaces0(line, at_paren + 1);
        if (j == line.size()) return true;
        if (line[j] != ',') return false;
        j = spaces0(line, j + 1);
        return j < line.size() && line[j] == '!';
    };
    for (size_t p = find_char(line, ','); p != NPOS; p = find_char(line, ',', p + 1)) {
        size_t j = spaces0(line, p + 1), type_end = NPOS;
        for (const char *type : {"half", "float", "double", "i1", "i8", "i16", "i32", "i64"}) {
            size_t end = after(line, j, type);
            if (end != NPOS && spaces1(line, end) != NPOS) {
                type_end = end;
                break;
            }
        }
        if (type_end == NPOS) continue;
        size_t group = spaces1(line, type_end);
        size_t stop = group;
        while (stop < line.size() && line[stop] != ',' && line[stop] != '(' && line[stop] != ')') ++stop;
        if (stop >= line.size() || line[stop] != ')' || !closes(stop)) continue;
        if (stop > group) {
            value = line.substr(group, stop - group);
            return true;
        }
        if (group - type_end >= 2) {
            value = line.substr(group - 1, 1);
            return true;
        }
    }
    return false;
}

struct CallParts { V result, return_type, callee, tail; std::vector<V> arguments; };
CallParts call_parts(V line) {
    auto result_char = [](U c) { return name_char(c) || c == '"' || c == '\\'; };
    size_t j = spaces0(line, 0);
    if (j >= line.size() || line[j] != '%' || j + 1 >= line.size() || !result_char(line[j + 1]))
        raise_graph("A DXIL surface helper call is malformed.");
    size_t result_end = j + 1;
    while (result_end < line.size() && result_char(line[result_end])) ++result_end;
    size_t call = spaces1(line, after(line, spaces0(line, after(line, spaces0(line, result_end), "=")), "call"));
    if (call == NPOS) raise_graph("A DXIL surface helper call is malformed.");
    size_t last_paren = line.rfind(U')');
    size_t call_keyword_end = call;
    while (call_keyword_end > 0 && space(line[call_keyword_end - 1])) --call_keyword_end;
    for (size_t type_start = call; type_start > call_keyword_end; --type_start) {
        for (size_t type_end = type_start + 1; type_end <= line.size(); ++type_end) {
            size_t at_name = spaces1(line, type_end);
            if (at_name == NPOS || at_name >= line.size() || line[at_name] != '@') continue;
            size_t name_end = llvm_name(line, at_name + 1);
            if (name_end == NPOS || name_end >= line.size() || line[name_end] != '(') continue;
            if (last_paren == NPOS || last_paren <= name_end) continue;
            CallParts parts;
            parts.result = line.substr(j, result_end - j);
            parts.return_type = line.substr(type_start, type_end - type_start);
            parts.callee = line.substr(at_name, name_end - at_name);
            parts.tail = line.substr(last_paren + 1);
            V inside = line.substr(name_end + 1, last_paren - name_end - 1);
            long long depth = 0;
            size_t current = 0;
            for (size_t index = 0; index < inside.size(); ++index) {
                U c = inside[index];
                if (c == ',' && depth == 0) {
                    parts.arguments.push_back(strip(inside.substr(current, index - current)));
                    current = index + 1;
                    continue;
                }
                depth += c == '(' || c == '[' || c == '{' || c == '<';
                depth -= c == ')' || c == ']' || c == '}' || c == '>';
            }
            V rest = strip(inside.substr(current));
            if (!rest.empty()) parts.arguments.push_back(rest);
            return parts;
        }
    }
    raise_graph("A DXIL surface helper call is malformed.");
}

struct HelperBody { std::vector<V> parameters, body; V returned; };
bool helper_body(const std::vector<V> &carrier_lines, const Spans &spans, V callee, HelperBody &helper) {
    V symbol = callee.substr(1);
    if (symbol.size() >= 2 && symbol.front() == '"' && symbol.back() == '"') symbol = symbol.substr(1, symbol.size() - 2);
    const FunctionSpan *span = spans.find(symbol);
    if (!span) raise_graph("DXIL carrier omitted a surface helper definition.");
    V header = carrier_lines[span->start];
    std::vector<V> body;
    for (size_t index = span->start + 1; index + 1 < span->stop; ++index)
        if (!strip(carrier_lines[index]).empty()) body.push_back(carrier_lines[index]);
    size_t open = find_char(header, '(');
    if (open == NPOS) raise_index();
    V signature = header.substr(open + 1);
    std::vector<V> parameters;
    for (size_t p = 0; p < signature.size(); ++p) {
        if (signature[p] != '%') continue;
        size_t end = p + 1;
        while (end < signature.size() && name_char(signature[end])) ++end;
        if (end == p + 1) continue;
        size_t next = spaces0(signature, end);
        if (next < signature.size() && (signature[next] == ',' || signature[next] == ')')) {
            parameters.push_back(signature.substr(p, end - p));
            p = end - 1;
        }
    }
    if (body.empty() || !starts(strip(body.back()), "ret ")) return false;
    for (V line : body) {
        V stripped = strip(line);
        if (starts(stripped, "br ") || starts(stripped, "switch ")) return false;
        size_t j = spaces0(line, 0);
        if (j < line.size() && line[j] == ';' && after(line, spaces0(line, j + 1), "<label>") != NPOS) return false;
        size_t end = j;
        while (end < line.size() && name_char(line[end])) ++end;
        if (end > j && end < line.size() && line[end] == ':') return false;
    }
    std::vector<V> words = split_space(strip(body.back()));
    if (words.size() < 3) raise_index();
    V last = strip(body.back());
    helper.returned = last.substr(static_cast<size_t>(words[2].data() - last.data()));
    helper.parameters = std::move(parameters);
    body.pop_back();
    helper.body = std::move(body);
    return true;
}

std::vector<V> value_closure(const std::vector<V> &carrier_entry, const std::vector<V> &roots) {
    VMap<V> definitions;
    std::vector<V> order;
    for (V line : carrier_entry) {
        Definition match = value_match(line);
        if (!match.found()) continue;
        V name = defined_name(line, match);
        definitions[name] = line;
        order.push_back(name);
    }
    VSet needed;
    std::vector<V> pending(roots);
    while (!pending.empty()) {
        V name = pending.back();
        pending.pop_back();
        auto found = definitions.find(name);
        if (needed.count(name) || found == definitions.end()) continue;
        needed.insert(name);
        V line = found->second;
        V body = line.substr(value_match(line).end);
        ssa_each(body, [&](size_t start, size_t end) { pending.push_back(body.substr(start, end - start)); });
    }
    std::vector<V> result;
    for (V name : order)
        if (needed.count(name)) result.push_back(definitions[name]);
    return result;
}

std::vector<V> merged_repeated(const std::vector<V> &code, const Accesses &access, VMap<V> &aliases, Arena &arena) {
    VMap<V> seen;
    VSet reads;
    std::vector<V> result;
    for (V original : code) {
        V line = renamed_text(original, aliases, arena);
        Definition match = value_match(line);
        V rest = match.found() ? line.substr(match.end) : line;
        size_t start = spaces0(rest, 0);
        if (start >= rest.size()) raise_index();
        size_t end = start;
        while (end < rest.size() && !space(rest[end])) ++end;
        V operation = rest.substr(start, end - start);
        Access kind = READNONE;
        Call callee = same(operation, "call") || same(operation, "tail") ? call_search(line) : Call{};
        if (callee.found()) {
            kind = access.get(line.substr(callee.name_start, callee.name_end - callee.name_start), EFFECT);
        } else {
            for (const char *effect : {"alloca", "store", "atomicrmw", "cmpxchg", "fence"})
                if (same(operation, effect)) kind = EFFECT;
            if (kind != EFFECT && same(operation, "load")) kind = READONLY;
        }
        if (kind == EFFECT || !match.found()) {
            for (V key : reads) seen.erase(key);
            reads.clear();
            result.push_back(line);
            continue;
        }
        V key = line.substr(match.end);
        auto found = seen.find(key);
        if (found != seen.end()) {
            aliases[defined_name(line, match)] = found->second;
            continue;
        }
        seen[key] = defined_name(line, match);
        if (kind == READONLY) reads.insert(key);
        result.push_back(line);
    }
    return result;
}

V with_arguments(V line, const std::vector<std::pair<long long, V>> &values, Arena &arena) {
    Call callee = call_search(line);
    size_t end = line.rfind(U')');
    if (!callee.found() || end == NPOS || end < callee.end) raise_graph("A DXIL store has an unexpected form.");
    std::vector<V> arguments;
    for (V piece : split_on(line.substr(callee.end, end - callee.end), ',')) arguments.push_back(strip(piece));
    std::vector<S> rewritten(arguments.size());
    std::vector<bool> changed(arguments.size(), false);
    for (const auto &[argument, value] : values) {
        if (argument >= static_cast<long long>(arguments.size())) raise_graph("A DXIL store has an unexpected form.");
        long long index = argument < 0 ? argument + static_cast<long long>(arguments.size()) : argument;
        if (index < 0) raise_index();
        V current = changed[index] ? V(rewritten[index]) : arguments[index];
        if (find_char(current, ' ') == NPOS) raise_graph("A DXIL store has an unexpected form.");
        S text(current.substr(0, current.rfind(U' ')));
        text.push_back(' ');
        text += value;
        rewritten[index] = std::move(text);
        changed[index] = true;
    }
    S out(line.substr(0, callee.end));
    for (size_t index = 0; index < arguments.size(); ++index) {
        if (index) append_literal(out, ", ");
        out += changed[index] ? V(rewritten[index]) : arguments[index];
    }
    out.append(line.substr(end));
    return kept(arena, out);
}

V numbered_name(const char *prefix, size_t number, Arena &arena) {
    S name;
    append_literal(name, prefix);
    for (char c : std::to_string(number)) name.push_back(static_cast<U>(c));
    return kept(arena, name);
}

struct PlanSite { PyObject *site; long long anchor; std::vector<V> results; };

void surface_plan(std::vector<V> &result, Ordered<std::vector<V>> &markers, const std::vector<V> &lines, long long start,
                  const std::vector<V> &carrier_lines, const Spans *carrier_spans, const std::vector<V> &carrier_entry,
                  PyObject *plan, PyObject *outputs, PyObject *value_width, Arena &arena) {
    static const char *const discarded = "%reasy.discarded";
    VMap<size_t> defining;
    for (size_t index = 0; index < lines.size(); ++index) {
        Definition match = value_match(lines[index]);
        if (match.found()) defining[defined_name(lines[index], match)] = index;
    }
    Numbered<std::vector<V>> carrier, native, prelude, replaced;
    Numbered<std::vector<std::pair<long long, V>>> stored;
    VMap<V> renamed;
    std::vector<Ref> held;
    std::vector<PlanSite> pending;
    size_t serial = 0;
    auto fresh = [&]() { return numbered_name("%reasy.surface.", ++serial, arena); };
    std::unique_ptr<Spans> own_spans;
    auto spans = [&]() -> const Spans & {
        if (carrier_spans) return *carrier_spans;
        if (!own_spans) own_spans = std::make_unique<Spans>(spans_in(carrier_lines));
        return *own_spans;
    };
    auto width_of = [&](PyObject *value_type) {
        Ref width(check(PyObject_CallOneArg(value_width, value_type)));
        return plan_integer(width.get());
    };
    auto python_text = [&](PyObject *value) { return kept(arena, text_of(value)); };
    Ref edits(check(PySequence_Tuple(plan)));
    for (Py_ssize_t edit_index = 0; edit_index < PyTuple_GET_SIZE(edits.get()); ++edit_index) {
        PyObject *edit = PyTuple_GET_ITEM(edits.get(), edit_index);
        Ref value_type(attribute(edit, "value_type"));
        long long width = width_of(value_type.get());
        Ref taps(attribute(edit, "taps"));
        long long tap_count = 0;
        {
            Ref iterator(check(PyObject_GetIter(taps.get())));
            while (PyObject *tap = PyIter_Next(iterator.get())) {
                Ref owned(tap);
                Ref parts(unpacked(tap, 2));
                tap_count += width_of(PyTuple_GET_ITEM(parts.get(), 1));
            }
            if (PyErr_Occurred()) throw Fail();
        }
        Ref records(attribute(edit, "records"));
        Ref discards(attribute(edit, "discards"));
        {
            Ref iterator(check(PyObject_GetIter(discards.get())));
            while (PyObject *line_object = PyIter_Next(iterator.get())) {
                Ref owned(line_object);
                long long index = plan_integer(line_object) - start;
                V condition;
                if (!stored_value(python_item(lines, index), condition)) raise_graph("A DXIL discard has an unexpected form.");
                std::vector<V> &replacement = replaced.at(index);
                replacement.clear();
                if (!truth(records.get())) continue;
                V recorded = fresh();
                S a(U"  "), b(U"  "), c(U"  "), d(U"  store i32 ");
                a += recorded; append_literal(a, " = load i32, i32* "); append_literal(a, discarded); append_literal(a, ", align 4");
                b += recorded; append_literal(b, ".bit = zext i1 "); b += condition; append_literal(b, " to i32");
                c += recorded; append_literal(c, ".any = or i32 "); c += recorded; append_literal(c, ", "); c += recorded;
                append_literal(c, ".bit");
                d += recorded; append_literal(d, ".any, i32* "); append_literal(d, discarded); append_literal(d, ", align 4");
                replacement = {kept(arena, a), kept(arena, b), kept(arena, c), kept(arena, d)};
            }
            if (PyErr_Occurred()) throw Fail();
        }
        Ref material(attribute(edit, "material"));
        bool is_material = truth(material.get());
        long long native_count = tap_count + plan_integer(material.get());
        Ref name_object(attribute(edit, "name"));
        S edit_name = text_of(name_object.get());
        Ref output(attribute(edit, "output"));
        Ref sites(attribute(edit, "sites"));
        Ref site_iterator(check(PyObject_GetIter(sites.get())));
        while (PyObject *site = PyIter_Next(site_iterator.get())) {
            held.emplace_back(site);
            Ref site_stores(attribute(site, "stores")), roots(attribute(site, "roots"));
            PyObject *places = truth(site_stores.get()) ? site_stores.get() : roots.get();
            std::vector<long long> store_lines;
            {
                Ref iterator(check(PyObject_GetIter(places)));
                while (PyObject *item = PyIter_Next(iterator.get())) {
                    Ref owned(item);
                    Ref parts(unpacked(item, 3));
                    store_lines.push_back(plan_integer(PyTuple_GET_ITEM(parts.get(), 0)));
                }
                if (PyErr_Occurred()) throw Fail();
            }
            Ref exit(attribute(site, "exit")), read_line(attribute(site, "read"));
            long long anchor;
            if (exit.get() != Py_None) {
                anchor = plan_integer(exit.get());
            } else if (read_line.get() != Py_None) {
                anchor = plan_integer(read_line.get()) + 1;
            } else {
                if (store_lines.empty()) {
                    PyErr_SetString(PyExc_ValueError, "min() iterable argument is empty");
                    throw Fail();
                }
                anchor = *std::min_element(store_lines.begin(), store_lines.end());
            }
            anchor -= start;
            std::vector<V> site_arguments;
            {
                Ref arguments(attribute(site, "arguments"));
                Ref iterator(check(PyObject_GetIter(arguments.get())));
                while (PyObject *item = PyIter_Next(iterator.get())) {
                    Ref owned(item);
                    site_arguments.push_back(python_text(item));
                }
                if (PyErr_Occurred()) throw Fail();
            }
            Ref texels_object(attribute(site, "texels"));
            if (read_line.get() != Py_None && truth(texels_object.get())) {
                V read_text = python_item(lines, plan_integer(read_line.get()) - start);
                size_t found = NPOS, found_end = NPOS;
                for (size_t p = find(read_text, "call %dx.types.ResRet."); p != NPOS; p = find(read_text, "call %dx.types.ResRet.", p + 1)) {
                    size_t end = p + 22;
                    while (end < read_text.size() && word(read_text[end])) ++end;
                    if (end > p + 22) {
                        found = p + 5;
                        found_end = end;
                        break;
                    }
                }
                if (found == NPOS) raise_graph("A DXIL texture read has an unexpected form.");
                V texel_type = read_text.substr(found, found_end - found), texels = python_text(texels_object.get());
                VMap<V> extracted;
                std::vector<V> &code = prelude.at(anchor);
                for (int component = 0; component < 4; ++component) {
                    V placeholder = numbered_name("%reasy.texel.", component, arena), name = fresh();
                    extracted[placeholder] = name;
                    S line(U"  ");
                    line += name;
                    append_literal(line, " = extractvalue ");
                    line += texel_type;
                    line.push_back(' ');
                    line += texels;
                    append_literal(line, ", ");
                    line.push_back(static_cast<U>('0' + component));
                    code.push_back(kept(arena, line));
                }
                for (V &item : site_arguments) {
                    auto replaced_item = extracted.find(item);
                    if (replaced_item != extracted.end()) item = replaced_item->second;
                }
            }
            if (std::find_if(site_arguments.begin(), site_arguments.end(), [](V item) { return same(item, discarded); })
                != site_arguments.end()) {
                V recorded = fresh();
                S a(U"  "), b(U"  ");
                a += recorded; append_literal(a, ".word = load i32, i32* "); append_literal(a, discarded); append_literal(a, ", align 4");
                b += recorded; append_literal(b, " = icmp ne i32 "); b += recorded; append_literal(b, ".word, 0");
                std::vector<V> &code = prelude.at(anchor);
                code.push_back(kept(arena, a));
                code.push_back(kept(arena, b));
                for (V &item : site_arguments)
                    if (same(item, discarded)) item = recorded;
            }
            Ref address_object(attribute(site, "material_address"));
            std::vector<V> code, results;
            for (long long component = 0; component < width; ++component) {
                S helper = edit_name;
                helper.push_back('_');
                for (char c : std::to_string(component)) helper.push_back(static_cast<U>(c));
                V call;
                bool found_call = false;
                for (V line : carrier_entry) {
                    if (!contains(line, " call ")) continue;
                    for (size_t p = line.find(V(helper)); p != NPOS; p = line.find(V(helper), p + 1)) {
                        size_t next = p + helper.size();
                        if (next < line.size() && ascii_digit(line[next])) continue;
                        found_call = true;
                        break;
                    }
                    if (found_call) {
                        call = line;
                        break;
                    }
                }
                if (!found_call) {
                    Ref shown(make(V(helper)));
                    raise_graph_format("DXIL carrier omitted surface helper %U.", shown.get());
                }
                CallParts parts = call_parts(call);
                if (static_cast<long long>(parts.arguments.size()) < native_count)
                    raise_graph("A DXIL surface helper lost its channel parameters.");
                std::vector<V> inputs(parts.arguments.begin(), parts.arguments.end() - native_count);
                std::vector<V> tapped;
                size_t tap_start = parts.arguments.size() - static_cast<size_t>(tap_count);
                for (size_t index = 0; tap_start + index < parts.arguments.size() && index < site_arguments.size(); ++index) {
                    V argument = parts.arguments[tap_start + index];
                    size_t space_at = argument.rfind(U' ');
                    S text(space_at == NPOS ? argument : argument.substr(0, space_at));
                    text.push_back(' ');
                    text += site_arguments[index];
                    tapped.push_back(kept(arena, text));
                }
                std::vector<V> address;
                if (is_material) {
                    S text(U"i32 ");
                    append_formatted(text, address_object.get());
                    address.push_back(kept(arena, text));
                }
                HelperBody helper_parts;
                bool inline_helper = helper_body(carrier_lines, spans(), parts.callee, helper_parts);
                std::vector<V> needed = inputs;
                if (inline_helper) {
                    VSet used;
                    for (V line : helper_parts.body) ssa_each(line, [&](size_t s, size_t e) { used.insert(line.substr(s, e - s)); });
                    ssa_each(helper_parts.returned, [&](size_t s, size_t e) { used.insert(helper_parts.returned.substr(s, e - s)); });
                    needed.clear();
                    for (size_t index = 0; index < helper_parts.parameters.size() && index < inputs.size(); ++index)
                        if (used.count(helper_parts.parameters[index])) needed.push_back(inputs[index]);
                }
                std::vector<V> roots;
                for (V argument : needed) ssa_each(argument, [&](size_t s, size_t e) { roots.push_back(argument.substr(s, e - s)); });
                std::vector<V> closure = value_closure(carrier_entry, roots);
                VMap<V> names;
                for (V line : closure) {
                    Definition match = value_match(line);
                    if (match.found()) names[defined_name(line, match)] = fresh();
                }
                for (V line : closure) code.push_back(renamed_text(line, names, arena));
                std::vector<V> arguments;
                for (V argument : inputs) arguments.push_back(renamed_text(argument, names, arena));
                arguments.insert(arguments.end(), address.begin(), address.end());
                arguments.insert(arguments.end(), tapped.begin(), tapped.end());
                if (inline_helper) {
                    VMap<V> inlined;
                    for (size_t index = 0; index < helper_parts.parameters.size() && index < arguments.size(); ++index) {
                        V argument = arguments[index];
                        size_t space_at = argument.rfind(U' ');
                        if (space_at == NPOS) raise_index();
                        inlined[helper_parts.parameters[index]] = argument.substr(space_at + 1);
                    }
                    for (V line : helper_parts.body) {
                        Definition match = value_match(line);
                        if (match.found()) inlined[defined_name(line, match)] = fresh();
                    }
                    for (V line : helper_parts.body) code.push_back(renamed_text(line, inlined, arena));
                    results.push_back(renamed_text(helper_parts.returned, inlined, arena));
                } else {
                    V value = fresh();
                    S line(U"  ");
                    line += value;
                    append_literal(line, " = call ");
                    line += parts.return_type;
                    line.push_back(' ');
                    line += parts.callee;
                    line.push_back('(');
                    for (size_t index = 0; index < arguments.size(); ++index) {
                        if (index) append_literal(line, ", ");
                        line += arguments[index];
                    }
                    line.push_back(')');
                    line += parts.tail;
                    code.push_back(kept(arena, line));
                    results.push_back(value);
                }
            }
            if (truth(output.get())) {
                Ref row(check(PyObject_GetItem(outputs, output.get())));
                Ref parts(unpacked(row.get(), 2));
                S type_text = text_of(value_type.get());
                bool floating = starts(V(type_text), "float");
                for (size_t component = 0; component < results.size(); ++component) {
                    S line(U"  call void @dx.op.storeOutput.");
                    append_literal(line, floating ? "f32" : "i32");
                    append_literal(line, "(i32 5, i32 ");
                    append_formatted(line, PyTuple_GET_ITEM(parts.get(), 0));
                    append_literal(line, ", i32 0, i8 ");
                    for (char c : std::to_string(component)) line.push_back(static_cast<U>(c));
                    append_literal(line, ", ");
                    append_literal(line, floating ? "float" : "i32");
                    line.push_back(' ');
                    line += results[component];
                    line.push_back(')');
                    code.push_back(kept(arena, line));
                }
            } else if (exit.get() != Py_None && !same(python_item(results, 0), "false")) {
                S line(U"  call void @dx.op.discard(i32 82, i1 ");
                line += results[0];
                line.push_back(')');
                code.push_back(kept(arena, line));
            }
            std::vector<V> &placed = carrier.at(anchor);
            placed.insert(placed.end(), code.begin(), code.end());
            pending.push_back({site, anchor, std::move(results)});
        }
        if (PyErr_Occurred()) throw Fail();
    }
    Accesses access = access_of(carrier_lines);
    VMap<V> aliases;
    for (auto &[anchor, code] : carrier.items) code = merged_repeated(code, access, aliases, arena);
    for (PlanSite &item : pending) {
        for (V &value : item.results) {
            auto alias = aliases.find(value);
            if (alias != aliases.end()) value = alias->second;
        }
        Ref exit(attribute(item.site, "exit"));
        if (exit.get() != Py_None) continue;
        Ref read_line(attribute(item.site, "read"));
        if (read_line.get() != Py_None) {
            Ref lanes(attribute(item.site, "lanes"));
            Ref iterator(check(PyObject_GetIter(lanes.get())));
            while (PyObject *lane = PyIter_Next(iterator.get())) {
                Ref owned(lane);
                Ref parts(unpacked(lane, 2));
                V value = python_item(item.results, plan_integer(PyTuple_GET_ITEM(parts.get(), 0)));
                renamed[python_text(PyTuple_GET_ITEM(parts.get(), 1))] = value;
            }
            if (PyErr_Occurred()) throw Fail();
            continue;
        }
        Ref site_stores(attribute(item.site, "stores"));
        if (truth(site_stores.get())) {
            Ref iterator(check(PyObject_GetIter(site_stores.get())));
            while (PyObject *store = PyIter_Next(iterator.get())) {
                Ref owned(store);
                Ref parts(unpacked(store, 3));
                long long line = plan_integer(PyTuple_GET_ITEM(parts.get(), 0));
                long long component = plan_integer(PyTuple_GET_ITEM(parts.get(), 1));
                long long argument = plan_integer(PyTuple_GET_ITEM(parts.get(), 2));
                V value = python_item(item.results, component);
                auto &values = stored.at(line - start);
                auto existing = std::find_if(values.begin(), values.end(), [&](const auto &entry) { return entry.first == argument; });
                if (existing != values.end()) existing->second = value;
                else values.emplace_back(argument, value);
            }
            if (PyErr_Occurred()) throw Fail();
            continue;
        }
        VMap<V> mapping;
        {
            Ref frontier(attribute(item.site, "frontier"));
            Ref iterator(check(PyObject_GetIter(frontier.get())));
            size_t index = 0;
            while (PyObject *name = PyIter_Next(iterator.get())) {
                Ref owned(name);
                if (index >= item.results.size()) break;
                mapping[python_text(name)] = item.results[index++];
            }
            if (PyErr_Occurred()) throw Fail();
        }
        Ref tail(attribute(item.site, "tail"));
        {
            Ref iterator(check(PyObject_GetIter(tail.get())));
            while (PyObject *name_object = PyIter_Next(iterator.get())) {
                Ref owned(name_object);
                V name = python_text(name_object);
                auto defined = defining.find(name);
                if (defined == defining.end()) {
                    PyErr_SetObject(PyExc_KeyError, name_object);
                    throw Fail();
                }
                V original = lines[defined->second];
                V body = original.substr(value_match(original).end);
                V rewritten = renamed_text(body, mapping, arena);
                if (rewritten == body) {
                    mapping[name] = name;
                    continue;
                }
                V value = fresh();
                mapping[name] = value;
                S line(U"  ");
                line += value;
                append_literal(line, " =");
                line += rewritten;
                native.at(item.anchor).push_back(kept(arena, line));
            }
            if (PyErr_Occurred()) throw Fail();
        }
        Ref site_roots(attribute(item.site, "roots"));
        Ref iterator(check(PyObject_GetIter(site_roots.get())));
        while (PyObject *root = PyIter_Next(iterator.get())) {
            Ref owned(root);
            Ref parts(unpacked(root, 3));
            long long line = plan_integer(PyTuple_GET_ITEM(parts.get(), 0));
            V name = python_text(PyTuple_GET_ITEM(parts.get(), 1));
            long long argument = plan_integer(PyTuple_GET_ITEM(parts.get(), 2));
            auto found = mapping.find(name);
            if (found == mapping.end() || found->second == name) continue;
            auto &values = stored.at(line - start);
            auto existing = std::find_if(values.begin(), values.end(), [&](const auto &entry) { return entry.first == argument; });
            if (existing != values.end()) existing->second = found->second;
            else values.emplace_back(argument, found->second);
        }
        if (PyErr_Occurred()) throw Fail();
    }
    bool any_records = false;
    for (Py_ssize_t edit_index = 0; edit_index < PyTuple_GET_SIZE(edits.get()) && !any_records; ++edit_index) {
        Ref records(attribute(PyTuple_GET_ITEM(edits.get(), edit_index), "records"));
        any_records = truth(records.get());
    }
    result.clear();
    for (size_t index = 0; index < lines.size(); ++index) {
        long long key = static_cast<long long>(index);
        if (index == 1 && any_records) {
            S a(U"  "), b(U"  store i32 0, i32* ");
            append_literal(a, discarded);
            append_literal(a, " = alloca i32, align 4");
            append_literal(b, discarded);
            append_literal(b, ", align 4");
            result.push_back(kept(arena, a));
            result.push_back(kept(arena, b));
        }
        if (std::vector<V> *replacement = replaced.find(key)) {
            result.insert(result.end(), replacement->begin(), replacement->end());
            continue;
        }
        if (std::vector<V> *code = prelude.find(key)) result.insert(result.end(), code->begin(), code->end());
        std::vector<V> *code = carrier.find(key);
        if (code && !code->empty()) {
            S marker(U"; reasy.surface.code ");
            for (char c : std::to_string(index)) marker.push_back(static_cast<U>(c));
            V marker_view = kept(arena, marker);
            markers.at(marker_view) = *code;
            result.push_back(marker_view);
        }
        if (std::vector<V> *added = native.find(key)) result.insert(result.end(), added->begin(), added->end());
        V line = lines[index];
        if (auto *values = stored.find(key)) line = with_arguments(line, *values, arena);
        result.push_back(line);
    }
    if (!renamed.empty())
        for (V &line : result) line = renamed_uses(line, renamed, arena);
}

PyObject *metadata_of(PyObject *metadata, const std::vector<V> &rows, PyObject *parse) {
    if (truth(metadata)) return Py_NewRef(metadata);
    Ref lines(row_list(rows));
    return check(PyObject_CallOneArg(parse, lines.get()));
}

void place_markers(std::vector<V> &body, const Ordered<std::vector<V>> &markers) {
    std::vector<V> result;
    result.reserve(body.size());
    size_t placed = 0;
    for (V line : body) {
        auto found = markers.position.find(line);
        if (found == markers.position.end()) {
            result.push_back(line);
            continue;
        }
        const std::vector<V> &code = markers.items[found->second].second;
        result.insert(result.end(), code.begin(), code.end());
        ++placed;
    }
    if (placed != markers.items.size()) raise_graph("A DXIL rewrite lost the place of a surface edit.");
    body.swap(result);
}

template <typename Make> void replace_record(std::vector<V> &rows, const S &reference, Make make_record, Arena &arena) {
    S prefix = reference;
    append_literal(prefix, " = ");
    V record;
    bool made = false;
    for (V &row : rows) {
        if (row.size() < prefix.size() || row.substr(0, prefix.size()) != V(prefix)) continue;
        if (!made) {
            S line = reference;
            append_literal(line, " = !{");
            make_record(line);
            line.push_back('}');
            record = kept(arena, line);
            made = true;
        }
        row = record;
    }
}

PyObject *merge_entry(PyObject *original_object, PyObject *carrier_object, PyObject *entry_point,
                      PyObject *native_object, PyObject *carrier_metadata_object, PyObject *indexed_reads,
                      PyObject *source_contract, PyObject *candidate_contract, PyObject *overrides, PyObject *plan,
                      PyObject *steps, PyObject *hooks) {
    PyObject *parse, *input_rows, *value_width, *classes, *numbers;
    if (!PyArg_ParseTuple(hooks, "OOOOO:merge_entry hooks", &parse, &input_rows, &value_width, &classes, &numbers))
        throw Fail();
    Arena arena;
    Rows original(original_object), carrier(carrier_object);
    const std::vector<V> &original_rows = original.rows;
    std::vector<V> carrier_rows = carrier.rows;
    Spans original_spans = spans_in(original_rows), carrier_spans = spans_in(carrier_rows);
    S entry_text = read(entry_point);
    V entry_name(entry_text);
    const FunctionSpan *carrier_span = carrier_spans.find(entry_name);
    if (!original_spans.find(entry_name) || !carrier_span) raise_graph("DXIL entry-function routing is inconsistent.");
    size_t carrier_start = carrier_span->start, carrier_stop = carrier_span->stop;
    Ref native(metadata_of(native_object, original_rows, parse));
    Ref carrier_metadata(metadata_of(carrier_metadata_object, carrier_rows, parse));
    Ref mapping(check(PyObject_CallFunctionObjArgs(input_rows, native.get(), carrier_metadata.get(), indexed_reads, nullptr)));
    std::vector<V> carrier_entry(carrier_rows.begin() + static_cast<std::ptrdiff_t>(carrier_start),
                                 carrier_rows.begin() + static_cast<std::ptrdiff_t>(carrier_stop));
    carrier_inputs(carrier_entry, mapping.get(), arena);
    Tables original_tables, carrier_tables;
    tables_of(original_rows, original_tables, arena);
    tables_of(carrier_rows, carrier_tables, arena);

    VSet referenced_attributes;
    for (const FunctionSpan &span : original_spans.items)
        for (size_t index = span.start; index < span.stop; ++index) {
            V line = original_rows[index];
            numbered_each(line, '#', [&](size_t, size_t start, size_t end) {
                referenced_attributes.insert(integer_text(line.substr(start, end - start), arena));
            });
        }
    for (const auto &item : original_tables.declare.items) {
        size_t start;
        V digits;
        if (declaration_attribute(item.second, start, digits)) referenced_attributes.insert(integer_text(digits, arena));
    }
    for (V number : referenced_attributes)
        if (!original_tables.attributes.count(number)) raise_graph("Native DXIL function references an absent attribute group.");
    std::vector<V> old_attributes(referenced_attributes.begin(), referenced_attributes.end());
    sort_integers(old_attributes);
    S next_attribute = integer_after(integer_keys(carrier_tables.attributes));
    VMap<V> attribute_map;
    for (V old : old_attributes) {
        V number = kept(arena, next_attribute);
        attribute_map[old] = number;
        increment(next_attribute);
        S line(U"attributes #");
        line += number;
        append_literal(line, " = ");
        line += original_tables.attributes[old];
        carrier_rows.push_back(kept(arena, line));
    }
    auto renumbered_attribute = [&](S &out, V digits) { out += attribute_map.at(integer_text(digits, arena)); };

    ResourceTables tables;
    Handles handles(source_contract, candidate_contract, overrides, classes, numbers, tables);
    std::vector<V> entry;
    for (const FunctionSpan &span : original_spans.items) {
        if (span.name != entry_name && carrier_spans.find(span.name)) {
            Ref name(make(span.name));
            raise_graph_format("Native DXIL function %U collides with an expression helper.", name.get());
        }
        std::vector<V> body;
        body.reserve(span.stop - span.start);
        for (size_t index = span.start; index < span.stop; ++index)
            body.push_back(numbers_renamed(original_rows[index], '#', arena, renumbered_attribute));
        Ordered<std::vector<V>> markers;
        if (truth(plan) && span.name == entry_name) {
            Ref outputs(check(PyObject_CallMethod(native.get(), "output_rows", nullptr)));
            std::vector<V> spliced;
            surface_plan(spliced, markers, body, static_cast<long long>(span.start), carrier_rows, &carrier_spans,
                         carrier_entry, plan, outputs.get(), value_width, arena);
            body.swap(spliced);
        }
        legacy_handles(body, handles, arena);
        modern_handles(body, handles, arena);
        if (steps != Py_None) {
            Ref body_list(row_list(body)), entry_list(row_list(carrier_entry));
            Ref stepped(check(PyObject_CallFunctionObjArgs(steps, body_list.get(), entry_list.get(), native.get(), nullptr)));
            read_rows(stepped.get(), body, arena);
        }
        if (!markers.items.empty()) place_markers(body, markers);
        entry.insert(entry.end(), body.begin(), body.end());
    }

    VSet calls;
    for (V line : entry) calls_each(line, [&](V name) { calls.insert(name); });
    std::vector<V> called(calls.begin(), calls.end());
    std::sort(called.begin(), called.end());
    std::vector<V> additions;
    for (V name : called) {
        V symbol = name.size() >= 2 && name.front() == '"' && name.back() == '"' ? name.substr(1, name.size() - 2) : name;
        if (original_spans.find(symbol) || carrier_tables.define.find(name) || carrier_tables.declare.find(name)) continue;
        V *declaration = original_tables.declare.find(name);
        if (!declaration) {
            Ref shown(make(symbol));
            raise_graph_format("Recovered DXIL calls undeclared function %U.", shown.get());
        }
        size_t start;
        V digits;
        if (!declaration_attribute(*declaration, start, digits)) {
            additions.push_back(*declaration);
            continue;
        }
        S line(declaration->substr(0, start));
        append_literal(line, " #");
        line += attribute_map.at(integer_text(digits, arena));
        additions.push_back(kept(arena, line));
    }

    VSet missing_globals;
    for (const auto &item : original_tables.global.items)
        if (!carrier_tables.global.find(item.first)) missing_globals.insert(item.first);
    VSet required_globals;
    if (!missing_globals.empty())
        for (V line : entry)
            module_each(line, [&](V token) {
                if (missing_globals.count(token)) required_globals.insert(token);
            });
    std::vector<V> pending_globals(required_globals.begin(), required_globals.end());
    while (!pending_globals.empty()) {
        V name = pending_globals.back();
        pending_globals.pop_back();
        module_each(*original_tables.global.find(name), [&](V dependency) {
            if (original_tables.global.find(dependency) && !carrier_tables.global.find(dependency)
                && required_globals.insert(dependency).second)
                pending_globals.push_back(dependency);
        });
    }
    std::vector<V> global_names(required_globals.begin(), required_globals.end());
    std::sort(global_names.begin(), global_names.end());
    std::vector<V> imported_globals;
    for (V name : global_names) imported_globals.push_back(*original_tables.global.find(name));

    VSet missing_types;
    for (const auto &item : original_tables.type.items)
        if (!carrier_tables.type.find(item.first)) missing_types.insert(item.first);
    VSet required_types;
    if (!missing_types.empty()) {
        std::vector<char> lengths;
        for (V name : missing_types) {
            if (lengths.size() <= name.size()) lengths.resize(name.size() + 1, 0);
            lengths[name.size()] = 1;
        }
        auto scan = [&](V line) {
            ssa_each(line, [&](size_t start, size_t end) {
                if (end - start >= lengths.size() || !lengths[end - start]) return;
                V token = line.substr(start, end - start);
                if (missing_types.count(token)) required_types.insert(token);
            });
        };
        for (V line : entry) scan(line);
        for (V line : additions) scan(line);
        for (V line : imported_globals) scan(line);
    }
    std::vector<V> pending_types(required_types.begin(), required_types.end());
    std::vector<V> imported_types;
    VSet imported_type_names;
    while (!pending_types.empty()) {
        V name = pending_types.back();
        pending_types.pop_back();
        if (carrier_tables.type.find(name) || imported_type_names.count(name)) continue;
        V *definition = original_tables.type.find(name);
        if (!definition) continue;
        imported_types.push_back(*definition);
        imported_type_names.insert(name);
        V text = *definition;
        ssa_each(text, [&](size_t start, size_t end) {
            V token = text.substr(start, end - start);
            if (token != name && original_tables.type.find(token)) pending_types.push_back(token);
        });
    }

    VSet referenced_metadata;
    auto instruction_metadata = [&](V line) {
        if (find_char(line, '!') == NPOS) return;
        instruction_metadata_each(line, [&](V digits) { referenced_metadata.insert(integer_text(digits, arena)); });
    };
    for (V line : entry) instruction_metadata(line);
    for (V line : imported_globals) instruction_metadata(line);
    Ref native_operands(attribute(native.get(), "operands"));
    {
        Ref second(item_of(native_operands.get(), 2)), fourth(item_of(native_operands.get(), 4));
        for (PyObject *reference : {second.get(), fourth.get()}) {
            S text = read(reference);
            V view(text);
            if (view.size() >= 2 && view[0] == '!' && all_decimal(view.substr(1)))
                referenced_metadata.insert(integer_text(arena.keep(view.substr(1)), arena));
        }
    }
    const V *native_view_state = nullptr;
    for (const V &line : original_rows)
        if (starts(line, "!dx.viewIdState = ")) {
            native_view_state = &line;
            break;
        }
    if (native_view_state)
        numbered_each(*native_view_state, '!', [&](size_t, size_t start, size_t end) {
            referenced_metadata.insert(integer_text(native_view_state->substr(start, end - start), arena));
        });
    VSet closure(referenced_metadata);
    std::vector<V> pending_ids(referenced_metadata.begin(), referenced_metadata.end());
    while (!pending_ids.empty()) {
        V current = pending_ids.back();
        pending_ids.pop_back();
        auto found = original_tables.metadata.find(current);
        if (found == original_tables.metadata.end()) raise_graph("Recovered DXIL references missing instruction metadata.");
        V rest = found->second.substr(find_char(found->second, '=') + 1);
        numbered_each(rest, '!', [&](size_t, size_t start, size_t end) {
            V number = integer_text(rest.substr(start, end - start), arena);
            if (closure.insert(number).second) pending_ids.push_back(number);
        });
    }
    std::vector<V> closure_ids(closure.begin(), closure.end());
    sort_integers(closure_ids);
    S next_metadata = integer_after(integer_keys(carrier_tables.metadata));
    VMap<V> metadata_map;
    for (V old : closure_ids) {
        metadata_map[old] = kept(arena, next_metadata);
        increment(next_metadata);
    }
    auto remap_metadata = [&](V line) {
        return numbers_renamed(line, '!', arena, [&](S &out, V digits) {
            V number = integer_text(digits, arena);
            auto found = metadata_map.find(number);
            out += found == metadata_map.end() ? number : found->second;
        });
    };
    for (V &line : entry) line = remap_metadata(line);
    std::vector<V> metadata_additions;
    for (V old : closure_ids) metadata_additions.push_back(remap_metadata(original_tables.metadata.at(old)));

    auto insert_rows = [&](size_t position, std::vector<V> rows) {
        rows.push_back(V());
        carrier_rows.insert(carrier_rows.begin() + static_cast<std::ptrdiff_t>(position), rows.begin(), rows.end());
        if (position <= carrier_start) {
            carrier_start += rows.size();
            carrier_stop += rows.size();
        }
    };
    auto first_row = [&](auto matches) {
        for (size_t index = 0; index < carrier_rows.size(); ++index)
            if (matches(carrier_rows[index])) return index;
        return carrier_rows.size();
    };
    if (!imported_types.empty()) {
        size_t position = first_row([](V line) { return module_symbol(line); });
        std::sort(imported_types.begin(), imported_types.end());
        insert_rows(position, imported_types);
    }
    if (!imported_globals.empty())
        insert_rows(first_row([](V line) { return define_symbol(line).found() || declare_symbol(line).found(); }),
                    imported_globals);
    size_t insertion = first_row([](V line) {
        V digits;
        return starts(line, "attributes ") || starts(line, "!llvm.") || starts(line, "!dx.") || metadata_number(line, digits);
    });
    if (!additions.empty()) insert_rows(insertion, additions);
    carrier_rows.erase(carrier_rows.begin() + static_cast<std::ptrdiff_t>(carrier_start),
                       carrier_rows.begin() + static_cast<std::ptrdiff_t>(carrier_stop));
    carrier_rows.insert(carrier_rows.begin() + static_cast<std::ptrdiff_t>(carrier_start), entry.begin(), entry.end());
    carrier_rows.insert(carrier_rows.end(), metadata_additions.begin(), metadata_additions.end());
    carrier_rows.erase(std::remove_if(carrier_rows.begin(), carrier_rows.end(),
                                      [](V line) { return starts(line, "!dx.viewIdState = "); }),
                       carrier_rows.end());
    if (native_view_state) carrier_rows.push_back(remap_metadata(*native_view_state));

    Ref carrier_operands(attribute(carrier_metadata.get(), "operands"));
    Ref entry_operands(check(PySequence_List(carrier_operands.get())));
    for (Py_ssize_t index : {2, 4}) {
        Ref operand(item_of(native_operands.get(), index));
        S text = read(operand.get());
        Ref remapped(make(remap_metadata(V(text))));
        check(PySequence_SetItem(entry_operands.get(), index, remapped.get()));
    }
    Ref reference(attribute(carrier_metadata.get(), "reference"));
    replace_record(carrier_rows, read(reference.get()), [&](S &line) { append_joined(line, entry_operands.get()); }, arena);
    Ref definitions(attribute(carrier_metadata.get(), "definitions"));
    Ref model_key = string("!dx.shaderModel");
    Ref nothing(check(PyTuple_New(0)));
    Ref model_reference(check(PyObject_CallMethod(definitions.get(), "get", "OO", model_key.get(), nothing.get())));
    Py_ssize_t model_size = PyObject_Size(model_reference.get());
    if (model_size < 0) throw Fail();
    if (model_size != 1) raise_graph("DXIL carrier has no shader-model declaration.");
    Ref target(attribute(source_contract, "target"));
    Ref target_parts(check(PyObject_CallMethod(target.get(), "split", "s", "_")));
    Ref stage(item_of(target_parts.get(), 0));
    S model(U"!\"");
    append_formatted(model, stage.get());
    append_literal(model, "\", i32 ");
    Ref major(attribute(source_contract, "model_major"));
    append_formatted(model, major.get());
    append_literal(model, ", i32 ");
    Ref minor(attribute(source_contract, "model_minor"));
    append_formatted(model, minor.get());
    Ref model_name(item_of(model_reference.get(), 0));
    replace_record(carrier_rows, read(model_name.get()), [&](S &line) { line += model; }, arena);

    VSet used_functions;
    for (V line : carrier_rows)
        if (!define_symbol(line).found() && !declare_symbol(line).found())
            calls_each(line, [&](V name) { used_functions.insert(name); });
    carrier_rows.erase(std::remove_if(carrier_rows.begin(), carrier_rows.end(), [&](V line) {
                           Span name = declare_symbol(line);
                           return name.found() && !used_functions.count(line.substr(name.start, name.end - name.start));
                       }),
                       carrier_rows.end());
    return joined(carrier_rows);
}

struct EntryRecord {
    V reference;
    std::vector<V> operands;
    VMap<std::vector<V>> definitions;
    const std::vector<V> *find(V name) const {
        auto found = definitions.find(name);
        return found == definitions.end() ? nullptr : &found->second;
    }
};
EntryRecord entry_of(const std::vector<V> &rows) {
    EntryRecord entry;
    for (V line : rows) {
        if (find_char(line, '!') == NPOS || !starts(lstrip(line), "!")) continue;
        V text = strip(line);
        size_t j = 1;
        while (j < text.size() && (word(text[j]) || text[j] == '.')) ++j;
        if (j == 1 || !at(text, j, " = !{") || text.size() < j + 6 || text.back() != '}') continue;
        entry.definitions[text.substr(0, j)] = metadata_parts(text.substr(j + 3));
    }
    VSet payloads;
    for (V line : rows) {
        if (contains(line, "@dx.op.getMeshPayload.")) {
            for (size_t p = 0; p < line.size(); ++p) {
                size_t q = at(line, p, "call ") ? p + 5 : at(line, p, "declare ") ? p + 8 : NPOS;
                if (q == NPOS || q >= line.size() || line[q] != '%') continue;
                size_t end = q + 1;
                while (end < line.size() && storage_char(line[end])) ++end;
                if (end > q + 1 && at(line, end, "* @dx.op.getMeshPayload.")) {
                    payloads.insert(line.substr(q, end - q));
                    break;
                }
            }
        }
        if (contains(line, "@dx.op.dispatchMesh.")) {
            bool done = false;
            for (size_t p = find(line, "@dx.op.dispatchMesh."); p != NPOS && !done; p = find(line, "@dx.op.dispatchMesh.", p + 1)) {
                size_t q = p + 20, open = find_char(line, '(', q);
                if (open == NPOS || open == q) continue;
                for (size_t k = open + 1; k <= line.size(); ++k) {
                    if (k > open + 1 && line[k - 1] == '\n') break;
                    if (!at(line, k, ", %")) continue;
                    size_t start = k + 3, end = start;
                    while (end < line.size() && storage_char(line[end])) ++end;
                    if (end > start && at(line, end, " addrspace(3)*")) {
                        payloads.insert(line.substr(k + 2, end - (k + 2)));
                        done = true;
                        break;
                    }
                }
            }
        }
    }
    const std::vector<V> *entries = entry.find(V(U"!dx.entryPoints"));
    bool valid = entries && !entries->empty();
    for (size_t index = 0; valid && index < entries->size(); ++index) {
        const std::vector<V> *record = entry.find((*entries)[index]);
        valid = record && record->size() == 5;
    }
    if (!valid) raise_graph("DXIL has invalid entry metadata records.");
    if (payloads.size() > 1) raise_graph("One mesh/amplification entry cannot declare conflicting payload types.");
    if (entries->size() != 1) raise_graph("Material DXIL needs one entry metadata record.");
    entry.reference = (*entries)[0];
    entry.operands = *entry.find(entry.reference);
    return entry;
}

void define_record(VMap<V> &definitions, PyObject *reference, PyObject *row, Arena &arena) {
    S reference_text = read(reference);
    S line = reference_text;
    append_literal(line, " = !{");
    append_joined(line, row);
    line.push_back('}');
    Ref number(to_int(V(reference_text).substr(std::min<size_t>(1, reference_text.size()))));
    S key;
    append_str(key, number.get());
    definitions[kept(arena, key)] = kept(arena, line);
}

void cbuffer_usage(const std::vector<V> &lines, PyObject *prepared, VMap<V> &definitions, PyObject *classes, Arena &arena) {
    static PyObject *const suffix = interned("* undef"), *const undef = interned(" undef");
    static PyObject *const tag_zero = interned("i32 0"), *const used_tag = interned("i32 9"), *const used = interned("i1 true");
    Ref entry(attribute(prepared, "reflection_metadata"));
    Ref metadata(attribute(entry.get(), "definitions"));
    Ref operands(attribute(entry.get(), "operands"));
    Ref third(item_of(operands.get(), 3));
    Ref nothing(check(PyTuple_New(0)));
    Ref resources(check(PyObject_CallMethod(metadata.get(), "get", "OO", third.get(), nothing.get())));
    Py_ssize_t resource_count = PyObject_Size(resources.get());
    if (resource_count < 0) throw Fail();
    if (resource_count != 4) return;
    Ref contract(attribute(prepared, "contract"));
    Ref buffers(check(PyDict_New()));
    {
        Ref constant_buffers(attribute(contract.get(), "constant_buffers"));
        Ref iterator(check(PyObject_GetIter(constant_buffers.get())));
        while (PyObject *item = PyIter_Next(iterator.get())) {
            Ref owned(item);
            Ref space(attribute(item, "register_space")), point(attribute(item, "bind_point"));
            Ref key(check(PyTuple_Pack(2, space.get(), point.get())));
            check(PyDict_SetItem(buffers.get(), key.get(), item));
        }
        if (PyErr_Occurred()) throw Fail();
    }
    Ref types(check(PyDict_New()));
    {
        Ref second(item_of(resources.get(), 2));
        Ref references(check(PyObject_CallMethod(metadata.get(), "get", "OO", second.get(), nothing.get())));
        Ref iterator(check(PyObject_GetIter(references.get())));
        while (PyObject *reference = PyIter_Next(iterator.get())) {
            Ref owned(reference);
            Ref row(check(PyObject_GetItem(metadata.get(), reference)));
            auto field = [&](Py_ssize_t index) {
                Ref text(item_of(row.get(), index));
                Ref parts(check(PyObject_CallMethod(text.get(), "split", nullptr)));
                Ref value(item_of(parts.get(), 1));
                return integer_of(value.get());
            };
            Ref space = field(3);
            Ref point = field(4);
            Ref key(check(PyTuple_Pack(2, space.get(), point.get())));
            Ref buffer(check(PyObject_GetItem(buffers.get(), key.get())));
            Ref first(item_of(row.get(), 1));
            Ref trimmed(check(PyObject_CallMethod(first.get(), "removesuffix", "O", suffix)));
            Ref name(check(PyNumber_Add(trimmed.get(), undef)));
            check(PyObject_SetItem(types.get(), name.get(), buffer.get()));
        }
        if (PyErr_Occurred()) throw Fail();
    }
    if (!truth(types.get())) return;

    constexpr long long DYNAMIC = -1, LARGEST = 1ll << 62;
    auto clamped = [&](V digits) {
        long long value = 0;
        for (U c : digits) {
            value = value * 10 + Py_UNICODE_TODECIMAL(c);
            if (value > LARGEST) return LARGEST;
        }
        return value;
    };
    std::map<std::pair<long long, long long>, std::vector<std::pair<long long, long long>>> reads;
    {
        Spans spans = spans_in(lines);
        ResourceTables tables;
        ConstantReads constant_reads(contract.get(), classes, tables);
        std::vector<V> body;
        for (const FunctionSpan &span : spans.items) {
            body.assign(lines.begin() + static_cast<std::ptrdiff_t>(span.start),
                        lines.begin() + static_cast<std::ptrdiff_t>(span.stop));
            std::vector<std::pair<Extraction, std::pair<long long, long long>>> extracted;
            constant_reads.read(
                body, [](const ConstantResource &) {},
                [&](const Extraction &read) {
                    const ConstantResource &item = constant_reads.resource(read.resource);
                    if (read.size == 4 && !read.dynamic && (item.point_value < 0 || item.space_value < 0))
                        raise_graph("Constant word has an invalid binding, offset or register space");
                    extracted.push_back({read, {item.space_value, item.point_value}});
                });
            VMap<bool> referenced;
            for (const auto &item : extracted) referenced.emplace(item.first.value, false);
            if (!referenced.empty())
                for (V line : body) {
                    size_t equals = find_char(line, '=');
                    V rest = equals == NPOS ? line : line.substr(equals + 1);
                    rest = rest.substr(0, std::min(rest.size(), find_char(rest, ';')));
                    ssa_each(rest, [&](size_t start, size_t end) {
                        auto found = referenced.find(rest.substr(start, end - start));
                        if (found != referenced.end()) found->second = true;
                    });
                }
            for (const auto &[read, binding] : extracted) {
                if (!referenced[read.value]) continue;
                long long offset = DYNAMIC;
                if (!read.dynamic) {
                    long long row = clamped(read.row), lane = clamped(read.lane);
                    offset = row > LARGEST / 16 || lane > LARGEST / read.size
                        ? LARGEST : std::min(row * 16 + lane * read.size, LARGEST);
                }
                reads[binding].emplace_back(offset, read.size);
            }
        }
    }

    S next_id = integer_after(integer_keys(definitions));
    auto add = [&](PyObject *operand_list) {
        V number = kept(arena, next_id);
        increment(next_id);
        S line(U"!");
        line += number;
        append_literal(line, " = !{");
        append_joined(line, operand_list);
        line.push_back('}');
        definitions[number] = kept(arena, line);
        S reference(U"!");
        reference += number;
        return Ref(make(V(reference)));
    };
    Ref annotations_key = string("!dx.typeAnnotations");
    Ref annotations(check(PyObject_CallMethod(metadata.get(), "get", "OO", annotations_key.get(), nothing.get())));
    Ref iterator(check(PyObject_GetIter(annotations.get())));
    while (PyObject *reference = PyIter_Next(iterator.get())) {
        Ref owned(reference);
        Ref record(check(PyObject_GetItem(metadata.get(), reference)));
        Ref row(check(PySequence_List(record.get())));
        Ref head(item_of(row.get(), 0));
        if (compare(head.get(), tag_zero, Py_NE)) continue;
        Py_ssize_t length = PyList_GET_SIZE(row.get());
        for (Py_ssize_t index = 1; index < length; index += 2) {
            Ref name(item_of(row.get(), index));
            PyObject *buffer = PyDict_GetItemWithError(types.get(), name.get());
            if (!buffer && PyErr_Occurred()) throw Fail();
            if (!buffer) continue;
            Ref keep_buffer(Py_NewRef(buffer));
            Ref aggregate_reference(item_of(row.get(), index + 1));
            Ref aggregate_record(check(PyObject_GetItem(metadata.get(), aggregate_reference.get())));
            Ref aggregate(check(PySequence_List(aggregate_record.get())));
            Ref variables(attribute(buffer, "variables"));
            Py_ssize_t variable_count = PyObject_Size(variables.get());
            if (variable_count < 0) throw Fail();
            if (PyList_GET_SIZE(aggregate.get()) != variable_count + 1) {
                Ref buffer_name(attribute(buffer, "name"));
                Ref shown(check(PyObject_Format(buffer_name.get(), constants().empty)));
                raise_graph_format("DXIL reflection fields disagree with %U.", shown.get());
            }
            Ref space_value(attribute(buffer, "register_space")), point_value(attribute(buffer, "bind_point"));
            long long space = PyLong_AsLongLong(space_value.get()), point = PyLong_AsLongLong(point_value.get());
            if (PyErr_Occurred()) throw Fail();
            auto found_reads = reads.find({space, point});
            Ref variable_iterator(check(PyObject_GetIter(variables.get())));
            Py_ssize_t field_index = 1;
            while (PyObject *variable = PyIter_Next(variable_iterator.get())) {
                Ref owned_variable(variable);
                Ref field_reference(item_of(aggregate.get(), field_index));
                Ref annotation(check(PyObject_GetItem(metadata.get(), field_reference.get())));
                Ref fields(check(PyList_New(0)));
                Py_ssize_t annotation_size = PyObject_Size(annotation.get());
                if (annotation_size < 0) throw Fail();
                for (Py_ssize_t pair = 0; pair + 1 < annotation_size; pair += 2) {
                    Ref tag(item_of(annotation.get(), pair)), value(item_of(annotation.get(), pair + 1));
                    if (!compare(tag.get(), used_tag, Py_NE)) continue;
                    check(PyList_Append(fields.get(), tag.get()));
                    check(PyList_Append(fields.get(), value.get()));
                }
                bool is_used = false;
                if (found_reads != reads.end() && !found_reads->second.empty()) {
                    Ref offset_value(attribute(variable, "byte_offset")), size_value(attribute(variable, "byte_size"));
                    long long offset = PyLong_AsLongLong(offset_value.get()), size = PyLong_AsLongLong(size_value.get());
                    if (PyErr_Occurred()) throw Fail();
                    for (const auto &[start, extent] : found_reads->second)
                        if (start == DYNAMIC || (start < offset + size && offset < start + extent)) {
                            is_used = true;
                            break;
                        }
                }
                if (is_used) {
                    check(PyList_Append(fields.get(), used_tag));
                    check(PyList_Append(fields.get(), used));
                }
                Ref added = add(fields.get());
                check(PyList_SetItem(aggregate.get(), field_index, added.release()));
                ++field_index;
            }
            if (PyErr_Occurred()) throw Fail();
            Ref added = add(aggregate.get());
            check(PySequence_SetItem(row.get(), index + 1, added.get()));
        }
        define_record(definitions, reference, row.get(), arena);
    }
    if (PyErr_Occurred()) throw Fail();
}

[[noreturn]] void raise_key(V digits) {
    Ref key(to_int(digits));
    PyErr_SetObject(PyExc_KeyError, key.get());
    throw Fail();
}

PyObject *attach_reflection(PyObject *assembly_object, PyObject *prepared, PyObject *classes) {
    Arena arena;
    Ref reflection(attribute(prepared, "reflection"));
    Rows reflected(reflection.get()), assembly(assembly_object);
    std::vector<V> lines = assembly.rows;
    VMap<V> definitions;
    for (V line : reflected.rows) {
        V digits;
        if (metadata_number(line, digits)) definitions[integer_text(digits, arena)] = line;
    }
    cbuffer_usage(lines, prepared, definitions, classes, arena);
    Ref reflected_entry(attribute(prepared, "reflection_metadata"));
    EntryRecord finalized = entry_of(lines);
    Ref nothing(check(PyTuple_New(0)));
    Ref reflected_definitions(attribute(reflected_entry.get(), "definitions"));
    Ref reflected_operands(attribute(reflected_entry.get(), "operands"));
    Ref reflected_third(item_of(reflected_operands.get(), 3));
    Ref reflected_resources(check(PyObject_CallMethod(reflected_definitions.get(), "get", "OO", reflected_third.get(), nothing.get())));
    const std::vector<V> *finalized_resources = finalized.find(finalized.operands[3]);
    Py_ssize_t reflected_size = PyObject_Size(reflected_resources.get());
    if (reflected_size < 0) throw Fail();
    if (reflected_size == 4 && finalized_resources && finalized_resources->size() == 4) {
        std::vector<std::pair<std::vector<V>, const std::vector<V> *>> finalized_uavs;
        if (const std::vector<V> *references = finalized.find((*finalized_resources)[1]))
            for (V reference : *references) {
                const std::vector<V> *row = finalized.find(reference);
                if (!row) {
                    Ref key(make(reference));
                    PyErr_SetObject(PyExc_KeyError, key.get());
                    throw Fail();
                }
                if (row->size() != 11) raise_graph("Finalized DXIL UAV has an invalid resource schema.");
                std::vector<V> key(row->begin() + 3, row->begin() + 6);
                auto existing = std::find_if(finalized_uavs.begin(), finalized_uavs.end(), [&](const auto &item) { return item.first == key; });
                if (existing != finalized_uavs.end()) existing->second = row;
                else finalized_uavs.emplace_back(std::move(key), row);
            }
        Ref reflected_second(item_of(reflected_resources.get(), 1));
        Ref reflected_references(check(PyObject_CallMethod(reflected_definitions.get(), "get", "OO", reflected_second.get(), nothing.get())));
        Ref reflected_iterator(check(PyObject_GetIter(reflected_references.get())));
        while (PyObject *reference = PyIter_Next(reflected_iterator.get())) {
            Ref owned(reference);
            Ref record(check(PyObject_GetItem(reflected_definitions.get(), reference)));
            Ref row(check(PySequence_List(record.get())));
            Ref slice(check(PySequence_GetSlice(row.get(), 3, 6)));
            std::vector<S> key_texts;
            for (Py_ssize_t index = 0; index < PyList_GET_SIZE(slice.get()); ++index) key_texts.push_back(read(PyList_GET_ITEM(slice.get(), index)));
            std::vector<V> key(key_texts.begin(), key_texts.end());
            auto found = std::find_if(finalized_uavs.begin(), finalized_uavs.end(), [&](const auto &item) { return item.first == key; });
            if (found == finalized_uavs.end()) continue;
            const std::vector<V> &native = *found->second;
            Ref renamed(check(PyList_New(0)));
            for (size_t token_index = 6; token_index < native.size(); ++token_index) {
                V view = native[token_index];
                S out;
                size_t last = 0;
                numbered_each(view, '!', [&](size_t position, size_t start, size_t end) {
                    out.append(view.substr(last, position - last));
                    append_literal(out, "!native.");
                    out.append(view.substr(start, end - start));
                    last = end;
                });
                out.append(view.substr(last));
                Ref value(make(V(out)));
                check(PyList_Append(renamed.get(), value.get()));
            }
            if (PyErr_Occurred()) throw Fail();
            Py_ssize_t length = PyList_GET_SIZE(row.get());
            check(PyList_SetSlice(row.get(), std::min<Py_ssize_t>(6, length), length, renamed.get()));
            define_record(definitions, reference, row.get(), arena);
        }
        if (PyErr_Occurred()) throw Fail();
    }
    Ordered<V> named;
    for (V line : reflected.rows) {
        if (!starts(line, "!dx.resources =") && !starts(line, "!dx.typeAnnotations =")) continue;
        size_t split = line.find(U" = ");
        named.at(split == NPOS ? line : line.substr(0, split)) = line;
    }
    S annotations_text;
    if (V *annotations = named.find(V(U"!dx.typeAnnotations"))) {
        V text = *annotations;
        annotations_text = U"!dx.typeAnnotations = !{";
        bool first = true;
        numbered_each(text, '!', [&](size_t, size_t start, size_t end) {
            V digits = text.substr(start, end - start);
            auto found = definitions.find(integer_text(digits, arena));
            if (found == definitions.end()) raise_key(digits);
            V definition = found->second, number;
            if (!metadata_number(definition, number) || !at(definition, 1 + number.size(), " = !{i32 0,")) return;
            if (!first) append_literal(annotations_text, ", ");
            first = false;
            annotations_text.push_back('!');
            annotations_text += digits;
        });
        annotations_text.push_back('}');
        *annotations = V(annotations_text);
    }
    VSet closure;
    std::vector<V> pending;
    for (const auto &item : named.items)
        numbered_each(item.second, '!', [&](size_t, size_t start, size_t end) {
            pending.push_back(integer_text(item.second.substr(start, end - start), arena));
        });
    while (!pending.empty()) {
        V number = pending.back();
        pending.pop_back();
        if (closure.count(number)) continue;
        auto found = definitions.find(number);
        if (found == definitions.end()) raise_graph("DXC reflection references missing type metadata.");
        closure.insert(number);
        V definition = found->second;
        size_t equals = find_char(definition, '=');
        if (equals == NPOS) raise_index();
        V rest = definition.substr(equals + 1);
        numbered_each(rest, '!', [&](size_t, size_t start, size_t end) {
            pending.push_back(integer_text(rest.substr(start, end - start), arena));
        });
    }
    std::vector<V> line_ids;
    for (V line : lines) {
        V digits;
        if (metadata_number(line, digits)) line_ids.push_back(integer_text(digits, arena));
    }
    S next_id = integer_after(line_ids);
    std::vector<V> closure_ids(closure.begin(), closure.end());
    sort_integers(closure_ids);
    VMap<V> ids;
    for (V number : closure_ids) {
        ids[number] = kept(arena, next_id);
        increment(next_id);
    }
    auto remap = [&](V line) {
        V numbered = numbers_renamed(line, '!', arena, [&](S &out, V digits) {
            auto found = ids.find(integer_text(digits, arena));
            if (found == ids.end()) raise_key(digits);
            out += found->second;
        });
        if (find(numbered, "!native.") == NPOS) return numbered;
        S out;
        size_t last = 0, p = 0;
        while ((p = find(numbered, "!native.", p)) != NPOS) {
            size_t end = decimals(numbered, p + 8);
            if (end == NPOS) {
                ++p;
                continue;
            }
            out.append(numbered.substr(last, p - last));
            out.push_back('!');
            out.append(numbered.substr(p + 8, end - p - 8));
            last = p = end;
        }
        out.append(numbered.substr(last));
        return kept(arena, out);
    };
    if (V *resources = named.find(V(U"!dx.resources"))) {
        V text = *resources;
        size_t start = NPOS, end = NPOS;
        numbered_each(text, '!', [&](size_t, size_t digits_start, size_t digits_end) {
            if (start != NPOS) return;
            start = digits_start;
            end = digits_end;
        });
        if (start != NPOS) {
            V digits = text.substr(start, end - start);
            auto found = ids.find(integer_text(digits, arena));
            if (found == ids.end()) raise_key(digits);
            S root(U"!");
            root += found->second;
            std::vector<V> entry_operands = finalized.operands;
            entry_operands[3] = V(root);
            replace_record(lines, S(finalized.reference), [&](S &line) {
                for (size_t index = 0; index < entry_operands.size(); ++index) {
                    if (index) append_literal(line, ", ");
                    line += entry_operands[index];
                }
            }, arena);
        }
    }
    Ordered<V> types, reflected_types;
    for (V line : lines) {
        size_t end = symbol_end(line, '%', true);
        if (end != NPOS) types.at(line.substr(0, end)) = line;
    }
    for (V line : reflected.rows) {
        size_t end = symbol_end(line, '%', true);
        if (end != NPOS) reflected_types.at(line.substr(0, end)) = line;
    }
    std::vector<V> imported;
    for (const auto &item : reflected_types.items) {
        V *existing = types.find(item.first);
        if (existing && *existing != item.second) {
            Ref name(make(item.first));
            raise_graph_format("DXC reflection has a conflicting type %U.", name.get());
        }
        if (!existing) imported.push_back(item.second);
    }
    size_t insertion = lines.size();
    for (size_t index = 0; index < lines.size(); ++index)
        if (module_symbol(lines[index])) {
            insertion = index;
            break;
        }
    lines.insert(lines.begin() + static_cast<std::ptrdiff_t>(insertion), imported.begin(), imported.end());
    lines.erase(std::remove_if(lines.begin(), lines.end(), [&](V line) {
                    if (line.empty() || line[0] != '!') return false;
                    size_t split = line.find(U" = ");
                    return named.find(split == NPOS ? line : line.substr(0, split)) != nullptr;
                }),
                lines.end());
    for (const auto &item : named.items) lines.push_back(remap(item.second));
    for (V number : closure_ids) lines.push_back(remap(definitions.at(number)));
    return joined(lines);
}

PyObject *always_inline(PyObject *text) {
    static PyObject *const needle = interned("noinline");
    Py_ssize_t size = PyUnicode_GET_LENGTH(text);
    int kind = PyUnicode_KIND(text);
    const void *data = PyUnicode_DATA(text);
    auto word_at = [&](Py_ssize_t index) { return index >= 0 && index < size && word(PyUnicode_READ(kind, data, index)); };
    PyUnicodeWriter *writer = nullptr;
    Py_ssize_t last = 0, from = 0;
    while (true) {
        Py_ssize_t found = PyUnicode_Find(text, needle, from, size, 1);
        if (found == -2) throw Fail();
        if (found < 0) break;
        from = found + 1;
        if (word_at(found - 1) || word_at(found + 8)) continue;
        if (!writer && !(writer = PyUnicodeWriter_Create(size + 64))) throw Fail();
        if (PyUnicodeWriter_WriteSubstring(writer, text, last, found) < 0
            || PyUnicodeWriter_WriteUTF8(writer, "alwaysinline", 12) < 0) {
            PyUnicodeWriter_Discard(writer);
            throw Fail();
        }
        last = from = found + 8;
    }
    if (!writer) return Py_NewRef(text);
    if (PyUnicodeWriter_WriteSubstring(writer, text, last, size) < 0) {
        PyUnicodeWriter_Discard(writer);
        throw Fail();
    }
    return check(PyUnicodeWriter_Finish(writer));
}

std::vector<V> rows_of(PyObject *lines, Texts &texts) {
    texts = read_lines(lines);
    std::vector<V> rows;
    rows.reserve(texts.size());
    for (size_t index = 0; index < texts.size(); ++index) rows.push_back(texts[index]);
    return rows;
}

PyObject *markers_object(const Ordered<std::vector<V>> &markers) {
    Ref result(check(PyDict_New()));
    for (const auto &[key, code] : markers.items) {
        Ref name(make(key)), lines(row_list(code));
        check(PyDict_SetItem(result.get(), name.get(), lines.get()));
    }
    return result.release();
}

PyObject *applied_plan(PyObject *args) {
    PyObject *lines, *carrier_lines, *carrier_entry, *plan, *outputs, *value_width;
    long long start;
    if (!PyArg_ParseTuple(args, "OLOOOOO:apply_surface_plan", &lines, &start, &carrier_lines, &carrier_entry, &plan,
                          &outputs, &value_width))
        throw Fail();
    Arena arena;
    Texts line_texts, carrier_texts, entry_texts;
    std::vector<V> rows = rows_of(lines, line_texts), carrier = rows_of(carrier_lines, carrier_texts);
    std::vector<V> entry = rows_of(carrier_entry, entry_texts), result;
    Ordered<std::vector<V>> markers;
    surface_plan(result, markers, rows, start, carrier, nullptr, entry, plan, outputs, value_width, arena);
    Ref result_list(row_list(result)), marker_dict(markers_object(markers));
    return check(PyTuple_Pack(2, result_list.get(), marker_dict.get()));
}

PyObject *placed_code(PyObject *args) {
    PyObject *lines, *markers_dict;
    if (!PyArg_ParseTuple(args, "OO!:place_surface_code", &lines, &PyDict_Type, &markers_dict)) throw Fail();
    Arena arena;
    Texts texts;
    std::vector<V> rows = rows_of(lines, texts);
    Ordered<std::vector<V>> markers;
    Py_ssize_t position = 0;
    PyObject *key, *value;
    S text;
    while (PyDict_Next(markers_dict, &position, &key, &value)) {
        text.clear();
        append(text, key);
        std::vector<V> code;
        read_rows(value, code, arena);
        markers.at(kept(arena, text)) = std::move(code);
    }
    place_markers(rows, markers);
    return row_list(rows);
}

PyObject *handles_rewritten(PyObject *args, bool modern) {
    PyObject *lines, *source, *candidate, *overrides, *classes, *numbers;
    if (!PyArg_ParseTuple(args, "OOOOO!O:rewrite_handles", &lines, &source, &candidate, &overrides, &PyDict_Type,
                          &classes, &numbers))
        throw Fail();
    Arena arena;
    Texts texts = read_lines(lines);
    std::vector<V> body;
    body.reserve(texts.size());
    for (size_t index = 0; index < texts.size(); ++index) body.push_back(texts[index]);
    ResourceTables tables;
    Handles handles(source, candidate, overrides, classes, numbers, tables);
    if (modern) modern_handles(body, handles, arena);
    else legacy_handles(body, handles, arena);
    return row_list(body);
}

template <typename Body> PyObject *guarded(Body body) {
    try {
        return body();
    } catch (const Fail &) {
        return nullptr;
    } catch (const std::bad_alloc &) {
        PyErr_NoMemory();
        return nullptr;
    } catch (const std::out_of_range &) {
        PyErr_SetString(PyExc_SystemError, "fastdxiltext lookup failed");
        return nullptr;
    }
}

PyObject *py_metadata_operands(PyObject *, PyObject *text) {
    return guarded([&] {
        Strings strings;
        S value = read(text);
        return operands_tuple(value, strings);
    });
}

PyObject *py_metadata_definitions(PyObject *, PyObject *lines) {
    return guarded([&] {
        Strings strings;
        Texts read_back = read_lines(lines, metadata_line);
        return definitions_of(read_back, strings);
    });
}

PyObject *py_entry_tables(PyObject *, PyObject *lines) { return guarded([&] { return entry_tables(lines); }); }

PyObject *py_function_spans(PyObject *, PyObject *lines) {
    return guarded([&] {
        Strings strings;
        Texts read_back = read_lines(lines, span_line);
        return spans_of(read_back, strings);
    });
}

PyObject *py_call_access(PyObject *, PyObject *lines) {
    return guarded([&] {
        Strings strings;
        Texts read_back = read_lines(lines, access_line);
        Accesses access = access_of(read_back);
        Ref result(check(PyDict_New()));
        for (const auto &item : access.order) {
            Ref kind(interned(ACCESS_NAMES[item.second]));
            check(PyDict_SetItem(result.get(), strings.get(item.first), kind.get()));
        }
        return result.release();
    });
}

PyObject *py_statements(PyObject *, PyObject *lines) {
    return guarded([&] {
        Strings strings;
        Texts read_back = read_lines(lines);
        Statements statements;
        statements_of(read_back, statements);
        Ref result(check(PyList_New(static_cast<Py_ssize_t>(statements.rows.size()))));
        for (size_t index = 0; index < statements.rows.size(); ++index) {
            const Statement &row = statements.rows[index];
            Ref line(check(PyLong_FromSize_t(row.line)));
            Ref text(make(row.text));
            PyList_SET_ITEM(result.get(), static_cast<Py_ssize_t>(index),
                            check(PyTuple_Pack(3, strings.get(statements.blocks[row.block]), line.get(), text.get())));
        }
        return result.release();
    });
}

PyObject *py_control_flow(PyObject *, PyObject *rows) {
    return guarded([&] {
        Strings strings;
        Statements statements;
        Buffer buffer;
        read_statements(rows, statements, buffer);
        return control_object(control_of(statements), strings);
    });
}

PyObject *py_flow(PyObject *, PyObject *args) {
    PyObject *lines, *statements, *outputs, *handles, *value_type, *branches;
    Py_ssize_t begin;
    if (!PyArg_ParseTuple(args, "OnOOOOO:flow", &lines, &begin, &statements, &outputs, &handles, &value_type, &branches))
        return nullptr;
    return guarded([&] { return flow(lines, begin, statements, outputs, handles, value_type, branches); });
}

PyObject *py_material_sites(PyObject *, PyObject *args) {
    PyObject *lines, *statements, *wanted;
    Py_ssize_t base;
    if (!PyArg_ParseTuple(args, "OOnO:material_sites", &lines, &statements, &base, &wanted)) return nullptr;
    return guarded([&] { return material_sites(lines, statements, base, wanted); });
}

PyObject *py_recovery_records(PyObject *, PyObject *args) {
    PyObject *assembly, *record_type, *executable, *prefixes, *flags;
    if (!PyArg_ParseTuple(args, "UO!OOO:recovery_records", &assembly, &PyType_Type, &record_type, &executable,
                          &prefixes, &flags))
        return nullptr;
    return guarded([&] { return recovery_records(assembly, record_type, executable, prefixes, flags); });
}

PyObject *py_record_accesses(PyObject *, PyObject *args) {
    PyObject *lines, *describe, *by_id, *by_binding, *classes, *inputs, *candidates, *word_type, *access_type;
    int spline_entry, speedtree_entry, wind_arena;
    if (!PyArg_ParseTuple(args, "OOO!O!O!O!pppOO!O!:record_accesses", &lines, &describe, &PyDict_Type, &by_id,
                          &PyDict_Type, &by_binding, &PyDict_Type, &classes, &PySet_Type, &inputs, &spline_entry,
                          &speedtree_entry, &wind_arena, &candidates, &PyType_Type, &word_type, &PyType_Type,
                          &access_type))
        return nullptr;
    if (candidates != Py_None && !PyList_Check(candidates)) {
        PyErr_SetString(PyExc_TypeError, "record_accesses' texture candidates are a list or None");
        return nullptr;
    }
    return guarded([&] {
        RecordReader reader(describe, by_id, by_binding, classes, inputs, spline_entry, speedtree_entry, wind_arena,
                            candidates);
        return reader.read(lines, word_type, access_type);
    });
}

PyObject *py_value_names(PyObject *, PyObject *args) {
    PyObject *lines, *inputs, *by_id, *by_binding, *classes, *sampler_heap, *resource_heap;
    if (!PyArg_ParseTuple(args, "OO!O!O!O!UU:value_names", &lines, &PyDict_Type, &inputs, &PyDict_Type, &by_id,
                          &PyDict_Type, &by_binding, &PyDict_Type, &classes, &sampler_heap, &resource_heap))
        return nullptr;
    return guarded([&] { return value_names(lines, inputs, by_id, by_binding, classes, sampler_heap, resource_heap); });
}

PyObject *py_constant_words(PyObject *, PyObject *lines) { return guarded([&] { return constant_words(lines); }); }

PyObject *py_texture_handles(PyObject *, PyObject *args) {
    PyObject *lines, *sources, *by_id, *by_binding, *classes, *describe;
    if (!PyArg_ParseTuple(args, "OO!O!O!O!O:texture_handles", &lines, &PyDict_Type, &sources, &PyDict_Type, &by_id,
                          &PyDict_Type, &by_binding, &PyDict_Type, &classes, &describe))
        return nullptr;
    return guarded([&] { return texture_handles(lines, sources, by_id, by_binding, classes, describe); });
}

PyObject *py_merge_entry(PyObject *, PyObject *args) {
    PyObject *original, *carrier, *entry_point, *native, *carrier_metadata, *indexed_reads, *source, *candidate;
    PyObject *overrides, *plan, *steps, *hooks;
    if (!PyArg_ParseTuple(args, "UUUOOOOOOOOO!:merge_entry", &original, &carrier, &entry_point, &native,
                          &carrier_metadata, &indexed_reads, &source, &candidate, &overrides, &plan, &steps,
                          &PyTuple_Type, &hooks))
        return nullptr;
    return guarded([&] {
        return merge_entry(original, carrier, entry_point, native, carrier_metadata, indexed_reads, source, candidate,
                           overrides, plan, steps, hooks);
    });
}

PyObject *py_attach_reflection(PyObject *, PyObject *args) {
    PyObject *assembly, *prepared, *classes;
    if (!PyArg_ParseTuple(args, "UOO!:attach_reflection", &assembly, &prepared, &PyDict_Type, &classes)) return nullptr;
    return guarded([&] { return attach_reflection(assembly, prepared, classes); });
}

PyObject *py_legacy_handles(PyObject *, PyObject *args) { return guarded([&] { return handles_rewritten(args, false); }); }

PyObject *py_modern_handles(PyObject *, PyObject *args) { return guarded([&] { return handles_rewritten(args, true); }); }

PyObject *py_constant_extracts(PyObject *, PyObject *args) {
    PyObject *lines, *contract, *classes, *bindings, *extracts, *ranges;
    if (!PyArg_ParseTuple(args, "OOO!OOO:constant_extracts", &lines, &contract, &PyDict_Type, &classes, &bindings,
                          &extracts, &ranges))
        return nullptr;
    if ((bindings != Py_None && !PySet_Check(bindings)) || (extracts != Py_None && !PyDict_Check(extracts))
        || (ranges != Py_None && !PyDict_Check(ranges))) {
        PyErr_SetString(PyExc_TypeError, "constant_extracts' dynamic bindings are a set, its extracts and ranges dicts");
        return nullptr;
    }
    return guarded([&] { return constant_extracts(lines, contract, classes, bindings, extracts, ranges); });
}

PyObject *py_always_inline(PyObject *, PyObject *text) {
    if (!PyUnicode_Check(text)) {
        PyErr_Format(PyExc_TypeError, "expected string or bytes-like object, got '%.200s'", Py_TYPE(text)->tp_name);
        return nullptr;
    }
    return guarded([&] { return always_inline(text); });
}

PyObject *py_apply_surface_plan(PyObject *, PyObject *args) { return guarded([&] { return applied_plan(args); }); }

PyObject *py_place_surface_code(PyObject *, PyObject *args) { return guarded([&] { return placed_code(args); }); }

PyMethodDef methods[] = {
    {"value_names", py_value_names, METH_VARARGS, "surface_data._dxil_sources' names"},
    {"constant_words", py_constant_words, METH_O, "surface_data._words' constant buffer words"},
    {"texture_handles", py_texture_handles, METH_VARARGS, "re_engine_bindless_material.texture_handles"},
    {"metadata_operands", py_metadata_operands, METH_O, "dxil_metadata.metadata_operands"},
    {"metadata_definitions", py_metadata_definitions, METH_O, "dxil_metadata.metadata_definitions"},
    {"entry_tables", py_entry_tables, METH_O, "(definitions, storage types, payload types) of DxilEntryMetadata"},
    {"function_spans", py_function_spans, METH_O, "re_engine_dxil_authoring._function_spans"},
    {"call_access", py_call_access, METH_O, "surface_flow_dxil.call_access"},
    {"statements", py_statements, METH_O, "surface_flow_dxil._statements, as a list"},
    {"control_flow", py_control_flow, METH_O, "surface_flow_dxil._control_flow"},
    {"flow", py_flow, METH_VARARGS, "dxil_flow's statement walk"},
    {"material_sites", py_material_sites, METH_VARARGS, "material_records' sites, numbers and dominated blocks"},
    {"recovery_records", py_recovery_records, METH_VARARGS, "parse_re_engine_dxil_assembly's records"},
    {"record_accesses", py_record_accesses, METH_VARARGS, "re_engine_bindless_material._record_accesses' reader"},
    {"merge_entry", py_merge_entry, METH_VARARGS, "re_engine_dxil_authoring._merge_entry"},
    {"attach_reflection", py_attach_reflection, METH_VARARGS, "re_engine_dxil_authoring._attach_reflection"},
    {"legacy_handles", py_legacy_handles, METH_VARARGS, "re_engine_dxil_authoring._rewrite_legacy_handles"},
    {"modern_handles", py_modern_handles, METH_VARARGS, "re_engine_dxil_authoring._rewrite_modern_handles"},
    {"constant_extracts", py_constant_extracts, METH_VARARGS, "re_engine_dxil_authoring._constant_extracts"},
    {"always_inline", py_always_inline, METH_O, "re.sub(r'\\bnoinline\\b', 'alwaysinline', text) for _execute_dxil_job"},
    {"apply_surface_plan", py_apply_surface_plan, METH_VARARGS, "surface_splice_dxil.apply_surface_plan"},
    {"place_surface_code", py_place_surface_code, METH_VARARGS, "surface_splice_dxil.place_surface_code"},
    {nullptr, nullptr, 0, nullptr},
};

PyModuleDef module = {PyModuleDef_HEAD_INIT, "fastdxiltext", nullptr, -1, methods};

}

PyMODINIT_FUNC PyInit_fastdxiltext() { return PyModule_Create(&module); }
