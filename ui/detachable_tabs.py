import sys
from dataclasses import dataclass

if sys.platform == "win32":
    lazy from ctypes import wintypes
else:
    wintypes = None

from PySide6.QtCore import QEvent, QLine, QPoint, QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QFont,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPen,
    QRegion,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QDockWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QStackedWidget,
    QStyle,
    QStyleOptionTab,
    QStylePainter,
    QTabBar,
    QTabWidget,
    QToolButton,
    QWidget,
)

lazy from ui.editor_groups import EditorGroupHost
lazy from ui.qt_timers import call_later
lazy from ui.styles import current_colors
lazy from ui.tab_preview import TabPreview

_WM_MOVING, _WM_EXITSIZEMOVE = 0x0216, 0x0232


@dataclass(slots=True)
class _TabState:
    title: str
    icon: QIcon
    tool_tip: str
    whats_this: str
    accessible_name: str
    data: object
    text_color: QColor
    enabled: bool
    visible: bool


def _floating_size(page: QWidget) -> QSize:
    return page.size().expandedTo(QSize(720, 480)).boundedTo(QSize(1600, 1200))


class _TabCloseButton(QAbstractButton):
    def __init__(self, notebook: "CustomNotebook", page: QWidget):
        super().__init__(notebook.tabBar())
        self.notebook, self.page = notebook, page
        self.setObjectName("documentTabClose")
        self.setAccessibleName(self.tr("Close tab"))
        self.setToolTip(self.tr("Close"))
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(18, 18)
        self.clicked.connect(self._close)

    def _close(self):
        index = self.notebook.indexOf(self.page)
        if index >= 0:
            self.notebook.tabCloseRequested.emit(index)

    def _tab_hovered(self, bar: QTabBar, index: int) -> bool:
        return bar.tabRect(index).contains(bar.mapFromGlobal(QCursor.pos()))

    def _text_offset(self, bar: QTabBar, index: int) -> float:
        """Vertical offset that lines the cross up with the tab text; the accent border sits above it."""
        option = QStyleOptionTab()
        bar.initStyleOption(option, index)
        text = bar.style().subElementRect(QStyle.SE_TabBarTabText, option, bar)
        return self.mapFromParent(text.center()).y() - self.rect().center().y()

    def paintEvent(self, _event):
        bar = self.parentWidget()
        index = self.notebook.indexOf(self.page)
        hovered = self.underMouse()
        active = self.notebook.currentWidget() is self.page
        if index < 0 or not (hovered or active or self._tab_hovered(bar, index)):
            return
        colors = current_colors()
        offset = max(-2.0, min(2.0, self._text_offset(bar, index)))
        area = QRectF(self.rect()).adjusted(2, 2, -2, -2).translated(0, offset)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        if hovered or self.isDown():
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(colors["surface_active" if self.isDown() else "surface_hover"]))
            painter.drawRoundedRect(area, 3, 3)
        pen = QPen(QColor(colors["text_bright" if hovered else "text" if active else "text_muted"]))
        pen.setWidthF(1.4)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        center = area.center()
        painter.drawLine(center + QPointF(-3, -3), center + QPointF(3, 3))
        painter.drawLine(center + QPointF(3, -3), center + QPointF(-3, 3))


