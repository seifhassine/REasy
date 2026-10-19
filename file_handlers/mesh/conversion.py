from array import array
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path

lazy import numpy as np

from .canonical_writer import CanonicalMeshWriteResult, serialize_mesh
lazy from .contract_utils import all_groups, all_lods, layer_groups
lazy from .conversion_coordinates import transform_coordinates
lazy from .conversion_allocations import make_resident
lazy from .conversion_geometry import (
    detach_topology, materialize_tangents, mesh_payloads, meshlets_to_mesh, retains_meshlets, separate_normal_lods,
    split_large_meshlets,
)
lazy from .conversion_skin import adapt_source_skinning, compact_skin_palette, convert_skin_streams, target_skinning_contract
lazy from .conversion_visibility import materialize_skinning_visibility
lazy from .conversion_morphs import translate_morphs
lazy from .conversion_indices import needs_draw_splitting, split_large_draws
lazy from .conversion_normals import (PARTITION_WORK_LOSS, WORK_ASSIGNMENT_LOSS, materialize_normal_work,
                                 normal_work_needs_rebuild, normal_work_signatures)
lazy from .mesh_file import (
    MESH_MAGIC, MPLY_INTEGER_PLANES, MPLY_MAGIC, MPLY_VECTOR_PLANES, SKIN_WEIGHT_STREAMS, MeshFile, MeshLoadMode,
)
from .skinning_contract import MeshSkinningContract
from .profiles import (
    LegacyLightMapLayout, MeshContentFlagLayout, MeshGameProfile,
    MeshHeaderLayout, NormalRecalcLayout, VertexNormalWSemantic,
    mesh_profile_for_game, mesh_profiles,
)
lazy from .conversion_parts import split_palettes


class MeshConversionError(ValueError):
    pass


class MeshConversionLossError(MeshConversionError):
    def __init__(self, losses):
        self.losses = tuple(losses)
        super().__init__("Conversion needs permission for data loss:\n" + "\n".join(self.losses))


@dataclass(frozen=True)
class MeshConversionPart:
    output: CanonicalMeshWriteResult
    palette: tuple[int, ...]
    geometry: tuple = ()


@dataclass(frozen=True)
class MeshConversionResult:
    profile: MeshGameProfile
    version_pair: tuple[int, int]
    parts: tuple[MeshConversionPart, ...]
    losses: tuple[str, ...] = ()
    requirements: tuple[str, ...] = ()
    target_skinning_contract: MeshSkinningContract | None = None

    @property
    def output(self):
        if len(self.parts) != 1:
            raise MeshConversionError("This conversion has multiple parts; export all result.parts together")
        return self.parts[0].output

    @property
    def file_version(self):
        return self.version_pair[1]

    def suggested_path(self, source):
        path = Path(source)
        stem = path.name.rsplit(".mesh", 1)[0].rsplit(".mply", 1)[0]
        return path.with_name(f"{stem}_{self.profile.game.lower()}.mesh.{self.file_version}")

    def part_paths(self, primary_path):
        path = Path(primary_path)
        stem = path.name.rsplit(".mesh", 1)[0]
        return (path, *(path.with_name(f"{stem}_part{i + 1:02}.mesh.{self.file_version}")
                        for i in range(1, len(self.parts))))


def conversion_targets():
    return tuple((profile, pair) for profile in mesh_profiles()
                 for pair in sorted(profile.canonical_version_pairs))


def _clone(mesh):
    memo = {id(mesh.profile): mesh.profile}
    if mesh.normal_recalc_data is not None:
        memo[id(mesh.normal_recalc_data)] = mesh.normal_recalc_data
    for payload in mesh_payloads(mesh):
        memo[id(payload.attribute_streams)] = []
        memo[id(payload.unknown_attributes)] = {}
        for name in ("normal_recalc_face_words", "normal_recalc_redirect_words",
                     "normal_recalc_runtime_ignored_work_padding_words"):
            values = getattr(payload, name)
            memo[id(values)] = values
    for chunk in mesh.mesh_buffer.mply_chunks:
        for name in (*MPLY_VECTOR_PLANES, *MPLY_INTEGER_PLANES):
            values = getattr(chunk, name)
            memo[id(values)] = values.copy()
    loader = mesh.mesh_buffer._streaming_data_loader
    if loader is not None:
        memo[id(loader)] = None
    return deepcopy(mesh, memo)


