"""MHW PC LMT v95/x64 animation source and MOD3 v237 reference skeleton.

Wire layouts/bit packing: PredatorCZ/RevilLib (GPL-3.0).
MHW quantization/TIML: AsteriskAmpersand/MHW-Free-HyperKinetics (GPL-3.0).
Bone function IDs in MOD3 bind LMT tracks; MOD3 array indices do not.
TIML channels remain native World data, rather than RE Engine events.
"""
from dataclasses import dataclass, field, replace
from pathlib import Path
import math
import struct

from .binary import ReadContext
from .errors import MotionParseError, MotionWriteError
from .lmt_codec import LmtDocument, decode_lmt_keys
from .mhr_codec import MHR_PROFILE
from .mot.model import Motion, Joint, Skeleton, AnimationNode, KeyTrack
from .mot_list.model import MotionSlot, MotionSlotType, EmbeddedPayload
from .evaluation.math3d import decompose_row_srt
from .mhw_timeline import MhwTimeline, read_mhw_timeline


MHW_LMT_PROFILE = replace(MHR_PROFILE, name='Monster Hunter World (MT Framework)',
                          motlist=replace(MHR_PROFILE.motlist, version=95),
                          mot=replace(MHR_PROFILE.mot, version=95))


@dataclass(slots=True)
class MhwLmtTrack:
    bone_id: int
    kind: int
    codec: int
    weight: float
    keys: KeyTrack
    joint: Joint | None


@dataclass(slots=True)
class MhwLmtMotion(Motion):
    frames_per_second: int = 60
    flags: int = 0
    timl_offset: int = 0
    timeline: MhwTimeline | None = None
    reserved_words: tuple = ()
    scene_translation: tuple = (0., 0., 0., 0.)
    scene_rotation: tuple = (0., 0., 0., 1.)
    source_tracks: list[MhwLmtTrack] = field(default_factory=list)

    @property
    def unbound_tracks(self):
        return tuple(t for t in self.source_tracks if t.joint is None)


def read_mod3_skeleton(data, *, label='MOD3'):
    c = ReadContext.from_bytes(data, label)
    c.require(0, 56, 'MOD3 header')
    if bytes(c.data[:4]) != b'MOD\0' or c.u16(4) != 237:
        raise MotionParseError(f'{label}: expected MHW MOD3 v237')
    count, base = c.u16(6), c.u64(48)
    if not count or not base:
        raise MotionParseError(f'{label}: missing MOD3 skeleton')
    c.require(base, count*(24+64+64), 'MOD3 skeleton')
    root = Joint('MHW_Root')
    joints, parents, ids = [], [], set()
    for i in range(count):
        bone, parent = struct.unpack_from('<HB', c.data, base+i*24)
        if bone in ids or (parent != 255 and parent >= count):
            raise MotionParseError(f'{label}: invalid MOD3 bone function ID/parent at {i}')
        ids.add(bone)
        matrix = struct.unpack_from('<16f', c.data, base+count*24+i*64)
        if not all(math.isfinite(v) for v in matrix):
            raise MotionParseError(f'{label}: nonfinite MOD3 reference matrix')
        pose = decompose_row_srt(matrix)
        joints.append(Joint(f'MHW_{bone}', translation=pose.translation,
                            rotation=pose.rotation, rest_scale=pose.scale))
        parents.append(parent)
    for i, joint in enumerate(joints):
        seen, parent = {i}, parents[i]
        while parent != 255:
            if parent in seen:
                raise MotionParseError(f'{label}: cyclic MOD3 skeleton')
            seen.add(parent)
            parent = parents[parent]
        joint.parent = root if parents[i] == 255 else joints[parents[i]]
        joint.parent.children.append(joint)
    return Skeleton([root, *joints])


def decode_mhw_track(c, offset, end_frame):
    c.require(offset, 48, 'MHW LMT track')
    codec, kind, bone_type, _padding, bone, weight, size, pointer, *tail = struct.unpack_from(
        '<4BifIQ4fQ', c.data, offset)
    return bone, kind, weight, decode_lmt_keys(
        c, codec, kind, weight, size, pointer, tuple(tail[:4]), tail[4], end_frame,
        bone_type=bone_type, quantization_padding=8)


