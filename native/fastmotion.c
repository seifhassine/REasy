/* A local transform is ten doubles (translation, rotation xyzw, scale); matrices are row-vector 4x4 doubles. */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

enum { TRANSLATION = 0, ROTATION = 3, SCALE = 7, WIDTH = 10 };
enum { FRAME_START, KEYS, VALUE_START, VALUE_WIDTH, CONSTANT, TRACK_ROW };
enum { BLEND_TRANSLATION = 1, BLEND_ROTATION = 2, BLEND_SCALE = 4 };

/* The work runs without the interpreter lock, so the caller raises this as a ValueError once it holds the lock again.
 * */
typedef const char *Failure;

static Failure normalize(double q[4]) {
    double length = sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2] + q[3] * q[3]);
    if (!isfinite(length) || length <= 1e-12) return "cannot normalize a zero-length or nonfinite quaternion";
    for (int i = 0; i < 4; ++i) q[i] /= length;
    return NULL;
}

static Failure interpolate(const double *left, const double *right, double amount, int slerp, double out[4]) {
    double a[4], b[4];
    memcpy(a, left, sizeof a);
    memcpy(b, right, sizeof b);
    Failure failure = normalize(a);
    if (!failure) failure = normalize(b);
    if (failure) return failure;
    double dot = a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3];
    if (dot < 0.0) {
        for (int i = 0; i < 4; ++i) b[i] = -b[i];
        dot = -dot;
    }
    amount = amount < 0.0 ? 0.0 : amount > 1.0 ? 1.0 : amount;
    if (dot > 1.0) dot = 1.0;
    if (!slerp || dot > 0.999999) {
        for (int i = 0; i < 4; ++i) out[i] = a[i] + (b[i] - a[i]) * amount;
        return normalize(out);
    }
    double angle = acos(dot), denominator = sin(angle);
    double left_weight = sin((1.0 - amount) * angle) / denominator, right_weight = sin(amount * angle) / denominator;
    for (int i = 0; i < 4; ++i) out[i] = a[i] * left_weight + b[i] * right_weight;
    return normalize(out);
}

static Failure multiply(const double *left, const double *right, double out[4]) {
    double a[4], b[4];
    memcpy(a, left, sizeof a);
    memcpy(b, right, sizeof b);
    Failure failure = normalize(a);
    if (!failure) failure = normalize(b);
    if (failure) return failure;
    out[0] = a[3] * b[0] + a[0] * b[3] + a[1] * b[2] - a[2] * b[1];
    out[1] = a[3] * b[1] - a[0] * b[2] + a[1] * b[3] + a[2] * b[0];
    out[2] = a[3] * b[2] + a[0] * b[1] - a[1] * b[0] + a[2] * b[3];
    out[3] = a[3] * b[3] - a[0] * b[0] - a[1] * b[1] - a[2] * b[2];
    return normalize(out);
}

static Failure sample_track(const int64_t *track, const double *frames, const double *values, double frame, int slerp,
                            double *out) {
    const double *keys = frames + track[FRAME_START], *held = values + track[VALUE_START];
    const int64_t count = track[KEYS], width = track[VALUE_WIDTH];
    if (track[CONSTANT] || frame < keys[0]) {
        memcpy(out, held, (size_t)width * sizeof(double));
        return NULL;
    }
    if (frame >= keys[count - 1]) {
        memcpy(out, held + (count - 1) * width, (size_t)width * sizeof(double));
        return NULL;
    }
    int64_t low = 0, high = count;
    while (low < high) {
        int64_t middle = (low + high) / 2;
        if (keys[middle] <= frame) low = middle + 1;
        else high = middle;
    }
    const double left_frame = keys[low - 1], right_frame = keys[low];
    const double *left = held + (low - 1) * width, *right = held + low * width;
    if (right_frame == left_frame) {
        memcpy(out, right, (size_t)width * sizeof(double));
        return NULL;
    }
    const double amount = (frame - left_frame) / (right_frame - left_frame);
    if (width == 4) return interpolate(left, right, amount, slerp, out);
    for (int64_t i = 0; i < width; ++i) out[i] = left[i] + (right[i] - left[i]) * amount;
    return NULL;
}

