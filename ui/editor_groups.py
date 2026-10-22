from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QEvent, QLine, QPoint, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QSizePolicy, QSplitter, QVBoxLayout, QWidget

lazy from ui.detachable_tabs import CustomNotebook, FloatingTabWindow
lazy from ui.styles import current_colors, set_style_role

_SIDES = {
    "left": (Qt.Horizontal, False),
    "right": (Qt.Horizontal, True),
    "top": (Qt.Vertical, False),
    "bottom": (Qt.Vertical, True),
}
_COMPASS = {
    "tab": QPoint(0, 0), "left": QPoint(-1, 0), "right": QPoint(1, 0), "top": QPoint(0, -1), "bottom": QPoint(0, 1),
}
_GUIDE, _GUIDE_GAP, _EDGE_INSET = 30, 4, 10
_EDGE_REACH, _GROUP_REACH = _EDGE_INSET + _GUIDE, 1 / 3


@dataclass(slots=True)
class DropTarget:
    zone: str
    area: QRect
    host: EditorGroupHost | None = None
    notebook: CustomNotebook | None = None
    index: int = -1
    marker: QLine | None = None


def _side_area(rect: QRect, side: str, fraction: float) -> QRect:
    width, height = int(rect.width() * fraction), int(rect.height() * fraction)
    return {
        "left": QRect(rect.left(), rect.top(), width, rect.height()),
        "right": QRect(rect.right() - width + 1, rect.top(), width, rect.height()),
        "top": QRect(rect.left(), rect.top(), rect.width(), height),
        "bottom": QRect(rect.left(), rect.bottom() - height + 1, rect.width(), height),
    }[side]


def _side_at(rect: QRect, pos: QPoint, reach_x: float, reach_y: float) -> str | None:
    if not rect.contains(pos):
        return None
    reach = {
        "left": (pos.x() - rect.left()) / reach_x,
        "right": (rect.right() - pos.x()) / reach_x,
        "top": (pos.y() - rect.top()) / reach_y,
        "bottom": (rect.bottom() - pos.y()) / reach_y,
    }
    side = min(reach, key=reach.get)
    return side if reach[side] < 1 else None


class _DropOverlay(QWidget):

    def __init__(self, parent: QWidget):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        for attribute in (Qt.WA_TranslucentBackground, Qt.WA_TransparentForMouseEvents, Qt.WA_ShowWithoutActivating):
            self.setAttribute(attribute)
        self._guides, self._target, self._title = [], None, ""

    def present(self, bounds: QRect, guides=(), target: DropTarget | None = None, title: str = "") -> None:
        self._guides, self._target, self._title = list(guides), target, title
        self.setGeometry(bounds)
        self.show()
        self.raise_()
        self.update()

    def paintEvent(self, _event):
        colors = current_colors()
        accent = QColor(colors["accent"])
        wash = QColor(accent)
        wash.setAlpha(46)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.translate(-self.pos())
        target = self._target
        if target is not None:
            painter.setPen(QPen(accent, 1))
            painter.setBrush(wash)
            painter.drawRoundedRect(QRectF(target.area).adjusted(1.5, 1.5, -1.5, -1.5), 3, 3)
            if target.marker is not None:
                painter.setPen(QPen(accent, 3))
                painter.drawLine(target.marker)
            if self._title:
                painter.setPen(QColor(colors["text_bright"]))
                painter.drawText(target.area.adjusted(12, 8, -12, -8), Qt.AlignLeft | Qt.AlignTop, self._title)
        for rect, zone, notebook in self._guides:
            hot = target is not None and (target.zone, target.notebook) == (zone, notebook)
            painter.setPen(QPen(accent if hot else QColor(colors["border_strong"]), 1))
            painter.setBrush(QColor(colors["panel"]))
            painter.drawRoundedRect(QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5), 3, 3)
            glyph = rect.adjusted(7, 7, -7, -7)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(glyph)
            painter.fillRect(glyph if zone == "tab" else _side_area(glyph, zone, 0.5), accent if hot else wash)


