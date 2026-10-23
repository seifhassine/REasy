#!/usr/bin/env python3
"""Fetch the DirectX Shader Compiler REasy pins into .cache/dxc, for REasy.exe to embed.

Only dxcompiler.dll is embedded: since DXC 1.8.2505 it validates and signs without dxil.dll, which is under Microsoft's
own terms. The license texts are those of the DLL and of the HLSL headers built into it.
"""
import hashlib
import io
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.shader_tools import PACKAGES

(URL, SHA256, _), = PACKAGES["dxc"][1]
VERSION = re.search(r"/download/v([^/]+)/", URL).group(1)
OUT = PROJECT_ROOT / ".cache" / "dxc"
FILES = {
    "bin/x64/dxcompiler.dll": "dxcompiler.dll",
    "LICENSE-LLVM.txt": "LICENSE-LLVM.txt",
    "LICENCE-MIT.txt": "LICENCE-MIT.txt",
    "inc/hlsl/LICENCE.txt": "HLSL-HEADERS-LICENCE.txt",
}


def main():
    stamp = OUT / ".reasy-dxc.stamp"
    if (stamp.is_file() and stamp.read_text(encoding="ascii").strip() == SHA256
            and all((OUT / name).is_file() for name in FILES.values())):
        return
    with urllib.request.urlopen(URL, timeout=60) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise SystemExit(f"{URL} failed SHA-256 verification")
    OUT.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member, name in FILES.items():
            (OUT / name).write_bytes(archive.read(member))
    stamp.write_text(SHA256, encoding="ascii")


if __name__ == "__main__":
    main()
