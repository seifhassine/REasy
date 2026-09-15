from __future__ import annotations
from pathlib import Path

from PySide6.QtCore    import Qt
from PySide6.QtGui     import QPixmap
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QFileDialog, QMessageBox, QFormLayout, QCheckBox
)

from .project_config import load_project_config, save_project_config


class ProjectSettingsDialog(QDialog):
    def __init__(self, project_dir: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Fluffy Settings"))
        self.setModal(True)
        self.project_dir = project_dir
        self.cfg = load_project_config(project_dir)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        fields = (
            ("name_edit", self.tr("Mod Name:"), "name", project_dir.name),
            ("desc_edit", self.tr("Description:"), "description", ""),
            ("auth_edit", self.tr("Author:"), "author", ""),
            ("ver_edit", self.tr("Version:"), "version", "v1.0"),
            ("pak_edit", self.tr("PAK File Name:"), "pak_name", project_dir.name),
        )
        for attr, label, key, default in fields:
            edit = QLineEdit(self.cfg.get(key, default))
            setattr(self, attr, edit)
            form.addRow(label, edit)

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
        self.pic_edit.setText(self._stored_screenshot_path(Path(fn)))
        self._reload_preview()

    def _stored_screenshot_path(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.project_dir.resolve()))
        except (OSError, RuntimeError, ValueError):
            return str(path)

    def _screenshot_path(self) -> Path | None:
        text = self.pic_edit.text().strip()
        if not text:
            return None
        path = Path(text)
        return path if path.is_absolute() else self.project_dir / path

    def _reload_preview(self):
        pic_path = self._screenshot_path()
        if pic_path and pic_path.is_file():
            pix = QPixmap(str(pic_path))
            self.preview.setPixmap(pix.scaled(
                self.preview.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation))
        else:
            self.preview.clear()

    def _save(self):
        self.cfg.update({
            "bundle_pak":  self.bundle_chk.isChecked(),
            "name":        self.name_edit.text().strip(),
            "description": self.desc_edit.text().strip(),
            "author":      self.auth_edit.text().strip(),
            "version":     self.ver_edit.text().strip(),
            "screenshot":  self.pic_edit.text().strip(),
            "pak_name":    self.pak_edit.text().strip(),
        })

        try:
            save_project_config(self.project_dir, self.cfg)
        except Exception as e:
            QMessageBox.critical(self, self.tr("Save failed"), str(e))
            return
        self.accept()
