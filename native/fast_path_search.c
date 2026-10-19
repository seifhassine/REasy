#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define MAX_PATH_CHARS 4096

static uint8_t lower_of[256], upper_of[256];

static inline uint32_t rotl(uint32_t x, int r) { return (x << r) | (x >> (32 - r)); }

static inline uint32_t mix(uint32_t h, uint32_t k) {
	k *= 0xcc9e2d51u; k = rotl(k, 15); k *= 0x1b873593u; h ^= k;
	return rotl(h, 13) * 5u + 0xe6546b64u;
}

static uint32_t hash_ascii(const uint8_t *text, size_t length, const uint8_t *table) {
	uint32_t h = 0xffffffffu;
	size_t i = 0;
	for (; i + 1 < length; i += 2) h = mix(h, table[text[i]] | ((uint32_t)table[text[i + 1]] << 16));
	if (i < length) {
		uint32_t k = table[text[i]];
		k *= 0xcc9e2d51u; k = rotl(k, 15); k *= 0x1b873593u; h ^= k;
	}
	h ^= (uint32_t)(length * 2);
	h ^= h >> 16; h *= 0x85ebca6bu; h ^= h >> 13; h *= 0xc2b2ae35u; h ^= h >> 16;
	return h;
}

typedef struct { const char *data; Py_ssize_t length; } Text;

static Text *texts_of(PyObject *sequence, Py_ssize_t *count) {
	PyObject *items = PySequence_Fast(sequence, "expected a sequence of strings");
	if (!items) return NULL;
	Py_ssize_t n = PySequence_Fast_GET_SIZE(items);
	Text *texts = PyMem_Malloc(sizeof(Text) * (n ? n : 1));
	if (!texts) { Py_DECREF(items); PyErr_NoMemory(); return NULL; }
	for (Py_ssize_t i = 0; i < n; ++i) {
		PyObject *item = PySequence_Fast_GET_ITEM(items, i);
		if (!PyUnicode_Check(item) || !PyUnicode_IS_ASCII(item)) {
			PyMem_Free(texts); Py_DECREF(items);
			PyErr_SetString(PyExc_ValueError, "blocks hold ASCII strings only");
			return NULL;
		}
		texts[i].data = (const char *)PyUnicode_1BYTE_DATA(item);
		texts[i].length = PyUnicode_GET_LENGTH(item);
	}
	Py_DECREF(items);
	*count = n;
	return texts;
}

enum { PRODUCT = 0, NUMERIC = 1, COMBINED = 2 };

typedef struct {
	int kind;
	uint64_t first, count;
	Text *heads, *tails, *endings;
	Py_ssize_t head_count, tail_count, ending_count;
	int tails_outer;
	Py_ssize_t ending_length;
	uint8_t *textures;
	int *slots;
	uint64_t sizes[3];
	int widths[3];
} Block;

static void block_free(Block *block) {
	PyMem_Free(block->heads); PyMem_Free(block->tails); PyMem_Free(block->endings);
	PyMem_Free(block->textures); PyMem_Free(block->slots);
}

static int as_u64(PyObject *value, uint64_t *out) {
	unsigned long long number = PyLong_AsUnsignedLongLong(value);
	if (number == (unsigned long long)-1 && PyErr_Occurred()) return -1;
	*out = number;
	return 0;
}

