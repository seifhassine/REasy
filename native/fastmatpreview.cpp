/* Draws on WARP by default, which gives the same image on every machine, or on the graphics adapter when asked. */
#define NOMINMAX
#define PY_SSIZE_T_CLEAN
#define Py_LIMITED_API 0x030B0000
#include <Python.h>
#include <d3d12.h>
#include <dxgi1_6.h>
#include <wrl/client.h>
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <exception>
#include <functional>
#include <list>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <set>
#include <string>
#include <tuple>
#include <vector>

using Microsoft::WRL::ComPtr;

namespace {

constexpr UINT MAX_SIZE = 16384;
constexpr UINT MAX_DESCRIPTORS = 1u << 20;
// Descriptors in each shared view heap: the most every Direct3D 12 device supports.
constexpr UINT MAX_VIEWS = 1000000;
constexpr UINT64 STAGING = 1ull << 22;
constexpr size_t FRAMES = 3;
constexpr UINT NONE = UINT_MAX;
// In bytes, per device.
constexpr UINT64 KEPT_TEXELS = 1ull << 30;
// Table kinds, as the Python side numbers them.
enum Kind { SRV = 0, UAV = 1, CBV = 2, SAMPLER = 3 };

struct Failure { std::string message; };
struct Invalid { std::string message; };

void check(HRESULT result, const char *what) {
    if (FAILED(result)) {
        char text[160];
        std::snprintf(text, sizeof text, "%s failed (0x%08lx)", what, static_cast<unsigned long>(result));
        throw Failure{text};
    }
}

// Bytes objects never change, so their contents are read in place without the interpreter lock. Hold and destroy only
// with the lock held.
class Bytes {
    PyObject *owner = nullptr;
    const uint8_t *start = nullptr;
    size_t length = 0;

public:
    Bytes() = default;
    Bytes(Bytes &&other) noexcept { *this = std::move(other); }
    Bytes &operator=(Bytes &&other) noexcept {
        std::swap(owner, other.owner);
        std::swap(start, other.start);
        std::swap(length, other.length);
        return *this;
    }
    ~Bytes() { Py_XDECREF(owner); }

    bool hold(PyObject *object) {
        char *data;
        Py_ssize_t size;
        if (PyBytes_AsStringAndSize(object, &data, &size) < 0) return false;
        Py_INCREF(object);
        Py_XDECREF(owner);
        owner = object;
        start = reinterpret_cast<const uint8_t *>(data);
        length = static_cast<size_t>(size);
        return true;
    }
    const uint8_t *data() const { return start; }
    size_t size() const { return length; }
    bool empty() const { return !length; }
};

struct Data { Bytes bytes; UINT resource = NONE; };

struct Entry {
    UINT index = 0;
    // ``depth`` is depth or array layers; texture subresources are tightly packed; a typed buffer's ``stride`` is its
    // element size.
    UINT format = 0, width = 0, height = 0, depth = 0, mips = 0, stride = 0;
    UINT mip = NONE;
    Data data;
    D3D12_SAMPLER_DESC sampler{};
};

struct Table {
    UINT kind = 0, reg = 0, space = 0, count = 0, dimension = 0, visibility = 0;
    std::vector<Entry> entries;
    UINT slots() const {
        UINT used = count;
        for (const Entry &entry : entries) used = entry.index + 1 > used ? entry.index + 1 : used;
        return used ? used : 1;
    }
};

// For Shader Model 6.6 shaders that index the descriptor heap themselves.
struct HeapEntry { UINT kind = 0, dimension = 0; Entry entry; bool null = false; };

struct Element { std::string semantic; UINT index, format, slot, offset, per_instance, step_rate; };
struct VertexBuffer { Data data; UINT stride; UINT offset; };
struct Target { UINT format; Data data; bool clear = false; };
struct Indirect { UINT buffer = NONE, offset = 0; };

struct Job {
    Bytes vertex_shader, hull_shader, domain_shader, geometry_shader, pixel_shader;
    Data indices;
    std::vector<VertexBuffer> vertex_buffers;
    std::vector<Element> elements;
    // In D3D_PRIMITIVE_TOPOLOGY numbering.
    UINT topology = 0;
    UINT index_format = 0, index_count = 0, instance_count = 0, first_index = 0, first_instance = 0;
    INT base_vertex = 0;
    bool indexed = true;
    Indirect indirect;
    UINT width = 0, height = 0;
    D3D12_VIEWPORT viewport{};
    D3D12_RECT scissor{};
    std::vector<Target> targets;
    bool has_depth = false;
    Target depth{};
    UINT stencil_reference = 0;
    D3D12_RASTERIZER_DESC rasterizer{};
    D3D12_DEPTH_STENCIL_DESC depth_stencil{};
    D3D12_BLEND_DESC blend{};
    std::vector<Table> tables;
    std::vector<HeapEntry> heap;
};

struct ComputeJob {
    Bytes compute_shader;
    UINT groups[3] = {};
    Indirect indirect;
    std::vector<Table> tables;
    std::vector<HeapEntry> heap;
};

bool unsigned_value(PyObject *object, UINT &out) {
    unsigned long value = PyLong_AsUnsignedLong(object);
    if (value == static_cast<unsigned long>(-1) && PyErr_Occurred()) return false;
    out = static_cast<UINT>(value);
    return true;
}

bool job_data(PyObject *object, Data &out) {
    return PyLong_Check(object) ? unsigned_value(object, out.resource) : out.bytes.hold(object);
}

template <typename Parse>
bool items(PyObject *sequence, Py_ssize_t length, const char *what, Parse parse) {
    if (PySequence_Size(sequence) != length) {
        if (!PyErr_Occurred()) PyErr_Format(PyExc_ValueError, "%s has the wrong shape", what);
        return false;
    }
    for (Py_ssize_t i = 0; i < length; ++i) {
        PyObject *item = PySequence_GetItem(sequence, i);
        if (!item) return false;
        bool done = parse(i, item);
        Py_DECREF(item);
        if (!done) return false;
    }
    return true;
}

template <typename Parse>
bool each(PyObject *sequence, Parse parse) {
    Py_ssize_t count = PySequence_Size(sequence);
    if (count < 0) return false;
    for (Py_ssize_t i = 0; i < count; ++i) {
        PyObject *item = PySequence_GetItem(sequence, i);
        if (!item) return false;
        bool done = parse(item);
        Py_DECREF(item);
        if (!done) return false;
    }
    return true;
}

bool unsigned_items(PyObject *sequence, const char *what, std::vector<UINT> &out, Py_ssize_t length) {
    out.assign(static_cast<size_t>(length), 0);
    return items(sequence, length, what, [&](Py_ssize_t i, PyObject *item) { return unsigned_value(item, out[i]); });
}

bool float_items(PyObject *sequence, const char *what, std::vector<float> &out, Py_ssize_t length) {
    out.assign(static_cast<size_t>(length), 0.0f);
    return items(sequence, length, what, [&](Py_ssize_t i, PyObject *item) {
        double value = PyFloat_AsDouble(item);
        if (value == -1.0 && PyErr_Occurred()) return false;
        out[i] = static_cast<float>(value);
        return true;
    });
}

PyObject *field(PyObject *job, const char *key) {
    PyObject *value = PyDict_GetItemString(job, key);
    if (!value) PyErr_Format(PyExc_KeyError, "the draw has no '%s'", key);
    return value;
}

bool parse_entry(PyObject *object, bool buffer, UINT kind, Entry &entry) {
    const Py_ssize_t length = PySequence_Size(object);
    if ((kind == SRV || kind == UAV) && !buffer && (length == 3 || length == 4)) {
        return items(object, length, "descriptor entry", [&](Py_ssize_t i, PyObject *item) {
            UINT *fields[] = {&entry.index, &entry.format, &entry.data.resource, &entry.mip};
            return unsigned_value(item, *fields[i]);
        });
    }
    Py_ssize_t expected = kind == CBV ? 2 : kind == SAMPLER ? 11 : buffer ? 4 : 7;
    return items(object, expected, "descriptor entry", [&](Py_ssize_t i, PyObject *item) {
        if (i == 0) return unsigned_value(item, entry.index);
        if (kind == CBV) return job_data(item, entry.data);
        if (kind == SAMPLER) {
            D3D12_SAMPLER_DESC &s = entry.sampler;
            UINT value = 0;
            std::vector<float> border;
            switch (i) {
            case 1: return unsigned_value(item, value) && (s.Filter = static_cast<D3D12_FILTER>(value), true);
            case 2: return unsigned_value(item, value) && (s.AddressU = static_cast<D3D12_TEXTURE_ADDRESS_MODE>(value), true);
            case 3: return unsigned_value(item, value) && (s.AddressV = static_cast<D3D12_TEXTURE_ADDRESS_MODE>(value), true);
            case 4: return unsigned_value(item, value) && (s.AddressW = static_cast<D3D12_TEXTURE_ADDRESS_MODE>(value), true);
            case 5: s.MipLODBias = static_cast<float>(PyFloat_AsDouble(item)); return !PyErr_Occurred();
            case 6: return unsigned_value(item, s.MaxAnisotropy);
            case 7:
                return unsigned_value(item, value) && (s.ComparisonFunc = static_cast<D3D12_COMPARISON_FUNC>(value), true);
            case 8:
                if (!float_items(item, "sampler border", border, 4)) return false;
                std::copy(border.begin(), border.end(), s.BorderColor);
                return true;
            case 9: s.MinLOD = static_cast<float>(PyFloat_AsDouble(item)); return !PyErr_Occurred();
            default: s.MaxLOD = static_cast<float>(PyFloat_AsDouble(item)); return !PyErr_Occurred();
            }
        }
        if (buffer) {
            if (i == 1) return unsigned_value(item, entry.format);
            if (i == 2) return unsigned_value(item, entry.stride);
            return job_data(item, entry.data);
        }
        UINT *fields[] = {nullptr, &entry.format, &entry.width, &entry.height, &entry.depth, &entry.mips};
        return i < 6 ? unsigned_value(item, *fields[i]) : entry.data.bytes.hold(item);
    });
}

bool buffer_dimension(UINT kind, UINT dimension) {
    return (kind == SRV && dimension == D3D12_SRV_DIMENSION_BUFFER)
        || (kind == UAV && dimension == D3D12_UAV_DIMENSION_BUFFER);
}

bool known_kind(UINT kind) {
    if (kind <= SAMPLER) return true;
    PyErr_SetString(PyExc_ValueError, "unknown descriptor kind");
    return false;
}

bool parse_table(PyObject *object, Table &table) {
    return items(object, 7, "descriptor table", [&](Py_ssize_t i, PyObject *item) {
        UINT *fields[] = {&table.kind, &table.reg, &table.space, &table.count, &table.dimension, &table.visibility};
        if (i < 6) {
            return unsigned_value(item, *fields[i]) && (i != 0 || known_kind(table.kind));
        }
        if (!each(item, [&](PyObject *value) {
                Entry entry;
                if (!parse_entry(value, buffer_dimension(table.kind, table.dimension), table.kind, entry)) return false;
                table.entries.push_back(std::move(entry));
                return true;
            })) return false;
        if (table.slots() > MAX_DESCRIPTORS) {
            PyErr_SetString(PyExc_ValueError, "descriptor table is too large");
            return false;
        }
        return true;
    });
}

bool parse_heap_entry(PyObject *object, HeapEntry &item) {
    return items(object, 3, "heap descriptor", [&](Py_ssize_t i, PyObject *value) {
        if (i == 0) return unsigned_value(value, item.kind) && known_kind(item.kind);
        if (i == 1) return unsigned_value(value, item.dimension);
        item.null = PySequence_Size(value) == 1;
        if (item.null ? !items(value, 1, "heap descriptor entry", [&](Py_ssize_t, PyObject *index) {
                            return unsigned_value(index, item.entry.index);
                        })
                      : !parse_entry(value, buffer_dimension(item.kind, item.dimension), item.kind, item.entry))
            return false;
        if (item.entry.index < MAX_DESCRIPTORS) return true;
        PyErr_SetString(PyExc_ValueError, "heap index is too large");
        return false;
    });
}

bool parse_element(PyObject *object, Element &element) {
    return items(object, 7, "vertex element", [&](Py_ssize_t i, PyObject *item) {
        if (i == 0) {
            PyObject *encoded = PyUnicode_AsASCIIString(item);
            if (!encoded) return false;
            element.semantic = PyBytes_AsString(encoded);
            Py_DECREF(encoded);
            return true;
        }
        UINT *fields[] = {nullptr, &element.index, &element.format, &element.slot, &element.offset,
                          &element.per_instance, &element.step_rate};
        return unsigned_value(item, *fields[i]);
    });
}

bool parse_target(PyObject *object, Target &target) {
    return items(object, 2, "target", [&](Py_ssize_t i, PyObject *item) {
        if (i == 0) return unsigned_value(item, target.format);
        target.clear = item == Py_None;
        return target.clear || job_data(item, target.data);
    });
}

bool parse_rasterizer(PyObject *object, D3D12_RASTERIZER_DESC &r) {
    std::vector<float> values;
    if (!float_items(object, "rasterizer state", values, 7)) return false;
    r.FillMode = static_cast<D3D12_FILL_MODE>(static_cast<UINT>(values[0]));
    r.CullMode = static_cast<D3D12_CULL_MODE>(static_cast<UINT>(values[1]));
    r.FrontCounterClockwise = values[2] != 0.0f;
    r.DepthBias = static_cast<INT>(values[3]);
    r.DepthBiasClamp = values[4];
    r.SlopeScaledDepthBias = values[5];
    r.DepthClipEnable = values[6] != 0.0f;
    return true;
}

bool parse_depth_stencil(PyObject *object, D3D12_DEPTH_STENCIL_DESC &d, UINT &reference) {
    return items(object, 9, "depth/stencil state", [&](Py_ssize_t i, PyObject *item) {
        UINT value = 0;
        if (i == 6 || i == 7) {
            std::vector<UINT> face;
            if (!unsigned_items(item, "stencil face", face, 4)) return false;
            D3D12_DEPTH_STENCILOP_DESC &op = i == 6 ? d.FrontFace : d.BackFace;
            op = {static_cast<D3D12_STENCIL_OP>(face[0]), static_cast<D3D12_STENCIL_OP>(face[1]),
                  static_cast<D3D12_STENCIL_OP>(face[2]), static_cast<D3D12_COMPARISON_FUNC>(face[3])};
            return true;
        }
        if (!unsigned_value(item, value)) return false;
        switch (i) {
        case 0: d.DepthEnable = value != 0; break;
        case 1: d.DepthWriteMask = value ? D3D12_DEPTH_WRITE_MASK_ALL : D3D12_DEPTH_WRITE_MASK_ZERO; break;
        case 2: d.DepthFunc = static_cast<D3D12_COMPARISON_FUNC>(value); break;
        case 3: d.StencilEnable = value != 0; break;
        case 4: d.StencilReadMask = static_cast<UINT8>(value); break;
        case 5: d.StencilWriteMask = static_cast<UINT8>(value); break;
        default: reference = value; break;
        }
        return true;
    });
}

bool parse_blend(PyObject *object, D3D12_BLEND_DESC &b) {
    return items(object, 3, "blend state", [&](Py_ssize_t i, PyObject *item) {
        UINT value = 0;
        if (i < 2) {
            if (!unsigned_value(item, value)) return false;
            (i == 0 ? b.AlphaToCoverageEnable : b.IndependentBlendEnable) = value != 0;
            return true;
        }
        Py_ssize_t count = PySequence_Size(item);
        if (count < 0 || count > D3D12_SIMULTANEOUS_RENDER_TARGET_COUNT) {
            if (!PyErr_Occurred()) PyErr_SetString(PyExc_ValueError, "too many blend targets");
            return false;
        }
        return items(item, count, "blend targets", [&](Py_ssize_t target, PyObject *row) {
            std::vector<UINT> v;
            if (!unsigned_items(row, "blend target", v, 8)) return false;
            D3D12_RENDER_TARGET_BLEND_DESC &t = b.RenderTarget[target];
            t.BlendEnable = v[0] != 0;
            t.SrcBlend = static_cast<D3D12_BLEND>(v[1]);
            t.DestBlend = static_cast<D3D12_BLEND>(v[2]);
            t.BlendOp = static_cast<D3D12_BLEND_OP>(v[3]);
            t.SrcBlendAlpha = static_cast<D3D12_BLEND>(v[4]);
            t.DestBlendAlpha = static_cast<D3D12_BLEND>(v[5]);
            t.BlendOpAlpha = static_cast<D3D12_BLEND_OP>(v[6]);
            t.LogicOp = D3D12_LOGIC_OP_NOOP;
            t.RenderTargetWriteMask = static_cast<UINT8>(v[7]);
            return true;
        });
    });
}

bool parse_resources(PyObject *object, std::vector<Table> &tables, std::vector<HeapEntry> &heap) {
    PyObject *value;
    if (!(value = field(object, "tables")) || !each(value, [&](PyObject *item) {
            Table table;
            if (!parse_table(item, table)) return false;
            tables.push_back(std::move(table));
            return true;
        })) return false;
    return (value = field(object, "heap")) && each(value, [&](PyObject *item) {
        HeapEntry entry;
        if (!parse_heap_entry(item, entry)) return false;
        heap.push_back(std::move(entry));
        return true;
    });
}

bool parse_indirect(PyObject *object, Indirect &indirect) {
    PyObject *value = PyDict_GetItemString(object, "indirect");
    return !value || items(value, 2, "indirect", [&](Py_ssize_t i, PyObject *item) {
        return unsigned_value(item, i == 0 ? indirect.buffer : indirect.offset);
    });
}

bool parse_hardware(PyObject *object, bool &hardware) {
    PyObject *value = field(object, "hardware");
    int truth = value ? PyObject_IsTrue(value) : -1;
    hardware = truth > 0;
    return truth >= 0;
}

bool parse_job(PyObject *object, Job &job) {
    PyObject *value;
    std::vector<UINT> numbers;
    std::vector<float> floats;
    if (!PyDict_Check(object)) {
        PyErr_SetString(PyExc_TypeError, "the draw must be a dict");
        return false;
    }
    if (!(value = field(object, "vertex_shader")) || !job.vertex_shader.hold(value)) return false;
    if ((value = PyDict_GetItemString(object, "hull_shader")) && !job.hull_shader.hold(value)) return false;
    if ((value = PyDict_GetItemString(object, "domain_shader")) && !job.domain_shader.hold(value)) return false;
    if ((value = PyDict_GetItemString(object, "geometry_shader")) && !job.geometry_shader.hold(value)) return false;
    if (!(value = field(object, "pixel_shader")) || !job.pixel_shader.hold(value)) return false;
    if (!(value = field(object, "vertex_buffers")) || !each(value, [&](PyObject *item) {
            VertexBuffer buffer{};
            if (!items(item, PySequence_Size(item) == 3 ? 3 : 2, "vertex buffer", [&](Py_ssize_t i, PyObject *part) {
                    return i == 0 ? job_data(part, buffer.data)
                                  : unsigned_value(part, i == 1 ? buffer.stride : buffer.offset);
                })) return false;
            job.vertex_buffers.push_back(std::move(buffer));
            return true;
        })) return false;
    if (!(value = field(object, "elements")) || !each(value, [&](PyObject *item) {
            Element element{};
            if (!parse_element(item, element)) return false;
            job.elements.push_back(std::move(element));
            return true;
        })) return false;
    if (!(value = field(object, "indices"))) return false;
    job.indexed = value != Py_None;
    if (job.indexed && (!job_data(value, job.indices) || !(value = field(object, "index_format"))
                        || !unsigned_value(value, job.index_format))) return false;
    if (!(value = field(object, "topology")) || !unsigned_value(value, job.topology)) return false;
    if (!parse_indirect(object, job.indirect)) return false;
    if (job.indirect.buffer == NONE
        && (!(value = field(object, "draw")) || !items(value, 5, "draw", [&](Py_ssize_t i, PyObject *item) {
            if (i == 3) {
                long base = PyLong_AsLong(item);
                if (base == -1 && PyErr_Occurred()) return false;
                job.base_vertex = static_cast<INT>(base);
                return true;
            }
            UINT *fields[] = {&job.index_count, &job.instance_count, &job.first_index, nullptr, &job.first_instance};
            return unsigned_value(item, *fields[i]);
        }))) return false;
    if (!(value = field(object, "size")) || !unsigned_items(value, "size", numbers, 2)) return false;
    job.width = numbers[0];
    job.height = numbers[1];
    if (!(value = field(object, "viewport")) || !float_items(value, "viewport", floats, 6)) return false;
    job.viewport = {floats[0], floats[1], floats[2], floats[3], floats[4], floats[5]};
    if (!(value = field(object, "scissor")) || !unsigned_items(value, "scissor", numbers, 4)) return false;
    job.scissor = {static_cast<LONG>(numbers[0]), static_cast<LONG>(numbers[1]), static_cast<LONG>(numbers[2]),
                   static_cast<LONG>(numbers[3])};
    if (!(value = field(object, "targets")) || !each(value, [&](PyObject *item) {
            Target target{};
            if (!parse_target(item, target)) return false;
            job.targets.push_back(std::move(target));
            return true;
        })) return false;
    if (!(value = field(object, "depth"))) return false;
    job.has_depth = value != Py_None;
    if (job.has_depth && !parse_target(value, job.depth)) return false;
    if (!(value = field(object, "rasterizer")) || !parse_rasterizer(value, job.rasterizer)) return false;
    if (!(value = field(object, "depth_stencil"))
        || !parse_depth_stencil(value, job.depth_stencil, job.stencil_reference)) return false;
    if (!(value = field(object, "blend")) || !parse_blend(value, job.blend)) return false;
    return parse_resources(object, job.tables, job.heap);
}

bool parse_compute_job(PyObject *object, ComputeJob &job) {
    PyObject *value;
    std::vector<UINT> numbers;
    if (!PyDict_Check(object)) {
        PyErr_SetString(PyExc_TypeError, "the dispatch must be a dict");
        return false;
    }
    if (!(value = field(object, "compute_shader")) || !job.compute_shader.hold(value)) return false;
    if (!parse_indirect(object, job.indirect)) return false;
    if (job.indirect.buffer == NONE) {
        if (!(value = field(object, "groups")) || !unsigned_items(value, "groups", numbers, 3)) return false;
        std::copy(numbers.begin(), numbers.end(), job.groups);
    }
    return parse_resources(object, job.tables, job.heap);
}

// The length prefix keeps keys of different parts from reading the same.
void add_part(std::string &key, const void *data, size_t size) {
    key.append(reinterpret_cast<const char *>(&size), sizeof size);
    key.append(static_cast<const char *>(data), size);
}

struct KeptTexture {
    D3D12_RESOURCE_DESC desc;
    std::vector<uint8_t> texels;
    ComPtr<ID3D12Resource> resource;

