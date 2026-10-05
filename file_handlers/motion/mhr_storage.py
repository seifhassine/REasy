"""Source-backed v528 editing with explicit relocation ownership."""
from __future__ import annotations

from dataclasses import dataclass, field
from bisect import bisect_left, bisect_right
from itertools import accumulate
import struct
import math

from .binary import align_up
from .errors import MotionWriteError
from .mot_list.model import MotList
from .mot.model import Joint, Motion, SyncPointGrid
from .sequence.model import SequenceTrack
from .mhr_tracks import encode_track
from utils.hash_util import murmur3_hash, murmur3_hash_utf16le


@dataclass(slots=True)
class Field:
    name: str
    offset: int
    format: str
    original: object
    value: object
    editable: bool = True
    owner: object = None
    attribute: str | None = None
    capacity: int = 0
    aliases: list | None = None

    def get(self):
        return getattr(self.owner, self.attribute) if self.attribute else self.value

    def set(self, value):
        if not self.editable:
            raise MotionWriteError(f"{self.name} is derived from the binary layout")
        for item in self.aliases or [self]:
            if item.attribute:
                setattr(item.owner, item.attribute, value)
            else:
                item.value = value

    def encode(self):
        value = self.get()
        if self.format == 'hash32':
            return struct.pack('<I', murmur3_hash_utf16le(value))
        if self.format == 'hash64':
            return struct.pack('<II', murmur3_hash(value.encode('utf-8')), murmur3_hash_utf16le(value))
        if self.format == 'loop':
            return struct.pack('<f', 0.0 if value else -1.0)
        if self.format == 'weight':
            if not 0 <= value <= 1:
                raise MotionWriteError('Animation weight must be between zero and one')
            return struct.pack('<B', round(value*255))
        if self.format in ('utf-16le', 'ascii'):
            data = value.encode(self.format) + (b'\0\0' if self.format == 'utf-16le' else b'\0')
            return data + bytes(max(0, self.capacity-len(data)))
        try:
            return struct.pack('<' + self.format, *(value if isinstance(value, (tuple, list)) else (value,)))
        except (struct.error, OverflowError) as exc:
            raise MotionWriteError(f"{self.name}: {exc}") from exc


@dataclass(slots=True)
class Group:
    name: str
    children: list = field(default_factory=list)


@dataclass(slots=True, eq=False)
class MhrJoint(Joint):
    binding_hash: int = 0


@dataclass(slots=True)
class MhrMotion(Motion):
    frames_per_second: int = 60
    frame_curves: list = field(default_factory=list)


@dataclass(slots=True)
class MhrSyncPointGrid(SyncPointGrid):
    start: int = 0
    end: int = 0


@dataclass(slots=True)
class MhrFrameCurve:
    # Native frame/scalar pairs adjacent to sync grids. The scalar's consumer
    # meaning is unresolved.
    points: list = field(default_factory=list)


@dataclass(slots=True)
class MhrSequenceTrack(SequenceTrack):
    filter_page3: int = 0
    filter_page4: int = 0
    filter_page5: int = 0


@dataclass(frozen=True, slots=True)
class Relocation:
    offset: int
    base: int
    target: int
    format: str = 'Q'
    unit: int = 1


@dataclass(slots=True)
class TrackBinding:
    name: str
    offset: int
    base: int
    track: object
    original_frames: tuple
    original_values: tuple


@dataclass(slots=True)
class MhrMotList(MotList):
    source: bytes = b''
    fields: list[Field] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)
    tracks: list[TrackBinding] = field(default_factory=list)
    relocations: dict[int, Relocation] = field(default_factory=dict)
    motion_spans: list[tuple[int, int, bool]] = field(default_factory=list)
    string_aliases: dict[tuple[int, str], list[Field]] = field(default_factory=dict)
    tree_spans: list = field(default_factory=list)
    slot_bindings: tuple = ()
    auxiliary_shapes: list = field(default_factory=list)
    motion_shapes: list = field(default_factory=list)
    clip_shapes: list = field(default_factory=list)


def auxiliary_shape(motion):
    append=motion.append
    return (tuple((id(g),g.block_count,g.point_count,g.start,g.end,id(g.frames),len(g.frames)) for g in motion.sync_points),
            tuple((id(c),tuple(id(p) for p in c.points)) for c in motion.frame_curves),
            None if append is None else (id(append),tuple((id(c),tuple((id(p),p.property_type) for p in c.properties),
                tuple((id(a),a.property_type,id(a.values),len(a.values)) for a in c.arrays)) for c in append.classes),tuple(id(r) for r in append.remaps)))