static int block_parse(PyObject *tuple, Block *block) {
	memset(block, 0, sizeof *block);
	if (!PyTuple_Check(tuple) || PyTuple_GET_SIZE(tuple) < 6) {
		PyErr_SetString(PyExc_TypeError, "a block is a tuple"); return -1;
	}
	block->kind = (int)PyLong_AsLong(PyTuple_GET_ITEM(tuple, 0));
	Py_ssize_t size = PyTuple_GET_SIZE(tuple);
	if (as_u64(PyTuple_GET_ITEM(tuple, size - 2), &block->first) < 0
	    || as_u64(PyTuple_GET_ITEM(tuple, size - 1), &block->count) < 0) return -1;
	if (block->kind == PRODUCT) {
		if (!(block->heads = texts_of(PyTuple_GET_ITEM(tuple, 1), &block->head_count))) return -1;
		if (!(block->tails = texts_of(PyTuple_GET_ITEM(tuple, 2), &block->tail_count))) return -1;
		block->tails_outer = PyObject_IsTrue(PyTuple_GET_ITEM(tuple, 3));
		if (block->head_count < 1 || block->tail_count < 1
		    || block->first + block->count > (uint64_t)block->head_count * (uint64_t)block->tail_count) {
			PyErr_SetString(PyExc_ValueError, "product block bounds"); return -1;
		}
	} else if (block->kind == NUMERIC) {
		PyObject *parts = PySequence_Fast(PyTuple_GET_ITEM(tuple, 1), "parts");
		if (!parts) return -1;
		Py_ssize_t n = PySequence_Fast_GET_SIZE(parts);
		block->heads = PyMem_Calloc(n ? n : 1, sizeof(Text));
		block->slots = PyMem_Malloc(sizeof(int) * (n ? n : 1));
		if (!block->heads || !block->slots) { Py_DECREF(parts); PyErr_NoMemory(); return -1; }
		for (Py_ssize_t i = 0; i < n; ++i) {
			PyObject *part = PySequence_Fast_GET_ITEM(parts, i);
			if (PyLong_Check(part)) {
				block->slots[i] = (int)PyLong_AsLong(part);
				if (block->slots[i] < 0 || block->slots[i] > 2) {
					Py_DECREF(parts); PyErr_SetString(PyExc_ValueError, "numeric slot"); return -1;
				}
			} else if (PyUnicode_Check(part) && PyUnicode_IS_ASCII(part)) {
				block->slots[i] = -1;
				block->heads[i].data = (const char *)PyUnicode_1BYTE_DATA(part);
				block->heads[i].length = PyUnicode_GET_LENGTH(part);
			} else {
				Py_DECREF(parts); PyErr_SetString(PyExc_ValueError, "blocks hold ASCII strings only"); return -1;
			}
		}
		Py_DECREF(parts);
		block->head_count = n;
		for (int s = 0; s < 3; ++s) {
			PyObject *size_item = PySequence_GetItem(PyTuple_GET_ITEM(tuple, 2), s);
			PyObject *width_item = PySequence_GetItem(PyTuple_GET_ITEM(tuple, 3), s);
			if (!size_item || !width_item) { Py_XDECREF(size_item); Py_XDECREF(width_item); return -1; }
			int bad = as_u64(size_item, &block->sizes[s]) < 0;
			block->widths[s] = (int)PyLong_AsLong(width_item);
			Py_DECREF(size_item); Py_DECREF(width_item);
			if (bad || PyErr_Occurred()) return -1;
			if (!block->sizes[s]) { PyErr_SetString(PyExc_ValueError, "numeric size"); return -1; }
		}
	} else if (block->kind == COMBINED && size == 8) {
		if (!(block->heads = texts_of(PyTuple_GET_ITEM(tuple, 1), &block->head_count))) return -1;
		block->ending_length = PyLong_AsSsize_t(PyTuple_GET_ITEM(tuple, 2));
		if (block->ending_length < 0 && PyErr_Occurred()) return -1;
		if (!(block->endings = texts_of(PyTuple_GET_ITEM(tuple, 3), &block->ending_count))) return -1;
		PyObject *textures = PySequence_Fast(PyTuple_GET_ITEM(tuple, 4), "textures");
		if (!textures) return -1;
		if (!(block->tails = texts_of(PyTuple_GET_ITEM(tuple, 5), &block->tail_count))) { Py_DECREF(textures); return -1; }
		if (PySequence_Fast_GET_SIZE(textures) != block->tail_count || block->tail_count < 1 || block->head_count < 1) {
			Py_DECREF(textures); PyErr_SetString(PyExc_ValueError, "combined block tails"); return -1;
		}
		block->textures = PyMem_Malloc((size_t)block->tail_count);
		if (!block->textures) { Py_DECREF(textures); PyErr_NoMemory(); return -1; }
		for (Py_ssize_t j = 0; j < block->tail_count; ++j)
			block->textures[j] = (uint8_t)PyObject_IsTrue(PySequence_Fast_GET_ITEM(textures, j));
		Py_DECREF(textures);
		for (Py_ssize_t s = 0; s < block->head_count; ++s)
			if (block->heads[s].length < block->ending_length) {
				PyErr_SetString(PyExc_ValueError, "a stem is shorter than its ending"); return -1;
			}
		uint64_t per_stem = 0;
		for (Py_ssize_t j = 0; j < block->tail_count; ++j)
			per_stem += (block->ending_length ? 2 : 1) + (block->textures[j] ? (uint64_t)block->ending_count : 0);
		if (block->first + block->count > per_stem * (uint64_t)block->head_count) {
			PyErr_SetString(PyExc_ValueError, "combined block bounds"); return -1;
		}
	} else {
		PyErr_SetString(PyExc_ValueError, "unknown block kind"); return -1;
	}
	return 0;
}

