import os
import json
from copy import deepcopy

SETTINGS_FILE = os.path.join(os.getcwd(), "settings.json")
DEFAULT_SETTINGS = {
    "dark_mode": True, 
    "rcol_json_path": "", 
    "show_debug_console": True,
    "show_rsz_advanced": True,
    "game_version": "RE4",  # Default game version
    "backup_on_save": True,
    "ui_language": "system",
    "translation_target_language": "en",
    "tree_highlight_color": "#ff851b",
    "vgmstream_cli_path": "",
    "keyboard_shortcuts": {
        "file_open": "Ctrl+O",
        "file_save": "Ctrl+S",
        "file_save_as": "Ctrl+Shift+S",
        "file_reload": "Ctrl+R",
        "file_close_tab": "Ctrl+W",
        "file_reopen_closed": "Ctrl+Shift+T",
        "find_search": "Ctrl+F",
        "find_search_guid": "Ctrl+G",
        "find_search_text": "Ctrl+T",
        "find_search_number": "Ctrl+N",
        "view_dark_mode": "Ctrl+D",
        "view_prev_tab": "PgDown",
        "view_next_tab": "PgUp",
        "view_debug_console": "Ctrl+Shift+D"
    },
    "confirmation_prompt": True,
    "verify_rsz_crc_on_open": True,
    "recently_closed_files": [],
    "last_seen_version": "",
    "enum_prompt_checked_json_path": "",
    "mesh_viewer_prefer_streaming_tex": False,
    "mesh_viewer_fps_limit": 60,
    "mesh_viewer_wireframe_mode": "off",
    "mesh_viewer_lighting_mode": "fixed",
    "mesh_viewer_line_width": 1.5,
    "mesh_viewer_ambient": 0.35,
    "mesh_viewer_diffuse": 0.65,
    "mesh_viewer_show_bones": False,
}


def normalize_settings(settings=None):
    """Return an independent settings dictionary with all defaults applied."""
    normalized = deepcopy(DEFAULT_SETTINGS)
    if not isinstance(settings, dict):
        return normalized

    for key, value in settings.items():
        if key == "keyboard_shortcuts" and isinstance(value, dict):
            normalized[key].update(value)
        else:
            normalized[key] = value
    return normalized


def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                settings = json.load(f)
            return normalize_settings(settings)
        except (IOError, json.JSONDecodeError) as e:
            print("Error loading settings:", e)
    return normalize_settings()


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
        settings["enum_prompt_checked_json_path"] = ""
        save_settings(settings)
    return True
