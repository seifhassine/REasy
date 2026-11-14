import math
from PySide6.QtCore import QElapsedTimer, QPointF, Qt, QTimer, QUrl
from PySide6.QtGui import QCursor, QDesktopServices, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (QDialog, QWidget, QLabel, QVBoxLayout, QHBoxLayout, QFrame, QPushButton,
                               QScrollArea, QSizePolicy)
lazy from ui.editor_widgets import styled_label
lazy from ui.icons import line_icon
lazy from ui.styles import current_colors, set_style_role
lazy from ui.support import SUPPORT_URL
lazy from utils.app_paths import application_root

_TAG_BADGES = {
    "new": "success",
    "improved": "info",
    "updated": "accent",
    "fixed": "warning",
    "planned": "neutral",
}
_HEAD = (521.5, 360.0, 259.0)


class IllustrationPanel(QFrame):
    def __init__(self, image_path: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("IllustrationPanel")
        self.setAttribute(Qt.WA_StyledBackground, True)
        colors = current_colors()
        self.setStyleSheet(
            f"QFrame#IllustrationPanel {{ background-color: {colors['view_bg']}; border: none; "
            f"border-right: 1px solid {colors['gutter']}; }}"
        )
        self.setMinimumWidth(200)
        self.setMaximumWidth(260)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self._source = QPixmap(image_path)
        self._parts = None
        self._look = self._hop = -10.0
        self._clock = QElapsedTimer()
        self._clock.start()
        self._timer = QTimer(self, interval=16, timeout=self.update)

    def showEvent(self, event):
        super().showEvent(event)
        self._timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._timer.stop()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._parts = None

    def mousePressEvent(self, event):
        self._hop = self._clock.elapsed() / 1000

    def _split(self):
        scale = min(0.8 * self.width() / self._source.width(), 0.8 * self.height() / self._source.height(), 1.0)
        ratio = self.devicePixelRatioF()
        body = self._source.scaled(self._source.size() * scale * ratio, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        body.setDevicePixelRatio(ratio)
        cx, cy, radius = (value * scale for value in _HEAD)
        disc = QPainterPath()
        disc.addEllipse(QPointF(cx, cy), radius, radius)
        head = QPixmap(body.size())
        head.setDevicePixelRatio(ratio)
        head.fill(Qt.transparent)
        painter = QPainter(head)
        painter.setClipPath(disc)
        painter.drawPixmap(0, 0, body)
        painter.end()
        painter = QPainter(body)
        painter.setCompositionMode(QPainter.CompositionMode_Clear)
        painter.fillPath(disc, Qt.black)
        painter.end()
        size = body.deviceIndependentSize()
        origin = QPointF((self.width() - size.width()) / 2, (self.height() - size.height()) / 2)
        return head, body, origin, QPointF(cx, cy + radius), QPointF(cx, cy), size.height()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._source.isNull():
            return
        if self._parts is None:
            self._parts = self._split()
        head, body, origin, neck, center, height = self._parts
        now = self._clock.elapsed() / 1000
        gaze = max(-1.0, min(1.0, (self.mapFromGlobal(QCursor.pos()).x() - origin.x() - neck.x()) / self.width()))
        self._look = gaze if self._look < -1 else self._look + (gaze - self._look) * 0.08
        hop = min(1.0, now - self._hop) * 2
        lift = 24 * math.sin(math.pi * hop) if hop < 1 else 0.0
        breath = 0.008 * math.sin(now * 2.2)
        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        painter.translate(origin.x(), origin.y() - lift)
        painter.save()
        painter.translate(0, height)
        painter.scale(1, 1 + breath)
        painter.translate(0, -height)
        painter.drawPixmap(0, 0, body)
        painter.restore()
        painter.translate(neck.x(), neck.y() - (height - neck.y()) * breath)
        painter.rotate(7 * self._look + 1.5 * math.sin(now * 1.3))
        painter.translate(center - neck)
        painter.rotate(360 * hop * hop if hop < 1 else 0)
        painter.translate(-center)
        painter.drawPixmap(0, 0, head)


class ChangelogDialog(QDialog):
    def __init__(self, parent: QWidget | None, version: str):
        super().__init__(parent)
        self.setWindowTitle(self.tr("What’s new in REasy v{version}").format(version=version))
        self.setModal(True)
        self.resize(820, 520)
        self.setMinimumSize(720, 480)
        self._apply_stylesheet(current_colors())

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        left = IllustrationPanel(str(application_root() / "resources" / "images" / "reasy_guy.png"))
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
                "<b>Behavior trees and state machines:</b> .bhvt, .fsmv2, .motfsm2, .tmlfsm2, .fsm and .motfsm files "
                "can now be edited in a graph editor."
            )),
            ("New", self.tr("Added Tools > New Texture… to create textures from PNG, TGA or DDS images.")),
            ("New", self.tr(
                "TEX lanes (roughness, normal, cavity…) are now named in the viewer and can be exported and imported "
                "as separate images."
            )),
            ("New", self.tr(
                "Motions can now be exported onto an existing armature in a .blend, or together with the previewed "
                "meshes."
            )),

            # Improved
            ("Improved", self.tr(
                "<b>GUI editor:</b> rebuilt for DMC5 with a hierarchy, state timeline, canvas editing and undo."
            )),
            ("Improved", self.tr(
                "GUI previews now render on the GPU like the game, including meshes, masks, materials, text effects "
                "and blur."
            )),
            ("Improved", self.tr(
                "Added a GUI Try mode for testing menus and lists, plus preview options for screen size, language and "
                "input device."
            )),
            ("Improved", self.tr("TEX import now supports TGA, and textures can be exported as PNG or TGA.")),
            ("Improved", self.tr("Blender motion export now only includes the selected motions.")),
            ("Improved", self.tr(
                "Blender mesh export now orients bones like RE Mesh Editor, so exported motions work on armatures "
                "from either tool."
            )),
            ("Improved", self.tr(
                "Most RE Mesh Editor and older Noesis exports that REasy refused now open with a warning, and are "
                "fixed on save."
            )),
            ("Improved", self.tr(
                "Timeline curves and motion preview tracks now evaluate keys the way each game does."
            )),
            ("Improved", self.tr("The Project Browser now shows file type icons and greys out unsupported files.")),

            # Updated
            ("Updated", self.tr(
                "Updated RSZ dumps for Dragon's Dogma 2 and MH Wilds, and fixed FSM types in the RE Resistance and "
                "non-RT RE2 and RE3 dumps."
            )),

            # Fixed
            ("Fixed", self.tr(
                "Fixed UVAR range variables showing as vectors and expression links being misread. UVAR files also "
                "now save in the game's layout."
            )),
            ("Fixed", self.tr("Fixed array copy/paste in scenes and prefabs with a 3D or Motion tab.")),
            ("Fixed", self.tr("Fixed motion and JCNS editor shortcuts firing window-wide or not at all.")),
            ("Fixed", self.tr("Fixed some master material pin edits being refused or built wrong.")),
            ("Fixed", self.tr("Fixed sound bank Connect Child offering no music children for music containers.")),
            ("Fixed", self.tr("Fixed some dropdowns, like the 3D view's Shading and Wireframe, opening too short.")),
            ("Fixed", self.tr("Fixed clipped buttons in the mod export dialog.")),
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
        support_btn = QPushButton(line_icon("heart", current_colors()["danger"]), self.tr("Support REasy"))
        support_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(SUPPORT_URL)))
        buttons_row.addWidget(support_btn)

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
            QScrollArea#changeList {{ border: 1px solid {colors['border_subtle']}; }}
            QFrame#ChangeItem {{
                background: transparent; border: none; border-bottom: 1px solid {colors['border_subtle']};
            }}
        """)
