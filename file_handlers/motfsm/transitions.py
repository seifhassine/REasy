"""MOTFSM transition-map indirection and native interpolation records.

Wire layout: REE-Lib/RszFile/Motfsm2File.cs TransitionData. Enum identities
come from each game's dump, including the FSM-specific StartType/EndType.
"""
from dataclasses import dataclass
import math
import struct

from utils.enum_manager import registry_enums
from .serialization import splice_document


@dataclass(frozen=True)
class TransitionField:
    name: str
    value: object
    type_name: str
    enum_values: list
    binding: object = None


BIT_FIELDS = {
    'EndType': (0, 4, 'via.motion.detail.MotionFsm2TransitionData.EndType'),
    'InterpolationMode': (4, 4, 'via.motion.InterpolationMode'),
    'InterpolationCurve': (8, 4, 'via.motion.InterpolationCurve'),
    'PrevMoveToEnd': (12, 1, ''),
    'StartType': (13, 4, 'via.motion.detail.MotionFsm2TransitionData.StartType'),
    'ElapsedTimeZero': (17, 1, ''),
    'ContOnLayer': (18, 1, ''),
    'ContOnLayerInterpCurve': (19, 4, 'via.motion.InterpolationCurve'),
}
SCALARS = {'exitFrame': (8, 'f'), 'startFrame': (12, 'f'), 'interpolationFrame': (16, 'f'),
           'contOnLayerSpeed': (20, 'f'), 'contOnLayerTimeout': (24, 'f'),
           'contOnLayerNo': (28, 'H'), 'contOnLayerJointMaskId': (30, 'H')}
ORDER = ('interpolationFrame', 'InterpolationMode', 'InterpolationCurve', 'StartType', 'startFrame',
         'EndType', 'exitFrame', 'PrevMoveToEnd', 'ElapsedTimeZero', 'ContOnLayer',
         'ContOnLayerInterpCurve', 'contOnLayerSpeed', 'contOnLayerTimeout', 'contOnLayerNo', 'contOnLayerJointMaskId')


class TransitionTables:
    def __init__(self, document):
        self.document = document
        if document.version not in (31, 43):
            raise ValueError(f'Unsupported transition-data layout: MOTFSM {document.version}')
        self.stride = 24 if document.version == 31 else 36
        self.maps = {}
        for index in range(document.transition_map_count):
            offset = document.transition_map_tbl_offset+index*8
            identity, target = struct.unpack_from('<Ii', document.source, offset)
            if identity in self.maps:
                raise ValueError(f'Duplicate transition-map identity {identity}')
            if not 0 <= target < document.transition_data_count:
                raise ValueError(f'Transition map {identity} has invalid data index {target}')
            self.maps[identity] = (index, target)
        end = document.transition_data_tbl_offset+document.transition_data_count*self.stride
        if end != len(document.source):
            raise ValueError('Transition-data extent does not match the native record layout')

    def resolve(self, identity):
        if identity == 0 and identity not in self.maps:
            return None
        if identity not in self.maps:
            raise ValueError(f'Unknown transition-map identity {identity}')
        return self.maps[identity][1]

    def record(self, index):
        if not 0 <= index < self.document.transition_data_count:
            raise ValueError('Transition-data index is out of range')
        offset = self.document.transition_data_tbl_offset+index*self.stride
        return self.document.source[offset:offset+self.stride]

    def fields(self, index):
        data = self.record(index)
        flags = struct.unpack_from('<I', data, 4)[0]
        catalog = registry_enums(self.document.type_registry.json_path)
        fields = []
        for name in ORDER:
            if self.stride == 24 and (name.startswith('contOnLayer') or name.startswith('ContOnLayer')):
                continue
            if name in BIT_FIELDS:
                shift, width, enum_type = BIT_FIELDS[name]
                value = (flags >> shift) & ((1 << width)-1)
                members = catalog.get(enum_type, [])
                if width == 1:
                    value = bool(value)
                    members = [{'name': 'True', 'value': True}, {'name': 'False', 'value': False}]
                fields.append(TransitionField(name, value, enum_type or 'bool', members))
            else:
                offset, fmt = SCALARS[name]
                fields.append(TransitionField(name, struct.unpack_from('<'+fmt, data, offset)[0],
                                              'F32' if fmt == 'f' else 'U16', []))
        return fields


