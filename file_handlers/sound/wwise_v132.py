"""Exact editable layouts for the modern Wwise banks used by RE Engine games.

The original public API is retained for Wwise v132 compatibility. The same
compact reader covers RE8's v135, MHRise/RE4's v140, MHWilds/RE9's v145,
and Pragmata's v150 structures.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import math
import struct

from .wwise_schema import (
    BNK_CURVE_INTERPOLATION,
    BNK_CURVE_SCALING,
    BNK_FX_ENUMS,
    BNK_FX_SCHEMAS,
    BNK_STANDARD_CUE_NAMES,
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


class _LayoutError(ValueError):
    pass


class _Reader:
    _FORMATS = {
        "u8": "B", "i8": "b", "u16": "H", "i16": "h",
        "u32": "I", "i32": "i", "f32": "f", "f64": "d", "bool": "B",
    }

    def __init__(self, data: bytes, version: int = 132):
        self.data, self.pos, self.version = data, 0, int(version)
        self.fields: list[WwiseField] = []
        self.parts: list[str] = []
        self.anchors: dict[str, int] = {}

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
        fmt = self._FORMATS[storage]
        size = struct.calcsize("<" + fmt)
        if self.pos + size > len(self.data):
            raise _LayoutError(f"{self._path(label)} exceeds the payload at 0x{self.pos:X}")
        offset = self.pos
        value = struct.unpack_from("<" + fmt, self.data, offset)[0]
        self.pos += size
        if storage.startswith("f") and not math.isfinite(value):
            raise _LayoutError(f"{self._path(label)} is not finite")
        self.fields.append(WwiseField(
            self._path(label), label, storage, offset, size, value,
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
        offset = self.pos
        value = 0
        for _ in range(5):
            if self.pos >= len(self.data):
                raise _LayoutError(f"{self._path(label)} has a truncated variable integer")
            byte = self.data[self.pos]
            self.pos += 1
            value = (value << 7) | (byte & 0x7F)
            if not byte & 0x80:
                self.fields.append(WwiseField(
                    self._path(label), label, "var", offset, self.pos - offset, value,
                    self._enum(enum), id_kind, "", editable, visible,
                ))
                return value
        raise _LayoutError(f"{self._path(label)} has an oversized variable integer")

    def flags(self, label: str, specs, *, storage: str = "u8") -> int:
        fmt = self._FORMATS[storage]
        size = struct.calcsize("<" + fmt)
        if self.pos + size > len(self.data):
            raise _LayoutError(f"{self._path(label)} exceeds the payload at 0x{self.pos:X}")
        offset = self.pos
        raw = struct.unpack_from("<" + fmt, self.data, offset)[0]
        self.pos += size
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

    def blob(self, label: str, size: int, *, visible: bool = False) -> bytes:
        if size < 0 or self.pos + size > len(self.data):
            raise _LayoutError(f"{self._path(label)} has invalid size {size}")
        offset, value = self.pos, self.data[self.pos:self.pos + size]
        self.pos += size
        self.fields.append(WwiseField(
            self._path(label), label, "bytes", offset, size, value.hex(" "),
            editable=False, visible=visible,
        ))
        return value

    def string(self, label: str, size: int) -> str:
        value = self.blob(label, size, visible=True)
        field = self.fields[-1]
        text = value.rstrip(b"\0").decode("utf-8", "replace")
        self.fields[-1] = WwiseField(
            field.path, label, "string", field.offset, field.size, text,
            editable=False, visible=True,
        )
        return text

    def zstring(self, label: str) -> str:
        end = self.data.find(b"\0", self.pos)
        if end < 0:
            raise _LayoutError(f"{self._path(label)} has no NUL terminator")
        return self.string(label, end + 1 - self.pos)

    def mark(self, name: str, offset: int | None = None) -> None:
        self.anchors[name] = self.pos if offset is None else offset


def _items(r: _Reader, section: str, count: int, parser) -> None:
    if count < 0 or count > 0x100000:
        raise _LayoutError(f"{section} has unreasonable count {count}")
    with r.section(section):
        for index in range(count):
            with r.section(f"{index + 1}"):
                parser(index)


def _property_value_storage(r: _Reader, kind: str, prop_id: int) -> str:
    return "u32" if prop_id in integer_properties(kind, r.version) else "f32"


def _properties(r: _Reader, *, kind: str = "object", ranges: bool = True) -> None:
    names = property_names(kind, r.version)
    r.mark("property_bundle")
    with r.section("Properties"):
        count = r.scalar("Property count", "u8", editable=False)
        ids = [r.scalar(
            f"Property {index + 1} type", "u8", enum=names, editable=False,
        ) for index in range(count)]
        for index, prop_id in enumerate(ids):
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
        count = r.scalar("Randomizer count", "u8", editable=False)
        ids = [r.scalar(
            f"Randomizer {index + 1} type", "u8", enum=names, editable=False,
        ) for index in range(count)]
        for prop_id in ids:
            storage = _property_value_storage(r, kind, prop_id)
            label = names.get(prop_id, f"Property 0x{prop_id:02X}")
            r.scalar(f"{label} minimum", storage)
            r.scalar(f"{label} maximum", storage)


def _state_properties(r: _Reader, names: dict[int, str] | None = None) -> None:
    names = names or property_names("state", r.version)
    with r.section("State properties"):
        count = r.var("Property count", editable=False)
        for index in range(count):
            with r.section(f"{index + 1}"):
                r.var("Property", enum=names, editable=False)
                r.scalar("Accumulation", "u8", enum=_ACCUM)
                r.scalar("Stored in decibels", "u8", enum=_BOOL)
    with r.section("State groups"):
        count = r.var("Group count", editable=False)
        for index in range(count):
            with r.section(f"{index + 1}"):
                r.scalar("State group", "u32", id_kind="state_group")
                r.scalar("Synchronization", "u8", enum=_SYNC)
                states = r.var("State count", editable=False)
                for state in range(states):
                    with r.section(f"State {state + 1}"):
                        r.scalar("State", "u32", id_kind="state_value")
                        if r.version <= 145:
                            r.scalar(
                                "State object", "u32", id_kind="hirc",
                                reference_role="state object",
                            )
                        else:
                            value_count = r.scalar(
                                "Property count", "u16", editable=False
                            )
                            ids = [
                                r.scalar(
                                    f"Property {item + 1} type", "u16",
                                    enum=names, editable=False,
                                )
                                for item in range(value_count)
                            ]
                            for prop_id in ids:
                                label = names.get(
                                    prop_id, f"Property 0x{prop_id:04X}"
                                )
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
    modern = r.version >= 144
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
    names = names or property_names(
        "modulator" if modulator else "state", r.version
    )
    with r.section("RTPC curves"):
        count = r.scalar("Curve count", "u16", editable=False)
        for index in range(count):
            with r.section(f"{index + 1}"):
                _typed_rtpc_input(r)
                r.scalar("Accumulation", "u8", enum=_ACCUM)
                r.var("Target property", enum=names, editable=False)
                r.scalar("Curve ID", "u32", id_kind="curve")
                r.scalar("Scaling", "u8", enum=_SCALING)
                points = r.scalar("Point count", "u16", editable=False)
                _graph(r, points)


def _effects(r: _Reader, *, node: bool) -> None:
    with r.section("Effects"):
        if node:
            r.scalar("Override parent effects", "u8", enum=_BOOL)
        count = r.scalar("Effect count", "u8", editable=False)
        if count:
            if r.version <= 145:
                r.flags(
                    "Bypass flags",
                    tuple((f"Bypass slot {slot}", slot) for slot in range(4)),
                )
            else:
                r.scalar("Bypass all effects", "u8", enum=_BOOL)
        for index in range(count):
            with r.section(f"{index + 1}"):
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
        count = r.scalar("Metadata count", "u8", editable=False)
        for index in range(count):
            with r.section(f"{index + 1}"):
                r.scalar("Slot", "u8")
                r.scalar(
                    "Effect", "u32", id_kind="hirc",
                    reference_role="metadata effect",
                )
                r.scalar("Shared effect", "u8", enum=_BOOL)


def _positioning(r: _Reader) -> None:
    with r.section("Positioning"):
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
                ("Enable diffraction", 6),
                ("Do not loop 3D automation path", 7),
            ))
        else:
            r.flags("3D flags", (
                ("Spatialization", 0, 2, _SPATIALIZATION),
                ("Hold emitter position and orientation", 3),
                ("Hold listener orientation", 4),
                ("Loop 3D automation path", 5),
            ))
        position_type = (flags >> 5) & 0x03
        if position_type == 0:
            return
        r.scalar("Path mode", "u8", enum=_PATH_MODE)
        r.scalar("Transition time (ms)", "i32")
        vertex_count = r.scalar("Vertex count", "u32", editable=False)
        def vertex(_index):
            r.scalar("X", "f32")
            r.scalar("Y", "f32")
            r.scalar("Z", "f32")
            r.scalar("Duration (ms)", "i32")
        _items(r, "Path vertices", vertex_count, vertex)
        item_count = r.scalar("Path count", "u32", editable=False)
        def path(_index):
            r.scalar("First vertex", "u32")
            r.scalar("Vertex count", "u32", editable=False)
        _items(r, "Paths", item_count, path)
        def ranges(_index):
            r.scalar("X random range", "f32")
            r.scalar("Y random range", "f32")
            r.scalar("Z random range", "f32")
        _items(r, "Path randomization", item_count, ranges)


def _aux(r: _Reader) -> None:
    with r.section("Auxiliary sends"):
        flags = r.flags("Aux flags", (
            ("Use game-defined sends", 0),
            ("Override game-defined sends", 1),
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


def _children(r: _Reader) -> None:
    with r.section("Children"):
        start = r.pos
        count = r.scalar("Child count", "u32", editable=False)
        r.mark("child_count", start)
        for index in range(count):
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
        if plugin & 0x0F == 2:
            size = r.scalar("Plug-in parameter bytes", "u32", editable=False)
            _plugin_parameters(r, plugin, size)


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
        count = r.scalar("Stinger count", "u32", editable=False)
        for index in range(count):
            with r.section(f"{index + 1}"):
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
        count = r.scalar("Rule count", "u32", editable=False)
        for index in range(count):
            with r.section(f"{index + 1}"):
                sources = r.scalar("Source count", "u32", editable=False)
                for item in range(sources):
                    r.scalar(
                        f"Source {item + 1}", "u32", id_kind="hirc",
                        reference_role="transition source",
                    )
                destinations = r.scalar("Destination count", "u32", editable=False)
                for item in range(destinations):
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
    with r.section("State values"):
        count = r.scalar("Value count", "u16", editable=False)
        ids = [r.scalar(f"Value {index + 1} property", "u16", enum=names, editable=False) for index in range(count)]
        for prop_id in ids:
            r.scalar(f"{names.get(prop_id, f'Property 0x{prop_id:04X}')} value", "f32")


def _action(r: _Reader) -> None:
    action_type = r.scalar("Action type", "u16", editable=False)
    target_kind = action_type & 0xFF00
    external_kind = {
        0x1200: "state_value",
        0x1300: "game_parameter",
        0x1400: "game_parameter",
        0x1900: "switch_value",
        (0x1B00 if r.version >= 150 else 0x1D00): "trigger",
    }.get(target_kind)
    is_event = target_kind in (
        {0x2100, 0x2300}
        if r.version >= 150 else
        {0x1500, 0x1600, 0x1700, 0x2100, 0x2300}
    )
    r.scalar(
        "Target", "u32", id_kind=external_kind or ("event" if is_event else "hirc"),
        reference_role=("event target" if is_event else "action target")
        if external_kind is None else "",
    )
    r.flags("Target flags", (("Target is a bus", 0),))
    _properties(r)

    def exceptions():
        with r.section("Exceptions"):
            count = r.var("Exception count", editable=False)
            for index in range(count):
                with r.section(f"{index + 1}"):
                    r.scalar("Object", "u32", id_kind="hirc", reference_role="exception")
                    r.scalar("Object is a bus", "u8", enum=_BOOL)

    if target_kind in {0x0400, 0x0500, 0x2300}:
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
    elif target_kind in {
        0x1000, 0x1100, 0x1500, 0x1600, 0x1700, 0x1800,
        0x1C00, 0x1D00, 0x1F00, 0x2100,
    } | ({0x1A00, 0x1B00} if r.version >= 150 else set()):
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
        count = r.var("Action count", editable=False)
        for index in range(count):
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
        count = r.scalar("Item count", "u16", editable=False)
        for index in range(count):
            with r.section(f"{index + 1}"):
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
        count = r.scalar("Assignment count", "u32", editable=False)
        for index in range(count):
            with r.section(f"{index + 1}"):
                r.scalar("Value", "u32", id_kind=f"{category}_value")
                nodes = r.scalar("Object count", "u32", editable=False)
                for item in range(nodes):
                    r.scalar(
                        f"Object {item + 1}", "u32", id_kind="hirc",
                        reference_role="switch assignment",
                    )
    with r.section("Switch transitions"):
        count = r.scalar("Transition count", "u32", editable=False)
        for index in range(count):
            with r.section(f"{index + 1}"):
                r.scalar("Object", "u32", id_kind="hirc", reference_role="switch child")
                r.flags("Playback", (("Play first only", 0), ("Continue playback", 1)))
                r.flags("Mode", (("On switch", 0, 3, {0: "Play to end", 1: "Stop"}),))
                r.scalar("Fade-out time (ms)", "i32")
                r.scalar("Fade-in time (ms)", "i32")


def _layer_container(r: _Reader) -> None:
    _node_base(r)
    _children(r)
    with r.section("Layers"):
        count = r.scalar("Layer count", "u32", editable=False)
        for index in range(count):
            with r.section(f"{index + 1}"):
                # CAkLayer is serialized inline inside the container.  ulLayerID
                # identifies that inline record; it is not a top-level HIRC ID.
                r.scalar("Layer ID", "u32", id_kind="layer")
                _rtpc(r)
                _typed_rtpc_input(r, "Crossfade input")
                associations = r.scalar("Child curve count", "u32", editable=False)
                for item in range(associations):
                    with r.section(f"Child curve {item + 1}"):
                        r.scalar("Child", "u32", id_kind="hirc", reference_role="layer child")
                        points = r.scalar("Point count", "u32", editable=False)
                        _graph(r, points)
    r.scalar("Continuous validation", "u8", enum=_BOOL)


def _music_segment(r: _Reader) -> None:
    _music_node(r)
    r.scalar("Duration (ms)", "f64")
    with r.section("Cues"):
        count = r.scalar("Cue count", "u32", editable=False)
        for index in range(count):
            with r.section(f"{index + 1}"):
                r.scalar("Cue ID", "u32", enum=_STANDARD_CUES, id_kind="cue")
                r.scalar("Position (ms)", "f64")
                if r.version >= 137:
                    r.zstring("Name")
                else:
                    size = r.scalar("Name bytes", "u32", editable=False)
                    r.string("Name", size)


def _music_track(r: _Reader) -> None:
    r.flags("Music inheritance flags", (
        ("Override parent MIDI tempo", 1),
        ("Override parent MIDI target", 2),
        ("MIDI target is a bus", 3),
    ))
    sources = r.scalar("Source count", "u32", editable=False)
    for index in range(sources):
        _source(r, f"Source {index + 1}")
    with r.section("Clips"):
        count = r.scalar("Clip count", "u32", editable=False)
        for index in range(count):
            with r.section(f"{index + 1}"):
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
        count = r.scalar("Automation count", "u32", editable=False)
        for index in range(count):
            with r.section(f"{index + 1}"):
                r.scalar("Clip index", "u32")
                r.scalar("Automation type", "u32", enum=_CLIP_AUTOMATION)
                points = r.scalar("Point count", "u32", editable=False)
                _graph(r, points)
    _node_base(r)
    track_type = r.scalar("Track type", "u8", enum=_TRACK_TYPE)
    if track_type == 3:
        group_type = r.scalar("Switch group type", "u8", enum=_GROUP)
        category = "switch" if group_type == 0 else "state"
        r.scalar("Switch group", "u32", id_kind=f"{category}_group")
        r.scalar("Default switch", "u32", id_kind=f"{category}_value")
        count = r.scalar("Switch association count", "u32", editable=False)
        for index in range(count):
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
    depth = r.scalar("Decision depth", "u32", editable=False)
    group_fields = []
    with r.section("Decision arguments"):
        for index in range(depth):
            group_fields.append(len(r.fields))
            r.scalar(f"Group {index + 1}", "u32")
        group_types = []
        for index in range(depth):
            group_type = r.scalar(f"Group {index + 1} type", "u8", enum=_GROUP)
            group_types.append(group_type)
            category = "switch" if group_type == 0 else "state"
            field_index = group_fields[index]
            r.fields[field_index] = replace(
                r.fields[field_index], id_kind=f"{category}_group",
            )
    size = r.scalar("Decision-tree bytes", "u32", editable=False)
    r.scalar("Decision mode", "u8", enum=_DECISION_MODE)
    if size % 12:
        raise _LayoutError(f"decision tree has non-node size {size}")
    count = size // 12
    with r.section("Decision tree"):
        rows = []
        for index in range(count):
            with r.section(f"{index + 1}"):
                key_offset = r.pos
                key = r.scalar("Value", "u32")
                branch_offset = r.pos
                branch = r.scalar("Branch or audio object", "u32", editable=False)
                r.scalar("Weight", "u16")
                r.scalar("Probability", "u16")
                rows.append((key, key_offset, branch_offset, branch))
        # Classify the packed branch union using the same depth traversal Wwise uses.
        levels: dict[int, int] = {}
        kinds: dict[int, bool] = {}
        pending = [(0, 0)] if rows else []
        while pending:
            index, level = pending.pop()
            if index >= count or index in kinds:
                continue
            levels[index] = level
            branch = rows[index][3]
            child_index, child_count = branch & 0xFFFF, branch >> 16
            leaf = level >= depth or child_index >= count or child_count > count - child_index
            kinds[index] = leaf
            if not leaf:
                pending.extend((child_index + item, level + 1) for item in range(child_count))
        for index, (_key, key_offset, offset, branch) in enumerate(rows):
            base = f"Decision tree/{index + 1}"
            level = levels.get(index, 0)
            if 0 < level <= len(group_types):
                category = "switch" if group_types[level - 1] == 0 else "state"
                key_field = next(
                    item for item, field in enumerate(r.fields)
                    if field.offset == key_offset and field.label == "Value"
                )
                r.fields[key_field] = replace(
                    r.fields[key_field], id_kind=f"{category}_value",
                )
            source = next(field for field in r.fields if field.offset == offset)
            r.fields.remove(source)
            if kinds.get(index, True):
                r.fields.append(WwiseField(
                    f"{base}/Audio object", "Audio object", "u32", offset, 4,
                    branch, (), "hirc", "decision leaf",
                ))
            else:
                r.fields.extend((
                    WwiseField(f"{base}/First child", "First child", "bit", offset, 4,
                               branch & 0xFFFF, (), mask=0xFFFF),
                    WwiseField(f"{base}/Child count", "Child count", "bit", offset, 4,
                               branch >> 16, (), editable=False, mask=0xFFFF0000, shift=16),
                ))


def _playlist_node(r: _Reader, remaining: list[int], index: list[int]) -> None:
    if remaining[0] <= 0:
        raise _LayoutError("music playlist contains more nodes than declared")
    remaining[0] -= 1
    index[0] += 1
    with r.section(f"{index[0]}"):
        r.scalar("Segment", "u32", id_kind="hirc", reference_role="playlist segment")
        r.scalar("Playlist item ID", "u32", id_kind="playlist_item")
        children = r.scalar("Child count", "u32", editable=False)
        r.scalar("Selection type", "u32", enum=_RANDOM_SEQUENCE_TYPE)
        r.scalar("Loop count", "i16")
        r.scalar("Loop random minimum", "i16")
        r.scalar("Loop random maximum", "i16")
        r.scalar("Weight", "u32")
        r.scalar("Avoid-repeat count", "u16")
        r.scalar("Use weight", "u8", enum=_BOOL)
        r.scalar("Shuffle", "u8", enum=_BOOL)
        for _ in range(children):
            _playlist_node(r, remaining, index)


def _music_random(r: _Reader) -> None:
    _music_node(r)
    _transitions(r)
    total = r.scalar("Playlist node count", "u32", editable=False)
    remaining, index = [total], [0]
    with r.section("Music playlist"):
        if total:
            _playlist_node(r, remaining, index)
    if remaining[0]:
        raise _LayoutError(f"music playlist left {remaining[0]} declared nodes unread")


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
        count = r.scalar("Curve count", "u8", editable=False)
        for index in range(count):
            with r.section(f"{index + 1}"):
                r.scalar("Scaling", "u8", enum=_SCALING)
                points = r.scalar("Point count", "u16", editable=False)
                _graph(r, points)
    _rtpc(r)


def _reflect_parameters(r: _Reader, _size: int) -> None:
    """Wwise Reflect parameter block used by Wwise 2021.1 through 2023.1.

    The scalar order and curve bundle are the exact SetParamsBlock layouts from
    AkReflect.dll. Authoring-only properties are not serialized.
    """

    if r.version >= 145:
        scalars = [
            ("Speed of sound", "f32", None),
            ("Distance warping", "f32", None),
            ("Diffraction warping", "f32", None),
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
            ("Output channel configuration", "u32", _REFLECT_OUTPUT),
            ("Maximum reflections", "f32", None),
            ("Center percentage", "f32", None),
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
        )
    for label, storage, enum in scalars:
        r.scalar(label, storage, enum=enum)

    count = r.scalar("Curve count", "u16", editable=False)
    with r.section("Curves"):
        for index in range(count):
            with r.section(f"{index + 1}"):
                r.scalar("Curve", "u32", enum=_REFLECT_CURVES)
                points = r.scalar("Point count", "u16", editable=False)
                _graph(r, points)


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
        fields = fields[:12] + fields[16:-1]
    elif size != 159:
        raise _LayoutError(f"unsupported McDSP FutzBox parameter size {size}")
    _schema_parameters(r, fields)


def _soundseed_woosh_parameters(r: _Reader, _size: int) -> None:
    """SoundSeed Air Woosh v2 block used by Wwise 2021.1."""

    # The plug-in serializes its general settings before the four automatable
    # value/random/enable triplets. Duration is deliberately Real32 here even
    # though the authoring XML exposes it as Real64.
    for label, storage, enum in (
        ("Dynamic range", "f32", None),
        ("Duration random", "f32", None),
        ("Channels", "u16", _WOOSH_CHANNELS),
        ("Minimum distance", "f32", None),
        ("Roll-off factor", "f32", None),
        ("Duration", "f32", None),
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
    count = r.scalar("Deflector count", "u16", editable=False)
    if anchor < -1 or anchor >= count:
        raise _LayoutError(f"SoundSeed Woosh has invalid anchor deflector {anchor}")
    r.fields[anchor_field] = replace(
        r.fields[anchor_field],
        enum=((-1, "None"), *((index, f"Deflector {index + 1}") for index in range(count))),
    )
    with r.section("Deflectors"):
        for index in range(count):
            with r.section(f"{index + 1}"):
                r.scalar("Frequency", "f32")
                r.scalar("Q factor", "f32")
                r.scalar("Gain", "f32")
                # Anchor is stored once as the zero-based deflector index.
                if index == anchor:
                    r.fields.append(WwiseField(
                        r._path("Anchor"), "Anchor", "derived", r.pos, 0,
                        1, tuple(_BOOL.items()), editable=False,
                    ))

    count = r.scalar("Automation curve count", "u16", editable=False)
    with r.section("Automation curves"):
        for index in range(count):
            with r.section(f"{index + 1}"):
                r.scalar("Property", "u32", enum=_WOOSH_CURVES)
                points = r.scalar("Point count", "u16", editable=False)
                _graph(r, points)

    # The final bundle mirrors the authored Path2D: LinearTime followed by each
    # point's X position, Y position, and time coordinate.
    count = r.scalar("Path point count", "u16", editable=False)
    r.scalar("Linear path timing", "f32", enum=_BOOL)
    with r.section("Path points"):
        for index in range(count):
            with r.section(f"{index + 1}"):
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

    count = r.scalar("Deflector count", "u16", editable=False)
    r.scalar("Maximum distance", "f32")
    with r.section("Deflectors"):
        for index in range(count):
            with r.section(f"{index + 1}"):
                r.scalar("Distance", "f32")
                r.scalar("Direction", "f32")
                r.scalar("Frequency", "f32")
                r.scalar("Q factor", "f32")
                r.scalar("Gain", "f32")

    count = r.scalar("Automation curve count", "u16", editable=False)
    with r.section("Automation curves"):
        for index in range(count):
            with r.section(f"{index + 1}"):
                r.scalar("Property", "u32", enum=_WIND_CURVES)
                points = r.scalar("Point count", "u16", editable=False)
                _graph(r, points)


def _schema_parameters(r: _Reader, fields) -> None:
    for label, storage, *enum_name in fields:
        choices = BNK_FX_ENUMS.get(enum_name[0]) if enum_name else None
        enum = (
            choices if isinstance(choices, dict)
            else dict(enumerate(choices)) if choices else None
        )
        r.scalar(
            label, storage, enum=enum,
            id_kind="game_parameter" if label == "Game Parameter ShortID" else "",
        )


def _convolution_reverb_parameters(r: _Reader, size: int) -> None:
    _schema_parameters(r, BNK_FX_SCHEMAS[0x007F0003][1])
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


def _plugin_parameters(r: _Reader, plugin: int, size: int) -> None:
    schema = BNK_FX_SCHEMAS.get(plugin)
    start = r.pos
    custom = {
        0x006E1003: _futzbox_parameters,
        0x00730003: _matrix_reverb_parameters,
        0x00770002: _soundseed_wind_parameters,
        0x00780002: _soundseed_woosh_parameters,
        0x007F0003: _convolution_reverb_parameters,
        0x00810003: _meter_parameters,
        0x00AB0003: _reflect_parameters,
    }.get(plugin)
    if custom and size:
        with r.section("Plug-in parameters"):
            custom(r, size)
        if r.pos - start != size:
            raise _LayoutError(
                f"plug-in 0x{plugin:08X} layout used {r.pos - start} of {size} bytes"
            )
    elif schema and size:
        with r.section("Plug-in parameters"):
            _schema_parameters(r, schema[1])
        if r.pos - start != size:
            raise _LayoutError(
                f"plug-in 0x{plugin:08X} schema used {r.pos - start} of {size} bytes"
            )
    elif size:
        r.blob("Plug-in parameters", size, visible=True)


def _fx(r: _Reader) -> None:
    plugin = r.scalar("Plug-in", "u32", id_kind="plugin")
    names = fx_property_names(plugin, r.version)
    size = r.scalar("Plug-in parameter bytes", "u32", editable=False)
    _plugin_parameters(r, plugin, size)
    with r.section("Plug-in media"):
        count = r.scalar("Media count", "u8", editable=False)
        for index in range(count):
            with r.section(f"{index + 1}"):
                r.scalar("Slot", "u8")
                r.scalar("Source ID", "u32", id_kind="source")
    _rtpc(r, names=names)
    _state_properties(r, names=names)
    with r.section("Plug-in property values"):
        count = r.scalar("Value count", "u16", editable=False)
        names = names or property_names("state", r.version)
        for index in range(count):
            with r.section(f"{index + 1}"):
                r.var("Property", enum=names, editable=False)
                r.scalar("Accumulation", "u8", enum=_ACCUM)
                r.scalar("Value", "f32")


def _bus(r: _Reader) -> None:
    output = r.scalar("Output bus", "u32", id_kind="hirc", reference_role="output bus")
    if not output:
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
        count = r.scalar("Duck count", "u32", editable=False)
        for index in range(count):
            with r.section(f"{index + 1}"):
                r.scalar("Bus", "u32", id_kind="hirc", reference_role="ducked bus")
                r.scalar("Volume", "f32")
                r.scalar("Fade-out time (ms)", "i32")
                r.scalar("Fade-in time (ms)", "i32")
                r.scalar("Fade curve", "u8", enum=_CURVE)
                r.scalar("Target property", "u8", enum=property_names("object", r.version))
    _effects(r, node=False)
    if r.version <= 145:
        r.scalar("Effect slot 0", "u32", id_kind="hirc", reference_role="effect")
        r.scalar("Effect slot 0 is a ShareSet", "u8", enum=_BOOL)
        r.scalar("Override attachment parameters", "u8", enum=_BOOL)
    _metadata(r, node=False)
    _rtpc(r)
    _state_properties(r)


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
    r.scalar("Unused alternate value", "u16", editable=False, visible=False)
    r.scalar("Device allocated", "u16", enum=_BOOL)
    r.scalar("Project ID", "u32", id_kind="project", editable=False)
    if r.version >= 142:
        r.scalar("SoundBank type", "u32", enum=_BANK_TYPE)
        r.blob("SoundBank hash", 16)
    if r.pos < len(r.data):
        r.blob("Alignment padding", len(r.data) - r.pos)


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
    count = r.scalar("Plug-in count", "u32", editable=False)
    with r.section("Registered plug-ins"):
        for index in range(count):
            with r.section(f"{index + 1}"):
                plugin = r.scalar("Plug-in ID", "u32", id_kind="plugin", editable=False)
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
                    size = r.scalar("Library name bytes", "u32", editable=False)
                    r.string("Library name", size)


def _chunk_stmg(r: _Reader) -> None:
    if r.version >= 141:
        r.scalar("Filter behavior", "u16", enum=_FILTER_BEHAVIOR)
    r.scalar("Volume threshold (dB)", "f32")
    r.scalar("Maximum voices", "u16")
    r.scalar("Maximum dangerous virtual voices", "u16")

    count = r.scalar("State group count", "u32", editable=False)
    with r.section("State groups"):
        for index in range(count):
            with r.section(f"{index + 1}"):
                r.scalar("State group", "u32", id_kind="state_group")
                r.scalar("Default transition time (ms)", "u32")
                transitions = r.scalar("Transition count", "u32", editable=False)
                with r.section("Transitions"):
                    for item in range(transitions):
                        with r.section(f"{item + 1}"):
                            r.scalar("From state", "u32", id_kind="state_value")
                            r.scalar("To state", "u32", id_kind="state_value")
                            r.scalar("Transition time (ms)", "u32")

    count = r.scalar("Switch group count", "u32", editable=False)
    with r.section("Switch groups"):
        for index in range(count):
            with r.section(f"{index + 1}"):
                r.scalar("Switch group", "u32", id_kind="switch_group")
                _typed_rtpc_input(r)
                points = r.scalar("Point count", "u32", editable=False)
                _switch_graph(r, points)

    count = r.scalar("Game parameter count", "u32", editable=False)
    with r.section("Game parameters"):
        for index in range(count):
            with r.section(f"{index + 1}"):
                r.scalar("Game parameter", "u32", id_kind="game_parameter")
                r.scalar("Default value", "f32")
                r.scalar("Ramping", "u32", enum=_RAMP)
                r.scalar("Ramp up", "f32")
                r.scalar("Ramp down", "f32")
                r.scalar("Built-in parameter", "u8", enum=_BUILT_IN)

    count = r.scalar("Acoustic texture count", "u32", editable=False)
    with r.section("Acoustic textures"):
        for index in range(count):
            with r.section(f"{index + 1}"):
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
                    points = r.scalar("Point count", "u16", editable=False)
                    _graph(r, points)


def _chunk_plat(r: _Reader) -> None:
    if r.version >= 137:
        r.zstring("Platform")
    else:
        size = r.scalar("Platform name bytes", "u32", editable=False)
        r.string("Platform", size)


_CHUNK_PARSERS = {
    "BKHD": ("Bank header", _chunk_bkhd),
    "DIDX": ("Embedded media index", _chunk_didx),
    "INIT": ("Registered plug-ins", _chunk_init),
    "STMG": ("Global Wwise settings", _chunk_stmg),
    "ENVS": ("Obstruction and occlusion", _chunk_envs),
    "PLAT": ("Target platform", _chunk_plat),
}


def _sound(r: _Reader) -> None:
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
    0x10: _fx,
    0x11: _fx,
    0x12: _bus,
    0x13: _envelope,
    0x14: _envelope,
    0x15: _audio_device,
    0x16: _envelope,
}


def parse_structured_object(
    type_id: int, payload: bytes, version: int
) -> WwiseObjectLayout:
    """Return an exact field map for one supported modern HIRC payload."""

    reader = _Reader(payload, version)
    try:
        reader.scalar("Object ID", "u32", editable=False, visible=False)
        parser = _PARSERS.get(int(type_id))
        if parser is None:
            raise _LayoutError(
                f"HIRC type 0x{int(type_id):02X} has no v{version} layout"
            )
        parser(reader)
        if reader.pos != len(payload):
            raise _LayoutError(
                f"layout stopped at 0x{reader.pos:X}; payload ends at 0x{len(payload):X}"
            )
        return WwiseObjectLayout(
            tuple(reader.fields), reader.pos, len(payload), tuple(reader.anchors.items())
        )
    except (IndexError, struct.error, _LayoutError, UnicodeError) as exc:
        return WwiseObjectLayout(
            tuple(reader.fields), reader.pos, len(payload), tuple(reader.anchors.items()),
            str(exc),
        )


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
    configured = _CHUNK_PARSERS.get(key)
    if configured is None:
        return None
    title, parser = configured
    reader = _Reader(payload, version)
    try:
        parser(reader)
        if reader.pos != len(payload):
            raise _LayoutError(
                f"layout stopped at 0x{reader.pos:X}; payload ends at 0x{len(payload):X}"
            )
        structure = WwiseObjectLayout(
            tuple(reader.fields), reader.pos, len(payload), tuple(reader.anchors.items())
        )
    except (IndexError, struct.error, _LayoutError, UnicodeError) as exc:
        structure = WwiseObjectLayout(
            tuple(reader.fields), reader.pos, len(payload), tuple(reader.anchors.items()),
            str(exc),
        )
    return WwiseChunkLayout(key, title, bytes(payload), structure)


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


def set_v132_fields(payload: bytes, layout: WwiseObjectLayout, changes: dict[str, object]) -> bytes:
    """Patch editable leaves; structural counts remain owned by focused editors."""

    if not layout.complete:
        raise ValueError(f"Cannot edit an incomplete Wwise layout: {layout.error}")
    by_path = {field.path: field for field in layout.fields}
    result = bytearray(payload)
    formats = _Reader._FORMATS
    for path, raw_value in changes.items():
        field = by_path.get(path)
        if field is None:
            raise ValueError(f"Unknown Wwise field: {path}")
        if not field.editable:
            raise ValueError(f"{path} controls structure and is read-only here")
        if field.storage == "bit":
            value = int(raw_value)
            maximum = field.mask >> field.shift
            if value < 0 or value > maximum:
                raise ValueError(f"{path} must be between 0 and {maximum}")
            fmt = "<" + formats["u8" if field.size == 1 else "u32"]
            current = struct.unpack_from(fmt, result, field.offset)[0]
            current = (current & ~field.mask) | ((value << field.shift) & field.mask)
            struct.pack_into(fmt, result, field.offset, current)
            continue
        if field.storage == "var":
            packed = _pack_var(int(raw_value))
            if len(packed) != field.size:
                raise ValueError(f"{path} must keep its {field.size}-byte encoded width")
        elif field.storage == "string":
            raise ValueError(f"{path} is resized through its focused editor")
        elif field.storage == "bytes":
            packed = bytes.fromhex(str(raw_value))
            if len(packed) != field.size:
                raise ValueError(f"{path} must remain {field.size} bytes")
        else:
            value = float(raw_value) if field.storage.startswith("f") else int(raw_value, 0) if isinstance(raw_value, str) else int(raw_value)
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"{path} must be finite")
            try:
                packed = struct.pack("<" + formats[field.storage], value)
            except (KeyError, struct.error) as exc:
                raise ValueError(f"Invalid value for {path}: {raw_value}") from exc
        result[field.offset:field.offset + field.size] = packed
    return bytes(result)


set_wwise_fields = set_v132_fields


__all__ = [
    "WwiseChunkLayout", "WwiseField", "WwiseObjectLayout",
    "parse_structured_chunk", "parse_structured_object",
    "parse_v132_chunk", "parse_v132_object", "parse_v135_chunk",
    "parse_v135_object", "parse_v140_chunk", "parse_v140_object",
    "parse_v145_chunk", "parse_v145_object", "parse_v150_chunk",
    "parse_v150_object",
    "set_v132_fields", "set_wwise_fields",
]
