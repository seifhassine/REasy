"""MT Framework XX LMT v67 (x86 pointers), with a bundled MOD v230 rig.

Wire layouts/codeword conventions: RevilLib src/mtf_lmt (GPL-3.0).
LMT reference keys occupy frame zero; buffer keys start at frame one.
"""
from dataclasses import dataclass, replace
from pathlib import Path
import math
import re
import struct

from utils.app_paths import resource_path

from .binary import ReadContext
from .errors import MotionParseError, MotionWriteError
from .mhr_codec import MHR_PROFILE
from .mot.model import Motion, Joint, Skeleton, AnimationNode, KeyTrack, TrackFamily
from .mot_list.model import MotList, MotionSlot, MotionSlotType, EmbeddedPayload
from .evaluation.math3d import decompose_row_srt


LMT_PROFILE = replace(MHR_PROFILE, name='Monster Hunter XX (MT Framework)',
                      motlist=replace(MHR_PROFILE.motlist, version=67),
                      mot=replace(MHR_PROFILE.mot, version=67))
# MT Framework numbering, NOT the RE Engine wpXX order.
# https://github.com/GReinoso96/XXModding/wiki/Weapons
XX_WEAPONS = {0: 'greatsword', 1: 'shortsword', 2: 'hammer', 3: 'lance',
              4: 'heavybowgun', 6: 'lightbowgun', 7: 'longsword', 8: 'slashaxe',
              9: 'gunlance', 10: 'bow', 11: 'dualblades', 12: 'horn',
              13: 'insectglaive', 14: 'chargeaxe'}
XX_HUNTER_MOD = 'resources/data/motion/xx_hunter.mod'


def xx_weapon_family(name):
    family = name.lower().removesuffix('_hunterart')
    if family in XX_WEAPONS.values():
        return family
    match = re.fullmatch(r'w(\d{2})(?:_sa)?', name, re.IGNORECASE)
    return XX_WEAPONS.get(int(match[1])) if match else None


@dataclass(slots=True)
class LmtMotion(Motion):
    frames_per_second: int = 60
    events: tuple = ()


@dataclass(slots=True)
class LmtDocument(MotList):
    source: bytes = b''
    skeleton_path: str = ''
    slot_count: int = 0


def read_mod_skeleton(data, *, label='MOD'):
    c = ReadContext.from_bytes(data, label)
    c.require(0, 44, 'MOD header')
    if bytes(c.data[:4]) != b'MOD\0' or c.u16(4) != 230:
        raise MotionParseError(f'{label}: expected XX MOD v230')
    count, base = c.u16(6), c.u32(40)
    c.require(base, count*(24+64+64), 'MOD skeleton')
    if not count or not base:
        raise MotionParseError(f'{label}: missing skeleton')
    root = Joint('XX_Root')
    joints, parents, ids = [], [], set()
    for i in range(count):
        bone, parent = struct.unpack_from('<BB', c.data, base+i*24)
        if bone == 255 or bone in ids or (parent != 255 and parent >= count):
            raise MotionParseError(f'{label}: invalid MOD bone ID/parent at {i}')
        ids.add(bone)
        matrix = struct.unpack_from('<16f', c.data, base+count*24+i*64)
        if not all(math.isfinite(v) for v in matrix):
            raise MotionParseError(f'{label}: nonfinite MOD reference matrix')
        pose = decompose_row_srt(matrix)
        joints.append(Joint(f'XX_{bone}', translation=pose.translation, rotation=pose.rotation))
        parents.append(parent)
    for i, joint in enumerate(joints):
        seen, parent = {i}, parents[i]
        while parent != 255:
            if parent in seen:
                raise MotionParseError(f'{label}: cyclic MOD skeleton')
            seen.add(parent)
            parent = parents[parent]
        joint.parent = root if parents[i] == 255 else joints[parents[i]]
        joint.parent.children.append(joint)
    return Skeleton([root, *joints])


