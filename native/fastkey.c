#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <math.h>
#include <stdint.h>
#include <string.h>

typedef struct {
    PyObject *record_schema;
    PyObject *is_dataclass, *fields, *field_marker, *field_type, *params_type;
    PyObject *mapping, *refused;
} Rules;

typedef struct {
    Rules rules;
    PyObject *update;
    char *data;
    Py_ssize_t size, capacity, limit;
    PyObject *known;
    int metadata;
    PyObject **active;
    Py_ssize_t depth, slots;
} Writer;

static PyObject *s_getattribute, *s_sizeof, *s_fields, *s_params, *s_module, *s_qualname, *s_frozen, *s_field_type,
    *s_name, *s_items, *s_release, *s_dot, *object_getattribute, *object_sizeof;

static int parse_rules(PyObject *rules, Rules *out) {
    return PyArg_ParseTuple(rules, "OOOOO!O!OO:rules", &out->record_schema, &out->is_dataclass, &out->fields,
                            &out->field_marker, &PyType_Type, &out->field_type, &PyType_Type, &out->params_type,
                            &out->mapping, &out->refused);
}

static int refuse(Writer *w, const char *message) {
    PyErr_SetString(w->rules.refused, message);
    return -1;
}

static int feed(Writer *w, const char *data, Py_ssize_t size, PyObject *owner) {
    PyObject *chunk = owner ? Py_NewRef(owner) : PyMemoryView_FromMemory((char *)data, size, PyBUF_READ);
    if (!chunk) return -1;
    PyObject *done = PyObject_CallOneArg(w->update, chunk);
    if (done && !owner) {
        Py_DECREF(done);
        done = PyObject_CallMethodNoArgs(chunk, s_release);
    }
    Py_DECREF(chunk);
    Py_XDECREF(done);
    return done ? 0 : -1;
}

static int flush(Writer *w) {
    Py_ssize_t size = w->size;
    w->size = 0;
    return size ? feed(w, w->data, size, NULL) : 0;
}

static int put(Writer *w, const void *bytes, Py_ssize_t count) {
    if (!count) return 0;
    if (w->size + count > w->capacity) {
        Py_ssize_t capacity = w->capacity ? w->capacity : 4096;
        while (capacity < w->size + count) capacity *= 2;
        char *data = PyMem_Realloc(w->data, (size_t)capacity);
        if (!data) { PyErr_NoMemory(); return -1; }
        w->data = data;
        w->capacity = capacity;
    }
    memcpy(w->data + w->size, bytes, (size_t)count);
    w->size += count;
    return w->size >= w->limit ? flush(w) : 0;
}

static int put_count(Writer *w, const char *tag, Py_ssize_t tag_size, uint64_t count) {
    unsigned char raw[10];
    memcpy(raw, tag, (size_t)tag_size);
    for (int index = 7; index >= 0; index--, count >>= 8) raw[tag_size + index] = (unsigned char)(count & 0xFF);
    return put(w, raw, tag_size + 8);
}

static int put_raw(Writer *w, const char *tag, const char *raw, Py_ssize_t size, PyObject *owner) {
    if (put_count(w, tag, 1, (uint64_t)size) < 0) return -1;
    if (size < w->limit) return put(w, raw, size);
    return flush(w) < 0 ? -1 : feed(w, raw, size, owner);
}

static int object_member(PyTypeObject *kind, PyObject *name, PyObject *expected) {
    PyObject *member = _PyType_LookupRef(kind, name);
    int same = member == expected;
    Py_XDECREF(member);
    return same;
}

static int slot(PyTypeObject *owner, PyObject *name) {
    PyObject *member = _PyType_LookupRef(owner, name);
    int plain = member && Py_IS_TYPE(member, &PyMemberDescr_Type) && PyDescr_TYPE(member) == owner;
    Py_XDECREF(member);
    return plain;
}