typedef struct { const uint8_t *bits; const uint64_t *keys; Py_ssize_t key_count; } Targets;

static inline int is_target(const uint8_t *text, size_t length, const Targets *targets) {
	uint32_t lower = hash_ascii(text, length, lower_of);
	if (!(targets->bits[lower >> 9] & (1u << ((lower >> 6) & 7)))) return 0;
	uint64_t key = ((uint64_t)hash_ascii(text, length, upper_of) << 32) | lower;
	Py_ssize_t low = 0, high = targets->key_count;
	while (low < high) {
		Py_ssize_t mid = low + (high - low) / 2;
		if (targets->keys[mid] < key) low = mid + 1; else high = mid;
	}
	return low < targets->key_count && targets->keys[low] == key;
}

typedef struct { Py_ssize_t *block; uint64_t *index; size_t count, capacity; int failed; } Found;

static void found_add(Found *found, Py_ssize_t block, uint64_t index) {
	if (found->count == found->capacity) {
		size_t capacity = found->capacity ? found->capacity * 2 : 64;
		Py_ssize_t *blocks = realloc(found->block, capacity * sizeof *blocks);
		uint64_t *indices = blocks ? realloc(found->index, capacity * sizeof *indices) : NULL;
		if (blocks) found->block = blocks;
		if (!indices) { found->failed = 1; return; }
		found->index = indices;
		found->capacity = capacity;
	}
	found->block[found->count] = block;
	found->index[found->count++] = index;
}

static inline size_t put(uint8_t *out, size_t at, const Text *text) {
	if (at + (size_t)text->length > MAX_PATH_CHARS) return MAX_PATH_CHARS + 1;
	memcpy(out + at, text->data, (size_t)text->length);
	return at + (size_t)text->length;
}

static inline size_t put_number(uint8_t *out, size_t at, uint64_t value, int width) {
	char digits[24];
	int n = 0;
	do { digits[n++] = (char)('0' + value % 10); value /= 10; } while (value);
	if (at + (size_t)(n > width ? n : width) > MAX_PATH_CHARS) return MAX_PATH_CHARS + 1;
	for (int pad = n; pad < width; ++pad) out[at++] = '0';
	while (n) out[at++] = (uint8_t)digits[--n];
	return at;
}

static void block_search(const Block *block, Py_ssize_t number, const Targets *targets, Found *found) {
	uint8_t out[MAX_PATH_CHARS + 1];
	if (block->kind == PRODUCT) {
		uint64_t inner = block->tails_outer ? (uint64_t)block->head_count : (uint64_t)block->tail_count;
		for (uint64_t k = block->first; k < block->first + block->count; ++k) {
			uint64_t major = k / inner, minor = k % inner;
			const Text *head = &block->heads[block->tails_outer ? minor : major];
			const Text *tail = &block->tails[block->tails_outer ? major : minor];
			size_t length = put(out, put(out, 0, head), tail);
			if (length <= MAX_PATH_CHARS && is_target(out, length, targets)) found_add(found, number, k);
		}
	} else if (block->kind == NUMERIC) {
		for (uint64_t k = block->first; k < block->first + block->count; ++k) {
			uint64_t values[3], rest = k;
			for (int s = 0; s < 3; ++s) { values[s] = rest % block->sizes[s]; rest /= block->sizes[s]; }
			size_t length = 0;
			for (Py_ssize_t i = 0; i < block->head_count && length <= MAX_PATH_CHARS; ++i)
				length = block->slots[i] < 0 ? put(out, length, &block->heads[i])
				                             : put_number(out, length, values[block->slots[i]], block->widths[block->slots[i]]);
			if (length <= MAX_PATH_CHARS && is_target(out, length, targets)) found_add(found, number, k);
		}
	} else {
		uint64_t plain = block->ending_length ? 2 : 1, textured = plain + (uint64_t)block->ending_count;
		uint64_t per_stem = 0;
		for (Py_ssize_t j = 0; j < block->tail_count; ++j) per_stem += block->textures[j] ? textured : plain;
		uint64_t k = block->first, end = block->first + block->count;
		uint64_t stem = k / per_stem, r = k % per_stem;
		Py_ssize_t tail = 0;
		while (r >= (block->textures[tail] ? textured : plain)) r -= block->textures[tail++] ? textured : plain;
		for (; k < end; ++k) {
			const Text *text = &block->heads[stem];
			Text base = { text->data, text->length - block->ending_length };
			size_t length = r == 0 ? put(out, 0, text) : put(out, 0, &base);
			if (r >= plain) length = put(out, length, &block->endings[r - plain]);
			length = length <= MAX_PATH_CHARS ? put(out, length, &block->tails[tail]) : length;
			if (length <= MAX_PATH_CHARS && is_target(out, length, targets)) found_add(found, number, k);
			if (++r == (block->textures[tail] ? textured : plain)) {
				r = 0;
				if (++tail == block->tail_count) { tail = 0; ++stem; }
			}
		}
	}
}

