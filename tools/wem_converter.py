"""Author WAV files as game-profiled Wwise media."""

from __future__ import annotations

import os
import struct
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
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


_EXTENSIBLE_SUBFORMAT_SUFFIX = bytes.fromhex("00001000800000aa00389b71")
SAMPLE_RATE_MATCH_ORIGINAL = "match_original"
SAMPLE_RATE_KEEP_SOURCE = "keep_source"


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
    if len(chunks.get(b"fmt ", b"")) < 16 or not chunks.get(b"data"):
        raise ValueError("WAV must contain valid, non-empty fmt and data chunks.")
    return wav_data


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
) -> WemAuthoringCodec:
    """Match an authorable original codec, otherwise use the game default."""

    codec = profile.wem_codec(wem_codec_tag(original_wem)) or profile.default_wem_codec
    if codec is None:
        raise ValueError(f"No Wwise media codec is configured for {profile.display_name}.")
    return codec


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
) -> WemSampleRatePlan:
    """Describe the rate Wwise will author before starting the converter."""

    wav_data = _read_source_wav(Path(src_path))
    codec = select_wwise_codec(profile, original_wem)
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


def _add_custom_conversion(
    project_dir: Path,
    codec: WemAuthoringCodec,
    sample_rate: int | None = None,
) -> str:
    """Add a codec/rate preset when a factory preset cannot express the plan."""

    plugin_spec = codec.conversion_plugin
    if plugin_spec is None and sample_rate is None:
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
    setting_name = (
        codec.conversion_setting
        if codec.conversion_plugin is not None
        else f"REasy {codec.name} {sample_rate} Hz"
    )
    conversion = ET.SubElement(children, "Conversion", Name=setting_name, ID=guid())
    properties = ET.SubElement(conversion, "PropertyList")
    for name, kind, value in (
        ("Channels", "int32", 4), ("LRMix", "Real64", 0),
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
    if plugin_properties:
        values = ET.SubElement(plugin, "PropertyList")
        for name, kind, value in plugin_properties:
            ET.SubElement(
                values, "Property", Name=name, Type=kind, Value=str(value)
            )
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return setting_name


def _validate_game_wem(
    wem_data: bytes,
    profile: SoundGameProfile,
    expected_codec: WemAuthoringCodec,
    expected_sample_rate: int | None = None,
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
    return sample_rate


def _create_source_list(
    source_list: Path,
    *,
    source_wav: Path,
    root: Path,
    conversion_setting: str,
) -> None:
    source_list.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<ExternalSourcesList SchemaVersion="1" Root="{escape(str(root), quote=True)}">\n'
        f'  <Source Path="{escape(source_wav.name, quote=True)}" '
        f'Conversion="{escape(conversion_setting, quote=True)}" />\n'
        "</ExternalSourcesList>\n",
        encoding="utf-8",
    )


def convert_file_to_wwise_wem(
    src_path: str | Path,
    *,
    game: str,
    installation: WwiseInstallation | str | Path,
    preserve_metadata_from: bytes | None = None,
    match_codec_from: bytes | None = None,
    sample_rate_policy: str = SAMPLE_RATE_MATCH_ORIGINAL,
) -> bytes:
    """Encode a WAV, matching an authorable original codec when available."""

    profile = require_wwise_profile(game)
    if not isinstance(installation, WwiseInstallation):
        installation = validate_wwise_installation(installation, game)
    elif installation.profile.game != profile.game:
        installation = validate_wwise_installation(installation.root, game)

    reference_wem = (
        match_codec_from if match_codec_from is not None else preserve_metadata_from
    )
    codec = select_wwise_codec(profile, reference_wem)
    wav_data = _read_source_wav(Path(src_path))
    rate_plan = _sample_rate_plan(
        wav_data, reference_wem, codec, sample_rate_policy
    )
    source_metadata = read_riff_metadata(wav_data)
    preserved_metadata = None
    expected_metadata = None
    if preserve_metadata_from:
        preserved_metadata = read_riff_metadata(preserve_metadata_from)
        wav_data, expected_metadata = inherit_riff_metadata(
            wav_data, preserve_metadata_from
        )
    with tempfile.TemporaryDirectory(prefix="reasy_wwise_") as temp_name:
        temp_dir = Path(temp_name)
        project_path = temp_dir / "REasyWem" / "REasyWem.wproj"
        source_wav = temp_dir / "reasy_source.wav"
        source_list = temp_dir / "REasyExternalSources.wsources"
        output_dir = temp_dir / "output"
        source_wav.write_bytes(wav_data)

        _run_wwise_cli(
            _wwise_command(
                installation.cli_path, project_path, "create-new-project"
            ),
            "project creation",
        )
        if not project_path.is_file():
            raise ValueError("Wwise project creation did not produce a project file.")

        conversion_rate = (
            rate_plan.target_rate
            if rate_plan.resamples or codec.conversion_plugin is not None else None
        )
        conversion_setting = _add_custom_conversion(
            project_path.parent, codec, conversion_rate
        )

        _create_source_list(
            source_list,
            source_wav=source_wav,
            root=temp_dir,
            conversion_setting=conversion_setting,
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
        )

        output_wem = output_dir / f"{source_wav.stem}.wem"
        if not output_wem.is_file():
            raise ValueError("Wwise conversion did not produce a WEM file.")
        wem_data = output_wem.read_bytes()
        authored_rate = _validate_game_wem(
            wem_data, installation.profile, codec, rate_plan.target_rate
        )
        if expected_metadata is not None:
            authored_source = rescale_riff_metadata(
                source_metadata, authored_rate
            )
            authored_preserved = rescale_riff_metadata(
                preserved_metadata, authored_rate
            )
            expected_metadata = RiffMetadata(
                authored_rate,
                authored_source.sample_count,
                authored_source.loops or authored_preserved.loops,
                authored_source.markers or authored_preserved.markers,
            )
            actual = read_riff_metadata(wem_data)
            if (
                actual.loops != expected_metadata.loops
                or actual.markers != expected_metadata.markers
            ):
                # Legacy External Sources conversion may drop smpl/cue/adtl.
                # Reattach them while preserving every generated codec chunk
                # and the new audio payload.
                wem_data = write_riff_metadata(wem_data, expected_metadata)
                actual = read_riff_metadata(wem_data)
                if (
                    actual.loops != expected_metadata.loops
                    or actual.markers != expected_metadata.markers
                ):
                    raise ValueError(
                        "The encoded WEM could not retain loop/cue metadata."
                    )
        return wem_data


__all__ = [
    "SAMPLE_RATE_KEEP_SOURCE",
    "SAMPLE_RATE_MATCH_ORIGINAL",
    "WemSampleRatePlan",
    "convert_file_to_wwise_wem",
    "plan_wwise_sample_rate",
    "select_wwise_codec",
    "wem_codec_tag",
]
