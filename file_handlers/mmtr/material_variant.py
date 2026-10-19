from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import os
from pathlib import Path, PurePosixPath

from file_handlers.mdf.material_sync import (
    MaterialSyncResult,
    MaterialTextureSyncResult,
    parameter_layout_errors,
    synchronize_material_parameters,
    synchronize_material_textures,
    texture_layout_errors,
)
from file_handlers.material_names import resolve_material_name
from file_handlers.mdf.mdf_file import MdfFile
from file_handlers.mdf.material_layout import MaterialDescriptorLayout
from file_handlers.sdf.editing import SdfBinaryEditor
from file_handlers.sdf.models import STAGE_KEYS
from services.file_io import write_files_atomically
from utils.app_paths import PROJECT_METADATA_FOLDER
from utils.resource_file_utils import normalize_resource_path, resource_version_from_path

from .material_project import (
    apply_material_build,
    material_authoring_coverage,
    material_parameter_abi,
    material_program_routes,
    material_program_contract,
    material_resource_dimension,
)
from .mmtr_file import MmtrFile


class MaterialVariantError(ValueError):
    pass


MATERIAL_PROJECT_METADATA_DIR = f"{PROJECT_METADATA_FOLDER}/materials"


@dataclass(frozen=True, slots=True)
class MaterialVariantArtifacts:
    mmtr_data: bytes
    mdf_data: bytes
    project_data: bytes
    mmtr_resource_path: str
    material_names: tuple[str, ...]
    sync_results: tuple[MaterialSyncResult, ...]
    texture_sync_results: tuple[MaterialTextureSyncResult, ...] = ()
    member_files: tuple[tuple[str, bytes], ...] = ()


@dataclass(frozen=True, slots=True)
class MaterialVariantWriteResult:
    mmtr_path: str
    mdf_path: str


def strip_resource_version(path: str, extension: str) -> str:
    normalized = normalize_resource_path(path)
    extension = extension.casefold().lstrip(".")
    parts = normalized.rsplit("/", 1)
    name = parts[-1]
    fields = name.split(".")
    for index in range(len(fields) - 1):
        if fields[index].casefold() != extension:
            continue
        if index + 1 < len(fields) and fields[index + 1].isdecimal():
            name = ".".join(fields[: index + 1])
            break
    return f"{parts[0]}/{name}" if len(parts) == 2 else name


def mmtr_resource_path_from_file(path: str) -> str:
    normalized = normalize_resource_path(path)
    folded = normalized.casefold()
    for prefix in ("natives/x64/", "natives/stm/"):
        marker = folded.find(prefix)
        if marker >= 0:
            normalized = normalized[marker + len(prefix) :]
            break
    else:
        if os.path.isabs(os.fspath(path)):
            marker = folded.find("mastermaterial/")
            normalized = normalized[marker:] if marker >= 0 else normalized.rsplit("/", 1)[-1]
    return strip_resource_version(strip_resource_version(normalized, "mmtrs"), "mmtr")


def suggest_variant_file(path: str) -> str:
    if not str(path or "").strip():
        return "material_variant.mmtr"
    source = Path(path)
    name = source.name
    marker = name.casefold().find(".mmtr")
    if marker < 0:
        return str(source.with_name(f"{source.stem}_variant{source.suffix}"))
    return str(source.with_name(f"{name[:marker]}_variant{name[marker:]}"))


def resolve_project_output_path(project_dir: str, relative_game_path: str) -> str:
    if not str(project_dir or "").strip():
        raise MaterialVariantError("Open a project before creating a material variant.")
    raw = str(relative_game_path or "").strip().replace("\\", "/")
    if (
        not raw
        or raw.startswith("/")
        or (len(raw) >= 2 and raw[1] == ":")
        or "//" in raw
    ):
        raise MaterialVariantError(
            "Variant outputs must be relative game paths inside the open project."
        )
    parts = PurePosixPath(raw).parts
    if (
        not parts
        or parts[0].casefold() != "natives"
        or any(part in {"", ".", ".."} or ":" in part for part in parts)
    ):
        raise MaterialVariantError(
            "Enter a path beginning with natives/ and do not use parent-directory segments."
        )
    root = Path(project_dir).resolve()
    target = root.joinpath(*parts).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise MaterialVariantError(
            "The variant output must remain inside the open project."
        ) from exc
    return str(target)


