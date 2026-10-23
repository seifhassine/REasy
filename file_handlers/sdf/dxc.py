from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, replace
from functools import lru_cache
import hashlib
import os
from pathlib import Path
import re
import sys
from threading import RLock
import uuid

lazy import fastdxil
from file_handlers.bounded_cache import BoundedCache
from tools.shader_tools import PAGES, prompt_shader_tool_download
from utils.app_paths import downloads_directory

if sys.platform == "win32":
    import winreg
else:  # pragma: no cover - DXC COM is Windows-only.
    winreg = None

lazy from .com import blob_bytes, com_method, com_release
lazy from .dxbc import (
    CONSTANT_BUFFER_TYPE_NAMES,
    RESOURCE_DIMENSION_NAMES,
    RESOURCE_INPUT_TYPE_NAMES,
    RESOURCE_RETURN_TYPE_NAMES,
    SHADER_STAGE_NAMES,
    VARIABLE_CLASS_NAMES,
    VARIABLE_TYPE_NAMES,
    DxbcConstantBuffer,
    DxbcLibraryFunction,
    DxbcReflection,
    DxbcResourceBinding,
    DxbcShader,
    DxbcType,
    DxbcTypeMember,
    DxbcVariable,
)
lazy from .hlsl import thread_group


class DxcError(ValueError):
    pass


class DxcNotFound(DxcError):
    pass


@dataclass(frozen=True, slots=True)
class DxcCompileResult:
    bytecode: bytes
    diagnostics: str = ""
    binary_format: str = "dxbc-container"


HRESULT = ctypes.c_long
UINT32 = ctypes.c_uint32
LPVOID = ctypes.c_void_p


class GUID(ctypes.Structure):
    _fields_ = (
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    )

    @classmethod
    def parse(cls, value: str) -> "GUID":
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


CLSID_DXC_LIBRARY = GUID.parse("6245d6af-66e0-48fd-80b4-4d271796748c")
IID_IDXC_LIBRARY = GUID.parse("e5204dc7-d18c-4c3c-bdfb-851673980fe7")
CLSID_DXC_COMPILER = GUID.parse("73e22d93-e6ce-47f3-b5bf-f0664f39c1b0")
IID_IDXC_COMPILER = GUID.parse("8c210bf3-011f-4422-8d70-6f9acb8db617")
CLSID_DXC_ASSEMBLER = GUID.parse("d728db68-f903-4f80-94cd-dccf76ec7151")
IID_IDXC_ASSEMBLER = GUID.parse("091f7a26-1c1f-4948-904b-e6e3a8a771d5")
CLSID_DXC_VALIDATOR = GUID.parse("8ca3e215-f728-4cf3-8cdd-88af917587a1")
IID_IDXC_VALIDATOR = GUID.parse("a6e82bd2-1fd7-4826-9811-2857e797f49a")
CLSID_DXC_OPTIMIZER = GUID.parse("ae2cd79f-cc22-453f-9b6b-b124e7a5204c")
IID_IDXC_OPTIMIZER = GUID.parse("25740e2e-9cba-401b-9119-4fb42f39f270")
CLSID_DXC_CONTAINER_REFLECTION = GUID.parse(
    "b9f54489-55b8-400c-ba3a-1675e4728b91"
)
IID_IDXC_CONTAINER_REFLECTION = GUID.parse(
    "d2c21b26-8350-4bdc-976a-331ce6f4c54c"
)

DXC_CP_UTF8 = 65001


def _check(result: int, operation: str) -> None:
    if int(result) < 0:
        raise DxcError(
            f"{operation} failed with HRESULT 0x{int(result) & 0xFFFFFFFF:08X}"
        )


