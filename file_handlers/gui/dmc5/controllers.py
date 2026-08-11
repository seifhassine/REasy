"""Companion game-controller context for GUI interaction previews."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

from utils.app_paths import resource_path
from utils.resource_file_utils import ResourceDataLoader
from utils.type_registry import TypeRegistry

from ..adapter import GuiPreviewScenario
from ..profiles import GuiFormatProfile
from ..scene import GuiScene, GuiSceneNode, normalize_gui_resource_path
from .runtime_scenarios import (
    Dmc5CapturedPreview,
    FrozenStateMap,
    captured_dmc5_previews,
)


@dataclass(frozen=True, slots=True)
class Dmc5RuntimeListBehavior:
    """One build-scoped native controller binding recovered from the EXE."""

    controller_type: str
    field_name: str
    field_offset: int
    list_type: str
    interactive: bool
    mouse_select_name: str
    mouse_select_type: int
    exclusive_target: bool


@dataclass(frozen=True, slots=True)
class Dmc5ControllerHost:
    """One verified companion PFB and its serialized/native capabilities."""

    source: str
    types: frozenset[str]
    resource_path: str = ""
    gui_owner_names: frozenset[str] = frozenset()
    behaviors: tuple[Dmc5RuntimeListBehavior, ...] = ()


@dataclass(frozen=True, slots=True)
class _Dmc5ControllerCatalog:
    paths_by_gui: dict[str, tuple[str, ...]]
    behaviors_by_type: dict[str, Dmc5RuntimeListBehavior]


@dataclass(frozen=True, slots=True)
class Dmc5GuiControllerContext:
    """Controller types proven by prefabs that instantiate this GUI."""

    hosts: tuple[Dmc5ControllerHost, ...] = ()
    errors: tuple[str, ...] = ()
    active_source: str | None = None
    active_list_path: str | None = None

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(host.source for host in self.hosts)

    @property
    def types(self) -> frozenset[str]:
        return frozenset(name for host in self.hosts for name in host.types)

    @property
    def active_host(self) -> Dmc5ControllerHost | None:
        if self.active_source is not None:
            return next(
                (host for host in self.hosts if host.source == self.active_source),
                None,
            )
        return self.hosts[0] if len(self.hosts) == 1 else None

    @property
    def active_types(self) -> frozenset[str]:
        """Types belonging to the explicitly selected (or sole) host PFB."""

        host = self.active_host
        return host.types if host is not None else frozenset()

    @property
    def active_behaviors(self) -> tuple[Dmc5RuntimeListBehavior, ...]:
        """Recovered behaviors belonging to the selected companion PFB."""

        host = self.active_host
        return host.behaviors if host is not None else ()

    @property
    def runtime_list_behavior(self) -> Dmc5RuntimeListBehavior | None:
        """The unambiguous runtime-list binding for the selected host."""

        behaviors = self.active_behaviors
        return behaviors[0] if len(behaviors) == 1 else None

    @property
    def diagnostics(self) -> tuple[str, ...]:
        if len(self.hosts) > 1 and self.active_source is None:
            return ("multiple companion PFB hosts; select one for controller behavior",)
        parts: list[str] = []
        if len(self.active_behaviors) > 1:
            parts.append("multiple recovered runtime-list bindings are ambiguous")
        behavior = self.runtime_list_behavior
        if behavior is not None and self.active_list_path is None:
            type_name = behavior.list_type.rsplit(".", 1)[-1]
            parts.append(
                f"external controller {type_name} target is unresolved in this scenario"
            )
        app_types = sorted(
            name for name in self.active_types if name.startswith("app.")
        )
        if app_types:
            parts.append(
                "external controller/save-state values from " + ", ".join(app_types)
            )
        return tuple(parts)

    @property
    def requires_runtime_list(self) -> bool:
        """Whether the selected host owns one recovered runtime-list field."""

        return self.runtime_list_behavior is not None

    def accepts_list(self, node: GuiSceneNode) -> bool:
        """Return whether ``node`` matches the recovered runtime target type."""

        behavior = self.runtime_list_behavior
        return behavior is not None and node.object.type_name == behavior.list_type

    def with_active_source(self, source: str | None) -> "Dmc5GuiControllerContext":
        if source is not None and source not in self.sources:
            raise ValueError(f"unknown GUI controller source: {source}")
        return replace(self, active_source=source, active_list_path=None)

    def with_active_list_path(self, path: str | None) -> "Dmc5GuiControllerContext":
        if path is not None and not self.requires_runtime_list:
            raise ValueError("the selected GUI controller has no recovered list field")
        return replace(self, active_list_path=str(path) if path is not None else None)

    def _selected_list_behavior(
        self,
        owner: GuiSceneNode,
    ) -> Dmc5RuntimeListBehavior | None:
        behavior = self.runtime_list_behavior
        return (
            behavior
            if behavior is not None
            and owner.object.type_name == behavior.list_type
            and owner.path == self.active_list_path
            else None
        )

    def interactive(self, owner: GuiSceneNode, authored: bool) -> bool:
        """Apply the controller's runtime Control.Interactive write."""

        behavior = self._selected_list_behavior(owner)
        return behavior.interactive if behavior is not None else authored

    def runtime_active(self, node: GuiSceneNode) -> bool:
        """Apply the explicitly supplied native controller-list binding."""

        behavior = self.runtime_list_behavior
        return (
            behavior is None
            or self.active_list_path is None
            or not behavior.exclusive_target
            or node.object.type_name != behavior.list_type
            or node.path == self.active_list_path
        )

    def mouse_select_type(self, owner: GuiSceneNode, authored: int) -> int:
        """Apply only reversed controller writes, leaving GUIR as the baseline."""

        behavior = self._selected_list_behavior(owner)
        return behavior.mouse_select_type if behavior is not None else authored


