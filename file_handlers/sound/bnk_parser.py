"""Wwise SoundBank/File Package parsing used by the REasy sound editor.

keeps unknown data opaque and only rewrites structures we
understand. Registered HIRC schemas expose enough relationships to follow
Event -> Action -> audio container -> Sound/Music Track -> WEM routes.
"""

from __future__ import annotations

import math
import struct
from collections import Counter
from dataclasses import dataclass, field, replace

from .pck_codec import (
    PckEntry,
    _safe_slice,
    export_non_streaming_pck as export_non_streaming_pck,
    parse_pck_layout,
    rewrite_pck,
)
from .wwise_schema import (
    BNK_FX_SCHEMAS,
    BNK_PLUGIN_NAMES,
    STRUCTURED_BANK_VERSIONS,
    attenuation_targets,
    integer_properties,
    property_names,
    _ACTION_EVENT_TARGETS,
    _ACTION_EXTERNAL_TARGET_KINDS,
    _ACTION_SET_VALUE_TYPES,
    _ACTION_TYPE_NAMES,
    _AUDIO_CONTAINER_TYPES,
    _hirc_layout,
    _hirc_type_name,
    _WWISE_SILENCE_PLUGIN_ID,
)
from .wwise_v132 import (
    WwiseChunkLayout,
    WwiseObjectLayout,
    parse_structured_chunk,
    parse_structured_object,
)
from .wwise_media import (
    WwiseMediaKind,
    detect_media_kind,
    parse_convolution_reverb_media,
    parse_hybrid_reverb_media,
    parse_wwise_midi,
    validate_crankcase_rev_model,
)


_PCK_MAGIC = b"AKPK"
_RIFF_MAGIC = b"RIFF"
_WAVE_MAGIC = b"WAVE"
_BNK = "bnk"
_PCK = "pck"
_DIDX_ENTRY_FMT = "<III"

# Bus references that Wwise routinely keeps in a separate init/master-bus bank,
# so a bank is allowed to reference them without resolving them locally.
_CROSS_BANK_BUS_ROLES = frozenset({
    "auxiliary bus",
    "reflections bus",
    "output bus",
    "ducked bus",
})

# Wwise music transition rules use 0xFFFFFFFF as the "Any" source/destination
# wildcard. It is a valid sentinel value, not a broken object reference, so
# consumers must treat it as "Any" rather than as a missing object.
WWISE_ANY_OBJECT_ID = 0xFFFFFFFF


@dataclass(slots=True)
class ChunkRecord:
    chunk_id: bytes
    payload: bytes

@dataclass(slots=True)
class BnkEmbeddedAudio:
    source_id: int
    offset: int
    length: int


@dataclass(slots=True, frozen=True)
class BnkSource:
    object_id: int
    source_id: int
    plugin_id: int
    stream_type: int
    in_memory_size: int
    source_bits: int
    payload_offset: int


@dataclass(slots=True, frozen=True)
class HircReference:
    offset: int
    target_id: int
    role: str = "reference"
    target_kind: str = "hirc"


@dataclass(slots=True, frozen=True)
class BnkPlaylistItem:
    object_id: int
    weight: int = 50_000


@dataclass(slots=True, frozen=True)
class BnkRandomSequence:
    loop_count: int
    loop_min: int
    loop_max: int
    transition_ms: float
    transition_min_ms: float
    transition_max_ms: float
    avoid_repeat: int
    transition_mode: int
    random_mode: int
    mode: int
    flags: int
    playlist: tuple[BnkPlaylistItem, ...]
    settings_offset: int
    playlist_offset: int
    playlist_end: int


@dataclass(slots=True, frozen=True)
class BnkSwitchMapping:
    value_id: int
    object_ids: tuple[int, ...]


@dataclass(slots=True, frozen=True)
class BnkSwitchParam:
    object_id: int
    flags: int
    mode: int
    fade_out_ms: int
    fade_in_ms: int


@dataclass(slots=True, frozen=True)
class BnkSwitchContainer:
    group_type: int
    group_id: int
    default_value_id: int
    continuous_validation: bool
    mappings: tuple[BnkSwitchMapping, ...]
    params: tuple[BnkSwitchParam, ...]
    settings_offset: int
    data_end: int


@dataclass(slots=True, frozen=True)
class BnkMusicMarker:
    marker_id: int
    position_ms: float
    name: str = ""


@dataclass(slots=True, frozen=True)
class BnkMusicSegment:
    duration_ms: float
    markers: tuple[BnkMusicMarker, ...]
    duration_offset: int
    markers_offset: int
    markers_end: int


@dataclass(slots=True, frozen=True)
class BnkMusicClip:
    track_id: int
    source_id: int
    play_at_ms: float
    begin_trim_ms: float
    end_trim_ms: float
    source_duration_ms: float
    event_id: int = 0


@dataclass(slots=True, frozen=True)
class BnkMusicTrack:
    clips: tuple[BnkMusicClip, ...]
    subtrack_count: int
    playlist_offset: int
    playlist_end: int


@dataclass(slots=True, frozen=True)
class BnkPropertyValue:
    property_id: int
    value_bits: int


@dataclass(slots=True, frozen=True)
class BnkPropertyRange:
    property_id: int
    minimum_bits: int
    maximum_bits: int


@dataclass(slots=True, frozen=True)
class BnkPropertyBundle:
    values: tuple[BnkPropertyValue, ...]
    ranges: tuple[BnkPropertyRange, ...]
    offset: int
    end: int
    has_ranges: bool = True
    kind: str = "object"
    bank_version: int | None = None
    id_width: int = 1


@dataclass(slots=True, frozen=True)
class BnkActionSettings:
    kind: str
    fade_curve: int | None
    flags: int
    offset: int
    bank_id: int | None = None
    stop_flags: int | None = None
    group_id: int | None = None
    value_id: int | None = None
    parameter_id: int | None = None
    bypass_transition: bool | None = None
    value_meaning: int | None = None
    value: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    bypass: bool | None = None
    target_mask: int | None = None
    relative_to_duration: bool | None = None
    snap_to_marker: bool | None = None
    exceptions: tuple[tuple[int, bool], ...] = ()
    end: int | None = None


@dataclass(slots=True, frozen=True)
class BnkGraphPoint:
    x: float
    y: float
    interpolation: int


@dataclass(slots=True, frozen=True)
class BnkAttenuationCurve:
    scaling: int
    points: tuple[BnkGraphPoint, ...]


@dataclass(slots=True, frozen=True)
class BnkAttenuation:
    cone_flags: int
    cone: tuple[float, float, float, float, float] | None
    assignments: tuple[int, ...]
    curves: tuple[BnkAttenuationCurve, ...]
    offset: int
    end: int


@dataclass(slots=True, frozen=True)
class BnkSilenceSource:
    duration_seconds: float
    random_minus_seconds: float
    random_plus_seconds: float
    offset: int


@dataclass(slots=True, frozen=True)
class BnkFxParameter:
    name: str
    storage: str
    value: float | int
    enum_name: str | None = None


@dataclass(slots=True, frozen=True)
class BnkFxPlugin:
    plugin_id: int
    name: str
    parameters: tuple[BnkFxParameter, ...]
    offset: int
    end: int


@dataclass(slots=True)
class HircObject:
    index: int
    type_id: int
    type_name: str
    object_id: int
    payload: bytes
    payload_offset: int
    event_action_ids: tuple[int, ...] = ()
    action_type: int | None = None
    action_name: str | None = None
    action_target_id: int | None = None
    action_target_kind: str = "object"
    action_raw_id: int | None = None
    action_target_is_bus: bool = False
    child_ids: tuple[int, ...] = ()
    child_list_offset: int | None = None
    parent_id: int | None = None
    parent_reference_offset: int | None = None
    references: tuple[int, ...] = ()
    reference_fields: tuple[HircReference, ...] = ()
    sources: tuple[BnkSource, ...] = ()
    random_sequence: BnkRandomSequence | None = None
    switch_container: BnkSwitchContainer | None = None
    music_segment: BnkMusicSegment | None = None
    music_track: BnkMusicTrack | None = None
    property_bundle: BnkPropertyBundle | None = None
    action_settings: BnkActionSettings | None = None
    attenuation: BnkAttenuation | None = None
    silence_source: BnkSilenceSource | None = None
    fx_plugin: BnkFxPlugin | None = None
    structure: WwiseObjectLayout | None = None
    bank_version: int | None = None


_PLAYBACK_TARGET_TYPES = frozenset({
    0x02, 0x05, 0x06, 0x07, 0x09, 0x0A, 0x0C, 0x0D,
})
_ACTOR_CHILD_TYPES = frozenset({0x02, 0x05, 0x06, 0x07, 0x09})
_ACTOR_PARENT_TYPES = frozenset({0x05, 0x06, 0x07, 0x09})
_MUSIC_HIERARCHY_TYPES = frozenset({0x0A, 0x0C, 0x0D})
# Music Switch Containers may nest, so they accept Segments, Random/Sequence
# Containers, and other Switch Containers as children. Music Random/Sequence
# Containers accept only Segments.
_MUSIC_SWITCH_CHILD_TYPES = frozenset({0x0A, 0x0C, 0x0D})
_MUSIC_RANSEQ_CHILD_TYPES = frozenset({0x0A})


def compatible_hirc_reference_types(
    owner: HircObject, field: HircReference
) -> frozenset[int]:
    """Return the HIRC types valid for one decoded reference field.

    Unknown roles deliberately return no types
    """

    role = field.role
    if field.target_kind == "event" or role in {"event target", "clip event"}:
        return frozenset({0x04})
    if field.target_kind != "hirc":
        return frozenset()

    type_names, bus_types, plugin_types, modulator_types = _hirc_layout(
        owner.bank_version
    )
    devices = frozenset(
        type_id for type_id in plugin_types
        if type_names.get(type_id) == "Audio Device"
    )
    effects = frozenset(plugin_types) - devices
    auxiliary_buses = frozenset(
        type_id for type_id in bus_types
        if type_names.get(type_id) == "Auxiliary Bus"
    )

    if role == "state object":
        return frozenset({0x01})
    if role in {"effect", "metadata effect", "attached effect"}:
        return effects
    if role in {"auxiliary bus", "reflections bus"}:
        return auxiliary_buses
    if role == "output bus":
        # Some RE9 nodes route their output bus directly to an Audio Device
        # (e.g. 0xE611314A in init), so a device is a valid output target in
        # addition to the bus types.
        return frozenset(bus_types) | devices
    if role == "ducked bus":
        return frozenset(bus_types)
    if role == "audio device":
        return devices
    if role == "attenuation":
        return frozenset({0x0E})
    if role == "modulator":
        return frozenset(modulator_types)
    if role == "MIDI target":
        return _PLAYBACK_TARGET_TYPES
    if role in {"stinger segment", "transition segment", "playlist segment"}:
        return frozenset({0x0A})
    if role in {"transition source", "transition destination"}:
        return _MUSIC_HIERARCHY_TYPES
    if role in {"playlist item", "switch assignment", "switch child", "layer child"}:
        return _ACTOR_CHILD_TYPES
    if role == "event action":
        return frozenset({0x03})
    if role == "action target":
        return frozenset(bus_types) if owner.action_target_is_bus else _PLAYBACK_TARGET_TYPES
    if role == "exception":
        exceptions = [item for item in owner.reference_fields if item.role == role]
        try:
            is_bus = owner.action_settings.exceptions[exceptions.index(field)][1]
        except (AttributeError, IndexError, ValueError):
            return frozenset()
        return frozenset(bus_types) if is_bus else _PLAYBACK_TARGET_TYPES
    if role == "child":
        if owner.type_id == 0x0A:
            return frozenset({0x0B})
        if owner.type_id in _ACTOR_PARENT_TYPES:
            return _ACTOR_CHILD_TYPES
        if owner.type_id == 0x0C:
            return _MUSIC_SWITCH_CHILD_TYPES
        if owner.type_id == 0x0D:
            return _MUSIC_RANSEQ_CHILD_TYPES
        return frozenset()
    if role == "parent":
        if owner.type_id == 0x0B:
            return frozenset({0x0A})
        if owner.type_id in _ACTOR_CHILD_TYPES:
            return _ACTOR_PARENT_TYPES
        if owner.type_id == 0x0A:
            return frozenset({0x0C, 0x0D})
        if owner.type_id == 0x0D:
            return frozenset({0x0C})
        if owner.type_id == 0x0C:
            # Nested Music Switch Containers are the only valid parent for a
            # Music Switch Container (a Segment/RanSeq never parents a Switch).
            return frozenset({0x0C})
        if owner.type_id in bus_types:
            return frozenset(bus_types)
    return frozenset()


def compatible_hirc_reference_targets(
    owner: HircObject,
    field: HircReference,
    objects,
) -> tuple[HircObject, ...]:
    """Return unambiguous local objects valid for a decoded reference."""

    objects = tuple(objects)
    counts = Counter(obj.object_id for obj in objects)
    allowed = compatible_hirc_reference_types(owner, field)
    return tuple(
        obj for obj in objects
        if counts[obj.object_id] == 1
        and obj.object_id != owner.object_id
        and obj.type_id in allowed
    )


@dataclass(slots=True, frozen=True)
class BnkAction:
    object_id: int
    action_type: int
    action_name: str
    target_id: int
    target_is_bus: bool = False
    target_kind: str = "object"
    raw_id: int = 0
    settings: BnkActionSettings | None = None


@dataclass(slots=True, frozen=True)
class BnkEvent:
    object_id: int
    action_ids: tuple[int, ...]
    source_ids: tuple[int, ...] = ()
    reachable_object_ids: tuple[int, ...] = ()
    unresolved_ids: tuple[int, ...] = ()


@dataclass(slots=True)
class BnkTrack:
    index: int
    source_id: int
    offset: int
    length: int
    absolute_offset: bool = False
    available: bool = True
    payload_complete: bool = True
    storage: str = "embedded"
    stream_type: int | None = None
    plugin_id: int | None = None
    media_kind: WwiseMediaKind = WwiseMediaKind.UNKNOWN
    object_ids: tuple[int, ...] = ()
    event_ids: tuple[int, ...] = ()


