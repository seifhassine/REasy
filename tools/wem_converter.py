"""Author WAV files as game-profiled Wwise media."""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from html import escape
from pathlib import Path

from file_handlers.sound.sound_profile import SoundGameProfile, WemAuthoringCodec
from tools.riff_metadata import (
    RiffMetadata,
    inherit_riff_metadata,
    read_riff_metadata,
    rescale_riff_metadata,
    write_riff_metadata,
)
from tools.wwise_toolchain import (
    WwiseInstallation,
    require_wwise_profile,
    validate_wwise_installation,
)
from utils.app_paths import cache_directory


_EXTENSIBLE_SUBFORMAT_SUFFIX = bytes.fromhex("00001000800000aa00389b71")
SAMPLE_RATE_MATCH_ORIGINAL = "match_original"
SAMPLE_RATE_KEEP_SOURCE = "keep_source"
QUALITY_PRESETS = ("low", "medium", "high", "maximum")
COMPRESSION_MODE_QUALITY = "quality"
COMPRESSION_MODE_BITRATE = "bitrate"
CHANNEL_AS_INPUT = "as_input"
CHANNEL_MONO = "mono"
CHANNEL_STEREO = "stereo"
_CHANNEL_VALUES = {
    CHANNEL_MONO: 0,
    CHANNEL_STEREO: 1,
    CHANNEL_AS_INPUT: 4,
}
_PROJECT_CACHE_VERSION = 1
_PROJECT_TEMPLATE_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class WemSampleRatePlan:
    source_rate: int
    original_rate: int | None
    target_rate: int
    policy: str
    forced_by_codec: bool = False

    @property
    def resamples(self) -> bool:
        return self.source_rate != self.target_rate


@dataclass(frozen=True, slots=True)
class WemCompressionSettings:
    """Optional exact Wwise compression and channel-conversion overrides."""

    mode: str = COMPRESSION_MODE_QUALITY
    quality: float | int | None = None
    average_bitrate: float | None = None
    minimum_bitrate: float | None = None
    maximum_bitrate: float | None = None
    channel_mode: str = CHANNEL_AS_INPUT


@dataclass(frozen=True, slots=True)
class WemConversionRequest:
    """One WAV and the original-media policy needed to author its WEM."""

    source_path: str | Path
    preserve_metadata_from: bytes | None = None
    match_codec_from: bytes | None = None
    sample_rate_policy: str = SAMPLE_RATE_MATCH_ORIGINAL
    codec_tag: int | None = None
    quality_preset: str | None = None
    compression: WemCompressionSettings | None = None


@dataclass(slots=True)
class _PreparedConversion:
    wav_data: bytes
    codec: WemAuthoringCodec
    rate_plan: WemSampleRatePlan
    quality_preset: str | None
    compression: WemCompressionSettings | None
    source_metadata: RiffMetadata
    preserved_metadata: RiffMetadata | None
    expected_metadata: RiffMetadata | None


def _riff_chunks(blob: bytes) -> dict[bytes, bytes]:
    if len(blob) < 12 or blob[:4] != b"RIFF" or blob[8:12] != b"WAVE":
        raise ValueError("Input is not a RIFF/WAVE file.")

    chunks = {}
    offset = 12
    while offset + 8 <= len(blob):
        chunk_id = blob[offset : offset + 4]
        size = int.from_bytes(blob[offset + 4 : offset + 8], "little")
        offset += 8
        end = offset + size
        if end > len(blob):
            raise ValueError("WAV contains a truncated chunk.")
        chunks.setdefault(chunk_id, blob[offset:end])
        offset = end + (size & 1)
    return chunks


def _read_source_wav(source_path: Path) -> bytes:
    if source_path.suffix.casefold() != ".wav":
        raise ValueError(
            "Wwise source import accepts WAV files only. Convert this file to WAV "
            "before importing it."
        )

    wav_data = source_path.read_bytes()
    chunks = _riff_chunks(wav_data)
    fmt = chunks.get(b"fmt ", b"")
    if len(fmt) < 16 or not chunks.get(b"data"):
        raise ValueError("WAV must contain valid, non-empty fmt and data chunks.")
    bits_per_sample = struct.unpack_from("<H", fmt, 14)[0]
    if bits_per_sample < 16:
        raise ValueError(
            f"Wwise silently encodes {bits_per_sample}-bit WAV as silence. "
            "Re-export the file as 16-bit PCM and import it again."
        )
    return wav_data


