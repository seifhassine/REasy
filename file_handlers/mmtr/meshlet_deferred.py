"""See docs/internals/meshlets.md, Deferred texturing chain."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

lazy from PySide6.QtCore import QT_TRANSLATE_NOOP

lazy from .material_preview import (
    RANGE_SRV, RANGE_UAV, VIEW_DIMENSIONS, _UAV_DIMENSIONS, SHADER_VISIBILITY, MaterialPreviewError,
)
lazy from .pretransform import SystemProgram
lazy from .pretransform_passes import SystemPrograms, dispatch_compute
lazy from .resident import BUFFER, UNORDERED_ACCESS, Resident

STAGES = ("material_ids", "transform", "shade")


class DeferredCatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DeferredRole:
    size: int = 0
    texel_bytes: int = 0
    format: int | None = None


@dataclass(frozen=True, slots=True)
class DeferredStep:
    program: str
    groups: int
    buffers: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class DeferredTexturing:
    steps: tuple[DeferredStep, ...]
    bound: Mapping[str, Mapping[str, str]]
    roles: Mapping[str, DeferredRole]
    zeroed: frozenset[str]
    transform_ids: int
    arguments_role: str
    arguments: int


def _names(data: Mapping) -> Mapping[str, str]:
    return frozendict({str(name): str(role) for name, role in data.items()})


def deferred_from_dict(data: Mapping) -> DeferredTexturing:
    roles = {}
    for name, item in data["roles"].items():
        form = item.get("format")
        roles[str(name)] = DeferredRole(int(item.get("size", 0)), int(item.get("texel_bytes", 0)),
                                        None if form is None else int(form))
    steps = tuple(DeferredStep(str(item["program"]), int(item["groups"]), _names(item["buffers"]))
                  for item in data["steps"])
    bound = {stage: _names(data["bound"][stage]) for stage in STAGES}
    named = {*item.values() for item in (*(step.buffers for step in steps), *bound.values())}
    zeroed = frozenset(map(str, data["zeroed"]))
    arguments = data["arguments"]
    named.add(str(arguments["role"]))
    if not named | zeroed <= set(roles) or any(roles[role].format is not None for role in zeroed):
        raise DeferredCatalogError("a deferred texturing catalog names roles it gives no size, or zeroes a texture")
    if not steps or any(step.groups < 1 for step in steps):
        raise DeferredCatalogError("a deferred texturing catalog runs no steps, or a step over no groups")
    return DeferredTexturing(steps, frozendict(bound), frozendict(roles), zeroed,
                             int(data["transform_ids"]), str(arguments["role"]), int(arguments["bytes"]))


class DeferredPasses:
    def __init__(self, device: Resident, catalog: DeferredTexturing, programs: SystemPrograms, target: int,
                 file: str, size: tuple[int, int]):
        self.device, self.catalog = device, catalog
        self._programs, self._target, self._file = programs, target, file
        texels = size[0] * size[1]
        self.held: dict[str, int] = {}
        self._zeros: dict[str, int] = {}
        for name, role in catalog.roles.items():
            if role.format is not None:
                self.held[name] = device.texture(role.format, size, UNORDERED_ACCESS)
                continue
            data = bytes(role.size + role.texel_bytes * texels)
            self.held[name] = device.scene.buffer(data, UNORDERED_ACCESS)
            if name in catalog.zeroed:
                self._zeros[name] = device.buffer(data)
        self._chain: list[int] | None = None

    def resources(self) -> list[int]:
        return list(self.held.values())

    def starts(self) -> list[int]:
        return [self.device.copy(self.held[name], self._zeros[name]) for name in sorted(self._zeros)]

    def chain(self, engine: Callable[[object, object], tuple | None]) -> list[int]:
        if self._chain is None:
            self._chain = [self._dispatch(step, engine) for step in self.catalog.steps]
        return self._chain

    def forget_chain(self) -> None:
        """The scene's meshlet buffers the dispatches read were made again."""
        self._chain = None

    def entry(self, stage: str, resource) -> tuple | None:
        role = self.catalog.bound[stage].get(resource.name)
        return None if role is None else self._entry(role)

    def bound(self, stage: str, name: str) -> int | None:
        role = self.catalog.bound[stage].get(name)
        return None if role is None else self.held[role]

    def arguments(self, transform: int) -> tuple[int, int]:
        return self.held[self.catalog.arguments_role], transform * self.catalog.arguments

    def _entry(self, role: str) -> tuple:
        held, form = self.held[role], self.catalog.roles[role].format
        if form is not None:
            return 0, form, held
        return 0, 0, 0, held

    def _dispatch(self, step: DeferredStep, engine: Callable[[object, object], tuple | None]) -> int:
        shader, contract = self._programs.get(self._target, SystemProgram(self._file, step.program))
        visibility, tables = SHADER_VISIBILITY["compute"], []
        for resource in contract.resources:
            place = (resource.bind_point, resource.register_space, resource.bind_count)
            role = step.buffers.get(resource.name)
            if role is not None:
                kind = RANGE_UAV if resource.register_class == "u" else RANGE_SRV
                dimension = (_UAV_DIMENSIONS if kind == RANGE_UAV else VIEW_DIMENSIONS).get(resource.dimension, BUFFER)
                if resource.input_type == "structured-buffer":
                    entry = (0, 0, contract.structured_buffer(resource.name).byte_size, self.held[role])
                else:
                    entry = self._entry(role)
                tables.append((kind, *place, dimension, visibility, (entry,)))
                continue
            found = engine(contract, resource)
            if found is None:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The compute program {program} declares {name}, which no capture shows the "
                                       "renderer binding."), {"program": step.program, "name": resource.name})
            tables.append(found)
        return dispatch_compute(self.device.scene, shader, tables, (step.groups, 1, 1), step.program)


__all__ = ["DeferredCatalogError", "DeferredPasses", "DeferredRole", "DeferredStep", "DeferredTexturing", "STAGES",
           "deferred_from_dict"]
