"""See docs/internals/maba-format.md, Editing."""
from __future__ import annotations

from functools import partial

from PySide6.QtCore import QEvent, QItemSelection, QItemSelectionModel, QSortFilterProxyModel, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QCompleter, QDialog, QHeaderView, QLineEdit, QSplitter,
                               QStyledItemDelegate, QTableView, QTabWidget, QVBoxLayout, QWidget)

lazy from file_handlers.motion.banks import handler_reader
lazy from ui.background_jobs import BackgroundJobs
lazy from ui.editor_widgets import editor_strip, pane_header, search_field, styled_label, tool_button, \
    toolbar_separator
lazy from ui.styles import set_style_role
lazy from utils.resource_file_utils import nearby_resource_paths

from .document import MabaDocument
from .models import DatabaseModel, KeyModel, ListModel, RangeModel, RowModel
from .motions import bank_motions
from .sample_dialog import SampleDialog
from .sampling import sample


def _key(item) -> int:
    return item if type(item) is int else id(item)


class _Choices(QStyledItemDelegate):
    def __init__(self, choices, parent: QWidget):
        super().__init__(parent)
        self.choices = choices

    def createEditor(self, parent, option, index) -> QComboBox:
        box = QComboBox(parent)
        box.setEditable(True)
        box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        box.addItems(self.choices())
        box.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        box.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        return box

    def setEditorData(self, editor: QComboBox, index) -> None:
        editor.setEditText(str(index.data(Qt.ItemDataRole.EditRole)))

    def setModelData(self, editor: QComboBox, model, index) -> None:
        model.setData(index, editor.currentText())


