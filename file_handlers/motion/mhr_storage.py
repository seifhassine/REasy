"""Source-backed v528 editing with explicit relocation ownership."""
from __future__ import annotations

from dataclasses import dataclass, field
from bisect import bisect_left, bisect_right
from itertools import accumulate
import struct

from .binary import align_up
from .errors import MotionWriteError
from .mot_list.model import MotList
from .mot.model import Joint, Motion
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


def write_document(model: MhrMotList) -> bytes:
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