static PyObject *plain_schema(Rules *rules, PyTypeObject *kind, int *metadata) {
    if (Py_TYPE(kind) != &PyType_Type || kind->tp_dictoffset || !object_member(kind, s_getattribute, object_getattribute)
            || !object_member(kind, s_sizeof, object_sizeof))
        Py_RETURN_NONE;
    PyTypeObject *field_type = (PyTypeObject *)rules->field_type, *params_type = (PyTypeObject *)rules->params_type;
    PyObject *namespace = PyType_GetDict(kind), *schema = NULL, *params = NULL, *frozen = NULL, *chosen = NULL;
    PyObject *names = NULL, *module = NULL, *qualname = NULL, *dotted = NULL, *name = NULL, *result = NULL;
    PyObject *key, *field;
    Py_ssize_t position = 0, count, index;
    if (!namespace || PyDict_GetItemRef(namespace, s_fields, &schema) < 0
            || PyDict_GetItemRef(namespace, s_params, &params) < 0)
        goto done;
    if (!schema || !PyDict_CheckExact(schema) || !params || !Py_IS_TYPE(params, params_type)) goto none;
    if (*metadata < 0)
        *metadata = object_member(field_type, s_getattribute, object_getattribute) && slot(field_type, s_name)
            && slot(field_type, s_field_type) && object_member(params_type, s_getattribute, object_getattribute)
            && slot(params_type, s_frozen);
    if (!*metadata) goto none;
    if (!(frozen = PyObject_GetAttr(params, s_frozen))) goto done;
    if (frozen != Py_True) goto none;
    while (PyDict_Next(schema, &position, &key, &field))
        if (!Py_IS_TYPE(field, field_type)) goto none;
    if (!(chosen = PyList_New(0))) goto done;
    for (position = 0; PyDict_Next(schema, &position, &key, &field);) {
        PyObject *marker = PyObject_GetAttr(field, s_field_type);
        int failed = !marker || (marker == rules->field_marker && PyList_Append(chosen, field) < 0);
        Py_XDECREF(marker);
        if (failed) goto done;
    }
    count = PyList_GET_SIZE(chosen);
    if (!(names = PyTuple_New(count))) goto done;
    for (index = 0; index < count; index++) {
        PyObject *read = PyObject_GetAttr(PyList_GET_ITEM(chosen, index), s_name);
        if (!read) goto done;
        PyTuple_SET_ITEM(names, index, read);
    }
    for (index = 0; index < count; index++) {
        PyObject *read = PyTuple_GET_ITEM(names, index), *member;
        if (!PyUnicode_CheckExact(read)) goto none;
        if (PyDict_GetItemRef(namespace, read, &member) < 0) goto done;
        int slotted = member && Py_IS_TYPE(member, &PyMemberDescr_Type);
        Py_XDECREF(member);
        if (!slotted) goto none;
    }
    if (PyDict_GetItemRef(namespace, s_module, &module) < 0 || !(qualname = PyType_GetQualName(kind))) goto done;
    if (!module || !PyUnicode_CheckExact(module) || !PyUnicode_CheckExact(qualname)) goto none;
    if ((dotted = PyUnicode_Concat(module, s_dot)) && (name = PyUnicode_Concat(dotted, qualname)))
        result = PyTuple_Pack(2, name, names);
    goto done;
none:
    result = Py_NewRef(Py_None);
done:
    Py_XDECREF(namespace);
    Py_XDECREF(schema);
    Py_XDECREF(params);
    Py_XDECREF(frozen);
    Py_XDECREF(chosen);
    Py_XDECREF(names);
    Py_XDECREF(module);
    Py_XDECREF(qualname);
    Py_XDECREF(dotted);
    Py_XDECREF(name);
    return result;
}

static void forget(Writer *w) {
    PyDict_Clear(w->known);
    w->metadata = -1;
}

static int known_schema(Writer *w, PyTypeObject *kind, PyObject *value, PyObject **schema) {
    *schema = NULL;
    if (Py_TYPE(kind) != &PyType_Type) return 0;
    int found = PyDict_GetItemRef(w->known, (PyObject *)kind, schema);
    if (found) return found < 0 ? -1 : 0;
    PyObject *named = plain_schema(&w->rules, kind, &w->metadata);
    if (!named || named == Py_None) {
        Py_XDECREF(named);
        return named ? 0 : -1;
    }
    int mapping = PyObject_IsInstance(value, w->rules.mapping);
    *schema = mapping < 0 ? NULL : mapping ? Py_NewRef(Py_True) : PyObject_Call(w->rules.record_schema, named, NULL);
    Py_DECREF(named);
    if (*schema && PyDict_SetItem(w->known, (PyObject *)kind, *schema) == 0) return 0;
    Py_CLEAR(*schema);
    return -1;
}

static int write_value(Writer *w, PyObject *value, int plain);

