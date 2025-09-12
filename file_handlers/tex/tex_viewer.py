from __future__ import annotations

from io import BytesIO

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox, QSpinBox, QScrollArea,
    QFileDialog, QMessageBox, QInputDialog
)

from PIL import Image

class TexViewer(QWidget):
    modified_changed = Signal(bool)

    def __init__(self, handler):
        super().__init__()
        self.handler = handler
        self._modified = False
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

        self.info_label = QLabel("")
        layout.addWidget(self.info_label)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.img_label = QLabel()
        self.img_label.setAlignment(Qt.AlignCenter)
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
        img_idx = self.image_index.value()
        mip_idx = self.mip_index.value()
        dds = b""
        if hasattr(self.handler, 'build_dds_bytes'):
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
            im = Image.open(BytesIO(dds))
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
            self.img_label.setPixmap(QPixmap.fromImage(qimg))
            if hasattr(self, 'export_dds_btn') and hasattr(self, 'export_tex_btn'):
                t = getattr(self.handler, 'tex', None)
                raw = getattr(self.handler, 'raw_data', b"")
                self.export_dds_btn.setVisible(bool(t))
                self.export_tex_btn.setVisible(raw[:4] == b'DDS ')
        except Exception as e:
            self.info_label.setText(f"Failed to decode: {e}")
            self.img_label.clear()

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