@dataclass(frozen=True, slots=True)
class Dmc5PreviewState:
    """DMC5-specific payload behind a generic preview scenario."""

    controller: Dmc5GuiControllerContext = Dmc5GuiControllerContext()
    properties: FrozenStateMap = ()
    active: tuple[tuple[str, bool], ...] = ()
    playback: FrozenStateMap = ()


def _host_label(source: str) -> str:
    name = Path(source).name
    return name.partition(".pfb")[0] or name


def _captured_host(
    record: Dmc5CapturedPreview,
    context: Dmc5GuiControllerContext,
) -> tuple[Dmc5ControllerHost | None, tuple[str, ...]]:
    """Resolve a capture to a host only through exact serialized identities."""

    if not context.hosts:
        if record.controller_type:
            return None, (
                f"runtime controller {record.controller_type} has no resolved companion PFB",
            )
        return None, ()

    constraints: list[set[str]] = []
    issues: list[str] = []
    if record.controller_type:
        matches = {
            host.source
            for host in context.hosts
            if record.controller_type in host.types
        }
        if matches:
            constraints.append(matches)
        else:
            issues.append(
                f"runtime controller {record.controller_type} is absent from "
                "companion PFBs"
            )
    if record.owner_name:
        matches = {
            host.source
            for host in context.hosts
            if record.owner_name in host.gui_owner_names
        }
        if matches:
            constraints.append(matches)
        else:
            issues.append(
                f"runtime GUI owner {record.owner_name!r} is absent from companion PFBs"
            )
    if not constraints:
        issues.append("runtime state does not identify one companion PFB exactly")
        return None, tuple(issues)
    sources = set.intersection(*constraints)
    if len(sources) != 1:
        issues.append(
            "runtime controller and GUI-owner identities do not resolve one "
            "companion PFB"
        )
        return None, tuple(issues)
    source = next(iter(sources))
    return next(host for host in context.hosts if host.source == source), tuple(issues)


def _captured_state_for_scene(
    values: FrozenStateMap,
    scene: GuiScene,
    runtime_types: dict[str, str],
) -> tuple[FrozenStateMap, int]:
    retained = tuple(
        (path, fields)
        for path, fields in values
        if path in scene.nodes_by_path
        and scene.nodes_by_path[path].object.type_name == runtime_types.get(path)
    )
    return retained, len(values) - len(retained)