def _operation_result(operation: LPVOID, failure_message: str, *,
                      hresult: bool = False, strip_nul: bool = False) -> tuple[bytes, str]:
    result_blob = error_blob = None
    try:
        status = HRESULT()
        _check(com_method(operation, 3, HRESULT, ctypes.POINTER(HRESULT))(
            operation, ctypes.byref(status),
        ), "IDxcOperationResult::GetStatus")
        error_blob = LPVOID()
        _check(com_method(operation, 5, HRESULT, ctypes.POINTER(LPVOID))(
            operation, ctypes.byref(error_blob),
        ), "IDxcOperationResult::GetErrorBuffer")
        errors = blob_bytes(error_blob)
        if strip_nul:
            errors = errors.rstrip(b"\0")
        diagnostics = errors.decode("utf-8", "replace").strip()
        if int(status.value) < 0:
            if hresult:
                failure_message += f" with HRESULT 0x{int(status.value) & 0xFFFFFFFF:08X}"
            raise DxcError(diagnostics or failure_message)
        result_blob = LPVOID()
        _check(com_method(operation, 4, HRESULT, ctypes.POINTER(LPVOID))(
            operation, ctypes.byref(result_blob),
        ), "IDxcOperationResult::GetResult")
        return blob_bytes(result_blob), diagnostics
    finally:
        com_release(error_blob)
        com_release(result_blob)


def _version_key(path: Path) -> tuple[int, ...]:
    values = tuple(int(item) for item in re.findall(r"\d+", path.name))
    return values or (0,)


def _renderdoc_dxcompiler() -> Path | None:
    if sys.platform != "win32" or winreg is None:
        return None
    for root, key_name in (
        (winreg.HKEY_CLASSES_ROOT, r"RenderDoc.RDCCapture.1\shell\open\command"),
        (winreg.HKEY_CLASSES_ROOT, r"RenderDoc.RDCCapture\shell\open\command"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Classes\RenderDoc.RDCCapture.1\shell\open\command"),
    ):
        try:
            with winreg.OpenKey(
                root,
                key_name,
                0,
                winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            ) as key:
                command = str(winreg.QueryValueEx(key, "")[0] or "")
        except OSError:
            continue
        command = os.path.expandvars(command)
        executable = re.match(r'\s*"(?P<path>[^"]+)"', command)
        if executable is None:
            continue
        candidate = Path(executable.group("path")).parent / "plugins" / "d3d12" / "dxcompiler.dll"
        if candidate.is_file():
            return candidate.resolve()
    return None


@lru_cache(maxsize=2)
def find_dxcompiler(spirv: bool = False) -> Path:
    if sys.platform != "win32":
        raise DxcNotFound("DirectX Shader Compiler is available only on Windows")
    candidates: list[Path] = []
    configured = os.environ.get(
        "REASY_SPIRV_DXCOMPILER_PATH" if spirv else "REASY_DXCOMPILER_PATH",
        "",
    ).strip()
    if configured:
        candidates.append(Path(configured))
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys._MEIPASS) / "dxcompiler.dll")
    downloaded = downloads_directory("dxc")
    if (downloaded / "dxil.dll").is_file():
        candidates.append(downloaded / "dxcompiler.dll")
    if spirv and (renderdoc := _renderdoc_dxcompiler()) is not None:
        candidates.append(renderdoc)
    program_files = os.environ.get("ProgramFiles(x86)", "").strip()
    if program_files:
        kits = Path(program_files) / "Windows Kits" / "10"
        candidates.append(kits / "Redist" / "D3D" / "x64" / "dxcompiler.dll")
        binary_root = kits / "bin"
        if binary_root.is_dir():
            versions = sorted(
                (item for item in binary_root.iterdir() if item.is_dir()),
                key=_version_key,
                reverse=True,
            )
            candidates.extend(
                item / "x64" / "dxcompiler.dll" for item in versions
            )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise DxcNotFound(
        "Microsoft DirectX Shader Compiler was not found. Open the file in REasy to download it, or get it from "
        f"{PAGES['dxc']} and put both DLLs in {downloaded}."
    )


@lru_cache(maxsize=2)
def _library(spirv: bool = False):
    try:
        path = find_dxcompiler(spirv)
    except DxcError:
        if not prompt_shader_tool_download("dxc"):
            raise
        path = find_dxcompiler(spirv)
    before = _runtime_file_state(path)
    try:
        library = ctypes.WinDLL(str(path))
    except OSError as exc:
        raise DxcError(f"could not load {path}: {exc}") from exc
    if before is not None and before == _runtime_file_state(path):
        library._reasy_runtime_file = str(path), before
    return library