    bool holds(const D3D12_RESOURCE_DESC &other, const uint8_t *data, size_t size) const {
        return desc.Dimension == other.Dimension && desc.Format == other.Format && desc.Width == other.Width
            && desc.Height == other.Height && desc.DepthOrArraySize == other.DepthOrArraySize
            && desc.MipLevels == other.MipLevels && texels.size() == size && !std::memcmp(texels.data(), data, size);
    }
};

struct Commands {
    ComPtr<ID3D12CommandQueue> queue;
    ComPtr<ID3D12CommandAllocator> allocator;
    ComPtr<ID3D12GraphicsCommandList> list;
    ComPtr<ID3D12Fence> fence;
    UINT64 signalled = 0;
};

// Shared because making the device and queues is most of a hardware job's time and pipeline compilation most of a
// software job's. Draws never change these.
struct Kept {
    ComPtr<ID3D12Device> device;
    std::mutex lock;
    std::vector<Commands> idle;
    std::map<std::string, ComPtr<ID3D12RootSignature>> roots;
    std::map<std::string, ComPtr<ID3D12PipelineState>> pipelines;
    std::map<std::string, ComPtr<ID3D12CommandSignature>> signatures;
    std::list<KeptTexture> textures;
    UINT64 texels = 0;

    Commands commands() {
        {
            std::lock_guard<std::mutex> guard(lock);
            if (!idle.empty()) {
                Commands found = std::move(idle.back());
                idle.pop_back();
                return found;
            }
        }
        Commands made;
        D3D12_COMMAND_QUEUE_DESC queue_desc{};
        check(device->CreateCommandQueue(&queue_desc, IID_PPV_ARGS(&made.queue)), "CreateCommandQueue");
        check(device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&made.allocator)),
              "CreateCommandAllocator");
        check(device->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, made.allocator.Get(), nullptr,
                                        IID_PPV_ARGS(&made.list)), "CreateCommandList");
        check(device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&made.fence)), "CreateFence");
        return made;
    }

    ComPtr<ID3D12CommandSignature> signature(D3D12_INDIRECT_ARGUMENT_TYPE type) {
        return one(signatures, std::string(reinterpret_cast<const char *>(&type), sizeof type), [&] {
            D3D12_INDIRECT_ARGUMENT_DESC argument{};
            argument.Type = type;
            D3D12_COMMAND_SIGNATURE_DESC desc{};
            desc.ByteStride = type == D3D12_INDIRECT_ARGUMENT_TYPE_DRAW ? sizeof(D3D12_DRAW_ARGUMENTS)
                : type == D3D12_INDIRECT_ARGUMENT_TYPE_DRAW_INDEXED ? sizeof(D3D12_DRAW_INDEXED_ARGUMENTS)
                : sizeof(D3D12_DISPATCH_ARGUMENTS);
            desc.NumArgumentDescs = 1;
            desc.pArgumentDescs = &argument;
            ComPtr<ID3D12CommandSignature> made;
            check(device->CreateCommandSignature(&desc, nullptr, IID_PPV_ARGS(&made)), "CreateCommandSignature");
            return made;
        });
    }

    // ``done``'s commands must have finished running.
    void rest(Commands &&done) {
        if (FAILED(done.allocator->Reset()) || FAILED(done.list->Reset(done.allocator.Get(), nullptr))) return;
        std::lock_guard<std::mutex> guard(lock);
        idle.push_back(std::move(done));
    }

    ComPtr<ID3D12Resource> texture(const D3D12_RESOURCE_DESC &desc, const uint8_t *data, size_t size) {
        std::lock_guard<std::mutex> guard(lock);
        for (auto item = textures.begin(); item != textures.end(); ++item) {
            if (!item->holds(desc, data, size)) continue;
            textures.splice(textures.end(), textures, item);
            return item->resource;
        }
        return nullptr;
    }

    void keep(std::vector<KeptTexture> &made) {
        std::lock_guard<std::mutex> guard(lock);
        for (KeptTexture &texture : made) {
            if (std::any_of(textures.begin(), textures.end(), [&](const KeptTexture &other) {
                    return other.holds(texture.desc, texture.texels.data(), texture.texels.size());
                })) continue;
            texels += texture.texels.size();
            textures.push_back(std::move(texture));
        }
        made.clear();
        while (texels > KEPT_TEXELS) {
            texels -= textures.front().texels.size();
            textures.pop_front();
        }
    }

    // ``make`` runs outside the lock, so two threads may each make one; the first kept is given to both.
    template <typename Object, typename Make>
    ComPtr<Object> one(std::map<std::string, ComPtr<Object>> &made, const std::string &key, Make make) {
        {
            std::lock_guard<std::mutex> guard(lock);
            auto found = made.find(key);
            if (found != made.end()) return found->second;
        }
        ComPtr<Object> object = make();
        std::lock_guard<std::mutex> guard(lock);
        return made.emplace(key, object).first->second;
    }
};

// Kept for the process, as D3D12 keeps one device per adapter.
std::shared_ptr<Kept> kept_on(bool hardware) {
    static std::mutex lock;
    static std::shared_ptr<Kept> *const all = new std::shared_ptr<Kept>[2];
    std::lock_guard<std::mutex> guard(lock);
    std::shared_ptr<Kept> &kept = all[hardware];
    if (kept && FAILED(kept->device->GetDeviceRemovedReason())) kept.reset();
    if (!kept) {
        std::shared_ptr<Kept> made = std::make_shared<Kept>();
        ComPtr<IDXGIFactory6> factory;
        ComPtr<IDXGIAdapter> adapter;
        check(CreateDXGIFactory2(0, IID_PPV_ARGS(&factory)), "CreateDXGIFactory2");
        if (hardware) {
            check(factory->EnumAdapterByGpuPreference(0, DXGI_GPU_PREFERENCE_HIGH_PERFORMANCE, IID_PPV_ARGS(&adapter)),
                  "EnumAdapterByGpuPreference");
        } else {
            check(factory->EnumWarpAdapter(IID_PPV_ARGS(&adapter)), "EnumWarpAdapter");
        }
        check(D3D12CreateDevice(adapter.Get(), D3D_FEATURE_LEVEL_12_0, IID_PPV_ARGS(&made->device)),
              "D3D12CreateDevice");
        kept = made;
    }
    return kept;
}

D3D12_RESOURCE_DESC buffer_desc(UINT64 size, D3D12_RESOURCE_FLAGS flags = D3D12_RESOURCE_FLAG_NONE) {
    D3D12_RESOURCE_DESC desc{};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    desc.Width = size;
    desc.Height = desc.DepthOrArraySize = desc.MipLevels = desc.SampleDesc.Count = 1;
    desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    desc.Flags = flags;
    return desc;
}

// Committed buffers each get their own heap, which is far slower, and scenes hold thousands of small buffers.
struct Placements {
    static constexpr UINT64 SLOT = D3D12_DEFAULT_RESOURCE_PLACEMENT_ALIGNMENT;
    static constexpr UINT SLOTS = 256;
    struct Heap {
        ComPtr<ID3D12Heap> heap;
        std::vector<UINT> free;
    };
    struct Placement {
        Heap *heap = nullptr;
        UINT slot = 0;
    };
    std::vector<std::unique_ptr<Heap>> heaps;

    ComPtr<ID3D12Resource> place(ID3D12Device *device, const D3D12_RESOURCE_DESC &desc, D3D12_RESOURCE_STATES state,
                                 Placement &placed) {
        auto open = std::find_if(heaps.begin(), heaps.end(), [](const auto &heap) { return !heap->free.empty(); });
        if (open == heaps.end()) {
            auto made = std::make_unique<Heap>();
            D3D12_HEAP_DESC heap{};
            heap.SizeInBytes = SLOT * SLOTS;
            heap.Properties.Type = D3D12_HEAP_TYPE_DEFAULT;
            heap.Alignment = SLOT;
            heap.Flags = D3D12_HEAP_FLAG_ALLOW_ONLY_BUFFERS;
            check(device->CreateHeap(&heap, IID_PPV_ARGS(&made->heap)), "CreateHeap");
            for (UINT slot = SLOTS; slot-- > 0;) made->free.push_back(slot);
            heaps.push_back(std::move(made));
            open = heaps.end() - 1;
        }
        Heap &heap = **open;
        ComPtr<ID3D12Resource> result;
        check(device->CreatePlacedResource(heap.heap.Get(), heap.free.back() * SLOT, &desc, state, nullptr,
                                           IID_PPV_ARGS(&result)), "CreatePlacedResource");
        placed = {&heap, heap.free.back()};
        heap.free.pop_back();
        return result;
    }

    // No command may still use the buffer.
    void free(const Placement &placed) {
        if (!placed.heap) return;
        placed.heap->free.push_back(placed.slot);
        if (placed.heap->free.size() == SLOTS && heaps.size() > 1) {
            heaps.erase(std::find_if(heaps.begin(), heaps.end(),
                                     [&](const auto &heap) { return heap.get() == placed.heap; }));
        }
    }
};