def _translate_features(mesh, source, target, loss):
    repack = False
    for payload in mesh_payloads(mesh):
        if not len(payload.colors) and target.canonical_ordinary_requires_colors and not payload.is_auxiliary_geometry:
            payload.colors = array("B", [255]) * (payload.vertex_count * 4)
        supported = target.canonical_vertex_semantics
        if supported is not None:
            for semantic, name in ((5, "colors"), (6, "uv2"), (7, "extra_skin_weights")):
                value = getattr(payload, name)
                if semantic not in supported and (value is not None if semantic == 7 else len(value)):
                    loss(f"{target.display_name} cannot store {name}; removed that plane")
                    setattr(payload, name, None if semantic == 7 else array(value.typecode))
            payload.declared_vertex_semantics = tuple(
                s for s in payload.declared_vertex_semantics if s in supported)
        for name, supported in (("secondary_skin_weights", target.supports_secondary_skin_weights),
                                ("additional_vertex_weights", target.supports_additional_vertex_weights)):
            if getattr(payload, name) is not None and not supported:
                loss(f"{target.display_name} cannot store {name}; removed that weight plane")
                setattr(payload, name, None)
        if target.vertex_normal_w_semantic is not source.vertex_normal_w_semantic:
            if (source.vertex_normal_w_semantic is VertexNormalWSemantic.LOCAL_PIVOT_INDEX
                    and any(payload.normal_ws)):
                loss("Target does not support the source's per-vertex local-pivot selectors")
            payload.normal_ws = array("B", [0]) * payload.vertex_count

    for name in ("shadow_mesh", "occluder_mesh"):
        layer = getattr(mesh, name)
        if layer is not None:
            unsupported = (target.ordinary_vertex_layout_has_no_auxiliary_elements and not layer.shares_main_payload
                           or name == "occluder_mesh" and not target.canonical_supports_occluders)
            if unsupported:
                if not mesh.meshes:
                    raise MeshConversionError(f"{target.display_name} cannot represent this {name}-only resource")
                loss(f"{target.display_name} cannot store independent {name} geometry; removed it")
                setattr(mesh, name, None)
    if target.canonical_single_part_shadow and mesh.shadow_mesh is not None:
        shadow = mesh.shadow_mesh
        if len(shadow.lods) > 1:
            loss("Target supports shadow geometry on LOD0 only; removed higher shadow LODs")
        shadow.lods = shadow.lods[:1]
    for data in (*mesh.meshes, mesh.shadow_mesh):
        if data is not None:
            data.skin_weight_influence_count = target.skin_weight_influence_count
    for group in all_groups(mesh):
        for submesh in group.submeshes:
            if submesh.is_quad and not target.submesh_has_modern_material:
                raise MeshConversionError("Target cannot represent this mesh's quad topology")
    if target.canonical_lod_levels_follow_order:
        assigned, copies = {}, {}
        for data in (*mesh.meshes, mesh.shadow_mesh):
            if data is None:
                continue
            for index, lod in enumerate(data.lods):
                identity = id(lod)
                if identity in assigned and assigned[identity] != index:
                    key = identity, index
                    if key not in copies:
                        copies[key] = deepcopy(lod, {id(mesh.mesh_buffer): mesh.mesh_buffer})
                    lod = data.lods[index] = copies[key]
                    repack |= data is not mesh.shadow_mesh
                else:
                    assigned[identity] = index
                if source.lod_has_level and lod.lod_level not in (0, index):
                    loss("Target uses canonical LOD levels in table order")
                lod.lod_level = index
    for _layer, index, lod in all_lods(mesh):
        if not target.lod_has_level and lod.lod_level not in (0, index):
            loss("Target uses LOD table order instead of explicit LOD levels")
    if repack:
        make_resident(mesh, loss)
    primary = {id(s) for data in mesh.meshes for lod in data.lods
               for group in lod.mesh_groups for s in group.submeshes}
    for layer in (mesh.shadow_mesh, mesh.occluder_mesh):
        if layer is None:
            continue
        groups = layer_groups(layer)
        aliases = {id(s) in primary for group in groups for s in group.submeshes}
        if len(aliases) > 1:
            detach_topology(layer, {id(mesh.mesh_buffer): mesh.mesh_buffer})
    has_part_hashes = (target.meshlet_has_implicit_part_hashes if mesh.header.magic == MPLY_MAGIC
                      else target.header_layout is MeshHeaderLayout.DD2)
    if mesh.parts_name_hashes and not has_part_hashes:
        loss("Target cannot store part-name hashes; part IDs are retained")
        mesh.parts_name_hashes = []
        mesh.parts_name_hash_ids = []
    if mesh.sdf_path and (source.game != target.game or
                         source.sdf_texture_resource_version != target.sdf_texture_resource_version):
        loss("Removed the source distance-field reference; rebake the SDF for the target game")
        mesh.sdf_path = ""
    if not target.canonical_supports_light_maps and any((mesh.header.light_map_used,
            mesh.header.light_map_width, mesh.header.light_map_height)):
        loss("Target cannot store light-map allocation metadata; retained supported UV planes")
        mesh.header.light_map_used = mesh.header.light_map_width = mesh.header.light_map_height = 0
    if (target.legacy_light_map_layout is LegacyLightMapLayout.BYTE_FIELDS
            and max(mesh.header.light_map_width, mesh.header.light_map_height) > 255):
        loss("Light-map dimensions exceed the target's byte fields; removed light-map metadata")
        mesh.header.light_map_used = mesh.header.light_map_width = mesh.header.light_map_height = 0

    flags = mesh.header.decoded_content_flags
    if flags is not None:
        policies = (flags.enable_rebraiding, flags.prefer_opaque,
                    flags.prefer_shadow_cast, flags.prefer_alpha_mask)
        if target.header_layout is MeshHeaderLayout.LEGACY and any((
                flags.use_ray_tracing_vertex_animation, flags.merge_static_mesh_bvh, *policies)):
            loss("Target's legacy header cannot retain extended rendering policy flags")
        elif (target.canonical_ordinary_content_policy is not None
              and any(value and value != target.canonical_ordinary_content_policy for value in policies)):
            loss("Target requires different fixed rendering policy flags")
        # Bit 6 is a light-map UV in these layouts and a tertiary UV in the others.
        lightmap = target.content_flag_layout in (MeshContentFlagLayout.SF6,
                                                MeshContentFlagLayout.WILDS,
                                                MeshContentFlagLayout.DD2_UPDATED)
        if (flags.has_tertiary_uv and lightmap) or (flags.has_lightmap_uv and not lightmap):
            loss("The source UV feature flag has different semantics in the target")
            flags = replace(flags, has_tertiary_uv=False, has_lightmap_uv=False)
        elif source is not target and (flags.has_lightmap_uv or flags.has_tertiary_uv):
            loss("Target rebuilds the UV feature flag from its own vertex and light-map layout")
            flags = replace(flags, has_tertiary_uv=False, has_lightmap_uv=False)
        if (target.content_flag_layout is MeshContentFlagLayout.SF6
                and max(flags.prefer_opaque, flags.prefer_shadow_cast, flags.prefer_alpha_mask) > 1):
            loss("Target reduces two-bit rendering preferences to boolean flags")
        mesh.header.decoded_content_flags = flags


