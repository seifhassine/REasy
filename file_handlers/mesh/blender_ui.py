from __future__ import annotations

import hashlib
from pathlib import Path

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QLabel, QMessageBox, QTextEdit, QVBoxLayout,
)

from settings import app_settings, remember_setting
from ui.background_jobs import run_job
from utils.resource_file_utils import resource_version_from_path, streaming_companion_path

from .blender import (
    Reference, apply_blend, blender_version, find_blender, read_blend, reference_bones, reference_import,
    resource_version, source_bones, suggest,
)
from .blender.export import joint_names
from .blender.runner import MINIMUM_VERSION, BlenderError

SETTINGS_KEY = "blender_path"
MAPPINGS_KEY = "blender_bone_mappings"
REMEMBERED_MAPPINGS = 20


translate = QCoreApplication.translate


def require_blender(parent, app):
    path = find_blender(str((app_settings(app) or {}).get(SETTINGS_KEY, "")).strip())
    if path is None:
        QMessageBox.information(parent, translate("BlenderRoundTrip", "Locate Blender"), translate("BlenderRoundTrip",
            "Blender {version} or newer is needed for .blend export and import. Choose blender.exe; you can "
            "change it later in Settings.").format(version=".".join(map(str, MINIMUM_VERSION))))
        chosen, _filter = QFileDialog.getOpenFileName(parent, translate("BlenderRoundTrip", "Choose blender.exe"), "",
                                                      "Blender (blender.exe blender);;All files (*)")
        if not chosen:
            return None
        path = find_blender(chosen)
        if path is None:
            return None
    try:
        version = blender_version(path)
    except (BlenderError, OSError) as exc:
        QMessageBox.warning(parent, translate("BlenderRoundTrip", "Blender unavailable"), str(exc))
        return None
    if version < MINIMUM_VERSION:
        QMessageBox.warning(parent, translate("BlenderRoundTrip", "Blender too old"), translate("BlenderRoundTrip",
            "Blender {found} cannot store exact custom normals. Install Blender {needed} or newer.").format(
                found=".".join(map(str, version)), needed=".".join(map(str, MINIMUM_VERSION))))
        return None
    remember_setting(app, SETTINGS_KEY, str(path))
    return path


def _run_busy(parent, label, work):
    """``work()`` runs off the GUI thread."""
    try:
        return run_job(parent, translate("BlenderRoundTrip", "Blender"), lambda _progress, _cancelled: work(),
                       status=label), ""
    except Exception as exc:
        return None, str(exc) or type(exc).__name__


class BlendImportReview(QDialog):
    def __init__(self, title, read, apply, parent=None, dump=None):
        super().__init__(parent)
        self.read, self.apply = read, apply
        self.result = None
        self._dumps = {True: dump} if dump is not None else {}
        self.setWindowTitle(self.tr("Import from Blender"))
        self.resize(760, 500)
        layout = QVBoxLayout(self)
        heading = QLabel(title)
        heading.setTextFormat(Qt.PlainText)
        heading.setStyleSheet("font-weight: 600;")
        layout.addWidget(heading)
        self.modifiers = QCheckBox(self.tr("Apply Blender modifiers (armatures excepted)"), self)
        self.modifiers.setChecked(True)
        self.modifiers.setToolTip(self.tr("Imports what the viewport shows in rest pose. Shape keys are "
                                          "evaluated through the same modifiers."))
        self.normals = QCheckBox(self.tr("Recompute normals of moved vertices from geometry"), self)
        self.normals.setToolTip(self.tr(
            "Blender keeps custom normals fixed when vertices move, and modifiers such as Mirror copy them "
            "unchanged. This recomputes smooth normals for moved vertices and keeps hard edges."))
        self.skin = QCheckBox(self.tr("Unweighted objects bend with the surface they sit on (unrigged clothing)"), self)
        self.skin.setToolTip(self.tr(
            "Off: an object without weights moves rigidly with the joint nearest its centre, as accessories should. "
            "On: each of its vertices copies the weights of the nearest vertex of the mesh."))
        for option in (self.modifiers, self.normals, self.skin):
            layout.addWidget(option)
            option.toggled.connect(self._refresh)
        self.report = QTextEdit(self)
        self.report.setReadOnly(True)
        layout.addWidget(self.report, 1)
        self.allow = QCheckBox(self.tr("Allow the reported data loss"), self)
        layout.addWidget(self.allow)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        self.apply_button = buttons.button(QDialogButtonBox.Ok)
        self.apply_button.setText(self.tr("Import"))
        self.allow.toggled.connect(self._update_apply)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def load(self):
        apply_modifiers = self.modifiers.isChecked()
        dump = self._dumps.get(apply_modifiers)
        if dump is None:
            dump, error = _run_busy(self, self.tr("Reading the Blender scene…"), lambda: self.read(apply_modifiers))
            if error:
                self._show_error(error)
                return False
            self._dumps[apply_modifiers] = dump
        result, error = _run_busy(self, self.tr("Building the mesh…"),
                                  lambda: self.apply(dump, self.normals.isChecked(), self.skin.isChecked()))
        if error:
            self._show_error(error)
            return False
        self.result = result
        if result.unchanged:
            text = self.tr("No edits found. The mesh is identical to the exported one.")
        else:
            text = self.tr("Changes:") + "\n" + "\n".join(f"• {item}" for item in result.changes or [self.tr("none")])
        if result.losses:
            text += "\n\n" + self.tr("Needs your approval:") + "\n" + "\n".join(f"• {item}" for item in result.losses)
        self.report.setPlainText(text)
        self.allow.setChecked(False)
        self.allow.setVisible(bool(result.losses))
        self._update_apply()
        return True

    def run(self, confirm=None):
        if not self.load():
            self.exec()
            return None
        if confirm is not None and not confirm(self.result):
            return None
        return self.result if self.exec() == QDialog.Accepted else None

    def _refresh(self):
        self.load()

    def _show_error(self, error):
        self.result = None
        self.report.setPlainText(self.tr("The scene cannot be imported:") + "\n" + error)
        self.allow.hide()
        self._update_apply()

    def _update_apply(self):
        result = self.result
        self.apply_button.setEnabled(result is not None and not result.unchanged
                                     and (not result.losses or self.allow.isChecked()))


