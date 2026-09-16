"""Bake the preview retarget into independent native MOT 495 payloads."""
from __future__ import annotations

import math
import struct
from bisect import bisect_right

import numpy as np

from utils.hash_util import murmur3_hash_utf16le
from .binary import ReadContext, pad_to_alignment
from .errors import MotionWriteError
from .evaluation.mhr import MHR_EVALUATION_PROFILE as PROFILE
from .evaluation.wilds_retarget import WILDS_TO_RISE
from .evaluation.binding import bind_motion
from .evaluation.sampling import MotionEvaluator
from .evaluation.math3d import decompose_row_srt
from .mhr_codec import MHR_MOTION_FORMAT_CODEC as CODEC, MHR_PROFILE
from .mhr_editing import _copy_skeleton
from .mhr_tracks import encode_track
from .mot.model import KeyTrack, TrackFamily
from .mot_clip.parser_v43 import CompactClipV43Parser

# Native Rise motions drive inverse kinematics through these control joints, and
# the runtime solves each limb toward the world transform stored in them.  The
# convention (measured on native motions: position error <= 0.0035, rotation
# error <= 0.56 degrees) is that a goal's world transform equals the world
# transform of the bone it drives, expressed in the goal's own parent space.
# LookAt is a pure position target: native motions keep its rotation at a single
# identity key.  A retargeted source has no such rig, so an import must bake the
# goals from the motion's own forward kinematics or the runtime drags the limbs
# toward the origin.
IK_GOAL_SOURCES = {'L_Hand_IK': 'L_Arm_03', 'R_Hand_IK': 'R_Arm_03',
                   'L_Foot_IK': 'L_Leg_02', 'R_Foot_IK': 'R_Leg_02', 'LookAt': 'Head_00'}
IK_ROTATION_GOALS = frozenset(IK_GOAL_SOURCES) - {'LookAt'}
IDENTITY_ROTATION = (0.0, 0.0, 0.0, 1.0)


def default_motion_name(document, motion_id):
    """Return ``<motlist>_<motion id>``, the naming native payloads use."""
    for slot in document.slots:
        if slot.payload is None:
            continue
        head, _, tail = slot.payload.value.name.rpartition('_')
        if head and tail.isdigit():
            return f'{head}_{motion_id}'
    if getattr(document, 'name', ''):
        return f'{document.name}_{motion_id}'
    return None


def bake_ik_goal_tracks(poses, rig, *, goal_sources=IK_GOAL_SOURCES):
    """Return ``{rig joint index: (translations, rotations)}`` for every IK goal.

    The rig uses the engine's row-vector matrices, so the goal's local transform
    is ``target_world @ inverse(parent_world)``.
    """
    by_name = {joint.name: index for index, joint in enumerate(rig.joints)}
    worlds = [np.asarray(pose.world_matrices, dtype=np.float64).reshape(-1, 4, 4) for pose in poses]
    baked = {}
    for goal, driver in goal_sources.items():
        index, driver_index = by_name.get(goal), by_name.get(driver)
        if index is None or driver_index is None:
            continue
        parent_index = rig.joints[index].parent_index
        if parent_index is None:
            continue
        translations, rotations = [], []
        for world in worlds:
            transform = decompose_row_srt(
                (world[driver_index] @ np.linalg.inv(world[parent_index])).reshape(16))
            translations.append(tuple(float(value) for value in transform.translation))
            rotation = tuple(float(value) for value in transform.rotation)
            if rotation[3] < 0.0:
                rotation = tuple(-value for value in rotation)
            rotations.append(rotation)
        baked[index] = (translations, rotations)
    return baked


def idle_template(document):
    candidates = {id(slot.payload): index for index, slot in enumerate(document.slots)
                  if slot.payload and slot.payload.value.name.lower().endswith('_001_loop')}
    if len(candidates) != 1:
        raise MotionWriteError('The hold template must contain one native 001_Loop motion')
    return next(iter(candidates.values()))


