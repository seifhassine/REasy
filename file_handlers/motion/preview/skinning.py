from __future__ import annotations

import numpy as np

from file_handlers.mesh.skinning_contract import (
    MeshSkinningContract,
    MeshSkinningContractError,
    resolve_mesh_skinning_contract,
)
from ui.scene.mesh_scene import (
    MeshScenePayload,
    mesh_lod0_submeshes,
    mesh_scene_payloads,
)
from ui.scene.scene_model import SceneSkinningBinding
from utils.native_build import ensure_fastmesh

from ..evaluation.model import Rig
from ..evaluation.shared_rig import SharedRigPoseMapper
from .blend_shapes import MeshBlendShapeDeformer
from .model import MotionPreviewSnapshot
from .normal_recalc import MeshNormalRecalculator


_fastmesh = ensure_fastmesh()
if _fastmesh is None or not hasattr(_fastmesh, "skin_vertices"):
    raise ImportError("fastmesh was built without motion-preview skinning support")
skin_vertices = _fastmesh.skin_vertices


class SkinningError(ValueError):
    pass


def shared_rig_model_space_transform(
    pose_space_matrix: np.ndarray,
    mesh_model_matrix: np.ndarray,
) -> np.ndarray:
    """Map row-vector shared poses into a mesh's model space."""
    try:
        pose_space = np.asarray(
            pose_space_matrix,
            dtype=np.float64,
        ).reshape(4, 4)
        mesh_model = np.asarray(
            mesh_model_matrix,
            dtype=np.float64,
        ).reshape(4, 4)
    except ValueError as exc:
        raise SkinningError("shared-rig model transforms must be 4x4 matrices") from exc
    if not np.isfinite(pose_space).all() or not np.isfinite(mesh_model).all():
        raise SkinningError("shared-rig model transforms contain a non-finite value")
    try:
        # SCN matrices use column vectors. M^-1 P maps shared-pose points into
        # mesh-model coordinates; transpose it for the evaluator's
        # row-vector matrices, where the conversion is post-multiplied.
        conversion = np.linalg.solve(mesh_model, pose_space).T
    except np.linalg.LinAlgError as exc:
        raise SkinningError("shared-rig mesh model transform is singular") from exc
    if not np.isfinite(conversion).all():
        raise SkinningError("shared-rig model-space conversion is non-finite")
    return conversion.astype(np.float32)


def build_skinned_mesh_deformer(
    mesh,
    rig: Rig,
    bind_positions: np.ndarray,
    bind_normals: np.ndarray | None,
    bind_indices: np.ndarray | None = None,
    *,
    handler=None,
    explicit_mdf_path: str = "",
) -> "SkinnedMeshDeformer":
    try:
        contract = resolve_mesh_skinning_contract(
            mesh,
            handler,
            explicit_mdf_path=explicit_mdf_path,
        )
    except MeshSkinningContractError as exc:
        raise SkinningError(str(exc)) from exc
    return SkinnedMeshDeformer(
        mesh,
        rig,
        mesh_scene_payloads(mesh),
        bind_positions,
        bind_normals,
        bind_indices,
        skinning_contract=contract,
    )


def build_shared_rig_deformer(
    mesh,
    constrained_rig: Rig,
    owner_rig: Rig,
    bind_positions: np.ndarray,
    bind_normals: np.ndarray | None,
    bind_indices: np.ndarray | None = None,
    *,
    pose_to_constrained_matrix: np.ndarray,
    handler=None,
    explicit_mdf_path: str = "",
) -> "SharedRigSkinningDeformer":
    return SharedRigSkinningDeformer(
        build_skinned_mesh_deformer(
            mesh,
            constrained_rig,
            bind_positions,
            bind_normals,
            bind_indices,
            handler=handler,
            explicit_mdf_path=explicit_mdf_path,
        ),
        SharedRigPoseMapper(
            owner_rig,
            constrained_rig,
            pose_to_constrained_matrix,
        ),
    )