class EditorGroupHost(QWidget):

    activePageChanged = Signal(object)
    layoutChanged = Signal()

    def __init__(self, primary: CustomNotebook, app_window, parent=None, root: EditorGroupHost | None = None):
        super().__init__(parent)
        self.setObjectName("editorGroupHost")
        self.app_window = app_window
        self.root = root or self
        self.dragging = False
        self._windows: list[FloatingTabWindow] = []
        self._active_notebook = primary
        self._splitter = self._new_splitter(Qt.Horizontal, self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._splitter)
        self._notebooks: list[CustomNotebook] = []
        self._register(primary)
        self._splitter.addWidget(primary)
        if root is None:
            self._guides, self._ghost = _DropOverlay(self), _DropOverlay(self)
        else:
            self.layoutChanged.connect(root.layoutChanged)
        if app := QApplication.instance():
            app.focusChanged.connect(self._on_focus_changed)

    @staticmethod
    def _new_splitter(orientation, parent=None) -> QSplitter:
        splitter = QSplitter(orientation, parent)
        splitter.setObjectName("editorGroupSplitter")
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(4)
        return splitter

    def _register(self, notebook: CustomNotebook, position: int = 0) -> None:
        notebook.app_instance = self.app_window
        notebook.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        notebook.setMinimumSize(80, 60)
        self._notebooks.insert(position, notebook)
        notebook.currentChanged.connect(
            lambda index, group=notebook: self._on_current_changed(group, index)
        )
        notebook.tabBar().tabBarClicked.connect(
            lambda _index, group=notebook: self._activate(group)
        )
        notebook.tabBar().tabMoved.connect(lambda *_: self.layoutChanged.emit())
        notebook.installEventFilter(self)
        notebook.tabBar().installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.FocusIn, QEvent.MouseButtonPress):
            group = next(
                (notebook for notebook in self._notebooks if watched in (notebook, notebook.tabBar())),
                None,
            )
            if group is not None:
                self._activate(group)
        return super().eventFilter(watched, event)

    def _activate(self, notebook: CustomNotebook) -> None:
        if notebook not in self._notebooks:
            return
        self._active_notebook = notebook
        self._sync_active_group()
        self.activePageChanged.emit(notebook.currentWidget())

    def _sync_active_group(self) -> None:
        active = self.active_notebook() if len(self._notebooks) > 1 else None
        for notebook in self._notebooks:
            bar = notebook.tabBar()
            inactive = active is not None and notebook is not active
            if bool(bar.property("inactiveGroup")) != inactive:
                set_style_role(bar, inactiveGroup=inactive)

    def _on_focus_changed(self, _old, current) -> None:
        widget = current
        while widget is not None:
            if widget in self._notebooks:
                if widget is not self._active_notebook:
                    self._activate(widget)
                return
            widget = widget.parentWidget()

    def _on_current_changed(self, notebook: CustomNotebook, index: int) -> None:
        if index >= 0:
            self._activate(notebook)
        else:
            self.activePageChanged.emit(self.active_page())
        self.layoutChanged.emit()

    def notebooks(self) -> tuple[CustomNotebook, ...]:
        return tuple(self._notebooks)

    def pages(self) -> list[QWidget]:
        return [notebook.widget(index) for notebook in self._notebooks for index in range(notebook.count())]

    def active_notebook(self) -> CustomNotebook:
        if self._active_notebook not in self._notebooks:
            self._active_notebook = self._notebooks[0]
        return self._active_notebook

    def active_page(self):
        return self.active_notebook().currentWidget()

    def count(self) -> int:
        return sum(notebook.count() for notebook in self._notebooks)

    def notebook_for(self, page) -> CustomNotebook | None:
        return next((notebook for notebook in self._notebooks if notebook.indexOf(page) >= 0), None)

    def group_index_for(self, page) -> int:
        notebook = self.notebook_for(page)
        return self._notebooks.index(notebook) if notebook is not None else 0

    def floating_windows(self) -> list[FloatingTabWindow]:
        return list(self.root._windows)

    def hosts(self) -> list[EditorGroupHost]:
        return [self.root, *(window.host for window in self.root._windows)]

    def all_notebooks(self) -> list[CustomNotebook]:
        return [notebook for host in self.hosts() for notebook in host._notebooks]

    def find_notebook(self, page) -> CustomNotebook | None:
        return next((notebook for notebook in self.all_notebooks() if notebook.indexOf(page) >= 0), None)

    def window_for(self, page) -> FloatingTabWindow | None:
        return next((window for window in self.root._windows if window.host.notebook_for(page)), None)

    def locate(self, page) -> tuple[int, int, int] | None:
        for window, host in enumerate(self.hosts(), -1):
            if (notebook := host.notebook_for(page)) is not None:
                return window, host._notebooks.index(notebook), notebook.indexOf(page)
        return None

    def activate_page(self, page) -> bool:
        notebook = self.find_notebook(page)
        if notebook is None:
            return False
        notebook.setCurrentWidget(page)
        host = notebook.host()
        host._activate(notebook)
        if not (window := host.window()).isActiveWindow():
            window.show()
            window.raise_()
            window.activateWindow()
        return True

    def remove_page(self, page) -> bool:
        notebook = self.find_notebook(page)
        if notebook is None:
            return False
        notebook.removeTab(notebook.indexOf(page))
        self.root.prune_empty_groups()
        return True

    def ensure_group(self, index: int, orientation=Qt.Horizontal) -> CustomNotebook:
        while len(self._notebooks) <= index:
            self.add_group(None, "right" if orientation == Qt.Horizontal else "bottom")
        return self._notebooks[index]

    def add_group(self, anchor: CustomNotebook | None, side: str) -> CustomNotebook:
        notebook = CustomNotebook()
        after = _SIDES[side][1]
        if anchor is None:
            self._register(notebook, len(self._notebooks) if after else 0)
        else:
            self._register(notebook, self._notebooks.index(anchor) + after)
        self._insert_beside(anchor or self._splitter, notebook, side)
        return notebook

    def _insert_beside(self, anchor: QWidget, widget: QWidget, side: str) -> None:
        orientation, after = _SIDES[side]
        edge = anchor is self._splitter
        parent = anchor if edge else anchor.parentWidget()
        if parent.count() > 1 and parent.orientation() != orientation:
            branch, sizes = self._new_splitter(orientation), parent.sizes()
            if edge:
                self._set_root(branch)
            else:
                parent.insertWidget(parent.indexOf(anchor), branch)
            branch.addWidget(anchor)
            if not edge:
                parent.setSizes(sizes)
            parent = branch
        parent.setOrientation(orientation)
        sizes = parent.sizes()
        index = (len(sizes) if after else 0) if anchor is parent else parent.indexOf(anchor) + after
        if edge:
            sizes = [max(1, size) * 2 for size in sizes]
            sizes.insert(index, sum(sizes) // 2)
        else:
            shared = max(2, sizes[index - after])
            sizes[index - after] = shared - shared // 2
            sizes.insert(index, shared // 2)
        parent.insertWidget(index, widget)
        parent.setSizes(sizes)

    def _set_root(self, splitter: QSplitter) -> None:
        layout = self.layout()
        layout.removeWidget(self._splitter)
        layout.addWidget(splitter)
        splitter.show()
        layout.activate()
        self._splitter = splitter

    def split_page(self, page=None, orientation=Qt.Horizontal):
        page = page or self.active_page()
        notebook = self.notebook_for(page)
        if page is None or notebook is None:
            return None
        side = "right" if orientation == Qt.Horizontal else "bottom"
        self.root.dock([page], DropTarget(side, QRect(), self, notebook))
        return self.notebook_for(page)

    def move_page_to_group(self, page, group_index: int, orientation=Qt.Horizontal):
        source = self.root.find_notebook(page)
        if source is None:
            return None
        target = self.ensure_group(max(0, int(group_index)), orientation)
        if source is not target:
            target.put_page(page, source.take_page(page))
        return target

    def prune_empty_groups(self) -> None:
        if self is self.root:
            for window in self.floating_windows():
                if window.host.count():
                    window.host.prune_empty_groups()
                else:
                    window.discard()
        changed = False
        for notebook in list(self._notebooks):
            if len(self._notebooks) > 1 and not notebook.count():
                self._remove_notebook(notebook)
                changed = True
        if self._active_notebook not in self._notebooks:
            self._active_notebook = self._notebooks[0]
            self.activePageChanged.emit(self._active_notebook.currentWidget())
        if changed:
            self._update_primary_reference()
            self._sync_active_group()
            self.layoutChanged.emit()

    def _remove_notebook(self, notebook: CustomNotebook) -> None:
        parent = notebook.parentWidget()
        self._notebooks.remove(notebook)
        notebook.setParent(None)
        notebook.deleteLater()
        if isinstance(parent, QSplitter):
            self._collapse(parent)

    def _collapse(self, splitter: QSplitter) -> None:
        while splitter.count() == 1:
            child = splitter.widget(0)
            if splitter is self._splitter:
                if isinstance(child, QSplitter):
                    self._set_root(child)
                    splitter.setParent(None)
                    splitter.deleteLater()
                return
            parent = splitter.parentWidget()
            sizes = parent.sizes()
            parent.insertWidget(parent.indexOf(splitter), child)
            splitter.setParent(None)
            splitter.deleteLater()
            parent.setSizes(sizes)
            splitter = parent

    def _update_primary_reference(self) -> None:
        if self is not self.root or getattr(self.app_window, "notebook", None) in self._notebooks:
            return
        self.app_window.notebook = self._notebooks[0]
        workspace = getattr(self.app_window, "project_workspace", None)
        sessions = getattr(workspace, "sessions", None)
        if sessions is not None:
            sessions.notebook = self._notebooks[0]

    def set_dragging(self, dragging: bool) -> None:
        self.dragging = dragging
        if not dragging:
            self._guides.hide()
            self._ghost.hide()
        self.layoutChanged.emit()

    def global_rect(self) -> QRect:
        return QRect(self.mapToGlobal(QPoint()), self.size())

    def guides(self, notebook: CustomNotebook | None, pages) -> list[tuple[QRect, str, CustomNotebook | None]]:
        guides = []
        if notebook is not None and not notebook.holds_only(pages):
            content = notebook.content_rect()
            size = max(14, min(_GUIDE, (min(content.width(), content.height()) - 4 * _GUIDE_GAP) // 3))
            for zone, offset in _COMPASS.items():
                if zone == "tab" and notebook.holds_any(pages):
                    continue
                rect = QRect(0, 0, size, size)
                rect.moveCenter(content.center() + offset * (size + _GUIDE_GAP))
                guides.append((rect, zone, notebook))
        if len(self._notebooks) > 1:
            area = self._edge_area().adjusted(_EDGE_INSET, _EDGE_INSET, -_EDGE_INSET, -_EDGE_INSET)
            centers = {
                "left": QPoint(area.left() + _GUIDE // 2, area.center().y()),
                "right": QPoint(area.right() - _GUIDE // 2, area.center().y()),
                "top": QPoint(area.center().x(), area.top() + _GUIDE // 2),
                "bottom": QPoint(area.center().x(), area.bottom() - _GUIDE // 2),
            }
            for zone, center in centers.items():
                rect = QRect(0, 0, _GUIDE, _GUIDE)
                rect.moveCenter(center)
                guides.append((rect, zone, None))
        return guides

    def _edge_area(self) -> QRect:
        return self.global_rect().adjusted(0, self._notebooks[0].header_rect().height(), 0, 0)

    def drop_target(self, pos: QPoint, notebook: CustomNotebook | None, pages) -> DropTarget | None:
        for rect, zone, group in self.guides(notebook, pages):
            if rect.adjusted(-2, -2, 2, 2).contains(pos):
                return self._target(zone, group)
        accepts = notebook is not None and not notebook.holds_only(pages)
        if accepts and notebook.header_rect().adjusted(0, -4, 0, 4).contains(pos):
            index, marker = notebook.insert_position(pos)
            return DropTarget("tab", notebook.global_rect(), self, notebook, index, marker)
        if len(self._notebooks) > 1 and (side := _side_at(self._edge_area(), pos, _EDGE_REACH, _EDGE_REACH)):
            return self._target(side, None)
        if accepts:
            content = notebook.content_rect()
            reach_x, reach_y = content.width() * _GROUP_REACH, content.height() * _GROUP_REACH
            if side := _side_at(content, pos, reach_x, reach_y):
                return self._target(side, notebook)
        return None

    def _target(self, zone: str, group: CustomNotebook | None) -> DropTarget:
        if group is None:
            return DropTarget(zone, _side_area(self.global_rect(), zone, 1 / 3), self)
        area = group.global_rect()
        return DropTarget(zone, area if zone == "tab" else _side_area(area, zone, 0.5), self, group)

    def raise_window(self, window: FloatingTabWindow) -> None:
        if window in self._windows:
            self._windows.remove(window)
            self._windows.append(window)

    def _hovered(self, pos: QPoint, exclude=None) -> tuple[EditorGroupHost | None, CustomNotebook | None]:
        window = next((
            window for window in [*reversed(self._windows), self.window()]
            if window is not exclude and window.isVisible() and not window.isMinimized()
            and window.frameGeometry().contains(pos)
        ), None)
        widget = window.childAt(window.mapFromGlobal(pos)) if window is not None else None
        notebook = None
        while widget is not None and widget is not window:
            if notebook is None and isinstance(widget, CustomNotebook):
                notebook = widget
            if isinstance(widget, EditorGroupHost):
                return widget, notebook
            widget = widget.parentWidget()
        return None, None

    def update_drag(self, pos: QPoint, pages, ghost: QRect | None = None, title: str = "",
                    exclude: FloatingTabWindow | None = None) -> DropTarget | None:
        host, notebook = self._hovered(pos, exclude)
        target = host.drop_target(pos, notebook, pages) if host is not None else None
        if host is not None:
            self._guides.present(host.global_rect(), host.guides(notebook, pages), target)
        else:
            self._guides.hide()
        if target is None and ghost is not None:
            target = DropTarget("float", ghost)
            self._ghost.present(ghost, target=target, title=title)
        else:
            self._ghost.hide()
        return target

    def dock(self, pages, target: DropTarget) -> None:
        pages = [page for page in pages if self.find_notebook(page) is not None]
        if not pages:
            return
        current = self.find_notebook(pages[0]).currentWidget()
        current = current if current in pages else pages[0]
        if target.zone == "float":
            self._float(pages, target.area)
        else:
            notebook = target.notebook
            if target.zone != "tab":
                notebook = target.host.add_group(notebook, target.zone)
            self._move_pages(pages, notebook, target.index)
        self.prune_empty_groups()
        self.activate_page(current)
        self.layoutChanged.emit()

    def float_pages(self, pages, area: QRect) -> None:
        self.dock(pages, DropTarget("float", area))

    def is_whole_window(self, pages) -> bool:
        return self is not self.root and set(self.pages()) <= set(pages)

    def _float(self, pages, area: QRect) -> None:
        source = self.find_notebook(pages[0]).host()
        if source.is_whole_window(pages):
            source.window().setGeometry(area)
            return
        window = FloatingTabWindow(self)
        window.setGeometry(area)
        self._move_pages(pages, window.host.active_notebook(), -1)
        window.show()

    def _move_pages(self, pages, target: CustomNotebook, index: int) -> None:
        for page in pages:
            source = self.find_notebook(page)
            current = source.indexOf(page)
            if source is target:
                destination = target.count() - 1 if index < 0 else index - (current < index)
                target.tabBar().moveTab(current, destination)
                target.setCurrentIndex(destination)
            else:
                target.put_page(page, source.take_page(page), index)
            if index >= 0:
                index = target.indexOf(page) + 1

    def redock(self, window: FloatingTabWindow) -> None:
        target = self.active_notebook()
        self._move_pages(window.host.pages(), target, -1)
        self._activate(target)
        self.layoutChanged.emit()

    @staticmethod
    def _orientation_name(splitter: QSplitter) -> str:
        return "vertical" if splitter.orientation() == Qt.Vertical else "horizontal"

    def _serialize_node(self, widget) -> dict | None:
        if isinstance(widget, CustomNotebook):
            if widget not in self._notebooks:
                return None
            return {"type": "group", "index": self._notebooks.index(widget)}
        if not isinstance(widget, QSplitter):
            return None
        children = [
            node
            for index in range(widget.count())
            if (node := self._serialize_node(widget.widget(index))) is not None
        ]
        return {
            "type": "split",
            "orientation": self._orientation_name(widget),
            "sizes": widget.sizes(),
            "children": children,
        }

    def snapshot(self) -> dict:
        return {
            "version": 2,
            "orientation": self._orientation_name(self._splitter),
            "sizes": self._splitter.sizes(),
            "tree": self._serialize_node(self._splitter),
            "active_group": self._notebooks.index(self.active_notebook()),
        }

    def restore_layout(self, state: dict) -> None:
        if not isinstance(state, dict):
            return
        tree = state.get("tree")
        if isinstance(tree, dict) and tree.get("type") == "split":
            self._restore_tree(tree)
        else:
            orientation = (
                Qt.Vertical if state.get("orientation") == "vertical" else Qt.Horizontal
            )
            self._splitter.setOrientation(orientation)
            sizes = state.get("sizes")
            if isinstance(sizes, list) and len(sizes) == self._splitter.count():
                self._splitter.setSizes([max(1, int(size)) for size in sizes])
        active = state.get("active_group", 0)
        if isinstance(active, int) and 0 <= active < len(self._notebooks):
            self._active_notebook = self._notebooks[active]
        self._sync_active_group()

    def _restore_tree(self, tree: dict) -> None:
        referenced: list[int] = []

        def collect(node) -> None:
            if not isinstance(node, dict):
                return
            if node.get("type") == "group":
                try:
                    referenced.append(int(node.get("index")))
                except (TypeError, ValueError):
                    pass
                return
            for child in node.get("children", []):
                collect(child)

        collect(tree)
        if (
            len(referenced) != len(set(referenced))
            or any(index < 0 or index >= len(self._notebooks) for index in referenced)
        ):
            return

        for notebook in self._notebooks:
            notebook.setParent(None)
        while self._splitter.count():
            child = self._splitter.widget(0)
            child.setParent(None)
            child.deleteLater()

        used: set[int] = set()
        self._populate_splitter(self._splitter, tree, used)
        for index, notebook in enumerate(self._notebooks):
            if index not in used:
                self._splitter.addWidget(notebook)

    def _populate_splitter(self, splitter: QSplitter, node: dict, used: set[int]) -> None:
        orientation = Qt.Vertical if node.get("orientation") == "vertical" else Qt.Horizontal
        splitter.setOrientation(orientation)
        for child_node in node.get("children", []):
            if not isinstance(child_node, dict):
                continue
            if child_node.get("type") == "group":
                try:
                    index = int(child_node.get("index"))
                except (TypeError, ValueError):
                    continue
                if 0 <= index < len(self._notebooks) and index not in used:
                    splitter.addWidget(self._notebooks[index])
                    used.add(index)
            elif child_node.get("type") == "split":
                branch = self._new_splitter(Qt.Horizontal)
                self._populate_splitter(branch, child_node, used)
                if branch.count():
                    splitter.addWidget(branch)
                else:
                    branch.deleteLater()
        sizes = node.get("sizes")
        if isinstance(sizes, list) and len(sizes) == splitter.count():
            splitter.setSizes([max(1, int(size)) for size in sizes])