// Buffers start each run in the common state: D3D12 returns them there when commands end and promotes them implicitly
// on first use.
struct Resource {
    ComPtr<ID3D12Resource> resource;
    Placements::Placement placed;
    bool buffer = false;
    // A buffer's size in bytes; its resource is rounded up to a multiple of 256, as constant buffer views need.
    UINT64 size = 0;
    D3D12_RESOURCE_STATES state = D3D12_RESOURCE_STATE_COMMON;
    std::map<std::pair<UINT, bool>, ComPtr<ID3D12DescriptorHeap>> views;
    std::map<UINT, D3D12_RESOURCE_STATES> apart;
};

struct Recorded {
    std::optional<ID3D12RootSignature *> roots[2];
    std::vector<UINT64> tables[2];
    std::optional<ID3D12PipelineState *> pipeline;
    std::optional<std::vector<D3D12_CPU_DESCRIPTOR_HANDLE>> targets;
    std::optional<UINT> reference, topology;
    std::optional<D3D12_VIEWPORT> viewport;
    std::optional<D3D12_RECT> scissor;
    std::optional<std::vector<D3D12_VERTEX_BUFFER_VIEW>> vertices;
    std::optional<D3D12_INDEX_BUFFER_VIEW> indices;
};

template <typename Value>
bool differs(std::optional<Value> &was, const Value &now) {
    if (was && std::memcmp(&*was, &now, sizeof now) == 0) return false;
    was = now;
    return true;
}

template <typename Value>
bool differs(std::optional<std::vector<Value>> &was, const std::vector<Value> &now) {
    if (was && was->size() == now.size() && std::memcmp(was->data(), now.data(), now.size() * sizeof(Value)) == 0)
        return false;
    if (!was) was.emplace();
    was->assign(now.begin(), now.end());
    return true;
}

void set_root(ID3D12GraphicsCommandList *list, Recorded &was, bool compute, ID3D12RootSignature *root,
              const std::vector<D3D12_GPU_DESCRIPTOR_HANDLE> &starts) {
    std::vector<UINT64> &tables = was.tables[compute];
    if (differs(was.roots[compute], root)) {
        if (compute) list->SetComputeRootSignature(root);
        else list->SetGraphicsRootSignature(root);
        tables.clear();
    }
    tables.resize(std::max(tables.size(), starts.size()));
    for (size_t i = 0; i < starts.size(); ++i) {
        if (tables[i] == starts[i].ptr) continue;
        if (compute) list->SetComputeRootDescriptorTable(static_cast<UINT>(i), starts[i]);
        else list->SetGraphicsRootDescriptorTable(static_cast<UINT>(i), starts[i]);
        tables[i] = starts[i].ptr;
    }
}

struct Use { UINT id, part; D3D12_RESOURCE_STATES state; };

struct Part { UINT id, part; };

constexpr D3D12_RESOURCE_STATES WRITING = D3D12_RESOURCE_STATE_RENDER_TARGET | D3D12_RESOURCE_STATE_UNORDERED_ACCESS
    | D3D12_RESOURCE_STATE_DEPTH_WRITE | D3D12_RESOURCE_STATE_STREAM_OUT | D3D12_RESOURCE_STATE_COPY_DEST
    | D3D12_RESOURCE_STATE_RESOLVE_DEST;

struct Written {
    explicit Written(size_t count) : whole(count), any(count) {}

    UINT64 of(const Part &item) const {
        if (item.part == D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES) return any[item.id];
        const auto found = parts.find(key(item));
        return whole[item.id] + (found == parts.end() ? 0 : found->second);
    }

    void write(const Part &item) {
        ++any[item.id];
        if (item.part == D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES) ++whole[item.id];
        else ++parts[key(item)];
    }

private:
    static UINT64 key(const Part &item) { return static_cast<UINT64>(item.id) << 32 | item.part; }
    std::vector<UINT64> whole, any;
    std::map<UINT64, UINT64> parts;
};

struct Step {
    std::vector<Use> uses;
    std::vector<Part> writes;
    struct Copied {
        Part from, to;
        UINT64 from_offset = 0, to_offset = 0, size = 0;
        bool operator<(const Copied &other) const {
            return std::tie(from.id, from.part, to.id, to.part, from_offset, to_offset, size)
                < std::tie(other.from.id, other.from.part, other.to.id, other.to.part, other.from_offset,
                           other.to_offset, other.size);
        }
    };
    std::optional<Copied> copied;
    ID3D12DescriptorHeap *heaps[2] = {};
    std::function<void(ID3D12GraphicsCommandList *, Recorded &)> record;
};

struct Slots { ID3D12DescriptorHeap *heap = nullptr; UINT first = 0; };

// Heap-indexed descriptors sit at their indices; tables are placed from the heaps' ends down. Steps placing different
// descriptors at one index go to other pages.
struct Page {
    ComPtr<ID3D12DescriptorHeap> heaps[2];
    std::map<UINT, std::string> indexed[2];
    UINT ends[2] = {}, tables[2] = {};
    std::map<std::string, UINT> sampler_tables;
};

struct Staging {
    ComPtr<ID3D12Resource> memory;
    uint8_t *mapped = nullptr;
    UINT64 size = 0, used = 0;
};

struct Frame {
    ComPtr<ID3D12CommandAllocator> allocator;
    std::vector<Staging> staging;
    UINT64 done = 0;
};

struct Fill {
    UINT resource;
    ID3D12Resource *memory;
    UINT64 offset, size;
    std::vector<D3D12_PLACED_SUBRESOURCE_FOOTPRINT> layouts;
    D3D12_RESOURCE_STATES after;
};

DXGI_FORMAT shown_format(DXGI_FORMAT format) {
    switch (format) {
    case DXGI_FORMAT_R8G8B8A8_TYPELESS: return DXGI_FORMAT_R8G8B8A8_UNORM;
    case DXGI_FORMAT_B8G8R8A8_TYPELESS: return DXGI_FORMAT_B8G8R8A8_UNORM;
    case DXGI_FORMAT_R10G10B10A2_TYPELESS: return DXGI_FORMAT_R10G10B10A2_UNORM;
    case DXGI_FORMAT_R16G16B16A16_TYPELESS: return DXGI_FORMAT_R16G16B16A16_FLOAT;
    default: return format;
    }
}

struct BufferView { DXGI_FORMAT format; UINT elements, stride; bool raw; };

BufferView buffer_view(const Entry &entry, UINT64 size) {
    UINT element = entry.stride ? entry.stride : entry.format ? 0 : 4;
    if (!element) throw Failure{"a typed buffer needs its element size"};
    if (!size || size % element) throw Failure{"buffer data is not whole elements"};
    UINT elements = static_cast<UINT>(size / element);
    if (entry.format) return {static_cast<DXGI_FORMAT>(entry.format), elements, 0, false};
    if (entry.stride) return {DXGI_FORMAT_UNKNOWN, elements, entry.stride, false};
    return {DXGI_FORMAT_R32_TYPELESS, elements, 0, true};
}

D3D12_RESOURCE_DESC texture_desc(UINT kind, UINT dimension, const Entry &entry, D3D12_RESOURCE_FLAGS flags) {
    D3D12_RESOURCE_DESC desc{};
    if (kind == UAV) {
        dimension = dimension == D3D12_UAV_DIMENSION_TEXTURE1D ? D3D12_SRV_DIMENSION_TEXTURE1D
            : dimension == D3D12_UAV_DIMENSION_TEXTURE1DARRAY ? D3D12_SRV_DIMENSION_TEXTURE1DARRAY
            : dimension == D3D12_UAV_DIMENSION_TEXTURE2DARRAY ? D3D12_SRV_DIMENSION_TEXTURE2DARRAY
            : dimension == D3D12_UAV_DIMENSION_TEXTURE3D ? D3D12_SRV_DIMENSION_TEXTURE3D
            : D3D12_SRV_DIMENSION_TEXTURE2D;
    }
    switch (dimension) {
    case D3D12_SRV_DIMENSION_TEXTURE1D:
    case D3D12_SRV_DIMENSION_TEXTURE1DARRAY:
        desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE1D;
        break;
    case D3D12_SRV_DIMENSION_TEXTURE3D:
        desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE3D;
        break;
    case D3D12_SRV_DIMENSION_TEXTURE2D:
    case D3D12_SRV_DIMENSION_TEXTURE2DARRAY:
    case D3D12_SRV_DIMENSION_TEXTURECUBE:
    case D3D12_SRV_DIMENSION_TEXTURECUBEARRAY:
        desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
        break;
    default:
        throw Failure{"texture entry in a table of another dimension"};
    }
    if (!entry.width || !entry.height || !entry.depth || !entry.mips || entry.width > MAX_SIZE
        || entry.height > MAX_SIZE || entry.depth > 2048 || entry.mips > 15) throw Failure{"texture size is invalid"};
    desc.Width = entry.width;
    desc.Height = entry.height;
    desc.DepthOrArraySize = static_cast<UINT16>(entry.depth);
    desc.MipLevels = static_cast<UINT16>(entry.mips);
    desc.Format = static_cast<DXGI_FORMAT>(entry.format);
    desc.SampleDesc.Count = 1;
    desc.Flags = flags;
    return desc;
}

struct Binding {
    ComPtr<ID3D12RootSignature> root;
    std::vector<D3D12_GPU_DESCRIPTOR_HANDLE> starts;
};

struct Scene {
    std::shared_ptr<Kept> kept;
    ComPtr<ID3D12Device> device;
    Commands commands;
    bool recording = true;
    Frame frames[FRAMES];
    size_t frame = 0;
    bool turned = false;
    Placements placements;
    struct Dropped {
        UINT64 after;
        ComPtr<ID3D12Resource> resource;
        Placements::Placement placed;
    };
    std::vector<Dropped> dropped;
    // Never held together with the interpreter lock.
    std::mutex lock;
    std::vector<Resource> resources;
    std::vector<Step> steps;
    std::vector<Page> pages;
    std::map<std::string, size_t> layouts;
    std::vector<Fill> fills;
    std::vector<KeptTexture> made;
    std::vector<UINT> made_resources;
    std::vector<UINT> promoted;
    // Allows tearing so presenting never waits for the screen.
    struct Window {
        ComPtr<IDXGISwapChain3> chain;
        UINT flags = 0;
        UINT width = 0, height = 0;
        DXGI_FORMAT format = DXGI_FORMAT_UNKNOWN;
    };
    std::map<HWND, Window> windows;

    explicit Scene(bool hardware) : kept(kept_on(hardware)), device(kept->device), commands(kept->commands()) {
        frames[0].allocator = commands.allocator;
    }

    ~Scene() {
        if (recording) commands.list->Close();
        try {
            wait(commands.signalled);
        } catch (const Failure &) {
            return;
        }
        windows.clear();
        commands.allocator = frames[0].allocator;
        kept->rest(std::move(commands));
    }

    void wait(UINT64 value) {
        if (commands.fence->GetCompletedValue() >= value) return;
        HANDLE event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
        if (!event) throw Failure{"CreateEvent failed"};
        HRESULT result = commands.fence->SetEventOnCompletion(value, event);
        if (SUCCEEDED(result)) WaitForSingleObject(event, INFINITE);
        CloseHandle(event);
        check(result, "SetEventOnCompletion");
        check(device->GetDeviceRemovedReason(), "Drawing");
    }

