"""Identify and convert the media payloads stored in Wwise banks/packages."""

from __future__ import annotations

import io
import math
import struct
import wave
from dataclasses import dataclass
from enum import Enum


WWISE_MIDI_PLUGIN_ID = 0x00100001
IZOTOPE_HYBRID_REVERB_PLUGIN_ID = 0x00021033
WWISE_CONVOLUTION_REVERB_PLUGIN_ID = 0x007F0003
CRANKCASE_REV_PLUGIN_ID = 0x01A01052


class WwiseMediaKind(str, Enum):
    AUDIO = "audio"
    MIDI = "midi"
    HYBRID_REVERB_IR = "hybrid_reverb_ir"
    CONVOLUTION_REVERB_IR = "convolution_reverb_ir"
    CRANKCASE_REV_MODEL = "crankcase_rev_model"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class WwiseMidi:
    division: int
    bpm: float
    tracks: tuple[bytes, ...]
    event_count: int
    note_count: int
    duration_ticks: int

    @property
    def duration_seconds(self) -> float:
        return self.duration_ticks * 60.0 / (self.division * self.bpm)


@dataclass(frozen=True, slots=True)
class HybridReverbMedia:
    decay_time: float
    decay_low_frequency: float
    decay_high_frequency: float
    decay_low_ratio: float
    decay_mid_ratio: float
    decay_high_ratio: float
    analysis_value: float
    filter_length: int
    frame_count: int
    channels: int
    filters: tuple[tuple[float, ...], tuple[float, ...]]
    samples: tuple[float, ...]

    @property
    def tuning(self) -> tuple[float, ...]:
        return (
            self.decay_time,
            self.decay_low_frequency,
            self.decay_high_frequency,
            self.decay_low_ratio,
            self.decay_mid_ratio,
            self.decay_high_ratio,
        )


@dataclass(frozen=True, slots=True)
class ConvolutionReverbMedia:
    """Header and decoded layout of Wwise Convolution Reverb plug-in media."""

    format_tag: int
    fft_size: int
    sample_rate: int
    channel_config: int
    channels: int
    analysis_entries: int
    estimated_rt60_seconds: float
    peak_db: float
    table_entries: int
    sample_count: int
    block_size: int
    data_offset: int

    @property
    def duration_seconds(self) -> float:
        return self.sample_count / self.sample_rate


@dataclass(frozen=True, slots=True)
class _MidiEvent:
    delta: int
    body: bytes
    status: int
    meta_type: int | None = None
    tempo_us: int | None = None
    note_on: bool = False


