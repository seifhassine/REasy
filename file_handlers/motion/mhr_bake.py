"""Bake ordered motion ranges into one Rise slot, with optional seam processing."""
from dataclasses import dataclass, replace
import math
import struct

from .binary import align_up
from .errors import MotionWriteError
from .evaluation.mhr import MHR_EVALUATION_PROFILE as PROFILE
from .evaluation.sampling import sample_track
from .mhr_codec import MHR_MOTION_FORMAT_CODEC as CODEC
from .mhr_editing import duplicate_slot, validate_slot_order
from .mhr_structure import Layout, Splice, Owner, materialize, private_motion
from .mhr_tracks import encode_track
from .mot.model import AnimationNode, KeyTrack, TrackFamily, Skeleton
from .motion_root import root_index, root_transforms, ROOT_TRANSFORM_MODES
from .evaluation.model import Transform
from .motion_bake_timing import BAKE_FPS, sample_schedule
from .motion_bake_blend import align_waist_rotations, blend_values, seam_windows


@dataclass(frozen=True)
class MotionSegment:
    motion_id: int
    start: float
    end: float | None
    speed: float = 1.0
    root_transform: str | None = None
    root_translation_scale: float = 1.0


CHANNELS = (('translation', TrackFamily.VECTOR3),
            ('rotation', TrackFamily.QUATERNION), ('scale', TrackFamily.VECTOR3))


def _slot(document, motion_id):
    if isinstance(motion_id, bool) or not isinstance(motion_id, int) or not 0 <= motion_id <= 0xFFFF:
        raise MotionWriteError('MotionID must be an unsigned 16-bit integer')
    matches = [s for s in document.slots if s.motion_id == motion_id and s.payload]
    if len(matches) != 1:
        raise MotionWriteError(f'MotionID {motion_id} must resolve to one embedded motion')
    return matches[0]


def _rig_signature(motion):
    return [(j.binding_hash, j.parent.binding_hash if j.parent else None, j.translation, j.rotation)
            for j in motion.skeleton.joints if all(math.isfinite(v) for v in (*j.translation, *j.rotation))]


def sample_segments(motions, segments, target, *, root_transform='authored'):
    """Preserve both boundary poses on adjacent integer frames at each seam."""
    if not segments or len(motions) != len(segments):
        raise MotionWriteError('At least one source segment is required')
    samples, ranges = sample_schedule(motions, segments, root_transform=root_transform)
    maps = []
    signature = _rig_signature(target)
    for motion, segment in zip(motions, segments, strict=True):
        if segment.root_transform is not None and segment.root_transform not in ROOT_TRANSFORM_MODES:
            raise MotionWriteError(f'Unknown segment root transform mode: {segment.root_transform}')
        if _rig_signature(motion) != signature:
            raise MotionWriteError('Segments and target must use the same skeleton and rest pose')
        nodes = {n.joint.binding_hash: n for n in motion.animation_nodes}
        if len(nodes) != len(motion.animation_nodes):
            raise MotionWriteError('Source contains duplicate animation nodes for one bone')
        maps.append(nodes)
    nodes = []
    hashes = dict.fromkeys(key for mapping in maps for key in mapping)
    for key in hashes:
        authored = [mapping[key] for mapping in maps if key in mapping]
        joint, weight = authored[0].joint, authored[0].weight
        if any(n.weight != weight for n in authored):
            raise MotionWriteError(f'{joint.name}: segment node weights differ; animated weights are unsupported')
        node = AnimationNode(joint, weight)
        for attr, family in CHANNELS:
            if not any(getattr(n, attr) is not None for n in authored):
                continue
            values = []
            for index, frame in samples:
                source_node = maps[index].get(key)
                track = getattr(source_node, attr) if source_node is not None else None
                if track is None:
                    rest = (1.0, 1.0, 1.0) if attr == 'scale' else getattr(joint, attr)
                    if not all(math.isfinite(v) for v in rest):
                        raise MotionWriteError(f'{joint.name}: missing {attr} has no known rest value')
                    values.append(rest)
                else:
                    values.append(sample_track(track, frame, PROFILE.sampling_policy.rotation_interpolation))
            constant = all(value == values[0] for value in values)
            setattr(node, attr, KeyTrack(family, [0] if constant else list(range(len(samples))),
                                        values[:1] if constant else values))
        nodes.append(node)
    return nodes, ranges, len(samples) - 1


