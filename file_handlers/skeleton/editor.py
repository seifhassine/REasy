"""See docs/skeleton-editing.md and docs/internals/skeleton-editor.md."""
from __future__ import annotations

from functools import partial

from PySide6.QtCore import QEvent, QItemSelection, QItemSelectionModel, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QHeaderView,
    QLabel,
    QLineEdit,
    QSplitter,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

lazy from ui.editor_widgets import editor_strip, search_field, styled_label, tool_button, toolbar_separator

from .inspector import JointInspector
from .outline import JointOutline
from .preview import SkeletonPreview
from .skeleton_file import Joint
from .viewport import MODES, SkeletonTool


class SkeletonEditor(QWidget):
    modified_changed = Signal(bool)

    def __init__(self, handler, parent: QWidget | None = None):
        super().__init__(parent)
        self.document, self._kept = handler.document, []
        self.preview = SkeletonPreview(handler, self)
        self.tool = SkeletonTool(self.preview.viewport(), self.document, self)
        self.model = JointOutline(self.document, self)
        self.tree = QTreeView(self)
        self.tree.setModel(self.model)
        self.tree.setSelectionMode(QTreeView.SelectionMode.ExtendedSelection)
        self.tree.setEditTriggers(QTreeView.EditTrigger.EditKeyPressed)
        self.tree.setIndentation(12)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.filter = search_field(QLineEdit())
        self.filter.setPlaceholderText(self.tr("Filter by name"))
        self.filter.textChanged.connect(self.model.filter_names)
        self.inspector = JointInspector(self.document, self)
        outline = QWidget(self)
        column = QVBoxLayout(outline)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(self.filter)
        column.addWidget(self.tree, 1)
        left = QSplitter(Qt.Orientation.Vertical, self)
        left.addWidget(outline)
        left.addWidget(self.preview.browser)
        left.setStretchFactor(0, 1)
        left.setSizes([560, 240])
        columns = QSplitter(Qt.Orientation.Horizontal, self)
        for pane in (left, self.preview, self.inspector):
            columns.addWidget(pane)
        columns.setStretchFactor(1, 1)
        columns.setSizes([300, 800, 400])
        self.status = styled_label("", self, banner="warning")
        self.status.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._toolbar())
        layout.addWidget(self.status)
        layout.addWidget(columns, 1)
        self._clear = QTimer(self)
        self._clear.setSingleShot(True)
        self._clear.setInterval(8000)
        self._clear.timeout.connect(self._show_problems)
        self._edited = QTimer(self)
        self._edited.setSingleShot(True)
        self._edited.timeout.connect(self._refresh)
        for keys, slot, widget in ((QKeySequence.StandardKey.Undo, self.document.undo_stack.undo, self),
                                   (QKeySequence.StandardKey.Redo, self.document.undo_stack.redo, self),
                                   (QKeySequence.StandardKey.Delete, self._delete, self.tree),
                                   ("Alt+Up", self._move_up, self.tree), ("Alt+Down", self._move_down, self.tree)):
            QShortcut(keys, widget, slot).setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut if widget is self
                                                     else Qt.ShortcutContext.WidgetShortcut)
        self.document.undo_stack.cleanChanged.connect(self._clean_changed)
        self.document.changed.connect(self._edited.start)
        self.document.noted.connect(self._note)
        self.model.refused.connect(self._note)
        self.model.modelReset.connect(self._restore)
        self.tree.selectionModel().selectionChanged.connect(self._selection_changed)
        self.tree.selectionModel().currentChanged.connect(self._selection_changed)
        self.inspector.noted.connect(self._note)
        self.inspector.pick_requested.connect(self.tool.arm)
        self.tool.noted.connect(self._note)
        self.tool.picked.connect(self._picked)
        self.tool.picking.connect(self._picking)
        self.tool.delete_requested.connect(self._delete)
        self.tool.frame_requested.connect(self._frame)
        self.tool.mode_keyed.connect(self._set_mode)
        self.preview.placed.connect(self._placed)
        self.preview.shown.played.connect(self._played)
        self.preview.viewport().gizmo_mode_changed.connect(self._mode_changed)
        self.preview.viewport().set_edit_tool(self.tool)
        self._mode_changed(self.preview.viewport().gizmo_mode)
        self._played()
        self._restore()
        self._show_problems()
        self._frame()

    def event(self, event) -> bool:
        # Accepting ShortcutOverride gives the filter Ctrl+F before the window's own Find.
        if event.type() == QEvent.Type.ShortcutOverride and event.matches(QKeySequence.StandardKey.Find):
            self.filter.setFocus()
            event.accept()
            return True
        return super().event(event)

    def _toolbar(self) -> QWidget:
        strip, row = editor_strip()
        stack, tool = self.document.undo_stack, self.tool
        undo = tool_button("undo", self.tr("Undo (Ctrl+Z)"), stack.undo)
        redo = tool_button("redo", self.tr("Redo (Ctrl+Y)"), stack.redo)
        undo.setEnabled(stack.canUndo())
        redo.setEnabled(stack.canRedo())
        stack.canUndoChanged.connect(undo.setEnabled)
        stack.canRedoChanged.connect(redo.setEnabled)
        self.modes = QButtonGroup(self)
        for number, (text, tip) in enumerate((
                (self.tr("Move"), self.tr("Move the selected joints in the view (1)")),
                (self.tr("Rotate"), self.tr("Turn the selected joints in the view (2)")),
                (self.tr("Scale"), self.tr("Scale the selected joints on their own axes in the view (3)")))):
            self.modes.addButton(tool_button(f"gizmo_{MODES[number]}", tip, text=text), number)
        self.modes.idClicked.connect(self._set_mode)
        toggles = [
            (tool_button("axes", self.tr("Move and turn along the last selected joint's own axes; off: along the "
                                         "model's"), text=self.tr("Own axes")), tool, "local"),
            (tool_button("pin", self.tr("Moving or turning a joint, in the view or by typing, leaves the joints right "
                                        "under it where they stand"), text=self.tr("Children stay")),
             self.document, "keep_children"),
            (tool_button("mirror", self.tr("Moving or turning a joint, in the view or by typing, carries its mirror "
                                           "partner along, reflected across the model's centre plane"),
                         text=self.tr("Mirror")), self.document, "mirror")]
        for button in (*self.modes.buttons(), *(button for button, *_ in toggles)):
            button.setCheckable(True)
        for button, owner, option in toggles:
            button.setChecked(getattr(owner, option))
            button.toggled.connect(partial(setattr, owner, option))
            button.toggled.connect(tool.refresh)
        add = tool_button("add", self.tr("Add a joint on the selected one, at the end of the table"), self._add,
                          text=self.tr("Add"))
        self.delete = tool_button("delete", self.tr("Delete the selected joints and every joint under them (Del)"),
                                  self._delete, text=self.tr("Delete"))
        frame = tool_button("fit", self.tr("Frame the selected joints, or all of them (F)"), self._frame)
        self.rest = tool_button("reset", self.tr("Stop the motion and show the skeleton's own pose, where joints are "
                                                 "edited"), self.preview.rest, text=self.tr("Rest pose"))
        for widget in (undo, redo, toolbar_separator(), *self.modes.buttons(), toolbar_separator(),
                       *(button for button, *_ in toggles), toolbar_separator(), add, self.delete, toolbar_separator(),
                       frame, self.rest):
            row.addWidget(widget)
        row.addStretch(1)
        self.count = QLabel(strip)
        row.addWidget(self.count)
        return strip

    def selected(self) -> list[Joint]:
        chosen = {id(self.model.item(index)) for index in self.tree.selectionModel().selectedRows()}
        current = self.tree.currentIndex()
        last = id(self.model.item(current)) if current.isValid() else None
        return sorted((joint for joint in self.document.joints if id(joint) in chosen),
                      key=lambda joint: id(joint) == last)

    def select(self, joints: list[Joint]) -> None:
        self._kept = joints
        self._restore()

    def _restore(self) -> None:
        self.tree.expandAll()
        # Selecting re-reads ``_kept`` with the tree's old current joint last.
        kept, selection = self._kept, QItemSelection()
        for joint in kept:
            if (index := self.model.index_of(joint)).isValid():
                selection.select(index, index.siblingAtColumn(1))
        self.tree.selectionModel().select(selection, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        if kept and (last := self.model.index_of(kept[-1])).isValid():
            self.tree.selectionModel().setCurrentIndex(last, QItemSelectionModel.SelectionFlag.NoUpdate)
            self.tree.scrollTo(last)
        self._selection_changed()

    def _selection_changed(self, *_) -> None:
        self._kept = self.selected()
        self.delete.setEnabled(bool(self._kept))
        self.delete.setText(self.tr("Delete {0}").format(len(self.document.under(self._kept))) if self._kept
                            else self.tr("Delete"))
        self.tool.set_selection(self._kept)
        self.inspector.show_joints(self._kept)

    def _refresh(self) -> None:
        self.inspector.show_joints(self._kept)
        self.preview.follow()
        self._show_problems()

    def _picked(self, joint: Joint | None, modifiers) -> None:
        kept = [held for held in self._kept if held is not joint]
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            chosen = kept if joint is None or len(kept) < len(self._kept) else [*kept, joint]
        elif modifiers & Qt.KeyboardModifier.ShiftModifier:
            chosen = self._kept if joint is None else [*kept, joint]
        else:
            chosen = [] if joint is None else [joint]
        self.filter.clear()
        self.select(chosen)

    def _picking(self, on: bool) -> None:
        if on:
            self._note(self.tr("Click a joint in the view (Esc cancels)."))
        else:
            self._clear.stop()
            self._show_problems()

    def _set_mode(self, number: int) -> None:
        self.preview.viewport().set_gizmo_mode(MODES[number])

    def _mode_changed(self, mode: str) -> None:
        self.modes.button(MODES.index(mode)).setChecked(True)
        self.tool.set_options(mode=mode)

    def _placed(self) -> None:
        self.tool.set_place(self.preview.place)

    def _played(self) -> None:
        self.rest.setEnabled(not self.preview.resting())
        self.tool.set_options(shown=self.preview.resting())

    def _frame(self) -> None:
        self.preview.viewport().fit_points(self.tool.points(self._kept or self.document.joints))

    def _add(self) -> None:
        self.filter.clear()
        self.select([self.document.add(self._kept[-1] if self._kept else next(iter(self.document.joints), None))])

    def _delete(self) -> None:
        if self._kept:
            above = self._kept[-1].parent
            try:
                self.document.remove(self._kept)
            except ValueError as error:
                self._note(str(error))
            else:
                self.select([above] if above in self.document.joints else [])

    def _move_up(self) -> None:
        self._move(-1)

    def _move_down(self) -> None:
        self._move(1)

    def _move(self, step: int) -> None:
        try:
            self.document.move(self._kept, step)
        except ValueError as error:
            self._note(str(error))

    def _clean_changed(self, clean: bool) -> None:
        self.modified_changed.emit(not clean)

    def _note(self, text: str) -> None:
        self.status.setText(text)
        self.status.show()
        self._clear.start()

    def _show_problems(self) -> None:
        if not self._clear.isActive():
            self.status.setText("\n".join(self.document.problems()))
            self.status.setVisible(bool(self.status.text()))
        self.count.setText(self.tr("Version {0} · {1} joints").format(
            self.document.skeleton.version, len(self.document.joints)))

    def cleanup(self) -> None:
        self.preview.cleanup()
