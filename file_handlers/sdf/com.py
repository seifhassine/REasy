from __future__ import annotations

import ctypes
from ctypes import wintypes
from functools import lru_cache


_POINTER_SIZE = ctypes.sizeof(ctypes.c_void_p)


def com_method(interface: ctypes.c_void_p, index: int, restype: object, *argtypes: object):
    if not interface.value:
        raise ValueError("NULL pointer access")
    vtable = ctypes.c_void_p.from_address(interface.value).value
    if not vtable:
        raise ValueError("NULL pointer access")
    address = ctypes.c_void_p.from_address(vtable + index * _POINTER_SIZE).value
    return _typed_method(address, restype, argtypes)


@lru_cache(maxsize=256)
def _typed_method(address, restype, argtypes):
    prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return prototype(address)


def com_release(interface: ctypes.c_void_p | None) -> None:
    if interface is not None and interface.value:
        com_method(interface, 2, wintypes.ULONG)(interface)


def blob_bytes(pointer: ctypes.c_void_p | None) -> bytes:
    """``pointer`` is an ID3DBlob or IDxcBlob."""
    if pointer is None or not pointer.value:
        return b""
    address = com_method(pointer, 3, ctypes.c_void_p)(pointer)  # GetBufferPointer
    size = int(com_method(pointer, 4, ctypes.c_size_t)(pointer))  # GetBufferSize
    return ctypes.string_at(address, size) if address and size else b""
