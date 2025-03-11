import os
import json

SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".reasy_editor_settings.json")
DEFAULT_SETTINGS = {
    "dark_mode": True, 
    "rcol_json_path": "", 
    "show_debug_console": True,
    "show_rsz_advanced": True,
    "game_version": "RE4"  # Default game version
}


def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                settings = json.load(f)
            # Ensure all default keys are present
            for key, value in DEFAULT_SETTINGS.items():
                settings.setdefault(key, value)
            return settings
        except (IOError, json.JSONDecodeError) as e:
            print("Error loading settings:", e)
    return DEFAULT_SETTINGS.copy()


def save_settings(settings):
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(settings, f, indent=4)
    except IOError as e:
        print("Error saving settings:", e)


def ensure_json_path(self) -> bool:
    # Retrieve the JSON path from the settings (which may be the default if file not present)
    settings = getattr(self, "settings", None) or (self.app.settings if hasattr(self, "app") else None)
    if not settings:
        return False
        
    json_path = settings.get("rcol_json_path")
    if not json_path or not os.path.exists(json_path):
        # Notify the main app to prompt the user
        app = self if not hasattr(self, "app") else self.app
        new_path = app.handle_missing_json()
        if not new_path or not os.path.exists(new_path):
            return False
        settings["rcol_json_path"] = new_path
        save_settings(settings)
    return True
