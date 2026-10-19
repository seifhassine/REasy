from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from pathlib import Path
lazy from utils.resource_file_utils import resource_version_from_path

lazy from file_handlers.sdf import SdfFile
lazy from file_handlers.sdf.models import RESOURCE_KINDS, LazyPrograms, SdfProgram, program_tables
lazy from file_handlers.sdf.sdf_file import parse_sdf_to_draw
lazy from file_handlers.sdf.shader_graph import ShaderContract

lazy from .analysis import MmtrAnalysisError, skinning_influence_count
lazy from .models import (
    LazyProgramSemantics,
    MmtrDocument,
    MmtrMaterialInterface,
    MmtrMaterialParameter,
    MmtrProgramSemantics,
    MmtrResourceContract,
    MmtrSemanticNote,
)
from .profiles import (
    ALL_RE_ENGINE_MMTR_PROFILES,
    DMC5_MMTR_PROFILE,
    MmtrGameProfile,
    mmtr_profile_for_game,
    mmtr_profile_for_resource,
)


def _signature_name(parameter) -> str:
    return f"{parameter.semantic_name}{parameter.semantic_index}"


def _program_semantics(
    program: SdfProgram,
    profile: MmtrGameProfile,
    *,
    program_count: int,
    cache: dict | None = None,
) -> MmtrProgramSemantics:
    vertex = program.shader("vertex")
    reflection = vertex.reflection if vertex is not None else None
    if cache is not None:
        depth = program.depth_stencil_state
        key = (
            program.name, program_count,
            depth.depth_write_mask if depth is not None else None,
            tuple(shader.present for shader in program.shaders),
            id(reflection.input_signature) if reflection is not None else None,
            id(reflection.output_signature) if reflection is not None else None,
        )
        existing = cache.get(key)
        if existing is not None:
            return replace(existing, target_index=program.target_index,
                           program_index=program.program_index)
    descriptor = profile.classify_program(
        program.name,
        program=program,
        program_count=program_count,
    )
    notes = list(descriptor.notes)
    vertex_inputs = ()
    influence_count = None
    if reflection is not None:
        signature = reflection.input_signature
        vertex_inputs = tuple(_signature_name(parameter) for parameter in signature)
        try:
            influence_count = skinning_influence_count(
                signature, profile, skinned_geometry=descriptor.geometry == "skinned")
        except MmtrAnalysisError as exc:
            notes.append(
                MmtrSemanticNote(
                    "program.skinning",
                    str(exc),
                )
            )

        outputs = reflection.output_signature
        has_clip_distance = any(
            parameter.system_value == "clip-distance"
            or parameter.semantic_name.upper() == "SV_CLIPDISTANCE"
            for parameter in outputs
        )
        if descriptor.clip_distance != has_clip_distance:
            notes.append(
                MmtrSemanticNote(
                    "program.clip_distance",
                    (
                        f"name contract says {descriptor.clip_distance}, while the "
                        f"vertex signature says {has_clip_distance}"
                    ),
                )
            )

    if descriptor.shader_type_id in {2, 3}:
        depth = program.depth_stencil_state
        if depth is not None and depth.depth_write_mask != "zero":
            notes.append(
                MmtrSemanticNote(
                    "program.shader_type",
                    "ZPrePassedGBuffer program unexpectedly writes depth",
                )
            )

    result = MmtrProgramSemantics(
        target_index=program.target_index,
        program_index=program.program_index,
        name=program.name,
        descriptor=descriptor,
        vertex_inputs=vertex_inputs,
        skinning_influence_count=influence_count,
        notes=tuple(notes),
    )
    if cache is not None:
        cache[key] = result
    return result


