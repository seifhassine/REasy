from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping

from .material_resolver import MeshMaterialResolver
from .profiles import MaterialSkinningLayout, NormalRecalcLayout


class MeshSkinningContractError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MeshSkinningContract:
    """Semantic rules needed to complete decoded mesh influence weights."""

    decoded_influence_count: int
    default_influence_count: int | None = None
    material_influence_counts: Mapping[str, int] | None = None
    implicit_last_weight: bool = False
    material_layout: MaterialSkinningLayout = MaterialSkinningLayout.NONE
    lod0_compute_influence_count: int | None = None


def packed_rigid_joint_indices(indices):
    import numpy as np

    result = indices[:, 0].astype(np.int64)
    escaped = indices[:, 2] == 1023
    result[escaped] |= indices[escaped, 1].astype(np.int64) << 10
    return result


def normal_compute_influence_count(mesh) -> int | None:
    profile = mesh.profile
    if mesh.normal_recalc_data is None:
        return None
    if profile.normal_compute_uses_packed_skin_width:
        counts = {int(data.skin_weight_influence_count) for data in mesh.meshes}
        if len(counts) != 1 or not counts <= {6, 12}:
            raise MeshSkinningContractError(f"Unsupported packed compute skin width: {sorted(counts)}")
        return counts.pop()
    if profile.normal_recalc_layout not in (NormalRecalcLayout.DMC5, NormalRecalcLayout.RE2, NormalRecalcLayout.RE3):
        return None
    counts = {int(data.skin_weight_count_raw) for data in mesh.meshes}
    if len(counts) != 1 or not counts <= {0, 1}:
        raise MeshSkinningContractError(f"Unsupported legacy compute SkinWeightCount: {sorted(counts)}")
    return 8 if counts == {1} else 4


def resolve_mesh_skinning_contract(
    mesh,
    handler=None,
    *,
    explicit_mdf_path: str = "",
) -> MeshSkinningContract | None:
    """Resolve version-specific skinning semantics outside animation preview."""
    profile = mesh.profile
    if not profile.has_material_skinning_contract:
        return None
    compute_count = normal_compute_influence_count(mesh)
    if handler is None:
        if compute_count is not None:
            width = compute_count if profile.normal_compute_uses_packed_skin_width else 8
            return MeshSkinningContract(width, default_influence_count=0, implicit_last_weight=True,
                material_layout=profile.material_skinning_layout, lod0_compute_influence_count=compute_count)
        raise MeshSkinningContractError(
            f"direct {profile.display_name} skinning requires the mesh handler "
            "to resolve MDF/MMTR"
        )
    try:
        material_counts = (
            MeshMaterialResolver.resolve_material_skinning_influences(
                handler,
                profile,
                explicit_mdf_path=explicit_mdf_path,
            )
        )
    except ValueError as exc:
        raise MeshSkinningContractError(str(exc)) from exc
    if (
        profile.material_skinning_layout
        is MaterialSkinningLayout.LATE_DXIL_PACKED_10BIT
    ):
        declared_widths = {
            int(mesh_data.skin_weight_influence_count)
            for mesh_data in mesh.meshes
            if int(mesh_data.skin_weight_influence_count) > 0
        }
        if len(declared_widths) != 1:
            raise MeshSkinningContractError(
                f"{profile.display_name} mesh does not declare one canonical "
                f"skin stream width: {sorted(declared_widths)}"
            )
        decoded_influence_count = declared_widths.pop()
    else:
        decoded_influence_count = 8
    return MeshSkinningContract(
        decoded_influence_count,
        material_influence_counts=material_counts,
        implicit_last_weight=(
            profile.material_skinning_layout
            is MaterialSkinningLayout.LEGACY_DXBC_RESIDUAL_BYTE8
        ),
        material_layout=profile.material_skinning_layout,
        lod0_compute_influence_count=compute_count,
    )
