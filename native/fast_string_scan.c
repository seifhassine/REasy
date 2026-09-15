#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>

static int is_utf8_candidate_byte(uint8_t value) {
	return (value >= 0x20 && value <= 0x7E) || value >= 0x80;
}

static int unicode_is_printable(PyObject* value) {
	Py_ssize_t length = PyUnicode_GetLength(value);
	if (length < 0) return -1;

	int kind = PyUnicode_KIND(value);
	void* data = PyUnicode_DATA(value);
	for (Py_ssize_t i = 0; i < length; ++i) {
		if (!Py_UNICODE_ISPRINTABLE(PyUnicode_READ(kind, data, i))) {
			return 0;
		}
	}
	return 1;
}

static PyObject* unicode_from_utf16_code_units(
	const uint8_t* data,
	Py_ssize_t start,
	Py_ssize_t end,
	Py_UCS4 maxchar
) {
	Py_ssize_t length = (end - start) / 2;
	PyObject* value = PyUnicode_New(length, maxchar);
	if (!value) return NULL;

	int kind = PyUnicode_KIND(value);
	void* unicode_data = PyUnicode_DATA(value);
	for (Py_ssize_t i = 0; i < length; ++i) {
		Py_ssize_t offset = start + (i * 2);
		Py_UCS4 codepoint = (Py_UCS4)data[offset] | ((Py_UCS4)data[offset + 1] << 8);
		PyUnicode_WRITE(kind, unicode_data, i, codepoint);
	}
	return value;
}

static PyObject* extract_strings(PyObject* self, PyObject* args) {
	PyObject* source;
	Py_ssize_t min_length;
	if (!PyArg_ParseTuple(args, "On", &source, &min_length)) {
		return NULL;
	}
	if (min_length <= 0) {
		PyErr_SetString(PyExc_ValueError, "min_length must be positive");
		return NULL;
	}
	if (min_length > PY_SSIZE_T_MAX / 2) {
		PyErr_SetString(PyExc_OverflowError, "min_length is too large");
		return NULL;
	}

	Py_buffer view;
	if (PyObject_GetBuffer(source, &view, PyBUF_CONTIG_RO) != 0) {
		return NULL;
	}

	const uint8_t* data = (const uint8_t*)view.buf;
	Py_ssize_t data_length = view.len;
	Py_ssize_t min_bytes = min_length * 2;
	PyObject* strings = PyList_New(0);
	if (!strings) {
		PyBuffer_Release(&view);
		return NULL;
	}

	for (Py_ssize_t parity = 0; parity <= 1; ++parity) {
		Py_ssize_t i = parity;
		while (i <= data_length - min_bytes) {
			if (!(data[i] >= 32 && data[i] <= 126 && data[i + 1] == 0)) {
				i += 2;
				continue;
			}

			Py_ssize_t j = i;
			Py_UCS4 maxchar = 0;
			while (j <= data_length - 2) {
				Py_UCS4 codepoint = (Py_UCS4)data[j] | ((Py_UCS4)data[j + 1] << 8);
				if (codepoint == 0 || !Py_UNICODE_ISPRINTABLE(codepoint)) {
					break;
				}
				if (codepoint > maxchar) maxchar = codepoint;
				j += 2;
			}

			if ((j - i) / 2 >= min_length) {
				PyObject* value = unicode_from_utf16_code_units(data, i, j, maxchar);
				if (!value || PyList_Append(strings, value) != 0) {
					Py_XDECREF(value);
					Py_DECREF(strings);
					PyBuffer_Release(&view);
					return NULL;
				}
				Py_DECREF(value);
				i = j + 2;
			} else {
				i += 2;
			}
		}
	}

	Py_ssize_t utf8_min_bytes = min_length > 10 ? min_length : 10;
	Py_ssize_t i = 0;
	while (i < data_length) {
		while (i < data_length && !is_utf8_candidate_byte(data[i])) i++;
		Py_ssize_t start = i;
		while (i < data_length && is_utf8_candidate_byte(data[i])) i++;

		Py_ssize_t length = i - start;
		if (length < utf8_min_bytes) continue;

		PyObject* value = PyUnicode_DecodeUTF8((const char*)data + start, length, "strict");
		if (!value) {
			if (PyErr_ExceptionMatches(PyExc_UnicodeDecodeError)) {
				PyErr_Clear();
				continue;
			}
			Py_DECREF(strings);
			PyBuffer_Release(&view);
			return NULL;
		}

		int printable = unicode_is_printable(value);
		if (printable < 0 || (printable && PyList_Append(strings, value) != 0)) {
			Py_DECREF(value);
			Py_DECREF(strings);
			PyBuffer_Release(&view);
			return NULL;
		}
		Py_DECREF(value);
	}

	PyBuffer_Release(&view);
	return strings;
}

static PyMethodDef Methods[] = {
	{"extract_strings", (PyCFunction)extract_strings, METH_VARARGS, "Extract UTF-16LE and UTF-8 printable strings from bytes"},
	{NULL, NULL, 0, NULL}
};

static struct PyModuleDef Module = {
	PyModuleDef_HEAD_INIT,
	"fast_string_scan",
	"Fast UTF-16LE and UTF-8 printable string extraction.",
	-1,
	Methods
};

PyMODINIT_FUNC PyInit_fast_string_scan(void) {
	return PyModule_Create(&Module);
}
