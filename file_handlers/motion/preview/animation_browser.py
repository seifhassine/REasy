from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ui.editor_widgets import (
    EDITOR_META_ROLE,
    EDITOR_TITLE_ROLE,
    EmbeddedPopupComboBox,
    EditorListItemDelegate,
)
from .entity_session import EntityMotionSession, ResolvedMotionTarget
from .resolution import PreviewMotionEntry


_MOTION_ROLE = int(Qt.ItemDataRole.UserRole)
_SEARCH_ROLE = _MOTION_ROLE + 1


class MotionEntryList(QWidget):
    """Reusable searchable list of resolved semantic motions."""

    selection_changed = Signal(int)
    entry_activated = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._entries: tuple[PreviewMotionEntry, ...] = ()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        search_row = QHBoxLayout()
        self.filter_edit = QLineEdit(self)
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setPlaceholderText(self.tr("Search animations…"))
        self.filter_edit.textChanged.connect(self._apply_filter)
        self.filter_edit.returnPressed.connect(self._focus_first_result)
        search_row.addWidget(self.filter_edit, 1)
        self.count_label = QLabel("0", self)
        self.count_label.setObjectName("motionCountLabel")
        search_row.addWidget(self.count_label)
        layout.addLayout(search_row)

        self.animation_list = QListWidget(self)
        self.animation_list.setUniformItemSizes(True)
        self.animation_list.setItemDelegate(
            EditorListItemDelegate(self.animation_list)
        )
        self.animation_list.currentItemChanged.connect(
            self._on_current_item_changed
        )
        self.animation_list.itemActivated.connect(self._on_item_activated)
        layout.addWidget(self.animation_list, 1)

    @property
    def current_index(self) -> int:
        item = self.animation_list.currentItem()
        value = item.data(_MOTION_ROLE) if item is not None else None
        return value if isinstance(value, int) else -1

    def set_entries(
        self,
        entries: Sequence[PreviewMotionEntry],
        selected_index: int = 0,
    ) -> None:
        self._entries = tuple(entries)
        with QSignalBlocker(self.animation_list):
            self.animation_list.clear()
            for index, entry in enumerate(self._entries):
                self.animation_list.addItem(self._motion_item(index, entry))
            if 0 <= selected_index < self.animation_list.count():
                self.animation_list.setCurrentRow(selected_index)
        self.count_label.setText(str(len(self._entries)))
        self._apply_filter(self.filter_edit.text())

    def _motion_item(
        self,
        motion_index: int,
        entry: PreviewMotionEntry,
    ) -> QListWidgetItem:
        name = entry.name or self.tr("(unnamed)")
        identity = self.tr("ID {id}").format(id=entry.motion_id)
        if entry.bank_id is not None:
            identity = self.tr("Bank {bank}  ·  {identity}").format(
                bank=entry.bank_id,
                identity=identity,
            )
        elif entry.slot_index >= 0:
            identity = self.tr("Slot {slot}  ·  {identity}").format(
                slot=entry.slot_index,
                identity=identity,
            )
        if entry.origin.value == "inherited":
            identity += self.tr("  ·  Inherited")

        item = QListWidgetItem(name)
        item.setData(_MOTION_ROLE, motion_index)
        item.setData(EDITOR_TITLE_ROLE, name)
        item.setData(EDITOR_META_ROLE, identity.upper())
        item.setData(
            _SEARCH_ROLE,
            " ".join(
                (
                    name,
                    "" if entry.bank_id is None else str(entry.bank_id),
                    str(entry.motion_id),
                    entry.source_list_name,
                    entry.source_path,
                    entry.origin.value,
                )
            ).casefold(),
        )
        details = [
            self.tr("{origin} · slot {slot}").format(
                origin=entry.origin.value.capitalize(),
                slot=entry.slot_index,
            ),
            entry.source_path,
        ]
        if entry.inheritance_chain:
            details.append(
                self.tr("Inherited through: {chain}").format(
                    chain=" → ".join(entry.inheritance_chain)
                )
            )
        details.append(self.tr("Double-click or press Enter to play."))
        item.setToolTip("\n".join(details))
        return item

    def _on_current_item_changed(
        self,
        _current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        self.selection_changed.emit(self.current_index)

    def _on_item_activated(self, _item: QListWidgetItem) -> None:
        if self.current_index >= 0:
            self.entry_activated.emit()

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().casefold()
        first_visible = None
        current_visible = False
        current = self.animation_list.currentItem()
        visible_count = 0
        for index in range(self.animation_list.count()):
            item = self.animation_list.item(index)
            visible = needle in str(item.data(_SEARCH_ROLE) or "")
            item.setHidden(not visible)
            if visible:
                visible_count += 1
                first_visible = first_visible or item
            current_visible |= item is current and visible
        self.count_label.setText(
            str(len(self._entries))
            if not needle
            else f"{visible_count}/{len(self._entries)}"
        )
        if not current_visible and first_visible is not None:
            self.animation_list.setCurrentItem(first_visible)

    def _focus_first_result(self) -> None:
        for index in range(self.animation_list.count()):
            item = self.animation_list.item(index)
            if item.isHidden():
                continue
            self.animation_list.setCurrentItem(item)
            self.animation_list.scrollToItem(item)
            self.animation_list.setFocus()
            return


class MotionAnimationBrowser(QWidget):
    """Animation set selector composed with the shared flat motion list."""

    selection_changed = Signal(int, int)
    animation_activated = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._targets: tuple[ResolvedMotionTarget, ...] = ()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.target_row = QHBoxLayout()
        self.target_row.setSpacing(5)
        self.target_label = QLabel(self.tr("Set"), self)
        self.target_label.setToolTip(self.tr("Animation set"))
        self.target_combo = EmbeddedPopupComboBox(self)
        self.target_combo.setSizeAdjustPolicy(
            EmbeddedPopupComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.target_combo.setMinimumContentsLength(18)
        self.target_combo.currentIndexChanged.connect(self._on_target_changed)
        self.target_row.addWidget(self.target_label)
        self.target_row.addWidget(self.target_combo, 1)
        layout.addLayout(self.target_row)

        self.entries = MotionEntryList(self)
        self.entries.selection_changed.connect(self._on_motion_changed)
        self.entries.entry_activated.connect(self.animation_activated)
        layout.addWidget(self.entries, 1)
        self.filter_edit = self.entries.filter_edit
        self.animation_list = self.entries.animation_list
        self._set_target_selector_visible(False)

    def set_session(self, session: EntityMotionSession | None) -> None:
        self._targets = session.targets if session is not None else ()
        selected = self._default_target_index(self._targets)
        with QSignalBlocker(self.target_combo):
            self.target_combo.clear()
            for index, target in enumerate(self._targets):
                self.target_combo.addItem(self._target_label(target), index)
                self.target_combo.setItemData(
                    index,
                    self._target_tooltip(target),
                    Qt.ItemDataRole.ToolTipRole,
                )
            self.target_combo.setCurrentIndex(selected)
        self._set_target_selector_visible(len(self._targets) > 1)
        self._populate_motions()

    @property
    def target_index(self) -> int:
        value = self.target_combo.currentData()
        return value if isinstance(value, int) else -1

    @property
    def motion_index(self) -> int:
        return self.entries.current_index

    def _populate_motions(self) -> None:
        target = self._current_target()
        self.entries.set_entries(target.motions if target is not None else ())

    def _current_target(self) -> ResolvedMotionTarget | None:
        index = self.target_index
        return self._targets[index] if 0 <= index < len(self._targets) else None

    def _on_target_changed(self, _index: int) -> None:
        self._populate_motions()
        self._emit_selection()

    def _on_motion_changed(self, _motion_index: int) -> None:
        self._emit_selection()

    def _emit_selection(self) -> None:
        self.selection_changed.emit(self.target_index, self.motion_index)

    def _target_label(self, target: ResolvedMotionTarget) -> str:
        definition = target.definition
        name = definition.name or self.tr("Motion target")
        return self.tr("{name} · Motion #{component}").format(
            name=name,
            component=definition.id.component_instance_id,
        )

    def _target_tooltip(self, target: ResolvedMotionTarget) -> str:
        definition = target.definition
        state = self.tr("Enabled") if definition.enabled else self.tr("Disabled")
        bank_path = target.motion_bank_path or definition.motion_bank_path
        result = self.tr(
            "{state} · {count} animations · {layers} runtime layers"
        ).format(
            state=state,
            count=len(target.motions),
            layers=len(definition.layers),
        )
        return f"{result}\n{bank_path}" if bank_path else result

    def _set_target_selector_visible(self, visible: bool) -> None:
        self.target_label.setVisible(visible)
        self.target_combo.setVisible(visible)

    @staticmethod
    def _default_target_index(
        targets: tuple[ResolvedMotionTarget, ...],
    ) -> int:
        for predicate in (
            lambda target: target.definition.enabled and bool(target.motions),
            lambda target: bool(target.motions),
            lambda target: target.definition.enabled,
        ):
            match = next(
                (
                    index
                    for index, target in enumerate(targets)
                    if predicate(target)
                ),
                None,
            )
            if match is not None:
                return match
        return 0 if targets else -1
