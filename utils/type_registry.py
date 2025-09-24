import json
from .type_registry_patcher import TypeRegistryPatcher


class TypeRegistry:
    def __init__(self, json_path: str):
        self.json_path = json_path
        patcher = TypeRegistryPatcher(json_path)
        
        with open(json_path, "r") as f:
            raw_registry = json.load(f)
        
        self.registry = patcher.patch_registry(raw_registry)
        
        self._name_to_info = {}
        for info in self.registry.values():
            if isinstance(info, dict) and "name" in info:
                self._name_to_info[info["name"]] = info

        self._type_id_cache = {}

    def _lookup_type_info(self, type_id: int):
        """Internal helper to resolve a type ID without touching the cache."""
        hex_key = format(type_id, "x")
        info = self.registry.get(hex_key)
        if info is None and len(hex_key) < 8:
            info = self.registry.get(hex_key.zfill(8))
        return info

    def get_type_info(self, type_id: int) -> dict:
        """
        Look up the type info for a given type_id.
        We convert type_id to lowercase hex without the "0x" prefix.
        We try both the unpadded and 8-digit padded keys.
        Returns a dict (or None if not found).
        """
        cache = self._type_id_cache
        if type_id in cache:
            return cache[type_id]

        info = self._lookup_type_info(type_id)
        cache[type_id] = info
        return info

    def pre_cache_types(self, type_ids):
        if not type_ids:
            return

        cache = self._type_id_cache
        lookup = self._lookup_type_info
        for type_id in type_ids:
            if type_id <= 0 or type_id in cache:
                continue
            cache[type_id] = lookup(type_id)

    def find_type_by_name(self, type_name: str) -> tuple:
        """
        Look up type info and ID by name.
        Returns a tuple of (type_info, type_id) or (None, None) if not found.
        """
        for type_key, info in self.registry.items():
            if info.get("name") == type_name:
                type_id = int(type_key, 16)
                return info, type_id
        return None, None

    def getTypeParents(self, type_name: str) -> list:
        """
        Return an ordered list of parent type names for the given type name.
        The list starts with the immediate parent and goes up the chain.
        Stops if a parent name cannot be resolved or a cycle is detected.
        """
        parents = []
        seen = set()
        current_name = type_name
        while True:
            info = self._name_to_info.get(current_name)
            if not info:
                break
            parent_name = info.get("parent")
            if not parent_name or not isinstance(parent_name, str):
                break
            if parent_name in seen:
                break
            parents.append(parent_name)
            seen.add(parent_name)
            current_name = parent_name
        return parents