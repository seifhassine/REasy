"""Dependency-aware semantic scene expansion for GUI editing."""

from __future__ import annotations

import copy
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from utils.resource_file_utils import ResourceDataLoader, resource_path_with_version

from .codec import parse_gui, parse_gui_file
from .errors import GuiFormatError, GuiSceneError
from .model import (
    ZERO_GUID,
    ChangeRecorder,
    GuiAnimation,
    GuiBinding,
    GuiDocument,
    GuiObject,
    GuiProperty,
    GuiSymbol,
    apply_property,
    iter_clip_nodes,
    resolve_properties,
)
from .profiles import GuiFormatProfile, gui_profile


def normalize_gui_resource_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip().lstrip("@/").casefold()
    for marker in ("natives/x64/", "natives/stm/"):
        index = normalized.find(marker)
        if index >= 0:
            normalized = normalized[index + len(marker) :]
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
    parent: "GuiSceneNode | None" = None
    children: list["GuiSceneNode"] = field(default_factory=list)
    properties: dict[str, object] = field(default_factory=dict)
    render_properties: dict[str, object] = field(default_factory=dict)
    local_position: tuple[float, float] = (0.0, 0.0)
    world_position: tuple[float, float] = (0.0, 0.0)
    size: tuple[float, float] = (80.0, 30.0)
    anchor: tuple[float, float] = (0.0, 0.0)
    world_transform: tuple[float, float, float, float, float, float] = (
        1.0, 0.0, 0.0, 1.0, 0.0, 0.0,
    )
    world_matrix: "Matrix4" | None = None
    effective_visible: bool = True
    color_scale: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    color_offset: tuple[float, float, float] = (0.0, 0.0, 0.0)
    saturation: float = 1.0

    @property
    def visible(self) -> bool:
        return self.effective_visible

    def walk(self) -> Iterator["GuiSceneNode"]:
        yield self
        for child in self.children:
            yield from child.walk()

@dataclass(slots=True)
class GuiLocalization:
    """The root-owned counterparts created for one imported scene branch."""

    object: GuiObject
    symbol: GuiSymbol | None
    copies: dict[int, object] = field(default_factory=dict)

    def copy_of(self, value):
        return self.copies.get(id(value), value)


