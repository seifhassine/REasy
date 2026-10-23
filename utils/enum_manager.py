import json
from functools import lru_cache

lazy from utils.app_paths import resource_path
from utils.game_keys import game_key


_ENUM_PATHS = {
    "RE4": "resources/data/enums/re4_enums.json",
    "RE2": "resources/data/enums/re2_enums.json",
    "RE2RT": "resources/data/enums/re2rt_enums.json",
    "RE8": "resources/data/enums/re8_enums.json",
    "RE3": "resources/data/enums/re3_enums.json",
    "RE3RT": "resources/data/enums/re3rt_enums.json",
    "REResistance": "resources/data/enums/reresistance_enums.json",
    "RE7": "resources/data/enums/re7_enums.json",
    "RE7RT": "resources/data/enums/re7rt_enums.json",
    "MHWilds": "resources/data/enums/mhws_enums.json",
    "MHRise": "resources/data/enums/mhrise_enums.json",
    "MHST3": "resources/data/enums/mhst3_enums.json",
    "Pragmata": "resources/data/enums/pragmata_enums.json",
    "DMC5": "resources/data/enums/dmc5_enums.json",
    "SF6": "resources/data/enums/sf6_enums.json",
    "O2": "resources/data/enums/o2_enums.json",
    "OnimushaWOTS": "resources/data/enums/oniwots_enums.json",
    "DD2": "resources/data/enums/dd2_enums.json",
    "KunitsuGami": "resources/data/enums/kunitsugami_enums.json",
    "RE9": "resources/data/enums/re9_enums.json",
    "DRDR": "resources/data/enums/drdr_enums.json",
}
_ENUM_GAME_KEYS = {game_key(game): game for game in _ENUM_PATHS}
_ENUM_GAME_KEYS.update({"MHWS": "MHWilds", "MHR": "MHRise",
                        "RE2RE3RT": "RE2RT", "RERT": "RE2RT", "RE3RESISTANCE": "REResistance",
                        "DD2OLD": "DD2", "ONIMUSHAWOTSDEMO": "OnimushaWOTS"})


class EnumDefinitionError(ValueError):
    pass


def enum_file_path(game):
    key = game_key(game)
    try:
        return resource_path(_ENUM_PATHS[_ENUM_GAME_KEYS[key]], required=True)
    except (KeyError, FileNotFoundError) as exc:
        raise EnumDefinitionError(f"No enum JSON available for game {game!r}: {exc}") from exc


@lru_cache(maxsize=None)
def _read_enum_namespace(path, namespace):
    try:
        with path.open(encoding="utf-8") as stream:
            definitions = json.load(stream)
        if not isinstance(definitions, dict):
            raise ValueError("expected an object of enum definitions")
        return {key: value for key, value in definitions.items() if key.startswith(namespace)}
    except (OSError, ValueError) as exc:
        raise EnumDefinitionError(f"Cannot read enum JSON {path}: {exc}") from exc


def game_enum_definitions(game, *, namespace=""):
    return _read_enum_namespace(enum_file_path(game), namespace)


def enum_games():
    return tuple(_ENUM_PATHS)


class EnumManager:
    _instance = None
    
    @staticmethod
    def instance():
        if not EnumManager._instance:
            EnumManager._instance = EnumManager()
        return EnumManager._instance
        
    def __init__(self):
        self.enums = {}
        self._loaded_versions = set()
        self._game_version = "RE4"  
        self._enum_paths = dict(_ENUM_PATHS)
        
    @property
    def game_version(self):
        return self._game_version
    
    @game_version.setter
    def game_version(self, version):
        if version != self._game_version and version in self._enum_paths:
            self._game_version = version
   
    def _ensure_enums_loaded(self):
        if self._game_version in self._loaded_versions:
            return
            
        self.load_enums()
    
    def load_enums(self):
        enum_path = self._enum_paths.get(self._game_version)
        if not enum_path:
            print(f"No enum file defined for game version: {self._game_version}")
            return
            
        try:
            self.enums[self._game_version] = _read_enum_namespace(resource_path(enum_path, required=True), "")
            self._loaded_versions.add(self._game_version)
        except Exception as e:
            print(f"Error loading enum values: {str(e)}")
    
    def get_enum_values(self, enum_type):
        self._ensure_enums_loaded()
        
        if not enum_type:
            return {}
        
        game_enums = self.enums.get(self._game_version, {})
        return game_enums.get(enum_type, {})
