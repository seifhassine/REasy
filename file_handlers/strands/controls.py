from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QT_TRANSLATE_NOOP, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QListWidget, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from file_handlers.mesh.device_preview import gather_master
from ui.background_jobs import BackgroundJobs
from ui.scene.device_frames import LIMIT_MOST
from file_handlers.mmtr.material_preview import MaterialPreviewError
from file_handlers.mmtr.pretransform import chain_level, pretransform_catalog_for
from file_handlers.mmtr.profiles import mmtr_profile_for_game
from utils.resource_file_utils import nearby_resource_paths
from .bindings import StrandBindings
from .chain_catalog import strand_catalog_for
from .chain_scene import (bound_mesh_path, character_candidates, chosen_character, handler_files, read_character,
                          read_set, shader_paths)
from .companions import prefab_graph
from .prefab_sets import prefab_sets
from .strands_file import StrandsFile

_BOUNDS_COLOURS = (0.3, 1.0, 0.6), (1.0, 0.65, 0.2)


@dataclass(frozen=True, slots=True)
class StrandSetEntry:
    name: str
    strands_path: str
    binding_path: str


@dataclass(frozen=True, slots=True)
class LoadedSet:
    entry: StrandSetEntry
    source: StrandsFile
    binding_mesh: str
    character: tuple[str, bytes]
    holder: int
    candidates: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PrefabStrands:
    path: str
    drawn: tuple[LoadedSet, ...]
    refused: Mapping[str, str]


def found_file(files, path: str, owner: str) -> tuple[str, bytes]:
    hit = files.find(path, owner)
    if hit is None:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP("MaterialPreview", "The file {path} was not found."),
                                   {"path": path})
    return hit


def prefab_strands(handler, prefab_path: str) -> PrefabStrands:
    game = handler.game_version
    catalog = strand_catalog_for(mmtr_profile_for_game(game))
    if catalog is None:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP("MaterialPreview",
                                                     "REasy has no catalog of this build's strand chain."))
    files = handler_files(handler, game)
    path, data = found_file(files, prefab_path, '')
    strands, binding = catalog.render.arguments['strands'], catalog.constants.arguments['binding']
    drawn, refused = [], {}
    for item in prefab_sets(prefab_graph(files.registry, path, data, game), catalog):
        entry = StrandSetEntry(str(item.owner.name), str(item.render[strands] or ''), str(item.dynamics[binding] or ''))
        try:
            drawn.append(load_set(handler, path, entry))
        except (ValueError, OSError) as error:
            refused[entry.name] = str(error)
    return PrefabStrands(path, tuple(drawn), refused)


def load_set(handler, prefab_path: str, entry: StrandSetEntry, character_path: str = '') -> LoadedSet:
    game = handler.game_version
    profile = mmtr_profile_for_game(game)
    files = handler_files(handler, game)
    if not entry.binding_path:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP("MaterialPreview", "The set has no binding file, so there is no "
                                                                        "mesh to draw it over."))
    bindings = StrandBindings.read(found_file(files, entry.binding_path, prefab_path)[1])
    bound = bindings.mesh_path or bound_mesh_path(
        bindings, strand_catalog_for(profile), prefab_graph(files.registry, *found_file(files, prefab_path, ''), game),
        entry.strands_path)
    mesh_path, data = found_file(files, bound, prefab_path)
    mesh = files.mesh(mesh_path, data)
    chains = pretransform_catalog_for(profile)
    read = read_set(files, game, profile, mesh, mesh_path, entry.strands_path, prefab_path, 0,
                    None if chains is None else chain_level(mesh, 0, chains))
    for path in shader_paths(read.catalog).values():
        found_file(files, path, '')
    if read.source.master and (reason := gather_master(read.source.master, lambda path: files.find(path, ''), {})):
        raise ValueError(reason)
    holding = character_candidates(files, game, read.catalog, read.graph, read.owner, mesh_path, character_path)
    drawn, refused = [], []
    for held in holding:
        try:
            choice = chosen_character([held], mesh_path)
            read_character(files, read, choice, 0)
        except (ValueError, OSError) as error:
            refused.append(str(error))
            continue
        drawn.append(choice)
    if not drawn:
        if not refused:
            chosen_character(holding, mesh_path)
        raise MaterialPreviewError(QT_TRANSLATE_NOOP("MaterialPreview", "No prefab holding the mesh {mesh} draws the "
                                                                        "strands: {reason}"),
                                   {"mesh": bound, "reason": refused[0]})
    path, _graph, (_document, holder, _fields) = drawn[0]
    return LoadedSet(entry, read.source.strands, bound, found_file(files, path, ''), holder.id.local_object_id,
                     tuple(choice[0] for choice in drawn))


