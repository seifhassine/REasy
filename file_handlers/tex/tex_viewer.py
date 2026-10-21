from __future__ import annotations

from pathlib import Path
from typing import Optional

lazy import numpy as np
from PySide6.QtCore import Qt, Signal, QPoint
lazy from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPixmap, QCursor, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox, QSpinBox, QCheckBox,
    QGroupBox, QScrollArea, QFileDialog, QInputDialog, QMessageBox, QSlider
)

lazy from ui.editor_widgets import editor_strip, styled_label, toolbar_separator
lazy from ui.icons import line_icon
lazy from ui.qt_timers import call_later
lazy from ui.styles import current_colors, set_style_role

from . import authoring
lazy from .dxgi import describe_format
lazy from .flags import CHOICE, SWITCH, TYPE_MASK, TexSetting
lazy from .profiles import tex_profile, tex_profiles, tex_version_for_game
lazy from .texture_decoder import decode_dds_mip, decode_tex_mip, dds_info
lazy from .dds import DDS_MAGIC

# A cook carrying every member still fits in two rows.
SETTINGS_PER_ROW = 4

SETTING_GAP = 16


class DraggableLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.dragging = False
        self.last_global_pos = QPoint()
        self.scroll_area = None
        self.viewer = None

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.dragging = True
            self.last_global_pos = event.globalPosition().toPoint()
            self.setCursor(QCursor(Qt.ClosedHandCursor))
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self.dragging and self.scroll_area:
            current_global_pos = event.globalPosition().toPoint()
            delta = current_global_pos - self.last_global_pos
            self.last_global_pos = current_global_pos
            h_bar = self.scroll_area.horizontalScrollBar()
            v_bar = self.scroll_area.verticalScrollBar()
            h_bar.setValue(h_bar.value() - delta.x())
            v_bar.setValue(v_bar.value() - delta.y())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.dragging = False
            self.setCursor(QCursor(Qt.OpenHandCursor))
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event: QWheelEvent):
        if self.viewer:
            current = self.viewer.zoom_slider.value()
            delta = 10 if event.angleDelta().y() > 0 else -10
            self.viewer.zoom_slider.setValue(max(10, min(500, current + delta)))
            event.accept()
        else:
            super().wheelEvent(event)

    def enterEvent(self, event):
        self.setCursor(QCursor(Qt.OpenHandCursor))
        super().enterEvent(event)

    def leaveEvent(self, event):
        if not self.dragging:
            self.setCursor(QCursor(Qt.ArrowCursor))
        super().leaveEvent(event)


_CHECKER_TILE = 8
_checker_cache: dict = {}


def checker_brush(colors: dict) -> QBrush:
    key = (colors["surface"], colors["surface_alt"])
    if key not in _checker_cache:
        tile = QPixmap(_CHECKER_TILE * 2, _CHECKER_TILE * 2)
        tile.fill(QColor(colors["surface"]))
        painter = QPainter(tile)
        painter.fillRect(0, 0, _CHECKER_TILE, _CHECKER_TILE, QColor(colors["surface_alt"]))
        painter.fillRect(_CHECKER_TILE, _CHECKER_TILE, _CHECKER_TILE, _CHECKER_TILE, QColor(colors["surface_alt"]))
        painter.end()
        _checker_cache.clear()
        _checker_cache[key] = QBrush(tile)
    return _checker_cache[key]


class TextureCanvas(QScrollArea):
    def paintEvent(self, event):
        colors = current_colors()
        painter = QPainter(self.viewport())
        painter.fillRect(self.viewport().rect(), QColor(colors["canvas_bg"]))
        image = self.widget()
        pixmap = image.pixmap() if image is not None else None
        if pixmap is not None and not pixmap.isNull():
            painter.setBrushOrigin(image.geometry().topLeft())
            painter.fillRect(image.geometry(), checker_brush(colors))
        painter.end()


