from __future__ import annotations

from enum import Enum, IntEnum

from utils.game_keys import game_key
lazy from file_handlers.mmtr.profiles import mmtr_profile_for_game


class MeshMainVersion(IntEnum):
    UNKNOWN = 0
    RE7 = 1
    DMC5 = 2
    RE8 = 3
    RE_RT = 4
    RE4 = 5
    SF6 = 6
    DD2_OLD = 7
    KUNITSUGAMI = 8
    DD2 = 9
    ONIMUSHA = 10
    MHWILDS = 11
    PRAGMATA = 13
    RE9 = 14
    ONIMUSHA_WOTS = 15
    DD2_UPDATED = 16
    ONIMUSHA_WOTS_UPDATED = 17


class MeshHeaderLayout(Enum):
    LEGACY = "legacy"
    RE4 = "re4"
    DD2 = "dd2"
    WILDS = "wilds"


class MeshContentFlagLayout(Enum):
    GENERIC = "generic"
    RE8 = "re8"
    SF6 = "sf6"
    WILDS = "wilds"
    DD2_UPDATED = "dd2_updated"


class LegacyLightMapLayout(Enum):
    BYTE_FIELDS = "byte_fields"
    RE8 = "re8"


class MeshBufferLayout(Enum):
    DMC5 = "dmc5"
    RE8 = "re8"
    RE_RT = "re_rt"
    RE4 = "re4"
    DD2 = "dd2"
    WILDS = "wilds"
    PRAGMATA = "pragmata"


class BlendShapeLayout(Enum):
    LEGACY = "legacy"
    RE8 = "re8"
    RE_RT = "re_rt"
    RE2_RT = "re2_rt"
    MHRISE = "mhrise"
    RE4 = "re4"
    SF6 = "sf6"
    EXTENDED = "extended"

    @property
    def has_lod_maps(self) -> bool:
        return self in (BlendShapeLayout.RE_RT, BlendShapeLayout.RE2_RT, BlendShapeLayout.MHRISE)


class NormalRecalcLayout(Enum):
    NONE = "none"
    DMC5 = "dmc5"
    RE2 = "re2"
    RE3 = "re3"
    RE8 = "re8"
    RE_RT = "re_rt"
    RE2_RT = "re2_rt"
    RE3_RT = "re3_rt"
    MHRISE = "mhrise"
    RE4 = "re4"
    SF6 = "sf6"
    MHWILDS = "mhwilds"
    RE9 = "re9"
    ONIMUSHA_WOTS = "onimusha_wots"
    DD2_UPDATED = "dd2_updated"


class SkinWeightLayout(Enum):
    BYTE8 = "byte8"
    PACKED_10BIT_6 = "packed_10bit_6"


class MeshletLodFactorEncoding(Enum):
    FLOAT32 = "float32"
    CPU_UNUSED = "cpu_unused"
    PACKED_BINARY16 = "packed_binary16"


class Half2ValueDomain(Enum):
    FINITE = "finite"
    FINITE_OR_INFINITY = "finite_or_infinity"
    FINITE_OR_NAN = "finite_or_nan"
    IEEE754 = "ieee754"


class MaterialSkinningLayout(Enum):
    NONE = "none"
    LEGACY_DXBC_RESIDUAL_BYTE8 = "legacy_dxbc_residual_byte8"
    LATE_DXIL_PACKED_10BIT = "late_dxil_packed_10bit"


class VertexNormalWSemantic(Enum):
    UNINTERPRETED = "uninterpreted"
    LOCAL_PIVOT_INDEX = "local_pivot_index"


