"""See docs/internals/motion-editor.md, Blender."""

from __future__ import annotations

import math
from pathlib import Path

lazy import numpy as np

lazy from file_handlers.mesh.blender import write_package
lazy from file_handlers.mesh.blender.export import bone_lengths
lazy from file_handlers.mesh.blender.package import AXES, BLEND_FORMAT, load_package, orthonormal, save_package
lazy from file_handlers.mesh.blender.runner import run_script, temporary_directory
lazy from utils.hash_util import murmur3_hash_utf16le
lazy from utils.resource_file_utils import resource_version_from_path

from .keys import full_track
from .layers import posed as layer_posed
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


def _bone_rig(names, parents, frames: np.ndarray) -> Rig:
    above = np.array([frames[parent] if parent >= 0 else np.eye(4) for parent in parents])
    return Rig.of([murmur3_hash_utf16le(name) for name in names], parents,
                  *_decompose(np.linalg.inv(above) @ frames), names)


def _space(armature: dict) -> np.ndarray:
    # An object turning its Y axis up holds a Y-up skeleton, as FBX imports leave one; any other only places it.
    return np.eye(4) if np.isclose(np.reshape(armature["turn"], (4, 4))[2, 1], 1) else _axes().T


def _own(armature: dict, rig: Rig, pairs) -> tuple[Rig, np.ndarray] | None:
    if 2 * sum(bone == joint for bone, joint in pairs) <= len(pairs):
        return None
    _stored, built, made = _rest(armature)
    # Only REasy's first format turned each bone's own axes too.
    turns = np.where((made & (armature["blend_format"] == 1))[:, None, None], _axes().T[:3, :3], np.eye(3))
    named, sizes = dict(pairs), dict(zip(rig.names, _rest_scales(rig)))
    names = [named.get(name, name) for name in armature["names"]]
    joints = _space(armature) @ built
    joints[:, :3, :3] @= turns.swapaxes(1, 2) * [[sizes.get(name, (1, 1, 1))] for name in names]
    return _bone_rig(names, _bone_parents(armature), joints), turns


def _onto(armature: dict, rig: Rig, pairs) -> tuple[Rig, Rig, Retarget]:
    names, parents = armature["names"], _bone_parents(armature)
    _stored, built, made = _rest(armature)
    bones = _bone_rig(names, parents, _space(armature) @ built)
    pairs = [(name, name) for name in names] if pairs is None else pairs
    if own := _own(armature, rig, pairs):
        return own[0], bones, Retarget(np.arange(len(names)), own[1], bones.parents, 1.0)
    engine = {joint: made[names.index(bone)] for bone, joint in pairs}
    rest = rig.models(rig.base_locals).astype(np.float64)
    rest[:, :3, :3] /= _rest_scales(rig)[:, None]
    return rig, bones, Retarget.of(bones, rig.names, rig.parents, rest, [(joint, bone) for bone, joint in pairs],
                                   np.array([engine.get(name, False) for name in rig.names]))