def _tab_for(handler):
    workspace = getattr(getattr(handler, "app", None), "project_workspace", None)
    sessions = getattr(workspace, "sessions", None)
    for tab in (sessions.all_tabs() if sessions is not None else ()):
        if getattr(tab, "handler", None) is handler:
            return tab
    return None


def apply_to_document(handler, result):
    from .mesh_handler import MeshHandler

    tab = _tab_for(handler)
    if tab is None:
        raise ValueError(translate("BlenderRoundTrip", "The mesh's editor tab was not found"))
    replacement = MeshHandler()
    replacement.take_streaming(result.output.streaming)
    if not tab.load_file(tab.filename, result.output.main, handler=replacement):
        raise ValueError(translate("BlenderRoundTrip", "The imported mesh could not be opened"))
    replacement.modified = True
    tab.modified = True
    tab.update_tab_title()


def save_and_open(parent, app, result, name):
    suffix = f".mesh.{result.file_version}"
    path, _filter = QFileDialog.getSaveFileName(
        parent, translate("BlenderRoundTrip", "Save the new mesh"), name + suffix,
        translate("BlenderRoundTrip", "Meshes (*{suffix})").format(suffix=suffix))
    if not path:
        return None
    if not path.endswith(suffix):
        path += suffix
    companion = streaming_companion_path(path) if result.output.streaming is not None else None
    if result.output.streaming is not None and companion is None:
        QMessageBox.warning(parent, translate("BlenderRoundTrip", "Save the new mesh"), translate("BlenderRoundTrip",
            "This mesh keeps part of its geometry in a streaming file, which belongs under "
            "natives/<platform>/streaming/. Save the mesh below a natives/<platform>/ folder."))
        return None
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(result.output.main)
        if companion is not None:
            Path(companion).parent.mkdir(parents=True, exist_ok=True)
            Path(companion).write_bytes(result.output.streaming)
    except OSError as exc:
        QMessageBox.warning(parent, translate("BlenderRoundTrip", "Save the new mesh"), str(exc))
        return None
    add_tab = getattr(app, "add_tab", None)
    if callable(add_tab):
        add_tab(path, result.output.main)
    return path


def _skeleton_key(mesh):
    return hashlib.sha1("\n".join(joint_names(mesh)).encode("utf-8")).hexdigest()[:16]


def _recall_mapping(app, mesh, mapping) -> str:
    saved = (app_settings(app) or {}).get(MAPPINGS_KEY, {})
    text = saved.get(_skeleton_key(mesh)) if isinstance(saved, dict) else None
    if text:
        try:
            mapping.apply_json(text)
        except ValueError as exc:
            return str(exc)
    return ""


def _store_mapping(app, mesh, mapping):
    saved = (app_settings(app) or {}).get(MAPPINGS_KEY, {})
    saved = dict(saved) if isinstance(saved, dict) else {}
    key = _skeleton_key(mesh)
    saved.pop(key, None)
    saved[key] = mapping.to_json()
    while len(saved) > REMEMBERED_MAPPINGS:
        saved.pop(next(iter(saved)))
    remember_setting(app, MAPPINGS_KEY, saved)


def _scene_reader(path, blender):
    return lambda apply_modifiers: read_blend(path, blender=blender, apply_modifiers=apply_modifiers)


