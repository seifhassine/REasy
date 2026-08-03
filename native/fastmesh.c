#include <Python.h>
#include <math.h>
#include <stdint.h>
#include <string.h>

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

static PyObject* skin_vertices(PyObject* self, PyObject* args) {
    PyObject *positions_obj, *normals_obj, *joints_obj, *weights_obj, *matrices_obj;
    Py_buffer positions = {0}, normals = {0}, joints = {0}, weights = {0}, matrices = {0};
    int has_normals = 0;
    PyObject *position_bytes = NULL, *normal_bytes = NULL;

    if (!PyArg_ParseTuple(
            args,
            "OOOOO",
            &positions_obj,
            &normals_obj,
            &joints_obj,
            &weights_obj,
            &matrices_obj))
        return NULL;
    if (PyObject_GetBuffer(positions_obj, &positions, PyBUF_CONTIG_RO) < 0)
        goto fail;
    if (normals_obj != Py_None) {
        if (PyObject_GetBuffer(normals_obj, &normals, PyBUF_CONTIG_RO) < 0)
            goto fail;
        has_normals = 1;
    }
    if (PyObject_GetBuffer(joints_obj, &joints, PyBUF_CONTIG_RO) < 0)
        goto fail;
    if (PyObject_GetBuffer(weights_obj, &weights, PyBUF_CONTIG_RO) < 0)
        goto fail;
    if (PyObject_GetBuffer(matrices_obj, &matrices, PyBUF_CONTIG_RO) < 0)
        goto fail;

    if (positions.len % (3 * (Py_ssize_t)sizeof(float)) != 0) {
        PyErr_SetString(PyExc_ValueError, "positions must contain packed float32 xyz values");
        goto fail;
    }
    Py_ssize_t vertex_count = positions.len / (3 * (Py_ssize_t)sizeof(float));
    if (vertex_count <= 0 || joints.len % (vertex_count * (Py_ssize_t)sizeof(uint16_t)) != 0) {
        PyErr_SetString(PyExc_ValueError, "joint indices do not match the vertex count");
        goto fail;
    }
    Py_ssize_t influence_count = joints.len / (vertex_count * (Py_ssize_t)sizeof(uint16_t));
    if (influence_count <= 0 || weights.len != vertex_count * influence_count * (Py_ssize_t)sizeof(float)) {
        PyErr_SetString(PyExc_ValueError, "weights do not match the joint-index layout");
        goto fail;
    }
    if (has_normals && normals.len != positions.len) {
        PyErr_SetString(PyExc_ValueError, "normals must match the position layout");
        goto fail;
    }
    if (matrices.len % (16 * (Py_ssize_t)sizeof(float)) != 0) {
        PyErr_SetString(PyExc_ValueError, "skin matrices must contain packed float32 4x4 values");
        goto fail;
    }
    Py_ssize_t matrix_count = matrices.len / (16 * (Py_ssize_t)sizeof(float));
    const uint16_t* joint_data = (const uint16_t*)joints.buf;
    const float* weight_data = (const float*)weights.buf;
    for (Py_ssize_t index = 0; index < vertex_count * influence_count; ++index) {
        if (weight_data[index] != 0.0f && joint_data[index] >= matrix_count) {
            PyErr_Format(
                PyExc_ValueError,
                "skin influence references joint %u, but only %zd matrices were provided",
                (unsigned int)joint_data[index],
                matrix_count);
            goto fail;
        }
    }

    position_bytes = PyBytes_FromStringAndSize(NULL, positions.len);
    if (!position_bytes)
        goto fail;
    if (has_normals) {
        normal_bytes = PyBytes_FromStringAndSize(NULL, normals.len);
        if (!normal_bytes)
            goto fail;
    } else {
        Py_INCREF(Py_None);
        normal_bytes = Py_None;
    }

    const float* input_positions = (const float*)positions.buf;
    const float* input_normals = has_normals ? (const float*)normals.buf : NULL;
    const float* matrix_data = (const float*)matrices.buf;
    float* output_positions = (float*)PyBytes_AsString(position_bytes);
    float* output_normals = has_normals ? (float*)PyBytes_AsString(normal_bytes) : NULL;

    Py_BEGIN_ALLOW_THREADS
    for (Py_ssize_t vertex = 0; vertex < vertex_count; ++vertex) {
        const float x = input_positions[vertex * 3 + 0];
        const float y = input_positions[vertex * 3 + 1];
        const float z = input_positions[vertex * 3 + 2];
        const float nx = has_normals ? input_normals[vertex * 3 + 0] : 0.0f;
        const float ny = has_normals ? input_normals[vertex * 3 + 1] : 0.0f;
        const float nz = has_normals ? input_normals[vertex * 3 + 2] : 0.0f;
        float ox = 0.0f, oy = 0.0f, oz = 0.0f;
        float onx = 0.0f, ony = 0.0f, onz = 0.0f;
        const Py_ssize_t influence_base = vertex * influence_count;
        for (Py_ssize_t influence = 0; influence < influence_count; ++influence) {
            const Py_ssize_t influence_index = influence_base + influence;
            const float weight = weight_data[influence_index];
            if (weight == 0.0f)
                continue;
            const float* matrix = matrix_data + joint_data[influence_index] * 16;
            ox += (x * matrix[0] + y * matrix[4] + z * matrix[8] + matrix[12]) * weight;
            oy += (x * matrix[1] + y * matrix[5] + z * matrix[9] + matrix[13]) * weight;
            oz += (x * matrix[2] + y * matrix[6] + z * matrix[10] + matrix[14]) * weight;
            if (has_normals) {
                onx += (nx * matrix[0] + ny * matrix[4] + nz * matrix[8]) * weight;
                ony += (nx * matrix[1] + ny * matrix[5] + nz * matrix[9]) * weight;
                onz += (nx * matrix[2] + ny * matrix[6] + nz * matrix[10]) * weight;
            }
        }
        output_positions[vertex * 3 + 0] = ox;
        output_positions[vertex * 3 + 1] = oy;
        output_positions[vertex * 3 + 2] = oz;
        if (has_normals) {
            const float length = sqrtf(onx * onx + ony * ony + onz * onz);
            if (length > 1e-12f) {
                onx /= length;
                ony /= length;
                onz /= length;
            }
            output_normals[vertex * 3 + 0] = onx;
            output_normals[vertex * 3 + 1] = ony;
            output_normals[vertex * 3 + 2] = onz;
        }
    }
    Py_END_ALLOW_THREADS

    PyBuffer_Release(&positions);
    if (has_normals) PyBuffer_Release(&normals);
    PyBuffer_Release(&joints);
    PyBuffer_Release(&weights);
    PyBuffer_Release(&matrices);
    return Py_BuildValue("NN", position_bytes, normal_bytes);

fail:
    Py_XDECREF(position_bytes);
    Py_XDECREF(normal_bytes);
    if (positions.obj) PyBuffer_Release(&positions);
    if (normals.obj) PyBuffer_Release(&normals);
    if (joints.obj) PyBuffer_Release(&joints);
    if (weights.obj) PyBuffer_Release(&weights);
    if (matrices.obj) PyBuffer_Release(&matrices);
    return NULL;
}

