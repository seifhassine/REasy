from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView,
                               QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

lazy from file_handlers.rtex import rtex_version
lazy from file_handlers.tex.kinds import cook_version
lazy from ui.editor_widgets import search_field
lazy from ui.styles import current_colors
lazy from utils.resource_file_utils import (_locate_in_root, _resource_path_candidates, find_matching_pak_path,
                                            held_folder_lookups, natives_split, resource_context_for_handler,
                                            resource_path_key, resource_path_with_version)

_ORDER = ("missing", "unchecked", "project", "game")


def texture_uses(mdf) -> dict[str, list]:
    uses = {}
    for material in mdf.materials:
        for texture in material.textures:
            if key := resource_path_key(texture.tex_path):
                uses.setdefault(key, []).append((material, texture))
    return uses


def texture_sources(handler, keys) -> dict[str, tuple[str, str]]:
    context = resource_context_for_handler(handler)
    versions = {"tex": cook_version(context.game), "rtex": rtex_version(context.game)}
    own = natives_split(handler.filepath) if Path(handler.filepath).is_absolute() else None
    roots = context.project_dir, str(own[0].parents[1]) if own else ""
    sources = {}
    with held_folder_lookups():
        for key in keys:
            extension = key.rpartition(".")[2]
            name = resource_path_with_version(key, extension, versions[extension]) if versions.get(extension) else key
            if loose := next(filter(None, (_locate_in_root(name, root, context.path_prefix) for root in roots)), None):
                sources[key] = "project", loose
            elif packed := find_matching_pak_path(context.pak_cached_reader, _resource_path_candidates(name),
                                                  allow_selection_dialog=False):
                sources[key] = "game", packed
            else:
                sources[key] = "missing" if context.pak_cached_reader else "unchecked", name
    return sources


class MdfTextureList(QDialog):
    def __init__(self, handler, completer, parent):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Textures in {file}").format(file=Path(handler.filepath).name))
        self.resize(900, 520)
        self._handler, self._sources, self._uses, self.renamed = handler, {}, {}, False
        self._labels = {"project": self.tr("Project"), "game": self.tr("Game"), "missing": self.tr("Not found"),
                        "unchecked": self.tr("Not in project")}
        self._tips = {"project": "{path}", "game": self.tr("In the game's PAKs: {path}"),
                      "missing": self.tr("Neither the project nor the game's PAKs hold {path}."),
                      "unchecked": self.tr("Not in the project as {path}. The game's PAKs aren't loaded, so REasy "
                                           "can't check them.")}
        self.filter_edit = search_field(QLineEdit())
        self.filter_edit.setPlaceholderText(self.tr("Filter textures or materials..."))
        self.filter_edit.textChanged.connect(lambda: self._fill())
        self.show_game = QCheckBox()
        self.show_game.setToolTip(self.tr(
            "Textures found only in the game's PAKs, such as the engine's null textures."))
        self.show_game.toggled.connect(lambda: self._fill())
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels([self.tr("Path"), self.tr("Found in"), self.tr("Used by")])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setTextElideMode(Qt.ElideMiddle)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.resizeSection(2, 260)
        self.table.itemSelectionChanged.connect(self._show_selected)
        self.table.itemDoubleClicked.connect(lambda: self.path_edit.setFocus(Qt.FocusReason.ShortcutFocusReason))
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText(self.tr("Select a texture to rename it"))
        completer.attach(self.path_edit)
        self.path_edit.textChanged.connect(self._update_rename)
        self.path_edit.returnPressed.connect(self._rename)
        self.rename_button = QPushButton(self.tr("Rename"))
        self.rename_button.setToolTip(self.tr("Changes this path in every material and slot listed under Used by."))
        self.rename_button.clicked.connect(self._rename)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        top, bottom, layout = QHBoxLayout(), QHBoxLayout(), QVBoxLayout(self)
        top.addWidget(self.filter_edit, 1)
        top.addWidget(self.show_game)
        bottom.addWidget(self.path_edit, 1)
        bottom.addWidget(self.rename_button)
        layout.addLayout(top)
        layout.addWidget(self.table, 1)
        layout.addLayout(bottom)
        layout.addWidget(buttons)
        self._fill()

    def _key(self) -> str:
        item = self.table.item(self.table.currentRow(), 0)
        return "" if item is None else item.data(Qt.UserRole)

    def _fill(self, select: str | None = None):
        select = self._key() if select is None else select
        self._uses = uses = texture_uses(self._handler.mdf)
        self._sources.update(texture_sources(self._handler, uses.keys() - self._sources.keys()))
        status = {key: self._sources[key][0] for key in uses}
        self.show_game.setText(self.tr("Show game textures ({count})").format(
            count=sum(found == "game" for found in status.values())))
        needle = self.filter_edit.text().casefold()
        rows = sorted((key for key, entries in uses.items() if (self.show_game.isChecked() or status[key] != "game")
                       and (needle in key or any(needle in m.header.mat_name.casefold() for m, _t in entries))),
                      key=lambda key: (_ORDER.index(status[key]), key))
        with QSignalBlocker(self.table):
            self.table.setRowCount(len(rows))
            for row, key in enumerate(rows):
                entries, (found, where) = uses[key], self._sources[key]
                path = QTableWidgetItem(entries[0][1].tex_path)
                path.setData(Qt.UserRole, key)
                path.setToolTip(path.text())
                state = QTableWidgetItem(self._labels[found])
                state.setToolTip(self._tips[found].format(path=where))
                if found == "missing":
                    state.setForeground(QColor(current_colors()["danger"]))
                used = QTableWidgetItem(", ".join(dict.fromkeys(m.header.mat_name for m, _t in entries)))
                used.setToolTip("\n".join(f"{m.header.mat_name}: {t.tex_type}" for m, t in entries))
                for column, item in enumerate((path, state, used)):
                    self.table.setItem(row, column, item)
            self.table.setCurrentCell(rows.index(select) if select in rows else -1, 0)
        self._show_selected()

    def _show_selected(self):
        key = self._key()
        self.path_edit.setEnabled(bool(key))
        self.path_edit.setText(self._uses[key][0][1].tex_path if key else "")
        self._update_rename()

    def _update_rename(self):
        entries, text = self._uses.get(self._key(), ()), self.path_edit.text()
        count = len({id(material) for material, _t in entries})
        self.rename_button.setText(self.tr("Rename in {count} material(s)").format(count=count) if count
                                   else self.tr("Rename"))
        self.rename_button.setEnabled(bool(entries and resource_path_key(text)) and text != entries[0][1].tex_path)

    def _rename(self):
        if self.rename_button.isEnabled():
            text = self.path_edit.text()
            for _material, texture in self._uses[self._key()]:
                texture.tex_path = text
            self.renamed = True
            self._fill(resource_path_key(text))