def _wav_channel_count(wav_data: bytes) -> int:
    fmt = _riff_chunks(wav_data).get(b"fmt ", b"")
    if len(fmt) < 4:
        raise ValueError("WAV format does not contain a channel count.")
    channels = struct.unpack_from("<H", fmt, 2)[0]
    if channels < 1:
        raise ValueError("WAV channel count must be greater than zero.")
    return channels


def _codec_tag_from_fmt(fmt: bytes) -> int | None:
    if len(fmt) < 2:
        return None
    tag = struct.unpack_from("<H", fmt)[0]
    if (
        tag == 0xFFFE
        and len(fmt) >= 40
        and fmt[28:40] == _EXTENSIBLE_SUBFORMAT_SUFFIX
    ):
        subtype = struct.unpack_from("<I", fmt, 24)[0]
        return subtype if subtype <= 0xFFFF else tag
    return tag


def wem_codec_tag(wem_data: bytes | None) -> int | None:
    """Read the codec tag, including WAVE_FORMAT_EXTENSIBLE subtypes."""

    if not wem_data:
        return None
    try:
        return _codec_tag_from_fmt(_riff_chunks(wem_data).get(b"fmt ", b""))
    except ValueError:
        return None


def select_wwise_codec(
    profile: SoundGameProfile,
    original_wem: bytes | None = None,
    codec_tag: int | None = None,
) -> WemAuthoringCodec:
    """Use an explicit supported codec, or match the original/profile default."""

    if codec_tag is None:
        codec = (
            profile.wem_codec(wem_codec_tag(original_wem))
            or profile.default_wem_codec
        )
    else:
        codec = profile.wem_codec(int(codec_tag))
    if codec is None:
        if codec_tag is not None:
            raise ValueError(
                f"Codec tag 0x{int(codec_tag):04X} cannot be authored for "
                f"{profile.display_name}."
            )
        raise ValueError(
            f"No Wwise media codec is configured for {profile.display_name}."
        )
    return codec


def _quality_value(
    codec: WemAuthoringCodec, preset: str | None
) -> float | int | None:
    if preset is None:
        return None
    key = str(preset).strip().casefold()
    if key not in QUALITY_PRESETS:
        raise ValueError(f"Unknown Wwise quality preset: {preset}")
    if codec.quality is None:
        return None
    value = codec.quality.value(key)
    if value is None:
        raise ValueError(f"{codec.name} does not support the {key} quality preset.")
    return value


