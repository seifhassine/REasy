"""In-window editor groups built from REasy's detachable notebooks."""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import QApplication, QSizePolicy, QSplitter, QVBoxLayout, QWidget

from ui.detachable_tabs import CustomNotebook


class EditorGroupHost(QWidget):

    activePageChanged = Signal(object)
    layoutChanged = Signal()

    def __init__(self, primary: CustomNotebook, app_window, parent=None):
        super().__init__(parent)
        self.setObjectName("editorGroupHost")
        self.app_window = app_window
        self._active_notebook = primary
        self._splitter = self._new_splitter(Qt.Horizontal, self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._splitter)
        self._notebooks: list[CustomNotebook] = []
        self._register(primary)
        self._splitter.addWidget(primary)
        if app := QApplication.instance():
            app.focusChanged.connect(self._on_focus_changed)

    @staticmethod
    def _new_splitter(orientation, parent=None) -> QSplitter:
        splitter = QSplitter(orientation, parent)
        splitter.setObjectName("editorGroupSplitter")
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(2)
        return splitter

    def _configure_notebook(self, notebook: CustomNotebook) -> None:
        notebook.app_instance = self.app_window
        notebook._set_icon_callback = getattr(self.app_window, "_set_app_icon_callback", None)
        notebook._editor_group_host = self
        notebook.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        notebook.setMinimumSize(80, 60)

    def _connect_notebook(self, notebook: CustomNotebook) -> None:
        notebook.currentChanged.connect(
            lambda index, group=notebook: self._on_current_changed(group, index)
        )
        notebook.tabBar().tabBarClicked.connect(
            lambda _index, group=notebook: self._activate(group)
        )
        notebook.tabReattached.connect(
            lambda _page, group=notebook: self._on_tab_reattached(group)
        )
        notebook.tabBar().tabMoved.connect(lambda *_: self.layoutChanged.emit())
        notebook.installEventFilter(self)
        notebook.tabBar().installEventFilter(self)

    def _register(self, notebook: CustomNotebook, position: int | None = None) -> None:
        self._configure_notebook(notebook)
        if position is None:
            self._notebooks.append(notebook)
        else:
            self._notebooks.insert(position, notebook)
        self._connect_notebook(notebook)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.FocusIn, QEvent.MouseButtonPress):
            group = next(
                (
                    notebook
                    for notebook in self._notebooks
                    if watched in (notebook, notebook.tabBar())
                ),
                None,
            )
            if group is not None:
                self._activate(group)
        return super().eventFilter(watched, event)

    def _activate(self, notebook: CustomNotebook) -> None:
        if notebook not in self._notebooks:
            return
        self._active_notebook = notebook
        self.activePageChanged.emit(notebook.currentWidget())

    def _on_tab_reattached(self, notebook: CustomNotebook) -> None:
        self._activate(notebook)
        self.prune_empty_groups()

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

    def active_notebook(self) -> CustomNotebook:
        if self._active_notebook not in self._notebooks:
            self._active_notebook = self._notebooks[0]
        return self._active_notebook

    def active_page(self):
        return self.active_notebook().currentWidget()

    def activate_page(self, page) -> bool:
        notebook = self.notebook_for(page)
        if notebook is None:
            return False
        notebook.setCurrentWidget(page)
        self._activate(notebook)
        return True

    def count(self) -> int:
        return sum(notebook.count() for notebook in self._notebooks)

    def notebook_for(self, page) -> CustomNotebook | None:
        return next(
            (notebook for notebook in self._notebooks if notebook.indexOf(page) >= 0),
            None,
        )

    def group_index_for(self, page) -> int:
        notebook = self.notebook_for(page)
        return self._notebooks.index(notebook) if notebook is not None else 0

    def all_floating_windows(self):
        return [
            window
            for notebook in self._notebooks
            for window in notebook._floating_windows
        ]

    def ensure_group(self, index: int, orientation=Qt.Horizontal) -> CustomNotebook:
        while len(self._notebooks) <= index:
            notebook = CustomNotebook()
            self._register(notebook)
            self._append_group(notebook, orientation)
        return self._notebooks[index]

    def _append_group(self, notebook: CustomNotebook, orientation) -> None:
        root = self._splitter
        if root.count() == 0:
            root.setOrientation(orientation)
            root.addWidget(notebook)
            return
        if root.count() == 1 or root.orientation() == orientation:
            root.setOrientation(orientation)
            old_sizes = root.sizes()
            root.addWidget(notebook)
            self._share_new_space(root, root.count() - 1, old_sizes)
            return

        old_orientation = root.orientation()
        old_sizes = root.sizes()
        branch = self._new_splitter(old_orientation)
        while root.count():
            child = root.widget(0)
            child.setParent(None)
            branch.addWidget(child)
        branch.setSizes(old_sizes)
        root.setOrientation(orientation)
        root.addWidget(branch)
        root.addWidget(notebook)
        root.setSizes([1, 1])

    def split_page(self, page=None, orientation=Qt.Horizontal):
        page = page or self.active_page()
        source = self.notebook_for(page)
        if page is None or source is None:
            return None
        source_position = self._notebooks.index(source)
        target = CustomNotebook()
        self._register(target, source_position + 1)
        self._insert_adjacent(source, target, orientation)
        self._move_page(page, source, target)
        self.prune_empty_groups()
        if target in self._notebooks:
            self._activate(target)
        self.layoutChanged.emit()
        return target if target in self._notebooks else self.active_notebook()

    def _insert_adjacent(
        self,
        source: CustomNotebook,
        target: CustomNotebook,
        orientation,
    ) -> None:
        parent = source.parentWidget()
        if not isinstance(parent, QSplitter):
            self._append_group(target, orientation)
            return

        source_index = parent.indexOf(source)
        old_sizes = parent.sizes()
        if parent.count() == 1 or parent.orientation() == orientation:
            parent.setOrientation(orientation)
            parent.insertWidget(source_index + 1, target)
            self._share_new_space(parent, source_index + 1, old_sizes)
            return

        branch = self._new_splitter(orientation)
        source.setParent(None)
        parent.insertWidget(source_index, branch)
        branch.addWidget(source)
        branch.addWidget(target)
        branch.setSizes([1, 1])
        if len(old_sizes) == parent.count():
            parent.setSizes(old_sizes)

    @staticmethod
    def _share_new_space(splitter: QSplitter, new_index: int, old_sizes: list[int]) -> None:
        if len(old_sizes) + 1 != splitter.count() or not old_sizes:
            splitter.setSizes([1] * splitter.count())
            return
        old_sizes = [max(1, int(size)) for size in old_sizes]
        source_index = max(0, min(new_index - 1, len(old_sizes) - 1))
        shared = max(2, old_sizes[source_index])
        first = max(1, shared // 2)
        sizes = list(old_sizes)
        sizes[source_index] = first
        sizes.insert(new_index, max(1, shared - first))
        splitter.setSizes(sizes)

    def move_page_to_group(self, page, group_index: int, orientation=Qt.Horizontal):
        source = self.notebook_for(page)
        if source is None:
            return None
        target = self.ensure_group(max(0, int(group_index)), orientation)
        if source is not target:
            self._move_page(page, source, target)
        return target

    @staticmethod
    def _move_page(page, source: CustomNotebook, target: CustomNotebook) -> None:
        index = source.indexOf(page)
        if index < 0:
            return
        state = source._capture_state(index)
        source.removeTab(index)
        target_index = target.addTab(page, state.icon, state.title)
        target._restore_state(target_index, state)
        if file_tab := getattr(page, "parent_tab", None):
            file_tab.parent_notebook = target
        target.setCurrentIndex(target_index)
        page.show()

    def remove_page(self, page) -> bool:
        notebook = self.notebook_for(page)
        if notebook is None:
            return False
        notebook.removeTab(notebook.indexOf(page))
        self.prune_empty_groups()
        return True

    def prune_empty_groups(self) -> None:
        changed = False
        for notebook in list(self._notebooks):
            if len(self._notebooks) <= 1:
                break
            if notebook.count() or notebook._floating_windows:
                continue
            changed |= self._remove_notebook(notebook)
        if self._active_notebook not in self._notebooks:
            self._active_notebook = self._notebooks[0]
            self.activePageChanged.emit(self._active_notebook.currentWidget())
        if changed:
            self._update_primary_reference()
            self.layoutChanged.emit()

    def _remove_notebook(self, notebook: CustomNotebook) -> bool:
        if (
            notebook not in self._notebooks
            or len(self._notebooks) <= 1
            or notebook.count()
            or notebook._floating_windows
        ):
            return False
        parent = notebook.parentWidget()
        self._notebooks.remove(notebook)
        notebook.setParent(None)
        notebook.deleteLater()
        if isinstance(parent, QSplitter):
            self._collapse_single_child_splitters(parent)
        return True

    def _collapse_single_child_splitters(self, splitter: QSplitter) -> None:
        while splitter is not self._splitter and splitter.count() <= 1:
            parent = splitter.parentWidget()
            if not isinstance(parent, QSplitter):
                break
            position = parent.indexOf(splitter)
            parent_sizes = parent.sizes()
            child = splitter.widget(0) if splitter.count() else None
            if child is not None:
                child.setParent(None)
            splitter.setParent(None)
            splitter.deleteLater()
            if child is not None:
                parent.insertWidget(position, child)
            if len(parent_sizes) == parent.count():
                parent.setSizes(parent_sizes)
            splitter = parent

    def _update_primary_reference(self) -> None:
        if not self._notebooks:
            return
        primary = getattr(self.app_window, "notebook", None)
        if primary in self._notebooks:
            return
        self.app_window.notebook = self._notebooks[0]
        workspace = getattr(self.app_window, "project_workspace", None)
        sessions = getattr(workspace, "sessions", None)
        if sessions is not None:
            sessions.notebook = self._notebooks[0]

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