def _precision_losses(expected, actual, loss):
    if expected.header.magic == MPLY_MAGIC:
        before = sorted(expected.mesh_buffer.mply_chunks, key=lambda chunk: chunk.lod_index)
        after = actual.mesh_buffer.mply_chunks
        if expected.mesh_buffer.mply_runtime_settings != actual.mesh_buffer.mply_runtime_settings:
            loss("Target changes meshlet LOD/raster settings")
    else:
        before = [p for p in mesh_payloads(expected) if p.vertex_count]
        after = [p for p in mesh_payloads(actual) if p.vertex_count]
    if len(before) != len(after):
        loss("Target changes the number of decoded geometry payloads")
    for left, right in zip(before, after):
        for name in ("positions", "normals", "tangents", "uv0", "uv1", "uv2", "colors"):
            a = np.asarray(getattr(left, name), dtype=np.float64).ravel()
            b = np.asarray(getattr(right, name), dtype=np.float64).ravel()
            if not len(a):
                continue
            if a.shape != b.shape:
                loss(f"Target cannot retain the complete {name} plane")
            elif not np.array_equal(a, b, equal_nan=True):
                finite = np.isfinite(a) & np.isfinite(b)
                if np.any(~finite & ~(a == b) & ~(np.isnan(a) & np.isnan(b))):
                    replacement = (expected.profile.canonical_mply_uv_infinity_replacements[int(name[-1])]
                                   if expected.header.magic == MPLY_MAGIC and name.startswith("uv") else None)
                    if replacement is None:
                        raise MeshConversionError(f"Target unexpectedly changed non-finite {name} values")
                    loss(f"Target replaces non-finite {name} values with canonical {replacement:g}")
                if np.any(a[finite] != b[finite]):
                    error = float(np.max(np.abs(a[finite] - b[finite]), initial=0))
                    loss(f"Target quantizes {name} (maximum finite component error {error:.9g})")
        before_w = np.maximum(np.asarray(left.tangent_ws, dtype=np.uint8).view(np.int8), -127)
        after_w = np.maximum(np.asarray(right.tangent_ws, dtype=np.uint8).view(np.int8), -127)
        if not np.array_equal(before_w, after_w):
            loss("Target changes tangent handedness")
        for name in SKIN_WEIGHT_STREAMS:
            a, b = getattr(left, name, None), getattr(right, name, None)
            if a is None:
                continue
            if b is None or a.influence_count != b.influence_count or a.vertex_count != b.vertex_count:
                loss(f"Target cannot retain the complete {name} layout")
                continue
            delta = np.abs(np.asarray(a.weights) - np.asarray(b.weights))
            if np.any(delta > 1e-7):
                loss(f"Target quantizes {name} (maximum weight error {float(delta.max()):.9g})")
    if expected.blend_shape_data is not None and actual.blend_shape_data is not None:
        for left, right in zip(expected.blend_shape_data.bodies, actual.blend_shape_data.bodies):
            channels = [channel for channel in left.channels if channel.segments]
            if len(channels) != len(right.channels):
                loss("Target cannot retain all morph channels")
            for a, b in zip(channels, right.channels):
                if (a.name, a.index) != (b.name, b.index):
                    loss("Target changes morph channel identities")
                for src, dst in zip(a.segments, b.segments):
                    if expected.normal_recalc_data is None and src.target_normals is not None:
                        if dst.target_normals is None or not np.array_equal(src.target_normals, dst.target_normals):
                            loss("Target quantizes morph target normals")