static Failure overwrite(double *current, const double *target, double amount, int channels, int slerp) {
    if (channels & BLEND_TRANSLATION)
        for (int i = 0; i < 3; ++i)
            current[TRANSLATION + i] += (target[TRANSLATION + i] - current[TRANSLATION + i]) * amount;
    if (channels & BLEND_ROTATION) {
        double rotation[4];
        Failure failure = interpolate(current + ROTATION, target + ROTATION, amount, slerp, rotation);
        if (failure) return failure;
        memcpy(current + ROTATION, rotation, sizeof rotation);
    }
    if (channels & BLEND_SCALE)
        for (int i = 0; i < 3; ++i) current[SCALE + i] += (target[SCALE + i] - current[SCALE + i]) * amount;
    return NULL;
}

static Failure add(double *current, const double *delta, double amount, int channels, int slerp) {
    static const double identity[4] = {0.0, 0.0, 0.0, 1.0};
    if (channels & BLEND_TRANSLATION)
        for (int i = 0; i < 3; ++i) current[TRANSLATION + i] += delta[TRANSLATION + i] * amount;
    if (channels & BLEND_ROTATION) {
        double part[4], rotation[4];
        Failure failure = interpolate(identity, delta + ROTATION, amount, slerp, part);
        if (!failure) failure = multiply(current + ROTATION, part, rotation);
        if (failure) return failure;
        memcpy(current + ROTATION, rotation, sizeof rotation);
    }
    if (channels & BLEND_SCALE)
        for (int i = 0; i < 3; ++i) current[SCALE + i] += (delta[SCALE + i] - 1.0) * amount;
    return NULL;
}

static Failure joint_matrix(const double *local, int scaled, double m[16]) {
    double q[4];
    memcpy(q, local + ROTATION, sizeof q);
    Failure failure = normalize(q);
    if (failure) return failure;
    const double x = q[0], y = q[1], z = q[2], w = q[3];
    const double xx = x * x, yy = y * y, zz = z * z, xy = x * y, xz = x * z, yz = y * z, xw = x * w, yw = y * w,
                 zw = z * w;
    const double rows[3][3] = {
        {1.0 - 2.0 * (yy + zz), 2.0 * (xy + zw), 2.0 * (xz - yw)},
        {2.0 * (xy - zw), 1.0 - 2.0 * (xx + zz), 2.0 * (yz + xw)},
        {2.0 * (xz + yw), 2.0 * (yz - xw), 1.0 - 2.0 * (xx + yy)},
    };
    for (int r = 0; r < 3; ++r) {
        const double s = scaled ? local[SCALE + r] : 1.0;
        for (int c = 0; c < 3; ++c) m[r * 4 + c] = rows[r][c] * s;
        m[r * 4 + 3] = 0.0;
    }
    for (int c = 0; c < 3; ++c) m[12 + c] = local[TRANSLATION + c];
    m[15] = 1.0;
    return NULL;
}

static void matmul(const double a[16], const double b[16], double out[16]) {
    double result[16];
    for (int r = 0; r < 4; ++r)
        for (int c = 0; c < 4; ++c)
            result[r * 4 + c] = a[r * 4] * b[c] + a[r * 4 + 1] * b[4 + c] + a[r * 4 + 2] * b[8 + c]
                + a[r * 4 + 3] * b[12 + c];
    memcpy(out, result, sizeof result);
}

/* A joint compensating its parent's scale (``via::Joint::getPartSegmentScaleMatrix``) uses the parent's unscaled world
 * for its basis but keeps the scale for its translation. */
typedef struct {
    const double *local;
    const int32_t *parents;
    const uint8_t *compensating;
    Py_ssize_t count;
    double *world[2];
    uint8_t *state[2];
} Hierarchy;