def _captured_active_for_scene(
    values: tuple[tuple[str, bool], ...],
    scene: GuiScene,
    runtime_types: dict[str, str],
) -> tuple[tuple[tuple[str, bool], ...], int]:
    retained = tuple(
        (path, active)
        for path, active in values
        if path in scene.nodes_by_path
        and scene.nodes_by_path[path].object.type_name == runtime_types.get(path)
    )
    return retained, len(values) - len(retained)


def _captured_preview_scenarios(
    record: Dmc5CapturedPreview,
    context: Dmc5GuiControllerContext,
    scene: GuiScene,
) -> list[GuiPreviewScenario]:
    runtime_types = dict(record.runtime_types)
    properties, missing_properties = _captured_state_for_scene(
        record.properties,
        scene,
        runtime_types,
    )
    active, missing_active = _captured_active_for_scene(
        record.active,
        scene,
        runtime_types,
    )
    playback, missing_playback = _captured_state_for_scene(
        record.playback,
        scene,
        runtime_types,
    )
    issues = [*context.errors]
    if record.matched_objects < record.sampled_objects:
        issues.append(
            f"runtime state matches {record.matched_objects}/"
            f"{record.sampled_objects} sampled objects"
        )
    if record.matched_objects < record.scene_objects:
        issues.append(
            "runtime state covers "
            f"{record.matched_objects}/{record.scene_objects} scene objects"
        )
    missing = missing_properties + missing_active + missing_playback
    if missing:
        issues.append(
            f"{missing} runtime object paths or types do not match this GUI"
        )

    host, host_issues = _captured_host(record, context)
    issues.extend(host_issues)
    selected = (
        context.with_active_source(host.source)
        if host is not None
        else context
    )
    alternatives: list[
        tuple[str, str, Dmc5GuiControllerContext, tuple[str, ...]]
    ] = []
    behavior = selected.runtime_list_behavior
    if behavior is None:
        alternatives.append(("", "", selected, ()))
    else:
        candidates = [node for node in scene.nodes if selected.accepts_list(node)]
        if len(candidates) == 1:
            alternatives.append(
                ("", "", selected.with_active_list_path(candidates[0].path), ())
            )
        elif candidates:
            for node in candidates:
                alternatives.append(
                    (
                        f"|target:{node.path}",
                        f" — {node.object.name}",
                        selected.with_active_list_path(node.path),
                        (
                            f"runtime state does not identify {behavior.controller_type}."
                            f"{behavior.field_name}; this alternative uses "
                            f"{node.path}",
                        ),
                    )
                )
        else:
            alternatives.append(
                (
                    "",
                    "",
                    selected,
                    (f"no {behavior.list_type} target exists in this GUI",),
                )
            )

    result: list[GuiPreviewScenario] = []
    for key_suffix, label_suffix, resolved, alternative_issues in alternatives:
        scenario_issues = tuple(dict.fromkeys(
            (*issues, *alternative_issues, *resolved.diagnostics)
        ))
        complete = (
            record.matched_objects
            == record.sampled_objects
            == record.scene_objects
            and not scenario_issues
        )
        description = record.description
        if key_suffix:
            description += (
                f" Recovered {behavior.controller_type}.{behavior.field_name} "
                f"is bound to {resolved.active_list_path}."
            )
        result.append(
            GuiPreviewScenario(
                key=record.key + key_suffix,
                label=record.label + label_suffix,
                description=description,
                coverage="Runtime state" if complete else "Partial runtime",
                state=Dmc5PreviewState(
                    controller=resolved,
                    properties=properties,
                    active=active,
                    playback=playback,
                ),
                issues=scenario_issues,
            )
        )
    return result