    Frame &current() {
        if (!turned) return frames[frame];
        turned = false;
        frame = (frame + 1) % FRAMES;
        Frame &next = frames[frame];
        wait(next.done);
        const UINT64 completed = commands.fence->GetCompletedValue();
        dropped.erase(std::remove_if(dropped.begin(), dropped.end(), [&](Dropped &item) {
                          if (item.after > completed) return false;
                          item.resource.Reset();
                          placements.free(item.placed);
                          return true;
                      }),
                      dropped.end());
        if (next.allocator) {
            check(next.allocator->Reset(), "Reset");
        } else {
            check(device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&next.allocator)),
                  "CreateCommandAllocator");
        }
        if (!next.staging.empty() && next.staging[0].size == STAGING) {
            next.staging.resize(1);
            next.staging[0].used = 0;
        } else {
            next.staging.clear();
        }
        return next;
    }

    ID3D12GraphicsCommandList *record(Frame &now) {
        if (!recording) {
            check(commands.list->Reset(now.allocator.Get(), nullptr), "Reset");
            recording = true;
        }
        return commands.list.Get();
    }

    void submit() {
        recording = false;
        check(commands.list->Close(), "Close");
        ID3D12CommandList *lists[] = {commands.list.Get()};
        commands.queue->ExecuteCommandLists(1, lists);
        check(commands.queue->Signal(commands.fence.Get(), ++commands.signalled), "Signal");
        frames[frame].done = commands.signalled;
        check(device->GetDeviceRemovedReason(), "Drawing");
        for (UINT id : promoted) resources[id].state = D3D12_RESOURCE_STATE_COMMON;
        promoted.clear();
    }

    void abandon() {
        if (!recording) return;
        recording = false;
        commands.list->Close();
        promoted.clear();
    }

    Resource &found(UINT id, bool buffer) {
        if (id >= resources.size()) throw Failure{"the scene has no such resource"};
        if (!resources[id].resource) throw Failure{"the resource was released"};
        if (resources[id].buffer != buffer) throw Failure{buffer ? "a texture is named as a buffer"
                                                                 : "a buffer is named as a texture"};
        return resources[id];
    }

    void release(const std::vector<UINT> &ids) {
        for (UINT id : ids) {
            if (id >= resources.size()) throw Failure{"the scene has no such resource"};
        }
        const std::set<UINT> gone(ids.begin(), ids.end());
        fills.erase(std::remove_if(fills.begin(), fills.end(),
                                   [&](const Fill &fill) { return gone.count(fill.resource) > 0; }),
                    fills.end());
        for (size_t i = made.size(); i-- > 0;) {
            if (gone.count(made_resources[i])) {
                made.erase(made.begin() + i);
                made_resources.erase(made_resources.begin() + i);
            }
        }
        for (UINT id : gone) {
            const Resource &resource = resources[id];
            if (resource.resource) dropped.push_back({commands.signalled, resource.resource, resource.placed});
            resources[id] = Resource{};
        }
    }

    UINT64 size(const Data &data) const {
        if (data.resource == NONE) return data.bytes.size();
        return data.resource < resources.size() ? resources[data.resource].size : 0;
    }

    ComPtr<ID3D12Resource> committed(D3D12_HEAP_TYPE heap, const D3D12_RESOURCE_DESC &desc,
                                     D3D12_RESOURCE_STATES state) {
        D3D12_HEAP_PROPERTIES properties{};
        properties.Type = heap;
        ComPtr<ID3D12Resource> result;
        check(device->CreateCommittedResource(&properties, D3D12_HEAP_FLAG_NONE, &desc, state, nullptr,
                                              IID_PPV_ARGS(&result)), "CreateCommittedResource");
        return result;
    }

    ComPtr<ID3D12Resource> memory(UINT64 size, D3D12_HEAP_TYPE heap, D3D12_RESOURCE_STATES state,
                                  D3D12_RESOURCE_FLAGS flags = D3D12_RESOURCE_FLAG_NONE) {
        return committed(heap, buffer_desc(size, flags), state);
    }

    ComPtr<ID3D12DescriptorHeap> descriptor_heap(D3D12_DESCRIPTOR_HEAP_TYPE type, UINT count, bool visible) {
        D3D12_DESCRIPTOR_HEAP_DESC desc{};
        desc.Type = type;
        desc.NumDescriptors = count;
        desc.Flags = visible ? D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE : D3D12_DESCRIPTOR_HEAP_FLAG_NONE;
        ComPtr<ID3D12DescriptorHeap> result;
        check(device->CreateDescriptorHeap(&desc, IID_PPV_ARGS(&result)), "CreateDescriptorHeap");
        return result;
    }

    // Formats with stencil have two planes, depth and stencil.
    UINT planes(DXGI_FORMAT format) {
        D3D12_FEATURE_DATA_FORMAT_INFO info{format, 0};
        check(device->CheckFeatureSupport(D3D12_FEATURE_FORMAT_INFO, &info, sizeof info), "CheckFeatureSupport");
        return info.PlaneCount;
    }

    UINT subresources(const D3D12_RESOURCE_DESC &desc) {
        UINT layers = desc.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE3D ? 1u : desc.DepthOrArraySize;
        return desc.MipLevels * layers * planes(desc.Format);
    }

    std::pair<Staging *, UINT64> stage(UINT64 size) {
        std::vector<Staging> &staging = current().staging;
        UINT64 start = staging.empty() ? 0 : (staging.back().used + D3D12_TEXTURE_DATA_PLACEMENT_ALIGNMENT - 1)
            & ~static_cast<UINT64>(D3D12_TEXTURE_DATA_PLACEMENT_ALIGNMENT - 1);
        if (staging.empty() || start + size > staging.back().size) {
            Staging made;
            made.size = std::max(size, STAGING);
            made.memory = memory(made.size, D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_STATE_GENERIC_READ);
            D3D12_RANGE none{0, 0};
            check(made.memory->Map(0, &none, reinterpret_cast<void **>(&made.mapped)), "Map");
            staging.push_back(std::move(made));
            start = 0;
        }
        staging.back().used = start + size;
        return {&staging.back(), start};
    }

    void fill_buffer(UINT id, const uint8_t *data, size_t size) {
        if (size != found(id, true).size) throw Failure{"the bytes are not the buffer's size"};
        UINT64 width = resources[id].resource->GetDesc().Width;
        auto [staged, offset] = stage(width);
        std::memcpy(staged->mapped + offset, data, size);
        std::memset(staged->mapped + offset + size, 0, static_cast<size_t>(width - size));
        fills.push_back({id, staged->memory.Get(), offset, width, {}, D3D12_RESOURCE_STATE_COMMON});
    }

    // ``data`` holds tightly packed subresources.
    void fill_texture(UINT id, const uint8_t *data, size_t size, D3D12_RESOURCE_STATES after) {
        D3D12_RESOURCE_DESC desc = found(id, false).resource->GetDesc();
        UINT count = subresources(desc);
        Fill fill{id, nullptr, 0, 0, std::vector<D3D12_PLACED_SUBRESOURCE_FOOTPRINT>(count), after};
        std::vector<UINT> rows(count);
        std::vector<UINT64> row_sizes(count);
        UINT64 total = 0, packed = 0;
        device->GetCopyableFootprints(&desc, 0, count, 0, fill.layouts.data(), rows.data(), row_sizes.data(), &total);
        for (UINT i = 0; i < count; ++i) packed += row_sizes[i] * rows[i] * fill.layouts[i].Footprint.Depth;
        if (packed != size) throw Failure{"texel data does not match its format, size and mips"};
        auto [staged, offset] = stage(total);
        fill.memory = staged->memory.Get();
        for (UINT i = 0; i < count; ++i) {
            D3D12_PLACED_SUBRESOURCE_FOOTPRINT &layout = fill.layouts[i];
            layout.Offset += offset;
            for (UINT slice = 0; slice < layout.Footprint.Depth; ++slice) {
                for (UINT row = 0; row < rows[i]; ++row) {
                    std::memcpy(staged->mapped + layout.Offset
                                + (static_cast<UINT64>(slice) * rows[i] + row) * layout.Footprint.RowPitch,
                                data, static_cast<size_t>(row_sizes[i]));
                    data += row_sizes[i];
                }
            }
        }
        fills.push_back(std::move(fill));
    }

    UINT buffer(const uint8_t *data, size_t size, D3D12_RESOURCE_FLAGS flags) {
        if (!size) throw Failure{"a buffer holds nothing"};
        Resource made;
        made.buffer = true;
        made.size = size;
        const UINT64 width = (size + 255) & ~static_cast<UINT64>(255);
        made.resource = width <= Placements::SLOT
            ? placements.place(device.Get(), buffer_desc(width, flags), D3D12_RESOURCE_STATE_COMMON, made.placed)
            : memory(width, D3D12_HEAP_TYPE_DEFAULT, D3D12_RESOURCE_STATE_COMMON, flags);
        resources.push_back(std::move(made));
        UINT id = static_cast<UINT>(resources.size() - 1);
        fill_buffer(id, data, size);
        return id;
    }

    // ``data`` holds tightly packed subresources, or is null.
    UINT texture(const D3D12_RESOURCE_DESC &desc, const uint8_t *data, size_t size,
                 D3D12_RESOURCE_STATES after = D3D12_RESOURCE_STATE_COMMON) {
        Resource made;
        made.state = D3D12_RESOURCE_STATE_COPY_DEST;
        made.resource = committed(D3D12_HEAP_TYPE_DEFAULT, desc, made.state);
        resources.push_back(std::move(made));
        UINT id = static_cast<UINT>(resources.size() - 1);
        if (data) fill_texture(id, data, size, after);
        return id;
    }

    UINT read_texture(const D3D12_RESOURCE_DESC &desc, const Bytes &data) {
        for (size_t i = 0; i < made.size(); ++i) {
            if (made[i].holds(desc, data.data(), data.size())) return made_resources[i];
        }
        ComPtr<ID3D12Resource> shared = kept->texture(desc, data.data(), data.size());
        if (shared) {
            Resource known;
            known.resource = shared;
            known.state = D3D12_RESOURCE_STATE_ALL_SHADER_RESOURCE;
            resources.push_back(std::move(known));
            return static_cast<UINT>(resources.size() - 1);
        }
        UINT id = texture(desc, data.data(), data.size(), D3D12_RESOURCE_STATE_ALL_SHADER_RESOURCE);
        made.push_back({desc, std::vector<uint8_t>(data.data(), data.data() + data.size()), resources[id].resource});
        made_resources.push_back(id);
        return id;
    }

    UINT held(const Data &data, D3D12_RESOURCE_FLAGS flags = D3D12_RESOURCE_FLAG_NONE) {
        if (data.resource != NONE) return data.resource;
        return buffer(data.bytes.data(), data.bytes.size(), flags);
    }

    ID3D12Resource *use(Step &step, UINT id, bool buffer, D3D12_RESOURCE_STATES state,
                        UINT part = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES) {
        ID3D12Resource *resource = found(id, buffer).resource.Get();
        for (Use &other : step.uses) {
            const bool whole = other.part == D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES
                || part == D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
            if (other.id != id || (other.part != part && !whole)) continue;
            if (other.part != part) throw Failure{"a step uses a resource whole and a subresource of it alone"};
            if (other.state == state) return resource;
            if ((other.state | state) & ~(D3D12_RESOURCE_STATE_GENERIC_READ | D3D12_RESOURCE_STATE_DEPTH_READ))
                throw Failure{"a step writes a resource it uses in another way"};
            other.state |= state;
            return resource;
        }
        step.uses.push_back({id, part, state});
        return resource;
    }

    UINT part(UINT id, UINT mip) {
        if (mip == NONE) return D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
        const D3D12_RESOURCE_DESC desc = found(id, false).resource->GetDesc();
        if (mip >= desc.MipLevels) throw Failure{"the texture has no such mip"};
        if ((desc.Dimension != D3D12_RESOURCE_DIMENSION_TEXTURE3D && desc.DepthOrArraySize != 1)
            || planes(desc.Format) > 1)
            throw Failure{"only textures of one layer and one plane are viewed one mip at a time"};
        return mip;
    }

    void srv(Step &step, UINT dimension, const Entry *entry, D3D12_CPU_DESCRIPTOR_HANDLE slot) {
        D3D12_SHADER_RESOURCE_VIEW_DESC view{};
        view.ViewDimension = static_cast<D3D12_SRV_DIMENSION>(dimension);
        view.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
        ID3D12Resource *resource = nullptr;
        UINT mips = 1, first = 0, layers = 1, plane = 0;
        if (entry && dimension == D3D12_SRV_DIMENSION_BUFFER) {
            UINT id = held(entry->data);
            resource = use(step, id, true, D3D12_RESOURCE_STATE_ALL_SHADER_RESOURCE);
            BufferView buffer = buffer_view(*entry, resources[id].size);
            view.Format = buffer.format;
            view.Buffer.NumElements = buffer.elements;
            view.Buffer.StructureByteStride = buffer.stride;
            view.Buffer.Flags = buffer.raw ? D3D12_BUFFER_SRV_FLAG_RAW : D3D12_BUFFER_SRV_FLAG_NONE;
        } else if (entry) {
            UINT id = entry->data.resource != NONE ? entry->data.resource : read_texture(
                texture_desc(SRV, dimension, *entry, D3D12_RESOURCE_FLAG_NONE), entry->data.bytes);
            resource = use(step, id, false, D3D12_RESOURCE_STATE_ALL_SHADER_RESOURCE, part(id, entry->mip));
            D3D12_RESOURCE_DESC desc = resource->GetDesc();
            mips = entry->mip == NONE ? desc.MipLevels : 1;
            first = entry->mip == NONE ? 0 : entry->mip;
            layers = desc.DepthOrArraySize;
            view.Format = static_cast<DXGI_FORMAT>(entry->format);
            // A depth/stencil texture's stencil is its second plane.
            plane = view.Format == DXGI_FORMAT_X32_TYPELESS_G8X24_UINT
                || view.Format == DXGI_FORMAT_X24_TYPELESS_G8_UINT;
        } else {
            view.Format = dimension == D3D12_SRV_DIMENSION_BUFFER ? DXGI_FORMAT_R32_UINT : DXGI_FORMAT_R8G8B8A8_UNORM;
        }
        switch (dimension) {
        case D3D12_SRV_DIMENSION_TEXTURE1D: view.Texture1D.MipLevels = mips; break;
        case D3D12_SRV_DIMENSION_TEXTURE1DARRAY:
            view.Texture1DArray.MipLevels = mips;
            view.Texture1DArray.ArraySize = layers;
            break;
        case D3D12_SRV_DIMENSION_TEXTURE2D:
            view.Texture2D.MostDetailedMip = first;
            view.Texture2D.MipLevels = mips;
            view.Texture2D.PlaneSlice = plane;
            break;
        case D3D12_SRV_DIMENSION_TEXTURE2DARRAY:
            view.Texture2DArray.MipLevels = mips;
            view.Texture2DArray.ArraySize = layers;
            view.Texture2DArray.PlaneSlice = plane;
            break;
        case D3D12_SRV_DIMENSION_TEXTURE3D:
            view.Texture3D.MostDetailedMip = first;
            view.Texture3D.MipLevels = mips;
            break;
        case D3D12_SRV_DIMENSION_TEXTURECUBE: view.TextureCube.MipLevels = mips; break;
        case D3D12_SRV_DIMENSION_TEXTURECUBEARRAY:
            view.TextureCubeArray.MipLevels = mips;
            view.TextureCubeArray.NumCubes = layers / 6;
            break;
        case D3D12_SRV_DIMENSION_BUFFER: break;
        default: throw Failure{"unsupported shader resource dimension"};
        }
        device->CreateShaderResourceView(resource, &view, slot);
    }

    void uav(Step &step, UINT dimension, const Entry *entry, D3D12_CPU_DESCRIPTOR_HANDLE slot) {
        D3D12_UNORDERED_ACCESS_VIEW_DESC view{};
        view.ViewDimension = static_cast<D3D12_UAV_DIMENSION>(dimension);
        ID3D12Resource *resource = nullptr;
        bool buffer = dimension == D3D12_UAV_DIMENSION_BUFFER;
        UINT id = NONE;
        if (entry) {
            id = buffer ? held(entry->data, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS)
                : entry->data.resource != NONE ? entry->data.resource
                : texture(texture_desc(UAV, dimension, *entry, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS),
                          entry->data.bytes.data(), entry->data.bytes.size());
            resource = use(step, id, buffer, D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                           buffer ? D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES : part(id, entry->mip));
            // Direct3D removes the device rather than refuse such a view.
            if (!(resource->GetDesc().Flags & D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS))
                throw Failure{"a step writes a resource made without unordered access"};
        }
        if (entry && buffer) {
            BufferView typed = buffer_view(*entry, resources[id].size);
            view.Format = typed.format;
            view.Buffer.NumElements = typed.elements;
            view.Buffer.StructureByteStride = typed.stride;
            view.Buffer.Flags = typed.raw ? D3D12_BUFFER_UAV_FLAG_RAW : D3D12_BUFFER_UAV_FLAG_NONE;
        } else if (entry) {
            UINT layers = resource->GetDesc().DepthOrArraySize;
            const UINT mip = entry->mip == NONE ? 0 : entry->mip;
            view.Format = static_cast<DXGI_FORMAT>(entry->format);
            if (dimension == D3D12_UAV_DIMENSION_TEXTURE2D) view.Texture2D.MipSlice = mip;
            if (dimension == D3D12_UAV_DIMENSION_TEXTURE2DARRAY) view.Texture2DArray.ArraySize = layers;
            if (dimension == D3D12_UAV_DIMENSION_TEXTURE1DARRAY) view.Texture1DArray.ArraySize = layers;
            if (dimension == D3D12_UAV_DIMENSION_TEXTURE3D) {
                view.Texture3D.MipSlice = mip;
                view.Texture3D.WSize = std::max(layers >> mip, 1u);
            }
        } else {
            view.Format = buffer ? DXGI_FORMAT_R32_UINT : DXGI_FORMAT_R8G8B8A8_UNORM;
        }
        device->CreateUnorderedAccessView(resource, nullptr, &view, slot);
    }

    void cbv(Step &step, const Entry *entry, D3D12_CPU_DESCRIPTOR_HANDLE slot) {
        D3D12_CONSTANT_BUFFER_VIEW_DESC view{};
        if (entry) {
            UINT id = held(entry->data);
            ID3D12Resource *resource = use(step, id, true, D3D12_RESOURCE_STATE_VERTEX_AND_CONSTANT_BUFFER);
            UINT64 size = resource->GetDesc().Width;
            if (size > 65536) throw Failure{"constant buffer size is invalid"};
            view.BufferLocation = resource->GetGPUVirtualAddress();
            view.SizeInBytes = static_cast<UINT>(size);
        }
        device->CreateConstantBufferView(&view, slot);
    }

    void keep(UINT kind, UINT dimension, Entry &entry) {
        if (kind == SAMPLER || entry.data.resource != NONE) return;
        if (kind == CBV || (kind == SRV && dimension == D3D12_SRV_DIMENSION_BUFFER)) {
            entry.data.resource = held(entry.data);
        } else if (kind == SRV) {
            entry.data.resource = read_texture(texture_desc(SRV, dimension, entry, D3D12_RESOURCE_FLAG_NONE),
                                               entry.data.bytes);
        } else {
            entry.data.resource = dimension == D3D12_UAV_DIMENSION_BUFFER
                ? held(entry.data, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS)
                : texture(texture_desc(UAV, dimension, entry, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS),
                          entry.data.bytes.data(), entry.data.bytes.size());
        }
    }

    void describe(Step &step, UINT kind, UINT dimension, const Entry *entry, D3D12_CPU_DESCRIPTOR_HANDLE slot) {
        if (kind == SRV) srv(step, dimension, entry, slot);
        else if (kind == UAV) uav(step, dimension, entry, slot);
        else if (kind == CBV) cbv(step, entry, slot);
        else if (entry) device->CreateSampler(&entry->sampler, slot);
    }

    // Unset heap and sampler slots are left unwritten: shaders must not read them.
    Binding bind(Step &step, const std::vector<Table> &tables, std::vector<HeapEntry> &heap,
                 D3D12_ROOT_SIGNATURE_FLAGS flags) {
        static const D3D12_DESCRIPTOR_HEAP_TYPE types[] = {D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV,
                                                           D3D12_DESCRIPTOR_HEAP_TYPE_SAMPLER};
        const UINT increments[] = {device->GetDescriptorHandleIncrementSize(types[0]),
                                   device->GetDescriptorHandleIncrementSize(types[1])};
        auto slot = [&](const Slots &slots, bool sampler, UINT index) {
            D3D12_CPU_DESCRIPTOR_HANDLE handle = slots.heap->GetCPUDescriptorHandleForHeapStart();
            handle.ptr += static_cast<SIZE_T>(slots.first + index) * increments[sampler];
            return handle;
        };
        UINT indexed[2] = {}, counts[2] = {};
        for (const HeapEntry &item : heap) {
            UINT &end = indexed[item.kind == SAMPLER];
            end = std::max(end, item.entry.index + 1);
        }
        std::string held_samplers;
        for (const Table &table : tables) {
            counts[table.kind == SAMPLER] += table.slots();
            if (table.kind != SAMPLER) continue;
            const UINT shape[] = {table.slots(), static_cast<UINT>(table.entries.size())};
            add_part(held_samplers, shape, sizeof shape);
            for (const Entry &entry : table.entries) {
                add_part(held_samplers, &entry.index, sizeof entry.index);
                add_part(held_samplers, &entry.sampler, sizeof entry.sampler);
            }
        }
        if (indexed[1] + counts[1] > D3D12_MAX_SHADER_VISIBLE_SAMPLER_HEAP_SIZE) throw Failure{"too many samplers"};
        if (indexed[0] + counts[0] > MAX_VIEWS) throw Failure{"too many descriptors"};
        std::map<UINT, std::string> placed[2];
        for (HeapEntry &item : heap) {
            if (!item.null) keep(item.kind, item.dimension, item.entry);
            const UINT shape[] = {item.kind, item.dimension, item.null, item.entry.format, item.entry.stride,
                                  item.entry.data.resource};
            std::string described;
            add_part(described, shape, sizeof shape);
            add_part(described, &item.entry.sampler, sizeof item.entry.sampler);
            if (!placed[item.kind == SAMPLER].emplace(item.entry.index, std::move(described)).second)
                throw Failure{"two descriptors share one heap index"};
        }
        auto fits = [&](const Page &page) {
            for (int i = 0; i < 2; ++i) {
                for (const auto &[index, described] : placed[i]) {
                    auto there = page.indexed[i].find(index);
                    if (there != page.indexed[i].end() && there->second != described) return false;
                }
            }
            const UINT samplers = page.sampler_tables.count(held_samplers) ? 0 : counts[1];
            return std::max(page.ends[0], indexed[0]) + counts[0] <= page.tables[0]
                && std::max(page.ends[1], indexed[1]) + samplers <= page.tables[1];
        };
        // Descriptions all have one length, so the joined layout is unambiguous.
        std::string layout;
        for (int i = 0; i < 2; ++i) {
            for (const auto &[index, described] : placed[i]) {
                const UINT where[] = {static_cast<UINT>(i), index};
                add_part(layout, where, sizeof where);
                layout += described;
            }
        }
        auto last = layouts.find(layout);
        auto page = last != layouts.end() && fits(pages[last->second])
            ? pages.begin() + last->second : std::find_if(pages.begin(), pages.end(), fits);
        if (page == pages.end()) {
            // Pages get the largest heaps, so a step moves to a new page only when its heap-indexed descriptors clash
            // with a page's.
            Page made;
            made.tables[0] = MAX_VIEWS;
            made.tables[1] = D3D12_MAX_SHADER_VISIBLE_SAMPLER_HEAP_SIZE;
            for (int i = 0; i < 2; ++i) made.heaps[i] = descriptor_heap(types[i], made.tables[i], true);
            pages.push_back(std::move(made));
            page = pages.end() - 1;
        }
        for (const HeapEntry &item : heap) {
            // Described again even when another step placed it, so this step records its use of the resource.
            bool sampler = item.kind == SAMPLER;
            describe(step, item.kind, item.dimension, item.null ? nullptr : &item.entry,
                     slot({page->heaps[sampler].Get(), 0}, sampler, item.entry.index));
        }
        layouts[layout] = static_cast<size_t>(page - pages.begin());
        for (int i = 0; i < 2; ++i) {
            page->indexed[i].merge(placed[i]);
            page->ends[i] = std::max(page->ends[i], indexed[i]);
        }
        auto shared = page->sampler_tables.find(held_samplers);
        const bool new_samplers = shared == page->sampler_tables.end();
        const Slots blocks[2] = {{page->heaps[0].Get(), page->tables[0] -= counts[0]},
                                 {page->heaps[1].Get(), new_samplers ? page->sampler_tables[held_samplers] =
                                                                           page->tables[1] -= counts[1]
                                                                     : shared->second}};
        step.heaps[0] = blocks[0].heap;
        step.heaps[1] = blocks[1].heap;

        Binding binding;
        std::vector<D3D12_DESCRIPTOR_RANGE1> ranges(tables.size());
        std::vector<D3D12_ROOT_PARAMETER1> parameters(tables.size());
        binding.starts.resize(tables.size());
        UINT next[2] = {};
        for (size_t i = 0; i < tables.size(); ++i) {
            const Table &table = tables[i];
            bool sampler = table.kind == SAMPLER;
            static const D3D12_DESCRIPTOR_RANGE_TYPE range_types[] = {
                D3D12_DESCRIPTOR_RANGE_TYPE_SRV, D3D12_DESCRIPTOR_RANGE_TYPE_UAV, D3D12_DESCRIPTOR_RANGE_TYPE_CBV,
                D3D12_DESCRIPTOR_RANGE_TYPE_SAMPLER};
            ranges[i].RangeType = range_types[table.kind];
            ranges[i].NumDescriptors = table.count ? table.count : UINT_MAX;
            ranges[i].BaseShaderRegister = table.reg;
            ranges[i].RegisterSpace = table.space;
            ranges[i].Flags = sampler ? D3D12_DESCRIPTOR_RANGE_FLAG_DESCRIPTORS_VOLATILE
                : D3D12_DESCRIPTOR_RANGE_FLAG_DESCRIPTORS_VOLATILE | D3D12_DESCRIPTOR_RANGE_FLAG_DATA_VOLATILE;
            parameters[i].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
            parameters[i].DescriptorTable = {1, &ranges[i]};
            parameters[i].ShaderVisibility = static_cast<D3D12_SHADER_VISIBILITY>(table.visibility);

            const Slots &block = blocks[sampler];
            binding.starts[i] = {block.heap->GetGPUDescriptorHandleForHeapStart().ptr
                                 + static_cast<UINT64>(block.first + next[sampler]) * increments[sampler]};
            UINT slots = table.slots();
            std::vector<const Entry *> placed(slots, nullptr);
            for (const Entry &entry : table.entries) {
                if (placed[entry.index]) throw Failure{"two descriptors share one slot"};
                placed[entry.index] = &entry;
            }
            for (UINT index = 0; index < slots && (!sampler || new_samplers); ++index) {
                describe(step, table.kind, table.dimension, placed[index], slot(block, sampler, next[sampler] + index));
            }
            next[sampler] += slots;
        }

        D3D12_VERSIONED_ROOT_SIGNATURE_DESC root_desc{};
        root_desc.Version = D3D_ROOT_SIGNATURE_VERSION_1_1;
        root_desc.Desc_1_1.NumParameters = static_cast<UINT>(parameters.size());
        root_desc.Desc_1_1.pParameters = parameters.data();
        // A shader that indexes the heaps on a path it never takes still needs the flags to make a pipeline.
        root_desc.Desc_1_1.Flags = flags | D3D12_ROOT_SIGNATURE_FLAG_CBV_SRV_UAV_HEAP_DIRECTLY_INDEXED
            | D3D12_ROOT_SIGNATURE_FLAG_SAMPLER_HEAP_DIRECTLY_INDEXED;
        ComPtr<ID3DBlob> blob, errors;
        HRESULT serialized = D3D12SerializeVersionedRootSignature(&root_desc, &blob, &errors);
        if (FAILED(serialized) && errors) {
            throw Failure{std::string("root signature: ") + static_cast<const char *>(errors->GetBufferPointer())};
        }
        check(serialized, "D3D12SerializeVersionedRootSignature");
        std::string key(static_cast<const char *>(blob->GetBufferPointer()), blob->GetBufferSize());
        binding.root = kept->one(kept->roots, key, [&] {
            ComPtr<ID3D12RootSignature> root;
            check(device->CreateRootSignature(0, blob->GetBufferPointer(), blob->GetBufferSize(),
                                              IID_PPV_ARGS(&root)), "CreateRootSignature");
            return root;
        });
        return binding;
    }

    // A ``read_only`` depth target is only tested, so the step can also read it as a shader resource.
    D3D12_CPU_DESCRIPTOR_HANDLE target_view(Step &step, UINT format, UINT id, bool depth, bool read_only = false) {
        ID3D12Resource *resource = use(step, id, false, !depth ? D3D12_RESOURCE_STATE_RENDER_TARGET
                                                       : read_only ? D3D12_RESOURCE_STATE_DEPTH_READ
                                                                   : D3D12_RESOURCE_STATE_DEPTH_WRITE);
        ComPtr<ID3D12DescriptorHeap> &view = resources[id].views[{format, read_only}];
        if (!view && depth) {
            view = descriptor_heap(D3D12_DESCRIPTOR_HEAP_TYPE_DSV, 1, false);
            D3D12_DEPTH_STENCIL_VIEW_DESC desc{};
            desc.Format = static_cast<DXGI_FORMAT>(format);
            desc.ViewDimension = D3D12_DSV_DIMENSION_TEXTURE2D;
            if (read_only) {
                // READ_ONLY_STENCIL is valid only for a format with stencil.
                desc.Flags = planes(desc.Format) > 1
                    ? D3D12_DSV_FLAG_READ_ONLY_DEPTH | D3D12_DSV_FLAG_READ_ONLY_STENCIL
                    : D3D12_DSV_FLAG_READ_ONLY_DEPTH;
            }
            device->CreateDepthStencilView(resource, &desc, view->GetCPUDescriptorHandleForHeapStart());
        } else if (!view) {
            view = descriptor_heap(D3D12_DESCRIPTOR_HEAP_TYPE_RTV, 1, false);
            D3D12_RENDER_TARGET_VIEW_DESC desc{};
            desc.Format = static_cast<DXGI_FORMAT>(format);
            desc.ViewDimension = D3D12_RTV_DIMENSION_TEXTURE2D;
            device->CreateRenderTargetView(resource, &desc, view->GetCPUDescriptorHandleForHeapStart());
        }
        return view->GetCPUDescriptorHandleForHeapStart();
    }

    UINT add(Step &&step, UINT planes_given = NONE) {
        for (const Use &item : step.uses) {
            if (item.state & WRITING && item.id != planes_given) step.writes.push_back({item.id, item.part});
        }
        steps.push_back(std::move(step));
        return static_cast<UINT>(steps.size() - 1);
    }

    UINT draw(Job &job) {
        Step step;
        Binding binding = bind(step, job.tables, job.heap,
                               D3D12_ROOT_SIGNATURE_FLAG_ALLOW_INPUT_ASSEMBLER_INPUT_LAYOUT);

        std::vector<D3D12_INPUT_ELEMENT_DESC> elements;
        for (const Element &element : job.elements) {
            elements.push_back({element.semantic.c_str(), element.index, static_cast<DXGI_FORMAT>(element.format),
                                element.slot, element.offset,
                                element.per_instance ? D3D12_INPUT_CLASSIFICATION_PER_INSTANCE_DATA
                                                     : D3D12_INPUT_CLASSIFICATION_PER_VERTEX_DATA,
                                element.per_instance ? element.step_rate : 0u});
        }
        D3D12_GRAPHICS_PIPELINE_STATE_DESC pipeline_desc{};
        pipeline_desc.pRootSignature = binding.root.Get();
        pipeline_desc.VS = {job.vertex_shader.data(), job.vertex_shader.size()};
        // A draw may have no tessellation stages or no pixel shader, which Direct3D takes as null bytecode only.
        if (!job.hull_shader.empty()) pipeline_desc.HS = {job.hull_shader.data(), job.hull_shader.size()};
        if (!job.domain_shader.empty()) pipeline_desc.DS = {job.domain_shader.data(), job.domain_shader.size()};
        if (!job.geometry_shader.empty())
            pipeline_desc.GS = {job.geometry_shader.data(), job.geometry_shader.size()};
        if (!job.pixel_shader.empty()) pipeline_desc.PS = {job.pixel_shader.data(), job.pixel_shader.size()};
        pipeline_desc.BlendState = job.blend;
        pipeline_desc.SampleMask = UINT_MAX;
        pipeline_desc.RasterizerState = job.rasterizer;
        pipeline_desc.DepthStencilState = job.depth_stencil;
        pipeline_desc.InputLayout = {elements.data(), static_cast<UINT>(elements.size())};
        const D3D12_PRIMITIVE_TOPOLOGY_TYPE types[] = {
            D3D12_PRIMITIVE_TOPOLOGY_TYPE_UNDEFINED, D3D12_PRIMITIVE_TOPOLOGY_TYPE_POINT,
            D3D12_PRIMITIVE_TOPOLOGY_TYPE_LINE, D3D12_PRIMITIVE_TOPOLOGY_TYPE_LINE,
            D3D12_PRIMITIVE_TOPOLOGY_TYPE_TRIANGLE, D3D12_PRIMITIVE_TOPOLOGY_TYPE_TRIANGLE};
        pipeline_desc.PrimitiveTopologyType = job.topology < D3D_PRIMITIVE_TOPOLOGY_1_CONTROL_POINT_PATCHLIST
            ? types[job.topology] : D3D12_PRIMITIVE_TOPOLOGY_TYPE_PATCH;
        pipeline_desc.NumRenderTargets = static_cast<UINT>(job.targets.size());
        for (size_t i = 0; i < job.targets.size(); ++i) {
            pipeline_desc.RTVFormats[i] = static_cast<DXGI_FORMAT>(job.targets[i].format);
        }
        pipeline_desc.DSVFormat = job.has_depth ? static_cast<DXGI_FORMAT>(job.depth.format) : DXGI_FORMAT_UNKNOWN;
        pipeline_desc.SampleDesc.Count = 1;
        // The root signature by its address: it is kept as long as the device.
        std::string key("draw");
        add_part(key, &pipeline_desc.pRootSignature, sizeof pipeline_desc.pRootSignature);
        add_part(key, job.vertex_shader.data(), job.vertex_shader.size());
        add_part(key, job.hull_shader.data(), job.hull_shader.size());
        add_part(key, job.domain_shader.data(), job.domain_shader.size());
        add_part(key, job.geometry_shader.data(), job.geometry_shader.size());
        add_part(key, job.pixel_shader.data(), job.pixel_shader.size());
        add_part(key, &job.blend, sizeof job.blend);
        add_part(key, &job.rasterizer, sizeof job.rasterizer);
        add_part(key, &job.depth_stencil, sizeof job.depth_stencil);
        for (const D3D12_INPUT_ELEMENT_DESC &element : elements) {
            add_part(key, element.SemanticName, strlen(element.SemanticName));
            const UINT rest[] = {element.SemanticIndex, static_cast<UINT>(element.Format), element.InputSlot,
                                 element.AlignedByteOffset, static_cast<UINT>(element.InputSlotClass),
                                 element.InstanceDataStepRate};
            add_part(key, rest, sizeof rest);
        }
        add_part(key, &pipeline_desc.PrimitiveTopologyType, sizeof pipeline_desc.PrimitiveTopologyType);
        add_part(key, &pipeline_desc.NumRenderTargets, sizeof pipeline_desc.NumRenderTargets);
        add_part(key, pipeline_desc.RTVFormats, sizeof pipeline_desc.RTVFormats);
        add_part(key, &pipeline_desc.DSVFormat, sizeof pipeline_desc.DSVFormat);
        ComPtr<ID3D12PipelineState> pipeline = kept->one(kept->pipelines, key, [&] {
            ComPtr<ID3D12PipelineState> made;
            check(device->CreateGraphicsPipelineState(&pipeline_desc, IID_PPV_ARGS(&made)),
                  "Creating the pipeline (the shaders do not fit together or with their resources)");
            return made;
        });

        std::vector<D3D12_CPU_DESCRIPTOR_HANDLE> targets;
        for (const Target &target : job.targets) {
            targets.push_back(target_view(step, target.format, target.data.resource, false));
        }
        const bool has_depth = job.has_depth;
        const D3D12_DEPTH_STENCIL_DESC &tested = job.depth_stencil;
        const bool stencil = has_depth && planes(static_cast<DXGI_FORMAT>(job.depth.format)) > 1;
        const bool read_only = (!tested.DepthEnable || tested.DepthWriteMask == D3D12_DEPTH_WRITE_MASK_ZERO)
            && (!stencil || !tested.StencilEnable || !tested.StencilWriteMask);
        targets.push_back(has_depth ? target_view(step, job.depth.format, job.depth.data.resource, true, read_only)
                                    : D3D12_CPU_DESCRIPTOR_HANDLE{});
        if (has_depth && !read_only) {
            const UINT id = job.depth.data.resource;
            const D3D12_RESOURCE_DESC desc = resources[id].resource->GetDesc();
            const UINT layers = desc.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE3D ? 1u : desc.DepthOrArraySize;
            auto changes = [](const D3D12_DEPTH_STENCILOP_DESC &face) {
                return face.StencilFailOp != D3D12_STENCIL_OP_KEEP || face.StencilDepthFailOp != D3D12_STENCIL_OP_KEEP
                    || face.StencilPassOp != D3D12_STENCIL_OP_KEEP;
            };
            if (tested.DepthEnable && tested.DepthWriteMask != D3D12_DEPTH_WRITE_MASK_ZERO)
                step.writes.push_back({id, 0});
            if (planes(desc.Format) > 1 && tested.StencilEnable && tested.StencilWriteMask
                && (changes(tested.FrontFace) || changes(tested.BackFace)))
                step.writes.push_back({id, desc.MipLevels * layers});
        }
        std::vector<D3D12_VERTEX_BUFFER_VIEW> vertices;
        for (const VertexBuffer &buffer : job.vertex_buffers) {
            UINT id = held(buffer.data);
            ID3D12Resource *resource = use(step, id, true, D3D12_RESOURCE_STATE_VERTEX_AND_CONSTANT_BUFFER);
            vertices.push_back({resource->GetGPUVirtualAddress() + buffer.offset,
                                static_cast<UINT>(resources[id].size) - buffer.offset, buffer.stride});
        }
        std::optional<D3D12_INDEX_BUFFER_VIEW> indices;
        if (job.indexed) {
            UINT index_buffer = held(job.indices);
            indices = D3D12_INDEX_BUFFER_VIEW{
                use(step, index_buffer, true, D3D12_RESOURCE_STATE_INDEX_BUFFER)->GetGPUVirtualAddress(),
                static_cast<UINT>(resources[index_buffer].size), static_cast<DXGI_FORMAT>(job.index_format)};
        }
        ID3D12Resource *arguments = indirect(step, job.indirect);
        ComPtr<ID3D12CommandSignature> signature = arguments ? kept->signature(job.indexed
            ? D3D12_INDIRECT_ARGUMENT_TYPE_DRAW_INDEXED : D3D12_INDIRECT_ARGUMENT_TYPE_DRAW) : nullptr;
        const UINT64 offset = job.indirect.offset;
        const D3D12_VIEWPORT viewport = job.viewport;
        const D3D12_RECT scissor = job.scissor;
        const UINT reference = job.stencil_reference, topology = job.topology, index_count = job.index_count,
                   instance_count = job.instance_count, first_index = job.first_index,
                   first_instance = job.first_instance;
        const INT base_vertex = job.base_vertex;
        step.record = [=](ID3D12GraphicsCommandList *list, Recorded &was) {
            set_root(list, was, false, binding.root.Get(), binding.starts);
            if (differs(was.pipeline, pipeline.Get())) list->SetPipelineState(pipeline.Get());
            if (differs(was.targets, targets)) {
                list->OMSetRenderTargets(static_cast<UINT>(targets.size() - 1), targets.data(), FALSE,
                                         has_depth ? &targets.back() : nullptr);
            }
            if (differs(was.reference, reference)) list->OMSetStencilRef(reference);
            if (differs(was.viewport, viewport)) list->RSSetViewports(1, &viewport);
            if (differs(was.scissor, scissor)) list->RSSetScissorRects(1, &scissor);
            if (differs(was.topology, topology)) {
                list->IASetPrimitiveTopology(static_cast<D3D_PRIMITIVE_TOPOLOGY>(topology));
            }
            if (differs(was.vertices, vertices)) {
                list->IASetVertexBuffers(0, static_cast<UINT>(vertices.size()), vertices.data());
            }
            if (indices && differs(was.indices, *indices)) list->IASetIndexBuffer(&*indices);
            if (arguments) list->ExecuteIndirect(signature.Get(), 1, arguments, offset, nullptr, 0);
            else if (indices) list->DrawIndexedInstanced(index_count, instance_count, first_index, base_vertex,
                                                         first_instance);
            else list->DrawInstanced(index_count, instance_count, first_index, first_instance);
        };
        return add(std::move(step), has_depth ? job.depth.data.resource : NONE);
    }

    ID3D12Resource *indirect(Step &step, const Indirect &arguments) {
        return arguments.buffer == NONE ? nullptr
            : use(step, arguments.buffer, true, D3D12_RESOURCE_STATE_INDIRECT_ARGUMENT);
    }

    UINT dispatch(ComputeJob &job) {
        Step step;
        Binding binding = bind(step, job.tables, job.heap, D3D12_ROOT_SIGNATURE_FLAG_NONE);
        D3D12_COMPUTE_PIPELINE_STATE_DESC pipeline_desc{};
        pipeline_desc.pRootSignature = binding.root.Get();
        pipeline_desc.CS = {job.compute_shader.data(), job.compute_shader.size()};
        std::string key("dispatch");
        add_part(key, &pipeline_desc.pRootSignature, sizeof pipeline_desc.pRootSignature);
        add_part(key, job.compute_shader.data(), job.compute_shader.size());
        ComPtr<ID3D12PipelineState> pipeline = kept->one(kept->pipelines, key, [&] {
            ComPtr<ID3D12PipelineState> made;
            check(device->CreateComputePipelineState(&pipeline_desc, IID_PPV_ARGS(&made)),
                  "Creating the pipeline (the shader does not fit its resources)");
            return made;
        });
        const UINT x = job.groups[0], y = job.groups[1], z = job.groups[2];
        ID3D12Resource *arguments = indirect(step, job.indirect);
        ComPtr<ID3D12CommandSignature> signature = arguments
            ? kept->signature(D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH) : nullptr;
        const UINT64 offset = job.indirect.offset;
        step.record = [=](ID3D12GraphicsCommandList *list, Recorded &was) {
            set_root(list, was, true, binding.root.Get(), binding.starts);
            if (differs(was.pipeline, pipeline.Get())) list->SetPipelineState(pipeline.Get());
            if (arguments) list->ExecuteIndirect(signature.Get(), 1, arguments, offset, nullptr, 0);
            else list->Dispatch(x, y, z);
        };
        return add(std::move(step));
    }

    UINT clear(UINT format, UINT id) {
        Step step;
        bool depth = found(id, false).resource->GetDesc().Flags & D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL;
        D3D12_CPU_DESCRIPTOR_HANDLE view = target_view(step, format, id, depth);
        D3D12_CLEAR_FLAGS flags = D3D12_CLEAR_FLAG_DEPTH;
        if (planes(static_cast<DXGI_FORMAT>(format)) > 1) flags |= D3D12_CLEAR_FLAG_STENCIL;
        step.record = [=](ID3D12GraphicsCommandList *list, Recorded &) {
            static const FLOAT zero[4] = {};
            if (depth) list->ClearDepthStencilView(view, flags, 0.0f, 0, 0, nullptr);
            else list->ClearRenderTargetView(view, zero, 0, nullptr);
        };
        return add(std::move(step));
    }

    // Texture-to-buffer copies lay rows out 256-byte aligned (block rows for compressed formats), slice after slice.
    UINT copy(UINT to, UINT from, UINT to_subresource, UINT from_subresource) {
        Step step;
        bool buffer = to < resources.size() && resources[to].buffer;
        bool laid_out = buffer && from < resources.size() && !resources[from].buffer;
        if (buffer && to_subresource != NONE) throw Failure{"a buffer has no subresources"};
        if (laid_out && from_subresource != NONE) throw Failure{"a buffer takes a texture of one subresource whole"};
        ID3D12Resource *target = use(step, to, buffer, D3D12_RESOURCE_STATE_COPY_DEST);
        ID3D12Resource *source = use(step, from, buffer && !laid_out, D3D12_RESOURCE_STATE_COPY_SOURCE);
        step.copied = Step::Copied{{from, from_subresource}, {to, to_subresource}};
        const UINT64 held = resources[to].size;
        if (laid_out) {
            const D3D12_RESOURCE_DESC desc = source->GetDesc();
            if (subresources(desc) != 1) throw Failure{"a buffer takes a texture of one subresource whole"};
            D3D12_PLACED_SUBRESOURCE_FOOTPRINT layout{};
            UINT rows = 0;
            UINT64 row = 0;
            device->GetCopyableFootprints(&desc, 0, 1, 0, &layout, &rows, &row, nullptr);
            const UINT64 pitch = (row + D3D12_TEXTURE_DATA_PITCH_ALIGNMENT - 1) / D3D12_TEXTURE_DATA_PITCH_ALIGNMENT
                * D3D12_TEXTURE_DATA_PITCH_ALIGNMENT;
            layout.Offset = 0;
            layout.Footprint.RowPitch = static_cast<UINT>(pitch);
            if (pitch * (static_cast<UINT64>(rows) * layout.Footprint.Depth - 1) + row > held)
                throw Failure{"the buffer is smaller than the texture laid out"};
            step.record = [=](ID3D12GraphicsCommandList *list, Recorded &) {
                D3D12_TEXTURE_COPY_LOCATION onto{target, D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT, {}};
                onto.PlacedFootprint = layout;
                D3D12_TEXTURE_COPY_LOCATION of{source, D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX, {}};
                of.SubresourceIndex = 0;
                list->CopyTextureRegion(&onto, 0, 0, 0, &of, nullptr);
            };
            return add(std::move(step));
        }
        const UINT64 size = buffer ? resources[from].size : 0;
        if (buffer && size > held) throw Failure{"a buffer is copied onto one at least its size"};
        if (buffer && size != held) {
            step.record = [=](ID3D12GraphicsCommandList *list, Recorded &) {
                list->CopyBufferRegion(target, 0, source, 0, size);
            };
            return add(std::move(step));
        }
        step.record = [=](ID3D12GraphicsCommandList *list, Recorded &) {
            if (to_subresource == NONE) return list->CopyResource(target, source);
            D3D12_TEXTURE_COPY_LOCATION onto{target, D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX, {}};
            onto.SubresourceIndex = to_subresource;
            D3D12_TEXTURE_COPY_LOCATION of{source, D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX, {}};
            of.SubresourceIndex = from_subresource;
            list->CopyTextureRegion(&onto, 0, 0, 0, &of, nullptr);
        };
        return add(std::move(step));
    }

    UINT copy_bytes(UINT to, UINT64 to_offset, UINT from, UINT64 from_offset, UINT64 size) {
        Step step;
        ID3D12Resource *target = use(step, to, true, D3D12_RESOURCE_STATE_COPY_DEST);
        ID3D12Resource *source = use(step, from, true, D3D12_RESOURCE_STATE_COPY_SOURCE);
        step.copied = Step::Copied{{from, NONE}, {to, NONE}, from_offset, to_offset, size};
        if (!size || to_offset + size > resources[to].size || from_offset + size > resources[from].size)
            throw Failure{"the bytes copied lie outside a buffer"};
        step.record = [=](ID3D12GraphicsCommandList *list, Recorded &) {
            list->CopyBufferRegion(target, to_offset, source, from_offset, size);
        };
        return add(std::move(step));
    }

    void need(ID3D12GraphicsCommandList *list, UINT id, D3D12_RESOURCE_STATES state,
              UINT part = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES) {
        Resource &resource = resources[id];
        auto transition = [&](UINT subresource, D3D12_RESOURCE_STATES before, D3D12_RESOURCE_STATES after) {
            D3D12_RESOURCE_BARRIER barrier{};
            barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
            barrier.Transition.pResource = resource.resource.Get();
            barrier.Transition.Subresource = subresource;
            barrier.Transition.StateBefore = before;
            barrier.Transition.StateAfter = after;
            list->ResourceBarrier(1, &barrier);
        };
        const bool whole = part == D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
        if (whole) {
            for (const auto &[subresource, held] : resource.apart) transition(subresource, held, resource.state);
            resource.apart.clear();
        }
        const auto apart = resource.apart.find(part);
        const D3D12_RESOURCE_STATES before = apart == resource.apart.end() ? resource.state : apart->second;
        if (before != state) {
            if (resource.buffer && resource.state == D3D12_RESOURCE_STATE_COMMON) {
                promoted.push_back(id);
                resource.state = state;
                return;
            }
            transition(part, before, state);
            if (whole) resource.state = state;
            else if (state == resource.state) resource.apart.erase(part);
            else resource.apart[part] = state;
        } else if (state == D3D12_RESOURCE_STATE_UNORDERED_ACCESS) {
            D3D12_RESOURCE_BARRIER barrier{};
            barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_UAV;
            barrier.UAV.pResource = resource.resource.Get();
            list->ResourceBarrier(1, &barrier);
        }
    }

    struct Readback {
        ComPtr<ID3D12Resource> memory;
        std::vector<D3D12_PLACED_SUBRESOURCE_FOOTPRINT> layouts;
        std::vector<UINT> rows;
        std::vector<UINT64> row_sizes;
    };

    Readback read_later(ID3D12GraphicsCommandList *list, UINT id) {
        const Resource &resource = resources[id];
        Readback result;
        need(list, id, D3D12_RESOURCE_STATE_COPY_SOURCE);
        if (resource.buffer) {
            result.layouts.resize(1);
            result.layouts[0].Footprint.Depth = 1;
            result.rows = {1};
            result.row_sizes = {resource.size};
            result.memory = memory(resource.size, D3D12_HEAP_TYPE_READBACK, D3D12_RESOURCE_STATE_COPY_DEST);
            list->CopyBufferRegion(result.memory.Get(), 0, resource.resource.Get(), 0, resource.size);
            return result;
        }
        D3D12_RESOURCE_DESC desc = resource.resource->GetDesc();
        UINT count = subresources(desc);
        result.layouts.resize(count);
        result.rows.resize(count);
        result.row_sizes.resize(count);
        UINT64 total = 0;
        device->GetCopyableFootprints(&desc, 0, count, 0, result.layouts.data(), result.rows.data(),
                                      result.row_sizes.data(), &total);
        result.memory = memory(total, D3D12_HEAP_TYPE_READBACK, D3D12_RESOURCE_STATE_COPY_DEST);
        for (UINT i = 0; i < count; ++i) {
            D3D12_TEXTURE_COPY_LOCATION to{result.memory.Get(), D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT, {}};
            to.PlacedFootprint = result.layouts[i];
            D3D12_TEXTURE_COPY_LOCATION from{resource.resource.Get(), D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX, {}};
            from.SubresourceIndex = i;
            list->CopyTextureRegion(&to, 0, 0, 0, &from, nullptr);
        }
        return result;
    }

    static std::vector<uint8_t> read(const Readback &readback) {
        uint8_t *mapped = nullptr;
        check(readback.memory->Map(0, nullptr, reinterpret_cast<void **>(&mapped)), "Map");
        std::vector<uint8_t> result;
        for (size_t i = 0; i < readback.layouts.size(); ++i) {
            const D3D12_PLACED_SUBRESOURCE_FOOTPRINT &layout = readback.layouts[i];
            for (UINT slice = 0; slice < layout.Footprint.Depth; ++slice) {
                for (UINT row = 0; row < readback.rows[i]; ++row) {
                    const uint8_t *start = mapped + layout.Offset
                        + (static_cast<UINT64>(slice) * readback.rows[i] + row) * layout.Footprint.RowPitch;
                    result.insert(result.end(), start, start + readback.row_sizes[i]);
                }
            }
        }
        D3D12_RANGE none{0, 0};
        readback.memory->Unmap(0, &none);
        return result;
    }

    void show(HWND window, UINT id) {
        if (id >= resources.size() || resources[id].buffer) throw Failure{"the scene has no such texture"};
        const D3D12_RESOURCE_DESC desc = resources[id].resource->GetDesc();
        if (desc.Dimension != D3D12_RESOURCE_DIMENSION_TEXTURE2D || desc.DepthOrArraySize != 1 || desc.MipLevels != 1)
            throw Failure{"a window shows a 2D texture of one layer and one mip"};
        const UINT width = static_cast<UINT>(desc.Width);
        const DXGI_FORMAT format = shown_format(desc.Format);
        Window &shown = windows[window];
        if (!shown.chain) {
            DXGI_SWAP_CHAIN_DESC1 chain_desc{};
            chain_desc.Width = width;
            chain_desc.Height = desc.Height;
            chain_desc.Format = format;
            chain_desc.SampleDesc.Count = 1;
            chain_desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
            chain_desc.BufferCount = 3;
            chain_desc.Scaling = DXGI_SCALING_NONE;
            chain_desc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
            chain_desc.AlphaMode = DXGI_ALPHA_MODE_IGNORE;
            ComPtr<IDXGIFactory2> factory;
            ComPtr<IDXGISwapChain1> chain;
            check(CreateDXGIFactory2(0, IID_PPV_ARGS(&factory)), "CreateDXGIFactory2");
            ComPtr<IDXGIFactory5> tearing_factory;
            BOOL tearing = FALSE;
            if (SUCCEEDED(factory.As(&tearing_factory))
                && SUCCEEDED(tearing_factory->CheckFeatureSupport(DXGI_FEATURE_PRESENT_ALLOW_TEARING, &tearing,
                                                                  sizeof tearing))
                && tearing)
                chain_desc.Flags = DXGI_SWAP_CHAIN_FLAG_ALLOW_TEARING;
            shown.flags = chain_desc.Flags;
            check(factory->CreateSwapChainForHwnd(commands.queue.Get(), window, &chain_desc, nullptr, nullptr,
                                                  &chain), "CreateSwapChainForHwnd");
            check(factory->MakeWindowAssociation(window, DXGI_MWA_NO_ALT_ENTER), "MakeWindowAssociation");
            check(chain.As(&shown.chain), "IDXGISwapChain3");
        } else if (shown.width != width || shown.height != desc.Height || shown.format != format) {
            // The buffers are the swap chain's alone once nothing presenting them is left to run.
            wait(commands.signalled);
            check(shown.chain->ResizeBuffers(0, width, desc.Height, format, shown.flags), "ResizeBuffers");
        }
        shown.width = width;
        shown.height = desc.Height;
        shown.format = format;

        ID3D12GraphicsCommandList *list = record(frames[frame]);
        ComPtr<ID3D12Resource> back;
        check(shown.chain->GetBuffer(shown.chain->GetCurrentBackBufferIndex(), IID_PPV_ARGS(&back)), "GetBuffer");
        need(list, id, D3D12_RESOURCE_STATE_COPY_SOURCE);
        D3D12_RESOURCE_BARRIER barrier{};
        barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
        barrier.Transition.pResource = back.Get();
        barrier.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
        barrier.Transition.StateBefore = D3D12_RESOURCE_STATE_PRESENT;
        barrier.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_DEST;
        list->ResourceBarrier(1, &barrier);
        list->CopyResource(back.Get(), resources[id].resource.Get());
        std::swap(barrier.Transition.StateBefore, barrier.Transition.StateAfter);
        list->ResourceBarrier(1, &barrier);
        submit();
        // The swap chain's buffers must be its own alone when it is next resized.
        back.Reset();
        check(shown.chain->Present(0, shown.flags ? DXGI_PRESENT_ALLOW_TEARING : 0), "Present");
    }

    void hide(HWND window) {
        if (!windows.count(window)) return;
        wait(commands.signalled);
        windows.erase(window);
    }

    // ``reads`` come back with subresources tightly packed.
    std::vector<std::vector<uint8_t>> run(const std::vector<UINT> &order, const std::vector<UINT> &reads) {
        for (UINT id : order) {
            if (id >= steps.size()) throw Failure{"the scene has no such step"};
            for (const Use &item : steps[id].uses) {
                if (!resources[item.id].resource) throw Failure{"a step uses a released resource"};
            }
        }
        for (UINT id : reads) {
            if (id >= resources.size() || !resources[id].resource) throw Failure{"the scene has no such resource"};
        }
        ID3D12GraphicsCommandList *list = record(current());
        try {
            record_run(list, order, reads);
        } catch (...) {
            abandon();
            throw;
        }
        submit();
        turned = true;
        fills.clear();
        // Other jobs use kept textures from other queues, so they must be filled first.
        if (!readbacks.empty() || !made.empty()) wait(commands.signalled);
        kept->keep(made);
        made_resources.clear();
        std::vector<std::vector<uint8_t>> contents;
        for (const Readback &readback : readbacks) contents.push_back(read(readback));
        readbacks.clear();
        return contents;
    }

    std::vector<Readback> readbacks;

    void record_run(ID3D12GraphicsCommandList *list, const std::vector<UINT> &order, const std::vector<UINT> &reads) {
        readbacks.clear();
        for (const Fill &fill : fills) {
            need(list, fill.resource, D3D12_RESOURCE_STATE_COPY_DEST);
            ID3D12Resource *resource = resources[fill.resource].resource.Get();
            if (fill.layouts.empty()) list->CopyBufferRegion(resource, 0, fill.memory, fill.offset, fill.size);
            for (size_t i = 0; i < fill.layouts.size(); ++i) {
                D3D12_TEXTURE_COPY_LOCATION to{resource, D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX, {}};
                to.SubresourceIndex = static_cast<UINT>(i);
                D3D12_TEXTURE_COPY_LOCATION from{fill.memory, D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT, {}};
                from.PlacedFootprint = fill.layouts[i];
                list->CopyTextureRegion(&to, 0, 0, 0, &from, nullptr);
            }
            if (fill.after) need(list, fill.resource, fill.after);
        }
        ID3D12DescriptorHeap *heaps[2] = {};
        Recorded recorded;
        Written written(resources.size());
        std::map<Step::Copied, std::pair<UINT64, UINT64>> copies;
        auto seen = [&](const Step::Copied &copied) {
            return std::pair{written.of(copied.from), written.of(copied.to)};
        };
        for (UINT id : order) {
            const Step &step = steps[id];
            if (step.copied) {
                const auto last = copies.find(*step.copied);
                if (last != copies.end() && last->second == seen(*step.copied)) continue;
            }
            for (const Use &item : step.uses) need(list, item.id, item.state, item.part);
            if (step.heaps[0] && (step.heaps[0] != heaps[0] || step.heaps[1] != heaps[1])) {
                heaps[0] = step.heaps[0];
                heaps[1] = step.heaps[1];
                list->SetDescriptorHeaps(2, heaps);
                // Tables set before lie in other heaps.
                for (std::vector<UINT64> &tables : recorded.tables) tables.clear();
            }
            step.record(list, recorded);
            for (const Part &item : step.writes) written.write(item);
            if (step.copied) copies[*step.copied] = seen(*step.copied);
        }
        for (UINT id : reads) readbacks.push_back(read_later(list, id));
    }
};

