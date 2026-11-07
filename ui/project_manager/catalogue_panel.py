"""See docs/internals/character-assembly.md, Catalogue."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import PurePosixPath

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

lazy from file_handlers.rsz.character_catalogs import CatalogFile, catalog_entries, rsz_reader
lazy from ui.background_jobs import BackgroundJobs
lazy from ui.editor_widgets import search_field, styled_label, tool_button
lazy from ui.icons import line_icon
lazy from ui.styles import current_colors, set_style_role

_TARGET = Qt.UserRole


class CataloguePanel(QWidget):
    """``context`` gives the current game and a resource path's reader ((path, bytes), None when not found).
    ``open_requested`` gives a file, its spawner instance (None: the file's own) and the choices picked at its
    levels."""
    open_requested = Signal(str, object, object)

    def __init__(self, context: Callable[[], tuple[str, Callable]], parent: QWidget | None = None):
        super().__init__(parent)
        self._context, self._game, self._files, self._reads, self._generation = context, None, {}, {}, 0
        self._jobs, self._fills = BackgroundJobs(self), BackgroundJobs(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        toolbar = QWidget(self)
        toolbar.setObjectName("projectBrowserToolbar")
        toolbar.setAttribute(Qt.WA_StyledBackground, True)
        set_style_role(toolbar, surface="section")
        row = QHBoxLayout(toolbar)
        row.setContentsMargins(6, 6, 6, 6)
        row.setSpacing(4)
        self._filter_edit = search_field(QLineEdit(toolbar))
        self._filter_edit.setPlaceholderText(self.tr("Filter characters"))
        self._filter_edit.setToolTip(self.tr("Shows the entries listed so far whose name holds the text"))
        row.addWidget(self._filter_edit, 1)
        row.addWidget(tool_button("refresh", self.tr("List the game's catalogs again"), self.reload, parent=toolbar))
        layout.addWidget(toolbar)
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(120)
        self._filter_timer.timeout.connect(self._filter)
        self._filter_edit.textChanged.connect(lambda _text: self._filter_timer.start())
        self._tree = QTreeWidget(self)
        self._tree.setHeaderHidden(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setIndentation(12)
        self._tree.setExpandsOnDoubleClick(False)
        self._tree.setTextElideMode(Qt.ElideMiddle)
        self._tree.itemExpanded.connect(self._fill)
        self._tree.itemActivated.connect(self._open)
        layout.addWidget(self._tree, 1)
        self._empty = QWidget(self)
        self._empty.setAttribute(Qt.WA_StyledBackground, True)
        set_style_role(self._empty, surface="view")
        column = QVBoxLayout(self._empty)
        column.setContentsMargins(16, 16, 16, 16)
        column.addStretch(1)
        self._empty_icon = QLabel(self._empty)
        self._empty_icon.setAlignment(Qt.AlignCenter)
        column.addWidget(self._empty_icon)
        self._empty_label = styled_label("", self._empty, tone="muted")
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setWordWrap(True)
        column.addWidget(self._empty_label)
        column.addStretch(2)
        layout.addWidget(self._empty, 1)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        icon = line_icon("user", current_colors()["text_subtle"])
        self._empty_icon.setPixmap(icon.pixmap(QSize(24, 24), self.devicePixelRatioF()))
        if self._context()[0] != self._game:
            self.reload()

    def reload(self) -> None:
        game, read = self._context()
        self._game, self._files, self._reads, self._generation = game, {}, {}, self._generation + 1
        self._tree.clear()
        if not game:
            return self._show_empty(self.tr("Open a project to list its game's characters."))
        self._show_empty(self.tr("Listing the game's catalogs…"))
        self._jobs.start(partial(catalog_entries, game, rsz_reader(game, read)), self._listed, self._show_empty)

    def sources_changed(self) -> None:
        self._game = None
        if self.isVisible():
            self.reload()

    def _show_empty(self, text: str) -> None:
        self._empty_label.setText(text)
        self._empty.setVisible(bool(text))
        self._tree.setVisible(not text)

    def _listed(self, entries) -> None:
        if not entries:
            return self._show_empty(self.tr("No characters found for {game}. The catalogs REasy knows are read "
                                            "from the project's PAK files or unpacked folder.").format(game=self._game))
        self._show_empty("")
        groups: dict[tuple[str, str], list] = {}
        for entry in entries:
            groups.setdefault((entry.catalog, entry.group), []).append(entry)
        catalogs: dict[str, QTreeWidgetItem] = {}
        for (catalog, group), listed in groups.items():
            if catalog not in catalogs:
                catalogs[catalog] = _item(self._tree, catalog)
            parent = _item(catalogs[catalog], group) if group else catalogs[catalog]
            for entry in listed:
                item = parent if len(listed) == 1 else _item(parent, PurePosixPath(entry.path).name)
                item.setToolTip(0, entry.path)
                _target(item, (entry.path, None, ()), True)
        for item in catalogs.values():
            item.setExpanded(len(catalogs) == 1 or item.data(0, _TARGET) is not None)
        self._filter()

    def _fill(self, item: QTreeWidgetItem) -> None:
        """In the background: a part swapper's characters take seconds to read."""
        target = item.data(0, _TARGET)
        if target is not None and not item.childCount():
            self._fills.start(partial(_children, self._files, self._reads, self._game, self._context()[1], *target),
                              partial(self._filled, item, self._generation), lambda _error: None)

    def _filled(self, item: QTreeWidgetItem, generation: int, children: list) -> None:
        if generation != self._generation or item.childCount():
            return
        for text, target, expandable in children:
            _target(_item(item, text), target, expandable)
        item.setChildIndicatorPolicy(QTreeWidgetItem.DontShowIndicatorWhenChildless)
        self._filter()

    def _open(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        target = item.data(0, _TARGET)
        if target is None:
            item.setExpanded(not item.isExpanded())
        else:
            self.open_requested.emit(*target)

    def _filter(self) -> None:
        needle = self._filter_edit.text().strip().casefold()
        for index in range(self._tree.topLevelItemCount()):
            _shown(self._tree.topLevelItem(index), needle, not needle)


def _children(files: dict, reads: dict, game: str, read: Callable, path: str, instance, picks: tuple) -> list:
    if path not in files:
        files[path] = CatalogFile.of(game, path, read, reads)
    found = files[path]
    shown = [spawner for spawner in found.spawners if instance in (None, spawner[0])]
    if len(shown) > 1:
        return [(name.rpartition(".")[2], (path, spawner, ()), levels > 0) for spawner, name, levels in shown]
    return [(label, (path, spawner, (*picks, index)), len(picks) + 1 < levels) for spawner, _name, levels in shown
            for index, label in enumerate(found.choices(spawner, picks))]


def _item(parent, text: str) -> QTreeWidgetItem:
    item = QTreeWidgetItem(parent, [text])
    item.setToolTip(0, text)
    return item


def _target(item: QTreeWidgetItem, target: tuple, expandable: bool) -> None:
    item.setData(0, _TARGET, target)
    if expandable:
        item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)


def _shown(item: QTreeWidgetItem, needle: str, matched: bool) -> bool:
    here = matched or needle in item.text(0).casefold()
    below = [_shown(item.child(index), needle, here) for index in range(item.childCount())]
    if needle and any(below):
        item.setExpanded(True)
    item.setHidden(not (here or any(below)))
    return here or any(below)
