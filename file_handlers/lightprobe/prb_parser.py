from __future__ import annotations

import struct
from array import array

import numpy as np

from .data import PrbData


def parse_prb_v9(data: bytes) -> PrbData:
    if len(data) < 4:
        raise ValueError("PRB data is too small")
    probe_count = struct.unpack_from("<I", data, 0)[0]
    positions_end = 4 + (probe_count * 16)
    if positions_end + 4 > len(data):
        raise ValueError("PRB probe position table is truncated")
    probe_positions = np.frombuffer(data, dtype="<f4", count=probe_count * 4, offset=4).reshape(probe_count, 4)[:, :3].astype(np.float32, copy=True)

    tetrahedron_count = struct.unpack_from("<I", data, positions_end)[0]
    tetra_start = positions_end + 4
    tetra_end = tetra_start + (tetrahedron_count * 80)
    if tetra_end > len(data):
        raise ValueError("PRB tetrahedron table is truncated")

    tetra_probe_indices = array("I")
    tetra_neighbors = array("i")
    tetra_transforms = array("f")
    record = struct.Struct("<4I4i12f")
    for unpacked in record.iter_unpack(memoryview(data)[tetra_start:tetra_end]):
        tetra_probe_indices.extend(unpacked[:4])
        tetra_neighbors.extend(unpacked[4:8])
        tetra_transforms.extend(unpacked[8:])
    if np.little_endian is False:
        tetra_probe_indices.byteswap()
        tetra_neighbors.byteswap()
        tetra_transforms.byteswap()

    if tetra_end + 36 > len(data):
        raise ValueError("PRB BSP header is missing")
    data_size = struct.unpack_from("<I", data, tetra_end)[0]
    bsp_payload_end = tetra_end + 4 + data_size
    if bsp_payload_end > len(data):
        raise ValueError("PRB BSP payload is truncated")
    grid_bias = struct.unpack_from("<3f", data, tetra_end + 4)
    grid_entry_count = struct.unpack_from("<I", data, tetra_end + 16)[0]
    inv_cell_size = struct.unpack_from("<3f", data, tetra_end + 20)
    linear_z_stride, linear_y_stride = struct.unpack_from("<HH", data, tetra_end + 32)
    dim_z = linear_y_stride
    dim_y = linear_z_stride // dim_z if dim_z else 0
    dim_x = grid_entry_count // linear_z_stride if linear_z_stride else 0

    grid_start = tetra_end + 36
    grid_end = grid_start + (grid_entry_count * 4)
    if grid_end > bsp_payload_end:
        raise ValueError("PRB BSP grid is truncated")
    bsp_padding = data[grid_end:bsp_payload_end]
    if bsp_padding and any(value != 0xFF for value in bsp_padding):
        raise ValueError("PRB BSP payload has unsupported non-padding bytes")
    if bsp_payload_end != len(data):
        raise ValueError("PRB data has unsupported trailing bytes")
    grid_indices = np.frombuffer(data, dtype="<u4", count=grid_entry_count, offset=grid_start).astype(np.uint32, copy=True)

    return PrbData(
        probe_count=int(probe_count),
        probe_positions=probe_positions,
        tetrahedron_count=int(tetrahedron_count),
        tetra_probe_indices=np.asarray(tetra_probe_indices, dtype=np.uint32),
        tetra_neighbors=np.asarray(tetra_neighbors, dtype=np.int32),
        tetra_transforms=np.asarray(tetra_transforms, dtype=np.float32),
        grid_bias=tuple(float(value) for value in grid_bias),
        inv_cell_size=tuple(float(value) for value in inv_cell_size),
        linear_z_stride=int(linear_z_stride),
        linear_y_stride=int(linear_y_stride),
        grid_dimensions=(int(dim_x), int(dim_y), int(dim_z)),
        grid_indices=grid_indices,
        bsp_data_size=int(data_size),
        bsp_padding_size=len(bsp_padding),
    )
