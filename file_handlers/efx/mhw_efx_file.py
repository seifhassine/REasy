"""Version-gated client for the standalone MHW EFX parser."""
from __future__ import annotations

import base64
import copy
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field

from utils.app_paths import resource_path
from .efx_file import EfxParseError

MHW_EFX_MAGIC = b"EFX\0"
MHW_EFX_VERSIONS = frozenset({711800})


def backend_command():
    if getattr(sys, "frozen", False):
        return [str(resource_path("resources/scripts/mhw_efx/mhw_efx_backend.exe", required=True))]
    return [sys.executable, str(resource_path("resources/scripts/mhw_efx/backend.py", required=True))]


def inspect_mhw_efx(data: bytes):
    request = {"operation": "inspect", "data": base64.b64encode(data).decode("ascii")}
    environment = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    completed = subprocess.run(backend_command(), input=json.dumps(request),
                               capture_output=True, text=True, encoding="utf-8",
                               env=environment, timeout=60,
                               creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    if not completed.stdout:
        raise EfxParseError(f"MHW EFX backend failed: {completed.stderr.strip()}")
    response = json.loads(completed.stdout)
    if not response["ok"]:
        raise EfxParseError(response["error"])
    if completed.returncode:
        raise EfxParseError(f"MHW EFX backend exited with code {completed.returncode}")
    return response["document"]


@dataclass(frozen=True)
class MhwEfxFile:
    version: int
    data: bytes = field(repr=False)
    _document: dict = field(repr=False)

    @property
    def document(self):
        return copy.deepcopy(self._document)

    @classmethod
    def from_bytes(cls, data: bytes, *, version: int):
        if version not in MHW_EFX_VERSIONS:
            raise EfxParseError(f"Unsupported MHW EFX version {version}")
        document = inspect_mhw_efx(data)
        if document["version"] != version:
            raise EfxParseError("MHW EFX header version differs from the selected profile")
        return cls(version, bytes(data), document)

    def build_validated(self):
        # The backend already checked parse/serialize byte equality. This model
        # exposes a read-only inspection snapshot and never rewrites its fields.
        return self.data
