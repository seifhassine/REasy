"""See docs/mesh-conversion.md, Blender round trip."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from ..canonical_writer import serialize_mesh
from .export import build_export_package
from .package import load_package, save_package
from .mapping import BoneMapping, suggest
from .rebuild import BlendImportError, BlendImportResult, parse_source, rebuild_from_dump
from .runner import BlenderError, blender_version, find_blender, run_script, temporary_directory
from .standalone import Reference, reference_bones, reference_import, source_bones


@dataclass
class BlendExportResult:
    path: Path
    objects: int
    meshes: int
    verified: bool | None
    round_trip: BlendImportResult | None = None


def resource_version(mesh, file_version=None):
    if file_version:
        return int(file_version)
    pairs = [pair for pair in mesh.profile.canonical_version_pairs if pair[0] == mesh.header.version]
    if not pairs:
        raise ValueError("This mesh version has no verified writer; it cannot be embedded in a .blend")
    return max(pairs)[1]


def export_blend(mesh, path, *, blender=None, title=None, file_version=None, merge_seams=True, verify=True):
    version = resource_version(mesh, file_version)
    encoded = serialize_mesh(mesh)
    source = parse_source(encoded.main, encoded.streaming, version)
    path = Path(path)
    title = title or path.name.split(".")[0]
    manifest, arrays, _sources, _scene = build_export_package(
        source, title=title, source_main=encoded.main, source_streaming=encoded.streaming,
        file_version=version, merge_seams=merge_seams)
    blender = blender or find_blender()
    with temporary_directory() as directory:
        directory = Path(directory)
        package = directory / "package"
        save_package(package, manifest, arrays)
        (package / "source_main.bin").write_bytes(encoded.main)
        if encoded.streaming is not None:
            (package / "source_streaming.bin").write_bytes(encoded.streaming)
        staged = directory / "scene.blend"
        run_script(blender, "export", package, staged)
        verified = result = None
        if verify:
            result = apply_blend(_read(blender, staged, directory / "verify", "", True))
            if not result.unchanged:
                raise BlenderError("The exported .blend does not re-import identically:\n" +
                                   "\n".join(result.changes + result.losses))
            verified = True
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(staged, path)
    return BlendExportResult(path, len(manifest["objects"]), len(manifest["meshes"]), verified, result)


@dataclass
class BlendDump:
    manifest: dict
    arrays: dict
    main: bytes | None
    streaming: bytes | None

    @property
    def foreign(self):
        return bool(self.manifest.get("foreign"))


def _read(blender, path, directory, root, apply_modifiers):
    run_script(blender, "dump", path, directory, root or "", *(() if apply_modifiers else ("--keep-modifiers",)))
    directory = Path(directory)
    manifest, arrays = load_package(directory)

    def optional(name):
        return (directory / name).read_bytes() if (directory / name).exists() else None

    return BlendDump(manifest, arrays, optional("source_main.bin"), optional("source_streaming.bin"))


def read_blend(path, *, blender=None, root="", apply_modifiers=True):
    """Armature modifiers are never applied, so the rest pose imports."""
    blender = blender or find_blender()
    with temporary_directory() as directory:
        return _read(blender, Path(path), Path(directory) / "dump", root, apply_modifiers)


def apply_blend(dump, *, recompute_normals=False, skin_unweighted=False):
    if dump.foreign:
        raise BlendImportError("This scene was not exported by REasy; import it onto a reference mesh")
    return rebuild_from_dump(dump.manifest, dump.arrays, dump.main, dump.streaming,
                             recompute_normals=recompute_normals, skin_unweighted=skin_unweighted)


def import_blend(path, *, blender=None, root="", apply_modifiers=True, recompute_normals=False):
    dump = read_blend(path, blender=blender, root=root, apply_modifiers=apply_modifiers)
    return apply_blend(dump, recompute_normals=recompute_normals)


__all__ = ["BlendDump", "BlendExportResult", "BlendImportError", "BlendImportResult", "BlenderError", "BoneMapping",
           "Reference", "apply_blend", "blender_version", "export_blend", "find_blender", "import_blend",
           "read_blend", "reference_bones", "reference_import", "resource_version", "source_bones", "suggest"]