static int write_members(Writer *w, PyObject *value, PyObject *schema, int plain) {
    PyObject *prefix = PyTuple_Check(schema) && PyTuple_GET_SIZE(schema) == 2 ? PyTuple_GET_ITEM(schema, 0) : NULL;
    PyObject *members = prefix ? PyTuple_GET_ITEM(schema, 1) : NULL;
    if (!prefix || !PyBytes_Check(prefix) || !PyTuple_Check(members)) {
        PyErr_SetString(PyExc_SystemError, "malformed record schema");
        return -1;
    }
    if (put(w, PyBytes_AS_STRING(prefix), PyBytes_GET_SIZE(prefix)) < 0) return -1;
    for (Py_ssize_t index = 0; index < PyTuple_GET_SIZE(members); index++) {
        PyObject *member = PyTuple_GET_ITEM(members, index);
        PyObject *encoded = PyTuple_Check(member) && PyTuple_GET_SIZE(member) == 2 ? PyTuple_GET_ITEM(member, 1) : NULL;
        if (!encoded || !PyBytes_Check(encoded)) {
            PyErr_SetString(PyExc_SystemError, "malformed record schema");
            return -1;
        }
        PyObject *field = put(w, PyBytes_AS_STRING(encoded), PyBytes_GET_SIZE(encoded)) < 0 ? NULL
            : PyObject_GetAttr(value, PyTuple_GET_ITEM(member, 0));
        int failed = !field || write_value(w, field, plain) < 0;
        Py_XDECREF(field);
        if (failed) return -1;
    }
    return put(w, ";", 1);
}

static PyObject *field_names(Writer *w, PyObject *value) {
    PyObject *found = PyObject_CallOneArg(w->rules.fields, value);
    PyObject *iterator = found ? PyObject_GetIter(found) : NULL, *names = iterator ? PyList_New(0) : NULL, *field;
    Py_XDECREF(found);
    int failed = !names;
    while (!failed && (field = PyIter_Next(iterator))) {
        PyObject *name = PyObject_GetAttr(field, s_name);
        failed = !name || (!PyUnicode_CheckExact(name) && refuse(w, "Unsupported dataclass field name"));
        Py_XDECREF(name);
        if (!failed) {
            name = PyObject_GetAttr(field, s_name);
            failed = !name || PyList_Append(names, name) < 0;
            Py_XDECREF(name);
        }
        Py_DECREF(field);
    }
    Py_XDECREF(iterator);
    PyObject *result = failed || PyErr_Occurred() ? NULL : PyList_AsTuple(names);
    Py_XDECREF(names);
    return result;
}

static int write_loose_record(Writer *w, PyObject *value, PyObject *kind) {
    PyObject *found = PyObject_CallOneArg(w->rules.is_dataclass, value);
    int record = found ? PyObject_IsTrue(found) : -1;
    Py_XDECREF(found);
    if (record > 0) {
        int type = PyObject_IsInstance(value, (PyObject *)&PyType_Type);
        record = type < 0 ? -1 : !type;
    }
    if (record <= 0) return record < 0 ? -1 : refuse(w, "Unsupported compiler input");
    PyObject *module = PyObject_GetAttr(kind, s_module);
    PyObject *dotted = module ? PyNumber_Add(module, s_dot) : NULL;
    PyObject *qualname = dotted ? PyObject_GetAttr(kind, s_qualname) : NULL;
    PyObject *name = qualname ? PyNumber_Add(dotted, qualname) : NULL, *names = NULL, *schema = NULL;
    Py_XDECREF(module);
    Py_XDECREF(dotted);
    Py_XDECREF(qualname);
    if (!name) return -1;
    if (!PyUnicode_CheckExact(name)) refuse(w, "Unsupported dataclass name");
    else if ((names = field_names(w, value)))
        schema = PyObject_CallFunctionObjArgs(w->rules.record_schema, name, names, NULL);
    Py_DECREF(name);
    Py_XDECREF(names);
    if (!schema) return -1;
    int failed = write_members(w, value, schema, 0);
    Py_DECREF(schema);
    return failed;
}

