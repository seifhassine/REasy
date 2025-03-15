import json
import os
import copy

class TypeRegistryPatcher:
    def __init__(self, registry_path):
        self.registry_path = registry_path
        self.cache_path = self._get_cache_path(registry_path)
        self.cache = {}
        self._load_cache()
        
    def _get_cache_path(self, registry_path):
        dir_name = os.path.dirname(registry_path)
        base_name = os.path.basename(registry_path)
        cache_dir = os.path.join(dir_name, ".cache")
        
        if not os.path.exists(cache_dir):
            os.makedirs(cache_dir, exist_ok=True)
        
        return os.path.join(cache_dir, f"{base_name}.patch_cache")
    
    def _load_cache(self):
        if os.path.exists(self.cache_path):
            try:
                with open(self.cache_path, 'r') as f:
                    self.cache = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                print(f"Error loading patch cache: {e}")
                self.cache = {}
        
    def _save_cache(self):
        try:
            cache_dir = os.path.dirname(self.cache_path)
            if not os.path.exists(cache_dir):
                os.makedirs(cache_dir, exist_ok=True)
                
            with open(self.cache_path, 'w') as f:
                json.dump(self.cache, f)
        except IOError as e:
            print(f"Error saving patch cache: {e}")
    
    def _get_file_timestamp(self, file_path):
        return os.path.getmtime(file_path)
    
    def patch_registry(self, registry):
        current_timestamp = self._get_file_timestamp(self.registry_path)
        cache_key = str(current_timestamp)
        
        if cache_key in self.cache:
            print(f"Using cached patches for {self.registry_path}")
            return self.cache[cache_key]
        
        print(f"Creating new patches for {self.registry_path}")
        patched_registry = copy.deepcopy(registry)
        
        for type_key, type_info in patched_registry.items():
            if "fields" not in type_info:
                continue
                
            self._patch_fields(type_info["fields"])
        
        self.cache.clear()
        self.cache[cache_key] = patched_registry
        self._save_cache()
        
        return patched_registry
    
    def _patch_fields(self, fields):
        seen_names = {}
        
        for i, field in enumerate(fields):
            name = field.get("name")
            if not name:
                continue
                
            if name in seen_names:
                count = seen_names[name] + 1
                seen_names[name] = count
                
                new_name = f"{name}_{count}"
                print(f"Renamed duplicate field '{name}' to '{new_name}'")
                
                field["name"] = new_name
            else:
                seen_names[name] = 1
