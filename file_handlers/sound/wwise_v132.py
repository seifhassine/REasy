"""Exact editable layouts for the Wwise banks used by RE Engine games.

The original public API is retained for Wwise v132 compatibility. The same
compact reader covers DMC5/RE2's v125, RE8's v135, MHRise/RE4's v140,
MHWilds/RE9's v145, and Pragmata/Onimusha: Way of the Sword's v150 structures.
"""

from __future__ import annotations

import bisect
from contextlib import contextmanager
from dataclasses import dataclass, replace
import math
import struct

from .wwise_schema import (
    BNK_CURVE_INTERPOLATION,
    BNK_CURVE_SCALING,
    BNK_FX_ENUMS,
    BNK_FX_SCHEMAS,
    BNK_FUTZBOX_139_FIELDS,
    BNK_STANDARD_CUE_NAMES,
    BNK_TIME_STRETCH_16_FIELDS,
    _ACTION_FAMILIES,
    attenuation_targets,
    fx_property_names,
    integer_properties,
    property_names,
)


# These Wwise generations write true as either 1 or 0xFF.
_BOOL = {0: "No", 1: "Yes", 0xFF: "Yes (0xFF encoding)"}
_GROUP = {0: "Switch", 1: "State"}
_RTPC_TYPE_140 = {0: "Game Parameter", 1: "MIDI Parameter", 2: "Modulator"}
_RTPC_TYPE_145 = {
    0: "Game Parameter", 1: "MIDI Parameter", 2: "Switch",
    3: "State", 4: "Modulator",
}
_ACCUM = {
    0: "None", 1: "Exclusive", 2: "Additive", 3: "Multiply",
    4: "Boolean", 5: "Maximum", 6: "Filter",
}
_ACCUM_125 = {0: "Exclusive", 1: "Additive", 2: "Multiply"}
_SYNC = {
    0: "Immediate", 1: "Next grid", 2: "Next bar", 3: "Next beat",
    4: "Next marker", 5: "Next user marker", 6: "Entry marker",
    7: "Exit marker", 8: "Never exit", 9: "Last exit position",
}
_CURVE = dict(BNK_CURVE_INTERPOLATION)
_SCALING = dict(BNK_CURVE_SCALING)
_VALUE_MEANING = {0: "Default", 1: "Independent", 2: "Offset"}
_VIRTUAL_QUEUE = {0: "From beginning", 1: "From elapsed time", 2: "Resume"}
_BELOW_THRESHOLD = {
    0: "Continue playing", 1: "Kill voice", 2: "Virtual voice",
    3: "Kill one-shot; otherwise virtual",
}
_PANNER = {0: "Direct speaker assignment", 1: "Balance / fade-height", 2: "Steering"}
_POSITION_TYPE = {0: "Emitter", 1: "Emitter with automation", 2: "Listener with automation"}
_SPATIALIZATION = {0: "None", 1: "Position only", 2: "Position and orientation"}
_PATH_MODE = {
    0: "Step sequence", 1: "Step random", 2: "Continuous sequence",
    3: "Continuous random", 4: "Step sequence / new path",
    5: "Step random / new path",
}
_TRACK_TYPE = {0: "Normal", 1: "Random", 2: "Sequence", 3: "Switch"}
_CLIP_AUTOMATION = {0: "Volume", 1: "LPF", 2: "HPF", 3: "Fade in", 4: "Fade out"}
_DECISION_MODE = {0: "Best match", 1: "Weighted"}
_RANDOM_SEQUENCE_TYPE = {
    0: "Continuous sequence", 1: "Step sequence",
    2: "Continuous random", 3: "Step random",
    0xFFFFFFFF: "None (leaf segment)",
}
_ENTRY_TYPE = {
    0: "Entry marker", 1: "Same time", 2: "Random marker",
    3: "Random user marker", 4: "Last exit time",
}
_JUMP_TO = {
    0: "Start of playlist", 1: "Specific item",
    2: "Last played segment", 3: "Next segment",
}
_RAMP = {0: "None", 1: "Slew rate", 2: "Filtering over time"}
_TRANSITION_MODE = {
    0: "Disabled", 1: "Crossfade (amplitude)", 2: "Crossfade (equal power)",
    3: "Delay", 4: "Sample accurate", 5: "Trigger rate",
}
_BUILT_IN = {
    0: "None", 1: "Distance", 2: "Azimuth", 3: "Elevation",
    4: "Emitter cone", 5: "Obstruction", 6: "Occlusion",
    7: "Listener cone", 8: "Diffraction", 9: "Transmission loss",
}
_BUILT_IN_126 = {
    0: "None", 1: "Distance", 2: "Azimuth", 3: "Elevation",
    4: "Object angle", 5: "Obstruction", 6: "Occlusion",
}
_PLUGIN_TYPE = {
    0: "None", 1: "Codec", 2: "Source", 3: "Effect",
    4: "Motion device", 5: "Motion source", 6: "Mixer",
    7: "Audio device", 8: "Global extension", 9: "Metadata",
}
_BANK_TYPE = {0: "User", 0x1E: "Event", 0x1F: "Bus"}
_FILTER_BEHAVIOR = {0: "Additive", 1: "Maximum"}
_STANDARD_CUES = dict(BNK_STANDARD_CUE_NAMES)
_REFLECT_SMOOTHING = {0: "IIR", 1: "FIR"}
_REFLECT_THRESHOLD = {0: "Continuous", 1: "Step"}
_REFLECT_DECORRELATION_ALGORITHM = {0: "Performance", 1: "Quality"}
_REFLECT_DECORRELATION_SOURCE = {0: "Textures", 1: "Global"}
_REFLECT_OUTPUT = {
    0: "Parent bus", 16641: "Mono", 12546: "Stereo", 28931: "3.0",
    6304004: "4.0", 6353158: "5.1", 6549768: "7.1",
    90239240: "5.1.2", 90435850: "7.1.2", 761524492: "7.1.4",
    516: "Ambisonics 1st order", 521: "Ambisonics 2nd order",
    528: "Ambisonics 3rd order", 761327882: "Auro 9.1",
    769716491: "Auro 10.1", 803270924: "Auro 11.1",
    803467534: "Auro 13.1", 33025: "LFE",
}
_REFLECT_CURVES = {
    0: "Image-source/listener distance attenuation",
    1: "Emitter/listener distance attenuation",
    2: "Image-source/listener distance spread",
    3: "Image-source/listener distance low-pass filter",
    4: "Image-source/listener distance high-pass filter",
    5: "Diffraction attenuation",
    6: "Diffraction low-pass filter",
    7: "Diffraction high-pass filter",
}
_REFLECT_CURVES_145 = {
    0: "Image-source/listener distance attenuation",
    1: "Image-source/listener distance low-pass filter",
    2: "Image-source/listener distance high-pass filter",
    3: "Image-source/listener distance spread",
    4: "Diffraction attenuation",
    5: "Diffraction low-pass filter",
    6: "Diffraction high-pass filter",
}
_WOOSH_CHANNELS = {0: "1.0", 1: "2.0", 2: "4.0"}
_WOOSH_NOISE = {0: "White", 1: "Pink", 2: "Red", 3: "Purple"}
_WOOSH_CURVES = {
    0: "Object speed", 20: "Frequency shift",
    23: "Q factor shift", 26: "Gain offset",
}
_WIND_CURVES = {
    0: "Wind speed", 3: "Direction", 6: "Variability", 9: "Gustiness",
    20: "Frequency shift", 23: "Q factor shift", 26: "Gain offset",
}


@dataclass(frozen=True, slots=True)
class WwiseField:
    """One scalar leaf in a compiled Wwise object."""

    path: str
    label: str
    storage: str
    offset: int
    size: int
    value: int | float | str
    enum: tuple[tuple[int, str], ...] = ()
    id_kind: str = ""
    reference_role: str = ""
    editable: bool = True
    visible: bool = True
    mask: int = 0
    shift: int = 0

    def enum_label(self) -> str:
        return dict(self.enum).get(int(self.value), "") if self.enum else ""


@dataclass(frozen=True, slots=True)
class WwiseObjectLayout:
    """Field map plus proof that the complete payload was consumed."""

    fields: tuple[WwiseField, ...]
    consumed: int
    size: int
    anchors: tuple[tuple[str, int], ...] = ()
    error: str = ""
    source: tuple = ()

    @property
    def complete(self) -> bool:
        return not self.error and self.consumed == self.size

    def field(self, path: str) -> WwiseField | None:
        return next((item for item in self.fields if item.path == path), None)

    def anchor(self, name: str) -> int | None:
        return dict(self.anchors).get(name)


@dataclass(frozen=True, slots=True)
class WwiseChunkLayout:
    """Exact settings layout for one non-media bank chunk."""

    chunk_id: str
    title: str
    payload: bytes
    structure: WwiseObjectLayout


@dataclass(frozen=True, slots=True)
class WwiseList:
    path: str
    count_path: str
    items: tuple[str, ...]
    lanes: tuple[tuple[str | None, ...], ...] = ()


class _LayoutError(ValueError):
    pass


class _Unit:
    __slots__ = ("path", "storage", "offset", "raw")

    def __init__(self, path, storage, offset, raw):
        self.path, self.storage, self.offset, self.raw = path, storage, offset, raw


class _Lane:
    __slots__ = ("base", "root", "start", "end", "items", "ordered")

    def __init__(self, base, root, start, ordered=False):
        self.base, self.root, self.start, self.end, self.items = base, root, start, start, []
        self.ordered = ordered

    def item_root(self, index):
        label = self.root.format(index + 1)
        return f"{self.base}/{label}" if self.base else label


class _List:
    __slots__ = ("count", "scale", "lanes", "editable")

    def __init__(self, count, scale, editable):
        self.count, self.scale, self.lanes, self.editable = count, scale, [], editable


