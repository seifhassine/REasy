#include <Python.h>
#include <math.h>
#include <stdint.h>
#include <string.h>

extern int PyFloat_Pack2(double, char*, int);
extern double PyFloat_Unpack2(const char*, int);

static double half_to_float(uint16_t h) {
    char buf[2];
    memcpy(buf, &h, 2);
    return PyFloat_Unpack2(buf, 1);
}

static uint16_t float_to_half(double f) {
    char buf[2];
    PyFloat_Pack2(f, buf, 1);
    uint16_t out;
    memcpy(&out, buf, 2);
    return out;
}

static PyObject* unpack_normals_tangents(PyObject* self, PyObject* args) {
    Py_buffer view;
    if (!PyArg_ParseTuple(args, "y*", &view))
        return NULL;
    const unsigned char* data = (const unsigned char*)view.buf;
    Py_ssize_t count = view.len / 8;

    PyObject* array_mod = PyImport_ImportModule("array");
    if (!array_mod) {
        PyBuffer_Release(&view);
        return NULL;
    }
    PyObject* array_cls = PyObject_GetAttrString(array_mod, "array");
    Py_DECREF(array_mod);
    if (!array_cls) {
        PyBuffer_Release(&view);
        return NULL;
    }

    PyObject* normals_arr = PyObject_CallFunction(array_cls, "s", "f");
    PyObject* tangents_arr = PyObject_CallFunction(array_cls, "s", "f");
    PyObject* normal_ws_arr = PyObject_CallFunction(array_cls, "s", "B");
    PyObject* tangent_ws_arr = PyObject_CallFunction(array_cls, "s", "B");

    PyObject* normals_bytes = PyBytes_FromStringAndSize(NULL, count * 3 * sizeof(float));
    PyObject* tangents_bytes = PyBytes_FromStringAndSize(NULL, count * 3 * sizeof(float));
    PyObject* normal_w_bytes = PyBytes_FromStringAndSize(NULL, count);
    PyObject* tangent_w_bytes = PyBytes_FromStringAndSize(NULL, count);
    if (!normals_arr || !tangents_arr || !normal_ws_arr || !tangent_ws_arr ||
        !normals_bytes || !tangents_bytes || !normal_w_bytes || !tangent_w_bytes) {
        Py_XDECREF(normals_arr); Py_XDECREF(tangents_arr);
        Py_XDECREF(normal_ws_arr); Py_XDECREF(tangent_ws_arr);
        Py_XDECREF(normals_bytes); Py_XDECREF(tangents_bytes);
        Py_XDECREF(normal_w_bytes); Py_XDECREF(tangent_w_bytes);
        Py_DECREF(array_cls);
        PyBuffer_Release(&view);
        return NULL;
    }

    float* np = (float*)PyBytes_AsString(normals_bytes);
    float* tp = (float*)PyBytes_AsString(tangents_bytes);
    unsigned char* nwp = (unsigned char*)PyBytes_AsString(normal_w_bytes);
    unsigned char* twp = (unsigned char*)PyBytes_AsString(tangent_w_bytes);

    for (Py_ssize_t i = 0; i < count; ++i) {
        const signed char* p = (const signed char*)(data + i * 8);
        np[i * 3 + 0] = p[0] / 127.0f;
        np[i * 3 + 1] = p[1] / 127.0f;
        np[i * 3 + 2] = p[2] / 127.0f;
        nwp[i] = (unsigned char)p[3];
        tp[i * 3 + 0] = p[4] / 127.0f;
        tp[i * 3 + 1] = p[5] / 127.0f;
        tp[i * 3 + 2] = p[6] / 127.0f;
        twp[i] = (unsigned char)p[7];
    }

    PyObject_CallMethod(normals_arr, "frombytes", "O", normals_bytes);
    PyObject_CallMethod(tangents_arr, "frombytes", "O", tangents_bytes);
    PyObject_CallMethod(normal_ws_arr, "frombytes", "O", normal_w_bytes);
    PyObject_CallMethod(tangent_ws_arr, "frombytes", "O", tangent_w_bytes);

    Py_DECREF(normals_bytes); Py_DECREF(tangents_bytes);
    Py_DECREF(normal_w_bytes); Py_DECREF(tangent_w_bytes);
    Py_DECREF(array_cls);
    PyBuffer_Release(&view);

    return Py_BuildValue("(OOOO)", normals_arr, normal_ws_arr, tangents_arr, tangent_ws_arr);
}

