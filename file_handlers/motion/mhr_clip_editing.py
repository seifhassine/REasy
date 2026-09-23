"""Source-preserving structural edits to compact CLIP 43 in MOTLIST 528."""
from dataclasses import asdict
import struct

from .binary import ReadContext, align_up
from .errors import MotionWriteError
from .mhr_codec import MHR_MOTION_FORMAT_CODEC as CODEC, MHR_PROFILE
from .mot_clip.parser_v43 import CompactClipV43Parser
from .mot_clip.model import ClipPropertyType
from .mhr_structure import copy_sequences, delete_sequences, edit_clip


def clone_bool_track(document, source_id, target_id, category, track_name, property_name, start, end):
    """Insert one four-key BOOL leaf; preserve every existing section and native pointer."""
    if not 0 < start <= end - 1:
        raise MotionWriteError('Frames must satisfy 0 < true-frame <= false-frame - 1')
    raw = document.source
    table = struct.unpack_from('<Q', raw, 16)[0]

    def select(motion_id):
        slots = [(i, s) for i, s in enumerate(document.slots) if s.motion_id == motion_id]
        if len(slots) != 1 or slots[0][1].payload is None:
            raise MotionWriteError(f'MotionID {motion_id} must resolve to one embedded payload')
        index, slot = slots[0]
        motion = slot.payload.value
        sequences = [(i, s) for i, s in enumerate(motion.sequences) if int(s.category) == category]
        if len(sequences) != 1:
            raise MotionWriteError(f'Motion {motion_id} category {category} resolved to {len(sequences)} sequences')
        position, sequence = sequences[0]
        base = struct.unpack_from('<Q', raw, table + index * 8)[0]
        wrappers = base + struct.unpack_from('<Q', raw, base + 48)[0]
        wrapper = base + struct.unpack_from('<Q', raw, wrappers + position * 8)[0]
        _, clip, tracks = struct.unpack_from('<3Q', raw, wrapper)
        parsed = CompactClipV43Parser(MHR_PROFILE).parse_result(
            ReadContext.from_bytes(raw), base + clip, base + tracks, pointer_base=base)
        return base, sequence, parsed

    source_base, source_sequence, template = select(source_id)
    base, target_sequence, target = select(target_id)
    selected = [r for r in template.nodes if track_name in r.node.name]
    if len(selected) != 1:
        raise MotionWriteError(f'Track {track_name!r} resolved to {len(selected)} template nodes')
    node = selected[0]
    if node.child_count or node.property_count != 1:
        raise MotionWriteError('BOOL track insertion requires a leaf with one property')
    prop = template.properties[node.property_index]
    if (prop.prop.name != property_name or prop.prop.property_type != ClipPropertyType.BOOL or
            len(prop.prop.keys) != 4 or prop.has_last_key or prop.speed_count or any(k.curve for k in prop.prop.keys)):
        raise MotionWriteError('Expected one four-key BOOL property without terminal keys, speed points, or curves')
    if any(track_name in r.node.name for r in target.nodes):
        raise MotionWriteError(f'Target motion {target_id} already owns a matching track')
    root = target.nodes[0]
    if root.child_index != 1 or root.child_count != len(target.nodes) - 1:
        raise MotionWriteError('Track insertion requires a flat target CLIP root')
    old_sections = list(target.section_absolute_offsets.values())
    clip_start, clip_end = target.clip_offset, target.physical_end
    sections = [bytearray(raw[a:b]) for a, b in zip(old_sections, [*old_sections[1:], clip_end])]
    counts = [len(target.nodes), len(target.properties), len(target.keys)]

    new_node = bytearray(raw[node.offset:node.offset + 40])
    struct.pack_into('<Q', new_node, 16, len(sections[8]) // 2)
    struct.pack_into('<Q', new_node, 24, 0)
    struct.pack_into('<Q', new_node, 32, counts[1])
    struct.pack_into('<H', sections[0], 0, root.child_count + 1)
    sections[0][40:40] = new_node
    new_prop = bytearray(raw[prop.offset:prop.offset + 72])
    struct.pack_into('<2f', new_prop, 0, 0, end)
    struct.pack_into('<Q', new_prop, 16, len(sections[7]))
    struct.pack_into('<Q', new_prop, 32, counts[2])
    sections[1].extend(new_prop)
    key_index = struct.unpack_from('<Q', raw, prop.offset + 32)[0]
    for index, (frame, value) in enumerate(((0, False), (start, True), (end - 1, True), (end, False))):
        record = template.keys[key_index + index]
        key = bytearray(raw[record.offset:record.offset + 32])
        struct.pack_into('<f', key, 0, frame)
        struct.pack_into('<Q', key, 16, int(value))
        sections[2].extend(key)
    sections[7].extend(property_name.encode('ascii') + b'\0')
    sections[8].extend(node.node.name.encode('utf-16le') + b'\0\0')

    def extra_rows(parsed, origin, *, only=None):
        offset = parsed.section_absolute_offsets['extra_ranges']
        count = struct.unpack_from('<I', raw, offset)[0]
        records = []
        for i in range(count):
            row = bytearray(raw[offset + 16 + i * 16:offset + 32 + i * 16])
            track, size, values = struct.unpack_from('<hhQ', row, 4)
            if only is not None and track + 1 != only:
                continue
            struct.pack_into('<h', row, 4, 0 if only is not None else track + 1)
            records.append((row, raw[origin + values:origin + values + size * 8]))
        return records

    added_ranges = extra_rows(template, source_base, only=node.index)
    old_ranges = extra_rows(target, base)
    all_ranges = added_ranges + old_ranges
    extra = bytearray(16 + 16 * len(all_ranges))
    struct.pack_into('<I', extra, 0, len(all_ranges))
    for i, (row, values) in enumerate(all_ranges):
        struct.pack_into('<Q', row, 8, len(extra))
        extra[16 + i * 16:32 + i * 16] = row
        extra.extend(values)
    sections[10] = extra

    offsets, cursor = [], clip_start + 112
    for section in sections:
        offsets.append(cursor)
        cursor += len(section)
    struct.pack_into('<Q', sections[10], 8, offsets[10] - base + 16)
    for i in range(len(all_ranges)):
        position = 16 + i * 16 + 8
        value = struct.unpack_from('<Q', sections[10], position)[0]
        struct.pack_into('<Q', sections[10], position, offsets[10] - base + value)
    header = bytearray(raw[clip_start:clip_start + 112])
    struct.pack_into('<3I', header, 12, counts[0] + 1, counts[1] + 1, counts[2] + 4)
    struct.pack_into('<11Q', header, 24, *(offset - base for offset in offsets))
    clip = header + b''.join(sections)
    clip.extend(bytes(-(clip_start + len(clip)) % 16))
    delta = len(clip) - (clip_end - clip_start)

    def relocate(offset):
        if offset < clip_start:
            return offset
        if offset >= clip_end:
            return offset + delta
        if offset < clip_start + 112:
            return offset
        section = max(i for i, start_offset in enumerate(old_sections) if start_offset <= offset)
        relative = offset - old_sections[section]
        if section == 0 and relative >= 40:
            relative += 40
        elif section == 10 and relative >= 16:
            if relative < 16 + len(old_ranges) * 16:
                relative += len(added_ranges) * 16
            else:
                relative += len(added_ranges) * 16 + sum(len(values) for _, values in added_ranges)
        return offsets[section] + relative

    output = bytearray(raw[:clip_start]) + clip + raw[clip_end:]
    markers = {start_offset + 16 for start_offset, _, shared in document.motion_spans if shared}
    for pointer in document.relocations.values():
        if pointer.offset in markers or clip_start <= pointer.offset < clip_end:
            continue
        value = relocate(pointer.target) - relocate(pointer.base)
        if value % pointer.unit:
            raise MotionWriteError('CLIP insertion violates pointer alignment')
        struct.pack_into('<' + pointer.format, output, relocate(pointer.offset), value // pointer.unit)
    old_size = struct.unpack_from('<I', raw, base + 12)[0]
    if old_size:
        struct.pack_into('<I', output, base + 12, old_size + delta)
        if base + 16 in markers:
            struct.pack_into('<Q', output, base + 16, struct.unpack_from('<Q', raw, base + 16)[0] + delta)

    output = bytes(output)
    verified = CODEC.parse(output, label='verified BOOL track insertion')
    if CODEC.write(verified) != output:
        raise MotionWriteError('Inserted CLIP does not roundtrip stably')
    target_after = next(s.payload.value for s in verified.slots if s.motion_id == target_id)
    sequence_after = next(s for s in target_after.sequences if int(s.category) == category)
    if [asdict(n) for n in sequence_after.clip.root.children[1:]] != [asdict(n) for n in target_sequence.clip.root.children]:
        raise MotionWriteError('Existing target CLIP nodes changed during insertion')
    if (sequence_after.clip.total_frame != target_sequence.clip.total_frame or
            [asdict(p) for p in sequence_after.clip.root.properties] != [asdict(p) for p in target_sequence.clip.root.properties]):
        raise MotionWriteError('Target CLIP timing or root properties changed during insertion')
    added = sequence_after.clip.root.children[0]
    expected_keys = [(struct.unpack('<f', struct.pack('<f', frame))[0], value)
                     for frame, value in ((0, False), (start, True), (end - 1, True), (end, False))]
    if added.name != node.node.name or [(k.frame, k.value) for k in added.properties[0].keys] != expected_keys:
        raise MotionWriteError('Inserted BOOL track differs from the requested values')
    if [s.motion_id for s in document.slots] != [s.motion_id for s in verified.slots]:
        raise MotionWriteError('Slot IDs changed during insertion')
    for span_start, span_end, _ in document.motion_spans:
        if span_start != base and raw[span_start:span_end] != output[relocate(span_start):relocate(span_end)]:
            raise MotionWriteError(f'Unrelated motion payload at {span_start} changed during insertion')
    if len(sequence_after.clip.extra_ranges) != len(added_ranges) + len(target_sequence.clip.extra_ranges):
        raise MotionWriteError('Unexpected extra-range count after insertion')
    for before, after in zip(target_sequence.clip.extra_ranges, sequence_after.clip.extra_ranges[len(added_ranges):]):
        if asdict(before) != asdict(after):
            raise MotionWriteError('Existing extra-range data changed during insertion')
    return output