def material_project_sidecar_path(mmtr_path: str, project_dir: str = "") -> str:
    absolute = Path(mmtr_path).resolve()
    if project_dir:
        root = Path(project_dir).resolve()
        try:
            relative = absolute.relative_to(root)
        except ValueError:
            pass
        else:
            return str(
                root
                / MATERIAL_PROJECT_METADATA_DIR
                / Path(f"{relative.as_posix()}.reasymaterial.json")
            )
    return f"{absolute}.reasymaterial.json"


def _normalized_mmtr_resource_path(path: str) -> str:
    raw = str(path or "").strip().replace("\\", "/").rstrip("\x00")
    if raw.startswith("@"):
        raw = raw[1:]
    if (
        not raw
        or raw.startswith("/")
        or (len(raw) >= 3 and raw[1:3] == ":/")
        or "//" in raw
    ):
        raise MaterialVariantError(
            "The MDF master-material path must be a relative in-game resource path."
        )
    result = mmtr_resource_path_from_file(path)
    parts = PurePosixPath(result).parts
    if (
        not parts
        or any(part in {"", ".", ".."} for part in parts)
        or any(":" in part for part in parts)
    ):
        raise MaterialVariantError(
            "The MDF master-material path must be a relative in-game resource path without parent-directory segments."
        )
    if not result.casefold().endswith((".mmtr", ".mmtrs")):
        raise MaterialVariantError(
            "The MDF master-material path must end in .mmtr or .mmtrs (without the numeric file version)."
        )
    if result.casefold().startswith(("natives/x64/", "natives/stm/")):
        raise MaterialVariantError(
            "The MDF master-material path must be relative to the game's natives directory."
        )
    return result


def validate_variant_targets(source_mmtr: str, source_mdf: str, target_mmtr: str, target_mdf: str) -> None:
    extension = "mmtrs" if mmtr_resource_path_from_file(source_mmtr).casefold().endswith(".mmtrs") else "mmtr"
    mmtr_version = resource_version_from_path(target_mmtr, extension)
    if mmtr_version is None:
        raise MaterialVariantError("The MMTR variant path must include its numeric resource version.")
    source_mmtr_version = resource_version_from_path(source_mmtr, extension)
    if source_mmtr_version is not None and mmtr_version != source_mmtr_version:
        raise MaterialVariantError(f"The MMTR variant must keep resource version {source_mmtr_version}.")
    mdf_version = resource_version_from_path(target_mdf, "mdf2")
    if mdf_version is None:
        raise MaterialVariantError(
            "The MDF output path must include its numeric resource version, such as .mdf2.10."
        )
    source_mdf_version = resource_version_from_path(source_mdf, "mdf2")
    if source_mdf_version is not None and mdf_version != source_mdf_version:
        raise MaterialVariantError(f"The MDF output must keep resource version {source_mdf_version}.")
    if os.path.isabs(source_mmtr):
        replaces_source = os.path.normcase(os.path.abspath(source_mmtr)) == os.path.normcase(
            os.path.abspath(target_mmtr)
        )
    else:
        replaces_source = (
            bool(source_mmtr)
            and not os.path.isabs(target_mmtr)
            and normalize_resource_path(source_mmtr).casefold()
            == normalize_resource_path(target_mmtr).casefold()
        )
    if replaces_source:
        raise MaterialVariantError(
            "Choose a new MMTR path for the variant. Use ordinary Save to replace the current MMTR."
        )


def _compiled_away_textures(build) -> set[str]:
    bound = {item.name for replacement in build.replacements for item in replacement.texture_bindings}
    return {str(item.name) for item in build.texture_layout} - bound