def convert_mesh(mesh: MeshFile, target: MeshGameProfile | str, *,
                 version_pair: tuple[int, int] | None = None,
                 allow_lossy: bool = False, axes: tuple[int, int, int] = (1, 2, 3),
                 output_kind: str = "auto", source_skinning_contract=None,
                 target_influence_count: int | None = None,
                 palette_policy: str = "split") -> MeshConversionResult:
    if not isinstance(mesh, MeshFile) or mesh.load_mode is not MeshLoadMode.FULL:
        raise MeshConversionError("Conversion requires a fully loaded MeshFile")
    source = mesh.profile
    if source is None or not source.canonical_version_pairs or mesh.mesh_buffer is None:
        raise MeshConversionError("Source has no fully verified mesh conversion contract")
    if not any(internal == mesh.header.version for internal, _resource in source.canonical_version_pairs):
        raise MeshConversionError("Source version has no verified conversion contract")
    if (mesh.header.magic == MESH_MAGIC and mesh.streaming_buffer_count and not mesh.streaming_data_loaded
            and any(entry.size for entry in mesh.streaming_info.entries)):
        raise MeshConversionError("Load the missing streaming companion before conversion")
    if isinstance(target, str):
        target = mesh_profile_for_game(target)
    if target is None or not target.canonical_version_pairs:
        raise MeshConversionError("Target has no verified canonical mesh writer")
    if version_pair is None:
        version_pair = max(target.canonical_version_pairs, key=lambda pair: (pair[1], pair[0]))
    if version_pair not in target.canonical_version_pairs:
        raise MeshConversionError("Target version pair has no verified canonical mesh writer")
    if output_kind not in ("auto", "mesh"):
        raise MeshConversionError("Output kind must be 'auto' or 'mesh'")
    if palette_policy not in ("split", "single"):
        raise MeshConversionError("Palette policy must be 'split' or 'single'")
    losses = []
    requirements = []
    def loss(message):
        if message not in losses:
            losses.append(message)
    def requirement(message):
        if message not in requirements:
            requirements.append(message)
    for payload in mesh_payloads(mesh):
        if payload.unknown_attributes:
            raise MeshConversionError("Unknown vertex attributes cannot be converted safely")
    converted = _clone(mesh)
    try:
        adapt_source_skinning(converted, source_skinning_contract)
        materialize_skinning_visibility(converted, loss)
        axes = tuple(axes)
        transform_coordinates(converted, axes)
        if axes != (1, 2, 3) and converted.sdf_path:
            loss("Removed the distance-field reference after changing coordinates; rebake the SDF")
            converted.sdf_path = ""
        is_mply = mesh.header.magic == MPLY_MAGIC
        expand = is_mply and not retains_meshlets(converted, target, output_kind)
        if expand:
            meshlets_to_mesh(converted, loss)
            flags = converted.header.decoded_content_flags
            if flags is not None:
                converted.header.decoded_content_flags = replace(flags, streaming_bvh=False)
        elif is_mply:
            split_large_meshlets(converted, target, loss)
            for chunk in converted.mesh_buffer.mply_chunks:
                if not len(chunk.colors) and target.canonical_mply_requires_colors:
                    chunk.colors = np.full(chunk.vert_count * 4, 255, np.int64)
                had_tangents = not chunk.flags.is_meshlet_no_tangent if chunk.flags else bool(len(chunk.tangents))
                if (target.canonical_mply_stores_tangents is not None
                        and had_tangents != target.canonical_mply_stores_tangents):
                    loss("Target changes meshlet tangents between stored vectors and runtime reconstruction")
                if target.canonical_mply_stores_tangents:
                    materialize_tangents(chunk, loss)
        if converted.header.magic == MESH_MAGIC:
            overflow = (target.canonical_buffer_count_limit is not None
                        and converted.streaming_buffer_count + 1 > target.canonical_buffer_count_limit)
            aliases = source is not target and bool(converted.mesh_buffer.streaming_payload_aliases)
            if not target.supports_streaming_buffers or overflow or aliases:
                make_resident(converted, loss)
                if overflow:
                    loss("Combined streaming buffers to fit the target's buffer-count limit")
        if target.normal_recalc_layout is NormalRecalcLayout.RE8:
            materialize_normal_work(converted)
        normal_work = normal_work_signatures(converted)
        source_work_needs_rebuild = normal_work_needs_rebuild(converted)
        _translate_features(converted, source, target, loss)
        translate_morphs(converted, source, target, loss)
        partition_normals = (target.normal_recalc_layout is NormalRecalcLayout.MHWILDS
                             and normal_work_needs_rebuild(converted, check_slots=False))
        partition_indices = (converted.header.magic == MESH_MAGIC
                             and (converted.blend_shape_data is not None or converted.normal_recalc_data is not None)
                             and needs_draw_splitting(converted, target))
        meshlets = converted.header.magic == MPLY_MAGIC
        owners = converted.mesh_buffer.mply_chunks if meshlets else mesh_payloads(converted)
        convert_skin_streams(converted, owners, target, loss, meshlets=meshlets,
                             requirement=requirement, target_influence_count=target_influence_count,
                             defer_palette=palette_policy == "split")
        parts = [(converted, ())]
        if len(converted.bone_remap_indices) > target.max_deform_bones or partition_normals or partition_indices:
            if meshlets:
                meshlets_to_mesh(converted, loss)
            if source_work_needs_rebuild:
                loss(PARTITION_WORK_LOSS)
            parts = split_palettes(converted, target, loss)
            if len(parts) > 1:
                if palette_policy == "single":
                    raise MeshConversionError("Target draw limits require multiple mesh files; enable automatic splitting")
                binding = ("on the same skeleton; bind matching joint names and synchronize animation, morphs and LOD selection"
                           if converted.bones else "with the same transform; synchronize morphs and LOD selection")
                requirement(f"Load all {len(parts)} mesh parts together {binding}. "
                            "Register every part in the target character/prefab; copying the files alone is insufficient")
            if any(g.support_triangles for _, geometry in parts for g in geometry):
                requirement("Keep the zero-area normal-support triangles; removing them can introduce shading seams")
        outputs = []
        contract = target_skinning_contract(converted, target, influence_count=target_influence_count)
        for converted, geometry in parts:
            if geometry:
                compact_skin_palette(converted, mesh_payloads(converted), target)
                normal_work = normal_work_signatures(converted)
                translate_morphs(converted, target, target, loss)
            if converted.header.magic == MESH_MAGIC:
                split_large_draws(converted, target, loss)
            output = _write_converted(converted, target, version_pair, contract, normal_work, loss)
            outputs.append(MeshConversionPart(output, tuple(converted.bone_remap_indices), geometry))
        if losses and not allow_lossy:
            raise MeshConversionLossError(losses)
        return MeshConversionResult(target, version_pair, tuple(outputs), tuple(losses), tuple(requirements), contract)
    except MeshConversionError:
        raise
    except (ValueError, OverflowError) as exc:
        raise MeshConversionError(f"Cannot convert to {target.display_name}: {exc}") from exc


