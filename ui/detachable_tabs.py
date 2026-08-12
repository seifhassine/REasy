import sys
from dataclasses import dataclass

if sys.platform == "win32":
    from ctypes import wintypes
else:
    wintypes = None

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPainter,
    QPalette,
    QPen,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QMainWindow,
    QMenu,
    QMessageBox,
    QTabBar,
    QTabWidget,
    QWidget,
)


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

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        if self.underMouse() or self.isDown():
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(255, 255, 255, 42 if self.isDown() else 28))
            painter.drawEllipse(self.rect().adjusted(1, 1, -1, -1))
        active = self.notebook.currentWidget() is self.page
        pen = QPen(QColor("#eef0f3" if self.underMouse() or active else "#9b9da3"))
        pen.setWidthF(1.45)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.drawLine(6, 6, 12, 12)
        painter.drawLine(12, 6, 6, 12)


class _TabDropIndicator(QWidget):
    def __init__(self, notebook: "CustomNotebook"):
        super().__init__(notebook)
        self.notebook = notebook
        self.marker_x = 0
        self.empty = False
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.NoFocus)
        self.hide()

    def show_target(self, geometry: QRect, marker_x: int, empty: bool):
        self.marker_x, self.empty = marker_x, empty
        self.setGeometry(geometry)
        self.raise_()
        self.show()
        self.update()

    def paintEvent(self, _event):
        accent = self.notebook._tab_drop_accent()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        wash, outline = QColor(accent), QColor(accent)
        wash.setAlpha(34)
        outline.setAlpha(185)
        painter.fillRect(self.rect(), wash)
        painter.setPen(QPen(outline, 1.25))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -2, -2), 5, 5)
        marker = QPen(accent, 3)
        marker.setCapStyle(Qt.RoundCap)
        painter.setPen(marker)
        x = max(3, min(self.marker_x, self.width() - 4))
        painter.drawLine(x, 4, x, max(4, self.height() - 5))
        if self.empty:
            painter.setPen(self.palette().color(QPalette.Text))
            painter.drawText(
                self.rect().adjusted(15, 0, -8, 0),
                Qt.AlignLeft | Qt.AlignVCenter,
                self.tr("Drop tab here"),
            )