@dataclass(slots=True)
class PckEmbeddedBank:
    entry: PckEntry
    result: BnkParseResult | None = None


@dataclass(slots=True)
class BnkParseResult:
    bank_version: int | None
    tracks: list[BnkTrack]
    container_type: str = _BNK
    has_embedded_data: bool = True
    bank_id: int | None = None
    language_id: int | None = None
    project_id: int | None = None
    objects: list[HircObject] = field(default_factory=list)
    events: list[BnkEvent] = field(default_factory=list)
    actions: list[BnkAction] = field(default_factory=list)
    pck_entries: list[PckEntry] = field(default_factory=list)
    embedded_banks: list[PckEmbeddedBank] = field(default_factory=list)
    bank_chunks: list[WwiseChunkLayout] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class WemMetadata:
    codec: str
    channels: int | None
    sample_rate: int | None
    duration_seconds: float | None
    media_kind: WwiseMediaKind = WwiseMediaKind.UNKNOWN
    details: str = ""


@dataclass(slots=True)
class _HircReadResult:
    objects: list[HircObject]
    trailing: bytes
    complete: bool


def rewrite_soundbank(
    data: bytes,
    replacements: dict[int, bytes],
    *,
    event_actions: dict[int, list[int] | tuple[int, ...]] | None = None,
    action_targets: dict[int, int] | None = None,
    deleted_event_ids: set[int] | None = None,
    hirc_upserts: dict[int | tuple[int, int], tuple[int, bytes]] | None = None,
    deleted_hirc_ids: set[int] | None = None,
    renamed_hirc_ids: dict[int, int] | None = None,
    reference_targets: dict[tuple[int, int], int] | None = None,
    bank_chunk_payloads: dict[bytes | str, bytes] | None = None,
) -> bytes:
    """Rewrite supported media/HIRC fields while preserving opaque structures."""

    event_actions = event_actions or {}
    action_targets = action_targets or {}
    deleted_event_ids = deleted_event_ids or set()
    hirc_upserts = hirc_upserts or {}
    deleted_hirc_ids = (deleted_hirc_ids or set()) | deleted_event_ids
    renamed_hirc_ids = renamed_hirc_ids or {}
    reference_targets = reference_targets or {}
    bank_chunk_payloads = bank_chunk_payloads or {}
    has_hirc_edits = any(
        (event_actions, action_targets, hirc_upserts, deleted_hirc_ids,
         renamed_hirc_ids, reference_targets)
    )
    if not replacements and not has_hirc_edits and not bank_chunk_payloads:
        return bytes(data)
    if data[:4] == _PCK_MAGIC:
        if has_hirc_edits or bank_chunk_payloads:
            raise ValueError("Wwise graph/settings edits require a BNK, not a PCK")
        return rewrite_pck(data, replacements)
    return _rewrite_bnk(
        data,
        replacements,
        event_actions=event_actions,
        action_targets=action_targets,
        hirc_upserts=hirc_upserts,
        deleted_hirc_ids=deleted_hirc_ids,
        renamed_hirc_ids=renamed_hirc_ids,
        reference_targets=reference_targets,
        bank_chunk_payloads=bank_chunk_payloads,
    )


def parse_soundbank(data: bytes) -> BnkParseResult:
    return parse_pck(data) if data[:4] == _PCK_MAGIC else parse_bnk(data)


def parse_bnk(data: bytes) -> BnkParseResult:
    chunks = _read_chunks(data)
    warnings: list[str] = []
    version = _read_bank_version(chunks.get("BKHD"))
    bank_id, language_id, project_id = _read_bank_header(chunks.get("BKHD"), version)
    media = _read_didx(chunks.get("DIDX"))
    indexed = {entry.source_id: entry for entry in media}

    hirc = _read_hirc_objects(chunks.get("HIRC"), warnings, version)
    _decode_hirc_objects(hirc.objects, version)
    if hirc.objects and version not in STRUCTURED_BANK_VERSIONS:
        warnings.append(
            f"No structured HIRC schema is registered for Wwise bank version {version}"
        )
    events = _resolve_events(hirc.objects)
    actions = [
        BnkAction(
            object_id=obj.object_id,
            action_type=obj.action_type or 0,
            action_name=obj.action_name or "Unknown",
            target_id=obj.action_target_id or 0,
            target_is_bus=obj.action_target_is_bus,
            target_kind=obj.action_target_kind,
            raw_id=obj.action_raw_id or 0,
            settings=obj.action_settings,
        )
        for obj in hirc.objects
        if obj.type_id == 0x03 and obj.action_type is not None
    ]

    source_records: dict[int, list[BnkSource]] = {}
    for obj in hirc.objects:
        for source in obj.sources:
            if source.source_id:
                source_records.setdefault(source.source_id, []).append(source)

    event_ids_by_source: dict[int, list[int]] = {}
    for event in events:
        for source_id in event.source_ids:
            event_ids_by_source.setdefault(source_id, []).append(event.object_id)

    ordered_source_ids = list(source_records)
    ordered_source_ids.extend(
        entry.source_id for entry in media if entry.source_id not in source_records
    )

    tracks: list[BnkTrack] = []
    for index, source_id in enumerate(ordered_source_ids, 1):
        records = source_records.get(source_id, [])
        embedded = indexed.get(source_id)
        payload = (
            _safe_slice(chunks.get("DATA") or b"", embedded.offset, embedded.length)
            if embedded else b""
        )
        available = bool(payload)
        stream_type = records[0].stream_type if records else None
        plugin_id = records[0].plugin_id if records else None
        storage = _source_storage(stream_type, embedded=embedded is not None)
        tracks.append(
            BnkTrack(
                index=index,
                source_id=source_id,
                offset=embedded.offset if embedded else 0,
                length=embedded.length if embedded else 0,
                available=available,
                payload_complete=available and stream_type != 1,
                storage=storage,
                stream_type=stream_type,
                plugin_id=plugin_id,
                media_kind=detect_media_kind(payload, plugin_id),
                object_ids=tuple(dict.fromkeys(record.object_id for record in records)),
                event_ids=tuple(dict.fromkeys(event_ids_by_source.get(source_id, ()))),
            )
        )

    if chunks.get("HIRC") and not hirc.complete:
        warnings.append("HIRC ended before all declared objects were read")

    bank_chunks = []
    if version in {132, 135, 140, 145, 150}:
        for record in _read_chunk_records(data):
            decoded = parse_structured_chunk(record.chunk_id, record.payload, version)
            if decoded is None:
                continue
            bank_chunks.append(decoded)
            if not decoded.structure.complete:
                warnings.append(
                    f"{decoded.chunk_id} settings layout is incomplete: "
                    f"{decoded.structure.error}"
                )

    return BnkParseResult(
        bank_version=version,
        tracks=tracks,
        has_embedded_data=any(track.available for track in tracks),
        bank_id=bank_id,
        language_id=language_id,
        project_id=project_id,
        objects=hirc.objects,
        events=events,
        actions=actions,
        bank_chunks=bank_chunks,
        warnings=warnings,
    )


def parse_pck(data: bytes) -> BnkParseResult:
    layout = parse_pck_layout(data)
    if layout is None:
        return BnkParseResult(
            bank_version=None,
            tracks=[],
            container_type=_PCK,
            has_embedded_data=False,
            warnings=["Invalid or truncated AKPK header"],
        )

    all_entries = [entry for table in layout.tables for entry in table.entries]
    media_entries = [entry for entry in all_entries if entry.table_kind != "banks"]
    tracks = [
        BnkTrack(
            index=index,
            source_id=entry.entry_id,
            offset=entry.offset,
            length=entry.length,
            absolute_offset=True,
            available=entry.available,
            payload_complete=entry.available,
            storage="packaged" if entry.available else (
                "external" if entry.table_kind == "externals" else "streamed placeholder"
            ),
            media_kind=detect_media_kind(
                _safe_slice(data, entry.offset, entry.length) if entry.available else b""
            ),
        )
        for index, entry in enumerate(media_entries, 1)
    ]

    embedded_banks: list[PckEmbeddedBank] = []
    warnings: list[str] = []
    for entry in (item for item in all_entries if item.table_kind == "banks"):
        payload = _safe_slice(data, entry.offset, entry.length)
        result = None
        if payload.startswith(b"BKHD"):
            try:
                result = parse_bnk(payload)
            except (ValueError, struct.error) as exc:
                warnings.append(f"Embedded bank {entry.entry_id} could not be parsed: {exc}")
        embedded_banks.append(PckEmbeddedBank(entry=entry, result=result))

    return BnkParseResult(
        bank_version=layout.version,
        tracks=tracks,
        container_type=_PCK,
        has_embedded_data=any(track.available for track in tracks),
        pck_entries=all_entries,
        embedded_banks=embedded_banks,
        warnings=warnings,
    )


def get_data_chunk(data: bytes) -> bytes | None:
    return _read_chunks(data).get("DATA")


def extract_embedded_wem(data: bytes, track: BnkTrack) -> bytes:
    if not track.available:
        return b""
    if track.absolute_offset:
        return _safe_slice(data, track.offset, track.length)
    chunk = get_data_chunk(data)
    return _safe_slice(chunk, track.offset, track.length) if chunk else b""



def parse_wem_metadata(data: bytes, plugin_id: int | None = None) -> WemMetadata:
    kind = detect_media_kind(data, plugin_id)
    unknown = WemMetadata(
        codec="Unknown", channels=None, sample_rate=None, duration_seconds=None,
        media_kind=kind,
    )
    if kind == WwiseMediaKind.MIDI:
        try:
            midi = parse_wwise_midi(data)
        except ValueError:
            return replace(unknown, codec="Invalid Wwise MIDI")
        return WemMetadata(
            codec="Wwise MIDI",
            channels=None,
            sample_rate=None,
            duration_seconds=midi.duration_seconds,
            media_kind=kind,
            details=(
                f"{len(midi.tracks)} track(s), {midi.note_count} note(s), "
                f"{midi.bpm:g} BPM, PPQ {midi.division}"
            ),
        )
    if kind == WwiseMediaKind.HYBRID_REVERB_IR:
        try:
            impulse = parse_hybrid_reverb_media(data)
        except ValueError:
            return replace(unknown, codec="Invalid Hybrid Reverb IR")
        return WemMetadata(
            codec="iZotope Hybrid Reverb IR",
            channels=impulse.channels,
            sample_rate=48_000,
            duration_seconds=impulse.frame_count / 48_000,
            media_kind=kind,
            details=f"{impulse.frame_count} processed early-reflection frames",
        )
    if kind == WwiseMediaKind.CONVOLUTION_REVERB_IR:
        try:
            impulse = parse_convolution_reverb_media(data)
        except ValueError:
            return replace(unknown, codec="Invalid Wwise Convolution Reverb IR")
        return WemMetadata(
            codec="Wwise Convolution Reverb IR",
            channels=impulse.channels,
            sample_rate=impulse.sample_rate,
            duration_seconds=impulse.duration_seconds,
            media_kind=kind,
            details=(
                f"{impulse.sample_count} processed frames, "
                f"block size {impulse.block_size}, estimated RT60 "
                f"{impulse.estimated_rt60_seconds:g} s"
            ),
        )
    if kind == WwiseMediaKind.CRANKCASE_REV_MODEL:
        try:
            validate_crankcase_rev_model(data)
        except ValueError:
            return replace(unknown, codec="Invalid Crankcase Audio REV model")
        return WemMetadata(
            codec="Crankcase Audio REV model",
            channels=None,
            sample_rate=None,
            duration_seconds=None,
            media_kind=kind,
            details=f"ADM3 · {len(data):,} compiled bytes",
        )
    if kind != WwiseMediaKind.AUDIO:
        return unknown
    if len(data) < 12 or data[:4] != _RIFF_MAGIC or data[8:12] != _WAVE_MAGIC:
        return unknown
    pos, fmt_chunk, data_size = 12, None, None
    while pos + 8 <= len(data):
        chunk_id = data[pos:pos + 4]
        chunk_size = struct.unpack_from("<I", data, pos + 4)[0]
        pos += 8
        end = pos + chunk_size
        if end > len(data):
            break
        if chunk_id == b"fmt ":
            fmt_chunk = data[pos:end]
        elif chunk_id == b"data":
            data_size = chunk_size
        pos = end + (chunk_size & 1)
    if not fmt_chunk or len(fmt_chunk) < 16:
        return unknown
    tag, channels, sample_rate, average_bps = struct.unpack_from("<HHII", fmt_chunk, 0)
    duration = (data_size / average_bps) if data_size and average_bps else None
    return WemMetadata(
        codec=f"0x{tag:04X}",
        channels=channels or None,
        sample_rate=sample_rate or None,
        duration_seconds=duration,
        media_kind=kind,
    )


def wwise_id_from_name(name: str) -> int:
    """Return Audiokinetic's lowercase 32-bit FNV-1 ShortID."""

    value = 2166136261
    for byte in name.lower().encode("utf-8"):
        value = ((value * 16777619) ^ byte) & 0xFFFFFFFF
    return value


def _split_chunk_records(data: bytes) -> tuple[list[ChunkRecord], bytes]:
    chunks: list[ChunkRecord] = []
    pos, size = 0, len(data)
    while pos + 8 <= size:
        chunk_id = data[pos:pos + 4]
        length = struct.unpack_from("<I", data, pos + 4)[0]
        payload_pos = pos + 8
        end = payload_pos + length
        if end > size:
            break
        chunks.append(ChunkRecord(chunk_id=chunk_id, payload=data[payload_pos:end]))
        pos = end
    return chunks, data[pos:]


def _read_chunk_records(data: bytes) -> list[ChunkRecord]:
    return _split_chunk_records(data)[0]


def _read_chunks(data: bytes) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for record in _read_chunk_records(data):
        if record.chunk_id.isascii():
            result[record.chunk_id.decode("ascii")] = record.payload
    return result