class _Reader:
    _FORMATS = {
        "u8": "B", "i8": "b", "u16": "H", "i16": "h",
        "u32": "I", "i32": "i", "f32": "f", "f64": "d", "bool": "B",
    }
    writing = False

    def __init__(
        self, data: bytes, version: int = 132, feedback: bool = False, record: bool = False
    ):
        self.data, self.pos, self.version = data, 0, int(version)
        # v126 and older nodes end with a feedback bus when the bank header enables it.
        self.feedback = bool(feedback)
        self.fields: list[WwiseField] = []
        self.parts: list[str] = []
        self.anchors: dict[str, int] = {}
        self.units: list[_Unit] | None = [] if record else None
        self.lists: list[_List] | None = [] if record else None
        self._pending = self._list = None

    def _raw(self, path: str, storage: str, size: int, problem: str = "") -> bytes:
        if size < 0 or self.pos + size > len(self.data):
            raise _LayoutError(f"{path} {problem or f'exceeds the payload at 0x{self.pos:X}'}")
        raw = bytes(self.data[self.pos:self.pos + size])
        if self.units is not None:
            self.units.append(_Unit(path, storage, self.pos, raw))
        self.pos += size
        return raw

    def _var_size(self, path: str) -> int:
        for size in range(1, 6):
            if self.pos + size > len(self.data):
                raise _LayoutError(f"{path} has a truncated variable integer")
            if not self.data[self.pos + size - 1] & 0x80:
                return size
        raise _LayoutError(f"{path} has an oversized variable integer")

    def _zstring_size(self, path: str) -> int:
        end = self.data.find(b"\0", self.pos)
        if end < 0:
            raise _LayoutError(f"{path} has no NUL terminator")
        return end + 1 - self.pos

    @contextmanager
    def section(self, name: str):
        self.parts.append(name)
        try:
            yield
        finally:
            self.parts.pop()

    def _path(self, label: str) -> str:
        return "/".join((*self.parts, label))

    @staticmethod
    def _enum(values) -> tuple[tuple[int, str], ...]:
        return tuple(sorted((int(key), str(value)) for key, value in (values or {}).items()))

    def scalar(
        self,
        label: str,
        storage: str,
        *,
        enum=None,
        id_kind: str = "",
        reference_role: str = "",
        editable: bool = True,
        visible: bool = True,
    ):
        fmt = "<" + self._FORMATS[storage]
        path, offset = self._path(label), self.pos
        value = struct.unpack(fmt, self._raw(path, storage, struct.calcsize(fmt)))[0]
        if storage.startswith("f") and not math.isfinite(value):
            raise _LayoutError(f"{path} is not finite")
        self.fields.append(WwiseField(
            path, label, storage, offset, self.pos - offset, value,
            self._enum(enum), id_kind, reference_role, editable, visible,
        ))
        return value

    def var(
        self,
        label: str,
        *,
        enum=None,
        id_kind: str = "",
        editable: bool = False,
        visible: bool = True,
    ) -> int:
        path, offset = self._path(label), self.pos
        value = 0
        for byte in self._raw(path, "var", self._var_size(path)):
            value = (value << 7) | (byte & 0x7F)
        self.fields.append(WwiseField(
            path, label, "var", offset, self.pos - offset, value,
            self._enum(enum), id_kind, "", editable, visible,
        ))
        return value

    def flags(self, label: str, specs, *, storage: str = "u8") -> int:
        fmt = "<" + self._FORMATS[storage]
        offset = self.pos
        raw = struct.unpack(fmt, self._raw(self._path(label), storage, struct.calcsize(fmt)))[0]
        size = self.pos - offset
        used = 0
        for spec in specs:
            name, shift = spec[:2]
            width = spec[2] if len(spec) > 2 else 1
            enum = spec[3] if len(spec) > 3 else (_BOOL if width == 1 else None)
            mask = ((1 << width) - 1) << shift
            used |= mask
            self.fields.append(WwiseField(
                self._path(name), name, "bit", offset, size,
                (raw & mask) >> shift, self._enum(enum), "", "", True, True,
                mask, shift,
            ))
        full_mask = (1 << (size * 8)) - 1
        reserved = full_mask & ~used
        if reserved:
            shift = (reserved & -reserved).bit_length() - 1
            self.fields.append(WwiseField(
                self._path(f"{label} reserved bits"), f"{label} reserved bits", "bit",
                offset, size, (raw & reserved) >> shift, (), "", "", False,
                bool(raw & reserved), reserved, shift,
            ))
        return raw

    def blob(
        self, label: str, size: int, *, visible: bool = False, editable: bool = False
    ) -> bytes:
        path, offset = self._path(label), self.pos
        value = self._raw(path, "bytes", size, f"has invalid size {size}")
        self.fields.append(WwiseField(
            path, label, "bytes", offset, len(value), value.hex(" "),
            editable=editable, visible=visible,
        ))
        return value

    def string(self, label: str, size: int, *, ending: str = "cstring") -> str:
        value = self.blob(label, size, visible=True)
        field = self.fields[-1]
        text = value.rstrip(b"\0").decode("utf-8", "replace")
        self.fields[-1] = WwiseField(
            field.path, label, "string", field.offset, field.size, text,
            editable=True, visible=True,
        )
        if self.units is not None:
            self.units[-1].storage = ending
        return text

    def zstring(self, label: str) -> str:
        return self.string(label, self._zstring_size(self._path(label)), ending="zstring")

    def mark(self, name: str, offset: int | None = None) -> None:
        self.anchors[name] = self.pos if offset is None else offset

    def count(self, label: str, storage: str, *, scale: int = 1) -> int:
        unit = len(self.units) if self.units is not None else None
        value = (
            self.var(label, editable=False) if storage == "var"
            else self.scalar(label, storage, editable=False)
        )
        if value % scale:
            raise _LayoutError(f"{self._path(label)} is not a multiple of {scale}")
        self._pending = unit, scale
        return value // scale

    def each(self, count: int, root: str | None = "{}", *, section: bool = True,
             again: bool = False, editable: bool = True, ordered: bool = False):
        if count < 0 or count > 0x100000:
            raise _LayoutError(f"{self._path('')} has unreasonable count {count}")
        lane = None
        if self.lists is not None:
            if not again:
                unit, scale = self._pending or (None, 1)
                self._list = _List(unit, scale, editable and unit is not None)
                self.lists.append(self._list)
            lane = _Lane("/".join(self.parts), root, len(self.units), ordered)
            self._list.lanes.append(lane)
        self._pending = None
        for index in range(count):
            start = len(self.units) if lane else 0
            if section:
                self.parts.append(root.format(index + 1))
            yield index
            if section:
                self.parts.pop()
            if lane:
                lane.items.append((start, len(self.units)))
        if lane:
            lane.end = len(self.units)

    def deferred(self, label: str, storage: str):
        offset = self.pos
        value = self.scalar(label, storage, editable=False)
        return value, (offset, storage, len(self.fields) - 1)

    def resolve(self, token, actual: int, message: str) -> None:
        if self.fields[token[2]].value != actual:
            raise _LayoutError(message)

    def tail(self, label: str) -> None:
        if self.pos < len(self.data):
            self.blob(label, len(self.data) - self.pos)


class _Writer(_Reader):
    writing = True

    def __init__(
        self, data: bytes, units: list[_Unit], version: int, feedback: bool, values=None
    ):
        super().__init__(data, version, feedback)
        self.out = bytearray()
        self._units = units
        self._values = values or {}
        self._where: dict[str, list[int]] = {}
        for index, unit in enumerate(units):
            self._where.setdefault(unit.path, []).append(index)
        self._next = 0

    def _raw(self, path: str, storage: str, size: int, problem: str = "") -> bytes:
        positions = self._where.get(path, ())
        index = bisect.bisect_left(positions, self._next)
        if index < len(positions):
            self._next = positions[index] + 1
            raw = self._units[positions[index]].raw
        elif path in self._values:
            value = self._values[path]
            raw = (
                bytes(value) if storage == "bytes"
                else _pack_var(value) if storage == "var"
                else struct.pack("<" + self._FORMATS[storage], value)
            )
        else:
            raw = bytes(max(size, 0))
        self.out += raw
        self.pos = len(self.out)
        return raw

    def _var_size(self, path: str) -> int:
        return 1

    def _zstring_size(self, path: str) -> int:
        return 1

    def resolve(self, token, actual: int, message: str) -> None:
        offset, storage, index = token
        packed = struct.pack("<" + self._FORMATS[storage], actual)
        self.out[offset:offset + len(packed)] = packed
        self.fields[index] = replace(self.fields[index], value=actual)

    def tail(self, label: str) -> None:
        if self._where.get(self._path(label)):
            self.blob(label, 0)


def _items(r: _Reader, section: str, count: int, parser, *, again: bool = False) -> None:
    with r.section(section):
        for index in r.each(count, again=again):
            parser(index)


def _property_value_storage(r: _Reader, kind: str, prop_id: int) -> str:
    return "u32" if prop_id in integer_properties(kind, r.version) else "f32"


def _properties(r: _Reader, *, kind: str = "object", ranges: bool = True) -> None:
    names = property_names(kind, r.version)
    r.mark("property_bundle")
    with r.section("Properties"):
        count = r.count("Property count", "u8")
        ids = [r.scalar(
            f"Property {index + 1} type", "u8", enum=names,
        ) for index in r.each(count, "Property {} type", section=False)]
        for index in r.each(count, None, section=False, again=True):
            prop_id = ids[index]
            storage = _property_value_storage(r, kind, prop_id)
            references = (
                {0x53: "MIDI target", 0x55: "attenuation"}
                if r.version >= 150 else
                {0x38: "MIDI target", 0x39: "attached effect", 0x46: "attenuation"}
            ) if kind == "object" else {}
            id_kind = "hirc" if prop_id in references else ""
            role = references.get(prop_id, "")
            r.scalar(
                f"{names.get(prop_id, f'Property 0x{prop_id:02X}')} value",
                storage, id_kind=id_kind, reference_role=role,
            )
    if not ranges:
        return
    with r.section("Randomizers"):
        count = r.count("Randomizer count", "u8")
        ids = [r.scalar(
            f"Randomizer {index + 1} type", "u8", enum=names,
        ) for index in r.each(count, "Randomizer {} type", section=False)]
        for index in r.each(count, None, section=False, again=True):
            prop_id = ids[index]
            storage = _property_value_storage(r, kind, prop_id)
            label = names.get(prop_id, f"Property 0x{prop_id:02X}")
            r.scalar(f"{label} minimum", storage)
            r.scalar(f"{label} maximum", storage)


def _state_properties(r: _Reader, names: dict[int, str] | None = None) -> None:
    names = property_names("state", r.version) if names is None else names
    with r.section("State properties"):
        count = r.count("Property count", "var")
        for index in r.each(count):
            r.var("Property", enum=names, editable=True)
            r.scalar("Accumulation", "u8", enum=_ACCUM_125 if r.version <= 125 else _ACCUM)
            if r.version > 126:
                r.scalar("Stored in decibels", "u8", enum=_BOOL)
    with r.section("State groups"):
        count = r.count("Group count", "var")
        for index in r.each(count):
            r.scalar("State group", "u32", id_kind="state_group")
            r.scalar("Synchronization", "u8", enum=_SYNC)
            states = r.count("State count", "var")
            for state in r.each(states, "State {}"):
                r.scalar("State", "u32", id_kind="state_value")
                if r.version <= 145:
                    r.scalar(
                        "State object", "u32", id_kind="hirc",
                        reference_role="state object",
                    )
                    continue
                value_count = r.count("Property count", "u16")
                ids = [
                    r.scalar(f"Property {item + 1} type", "u16", enum=names)
                    for item in r.each(value_count, "Property {} type", section=False)
                ]
                for item in r.each(value_count, None, section=False, again=True):
                    label = names.get(ids[item], f"Property 0x{ids[item]:04X}")
                    r.scalar(f"{label} value", "f32")


def _graph(r: _Reader, count: int, section: str = "Points") -> None:
    def point(_index):
        r.scalar("From", "f32")
        r.scalar("To", "f32")
        r.scalar("Interpolation", "u32", enum=_CURVE)
    _items(r, section, count, point)


def _switch_graph(r: _Reader, count: int) -> None:
    def point(_index):
        r.scalar("From", "f32")
        r.scalar("Switch value", "u32", id_kind="switch_value")
        r.scalar("Interpolation", "u32", enum=_CURVE)
    _items(r, "Switch mapping", count, point)


def _typed_rtpc_input(r: _Reader, label: str = "Input ID") -> int:
    """Read an RTPC source and type its ID from the following discriminator."""

    field_index = len(r.fields)
    source_id = r.scalar(label, "u32")
    modern = r.version > 140
    source_type = r.scalar(
        "Input type", "u8", enum=_RTPC_TYPE_145 if modern else _RTPC_TYPE_140
    )
    id_kind, role = {
        0: ("game_parameter", ""),
        1: ("midi_parameter", ""),
        2: ("switch_group", "") if modern else ("hirc", "modulator"),
        3: ("state_group", ""),
        4: ("hirc", "modulator"),
    }.get(source_type, ("", ""))
    r.fields[field_index] = replace(
        r.fields[field_index], id_kind=id_kind, reference_role=role,
    )
    return source_id


def _rtpc(
    r: _Reader, *, modulator: bool = False,
    names: dict[int, str] | None = None,
) -> None:
    if names is None:
        names = property_names("modulator_rtpc" if modulator else "state", r.version)
    with r.section("RTPC curves"):
        count = r.count("Curve count", "u16")
        for index in r.each(count):
            _typed_rtpc_input(r)
            r.scalar("Accumulation", "u8", enum=_ACCUM_125 if r.version <= 125 else _ACCUM)
            r.var("Target property", enum=names, editable=True)
            r.scalar("Curve ID", "u32", id_kind="curve")
            r.scalar("Scaling", "u8", enum=_SCALING)
            _graph(r, r.count("Point count", "u16"))


def _effects(r: _Reader, *, node: bool) -> None:
    with r.section("Effects"):
        if node:
            r.scalar("Override parent effects", "u8", enum=_BOOL)
        count = r.count("Effect count", "u8")
        if count:
            if r.version <= 145:
                r.flags(
                    "Bypass flags",
                    (*((f"Bypass slot {slot}", slot) for slot in range(4)), ("Bypass all effects", 4)),
                )
            else:
                r.scalar("Bypass all effects", "u8", enum=_BOOL)
        for index in r.each(count):
            r.scalar("Slot", "u8")
            r.scalar("Effect", "u32", id_kind="hirc", reference_role="effect")
            if r.version <= 145:
                r.scalar("Shared effect", "u8", enum=_BOOL)
                r.scalar("Rendered effect", "u8", enum=_BOOL)
            else:
                flags = (("Bypassed", 0), ("Shared effect", 1))
                if node:
                    flags += (("Rendered effect", 2),)
                r.flags("Effect flags", flags)