def clip_shape(clip):
    """Identity and unbound fields; scalar/name edits remain source-bound."""
    from .mot_clip.model import ClipPropertyType
    def flatten(roots, label):
        items=list(roots);seen=set()
        for item in items:
            if id(item) in seen:raise MotionWriteError(f'CLIP {label} has a cycle or shared owner')
            seen.add(id(item));items.extend(item.children)
        return items
    nodes=flatten([clip.root],'nodes')
    props=flatten([p for n in nodes for p in n.properties],'properties')
    def key_shape(key,kind):
        return (id(key),id(key.curve),key.offset_frame,
                key.value if kind in (ClipPropertyType.ACTION,ClipPropertyType.UNKNOWN) else None)
    return (tuple((id(n),n.start_frame,n.end_frame,n.root_guid,n.extra_guid,
                   tuple(id(c) for c in n.children),tuple(id(p) for p in n.properties)) for n in nodes),
            tuple((id(p),p.property_type,p.enum_closed,p.set_after_end_frame,p.restoration,p.set_delegate_enable,
                   p.prev_diff_frame_set,p.next_diff_frame_set,p.prev_key_value_set,tuple(id(c) for c in p.children),
                   tuple(key_shape(k,p.property_type) for k in p.keys),key_shape(p.last_key,p.property_type) if p.last_key else None,
                   tuple((id(s),id(s.curve)) for s in p.speed_points)) for p in props),
            tuple((id(r),id(r.owner),tuple((id(i),i.begin_frame is None) for i in r.intervals)) for r in clip.extra_ranges))


def validate_bound_model(model):
    current=slot_shape(model)
    if current!=model.slot_bindings:
        raise MotionWriteError('Slot structure changes require the Rise batch editing API')
    if len({s.motion_id for s in model.slots})!=len(model.slots):raise MotionWriteError('Duplicate MotionIDs')
    for clip,shape in model.clip_shapes:
        if clip_shape(clip)!=shape:raise MotionWriteError('CLIP graph/flag changes require ClipGraph and the Rise batch editing API')
    for motion,shape in model.motion_shapes:
        if motion_shape(motion)!=shape:raise MotionWriteError('Rig/track/sequence structure changes require the Rise batch editing API')
    for motion,shape in model.auxiliary_shapes:
        if auxiliary_shape(motion)!=shape:
            raise MotionWriteError('Auxiliary structure changes require build_motion with explicit native fields')
        if not 0<motion.frames_per_second<=0xffffffff or not math.isfinite(motion.end_frame):
            raise MotionWriteError('Invalid MOT timing')
        if motion.looping and motion.loop_start_frame:raise MotionWriteError('Rise loop intros require separate motions')
        for grid in motion.sync_points:
            if not all(math.isfinite(f) for f in grid.frames):raise MotionWriteError('Sync frames must be finite')
        for curve in motion.frame_curves:
            if any(len(p)!=2 or not all(math.isfinite(v) for v in p) for p in curve.points):
                raise MotionWriteError('MOT frame curves require finite frame/scalar pairs')


def motion_shape(motion):
    def track_shape(track):return None if track is None else (id(track),track.family)
    return (id(motion.skeleton),tuple((id(j),id(j.parent),tuple(id(c) for c in j.children),j.rest_scale,j.joint_map_extra_type) for j in motion.skeleton.joints),
            tuple((id(n),id(n.joint),track_shape(n.translation),track_shape(n.rotation),track_shape(n.scale)) for n in motion.animation_nodes),
            tuple(sequence_shape(s) for s in motion.sequences),tuple(id(p) for p in motion.property_tracks))


def sequence_shape(sequence):
    return (id(sequence),id(sequence.clip),tuple(id(t) for t in sequence.tracks))


def slot_shape(model):
    return tuple((id(s),id(s.payload),tuple(sequence_shape(o) for o in s.overrides)) for s in model.slots)