def _finite_number(value, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Wwise {label} must be a number.") from exc
    if not math.isfinite(number):
        raise ValueError(f"Wwise {label} must be finite.")
    return number


def _normalize_compression(
    codec: WemAuthoringCodec,
    settings: WemCompressionSettings | None,
) -> WemCompressionSettings | None:
    if settings is None:
        return None
    if not isinstance(settings, WemCompressionSettings):
        raise TypeError("Wwise compression settings must be WemCompressionSettings.")

    mode = str(settings.mode or COMPRESSION_MODE_QUALITY).strip().casefold()
    channel_mode = str(settings.channel_mode or CHANNEL_AS_INPUT).strip().casefold()
    if mode not in {COMPRESSION_MODE_QUALITY, COMPRESSION_MODE_BITRATE}:
        raise ValueError(f"Unknown Wwise compression mode: {settings.mode}")
    if channel_mode not in _CHANNEL_VALUES:
        raise ValueError(f"Unknown Wwise channel mode: {settings.channel_mode}")

    if mode == COMPRESSION_MODE_QUALITY:
        quality = codec.quality
        if quality is None:
            raise ValueError(f"{codec.name} has no adjustable compression quality.")
        value = _finite_number(
            quality.default if settings.quality is None else settings.quality,
            f"{codec.name} quality",
        )
        if value < quality.minimum or value > quality.maximum:
            raise ValueError(
                f"{codec.name} quality must be between "
                f"{quality.minimum:g} and {quality.maximum:g}."
            )
        if quality.property_type.casefold() == "int32":
            if not value.is_integer():
                raise ValueError(f"{codec.name} quality must be a whole number.")
            exact_quality: float | int = int(value)
        else:
            exact_quality = value
        if any(value is not None for value in (
            settings.average_bitrate,
            settings.minimum_bitrate,
            settings.maximum_bitrate,
        )):
            raise ValueError("Quality mode cannot include target bitrate values.")
        return WemCompressionSettings(
            mode=mode,
            quality=exact_quality,
            channel_mode=channel_mode,
        )

    if not codec.supports_bitrate_mode:
        raise ValueError(f"{codec.name} does not support target bitrate mode.")
    if settings.quality is not None:
        raise ValueError("Target bitrate mode cannot include an exact quality value.")
    average = _finite_number(
        64 if settings.average_bitrate is None else settings.average_bitrate,
        "average bitrate",
    )
    minimum = (
        _finite_number(settings.minimum_bitrate, "minimum bitrate")
        if settings.minimum_bitrate is not None else None
    )
    maximum = (
        _finite_number(settings.maximum_bitrate, "maximum bitrate")
        if settings.maximum_bitrate is not None else None
    )
    if any(value is not None and value <= 0 for value in (
        average, minimum, maximum,
    )):
        raise ValueError("Wwise bitrate values must be greater than zero.")
    if minimum is not None and minimum > average:
        raise ValueError("Minimum bitrate cannot exceed the average bitrate.")
    if maximum is not None and maximum < average:
        raise ValueError("Maximum bitrate cannot be below the average bitrate.")
    return WemCompressionSettings(
        mode=mode,
        average_bitrate=average,
        minimum_bitrate=minimum,
        maximum_bitrate=maximum,
        channel_mode=channel_mode,
    )


def _sample_rate_plan(
    wav_data: bytes,
    original_wem: bytes | None,
    codec: WemAuthoringCodec,
    policy: str,
) -> WemSampleRatePlan:
    if policy not in {SAMPLE_RATE_MATCH_ORIGINAL, SAMPLE_RATE_KEEP_SOURCE}:
        raise ValueError(f"Unknown Wwise sample-rate policy: {policy}")
    source_rate = int(read_riff_metadata(wav_data).sample_rate or 0)
    if source_rate <= 0:
        raise ValueError("The replacement WAV has no valid sample rate.")
    original_rate = (
        int(read_riff_metadata(original_wem).sample_rate or 0)
        if original_wem else 0
    ) or None
    forced_rate = int(codec.required_sample_rate or 0)
    target_rate = (
        forced_rate
        or (
            original_rate
            if policy == SAMPLE_RATE_MATCH_ORIGINAL and original_rate else source_rate
        )
    )
    return WemSampleRatePlan(
        source_rate, original_rate, target_rate, policy, bool(forced_rate)
    )


def plan_wwise_sample_rate(
    src_path: str | Path,
    profile: SoundGameProfile,
    original_wem: bytes | None = None,
    policy: str = SAMPLE_RATE_MATCH_ORIGINAL,
    *,
    codec_tag: int | None = None,
) -> WemSampleRatePlan:
    """Describe the rate Wwise will author before starting the converter."""

    wav_data = _read_source_wav(Path(src_path))
    codec = select_wwise_codec(profile, original_wem, codec_tag)
    return _sample_rate_plan(wav_data, original_wem, codec, policy)


def _run_wwise_cli(arguments: list[str], operation: str, *, timeout: int = 120):
    try:
        result = subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            ),
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Wwise conversion timed out.") from exc
    except OSError as exc:
        raise ValueError(f"Wwise could not be started: {exc}") from exc
    if result.returncode:
        message = (result.stderr or result.stdout or "Unknown Wwise error").strip()
        raise ValueError(
            f"Wwise {operation} failed (exit {result.returncode}): {message}"
        )
    return result


def _wwise_command(
    cli_path: Path, project_path: Path, operation: str, *, bank: str = "",
) -> list[str]:
    cli, project = str(cli_path), str(project_path)
    if cli_path.stem.casefold() == "wwiseconsole":
        command = [cli, operation, project, "--platform", "Windows"]
        return command + (["--bank", bank] if bank else [])
    if operation == "create-new-project":
        return [cli, "-CreateNewProject", project, "-Platform", "Windows"]
    if operation == "convert-external-source":
        return [cli, project, "-ConvertExternalSources", "Windows"]
    if operation == "generate-soundbank" and bank:
        return [
            cli, project, "-GenerateSoundBanks", "-Bank", bank,
            "-Platform", "Windows",
        ]
    raise ValueError(f"Unknown Wwise command: {operation}")


