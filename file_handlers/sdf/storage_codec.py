from __future__ import annotations

import base64
import re
import zlib

from .shader_graph import ShaderGraphError


IR_STORAGE_PREFIX = "zlib-base64:"
_URLSAFE_BASE64 = re.compile(rb"[A-Za-z0-9_-]*={0,2}")


def pack_ir_storage(raw: bytes, *, level: int) -> str:
    return IR_STORAGE_PREFIX + base64.urlsafe_b64encode(
        zlib.compress(raw, level=level)
    ).decode("ascii")


def unpack_ir_storage(text: object, max_bytes: int, label: str) -> str:
    if not isinstance(text, str) or not text.startswith(IR_STORAGE_PREFIX):
        raise ShaderGraphError(f"{label} are invalid.")
    try:
        encoded = text[len(IR_STORAGE_PREFIX) :].encode("ascii")
        if _URLSAFE_BASE64.fullmatch(encoded) is None:
            raise ValueError("not URL-safe base64")
        packed = base64.b64decode(encoded, altchars=b"-_", validate=True)
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(packed, max_bytes + 1)
    except (ValueError, zlib.error) as exc:
        raise ShaderGraphError(f"{label} are invalid.") from exc
    if (
        len(raw) > max_bytes
        or not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
    ):
        raise ShaderGraphError(f"{label} are too large.")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ShaderGraphError(f"{label} are invalid.") from exc