static Failure resolve(Hierarchy *h, Py_ssize_t index, int unscaled) {
    if (h->state[unscaled][index] == 2) return NULL;
    if (h->state[unscaled][index] == 1) return "hierarchy contains a cycle";
    h->state[unscaled][index] = 1;
    double *out = h->world[unscaled] + index * 16;
    const double *local = h->local + index * WIDTH;
    Failure failure = joint_matrix(local, !unscaled, out);
    const int32_t parent = h->parents[index];
    if (!failure && parent >= 0) {
        if (parent >= h->count) return "hierarchy contains an invalid parent index";
        const int compensating = h->compensating[index] != 0;
        failure = resolve(h, parent, compensating);
        if (!failure) {
            const double *parent_world = h->world[compensating] + (Py_ssize_t)parent * 16;
            if (compensating) {
                const double *parent_scale = h->local + (Py_ssize_t)parent * WIDTH + SCALE;
                double moved[16] = {0.0};
                moved[15] = 1.0;
                for (int c = 0; c < 3; ++c) moved[12 + c] = local[TRANSLATION + c] * parent_scale[c];
                out[12] = out[13] = out[14] = 0.0;
                matmul(out, parent_world, out);
                matmul(moved, parent_world, moved);
                memcpy(out + 12, moved + 12, 4 * sizeof(double));
            } else {
                matmul(out, parent_world, out);
            }
        }
    }
    h->state[unscaled][index] = 2;
    return failure;
}

static int get_items(PyObject *object, Py_buffer *view, int writable, Py_ssize_t size, Py_ssize_t count,
                     const char *name) {
    if (PyObject_GetBuffer(object, view, writable ? PyBUF_CONTIG : PyBUF_CONTIG_RO) < 0) return -1;
    if (view->len != size * count) {
        PyErr_Format(PyExc_ValueError, "%s holds %zd bytes, not %zd", name, view->len, size * count);
        PyBuffer_Release(view);
        return -1;
    }
    return 0;
}

static void release(Py_buffer *views, int count) {
    for (int i = 0; i < count; ++i)
        if (views[i].obj) PyBuffer_Release(&views[i]);
}

static PyObject *sample(PyObject *self, PyObject *args) {
    double frame;
    int slerp, weighted;
    PyObject *objects[7];
    Py_buffer views[7] = {{0}};
    if (!PyArg_ParseTuple(args, "dppOOOOOOO", &frame, &slerp, &weighted, &objects[0], &objects[1], &objects[2],
                          &objects[3], &objects[4], &objects[5], &objects[6]))
        return NULL;
    if (!isfinite(frame)) {
        PyErr_SetString(PyExc_ValueError, "sample frame must be finite");
        return NULL;
    }
    if (PyObject_GetBuffer(objects[0], &views[0], PyBUF_CONTIG_RO) < 0
        || PyObject_GetBuffer(objects[1], &views[1], PyBUF_CONTIG_RO) < 0
        || PyObject_GetBuffer(objects[2], &views[2], PyBUF_CONTIG_RO) < 0
        || views[2].len % (TRACK_ROW * (Py_ssize_t)sizeof(int64_t))
        || PyObject_GetBuffer(objects[6], &views[6], PyBUF_CONTIG_RO) < 0) {
        if (!PyErr_Occurred()) PyErr_SetString(PyExc_ValueError, "the track table is not whole rows");
        release(views, 7);
        return NULL;
    }
    const Py_ssize_t count = views[6].len / (Py_ssize_t)sizeof(double);
    const Py_ssize_t track_count = views[2].len / (TRACK_ROW * (Py_ssize_t)sizeof(int64_t));
    if (get_items(objects[3], &views[3], 0, 3 * sizeof(int32_t), count, "channels") < 0
        || get_items(objects[4], &views[4], 0, WIDTH * sizeof(double), count, "base") < 0
        || get_items(objects[5], &views[5], 0, WIDTH * sizeof(double), count, "rest") < 0) {
        release(views, 7);
        return NULL;
    }
    const double *frames = views[0].buf, *values = views[1].buf, *base = views[4].buf, *rest = views[5].buf,
                 *weights = views[6].buf;
    const int64_t *tracks = views[2].buf;
    const int32_t *channels = views[3].buf;
    const Py_ssize_t frame_count = views[0].len / (Py_ssize_t)sizeof(double);
    const Py_ssize_t value_count = views[1].len / (Py_ssize_t)sizeof(double);
    for (Py_ssize_t t = 0; t < track_count; ++t) {
        const int64_t *track = tracks + t * TRACK_ROW;
        if (track[KEYS] <= 0 || track[FRAME_START] < 0 || track[FRAME_START] + track[KEYS] > frame_count
            || track[VALUE_START] < 0 || track[VALUE_START] + track[KEYS] * track[VALUE_WIDTH] > value_count
            || (track[VALUE_WIDTH] != 3 && track[VALUE_WIDTH] != 4)) {
            PyErr_SetString(PyExc_ValueError, "a track's keys or values lie outside their arrays");
            release(views, 7);
            return NULL;
        }
    }
    for (Py_ssize_t i = 0; i < count * 3; ++i) {
        if (channels[i] >= track_count || (channels[i] >= 0 && tracks[channels[i] * TRACK_ROW + VALUE_WIDTH]
                                                                 != (i % 3 == 1 ? 4 : 3))) {
            PyErr_SetString(PyExc_ValueError, "a joint's channel names no track of its width");
            release(views, 7);
            return NULL;
        }
    }
    PyObject *result = PyBytes_FromStringAndSize(NULL, count * WIDTH * (Py_ssize_t)sizeof(double));
    if (!result) {
        release(views, 7);
        return NULL;
    }
    double *local = (double *)PyBytes_AS_STRING(result);
    Failure failure = NULL;
    Py_BEGIN_ALLOW_THREADS
    static const int offsets[3] = {TRANSLATION, ROTATION, SCALE};
    for (Py_ssize_t j = 0; j < count && !failure; ++j) {
        double *out = local + j * WIDTH;
        memcpy(out, base + j * WIDTH, WIDTH * sizeof(double));
        for (int c = 0; c < 3 && !failure; ++c) {
            const int32_t track = channels[j * 3 + c];
            if (track >= 0)
                failure = sample_track(tracks + (Py_ssize_t)track * TRACK_ROW, frames, values, frame, slerp,
                                       out + offsets[c]);
        }
        if (failure || !weighted) continue;
        const double weight = weights[j];
        if (weight <= 0.0) {
            memcpy(out, rest + j * WIDTH, WIDTH * sizeof(double));
        } else if (weight < 1.0) {
            double blended[WIDTH];
            memcpy(blended, rest + j * WIDTH, sizeof blended);
            failure = overwrite(blended, out, weight, BLEND_TRANSLATION | BLEND_ROTATION | BLEND_SCALE, slerp);
            memcpy(out, blended, sizeof blended);
        }
    }
    Py_END_ALLOW_THREADS
    release(views, 7);
    if (failure) {
        Py_DECREF(result);
        PyErr_SetString(PyExc_ValueError, failure);
        return NULL;
    }
    return result;
}

