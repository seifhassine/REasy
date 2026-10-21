from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app_config import CURRENT_VERSION
lazy from ui.editor_widgets import styled_label
lazy from ui.icons import line_icon
lazy from ui.styles import current_colors
lazy from utils.app_paths import resource_path

RECENT_FILES_SHOWN = 8


class _ActionTile(QPushButton):
    def __init__(self, icon_name: str, title: str, hint: str, parent=None):
        super().__init__(parent)
        self.setObjectName("homeTile")
        self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName(title)
        self.setMinimumSize(196, 92)
        self.icon_name = icon_name
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(3)
        self.icon_label = QLabel(self)
        self.icon_label.setObjectName("homeTileIcon")
        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("homeTileTitle")
        self.hint_label = QLabel(hint, self)
        self.hint_label.setObjectName("homeTileHint")
        layout.addWidget(self.icon_label)
        layout.addSpacing(4)
        layout.addWidget(self.title_label)
        layout.addWidget(self.hint_label)
        for label in (self.icon_label, self.title_label, self.hint_label):
            label.setAttribute(Qt.WA_TransparentForMouseEvents)

    def set_icon_color(self, color: str):
        self.icon_label.setPixmap(line_icon(self.icon_name, color).pixmap(22, 22))

    def set_hint(self, hint: str):
        self.hint_label.setText(hint)


