"""See docs/internals/motion-editor.md, Blender."""

from __future__ import annotations

import math
from pathlib import Path

lazy import numpy as np

lazy from file_handlers.mesh.blender.export import bone_lengths
lazy from file_handlers.mesh.blender.package import AXES, load_package, save_package
lazy from file_handlers.mesh.blender.runner import run_script, temporary_directory

from .keys import full_track
from .model import Motion, Track
from .pose import TRANSFORMS, MotionSampler, Rig
from .retarget import Retarget, bones_of
from .rules import mot_profile

CHANNELS = {"translation": "location", "rotation": "rotation_quaternion", "scale": "scale"}
TOLERANCE = {"translation": 1e-5, "rotation": 1e-6, "scale": 1e-6}


def _product(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    ax, ay, az, aw = np.moveaxis(a, -1, 0)
    bx, by, bz, bw = np.moveaxis(b, -1, 0)
    return np.stack([aw * bx + ax * bw + ay * bz - az * by, aw * by - ax * bz + ay * bw + az * bx,
                     aw * bz + ax * by - ay * bx + az * bw, aw * bw - ax * bx - ay * by - az * bz], -1)


def _rotate(q: np.ndarray, v: np.ndarray) -> np.ndarray:
    twice = 2.0 * np.cross(q[..., :3], v)
    return v + q[..., 3:] * twice + np.cross(q[..., :3], twice)


def _inverse(q: np.ndarray) -> np.ndarray:
    return q * np.array([-1.0, -1.0, -1.0, 1.0])


def _rotation(m: np.ndarray) -> np.ndarray:
    (m00, m01, m02), (m10, m11, m12), (m20, m21, m22) = np.moveaxis(m, (-2, -1), (0, 1))
    k = np.stack([np.stack([m00 - m11 - m22, m01 + m10, m02 + m20, m21 - m12], -1),
                  np.stack([m01 + m10, m11 - m00 - m22, m12 + m21, m02 - m20], -1),
                  np.stack([m02 + m20, m12 + m21, m22 - m00 - m11, m10 - m01], -1),
                  np.stack([m21 - m12, m02 - m20, m10 - m01, m00 + m11 + m22], -1)], -2)
    return np.linalg.eigh(k)[1][..., -1]


def _decompose(matrices: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    basis = matrices[..., :3, :3]
    scales = np.linalg.norm(basis, axis=-2)
    axes = basis / np.maximum(scales, np.finfo(np.float64).tiny)[..., None, :]
    readable = scales.min(-1) > 1e-4 * scales.max(-1)
    mirrored = np.where(readable & (np.linalg.det(axes) < 0), -1.0, 1.0)
    scales[..., 0] *= mirrored
    axes[..., 0] *= mirrored[..., None]
    shortest = np.argmin(np.abs(scales), -1)
    for axis in range(3):
        crossed = np.cross(axes[..., (axis + 1) % 3], axes[..., (axis + 2) % 3])
        axes[..., axis] = np.where((shortest == axis)[..., None], crossed, axes[..., axis])
    return matrices[..., :3, 3], _rotation(axes), scales


def action_package(rig: Rig, motions: list[Motion], title: str) -> tuple[dict, dict]:
    axes = _axes()
    world = axes @ rig.models(rig.base_locals).astype(np.float64) @ axes.T
    arrays = {"bones.matrix": world.reshape(-1, 16),
              "bones.length": bone_lengths(rig.parents.tolist(), world[:, :3, 3])}
    t0, q0 = (array.astype(np.float64) for array in rig.base[:2])
    actions = []
    for number, motion in enumerate(motions):
        sampler = MotionSampler(motion)
        binding = rig.bind(sampler.hashes)
        frames = np.arange(int(math.floor(max(motion.end_frame, 0.0))) + 1)
        samples = [sampler.sample(float(frame)) for frame in frames]
        posed = [rig.posed(sample, binding) for sample in samples]
        spec = {"name": motion.name, "fps": motion.fps or 60}
        for component, name in enumerate(TRANSFORMS):
            keyed = np.zeros(len(rig.hashes), bool)
            for values, flags in samples:
                keyed[binding[0]] |= flags[component][binding[1]]
            joints = np.flatnonzero(keyed)
            values = np.array([pose[component][joints] for pose in posed], np.float64)
            if name == "translation":
                values = _rotate(_inverse(q0[joints]), values - t0[joints]) @ AXES.T
            elif name == "rotation":
                local = _product(_inverse(q0[joints]), values)
                values = np.concatenate([local[..., 3:], local[..., :3] @ AXES.T], -1)
            else:
                values = values[..., [0, 2, 1]]
            spec[name] = joints.tolist()
            arrays[f"a{number}.{name}"] = values.astype(np.float32)
        arrays[f"a{number}.frames"] = frames.astype(np.float32)
        actions.append(spec)
    manifest = {"title": title, "skeleton": {"names": list(rig.names), "parents": rig.parents.tolist(),
                                             "palette": list(range(len(rig.hashes)))},
                "channels": CHANNELS, "actions": actions}
    return manifest, arrays


def export_blend(blender, rig: Rig, motions: list[Motion], output, title: str) -> None:
    manifest, arrays = action_package(rig, motions, title)
    with temporary_directory() as folder:
        save_package(folder, manifest, arrays)
        run_script(blender, "motion_export", folder, Path(output))


def read_actions(blender, path) -> tuple[dict, dict]:
    with temporary_directory() as folder:
        run_script(blender, "motion_dump", Path(path), folder)
        return load_package(folder)


def _reduced(frames: np.ndarray, values: np.ndarray, tolerance: float) -> np.ndarray:
    kept, start = [0], 0
    for end in range(2, len(frames)):
        span = slice(start + 1, end)
        weight = ((frames[span] - frames[start]) / (frames[end] - frames[start]))[:, None]
        line = values[start] + (values[end] - values[start]) * weight
        if np.abs(line - values[span]).max(initial=0.0) > tolerance:
            kept.append(end - 1)
            start = end - 1
    return np.array(kept + [len(frames) - 1] if len(frames) > 1 else kept, np.int64)


def _compose(translations: np.ndarray, rotations: np.ndarray, scales: np.ndarray) -> np.ndarray:
    x, y, z, w = np.moveaxis(rotations / np.linalg.norm(rotations, axis=-1, keepdims=True), -1, 0)
    turn = np.stack([np.stack([1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)], -1),
                     np.stack([2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)], -1),
                     np.stack([2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)], -1)], -2)
    matrices = np.zeros((*translations.shape[:-1], 4, 4))
    matrices[..., :3, :3], matrices[..., :3, 3], matrices[..., 3, 3] = turn * scales[..., None, :], translations, 1.0
    return matrices