static PyObject *blend(PyObject *self, PyObject *args) {
    PyObject *objects[4];
    int additive, slerp;
    Py_buffer views[4] = {{0}};
    if (!PyArg_ParseTuple(args, "OOOOpp", &objects[0], &objects[1], &objects[2], &objects[3], &additive, &slerp))
        return NULL;
    if (PyObject_GetBuffer(objects[0], &views[0], PyBUF_CONTIG) < 0) return NULL;
    const Py_ssize_t count = views[0].len / (WIDTH * (Py_ssize_t)sizeof(double));
    if (views[0].len % (WIDTH * (Py_ssize_t)sizeof(double))
        || get_items(objects[1], &views[1], 0, WIDTH * sizeof(double), count, "layer") < 0
        || get_items(objects[2], &views[2], 0, sizeof(double), count, "amounts") < 0
        || get_items(objects[3], &views[3], 0, sizeof(uint8_t), count, "channels") < 0) {
        if (!PyErr_Occurred()) PyErr_SetString(PyExc_ValueError, "local transforms are not whole joints");
        release(views, 4);
        return NULL;
    }
    double *local = views[0].buf;
    const double *layer = views[1].buf, *amounts = views[2].buf;
    const uint8_t *channels = views[3].buf;
    Failure failure = NULL;
    Py_BEGIN_ALLOW_THREADS
    for (Py_ssize_t j = 0; j < count && !failure; ++j) {
        if (amounts[j] <= 0.0 || !channels[j]) continue;
        failure = (additive ? add : overwrite)(local + j * WIDTH, layer + j * WIDTH, amounts[j], channels[j], slerp);
    }
    Py_END_ALLOW_THREADS
    release(views, 4);
    if (failure) {
        PyErr_SetString(PyExc_ValueError, failure);
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject *matrices(PyObject *self, PyObject *object) {
    Py_buffer view = {0};
    if (PyObject_GetBuffer(object, &view, PyBUF_CONTIG_RO) < 0) return NULL;
    if (view.len % (WIDTH * (Py_ssize_t)sizeof(double))) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError, "local transforms are not whole joints");
        return NULL;
    }
    const Py_ssize_t count = view.len / (WIDTH * (Py_ssize_t)sizeof(double));
    PyObject *result = PyBytes_FromStringAndSize(NULL, count * 16 * (Py_ssize_t)sizeof(double));
    if (!result) {
        PyBuffer_Release(&view);
        return NULL;
    }
    double *out = (double *)PyBytes_AS_STRING(result);
    const double *local = view.buf;
    Failure failure = NULL;
    Py_BEGIN_ALLOW_THREADS
    for (Py_ssize_t j = 0; j < count && !failure; ++j) failure = joint_matrix(local + j * WIDTH, 1, out + j * 16);
    Py_END_ALLOW_THREADS
    PyBuffer_Release(&view);
    if (failure) {
        Py_DECREF(result);
        PyErr_SetString(PyExc_ValueError, failure);
        return NULL;
    }
    return result;
}