def build_dmc5_preview_scenarios(
    context: Dmc5GuiControllerContext,
    scene: GuiScene,
    gui_path: str,
    profile: GuiFormatProfile,
) -> tuple[GuiPreviewScenario, ...]:
    """Enumerate coherent contexts without exposing native pointer controls."""

    captures, capture_errors = captured_dmc5_previews(
        gui_path,
        profile.preview_scenario_catalog_path,
        profile.version,
    )
    authored_context = replace(
        context,
        hosts=(),
        active_source=None,
        active_list_path=None,
    )
    authored_issues = context.errors + capture_errors + (
        ("companion controller state is not applied",)
        if context.hosts
        else ()
    )
    authored = GuiPreviewScenario(
        key="authored",
        label="File default",
        description="State stored in the GUI file, without external game-controller values.",
        coverage="File default",
        state=Dmc5PreviewState(controller=authored_context),
        issues=authored_issues,
    )
    captured = [
        scenario
        for record in captures
        for scenario in _captured_preview_scenarios(record, context, scene)
    ]
    captured_sources = {
        scenario.state.controller.active_source
        for scenario in captured
        if scenario.state.controller.active_source is not None
    }
    runtime: list[GuiPreviewScenario] = []
    for host in context.hosts:
        if host.source in captured_sources:
            continue
        selected = context.with_active_source(host.source)
        behavior = selected.runtime_list_behavior
        candidates = (
            [node for node in scene.nodes if selected.accepts_list(node)]
            if behavior is not None
            else []
        )
        if not candidates:
            issues = (*selected.errors, *selected.diagnostics)
            runtime.append(
                GuiPreviewScenario(
                    key=f"host:{host.source}",
                    label=_host_label(host.source),
                    description=(
                        "Companion controller host; additional game-runtime values "
                        "are unavailable."
                    ),
                    coverage="Partial runtime",
                    state=Dmc5PreviewState(controller=selected),
                    issues=issues,
                )
            )
            continue
        for node in candidates:
            resolved = selected.with_active_list_path(node.path)
            issues = (*resolved.errors, *resolved.diagnostics)
            runtime.append(
                GuiPreviewScenario(
                    key=f"host:{host.source}|target:{node.path}",
                    label=f"{_host_label(host.source)} — {node.object.name}",
                    description=(
                        f"{behavior.controller_type}.{behavior.field_name} targets "
                        f"{node.path}; remaining save/controller values stay "
                        "explicit."
                    ),
                    coverage="Partial runtime" if issues else "Recovered runtime",
                    state=Dmc5PreviewState(controller=resolved),
                    issues=issues,
                )
            )
    if len(captured) == 1:
        captured[0] = replace(captured[0], preferred=True)
    elif not captured and len(runtime) == 1:
        runtime[0] = replace(runtime[0], preferred=True)
    elif not captured and not runtime:
        authored = replace(authored, preferred=True)
    return authored, *captured, *runtime


@lru_cache(maxsize=1)
def _dmc5_registry() -> TypeRegistry | None:
    path = resource_path("resources/data/dumps/rszdmc5.json")
    return TypeRegistry(str(path)) if path.is_file() else None


def _value(value: object) -> object:
    scalar = getattr(value, "value", value)
    return scalar.rstrip("\0") if isinstance(scalar, str) else scalar


def _pfb_gui_owner_names(
    parsed: object,
    expected: str,
    registry: TypeRegistry,
) -> frozenset[str]:
    """Return GameObject names that own a GUI for ``expected`` in one PFB."""

    owners: set[str] = set()
    cursor = 0
    object_table = parsed.object_table
    instance_infos = parsed.instance_infos
    elements = parsed.parsed_elements
    for game_object in parsed.gameobjects:
        end = cursor + int(game_object.component_count) + 1
        instances = object_table[cursor:end]
        cursor = end
        if len(instances) != int(game_object.component_count) + 1:
            continue
        game_object_index = instances[0]
        if not 0 <= game_object_index < len(instance_infos):
            continue
        owner_name = _value(elements.get(game_object_index, {}).get("Name", ""))
        if not owner_name:
            continue
        for component_index in instances[1:]:
            if not 0 <= component_index < len(instance_infos):
                continue
            info = instance_infos[component_index]
            type_name = str((registry.get_type_info(info.type_id) or {}).get("name", ""))
            if type_name != "via.gui.GUI":
                continue
            asset = _value(elements.get(component_index, {}).get("Asset", ""))
            if asset and normalize_gui_resource_path(str(asset)) == expected:
                owners.add(str(owner_name))
    return frozenset(owners)