def patch_record(tables, index, name, value):
    field = next((field for field in tables.fields(index) if field.name == name), None)
    if field is None:
        raise ValueError(f'Unknown transition field {name}')
    member = next((m for m in field.enum_values if m['name'] == str(value)), None)
    if member:
        value = member['value']
    data = bytearray(tables.record(index))
    if name in BIT_FIELDS:
        shift, width, _ = BIT_FIELDS[name]
        if width == 1 and str(value).lower() in ('true', 'false'):
            value = str(value).lower() == 'true'
        value = int(value)
        if not 0 <= value < (1 << width):
            raise ValueError(f'{name} is outside its {width}-bit range')
        flags = struct.unpack_from('<I', data, 4)[0]
        mask = ((1 << width)-1) << shift
        struct.pack_into('<I', data, 4, (flags & ~mask) | (value << shift))
    else:
        offset, fmt = SCALARS[name]
        value = float(value) if fmt == 'f' else int(value)
        if fmt == 'f' and not math.isfinite(value):
            raise ValueError(f'{name} must be finite')
        try:
            struct.pack_into('<'+fmt, data, offset, value)
        except (struct.error, OverflowError) as exc:
            raise ValueError(f'Invalid {name}: {value}') from exc
    return bytes(data)


def edit_state_transition(document, binding, name, value):
    """Detach shared records/maps before editing the selected state's transition."""
    tables = TransitionTables(document)
    identity = binding.value
    index = tables.resolve(identity)
    if index is None:
        raise ValueError('This state has no transition-data record')
    changed = patch_record(tables, index, name, value)
    if changed == tables.record(index):
        return document.source
    map_users = sum(s.TransitionMaps == identity for n in document.bhvt.nodes for s in n.states)
    map_users += sum(s.mAllTransitionID == identity for n in document.bhvt.nodes for s in n.all_states)
    shared_map = map_users > 1
    shared_data = (shared_map or sum(target == index for _, target in tables.maps.values()) > 1
                   or document.start_transition_data_index == index)
    if not shared_data:
        output = bytearray(document.source)
        start = document.transition_data_tbl_offset+index*tables.stride
        output[start:start+tables.stride] = changed
        return bytes(output)

    fresh = max(struct.unpack_from('<I', tables.record(i))[0] for i in range(document.transition_data_count))+1
    if fresh > 0xFFFFFFFF:
        raise ValueError('Transition-data identities are exhausted')
    changed = bytearray(changed)
    struct.pack_into('<I', changed, 0, fresh)
    splices = [(len(document.source), len(document.source), bytes(changed))]
    if shared_map:
        if list(tables.maps) != sorted(tables.maps):
            raise ValueError('Transition-map table is not sorted')
        new_map = max(tables.maps)+1
        if new_map > 0xFFFFFFFF:
            raise ValueError('Transition-map identities are exhausted')
        start = document.transition_map_tbl_offset+document.transition_map_count*8
        data = struct.pack('<Ii', new_map, document.transition_data_count)
        data += bytes((-(start+len(data))) % 16)
        splices.append((start, document.transition_data_tbl_offset, data))
    output = splice_document(document, splices)
    struct.pack_into('<I', output, 52, document.transition_data_count+1)
    if shared_map:
        struct.pack_into('<I', output, 48, document.transition_map_count+1)
        struct.pack_into('<I', output, binding.offset, new_map)
    else:
        map_index = tables.maps[identity][0]
        struct.pack_into('<i', output, document.transition_map_tbl_offset+map_index*8+4,
                         document.transition_data_count)
    return bytes(output)
