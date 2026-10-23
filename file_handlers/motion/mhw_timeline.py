"""Read the embedded TIML data record used by MHW PC LMT v95 (x64).

Layouts: MHW-Free-HyperKinetics and MHW-EFX-Editor. Pointers are absolute
within the LMT, rather than offsets from the embedded TIML record.
"""
from dataclasses import dataclass
import math
import struct
import zlib

from .errors import MotionParseError


def property_hash(name):
    return ~zlib.crc32(name.encode('utf8')) & 0xffffffff


EFFECT_TYPES = {property_hash('nTimelineParam::EffectParameter'+str(i)) & 0x7fffffff:
                'nTimelineParam::EffectParameter'+str(i) for i in (1, 2)}
EFFECT_PROPERTIES = {property_hash(prefix+f'{i:02}'): (prefix, i)
                     for prefix in ('KeyFrame', 'WorkNo') for i in range(32)}


@dataclass(frozen=True, slots=True)
class MhwTimelineKey:
    value: object
    control_left: object
    control_right: object
    frame: float
    interpolation: int
    easing: int


@dataclass(frozen=True, slots=True)
class MhwTimelineProperty:
    hash: int
    data_type: int
    keys: tuple[MhwTimelineKey, ...]


@dataclass(frozen=True, slots=True)
class MhwTimelineType:
    hash: int
    reserved: int
    properties: tuple[MhwTimelineProperty, ...]


@dataclass(frozen=True, slots=True)
class MhwEffectCall:
    type_name: str
    slot: int
    work_numbers: tuple[MhwTimelineKey, ...]
    activation: tuple[MhwTimelineKey, ...]


@dataclass(frozen=True, slots=True)
class MhwTimeline:
    data_indices: tuple[int, int]
    length: float
    loop_start: float
    loop_control: int
    label_hash: int
    types: tuple[MhwTimelineType, ...]

    @property
    def effect_calls(self):
        calls = []
        for type in self.types:
            if type.hash not in EFFECT_TYPES:
                continue
            slots = {}
            for prop in type.properties:
                if prop.hash in EFFECT_PROPERTIES:
                    prefix, index = EFFECT_PROPERTIES[prop.hash]
                    fields = slots.setdefault(index, {})
                    if prefix in fields:
                        raise MotionParseError('Duplicate MHW TIML effect-slot property')
                    fields[prefix] = prop.keys
            for index, fields in sorted(slots.items()):
                calls.append(MhwEffectCall(EFFECT_TYPES[type.hash], index,
                                           fields.get('WorkNo', ()), fields.get('KeyFrame', ())))
        return tuple(calls)


def read_mhw_timeline(context, offset):
    context.require(offset, 40, 'MHW embedded TIML data')
    table, count, a, b, length, loop, control, label = struct.unpack_from('<QQiiffiI', context.data, offset)
    if not math.isfinite(length) or not math.isfinite(loop):
        raise MotionParseError(f'{context.label}: nonfinite MHW TIML length/loop')
    context.require(table, count*24, 'MHW TIML type table')
    types = []
    for i in range(count):
        props_at, props_count, type_hash, reserved = struct.unpack_from('<QQIi', context.data, table+i*24)
        context.require(props_at, props_count*24, 'MHW TIML property table')
        properties = []
        for j in range(props_count):
            keys_at, keys_count, prop_hash, data_type = struct.unpack_from('<QQIi', context.data, props_at+j*24)
            if data_type not in (0, 1, 2, 3, 4):
                raise MotionParseError(f'{context.label}: unsupported MHW TIML data type {data_type}')
            context.require(keys_at, keys_count*20, 'MHW TIML keys')
            fmt = {0: '<iiifhh', 1: '<IIIfhh', 2: '<ffffhh', 3: '<4Bfffhh', 4: '<IIIfhh'}[data_type]
            keys = []
            for k in range(keys_count):
                values = struct.unpack_from(fmt, context.data, keys_at+k*20)
                if data_type == 3:
                    value, left, right, frame, interpolation, easing = tuple(values[:4]), *values[4:]
                else:
                    value, left, right, frame, interpolation, easing = values
                if not math.isfinite(frame):
                    raise MotionParseError(f'{context.label}: nonfinite MHW TIML key frame')
                keys.append(MhwTimelineKey(value, left, right, frame, interpolation, easing))
            properties.append(MhwTimelineProperty(prop_hash, data_type, tuple(keys)))
        types.append(MhwTimelineType(type_hash, reserved, tuple(properties)))
    return MhwTimeline((a, b), length, loop, control, label, tuple(types))
