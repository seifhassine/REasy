from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
import sys
from uuid import UUID

lazy from .com import blob_bytes, com_method, com_release


class HlslCompileError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class HlslCompileResult:
    bytecode: bytes
    diagnostics: str = ""


_COMPILER_NAME = "d3dcompiler_47.dll"


def _compiler():
    if sys.platform != "win32":
        raise HlslCompileError("Direct3D HLSL compilation is available only on Windows")
    try:
        return ctypes.WinDLL(_COMPILER_NAME)
    except OSError as error:
        raise HlslCompileError(f"{_COMPILER_NAME} was not found: {error}") from error


def hlsl_compiler_available() -> bool:
    try:
        _compiler()
    except HlslCompileError:
        return False
    return True


def reflection_thread_group_size(pointer: ctypes.c_void_p) -> tuple[int, int, int]:
    dimensions = (wintypes.UINT(), wintypes.UINT(), wintypes.UINT())
    # ID3D11ShaderReflection::GetThreadGroupSize
    query = com_method(pointer, 20, wintypes.UINT, *(ctypes.POINTER(wintypes.UINT),) * 3)
    count = query(pointer, *(ctypes.byref(n) for n in dimensions))
    return thread_group(tuple(int(n.value) for n in dimensions), count)


def thread_group(group: tuple[int, int, int], count: int) -> tuple[int, int, int]:
    if not all(group) or count != group[0] * group[1] * group[2]:
        raise HlslCompileError("Native shader reflection has no valid compute thread group")
    return tuple(group)


def dxbc_thread_group_size(container: bytes) -> tuple[int, int, int]:
    reflect = _compiler().D3DReflect
    reflect.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
    reflect.restype = ctypes.c_long
    raw = ctypes.create_string_buffer(bytes(container))
    iid = ctypes.create_string_buffer(UUID("8d536ca1-0cca-4956-a837-786963755584").bytes_le)
    pointer = ctypes.c_void_p()
    try:
        status = reflect(raw, len(container), iid, ctypes.byref(pointer))
        if status < 0 or not pointer.value:
            raise HlslCompileError(f"D3DReflect failed with HRESULT 0x{status & 0xffffffff:08X}")
        return reflection_thread_group_size(pointer)
    finally:
        com_release(pointer)


_TARGETS = frozenset(
    {
        f"{stage}_5_{minor}"
        for stage in ("vs", "hs", "ds", "gs", "ps", "cs")
        for minor in (0, 1)
    }
)


def _compile_arguments(
    entry_point: str,
    target: str,
    *,
    debug: bool,
    warnings_as_errors: bool,
    ieee_strict: bool = False,
    optimize: bool = True,
) -> tuple[bytes, bytes, int]:
    entry = str(entry_point).strip()
    profile = str(target).strip().lower()
    if not entry:
        raise HlslCompileError("HLSL entry point is empty")
    if profile not in _TARGETS:
        raise HlslCompileError(f"unsupported Shader Model 5 target {target!r}")
    try:
        entry_bytes = entry.encode("ascii")
    except UnicodeEncodeError as exc:
        raise HlslCompileError("HLSL entry point must be ASCII") from exc
    # Strictness + O3; debug builds keep symbols and disable optimization.
    flags = (1 << 11) | ((1 << 0) | (1 << 2) if debug else (1 << 15))
    if warnings_as_errors:
        flags |= 1 << 18
    if ieee_strict:
        flags |= 1 << 13
    if not optimize:
        flags = (flags & ~((1 << 14) | (1 << 15))) | (1 << 2)
    return entry_bytes, profile.encode("ascii"), flags


def _finish_compile(
    result: int,
    code: ctypes.c_void_p,
    errors: ctypes.c_void_p,
    operation: str,
) -> HlslCompileResult:
    diagnostics = blob_bytes(errors).decode("utf-8", "replace").strip()
    if result < 0 or not code.value:
        raise HlslCompileError(
            diagnostics
            or f"{operation} failed with HRESULT 0x{result & 0xFFFFFFFF:08X}"
        )
    return HlslCompileResult(blob_bytes(code), diagnostics)


_COMPILE_TAIL_ARGTYPES = (
    wintypes.LPCSTR, wintypes.LPCSTR, wintypes.UINT, wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
)


def _call_compiler(name: str, head_types: tuple, head_args: tuple, entry: bytes, profile: bytes,
                   flags: int) -> HlslCompileResult:
    function = getattr(_compiler(), name)
    function.argtypes = (*head_types, *_COMPILE_TAIL_ARGTYPES)
    function.restype = ctypes.c_long
    code, errors = ctypes.c_void_p(), ctypes.c_void_p()
    try:
        result = function(*head_args, entry, profile, flags, 0, ctypes.byref(code), ctypes.byref(errors))
        return _finish_compile(result, code, errors, name)
    finally:
        com_release(errors)
        com_release(code)


def compile_hlsl_source_detailed(
    source: str | bytes,
    *,
    entry_point: str,
    target: str,
    warnings_as_errors: bool = False,
    ieee_strict: bool = False,
    optimize: bool = True,
) -> HlslCompileResult:
    if isinstance(source, str):
        raw = source.encode("utf-8")
    elif isinstance(source, (bytes, bytearray, memoryview)):
        raw = bytes(source)
    else:
        raise HlslCompileError("HLSL source must be text or bytes")
    if not raw.strip():
        raise HlslCompileError("HLSL source is empty")
    if b"\x00" in raw:
        raise HlslCompileError("HLSL source contains an embedded NUL byte")
    entry, profile, flags = _compile_arguments(
        entry_point,
        target,
        debug=False,
        warnings_as_errors=warnings_as_errors,
        ieee_strict=ieee_strict,
        optimize=optimize,
    )

    source_buffer = ctypes.create_string_buffer(raw)
    return _call_compiler(
        "D3DCompile",
        (ctypes.c_void_p, ctypes.c_size_t, wintypes.LPCSTR, ctypes.c_void_p, ctypes.c_void_p),
        (ctypes.cast(source_buffer, ctypes.c_void_p), len(raw), b"REasyShaderGraph.hlsl", None, None),
        entry, profile, flags,
    )


def compile_hlsl_source(source: str | bytes, **options) -> bytes:
    return compile_hlsl_source_detailed(source, **options).bytecode


def compile_hlsl_file_detailed(
    path: str | Path,
    *,
    entry_point: str,
    target: str,
    debug: bool = False,
    warnings_as_errors: bool = False,
) -> HlslCompileResult:
    source = Path(path)
    if not source.is_file():
        raise HlslCompileError(f"HLSL source does not exist: {source}")
    entry_bytes, profile_bytes, flags = _compile_arguments(
        entry_point,
        target,
        debug=debug,
        warnings_as_errors=warnings_as_errors,
    )

    return _call_compiler(
        "D3DCompileFromFile",
        (wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p),
        # D3D_COMPILE_STANDARD_FILE_INCLUDE resolves #include relative to the file.
        (str(source.resolve()), None, ctypes.c_void_p(1)),
        entry_bytes, profile_bytes, flags,
    )


def compile_hlsl_file(path: str | Path, **options) -> bytes:
    return compile_hlsl_file_detailed(path, **options).bytecode


__all__ = [
    "HlslCompileError",
    "HlslCompileResult",
    "compile_hlsl_file",
    "compile_hlsl_file_detailed",
    "compile_hlsl_source",
    "compile_hlsl_source_detailed",
    "hlsl_compiler_available",
]
