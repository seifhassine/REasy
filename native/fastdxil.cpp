/* One call without the interpreter lock instead of one ctypes call per descriptor. ``reflect`` takes the address of the
 * loaded compiler's ``DxcCreateInstance``. */
#define NOMINMAX
#define PY_SSIZE_T_CLEAN
#define Py_LIMITED_API 0x030B0000
#include <Python.h>
#include <windows.h>
#include <d3d12shader.h>
#include <dxcapi.h>
#include <wrl/client.h>
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <exception>
#include <string>
#include <unordered_map>
#include <vector>

using Microsoft::WRL::ComPtr;

namespace {

// A cyclic type is refused by this limit too.
constexpr int MAX_DEPTH = 32;
constexpr UINT32 DXIL_PART = 'D' | 'X' << 8 | 'I' << 16 | 'L' << 24;
constexpr UINT32 COMPUTE = 5;

// Messages follow ``dxc.DxcError``'s wording.
struct Failure { std::string message; };

void check(HRESULT result, const char *operation) {
    if (FAILED(result)) {
        char text[200];
        std::snprintf(text, sizeof text, "%s failed with HRESULT 0x%08lX", operation,
                      static_cast<unsigned long>(result));
        throw Failure{text};
    }
}

std::string text(LPCSTR value) { return value ? value : ""; }

struct Type {
    D3D12_SHADER_TYPE_DESC desc;
    std::string name;
    std::vector<std::pair<std::string, size_t>> members;
};

struct Variable {
    D3D12_SHADER_VARIABLE_DESC desc;
    std::string name;
    bool has_default;
    std::string default_value;
    size_t type;
};

struct Buffer {
    D3D12_SHADER_BUFFER_DESC desc;
    std::string name;
    std::vector<Variable> variables;
};

struct Resource {
    D3D12_SHADER_INPUT_BIND_DESC desc;
    std::string name;
};

struct Bound {
    std::vector<Resource> resources;
    std::vector<Buffer> buffers;
    std::vector<Type> types;
    std::unordered_map<ID3D12ShaderReflectionType *, std::pair<size_t, int>> seen;
};

size_t walk_type(ID3D12ShaderReflectionType *type, int depth, Bound &bound) {
    if (!type || depth > MAX_DEPTH) throw Failure{"DXIL reflection contains an invalid/nested type"};
    auto found = bound.seen.find(type);
    if (found != bound.seen.end()) {
        if (depth + found->second.second > MAX_DEPTH) throw Failure{"DXIL reflection contains an invalid/nested type"};
        return found->second.first;
    }
    Type made{};
    check(type->GetDesc(&made.desc), "ID3D12ShaderReflectionType::GetDesc");
    made.name = text(made.desc.Name);
    int height = 0;
    for (UINT index = 0; index < made.desc.Members; ++index) {
        ID3D12ShaderReflectionType *member = type->GetMemberTypeByIndex(index);
        if (!member) throw Failure{"DXIL reflection returned a null structure member"};
        std::string name = text(type->GetMemberTypeName(index));
        size_t number = walk_type(member, depth + 1, bound);
        height = std::max(height, 1 + bound.seen[member].second);
        made.members.emplace_back(std::move(name), number);
    }
    made.desc.Name = nullptr;
    bound.types.push_back(std::move(made));
    size_t number = bound.types.size() - 1;
    bound.seen[type] = {number, height};
    return number;
}

template <class Reflection>
Bound walk_bound(Reflection *reflection, UINT resources, UINT buffers) {
    Bound bound;
    for (UINT index = 0; index < resources; ++index) {
        Resource resource{};
        check(reflection->GetResourceBindingDesc(index, &resource.desc),
              "ID3D12ShaderReflection::GetResourceBindingDesc");
        resource.name = text(resource.desc.Name);
        resource.desc.Name = nullptr;
        bound.resources.push_back(std::move(resource));
    }
    for (UINT index = 0; index < buffers; ++index) {
        ID3D12ShaderReflectionConstantBuffer *buffer = reflection->GetConstantBufferByIndex(index);
        if (!buffer) throw Failure{"DXIL reflection returned a null constant buffer"};
        Buffer made{};
        check(buffer->GetDesc(&made.desc), "ID3D12ShaderReflectionConstantBuffer::GetDesc");
        made.name = text(made.desc.Name);
        made.desc.Name = nullptr;
        for (UINT number = 0; number < made.desc.Variables; ++number) {
            ID3D12ShaderReflectionVariable *variable = buffer->GetVariableByIndex(number);
            if (!variable) throw Failure{"DXIL reflection returned a null variable"};
            Variable field{};
            check(variable->GetDesc(&field.desc), "ID3D12ShaderReflectionVariable::GetDesc");
            field.name = text(field.desc.Name);
            field.desc.Name = nullptr;
            field.type = walk_type(variable->GetType(), 0, bound);
            field.has_default = field.desc.DefaultValue && field.desc.Size;
            if (field.has_default)
                field.default_value.assign(static_cast<const char *>(field.desc.DefaultValue), field.desc.Size);
            field.desc.DefaultValue = nullptr;
            made.variables.push_back(std::move(field));
        }
        bound.buffers.push_back(std::move(made));
    }
    return bound;
}

struct Function {
    std::string name;
    UINT version;
    UINT64 required_flags;
    Bound bound;
};

struct Reflected {
    std::string creator;
    UINT version = 0, flags = 0;
    Bound bound;
    bool has_group = false;
    UINT group[3] = {}, group_count = 0;
    std::vector<Function> functions;
};

Reflected reflect_container(DxcCreateInstanceProc create, const uint8_t *data, size_t size, bool library) {
    ComPtr<IDxcLibrary> dxc;
    check(create(CLSID_DxcLibrary, __uuidof(IDxcLibrary), reinterpret_cast<void **>(dxc.GetAddressOf())),
          "DxcCreateInstance");
    ComPtr<IDxcBlobEncoding> blob;
    check(dxc->CreateBlobWithEncodingFromPinned(data, static_cast<UINT32>(size), CP_UTF8, &blob),
          "IDxcLibrary::CreateBlobWithEncodingFromPinned");
    ComPtr<IDxcContainerReflection> container;
    check(create(CLSID_DxcContainerReflection, __uuidof(IDxcContainerReflection),
                 reinterpret_cast<void **>(container.GetAddressOf())), "DxcCreateInstance");
    check(container->Load(blob.Get()), "IDxcContainerReflection::Load");
    UINT32 part = 0;
    check(container->FindFirstPartKind(DXIL_PART, &part), "IDxcContainerReflection::FindFirstPartKind(DXIL)");
    Reflected reflected;
    if (library) {
        ComPtr<ID3D12LibraryReflection> reflection;
        check(container->GetPartReflection(part, IID_PPV_ARGS(&reflection)),
              "IDxcContainerReflection::GetPartReflection");
        D3D12_LIBRARY_DESC desc{};
        check(reflection->GetDesc(&desc), "ID3D12LibraryReflection::GetDesc");
        reflected.creator = text(desc.Creator);
        reflected.flags = desc.Flags;
        for (UINT index = 0; index < desc.FunctionCount; ++index) {
            ID3D12FunctionReflection *function = reflection->GetFunctionByIndex(static_cast<INT>(index));
            if (!function) throw Failure{"DXIL library returned a null function"};
            D3D12_FUNCTION_DESC described{};
            check(function->GetDesc(&described), "ID3D12FunctionReflection::GetDesc");
            reflected.functions.push_back({text(described.Name), described.Version, described.RequiredFeatureFlags,
                                           walk_bound(function, described.BoundResources, described.ConstantBuffers)});
        }
        return reflected;
    }
    ComPtr<ID3D12ShaderReflection> reflection;
    check(container->GetPartReflection(part, IID_PPV_ARGS(&reflection)), "IDxcContainerReflection::GetPartReflection");
    D3D12_SHADER_DESC desc{};
    check(reflection->GetDesc(&desc), "ID3D12ShaderReflection::GetDesc");
    reflected.creator = text(desc.Creator);
    reflected.version = desc.Version;
    reflected.flags = desc.Flags;
    reflected.bound = walk_bound(reflection.Get(), desc.BoundResources, desc.ConstantBuffers);
    if ((desc.Version >> 16 & 0xFFFF) == COMPUTE) {
        reflected.has_group = true;
        reflected.group_count = reflection->GetThreadGroupSize(&reflected.group[0], &reflected.group[1],
                                                               &reflected.group[2]);
    }
    return reflected;
}

PyObject *bytes(const std::string &value) { return PyBytes_FromStringAndSize(value.data(), value.size()); }

// Equal types make equal values. ``types`` must already hold every member's tuple.
PyObject *type_value(const Type &type, const std::vector<PyObject *> &types) {
    PyObject *members = PyTuple_New(static_cast<Py_ssize_t>(type.members.size()));
    if (!members) return nullptr;
    for (size_t number = 0; number < type.members.size(); ++number) {
        PyObject *item = Py_BuildValue("(NO)", bytes(type.members[number].first), types[type.members[number].second]);
        if (!item || PyTuple_SetItem(members, static_cast<Py_ssize_t>(number), item) < 0) {
            Py_DECREF(members);
            return nullptr;
        }
    }
    const D3D12_SHADER_TYPE_DESC &d = type.desc;
    return Py_BuildValue("(IIIIIIINN)", d.Offset, d.Class, d.Type, d.Rows, d.Columns, d.Elements, d.Members,
                         bytes(type.name), members);
}

PyObject *buffer_value(const Buffer &buffer, const std::vector<PyObject *> &types) {
    PyObject *variables = PyTuple_New(static_cast<Py_ssize_t>(buffer.variables.size()));
    if (!variables) return nullptr;
    for (size_t number = 0; number < buffer.variables.size(); ++number) {
        const Variable &variable = buffer.variables[number];
        const D3D12_SHADER_VARIABLE_DESC &d = variable.desc;
        PyObject *value = variable.has_default ? bytes(variable.default_value) : Py_NewRef(Py_None);
        PyObject *item = Py_BuildValue("(NIIINIIIIO)", bytes(variable.name), d.StartOffset, d.Size, d.uFlags, value,
                                       d.StartTexture, d.TextureSize, d.StartSampler, d.SamplerSize,
                                       types[variable.type]);
        if (!item || PyTuple_SetItem(variables, static_cast<Py_ssize_t>(number), item) < 0) {
            Py_DECREF(variables);
            return nullptr;
        }
    }
    const D3D12_SHADER_BUFFER_DESC &d = buffer.desc;
    return Py_BuildValue("(NIIIN)", bytes(buffer.name), d.Type, d.Size, d.uFlags, variables);
}

PyObject *bound_value(const Bound &bound) {
    std::vector<PyObject *> types;
    PyObject *resources = PyTuple_New(static_cast<Py_ssize_t>(bound.resources.size()));
    PyObject *buffers = PyTuple_New(static_cast<Py_ssize_t>(bound.buffers.size()));
    bool made = resources && buffers;
    for (size_t index = 0; made && index < bound.types.size(); ++index) {
        PyObject *item = type_value(bound.types[index], types);
        made = item != nullptr;
        if (made) types.push_back(item);
    }
    for (size_t index = 0; made && index < bound.resources.size(); ++index) {
        const Resource &resource = bound.resources[index];
        const D3D12_SHADER_INPUT_BIND_DESC &d = resource.desc;
        PyObject *item = Py_BuildValue("(NIIIIIIIII)", bytes(resource.name), d.Type, d.ReturnType, d.Dimension,
                                       d.NumSamples, d.BindPoint, d.BindCount, d.uFlags, d.Space, d.uID);
        made = item && PyTuple_SetItem(resources, static_cast<Py_ssize_t>(index), item) == 0;
    }
    for (size_t index = 0; made && index < bound.buffers.size(); ++index) {
        PyObject *item = buffer_value(bound.buffers[index], types);
        made = item && PyTuple_SetItem(buffers, static_cast<Py_ssize_t>(index), item) == 0;
    }
    for (PyObject *type : types) Py_DECREF(type);
    if (made) return Py_BuildValue("(NN)", resources, buffers);
    Py_XDECREF(resources);
    Py_XDECREF(buffers);
    return nullptr;
}

PyObject *reflected_value(const Reflected &reflected, bool library) {
    if (library) {
        PyObject *functions = PyList_New(0);
        bool made = functions != nullptr;
        for (size_t index = 0; made && index < reflected.functions.size(); ++index) {
            const Function &function = reflected.functions[index];
            PyObject *bound = bound_value(function.bound);
            PyObject *item = bound ? Py_BuildValue("(NIKN)", bytes(function.name), function.version,
                                                   static_cast<unsigned long long>(function.required_flags), bound)
                                   : nullptr;
            made = item && PyList_Append(functions, item) == 0;
            Py_XDECREF(item);
        }
        if (!made) {
            Py_XDECREF(functions);
            return nullptr;
        }
        return Py_BuildValue("(NIN)", bytes(reflected.creator), reflected.flags, functions);
    }
    PyObject *bound = bound_value(reflected.bound);
    if (!bound) return nullptr;
    PyObject *group = reflected.has_group ? Py_BuildValue("(IIII)", reflected.group[0], reflected.group[1],
                                                          reflected.group[2], reflected.group_count)
                                          : Py_NewRef(Py_None);
    return Py_BuildValue("(NIINN)", bytes(reflected.creator), reflected.version, reflected.flags, bound, group);
}

PyObject *error = nullptr;

PyObject *reflect_call(PyObject *, PyObject *args) {
    unsigned long long address;
    PyObject *container;
    int library;
    if (!PyArg_ParseTuple(args, "KSp", &address, &container, &library)) return nullptr;
    char *data;
    Py_ssize_t size;
    if (PyBytes_AsStringAndSize(container, &data, &size) < 0) return nullptr;
    if (static_cast<unsigned long long>(size) > UINT32_MAX) {
        PyErr_SetString(error, "DXIL container is too large");
        return nullptr;
    }
    auto create = reinterpret_cast<DxcCreateInstanceProc>(static_cast<uintptr_t>(address));
    Reflected reflected;
    std::string failure;
    bool failed = false;
    // The container's bytes never change and the caller's reference keeps them while the lock is let go.
    Py_BEGIN_ALLOW_THREADS
    try {
        reflected = reflect_container(create, reinterpret_cast<const uint8_t *>(data), static_cast<size_t>(size),
                                      library != 0);
    } catch (const Failure &caught) {
        failure = caught.message;
        failed = true;
    } catch (const std::exception &caught) {
        failure = caught.what();
        failed = true;
    }
    Py_END_ALLOW_THREADS
    if (failed) {
        PyErr_SetString(error, failure.c_str());
        return nullptr;
    }
    return reflected_value(reflected, library != 0);
}

PyMethodDef methods[] = {
    {"reflect", reflect_call, METH_VARARGS,
     "reflect(create_instance, container, library) -> reflection\n"
     "The DXIL part of ``container`` (bytes of a DXBC container) reflected by the compiler whose DxcCreateInstance\n"
     "is at the address ``create_instance``. Bound data is (resources, buffers):\n"
     "resources [(name, type, return type, dimension, samples, bind point, bind count, flags, space, id)];\n"
     "buffers [(name, type, size, flags, variables)], variables [(name, start offset, size, flags, default value\n"
     "or None, start texture, texture size, start sampler, sampler size, type)];\n"
     "a type is (offset, class, type, rows, columns, elements, member count, name, members [(name, type)]),\n"
     "a type reflected once being one tuple.\n"
     "A shader's (``library`` false) is (creator, version, flags, bound data, thread group or None), a compute\n"
     "shader's thread group (x, y, z, the count reflection gave with it). A library's is (creator, flags,\n"
     "functions [(name, version, required feature flags, bound data)]).\n"
     "Raises fastdxil.Error, worded as dxc.DxcError, when the compiler fails."},
    {nullptr, nullptr, 0, nullptr},
};

PyModuleDef module = {PyModuleDef_HEAD_INIT, "fastdxil", nullptr, -1, methods};

}  // namespace

PyMODINIT_FUNC PyInit_fastdxil(void) {
    PyObject *made = PyModule_Create(&module);
    if (!made) return nullptr;
    error = PyErr_NewException("fastdxil.Error", nullptr, nullptr);
    if (error && PyModule_AddObject(made, "Error", Py_NewRef(error)) == 0) return made;
    Py_XDECREF(error);
    Py_DECREF(made);
    return nullptr;
}
