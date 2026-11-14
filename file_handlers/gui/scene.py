"""See docs/internals/gui-format.md, Scene."""

from __future__ import annotations

import copy
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator

lazy from utils.resource_file_utils import ResourceDataLoader, resource_path_with_version

lazy from .codec import parse_gui
lazy from .errors import GuiFormatError, GuiSceneError
lazy from .model import ZERO_GUID, GuiBinding, GuiDocument, GuiObject, GuiSymbol
lazy from .profiles import GuiRuntimeProfile, gui_runtime


def normalize_gui_resource_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip().lstrip("@/").casefold()
    for marker in ("natives/x64/", "natives/stm/"):
        index = normalized.find(marker)
        if index >= 0:
            normalized = normalized[index + len(marker):]
            break
    normalized = re.sub(r"(?<=\.gui)\.\d+(?:\.(?:x64|stm))?$", "", normalized)
    if not normalized.endswith(".gui"):
        raise GuiSceneError(f"not a GUI resource path: {value!r}")
    return normalized


@dataclass(frozen=True, slots=True)
class GuiResource:
    key: str
    source: str
    document: GuiDocument


@dataclass(frozen=True, slots=True)
class GuiSymbolReference:
    resource: GuiResource
    symbol: GuiSymbol


@dataclass(slots=True, eq=False)
class GuiSceneNode:
    path: str
    resource: GuiResource
    object: GuiObject
    prototype: GuiSymbolReference | None
    runtime: GuiRuntimeProfile
    parent: GuiSceneNode | None = None
    children: list[GuiSceneNode] = field(default_factory=list)
    properties: dict[str, object] = field(default_factory=dict)
    render_properties: dict[str, object] = field(default_factory=dict)
    local_position: tuple[float, float] = (0.0, 0.0)
    world_position: tuple[float, float] = (0.0, 0.0)
    size: tuple[float, float] = (80.0, 30.0)
    anchor: tuple[float, float] = (0.0, 0.0)
    world_transform: tuple[float, ...] = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    world_matrix: tuple | None = None
    scene_matrix: tuple | None = None
    effective_visible: bool = True
    color_scale: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    color_offset: tuple[float, float, float] = (0.0, 0.0, 0.0)
    saturation: float = 1.0
    segment: int | None = None
    source: GuiSceneNode | None = None

    def origin(self) -> GuiSceneNode:
        return self if self.source is None else self.source.origin()

    def walk(self) -> Iterator[GuiSceneNode]:
        yield self
        for child in self.children:
            yield from child.walk()


class GuiWorkspace:
    def __init__(self, resources: list[GuiResource], root_key: str, profile: GuiRuntimeProfile) -> None:
        self.resources = {item.key: item for item in resources}
        self.root_key = root_key
        self.profile = profile
        self.missing_dependencies: dict[str, tuple[str, str, str]] = {}
        self._symbol_cache: dict[tuple[str, uuid.UUID], GuiSymbolReference | None] = {}

    @classmethod
    def from_document(cls, resource_path: str, document: GuiDocument, loader: ResourceDataLoader | None = None, *,
                      profile: GuiRuntimeProfile | None = None) -> GuiWorkspace:
        root_key = normalize_gui_resource_path(resource_path)
        resources = [GuiResource(root_key, document.source, document)]
        loaded, missing, queue = {root_key}, {}, [resources[0]]
        while queue and loader is not None:
            owner = queue.pop(0)
            for path in owner.document.imported_gui_paths:
                key = normalize_gui_resource_path(path)
                if key in loaded or key in missing:
                    continue
                resolved = loader(resource_path_with_version(path, "gui", document.version))
                if resolved is None:
                    missing[key] = path, owner.key, ""
                    continue
                source, data = resolved
                try:
                    dependency = parse_gui(bytes(data), str(source))
                except GuiFormatError as exc:
                    missing[key] = path, owner.key, str(exc)
                    continue
                resource = GuiResource(key, str(source), dependency)
                resources.append(resource)
                queue.append(resource)
                loaded.add(key)
        result = cls(resources, root_key, profile or gui_runtime(document.version))
        result.missing_dependencies = missing
        return result

    @property
    def root(self) -> GuiResource:
        return self.resources[self.root_key]

    def _resolve_symbol(self, resource_key: str, guid: uuid.UUID, visiting: set[str]) -> GuiSymbolReference | None:
        if guid == ZERO_GUID or resource_key in visiting:
            return None
        visiting = {*visiting, resource_key}
        resource = self.resources[resource_key]
        symbol = resource.document.symbol(guid)
        if symbol is not None:
            return GuiSymbolReference(resource, symbol)
        for dependency in resource.document.imported_gui_paths:
            key = normalize_gui_resource_path(dependency)
            if key in self.resources and (found := self._resolve_symbol(key, guid, visiting)) is not None:
                return found
        return None

    def resolve_symbol(self, resource_key: str, guid: uuid.UUID) -> GuiSymbolReference | None:
        cache_key = resource_key, guid
        if cache_key not in self._symbol_cache:
            self._symbol_cache[cache_key] = self._resolve_symbol(resource_key, guid, set())
        return self._symbol_cache[cache_key]

    def invalidate(self) -> None:
        self._symbol_cache.clear()

    def instantiate(self, scope: GuiSymbol | None = None) -> GuiScene:
        resource = self.root
        start = resource.document.root_object
        if start is None:
            raise GuiSceneError(f"{resource.key} has no root object")
        paths: set[str] = set()

        def build(owner: GuiResource, obj: GuiObject, parent: GuiSceneNode | None,
                  stack: tuple[tuple[str, uuid.UUID], ...]) -> GuiSceneNode:
            prototype = self.resolve_symbol(owner.key, obj.prototype_guid)
            base = "/" if parent is None else f"{parent.path.rstrip('/')}/{obj.name}"
            path, suffix = base, 2
            while path in paths:
                path = f"{base}#{suffix}"
                suffix += 1
            paths.add(path)
            node = GuiSceneNode(path, owner, obj, prototype, self.profile, parent)
            identity = prototype and (prototype.resource.key, prototype.symbol.guid)
            if prototype is not None and identity not in stack:
                node.children = [build(prototype.resource, child, node, (*stack, identity))
                                 for child in prototype.symbol.objects]
            return node

        if scope is None:
            root = build(resource, start, None, ())
        else:
            paths.add("/")
            root = GuiSceneNode("/", resource, start, None, self.profile)
            root.children = [build(resource, GuiObject(ZERO_GUID, scope.guid, scope.name, scope.type_name), root, ())]
        scene = self.profile.adapter.create_scene(self, resource, root)
        scene.rebind([] if scope is not None else resource.document.bindings)
        return scene


