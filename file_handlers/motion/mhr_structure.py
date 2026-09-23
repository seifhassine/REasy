"""Source-preserving MOTLIST splices and sequence ownership edits."""
from dataclasses import asdict, dataclass
import math
import struct

from .binary import ReadContext, align_up
from .errors import MotionWriteError
from .mhr_codec import MHR_MOTION_FORMAT_CODEC as CODEC, MHR_PROFILE
from .mot_clip.parser_v43 import CompactClipV43Parser
from .sequence.model import SequenceCategory


def snapshot(value):
    # repr also compares native NaN values consistently across separate parses.
    return repr(asdict(value))


def materialize(document):
    return CODEC.parse(CODEC.write(document), label='before structural edit')


def private_motion(document, motion_id, scope):
    """A slot edit must not mutate other slots that share the same MOT payload."""
    owner = Owner(document, motion_id, scope)
    if scope != 'motion' or sum(s.payload is owner.slot.payload for s in document.slots) == 1:
        return document
    raw = document.source
    pointers = struct.unpack_from('<Q', raw, 16)[0]
    _, end, shared = next(span for span in document.motion_spans if span[0] == owner.base)
    payload = bytearray(raw[owner.base:end])
    struct.pack_into('<I', payload, 12, len(payload))
    if shared:
        struct.pack_into('<Q', payload, 16, len(payload))
    # Stay beside the original so shared motions retain the same active rig.
    layout = Layout(document, [Splice(end, end, payload)])
    output = layout.build()
    struct.pack_into('<Q', output, layout.position(pointers + owner.index * 8), end)
    return CODEC.parse(bytes(output), label='private MOT for selected slot')


def category_id(value):
    try:
        return int(SequenceCategory[value.upper()] if isinstance(value, str) and not value.isdecimal()
                   else SequenceCategory(int(value)))
    except (KeyError, ValueError, TypeError) as exc:
        raise MotionWriteError(f'Unknown sequence category: {value!r}') from exc


@dataclass
class SequenceRecord:
    sequence: object
    wrapper: int
    base: int
    parsed: object
    tracks: int

    @property
    def ranges(self):
        return [(self.wrapper, self.wrapper + 64),
                (self.parsed.clip_offset, self.parsed.physical_end),
                (self.tracks, align_up(self.tracks + len(self.sequence.tracks) * 28, 16))]


class Owner:
    def __init__(self, document, motion_id, scope):
        if scope not in ('motion', 'override'):
            raise MotionWriteError('Sequence scope must be motion or override')
        matches = [(i, s) for i, s in enumerate(document.slots) if s.motion_id == motion_id]
        if len(matches) != 1:
            raise MotionWriteError(f'MotionID {motion_id} resolved to {len(matches)} slots')
        self.index, self.slot = matches[0]
        raw = document.source
        pointers, rows = struct.unpack_from('<QQ', raw, 16)
        if scope == 'motion':
            if self.slot.payload is None:
                raise MotionWriteError(f'MotionID {motion_id} has no embedded MOT')
            self.base = struct.unpack_from('<Q', raw, pointers + self.index * 8)[0]
            self.pointer_offset, self.count_offset = self.base + 48, self.base + 116
            self.sequences = self.slot.payload.value.sequences
            self.insertion = next(end for start, end, _ in document.motion_spans if start == self.base)
        else:
            self.base = 0
            self.pointer_offset, self.count_offset = rows + self.index * 72, rows + self.index * 72 + 23
            self.sequences = self.slot.overrides
            self.insertion = len(raw)
        self.table = self.base + struct.unpack_from('<Q', raw, self.pointer_offset)[0]
        self.records = []
        for index, sequence in enumerate(self.sequences):
            wrapper = self.base + struct.unpack_from('<Q', raw, self.table + index * 8)[0]
            _, clip, tracks = struct.unpack_from('<3Q', raw, wrapper)
            parsed = CompactClipV43Parser(MHR_PROFILE).parse_result(
                ReadContext.from_bytes(raw), self.base + clip, self.base + tracks, pointer_base=self.base)
            self.records.append(SequenceRecord(sequence, wrapper, self.base, parsed, self.base + tracks))

    def select(self, *, categories=None, positions=None):
        if categories is not None and positions is not None:
            raise MotionWriteError('Select sequence categories or positions, not both')
        if positions is not None:
            wanted = list(positions)
            if not wanted or len(set(wanted)) != len(wanted) or any(
                    not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(self.records) for i in wanted):
                raise MotionWriteError('Sequence positions must be distinct valid indices')
            return wanted
        if categories is None:
            raise MotionWriteError('An explicit sequence selection is required')
        wanted = [category_id(c) for c in categories]
        if not wanted or len(set(wanted)) != len(wanted):
            raise MotionWriteError('Select at least one distinct category')
        indexes = []
        for category in wanted:
            matches = [i for i, r in enumerate(self.records) if int(r.sequence.category) == category]
            if len(matches) != 1:
                raise MotionWriteError(f'Category {SequenceCategory(category).name} resolved to {len(matches)} sequences; use positions')
            indexes.extend(matches)
        return sorted(indexes)

    def one(self, *, sequence=None, category=None):
        if sequence is None and category is None:
            if len(self.records) != 1:
                raise MotionWriteError('Select a sequence position or category')
            return self.records[0]
        indexes = self.select(categories=None if category is None else [category],
                              positions=None if sequence is None else [sequence])
        return self.records[indexes[0]]


