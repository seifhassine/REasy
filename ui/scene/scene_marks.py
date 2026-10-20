from __future__ import annotations

from collections.abc import Iterable, Set

import numpy as np

lazy from file_handlers.mesh.lit_preview import LitLines, LitPoints, LitTriangles

GIZMO_AXES = np.identity(3, dtype=np.float32)
GIZMO_COLORS = ((1.0, 0.18, 0.12), (0.2, 0.9, 0.25), (0.25, 0.45, 1.0))
GIZMO_BOX_FACES = ((0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1), (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0))
BOX_EDGES = ((0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7))
# Corners are numbered x * 4 + y * 2 + z, each 0 at the minimum and 1 at the maximum.
ALIGNED_EDGES = tuple((corner, corner | axis) for corner in range(8) for axis in (4, 2, 1) if not corner & axis)
GIZMO_PLANES = ((0, 1, (1.0, 0.85, 0.12, 0.24)), (0, 2, (0.9, 0.15, 1.0, 0.22)), (1, 2, (0.15, 0.8, 1.0, 0.22)))
_CENTRE_COLOUR = (0.95, 0.95, 0.9, 1.0)
_PROBE_BOX_COLOUR = (0.1, 0.9, 1.0)
_PROBE_AXIS_COLOURS = ((1.0, 0.25, 0.2), (0.25, 1.0, 0.35), (0.35, 0.55, 1.0))


def unorm(colour: Iterable[float]) -> tuple[int, ...]:
    channels = [round(float(channel) * 255) for channel in colour]
    return (*channels, 255) if len(channels) == 3 else tuple(channels)