const char *invalid_arguments(const Indirect &indirect, UINT64 size, const Scene &scene) {
    if (indirect.buffer >= scene.resources.size() || !scene.resources[indirect.buffer].buffer)
        return "indirect arguments lie in no buffer of the scene";
    if (indirect.offset % 4 || indirect.offset + size > scene.resources[indirect.buffer].size)
        return "the indirect arguments lie outside their buffer";
    return nullptr;
}

const char *invalid_job(const Job &job, const Scene &scene) {
    if (!job.width || !job.height || job.width > MAX_SIZE || job.height > MAX_SIZE) return "image size is out of range";
    if ((job.targets.empty() && !job.has_depth) || job.targets.size() > D3D12_SIMULTANEOUS_RENDER_TARGET_COUNT)
        return "render target count is out of range";
    if (job.elements.size() > D3D12_IA_VERTEX_INPUT_STRUCTURE_ELEMENT_COUNT)
        return "vertex element count is out of range";
    if (job.vertex_buffers.size() > D3D12_IA_VERTEX_INPUT_RESOURCE_SLOT_COUNT) return "too many vertex buffers";
    if (job.tables.size() > 64) return "too many descriptor tables";
    if (job.scissor.left >= job.scissor.right || job.scissor.top >= job.scissor.bottom
        || job.scissor.right > static_cast<LONG>(job.width) || job.scissor.bottom > static_cast<LONG>(job.height))
        return "the scissor rectangle lies outside the targets";
    for (const Table &table : job.tables) {
        if (table.visibility > D3D12_SHADER_VISIBILITY_PIXEL) return "unknown shader visibility";
    }
    if (job.indexed && job.index_format != DXGI_FORMAT_R16_UINT && job.index_format != DXGI_FORMAT_R32_UINT)
        return "indices must be 16- or 32-bit";
    UINT64 index_size = job.index_format == DXGI_FORMAT_R16_UINT ? 2 : 4;
    const bool patches = job.topology >= D3D_PRIMITIVE_TOPOLOGY_1_CONTROL_POINT_PATCHLIST
        && job.topology <= D3D_PRIMITIVE_TOPOLOGY_32_CONTROL_POINT_PATCHLIST;
    if (!patches && (job.topology < D3D_PRIMITIVE_TOPOLOGY_POINTLIST
                     || job.topology > D3D_PRIMITIVE_TOPOLOGY_TRIANGLESTRIP))
        return "the topology is no list or strip of points, lines or triangles, nor a list of patches";
    if (job.hull_shader.empty() != job.domain_shader.empty()) return "a hull shader needs a domain shader, and back";
    if (patches == job.hull_shader.empty()) return "patches are drawn through hull and domain shaders, and only they";
    if (job.indirect.buffer != NONE) {
        const char *invalid = invalid_arguments(job.indirect, job.indexed ? sizeof(D3D12_DRAW_INDEXED_ARGUMENTS)
                                                                         : sizeof(D3D12_DRAW_ARGUMENTS), scene);
        if (invalid) return invalid;
    } else {
        const UINT least[] = {0, 1, 2, 2, 3, 3}, apiece[] = {0, 1, 2, 1, 3, 1};
        const UINT points = job.topology - D3D_PRIMITIVE_TOPOLOGY_1_CONTROL_POINT_PATCHLIST + 1;
        if (patches ? !job.index_count || job.index_count % points
                    : job.index_count < least[job.topology] || job.index_count % apiece[job.topology])
            return job.indexed ? "the index count makes no whole primitives"
                               : "the vertex count makes no whole primitives";
        if (job.indexed && (scene.size(job.indices) % index_size
                            || (static_cast<UINT64>(job.first_index) + job.index_count) * index_size
                                > scene.size(job.indices)))
            return "the draw's primitives lie outside the indices";
        if (!job.instance_count) return "the draw has no instances";
    }
    if (!job.indexed && job.base_vertex) return "a draw without indices has no base vertex";
    for (const VertexBuffer &buffer : job.vertex_buffers) {
        if (!buffer.stride || buffer.offset >= scene.size(buffer.data)) return "vertex buffer is empty";
    }
    for (const Element &element : job.elements) {
        if (element.slot >= job.vertex_buffers.size()) return "vertex element reads a missing vertex buffer";
        if (element.offset >= job.vertex_buffers[element.slot].stride) return "vertex element lies outside the vertex";
    }
    return nullptr;
}