def _metadata(r: _Reader, *, node: bool) -> None:
    """Read the metadata-effect slots introduced after Wwise 2019.1."""

    if r.version < 137:
        return
    with r.section("Metadata"):
        if node:
            r.scalar("Override parent metadata", "u8", enum=_BOOL)
        count = r.count("Metadata count", "u8")
        for index in r.each(count):
            r.scalar("Slot", "u8")
            r.scalar(
                "Effect", "u32", id_kind="hirc",
                reference_role="metadata effect",
            )
            r.scalar("Shared effect", "u8", enum=_BOOL)


def _positioning_129(r: _Reader) -> None:
    """Positioning before Wwise v130: 3D is its own bit and owns the attenuation ID."""

    flags = r.flags("Routing", (
        ("Override parent positioning", 0),
        ("Positioning type from RTPC", 1),
        ("2D positioning", 2),
        ("Enable panner", 3),
        ("3D positioning available", 4),
    ) if r.version <= 126 else (
        ("Override parent positioning", 0),
        ("3D positioning available", 4),
    ))
    if not (flags & 0x01 and flags & 0x10):
        return
    # Bit 4 (v126-) or 6 (v127-129) is the 3D position type; 1 has no automation path.
    automation_bit = 4 if r.version <= 126 else 6
    bits = r.flags("3D flags", (
        ("Spatialization", 0),
        ("Loop 3D automation path", 1),
        ("Update at each frame", 2),
        ("Follow listener orientation", 3),
        ("3D position type", automation_bit, 1, {0: "Automated path", 1: "Emitter"}),
    ) if r.version <= 126 else (
        ("Spatialization", 0, 2, _SPATIALIZATION),
        ("Hold emitter position and orientation", 3),
        ("Hold listener orientation", 4),
        ("Loop 3D automation path", 5),
        ("3D position type", automation_bit, 1, {0: "Automated path", 1: "Emitter"}),
    ))
    r.scalar("Attenuation", "u32", id_kind="hirc", reference_role="attenuation")
    if not (bits >> automation_bit) & 1:
        _automation_path(r)


def _automation_path(r: _Reader) -> None:
    r.scalar("Path mode", "u8", enum=_PATH_MODE)
    r.scalar("Transition time (ms)", "i32")
    vertex_count = r.count("Vertex count", "u32")
    def vertex(_index):
        r.scalar("X", "f32")
        r.scalar("Y", "f32")
        r.scalar("Z", "f32")
        r.scalar("Duration (ms)", "i32")
    _items(r, "Path vertices", vertex_count, vertex)
    item_count = r.count("Path count", "u32")
    def path(_index):
        r.scalar("First vertex", "u32")
        r.scalar("Vertex count", "u32")
    _items(r, "Paths", item_count, path)
    def ranges(_index):
        r.scalar("X random range", "f32")
        r.scalar("Y random range", "f32")
        r.scalar("Z random range", "f32")
    _items(r, "Path randomization", item_count, ranges, again=True)


def _positioning(r: _Reader) -> None:
    with r.section("Positioning"):
        if r.version <= 129:
            _positioning_129(r)
            return
        flags = r.flags("Routing", (
            ("Override parent positioning", 0),
            ("Listener-relative routing", 1),
            ("Speaker panner", 2, 2, _PANNER),
            ("3D position source", 5, 2, _POSITION_TYPE),
        ))
        if not (flags & 0x01 and flags & 0x02):
            return
        if r.version >= 135:
            r.flags("3D flags", (
                ("Spatialization", 0, 2, _SPATIALIZATION),
                ("Enable attenuation", 3),
                ("Hold emitter position and orientation", 4),
                ("Hold listener orientation", 5),
                ("Loop 3D automation path", 6),
                ("Enable diffraction", 7),
            ))
        else:
            r.flags("3D flags", (
                ("Spatialization", 0, 2, _SPATIALIZATION),
                ("Hold emitter position and orientation", 3),
                ("Hold listener orientation", 4),
                ("Loop 3D automation path", 5),
            ))
        if (flags >> 5) & 0x03:
            _automation_path(r)


def _aux(r: _Reader) -> None:
    with r.section("Auxiliary sends"):
        flags = r.flags("Aux flags", (
            ("Override game-defined sends", 0),
            ("Use game-defined sends", 1),
            ("Override user-defined sends", 2),
            ("Has user-defined sends", 3),
            ("Override reflections bus", 4),
        ))
        if flags & 0x08:
            for index in range(4):
                r.scalar(
                    f"User send {index + 1} bus", "u32", id_kind="hirc",
                    reference_role="auxiliary bus",
                )
        if r.version >= 135:
            r.scalar(
                "Reflections bus", "u32", id_kind="hirc",
                reference_role="reflections bus",
            )


def _advanced(r: _Reader) -> None:
    with r.section("Playback limits and virtualization"):
        r.flags("Limit flags", (
            ("When limit reached: kill newest", 0),
            ("Use virtual behavior", 1),
            ("Global playback limit", 2),
            ("Ignore parent instance limit", 3),
            ("Override virtual voice behavior", 4),
        ))
        r.scalar("Virtual queue behavior", "u8", enum=_VIRTUAL_QUEUE)
        r.scalar("Maximum instances", "u16")
        r.scalar("Below-threshold behavior", "u8", enum=_BELOW_THRESHOLD)
        r.flags("Analysis flags", (
            ("Override HDR envelope", 0),
            ("Override analysis", 1),
            ("Normalize loudness", 2),
            ("Enable envelope", 3),
        ))


def _node_base(r: _Reader) -> None:
    with r.section("Node"):
        _effects(r, node=True)
        _metadata(r, node=True)
        if r.version <= 145:
            r.scalar("Override attachment parameters", "u8", enum=_BOOL)
        r.scalar("Output bus", "u32", id_kind="hirc", reference_role="output bus")
        parent_offset = r.pos
        r.scalar("Parent", "u32", id_kind="hirc", reference_role="parent")
        r.mark("parent", parent_offset)
        r.flags("Priority and MIDI flags", (
            ("Override parent priority", 0),
            ("Apply distance to priority", 1),
            ("Override MIDI event behavior", 2),
            ("Override MIDI note tracking", 3),
            ("Enable MIDI note tracking", 4),
            ("Break MIDI loop on note-off", 5),
        ))
        _properties(r)
        _positioning(r)
        _aux(r)
        _advanced(r)
        _state_properties(r)
        _rtpc(r)
        _feedback(r)


def _feedback(r: _Reader) -> None:
    if r.version <= 126 and r.feedback:
        r.scalar("Feedback bus", "u32", id_kind="hirc", reference_role="feedback bus")


def _children(r: _Reader) -> None:
    with r.section("Children"):
        start = r.pos
        count = r.count("Child count", "u32")
        r.mark("child_count", start)
        for index in r.each(count, "Child {}", section=False, ordered=True):
            r.scalar(
                f"Child {index + 1}", "u32", id_kind="hirc",
                reference_role="child",
            )


def _source(r: _Reader, section: str = "Source") -> None:
    with r.section(section):
        plugin = r.scalar("Codec / source plug-in", "u32", id_kind="plugin")
        r.scalar("Storage type", "u8", enum={0: "Bank", 1: "Prefetch", 2: "Streaming"})
        r.scalar("Source ID", "u32", id_kind="source")
        r.scalar("In-memory media bytes", "u32")
        r.flags("Source flags", (
            ("Language-specific", 0), ("Prefetch", 1),
            ("Non-cacheable", 3), ("Has source", 7),
        ))
        if plugin & 0x0F in ((2, 5) if r.version <= 126 else (2,)):
            _sized_plugin_parameters(r, plugin)


def _music_node(r: _Reader) -> None:
    r.flags("Music inheritance flags", (
        ("Override parent MIDI tempo", 1),
        ("Override parent MIDI target", 2),
        ("MIDI target is a bus", 3),
    ))
    _node_base(r)
    _children(r)
    with r.section("Meter"):
        r.scalar("Grid period", "f64")
        r.scalar("Grid offset", "f64")
        r.scalar("Tempo", "f32")
        r.scalar("Beats per bar", "u8")
        r.scalar("Beat value", "u8")
        r.scalar("Override meter", "u8", enum=_BOOL)
    with r.section("Stingers"):
        count = r.count("Stinger count", "u32")
        for index in r.each(count):
            r.scalar("Trigger", "u32", id_kind="trigger")
            r.scalar("Segment", "u32", id_kind="hirc", reference_role="stinger segment")
            r.scalar("Synchronization", "u32", enum=_SYNC)
            r.scalar(
                "Cue-name filter", "u32", enum=_STANDARD_CUES,
                id_kind="cue",
            )
            r.scalar("Do-not-repeat time (ms)", "i32")
            r.scalar("Segment look-ahead count", "u32")


def _transitions(r: _Reader) -> None:
    with r.section("Transition rules"):
        count = r.count("Rule count", "u32")
        for index in r.each(count):
            sources = r.count("Source count", "u32")
            for item in r.each(sources, "Source {}", section=False, ordered=True):
                r.scalar(
                    f"Source {item + 1}", "u32", id_kind="hirc",
                    reference_role="transition source",
                )
            destinations = r.count("Destination count", "u32")
            for item in r.each(destinations, "Destination {}", section=False, ordered=True):
                r.scalar(
                    f"Destination {item + 1}", "u32", id_kind="hirc",
                    reference_role="transition destination",
                )
            with r.section("Source behavior"):
                r.scalar("Fade time (ms)", "i32")
                r.scalar("Fade curve", "u32", enum=_CURVE)
                r.scalar("Fade offset (ms)", "i32")
                r.scalar("Synchronization", "u32", enum=_SYNC)
                r.scalar(
                    "Cue-name filter", "u32", enum=_STANDARD_CUES,
                    id_kind="cue",
                )
                r.scalar("Play post-exit", "u8", enum=_BOOL)
            with r.section("Destination behavior"):
                r.scalar("Fade time (ms)", "i32")
                r.scalar("Fade curve", "u32", enum=_CURVE)
                r.scalar("Fade offset (ms)", "i32")
                r.scalar(
                    "Cue-name filter", "u32", enum=_STANDARD_CUES,
                    id_kind="cue",
                )
                r.scalar("Jump-to playlist item", "u32", id_kind="playlist_item")
                if r.version >= 133:
                    r.scalar("Jump-to selection", "u16", enum=_JUMP_TO)
                r.scalar("Entry type", "u16", enum=_ENTRY_TYPE)
                r.scalar("Play pre-entry", "u8", enum=_BOOL)
                r.scalar("Match source cue name", "u8", enum=_BOOL)
            has_object = r.scalar("Use transition segment", "u8", enum=_BOOL)
            if has_object:
                with r.section("Transition segment"):
                    r.scalar("Segment", "u32", id_kind="hirc", reference_role="transition segment")
                    for side in ("Fade in", "Fade out"):
                        with r.section(side):
                            r.scalar("Time (ms)", "i32")
                            r.scalar("Curve", "u32", enum=_CURVE)
                            r.scalar("Offset (ms)", "i32")
                    r.scalar("Play pre-entry", "u8", enum=_BOOL)
                    r.scalar("Play post-exit", "u8", enum=_BOOL)


def _state(r: _Reader) -> None:
    names = property_names("state", r.version)
    width = "u8" if r.version <= 126 else "u16"
    with r.section("State values"):
        count = r.count("Value count", width)
        ids = [
            r.scalar(f"Value {index + 1} property", width, enum=names)
            for index in r.each(count, "Value {} property", section=False)
        ]
        for index in r.each(count, None, section=False, again=True):
            r.scalar(f"{names.get(ids[index], f'Property 0x{ids[index]:04X}')} value", "f32")