def _validate_compiled_interface(document, build) -> None:
    interface, layout = document.material_interface, build.buffer_layout
    actual, expected, sizes = material_parameter_abi(interface, layout)
    bindless = build.descriptor_layout is not None
    # A graph that bakes every parameter leaves no compiled shader bound to the material buffer.
    user_buffers = {str(name).casefold() for name in document.profile.user_material_buffer_names}
    consumed = any(buffer.name.casefold() in user_buffers
                   for item in build.replacements for buffer in item.constant_buffer_bindings)
    unbound = bindless or not consumed
    if actual != expected and (actual or not unbound):
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise MaterialVariantError(
            "The rebuilt MMTR did not reparse with its compiled parameter ABI. "
            f"Missing={missing!r}; unexpected={extra!r}."
        )
    if sizes != {int(layout.byte_size)} and (actual or not unbound):
        raise MaterialVariantError(
            "The rebuilt MMTR exposes inconsistent material-buffer sizes: "
            f"{sorted(sizes)!r}; expected {layout.byte_size}."
        )

    desired = {
        str(item.name): str(item.dimension)
        for item in build.texture_layout
    }
    resources = {
        str(item.name): item
        for item in interface.resources
        if item.resource_kind == "shader_resource"
    }
    missing_textures = [] if bindless else sorted(set(desired) - set(resources) - _compiled_away_textures(build))
    wrong_dimensions = sorted(
        name
        for name, dimension in desired.items()
        if name in resources and material_resource_dimension(resources[name]) != dimension
    )
    removed_textures = sorted(
        str(name)
        for name in build.source_texture_names
        if str(name) not in desired
        and str(name) in resources
    )
    if missing_textures or wrong_dimensions or removed_textures:
        raise MaterialVariantError(
            "The rebuilt MMTR did not reparse with its compiled texture ABI. "
            f"Missing={missing_textures!r}; wrong dimensions={wrong_dimensions!r}; "
            f"not removed={removed_textures!r}."
        )

    for replacement in build.replacements:
        material_program_contract(document, replacement.target_index, replacement.program_index, replacement.stage)
        program = document.sdf.targets[replacement.target_index].programs[
            replacement.program_index
        ]
        rows = {
            str(metadata.name): (metadata, descriptor)
            for metadata, descriptor in zip(
                program.shader_resources,
                program.bindings[2],
            )
        }
        expected_names = {
            item.name for item in replacement.texture_bindings
        }
        stage_index = STAGE_KEYS.index(replacement.stage)
        stale = sorted(
            str(name)
            for name in build.source_texture_names
            if str(name) not in expected_names
            and str(name) in rows
            and int(rows[str(name)][1].stage_mask) & (1 << stage_index)
        )
        if stale:
            raise MaterialVariantError(
                f"Program {replacement.target_index}:{replacement.program_index} "
                f"still binds deleted material textures {stale!r}."
            )
        for expected_texture in replacement.texture_bindings:
            row = rows.get(expected_texture.name)
            if row is None:
                raise MaterialVariantError(
                    f"Program {replacement.target_index}:{replacement.program_index} "
                    f"does not bind material texture {expected_texture.name!r}."
                )
            _metadata, descriptor = row
            if (
                not int(descriptor.stage_mask) & (1 << stage_index)
                or int(descriptor.bind_slots[stage_index])
                != int(expected_texture.bind_point)
                or int(descriptor.type_code) != int(expected_texture.type_code)
            ):
                raise MaterialVariantError(
                    f"Program {replacement.target_index}:{replacement.program_index} "
                    f"reparsed {expected_texture.name!r} with an inconsistent {replacement.stage} binding."
                )


def _texture_contract_errors(material, document, descriptor_layout, compiled_away) -> tuple[str, ...]:
    available = {
        str(resource.name)
        for resource in document.material_interface.resources
        if resource.resource_kind in {"shader_resource", "optional_resource"}
        and any(
            "texture" in str(type_name).casefold()
            for type_name in resource.type_names
        )
    }
    if descriptor_layout is not None:
        available.update(descriptor_layout.textures)
    available.update(compiled_away)
    return tuple(
        str(texture.tex_type)
        for texture in material.textures
        if resolve_material_name(str(texture.tex_type), available) is None
    )


def _rebuild_variant_member(document, build, project, path):
    coverage = material_authoring_coverage(document, project)
    keys = {(int(item.target_index), int(item.program_index), item.stage) for item in build.replacements}
    if not coverage.complete or keys != set(material_program_routes(document, project)):
        raise MaterialVariantError("Create Variant requires complete coverage for every render path affected by this graph.")
    editor = SdfBinaryEditor(document.sdf)
    apply_material_build(editor, document, build)
    rebuilt = MmtrFile.from_bytes(editor.data,
        resource_version=resource_version_from_path(path, "mmtr"), profile=document.profile, label=path)
    canonical = rebuilt.write()
    result = MmtrFile.from_bytes(canonical, profile=document.profile, label=path).require_document()
    _validate_compiled_interface(result, build)
    if result.write() != canonical:
        raise MaterialVariantError("The rebuilt MMTR does not have an idempotent canonical serialization.")
    return canonical, result