const char *invalid_compute_job(const ComputeJob &job, const Scene &scene) {
    if (job.indirect.buffer != NONE) {
        if (const char *invalid = invalid_arguments(job.indirect, sizeof(D3D12_DISPATCH_ARGUMENTS), scene))
            return invalid;
    } else {
        for (UINT count : job.groups) {
            if (!count || count > D3D12_CS_DISPATCH_MAX_THREAD_GROUPS_PER_DIMENSION)
                return "group count is out of range";
        }
    }
    if (job.tables.size() > 64) return "too many descriptor tables";
    for (const Table &table : job.tables) {
        if (table.visibility != D3D12_SHADER_VISIBILITY_ALL) return "a compute shader's tables are visible to all";
    }
    return nullptr;
}

UINT draw_step(Scene &scene, Job &job) {
    if (const char *invalid = invalid_job(job, scene)) throw Invalid{invalid};
    return scene.draw(job);
}

UINT dispatch_step(Scene &scene, ComputeJob &job) {
    if (const char *invalid = invalid_compute_job(job, scene)) throw Invalid{invalid};
    return scene.dispatch(job);
}

std::vector<std::vector<uint8_t>> draw(Job &job, bool hardware) {
    Scene scene(hardware);
    if (const char *invalid = invalid_job(job, scene)) throw Invalid{invalid};
    std::vector<UINT> order, reads;
    for (size_t i = 0; i < job.targets.size() + job.has_depth; ++i) {
        bool depth = i == job.targets.size();
        Target &target = depth ? job.depth : job.targets[i];
        if (target.data.resource != NONE) throw Invalid{"a draw alone has no texture to name"};
        Entry shape;
        shape.format = target.format;
        shape.width = job.width;
        shape.height = job.height;
        shape.depth = shape.mips = 1;
        target.data.resource = scene.texture(
            texture_desc(SRV, D3D12_SRV_DIMENSION_TEXTURE2D, shape,
                         depth ? D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL : D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET),
            target.clear ? nullptr : target.data.bytes.data(), target.data.bytes.size());
        if (target.clear) order.push_back(scene.clear(target.format, target.data.resource));
        reads.push_back(target.data.resource);
    }
    order.push_back(scene.draw(job));
    return scene.run(order, reads);
}

