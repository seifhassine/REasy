import os
import json
from copy import deepcopy

from services.ai.chat_service import (
    AI_PROVIDER_CONFIGS,
    DEEPSEEK_PROVIDER,
    LOCAL_PROVIDER,
    get_ai_provider_config,
    normalize_context_window,
    thinking_config_for_model,
)

SETTINGS_FILE = os.path.join(os.getcwd(), "settings.json")
AI_FILE_ACTION_MODES = frozenset({"review", "request", "scoped_autopilot"})
SHORTCUT_SCHEME_VERSION = 2
LEGACY_SHORTCUT_DEFAULTS = {
    "find_search_guid": "Ctrl+G",
    "find_search_text": "Ctrl+T",
    "find_search_number": "Ctrl+N",
    "view_prev_tab": "PgDown",
    "view_next_tab": "PgUp",
    "view_debug_console": "Ctrl+Shift+D",
    "view_ai_chat": "Ctrl+Shift+A",
}
DEFAULT_SETTINGS = {
    "rcol_json_path": "", 
    "show_debug_console": True,
    "show_ai_chat": True,
    "ai_provider": DEEPSEEK_PROVIDER.id,
    "deepseek_model": DEEPSEEK_PROVIDER.default_model,
    "deepseek_context_window_tokens": 0,
    "deepseek_thinking_mode": "enabled",
    "deepseek_reasoning_effort": "high",
    "local_ai_endpoint": LOCAL_PROVIDER.default_endpoint,
    "local_ai_model": LOCAL_PROVIDER.default_model,
    "local_ai_context_window_tokens": 0,
    "ai_file_action_mode": "review",
    "ai_file_autopilot_trash": False,
    "show_rsz_advanced": True,
    "game_version": "RE4",  # Default game version
    "backup_on_save": True,
    "ui_language": "system",
    "translation_target_language": "en",
    "tree_highlight_color": "#00aaff",
    "vgmstream_cli_path": "",
    "wwise_install_paths": {},
    "keyboard_shortcuts": {
        "file_open": "Ctrl+O",
        "file_save": "Ctrl+S",
        "file_save_all": "Ctrl+Alt+S",
        "file_save_as": "Ctrl+Shift+S",
        "file_reload": "Ctrl+R",
        "file_close_tab": "Ctrl+W",
        "file_reopen_closed": "Ctrl+Shift+T",
        "find_search": "Ctrl+F",
        "find_search_guid": "Ctrl+Alt+G",
        "find_search_text": "Ctrl+Alt+T",
        "find_search_number": "Ctrl+Alt+N",
        "find_search_hex": "Ctrl+Alt+H",
        "find_rsz_field_value": "Ctrl+Alt+R",
        "view_prev_tab": "Ctrl+PgUp",
        "view_next_tab": "Ctrl+PgDown",
        "view_debug_console": "Ctrl+Shift+U",
        "view_ai_chat": "Ctrl+Alt+A",
        "editor_split_right": "Ctrl+\\",
        "editor_split_down": "Ctrl+Alt+\\"
    },
    "shortcut_scheme_version": SHORTCUT_SCHEME_VERSION,
    "workbench": {
        "restore_session": True,
        "state_version": 1,
        "window_geometry": "",
        "window_state": "",
        "project_browser": {},
        "session": {},
    },
    "confirmation_prompt": True,
    "verify_rsz_crc_on_open": True,
    "recently_closed_files": [],
    "last_seen_version": "",
    "enum_prompt_checked_json_path": "",
    "renderer_texture_quality": "balanced",
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

    if "renderer_texture_quality" not in settings and settings.get("mesh_viewer_prefer_streaming_tex"):
        normalized["renderer_texture_quality"] = "high"

    for key, value in settings.items():
        if key == "dark_mode":
            continue
        if key == "wwise_install_paths":
            normalized[key] = (
                {
                    str(game).strip().upper(): str(path).strip()
                    for game, path in value.items()
                    if str(game).strip() and str(path).strip()
                }
                if isinstance(value, dict)
                else {}
            )
            continue
        if key == "keyboard_shortcuts" and isinstance(value, dict):
            try:
                shortcut_scheme = int(settings.get("shortcut_scheme_version", 1) or 1)
            except (TypeError, ValueError):
                shortcut_scheme = 1
            legacy_scheme = shortcut_scheme < SHORTCUT_SCHEME_VERSION
            for name, shortcut in value.items():
                if name == "view_dark_mode":
                    continue
                if legacy_scheme and LEGACY_SHORTCUT_DEFAULTS.get(name) == shortcut:
                    shortcut = DEFAULT_SETTINGS["keyboard_shortcuts"].get(name, shortcut)
                normalized[key][name] = shortcut
        elif key == "workbench":
            if isinstance(value, dict):
                normalized[key].update(value)
        elif key == "keyboard_shortcuts":
            continue
        else:
            normalized[key] = value

    normalized["shortcut_scheme_version"] = SHORTCUT_SCHEME_VERSION

    normalized["ai_provider"] = get_ai_provider_config(
        normalized["ai_provider"]
    ).id
    for provider in AI_PROVIDER_CONFIGS.values():
        normalized[provider.context_setting] = normalize_context_window(
            normalized[provider.context_setting]
        )
        for key, fallback in (
            (provider.model_setting, provider.default_model),
            (provider.endpoint_setting, provider.default_endpoint),
        ):
            if key is None:
                continue
            value = normalized[key]
            normalized[key] = (
                value.strip()
                if isinstance(value, str) and value.strip()
                else fallback
            )
        thinking = thinking_config_for_model(
            provider,
            normalized[provider.model_setting],
        )
        if thinking is None:
            continue
        for key, allowed, fallback in (
            (
                provider.thinking_mode_setting,
                thinking.modes,
                thinking.default_mode,
            ),
            (
                provider.reasoning_effort_setting,
                thinking.reasoning_efforts,
                thinking.default_reasoning_effort,
            ),
        ):
            if key is None:
                continue
            value = str(normalized.get(key, "")).strip().casefold()
            normalized[key] = value if value in allowed else fallback
    file_action_mode = str(
        normalized.get("ai_file_action_mode", "review")
    ).strip().casefold()
    normalized["ai_file_action_mode"] = (
        file_action_mode
        if file_action_mode in AI_FILE_ACTION_MODES
        else "review"
    )
    normalized["ai_file_autopilot_trash"] = (
        normalized.get("ai_file_autopilot_trash") is True
    )
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