class FloatingTabWindow(QMainWindow):
    """Top-level home for a detached live page and both drag directions."""

    _HEADER_HEIGHT = 34

    def __init__(
        self,
        page: QWidget,
        state: _TabState,
        notebook: "CustomNotebook",
        return_index: int,
        set_app_icon=None,
    ):
        super().__init__()
        self.page, self.notebook = page, notebook
        self.tab_state, self.return_index = state, return_index
        self.file_tab = getattr(page, "parent_tab", None)
        self._reattach_on_close = True
        self._dragging = False
        self._drag_moved = False
        self._drag_origin = QPoint()
        self._drag_hotspot = QPoint()
        self._drop_index = None
        self._filter_installed = False
        self._drag_poll = QTimer(self)
        self._drag_poll.setInterval(24)
        self._drag_poll.timeout.connect(self._poll_native_drag)
        if set_app_icon:
            set_app_icon(self)

        self.setWindowTitle(state.title)
        self.setCentralWidget(page)
        page.show()
        self.setFocusPolicy(Qt.StrongFocus)

        main_window = self._main_window()
        if main_window:
            self.setStyleSheet(main_window.styleSheet())
            self.setPalette(main_window.palette())
            self.setFont(main_window.font())
        tab_menu = self.menuBar().addMenu(self.tr("Tab"))
        tab_menu.addAction(self.tr("Reattach"), self.close)
        if main_window and not getattr(self.file_tab, "skip_detached_menus", False):
            self._setup_main_window_actions(main_window)

    def _main_window(self):
        candidate = self.notebook.app_instance or self.notebook.window()
        return candidate if isinstance(candidate, QMainWindow) else None

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
        for action in source.actions():
            submenu = action.menu()
            if action.objectName() == "find_search":
                destination.addAction(self._find_action)
            elif action.isSeparator():
                destination.addSeparator()
            elif submenu:
                self._copy_menu(submenu, destination.addMenu(submenu.title()))
            else:
                destination.addAction(action)

    @staticmethod
    def _event_position(event):
        getter = getattr(event, "globalPosition", None)
        return getter().toPoint() if callable(getter) else QCursor.pos()

    def _start_drag(self, *, moved=False, origin=None):
        if self._dragging:
            return
        self._dragging = True
        self._drag_moved = moved
        self._drag_origin = QPoint(self.pos() if origin is None else origin)
        self._drop_index = None
        self.notebook.clear_floating_drop_target()

    def begin_tab_drag(self, global_position: QPoint, hotspot: QPoint):
        """Continue the tab gesture with the OS moving this final window."""
        self._drag_hotspot = QPoint(hotspot)
        self._start_drag(moved=True)
        self._move_with_pointer(global_position)
        handle = self.windowHandle()
        if handle and handle.startSystemMove():
            return
        if app := QApplication.instance():
            app.installEventFilter(self)
            self._filter_installed = True
        self.setCursor(Qt.ClosedHandCursor)

    def _move_with_pointer(self, global_position: QPoint):
        position = global_position - self._drag_hotspot
        screen = QGuiApplication.screenAt(global_position) or QGuiApplication.primaryScreen()
        if screen:
            area = screen.availableGeometry()
            edge = min(180, self.width())
            position.setX(max(area.left() - self.width() + edge,
                              min(position.x(), area.right() - edge + 1)))
            position.setY(max(area.top(), min(
                position.y(), area.bottom() - self._HEADER_HEIGHT + 1
            )))
        self.move(position)
        self._update_drag(global_position)

    def _update_drag(self, global_position: QPoint):
        if not self._dragging:
            return
        self._drag_moved |= (
            self.pos() - self._drag_origin
        ).manhattanLength() >= QApplication.startDragDistance()
        self._drop_index = self.notebook.update_floating_drop_target(
            global_position
        ) if self._drag_moved else None
        if self._drop_index is None:
            self.notebook.clear_floating_drop_target()

    def _finish_drag(self, global_position: QPoint, *, cancel=False):
        if not self._dragging:
            return
        self._update_drag(global_position)
        drop_index = self.return_index if cancel else self._drop_index
        self._clear_drag()
        if drop_index is not None:
            self._dock(drop_index)
        else:
            self.raise_()
            self.activateWindow()

    def _clear_drag(self):
        if self._filter_installed:
            if app := QApplication.instance():
                app.removeEventFilter(self)
            self._filter_installed = False
        self._drag_poll.stop()
        self._dragging = False
        self._drag_moved = False
        self._drop_index = None
        self.unsetCursor()
        self.notebook.clear_floating_drop_target()

    def _dock(self, index: int):
        page = self.takeCentralWidget()
        if page is None:
            return
        self._reattach_on_close = False
        self.hide()
        self.notebook._reattach_window(self, page, index=index)
        self.close()

    def eventFilter(self, watched, event):
        if self._filter_installed:
            if event.type() == QEvent.MouseMove:
                if event.buttons() & Qt.LeftButton:
                    self._move_with_pointer(self._event_position(event))
            if event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                position = self._event_position(event)
                QTimer.singleShot(0, lambda: self._finish_drag(position))
            if event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape:
                self._finish_drag(QCursor.pos(), cancel=True)
                return True
        return super().eventFilter(watched, event)

    def event(self, event):
        event_type = event.type()
        if wintypes is None and event_type == QEvent.NonClientAreaMouseButtonPress:
            if event.button() == Qt.LeftButton:
                self._start_drag()
        elif self._dragging and event_type == QEvent.NonClientAreaMouseMove:
            self._update_drag(self._event_position(event))
        elif self._dragging and event_type == QEvent.NonClientAreaMouseButtonRelease:
            if event.button() == Qt.LeftButton:
                position = self._event_position(event)
                QTimer.singleShot(0, lambda: self._finish_drag(position))
        return super().event(event)

    def nativeEvent(self, event_type, message):
        if wintypes is not None and not self._filter_installed:
            try:
                native = wintypes.MSG.from_address(int(message))
                if native.message == 0x00A1 and int(native.wParam) == 2:
                    self._start_drag()  # WM_NCLBUTTONDOWN / HTCAPTION
                elif native.message == 0x0216 and self._dragging:
                    self._update_drag(QCursor.pos())  # WM_MOVING
                elif native.message in (0x00A2, 0x0232) and self._dragging:
                    position = QCursor.pos()  # WM_NCLBUTTONUP / WM_EXITSIZEMOVE
                    QTimer.singleShot(0, lambda: self._finish_drag(position))
            except (AttributeError, TypeError, ValueError, OSError):
                pass
        return super().nativeEvent(event_type, message)

    def moveEvent(self, event):
        super().moveEvent(event)
        if wintypes is None and QGuiApplication.mouseButtons() & Qt.LeftButton:
            if not self._dragging:
                self._start_drag(origin=event.oldPos())
            self._update_drag(QCursor.pos())
            self._drag_poll.start()

    def _poll_native_drag(self):
        if not self._dragging:
            self._drag_poll.stop()
        elif QGuiApplication.mouseButtons() & Qt.LeftButton:
            self._update_drag(QCursor.pos())
        else:
            self._finish_drag(QCursor.pos())

    def set_tab_title(self, title: str):
        self.tab_state.title = title
        self.setWindowTitle(title)

    def close_without_reattach(self):
        self._reattach_on_close = False
        page = self.takeCentralWidget()
        self.close()
        if page:
            page.setParent(None)

    def closeEvent(self, event):
        self._clear_drag()
        if self.file_tab and hasattr(self.file_tab, "_find_dialog"):
            try:
                if self.file_tab._find_dialog and self.file_tab._find_dialog.isVisible():
                    self.file_tab._find_dialog.close()
            except RuntimeError:
                pass
        page = self.centralWidget()
        if self._reattach_on_close and page:
            self.takeCentralWidget()
            self.notebook._reattach_window(self, page)
        else:
            self.notebook._forget_floating_window(self)
        main_window = self._main_window()
        if main_window and hasattr(main_window, "_on_tab_changed_for_find"):
            QTimer.singleShot(0, main_window._on_tab_changed_for_find)
        super().closeEvent(event)

    def open_find_dialog(self):
        if not self.file_tab:
            main_window = self._main_window()
            if main_window:
                self.file_tab = next((
                    tab for tab in getattr(main_window, "tabs", {}).values()
                    if getattr(tab, "notebook_widget", None) is self.centralWidget()
                ), None)
        if self.file_tab:
            self.file_tab.open_find_dialog()
        else:
            QMessageBox.warning(
                self, self.tr("Warning"),
                self.tr("Cannot open find dialog for this tab"),
            )

    def keyPressEvent(self, event):
        if (
            not getattr(self.file_tab, "suppress_general_shortcuts", False)
            and event.modifiers() == Qt.ControlModifier
            and event.key() == Qt.Key_F
        ):
            self.open_find_dialog()
            event.accept()
        else:
            super().keyPressEvent(event)