def action_package(rig: Rig, motions: list[Motion], title: str, add: bool = False, armature: dict | None = None,
                   pairs=None) -> tuple[dict, dict]:
    bones, plan = rig, None
    if armature is None:
        world = _axes() @ rig.models(rig.base_locals).astype(np.float64)
        arrays = {"bones.matrix": world.reshape(-1, 16),
                  "bones.length": bone_lengths(rig.parents.tolist(), world[:, :3, 3])}
    else:
        rig, bones, plan = _onto(armature, rig, pairs)
        arrays = {"bones.matrix": _rest(armature)[1].reshape(-1, 16)}
    t0, q0, s0 = (array.astype(np.float64) for array in bones.base)
    sizes, above = _rest_scales(rig), np.vstack([_rest_scales(bones), np.ones(3)])[bones.parents]
    actions = []
    for number, motion in enumerate(motions):
        sampler = MotionSampler(motion)
        binding = rig.bind(sampler.hashes)
        frames = np.arange(int(math.floor(max(motion.end_frame, 0.0))) + 1)
        samples = [sampler.sample(float(frame)) for frame in frames]
        keyed = np.zeros((len(rig.hashes), 3), bool)
        for _values, flags in samples:
            keyed[binding[0]] |= np.stack(flags, 1)[binding[1]]
        values = [np.array(part, np.float64)
                  for part in zip(*(layer_posed(rig, sample, binding, add) for sample in samples))]
        if plan is not None:
            keyed = keyed[plan.bones] & (plan.bones >= 0)[:, None]
            frames_of = _world(rig, values)
            frames_of[..., :3, :3] /= sizes[:, None]
            values = _solved(bones, plan, frames_of, keyed)
            flips = np.cumsum(np.sum(values[1] * np.concatenate([q0[None], values[1][:-1]]), -1) < 0, axis=0) % 2
            values[1] *= np.where(flips, -1.0, 1.0)[..., None]
        spec = {"name": motion.name, "fps": motion.fps or 60}
        for component, name in enumerate(TRANSFORMS):
            joints = np.flatnonzero(keyed[:, component])
            part = values[component][:, joints]
            if name == "translation":
                part = _rotate(_inverse(q0[joints]), (part - t0[joints]) * above[joints])
            elif name == "rotation":
                local = _product(_inverse(q0[joints]), part)
                part = np.concatenate([local[..., 3:], local[..., :3]], -1)
            else:
                part = part / s0[joints]
            spec[name] = joints.tolist()
            arrays[f"a{number}.{name}"] = part.astype(np.float32)
        arrays[f"a{number}.frames"] = frames.astype(np.float32)
        actions.append(spec)
    manifest = {"title": title, "armature": armature and armature["name"],
                "skeleton": {"names": list(bones.names), "parents": bones.parents.tolist(),
                             "palette": list(range(len(bones.hashes)))},
                "channels": CHANNELS, "actions": actions, "models": [], "follows": [], "hangs": []}
    return manifest, arrays


def _model_armature(number: int, manifest: dict, arrays: dict) -> dict:
    names, parents = manifest["skeleton"]["names"], manifest["skeleton"]["parents"]
    rest = [orthonormal(matrix).ravel().tolist() for matrix in arrays["bones.matrix"].reshape(-1, 4, 4)]
    return {"name": number, "names": names, "turn": np.eye(4), "rest": rest, "built": rest,
            "parents": [names[parent] if 0 <= parent != joint else "" for joint, parent in enumerate(parents)],
            "blend_format": BLEND_FORMAT}


def _relations(rigs, main: int, models: list[tuple[int, list[str]]]) -> tuple[list, list]:
    bones, leaders = [], {}
    for number, (at, names) in enumerate(models):
        index = {name: bone for bone, name in enumerate(names)}
        bones.append(np.array([index.get(name, -1) for name in rigs[at].rig.names]))
        leaders.setdefault(at, (number, bones[-1]))
    leaders.setdefault(main, (len(models), np.arange(len(rigs[main].rig.hashes))))
    follows, hangs = [], []
    for number, (at, _names) in enumerate(models):
        entry = rigs[at]
        if entry.owner in leaders:
            leader, theirs = leaders[entry.owner]
            tied = np.column_stack([bones[number][entry.follows[0]], theirs[entry.follows[1]]])
            follows += [[number, bone, leader, joint] for bone, joint in tied[(tied >= 0).all(1)].tolist()]
        if entry.hung is not None and entry.hung[0] in leaders:
            owner, row, _inverse, _world, placed = entry.hung
            leader, theirs = leaders[owner]
            # A transform scaled absolutely (held whole, not as a matrix) keeps its own scale under the joint's.
            scaled = isinstance(placed, np.ndarray)
            matrix = np.diag([*_rest_scales(rigs[owner].rig)[row], 1.0]) @ (
                placed if scaled else placed.local_matrix) @ _axes().T
            if theirs[row] >= 0:
                hangs.append([number, leader, int(theirs[row]), matrix.ravel().tolist(), scaled])
    return follows, hangs


