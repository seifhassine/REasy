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
        self._loaded_versions = set()
        self._game_version = "RE4"  
        self._enum_paths = {
            "RE4": "resources/data/enums/re4_enums.json",
            "RE2": "resources/data/enums/re2_enums.json",
            "RE2RT": "resources/data/enums/re2rt_enums.json",
            "RE8": "resources/data/enums/re8_enums.json",
            "RE3": "resources/data/enums/re3_enums.json",
            "RE7": "resources/data/enums/re7_enums.json",
            "RE7RT": "resources/data/enums/re7rt_enums.json",
            "MHWS": "resources/data/enums/mhws_enums.json",
            "DMC5": "resources/data/enums/dmc5_enums.json",
            "SF6": "resources/data/enums/sf6_enums.json",
            "O2": "resources/data/enums/o2_enums.json",
            "DD2": "resources/data/enums/dd2_enums.json",
        }
        
    @property
    def game_version(self):
        return self._game_version
    
    @game_version.setter
    def game_version(self, version):
        if version != self._game_version and version in self._enum_paths:
            self._game_version = version
   
    def _ensure_enums_loaded(self):
        """Ensure enums for the current game version are loaded"""
        if self._game_version in self._loaded_versions:
            return
            
        self.load_enums()
    
    def load_enums(self):
        """Load enum values from the appropriate game version file"""
        enum_path = self._enum_paths.get(self._game_version)
        if not enum_path:
            print(f"No enum file defined for game version: {self._game_version}")
            return
            
        try:
            if os.path.exists(enum_path):
                with open(enum_path, 'r') as f:
                    self.enums[self._game_version] = json.load(f)
                print(f"Loaded enums for game version {self._game_version} from {enum_path}")
                self._loaded_versions.add(self._game_version)
            else:
                print(f"Enum file not found: {enum_path}")
        except Exception as e:
            print(f"Error loading enum values: {str(e)}")
    
    def get_enum_values(self, enum_type):
        """Get enum values for a specific enum type"""
        self._ensure_enums_loaded()
        
        if not enum_type:
            return {}
        
        game_enums = self.enums.get(self._game_version, {})
        return game_enums.get(enum_type, {})