def _set_external_source_paths(
    project_path: Path,
    *,
    source_list: Path,
    output_dir: Path,
) -> None:
    tree = ET.parse(project_path)
    properties = tree.find("./ProjectInfo/Project/PropertyList")
    if properties is None:
        raise ValueError("The temporary Wwise project has no project PropertyList.")

    values = {
        "ExternalSourcesInputPath": str(source_list),
        "ExternalSourcesOutputPath": str(output_dir) + "\\",
    }
    for property_name, value in values.items():
        target = properties.find(
            f"./Property[@Name='{property_name}']/ValueList/Value[@Platform='Windows']"
        )
        if target is None:
            raise ValueError(
                f"The temporary Wwise project has no Windows {property_name} setting."
            )
        target.text = value
    tree.write(project_path, encoding="utf-8", xml_declaration=True)


_FACTORY_PLUGIN_BY_TAG = {
    0xFFFE: ("PCM", 1, ()),
    0x0002: ("ADPCM", 2, ()),
    0x8311: ("ADPCM", 2, ()),
    0xFFFF: ("Vorbis", 4, ()),
}


def _xml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _add_custom_conversion(
    project_dir: Path,
    codec: WemAuthoringCodec,
    sample_rate: int | None = None,
    quality_preset: str | None = None,
    compression: WemCompressionSettings | None = None,
) -> str:
    """Add a conversion preset when factory settings cannot express the plan."""

    quality_value = (
        compression.quality
        if compression and compression.mode == COMPRESSION_MODE_QUALITY
        else _quality_value(codec, quality_preset)
    )
    plugin_spec = codec.conversion_plugin
    if (
        plugin_spec is None
        and sample_rate is None
        and quality_value is None
        and compression is None
    ):
        return codec.conversion_setting
    plugin_spec = plugin_spec or _FACTORY_PLUGIN_BY_TAG.get(codec.tag)
    if plugin_spec is None:
        raise ValueError(f"No Wwise conversion plug-in is known for {codec.name}.")
    plugin_name, plugin_id, plugin_properties = plugin_spec
    path = project_dir / "Conversion Settings" / "Default Work Unit.wwu"
    tree = ET.parse(path)
    children = tree.find("./Conversions/WorkUnit/ChildrenList")
    if children is None:
        raise ValueError("The temporary Wwise project has no conversion work unit.")

    guid = lambda: "{" + str(uuid.uuid4()).upper() + "}"
    if (
        codec.conversion_plugin is not None
        and quality_preset is None
        and compression is None
    ):
        setting_name = codec.conversion_setting
    else:
        details = []
        if sample_rate:
            details.append(f"{sample_rate} Hz")
        if quality_preset:
            details.append(f"{quality_preset.title()} quality")
        elif compression and compression.mode == COMPRESSION_MODE_QUALITY:
            details.append(f"quality {compression.quality:g}")
        elif compression and compression.mode == COMPRESSION_MODE_BITRATE:
            details.append(f"{compression.average_bitrate:g} kbps per channel")
            if compression.minimum_bitrate is not None:
                details.append(f"min {compression.minimum_bitrate:g}")
            if compression.maximum_bitrate is not None:
                details.append(f"max {compression.maximum_bitrate:g}")
        if compression and compression.channel_mode != CHANNEL_AS_INPUT:
            details.append(compression.channel_mode.title())
        setting_name = " ".join(("REasy", codec.name, *details))
    conversion = ET.SubElement(children, "Conversion", Name=setting_name, ID=guid())
    properties = ET.SubElement(conversion, "PropertyList")
    channel_value = _CHANNEL_VALUES[
        compression.channel_mode if compression else CHANNEL_AS_INPUT
    ]
    for name, kind, value in (
        ("Channels", "int32", channel_value), ("LRMix", "Real64", 0),
        ("MaxSampleRate", "int32", 0), ("MinSampleRate", "int32", 0),
        ("SampleRate", "int32", sample_rate or 0),
        ("SRConversionQuality", "int32", 1 if sample_rate else 0),
    ):
        prop = ET.SubElement(properties, "Property", Name=name, Type=kind)
        values = ET.SubElement(prop, "ValueList")
        ET.SubElement(values, "Value", Platform="Windows").text = str(value)
    infos = ET.SubElement(conversion, "ConversionPluginInfoList")
    info = ET.SubElement(infos, "ConversionPluginInfo", Platform="Windows")
    plugin = ET.SubElement(
        info, "ConversionPlugin", Name="", ID=guid(), PluginName=plugin_name,
        CompanyID="0", PluginID=str(plugin_id),
    )
    plugin_properties = {
        name: (kind, value) for name, kind, value in plugin_properties
    }

    if quality_value is not None:
        quality = codec.quality
        if quality is None:
            raise ValueError(f"{codec.name} has no adjustable compression quality.")
        plugin_properties[quality.property_name] = (
            quality.property_type, quality_value
        )
    if compression and codec.supports_bitrate_mode:
        bitrate_mode = compression.mode == COMPRESSION_MODE_BITRATE
        plugin_properties["BitrateManagmentMode"] = (
            "int32", int(bitrate_mode)
        )
        if bitrate_mode:
            plugin_properties["AverageBitrate"] = (
                "Real32", compression.average_bitrate
            )
            for bound, value in (
                ("Minimum", compression.minimum_bitrate),
                ("Maximum", compression.maximum_bitrate),
            ):
                plugin_properties[f"Enable{bound}Bitrate"] = (
                    "bool", value is not None
                )
                if value is not None:
                    plugin_properties[f"{bound}Bitrate"] = ("Real32", value)
    if plugin_properties:
        values = ET.SubElement(plugin, "PropertyList")
        for name, (kind, value) in plugin_properties.items():
            ET.SubElement(
                values, "Property", Name=name, Type=kind,
                Value=_xml_scalar(value),
            )
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return setting_name