static PyObject* pack_normals_tangents(PyObject* self, PyObject* args) {
    PyObject *normals_obj, *normal_ws_obj, *tangents_obj, *tangent_ws_obj;
    if (!PyArg_ParseTuple(args, "OOOO", &normals_obj, &normal_ws_obj, &tangents_obj, &tangent_ws_obj))
        return NULL;

    Py_buffer normals, normal_ws, tangents, tangent_ws;
    if (PyObject_GetBuffer(normals_obj, &normals, PyBUF_SIMPLE) < 0) return NULL;
    if (PyObject_GetBuffer(normal_ws_obj, &normal_ws, PyBUF_SIMPLE) < 0) {
        PyBuffer_Release(&normals); return NULL; }
    if (PyObject_GetBuffer(tangents_obj, &tangents, PyBUF_SIMPLE) < 0) {
        PyBuffer_Release(&normals); PyBuffer_Release(&normal_ws); return NULL; }
    if (PyObject_GetBuffer(tangent_ws_obj, &tangent_ws, PyBUF_SIMPLE) < 0) {
        PyBuffer_Release(&normals); PyBuffer_Release(&normal_ws); PyBuffer_Release(&tangents); return NULL; }

    Py_ssize_t count = normals.len / (3 * sizeof(float));
    PyObject* bytes = PyBytes_FromStringAndSize(NULL, count * 8);
    if (!bytes) {
        PyBuffer_Release(&normals); PyBuffer_Release(&normal_ws);
        PyBuffer_Release(&tangents); PyBuffer_Release(&tangent_ws);
        return NULL;
    }
    unsigned char* buf = (unsigned char*)PyBytes_AsString(bytes);
    const float* np = (const float*)normals.buf;
    const float* tp = (const float*)tangents.buf;
    const unsigned char* nwp = (const unsigned char*)normal_ws.buf;
    const unsigned char* twp = (const unsigned char*)tangent_ws.buf;

    for (Py_ssize_t i = 0; i < count; ++i) {
        buf[i*8 + 0] = (unsigned char)lroundf(np[i*3 + 0] * 127.0f);
        buf[i*8 + 1] = (unsigned char)lroundf(np[i*3 + 1] * 127.0f);
        buf[i*8 + 2] = (unsigned char)lroundf(np[i*3 + 2] * 127.0f);
        buf[i*8 + 3] = nwp[i];
        buf[i*8 + 4] = (unsigned char)lroundf(tp[i*3 + 0] * 127.0f);
        buf[i*8 + 5] = (unsigned char)lroundf(tp[i*3 + 1] * 127.0f);
        buf[i*8 + 6] = (unsigned char)lroundf(tp[i*3 + 2] * 127.0f);
        buf[i*8 + 7] = twp[i];
    }

    PyBuffer_Release(&normals); PyBuffer_Release(&normal_ws);
    PyBuffer_Release(&tangents); PyBuffer_Release(&tangent_ws);
    return bytes;
}


static PyObject* unpack_uvs(PyObject* self, PyObject* args) {
    Py_buffer view;
    if (!PyArg_ParseTuple(args, "y*", &view))
        return NULL;
    Py_ssize_t count = view.len / 4;
    const unsigned short* data = (const unsigned short*)view.buf;

    PyObject* array_mod = PyImport_ImportModule("array");
    if (!array_mod) { PyBuffer_Release(&view); return NULL; }
    PyObject* array_cls = PyObject_GetAttrString(array_mod, "array");
    Py_DECREF(array_mod);
    if (!array_cls) { PyBuffer_Release(&view); return NULL; }
    PyObject* arr = PyObject_CallFunction(array_cls, "s", "d");
    Py_DECREF(array_cls);
    if (!arr) { PyBuffer_Release(&view); return NULL; }

    PyObject* bytes = PyBytes_FromStringAndSize(NULL, count * 2 * sizeof(double));
    if (!bytes) { Py_DECREF(arr); PyBuffer_Release(&view); return NULL; }
    double* out = (double*)PyBytes_AsString(bytes);
    for (Py_ssize_t i = 0; i < count; ++i) {
        double u = half_to_float(data[i*2 + 0]);
        double v = half_to_float(data[i*2 + 1]);
        /* perform UV flip using double precision to avoid losing LSBs */
        out[i*2 + 0] = 1.0 - u;
        out[i*2 + 1] = 1.0 - v;
    }
    PyObject_CallMethod(arr, "frombytes", "O", bytes);
    Py_DECREF(bytes);
    PyBuffer_Release(&view);
    return arr;
}

