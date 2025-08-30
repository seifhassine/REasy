#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>

static inline uint32_t rotl32(uint32_t x, int8_t r) {
	return (x << r) | (x >> (32 - r));
}
static inline uint32_t fmix32(uint32_t h) {
	h ^= h >> 16;
	h *= 0x85ebca6bU;
	h ^= h >> 13;
	h *= 0xc2b2ae35U;
	h ^= h >> 16;
	return h;
}
static uint32_t murmur3_32(const uint8_t* data, Py_ssize_t len, uint32_t seed) {
	const uint32_t c1 = 0xcc9e2d51U, c2 = 0x1b873593U; uint32_t h1 = seed; Py_ssize_t i = 0;
	while (i + 4 <= len) {
		uint32_t k1 = (uint32_t)data[i] | ((uint32_t)data[i+1] << 8) | ((uint32_t)data[i+2] << 16) | ((uint32_t)data[i+3] << 24);
		i += 4; k1 *= c1; k1 = rotl32(k1, 15); k1 *= c2; h1 ^= k1; h1 = rotl32(h1, 13); h1 = h1 * 5 + 0xe6546b64U;
	}
	uint32_t k1 = 0; switch (len - i) {
		case 3: k1 ^= ((uint32_t)data[i+2] << 16);
		case 2: k1 ^= ((uint32_t)data[i+1] << 8);
		case 1: k1 ^= ((uint32_t)data[i]); k1 *= c1; k1 = rotl32(k1, 15); k1 *= c2; h1 ^= k1;
	}
	h1 ^= (uint32_t)len; return fmix32(h1);
}

static int get_utf8_ptr(PyObject* u, const uint8_t** buf, Py_ssize_t* len) {
	const char* s = PyUnicode_AsUTF8AndSize(u, len);
	if (!s) return -1; *buf = (const uint8_t*)s; return 0;
}

static int get_utf16le_bytes(PyObject* u, PyObject** out_b, const uint8_t** buf, Py_ssize_t* len) {
	PyObject* b = PyUnicode_AsEncodedString(u, "utf-16le", "strict");
	if (!b) return -1; char* p = NULL; if (PyBytes_AsStringAndSize(b, &p, len) != 0) { Py_DECREF(b); return -1; }
	*out_b = b; *buf = (const uint8_t*)p; return 0;
}

