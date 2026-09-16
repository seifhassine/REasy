"""Rise/Sunbreak MOTLIST 528, MOT 495 and compact CLIP 43."""
from __future__ import annotations

from dataclasses import replace
import struct

from .binary import ReadContext
from .errors import MotionParseError
from .profiles import DMC5_PROFILE
from .mot.model import Motion, Skeleton, Joint, AnimationNode, TrackFamily
from .mot_list.model import EmbeddedPayload, MotionSlot, MotionSlotType
from .sequence.model import SequenceData, SequenceCategory, SequenceTrack
from .mot_clip.model import (CONTAINER_PROPERTY_TYPES, ASCII_VALUE_PROPERTY_TYPES,
                             UTF16_VALUE_PROPERTY_TYPES, ClipPropertyType)
from .mot_clip.parser_v43 import CompactClipV43Parser
from .mhr_storage import MhrMotList, MhrJoint, MhrMotion, Relocation, Field, Group, TrackBinding, write_document
from .mhr_tracks import decode_track


MHR_PROFILE = replace(DMC5_PROFILE, name='Monster Hunter Rise / Sunbreak',
    motlist=replace(DMC5_PROFILE.motlist, version=528, row_size=72),
    mot=replace(DMC5_PROFILE.mot, version=495, header_size=128, sequence_wrapper_size=64,
                tracks_data_size=28, sequence_categories=frozenset(range(10))),
    mot_clip=replace(DMC5_PROFILE.mot_clip, version=43, header_size=112, node_size=40,
                     property_size=72, key_size=32))