PyObject *byte_list(const std::vector<std::vector<uint8_t>> &items) {
    PyObject *result = PyList_New(static_cast<Py_ssize_t>(items.size()));
    if (!result) return nullptr;
    for (size_t i = 0; i < items.size(); ++i) {
        PyObject *item = PyBytes_FromStringAndSize(reinterpret_cast<const char *>(items[i].data()),
                                                   static_cast<Py_ssize_t>(items[i].size()));
        if (!item) {
            Py_DECREF(result);
            return nullptr;
        }
        PyList_SetItem(result, static_cast<Py_ssize_t>(i), item);
    }
    return result;
}

template <typename Work>
bool unlocked(Work work) {
    std::string failure;
    bool invalid = false;
    Py_BEGIN_ALLOW_THREADS
    try {
        work();
    } catch (const Invalid &error) {
        failure = error.message;
        invalid = true;
    } catch (const Failure &error) {
        failure = error.message;
    } catch (const std::exception &error) {
        failure = error.what();
    }
    Py_END_ALLOW_THREADS
    if (failure.empty()) return true;
    PyErr_SetString(invalid ? PyExc_ValueError : PyExc_RuntimeError, failure.c_str());
    return false;
}

PyObject *draw_call(PyObject *, PyObject *object) {
    Job job;
    bool hardware;
    std::vector<std::vector<uint8_t>> images;
    if (!parse_job(object, job) || !parse_hardware(object, hardware)) return nullptr;
    return unlocked([&] { images = draw(job, hardware); }) ? byte_list(images) : nullptr;
}

struct SceneObject {
    PyObject_HEAD
    Scene *scene;
};

template <typename Work>
bool on_scene(PyObject *self, Work work) {
    Scene &scene = *reinterpret_cast<SceneObject *>(self)->scene;
    return unlocked([&] {
        std::lock_guard<std::mutex> guard(scene.lock);
        work(scene);
    });
}

PyObject *scene_new(PyTypeObject *type, PyObject *arguments, PyObject *) {
    int hardware;
    if (!PyArg_ParseTuple(arguments, "p", &hardware)) return nullptr;
    PyObject *self = reinterpret_cast<allocfunc>(PyType_GetSlot(type, Py_tp_alloc))(type, 0);
    if (!self) return nullptr;
    Scene *&scene = reinterpret_cast<SceneObject *>(self)->scene;
    scene = nullptr;
    if (unlocked([&] { scene = new Scene(hardware != 0); })) return self;
    Py_DECREF(self);
    return nullptr;
}