class FloatingTabWindow(QMainWindow):

    def __init__(self, root: "EditorGroupHost"):
        super().__init__(root.window(), Qt.Window)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._discarding = False
        self._title_drag_start = None
        main_window = root.app_window
        self.host = EditorGroupHost(CustomNotebook(), main_window, self, root=root)
        self.setCentralWidget(self.host)
        self.host.activePageChanged.connect(self.sync_title)
        if set_icon := getattr(main_window, "_set_app_icon_callback", None):
            set_icon(self)
        self.menuBar().addMenu(self.tr("Tab")).addAction(self.tr("Reattach"), self.close)
        if isinstance(main_window, QMainWindow):
            self._setup_main_window_actions(main_window)
        root._windows.append(self)

    def _setup_main_window_actions(self, main_window):
        self._find_action = QAction(self.tr("Find"), self)
        shortcut = getattr(main_window, "settings", {}).get(
            "keyboard_shortcuts", {}
        ).get("find_search", "Ctrl+F")
        self._find_action.setObjectName("find_search_detached")
        self._find_action.setShortcut(QKeySequence(shortcut))
        self._find_action.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        self._find_action.triggered.connect(self.open_find_dialog)
        self.addAction(self._find_action)

        for top_action in main_window.menuBar().actions():
            if source := top_action.menu():
                self._copy_menu(source, self.menuBar().addMenu(source.title()))
        for action in main_window.findChildren(QAction):
            if action.objectName() != "find_search" and (
                not action.shortcut().isEmpty()
                or action.parent() == main_window.menuBar()
            ):
                self.addAction(action)
                action.setShortcutContext(Qt.ApplicationShortcut)

    def _copy_menu(self, source, destination):
        destination.setProperty("iconMenu", source.property("iconMenu"))
        destination.setStyleSheet(source.styleSheet())
        for action in source.actions():
            submenu = action.menu()
            if action.objectName() == "find_search":
                destination.addAction(self._find_action)
            elif action.isSeparator():
                destination.addSeparator()
            elif submenu:
                self._copy_menu(submenu, destination.addMenu(submenu.icon(), submenu.title()))
            else:
                destination.addAction(action)

    def pages(self) -> list[QWidget]:
        return self.host.pages()

    def sync_title(self, _page=None):
        notebook = self.host.active_notebook()
        self.setWindowTitle(notebook.tabText(notebook.currentIndex()))

    def open_find_dialog(self):
        tab = getattr(self.host.app_window, "tabs", {}).get(self.host.active_page())
        if tab is not None:
            tab.open_find_dialog()
        else:
            QMessageBox.warning(
                self, self.tr("Warning"),
                self.tr("Cannot open find dialog for this tab"),
            )

    def discard(self):
        self._discarding = True
        self.close()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.ActivationChange and self.isActiveWindow():
            self.host.root.raise_window(self)

    def nativeEvent(self, event_type, message):
        if wintypes is not None:
            kind = wintypes.MSG.from_address(int(message)).message
            if kind == _WM_MOVING:
                self.title_dragged(QCursor.pos())
            elif kind == _WM_EXITSIZEMOVE and self._title_drag_start is not None:
                self.title_released(QCursor.pos())
        return super().nativeEvent(event_type, message)

    def title_dragged(self, pos: QPoint):
        root = self.host.root
        if self._title_drag_start is None:
            self._title_drag_start = self.pos()
            root.set_dragging(True)
        root.update_drag(pos, self.pages(), exclude=self)

    def title_released(self, pos: QPoint):
        root, moved = self.host.root, self.pos() != self._title_drag_start
        target = root.update_drag(pos, self.pages(), exclude=self)
        self._title_drag_start = None
        root.set_dragging(False)
        if moved and target is not None:
            call_later(root, 0, lambda pages=self.pages(): root.dock(pages, target))

    def closeEvent(self, event):
        root = self.host.root
        for dock in self.findChildren(QDockWidget):
            dock.close()
        if not self._discarding:
            root.redock(self)
        if self in root._windows:
            root._windows.remove(self)
        super().closeEvent(event)