static PyObject* resolve_paths_common(PyObject* self, PyObject* args, int encoding_utf16) {
	PyObject* cache; PyObject* seq;
	if (!PyArg_ParseTuple(args, "OO", &cache, &seq)) return NULL;
	PyObject* list = PySequence_Fast(seq, "paths must be a sequence");
	if (!list) return NULL;
	Py_ssize_t n = PySequence_Fast_GET_SIZE(list);
	PyObject** items = PySequence_Fast_ITEMS(list);
	PyObject* remaining = PyList_New(0);
	if (!remaining) { Py_DECREF(list); return NULL; }
	unsigned long long updated = 0ULL;

	PyObject** lowers = (PyObject**)PyMem_Calloc(n, sizeof(PyObject*));
	PyObject** uppers = (PyObject**)PyMem_Calloc(n, sizeof(PyObject*));
	PyObject** lbytes = (PyObject**)PyMem_Calloc(n, sizeof(PyObject*));
	PyObject** ubytes = (PyObject**)PyMem_Calloc(n, sizeof(PyObject*));
	const uint8_t** lbufs = (const uint8_t**)PyMem_Calloc(n, sizeof(uint8_t*));
	const uint8_t** ubufs = (const uint8_t**)PyMem_Calloc(n, sizeof(uint8_t*));
	Py_ssize_t* llens = (Py_ssize_t*)PyMem_Calloc(n, sizeof(Py_ssize_t));
	Py_ssize_t* ulens = (Py_ssize_t*)PyMem_Calloc(n, sizeof(Py_ssize_t));
	unsigned long long* hashes = (unsigned long long*)PyMem_Malloc(n * sizeof(unsigned long long));
	if (!lowers || !uppers || !lbytes || !ubytes || !lbufs || !ubufs || !llens || !ulens || !hashes) {
		PyMem_Free(lowers); PyMem_Free(uppers); PyMem_Free(lbytes); PyMem_Free(ubytes);
		PyMem_Free(lbufs); PyMem_Free(ubufs); PyMem_Free(llens); PyMem_Free(ulens); PyMem_Free(hashes);
		Py_DECREF(remaining); Py_DECREF(list); return PyErr_NoMemory();
	}

	for (Py_ssize_t i = 0; i < n; ++i) {
		PyObject* s = items[i];
		if (!PyUnicode_Check(s)) continue;
		lowers[i] = PyObject_CallMethod(s, "lower", NULL);
		uppers[i] = PyObject_CallMethod(s, "upper", NULL);
		if (!lowers[i] || !uppers[i]) { Py_XDECREF(lowers[i]); Py_XDECREF(uppers[i]); lowers[i]=uppers[i]=NULL; continue; }
		if (!encoding_utf16) {
			if (get_utf8_ptr(lowers[i], &lbufs[i], &llens[i]) != 0 || get_utf8_ptr(uppers[i], &ubufs[i], &ulens[i]) != 0) {
				lbufs[i]=ubufs[i]=NULL; llens[i]=ulens[i]=0;
			}
		} else {
			if (get_utf16le_bytes(lowers[i], &lbytes[i], &lbufs[i], &llens[i]) != 0 || get_utf16le_bytes(uppers[i], &ubytes[i], &ubufs[i], &ulens[i]) != 0) {
				lbufs[i]=ubufs[i]=NULL; llens[i]=ulens[i]=0;
			}
		}
	}

	Py_BEGIN_ALLOW_THREADS
	for (Py_ssize_t i = 0; i < n; ++i) {
		if (!lbufs[i] || !ubufs[i]) { hashes[i] = 0ULL; continue; }
		uint32_t lo = murmur3_32(lbufs[i], llens[i], 0xFFFFFFFFU);
		uint32_t up = murmur3_32(ubufs[i], ulens[i], 0xFFFFFFFFU);
		hashes[i] = ((unsigned long long)up << 32) | (unsigned long long)lo;
	}
	Py_END_ALLOW_THREADS

	for (Py_ssize_t i = 0; i < n; ++i) {
		if (!hashes[i]) continue;
		PyObject* key = PyLong_FromUnsignedLongLong(hashes[i]);
		PyObject* val = PyDict_GetItemWithError(cache, key);
		Py_DECREF(key);
		if (!val) {
			if (PyErr_Occurred()) {
				goto cleanup;
			}
			if (!encoding_utf16) {
				PyObject* s = items[i];
				PyList_Append(remaining, s);
			}
			continue;
		}
		PyObject* entry = PyTuple_GetItem(val, 1);
		if (entry) {
			PyObject* cur = PyObject_GetAttrString(entry, "path");
			if (cur == Py_None || cur == NULL) {
				PyObject* s = items[i];
				if (PyObject_SetAttrString(entry, "path", s) == 0) { updated++; }
			}
			Py_XDECREF(cur);
		}
	}

cleanup:
	for (Py_ssize_t i = 0; i < n; ++i) {
		Py_XDECREF(lbytes[i]); Py_XDECREF(ubytes[i]);
		Py_XDECREF(lowers[i]); Py_XDECREF(uppers[i]);
	}
	PyMem_Free(lowers); PyMem_Free(uppers); PyMem_Free(lbytes); PyMem_Free(ubytes);
	PyMem_Free(lbufs); PyMem_Free(ubufs); PyMem_Free(llens); PyMem_Free(ulens); PyMem_Free(hashes);
	{
		PyObject* result = Py_BuildValue("(NK)", remaining, (unsigned long long)updated);
		Py_DECREF(list);
		return result;
	}
}

static PyObject* resolve_paths_utf8(PyObject* self, PyObject* args) { return resolve_paths_common(self, args, 0); }
static PyObject* resolve_paths_utf16le(PyObject* self, PyObject* args) { return resolve_paths_common(self, args, 1); }

static PyObject* murmur3_hash(PyObject* self, PyObject* args) {
	const char* data;
	Py_ssize_t len;
	if (!PyArg_ParseTuple(args, "y#", &data, &len)) {
		return NULL;
	}
	uint32_t hash = murmur3_32((const uint8_t*)data, len, 0xFFFFFFFFU);
	return PyLong_FromUnsignedLong(hash);
}

static PyMethodDef Methods[] = {
	{"resolve_paths_utf8", (PyCFunction)resolve_paths_utf8, METH_VARARGS, "Resolve paths via UTF-8 hashing; returns (remaining_paths, updated_count)"},
	{"resolve_paths_utf16le", (PyCFunction)resolve_paths_utf16le, METH_VARARGS, "Resolve paths via UTF-16LE hashing; returns (remaining_paths, updated_count)"},
	{"murmur3_hash", (PyCFunction)murmur3_hash, METH_VARARGS, "Compute MurmurHash3 32-bit hash of bytes"},
	{NULL, NULL, 0, NULL}
};

static struct PyModuleDef Module = {
	PyModuleDef_HEAD_INIT,
	"fast_pakresolve",
	"Fast resolve of PAK paths into cache entries.",
	-1,
	Methods
};

PyMODINIT_FUNC PyInit_fast_pakresolve(void) { return PyModule_Create(&Module); }

