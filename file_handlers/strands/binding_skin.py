"""See docs/internals/strands-rendering.md, Binding skinning."""
from __future__ import annotations

import struct
from collections.abc import Sequence

import numpy as np

from file_handlers.mmtr.material_preview import RANGE_CBV, RANGE_SRV, RANGE_UAV, SHADER_VISIBILITY
from file_handlers.mmtr.pretransform import SystemProgram
from file_handlers.mmtr.pretransform_passes import SystemPrograms, check_threads, dispatch_compute
from file_handlers.mmtr.resident import BUFFER, UNORDERED_ACCESS, Resident

from .chain_catalog import SkinningSection
from .lod import F

POSITION_BYTES, _WEIGHT_BYTES, _MATRIX_BYTES, _THREADS = 12, 16, 48, 256


def skin_matrices(joints: np.ndarray, inverse_binds: np.ndarray, remap: Sequence[int], origin: np.ndarray) -> bytes:
    index = np.asarray(remap, np.intp)
    world = np.array(joints, F)[index]
    world[:, 3, :3] -= np.asarray(origin, F)
    bind = np.asarray(inverse_binds, F)[index]
    made = (((bind[:, :, 0, None] * world[:, None, 0]) + bind[:, :, 1, None] * world[:, None, 1])
            + bind[:, :, 2, None] * world[:, None, 2]) + bind[:, :, 3, None] * world[:, None, 3]
    return np.ascontiguousarray(made.transpose(0, 2, 1)[:, :3]).tobytes()


class BindingSkin:
    def __init__(self, device: Resident, section: SkinningSection, programs: SystemPrograms, program: str, mesh: int,
                 source: tuple[int, int], groups: Sequence[tuple[int, int]], base: int, weights: int, joints: int):
        shader, contract = programs.get(section.target, SystemProgram(section.file, program))
        check_threads(contract, program)
        scene = device.scene
        self.device = device
        self.matrices = scene.buffer(bytes(_MATRIX_BYTES * joints), 0)
        self.positions = scene.buffer(bytes(POSITION_BYTES * sum(count for _first, count in groups)),
                                      UNORDERED_ACCESS)
        bound = {section.buffers[kind]: buffer for kind, buffer in (
            ("matrices", self.matrices), ("mesh", mesh), ("source", source[0]), ("destination", self.positions))}
        visibility = SHADER_VISIBILITY["compute"]
        self._steps = []
        for first, count in groups:
            values = {section.constants[name]: value for name, value in (
                ("joint_offset", 0), ("vertex_count", count), ("positions", source[1] + POSITION_BYTES * first),
                ("weights", weights + _WEIGHT_BYTES * first), ("destination", POSITION_BYTES * (first - base)))}
            tables = []
            for resource in contract.resources:
                name, place = resource.name, (resource.bind_point, resource.register_space, resource.bind_count)
                if resource.register_class == "b":
                    layout = contract.buffer(name)
                    block = bytearray(layout.byte_size)
                    for variable in layout.variables:
                        if variable.name in values:
                            struct.pack_into("<i", block, variable.byte_offset, values[variable.name])
                    tables.append((RANGE_CBV, *place, 0, visibility, ((0, device.buffer(bytes(block))),)))
                elif resource.register_class == "t":
                    stride = contract.structured_buffer(name).byte_size if "structured" in resource.input_type else 0
                    tables.append((RANGE_SRV, *place, BUFFER, visibility, ((0, 0, stride, bound[name]),)))
                else:
                    tables.append((RANGE_UAV, *place, BUFFER, visibility, ((0, 0, 0, bound[name]),)))
            self._steps.append(dispatch_compute(scene, shader, tables, (-(-count // _THREADS), 1, 1), program))

    def steps(self, matrices: bytes) -> list[int]:
        """``matrices`` must be packed by ``skin_matrices``."""
        self.device.scene.write(self.matrices, matrices)
        return self._steps


__all__ = ["POSITION_BYTES", "BindingSkin", "skin_matrices"]