static int unpack_pair(PyObject *pair, PyObject **key, PyObject **item) {
    if ((PyTuple_CheckExact(pair) || PyList_CheckExact(pair)) && Py_SIZE(pair) == 2) {
        *key = Py_NewRef(PySequence_Fast_ITEMS(pair)[0]);
        *item = Py_NewRef(PySequence_Fast_ITEMS(pair)[1]);
        return 0;
    }
    PyObject *iterator = PyObject_GetIter(pair), *extra = NULL;
    *key = iterator ? PyIter_Next(iterator) : NULL;
    *item = *key ? PyIter_Next(iterator) : NULL;
    extra = *item ? PyIter_Next(iterator) : NULL;
    Py_XDECREF(iterator);
    if (extra) {
        Py_DECREF(extra);
        PyErr_SetString(PyExc_ValueError, "too many values to unpack (expected 2)");
    } else if (!*item && !PyErr_Occurred()) {
        PyErr_SetString(PyExc_ValueError, "not enough values to unpack (expected 2)");
    }
    if (!PyErr_Occurred()) return 0;
    Py_CLEAR(*key);
    Py_CLEAR(*item);
    return -1;
}

static int write_mapping(Writer *w, PyObject *value, int plain) {
    Py_ssize_t count = PyObject_Size(value);
    if (count < 0 || put_count(w, "{", 1, (uint64_t)count) < 0) return -1;
    PyObject *items = PyObject_CallMethodNoArgs(value, s_items);
    PyObject *iterator = items ? PyObject_GetIter(items) : NULL, *pair;
    Py_XDECREF(items);
    if (!iterator) return -1;
    int failed = 0;
    while (!failed && (pair = PyIter_Next(iterator))) {
        PyObject *key = NULL, *item = NULL;
        failed = unpack_pair(pair, &key, &item) < 0 || write_value(w, key, plain) < 0 || write_value(w, item, plain) < 0;
        Py_XDECREF(key);
        Py_XDECREF(item);
        Py_DECREF(pair);
    }
    Py_DECREF(iterator);
    return failed || PyErr_Occurred() ? -1 : 0;
}

static int write_items(Writer *w, PyObject *value, int plain) {
    int tuple = PyTuple_CheckExact(value);
    if (put_count(w, tuple ? "(" : "[", 1, (uint64_t)Py_SIZE(value)) < 0) return -1;
    for (Py_ssize_t index = 0; index < Py_SIZE(value); index++) {
        PyObject *item = Py_NewRef(tuple ? PyTuple_GET_ITEM(value, index) : PyList_GET_ITEM(value, index));
        int failed = write_value(w, item, plain);
        Py_DECREF(item);
        if (failed) return -1;
    }
    return 0;
}

static int write_container(Writer *w, PyObject *value, int plain) {
    for (Py_ssize_t index = 0; index < w->depth; index++)
        if (w->active[index] == value) return refuse(w, "Cyclic compiler input");
    if (w->depth == w->slots) {
        Py_ssize_t slots = w->slots ? w->slots * 2 : 32;
        PyObject **active = PyMem_Realloc(w->active, (size_t)slots * sizeof *active);
        if (!active) { PyErr_NoMemory(); return -1; }
        w->active = active;
        w->slots = slots;
    }
    PyTypeObject *kind = (PyTypeObject *)Py_NewRef(Py_TYPE(value));
    int sequence = kind == &PyTuple_Type || kind == &PyList_Type, loose = 0, failed = 0;
    PyObject *schema = NULL;
    if (plain && !sequence && kind != &PyDict_Type) {
        failed = known_schema(w, kind, value, &schema);
        if (!failed && !schema) {
            forget(w);
            plain = 0;
            loose = 1;
        }
    }
    if (!failed) {
        w->active[w->depth++] = value;
        if (sequence) {
            failed = write_items(w, value, plain);
        } else {
            int mapping = kind == &PyDict_Type || schema == Py_True ? 1 : schema ? 0
                : PyObject_IsInstance(value, w->rules.mapping);
            failed = mapping < 0 ? -1 : mapping ? write_mapping(w, value, plain)
                : schema ? write_members(w, value, schema, plain) : write_loose_record(w, value, (PyObject *)kind);
        }
        w->depth--;
    }
    if (loose) forget(w);
    Py_XDECREF(schema);
    Py_DECREF(kind);
    return failed;
}