def _axes() -> np.ndarray:
    axes = np.eye(4)
    axes[:3, :3] = AXES
    return axes


def _rest(manifest: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    stored = np.asarray(manifest["rest"], np.float64).reshape(-1, 4, 4)
    built = manifest["built"]
    engine = np.array([len(matrix) == 16 for matrix in built], bool)
    source = stored.copy()
    source[engine] = np.array([built[bone] for bone in np.flatnonzero(engine)], np.float64).reshape(-1, 4, 4)
    return stored, source, engine


def _bone_parents(manifest: dict) -> list[int]:
    index = {name: bone for bone, name in enumerate(manifest["names"])}
    return [index.get(parent, -1) for parent in manifest["parents"]]


def action_bones(manifest: dict) -> list:
    """The armature's bones at rest, in engine space."""
    axes = _axes()
    return bones_of(manifest["names"], _bone_parents(manifest),
                    axes.T @ np.reshape(manifest["turn"], (4, 4)) @ _rest(manifest)[1] @ axes)


def action_tracks(manifest: dict, arrays: dict, number: int, rig: Rig, motion: Motion,
                  pairs=None) -> tuple[dict, float]:
    """Tracks by joint name hash and transform, and the end frame; ``pairs`` of bone and joint names map the
    armature onto the rig (else equal names)."""
    action, names = manifest["actions"][number], manifest["names"]
    axes = _axes()
    space = axes.T @ np.reshape(manifest["turn"], (4, 4))
    stored, source, engine = _rest(manifest)
    posed = arrays[f"a{number}.matrix"].astype(np.float64).reshape(-1, len(names), 4, 4)
    frames_of = space @ posed @ np.linalg.inv(stored) @ source @ axes
    plan = Retarget.of(rig, names, _bone_parents(manifest), space @ source @ axes,
                       [(name, name) for name in names] if pairs is None else pairs, engine)
    scales = np.linalg.norm(rig.models(rig.base_locals)[:, :3, :3].astype(np.float64), axis=1)
    turns = np.tile(np.eye(4), (len(rig.hashes), 1, 1))
    turns[:, :3, :3] = plan.turns * scales[:, None, :]
    keyed = np.zeros((len(rig.hashes), 3), bool)
    for row in np.flatnonzero(plan.bones >= 0):
        components = action["bones"].get(names[plan.bones[row]], ())
        keyed[row] = ("location" in components, any(component.startswith("rotation") for component in components),
                      "scale" in components)
    values = [np.broadcast_to(base.astype(np.float64), (len(posed), *base.shape)).copy() for base in rig.base]
    world = np.empty((len(posed), len(rig.hashes), 4, 4))
    for level in rig.levels:
        parents = rig.parents[level]
        above = np.where((parents >= 0)[:, None, None], world[:, np.maximum(parents, 0)], np.eye(4))
        if (moved := keyed[level].any(1)).any():
            rows = level[moved]
            bones, anchors = plan.bones[rows], plan.anchors[rows]
            aim = frames_of[:, bones] @ turns[rows]
            held, first = (anchors >= 0)[:, None], frames_of[:1, bones][..., :3, 3]
            start = np.where(held, world[:, np.maximum(anchors, 0)][..., :3, 3], first)
            origin = np.where(held, frames_of[:, plan.bones[np.maximum(anchors, 0)]][..., :3, 3], first)
            aim[..., :3, 3] = start + plan.scale * (frames_of[:, bones][..., :3, 3] - origin)
            for component, part in enumerate(_decompose(np.linalg.inv(above[:, moved]) @ aim)):
                mask = keyed[rows, component]
                values[component][:, rows[mask]] = part[:, mask]
        world[:, level] = above @ _compose(*(value[:, level] for value in values))
    rotations = values[1]
    flips = np.cumsum(np.sum(rotations[1:] * rotations[:-1], -1) < 0, axis=0) % 2
    rotations[1:] *= np.where(flips, -1.0, 1.0)[..., None]
    frames = np.arange(len(posed))
    legacy = mot_profile(motion.version).rules.tracks == "legacy"
    end = float(len(posed) - 1)
    kinds = {"translation": "vector3", "rotation": "quaternion", "scale": "vector3"}
    tracks: dict[int, dict[str, Track]] = {}
    for row, component in zip(*np.nonzero(keyed)):
        transform = TRANSFORMS[component]
        kept = _reduced(frames, values[component][:, row], TOLERANCE[transform])
        tracks.setdefault(rig.hashes[row], {})[transform] = full_track(
            kinds[transform], frames[kept].tolist(), values[component][kept, row], end if legacy else 0.0)
    return tracks, end