def _runtime_file_state(path):
    try:
        value = Path(path).stat()
    except OSError:
        return None
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _runtime_identity(library):
    attributes = getattr(library, "__dict__", {})
    recorded = attributes.get("_reasy_runtime_file")
    if recorded is None:
        return None
    path, state = recorded
    if state != _runtime_file_state(path):
        return None
    identity = attributes.get("_reasy_runtime_identity")
    if identity is not None:
        return identity
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    if state != _runtime_file_state(path):
        return None
    identity = str(Path(path).resolve()), digest.hexdigest()
    library._reasy_runtime_identity = identity
    return identity


def dxc_available(*, spirv: bool = False) -> bool:
    try:
        find_dxcompiler(spirv)
        _library(spirv)
    except DxcError:
        return False
    return True


def _create(clsid: GUID, iid: GUID, *, spirv: bool = False) -> LPVOID:
    create = _library(spirv).DxcCreateInstance
    create.argtypes = (
        ctypes.POINTER(GUID),
        ctypes.POINTER(GUID),
        ctypes.POINTER(LPVOID),
    )
    create.restype = HRESULT
    result = LPVOID()
    _check(
        create(ctypes.byref(clsid), ctypes.byref(iid), ctypes.byref(result)),
        "DxcCreateInstance",
    )
    return result


def _source_blob(raw: bytes, library: LPVOID) -> tuple[LPVOID, object]:
    pinned = ctypes.create_string_buffer(raw)
    blob = LPVOID()
    create_pinned = com_method(
        library,
        6,
        HRESULT,
        LPVOID,
        UINT32,
        UINT32,
        ctypes.POINTER(LPVOID),
    )
    _check(
        create_pinned(
            library,
            ctypes.cast(pinned, LPVOID),
            len(raw),
            DXC_CP_UTF8,
            ctypes.byref(blob),
        ),
        "IDxcLibrary::CreateBlobWithEncodingFromPinned",
    )
    return blob, pinned


_DXC_TARGET = re.compile(
    r"^(?:vs|hs|ds|gs|ps|cs|ms|as|lib)_6_[0-9]+$",
    re.IGNORECASE,
)


def compile_hlsl_dxc(
    source: str | bytes,
    *,
    entry_point: str,
    target: str,
    source_name: str = "REasyShaderGraph.hlsl",
    spirv: bool = False,
    extra_arguments: tuple[str, ...] = (),
) -> DxcCompileResult:
    raw = source.encode("utf-8") if isinstance(source, str) else bytes(source)
    if not raw.strip() or b"\0" in raw:
        raise DxcError("HLSL source is empty or contains an embedded NUL")
    entry = str(entry_point).strip()
    profile = str(target).strip().lower()
    if (not entry and not profile.startswith("lib_")) or not _DXC_TARGET.fullmatch(profile):
        raise DxcError(f"unsupported DXC target {target!r}")
    arguments = ["-Ges", "-O3", "-WX", "-Qstrip_debug"]
    if spirv:
        arguments.extend(("-spirv", "-fspv-target-env=vulkan1.1", "-fvk-use-dx-layout"))
    arguments.extend(str(item) for item in extra_arguments)
    argument_array = (wintypes.LPCWSTR * len(arguments))(*arguments)

    library = compiler = source_blob = operation = None
    pinned = None
    try:
        library = _create(CLSID_DXC_LIBRARY, IID_IDXC_LIBRARY, spirv=spirv)
        compiler = _create(CLSID_DXC_COMPILER, IID_IDXC_COMPILER, spirv=spirv)
        source_blob, pinned = _source_blob(raw, library)
        operation = LPVOID()
        compile_method = com_method(
            compiler,
            3,
            HRESULT,
            LPVOID,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            ctypes.POINTER(wintypes.LPCWSTR),
            UINT32,
            LPVOID,
            UINT32,
            LPVOID,
            ctypes.POINTER(LPVOID),
        )
        _check(
            compile_method(
                compiler,
                source_blob,
                str(source_name),
                entry,
                profile,
                argument_array,
                len(arguments),
                None,
                0,
                None,
                ctypes.byref(operation),
            ),
            "IDxcCompiler::Compile",
        )
        bytecode, diagnostics = _operation_result(operation, "DXC compilation failed", hresult=True)
        if not bytecode:
            raise DxcError("DXC returned an empty shader")
        if spirv and bytecode[:4] != b"\x03\x02\x23\x07":
            raise DxcError("DXC did not return a SPIR-V module")
        if not spirv and bytecode[:4] != b"DXBC":
            raise DxcError("DXC did not return a DXIL container")
        return DxcCompileResult(
            bytecode,
            diagnostics,
            "spirv-module" if spirv else "dxbc-container",
        )
    finally:
        _ = pinned
        com_release(operation)
        com_release(source_blob)
        com_release(compiler)
        com_release(library)