def _template_offsets(document, index):
    pointers, rows = struct.unpack_from('<QQ', document.source, 16)
    return struct.unpack_from('<Q', document.source, pointers+index*8)[0], rows+index*72


def _hold_sequence(document, index, origin, end_frame):
    """Copy only the native WeaponHold node, preserving its wire metadata.

    The supported template is the flat, constant hold track from 001_Loop.
    Other idle events, IK controls and slot overrides are not import content.
    """
    raw = document.source
    base, _ = _template_offsets(document, index)
    motion = document.slots[index].payload.value
    table = base+struct.unpack_from('<Q', raw, base+48)[0]
    matches = [(i, seq, node) for i, seq in enumerate(motion.sequences)
               for node in seq.clip.root.children if node.name.rsplit('.', 1)[-1] == 'WeaponHold']
    if len(matches) != 1:
        raise MotionWriteError('001_Loop must contain one native WeaponHold node')
    sequence_index, sequence, node = matches[0]
    wrapper = base+struct.unpack_from('<Q', raw, table+sequence_index*8)[0]
    name, clip, tracks = struct.unpack_from('<3Q', raw, wrapper)
    if name or node.children or any(p.children or p.last_key or p.speed_points for p in node.properties):
        raise MotionWriteError('Unsupported structure in the native idle WeaponHold template')
    if motion.end_frame <= 0:
        raise MotionWriteError('WeaponHold template has no positive duration')
    if not {'_leftWp', '_rightWp'}.issubset(p.name for p in node.properties):
        raise MotionWriteError('WeaponHold template is missing a hand state')
    for prop in node.properties:
        if not prop.keys or any(k.curve or k.value != prop.keys[0].value for k in prop.keys):
            raise MotionWriteError('001_Loop WeaponHold must describe a constant hold state')
    parsed = CompactClipV43Parser(MHR_PROFILE).parse_result(
        ReadContext.from_bytes(raw), base+clip, base+tracks, pointer_base=base)
    hold = next(r for r in parsed.nodes if r.node.name == node.name)
    if len(sequence.tracks) != len(parsed.nodes)-1 or any(r.owner.name == node.name for r in parsed.clip.extra_ranges):
        raise MotionWriteError('Unsupported WeaponHold track metadata or ranges')
    props = parsed.properties[hold.property_index:hold.property_index+hold.property_count]
    sections = parsed.section_absolute_offsets
    out = bytearray(raw[wrapper:wrapper+64]+raw[base+clip:base+clip+112])
    offsets = {}
    offsets['nodes'] = len(out)
    root_record = bytearray(raw[parsed.nodes[0].offset:parsed.nodes[0].offset+40])
    struct.pack_into('<HH', root_record, 0, 1, 0)
    struct.pack_into('<QQ', root_record, 24, 1, 0)
    hold_record = bytearray(raw[hold.offset:hold.offset+40])
    struct.pack_into('<QQ', hold_record, 24, 0, 0)
    out.extend(root_record+hold_record)
    offsets['properties'] = len(out)
    key_count = 0
    ratio = end_frame/motion.end_frame
    for record in props:
        data = bytearray(raw[record.offset:record.offset+72])
        struct.pack_into('<ff', data, 0, record.prop.start_frame*ratio, record.prop.end_frame*ratio)
        struct.pack_into('<Q', data, 32, key_count)
        out.extend(data)
        key_count += len(record.prop.keys)
    offsets['keys'] = len(out)
    key_records = {id(record.key): record for record in parsed.keys}
    for record in props:
        for key in record.prop.keys:
            offset = key_records[id(key)].offset
            data = bytearray(raw[offset:offset+32])
            struct.pack_into('<f', data, 0, key.frame*ratio)
            out.extend(data)
    for section in ('speed_points', 'hermite_curves', 'bezier3d_curves', 'last_keys'):
        offsets[section] = len(out)
    for section, following in (('ascii_strings', 'unicode_strings'), ('unicode_strings', 'owords'), ('owords', 'extra_ranges')):
        offsets[section] = len(out)
        out.extend(raw[sections[section]:sections[following]])
    offsets['extra_ranges'] = len(out)
    out.extend(struct.pack('<IIQ', 0, 0, origin+len(out)+16))
    pad_to_alignment(out, 16)
    new_tracks = len(out)
    metadata = base+tracks+(hold.index-1)*28
    out.extend(raw[metadata:metadata+28])
    pad_to_alignment(out, 16)
    struct.pack_into('<3Q', out, 0, 0, origin+64, origin+new_tracks)
    struct.pack_into('<I', out, 28, 1)
    struct.pack_into('<fIII', out, 64+8, end_frame, 2, len(props), key_count)
    for i, section in enumerate(parsed.section_relative_offsets):
        struct.pack_into('<Q', out, 64+24+i*8, origin+offsets[section])
    return out


