from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Callable

from utils.game_keys import game_key

BINNER_STATES = frozenset(("Automatic", "Force on", "Force off"))


@dataclass(frozen=True, slots=True)
class SdfStageLayout:
    key: str
    pointer_offset: int
    size_offset: int
    stage_mask: int


@dataclass(frozen=True, slots=True)
class SdfTargetLayout:
    key: str
    display_name: str
    selection_capability_bit: int | None


@dataclass(frozen=True, slots=True)
class SdfProgramLayout:
    name_pointer_offset: int
    input_signature_pointer_offset: int
    input_layout_pointer_offset: int
    rasterizer_state_pointer_offset: int
    blend_state_pointer_offset: int
    depth_stencil_state_pointer_offset: int
    compact_declaration_pointer_offset: int
    resource_binding_pointer_offsets: tuple[int, int, int, int, int]
    resource_metadata_pointer_offsets: tuple[int, int, int, int, int]
    input_signature_size_offset: int
    total_stage_binding_count_offset: int
    per_stage_binding_counts_offset: int
    distinct_resource_count_offsets: tuple[int, int, int, int, int]
    compact_declaration_count_offset: int
    reserved_tail_offset: int
    compute_group_size_offset: int
    primitive_topology_offset: int
    input_layout_header_size: int
    input_element_size: int
    compact_declaration_size: int
    binding_descriptor_size: int
    constant_buffer_metadata_size: int
    constant_variable_size: int
    named_resource_metadata_size: int


@dataclass(frozen=True, slots=True)
class SdfGameProfile:
    game: str
    display_name: str
    aliases: tuple[str, ...]
    resource_versions: frozenset[int]
    record_size: int
    program_layout: SdfProgramLayout
    stages: tuple[SdfStageLayout, ...]
    targets: tuple[SdfTargetLayout, ...]
    semantic_names: dict[int, str] = field(default_factory=dict)
    system_value_names: Mapping[int, str] = field(default_factory=dict)
    component_type_names: Mapping[int, str] = field(default_factory=dict)
    input_formats: Mapping[int, tuple[int, str]] = field(default_factory=dict)
    primitive_topology_names: Mapping[int, str] = field(default_factory=dict)
    resource_type_names: dict[str, dict[int, str]] = field(default_factory=dict)
    default_for_version: bool = False
    binary_editing: bool = False
    metadata_name_hash: Callable[[str], int] | None = None
    rasterizer_state_size: int = 0x10
    blend_state_size: int = 0x24
    depth_stencil_state_size: int = 0x08
    fill_mode_table: tuple[int, ...] = ()
    cull_mode_table: tuple[int, ...] = ()
    blend_factor_table: tuple[int, ...] = ()
    blend_operation_table: tuple[int, ...] = ()
    depth_write_mask_table: tuple[int, ...] = ()
    comparison_function_table: tuple[int, ...] = ()
    stencil_operation_table: tuple[int, ...] = ()
    # ``uniform``: program count at header + 0x06, payload offset at +0x08.
    # ``per-target``: one u16 count per target padded to 8 bytes, then the payload offset.
    header_layout: str = "uniform"
    # 0x110 records insert a reserved qword before the compute group dimensions.
    reserved_record_ranges: tuple[tuple[int, int], ...] = ()
    # RasterizerDesc::mBinnerControl, from the DD2 beta PDB.
    rasterizer_binner_control: int = 0
    rasterizer_binner_control_names: Mapping[int, str] = field(default_factory=dict)
    rasterizer_binner_control_shift: int = 10
    rasterizer_binner_control_bits: int = 2
    conservative_rasterization_supported: bool = False
    program_requirement_names: dict[int, str] = field(default_factory=dict)
    # Target indices that may hold the length-prefixed SPIR-V wrapper.
    spirv_target_indices: frozenset[int] = frozenset()
    # DXIL builds store mesh/amplification shaders in these legacy lanes.
    shader_role_stages: dict[str, frozenset[str]] = field(default_factory=dict)
    # Root-signature/static-sampler resources without SDF rows: (stage, name, register class, bind point, space).
    implicit_resource_bindings: frozenset[
        tuple[str, str, str, int, int]
    ] = frozenset()
    variant_of: str = ""
    # A full 64-KiB field has a zero u16 size in these executable builds.
    constant_variable_size_64k: bool = False
    raytracing_libraries: bool = False
    # Export slots in the 0x140-byte DXR envelope: RE2/RE3 RT use nine, current DD2 four.
    raytracing_export_layout: str = "standard"
    # RE4+ records append a runtime u64 after each shader, input layout and render-state descriptor.
    runtime_cache_keys: bool = False

    def accepts(self, resource_version: int | None) -> bool:
        return resource_version is None or int(resource_version) in self.resource_versions

    def target(self, index: int) -> SdfTargetLayout:
        if index < len(self.targets):
            return self.targets[index]
        return SdfTargetLayout(
            key=f"target-{index}",
            display_name=f"Target {index}",
            selection_capability_bit=None,
        )

    def accepts_binner_control(self, value: int) -> bool:
        if int(value) == self.rasterizer_binner_control:
            return True
        # Shipped programs use non-default states (RE7 culling tests use ForceOff).
        return bool(self.rasterizer_binner_control) and self.rasterizer_binner_control_names.get(int(value)) in BINNER_STATES

    def accepts_shader_stage(self, role: str, stage: str) -> bool:
        actual = str(stage).strip().lower()
        lane = str(role).strip().lower()
        if actual == "unknown":
            return True
        return actual in self.shader_role_stages.get(lane, frozenset((lane,)))


