from __future__ import annotations
from pathlib import Path

from PySide6.QtCore    import Qt
from PySide6.QtGui     import QPixmap
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLineEdit, QDialogButtonBox, QPlainTextEdit, QSpinBox, QListWidget,
    QListWidgetItem, QToolButton, QFileDialog, QMessageBox, QFormLayout, QCheckBox
)

lazy from tools.fluffy_exporter import MAX_CATEGORIES, modinfo_problems
lazy from ui.editor_widgets import set_invalid, styled_label
lazy from ui.icons import line_icon

lazy from .project_config import load_project_config, save_project_config


class ProjectSettingsDialog(QDialog):
    def __init__(self, project_dir: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Fluffy Settings"))
        self.setModal(True)
        self.setMinimumWidth(720)
        self.project_dir = project_dir
        self.cfg = cfg = load_project_config(project_dir)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        columns = QHBoxLayout()
        columns.setSpacing(16)
        layout.addLayout(columns)

        pic_layout = QHBoxLayout()
        pic_layout.setSpacing(4)
        self.pic_edit = QLineEdit(cfg.get("screenshot", ""))
        self.pic_btn  = QToolButton()
        self.pic_btn.setIcon(line_icon("folder_open"))
        self.pic_btn.setToolTip(self.tr("Select Screenshot"))
        self.pic_btn.clicked.connect(self._choose_image)
        pic_layout.addWidget(self.pic_edit)
        pic_layout.addWidget(self.pic_btn)

        self.preview = styled_label(surface="recessed", tone="subtle")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setFixedSize(160, 90)

        options = QListWidget()
        for sibling in sorted(project_dir.parent.iterdir(), key=lambda path: path.name.lower()):
            if sibling.is_dir() and sibling.name != project_dir.name and not sibling.name.startswith("."):
                QListWidgetItem(sibling.name, options).setCheckState(
                    Qt.Checked if sibling.name in cfg.get("option_projects", ()) else Qt.Unchecked)
        if not options.count():
            QListWidgetItem(self.tr("No other projects of this game"), options).setFlags(Qt.NoItemFlags)
        options.itemDoubleClicked.connect(self._edit_option)

        self.edits = {
            "name": QLineEdit(cfg.get("name", project_dir.name)),
            "version": QLineEdit(cfg.get("version", "v1.0")),
            "author": QLineEdit(cfg.get("author", "")),
            "homepage": QLineEdit(cfg.get("homepage", "")),
            "description": QPlainTextEdit(cfg.get("description", ""), tabChangesFocus=True),
            "screenshot": self.pic_edit,
            "categories": QPlainTextEdit("\n".join(cfg.get("categories", ())), tabChangesFocus=True,
                                         placeholderText="!Characters > Leon"),
            "addon_for": QLineEdit(cfg.get("addon_for", "")),
            "name_as_bundle": QLineEdit(cfg.get("name_as_bundle", "")),
            "requires": QLineEdit(cfg.get("requires", "")),
            "menu_priority": QSpinBox(minimum=-999, maximum=999, value=cfg.get("menu_priority", 0)),
            "dummy_mod": QCheckBox(self.tr("Only groups add-ons"), checked=cfg.get("dummy_mod", False)),
            "pak_name": QLineEdit(cfg.get("pak_name", project_dir.name)),
            "bundle_pak": QCheckBox(self.tr("Build PAK instead of loose folders in Fluffy ZIP"),
                                    checked=cfg.get("bundle_pak", False)),
            "option_projects": options,
        }
        for key, lines in (("description", 5), ("categories", 3), ("option_projects", 4)):
            self.edits[key].setFixedHeight(self.edits[key].fontMetrics().lineSpacing() * lines + 12)

        self.labels = {}
        for rows in self._columns():
            form = QFormLayout()
            form.setHorizontalSpacing(10)
            form.setVerticalSpacing(6)
            column = QVBoxLayout()
            column.addLayout(form)
            column.addStretch(1)
            columns.addLayout(column, 1)
            for key, label, tip in rows:
                if not key:
                    form.addRow(styled_label(label, heading="caption"))
                    continue
                self.labels[key] = label
                self.edits[key].setToolTip(tip)
                form.addRow(label, pic_layout if key == "screenshot" else self.edits[key])
                if key == "screenshot":
                    form.addRow("", self.preview)

        for widget in self.edits.values():
            if isinstance(widget, (QLineEdit, QPlainTextEdit)):
                widget.textChanged.connect(self._validate)
        self.pic_edit.textChanged.connect(self._reload_preview)

        self.status = styled_label(tone="danger")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_button = buttons.button(QDialogButtonBox.Ok)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addStretch(1)
        layout.addWidget(buttons)
        self._reload_preview()
        self._validate()

    def _columns(self) -> tuple:
        return (
            (
                ("", self.tr("Mod"), ""),
                ("name", self.tr("Mod Name:"), self.tr("Name in the mod list (name)")),
                ("version", self.tr("Version:"), ""),
                ("author", self.tr("Author:"), ""),
                ("homepage", self.tr("Homepage:"), self.tr("Link the mod preview's homepage button opens (homepage)")),
                ("description", self.tr("Description:"), self.tr(
                    "Shown with the mod; line breaks are kept (description)")),
                ("screenshot", self.tr("Screenshot:"), self.tr("Mod preview image; 1280x720 fits best (screenshot)")),
            ),
            (
                ("", self.tr("Mod list"), ""),
                ("categories", self.tr("Categories:"), self.tr(
                    "One per line, up to {}. \"!Group > Name\" files the mod under one of Fluffy Mod Manager's own "
                    "categories; other text is a tag for the mod filter (category)").format(MAX_CATEGORIES)),
                ("addon_for", self.tr("Add-on for:"), self.tr(
                    "Name of the main mod: this mod is listed in its add-on menu instead of the mod list (AddonFor)")),
                ("name_as_bundle", self.tr("Bundle name:"), self.tr(
                    "Mods with the same bundle name share one button in the mod list (NameAsBundle)")),
                ("requires", self.tr("Requires:"), self.tr(
                    "Name of a mod this one needs: installing warns when it isn't installed (requires)")),
                ("menu_priority", self.tr("Menu priority:"), self.tr(
                    "Place in the add-on menu: higher numbers are listed first (MenuPriority)")),
                ("dummy_mod", "", self.tr(
                    "The entry only organizes add-ons and stays hidden until an add-on names it (DummyMod)")),
                ("", self.tr("Package"), ""),
                ("pak_name", self.tr("PAK File Name:"), ""),
                ("bundle_pak", "", ""),
                ("", self.tr("Options in this ZIP"), ""),
                ("option_projects", self.tr("Projects:"), self.tr(
                    "Other projects of this game packed into the same ZIP, each as its own mod in its own folder "
                    "with its own Fluffy settings; double-click one to edit them. Set Add-on for or Bundle name in "
                    "the options to group them with this mod")),
            ),
        )

    def _choose_image(self):
        fn, _ = QFileDialog.getOpenFileName(
            self, self.tr("Select Screenshot"),
            str(self.project_dir),
            "Images (*.png *.jpg *.jpeg *.bmp)")
        if fn:
            self.pic_edit.setText(self._stored_screenshot_path(Path(fn)))

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
            self.preview.setText(self.tr("No screenshot"))

    def _config(self) -> dict:
        values = {}
        for key, widget in self.edits.items():
            if isinstance(widget, QCheckBox):
                values[key] = widget.isChecked()
            elif isinstance(widget, QSpinBox):
                values[key] = widget.value()
            elif isinstance(widget, QListWidget):
                items = (widget.item(row) for row in range(widget.count()))
                values[key] = [item.text() for item in items if item.checkState() == Qt.Checked]
            else:
                values[key] = (widget.text() if isinstance(widget, QLineEdit) else widget.toPlainText()).strip()
        values["categories"] = [line.strip() for line in values["categories"].splitlines() if line.strip()]
        return self.cfg | values

    def _edit_option(self, item):
        dialog = ProjectSettingsDialog(self.project_dir.parent / item.text(), self)
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.exec()

    def _validate(self):
        problems = modinfo_problems(self._config(), self.project_dir)
        for key, widget in self.edits.items():
            set_invalid(widget, key in problems)
        self.status.setText("\n".join(f"{self.labels[key]} {problem}" for key, problem in problems.items()))
        self.status.setVisible(bool(problems))
        self.ok_button.setEnabled(not problems)

    def _save(self):
        self.cfg = self._config()
        try:
            save_project_config(self.project_dir, self.cfg)
        except Exception as e:
            QMessageBox.critical(self, self.tr("Save failed"), str(e))
            return
        self.accept()
