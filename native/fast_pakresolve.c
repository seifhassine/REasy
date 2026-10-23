#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>
#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#else
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#endif

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

static PyObject* path_hashes(PyObject* self, PyObject* args) {
	PyObject* seq;
	if (!PyArg_ParseTuple(args, "O", &seq)) return NULL;
	PyObject* list = PySequence_Fast(seq, "paths must be a sequence");
	if (!list) return NULL;
	Py_ssize_t n = PySequence_Fast_GET_SIZE(list);
	PyObject** items = PySequence_Fast_ITEMS(list);
	PyObject* out = PyBytes_FromStringAndSize(NULL, n * (Py_ssize_t)sizeof(unsigned long long));
	PyObject** encoded = (PyObject**)PyMem_Calloc(n ? n * 2 : 1, sizeof(PyObject*));
	if (!out || !encoded) {
		Py_XDECREF(out); PyMem_Free(encoded); Py_DECREF(list);
		return PyErr_NoMemory();
	}
	unsigned long long* hashes = (unsigned long long*)PyBytes_AS_STRING(out);
	for (Py_ssize_t i = 0; i < n; ++i) {
		if (!PyUnicode_Check(items[i])) continue;
		PyObject* lower = PyObject_CallMethod(items[i], "lower", NULL);
		PyObject* upper = lower ? PyObject_CallMethod(items[i], "upper", NULL) : NULL;
		if (lower && upper) {
			encoded[2 * i] = PyUnicode_AsEncodedString(lower, "utf-16le", "strict");
			encoded[2 * i + 1] = encoded[2 * i] ? PyUnicode_AsEncodedString(upper, "utf-16le", "strict") : NULL;
		}
		Py_XDECREF(lower); Py_XDECREF(upper);
		if (!encoded[2 * i] || !encoded[2 * i + 1]) {
			for (Py_ssize_t j = 0; j <= 2 * i + 1; ++j) Py_XDECREF(encoded[j]);
			PyMem_Free(encoded); Py_DECREF(out); Py_DECREF(list);
			return NULL;
		}
	}
	Py_BEGIN_ALLOW_THREADS
	for (Py_ssize_t i = 0; i < n; ++i) {
		if (!encoded[2 * i]) { hashes[i] = 0ULL; continue; }
		uint32_t lo = murmur3_32((const uint8_t*)PyBytes_AS_STRING(encoded[2 * i]), PyBytes_GET_SIZE(encoded[2 * i]), 0xFFFFFFFFU);
		uint32_t up = murmur3_32((const uint8_t*)PyBytes_AS_STRING(encoded[2 * i + 1]), PyBytes_GET_SIZE(encoded[2 * i + 1]), 0xFFFFFFFFU);
		hashes[i] = ((unsigned long long)up << 32) | (unsigned long long)lo;
	}
	Py_END_ALLOW_THREADS
	for (Py_ssize_t i = 0; i < 2 * n; ++i) Py_XDECREF(encoded[i]);
	PyMem_Free(encoded);
	Py_DECREF(list);
	return out;
}

static PyObject* murmur3_hash(PyObject* self, PyObject* args) {
	const char* data;
	Py_ssize_t len;
	if (!PyArg_ParseTuple(args, "y#", &data, &len)) {
		return NULL;
	}
	uint32_t hash = murmur3_32((const uint8_t*)data, len, 0xFFFFFFFFU);
	return PyLong_FromUnsignedLong(hash);
}

/* Without the GIL: Python's open, seek, read and close each wait to reacquire it, stalling reads beside busy threads.
 * Full sharing lets the file be replaced meanwhile. */
static PyObject* read_at(PyObject* self, PyObject* args) {
	PyObject* path;
	long long offset;
	Py_ssize_t size;
	if (!PyArg_ParseTuple(args, "ULn", &path, &offset, &size)) {
		return NULL;
	}
	if (offset < 0 || size < 0) {
		PyErr_SetString(PyExc_ValueError, "read_at takes a non-negative offset and size");
		return NULL;
	}
	PyObject* out = PyBytes_FromStringAndSize(NULL, size);
	if (!out) {
		return NULL;
	}
	char* buffer = PyBytes_AS_STRING(out);
	Py_ssize_t done = 0;
#ifdef _WIN32
	wchar_t* wide = PyUnicode_AsWideCharString(path, NULL);
	if (!wide) {
		Py_DECREF(out);
		return NULL;
	}
	DWORD error = 0;
	Py_BEGIN_ALLOW_THREADS
	HANDLE file = CreateFileW(wide, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL,
		OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
	if (file == INVALID_HANDLE_VALUE) {
		error = GetLastError();
	} else {
		while (done < size) {
			unsigned long long position = (unsigned long long)offset + (unsigned long long)done;
			OVERLAPPED at = {0};
			at.Offset = (DWORD)position;
			at.OffsetHigh = (DWORD)(position >> 32);
			DWORD wanted = (DWORD)(size - done < (1 << 30) ? size - done : (1 << 30)), got = 0;
			if (!ReadFile(file, buffer + done, wanted, &got, &at)) {
				error = GetLastError() == ERROR_HANDLE_EOF ? 0 : GetLastError();
				break;
			}
			if (got == 0) {
				break;
			}
			done += got;
		}
		CloseHandle(file);
	}
	Py_END_ALLOW_THREADS
	PyMem_Free(wide);
	if (error) {
		Py_DECREF(out);
		return PyErr_SetExcFromWindowsErrWithFilenameObject(PyExc_OSError, (int)error, path);
	}
#else
	PyObject* encoded;
	if (!PyUnicode_FSConverter(path, &encoded)) {
		Py_DECREF(out);
		return NULL;
	}
	const char* name = PyBytes_AS_STRING(encoded);
	int error = 0;
	Py_BEGIN_ALLOW_THREADS
	int file = open(name, O_RDONLY | O_CLOEXEC);
	if (file < 0) {
		error = errno;
	} else {
		while (done < size) {
			ssize_t got = pread(file, buffer + done, (size_t)(size - done), (off_t)(offset + done));
			if (got < 0 && errno == EINTR) {
				continue;
			}
			if (got <= 0) {
				error = got < 0 ? errno : 0;
				break;
			}
			done += got;
		}
		close(file);
	}
	Py_END_ALLOW_THREADS
	if (error) {
		Py_DECREF(out);
		errno = error;
		PyErr_SetFromErrnoWithFilenameObject(PyExc_OSError, path);
		Py_DECREF(encoded);
		return NULL;
	}
	Py_DECREF(encoded);
#endif
	if (done < size && _PyBytes_Resize(&out, done) < 0) {
		return NULL;
	}
	return out;
}

static PyMethodDef Methods[] = {
	{"path_hashes", (PyCFunction)path_hashes, METH_VARARGS, "path_hashes(paths): each normalized path's PAK hash, as little-endian u64 bytes (0 for a non-string)"},
	{"murmur3_hash", (PyCFunction)murmur3_hash, METH_VARARGS, "Compute MurmurHash3 32-bit hash of bytes"},
	{"read_at", (PyCFunction)read_at, METH_VARARGS, "read_at(path, offset, size): the bytes there, read without the GIL"},
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