static PyObject *compose(PyObject *self, PyObject *args) {
    PyObject *objects[3];
    Py_buffer views[3] = {{0}};
    if (!PyArg_ParseTuple(args, "OOO", &objects[0], &objects[1], &objects[2])) return NULL;
    if (PyObject_GetBuffer(objects[0], &views[0], PyBUF_CONTIG_RO) < 0) return NULL;
    const Py_ssize_t count = views[0].len / (WIDTH * (Py_ssize_t)sizeof(double));
    if (views[0].len % (WIDTH * (Py_ssize_t)sizeof(double))
        || get_items(objects[1], &views[1], 0, sizeof(int32_t), count, "parents") < 0
        || get_items(objects[2], &views[2], 0, sizeof(uint8_t), count, "compensating") < 0) {
        if (!PyErr_Occurred()) PyErr_SetString(PyExc_ValueError, "local transforms are not whole joints");
        release(views, 3);
        return NULL;
    }
    PyObject *result = PyBytes_FromStringAndSize(NULL, count * 16 * (Py_ssize_t)sizeof(double));
    double *unscaled = count ? PyMem_RawMalloc((size_t)count * 16 * sizeof(double)) : NULL;
    uint8_t *state = count ? PyMem_RawCalloc((size_t)count * 2, 1) : NULL;
    if (!result || (count && (!unscaled || !state))) {
        Py_XDECREF(result);
        PyMem_RawFree(unscaled);
        PyMem_RawFree(state);
        release(views, 3);
        return PyErr_Occurred() ? NULL : PyErr_NoMemory();
    }
    Hierarchy h = {views[0].buf, views[1].buf, views[2].buf, count,
                   {(double *)PyBytes_AS_STRING(result), unscaled}, {state, state + count}};
    Failure failure = NULL;
    Py_BEGIN_ALLOW_THREADS
    for (Py_ssize_t j = 0; j < count && !failure; ++j) failure = resolve(&h, j, 0);
    Py_END_ALLOW_THREADS
    PyMem_RawFree(unscaled);
    PyMem_RawFree(state);
    release(views, 3);
    if (failure) {
        Py_DECREF(result);
        PyErr_SetString(PyExc_ValueError, failure);
        return NULL;
    }
    return result;
}

static PyMethodDef methods[] = {
    {"sample", sample, METH_VARARGS,
     "sample(frame, slerp, weighted, frames, values, tracks, channels, base, rest, weights) -> bytes\n"
     "Each joint's local transform at ``frame``: its ``base``, with the tracks its ``channels`` name (translation,\n"
     "rotation, scale; -1 for none) sampled over it, and, ``weighted``, blended from its ``rest`` by its weight."},
    {"blend", blend, METH_VARARGS,
     "blend(local, layer, amounts, channels, additive, slerp)\n"
     "Blends ``layer`` into ``local`` in place: overwriting, or added, by each joint's amount over its channels."},
    {"matrices", matrices, METH_O, "matrices(local) -> bytes\nEach joint's matrix."},
    {"compose", compose, METH_VARARGS,
     "compose(local, parents, compensating) -> bytes\nEach joint's world matrix through its parents (-1: none)."},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef moduledef = {PyModuleDef_HEAD_INIT, "fastmotion", NULL, -1, methods};

PyMODINIT_FUNC PyInit_fastmotion(void) {
    return PyModule_Create(&moduledef);
}
