"""Fixed-size CLIP event edits using the native sequence locator and verifier."""
from dataclasses import asdict
import math
import struct

from .mhr_codec import MHR_MOTION_FORMAT_CODEC as CODEC
from .mhr_structure import Owner, materialize
from .mhr_validation import assert_other_slots
from .errors import MotionWriteError
from .mot_clip.model import ClipPropertyType, ASCII_VALUE_PROPERTY_TYPES, UTF16_VALUE_PROPERTY_TYPES


class _Edit:
    def __init__(self, document, motion_id):
        self.document = materialize(document)
        self.owner = Owner(self.document, motion_id, 'motion')
        self.output = bytearray(self.document.source)
        self.allowed = set()

    def write(self, offset, data):
        if not 0 <= offset <= len(self.output) - len(data):
            raise ValueError('CLIP edit exceeds the source extent')
        self.output[offset:offset + len(data)] = data
        self.allowed.update(range(offset, offset + len(data)))

    def frame(self, owner, name, offset, value):
        if not math.isfinite(value):
            raise ValueError('CLIP frame must be finite')
        encoded = struct.pack('<f', value)
        self.write(offset, encoded)
        setattr(owner, name, struct.unpack('<f', encoded)[0])

    def finish(self):
        output = bytes(self.output)
        if any(a != b and i not in self.allowed for i, (a, b) in enumerate(zip(self.document.source, output))):
            raise ValueError('CLIP edit changed unrelated bytes')
        reopened = CODEC.parse(output, label='verified CLIP event edit')
        if CODEC.write(reopened) != output:
            raise ValueError('CLIP event edit did not stabilize')
        actual = Owner(reopened, self.owner.slot.motion_id, 'motion')
        expected = [repr(asdict(record.parsed.clip)) for record in self.owner.records]
        if expected != [repr(asdict(record.parsed.clip)) for record in actual.records]:
            raise ValueError('Reopened CLIP graph differs from the requested edit')
        try:
            assert_other_slots(self.document, reopened, {self.owner.slot.motion_id})
        except AssertionError as exc:
            raise MotionWriteError('The edited CLIP storage is shared with another slot; detach it before a fixed-size edit') from exc
        return output


def _properties(record, node_name, names=()):
    for node in record.parsed.nodes:
        if node_name not in node.node.name:
            continue
        for prop in record.parsed.properties[node.property_index:node.property_index + node.property_count]:
            if not names or prop.prop.name in names:
                yield prop


def _keys(record, prop):
    selected = {id(key) for key in prop.keys}
    return [key for key in record.parsed.keys if id(key.key) in selected]


def set_bool_window(document, motion_id, *, node, property_name, on, off, category='EXTRA_0'):
    if not 0 < on < off or not math.isfinite(off):
        raise ValueError('Require finite frames with 0 < on < off')
    edit = _Edit(document, motion_id)
    record = edit.owner.one(category=category)
    selected = list(_properties(record, node, [property_name]))
    if not selected:
        raise ValueError('No matching BOOL property')
    frames = (0., on, off, off + 1.)
    for item in selected:
        prop = item.prop
        keys = _keys(record, prop)
        if prop.property_type != ClipPropertyType.BOOL or len(keys) != 4:
            raise ValueError('BOOL windows require one four-key BOOL property')
        edit.frame(prop, 'start_frame', item.offset, frames[0])
        edit.frame(prop, 'end_frame', item.offset + 4, frames[-1])
        for key, frame in zip(keys, frames):
            edit.frame(key.key, 'frame', key.offset, frame)
    return edit.finish()


def extend_property_range(document, motion_id, *, node, properties=(), end_frame=None, category='EXTRA_0'):
    edit = _Edit(document, motion_id)
    record = edit.owner.one(category=category)
    end = edit.owner.slot.payload.value.end_frame if end_frame is None else end_frame
    if not math.isfinite(end):
        raise ValueError('CLIP end frame must be finite')
    selected = list(_properties(record, node, properties))
    if not selected:
        raise ValueError('No matching CLIP property')
    for item in selected:
        prop = item.prop
        if prop.end_frame >= end - 1e-4:
            continue
        keys = _keys(record, prop)
        if not keys:
            raise ValueError(f'{prop.name} has no key to extend')
        edit.frame(prop, 'end_frame', item.offset + 4, end)
        edit.frame(keys[-1].key, 'frame', keys[-1].offset, end)
    return edit.finish()