class TexViewer(QWidget):
    modified_changed = Signal(bool)

    def __init__(self, handler):
        super().__init__()
        self.setObjectName("texViewer")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.handler = handler
        self._modified = False
        self.settings: dict[str, QWidget] = {}
        self.zoom_level = 1.0
        self.source_rgba = b""
        self.source_width = 0
        self.source_height = 0
        self.original_pixmap = None
        self.is_initial_load = True
        self._setup_ui()
        self._populate()

    @property
    def modified(self):
        return self._modified

    @modified.setter
    def modified(self, value: bool):
        if self._modified != value:
            self._modified = value
            self.modified_changed.emit(value)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        toolbar, top = editor_strip()
        self.image_label = styled_label(self.tr("Image:"), tone="muted")
        top.addWidget(self.image_label)
        self.image_index = QSpinBox()
        self.image_index.setMinimum(0)
        self.image_index.valueChanged.connect(self._refresh)
        top.addWidget(self.image_index)

        self.mip_label = styled_label(self.tr("Mip:"), tone="muted")
        top.addWidget(self.mip_label)
        self.mip_index = QSpinBox()
        self.mip_index.setMinimum(0)
        self.mip_index.valueChanged.connect(self._refresh)
        top.addWidget(self.mip_index)

        self.slice_label = styled_label(self.tr("Slice:"), tone="muted")
        top.addWidget(self.slice_label)
        self.slice_index = QSpinBox()
        self.slice_index.setMinimum(0)
        self.slice_index.valueChanged.connect(self._refresh)
        top.addWidget(self.slice_index)

        channel_label = styled_label(self.tr("Channel:"), tone="muted")
        top.addWidget(channel_label)
        self.channel_box = QComboBox()
        self.channel_box.addItems(["RGBA", "R", "G", "B", "A", "RGB"])
        self.channel_box.currentIndexChanged.connect(self._apply_channel_filter)
        top.addWidget(self.channel_box)
        top.addSpacing(4)
        top.addWidget(toolbar_separator())
        top.addSpacing(4)

        zoom_title = styled_label(self.tr("Zoom:"), tone="muted")
        top.addWidget(zoom_title)
        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setMinimum(10)
        self.zoom_slider.setMaximum(500)
        self.zoom_slider.setValue(100)
        self.zoom_slider.setFixedWidth(160)
        self.zoom_slider.valueChanged.connect(self._on_zoom_changed)
        top.addWidget(self.zoom_slider)
        self.zoom_label = QLabel("100%")
        self.zoom_label.setMinimumWidth(40)
        set_style_role(self.zoom_label, tone="muted")
        top.addWidget(self.zoom_label)
        self.fit_btn = QPushButton(self.tr("Fit to Window"))
        self.fit_btn.setIcon(line_icon("fit"))
        set_style_role(self.fit_btn, compact=True)
        self.fit_btn.clicked.connect(self._fit_to_window)
        top.addWidget(self.fit_btn)
        self.reset_zoom_btn = QPushButton("100%")
        set_style_role(self.reset_zoom_btn, compact=True)
        self.reset_zoom_btn.clicked.connect(self._reset_zoom)
        top.addWidget(self.reset_zoom_btn)
        top.addStretch()
        layout.addWidget(toolbar)

        body = QVBoxLayout()
        body.setContentsMargins(8, 6, 8, 6)
        body.setSpacing(6)
        layout.addLayout(body, 1)

        self.settings_box = QGroupBox(self.tr("Settings"))
        self.settings_layout = QVBoxLayout(self.settings_box)
        self.settings_layout.setContentsMargins(6, 4, 6, 6)
        self.settings_layout.setSpacing(4)
        self.settings_content: QWidget | None = None
        self.settings_rows: list[QHBoxLayout] = []
        body.addWidget(self.settings_box)

        self.scroll = TextureCanvas()
        self.scroll.setWidgetResizable(False)
        self.scroll.setAlignment(Qt.AlignCenter)
        self.img_label = DraggableLabel()
        self.img_label.scroll_area = self.scroll
        self.img_label.viewer = self
        self.img_label.setAlignment(Qt.AlignCenter)
        self.img_label.setScaledContents(False)
        self.scroll.setWidget(self.img_label)
        body.addWidget(self.scroll, 1)

        self.info_label = QLabel("")
        self.info_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        set_style_role(self.info_label, tone="muted", mono=True)
        body.addWidget(self.info_label)

        exp = QHBoxLayout()
        exp.setSpacing(6)
        self.export_dds_btn = QPushButton(self.tr("Export DDS"))
        self.export_dds_btn.setIcon(line_icon("export"))
        self.export_dds_btn.clicked.connect(self._export_dds)
        exp.addWidget(self.export_dds_btn)
        self.export_label = QLabel("")
        self.export_label.setWordWrap(True)
        set_style_role(self.export_label, tone="muted")
        exp.addWidget(self.export_label, 1)
        body.addLayout(exp)

        author = QHBoxLayout()
        author.setSpacing(6)
        cook_label = styled_label(self.tr("Cook:"), tone="muted")
        author.addWidget(cook_label)
        self.cook_box = QComboBox()
        self.cook_box.addItem(self.tr("Choose a game..."), None)
        for profile in tex_profiles():
            self.cook_box.addItem(f"{profile.display_name} ({profile.version})", profile.version)
        author.addWidget(self.cook_box)
        self.import_dds_btn = QPushButton(self.tr("Import DDS..."))
        self.import_dds_btn.setIcon(line_icon("import"))
        self.import_dds_btn.clicked.connect(self._import_dds)
        author.addWidget(self.import_dds_btn)
        self.convert_btn = QPushButton(self.tr("Convert to this cook"))
        self.convert_btn.clicked.connect(self._convert_to_cook)
        author.addWidget(self.convert_btn)
        self.authoring_label = QLabel("")
        self.authoring_label.setWordWrap(True)
        self.authoring_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        set_style_role(self.authoring_label, tone="muted")
        author.addWidget(self.authoring_label, 1)
        body.addLayout(author)

    def _populate(self):
        tex = getattr(self.handler, 'tex', None)
        raw = getattr(self.handler, 'raw_data', b"")
        info = None
        if tex is None and raw[:4] == b'DDS ':
            try:
                info = dds_info(raw)
            except ValueError:
                info = None

        images, mips = 1, 1
        if tex is not None:
            images, mips = tex.header.image_count, tex.header.mip_count
            self.image_label.setText(self.tr("Face:") if tex.header.is_cubemap else self.tr("Image:"))
        elif info is not None:
            images, mips = info.image_count, info.mip_count
            self.image_label.setText(self.tr("Face:") if info.cubemap else self.tr("Image:"))

        for box in (self.image_index, self.mip_index, self.slice_index):
            box.blockSignals(True)
        self.image_index.setMaximum(max(0, images - 1))
        self.mip_index.setMaximum(max(0, mips - 1))
        for box in (self.image_index, self.mip_index, self.slice_index):
            box.blockSignals(False)

        self._show_selector(self.image_label, self.image_index, images > 1)
        self._show_selector(self.mip_label, self.mip_index, mips > 1)

        self.export_dds_btn.setVisible(tex is not None)
        self.export_label.setText(self._export_description(tex, info))
        self.convert_btn.setVisible(tex is not None)
        self._default_cook()
        self._build_settings(tex)
        self._refresh()

    @staticmethod
    def _show_selector(label: QLabel, box: QSpinBox, visible: bool):
        label.setVisible(visible)
        box.setVisible(visible)

    def _export_description(self, tex, info) -> str:
        if tex:
            header = tex.header
            return self.tr("Exports every image, mip and slice: {images}, {mips}{slices}.").format(
                images=self._noun(header.image_count, "face" if header.is_cubemap else "image"),
                mips=self._noun(header.mip_count, "mip"),
                slices=(
                    ", " + self._noun(header.depth, "slice") if header.is_volume else ""
                ),
            )
        if info:
            return self.tr("Already a DDS: {images} x {mips}.").format(
                images=self._noun(info.image_count, "image"),
                mips=self._noun(info.mip_count, "mip"),
            )
        return ""

    @staticmethod
    def _noun(count: int, noun: str) -> str:
        return f"{count} {noun if count == 1 else noun + 's'}"

    def _setting_text(self) -> dict[str, tuple[str, str, tuple[str, str] | None]]:
        return {
            "resident": (
                self.tr("Mip chain"),
                self.tr(
                    "The full chain ships as a separate resource under a streaming/ root; "
                    "the resident copy is a tail of it, kept beside its full texture."
                ),
                (self.tr("Full texture (streamed)"), self.tr("Resident lower-detail copy")),
            ),
            "ev": (self.tr("EV"), self.tr("Exposure the effects apply as 2 ** EV."), None),
            "texture_type": (self.tr("Type"), self.tr("The build's own via.render.TextureType."), None),
            "streaming_mips": (
                self.tr("Mips omitted"),
                self.tr("Top mip levels this copy leaves out; they are in the full texture."),
                None,
            ),
            "refiltered_normal": (
                self.tr("Tail re-filtered"),
                self.tr("This resident normal map's low mips were re-filtered, not sliced from the chain."),
                None,
            ),
            "field_18_23": (
                self.tr("Value 18-23"),
                self.tr(
                    "A 6-bit integer of unknown meaning; the engine converts it to a float."
                ),
                None,
            ),
            "opaque_flag": (self.tr("Flag 25"), self.tr("A boolean this reversal cannot name."), None),
        }

    def _build_settings(self, tex):
        self.settings = {}
        self.settings_rows = []
        self.settings_box.setVisible(tex is not None)
        if self.settings_content is not None:
            self.settings_content.setParent(None)
            self.settings_content.deleteLater()
            self.settings_content = None
        if tex is None:
            return

        settings = tex.header.tex_flags.settings(tex_profile(tex.header.version))
        texts = self._setting_text()
        content = QWidget()
        strip = QVBoxLayout(content)
        strip.setContentsMargins(0, 0, 0, 0)
        strip.setSpacing(4)
        for index, setting in enumerate(settings):
            label, tooltip, states = texts[setting.field]
            self._add_setting(index, setting, label, tooltip, states, strip)
        for row in self.settings_rows:
            row.addStretch()
        self.settings_content = content
        self.settings_layout.addWidget(content)

    def _control(self, setting: TexSetting, label: str, tooltip: str, states) -> QWidget:
        if states is not None:
            return self._choice(list(states), setting.value, tooltip)
        if setting.kind == SWITCH:
            return self._checkbox(bool(setting.value), label, tooltip)
        if setting.kind == CHOICE and setting.choices:
            return self._choice(list(setting.choices), setting.value, tooltip)
        return self._spin(setting.value, setting.maximum, tooltip)

    def _add_setting(self, index: int, setting: TexSetting, label: str, tooltip: str, states, strip: QVBoxLayout):
        control = self._control(setting, label, tooltip, states)
        signal = (
            control.toggled if isinstance(control, QCheckBox)
            else control.currentIndexChanged if isinstance(control, QComboBox)
            else control.valueChanged
        )
        signal.connect(self._apply_settings)
        if index % SETTINGS_PER_ROW == 0:
            self.settings_rows.append(QHBoxLayout())
            self.settings_rows[-1].setContentsMargins(0, 0, 0, 0)
            self.settings_rows[-1].setSpacing(SETTING_GAP)
            strip.addLayout(self.settings_rows[-1])

        cluster = QWidget()
        line = QHBoxLayout(cluster)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(4)
        if not isinstance(control, QCheckBox):
            line.addWidget(QLabel(label))
        line.addWidget(control)
        self.settings_rows[-1].addWidget(cluster)
        self.settings[setting.field] = control

    @staticmethod
    def _checkbox(checked: bool, text: str, tooltip: str) -> QCheckBox:
        box = QCheckBox(text)
        box.setChecked(checked)
        box.setToolTip(tooltip)
        return box

    @staticmethod
    def _choice(items, index: int, tooltip: str) -> QComboBox:
        box = QComboBox()
        box.addItems(items)
        box.setCurrentIndex(index)
        box.setToolTip(tooltip)
        return box

    @staticmethod
    def _spin(value: int, maximum: int, tooltip: str) -> QSpinBox:
        box = QSpinBox()
        box.setRange(0, maximum)
        box.setValue(value)
        box.setToolTip(tooltip)
        return box

    def _apply_settings(self, *_args):
        tex = getattr(self.handler, 'tex', None)
        if tex is None:
            return
        values = {field: self._value(control) for field, control in self.settings.items()}
        tex.header.flags = tex.header.tex_flags.encode(tex_profile(tex.header.version), **values)
        self.modified = True
        self._refresh()

    @staticmethod
    def _value(control: QWidget):
        if isinstance(control, QCheckBox):
            return control.isChecked()
        if isinstance(control, QComboBox):
            return control.currentIndex()
        return control.value()

    def _mip_slice_range(self, slices: int) -> int:
        self.slice_index.blockSignals(True)
        self.slice_index.setMaximum(slices - 1)
        self.slice_index.blockSignals(False)
        self._show_selector(self.slice_label, self.slice_index, slices > 1)
        return self.slice_index.value()

    def _refresh(self):
        tex = getattr(self.handler, 'tex', None)
        dds = b"" if tex is not None else self.handler.build_dds_bytes()
        if tex is None and not dds:
            return
        image, mip = self.image_index.value(), self.mip_index.value()
        try:
            info = None if tex else dds_info(dds)
            slices = tex.header.slice_count(mip) if tex else max(1, info.depth >> mip)
            slice_index = self._mip_slice_range(slices)
            decoded, dxgi_format, shape = self._subresource(tex, dds, info, image, mip, slice_index)
        except Exception as error:
            self._show_failure(self.tr("Failed to decode: {error}").format(error=error))
            return

        d = describe_format(dxgi_format)
        parts = [
            f"{decoded.width}x{decoded.height}",
            f"slice {slice_index + 1}/{slices}" if slices > 1 else "",
            f"{d['name']} | {'BC' if d['compressed'] else 'RGB'} | {d['bits_per_pixel']}bpp",
            shape,
        ]
        self.info_label.setText(" | ".join(part for part in parts if part))
        set_style_role(self.info_label, tone="muted")
        self.source_rgba = decoded.rgba
        self.source_width = decoded.width
        self.source_height = decoded.height
        self._apply_channel_filter()

    @staticmethod
    def _subresource(tex, dds: bytes, info, image: int, mip: int, slice_index: int):
        if tex is not None:
            return decode_tex_mip(tex, image, mip, slice_index), tex.header.format, ""
        shape = "volume" if info.is_volume else "cubemap" if info.cubemap else "2D"
        return decode_dds_mip(dds, mip, image, slice_index), info.dxgi_format, shape

    def _show_failure(self, message: str):
        self.info_label.setText(message)
        set_style_role(self.info_label, tone="danger")
        self.source_rgba = b""
        self.source_width = 0
        self.source_height = 0
        self.original_pixmap = None
        self.img_label.clear()

    def _apply_channel_filter(self):
        if not self.source_rgba or self.source_width <= 0 or self.source_height <= 0:
            return
        ch = self.channel_box.currentText()
        filtered_raw = self.source_rgba if ch == 'RGBA' else self._filter_channels(self.source_rgba, ch)
        filtered = QPixmap.fromImage(
            QImage(filtered_raw, self.source_width, self.source_height, QImage.Format_RGBA8888).copy()
        )
        self.original_pixmap = filtered
        if self.is_initial_load:
            self.is_initial_load = False
            call_later(self, 0, self._fit_to_window)
        else:
            self._apply_zoom()

    @staticmethod
    def _filter_channels(raw: bytes, channel: str) -> bytes:
        px = np.frombuffer(raw, dtype=np.uint8).reshape((-1, 4))
        out = np.empty_like(px)

        if channel == 'R':
            out[:, :3] = px[:, [0, 0, 0]]
            out[:, 3] = 255
        elif channel == 'G':
            out[:, :3] = px[:, [1, 1, 1]]
            out[:, 3] = 255
        elif channel == 'B':
            out[:, :3] = px[:, [2, 2, 2]]
            out[:, 3] = 255
        elif channel == 'A':
            out[:, :3] = px[:, [3, 3, 3]]
            out[:, 3] = 255
        elif channel == 'RGB':
            out[:, :3] = px[:, :3]
            out[:, 3] = 255
        else:
            raise ValueError(f"Unsupported channel filter: {channel}")

        return out.tobytes()

    def _on_zoom_changed(self, value):
        self.zoom_level = value / 100.0
        self.zoom_label.setText(f"{value}%")
        self._apply_zoom()

    def _apply_zoom(self):
        if not self.original_pixmap:
            return
        if abs(self.zoom_level - 1.0) < 0.0001:
            pixmap = self.original_pixmap
        else:
            pixmap = self.original_pixmap.scaled(
                int(self.original_pixmap.width() * self.zoom_level),
                int(self.original_pixmap.height() * self.zoom_level),
                Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        self.img_label.setPixmap(pixmap)
        self.img_label.setFixedSize(pixmap.size())

    def _fit_to_window(self):
        if not self.original_pixmap:
            return
        viewport = self.scroll.viewport().size()
        w = max(100, viewport.width() - 30)
        h = max(100, viewport.height() - 30)
        img_w = self.original_pixmap.width()
        img_h = self.original_pixmap.height()
        if img_w == 0 or img_h == 0:
            return
        fit_ratio = min(w / img_w, h / img_h, 1.0)
        scaled = self.original_pixmap.scaled(
            int(img_w * fit_ratio), int(img_h * fit_ratio),
            Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        self.img_label.setPixmap(scaled)
        self.img_label.setFixedSize(scaled.size())
        zoom_percent = int(fit_ratio * 100)
        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(zoom_percent)
        self.zoom_label.setText(f"{zoom_percent}%")
        self.zoom_slider.blockSignals(False)
        self.zoom_level = fit_ratio

    def _reset_zoom(self):
        self.zoom_slider.setValue(100)

    def _default_cook(self):
        tex = getattr(self.handler, "tex", None)
        app = getattr(self.handler, "app", None)
        version = tex.header.version if tex is not None else tex_version_for_game(
            str(getattr(self.handler, "game_version", "") or getattr(app, "current_game", ""))
        )
        index = self.cook_box.findData(version) if version is not None else -1
        if index >= 0:
            self.cook_box.setCurrentIndex(index)

    def _chosen_cook(self) -> Optional[int]:
        version = self.cook_box.currentData()
        if version is None:
            self._show_failure(self.tr("Choose a game before importing a DDS."))
        return version

    def _import_dds(self):
        path, _ = QFileDialog.getOpenFileName(self, self.tr("Import DDS"), "", "DDS files (*.dds)")
        if not path:
            return
        try:
            data = Path(path).read_bytes()
        except OSError as error:
            self._show_failure(str(error))
            return
        version = self._chosen_cook()
        if version is None:
            return
        stem = Path(path).stem
        if getattr(self.handler, "tex", None) is not None:
            self._author(self.handler.import_dds, data, version=version, name=Path(path).name)
        else:
            self._write_new_tex(lambda **type_choice: authoring.import_dds(
                data, version=version, name=stem, **type_choice), stem, version)

    def _convert_to_cook(self):
        version = self._chosen_cook()
        if version is not None:
            self._author(self.handler.convert_to, version=version)

    def _run_authoring(self, call):
        try:
            return call()
        except authoring.TypeNeedsChoice as choice:
            picked = self._choose_type(choice)
            return None if picked is None else call(**picked)
        except authoring.AuthoringError as error:
            self._show_failure(str(error))
            return None

    def _author(self, action, *args, **kwargs):
        notes = self._run_authoring(lambda **picked: action(*args, **{**kwargs, **picked}))
        if notes is None:
            return
        self._populate()
        self.modified = True
        self._sync_tab_path()
        self._describe_authoring(notes, self.handler.tex.header.version)

    def _sync_tab_path(self):
        app = self.handler.app
        tab = app._resolve_tab_from_widget(self) if app is not None else None
        if tab is None or tab.handler is not self.handler:
            return
        tab.filename = self.handler.filepath
        tab.update_tab_title()

    def _write_new_tex(self, author, stem: str, version: int):
        suggested = f"{stem}.tex.{version}"
        path, _ = QFileDialog.getSaveFileName(
            self, self.tr("Save .tex"), suggested, "TEX files (*.tex)"
        )
        if not path:
            return
        authored = self._run_authoring(author)
        if authored is None:
            return
        try:
            Path(path).write_bytes(authored.data)
        except OSError as error:
            self._show_failure(str(error))
            return
        self._describe_authoring(authored.notes, authored.version, path)
        app = getattr(self.handler, "app", None)
        if app is not None and hasattr(app, "add_tab"):
            app.add_tab(filename=path)

    def _choose_type(self, choice: authoring.TypeNeedsChoice):
        if choice.options:
            name, accepted = QInputDialog.getItem(
                self, self.tr("Texture type"), str(choice), list(choice.options), 0, False
            )
            return {"type_name": name} if accepted else None
        index, accepted = QInputDialog.getInt(
            self, self.tr("Texture type index"), str(choice), 0, 0, TYPE_MASK
        )
        return {"type_index": index} if accepted else None

    def _describe_authoring(self, notes, version: int, path: str = ""):
        written = self.tr("Written as {name}.").format(
            name=Path(path or str(getattr(self.handler, "filepath", "") or version)).name
        )
        self.authoring_label.setText(" ".join([written, *notes]))

    def _export_dds(self):
        raw = getattr(self.handler, 'raw_data', b"")
        if raw[:4] == DDS_MAGIC.to_bytes(4, 'little'):
            return

        try:
            dds = self.handler.build_dds_bytes()
        except ValueError as error:
            QMessageBox.warning(self, self.tr("Export DDS"), str(error))
            return
        if not dds:
            QMessageBox.warning(self, self.tr("Export DDS"), self.tr("No TEX data loaded."))
            return

        path, _ = QFileDialog.getSaveFileName(self, self.tr("Save DDS"), "", "DDS files (*.dds)")
        if not path:
            return

        with open(path, 'wb') as f:
            f.write(dds)