def _pack_chunk_records(chunks: list[ChunkRecord], trailing: bytes = b"") -> bytes:
    out = bytearray()
    for chunk in chunks:
        out += chunk.chunk_id + struct.pack("<I", len(chunk.payload)) + chunk.payload
    out += trailing
    return bytes(out)


def _read_bank_version(chunk: bytes | None) -> int | None:
    return struct.unpack_from("<I", chunk, 0)[0] if chunk and len(chunk) >= 4 else None


def _read_bank_header(
    chunk: bytes | None, version: int | None
) -> tuple[int | None, int | None, int | None]:
    if not chunk or version is None or version <= 26 or len(chunk) < 12:
        return None, None, None
    bank_id = struct.unpack_from("<I", chunk, 4)[0]
    language_id = struct.unpack_from("<I", chunk, 8)[0]
    project_id = struct.unpack_from("<I", chunk, 16)[0] if version >= 77 and len(chunk) >= 20 else None
    return bank_id, language_id, project_id


def _read_didx(chunk: bytes | None) -> list[BnkEmbeddedAudio]:
    if not chunk:
        return []
    size = struct.calcsize(_DIDX_ENTRY_FMT)
    return [
        BnkEmbeddedAudio(*struct.unpack_from(_DIDX_ENTRY_FMT, chunk, pos))
        for pos in range(0, len(chunk) - len(chunk) % size, size)
    ]


def _read_hirc_objects(
    chunk: bytes | None,
    warnings: list[str] | None = None,
    bank_version: int | None = None,
) -> _HircReadResult:
    if not chunk or len(chunk) < 4:
        return _HircReadResult([], b"", not chunk or len(chunk) == 0)
    warnings = warnings if warnings is not None else []
    count = struct.unpack_from("<I", chunk, 0)[0]
    pos = 4
    objects: list[HircObject] = []
    complete = True
    for index in range(count):
        header_size = 5
        if pos + header_size > len(chunk):
            complete = False
            break
        type_id = chunk[pos]
        section_size = struct.unpack_from("<I", chunk, pos + 1)[0]
        payload_pos = pos + header_size
        end = payload_pos + section_size
        if end > len(chunk):
            complete = False
            warnings.append(
                f"HIRC object {index + 1} type 0x{type_id:02X} extends past the chunk"
            )
            break
        payload = chunk[payload_pos:end]
        object_id = struct.unpack_from("<I", payload, 0)[0] if len(payload) >= 4 else 0
        objects.append(
            HircObject(
                index=index + 1,
                type_id=type_id,
                type_name=_hirc_type_name(type_id, bank_version),
                object_id=object_id,
                payload=payload,
                payload_offset=payload_pos,
            )
        )
        pos = end
    return _HircReadResult(objects, chunk[pos:], complete and len(objects) == count)


def _decode_hirc_objects(objects: list[HircObject], bank_version: int | None = None) -> None:
    if bank_version not in STRUCTURED_BANK_VERSIONS:
        return
    _type_names, bus_types, fx_types, modulator_types = _hirc_layout(bank_version)
    by_id = {obj.object_id: obj for obj in objects if obj.object_id}
    known_ids = set(by_id)
    for obj in objects:
        obj.bank_version = bank_version
        obj.type_name = _hirc_type_name(obj.type_id, bank_version)
        if bank_version in {132, 135, 140, 145, 150}:
            obj.structure = parse_structured_object(
                obj.type_id, obj.payload, bank_version
            )
        if obj.type_id == 0x04:
            obj.event_action_ids = _parse_event_action_ids(obj.payload)
        elif obj.type_id == 0x01:
            obj.property_bundle = _parse_property_bundle(
                obj.payload, 4, has_ranges=False, kind="state",
                bank_version=bank_version, id_width=2 if bank_version >= 128 else 1,
            )
        elif obj.type_id == 0x03:
            _decode_action(obj)
        elif obj.type_id == 0x02:
            source, _ = _read_bank_source(obj.payload, 4, obj.object_id, bank_version)
            obj.sources = (source,) if source else ()
        elif obj.type_id == 0x0B:
            obj.sources = _read_music_track_sources(obj.payload, obj.object_id, bank_version)
        elif obj.type_id in bus_types:
            # CAkBus places its output-bus/device IDs directly before the same
            # AkPropValue bundles used by hierarchy nodes.
            structured_offset = (
                obj.structure.anchor("property_bundle")
                if obj.structure and obj.structure.complete else None
            )
            offset = structured_offset if structured_offset is not None else (
                12 if bank_version >= 128 and len(obj.payload) >= 12
                and not struct.unpack_from("<I", obj.payload, 4)[0] else 8
            )
            obj.property_bundle = _parse_property_bundle(
                obj.payload, offset, has_ranges=False, bank_version=bank_version
            )
        elif obj.type_id == 0x0E:
            obj.attenuation = _parse_attenuation(obj.payload, bank_version)
        elif obj.type_id in modulator_types:
            obj.property_bundle = _parse_property_bundle(
                obj.payload, 4, kind="modulator", bank_version=bank_version
            )
        elif obj.type_id in fx_types:
            obj.silence_source = _parse_silence_source(obj.payload)
            if obj.silence_source is None:
                obj.fx_plugin = _parse_fx_plugin(obj.payload)

    for obj in objects:
        if obj.type_id in _AUDIO_CONTAINER_TYPES:
            if obj.structure and obj.structure.complete:
                obj.child_list_offset = obj.structure.anchor("child_count")
                obj.child_ids = tuple(
                    int(field.value) for field in obj.structure.fields
                    if field.reference_role == "child"
                )
            else:
                obj.child_list_offset, obj.child_ids = _find_child_list(obj, by_id)
            if obj.child_list_offset is not None:
                if obj.type_id == 0x05:
                    obj.random_sequence = _parse_random_sequence(obj)
                elif obj.type_id == 0x06:
                    obj.switch_container = _parse_switch_container(obj)
                elif obj.type_id == 0x0A:
                    obj.music_segment = _parse_music_segment(obj)
        if obj.type_id == 0x0B:
            obj.music_track = _parse_music_track(obj, bank_version)

    for obj in objects:
        _decode_node_base(obj, known_ids)

        if obj.structure and obj.structure.complete:
            parent = next((
                field for field in obj.structure.fields
                if field.reference_role == "parent"
            ), None)
            if parent is not None:
                obj.parent_id = int(parent.value)
                obj.parent_reference_offset = parent.offset

    for parent in objects:
        for child_id in parent.child_ids:
            child = by_id.get(child_id)
            if child is None or child.parent_id is not None:
                continue
            offsets = _matching_u32_offsets(child.payload, parent.object_id, 4)
            if len(offsets) == 1:
                child.parent_id = parent.object_id
                child.parent_reference_offset = offsets[0]

    for obj in objects:
        if obj.structure and obj.structure.complete:
            fields = tuple(
                HircReference(
                    field.offset, int(field.value), field.reference_role,
                    field.id_kind,
                )
                for field in obj.structure.fields
                if field.reference_role
                and int(field.value)
                and int(field.value) != obj.object_id
            )
            obj.reference_fields = fields
            obj.references = tuple(dict.fromkeys(field.target_id for field in fields))
            continue
        if obj.type_id == 0x03:
            fields: list[HircReference] = []
            if obj.action_target_id:
                role = (
                    "event target"
                    if obj.action_target_kind == "event"
                    else "action target"
                )
                fields.append(HircReference(
                    6,
                    obj.action_target_id,
                    role,
                    "event" if obj.action_target_kind == "event" else "hirc",
                ))
            settings = obj.action_settings
            if settings:
                for exception_id, _is_bus in settings.exceptions:
                    if exception_id in known_ids and exception_id != obj.object_id:
                        offsets = _matching_u32_offsets(
                            obj.payload, exception_id, settings.offset
                        )
                        if len(offsets) == 1:
                            fields.append(
                                HircReference(offsets[0], exception_id, "exception")
                            )
            obj.reference_fields = tuple(fields)
            obj.references = tuple(
                dict.fromkeys(field.target_id for field in fields)
            )
            continue
        roles: dict[int, str] = {}
        if obj.type_id == 0x04:
            parsed = _read_varuint(obj.payload, 4)
            if parsed:
                count, pos = parsed
                roles.update((pos + index * 4, "event action") for index in range(count))
        if obj.child_list_offset is not None:
            roles.update(
                (obj.child_list_offset + 4 + index * 4, "child")
                for index in range(len(obj.child_ids))
            )
        if obj.parent_reference_offset is not None:
            output_bus_offset = obj.parent_reference_offset - 4
            if output_bus_offset >= 4:
                roles[output_bus_offset] = "output bus"
            roles[obj.parent_reference_offset] = "parent"

        fields = []
        for pos in range(4, max(4, len(obj.payload) - 3)):
            value = struct.unpack_from("<I", obj.payload, pos)[0]
            if value in known_ids and value != obj.object_id:
                fields.append(HircReference(pos, value, roles.get(pos, "reference")))
        obj.reference_fields = tuple(fields)
        obj.references = tuple(dict.fromkeys(field.target_id for field in fields))


def _parse_event_action_ids(payload: bytes) -> tuple[int, ...]:
    if len(payload) < 5:
        return ()
    parsed = _read_varuint(payload, 4)
    if parsed is None:
        return ()
    count, pos = parsed
    if count > (len(payload) - pos) // 4:
        return ()
    return tuple(struct.unpack_from(f"<{count}I", payload, pos)) if count else ()


def _parse_attenuation(
    payload: bytes, bank_version: int | None = 125
) -> BnkAttenuation | None:
    """Decode CAkAttenuation's cone and versioned distance/acoustics curves."""

    offset = 5 if (bank_version or 0) >= 137 else 4
    pos = offset
    if pos >= len(payload):
        return None
    cone_flags = payload[pos]
    pos += 1
    cone = None
    if cone_flags & 1:
        if pos + 20 > len(payload):
            return None
        cone = struct.unpack_from("<5f", payload, pos)
        pos += 20
    targets = attenuation_targets(bank_version)
    if pos + len(targets) + 1 > len(payload):
        return None
    assignments = struct.unpack_from(f"<{len(targets)}b", payload, pos)
    pos += len(assignments)
    curve_count = payload[pos]
    pos += 1
    curves = []
    for _ in range(curve_count):
        if pos + 3 > len(payload):
            return None
        scaling, point_count = struct.unpack_from("<BH", payload, pos)
        pos += 3
        if point_count > (len(payload) - pos) // 12:
            return None
        points = tuple(
            BnkGraphPoint(*struct.unpack_from("<ffI", payload, pos + index * 12))
            for index in range(point_count)
        )
        pos += point_count * 12
        curves.append(BnkAttenuationCurve(scaling, points))
    if any(index < -1 or index >= curve_count for index in assignments):
        return None
    return BnkAttenuation(cone_flags, cone, assignments, tuple(curves), offset, pos)


def _parse_silence_source(payload: bytes) -> BnkSilenceSource | None:
    if len(payload) < 24:
        return None
    plugin_id, parameter_size = struct.unpack_from("<II", payload, 4)
    if plugin_id != _WWISE_SILENCE_PLUGIN_ID or parameter_size != 12:
        return None
    return BnkSilenceSource(*struct.unpack_from("<3f", payload, 12), 12)


_FX_STORAGE_FORMAT = {
    "f32": "f", "u32": "I", "i32": "i", "i16": "h", "u16": "H",
    "u8": "B", "bool": "B",
}


def _parse_fx_plugin(payload: bytes) -> BnkFxPlugin | None:
    if len(payload) < 12:
        return None
    plugin_id, parameter_size = struct.unpack_from("<II", payload, 4)
    schema = BNK_FX_SCHEMAS.get(plugin_id)
    if schema is None:
        if plugin_id in {0x00770002, 0x00780002, 0x00AB0003} and 12 + parameter_size <= len(payload):
            return BnkFxPlugin(
                plugin_id, BNK_PLUGIN_NAMES[plugin_id], (), 12, 12 + parameter_size
            )
        return None
    name, fields = schema
    if plugin_id == 0x006E1003 and parameter_size == 139:
        fields = fields[:12] + fields[16:-1]
    if plugin_id == 0x00730003 and parameter_size >= 29:
        delay_count = struct.unpack_from("<I", payload, 20)[0]
        delay_mode = struct.unpack_from("<I", payload, 37)[0]
        if delay_mode == 1 and delay_count in {4, 8, 12, 16}:
            fields += tuple(
                (f"Delay time {index + 1}", "f32")
                for index in range(delay_count)
            )
    if plugin_id == 0x007F0003 and parameter_size == 57:
        fields = fields + (("Block size", "u8", "convolution_block_size"),)
    elif plugin_id == 0x00810003 and parameter_size == 28:
        fields = fields[:5] + (("Infinite hold", "bool"),) + fields[5:]
    if parameter_size == 0:
        return BnkFxPlugin(plugin_id, name, (), 12, 12)
    fmt = "<" + "".join(_FX_STORAGE_FORMAT[field[1]] for field in fields)
    if parameter_size != struct.calcsize(fmt) or 12 + parameter_size > len(payload):
        return None
    values = struct.unpack_from(fmt, payload, 12)
    parameters = tuple(
        BnkFxParameter(field[0], field[1], value, field[2] if len(field) > 2 else None)
        for field, value in zip(fields, values)
    )
    if any(item.storage == "bool" and item.value not in {0, 1} for item in parameters):
        return None
    return BnkFxPlugin(plugin_id, name, parameters, 12, 12 + parameter_size)


def _action_target_kind(type_key: int, bank_version: int | None = None) -> str:
    if (bank_version or 0) >= 150 and type_key == 0x1B00:
        return "trigger"
    if type_key in _ACTION_EXTERNAL_TARGET_KINDS:
        if (bank_version or 0) >= 150 and type_key == 0x1D00:
            return "object"
        return _ACTION_EXTERNAL_TARGET_KINDS[type_key]
    if type_key in _ACTION_EVENT_TARGETS and not (
        (bank_version or 0) >= 150 and type_key in {0x1500, 0x1600, 0x1700}
    ):
        return "event"
    return "object"