def _resolve_skeleton(label, bone_ids):
    path = Path(label)
    preferred = path.stem
    candidates = sorted(path.parent.glob('*.mod3'))
    if not candidates:
        for parent in path.parents:
            directory = parent/'mod'
            if directory.is_dir():
                candidates = sorted(directory.glob('*.mod3'))
                # Extracted monster resources: em/emNNN/variant/{mot,mod}.
                preferred = f'{parent.parent.name}_{parent.name}'
                break
    matches = []
    for candidate in candidates:
        skeleton = read_mod3_skeleton(candidate.read_bytes(), label=str(candidate))
        ids = {int(j.name[4:]) for j in skeleton.joints[1:]}
        if candidate.stem == preferred or bone_ids <= ids:
            matches.append((candidate, skeleton))
    exact = [item for item in matches if item[0].stem == preferred]
    if len(exact) == 1:
        return exact[0]
    if len(matches) == 1:
        return matches[0]
    reason = 'ambiguous compatible MOD3 skeletons' if matches else 'no compatible MOD3 skeleton'
    raise MotionParseError(f'{label}: {reason}; place the matching .mod3 beside the LMT '
                           'or retain the extracted mot/mod directory layout '
                           '(Python API: skeleton_data=MOD3 bytes)')


class MhwLmtMotionFormatCodec:
    profile = MHW_LMT_PROFILE

    def matches(self, data):
        return (len(data) >= 8 and bytes(data[:4]) == b'LMT\0'
                and struct.unpack_from('<H', data, 4)[0] == 95)

    def parse(self, data, *, label='MHW LMT', skeleton_data=None):
        c = ReadContext.from_bytes(data, label)
        if not self.matches(data):
            raise MotionParseError(f'{label}: expected little-endian MHW PC LMT v95')
        count = c.u16(6)
        c.require(0, 16+count*8, 'MHW LMT header and animation pointers')
        pointers = [c.u64(16+i*8) for i in range(count)]
        headers, bone_ids = {}, set()
        for base in dict.fromkeys(pointers):
            if not base:
                continue
            c.require(base, 96, 'MHW LMT animation header')
            table, tracks, duration, loop = struct.unpack_from('<QIIi', c.data, base)
            if not duration or loop < -1 or loop >= duration or (tracks and not table):
                raise MotionParseError(f'{label}: invalid MHW LMT duration/loop/track pointer')
            c.require(table, tracks*48, 'MHW LMT track table')
            headers[base] = (table, tracks, duration, loop)
            for i in range(tracks):
                bone = c.i32(table+i*48+4)
                if bone != -1:
                    bone_ids.add(bone)
        path = ''
        skeleton = None
        if skeleton_data is not None:
            skeleton = read_mod3_skeleton(skeleton_data)
        elif headers:
            path, skeleton = _resolve_skeleton(label, bone_ids)
        by_id = ({int(j.name[4:]): j for j in skeleton.joints[1:]} if skeleton else {})
        if skeleton:
            by_id[-1] = skeleton.joints[0]
        model = LmtDocument(Path(label).stem, source=bytes(data),
                            skeleton_path=str(path), slot_count=count)
        payloads = {}
        for slot_id, base in enumerate(pointers):
            if not base:
                continue
            if base not in payloads:
                table, tracks, duration, loop = headers[base]
                motion = MhwLmtMotion(f'{model.name}_{slot_id:03}', end_frame=duration,
                    raw_end_frame=duration, looping=loop >= 0,
                    loop_start_frame=max(0, loop), skeleton=skeleton,
                    flags=c.u32(base+64), timl_offset=c.u64(base+88),
                    reserved_words=struct.unpack_from('<5I', c.data, base+68),
                    scene_translation=struct.unpack_from('<4f', c.data, base+32),
                    scene_rotation=struct.unpack_from('<4f', c.data, base+48))
                if not all(math.isfinite(v) for v in (*motion.scene_translation, *motion.scene_rotation)):
                    raise MotionParseError(f'{label}: nonfinite MHW scene transform')
                if motion.timl_offset:
                    motion.timeline = read_mhw_timeline(c, motion.timl_offset)
                nodes = {}
                for i in range(tracks):
                    bone, kind, weight, track = decode_mhw_track(c, table+i*48, duration)
                    if (kind in (3, 4)) != (bone == -1):
                        raise MotionParseError(f'{label}: unsupported MHW bone binding {bone}/{kind}')
                    joint = by_id.get(bone)
                    motion.source_tracks.append(MhwLmtTrack(bone, kind, c.u8(table+i*48), weight, track, joint))
                    if joint is None:
                        continue
                    node = nodes.setdefault(bone, AnimationNode(by_id[bone], weight=weight))
                    attr = {0: 'rotation', 1: 'translation', 2: 'scale',
                            3: 'rotation', 4: 'translation'}[kind]
                    if getattr(node, attr) is not None or node.weight != weight:
                        raise MotionParseError(f'{label}: conflicting MHW bone channels')
                    setattr(node, attr, track)
                motion.animation_nodes = list(nodes.values())
                payloads[base] = EmbeddedPayload(motion)
            model.slots.append(MotionSlot(slot_id, MotionSlotType.MOT, payloads[base]))
        return model

    def write(self, model):
        raise MotionWriteError('MHW LMT is a read-only animation source')


MHW_LMT_MOTION_FORMAT_CODEC = MhwLmtMotionFormatCodec()