class DetachTabBar(QTabBar):
    _DETACH_MARGIN = 36

    def __init__(self, notebook: "CustomNotebook"):
        super().__init__(notebook)
        self.notebook = notebook
        self._page = None
        self._press_position = QPoint()
        self._hotspot_x = 0
        self.setObjectName("documentTabBar")
        self.setDocumentMode(True)
        self.setDrawBase(False)
        self.setMovable(True)
        self.setExpanding(False)
        self.setElideMode(Qt.ElideMiddle)
        self.setUsesScrollButtons(True)
        self.setSelectionBehaviorOnRemove(QTabBar.SelectPreviousTab)
        self.setToolTip(self.tr("Drag to reorder. Drag a tab away to detach it."))

    def _reset_drag(self):
        self._page = None
        self._hotspot_x = 0
        self.unsetCursor()

    def mousePressEvent(self, event):
        self._reset_drag()
        if event.button() == Qt.LeftButton:
            index = self.tabAt(event.position().toPoint())
            if index >= 0:
                self._page = self.notebook.widget(index)
                self._press_position = event.position().toPoint()
                self._hotspot_x = self._press_position.x() - self.tabRect(index).left()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        position = event.position().toPoint()
        dragging = (
            self._page is not None
            and event.buttons() & Qt.LeftButton
            and (position - self._press_position).manhattanLength()
            >= QApplication.startDragDistance()
        )
        if not dragging:
            return super().mouseMoveEvent(event)
        self.setCursor(Qt.ClosedHandCursor)
        super().mouseMoveEvent(event)
        if self._outside(position):
            page, hotspot = self._page, self._hotspot_x
            self._reset_drag()
            size = self.notebook._detached_window_size(page)
            hotspot = QPoint(min(max(hotspot, 24), max(24, size.width() - 24)), 18)
            self.notebook.detach_widget(
                page, event.globalPosition().toPoint(), drag_hotspot=hotspot
            )

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self._reset_drag()

    def _outside(self, position: QPoint):
        horizontal = self.shape() in (
            QTabBar.RoundedNorth, QTabBar.RoundedSouth,
            QTabBar.TriangularNorth, QTabBar.TriangularSouth,
        )
        coordinate, extent = (
            (position.y(), self.height()) if horizontal
            else (position.x(), self.width())
        )
        return coordinate < -self._DETACH_MARGIN or coordinate > extent + self._DETACH_MARGIN