def _action(r: _Reader) -> None:
    action_type = r.scalar("Action type", "u16")
    target_kind = action_type & 0xFF00
    if target_kind not in _ACTION_FAMILIES[r.version]:
        raise _LayoutError(f"Wwise v{r.version} cannot load action family 0x{target_kind:04X}")
    external_kind = {
        0x1200: "state_value",
        0x1300: "game_parameter",
        0x1400: "game_parameter",
        0x1900: "switch_value",
        (0x1B00 if r.version >= 150 else 0x1D00): "trigger",
    }.get(target_kind)
    is_event = target_kind == 0x2100
    r.scalar(
        "Target", "u32", id_kind=external_kind or ("event" if is_event else "hirc"),
        reference_role=("event target" if is_event else "action target")
        if external_kind is None else "",
    )
    r.flags("Target flags", (("Target is a bus", 0),))
    _properties(r)

    def exceptions():
        with r.section("Exceptions"):
            count = r.count("Exception count", "var")
            for index in r.each(count):
                r.scalar("Object", "u32", id_kind="hirc", reference_role="exception")
                r.scalar("Object is a bus", "u8", enum=_BOOL)

    if target_kind == 0x0400:
        r.flags("Play fade", (("Fade curve", 0, 5, _CURVE),))
        r.scalar("Required bank", "u32", id_kind="bank")
        if r.version >= 144:
            r.scalar("Required bank type", "u32", enum=_BANK_TYPE)
    elif target_kind in {0x0100, 0x0200, 0x0300, 0x2200}:
        r.flags("Fade", (("Fade curve", 0, 5, _CURVE),))
        if target_kind != 0x2200:
            labels = (
                (("Apply to state transitions", 1), ("Apply to dynamic sequences", 2))
                if target_kind == 0x0100 else
                (("Include pending resume", 0), ("Apply to state transitions", 1), ("Apply to dynamic sequences", 2))
                if target_kind == 0x0200 else
                (("Master resume", 0), ("Apply to state transitions", 1), ("Apply to dynamic sequences", 2))
            )
            r.flags("Scope", labels)
        exceptions()
    elif target_kind in {0x0600, 0x0700}:  # Mute / Unmute have no value payload.
        r.flags("Fade", (("Fade curve", 0, 5, _CURVE),))
        exceptions()
    elif target_kind in {
        0x0800, 0x0900, 0x0A00, 0x0B00,
        0x0C00, 0x0D00, 0x0E00, 0x0F00, 0x2000, 0x3000,
    }:
        r.flags("Fade", (("Fade curve", 0, 5, _CURVE),))
        r.scalar("Value meaning", "u8", enum=_VALUE_MEANING)
        r.scalar("Value", "f32")
        r.scalar("Random minimum", "f32")
        r.scalar("Random maximum", "f32")
        exceptions()
    elif target_kind in {0x1300, 0x1400}:
        r.flags("Fade", (("Fade curve", 0, 5, _CURVE),))
        r.scalar("Bypass transition", "u8", enum=_BOOL)
        r.scalar("Value meaning", "u8", enum=_VALUE_MEANING)
        r.scalar("Value", "f32")
        r.scalar("Random minimum", "f32")
        r.scalar("Random maximum", "f32")
        exceptions()
    elif target_kind in {0x1200, 0x1900}:
        prefix = "State" if target_kind == 0x1200 else "Switch"
        r.scalar(f"{prefix} group", "u32", id_kind=f"{prefix.lower()}_group")
        r.scalar(prefix, "u32", id_kind=f"{prefix.lower()}_value")
    elif target_kind in (
        {0x1000, 0x1100, 0x1A00, 0x1B00, 0x1F00, 0x2100}
        if r.version >= 150 else
        {0x1000, 0x1100, 0x1C00, 0x1D00, 0x1F00, 0x2100}
    ):
        pass
    elif target_kind in (
        {0x3300, 0x3400, 0x3500, 0x3600, 0x3700}
        if r.version >= 150 else {0x1A00, 0x1B00}
    ):
        r.scalar("Bypass", "u8", enum=_BOOL)
        r.scalar(
            "Effect slot" if r.version >= 150 else "Target effect mask", "u8"
        )
        exceptions()
    elif target_kind == 0x1E00:
        r.scalar("Relative to duration", "u8", enum=_BOOL)
        r.scalar("Seek value", "f32")
        r.scalar("Random minimum", "f32")
        r.scalar("Random maximum", "f32")
        r.scalar("Snap to nearest marker", "u8", enum=_BOOL)
        exceptions()
    elif target_kind in {0x3100, 0x3200}:
        r.scalar("Target is an audio-device element", "u8", enum=_BOOL)
        r.scalar("Effect slot", "u8")
        r.scalar("Effect", "u32", id_kind="hirc", reference_role="effect")
        r.scalar("Shared effect", "u8", enum=_BOOL)
        exceptions()
    else:
        raise _LayoutError(f"unsupported action family 0x{target_kind:04X}")


def _event(r: _Reader) -> None:
    with r.section("Actions"):
        count = r.count("Action count", "var")
        for index in r.each(count, "Action {}", section=False):
            r.scalar(
                f"Action {index + 1}", "u32", id_kind="hirc",
                reference_role="event action",
            )


def _random_container(r: _Reader) -> None:
    _node_base(r)
    with r.section("Playlist behavior"):
        r.scalar("Loop count", "u16")
        r.scalar("Loop random minimum", "u16")
        r.scalar("Loop random maximum", "u16")
        r.scalar("Transition time (ms)", "f32")
        r.scalar("Transition random minimum (ms)", "f32")
        r.scalar("Transition random maximum (ms)", "f32")
        r.scalar("Avoid-repeat count", "u16")
        r.scalar("Transition mode", "u8", enum=_TRANSITION_MODE)
        r.scalar("Random mode", "u8", enum={0: "Normal", 1: "Shuffle"})
        r.scalar("Container mode", "u8", enum={0: "Random", 1: "Sequence"})
        r.flags("Playlist flags", (
            ("Use weights", 0), ("Reset playlist on play", 1),
            ("Restart backwards", 2), ("Continuous", 3), ("Global playlist", 4),
        ))
    _children(r)
    with r.section("Playlist"):
        count = r.count("Item count", "u16")
        for index in r.each(count):
            r.scalar("Object", "u32", id_kind="hirc", reference_role="playlist item")
            r.scalar("Weight", "i32")


def _switch_container(r: _Reader) -> None:
    _node_base(r)
    group_type = r.scalar("Group type", "u8", enum=_GROUP)
    category = "switch" if group_type == 0 else "state"
    r.scalar("Group", "u32", id_kind=f"{category}_group")
    r.scalar("Default value", "u32", id_kind=f"{category}_value")
    r.scalar("Continuous validation", "u8", enum=_BOOL)
    _children(r)
    with r.section("Value assignments"):
        count = r.count("Assignment count", "u32")
        for index in r.each(count):
            r.scalar("Value", "u32", id_kind=f"{category}_value")
            nodes = r.count("Object count", "u32")
            for item in r.each(nodes, "Object {}", section=False):
                r.scalar(
                    f"Object {item + 1}", "u32", id_kind="hirc",
                    reference_role="switch assignment",
                )
    with r.section("Switch transitions"):
        count = r.count("Transition count", "u32")
        for index in r.each(count):
            r.scalar("Object", "u32", id_kind="hirc", reference_role="switch child")
            r.flags("Playback", (("Play first only", 0), ("Continue playback", 1)))
            r.flags("Mode", (("On switch", 0, 3, {0: "Play to end", 1: "Stop"}),))
            r.scalar("Fade-out time (ms)", "i32")
            r.scalar("Fade-in time (ms)", "i32")


def _layer_container(r: _Reader) -> None:
    _node_base(r)
    _children(r)
    with r.section("Layers"):
        count = r.count("Layer count", "u32")
        for index in r.each(count):
            # CAkLayer is serialized inline inside the container.  ulLayerID
            # identifies that inline record; it is not a top-level HIRC ID.
            r.scalar("Layer ID", "u32", id_kind="layer")
            _rtpc(r)
            _typed_rtpc_input(r, "Crossfade input")
            associations = r.count("Child curve count", "u32")
            for item in r.each(associations, "Child curve {}"):
                r.scalar("Child", "u32", id_kind="hirc", reference_role="layer child")
                _graph(r, r.count("Point count", "u32"))
    r.scalar("Continuous validation", "u8", enum=_BOOL)


def _sized_string(
    r: _Reader, size_label: str, label: str, storage: str = "u32", ending: str = "cstring"
) -> None:
    size, token = r.deferred(size_label, storage)
    start = r.pos
    r.string(label, size, ending=ending)
    r.resolve(token, r.pos - start, f"{label} does not match {size_label}")


def _music_segment(r: _Reader) -> None:
    _music_node(r)
    r.scalar("Duration (ms)", "f64")
    with r.section("Cues"):
        count = r.count("Cue count", "u32")
        for index in r.each(count):
            r.scalar("Cue ID", "u32", enum={**_STANDARD_CUES, 0: "Unnamed custom cue"}, id_kind="cue")
            r.scalar("Position (ms)", "f64")
            if r.version >= 137:
                r.zstring("Name")
            else:
                _sized_string(r, "Name bytes", "Name")


def _music_track(r: _Reader) -> None:
    r.flags("Music inheritance flags", (
        ("Override parent MIDI tempo", 1),
        ("Override parent MIDI target", 2),
        ("MIDI target is a bus", 3),
    ))
    sources = r.count("Source count", "u32")
    for index in r.each(sources, "Source {}", section=False):
        _source(r, f"Source {index + 1}")
    with r.section("Clips"):
        count = r.count("Clip count", "u32")
        for index in r.each(count):
            r.scalar("Subtrack ID", "u32")
            r.scalar("Source", "u32", id_kind="source")
            if r.version >= 133:
                r.scalar(
                    "Event", "u32", id_kind="event",
                    reference_role="clip event",
                )
            r.scalar("Play at (ms)", "f64")
            r.scalar("Begin trim (ms)", "f64")
            r.scalar("End trim (ms)", "f64")
            r.scalar("Source duration (ms)", "f64")
        if count:
            r.scalar("Subtrack count", "u32")
    with r.section("Clip automation"):
        count = r.count("Automation count", "u32")
        for index in r.each(count):
            r.scalar("Clip index", "u32")
            r.scalar("Automation type", "u32", enum=_CLIP_AUTOMATION)
            _graph(r, r.count("Point count", "u32"))
    _node_base(r)
    track_type = r.scalar("Track type", "u8", enum=_TRACK_TYPE)
    if track_type == 3:
        group_type = r.scalar("Switch group type", "u8", enum=_GROUP)
        category = "switch" if group_type == 0 else "state"
        r.scalar("Switch group", "u32", id_kind=f"{category}_group")
        r.scalar("Default switch", "u32", id_kind=f"{category}_value")
        count = r.count("Switch association count", "u32")
        for index in r.each(count, "Switch association {}", section=False):
            r.scalar(f"Switch association {index + 1}", "u32", id_kind=f"{category}_value")
        with r.section("Track transition"):
            r.scalar("Source fade time (ms)", "i32")
            r.scalar("Source fade curve", "u32", enum=_CURVE)
            r.scalar("Source fade offset (ms)", "i32")
            r.scalar("Synchronization", "u32", enum=_SYNC)
            r.scalar(
                "Cue-name filter", "u32", enum=_STANDARD_CUES,
                id_kind="cue",
            )
            r.scalar("Destination fade time (ms)", "i32")
            r.scalar("Destination fade curve", "u32", enum=_CURVE)
            r.scalar("Destination fade offset (ms)", "i32")
    r.scalar("Look-ahead time (ms)", "i32")


def _music_switch(r: _Reader) -> None:
    _music_node(r)
    _transitions(r)
    r.scalar("Continue playback", "u8", enum=_BOOL)
    _decision_tree(r)


def _dialogue_event(r: _Reader) -> None:
    r.scalar("Probability (%)", "u8")
    _decision_tree(r, "dialogue leaf")
    _properties(r)