static PyObject *match_blocks(PyObject *self, PyObject *args) {
	PyObject *blocks_object, *bits_object, *keys_object;
	if (!PyArg_ParseTuple(args, "OOO", &blocks_object, &bits_object, &keys_object)) return NULL;
	Py_buffer bits, keys;
	if (PyObject_GetBuffer(bits_object, &bits, PyBUF_CONTIG_RO) < 0) return NULL;
	if (PyObject_GetBuffer(keys_object, &keys, PyBUF_CONTIG_RO) < 0) { PyBuffer_Release(&bits); return NULL; }
	PyObject *result = NULL;
	PyObject *sequence = NULL;
	Block *blocks = NULL;
	Py_ssize_t count = 0;
	Found found = {0};
	if (bits.len != (1 << 23) || keys.len % 8) {
		PyErr_SetString(PyExc_ValueError, "the targets' bits are 2**23 bytes and their keys 64-bit");
		goto done;
	}
	sequence = PySequence_Fast(blocks_object, "blocks");
	if (!sequence) goto done;
	count = PySequence_Fast_GET_SIZE(sequence);
	blocks = PyMem_Calloc(count ? count : 1, sizeof(Block));
	if (!blocks) { PyErr_NoMemory(); goto done; }
	for (Py_ssize_t i = 0; i < count; ++i)
		if (block_parse(PySequence_Fast_GET_ITEM(sequence, i), &blocks[i]) < 0) goto done;
	Targets targets = { bits.buf, keys.buf, keys.len / 8 };
	Py_BEGIN_ALLOW_THREADS
	for (Py_ssize_t i = 0; i < count && !found.failed; ++i) block_search(&blocks[i], i, &targets, &found);
	Py_END_ALLOW_THREADS
	if (found.failed) { PyErr_NoMemory(); goto done; }
	result = PyList_New((Py_ssize_t)found.count);
	if (!result) goto done;
	for (size_t i = 0; i < found.count; ++i) {
		PyObject *pair = Py_BuildValue("(nK)", found.block[i], (unsigned long long)found.index[i]);
		if (!pair) { Py_CLEAR(result); goto done; }
		PyList_SET_ITEM(result, (Py_ssize_t)i, pair);
	}
done:
	if (blocks) {
		for (Py_ssize_t i = 0; i < count; ++i) block_free(&blocks[i]);
		PyMem_Free(blocks);
	}
	Py_XDECREF(sequence);
	free(found.block); free(found.index);
	PyBuffer_Release(&bits); PyBuffer_Release(&keys);
	return result;
}

