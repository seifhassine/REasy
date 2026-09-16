"""Structural edits to source-backed Rise motion lists."""
from bisect import bisect_right
import struct

from .binary import align_up
from .errors import MotionWriteError
from .mhr_codec import MHR_MOTION_FORMAT_CODEC as CODEC


def next_motion_id(document):
    used = {slot.motion_id for slot in document.slots}
    candidate = max(used, default=-1)+1
    if candidate <= 0xFFFF:
        return candidate
    for candidate in range(0x10000):
        if candidate not in used:
            return candidate
    raise MotionWriteError('All motion IDs are in use')


def _copy_skeleton(source, anchor, destination):
    header = anchor + struct.unpack_from('<Q', source, anchor+16)[0]
    relative_table, count = struct.unpack_from('<QQ', source, header)
    table = anchor + relative_table
    result = bytearray(struct.pack('<QQ', destination+16, count))
    result.extend(source[table:table+count*80])
    for index in range(count):
        original = table+index*80
        record = 16+index*80
        name = anchor+struct.unpack_from('<Q', source, original)[0]
        end = name
        while source[end:end+2] != b'\0\0':
            end += 2
            if end+2 > len(source):
                raise MotionWriteError('Unterminated skeleton joint name')
        struct.pack_into('<Q', result, record, destination+len(result))
        result.extend(source[name:end+2])
        for field in (8, 16, 24):
            pointer = struct.unpack_from('<Q', source, original+field)[0]
            if pointer:
                joint_offset = anchor+pointer-table
                if joint_offset % 80 or not 0 <= joint_offset < count*80:
                    raise MotionWriteError('Skeleton link is outside its joint table')
                struct.pack_into('<Q', result, record+field, destination+16+joint_offset)
    result.extend(bytes((-len(result)) % 16))
    return result, count


def duplicate_slot(document, source_index, motion_id, *, name=None):
    """Append an independent MOT payload and slot overrides; retain old slots.

    Materialize pending edits first so all copied native offsets describe the
    same byte image. The returned document owns its new relocation bindings.
    """
    model = CODEC.parse(CODEC.write(document), label='before slot duplication')
    if not 0 <= source_index < len(model.slots):
        raise MotionWriteError('Source slot is out of range')
    if isinstance(motion_id, bool) or not isinstance(motion_id, int) or not 0 <= motion_id <= 0xFFFF:
        raise MotionWriteError('Motion ID must be an unsigned 16-bit integer')
    if any(slot.motion_id == motion_id for slot in model.slots):
        raise MotionWriteError(f'Motion ID {motion_id} already exists')
    slot = model.slots[source_index]
    if slot.payload is None:
        raise MotionWriteError('Source slot has no embedded motion')
    source = model.source
    pointers, rows = struct.unpack_from('<QQ', source, 16)
    count = len(model.slots)
    base = struct.unpack_from('<Q', source, pointers+source_index*8)[0]
    _, end, _shared = next(span for span in model.motion_spans if span[0] == base)
    payload = bytearray(source[base:end])
    # Native payloads share the file's rig (pointers[0] >= size); only the single
    # 001_Loop anchor owns a skeleton.  A shared copy stays shared, which keeps
    # the duplicate position independent without inventing a second anchor.
    struct.pack_into('<I', payload, 12, len(payload))

    row = bytearray(source[rows+source_index*72:rows+(source_index+1)*72])
    struct.pack_into('<H', row, 8, motion_id)
    # Slot +0x0C carries private per-motion data.  Native files never repeat a
    # nonzero value across motions, and inheriting it makes the engine reject the
    # new slot (the pose collapses to a T-pose), so a new motion starts at zero.
    struct.pack_into('<I', row, 12, 0)
    row.extend(bytes(8))
    override_table = struct.unpack_from('<Q', row)[0]
    override_count = row[23]
    overrides = bytearray(align_up(override_count*8, 16))
    override_ranges = []
    for index in range(override_count):
        wrapper = struct.unpack_from('<Q', source, override_table+index*8)[0]
        track_table = struct.unpack_from('<Q', source, wrapper+16)[0]
        tracks = struct.unpack_from('<I', source, wrapper+28)[0]
        stop = align_up(track_table+tracks*28, 16)
        position = len(overrides)
        overrides.extend(source[wrapper:stop])
        override_ranges.append((wrapper, stop, position))

    insertions = [(pointers+count*8, bytearray(16), 'pointer'),
                  (rows, payload, 'motion'), (rows+count*72, row, 'slot')]
    if overrides:
        padding = bytes((-len(source)) % 16)
        insertions.append((len(source), bytearray(padding)+overrides, 'overrides'))
    insertions.sort(key=lambda item: item[0])
    output, positions, shifts, blocks = bytearray(), [], [], {}
    cursor, shift = 0, 0
    for offset, data, label in insertions:
        output.extend(source[cursor:offset])
        blocks[label] = len(output)
        output.extend(data)
        cursor = offset
        shift += len(data)
        positions.append(offset)
        shifts.append(shift)
    output.extend(source[cursor:])

    def relocated(offset):
        index = bisect_right(positions, offset)
        return offset+(shifts[index-1] if index else 0)

    shared_markers = {start+16 for start, _, is_shared in model.motion_spans if is_shared}
    for pointer in model.relocations.values():
        if pointer.offset in shared_markers:
            continue
        target = relocated(pointer.target)
        if overrides and pointer.target == len(source):
            target = blocks['overrides']
        # A payload's empty terminal section points to its end, before the new
        # MOT inserted at the old slot table. Container pointers point after it.
        if pointer.target == rows and any(start <= pointer.offset < stop for start, stop, _ in model.motion_spans):
            target = blocks['motion']
        delta = target-relocated(pointer.base)
        if delta % pointer.unit:
            raise MotionWriteError('Slot duplication violates pointer alignment')
        struct.pack_into('<'+pointer.format, output, relocated(pointer.offset), delta//pointer.unit)
    struct.pack_into('<I', output, 48, count+1)
    struct.pack_into('<Q', output, blocks['pointer'], blocks['motion'])

    if overrides:
        new_table = blocks['overrides']+len(padding)
        struct.pack_into('<Q', output, blocks['slot'], new_table)
        for index, (start, stop, position) in enumerate(override_ranges):
            new_wrapper = new_table+position
            struct.pack_into('<Q', output, new_table+index*8, new_wrapper)
            def remap(value):
                if start <= value <= stop:
                    return new_wrapper+value-start
                if value == 0:
                    return 0
                raise MotionWriteError('Slot override references data outside its sequence')
            for pointer in model.relocations.values():
                if start <= pointer.offset < stop:
                    delta = remap(pointer.target)-remap(pointer.base)
                    if delta % pointer.unit:
                        raise MotionWriteError('Override duplication violates pointer alignment')
                    struct.pack_into('<'+pointer.format, output, new_wrapper+pointer.offset-start, delta//pointer.unit)
    result = CODEC.parse(bytes(output), label='duplicated slot')
    if name is not None:
        result.slots[-1].payload.value.name = name
        result = CODEC.parse(CODEC.write(result), label='named duplicated slot')
    return result