def _decision_tree(r: _Reader, leaf_role: str = "decision leaf") -> None:
    depth = r.count("Decision depth", "u32")
    group_fields = []
    with r.section("Decision arguments"):
        for index in r.each(depth, "Group {}", section=False, editable=False):
            group_fields.append(len(r.fields))
            r.scalar(f"Group {index + 1}", "u32")
        group_types = []
        for index in r.each(depth, "Group {} type", section=False, again=True):
            group_type = r.scalar(f"Group {index + 1} type", "u8", enum=_GROUP)
            group_types.append(group_type)
            category = "switch" if group_type == 0 else "state"
            field_index = group_fields[index]
            r.fields[field_index] = replace(
                r.fields[field_index], id_kind=f"{category}_group",
            )
    count = r.count("Decision-tree bytes", "u32", scale=12)
    r.scalar("Decision mode", "u8", enum=_DECISION_MODE)
    with r.section("Decision tree"):
        rows = []
        for index in r.each(count, editable=False):
            key_field = len(r.fields)
            r.scalar("Value", "u32", editable=False)
            branch_field = len(r.fields)
            branch = r.scalar("Branch or audio object", "u32", editable=False)
            r.scalar("Weight", "u16")
            r.scalar("Probability", "u16")
            rows.append((key_field, branch_field, branch))
        # Classify the packed branch union by depth, as Wwise walks it: nodes at the last level
        # are leaves; the root and inner nodes are branches (a depth-0 root packs first child 1, count 0).
        levels: dict[int, int] = {}
        kinds: dict[int, bool] = {}
        pending = [(0, 0)] if rows else []
        while pending:
            index, level = pending.pop()
            if index >= count or index in kinds:
                raise _LayoutError(f"decision tree node {index + 1} is outside the tree or reached twice")
            levels[index] = level
            kinds[index] = leaf = index > 0 and level == depth
            if not leaf:
                branch = rows[index][2]
                pending.extend(((branch & 0xFFFF) + item, level + 1) for item in range(branch >> 16))
        if len(kinds) != count:
            raise _LayoutError("decision tree has nodes no branch reaches")
        branch_fields = {branch_field for _key, branch_field, _branch in rows}
        replacements = []
        for index, (key_field, branch_field, branch) in enumerate(rows):
            base = f"Decision tree/{index + 1}"
            offset = r.fields[branch_field].offset
            level = levels.get(index, 0)
            if 0 < level <= len(group_types):
                category = "switch" if group_types[level - 1] == 0 else "state"
                r.fields[key_field] = replace(
                    r.fields[key_field], id_kind=f"{category}_value",
                )
            if kinds.get(index, True):
                replacements.append(WwiseField(
                    f"{base}/Audio object", "Audio object", "u32", offset, 4,
                    branch, (), "hirc", leaf_role,
                ))
            else:
                replacements.extend((
                    WwiseField(f"{base}/First child", "First child", "bit", offset, 4,
                               branch & 0xFFFF, (), editable=False, mask=0xFFFF),
                    WwiseField(f"{base}/Child count", "Child count", "bit", offset, 4,
                               branch >> 16, (), editable=False, mask=0xFFFF0000, shift=16),
                ))
        r.fields = [
            field for item, field in enumerate(r.fields) if item not in branch_fields
        ] + replacements


def _playlist_node(r: _Reader, nodes: list[int], total: int) -> None:
    nodes[0] += 1
    if nodes[0] > total and not r.writing:
        raise _LayoutError("music playlist contains more nodes than declared")
    r.scalar("Segment", "u32", id_kind="hirc", reference_role="playlist segment")
    r.scalar("Playlist item ID", "u32", id_kind="playlist_item")
    children = r.count("Child count", "u32")
    r.scalar("Selection type", "u32", enum=_RANDOM_SEQUENCE_TYPE)
    r.scalar("Loop count", "i16")
    r.scalar("Loop random minimum", "i16")
    r.scalar("Loop random maximum", "i16")
    r.scalar("Weight", "u32")
    r.scalar("Avoid-repeat count", "u16")
    r.scalar("Use weight", "u8", enum=_BOOL)
    r.scalar("Shuffle", "u8", enum=_BOOL)
    for _ in r.each(children):
        _playlist_node(r, nodes, total)


def _music_random(r: _Reader) -> None:
    _music_node(r)
    _transitions(r)
    total, token = r.deferred("Playlist node count", "u32")
    nodes = [0]
    with r.section("Music playlist"):
        if total:
            _playlist_node(r, nodes, total)
    r.resolve(token, nodes[0], f"music playlist left {total - nodes[0]} declared nodes unread")


def _attenuation(r: _Reader) -> None:
    if r.version >= 137:
        r.scalar("Height spread enabled", "u8", enum=_BOOL)
    enabled = r.scalar("Directional cone enabled", "u8", enum=_BOOL)
    if enabled & 1:
        with r.section("Directional cone"):
            r.scalar("Inside angle", "f32")
            r.scalar("Outside angle", "f32")
            r.scalar("Outside volume", "f32")
            r.scalar("Outside LPF", "f32")
            r.scalar("Outside HPF", "f32")
    with r.section("Curve assignments"):
        for name in attenuation_targets(r.version):
            r.scalar(name, "i8")
    with r.section("Attenuation curves"):
        count = r.count("Curve count", "u8")
        for index in r.each(count):
            r.scalar("Scaling", "u8", enum=_SCALING)
            _graph(r, r.count("Point count", "u16"))
    _rtpc(r)


def _reflect_parameters(r: _Reader, size: int) -> None:
    """Wwise Reflect parameter block used by Wwise 2017.1 through 2023.1.

    The scalar order and curve bundle are the exact GetBankParameters layouts from
    AkReflect.dll. Authoring-only properties are not serialized.
    """

    start = r.pos
    if r.version >= 145:
        scalars = [
            ("Speed of sound", "f32", None),
            ("Center percentage", "f32", None),
            ("Maximum reflections", "f32", None),
            ("Dry level", "f32", None),
            ("Wet level", "f32", None),
            ("Maximum distance", "f32", None),
            ("Base texture frequency", "f32", None),
            ("Curve usage mask", "u32", None),
            ("Distance smoothing", "f32", None),
            ("Smoothing type", "u32", _REFLECT_SMOOTHING),
            ("Pitch threshold", "f32", None),
            ("Distance threshold", "f32", None),
            ("Threshold mode", "u32", _REFLECT_THRESHOLD),
            ("Distance warping", "f32", None),
            ("Diffraction warping", "f32", None),
            ("Output channel configuration", "u32", _REFLECT_OUTPUT),
        ]
        if r.version >= 150:
            scalars.extend((
                ("Fusing time", "f32", None),
                ("Decorrelation strength", "f32", None),
                ("Decorrelation algorithm", "u32", _REFLECT_DECORRELATION_ALGORITHM),
                ("Decorrelation strength source", "u32", _REFLECT_DECORRELATION_SOURCE),
                ("Decorrelation maximum reflection order", "u32", None),
                ("Stereo decorrelation", "bool", _BOOL),
                ("Decorrelation window width", "u32", None),
                ("Hardware acceleration", "bool", _BOOL),
            ))
    else:
        scalars = (
            ("Speed of sound", "f32", None),
            ("Center ratio", "f32", None),
            ("Maximum reflections", "f32", None),
            ("Dry level", "f32", None),
            ("Output level", "f32", None),
            ("Maximum distance", "f32", None),
            ("Base texture frequency", "f32", None),
            ("Fade-out frame count", "u32", None),
            ("Distance smoothing", "f32", None),
            ("Smoothing type", "u32", _REFLECT_SMOOTHING),
            ("Pitch threshold", "f32", None),
            ("Distance threshold", "f32", None),
            ("Threshold mode", "u32", _REFLECT_THRESHOLD),
            ("Output channel configuration", "u32", _REFLECT_OUTPUT),
        )[:8 if r.version <= 125 else 13 if r.version <= 132 else 14]
    for label, storage, enum in scalars:
        r.scalar(label, storage, enum=enum)

    count = r.count("Curve count", "u16")
    with r.section("Curves"):
        for index in r.each(count):
            r.scalar("Curve", "u32", enum=_REFLECT_CURVES_145 if r.version >= 145 else _REFLECT_CURVES)
            _graph(r, r.count("Point count", "u16"))
    path = r._path("Phasing suppression")
    if r.version == 145 and (r._where.get(path) if r.writing else size - (r.pos - start) == 4):
        r.scalar("Phasing suppression", "f32")


def _matrix_reverb_parameters(r: _Reader, _size: int) -> None:
    """Parse Matrix Reverb's fixed values and optional authored delay array."""

    r.scalar("Reverb time", "f32")
    r.scalar("High-frequency ratio", "f32")
    count = r.scalar(
        "Number of delays", "u32", enum=BNK_FX_ENUMS["matrix_delays"]
    )
    r.scalar("Dry level", "f32")
    r.scalar("Wet level", "f32")
    r.scalar("Pre-delay", "f32")
    r.scalar("Process LFE", "bool", enum=_BOOL)
    mode = r.scalar(
        "Delay-length mode", "u32", enum=BNK_FX_ENUMS["matrix_mode"]
    )
    if mode == 1:
        if count not in BNK_FX_ENUMS["matrix_delays"]:
            raise _LayoutError(f"Matrix Reverb has invalid delay count {count}")
        with r.section("Custom delay lengths"):
            for index in range(count):
                r.scalar(f"Delay {index + 1}", "f32")


def _futzbox_parameters(r: _Reader, size: int) -> None:
    """Parse the v139 and v159 McDSP FutzBox parameter blocks."""

    fields = BNK_FX_SCHEMAS[0x006E1003][1]
    if size == 139:
        fields = BNK_FUTZBOX_139_FIELDS
    elif size != 159:
        raise _LayoutError(f"unsupported McDSP FutzBox parameter size {size}")
    _schema_parameters(r, fields)


def _soundseed_woosh_parameters(r: _Reader, _size: int) -> None:
    """SoundSeed Air Woosh v2 block used by Wwise 2021.1."""

    # The plug-in serializes its general settings before the four automatable
    # value/random/enable triplets. Duration is deliberately Real32 here even
    # though the authoring XML exposes it as Real64.
    for label, storage, enum in (
        ("Duration", "f32", None),
        ("Duration random", "f32", None),
        ("Channels", "u16", _WOOSH_CHANNELS),
        ("Minimum distance", "f32", None),
        ("Roll-off factor", "f32", None),
        ("Dynamic range", "f32", None),
        ("Playback rate", "f32", None),
        ("Noise color", "u16", _WOOSH_NOISE),
        ("Point time random", "f32", None),
        ("Point speed random", "f32", None),
        ("Distance attenuation enabled", "bool", _BOOL),
        ("Oversampling", "u16", None),
    ):
        r.scalar(label, storage, enum=enum)

    for name in ("Object speed", "Frequency shift", "Q factor shift", "Gain offset"):
        with r.section(name):
            r.scalar("Value", "f32")
            r.scalar("Random", "f32")
            r.scalar("Automate", "bool", enum=_BOOL)

    anchor_field = len(r.fields)
    anchor = r.scalar("Anchor deflector", "i16")
    count = r.count("Deflector count", "u16")
    if anchor < -1 or anchor >= count:
        raise _LayoutError(f"SoundSeed Woosh has invalid anchor deflector {anchor}")
    r.fields[anchor_field] = replace(
        r.fields[anchor_field],
        enum=((-1, "None"), *((index, f"Deflector {index + 1}") for index in range(count))),
    )
    with r.section("Deflectors"):
        for index in r.each(count):
            r.scalar("Frequency", "f32")
            r.scalar("Q factor", "f32")
            r.scalar("Gain", "f32")
            # Anchor is stored once as the zero-based deflector index.
            if index == anchor:
                r.fields.append(WwiseField(
                    r._path("Anchor"), "Anchor", "derived", r.pos, 0,
                    1, tuple(_BOOL.items()), editable=False,
                ))

    count = r.count("Automation curve count", "u16")
    with r.section("Automation curves"):
        for index in r.each(count):
            r.scalar("Property", "u32", enum=_WOOSH_CURVES)
            _graph(r, r.count("Point count", "u16"))

    # The final bundle mirrors the authored Path2D: LinearTime followed by each
    # point's X position, Y position, and time coordinate.
    count = r.count("Path point count", "u16")
    r.scalar("Linear path timing", "f32", enum=_BOOL)
    with r.section("Path points"):
        for index in r.each(count):
            r.scalar("X", "f32")
            r.scalar("Y", "f32")
            r.scalar("Time", "f32")


def _soundseed_wind_parameters(r: _Reader, _size: int) -> None:
    """SoundSeed Air Wind block emitted by Wwise 2022.1.

    The authoring plug-in writes Max Distance and its inner deflectors after
    the ordinary AudioEnginePropertyID values. Deflector pan positions are
    compiled into a distance and direction; only the runtime values are stored.
    """

    for label, storage, enum in (
        ("Duration", "f32", None), ("Duration random", "f32", None),
        ("Channels", "u16", _WOOSH_CHANNELS),
        ("Minimum distance", "f32", None),
        ("Roll-off factor", "f32", None),
        ("Dynamic range", "f32", None),
        ("Playback rate", "f32", None),
    ):
        r.scalar(label, storage, enum=enum)

    for name in (
        "Wind speed", "Direction", "Variability", "Gustiness",
        "Frequency shift", "Q factor shift", "Gain offset",
    ):
        with r.section(name):
            r.scalar("Value", "f32")
            r.scalar("Random", "f32")
            r.scalar("Automate", "bool", enum=_BOOL)

    count = r.count("Deflector count", "u16")
    r.scalar("Maximum distance", "f32")
    with r.section("Deflectors"):
        for index in r.each(count):
            r.scalar("Distance", "f32")
            r.scalar("Direction", "f32")
            r.scalar("Frequency", "f32")
            r.scalar("Q factor", "f32")
            r.scalar("Gain", "f32")

    count = r.count("Automation curve count", "u16")
    with r.section("Automation curves"):
        for index in r.each(count):
            r.scalar("Property", "u32", enum=_WIND_CURVES)
            _graph(r, r.count("Point count", "u16"))


