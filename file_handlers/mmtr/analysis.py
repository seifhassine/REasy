from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

lazy from file_handlers.sdf.dxbc import DxbcSignatureParameter


class SkinningSemanticProfile(Protocol):
    name: str
    index_semantic: str
    weight_semantic: str


class MmtrAnalysisError(ValueError):
    pass


def skinning_influence_count(
    parameters: Iterable[DxbcSignatureParameter],
    profile: SkinningSemanticProfile,
    *,
    skinned_geometry: bool = False,
) -> int | None:
    source = tuple(parameters)
    semantics = [(str(parameter.semantic_name).upper(), int(parameter.semantic_index)) for parameter in source]

    indices = {
        index for name, index in semantics if name == profile.index_semantic
    }
    weights = {
        index for name, index in semantics if name == profile.weight_semantic
    }
    if not indices and not weights:
        return None

    def used(name: str) -> dict[int, int]:
        result: dict[int, int] = {}
        for parameter in source:
            if str(parameter.semantic_name).upper() != name:
                continue
            mask = int(parameter.read_write_mask) & 0xF
            if not mask:
                continue
            index = int(parameter.semantic_index)
            if index in result:
                raise MmtrAnalysisError(
                    f"{profile.name}: duplicate used {name}{index} input"
                )
            if mask & ~int(parameter.mask):
                raise MmtrAnalysisError(
                    f"{profile.name}: {name}{index} use mask exceeds its declaration"
                )
            result[index] = mask
        return result

    used_indices = used(profile.index_semantic)
    used_weights = used(profile.weight_semantic)
    if used_indices or used_weights:
        if used_indices and not used_weights:
            # Rigid skinning: shipped masters read INDEX0 (xyz or xyzw) without weights. See
            # docs/mesh-conversion-formats.md, Skinning interpretation.
            if skinned_geometry and used_indices in ({0: 0x7}, {0: 0xF}):
                return 1
            return None
        if used_weights and not used_indices:
            raise MmtrAnalysisError(
                f"{profile.name}: incomplete skin index/weight input pair"
            )
        patterns = {
            (((0, 0xF),), ((0, 0xF),)): 4,
            (((0, 0x7),), ((0, 0x7),)): 4,
            (
                ((0, 0xF), (1, 0xF)),
                ((0, 0xF), (1, 0x7)),
            ): 8,
            (
                ((0, 0x7), (1, 0x7)),
                ((0, 0xF), (1, 0x1)),
            ): 6,
            (
                ((0, 0x7), (1, 0x7), (2, 0x7), (3, 0x7)),
                ((0, 0xF), (1, 0xF), (2, 0x7)),
            ): 12,
        }
        key = (
            tuple(sorted(used_indices.items())),
            tuple(sorted(used_weights.items())),
        )
        resolved = patterns.get(key)
        if resolved is not None:
            return resolved
        raise MmtrAnalysisError(
            f"{profile.name}: unsupported skin input-use masks "
            f"INDEX={dict(sorted(used_indices.items()))}, "
            f"WEIGHT={dict(sorted(used_weights.items()))}"
        )
    if indices != weights or 0 not in indices:
        raise MmtrAnalysisError(
            f"{profile.name}: inconsistent skin index/weight shader inputs"
        )
    if indices == {0}:
        return 4
    if indices == {0, 1}:
        return 8
    raise MmtrAnalysisError(
        f"{profile.name}: unsupported skin input semantic indices {sorted(indices)}"
    )


__all__ = ["MmtrAnalysisError", "skinning_influence_count"]
