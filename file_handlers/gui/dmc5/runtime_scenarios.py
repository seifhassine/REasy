"""Validated runtime-preview states captured from an exact DMC5 build."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache

from utils.app_paths import resource_path

from ..scene import normalize_gui_resource_path


FrozenFields = tuple[tuple[str, object], ...]
FrozenStateMap = tuple[tuple[str, FrozenFields], ...]
_NONFINITE_SENTINELS = frozenset({"nan", "inf", "-inf"})


@dataclass(frozen=True, slots=True)
class Dmc5CapturedPreview:
    """Snapshots (captured data) I generated using REF. Unimportant."""

    key: str
    label: str
    description: str
    owner_name: str | None
    controller_type: str | None
    runtime_types: tuple[tuple[str, str], ...]
    properties: FrozenStateMap
    active: tuple[tuple[str, bool], ...]
    playback: FrozenStateMap
    capture: str
    capture_sha256: str
    controller_capture: str | None
    controller_capture_sha256: str | None
    matched_objects: int
    sampled_objects: int
    scene_objects: int


def _text(value: object, field: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _count(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _hash(value: object, field: str) -> str:
    text = _text(value, field)
    assert text is not None
    invalid = any(
        character not in "0123456789abcdefABCDEF"
        for character in text
    )
    if len(text) != 64 or invalid:
        raise ValueError(f"{field} must be a SHA-256 digest")
    return text.casefold()


def _freeze_value(value: object, field: str) -> object:
    if isinstance(value, str) and value.casefold() in _NONFINITE_SENTINELS:
        raise ValueError(f"{field} contains a non-finite probe sentinel")
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, list):
        return tuple(
            _freeze_value(item, f"{field}[{index}]")
            for index, item in enumerate(value)
        )
    raise ValueError(f"{field} has an unsupported runtime value")


def _state_map(value: object, field: str) -> FrozenStateMap:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    result: list[tuple[str, FrozenFields]] = []
    for path, raw_fields in value.items():
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError(f"{field} has an invalid GUI path")
        if not isinstance(raw_fields, dict):
            raise ValueError(f"{field}.{path} must be an object")
        fields: list[tuple[str, object]] = []
        for name, raw_value in raw_fields.items():
            if not isinstance(name, str) or not name:
                raise ValueError(f"{field}.{path} has an invalid field name")
            fields.append(
                (name, _freeze_value(raw_value, f"{field}.{path}.{name}"))
            )
        result.append((path, tuple(fields)))
    return tuple(result)


def _active_map(value: object) -> tuple[tuple[str, bool], ...]:
    if not isinstance(value, dict):
        raise ValueError("active must be an object")
    result: list[tuple[str, bool]] = []
    for path, active in value.items():
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("active has an invalid GUI path")
        if type(active) is not bool:
            raise ValueError(f"active.{path} must be Boolean")
        result.append((path, active))
    return tuple(result)


def _type_map(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict):
        raise ValueError("runtime_types must be an object")
    result: list[tuple[str, str]] = []
    for path, type_name in value.items():
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("runtime_types has an invalid GUI path")
        name = _text(type_name, f"runtime_types.{path}")
        assert name is not None
        result.append((path, name))
    return tuple(result)


def _record(value: object, asset: str, index: int) -> Dmc5CapturedPreview:
    if not isinstance(value, dict):
        raise ValueError(f"{asset} scenario {index} must be an object")
    evidence = value.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError(f"{asset} scenario {index} has no evidence")
    matched = _count(
        evidence.get("matched_objects"),
        "evidence.matched_objects",
    )
    sampled = _count(
        evidence.get("sampled_objects"),
        "evidence.sampled_objects",
    )
    scene = _count(
        evidence.get("scene_objects"),
        "evidence.scene_objects",
    )
    if matched > sampled or matched > scene:
        raise ValueError(f"{asset} scenario {index} has inconsistent object counts")
    controller_evidence = evidence.get("controller_identity")
    controller_capture = None
    controller_capture_sha256 = None
    if controller_evidence is not None:
        if not isinstance(controller_evidence, dict):
            raise ValueError("evidence.controller_identity must be an object")
        controller_capture = _text(
            controller_evidence.get("capture"),
            "evidence.controller_identity.capture",
        )
        controller_capture_sha256 = _hash(
            controller_evidence.get("capture_sha256"),
            "evidence.controller_identity.capture_sha256",
        )
    runtime_types = _type_map(value.get("runtime_types"))
    properties = _state_map(value.get("properties"), "properties")
    active = _active_map(value.get("active"))
    playback = _state_map(value.get("playback"), "playback")
    state_paths = {
        path
        for values in (properties, active, playback)
        for path, _fields in values
    }
    if state_paths != {path for path, _name in runtime_types}:
        raise ValueError(
            f"{asset} scenario {index} runtime-type coverage is inconsistent"
        )
    return Dmc5CapturedPreview(
        key=_text(value.get("key"), "key") or "",
        label=_text(value.get("label"), "label") or "",
        description=_text(value.get("description"), "description") or "",
        owner_name=_text(value.get("owner_name"), "owner_name", optional=True),
        controller_type=_text(
            value.get("controller_type"),
            "controller_type",
            optional=True,
        ),
        runtime_types=runtime_types,
        properties=properties,
        active=active,
        playback=playback,
        capture=_text(evidence.get("capture"), "evidence.capture") or "",
        capture_sha256=_hash(
            evidence.get("capture_sha256"),
            "evidence.capture_sha256",
        ),
        controller_capture=controller_capture,
        controller_capture_sha256=controller_capture_sha256,
        matched_objects=matched,
        sampled_objects=sampled,
        scene_objects=scene,
    )


@lru_cache(maxsize=None)
def _catalog(
    path: str,
    gui_version: int,
) -> tuple[dict[str, tuple[Dmc5CapturedPreview, ...]], str | None]:
    source = resource_path(path)
    if not source.is_file():
        return {}, "runtime preview catalog is unavailable"
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("catalog root must be an object")
        if (
            payload.get("schema") != 2
            or payload.get("game") != "DMC5"
            or int(payload.get("gui_version", -1)) != int(gui_version)
        ):
            raise ValueError("unsupported catalog identity, schema, or GUI version")
        sources = payload.get("capture_sources")
        if not isinstance(sources, list) or not sources:
            raise ValueError("catalog has no capture sources")
        source_hashes: dict[str, str] = {}
        for index, capture in enumerate(sources):
            if not isinstance(capture, dict):
                raise ValueError(f"capture source {index} must be an object")
            name = _text(capture.get("path"), f"capture source {index}.path")
            digest = _hash(capture.get("sha256"), f"capture source {index}.sha256")
            assert name is not None
            if name in source_hashes:
                raise ValueError(f"duplicate capture source {name!r}")
            source_hashes[name] = digest
        values = payload.get("scenarios")
        if not isinstance(values, dict):
            raise ValueError("catalog has no scenario mapping")
        result: dict[str, tuple[Dmc5CapturedPreview, ...]] = {}
        keys: set[str] = set()
        for raw_asset, raw_records in values.items():
            asset = normalize_gui_resource_path(str(raw_asset))
            if asset in result:
                raise ValueError(f"duplicate normalized GUI asset {asset!r}")
            if not isinstance(raw_records, list):
                raise ValueError(f"{asset} scenarios must be an array")
            records = tuple(
                _record(value, asset, index)
                for index, value in enumerate(raw_records)
            )
            for record in records:
                if record.key in keys:
                    raise ValueError(f"duplicate scenario key {record.key!r}")
                if source_hashes.get(record.capture) != record.capture_sha256:
                    raise ValueError(
                        f"scenario {record.key!r} has unknown capture evidence"
                    )
                if (
                    record.controller_capture is not None
                    and source_hashes.get(record.controller_capture)
                    != record.controller_capture_sha256
                ):
                    raise ValueError(
                        f"scenario {record.key!r} has unknown controller evidence"
                    )
                keys.add(record.key)
            result[asset] = records
        return result, None
    except (OSError, TypeError, ValueError) as exc:
        return {}, f"runtime preview catalog is invalid: {exc}"


def captured_dmc5_previews(
    gui_path: str,
    catalog_path: str | None,
    gui_version: int,
) -> tuple[tuple[Dmc5CapturedPreview, ...], tuple[str, ...]]:
    """Return exact-build captures for ``gui_path`` without guessing state."""

    if not catalog_path:
        return (), ()
    catalog, error = _catalog(catalog_path, gui_version)
    if error is not None:
        return (), (error,)
    return catalog.get(normalize_gui_resource_path(gui_path), ()), ()
