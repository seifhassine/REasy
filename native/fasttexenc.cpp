#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <algorithm>
#include <cstdint>
#include <cstring>
#include <thread>
#include <vector>

#include "third_party/bc7e/basisu_bc7e_scalar.h"
#include "third_party/rgbcx/rgbcx.h"

namespace {

enum : int { BC1 = 71, BC1_SRGB = 72, BC3 = 77, BC3_SRGB = 78, BC4 = 80, BC5 = 83, BC7 = 98, BC7_SRGB = 99 };
constexpr uint32_t BC1_LEVEL = 18;

bc7e_scalar::bc7e_compress_block_params bc7_data, bc7_colour;

size_t block_size(int format) { return format == BC1 || format == BC1_SRGB || format == BC4 ? 8 : 16; }

void encode_rows(int format, const uint8_t* source, int width, int height, uint8_t* out, int first, int last) {
    const int columns = (width + 3) / 4;
    const size_t block_bytes = block_size(format);
    std::vector<uint32_t> texels(size_t(columns) * 16);
    for (int row = first; row < last; ++row) {
        for (int column = 0; column < columns; ++column)
            for (int texel = 0; texel < 16; ++texel)
                std::memcpy(&texels[size_t(column) * 16 + texel],
                            source + (size_t(std::min(row * 4 + texel / 4, height - 1)) * width +
                                      std::min(column * 4 + texel % 4, width - 1)) * 4, 4);
        uint8_t* blocks = out + size_t(row) * columns * block_bytes;
        if (format == BC7 || format == BC7_SRGB) {
            bc7e_scalar::bc7e_compress_blocks(columns, reinterpret_cast<uint64_t*>(blocks), texels.data(),
                                              format == BC7_SRGB ? &bc7_colour : &bc7_data);
            continue;
        }
        for (int column = 0; column < columns; ++column) {
            const auto* block_texels = reinterpret_cast<const uint8_t*>(&texels[size_t(column) * 16]);
            uint8_t* block = blocks + column * block_bytes;
            if (format == BC4) rgbcx::encode_bc4_hq(block, block_texels);
            else if (format == BC5) rgbcx::encode_bc5_hq(block, block_texels);
            else if (format == BC3 || format == BC3_SRGB) rgbcx::encode_bc3_hq(BC1_LEVEL, block, block_texels);
            else rgbcx::encode_bc1(BC1_LEVEL, block, block_texels, true, false);
        }
    }
}

PyObject* encode(PyObject*, PyObject* args) {
    int format, width, height;
    Py_buffer view;
    if (!PyArg_ParseTuple(args, "iy*ii:encode", &format, &view, &width, &height)) return nullptr;
    if (format != BC1 && format != BC1_SRGB && format != BC3 && format != BC3_SRGB && format != BC4 && format != BC5 &&
        format != BC7 && format != BC7_SRGB)
        PyErr_Format(PyExc_ValueError, "no block encoder for DXGI format %d", format);
    else if (width < 1 || height < 1 || view.len != Py_ssize_t(width) * height * 4)
        PyErr_Format(PyExc_ValueError, "%zd bytes are not %d x %d RGBA8 texels", view.len, width, height);
    if (PyErr_Occurred()) {
        PyBuffer_Release(&view);
        return nullptr;
    }
    const int rows = (height + 3) / 4;
    PyObject* out = PyBytes_FromStringAndSize(nullptr, Py_ssize_t((width + 3) / 4) * rows * block_size(format));
    if (out) {
        const auto* source = static_cast<const uint8_t*>(view.buf);
        auto* blocks = reinterpret_cast<uint8_t*>(PyBytes_AS_STRING(out));
        Py_BEGIN_ALLOW_THREADS
        const int count = std::clamp(int(std::thread::hardware_concurrency()), 1, rows);
        std::vector<std::thread> threads;
        for (int part = 1; part < count; ++part)
            threads.emplace_back(encode_rows, format, source, width, height, blocks, rows * part / count,
                                 rows * (part + 1) / count);
        encode_rows(format, source, width, height, blocks, 0, rows / count);
        for (auto& thread : threads) thread.join();
        Py_END_ALLOW_THREADS
    }
    PyBuffer_Release(&view);
    return out;
}

PyMethodDef methods[] = {
    {"encode", encode, METH_VARARGS, "encode(dxgi_format, rgba8, width, height) -> bytes: one mip's BC blocks"},
    {nullptr, nullptr, 0, nullptr},
};

PyModuleDef module = {PyModuleDef_HEAD_INIT, "fasttexenc", nullptr, -1, methods};

}  // namespace

PyMODINIT_FUNC PyInit_fasttexenc() {
    bc7e_scalar::bc7e_compress_block_init();
    bc7e_scalar::bc7e_compress_block_params_init_slow(&bc7_data, false);
    bc7e_scalar::bc7e_compress_block_params_init_slow(&bc7_colour, true);
    rgbcx::init();
    return PyModule_Create(&module);
}