def decode_track(c, offset, end_frame):
    c.require(offset, 36, 'LMT track')
    codec, kind, bone_type, bone, weight, size, pointer, *tail = struct.unpack_from('<4BfII4fI', c.data, offset)
    reference, extremes = tuple(tail[:4]), tail[4]
    strides = {1: 12, 2: 12, 4: 8, 5: 4, 6: 8, 7: 4, 11: 4, 12: 4}
    if codec not in strides or kind not in (0, 1, 2, 4) or bone_type != 0:
        raise MotionParseError(f'{c.label}: unsupported LMT track {codec=}, {kind=}, {bone_type=}')
    stride = strides[codec]
    if not math.isfinite(weight) or not 0 <= weight <= 1 or size % stride or (size and not pointer):
        raise MotionParseError(f'{c.label}: invalid LMT track buffer/weight')
    c.require(pointer, size, 'LMT keys')
    bounds = None
    if codec in (4, 5, 7, 11, 12) and size:
        if not extremes:
            raise MotionParseError(f'{c.label}: missing LMT quantization bounds')
        c.require(extremes, 32, 'LMT quantization bounds')
        bounds = struct.unpack_from('<8f', c.data, extremes)
    family = TrackFamily.QUATERNION if kind == 0 else TrackFamily.VECTOR3
    def value(v):
        v = tuple(v if family == TrackFamily.QUATERNION else v[:3])
        if not all(math.isfinite(x) for x in v) or (family == TrackFamily.QUATERNION and sum(x*x for x in v) < 1e-12):
            raise MotionParseError(f'{c.label}: invalid LMT key value')
        return v
    frames, values, frame = [0], [value(reference)], 1
    for pos in range(pointer, pointer+size, stride):
        raw = int.from_bytes(c.data[pos:pos+stride], 'little')
        if codec in (1, 2):
            xyz = struct.unpack_from('<3f', c.data, pos)
            v = (*xyz, math.sqrt(max(0, 1-sum(x*x for x in xyz))) if codec == 2 else 1)
            delta = 1
        elif codec in (4, 5):
            x, y, z, delta = struct.unpack_from('<4H' if codec == 4 else '<4B', c.data, pos)
            denominator = 65535 if codec == 4 else 255
            v = (x/denominator, y/denominator, z/denominator, 0)
        elif codec == 6:
            codes = [(raw >> shift) & 16383 for shift in (42, 28, 14, 0)]
            v = tuple((x if x < 8192 else x-16383)*4/16383 for x in codes)
            delta = raw >> 56
        elif codec == 7:
            v = tuple(((raw >> shift) & 127)/127 for shift in (21, 14, 7, 0))
            delta = raw >> 28
        else:
            v = [0., 0., 0., ((raw >> 14) & 16383)/16383]
            v[0 if codec == 11 else 1] = (raw & 16383)/16383
            delta = raw >> 28
        if bounds is not None:
            v = tuple(bounds[i+4]+bounds[i]*v[i] for i in range(4))
        if frame > end_frame:
            raise MotionParseError(f'{c.label}: LMT key exceeds animation duration')
        frames.append(frame)
        values.append(value(v))
        frame += delta
    return bone, kind, weight, KeyTrack(family, frames, values)


class LmtMotionFormatCodec:
    profile = LMT_PROFILE

    def matches(self, data):
        return len(data) >= 8 and bytes(data[:4]) == b'LMT\0' and struct.unpack_from('<H', data, 4)[0] == 67

    def parse(self, data, *, label='LMT', skeleton_data=None):
        c = ReadContext.from_bytes(data, label)
        if not self.matches(data):
            raise MotionParseError(f'{label}: expected little-endian LMT v67')
        path = ''
        if skeleton_data is None:
            path = resource_path(XX_HUNTER_MOD, required=True)
            skeleton_data = path.read_bytes()
        skeleton = read_mod_skeleton(skeleton_data, label=str(path) or 'MOD')
        by_id = {int(j.name[3:]): j for j in skeleton.joints[1:]}
        by_id[255] = skeleton.joints[0]
        model = LmtDocument(Path(label).stem, source=bytes(data), skeleton_path=str(path))
        count = c.u16(6)
        model.slot_count = count
        c.require(8, count*4, 'LMT animation pointers')
        payloads = {}
        for slot_id in range(count):
            base = c.u32(8+slot_id*4)
            payload = None
            if base:
                if base not in payloads:
                    c.require(base, 64, 'LMT animation header')
                    table, tracks, duration, loop = struct.unpack_from('<IIIi', c.data, base)
                    if not duration or loop < -1 or loop >= duration:
                        raise MotionParseError(f'{label}: invalid LMT duration/loop')
                    c.require(table, tracks*36, 'LMT track table')
                    motion = LmtMotion(f'{model.name}_{slot_id:03}', end_frame=duration,
                                       raw_end_frame=duration, looping=loop >= 0,
                                       loop_start_frame=max(0, loop), skeleton=skeleton)
                    nodes = {}
                    for i in range(tracks):
                        bone, kind, weight, track = decode_track(c, table+i*36, duration)
                        if bone not in by_id or (kind == 4) != (bone == 255):
                            raise MotionParseError(f'{label}: unsupported LMT bone binding {bone}/{kind}')
                        node = nodes.setdefault(bone, AnimationNode(by_id[bone], weight=weight))
                        attr = {0: 'rotation', 1: 'translation', 2: 'scale', 4: 'translation'}[kind]
                        if getattr(node, attr) is not None or node.weight != weight:
                            raise MotionParseError(f'{label}: conflicting LMT bone channels')
                        setattr(node, attr, track)
                    motion.animation_nodes = list(nodes.values())
                    events, floats = c.u32(base+52), c.u32(base+56)
                    if floats:
                        raise MotionParseError(f'{label}: LMT float tracks are not supported')
                    groups = []
                    if events:
                        c.require(events, 4*72, 'LMT event groups')
                        for g in range(4):
                            offset = events+g*72
                            remap = struct.unpack_from('<32H', c.data, offset)
                            n, ptr = c.u32(offset+64), c.u32(offset+68)
                            c.require(ptr, n*8, 'LMT events')
                            groups.append((remap, tuple(struct.unpack_from('<II', c.data, ptr+k*8) for k in range(n))))
                    motion.events = tuple(groups)
                    payloads[base] = EmbeddedPayload(motion)
                payload = payloads[base]
            # Zero LMT pointers are vacant IDs, not inherited RE MOT slots.
            if payload is not None:
                model.slots.append(MotionSlot(slot_id, MotionSlotType.MOT, payload))
        return model

    def write(self, model):
        raise MotionWriteError('LMT is a read-only animation source; bake to a Rise MOTLIST to save')


LMT_MOTION_FORMAT_CODEC = LmtMotionFormatCodec()