class GuiWorkspace:
    """One GUI and its imported GUI definitions, loaded exactly once."""

    def __init__(
        self,
        resources: list[GuiResource],
        root_key: str,
        profile: GuiFormatProfile,
    ) -> None:
        self.resources = {item.key: item for item in resources}
        self.root_key = root_key
        self.profile = profile
        self.missing_dependencies: dict[str, str] = {}
        self._symbol_cache: dict[tuple[str, uuid.UUID], GuiSymbolReference | None] = {}

    @classmethod
    def from_document(
        cls,
        resource_path: str,
        document: GuiDocument,
        loader: ResourceDataLoader | None = None,
        *,
        profile: GuiFormatProfile | None = None,
    ) -> "GuiWorkspace":
        selected = profile or gui_profile(document.version)
        root_key = normalize_gui_resource_path(resource_path)
        resources = [GuiResource(root_key, document.source, document)]
        loaded = {root_key}
        missing: dict[str, str] = {}
        queue = [resources[0]]
        while queue and loader is not None:
            owner = queue.pop(0)
            for path in owner.document.imported_gui_paths:
                key = normalize_gui_resource_path(path)
                if key in loaded or key in missing:
                    continue
                versioned = resource_path_with_version(path, "gui", selected.version)
                resolved = loader(versioned) or loader(path)
                if resolved is None:
                    missing[key] = f"{owner.key} imports unresolved {path!r}"
                    continue
                source, data = resolved
                try:
                    dependency = parse_gui(bytes(data), str(source), profile=selected)
                except GuiFormatError as exc:
                    missing[key] = str(exc)
                    continue
                resource = GuiResource(key, str(source), dependency)
                resources.append(resource)
                queue.append(resource)
                loaded.add(key)
        result = cls(resources, root_key, selected)
        result.missing_dependencies = missing
        return result

    @classmethod
    def from_root(
        cls,
        root: str | Path,
        *,
        profile: GuiFormatProfile | None = None,
    ) -> "GuiWorkspace":
        root = Path(root)
        resources = []
        selected = profile
        for path in sorted(root.rglob("*.gui.*")):
            try:
                document = parse_gui_file(path, profile=selected)
                key = normalize_gui_resource_path(path.relative_to(root).as_posix())
            except (GuiFormatError, GuiSceneError):
                raise
            document_profile = gui_profile(document.version)
            if selected is None:
                selected = document_profile
            elif document_profile != selected:
                raise GuiSceneError(
                    f"mixed GUI profiles below {root}: "
                    f"{selected.name!r} and {document_profile.name!r}"
                )
            resources.append(GuiResource(key, str(path), document))
        if not resources:
            raise GuiSceneError(f"no supported GUI files below {root}")
        assert selected is not None
        return cls(resources, resources[0].key, selected)

    def _resolve_symbol(
        self,
        resource_key: str,
        guid: uuid.UUID,
        visiting: set[str],
    ) -> GuiSymbolReference | None:
        if guid == ZERO_GUID or resource_key in visiting:
            return None
        visiting = {*visiting, resource_key}
        resource = self.resources[resource_key]
        symbol = resource.document.symbol(guid)
        if symbol is not None:
            return GuiSymbolReference(resource, symbol)
        for dependency in resource.document.imported_gui_paths:
            key = normalize_gui_resource_path(dependency)
            if key not in self.resources:
                continue
            result = self._resolve_symbol(key, guid, visiting)
            if result is not None:
                return result
        return None

    def resolve_symbol(self, resource_key: str, guid: uuid.UUID) -> GuiSymbolReference | None:
        cache_key = resource_key, guid
        if cache_key not in self._symbol_cache:
            self._symbol_cache[cache_key] = self._resolve_symbol(resource_key, guid, set())
        return self._symbol_cache[cache_key]

    def invalidate(self) -> None:
        """Drop ownership-sensitive lookups after a semantic graph edit."""

        self._symbol_cache.clear()

    def instantiate(self, resource_path: str | None = None) -> "GuiScene":
        key = normalize_gui_resource_path(resource_path) if resource_path else self.root_key
        try:
            resource = self.resources[key]
        except KeyError as exc:
            raise GuiSceneError(f"GUI resource {key!r} is not loaded") from exc
        root_object = resource.document.root_object
        if root_object is None:
            raise GuiSceneError(f"{key} has no root object")
        paths: set[str] = set()

        def build(
            owner: GuiResource,
            obj: GuiObject,
            parent: GuiSceneNode | None,
            stack: tuple[tuple[str, uuid.UUID], ...],
        ) -> GuiSceneNode:
            prototype = self.resolve_symbol(owner.key, obj.prototype_guid)
            base = "/" if parent is None else f"{parent.path.rstrip('/')}/{obj.name}"
            path = base
            suffix = 2
            while path in paths:
                path = f"{base}#{suffix}"
                suffix += 1
            paths.add(path)
            node = GuiSceneNode(path, owner, obj, prototype, parent)
            if prototype is None:
                return node
            identity = prototype.resource.key, prototype.symbol.guid
            if identity in stack:
                raise GuiSceneError(f"cyclic symbol expansion at {prototype.symbol.name!r}")
            node.children = [
                build(prototype.resource, child, node, (*stack, identity))
                for child in prototype.symbol.objects
            ]
            return node

        root = build(resource, root_object, None, ())
        scene = self.profile.adapter.create_scene(self, resource, root)
        scene.refresh()
        scene.apply_bindings()
        return scene


