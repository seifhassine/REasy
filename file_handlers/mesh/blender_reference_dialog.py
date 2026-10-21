from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

lazy from PySide6.QtCore import QSortFilterProxyModel, QStringListModel, Qt
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListView, QPushButton, QRadioButton, QStackedWidget, QVBoxLayout, QWidget,
)

lazy from settings import app_settings, remember_setting
lazy from ui.background_jobs import BackgroundJobs
lazy from ui.editor_widgets import styled_label
lazy from ui.styles import set_style_role
lazy from utils.resource_file_utils import resource_context_for_app, resource_version_from_path, streaming_companion_path

from .blender import Reference, reference_bones, suggest
lazy from .mesh_file import MeshFile
lazy from file_handlers.pak import scan_pak_files
lazy from file_handlers.pak.reader import CachedPakReader
lazy from ui.project_manager.constants import BASE_DIR
lazy from ui.project_manager.pak_file_lists import find_default_pak_list_path, read_pak_list_file
lazy from app_config import GAMES

DIRECTORIES_KEY = "blender_reference_directories"


@dataclass
class ReferenceChoice:
    reference: Reference
    mesh: object
    label: str


def _mesh_from_bytes(data, streaming, file_version, label):
    mesh = MeshFile()
    mesh.read(data, file_version=int(file_version), streaming_data=streaming)
    return ReferenceChoice(Reference.from_mesh(mesh, file_version, label), mesh, label)


def load_disk_reference(path):
    path = Path(path)
    version = resource_version_from_path(str(path), "mesh")
    if version is None:
        raise ValueError(f"{path.name} is not a versioned .mesh file")
    companion = streaming_companion_path(str(path))
    streaming = Path(companion).read_bytes() if companion and Path(companion).is_file() else None
    return _mesh_from_bytes(path.read_bytes(), streaming, version, path.name)


def load_pak_reference(reader, path):
    def read(name):
        handle = reader.get_file(name) if name else None
        return handle.read() if handle is not None else None

    data = read(path)
    if data is None:
        raise ValueError(f"{path} is not in the game files")
    return _mesh_from_bytes(data, read(streaming_companion_path(path)), resource_version_from_path(path, "mesh"),
                            path.rsplit("/", 1)[-1])


def open_game_files(game, directory):
    paks = scan_pak_files(directory)
    if not paks:
        raise ValueError(f"No game PAK files in {directory}")
    reader = CachedPakReader.from_paks(paks, game=game)
    listing = find_default_pak_list_path(game, Path(BASE_DIR))
    if listing is not None:
        reader.add_files(*read_pak_list_file(listing))
    reader.cache_entries(assign_paths=True)
    return reader, _mesh_paths(reader)


def _mesh_paths(reader):
    return sorted(path for path in reader.cached_known_paths()
                  if ".mesh." in path.casefold() and "/streaming/" not in path.casefold())


def _project_reader(app):
    context = resource_context_for_app(app)
    if context is None or context.pak_cached_reader is None or not context.game:
        return None
    return context.game, context.pak_cached_reader


