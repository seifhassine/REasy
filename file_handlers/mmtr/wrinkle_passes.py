"""See docs/internals/wrinkle.md."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.sdf.contracts import shader_contract_for_program

from .material_preview import RANGE_CBV, RANGE_SRV, RANGE_UAV, SHADER_VISIBILITY, MaterialPreviewError
from .mesh_draw import DXGI_FORMAT_R16_UINT, DXGI_FORMAT_R32_UINT, program_states
from .pretransform import constant_buffer
from .pretransform_passes import SystemPrograms, dispatch_compute
from .resident import BUFFER, UNORDERED_ACCESS, Resident
from .wrinkle import (
    INDICES, VERTICES, WrinkleCatalog, WrinkleObject, WrinkleProxy, WrinkleStep, buffer_sizes, frame_steps,
    setup_steps,
)

# Readable buffer types: whether their elements are laid out.
_READ = {"byte-address-buffer": False, "structured-buffer": True}
_SCALE = "cGameObjectScale"


@dataclass(slots=True)
class _Setup:
    held: dict[str, int]
    steps: list[int]
    ran: bool = False


@dataclass(slots=True)
class _Object:
    """``scaled``: (buffer, layout, step number) of each constant buffer holding the scale."""
    wrinkle: WrinkleObject
    setup: _Setup
    held: dict[str, int]
    steps: list[int]
    engine: list[tuple[int, Callable[[], bytes]]]
    scale: Callable[[], object]
    scaled: list[tuple[int, object, int]]


class WrinklePasses:
    def __init__(self, device: Resident, programs: SystemPrograms, catalog: WrinkleCatalog):
        self.device, self.programs, self.catalog = device, programs, catalog
        self._setups: dict[int, _Setup] = {}
        self._objects: dict[int, _Object] = {}

    def _program(self, step: WrinkleStep) -> tuple[object, dict[str, object]]:
        program = self.programs.program(self.catalog.target, step.program)
        stages = [shader.role for shader in program.shaders if shader.present] if program is not None else []
        wanted = ["vertex", "pixel"] if step.draw else ["compute"]
        if stages != wanted:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The game's shader file {path} has no program {name} of the stages the wrinkle "
                                   "passes run."), {"path": step.program.file, "name": step.program.name})
        profile = self.programs.profile.sdf_profile
        return program, {stage: shader_contract_for_program(profile, program, stage) for stage in stages}

    def _tables(self, step: WrinkleStep, contracts: Mapping[str, object], held: Mapping[str, int],
                engine: Callable[[object, object], int | None] | None,
                scaled: Callable[[int, object], None] | None) -> list[tuple]:
        tables = []
        for stage, contract in contracts.items():
            visibility = SHADER_VISIBILITY[stage]
            for resource in contract.resources:
                name, place = resource.name, (resource.bind_point, resource.register_space, resource.bind_count)
                kind = step.buffers.get(name)
                if resource.register_class == "b":
                    layout = contract.buffer(name)
                    if any(variable.name == _SCALE for variable in layout.variables) and scaled is not None:
                        bound = self.device.scene.buffer(constant_buffer(layout, step.constants), 0)
                        scaled(bound, layout)
                    elif any(variable.name in step.constants for variable in layout.variables):
                        bound = self.device.buffer(constant_buffer(layout, step.constants))
                    elif engine is None or (bound := engine(contract, resource)) is None:
                        raise _undeclared(step, name)
                    tables.append((RANGE_CBV, *place, 0, visibility, ((0, bound),)))
                elif resource.register_class == "t" and kind in held and resource.input_type in _READ:
                    stride = contract.structured_buffer(name).byte_size if _READ[resource.input_type] else 0
                    tables.append((RANGE_SRV, *place, BUFFER, visibility, ((0, 0, stride, held[kind]),)))
                elif resource.register_class == "u" and kind in held:
                    stride = 0 if resource.input_type == "rw-byte-address-buffer" else \
                        contract.structured_buffer(name).byte_size
                    tables.append((RANGE_UAV, *place, BUFFER, visibility, ((0, 0, stride, held[kind]),)))
                elif resource.register_class == "t" and kind is None and engine is not None \
                        and (bound := engine(contract, resource)) is not None:
                    stride = contract.structured_buffer(name).byte_size
                    tables.append((RANGE_SRV, *place, BUFFER, visibility, ((0, 0, stride, bound),)))
                else:
                    raise _undeclared(step, name)
        return tables

    def _step(self, step: WrinkleStep, held: Mapping[str, int], proxy: WrinkleProxy,
              engine: Callable[[object, object], int | None] | None = None, target: tuple | None = None,
              scaled: Callable[[int, object], None] | None = None) -> int:
        if step.copy is not None:
            to, source = step.copy
            return self.device.copy(held[to], held[source])
        program, contracts = self._program(step)
        tables = self._tables(step, contracts, held, engine, scaled)
        if not step.draw:
            return dispatch_compute(self.device.scene, bytes(program.shader("compute").payload), tables,
                                    (step.groups, 1, 1), step.program.name)
        form, texture, (width, height) = target
        elements = []
        for element in program.input_layout.elements:
            if element.semantic.upper() != "TEXCOORD" or element.semantic_index or element.per_instance:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The wrinkle draw reads {semantic}{index}, which no capture shows the renderer "
                                       "binding."), {"semantic": element.semantic, "index": element.semantic_index})
            elements.append((element.semantic, element.semantic_index, element.dxgi_format_id, 0,
                             element.aligned_byte_offset, 0, 0))
        try:
            return self.device.scene.draw({
                "vertex_shader": bytes(program.shader("vertex").payload),
                "pixel_shader": bytes(program.shader("pixel").payload),
                "vertex_buffers": [proxy.uvs()], "elements": elements, "indices": proxy.indices,
                "index_format": DXGI_FORMAT_R32_UINT if proxy.wide else DXGI_FORMAT_R16_UINT,
                "draw": (proxy.index_count, 1, 0, 0, 0), "size": (width, height),
                "viewport": (0.0, 0.0, float(width), float(height), 0.0, 1.0), "scissor": (0, 0, width, height),
                "targets": [(form, texture)], "depth": None, "tables": tables, "heap": [],
                **program_states(program, 1, 0)})
        except RuntimeError as exc:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "Direct3D could not run the program {name}: {reason}"),
                {"name": step.program.name, "reason": str(exc)}) from exc

    def _setup(self, proxy: WrinkleProxy) -> _Setup:
        found = self._setups.get(id(proxy))
        if found is None:
            scene = self.device.scene
            # Buffers read word by word hold whole words.
            held = {VERTICES: self.device.buffer(proxy.level.source + bytes(-len(proxy.level.source) % 4)),
                    INDICES: self.device.buffer(proxy.indices + bytes(-len(proxy.indices) % 4))}
            sizes, steps = buffer_sizes(proxy, self.catalog), setup_steps(proxy, self.catalog)
            for kind in (*(kind for step in steps for kind in step.buffers.values()),
                         *(step.copy[0] for step in steps if step.copy is not None)):
                if kind not in held:
                    held[kind] = scene.buffer(bytes(sizes[kind]), UNORDERED_ACCESS)
            # The proxy is the scene's while it lasts.
            found = self._setups[id(proxy)] = _Setup(held, [self._step(step, held, proxy) for step in steps])
        return found

    def add(self, owner: int, wrinkle: WrinkleObject, target: tuple[int, int, tuple[int, int]],
            scale: Callable[[], object], engine: Mapping[str, Callable[[object, object], bytes | None]]) -> None:
        """``scale()`` and ``engine[name]`` give the object's current scale and engine buffer bytes."""
        proxy, scene = wrinkle.proxy, self.device.scene
        setup = self._setup(proxy)
        sizes = buffer_sizes(proxy, self.catalog)
        held = dict(setup.held)
        steps = frame_steps(proxy, self.catalog, scale(), wrinkle.spread)
        for step in steps:
            for kind in step.buffers.values():
                if kind not in held:
                    held[kind] = scene.buffer(bytes(sizes[kind]), UNORDERED_ACCESS)
        made: list[tuple[int, Callable[[], bytes]]] = []

        def bound(contract, resource) -> int | None:
            give = engine.get(resource.name)
            if give is None:
                return None
            layout = contract.buffer(resource.name) if resource.register_class == "b" else \
                contract.structured_buffer(resource.name)

            def make(give=give, contract=contract, resource=resource) -> bytes:
                return give(contract, resource)
            data = make()
            if data is None:
                return None
            buffer = scene.buffer(bytes(data).ljust(layout.byte_size, b"\0"), 0)
            made.append((buffer, make))
            return buffer
        found = self._objects[owner] = _Object(wrinkle, setup, held, [], made, scale, [])
        for number, step in enumerate(steps):
            found.steps.append(self._step(step, held, proxy, bound, target, lambda buffer, layout, number=number:
                                          found.scaled.append((buffer, layout, number))))

    def place(self, owner: int) -> None:
        found, scene = self._objects[owner], self.device.scene
        for buffer, make in found.engine:
            scene.write(buffer, make())
        if found.scaled:
            steps = frame_steps(found.wrinkle.proxy, self.catalog, found.scale(), found.wrinkle.spread)
            for buffer, layout, number in found.scaled:
                scene.write(buffer, constant_buffer(layout, steps[number].constants))

    def buffers(self, owner: int) -> Mapping[str, int]:
        return MappingProxyType(self._objects[owner].held)

    def steps(self, owner: int) -> list[int]:
        found = self._objects[owner]
        setup = [] if found.setup.ran else found.setup.steps
        found.setup.ran = True
        return [*setup, *found.steps]


def _undeclared(step: WrinkleStep, name: str) -> MaterialPreviewError:
    return MaterialPreviewError(QT_TRANSLATE_NOOP(
        "MaterialPreview", "The program {program} declares {name}, which no capture shows the renderer binding for "
                           "the wrinkle passes."), {"program": step.program.name, "name": name})


__all__ = ["WrinklePasses"]