static PyObject *render_stems(PyObject *self, PyObject *args) {
	PyObject *stem, *runs;
	if (!PyArg_ParseTuple(args, "UO", &stem, &runs)) return NULL;
	PyObject *seen = PySet_New(NULL), *stems = PyList_New(0), *sequence = PySequence_Fast(runs, "runs");
	if (!seen || !stems || !sequence || PySet_Add(seen, stem) < 0) goto fail;
	for (Py_ssize_t r = 0; r < PySequence_Fast_GET_SIZE(sequence); ++r) {
		PyObject *run = PySequence_Fast_GET_ITEM(sequence, r);
		PyObject *made = NULL;
		if (PyTuple_Check(run) && PyTuple_GET_SIZE(run) == 3) {
			PyObject *pieces = PyTuple_GET_ITEM(run, 0), *values = PySequence_Fast(PyTuple_GET_ITEM(run, 2), "values");
			int width = (int)PyLong_AsLong(PyTuple_GET_ITEM(run, 1));
			if (!values || PyErr_Occurred()) { Py_XDECREF(values); goto fail; }
			PyObject *piece_list = PySequence_List(pieces);
			if (!piece_list) { Py_DECREF(values); goto fail; }
			Py_ssize_t n = PySequence_Fast_GET_SIZE(values);
			made = PyList_New(n);
			Py_ssize_t piece_count = PyList_GET_SIZE(piece_list), fixed = 0;
			int ascii = 1;
			for (Py_ssize_t p = 0; p < piece_count; ++p) {
				PyObject *piece = PyList_GET_ITEM(piece_list, p);
				if (!PyUnicode_Check(piece) || !PyUnicode_IS_ASCII(piece)) { ascii = 0; break; }
				fixed += PyUnicode_GET_LENGTH(piece);
			}
			for (Py_ssize_t i = 0; made && ascii && i < n; ++i) {
				unsigned long long value = PyLong_AsUnsignedLongLong(PySequence_Fast_GET_ITEM(values, i));
				if (PyErr_Occurred()) { Py_CLEAR(made); break; }
				char digits[24], number[64];
				int length = 0, at = 0;
				do { digits[length++] = (char)('0' + value % 10); value /= 10; } while (value);
				for (int pad = length; pad < width && at < 40; ++pad) number[at++] = '0';
				while (length) number[at++] = digits[--length];
				PyObject *candidate = PyUnicode_New(fixed + (piece_count - 1) * at, 127);
				if (!candidate) { Py_CLEAR(made); break; }
				Py_UCS1 *out = PyUnicode_1BYTE_DATA(candidate);
				for (Py_ssize_t p = 0; p < piece_count; ++p) {
					PyObject *piece = PyList_GET_ITEM(piece_list, p);
					if (p) { memcpy(out, number, (size_t)at); out += at; }
					memcpy(out, PyUnicode_1BYTE_DATA(piece), (size_t)PyUnicode_GET_LENGTH(piece));
					out += PyUnicode_GET_LENGTH(piece);
				}
				PyList_SET_ITEM(made, i, candidate);
			}
			for (Py_ssize_t i = 0; made && !ascii && i < n; ++i) {
				unsigned long long value = PyLong_AsUnsignedLongLong(PySequence_Fast_GET_ITEM(values, i));
				if (PyErr_Occurred()) { Py_CLEAR(made); break; }
				char digits[64];
				int length = 0;
				do { digits[length++] = (char)('0' + value % 10); value /= 10; } while (value);
				char text[64];
				int at = 0;
				for (int pad = length; pad < width && at < 40; ++pad) text[at++] = '0';
				while (length) text[at++] = digits[--length];
				PyObject *number = PyUnicode_FromStringAndSize(text, at);
				PyObject *candidate = number ? PyUnicode_Join(number, piece_list) : NULL;
				Py_XDECREF(number);
				if (!candidate) { Py_CLEAR(made); break; }
				PyList_SET_ITEM(made, i, candidate);
			}
			Py_DECREF(piece_list);
			Py_DECREF(values);
			if (!made) goto fail;
		} else {
			made = PySequence_List(run);
			if (!made) goto fail;
		}
		for (Py_ssize_t i = 0; i < PyList_GET_SIZE(made); ++i) {
			PyObject *candidate = PyList_GET_ITEM(made, i);
			int known = PySet_Contains(seen, candidate);
			if (known < 0 || (!known && (PySet_Add(seen, candidate) < 0 || PyList_Append(stems, candidate) < 0))) {
				Py_DECREF(made); goto fail;
			}
		}
		Py_DECREF(made);
	}
	Py_DECREF(seen); Py_DECREF(sequence);
	return stems;
fail:
	Py_XDECREF(seen); Py_XDECREF(stems); Py_XDECREF(sequence);
	return NULL;
}

static PyMethodDef Methods[] = {
	{"match_blocks", match_blocks, METH_VARARGS, "Candidates of blocks whose RE Engine path hash is a target."},
	{"render_stems", render_stems, METH_VARARGS, "Numeric probe stems in order, without duplicates."},
	{NULL, NULL, 0, NULL}
};

static struct PyModuleDef Module = {
	PyModuleDef_HEAD_INIT, "fast_path_search", "The list improver's CPU search.", -1, Methods
};

PyMODINIT_FUNC PyInit_fast_path_search(void) {
	for (int c = 0; c < 256; ++c) {
		lower_of[c] = (uint8_t)(c >= 'A' && c <= 'Z' ? c + 32 : c);
		upper_of[c] = (uint8_t)(c >= 'a' && c <= 'z' ? c - 32 : c);
	}
	return PyModule_Create(&Module);
}
