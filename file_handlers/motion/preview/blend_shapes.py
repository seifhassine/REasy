from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable

lazy import numpy as np

from ..evaluation import DeformationTarget


def mesh_blend_shape_targets(
    mesh,
    property_name_hash: Callable[[str], int] | None,
    *,
    motion_name_key: Callable[[str], Hashable] | None = None,
) -> tuple[DeformationTarget, ...]:
    return blend_shape_targets((mesh,), property_name_hash, motion_name_key=motion_name_key)


def blend_shape_targets(
    meshes: Iterable[object],
    property_name_hash: Callable[[str], int] | None,
    *,
    motion_name_key: Callable[[str], Hashable] | None = None,
) -> tuple[DeformationTarget, ...]:
    if property_name_hash is None and motion_name_key is None:
        return ()
    targets: dict[str, DeformationTarget] = {}
    by_hash: dict[int, str] = {}
    for mesh in meshes:
        blend_shapes = getattr(mesh, "blend_shape_data", None)
        for channel in getattr(blend_shapes, "channels", ()) if blend_shapes else ():
            if not channel.name or not channel.segments or channel.name in targets:
                continue
            target = targets[channel.name] = DeformationTarget(
                binding_key=motion_name_key(channel.name) if motion_name_key is not None else None,
                name=channel.name,
                property_hash=property_name_hash(channel.name) if property_name_hash is not None else None,
            )
            if target.property_hash is None:
                continue
            previous = by_hash.setdefault(target.property_hash, target.name)
            if previous != target.name:
                raise ValueError(
                    f"blendshape names {previous!r} and {target.name!r} "
                    f"share property hash 0x{target.property_hash:08X}"
                )
    return tuple(targets.values())


class MeshBlendShapeDeformer:
    """Apply decoded position deltas and target normals before skinning."""

    def __init__(
        self,
        mesh,
        bind_positions: np.ndarray,
        bind_normals: np.ndarray | None = None,
    ):
        self.bind_positions = np.ascontiguousarray(
            bind_positions,
            dtype=np.float32,
        ).reshape(-1, 3)
        self.bind_normals = (
            np.ascontiguousarray(bind_normals, dtype=np.float32).reshape(-1, 3)
            if bind_normals is not None
            else None
        )
        if (
            self.bind_normals is not None
            and len(self.bind_normals) != len(self.bind_positions)
        ):
            raise ValueError("mesh normals do not match its positions")
        blend_shapes = getattr(mesh, "blend_shape_data", None)
        channels = getattr(blend_shapes, "channels", ()) if blend_shapes else ()
        self._sort_active_weights = mesh.profile is not None and mesh.profile.blend_shapes_by_descending_weight
        self.channels = {
            channel.name: tuple(channel.segments)
            for channel in channels
            if channel.name and channel.segments
        }
        if len(self.channels) != sum(
            bool(channel.name and channel.segments)
            for channel in channels
        ):
            raise ValueError("mesh has duplicate named blendshape channels")
        for name, segments in self.channels.items():
            for segment in segments:
                start = segment.base_vertex_location
                stop = start + len(segment.position_deltas)
                if start < 0 or stop > len(self.bind_positions):
                    raise ValueError(
                        f"blendshape channel {name!r} exceeds the mesh vertices"
                    )
                if (
                    segment.target_normals is not None
                    and np.shape(segment.target_normals)
                    != np.shape(segment.position_deltas)
                ):
                    raise ValueError(
                        f"blendshape channel {name!r} has mismatched normals"
                    )
                if (
                    segment.vertex_cull_mask is not None
                    and np.shape(segment.vertex_cull_mask)
                    != (len(segment.position_deltas),)
                ):
                    raise ValueError(
                        f"blendshape channel {name!r} has a mismatched "
                        "vertex-cull mask"
                    )
        self._weights: tuple[tuple[str, float], ...] | None = None
        self._positions = self.bind_positions
        self._normals = self.bind_normals

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self.channels)

    def deform(
        self,
        weights: Iterable[tuple[str, float]],
    ) -> tuple[np.ndarray, np.ndarray | None]:
        selected = []
        for name, value in weights:
            weight = float(value)
            if name in self.channels and weight:
                selected.append((name, weight))
        if self._sort_active_weights:
            # The game's order (``blend_shapes_by_descending_weight``); ties keep source order, as the native tie-break
            # isn't observable.
            selected.sort(key=lambda item: item[1], reverse=True)
        selected = tuple(selected)
        if selected == self._weights:
            return self._positions, self._normals
        self._weights = selected
        if not selected:
            self._positions = self.bind_positions
            self._normals = self.bind_normals
            return self._positions, self._normals
        positions = self.bind_positions.copy()
        normals = self.bind_normals
        normals_changed = False
        for name, weight in selected:
            if not np.isfinite(weight):
                raise ValueError(
                    f"blendshape channel {name!r} has a nonfinite weight"
                )
            for segment in self.channels[name]:
                start = segment.base_vertex_location
                stop = start + len(segment.position_deltas)
                positions[start:stop] += segment.position_contribution(weight)
                if (
                    segment.vertex_cull_mask is not None
                    and np.any(segment.vertex_cull_mask)
                ):
                    positions[start:stop][segment.vertex_cull_mask] = np.nan
                if (
                    self.bind_normals is not None
                    and segment.target_normals is not None
                ):
                    if not normals_changed:
                        normals = self.bind_normals.copy()
                        normals_changed = True
                    normals[start:stop] += (
                        segment.target_normals
                        - self.bind_normals[start:stop]
                    ) * weight
        if normals_changed:
            lengths = np.linalg.norm(normals, axis=1, keepdims=True)
            np.divide(
                normals,
                lengths,
                out=normals,
                where=lengths > 1e-12,
            )
        self._positions = positions
        self._normals = normals
        return positions, normals
