from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from PySide6.QtCore import QEvent, QT_TRANSLATE_NOOP, Qt, QSize
from PySide6.QtGui import QBrush, QColor, QFont, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

lazy from ui.editor_widgets import styled_label, tool_button
lazy from ui.styles import current_colors, set_style_role

lazy from .project_config import load_project_config, project_config_path


NO_PREVIEW_TEXT = QT_TRANSLATE_NOOP("ProjectPickerDialog", "No preview")


@dataclass(frozen=True)
class ProjectEntry:
    path: Path
    game: str
    name: str
    description: str
    author: str
    version: str
    source: str
    screenshot: str
    modified: float

    @property
    def modified_label(self) -> str:
        return datetime.fromtimestamp(self.modified).strftime("%Y-%m-%d")


def discover_projects(projects_root: Path, games: Sequence[str]) -> list[ProjectEntry]:
    projects_root.mkdir(parents=True, exist_ok=True)
    known_games = list(dict.fromkeys(games))
    for folder in sorted(projects_root.iterdir()):
        if folder.is_dir() and folder.name not in known_games:
            known_games.append(folder.name)

    entries: list[ProjectEntry] = []
    for game in known_games:
        game_dir = projects_root / game
        if not game_dir.is_dir():
            continue

        for project_dir in sorted(game_dir.iterdir(), key=lambda p: p.name.lower()):
            if not project_dir.is_dir() or project_dir.name.startswith("."):
                continue
            entries.append(_entry_from_dir(project_dir, game))

    return sorted(entries, key=lambda entry: (entry.game.upper(), entry.name.lower()))


def _entry_from_dir(project_dir: Path, fallback_game: str) -> ProjectEntry:
    cfg = load_project_config(project_dir)
    cfg_game = cfg.get("game")
    game = cfg_game if isinstance(cfg_game, str) and cfg_game.strip() else fallback_game
    source = "Folder"
    if cfg.get("pak_game_dir"):
        source = "PAKs"
    elif cfg.get("unpacked_dir"):
        source = "Unpacked"

    cfg_path = project_config_path(project_dir)
    cfg_mtime = cfg_path.stat().st_mtime if cfg_path.exists() else 0
    modified = max(project_dir.stat().st_mtime, cfg_mtime)
    return ProjectEntry(
        path=project_dir,
        game=game,
        name=str(cfg.get("name") or project_dir.name),
        description=str(cfg.get("description") or ""),
        author=str(cfg.get("author") or ""),
        version=str(cfg.get("version") or ""),
        source=source,
        screenshot=str(cfg.get("screenshot") or ""),
        modified=modified,
    )