def _schema_parameters(r: _Reader, fields) -> None:
    for label, storage, *enum_name in fields:
        choices = BNK_FX_ENUMS.get(enum_name[0]) if enum_name else None
        enum = (
            choices if isinstance(choices, dict)
            else dict(enumerate(choices)) if choices else _BOOL if storage == "bool" else None
        )
        r.scalar(
            label, storage, enum=enum,
            id_kind="game_parameter" if label == "Game Parameter ShortID" else "",
        )


def _convolution_reverb_parameters(r: _Reader, size: int) -> None:
    _schema_parameters(r, BNK_FX_SCHEMAS[0x007F0003][1][:None if size >= 56 else -1])
    if size >= 57:
        r.scalar("Block size", "u8", enum=dict(enumerate(
            BNK_FX_ENUMS["convolution_block_size"]
        )))


def _meter_parameters(r: _Reader, size: int) -> None:
    for label in ("Attack", "Release", "Minimum", "Maximum", "Hold"):
        r.scalar(label, "f32")
    if size >= 28:
        r.scalar("Infinite hold", "bool", enum=_BOOL)
    r.scalar("Mode", "u8", enum=dict(enumerate(BNK_FX_ENUMS["meter_mode"])))
    r.scalar("Scope", "u8", enum=dict(enumerate(BNK_FX_ENUMS["meter_scope"])))
    r.scalar("Apply downstream volume", "bool", enum=_BOOL)
    r.scalar("Game Parameter ShortID", "u32", id_kind="game_parameter")


def _rev_parameters(r: _Reader, size: int) -> None:
    if size != 212:
        r.blob("Bytes", size, visible=True, editable=True)
        return

    def block(words):
        r.scalar("Version", "u16", editable=False)
        r.scalar("Size", "u16", editable=False)
        for number, kind in enumerate(words, 1):
            r.scalar(f"Value {number}", "u32" if kind == "i" else "f32")

    for label in ("Gain", "RPM", "Throttle"):
        r.scalar(label, "f32")
    r.scalar("Gear", "u32")
    r.scalar("Velocity", "f32")
    r.scalar("Enable shifting", "u32", enum=_BOOL)
    with r.section("Property 6 block"):
        block("fffffiffffffffff")
    with r.section("Property 7 block"):
        block("ffifiiifif")
        for index in (1, 2):
            with r.section(f"Sub-block {index}"):
                block("ifffffii")
    r.scalar("Last value", "u32")


def _sized_plugin_parameters(r: _Reader, plugin: int) -> None:
    size, token = r.deferred("Plug-in parameter bytes", "u32")
    start = r.pos
    custom = {
        0x006E1003: _futzbox_parameters,
        0x00730003: _matrix_reverb_parameters,
        0x00770002: _soundseed_wind_parameters,
        0x00780002: _soundseed_woosh_parameters,
        0x007F0003: _convolution_reverb_parameters,
        0x00810003: _meter_parameters,
        0x00AB0003: _reflect_parameters,
        0x01A01052: _rev_parameters,
    }.get(plugin)
    schema = BNK_FX_SCHEMAS.get(plugin)
    if plugin == 0x00820003 and size == 16:
        schema = (schema[0], BNK_TIME_STRETCH_16_FIELDS)
    if (custom or schema) and size:
        with r.section("Plug-in parameters"):
            if custom:
                custom(r, size)
            else:
                _schema_parameters(r, schema[1])
    elif size:
        r.blob("Plug-in parameters", size, visible=True, editable=True)
    r.resolve(
        token, r.pos - start,
        f"plug-in 0x{plugin:08X} layout used {r.pos - start} of {size} bytes",
    )


def _fx(r: _Reader) -> None:
    plugin = r.scalar("Plug-in", "u32", id_kind="plugin")
    names = fx_property_names(plugin, r.version) or {}
    _sized_plugin_parameters(r, plugin)
    with r.section("Plug-in media"):
        count = r.count("Media count", "u8")
        for index in r.each(count):
            r.scalar("Slot", "u8")
            r.scalar("Source ID", "u32", id_kind="source")
    _rtpc(r, names=names)
    if r.version <= 126:
        r.scalar("Reserved 1", "u8", editable=False, visible=False)
        r.scalar("Reserved 2", "u8", editable=False, visible=False)
        with r.section("RTPC initial values"):
            count = r.count("Value count", "u16")
            for index in r.each(count):
                r.var("Property", enum=names, editable=True)
                r.scalar("Value", "f32")
        return
    _state_properties(r, names=names)
    with r.section("Plug-in property values"):
        count = r.count("Value count", "u16")
        for index in r.each(count):
            r.var("Property", enum=names, editable=True)
            r.scalar("Accumulation", "u8", enum=_ACCUM_125 if r.version <= 125 else _ACCUM)
            r.scalar("Value", "f32")


def _bus(r: _Reader) -> None:
    output = r.scalar("Output bus", "u32", id_kind="hirc", reference_role="output bus")
    if not output and r.version > 126:
        r.scalar("Audio device", "u32", id_kind="hirc", reference_role="audio device")
    _properties(r, ranges=False)
    _positioning(r)
    _aux(r)
    with r.section("Bus playback"):
        r.flags("Limit flags", (
            ("When limit reached: kill newest", 0),
            ("Use virtual behavior", 1),
            ("Ignore parent instance limit", 2),
            ("Background music", 3),
        ))
        r.scalar("Maximum instances", "u16")
        r.flags("Channel configuration", (
            ("Channel count", 0, 8, None),
            ("Configuration type", 8, 4, {
                0: "Anonymous", 1: "Standard", 2: "Ambisonic",
                3: "Objects", 14: "Device main", 15: "Device passthrough",
            }),
            ("Channel mask", 12, 20, None),
        ), storage="u32")
        r.flags("HDR flags", (("HDR bus", 0), ("Exponential HDR release", 1)))
    r.scalar("Recovery time (ms)", "i32")
    r.scalar("Maximum duck volume", "f32")
    with r.section("Ducking"):
        count = r.count("Duck count", "u32")
        for index in r.each(count):
            r.scalar("Bus", "u32", id_kind="hirc", reference_role="ducked bus")
            r.scalar("Volume", "f32")
            r.scalar("Fade-out time (ms)", "i32")
            r.scalar("Fade-in time (ms)", "i32")
            r.scalar("Fade curve", "u8", enum=_CURVE)
            r.scalar("Target property", "u8", enum=property_names("object", r.version))
    _effects(r, node=False)
    if r.version <= 145:
        r.scalar("Mixer plug-in", "u32", id_kind="hirc", reference_role="effect")
        r.scalar("Mixer plug-in is a ShareSet", "u8", enum=_BOOL)
        r.scalar("Override attachment parameters", "u8", enum=_BOOL)
    _metadata(r, node=False)
    _rtpc(r)
    _state_properties(r)
    _feedback(r)


def _envelope(r: _Reader) -> None:
    _properties(r, kind="modulator")
    _rtpc(r, modulator=True)


def _audio_device(r: _Reader) -> None:
    _fx(r)
    if r.version >= 137:
        _effects(r, node=False)


def _chunk_bkhd(r: _Reader) -> None:
    r.scalar("Bank generator version", "u32", editable=False)
    r.scalar("SoundBank ID", "u32", id_kind="bank")
    r.scalar("Language ID", "u32", id_kind="language")
    if r.version <= 126:
        r.scalar("Feedback in bank", "u32", enum=_BOOL, editable=False)
    else:
        if r.version >= 135:
            r.scalar("Media alignment", "u16")
        else:
            r.scalar("Unused alternate value", "u16", editable=False, visible=False)
        r.scalar("Device allocated", "u16", enum=_BOOL)
    r.scalar("Project ID", "u32", id_kind="project")
    if r.version >= 142:
        r.scalar("SoundBank type", "u32", enum=_BANK_TYPE)
        r.blob("SoundBank hash", 16, visible=True, editable=True)
    r.tail("Alignment padding")


def _chunk_didx(r: _Reader) -> None:
    if len(r.data) % 12:
        raise _LayoutError("DIDX size is not a multiple of its 12-byte entry")
    count = len(r.data) // 12
    with r.section("Embedded media index"):
        for index in range(count):
            with r.section(f"{index + 1}"):
                r.scalar("Source ID", "u32", id_kind="source", editable=False)
                r.scalar("DATA offset", "u32", editable=False)
                r.scalar("Media bytes", "u32", editable=False)


def _chunk_init(r: _Reader) -> None:
    count = r.count("Plug-in count", "u32")
    with r.section("Registered plug-ins"):
        for index in r.each(count):
            plugin = r.scalar("Plug-in ID", "u32", id_kind="plugin")
            field = r.fields[-1]
            for label, value, enum in (
                ("Type", plugin & 0x0F, _PLUGIN_TYPE),
                ("Company ID", (plugin >> 4) & 0x03FF, None),
                ("Plug-in number", plugin >> 16, None),
            ):
                r.fields.append(WwiseField(
                    f"{field.path}/{label}", label, "derived", field.offset,
                    field.size, value, r._enum(enum), editable=False,
                ))
            if r.version >= 137:
                r.zstring("Library name")
            else:
                _sized_string(r, "Library name bytes", "Library name")


def _chunk_stmg(r: _Reader) -> None:
    if r.version >= 141:
        r.scalar("Filter behavior", "u16", enum=_FILTER_BEHAVIOR)
    r.scalar("Volume threshold (dB)", "f32")
    r.scalar("Maximum voices", "u16")
    if r.version > 126:
        r.scalar("Maximum dangerous virtual voices", "u16")

    count = r.count("State group count", "u32")
    with r.section("State groups"):
        for index in r.each(count):
            r.scalar("State group", "u32", id_kind="state_group")
            r.scalar("Default transition time (ms)", "u32")
            transitions = r.count("Transition count", "u32")
            with r.section("Transitions"):
                for item in r.each(transitions):
                    r.scalar("From state", "u32", id_kind="state_value")
                    r.scalar("To state", "u32", id_kind="state_value")
                    r.scalar("Transition time (ms)", "u32")

    count = r.count("Switch group count", "u32")
    with r.section("Switch groups"):
        for index in r.each(count):
            r.scalar("Switch group", "u32", id_kind="switch_group")
            _typed_rtpc_input(r)
            _switch_graph(r, r.count("Point count", "u32"))

    count = r.count("Game parameter count", "u32")
    with r.section("Game parameters"):
        for index in r.each(count):
            r.scalar("Game parameter", "u32", id_kind="game_parameter")
            r.scalar("Default value", "f32")
            r.scalar("Ramping", "u32", enum=_RAMP)
            r.scalar("Ramp up", "f32")
            r.scalar("Ramp down", "f32")
            r.scalar("Built-in parameter", "u8", enum=_BUILT_IN if r.version > 126 else _BUILT_IN_126)

    count = r.count("Acoustic texture count", "u32")
    with r.section("Acoustic textures"):
        for index in r.each(count):
            r.scalar("Texture ID", "u32", id_kind="acoustic_texture")
            r.scalar("Absorption offset", "f32")
            r.scalar("Low-frequency absorption", "f32")
            r.scalar("Mid-low absorption", "f32")
            r.scalar("Mid-high absorption", "f32")
            r.scalar("High-frequency absorption", "f32")
            r.scalar("Scattering", "f32")


def _chunk_envs(r: _Reader) -> None:
    for x_name in ("Obstruction", "Occlusion"):
        with r.section(x_name):
            for y_name in ("Volume", "Low-pass filter", "High-pass filter"):
                with r.section(y_name):
                    r.scalar("Enabled", "u8", enum=_BOOL)
                    r.scalar("Scaling", "u8", enum=_SCALING)
                    _graph(r, r.count("Point count", "u16"))


def _chunk_plat(r: _Reader) -> None:
    if r.version >= 137:
        r.zstring("Platform")
    else:
        _sized_string(r, "Platform name bytes", "Platform")