def create_material_variant_artifacts(
    document,
    build,
    project,
    *,
    mdf_source_data: bytes,
    mdf_source_path: str,
    target_mmtr_path: str,
    target_mdf_path: str,
    mmtr_resource_path: str,
    material_indices: tuple[int, ...],
) -> MaterialVariantArtifacts:
    family = getattr(document, "material_family", None)
    source_mmtr = family.source_path if family else str(getattr(document, "source_label", "") or "")
    validate_variant_targets(source_mmtr, mdf_source_path, target_mmtr_path, target_mdf_path)
    resource_path = _normalized_mmtr_resource_path(mmtr_resource_path)
    derived_target_resource = mmtr_resource_path_from_file(target_mmtr_path)
    target_resource_name = PurePosixPath(derived_target_resource).name.casefold()
    if PurePosixPath(resource_path).name.casefold() != target_resource_name:
        raise MaterialVariantError(
            "The MMTR resource path and output filename must name the same material."
        )
    normalized_target = normalize_resource_path(target_mmtr_path).casefold()
    if any(
        marker in normalized_target
        for marker in ("natives/x64/", "natives/stm/")
    ) and derived_target_resource.casefold() != resource_path.casefold():
        raise MaterialVariantError(
            "The in-game MMTR path must match the new file's directory below natives."
        )
    selected = tuple(dict.fromkeys(int(index) for index in material_indices))
    if not selected:
        raise MaterialVariantError("Select at least one MDF material to assign.")
    expected_profile = document.profile.shader_graph_profile.profile_id
    if str(build.profile) != expected_profile:
        raise MaterialVariantError(
            "The compiled graph belongs to a different game/material profile."
        )
    member_files = []
    if family is not None:
        from .material_family import family_member_filename, family_member_project
        from .mmtrs_file import MmtrsFile
        if len(build.family_builds) != len(family.members):
            raise MaterialVariantError("Every MMTRS member must be rebuilt together.")
        paths, payload_paths = [], {}
        for lod, (member, member_build) in enumerate(zip(family.members, build.family_builds)):
            if (member_build.buffer_layout, member_build.texture_layout, member_build.descriptor_layout) != (build.buffer_layout, build.texture_layout, build.descriptor_layout):
                raise MaterialVariantError("MMTRS members must share one compiled MDF interface.")
            member_resource = resource_path[:-len('.mmtrs')] + f"_lod{lod}.mmtr"
            filename = family_member_filename(target_mmtr_path, member, lod)
            payload, rebuilt = _rebuild_variant_member(member, member_build, family_member_project(project, lod),
                                                       str(Path(target_mmtr_path).with_name(filename)))
            if payload not in payload_paths:
                payload_paths[payload] = member_resource
                member_files.append((filename, payload))
            paths.append(payload_paths[payload])
            if lod == 0:
                rebuilt_document = rebuilt
        mmtr_data = MmtrsFile(tuple(paths), family.resource.resource_version).write()
    else:
        if build.family_builds:
            raise MaterialVariantError("A family build requires its source MMTRS context.")
        mmtr_data, rebuilt_document = _rebuild_variant_member(document, build, project, target_mmtr_path)

    mdf = MdfFile()
    try:
        mdf.read(bytes(mdf_source_data), mdf_source_path)
    except Exception as exc:
        raise MaterialVariantError(f"The selected MDF could not be parsed: {exc}") from exc
    if any(index < 0 or index >= len(mdf.materials) for index in selected):
        raise MaterialVariantError("The selected MDF material no longer exists.")

    results: list[MaterialSyncResult] = []
    texture_results: list[MaterialTextureSyncResult] = []
    names: list[str] = []
    compiled_away = _compiled_away_textures(build)
    for index in selected:
        material = mdf.materials[index]
        if material.shader_lod_redirects and family is None:
            raise MaterialVariantError("This MDF uses shader LODs. A single-MMTR variant cannot replace its MMTRS family.")
        if family is not None and mmtr_resource_path_from_file(material.header.mmtr_path).casefold() != mmtr_resource_path_from_file(family.source_path).casefold():
            raise MaterialVariantError("The selected MDF does not reference this MMTRS family.")
        if build.source_descriptor_layout is not None:
            if MaterialDescriptorLayout.from_material(material) != build.source_descriptor_layout:
                raise MaterialVariantError("The selected MDF has a different bindless descriptor/LOD layout than the source shader ABI.")
        material.header.mmtr_path = resource_path
        results.append(
            synchronize_material_parameters(material, build.buffer_layout)
        )
        texture_results.append(
            synchronize_material_textures(material, build.texture_layout)
        )
        if build.descriptor_layout is not None:
            material.shader_lod_redirects = [(list(t), list(b)) for t, b in build.descriptor_layout.shader_lod_redirects]
        texture_errors = _texture_contract_errors(material, rebuilt_document, build.descriptor_layout, compiled_away)
        if texture_errors:
            raise MaterialVariantError(
                f"MDF material {material.header.mat_name!r} has texture inputs not provided "
                f"by this MMTR: {', '.join(texture_errors)}."
            )
        layout_errors = parameter_layout_errors(material, build.buffer_layout)
        layout_errors += texture_layout_errors(material, build.texture_layout)
        if layout_errors:
            raise MaterialVariantError(
                f"MDF material {material.header.mat_name!r} could not be synchronized: "
                + "; ".join(layout_errors)
            )
        names.append(str(material.header.mat_name))

    try:
        mdf_data = mdf.write()
        reparsed_mdf = MdfFile()
        reparsed_mdf.read(mdf_data, target_mdf_path)
    except Exception as exc:
        raise MaterialVariantError(f"The synchronized MDF could not be serialized: {exc}") from exc
    for index in selected:
        material = reparsed_mdf.materials[index]
        errors = parameter_layout_errors(material, build.buffer_layout)
        errors += texture_layout_errors(material, build.texture_layout)
        if build.descriptor_layout is not None:
            if MaterialDescriptorLayout.from_material(material) != build.descriptor_layout:
                errors += ("The saved MDF does not reproduce the compiled bindless descriptor layout.",)
        if material.header.mmtr_path != resource_path:
            errors += (
                f"MMTR path reparsed as {material.header.mmtr_path!r}, expected {resource_path!r}",
            )
        texture_errors = _texture_contract_errors(material, rebuilt_document, build.descriptor_layout, compiled_away)
        if texture_errors:
            errors += ("unmatched texture inputs: " + ", ".join(texture_errors),)
        if errors:
            raise MaterialVariantError(
                f"MDF material {material.header.mat_name!r} failed save/reload validation: "
                + "; ".join(errors)
            )

    variant_project = deepcopy(project)
    variant_project.source_profile = str(build.profile)
    texture_slots = {
        item.texture_id: item for item in build.texture_layout
    }
    variant_project.textures = [
        replace(
            definition,
            source_name=texture_slots[definition.texture_id].name,
        )
        for definition in variant_project.textures
    ]
    variant_project.source_texture_names = [
        str(item.name) for item in build.texture_layout
    ]
    if build.descriptor_layout is not None:
        boundary = next(node for node in variant_project.graph.nodes if node.properties.get("semantic_reconstruction_boundary"))
        boundary.properties["instance_descriptor_layout"] = build.descriptor_layout.to_dict()
    return MaterialVariantArtifacts(
        mmtr_data=mmtr_data,
        mdf_data=mdf_data,
        project_data=variant_project.to_json().encode("utf-8"),
        mmtr_resource_path=resource_path,
        material_names=tuple(names),
        sync_results=tuple(results),
        texture_sync_results=tuple(texture_results),
        member_files=tuple(member_files),
    )


