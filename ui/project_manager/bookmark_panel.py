from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QModelIndex, Qt, QTimer, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
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

from .bookmark_dialog import BookmarkDialog
from .bookmarks import Bookmark, BookmarksStore, normalize_root
from .delegate import _BookmarksDelegate, _ScopeBadgeDelegate, _TagChipsDelegate

_ContextProvider = Callable[[], tuple[str | None, str | None]]


class BookmarksPanel(QWidget):
    """Bookmark browsing, editing, filtering, and persistence UI."""

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
        self._tag_buttons: dict[str | None, QToolButton] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(self._build_toolbar())

        self._tags_layout = QHBoxLayout()
        self._tags_layout.setContentsMargins(0, 0, 0, 0)
        self._tags_layout.setSpacing(4)
        layout.addLayout(self._tags_layout)

        self._model = QStandardItemModel(self)
        self._model.setHorizontalHeaderLabels(
            [self.tr("Path"), self.tr("Tags"), self.tr("Scope")]
        )
        self._model.setHeaderData(1, Qt.Horizontal, Qt.AlignRight, Qt.TextAlignmentRole)
        self._model.setHeaderData(2, Qt.Horizontal, Qt.AlignRight, Qt.TextAlignmentRole)
        self._tree = self._build_tree()
        layout.addWidget(self._tree)

        self._empty_label = QLabel(self.tr("No bookmarks"))
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._empty_label.hide()
        layout.addWidget(self._empty_label)

        self._store.changed.connect(self._on_store_changed)
        if self._store.load_warnings:
            QTimer.singleShot(0, self._show_load_warnings)

    def _build_toolbar(self) -> QHBoxLayout:
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel(self.tr("Filter:")))

        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText(self.tr("Search bookmarks"))
        toolbar.addWidget(self._filter_edit, 1)

        self._filter_timer = QTimer(self)
        self._filter_timer.setInterval(120)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.timeout.connect(self._populate)
        self._filter_edit.textChanged.connect(lambda _text: self._filter_timer.start())

        for text, tooltip, callback in (
            (self.tr("Import…"), self.tr("Import bookmarks from a JSON file"), self._import),
            (self.tr("Export…"), self.tr("Export bookmarks to a JSON file"), self._export),
        ):
            button = QToolButton(text=text)
            button.setToolTip(tooltip)
            button.clicked.connect(callback)
            toolbar.addWidget(button)
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
        tree.header().setStretchLastSection(False)
        tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        tree.setItemDelegateForColumn(
            0,
            _BookmarksDelegate(tree, self.request_open, self.remove_bookmark),
        )
        tree.setItemDelegateForColumn(1, _TagChipsDelegate(tree))
        tree.setItemDelegateForColumn(2, _ScopeBadgeDelegate(tree))
        tree.doubleClicked.connect(self._open_index)
        tree.customContextMenuRequested.connect(self._show_context_menu)
        return tree

    def showEvent(self, event):
        super().showEvent(event)
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
        dialog = BookmarkDialog(
            self,
            title=self.tr("Edit bookmark") if existing else self.tr("Bookmark & tag"),
            path=existing.path if existing else path,
            tags=existing.tags if existing else (),
            note=existing.note if existing else "",
            completer_tags=self._store.all_tags(),
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
        tags = tuple(self._store.all_tags())
        if self._active_tag not in tags:
            self._active_tag = None
        if tags != self._rendered_tags:
            self._rebuild_tag_buttons(tags)
        for tag, button in self._tag_buttons.items():
            button.setChecked(tag == self._active_tag)

    def _rebuild_tag_buttons(self, tags: tuple[str, ...]) -> None:
        while self._tags_layout.count():
            item = self._tags_layout.takeAt(0)
            if widget := item.widget():
                widget.deleteLater()
        self._tag_buttons.clear()
        for tag in (None, *tags):
            button = QToolButton(
                text=self.tr("All") if tag is None else tag,
                checkable=True,
            )
            button.clicked.connect(lambda _=False, value=tag: self._set_tag(value))
            self._tags_layout.addWidget(button)
            self._tag_buttons[tag] = button
        self._tags_layout.addStretch(1)
        self._rendered_tags = tags

    def _set_tag(self, tag: str | None) -> None:
        self._active_tag = tag
        self.refresh()

    def _populate(self) -> None:
        game, project_root = self._context_provider()
        active_game = str(game or "").upper()
        active_project = normalize_root(project_root)
        self._model.removeRows(0, self._model.rowCount())
        bookmarks = self._store.matches(self._filter_edit.text(), self._active_tag)
        for bookmark in sorted(bookmarks, key=lambda item: (-item.opened_at, -item.created_at)):
            if bookmark.scope in ("pak", "unpacked"):
                if bookmark.game.upper() != active_game:
                    continue
            elif bookmark.root != active_project:
                continue
            self._model.appendRow(self._row_items(bookmark))

        empty = self._model.rowCount() == 0
        self._empty_label.setVisible(empty)
        self._tree.setVisible(not empty)
        if empty:
            self._empty_label.setText(
                self.tr("No matching bookmarks") if len(self._store) else self.tr("No bookmarks")
            )

    def _row_items(self, bookmark: Bookmark) -> list[QStandardItem]:
        path = QStandardItem(bookmark.path)
        path.setData(bookmark.id, Qt.UserRole)
        path.setToolTip(self._tooltip(bookmark))
        tags = QStandardItem(", ".join(bookmark.tags))
        tags.setData(bookmark.tags, Qt.UserRole)
        scope = QStandardItem(self._scope_label(bookmark))
        scope.setData(bookmark.scope, Qt.UserRole)
        return [path, tags, scope]

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
            self.remove_bookmark(bookmark.id)

    def _scope_label(self, bookmark: Bookmark) -> str:
        if bookmark.scope == "pak":
            return self.tr("PAK")
        return self.tr("Project") if bookmark.scope == "project" else self.tr("Unpacked")

    def _tooltip(self, bookmark: Bookmark) -> str:
        parts = [bookmark.path]
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