class DetachTabBar(QTabBar):
    _UNDOCK_MARGIN = 16

    def __init__(self, notebook: "CustomNotebook"):
        super().__init__(notebook)
        self.notebook = notebook
        self._grab = None
        self.setObjectName("documentTabBar")
        self.setDocumentMode(True)
        self.setDrawBase(False)
        self.setMovable(True)
        self.setExpanding(False)
        self.setElideMode(Qt.ElideMiddle)
        self.setUsesScrollButtons(True)
        self.setSelectionBehaviorOnRemove(QTabBar.SelectPreviousTab)
        self.setToolTip(
            self.tr("Drag to reorder or move between editor groups. Drag away to detach.")
        )
        TabPreview(self)
        # EditorGroupHost sets inactiveGroup on the tab bars of groups that are not active.
        colors = current_colors()
        self.setStyleSheet(
            f'QTabBar#documentTabBar[inactiveGroup="true"]::tab:selected {{'
            f' border-top-color: {colors["border_strong"]}; color: {colors["text"]}; }}'
        )
        self._sync_visibility()

    def tabInserted(self, index):
        super().tabInserted(index)
        self._sync_visibility()

    def tabRemoved(self, index):
        super().tabRemoved(index)
        self._sync_visibility()

    def set_tab_shown(self, index: int, shown: bool):
        self.setTabVisible(index, shown)
        self._sync_visibility()

    def _sync_visibility(self):
        # Without a visible tab the bar is 0 x 0, which Windows shows at 160 x 160 once the window is native.
        self.setVisible(any(self.isTabVisible(index) for index in range(self.count())))

    def paintEvent(self, event):
        current = self.currentIndex()
        if current < 0 or self.isTabVisible(current):
            super().paintEvent(event)
            return
        # QTabBar paints the current tab even when it is hidden, so paint the visible ones here.
        painter = QStylePainter(self)
        clip = QRegion(self.rect())
        for button in self.findChildren(QToolButton, options=Qt.FindDirectChildrenOnly):
            if button.isVisible():
                clip -= QRegion(button.geometry())
        painter.setClipRegion(clip)
        for index in range(self.count()):
            if self.isTabVisible(index):
                option = QStyleOptionTab()
                self.initStyleOption(option, index)
                painter.drawControl(QStyle.CE_TabBarTab, option)

    def mousePressEvent(self, event):
        self._grab = None
        index = self.tabAt(event.position().toPoint())
        if event.button() == Qt.LeftButton and index >= 0:
            self._grab = self.notebook.widget(index), event.position().toPoint().x() - self.tabRect(index).left()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        position = event.globalPosition().toPoint()
        if self.notebook.dragging():
            self.notebook.drag_to(position)
            return
        super().mouseMoveEvent(event)
        band = self.notebook.header_rect().adjusted(0, -self._UNDOCK_MARGIN, 0, self._UNDOCK_MARGIN)
        if self._grab and event.buttons() & Qt.LeftButton and not band.contains(position):
            page, offset = self._grab
            self._grab = None
            super().mouseReleaseEvent(QMouseEvent(
                QEvent.MouseButtonRelease, event.position(), event.globalPosition(),
                Qt.LeftButton, Qt.NoButton, event.modifiers(),
            ))
            self.notebook.begin_drag([page], QPoint(offset + 4, 36))
            self.notebook.drag_to(position)

    def mouseReleaseEvent(self, event):
        self._grab = None
        if self.notebook.dragging():
            self.notebook.end_drag(event.globalPosition().toPoint())
        else:
            super().mouseReleaseEvent(event)