class SharedRigSkinningDeformer:
    """Skin a constrained mesh from its owner's evaluated joint pose."""

    def __init__(
        self,
        deformer: "SkinnedMeshDeformer",
        pose_mapper: SharedRigPoseMapper,
    ):
        self.deformer = deformer
        self.pose_mapper = pose_mapper

    def deform(
        self,
        snapshot: MotionPreviewSnapshot,
    ) -> tuple[np.ndarray, np.ndarray | None]:
        positions, normals = self.deformer.source_geometry(
            getattr(snapshot, "deformation_weights", ())
        )
        return self.deformer.deform_matrices(
            self.skin_matrices(snapshot),
            positions=positions,
            normals=normals,
        )

    @property
    def binding(self) -> SceneSkinningBinding:
        return self.deformer.binding

    @property
    def requires_post_skin_normals(self) -> bool:
        return self.deformer.requires_post_skin_normals

    def skin_matrices(self, snapshot: MotionPreviewSnapshot) -> np.ndarray:
        return self.pose_mapper.skin_matrices(
            snapshot.pose.world_matrices,
            snapshot.root_deltas,
        )

    def source_geometry(
        self,
        deformation_weights,
    ) -> tuple[np.ndarray, np.ndarray | None]:
        return self.deformer.source_geometry(deformation_weights)