class GuiScene(ABC):
    def __init__(self, workspace: GuiWorkspace, resource: GuiResource, root: GuiSceneNode) -> None:
        self.workspace = workspace
        self.resource = resource
        self.root = root
        self.nodes = list(root.walk())
        self.nodes_by_path = {item.path: item for item in self.nodes}
        self._preview_overrides: dict[str, dict[str, object]] = {}
        self.bindings: list[tuple[GuiBinding, GuiSceneNode]] = []
        self._bindings_by_node: dict[int, list[GuiBinding]] = {}

    def rebind(self, bindings: list[GuiBinding]) -> None:
        self.bindings, self._bindings_by_node = [], {}
        for binding in bindings:
            target = self.binding_target(binding.target_path)
            held = target.object.property(binding.property.name) if target is not None else None
            if target is not None and binding.target_type == target.object.type_name \
                    and (held is None or held.type == binding.property.type):
                self.bindings.append((binding, target))
                self._bindings_by_node.setdefault(id(target), []).append(binding)
        self.refresh()
        self.apply_bindings()

    @property
    @abstractmethod
    def screen_size(self) -> tuple[float, float]:
        raise NotImplementedError

    @abstractmethod
    def _invalidate_runtime(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def native_children(self, node: GuiSceneNode,
                        properties_by_path: dict[str, dict[str, object]] | None = None) -> tuple[GuiSceneNode, ...]:
        raise NotImplementedError

    def refresh(self) -> None:
        self._invalidate_runtime()
        for item in self.nodes:
            item.properties = item.object.values()
        self.update_preview(self._preview_overrides)

    def apply_bindings(self) -> None:
        self._invalidate_runtime()
        for binding, target in self.bindings:
            target.properties[binding.property.name] = copy.deepcopy(binding.property.value)
        self.update_preview(self._preview_overrides)

    def bindings_for(self, node: GuiSceneNode, name: str | None = None) -> tuple[GuiBinding, ...]:
        bindings = self._bindings_by_node.get(id(node), ())
        return tuple(item for item in bindings if name is None or item.property.name == name)

    @abstractmethod
    def update_preview(self, overrides: dict[str, dict[str, object]] | None = None, *,
                       output_size: tuple[float, float] | None = None, safe_area_ratio: float = 1.0,
                       transient: tuple | None = None) -> None:
        raise NotImplementedError

    def binding_target(self, value: str) -> GuiSceneNode | None:
        normalized = "/" + value.replace("\\", "/").strip("/")
        return self.root if normalized == "/" else self.nodes_by_path.get(normalized)