def disassemble_dxil(container: bytes) -> str:
    raw = bytes(container)
    if raw[:4] not in {b"DXBC", b"BC\xc0\xde"}:
        raise DxcError("DXIL disassembly needs a DXBC container or LLVM bitcode")
    library = compiler = source_blob = disassembly_blob = None
    pinned = None
    try:
        library = _create(CLSID_DXC_LIBRARY, IID_IDXC_LIBRARY)
        compiler = _create(CLSID_DXC_COMPILER, IID_IDXC_COMPILER)
        source_blob, pinned = _source_blob(raw, library)
        disassembly_blob = LPVOID()
        _check(
            com_method(
                compiler,
                5,
                HRESULT,
                LPVOID,
                ctypes.POINTER(LPVOID),
            )(
                compiler,
                source_blob,
                ctypes.byref(disassembly_blob),
            ),
            "IDxcCompiler::Disassemble",
        )
        result = blob_bytes(disassembly_blob)
        if not result:
            raise DxcError("DXC returned an empty DXIL disassembly")
        try:
            # IDxcBlobEncoding's byte length often includes the C-string terminator.
            return result.rstrip(b"\0").decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DxcError("DXC returned non-UTF-8 DXIL assembly") from exc
    finally:
        _ = pinned
        com_release(disassembly_blob)
        com_release(source_blob)
        com_release(compiler)
        com_release(library)


def assemble_dxil(assembly: str | bytes) -> DxcCompileResult:
    raw = assembly.encode("utf-8") if isinstance(assembly, str) else bytes(assembly)
    if not raw.strip() or (b"\0" in raw and not raw.startswith(b"BC\xc0\xde")):
        raise DxcError("DXIL assembly is empty or contains an embedded NUL")
    library = assembler = source_blob = operation = None
    pinned = None
    try:
        library = _create(CLSID_DXC_LIBRARY, IID_IDXC_LIBRARY)
        assembler = _create(CLSID_DXC_ASSEMBLER, IID_IDXC_ASSEMBLER)
        source_blob, pinned = _source_blob(raw, library)
        operation = LPVOID()
        _check(
            com_method(
                assembler,
                3,
                HRESULT,
                LPVOID,
                ctypes.POINTER(LPVOID),
            )(
                assembler,
                source_blob,
                ctypes.byref(operation),
            ),
            "IDxcAssembler::AssembleToContainer",
        )
        bytecode, diagnostics = _operation_result(operation, "DXIL assembly failed", hresult=True)
        if bytecode[:4] != b"DXBC":
            raise DxcError("DXC assembler did not return a DXIL container")
        return DxcCompileResult(bytecode, diagnostics)
    finally:
        _ = pinned
        com_release(operation)
        com_release(source_blob)
        com_release(assembler)
        com_release(library)


def optimize_dxil(module: str | bytes, options: tuple[str, ...]) -> bytes:
    raw = module.encode("utf-8") if isinstance(module, str) else bytes(module)
    library = optimizer = source = result = output = None
    pinned = None
    try:
        library = _create(CLSID_DXC_LIBRARY, IID_IDXC_LIBRARY)
        optimizer = _create(CLSID_DXC_OPTIMIZER, IID_IDXC_OPTIMIZER)
        source, pinned = _source_blob(raw, library)
        arguments = (ctypes.c_wchar_p * len(options))(*options)
        result, output = LPVOID(), LPVOID()
        status = com_method(
            optimizer, 5, HRESULT, LPVOID, ctypes.POINTER(ctypes.c_wchar_p),
            UINT32, ctypes.POINTER(LPVOID), ctypes.POINTER(LPVOID),
        )(optimizer, source, arguments, len(options), ctypes.byref(result), ctypes.byref(output))
        if int(status) < 0:
            diagnostics = blob_bytes(output).rstrip(b"\0").decode("utf-8", "replace").strip()
            if diagnostics:
                raise DxcError(diagnostics)
            _check(status, "IDxcOptimizer::RunOptimizer")
        bitcode = blob_bytes(result)
        if not bitcode.startswith(b"BC\xc0\xde"):
            raise DxcError("DXC optimizer did not return LLVM bitcode")
        return bitcode
    finally:
        _ = pinned
        for value in (output, result, source, optimizer, library):
            com_release(value)


