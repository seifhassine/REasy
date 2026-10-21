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
        print(base_dir)
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
                "Added full sound modding support for RE2 RT, RE3 RT, and RE7 RT."
            )),
            ("New", self.tr("MSG files now include a Sound Scan button. Sound preview is now available wherever applicable.")),
            ("New", self.tr("New sound sources can now be added to BNK and PCK files.")),
            ("New", self.tr("Sound banks can now be searched by source ID.")),
            ("New", self.tr(
                "Added quality, codec, and compression settings for sound replacements "
                "and additions."
            )),
            ("New", self.tr(
                "Users are now warned when saving a sound file while other affected "
                "files are open."
            )),
            ("New", self.tr("Added support for WCP, WCST, WGS, and WSS files.")),
            ("New", self.tr("Added Spanish and Russian translations.")),

            # Improved
            ("Improved", self.tr("Improved performance during sound editing.")),
            ("Improved", self.tr(
                "New sounds now automatically adjust their duration and fade curves."
            )),

            # Updated
            ("Updated", self.tr(
                "<b>General UI overhaul:</b> refined the workbench to feel more fluid "
                "and VS Code-like."
            )),
            ("Updated", self.tr("Added a search bar to the Project Files tab.")),
            ("Updated", self.tr("Added a bookmarks system.")),
            ("Updated", self.tr("Project folders can now be changed.")),
            ("Updated", self.tr(
                "MOV files can now be opened through the operating system."
            )),

            # Fixed
            ("Fixed", self.tr("Fixed sound replacement issues.")),
            ("Fixed", self.tr(
                "8-bit WAV files are now rejected instead of becoming silent tracks."
            )),
            ("Fixed", self.tr("Fixed RE9 RSZ dump issues.")),
            ("Fixed", self.tr("Fixed AI Assistant failures while editing RSZ files.")),
            ("Fixed", self.tr(
                "Fixed resource links failing to resolve files stored in PAKs."
            )),
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