def _write_converted(converted, target, version_pair, contract, normal_work, loss):
    if target.canonical_single_part_shadow and converted.shadow_mesh is not None:
        shadow = converted.shadow_mesh
        if any(s.buffer_index for lod in shadow.lods for g in lod.mesh_groups for s in g.submeshes):
            make_resident(converted, loss)
        groups = shadow.lods[0].mesh_groups
        if len(groups) != 1 or groups[0].group_id != 0:
            shadow.lods = deepcopy(shadow.lods, {id(converted.mesh_buffer): converted.mesh_buffer})
            groups = shadow.lods[0].mesh_groups
            loss("Target combines shadow parts under part ID 0")
            groups[0].submeshes = [*group.submeshes for group in groups]
            groups[0].vertex_count = sum(group.vertex_count for group in groups)
            groups[0].group_id = 0
            shadow.lods[0].mesh_groups = groups[:1]
    if (converted.header.magic == MESH_MAGIC and converted.normal_recalc_data is not None
            and target.normal_recalc_layout is NormalRecalcLayout.MHWILDS):
        separate_normal_lods(converted)
    converted.header.profile = converted.mesh_buffer.profile = target
    converted.header.format_version = target.format_version
    converted.header.version = version_pair[0]
    output = serialize_mesh(converted)
    rebuilt = MeshFile()
    rebuilt.read(output.main, file_version=version_pair[1], streaming_data=output.streaming)
    if rebuilt.material_names != converted.material_names:
        raise MeshConversionError("Target output changed material assignments")
    adapt_source_skinning(rebuilt, contract)
    _precision_losses(converted, rebuilt, loss)
    rebuilt_work = normal_work_signatures(rebuilt)
    if any(signature != rebuilt_work[index] for index, signature in normal_work.items() if index in rebuilt_work):
        loss(WORK_ASSIGNMENT_LOSS)
    return output

__all__ = ["MeshConversionError", "MeshConversionLossError", "MeshConversionResult",
           "conversion_targets", "convert_mesh"]