def _runtime_list_behavior(
    value: object,
    index: int,
) -> Dmc5RuntimeListBehavior:
    if not isinstance(value, dict):
        raise ValueError(f"controller behavior {index} is not an object")
    if value.get("kind") != "runtime_list_binding":
        raise ValueError(f"controller behavior {index} has an unsupported kind")
    target = value.get("target")
    effects = value.get("effects")
    if not isinstance(target, dict) or not isinstance(effects, dict):
        raise ValueError(f"controller behavior {index} is incomplete")
    if target.get("binding") != "runtime":
        raise ValueError(f"controller behavior {index} is not runtime-bound")
    mouse = effects.get("mouse_select_type")
    if not isinstance(mouse, dict):
        raise ValueError(f"controller behavior {index} has no mouse-select effect")
    evidence = value.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError(f"controller behavior {index} has no native evidence")
    call_sites = evidence.get("call_site_rvas")
    if not isinstance(call_sites, list) or not call_sites:
        raise ValueError(f"controller behavior {index} has no native call sites")
    interactive = effects.get("interactive")
    exclusive_target = effects.get("exclusive_target")
    if not isinstance(interactive, bool) or not isinstance(exclusive_target, bool):
        raise ValueError(f"controller behavior {index} has invalid boolean effects")

    controller_type = str(value.get("controller_type", "")).strip()
    field_name = str(target.get("field", "")).strip()
    list_type = str(target.get("type", "")).strip()
    mouse_name = str(mouse.get("name", "")).strip()
    if not all((controller_type, field_name, list_type, mouse_name)):
        raise ValueError(f"controller behavior {index} has an empty semantic name")
    try:
        field_offset = int(str(target["offset"]), 0)
        int(str(evidence["setter_rva"]), 0)
        for call_site in call_sites:
            int(str(call_site), 0)
        if isinstance(mouse["value"], bool):
            raise ValueError
        mouse_value = int(mouse["value"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"controller behavior {index} has an invalid numeric value"
        ) from exc
    return Dmc5RuntimeListBehavior(
        controller_type=controller_type,
        field_name=field_name,
        field_offset=field_offset,
        list_type=list_type,
        interactive=interactive,
        mouse_select_name=mouse_name,
        mouse_select_type=mouse_value,
        exclusive_target=exclusive_target,
    )


@lru_cache(maxsize=None)
def _controller_catalog(
    path: str,
    gui_version: int,
) -> tuple[_Dmc5ControllerCatalog, str | None]:
    empty = _Dmc5ControllerCatalog({}, {})
    source = resource_path(path)
    if not source.is_file():
        return empty, "GUI controller catalog is unavailable"
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
        if payload.get("schema") != 2 or payload.get("game") != "DMC5":
            raise ValueError("unsupported catalog identity or schema")
        if int(payload.get("gui_version", -1)) != int(gui_version):
            raise ValueError("catalog GUI version does not match the active profile")
        runtime_build = payload.get("runtime_build")
        if not isinstance(runtime_build, dict):
            raise ValueError("catalog has no runtime-build fingerprint")
        hashes = (
            runtime_build.get("executable_sha256"),
            runtime_build.get("analysis_image_sha256"),
        )
        if any(
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in value)
            for value in hashes
        ):
            raise ValueError("catalog has an invalid runtime-build fingerprint")
        int(str(runtime_build["image_base"]), 0)
        behavior_values = payload.get("controller_behaviors")
        if not isinstance(behavior_values, list):
            raise ValueError("catalog has no recovered controller behaviors")
        behaviors: dict[str, Dmc5RuntimeListBehavior] = {}
        for index, value in enumerate(behavior_values):
            behavior = _runtime_list_behavior(value, index)
            if behavior.controller_type in behaviors:
                raise ValueError(
                    f"duplicate behavior for {behavior.controller_type}"
                )
            behaviors[behavior.controller_type] = behavior
    except (KeyError, OSError, ValueError, TypeError) as exc:
        return empty, f"GUI controller catalog is invalid: {exc}"
    controllers = payload.get("controllers", {})
    if not isinstance(controllers, dict):
        return empty, "GUI controller catalog has no controller mapping"
    result: dict[str, tuple[str, ...]] = {}
    indexed_types: set[str] = set()
    for gui_path, records in controllers.items():
        if not isinstance(records, list):
            continue
        paths: list[str] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            if record.get("path"):
                paths.append(str(record["path"]))
            record_types = record.get("types")
            if isinstance(record_types, list):
                indexed_types.update(str(name) for name in record_types if name)
        if paths:
            result[normalize_gui_resource_path(str(gui_path))] = tuple(paths)
    unknown = behaviors.keys() - indexed_types
    if unknown:
        return empty, (
            "GUI controller catalog behavior types are not indexed: "
            + ", ".join(sorted(unknown))
        )
    return _Dmc5ControllerCatalog(result, behaviors), None