static int write_value(Writer *w, PyObject *value, int plain) {
    PyTypeObject *kind = Py_TYPE(value);
    if (value == Py_None) return put(w, "n", 1);
    if (kind == &PyBool_Type) return put(w, value == Py_True ? "t" : "f", 1);
    if (kind == &PyLong_Type) {
        int overflow;
        long long number = PyLong_AsLongLongAndOverflow(value, &overflow);
        if (number == -1 && PyErr_Occurred()) return -1;
        if (!overflow) {
            char digits[24], *start = digits + sizeof digits;
            uint64_t magnitude = number < 0 ? 0 - (uint64_t)number : (uint64_t)number;
            do *--start = (char)('0' + magnitude % 10); while (magnitude /= 10);
            if (number < 0) *--start = '-';
            Py_ssize_t size = digits + sizeof digits - start;
            return put_count(w, "is", 2, (uint64_t)size) < 0 ? -1 : put(w, start, size);
        }
        PyObject *text = PyObject_Str(value);
        Py_ssize_t size;
        const char *raw = text ? PyUnicode_AsUTF8AndSize(text, &size) : NULL;
        int failed = !raw || put_count(w, "is", 2, (uint64_t)size) < 0 || put(w, raw, size) < 0;
        Py_XDECREF(text);
        return failed ? -1 : 0;
    }
    if (kind == &PyFloat_Type) {
        double number = PyFloat_AS_DOUBLE(value);
        uint64_t bits;
        memcpy(&bits, &number, sizeof bits);
        return put_count(w, "d", 1, bits);
    }
    if (kind == &PyUnicode_Type) {
        if (PyUnicode_IS_ASCII(value))
            return put_raw(w, "s", PyUnicode_DATA(value), PyUnicode_GET_LENGTH(value), NULL);
        PyObject *raw = PyUnicode_AsUTF8String(value);
        int failed = !raw || put_raw(w, "s", PyBytes_AS_STRING(raw), PyBytes_GET_SIZE(raw), raw) < 0;
        Py_XDECREF(raw);
        return failed ? -1 : 0;
    }
    if (kind == &PyBytes_Type) return put_raw(w, "b", PyBytes_AS_STRING(value), PyBytes_GET_SIZE(value), value);
    if (Py_EnterRecursiveCall(" while writing a native build key")) return -1;
    int failed = write_container(w, value, plain);
    Py_LeaveRecursiveCall();
    return failed;
}

static PyObject *write_key(PyObject *self, PyObject *args) {
    PyObject *value, *rules;
    Writer w = {0};
    if (!PyArg_ParseTuple(args, "OOnO!:write_key", &value, &w.update, &w.limit, &PyTuple_Type, &rules)
            || !parse_rules(rules, &w.rules))
        return NULL;
    w.metadata = -1;
    w.known = PyDict_New();
    int failed = !w.known || write_value(&w, value, 1) < 0 || flush(&w) < 0;
    PyMem_Free(w.data);
    PyMem_Free(w.active);
    Py_XDECREF(w.known);
    if (failed) return NULL;
    Py_RETURN_NONE;
}

typedef struct {
    char *data;
    Py_ssize_t size, capacity;
    PyObject *other, *fields, *names;
} Json;

static int json_put(Json *j, const char *bytes, Py_ssize_t count) {
    if (j->size + count > j->capacity) {
        Py_ssize_t capacity = j->capacity ? j->capacity : 4096;
        while (capacity < j->size + count) capacity *= 2;
        char *data = PyMem_Realloc(j->data, capacity);
        if (!data) {
            PyErr_NoMemory();
            return -1;
        }
        j->data = data;
        j->capacity = capacity;
    }
    memcpy(j->data + j->size, bytes, count);
    j->size += count;
    return 0;
}

static int json_text(Json *j, PyObject *text) {
    if (!text) return -1;
    Py_ssize_t size;
    const char *data = PyUnicode_Check(text) ? PyUnicode_AsUTF8AndSize(text, &size) : NULL;
    if (!data && !PyErr_Occurred())
        PyErr_Format(PyExc_TypeError, "JSON text must be str, not %.200s", Py_TYPE(text)->tp_name);
    int failed = !data || json_put(j, data, size) < 0;
    Py_DECREF(text);
    return failed ? -1 : 0;
}