def bake_motion(source_motion, rig, hold_document, *, name=None):
    """Return one MOT 495 payload that follows the native v528 layout.

    Native payloads keep the animation block at ``+0x80``, the sequences after
    it, the name last, and ``pointers[0] >= size`` so a payload shares the file's
    rig instead of embedding a copy: files embed no skeleton at all, their single
    anchor being the 001_Loop template payload (``size == 0``).  Everything is
    position independent because the internal pointers are payload relative.
    """
    end = source_motion.end_frame
    if not math.isfinite(end) or end < 0 or math.ceil(end) > 0xFFFFFFFF:
        raise MotionWriteError('Source duration is outside the native key-frame range')
    if source_motion.frames_per_second <= 0:
        raise MotionWriteError('Source frame rate must be positive')
    count = len(rig.joints)
    if not 0 < count <= 0xFFFF:
        raise MotionWriteError('Target rig exceeds the MOT 495 joint limit')
    template_index = idle_template(hold_document)
    template_base, _ = _template_offsets(hold_document, template_index)
    binding = WILDS_TO_RISE.bind(source_motion, rig)
    evaluator = WILDS_TO_RISE.evaluator(binding, PROFILE.sampling_policy, PROFILE.pose_composition_policy, PROFILE.joint_binding)
    frames = list(range(math.ceil(end)+1))
    poses = [evaluator.sample_frame(frame, wrap_looping=False) for frame in frames]
    goals = bake_ik_goal_tracks(poses, rig)
    name = source_motion.name if name is None else name
    if not name or '\0' in name:
        raise MotionWriteError('Motion name must be nonempty and contain no NUL')
    out = bytearray(hold_document.source[template_base:template_base+128])
    struct.pack_into('<10Q', out, 16, *([0]*10))
    node_table = len(out)
    channels = [3 if index in goals else 7 for index in range(count)]
    out.extend(bytes(count*12))
    out.extend(bytes(sum(value*20 for value in channels)))
    record_offset = node_table + count*12
    for index, joint in enumerate(rig.joints):
        struct.pack_into('<HHII', out, node_table+index*12, index, (0xFF << 8) | channels[index],
                         murmur3_hash_utf16le(joint.name), record_offset)
        for channel, (attribute, family) in enumerate((('translation', TrackFamily.VECTOR3),
                ('rotation', TrackFamily.QUATERNION), ('scale', TrackFamily.VECTOR3))):
            if not channels[index] & (1 << channel):
                continue
            if index in goals:
                translations, rotations = goals[index]
                values = (translations if channel == 0
                          else rotations if joint.name in IK_ROTATION_GOALS
                          else [IDENTITY_ROTATION]*len(frames))
            else:
                values = [getattr(pose.local_transforms[index], attribute) for pose in poses]
            array = np.asarray(values, dtype=np.float32)
            constant = np.all(array == array[0])
            track = KeyTrack(family, [0] if constant else frames,
                             [tuple(value) for value in (array[:1] if constant else array)])
            flags, frame_data, value_data = encode_track(track)
            pad_to_alignment(out, 4)
            frame_offset = len(out)
            out.extend(frame_data)
            pad_to_alignment(out, 4)
            value_offset = len(out)
            out.extend(value_data)
            struct.pack_into('<5I', out, record_offset, flags, len(track.frames), frame_offset, value_offset, 0)
            record_offset += 20
        pad_to_alignment(out, 16)
    sequence_table = len(out)
    out.extend(bytes(16))
    struct.pack_into('<Q', out, sequence_table, len(out))
    out.extend(_hold_sequence(hold_document, template_index, len(out), end))
    name_offset = len(out)
    out.extend(name.encode('utf-16le')+b'\0\0')
    pad_to_alignment(out, 16)
    size = len(out)
    struct.pack_into('<I', out, 12, size)
    for field, value in ((16, size), (24, node_table), (48, sequence_table), (88, name_offset)):
        struct.pack_into('<Q', out, field, value)
    struct.pack_into('<4fHHBB', out, 96, end, 0.0 if source_motion.looping else -1.0,
                     source_motion.raw_start_frame, source_motion.raw_end_frame, count, count, 1, 0)
    struct.pack_into('<I', out, 120, source_motion.frames_per_second)
    return bytes(out)