def discover_dmc5_controller_context(
    gui_path: str,
    loader: ResourceDataLoader | None,
    profile: GuiFormatProfile,
) -> Dmc5GuiControllerContext:
    """Find prefabs whose serialized GUI asset exactly matches ``gui_path``.

    The profile's reverse-dependency index was generated by parsing every PFB
    in the base game. Candidates are parsed again through the active project/
    PAK loader, so loose-file mods override the indexed base data and an entry
    is accepted only when its current serialized ``via.gui.GUI.Asset`` matches.
    """

    if loader is None:
        return Dmc5GuiControllerContext()
    registry = _dmc5_registry()
    if registry is None:
        return Dmc5GuiControllerContext(
            errors=("DMC5 RSZ type registry is unavailable",)
        )
    if not profile.controller_index_path:
        return Dmc5GuiControllerContext(
            errors=("GUI controller catalog is not configured",)
        )
    if not resource_path(profile.controller_index_path).is_file():
        return Dmc5GuiControllerContext(
            errors=("GUI controller catalog is unavailable",)
        )
    expected = normalize_gui_resource_path(gui_path)
    catalog, index_error = _controller_catalog(
        profile.controller_index_path,
        profile.version,
    )
    if index_error:
        return Dmc5GuiControllerContext(errors=(index_error,))
    candidates = catalog.paths_by_gui.get(expected, ())
    hosts: list[Dmc5ControllerHost] = []
    errors: list[str] = []
    from file_handlers.rsz.rsz_file import RszFile

    for candidate in candidates:
        resolved = loader(candidate)
        if resolved is None:
            errors.append(f"indexed GUI controller prefab is unresolved: {candidate}")
            continue
        source, data = resolved
        try:
            parsed = RszFile()
            parsed.type_registry = registry
            parsed.game_version = "DMC5"
            parsed.filepath = candidate
            parsed.read(bytes(data))
            names: list[str] = []
            assets: list[str] = []
            for index, info in enumerate(parsed.instance_infos):
                type_info = registry.get_type_info(info.type_id) or {}
                name = str(type_info.get("name", ""))
                names.append(name)
                if name == "via.gui.GUI":
                    asset = _value(
                        parsed.parsed_elements.get(index, {}).get("Asset", "")
                    )
                    if asset:
                        assets.append(normalize_gui_resource_path(str(asset)))
            if expected not in assets:
                continue
        except Exception as exc:
            errors.append(f"{source}: could not inspect GUI controller: {exc}")
            continue
        source_name = str(source)
        serialized_types = frozenset(name for name in names if name)
        hosts.append(
            Dmc5ControllerHost(
                source=source_name,
                types=serialized_types,
                resource_path=candidate,
                gui_owner_names=_pfb_gui_owner_names(
                    parsed,
                    expected,
                    registry,
                ),
                behaviors=tuple(
                    catalog.behaviors_by_type[name]
                    for name in sorted(serialized_types)
                    if name in catalog.behaviors_by_type
                ),
            )
        )
    return Dmc5GuiControllerContext(
        hosts=tuple(hosts),
        errors=tuple(errors),
    )