def _animation_block(nodes, origin, bone_indices):
    """Encode a new node table using the existing native track encoder."""
    if len(nodes) > 0xFFFF:
        raise MotionWriteError('Too many animation nodes for MOT 495')
    output = bytearray(len(nodes) * 12)
    for index, node in enumerate(nodes):
        channels = [(bit, attr) for bit, (attr, _) in enumerate(CHANNELS) if getattr(node, attr) is not None]
        record = len(output)
        output.extend(bytes(20 * len(channels)))
        flags = sum(1 << bit for bit, _ in channels) | (round(node.weight * 255) << 8)
        struct.pack_into('<HHII', output, index * 12, bone_indices[node.joint.binding_hash], flags,
                         node.joint.binding_hash, origin + record)
        for position, (_, attr) in enumerate(channels):
            track = getattr(node, attr)
            encoding, frames, values = encode_track(track)
            output.extend(bytes(align_up(len(output), 4) - len(output)))
            frame_offset = origin + len(output)
            output.extend(frames)
            output.extend(bytes(align_up(len(output), 4) - len(output)))
            value_offset = origin + len(output)
            output.extend(values)
            struct.pack_into('<5I', output, record + position * 20, encoding, len(track.frames),
                             frame_offset, value_offset, 0)
    output.extend(bytes(align_up(len(output), 16) - len(output)))
    return output


def _set_values(node, attr, family, values):
    constant = all(value == values[0] for value in values)
    setattr(node, attr, KeyTrack(family, [0] if constant else list(range(len(values))), values[:1] if constant else values))


def _process_seams(nodes, target, ranges, end_frame, *, align_waist, blend_frames):
    windows = seam_windows(ranges, blend_frames)
    if not align_waist and not windows:
        return
    if align_waist:
        joints = [j for j in target.skeleton.joints if j.name == 'Waist_00']
        if len(joints) != 1:
            raise MotionWriteError('Waist alignment requires one bone named Waist_00')
        waist = next((n for n in nodes if n.joint.name == 'Waist_00'), None)
        if waist is None:
            waist = AnimationNode(joints[0])
            nodes.append(waist)
        rotations = [sample_track(waist.rotation, frame) if waist.rotation else waist.joint.rotation
                     for frame in range(end_frame+1)]
        _set_values(waist, 'rotation', TrackFamily.QUATERNION, align_waist_rotations(rotations, ranges))
    for node in nodes:
        if node.joint.name.casefold() == 'root':
            continue
        for attr, family in CHANNELS:
            track = getattr(node, attr)
            if track is None or (node.joint.name == 'Waist_00' and attr != 'rotation'):
                continue
            values = [sample_track(track, frame) for frame in range(end_frame+1)]
            _set_values(node, attr, family, blend_values(values, windows, rotation=attr == 'rotation'))
    # FK changed; author the limb goals from that FK instead of blending stale IK.
    from .evaluation.source_adapter import rig_from_motion_skeleton
    from .evaluation.binding import bind_motion
    from .evaluation.sampling import MotionEvaluator
    from .mhr_import import bake_ik_goal_tracks, IK_ROTATION_GOALS, IDENTITY_ROTATION
    known = [j for j in target.skeleton.joints if all(math.isfinite(v) for v in (*j.translation, *j.rotation))]
    by_hash = {j.binding_hash: j for j in known}
    evaluation_motion = replace(target, end_frame=end_frame, skeleton=Skeleton(known),
                                animation_nodes=[replace(n, joint=by_hash[n.joint.binding_hash])
                                                 for n in nodes if n.joint.binding_hash in by_hash])
    rig = rig_from_motion_skeleton(evaluation_motion, scale=(1, 1, 1), joint_binding=PROFILE.joint_binding)
    evaluator = MotionEvaluator(bind_motion(evaluation_motion, rig, PROFILE.joint_binding),
                                PROFILE.sampling_policy, PROFILE.pose_composition_policy)
    poses = [evaluator.sample_frame(frame, wrap_looping=False) for frame in range(end_frame+1)]
    for index, (translations, rotations) in bake_ik_goal_tracks(poses, rig).items():
        joint = known[index]
        node = next((n for n in nodes if n.joint.binding_hash == joint.binding_hash), None)
        if node is None:
            node = AnimationNode(joint)
            nodes.append(node)
        _set_values(node, 'translation', TrackFamily.VECTOR3, translations)
        _set_values(node, 'rotation', TrackFamily.QUATERNION,
                    rotations if joint.name in IK_ROTATION_GOALS else [IDENTITY_ROTATION]*(end_frame+1))


