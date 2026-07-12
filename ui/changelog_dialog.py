import os
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QPixmap, QPainter, QLinearGradient, QColor, QDesktopServices
from PySide6.QtWidgets import (QDialog, QWidget, QLabel, QVBoxLayout, QHBoxLayout, QFrame, QPushButton, 
                               QScrollArea, QSpacerItem, QSizePolicy, QGraphicsDropShadowEffect)
from ui.styles import get_color_scheme
from utils.app_paths import application_root

class _IllustrationPanel(QFrame):
    def __init__(self, image_path: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("IllustrationPanel")
        self.setMinimumWidth(200)
        self.setMaximumWidth(260)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._orig_pixmap = QPixmap(image_path) if image_path and os.path.exists(image_path) else QPixmap()
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setStyleSheet("background: transparent;")
        self.image_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        layout.addStretch(1)
        layout.addWidget(self.image_label, 0, Qt.AlignHCenter | Qt.AlignVCenter)
        layout.addStretch(1)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(40)
        shadow.setOffset(0, 0)
        shadow.setColor(Qt.black)
        self.image_label.setGraphicsEffect(shadow)

        colors = get_color_scheme()

        self._bg_start = QColor(colors['bg'])
        highlight = colors.get('highlight', '#00aaff')
        self._bg_end = QColor(highlight) if isinstance(highlight, str) else QColor(0, 170, 255)

        self.setStyleSheet("QFrame#IllustrationPanel { border: none; }")

        self._update_scaled_pixmap()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_scaled_pixmap()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0.0, self._bg_start)
        gradient.setColorAt(1.0, self._bg_end)
        painter.fillRect(rect, gradient)
        super().paintEvent(event)

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

        colors = get_color_scheme()
        self._apply_stylesheet(colors)

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
        right_layout.setContentsMargins(24, 16, 24, 16)
        right_layout.setSpacing(10)

        title = QLabel(self.tr(
            "<span style='font-size:18pt; font-weight:700;'>What’s new in REasy v{version}</span>"
        ).format(version=version))
        subtitle = QLabel(self.tr("Thanks for updating! Here are the highlights:"))
        subtitle.setStyleSheet("opacity: 0.85;")
        right_layout.addWidget(title)
        right_layout.addWidget(subtitle)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        right_layout.addWidget(scroll, 1)

        scroll_body = QWidget()
        scroll.setWidget(scroll_body)
        scroll_layout = QVBoxLayout(scroll_body)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(8)

        changes = [
            ("New", self.tr("CLIP/TML/UCURVE full support.")),
            ("New", self.tr("Project library, replacing the old file system picker for browsing projects.")),
            ("New", self.tr("Multi-project mode, allowing user to open and edit multiple projects at the same time.")),
            ("New", self.tr("Support for Onimusha Way of the Sword Demo and Pragmata full release.")),
            ("Updated", self.tr("DD2, RE9 and Onimusha WOTS RSZ dumps and enums (<a href=https://github.com/kagenocookie>shadowcookie</a>)).")),
            ("Updated", self.tr("RSZ dumps, file lists and enums of most other games.")),
            ("Improved", self.tr("Floats will now no longer use scientific notation.")),
            ("Improved", self.tr("RSZ json will now be autodetected when opening projects.")),
            ("Improved", self.tr("UI improvements for UVS.")),
            ("Improved", self.tr("Better performance and coverage for file list generator, as well as a new tex/mdf/mesh mode.")),
            ("Improved", self.tr("Better load speed for new and existing projects.")),
            ("Fixed", self.tr("Fixed bad aspect ratio of right-side preview in UVS files.")),
            ("Fixed", self.tr("File list was not updating correctly in PAK Browser even when selecting a different list or toggling valid files/unknown files.")),
            ("Fixed", self.tr("Fuzzy 3D text for bone names in mesh preview.")),
            ("Fixed", self.tr("An issue with floats changing values without edits on systems with different locales (comma vs dot).")),
        ]
        for tag, text in changes:
            item = self._create_change_item(tag, text)
            scroll_layout.addWidget(item)

        scroll_layout.addItem(QSpacerItem(0, 0, QSizePolicy.Minimum, QSizePolicy.Expanding))

        buttons_row = QHBoxLayout()

        github_btn = QPushButton(self.tr("⭐ Star on GitHub"))
        github_btn.setObjectName("GitHubButton")
        github_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://github.com/seifhassine/REasy")))
        github_btn.setCursor(Qt.PointingHandCursor)
        buttons_row.addWidget(github_btn)
        
        buttons_row.addStretch(1)
        view_btn = QPushButton(self.tr("View release notes…"))
        view_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://github.com/seifhassine/REasy/releases")))
        close_btn = QPushButton(self.tr("Let’s go!"))
        close_btn.clicked.connect(self.accept)
        buttons_row.addWidget(view_btn)
        buttons_row.addWidget(close_btn)
        right_layout.addLayout(buttons_row)

        root.addWidget(right_container, 1)

    def _create_change_item(self, tag: str, text: str) -> QWidget:
        container = QFrame()
        container.setObjectName("ChangeItem")
        container.setFrameShape(QFrame.StyledPanel)
        container.setProperty("tag", tag)
        layout = QHBoxLayout(container)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(12)

        tag_labels = {
            "New": self.tr("New"),
            "Updated": self.tr("Updated"),
            "Improved": self.tr("Improved"),
            "Fixed": self.tr("Fixed"),
            "Planned": self.tr("Planned"),
        }
        pill = QLabel(tag_labels.get(tag, tag))
        pill.setObjectName("TagPill")
        pill.setAlignment(Qt.AlignCenter)
        pill.setMinimumWidth(88)
        pill.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        pill.setProperty("tagName", tag.lower())

        desc = QLabel(text)
        desc.setWordWrap(True)
        desc.setOpenExternalLinks(True)

        layout.addWidget(pill)
        layout.addWidget(desc, 1)
        return container

    def _apply_stylesheet(self, colors: dict):
        bg = colors['bg']
        fg = colors['fg']
        border = colors['border']
        highlight = colors['highlight'] if isinstance(colors['highlight'], str) else '#00aaff'
        self.setStyleSheet(f"""
            QDialog {{ background-color: {bg}; color: {fg}; }}
            QLabel {{ color: {fg}; }}
            QScrollArea {{ background: transparent; }}
            /* Seamless panel, no divider */
            #IllustrationPanel {{ border-right: none; }}
            QPushButton {{
                background-color: {colors['input_bg']};
                color: {fg};
                border: 1px solid {border};
                padding: 8px 14px;
                border-radius: 6px;
            }}
            QPushButton:hover {{ border-color: {highlight}; }}
            QPushButton:pressed {{ background-color: rgba(0,0,0,0.06); }}
            QFrame#ChangeItem {{
                border: 1px solid {border};
                border-radius: 8px;
            }}
            QLabel#TagPill {{
                padding: 4px 10px;
                border-radius: 12px;
                background-color: {highlight};
                color: white;
                font-weight: 600;
            }}
            QLabel#TagPill[tagName="new"] {{
                background-color: #27ae60;
            }}
            QLabel#TagPill[tagName="improved"] {{
                background-color: #f39c12;
            }}
            QLabel#TagPill[tagName="updated"] {{
                background-color: #1034A6;
            }}
            QLabel#TagPill[tagName="fixed"] {{
                background-color: #e74c3c;
            }}
            QLabel#TagPill[tagName="planned"] {{
                background-color: #3498db;
            }}
            QPushButton#GitHubButton {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {highlight}, stop:1 rgba(0, 170, 255, 0.8));
                color: white;
                border: none;
                padding: 8px 14px;
                border-radius: 6px;
                font-weight: 600;
            }}
            QPushButton#GitHubButton:hover {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 rgba(51, 187, 255, 1.0), stop:1 {highlight});
            }}
            QPushButton#GitHubButton:pressed {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 rgba(0, 170, 255, 0.9), stop:1 rgba(0, 170, 255, 0.7));
            }}
        """)
