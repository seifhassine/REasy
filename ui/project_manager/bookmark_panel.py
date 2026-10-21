from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QModelIndex, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QCompleter,
    QDialog,
    QFileDialog,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

lazy from ui.editor_widgets import styled_label, tool_button
lazy from ui.icons import line_icon
lazy from ui.styles import current_colors, set_style_role

lazy from .bookmark_dialog import BookmarkDialog
lazy from .bookmarks import Bookmark, BookmarksStore, normalize_root
lazy from .delegate import _BookmarkRowDelegate

_ContextProvider = Callable[[], tuple[str | None, str | None]]


class BookmarksPanel(QWidget):
    changed = Signal()
    open_requested = Signal(object)

    def __init__(
        self,
        context_provider: _ContextProvider,
        parent: QWidget | None = None,
        store: BookmarksStore | None = None,
    ):
        super().__init__(parent)
        self._context_provider = context_provider
        self._store = store if store is not None else BookmarksStore(parent=self)
        self._active_tag: str | None = None
        self._rendered_tags: tuple[str, ...] | None = None
        self._rendered_ids: list[str] | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        toolbar = QWidget(self)
        toolbar.setObjectName("projectBrowserToolbar")
        toolbar.setAttribute(Qt.WA_StyledBackground, True)
        set_style_role(toolbar, surface="section")
        toolbar_layout = QVBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(6, 6, 6, 6)
        toolbar_layout.setSpacing(4)
        toolbar_layout.addLayout(self._build_toolbar())
        layout.addWidget(toolbar)

        filters = QHBoxLayout()
        filters.setSpacing(6)
        self._tag_combo = QComboBox()
        self._tag_combo.setEditable(True)
        self._tag_combo.setInsertPolicy(QComboBox.NoInsert)
        self._tag_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self._tag_combo.setMinimumContentsLength(6)
        self._tag_combo.setMinimumWidth(0)
        self._tag_combo.setMaxVisibleItems(12)
        self._tag_combo.setAccessibleName(self.tr("Filter by tag"))
        self._tag_combo.setToolTip(self.tr("Choose a tag, or type to find one"))
        self._tag_combo.completer().setCompletionMode(QCompleter.PopupCompletion)
        self._tag_combo.completer().setFilterMode(Qt.MatchContains)
        self._tag_combo.completer().setCaseSensitivity(Qt.CaseInsensitive)
        self._tag_combo.currentIndexChanged.connect(lambda: self._set_tag(self._tag_combo.currentData()))
        self._tag_combo.lineEdit().editingFinished.connect(self._finish_tag_edit)
        filters.addWidget(self._tag_combo, 1)
        self._count_label = styled_label(tone="muted")
        filters.addWidget(self._count_label)
        toolbar_layout.addLayout(filters)

        self._model = QStandardItemModel(self)
        self._model.setHorizontalHeaderLabels([self.tr("Bookmarks")])
        self._tree = self._build_tree()
        layout.addWidget(self._tree)

        self._empty_state = QWidget(self)
        self._empty_state.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._empty_state.setAttribute(Qt.WA_StyledBackground, True)
        set_style_role(self._empty_state, surface="view")
        empty_layout = QVBoxLayout(self._empty_state)
        empty_layout.setContentsMargins(16, 16, 16, 16)
        empty_layout.setSpacing(6)
        empty_layout.addStretch(1)
        self._empty_icon = QLabel(self._empty_state)
        self._empty_icon.setAlignment(Qt.AlignCenter)
        empty_layout.addWidget(self._empty_icon)
        self._empty_label = styled_label(self.tr("No bookmarks"), self._empty_state, tone="muted")
        self._empty_label.setAlignment(Qt.AlignCenter)
        empty_layout.addWidget(self._empty_label)
        self._empty_hint = styled_label(
            self.tr("Click the star on a file or folder, or right-click it and choose Bookmark & tag…"),
            self._empty_state,
            tone="subtle",
        )
        self._empty_hint.setAlignment(Qt.AlignCenter)
        self._empty_hint.setWordWrap(True)
        empty_layout.addWidget(self._empty_hint)
        empty_layout.addStretch(2)
        self._empty_state.hide()
        layout.addWidget(self._empty_state)

        self._store.changed.connect(self._on_store_changed)
        # Deferred until the panel is up; a child timer dies with the panel.
        self._warnings_timer = QTimer(self)
        self._warnings_timer.setSingleShot(True)
        self._warnings_timer.setInterval(0)
        self._warnings_timer.timeout.connect(self._show_load_warnings)
        if self._store.load_warnings:
            self._warnings_timer.start()

    def set_store(self, store: BookmarksStore) -> None:
        """Swap the backing store (e.g. a project's own bookmarks file)."""
        if store is self._store:
            return
        try:
            self._store.changed.disconnect(self._on_store_changed)
        except (RuntimeError, TypeError):
            pass
        self._store = store
        store.changed.connect(self._on_store_changed)
        if store.load_warnings:
            self._warnings_timer.start()
        self.refresh()

    def _build_toolbar(self) -> QHBoxLayout:
        toolbar = QHBoxLayout()
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText(self.tr("Search paths, tags and notes…"))
        self._filter_edit.setToolTip(
            self.tr('All words must match; use tag:name, "quoted phrase" or -exclude')
        )
        self._filter_edit.setClearButtonEnabled(True)
        self._filter_edit.setAccessibleName(self.tr("Search bookmarks"))
        self._filter_edit.setMinimumWidth(0)
        toolbar.addWidget(self._filter_edit, 1)

        self._filter_timer = QTimer(self)
        self._filter_timer.setInterval(120)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.timeout.connect(self._populate)
        self._filter_edit.textChanged.connect(lambda _text: self._filter_timer.start())

        menu = QMenu(self)
        for text, tooltip, callback in (
            (self.tr("Import…"), self.tr("Import bookmarks from a JSON file"), self._import),
            (self.tr("Export…"), self.tr("Export bookmarks to a JSON file"), self._export),
        ):
            action = menu.addAction(text, callback)
            action.setToolTip(tooltip)
        options = tool_button("more", self.tr("Import or export bookmarks"))
        # The icon already reads as a menu: no arrow, and no room kept for one.
        options.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0px; }")
        options.setAccessibleName(self.tr("Bookmark options"))
        options.setMenu(menu)
        options.setPopupMode(QToolButton.InstantPopup)
        toolbar.addWidget(options)
        return toolbar

    def _build_tree(self) -> QTreeView:
        tree = QTreeView()
        tree.setModel(self._model)
        tree.setUniformRowHeights(True)
        tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tree.setContextMenuPolicy(Qt.CustomContextMenu)
        tree.setIndentation(0)
        tree.setRootIsDecorated(False)
        tree.setHeaderHidden(True)
        tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        tree.header().setStretchLastSection(False)
        tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        tree.setItemDelegate(_BookmarkRowDelegate(tree, self.remove_bookmark))
        tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        tree.doubleClicked.connect(self._open_index)
        tree.activated.connect(self._open_index)
        tree.customContextMenuRequested.connect(self._show_context_menu)
        self._add_shortcuts(tree)
        return tree

    def _add_shortcuts(self, tree: QTreeView) -> None:
        for text, shortcut, slot in (
            (self.tr("Copy path"), QKeySequence.StandardKey.Copy, self._copy_selected),
            (self.tr("Remove"), QKeySequence.StandardKey.Delete, self._remove_selected),
            (self.tr("Focus search"), QKeySequence.StandardKey.Find, self._focus_search),
        ):
            action = QAction(text, tree)
            action.setShortcut(shortcut)
            action.setShortcutContext(Qt.WidgetWithChildrenShortcut)
            action.triggered.connect(slot)
            tree.addAction(action)

    def showEvent(self, event):
        super().showEvent(event)
        icon = line_icon("bookmark", current_colors()["text_subtle"])
        self._empty_icon.setPixmap(icon.pixmap(QSize(24, 24), self.devicePixelRatioF()))
        self.refresh()

    def is_bookmarked(self, scope, path, root="", game="") -> bool:
        return self._store.is_bookmarked(scope, path, root, game)

    def toggle_bookmark(self, scope, path, root="", game="") -> None:
        bookmark = self._store.get(scope, path, root, game)
        if bookmark:
            self.remove_bookmark(bookmark.id)
        else:
            self.edit_bookmark(scope, path, root, game)

    def edit_bookmark(
        self,
        scope="",
        path="",
        root="",
        game="",
        *,
        bookmark: Bookmark | None = None,
    ) -> None:
        existing = bookmark or self._store.get(scope, path, root, game)
        context_game, context_root = self._context_provider()
        dialog = BookmarkDialog(
            self,
            title=self.tr("Edit bookmark") if existing else self.tr("Bookmark & tag"),
            path=existing.path if existing else path,
            tags=existing.tags if existing else (),
            note=existing.note if existing else "",
            completer_tags=self._store.all_tags(str(context_game or ""), str(context_root or "")),
        )
        if dialog.exec() != QDialog.Accepted:
            return
        if existing:
            self._store_call(
                self._store.update,
                existing.id,
                tags=dialog.tags(),
                note=dialog.note(),
            )
        else:
            self._store_call(
                self._store.upsert,
                scope=scope,
                path=path,
                root=root,
                game=game,
                tags=dialog.tags(),
                note=dialog.note(),
            )

    def request_open(self, bookmark_id: str) -> None:
        bookmark = self._store.get_by_id(bookmark_id)
        if bookmark is not None:
            self.open_requested.emit(bookmark)

    def remove_bookmark(self, bookmark_id: str) -> None:
        self._store_call(self._store.remove, bookmark_id)

    def touch(self, bookmark_id: str) -> None:
        self._store_call(self._store.touch, bookmark_id)

    def refresh(self) -> None:
        self._sync_tags()
        self._populate()

    def _store_call(self, operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, self.tr("Bookmark update failed"), str(exc))
            return None

    def _show_load_warnings(self) -> None:
        if self._store.load_warnings:
            QMessageBox.warning(
                self,
                self.tr("Bookmarks"),
                self.tr("Some bookmarks could not be loaded:\n{details}").format(
                    details="\n".join(self._store.load_warnings)
                ),
            )

    def _sync_tags(self) -> None:
        game, project_root = self._context_provider()
        tags = tuple(self._store.all_tags(str(game or ""), str(project_root or "")))
        if self._active_tag not in tags:
            self._active_tag = None
        if tags != self._rendered_tags:
            self._rebuild_tags(tags)
        blocked = self._tag_combo.blockSignals(True)
        self._tag_combo.setCurrentIndex(max(0, self._tag_combo.findData(self._active_tag)))
        self._tag_combo.blockSignals(blocked)

    def _rebuild_tags(self, tags: tuple[str, ...]) -> None:
        blocked = self._tag_combo.blockSignals(True)
        self._tag_combo.clear()
        self._tag_combo.addItem(self.tr("All tags"), None)
        for tag in tags:
            self._tag_combo.addItem(tag, tag)
        self._tag_combo.blockSignals(blocked)
        self._rendered_tags = tags

    def _finish_tag_edit(self):
        text = self._tag_combo.currentText().strip()
        index = self._tag_combo.findText(text, Qt.MatchFixedString)
        if index >= 0:
            self._tag_combo.setCurrentIndex(index)
        self._tag_combo.setEditText(self._tag_combo.itemText(self._tag_combo.currentIndex()))

    def _set_tag(self, tag: str | None) -> None:
        self._active_tag = tag
        self.refresh()

    def _populate(self) -> None:
        active_game, active_project = self._context_key()
        selection = self._tree.selectionModel()
        selected = {index.data(Qt.UserRole) for index in selection.selectedRows()}
        current = self._tree.currentIndex().data(Qt.UserRole)
        scroll = self._tree.verticalScrollBar().value()
        bookmarks = [
            bookmark
            for bookmark in sorted(
                self._store.matches(self._filter_edit.text(), self._active_tag),
                key=lambda item: (-item.opened_at, -item.created_at),
            )
            if self._in_context(bookmark, active_game, active_project)
        ]
        ids = [bookmark.id for bookmark in bookmarks]
        if ids == self._rendered_ids:
            for row, bookmark in enumerate(bookmarks):
                self._fill_item(self._model.item(row, 0), bookmark)
        else:
            self._model.removeRows(0, self._model.rowCount())
            for bookmark in bookmarks:
                self._model.appendRow(self._row_items(bookmark))
            self._rendered_ids = ids
            for row, bookmark in enumerate(bookmarks):
                index = self._model.index(row, 0)
                if bookmark.id in selected:
                    selection.select(index, selection.SelectionFlag.Select | selection.SelectionFlag.Rows)
                if bookmark.id == current:
                    selection.setCurrentIndex(index, selection.SelectionFlag.NoUpdate)

        self._tree.doItemsLayout()
        self._tree.verticalScrollBar().setValue(scroll)
        self._count_label.setText(self.tr("{count} saved").format(count=len(bookmarks)))
        empty = not bookmarks
        self._empty_state.setVisible(empty)
        self._tree.setVisible(not empty)
        if empty:
            self._empty_label.setText(
                self.tr("No matching bookmarks") if len(self._store) else self.tr("No bookmarks")
            )
            self._empty_hint.setVisible(not len(self._store))

    def _context_key(self) -> tuple[str, str]:
        game, project_root = self._context_provider()
        return str(game or "").upper(), normalize_root(project_root)

    @staticmethod
    def _in_context(bookmark: Bookmark, active_game: str, active_project: str) -> bool:
        if bookmark.scope in ("pak", "unpacked"):
            return bookmark.game.upper() == active_game
        return bookmark.root == active_project

    def _row_items(self, bookmark: Bookmark) -> list[QStandardItem]:
        item = QStandardItem()
        self._fill_item(item, bookmark)
        return [item]

    def _fill_item(self, item: QStandardItem, bookmark: Bookmark) -> None:
        item.setText(self._row_name(bookmark))
        item.setData(bookmark, Qt.UserRole + 1)
        item.setData(self._scope_label(bookmark), Qt.UserRole + 2)
        item.setData(bookmark.id, Qt.UserRole)
        item.setToolTip(self._tooltip(bookmark))
        item.setData(self._accessible_text(bookmark), Qt.AccessibleTextRole)
        item.setData(self._accessible_description(bookmark), Qt.AccessibleDescriptionRole)

    @staticmethod
    def _row_name(bookmark: Bookmark) -> str:
        name = bookmark.path.rstrip('/').rsplit('/', 1)[-1]
        return name + ('/' if bookmark.path.endswith('/') else '')

    def _accessible_text(self, bookmark: Bookmark) -> str:
        parts = [self._row_name(bookmark), self._scope_label(bookmark)]
        if bookmark.tags:
            parts.append(self.tr("Tags: {tags}").format(tags=", ".join(bookmark.tags)))
        return ", ".join(parts)

    def _accessible_description(self, bookmark: Bookmark) -> str:
        parts = [self.tr("Path: {path}").format(path=bookmark.path)]
        if bookmark.note:
            parts.append(self.tr("Note: {note}").format(note=bookmark.note))
        return "\n".join(parts)

    def _selected_bookmarks(self) -> list[Bookmark]:
        return [
            bookmark
            for index in self._tree.selectionModel().selectedRows()
            if (bookmark := self._bookmark_at(index)) is not None
        ]

    def _copy_selected(self) -> None:
        paths = [bookmark.path for bookmark in self._selected_bookmarks()]
        if paths:
            QApplication.clipboard().setText("\n".join(paths))

    def _remove_selected(self) -> None:
        bookmarks = self._selected_bookmarks()
        if not bookmarks:
            return
        if len(bookmarks) > 1 and QMessageBox.question(
            self,
            self.tr("Remove bookmarks"),
            self.tr("Remove {count} bookmarks?").format(count=len(bookmarks)),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        for bookmark in bookmarks:
            self.remove_bookmark(bookmark.id)

    def _focus_search(self) -> None:
        self._filter_edit.setFocus(Qt.ShortcutFocusReason)
        self._filter_edit.selectAll()

    def _bookmark_at(self, index: QModelIndex) -> Bookmark | None:
        item = self._model.item(index.row(), 0)
        bookmark_id = item.data(Qt.UserRole) if item else None
        return self._store.get_by_id(bookmark_id) if isinstance(bookmark_id, str) else None

    def _open_index(self, index: QModelIndex) -> None:
        if bookmark := self._bookmark_at(index):
            self.open_requested.emit(bookmark)

    def _show_context_menu(self, position) -> None:
        bookmark = self._bookmark_at(self._tree.indexAt(position))
        if bookmark is None:
            return
        menu = QMenu(self)
        open_action = menu.addAction(self.tr("Open"))
        edit_action = menu.addAction(self.tr("Edit tags…"))
        copy_action = menu.addAction(self.tr("Copy path"))
        remove_action = menu.addAction(self.tr("Remove"))
        chosen = menu.exec(self._tree.viewport().mapToGlobal(position))
        if chosen is open_action:
            self.open_requested.emit(bookmark)
        elif chosen is edit_action:
            self.edit_bookmark(bookmark=bookmark)
        elif chosen is copy_action:
            QApplication.clipboard().setText(bookmark.path)
        elif chosen is remove_action:
            if self._tree.selectionModel().isSelected(self._tree.indexAt(position)):
                self._remove_selected()
            else:
                self.remove_bookmark(bookmark.id)

    def _scope_label(self, bookmark: Bookmark) -> str:
        if bookmark.scope == "pak":
            return self.tr("PAK")
        return self.tr("Project") if bookmark.scope == "project" else self.tr("Unpacked")

    def _tooltip(self, bookmark: Bookmark) -> str:
        parts = [bookmark.path, self.tr("Source: {scope}").format(scope=self._scope_label(bookmark))]
        if bookmark.note:
            parts.append(bookmark.note)
        if bookmark.tags:
            parts.append(self.tr("Tags: {tags}").format(tags=", ".join(bookmark.tags)))
        return "\n".join(parts)

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("Export bookmarks"),
            "bookmarks.json",
            self.tr("JSON files (*.json)"),
        )
        if not path:
            return
        try:
            count = self._store.export_to(path)
        except OSError as exc:
            QMessageBox.critical(self, self.tr("Export failed"), str(exc))
            return
        QMessageBox.information(
            self,
            self.tr("Export"),
            self.tr("Exported {count} bookmark(s).").format(count=count),
        )

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Import bookmarks"),
            "",
            self.tr("JSON files (*.json)"),
        )
        if not path:
            return
        try:
            added = self._store.import_from(path)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, self.tr("Import failed"), str(exc))
            return
        QMessageBox.information(
            self,
            self.tr("Import"),
            self.tr("Imported {added} new bookmark(s).").format(added=added),
        )

    def _on_store_changed(self) -> None:
        if self.isVisible():
            self.refresh()
        self.changed.emit()