def _parse_action_exceptions(
    payload: bytes,
    offset: int,
) -> tuple[tuple[tuple[int, bool], ...], int] | None:
    parsed = _read_varuint(payload, offset)
    if parsed is None:
        return None
    count, pos = parsed
    if count > 8192 or pos + count * 5 > len(payload):
        return None
    values = tuple(
        (
            struct.unpack_from("<I", payload, pos + index * 5)[0],
            bool(payload[pos + index * 5 + 4] & 1),
        )
        for index in range(count)
    )
    return values, pos + count * 5


def _decode_action(obj: HircObject) -> None:
    payload = obj.payload
    if len(payload) < 10:
        return
    action_type = struct.unpack_from("<H", payload, 4)[0]
    type_key = action_type & 0xFF00
    obj.action_type = action_type
    action_names = (
        {0x1A00: "Break", 0x1B00: "Trigger"}
        if (obj.bank_version or 0) >= 150 else {}
    )
    obj.action_name = action_names.get(
        type_key, _ACTION_TYPE_NAMES.get(type_key, f"Action 0x{type_key:04X}")
    )
    obj.action_raw_id = struct.unpack_from("<I", payload, 6)[0]
    obj.action_target_kind = _action_target_kind(type_key, obj.bank_version)
    if obj.action_target_kind in {"object", "event"}:
        obj.action_target_id = obj.action_raw_id
    obj.action_target_is_bus = len(payload) > 10 and bool(payload[10] & 1)
    obj.property_bundle = _parse_property_bundle(
        payload, 11, bank_version=obj.bank_version
    )
    if obj.property_bundle is None:
        return
    pos = obj.property_bundle.end
    if type_key in {0x0400, 0x0500, 0x2300} and pos + 5 <= len(payload):
        flags = payload[pos]
        obj.action_settings = BnkActionSettings(
            "play", flags & 0x1F, flags, pos,
            bank_id=struct.unpack_from("<I", payload, pos + 1)[0],
            end=pos + 5,
        )
    elif type_key in {0x0100, 0x0200, 0x0300, 0x2200}:
        specific_size = 2 if type_key in {0x0100, 0x0200, 0x0300} else 1
        if pos + specific_size > len(payload):
            return
        flags = payload[pos]
        specific_flags = payload[pos + 1] if specific_size == 2 else None
        parsed = _parse_action_exceptions(payload, pos + specific_size)
        if parsed is None:
            return
        exceptions, end = parsed
        obj.action_settings = BnkActionSettings(
            {
                0x0100: "stop", 0x0200: "pause", 0x0300: "resume",
                0x2200: "reset_playlist",
            }[type_key],
            flags & 0x1F,
            flags,
            pos,
            stop_flags=specific_flags,
            exceptions=exceptions,
            end=end,
        )
    elif type_key in {0x0600, 0x0700} and pos < len(payload):
        parsed = _parse_action_exceptions(payload, pos + 1)
        if parsed is None:
            return
        exceptions, end = parsed
        obj.action_settings = BnkActionSettings(
            "mute", payload[pos] & 0x1F, payload[pos], pos,
            exceptions=exceptions, end=end,
        )
    elif type_key in _ACTION_SET_VALUE_TYPES and pos + 14 <= len(payload):
        parsed = _parse_action_exceptions(payload, pos + 14)
        if parsed is None:
            return
        exceptions, end = parsed
        value, minimum, maximum = struct.unpack_from("<3f", payload, pos + 2)
        if not all(math.isfinite(item) for item in (value, minimum, maximum)):
            return
        obj.action_settings = BnkActionSettings(
            "set_value", payload[pos] & 0x1F, payload[pos], pos,
            value_meaning=payload[pos + 1], value=value,
            minimum=minimum, maximum=maximum,
            exceptions=exceptions, end=end,
        )
    elif type_key in {0x1300, 0x1400} and pos + 15 <= len(payload):
        parsed = _parse_action_exceptions(payload, pos + 15)
        if parsed is None:
            return
        exceptions, end = parsed
        value, minimum, maximum = struct.unpack_from("<3f", payload, pos + 3)
        if not all(math.isfinite(item) for item in (value, minimum, maximum)):
            return
        obj.action_settings = BnkActionSettings(
            "game_parameter", payload[pos] & 0x1F, payload[pos], pos,
            parameter_id=obj.action_raw_id,
            bypass_transition=bool(payload[pos + 1]),
            value_meaning=payload[pos + 2], value=value,
            minimum=minimum, maximum=maximum,
            exceptions=exceptions, end=end,
        )
    elif type_key in {0x1200, 0x1900} and pos + 8 <= len(payload):
        group_id, value_id = struct.unpack_from("<II", payload, pos)
        obj.action_settings = BnkActionSettings(
            "state" if type_key == 0x1200 else "switch",
            None, 0, pos, group_id=group_id, value_id=value_id, end=pos + 8,
        )
    elif type_key in (
        {0x3300, 0x3400, 0x3500, 0x3600, 0x3700}
        if (obj.bank_version or 0) >= 150 else {0x1A00, 0x1B00}
    ) and pos + 2 <= len(payload):
        parsed = _parse_action_exceptions(payload, pos + 2)
        if parsed is None:
            return
        exceptions, end = parsed
        obj.action_settings = BnkActionSettings(
            "bypass_fx", None, 0, pos,
            bypass=bool(payload[pos]), target_mask=payload[pos + 1],
            exceptions=exceptions, end=end,
        )
    elif type_key == 0x1E00 and pos + 14 <= len(payload):
        parsed = _parse_action_exceptions(payload, pos + 14)
        if parsed is None:
            return
        exceptions, end = parsed
        value, minimum, maximum = struct.unpack_from("<3f", payload, pos + 1)
        if not all(math.isfinite(item) for item in (value, minimum, maximum)):
            return
        obj.action_settings = BnkActionSettings(
            "seek", None, 0, pos, value=value, minimum=minimum,
            maximum=maximum, relative_to_duration=bool(payload[pos]),
            snap_to_marker=bool(payload[pos + 13]), exceptions=exceptions, end=end,
        )


def _node_base_start(obj: HircObject) -> int | None:
    payload = obj.payload
    if obj.type_id == 0x02:
        _source, start = _read_bank_source(
            payload, 4, obj.object_id, obj.bank_version
        )
    elif obj.type_id in {0x05, 0x06, 0x07, 0x09}:
        start = 4
    elif obj.type_id in {0x0A, 0x0C, 0x0D}:
        start = 5
    elif obj.type_id == 0x0B:
        start = _music_track_node_base_offset(obj)
    else:
        return None
    return start


def _decode_node_base(obj: HircObject, known_ids: set[int]) -> None:
    payload = obj.payload
    start = _node_base_start(obj)
    if start is not None and obj.structure and obj.structure.complete:
        parent = next((
            field for field in obj.structure.fields
            if field.reference_role == "parent"
        ), None)
        if parent is not None:
            obj.parent_id = int(parent.value)
            obj.parent_reference_offset = parent.offset
        offset = obj.structure.anchor("property_bundle")
        if offset is not None:
            obj.property_bundle = _parse_property_bundle(
                payload, offset, bank_version=obj.bank_version
            )
        return
    offset = _node_base_parent_offset(payload, start)
    if offset is None or (obj.child_list_offset is not None and offset >= obj.child_list_offset):
        return
    parent_id = struct.unpack_from("<I", payload, offset)[0]
    if parent_id == 0 or parent_id in known_ids:
        obj.parent_id = parent_id
        obj.parent_reference_offset = offset
    obj.property_bundle = _parse_property_bundle(
        payload, offset + 5, bank_version=obj.bank_version
    )


def _parse_property_bundle(
    payload: bytes,
    offset: int,
    *,
    has_ranges: bool = True,
    kind: str = "object",
    bank_version: int | None = None,
    id_width: int = 1,
) -> BnkPropertyBundle | None:
    """Decode adjacent AkPropValue and ranged-modifier bundles."""

    if id_width not in {1, 2} or offset < 0 or offset + id_width > len(payload):
        return None
    id_format = "B" if id_width == 1 else "H"
    count = struct.unpack_from(f"<{id_format}", payload, offset)[0]
    ids_start = offset + id_width
    values_start = ids_start + count * id_width
    ranges_count_offset = values_start + count * 4
    if ranges_count_offset > len(payload) or (has_ranges and ranges_count_offset == len(payload)):
        return None
    values = tuple(
        BnkPropertyValue(
            struct.unpack_from(f"<{id_format}", payload, ids_start + index * id_width)[0],
            struct.unpack_from("<I", payload, values_start + index * 4)[0],
        )
        for index in range(count)
    )
    if len({item.property_id for item in values}) != len(values):
        return None
    if not has_ranges:
        return BnkPropertyBundle(
            values, (), offset, ranges_count_offset, False, kind,
            bank_version, id_width,
        )
    if ranges_count_offset + id_width > len(payload):
        return None
    range_count = struct.unpack_from(f"<{id_format}", payload, ranges_count_offset)[0]
    range_ids_start = ranges_count_offset + id_width
    ranges_start = range_ids_start + range_count * id_width
    end = ranges_start + range_count * 8
    if end > len(payload):
        return None
    ranges = tuple(
        BnkPropertyRange(
            struct.unpack_from(
                f"<{id_format}", payload, range_ids_start + index * id_width
            )[0],
            *struct.unpack_from("<II", payload, ranges_start + index * 8),
        )
        for index in range(range_count)
    )
    if len({item.property_id for item in ranges}) != len(ranges):
        return None
    return BnkPropertyBundle(
        values, ranges, offset, end, True, kind, bank_version, id_width
    )


def _node_base_parent_offset(payload: bytes, start: int | None) -> int | None:
    if start is None or start < 4 or start + 2 > len(payload):
        return None
    pos = start
    override, count = payload[pos], payload[pos + 1]
    if override > 1 or count > 4:
        return None
    pos += 2
    if count:
        pos += 1
        for _ in range(count):
            if pos + 7 > len(payload) or payload[pos] > 3 or payload[pos + 5] > 1 or payload[pos + 6] > 1:
                return None
            pos += 7
    if pos + 9 > len(payload) or payload[pos] > 1:
        return None
    return pos + 5


def _music_track_node_base_offset(obj: HircObject) -> int | None:
    track = obj.music_track
    if track is None:
        return None
    payload, pos = obj.payload, track.playlist_end
    if pos + 4 > len(payload):
        return None
    count = struct.unpack_from("<I", payload, pos)[0]
    pos += 4
    if count > 8192:
        return None
    for _ in range(count):
        if pos + 12 > len(payload):
            return None
        point_count = struct.unpack_from("<I", payload, pos + 8)[0]
        pos += 12
        if point_count > 0x100000 or pos + point_count * 12 > len(payload):
            return None
        pos += point_count * 12
    return pos


def _read_bank_source(
    payload: bytes,
    pos: int,
    object_id: int,
    bank_version: int | None = 125,
) -> tuple[BnkSource | None, int]:
    if pos + 14 > len(payload):
        return None, pos
    plugin_id = struct.unpack_from("<I", payload, pos)[0]
    stream_type = payload[pos + 4]
    source_id = struct.unpack_from("<I", payload, pos + 5)[0]
    in_memory_size = struct.unpack_from("<I", payload, pos + 9)[0]
    source_bits = payload[pos + 13]
    end = pos + 14
    plugin_type = plugin_id & 0x0F
    if plugin_type == 2 or (plugin_type == 5 and (bank_version or 0) <= 126):
        if end + 4 > len(payload):
            return None, pos
        parameter_size = struct.unpack_from("<I", payload, end)[0]
        if parameter_size > len(payload) - end - 4:
            return None, pos
        end += 4 + parameter_size
    return (
        BnkSource(
            object_id=object_id,
            source_id=source_id,
            plugin_id=plugin_id,
            stream_type=stream_type,
            in_memory_size=in_memory_size,
            source_bits=source_bits,
            payload_offset=pos + 5,
        ),
        end,
    )


def _read_music_track_sources(
    payload: bytes,
    object_id: int,
    bank_version: int | None = 125,
) -> tuple[BnkSource, ...]:
    if len(payload) < 8:
        return ()
    pos = 5
    if pos + 4 > len(payload):
        return ()
    count = struct.unpack_from("<I", payload, pos)[0]
    pos += 4
    if count > 0x10000:
        return ()
    sources: list[BnkSource] = []
    for _ in range(count):
        source, next_pos = _read_bank_source(
            payload, pos, object_id, bank_version
        )
        if source is None or next_pos <= pos:
            break
        sources.append(source)
        pos = next_pos
    return tuple(sources)


def _matching_u32_offsets(payload: bytes, value: int, start: int = 0) -> list[int]:
    packed = struct.pack("<I", value & 0xFFFFFFFF)
    return [
        pos
        for pos in range(max(0, start), max(max(0, start), len(payload) - 3))
        if payload[pos : pos + 4] == packed
    ]