class ProfileRegistry:
    def __init__(self, label: str, variant_of: Callable[[object], str]):
        self.label, self.variant_of = label, variant_of
        self.profiles: dict[str, object] = {}
        self.aliases: dict[str, str] = {}
        self.versions: dict[int, list] = {}

    def checked_key(self, profile) -> str:
        key = game_key(profile.game)
        if not key:
            raise ValueError(f"an {self.label} profile needs a game key")
        if key in self.profiles:
            raise ValueError(f"{self.label} profile {profile.game!r} is already registered")
        if not profile.resource_versions:
            raise ValueError(f"{self.label} profile {profile.game!r} has no resource versions")
        return key

    def add(self, profile, key: str):
        aliases = {normalized for alias in (profile.game, profile.display_name, *profile.aliases)
                   if (normalized := game_key(alias))}
        for alias in aliases:
            owner = self.aliases.get(alias)
            if owner is not None and owner != key:
                raise ValueError(f"{self.label} profile alias {alias!r} is already registered")
        for version in profile.resource_versions:
            existing = self.versions.get(int(version), ())
            if profile.default_for_version and any(p.default_for_version for p in existing):
                raise ValueError(f"{self.label} version {version} has multiple default profiles")
        self.profiles[key] = profile
        for alias in aliases:
            self.aliases[alias] = key
        for version in profile.resource_versions:
            self.versions.setdefault(int(version), []).append(profile)
        return profile

    def for_game(self, game: object):
        key = game_key(game)
        return self.profiles.get(self.aliases.get(key, key))

    def all(self) -> tuple:
        return tuple(self.profiles.values())

    def for_resource(self, resource_version: int | None, *, game: object = ""):
        if game:
            profile = self.for_game(game)
            if profile is None:
                raise ValueError(f"no {self.label} profile is registered for game {game!r}")
            if not profile.accepts(resource_version):
                variants = [p for p in self.versions.get(int(resource_version), ())
                            if game_key(self.variant_of(p)) == game_key(profile.game)]
                if len(variants) == 1:
                    return variants[0]
                raise ValueError(f"{self.label} version {resource_version!r} is not supported by "
                                 f"{profile.display_name}")
            return profile
        if resource_version is not None:
            matches = self.versions.get(int(resource_version), ())
            if not matches:
                raise ValueError(f"unsupported {self.label} resource version: {resource_version}")
            return next((p for p in matches if p.default_for_version), matches[0])
        profiles = self.all()
        if len(profiles) == 1:
            return profiles[0]
        raise ValueError(f"{self.label} profile selection needs a game or resource version")


_REGISTRY = ProfileRegistry("SDF", lambda profile: profile.variant_of)


def register_sdf_profile(profile: SdfGameProfile) -> SdfGameProfile:
    key = _REGISTRY.checked_key(profile)
    if len(profile.stages) != 6:
        raise ValueError(f"SDF profile {profile.game!r} must describe six D3D11 stages")
    layout = profile.program_layout
    if len(layout.resource_binding_pointer_offsets) != 5:
        raise ValueError(f"SDF profile {profile.game!r} must describe five resource groups")
    if len(layout.resource_metadata_pointer_offsets) != 5:
        raise ValueError(f"SDF profile {profile.game!r} must describe five metadata groups")
    if len(layout.distinct_resource_count_offsets) != 5:
        raise ValueError(f"SDF profile {profile.game!r} must describe five resource counts")
    if profile.header_layout not in {"uniform", "per-target"}:
        raise ValueError(
            f"SDF profile {profile.game!r} has invalid header layout "
            f"{profile.header_layout!r}"
        )
    for offset, size in profile.reserved_record_ranges:
        if offset < 0 or size <= 0 or offset + size > profile.record_size:
            raise ValueError(
                f"SDF profile {profile.game!r} has an invalid reserved record range"
            )
    for role, stages in profile.shader_role_stages.items():
        if role not in {stage.key for stage in profile.stages} or not stages:
            raise ValueError(
                f"SDF profile {profile.game!r} has an invalid shader-stage lane"
            )
    for role, name, register_class, bind_point, register_space in (
        profile.implicit_resource_bindings
    ):
        if (
            role not in {stage.key for stage in profile.stages}
            or not name
            or register_class not in {"b", "s", "t", "u"}
            or int(bind_point) < 0
            or int(register_space) < 0
        ):
            raise ValueError(
                f"SDF profile {profile.game!r} has an invalid implicit resource"
            )
    requirement_mask = sum(int(bit) for bit in profile.program_requirement_names)
    if any(
        int(bit) <= 0 or int(bit) & (int(bit) - 1)
        for bit in profile.program_requirement_names
    ) or requirement_mask > 0xFF:
        raise ValueError(
            f"SDF profile {profile.game!r} has invalid compact requirement bits"
        )

    return _REGISTRY.add(profile, key)


def sdf_profile_for_game(game: object) -> SdfGameProfile | None:
    return _REGISTRY.for_game(game)


def sdf_profile_for_resource(resource_version: int | None, *, game: object = "") -> SdfGameProfile:
    return _REGISTRY.for_resource(resource_version, game=game)


__all__ = [
    "SdfGameProfile",
    "SdfProgramLayout",
    "SdfStageLayout",
    "SdfTargetLayout",
    "register_sdf_profile",
    "sdf_profile_for_game",
    "sdf_profile_for_resource",
]
