import json
from .type_registry_patcher import TypeRegistryPatcher


class TypeRegistry:
    def __init__(self, json_path: str):
        self.json_path = json_path
        patcher = TypeRegistryPatcher(json_path)
        
        with open(json_path, "r") as f:
            raw_registry = json.load(f)
        
        self.registry = patcher.patch_registry(raw_registry)

    def get_type_info(self, type_id: int) -> dict:
        """
        Look up the type info for a given type_id.
        We convert type_id to lowercase hex without the "0x" prefix.
        We try both the unpadded and 8-digit padded keys.
        Returns a dict (or None if not found).
        """
        hex_key = format(type_id, "x")
        if hex_key in self.registry:
            return self.registry[hex_key]
        return None

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
