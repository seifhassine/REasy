import json
lazy from .type_registry_patcher import TypeRegistryPatcher


class TypeRegistry:
    def __init__(self, json_path: str):
        self.json_path = json_path
        patcher = TypeRegistryPatcher(json_path)

        cached_registry = patcher.get_cached_registry()
        if cached_registry is not None:
            self.registry = cached_registry
        else:
            with open(json_path, "r") as f:
                raw_registry = json.load(f)

            self.registry = patcher.patch_registry(raw_registry)
        
        self._name_to_info = {}
        self._name_to_type_id = {}
        for type_key, info in self.registry.items():
            if isinstance(info, dict) and "name" in info:
                name = info["name"]
                if name in self._name_to_info:
                    continue
                self._name_to_info[name] = info
                try:
                    self._name_to_type_id[name] = int(type_key, 16)
                except (TypeError, ValueError):
                    self._name_to_type_id[name] = None

        self._type_id_cache = {}

    def _lookup_type_info(self, type_id: int):
        hex_key = format(type_id, "x")
        info = self.registry.get(hex_key)
        if info is None and len(hex_key) < 8:
            info = self.registry.get(hex_key.zfill(8))
        return info

    def get_type_info(self, type_id: int) -> dict:
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
        info = self._name_to_info.get(type_name)
        if info is None:
            return None, None
        return info, self._name_to_type_id.get(type_name)

    def getTypeParents(self, type_name: str) -> list:
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