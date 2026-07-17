from __future__ import annotations

import struct

import numpy as np

from .data import PrbData


_SUPPORTED_VERSIONS = frozenset((9, 10, 11))
_LEGACY_TETRAHEDRON_DTYPE = np.dtype(
    [
        ("probe_indices", "<u4", (4,)),
        ("neighbors", "<i4", (4,)),
        ("transform", "<f4", (3, 4)),
    ]
)
_COMPACT_TETRAHEDRON_SIZE = 24
_UINT24_MASK = np.uint32(0xFFFFFF)
_BSP_HEADER = struct.Struct("<3fI3fHH")


def parse_prb(data: bytes, *, version: int | None = None) -> PrbData:
    """Parse PRB based on version from file suffix.

    ``None`` retains the original v9/v10 layout for unversioned callers; v11
    must be selected explicitly because the payload has no internla version.
    """
    if version is not None and version not in _SUPPORTED_VERSIONS:
        raise ValueError(f"Unsupported PRB version: {version}")
    if len(data) < 4:
        raise ValueError("PRB data is too small")

    probe_count = struct.unpack_from("<I", data, 0)[0]
    positions_end = 4 + (probe_count * 16)
    if positions_end + 4 > len(data):
        raise ValueError("PRB probe position table is truncated")
    probe_positions = np.frombuffer(
        data,
        dtype="<f4",
        count=probe_count * 4,
        offset=4,
    ).reshape(probe_count, 4)[:, :3].astype(np.float32, copy=True)

    tetrahedron_count = struct.unpack_from("<I", data, positions_end)[0]
    tetra_start = positions_end + 4
    (
        tetra_probe_indices,
        tetra_neighbors,
        tetra_transforms,
        tetra_end,
    ) = _parse_tetrahedra(
        data,
        tetra_start=tetra_start,
        tetrahedron_count=tetrahedron_count,
        version=version,
    )
    _validate_tetrahedra(
        tetra_probe_indices,
        tetra_neighbors,
        probe_count=probe_count,
        tetrahedron_count=tetrahedron_count,
    )

    if tetra_end + 4 > len(data):
        raise ValueError("PRB BSP data size is missing")
    data_size = struct.unpack_from("<I", data, tetra_end)[0]
    bsp_start = tetra_end + 4
    bsp_payload_end = bsp_start + data_size
    if data_size < _BSP_HEADER.size:
        raise ValueError("PRB BSP header is truncated")
    if bsp_payload_end > len(data):
        raise ValueError("PRB BSP payload is truncated")
    unpacked_header = _BSP_HEADER.unpack_from(data, bsp_start)
    grid_bias = unpacked_header[:3]
    grid_entry_count = unpacked_header[3]
    inv_cell_size = unpacked_header[4:7]
    linear_x_stride, linear_y_stride = unpacked_header[7:9]
    grid_dimensions = _grid_dimensions(
        grid_entry_count,
        linear_x_stride,
        linear_y_stride,
    )

    grid_start = bsp_start + _BSP_HEADER.size
    grid_end = grid_start + (grid_entry_count * 4)
    if grid_end > bsp_payload_end:
        raise ValueError("PRB BSP grid is truncated")
    bsp_padding = data[grid_end:bsp_payload_end]
    if bsp_padding and any(value != 0xFF for value in bsp_padding):
        raise ValueError("PRB BSP payload has unsupported non-padding bytes")
    if bsp_payload_end != len(data):
        raise ValueError("PRB data has unsupported trailing bytes")
    grid_indices = np.frombuffer(
        data,
        dtype="<u4",
        count=grid_entry_count,
        offset=grid_start,
    ).astype(np.uint32, copy=True)
    if version == 11:
        valid_grid_indices = grid_indices < _UINT24_MASK
        grid_indices[~valid_grid_indices] = np.uint32(0xFFFFFFFF)
    else:
        valid_grid_indices = grid_indices != np.uint32(0xFFFFFFFF)
    if np.any(grid_indices[valid_grid_indices] >= tetrahedron_count):
        raise ValueError("PRB BSP grid contains an invalid tetrahedron index")

    return PrbData(
        probe_count=int(probe_count),
        probe_positions=probe_positions,
        tetrahedron_count=int(tetrahedron_count),
        tetra_probe_indices=tetra_probe_indices,
        tetra_neighbors=tetra_neighbors,
        tetra_transforms=tetra_transforms,
        grid_bias=tuple(float(value) for value in grid_bias),
        inv_cell_size=tuple(float(value) for value in inv_cell_size),
        linear_x_stride=int(linear_x_stride),
        linear_y_stride=int(linear_y_stride),
        grid_dimensions=grid_dimensions,
        grid_indices=grid_indices,
        bsp_data_size=int(data_size),
        bsp_padding_size=len(bsp_padding),
        version=version,
    )


