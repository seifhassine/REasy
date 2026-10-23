#!/usr/bin/env python3
"""Copy the license texts of everything bundled into REasy.exe into a folder."""
import re
import shutil
import sys
from importlib import metadata
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader

from fetch_dxc import FILES as DXC_FILES, OUT as DXC, VERSION as DXC_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GDEFLATE = PROJECT_ROOT / ".cache" / "gdeflate" / "GDeflateCore"
UNRECORDED = {
    "Python": (sys.version.split()[0], [Path(sys.base_prefix) / "LICENSE.txt"]),
    "GDeflate": ("", [GDEFLATE / "LICENSE", GDEFLATE / "subprojects" / "GDeflate" / "COPYING"]),
    "DirectXShaderCompiler": (DXC_VERSION, [DXC / name for name in DXC_FILES.values() if name.endswith(".txt")]),
}
QT_DISTRIBUTIONS = ("pyside6", "shiboken6")
NOTICES = (
    "Qt 6 and Qt for Python: Qt.txt",
    "SPIRV-Tools: SPIRV-Tools.txt",
    "Microsoft Visual C++ runtime: Microsoft Distributable Code, see Python/LICENSE.txt",
)
LICENSE_FILE = re.compile(r"(?i)(licen[cs]e|copying|notice|authors)")


def bundled_distributions(exe):
    archive = CArchiveReader(str(exe))
    names = {name.replace("\\", "/").casefold() for name in archive.toc}
    names |= {name.replace(".", "/").casefold() for name in archive.open_embedded_archive("PYZ.pyz").toc}
    owners = {}
    for dist in metadata.distributions():
        for file in dist.files or ():
            path = file.as_posix().casefold()
            for key in (path, path.removesuffix(".py").removesuffix("/__init__")):
                owners.setdefault(key, {})[dist.name.casefold()] = dist
    # A file several distributions record (PySide6/__init__.py) names none of them.
    found = (owner for name in names if len(owner := owners.get(name, {})) == 1)
    return {dist.name: dist for owner in found for dist in owner.values()}


def license_files(dist):
    return [file for file in dist.files or ()
            if any(part.endswith(".dist-info") for part in file.parts[:-1])
            and ("licenses" in file.parts or LICENSE_FILE.match(file.name))]


def copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def main(exe, out):
    out = Path(out)
    lines = []
    for name, dist in sorted(bundled_distributions(exe).items(), key=lambda item: item[0].casefold()):
        if name.casefold().startswith(QT_DISTRIBUTIONS):
            lines.append(f"{name} {dist.version}: Qt.txt")
            continue
        files = license_files(dist)
        if not files:
            raise SystemExit(f"{name} is bundled but has no license file")
        for file in files:
            copy(dist.locate_file(file), out / name / Path(*file.parts[1:]))
        lines.append(f"{name} {dist.version}: {name}/")
    for name, (version, files) in UNRECORDED.items():
        for file in files:
            copy(file, out / name / file.name)
        lines.append(f"{name} {version}".rstrip() + f": {name}/")
    text = "REasy.exe bundles the components below. Their license texts are in this folder.\n\n"
    (out / "THIRD-PARTY-NOTICES.txt").write_text(text + "\n".join(sorted(lines, key=str.casefold) + [""] + list(NOTICES)) + "\n",
                                                  encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: collect_licenses.py <REasy.exe> <output folder>")
    main(sys.argv[1], sys.argv[2])