static PyObject* pack_uvs(PyObject* self, PyObject* args) {
    PyObject* arr_obj;
    if (!PyArg_ParseTuple(args, "O", &arr_obj))
        return NULL;
    Py_buffer view;
    if (PyObject_GetBuffer(arr_obj, &view, PyBUF_SIMPLE) < 0)
        return NULL;
    Py_ssize_t count = view.len / (2 * sizeof(double));
    PyObject* bytes = PyBytes_FromStringAndSize(NULL, count * 4);
    if (!bytes) { PyBuffer_Release(&view); return NULL; }
    unsigned short* out = (unsigned short*)PyBytes_AsString(bytes);
    const double* f = (const double*)view.buf;
    for (Py_ssize_t i = 0; i < count; ++i) {
        double u = f[i*2 + 0];
        double v = f[i*2 + 1];
        u = 1.0 - u;
        v = 1.0 - v;
        out[i*2 + 0] = float_to_half(u);
        out[i*2 + 1] = float_to_half(v);
    }
    PyBuffer_Release(&view);
    return bytes;
}

static PyObject* unpack_colors(PyObject* self, PyObject* args) {
    Py_buffer view;
    if (!PyArg_ParseTuple(args, "y*", &view))
        return NULL;
    PyObject* array_mod = PyImport_ImportModule("array");
    if (!array_mod) { PyBuffer_Release(&view); return NULL; }
    PyObject* array_cls = PyObject_GetAttrString(array_mod, "array");
    Py_DECREF(array_mod);
    if (!array_cls) { PyBuffer_Release(&view); return NULL; }
    PyObject* arr = PyObject_CallFunction(array_cls, "s", "B");
    Py_DECREF(array_cls);
    if (!arr) { PyBuffer_Release(&view); return NULL; }
    PyObject* bytes = PyBytes_FromStringAndSize((const char*)view.buf, view.len);
    if (!bytes) { Py_DECREF(arr); PyBuffer_Release(&view); return NULL; }
    PyObject_CallMethod(arr, "frombytes", "O", bytes);
    Py_DECREF(bytes);
    PyBuffer_Release(&view);
    return arr;
}

static PyObject* pack_colors(PyObject* self, PyObject* args) {
    PyObject* arr_obj;
    if (!PyArg_ParseTuple(args, "O", &arr_obj))
        return NULL;
    Py_buffer view;
    if (PyObject_GetBuffer(arr_obj, &view, PyBUF_SIMPLE) < 0)
        return NULL;
    PyObject* bytes = PyBytes_FromStringAndSize((const char*)view.buf, view.len);
    PyBuffer_Release(&view);
    return bytes;
}

static PyMethodDef methods[] = {
    {"unpack_normals_tangents", unpack_normals_tangents, METH_VARARGS, "Decode normals/tangents from bytes"},
    {"pack_normals_tangents", pack_normals_tangents, METH_VARARGS, "Encode normals/tangents to bytes"},
    {"unpack_uvs", unpack_uvs, METH_VARARGS, "Decode UV half floats"},
    {"pack_uvs", pack_uvs, METH_VARARGS, "Encode UV floats"},
    {"unpack_colors", unpack_colors, METH_VARARGS, "Decode RGBA colors"},
    {"pack_colors", pack_colors, METH_VARARGS, "Encode RGBA colors"},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef moduledef = {
    PyModuleDef_HEAD_INIT,
    "fastmesh",
    NULL,
    -1,
    methods
};

PyMODINIT_FUNC PyInit_fastmesh(void) {
    return PyModule_Create(&moduledef);
}