static PyObject* recalculate_normals(PyObject* self, PyObject* args) {
    PyObject *positions_obj, *triangles_obj, *redirects_obj;
    Py_buffer positions = {0}, triangles = {0}, redirects = {0};
    PyObject* output_bytes = NULL;
    double* accumulated = NULL;

    if (!PyArg_ParseTuple(
            args,
            "OOO",
            &positions_obj,
            &triangles_obj,
            &redirects_obj))
        return NULL;
    if (PyObject_GetBuffer(positions_obj, &positions, PyBUF_CONTIG_RO) < 0)
        goto fail;
    if (PyObject_GetBuffer(triangles_obj, &triangles, PyBUF_CONTIG_RO) < 0)
        goto fail;
    if (PyObject_GetBuffer(redirects_obj, &redirects, PyBUF_CONTIG_RO) < 0)
        goto fail;

    if (positions.len % (3 * (Py_ssize_t)sizeof(float)) != 0) {
        PyErr_SetString(PyExc_ValueError, "positions must contain packed float32 xyz values");
        goto fail;
    }
    const Py_ssize_t vertex_count = positions.len / (3 * (Py_ssize_t)sizeof(float));
    if (vertex_count <= 0) {
        PyErr_SetString(PyExc_ValueError, "normal recalculation requires vertices");
        goto fail;
    }
    if (triangles.len % (3 * (Py_ssize_t)sizeof(Py_ssize_t)) != 0) {
        PyErr_SetString(PyExc_ValueError, "triangles must contain packed native-size index triples");
        goto fail;
    }
    if (redirects.len != vertex_count * (Py_ssize_t)sizeof(Py_ssize_t)) {
        PyErr_SetString(PyExc_ValueError, "normal redirects must match the vertex count");
        goto fail;
    }

    const Py_ssize_t* triangle_data = (const Py_ssize_t*)triangles.buf;
    const Py_ssize_t* redirect_data = (const Py_ssize_t*)redirects.buf;
    const Py_ssize_t triangle_index_count = triangles.len / (Py_ssize_t)sizeof(Py_ssize_t);
    for (Py_ssize_t index = 0; index < triangle_index_count; ++index) {
        if (triangle_data[index] < 0 || triangle_data[index] >= vertex_count) {
            PyErr_SetString(PyExc_ValueError, "normal triangle index is outside the vertex buffer");
            goto fail;
        }
    }
    for (Py_ssize_t vertex = 0; vertex < vertex_count; ++vertex) {
        if (redirect_data[vertex] < 0 || redirect_data[vertex] >= vertex_count) {
            PyErr_SetString(PyExc_ValueError, "normal redirect is outside the vertex buffer");
            goto fail;
        }
    }

    output_bytes = PyBytes_FromStringAndSize(NULL, positions.len);
    accumulated = PyMem_Calloc((size_t)vertex_count * 3, sizeof(double));
    if (!output_bytes)
        goto fail;
    if (!accumulated) {
        PyErr_NoMemory();
        goto fail;
    }

    const float* position_data = (const float*)positions.buf;
    float* output = (float*)PyBytes_AsString(output_bytes);
    Py_BEGIN_ALLOW_THREADS
    for (Py_ssize_t index = 0; index < triangle_index_count; index += 3) {
        const Py_ssize_t a = triangle_data[index];
        const Py_ssize_t b = triangle_data[index + 1];
        const Py_ssize_t c = triangle_data[index + 2];
        const float abx = position_data[b * 3] - position_data[a * 3];
        const float aby = position_data[b * 3 + 1] - position_data[a * 3 + 1];
        const float abz = position_data[b * 3 + 2] - position_data[a * 3 + 2];
        const float acx = position_data[c * 3] - position_data[a * 3];
        const float acy = position_data[c * 3 + 1] - position_data[a * 3 + 1];
        const float acz = position_data[c * 3 + 2] - position_data[a * 3 + 2];
        float nx = aby * acz - abz * acy;
        float ny = abz * acx - abx * acz;
        float nz = abx * acy - aby * acx;
        const float length = sqrtf(nx * nx + ny * ny + nz * nz);
        if (length > 0.0f) {
            nx /= length;
            ny /= length;
            nz /= length;
        }
        nx = truncf(fabsf(nx) * 1023.0f) * (nx < 0.0f ? -1.0f : 1.0f);
        ny = truncf(fabsf(ny) * 1023.0f) * (ny < 0.0f ? -1.0f : 1.0f);
        nz = truncf(fabsf(nz) * 1023.0f) * (nz < 0.0f ? -1.0f : 1.0f);
        const Py_ssize_t vertices[3] = {a, b, c};
        for (int corner = 0; corner < 3; ++corner) {
            const Py_ssize_t base = vertices[corner] * 3;
            accumulated[base] += nx;
            accumulated[base + 1] += ny;
            accumulated[base + 2] += nz;
        }
    }
    for (Py_ssize_t vertex = 0; vertex < vertex_count; ++vertex) {
        const Py_ssize_t source = redirect_data[vertex] * 3;
        float nx = (float)accumulated[source];
        float ny = (float)accumulated[source + 1];
        float nz = (float)accumulated[source + 2];
        const float length = sqrtf(nx * nx + ny * ny + nz * nz);
        if (length > 0.0f) {
            nx /= length;
            ny /= length;
            nz /= length;
        }
        output[vertex * 3] = nx;
        output[vertex * 3 + 1] = ny;
        output[vertex * 3 + 2] = nz;
    }
    Py_END_ALLOW_THREADS

    PyMem_Free(accumulated);
    PyBuffer_Release(&positions);
    PyBuffer_Release(&triangles);
    PyBuffer_Release(&redirects);
    return output_bytes;

fail:
    PyMem_Free(accumulated);
    Py_XDECREF(output_bytes);
    if (positions.obj) PyBuffer_Release(&positions);
    if (triangles.obj) PyBuffer_Release(&triangles);
    if (redirects.obj) PyBuffer_Release(&redirects);
    return NULL;
}

static PyMethodDef methods[] = {
    {"unpack_normals_tangents", unpack_normals_tangents, METH_VARARGS, "Decode normals/tangents from bytes"},
    {"pack_normals_tangents", pack_normals_tangents, METH_VARARGS, "Encode normals/tangents to bytes"},
    {"unpack_uvs", unpack_uvs, METH_VARARGS, "Decode UV half floats"},
    {"pack_uvs", pack_uvs, METH_VARARGS, "Encode UV floats"},
    {"unpack_colors", unpack_colors, METH_VARARGS, "Decode RGBA colors"},
    {"pack_colors", pack_colors, METH_VARARGS, "Encode RGBA colors"},
    {"skin_vertices", skin_vertices, METH_VARARGS, "Apply linear-blend skinning"},
    {"recalculate_normals", recalculate_normals, METH_VARARGS, "Recalculate redirected render normals"},
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