class SkinnedMeshDeformer:
    """CPU linear-blend skinning over modeled RE Engine mesh influences."""

    def __init__(
        self,
        mesh,
        rig: Rig,
        payloads: list[MeshScenePayload],
        bind_positions: np.ndarray,
        bind_normals: np.ndarray | None,
        bind_indices: np.ndarray | None = None,
        *,
        skinning_contract: MeshSkinningContract | None = None,
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
        self._blend_shapes = MeshBlendShapeDeformer(
            mesh,
            self.bind_positions,
            self.bind_normals,
        )
        self._normal_recalculator = MeshNormalRecalculator.from_mesh(
            mesh,
            bind_indices,
            len(self.bind_positions),
        )
        if self.bind_normals is not None and len(self.bind_normals) != len(
            self.bind_positions
        ):
            raise SkinningError("mesh normals do not match its positions")
        expected_vertices = sum(record.vertex_count for record in payloads)
        if expected_vertices != len(self.bind_positions):
            raise SkinningError(
                f"mesh skinning covers {expected_vertices} vertices; scene has {len(self.bind_positions)}"
            )
        self._root_by_joint = rig.root_indices
        self._joint_indices, self._weights = self._build_influence_buffers(
            mesh,
            rig,
            payloads,
            skinning_contract,
        )
        self.binding = SceneSkinningBinding(
            self.bind_positions,
            self.bind_normals,
            self._joint_indices,
            self._weights,
        )

    @property
    def requires_post_skin_normals(self) -> bool:
        return self._normal_recalculator is not None

    def _build_influence_buffers(
        self,
        mesh,
        rig: Rig,
        payloads: list[MeshScenePayload],
        skinning_contract: MeshSkinningContract | None,
    ) -> tuple[np.ndarray, np.ndarray]:
        deform_to_joint = np.asarray(mesh.bone_remap_indices, dtype=np.int64)
        if not len(deform_to_joint):
            raise SkinningError("mesh has no deform-bone remap table")
        records: list[tuple[np.ndarray, np.ndarray]] = []
        max_width = 0

        for record in payloads:
            streams = [
                stream
                for stream in (
                    getattr(record.payload, "skin_weights", None),
                    getattr(record.payload, "extra_skin_weights", None),
                )
                if stream is not None
            ]
            if not streams:
                raise SkinningError(
                    f"mesh buffer {record.buffer_index} has no decoded skin weights"
                )
            deform_parts = []
            weight_parts = []
            for stream in streams:
                if stream.vertex_count < record.vertex_count:
                    raise SkinningError(
                        f"mesh buffer {record.buffer_index} has fewer weights than LOD0 vertices"
                    )
                width = stream.influence_count
                deform_parts.append(
                    np.asarray(stream.deform_indices, dtype=np.int64)
                    .reshape(-1, width)[: record.vertex_count]
                )
                weight_parts.append(
                    np.asarray(stream.weights, dtype=np.float32)
                    .reshape(-1, width)[: record.vertex_count]
                )
            deform = np.concatenate(deform_parts, axis=1)
            weights = np.concatenate(weight_parts, axis=1)
            records.append((deform, weights))
            max_width = max(max_width, deform.shape[1])

        if not records:
            raise SkinningError("mesh has no skinnable LOD 0 payloads")
        deform_chunks = []
        weight_chunks = []
        for deform, weights in records:
            padding = max_width - deform.shape[1]
            if padding:
                deform = np.pad(deform, ((0, 0), (0, padding)))
                weights = np.pad(weights, ((0, 0), (0, padding)))
            deform_chunks.append(deform)
            weight_chunks.append(weights)
        deform = np.concatenate(deform_chunks)
        weights = np.concatenate(weight_chunks)
        self._apply_influence_contract(
            mesh,
            payloads,
            weights,
            skinning_contract,
        )
        active = weights > 0.0
        missing = np.flatnonzero(~np.any(active, axis=1))
        if len(missing):
            raise SkinningError(
                f"mesh has {len(missing)} vertices without a skin influence"
            )
        invalid_deform = active & ((deform < 0) | (deform >= len(deform_to_joint)))
        if np.any(invalid_deform):
            invalid = int(deform[invalid_deform][0])
            raise SkinningError(
                f"mesh weight references deform bone {invalid}, but only "
                f"{len(deform_to_joint)} are mapped"
            )
        safe_deform = deform.copy()
        safe_deform[~active] = 0
        joints = deform_to_joint[safe_deform]
        invalid_joint = active & ((joints < 0) | (joints >= len(rig.joints)))
        if np.any(invalid_joint):
            invalid = int(joints[invalid_joint][0])
            raise SkinningError(
                f"mesh deform table references joint {invalid}, but the rig has "
                f"{len(rig.joints)} joints"
            )
        if np.any(joints > np.iinfo(np.uint16).max):
            raise SkinningError("mesh joint indices exceed the native skinning layout")
        return (
            np.ascontiguousarray(joints, dtype=np.uint16),
            np.ascontiguousarray(weights, dtype=np.float32),
        )

    def _apply_influence_contract(
        self,
        mesh,
        payloads: list[MeshScenePayload],
        weights: np.ndarray,
        contract: MeshSkinningContract | None,
    ) -> None:
        if contract is None:
            return
        width = weights.shape[1]
        if width != contract.decoded_influence_count:
            raise SkinningError(
                f"mesh skinning expected {contract.decoded_influence_count} "
                f"decoded weight lanes, got {width}"
            )

        if contract.default_influence_count is not None:
            limits = np.full(
                len(weights),
                contract.default_influence_count,
                dtype=np.uint8,
            )
        else:
            if contract.material_influence_counts is None:
                raise SkinningError(
                    "mesh skinning contract has no influence-count source"
                )
            limits = self._vertex_influence_limits(
                mesh,
                payloads,
                contract.material_influence_counts,
                width,
            )

        counts = tuple(int(value) for value in np.unique(limits) if value)
        if any(count < 1 or count > width for count in counts):
            raise SkinningError("mesh skinning contract has an invalid influence count")
        for count in (value for value in counts if value < width):
            rows = np.flatnonzero(limits == count)
            active_weights = weights[rows, :count]
            totals = np.sum(active_weights, axis=1)
            if np.any(totals <= 0.0):
                raise SkinningError(
                    f"{count}-weight shader span has a vertex without a "
                    "usable influence"
                )
            weights[rows, :count] = active_weights / totals[:, np.newaxis]
            weights[rows, count:] = 0.0

        full_rows = np.flatnonzero(limits == width)
        if contract.implicit_last_weight and len(full_rows):
            residual = 1.0 - np.sum(weights[full_rows, :-1], axis=1)
            tolerance = 1e-5
            if np.any((residual < -tolerance) | (residual > 1.0 + tolerance)):
                raise SkinningError(
                    f"{width}-weight shader span has invalid implicit final "
                    "weights"
                )
            weights[full_rows, -1] = np.clip(residual, 0.0, 1.0)

    @staticmethod
    def _vertex_influence_limits(
        mesh,
        payloads: list[MeshScenePayload],
        material_influences,
        decoded_influence_count: int,
    ) -> np.ndarray:
        limits = np.zeros(
            sum(record.vertex_count for record in payloads),
            dtype=np.uint8,
        )
        records = {record.buffer_index: record for record in payloads}
        material_names = list(getattr(mesh, "material_names", ()) or ())

        for submesh in mesh_lod0_submeshes(mesh):
            if int(submesh.indices_count) <= 0:
                continue
            record = records.get(int(submesh.buffer_index))
            if record is None:
                raise SkinningError(
                    f"submesh references missing buffer "
                    f"{submesh.buffer_index}"
                )
            material_index = int(submesh.material_index)
            if not 0 <= material_index < len(material_names):
                raise SkinningError(
                    f"submesh has invalid material index {material_index}"
                )
            material_name = material_names[material_index]
            influence_count = material_influences.get(material_name)
            if not isinstance(influence_count, int) or not (
                1 <= influence_count <= decoded_influence_count
            ):
                raise SkinningError(
                    f"material {material_name!r} has no supported "
                    "skinning contract"
                )

            local_start = int(submesh.verts_index_offset)
            count = SkinnedMeshDeformer._submesh_vertex_count(
                submesh,
                record.payload,
            )
            local_end = local_start + count
            if (
                local_start < 0
                or count <= 0
                or local_end > record.vertex_count
            ):
                raise SkinningError(
                    f"material {material_name!r} has invalid vertex span "
                    f"[{local_start}, {local_end}) in buffer "
                    f"{record.buffer_index}"
                )
            start = record.vertex_base + local_start
            end = record.vertex_base + local_end
            current = limits[start:end]
            if np.any((current != 0) & (current != influence_count)):
                raise SkinningError(
                    f"vertex span [{start}, {end}) has conflicting "
                    "material skinning contracts"
                )
            limits[start:end] = influence_count
        return limits

    @staticmethod
    def _submesh_vertex_count(submesh, payload) -> int:
        count = int(getattr(submesh, "vert_count", 0))
        if count > 0:
            return count
        face_count = int(submesh.indices_count)
        face_start = int(submesh.faces_index_offset)
        faces = (
            payload.integer_faces
            if payload.integer_faces is not None
            else payload.faces
        )
        face_end = face_start + face_count
        if face_start < 0 or face_count <= 0 or face_end > len(faces):
            return 0
        return max(int(index) for index in faces[face_start:face_end]) + 1

    def deform(
        self,
        snapshot: MotionPreviewSnapshot,
    ) -> tuple[np.ndarray, np.ndarray | None]:
        positions, normals = self.source_geometry(
            getattr(snapshot, "deformation_weights", ())
        )
        return self.deform_matrices(
            self.skin_matrices(snapshot),
            positions=positions,
            normals=normals,
        )

    def source_geometry(
        self,
        deformation_weights,
    ) -> tuple[np.ndarray, np.ndarray | None]:
        return self._blend_shapes.deform(deformation_weights)

    def deform_matrices(
        self,
        matrices: np.ndarray,
        *,
        positions: np.ndarray | None = None,
        normals: np.ndarray | None = None,
    ) -> tuple[np.ndarray, np.ndarray | None]:
        matrices = np.asarray(matrices, dtype=np.float32).reshape(-1, 4, 4)
        if len(matrices) != len(self._root_by_joint):
            raise SkinningError("skin matrix count does not match the mesh rig")
        if not np.isfinite(matrices).all():
            raise SkinningError("skin matrices contain a non-finite value")
        source_normals = (
            None
            if self._normal_recalculator is not None
            else (self.bind_normals if normals is None else normals)
        )
        position_bytes, normal_bytes = skin_vertices(
            self.bind_positions if positions is None else positions,
            source_normals,
            self._joint_indices,
            self._weights,
            matrices,
        )
        positions = np.frombuffer(position_bytes, dtype=np.float32).reshape(-1, 3)
        normals = (
            np.frombuffer(normal_bytes, dtype=np.float32).reshape(-1, 3)
            if normal_bytes is not None
            else None
        )
        if self._normal_recalculator is not None:
            normals = self._normal_recalculator.recalculate(positions)
        return positions, normals

    def skin_matrices(self, snapshot: MotionPreviewSnapshot) -> np.ndarray:
        raw = snapshot.pose.skin_matrices
        if len(raw) != len(self._root_by_joint) or any(
            matrix is None for matrix in raw
        ):
            raise SkinningError(
                "evaluated target pose has no skin matrix for every mesh joint"
            )
        matrices = np.asarray(raw, dtype=np.float32).reshape(-1, 4, 4).copy()
        if not np.isfinite(matrices).all():
            raise SkinningError(
                "evaluated target pose contains a non-finite skin matrix"
            )
        root_deltas = dict(snapshot.root_deltas)
        if root_deltas:
            for joint_index, root_index in enumerate(self._root_by_joint):
                delta = root_deltas.get(root_index)
                if delta is not None:
                    matrices[joint_index, 3, :3] -= np.asarray(delta, dtype=np.float32)
        return matrices
