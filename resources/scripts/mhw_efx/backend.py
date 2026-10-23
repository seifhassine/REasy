"""Standalone GPL-3.0-or-later MHW EFX inspection service.

The adjacent efx_format package vendors MHW-EFX-Editor v0.7.7 code;
local changes are recorded in UPSTREAM.json.
REasy communicates with this separate program through JSON on standard streams.
See LICENSE and UPSTREAM.json for the upstream source and attribution.
"""
from __future__ import annotations

import base64
import json
import struct
import sys

from efx_format.efxfile import EFXFile, EFXHeader
from efx_format.assembly.efx import efx_to_json

SUPPORTED_VERSIONS = {711800}


def inspect(data: bytes) -> dict:
    if len(data) < EFXHeader.SIZE:
        raise ValueError("Truncated MHW EFX header")
    fields = EFXHeader.STRUCT.unpack_from(data)
    if fields[0] != b"EFX\0" or fields[7] != b"efxr":
        raise ValueError("Invalid MHW EFX header signature")
    version = fields[1]
    if version not in SUPPORTED_VERSIONS:
        raise ValueError(f"Unsupported MHW EFX version {version}")
    if fields[8] not in (0, 1):
        raise ValueError(f"Invalid MHW EFX dimension {fields[8]}")
    for index in range(10, 17):
        if fields[index] > len(data) - EFXHeader.SIZE:
            raise ValueError(f"Invalid MHW EFX header count/size at 0x{index * 4:X}")
    native = EFXFile.parse(data)
    # efx_to_json rejects opaque Main/Root data and unsupported attribute codecs.
    # A preserved raw tail alone does not constitute a successful parse.
    document = efx_to_json(native)
    if native.serialize() != data:
        raise ValueError("MHW EFX native roundtrip differs from the input")
    label_offset = 0
    for section in ("actions", "externs", "entries"):
        for index, item in enumerate(document[section]):
            item["index"] = index
            item["name"] = document["labels"][label_offset] if label_offset < len(document["labels"]) else ""
            label_offset += 1
    return {"format": "MHW_EFX", "version": version, "dimension": native.header.is_3d,
            "roundtrip_exact": True, **document}


def main():
    try:
        request = json.load(sys.stdin)
        if request["operation"] != "inspect":
            raise ValueError(f"Unsupported MHW EFX operation {request['operation']!r}")
        data = base64.b64decode(request["data"], validate=True)
        response = {"ok": True, "document": inspect(data)}
    except (ValueError, KeyError, TypeError, IndexError, struct.error, UnicodeError) as error:
        response = {"ok": False, "error": f"{type(error).__name__}: {error}"}
    json.dump(response, sys.stdout, ensure_ascii=True, allow_nan=False)
    sys.stdout.write("\n")
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