def plain_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
    label.setTextFormat(Qt.PlainText)
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return label


class StrandsControls(QScrollArea):
    set_loaded = Signal(object)

    def __init__(self, viewer, prefab: str = '', *, editing: bool = False):
        super().__init__(viewer)
        self.viewer = viewer
        self.prefab: PrefabStrands | None = None
        self.loaded: LoadedSet | None = None
        self._jobs = BackgroundJobs(self)
        self.setWidgetResizable(True)
        self.setMinimumWidth(285)
        self.setMaximumWidth(440)
        body = QWidget(self)
        self.setWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(6, 6, 6, 6)
        self.status = plain_label('')
        layout.addWidget(self.status)
        self.resource = None
        if not prefab:
            self.resource = self._prefab_row(layout)
        layout.addWidget(QLabel(self.tr('Strand sets')))
        self.sets = QListWidget(body)
        self.sets.setToolTip(self.tr('The prefab\'s objects drawing strands, with the strands file each draws.'))
        self.sets.currentRowChanged.connect(self._choose)
        layout.addWidget(self.sets)
        layout.addWidget(plain_label(self.tr('Strands are drawn by the game\'s chain in Game lighting, over the mesh '
                                             'their set is bound to.')))
        self.options = QWidget(body)
        layout.addWidget(self.options)
        form = QFormLayout(self.options)
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.character = QComboBox()
        self.character.setEditable(True)
        self.character.setInsertPolicy(QComboBox.NoInsert)
        self.character.setMinimumContentsLength(12)
        self.character.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.character.setToolTip(self.tr("Shipped prefabs that hold the strands' bound mesh and draw them. The "
                                          "chosen one is shown with the prefab and gives the mesh's values and "
                                          "colliders."))
        self.character.activated.connect(self._reload)
        self.character.lineEdit().editingFinished.connect(self._reload)
        browse = QPushButton(self.tr('Browse…'))
        browse.clicked.connect(self._choose_character)
        row = QHBoxLayout()
        row.addWidget(self.character)
        row.addWidget(browse)
        form.addRow(self.tr('Character prefab'), row)
        self.enabled = QCheckBox(self.tr('Show strands'))
        self.enabled.toggled.connect(self.refresh)
        fit = QPushButton(self.tr('Fit strands'))
        fit.clicked.connect(self.fit)
        row = QHBoxLayout()
        row.addWidget(self.enabled)
        row.addWidget(fit)
        form.addRow(row)
        self.show_mesh = QCheckBox(self.tr('Show mesh'))
        self.show_mesh.setToolTip(self.tr('Show the mesh beneath the strands.'))
        self.show_mesh.setChecked(True)
        self.show_mesh.toggled.connect(self.refresh)
        form.addRow(self.show_mesh)
        self.bounds = QCheckBox(self.tr('Bounds'))
        self.bounds.setToolTip(self.tr('Green: geometry bounds. Orange: movement bounds.'))
        self.bounds.toggled.connect(self.refresh)
        form.addRow(self.bounds)
        self.stepping = plain_label('')
        self.stepping.setToolTip(self.tr("The chain steps the set once a frame at the view's Limit ({most} a second "
                                         "when it is 0), or slower when the device can't keep up. Set Limit to the "
                                         "game's frame rate to see the hair move as in game.").format(most=LIMIT_MOST))
        form.addRow(self.stepping)
        viewport = viewer.viewport
        if viewport is not None:
            viewport.strands_stepped.connect(self._stepped)
        self.editor = None
        if editing:
            from .edit_panel import StrandEditPanel
            self.editor = StrandEditPanel(self, self.options)
            self.editor.editing_changed.connect(self._editing_changed)
            form.addRow(self.editor)
        self.options.hide()
        layout.addStretch()
        if prefab:
            self.load_prefab(prefab)
        else:
            self.status.setText(self.tr('Choose the prefab holding the strands.'))

    def _prefab_row(self, layout) -> QComboBox:
        layout.addWidget(QLabel(self.tr('Prefab (PFB)')))
        combo = QComboBox()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.NoInsert)
        combo.setMinimumContentsLength(12)
        combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        combo.lineEdit().setPlaceholderText(self.tr('File or resource path'))
        combo.addItems(nearby_resource_paths(self.viewer.handler, 'pfb', parent_levels=1))
        combo.setCurrentIndex(-1)
        combo.lineEdit().returnPressed.connect(lambda: self.load_prefab(combo.currentText().strip()))
        layout.addWidget(combo)
        row = QHBoxLayout()
        browse, load = QPushButton(self.tr('Browse…')), QPushButton(self.tr('Load'))
        browse.clicked.connect(lambda: self._browse(combo))
        load.clicked.connect(lambda: self.load_prefab(combo.currentText().strip()))
        row.addWidget(browse)
        row.addWidget(load)
        layout.addLayout(row)
        return combo

    def _browse(self, combo) -> None:
        path, _ = QFileDialog.getOpenFileName(self, self.tr('Prefab (PFB)'), '',
                                              'PFB (*.pfb *.pfb.*);;' + self.tr('All files (*)'))
        if path:
            combo.setEditText(path)
            self.load_prefab(path)

    def _choose_character(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, self.tr('Character prefab'), '',
                                              'PFB (*.pfb *.pfb.*);;' + self.tr('All files (*)'))
        if path:
            self.character.setEditText(path)
            self._reload()

    def _start(self, operation, done) -> None:
        self.status.setText(self.tr('Loading…'))
        self._jobs.start(operation, done, self.status.setText)

    def load_prefab(self, path: str) -> None:
        if path:
            handler = self.viewer.handler
            self.status.setText(self.tr('Checking which strand sets REasy can draw…'))
            self._jobs.start(lambda: prefab_strands(handler, path), self._listed, self.status.setText)

    def _listed(self, prefab: PrefabStrands) -> None:
        self.prefab = prefab
        self.sets.blockSignals(True)
        self.sets.clear()
        for loaded in prefab.drawn:
            entry = loaded.entry
            name = entry.strands_path.replace('\\', '/').rsplit('/', 1)[-1]
            self.sets.addItem(f'{entry.name} · {name}')
            self.sets.item(self.sets.count() - 1).setToolTip(entry.strands_path)
        self.sets.setCurrentRow(-1)
        self.sets.blockSignals(False)
        if prefab.drawn:
            self.sets.setCurrentRow(0)
        else:
            self.status.setText('\n'.join((self.tr('The prefab holds no strand set REasy can draw.'),
                                           *self._left_out())))

    def _left_out(self) -> list[str]:
        return [f'{name}: {reason}' for name, reason in self.prefab.refused.items()]

    def _choose(self, row: int) -> None:
        if self.prefab is not None and 0 <= row < len(self.prefab.drawn):
            self._loaded(self.prefab.drawn[row])

    def _reload(self, *_) -> None:
        chosen = self.character.currentText().strip()
        if self.loaded is not None and chosen and chosen != self.loaded.character[0]:
            handler, path, entry = self.viewer.handler, self.prefab.path, self.loaded.entry
            self._start(lambda: load_set(handler, path, entry, chosen), self._loaded)

    def _loaded(self, loaded: LoadedSet) -> None:
        self.loaded = loaded
        source, character = loaded.source, loaded.character[0]
        self.character.blockSignals(True)
        self.character.clear()
        self.character.addItems(loaded.candidates)
        self.character.setCurrentIndex(loaded.candidates.index(character) if character in loaded.candidates else -1)
        self.character.setEditText(character)
        self.character.blockSignals(False)
        lines = [self.tr(
            "{count} strands over the mesh in {character} ({drawn} prefabs hold that mesh and draw them).").format(
            count=f'{source.lods[0].strand_count:,}', drawn=len(loaded.candidates),
            character=character.replace('\\', '/').rsplit('/', 1)[-1])]
        if left_out := self._left_out():
            lines += [self.tr('Left out, as REasy cannot draw them:'), *left_out]
        self.status.setText('\n'.join(lines))
        self.enabled.setChecked(True)
        self.options.show()
        self.set_loaded.emit(loaded)
        self.refresh()

    def _stepped(self, stepped) -> None:
        if stepped is None:
            self.stepping.setText('')
            return
        limit = self.viewer.viewport.fps_limit
        wanted = limit or LIMIT_MOST
        rate = f'{round(stepped.rate, 1):g}'
        if stepped.rate < wanted:
            line = self.tr("The chain steps {rate} times a second, as fast as the device draws (the view's Limit "
                           "asks {wanted}).").format(rate=rate, wanted=wanted)
        elif limit:
            line = self.tr("The chain steps {rate} times a second, at the view's Limit.").format(rate=rate)
        else:
            line = self.tr("The chain steps {rate} times a second, the most it steps at while the view's Limit is "
                           "0 (uncapped).").format(rate=rate)
        lines = [line]
        if stepped.sub_step is not None:
            lines.append(self.tr('The set simulates {simulated} steps a second in {sub} sub steps: a sub step lasts '
                                 '{length} ms (1/{per} s).').format(
                simulated=f'{round(stepped.simulated, 1):g}', sub=stepped.sub_steps,
                length=f'{stepped.sub_step * 1000:.3f}', per=f'{round(stepped.simulated * stepped.sub_steps):g}'))
        self.stepping.setText('\n'.join(lines))

    def _editing_changed(self, editing: bool) -> None:
        for widget in (self.sets, self.character):
            widget.setEnabled(not editing)
        if self.resource is not None:
            self.resource.setEnabled(not editing)

    def edited_files(self) -> dict:
        return {} if self.editor is None else self.editor.edited_files()

    def shown(self):
        if self.loaded is None or self.prefab is None or not self.enabled.isChecked():
            return None
        return self.loaded.entry.strands_path, self.prefab.path, self.loaded.character[0]

    def boxes(self):
        if self.loaded is None or not self.bounds.isChecked():
            return ()
        source = self.loaded.source
        return tuple((np.asarray(box.min, np.float64), np.asarray(box.max, np.float64), colour)
                     for box, colour in zip((source.bounds, source.movement_bounds), _BOUNDS_COLOURS)
                     if box is not None)

    def refresh(self, *_):
        viewport = self.viewer.viewport
        if viewport is not None:
            viewport.strands_only = not self.show_mesh.isChecked()
            viewport.strands_boxes = self.boxes()
            viewport.update()
        self.viewer.strands_changed()

    def fit(self):
        viewport = self.viewer.viewport
        if self.loaded is not None and viewport is not None:
            records = self.loaded.source.lods[0].vertex_records
            viewport.fit_points(np.frombuffer(records, '<f4').reshape(-1, 4)[:, :3])

    def cleanup(self):
        if self.editor is not None:
            self.editor.cleanup()
        self._jobs.close()
        self.loaded = None
        self.options.hide()
        self.refresh()


__all__ = ["LoadedSet", "PrefabStrands", "StrandSetEntry", "StrandsControls", "found_file", "load_set", "plain_label",
           "prefab_strands"]
