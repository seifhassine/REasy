#include <Python.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

static int append_array_bytes(PyObject* array, PyObject* bytes) {
    PyObject* result = PyObject_CallMethod(array, "frombytes", "O", bytes);
    if (!result)
        return -1;
    Py_DECREF(result);
    return 0;
}

static float snorm8_to_float(signed char value) {
    return fmaxf((float)value / 127.0f, -1.0f);
}

static PyObject* unpack_normals_tangents(PyObject* self, PyObject* args) {
    Py_buffer view;
    if (!PyArg_ParseTuple(args, "y*", &view))
        return NULL;
    if (view.len % 8 != 0) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError, "normal/tangent data must contain whole 8-byte records");
        return NULL;
    }
    const unsigned char* data = (const unsigned char*)view.buf;
    Py_ssize_t count = view.len / 8;
    if (count > PY_SSIZE_T_MAX / (3 * (Py_ssize_t)sizeof(float))) {
        PyBuffer_Release(&view);
        return PyErr_NoMemory();
    }

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
        np[i * 3 + 0] = snorm8_to_float(p[0]);
        np[i * 3 + 1] = snorm8_to_float(p[1]);
        np[i * 3 + 2] = snorm8_to_float(p[2]);
        nwp[i] = (unsigned char)p[3];
        tp[i * 3 + 0] = snorm8_to_float(p[4]);
        tp[i * 3 + 1] = snorm8_to_float(p[5]);
        tp[i * 3 + 2] = snorm8_to_float(p[6]);
        twp[i] = (unsigned char)p[7];
    }

    int copied = append_array_bytes(normals_arr, normals_bytes) == 0
        && append_array_bytes(tangents_arr, tangents_bytes) == 0
        && append_array_bytes(normal_ws_arr, normal_w_bytes) == 0
        && append_array_bytes(tangent_ws_arr, tangent_w_bytes) == 0;

    Py_DECREF(normals_bytes); Py_DECREF(tangents_bytes);
    Py_DECREF(normal_w_bytes); Py_DECREF(tangent_w_bytes);
    Py_DECREF(array_cls);
    PyBuffer_Release(&view);

    if (!copied) {
        Py_DECREF(normals_arr); Py_DECREF(tangents_arr);
        Py_DECREF(normal_ws_arr); Py_DECREF(tangent_ws_arr);
        return NULL;
    }
    return Py_BuildValue("(NNNN)", normals_arr, normal_ws_arr, tangents_arr, tangent_ws_arr);
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
    if (normals.len % (3 * sizeof(float)) != 0 || tangents.len != normals.len
            || normal_ws.len != count || tangent_ws.len != count) {
        PyBuffer_Release(&normals); PyBuffer_Release(&normal_ws);
        PyBuffer_Release(&tangents); PyBuffer_Release(&tangent_ws);
        PyErr_SetString(PyExc_ValueError, "normal, tangent, and W buffers must describe the same vertices");
        return NULL;
    }
    PyObject* bytes = PyBytes_FromStringAndSize(NULL, count * 8);
    if (!bytes) {
        PyBuffer_Release(&normals); PyBuffer_Release(&normal_ws);
        PyBuffer_Release(&tangents); PyBuffer_Release(&tangent_ws);
        return NULL;
    }
    unsigned char* buf = (unsigned char*)PyBytes_AsString(bytes);
    const unsigned char* nwp = (const unsigned char*)normal_ws.buf;
    const unsigned char* twp = (const unsigned char*)tangent_ws.buf;

    for (Py_ssize_t i = 0; i < count; ++i) {
        float normal[3], tangent[3];
        memcpy(normal, (const char*)normals.buf + i * sizeof(normal), sizeof(normal));
        memcpy(tangent, (const char*)tangents.buf + i * sizeof(tangent), sizeof(tangent));
        for (int axis = 0; axis < 3; ++axis) {
            if (!isfinite(normal[axis]) || !isfinite(tangent[axis])) {
                Py_DECREF(bytes);
                PyBuffer_Release(&normals); PyBuffer_Release(&normal_ws);
                PyBuffer_Release(&tangents); PyBuffer_Release(&tangent_ws);
                PyErr_SetString(PyExc_ValueError, "normal and tangent values must be finite");
                return NULL;
            }
            const float n = fmaxf(-1.0f, fminf(1.0f, normal[axis]));
            const float t = fmaxf(-1.0f, fminf(1.0f, tangent[axis]));
            buf[i*8 + axis] = (unsigned char)lroundf(n * 127.0f);
            buf[i*8 + axis + 4] = (unsigned char)lroundf(t * 127.0f);
        }
        buf[i*8 + 3] = nwp[i];
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
    if (view.len % 4 != 0) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError, "UV data must contain whole half-float pairs");
        return NULL;
    }
    Py_ssize_t count = view.len / 4;
    if (count > PY_SSIZE_T_MAX / (2 * (Py_ssize_t)sizeof(double))) {
        PyBuffer_Release(&view);
        return PyErr_NoMemory();
    }
    const char* data = (const char*)view.buf;

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
        double u = PyFloat_Unpack2(data + i * 4, 1);
        double v = PyFloat_Unpack2(data + i * 4 + 2, 1);
        /* perform UV flip using double precision to avoid losing LSBs */
        out[i*2 + 0] = 1.0 - u;
        out[i*2 + 1] = 1.0 - v;
    }
    int copied = append_array_bytes(arr, bytes) == 0;
    Py_DECREF(bytes);
    PyBuffer_Release(&view);
    if (!copied) {
        Py_DECREF(arr);
        return NULL;
    }
    return arr;
}

static PyObject* pack_uvs(PyObject* self, PyObject* args) {
    PyObject* arr_obj;
    if (!PyArg_ParseTuple(args, "O", &arr_obj))
        return NULL;
    Py_buffer view;
    if (PyObject_GetBuffer(arr_obj, &view, PyBUF_SIMPLE) < 0)
        return NULL;
    if (view.len % (2 * sizeof(double)) != 0) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError, "UV values must contain packed float64 pairs");
        return NULL;
    }
    Py_ssize_t count = view.len / (2 * sizeof(double));
    PyObject* bytes = PyBytes_FromStringAndSize(NULL, count * 4);
    if (!bytes) { PyBuffer_Release(&view); return NULL; }
    char* out = PyBytes_AsString(bytes);
    for (Py_ssize_t i = 0; i < count; ++i) {
        double uv[2];
        memcpy(uv, (const char*)view.buf + i * sizeof(uv), sizeof(uv));
        if (PyFloat_Pack2(1.0 - uv[0], out + i * 4, 1) < 0
                || PyFloat_Pack2(1.0 - uv[1], out + i * 4 + 2, 1) < 0) {
            Py_DECREF(bytes);
            PyBuffer_Release(&view);
            return NULL;
        }
    }
    PyBuffer_Release(&view);
    return bytes;
}

static PyObject* unpack_colors(PyObject* self, PyObject* args) {
    Py_buffer view;
    if (!PyArg_ParseTuple(args, "y*", &view))
        return NULL;
    if (view.len % 4 != 0) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError, "color data must contain whole RGBA records");
        return NULL;
    }
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
    int copied = append_array_bytes(arr, bytes) == 0;
    Py_DECREF(bytes);
    PyBuffer_Release(&view);
    if (!copied) {
        Py_DECREF(arr);
        return NULL;
    }
    return arr;
}