class CustomNotebook(QTabWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.app_instance = None
        self._drag = None
        self._header_press = None
        self.setObjectName("documentNotebook")
        self.setTabBar(DetachTabBar(self))
        self.setDocumentMode(True)
        self.setTabsClosable(True)
        self.setMovable(True)
        self.setElideMode(Qt.ElideMiddle)
        self.setUsesScrollButtons(True)
        self.tabBar().setDrawBase(False)
        self.tabCloseRequested.connect(self.on_tab_close_requested)
        self.currentChanged.connect(self._update_close_buttons)
        self.tabBar().setContextMenuPolicy(Qt.CustomContextMenu)
        self.tabBar().customContextMenuRequested.connect(self._show_context_menu)
        self._page_stack = self.findChild(QStackedWidget, "qt_tabwidget_stackedwidget")
        self._page_stack.installEventFilter(self)

    def eventFilter(self, watched, event):
        if self._drag is not None and event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape:
            self.end_drag()
            return True
        if watched is self._page_stack and event.type() == QEvent.Paint and not self.count():
            self._paint_empty_hint(watched)
        return super().eventFilter(watched, event)

    def _paint_empty_hint(self, widget: QWidget):
        app = self.app_instance
        action = app.findChild(QAction, "file_open", Qt.FindDirectChildrenOnly) if isinstance(app, QWidget) else None
        shortcut = action.shortcut().toString(QKeySequence.NativeText) if action is not None else ""
        if shortcut:
            hint = self.tr(
                "Open a file from the Project Browser, drop one here, or press {shortcut}."
            ).format(shortcut=shortcut)
        else:
            hint = self.tr("Open a file from the Project Browser or drop one here.")
        colors = current_colors()
        line = widget.fontMetrics().height()
        top = widget.rect().center().y() - line
        width = widget.width() - 32
        title_font = QFont(widget.font())
        title_font.setWeight(QFont.Weight.DemiBold)
        painter = QPainter(widget)
        painter.setFont(title_font)
        painter.setPen(QColor(colors["text_muted"]))
        painter.drawText(QRect(16, top, width, line), Qt.AlignCenter, self.tr("No open files"))
        painter.setFont(widget.font())
        painter.setPen(QColor(colors["text_subtle"]))
        painter.drawText(
            QRect(16, top + line + 4, width, line * 3), Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap, hint
        )

    def host(self) -> "EditorGroupHost | None":
        widget = self.parentWidget()
        while widget is not None and not isinstance(widget, EditorGroupHost):
            widget = widget.parentWidget()
        return widget

    def addTab(self, widget: QWidget, *args):
        return self._finish_insert(super().addTab(widget, *args), widget)

    def insertTab(self, index: int, widget: QWidget, *args):
        return self._finish_insert(super().insertTab(index, widget, *args), widget)

    def _finish_insert(self, index: int, page: QWidget):
        if file_tab := getattr(page, "parent_tab", None):
            file_tab.parent_notebook = self
        self.tabBar().setTabButton(index, QTabBar.RightSide, _TabCloseButton(self, page))
        if tooltip := getattr(page, "_reasy_tab_tooltip", ""):
            self.setTabToolTip(index, tooltip)
        if getattr(file_tab, "hide_notebook_tab", False):
            self.tabBar().set_tab_shown(index, False)
        return index

    def take_page(self, page: QWidget) -> _TabState:
        index = self.indexOf(page)
        state = self._capture_state(index)
        self.removeTab(index)
        return state

    def put_page(self, page: QWidget, state: _TabState, index: int = -1) -> int:
        index = self.insertTab(index, page, state.icon, state.title)
        self._restore_state(index, state)
        self.setCurrentIndex(index)
        page.show()
        return index

    def _update_close_buttons(self, _index):
        for index in range(self.count()):
            button = self.tabBar().tabButton(index, QTabBar.RightSide)
            if isinstance(button, _TabCloseButton):
                button.update()

    def on_tab_close_requested(self, index):
        if self.app_instance:
            self.app_instance.close_tab(index, notebook=self)
        else:
            self.removeTab(index)

    def _show_context_menu(self, position: QPoint):
        index = self.tabBar().tabAt(position)
        if index < 0:
            return
        self.setCurrentIndex(index)
        menu = QMenu(self)
        page = self.widget(index)
        host = self.host()
        split_right = menu.addAction(self.tr("Split Right"))
        split_down = menu.addAction(self.tr("Split Down"))
        detach = menu.addAction(self.tr("Move into New Window"))
        for action in (split_right, split_down, detach):
            action.setEnabled(host is not None)
        menu.addSeparator()
        close = menu.addAction(self.tr("Close Tab"))
        close_others = menu.addAction(self.tr("Close Other Tabs"))
        close_right = menu.addAction(self.tr("Close Tabs to the Right"))
        close_right.setEnabled(index < self.count() - 1)
        close_saved = menu.addAction(self.tr("Close Saved Tabs"))
        menu.addSeparator()
        copy_path = menu.addAction(self.tr("Copy Path"))
        reveal = menu.addAction(self.tr("Reveal in Project Browser"))
        tab = getattr(page, "parent_tab", None)
        path = getattr(tab, "pak_source_path", None) or getattr(tab, "filename", None)
        copy_path.setEnabled(bool(path))
        reveal.setEnabled(bool(path and self.app_instance))
        chosen = menu.exec(self.tabBar().mapToGlobal(position))
        if chosen is split_right:
            host.split_page(page, Qt.Horizontal)
        elif chosen is split_down:
            host.split_page(page, Qt.Vertical)
        elif chosen is detach:
            host.root.float_pages([page], QRect(QCursor.pos() - QPoint(160, 18), _floating_size(page)))
        elif chosen is close:
            self.on_tab_close_requested(index)
        elif chosen is close_others:
            self._close_pages([self.widget(i) for i in range(self.count()) if i != index])
        elif chosen is close_right:
            self._close_pages([self.widget(i) for i in range(index + 1, self.count())])
        elif chosen is close_saved:
            self._close_pages([
                self.widget(i) for i in range(self.count())
                if not bool(getattr(getattr(self.widget(i), "parent_tab", None), "modified", False))
            ])
        elif chosen is copy_path:
            QGuiApplication.clipboard().setText(str(path))
        elif chosen is reveal:
            self.app_instance.reveal_tab_in_project(tab)

    def _close_pages(self, pages):
        if not self.app_instance:
            for page in pages:
                if (index := self.indexOf(page)) >= 0:
                    self.removeTab(index)
            return
        tabs = [self.app_instance.tabs.get(page) for page in pages]
        tabs = [tab for tab in tabs if tab is not None]
        if tabs and self.app_instance._confirm_tabs_close(tabs):
            for tab in tabs:
                self.app_instance._close_tab_object(tab)

    def _capture_state(self, index: int):
        bar = self.tabBar()
        return _TabState(
            self.tabText(index), self.tabIcon(index), self.tabToolTip(index),
            self.tabWhatsThis(index), bar.accessibleTabName(index),
            bar.tabData(index), bar.tabTextColor(index),
            self.isTabEnabled(index), bar.isTabVisible(index),
        )

    def _restore_state(self, index: int, state: _TabState):
        bar = self.tabBar()
        self.setTabToolTip(index, state.tool_tip)
        self.setTabWhatsThis(index, state.whats_this)
        self.setTabEnabled(index, state.enabled)
        bar.setAccessibleTabName(index, state.accessible_name)
        bar.setTabData(index, state.data)
        bar.setTabTextColor(index, state.text_color)
        bar.set_tab_shown(index, state.visible)

    def set_page_title(self, page, title: str):
        index = self.indexOf(page)
        if index >= 0:
            self.setTabText(index, title)
            if isinstance(window := self.window(), FloatingTabWindow):
                window.sync_title()

    def global_rect(self) -> QRect:
        return QRect(self.mapToGlobal(QPoint()), self.size())

    def header_rect(self) -> QRect:
        bar = self.tabBar()
        height = bar.height() if bar.isVisible() else self.fontMetrics().height() + 14
        return QRect(self.mapToGlobal(QPoint()), QSize(self.width(), height))

    def content_rect(self) -> QRect:
        return self.global_rect().adjusted(0, self.header_rect().height(), 0, 0)

    def holds_any(self, pages) -> bool:
        return any(self.indexOf(page) >= 0 for page in pages)

    def holds_only(self, pages) -> bool:
        return self.count() > 0 and {self.widget(index) for index in range(self.count())} <= set(pages)

    def insert_position(self, pos: QPoint) -> tuple[int, QLine]:
        bar, header = self.tabBar(), self.header_rect()
        x = bar.mapFromGlobal(pos).x()
        visible = [index for index in range(bar.count()) if bar.isTabVisible(index)]
        index = next((i for i in visible if x < bar.tabRect(i).center().x()), bar.count())
        if index < bar.count():
            edge = bar.tabRect(index).left()
        else:
            edge = bar.tabRect(visible[-1]).right() + 1 if visible else 4
        x = bar.mapToGlobal(QPoint(edge, 0)).x()
        return index, QLine(x, header.top() + 3, x, header.bottom() - 3)

    def dragging(self) -> bool:
        return self._drag is not None

    def begin_drag(self, pages, hotspot: QPoint):
        host = self.host()
        size = host.window().size() if host.is_whole_window(pages) else _floating_size(pages[0])
        self._drag = pages, QRect(-hotspot, size), self.tabText(self.indexOf(pages[0]))
        QApplication.setOverrideCursor(Qt.ClosedHandCursor)
        QApplication.instance().installEventFilter(self)
        host.root.set_dragging(True)

    def drag_to(self, pos: QPoint):
        pages, ghost, title = self._drag
        return self.host().root.update_drag(pos, pages, ghost.translated(pos), title)

    def end_drag(self, pos: QPoint | None = None):
        target = self.drag_to(pos) if pos is not None else None
        pages, self._drag = self._drag[0], None
        QApplication.instance().removeEventFilter(self)
        QApplication.restoreOverrideCursor()
        root = self.host().root
        root.set_dragging(False)
        if target is not None:
            root.dock(pages, target)

    def mousePressEvent(self, event):
        position = event.position().toPoint()
        on_header = self.count() and position.y() < self.header_rect().height()
        self._header_press = position if event.button() == Qt.LeftButton and on_header else None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        position = event.globalPosition().toPoint()
        if self._drag is not None:
            self.drag_to(position)
        elif self._header_press is not None and (
            (event.position().toPoint() - self._header_press).manhattanLength() >= QApplication.startDragDistance()
        ):
            self.begin_drag([self.widget(index) for index in range(self.count())],
                            QPoint(min(self._header_press.x(), 240), 36))
            self._header_press = None
            self.drag_to(position)
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._header_press = None
        if self._drag is not None:
            self.end_drag(event.globalPosition().toPoint())
        else:
            super().mouseReleaseEvent(event)