def _chunk_stid(r: _Reader) -> None:
    r.scalar("String type", "u32", enum={0: "None", 1: "Bank"})
    count = r.count("Entry count", "u32")
    with r.section("Bank file names"):
        for index in r.each(count):
            r.scalar("SoundBank", "u32", id_kind="bank")
            _sized_string(r, "Name bytes", "File name", "u8", "bytes")


_CHUNK_PARSERS = {
    "BKHD": ("Bank header", _chunk_bkhd),
    "DIDX": ("Embedded media index", _chunk_didx),
    "INIT": ("Registered plug-ins", _chunk_init),
    "STMG": ("Global Wwise settings", _chunk_stmg),
    "ENVS": ("Obstruction and occlusion", _chunk_envs),
    "PLAT": ("Target platform", _chunk_plat),
    "STID": ("SoundBank file names", _chunk_stid),
}


def _sound(r: _Reader) -> None:
    _source(r)
    _node_base(r)


def _feedback_node(r: _Reader) -> None:
    with r.section("Sources"):
        count = r.count("Source count", "u32")
        for index in r.each(count):
            r.scalar("Company ID", "u16")
            r.scalar("Device ID", "u16")
            r.scalar("Volume offset (dB)", "f32")
            _source(r)
    _node_base(r)


def _actor_mixer(r: _Reader) -> None:
    _node_base(r)
    _children(r)


_PARSERS = {
    0x01: _state,
    0x02: _sound,
    0x03: _action,
    0x04: _event,
    0x05: _random_container,
    0x06: _switch_container,
    0x07: _actor_mixer,
    0x08: _bus,
    0x09: _layer_container,
    0x0A: _music_segment,
    0x0B: _music_track,
    0x0C: _music_switch,
    0x0D: _music_random,
    0x0E: _attenuation,
    0x0F: _dialogue_event,
    0x10: _fx,
    0x11: _fx,
    0x12: _bus,
    0x13: _envelope,
    0x14: _envelope,
    0x15: _audio_device,
    0x16: _envelope,
}
# v126 and older number the types after 0x0F differently (0x10/0x11 are feedback).
_PARSERS_126 = {
    **{type_id: parser for type_id, parser in _PARSERS.items() if type_id <= 0x0F},
    0x10: _bus,
    0x11: _feedback_node,
    0x12: _fx,
    0x13: _fx,
    0x14: _bus,
    0x15: _envelope,
    0x16: _envelope,
    0x17: _audio_device,
}


_ERRORS = (IndexError, struct.error, _LayoutError, UnicodeError)


def _run(r: _Reader, source: tuple) -> None:
    if source[0] == "hirc":
        r.scalar("Object ID", "u32", editable=False, visible=False)
        parser = (_PARSERS_126 if r.version <= 126 else _PARSERS).get(source[1])
        if parser is None:
            raise _LayoutError(f"HIRC type 0x{source[1]:02X} has no v{r.version} layout")
    else:
        parser = _CHUNK_PARSERS[source[1]][1]
    parser(r)
    if not r.writing and r.pos != len(r.data):
        raise _LayoutError(
            f"layout stopped at 0x{r.pos:X}; payload ends at 0x{len(r.data):X}"
        )


def _parse(payload: bytes, source: tuple) -> WwiseObjectLayout:
    reader = _Reader(payload, source[2], source[0] == "hirc" and source[3])
    error = ""
    try:
        _run(reader, source)
    except _ERRORS as exc:
        error = str(exc)
    return WwiseObjectLayout(
        tuple(reader.fields), reader.pos, len(payload), tuple(reader.anchors.items()),
        error, source,
    )


def parse_structured_object(
    type_id: int, payload: bytes, version: int, *, feedback: bool = False
) -> WwiseObjectLayout:
    """Return an exact field map for one supported HIRC payload.

    ``feedback`` is the v126-and-older bank header flag that adds a feedback bus to nodes.
    """

    return _parse(payload, ("hirc", int(type_id), int(version), bool(feedback)))


def parse_v132_object(type_id: int, payload: bytes) -> WwiseObjectLayout:
    return parse_structured_object(type_id, payload, 132)


def parse_v135_object(type_id: int, payload: bytes) -> WwiseObjectLayout:
    return parse_structured_object(type_id, payload, 135)


def parse_v140_object(type_id: int, payload: bytes) -> WwiseObjectLayout:
    return parse_structured_object(type_id, payload, 140)


def parse_v145_object(type_id: int, payload: bytes) -> WwiseObjectLayout:
    return parse_structured_object(type_id, payload, 145)


def parse_v150_object(type_id: int, payload: bytes) -> WwiseObjectLayout:
    return parse_structured_object(type_id, payload, 150)


def parse_structured_chunk(
    chunk_id: bytes | str, payload: bytes, version: int
) -> WwiseChunkLayout | None:
    """Decode one supported non-media top-level bank chunk."""

    key = (
        chunk_id.decode("ascii", "replace")
        if isinstance(chunk_id, bytes) else str(chunk_id)
    )
    if key not in _CHUNK_PARSERS:
        return None
    payload = bytes(payload)
    return WwiseChunkLayout(
        key, _CHUNK_PARSERS[key][0], payload, _parse(payload, ("chunk", key, int(version)))
    )


def parse_v132_chunk(chunk_id: bytes | str, payload: bytes) -> WwiseChunkLayout | None:
    return parse_structured_chunk(chunk_id, payload, 132)


def parse_v135_chunk(chunk_id: bytes | str, payload: bytes) -> WwiseChunkLayout | None:
    return parse_structured_chunk(chunk_id, payload, 135)


def parse_v140_chunk(chunk_id: bytes | str, payload: bytes) -> WwiseChunkLayout | None:
    return parse_structured_chunk(chunk_id, payload, 140)


def parse_v145_chunk(chunk_id: bytes | str, payload: bytes) -> WwiseChunkLayout | None:
    return parse_structured_chunk(chunk_id, payload, 145)


def parse_v150_chunk(chunk_id: bytes | str, payload: bytes) -> WwiseChunkLayout | None:
    return parse_structured_chunk(chunk_id, payload, 150)


def _pack_var(value: int) -> bytes:
    if value < 0:
        raise ValueError("Variable integers cannot be negative")
    groups = [value & 0x7F]
    value >>= 7
    while value:
        groups.append(value & 0x7F)
        value >>= 7
    groups.reverse()
    return bytes(part | (0x80 if index + 1 < len(groups) else 0) for index, part in enumerate(groups))


def _record(payload: bytes, layout: WwiseObjectLayout) -> _Reader:
    if not layout.complete:
        raise ValueError(f"Cannot edit an incomplete Wwise layout: {layout.error}")
    source = layout.source
    reader = _Reader(payload, source[2], source[0] == "hirc" and source[3], record=True)
    try:
        _run(reader, source)
    except _ERRORS as exc:
        raise ValueError(f"Cannot edit an incomplete Wwise layout: {exc}") from exc
    return reader


def _write(payload: bytes, source: tuple, units: list[_Unit], values=None) -> bytes:
    writer = _Writer(payload, units, source[2], source[0] == "hirc" and source[3], values)
    try:
        _run(writer, source)
    except _ERRORS as exc:
        raise ValueError(f"The edited layout cannot be written: {exc}") from exc
    return writer.out.take_bytes()


def _rebuild(payload: bytes, layout: WwiseObjectLayout, units: list[_Unit], values=None) -> bytes:
    source = layout.source
    result = _write(payload, source, units, values)
    reader = _Reader(result, source[2], source[0] == "hirc" and source[3], record=True)
    try:
        _run(reader, source)
    except _ERRORS as exc:
        raise ValueError(f"The edited layout does not read back: {exc}") from exc
    unsorted = False
    for lane in (lane for item in reader.lists for lane in item.lanes if lane.ordered):
        cells = [reader.units[start] for start, _end in lane.items]
        for cell, raw in zip(cells, sorted(
            (cell.raw for cell in cells), key=lambda raw: int.from_bytes(raw, "little")
        )):
            unsorted |= cell.raw != raw
            cell.raw = raw
    return _write(result, source, reader.units) if unsorted else result


def _encode(field: WwiseField, unit: _Unit, value) -> bytes:
    path, raw = field.path, unit.raw
    if field.storage == "bit":
        number = int(value)
        maximum = field.mask >> field.shift
        if not 0 <= number <= maximum:
            raise ValueError(f"{path} must be between 0 and {maximum}")
        current = int.from_bytes(raw, "little") & ~field.mask
        return (current | (number << field.shift)).to_bytes(len(raw), "little")
    if field.storage == "var":
        return _pack_var(int(value))
    if field.storage == "string":
        text = str(value)
        if "\0" in text:
            raise ValueError(f"{path} cannot contain NUL characters")
        encoded = text.encode("utf-8")
        if unit.storage == "bytes":
            return encoded
        return encoded + b"\0" if encoded or unit.storage == "zstring" else b""
    if field.storage == "bytes":
        try:
            return bytes.fromhex(str(value))
        except ValueError as exc:
            raise ValueError(f"{path} must be hexadecimal bytes") from exc
    number = (
        float(value) if field.storage.startswith("f")
        else int(value, 0) if isinstance(value, str) else int(value)
    )
    if isinstance(number, float) and not math.isfinite(number):
        raise ValueError(f"{path} must be finite")
    try:
        return struct.pack("<" + _Reader._FORMATS[field.storage], number)
    except (KeyError, struct.error) as exc:
        raise ValueError(f"Invalid value for {path}: {value}") from exc


def set_v132_fields(payload: bytes, layout: WwiseObjectLayout, changes: dict[str, object]) -> bytes:
    if not changes:
        return bytes(payload)
    reader = _record(payload, layout)
    by_path = {field.path: field for field in layout.fields}
    starts: dict[int, list[_Unit]] = {}
    for unit in reader.units:
        starts.setdefault(unit.offset, []).append(unit)
    for path, value in changes.items():
        field = by_path.get(path)
        if field is None:
            raise ValueError(f"Unknown Wwise field: {path}")
        if not field.editable:
            raise ValueError(f"{path} controls structure and is read-only here")
        units = starts.get(field.offset, ())
        unit = next((item for item in units if item.path == path), None) or next(
            item for item in units if item.raw
        )
        unit.raw = _encode(field, unit, value)
    return _rebuild(payload, layout, reader.units)


set_wwise_fields = set_v132_fields


_ANY_TO_ANY = {
    "Source count": 1, "Source 1": 0xFFFFFFFF, "Destination count": 1, "Destination 1": 0xFFFFFFFF,
    "Source behavior/Fade curve": 4, "Destination behavior/Fade curve": 4,
}
_NEW_ITEM_VALUES = {
    "Points": {"Interpolation": 4},
    "Playlist": {"Weight": 50_000},
    "Music playlist": {"Selection type": 0xFFFFFFFF, "Loop count": 1, "Weight": 50_000},
    "Transition rules": _ANY_TO_ANY,
}
_METER = {
    "Meter/Grid period": 2000.0, "Meter/Tempo": 120.0, "Meter/Beats per bar": 4,
    "Meter/Beat value": 4, "Meter/Override meter": 1,
}
_FIRST_RULE = {
    "Transition rules/Rule count": 1,
    **{f"Transition rules/1/{label}": value for label, value in _ANY_TO_ANY.items()},
}
_ENTRY_CUE, _EXIT_CUE = 43_573_010, 1_539_036_744
_NEW_OBJECT_VALUES = {
    0x0A: {**_METER, "Cues/Cue count": 2, "Cues/1/Cue ID": _ENTRY_CUE, "Cues/2/Cue ID": _EXIT_CUE},
    0x0C: {
        **_METER, **_FIRST_RULE, "Decision-tree bytes": 12,
        "Decision tree/1/Branch or audio object": 1, "Decision tree/1/Weight": 50,
        "Decision tree/1/Probability": 100,
    },
    0x0D: {
        **_METER, **_FIRST_RULE, "Playlist node count": 1,
        "Music playlist/Loop count": 1, "Music playlist/Weight": 50_000,
    },
    0x0F: {"Probability (%)": 100},
}


def wwise_lists(payload: bytes, layout: WwiseObjectLayout) -> tuple[WwiseList, ...]:
    reader = _record(payload, layout)
    return tuple(
        WwiseList(
            item.lanes[0].base, reader.units[item.count].path,
            tuple(item.lanes[0].item_root(index) for index in range(len(item.lanes[0].items))),
            tuple(
                tuple(lane.item_root(index) if lane.root else None for index in range(len(lane.items)))
                for lane in item.lanes
            ),
        )
        for item in reader.lists if item.editable
    )