class ProjectPickerDialog(QDialog):
    def __init__(
        self,
        projects_root: Path,
        games: Sequence[str],
        *,
        current_project: str | None = None,
        on_project_delete: Callable[[Path], bool] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("ProjectPickerDialog")
        self.setWindowTitle(self.tr("Project Library"))
        self.resize(860, 520)

        self.projects_root = Path(projects_root)
        self.games = list(games)
        self.entries: list[ProjectEntry] = []
        self._entry_by_path: dict[str, ProjectEntry] = {}
        self._selected: ProjectEntry | None = None
        self._wants_new_project = False
        self._preferred_path = Path(current_project).resolve() if current_project else None
        self._on_project_delete = on_project_delete

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.toolbar = QFrame(self)
        self.toolbar.setObjectName("projectPickerToolbar")
        set_style_role(self.toolbar, surface="toolbar")
        filters = QHBoxLayout(self.toolbar)
        filters.setContentsMargins(8, 6, 8, 6)
        filters.setSpacing(6)
        root.addWidget(self.toolbar)

        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText(self.tr("Search projects"))
        self.search_edit.textChanged.connect(self._populate)
        filters.addWidget(self.search_edit, 1)

        self.game_filter = QComboBox(self)
        self.game_filter.addItem(self.tr("All games"), "")
        for game in self.games:
            self.game_filter.addItem(game, game)
        self.game_filter.currentIndexChanged.connect(self._populate)
        filters.addWidget(self.game_filter)
        self.count_label = styled_label("", self, tone="muted")
        filters.addWidget(self.count_label)

        refresh_btn = tool_button("refresh", self.tr("Refresh"), self.refresh, parent=self)
        filters.addWidget(refresh_btn)

        body = QVBoxLayout()
        body.setContentsMargins(8, 8, 8, 8)
        body.setSpacing(8)
        root.addLayout(body, 1)
        splitter = QSplitter(Qt.Horizontal, self)
        body.addWidget(splitter, 1)

        self.tree = QTreeWidget(self)
        self.tree.setObjectName("projectTree")
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels([
            self.tr("Project"),
            self.tr("Source"),
            self.tr("Modified"),
        ])
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAnimated(True)
        self.tree.setIndentation(12)
        self.tree.viewport().installEventFilter(self)
        self.tree.itemSelectionChanged.connect(self._sync_details)
        self.tree.itemDoubleClicked.connect(self._open_from_item)
        self.tree.header().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        splitter.addWidget(self.tree)

        detail = QFrame(self)
        detail.setObjectName("detailPanel")
        set_style_role(detail, surface="card")
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(12, 12, 12, 12)
        detail_layout.setSpacing(6)

        self.preview = styled_label(parent=self, surface="recessed", tone="subtle")
        self.preview.setObjectName("previewLabel")
        self.preview.setFixedSize(240, 135)
        self.preview.setAlignment(Qt.AlignCenter)
        detail_layout.addWidget(self.preview, alignment=Qt.AlignHCenter)
        detail_layout.addSpacing(4)

        self.name_label = styled_label(self.tr("No project selected"), self, heading="title")
        self.name_label.setWordWrap(True)
        detail_layout.addWidget(self.name_label)

        self.meta_label = styled_label("", self, tone="muted")
        self.meta_label.setObjectName("metaLabel")
        self.meta_label.setWordWrap(True)
        detail_layout.addWidget(self.meta_label)

        self.description_label = QLabel("", self)
        self.description_label.setObjectName("descriptionLabel")
        self.description_label.setWordWrap(True)
        detail_layout.addWidget(self.description_label)

        self.path_label = styled_label("", self, tone="subtle")
        self.path_label.setObjectName("pathLabel")
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        detail_layout.addWidget(self.path_label)
        detail_layout.addStretch(1)
        splitter.addWidget(detail)
        splitter.setChildrenCollapsible(False)
        splitter.setSizes([560, 300])

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel, self)
        self.new_btn = buttons.addButton(self.tr("New Project..."), QDialogButtonBox.ActionRole)
        self.delete_btn = buttons.addButton(self.tr("Delete"), QDialogButtonBox.ActionRole)
        self.open_btn = buttons.addButton(self.tr("Open"), QDialogButtonBox.AcceptRole)
        set_style_role(self.delete_btn, variant="danger")
        set_style_role(self.open_btn, variant="primary")
        self.delete_btn.setEnabled(False)
        self.open_btn.setEnabled(False)
        self.new_btn.clicked.connect(self._new_project)
        self.delete_btn.clicked.connect(self._delete_selected)
        self.open_btn.clicked.connect(self._open_selected)
        buttons.rejected.connect(self.reject)
        body.addWidget(buttons)

        self.refresh()

    def selected_project(self) -> ProjectEntry | None:
        return self._selected

    def wants_new_project(self) -> bool:
        return self._wants_new_project

    def eventFilter(self, watched, event):
        if (
            watched is self.tree.viewport()
            and event.type() == QEvent.MouseButtonPress
            and event.button() == Qt.LeftButton
        ):
            item = self.tree.itemAt(event.pos())
            if item and item.childCount() > 0 and not item.data(0, Qt.UserRole):
                item.setExpanded(not item.isExpanded())
                return True
        return super().eventFilter(watched, event)

    def refresh(self):
        self.entries = discover_projects(self.projects_root, self.games)
        self._populate()

    def _source_label(self, source: str) -> str:
        return {
            "Folder": self.tr("Folder"),
            "PAKs": self.tr("PAKs"),
            "Unpacked": self.tr("Unpacked"),
        }.get(source, source)

    def _filtered_entries(self, needle: str, game_filter):
        for entry in self.entries:
            if game_filter and entry.game != game_filter:
                continue
            haystack = " ".join(
                (entry.name, entry.game, entry.description, entry.author, str(entry.path))
            ).lower()
            if not needle or needle in haystack:
                yield entry

    def _populate(self):
        self.tree.clear()
        self._entry_by_path.clear()
        needle = self.search_edit.text().strip().lower()
        game_filter = self.game_filter.currentData()
        groups: dict[str, QTreeWidgetItem] = {}
        preferred_item = None
        shown_count = 0

        for entry in self._filtered_entries(needle, game_filter):
            group = groups.get(entry.game)
            if group is None:
                group = QTreeWidgetItem([entry.game, "", ""])
                group.setSizeHint(0, QSize(0, 26))
                group.setFirstColumnSpanned(True)
                group.setFlags(group.flags() & ~Qt.ItemIsSelectable)
                group.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
                font = group.font(0)
                font.setWeight(QFont.DemiBold)
                group.setFont(0, font)
                self.tree.addTopLevelItem(group)
                groups[entry.game] = group

            item = QTreeWidgetItem([
                entry.name, self._source_label(entry.source), entry.modified_label
            ])
            item.setSizeHint(0, QSize(0, 26))
            item.setData(0, Qt.UserRole, str(entry.path))
            item.setToolTip(0, str(entry.path))
            item.setChildIndicatorPolicy(QTreeWidgetItem.DontShowIndicator)
            group.addChild(item)
            self._entry_by_path[str(entry.path)] = entry
            shown_count += 1

            if self._preferred_path and entry.path.resolve() == self._preferred_path:
                preferred_item = item

        for game, group in groups.items():
            group.setText(0, f"{game} ({group.childCount()})")
        self.count_label.setText(self._project_count_label(shown_count))

        if not groups:
            empty = QTreeWidgetItem([self.tr("No projects found"), "", ""])
            empty.setSizeHint(0, QSize(0, 26))
            empty.setForeground(0, QBrush(QColor(current_colors()["text_muted"])))
            empty.setFirstColumnSpanned(True)
            empty.setFlags(empty.flags() & ~Qt.ItemIsSelectable)
            empty.setChildIndicatorPolicy(QTreeWidgetItem.DontShowIndicator)
            self.tree.addTopLevelItem(empty)

        if needle or game_filter:
            self.tree.expandAll()
        elif preferred_item:
            preferred_item.parent().setExpanded(True)
            self.tree.setCurrentItem(preferred_item)
            return

        self.tree.clearSelection()
        self._selected = None
        self._sync_details()

    def _sync_details(self):
        items = self.tree.selectedItems()
        key = items[0].data(0, Qt.UserRole) if items else None
        entry = self._entry_by_path.get(key) if isinstance(key, str) else None
        self._selected = entry
        self.open_btn.setEnabled(entry is not None)
        self.delete_btn.setEnabled(entry is not None)

        if entry is None:
            self.preview.clear()
            self.name_label.setText(self.tr("No project selected"))
            self.meta_label.setText("")
            self.description_label.setText("")
            self.description_label.setVisible(False)
            self.path_label.setText("")
            return

        self.name_label.setText(entry.name)
        details = [entry.game, self._source_label(entry.source)]
        if entry.version:
            details.append(entry.version)
        if entry.author:
            details.append(self.tr("by {}").format(entry.author))
        self.meta_label.setText(" - ".join(details))
        self.description_label.setText(entry.description)
        self.description_label.setVisible(bool(entry.description))
        self.path_label.setText(str(entry.path))
        self._load_preview(entry)

    def _load_preview(self, entry: ProjectEntry):
        self.preview.clear()
        screenshot = entry.screenshot.strip()
        if not screenshot:
            self.preview.setText(self.tr(NO_PREVIEW_TEXT))
            return

        path = Path(screenshot)
        if not path.is_absolute():
            path = entry.path / path
        if not path.is_file():
            self.preview.setText(self.tr(NO_PREVIEW_TEXT))
            return

        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self.preview.setText(self.tr(NO_PREVIEW_TEXT))
            return
        self.preview.setPixmap(pixmap.scaled(self.preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _open_from_item(self, item: QTreeWidgetItem, _column: int):
        key = item.data(0, Qt.UserRole)
        if isinstance(key, str) and self._entry_by_path.get(key):
            self._open_selected()

    def _open_selected(self):
        if self._selected:
            self.accept()

    def _new_project(self):
        self._wants_new_project = True
        self.accept()

    def _delete_selected(self):
        entry = self._selected
        if entry is None:
            return

        root = self.projects_root.resolve()
        path = entry.path.resolve()
        if path == root or not path.is_relative_to(root):
            QMessageBox.warning(self, self.tr("Delete project"), self.tr("Invalid project path."))
            return

        answer = QMessageBox.question(
            self,
            self.tr("Delete project?"),
            self.tr("Delete project \"{}\"?\n\nThis will permanently remove:\n{}").format(entry.name, path),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        if self._on_project_delete:
            if not self._on_project_delete(path):
                return
        else:
            try:
                shutil.rmtree(path)
            except Exception as exc:
                QMessageBox.critical(self, self.tr("Delete failed"), str(exc))
                return

        if self._preferred_path and path == self._preferred_path:
            self._preferred_path = None
        self.refresh()

    def _project_count_label(self, count: int) -> str:
        return self.tr("{} project").format(count) if count == 1 else self.tr("{} projects").format(count)