class MeshGameProfile:
    game = ""
    sdf_texture_resource_version: int | None = None
    stmesh_version: int | None = None
    registration_key = ""
    display_name = ""
    aliases: tuple[str, ...] = ()
    format_version = MeshMainVersion.UNKNOWN
    version_pairs: frozenset[tuple[int, int]] = frozenset()
    read_only_version_pairs: frozenset[tuple[int, int]] = frozenset()
    primary_version_pairs: frozenset[tuple[int, int]] = frozenset()
    default_for_format = False

    header_layout = MeshHeaderLayout.LEGACY
    content_flag_layout = MeshContentFlagLayout.GENERIC
    content_uv_flag_uses_light_map_metadata = False
    legacy_light_map_layout = LegacyLightMapLayout.BYTE_FIELDS
    buffer_layout = MeshBufferLayout.DMC5
    blend_shape_layout = BlendShapeLayout.LEGACY
    # The renderer applies a mesh's active blend shapes in descending weight (DMC5's bWeightList), not channel order.
    blend_shapes_by_descending_weight = False
    normal_recalc_layout = NormalRecalcLayout.NONE
    normal_recalc_section_alignment = 8
    # DMC5 emits one aligned qword between MeshHeader and the first section.
    header_trailing_padding = 0
    skin_weight_layout = SkinWeightLayout.BYTE8
    material_skinning_layout = MaterialSkinningLayout.NONE
    normal_compute_uses_packed_skin_width = False
    supports_packed_rigid_skinning = False
    @property
    def material_shader_resource_version(self) -> int | None:
        profile = mmtr_profile_for_game(self.game)
        return profile.build_resource_version if profile is not None else None

    supports_streaming_buffers = False
    streaming_header_size = 64
    streaming_header_has_extended_prefix = False
    streaming_header_has_wilds_tail = False
    skip_empty_stream_entries = False
    has_shape_key_weight_pointer = False
    has_shape_key_weight_size = False
    has_auxiliary_index_offsets = False
    has_rt_buffer_padding = False
    has_split_legacy_buffer_sizes = False
    legacy_buffer_sizes_are_vertex_and_index = False
    has_wilds_buffer_tail = False
    additional_vertex_weights_size_in_modern_tail = False
    header_buffer_count_bits = 0
    submesh_has_modern_material = False
    submesh_has_buffer_index = False
    submesh_has_streaming_offsets = False
    submesh_has_ukn1 = False
    submesh_has_wilds_tail = False
    submesh_has_runtime_ignored_tail_residue = False
    submesh_reserve03_is_runtime_ignored = False
    # MeshGroup::indexCount includes the padding index.
    submesh_index_ranges_are_even_aligned = False
    auxiliary_index_offsets_are_boundaries = False
    auxiliary_index_domain_end_marks_main_payload_alias = False
    lod_has_level = False
    ordinary_lod_factor_supports_nan = False
    normal_recalc_has_split_vertex_page_plane = False
    mesh_data_has_legacy_unknown = True
    mesh_data_has_modern_bounds = False
    shadow_has_legacy_pad = True
    shadow_has_reserved_slots = True
    supports_secondary_skin_weights = False
    supports_additional_vertex_weights = False
    legacy_flags_are_content_flags = False
    legacy_reserved_storage_is_corpus_zero = False
    vertex_normal_w_semantic = VertexNormalWSemantic.UNINTERPRETED
    auxiliary_vertex_layout_order: tuple[str, ...] = ()
    auxiliary_topology_sharing_uses_lod_pointer_identity = False
    auxiliary_vertex_planes_may_omit_zero_suffix = False
    skin_weight_uses_implicit_last_weight = False
    skin_weight_count_selects_influence_layout = False
    skin_weight_influences_by_raw: tuple[tuple[int, int], ...] = ()
    name_string_data_alignment = 1
    skeleton_hierarchy_alignment = 8
    canonical_bounding_box_w = 0.0
    # RE4-family optional GPU fields are relative to vertexBufferOffset, so null is stored as -vertexBufferOffset.
    optional_gpu_offsets_use_relative_null = False
    group_pivot_payload_is_trailing = False
    blend_shape_standard_w_is_padding = False
    blend_shape_standard_w_is_runtime_ignored = False
    blend_shape_normal_w_is_padding = False
    blend_shape_normal_w_is_runtime_ignored = False
    blend_shape_aabb_w_is_runtime_ignored = False
    blend_shape_deform_percent_is_runtime_ignored = False
    blend_shape_nan_positions_cull_vertices = False
    blend_shape_type3_is_enum_sentinel = False
    blend_shape_range_has_part_surface_identity = False
    blend_shape_range_identity_is_corpus_zero = False
    # WotS BlendShapeType 3: three bytes decoded as min + (q + 0.5) * (max - min) / 255.
    blend_shape_u8x3_uses_aabb_quantization = False
    blend_shape_reserved_storage_is_inert = False
    blend_shape_layout_tail_is_producer_residue = False
    normal_redirect_annotation_is_runtime_semantic = False
    resident_buffer_tail_is_corpus_zero = False
    streaming_buffer_tail_is_corpus_zero = False
    streaming_buffer_index_is_one_based = False
    content_buffer_count_includes_resident = False
    content_buffer_count_can_wrap = True

    def streaming_layout_count(self, entries) -> int:
        return sum(not self.skip_empty_stream_entries or entry.size > 0
                   for entry in entries)

    shape_key_weight_pointer_is_corpus_zero = False
    ordinary_vertex_layout_has_no_auxiliary_elements = False
    meshlet_position_alignment_padding_is_corpus_zero = False

    canonical_import_contract: str | None = None
    canonical_vertex_semantics: frozenset[int] | None = None
    canonical_ordinary_supports_32bit_indices = True
    canonical_ordinary_requires_colors = False
    canonical_mply_requires_colors = False
    canonical_mply_supports_skinning = True
    canonical_mply_supports_skeleton = True
    canonical_mply_supports_pivots = True
    canonical_mply_supports_uv2 = True
    canonical_mply_supports_render_flags = True
    canonical_mply_supports_material_extension = True
    canonical_mply_requires_shared_skinning = False
    canonical_mply_raster_thresholds: frozenset[int] | None = None
    canonical_supports_light_maps = False
    canonical_supports_occluders = True
    canonical_single_channel_morph_targets = False
    canonical_lod_levels_follow_order = False
    canonical_single_part_shadow = False
    canonical_buffer_count_limit: int | None = None
    authored_blend_shape_codecs = (0, 1, 2)
    blend_shape_vertex_offsets_are_lod_relative = False

    @property
    def canonical_version_pairs(self) -> frozenset[tuple[int, int]]:
        if not self.canonical_import_contract:
            return frozenset()
        return self.version_pairs - self.read_only_version_pairs

    canonical_ordinary_content_policy: int | None = None
    canonical_modern_aabb_padding = 0
    canonical_mply_stores_tangents: bool | None = None
    canonical_ordinary_uv_half2_domains = (Half2ValueDomain.FINITE,) * 3
    canonical_mply_uv_half2_domains = (Half2ValueDomain.FINITE,) * 3
    canonical_mply_uv_infinity_replacements: tuple[
        float | None,
        float | None,
        float | None,
    ] = (None, None, None)
    canonical_mply_raster_switch_threshold: int | None = None

    meshlet_uses_legacy_flag_bits = False
    meshlet_uses_pragmata_flag_bits = False
    meshlet_has_wilds_layout = False
    meshlet_extension_has_skin_weight_count = False
    meshlet_streaming_indices_exclude_no_ray_tracing = False
    meshlet_has_extended_indices = False
    meshlet_lod_factor_encoding = MeshletLodFactorEncoding.FLOAT32
    meshlet_bvh_scale_exponent_is_signed = False
    meshlet_uses_centered_exponent_position_decode = False
    meshlet_uses_dual_exponent_position_decode = False
    # Wilds tangent sign in flag bits 14-15: 0 positive, 1 negative, 2 per-vertex bits after the normals.
    meshlet_uses_mhwilds_tangent_handedness = False
    meshlet_skin_before_texcoord3 = False
    mply_material_count_uses_name_partition = False
    meshlet_chunk_payload_is_fully_decoded = False
    meshlet_has_implicit_part_hashes = False
    # The most triangles and vertices of a meshlet the game draws (no shipped chunk is larger).
    meshlet_max_triangles: int | None = None
    meshlet_max_vertices: int | None = None

    @property
    def skin_weight_influence_count(self) -> int:
        return (
            6
            if self.skin_weight_layout is SkinWeightLayout.PACKED_10BIT_6
            else 8
        )

    @property
    def max_deform_bones(self) -> int:
        return 256 if self.skin_weight_layout is SkinWeightLayout.BYTE8 else 1024

    def mesh_data_skin_weight_influence_count(self, raw: int) -> int:
        raw = int(raw) & 0xFF
        mapping = dict(self.skin_weight_influences_by_raw)
        if mapping:
            exact = mapping.get(raw)
            if exact is not None:
                return int(exact)
            canonical = {
                int(stored) & 0x7: int(influences)
                for stored, influences in mapping.items()
            }
            return int(canonical.get(raw, 0))
        return {
            0: 4,
            1: 6,
            2: 8,
            3: 12,
        }.get(raw & 0x7, 0)

    def canonical_skin_weight_count(self, influence_count: int) -> int:
        influence_count = int(influence_count)
        matches = {
            int(stored)
            for stored, influences in self.skin_weight_influences_by_raw
            if int(influences) == influence_count
        }
        if len(matches) == 1:
            return matches.pop()
        generic = {4: 0, 6: 1, 8: 2, 12: 3}
        if not self.skin_weight_influences_by_raw and influence_count in generic:
            return generic[influence_count]
        raise ValueError(
            f"{self.display_name} has no unique canonical SkinWeightCount "
            f"for {influence_count} influences"
        )

    @property
    def has_material_skinning_contract(self) -> bool:
        return (
            self.material_skinning_layout is not MaterialSkinningLayout.NONE
            and self.material_shader_resource_version is not None
        )

    @property
    def has_modern_blend_shape_targets(self) -> bool:
        return self.blend_shape_layout in {
            BlendShapeLayout.RE4,
            BlendShapeLayout.SF6,
            BlendShapeLayout.EXTENDED,
        }

    @property
    def has_extended_blend_shape_layout(self) -> bool:
        return self.blend_shape_layout is BlendShapeLayout.EXTENDED

    @property
    def has_blend_shape_lod_maps(self) -> bool:
        return self.blend_shape_layout in {
            BlendShapeLayout.RE_RT,
            BlendShapeLayout.RE2_RT,
            BlendShapeLayout.MHRISE,
            BlendShapeLayout.RE4,
            BlendShapeLayout.SF6,
            BlendShapeLayout.EXTENDED,
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}(game={self.game!r})"