def bake_segments(document, segments, motion_id, *, replace_existing=False, name=None, root_transform='authored',
                  align_waist=False, blend_frames=0):
    """Replace animation channels only; retain target CLIPs, overrides and slot metadata.

    Sources are sampled before touching the target, so it may also be a source.
    Each segment includes both endpoints; the next starts one frame after it.
    Root transforms can remain authored, accumulate relative deltas, or be identity.
    No event merging is applied.
    """
    model = materialize(document)
    if root_transform not in ROOT_TRANSFORM_MODES:
        raise MotionWriteError(f'Unknown root transform mode: {root_transform}')
    validate_slot_order(model)
    segments = list(segments)
    if not segments:
        raise MotionWriteError('At least one source segment is required')
    if name is not None and (not name or '\0' in name):
        raise MotionWriteError('Motion name must be nonempty and contain no NUL')
    motions = [_slot(model, segment.motion_id).payload.value for segment in segments]
    segments = [replace(segment, end=motion.end_frame) if segment.end is None else segment
                for segment, motion in zip(segments, motions, strict=True)]
    if replace_existing:
        target = _slot(model, motion_id).payload.value
    else:
        source_index = next(i for i, s in enumerate(model.slots) if s.motion_id == segments[0].motion_id)
        model = duplicate_slot(model, source_index, motion_id, name=name or f'{model.name}_{motion_id}')
        target = _slot(model, motion_id).payload.value
    nodes, ranges, end_frame = sample_segments(motions, segments, target, root_transform=root_transform)
    if any((segment.root_transform or root_transform) != 'authored' or segment.root_translation_scale != 1
           for segment in segments):
        joint = target.skeleton.joints[root_index(target.skeleton.joints)]
        root = next((node for node in nodes if node.joint.binding_hash == joint.binding_hash), None)
        if root is None:
            root = AnimationNode(joint)
            nodes.append(root)
        transforms = [Transform(*(sample_track(getattr(root, attr), frame)
                                  if getattr(root, attr) is not None else
                                  (1.0, 1.0, 1.0) if attr == 'scale' else getattr(joint, attr)
                                  for attr, _ in CHANNELS)) for frame in range(end_frame+1)]
        transforms = root_transforms(transforms, ranges, root_transform)
        for attr, family in CHANNELS:
            values = [getattr(transform, attr) for transform in transforms]
            constant = all(value == values[0] for value in values)
            setattr(root, attr, KeyTrack(family, [0] if constant else list(range(end_frame+1)),
                                         values[:1] if constant else values))
    _process_seams(nodes, target, ranges, end_frame, align_waist=align_waist, blend_frames=blend_frames)
    model = private_motion(model, motion_id, 'motion')
    if name is not None:
        _slot(model, motion_id).payload.value.name = name
        model = materialize(model)
    owner = Owner(model, motion_id, 'motion')
    target = owner.slot.payload.value
    raw, base = model.source, owner.base
    bone_indices = {}
    # External weapon/mesh channels retain their authored native bone indices.
    for motion_id_source in dict.fromkeys(s.motion_id for s in segments):
        source_owner = Owner(model, motion_id_source, 'motion')
        table = source_owner.base + struct.unpack_from('<Q', raw, source_owner.base + 24)[0]
        for index, node in enumerate(source_owner.slot.payload.value.animation_nodes):
            bone_indices.setdefault(node.joint.binding_hash, struct.unpack_from('<H', raw, table + index * 12)[0])
    for index, joint in enumerate(target.skeleton.joints):
        bone_indices.setdefault(joint.binding_hash, index)
    pointers = struct.unpack_from('<10Q', raw, base + 16)
    start = base + pointers[1]
    # Replace the native animation section in place, keeping its table at +0x80.
    # Retimed channel buffers outside this section may remain source-owned bytes.
    sections = [base + value for value in pointers[2:] if value > pointers[1]]
    shared = next(shared for begin, _, shared in model.motion_spans if begin == base)
    if not shared and pointers[0] > pointers[1]:
        sections.append(base + pointers[0])
    stop = min(sections)
    block = _animation_block(nodes, pointers[1], bone_indices)
    block.extend(bytes((stop - start - len(block)) % 16))
    layout = Layout(model, [Splice(start, stop, block)], motion_base=base,
                    pointer_overrides={base + 24: pointers[1]})
    output = layout.build()
    struct.pack_into('<4f', output, base + 96, end_frame, -1.0, 0.0, end_frame)
    struct.pack_into('<H', output, base + 114, len(nodes))
    struct.pack_into('<I', output, base + 120, BAKE_FPS)
    result = CODEC.parse(bytes(output), label='baked motion segments')
    if CODEC.write(result) != bytes(output):
        raise MotionWriteError('Baked MOTLIST did not roundtrip stably')
    saved = _slot(result, motion_id).payload.value
    if saved.end_frame != end_frame or saved.frames_per_second != BAKE_FPS or len(saved.animation_nodes) != len(nodes):
        raise MotionWriteError('Baked animation metadata differs after serialization')
    for want, got in zip(nodes, saved.animation_nodes, strict=True):
        if want.joint.binding_hash != got.joint.binding_hash or want.weight != got.weight:
            raise MotionWriteError('Baked bone binding differs after serialization')
        for attr, family in CHANNELS:
            expected, actual = getattr(want, attr), getattr(got, attr)
            if expected is None:
                if actual is not None:
                    raise MotionWriteError('Baking created an unexpected channel')
                continue
            if actual is None or expected.frames != actual.frames:
                raise MotionWriteError('Baked channel frames differ after serialization')
            for a, b in zip(expected.values, actual.values, strict=True):
                signs = (1, -1) if family == TrackFamily.QUATERNION else (1,)
                if not any(all(math.isclose(x, sign * y, abs_tol=2e-5, rel_tol=2e-6)
                               for x, y in zip(a, b, strict=True)) for sign in signs):
                    raise MotionWriteError('Baked channel values differ after serialization')
    return result, ranges