static PyObject* pack_colors(PyObject* self, PyObject* args) {
    PyObject* arr_obj;
    if (!PyArg_ParseTuple(args, "O", &arr_obj))
        return NULL;
    Py_buffer view;
    if (PyObject_GetBuffer(arr_obj, &view, PyBUF_SIMPLE) < 0)
        return NULL;
    if (view.len % 4 != 0) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError, "color values must contain whole RGBA records");
        return NULL;
    }
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
    int invalid_joint = -1;

    Py_BEGIN_ALLOW_THREADS
    for (Py_ssize_t vertex = 0; vertex < vertex_count && invalid_joint < 0; ++vertex) {
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
            const uint16_t joint = joint_data[influence_index];
            /* Exported buffers can change while the GIL is released. */
            if (joint >= matrix_count) {
                invalid_joint = joint;
                break;
            }
            const float* matrix = matrix_data + joint * 16;
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
    if (invalid_joint >= 0) {
        PyErr_Format(
            PyExc_ValueError,
            "skin influence references joint %d, but only %zd matrices were provided",
            invalid_joint,
            matrix_count);
        goto fail;
    }

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

static int64_t quantize_normal(float value) {
    // Match float32-to-int64 truncation, including nonfinite/overflow sentinels.
    if (!isfinite(value) || value < -0x1p63f || value >= 0x1p63f)
        return INT64_MIN;
    return (int64_t)value;
}

static PyObject* recalculate_normals(PyObject* self, PyObject* args) {
    PyObject *positions_obj, *triangles_obj, *redirects_obj;
    Py_buffer positions = {0}, triangles = {0}, redirects = {0};
    PyObject* output_bytes = NULL;
    double* accumulated = NULL;
    uint64_t* quantized = NULL;
    double quantization_scale = 1023.0;
    const int dispatch = PyTuple_Size(args) == 4;

    if (!PyArg_ParseTuple(
            args,
            "OOO|d",
            &positions_obj,
            &triangles_obj,
            &redirects_obj,
            &quantization_scale))
        return NULL;
    if (!isfinite(quantization_scale) || quantization_scale <= 0.0) {
        PyErr_SetString(PyExc_ValueError, "normal quantization scale must be positive and finite");
        return NULL;
    }
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
    if (dispatch)
        quantized = PyMem_Calloc((size_t)vertex_count * 3, sizeof(uint64_t));
    else
        accumulated = PyMem_Calloc((size_t)vertex_count * 3, sizeof(double));
    if (!output_bytes)
        goto fail;
    if (!accumulated && !quantized) {
        PyErr_NoMemory();
        goto fail;
    }

    const float* position_data = (const float*)positions.buf;
    float* output = (float*)PyBytes_AsString(output_bytes);
    int invalid_index = 0;
    Py_BEGIN_ALLOW_THREADS
    for (Py_ssize_t index = 0; index < triangle_index_count; index += 3) {
        const Py_ssize_t a = triangle_data[index];
        const Py_ssize_t b = triangle_data[index + 1];
        const Py_ssize_t c = triangle_data[index + 2];
        if (a < 0 || a >= vertex_count || b < 0 || b >= vertex_count
                || c < 0 || c >= vertex_count) {
            invalid_index = 1;
            break;
        }
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
        } else if (dispatch) {
            nx = ny = nz = 0.0f;
        }
        if (dispatch) {
            const int64_t values[3] = {
                quantize_normal(nx * (float)quantization_scale),
                quantize_normal(ny * (float)quantization_scale),
                quantize_normal(nz * (float)quantization_scale)
            };
            const Py_ssize_t vertices[3] = {a, b, c};
            for (int corner = 0; corner < 3; ++corner)
                for (int axis = 0; axis < 3; ++axis)
                    quantized[vertices[corner] * 3 + axis] += (uint64_t)values[axis];
            continue;
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
    for (Py_ssize_t vertex = 0; vertex < vertex_count && !invalid_index; ++vertex) {
        const Py_ssize_t redirect = redirect_data[vertex];
        if (redirect < 0 || redirect >= vertex_count) {
            invalid_index = 1;
            break;
        }
        const Py_ssize_t source = redirect * 3;
        float nx, ny, nz;
        if (dispatch) {
            int64_t values[3];
            memcpy(values, quantized + source, sizeof(values));
            nx = (float)values[0];
            ny = (float)values[1];
            nz = (float)values[2];
        } else {
            nx = (float)accumulated[source];
            ny = (float)accumulated[source + 1];
            nz = (float)accumulated[source + 2];
        }
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
    if (invalid_index) {
        PyErr_SetString(PyExc_ValueError, "normal index is outside the vertex buffer");
        goto fail;
    }

    PyMem_Free(accumulated);
    PyMem_Free(quantized);
    PyBuffer_Release(&positions);
    PyBuffer_Release(&triangles);
    PyBuffer_Release(&redirects);
    return output_bytes;

fail:
    PyMem_Free(accumulated);
    PyMem_Free(quantized);
    Py_XDECREF(output_bytes);
    if (positions.obj) PyBuffer_Release(&positions);
    if (triangles.obj) PyBuffer_Release(&triangles);
    if (redirects.obj) PyBuffer_Release(&redirects);
    return NULL;
}

static int moved(const char* record, const float* was) {
    const float* p = (const float*)record;
    return p[0] != was[0] || p[1] != was[1] || p[2] != was[2];
}

static void keep_lengths(char* line, Py_ssize_t stride, int64_t count, const float* before) {
    int64_t first = 0;
    while (first < count && !moved(line + first * stride, before + 3 * first)) ++first;
    if (first < 1) first = 1;
    if (first >= count) return;
    float held[3];
    memcpy(held, line + (first - 1) * stride, sizeof held);
    for (int64_t k = first; k < count; ++k) {
        const float* previous = (const float*)(line + (k - 1) * stride);
        const float* was = before + 3 * k;
        float* p = (float*)(line + k * stride);
        const float now[3] = {p[0], p[1], p[2]};
        float x = now[0] - previous[0], y = now[1] - previous[1], z = now[2] - previous[2];
        float norm = sqrtf(x * x + y * y + z * z);
        if (norm <= 1e-12f) {
            x = now[0] - held[0], y = now[1] - held[1], z = now[2] - held[2];
            norm = sqrtf(x * x + y * y + z * z);
        }
        const float sx = was[0] - was[-3], sy = was[1] - was[-2], sz = was[2] - was[-1];
        const float scale = sqrtf(sx * sx + sy * sy + sz * sz) / (norm < 1e-12f ? 1e-12f : norm);
        p[0] = previous[0] + x * scale;
        p[1] = previous[1] + y * scale;
        p[2] = previous[2] + z * scale;
        memcpy(held, now, sizeof held);
    }
}

static PyObject* move_keeping_lengths(PyObject* self, PyObject* args) {
    Py_buffer vertices = {0}, starts = {0}, strands = {0}, rows = {0}, positions = {0};
    Py_ssize_t stride;
    PyObject* result = NULL;
    float* before = NULL;
    if (!PyArg_ParseTuple(args, "w*ny*y*y*y*", &vertices, &stride, &starts, &strands, &rows, &positions))
        return NULL;
    const int64_t* start = (const int64_t*)starts.buf;
    const int64_t* strand = (const int64_t*)strands.buf;
    const int64_t* row = (const int64_t*)rows.buf;
    const Py_ssize_t start_count = starts.len / (Py_ssize_t)sizeof(int64_t);
    const Py_ssize_t strand_count = strands.len / (Py_ssize_t)sizeof(int64_t);
    const Py_ssize_t row_count = rows.len / (Py_ssize_t)sizeof(int64_t);
    if (stride < 3 * (Py_ssize_t)sizeof(float) || vertices.len % stride || starts.len % sizeof(int64_t)
            || strands.len % sizeof(int64_t) || rows.len % sizeof(int64_t)
            || positions.len != row_count * 3 * (Py_ssize_t)sizeof(float)) {
        PyErr_SetString(PyExc_ValueError, "invalid strand move buffers");
        goto done;
    }
    const int64_t vertex_count = vertices.len / stride;
    int64_t total = 0;
    for (Py_ssize_t i = 0; i < strand_count; ++i) {
        const int64_t s = strand[i];
        if (s < 0 || s >= start_count - 1 || start[s] < 0 || start[s + 1] < start[s] || start[s + 1] > vertex_count) {
            PyErr_SetString(PyExc_ValueError, "a strand lies outside the vertices");
            goto done;
        }
        total += start[s + 1] - start[s];
    }
    for (Py_ssize_t i = 0; i < row_count; ++i) {
        if (row[i] < 0 || row[i] >= vertex_count) {
            PyErr_SetString(PyExc_ValueError, "a vertex moved lies outside the vertices");
            goto done;
        }
    }
    if (!(before = PyMem_RawMalloc((size_t)total * 3 * sizeof(float)))) {
        PyErr_NoMemory();
        goto done;
    }
    Py_BEGIN_ALLOW_THREADS
    char* base = (char*)vertices.buf;
    float* kept = before;
    for (Py_ssize_t i = 0; i < strand_count; ++i)
        for (int64_t k = start[strand[i]]; k < start[strand[i] + 1]; ++k, kept += 3)
            memcpy(kept, base + k * stride, 3 * sizeof(float));
    for (Py_ssize_t i = 0; i < row_count; ++i)
        memcpy(base + row[i] * stride, (const float*)positions.buf + 3 * i, 3 * sizeof(float));
    kept = before;
    for (Py_ssize_t i = 0; i < strand_count; ++i) {
        const int64_t count = start[strand[i] + 1] - start[strand[i]];
        keep_lengths(base + start[strand[i]] * stride, stride, count, kept);
        kept += 3 * count;
    }
    Py_END_ALLOW_THREADS
    result = Py_NewRef(Py_None);
 done:
    PyMem_RawFree(before);
    PyBuffer_Release(&vertices);
    PyBuffer_Release(&starts);
    PyBuffer_Release(&strands);
    PyBuffer_Release(&rows);
    PyBuffer_Release(&positions);
    return result;
}

/* See docs/internals/strand-editing.md. Strands drape independently, so callers split them among threads. */

/* A drape collision shape, DRAPE_WIDTH doubles: kind, bounding box (a plane's is infinite), then a capsule's ends and
 * radii (a sphere repeats them), a box's centre, unit axes and half extents, or a plane's unit normal and offset
 * (inside behind it); the last double is 1 for a shoulder hair slides off. */
#define DRAPE_WIDTH 24
enum { DRAPE_CAPSULE = 0, DRAPE_BOX = 1, DRAPE_PLANE = 2 };

/* ``drape_strands``' params, in ``growing._PARAMS`` order. */
enum {
    P_KEPT, P_STEPS, P_GROWING, P_START, P_GRAVITY, P_SMOOTH = P_GRAVITY + 3, P_REACH, P_SLIDE, P_SLIDE_FRONT,
    P_ON_SHOULDER, P_BASE,
    P_THICKENS, P_THICKEST, P_DEEPENS, P_LOWER, P_TAPER, P_NAPE, P_PART, P_FACE_MIDDLE, P_FACE_SIDE, P_FACE_LOW,
    P_FACE_HIGH, P_FACE_FRONT, P_COMB, P_ON_HEAD, P_COMB_LOW, P_COMB_REAR, P_COMB_LEAVE, P_GRID,
    P_ORIGIN = P_GRID + 3, P_CELL = P_ORIGIN + 3, P_FLOOR, P_SETTLE, P_WINDOW, P_CALM, P_FOLD, P_BLOCK, P_COUNT
};
/* A drape strand row: kept and grown segment lengths, parting side, depth scale and extra (``drape_room``), and comb
 * direction (sideways +x, back -z) with its ramp length (0 for immediate). */
#define DRAPE_STRAND 8

typedef struct {
    const double* p;
    const double* styled;
    const double* shapes;
    Py_ssize_t shape_count, points;
    /* The distance grid, and each block's least distance over its cells' corners; a block is P_BLOCK = 1 << ``shift``
     * cells a side. */
    const float* grid;
    const float* blocks;
    int64_t size[3], blocks_size[3];
    int shift;
    int kept;
} Drape;

/* Depth of ``q`` inside ``s`` (negative outside); ``way`` gets the unit way out. Matches ``clsp.geometry.depth``. */
static double drape_depth(const double* s, const double* q, double* way) {
    if (s[0] == DRAPE_PLANE) {
        const double* n = s + 7;
        way[0] = n[0], way[1] = n[1], way[2] = n[2];
        return -(q[0] * n[0] + q[1] * n[1] + q[2] * n[2] + s[10]);
    }
    if (s[0] == DRAPE_BOX) {
        const double* c = s + 7;
        double best = INFINITY, sign = 1;
        int axis = 0;
        for (int i = 0; i < 3; ++i) {
            const double* u = s + 10 + 3 * i;
            const double local = (q[0] - c[0]) * u[0] + (q[1] - c[1]) * u[1] + (q[2] - c[2]) * u[2];
            const double room = s[19 + i] - fabs(local);
            if (room < best)
                best = room, axis = i, sign = local < 0 ? -1.0 : 1.0;
        }
        for (int k = 0; k < 3; ++k)
            way[k] = s[10 + 3 * axis + k] * sign;
        return best;
    }
    const double* a = s + 7;
    const double* b = s + 11;
    const double span[3] = {b[0] - a[0], b[1] - a[1], b[2] - a[2]};
    const double length = span[0] * span[0] + span[1] * span[1] + span[2] * span[2];
    double t = 0;
    if (length > 0) {
        t = ((q[0] - a[0]) * span[0] + (q[1] - a[1]) * span[1] + (q[2] - a[2]) * span[2]) / length;
        t = t < 0 ? 0 : t > 1 ? 1 : t;
    }
    const double away[3] = {q[0] - a[0] - t * span[0], q[1] - a[1] - t * span[1], q[2] - a[2] - t * span[2]};
    const double distance = sqrt(away[0] * away[0] + away[1] * away[1] + away[2] * away[2]);
    if (distance > 1e-12) {
        for (int k = 0; k < 3; ++k)
            way[k] = away[k] / distance;
    } else {
        /* Matches geometry._basis(axis)[0]. */
        double axis[3] = {1, 1, 1};
        if (length > 0)
            axis[0] = span[0], axis[1] = span[1], axis[2] = span[2];
        const int along_x = fabs(axis[0]) >= 0.9 * sqrt(axis[0] * axis[0] + axis[1] * axis[1] + axis[2] * axis[2]);
        const double other[3] = {along_x ? 0.0 : 1.0, along_x ? 1.0 : 0.0, 0};
        way[0] = axis[1] * other[2] - axis[2] * other[1];
        way[1] = axis[2] * other[0] - axis[0] * other[2];
        way[2] = axis[0] * other[1] - axis[1] * other[0];
        const double norm = sqrt(way[0] * way[0] + way[1] * way[1] + way[2] * way[2]);
        for (int k = 0; k < 3; ++k)
            way[k] /= norm;
    }
    return s[10] + (s[14] - s[10]) * t - distance;
}

static double drape_node(const Drape* d, int64_t i, int64_t j, int64_t k) {
    return d->grid[(i * d->size[1] + j) * d->size[2] + k];
}

/* Central differences, one-sided at the grid's sides, as ``np.gradient``. */
static void drape_node_gradient(const Drape* d, const int64_t* at, double cell, double* out) {
    for (int axis = 0; axis < 3; ++axis) {
        int64_t low[3] = {at[0], at[1], at[2]}, high[3] = {at[0], at[1], at[2]};
        double span = 2 * cell;
        if (at[axis] > 0)
            --low[axis];
        else
            span = cell;
        if (at[axis] < d->size[axis] - 1)
            ++high[axis];
        else
            span = cell;
        out[axis] = (drape_node(d, high[0], high[1], high[2]) - drape_node(d, low[0], low[1], low[2])) / span;
    }
}

static int drape_grid(const Drape* d, const double* q, double room, double* distance, double* gradient) {
    const double* origin = d->p + P_ORIGIN;
    const double cell = d->p[P_CELL], across = 1 / cell;
    int64_t base[3];
    double share[3];
    for (int k = 0; k < 3; ++k) {
        const double g = (q[k] - origin[k]) * across;
        if (!(g >= 0) || g >= (double)(d->size[k] - 1))
            return 0;
        base[k] = (int64_t)g;
        share[k] = g - (double)base[k];
    }
    const int b = d->shift;
    if (d->blocks[((base[0] >> b) * d->blocks_size[1] + (base[1] >> b)) * d->blocks_size[2] + (base[2] >> b)] >= room)
        return 0;
    double value = 0;
    if (gradient)
        gradient[0] = gradient[1] = gradient[2] = 0;
    for (int corner = 0; corner < 8; ++corner) {
        const int64_t at[3] = {base[0] + ((corner >> 2) & 1), base[1] + ((corner >> 1) & 1), base[2] + (corner & 1)};
        const double weight = (corner & 4 ? share[0] : 1 - share[0]) * (corner & 2 ? share[1] : 1 - share[1])
                              * (corner & 1 ? share[2] : 1 - share[2]);
        value += weight * drape_node(d, at[0], at[1], at[2]);
        if (gradient) {
            double node[3];
            drape_node_gradient(d, at, cell, node);
            for (int k = 0; k < 3; ++k)
                gradient[k] += weight * node[k];
        }
    }
    *distance = value;
    return 1;
}

static void drape_follow(double* chain, const double* segment, Py_ssize_t points) {
    for (Py_ssize_t k = 1; k < points; ++k) {
        double* q = chain + 3 * k;
        const double* previous = q - 3;
        const double way[3] = {q[0] - previous[0], q[1] - previous[1], q[2] - previous[2]};
        double length = sqrt(way[0] * way[0] + way[1] * way[1] + way[2] * way[2]);
        const double scale = segment[k - 1] / (length > 1e-9 ? length : 1e-9);
        for (int i = 0; i < 3; ++i)
            q[i] = previous[i] + way[i] * scale;
    }
}

static void drape_unfold(double* chain, const double* segment, Py_ssize_t points, Py_ssize_t first, double fold) {
    for (Py_ssize_t k = first < 1 ? 1 : first; k < points - 1; ++k) {
        const double* q = chain + 3 * k;
        const double in[3] = {q[0] - q[-3], q[1] - q[-2], q[2] - q[-1]};
        const double out[3] = {q[3] - q[0], q[4] - q[1], q[5] - q[2]};
        const double turn = in[0] * out[0] + in[1] * out[1] + in[2] * out[2];
        if (turn >= 0 && fold <= 0)
            continue;
        const double a = sqrt(in[0] * in[0] + in[1] * in[1] + in[2] * in[2]);
        const double b = sqrt(out[0] * out[0] + out[1] * out[1] + out[2] * out[2]);
        if (a <= 1e-9 || b <= 1e-9 || turn >= fold * a * b)
            continue;
        for (Py_ssize_t j = k + 1; j < points; ++j)
            for (int i = 0; i < 3; ++i)
                chain[3 * j + i] = chain[3 * j - 3 + i] + in[i] / a * segment[j - 1];
        return;
    }
}

static void drape_push(double* q, const double* way, double amount, double reach) {
    amount = amount < reach ? amount : reach;
    for (int k = 0; k < 3; ++k)
        q[k] += way[k] * amount;
}

static int drape_near(const double* s, const double* q, double room) {
    return q[0] >= s[1] - room && q[1] >= s[2] - room && q[2] >= s[3] - room && q[0] <= s[4] + room
           && q[1] <= s[5] + room && q[2] <= s[6] + room;
}

static double drape_room(const double* p, const double* q, double root, const double* strand, double along) {
    double hangs = root - q[1];
    hangs = hangs > 0 ? hangs * p[P_THICKENS] : 0;
    const double thicker = hangs < p[P_THICKEST] ? hangs : p[P_THICKEST];
    double deepened = along / p[P_DEEPENS];
    deepened = deepened < 0 ? 0 : deepened > 1 ? 1 : deepened;
    const double room = (p[P_BASE] + thicker) * strand[3] + strand[4] * deepened;
    double below = (p[P_NAPE] - q[1]) / p[P_TAPER];
    below = below < 0 ? 0 : below > 1 ? 1 : below;
    return p[P_BASE] + (room - p[P_BASE]) * (1 - below * (1 - p[P_LOWER]));
}

static void drape_point(const Drape* d, double* q, const int32_t* near, int32_t count, double root,
                        const double* strand, double along) {
    const double* p = d->p;
    const double reach = p[P_REACH], side = strand[2];
    const double room = drape_room(p, q, root, strand, along);
    double way[3];
    for (int32_t i = 0; i < count; ++i) {
        const double* s = d->shapes + near[i] * DRAPE_WIDTH;
        if (!drape_near(s, q, room))
            continue;
        const double depth = drape_depth(s, q, way);
        if (depth > -room)
            drape_push(q, way, depth + room, reach);
    }
    for (int32_t i = 0; i < count; ++i) {
        const double* s = d->shapes + near[i] * DRAPE_WIDTH;
        if (s[DRAPE_WIDTH - 1] == 0 || !drape_near(s, q, p[P_ON_SHOULDER]))
            continue;
        const double depth = drape_depth(s, q, way);
        const double* a = s + 7;
        const double span[3] = {s[11] - a[0], s[12] - a[1], s[13] - a[2]};
        const double length = span[0] * span[0] + span[1] * span[1] + span[2] * span[2];
        double t = length > 0 ? ((q[0] - a[0]) * span[0] + (q[1] - a[1]) * span[1] + (q[2] - a[2]) * span[2])
                                / length : 0;
        t = t < 0 ? 0 : t > 1 ? 1 : t;
        const double ridge[3] = {a[0] + t * span[0], a[1] + t * span[1], a[2] + t * span[2]};
        if (-depth < p[P_ON_SHOULDER] && q[1] > ridge[1])
            q[2] += (q[2] - ridge[2] > p[P_SLIDE_FRONT] ? 1 : -1) * p[P_SLIDE];
    }
    if (p[P_PART] != 0 && q[1] > p[P_FACE_LOW] && q[1] < p[P_FACE_HIGH] && q[2] > p[P_FACE_FRONT]) {
        const double width = p[P_FACE_SIDE] + room;
        const double off = q[0] - p[P_FACE_MIDDLE];
        if (fabs(off) < width) {
            double step = side * width - off;
            step = step < -reach ? -reach : step > reach ? reach : step;
            q[0] += step;
        }
    }
    double distance, gradient[3];
    if (d->grid && q[1] > p[P_FLOOR] && drape_grid(d, q, room, &distance, NULL) && distance < room) {
        drape_grid(d, q, room, &distance, gradient);
        const double norm = sqrt(gradient[0] * gradient[0] + gradient[1] * gradient[1] + gradient[2] * gradient[2]);
        if (norm > 1e-9) {
            for (int k = 0; k < 3; ++k)
                gradient[k] /= norm;
            drape_push(q, gradient, room - distance, reach);
        }
    }
}

static void drape_comb(const Drape* d, double* q, double sideways, double back, double ramp, double along) {
    const double* p = d->p;
    if (!d->grid || q[1] <= p[P_COMB_LOW] || q[2] <= p[P_COMB_REAR])
        return;
    if (ramp > 0) {
        const double share = along < ramp ? along / ramp : 1;
        sideways *= p[P_COMB_LEAVE] + (1 - p[P_COMB_LEAVE]) * share;
    }
    double distance, normal[3];
    if (!drape_grid(d, q, p[P_ON_HEAD], &distance, NULL) || distance >= p[P_ON_HEAD])
        return;
    drape_grid(d, q, p[P_ON_HEAD], &distance, normal);
    const double norm = sqrt(normal[0] * normal[0] + normal[1] * normal[1] + normal[2] * normal[2]);
    if (norm <= 1e-9)
        return;
    double way[3] = {sideways, 0, -back};
    const double across = (way[0] * normal[0] + way[1] * normal[1] + way[2] * normal[2]) / (norm * norm);
    for (int k = 0; k < 3; ++k)
        way[k] -= normal[k] * across;
    const double length = sqrt(way[0] * way[0] + way[1] * way[1] + way[2] * way[2]);
    if (length <= 1e-9)
        return;
    for (int k = 0; k < 3; ++k)
        q[k] += way[k] / length * p[P_COMB];
}

#define DRAPE_GROUP 8

static int32_t drape_shapes_near(const Drape* d, const double* chain, Py_ssize_t first, Py_ssize_t last,
                                 const uint8_t* use, double room, int32_t* near) {
    double low[3] = {INFINITY, INFINITY, INFINITY}, high[3] = {-INFINITY, -INFINITY, -INFINITY};
    for (Py_ssize_t k = first; k < last; ++k)
        for (int i = 0; i < 3; ++i)
            low[i] = fmin(low[i], chain[3 * k + i]), high[i] = fmax(high[i], chain[3 * k + i]);
    int32_t count = 0;
    for (Py_ssize_t n = 0; n < d->shape_count; ++n) {
        const double* s = d->shapes + n * DRAPE_WIDTH;
        if (use[n] && high[0] >= s[1] - room && high[1] >= s[2] - room && high[2] >= s[3] - room
                && low[0] <= s[4] + room && low[1] <= s[5] + room && low[2] <= s[6] + room)
            near[count++] = (int32_t)n;
    }
    return count;
}

/* ``anchor``, ``held``, ``before``, ``segment`` and ``near`` are scratch space. */
static int64_t drape_one(const Drape* d, double* chain, const double* strand, const uint8_t* use, double* anchor,
                         double* held, double* before, double* segment, int32_t* near) {
    const double* p = d->p;
    const Py_ssize_t points = d->points, size = 3 * points;
    const int kept = d->kept;
    const int64_t steps = (int64_t)p[P_STEPS], window = (int64_t)p[P_WINDOW];
    memcpy(anchor, chain, (size_t)size * sizeof(double));
    for (Py_ssize_t k = 0; k < points - 1; ++k)
        segment[k] = strand[0];
    int64_t calm = 0, step = 0, since = -1;
    while (step < steps) {
        double growth = p[P_START] + (double)step / p[P_GROWING];
        growth = growth < 1 ? growth : 1;
        for (Py_ssize_t k = kept - 1; k < points - 1; ++k)
            segment[k] = strand[1] * growth;
        const int grown = (double)step >= p[P_GROWING];
        if (grown && since < 0) {
            memcpy(before, chain, (size_t)size * sizeof(double));
            since = step;
        }
        for (Py_ssize_t k = 1; k < points; ++k)
            for (int i = 0; i < 3; ++i)
                chain[3 * k + i] += p[P_GRAVITY + i];
        if (strand[5] != 0 || strand[6] != 0) {
            double along = 0;
            for (Py_ssize_t k = 1; k < points; ++k) {
                along += segment[k - 1];
                drape_comb(d, chain + 3 * k, strand[5], strand[6], strand[7], along);
            }
        }
        for (Py_ssize_t k = 0; k < points; ++k) {
            const double weight = d->styled[k];
            if (weight != 0)
                for (int i = 0; i < 3; ++i)
                    chain[3 * k + i] += weight * (anchor[3 * k + i] - chain[3 * k + i]);
        }
        memcpy(held, chain, (size_t)size * sizeof(double));
        for (Py_ssize_t k = 1; k < points - 1; ++k)
            for (int i = 0; i < 3; ++i)
                chain[3 * k + i] += p[P_SMOOTH] * (0.5 * (held[3 * k - 3 + i] + held[3 * k + 3 + i]) - held[3 * k + i]);
        drape_follow(chain, segment, points);
        /* A point moves at most one reach per push, so shapes beyond its room plus a few reaches cannot catch it. */
        const double reach = fmax((p[P_BASE] + p[P_THICKEST]) * fmax(strand[3], 1) + strand[4], p[P_ON_SHOULDER])
                             + 4 * p[P_REACH];
        double along = 0;
        for (Py_ssize_t first = 1; first < points; first += DRAPE_GROUP) {
            const Py_ssize_t last = first + DRAPE_GROUP < points ? first + DRAPE_GROUP : points;
            const int32_t count = drape_shapes_near(d, chain, first, last, use, reach, near);
            for (Py_ssize_t k = first; k < last; ++k) {
                along += segment[k - 1];
                drape_point(d, chain + 3 * k, near, count, chain[1], strand, along);
            }
        }
        drape_follow(chain, segment, points);
        drape_unfold(chain, segment, points, kept, p[P_FOLD]);
        ++step;
        if (since >= 0 && step - since >= window) {
            double most = 0;
            for (Py_ssize_t i = 0; i < size; ++i)
                most = fmax(most, fabs(chain[i] - before[i]));
            calm = most < p[P_SETTLE] ? calm + 1 : 0;
            if ((double)calm >= p[P_CALM])
                break;
            memcpy(before, chain, (size_t)size * sizeof(double));
            since = step;
        }
    }
    return step;
}

static PyObject* drape_strands(PyObject* self, PyObject* args) {
    Py_buffer chains = {0}, strands = {0}, use = {0}, shapes = {0}, styled = {0}, grid = {0}, blocks = {0};
    Py_buffer params = {0};
    PyObject* result = NULL;
    double* room = NULL;
    int32_t* near = NULL;
    if (!PyArg_ParseTuple(args, "w*y*y*y*y*y*y*y*", &chains, &strands, &use, &shapes, &styled, &grid, &blocks,
                          &params))
        return NULL;
    Drape d = {0};
    d.p = (const double*)params.buf;
    d.styled = (const double*)styled.buf;
    d.shapes = (const double*)shapes.buf;
    d.points = styled.len / (Py_ssize_t)sizeof(double);
    d.shape_count = shapes.len / (Py_ssize_t)(DRAPE_WIDTH * sizeof(double));
    const Py_ssize_t count = strands.len / (Py_ssize_t)(DRAPE_STRAND * sizeof(double));
    if (params.len != P_COUNT * (Py_ssize_t)sizeof(double) || styled.len % sizeof(double) || d.points < 2
            || shapes.len != d.shape_count * (Py_ssize_t)(DRAPE_WIDTH * sizeof(double))
            || strands.len != count * (Py_ssize_t)(DRAPE_STRAND * sizeof(double))
            || chains.len != count * d.points * 3 * (Py_ssize_t)sizeof(double) || use.len != count * d.shape_count) {
        PyErr_SetString(PyExc_ValueError, "invalid drape buffers");
        goto done;
    }
    d.kept = (int)d.p[P_KEPT];
    if (d.kept < 1 || d.kept > d.points || !(d.p[P_GROWING] > 0) || !(d.p[P_CELL] > 0) || !(d.p[P_TAPER] > 0)
            || !(d.p[P_WINDOW] >= 1) || !(d.p[P_DEEPENS] > 0)) {
        PyErr_SetString(PyExc_ValueError, "invalid drape settings");
        goto done;
    }
    int64_t cells = 1;
    for (int k = 0; k < 3; ++k) {
        d.size[k] = (int64_t)d.p[P_GRID + k];
        cells *= d.size[k];
    }
    if (grid.len) {
        const int64_t block = (int64_t)d.p[P_BLOCK];
        while (d.shift < 30 && ((int64_t)1 << d.shift) < block)
            ++d.shift;
        int64_t block_cells = 1;
        for (int k = 0; k < 3; ++k) {
            d.blocks_size[k] = d.size[k] > 1 ? ((d.size[k] - 2) >> d.shift) + 1 : 0;
            block_cells *= d.blocks_size[k];
        }
        if (d.size[0] < 2 || d.size[1] < 2 || d.size[2] < 2 || grid.len != cells * (Py_ssize_t)sizeof(float)
                || block != ((int64_t)1 << d.shift) || blocks.len != block_cells * (Py_ssize_t)sizeof(float)) {
            PyErr_SetString(PyExc_ValueError, "the drape's grid does not match its size");
            goto done;
        }
        d.grid = (const float*)grid.buf;
        d.blocks = (const float*)blocks.buf;
    }
    PyObject* steps = PyBytes_FromStringAndSize(NULL, count * (Py_ssize_t)sizeof(int64_t));
    if (!steps)
        goto done;
    room = PyMem_RawMalloc((size_t)(10 * d.points) * sizeof(double));
    near = PyMem_RawMalloc((size_t)(d.shape_count + 1) * sizeof(int32_t));
    if (!room || !near) {
        Py_DECREF(steps);
        PyErr_NoMemory();
        goto done;
    }
    int64_t* taken = (int64_t*)PyBytes_AS_STRING(steps);
    Py_BEGIN_ALLOW_THREADS
    double* chain = (double*)chains.buf;
    for (Py_ssize_t s = 0; s < count; ++s)
        taken[s] = drape_one(&d, chain + s * 3 * d.points, (const double*)strands.buf + DRAPE_STRAND * s,
                             (const uint8_t*)use.buf + s * d.shape_count, room, room + 3 * d.points,
                             room + 6 * d.points, room + 9 * d.points, near);
    Py_END_ALLOW_THREADS
    result = steps;
 done:
    PyMem_RawFree(room);
    PyMem_RawFree(near);
    PyBuffer_Release(&chains);
    PyBuffer_Release(&strands);
    PyBuffer_Release(&use);
    PyBuffer_Release(&shapes);
    PyBuffer_Release(&styled);
    PyBuffer_Release(&grid);
    PyBuffer_Release(&blocks);
    PyBuffer_Release(&params);
    return result;
}

typedef struct {
    double x;
    int64_t number;
} Ordered;

static int compare_ordered(const void* a, const void* b) {
    const Ordered* p = (const Ordered*)a;
    const Ordered* q = (const Ordered*)b;
    return p->x < q->x ? -1 : p->x > q->x ? 1 : (p->number > q->number) - (p->number < q->number);
}

/* ``nearest_points(points, k)``: ``points`` float64 xyz; for each point, its k nearest other points (itself excluded)
 * as int64 bytes (points x k), nearest first, ties by lower number. */
static PyObject* nearest_points(PyObject* self, PyObject* args) {
    Py_buffer view = {0};
    Py_ssize_t k;
    PyObject* result = NULL;
    Ordered* order = NULL;
    double* found = NULL;
    if (!PyArg_ParseTuple(args, "y*n", &view, &k))
        return NULL;
    const Py_ssize_t count = view.len / (Py_ssize_t)(3 * sizeof(double));
    if (view.len != count * (Py_ssize_t)(3 * sizeof(double)) || k < 1 || k >= count) {
        PyErr_SetString(PyExc_ValueError, "nearest_points needs xyz float64 points and 0 < k < their count");
        goto done;
    }
    const double* points = (const double*)view.buf;
    for (Py_ssize_t i = 0; i < 3 * count; ++i) {
        if (!isfinite(points[i])) {
            PyErr_SetString(PyExc_ValueError, "nearest_points needs finite points");
            goto done;
        }
    }
    result = PyBytes_FromStringAndSize(NULL, count * k * (Py_ssize_t)sizeof(int64_t));
    order = PyMem_RawMalloc((size_t)count * sizeof(Ordered));
    found = PyMem_RawMalloc((size_t)k * sizeof(double));
    if (!result || !order || !found) {
        Py_CLEAR(result);
        if (!PyErr_Occurred())
            PyErr_NoMemory();
        goto done;
    }
    int64_t* out = (int64_t*)PyBytes_AS_STRING(result);
    Py_BEGIN_ALLOW_THREADS
    for (Py_ssize_t i = 0; i < count; ++i)
        order[i].x = points[3 * i], order[i].number = i;
    qsort(order, (size_t)count, sizeof(Ordered), compare_ordered);
    for (Py_ssize_t r = 0; r < count; ++r) {
        const int64_t self_number = order[r].number;
        const double* p = points + 3 * self_number;
        int64_t* best = out + self_number * k;
        Py_ssize_t held = 0;
        for (int direction = -1; direction <= 1; direction += 2) {
            for (Py_ssize_t j = r + direction; j >= 0 && j < count; j += direction) {
                const double gap = order[j].x - p[0];
                if (held == k && gap * gap > found[k - 1])
                    break;
                const int64_t number = order[j].number;
                const double* q = points + 3 * number;
                const double distance = gap * gap + (q[1] - p[1]) * (q[1] - p[1]) + (q[2] - p[2]) * (q[2] - p[2]);
                if (held == k && (distance > found[k - 1] || (distance == found[k - 1] && number > best[k - 1])))
                    continue;
                Py_ssize_t at = held < k ? held++ : k - 1;
                while (at > 0 && (found[at - 1] > distance || (found[at - 1] == distance && best[at - 1] > number))) {
                    found[at] = found[at - 1], best[at] = best[at - 1];
                    --at;
                }
                found[at] = distance, best[at] = number;
            }
        }
    }
    Py_END_ALLOW_THREADS
 done:
    PyMem_RawFree(order);
    PyMem_RawFree(found);
    PyBuffer_Release(&view);
    return result;
}

/* MPLY meshlet chunks: docs/internals/mesh-preview.md, MPLY chunks. */

enum {
    MPLY_CENTERED, MPLY_DUAL, MPLY_WILDS_LAYOUT, MPLY_LEGACY_BITS, MPLY_PRAGMATA_BITS, MPLY_WILDS_HANDEDNESS,
    MPLY_PADDING_ZERO, MPLY_FULLY_DECODED, MPLY_SKIN_BEFORE_TEXCOORD3, MPLY_OPTIONS
};

/* Each chunk's integers, in this order. */
enum {
    C_VERTS, C_FACES, C_MATERIAL, C_PART, C_FLAGS, C_AABB, C_FACE_PAD = C_AABB + 6, C_FACE_PAD_SIZE, C_POS_PAD,
    C_POS_PAD_SIZE, C_MASK, C_MASK_SIZE, C_SKIN, C_SKIN_SIZE, C_SEMANTIC_END, C_END, C_NORMAL_ROWS, C_TANGENT_ROWS,
    C_TANGENT_W_ROWS, C_UV0_ROWS, C_UV1_ROWS, C_UV2_ROWS, C_COLORS, C_MASK_BITS, C_FIELDS
};

typedef struct {
    int64_t ints[C_FIELDS];
    double center[3], aabb[6];
    unsigned position_size;
    int no_tangent, wilds_tangents, compressed_normal, compressed_uv1, use_uv1, compressed_uv2, use_uv2,
        use_colors, compressed_colors, compressed_skin, has_skin, mode;
} MplyChunk;

/* numpy's half to double (``npy_halfbits_to_doublebits``), so infinities and NaN payloads keep their bits. */
static double half_to_double(uint16_t half) {
    const uint64_t sign = (uint64_t)(half & 0x8000) << 48;
    const uint16_t exponent = half & 0x7C00;
    uint64_t bits;
    if (exponent == 0x7C00) {
        bits = sign | 0x7FF0000000000000ULL | ((uint64_t)(half & 0x03FF) << 42);
    } else if (exponent == 0) {
        uint16_t mantissa = half & 0x03FF;
        if (!mantissa) {
            bits = sign;
        } else {
            int shift = -1;
            do {
                ++shift;
                mantissa <<= 1;
            } while (!(mantissa & 0x0400));
            bits = sign | ((uint64_t)(1023 - 15 - shift) << 52) | ((uint64_t)(mantissa & 0x03FF) << 42);
        }
    } else {
        bits = sign | ((uint64_t)((half & 0x7FFF) + 0xFC000) << 42);
    }
    double value;
    memcpy(&value, &bits, 8);
    return value;
}

/* BinaryHandler.read_bytes's refusal of a read past the end. */
static int mply_read(char *failure, size_t size, Py_ssize_t *at, Py_ssize_t length, Py_ssize_t count) {
    if (length - *at < count) {
        snprintf(failure, size, "Attempted to read %zd bytes but only %zd bytes available at position %zd", count,
                 length - *at, *at);
        return 0;
    }
    *at += count;
    return 1;
}

/* A chunk's header, flags and the reach of each block; 0 with ``failure`` in the parser's words. */
static int mply_measure(const uint8_t *data, Py_ssize_t length, Py_ssize_t origin, Py_ssize_t start, Py_ssize_t end,
                        const long *options, const char *name, MplyChunk *chunk, char *failure, size_t size) {
    memset(chunk, 0, sizeof *chunk);
    int64_t *ints = chunk->ints;
    if (start < 0 || start >= length) {
        snprintf(failure, size, "Invalid MPLY chunk start %zd", start);
        return 0;
    }
    if (end >= 0 && !(start < end && end <= length)) {
        snprintf(failure, size, "Invalid MPLY chunk range [%zd, %zd)", start, end);
        return 0;
    }
    Py_ssize_t at = start;
    if (length - start < 32) {
        /* The field-by-field reads of a truncated header. */
        static const Py_ssize_t reads[] = {12, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 4};
        for (size_t i = 0; i < sizeof reads / sizeof reads[0]; ++i) {
            if (!mply_read(failure, size, &at, length, reads[i]))
                return 0;
        }
    }
    float center[3];
    uint16_t aabb[6];
    uint32_t raw;
    memcpy(center, data + start, 12);
    memcpy(aabb, data + start + 16, 12);
    memcpy(&raw, data + start + 28, 4);
    at = start + 32;
    const int verts = data[start + 12], faces = data[start + 13];
    ints[C_VERTS] = verts;
    ints[C_FACES] = faces;
    ints[C_MATERIAL] = data[start + 14];
    ints[C_PART] = data[start + 15];
    ints[C_FLAGS] = raw;
    for (int i = 0; i < 3; ++i)
        chunk->center[i] = center[i];
    for (int i = 0; i < 6; ++i)
        ints[C_AABB + i] = aabb[i];
    const int pragmata = options[MPLY_PRAGMATA_BITS] != 0;
    chunk->mode = (raw >> 14) & 0x3;
    const int wilds_handedness = options[MPLY_WILDS_HANDEDNESS] != 0;
    const int has_mask_block = wilds_handedness ? chunk->mode == 2
                                                : (!options[MPLY_LEGACY_BITS] && (raw & (1u << 15)));
    const int level = (raw >> 12) & 0x3;
    chunk->compressed_normal = raw & 1;
    chunk->compressed_uv1 = (raw >> 1) & 1;
    chunk->compressed_uv2 = (raw >> 2) & 1;
    const int compressed_uv3 = (raw >> 3) & 1;
    chunk->compressed_colors = (raw >> 4) & 1;
    chunk->no_tangent = (raw >> (pragmata ? 5 : 6)) & 1;
    const int skinned = (raw >> (pragmata ? 6 : 11)) & 1;
    chunk->compressed_skin = (raw >> (pragmata ? 7 : 5)) & 1;
    chunk->has_skin = skinned || chunk->compressed_skin;
    chunk->use_colors = (raw >> 8) & 1;
    chunk->use_uv1 = (raw >> 9) & 1;
    chunk->use_uv2 = (raw >> 10) & 1;
    chunk->wilds_tangents = wilds_handedness && chunk->no_tangent;
    chunk->position_size = level == 1 ? 4 : level >= 2 ? 3 : 6;
    /* The cluster box: its center and half extent. */
    const double unit = ldexp(1.0, (int)((raw >> 16) & 0xFF) - 143);
    for (int i = 0; i < 3; ++i) {
        chunk->aabb[i] = chunk->center[i] + (double)((int)aabb[2 * i] - 32768) * unit;
        chunk->aabb[3 + i] = (double)aabb[2 * i + 1] * unit;
    }
    /* Faces, padded to four bytes of the file. */
    if (!mply_read(failure, size, &at, length, (Py_ssize_t)faces * 3))
        return 0;
    Py_ssize_t pad = (4 - ((origin + at) % 4)) % 4;
    ints[C_FACE_PAD] = at;
    ints[C_FACE_PAD_SIZE] = pad;
    if (!mply_read(failure, size, &at, length, pad))
        return 0;
    if (!mply_read(failure, size, &at, length, (Py_ssize_t)verts * chunk->position_size))
        return 0;
    pad = (4 - ((origin + at) % 4)) % 4;
    ints[C_POS_PAD] = at;
    ints[C_POS_PAD_SIZE] = pad;
    if (!mply_read(failure, size, &at, length, pad))
        return 0;
    if (options[MPLY_PADDING_ZERO]) {
        for (Py_ssize_t i = 0; i < pad; ++i) {
            if (data[ints[C_POS_PAD] + i]) {
                snprintf(failure, size, "%s MPLY position alignment padding is nonzero", name);
                return 0;
            }
        }
    }
    const int normal_count = chunk->compressed_normal ? 1 : verts;
    if (!mply_read(failure, size, &at, length, (Py_ssize_t)normal_count * (chunk->no_tangent ? 4 : 8)))
        return 0;
    const int normal_rows = normal_count == 1 && verts > 1 ? verts : normal_count;
    ints[C_NORMAL_ROWS] = normal_rows;
    ints[C_TANGENT_ROWS] = chunk->wilds_tangents ? 0 : normal_rows;
    ints[C_TANGENT_W_ROWS] = chunk->wilds_tangents ? verts : normal_rows;
    ints[C_MASK] = -1;
    if (has_mask_block && (!pragmata || chunk->no_tangent)) {
        ints[C_MASK] = at;
        ints[C_MASK_SIZE] = ((Py_ssize_t)(verts + 31) / 32) * 4;
        if (!mply_read(failure, size, &at, length, ints[C_MASK_SIZE]))
            return 0;
        ints[C_MASK_BITS] = verts;
    }
    if (chunk->wilds_tangents && !chunk->compressed_uv1 && chunk->mode == 2 && ints[C_MASK_BITS] != verts) {
        snprintf(failure, size, "Wilds MPLY tangent mask does not match its vertex count");
        return 0;
    }
    const int uv0_count = chunk->compressed_uv1 ? 1 : verts;
    if (!mply_read(failure, size, &at, length, (Py_ssize_t)uv0_count * 4))
        return 0;
    ints[C_UV0_ROWS] = uv0_count == 1 && verts > 1 ? verts : uv0_count;
    if (chunk->use_uv1) {
        const int count = chunk->compressed_uv2 ? 1 : verts;
        if (!mply_read(failure, size, &at, length, (Py_ssize_t)count * 4))
            return 0;
        ints[C_UV1_ROWS] = count == 1 && verts > 1 ? verts : count;
    }
    ints[C_SKIN] = -1;
    const int skin_count = chunk->compressed_skin ? 1 : verts;
    if (options[MPLY_SKIN_BEFORE_TEXCOORD3] && chunk->has_skin) {
        ints[C_SKIN] = at;
        ints[C_SKIN_SIZE] = (Py_ssize_t)skin_count * 16;
        if (!mply_read(failure, size, &at, length, ints[C_SKIN_SIZE]))
            return 0;
    }
    if (chunk->use_uv2) {
        const int count = compressed_uv3 ? 1 : verts;
        if (!mply_read(failure, size, &at, length, (Py_ssize_t)count * 4))
            return 0;
        ints[C_UV2_ROWS] = count == 1 && verts > 1 ? verts : count;
    }
    if (chunk->use_colors) {
        const int count = chunk->compressed_colors ? 1 : verts;
        if (!mply_read(failure, size, &at, length, (Py_ssize_t)count * 4))
            return 0;
        ints[C_COLORS] = count == 1 && verts > 1 ? 4 * (int64_t)verts : 4 * (int64_t)count;
    }
    if (!options[MPLY_SKIN_BEFORE_TEXCOORD3] && chunk->has_skin) {
        ints[C_SKIN] = at;
        ints[C_SKIN_SIZE] = (Py_ssize_t)skin_count * 16;
        if (!mply_read(failure, size, &at, length, ints[C_SKIN_SIZE]))
            return 0;
    }
    ints[C_SEMANTIC_END] = at;
    if (end < 0)
        end = at;
    ints[C_END] = end;
    if (at > end) {
        snprintf(failure, size, "MPLY chunk data exceeds its boundary at %zd", end);
        return 0;
    }
    if (options[MPLY_FULLY_DECODED] && end > at) {
        snprintf(failure, size, "%s MPLY chunk has %zd unparsed payload bytes before boundary %zd", name, end - at, end);
        return 0;
    }
    return 1;
}

static void mply_uvs(const uint8_t *from, int count, int rows, double *to) {
    for (int row = 0; row < rows; ++row) {
        const uint8_t *item = from + 4 * (count == 1 ? 0 : row);
        to[2 * row] = half_to_double((uint16_t)(item[0] | item[1] << 8));
        to[2 * row + 1] = half_to_double((uint16_t)(item[2] | item[3] << 8));
    }
}

/* Positions, in the shaders' order of operations. */
static void mply_positions(const uint8_t *from, const MplyChunk *chunk, const long *options, double *to) {
    const int64_t *ints = chunk->ints;
    const int verts = (int)ints[C_VERTS], level = (int)((ints[C_FLAGS] >> 12) & 0x3);
    const uint32_t raw = (uint32_t)ints[C_FLAGS];
    const int low = (raw >> 16) & 0xFF, high = (raw >> 24) & 0xFF;
    for (int vertex = 0; vertex < verts; ++vertex) {
        int64_t q[3];
        if (level == 0) {
            for (int i = 0; i < 3; ++i)
                q[i] = from[6 * vertex + 2 * i] | from[6 * vertex + 2 * i + 1] << 8;
        } else if (level == 1) {
            uint32_t packed;
            memcpy(&packed, from + 4 * vertex, 4);
            q[0] = packed & 0x3FF;
            q[1] = (packed >> 10) & 0x3FF;
            q[2] = (packed >> 20) & 0x3FF;
        } else {
            for (int i = 0; i < 3; ++i)
                q[i] = from[3 * vertex + i];
        }
        double *out = to + 3 * vertex;
        if (options[MPLY_CENTERED]) {
            const double position_unit = ldexp(1.0, (options[MPLY_DUAL] ? high : low) - 143);
            const double aabb_unit = ldexp(1.0, low - 143);
            for (int i = 0; i < 3; ++i) {
                const double relative = chunk->center[i] + (double)(ints[C_AABB + 2 * i] - 32768) * aabb_unit;
                if (level == 0) {
                    const double base = options[MPLY_DUAL] ? relative : chunk->center[i];
                    out[i] = base + (double)(q[i] - 32768) * position_unit;
                } else {
                    out[i] = relative + (double)(q[i] - (level == 1 ? 512 : 128)) * position_unit;
                }
            }
            continue;
        }
        double p[3];
        for (int i = 0; i < 3; ++i)
            p[i] = level >= 2 ? (double)q[i] / 255.0 : level == 1 ? (double)q[i] * (1.0 / 1023.0) : (double)q[i] / 65535.0;
        if (!options[MPLY_WILDS_LAYOUT]) {
            const double scale = ldexp(1.0, low - 127);
            for (int i = 0; i < 3; ++i)
                out[i] = (p[i] - 0.5) * scale + chunk->center[i];
            continue;
        }
        const double scale = ldexp(1.0, high - 127), offset = ldexp(1.0, low - high);
        const double quantum = level >= 2 ? 1.0 / 256.0 : level == 1 ? 1.0 / 64.0 : 1.0;
        for (int i = 0; i < 3; ++i) {
            const double term = ((double)ints[C_AABB + 2 * i] / 65535.0 - 0.5) * offset;
            out[i] = (((p[i] - 0.5) * quantum) + term) * scale + chunk->center[i];
        }
    }
}

typedef struct {
    double *positions, *normals, *tangents, *uv0, *uv1, *uv2, *boxes;
    int64_t *faces, *normal_ws, *tangent_ws, *colors, *mask_bits;
} MplyPlanes;

static void mply_decode(const uint8_t *data, const MplyChunk *chunk, const long *options, MplyPlanes *at) {
    const int64_t *ints = chunk->ints;
    const int verts = (int)ints[C_VERTS], faces = (int)ints[C_FACES];
    const Py_ssize_t start = (Py_ssize_t)(ints[C_FACE_PAD] - faces * 3);
    for (int i = 0; i < faces * 3; ++i)
        *at->faces++ = data[start + i];
    mply_positions(data + ints[C_FACE_PAD] + ints[C_FACE_PAD_SIZE], chunk, options, at->positions);
    at->positions += 3 * (Py_ssize_t)verts;
    const uint8_t *normals = data + ints[C_POS_PAD] + ints[C_POS_PAD_SIZE];
    const int stride = chunk->no_tangent ? 4 : 8, shared = chunk->compressed_normal;
    const int rows = (int)ints[C_NORMAL_ROWS];
    for (int row = 0; row < rows; ++row) {
        const int8_t *item = (const int8_t *)(normals + stride * (shared ? 0 : row));
        for (int i = 0; i < 3; ++i)
            *at->normals++ = fmax((double)item[i] / 127.0, -1.0);
        *at->normal_ws++ = (uint8_t)item[3];
        if (chunk->wilds_tangents)
            continue;
        for (int i = 0; i < 3; ++i)
            *at->tangents++ = chunk->no_tangent ? 1.0 : fmax((double)item[4 + i] / 127.0, -1.0);
        *at->tangent_ws++ = chunk->no_tangent ? 127 : (uint8_t)item[7];
    }
    if (ints[C_MASK] >= 0) {
        for (int vertex = 0; vertex < verts; ++vertex)
            at->mask_bits[vertex] = (data[ints[C_MASK] + vertex / 8] >> (vertex % 8)) & 1;
    }
    if (chunk->wilds_tangents) {
        for (int vertex = 0; vertex < verts; ++vertex) {
            int64_t value = chunk->mode == 0 ? 0x7F : 0x81;
            if (chunk->compressed_uv1)
                value = 0;
            else if (chunk->mode == 2)
                value = at->mask_bits[vertex] ? 0x81 : 0x7F;
            *at->tangent_ws++ = value;
        }
    }
    at->mask_bits += ints[C_MASK_BITS];
    Py_ssize_t cursor = (ints[C_MASK] >= 0 ? ints[C_MASK] + ints[C_MASK_SIZE] : (Py_ssize_t)(normals - data)
                         + (Py_ssize_t)(shared ? 1 : verts) * stride);
    mply_uvs(data + cursor, chunk->compressed_uv1 ? 1 : verts, (int)ints[C_UV0_ROWS], at->uv0);
    at->uv0 += 2 * ints[C_UV0_ROWS];
    cursor += 4 * (Py_ssize_t)(chunk->compressed_uv1 ? 1 : verts);
    if (chunk->use_uv1) {
        mply_uvs(data + cursor, chunk->compressed_uv2 ? 1 : verts, (int)ints[C_UV1_ROWS], at->uv1);
        at->uv1 += 2 * ints[C_UV1_ROWS];
        cursor += 4 * (Py_ssize_t)(chunk->compressed_uv2 ? 1 : verts);
    }
    if (options[MPLY_SKIN_BEFORE_TEXCOORD3] && chunk->has_skin)
        cursor += ints[C_SKIN_SIZE];
    if (chunk->use_uv2) {
        const int compressed = ((uint32_t)ints[C_FLAGS] >> 3) & 1;
        mply_uvs(data + cursor, compressed ? 1 : verts, (int)ints[C_UV2_ROWS], at->uv2);
        at->uv2 += 2 * ints[C_UV2_ROWS];
        cursor += 4 * (Py_ssize_t)(compressed ? 1 : verts);
    }
    if (chunk->use_colors) {
        for (int64_t i = 0; i < ints[C_COLORS]; ++i)
            *at->colors++ = data[cursor + (chunk->compressed_colors ? i % 4 : i)];
    }
    for (int i = 0; i < 6; ++i)
        *at->boxes++ = chunk->aabb[i];
}

static PyObject *mply_buffer(Py_ssize_t items, Py_ssize_t size, void **start) {
    PyObject *made = PyByteArray_FromStringAndSize(NULL, items * size);
    if (made)
        *start = PyByteArray_AS_STRING(made);
    return made;
}

/* The payload's fields as ``MPLYParser.parse`` joins the chunks' planes. */
static PyObject *mply_joined(const MplyChunk *chunks, Py_ssize_t count, const MplyPlanes *planes) {
    Py_ssize_t verts = 0, faces = 0, normal_rows = 0, tangent_rows = 0, tangent_w_rows = 0, uv0 = 0, uv1 = 0, uv2 = 0;
    Py_ssize_t colors = 0, tangent_lead = -1, tangent_total = 0;
    int uv1_whole = 1, uv2_whole = 1;
    for (Py_ssize_t i = 0; i < count; ++i) {
        const int64_t *ints = chunks[i].ints;
        if (ints[C_TANGENT_ROWS]) {
            if (tangent_lead < 0)
                tangent_lead = verts;
            tangent_total += ints[C_TANGENT_ROWS];
        } else if (tangent_lead >= 0) {
            tangent_total += ints[C_VERTS];
        }
        verts += ints[C_VERTS];
        faces += 3 * ints[C_FACES];
        normal_rows += ints[C_NORMAL_ROWS];
        tangent_rows += ints[C_TANGENT_ROWS];
        tangent_w_rows += ints[C_TANGENT_W_ROWS];
        uv0 += ints[C_UV0_ROWS];
        uv1 += ints[C_UV1_ROWS];
        uv2 += ints[C_UV2_ROWS];
        uv1_whole &= ints[C_UV1_ROWS] > 0;
        uv2_whole &= ints[C_UV2_ROWS] > 0;
        colors += ints[C_COLORS] ? ints[C_COLORS] : 4 * ints[C_VERTS];
    }
    if (tangent_lead > 0)
        tangent_total += tangent_lead;
    float *positions, *normals, *tangents;
    uint8_t *normal_ws, *tangent_ws, *color_bytes;
    double *uv0_out, *uv1_out, *uv2_out;
    uint32_t *face_out;
    PyObject *made[10] = {
        mply_buffer(verts * 3, 4, (void **)&positions), mply_buffer(normal_rows * 3, 4, (void **)&normals),
        mply_buffer(normal_rows, 1, (void **)&normal_ws), mply_buffer(tangent_total * 3, 4, (void **)&tangents),
        mply_buffer(tangent_w_rows, 1, (void **)&tangent_ws), mply_buffer(uv0 * 2, 8, (void **)&uv0_out),
        mply_buffer(uv1_whole ? uv1 * 2 : 0, 8, (void **)&uv1_out),
        mply_buffer(uv2_whole ? uv2 * 2 : 0, 8, (void **)&uv2_out), mply_buffer(colors, 1, (void **)&color_bytes),
        mply_buffer(faces, 4, (void **)&face_out),
    };
    for (int i = 0; i < 10; ++i) {
        if (!made[i]) {
            for (int j = 0; j < 10; ++j)
                Py_XDECREF(made[j]);
            return NULL;
        }
    }
    for (Py_ssize_t i = 0; i < verts * 3; ++i)
        positions[i] = (float)planes->positions[i];
    /* The positions' least and greatest, of the floats held; a column with a NaN is NaN, as numpy's reductions. */
    double low[3] = {Py_HUGE_VAL, Py_HUGE_VAL, Py_HUGE_VAL}, high[3] = {-Py_HUGE_VAL, -Py_HUGE_VAL, -Py_HUGE_VAL};
    int unordered[3] = {0, 0, 0};
    for (Py_ssize_t i = 0; i < verts * 3; ++i) {
        const double value = positions[i];
        const int column = (int)(i % 3);
        if (value != value)
            unordered[column] = 1;
        if (value < low[column])
            low[column] = value;
        if (value > high[column])
            high[column] = value;
    }
    for (int column = 0; column < 3; ++column) {
        if (unordered[column])
            low[column] = high[column] = Py_NAN;
    }
    PyObject *bounds = verts ? Py_BuildValue("((ddd)(ddd))", low[0], low[1], low[2], high[0], high[1], high[2])
                             : Py_NewRef(Py_None);
    if (!bounds) {
        for (int j = 0; j < 10; ++j)
            Py_DECREF(made[j]);
        return NULL;
    }
    for (Py_ssize_t i = 0; i < normal_rows * 3; ++i)
        normals[i] = (float)planes->normals[i];
    for (Py_ssize_t i = 0; i < normal_rows; ++i)
        normal_ws[i] = (uint8_t)planes->normal_ws[i];
    for (Py_ssize_t i = 0; i < tangent_w_rows; ++i)
        tangent_ws[i] = (uint8_t)planes->tangent_ws[i];
    memcpy(uv0_out, planes->uv0, (size_t)uv0 * 2 * 8);
    if (uv1_whole)
        memcpy(uv1_out, planes->uv1, (size_t)uv1 * 2 * 8);
    if (uv2_whole)
        memcpy(uv2_out, planes->uv2, (size_t)uv2 * 2 * 8);
    for (Py_ssize_t i = 0; i < faces; ++i)
        face_out[i] = (uint32_t)planes->faces[i];
    /* Tangents start at the first chunk with them, zero before; a chunk without them after is zero. */
    const double *tangent_from = planes->tangents;
    const int64_t *color_from = planes->colors;
    float *tangent_to = tangents;
    if (tangent_lead > 0) {
        memset(tangent_to, 0, (size_t)tangent_lead * 3 * sizeof(float));
        tangent_to += tangent_lead * 3;
    }
    int seen = 0;
    for (Py_ssize_t i = 0; i < count; ++i) {
        const int64_t *ints = chunks[i].ints;
        if (ints[C_TANGENT_ROWS]) {
            seen = 1;
            for (int64_t j = 0; j < ints[C_TANGENT_ROWS] * 3; ++j)
                *tangent_to++ = (float)*tangent_from++;
        } else if (seen) {
            memset(tangent_to, 0, (size_t)ints[C_VERTS] * 3 * sizeof(float));
            tangent_to += ints[C_VERTS] * 3;
        }
        if (ints[C_COLORS]) {
            for (int64_t j = 0; j < ints[C_COLORS]; ++j)
                *color_bytes++ = (uint8_t)*color_from++;
        } else {
            memset(color_bytes, 255, (size_t)ints[C_VERTS] * 4);
            color_bytes += ints[C_VERTS] * 4;
        }
    }
    return Py_BuildValue("{sNsNsNsNsNsNsNsNsNsNsN}", "positions", made[0], "normals", made[1], "normal_ws", made[2],
                         "tangents", made[3], "tangent_ws", made[4], "uv0", made[5], "uv1", made[6], "uv2", made[7],
                         "colors", made[8], "integer_faces", made[9], "bounds", bounds);
}

/* mply_chunks(data, origin, spans, options, name, planes, joined) -> (decoded, failure, ints, floats, planes,
   joined): each chunk of ``spans`` ((start, end) with -1 for no end) until the first that fails, its ``failure``
   then in the parser's words. */
static PyObject *mply_chunks(PyObject *self, PyObject *args) {
    Py_buffer view;
    Py_ssize_t origin;
    PyObject *spans_object, *options_object;
    const char *name;
    int want_planes, want_joined;
    if (!PyArg_ParseTuple(args, "y*nOOspp", &view, &origin, &spans_object, &options_object, &name, &want_planes,
                          &want_joined))
        return NULL;
    PyObject *result = NULL, *spans = NULL, *ints_out = NULL, *floats_out = NULL, *planes_out = NULL;
    PyObject *joined_out = NULL;
    MplyChunk *chunks = NULL;
    PyObject *buffers[12] = {0};
    long options[MPLY_OPTIONS];
    if (PySequence_Size(options_object) != MPLY_OPTIONS) {
        PyErr_SetString(PyExc_ValueError, "mply_chunks takes nine profile options");
        goto done;
    }
    for (int i = 0; i < MPLY_OPTIONS; ++i) {
        PyObject *item = PySequence_GetItem(options_object, i);
        options[i] = item ? PyLong_AsLong(item) : -1;
        Py_XDECREF(item);
        if (PyErr_Occurred())
            goto done;
    }
    spans = PySequence_Fast(spans_object, "spans must be a sequence");
    if (!spans)
        goto done;
    const Py_ssize_t count = PySequence_Fast_GET_SIZE(spans);
    chunks = (MplyChunk *)PyMem_Calloc(count ? count : 1, sizeof(MplyChunk));
    if (!chunks) {
        PyErr_NoMemory();
        goto done;
    }
    Py_ssize_t *bounds = (Py_ssize_t *)PyMem_Calloc(count ? 2 * count : 1, sizeof(Py_ssize_t));
    if (!bounds) {
        PyErr_NoMemory();
        goto done;
    }
    for (Py_ssize_t i = 0; i < count; ++i) {
        if (!PyArg_ParseTuple(PySequence_Fast_GET_ITEM(spans, i), "nn", &bounds[2 * i], &bounds[2 * i + 1])) {
            PyMem_Free(bounds);
            goto done;
        }
    }
    char failure[256] = "";
    Py_ssize_t decoded = 0;
    const uint8_t *data = (const uint8_t *)view.buf;
    Py_BEGIN_ALLOW_THREADS
    for (; decoded < count; ++decoded) {
        if (!mply_measure(data, view.len, origin, bounds[2 * decoded], bounds[2 * decoded + 1], options, name,
                          &chunks[decoded], failure, sizeof failure))
            break;
    }
    Py_END_ALLOW_THREADS
    PyMem_Free(bounds);
    Py_ssize_t totals[12] = {0};
    for (Py_ssize_t i = 0; i < decoded; ++i) {
        const int64_t *ints = chunks[i].ints;
        totals[0] += 3 * ints[C_VERTS];
        totals[1] += 3 * ints[C_FACES];
        totals[2] += 3 * ints[C_NORMAL_ROWS];
        totals[3] += ints[C_NORMAL_ROWS];
        totals[4] += 3 * ints[C_TANGENT_ROWS];
        totals[5] += ints[C_TANGENT_W_ROWS];
        totals[6] += 2 * ints[C_UV0_ROWS];
        totals[7] += 2 * ints[C_UV1_ROWS];
        totals[8] += 2 * ints[C_UV2_ROWS];
        totals[9] += ints[C_COLORS];
        totals[10] += ints[C_MASK_BITS];
        totals[11] += 6;
    }
    MplyPlanes planes;
    void *starts[12];
    for (int i = 0; i < 12; ++i) {
        buffers[i] = mply_buffer(totals[i], 8, &starts[i]);
        if (!buffers[i])
            goto done;
    }
    planes.positions = starts[0];
    planes.faces = starts[1];
    planes.normals = starts[2];
    planes.normal_ws = starts[3];
    planes.tangents = starts[4];
    planes.tangent_ws = starts[5];
    planes.uv0 = starts[6];
    planes.uv1 = starts[7];
    planes.uv2 = starts[8];
    planes.colors = starts[9];
    planes.mask_bits = starts[10];
    planes.boxes = starts[11];
    MplyPlanes cursor = planes;
    Py_BEGIN_ALLOW_THREADS
    for (Py_ssize_t i = 0; i < decoded; ++i)
        mply_decode(data, &chunks[i], options, &cursor);
    Py_END_ALLOW_THREADS
    void *ints_start, *floats_start;
    ints_out = mply_buffer(decoded * C_FIELDS, 8, &ints_start);
    floats_out = mply_buffer(decoded * 3, 8, &floats_start);
    if (!ints_out || !floats_out)
        goto done;
    for (Py_ssize_t i = 0; i < decoded; ++i) {
        memcpy((int64_t *)ints_start + i * C_FIELDS, chunks[i].ints, sizeof chunks[i].ints);
        memcpy((double *)floats_start + i * 3, chunks[i].center, sizeof chunks[i].center);
    }
    if (want_joined && decoded == count) {
        joined_out = mply_joined(chunks, decoded, &planes);
        if (!joined_out)
            goto done;
    } else {
        joined_out = Py_NewRef(Py_None);
    }
    if (want_planes) {
        planes_out = Py_BuildValue("{sOsOsOsOsOsOsOsOsOsOsOsO}", "positions", buffers[0], "faces", buffers[1],
                                   "normals", buffers[2], "normal_ws", buffers[3], "tangents", buffers[4],
                                   "tangent_ws", buffers[5], "uv0", buffers[6], "uv1", buffers[7], "uv2", buffers[8],
                                   "colors", buffers[9], "tangent_mask_bits", buffers[10], "boxes", buffers[11]);
    } else {
        planes_out = Py_NewRef(Py_None);
    }
    if (!planes_out)
        goto done;
    result = Py_BuildValue("nsNNNN", decoded, decoded < count ? failure : NULL, ints_out, floats_out, planes_out,
                           joined_out);
    ints_out = floats_out = planes_out = joined_out = NULL;
done:
    for (int i = 0; i < 12; ++i)
        Py_XDECREF(buffers[i]);
    Py_XDECREF(ints_out);
    Py_XDECREF(floats_out);
    Py_XDECREF(planes_out);
    Py_XDECREF(joined_out);
    Py_XDECREF(spans);
    PyMem_Free(chunks);
    PyBuffer_Release(&view);
    return result;
}

/* ``boxes_seen(boxes, view_projection, margin)``: ``boxes`` float64 n x (min xyz, max xyz) in the world;
 * ``view_projection`` float64 4 x 4 for row vectors. Bytes, 1 where a box may be in view: 0 where all its corners lie
 * beyond one clip plane (left, right, bottom, top or behind the eye) by more than ``margin`` times the sum of the
 * corner's magnitudes through ``|view_projection|``. */
static PyObject* boxes_seen(PyObject* self, PyObject* args) {
    Py_buffer boxes = {0}, matrix = {0};
    double margin;
    PyObject* result = NULL;
    if (!PyArg_ParseTuple(args, "y*y*d", &boxes, &matrix, &margin))
        return NULL;
    const Py_ssize_t count = boxes.len / (Py_ssize_t)(6 * sizeof(double));
    if (boxes.len != count * (Py_ssize_t)(6 * sizeof(double)) || matrix.len != 16 * (Py_ssize_t)sizeof(double)) {
        PyErr_SetString(PyExc_ValueError, "boxes_seen needs float64 boxes of 6 and a 4 x 4 float64 matrix");
        goto done;
    }
    result = PyBytes_FromStringAndSize(NULL, count);
    if (!result)
        goto done;
    const double* box = (const double*)boxes.buf;
    const double* m = (const double*)matrix.buf;
    unsigned char* out = (unsigned char*)PyBytes_AS_STRING(result);
    double a[16];
    for (int i = 0; i < 16; ++i)
        a[i] = fabs(m[i]);
    /* Other threads run meanwhile only when that is worth the GIL's hand-off: getting it back can wait longer. */
    PyThreadState* state = count > 32768 ? PyEval_SaveThread() : NULL;
    for (Py_ssize_t n = 0; n < count; ++n, box += 6) {
        int beyond = 0x1F;
        for (int corner = 0; corner < 8 && beyond; ++corner) {
            const double p[4] = {box[(corner & 1) ? 3 : 0], box[(corner & 2) ? 4 : 1], box[(corner & 4) ? 5 : 2], 1.0};
            double c[4], reach = 0.0;
            for (int j = 0; j < 4; ++j) {
                c[j] = p[0] * m[j] + p[1] * m[4 + j] + p[2] * m[8 + j] + m[12 + j];
                reach += fabs(p[0]) * a[j] + fabs(p[1]) * a[4 + j] + fabs(p[2]) * a[8 + j] + a[12 + j];
            }
            reach *= margin;
            const double x = c[0], y = c[1], w = c[3];
            beyond &= (x - w > reach) | (-x - w > reach) << 1 | (y - w > reach) << 2 | (-y - w > reach) << 3
                      | (-w > reach) << 4;
        }
        out[n] = !beyond;
    }
    if (state)
        PyEval_RestoreThread(state);
done:
    PyBuffer_Release(&boxes);
    PyBuffer_Release(&matrix);
    return result;
}

/* ``steps_kept(steps, owners, levels, seen, lods)``: of the list ``steps``, those a frame keeps. ``owners`` and
 * ``levels`` (int64, by step; -1 for none, as for steps past them, and an owner below for a step left out) give each
 * step's owner and LOD; ``seen`` (bytes, by owner) whether its owner is in view and ``lods`` (int64 by owner, or None)
 * the LOD it is drawn at, their last entries answering owner -1. A step of a LOD is kept at its owner's. */
static PyObject* steps_kept(PyObject* self, PyObject* args) {
    PyObject *steps, *lods_given, *result = NULL;
    Py_buffer owners = {0}, levels = {0}, seen = {0}, lods = {0};
    if (!PyArg_ParseTuple(args, "O!y*y*y*O", &PyList_Type, &steps, &owners, &levels, &seen, &lods_given))
        return NULL;
    if (lods_given != Py_None && PyObject_GetBuffer(lods_given, &lods, PyBUF_SIMPLE) < 0)
        goto done;
    const Py_ssize_t known = owners.len / (Py_ssize_t)sizeof(int64_t), shown = seen.len;
    const int64_t* owner_of = (const int64_t*)owners.buf;
    const int64_t* level_of = (const int64_t*)levels.buf;
    const unsigned char* in_view = (const unsigned char*)seen.buf;
    const int64_t* drawn_at = lods.buf ? (const int64_t*)lods.buf : NULL;
    if (levels.len != owners.len || !shown || (drawn_at && lods.len != shown * (Py_ssize_t)sizeof(int64_t))) {
        PyErr_SetString(PyExc_ValueError, "steps_kept needs owners and levels alike, and an entry of seen and lods "
                                          "for each owner and one more");
        goto done;
    }
    const Py_ssize_t count = PyList_GET_SIZE(steps);
    result = PyList_New(0);
    if (!result)
        goto done;
    for (Py_ssize_t i = 0; i < count; ++i) {
        PyObject* step = PyList_GET_ITEM(steps, i);
        const long long number = PyLong_AsLongLong(step);
        if (number == -1 && PyErr_Occurred()) {
            Py_CLEAR(result);
            goto done;
        }
        const int64_t owner = number >= 0 && number < known ? owner_of[number] : -1;
        if (owner < -1)
            continue;
        const int64_t level = number >= 0 && number < known ? level_of[number] : -1;
        const Py_ssize_t at = owner < 0 ? shown - 1 : (Py_ssize_t)owner;
        if (at >= shown) {
            PyErr_SetString(PyExc_ValueError, "a step's owner has no entry in seen");
            Py_CLEAR(result);
            goto done;
        }
        if (in_view[at] && (!drawn_at || level < 0 || level == drawn_at[at]) && PyList_Append(result, step) < 0) {
            Py_CLEAR(result);
            goto done;
        }
    }
done:
    PyBuffer_Release(&owners);
    PyBuffer_Release(&levels);
    PyBuffer_Release(&seen);
    if (lods.buf)
        PyBuffer_Release(&lods);
    return result;
}

/* ``rects_meet(rects, targets)``: float64 n x (least x, y, most x, y) and k x the same. Bytes, 1 where a rect meets
 * any target, edges touching. */
static PyObject* rects_meet(PyObject* self, PyObject* args) {
    Py_buffer rects = {0}, targets = {0};
    PyObject* result = NULL;
    if (!PyArg_ParseTuple(args, "y*y*", &rects, &targets))
        return NULL;
    const Py_ssize_t count = rects.len / (Py_ssize_t)(4 * sizeof(double));
    const Py_ssize_t wanted = targets.len / (Py_ssize_t)(4 * sizeof(double));
    if (rects.len != count * (Py_ssize_t)(4 * sizeof(double)) || targets.len != wanted * (Py_ssize_t)(4 * sizeof(double))) {
        PyErr_SetString(PyExc_ValueError, "rects_meet needs float64 rects and targets of 4");
        goto done;
    }
    result = PyBytes_FromStringAndSize(NULL, count);
    if (!result)
        goto done;
    const double *r = (const double*)rects.buf, *t = (const double*)targets.buf;
    unsigned char* out = (unsigned char*)PyBytes_AS_STRING(result);
    for (Py_ssize_t i = 0; i < count; ++i, r += 4) {
        int meets = 0;
        for (Py_ssize_t j = 0; j < wanted && !meets; ++j) {
            const double* g = t + 4 * j;
            meets = r[0] <= g[2] && r[1] <= g[3] && r[2] >= g[0] && r[3] >= g[1];
        }
        out[i] = (unsigned char)meets;
    }
done:
    PyBuffer_Release(&rects);
    PyBuffer_Release(&targets);
    return result;
}

/* ``moved_rows(items, before)``: int64 bytes, the rows of the sequence ``items`` holding another object than
 * ``before`` does there, then those past its end. */
static PyObject* moved_rows(PyObject* self, PyObject* args) {
    PyObject *items, *before, *result = NULL;
    if (!PyArg_ParseTuple(args, "OO", &items, &before))
        return NULL;
    PyObject* now = PySequence_Fast(items, "moved_rows needs sequences");
    PyObject* held = now ? PySequence_Fast(before, "moved_rows needs sequences") : NULL;
    if (held) {
        const Py_ssize_t count = PySequence_Fast_GET_SIZE(now), kept = PySequence_Fast_GET_SIZE(held);
        const Py_ssize_t shared = count < kept ? count : kept;
        PyObject **a = PySequence_Fast_ITEMS(now), **b = PySequence_Fast_ITEMS(held);
        Py_ssize_t moved = count - shared;
        for (Py_ssize_t i = 0; i < shared; ++i)
            moved += a[i] != b[i];
        result = PyBytes_FromStringAndSize(NULL, moved * (Py_ssize_t)sizeof(int64_t));
        if (result) {
            int64_t* out = (int64_t*)PyBytes_AS_STRING(result);
            for (Py_ssize_t i = 0; i < count; ++i)
                if (i >= shared || a[i] != b[i])
                    *out++ = (int64_t)i;
        }
    }
    Py_XDECREF(now);
    Py_XDECREF(held);
    return result;
}

/* ``boxes_rects(boxes, view_projection)``: each world box's extent on screen as float64 (x, y) of clip space over w,
 * least then most; unbounded where a corner is not ahead of the eye. */
static PyObject* boxes_rects(PyObject* self, PyObject* args) {
    Py_buffer boxes = {0}, matrix = {0};
    PyObject* result = NULL;
    if (!PyArg_ParseTuple(args, "y*y*", &boxes, &matrix))
        return NULL;
    const Py_ssize_t count = boxes.len / (Py_ssize_t)(6 * sizeof(double));
    if (boxes.len != count * (Py_ssize_t)(6 * sizeof(double)) || matrix.len != 16 * (Py_ssize_t)sizeof(double)) {
        PyErr_SetString(PyExc_ValueError, "boxes_rects needs float64 boxes of 6 and a 4 x 4 float64 matrix");
        goto done;
    }
    result = PyBytes_FromStringAndSize(NULL, count * 4 * (Py_ssize_t)sizeof(double));
    if (!result)
        goto done;
    const double* box = (const double*)boxes.buf;
    const double* m = (const double*)matrix.buf;
    double* out = (double*)PyBytes_AS_STRING(result);
    for (Py_ssize_t n = 0; n < count; ++n, box += 6, out += 4) {
        double low_x = INFINITY, low_y = INFINITY, high_x = -INFINITY, high_y = -INFINITY;
        int unsure = 0;
        for (int corner = 0; corner < 8 && !unsure; ++corner) {
            const double x = box[(corner & 1) ? 3 : 0], y = box[(corner & 2) ? 4 : 1], z = box[(corner & 4) ? 5 : 2];
            const double w = x * m[3] + y * m[7] + z * m[11] + m[15];
            if (!(w > 0)) {
                unsure = 1;
                break;
            }
            const double sx = (x * m[0] + y * m[4] + z * m[8] + m[12]) / w;
            const double sy = (x * m[1] + y * m[5] + z * m[9] + m[13]) / w;
            low_x = fmin(low_x, sx), low_y = fmin(low_y, sy), high_x = fmax(high_x, sx), high_y = fmax(high_y, sy);
        }
        if (unsure || !isfinite(low_x) || !isfinite(low_y) || !isfinite(high_x) || !isfinite(high_y))
            low_x = low_y = -INFINITY, high_x = high_y = INFINITY;
        out[0] = low_x, out[1] = low_y, out[2] = high_x, out[3] = high_y;
    }
done:
    PyBuffer_Release(&boxes);
    PyBuffer_Release(&matrix);
    return result;
}

/* ``ray_pick(origin, direction, positions, triangles, runs, flipped)``: of ``triangles`` (uint32 x 3 into the float32
 * xyz ``positions``), the nearest the ray (float64 origin and direction, in the positions' space) meets past its origin,
 * as (distance in directions, triangle), or None. ``runs`` (int32 first triangle, count, culling, whether a front face
 * turns counter-clockwise; culling 1 none, 2 front, 3 back) cull as the overlay's rules do, a face being in front where
 * it turns clockwise on screen; ``flipped``: the space turns faces over. Of equal distances the lower triangle. */
static PyObject* ray_pick(PyObject* self, PyObject* args) {
    double o[3], d[3];
    Py_buffer positions = {0}, triangles = {0}, runs = {0};
    int flipped;
    PyObject* result = NULL;
    if (!PyArg_ParseTuple(args, "(ddd)(ddd)y*y*y*p", &o[0], &o[1], &o[2], &d[0], &d[1], &d[2], &positions, &triangles,
                          &runs, &flipped))
        return NULL;
    const Py_ssize_t vertices = positions.len / (Py_ssize_t)(3 * sizeof(float));
    const Py_ssize_t count = triangles.len / (Py_ssize_t)(3 * sizeof(uint32_t));
    const Py_ssize_t run_count = runs.len / (Py_ssize_t)(4 * sizeof(int32_t));
    if (positions.len % (3 * sizeof(float)) || triangles.len % (3 * sizeof(uint32_t)) ||
        runs.len % (4 * sizeof(int32_t))) {
        PyErr_SetString(PyExc_ValueError, "ray_pick needs float32 xyz positions, uint32 triangles and int32 runs of 4");
        goto done;
    }
    const float* p = (const float*)positions.buf;
    const uint32_t* t = (const uint32_t*)triangles.buf;
    const int32_t* r = (const int32_t*)runs.buf;
    double best = INFINITY;
    Py_ssize_t found = -1;
    for (Py_ssize_t run = 0; run < run_count; ++run) {
        const Py_ssize_t first = r[4 * run], last = first + r[4 * run + 1];
        const int cull = r[4 * run + 2], counter_clockwise = r[4 * run + 3] != 0;
        if (first < 0 || last > count) {
            PyErr_SetString(PyExc_ValueError, "a run lies outside the triangles");
            goto done;
        }
        for (Py_ssize_t n = first; n < last; ++n) {
            const uint32_t ia = t[3 * n], ib = t[3 * n + 1], ic = t[3 * n + 2];
            if (ia >= vertices || ib >= vertices || ic >= vertices) {
                PyErr_SetString(PyExc_ValueError, "a triangle names a vertex past the positions");
                goto done;
            }
            const double a[3] = {p[3 * ia], p[3 * ia + 1], p[3 * ia + 2]};
            const double e1[3] = {p[3 * ib] - a[0], p[3 * ib + 1] - a[1], p[3 * ib + 2] - a[2]};
            const double e2[3] = {p[3 * ic] - a[0], p[3 * ic + 1] - a[1], p[3 * ic + 2] - a[2]};
            const double q[3] = {d[1] * e2[2] - d[2] * e2[1], d[2] * e2[0] - d[0] * e2[2], d[0] * e2[1] - d[1] * e2[0]};
            const double det = e1[0] * q[0] + e1[1] * q[1] + e1[2] * q[2];
            if (det == 0)
                continue;
            /* The face's normal (e1 x e2) against the ray is -det. */
            const int front = ((-det > 0) != (flipped != 0)) != counter_clockwise;
            if ((cull == 2 && front) || (cull == 3 && !front))
                continue;
            const double s[3] = {o[0] - a[0], o[1] - a[1], o[2] - a[2]};
            const double u = (s[0] * q[0] + s[1] * q[1] + s[2] * q[2]) / det;
            if (u < 0 || u > 1)
                continue;
            const double w[3] = {s[1] * e1[2] - s[2] * e1[1], s[2] * e1[0] - s[0] * e1[2], s[0] * e1[1] - s[1] * e1[0]};
            const double v = (d[0] * w[0] + d[1] * w[1] + d[2] * w[2]) / det;
            if (v < 0 || u + v > 1)
                continue;
            const double distance = (e2[0] * w[0] + e2[1] * w[1] + e2[2] * w[2]) / det;
            if (distance > 0 && distance < best)
                best = distance, found = n;
        }
    }
    result = found < 0 ? Py_NewRef(Py_None) : Py_BuildValue("(dn)", best, found);
done:
    PyBuffer_Release(&positions);
    PyBuffer_Release(&triangles);
    PyBuffer_Release(&runs);
    return result;
}

/* ``stream_levels(origins, eye, start, end, billboard, cull, solid, count)``: of objects at ``origins`` (float64 n x
 * 3) seen from ``eye`` (the view's translation through its rotation, three floats), (distances float64, LODs int64) as
 * bytearrays, by ``scene_lit._Streams``' rule: 3D LODs evenly from ``start`` to ``end`` over ``solid`` of them, the
 * last of ``count`` from ``billboard`` on, -1 past ``cull``; numpy's arithmetic, element by element. */
static PyObject* stream_levels(PyObject* self, PyObject* args) {
    Py_buffer origins = {0}, start = {0}, end = {0}, billboard = {0}, cull = {0}, solid = {0}, count = {0};
    double ex, ey, ez;
    PyObject *distances = NULL, *levels = NULL, *result = NULL;
    if (!PyArg_ParseTuple(args, "y*(ddd)y*y*y*y*y*y*", &origins, &ex, &ey, &ez, &start, &end, &billboard, &cull,
                          &solid, &count))
        return NULL;
    const Py_ssize_t n = origins.len / (Py_ssize_t)(3 * sizeof(double));
    const Py_ssize_t f = n * (Py_ssize_t)sizeof(double);
    if (origins.len != 3 * f || start.len != f || end.len != f || billboard.len != f || cull.len != f
        || solid.len != f || count.len != f) {
        PyErr_SetString(PyExc_ValueError, "stream_levels needs float64 n x 3 origins and n of each float64 and int64");
        goto done;
    }
    distances = PyByteArray_FromStringAndSize(NULL, f);
    levels = PyByteArray_FromStringAndSize(NULL, f);
    if (!distances || !levels)
        goto done;
    const double* o = (const double*)origins.buf;
    const double *from = (const double*)start.buf, *to = (const double*)end.buf;
    const double *flat = (const double*)billboard.buf, *gone = (const double*)cull.buf;
    const int64_t *solids = (const int64_t*)solid.buf, *counts = (const int64_t*)count.buf;
    double* d = (double*)PyByteArray_AS_STRING(distances);
    int64_t* level = (int64_t*)PyByteArray_AS_STRING(levels);
    for (Py_ssize_t i = 0; i < n; ++i) {
        const double x = o[3 * i] + ex, y = o[3 * i + 1] + ey, z = o[3 * i + 2] + ez;
        d[i] = sqrt(x * x + y * y + z * z);
        double share;
        if (to[i] > from[i]) {
            share = (d[i] - from[i]) / (to[i] - from[i]);
            share = share < 0.0 ? 0.0 : share;
            share = share > 1.0 ? 1.0 : share;
        } else {
            share = d[i] >= to[i] ? 1.0 : 0.0;
        }
        level[i] = (int64_t)(share * (double)(solids[i] - 1));
        if (d[i] >= flat[i])
            level[i] = counts[i] - 1;
        if (d[i] > gone[i])
            level[i] = -1;
    }
    result = PyTuple_Pack(2, distances, levels);
done:
    Py_XDECREF(distances);
    Py_XDECREF(levels);
    PyBuffer_Release(&origins);
    PyBuffer_Release(&start);
    PyBuffer_Release(&end);
    PyBuffer_Release(&billboard);
    PyBuffer_Release(&cull);
    PyBuffer_Release(&solid);
    PyBuffer_Release(&count);
    return result;
}

/* SSE lanes, each operation lane by lane in single precision. */
typedef struct { float v[4]; } Lanes;

static Lanes lanes_shuffle(Lanes a, Lanes b, int order) {
    Lanes r = {{a.v[order & 3], a.v[(order >> 2) & 3], b.v[(order >> 4) & 3], b.v[(order >> 6) & 3]}};
    return r;
}
static Lanes lanes_mul(Lanes a, Lanes b) {
    Lanes r;
    for (int i = 0; i < 4; ++i) r.v[i] = a.v[i] * b.v[i];
    return r;
}
static Lanes lanes_add(Lanes a, Lanes b) {
    Lanes r;
    for (int i = 0; i < 4; ++i) r.v[i] = a.v[i] + b.v[i];
    return r;
}
static Lanes lanes_sub(Lanes a, Lanes b) {
    Lanes r;
    for (int i = 0; i < 4; ++i) r.v[i] = a.v[i] - b.v[i];
    return r;
}

/* ``engine_inverse(matrix)``: float32 16 bytes x 4 -> float32 16 x 4 bytes, ``via::matrix::inverse``'s SSE
 * operations in turn (docs/internals/engine-buffers.md). */
static PyObject* engine_inverse(PyObject* self, PyObject* args) {
    Py_buffer given = {0};
    if (!PyArg_ParseTuple(args, "y*", &given))
        return NULL;
    if (given.len != 16 * (Py_ssize_t)sizeof(float)) {
        PyBuffer_Release(&given);
        PyErr_SetString(PyExc_ValueError, "engine_inverse needs 16 float32");
        return NULL;
    }
    const float* src = (const float*)given.buf;
    Lanes tmp = {{src[0], src[1], src[4], src[5]}}, row1 = {{src[8], src[9], src[12], src[13]}};
    Lanes row0 = lanes_shuffle(tmp, row1, 0x88);
    row1 = lanes_shuffle(row1, tmp, 0xDD);
    tmp = (Lanes){{src[2], src[3], src[6], src[7]}};
    Lanes row3 = {{src[10], src[11], src[14], src[15]}};
    Lanes row2 = lanes_shuffle(tmp, row3, 0x88);
    row3 = lanes_shuffle(row3, tmp, 0xDD);
    PyBuffer_Release(&given);
    Lanes minor0, minor1, minor2, minor3, t;

    t = lanes_mul(row2, row3);
    tmp = lanes_shuffle(t, t, 0xB1);
    minor0 = lanes_mul(row1, tmp);
    minor1 = lanes_mul(row0, tmp);
    tmp = lanes_shuffle(tmp, tmp, 0x4E);
    minor0 = lanes_sub(lanes_mul(row1, tmp), minor0);
    minor1 = lanes_sub(lanes_mul(row0, tmp), minor1);
    minor1 = lanes_shuffle(minor1, minor1, 0x4E);

    t = lanes_mul(row1, row2);
    tmp = lanes_shuffle(t, t, 0xB1);
    minor0 = lanes_add(lanes_mul(row3, tmp), minor0);
    minor3 = lanes_mul(row0, tmp);
    tmp = lanes_shuffle(tmp, tmp, 0x4E);
    minor0 = lanes_sub(minor0, lanes_mul(row3, tmp));
    minor3 = lanes_sub(lanes_mul(row0, tmp), minor3);
    minor3 = lanes_shuffle(minor3, minor3, 0x4E);

    t = lanes_mul(lanes_shuffle(row1, row1, 0x4E), row3);
    tmp = lanes_shuffle(t, t, 0xB1);
    row2 = lanes_shuffle(row2, row2, 0x4E);
    minor0 = lanes_add(lanes_mul(row2, tmp), minor0);
    minor2 = lanes_mul(row0, tmp);
    tmp = lanes_shuffle(tmp, tmp, 0x4E);
    minor0 = lanes_sub(minor0, lanes_mul(row2, tmp));
    minor2 = lanes_sub(lanes_mul(row0, tmp), minor2);
    minor2 = lanes_shuffle(minor2, minor2, 0x4E);

    t = lanes_mul(row0, row1);
    tmp = lanes_shuffle(t, t, 0xB1);
    minor2 = lanes_add(lanes_mul(row3, tmp), minor2);
    minor3 = lanes_sub(lanes_mul(row2, tmp), minor3);
    tmp = lanes_shuffle(tmp, tmp, 0x4E);
    minor2 = lanes_sub(lanes_mul(row3, tmp), minor2);
    minor3 = lanes_sub(minor3, lanes_mul(row2, tmp));

    t = lanes_mul(row0, row3);
    tmp = lanes_shuffle(t, t, 0xB1);
    minor1 = lanes_sub(minor1, lanes_mul(row2, tmp));
    minor2 = lanes_add(lanes_mul(row1, tmp), minor2);
    tmp = lanes_shuffle(tmp, tmp, 0x4E);
    minor1 = lanes_add(lanes_mul(row2, tmp), minor1);
    minor2 = lanes_sub(minor2, lanes_mul(row1, tmp));

    t = lanes_mul(row0, row2);
    tmp = lanes_shuffle(t, t, 0xB1);
    minor1 = lanes_add(lanes_mul(row3, tmp), minor1);
    minor3 = lanes_sub(minor3, lanes_mul(row1, tmp));
    tmp = lanes_shuffle(tmp, tmp, 0x4E);
    minor1 = lanes_sub(minor1, lanes_mul(row3, tmp));
    minor3 = lanes_add(lanes_mul(row1, tmp), minor3);

    Lanes determinant = lanes_mul(row0, minor0);
    determinant = lanes_add(lanes_shuffle(determinant, determinant, 0x4E), determinant);
    determinant = lanes_add(lanes_shuffle(determinant, determinant, 0xB1), determinant);
    float out[16] = {0};
    if (determinant.v[0] == 0.0f) {
        out[0] = out[5] = out[10] = out[15] = 1.0f;
    } else {
        const float scale = 1.0f / determinant.v[0];
        const Lanes* minors[4] = {&minor0, &minor1, &minor2, &minor3};
        for (int row = 0; row < 4; ++row)
            for (int i = 0; i < 4; ++i)
                out[4 * row + i] = minors[row]->v[i] * scale;
    }
    return PyBytes_FromStringAndSize((const char*)out, sizeof(out));
}

/* ``edges_of(positions, triangles)``: as uint32, each vertex's first vertex at its position (float32 x, y, z, bit for
 * bit), and the edges between those of the triangles' corners (uint32), each a pair least first, once, in order. */
static PyObject* edges_of(PyObject* self, PyObject* args) {
    Py_buffer positions = {0}, triangles = {0};
    PyObject *points = NULL, *edges = NULL, *result = NULL;
    uint32_t* table = NULL;
    uint64_t *sides = NULL, *spare = NULL;
    size_t* counts = NULL;
    if (!PyArg_ParseTuple(args, "y*y*", &positions, &triangles))
        return NULL;
    const Py_ssize_t count = positions.len / 12, corners = triangles.len / 4;
    if (positions.len != count * 12 || triangles.len != corners * 4 || corners % 3 || count > UINT32_MAX - 1) {
        PyErr_SetString(PyExc_ValueError, "edges_of needs float32 positions of 3 and uint32 triangles of 3");
        goto done;
    }
    size_t size = 1;
    while (size < 2 * (size_t)count)
        size <<= 1;
    table = (uint32_t*)calloc(size, sizeof(uint32_t));
    sides = (uint64_t*)malloc((corners ? corners : 1) * sizeof(uint64_t));
    spare = (uint64_t*)malloc((corners ? corners : 1) * sizeof(uint64_t));
    counts = (size_t*)malloc(65536 * sizeof(size_t));
    points = PyBytes_FromStringAndSize(NULL, count * 4);
    if (!table || !sides || !spare || !counts || !points) {
        if (!PyErr_Occurred())
            PyErr_NoMemory();
        goto done;
    }
    const uint32_t* xyz = (const uint32_t*)positions.buf;
    uint32_t* point = (uint32_t*)PyBytes_AS_STRING(points);
    for (Py_ssize_t v = 0; v < count; ++v) {
        const uint32_t* at = xyz + 3 * v;
        uint64_t hash = (at[0] * 0x9E3779B97F4A7C15ull) ^ (at[1] * 0xC2B2AE3D27D4EB4Full)
                        ^ (at[2] * 0x165667B19E3779F9ull);
        hash ^= hash >> 31;
        size_t slot = (size_t)hash & (size - 1);
        for (;;) {
            const uint32_t held = table[slot];
            if (!held) {
                table[slot] = (uint32_t)v + 1;
                point[v] = (uint32_t)v;
                break;
            }
            const uint32_t* other = xyz + 3 * (held - 1);
            if (other[0] == at[0] && other[1] == at[1] && other[2] == at[2]) {
                point[v] = held - 1;
                break;
            }
            slot = (slot + 1) & (size - 1);
        }
    }
    const uint32_t* corner = (const uint32_t*)triangles.buf;
    size_t made = 0;
    for (Py_ssize_t t = 0; t < corners; t += 3) {
        if (corner[t] >= count || corner[t + 1] >= count || corner[t + 2] >= count) {
            PyErr_SetString(PyExc_IndexError, "a triangle's corner is past the vertices");
            goto done;
        }
        const uint32_t ends[4] = {point[corner[t]], point[corner[t + 1]], point[corner[t + 2]], point[corner[t]]};
        for (int side = 0; side < 3; ++side) {
            const uint32_t a = ends[side], b = ends[side + 1];
            if (a != b)
                sides[made++] = a < b ? (uint64_t)a << 32 | b : (uint64_t)b << 32 | a;
        }
    }
    for (int shift = 0; made && shift < 64; shift += 16) {
        memset(counts, 0, 65536 * sizeof(size_t));
        for (size_t i = 0; i < made; ++i)
            ++counts[(sides[i] >> shift) & 0xFFFF];
        if (counts[(sides[0] >> shift) & 0xFFFF] == made)
            continue;
        for (size_t digit = 0, sum = 0; digit < 65536; ++digit) {
            const size_t here = counts[digit];
            counts[digit] = sum;
            sum += here;
        }
        for (size_t i = 0; i < made; ++i)
            spare[counts[(sides[i] >> shift) & 0xFFFF]++] = sides[i];
        uint64_t* swap = sides;
        sides = spare;
        spare = swap;
    }
    size_t kept = 0;
    for (size_t i = 0; i < made; ++i)
        if (!kept || sides[i] != sides[kept - 1])
            sides[kept++] = sides[i];
    edges = PyBytes_FromStringAndSize(NULL, (Py_ssize_t)kept * 8);
    if (!edges)
        goto done;
    uint32_t* out = (uint32_t*)PyBytes_AS_STRING(edges);
    for (size_t i = 0; i < kept; ++i) {
        out[2 * i] = (uint32_t)(sides[i] >> 32);
        out[2 * i + 1] = (uint32_t)sides[i];
    }
    result = PyTuple_Pack(2, points, edges);
done:
    Py_XDECREF(points);
    Py_XDECREF(edges);
    free(table);
    free(sides);
    free(spare);
    free(counts);
    PyBuffer_Release(&positions);
    PyBuffer_Release(&triangles);
    return result;
}

static PyMethodDef methods[] = {
    {"move_keeping_lengths", move_keeping_lengths, METH_VARARGS, "Move strand vertices, keeping their strands' segment lengths"},
    {"drape_strands", drape_strands, METH_VARARGS, "Grow strands long in a drape under gravity"},
    {"nearest_points", nearest_points, METH_VARARGS, "The k nearest other points of each point"},
    {"boxes_seen", boxes_seen, METH_VARARGS, "Whether each world box may be in a view"},
    {"steps_kept", steps_kept, METH_VARARGS, "The steps a frame keeps of those in view"},
    {"boxes_rects", boxes_rects, METH_VARARGS, "Each world box's extent on screen"},
    {"ray_pick", ray_pick, METH_VARARGS, "The nearest triangle a ray meets"},
    {"engine_inverse", engine_inverse, METH_VARARGS, "The engine's single-precision 4 x 4 inverse"},
    {"stream_levels", stream_levels, METH_VARARGS, "Objects' distances and LODs from an eye"},
    {"rects_meet", rects_meet, METH_VARARGS, "Whether each rect meets any of others"},
    {"moved_rows", moved_rows, METH_VARARGS, "The rows of a sequence holding other objects than another"},
    {"edges_of", edges_of, METH_VARARGS, "Each vertex's first at its position, and the edges of triangles"},
    {"unpack_normals_tangents", unpack_normals_tangents, METH_VARARGS, "Decode normals/tangents from bytes"},
    {"pack_normals_tangents", pack_normals_tangents, METH_VARARGS, "Encode normals/tangents to bytes"},
    {"unpack_uvs", unpack_uvs, METH_VARARGS, "Decode UV half floats"},
    {"pack_uvs", pack_uvs, METH_VARARGS, "Encode UV floats"},
    {"unpack_colors", unpack_colors, METH_VARARGS, "Decode RGBA colors"},
    {"pack_colors", pack_colors, METH_VARARGS, "Encode RGBA colors"},
    {"skin_vertices", skin_vertices, METH_VARARGS, "Apply linear-blend skinning"},
    {"recalculate_normals", recalculate_normals, METH_VARARGS, "Recalculate redirected render normals"},
    {"recalculate_dispatch_normals", recalculate_normals, METH_VARARGS, "Recalculate dispatch normals with an explicit quantization scale"},
    {"mply_chunks", mply_chunks, METH_VARARGS, "Decode MPLY meshlet chunks and join their planes"},
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