def _project_template_key(installation: WwiseInstallation) -> str:
    location = str(installation.root.resolve()).casefold().encode("utf-8")
    digest = hashlib.sha256(location).hexdigest()[:12]
    return (
        f"v{_PROJECT_CACHE_VERSION}_"
        f"{installation.version.text.replace('.', '_')}_{digest}"
    )


def _valid_project_dir(project_dir: Path) -> bool:
    return (
        (project_dir / "REasyWem.wproj").is_file()
        and (
            project_dir / "Conversion Settings" / "Default Work Unit.wwu"
        ).is_file()
    )


def _create_project(project_dir: Path, installation: WwiseInstallation) -> None:
    project_path = project_dir / "REasyWem.wproj"
    _run_wwise_cli(
        _wwise_command(
            installation.cli_path, project_path, "create-new-project"
        ),
        "project creation",
    )
    if not _valid_project_dir(project_dir):
        raise ValueError("Wwise project creation did not produce a complete project.")


def prepare_wwise_project(installation: WwiseInstallation) -> Path:
    """Create a persistent pristine project once for this exact Wwise install."""

    root = cache_directory() / "wwise_projects"
    template = root / _project_template_key(installation)
    if _valid_project_dir(template):
        return template
    with _PROJECT_TEMPLATE_LOCK:
        if _valid_project_dir(template):
            return template
        root.mkdir(parents=True, exist_ok=True)
        staging_root = root / f".{template.name}_{uuid.uuid4().hex}"
        staging = staging_root / "REasyWem"
        try:
            _create_project(staging, installation)
            # Another REasy process may have populated the same cache while
            # this Wwise invocation was running. Keep the first complete
            # project instead of deleting and replacing it underneath a copy.
            if _valid_project_dir(template):
                return template
            if template.exists():
                shutil.rmtree(template)
            try:
                staging.replace(template)
            except OSError:
                if not _valid_project_dir(template):
                    raise
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)
    return template


def _copy_wwise_project(
    project_dir: Path, installation: WwiseInstallation
) -> None:
    """Copy the cached project, falling back to direct creation if cache is unwritable."""

    try:
        template = prepare_wwise_project(installation)
        shutil.copytree(template, project_dir)
    except OSError:
        shutil.rmtree(project_dir, ignore_errors=True)
        _create_project(project_dir, installation)


def _validate_game_wem(
    wem_data: bytes,
    profile: SoundGameProfile,
    expected_codec: WemAuthoringCodec,
    expected_sample_rate: int | None = None,
    expected_channels: int | None = None,
) -> int:
    chunks = _riff_chunks(wem_data)
    fmt = chunks.get(b"fmt ", b"")
    if len(fmt) < 2 or not chunks.get(b"data"):
        raise ValueError("Wwise produced an incomplete WEM file.")
    codec_tag = _codec_tag_from_fmt(fmt)
    if codec_tag != expected_codec.tag:
        raise ValueError(
            f"Wwise produced codec tag "
            f"{f'0x{codec_tag:04X}' if codec_tag is not None else 'Unknown'}; the selected "
            f"{expected_codec.name} setting requires 0x{expected_codec.tag:04X}."
        )
    sample_rate = struct.unpack_from("<I", fmt, 4)[0] if len(fmt) >= 8 else 0
    if expected_sample_rate and sample_rate != expected_sample_rate:
        raise ValueError(
            f"Wwise produced {sample_rate} Hz; the replacement plan requires "
            f"{expected_sample_rate} Hz."
        )
    channels = struct.unpack_from("<H", fmt, 2)[0] if len(fmt) >= 4 else 0
    if expected_channels and channels != expected_channels:
        raise ValueError(
            f"Wwise produced {channels} channel(s); the advanced compression "
            f"plan requires {expected_channels}."
        )
    return sample_rate


