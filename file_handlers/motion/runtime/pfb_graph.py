from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .model import MotionRuntimeContextError


@dataclass(frozen=True, slots=True)
class PfbComponent:
    instance_id: int
    object_table_id: int
    owner_object_id: int
    type_name: str


class PfbRuntimeGraph:
    """Small, reusable ownership/reference index over an already parsed PFB."""

    def __init__(self, parsed):
        if not getattr(parsed, "is_pfb", False):
            raise MotionRuntimeContextError("runtime scene is not a parsed PFB")
        if getattr(parsed, "type_registry", None) is None:
            raise MotionRuntimeContextError("parsed PFB has no type registry")
        self.parsed = parsed
        self.registry = parsed.type_registry
        self.components: dict[int, PfbComponent] = {}
        self.object_names: dict[int, str] = {}
        self.components_by_object: dict[int, list[int]] = {}
        self._type_names: dict[int, str] = {}
        self._parents: dict[str, frozenset[str]] = {}
        self._references: dict[tuple[int, int, int], list[int]] = {}
        self._index_objects()
        self._index_references()

    def _index_objects(self) -> None:
        table = getattr(self.parsed, "object_table", ())
        for game_object in getattr(self.parsed, "gameobjects", ()) or ():
            object_id = int(getattr(game_object, "id", -1))
            if not 0 <= object_id < len(table):
                continue
            object_instance = int(table[object_id])
            self.object_names[object_id] = self._object_name(object_instance, object_id)
            owned = self.components_by_object.setdefault(object_id, [])
            count = int(getattr(game_object, "component_count", 0) or 0)
            for offset in range(1, count + 1):
                object_table_id = object_id + offset
                if not 0 <= object_table_id < len(table):
                    continue
                instance_id = int(table[object_table_id])
                component = PfbComponent(
                    instance_id,
                    object_table_id,
                    object_id,
                    self.type_name(instance_id),
                )
                self.components[instance_id] = component
                owned.append(instance_id)

    def _index_references(self) -> None:
        for item in getattr(self.parsed, "gameobject_ref_infos", ()) or ():
            key = (
                int(getattr(item, "object_id", -1)),
                int(getattr(item, "property_id", -1)),
                int(getattr(item, "array_index", 0)),
            )
            self._references.setdefault(key, []).append(
                int(getattr(item, "target_id", -1))
            )

    def _object_name(self, instance_id: int, object_id: int) -> str:
        fields = self.fields(instance_id)
        value = getattr(fields.get("Name"), "value", "")
        if isinstance(value, str):
            value = value.strip().rstrip("\0")
        return value or f"GameObject {object_id}"

    def fields(self, instance_id: int) -> Mapping[str, object]:
        fields = getattr(self.parsed, "parsed_elements", {}).get(instance_id, {})
        return fields if isinstance(fields, Mapping) else {}

    def type_name(self, instance_id: int) -> str:
        cached = self._type_names.get(instance_id)
        if cached is not None:
            return cached
        infos = getattr(self.parsed, "instance_infos", ())
        if not 0 <= instance_id < len(infos):
            return ""
        type_id = int(getattr(infos[instance_id], "type_id", 0) or 0)
        info = self.registry.get_type_info(type_id) if type_id else None
        name = str((info or {}).get("name", "") or "")
        self._type_names[instance_id] = name
        return name

    def is_a(self, type_name: str, base_type: str) -> bool:
        if type_name == base_type:
            return True
        parents = self._parents.get(type_name)
        if parents is None:
            parents = frozenset(self.registry.getTypeParents(type_name) or ())
            self._parents[type_name] = parents
        return base_type in parents

    def instances_of(self, base_type: str) -> tuple[int, ...]:
        return tuple(
            instance_id
            for instance_id, component in self.components.items()
            if self.is_a(component.type_name, base_type)
        )

    def owner_of(self, component_instance_id: int) -> int | None:
        component = self.components.get(component_instance_id)
        return component.owner_object_id if component is not None else None

    def reference_target(
        self,
        component_instance_id: int,
        property_id: int,
        array_index: int = 0,
    ) -> int | None:
        component = self.components.get(component_instance_id)
        if component is None:
            return None
        matches = self._references.get(
            (component.object_table_id, property_id, array_index),
            (),
        )
        if len(matches) > 1:
            raise MotionRuntimeContextError(
                f"component {component_instance_id} property 0x{property_id:X} "
                f"has {len(matches)} PFB GameObject references"
            )
        if not matches:
            return None
        target = matches[0]
        return target if target in self.object_names else None
