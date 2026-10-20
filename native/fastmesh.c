#include <Python.h>
#include <math.h>
#include <stdint.h>
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

static PyMethodDef methods[] = {
    {"move_keeping_lengths", move_keeping_lengths, METH_VARARGS, "Move strand vertices, keeping their strands' segment lengths"},
    {"drape_strands", drape_strands, METH_VARARGS, "Grow strands long in a drape under gravity"},
    {"nearest_points", nearest_points, METH_VARARGS, "The k nearest other points of each point"},
    {"unpack_normals_tangents", unpack_normals_tangents, METH_VARARGS, "Decode normals/tangents from bytes"},
    {"pack_normals_tangents", pack_normals_tangents, METH_VARARGS, "Encode normals/tangents to bytes"},
    {"unpack_uvs", unpack_uvs, METH_VARARGS, "Decode UV half floats"},
    {"pack_uvs", pack_uvs, METH_VARARGS, "Encode UV floats"},
    {"unpack_colors", unpack_colors, METH_VARARGS, "Decode RGBA colors"},
    {"pack_colors", pack_colors, METH_VARARGS, "Encode RGBA colors"},
    {"skin_vertices", skin_vertices, METH_VARARGS, "Apply linear-blend skinning"},
    {"recalculate_normals", recalculate_normals, METH_VARARGS, "Recalculate redirected render normals"},
    {"recalculate_dispatch_normals", recalculate_normals, METH_VARARGS, "Recalculate dispatch normals with an explicit quantization scale"},
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
