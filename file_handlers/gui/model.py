"""Semantic GUIR document model.

"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Protocol

from file_handlers.clip.enums import PropertyType
from file_handlers.motion.mot_clip.model import (
    ClipNode,
    ClipProperty,
    CompactMotClip,
)


GUI_MAGIC = b"GUIR"
ZERO_GUID = uuid.UUID(int=0)

COMPONENT_NAMES: dict[PropertyType, tuple[str, ...]] = {
    PropertyType.QUATERNION: ("x", "y", "z", "w"),
    PropertyType.VEC2: ("x", "y"),
    PropertyType.VEC3: ("x", "y", "z"),
    PropertyType.VEC4: ("x", "y", "z", "w"),
    PropertyType.COLOR: ("r", "g", "b", "a"),
    PropertyType.RANGE: ("min", "max"),
    PropertyType.FLOAT2: ("x", "y"),
    PropertyType.FLOAT3: ("x", "y", "z"),
    PropertyType.FLOAT4: ("x", "y", "z", "w"),
    PropertyType.RANGEI: ("min", "max"),
    PropertyType.POINT: ("x", "y"),
    PropertyType.SIZE: ("width", "height"),
    PropertyType.UINT2: ("x", "y"),
    PropertyType.UINT3: ("x", "y", "z"),
    PropertyType.UINT4: ("x", "y", "z", "w"),
    PropertyType.INT2: ("x", "y"),
    PropertyType.INT3: ("x", "y", "z"),
    PropertyType.INT4: ("x", "y", "z", "w"),
    PropertyType.RECT: ("left", "top", "right", "bottom"),
}


GuiValue = bool | int | float | str | uuid.UUID | list[Any] | tuple[Any, ...] | None


@dataclass(slots=True, eq=False)
class GuiProperty:
    name: str
    type: PropertyType
    value: GuiValue
    component_mask: int = -1

    @property
    def components(self) -> tuple[str, ...]:
        names = COMPONENT_NAMES.get(self.type, ())
        if self.component_mask == -1:
            return names
        return tuple(
            name for index, name in enumerate(names)
            if self.component_mask & (1 << index)
        )


@dataclass(slots=True)
class GuiTextureEntry:
    sequence: int
    pattern: int
    bounds: tuple[float, float, float, float]
    tagged: bool = False


@dataclass(slots=True)
class GuiTextureSet:
    entries: list[GuiTextureEntry] = field(default_factory=list)


NINE_SLICE_CELL_NAMES = (
    "top_left", "top_center", "top_right",
    "middle_left", "middle_center", "middle_right",
    "bottom_left", "bottom_center", "bottom_right",
)


@dataclass(slots=True)
class GuiNineSliceCell:
    name: str
    sequence: int
    pattern: int
    repeat_size: tuple[float, float]


@dataclass(slots=True)
class GuiNineSlice:
    borders: tuple[float, float, float, float]
    cells: list[GuiNineSliceCell] = field(default_factory=list)


GuiSpecialData = GuiTextureSet | GuiNineSlice | None


@dataclass(slots=True, eq=False)
class GuiObject:
    instance_guid: uuid.UUID
    prototype_guid: uuid.UUID
    name: str
    type_name: str
    properties: list[GuiProperty] = field(default_factory=list)
    animation_defaults: list[GuiProperty] = field(default_factory=list)
    special_data: GuiSpecialData = None

    def property(self, name: str) -> GuiProperty | None:
        return next((item for item in reversed(self.properties) if item.name == name), None)


@dataclass(slots=True, eq=False)
class GuiAnimation:
    guid: uuid.UUID
    name: str
    loop: bool
    clip: CompactMotClip
    transition: "GuiAnimation | None" = None


@dataclass(slots=True, eq=False)
class GuiSymbol:
    guid: uuid.UUID
    name: str
    type_name: str
    objects: list[GuiObject] = field(default_factory=list)
    animations: list[GuiAnimation] = field(default_factory=list)

    def animation_states(self) -> dict[str, list[GuiAnimation]]:
        result: dict[str, list[GuiAnimation]] = {}
        for animation in self.animations:
            result.setdefault(animation.name, []).append(animation)
        return result


@dataclass(slots=True, eq=False)
class GuiBinding:
    target_path: str
    target_type: str
    property: GuiProperty


class ChangeRecorder(Protocol):
    def set(self, target: object, attribute: str, value: Any) -> None: ...


def _copy(value: Any) -> Any:
    return copy.deepcopy(value)


def apply_property(current: GuiValue, prop: GuiProperty) -> GuiValue:
    """Apply a serialized component mask to the current semantic value."""

    value = _copy(prop.value)
    names = COMPONENT_NAMES.get(prop.type, ())
    if prop.component_mask == -1 or not names or current is None:
        return value
    result = list(current)
    source = list(value)
    for index in range(min(len(names), len(result), len(source))):
        if prop.component_mask & (1 << index):
            result[index] = source[index]
    return result


def resolve_properties(records: Iterable[GuiProperty]) -> dict[str, GuiValue]:
    result: dict[str, GuiValue] = {}
    for prop in records:
        result[prop.name] = apply_property(result.get(prop.name), prop)
    return result


def iter_clip_nodes(root: ClipNode) -> Iterator[ClipNode]:
    yield root
    for child in root.children:
        yield from iter_clip_nodes(child)


def iter_clip_properties(properties: Iterable[ClipProperty]) -> Iterator[ClipProperty]:
    for prop in properties:
        yield prop
        yield from iter_clip_properties(prop.children)


@dataclass(slots=True, eq=False)
class GuiDocument:
    version: int
    root_object: GuiObject | None
    symbols: list[GuiSymbol]
    bindings: list[GuiBinding] = field(default_factory=list)
    imported_gui_paths: list[str] = field(default_factory=list)
    asset_paths: list[str] = field(default_factory=list)
    source: str = "<bytes>"
    objects: list[GuiObject] = field(default_factory=list, repr=False)
    animations: list[GuiAnimation] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self.objects = self.objects or self._owned_objects()
        self.animations = self.animations or self._owned_animations()

    def _owned_objects(self) -> list[GuiObject]:
        result: list[GuiObject] = []
        seen: set[int] = set()
        values = [
            self.root_object,
            *(item for symbol in self.symbols for item in symbol.objects),
        ]
        for obj in values:
            if obj is not None and id(obj) not in seen:
                seen.add(id(obj))
                result.append(obj)
        return result

    def _owned_animations(self) -> list[GuiAnimation]:
        result: list[GuiAnimation] = []
        seen: set[int] = set()
        for animation in (item for symbol in self.symbols for item in symbol.animations):
            if id(animation) not in seen:
                seen.add(id(animation))
                result.append(animation)
        return result

    def symbol(self, guid: uuid.UUID) -> GuiSymbol | None:
        return next((item for item in self.symbols if item.guid == guid), None)

    def validate_relationships(self) -> None:
        objects = self.objects
        animations = self.animations
        if len({id(item) for item in objects}) != len(objects):
            raise ValueError("GUI object declarations must not contain duplicates")
        if len({id(item) for item in animations}) != len(animations):
            raise ValueError("GUI animation declarations must not contain duplicates")
        if any(item not in objects for item in self._owned_objects()):
            raise ValueError("every owned GUI object must have a declaration")
        if any(item not in animations for item in self._owned_animations()):
            raise ValueError("every owned GUI animation must have a declaration")
        if len({item.instance_guid for item in objects}) != len(objects):
            raise ValueError("GUI object instance GUIDs must be unique")
        if len({item.guid for item in self.symbols}) != len(self.symbols):
            raise ValueError("GUI symbol GUIDs must be unique")
        if len({item.guid for item in animations}) != len(animations):
            raise ValueError("GUI animation GUIDs must be unique")
        if any(item.transition not in animations for item in animations if item.transition):
            raise ValueError("animation transitions must target this GUI document")
        for symbol in self.symbols:
            if len({id(item) for item in symbol.objects}) != len(symbol.objects):
                raise ValueError(f"symbol {symbol.name!r} contains a duplicate object")
            if len({id(item) for item in symbol.animations}) != len(symbol.animations):
                raise ValueError(f"symbol {symbol.name!r} contains a duplicate animation")


    def set_effective_records(
        self,
        recorder: ChangeRecorder,
        candidates: Iterable[GuiProperty],
        name: str,
        value: GuiValue,
    ) -> None:
        """Edit the authored layers that produce one effective scene value."""

        records = [item for item in candidates if item.name == name]
        if not records:
            raise ValueError(f"there is no editable {name} property layer")
        desired = list(value) if isinstance(value, (list, tuple)) else value
        names = COMPONENT_NAMES.get(records[-1].type, ())
        remaining = (1 << len(names)) - 1 if names else 1
        for prop in reversed(records):
            mask = remaining if prop.component_mask == -1 else prop.component_mask & remaining
            if not mask:
                continue
            if names and isinstance(desired, list):
                updated = list(prop.value)
                for index in range(min(len(updated), len(desired), len(names))):
                    if mask & (1 << index):
                        updated[index] = desired[index]
                recorder.set(prop, "value", updated)
            else:
                recorder.set(prop, "value", _copy(value))
            remaining &= ~mask
            if not remaining:
                break

    def rename_object(
        self,
        recorder: ChangeRecorder,
        obj: GuiObject,
        name: str,
    ) -> None:
        if not name or "/" in name or "\\" in name:
            raise ValueError("object names cannot be empty or contain path separators")
        old = obj.name
        binding_targets = self._local_binding_targets()
        recorder.set(obj, "name", name)
        for prop in (*obj.properties, *obj.animation_defaults):
            if prop.name == "Name" and prop.value == old:
                recorder.set(prop, "value", name)
        target = obj.instance_guid.bytes_le
        for animation in self.animations:
            for node in iter_clip_nodes(animation.clip.root):
                if node.root_guid == target or node.extra_guid == target:
                    recorder.set(node, "name", name)
        for binding, chain in binding_targets:
            if obj not in chain:
                continue
            parts = binding.target_path.replace("\\", "/").split("/")
            object_names = [item.name for item in chain if item is not self.root_object]
            cursor = 0
            for index, part in enumerate(parts):
                if cursor < len(object_names) and part == object_names[cursor]:
                    if chain[cursor + 1] is obj:
                        parts[index] = name
                    cursor += 1
            recorder.set(binding, "target_path", "/".join(parts))

    def _local_binding_targets(self) -> list[tuple[GuiBinding, tuple[GuiObject, ...]]]:
        """Resolve bindings through local prototype ownership without guessing."""

        if self.root_object is None:
            return []
        paths: dict[str, tuple[GuiObject, ...]] = {}

        def expand(
            current: GuiObject,
            path: str,
            chain: tuple[GuiObject, ...],
            symbols: frozenset[uuid.UUID],
        ) -> None:
            paths.setdefault(path.casefold(), chain)
            symbol = self.symbol(current.prototype_guid)
            if symbol is None or symbol.guid in symbols:
                return
            for child in symbol.objects:
                child_path = f"/{child.name}" if path == "/" else f"{path}/{child.name}"
                expand(child, child_path, (*chain, child), symbols | {symbol.guid})

        expand(self.root_object, "/", (self.root_object,), frozenset())
        result = []
        for binding in self.bindings:
            path = "/" + binding.target_path.replace("\\", "/").strip("/")
            chain = paths.get(path.casefold())
            if chain is not None:
                result.append((binding, chain))
        return result


    def summary(self) -> dict[str, int | str]:
        properties = [item for obj in self.objects for item in obj.properties]
        tracks = [
            prop
            for animation in self.animations
            for node in iter_clip_nodes(animation.clip.root)
            for prop in iter_clip_properties(node.properties)
        ]
        return {
            "source": self.source,
            "version": self.version,
            "symbols": len(self.symbols),
            "objects": len(self.objects),
            "animations": len(self.animations),
            "properties": len(properties),
            "animation_tracks": len(tracks),
            "keyframes": sum(len(item.keys) + (item.last_key is not None) for item in tracks),
            "bindings": len(self.bindings),
            "imported_gui_paths": len(self.imported_gui_paths),
            "asset_paths": len(self.asset_paths),
        }