def _find_child_list(
    obj: HircObject,
    by_id: dict[int, HircObject],
) -> tuple[int | None, tuple[int, ...]]:
    payload = obj.payload
    known_ids = set(by_id)
    candidates: list[tuple[tuple[int, int, int, int, int], int, tuple[int, ...]]] = []
    for pos in range(4, max(4, len(payload) - 7)):
        count = struct.unpack_from("<I", payload, pos)[0]
        if count == 0 or count > 8192 or count > (len(payload) - pos - 4) // 4:
            continue
        first = struct.unpack_from("<I", payload, pos + 4)[0]
        if first not in known_ids or first == obj.object_id:
            continue
        values = tuple(struct.unpack_from(f"<{count}I", payload, pos + 4))
        if any(value not in known_ids or value == obj.object_id for value in values):
            continue
        parent_bytes = struct.pack("<I", obj.object_id)
        reciprocal = sum(parent_bytes in by_id[value].payload[4:] for value in values)
        probe = HircObject(0, obj.type_id, obj.type_name, obj.object_id, payload, 0)
        probe.child_list_offset, probe.child_ids = pos, values
        typed = (
            _parse_random_sequence(probe)
            if obj.type_id == 0x05
            else _parse_switch_container(probe)
            if obj.type_id == 0x06
            else _parse_music_segment(probe)
            if obj.type_id == 0x0A
            else True
        )
        # The all-children list normally has every child's DirectParentID pointing back.
        score = (int(typed is not None), int(reciprocal == count), reciprocal, count, pos)
        candidates.append((score, pos, values))
    if candidates:
        _score, pos, values = max(candidates)
        return pos, values

    # Empty child lists need type-specific suffix validation because there are
    # many unrelated zero u32s in NodeBaseParams.
    for pos in range(len(payload) - 4, 3, -1):
        if payload[pos : pos + 4] != b"\0\0\0\0":
            continue
        probe = HircObject(0, obj.type_id, obj.type_name, obj.object_id, payload, 0)
        probe.child_list_offset = pos
        parsed = (
            _parse_random_sequence(probe)
            if obj.type_id == 0x05
            else _parse_switch_container(probe)
            if obj.type_id == 0x06
            else _parse_music_segment(probe)
            if obj.type_id == 0x0A
            else None
        )
        if (
            obj.type_id == 0x07 and pos + 4 == len(payload)
        ) or (
            obj.type_id == 0x09
            and pos + 5 == len(payload)
            and payload[pos : pos + 4] == b"\0\0\0\0"
            and payload[pos + 4] <= 1
        ) or (
            parsed is not None
            and getattr(parsed, "playlist_end", getattr(parsed, "data_end", getattr(parsed, "markers_end", -1)))
            == len(payload)
        ):
            return pos, ()
    return None, ()


def _parse_random_sequence(obj: HircObject) -> BnkRandomSequence | None:
    child_pos = obj.child_list_offset
    if child_pos is None or child_pos < 28:
        return None
    playlist_offset = child_pos + 4 + len(obj.child_ids) * 4
    if playlist_offset + 2 > len(obj.payload):
        return None
    count = struct.unpack_from("<H", obj.payload, playlist_offset)[0]
    playlist_end = playlist_offset + 2 + count * 8
    if count > 8192 or playlist_end > len(obj.payload):
        return None
    values = struct.unpack_from("<HHHfffHBBBB", obj.payload, child_pos - 24)
    if not all(math.isfinite(value) for value in values[3:6]):
        return None
    playlist = tuple(
        BnkPlaylistItem(*struct.unpack_from("<Ii", obj.payload, playlist_offset + 2 + index * 8))
        for index in range(count)
    )
    return BnkRandomSequence(*values, playlist, child_pos - 24, playlist_offset, playlist_end)


def _parse_switch_container(obj: HircObject) -> BnkSwitchContainer | None:
    child_pos = obj.child_list_offset
    if child_pos is None or child_pos < 14:
        return None
    payload = obj.payload
    pos = child_pos + 4 + len(obj.child_ids) * 4
    if pos + 4 > len(payload):
        return None
    mapping_count = struct.unpack_from("<I", payload, pos)[0]
    pos += 4
    if mapping_count > 8192:
        return None
    mappings = []
    for _ in range(mapping_count):
        if pos + 8 > len(payload):
            return None
        value_id, count = struct.unpack_from("<II", payload, pos)
        pos += 8
        if count > 8192 or pos + count * 4 > len(payload):
            return None
        values = tuple(struct.unpack_from(f"<{count}I", payload, pos)) if count else ()
        pos += count * 4
        mappings.append(BnkSwitchMapping(value_id, values))
    if pos + 4 > len(payload):
        return None
    param_count = struct.unpack_from("<I", payload, pos)[0]
    pos += 4
    if param_count > 8192 or pos + param_count * 14 > len(payload):
        return None
    params = tuple(
        BnkSwitchParam(*struct.unpack_from("<IBBii", payload, pos + index * 14))
        for index in range(param_count)
    )
    pos += param_count * 14
    group_type = payload[child_pos - 10]
    group_id, default_id = struct.unpack_from("<II", payload, child_pos - 9)
    return BnkSwitchContainer(
        group_type,
        group_id,
        default_id,
        bool(payload[child_pos - 1]),
        tuple(mappings),
        params,
        child_pos - 10,
        pos,
    )


def _parse_music_segment(obj: HircObject) -> BnkMusicSegment | None:
    child_pos = obj.child_list_offset
    if child_pos is None:
        return None
    payload = obj.payload
    pos = child_pos + 4 + len(obj.child_ids) * 4 + 23
    if pos + 4 > len(payload):
        return None
    stinger_count = struct.unpack_from("<I", payload, pos)[0]
    pos += 4
    if stinger_count > 8192 or pos + stinger_count * 24 + 12 > len(payload):
        return None
    duration_offset = pos + stinger_count * 24
    duration = struct.unpack_from("<d", payload, duration_offset)[0]
    marker_count_offset = duration_offset + 8
    marker_count = struct.unpack_from("<I", payload, marker_count_offset)[0]
    if not math.isfinite(duration) or marker_count > 8192:
        return None
    pos = marker_count_offset + 4
    markers = []
    for _ in range(marker_count):
        if pos + 12 > len(payload):
            return None
        marker_id, position = struct.unpack_from("<Id", payload, pos)
        pos += 12
        if not math.isfinite(position):
            return None
        if (obj.bank_version or 0) >= 137:
            end = payload.find(b"\0", pos)
            if end < 0:
                return None
            raw_name, pos = payload[pos:end], end + 1
        else:
            if pos + 4 > len(payload):
                return None
            size = struct.unpack_from("<I", payload, pos)[0]
            pos += 4
            if size > len(payload) - pos:
                return None
            raw_name = payload[pos : pos + size]
            pos += size
        markers.append(
            BnkMusicMarker(
                marker_id,
                position,
                raw_name.rstrip(b"\0").decode("utf-8", "replace"),
            )
        )
    return BnkMusicSegment(duration, tuple(markers), duration_offset, marker_count_offset, pos)


def _parse_music_track(
    obj: HircObject, bank_version: int | None = 125
) -> BnkMusicTrack | None:
    payload = obj.payload
    if len(payload) < 9:
        return None
    pos = 5
    source_count = struct.unpack_from("<I", payload, pos)[0]
    pos += 4
    if source_count > 8192:
        return None
    for _ in range(source_count):
        _source, next_pos = _read_bank_source(
            payload, pos, obj.object_id, bank_version
        )
        if next_pos <= pos:
            return None
        pos = next_pos
    if pos + 4 > len(payload):
        return None
    playlist_offset = pos
    count = struct.unpack_from("<I", payload, pos)[0]
    pos += 4
    clip_size = 44 if (bank_version or 0) >= 133 else 40
    if count > 8192 or pos + count * clip_size > len(payload):
        return None
    clips = []
    for index in range(count):
        offset = pos + index * clip_size
        if clip_size == 44:
            track_id, source_id, event_id, *times = struct.unpack_from(
                "<IIIdddd", payload, offset
            )
        else:
            track_id, source_id, *times = struct.unpack_from(
                "<IIdddd", payload, offset
            )
            event_id = 0
        clips.append(BnkMusicClip(track_id, source_id, *times, event_id))
    clips = tuple(clips)
    pos += count * clip_size
    subtracks = 0
    if count:
        if pos + 4 > len(payload):
            return None
        subtracks = struct.unpack_from("<I", payload, pos)[0]
        pos += 4
    return BnkMusicTrack(clips, subtracks, playlist_offset, pos)


def _resolve_events(objects: list[HircObject]) -> list[BnkEvent]:
    by_id = {obj.object_id: obj for obj in objects if obj.object_id}
    events: list[BnkEvent] = []
    for event_obj in (obj for obj in objects if obj.type_id == 0x04):
        reachable: list[int] = []
        sources: list[int] = []
        unresolved: list[int] = []
        visited: set[int] = set()

        def walk(object_id: int) -> None:
            if not object_id or object_id in visited:
                return
            visited.add(object_id)
            obj = by_id.get(object_id)
            if obj is None:
                unresolved.append(object_id)
                return
            reachable.append(object_id)
            if obj.type_id == 0x04:
                for action_id in obj.event_action_ids:
                    walk(action_id)
                return
            if obj.type_id == 0x03:
                if obj.action_target_id:
                    walk(obj.action_target_id)
                return
            sources.extend(source.source_id for source in obj.sources if source.source_id)
            for child_id in obj.child_ids:
                walk(child_id)

        for action_id in event_obj.event_action_ids:
            walk(action_id)
        events.append(
            BnkEvent(
                object_id=event_obj.object_id,
                action_ids=event_obj.event_action_ids,
                source_ids=tuple(dict.fromkeys(sources)),
                reachable_object_ids=tuple(reachable),
                unresolved_ids=tuple(dict.fromkeys(unresolved)),
            )
        )
    return events


def _source_storage(stream_type: int | None, *, embedded: bool = False) -> str:
    if embedded:
        return "prefetch (BNK + PCK)" if stream_type == 1 else "embedded"
    if stream_type == 2:
        return "streamed (PCK)"
    if stream_type == 1:
        return "prefetch (PCK)"
    if stream_type == 0:
        return "bank media (not present)"
    if stream_type is None:
        return "external"
    return "external"


def _read_varuint(data: bytes, pos: int) -> tuple[int, int] | None:
    value = 0
    for _ in range(10):
        if pos >= len(data):
            return None
        current = data[pos]
        pos += 1
        value = (value << 7) | (current & 0x7F)
        if not current & 0x80:
            return value, pos
    return None


def _pack_varuint(value: int) -> bytes:
    if value < 0:
        raise ValueError("Variable-length integers cannot be negative")
    groups = [value & 0x7F]
    value >>= 7
    while value:
        groups.append(value & 0x7F)
        value >>= 7
    groups.reverse()
    return bytes(group | (0x80 if index < len(groups) - 1 else 0) for index, group in enumerate(groups))


def _pack_event_payload(object_id: int, action_ids: tuple[int, ...]) -> bytes:
    out = bytearray(struct.pack("<I", object_id) + _pack_varuint(len(action_ids)))
    if action_ids:
        out += struct.pack(f"<{len(action_ids)}I", *action_ids)
    return bytes(out)


def clone_hirc_payload(obj: HircObject, object_id: int) -> bytes:
    """Clone an opaque HIRC payload, changing only its leading ShortID."""

    object_id = _object_id(object_id)
    if len(obj.payload) < 4:
        raise ValueError(f"HIRC object {obj.object_id} has no editable ShortID")
    payload = bytearray(obj.payload)
    struct.pack_into("<I", payload, 0, object_id)
    return bytes(payload)


def create_play_action_payload(object_id: int, target_id: int) -> bytes:
    """Create a minimal Play Action for the registered legacy schemas."""

    return struct.pack(
        "<IHIBBBBI",
        _object_id(object_id),
        0x0403,
        _object_id(target_id),
        0,  # target is not a bus
        0,  # property count
        0,  # ranged-property count
        0,  # play flags
        0,  # bank file ID
    )


def create_stop_action_payload(
    object_id: int,
    target_id: int,
    *,
    target_is_bus: bool = False,
) -> bytes:
    """Create a minimal Stop Action with linear fade settings."""

    return struct.pack(
        "<IHIBBBBBB",
        _object_id(object_id),
        0x0102 if target_is_bus else 0x0103,
        _object_id(target_id),
        int(bool(target_is_bus)),
        0,  # property count
        0,  # ranged-property count
        4,  # linear fade curve
        6,  # apply to state transitions and dynamic sequences
        0,  # exception-list count (varuint)
    )


def patch_hirc_reference(obj: HircObject, offset: int, target_id: int) -> bytes:
    """Patch one explicitly selected four-byte field in a HIRC payload."""

    offset = int(offset)
    if offset < 4 or offset + 4 > len(obj.payload):
        raise ValueError(f"Reference offset {offset} is outside HIRC object {obj.object_id}")
    payload = bytearray(obj.payload)
    struct.pack_into("<I", payload, offset, _object_id(target_id, allow_zero=True))
    return bytes(payload)


def _validate_hirc_reference_target(
    owner: HircObject,
    field: HircReference,
    target_id: int,
    objects,
) -> None:
    """Reject ambiguous or type-incompatible local HIRC retargeting."""

    target_id = _object_id(target_id)
    if (
        target_id == WWISE_ANY_OBJECT_ID
        and field.role in {"transition source", "transition destination"}
    ):
        # The "Any" wildcard is only meaningful for music transition rules.
        return
    allowed = compatible_hirc_reference_types(owner, field)
    if not allowed:
        raise ValueError(
            f"{owner.type_name} {owner.object_id} field '{field.role}' is not a "
            "safely typed HIRC reference"
        )
    matches = [obj for obj in objects if obj.object_id == target_id]
    if not matches and (
        field.target_kind == "event"
        or field.role in _CROSS_BANK_BUS_ROLES
    ):
        # Event Actions may intentionally call an Event in another loaded bank,
        # and buses (output/auxiliary/reflections/ducked) conventionally live in
        # a separate init/master-bus bank, so both are valid cross-bank links.
        return
    if len(matches) != 1:
        state = "missing" if not matches else f"ambiguous ({len(matches)} objects)"
        raise ValueError(
            f"Cannot retarget '{field.role}' in {owner.object_id} to {target_id}: "
            f"the target is {state} in this bank"
        )
    target = matches[0]
    if target is owner:
        raise ValueError(f"HIRC object {owner.object_id} cannot target itself")
    if target.type_id not in allowed:
        expected = ", ".join(
            _hirc_type_name(type_id, owner.bank_version)
            for type_id in sorted(allowed)
        )
        raise ValueError(
            f"Cannot retarget '{field.role}' in {owner.object_id} to "
            f"{target.type_name} {target_id}; expected {expected}"
        )