def import_motion(document, source_motion, rig, hold_document, motion_id, *, replace_existing=False, name=None):
    """Append or replace one slot, detaching aliases and preserving other slots.

    Run :func:`verify_native_contract` on the result before shipping a file: the
    engine-side rules it checks (slot +0x0C, native layout, IK goal convention)
    are invisible to the preview model.
    """
    if isinstance(motion_id, bool) or not isinstance(motion_id, int) or not 0 <= motion_id <= 0xFFFF:
        raise MotionWriteError('Target MotionID must be an unsigned 16-bit integer')
    model = CODEC.parse(CODEC.write(document), label='before motion import')
    matches = [i for i, slot in enumerate(model.slots) if slot.motion_id == motion_id]
    if len(matches) > 1:
        raise MotionWriteError(f'Target MotionID {motion_id} is ambiguous')
    if bool(matches) != replace_existing:
        raise MotionWriteError(f'Target MotionID {motion_id} '+('already exists; request replacement explicitly' if matches else 'does not exist for replacement'))
    raw = model.source
    pointers, rows = struct.unpack_from('<QQ', raw, 16)
    count = len(model.slots)
    addresses = [struct.unpack_from('<Q', raw, pointers+i*8)[0] for i in range(count)]
    template_index = idle_template(hold_document)
    _, template_row = _template_offsets(hold_document, template_index)
    row = bytearray(hold_document.source[template_row:template_row+72])
    struct.pack_into('<QH', row, 0, 0, motion_id)
    # The template row carries private per-motion data (the 001_Loop value at
    # +0x0C belongs to that motion alone: native files store 0x17/0x1A/0x1B for
    # a handful of motions and 0 for the rest, never the same value twice).
    # Inheriting it makes the engine reject the new slot and the pose collapses
    # to a T-pose, so a new motion starts from the neutral value.
    struct.pack_into('<I', row, 12, 0)
    row[23] = 0
    payload = bake_motion(source_motion, rig, hold_document,
                          name=default_motion_name(model, motion_id) if name is None else name)
    insertions = []
    recovered = None
    if matches:
        removed = addresses[matches[0]]
        spans = model.motion_spans
        if removed and addresses.count(removed) == 1:
            position = next(i for i, span in enumerate(spans) if span[0] == removed)
            if not spans[position][2] and position+1 < len(spans) and spans[position+1][2]:
                start, end, _ = spans[position+1]
                skeleton, joints = _copy_skeleton(raw, removed, end-start)
                insertions.append((end, skeleton, 'skeleton'))
                recovered = start, end, len(skeleton), joints
    else:
        insertions.extend(((pointers+count*8, bytes(16), 'pointer'), (rows+count*72, row+bytes(8), 'slot')))
    insertions.append((rows, payload, 'motion'))
    insertions.sort(key=lambda item: item[0])
    output, positions, shifts, blocks = bytearray(), [], [], {}
    cursor = shift = 0
    for offset, data, label in insertions:
        output.extend(raw[cursor:offset])
        blocks[label] = len(output)
        output.extend(data)
        cursor = offset
        shift += len(data)
        positions.append(offset)
        shifts.append(shift)
    output.extend(raw[cursor:])
    def relocated(offset):
        index = bisect_right(positions, offset)
        return offset+(shifts[index-1] if index else 0)
    markers = {start+16 for start, _, shared in model.motion_spans if shared}
    for pointer in model.relocations.values():
        if pointer.offset in markers:
            continue
        target = relocated(pointer.target)
        if pointer.target == rows and any(start <= pointer.offset < stop for start, stop, _ in model.motion_spans):
            target = blocks['motion']
        delta = target-relocated(pointer.base)
        if delta % pointer.unit:
            raise MotionWriteError('Motion import violates native pointer alignment')
        struct.pack_into('<'+pointer.format, output, relocated(pointer.offset), delta//pointer.unit)
    if recovered:
        start, end, size, joints = recovered
        struct.pack_into('<IQ', output, relocated(start)+12, end-start+size, end-start)
        struct.pack_into('<H', output, relocated(start)+112, joints)
    if matches:
        index = matches[0]
        struct.pack_into('<Q', output, relocated(pointers+index*8), blocks['motion'])
        output[relocated(rows+index*72):relocated(rows+index*72)+72] = row
    else:
        struct.pack_into('<I', output, 48, count+1)
        struct.pack_into('<Q', output, blocks['pointer'], blocks['motion'])
    return CODEC.parse(bytes(output), label='imported MOTLIST 528')


def verify_imported_motion(source_motion, imported_motion, rig):
    """Check every baked deform frame against the existing preview evaluator.

    The IK goal joints are control bones: an import deliberately replaces their
    authored values with the native goal convention, so they are excluded here
    and covered by :func:`verify_native_contract` instead.
    """
    if (source_motion.end_frame, source_motion.looping, source_motion.frames_per_second) != (
            imported_motion.end_frame, imported_motion.looping, imported_motion.frames_per_second):
        raise MotionWriteError('Imported motion timing differs from the source')
    source = WILDS_TO_RISE.evaluator(WILDS_TO_RISE.bind(source_motion, rig), PROFILE.sampling_policy,
                                    PROFILE.pose_composition_policy, PROFILE.joint_binding)
    target = MotionEvaluator(bind_motion(imported_motion, rig, PROFILE.joint_binding),
                             PROFILE.sampling_policy, PROFILE.pose_composition_policy)
    deform = np.asarray([joint.name not in IK_GOAL_SOURCES for joint in rig.joints], dtype=bool)
    max_error = 0.0
    for frame in range(math.ceil(source_motion.end_frame)+1):
        expected = np.asarray(source.sample_frame(frame, wrap_looping=False).world_matrices).reshape(-1, 4, 4)[deform]
        actual = np.asarray(target.sample_frame(frame, wrap_looping=False).world_matrices).reshape(-1, 4, 4)[deform]
        error = float(np.max(np.abs(expected-actual)))
        max_error = max(max_error, error)
        if not np.allclose(actual, expected, atol=2e-5, rtol=2e-6):
            raise MotionWriteError(f'Imported pose differs at frame {frame}: matrix error {error:g}')
    return max_error


def verify_native_contract(document, motion_id, imported_motion, rig, *, goals=IK_GOAL_SOURCES):
    """Assert the engine-side conventions the preview model cannot see.

    Every rule below was measured on native files (174 motlists, 4751 payloads)
    or confirmed in game; the preview evaluator checks none of them, which is why
    an import can look perfect in REasy and still collapse to a T-pose.
    """
    pointers, rows = struct.unpack_from('<QQ', document.source, 16)
    slots = [index for index, slot in enumerate(document.slots) if slot.motion_id == motion_id]
    if len(slots) != 1:
        raise MotionWriteError(f'MotionID {motion_id} does not resolve to exactly one slot')
    index = slots[0]
    base = struct.unpack_from('<Q', document.source, pointers+index*8)[0]
    ptr = struct.unpack_from('<10Q', document.source, base+16)
    size = struct.unpack_from('<I', document.source, base+12)[0]
    if not size or size % 16 or base+size > len(document.source):
        raise MotionWriteError(f'MotionID {motion_id}: payload size {size} is not a native aligned size')
    if ptr[1] != 0x80:
        raise MotionWriteError(f'MotionID {motion_id}: animation nodes must start at +0x80, not 0x{ptr[1]:X}')
    if ptr[0] < size:
        raise MotionWriteError(
            f'MotionID {motion_id}: payload embeds a skeleton copy; native payloads share the file rig')
    if ptr[9] < size*9//10:
        raise MotionWriteError(f'MotionID {motion_id}: name block must sit at the end of the payload')
    if any(ptr[field] for field in (2, 3, 5, 6, 7)):
        raise MotionWriteError(f'MotionID {motion_id}: reserved payload sections must stay empty')
    private_field = struct.unpack_from('<I', document.source, rows+index*72+12)[0]
    if private_field:
        raise MotionWriteError(
            f'MotionID {motion_id}: slot +0x0C is 0x{private_field:X}; a new motion must not inherit '
            'another motion value or the engine rejects the slot (T-pose)')
    if imported_motion.name != f'{document.name}_{motion_id}':
        raise MotionWriteError(
            f'MotionID {motion_id}: name {imported_motion.name!r} must follow the native '
            f'<motlist>_<id> convention ({document.name}_{motion_id!r})')
    return verify_ik_goal_contract(imported_motion, rig, goals=goals)


def verify_ik_goal_contract(imported_motion, rig, *, goals=IK_GOAL_SOURCES):
    """Check that every IK goal follows the bone it drives, from the motion's own FK.

    The runtime solves each limb toward the goal's stored world transform, so a
    goal that sits at rest drags the limb toward the origin (curled arms).
    """
    by_name = {joint.name: index for index, joint in enumerate(rig.joints)}
    evaluator = MotionEvaluator(bind_motion(imported_motion, rig, PROFILE.joint_binding),
                               PROFILE.sampling_policy, PROFILE.pose_composition_policy)
    end = math.ceil(imported_motion.end_frame)
    frames = sorted({0, end//4, end//2, 3*end//4, max(0, end-1), end})
    worst = 0.0
    for goal, driver in goals.items():
        index, driver_index = by_name.get(goal), by_name.get(driver)
        if index is None or driver_index is None:
            continue
        parent_index = rig.joints[index].parent_index
        author = next((node for node in imported_motion.animation_nodes
                       if node.joint.name == goal), None)
        if author is None or author.translation is None:
            raise MotionWriteError(f'Imported motion has no IK goal channel for {goal}')
        if author.rotation is None:
            raise MotionWriteError(f'Imported motion has no IK goal rotation channel for {goal}')
        if goal not in IK_ROTATION_GOALS and (author.rotation.frames != [0]
                                             or max(abs(a-b) for a, b in
                                                    zip(author.rotation.values[0], IDENTITY_ROTATION)) > 1e-5):
            raise MotionWriteError(f'{goal} must stay a pure position target (single identity rotation key)')
        for frame in frames:
            pose = evaluator.sample_frame(frame, wrap_looping=False)
            world = np.asarray(pose.world_matrices, dtype=np.float64).reshape(-1, 4, 4)
            if goal in IK_ROTATION_GOALS:
                error, tolerance = float(np.max(np.abs(world[index] - world[driver_index]))), 1e-4
            else:
                # LookAt is a position target: its own rotation stays neutral.
                error, tolerance = float(np.max(np.abs(world[index][3, :3] - world[driver_index][3, :3]))), 4e-3
            if error > tolerance:
                raise MotionWriteError(
                    f'{goal} does not follow {driver} at frame {frame}: matrix error {error:g}')
            worst = max(worst, error)
    return worst
