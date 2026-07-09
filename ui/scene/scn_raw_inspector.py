from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QTimer, Signal
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

from file_handlers.rsz.rsz_array_operations import RszArrayOperations
from file_handlers.rsz.rsz_handler import RszViewer
from file_handlers.rsz.rsz_lazy_loading import RszLazyNodeBuilder
from file_handlers.rsz.rsz_object_operations import RszObjectOperations
from file_handlers.rsz.utils.rsz_name_helper import RszViewerNameHelper
from ui.styles import get_color_scheme, get_tree_stylesheet


class ScnRawInspector(QFrame):
    document_modified = Signal(str)

    def __init__(self, parent, app, document_store):
        super().__init__(parent)
        self.app = app
        self.document_store = document_store
        self.viewer = None
        self.document_id = ""
        self._instance_indexes: dict[int, QPersistentModelIndex] = {}
        self.setObjectName("rawScenePreview")
        self.setMinimumHeight(260)
        self.setMaximumHeight(520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        self.title = QLabel("Raw SCN", self)
        layout.addWidget(self.title)

        self.body = QWidget(self)
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(0)
        layout.addWidget(self.body, 1)
        self.clear("Select a mesh, foliage, or light probe object to edit its raw SCN node here.")

    def set_record(self, document, scene_object, renderable) -> None:
        instance_id = scene_object.instance_id if scene_object else renderable.source_component_id.instance_id
        if (self.viewer is None or self.document_id != document.document_id) and not self._mount(document):
            return
        raw_name = Path((document.source_path or document.document_id).replace("\\", "/")).name
        self.title.setText(f"Raw SCN | {raw_name} | ID {instance_id}")
        QTimer.singleShot(0, self, lambda iid=instance_id: select_instance_in_tree(getattr(self.viewer, "tree", None), iid, self._instance_indexes))

    def clear(self, text: str = "Select a mesh, foliage, or light probe object to edit its raw SCN node here.") -> None:
        self._reset()
        self.title.setText("Raw SCN")
        hint = QLabel(text, self.body)
        hint.setWordWrap(True)
        self.body_layout.addWidget(hint)

    def cleanup(self) -> None:
        self._reset()

    def _mount(self, document) -> bool:
        self._reset()
        store_doc = self.document_store.get(document.document_id)
        handler = getattr(store_doc, "handler", None)
        if handler is None:
            self.clear("Raw SCN document is not loaded in the scene document store.")
            return False
        viewer = RszViewer(self.body)
        viewer.scn = document.rsz_file
        viewer.handler = handler
        viewer.type_registry = getattr(document.rsz_file, "type_registry", None) or getattr(handler, "type_registry", None)
        viewer.dark_mode = getattr(self.app, "dark_mode", False)
        viewer.game_version = getattr(document.rsz_file, "game_version", "") or getattr(handler, "game_version", "")
        viewer.show_advanced = False
        viewer.tree.setStyleSheet(get_tree_stylesheet(get_color_scheme(viewer.dark_mode)))
        viewer.name_helper = RszViewerNameHelper(viewer.scn, viewer.type_registry)
        viewer.array_operations = RszArrayOperations(viewer)
        viewer.object_operations = RszObjectOperations(viewer)
        viewer.lazy_builder = RszLazyNodeBuilder(viewer)
        viewer._configure_scene_actions = lambda: None
        viewer.modified_changed.connect(lambda changed, doc_id=document.document_id: self._on_viewer_modified(doc_id, changed))
        viewer.populate_tree()
        handler._scene_raw_viewer = viewer
        self.body_layout.addWidget(viewer)
        self.viewer = viewer
        self.document_id = document.document_id
        return True

    def _reset(self) -> None:
        self.document_id = ""
        self.viewer = None
        self._instance_indexes.clear()
        self._clear_layout()

    def _on_viewer_modified(self, document_id: str, changed: bool) -> None:
        if changed:
            self.document_modified.emit(document_id)

    def _clear_layout(self) -> None:
        while self.body_layout.count():
            item = self.body_layout.takeAt(0)
            if widget := item.widget():
                handler = getattr(widget, "handler", None)
                if getattr(handler, "_scene_raw_viewer", None) is widget:
                    handler._scene_raw_viewer = None
                cleanup = getattr(widget, "cleanup", None)
                if callable(cleanup):
                    cleanup()
                widget.deleteLater()


def select_instance_in_tree(tree, instance_id: int, cache: dict[int, QPersistentModelIndex] | None = None) -> None:
    model = tree.model() if tree is not None else None
    index = QModelIndex(cache.get(instance_id)) if cache and cache.get(instance_id) else QModelIndex()
    if model is None or not (index.isValid() or (index := _find_instance_index(model, instance_id)).isValid()):
        return
    if cache is not None:
        cache[instance_id] = QPersistentModelIndex(index)
    parent = index.parent()
    while parent.isValid():
        tree.expand(parent)
        parent = parent.parent()
    tree.setCurrentIndex(index)
    tree.scrollTo(index)


def _find_instance_index(model, instance_id: int, parent=QModelIndex()) -> QModelIndex:
    needle = f"(ID: {instance_id})"
    parent_item = model.rootItem if not parent.isValid() else parent.internalPointer()
    if getattr(parent_item, "_deferred_builder", None) and not getattr(parent_item, "_children_built", False):
        return QModelIndex()
    for row in range(len(getattr(parent_item, "_raw_children", []) or [])):
        index = model.index(row, 0, parent)
        item = index.internalPointer()
        raw = getattr(item, "raw", {}) or {}
        if raw.get("instance_id") == instance_id or needle in str(model.data(index) or ""):
            return index
        if (hit := _find_instance_index(model, instance_id, index)).isValid():
            return hit
    return QModelIndex()