def validate_dxil(container: bytes) -> bytes:
    library = validator = source = operation = None
    pinned = None
    try:
        library = _create(CLSID_DXC_LIBRARY, IID_IDXC_LIBRARY)
        validator = _create(CLSID_DXC_VALIDATOR, IID_IDXC_VALIDATOR)
        source, pinned = _source_blob(bytes(container), library)
        operation = LPVOID()
        _check(com_method(validator, 3, HRESULT, LPVOID, UINT32, ctypes.POINTER(LPVOID))(
            validator, source, 0, ctypes.byref(operation),
        ), "IDxcValidator::Validate")
        result, _diagnostics = _operation_result(operation, "DXIL runtime validation failed", strip_nul=True)
        return result or bytes(container)
    finally:
        _ = pinned
        for value in (operation, source, validator, library):
            com_release(value)


def dxil_container_part(container: bytes, fourcc: str | bytes) -> bytes:
    raw = bytes(container)
    tag = fourcc.encode("ascii") if isinstance(fourcc, str) else bytes(fourcc)
    if raw[:4] != b"DXBC" or len(tag) != 4:
        raise DxcError("DXIL container parts need a DXBC container and four-byte tag")
    library = reflection_api = source_blob = pinned = None
    part_blob = LPVOID()
    try:
        library = _create(CLSID_DXC_LIBRARY, IID_IDXC_LIBRARY)
        source_blob, pinned = _source_blob(raw, library)
        reflection_api = _create(
            CLSID_DXC_CONTAINER_REFLECTION,
            IID_IDXC_CONTAINER_REFLECTION,
        )
        _check(
            com_method(reflection_api, 3, HRESULT, LPVOID)(
                reflection_api,
                source_blob,
            ),
            "IDxcContainerReflection::Load",
        )
        part_index = UINT32()
        _check(
            com_method(reflection_api, 7, HRESULT, UINT32, ctypes.POINTER(UINT32))(
                reflection_api,
                int.from_bytes(tag, "little"),
                ctypes.byref(part_index),
            ),
            f"IDxcContainerReflection::FindFirstPartKind({tag.decode('ascii')})",
        )
        _check(
            com_method(
                reflection_api,
                6,
                HRESULT,
                UINT32,
                ctypes.POINTER(LPVOID),
            )(
                reflection_api,
                part_index.value,
                ctypes.byref(part_blob),
            ),
            "IDxcContainerReflection::GetPartContent",
        )
        result = blob_bytes(part_blob)
    finally:
        _ = pinned
        for value in (part_blob, reflection_api, source_blob, library):
            com_release(value)
    if not result:
        raise DxcError(f"DXIL container part {tag.decode('ascii')} is empty")
    return result


def _text(value: bytes | None) -> str:
    return value.decode("utf-8", "replace") if value else ""


# Cached per distinct value: a game's shaders bind the same few hundred resources thousands of times.
_RECORD_CACHE_LIMIT = 16384


@lru_cache(maxsize=_RECORD_CACHE_LIMIT)
def _type_record(found) -> DxbcType:
    offset, class_id, type_id, rows, columns, elements, member_count, name, found_members = found
    members = [(_text(member_name), _type_record(member)) for member_name, member in found_members]
    return DxbcType(
        offset=offset,
        variable_class_id=class_id,
        variable_class=VARIABLE_CLASS_NAMES.get(class_id, f"class-{class_id}"),
        variable_type_id=type_id,
        variable_type=VARIABLE_TYPE_NAMES.get(type_id, f"type-{type_id}"),
        rows=rows,
        columns=columns,
        element_count=elements,
        member_count=member_count,
        members_offset=0,
        name_offset=0,
        name=_text(name),
        reserved_data=(),
        extended_data=(),
        members=tuple(
            DxbcTypeMember(
                name_offset=0, name=member_name, type_offset=0, byte_offset=member.offset, variable_type=member,
                extended_data=(),
            )
            for member_name, member in members
        ),
    )