def _read_vlq(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for _ in range(4):
        if offset >= len(data):
            raise ValueError("MIDI ends inside a variable-length number.")
        byte = data[offset]
        offset += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, offset
    raise ValueError("MIDI variable-length number is longer than four bytes.")


def _write_vlq(value: int) -> bytes:
    if not 0 <= value <= 0x0FFFFFFF:
        raise ValueError("MIDI delta time is out of range.")
    encoded = bytearray((value & 0x7F,))
    value >>= 7
    while value:
        encoded.insert(0, 0x80 | (value & 0x7F))
        value >>= 7
    return bytes(encoded)


def _parse_midi_track(data: bytes, offset: int = 0) -> tuple[list[_MidiEvent], int]:
    events: list[_MidiEvent] = []
    running_status = None
    while offset < len(data):
        delta, offset = _read_vlq(data, offset)
        if offset >= len(data):
            raise ValueError("MIDI track ends before its event.")
        byte = data[offset]
        explicit_status = byte >= 0x80
        if explicit_status:
            status = byte
            offset += 1
        elif running_status is not None:
            status = running_status
        else:
            raise ValueError("MIDI uses running status before declaring a status byte.")

        meta_type = tempo_us = None
        note_on = False
        if 0x80 <= status <= 0xEF:
            running_status = status
            length = 1 if status >> 4 in (0xC, 0xD) else 2
            end = offset + length
            if end > len(data) or any(value >= 0x80 for value in data[offset:end]):
                raise ValueError("MIDI channel event is truncated or invalid.")
            values = data[offset:end]
            offset = end
            body = bytes((status,)) + values
            note_on = status >> 4 == 0x9 and values[1] != 0
        elif status == 0xFF:
            if offset >= len(data):
                raise ValueError("MIDI meta event is truncated.")
            meta_type = data[offset]
            offset += 1
            length, offset = _read_vlq(data, offset)
            end = offset + length
            if end > len(data):
                raise ValueError("MIDI meta event data is truncated.")
            value = data[offset:end]
            offset = end
            body = b"\xFF" + bytes((meta_type,)) + _write_vlq(length) + value
            if meta_type == 0x51:
                if length != 3 or not int.from_bytes(value, "big"):
                    raise ValueError("MIDI tempo event is invalid.")
                tempo_us = int.from_bytes(value, "big")
            if meta_type == 0x2F:
                if length:
                    raise ValueError("MIDI end-of-track event must be empty.")
                events.append(_MidiEvent(delta, body, status, meta_type))
                return events, offset
        elif status in (0xF0, 0xF7):
            length, offset = _read_vlq(data, offset)
            end = offset + length
            if end > len(data):
                raise ValueError("MIDI system-exclusive event is truncated.")
            body = bytes((status,)) + _write_vlq(length) + data[offset:end]
            offset = end
        else:
            lengths = {0xF1: 1, 0xF2: 2, 0xF3: 1, 0xF6: 0, 0xF8: 0,
                       0xFA: 0, 0xFB: 0, 0xFC: 0, 0xFE: 0}
            if status not in lengths:
                raise ValueError(f"Unsupported MIDI status 0x{status:02X}.")
            end = offset + lengths[status]
            if end > len(data):
                raise ValueError("MIDI system event is truncated.")
            body = bytes((status,)) + data[offset:end]
            offset = end
            if status < 0xF8:
                running_status = None
        events.append(
            _MidiEvent(delta, body, status, meta_type, tempo_us, note_on)
        )
    raise ValueError("MIDI track has no end-of-track event.")


def _encode_midi_track(events: list[_MidiEvent]) -> bytes:
    encoded, running_status = bytearray(), None
    for event in events:
        body = event.body
        if 0x80 <= event.status <= 0xEF:
            if event.status == running_status:
                body = body[1:]
            else:
                running_status = event.status
        else:
            running_status = None
        encoded += _write_vlq(event.delta) + body
    return bytes(encoded)


def parse_wwise_midi(data: bytes) -> WwiseMidi:
    """Parse Wwise's compact MIDI representation used by legacy SoundBanks."""

    if len(data) < 10:
        raise ValueError("Wwise MIDI payload is too short.")
    division = struct.unpack_from(">H", data)[0]
    bpm = struct.unpack_from("<f", data, 2)[0]
    if not 0 < division < 0x8000 or not math.isfinite(bpm) or not 1.0 <= bpm <= 1000.0:
        raise ValueError("Wwise MIDI header is invalid.")
    offset, tracks, event_count, note_count, duration = 6, [], 0, 0, 0
    while offset < len(data):
        events, end = _parse_midi_track(data, offset)
        track = data[offset:end]
        tracks.append(track)
        event_count += len(events)
        note_count += sum(event.note_on for event in events)
        duration = max(duration, sum(event.delta for event in events))
        offset = end
    if not tracks:
        raise ValueError("Wwise MIDI contains no tracks.")
    return WwiseMidi(
        division, bpm, tuple(tracks), event_count, note_count, duration
    )


def _parse_standard_midi(data: bytes) -> tuple[int, list[list[_MidiEvent]]]:
    if len(data) < 14 or data[:4] != b"MThd":
        raise ValueError("Input is not a Standard MIDI File.")
    header_length = int.from_bytes(data[4:8], "big")
    if header_length < 6 or 8 + header_length > len(data):
        raise ValueError("MIDI header is truncated.")
    midi_format, track_count, division = struct.unpack_from(">HHH", data, 8)
    if midi_format not in (0, 1) or not track_count:
        raise ValueError("Only Standard MIDI format 0 and 1 files are supported.")
    if not 0 < division < 0x8000:
        raise ValueError("SMPTE-time MIDI files cannot be imported into Wwise MIDI.")
    offset, tracks = 8 + header_length, []
    for _ in range(track_count):
        if offset + 8 > len(data) or data[offset:offset + 4] != b"MTrk":
            raise ValueError("MIDI track chunk is missing or truncated.")
        length = int.from_bytes(data[offset + 4:offset + 8], "big")
        start, end = offset + 8, offset + 8 + length
        if end > len(data):
            raise ValueError("MIDI track chunk is truncated.")
        events, consumed = _parse_midi_track(data[start:end])
        if consumed != length:
            raise ValueError("MIDI track contains bytes after its end marker.")
        tracks.append(events)
        offset = end
    if offset != len(data):
        raise ValueError("MIDI contains unsupported trailing chunks or bytes.")
    return division, tracks


def midi_to_wwise(data: bytes) -> bytes:
    """Convert a constant-tempo Standard MIDI File to Wwise's compact form."""

    division, tracks = _parse_standard_midi(data)
    tempos = {
        event.tempo_us
        for track in tracks
        for event in track
        if event.tempo_us is not None
    }
    if len(tempos) > 1:
        raise ValueError(
            "Wwise MIDI stores one global tempo; MIDI files with tempo changes "
            "cannot be imported without changing timing."
        )
    tempo_us = next(iter(tempos), 500_000)
    bpm = 60_000_000.0 / tempo_us
    compact_tracks = []
    for track in tracks:
        carry, kept = 0, []
        for event in track:
            if event.tempo_us is not None:
                carry += event.delta
                continue
            kept.append(
                _MidiEvent(
                    event.delta + carry,
                    event.body,
                    event.status,
                    event.meta_type,
                    note_on=event.note_on,
                )
            )
            carry = 0
        if not kept or kept[-1].meta_type != 0x2F:
            raise ValueError("MIDI track has no end-of-track marker.")
        compact_tracks.append(_encode_midi_track(kept))
    result = struct.pack(">H", division) + struct.pack("<f", bpm) + b"".join(compact_tracks)
    parse_wwise_midi(result)
    return result


def wwise_to_midi(data: bytes) -> bytes:
    """Wrap Wwise compact MIDI as an editable Standard MIDI File."""

    midi = parse_wwise_midi(data)
    tempo_us = max(1, min(0xFFFFFF, round(60_000_000.0 / midi.bpm)))
    tempo = b"\x00\xFF\x51\x03" + tempo_us.to_bytes(3, "big")
    tracks = list(midi.tracks)
    tracks[0] = tempo + tracks[0]
    header = struct.pack(">4sIHHH", b"MThd", 6, 1 if len(tracks) > 1 else 0,
                         len(tracks), midi.division)
    return header + b"".join(
        b"MTrk" + len(track).to_bytes(4, "big") + track for track in tracks
    )


_IR_RANGES = (
    (0.125, 5.0),
    (20.0, 1000.0),
    (800.0, 20_000.0),
    (0.25, 2.0),
    (0.25, 2.0),
    (0.25, 2.0),
)


def parse_hybrid_reverb_media(data: bytes) -> HybridReverbMedia:
    """Parse compiled iZotope Hybrid Reverb early-reflection media."""

    if len(data) < 44:
        raise ValueError("Hybrid Reverb media is too short.")
    values = struct.unpack_from("<7f", data)
    if any(
        not math.isfinite(value) or not low <= value <= high
        for value, (low, high) in zip(values[:6], _IR_RANGES)
    ) or not math.isfinite(values[6]):
        raise ValueError("Hybrid Reverb tuning header is invalid.")
    filter_length = struct.unpack_from("<I", data, 28)[0]
    if not 1 <= filter_length <= 4096:
        raise ValueError("Hybrid Reverb filter length is invalid.")
    frame_offset = 32 + filter_length * 8
    if frame_offset + 4 > len(data):
        raise ValueError("Hybrid Reverb filter data is truncated.")
    frame_count = struct.unpack_from("<I", data, frame_offset)[0]
    sample_bytes = len(data) - frame_offset - 4
    if not frame_count or sample_bytes % (frame_count * 4):
        raise ValueError("Hybrid Reverb sample layout is invalid.")
    channels = sample_bytes // (frame_count * 4)
    if channels not in (1, 2):
        raise ValueError("Hybrid Reverb media must be mono or stereo.")
    filters = struct.unpack_from(f"<{filter_length * 2}f", data, 32)
    samples = struct.unpack_from(
        f"<{frame_count * channels}f", data, frame_offset + 4
    )
    if not all(math.isfinite(value) for value in filters) or not all(
        math.isfinite(value) for value in samples
    ):
        raise ValueError("Hybrid Reverb media contains non-finite samples.")
    split = filter_length
    return HybridReverbMedia(
        *values,
        filter_length,
        frame_count,
        channels,
        (tuple(filters[:split]), tuple(filters[split:])),
        tuple(samples),
    )


def hybrid_reverb_to_wav(data: bytes, sample_rate: int = 48_000) -> bytes:
    """Export the compiled, channel-major early reflections as editable PCM24."""

    media = parse_hybrid_reverb_media(data)
    frames = bytearray()
    for frame in range(media.frame_count):
        for channel in range(media.channels):
            sample = media.samples[channel * media.frame_count + frame]
            value = round(max(-1.0, min(1.0, sample)) * 0x7FFFFF)
            frames += int(value).to_bytes(3, "little", signed=True)
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(media.channels)
        wav.setsampwidth(3)
        wav.setframerate(sample_rate)
        wav.writeframes(frames)
    return output.getvalue()


def parse_convolution_reverb_media(data: bytes) -> ConvolutionReverbMedia:
    """Parse Wwise's compiled Convolution Reverb header and float IR layout."""

    if len(data) < 52:
        raise ValueError("Convolution Reverb media is too short.")
    (
        format_tag, fft_size, sample_rate, channel_config, analysis_entries,
        rt60, peak_db, table_entries, declared_samples, block_size,
    ) = struct.unpack_from("<IIIIIffIII", data)
    channels = channel_config & 0xFF
    if format_tag not in {0x00000400, 0x00020400} or fft_size != 0x800:
        raise ValueError("Convolution Reverb format header is invalid.")
    if not 8_000 <= sample_rate <= 384_000 or not 1 <= channels <= 32:
        raise ValueError("Convolution Reverb sample rate or channel layout is invalid.")
    if not analysis_entries or not table_entries or table_entries > 0x100000:
        raise ValueError("Convolution Reverb analysis table is invalid.")
    if not math.isfinite(rt60) or rt60 < 0 or not math.isfinite(peak_db):
        raise ValueError("Convolution Reverb analysis values are invalid.")
    data_offset = 0x30 + ((table_entries * 2 + 0x0F) & ~0x0F)
    sample_bytes = len(data) - data_offset
    frame_size = channels * 4
    if data_offset > len(data) or sample_bytes <= 0 or sample_bytes % frame_size:
        raise ValueError("Convolution Reverb float sample layout is invalid.")
    sample_count = sample_bytes // frame_size
    if declared_samples != sample_count or not block_size:
        raise ValueError("Convolution Reverb sample count or block size is invalid.")
    if any(not math.isfinite(value[0]) for value in struct.iter_unpack("<f", data[data_offset:])):
        raise ValueError("Convolution Reverb media contains non-finite samples.")
    return ConvolutionReverbMedia(
        format_tag, fft_size, sample_rate, channel_config, channels,
        analysis_entries, rt60, peak_db, table_entries, sample_count,
        block_size, data_offset,
    )


def convolution_reverb_to_wav(data: bytes) -> bytes:
    """Export Wwise's interleaved float IR as editable PCM24 WAV."""

    media = parse_convolution_reverb_media(data)
    frames = bytearray()
    for sample, in struct.iter_unpack("<f", data[media.data_offset:]):
        value = round(max(-1.0, min(1.0, sample)) * 0x7FFFFF)
        frames += int(value).to_bytes(3, "little", signed=True)
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(media.channels)
        wav.setsampwidth(3)
        wav.setframerate(media.sample_rate)
        wav.writeframes(frames)
    return output.getvalue()


def detect_media_kind(data: bytes = b"", plugin_id: int | None = None) -> WwiseMediaKind:
    """Classify media without treating arbitrary non-RIFF data as audio."""

    if plugin_id == WWISE_MIDI_PLUGIN_ID:
        return WwiseMediaKind.MIDI
    if plugin_id == IZOTOPE_HYBRID_REVERB_PLUGIN_ID:
        return WwiseMediaKind.HYBRID_REVERB_IR
    if plugin_id == WWISE_CONVOLUTION_REVERB_PLUGIN_ID:
        return WwiseMediaKind.CONVOLUTION_REVERB_IR
    if plugin_id == CRANKCASE_REV_PLUGIN_ID or data[:4] == b"ADM3":
        return WwiseMediaKind.CRANKCASE_REV_MODEL
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return WwiseMediaKind.AUDIO
    # Wwise source-codec plug-ins have type 1 in the low nibble. MIDI is the
    # only such non-audio plug-in used by the supported games and is handled above.
    if plugin_id is not None and plugin_id & 0xF == 1:
        return WwiseMediaKind.AUDIO
    for parser, kind in (
        (parse_wwise_midi, WwiseMediaKind.MIDI),
        (parse_hybrid_reverb_media, WwiseMediaKind.HYBRID_REVERB_IR),
        (parse_convolution_reverb_media, WwiseMediaKind.CONVOLUTION_REVERB_IR),
    ):
        try:
            parser(data)
            return kind
        except (ValueError, struct.error):  # noqa: PERF203 - probe next format
            pass
    return WwiseMediaKind.UNKNOWN


def validate_crankcase_rev_model(data: bytes) -> None:
    """Accept only complete-looking REV generation-3 compiled model payloads."""

    if len(data) < 48 or data[:4] != b"ADM3":
        raise ValueError("Expected a compiled Crankcase Audio REV ADM3 model.")


__all__ = [
    "CRANKCASE_REV_PLUGIN_ID",
    "ConvolutionReverbMedia",
    "HybridReverbMedia",
    "IZOTOPE_HYBRID_REVERB_PLUGIN_ID",
    "WWISE_CONVOLUTION_REVERB_PLUGIN_ID",
    "WWISE_MIDI_PLUGIN_ID",
    "WwiseMediaKind",
    "WwiseMidi",
    "convolution_reverb_to_wav",
    "detect_media_kind",
    "hybrid_reverb_to_wav",
    "midi_to_wwise",
    "parse_convolution_reverb_media",
    "parse_hybrid_reverb_media",
    "parse_wwise_midi",
    "validate_crankcase_rev_model",
    "wwise_to_midi",
]