def _expected_output_channels(item: _PreparedConversion) -> int | None:
    if item.compression is None:
        return None
    source_channels = _wav_channel_count(item.wav_data)
    if item.compression.channel_mode == CHANNEL_MONO:
        return 1
    if item.compression.channel_mode == CHANNEL_STEREO:
        # Wwise does not upmix mono unless Allow Channel Upmix is explicitly set.
        return min(source_channels, 2)
    return source_channels


def _create_source_list(
    source_list: Path,
    *,
    sources: tuple[tuple[Path, str], ...],
    root: Path,
) -> None:
    entries = "".join(
        f'  <Source Path="{escape(source_wav.name, quote=True)}" '
        f'Conversion="{escape(conversion_setting, quote=True)}" />\n'
        for source_wav, conversion_setting in sources
    )
    source_list.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<ExternalSourcesList SchemaVersion="1" Root="{escape(str(root), quote=True)}">\n'
        f"{entries}"
        "</ExternalSourcesList>\n",
        encoding="utf-8",
    )


def _prepare_conversion(
    request: WemConversionRequest, profile: SoundGameProfile
) -> _PreparedConversion:
    reference_wem = (
        request.match_codec_from
        if request.match_codec_from is not None
        else request.preserve_metadata_from
    )
    codec = select_wwise_codec(profile, reference_wem, request.codec_tag)
    quality_preset = (
        str(request.quality_preset).strip().casefold()
        if request.quality_preset is not None else None
    )
    _quality_value(codec, quality_preset)
    compression = _normalize_compression(codec, request.compression)
    if compression is not None and quality_preset is not None:
        raise ValueError(
            "Choose either a quality preset or advanced compression settings."
        )
    wav_data = _read_source_wav(Path(request.source_path))
    rate_plan = _sample_rate_plan(
        wav_data, reference_wem, codec, request.sample_rate_policy
    )
    source_metadata = read_riff_metadata(wav_data)
    preserved_metadata = None
    expected_metadata = None
    if request.preserve_metadata_from:
        preserved_metadata = read_riff_metadata(request.preserve_metadata_from)
        wav_data, expected_metadata = inherit_riff_metadata(
            wav_data, request.preserve_metadata_from
        )
    return _PreparedConversion(
        wav_data,
        codec,
        rate_plan,
        quality_preset,
        compression,
        source_metadata,
        preserved_metadata,
        expected_metadata,
    )


def _restore_authored_metadata(
    wem_data: bytes, prepared: _PreparedConversion, authored_rate: int
) -> bytes:
    if prepared.expected_metadata is None:
        return wem_data
    authored_source = rescale_riff_metadata(
        prepared.source_metadata, authored_rate
    )
    authored_preserved = rescale_riff_metadata(
        prepared.preserved_metadata, authored_rate
    )
    expected = RiffMetadata(
        authored_rate,
        authored_source.sample_count,
        authored_source.loops or authored_preserved.loops,
        authored_source.markers or authored_preserved.markers,
    )
    actual = read_riff_metadata(wem_data)
    if actual.loops != expected.loops or actual.markers != expected.markers:
        # Legacy External Sources conversion may drop smpl/cue/adtl. Reattach
        # them while preserving every generated codec chunk and audio payload.
        wem_data = write_riff_metadata(wem_data, expected)
        actual = read_riff_metadata(wem_data)
        if actual.loops != expected.loops or actual.markers != expected.markers:
            raise ValueError("The encoded WEM could not retain loop/cue metadata.")
    return wem_data


