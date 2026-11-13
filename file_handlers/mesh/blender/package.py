from __future__ import annotations

import json
from pathlib import Path

import numpy as np

FORMAT = "reasy-blend"
FORMAT_VERSION = 1
BLEND_FORMAT = 2

AXES = np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], dtype=np.float64)


def to_blender(vectors):
    values = np.asarray(vectors, dtype=np.float32).reshape(-1, 3)
    return np.column_stack((values[:, 0], -values[:, 2], values[:, 1]))


def from_blender(vectors):
    values = np.asarray(vectors, dtype=np.float32).reshape(-1, 3)
    return np.column_stack((values[:, 0], values[:, 2], -values[:, 1]))


def flip_uv(uv):
    values = np.asarray(uv, dtype=np.float32).reshape(-1, 2).copy()
    values[:, 1] = np.float32(1.0) - values[:, 1]
    return values


def matrix_to_blender(row_major):
    axes = np.eye(4)
    axes[:3, :3] = AXES
    return axes @ np.asarray(row_major, dtype=np.float64).reshape(4, 4).T


def matrix_from_blender(matrix, blend_format):
    axes = np.eye(4)
    axes[:3, :3] = AXES
    # Bones of format 1 turned their own axes into Blender's too.
    column = axes.T @ np.asarray(matrix, dtype=np.float64).reshape(4, 4) @ (axes if blend_format == 1 else np.eye(4))
    return column.T.reshape(-1)


def orthonormal(matrix):
    result = np.array(matrix, np.float64).reshape(4, 4)
    u, _s, vt = np.linalg.svd(result[:3, :3])
    rotation = u @ vt
    if np.linalg.det(rotation) < 0:
        rotation[:, 0] = -rotation[:, 0]
    result[:3, :3], result[3] = rotation, (0, 0, 0, 1)
    return result


def save_package(directory, manifest, arrays):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = dict(manifest, format=FORMAT, format_version=FORMAT_VERSION)
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    np.savez(directory / "arrays.npz", **arrays)


def load_package(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != FORMAT:
        raise ValueError("Not a REasy Blender package")
    if int(manifest.get("format_version", 0)) > FORMAT_VERSION:
        raise ValueError("The Blender package was written by a newer REasy")
    with np.load(directory / "arrays.npz", allow_pickle=False) as archive:
        arrays = {name: archive[name] for name in archive.files}
    return manifest, arrays