def set_effect(document, motion_id, *, old, new, field='EffectId', category='VFX'):
    edit = _Edit(document, motion_id)
    selected = edit.owner.one(category=category)
    owners, hits = {}, []
    for record in edit.owner.records:
        parsed = record.parsed
        keys = {id(key.key): key for key in [*parsed.keys, *parsed.last_keys]}
        for item in parsed.properties:
            prop = item.prop
            if prop.property_type in UTF16_VALUE_PROPERTY_TYPES:
                pool, unit, encoding, terminator = parsed.section_absolute_offsets['unicode_strings'], 2, 'utf-16le', b'\0\0'
            elif prop.property_type in ASCII_VALUE_PROPERTY_TYPES:
                pool, unit, encoding, terminator = parsed.section_absolute_offsets['ascii_strings'], 1, 'ascii', b'\0'
            else:
                continue
            for key in [*prop.keys, *([prop.last_key] if prop.last_key else [])]:
                offset = pool + keys[id(key)].payload * unit
                owners[offset] = owners.get(offset, 0) + 1
                if record is selected and prop.name == field and key.value == old:
                    before, after = old.encode(encoding) + terminator, new.encode(encoding) + terminator
                    if len(before) != len(after):
                        raise ValueError('Replacement must occupy the same encoded byte length')
                    hits.append((key, offset, before, after))
    if not hits:
        raise ValueError(f'No {field} key equal to {old!r}')
    for key, offset, before, after in hits:
        if owners[offset] != 1:
            raise ValueError('Refusing to edit shared string storage')
        if edit.output[offset:offset + len(before)] != before:
            raise ValueError('Effect string disagrees with its native storage')
        edit.write(offset, after)
        key.value = new
    return edit.finish()


_INLINE_TYPES = {ClipPropertyType[name] for name in ('BOOL', 'U8', 'U16', 'U32', 'U64', 'S8', 'S16', 'S32', 'S64', 'F32', 'F64')}


def silence_events(document, motion_id, *, category='SOUND', node_classes=('snow.wwise.SoundBakeTriggerTracks',),
                   from_frame=100., frames=()):
    edit = _Edit(document, motion_id)
    record = edit.owner.one(category=category)
    explicit = {round(frame) for frame in frames}
    for node in record.parsed.nodes:
        if node.node.name not in node_classes:
            continue
        for item in record.parsed.properties[node.property_index:node.property_index + node.property_count]:
            if item.prop.property_type not in _INLINE_TYPES:
                continue
            for key in _keys(record, item.prop):
                if (round(key.key.frame) in explicit if explicit else key.key.frame >= from_frame):
                    edit.write(key.offset + 16, bytes(8))
                    key.key.value = type(key.key.value)(0)
    return edit.finish()


def move_event(document, motion_id, *, effect=None, trigger=None, track_index=None, to=None, delta=None):
    if sum(value is not None for value in (effect, trigger, track_index)) != 1:
        raise ValueError('Select an effect, trigger, or track index')
    if (to is None) == (delta is None):
        raise ValueError('Select an absolute frame or a delta')
    edit = _Edit(document, motion_id)
    selected, seen = None, 0
    for record in edit.owner.records:
        for node in record.parsed.nodes[1:]:
            props = {prop.name: prop for prop in node.node.properties}
            if node.node.name.endswith('VFXRangeTrack'):
                prop = props.get('EffectId')
                value = prop.keys[0].value if prop and prop.keys else None
                if effect is not None and value == effect or track_index is not None and seen == track_index:
                    selected = record, prop
                    break
                seen += 1
        if selected is None and trigger is not None:
            for node in record.parsed.nodes[1:]:
                if 'Sound' not in node.node.name and 'Voice' not in node.node.name:
                    continue
                props = {prop.name: prop for prop in node.node.properties}
                for name in ('_TriggerId', '_StopTriggerId'):
                    prop = props.get(name)
                    if prop and any(key.value == trigger for key in prop.keys):
                        selected = record, prop
                        break
                if selected:
                    break
        if selected:
            break
    if selected is None or selected[1] is None:
        raise ValueError('No matching CLIP event')
    record, prop = selected
    keys = [key for key in prop.keys if trigger is None or key.value == trigger]
    if not keys:
        raise ValueError('Selected event has no keys')
    shift = delta if delta is not None else to - keys[0].frame
    item = next(item for item in record.parsed.properties if item.prop is prop)
    moved = [key.frame + (shift if any(key is selected for selected in keys) else 0.) for key in prop.keys]
    edit.frame(prop, 'start_frame', item.offset, prop.start_frame + shift if len(prop.keys) == 1 else min(moved))
    if prop.end_frame >= 0:
        edit.frame(prop, 'end_frame', item.offset + 4, prop.end_frame + shift if len(prop.keys) == 1 else max(moved))
    selected_keys = {id(key) for key in keys + ([prop.last_key] if prop.last_key else [])}
    for key in [*record.parsed.keys, *record.parsed.last_keys]:
        if id(key.key) in selected_keys:
            edit.frame(key.key, 'frame', key.offset, key.key.frame + shift)
    return edit.finish()
