"""See docs/internals/jcns-editor.md."""

from __future__ import annotations

import math
from collections import Counter

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import QCheckBox, QLineEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

lazy from ui.editor_widgets import editor_strip, search_field, styled_label
lazy from ui.icons import line_icon
lazy from ui.styles import set_style_role
lazy from utils.hash_util import murmur3_hash_utf16le

from ui.field_form import Field, FieldPanel

from ..derived import rows
from ..edits import NO_CONE, EditRefused, JointEdits
from ..joints import Card, JointView
from ..model import Record
from ..rules import joint_view

ICONS = {"joint": "bone", "channel": "layers", "material": "palette", "property": "component", "value": "variable"}


def _label(name: str) -> str:
    return name.replace("_", " ").capitalize()


class JointTree(QWidget):
    chosen = Signal(object)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.view: JointView | None = None
        self.rig, self.names = None, {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        bar, row = editor_strip()
        self.filter = search_field(QLineEdit())
        self.filter.setPlaceholderText(self.tr("Filter"))
        self.filter.textChanged.connect(self._fill)
        self.constrained = QCheckBox(self.tr("Constrained only"))
        self.constrained.toggled.connect(self._fill)
        row.addWidget(self.filter, 1)
        row.addWidget(self.constrained)
        layout.addWidget(bar)
        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels([self.tr("Target"), self.tr("Constraints")])
        self.tree.currentItemChanged.connect(lambda item, _old: self.chosen.emit(self.target_of(item)))
        layout.addWidget(self.tree, 1)

    @staticmethod
    def target_of(item: QTreeWidgetItem | None) -> tuple | None:
        return None if item is None else item.data(0, Qt.UserRole)

    def show_view(self, view: JointView, rig, names: dict[int, str]) -> None:
        self.view, self.rig, self.names = view, rig, names
        self._fill()

    def select(self, target: tuple | None) -> None:
        found = next((item for item in self._items() if self.target_of(item) == target), None)
        self.tree.setCurrentItem(found)

    def _items(self):
        stack = [self.tree.topLevelItem(at) for at in range(self.tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            yield item
            stack += [item.child(at) for at in range(item.childCount())]

    def _fill(self) -> None:
        if self.view is None:
            return
        current, view = self.target_of(self.tree.currentItem()), self.view
        text, only = self.filter.text().casefold(), self.constrained.isChecked()
        titles = {kind: spec["title"] for kind, spec in view.data["cards"].items()}

        def shown(target: tuple) -> bool:
            return (not only or target in view.by_target) and text in view.label(target, self.names).casefold()

        def add(parent, target: tuple) -> QTreeWidgetItem:
            counts = Counter(titles[card.kind] for card in view.by_target.get(target, ()))
            item = QTreeWidgetItem(parent, [view.label(target, self.names), ", ".join(
                title if count == 1 else f"{title} x{count}" for title, count in counts.items())])
            item.setData(0, Qt.UserRole, target)
            item.setIcon(0, line_icon(ICONS[target[0]]))
            return item

        with QSignalBlocker(self.tree):
            self.tree.clear()
            sections = {group: QTreeWidgetItem(self.tree, [title]) for group, title in (
                ("joint", self.tr("Joints")), ("channel", self.tr("Blend shapes")), ("material", self.tr("Materials")),
                ("property", self.tr("Other")))}
            sections["value"] = sections["property"]
            rig_joints = self._rig_items(sections["joint"], shown, add)
            others = {target for target in view.by_target if target[0] != "joint"} | {
                ("joint", joint) for joint in view.joint_hashes()}
            for target in sorted(others - rig_joints, key=lambda target: view.label(target, self.names).casefold()):
                if shown(target):
                    add(sections[target[0]], target)
            for item in list(sections.values()):
                if item.childCount() == 0 and item.treeWidget() is not None:
                    self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
            self.tree.expandAll()
            self.tree.resizeColumnToContents(0)
            self.select(current)
        self.chosen.emit(self.target_of(self.tree.currentItem()))

    def _rig_items(self, section: QTreeWidgetItem, shown, add) -> set[tuple]:
        if self.rig is None:
            return set()
        hashes, parents = self.rig.hashes, self.rig.parents
        kept = {row for row, value in enumerate(hashes) if shown(("joint", value))}
        for row in list(kept):
            while (row := int(parents[row])) >= 0 and row not in kept:
                kept.add(row)
        items = {}
        for level in self.rig.levels:
            for row in (int(row) for row in level if row in kept):
                items[row] = add(items.get(int(parents[row]), section), ("joint", hashes[row]))
        return {("joint", value) for value in hashes}


class JointCards(FieldPanel):
    pick_requested = Signal(object)

    def __init__(self, edits: JointEdits, parent: QWidget | None = None):
        super().__init__(parent)
        self.edits, self.data = edits, joint_view()
        self.view: JointView | None = None
        self.target: tuple | None = None
        self.names: dict[int, str] = {}
        self.joints: dict[int, str] = {}
        self.expanded: set[int] = set()
        self.message = styled_label("", self, banner="warning")
        self.message.setWordWrap(True)
        self.message.hide()
        self.layout().insertWidget(1, self.message)

    def show_target(self, view: JointView, target: tuple | None, names: dict[int, str], joints) -> None:
        self.view, self.target, self.names = view, target, names
        self.joints = {joint: view.name(joint, names) for joint in joints}
        if target is None:
            self.show_fields((id(view.model), None), self.tr("Constraints"), self._file_fields())
            return
        fields = [field for card in view.by_target.get(target, ()) for field in self._card(card)]
        if target[0] == "joint" and (readers := view.readers.get(target[1])):
            fields.append(Field(self.tr("Drives"), "section", lambda: None))
            fields += [Field(self.data["cards"][card.kind]["title"], "readonly",
                             lambda card=card: ", ".join(view.label(listed, names) for listed in card.targets))
                       for card in readers]
        fields.append(self._adder())
        self.show_fields((id(view.model), target), view.label(target, names), fields)

    def _run(self, action, *args):
        try:
            result = action(*args)
        except EditRefused as error:
            self._say(str(error), "warning")
            return None
        self.message.hide()
        return result

    def _say(self, text: str, banner: str) -> None:
        self.message.setText(text)
        set_style_role(self.message, banner=banner)
        self.message.show()

    def picking(self, on: bool) -> None:
        if not on:
            self.message.hide()

    def _file_fields(self) -> list[Field]:
        fields = [Field(self.tr("Version"), "readonly", lambda: self.edits.document.model.version)]
        if not (order := rows(self.edits.root, "constraint_order")):
            return fields
        kinds = {value: member for member, value in self.edits.enums.of_field(
            self.edits.layouts["constraint_kind"], "kind").items()}
        fields.append(Field(self.tr("Evaluation order"), "section", lambda: None))
        for row in order:
            kind = row.values["kind"]
            fields.append(Field(kinds.get(kind, str(kind)), "row", lambda: None, options={"fields": [
                Field("", "button", lambda: self.tr("Earlier"), lambda _, kind=kind: self._run(
                    self.edits.move_kind, kind, -1)),
                Field("", "button", lambda: self.tr("Later"), lambda _, kind=kind: self._run(
                    self.edits.move_kind, kind, 1))]}))
        return fields

    def _adder(self) -> Field:
        cards = self.data["cards"]
        kinds = [kind for kind in self.edits.tables if self.target[0] in cards.get(kind, {}).get("under", ())]
        options = {-1: self.tr("Add constraint..."), **{at: cards[kind]["title"] for at, kind in enumerate(kinds)}}
        return Field(self.tr("Add"), "enum", lambda: -1, lambda at: self._run(
            self.edits.add_card, kinds[at], self.target, self.names), options)

    def _expand(self, key: int, shown: bool) -> None:
        (self.expanded.add if shown else self.expanded.discard)(key)
        self.show_target(self.view, self.target, self.names, self.joints)

    def _heading(self, card: Card) -> str:
        title, record = self.data["cards"][card.kind]["title"], card.records[0]
        if card.kind != "output":
            return title
        members = {field: {value: member for member, value in self.edits.enums.of_field(
            self.edits.layouts["output"], field).items()} for field in ("output_type", "axis")}
        return " · ".join((title, members["output_type"].get(record.values["output_type"], ""), " ".join(
            members["axis"].get(output.values["axis"], "?") for output in card.records)))

    def _card(self, card: Card) -> list[Field]:
        spec, record = self.data["cards"][card.kind], card.records[0]
        key, shown = id(record), id(record) in self.expanded or len(self.view.by_target[self.target]) == 1
        fields = [Field(self._heading(card), "toggle", lambda: shown, lambda on: self._expand(key, on))]
        if not shown:
            return fields
        for prefix in (spec.get("target"), *spec.get("reads", ())):
            if prefix and prefix != "material" and self._has(record, prefix):
                fields.append(self._joint(record, prefix))
        if card.kind == "material_constraint":
            fields += [self._hash_name(record, "material_name_hash"), self._hash_name(record, "variable_name_hash")]
        if card.kind == "output":
            fields += self._values(card.records, "output")
            for output in card.records:
                fields += self._axis(output)
            fields.append(Field("", "button", lambda: self.tr("Add axis"), lambda _: self._run(
                self.edits.add_axis, card)))
        else:
            for row in card.records:
                fields += self._values((row,), row.kind)
            for link, prefix in spec.get("rows", {}).items():
                fields += self._rows(record, link, prefix)
        fields.append(Field("", "row", lambda: None, options={"fields": [
            Field("", "button", lambda: self.tr("Move up"), lambda _: self._run(self.edits.move_card, card, -1)),
            Field("", "button", lambda: self.tr("Move down"), lambda _: self._run(self.edits.move_card, card, 1)),
            Field("", "button", lambda: self.tr("Delete"), lambda _: self._run(self.edits.remove_card, card))]}))
        return fields

    def _axis(self, output: Record) -> list[Field]:
        axes = self.edits.enums.of_field(self.edits.layouts["output"], "axis")
        name = next((member for member, value in axes.items() if value == output.values["axis"]), "")
        fields = [Field(self.tr("Axis {0}").format(name), "section", lambda: None),
                  *self._values((output,), "output_axis")]
        fields += self._rows(output, "joint_drivers", "joint_name")
        for driver in rows(output, "cone_drivers"):
            fields += [Field(self.tr("Cone driver"), "section", lambda: None), self._cone(driver),
                       *self._values((driver,), "cone_driver"), self._remove(output, "cone_drivers", driver)]
        fields.append(Field("", "row", lambda: None, options={"fields": [
            Field("", "button", lambda: self.tr("Add joint driver"), lambda _: self._run(
                self.edits.add_row, output, "joint_drivers", "joint_driver")),
            Field("", "button", lambda: self.tr("Add cone driver"), lambda _: self._run(
                self.edits.add_row, output, "cone_drivers", "cone_driver"))]}))
        return fields

    def _rows(self, record: Record, link: str, prefix: str) -> list[Field]:
        fields = []
        for row in rows(record, link):
            fields += [Field(_label(row.kind), "section", lambda: None), self._joint(row, prefix),
                       *self._values((row,), row.kind), self._remove(record, link, row)]
        if link != "joint_drivers":
            kind = next(item.record for item in self.edits.layouts[record.kind].links if item.name == link)
            fields.append(Field("", "button", lambda: self.tr("Add {0}").format(_label(kind).lower()),
                                lambda _: self._run(self.edits.add_row, record, link, kind)))
        return fields

    def _remove(self, owner: Record, link: str, row: Record) -> Field:
        return Field("", "button", lambda: self.tr("Delete {0}").format(_label(row.kind).lower()),
                     lambda _: self._run(self.edits.remove_row, owner, link, row))

    @staticmethod
    def _has(record: Record, prefix: str) -> bool:
        return prefix + "_hash" in record.values or prefix + "_index" in record.values

    def _joint(self, record: Record, prefix: str) -> Field:
        def chosen(joint: int) -> None:
            self._run(self.edits.set_joint, record, prefix, joint, self.names.get(joint) or self.view.names.get(joint))

        def pick(_) -> None:
            self._say(self.tr("Click a joint in the viewport to set {0}; Esc cancels.").format(_label(prefix)), "info")
            self.pick_requested.emit(chosen)
        return Field(_label(prefix), "row", lambda: None, options={"fields": [
            Field("", "choice", lambda: self.view.joints.of(record, prefix), chosen, self.joints),
            Field("", "button", lambda: self.tr("Pick"), pick)]})

    def _cone(self, driver: Record) -> Field:
        cones = rows(self.edits.root, "cone_inputs")
        options = {NO_CONE: self.tr("None"), **{at: f"{at}: {self.view.name(self.view.joints.of(cone), self.names)}"
                                                for at, cone in enumerate(cones)}}
        return Field(self.tr("Cone"), "enum", lambda: driver.values["input_cone_no"], lambda value: self._run(
            self.edits.set_value, driver, "input_cone_no", value), options)

    def _hash_name(self, record: Record, name: str) -> Field:
        def hashed(text: str) -> None:
            text = text.strip()
            value = int(text, 16) if text.lower().startswith("0x") else murmur3_hash_utf16le(text)
            self._run(self.edits.set_value, record, name, value)
        return Field(_label(name.removesuffix("_hash")), "text",
                     lambda: self.view.name(record.values[name], self.names), hashed)

    def _values(self, records: tuple[Record, ...], listed: str) -> list[Field]:
        record = records[0]
        layout = self.edits.layouts[record.kind]
        fields = []
        for name in self.data["fields"].get(listed, ()):
            if name not in record.values:
                continue
            members = self.edits.enums.of_field(layout, name)
            value = record.values[name]

            def setter(value, name=name) -> None:
                self._run(self.edits.set_value, records, name, value)
            if members and name == "flags":
                fields.append(Field(_label(name), "row", lambda: None, options={"fields": [
                    Field(member, "bool", lambda bit=bit, name=name: bool(record.values[name] & bit),
                          lambda on, bit=bit, name=name: setter(record.values[name] | bit if on else
                                                                record.values[name] & ~bit, name))
                    for member, bit in members.items() if bit and not bit & (bit - 1)]}))
            elif members:
                options = {code: member for member, code in sorted(members.items(), key=lambda item: item[1])}
                fields.append(Field(_label(name), "enum", lambda name=name: record.values[name], setter, options))
            elif isinstance(value, tuple):
                fields.append(Field(_label(name), "row", lambda: None, options={"fields": [
                    Field("", "float", lambda lane=lane, name=name: record.values[name][lane],
                          lambda number, lane=lane, name=name: setter(
                              tuple(number if at == lane else item for at, item in enumerate(record.values[name])),
                              name))
                    for lane in range(len(value))]}))
            elif name in self.data["degrees"]:
                fields.append(Field(_label(name).replace(" rad", ""), "float",
                                    lambda name=name: math.degrees(record.values[name]),
                                    lambda degrees, name=name: setter(math.radians(degrees), name)))
            else:
                fields.append(Field(_label(name), "float" if isinstance(value, float) else "int",
                                    lambda name=name: record.values[name], setter))
        return fields