_PROFILES: dict[str, MeshGameProfile] = {}
_ALIASES: dict[str, str] = {}
_PAIR_PROFILES: dict[tuple[int, int], list[MeshGameProfile]] = {}
_PRIMARY_PAIR_PROFILES: dict[tuple[int, int], MeshGameProfile] = {}


def register_mesh_profile(profile: MeshGameProfile) -> MeshGameProfile:
    key = game_key(type(profile).__dict__.get("registration_key") or profile.game)
    if not key:
        raise ValueError("A mesh profile must have a game key")
    if profile.format_version is MeshMainVersion.UNKNOWN:
        raise ValueError(f"Mesh profile {profile.game} has no format version")
    if not profile.version_pairs:
        raise ValueError(f"Mesh profile {profile.game} has no version pairs")
    invalid_primary_pairs = profile.primary_version_pairs - profile.version_pairs
    if invalid_primary_pairs:
        raise ValueError(
            f"Mesh profile {profile.game} marks unsupported primary pairs: "
            f"{sorted(invalid_primary_pairs)}"
        )
    if key in _PROFILES:
        raise ValueError(f"Mesh profile {profile.game!r} is already registered")
    for pair in profile.version_pairs:
        existing = _PAIR_PROFILES.get(pair, ())
        if any(item.format_version is not profile.format_version for item in existing):
            raise ValueError(
                f"MESH version pair {pair} is claimed by incompatible formats"
            )
        if profile.default_for_format and any(
            item.default_for_format for item in existing
        ):
            raise ValueError(
                f"MESH version pair {pair} has multiple format defaults"
            )
    for pair in profile.primary_version_pairs:
        owner = _PRIMARY_PAIR_PROFILES.get(pair)
        if owner is not None:
            raise ValueError(
                f"MESH version pair {pair} is already primarily owned by "
                f"{owner.game}"
            )
    normalized_aliases: dict[str, str] = {}
    game_alias = (profile.game,) if key == game_key(profile.game) else ()
    for alias in (key, *game_alias, profile.display_name, *profile.aliases):
        normalized = game_key(alias)
        if normalized:
            normalized_aliases.setdefault(normalized, str(alias))
    for normalized, alias in normalized_aliases.items():
        owner = _ALIASES.get(normalized)
        if owner is not None and owner != key:
            raise ValueError(
                f"Mesh profile alias {alias!r} is already owned by {owner}"
            )
    _PROFILES[key] = profile
    for normalized in normalized_aliases:
        _ALIASES[normalized] = key
    for pair in profile.version_pairs:
        _PAIR_PROFILES.setdefault(pair, []).append(profile)
    for pair in profile.primary_version_pairs:
        _PRIMARY_PAIR_PROFILES[pair] = profile
    return profile