def export_blend(blender, rig: Rig, motions: list[Motion], output, title: str, add: bool = False, scene="",
                 armature: dict | None = None, pairs=None, rigs=(), meshes=()) -> None:
    with temporary_directory() as folder:
        models = []
        for number, (at, handler) in enumerate(meshes):
            name = Path(handler.filepath).name
            try:
                if (mesh := handler.full_mesh()) is None:
                    raise ValueError("the file could not be read")
                models.append((at, *write_package(mesh, Path(folder, f"model{number}"), title=name.split(".")[0],
                                                  file_version=resource_version_from_path(name, "mesh"))))
            except ValueError as error:
                raise ValueError(f"{name}: {error}") from error
        main = next((at for at, entry in enumerate(rigs) if entry.rig is rig), None)
        target = next((number for number, model in enumerate(models) if model[0] == main), None)
        if target is not None:
            armature = _model_armature(target, *models[target][1:])
        manifest, arrays = action_package(rig, motions, title, add, armature, pairs)
        if models:
            manifest["models"] = [f"model{number}" for number in range(len(models))]
            manifest["follows"], manifest["hangs"] = _relations(
                rigs, main, [(at, model["skeleton"]["names"]) for at, model, _arrays in models])
        save_package(folder, manifest, arrays)
        run_script(blender, "motion_export", folder, Path(output), scene)


def read_actions(blender, path) -> tuple[dict, dict]:
    with temporary_directory() as folder:
        run_script(blender, "motion_dump", Path(path), folder)
        return load_package(folder)


def read_armatures(blender, path) -> list[dict]:
    with temporary_directory() as folder:
        run_script(blender, "armatures", Path(path), folder)
        return load_package(folder)[0]["armatures"]


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


def _world(rig: Rig, values) -> np.ndarray:
    models = _compose(*values)
    for level in rig.levels[1:]:
        models[..., level, :, :] = models[..., rig.parents[level], :, :] @ models[..., level, :, :]
    return models


def _rest_scales(rig: Rig) -> np.ndarray:
    rest = _world(rig, [part.astype(np.float64) for part in rig.base])
    # float32, as joints store them: a skeleton resting at 1 keeps exactly 1.
    return np.linalg.norm(rest[:, :3, :3], axis=1).astype(np.float32)


def _solved(rig: Rig, plan: Retarget, frames_of: np.ndarray, keyed: np.ndarray) -> list[np.ndarray]:
    turns = np.tile(np.eye(4), (len(rig.hashes), 1, 1))
    turns[:, :3, :3] = plan.turns * _rest_scales(rig)[:, None]
    values = [np.broadcast_to(base.astype(np.float64), (len(frames_of), *base.shape)).copy() for base in rig.base]
    world = np.empty((len(frames_of), len(rig.hashes), 4, 4))
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
    return values


def action_bones(armature: dict) -> list:
    """The armature's bones at rest, in engine space."""
    return bones_of(armature["names"], _bone_parents(armature), _space(armature) @ _rest(armature)[1])


def action_tracks(manifest: dict, arrays: dict, number: int, rig: Rig, motion: Motion,
                  pairs=None, add: bool = False) -> tuple[dict, float]:
    action = manifest["actions"][number]
    armature = manifest["armatures"][action["armature"]]
    names, parents, space = armature["names"], _bone_parents(armature), _space(armature)
    stored, built, made = _rest(armature)
    posed = arrays[f"a{number}.matrix"].astype(np.float64).reshape(-1, len(names), 4, 4)
    frames_of = space @ posed @ np.linalg.inv(stored) @ built
    pairs = [(name, name) for name in names if name in rig.names] if pairs is None else pairs
    parts = {bone: action["bones"].get(bone, ()) for bone, _joint in pairs}
    if own := _own(armature, rig, pairs):
        rig, plan = own[0], Retarget(np.arange(len(names)), own[1].swapaxes(1, 2), own[0].parents, 1.0)
    else:
        plan = Retarget.of(rig, names, parents, space @ built, pairs, made)
    keyed = np.zeros((len(rig.hashes), 3), bool)
    for row in np.flatnonzero(plan.bones >= 0):
        components = parts.get(names[plan.bones[row]], ())
        keyed[row] = ("location" in components, any(component.startswith("rotation") for component in components),
                      "scale" in components)
    values = _solved(rig, plan, frames_of, keyed)
    if add:
        base = [part.astype(np.float64) for part in rig.base]
        values = [values[0] - base[0], _product(_inverse(base[1]), values[1]), values[2] - base[2] + 1.0]
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
