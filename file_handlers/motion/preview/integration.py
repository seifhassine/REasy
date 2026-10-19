from __future__ import annotations

from .support_registry import entity_motion_support_for_game
lazy from PySide6.QtCore import QCoreApplication, Qt
lazy from PySide6.QtWidgets import QLabel
lazy from .entity_widget import PfbMotionPreviewWidget


def create_pfb_motion_preview(handler):
    if not getattr(getattr(handler, "rsz_file", None), "is_pfb", False):
        return None
    support = entity_motion_support_for_game(
        getattr(handler, "game_version", "")
    )
    if support is None:
        return None
    if not getattr(getattr(handler, "resource_context", None), "project_dir", ""):
        notice = QLabel(
            QCoreApplication.translate(
                "MotionPreview",
                "PFB 3D preview is only available in project mode. "
                "Please open or create a project."
            )
        )
        notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        notice.setWordWrap(True)
        return notice


    return PfbMotionPreviewWidget(handler, support=support)