def mesh_profile_for_game(game: object) -> MeshGameProfile | None:
    key = game_key(game)
    return _PROFILES.get(_ALIASES.get(key, key))


def mesh_profiles() -> tuple[MeshGameProfile, ...]:
    return tuple(_PROFILES.values())


def mesh_profile_for_stmesh_version(version: int) -> MeshGameProfile | None:
    matches = [profile for profile in _PROFILES.values() if profile.stmesh_version == int(version)]
    if len(matches) > 1:
        raise ValueError(f"SpeedTree mesh version {version} is claimed by {', '.join(p.game for p in matches)}")
    return matches[0] if matches else None


def mesh_profile_for_versions(
    internal_version: int,
    file_version: int,
) -> MeshGameProfile:
    pair = int(internal_version), int(file_version)
    matches = _PAIR_PROFILES.get(pair, ())
    if not matches:
        raise ValueError(f"Unsupported MESH version pair: {pair}")
    if primary := _PRIMARY_PAIR_PROFILES.get(pair):
        return primary
    if len(matches) == 1:
        return matches[0]
    raise ValueError(
        f"Ambiguous MESH version pair {pair}; register one primary pair profile"
    )


def mesh_profile_for_main_version(version: MeshMainVersion) -> MeshGameProfile:
    matches = [
        profile
        for profile in _PROFILES.values()
        if profile.format_version is MeshMainVersion(version)
    ]
    if not matches:
        raise ValueError(f"No mesh profile is registered for {version!r}")
    return next(
        (profile for profile in matches if profile.default_for_format),
        matches[0],
    )


def coerce_mesh_profile(
    value: MeshGameProfile | MeshMainVersion | int,
) -> MeshGameProfile:
    if isinstance(value, MeshGameProfile):
        return value
    return mesh_profile_for_main_version(MeshMainVersion(value))


__all__ = [
    "BlendShapeLayout",
    "Half2ValueDomain",
    "LegacyLightMapLayout",
    "MeshBufferLayout",
    "MeshContentFlagLayout",
    "MeshGameProfile",
    "MeshHeaderLayout",
    "MeshMainVersion",
    "MeshletLodFactorEncoding",
    "MaterialSkinningLayout",
    "NormalRecalcLayout",
    "SkinWeightLayout",
    "VertexNormalWSemantic",
    "coerce_mesh_profile",
    "mesh_profile_for_game",
    "mesh_profile_for_main_version",
    "mesh_profile_for_versions",
    "mesh_profiles",
    "register_mesh_profile",
]
