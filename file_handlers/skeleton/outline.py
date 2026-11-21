"""See docs/internals/skeleton-editor.md, Outline."""
from __future__ import annotations

from collections import defaultdict

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget

lazy from ui.icons import line_icon
lazy from ui.styles import current_colors, qcolor

from ui.outline_model import OutlineModel

from .document import SkeletonDocument
from .skeleton_file import Joint


class JointOutline(OutlineModel):
    refused = Signal(str)

    def __init__(self, document: SkeletonDocument, parent: QWidget | None = None):
        super().__init__(document.skeleton, parent)
        self.document, self._clashing, self._filter = document, {}, ""
        self._changed()
        document.changed.connect(self._changed)

    def filter_names(self, text: str) -> None:
        self._filter = text.casefold()
        self._changed()

    def children_of(self, item) -> list:
        return self._under[id(item) if isinstance(item, Joint) else id(None)]

    def owner_of(self, item) -> object:
        return item.parent or self.document.skeleton

    def headerData(self, section: int, orientation, role: int = Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return (self.tr("Joint"), self.tr("Index"))[section]
        return None

    def display(self, item, row: int, column: int, role: int):
        if role in (Qt.DisplayRole, Qt.EditRole):
            return item.name if column == 0 else str(self._rows[id(item)])
        if role == Qt.DecorationRole and column == 0:
            return line_icon("bone")
        if role == Qt.ForegroundRole and id(item) in self._clashing:
            return qcolor(current_colors()["danger"])
        return None

    def rename(self, item, name: str | None) -> bool:
        if name is not None:
            try:
                self.document.rename(item, name)
            except ValueError as error:
                self.refused.emit(str(error))
                return False
        return True

    def _changed(self, change=None) -> None:
        if change is not None and not change.structural and change.field not in ("name", "parent"):
            return
        skeleton = self.document.skeleton
        clashing = {id(joint): joint for held in self.document.clashes() for joint in held}
        stale, self._clashing, self._rows = self._clashing | clashing, clashing, skeleton.rows()
        if change is None or change.structural or change.field == "parent":
            shown, self._under = set(), defaultdict(list)
            # A joint follows its parent in the table, so a walk backwards meets it after every joint under it.
            for joint in reversed(skeleton.joints):
                if self._filter in joint.name.casefold() or id(joint) in shown:
                    shown |= {id(joint), id(joint.parent)}
            for joint in skeleton.joints:
                if id(joint) in shown:
                    self._under[id(joint.parent)].append(joint)
            self.reset(skeleton)
        else:
            for joint in ({id(change.target): change.target} | stale).values():
                self.refresh(joint)
