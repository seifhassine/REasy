from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable, Mapping

from .material_resolver import MeshMaterialResolver
from .mesh_file import MeshMainVersion


class MeshSkinningContractError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MeshSkinningContract:
    """Semantic rules needed to complete decoded mesh influence weights."""

    decoded_influence_count: int
    default_influence_count: int | None = None
    material_influence_counts: Mapping[str, int] | None = None
    implicit_last_weight: bool = False


def resolve_mesh_skinning_contract(
    mesh,
    handler=None,
    *,
    explicit_mdf_path: str = "",
) -> MeshSkinningContract | None:
    """Resolve version-specific skinning semantics outside animation preview."""
    version = getattr(getattr(mesh, "mesh_buffer", None), "version", None)
    resolver = _SKINNING_CONTRACT_RESOLVERS.get(version)
    return (
        resolver(mesh, handler, explicit_mdf_path)
        if resolver is not None
        else None
    )


def _resolve_dmc5_skinning_contract(
    mesh,
    handler,
    explicit_mdf_path: str,
) -> MeshSkinningContract:
    if getattr(mesh, "normal_recalc_data", None) is not None:
        return MeshSkinningContract(8, default_influence_count=4)
    if handler is None:
        raise MeshSkinningContractError(
            "direct DMC5 skinning requires the mesh handler to resolve MDF/MMTR"
        )
    try:
        material_counts = MeshMaterialResolver.resolve_dmc5_skinning_influences(
            handler,
            explicit_mdf_path=explicit_mdf_path,
        )
    except ValueError as exc:
        raise MeshSkinningContractError(str(exc)) from exc
    return MeshSkinningContract(
        8,
        material_influence_counts=material_counts,
        implicit_last_weight=True,
    )


SkinningContractResolver = Callable[
    [object, object | None, str],
    MeshSkinningContract,
]

_SKINNING_CONTRACT_RESOLVERS: Mapping[
    MeshMainVersion,
    SkinningContractResolver,
] = {
    MeshMainVersion.DMC5: _resolve_dmc5_skinning_contract,
}