static int json_string(Json *j, PyObject *text) {
    static const char hex[] = "0123456789abcdef";
    int kind = PyUnicode_KIND(text), narrow = kind == PyUnicode_1BYTE_KIND;
    const void *data = PyUnicode_DATA(text);
    Py_ssize_t length = PyUnicode_GET_LENGTH(text), run = 0;
    if (json_put(j, "\"", 1) < 0) return -1;
    for (Py_ssize_t index = 0; index < length; index++) {
        Py_UCS4 c = PyUnicode_READ(kind, data, index);
        char out[12];
        int count = 0;
        if (c >= ' ' && c <= '~' && c != '\\' && c != '"') {
            if (narrow) continue;
            out[count++] = (char)c;
        } else {
            if (narrow && json_put(j, (const char *)data + run, index - run) < 0) return -1;
            run = index + 1;
            out[count++] = '\\';
            switch (c) {
            case '\\': case '"': out[count++] = (char)c; break;
            case '\b': out[count++] = 'b'; break;
            case '\f': out[count++] = 'f'; break;
            case '\n': out[count++] = 'n'; break;
            case '\r': out[count++] = 'r'; break;
            case '\t': out[count++] = 't'; break;
            default:
                if (c >= 0x10000) {
                    Py_UCS4 high = Py_UNICODE_HIGH_SURROGATE(c);
                    out[count++] = 'u';
                    for (int shift = 12; shift >= 0; shift -= 4) out[count++] = hex[(high >> shift) & 0xf];
                    c = Py_UNICODE_LOW_SURROGATE(c);
                    out[count++] = '\\';
                }
                out[count++] = 'u';
                for (int shift = 12; shift >= 0; shift -= 4) out[count++] = hex[(c >> shift) & 0xf];
            }
        }
        if (json_put(j, out, count) < 0) return -1;
    }
    if (narrow && json_put(j, (const char *)data + run, length - run) < 0) return -1;
    return json_put(j, "\"", 1);
}

static int json_value(Json *j, PyObject *value, PyObject *omitted);

static int json_record(Json *j, PyObject *value, PyObject *omitted) {
    PyObject *names = PyDict_GetItemWithError(j->names, (PyObject *)Py_TYPE(value));
    if (!names) {
        if (PyErr_Occurred()) return -1;
        PyObject *fields = PyObject_CallOneArg(j->fields, value);
        PyObject *list = fields ? PySequence_List(fields) : NULL;
        Py_XDECREF(fields);
        if (!list) return -1;
        for (Py_ssize_t index = 0; index < PyList_GET_SIZE(list); index++) {
            PyObject *name = PyObject_GetAttr(PyList_GET_ITEM(list, index), s_name);
            if (!name) {
                Py_DECREF(list);
                return -1;
            }
            PyList_SetItem(list, index, name);
        }
        int failed = PyList_Sort(list) < 0 || PyDict_SetItem(j->names, (PyObject *)Py_TYPE(value), list) < 0;
        Py_DECREF(list);
        if (failed) return -1;
        names = list;
    }
    if (json_put(j, "{", 1) < 0) return -1;
    int first = 1;
    for (Py_ssize_t index = 0; index < PyList_GET_SIZE(names); index++) {
        PyObject *name = PyList_GET_ITEM(names, index);
        int skip = omitted ? PySequence_Contains(omitted, name) : 0;
        if (skip < 0) return -1;
        if (skip) continue;
        if ((!first && json_put(j, ",", 1) < 0) || json_string(j, name) < 0 || json_put(j, ":", 1) < 0) return -1;
        first = 0;
        PyObject *item = PyObject_GetAttr(value, name);
        int failed = !item || json_value(j, item, NULL) < 0;
        Py_XDECREF(item);
        if (failed) return -1;
    }
    return json_put(j, "}", 1);
}