class _Section(QWidget):
    picked = Signal()

    def __init__(self, title: str, model: ListModel, texts: tuple[str, str, str, str], parent: QWidget):
        super().__init__(parent)
        self.model, self.texts, self._kept = model, texts, []
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.table = QTableView(self)
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.buttons = [tool_button("add", self.tr("Add after the selection"), self._add),
                        tool_button("copy", self.tr("Duplicate (Ctrl+D)"), self._duplicate),
                        tool_button("delete", self.tr("Delete (Del)"), self._delete),
                        tool_button("arrow_up", self.tr("Move up (Alt+Up)"), partial(self._move, -1)),
                        tool_button("arrow_down", self.tr("Move down (Alt+Down)"), partial(self._move, 1))]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(pane_header(title, *self.buttons))
        layout.addWidget(self.table, 1)
        for keys, slot in ((QKeySequence.StandardKey.Delete, self._delete), ("Ctrl+D", self._duplicate),
                           ("Alt+Up", partial(self._move, -1)), ("Alt+Down", partial(self._move, 1))):
            QShortcut(keys, self.table, slot).setContext(Qt.ShortcutContext.WidgetShortcut)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        self.proxy.modelReset.connect(self._restore)
        self._selection_changed()

    def rows(self) -> list[int]:
        return sorted({self.proxy.mapToSource(index).row() for index in self.table.selectionModel().selectedRows()})

    def current(self):
        return self.model.items()[rows[0]] if (rows := self.rows()) else None

    def select(self, rows) -> None:
        selection, last = QItemSelection(), self.model.columnCount() - 1
        for row in rows:
            selection.select(*(self.proxy.mapFromSource(self.model.index(row, column)) for column in (0, last)))
        chosen = self.table.selectionModel()
        chosen.select(selection, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        if not selection.isEmpty():
            chosen.setCurrentIndex(selection.indexes()[0], QItemSelectionModel.SelectionFlag.NoUpdate)
            self.table.scrollTo(selection.indexes()[0])

    def _selection_changed(self, *_) -> None:
        items = self.model.items()
        self._kept = [items[row] for row in self.rows()]
        self.buttons[0].setEnabled(self.model.ready())
        for button in self.buttons[1:]:
            button.setEnabled(bool(self._kept))
        self.picked.emit()

    def _restore(self) -> None:
        kept = set(map(_key, self._kept))
        self.select([row for row, item in enumerate(self.model.items()) if _key(item) in kept])
        self._selection_changed()

    def _add(self) -> None:
        if self.model.ready():
            rows = self.rows()
            self._insert(rows[-1] + 1 if rows else len(self.model.items()), self.model.make([None]), self.texts[0])

    def _duplicate(self) -> None:
        if rows := self.rows():
            items = self.model.items()
            self._insert(rows[-1] + 1, self.model.make([items[row] for row in rows]), self.texts[1])

    def _insert(self, at: int, made: list, text: str) -> None:
        items = self.model.items()
        self.model.commit([*items[:at], *made, *items[at:]], text)
        self.select(range(at, at + len(made)))

    def _delete(self) -> None:
        if rows := self.rows():
            self.model.commit([item for row, item in enumerate(self.model.items()) if row not in rows], self.texts[2])
            if count := len(self.model.items()):
                self.select([min(rows[0], count - 1)])

    def _move(self, step: int) -> None:
        rows, order = self.rows(), list(self.model.items())
        if not rows or rows[0] + step < 0 or rows[-1] + step >= len(order):
            return
        for row in rows if step < 0 else reversed(rows):
            order[row], order[row + step] = order[row + step], order[row]
        self.model.commit(order, self.texts[3])
        self.select([row + step for row in rows])


class MabaEditor(QWidget):
    modified_changed = Signal(bool)

    def __init__(self, document: MabaDocument, handler, parent: QWidget | None = None):
        super().__init__(parent)
        self.document, self.handler, self._bank, self._paths, self._edits = document, handler, "", ("", ""), 0
        self.jobs, self.sampling = BackgroundJobs(self), BackgroundJobs(self)
        self.databases, self.keys = DatabaseModel(document, self), KeyModel(document, self)
        self.ranges, self.rows = RangeModel(document, self), RowModel(document, self)
        self.database_section = _Section(self.tr("Databases"), self.databases, (
            self.tr("Add database"), self.tr("Duplicate databases"), self.tr("Delete databases"),
            self.tr("Move databases")), self)
        self.range_section = _Section(self.tr("Motions"), self.ranges, (
            self.tr("Add motion"), self.tr("Duplicate motions"), self.tr("Delete motions"), self.tr("Move motions")),
            self)
        self.row_section = _Section(self.tr("Sampled frames"), self.rows, (
            self.tr("Add frame"), self.tr("Duplicate frames"), self.tr("Delete frames"), self.tr("Move frames")), self)
        self.key_section = _Section(self.tr("Keys"), self.keys, (
            self.tr("Add key"), self.tr("Duplicate keys"), self.tr("Delete keys"), self.tr("Move keys")), self)
        self.key_section.table.setItemDelegateForColumn(1, _Choices(
            lambda: [name for name, _class, _components in document.rules.commands.values()], self))
        self.range_section.table.setItemDelegateForColumn(2, _Choices(lambda: sorted(self.ranges.by_name), self))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._toolbar())
        layout.addWidget(self._body(), 1)
        self._clear = QTimer(self)
        self._clear.setSingleShot(True)
        self._clear.setInterval(8000)
        self._clear.timeout.connect(self.status.clear)
        for model in (self.databases, self.keys, self.ranges, self.rows):
            model.refused.connect(self._refused)
        for keys, slot in ((QKeySequence.StandardKey.Undo, document.undo_stack.undo),
                           (QKeySequence.StandardKey.Redo, document.undo_stack.redo)):
            QShortcut(keys, self, slot).setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        document.undo_stack.cleanChanged.connect(self._clean_changed)
        self.database_section.picked.connect(self._owners)
        self.range_section.picked.connect(self._owners)
        document.changed.connect(self._owners)
        document.changed.connect(self._edited)
        if document.databases:
            self.database_section.select([0])

    def event(self, event) -> bool:
        # Accepting ShortcutOverride gives the filter Ctrl+F before the window's own Find.
        if event.type() == QEvent.Type.ShortcutOverride and event.matches(QKeySequence.StandardKey.Find):
            self.filter.setFocus()
            event.accept()
            return True
        return super().event(event)

    def cleanup(self) -> None:
        self.jobs.close()
        self.sampling.close()

    def _toolbar(self) -> QWidget:
        strip, row = editor_strip()
        stack = self.document.undo_stack
        undo = tool_button("undo", self.tr("Undo (Ctrl+Z)"), stack.undo)
        redo = tool_button("redo", self.tr("Redo (Ctrl+Y)"), stack.redo)
        undo.setEnabled(stack.canUndo())
        redo.setEnabled(stack.canRedo())
        stack.canUndoChanged.connect(undo.setEnabled)
        stack.canRedoChanged.connect(redo.setEnabled)
        self.bank = QComboBox()
        self.bank.setEditable(True)
        self.bank.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.bank.setMinimumContentsLength(28)
        self.bank.lineEdit().setPlaceholderText(self.tr("Motion bank (.motbank) that names the motions"))
        self.bank.setToolTip(self.tr("The game finds a motion by BankID and MotID in the motion banks of the "
                                     "character that uses these databases."))
        self.bank.addItems(nearby_resource_paths(self.handler, "motbank", parent_levels=2, child_levels=2))
        self.bank.setCurrentIndex(-1)
        self.bank.activated.connect(self._load_bank)
        self.bank.lineEdit().returnPressed.connect(self._load_bank)
        self.bank_status = styled_label(tone="muted")
        self.sample_button = tool_button("bone", self.tr(
            "Sample key values from the motions of the picked bank on the character's skeleton"), self._sample,
            text=self.tr("Sample"))
        self.sample_button.setEnabled(False)
        self.filter = search_field(QLineEdit())
        self.filter.setPlaceholderText(self.tr("Filter databases"))
        self.filter.setFixedWidth(200)
        self.filter.textChanged.connect(self.database_section.proxy.setFilterFixedString)
        self.status = styled_label(tone="danger")
        self.status.setWordWrap(True)
        for widget in (undo, redo, toolbar_separator(), self.bank, self.bank_status, self.sample_button,
                       toolbar_separator(), self.filter):
            row.addWidget(widget)
        row.addWidget(self.status, 1)
        return strip

    def _body(self) -> QWidget:
        motions = QSplitter(Qt.Orientation.Vertical)
        motions.addWidget(self.range_section)
        motions.addWidget(self.row_section)
        motions.setStretchFactor(0, 3)
        motions.setStretchFactor(1, 1)
        self.tabs = QTabWidget()
        self.tabs.addTab(motions, self.tr("Motions"))
        self.tabs.addTab(self.key_section, self.tr("Keys"))
        body = QSplitter(Qt.Orientation.Horizontal)
        body.addWidget(self.database_section)
        body.addWidget(self.tabs)
        body.setStretchFactor(1, 1)
        body.setSizes([420, 980])
        return body

    def _owners(self, *_) -> None:
        database = self.database_section.current()
        if self.keys.owner is not database:
            self.keys.set_owner(database)
            self.ranges.set_owner(database)
        motion = self.range_section.current()
        if (None if self.rows.owner is None else self.rows.owner[1]) is not motion:
            self.rows.set_owner(None if motion is None else (database, motion))

    def _load_bank(self, *_) -> None:
        if not (path := self.bank.currentText().strip()) or path == self._bank:
            return
        self._bank = path
        self.bank_status.setText(self.tr("Reading motions…"))
        self.jobs.start(partial(bank_motions, handler_reader(self.handler), path), self._bank_loaded,
                        self._bank_failed)

    def _bank_loaded(self, motions: dict) -> None:
        self.ranges.set_motions(motions)
        self.sample_button.setEnabled(bool(motions))
        self.bank_status.setText(self.tr("{0} motions").format(len(motions)) if motions else
                                 self.tr("No motions found in this bank"))

    def _bank_failed(self, error: str) -> None:
        self.ranges.set_motions(None)
        self.sample_button.setEnabled(False)
        self.bank_status.clear()
        self._show(error, "danger")

    def _scopes(self) -> list[tuple[str, list]]:
        database, frames, picked = self.database_section.current(), self.row_section.rows(), self.key_section.rows()
        every = [] if database is None else [(database, motion, range(len(motion.frames)), range(len(database.keys)))
                                             for motion in database.ranges]
        whole = [(held, motion, range(len(motion.frames)), range(len(held.keys))) for held in self.document.databases
                 for motion in held.ranges]
        chosen = [every[row] for row in self.range_section.rows()]
        return [(self.tr("Selected frames ({0})").format(len(frames)),
                 [(*self.rows.owner, frames, range(len(self.rows.owner[0].keys)))] if frames else []),
                (self.tr("Selected motions ({0})").format(len(chosen)), chosen),
                (self.tr("Selected keys, in every motion of the database ({0})").format(len(picked)),
                 [(database, motion, rows, picked) for _database, motion, rows, _keys in every] if picked else []),
                (self.tr("Every motion of the database ({0})").format(len(every)), every),
                (self.tr("Every database ({0} motions)").format(len(whole)), whole)]

    def _kept(self, items: list) -> list[str]:
        return sorted({database.keys[index].name.split(" ")[0] for database, _motion, _rows, keys in items
                       for index in keys} - set(self.document.rules.sampled))

    def _sample(self) -> None:
        scopes = self._scopes()
        first = (2,) if self.tabs.currentIndex() == 1 else ()
        dialog = SampleDialog(scopes, next(index for index in (*first, 0, 1, 3, 4) if scopes[index][1] or index == 4),
                              self._paths, self._kept, self)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            items, self._paths = dialog.chosen(), (dialog.skeleton.text().strip(), dialog.joint_map.text().strip())
        finally:
            dialog.deleteLater()
        if not self._paths[0]:
            self._refused(self.tr("Type the character's skeleton to sample on."))
            return
        named = self.ranges.motions
        work = [(id(motion), tuple((key.name, key.size) for key in database.keys),
                 (held := named.get((motion.bank_id, motion.motion_id))) and held.list_path, motion.motion_id,
                 motion.frames, motion.values, list(rows), list(keys)) for database, motion, rows, keys in items]
        self.sample_button.setEnabled(False)
        self._show(self.tr("Sampling…"))
        self.sampling.start(partial(sample, handler_reader(self.handler), *self._paths, self.document.rules.sampled,
                                    work),
                            partial(self._sampled, [motion for _database, motion, _rows, _keys in items], self._edits),
                            self._sample_failed)

    def _sampled(self, motions: list, edits: int, found) -> None:
        self.sample_button.setEnabled(bool(self.ranges.motions))
        if self._edits != edits:
            self._show(self.tr("The file changed while sampling: sample again."), "danger")
            return
        self.document.apply_samples(motions, found.values)
        notes = [self.tr("Sampled {0} values").format(found.count)]
        if found.missing:
            notes.append(self.tr("motions missing from the bank: {0}").format(len(found.missing)))
        if found.unsampled:
            notes.append(self.tr("kept {0}").format(", ".join(sorted(found.unsampled))))
        self._show("; ".join(notes) + ".")

    def _sample_failed(self, error: str) -> None:
        self.sample_button.setEnabled(bool(self.ranges.motions))
        self._show(error, "danger")

    def _edited(self, _change) -> None:
        self._edits += 1

    def _clean_changed(self, clean: bool) -> None:
        self.modified_changed.emit(not clean)

    def _show(self, text: str, tone: str = "muted") -> None:
        self._clear.stop()
        set_style_role(self.status, tone=tone)
        self.status.setText(text)

    def _refused(self, text: str) -> None:
        self._show(text, "danger")
        self._clear.start()
