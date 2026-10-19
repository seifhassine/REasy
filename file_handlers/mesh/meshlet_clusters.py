"""A game drops triangles past its meshlet and chunk limits."""

from __future__ import annotations

from collections import deque
from dataclasses import replace

import numpy as np

from .conversion_allocations import select_skin_rows
from .mesh_file import MPLY_VECTOR_PLANES, MPLYChunk

# Raw bytes of the whole source chunk; the writer lays them out again, so sub-chunks clear them.
_CHUNK_BYTES = ("skin_record_bytes", "tangent_mask_bytes", "face_alignment_padding", "position_alignment_padding",
                "trailing_payload")


def meshletize(triangles, points, max_vertices, max_triangles):
    """``points`` gives each corner's location, so flat-shaded or seam-split geometry still clusters compactly;
    ``max_vertices`` counts native vertices of ``triangles``."""
    count = len(triangles)
    if not count:
        return []
    _ids, dense = np.unique(points.ravel(), return_inverse=True)
    dense = dense.ravel()
    order = np.argsort(dense, kind="stable")
    touching = (order // 3).tolist()
    bounds = np.searchsorted(dense[order], np.arange(len(_ids) + 1)).tolist()
    corner_points = dense.reshape(-1, 3).tolist()
    corners = triangles.tolist()
    assigned = bytearray(count)
    clusters = []
    cursor = 0
    while cursor < count:
        if assigned[cursor]:
            cursor += 1
            continue
        members, vertices = [], set()
        queue, queued = deque([cursor]), {cursor}
        # Its own scan: triangles this meshlet rejected stay unassigned for the outer cursor to seed later.
        scan = cursor
        while len(members) < max_triangles:
            if not queue:
                while scan < count and (assigned[scan] or scan in queued):
                    scan += 1
                if scan >= count:
                    break
                queue.append(scan)
                queued.add(scan)
            index = queue.popleft()
            if assigned[index]:
                continue
            corner = vertices.union(corners[index])
            if len(corner) > max_vertices:
                if not members:
                    raise ValueError("A triangle cannot fit a meshlet")
                if not queue:
                    break
                continue
            assigned[index] = 1
            members.append(index)
            vertices = corner
            for point in corner_points[index]:
                for neighbor in touching[bounds[point]:bounds[point + 1]]:
                    if not assigned[neighbor] and neighbor not in queued:
                        queued.add(neighbor)
                        queue.append(neighbor)
        clusters.append(members)
    return clusters


def _sub_chunk(chunk: MPLYChunk, members) -> MPLYChunk:
    corners = chunk.faces.reshape(-1, 3)[members].ravel()
    _used, first = np.unique(corners, return_index=True)
    vertices = corners[np.sort(first)]
    local = np.empty(chunk.vert_count, np.int64)
    local[vertices] = np.arange(len(vertices))
    planes = {name: getattr(chunk, name)[vertices]
              for name in (*MPLY_VECTOR_PLANES, "normal_ws", "tangent_ws", "tangent_mask_bits")
              if len(getattr(chunk, name))}
    if len(chunk.colors):
        planes["colors"] = chunk.colors.reshape(-1, 4)[vertices].ravel()
    skin = chunk.skin_weights and select_skin_rows(chunk.skin_weights, vertices, len(vertices))
    return replace(chunk, vert_count=len(vertices), faces=local[corners], skin_weights=skin, cluster_aabb=None,
                   **planes, **dict.fromkeys(_CHUNK_BYTES, b""))


def split_chunk(chunk: MPLYChunk, max_vertices: int, max_triangles: int) -> list[MPLYChunk]:
    triangles = chunk.faces.reshape(-1, 3)
    if len(triangles) <= max_triangles and chunk.vert_count <= max_vertices:
        return [chunk]
    return [_sub_chunk(chunk, members) for members in meshletize(triangles, triangles, max_vertices, max_triangles)]


__all__ = ["meshletize", "split_chunk"]
