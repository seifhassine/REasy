import os
import json

SETTINGS_FILE = os.path.join(os.getcwd(), "settings.json")
DEFAULT_SETTINGS = {
    "dark_mode": True, 
    "rcol_json_path": "", 
    "show_debug_console": True,
    "show_rsz_advanced": True,
    "game_version": "RE4",  # Default game version
    "backup_on_save": True,
    "translation_target_language": "en",
    "keyboard_shortcuts": {
        "file_open": "Ctrl+O",
        "file_save": "Ctrl+S",
        "file_save_as": "Ctrl+Shift+S",
        "file_reload": "Ctrl+R",
        "file_close_tab": "Ctrl+W",
        "find_search": "Ctrl+F",
        "find_search_guid": "Ctrl+G",
        "find_search_text": "Ctrl+T",
        "find_search_number": "Ctrl+N",
        "view_dark_mode": "Ctrl+D",
        "view_prev_tab": "PgDown",
        "view_next_tab": "PgUp",
        "view_debug_console": "Ctrl+Shift+D"
    },
    "confirmation_prompt": True
}


def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                settings = json.load(f)
            # Ensure all default keys are present
            for key, value in DEFAULT_SETTINGS.items():
                if key == "keyboard_shortcuts" and key in settings:
                    for shortcut_key, shortcut_value in DEFAULT_SETTINGS["keyboard_shortcuts"].items():
                        settings["keyboard_shortcuts"].setdefault(shortcut_key, shortcut_value)
                else:
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