class MhrParser:
    container_version = 528
    motion_version = 495
    decode_track = staticmethod(decode_track)

    def __init__(self, data, label):
        self.c = ReadContext.from_bytes(data, label)
        self.model = MhrMotList('', source=bytes(data))
        self.clip_parser = CompactClipV43Parser(MHR_PROFILE)
        self.skeleton = None
        self.joint_hashes = {}

    def field(self, c, group, name, offset, fmt, *, owner=None, attr=None, editable=True):
        c.require(offset, struct.calcsize('<'+fmt), name)
        values = struct.unpack_from('<'+fmt, c.data, offset)
        value = values[0] if len(values) == 1 else values
        item = Field(name, offset, fmt, value, value, editable, owner, attr)
        self.model.fields.append(item)
        group.children.append(item)
        return value

    def string(self, c, group, name, offset, owner=None, attr=None, wide=True):
        value, end = c.utf16_z(offset, name) if wide else c.ascii_z(offset, name)
        item = Field(name, offset, 'utf-16le' if wide else 'ascii', value, value,
                     True, owner, attr, end-offset)
        aliases = self.model.string_aliases.setdefault((offset, item.format), [])
        aliases.append(item)
        item.aliases = aliases
        self.model.fields.append(item)
        group.children.append(item)
        return value

    def pointer(self, c, group, name, offset, base=0):
        value = self.field(c, group, name, offset, 'Q', editable=False)
        self.relocation(c, offset, base)
        return value+base if value else 0

    def relocation(self, c, offset, base=0, fmt='Q', unit=1, nullable=True):
        value = struct.unpack_from('<'+fmt, c.data, offset)[0]
        if value or not nullable:
            self.model.relocations[offset] = Relocation(offset, base, base+value*unit, fmt, unit)

    def name_hash(self, group, offset, owner, *, wide_only=False):
        item = Field('Name hash', offset, 'hash32' if wide_only else 'hash64', owner.name,
                     owner.name, False, owner, 'name')
        self.model.fields.append(item)
        group.children.append(item)

    def parse(self):
        c, model = self.c, self.model
        if c.u32(0) != self.container_version or c.bytes(4, 4) != b'mlst':
            raise MotionParseError(f'{c.label}: expected MOTLIST {self.container_version}')
        header = Group(f'MOTLIST {self.container_version}')
        model.groups.append(header)
        self.field(c, header, 'Version', 0, 'I', editable=False)
        self.field(c, header, 'Flags', 8, 'Q')
        pointers = self.pointer(c, header, 'Motion pointer table', 16)
        rows = self.pointer(c, header, 'Slot table', 24)
        name = self.pointer(c, header, 'Name pointer', 32)
        path = self.pointer(c, header, 'Base MOTLIST path pointer', 40)
        count = self.field(c, header, 'Slot count', 48, 'I', editable=False)
        c.require(pointers, count*8, 'motion pointers')
        c.require(rows, count*72, 'v528 slots')
        model.name = self.string(c, header, 'Name', name, model, 'name')
        if path:
            model.base_motion_list_path = self.string(c, header, 'Base MOTLIST path', path, model, 'base_motion_list_path')
        addresses = [c.u64(pointers+i*8) for i in range(count)]
        for i in range(count):
            self.relocation(c, pointers+i*8)
        unique = sorted(set(addresses)-{0})
        if any(v < pointers+count*8 or v >= rows for v in unique):
            raise MotionParseError(f'{c.label}: motion payload outside the v528 payload area')
        payloads = {}
        motions = Group('Motions')
        model.groups.append(motions)
        for start, end in zip(unique, [*unique[1:], rows]):
            sub = c.subcontext(start, end, label=f'{c.label} / MOT @0x{start:X}')
            group = Group(f'MOT @0x{start:X}')
            motion = self.motion(sub, start, end, group)
            group.name = motion.name
            motions.children.append(group)
            payloads[start] = EmbeddedPayload(motion)
        slots = Group('Slots')
        model.groups.append(slots)
        for i, address in enumerate(addresses):
            r = rows+i*72
            slot = MotionSlot(c.u16(r+8), MotionSlotType.MOT, payloads.get(address))
            model.slots.append(slot)
            group = Group(f'[{i}] Motion ID {slot.motion_id}')
            slots.children.append(group)
            overrides = self.pointer(c, group, 'Override table pointer', r)
            self.field(c, group, 'Motion ID', r+8, 'H', owner=slot, attr='motion_id')
            self.field(c, group, 'Switch', r+10, 'H')
            self.field(c, group, 'Unknown 0x0C', r+12, 'I')
            slot.tag_hash = self.field(c, group, 'Tag hash', r+16, 'I', owner=slot, attr='tag_hash')
            self.field(c, group, 'Type', r+20, 'B', editable=False)
            self.field(c, group, 'Flags 0x15', r+21, 'B')
            self.field(c, group, 'Flags 0x16', r+22, 'B')
            override_count = self.field(c, group, 'Override count', r+23, 'B', editable=False)
            for j in range(24, 72, 4):
                self.field(c, group, f'Unknown 0x{j:02X}', r+j, 'I')
            if bool(overrides) != bool(override_count):
                raise MotionParseError(f'{c.label}: inconsistent slot override table')
            c.require(overrides, override_count*8, 'slot override pointers')
            self.read_overrides(c, overrides, override_count, slot, group)
        return model

    def read_overrides(self, c, table, count, slot, group):
        for j in range(count):
            wrapper = self.pointer(c, group, f'Override {j} pointer', table+j*8)
            child = Group(f'Override {j}')
            group.children.append(child)
            slot.overrides.append(self.sequence(c, wrapper, 0, child))

    def check_auxiliary(self, c, base, pointers):
        if any(pointers[i] for i in (2, 3, 5, 6, 7)):
            raise MotionParseError(f'{c.label}: unsupported nonempty MOT auxiliary section')

    def motion(self, c, base, end, group):
        if c.bytes(base+4, 4) == b'mtre':
            raise MotionParseError(f'{c.label}: Motion Tree {c.u32(base)} is not supported by the MOT {self.motion_version} preview')
        mot_version = c.u32(base)
        c.require(base, 128, f'MOT {mot_version} header')
        if mot_version != self.motion_version or c.bytes(base+4, 4) != b'mot ':
            raise MotionParseError(f'{c.label}: expected MOT {self.motion_version}')
        header = Group(f'Header (MOT {mot_version})')
        group.children.append(header)
        self.field(c, header, 'Version', base, 'I', editable=False)
        self.field(c, header, 'Flags', base+8, 'I')
        size = self.field(c, header, 'MOT size', base+12, 'I', editable=False)
        ptrs = [self.field(c, header, label, base+16+i*8, 'Q', editable=False) for i, label in enumerate(
            ('Skeleton', 'Animation nodes', 'Reserved pointer 0x20', 'Reserved pointer 0x28',
             'Sequences', 'Joint map', 'Extra data', 'Reserved pointer 0x48', 'Append data', 'Name'))]
        for i in range(10):
            self.relocation(c, base+16+i*8, base)
        self.check_auxiliary(c, base, ptrs)
        motion = MhrMotion(c.utf16_z(base+ptrs[9])[0])
        self.string(c, header, 'Name', base+ptrs[9], motion, 'name')
        for offset, label, attr in ((96, 'End frame', 'end_frame'),
                                    (104, 'Start frame', 'raw_start_frame'), (108, 'Raw end frame', 'raw_end_frame')):
            value = self.field(c, header, label, base+offset, 'f', owner=motion, attr=attr)
            setattr(motion, attr, value)
        loop_time = c.f32(base+100)
        if loop_time not in (-1, 0):
            raise MotionParseError(f'{c.label}: invalid loop time')
        motion.looping = loop_time == 0
        loop = Field('Looping', base+100, 'loop', motion.looping, motion.looping, True, motion, 'looping')
        self.model.fields.append(loop)
        header.children.append(loop)
        joint_count = self.field(c, header, 'Joint count', base+112, 'H', editable=False)
        node_count = self.field(c, header, 'Animation node count', base+114, 'H', editable=False)
        sequence_count = self.field(c, header, 'Sequence count', base+116, 'B', editable=False)
        self.field(c, header, 'Sync count', base+117, 'B', editable=False)
        for offset in (118, 124, 126):
            self.field(c, header, f'Unknown 0x{offset:02X}', base+offset, 'H')
        motion.frames_per_second = self.field(c, header, 'Frame rate', base+120, 'I', owner=motion, attr='frames_per_second')
        if not motion.frames_per_second:
            raise MotionParseError(f'{c.label}: zero frame rate')
        shared = bool(size and ptrs[0] >= size)
        if not shared:
            self.skeleton, self.joint_hashes = self.read_skeleton(c, base, base+ptrs[0], joint_count, group)
        # Shared motions retain their authored rig's count; the shared table can
        # have a different size. Channels bind by bone hash, checked below.
        if self.skeleton is None:
            raise MotionParseError(f'{c.label}: shared skeleton is absent')
        motion.skeleton = Skeleton(list(self.skeleton.joints))
        motion_hashes = dict(self.joint_hashes)
        self.model.motion_spans.append((base, end, shared))
        c.require(base+ptrs[1], node_count*12, 'v495 animation nodes')
        animation = Group('Bone tracks')
        group.children.append(animation)
        for i in range(node_count):
            r = base+ptrs[1]+i*12
            index, flags, hash_value, track_offset = struct.unpack_from('<HHII', c.data, r)
            self.relocation(c, r+8, base, 'I')
            joint = motion_hashes.get(hash_value)
            if joint is None:
                # These channels belong to an external mesh rig. Preserve their
                # actual identity without inventing a bone name or rest pose.
                joint = MhrJoint(f'Bone hash 0x{hash_value:08X}', translation=(float('nan'),)*3,
                                 rotation=(float('nan'),)*4, binding_hash=hash_value)
                motion_hashes[hash_value] = joint
                motion.skeleton.joints.append(joint)
            node = AnimationNode(joint, (flags >> 8)/255)
            motion.animation_nodes.append(node)
            ng = Group(joint.name)
            animation.children.append(ng)
            self.field(c, ng, 'Bone index', r, 'H', editable=False)
            self.field(c, ng, 'Channels', r+2, 'B', editable=False)
            weight = Field('Weight', r+3, 'weight', node.weight, node.weight, True, node, 'weight')
            self.model.fields.append(weight)
            ng.children.append(weight)
            if hash_value in self.joint_hashes:
                self.name_hash(ng, r+4, joint, wide_only=True)
            else:
                self.field(c, ng, 'Bone hash', r+4, 'I', editable=False)
            if flags & 0xF8:
                raise MotionParseError(f'{c.label}: unsupported animation channel flags')
            cursor = base+track_offset
            for bit, attr, family in ((1, 'translation', TrackFamily.VECTOR3),
                                       (2, 'rotation', TrackFamily.QUATERNION), (4, 'scale', TrackFamily.VECTOR3)):
                if flags & bit:
                    track = self.decode_track(c, cursor, base, family)
                    for offset in (8, 12, 16):
                        self.relocation(c, cursor+offset, base, 'I')
                    setattr(node, attr, track)
                    binding = TrackBinding(attr.title(), cursor, base, track, tuple(track.frames), tuple(track.values))
                    self.model.tracks.append(binding)
                    ng.children.append(binding)
                    cursor += 20
        self.read_sequences(c, base, ptrs[4], sequence_count, motion, group)
        if ptrs[8]:
            ag = Group('Append header')
            group.children.append(ag)
            for i in range(4):
                self.field(c, ag, f'Value {i}', base+ptrs[8]+i*8, 'Q', editable=False if i == 0 else True)
            self.relocation(c, base+ptrs[8], base+ptrs[8])
        return motion

    def read_sequences(self, c, base, table, count, motion, group):
        if not count:
            return
        c.require(base+table, count*8, 'sequence pointers')
        sequences = Group('Sequences')
        group.children.append(sequences)
        for i in range(count):
            self.relocation(c, base+table+i*8, base)
            wrapper = base+c.u64(base+table+i*8)
            child = Group(f'Sequence {i}')
            sequences.children.append(child)
            sequence = self.sequence(c, wrapper, base, child)
            child.name = f'{i}: {sequence.category.name}'
            motion.sequences.append(sequence)

    def read_skeleton(self, c, base, offset, count, parent):
        group = Group('Skeleton')
        parent.children.append(group)
        table = base+c.u64(offset)
        self.relocation(c, offset, base)
        if c.u64(offset+8) != count:
            raise MotionParseError(f'{c.label}: skeleton joint count mismatch')
        c.require(table, count*80, 'skeleton table')
        joints, hashes, parents = [], {}, []
        for i in range(count):
            r = table+i*80
            for j in (0, 8, 16, 24):
                self.relocation(c, r+j, base)
            joint = MhrJoint(c.utf16_z(base+c.u64(r))[0], binding_hash=c.u32(r+68))
            joints.append(joint)
            group_i = Group(f'[{i}] {joint.name}')
            group.children.append(group_i)
            self.string(c, group_i, 'Name', base+c.u64(r), joint, 'name')
            parents.append(c.u64(r+8))
            for j, label in ((8, 'Parent'), (16, 'First child'), (24, 'Next sibling')):
                self.field(c, group_i, label, r+j, 'Q', editable=False)
            joint.translation = self.field(c, group_i, 'Translation', r+32, '3f', owner=joint, attr='translation')
            self.field(c, group_i, 'Translation W', r+44, 'f')
            joint.rotation = self.field(c, group_i, 'Rotation', r+48, '4f', owner=joint, attr='rotation')
            self.field(c, group_i, 'Index', r+64, 'I', editable=False)
            hash_value = c.u32(r+68)
            self.name_hash(group_i, r+68, joint, wide_only=True)
            self.field(c, group_i, 'Unknown', r+72, 'Q')
            hashes[hash_value] = joint
        for joint, pointer in zip(joints, parents):
            if pointer:
                delta = base+pointer-table
                if delta < 0 or delta%80 or delta//80 >= count:
                    raise MotionParseError(f'{c.label}: invalid skeleton parent')
                joint.parent = joints[delta//80]
                joint.parent.children.append(joint)
        for joint in joints:
            seen = set()
            cursor = joint
            while cursor:
                if id(cursor) in seen:
                    raise MotionParseError(f'{c.label}: cyclic skeleton')
                seen.add(id(cursor))
                cursor = cursor.parent
        return Skeleton(joints), hashes

    def sequence(self, c, wrapper, base, group):
        c.require(wrapper, 64, 'sequence wrapper')
        name = self.pointer(c, group, 'Name pointer', wrapper, base)
        if name:
            self.string(c, group, 'Name', name)
        clip_offset = self.pointer(c, group, 'CLIP pointer', wrapper+8, base)
        tracks = self.pointer(c, group, 'Track metadata pointer', wrapper+16, base)
        self.field(c, group, 'Attributes', wrapper+24, 'I')
        count = self.field(c, group, 'Track metadata count', wrapper+28, 'I', editable=False)
        self.field(c, group, 'Use flags', wrapper+32, 'I')
        category = self.field(c, group, 'Category', wrapper+36, 'I')
        for offset in range(40, 64, 4):
            self.field(c, group, f'Unknown 0x{offset:02X}', wrapper+offset, 'I')
        parsed = self.clip_parser.parse_result(c, clip_offset, tracks, pointer_base=base)
        clip_group = Group('CLIP 43')
        group.children.append(clip_group)
        self.clip_fields(c, parsed, clip_group)
        metadata = Group('Track metadata')
        group.children.append(metadata)
        c.require(tracks, count*28, 'v495 sequence track metadata')
        seq = SequenceData(SequenceCategory(category), parsed.clip)
        category_field = next(item for item in group.children if isinstance(item, Field) and item.name == 'Category')
        category_field.owner, category_field.attribute = seq, 'category'
        for i in range(count):
            tg = Group(f'Track {i}')
            metadata.children.append(tg)
            values = [self.field(c, tg, 'Authored ID' if j == 0 else f'Filter page {j-1}', tracks+i*28+j*4, 'I')
                      for j in range(7)]
            seq.tracks.append(SequenceTrack(*values[:4]))
        return seq

    def clip_fields(self, c, parsed, group):
        h, base = parsed.clip_offset, parsed.pointer_base
        self.field(c, group, 'Total frame', h+8, 'f', owner=parsed.clip, attr='total_frame')
        for i, name in enumerate(parsed.section_relative_offsets):
            self.pointer(c, group, name+' pointer', h+24+i*8, base)
        sections = parsed.section_absolute_offsets
        for record in parsed.nodes:
            r = record.offset
            ng = Group(f'Node {record.index}: {record.node.name}')
            group.children.append(ng)
            self.field(c, ng, 'Child count', r, 'H', editable=False)
            self.field(c, ng, 'Property count', r+2, 'H', editable=False)
            self.field(c, ng, 'Node type', r+4, 'I')
            self.name_hash(ng, r+8, record.node)
            self.string(c, ng, 'Name', sections['unicode_strings']+record.unicode_name_index*2, record.node, 'name')
            self.relocation(c, r+16, sections['unicode_strings'], unit=2, nullable=False)
        key_records = {id(r.key): r for r in [*parsed.keys, *parsed.last_keys]}
        for record in parsed.properties:
            r, prop = record.offset, record.prop
            pg = Group(f'Property {record.index}: {prop.name} ({prop.property_type.name})')
            group.children.append(pg)
            self.field(c, pg, 'Start frame', r, 'f', owner=prop, attr='start_frame')
            self.field(c, pg, 'End frame', r+4, 'f', owner=prop, attr='end_frame')
            self.name_hash(pg, r+8, prop)
            self.string(c, pg, 'Name', sections['ascii_strings']+record.ascii_name_offset, prop, 'name', wide=False)
            self.relocation(c, r+16, sections['ascii_strings'], nullable=False)
            self.field(c, pg, 'Data offset', r+24, 'Q', editable=False)
            self.field(c, pg, 'Member start', r+32, 'Q', editable=False)
            self.field(c, pg, 'Member count', r+40, 'H', editable=False)
            self.field(c, pg, 'Array index', r+42, 'h', owner=prop, attr='array_index')
            self.field(c, pg, 'Speed count', r+44, 'B', editable=False)
            self.field(c, pg, 'Type', r+45, 'B', editable=False)
            self.field(c, pg, 'Unknown 0x2E', r+46, 'B')
            self.field(c, pg, 'Flags', r+47, 'B')
            self.field(c, pg, 'Last key index', r+48, 'Q', editable=False)
            self.field(c, pg, 'Speed index', r+56, 'Q', editable=False)
            self.field(c, pg, 'Clip property index', r+64, 'Q', editable=False)
            if prop.property_type in CONTAINER_PROPERTY_TYPES:
                continue
            for key in [*prop.keys, *([prop.last_key] if prop.last_key else [])]:
                kr = key_records[id(key)]
                kg = Group(f'Key @ {key.frame:g}')
                pg.children.append(kg)
                self.field(c, kg, 'Frame', kr.offset, 'f', owner=key, attr='frame')
                self.field(c, kg, 'Rate', kr.offset+4, 'f', owner=key, attr='rate')
                self.field(c, kg, 'Interpolation', kr.offset+8, 'B', owner=key, attr='interpolation')
                self.field(c, kg, 'Flags', kr.offset+9, 'B')
                self.field(c, kg, 'Reserved', kr.offset+12, 'I')
                self.field(c, kg, 'Curve index', kr.offset+24, 'Q', editable=False)
                self.key_value(c, kg, kr, prop.property_type, sections)
        for name, curves, width in (('hermite_curves', parsed.hermite_curves, 4), ('bezier3d_curves', parsed.bezier3d_curves, 8)):
            for i, curve in enumerate(curves):
                self.field(c, group, f'{name} {i}', sections[name]+i*width*4, f'{width}f', owner=curve, attr='values')
        for i, record in enumerate(parsed.speed_points):
            sg = Group(f'Speed point {i}')
            group.children.append(sg)
            self.field(c, sg, 'Frame', record.offset, 'f', owner=record.point, attr='frame')
            self.field(c, sg, 'Rate', record.offset+4, 'f', owner=record.point, attr='rate')
            self.field(c, sg, 'Interpolation', record.offset+8, 'I', owner=record.point, attr='interpolation')
        extra = sections['extra_ranges']
        self.pointer(c, group, 'Extra range table', extra+8, base)
        table = base+c.u64(extra+8)
        for i in range(c.u32(extra)):
            r = table+i*16
            eg = Group(f'Extra range {i}')
            group.children.append(eg)
            self.name_hash(eg, r, parsed.clip.extra_ranges[i].owner, wide_only=True)
            self.field(c, eg, 'Track', r+4, 'h', editable=False)
            count = self.field(c, eg, 'Interval count', r+6, 'h', editable=False)
            values = self.pointer(c, eg, 'Intervals', r+8, base)
            for j in range(count):
                self.field(c, eg, f'Begin {j}', values+j*8, 'f')
                self.field(c, eg, f'Span {j}', values+j*8+4, 'I')

    def key_value(self, c, group, record, kind, sections):
        key, raw = record.key, record.payload
        if kind in ASCII_VALUE_PROPERTY_TYPES:
            self.string(c, group, 'Value', sections['ascii_strings']+raw, key, 'value', wide=False)
            self.relocation(c, record.offset+16, sections['ascii_strings'], nullable=False)
        elif kind in UTF16_VALUE_PROPERTY_TYPES:
            self.string(c, group, 'Value', sections['unicode_strings']+raw*2, key, 'value')
            self.relocation(c, record.offset+16, sections['unicode_strings'], unit=2, nullable=False)
        elif kind == ClipPropertyType.PATH_POINT3D:
            self.field(c, group, 'Value', sections['owords']+raw*16, '3f', owner=key, attr='value')
        elif kind not in (ClipPropertyType.ACTION, ClipPropertyType.UNKNOWN):
            formats = {ClipPropertyType.BOOL: 'B', ClipPropertyType.S8: 'b', ClipPropertyType.U8: 'B',
                       ClipPropertyType.S16: 'h', ClipPropertyType.U16: 'H', ClipPropertyType.S32: 'i',
                       ClipPropertyType.U32: 'I', ClipPropertyType.S64: 'q', ClipPropertyType.U64: 'Q',
                       ClipPropertyType.F32: 'd', ClipPropertyType.F64: 'd'}
            self.field(c, group, 'Value', record.offset+16, formats[kind], owner=key, attr='value')


class MhrMotionFormatCodec:
    profile = MHR_PROFILE

    def matches(self, data):
        return len(data) >= 52 and struct.unpack_from('<I', data)[0] == 528 and bytes(data[4:8]) == b'mlst'

    def parse(self, data, *, label='MOTLIST'):
        return MhrParser(data, label).parse()

    def write(self, model):
        return write_document(model)


MHR_MOTION_FORMAT_CODEC = MhrMotionFormatCodec()