def _parse_tetrahedra(
    data: bytes,
    *,
    tetra_start: int,
    tetrahedron_count: int,
    version: int | None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray | None, int]:
    if version == 11:
        tetra_end = tetra_start + tetrahedron_count * _COMPACT_TETRAHEDRON_SIZE
        if tetra_end > len(data):
            raise ValueError("PRB tetrahedron table is truncated")
        packed = np.frombuffer(
            data,
            dtype="<u4",
            count=tetrahedron_count * 6,
            offset=tetra_start,
        ).reshape(-1, 6)
        probe_indices = _unpack_uint24_quads(packed[:, :3]).reshape(-1)
        packed_neighbors = _unpack_uint24_quads(packed[:, 3:])
        neighbors = packed_neighbors.view(np.int32)
        neighbors[packed_neighbors == _UINT24_MASK] = -1
        return probe_indices, neighbors.reshape(-1), None, tetra_end

    tetra_end = (
        tetra_start + tetrahedron_count * _LEGACY_TETRAHEDRON_DTYPE.itemsize
    )
    if tetra_end > len(data):
        raise ValueError("PRB tetrahedron table is truncated")
    tetrahedra = np.frombuffer(
        data,
        dtype=_LEGACY_TETRAHEDRON_DTYPE,
        count=tetrahedron_count,
        offset=tetra_start,
    )
    return (
        tetrahedra["probe_indices"].astype(np.uint32, copy=True).reshape(-1),
        tetrahedra["neighbors"].astype(np.int32, copy=True).reshape(-1),
        tetrahedra["transform"].astype(np.float32, copy=True).reshape(-1),
        tetra_end,
    )


def _unpack_uint24_quads(packed: np.ndarray) -> np.ndarray:
    """Unpack four uint24 values stored across three little-endian uint32s."""
    words = np.asarray(packed, dtype=np.uint32).reshape(-1, 3)
    values = np.empty((len(words), 4), dtype=np.uint32)
    values[:, :3] = words & _UINT24_MASK
    values[:, 3] = (
        (words[:, 0] >> np.uint32(24))
        | ((words[:, 1] >> np.uint32(24)) << np.uint32(8))
        | ((words[:, 2] >> np.uint32(24)) << np.uint32(16))
    )
    return values


def _validate_tetrahedra(
    probe_indices: np.ndarray,
    neighbors: np.ndarray,
    *,
    probe_count: int,
    tetrahedron_count: int,
) -> None:
    if np.any(probe_indices >= probe_count):
        raise ValueError("PRB tetrahedron contains an invalid probe index")
    if np.any((neighbors < -1) | (neighbors >= tetrahedron_count)):
        raise ValueError("PRB tetrahedron contains an invalid neighbor index")


def _grid_dimensions(
    entry_count: int,
    linear_x_stride: int,
    linear_y_stride: int,
) -> tuple[int, int, int]:
    if entry_count == 0:
        return 0, 0, 0
    if not linear_x_stride or not linear_y_stride:
        raise ValueError("PRB BSP grid has a zero linear stride")
    if linear_x_stride % linear_y_stride or entry_count % linear_x_stride:
        raise ValueError("PRB BSP grid dimensions are inconsistent with its strides")
    return (
        entry_count // linear_x_stride,
        linear_x_stride // linear_y_stride,
        linear_y_stride,
    )


def parse_prb_v9(data: bytes) -> PrbData:
    return parse_prb(data, version=9)


def parse_prb_v10(data: bytes) -> PrbData:
    return parse_prb(data, version=10)


def parse_prb_v11(data: bytes) -> PrbData:
    return parse_prb(data, version=11)
