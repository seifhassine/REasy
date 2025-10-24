from __future__ import annotations
import json
from pathlib import Path

from PySide6.QtCore    import Qt
from PySide6.QtGui     import QPixmap
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QFileDialog, QMessageBox, QFormLayout, QCheckBox
)

class ProjectSettingsDialog(QDialog):
    CONFIG_NAME = ".reasy_project.json"

    def __init__(self, project_dir: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Fluffy Settings"))
        self.setModal(True)
        self.project_dir = project_dir
        self.cfg_path    = project_dir / self.CONFIG_NAME

        if self.cfg_path.exists():
            try:
                self.cfg = json.loads(self.cfg_path.read_text())
            except Exception:
                self.cfg = {}
        else:
            self.cfg = {}

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self.name_edit = QLineEdit(self.cfg.get("name", project_dir.name))
        form.addRow(self.tr("Mod Name:"), self.name_edit)

        self.desc_edit = QLineEdit(self.cfg.get("description", ""))
        form.addRow(self.tr("Description:"), self.desc_edit)

        self.auth_edit = QLineEdit(self.cfg.get("author", ""))
        form.addRow(self.tr("Author:"), self.auth_edit)

        self.ver_edit  = QLineEdit(self.cfg.get("version", "v1.0"))
        form.addRow(self.tr("Version:"), self.ver_edit)

        self.pak_edit = QLineEdit(self.cfg.get("pak_name", project_dir.name))
        form.addRow(self.tr("PAK File Name:"), self.pak_edit)

        self.bundle_chk = QCheckBox(self.tr("Build PAK instead of loose folders in Fluffy ZIP"))
        self.bundle_chk.setChecked(self.cfg.get("bundle_pak", False))
        form.addRow(self.bundle_chk)

        pic_layout = QHBoxLayout()
        self.pic_edit = QLineEdit(self.cfg.get("screenshot", ""))
        self.pic_btn  = QPushButton("…")
        self.pic_btn.clicked.connect(self._choose_image)
        pic_layout.addWidget(self.pic_edit)
        pic_layout.addWidget(self.pic_btn)
        form.addRow(self.tr("Screenshot:"), pic_layout)

        self.preview = QLabel(alignment=Qt.AlignCenter)
        self.preview.setFixedSize(160, 90)
        layout.addWidget(self.preview)
        self._reload_preview()

        # Buttons
        btns = QHBoxLayout()
        layout.addLayout(btns)
        btn_ok = QPushButton(self.tr("OK"))
        btn_ok.clicked.connect(self._save)
        btn_cancel = QPushButton(self.tr("Cancel"))
        btn_cancel.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(btn_ok)
        btns.addWidget(btn_cancel)

    def _choose_image(self):
        fn, _ = QFileDialog.getOpenFileName(
            self, self.tr("Select Screenshot"),
            str(self.project_dir),
            "Images (*.png *.jpg *.jpeg *.bmp)")
        if not fn:
            return
        self.pic_edit.setText(fn)
        self._reload_preview()

    def _reload_preview(self):
        pic_path = Path(self.pic_edit.text().strip())
        if pic_path.is_file():
            pix = QPixmap(str(pic_path))
            self.preview.setPixmap(pix.scaled(
                self.preview.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation))
        else:
            self.preview.clear()

    def _save(self):
        self.cfg["bundle_pak"] = self.bundle_chk.isChecked()

        self.cfg.update({
            "name":        self.name_edit.text().strip(),
            "description": self.desc_edit.text().strip(),
            "author":      self.auth_edit.text().strip(),
            "version":     self.ver_edit.text().strip(),
            "screenshot":  self.pic_edit.text().strip(),
            "pak_name":    self.pak_edit.text().strip(),
        })

        try:
            self.cfg_path.write_text(json.dumps(self.cfg, indent=2))
        except Exception as e:
            QMessageBox.critical(self, self.tr("Save failed"), str(e))
            return
        self.accept()