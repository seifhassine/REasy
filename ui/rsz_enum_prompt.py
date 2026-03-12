from pathlib import Path

from PySide6.QtWidgets import QMessageBox



class RszEnumPromptController:
    """Handles RSZ registry/enums mismatch prompting flow."""

    _ALIASES = (
        ("re2rt", "RE2RT"), 
        ("re3rt", "RE3RT"), 
        ("re7rt", "RE7RT"),
        ("reresistance", "REResistance"), 
        ("mhws", "MHWilds"),
        ("mhwilds", "MHWilds"), 
        ("mhrise", "MHRise"), 
        ("mhst3", "MHST3"), 
        ("pragmata", "Pragmata"), 
        ("kunitsugami", "KunitsuGami"),
        ("dmc5", "DMC5"), 
        ("sf6", "SF6"), 
        ("dd2", "DD2"), 
        ("o2", "O2"),
        ("re8", "RE8"), 
        ("re7", "RE7"), 
        ("re4", "RE4"), 
        ("re3", "RE3"), 
        ("re2", "RE2"),
        ("re9", "RE9"),
    )

    @classmethod
    def infer_game_version(cls, json_path: str):
        key = Path(json_path).stem.lower().replace("_", "")
        for token, version in cls._ALIASES:
            if token in key:
                return version
        return None

    @classmethod
    def maybe_prompt_for_loaded_rsz(cls, app):
        json_path = app.settings.get("rcol_json_path", "")
        current = app.settings.get("game_version", "RE4")
        current_signature = f"{json_path}|{current}"

        if not json_path or app.settings.get("enum_prompt_checked_json_path") == current_signature:
            return

        expected = cls.infer_game_version(json_path)
        if expected and expected != current:
            ans = QMessageBox.question(
                app,
                "Enum Mismatch Detected",
                (
                    f"The selected type registry appears to be for {expected}, "
                    f"but enums are set to {current}.\n\n"
                    f"Switch enums to {expected}?"
                ),
                QMessageBox.Yes | QMessageBox.No,
            )
            if ans == QMessageBox.Yes:
                app.settings["game_version"] = expected
                app.update_from_app_settings()
                current = expected

        app.settings["enum_prompt_checked_json_path"] = f"{json_path}|{current}"
        app.save_settings()
