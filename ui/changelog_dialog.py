import math
from PySide6.QtCore import QElapsedTimer, QPointF, Qt, QTimer, QUrl
from PySide6.QtGui import QCursor, QDesktopServices, QPainter, QPainterPath, QPixmap
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
_HEAD = (521.5, 360.0, 259.0)


class _IllustrationPanel(QFrame):
    def __init__(self, image_path: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("IllustrationPanel")
        self.setAttribute(Qt.WA_StyledBackground, True)
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

        left = _IllustrationPanel(str(application_root() / "resources" / "images" / "reasy_guy.png"))
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
                "<b>Motion editing:</b> .mot, .motlist, .mottree and .motpack files open in a new motion editor in "
                "every supported game except RE7 (non-RT), MH Stories 3, Onimusha 2, Kunitsu-Gami and Dead Rising "
                "Deluxe Remaster: edit keys as curves, motion trees as a graph, and preview motions on a character."
            )),
            ("New", self.tr(
                "Motions can be exported to Blender and imported back, even onto another game's characters: bones "
                "are matched by body part and each game's joint axes are lined up."
            )),
            ("New", self.tr(
                "Prefabs that use motion banks or lists have a new Motion tab that plays their motions on the "
                "character, with facial blend shapes, wrinkles and joint constraints."
            )),
            ("New", self.tr(
                "Characters are now assembled from the files that list their parts, such as costume presets, "
                "montages and armor lists, so scenes, prefabs and these files show whole characters in 3D."
            )),
            ("New", self.tr(
                "The Project Browser has a new Catalogue view listing the game's characters; double-click one to "
                "open it on its Motion tab."
            )),
            ("New", self.tr(
                "<b>Joint constraints:</b> .jcns files can now be edited joint by joint, with the constraints shown "
                "live on a character and their driver curves editable."
            )),
            ("New", self.tr(
                "Added DLGTML dialogue timeline editing for MH Wilds and Onimusha: Way of the Sword."
            )),
            ("New", self.tr(
                "<b>Scene features panel:</b> scenes show game-specific options over the 3D view: SF6 costume "
                "colors (pick a part, change its colors and save the edited files to your project), the costume, "
                "preset or outfit each spawner uses, and RE4's campaign state."
            )),
            ("New", self.tr(
                "<b>Game lighting:</b> shell fur is now drawn in RE4, Dragon's Dogma 2, MH Wilds and RE9, skin "
                "uses each game's own subsurface scattering profiles, face wrinkles work in RE2 RT, RE3, RE3 RT and "
                "RE7 RT, and meshes with blend shapes are drawn the game's way in RE3, RE3 RT and RE8."
            )),
            ("New", self.tr(
                "RE4 scenes now start the way the game starts them: start-up scripts show or hide objects for the "
                "chosen campaign state; lights, fans and pendulums animate over time; and volume decals and water "
                "are drawn."
            )),
            ("New", self.tr(
                "<b>Effect from Material:</b> the master material editor can copy an effect, such as a dissolve or "
                "Fresnel, from another material into the one you are editing (Add > Effect from Material…)."
            )),
            ("New", self.tr(
                "Sound bank editing can now create HIRC objects, edit object lists and dialogue event decision "
                "trees, and remove media."
            )),
            ("New", self.tr(
                "TEX images can now be replaced with PNG files, compressed with mipmaps the way the game's own "
                "textures are."
            )),
            ("New", self.tr("MDF texture paths now autocomplete from the game's file list.")),
            ("New", self.tr("Hovering over a tab shows a preview of its contents; 3D tabs show their last frame.")),
            ("New", self.tr(
                "RSZ fields holding flag enums now show each set flag by name and can be toggled from a checklist."
            )),
            ("New", self.tr("RE7 RT meshes can now be edited and saved.")),
            ("New", self.tr(
                "RCOL files from MH Wilds and Onimusha: Way of the Sword can now be opened and edited, and their "
                "stripped names are shown where REasy can recover them."
            )),
            ("New", self.tr("Render texture files (.rtex) can now be viewed.")),
            ("New", self.tr("Added support for many more headless RSZ files, such as WCBK, AMIX, WFA, WMS and WTOT.")),

            # Improved
            ("Improved", self.tr(
                "<b>Timeline editor:</b> CLIP, TML and UCURVE files open in a rebuilt editor with a dope sheet, "
                "curves, undo and checks that stop invalid files from being saved."
            )),
            ("Improved", self.tr(
                "<b>Sound property pages:</b> every sound object and bank chunk is now edited in Wwise-style "
                "property pages instead of a raw field list."
            )),
            ("Improved", self.tr("Master materials (MMTR) now open and build several times faster.")),
            ("Improved", self.tr(
                "The MDF editor is much faster with large materials and now opens on its Overview tab."
            )),
            ("Improved", self.tr(
                "The PAK Browser shows large file lists much faster, and updating its file list no longer freezes "
                "the window."
            )),
            ("Improved", self.tr(
                "Node highlighting in SCN, PFB and USER files moved from the Highlight menu to a brush button in "
                "the file's toolbar, with color presets and custom colors."
            )),
            ("Improved", self.tr(
                "MDF shading types now use each game's own names, and material flags follow each MDF version's "
                "own layout, with a raw value field for every bit."
            )),
            ("Improved", self.tr(
                "Pasting materials or importing MDF templates from another game or version now converts their "
                "shading types and material flags by name."
            )),
            ("Improved", self.tr(
                "When opening a master material, the MDF files offered now say whether each comes from the "
                "project, a PAK, system files or disk."
            )),
            ("Improved", self.tr("Freecam keys in the scene viewer now take priority over window shortcuts.")),
            ("Improved", self.tr(
                "When Wwise cannot create its project, REasy now explains how to fix it, such as moving REasy out "
                "of protected folders."
            )),

            # Updated
            ("Updated", self.tr(
                "Selecting objects in a scene's list no longer moves the camera unless the new focus button in the "
                "scene header is on."
            )),
            ("Updated", self.tr(
                "Improved RSZ dumps for Dragon's Dogma 2, DMC5, MH Wilds, Onimusha: Way of the Sword, Pragmata, "
                "RE2 RT, RE3 RT, RE4, RE7 RT and RE8 (credit to @kagenocookie for the Dragon's Dogma 2 update)."
            )),
            ("Updated", self.tr("Updated enums for every supported game, and the Dragon's Dogma 2 file list.")),
            ("Updated", self.tr(
                "The About dialog now links the REasy Wiki, maintained by BrewedVFX, and plays music with a volume "
                "slider."
            )),
            ("Updated", self.tr(
                "REasy now shows a message and stops if REasy.exe is started outside its folder, away from its "
                "resources."
            )),

            # Fixed
            ("Fixed", self.tr(
                "Fixed 3D views failing on Windows 10 with a root signature error: REasy now offers to download "
                "Microsoft's Direct3D 12 runtime when Windows' own is too old."
            )),
            ("Fixed", self.tr("Fixed MH Wilds meshes and materials previewing black after the game's latest update.")),
            ("Fixed", self.tr(
                "Fixed the start of sounds being cut off on some audio devices, such as Bluetooth headsets."
            )),
            ("Fixed", self.tr("Fixed the RCOL 3D view drawing nothing with Game lighting on.")),
            ("Fixed", self.tr(
                "Fixed the Project Browser's width and folder trees resetting when switching between project and "
                "scene tabs."
            )),
            ("Fixed", self.tr(
                "Fixed long breadcrumb paths and the hidden home page stopping the window from being made smaller."
            )),
            ("Fixed", self.tr("Fixed collapsed RSZ nodes showing the wrong contents after a paste.")),
            ("Fixed", self.tr(
                "Fixed RSZ tree ID labels and Advanced Information going stale, and scene picks selecting the wrong "
                "node, after objects were added or removed."
            )),
            ("Fixed", self.tr("Fixed the hair strand editor's panel squeezing its controls.")),
            ("Fixed", self.tr("Fixed UVS files not updating their cutout flag when cutout UVs were added or removed.")),
            ("Fixed", self.tr(
                "Fixed some dialogs and panels, such as mesh conversion and the material function editor, staying "
                "in memory after being closed."
            )),
            ("Fixed", self.tr(
                "Fixed the UVS editor, MDF template manager, scene preview and RSZ CSV extractor getting stuck after "
                "an error or a cancelled operation."
            )),
            ("Fixed", self.tr("Fixed the File List Generator misreading very long numbers in file names.")),
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
