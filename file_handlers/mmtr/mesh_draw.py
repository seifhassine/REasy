"""See docs/internals/mesh-preview.md, Draw inputs."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

lazy from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.mesh.mesh_file import VertexBufferType
from file_handlers.sdf.dxbc import PRIMITIVE_TOPOLOGY_NAMES
from file_handlers.tex.dxgi import DXGI_FORMAT_R16_UINT, DXGI_FORMAT_R32_UINT

lazy from .material_preview import MaterialPreviewError
lazy from .pretransform import Destination, destination_view

_WHITE_STRIDE = 4
_TOPOLOGIES = {name: number for number, name in PRIMITIVE_TOPOLOGY_NAMES.items()
               if number and not name.endswith("-adjacency")}
_ROUTE_STREAMS = {("TEXCOORD", 0): int(VertexBufferType.UV0), ("TEXCOORD", 1): int(VertexBufferType.UV1),
                  ("TEXCOORD", 2): int(VertexBufferType.UV2), ("COLOR", 0): int(VertexBufferType.Colors)}


@dataclass(frozen=True, slots=True)
class MeshBuffer:
    streams: dict[int, tuple[bytes, int]]
    vertex_count: int
    indices: bytes
    wide: bool

    @classmethod
    def from_payload(cls, payload) -> MeshBuffer:
        return cls({int(stream.type): (bytes(stream.data), int(stream.stride)) for stream in payload.attribute_streams},
                   int(payload.vertex_count), bytes(payload.face_bytes), payload.integer_faces is not None)


@dataclass(frozen=True, slots=True)
class SubmeshRange:
    start: int
    count: int
    base_vertex: int

    @classmethod
    def from_submesh(cls, submesh) -> SubmeshRange:
        return cls(int(submesh.faces_index_offset), int(submesh.indices_count), int(submesh.verts_index_offset))


@dataclass(frozen=True, slots=True)
class _InstanceSlot:
    name: str


def submesh_geometry(program, vertex, buffer: MeshBuffer, submesh: SubmeshRange,
                     destination: Destination | None = None, instance: Mapping[str, bytes] | None = None) -> dict:
    layout = program.input_layout
    if layout is None:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP("MaterialPreview", "The program has no vertex input layout."))
    declared = {(item.semantic_name.upper(), item.semantic_index) for item in vertex.inputs}
    streams = buffer.streams
    used, elements = [], []
    for element in layout.elements:
        if (element.semantic.upper(), element.semantic_index) not in declared:
            continue
        if element.per_instance:
            name = f"{element.semantic.upper()}{element.semantic_index}"
            if instance is None or name not in instance:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The program reads {semantic}{index} per instance, which the engine supplies."),
                    {"semantic": element.semantic, "index": element.semantic_index})
            slot = _InstanceSlot(name)
            if slot not in used:
                used.append(slot)
            elements.append((element.semantic, element.semantic_index, element.dxgi_format_id, used.index(slot),
                             element.aligned_byte_offset, 1, 1))
            continue
        slot = element.input_slot
        if destination is not None:
            slot = (destination_view(destination, element.semantic, element.semantic_index)
                    or _ROUTE_STREAMS.get((element.semantic, element.semantic_index)))
            if slot is None:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The program reads {semantic}{index}, which no capture shows the renderer "
                                       "binding for the pre-transform route."),
                    {"semantic": element.semantic, "index": element.semantic_index})
        lies_there = isinstance(slot, tuple)
        if not lies_there and slot not in streams and element.semantic == "TEXCOORD":
            slot = int(VertexBufferType.UV0)
        if not lies_there and slot not in streams and element.semantic != "COLOR":
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The mesh has no stream {slot} for the program's input {semantic}{index}."),
                {"slot": element.input_slot, "semantic": element.semantic, "index": element.semantic_index})
        if slot not in used:
            used.append(slot)
        elements.append((element.semantic, element.semantic_index, element.dxgi_format_id, used.index(slot),
                         element.aligned_byte_offset, 0, 0))

    def vertex_buffer(index: int, slot: int | tuple[int, int] | _InstanceSlot) -> tuple:
        if isinstance(slot, _InstanceSlot):
            data = bytes(instance[slot.name])
            return data, len(data)
        if isinstance(slot, tuple):
            return None, *slot
        if slot in streams:
            return streams[slot]
        reach = max(row[4] for row in elements if row[3] == index)
        return bytes([255]) * (reach + _WHITE_STRIDE * buffer.vertex_count), _WHITE_STRIDE
    size = 4 if buffer.wide else 2
    start, count = submesh.start, submesh.count
    return {"vertex_buffers": [vertex_buffer(index, slot) for index, slot in enumerate(used)], "elements": elements,
            "indices": buffer.indices[start * size:(start + count) * size],
            "index_format": DXGI_FORMAT_R32_UINT if buffer.wide else DXGI_FORMAT_R16_UINT,
            "draw": (count, 1, 0, submesh.base_vertex, 0)}


def program_states(program, targets: int, stencil_reference: int) -> dict:
    raster, depth, blend = program.rasterizer_state, program.depth_stencil_state, program.blend_state
    if None in (raster, depth, blend):
        raise MaterialPreviewError(QT_TRANSLATE_NOOP("MaterialPreview", "The program has no fixed-function states."))
    if program.primitive_topology not in _TOPOLOGIES:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The program draws the primitive topology {topology}, which the preview device does "
                               "not."), {"topology": program.primitive_topology})
    if raster.conservative_rasterization or depth.depth_bounds_test_enable:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The program rasterizes conservatively or tests depth bounds, which the preview "
                               "device does not."))
    if len(blend.render_targets) < targets:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The program blends {count} render targets, not {targets}."),
            {"count": len(blend.render_targets), "targets": targets})

    def face(item):
        return (item.stencil_fail_operation_id, item.stencil_depth_fail_operation_id, item.stencil_pass_operation_id,
                item.stencil_function_id)
    return {"topology": _TOPOLOGIES[program.primitive_topology],
            "rasterizer": (raster.fill_mode_id, raster.cull_mode_id, int(raster.front_counter_clockwise),
                           raster.depth_bias, raster.depth_bias_clamp, raster.slope_scaled_depth_bias,
                           int(raster.depth_clip_enable)),
            "depth_stencil": (int(depth.depth_enable), depth.depth_write_mask_id, depth.depth_function_id,
                              int(depth.stencil_enable), depth.stencil_read_mask, depth.stencil_write_mask,
                              face(depth.front_face), face(depth.back_face), stencil_reference),
            "blend": (int(blend.alpha_to_coverage_enable), int(blend.independent_blend_enable),
                      [(int(item.blend_enable), item.source_blend_id, item.destination_blend_id,
                        item.blend_operation_id, item.source_alpha_blend_id, item.destination_alpha_blend_id,
                        item.alpha_blend_operation_id, item.write_mask) for item in blend.render_targets[:targets]])}