def _material_interface(
    sdf,
    profile: MmtrGameProfile,
) -> MmtrMaterialInterface:
    user_names = {name.casefold() for name in profile.user_material_buffer_names}
    user_buffer_sizes: set[int] = set()
    outer_user_buffers: set[str] = set()
    reflected_user_buffer_sizes: set[int] = set()
    parameters: dict[tuple[str, int, int], dict[str, set]] = {}
    resources: dict[tuple[str, str], dict[str, set]] = {}
    logical_names = {}
    generated_parameter_names = {}
    buffer_groups = {}
    resource_groups = {}

    for target in sdf.targets:
        for program in program_tables(target):
            program_key = (target.index, program.program_index)
            buffer_key = id(program.constant_buffers)
            buffer_group = buffer_groups.get(buffer_key)
            if buffer_group is None:
                entries = []
                for buffer in program.constant_buffers:
                    if buffer.name.casefold() not in user_names:
                        continue
                    outer_user_buffers.add(buffer.name.casefold())
                    user_buffer_sizes.add(buffer.byte_size)
                    for variable in buffer.variables:
                        key = (variable.name, variable.byte_offset, variable.byte_size)
                        item = parameters.get(key)
                        if item is None:
                            item = parameters[key] = {"types": set(), "programs": set()}
                        entries.append(item)
                buffer_group = buffer_groups[buffer_key] = (set(), tuple(entries))
            if buffer_group[1]:
                buffer_group[0].add(program_key)

            metadata_groups = (
                program.constant_buffers,
                program.samplers,
                program.shader_resources,
                program.unordered_access_resources,
                program.optional_resources,
            )
            program_resource_groups = []
            for kind_index, (metadata, bindings) in enumerate(
                zip(metadata_groups, program.bindings)
            ):
                if not metadata or not bindings:
                    continue
                group_key = (kind_index, id(metadata), id(bindings))
                group = resource_groups.get(group_key)
                if group is None:
                    kind = RESOURCE_KINDS[kind_index]
                    entries = []
                    for resource, binding in zip(metadata, bindings):
                        key = (kind, resource.name)
                        item = resources.get(key)
                        if item is None:
                            item = resources[key] = {"type_names": set()}
                        item["type_names"].add(binding.type_name)
                        entries.append((resource.name, item))
                    group = resource_groups[group_key] = tuple(entries)
                program_resource_groups.append(group)

            program_resources = None
            for shader in program.shaders:
                # ``parse_sdf_to_draw`` leaves the DXIL reflection unread.
                dxbc = shader.reflection
                reflection = dxbc.reflection if dxbc is not None else None
                if reflection is None:
                    continue
                for buffer in reflection.constant_buffers:
                    if buffer.name.casefold() not in user_names:
                        continue
                    reflected_user_buffer_sizes.add(buffer.byte_size)
                    for variable in buffer.variables:
                        key = (variable.name, variable.start_offset, variable.size)
                        item = parameters.get(key)
                        logical_name = logical_names.get(variable.name)
                        if logical_name is None:
                            logical_name = logical_names[variable.name] = profile.material_parameter_name(variable.name)
                        key = (logical_name, variable.start_offset, variable.size)
                        if item is None:
                            item = parameters.get(key)
                        if item is None and variable.name.startswith("REasy_"):
                            alias_key = id(shader), id(program.constant_buffers)
                            aliases = generated_parameter_names.get(alias_key)
                            if aliases is None:
                                from file_handlers.sdf.contracts import material_buffer_contract
                                try:
                                    typed = material_buffer_contract(
                                        ShaderContract.from_shader(dxbc), program,
                                        buffer_names=profile.user_material_buffer_names,
                                        logical_name=profile.material_parameter_name,
                                        allow_unreflected_fields=True, _indexed_names=True,
                                    )
                                except ValueError:
                                    aliases = {}
                                else:
                                    aliases = defaultdict(list)
                                    for declaration in typed.constant_buffers:
                                        for field in declaration.variables:
                                            aliases[(declaration.name, field.byte_offset, field.byte_size)].append(field.name)
                                generated_parameter_names[alias_key] = aliases
                            names = aliases.get((buffer.name, variable.start_offset, variable.size), ())
                            if len(names) == 1:
                                item = parameters.get((names[0], variable.start_offset, variable.size))
                        if item is None:
                            continue
                        item["types"].add(variable.variable_type.display_name)
                        item["programs"].add(program_key)

                if program_resources is None:
                    program_resources = defaultdict(list)
                    for group in program_resource_groups:
                        for name, item in group:
                            program_resources[name].append(item)
                for reflected in reflection.resources:
                    for item in program_resources.get(reflected.name, ()):
                        item["type_names"].add(
                            f"{reflected.input_type} {reflected.dimension}"
                        )

    for programs, entries in buffer_groups.values():
        for item in entries:
            item["programs"].update(programs)

    parameter_models = tuple(
        MmtrMaterialParameter(
            name=name,
            byte_offset=byte_offset,
            byte_size=byte_size,
            type_names=tuple(sorted(item["types"])),
            program_count=len(item["programs"]),
        )
        for (name, byte_offset, byte_size), item in sorted(
            parameters.items(),
            key=lambda pair: (pair[0][1], pair[0][0]),
        )
    )
    resource_models = tuple(
        MmtrResourceContract(resource_kind=kind, name=name, type_names=tuple(sorted(item["type_names"])))
        for (kind, name), item in sorted(resources.items())
    )
    return MmtrMaterialInterface(
        user_buffer_sizes=tuple(
            sorted(
                user_buffer_sizes
                if outer_user_buffers
                else reflected_user_buffer_sizes
            )
        ),
        parameters=parameter_models,
        resources=resource_models,
        parameter_storage=(
            "explicit-constant-buffer" if parameter_models else "none"
        ),
    )