def write_material_variant(
    artifacts: MaterialVariantArtifacts,
    *,
    target_mmtr_path: str,
    target_mdf_path: str,
    project_path: str,
) -> MaterialVariantWriteResult:
    sidecar = os.path.abspath(project_path)
    mmtr_path = os.path.abspath(target_mmtr_path)
    mdf_path = os.path.abspath(target_mdf_path)
    destinations = (mmtr_path, mdf_path, sidecar)
    if len({os.path.normcase(path) for path in destinations}) != len(destinations):
        raise MaterialVariantError(
            "MMTR, MDF, and editable graph outputs must be three distinct files."
        )
    outputs = {
        mmtr_path: artifacts.mmtr_data,
        mdf_path: artifacts.mdf_data,
        sidecar: artifacts.project_data,
    }
    for filename, payload in artifacts.member_files:
        if filename != Path(filename).name or "/" in filename or "\\" in filename or ":" in filename:
            raise MaterialVariantError("A material family output must be a sibling filename.")
        path = str(Path(mmtr_path).with_name(filename))
        if os.path.normcase(path) in {os.path.normcase(p) for p in outputs}:
            raise MaterialVariantError(f"Material family outputs collide at {path!r}.")
        outputs[path] = payload
    try:
        write_files_atomically(outputs)
    except OSError as exc:
        raise MaterialVariantError(
            f"The material variant could not be committed safely: {exc}"
        ) from exc
    return MaterialVariantWriteResult(
        mmtr_path=mmtr_path,
        mdf_path=mdf_path,
    )


__all__ = [
    "MaterialVariantArtifacts",
    "MaterialVariantError",
    "MaterialVariantWriteResult",
    "create_material_variant_artifacts",
    "mmtr_resource_path_from_file",
    "material_project_sidecar_path",
    "resolve_project_output_path",
    "strip_resource_version",
    "suggest_variant_file",
    "validate_variant_targets",
    "write_material_variant",
]
