"""See docs/internals/skeleton-format.md, Editing."""
from __future__ import annotations

import re
from collections import defaultdict

from PySide6.QtCore import QEvent, QItemSelection, QItemSelectionModel, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QHeaderView, QLineEdit, QSplitter, QTreeView, QVBoxLayout, QWidget

lazy import numpy as np

lazy from ui.editor_widgets import editor_strip, search_field, styled_label, tool_button, toolbar_separator
lazy from ui.icons import line_icon
lazy from ui.styles import current_colors, qcolor
lazy from utils.number_format import format_float_sequence

from ui.field_form import Field, FieldPanel
from ui.outline_model import OutlineModel

from .document import SkeletonDocument
from .skeleton_file import Joint

_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


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


class SkeletonEditor(QWidget):
    modified_changed = Signal(bool)

    def __init__(self, document: SkeletonDocument, parent: QWidget | None = None):
        super().__init__(parent)
        self.document, self._kept = document, []
        self.model = JointOutline(document, self)
        self.tree = QTreeView(self)
        self.tree.setModel(self.model)
        self.tree.setSelectionMode(QTreeView.SelectionMode.ExtendedSelection)
        self.tree.setEditTriggers(QTreeView.EditTrigger.EditKeyPressed)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.panel = FieldPanel(self)
        splitter = QSplitter(self)
        splitter.addWidget(self.tree)
        splitter.addWidget(self.panel)
        splitter.setStretchFactor(0, 1)
        splitter.setSizes([600, 480])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._toolbar())
        layout.addWidget(splitter, 1)
        self._clear = QTimer(self)
        self._clear.setSingleShot(True)
        self._clear.setInterval(8000)
        self._clear.timeout.connect(self._show_problems)
        for keys, slot, widget in ((QKeySequence.StandardKey.Undo, document.undo_stack.undo, self),
                                   (QKeySequence.StandardKey.Redo, document.undo_stack.redo, self),
                                   (QKeySequence.StandardKey.Delete, self._delete, self.tree),
                                   ("Alt+Up", self._move_up, self.tree), ("Alt+Down", self._move_down, self.tree)):
            QShortcut(keys, widget, slot).setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut if widget is self
                                                     else Qt.ShortcutContext.WidgetShortcut)
        document.undo_stack.cleanChanged.connect(self._clean_changed)
        document.changed.connect(self._show)
        document.changed.connect(self._show_problems)
        self.model.refused.connect(self._refused)
        self.tree.selectionModel().selectionChanged.connect(self._selection_changed)
        self.model.modelReset.connect(self._restore)
        self._restore()
        self._show_problems()

    def event(self, event) -> bool:
        # Accepting ShortcutOverride gives the filter Ctrl+F before the window's own Find.
        if event.type() == QEvent.Type.ShortcutOverride and event.matches(QKeySequence.StandardKey.Find):
            self.filter.setFocus()
            event.accept()
            return True
        return super().event(event)

    def _toolbar(self) -> QWidget:
        strip, row = editor_strip()
        stack = self.document.undo_stack
        undo = tool_button("undo", self.tr("Undo (Ctrl+Z)"), stack.undo)
        redo = tool_button("redo", self.tr("Redo (Ctrl+Y)"), stack.redo)
        undo.setEnabled(stack.canUndo())
        redo.setEnabled(stack.canRedo())
        stack.canUndoChanged.connect(undo.setEnabled)
        stack.canRedoChanged.connect(redo.setEnabled)
        add = tool_button("add", self.tr("Add a joint under the selected one, at the end of the table"), self._add,
                          text=self.tr("Add"))
        self.delete = tool_button("delete", self.tr("Delete the selected joints and every joint under them (Del)"),
                                  self._delete, text=self.tr("Delete"))
        up = tool_button("arrow_up", self.tr("Move up in the table (Alt+Up)"), self._move_up)
        down = tool_button("arrow_down", self.tr("Move down in the table (Alt+Down)"), self._move_down)
        self.selected_only = self.delete, up, down
        self.filter = search_field(QLineEdit())
        self.filter.setPlaceholderText(self.tr("Filter by name"))
        self.filter.setFixedWidth(240)
        self.filter.textChanged.connect(self.model.filter_names)
        self.status = styled_label(tone="danger")
        for widget in (undo, redo, toolbar_separator(), add, self.delete, toolbar_separator(), up, down,
                       toolbar_separator(), self.filter):
            row.addWidget(widget)
        row.addWidget(self.status, 1)
        return strip

    def selected(self) -> list[Joint]:
        chosen = {id(self.model.item(index)) for index in self.tree.selectionModel().selectedRows()}
        return [joint for joint in self.document.joints if id(joint) in chosen]

    def select(self, joints: list[Joint]) -> None:
        self._kept = joints
        self._restore()

    def _restore(self) -> None:
        self.tree.expandAll()
        selection = QItemSelection()
        for joint in self._kept:
            if (index := self.model.index_of(joint)).isValid():
                selection.select(index, index.siblingAtColumn(1))
        self.tree.selectionModel().select(selection, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        if not selection.isEmpty():
            self.tree.selectionModel().setCurrentIndex(selection.indexes()[0],
                                                       QItemSelectionModel.SelectionFlag.NoUpdate)
            self.tree.scrollTo(selection.indexes()[0])
        self._selection_changed()

    def _selection_changed(self, *_) -> None:
        self._kept = self.selected()
        for button in self.selected_only:
            button.setEnabled(bool(self._kept))
        self.delete.setText(self.tr("Delete {0}").format(len(self.document.under(self._kept))) if self._kept
                            else self.tr("Delete"))
        self._show()

    def _show(self, _change=None) -> None:
        if self._kept:
            self.panel.show_fields(id(self._kept[0]), self._kept[0].name, self._fields(self._kept[0]))
        else:
            self.panel.show_fields(None, "", [])

    def _fields(self, joint: Joint) -> list[Field]:
        document, joints = self.document, self.document.joints

        def typed(label: str, field: str, undo: str, tooltip: str = "") -> Field:
            # A 32-bit value shows as the shortest decimal that gives it back.
            return Field(label, "text", lambda: joint.name if field == "name" else format_float_sequence(
                float(str(np.float32(value))) for value in getattr(joint, field)),
                lambda text: self._typed(joint, field, undo, text), tooltip=tooltip)
        fields = [typed(self.tr("Name"), "name", ""),
                  Field(self.tr("Name hash"), "readonly", lambda: f"0x{joint.name_hash:08X}")]
        if joint.parent is not None:
            fields.append(Field(self.tr("Parent"), "choice", lambda: joint.parent,
                                lambda parent: document.set_values(joint, self.tr("Set parent"), parent=parent),
                                {held: held.name for held in joints[:joints.index(joint)]},
                                self.tr("A joint earlier in the table.")))
        fields += [Field(self.tr("Mirror"), "choice", lambda: joint.mirror or "",
                         lambda mirror: document.set_values(joint, self.tr("Set mirror"), mirror=mirror or None),
                         {"": self.tr("None")} | {held: held.name for held in joints},
                         self.tr("The joint a mirrored motion swaps this one with: itself on the centre line, None "
                                 "for a joint a mirrored motion leaves alone.")),
                   typed(self.tr("Position"), "position", self.tr("Set position"), self.tr("x, y, z in metres")),
                   typed(self.tr("Rotation"), "rotation", self.tr("Set rotation"),
                         self.tr("A quaternion as x, y, z, w; it is kept at length 1.")),
                   typed(self.tr("Scale"), "scale", self.tr("Set scale"), "x, y, z")]
        if document.skeleton.segment_scales:
            fields.append(Field(self.tr("Segment scale"), "bool", lambda: joint.segment_scale,
                                lambda on: document.set_values(joint, self.tr("Set segment scale"), segment_scale=on),
                                tooltip=self.tr("On: the joint leaves out its parent's scale, as Maya's Segment "
                                                "Scale Compensate does.")))
        return fields

    def _typed(self, joint: Joint, field: str, undo: str, text: str) -> None:
        try:
            if field == "name":
                self.document.rename(joint, text)
            else:
                parts, count = text.replace(",", " ").split(), len(getattr(joint, field))
                if len(parts) != count or not all(map(_NUMBER.fullmatch, parts)):
                    raise ValueError(self.tr("Type {0} numbers, separated by commas.").format(count))
                self.document.set_values(joint, undo, **{field: [float(part) for part in parts]})
        except ValueError as error:
            self._refused(str(error))
            raise

    def _add(self) -> None:
        self.filter.clear()
        self.select([self.document.add(self._kept[0] if self._kept else next(iter(self.document.joints), None))])

    def _delete(self) -> None:
        if self._kept:
            above = self._kept[0].parent
            try:
                self.document.remove(self._kept)
            except ValueError as error:
                self._refused(str(error))
            else:
                self.select([above] if above is not None else [])

    def _move_up(self) -> None:
        self._move(-1)

    def _move_down(self) -> None:
        self._move(1)

    def _move(self, step: int) -> None:
        try:
            self.document.move(self._kept, step)
        except ValueError as error:
            self._refused(str(error))

    def _clean_changed(self, clean: bool) -> None:
        self.modified_changed.emit(not clean)

    def _refused(self, text: str) -> None:
        self.status.setText(text)
        self._clear.start()

    def _show_problems(self, _change=None) -> None:
        if not self._clear.isActive():
            self.status.setText("\n".join(self.document.problems()))