def set_action_fields(
    obj: HircObject,
    action_type: int,
    target_id: int,
    target_is_bus: bool,
) -> bytes:
    if obj.type_id != 0x03 or len(obj.payload) < 11:
        raise ValueError(f"Action fields were not decoded for object {obj.object_id}")
    action_type = int(action_type)
    if not 0 <= action_type <= 0xFFFF:
        raise ValueError("Action type is outside the u16 range")
    if obj.action_type is not None and (action_type & 0xFF00) != (obj.action_type & 0xFF00):
        raise ValueError(
            "Changing the Action family would invalidate its type-specific payload; clone a matching template"
        )
    payload = bytearray(obj.payload)
    struct.pack_into("<HI", payload, 4, action_type, _object_id(target_id, allow_zero=True))
    payload[10] = (payload[10] & ~1) | int(bool(target_is_bus))
    return bytes(payload)


def _pack_action_exceptions(exceptions) -> bytes:
    values = tuple(exceptions)
    out = bytearray(_pack_varuint(len(values)))
    for object_id, is_bus in values:
        out += struct.pack(
            "<IB", _object_id(object_id, allow_zero=True), int(bool(is_bus))
        )
    return bytes(out)


def set_action_specific(obj: HircObject, settings: BnkActionSettings) -> bytes:
    """Rebuild one decoded Wwise Action body and preserve its suffix."""

    original = obj.action_settings
    if original is None or original.end is None:
        raise ValueError(f"Action-specific settings were not decoded for object {obj.object_id}")
    if settings.kind != original.kind:
        raise ValueError("Changing an Action settings family is not safe")

    out = bytearray(obj.payload[: original.offset])
    fade = 0 if settings.fade_curve is None else int(settings.fade_curve)
    if settings.fade_curve is not None and not 0 <= fade <= 0x1F:
        raise ValueError("Fade curve is outside the five-bit Wwise range")
    fade_flags = (original.flags & 0xE0) | fade

    if settings.kind == "play":
        out += struct.pack(
            "<BI", fade_flags,
            _u32(settings.bank_id or 0, "Bank ID"),
        )
    elif settings.kind in {"stop", "pause", "resume"}:
        out += struct.pack("<BB", fade_flags, int(settings.stop_flags or 0) & 0xFF)
        out += _pack_action_exceptions(settings.exceptions)
    elif settings.kind in {"reset_playlist", "mute"}:
        out += struct.pack("<B", fade_flags)
        out += _pack_action_exceptions(settings.exceptions)
    elif settings.kind == "set_value":
        values = (settings.value, settings.minimum, settings.maximum)
        if any(value is None or not math.isfinite(float(value)) for value in values):
            raise ValueError("Action value and random range must be finite")
        out += struct.pack(
            "<BB3f", fade_flags, int(settings.value_meaning or 0) & 0xFF,
            *map(float, values),
        )
        out += _pack_action_exceptions(settings.exceptions)
    elif settings.kind == "game_parameter":
        values = (settings.value, settings.minimum, settings.maximum)
        if any(value is None or not math.isfinite(float(value)) for value in values):
            raise ValueError("Game Parameter value and random range must be finite")
        out += struct.pack(
            "<BBB3f", fade_flags, int(bool(settings.bypass_transition)),
            int(settings.value_meaning or 0) & 0xFF, *map(float, values),
        )
        out += _pack_action_exceptions(settings.exceptions)
    elif settings.kind == "bypass_fx":
        out += struct.pack(
            "<BB", int(bool(settings.bypass)),
            _u8(settings.target_mask or 0, "Target effect mask"),
        )
        out += _pack_action_exceptions(settings.exceptions)
    elif settings.kind == "seek":
        values = (settings.value, settings.minimum, settings.maximum)
        if any(value is None or not math.isfinite(float(value)) for value in values):
            raise ValueError("Seek value and random range must be finite")
        out += struct.pack(
            "<B3fB", int(bool(settings.relative_to_duration)),
            *map(float, values), int(bool(settings.snap_to_marker)),
        )
        out += _pack_action_exceptions(settings.exceptions)
    elif settings.kind in {"state", "switch"}:
        out += struct.pack(
            "<II", _u32(settings.group_id or 0, "Group ID"),
            _u32(settings.value_id or 0, "Value ID"),
        )
    else:
        raise ValueError(f"Unsupported Action settings kind: {settings.kind}")

    out += obj.payload[original.end :]
    if settings.kind in {"state", "switch"}:
        struct.pack_into("<I", out, 6, _u32(settings.value_id or 0, "Value ID"))
    elif settings.kind == "game_parameter":
        struct.pack_into(
            "<I", out, 6, _u32(settings.parameter_id or 0, "Game Parameter ID")
        )
    return bytes(out)




def set_attenuation(obj: HircObject, settings: BnkAttenuation) -> bytes:
    """Replace a decoded attenuation cone/curve block and preserve RTPC data."""

    original = obj.attenuation
    if original is None:
        raise ValueError(f"Attenuation data was not decoded for object {obj.object_id}")
    assignments, curves = tuple(settings.assignments), tuple(settings.curves)
    targets = attenuation_targets(obj.bank_version)
    if len(assignments) != len(targets):
        raise ValueError(
            f"Wwise v{obj.bank_version or 125} attenuation requires "
            f"{len(targets)} curve assignments"
        )
    if len(curves) > 0xFF:
        raise ValueError("An attenuation cannot contain more than 255 curves")
    if any(index < -1 or index >= len(curves) for index in assignments):
        raise ValueError("Each attenuation target must use an existing curve or None")

    cone = None if settings.cone is None else tuple(map(float, settings.cone))
    if cone is not None and (len(cone) != 5 or not all(map(math.isfinite, cone))):
        raise ValueError("Cone settings require five finite values")
    out = bytearray(obj.payload[: original.offset])
    out += struct.pack("<B", (original.cone_flags & ~1) | int(cone is not None))
    if cone is not None:
        out += struct.pack("<5f", *cone)
    out += struct.pack(f"<{len(assignments)}b", *assignments)
    out += struct.pack("<B", len(curves))
    for curve in curves:
        points = tuple(curve.points)
        if len(points) > 0xFFFF:
            raise ValueError("An attenuation curve cannot contain more than 65,535 points")
        out += struct.pack("<BH", _u8(curve.scaling, "Curve scaling"), len(points))
        for point in points:
            x, y = float(point.x), float(point.y)
            if not math.isfinite(x) or not math.isfinite(y):
                raise ValueError("Attenuation curve points must be finite")
            out += struct.pack(
                "<ffI", x, y, _u32(point.interpolation, "Curve interpolation")
            )
    out += obj.payload[original.end :]
    return bytes(out)


def set_silence_source(obj: HircObject, settings: BnkSilenceSource) -> bytes:
    """Edit Audiokinetic Wwise Silence duration/randomization in place."""

    original = obj.silence_source
    if original is None:
        raise ValueError(f"Wwise Silence data was not decoded for object {obj.object_id}")
    values = (
        float(settings.duration_seconds), float(settings.random_minus_seconds),
        float(settings.random_plus_seconds),
    )
    if not all(map(math.isfinite, values)):
        raise ValueError("Silence duration and randomization must be finite")
    if values[0] < 0:
        raise ValueError("Silence duration must be non-negative")
    payload = bytearray(obj.payload)
    struct.pack_into("<3f", payload, original.offset, *values)
    return bytes(payload)


def set_fx_parameters(obj: HircObject, parameters) -> bytes:
    """Edit a known built-in Wwise plug-in block without touching its suffix."""

    fx, parameters = obj.fx_plugin, tuple(parameters)
    if fx is None:
        raise ValueError(f"Built-in plug-in parameters were not decoded for object {obj.object_id}")
    if len(parameters) != len(fx.parameters):
        raise ValueError("Plug-in parameters cannot be added or removed")
    values = []
    for original, item in zip(fx.parameters, parameters):
        if (item.name, item.storage, item.enum_name) != (
            original.name, original.storage, original.enum_name
        ):
            raise ValueError("Plug-in parameter layout cannot be changed")
        if item.storage == "f32":
            value = float(item.value)
            if not math.isfinite(value):
                raise ValueError(f"{item.name} must be finite")
        elif item.storage == "u32":
            value = _u32(item.value, item.name)
        elif item.storage == "i32":
            value = _s32(item.value, item.name)
        elif item.storage == "i16":
            value = _s16(item.value, item.name)
        elif item.storage == "u16":
            value = _u16(item.value, item.name)
        else:
            value = _u8(item.value, item.name)
            if item.storage == "bool" and value not in {0, 1}:
                raise ValueError(f"{item.name} must be enabled or disabled")
        values.append(value)
    fmt = "<" + "".join(_FX_STORAGE_FORMAT[item.storage] for item in fx.parameters)
    payload = bytearray(obj.payload)
    struct.pack_into(fmt, payload, fx.offset, *values)
    return bytes(payload)


def set_bank_sources(obj: HircObject, sources) -> bytes:
    """Edit fixed fields of existing AkBankSourceData records in place."""

    sources = tuple(sources)
    if len(sources) != len(obj.sources):
        raise ValueError("Adding or removing source records requires a cloned Wwise template")
    payload = bytearray(obj.payload)
    for original, source in zip(obj.sources, sources):
        if (int(source.plugin_id) & 0x0F) != (original.plugin_id & 0x0F):
            raise ValueError("Changing a source plugin type would change its binary layout")
        pos = original.payload_offset - 5
        if pos < 4 or pos + 14 > len(payload):
            raise ValueError(f"Source {original.source_id} is outside HIRC object {obj.object_id}")
        struct.pack_into(
            "<IBIIB",
            payload,
            pos,
            _u32(source.plugin_id, "Plugin ID"),
            _u8(source.stream_type, "Stream type"),
            _object_id(source.source_id),
            _u32(source.in_memory_size, "In-memory size"),
            _u8(source.source_bits, "Source flags"),
        )
    return bytes(payload)


def bnk_property_name(
    property_id: int, kind: str = "object", bank_version: int | None = 125
) -> str:
    names = property_names(kind, bank_version)
    return names.get(int(property_id), f"Unknown 0x{int(property_id):02X}")


def format_bnk_property_value(
    property_id: int,
    value_bits: int,
    kind: str = "object",
    bank_version: int | None = 125,
) -> str:
    """Return an editable, bit-round-trippable AkPropValue string."""

    property_id, value_bits = int(property_id), int(value_bits) & 0xFFFFFFFF
    names = property_names(kind, bank_version)
    if property_id not in names:
        return f"0x{value_bits:08X}"
    integer = property_id in integer_properties(kind, bank_version)
    if integer:
        return str(struct.unpack("<i", struct.pack("<I", value_bits))[0])
    value = struct.unpack("<f", struct.pack("<I", value_bits))[0]
    return repr(value) if math.isfinite(value) else f"0x{value_bits:08X}"


def parse_bnk_property_value(
    property_id: int,
    value,
    kind: str = "object",
    bank_version: int | None = 125,
) -> int:
    """Encode an AkPropValue entered as a number or exact 0xXXXXXXXX bits."""

    text = str(value).strip()
    if text.lower().startswith("0x"):
        return _u32(int(text, 16), "Property value")
    integer = int(property_id) in integer_properties(kind, bank_version)
    if integer:
        parsed = int(text, 0)
        if not -(1 << 31) <= parsed < (1 << 32):
            raise ValueError("Property value is outside the 32-bit range")
        return parsed & 0xFFFFFFFF
    parsed = float(text)
    if not math.isfinite(parsed):
        raise ValueError("Property value must be finite")
    return struct.unpack("<I", struct.pack("<f", parsed))[0]


def set_property_bundle(obj: HircObject, values, ranges) -> bytes:
    """Replace a decoded property bundle while preserving its suffix."""

    bundle, values, ranges = obj.property_bundle, tuple(values), tuple(ranges)
    if bundle is None:
        raise ValueError(f"Playback properties were not decoded for object {obj.object_id}")
    validate_id = _u16 if bundle.id_width == 2 else _u8
    max_count = 0xFFFF if bundle.id_width == 2 else 0xFF
    id_format = "H" if bundle.id_width == 2 else "B"
    value_ids = tuple(validate_id(item.property_id, "Property ID") for item in values)
    range_ids = tuple(validate_id(item.property_id, "Range property ID") for item in ranges)
    if len(values) > max_count or len(ranges) > max_count:
        raise ValueError(
            f"A Wwise property bundle cannot contain more than {max_count} entries"
        )
    if len(set(value_ids)) != len(value_ids) or len(set(range_ids)) != len(range_ids):
        raise ValueError("Property IDs must be unique within each table")
    if ranges and not bundle.has_ranges:
        raise ValueError(f"{obj.type_name} does not store random property ranges")
    out = bytearray(obj.payload[: bundle.offset])
    out += struct.pack(f"<{id_format}", len(values))
    if value_ids:
        out += struct.pack(f"<{len(value_ids)}{id_format}", *value_ids)
    for item in values:
        out += struct.pack("<I", _u32(item.value_bits, "Property bits"))
    if bundle.has_ranges:
        out += struct.pack(f"<{id_format}", len(ranges))
        if range_ids:
            out += struct.pack(f"<{len(range_ids)}{id_format}", *range_ids)
        for item in ranges:
            out += struct.pack(
                "<II",
                _u32(item.minimum_bits, "Range minimum bits"),
                _u32(item.maximum_bits, "Range maximum bits"),
            )
    out += obj.payload[bundle.end :]
    return bytes(out)


def can_edit_hirc_children(obj: HircObject) -> bool:
    """Whether chilrden can be rebuilt without an unknown companion table."""

    if obj.child_list_offset is None:
        return False
    if obj.type_id in {0x05, 0x06, 0x07, 0x0A}:
        return True
    if obj.type_id != 0x09:
        return False
    end = obj.child_list_offset + 4 + len(obj.child_ids) * 4
    suffix = obj.payload[end:]
    # Layer containers append u32 layer-count plus a continuous-validation byte.
    return len(suffix) == 5 and suffix[:4] == b"\0\0\0\0"