class GuiScene(ABC):
    def __init__(self, workspace: GuiWorkspace, resource: GuiResource, root: GuiSceneNode) -> None:
        self.workspace = workspace
        self.resource = resource
        self.root = root
        self.nodes = list(root.walk())
        self.nodes_by_path = {item.path: item for item in self.nodes}
        self._preview_overrides: dict[str, dict[str, object]] = {}
        self._binding_targets: list[tuple[GuiBinding, GuiSceneNode]] = []
        self._bindings_by_node: dict[int, list[GuiBinding]] = {}
        self.unresolved_bindings: list[GuiBinding] = []
        for binding in resource.document.bindings:
            target = self.binding_target(binding.target_path)
            if target is None:
                self.unresolved_bindings.append(binding)
                continue
            if binding.target_type != target.object.type_name:
                raise GuiSceneError(
                    f"binding {binding.target_path!r} expects {binding.target_type}, "
                    f"found {target.object.type_name}"
                )
            base_types = {
                item.type
                for item in target.object.properties
                if item.name == binding.property.name
            }
            base_types.update(
                item.property.type
                for item in self._bindings_by_node.get(id(target), ())
                if item.property.name == binding.property.name
            )
            if base_types and base_types != {binding.property.type}:
                raise GuiSceneError(
                    f"binding {binding.target_path!r}.{binding.property.name} "
                    "does not match the target property type"
                )
            self._binding_targets.append((binding, target))
            self._bindings_by_node.setdefault(id(target), []).append(binding)

    @property
    @abstractmethod
    def screen_size(self) -> tuple[float, float]:
        raise NotImplementedError

    @abstractmethod
    def _invalidate_runtime(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def native_children(
        self,
        node: GuiSceneNode,
        properties_by_path: dict[str, dict[str, object]] | None = None,
    ) -> tuple[GuiSceneNode, ...]:
        raise NotImplementedError

    def refresh(self, node: GuiSceneNode | None = None) -> None:
        self._invalidate_runtime()
        start = node or self.root

        def update(item: GuiSceneNode) -> None:
            item.properties = resolve_properties(item.object.properties)
            for child in item.children:
                update(child)

        update(start)
        self.update_preview(self._preview_overrides)

    def apply_bindings(self) -> None:
        self._invalidate_runtime()
        for binding, target in self._binding_targets:
            target.properties[binding.property.name] = apply_property(
                target.properties.get(binding.property.name),
                binding.property,
            )
        self.update_preview(self._preview_overrides)

    def bindings_for(
        self,
        node: GuiSceneNode,
        name: str | None = None,
    ) -> tuple[GuiBinding, ...]:
        bindings = self._bindings_by_node.get(id(node), ())
        if name is None:
            return tuple(bindings)
        return tuple(item for item in bindings if item.property.name == name)

    def property_records(self, node: GuiSceneNode, name: str) -> list[GuiProperty]:
        records = [item for item in node.object.properties if item.name == name]
        records.extend(item.property for item in self.bindings_for(node, name))
        return records

    def editable_property_records(
        self,
        node: GuiSceneNode,
        name: str,
    ) -> list[GuiProperty]:
        records = []
        if node.resource.document is self.resource.document:
            records.extend(item for item in node.object.properties if item.name == name)
        records.extend(item.property for item in self.bindings_for(node, name))
        return records

    def localize_object(
        self,
        recorder: ChangeRecorder,
        node: GuiSceneNode,
    ) -> GuiLocalization:
        """Copy the imported prototype chain that owns ``node`` into the root GUI."""

        return self._localize_branch(recorder, node, include_prototype=False)

    def localize_prototype(
        self,
        recorder: ChangeRecorder,
        node: GuiSceneNode,
    ) -> GuiLocalization:
        """Copy the imported prototype and animation table instantiated by ``node``."""

        return self._localize_branch(recorder, node, include_prototype=True)

    def _localize_branch(
        self,
        recorder: ChangeRecorder,
        node: GuiSceneNode,
        *,
        include_prototype: bool,
    ) -> GuiLocalization:
        document = self.resource.document
        lineage: list[GuiSceneNode] = []
        current: GuiSceneNode | None = node
        while current is not None:
            lineage.append(current)
            current = current.parent
        lineage.reverse()

        owners = lineage if include_prototype else lineage[:-1]
        localized_object = document.root_object
        if localized_object is None:
            raise GuiSceneError("cannot localize a GUI without a root object")

        symbols = list(document.symbols)
        objects = list(document.objects)
        animations = list(document.animations)
        used_symbols = {item.guid for item in symbols}
        used_objects = {item.instance_guid for item in objects}
        used_animations = {item.guid for item in animations}
        namespace = localized_object.instance_guid
        copies: dict[int, object] = {}
        localized_symbol: GuiSymbol | None = None

        for index, owner in enumerate(owners):
            reference = owner.prototype
            if reference is None:
                raise GuiSceneError(f"{owner.path} has no prototype to localize")
            if reference.resource.document is document:
                localized_symbol = reference.symbol
            else:
                seed = f"{reference.resource.key}:{owner.path}:{reference.symbol.guid}"
                localized_symbol, memo, extra_animations = _clone_symbol(
                    reference.symbol,
                    namespace,
                    seed,
                    used_symbols,
                    used_objects,
                    used_animations,
                )
                copies.update(memo)
                recorder.set(localized_object, "prototype_guid", localized_symbol.guid)
                symbols.append(localized_symbol)
                objects.extend(localized_symbol.objects)
                animations.extend(localized_symbol.animations)
                animations.extend(extra_animations)

            if index + 1 < len(lineage):
                original = lineage[index + 1].object
                localized_object = copies.get(id(original), original)

        recorder.set(document, "symbols", symbols)
        recorder.set(document, "objects", objects)
        recorder.set(document, "animations", animations)
        return GuiLocalization(
            object=localized_object,
            symbol=localized_symbol if include_prototype else None,
            copies=copies,
        )

    def can_move(self, node: GuiSceneNode) -> bool:
        if (
            node.resource.document is not self.resource.document
            and self.property_records(node, "Position")
        ):
            return True
        records = self.editable_property_records(node, "Position")
        if any(item.component_mask == -1 for item in records):
            return True
        mask = 0
        for item in records:
            mask |= item.component_mask
        return mask & 0b11 == 0b11

    @abstractmethod
    def update_preview(
        self,
        overrides: dict[str, dict[str, object]] | None = None,
        *,
        output_size: tuple[float, float] | None = None,
        safe_area_ratio: float = 1.0,
        transient: tuple[GuiSceneNode, float, float] | None = None,
    ) -> None:
        raise NotImplementedError

    @staticmethod
    def local_position_for_scene_point(
        node: GuiSceneNode,
        x: float,
        y: float,
    ) -> tuple[float, float]:
        parent = (
            node.parent.world_transform
            if node.parent
            else (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        )
        a, b, c, d, tx, ty = parent
        determinant = a * d - b * c
        if abs(determinant) < 1e-8:
            raise GuiSceneError("cannot move a node below a singular parent transform")
        x, y = x - tx, y - ty
        return (d * x - c * y) / determinant, (-b * x + a * y) / determinant

    def binding_target(self, value: str) -> GuiSceneNode | None:
        normalized = "/" + value.replace("\\", "/").strip("/")
        if normalized == "/":
            return self.root
        return self.nodes_by_path.get(normalized)


def _clone_symbol(
    source: GuiSymbol,
    namespace: uuid.UUID,
    seed: str,
    used_symbols: set[uuid.UUID],
    used_objects: set[uuid.UUID],
    used_animations: set[uuid.UUID],
) -> tuple[GuiSymbol, dict[int, object], list[GuiAnimation]]:
    """Deep-copy one definition and give every newly owned identity a fresh GUID."""

    memo: dict[int, object] = {}
    clone = copy.deepcopy(source, memo)
    clone.guid = _fresh_guid(namespace, used_symbols, f"{seed}:symbol")

    source_animations = list(source.animations)
    cursor = 0
    while cursor < len(source_animations):
        transition = source_animations[cursor].transition
        if transition is not None and transition not in source_animations:
            source_animations.append(transition)
        cursor += 1
    cloned_animations = [memo[id(item)] for item in source_animations]
    extra_animations = cloned_animations[len(clone.animations) :]

    guid_map: dict[bytes, bytes] = {}
    for index, (original, cloned) in enumerate(zip(source.objects, clone.objects)):
        new_guid = _fresh_guid(
            namespace,
            used_objects,
            f"{seed}:object:{index}:{original.instance_guid}",
        )
        guid_map[original.instance_guid.bytes_le] = new_guid.bytes_le
        cloned.instance_guid = new_guid
    for index, (original, cloned) in enumerate(
        zip(source_animations, cloned_animations)
    ):
        new_guid = _fresh_guid(
            namespace,
            used_animations,
            f"{seed}:animation:{index}:{original.guid}",
        )
        old = original.guid.bytes_le
        replacement = new_guid.bytes_le
        if old in guid_map and guid_map[old] != replacement:
            raise GuiSceneError(f"ambiguous object/animation GUID {original.guid}")
        guid_map[old] = replacement
        cloned.guid = new_guid
    for animation in cloned_animations:
        for clip_node in iter_clip_nodes(animation.clip.root):
            clip_node.root_guid = guid_map.get(clip_node.root_guid, clip_node.root_guid)
            clip_node.extra_guid = guid_map.get(clip_node.extra_guid, clip_node.extra_guid)
    return clone, memo, extra_animations


def _fresh_guid(
    namespace: uuid.UUID,
    used: set[uuid.UUID],
    seed: str,
) -> uuid.UUID:
    attempt = 0
    while True:
        candidate = uuid.uuid5(namespace, f"{seed}:{attempt}")
        if candidate not in used and candidate != ZERO_GUID:
            used.add(candidate)
            return candidate
        attempt += 1
