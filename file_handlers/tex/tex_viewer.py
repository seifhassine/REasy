from __future__ import annotations

from io import BytesIO

from PySide6.QtCore import Qt, Signal, QPoint, QTimer
from PySide6.QtGui import QImage, QPixmap, QCursor, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox, QSpinBox, QScrollArea,
    QFileDialog, QMessageBox, QInputDialog, QSlider
)

from PIL import Image
PIL_OK = True

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


class TexViewer(QWidget):
    modified_changed = Signal(bool)

    def __init__(self, handler):
        super().__init__()
        self.handler = handler
        self._modified = False
        self.zoom_level = 1.0
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

        top = QHBoxLayout()
        top.addWidget(QLabel("Image:"))
        self.image_index = QSpinBox()
        self.image_index.setMinimum(0)
        self.image_index.valueChanged.connect(self._refresh)
        top.addWidget(self.image_index)

        top.addWidget(QLabel("Mip:"))
        self.mip_index = QSpinBox()
        self.mip_index.setMinimum(0)
        self.mip_index.valueChanged.connect(self._refresh)
        top.addWidget(self.mip_index)

        self.reload_btn = QPushButton("Reload")
        self.reload_btn.clicked.connect(self._refresh)
        top.addWidget(self.reload_btn)

        top.addWidget(QLabel("Channel:"))
        self.channel_box = QComboBox()
        self.channel_box.addItems(["RGBA", "R", "G", "B", "A", "RGB", "RG", "BA"])
        self.channel_box.currentIndexChanged.connect(self._refresh)
        top.addWidget(self.channel_box)
        top.addStretch()
        layout.addLayout(top)

        zoom_layout = QHBoxLayout()
        zoom_layout.addWidget(QLabel("Zoom:"))
        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setMinimum(10)
        self.zoom_slider.setMaximum(500)
        self.zoom_slider.setValue(100)
        self.zoom_slider.setTickPosition(QSlider.TicksBelow)
        self.zoom_slider.setTickInterval(50)
        self.zoom_slider.valueChanged.connect(self._on_zoom_changed)
        zoom_layout.addWidget(self.zoom_slider)
        self.zoom_label = QLabel("100%")
        self.zoom_label.setMinimumWidth(50)
        zoom_layout.addWidget(self.zoom_label)
        self.fit_btn = QPushButton("Fit to Window")
        self.fit_btn.clicked.connect(self._fit_to_window)
        zoom_layout.addWidget(self.fit_btn)
        self.reset_zoom_btn = QPushButton("100%")
        self.reset_zoom_btn.clicked.connect(self._reset_zoom)
        zoom_layout.addWidget(self.reset_zoom_btn)
        zoom_layout.addStretch()
        layout.addLayout(zoom_layout)

        self.info_label = QLabel("")
        layout.addWidget(self.info_label)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(False)
        self.scroll.setAlignment(Qt.AlignCenter)
        self.img_label = DraggableLabel()
        self.img_label.scroll_area = self.scroll
        self.img_label.viewer = self
        self.img_label.setAlignment(Qt.AlignCenter)
        self.img_label.setScaledContents(False)
        self.scroll.setWidget(self.img_label)
        layout.addWidget(self.scroll)

        exp = QHBoxLayout()
        self.export_dds_btn = QPushButton("Export DDS")
        self.export_dds_btn.clicked.connect(self._export_dds)
        exp.addWidget(self.export_dds_btn)

        self.export_tex_btn = QPushButton("Export TEX")
        self.export_tex_btn.clicked.connect(self._export_tex)
        exp.addWidget(self.export_tex_btn)
        exp.addStretch()
        layout.addLayout(exp)

        if not PIL_OK:
            self.info_label.setText("Pillow + pillow-dds not installed. Cannot display images.")

    def _populate(self):
        t = getattr(self.handler, 'tex', None)
        raw = getattr(self.handler, 'raw_data', b"")
        is_tex = bool(t)
        is_dds = raw[:4] == b'DDS '

        if hasattr(self, 'export_dds_btn') and hasattr(self, 'export_tex_btn'):
            self.export_dds_btn.setVisible(is_tex)
            self.export_tex_btn.setVisible(is_dds)

        self.image_index.blockSignals(True)
        self.mip_index.blockSignals(True)

        if is_tex:
            self.image_index.setMaximum(max(0, t.header.image_count - 1))
            self.mip_index.setMaximum(max(0, t.header.mip_count - 1))
        elif is_dds:
            try:
                import struct as _st
                mip_count = _st.unpack_from('<I', raw, 28)[0]
                self.image_index.setMaximum(0)
                self.image_index.setEnabled(False)
                self.mip_index.setMaximum(max(0, mip_count - 1))
            except Exception:
                self.image_index.setMaximum(0)
                self.mip_index.setMaximum(0)
        else:
            self.image_index.setMaximum(0)
            self.mip_index.setMaximum(0)

        self.image_index.blockSignals(False)
        self.mip_index.blockSignals(False)
        self._refresh()

    def _refresh(self):
        if not PIL_OK:
            return
        img_idx = self.image_index.value()
        mip_idx = self.mip_index.value()
        dds = b""
        if hasattr(self.handler, 'build_dds_bytes_for_viewing'):
            try:
                dds = self.handler.build_dds_bytes_for_viewing(img_idx)
            except Exception:
                dds = b""
        elif hasattr(self.handler, 'build_dds_bytes'):
            try:
                dds = self.handler.build_dds_bytes(img_idx)
            except Exception:
                dds = b""
        if not dds:
            raw = getattr(self.handler, 'raw_data', b"")
            if raw[:4] == b'DDS ':
                dds = raw
        if not dds:
            return
        try:
            if hasattr(self.handler, 'build_dds_bytes_for_viewing'):
                im = Image.open(BytesIO(dds))
            else:
                from .dds import convert_dds_for_pil_compatibility
                compatible_dds = convert_dds_for_pil_compatibility(dds)
                im = Image.open(BytesIO(compatible_dds))
            for _ in range(mip_idx):
                if im.width <= 1 and im.height <= 1:
                    break
                im = im.reduce(2)
            fmt_info = ""
            t = getattr(self.handler, 'tex', None)
            raw = getattr(self.handler, 'raw_data', b"")
            if t and getattr(t, 'header', None):
                try:
                    from .dxgi import describe_format
                    d = describe_format(t.header.format)
                    fmt_info = f" | {d['name']} | {'BC' if d['compressed'] else 'RGB'} | {d['bits_per_pixel']}bpp"
                except Exception:
                    pass
            self.info_label.setText(f"{im.width}x{im.height}{fmt_info}")
            rgba = im.convert('RGBA')
            ch = self.channel_box.currentText() if hasattr(self, 'channel_box') else 'RGBA'
            if ch != 'RGBA':
                bands = rgba.split()
                r, g, b, a = bands
                if ch == 'R':
                    rgba = Image.merge('RGBA', (r, r.point(lambda _: 0), r.point(lambda _: 0), a))
                elif ch == 'G':
                    rgba = Image.merge('RGBA', (g.point(lambda _: 0), g, g.point(lambda _: 0), a))
                elif ch == 'B':
                    rgba = Image.merge('RGBA', (b.point(lambda _: 0), b.point(lambda _: 0), b, a))
                elif ch == 'A':
                    rgba = Image.merge('RGBA', (a, a, a, a))
                elif ch == 'RGB':
                    rgba = Image.merge('RGBA', (r, g, b, a))
                elif ch == 'RG':
                    rgba = Image.merge('RGBA', (r, g, g.point(lambda _: 0), a))
                elif ch == 'BA':
                    rgba = Image.merge('RGBA', (b, a, b, a))
            qimg = QImage(rgba.tobytes(), rgba.width, rgba.height, QImage.Format_RGBA8888)
            self.original_pixmap = QPixmap.fromImage(qimg)
            if self.is_initial_load:
                self.is_initial_load = False
                QTimer.singleShot(0, self._fit_to_window)
            else:
                self._apply_zoom()
            
            if hasattr(self, 'export_dds_btn') and hasattr(self, 'export_tex_btn'):
                t = getattr(self.handler, 'tex', None)
                raw = getattr(self.handler, 'raw_data', b"")
                self.export_dds_btn.setVisible(bool(t))
                self.export_tex_btn.setVisible(raw[:4] == b'DDS ')
        except Exception as e:
            self.info_label.setText(f"Failed to decode: {e}")
            self.img_label.clear()
            self.original_pixmap = None

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

    def _export_dds(self):
        try:
            from PySide6.QtWidgets import QFileDialog
            t = self.handler
            from .dds import DDS_MAGIC
            raw = getattr(t, 'raw_data', b"")
            is_dds = raw[:4] == DDS_MAGIC.to_bytes(4, 'little')
            if is_dds:
                return
            if hasattr(t, 'build_dds_bytes'):
                dds = t.build_dds_bytes(self.image_index.value() if hasattr(self, 'image_index') else 0)
            else:
                dds = b""
            if not dds:
                return
            path, _ = QFileDialog.getSaveFileName(self, "Save DDS", "", "DDS files (*.dds)")
            if path:
                with open(path, 'wb') as f:
                    f.write(dds)
        except Exception:
            pass

    def _export_tex(self):
        try:
            if hasattr(self.handler, 'tex') and self.handler.tex:
                return

            from .dds import DDS_MAGIC
            if hasattr(self.handler, 'raw_data') and self.handler.raw_data[:4] == DDS_MAGIC.to_bytes(4, 'little'):
                from .tex_file import TexFile
                dds_bytes = getattr(self.handler, 'raw_data', b"")
                if not dds_bytes:
                    QMessageBox.warning(self, "Export TEX", "No DDS data loaded.")
                    return
                import struct as _st
                size = _st.unpack_from('<I', dds_bytes, 4)[0]
                height = _st.unpack_from('<I', dds_bytes, 12)[0]
                width = _st.unpack_from('<I', dds_bytes, 16)[0]
                mip_count = _st.unpack_from('<I', dds_bytes, 28)[0]
                fmt = _st.unpack_from('<I', dds_bytes, 128)[0]

                data = dds_bytes[148:]
                levels = []
                w = width
                h = height
                for i in range(mip_count):
                    if w == 0 or h == 0:
                        break
                    from .dxgi import top_mip_size_bytes
                    lvl_size = top_mip_size_bytes(fmt, w, h)
                    levels.append(data[:lvl_size])
                    data = data[lvl_size:]
                    w = max(1, w >> 1)
                    h = max(1, h >> 1)

                pitches = []
                w = width
                for i in range(len(levels)):
                    from .dxgi import get_block_size_bytes, get_bits_per_pixel
                    bs = get_block_size_bytes(fmt)
                    if bs:
                        blocks_w = (w + 3) // 4
                        pitches.append(blocks_w * bs)
                    else:
                        pitches.append(w * (get_bits_per_pixel(fmt) // 8))
                    w = max(1, w >> 1)

                version, ok = QInputDialog.getInt(self, "TEX Version", "Enter TEX version (e.g., 190820018 for RE3):", 28, 1, 2000000000, 1)
                if not ok:
                    return
                tex_bytes = TexFile.build_tex_bytes_from_dds(
                    fmt,
                    width,
                    height,
                    levels,
                    pitches_override=pitches,
                    version_override=version,
                )
                path, _ = QFileDialog.getSaveFileName(self, "Save TEX", "", "TEX files (*.tex)")
                if path:
                    with open(path, 'wb') as f:
                        f.write(tex_bytes)
                else:
                    QMessageBox.information(self, "Export TEX", "Save cancelled.")
            else:
                QMessageBox.warning(self, "Export TEX", "Open a DDS file first.")
        except Exception as e:
            QMessageBox.critical(self, "Export TEX failed", str(e))

