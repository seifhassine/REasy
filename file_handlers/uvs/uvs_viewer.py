from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QTimer, QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFileDialog,
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QComboBox,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QSizePolicy,
)

from .uvs_file import UvsPattern, UvsSequence, UvsTexture
from file_handlers.tex.tex_handler import TexHandler
from file_handlers.tex.texture_decoder import decode_tex_mip
from file_handlers.pak.reader import CachedPakReader
from ui.project_manager.constants import EXPECTED_NATIVE


class UvsPreviewWidget(QWidget):
    pattern_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(260)
        self._uvs = None
        self._sequence_index = -1
        self._pattern_index = -1
        self._pixmaps: dict[int, QPixmap] = {}
        self._missing_state: dict[int, bool] = {}

        self._drag_mode = None  # move|lt|rt|lb|rb
        self._drag_start = QPointF()
        self._drag_origin = (0.0, 0.0, 0.0, 0.0)
        self.setMouseTracking(True)
        self._crop_selected_only = False
        self._interaction_enabled = True
        self._show_cutouts = False
        self._edit_cutouts = False
        self._rect_edit_enabled = True
        self._drag_cutout_index = -1

    def set_uvs(self, uvs):
        self._uvs = uvs
        self.update()

    def set_selection(self, sequence_index: int, pattern_index: int):
        self._sequence_index = sequence_index
        self._pattern_index = pattern_index
        self.update()

    def set_texture_pixmap(self, tex_idx: int, pixmap: QPixmap | None):
        if pixmap is None:
            self._pixmaps.pop(tex_idx, None)
        else:
            self._pixmaps[tex_idx] = pixmap
        self.update()

    def set_texture_missing(self, tex_idx: int, missing: bool):
        self._missing_state[tex_idx] = bool(missing)
        self.update()

    def set_crop_selected_only(self, enabled: bool):
        self._crop_selected_only = bool(enabled)
        self.update()

    def set_interaction_enabled(self, enabled: bool):
        self._interaction_enabled = bool(enabled)

    def set_show_cutouts(self, enabled: bool):
        self._show_cutouts = bool(enabled)
        self.update()

    def set_edit_cutouts(self, enabled: bool):
        self._edit_cutouts = bool(enabled)

    def set_rect_edit_enabled(self, enabled: bool):
        self._rect_edit_enabled = bool(enabled)

    def _current_pattern(self):
        if not self._uvs:
            return None
        if self._sequence_index < 0 or self._sequence_index >= len(self._uvs.sequences):
            return None
        seq = self._uvs.sequences[self._sequence_index]
        if self._pattern_index < 0 or self._pattern_index >= len(seq.patterns):
            return None
        return seq.patterns[self._pattern_index]

    def _pattern_rect(self, pat: UvsPattern, draw_rect: QRectF) -> QRectF:
        left = draw_rect.left() + pat.left * draw_rect.width()
        right = draw_rect.left() + pat.right * draw_rect.width()
        top = draw_rect.top() + pat.top * draw_rect.height()
        bottom = draw_rect.top() + pat.bottom * draw_rect.height()
        return QRectF(QPointF(left, top), QPointF(right, bottom)).normalized()

    def _find_draw_area(self, pm: QPixmap | None) -> QRectF:
        margin = 10
        avail = QRectF(margin, margin, max(1, self.width() - margin * 2), max(1, self.height() - margin * 2))
        if not pm or pm.isNull():
            return avail
        scale = min(avail.width() / pm.width(), avail.height() / pm.height())
        w = pm.width() * scale
        h = pm.height() * scale
        x = avail.left() + (avail.width() - w) / 2
        y = avail.top() + (avail.height() - h) / 2
        return QRectF(x, y, w, h)

    def _rect_handles(self, r: QRectF):
        s = 5
        return {
            "lt": QRectF(r.left() - s, r.top() - s, 2 * s, 2 * s),
            "rt": QRectF(r.right() - s, r.top() - s, 2 * s, 2 * s),
            "lb": QRectF(r.left() - s, r.bottom() - s, 2 * s, 2 * s),
            "rb": QRectF(r.right() - s, r.bottom() - s, 2 * s, 2 * s),
        }


    def _uv_to_widget(self, pat: UvsPattern, draw_area: QRectF, u: float, v: float) -> QPointF:
        if self._crop_selected_only:
            w = max(1e-6, pat.right - pat.left)
            h = max(1e-6, pat.bottom - pat.top)
            x = draw_area.left() + ((u - pat.left) / w) * draw_area.width()
            y = draw_area.top() + ((v - pat.top) / h) * draw_area.height()
            return QPointF(x, y)
        return QPointF(draw_area.left() + u * draw_area.width(), draw_area.top() + v * draw_area.height())

    def _widget_to_uv(self, pat: UvsPattern, draw_area: QRectF, pos: QPointF) -> tuple[float, float]:
        if self._crop_selected_only:
            w = max(1e-6, pat.right - pat.left)
            h = max(1e-6, pat.bottom - pat.top)
            ru = (pos.x() - draw_area.left()) / max(1e-6, draw_area.width())
            rv = (pos.y() - draw_area.top()) / max(1e-6, draw_area.height())
            return pat.left + ru * w, pat.top + rv * h
        return self._to_uv(pos, draw_area)

    def _hit_cutout(self, pat: UvsPattern, draw_area: QRectF, pos: QPointF) -> int:
        best = -1
        best_d2 = 10.0 * 10.0
        for i, (u, v) in enumerate(pat.cutout_uvs):
            pt = self._uv_to_widget(pat, draw_area, u, v)
            dx = pt.x() - pos.x()
            dy = pt.y() - pos.y()
            d2 = dx * dx + dy * dy
            if d2 <= best_d2:
                best = i
                best_d2 = d2
        return best

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(30, 30, 30))

        pat = self._current_pattern()
        pm = self._pixmaps.get(pat.texture_index) if pat and pat.texture_index >= 0 else None
        draw_area = self._find_draw_area(pm)

        p.fillRect(draw_area, QColor(60, 60, 60))
        if not pat:
            return

        if pm and not pm.isNull():
            if self._crop_selected_only and pat:
                src = self._pattern_rect(pat, QRectF(0, 0, pm.width(), pm.height())).toRect()
                src = src.intersected(pm.rect())
                if src.width() > 0 and src.height() > 0:
                    p.drawPixmap(draw_area.toRect(), pm, src)
                else:
                    p.drawPixmap(draw_area.toRect(), pm)
            else:
                p.drawPixmap(draw_area.toRect(), pm)
        else:
            if self._missing_state.get(pat.texture_index, False):
                p.setPen(QPen(QColor(190, 190, 190)))
                p.drawText(draw_area, Qt.AlignmentFlag.AlignCenter, "No preview texture found in loaded PAKs")

        if pat and self._show_cutouts and pat.cutout_uvs:
            pts = [self._uv_to_widget(pat, draw_area, u, v) for (u, v) in pat.cutout_uvs]
            p.setPen(QPen(QColor(255, 105, 180), 2))
            for i in range(len(pts)):
                a = pts[i]
                b = pts[(i + 1) % len(pts)] if len(pts) > 2 else pts[i]
                if i + 1 < len(pts) or len(pts) > 2:
                    p.drawLine(a, b)
            p.setBrush(QColor(255, 105, 180))
            for pt in pts:
                p.drawEllipse(pt, 4, 4)

        if not self._uvs or self._sequence_index < 0 or self._sequence_index >= len(self._uvs.sequences):
            return

        if self._crop_selected_only:
            return

        seq = self._uvs.sequences[self._sequence_index]
        for i, sp in enumerate(seq.patterns):
            rr = self._pattern_rect(sp, draw_area)
            if i == self._pattern_index:
                p.setPen(QPen(QColor(255, 215, 0), 2))
                p.drawRect(rr)
                p.setBrush(QColor(255, 215, 0))
                for h in self._rect_handles(rr).values():
                    p.drawRect(h)
            else:
                p.setPen(QPen(QColor(80, 220, 255), 1))
                p.drawRect(rr)

    def _to_uv(self, pos: QPointF, draw_rect: QRectF) -> tuple[float, float]:
        u = (pos.x() - draw_rect.left()) / max(1e-6, draw_rect.width())
        v = (pos.y() - draw_rect.top()) / max(1e-6, draw_rect.height())
        return u, v

    def mousePressEvent(self, e):
        if not self._interaction_enabled:
            return
        pat = self._current_pattern()
        if not pat:
            return
        pm = self._pixmaps.get(pat.texture_index) if pat.texture_index >= 0 else None
        draw_rect = self._find_draw_area(pm)
        pr = self._pattern_rect(pat, draw_rect)

        if self._edit_cutouts and pat.cutout_uvs:
            idx = self._hit_cutout(pat, draw_rect, QPointF(e.position()))
            if idx >= 0:
                self._drag_mode = "cutout"
                self._drag_cutout_index = idx
                self._drag_start = QPointF(e.position())
                return

        if self._rect_edit_enabled:
            for k, hr in self._rect_handles(pr).items():
                if hr.contains(e.position()):
                    self._drag_mode = k
                    break
            if self._drag_mode is None and pr.contains(e.position()):
                self._drag_mode = "move"

            self._drag_start = QPointF(e.position())
            self._drag_origin = (pat.left, pat.top, pat.right, pat.bottom)

    def mouseMoveEvent(self, e):
        if not self._interaction_enabled:
            return
        if not self._drag_mode:
            return
        pat = self._current_pattern()
        if not pat:
            return
        pm = self._pixmaps.get(pat.texture_index) if pat.texture_index >= 0 else None
        draw_rect = self._find_draw_area(pm)
        if self._drag_mode == "cutout" and 0 <= self._drag_cutout_index < len(pat.cutout_uvs):
            u, v = self._widget_to_uv(pat, draw_rect, QPointF(e.position()))
            pat.cutout_uvs[self._drag_cutout_index] = (u, v)
            self.pattern_changed.emit()
            self.update()
            return

        du, dv = self._to_uv(e.position(), draw_rect)
        su, sv = self._to_uv(self._drag_start, draw_rect)
        dx, dy = du - su, dv - sv

        l0, t0, r0, b0 = self._drag_origin
        if self._drag_mode == "move":
            w = r0 - l0
            h = b0 - t0
            nl = l0 + dx
            nt = t0 + dy
            pat.left = nl
            pat.top = nt
            pat.right = nl + w
            pat.bottom = nt + h
        elif self._drag_mode == "lt":
            pat.left = l0 + dx
            pat.top = t0 + dy
        elif self._drag_mode == "rt":
            pat.right = r0 + dx
            pat.top = t0 + dy
        elif self._drag_mode == "lb":
            pat.left = l0 + dx
            pat.bottom = b0 + dy
        elif self._drag_mode == "rb":
            pat.right = r0 + dx
            pat.bottom = b0 + dy

        if pat.left > pat.right:
            pat.left, pat.right = pat.right, pat.left
        if pat.top > pat.bottom:
            pat.top, pat.bottom = pat.bottom, pat.top

        self.pattern_changed.emit()
        self.update()

    def mouseReleaseEvent(self, _):
        if not self._interaction_enabled:
            return
        if self._drag_mode:
            self.pattern_changed.emit()
        self._drag_mode = None
        self._drag_cutout_index = -1


