"""Author Wwise plug-in impulse responses with the game's Wwise build."""

from __future__ import annotations

import math
import shutil
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from file_handlers.sound.bnk_parser import extract_embedded_wem, parse_soundbank
from file_handlers.sound.wwise_media import (
    WwiseMediaKind,
    parse_convolution_reverb_media,
    parse_hybrid_reverb_media,
)
from tools.wem_converter import (
    _codec_tag_from_fmt, _read_source_wav, _riff_chunks, _run_wwise_cli,
    _wwise_command,
)
from tools.wwise_toolchain import (
    WwiseInstallation,
    require_wwise_profile,
    validate_wwise_installation,
)


_DEFAULT_TUNING = (0.5, 400.0, 10_000.0, 1.0, 1.0, 1.0)
_PROPERTY_NAMES = (
    "DecayTime",
    "DecayLowFrequency",
    "DecayHighFrequency",
    "DecayLowRatio",
    "DecayMidRatio",
    "DecayHighRatio",
)


def _guid() -> str:
    return "{" + str(uuid.uuid4()).upper() + "}"


def _validate_ir_wav(path: Path) -> None:
    chunks = _riff_chunks(_read_source_wav(path))
    fmt = chunks[b"fmt "]
    if len(fmt) < 16:
        raise ValueError("Impulse-response WAV has an incomplete format chunk.")
    channels = int.from_bytes(fmt[2:4], "little")
    bits = int.from_bytes(fmt[14:16], "little")
    if _codec_tag_from_fmt(fmt) != 1 or channels not in (1, 2) or bits not in (16, 24):
        raise ValueError(
            "iZotope Hybrid Reverb accepts non-empty PCM WAV input with one or "
            "two channels and 16- or 24-bit samples."
        )


def _plugin_is_installed(
    root: Path,
    names=("iZHybridReverb_Plugin.xml", "iZHybridReverb.xml"),
) -> bool:
    return any(next(root.rglob(name), None) is not None for name in names)


def _validate_convolution_ir_wav(path: Path) -> None:
    chunks = _riff_chunks(_read_source_wav(path))
    fmt = chunks[b"fmt "]
    channels = int.from_bytes(fmt[2:4], "little")
    bits = int.from_bytes(fmt[14:16], "little")
    if _codec_tag_from_fmt(fmt) != 1 or not 1 <= channels <= 32 or bits not in (16, 24):
        raise ValueError(
            "Wwise Convolution Reverb accepts non-empty PCM WAV input with "
            "16- or 24-bit samples."
        )