@dataclass
class Splice:
    start: int
    end: int
    data: bytearray


class Layout:
    def __init__(self, document, edits, *, motion_base=0, pointer_overrides=None):
        self.document, self.motion_base = document, motion_base
        self.pointer_overrides = pointer_overrides or {}
        self.edits = sorted(edits, key=lambda e: (e.start, e.end))
        previous = -1
        for edit in self.edits:
            if edit.start < previous or edit.end < edit.start or not 0 <= edit.start <= edit.end <= len(document.source):
                raise MotionWriteError('Overlapping or invalid structural regions')
            if (len(edit.data) - (edit.end - edit.start)) % 16:
                raise MotionWriteError('Structural edit must preserve native alignment')
            previous = edit.end

    def position(self, offset, *, before=False):
        shift = 0
        for edit in self.edits:
            if offset < edit.start or (before and offset == edit.start):
                break
            if edit.start <= offset < edit.end:
                if offset != edit.start:
                    raise MotionWriteError(f'Pointer targets a removed structural record at 0x{offset:X}')
                return edit.start + shift
            shift += len(edit.data) - (edit.end - edit.start)
        return offset + shift

    def replaced(self, offset):
        return any(e.start <= offset < e.end for e in self.edits)

    def build(self):
        raw = self.document.source
        output, cursor = bytearray(), 0
        for edit in self.edits:
            output.extend(raw[cursor:edit.start])
            output.extend(edit.data)
            cursor = edit.end
        output.extend(raw[cursor:])
        markers = {start + 16 for start, _, shared in self.document.motion_spans if shared}
        # A zero-sized terminal table belongs before new data appended at its end.
        boundaries = [(start, end) for start, end, _ in self.document.motion_spans]
        for pointer in self.document.relocations.values():
            if self.replaced(pointer.offset) or pointer.offset in markers:
                continue
            if pointer.offset in self.pointer_overrides:
                struct.pack_into('<' + pointer.format, output, self.position(pointer.offset),
                                 self.pointer_overrides[pointer.offset])
                continue
            before = any(pointer.target == e.start == e.end and
                         ((e.start == len(raw) and pointer.offset < e.start) or
                          any(start <= pointer.offset < end == e.start for start, end in boundaries))
                         for e in self.edits)
            target = self.position(pointer.target, before=before)
            base = self.position(pointer.base) if pointer.base else 0
            value = target - base
            if value < 0 or value % pointer.unit:
                raise MotionWriteError('Structural relocation violates pointer encoding')
            struct.pack_into('<' + pointer.format, output, self.position(pointer.offset), value // pointer.unit)
        if self.motion_base:
            delta = sum(len(e.data) - (e.end - e.start) for e in self.edits)
            size = struct.unpack_from('<I', raw, self.motion_base + 12)[0]
            if size:
                struct.pack_into('<I', output, self.position(self.motion_base + 12), size + delta)
            if self.motion_base + 16 in markers:
                marker = struct.unpack_from('<Q', raw, self.motion_base + 16)[0]
                struct.pack_into('<Q', output, self.position(self.motion_base + 16), marker + delta)
        return output


def _clone_size(raw, record):
    name = struct.unpack_from('<Q', raw, record.wrapper)[0]
    name_bytes = b''
    if name:
        _, end = ReadContext.from_bytes(raw).utf16_z(record.base + name)
        name_bytes = raw[record.base + name:end]
    size = 64 + record.parsed.physical_end - record.parsed.clip_offset
    size += align_up(len(record.sequence.tracks) * 28, 16)
    return align_up(size + len(name_bytes), 16), name_bytes


def _clone(document, record, origin, base, *, shift=0, total_frame=None):
    raw = document.source
    size, name = _clone_size(raw, record)
    clip_size = record.parsed.physical_end - record.parsed.clip_offset
    track_size = align_up(len(record.sequence.tracks) * 28, 16)
    blocks = [(record.wrapper, record.wrapper + 64, origin),
              (record.parsed.clip_offset, record.parsed.physical_end, origin + 64),
              (record.tracks, record.tracks + track_size, origin + 64 + clip_size)]
    result = bytearray(size)
    for start, end, destination in blocks:
        result[destination - origin:destination - origin + end - start] = raw[start:end]
    name_offset = 64 + clip_size + track_size
    result[name_offset:name_offset + len(name)] = name

    def remap(value):
        for start, end, destination in blocks:
            if start <= value < end:
                return destination + value - start
        for start, end, destination in reversed(blocks):
            if value == end:
                return destination + end - start
        raise MotionWriteError(f'Sequence pointer escapes its owned data: 0x{value:X}')

    for pointer in document.relocations.values():
        if not any(start <= pointer.offset < end for start, end, _ in blocks):
            continue
        if pointer.offset == record.wrapper:
            continue
        pointer_base = base if pointer.base == record.base else remap(pointer.base)
        value = remap(pointer.target) - pointer_base
        if value < 0 or value % pointer.unit:
            raise MotionWriteError('Copied sequence violates pointer alignment')
        struct.pack_into('<' + pointer.format, result, remap(pointer.offset) - origin, value // pointer.unit)
    struct.pack_into('<Q', result, 0, origin + name_offset - base if name else 0)
    if total_frame is not None:
        struct.pack_into('<f', result, 64 + 8, total_frame)
    if shift:
        for prop in record.parsed.properties:
            offset = remap(prop.offset) - origin
            start, end = struct.unpack_from('<2f', result, offset)
            struct.pack_into('<f', result, offset, max(0.0, start + shift))
            if end >= 0:
                struct.pack_into('<f', result, offset + 4, end + shift)
        for key in [*record.parsed.keys, *record.parsed.last_keys, *record.parsed.speed_points]:
            offset = remap(key.offset) - origin
            frame = struct.unpack_from('<f', result, offset)[0]
            struct.pack_into('<f', result, offset, max(0.0, frame + shift))
        extra = record.parsed.section_absolute_offsets['extra_ranges']
        count = struct.unpack_from('<I', raw, extra)[0]
        for i in range(count):
            row = extra + 16 + i * 16
            length = struct.unpack_from('<h', raw, row + 6)[0]
            values = record.base + struct.unpack_from('<Q', raw, row + 8)[0]
            for j in range(length):
                offset = remap(values + j * 8) - origin
                if struct.unpack_from('<I', result, offset)[0] != 0xFFFFFFFF:
                    frame = struct.unpack_from('<f', result, offset)[0]
                    struct.pack_into('<f', result, offset, max(0.0, frame + shift))
    return result


def verify(document, output, expected, *, motion_id, scope):
    reopened = CODEC.parse(bytes(output), label='verified CLIP structural edit')
    if [s.motion_id for s in reopened.slots] != [s.motion_id for s in document.slots]:
        raise MotionWriteError('Structural edit changed MotionIDs')
    target = Owner(document, motion_id, scope)
    for before, after in zip(document.slots, reopened.slots):
        edits_payload = scope == 'motion' and before.payload is target.slot.payload
        for left, right, changed in (
                (before.overrides, after.overrides, scope == 'override' and before is target.slot),
                (before.payload.value.sequences if before.payload else [],
                 after.payload.value.sequences if after.payload else [], edits_payload)):
            wanted = expected if changed else [snapshot(s) for s in left]
            if [snapshot(s) for s in right] != wanted:
                raise MotionWriteError(f'Sequence graph mismatch in MotionID {before.motion_id}')
    if CODEC.write(reopened) != bytes(output):
        raise MotionWriteError('Structural edit does not roundtrip stably')
    return reopened


def _edit_sequences(document, owner, plan, *, motion_id, scope):
    """Plan entries: retained index or (donor document, record, shift, total_frame)."""
    raw = document.source
    retained = {item for item in plan if isinstance(item, int)}
    keep_ranges = [span for i in retained for span in owner.records[i].ranges]
    removed_ranges = set(span for i, r in enumerate(owner.records) if i not in retained
                         for span in r.ranges if span[0] != span[1] and span not in keep_ranges)
    for start, end in removed_ranges:
        if any(start < b and a < end for a, b in keep_ranges):
            raise MotionWriteError('Partially shared sequence regions cannot be removed')
    edits = [Splice(a, b, bytearray()) for a, b in sorted(removed_ranges)]
    old_table_size = len(owner.records) * 8
    table_size = len(plan) * 8
    table_size += (old_table_size - table_size) % 16
    table_edit = Splice(owner.table, owner.table + old_table_size, bytearray(table_size)) if old_table_size else None
    if table_edit:
        edits.append(table_edit)
    added_size = sum(_clone_size(item[0].source, item[1])[0] for item in plan if not isinstance(item, int))
    if not old_table_size:
        table_size = align_up(len(plan) * 8, 16)
    body_size = added_size + (table_size if table_edit is None else 0)
    padding = -owner.insertion % 16 if body_size else 0
    insert = Splice(owner.insertion, owner.insertion, bytearray(align_up(padding + body_size, 16)))
    if insert.data:
        edits.append(insert)
    layout = Layout(document, edits, motion_base=owner.base)
    inserted_at = layout.position(owner.insertion, before=True)
    table_at = layout.position(owner.table) if table_edit else inserted_at + padding
    new_base = layout.position(owner.base) if owner.base else 0
    cursor = padding + (0 if table_edit else table_size)
    pointers, expected = [], []
    for item in plan:
        if isinstance(item, int):
            record = owner.records[item]
            pointers.append(layout.position(record.wrapper) - new_base)
            expected.append(snapshot(record.sequence))
        else:
            donor, record, shift, total_frame = item
            cloned = _clone(donor, record, inserted_at + cursor, new_base, shift=shift, total_frame=total_frame)
            insert.data[cursor:cursor + len(cloned)] = cloned
            pointers.append(inserted_at + cursor - new_base)
            # Parse the copied CLIP independently at its final pointer base.
            temporary = bytes(inserted_at + cursor) + bytes(cloned)
            clip = CompactClipV43Parser(MHR_PROFILE).parse_result(ReadContext.from_bytes(temporary),
                inserted_at + cursor + 64,
                new_base + struct.unpack_from('<Q', cloned, 16)[0], pointer_base=new_base).clip
            from copy import deepcopy
            seq = deepcopy(record.sequence)
            seq.clip = clip
            expected.append(snapshot(seq))
            cursor += len(cloned)
    table = table_edit.data if table_edit else insert.data
    for i, pointer in enumerate(pointers):
        struct.pack_into('<Q', table, i * 8 + (0 if table_edit else padding), pointer)
    output = layout.build()
    struct.pack_into('<Q', output, layout.position(owner.pointer_offset), table_at - new_base if plan else 0)
    struct.pack_into('<B', output, layout.position(owner.count_offset), len(plan))
    return verify(document, output, expected, motion_id=motion_id, scope=scope)


def copy_sequences(document, source_id, target_id, *, categories=None, positions=None,
                   shift=0.0, total_frame='keep', replace=False, donor=None,
                   source_scope='motion', target_scope='motion'):
    if not math.isfinite(shift) or total_frame not in ('keep', 'target'):
        raise MotionWriteError('Invalid sequence timing options')
    document = materialize(document)
    donor = materialize(donor) if donor is not None else document
    document = private_motion(document, target_id, target_scope)
    source, target = Owner(donor, source_id, source_scope), Owner(document, target_id, target_scope)
    selected = source.select(categories=categories, positions=positions)
    duration = None
    if total_frame == 'target':
        if target.slot.payload is None:
            raise MotionWriteError('Target duration requires an embedded MOT')
        duration = target.slot.payload.value.end_frame
    copies = [(donor, source.records[i], shift, duration) for i in selected]
    if replace:
        by_category = {}
        for item in copies:
            category = int(item[1].sequence.category)
            if category in by_category:
                raise MotionWriteError('Replacement requires one source sequence per category')
            by_category[category] = item
        plan, placed = [], set()
        for i, record in enumerate(target.records):
            category = int(record.sequence.category)
            if category in by_category:
                if category not in placed:
                    plan.append(by_category[category])
                    placed.add(category)
            else:
                plan.append(i)
        plan.extend(item for category, item in by_category.items() if category not in placed)
    else:
        plan = copies + list(range(len(target.records)))
    if len(plan) > 255:
        raise MotionWriteError('Sequence count exceeds the native byte limit')
    return _edit_sequences(document, target, plan, motion_id=target_id, scope=target_scope)


def delete_sequences(document, motion_id, *, categories=None, positions=None, scope='motion'):
    document = materialize(document)
    document = private_motion(document, motion_id, scope)
    owner = Owner(document, motion_id, scope)
    selected = set(owner.select(categories=categories, positions=positions))
    return _edit_sequences(document, owner, [i for i in range(len(owner.records)) if i not in selected],
                           motion_id=motion_id, scope=scope)


def edit_clip(document, motion_id, *, sequence=None, category=None, scope='motion', operation,
              donor=None, source_id=None, source_sequence=None, source_category=None, source_scope='motion',
              node_index=None, property_index=None, key_index=None, target_node_index=None,
              target_property_index=None, shift=0.0, frame=None):
    from .mot_clip.edit_v43 import ClipGraph
    document = materialize(document)
    original = document
    document = private_motion(document, motion_id, scope)
    owner = Owner(document, motion_id, scope)
    record = owner.one(sequence=sequence, category=category)
    graph = ClipGraph(document.source, record.parsed)
    metadata = [document.source[record.tracks + i * 28:record.tracks + (i + 1) * 28]
                for i in range(len(record.sequence.tracks))]
    from copy import deepcopy
    expected_tracks = deepcopy(record.sequence.tracks)
    if operation.endswith('-copy'):
        donor = materialize(donor) if donor is not None else original
        source = Owner(donor, motion_id if source_id is None else source_id, source_scope)
        selected = source.one(sequence=source_sequence, category=source_category)
        source_graph = ClipGraph(donor.source, selected.parsed)
        if operation == 'node-copy':
            graph.copy_node(source_graph, node_index, parent_index=0 if target_node_index is None else target_node_index, shift=shift)
            if target_node_index in (None, 0):
                source_node = source_graph.nodes[node_index]
                if source_node not in source_graph.clip.root.children:
                    raise MotionWriteError('A nested source node has no root track metadata to copy')
                index = source_graph.clip.root.children.index(source_node)
                if (len(record.sequence.tracks) != len(record.sequence.clip.root.children) or
                        len(selected.sequence.tracks) != len(selected.sequence.clip.root.children)):
                    raise MotionWriteError('Root node editing requires one metadata row per root child')
                metadata.append(donor.source[selected.tracks + index * 28:selected.tracks + (index + 1) * 28])
                expected_tracks.append(deepcopy(selected.sequence.tracks[index]))
        elif operation == 'property-copy':
            graph.copy_property(source_graph, property_index, node_index=target_node_index,
                                parent_property_index=target_property_index, shift=shift)
        elif operation == 'key-copy':
            graph.copy_key(source_graph, property_index, key_index, target_property_index, frame=frame, shift=shift)
        else:
            raise MotionWriteError(f'Unknown CLIP edit: {operation}')
    elif operation == 'node-delete':
        node = graph._select(graph.nodes, node_index, 'Node')
        if node in graph.clip.root.children:
            if len(record.sequence.tracks) != len(record.sequence.clip.root.children):
                raise MotionWriteError('Root node editing requires one metadata row per root child')
            index = graph.clip.root.children.index(node)
            del metadata[index]
            del expected_tracks[index]
        graph.delete_node(node_index)
    elif operation == 'property-delete':
        graph.delete_property(property_index)
    elif operation == 'key-delete':
        graph.delete_key(property_index, key_index)
    else:
        raise MotionWriteError(f'Unknown CLIP edit: {operation}')
    start, end = record.parsed.clip_offset, record.parsed.physical_end
    data = graph.build(origin_offset=start, pointer_base=owner.base)
    track_data = b''.join(metadata)
    track_data += bytes(-len(track_data) % 16)
    if record.tracks != end:
        raise MotionWriteError('CLIP track metadata must follow its CLIP section')
    end = align_up(record.tracks + len(record.sequence.tracks) * 28, 16)
    layout = Layout(document, [Splice(start, end, bytearray(data + track_data))], motion_base=owner.base,
                    pointer_overrides={record.wrapper + 16: start + len(data) - owner.base})
    output = layout.build()
    struct.pack_into('<I', output, layout.position(record.wrapper + 28), len(metadata))
    expected = [snapshot(s) for s in owner.sequences]
    replacement = deepcopy(record.sequence)
    replacement.clip = graph.clip
    replacement.tracks = expected_tracks
    expected[owner.records.index(record)] = snapshot(replacement)
    return verify(document, output, expected, motion_id=motion_id, scope=scope)