class UvsViewer(QWidget):
    modified_changed = Signal(bool)

    def __init__(self, handler):
        super().__init__()
        self.handler = handler
        self._modified = False
        self._loading = False
        self._selected_sequence = -1
        self._selected_pattern = -1

        self.textures_table: QTableWidget | None = None
        self.normal_path_edit: QLineEdit | None = None
        self.specular_path_edit: QLineEdit | None = None
        self.alpha_path_edit: QLineEdit | None = None
        self.sequences_table: QTableWidget | None = None
        self.patterns_table: QTableWidget | None = None
        self.cutouts_table: QTableWidget | None = None
        self.preview: UvsPreviewWidget | None = None
        self.frame_slider: QSlider | None = None
        self.play_btn: QPushButton | None = None
        self.fps_spin: QSpinBox | None = None
        self.loop_chk: QCheckBox | None = None
        self.preview_focused: UvsPreviewWidget | None = None
        self.focused_cutout_chk: QCheckBox | None = None
        self.preview_source_combo: QComboBox | None = None

        self._tex_pixmap_cache: dict[tuple[int, str], QPixmap | None] = {}
        self._tex_path_cache: dict[tuple[int, str], str] = {}
        self._custom_tex_pixmap: dict[int, QPixmap | None] = {}
        self._custom_tex_file: dict[int, str] = {}

        self.play_timer = QTimer(self)
        self.play_timer.timeout.connect(self._advance_frame)

        self._setup_ui()
        self._reload_all()

    @property
    def modified(self):
        return self._modified

    @modified.setter
    def modified(self, value: bool):
        if self._modified != value:
            self._modified = value
            self.modified_changed.emit(value)

    def _mark_modified(self):
        self.modified = True
        self.handler.modified = True

    def _set_loading(self, value: bool):
        self._loading = value

    def _set_table_item(self, table: QTableWidget, row: int, col: int, txt: str, editable=True):
        item = QTableWidgetItem(txt)
        if not editable:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        table.setItem(row, col, item)

    def _set_pattern_row_values(self, row: int, pat: UvsPattern):
        for col, val in enumerate([pat.left, pat.top, pat.right, pat.bottom], 2):
            self._set_table_item(self.patterns_table, row, col, str(val))

    def _refresh_pattern_views(self):
        self._set_loading(True)
        self._reload_patterns()
        self._reload_cutouts()
        self._set_loading(False)

    def _create_detail_field(self, layout: QVBoxLayout, label: str, placeholder: str, callback) -> QLineEdit:
        layout.addWidget(QLabel(f"   {label}"))
        field = QLineEdit()
        field.setPlaceholderText(placeholder)
        field.editingFinished.connect(callback)
        layout.addWidget(field)
        return field

    def _setup_ui(self):
        root = QVBoxLayout(self)
        title = QLabel("🧭 UVS Editor")
        root.addWidget(title)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter)

        left = QWidget()
        left_layout = QVBoxLayout(left)

        top_bottom = QSplitter(Qt.Orientation.Vertical)

        textures_panel = QWidget()
        textures_layout = QVBoxLayout(textures_panel)
        textures_layout.addWidget(QLabel("Textures"))
        textures_layout.addLayout(self._toolbar(self._add_texture, self._remove_texture, extra=[("Custom Texture", self._load_custom_texture_for_selected), ("Clear Custom Texture", self._clear_custom_texture_for_selected)]))
        self.textures_table = self._table(["Index", "Albedo Path", "State"])
        self.textures_table.itemChanged.connect(self._on_texture_changed)
        self.textures_table.itemSelectionChanged.connect(self._on_texture_selected)
        textures_layout.addWidget(self.textures_table)

        details_row = QVBoxLayout()
        details_row.setSpacing(4)
        self.normal_path_edit = self._create_detail_field(details_row, "Normal Path", "Normal Path", self._on_texture_detail_changed)
        self.specular_path_edit = self._create_detail_field(details_row, "Specular Path", "Specular Path", self._on_texture_detail_changed)
        self.alpha_path_edit = self._create_detail_field(details_row, "Alpha Path", "Alpha Path", self._on_texture_detail_changed)
        textures_layout.addLayout(details_row)

        sequences_panel = QWidget()
        sequences_layout = QVBoxLayout(sequences_panel)
        sequences_layout.addWidget(QLabel("Sequences"))
        sequences_layout.addLayout(self._toolbar(self._add_sequence, self._remove_sequence))
        self.sequences_table = self._table(["Index", "Pattern Count"])
        self.sequences_table.itemSelectionChanged.connect(self._on_sequence_selected)
        sequences_layout.addWidget(self.sequences_table)

        top_bottom.addWidget(textures_panel)
        top_bottom.addWidget(sequences_panel)
        top_bottom.setSizes([1, 1])
        left_layout.addWidget(top_bottom)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(QLabel("Patterns"))
        right_layout.addLayout(self._toolbar(self._add_pattern, self._remove_pattern))
        self.patterns_table = self._table(["Index", "Flags", "Left", "Top", "Right", "Bottom", "Texture", "Cutouts"])
        self.patterns_table.itemChanged.connect(self._on_pattern_changed)
        self.patterns_table.itemSelectionChanged.connect(self._on_pattern_selected)
        right_layout.addWidget(self.patterns_table)

        cutouts_label = QLabel("Cutout UVs")
        right_layout.addWidget(cutouts_label)
        right_layout.addLayout(self._toolbar(self._add_cutout, self._remove_cutout))
        self.cutouts_table = self._table(["Index", "U", "V"])
        self.cutouts_table.itemChanged.connect(self._on_cutout_changed)
        right_layout.addWidget(self.cutouts_table)

        preview_group = QGroupBox("Playback")
        pv_layout = QVBoxLayout(preview_group)
        ctl = QHBoxLayout()
        self.play_btn = QPushButton("▶ Play")
        self.play_btn.clicked.connect(self._toggle_play)
        self.frame_slider = QSlider(Qt.Orientation.Horizontal)
        self.frame_slider.valueChanged.connect(self._on_frame_slider)
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 120)
        self.fps_spin.setValue(60)
        self.loop_chk = QCheckBox("Loop")
        self.loop_chk.setChecked(True)
        self.focused_cutout_chk = QCheckBox("Show Cutouts in Focused View")
        self.focused_cutout_chk.toggled.connect(self._on_focused_cutout_toggled)
        self.preview_source_combo = QComboBox()
        self.preview_source_combo.addItems(["Albedo", "Normal", "Specular", "Alpha"])
        self.preview_source_combo.currentIndexChanged.connect(self._on_preview_source_changed)
        ctl.addWidget(self.play_btn)
        ctl.addWidget(QLabel("Frame"))
        ctl.addWidget(self.frame_slider, 1)
        ctl.addWidget(QLabel("FPS"))
        ctl.addWidget(self.fps_spin)
        ctl.addWidget(self.loop_chk)
        ctl.addWidget(QLabel("Source"))
        ctl.addWidget(self.preview_source_combo)
        ctl.addWidget(self.focused_cutout_chk)
        pv_layout.addLayout(ctl)

        previews_row = QSplitter(Qt.Orientation.Horizontal)

        self.preview = UvsPreviewWidget(self)
        self.preview.pattern_changed.connect(self._on_preview_pattern_changed)

        self.preview_focused = UvsPreviewWidget(self)
        self.preview_focused.set_crop_selected_only(True)
        self.preview_focused.set_interaction_enabled(False)
        self.preview_focused.set_rect_edit_enabled(False)
        self.preview_focused.pattern_changed.connect(self._on_preview_pattern_changed)

        previews_row.addWidget(self.preview)
        previews_row.addWidget(self.preview_focused)
        previews_row.setSizes([1, 1])
        pv_layout.addWidget(previews_row)

        right_layout.addWidget(preview_group)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([520, 970])

    def _toolbar(self, add_cb, remove_cb, extra=None):
        row = QHBoxLayout()
        add_btn = QPushButton("➕")
        self._icon_toolbar_button(add_btn)
        add_btn.clicked.connect(add_cb)
        rm_btn = QPushButton("🗑️")
        self._icon_toolbar_button(rm_btn)
        rm_btn.clicked.connect(remove_cb)
        row.addWidget(add_btn)
        row.addWidget(rm_btn)
        if extra:
            for label, cb in extra:
                btn = QPushButton(label)
                self._compact_button(btn)
                btn.clicked.connect(cb)
                row.addWidget(btn)
        row.addStretch()
        return row

    def _icon_toolbar_button(self, btn: QPushButton):
        btn.setStyleSheet("QPushButton { min-width: 0px; max-width: 24px; padding: 0px; }")
        btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        btn.setFixedSize(24, 24)

    def _compact_button(self, btn: QPushButton):
        btn.setStyleSheet("QPushButton { min-width: 0px; }")
        btn.setMinimumWidth(0)
        btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        btn.setFixedWidth(btn.sizeHint().width())

    def _table(self, headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        hdr = table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hdr.setStretchLastSection(True)
        return table

    def _read_int(self, txt: str) -> int:
        txt = txt.strip()
        return int(txt, 16) if txt.lower().startswith("0x") else int(txt)

    def _reload_all(self):
        self._loading = True
        self._reload_textures()
        self._reload_sequences()
        self._reload_patterns()
        self._reload_cutouts()
        self._sync_preview()
        self._on_texture_selected()
        self._loading = False

    def _reload_textures(self):
        self.textures_table.setRowCount(0)
        if not (uvs := self.handler.uvs):
            return
        for i, tex in enumerate(uvs.textures):
            self.textures_table.insertRow(i)
            self._set_table_item(self.textures_table, i, 0, str(i), editable=False)
            self._set_table_item(self.textures_table, i, 1, tex.path)
            self._set_table_item(self.textures_table, i, 2, str(tex.state_holder))

    def _reload_sequences(self):
        self.sequences_table.setRowCount(0)
        if not (uvs := self.handler.uvs):
            return
        for i, seq in enumerate(uvs.sequences):
            self.sequences_table.insertRow(i)
            self._set_table_item(self.sequences_table, i, 0, str(i), editable=False)
            self._set_table_item(self.sequences_table, i, 1, str(len(seq.patterns)), editable=False)

    def _reload_patterns(self):
        self.patterns_table.setRowCount(0)
        if not (uvs := self.handler.uvs) or self._selected_sequence < 0 or self._selected_sequence >= len(uvs.sequences):
            return
        for i, pat in enumerate(uvs.sequences[self._selected_sequence].patterns):
            self.patterns_table.insertRow(i)
            for col, val in enumerate([i, pat.flags, pat.left, pat.top, pat.right, pat.bottom, pat.texture_index, len(pat.cutout_uvs)]):
                self._set_table_item(self.patterns_table, i, col, str(val), editable=(col not in (0, 7)))

    def _reload_cutouts(self):
        self.cutouts_table.setRowCount(0)
        if not (pat := self._current_pattern()):
            return
        for i, (u, v) in enumerate(pat.cutout_uvs):
            self.cutouts_table.insertRow(i)
            self._set_table_item(self.cutouts_table, i, 0, str(i), editable=False)
            self._set_table_item(self.cutouts_table, i, 1, str(u))
            self._set_table_item(self.cutouts_table, i, 2, str(v))

    def _sync_preview(self):
        for preview in (self.preview, self.preview_focused):
            if preview:
                preview.set_uvs(self.handler.uvs)
                preview.set_selection(self._selected_sequence, self._selected_pattern)
        self._ensure_texture_for_selected_pattern()
        self._update_frame_slider()

    def _update_frame_slider(self):
        seq = self._current_sequence()
        max_idx = max(0, len(seq.patterns) - 1 if seq else 0)
        self.frame_slider.blockSignals(True)
        self.frame_slider.setMinimum(0)
        self.frame_slider.setMaximum(max_idx)
        self._selected_pattern = min(self._selected_pattern, max_idx)
        self.frame_slider.setValue(max(0, self._selected_pattern))
        self.frame_slider.blockSignals(False)

    def _preview_source_key(self) -> str:
        if not self.preview_source_combo:
            return "albedo"
        txt = self.preview_source_combo.currentText().strip().lower()
        return txt if txt in ("albedo", "normal", "specular", "alpha") else "albedo"

    def _on_preview_source_changed(self):
        if self._loading:
            return
        self._sync_preview()

    def _clear_texture_preview_cache(self, row: int):
        self._tex_pixmap_cache = {k: v for k, v in self._tex_pixmap_cache.items() if k[0] != row}
        self._tex_path_cache = {k: v for k, v in self._tex_path_cache.items() if k[0] != row}

    def _on_texture_selected(self):
        uvs = self.handler.uvs
        row = self.textures_table.currentRow()
        if not uvs or row < 0 or row >= len(uvs.textures):
            self._set_texture_detail_fields(None)
            return
        self._set_texture_detail_fields(uvs.textures[row])

    def _set_texture_detail_fields(self, tex: UvsTexture | None):
        self._set_loading(True)
        for field, attr in [(self.normal_path_edit, 'normal_path'), 
                            (self.specular_path_edit, 'specular_path'), 
                            (self.alpha_path_edit, 'alpha_path')]:
            if field:
                field.setText(getattr(tex, attr, "") if tex else "")
        self._set_loading(False)

    def _on_texture_detail_changed(self):
        if self._loading or not (uvs := self.handler.uvs):
            return
        row = self.textures_table.currentRow()
        if row < 0 or row >= len(uvs.textures):
            return
        tex = uvs.textures[row]
        tex.normal_path = self.normal_path_edit.text() if self.normal_path_edit else ""
        tex.specular_path = self.specular_path_edit.text() if self.specular_path_edit else ""
        tex.alpha_path = self.alpha_path_edit.text() if self.alpha_path_edit else ""
        self._clear_texture_preview_cache(row)
        self._custom_tex_pixmap.pop(row, None)
        self._custom_tex_file.pop(row, None)
        self._mark_modified()
        self._sync_preview()

    def _on_sequence_selected(self):
        row = self.sequences_table.currentRow()
        self._selected_sequence = row
        self._selected_pattern = 0
        self._loading = True
        self._reload_patterns()
        self._reload_cutouts()
        self._sync_preview()
        if self.patterns_table.rowCount() > 0:
            self.patterns_table.selectRow(0)
        self._loading = False

    def _on_pattern_selected(self):
        self._selected_pattern = self.patterns_table.currentRow()
        self._loading = True
        self._reload_cutouts()
        self._sync_preview()
        self.frame_slider.blockSignals(True)
        self.frame_slider.setValue(max(0, self._selected_pattern))
        self.frame_slider.blockSignals(False)
        self._loading = False

    def _current_sequence(self):
        uvs = self.handler.uvs
        if not uvs or self._selected_sequence < 0 or self._selected_sequence >= len(uvs.sequences):
            return None
        return uvs.sequences[self._selected_sequence]

    def _current_pattern(self):
        seq = self._current_sequence()
        if not seq or self._selected_pattern < 0 or self._selected_pattern >= len(seq.patterns):
            return None
        return seq.patterns[self._selected_pattern]

    def _project_mode_cached_reader(self):
        app = getattr(self.handler, "app", None)
        proj = getattr(app, "proj_dock", None)
        if proj is None:
            return None
        if getattr(proj, "_active_tab", "") != "pak":
            return None

        reader = getattr(proj, "_pak_cached_reader", None)
        if reader:
            return reader

        selected = list(getattr(proj, "_pak_selected_paks", []) or [])
        if not selected:
            return None

        reader = CachedPakReader()
        reader.pak_file_priority = selected
        base_paths = list(getattr(proj, "_pak_base_paths", []) or [])
        if base_paths:
            reader.add_files(*base_paths)
            reader.cache_entries(assign_paths=True)
        else:
            reader.cache_entries(assign_paths=False)
        proj._pak_cached_reader = reader
        return reader

    def _normalize_uvs_path(self, p: str) -> str:
        s = (p or "").strip().replace("\\", "/")
        if s.startswith("@"):
            s = s[1:]
        while s.startswith("/"):
            s = s[1:]
        return s.lower()

    def _game_native_prefix(self) -> str:
        app = getattr(self.handler, "app", None)
        game = str(getattr(app, "settings", {}).get("game_version", "RE4"))
        parts = EXPECTED_NATIVE.get(game, ("natives", "stm"))
        return "/".join(parts).strip("/") + "/"

    def _prefixed_texture_path(self, raw_path: str) -> str:
        base = self._normalize_uvs_path(raw_path)
        if not base:
            return ""
        return self._game_native_prefix() + base

    def _resolve_pak_path_for_texture(self, tex_idx: int, source: str) -> str | None:
        if not (uvs := self.handler.uvs) or tex_idx < 0 or tex_idx >= len(uvs.textures):
            return None

        tex = uvs.textures[tex_idx]
        raw_path = {'normal': tex.normal_path, 'specular': tex.specular_path, 'alpha': tex.alpha_path}.get(source, tex.path)

        if not (candidate := self._prefixed_texture_path(raw_path)):
            return None

        if not (reader := self._project_mode_cached_reader()):
            return None

        pref = candidate.lower()
        return next((path for path in reader.cached_paths(include_unknown=False) if path.lower().startswith(pref + ".")), None)
    def _decode_tex_to_pixmap(self, tex_bytes: bytes) -> QPixmap | None:
        th = TexHandler()
        th.read(tex_bytes)
        if not th.tex:
            return None
        decoded = decode_tex_mip(th.tex, 0, 0)
        qimg = QImage(decoded.rgba, decoded.width, decoded.height, QImage.Format.Format_RGBA8888).copy()
        return QPixmap.fromImage(qimg)

    def _ensure_texture_preview(self, tex_idx: int):
        source = self._preview_source_key()
        cache_key = (tex_idx, source)

        if source == "albedo" and tex_idx in self._custom_tex_pixmap:
            pix = self._custom_tex_pixmap.get(tex_idx)
            self._apply_preview_texture(tex_idx, pix, pix is None)
            return

        if cache_key in self._tex_pixmap_cache:
            pix = self._tex_pixmap_cache[cache_key]
            self._apply_preview_texture(tex_idx, pix, pix is None)
            return

        pak_path = self._resolve_pak_path_for_texture(tex_idx, source)
        self._tex_path_cache[cache_key] = pak_path or ""
        if not pak_path:
            self._tex_pixmap_cache[cache_key] = None
            self._apply_preview_texture(tex_idx, None, True)
            return

        reader = self._project_mode_cached_reader()
        if not reader:
            self._tex_pixmap_cache[cache_key] = None
            self._apply_preview_texture(tex_idx, None, True)
            return

        stream = reader.get_file(pak_path)
        pix = self._decode_tex_to_pixmap(stream.read()) if stream else None
        self._tex_pixmap_cache[cache_key] = pix
        self._apply_preview_texture(tex_idx, pix, pix is None)

    def _ensure_texture_for_selected_pattern(self):
        pat = self._current_pattern()
        if not pat or pat.texture_index < 0:
            return
        self._ensure_texture_preview(pat.texture_index)

    def _apply_preview_texture(self, tex_idx: int, pixmap: QPixmap | None, missing: bool):
        for preview in (self.preview, self.preview_focused):
            if preview:
                preview.set_texture_pixmap(tex_idx, pixmap)
                preview.set_texture_missing(tex_idx, missing)

    def _on_texture_changed(self, item: QTableWidgetItem):
        if self._loading or not (uvs := self.handler.uvs):
            return
        row, col = item.row(), item.column()
        if row >= len(uvs.textures) or col not in (1, 2):
            return
        tex = uvs.textures[row]
        try:
            if col == 1:
                tex.path = item.text()
                self._tex_pixmap_cache.pop((row, "albedo"), None)
                self._tex_path_cache.pop((row, "albedo"), None)
                self._custom_tex_pixmap.pop(row, None)
                self._custom_tex_file.pop(row, None)
            else:  # col == 2
                tex.state_holder = self._read_int(item.text())
            self._mark_modified()
            self._sync_preview()
        except Exception as ex:
            QMessageBox.warning(self, "Invalid value", str(ex))
            self._set_loading(True)
            self._reload_textures()
            self._set_loading(False)

    def _on_pattern_changed(self, item: QTableWidgetItem):
        if self._loading or not (pat := self._current_pattern()):
            return
        col = item.column()
        try:
            if col == 1:
                pat.flags = self._read_int(item.text())
            elif col == 2:
                pat.left = float(item.text())
            elif col == 3:
                pat.top = float(item.text())
            elif col == 4:
                pat.right = float(item.text())
            elif col == 5:
                pat.bottom = float(item.text())
            elif col == 6:
                pat.texture_index = self._read_int(item.text())
            else:
                return
            self._mark_modified()
            self._sync_preview()
        except Exception as ex:
            QMessageBox.warning(self, "Invalid value", str(ex))
            self._set_loading(True)
            self._reload_patterns()
            self._set_loading(False)

    def _on_cutout_changed(self, item: QTableWidgetItem):
        if self._loading or not (pat := self._current_pattern()):
            return
        row, col = item.row(), item.column()
        if row >= len(pat.cutout_uvs) or col not in (1, 2):
            return
        try:
            u, v = pat.cutout_uvs[row]
            if col == 1:
                u = float(item.text())
            else:
                v = float(item.text())
            pat.cutout_uvs[row] = (u, v)
            if self.preview_focused:
                self.preview_focused.update()
            self._mark_modified()
        except Exception as ex:
            QMessageBox.warning(self, "Invalid value", str(ex))
            self._set_loading(True)
            self._reload_cutouts()
            self._set_loading(False)

    def _on_preview_pattern_changed(self):
        pat = self._current_pattern()
        row = self._selected_pattern
        if pat and row >= 0 and row < self.patterns_table.rowCount():
            self._set_loading(True)
            self._set_pattern_row_values(row, pat)
            self._set_loading(False)
        self._set_loading(True)
        self._reload_cutouts()
        self._set_loading(False)
        if self.preview_focused:
            self.preview_focused.update()
        self._mark_modified()

    def _on_focused_cutout_toggled(self, checked: bool):
        if self.preview_focused:
            self.preview_focused.set_show_cutouts(checked)
            self.preview_focused.set_edit_cutouts(checked)
            self.preview_focused.set_interaction_enabled(checked)
            self.preview_focused.update()

    def _on_frame_slider(self, value: int):
        seq = self._current_sequence()
        if not seq:
            return
        if value < 0 or value >= len(seq.patterns):
            return
        self._selected_pattern = value
        self.patterns_table.selectRow(value)
        self._sync_preview()

    def _toggle_play(self):
        if self.play_timer.isActive():
            self.play_timer.stop()
            self.play_btn.setText("▶ Play")
            return
        seq = self._current_sequence()
        if not seq or len(seq.patterns) <= 1:
            return
        ms = int(1000 / max(1, self.fps_spin.value()))
        self.play_timer.start(ms)
        self.play_btn.setText("⏸ Pause")

    def _advance_frame(self):
        seq = self._current_sequence()
        if not seq or len(seq.patterns) <= 1:
            self.play_timer.stop()
            self.play_btn.setText("▶ Play")
            return
        nxt = self._selected_pattern + 1
        if nxt >= len(seq.patterns):
            if self.loop_chk.isChecked():
                nxt = 0
            else:
                self.play_timer.stop()
                self.play_btn.setText("▶ Play")
                return
        self.frame_slider.setValue(nxt)

    def _load_custom_texture_pixmap(self, path: str) -> QPixmap | None:
        lowered = path.lower()
        if ".tex." in lowered:
            try:
                with open(path, "rb") as f:
                    return self._decode_tex_to_pixmap(f.read())
            except Exception:
                return None

        pm = QPixmap(path)
        return None if pm.isNull() else pm

    def _load_custom_texture_for_selected(self):
        if (row := self.textures_table.currentRow()) < 0:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Select custom texture", "", "Textures/Images (*.tex.* *.png *.jpg *.jpeg *.bmp *.tga *.tif *.tiff *.webp);;All Files (*)")
        if not path or not (pm := self._load_custom_texture_pixmap(path)):
            if path:
                QMessageBox.warning(self, "Invalid Texture", "Failed to load the selected texture/image file.")
            return
        self._custom_tex_file[row] = path
        self._custom_tex_pixmap[row] = pm
        self._ensure_texture_for_selected_pattern()
        for preview in (self.preview, self.preview_focused):
            if preview:
                preview.update()

    def _clear_custom_texture_for_selected(self):
        if (row := self.textures_table.currentRow()) < 0:
            return
        self._custom_tex_file.pop(row, None)
        self._custom_tex_pixmap.pop(row, None)
        self._tex_pixmap_cache.pop((row, "albedo"), None)
        self._ensure_texture_for_selected_pattern()
        for preview in (self.preview, self.preview_focused):
            if preview:
                preview.update()

    def _add_texture(self):
        if not (uvs := self.handler.uvs):
            return
        uvs.textures.append(UvsTexture())
        self._reload_textures()
        if self.textures_table.rowCount() > 0:
            self.textures_table.selectRow(self.textures_table.rowCount() - 1)
        self._mark_modified()

    def _remove_texture(self):
        if not (uvs := self.handler.uvs):
            return
        row = self.textures_table.currentRow()
        if row < 0 or row >= len(uvs.textures):
            return
        uvs.textures.pop(row)
        
        # Rebuild caches with adjusted indices
        self._tex_pixmap_cache = {
            (i - (1 if i > row else 0), src): v
            for (i, src), v in self._tex_pixmap_cache.items()
            if i != row
        }
        self._tex_path_cache = {
            (i - (1 if i > row else 0), src): v
            for (i, src), v in self._tex_path_cache.items()
            if i != row
        }
        
        for seq in uvs.sequences:
            for pat in seq.patterns:
                if pat.texture_index == row:
                    pat.texture_index = -1
                elif pat.texture_index > row:
                    pat.texture_index -= 1
        
        self._reload_textures()
        self._reload_patterns()
        self._sync_preview()
        self._on_texture_selected()
        self._mark_modified()

    def _add_sequence(self):
        if not (uvs := self.handler.uvs):
            return
        uvs.sequences.append(UvsSequence())
        self._reload_sequences()
        self._mark_modified()

    def _remove_sequence(self):
        if not (uvs := self.handler.uvs):
            return
        row = self.sequences_table.currentRow()
        if row < 0 or row >= len(uvs.sequences):
            return
        uvs.sequences.pop(row)
        self._selected_sequence = self._selected_pattern = -1
        self._reload_sequences()
        self._refresh_pattern_views()
        self._sync_preview()
        self._mark_modified()

    def _add_pattern(self):
        if not (uvs := self.handler.uvs) or self._selected_sequence < 0 or self._selected_sequence >= len(uvs.sequences):
            return
        uvs.sequences[self._selected_sequence].patterns.append(UvsPattern())
        self._reload_sequences()
        self._reload_patterns()
        self._sync_preview()
        self._mark_modified()

    def _remove_pattern(self):
        if not (uvs := self.handler.uvs) or self._selected_sequence < 0 or self._selected_sequence >= len(uvs.sequences):
            return
        seq = uvs.sequences[self._selected_sequence]
        row = self.patterns_table.currentRow()
        if row < 0 or row >= len(seq.patterns):
            return
        seq.patterns.pop(row)
        self._selected_pattern = -1
        self._reload_sequences()
        self._refresh_pattern_views()
        self._sync_preview()
        self._mark_modified()

    def _add_cutout(self):
        if not (pat := self._current_pattern()):
            return
        pat.cutout_uvs.append((0.0, 0.0))
        self._reload_patterns()
        self._reload_cutouts()
        self._mark_modified()

    def _remove_cutout(self):
        if not (pat := self._current_pattern()):
            return
        row = self.cutouts_table.currentRow()
        if row < 0 or row >= len(pat.cutout_uvs):
            return
        pat.cutout_uvs.pop(row)
        self._reload_patterns()
        self._reload_cutouts()
        self._mark_modified()