def set_hirc_children(obj: HircObject, child_ids) -> bytes:
    """Replace a decoded children array and keep companion tables consistent."""

    children = tuple(dict.fromkeys(_object_id(value) for value in child_ids))
    if not can_edit_hirc_children(obj):
        raise ValueError(
            f"The Children field of {obj.type_name} {obj.object_id} has an unsupported companion layout"
        )
    if children == obj.child_ids:
        return obj.payload
    if obj.random_sequence:
        removed = set(obj.child_ids) - set(children)
        playlist = tuple(
            item for item in obj.random_sequence.playlist
            if item.object_id not in removed
        )
        represented = {item.object_id for item in playlist}
        playlist += tuple(
            BnkPlaylistItem(value) for value in children if value not in represented
        )
        return set_random_sequence(obj, obj.random_sequence, children, playlist)
    if obj.switch_container:
        removed = set(obj.child_ids) - set(children)
        switch = obj.switch_container
        mappings = tuple(
            BnkSwitchMapping(
                item.value_id,
                tuple(value for value in item.object_ids if value not in removed),
            )
            for item in switch.mappings
        )
        params = tuple(item for item in switch.params if item.object_id not in removed)
        return set_switch_container(obj, switch, children, mappings, params)

    start = obj.child_list_offset
    end = start + 4 + len(obj.child_ids) * 4
    return obj.payload[:start] + _pack_id_list(children) + obj.payload[end:]


def set_random_sequence(
    obj: HircObject,
    settings: BnkRandomSequence,
    child_ids,
    playlist: tuple[BnkPlaylistItem, ...] | list[BnkPlaylistItem],
) -> bytes:
    children = tuple(dict.fromkeys(_object_id(value) for value in child_ids))
    playlist = tuple(playlist)
    if obj.random_sequence is None or obj.child_list_offset is None:
        raise ValueError(f"Random/Sequence data was not decoded for object {obj.object_id}")
    values = (
        _u16(settings.loop_count, "Loop count"),
        _u16(settings.loop_min, "Loop minimum"),
        _u16(settings.loop_max, "Loop maximum"),
        float(settings.transition_ms),
        float(settings.transition_min_ms),
        float(settings.transition_max_ms),
        _u16(settings.avoid_repeat, "Avoid-repeat count"),
        _u8(settings.transition_mode, "Transition mode"),
        _u8(settings.random_mode, "Random mode"),
        _u8(settings.mode, "Container mode"),
        _u8(settings.flags, "Flags"),
    )
    if not all(math.isfinite(value) for value in values[3:6]):
        raise ValueError("Transition values must be finite")
    out = bytearray(obj.payload[: settings.settings_offset])
    out += struct.pack("<HHHfffHBBBB", *values)
    out += _pack_id_list(children)
    out += struct.pack("<H", _u16(len(playlist), "Playlist length"))
    for item in playlist:
        out += struct.pack("<Ii", _object_id(item.object_id), _s32(item.weight, "Playlist weight"))
    out += obj.payload[settings.playlist_end :]
    return bytes(out)


def set_switch_container(
    obj: HircObject,
    settings: BnkSwitchContainer,
    child_ids,
    mappings: tuple[BnkSwitchMapping, ...] | list[BnkSwitchMapping],
    params: tuple[BnkSwitchParam, ...] | list[BnkSwitchParam],
) -> bytes:
    children = tuple(dict.fromkeys(_object_id(value) for value in child_ids))
    mappings, params = tuple(mappings), tuple(params)
    if obj.switch_container is None or obj.child_list_offset is None:
        raise ValueError(f"Switch data was not decoded for object {obj.object_id}")
    out = bytearray(obj.payload[: settings.settings_offset])
    out += struct.pack(
        "<BIIB",
        _u8(settings.group_type, "Group type"),
        _object_id(settings.group_id, allow_zero=True),
        _object_id(settings.default_value_id, allow_zero=True),
        int(bool(settings.continuous_validation)),
    )
    out += _pack_id_list(children)
    out += struct.pack("<I", len(mappings))
    for mapping in mappings:
        values = tuple(_object_id(value) for value in mapping.object_ids)
        out += struct.pack("<II", _object_id(mapping.value_id, allow_zero=True), len(values))
        if values:
            out += struct.pack(f"<{len(values)}I", *values)
    out += struct.pack("<I", len(params))
    for param in params:
        out += struct.pack(
            "<IBBii",
            _object_id(param.object_id),
            _u8(param.flags, "Switch flags"),
            _u8(param.mode, "Switch mode"),
            _s32(param.fade_out_ms, "Fade-out time"),
            _s32(param.fade_in_ms, "Fade-in time"),
        )
    out += obj.payload[settings.data_end :]
    return bytes(out)


def set_music_segment(
    obj: HircObject,
    duration_ms: float,
    markers: tuple[BnkMusicMarker, ...] | list[BnkMusicMarker],
) -> bytes:
    segment = obj.music_segment
    duration_ms, markers = float(duration_ms), tuple(markers)
    if segment is None:
        raise ValueError(f"Music Segment data was not decoded for object {obj.object_id}")
    if not math.isfinite(duration_ms) or duration_ms < 0:
        raise ValueError("Music Segment duration must be a non-negative finite number")
    out = bytearray(obj.payload[: segment.duration_offset])
    out += struct.pack("<dI", duration_ms, len(markers))
    for marker in markers:
        position = float(marker.position_ms)
        if not math.isfinite(position):
            raise ValueError("Marker positions must be finite")
        name = str(marker.name).encode("utf-8")
        out += struct.pack(
            "<Id", _object_id(marker.marker_id, allow_zero=True), position
        )
        if (obj.bank_version or 0) >= 137:
            out += name + b"\0"
        else:
            name += b"\0" if name else b""
            out += struct.pack("<I", len(name)) + name
    out += obj.payload[segment.markers_end :]
    return bytes(out)


def set_music_track_clips(
    obj: HircObject,
    clips: tuple[BnkMusicClip, ...] | list[BnkMusicClip],
    subtrack_count: int,
) -> bytes:
    track, clips = obj.music_track, tuple(clips)
    if track is None:
        raise ValueError(f"Music Track playlist was not decoded for object {obj.object_id}")
    subtrack_count = int(subtrack_count)
    if subtrack_count < 0 or subtrack_count > 0xFFFFFFFF:
        raise ValueError("Subtrack count is outside the u32 range")
    out = bytearray(obj.payload[: track.playlist_offset])
    out += struct.pack("<I", len(clips))
    for clip in clips:
        times = tuple(
            float(value)
            for value in (
                clip.play_at_ms,
                clip.begin_trim_ms,
                clip.end_trim_ms,
                clip.source_duration_ms,
            )
        )
        if not all(math.isfinite(value) for value in times):
            raise ValueError("Music Track clip times must be finite")
        if (obj.bank_version or 0) >= 133:
            out += struct.pack(
                "<IIIdddd",
                _u32(clip.track_id, "Track ID"),
                _object_id(clip.source_id),
                _object_id(clip.event_id, allow_zero=True),
                *times,
            )
        else:
            out += struct.pack(
                "<IIdddd",
                _u32(clip.track_id, "Track ID"),
                _object_id(clip.source_id),
                *times,
            )
    if clips:
        out += struct.pack("<I", subtrack_count)
    out += obj.payload[track.playlist_end :]
    return bytes(out)


def _pack_id_list(values: tuple[int, ...]) -> bytes:
    return struct.pack("<I", len(values)) + (
        struct.pack(f"<{len(values)}I", *values) if values else b""
    )


def _object_id(value: int, *, allow_zero: bool = False) -> int:
    value = int(value)
    if value < 0 or value > 0xFFFFFFFF or (not allow_zero and value == 0):
        raise ValueError(f"Invalid Wwise ShortID: {value}")
    return value


def _u8(value: int, label: str) -> int:
    value = int(value)
    if not 0 <= value <= 0xFF:
        raise ValueError(f"{label} is outside the u8 range")
    return value


def _u16(value: int, label: str) -> int:
    value = int(value)
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"{label} is outside the u16 range")
    return value


def _u32(value: int, label: str) -> int:
    value = int(value)
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"{label} is outside the u32 range")
    return value


def _s16(value: int, label: str) -> int:
    value = int(value)
    if not -(1 << 15) <= value < (1 << 15):
        raise ValueError(f"{label} is outside the s16 range")
    return value


def _s32(value: int, label: str) -> int:
    value = int(value)
    if not -(1 << 31) <= value < (1 << 31):
        raise ValueError(f"{label} is outside the s32 range")
    return value


def _pack_hirc(objects: list[HircObject], trailing: bytes = b"") -> bytes:
    out = bytearray(struct.pack("<I", len(objects)))
    for obj in objects:
        out += struct.pack("<B", obj.type_id)
        out += struct.pack("<I", len(obj.payload))
        out += obj.payload
    out += trailing
    return bytes(out)


def _edit_hirc_chunk(
    chunk: bytes,
    *,
    bank_version: int,
    event_actions: dict[int, list[int] | tuple[int, ...]],
    action_targets: dict[int, int],
    hirc_upserts: dict[int | tuple[int, int], tuple[int, bytes]],
    deleted_hirc_ids: set[int],
    renamed_hirc_ids: dict[int, int],
    reference_targets: dict[tuple[int, int], int],
) -> bytes:
    parsed = _read_hirc_objects(chunk, bank_version=bank_version)
    if not parsed.complete:
        raise ValueError("Refusing to edit a truncated HIRC chunk")
    objects = parsed.objects
    _decode_hirc_objects(objects, bank_version)

    def group_by_id():
        grouped: dict[int, list[HircObject]] = {}
        for item in objects:
            grouped.setdefault(item.object_id, []).append(item)
        return grouped

    by_id = group_by_id()

    def unique(object_id: int, operation: str) -> HircObject | None:
        matches = by_id.get(object_id, ())
        if len(matches) > 1:
            raise ValueError(
                f"Cannot {operation} ShortID {object_id}: it identifies {len(matches)} HIRC objects"
            )
        return matches[0] if matches else None

    renames = {
        _object_id(old): _object_id(new)
        for old, new in renamed_hirc_ids.items()
        if int(old) != int(new)
    }
    missing = set(renames) - set(by_id)
    if missing:
        raise ValueError(f"Cannot rename missing HIRC object(s): {sorted(missing)}")
    for object_id in renames:
        unique(object_id, "rename")
    final_ids = (set(by_id) - set(renames)) | set(renames.values())
    if len(final_ids) != len(by_id) or len(set(renames.values())) != len(renames):
        raise ValueError("HIRC rename targets collide with existing objects")
    if renames:
        for obj in objects:
            payload = bytearray(obj.payload)
            offsets = {0, *(field.offset for field in obj.reference_fields)}
            for offset in sorted(offsets):
                if offset + 4 > len(payload):
                    continue
                old = struct.unpack_from("<I", obj.payload, offset)[0]
                if old in renames:
                    struct.pack_into("<I", payload, offset, renames[old])
            obj.payload = bytes(payload)
            obj.object_id = renames.get(obj.object_id, obj.object_id)
        _decode_hirc_objects(objects, bank_version)

    by_id = group_by_id()
    upsert_reference_baselines = {}

    def reference_signature(owner: HircObject, field: HircReference):
        return (
            field.role,
            field.target_kind,
            field.target_id,
            tuple(sorted(compatible_hirc_reference_types(owner, field))),
        )

    for raw_key, (raw_type, raw_payload) in hirc_upserts.items():
        type_id, payload = int(raw_type), bytes(raw_payload)
        if not 0 < type_id <= 0xFF:
            raise ValueError(f"Invalid HIRC type: {type_id}")
        typed = isinstance(raw_key, tuple)
        if typed:
            if len(raw_key) != 2:
                raise ValueError(f"Invalid typed HIRC key: {raw_key!r}")
            object_id, key_type = _object_id(raw_key[0]), int(raw_key[1])
            if key_type != type_id:
                raise ValueError(
                    f"HIRC key type 0x{key_type:02X} does not match payload type 0x{type_id:02X}"
                )
        else:
            object_id = _object_id(raw_key)
        if len(payload) < 4 or struct.unpack_from("<I", payload)[0] != object_id:
            raise ValueError(f"HIRC payload {object_id} does not start with its ShortID")
        if typed:
            matches = [obj for obj in by_id.get(object_id, ()) if obj.type_id == type_id]
            if len(matches) > 1:
                raise ValueError(
                    f"Cannot update type 0x{type_id:02X} ShortID {object_id}: "
                    f"it identifies {len(matches)} HIRC objects"
                )
            existing = matches[0] if matches else None
            if existing is None and object_id in by_id:
                existing_types = ", ".join(
                    f"0x{obj.type_id:02X}" for obj in by_id[object_id]
                )
                raise ValueError(
                    f"Cannot insert type 0x{type_id:02X} ShortID {object_id}: "
                    f"that ShortID already exists as {existing_types}"
                )
        else:
            existing = unique(object_id, "update")
        baseline = existing
        if baseline is None:
            baseline = next((
                obj for obj in objects
                if obj.type_id == type_id and obj.payload[4:] == payload[4:]
            ), None)
        upsert_reference_baselines[(object_id, type_id)] = Counter(
            reference_signature(baseline, field)
            for field in (baseline.reference_fields if baseline else ())
        )
        if existing is None:
            existing = HircObject(
                len(objects) + 1,
                type_id,
                _hirc_type_name(type_id, bank_version),
                object_id,
                payload,
                0,
                bank_version=bank_version,
            )
            objects.append(existing)
            by_id[object_id] = [existing]
        else:
            existing.type_id, existing.type_name, existing.payload = (
                type_id,
                _hirc_type_name(type_id, bank_version),
                payload,
            )

    conflicts = (
        set(event_actions)
        | set(action_targets)
        | {key[0] for key in reference_targets}
    ) & set(deleted_hirc_ids)
    if conflicts:
        raise ValueError(f"HIRC objects cannot be updated and deleted together: {sorted(conflicts)}")
    remaining = set(by_id) - set(deleted_hirc_ids)
    for event_id, raw_actions in event_actions.items():
        event_id = _object_id(event_id)
        actions = tuple(_object_id(value) for value in raw_actions)
        existing = unique(event_id, "edit Event")
        if existing is not None and existing.type_id != 0x04:
            raise ValueError(f"HIRC object {event_id} already exists and is not an Event")
        invalid = [
            value for value in actions
            if value not in remaining
            or len(by_id[value]) != 1
            or by_id[value][0].type_id != 0x03
        ]
        if invalid:
            raise ValueError(
                f"Event {event_id} references missing/non-Action objects: {invalid}"
            )
        payload = _pack_event_payload(event_id, actions)
        if existing is None:
            existing = HircObject(
                len(objects) + 1, 0x04, "Event", event_id, payload, 0
            )
            objects.append(existing)
            by_id[event_id] = [existing]
            remaining.add(event_id)
        else:
            existing.payload = payload

    # Re-decode every changed payload before accepting its newly introduced
    # references. Unchanged cross-bank links remain valid, while new HIRC links
    # must resolve to one compatible local object.
    reparsed = _read_hirc_objects(
        _pack_hirc(objects), bank_version=bank_version
    )
    objects = reparsed.objects
    _decode_hirc_objects(objects, bank_version)
    by_id = group_by_id()
    remaining_objects = [
        obj for obj in objects if obj.object_id not in deleted_hirc_ids
    ]
    for (object_id, type_id), baseline in upsert_reference_baselines.items():
        matches = [
            obj for obj in by_id.get(object_id, ()) if obj.type_id == type_id
        ]
        if len(matches) != 1:
            raise ValueError(
                f"Cannot validate type 0x{type_id:02X} ShortID {object_id}: "
                f"it identifies {len(matches)} objects"
            )
        owner = matches[0]
        preserved = baseline.copy()
        for reference in owner.reference_fields:
            signature = reference_signature(owner, reference)
            if preserved[signature]:
                preserved[signature] -= 1
            elif reference.target_kind in {"hirc", "event"}:
                _validate_hirc_reference_target(
                    owner, reference, reference.target_id, remaining_objects
                )

    for action_id, target_id in action_targets.items():
        action_id = _object_id(action_id)
        obj = unique(action_id, "edit Action")
        if obj is None or obj.type_id != 0x03:
            raise ValueError(f"HIRC object {action_id} is not an editable Action")
        if len(obj.payload) < 10:
            raise ValueError(f"Action {action_id} is too short to edit")
        if obj.action_target_kind not in {"object", "event"}:
            raise ValueError(
                f"Action {action_id} uses its ID field as a {obj.action_target_kind}; "
                "edit its typed Action properties instead"
            )
        field = next((
            field for field in obj.reference_fields if field.offset == 6
        ), HircReference(
            6,
            int(target_id),
            "event target" if obj.action_target_kind == "event" else "action target",
            "event" if obj.action_target_kind == "event" else "hirc",
        ))
        _validate_hirc_reference_target(
            obj, field, target_id, remaining_objects
        )
        obj.payload = patch_hirc_reference(obj, 6, target_id)

    for (object_id, offset), target_id in reference_targets.items():
        object_id = _object_id(object_id)
        obj = unique(object_id, "edit reference in")
        if obj is None:
            raise ValueError(f"Cannot edit reference in missing HIRC object {object_id}")
        fields = [field for field in obj.reference_fields if field.offset == int(offset)]
        if len(fields) != 1:
            raise ValueError(
                f"Offset {offset} in HIRC object {object_id} is not one safely decoded reference"
            )
        _validate_hirc_reference_target(
            obj, fields[0], target_id, remaining_objects
        )
        obj.payload = patch_hirc_reference(obj, offset, target_id)

    missing_deletes = set(deleted_hirc_ids) - set(by_id)
    if missing_deletes:
        raise ValueError(f"Cannot delete missing HIRC object(s): {sorted(missing_deletes)}")
    for object_id in deleted_hirc_ids:
        unique(object_id, "delete")
    rewritten = [obj for obj in objects if obj.object_id not in deleted_hirc_ids]
    return _pack_hirc(rewritten, parsed.trailing)