def _reference_review(parent, app, path, blender, dump, current=None):
    from .blender_reference_dialog import ReferencePicker
    from .bone_mapping_dialog import BoneMappingDialog

    bones, weighted = source_bones(dump)
    picker = ReferencePicker(bones, weighted, app, current, parent)
    if picker.exec() != QDialog.Accepted or picker.choice is None:
        return None
    choice = picker.choice
    mapping = suggest(bones, reference_bones(choice.mesh), weighted)
    if choice.mesh.bones and mapping.rows:
        unread = _recall_mapping(app, choice.mesh, mapping)
        if unread:
            QMessageBox.warning(parent, translate("BlenderRoundTrip", "Bone mapping"), translate("BlenderRoundTrip",
                "The bone mapping remembered for this skeleton could not be read; the suggested mapping is shown."
            ) + "\n\n" + unread)
        if BoneMappingDialog(mapping, choice.label, parent).exec() != QDialog.Accepted:
            return None
        _store_mapping(app, choice.mesh, mapping)
    review = BlendImportReview(
        f"{Path(path).name} → {choice.label}", _scene_reader(path, blender),
        lambda scene, recompute, skin: reference_import(scene, choice.reference, mapping, recompute_normals=recompute,
                                                        skin_unweighted=skin),
        parent, dump)
    review.choice = choice
    return review


def _exported_review(parent, path, blender, dump):
    return BlendImportReview(Path(path).name, _scene_reader(path, blender),
                             lambda scene, recompute, skin: apply_blend(scene, recompute_normals=recompute,
                                                                        skin_unweighted=skin), parent, dump)


def _open_scene(parent, app, start=""):
    path, _filter = QFileDialog.getOpenFileName(parent, translate("BlenderRoundTrip", "Import from Blender"), start,
                                                translate("BlenderRoundTrip", "Blender scenes (*.blend)"))
    blender = require_blender(parent, app) if path else None
    if blender is None:
        return None
    dump, error = _run_busy(parent, translate("BlenderRoundTrip", "Reading the Blender scene…"),
                            lambda: read_blend(path, blender=blender))
    if not error and dump.foreign and not dump.manifest["objects"]:
        error = translate("BlenderRoundTrip", "The scene has no visible mesh objects.")
    if error:
        QMessageBox.warning(parent, translate("BlenderRoundTrip", "Blender import failed"), error)
        return None
    return path, blender, dump


def _open_mesh_choice(viewer):
    from .blender_reference_dialog import ReferenceChoice

    handler = viewer.handler
    label = Path(handler.filepath).name if handler.filepath else translate("BlenderRoundTrip", "The open mesh")
    try:
        version = resource_version(handler.mesh, resource_version_from_path(handler.filepath or "", "mesh"))
        return ReferenceChoice(Reference.from_mesh(handler.mesh, version, label), handler.mesh, label)
    except ValueError:
        return None


def import_into_viewer(viewer):
    handler = viewer.handler
    app = getattr(handler, "app", None)
    opened = _open_scene(viewer, app, str(Path(handler.filepath).with_suffix(".blend")) if handler.filepath else "")
    if opened is None:
        return
    path, blender, dump = opened

    def same_mesh(result):
        version = result.file_version
        if not version or not handler.filepath or str(handler.filepath).endswith(f".{version}"):
            return True
        question = translate(
            "BlenderRoundTrip",
            "This .blend was exported from a different mesh version (.{version}). Import it anyway?")
        return QMessageBox.question(viewer, translate("BlenderRoundTrip", "Different mesh"),
                                    question.format(version=version)) == QMessageBox.Yes

    current = _open_mesh_choice(viewer) if dump.foreign else None
    review = (_reference_review(viewer, app, path, blender, dump, current) if dump.foreign
              else _exported_review(viewer, path, blender, dump))
    result = review.run(None if dump.foreign else same_mesh) if review is not None else None
    if result is None:
        return
    if dump.foreign and review.choice is not current:
        save_and_open(viewer, app, result, Path(path).stem)
        return
    try:
        apply_to_document(handler, result)
    except Exception as exc:
        QMessageBox.warning(viewer, translate("BlenderRoundTrip", "Blender import failed"), str(exc))


def import_mesh_from_blender(window):
    opened = _open_scene(window, window)
    if opened is None:
        return
    path, blender, dump = opened
    review = (_reference_review(window, window, path, blender, dump) if dump.foreign
              else _exported_review(window, path, blender, dump))
    result = review.run() if review is not None else None
    if result is not None:
        save_and_open(window, window, result, Path(path).stem)


__all__ = ["BlendImportReview", "apply_to_document", "import_into_viewer", "import_mesh_from_blender",
           "require_blender", "save_and_open"]
