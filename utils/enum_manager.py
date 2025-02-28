import json
import os

class EnumManager:
    """Manages enum definitions loaded from JSON"""
    _instance = None
    
    @staticmethod
    def instance():
        """Get singleton instance"""
        if not EnumManager._instance:
            EnumManager._instance = EnumManager()
        return EnumManager._instance
        
    def __init__(self):
        self.enums = {}
        self._loaded = False
        
    def load_enums(self):
        """Load enum definitions from JSON file"""
        if self._loaded:
            return
            
        try:
            enum_path = os.path.join("resources", "data", "enums.json")
            with open(enum_path, 'r') as f:
                self.enums = json.load(f)
            self._loaded = True
            print(f"Loaded {len(self.enums)} enum types")
        except Exception as e:
            print(f"Error loading enums: {str(e)}")
            self.enums = {}
            
    def get_enum_values(self, enum_type):
        """Get enum values for a specific type"""
        if not self._loaded:
            self.load_enums()
            
        return self.enums.get(enum_type, [])