def gizmo_circle_points(center: np.ndarray, radius: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    angles = np.linspace(0.0, np.pi * 2.0, 49, dtype=np.float32)
    circle = np.stack((np.cos(angles), np.sin(angles)), axis=1) * radius
    return (center + np.column_stack((np.zeros(len(circle)), circle[:, 0], circle[:, 1])),
            center + np.column_stack((circle[:, 0], np.zeros(len(circle)), circle[:, 1])),
            center + np.column_stack((circle[:, 0], circle[:, 1], np.zeros(len(circle)))))


def gizmo_plane_corners(center: np.ndarray, size: float, a: int, b: int) -> tuple[np.ndarray, ...]:
    u, v = GIZMO_AXES[a], GIZMO_AXES[b]
    return (center + (u + v) * size * 0.22, center + (u * 0.42 + v * 0.22) * size, center + (u + v) * size * 0.42,
            center + (u * 0.22 + v * 0.42) * size)


def _axis_colour(axis: int, active) -> tuple[int, ...]:
    colour = np.array(GIZMO_COLORS[axis], dtype=np.float32)
    if axis == active:
        colour = np.clip(colour * 1.25 + 0.35, 0.0, 1.0)
    return unorm(colour)


def _cube(center: np.ndarray, x: np.ndarray, y: np.ndarray, z: np.ndarray) -> np.ndarray:
    corners = [center + x * first + y * second + z * third
               for first in (1, -1) for second, third in ((1, 1), (1, -1), (-1, -1), (-1, 1))]
    return np.array([[corners[face[0]], corners[face[i]], corners[face[i + 1]]]
                     for face in GIZMO_BOX_FACES for i in (1, 2)], np.float64)


def _arrow(center: np.ndarray, axis: int, size: float, active) -> list:
    direction = GIZMO_AXES[axis]
    p1, p2 = GIZMO_AXES[(axis + 1) % 3], GIZMO_AXES[(axis + 2) % 3]
    end = center + direction * size
    base = end - direction * size * 0.16
    radius = size * (0.07 if axis == active else 0.05)
    ring = (base + p1 * radius, base + p2 * radius, base - p1 * radius, base - p2 * radius)
    colour = _axis_colour(axis, active)
    return [LitLines(np.array([[center, base]], np.float64), 4.0 if axis == active else 2.4, colour, False),
            LitTriangles(np.array([[end, a, b] for a, b in zip(ring, ring[1:] + ring[:1])], np.float64), colour,
                         False)]


def gizmo_marks(mode: str, center: np.ndarray, size: float, active) -> tuple:
    center = np.asarray(center, np.float64)
    marks = []
    if mode == "rotation":
        for axis, points in enumerate(gizmo_circle_points(center, size)):
            marks.append(LitLines(np.stack([points[:-1], points[1:]], axis=1).astype(np.float64),
                                  4.5 if axis == active else 2.4, _axis_colour(axis, active), False))
    elif mode == "scale":
        for axis in range(3):
            marks += _arrow(center, axis, size * 0.82, active)
            direction = GIZMO_AXES[axis]
            p1, p2 = GIZMO_AXES[(axis + 1) % 3], GIZMO_AXES[(axis + 2) % 3]
            end = center + direction * size
            half = size * (0.085 if axis == active else 0.065)
            marks.append(LitTriangles(_cube(end, direction * half, p1 * half, p2 * half), _axis_colour(axis, active),
                                      False))
    else:
        for a, b, colour in GIZMO_PLANES:
            rgba = np.array(colour, dtype=np.float32)
            if active in ((a, b), (b, a)):
                rgba[:3] = np.clip(rgba[:3] * 1.2 + 0.25, 0.0, 1.0)
                rgba[3] = min(float(rgba[3]) * 1.9, 0.55)
            q = gizmo_plane_corners(center, size, a, b)
            marks.append(LitTriangles(np.array([[q[0], q[1], q[2]], [q[0], q[2], q[3]]], np.float64), unorm(rgba),
                                      False))
        for axis in range(3):
            marks += _arrow(center, axis, size, active)
    marks.append(LitTriangles(_cube(center, *GIZMO_AXES * size * 0.045), unorm(_CENTRE_COLOUR), False))
    return tuple(marks)


def probe_marks(boxes: Iterable[tuple[str, object]], selected: Set[str], points: np.ndarray,
                colours: np.ndarray) -> tuple:
    marks = []
    for key, box in boxes:
        corners = np.asarray(box.corners(), np.float64).reshape(8, 3)
        if not np.isfinite(corners).all():
            continue
        alpha = 0.9 if key in selected else 0.55
        marks.append(LitLines(corners[np.array(BOX_EDGES)], 2.0, unorm((*_PROBE_BOX_COLOUR, alpha)), False))
        center = np.asarray(box.center, np.float64).reshape(3)
        axes = np.asarray(box.axes, np.float64).reshape(3, 3)
        extent = np.asarray(box.extent, np.float64).reshape(3)
        if not (np.isfinite(center).all() and np.isfinite(axes).all() and np.isfinite(extent).all()):
            continue
        for axis, colour in enumerate(_PROBE_AXIS_COLOURS):
            end = center + axes[axis] * max(float(extent[axis]), 0.0)
            marks.append(LitLines(np.array([[center, end]]), 1.5, unorm((*colour, alpha)), False))
    if len(points):
        rgb = np.maximum(np.asarray(colours, np.float64)[:, :3], 0.08)
        shown = np.rint(np.hstack([rgb, np.full((len(rgb), 1), 0.88)]) * 255).astype(np.int64)
        marks.append(LitPoints(np.asarray(points, np.float64).reshape(-1, 3), 4.0, shown))
    return tuple(marks)


def bounds_marks(boxes: Iterable[tuple]) -> tuple:
    marks = []
    for minimum, maximum, colour in boxes:
        ends = np.asarray([minimum, maximum], np.float64).reshape(2, 3)
        corners = np.array([[ends[(corner >> (2 - axis)) & 1, axis] for axis in range(3)] for corner in range(8)])
        marks.append(LitLines(corners[np.array(ALIGNED_EDGES)], 1.5, unorm(colour)))
    return tuple(marks)


__all__ = ["ALIGNED_EDGES", "BOX_EDGES", "GIZMO_AXES", "GIZMO_BOX_FACES", "GIZMO_COLORS", "GIZMO_PLANES",
           "bounds_marks", "gizmo_circle_points", "gizmo_marks", "gizmo_plane_corners", "probe_marks", "unorm"]