@lru_cache(maxsize=_RECORD_CACHE_LIMIT)
def _resource_record(found) -> DxbcResourceBinding:
    name, input_type, return_type, dimension, samples, bind_point, bind_count, flags, space, resource_id = found
    return DxbcResourceBinding(
        name=_text(name),
        input_type_id=input_type,
        input_type=RESOURCE_INPUT_TYPE_NAMES.get(input_type, f"input-{input_type}"),
        return_type_id=return_type,
        return_type=RESOURCE_RETURN_TYPE_NAMES.get(return_type, f"return-{return_type}"),
        dimension_id=dimension,
        dimension=RESOURCE_DIMENSION_NAMES.get(dimension, f"dimension-{dimension}"),
        sample_count=samples,
        bind_point=bind_point,
        bind_count=bind_count,
        flags=flags,
        register_space=space,
        resource_id=resource_id,
        extended_data=(),
    )


@lru_cache(maxsize=_RECORD_CACHE_LIMIT)
def _buffer_record(found) -> DxbcConstantBuffer:
    name, buffer_type, byte_size, flags, found_variables = found
    variables = tuple(
        DxbcVariable(
            name=_text(variable_name),
            start_offset=start_offset,
            size=size,
            flags=variable_flags,
            type_offset=0,
            variable_type=_type_record(found_type),
            default_value_offset=0,
            default_value=default,
            start_texture=start_texture,
            texture_size=texture_size,
            start_sampler=start_sampler,
            sampler_size=sampler_size,
            extended_data=(),
        )
        for (variable_name, start_offset, size, variable_flags, default, start_texture, texture_size, start_sampler,
             sampler_size, found_type) in found_variables
    )
    return DxbcConstantBuffer(
        name=_text(name),
        variable_count=len(variables),
        byte_size=byte_size,
        flags=flags,
        buffer_type_id=buffer_type,
        buffer_type=CONSTANT_BUFFER_TYPE_NAMES.get(buffer_type, f"buffer-{buffer_type}"),
        variables=variables,
        extended_data=(),
    )


def _bound_data(bound) -> tuple[
    tuple[DxbcResourceBinding, ...], tuple[DxbcConstantBuffer, ...], tuple[DxbcType, ...],
]:
    found_resources, found_buffers = bound
    buffers = tuple(map(_buffer_record, found_buffers))
    return (tuple(map(_resource_record, found_resources)), buffers,
            tuple(variable.variable_type for buffer in buffers for variable in buffer.variables))


_symbol_name_lock = RLock()  # DbgHelp's symbol APIs are process-global and single-threaded.


@lru_cache(maxsize=256)
def _library_export_name(name: str) -> str:
    # LLVM prefixes verbatim symbols with SOH.
    symbol = name.removeprefix("\x01")
    if not symbol.startswith("?"):
        return symbol
    api = ctypes.WinDLL("dbghelp.dll").UnDecorateSymbolName
    api.argtypes = (ctypes.c_char_p, LPVOID, UINT32, UINT32)
    api.restype = UINT32
    result = ctypes.create_string_buffer(4096)
    with _symbol_name_lock:
        if not api(symbol.encode("utf-8"), result, len(result), 0x1000):
            raise DxcError(f"Cannot resolve library export {name!r}")
    return result.value.decode("utf-8")