def write_document(model: MhrMotList) -> bytes:
    validate_bound_model(model)
    from .mhr_tree import build_tree, validate_tree
    from .mhr_structure import Layout, Splice
    changed = []
    for start,end,tree in model.tree_spans:
        validate_tree(tree)
        if tree.signature()!=tree.original_signature:
            changed.append((start,end,tree))
    raw = _write_fields_tracks(model)
    if not changed:
        return raw
    from .mhr_codec import MHR_MOTION_FORMAT_CODEC as codec
    current = codec.parse(raw)
    # Field/track edits can move payloads. Their order and sharing remain fixed.
    original_index = {start:i for i,(start,_,_) in enumerate(model.tree_spans)}
    edits = [Splice(current.tree_spans[original_index[start]][0],
                    current.tree_spans[original_index[start]][1], bytearray(build_tree(tree)))
             for start,end,tree in changed]
    output = bytes(Layout(current,edits).build())
    verified = codec.parse(output,label='rebuilt MTRE graphs')
    for start,end,tree in changed:
        if verified.tree_spans[original_index[start]][2].signature()!=tree.signature():
            raise MotionWriteError('Rebuilt MTRE graph differs from requested graph')
    return output


def _write_fields_tracks(model: MhrMotList) -> bytes:
    for aliases in model.string_aliases.values():
        changed = {item.get() for item in aliases if item.get() != item.original}
        if len(changed) > 1:
            raise MotionWriteError('Conflicting edits to a shared string')
        if changed:
            aliases[0].set(changed.pop())
    patched = bytearray(model.source)
    patches = {}
    insertions: dict[int, bytearray] = {}
    string_extensions = {}
    for item in model.fields:
        if item.get() != item.original:
            encoded = item.encode()
            if item.capacity and len(encoded) > item.capacity:
                extra = encoded[item.capacity:]
                extra += bytes(align_up(len(extra), 16)-len(extra))
                end = item.offset+item.capacity
                if end in string_extensions and string_extensions[end] != extra:
                    raise MotionWriteError('Conflicting edits to a shared string')
                string_extensions[end] = extra
                encoded = encoded[:item.capacity]
            for index, byte in enumerate(encoded, item.offset):
                if index in patches and patches[index] != byte:
                    raise MotionWriteError("Conflicting edits to shared MOTLIST data")
                patches[index] = byte
    for index, byte in patches.items():
        patched[index] = byte
    insertions.update((end, bytearray(data)) for end, data in string_extensions.items())

    spans = {start: (end, shared) for start, end, shared in model.motion_spans}
    rewritten = []
    rewritten_pointers = set()
    for binding in model.tracks:
        track = binding.track
        if tuple(track.frames) == binding.original_frames and tuple(track.values) == binding.original_values:
            continue
        end, _ = spans[binding.base]
        extra = insertions.setdefault(end, bytearray())
        def append(data, alignment):
            position = align_up(end+len(extra), alignment)-end
            extra.extend(bytes(position-len(extra)))
            extra.extend(data)
            return position
        flags, frames, values = encode_track(track)
        frame_offset = append(frames, 4)
        value_offset = append(values, 4)
        rewritten.append((binding, end, flags, frame_offset, value_offset))
        rewritten_pointers.update((binding.offset+8, binding.offset+12, binding.offset+16))
    if not insertions:
        return bytes(patched)

    for extra in insertions.values():
        extra.extend(bytes(align_up(len(extra), 16)-len(extra)))
    positions = sorted(insertions)
    shifts = [0, *accumulate(len(insertions[position]) for position in positions)]

    def relocated(offset, *, inclusive=True):
        index = bisect_right(positions, offset) if inclusive else bisect_left(positions, offset)
        return offset + shifts[index]

    for pointer in model.relocations.values():
        if pointer.offset in rewritten_pointers:
            continue
        delta = relocated(pointer.target)-relocated(pointer.base)
        if delta % pointer.unit:
            raise MotionWriteError('Relocation violates pointer element alignment')
        struct.pack_into('<'+pointer.format, patched, pointer.offset, delta//pointer.unit)
    for base, (end, shared) in spans.items():
        if struct.unpack_from('<I', patched, base+12)[0] and any(base < offset <= end for offset in insertions):
            size = relocated(end)-relocated(base)
            struct.pack_into('<I', patched, base+12, size)
            if shared:
                struct.pack_into('<Q', patched, base+16, size)
    output = bytearray()
    cursor = 0
    for end, extra in sorted(insertions.items()):
        output.extend(patched[cursor:end])
        output.extend(extra)
        cursor = end
    output.extend(patched[cursor:])
    for binding, end, flags, frames, values in rewritten:
        origin = relocated(end, inclusive=False)-relocated(binding.base)
        struct.pack_into('<5I', output, relocated(binding.offset), flags, len(binding.track.frames),
                         origin+frames, origin+values, 0)
    return bytes(output)