static int json_value(Json *j, PyObject *value, PyObject *omitted) {
    if (value == Py_None) return json_put(j, "null", 4);
    if (value == Py_True) return json_put(j, "true", 4);
    if (value == Py_False) return json_put(j, "false", 5);
    if (PyUnicode_CheckExact(value)) return json_string(j, value);
    if (PyFloat_CheckExact(value) && !isfinite(PyFloat_AS_DOUBLE(value))) {
        double number = PyFloat_AS_DOUBLE(value);
        return number > 0 ? json_put(j, "Infinity", 8) : number < 0 ? json_put(j, "-Infinity", 9) : json_put(j, "NaN", 3);
    }
    if (PyLong_CheckExact(value) || PyFloat_CheckExact(value)) return json_text(j, Py_TYPE(value)->tp_repr(value));
    int record = 0;
    if (!PyTuple_CheckExact(value) && !PyList_CheckExact(value)) {
        record = !PyType_Check(value) ? PyObject_HasAttrWithError((PyObject *)Py_TYPE(value), s_fields) : 0;
        if (record < 0) return -1;
        if (!record) return json_text(j, PyObject_CallOneArg(j->other, value));
    }
    if (Py_EnterRecursiveCall(" while writing JSON")) return -1;
    int failed;
    if (record) {
        failed = json_record(j, value, omitted);
    } else {
        PyObject *items = PySequence_Tuple(value);
        failed = !items || json_put(j, "[", 1) < 0;
        for (Py_ssize_t index = 0; !failed && index < PyTuple_GET_SIZE(items); index++)
            failed = (index && json_put(j, ",", 1) < 0) || json_value(j, PyTuple_GET_ITEM(items, index), NULL) < 0;
        failed = failed || json_put(j, "]", 1) < 0;
        Py_XDECREF(items);
    }
    Py_LeaveRecursiveCall();
    return failed ? -1 : 0;
}

static PyObject *record_json(PyObject *self, PyObject *args) {
    PyObject *value, *omitted, *module;
    Json j = {0};
    if (!PyArg_ParseTuple(args, "OO!O:record_json", &value, &PyTuple_Type, &omitted, &j.other)) return NULL;
    if (!(module = PyImport_ImportModule("dataclasses"))) return NULL;
    j.fields = PyObject_GetAttrString(module, "fields");
    Py_DECREF(module);
    j.names = j.fields ? PyDict_New() : NULL;
    PyObject *result = j.names && json_value(&j, value, omitted) == 0
        ? PyBytes_FromStringAndSize(j.data, j.size) : NULL;
    PyMem_Free(j.data);
    Py_XDECREF(j.fields);
    Py_XDECREF(j.names);
    return result;
}

static PyObject *plain_record_schema(PyObject *self, PyObject *args) {
    PyObject *kind, *rules;
    Rules parsed;
    int metadata = -1;
    if (!PyArg_ParseTuple(args, "OO!:plain_record_schema", &kind, &PyTuple_Type, &rules) || !parse_rules(rules, &parsed))
        return NULL;
    if (!PyType_Check(kind)) Py_RETURN_NONE;
    return plain_schema(&parsed, (PyTypeObject *)kind, &metadata);
}

static PyMethodDef methods[] = {
    {"write_key", write_key, METH_VARARGS,
     "write_key(value, update, buffer_size, rules): hand the stream native_build_key hashes for value to update."},
    {"plain_record_schema", plain_record_schema, METH_VARARGS,
     "plain_record_schema(kind, rules) -> (module.qualname, field names) of a plain record type, else None."},
    {"record_json", record_json, METH_VARARGS,
     "record_json(value, omitted, other) -> bytes: json.dumps(dataclasses.asdict(value), sort_keys=True,\n"
     "separators=(',', ':')) less the top-level fields named in omitted; values that are not records, tuples, lists,\n"
     "str, int, float, bool or None are written as other(value) returns them."},
    {NULL, NULL, 0, NULL},
};
static struct PyModuleDef module = {PyModuleDef_HEAD_INIT, "fastkey", NULL, -1, methods};

PyMODINIT_FUNC PyInit_fastkey(void) {
    static const char *texts[] = {"__getattribute__", "__sizeof__", "__dataclass_fields__", "__dataclass_params__",
                                  "__module__", "__qualname__", "frozen", "_field_type", "name", "items", "release", "."};
    PyObject **names[] = {&s_getattribute, &s_sizeof, &s_fields, &s_params, &s_module, &s_qualname, &s_frozen,
                          &s_field_type, &s_name, &s_items, &s_release, &s_dot};
    for (size_t index = 0; index < sizeof names / sizeof *names; index++)
        if (!*names[index] && !(*names[index] = PyUnicode_InternFromString(texts[index]))) return NULL;
    if (!object_getattribute) object_getattribute = _PyType_LookupRef(&PyBaseObject_Type, s_getattribute);
    if (!object_sizeof) object_sizeof = _PyType_LookupRef(&PyBaseObject_Type, s_sizeof);
    if (!object_getattribute || !object_sizeof) {
        PyErr_SetString(PyExc_ImportError, "object's __getattribute__ or __sizeof__ is missing");
        return NULL;
    }
    return PyModule_Create(&module);
}
