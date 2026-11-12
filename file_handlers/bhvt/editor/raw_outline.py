"""See docs/internals/bhvt-editor.md, Raw view."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem, QTreeWidgetItemIterator, QWidget

from ..document import BhvtDocument
from ..edits import section_blank, zero
from ..model import Hfsm, MotionFsm, MotionFsmV1, TimelineFsm, Tree
from ..rules import motion_fsm_rules, motion_fsm_v1_rules, rules_of

ROLE = Qt.ItemDataRole.UserRole
_TABLES = {"game_objects", "paths", "conditions"}


def page_key(page: tuple) -> tuple:
    return tuple(part if isinstance(part, str) else id(part) for part in page[:3])


class Outline(QTreeWidget):
    def __init__(self, document: BhvtDocument, parent: QWidget | None = None):
        super().__init__(parent)
        self.document = document
        self.pages: list[tuple] = []
        self.setHeaderHidden(True)
        self.rebuild()

    def page(self, item: QTreeWidgetItem | None) -> tuple | None:
        index = None if item is None else item.data(0, ROLE)
        return None if index is None else self.pages[index]

    def current_page(self) -> tuple | None:
        return self.page(self.currentItem())

    def select_page(self, page: tuple) -> None:
        key, items = page_key(page), QTreeWidgetItemIterator(self)
        while (item := items.value()) is not None:
            if (found := self.page(item)) is not None and page_key(found) == key:
                self.setCurrentItem(item)
                return
            items += 1

    def rebuild(self) -> None:
        current = self.current_page()
        self.blockSignals(True)
        self.clear()
        self.pages.clear()
        model = self.document.resource.model
        if isinstance(model, (Tree, Hfsm)):
            self._parts(self, model)
        elif isinstance(model, MotionFsm):
            self._motion(model)
        elif isinstance(model, TimelineFsm):
            self._timelines(model)
        else:
            self._motion_v1(model)
        self.expandToDepth(0)
        self.blockSignals(False)
        if current is not None:
            self.select_page(current)
            if self.currentItem() is None:
                self.setCurrentItem(self.topLevelItem(0))

    def _add(self, parent, text: str, page: tuple | None = None) -> QTreeWidgetItem:
        item = QTreeWidgetItem(parent, [text])
        if page is not None:
            item.setData(0, ROLE, len(self.pages))
            self.pages.append(page)
        return item

    def _parts(self, parent, model: Tree | Hfsm) -> None:
        self._add(parent, "header", ("record", model.header))
        for section in rules_of(model).sections:
            self._section(parent, model, section)
        self._add(parent, "variable_references", ("variables", model))

    def _section(self, parent, model: Tree | Hfsm, section) -> None:
        name, kind = section.name, section.kind
        if kind == "nodes":
            group = self._add(parent, name)
            by_key = {(node.id, node.ex_id): node for node in model.nodes}
            children = {key for node in model.nodes for key in zip(node.children, node.children_ex)}
            for node in model.nodes:
                if (node.id, node.ex_id) not in children:
                    self._node(group, model, node, by_key)
            for field in ("action_ex_ids", "static_action_ex_ids"):
                self._add(parent, field, ("table", model, field, 0))
        elif kind == "states":
            self._state(self._add(parent, name), model, model.root)
        elif kind == "rsz":
            self._add(parent, name, ("rsz", model.blocks[name]))
        elif kind in _TABLES:
            self._add(parent, name, ("table", model, name, section_blank(model, kind)))
        elif kind == "uvar":
            self._add(parent, name, ("uvar", model.variable))
        elif kind == "uvars":
            group = self._add(parent, name)
            for index, uvar in enumerate(model.base_variables):
                self._add(group, uvar.header.name or str(index), ("uvar", uvar))

    def _node(self, parent, tree: Tree, node, by_key: dict) -> None:
        item = self._add(parent, node.name, ("node", tree, node))
        for key in zip(node.children, node.children_ex):
            self._node(item, tree, by_key[key], by_key)

    def _state(self, parent, hfsm: Hfsm, state) -> None:
        item = self._add(parent, state.name, ("node", hfsm, state))
        for child in state.children:
            self._state(item, hfsm, child)

    def _motion(self, fsm: MotionFsm) -> None:
        self._add(self, "header", ("record", fsm.header))
        self._parts(self._add(self, "tree"), fsm.tree)
        rules = motion_fsm_rules(fsm.version)
        tables = {"transition_maps": rules.transition_map, "transition_data": rules.transition_data}
        if "transition_extra_table" in rules.header.fields:
            tables["transition_extras"] = rules.transition_data
        for field, codec in tables.items():
            self._add(self, field, ("table", fsm, field, zero(codec)))

    def _timelines(self, fsm: TimelineFsm) -> None:
        for entry in fsm.trees:
            self._parts(self._add(self, entry.name, ("record", entry)), entry.tree)
        group = self._add(self, "timelines")
        for entry in fsm.timelines:
            self._add(self._add(group, entry.name, ("record", entry)), "timeline", ("timeline", entry.timeline))
        self._add(self, "hold_points", ("table", fsm, "hold_points", {"hold_point_id": bytes(16), "targets": []}))

    def _motion_v1(self, fsm: MotionFsmV1) -> None:
        blank = zero(motion_fsm_v1_rules(fsm.version).transition_data)
        for index, entry in enumerate(fsm.fsms):
            item = self._add(self, self.tr("FSM {0}").format(index), ("record", entry.info))
            self._parts(item, entry.hfsm)
            self._add(item, "transitions", ("table", entry, "transitions", blank))