def set_wwise_keyed_values(payload: bytes, layout: WwiseObjectLayout, count_path: str, entries) -> bytes:
    reader = _record(payload, layout)
    units = reader.units
    target = next(item for item in reader.lists if units[item.count].path == count_path)
    lanes, count = target.lanes, units[target.count]
    fmt = "<" + _Reader._FORMATS[count.storage]
    ids = sorted(entries)
    result = list(units)
    for lane in reversed(lanes[1:]):
        result[lane.start:lane.end] = []
    result[lanes[0].start:lanes[0].end] = [
        _Unit(lanes[0].item_root(index), count.storage, -1, struct.pack(fmt, prop_id)) for index, prop_id in enumerate(ids)
    ]
    result[target.count] = _Unit(count.path, count.storage, count.offset, struct.pack(fmt, len(ids)))
    edited = _rebuild(payload, layout, result)
    layout = _parse(edited, layout.source)
    reader = _record(edited, layout)
    target = next(item for item in reader.lists if reader.units[item.count].path == count_path)
    changes = {}
    for index, prop_id in enumerate(ids):
        values = entries[prop_id] if isinstance(entries[prop_id], tuple) else (entries[prop_id],)
        paths = [unit.path for lane in target.lanes[1:] for unit in reader.units[slice(*lane.items[index])]]
        changes.update(zip(paths, values))
    return set_v132_fields(edited, layout, changes)


def reorder_wwise_list(
    payload: bytes, layout: WwiseObjectLayout, count_path: str, order
) -> bytes:
    reader = _record(payload, layout)
    units = reader.units
    target = next((
        item for item in reader.lists
        if item.editable and units[item.count].path == count_path
    ), None)
    if target is None:
        raise ValueError(f"{count_path} is not an editable list")
    order = list(order)
    first = target.lanes[0]
    size = len(first.items)
    if any(index is not None and not 0 <= index < size for index in order):
        raise ValueError(f"{count_path} has no such item")
    values = {field.path: field.value for field in layout.fields}
    if count_path == "Transition rules/Rule count" and size and order[:1] != [0]:
        raise ValueError("The first transition rule (Any to Any) must stay first")
    if count_path == "Cues/Cue count":
        order.sort(key=lambda index: (1, 0.0) if index is None else (
            {_ENTRY_CUE: 0, _EXIT_CUE: 2}.get(values[f"Cues/{index + 1}/Cue ID"], 1),
            values[f"Cues/{index + 1}/Position (ms)"],
        ))
    if count_path == "Clips/Clip count":
        order.sort(key=lambda index: 0 if index is None else values[f"Clips/{index + 1}/Subtrack ID"])
    keyed = size and any(lane.root is None for lane in target.lanes)
    if keyed:
        storage = units[first.items[0][0]].storage
        fmt = "<" + _Reader._FORMATS[storage]
        used = {struct.unpack(fmt, units[start].raw)[0] for start, _end in first.items}
        free = (value for value in range(1 << 16) if value not in used)
    result = list(units)
    for lane in reversed(target.lanes):
        block = []
        for position, index in enumerate(order):
            if index is None:
                if keyed and lane is first:
                    block.append(_Unit(
                        lane.item_root(position), storage, -1, struct.pack(fmt, next(free))
                    ))
                continue
            start, end = lane.items[index]
            old = new = ""
            if lane.root is not None:
                old, new = lane.item_root(index), lane.item_root(position)
            for unit in units[start:end]:
                path = unit.path
                if old != new and (path == old or path.startswith(old + "/")):
                    path = new + path[len(old):]
                block.append(_Unit(path, unit.storage, unit.offset, unit.raw))
        result[lane.start:lane.end] = block
    count = units[target.count]
    value = len(order) * target.scale
    try:
        raw = _pack_var(value) if count.storage == "var" else struct.pack(
            "<" + _Reader._FORMATS[count.storage], value
        )
    except struct.error as exc:
        raise ValueError(f"{count_path} cannot hold {len(order)} items") from exc
    result[target.count] = _Unit(count.path, count.storage, count.offset, raw)
    kind = "Music playlist" if first.base.split("/")[0] == "Music playlist" else first.base.rsplit("/", 1)[-1]
    defaults = _NEW_ITEM_VALUES.get(kind, {}) if first.root == "{}" else {}
    edited = _rebuild(payload, layout, result, {
        "Clips/Subtrack count": 1,
        **{
            f"{first.item_root(position)}/{label}": value
            for position, index in enumerate(order) if index is None
            for label, value in defaults.items()
        },
    })
    source = layout.source
    if kind == "Music playlist":
        reader = _Reader(edited, source[2], source[0] == "hirc" and source[3], record=True)
        _run(reader, source)
        ids = [unit for unit in reader.units if unit.path.endswith("Playlist item ID")]
        seen, top = set(), max((int.from_bytes(unit.raw, "little") for unit in ids), default=0)
        for unit in ids:
            item = int.from_bytes(unit.raw, "little")
            if not item or item in seen:
                top = item = top + 1
                unit.raw = item.to_bytes(4, "little")
            seen.add(item)
        edited = _write(edited, source, reader.units)
    if count_path == "Clips/Clip count":
        mapping = {}
        for position, index in enumerate(order):
            if index is not None:
                mapping.setdefault(index, position)
        current = _parse(edited, source)
        fields = {field.path: field.value for field in current.fields}
        indexes = [
            fields[f"Clip automation/{item}/Clip index"]
            for item in range(1, fields["Clip automation/Automation count"] + 1)
        ]
        kept = [item for item, index in enumerate(indexes) if index in mapping]
        if len(kept) != len(indexes):
            edited = reorder_wwise_list(edited, current, "Clip automation/Automation count", kept)
            current = _parse(edited, source)
        changes = {
            f"Clip automation/{row}/Clip index": mapping[indexes[item]]
            for row, item in enumerate(kept, 1) if mapping[indexes[item]] != indexes[item]
        }
        if changes:
            edited = set_v132_fields(edited, current, changes)
            current = _parse(edited, source)
        fields = {field.path: field.value for field in current.fields}
        have, need = fields.get("Switch association count", 0), fields.get("Clips/Subtrack count", 0)
        if fields["Track type"] == 3 and have < need:
            edited = reorder_wwise_list(
                edited, current, "Switch association count", [*range(have), *[None] * (need - have)]
            )
    return edited


def hirc_layout_exists(type_id: int, version: int) -> bool:
    return int(type_id) in (_PARSERS_126 if version <= 126 else _PARSERS)


def new_structured_object(
    type_id: int, object_id: int, version: int, values=None, *, feedback: bool = False
) -> bytes:
    source = ("hirc", int(type_id), int(version), bool(feedback))
    seeds = {f"Curve assignments/{name}": -1 for name in attenuation_targets(version)} if type_id == 0x0E else {
        **_NEW_OBJECT_VALUES.get(int(type_id), {}),
        **({"Music playlist/Playlist item ID": int(object_id)} if type_id == 0x0D else {}),
    }
    writer = _Writer(b"", [], version, feedback, {**seeds, "Object ID": int(object_id), **(values or {})})
    try:
        _run(writer, source)
    except _ERRORS as exc:
        raise ValueError(f"Cannot create HIRC type 0x{int(type_id):02X}: {exc}") from exc
    result = writer.out.take_bytes()
    check = _parse(result, source)
    if not check.complete:
        raise ValueError(f"Cannot create HIRC type 0x{int(type_id):02X}: {check.error}")
    return result


@dataclass(frozen=True, slots=True)
class WwiseDecisionPath:
    keys: tuple[int, ...]
    target: int
    weight: int = 50
    probability: int = 100


def _decision_rows(layout: WwiseObjectLayout):
    values = {field.path: field.value for field in layout.fields}
    rows, index = [], 1
    while f"Decision tree/{index}/Value" in values:
        base = f"Decision tree/{index}"
        rows.append((
            values[f"{base}/Value"], values.get(f"{base}/Audio object"),
            values.get(f"{base}/First child", 0), values.get(f"{base}/Child count", 0),
            values[f"{base}/Weight"], values[f"{base}/Probability"],
        ))
        index += 1
    return values, rows


def _decision_walk(rows, visit, index=0, keys=(), limit=64):
    key, audio, first, count, weight, probability = rows[index]
    visit(keys, rows[index])
    if audio is None and len(keys) < limit:
        for child in range(first, min(first + count, len(rows))):
            _decision_walk(rows, visit, child, keys + (rows[child][0],), limit)


def decision_tree(layout: WwiseObjectLayout):
    values, rows = _decision_rows(layout)
    depth = values["Decision depth"]
    arguments = tuple(
        (values[f"Decision arguments/Group {index}"], values[f"Decision arguments/Group {index} type"])
        for index in range(1, depth + 1)
    )
    paths = []
    if rows:
        _decision_walk(rows, lambda keys, row: keys and row[1] is not None and paths.append(
            WwiseDecisionPath(keys, row[1], row[4], row[5])
        ), limit=depth)
    return arguments, tuple(paths)


def set_decision_tree(payload: bytes, layout: WwiseObjectLayout, arguments, paths) -> bytes:
    values, rows = _decision_rows(layout)
    arguments = [(int(group), int(kind)) for group, kind in arguments]
    depth = len(arguments)
    leaves = {}
    for path in paths:
        keys = tuple(int(key) for key in path.keys)
        if not depth or len(keys) != depth:
            raise ValueError("Each decision path needs one value per argument")
        if keys in leaves:
            raise ValueError(f"Decision path {keys} appears twice")
        leaves[keys] = path
    branches = {}
    if rows:
        _decision_walk(rows, lambda keys, row: row[1] is None and branches.setdefault(
            keys, (row[0], row[4], row[5])
        ), limit=values["Decision depth"])
    key, weight, probability = branches.get((), (0, 50, 100))
    packed = [[key, 0, weight, probability]]

    def place(prefix, row):
        kids = sorted({keys[len(prefix)] for keys in leaves if keys[:len(prefix)] == prefix})
        first = len(packed)
        packed[row][1] = first | len(kids) << 16
        for key in kids:
            child = prefix + (key,)
            if len(child) == depth:
                leaf = leaves[child]
                packed.append([key, leaf.target, leaf.weight, leaf.probability])
            else:
                packed.append([key, 0, *branches.get(child, (key, 50, 100))[1:]])
        for offset, key in enumerate(kids):
            if len(prefix) + 1 < depth:
                place(prefix + (key,), first + offset)

    place((), 0)

    reader = _record(payload, layout)
    units = reader.units
    position = {unit.path: index for index, unit in enumerate(units)}
    lists = {item.count: item for item in reader.lists}

    def unit(path, storage, value):
        return _Unit(path, storage, -1, struct.pack("<" + _Reader._FORMATS[storage], value))

    result = list(units)
    lane = lists[position["Decision-tree bytes"]].lanes[0]
    result[lane.start:lane.end] = [
        unit(f"Decision tree/{row}/{label}", storage, value)
        for row, item in enumerate(packed, 1)
        for label, storage, value in zip(
            ("Value", "Branch or audio object", "Weight", "Probability"),
            ("u32", "u32", "u16", "u16"), item,
        )
    ]
    result[position["Decision-tree bytes"]] = unit("Decision-tree bytes", "u32", 12 * len(packed))
    lanes = lists[position["Decision depth"]].lanes
    result[lanes[0].start:lanes[-1].end] = [
        *(unit(f"Decision arguments/Group {row}", "u32", group) for row, (group, _kind) in enumerate(arguments, 1)),
        *(unit(f"Decision arguments/Group {row} type", "u8", kind) for row, (_group, kind) in enumerate(arguments, 1)),
    ]
    result[position["Decision depth"]] = unit("Decision depth", "u32", depth)
    return _rebuild(payload, layout, result)


__all__ = [
    "WwiseChunkLayout", "WwiseDecisionPath", "WwiseField", "WwiseList", "WwiseObjectLayout",
    "decision_tree", "hirc_layout_exists", "new_structured_object", "set_decision_tree",
    "parse_structured_chunk", "parse_structured_object",
    "parse_v132_chunk", "parse_v132_object", "parse_v135_chunk",
    "parse_v135_object", "parse_v140_chunk", "parse_v140_object",
    "parse_v145_chunk", "parse_v145_object", "parse_v150_chunk",
    "parse_v150_object",
    "reorder_wwise_list", "set_v132_fields", "set_wwise_fields", "set_wwise_keyed_values", "wwise_lists",
]
