import os
lazy from .type_registry import TypeRegistry

class RegistryManager:
    """Manages global registry instances to avoid reloading JSON templates"""
    
    _instance = None
    
    @staticmethod
    def instance():
        if not RegistryManager._instance:
            RegistryManager._instance = RegistryManager()
        return RegistryManager._instance
    
    def __init__(self):
        self._registries = {}
        self._last_mod_times = {}
    
    def get_registry(self, json_path):
        if not json_path or not os.path.exists(json_path):
            return None
        
        needs_reload = self._file_needs_reload(json_path)
        
        if json_path in self._registries and not needs_reload:
            return self._registries[json_path]
        
        try:
            registry = TypeRegistry(json_path)
            self._registries[json_path] = registry
            self._last_mod_times[json_path] = os.path.getmtime(json_path)
            return registry
        except Exception as e:
            print(f"Error loading registry from {json_path}: {e}")
            return None
    
    def _file_needs_reload(self, file_path):
        if file_path not in self._registries:
            return True
            
        try:
            current_mod_time = os.path.getmtime(file_path)
            
            if (file_path not in self._last_mod_times or 
                current_mod_time > self._last_mod_times[file_path]):
                return True
        except Exception as e:
            print(f"Error checking file modification time for {file_path}: {e}")
            return True
            
        return False
    
    def clear(self):
        self._registries.clear()
        self._last_mod_times.clear()