void scene_dealloc(PyObject *self) {
    PyTypeObject *type = Py_TYPE(self);
    delete reinterpret_cast<SceneObject *>(self)->scene;
    reinterpret_cast<freefunc>(PyType_GetSlot(type, Py_tp_free))(self);
    Py_DECREF(type);
}

// ``id`` by reference: it is read after the work that sets it, whatever order the arguments are evaluated in.
PyObject *number(bool made, const UINT &id) { return made ? PyLong_FromUnsignedLong(id) : nullptr; }

PyObject *scene_texture(PyObject *self, PyObject *arguments) {
    Entry shape;
    unsigned int dimension, flags;
    PyObject *texels;
    Bytes data;
    if (!PyArg_ParseTuple(arguments, "IIIIIIIO", &shape.format, &shape.width, &shape.height, &shape.depth,
                          &shape.mips, &dimension, &flags, &texels)) return nullptr;
    if (texels != Py_None && !data.hold(texels)) return nullptr;
    UINT id = 0;
    return number(on_scene(self, [&](Scene &scene) {
        id = scene.texture(texture_desc(SRV, dimension, shape, static_cast<D3D12_RESOURCE_FLAGS>(flags)),
                           texels == Py_None ? nullptr : data.data(), data.size());
    }), id);
}

PyObject *scene_buffer(PyObject *self, PyObject *arguments) {
    PyObject *object;
    unsigned int flags;
    Bytes data;
    if (!PyArg_ParseTuple(arguments, "OI", &object, &flags) || !data.hold(object)) return nullptr;
    UINT id = 0;
    return number(on_scene(self, [&](Scene &scene) {
        id = scene.buffer(data.data(), data.size(), static_cast<D3D12_RESOURCE_FLAGS>(flags));
    }), id);
}

PyObject *scene_write(PyObject *self, PyObject *arguments) {
    unsigned int id;
    PyObject *object;
    Bytes data;
    if (!PyArg_ParseTuple(arguments, "IO", &id, &object) || !data.hold(object)) return nullptr;
    if (!on_scene(self, [&](Scene &scene) {
            if (id < scene.resources.size() && !scene.resources[id].buffer) {
                scene.fill_texture(id, data.data(), data.size(), D3D12_RESOURCE_STATE_COMMON);
            } else {
                scene.fill_buffer(id, data.data(), data.size());
            }
        })) return nullptr;
    Py_RETURN_NONE;
}

PyObject *scene_draw(PyObject *self, PyObject *object) {
    Job job;
    UINT id = 0;
    if (!parse_job(object, job)) return nullptr;
    return number(on_scene(self, [&](Scene &scene) { id = draw_step(scene, job); }), id);
}

PyObject *scene_dispatch(PyObject *self, PyObject *object) {
    ComputeJob job;
    UINT id = 0;
    if (!parse_compute_job(object, job)) return nullptr;
    return number(on_scene(self, [&](Scene &scene) { id = dispatch_step(scene, job); }), id);
}

PyObject *scene_clear(PyObject *self, PyObject *arguments) {
    unsigned int format, texture;
    UINT id = 0;
    if (!PyArg_ParseTuple(arguments, "II", &format, &texture)) return nullptr;
    return number(on_scene(self, [&](Scene &scene) { id = scene.clear(format, texture); }), id);
}

PyObject *scene_copy(PyObject *self, PyObject *arguments) {
    unsigned int to, from, to_subresource = NONE, from_subresource = NONE;
    UINT id = 0;
    if (!PyArg_ParseTuple(arguments, "II|II", &to, &from, &to_subresource, &from_subresource)) return nullptr;
    return number(on_scene(self, [&](Scene &scene) {
        id = scene.copy(to, from, to_subresource, from_subresource);
    }), id);
}

PyObject *scene_copy_bytes(PyObject *self, PyObject *arguments) {
    unsigned int to, from;
    unsigned long long to_offset, from_offset, size;
    UINT id = 0;
    if (!PyArg_ParseTuple(arguments, "IKIKK", &to, &to_offset, &from, &from_offset, &size)) return nullptr;
    return number(on_scene(self, [&](Scene &scene) {
        id = scene.copy_bytes(to, to_offset, from, from_offset, size);
    }), id);
}

PyObject *scene_run(PyObject *self, PyObject *arguments) {
    PyObject *steps, *resources;
    std::vector<UINT> order, reads;
    if (!PyArg_ParseTuple(arguments, "OO", &steps, &resources)) return nullptr;
    auto numbers = [](PyObject *sequence, std::vector<UINT> &out) {
        return each(sequence, [&](PyObject *item) {
            out.push_back(0);
            return unsigned_value(item, out.back());
        });
    };
    if (!numbers(steps, order) || !numbers(resources, reads)) return nullptr;
    std::vector<std::vector<uint8_t>> contents;
    return on_scene(self, [&](Scene &scene) { contents = scene.run(order, reads); }) ? byte_list(contents) : nullptr;
}

PyObject *scene_show(PyObject *self, PyObject *arguments) {
    unsigned long long window;
    unsigned int texture;
    if (!PyArg_ParseTuple(arguments, "KI", &window, &texture)) return nullptr;
    if (!on_scene(self, [&](Scene &scene) { scene.show(reinterpret_cast<HWND>(window), texture); })) return nullptr;
    Py_RETURN_NONE;
}

PyObject *scene_finish(PyObject *self, PyObject *) {
    if (!on_scene(self, [&](Scene &scene) { scene.wait(scene.commands.signalled); })) return nullptr;
    Py_RETURN_NONE;
}

PyObject *scene_hide(PyObject *self, PyObject *object) {
    const unsigned long long window = PyLong_AsUnsignedLongLong(object);
    if (PyErr_Occurred()) return nullptr;
    if (!on_scene(self, [&](Scene &scene) { scene.hide(reinterpret_cast<HWND>(window)); })) return nullptr;
    Py_RETURN_NONE;
}

PyObject *scene_release(PyObject *self, PyObject *object) {
    std::vector<UINT> ids;
    if (!each(object, [&](PyObject *item) {
            ids.push_back(0);
            return unsigned_value(item, ids.back());
        }))
        return nullptr;
    if (!on_scene(self, [&](Scene &scene) { scene.release(ids); })) return nullptr;
    Py_RETURN_NONE;
}

PyObject *scene_rasterizer_ordered(PyObject *self, PyObject *) {
    D3D12_FEATURE_DATA_D3D12_OPTIONS options{};
    if (!on_scene(self, [&](Scene &scene) {
            check(scene.device->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS, &options, sizeof options),
                  "CheckFeatureSupport");
        }))
        return nullptr;
    return PyBool_FromLong(options.ROVsSupported);
}

PyObject *scene_adapter(PyObject *self, PyObject *) {
    std::wstring name;
    if (!on_scene(self, [&](Scene &scene) {
            ComPtr<IDXGIFactory4> factory;
            ComPtr<IDXGIAdapter1> adapter;
            DXGI_ADAPTER_DESC1 description{};
            check(CreateDXGIFactory2(0, IID_PPV_ARGS(&factory)), "CreateDXGIFactory2");
            check(factory->EnumAdapterByLuid(scene.device->GetAdapterLuid(), IID_PPV_ARGS(&adapter)),
                  "EnumAdapterByLuid");
            check(adapter->GetDesc1(&description), "GetDesc1");
            name = description.Description;
        }))
        return nullptr;
    return PyUnicode_FromWideChar(name.c_str(), -1);
}

PyObject *scene_atomics64(PyObject *self, PyObject *) {
    D3D12_FEATURE_DATA_D3D12_OPTIONS9 options{};
    HRESULT result = S_OK;
    if (!on_scene(self, [&](Scene &scene) {
            result = scene.device->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS9, &options, sizeof options);
        }))
        return nullptr;
    return PyBool_FromLong(SUCCEEDED(result) && options.AtomicInt64OnTypedResourceSupported
                           && options.AtomicInt64OnGroupSharedSupported);
}

PyMethodDef scene_methods[] = {
    {"texture", scene_texture, METH_VARARGS,
     "texture(DXGI format, width, height, depth or layers, mips, shader resource view dimension, resource flags,\n"
     "texels) -> resource\n"
     "A texture of these subresources, packed tightly in Direct3D's order, or, given None, of whatever a step first\n"
     "leaves in it. The flags, in Direct3D 12's numbering, allow it as a render target, a depth/stencil target or\n"
     "for unordered access; one read through views of several formats has a typeless format."},
    {"buffer", scene_buffer, METH_VARARGS,
     "buffer(data, resource flags) -> resource\n"
     "A buffer of these bytes: vertices, indices, constants, or what a shader reads or writes as a buffer."},
    {"write", scene_write, METH_VARARGS,
     "write(resource, data)\n"
     "The bytes a buffer, or the tightly packed subresources a texture, holds from the next run on."},
    {"draw", scene_draw, METH_O,
     "draw(job) -> step\n"
     "The step of one indexed draw: ``fastmatpreview.draw``'s job, without ``hardware``, each target (DXGI format,\n"
     "texture) and the depth likewise. A resource's number stands wherever a job gives bytes for a buffer, and an\n"
     "entry (index, DXGI format, texture) for a texture's view of that format. ``indices`` None draws without\n"
     "indices, the counts and the first index being vertices'; ``indirect`` (buffer, byte offset) in place of\n"
     "``draw`` takes the counts from the buffer when the step runs, as Direct3D lays draw arguments out."},
    {"dispatch", scene_dispatch, METH_O,
     "dispatch(job) -> step\n"
     "The step of one compute dispatch. ``job`` is a dict: compute_shader (bytes); groups (x, y, z); tables and\n"
     "heap as ``fastmatpreview.draw``'s, every table visible to all stages, its resources named as draw's;\n"
     "``indirect`` (buffer, byte offset) in place of ``groups`` takes the group counts from the buffer when the\n"
     "step runs."},
    {"clear", scene_clear, METH_VARARGS,
     "clear(DXGI format, texture) -> step\n"
     "The step clearing a render target, or a depth/stencil target, to zero through its view of the format."},
    {"copy", scene_copy, METH_VARARGS,
     "copy(to, from[, to subresource, from subresource]) -> step\n"
     "The step copying a resource onto another of its description, or one subresource of a texture onto one of\n"
     "another's; or a buffer onto the start of a larger one, or a texture of one subresource onto the start of a\n"
     "buffer, each row of texels (of blocks, for block-compressed formats) the least multiple of 256 bytes holding it\n"
     "after the last, each depth slice's rows after the last slice's."},
    {"copy_bytes", scene_copy_bytes, METH_VARARGS,
     "copy_bytes(to, offset, from, offset, size) -> step\n"
     "The step copying ``size`` bytes of a buffer, from its offset on, onto another buffer at its offset."},
    {"run", scene_run, METH_VARARGS,
     "run(steps, resources) -> contents\n"
     "Fills the resources given bytes since the last run, runs the steps in the order given, and returns what each\n"
     "of the resources named holds afterwards, a texture's subresources packed tightly. A step reading a texture\n"
     "reads what the steps before it left there, in this run or an earlier one."},
    {"show", scene_show, METH_VARARGS,
     "show(window, texture)\n"
     "Shows what a 2D texture of one layer and one mip holds in the window of this handle, a texel a pixel from the\n"
     "window's top left corner, without waiting for the screen's refresh: a texture of a format windows show, or a\n"
     "typeless one of the family of R8G8B8A8_UNORM, B8G8R8A8_UNORM, R10G10B10A2_UNORM or R16G16B16A16_FLOAT, shown\n"
     "as that. Call ``hide`` before the window is destroyed."},
    {"finish", scene_finish, METH_NOARGS,
     "finish()\n"
     "Waits until what was run and shown has been drawn."},
    {"hide", scene_hide, METH_O,
     "hide(window)\n"
     "Stops showing textures in the window."},
    {"release", scene_release, METH_O,
     "release(resources)\n"
     "Lets go of the buffers and textures numbered ``resources``, with the bytes waiting for them: no step using one\n"
     "may run again, and none is made with one."},
    {"rasterizer_ordered", scene_rasterizer_ordered, METH_NOARGS,
     "rasterizer_ordered() -> bool\n"
     "Whether pixel shaders on the device may use rasterizer-ordered views."},
    {"adapter", scene_adapter, METH_NOARGS,
     "adapter() -> str\n"
     "The name of the graphics adapter the device is of, as the adapter describes itself."},
    {"atomics64", scene_atomics64, METH_NOARGS,
     "atomics64() -> bool\n"
     "Whether shaders on the device may use 64-bit atomics on typed resources and on group shared memory; a\n"
     "runtime that cannot tell does not allow them."},
    {nullptr, nullptr, 0, nullptr},
};

PyType_Slot scene_slots[] = {
    {Py_tp_new, reinterpret_cast<void *>(scene_new)},
    {Py_tp_dealloc, reinterpret_cast<void *>(scene_dealloc)},
    {Py_tp_methods, scene_methods},
    {Py_tp_doc, const_cast<char *>(
        "Scene(hardware)\n"
        "Buffers and textures kept on the software rasterizer or, ``hardware``, on the system's high-performance\n"
        "graphics adapter, and steps drawing with them and onto them; ``run`` runs steps together. Resources and\n"
        "steps are numbers of this scene, and last as long as it does.")},
    {0, nullptr},
};

PyType_Spec scene_spec = {"fastmatpreview.Scene", sizeof(SceneObject), 0, Py_TPFLAGS_DEFAULT, scene_slots};

PyMethodDef methods[] = {
    {"draw", draw_call, METH_O,
     "draw(job) -> texels\n"
     "One indexed draw in a scene of its own. ``job`` is a dict: vertex_shader, pixel_shader (bytes, empty for no\n"
     "pixel shader), hull_shader and domain_shader (bytes, both or neither: for a list of patches alone),\n"
     "geometry_shader (bytes, or left out for none);\n"
     "vertex_buffers\n"
     "[(data, stride) or (data, stride, offset of the first vertex in the data)]; elements [(semantic, index, DXGI\n"
     "format, slot, offset, per instance, instance step rate)],\n"
     "none for a vertex shader reading system values alone;\n"
     "indices, index_format\n"
     "(R16_UINT or R32_UINT); topology (a list or strip of points, lines or triangles, or a list of patches, as\n"
     "D3D_PRIMITIVE_TOPOLOGY numbers it); draw (index count, instance count, first index, base vertex, first\n"
     "instance); size\n"
     "(width, height); viewport (x, y, width, height, min depth, max depth); scissor (left, top, right, bottom);\n"
     "targets [(DXGI format, texels before, or None to clear them to zero)]; depth (DXGI format, texels before,\n"
     "planes in turn, or None to clear depth and stencil to zero) or None;\n"
     "rasterizer, depth_stencil and blend states in Direct3D 12's numbering; tables [(kind, register, space, count\n"
     "or 0 for unbounded, view dimension, shader visibility, entries)]; heap [(kind, view dimension, entry)] for\n"
     "shaders indexing the descriptor heap, each entry's index its heap index, an entry of the index alone a null\n"
     "descriptor; hardware (draw on the system's\n"
     "high-performance graphics adapter instead of the software rasterizer).\n"
     "Returns each target's texels after the draw, then the depth target's, packed tightly."},
    {nullptr, nullptr, 0, nullptr},
};

PyModuleDef module = {PyModuleDef_HEAD_INIT, "fastmatpreview", nullptr, -1, methods};

}  // namespace

PyMODINIT_FUNC PyInit_fastmatpreview(void) {
    PyObject *made = PyModule_Create(&module);
    PyObject *scene = made ? PyType_FromSpec(&scene_spec) : nullptr;
    if (scene && PyModule_AddObject(made, "Scene", scene) == 0) return made;
    Py_XDECREF(scene);
    Py_XDECREF(made);
    return nullptr;
}