def _write_plugin_work_units(
    project: Path,
    source_id: int,
    *,
    plugin_name: str,
    company_id: int,
    plugin_id: int,
    parent_plugin_id: int,
    properties=(),
) -> None:
    effects_path = project / "Effects" / "Default Work Unit.wwu"
    banks_path = project / "SoundBanks" / "Default Work Unit.wwu"
    schema = ET.parse(effects_path).getroot().get("SchemaVersion", "80")
    effects_id, effect_id, media_id = _guid(), _guid(), _guid()

    root = ET.Element("WwiseDocument", Type="WorkUnit", ID=effects_id, SchemaVersion=schema)
    effects = ET.SubElement(root, "Effects")
    unit = ET.SubElement(effects, "WorkUnit", Name="Default Work Unit", ID=effects_id,
                         PersistMode="Standalone")
    children = ET.SubElement(unit, "ChildrenList")
    effect = ET.SubElement(
        children, "Effect", Name="REasy_IR", ID=effect_id,
        PluginName=plugin_name, CompanyID=str(company_id), PluginID=str(plugin_id),
        PluginType="3",
    )
    property_list = ET.SubElement(effect, "PropertyList")
    for name, value in properties:
        ET.SubElement(
            property_list, "Property", Name=name, Type="Real32", Value=repr(value)
        )
    media_children = ET.SubElement(effect, "ChildrenList")
    media = ET.SubElement(
        media_children, "PluginMediaSource", Name="REasy_IR", ID=media_id,
        ShortID=str(source_id),
    )
    ET.SubElement(media, "ParentPluginID").text = str(parent_plugin_id)
    media_properties = ET.SubElement(media, "PropertyList")
    ET.SubElement(
        media_properties, "Property", Name="DataFileName", Type="string",
        Value="REasy_IR.wav",
    )
    ET.ElementTree(root).write(effects_path, encoding="utf-8", xml_declaration=True)

    bank_unit_id, bank_id = _guid(), _guid()
    root = ET.Element("WwiseDocument", Type="WorkUnit", ID=bank_unit_id,
                      SchemaVersion=schema)
    banks = ET.SubElement(root, "SoundBanks")
    unit = ET.SubElement(banks, "WorkUnit", Name="Default Work Unit", ID=bank_unit_id,
                         PersistMode="Standalone")
    children = ET.SubElement(unit, "ChildrenList")
    bank = ET.SubElement(children, "SoundBank", Name="REasyIR", ID=bank_id)
    includes = ET.SubElement(bank, "ObjectInclusionList")
    ET.SubElement(
        includes, "ObjectRef", Name="REasy_IR", ID=effect_id,
        WorkUnitID=effects_id, Origin="Manual", Filter="7",
    )
    ET.SubElement(bank, "ObjectExclusionList")
    ET.SubElement(bank, "GameSyncExclusionList")
    ET.ElementTree(root).write(banks_path, encoding="utf-8", xml_declaration=True)


def _write_work_units(project: Path, tuning: tuple[float, ...], source_id: int) -> None:
    """Compatibility wrapper for the iZotope authoring tests and workflow."""

    _write_plugin_work_units(
        project,
        source_id,
        plugin_name="iZotope Hybrid Reverb",
        company_id=259,
        plugin_id=2,
        parent_plugin_id=0x00021033,
        properties=zip(_PROPERTY_NAMES, tuning),
    )


def _write_convolution_work_units(project: Path, source_id: int) -> None:
    _write_plugin_work_units(
        project,
        source_id,
        plugin_name="Wwise Convolution Reverb",
        company_id=0,
        plugin_id=127,
        parent_plugin_id=0x007F0003,
    )


def _installation_for_plugin(game, installation, capability: str, label: str):
    profile = require_wwise_profile(game)
    if not getattr(profile, capability):
        raise ValueError(f"{label} media is not enabled for {profile.display_name}.")
    if not isinstance(installation, WwiseInstallation):
        installation = validate_wwise_installation(installation, game)
    elif installation.profile.game != profile.game:
        installation = validate_wwise_installation(installation.root, game)
    return profile, installation