class CustomNotebook(QTabWidget):
    tabDetached = Signal(QWidget)
    tabReattached = Signal(QWidget)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.app_instance = None
        self._floating_windows = []
        self._set_icon_callback = None
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
        self._drop_indicator = _TabDropIndicator(self)

    def addTab(self, widget: QWidget, *args):
        return self._finish_insert(super().addTab(widget, *args), widget)

    def insertTab(self, index: int, widget: QWidget, *args):
        return self._finish_insert(super().insertTab(index, widget, *args), widget)

    def _finish_insert(self, index: int, page: QWidget):
        self.tabBar().setTabButton(index, QTabBar.RightSide, _TabCloseButton(self, page))
        if tooltip := getattr(page, "_reasy_tab_tooltip", ""):
            self.setTabToolTip(index, tooltip)
        return index

    def _update_close_buttons(self, _index):
        for index in range(self.count()):
            button = self.tabBar().tabButton(index, QTabBar.RightSide)
            if isinstance(button, _TabCloseButton):
                button.update()

    def on_tab_close_requested(self, index):
        if self.app_instance:
            self.app_instance.close_tab(index)
        else:
            self.removeTab(index)

    def _show_context_menu(self, position: QPoint):
        index = self.tabBar().tabAt(position)
        if index < 0:
            return
        self.setCurrentIndex(index)
        menu = QMenu(self)
        detach = menu.addAction(self.tr("Detach Tab"))
        close = menu.addAction(self.tr("Close Tab"))
        chosen = menu.exec(self.tabBar().mapToGlobal(position))
        if chosen is detach:
            self.detach_tab(index)
        elif chosen is close:
            self.on_tab_close_requested(index)

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
        bar.setTabVisible(index, state.visible)

    def _tab_drop_accent(self):
        provider = getattr(self.app_instance, "_theme_accent_color", None)
        if callable(provider):
            try:
                if (accent := QColor(provider())).isValid():
                    return accent
            except (RuntimeError, TypeError):
                pass
        accent = self.palette().color(QPalette.Highlight)
        return accent if accent.isValid() else QColor("#00aaff")

    def _drop_geometry(self):
        bar = self.tabBar()
        height = bar.height()
        top = bar.mapTo(self, QPoint()).y() if height else 0
        return QRect(0, top, max(1, self.width()), height or max(32, self.fontMetrics().height() + 14))

    def _drop_position(self, global_position: QPoint):
        if not self.isVisible():
            return None
        target = self._drop_geometry()
        global_target = QRect(self.mapToGlobal(target.topLeft()), target.size())
        if not global_target.adjusted(0, -6, 0, 8).contains(global_position):
            return None
        bar = self.tabBar()
        visible = [i for i in range(bar.count()) if bar.isTabVisible(i)]
        if not visible:
            return bar.count(), 9, True
        x = bar.mapFromGlobal(global_position).x()
        origin = bar.mapTo(self, QPoint()).x() - target.left()
        for index in visible:
            rect = bar.tabRect(index)
            if x < rect.center().x():
                return index, origin + rect.left(), False
        return bar.count(), origin + bar.tabRect(visible[-1]).right() + 1, False

    def update_floating_drop_target(self, global_position: QPoint):
        target = self._drop_position(global_position)
        if target is None:
            self.clear_floating_drop_target()
            return None
        index, marker, empty = target
        self._drop_indicator.show_target(self._drop_geometry(), marker, empty)
        return index

    def clear_floating_drop_target(self):
        self._drop_indicator.hide()

    @staticmethod
    def _detached_window_size(page: QWidget):
        return page.size().expandedTo(QSize(720, 480)).boundedTo(QSize(1600, 1200))

    def detach_widget(self, page: QWidget, position=None, *, drag_hotspot=None):
        index = self.indexOf(page)
        return None if index < 0 else self.detach_tab(
            index, position, drag_hotspot=drag_hotspot
        )

    def detach_tab(self, index: int, position=None, *, drag_hotspot=None):
        page = self.widget(index)
        if page is None:
            return None
        state = self._capture_state(index)
        content_size = self._detached_window_size(page)
        self.removeTab(index)
        window = FloatingTabWindow(
            page, state, self, index, self._set_icon_callback
        )
        self._floating_windows.append(window)
        size = content_size + QSize(0, window.menuBar().sizeHint().height())
        window.resize(size)
        live_drag = position is not None and drag_hotspot is not None
        if position is not None and not live_drag:
            window.move(position - QPoint(min(160, size.width() // 3), 18))
        window.show()
        window.raise_()
        self.tabDetached.emit(page)
        if self.app_instance and hasattr(
            self.app_instance, "_check_and_close_shared_find_dialog"
        ):
            self.app_instance._check_and_close_shared_find_dialog()
        if live_drag:
            window.begin_tab_drag(position, drag_hotspot)
        else:
            window.activateWindow()
        return window

    def _reattach_window(self, window, page, *, index=None):
        index = window.return_index if index is None else index
        index = min(max(index, 0), self.count())
        index = self.insertTab(index, page, window.tab_state.icon, window.tab_state.title)
        self._restore_state(index, window.tab_state)
        self.setCurrentIndex(index)
        page.show()
        self._forget_floating_window(window)
        self.tabReattached.emit(page)
        return index

    def _forget_floating_window(self, window):
        if window in self._floating_windows:
            self._floating_windows.remove(window)

    def floating_window_for(self, page):
        return next((window for window in self._floating_windows if window.page is page), None)

    def set_page_title(self, page, title: str):
        index = self.indexOf(page)
        if index >= 0:
            self.setTabText(index, title)
        elif window := self.floating_window_for(page):
            window.set_tab_title(title)