class ReferencePicker(QDialog):
    def __init__(self, source_bones, weighted, app=None, current=None, parent=None):
        super().__init__(parent)
        self.source_bones, self.weighted, self.app = source_bones, weighted, app
        self.current = current
        self.project = _project_reader(app)
        self.choice = self.reader = None
        self._indexing, self._loads = BackgroundJobs(self), BackgroundJobs(self)
        self.finished.connect(self._close_jobs)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.PlainText)
        self.setWindowTitle(self.tr("Choose a reference mesh"))
        self.resize(820, 600)
        layout = QVBoxLayout(self)
        intro = styled_label(self.tr(
            "The new mesh takes the reference's game, skeleton, LOD layout and engine data. Its own geometry, "
            "materials and morphs are replaced by the scene's. Pick a mesh whose skeleton the model is rigged to."),
            tone="muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        sources = QHBoxLayout()
        self.sources = QButtonGroup(self)
        labels = [self.tr("The open mesh"), self.tr("Game files"), self.tr("Mesh file on disk")]
        for number, text in enumerate(labels):
            button = QRadioButton(text)
            button.setEnabled(number != 0 or current is not None)
            self.sources.addButton(button, number)
            sources.addWidget(button)
        sources.addStretch(1)
        layout.addLayout(sources)

        self.pages = QStackedWidget()
        self.pages.addWidget(QLabel(current.label if current is not None else ""))
        self.pages.addWidget(self._game_page())
        self.pages.addWidget(self._disk_page())
        layout.addWidget(self.pages, 1)
        layout.addWidget(self.info)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText(self.tr("Use this reference"))
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.sources.idToggled.connect(self._source_changed)
        self.sources.button(0 if current is not None else 1).setChecked(True)

    def _game_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self.game = QComboBox()
        self.game.addItems(GAMES)
        self.directory = QLineEdit()
        self.directory.setPlaceholderText(self.tr("The game's installation folder"))
        browse = QPushButton(self.tr("Browse…"))
        self.load_game = QPushButton(self.tr("Load"))
        row = QHBoxLayout()
        row.addWidget(self.directory, 1)
        row.addWidget(browse)
        row.addWidget(self.load_game)
        form.addRow(self.tr("Game:"), self.game)
        form.addRow(self.tr("Folder:"), row)
        layout.addLayout(form)
        self.search = QLineEdit()
        self.search.setPlaceholderText(self.tr("Search mesh paths, e.g. character body"))
        layout.addWidget(self.search)
        self.paths = QStringListModel(self)
        self.filtered = QSortFilterProxyModel(self)
        self.filtered.setSourceModel(self.paths)
        self.filtered.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.list = QListView()
        self.list.setModel(self.filtered)
        self.list.setUniformItemSizes(True)
        layout.addWidget(self.list, 1)

        if self.project is not None and self.project[0] in GAMES:
            self.game.setCurrentText(self.project[0])
        self.game.currentTextChanged.connect(self._game_changed)
        browse.clicked.connect(self._browse_directory)
        self.load_game.clicked.connect(self._open_game)
        self.search.textChanged.connect(self._filter)
        self.list.selectionModel().currentChanged.connect(self._pak_selected)
        self._game_changed(self.game.currentText())
        return page

    def _disk_page(self):
        page = QWidget()
        layout = QHBoxLayout(page)
        self.file = QLineEdit()
        self.file.setReadOnly(True)
        browse = QPushButton(self.tr("Choose a .mesh file…"))
        layout.addWidget(self.file, 1)
        layout.addWidget(browse)
        layout.setAlignment(Qt.AlignTop)
        browse.clicked.connect(self._browse_file)
        return page

    def _directories(self):
        value = (app_settings(self.app) or {}).get(DIRECTORIES_KEY, {})
        return value if isinstance(value, dict) else {}

    def _game_changed(self, game):
        self.reader = None
        self.paths.setStringList([])
        self.directory.setText(self._directories().get(game, ""))
        if self.project is not None and self.project[0] == game:
            self.reader = self.project[1]
            self.paths.setStringList(_mesh_paths(self.reader))
            self._tell(self.tr("Using the open project's game files."))

    def _browse_directory(self):
        chosen = QFileDialog.getExistingDirectory(self, self.tr("Game installation folder"), self.directory.text())
        if chosen:
            self.directory.setText(chosen)
            self._open_game()

    def _open_game(self):
        game, directory = self.game.currentText(), self.directory.text().strip()
        if not directory:
            return
        self._remember_directory(game, directory)
        self._tell(self.tr("Indexing the game files…"))
        self.load_game.setEnabled(False)
        self._indexing.start(lambda: open_game_files(game, directory), self._game_opened, self._game_failed)

    def _game_opened(self, result):
        self.load_game.setEnabled(True)
        self.reader, paths = result
        self.paths.setStringList(paths)
        self._tell(self.tr("{count} meshes. Select one.").format(count=len(paths)))

    def _game_failed(self, error):
        self.load_game.setEnabled(True)
        self._tell(self.tr("The game files could not be read: {error}").format(error=error), "danger")

    def _remember_directory(self, game, directory):
        directories = dict(self._directories(), **{game: directory})
        remember_setting(self.app, DIRECTORIES_KEY, directories)

    def _filter(self, text):
        self.filtered.setFilterFixedString(text.strip())

    def _pak_selected(self, current, _previous):
        if not current.isValid() or self.reader is None:
            return
        path, reader = current.data(), self.reader
        self._load(lambda: load_pak_reference(reader, path), path)

    def _browse_file(self):
        path, _filter = QFileDialog.getOpenFileName(self, self.tr("Reference mesh"), self.file.text(),
                                                    self.tr("Meshes (*.mesh.*)"))
        if path:
            self.file.setText(path)
            self._load(lambda: load_disk_reference(path), Path(path).name)

    def _tell(self, text: str, tone: str = "") -> None:
        self.info.setText(text)
        set_style_role(self.info, tone=tone)

    def _close_jobs(self):
        self._indexing.close()
        self._loads.close()

    def _source_changed(self, number, checked):
        if not checked:
            return
        self.pages.setCurrentIndex(number)
        self._loads.close()
        self._loads = BackgroundJobs(self)
        self._show(self.current if number == 0 else None, "")

    def _load(self, work, label):
        self.choice = None
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(False)
        self._tell(self.tr("Reading {name}…").format(name=label))
        self._loads.start(work, lambda choice: self._show(choice, ""), lambda error: self._show(None, error))

    def _show(self, choice, error):
        self.choice = None if error else choice
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(self.choice is not None)
        if error:
            self._tell(self.tr("This mesh cannot be a reference: {error}").format(error=error), "danger")
        elif choice is not None:
            self._tell(self._describe(choice))

    def _describe(self, choice):
        mesh = choice.mesh
        lines = [self.tr("{name} · {game} · version {version}").format(
            name=choice.label, game=mesh.profile.display_name, version=choice.reference.file_version)]
        lods = len(mesh.meshes[0].lods) if mesh.meshes else 0
        lines.append(self.tr("{joints} joints · {lods} LODs · {materials} materials").format(
            joints=len(mesh.bones), lods=lods, materials=len(mesh.material_names)))
        if not mesh.bones:
            lines.append(self.tr("No skeleton: the new mesh is static and the model's weights are ignored."))
        else:
            mapping = suggest(self.source_bones, reference_bones(mesh), self.weighted)
            weighted = [row for row in mapping.rows if row.weighted]
            matched = sum(row.mapped for row in weighted)
            lines.append(self.tr("{matched} of {total} weighted bones or vertex groups match its joints.").format(
                matched=matched, total=len(weighted)))
        return "\n".join(lines)


__all__ = ["ReferenceChoice", "ReferencePicker", "load_disk_reference", "load_pak_reference", "open_game_files"]