def convert_files_to_wwise_wem(
    requests: Iterable[WemConversionRequest],
    *,
    game: str,
    installation: WwiseInstallation | str | Path,
) -> tuple[bytes, ...]:
    """Author multiple WAVs in one project and one Wwise conversion process."""

    requests = tuple(requests)
    if not requests:
        return ()
    if not all(isinstance(item, WemConversionRequest) for item in requests):
        raise TypeError("Wwise batch requests must be WemConversionRequest values.")
    profile = require_wwise_profile(game)
    if not isinstance(installation, WwiseInstallation):
        installation = validate_wwise_installation(installation, game)
    elif installation.profile.game != profile.game:
        installation = validate_wwise_installation(installation.root, game)
    prepared = tuple(_prepare_conversion(item, profile) for item in requests)

    with tempfile.TemporaryDirectory(prefix="reasy_wwise_") as temp_name:
        temp_dir = Path(temp_name)
        project_path = temp_dir / "REasyWem" / "REasyWem.wproj"
        source_list = temp_dir / "REasyExternalSources.wsources"
        output_dir = temp_dir / "output"
        _copy_wwise_project(project_path.parent, installation)

        settings = {}
        sources = []
        source_paths = []
        for index, item in enumerate(prepared):
            source_wav = temp_dir / f"reasy_source_{index:06d}.wav"
            source_wav.write_bytes(item.wav_data)
            conversion_rate = (
                item.rate_plan.target_rate
                if item.rate_plan.resamples
                or item.codec.conversion_plugin is not None
                else None
            )
            key = (
                item.codec.tag,
                conversion_rate,
                item.quality_preset,
                item.compression,
            )
            if key not in settings:
                settings[key] = _add_custom_conversion(
                    project_path.parent,
                    item.codec,
                    conversion_rate,
                    item.quality_preset,
                    item.compression,
                )
            sources.append((source_wav, settings[key]))
            source_paths.append(source_wav)
        _create_source_list(
            source_list,
            sources=tuple(sources),
            root=temp_dir,
        )
        _set_external_source_paths(
            project_path,
            source_list=source_list,
            output_dir=output_dir,
        )
        _run_wwise_cli(
            _wwise_command(
                installation.cli_path, project_path, "convert-external-source"
            ),
            "external-source conversion",
            timeout=max(120, 30 * len(prepared)),
        )

        results = []
        for source_wav, item in zip(source_paths, prepared):
            output_wem = output_dir / f"{source_wav.stem}.wem"
            if not output_wem.is_file():
                raise ValueError(
                    f"Wwise conversion did not produce {output_wem.name}."
                )
            wem_data = output_wem.read_bytes()
            authored_rate = _validate_game_wem(
                wem_data,
                installation.profile,
                item.codec,
                item.rate_plan.target_rate,
                _expected_output_channels(item),
            )
            results.append(
                _restore_authored_metadata(wem_data, item, authored_rate)
            )
        return tuple(results)


def convert_file_to_wwise_wem(
    src_path: str | Path,
    *,
    game: str,
    installation: WwiseInstallation | str | Path,
    preserve_metadata_from: bytes | None = None,
    match_codec_from: bytes | None = None,
    sample_rate_policy: str = SAMPLE_RATE_MATCH_ORIGINAL,
    codec_tag: int | None = None,
    quality_preset: str | None = None,
    compression: WemCompressionSettings | None = None,
) -> bytes:
    """Encode one WAV with explicit or original-matched authoring settings."""

    request = WemConversionRequest(
        src_path,
        preserve_metadata_from=preserve_metadata_from,
        match_codec_from=match_codec_from,
        sample_rate_policy=sample_rate_policy,
        codec_tag=codec_tag,
        quality_preset=quality_preset,
        compression=compression,
    )
    return convert_files_to_wwise_wem(
        (request,), game=game, installation=installation
    )[0]


__all__ = [
    "CHANNEL_AS_INPUT",
    "CHANNEL_MONO",
    "CHANNEL_STEREO",
    "COMPRESSION_MODE_BITRATE",
    "COMPRESSION_MODE_QUALITY",
    "QUALITY_PRESETS",
    "SAMPLE_RATE_KEEP_SOURCE",
    "SAMPLE_RATE_MATCH_ORIGINAL",
    "WemConversionRequest",
    "WemCompressionSettings",
    "WemSampleRatePlan",
    "convert_file_to_wwise_wem",
    "convert_files_to_wwise_wem",
    "plan_wwise_sample_rate",
    "prepare_wwise_project",
    "select_wwise_codec",
    "wem_codec_tag",
]
