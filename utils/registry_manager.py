import os
from .type_registry import TypeRegistry

class RegistryManager:
    """Manages global registry instances to avoid reloading JSON templates"""
    
    _instance = None
    
    @staticmethod
    def instance():
        """Get singleton instance"""
        if not RegistryManager._instance:
            RegistryManager._instance = RegistryManager()
        return RegistryManager._instance
    
    def __init__(self):
        self._registries = {}
        self._last_mod_times = {}
    
    def get_registry(self, json_path):
        """Get or create a TypeRegistry for the specified JSON path"""
        if not json_path or not os.path.exists(json_path):
            return None
        
        # Check if file was modified since we last loaded it
        needs_reload = self._file_needs_reload(json_path)
        
        # If registry exists and file hasn't changed, return it directly
        if json_path in self._registries and not needs_reload:
            return self._registries[json_path]
        
        # Otherwise load/reload the registry
        try:
            registry = TypeRegistry(json_path)
            self._registries[json_path] = registry
            self._last_mod_times[json_path] = os.path.getmtime(json_path)
            return registry
        except Exception as e:
            print(f"Error loading registry from {json_path}: {e}")
            return None
    
    def _file_needs_reload(self, file_path):
        """Check if file has been modified since last load"""
        # If we don't have the registry yet, we need to load it
        if file_path not in self._registries:
            return True
            
        try:
            # Get the current file modification time
            current_mod_time = os.path.getmtime(file_path)
            
            # If we haven't recorded a mod time or the file is newer, reload
            if (file_path not in self._last_mod_times or 
                current_mod_time > self._last_mod_times[file_path]):
                return True
        except Exception as e:
            # If we can't check the file time, assume we need to reload
            print(f"Error checking file modification time for {file_path}: {e}")
            return True
            
        return False
    
    def clear(self):
        """Clear all cached registries"""
        self._registries.clear()
        self._last_mod_times.clear()