def _document_from_sdf(
    sdf_document,
    profile: MmtrGameProfile,
    label: str,
) -> MmtrDocument:
    semantic_cache = {} if _supports_preview_without_native_reflection(profile) else None

    def semantics(target_index: int, program_index: int) -> MmtrProgramSemantics:
        target = sdf_document.targets[target_index]
        return _program_semantics(target.programs[program_index], profile, program_count=len(target.programs),
                                  cache=semantic_cache)
    keys = tuple((target.index, index) for target in sdf_document.targets for index in range(len(target.programs)))
    if any(isinstance(target.programs, LazyPrograms) for target in sdf_document.targets):
        programs, family, notes = LazyProgramSemantics(keys, semantics), "", ()
    else:
        programs = tuple(semantics(*key) for key in keys)
        family = profile.document_classifier(tuple(program.descriptor for program in programs))
        notes = tuple(dict.fromkeys(note for program in programs for note in program.notes))
    material_interface = _material_interface(sdf_document, profile)
    material_interface = profile.shader_graph_profile.supplement_material_interface(material_interface, str(label or ""))
    document = MmtrDocument(
        profile=profile,
        sdf=sdf_document,
        family=family,
        programs=programs,
        material_interface=material_interface,
        source_label=str(label or ""),
        notes=notes,
    )
    return document


def _preview_hooks(profile: MmtrGameProfile) -> tuple:
    graph_profile = profile.shader_graph_profile
    hooks = [
        profile.program_classifier, profile.program_refiner,
        profile.document_classifier, type(graph_profile),
    ]
    hooks.append(graph_profile.supplement_material_interface.__func__)
    return tuple(hooks)


_PREVIEW_HOOKS = tuple(dict.fromkeys(
    _preview_hooks(profile) for profile in (DMC5_MMTR_PROFILE, *ALL_RE_ENGINE_MMTR_PROFILES)
))


def _supports_preview_without_native_reflection(profile: MmtrGameProfile) -> bool:
    if type(profile) is not MmtrGameProfile:
        return False
    hooks = _preview_hooks(profile)
    return any(
        all(current is known for current, known in zip(hooks, registered))
        for registered in _PREVIEW_HOOKS
    )


def _parse_preview_document(
    data: bytes | bytearray | memoryview,
    profile: MmtrGameProfile,
    label: str,
) -> MmtrDocument:
    if not _supports_preview_without_native_reflection(profile):
        return MmtrFile.from_bytes(data, profile=profile, label=label).require_document()

    return _document_from_sdf(parse_sdf_to_draw(data, profile.sdf_profile, label), profile, label)


def parse_drawn_master(data: bytes, game: str, label: str) -> MmtrDocument:
    """For drawing only: the interface lacks the reflected types and stages, and the master is not classified."""
    return _parse_preview_document(data, mmtr_profile_for_game(game), label)


class MmtrFile:
    def __init__(self) -> None:
        self.document: MmtrDocument | None = None
        self.sdf: SdfFile | None = None
        self.raw_data = b""

    @classmethod
    def from_bytes(
        cls,
        data: bytes | bytearray | memoryview,
        *,
        resource_version: int | None = None,
        game: object = "",
        profile: MmtrGameProfile | None = None,
        label: str = "MMTR",
    ) -> "MmtrFile":
        result = cls()
        result.read(
            data,
            resource_version=resource_version,
            game=game,
            profile=profile,
            label=label,
        )
        return result

    @classmethod
    def from_path(cls, path: str | Path, **kwargs) -> "MmtrFile":
        source = Path(path)
        kwargs.setdefault("label", str(source))
        kwargs.setdefault("resource_version", resource_version_from_path(str(source), "mmtr"))
        return cls.from_bytes(source.read_bytes(), **kwargs)

    def read(
        self,
        data: bytes | bytearray | memoryview,
        *,
        resource_version: int | None = None,
        game: object = "",
        profile: MmtrGameProfile | None = None,
        label: str = "MMTR",
    ) -> None:
        raw = bytes(data)
        selected = profile or mmtr_profile_for_resource(resource_version, game=game)
        self.sdf = SdfFile.from_bytes(
            raw,
            profile=selected.sdf_profile,
            label=label,
        )
        sdf_document = self.sdf.require_document()
        self.document = _document_from_sdf(sdf_document, selected, label)
        self.raw_data = raw

    def require_document(self) -> MmtrDocument:
        if self.document is None:
            raise ValueError("no MMTR file is loaded")
        return self.document

    def write(self) -> bytes:
        return self.require_document().write()


__all__ = ["MmtrFile", "parse_drawn_master"]