def _hirc_source_map(payload: bytes, bank_version: int) -> dict[int, tuple[BnkSource, ...]]:
    """Decode every HIRC object that carries bank source records."""

    parsed = _read_hirc_objects(payload, bank_version=bank_version)
    _decode_hirc_objects(parsed.objects, bank_version)
    return {
        obj.object_id: tuple(obj.sources)
        for obj in parsed.objects
        if obj.sources
    }


def _source_id_renames(
    before: dict[int, tuple[BnkSource, ...]],
    after: dict[int, tuple[BnkSource, ...]],
) -> dict[int, int]:
    """Return source IDs retargeted in place on an unchanged HIRC object."""

    renames: dict[int, int] = {}
    still_in_use = {
        source.source_id for sources in after.values() for source in sources
    }
    for object_id, old_sources in before.items():
        new_sources = after.get(object_id)
        if not new_sources or len(old_sources) != len(new_sources):
            continue
        for old, new in zip(old_sources, new_sources):
            if old.source_id == new.source_id:
                continue
            if (old.plugin_id & 0x0F) != (new.plugin_id & 0x0F):
                continue
            if old.stream_type != new.stream_type:
                continue
            if old.source_id in still_in_use:
                # Another object still uses it; leave the shared media entry.
                continue
            renames[old.source_id] = new.source_id
    return renames


def _rekey_didx(chunks: list[ChunkRecord], renames: dict[int, int]) -> None:
    """Re-point embedded-media entries whose source ID was retargeted."""

    if not renames:
        return
    index = next(
        (i for i, chunk in enumerate(chunks) if chunk.chunk_id == b"DIDX"), None
    )
    if index is None:
        return
    entries = _read_didx(chunks[index].payload)
    if not any(entry.source_id in renames for entry in entries):
        return
    # Process unchanged entries first so they keep their media if a rename
    # collides with an existing ID; later renames to an already-claimed target
    # are dropped to avoid duplicate DIDX rows.
    claimed: set[int] = set()
    payload = bytearray()
    for entry in sorted(entries, key=lambda item: item.source_id in renames):
        target = renames.get(entry.source_id, entry.source_id)
        if target in claimed:
            continue
        claimed.add(target)
        payload += struct.pack(_DIDX_ENTRY_FMT, target, entry.offset, entry.length)
    chunks[index] = ChunkRecord(b"DIDX", bytes(payload))


def _rewrite_bnk(
    data: bytes,
    replacements: dict[int, bytes],
    *,
    event_actions: dict[int, list[int] | tuple[int, ...]],
    action_targets: dict[int, int],
    hirc_upserts: dict[int | tuple[int, int], tuple[int, bytes]],
    deleted_hirc_ids: set[int],
    renamed_hirc_ids: dict[int, int],
    reference_targets: dict[tuple[int, int], int],
    bank_chunk_payloads: dict[bytes | str, bytes],
) -> bytes:
    chunks, trailing = _split_chunk_records(data)
    header = next((chunk.payload for chunk in chunks if chunk.chunk_id == b"BKHD"), None)
    bank_version = _read_bank_version(header)
    if bank_version not in STRUCTURED_BANK_VERSIONS:
        raise ValueError(
            f"Wwise bank version {bank_version} has no registered rewrite schema"
        )

    for raw_id, payload in bank_chunk_payloads.items():
        chunk_id = raw_id.encode("ascii") if isinstance(raw_id, str) else bytes(raw_id)
        if len(chunk_id) != 4 or chunk_id in {b"HIRC", b"DATA", b"DIDX"}:
            raise ValueError(f"Bank chunk {chunk_id!r} is not a settings chunk")
        matches = [index for index, chunk in enumerate(chunks) if chunk.chunk_id == chunk_id]
        if len(matches) != 1:
            raise ValueError(
                f"Expected exactly one {chunk_id.decode('ascii', 'replace')} chunk; "
                f"found {len(matches)}"
            )
        decoded = parse_structured_chunk(
            chunk_id, bytes(payload), 132 if bank_version == 125 else bank_version
        )
        if decoded is None or not decoded.structure.complete:
            reason = decoded.structure.error if decoded else "unsupported chunk"
            raise ValueError(
                f"Refusing incomplete {chunk_id.decode('ascii', 'replace')} settings: {reason}"
            )
        index = matches[0]
        chunks[index] = ChunkRecord(chunk_id, bytes(payload))

    if any((event_actions, action_targets, hirc_upserts, deleted_hirc_ids, renamed_hirc_ids, reference_targets)):
        hirc_index = next((i for i, chunk in enumerate(chunks) if chunk.chunk_id == b"HIRC"), None)
        if hirc_index is None:
            raise ValueError("BNK has no HIRC chunk")
        sources_before = _hirc_source_map(chunks[hirc_index].payload, bank_version)
        chunks[hirc_index] = ChunkRecord(
            chunk_id=b"HIRC",
            payload=_edit_hirc_chunk(
                chunks[hirc_index].payload,
                bank_version=bank_version,
                event_actions=event_actions,
                action_targets=action_targets,
                hirc_upserts=hirc_upserts,
                deleted_hirc_ids=deleted_hirc_ids,
                renamed_hirc_ids=renamed_hirc_ids,
                reference_targets=reference_targets,
            ),
        )
        sources_after = _hirc_source_map(chunks[hirc_index].payload, bank_version)
        _rekey_didx(chunks, _source_id_renames(sources_before, sources_after))

    if replacements:
        _rewrite_bnk_media(chunks, replacements, bank_version)
    return _pack_chunk_records(chunks, trailing)


def _rewrite_bnk_media(
    chunks: list[ChunkRecord],
    replacements: dict[int, bytes],
    bank_version: int,
) -> None:
    hirc_index = next((i for i, chunk in enumerate(chunks) if chunk.chunk_id == b"HIRC"), None)
    hirc = (
        _read_hirc_objects(chunks[hirc_index].payload, bank_version=bank_version)
        if hirc_index is not None else _HircReadResult([], b"", False)
    )
    _decode_hirc_objects(hirc.objects, bank_version)
    sources = [source for obj in hirc.objects for source in obj.sources]
    embedded_ids = {source.source_id for source in sources if source.stream_type == 0}
    prefetch_ids = {source.source_id for source in sources if source.stream_type == 1}

    didx_index = next((i for i, chunk in enumerate(chunks) if chunk.chunk_id == b"DIDX"), None)
    data_index = next((i for i, chunk in enumerate(chunks) if chunk.chunk_id == b"DATA"), None)
    if (didx_index is None) != (data_index is None):
        raise ValueError("BNK has only one of DIDX/DATA; refusing to rebuild media")
    entries = _read_didx(chunks[didx_index].payload) if didx_index is not None else []
    existing_ids = {entry.source_id for entry in entries}
    media_target_ids = set(replacements) & (existing_ids | embedded_ids | prefetch_ids)
    hirc_target_ids = set(replacements) & (embedded_ids | prefetch_ids)
    if not media_target_ids and not hirc_target_ids:
        return

    entry_lengths = {entry.source_id: entry.length for entry in entries}
    prefetch_sizes = {
        source.source_id: min(
            source.in_memory_size or entry_lengths.get(source.source_id, 0),
            len(replacements[source.source_id]),
        )
        for source in sources
        if source.stream_type == 1 and source.source_id in hirc_target_ids
    }
    if hirc.complete and hirc_target_ids:
        for obj in hirc.objects:
            payload = bytearray(obj.payload)
            changed = False
            for source in obj.sources:
                if source.source_id not in hirc_target_ids or source.stream_type not in {0, 1}:
                    continue
                size = (
                    len(replacements[source.source_id])
                    if source.stream_type == 0 else prefetch_sizes[source.source_id]
                )
                struct.pack_into("<I", payload, source.payload_offset + 4, size)
                changed = True
            if changed:
                obj.payload = bytes(payload)
        chunks[hirc_index] = ChunkRecord(
            b"HIRC", _pack_hirc(hirc.objects, hirc.trailing)
        )
    if not media_target_ids:
        return

    if didx_index is None:
        insert_at = hirc_index if hirc_index is not None else len(chunks)
        chunks[insert_at:insert_at] = [ChunkRecord(b"DIDX", b""), ChunkRecord(b"DATA", b"")]
        didx_index, data_index = insert_at, insert_at + 1
    for source_id in sorted(media_target_ids - existing_ids):
        entries.append(BnkEmbeddedAudio(source_id, 0, 0))
    media = dict(replacements)
    for source_id, size in prefetch_sizes.items():
        if source_id in media_target_ids:
            media[source_id] = bytes(media[source_id])[:size]

    old_data = chunks[data_index].payload
    alignment = _infer_media_alignment(entries) if existing_ids else 16
    new_didx, new_data = bytearray(), bytearray()
    for entry in entries:
        while len(new_data) % alignment:
            new_data.append(0)
        payload = bytes(media[entry.source_id]) if entry.source_id in media_target_ids else _safe_slice(
            old_data, entry.offset, entry.length
        )
        new_didx += struct.pack(_DIDX_ENTRY_FMT, entry.source_id, len(new_data), len(payload))
        new_data += payload
    chunks[didx_index] = ChunkRecord(b"DIDX", bytes(new_didx))
    chunks[data_index] = ChunkRecord(b"DATA", bytes(new_data))


def _infer_media_alignment(entries: list[BnkEmbeddedAudio]) -> int:
    offsets = [entry.offset for entry in entries if entry.offset]
    if not offsets:
        return 1
    common = offsets[0]
    for offset in offsets[1:]:
        common = math.gcd(common, offset)
    for candidate in (16, 8, 4, 2):
        if common % candidate == 0:
            return candidate
    return 1
