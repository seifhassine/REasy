import os
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QPixmap, QDesktopServices
from PySide6.QtWidgets import (QDialog, QWidget, QLabel, QVBoxLayout, QHBoxLayout, QFrame, QPushButton,
                               QScrollArea, QSizePolicy)
lazy from ui.editor_widgets import styled_label
lazy from ui.icons import line_icon
lazy from ui.styles import current_colors, set_style_role
lazy from utils.app_paths import application_root

_TAG_BADGES = {
    "new": "success",
    "improved": "info",
    "updated": "accent",
    "fixed": "warning",
    "planned": "neutral",
}


class _IllustrationPanel(QFrame):
    def __init__(self, image_path: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("IllustrationPanel")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMinimumWidth(200)
        self.setMaximumWidth(260)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._orig_pixmap = QPixmap(image_path) if image_path and os.path.exists(image_path) else QPixmap()
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        layout.addStretch(1)
        layout.addWidget(self.image_label, 0, Qt.AlignHCenter | Qt.AlignVCenter)
        layout.addStretch(1)

        self._update_scaled_pixmap()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_scaled_pixmap()

    def _update_scaled_pixmap(self):
        if self._orig_pixmap.isNull():
            self.image_label.clear()
            return
            
        margins = self.layout().contentsMargins()
        avail_width = max(80, self.width() - (margins.left() + margins.right()) - 20)
        avail_height = max(80, self.height() - (margins.top() + margins.bottom()) - 20)
        
        orig_width = self._orig_pixmap.width()
        orig_height = self._orig_pixmap.height()
        
        if orig_width == 0 or orig_height == 0:
            return
            
        min_display_width = 150
        min_display_height = 200
        
        target_width = max(min_display_width, int(avail_width * 0.8))
        target_height = max(min_display_height, int(avail_height * 0.8))
        
        scale_x = target_width / orig_width
        scale_y = target_height / orig_height
        scale_factor = min(scale_x, scale_y, 1.0) 
        
        min_scale = min(min_display_width / orig_width, min_display_height / orig_height)
        scale_factor = max(scale_factor, min_scale)
        
        final_width = int(orig_width * scale_factor)
        final_height = int(orig_height * scale_factor)
        
        scaled = self._orig_pixmap.scaled(
            final_width, 
            final_height, 
            Qt.KeepAspectRatio, 
            Qt.SmoothTransformation
        )
        
        scaled.setDevicePixelRatio(self.devicePixelRatio())
        
        self.image_label.setPixmap(scaled)
        self.image_label.setFixedSize(scaled.size())


class ChangelogDialog(QDialog):
    def __init__(self, parent: QWidget | None, version: str):
        super().__init__(parent)
        self.setWindowTitle(self.tr("What’s new in REasy v{version}").format(version=version))
        self.setModal(True)
        self.resize(820, 520)
        self.setMinimumSize(720, 480)
        self._apply_stylesheet(current_colors())

        base_dir = application_root()
        image_path_candidates = [
            base_dir / "resources" / "images" / "reasy_guy.png",
            base_dir / "resources" / "images" / "reasy_editor_logo.png",
            base_dir / "resources" / "icons" / "reasy_editor_logo.ico",
        ]
        image_path = next((str(p) for p in image_path_candidates if p.exists()), "")

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        left = _IllustrationPanel(image_path)
        root.addWidget(left)

        right_container = QWidget()
        right_layout = QVBoxLayout(right_container)
        right_layout.setContentsMargins(16, 14, 16, 12)
        right_layout.setSpacing(8)

        title = styled_label(self.tr("What’s new in REasy v{version}").format(version=version), heading="title")
        subtitle = styled_label(self.tr("Thanks for updating! Here are the highlights:"), tone="muted")
        right_layout.addWidget(title)
        right_layout.addWidget(subtitle)

        scroll = QScrollArea()
        scroll.setObjectName("changeList")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        right_layout.addWidget(scroll, 1)

        scroll_body = QWidget()
        set_style_role(scroll_body, surface="view")
        scroll.setWidget(scroll_body)
        scroll_layout = QVBoxLayout(scroll_body)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(0)

        changes = [
            # New
            ("New", self.tr(
                "<b>Master material (MMTR) editing:</b> edit a material's shaders as a node "
                "graph from the MDF editor's Edit Shader… button, then save the result as a "
                "variant for just the materials you choose."
            )),
            ("New", self.tr(
                "Added SDF (shader definition) viewing and editing: inspect each pass's "
                "pipeline, edit its render state, and replace or export its compiled shaders."
            )),
            ("New", self.tr(
                "MDF files have a new Material Graph tab, and now warn when their parameters "
                "or textures don't match their master material."
            )),
            ("New", self.tr(
                "<b>Game lighting:</b> meshes and scenes can be drawn with the game's own "
                "shaders and lighting in RE2 and RE3 (RT and non-RT), RE4, RE7 RT, RE8, RE9, "
                "DMC5, SF6, MH Wilds, Dragon's Dogma 2, Onimusha: Way of the Sword and Pragmata."
            )),
            ("New", self.tr(
                "<b>Hair strands:</b> hair prefabs now have a 3D tab that draws their strands "
                "with the game's hair rendering and physics in RE4, RE9 and Pragmata."
            )),
            ("New", self.tr(
                "Added a hair strand editor for RE9 and Pragmata: comb, paint, smooth, grow or "
                "trim strands, edit colliders, physics presets and LOD settings, and save the "
                "result to your project."
            )),
            ("New", self.tr(
                "<b>Mesh conversion:</b> convert meshes to another game's format, with a before "
                "and after preview and a report of any data loss. MDF/Textures are not included for now."
            )),
            ("New", self.tr(
                "<b>Blender round trip:</b> export meshes to .blend and import your edits back "
                "(Blender 5.0 or newer), or import models from other sources onto a game's "
                "reference mesh with automatic bone mapping. MDF/Textures are not included for now."
            )),
            ("New", self.tr(
                "TEX files can now be edited: create them from DDS images, replace their image, "
                "change their settings, or convert them to another game's version."
            )),
            ("New", self.tr(
                "SpeedTree meshes (.stmesh) can now be viewed, and trees and foliage now appear "
                "in scenes, for RE4, SF6, MH Wilds, Dragon's Dogma 2, Onimusha: Way of the Sword "
                "and Pragmata."
            )),
            ("New", self.tr(
                "Mesh distance fields (.sdftex) can now be viewed, or baked from a mesh, for RE4, "
                "RE9, MH Wilds, Dragon's Dogma 2, Onimusha: Way of the Sword and Pragmata."
            )),
            ("New", self.tr(
                "The mesh viewer can now show each LOD, shadow and occluder meshes, shape keys "
                "and vertex attributes, and has a new Inspect panel for isolating and hiding "
                "submeshes."
            )),
            ("New", self.tr("PFB files can now be opened in the 3D scene viewer and added to scenes.")),
            ("New", self.tr(
                "Added Dead Rising Deluxe Remaster support, including SCN, PFB and USER editing "
                "and PAK browsing."
            )),
            ("New", self.tr("Added full sound modding support for Onimusha: Way of the Sword.")),
            ("New", self.tr(
                "Sound preview now has a basic editor for silencing, trimming and cropping a "
                "sound before using it as a replacement."
            )),
            ("New", self.tr(
                "Sound preview has a Normalize option, and sounds can be played and paused "
                "with Space or Enter."
            )),
            ("New", self.tr("Onimusha: Way of the Sword MDF files can now be opened, edited and saved.")),
            ("New", self.tr("Added support for WCSW, WCSS, WCSA and WCSF sound files.")),
            ("New", self.tr("Added a file list for RE Resistance.")),

            # Improved
            ("Improved", self.tr(
                "<b>Fully dockable tabs:</b> drag a tab to split the editor on any side, or "
                "float it in its own window."
            )),
            ("Improved", self.tr(
                "Large scenes are much more responsive: hovering and selecting objects respond "
                "immediately, and objects outside the view are no longer drawn."
            )),
            ("Improved", self.tr(
                "REasy starts up faster, and opening game PAKs is faster and uses less memory."
            )),
            ("Improved", self.tr(
                "Projects open without freezing the window, and Project Browser trees now start "
                "directly inside the natives folder."
            )),
            ("Improved", self.tr(
                "Redesigned the sound preview: Play now pauses and resumes, the source list can "
                "be sorted and filtered by Used by, and replacement options moved to a "
                "Replacement Settings dialog."
            )),
            ("Improved", self.tr(
                "Sound banks now identify many more sounds, and MSG Sound Scan finds cutscene "
                "and event dialogue in Dragon's Dogma 2, MH Rise, MH Wilds, Pragmata and SF6."
            )),
            ("Improved", self.tr(
                "The Project Browser and PAK Browser now always skip mod PAKs and warn when the "
                "game folder contains modded PAKs."
            )),
            ("Improved", self.tr(
                "Redesigned the File List Generator with new search modes, time estimates, much "
                "faster searches and optional GPU acceleration."
            )),
            ("Improved", self.tr("GCF and IFT font files can now be viewed in every supported game.")),

            # Updated
            ("Updated", self.tr(
                "<b>UI refresh:</b> restyled dark theme, line icons and more compact layouts "
                "across viewers, dialogs and tools."
            )),
            ("Updated", self.tr(
                "Updated file lists, RSZ dumps and enums for Dragon's Dogma 2's latest update, "
                "the full release of Onimusha: Way of the Sword, SF6 and Pragmata, and improved "
                "file lists for many other games."
            )),
            ("Updated", self.tr(
                "Updated Dragon's Dogma 2 sound support for the game's latest update, and sound "
                "data for all supported games."
            )),
            ("Updated", self.tr(
                "Replacing or adding sounds now requires an open project with its game folder set."
            )),
            ("Updated", self.tr("3D views now render with Direct3D 12 instead of OpenGL.")),
            ("Updated", self.tr(
                "Added a setting to choose whether REasy restores your workspace on startup."
            )),
            ("Updated", self.tr(
                "The AI Assistant and Output console now start hidden for new users and can be "
                "turned on from the View menu."
            )),

            # Fixed
            ("Fixed", self.tr(
                "Fixed PAK browsing and extraction sometimes using an outdated copy of a file."
            )),
            ("Fixed", self.tr(
                "Fixed Dragon's Dogma 2 PAKs failing to open after the game's latest update, and "
                "corrupted files extracted from Onimusha: Way of the Sword's main PAK."
            )),
            ("Fixed", self.tr(
                "Fixed memory build-up, freezes and occasional crashes after closing SCN, PFB "
                "and USER tabs."
            )),
            ("Fixed", self.tr("Fixed RE4 sound replacements sometimes not updating every linked sound bank.")),
            ("Fixed", self.tr("Fixed some sound banks with large music containers loading very slowly.")),
            ("Fixed", self.tr("Fixed some MH Wilds CLIP files failing to open.")),
            ("Fixed", self.tr("Fixed MDF parameters with two or three values saving only their first value.")),
            ("Fixed", self.tr(
                "Fixed new GameObjects, folders and Transforms being created with drawing turned "
                "off and zero scale."
            )),
            ("Fixed", self.tr("Fixed a crash when switching back to a project after opening a scene.")),
            ("Fixed", self.tr("Fixed DDS export of cubemaps and texture arrays producing broken files.")),
        ]
        badges = []
        for tag, text in changes:
            item, badge = self._create_change_item(tag, text)
            badges.append(badge)
            scroll_layout.addWidget(item)
        badge_width = max(badge.sizeHint().width() for badge in badges)
        for badge in badges:
            badge.setFixedWidth(badge_width)

        scroll_layout.addStretch(1)

        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(6)

        github_btn = QPushButton(line_icon("star"), self.tr("Star on GitHub"))
        github_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://github.com/seifhassine/REasy")))
        github_btn.setCursor(Qt.PointingHandCursor)
        buttons_row.addWidget(github_btn)

        buttons_row.addStretch(1)
        view_btn = QPushButton(line_icon("external"), self.tr("View release notes…"))
        view_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://github.com/seifhassine/REasy/releases")))
        close_btn = QPushButton(self.tr("Let’s go!"))
        set_style_role(close_btn, variant="primary")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        buttons_row.addWidget(view_btn)
        buttons_row.addWidget(close_btn)
        right_layout.addLayout(buttons_row)

        root.addWidget(right_container, 1)

    def _create_change_item(self, tag: str, text: str) -> tuple[QWidget, QLabel]:
        container = QFrame()
        container.setObjectName("ChangeItem")
        layout = QHBoxLayout(container)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(10)

        tag_labels = {
            "New": self.tr("New"),
            "Updated": self.tr("Updated"),
            "Improved": self.tr("Improved"),
            "Fixed": self.tr("Fixed"),
            "Planned": self.tr("Planned"),
        }
        badge = styled_label(tag_labels.get(tag, tag), badge=_TAG_BADGES.get(tag.lower(), "neutral"))
        badge.setAlignment(Qt.AlignCenter)
        badge.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        desc = QLabel(text)
        desc.setWordWrap(True)
        desc.setOpenExternalLinks(True)

        layout.addWidget(badge, 0, Qt.AlignTop)
        layout.addWidget(desc, 1)
        return container, badge

    def _apply_stylesheet(self, colors: dict):
        self.setStyleSheet(f"""
            QFrame#IllustrationPanel {{
                background-color: {colors['view_bg']}; border: none; border-right: 1px solid {colors['gutter']};
            }}
            QScrollArea#changeList {{ border: 1px solid {colors['border_subtle']}; }}
            QFrame#ChangeItem {{
                background: transparent; border: none; border-bottom: 1px solid {colors['border_subtle']};
            }}
        """)
