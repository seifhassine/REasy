"""Typed, preview-only DMC5 runtime-state customization."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from file_handlers.clip.enums import PropertyType

from ..adapter import GuiPreviewControl, GuiPreviewOption, GuiPreviewScenario
from ..scene import GuiScene, normalize_gui_resource_path
from .controllers import (
    Dmc5ControllerHost,
    Dmc5GuiControllerContext,
    Dmc5PreviewState,
)
from .playback import DMC5_SEGMENT_VALUES, dmc5_control_defaults
from .property_codec import encode_dmc5_gui_value


_CONTROL_FIELDS = (
    "PlayState",
    "StatePattern",
    "PlayFrame",
    "Play",
    "NextStateEnable",
    "Segment",
)
_IDENTITY_FIELDS = frozenset({"Name"})


@dataclass(frozen=True, slots=True)
class _ControlKey:
    scope: str
    path: str = ""
    name: str = ""


def _host_id(host: Dmc5ControllerHost) -> str:
    return host.resource_path or host.source


def _host_label(host: Dmc5ControllerHost) -> str:
    name = Path(_host_id(host)).name
    return name.partition(".pfb")[0] or name


def _available_context(
    scenarios: tuple[GuiPreviewScenario, ...],
) -> Dmc5GuiControllerContext:
    hosts: dict[str, Dmc5ControllerHost] = {}
    errors: list[str] = []
    for scenario in scenarios:
        state = scenario.state
        if not isinstance(state, Dmc5PreviewState):
            continue
        for host in state.controller.hosts:
            hosts.setdefault(_host_id(host).casefold(), host)
        errors.extend(state.controller.errors)
    return Dmc5GuiControllerContext(
        hosts=tuple(hosts.values()),
        errors=tuple(dict.fromkeys(errors)),
    )


def _base_scenario(
    scenarios: tuple[GuiPreviewScenario, ...],
    scenario: GuiPreviewScenario,
) -> GuiPreviewScenario:
    key = scenario.base_key if scenario.custom else scenario.key
    return next(
        (item for item in scenarios if item.key == key),
        next(item for item in scenarios if item.key == "authored"),
    )


def _state(scenario: GuiPreviewScenario) -> Dmc5PreviewState:
    if not isinstance(scenario.state, Dmc5PreviewState):
        raise ValueError("preview scenario has no DMC5 runtime state")
    return scenario.state


def _freeze(value: object) -> object:
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _state_map(values) -> dict[str, dict[str, object]]:
    return {
        path: {name: value for name, value in fields}
        for path, fields in values
    }


def _frozen_state(values: dict[str, dict[str, object]]):
    return tuple(
        (path, tuple((name, _freeze(value)) for name, value in fields.items()))
        for path, fields in values.items()
        if fields
    )


def _set_state_field(values, path: str, name: str, value: object, inherit: bool):
    result = _state_map(values)
    fields = result.setdefault(path, {})
    if inherit:
        fields.pop(name, None)
    else:
        fields[name] = _freeze(value)
    if not fields:
        result.pop(path, None)
    return _frozen_state(result)


def _custom_scenario(
    base: GuiPreviewScenario,
    state: Dmc5PreviewState,
    issues: tuple[str, ...] = (),
) -> GuiPreviewScenario:
    return GuiPreviewScenario(
        key=f"custom:{base.key}",
        label=f"Custom preview · {base.label}",
        description=(
            f"User-defined preview-only state based on {base.label}. "
            "These runtime values are not serialized in the GUI."
        ),
        coverage="Custom preview",
        state=state,
        issues=tuple(dict.fromkeys((*base.issues, *issues))),
        custom=True,
        base_key=base.key,
    )


def _selected_node(scene: GuiScene, path: str | None):
    return scene.nodes_by_path.get(path) if path else None


def _controller_controls(
    scenarios: tuple[GuiPreviewScenario, ...],
    state: Dmc5PreviewState,
    scene: GuiScene,
) -> list[GuiPreviewControl]:
    available = _available_context(scenarios)
    if not available.hosts:
        return []
    active = state.controller.active_host
    controls = [GuiPreviewControl(
        key=_ControlKey("controller"),
        group="Controller",
        label="Controller host",
        description="Companion PFB that supplies external GUI behavior.",
        value=_host_id(active) if active is not None else None,
        options=(
            GuiPreviewOption(None, "No external controller"),
            *(
                GuiPreviewOption(
                    _host_id(host),
                    _host_label(host),
                    ", ".join(sorted(name for name in host.types if name.startswith("app."))),
                )
                for host in available.hosts
            ),
        ),
    )]
    behavior = state.controller.runtime_list_behavior
    if behavior is None:
        return controls
    candidates = tuple(
        node for node in scene.nodes if state.controller.accepts_list(node)
    )
    controls.append(GuiPreviewControl(
        key=_ControlKey("list_target"),
        group="Controller",
        label=behavior.field_name,
        description=(
            f"Runtime {behavior.list_type.rsplit('.', 1)[-1]} target used for "
            "interaction and exclusive branch activation."
        ),
        value=state.controller.active_list_path,
        options=(
            GuiPreviewOption(None, "Unresolved"),
            *(
                GuiPreviewOption(node.path, f"{node.object.name} — {node.path}")
                for node in candidates
            ),
        ),
    ))
    return controls


def _property_controls(
    state: Dmc5PreviewState,
    scene: GuiScene,
    path: str | None,
) -> list[GuiPreviewControl]:
    node = _selected_node(scene, path)
    if node is None:
        return []
    controls = [GuiPreviewControl(
        key=_ControlKey("active", node.path),
        group="Selected object",
        label="Runtime active",
        description="External native Active state; inherit leaves the file/controller result.",
        value=dict(state.active).get(node.path),
        options=(
            GuiPreviewOption(None, "Inherit"),
            GuiPreviewOption(True, "Active"),
            GuiPreviewOption(False, "Inactive"),
        ),
    )]
    overrides = _state_map(state.properties).get(node.path, {})
    records = {
        prop.name: prop
        for prop in (
            item
            for name in node.properties
            for item in scene.property_records(node, name)
        )
        if prop.name not in _IDENTITY_FIELDS and prop.name not in _CONTROL_FIELDS
    }
    for name in sorted(records, key=str.casefold):
        prop = records[name]
        inherited = name not in overrides
        controls.append(GuiPreviewControl(
            key=_ControlKey("property", node.path, name),
            group="Runtime properties",
            label=name,
            description=(
                "Preview-only reflected property override; disable the checkbox "
                "to inherit the file/animation value."
            ),
            value=node.properties.get(name) if inherited else overrides[name],
            value_type=prop.type,
            can_inherit=True,
            inherited=inherited,
        ))
    return controls


def _playback_controls(
    state: Dmc5PreviewState,
    scene: GuiScene,
    path: str | None,
) -> list[GuiPreviewControl]:
    node = _selected_node(scene, path)
    if node is None:
        return []
    defaults = dmc5_control_defaults(node)
    if not defaults:
        return []
    overrides = _state_map(state.playback).get(node.path, {})
    symbol = node.prototype.symbol
    states = tuple(dict.fromkeys(item.name for item in symbol.animations))
    max_pattern = max(
        (len(symbol.animation_states()[name]) - 1 for name in states),
        default=0,
    )
    max_frame = max(
        (float(item.clip.total_frame) for item in symbol.animations),
        default=0.0,
    )

    def control(
        name: str,
        kind: PropertyType,
        *,
        options: tuple[GuiPreviewOption, ...] = (),
        minimum: float | None = None,
        maximum: float | None = None,
        decimals: int | None = None,
    ) -> GuiPreviewControl:
        inherited = name not in overrides
        value = defaults[name] if inherited else overrides[name]
        if name == "Segment" and isinstance(value, str):
            value = DMC5_SEGMENT_VALUES.get(value, value)
        return GuiPreviewControl(
            key=_ControlKey("playback", node.path, name),
            group="Animation runtime",
            label=name,
            description="Preview-only value passed through the recovered Control setter.",
            value=value,
            value_type=kind,
            options=options,
            can_inherit=True,
            inherited=inherited,
            minimum=minimum,
            maximum=maximum,
            decimals=decimals,
        )

    return [
        control(
            "PlayState",
            PropertyType.ENUM,
            options=(
                GuiPreviewOption("", "No state"),
                *(GuiPreviewOption(name, name) for name in states),
            ),
        ),
        control(
            "StatePattern",
            PropertyType.S32,
            minimum=0,
            maximum=max_pattern,
            decimals=0,
        ),
        control(
            "PlayFrame",
            PropertyType.F32,
            minimum=-1.0,
            maximum=max_frame,
            decimals=3,
        ),
        control("Play", PropertyType.BOOL),
        control("NextStateEnable", PropertyType.BOOL),
        control(
            "Segment",
            PropertyType.S32,
            options=tuple(
                GuiPreviewOption(value, name)
                for name, value in DMC5_SEGMENT_VALUES.items()
            ),
        ),
    ]


def dmc5_preview_controls(
    scenarios: tuple[GuiPreviewScenario, ...],
    scenario: GuiPreviewScenario,
    scene: GuiScene,
    selected_path: str | None,
) -> tuple[GuiPreviewControl, ...]:
    state = _state(scenario)
    return tuple((
        *_controller_controls(scenarios, state, scene),
        *_property_controls(state, scene, selected_path),
        *_playback_controls(state, scene, selected_path),
    ))


def _validate_control(control: GuiPreviewControl, value: object) -> None:
    if control.options:
        if not any(value == option.value for option in control.options):
            raise ValueError(f"{control.label}: unsupported choice")
        return
    if control.value_type is None:
        raise ValueError(f"{control.label}: control has no value type")
    encode_dmc5_gui_value(control.value_type, value, control.label)
    if control.minimum is not None and float(value) < control.minimum:
        raise ValueError(f"{control.label}: value is below {control.minimum:g}")
    if control.maximum is not None and float(value) > control.maximum:
        raise ValueError(f"{control.label}: value is above {control.maximum:g}")


def set_dmc5_preview_control(
    scenarios: tuple[GuiPreviewScenario, ...],
    scenario: GuiPreviewScenario,
    scene: GuiScene,
    key: object,
    value: object,
    *,
    inherit: bool = False,
) -> GuiPreviewScenario:
    controls = dmc5_preview_controls(scenarios, scenario, scene, getattr(key, "path", None))
    control = next((item for item in controls if item.key == key), None)
    if control is None or not isinstance(key, _ControlKey):
        raise ValueError("preview control is not available for this GUI state")
    if inherit and not control.can_inherit:
        raise ValueError(f"{control.label} cannot inherit a value")
    if not inherit:
        _validate_control(control, value)

    base = _base_scenario(scenarios, scenario)
    state = _state(scenario)
    if key.scope == "controller":
        available = _available_context(scenarios)
        host = next(
            (item for item in available.hosts if _host_id(item) == value),
            None,
        )
        controller = (
            available.with_active_source(host.source)
            if host is not None
            else Dmc5GuiControllerContext(errors=available.errors)
        )
        state = replace(state, controller=controller)
    elif key.scope == "list_target":
        state = replace(
            state,
            controller=state.controller.with_active_list_path(
                str(value) if value is not None else None
            ),
        )
    elif key.scope == "active":
        active = dict(state.active)
        if value is None:
            active.pop(key.path, None)
        else:
            active[key.path] = bool(value)
        state = replace(state, active=tuple(active.items()))
    elif key.scope == "property":
        state = replace(
            state,
            properties=_set_state_field(
                state.properties, key.path, key.name, value, inherit
            ),
        )
    elif key.scope == "playback":
        state = replace(
            state,
            playback=_set_state_field(
                state.playback, key.path, key.name, value, inherit
            ),
        )
    else:
        raise ValueError(f"unknown preview-control scope {key.scope!r}")
    return _custom_scenario(base, state)


def rebase_dmc5_custom_preview(
    scenarios: tuple[GuiPreviewScenario, ...],
    scenario: GuiPreviewScenario,
    scene: GuiScene,
) -> GuiPreviewScenario:
    if not scenario.custom:
        return scenario
    base = _base_scenario(scenarios, scenario)
    state = _state(scenario)
    issues: list[str] = []

    available = _available_context(scenarios)
    active = state.controller.active_host
    controller = Dmc5GuiControllerContext(errors=available.errors)
    if active is not None:
        host = next(
            (item for item in available.hosts if _host_id(item) == _host_id(active)),
            None,
        )
        if host is None:
            issues.append(f"custom controller {_host_label(active)!r} is unavailable")
        else:
            controller = available.with_active_source(host.source)
            target = state.controller.active_list_path
            if target is not None:
                node = scene.nodes_by_path.get(target)
                if node is not None and controller.accepts_list(node):
                    controller = controller.with_active_list_path(target)
                else:
                    issues.append(f"custom controller target {target!r} is unavailable")

    properties: dict[str, dict[str, object]] = {}
    for path, fields in _state_map(state.properties).items():
        node = scene.nodes_by_path.get(path)
        if node is None:
            issues.append(f"custom property target {path!r} is unavailable")
            continue
        types = {
            prop.name: prop.type
            for name in node.properties
            for prop in scene.property_records(node, name)
        }
        for name, value in fields.items():
            kind = types.get(name)
            try:
                if kind is None or name in _IDENTITY_FIELDS or name in _CONTROL_FIELDS:
                    raise ValueError
                encode_dmc5_gui_value(kind, value, name)
            except (TypeError, ValueError):
                issues.append(f"custom property {path}.{name} is unavailable")
                continue
            properties.setdefault(path, {})[name] = value

    active_values = tuple(
        (path, value)
        for path, value in state.active
        if path in scene.nodes_by_path
    )
    missing_active = len(state.active) - len(active_values)
    if missing_active:
        issues.append(f"{missing_active} custom Active targets are unavailable")

    playback: dict[str, dict[str, object]] = {}
    for path, fields in _state_map(state.playback).items():
        node = scene.nodes_by_path.get(path)
        if node is None or not dmc5_control_defaults(node):
            issues.append(f"custom animation target {path!r} is unavailable")
            continue
        valid = {
            control.key.name: control
            for control in _playback_controls(state, scene, path)
            if isinstance(control.key, _ControlKey)
        }
        for name, value in fields.items():
            try:
                _validate_control(valid[name], value)
            except (KeyError, TypeError, ValueError):
                issues.append(f"custom animation value {path}.{name} is unavailable")
                continue
            playback.setdefault(path, {})[name] = value

    return _custom_scenario(
        base,
        Dmc5PreviewState(
            controller=controller,
            properties=_frozen_state(properties),
            active=active_values,
            playback=_frozen_state(playback),
        ),
        tuple(issues),
    )


def _json_value(value: object) -> object:
    if isinstance(value, uuid.UUID):
        return {"uuid": str(value)}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError("custom preview contains an unsupported preset value")


def _from_json(value: object) -> object:
    if isinstance(value, dict) and set(value) == {"uuid"}:
        return uuid.UUID(str(value["uuid"]))
    if isinstance(value, list):
        return tuple(_from_json(item) for item in value)
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError("preset contains an unsupported value")


def _export_state(values) -> dict[str, dict[str, object]]:
    return {
        path: {name: _json_value(value) for name, value in fields}
        for path, fields in values
    }


def export_dmc5_preview_preset(
    gui_path: str,
    gui_version: int,
    scenario: GuiPreviewScenario,
) -> dict[str, Any]:
    if not scenario.custom:
        raise ValueError("only a custom preview state can be saved as a preset")
    state = _state(scenario)
    host = state.controller.active_host
    return {
        "schema": 1,
        "game": "DMC5",
        "gui_version": gui_version,
        "gui_path": normalize_gui_resource_path(gui_path),
        "base_scenario": scenario.base_key,
        "controller": {
            "resource": _host_id(host) if host is not None else None,
            "list_target": state.controller.active_list_path,
        },
        "properties": _export_state(state.properties),
        "active": dict(state.active),
        "playback": _export_state(state.playback),
    }


def _import_state(value: object, field: str):
    if not isinstance(value, dict):
        raise ValueError(f"preset {field} must be an object")
    result: dict[str, dict[str, object]] = {}
    for path, fields in value.items():
        if not isinstance(path, str) or not isinstance(fields, dict):
            raise ValueError(f"preset {field} has an invalid target")
        result[path] = {
            str(name): _from_json(raw)
            for name, raw in fields.items()
        }
    return _frozen_state(result)


def import_dmc5_preview_preset(
    gui_path: str,
    gui_version: int,
    payload: dict[str, Any],
    scenarios: tuple[GuiPreviewScenario, ...],
    scene: GuiScene,
) -> GuiPreviewScenario:
    if (
        payload.get("schema") != 1
        or payload.get("game") != "DMC5"
        or int(payload.get("gui_version", -1)) != gui_version
        or normalize_gui_resource_path(str(payload.get("gui_path", "")))
        != normalize_gui_resource_path(gui_path)
    ):
        raise ValueError("preset does not match this DMC5 GUI")
    base_key = payload.get("base_scenario")
    base = next(
        (item for item in scenarios if item.key == base_key),
        next(item for item in scenarios if item.key == "authored"),
    )
    controller_data = payload.get("controller")
    if not isinstance(controller_data, dict):
        raise ValueError("preset controller must be an object")
    available = _available_context(scenarios)
    resource = controller_data.get("resource")
    controller = Dmc5GuiControllerContext(errors=available.errors)
    if resource is not None:
        host = next(
            (item for item in available.hosts if _host_id(item) == resource),
            None,
        )
        if host is None:
            raise ValueError(f"preset controller {resource!r} is unavailable")
        controller = available.with_active_source(host.source)
        target = controller_data.get("list_target")
        if target is not None:
            controller = controller.with_active_list_path(str(target))
    active_data = payload.get("active")
    if not isinstance(active_data, dict) or any(
        not isinstance(path, str) or type(value) is not bool
        for path, value in active_data.items()
    ):
        raise ValueError("preset active state is invalid")
    custom = _custom_scenario(
        base,
        Dmc5PreviewState(
            controller=controller,
            properties=_import_state(payload.get("properties"), "properties"),
            active=tuple(active_data.items()),
            playback=_import_state(payload.get("playback"), "playback"),
        ),
        (
            ("preset base scenario is unavailable; using File default",)
            if base.key != base_key
            else ()
        ),
    )
    return rebase_dmc5_custom_preview(scenarios, custom, scene)