def _reflect_library(reflected, shader: DxbcShader) -> DxbcReflection:
    creator, flags, found_functions = reflected
    functions = []
    resources, buffers, types = {}, {}, {}
    for name, version, _flags, bound in found_functions:
        function_resources, function_buffers, function_types = _bound_data(bound)
        functions.append(DxbcLibraryFunction(
            _library_export_name(_text(name)), SHADER_STAGE_NAMES.get(version >> 16, "unknown"),
            function_resources, function_buffers, _text(name)))
        for resource in function_resources:
            key = (resource.name, resource.input_type, resource.bind_point, resource.register_space)
            if key in resources and resources[key] != resource:
                raise DxcError(f"DXIL library functions disagree about resource {resource.name}")
            resources[key] = resource
        for buffer in function_buffers:
            previous = buffers.get(buffer.name)
            if previous is not None:
                if len(previous.variables) != len(buffer.variables):
                    raise DxcError(f"DXIL library functions disagree about buffer {buffer.name}")
                variables = []
                for old, new in zip(previous.variables, buffer.variables, strict=True):
                    if replace(old, flags=new.flags) != new:
                        raise DxcError(f"DXIL library functions disagree about field {new.name}")
                    variables.append(replace(new, flags=old.flags | new.flags))
                buffer = replace(buffer, variables=tuple(variables))
                if replace(previous, variables=buffer.variables) != buffer:
                    raise DxcError(f"DXIL library functions disagree about buffer {buffer.name}")
            buffers[buffer.name] = buffer
        for value in function_types:
            types[value] = value
    return DxbcReflection(
        shader_type_id=6, shader_type="library", major_version=shader.model_major,
        minor_version=shader.model_minor, flags=flags, creator=_text(creator),
        descriptor_tag=0, descriptor_abi="d3d12-library-reflection",
        header_size=0, constant_buffer_stride=0, resource_stride=0, variable_stride=0,
        type_stride=0, type_member_stride=0, header_reserved=0,
        constant_buffers=tuple(buffers.values()), resources=tuple(resources.values()),
        types=tuple(types.values()), library_functions=tuple(functions),
    )


def _reflect_container(container: bytes, *, library_shader: DxbcShader | None = None) -> DxbcReflection:
    raw = bytes(container)
    if raw[:4] != b"DXBC":
        raise DxcError("DXIL reflection needs a DXBC/DXIL container")
    try:
        reflected = fastdxil.reflect(
            ctypes.cast(_library().DxcCreateInstance, LPVOID).value, raw, library_shader is not None,
        )
    except fastdxil.Error as exc:
        raise DxcError(str(exc)) from None
    if library_shader is not None:
        return _reflect_library(reflected, library_shader)
    creator, version, flags, bound, group = reflected
    resources, buffers, types = _bound_data(bound)
    shader_type = (version >> 16) & 0xFFFF
    return DxbcReflection(
        shader_type_id=shader_type,
        shader_type=SHADER_STAGE_NAMES.get(shader_type, f"stage-{shader_type}"),
        major_version=(version >> 4) & 0xF,
        minor_version=version & 0xF,
        flags=flags,
        creator=_text(creator),
        descriptor_tag=0,
        descriptor_abi="d3d12-reflection",
        header_size=0,
        constant_buffer_stride=0,
        resource_stride=0,
        variable_stride=0,
        type_stride=0,
        type_member_stride=0,
        header_reserved=0,
        constant_buffers=buffers,
        resources=resources,
        types=types,
        thread_group_size=None if group is None else thread_group(group[:3], group[3]),
    )


_REFLECTION_CACHE_LIMIT = 4096
_reflection_cache = BoundedCache()


def _cached_container_reflection(shader: DxbcShader, container: bytes) -> DxbcReflection:
    key = str(shader.sha256)
    reflected = _reflection_cache.get(key)
    if reflected is None:
        reflected = _reflect_container(container, library_shader=shader if shader.stage == "library" else None)
        # Each reflection costs one, so the byte limit is the entry limit.
        _reflection_cache.put(key, reflected, 1, _REFLECTION_CACHE_LIMIT, _REFLECTION_CACHE_LIMIT)
    return reflected


def enrich_dxil_shader(shader: DxbcShader, container: bytes) -> DxbcShader:
    if shader.reflection is not None:
        return shader
    if not any(chunk.tag == "DXIL" for chunk in shader.chunks):
        return shader
    reflection = _cached_container_reflection(shader, container)
    if reflection.shader_type != shader.stage or (
        reflection.major_version,
        reflection.minor_version,
    ) != (shader.model_major, shader.model_minor):
        raise DxcError("DXIL header and native shader reflection disagree")
    return replace(shader, reflection=reflection)


__all__ = [
    "DxcCompileResult",
    "DxcError",
    "DxcNotFound",
    "assemble_dxil",
    "compile_hlsl_dxc",
    "disassemble_dxil",
    "dxc_available",
    "dxil_container_part",
    "enrich_dxil_shader",
    "find_dxcompiler",
    "optimize_dxil",
    "validate_dxil",
]
