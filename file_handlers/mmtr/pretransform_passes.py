"""See docs/internals/pretransform.md."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

lazy import numpy as np
lazy from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.sdf.contracts import shader_contract_for_program
lazy from file_handlers.sdf.models import program_names
lazy from file_handlers.sdf.sdf_file import parse_sdf_to_draw

lazy from .material_preview import RANGE_CBV, RANGE_SRV, RANGE_UAV, SHADER_VISIBILITY, MaterialPreviewError
lazy from .pretransform import (
    ACCUMULATOR, CHAIN_BUFFERS, DESTINATION, INDEX_WORDS, SOURCE, THREADS, VERTEX_WORDS, WEIGHTS, ChainStep,
    Deformation, SystemProgram, constant_buffer, used_channels, weight_list,
)
lazy from .resident import BUFFER, UNORDERED_ACCESS, Resident

_READ = {"byte-address-buffer": False, "structured-buffer": True}
_WRITTEN = "rw-byte-address-buffer"


def dispatch_compute(scene, shader, tables, groups, name: str) -> int:
    try:
        return scene.dispatch({"compute_shader": shader, "tables": tables, "heap": [], "groups": groups})
    except RuntimeError as exc:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "Direct3D could not run the compute program {name}: {reason}"),
            {"name": name, "reason": str(exc)}) from exc


def check_threads(contract, name: str) -> None:
    if tuple(contract.thread_group_size or ()) != (THREADS, 1, 1):
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The compute program {name} uses a different thread group size from the renderer's "
                               "passes."), {"name": name})


def written_entry(resource, step: ChainStep, held: Mapping[str, int]) -> tuple:
    kind = step.buffers.get(resource.name)
    if kind not in held or resource.input_type != _WRITTEN:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The compute program writes {name}, which no capture shows the renderer binding."),
            {"name": resource.name})
    return 0, 0, 0, held[kind]


@dataclass(slots=True)
class _Deformed:
    deformation: Deformation
    held: dict[str, int]
    steps: list[int]
    blends: list[tuple[int, object, ChainStep]]
    engine: Callable[[object, object], int | None]
    skinning: int | None = None
    morphed: int | None = None


class SystemPrograms:
    def __init__(self, profile, load_file: Callable[[str], bytes | None],
                 pass_file: Callable[[str, bytes], object] | None = None):
        if pass_file is None:
            def pass_file(path, data):
                return parse_sdf_to_draw(data, profile.sdf_profile, path)
        self.profile, self.load_file, self.pass_file = profile, load_file, pass_file
        self._documents: dict[str, object] = {}
        self._programs: dict[tuple[int, SystemProgram], tuple[bytes, object]] = {}

    def program(self, target: int, system: SystemProgram):
        if system.file not in self._documents:
            data = self.load_file(system.file)
            if data is None:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The game's shader file {path} was not found."), {"path": system.file})
            self._documents[system.file] = self.pass_file(system.file, data)
        found = self._documents[system.file].targets[target]
        names = program_names(found)
        return found.programs[names.index(system.name)] if system.name in names else None

    def get(self, target: int, system: SystemProgram) -> tuple[bytes, object]:
        if (target, system) not in self._programs:
            program = self.program(target, system)
            if program is None or not any(shader.present and shader.role == "compute" for shader in program.shaders):
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The game's shader file {path} has no compute program {name}."),
                    {"path": system.file, "name": system.name})
            contract = shader_contract_for_program(self.profile.sdf_profile, program, "compute")
            self._programs[target, system] = (bytes(program.shader("compute").payload), contract)
        return self._programs[target, system]


class ChainPasses:
    def __init__(self, device: Resident, profile, load_file: Callable[[str], bytes | None],
                 pass_file: Callable[[str, bytes], object] | None = None):
        self.device = device
        self.programs = SystemPrograms(profile, load_file, pass_file)
        self._objects: dict[int, _Deformed] = {}

    def _program(self, target: int, system: SystemProgram) -> tuple[bytes, object]:
        shader, contract = self.programs.get(target, system)
        check_threads(contract, system.name)
        return shader, contract

    def _constants(self, step: ChainStep, weights: np.ndarray | None) -> dict[str, np.ndarray]:
        if step.target is None:
            return dict(step.constants)
        return {**step.constants, "cUsingChannelCount": np.array([len(used_channels(step.target, weights))], "<u4")}

    def _dispatch(self, deformed: _Deformed, step: ChainStep) -> int:
        deformation, held, scene = deformed.deformation, deformed.held, self.device.scene
        shader, contract = self._program(deformation.catalog.target, step.program)
        visibility, tables = SHADER_VISIBILITY["compute"], []
        for resource in contract.resources:
            name, place = resource.name, (resource.bind_point, resource.register_space, resource.bind_count)
            kind = step.buffers.get(name)
            if resource.register_class == "b" and name in CHAIN_BUFFERS:
                layout = contract.buffer(name)
                data = constant_buffer(layout, self._constants(step, deformation.weights))
                if step.target is None:
                    bound = self.device.buffer(data)
                else:
                    bound = scene.buffer(data, 0)
                    deformed.blends.append((bound, layout, step))
                tables.append((RANGE_CBV, *place, 0, visibility, ((0, bound),)))
            elif resource.register_class == "t" and kind in held and resource.input_type in _READ:
                stride = contract.structured_buffer(name).byte_size if _READ[resource.input_type] else 0
                tables.append((RANGE_SRV, *place, BUFFER, visibility, ((0, 0, stride, held[kind]),)))
            elif resource.register_class == "u":
                tables.append((RANGE_UAV, *place, BUFFER, visibility, (written_entry(resource, step, held),)))
            elif kind is None and (bound := deformed.engine(contract, resource)) is not None:
                if resource.register_class == "b":
                    tables.append((RANGE_CBV, *place, 0, visibility, ((0, bound),)))
                else:
                    stride = contract.structured_buffer(name).byte_size
                    tables.append((RANGE_SRV, *place, BUFFER, visibility, ((0, 0, stride, bound),)))
            else:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The compute program {program} declares {name}, which no capture shows the "
                                       "renderer binding."), {"program": step.program.name, "name": name})
        return dispatch_compute(scene, shader, tables, (step.groups, 1, 1), step.program.name)

    def add(self, owner: int, deformation: Deformation, skin: Callable[[ChainStep, Mapping[str, int]], int],
            engine: Callable[[object, object], int | None]) -> None:
        """``skin(step, held)`` makes a master program's step; ``engine`` gives the buffer for an engine buffer a system
        program declares, None when the engine does not fill it."""
        level, destination, scene = deformation.level, deformation.destination, self.device.scene
        # Buffers read word by word hold whole words.
        held = {SOURCE: self.device.buffer(level.source + bytes(-len(level.source) % 4)),
                DESTINATION: scene.buffer(bytes(destination.size), UNORDERED_ACCESS)}
        deformed = self._objects[owner] = _Deformed(deformation, held, [], [], engine)
        if level.blend is not None:
            held[WEIGHTS] = scene.buffer(weight_list(level.blend, deformation.weights), 0)
        if level.normals is not None:
            zeros = bytes(destination.accumulator_size)
            held[ACCUMULATOR] = scene.buffer(zeros, UNORDERED_ACCESS)
            held[VERTEX_WORDS] = self.device.buffer(level.normals.vertex_words)
            held[INDEX_WORDS] = self.device.buffer(level.normals.index_words)
            deformed.steps.append(self.device.copy(held[ACCUMULATOR], self.device.buffer(zeros)))
        for step in deformation.steps():
            if step.material is not None and level.blend is not None and deformed.skinning is None:
                deformed.skinning = len(deformed.steps)
            deformed.steps.append(self._dispatch(deformed, step) if step.material is None else skin(step, held))

    def destination(self, owner: int) -> int:
        return self._objects[owner].held[DESTINATION]

    def keep_morphed(self, owner: int) -> int:
        """The returned buffer holds the blend-shaped, unskinned positions from the next run on."""
        deformed = self._objects[owner]
        if deformed.morphed is None:
            if deformed.skinning is None:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The renderer's passes leave no blend-shaped positions before skinning this "
                                       "mesh's LOD."))
            deformed.morphed = self.device.scene.buffer(bytes(deformed.deformation.destination.size), 0)
            deformed.steps.insert(deformed.skinning, self.device.copy(deformed.morphed, deformed.held[DESTINATION]))
        return deformed.morphed

    def morphed(self, owner: int) -> int | None:
        return self._objects[owner].morphed

    def steps(self, owner: int) -> list[int]:
        return self._objects[owner].steps

    def morph(self, owner: int, weights: np.ndarray) -> None:
        deformed, scene = self._objects[owner], self.device.scene
        deformation = deformed.deformation = replace(deformed.deformation, weights=weights)
        scene.write(deformed.held[WEIGHTS], weight_list(deformation.level.blend, weights))
        for bound, layout, step in deformed.blends:
            scene.write(bound, constant_buffer(layout, self._constants(step, weights)))


__all__ = ["ChainPasses", "SystemPrograms", "check_threads", "written_entry"]