def _author_plugin_ir(
    source: Path,
    installation: WwiseInstallation,
    *,
    label: str,
    plugin_files: tuple[str, ...],
    originals_folder: str,
    media_kind: WwiseMediaKind,
    validate_source,
    write_units,
) -> bytes:
    if not _plugin_is_installed(installation.root, plugin_files):
        raise ValueError(
            f"The selected Wwise {installation.profile.required_version_text} "
            f"installation does not include the {label} authoring plug-in. Add "
            "it to this Wwise version in Audiokinetic Launcher, then retry."
        )
    validate_source(source)
    source_id = 0x52534159  # Stable ID used only inside the temporary project.
    with tempfile.TemporaryDirectory(prefix="reasy_wwise_ir_") as temp_name:
        project_path = Path(temp_name) / "REasyIR" / "REasyIR.wproj"
        _run_wwise_cli(
            _wwise_command(
                installation.cli_path, project_path, "create-new-project"
            ),
            "project creation",
        )
        if not project_path.is_file():
            raise ValueError("Wwise project creation did not produce a project file.")
        originals = project_path.parent / "Originals" / "Plugins" / originals_folder
        originals.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, originals / "REasy_IR.wav")
        write_units(project_path.parent, source_id)
        try:
            generated = _run_wwise_cli(
                _wwise_command(
                    installation.cli_path, project_path,
                    "generate-soundbank", bank="REasyIR",
                ),
                f"{label} authoring",
                timeout=180,
            )
        except ValueError as exc:
            if "license" not in str(exc).casefold():
                raise
            raise ValueError(
                f"Wwise reports that no {label} license is active. Activate that "
                "plug-in for the game's required Wwise version, then retry."
            ) from exc
        bank_path = project_path.parent / "GeneratedSoundBanks" / "Windows" / "REasyIR.bnk"
        logs = (generated.stdout or "") + "\n" + (generated.stderr or "")
        if not bank_path.is_file():
            raise ValueError(f"Wwise did not produce the {label} SoundBank. {logs.strip()}")
        bank_data = bank_path.read_bytes()
        result = parse_soundbank(bank_data)
        track = next(
            (item for item in result.tracks
             if item.available and item.media_kind == media_kind),
            None,
        )
        if track is None:
            license_note = (
                "Wwise reports that no plug-in license is available. "
                if "license" in logs.casefold() else ""
            )
            raise ValueError(
                license_note + f"{label} did not author any impulse media. The "
                f"matching Wwise installation must include an activated {label} license."
            )
        return extract_embedded_wem(bank_data, track)


def convert_file_to_hybrid_reverb_ir(
    src_path: str | Path,
    *,
    game: str,
    installation: WwiseInstallation | str | Path,
    preserve_tuning_from: bytes | None = None,
) -> bytes:
    """Compile a WAV as Hybrid Reverb media while retaining game tuning."""

    source = Path(src_path)
    _, installation = _installation_for_plugin(
        game, installation, "supports_hybrid_reverb_ir", "Hybrid Reverb"
    )
    tuning = (
        parse_hybrid_reverb_media(preserve_tuning_from).tuning
        if preserve_tuning_from else _DEFAULT_TUNING
    )
    authored = _author_plugin_ir(
        source,
        installation,
        label="iZotope Hybrid Reverb",
        plugin_files=("iZHybridReverb_Plugin.xml", "iZHybridReverb.xml"),
        originals_folder="iZotope Hybrid Reverb",
        media_kind=WwiseMediaKind.HYBRID_REVERB_IR,
        validate_source=_validate_ir_wav,
        write_units=lambda project, source_id: _write_work_units(
            project, tuning, source_id
        ),
    )
    actual = parse_hybrid_reverb_media(authored)
    if any(not math.isclose(a, b, abs_tol=1e-5) for a, b in zip(actual.tuning, tuning)):
        raise ValueError("Wwise changed the original Hybrid Reverb tuning unexpectedly.")
    return authored


def convert_file_to_convolution_reverb_ir(
    src_path: str | Path,
    *,
    game: str,
    installation: WwiseInstallation | str | Path,
) -> bytes:
    """Compile a WAV as Wwise Convolution Reverb media for a profiled game."""

    source = Path(src_path)
    _, installation = _installation_for_plugin(
        game, installation, "supports_convolution_reverb_ir", "Convolution Reverb"
    )
    authored = _author_plugin_ir(
        source,
        installation,
        label="Wwise Convolution Reverb",
        plugin_files=("AkConvolutionReverb.xml", "ConvolutionReverb.xml"),
        originals_folder="Wwise Convolution Reverb",
        media_kind=WwiseMediaKind.CONVOLUTION_REVERB_IR,
        validate_source=_validate_convolution_ir_wav,
        write_units=_write_convolution_work_units,
    )
    parse_convolution_reverb_media(authored)
    return authored


__all__ = [
    "convert_file_to_convolution_reverb_ir",
    "convert_file_to_hybrid_reverb_ir",
]
