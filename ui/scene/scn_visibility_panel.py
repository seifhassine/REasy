from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Iterable, Mapping

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from file_handlers.rsz.scn_scene_graph import ScnSceneGraph
from ui.editor_widgets import (
    EDITOR_META_ROLE,
    EDITOR_TITLE_ROLE,
    EditorListItemDelegate,
)


_KEYS_ROLE = int(Qt.ItemDataRole.UserRole)
_SEARCH_ROLE = _KEYS_ROLE + 1


class ScnGameObjectVisibilityPanel(QWidget):
    """Flat GameObject visibility controls over a shared SCN/PFB scene."""

    visibility_changed = Signal(object)
    focus_keys_changed = Signal(object)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._authored_visibility: dict[str, bool] = {}
        self._runtime_overrides: dict[str, bool] = {}
        self._user_overrides: dict[str, bool] = {}
        self._related_keys: dict[str, set[str]] = {}
        self._animated_keys: set[str] = set()
        self._items: list[QListWidgetItem] = []
        self._updating = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)

        controls = QHBoxLayout()
        self.animated_only = QCheckBox(
            self.tr("Animation focus")
        )
        self.animated_only.setToolTip(
            self.tr(
                "Temporarily hide objects unrelated to the selected animation."
            )
        )
        self.animated_only.setEnabled(False)
        self.animated_only.toggled.connect(self._emit_focus_keys)
        controls.addWidget(self.animated_only, 1)

        self.reset_button = QPushButton(self.tr("Reset"))
        self.reset_button.setToolTip(
            self.tr("Restore visibility from the PFB's authored settings.")
        )
        self.reset_button.setEnabled(False)
        self.reset_button.clicked.connect(self.reset_to_authored)
        controls.addWidget(self.reset_button)
        layout.addLayout(controls)

        self.focus_status = QLabel()
        self.focus_status.setWordWrap(True)
        self.focus_status.hide()
        layout.addWidget(self.focus_status)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText(self.tr("Filter objects…"))
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)
        layout.addWidget(self.filter_edit)

        self.object_list = QListWidget()
        self.object_list.setUniformItemSizes(True)
        self.object_list.setItemDelegate(
            EditorListItemDelegate(self.object_list)
        )
        self.object_list.itemChanged.connect(self._visibility_edited)
        layout.addWidget(self.object_list, 1)

    @property
    def user_overrides(self) -> dict[str, bool]:
        return dict(self._user_overrides)

    def resolve_visibility(self, key: str, default: bool) -> bool:
        return self._user_overrides.get(key, default)

    def set_graphs(self, graphs: Iterable[ScnSceneGraph]) -> None:
        graphs = tuple(graphs)
        animated_keys = set(self._animated_keys)
        self._authored_visibility = {
            renderable.key: renderable.visible_by_default
            for graph in graphs
            for renderable in getattr(graph, "renderables", ())
        }
        valid_keys = set(self._authored_visibility)
        self._user_overrides = {
            key: value
            for key, value in self._user_overrides.items()
            if key in valid_keys
        }
        self._runtime_overrides = {
            key: value
            for key, value in self._runtime_overrides.items()
            if key in valid_keys
        }
        self._related_keys.clear()
        self._items.clear()
        self._updating = True
        try:
            self.object_list.clear()
            for graph in graphs:
                self._add_graph(graph)
            self._refresh_check_states()
        finally:
            self._updating = False
        self._apply_filter(self.filter_edit.text())
        self.set_animated_keys(animated_keys, force=True)

    def set_animated_keys(
        self,
        keys: Iterable[str],
        *,
        force: bool = False,
    ) -> None:
        expanded = set()
        for key in keys:
            expanded.update(self._related_keys.get(key, (key,)))
        animated_keys = expanded & set(self._authored_visibility)
        changed = animated_keys != self._animated_keys
        self._animated_keys = animated_keys
        self.animated_only.setEnabled(bool(self._animated_keys))
        if changed or force:
            self._emit_focus_keys()

    def set_runtime_visibility_overrides(
        self,
        overrides: Mapping[str, bool],
    ) -> None:
        normalized = {
            str(key): bool(value)
            for key, value in overrides.items()
            if key in self._authored_visibility
        }
        if normalized == self._runtime_overrides:
            return
        self._runtime_overrides = normalized
        self._refresh_check_states()

    def reset_to_authored(self) -> None:
        if not self._user_overrides:
            return
        self._user_overrides.clear()
        self._refresh_check_states()
        self.visibility_changed.emit({})

    def _add_graph(self, graph: ScnSceneGraph) -> None:
        if not hasattr(graph, "documents"):
            return
        instances = tuple(
            (instance.instance_id, instance.document_id)
            for instance in graph.document_instances.values()
        ) or ((graph.root_instance_id, graph.root_document_id),)

        for instance_id, document_id in instances:
            document = graph.documents.get(document_id)
            if document is None:
                continue
            direct_keys = defaultdict(set)
            for renderable in graph.renderables:
                if renderable.document_instance_id == instance_id:
                    direct_keys[renderable.source_object_id].add(renderable.key)

            children = defaultdict(list)
            for scene_object in document.objects.values():
                parent = document.object_by_local_id.get(scene_object.parent_id)
                if parent == scene_object.id:
                    parent = None
                children[parent].append(scene_object)

            source_name = Path(
                (document.source_path or document_id).replace("\\", "/")
            ).name
            for scene_object in children.get(None, ()):
                _keys, rows = self._object_rows(
                    scene_object,
                    children,
                    direct_keys,
                    (),
                    set(),
                )
                for label, keys in rows:
                    self._add_item(label, keys, source_name)

    def _object_rows(
        self,
        scene_object,
        children,
        direct_keys,
        parent_names: tuple[str, ...],
        ancestors: set,
    ) -> tuple[set[str], list[tuple[str, set[str]]]]:
        if scene_object.id in ancestors:
            return set(), []
        name = (
            scene_object.name
            or scene_object.type_name
            or self.tr("GameObject {id}").format(
                id=scene_object.id.local_object_id
            )
        )
        path = (*parent_names, name)
        direct = set(direct_keys.get(scene_object.id, ()))
        subtree = set(direct)
        child_rows = []
        for child in children.get(scene_object.id, ()):
            child_keys, rows = self._object_rows(
                child,
                children,
                direct_keys,
                path,
                ancestors | {scene_object.id},
            )
            subtree.update(child_keys)
            child_rows.extend(rows)
        for key in direct:
            self._related_keys.setdefault(key, set()).update(subtree)
        rows = (
            [(" › ".join(path), subtree), *child_rows]
            if subtree
            else child_rows
        )
        return subtree, rows

    def _add_item(
        self,
        label: str,
        keys: set[str],
        source_name: str,
    ) -> None:
        parts = label.split(" › ")
        title = parts[-1]
        context = " › ".join(parts[:-1]) or source_name or self.tr("PFB")
        item = QListWidgetItem(title)
        item.setData(_KEYS_ROLE, tuple(keys))
        item.setData(_SEARCH_ROLE, f"{label} {source_name}".casefold())
        item.setData(EDITOR_TITLE_ROLE, title)
        item.setData(EDITOR_META_ROLE, context)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setToolTip(
            self.tr(
                "{path}\n{source}\nInitial visibility comes from the PFB's "
                "authored settings."
            ).format(path=label, source=source_name or self.tr("PFB"))
        )
        self._items.append(item)
        self.object_list.addItem(item)

    def _visibility_edited(self, item: QListWidgetItem) -> None:
        if self._updating:
            return
        keys = item.data(_KEYS_ROLE) or ()
        visible = item.checkState() == Qt.CheckState.Checked
        for key in keys:
            baseline = self._runtime_overrides.get(
                key,
                self._authored_visibility.get(key, True),
            )
            if baseline == visible:
                self._user_overrides.pop(key, None)
            else:
                self._user_overrides[key] = visible
        self._refresh_check_states()
        self.visibility_changed.emit(dict(self._user_overrides))

    def _refresh_check_states(self) -> None:
        self.reset_button.setEnabled(bool(self._user_overrides))
        self._updating = True
        try:
            for item in self._items:
                states = {
                    self._user_overrides.get(
                        key,
                        self._runtime_overrides.get(
                            key,
                            self._authored_visibility.get(key, True),
                        ),
                    )
                    for key in (item.data(_KEYS_ROLE) or ())
                }
                state = (
                    Qt.CheckState.Checked
                    if states == {True}
                    else Qt.CheckState.Unchecked
                    if states == {False}
                    else Qt.CheckState.PartiallyChecked
                )
                item.setCheckState(state)
        finally:
            self._updating = False
        self._update_focus_status()

    def _emit_focus_keys(self, _checked: bool | None = None) -> None:
        keys = (
            set(self._animated_keys)
            if self.animated_only.isChecked() and self._animated_keys
            else None
        )
        self.focus_keys_changed.emit(keys)
        self._update_focus_status()

    def _update_focus_status(self) -> None:
        if not self.animated_only.isChecked():
            self.focus_status.hide()
            return
        if not self._animated_keys:
            self.focus_status.setText(
                self.tr("Select an animation to establish a focus scope.")
            )
            self.focus_status.show()
            return
        visible = {
            key
            for key, authored in self._authored_visibility.items()
            if self._user_overrides.get(
                key,
                self._runtime_overrides.get(key, authored),
            )
        }
        hidden_count = len(visible - self._animated_keys)
        self.focus_status.setText(
            self.tr(
                "Animation focus hides {count} otherwise visible objects."
            ).format(count=hidden_count)
            if hidden_count
            else self.tr("All visible objects are related to this animation.")
        )
        self.focus_status.show()

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().casefold()
        for item in self._items:
            item.setHidden(needle not in str(item.data(_SEARCH_ROLE) or ""))