class HomePageWidget(QWidget):
    def __init__(self, on_open_file, on_new_project, on_open_project, on_reopen_last, on_reopen, parent=None):
        super().__init__(parent)
        self.setObjectName("homePage")
        self._on_reopen = on_reopen
        self._open_hint = self.tr("Browse for a game file")

        root = QVBoxLayout(self)
        root.setContentsMargins(32, 32, 32, 32)
        root.addStretch(2)

        hero = QWidget(self)
        hero.setObjectName("homeHero")
        hero.setMaximumWidth(680)
        hero.setMinimumWidth(620)
        layout = QVBoxLayout(hero)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QHBoxLayout()
        header.setSpacing(14)
        logo = QLabel(hero)
        logo.setObjectName("homeLogo")
        logo.setFixedSize(44, 44)
        logo.setPixmap(
            QIcon(str(resource_path("resources/icons/reasy_editor_logo.ico"))).pixmap(44, 44)
        )
        header.addWidget(logo, 0, Qt.AlignVCenter)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title = QLabel(self.tr("REasy Editor"), hero)
        title.setObjectName("homeTitle")
        title_row.addWidget(title, 0, Qt.AlignBottom)
        version = QLabel(f"v{CURRENT_VERSION}", hero)
        version.setObjectName("homeVersion")
        title_row.addWidget(version, 0, Qt.AlignBottom)
        title_row.addStretch()
        titles.addLayout(title_row)
        subtitle = QLabel(
            self.tr("Open files. Change things. Try not to anger the engine."),
            hero,
        )
        subtitle.setObjectName("homeSubtitle")
        titles.addWidget(subtitle)
        header.addLayout(titles, 1)
        layout.addLayout(header)
        layout.addSpacing(28)

        layout.addWidget(styled_label(self.tr("START"), hero, heading="caption"))
        layout.addSpacing(8)
        tiles = QHBoxLayout()
        tiles.setSpacing(10)
        self.open_button = _ActionTile("folder_open", self.tr("Open File"), self._open_hint, hero)
        self.new_project_button = _ActionTile(
            "new_folder", self.tr("New Project"), self.tr("Start a new mod"), hero
        )
        self.library_button = _ActionTile(
            "box", self.tr("Project Library"), self.tr("Open a saved project"), hero
        )
        for tile, callback in (
            (self.open_button, on_open_file),
            (self.new_project_button, on_new_project),
            (self.library_button, on_open_project),
        ):
            tile.clicked.connect(callback)
            tiles.addWidget(tile, 1)
        layout.addLayout(tiles)
        layout.addSpacing(28)

        recent_header = QHBoxLayout()
        recent_header.setContentsMargins(0, 0, 0, 0)
        recent_header.addWidget(styled_label(self.tr("RECENTLY CLOSED"), hero, heading="caption"))
        recent_header.addStretch()
        self.reopen_button = QPushButton(self.tr("Reopen last closed  →"), hero)
        self.reopen_button.setProperty("variant", "link")
        self.reopen_button.setCursor(Qt.PointingHandCursor)
        self.reopen_button.clicked.connect(on_reopen_last)
        recent_header.addWidget(self.reopen_button)
        layout.addLayout(recent_header)
        layout.addSpacing(6)

        self.recent_list = QTreeWidget(hero)
        self.recent_list.setObjectName("homeRecentList")
        self.recent_list.setColumnCount(2)
        self.recent_list.setHeaderHidden(True)
        self.recent_list.setRootIsDecorated(False)
        self.recent_list.setUniformRowHeights(True)
        self.recent_list.setSelectionMode(QAbstractItemView.NoSelection)
        self.recent_list.setFocusPolicy(Qt.NoFocus)
        self.recent_list.setTextElideMode(Qt.ElideMiddle)
        self.recent_list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.recent_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.recent_list.setIconSize(QSize(16, 16))
        self.recent_list.setCursor(Qt.PointingHandCursor)
        self.recent_list.itemClicked.connect(self._open_recent)
        layout.addWidget(self.recent_list)

        self.recent_label = QLabel(self.tr("No recently closed files yet."), hero)
        self.recent_label.setObjectName("recentLabel")
        self.recent_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.recent_label)
        layout.addSpacing(24)

        tip = QLabel(self.tr("Tip: drag and drop a file anywhere in the window."), hero)
        tip.setObjectName("homeTip")
        tip.setAlignment(Qt.AlignCenter)
        layout.addWidget(tip)

        root.addWidget(hero, alignment=Qt.AlignHCenter)
        root.addStretch(3)
        self._subtle_color = current_colors()["text_subtle"]
        self.set_recent_files([])

    def _open_recent(self, item, _column=0):
        entry = item.data(0, Qt.UserRole)
        if entry:
            self._on_reopen(entry)

    def set_theme(self, colors: dict):
        self._subtle_color = colors["text_subtle"]
        for tile in (self.open_button, self.new_project_button, self.library_button):
            tile.set_icon_color(colors["accent_fg"])
        for row in range(self.recent_list.topLevelItemCount()):
            self.recent_list.topLevelItem(row).setForeground(1, QColor(self._subtle_color))

        self.setStyleSheet(f"""
            QWidget#homePage {{ background: {colors['editor_bg']}; }}
            QWidget#homeHero {{ background: transparent; }}
            QLabel#homeLogo {{ background: transparent; }}
            QLabel#homeTitle {{ color: {colors['text_bright']}; font-size: 22px; font-weight: 600; }}
            QLabel#homeVersion {{
                color: {colors['text_muted']}; background: {colors['surface']};
                border: 1px solid {colors['border_subtle']}; border-radius: 3px;
                padding: 1px 6px; margin-bottom: 4px; font-size: 11px;
            }}
            QLabel#homeSubtitle {{ color: {colors['text_muted']}; }}
            QPushButton#homeTile {{
                background: {colors['surface']}; border: 1px solid {colors['border_subtle']};
                border-radius: 4px; padding: 0px; text-align: left; min-width: 180px; min-height: 90px;
            }}
            QPushButton#homeTile:hover {{
                background: {colors['surface_hover']}; border-color: {colors['accent_border']};
            }}
            QPushButton#homeTile:pressed {{ background: {colors['button_pressed']}; }}
            QLabel#homeTileTitle {{ color: {colors['text_bright']}; font-weight: 600; }}
            QLabel#homeTileHint {{ color: {colors['text_subtle']}; }}
            QTreeWidget#homeRecentList {{
                background: {colors['view_bg']}; border: 1px solid {colors['border_subtle']};
                border-radius: 4px; padding: 4px;
            }}
            QTreeWidget#homeRecentList::item {{ padding: 4px 6px; border: none; }}
            QTreeWidget#homeRecentList::item:hover {{ background: {colors['surface_hover']}; }}
            QLabel#recentLabel {{
                color: {colors['text_subtle']}; background: {colors['view_bg']};
                border: 1px dashed {colors['border']}; border-radius: 4px; padding: 18px;
            }}
            QLabel#homeTip {{ color: {colors['text_subtle']}; }}
        """)

    def set_shortcuts(self, open_file: str = "", reopen_last: str = ""):
        self.open_button.set_hint(f"{self._open_hint}  ·  {open_file}" if open_file else self._open_hint)
        self.reopen_button.setToolTip(reopen_last)

    def set_recent_files(self, entries):
        """Show (name, location, history entry) rows, newest first."""
        self.recent_list.clear()
        file_icon = line_icon("file")
        for name, location, entry in entries[:RECENT_FILES_SHOWN]:
            item = QTreeWidgetItem([name, location])
            item.setIcon(0, file_icon)
            item.setToolTip(0, location)
            item.setToolTip(1, location)
            item.setForeground(1, QColor(self._subtle_color))
            item.setData(0, Qt.UserRole, entry)
            self.recent_list.addTopLevelItem(item)
        has_entries = bool(entries)
        self.recent_list.setVisible(has_entries)
        self.recent_label.setVisible(not has_entries)
        self.reopen_button.setEnabled(has_entries)
        if has_entries:
            self.recent_list.resizeColumnToContents(0)
            self.recent_list.setColumnWidth(0, min(self.recent_list.columnWidth(0) + 12, 300))
            row_height = self.recent_list.sizeHintForRow(0) or 26
            shown = min(len(entries), RECENT_FILES_SHOWN)
            self.recent_list.setFixedHeight(row_height * shown + 10)


class HomePageStack:
    def __init__(self, notebook: QWidget, homepage: HomePageWidget):
        self.notebook = notebook
        self.homepage = homepage
        self.widget = QStackedWidget()
        self.widget.addWidget(self.homepage)
        self.widget.addWidget(self.notebook)

    def refresh(self, show_notebook: bool, recent_entries=()):
        self.widget.setCurrentWidget(self.notebook if show_notebook else self.homepage)
        self.homepage.set_recent_files(list(recent_entries))
