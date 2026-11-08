"""See docs/internals/jcns-editor.md."""

from __future__ import annotations

import copy
from contextlib import contextmanager

from PySide6.QtCore import QCoreApplication

from file_handlers.document_commands import ReplaceContents

from .derived import Assignment, derived_changes, rows, same
from .document import JcnsDocument, Table
from .joints import Card, card_rows
from .model import Record, Text
from .rules import Enums, joint_view, new_record, rules_for, stored

Replacement = tuple[Record, str, dict]
NO_CONE = 0xFF


class EditRefused(ValueError):
    pass


translate = QCoreApplication.translate


@contextmanager
def _swapped(replacements: list[Replacement]):
    held = [(record, where, getattr(record, where)) for record, where, _ in replacements]
    for record, where, contents in replacements:
        setattr(record, where, dict(contents))
    try:
        yield
    finally:
        for record, where, contents in reversed(held):
            setattr(record, where, contents)


def _alike(before: list[Assignment], after: list[Assignment]) -> bool:
    return len(before) == len(after) and all(
        a is c and b == d and same(e, f) for (a, b, e), (c, d, f) in zip(before, after))


class JointEdits:
    def __init__(self, document: JcnsDocument, enums: Enums):
        self.document, self.enums = document, enums
        self.layouts = rules_for(document.model.version).records
        self.tables = {link.record: link.name for link in self.layouts[document.root.kind].links if link.record}

    @property
    def root(self) -> Record:
        return self.document.root

    def commit(self, text: str, replacements: list[Replacement], raw: bool = False) -> None:
        kept = self.kept()
        with _swapped(replacements):
            tables = derived_changes(self.document.model, self.enums)
        if not raw and (refused := [name for name, before in kept.items() if not _alike(before, tables[name])]):
            raise EditRefused(translate(
                "JcnsEditor", "This file's {0} do not follow the exporter's rule, so REasy keeps them as they are and "
                "cannot make this edit.").format(", ".join(refused)))
        set_here = {(id(record), field) for record, where, new in replacements
                    for field, value in new.items() if value is not getattr(record, where).get(field)}
        contents = {(id(record), where): (record, where, dict(new)) for record, where, new in replacements}
        for name, changes in tables.items():
            if name in kept or raw and any((id(record), field) in set_here for record, field, _ in changes):
                continue
            for record, field, value in changes:
                where = "links" if field in record.links else "values"
                entry = contents.setdefault((id(record), where), (record, where, dict(getattr(record, where))))
                entry[2][field] = value
        with self.document.macro(text):
            for record, where, new in contents.values():
                self.document.push(ReplaceContents(self.document, record, where, new, text))

    def kept(self) -> dict[str, list[Assignment]]:
        return {name: changes for name, changes in derived_changes(self.document.model, self.enums).items() if changes}

    def new(self, kind: str, **values) -> Record:
        record = new_record(self.document.model.version, kind)
        for name, value in {**joint_view()["new"].get(kind, {}), **values}.items():
            if name not in record.values:
                continue
            members = self.enums.of_field(self.layouts[kind], name)
            if isinstance(value, str):
                if value not in members:
                    continue
                value = members[value]
            elif isinstance(value, list) and all(isinstance(member, str) for member in value):
                value = sum(members.get(member, 0) for member in value)
            elif isinstance(value, list):
                value = tuple(value)
            record.values[name] = stored(self.layouts[kind], name, value)
        return record

    def joint(self, record: Record, prefix: str, joint: int, name: str | None, root_links: dict) -> tuple[dict, dict]:
        """The record's values and links with reference ``prefix`` set to ``joint``; rows the tables need are added
        to ``root_links``."""
        values, links = dict(record.values), dict(record.links)
        if prefix in links:
            if not name:
                raise EditRefused(translate(
                    "JcnsEditor", "REasy needs this joint's name to write it here: choose a character with it."))
            links[prefix] = Text(name)
        if prefix + "_hash" in values:
            values[prefix + "_hash"] = joint
        elif record.kind == "constraint_weight" and "multi_parents" in root_links:
            parents = root_links["multi_parents"] = list(root_links["multi_parents"] or ())
            index = self._index(joint, root_links)
            values["parent_name_index"] = next(
                (slot for slot, row in enumerate(parents) if row.values["joint_name_index"] == index), len(parents))
            if values["parent_name_index"] == len(parents):
                parents.append(self.new("multi_parent", joint_name_index=index))
        elif prefix + "_index" in values:
            values[prefix + "_index"] = self._index(joint, root_links)
        return values, links

    def _index(self, joint: int, root_links: dict) -> int:
        table = root_links["name_hashes"] = list(root_links["name_hashes"] or ())
        found = next((at for at, row in enumerate(table) if row.values["hash"] == joint), len(table))
        if found == len(table):
            table.append(self.new("name_hash", hash=joint))
        return found

    def set_fields(self, record: Record, values: dict, text: str) -> None:
        layout = self.layouts[record.kind]
        values = {name: stored(layout, name, value) for name, value in values.items()}
        if any(record.values[name] != value for name, value in values.items()):
            self.commit(text, [(record, "values", {**record.values, **values})])

    def set_joint(self, record: Record, prefix: str, joint: int, name: str | None) -> None:
        root_links = dict(self.root.links)
        values, links = self.joint(record, prefix, joint, name, root_links)
        self.commit(translate("JcnsEditor", "Set {0}").format(prefix),
                    [(record, "values", values), (record, "links", links), (self.root, "links", root_links)])

    def set_value(self, records: Record | tuple[Record, ...], name: str, value, raw: bool = False) -> None:
        records = records if isinstance(records, tuple) else (records,)
        value = stored(self.layouts[records[0].kind], name, value)
        if changes := [(record, "values", {**record.values, name: value}) for record in records
                       if record.values[name] != value]:
            self.commit(translate("JcnsEditor", "Set {0}").format(name), changes, raw)

    def set_text(self, record: Record, name: str, text: str, raw: bool = False) -> None:
        if (current.value if isinstance(current := record.links.get(name), Text) else "") != text:
            self.commit(translate("JcnsEditor", "Set {0}").format(name),
                        [(record, "links", {**record.links, name: Text(text) if text else None})], raw)

    def set_rows(self, owner: Record, link: str, records: list[Record], text: str, raw: bool = False) -> None:
        self.commit(text, [(owner, "links", {**owner.links, link: records})], raw)

    def insert(self, table: Table, index: int, record: Record | None = None) -> Record:
        record = record or new_record(self.document.model.version, table.kind)
        listed = list(rows(table.owner, table.name))
        listed.insert(index, record)
        self.set_rows(table.owner, table.name, listed, translate("JcnsEditor", "Add {0}").format(table.kind), raw=True)
        return record

    def duplicate(self, record: Record) -> Record | None:
        if not isinstance(table := self.document.parent(record), Table):
            return None
        return self.insert(table, rows(table.owner, table.name).index(record) + 1, copy.deepcopy(record))

    def remove(self, records: list[Record]) -> None:
        owners = {}
        for record in records:
            if isinstance(table := self.document.parent(record), Table):
                links = owners.setdefault(id(table.owner), (table.owner, dict(table.owner.links)))[1]
                links[table.name] = [row for row in links[table.name] if row is not record]
        if owners:
            self.commit(translate("JcnsEditor", "Delete"),
                        [(owner, "links", links) for owner, links in owners.values()], raw=True)

    def add_row(self, owner: Record, link: str, kind: str) -> Record:
        record = self.new(kind)
        self.set_rows(owner, link, [*rows(owner, link), record], translate("JcnsEditor", "Add {0}").format(kind))
        return record

    def remove_row(self, owner: Record, link: str, record: Record) -> None:
        self.set_rows(owner, link, [row for row in rows(owner, link) if row is not record],
                      translate("JcnsEditor", "Delete {0}").format(record.kind))

    def add_card(self, kind: str, target: tuple, names: dict[int, str]) -> Record:
        spec, table, (group, *hashes) = joint_view()["cards"][kind], self.tables[kind], target
        record, root_links = self.new(kind, **joint_view()["new"].get(f"{kind}.{group}", {})), dict(self.root.links)
        if kind == "material_constraint":
            record.values.update(material_name_hash=hashes[0], variable_name_hash=hashes[1])
        else:
            if group == "material":
                if not names.get(hashes[1]):
                    raise EditRefused(translate("JcnsEditor", "REasy needs the variable's name to write it here."))
                record.links["variable_name"] = Text(names[hashes[1]])
            record.values, record.links = self.joint(record, spec.get("target", "joint_name"), hashes[0],
                                                     names.get(hashes[0]), root_links)
        if kind == "rotation":
            record.values["from_joint_name_hash"] = hashes[0]
            root_links["lods"] = [*rows(self.root, "lods"), self.new("lod")]
        root_links[table] = [*rows(self.root, table), record]
        self.commit(translate("JcnsEditor", "Add {0}").format(spec["title"]), [(self.root, "links", root_links)])
        return record

    def add_axis(self, card: Card) -> Record:
        last, outputs = card.records[-1], rows(self.root, "outputs")
        free = sorted(set(range(4)) - {output.values["axis"] for output in card.records})
        record = Record(last.kind, {**last.values, "axis": free[0] if free else last.values["axis"]},
                        {**last.links, "cone_drivers": [], "joint_drivers": []})
        at = outputs.index(last) + 1
        self.set_rows(self.root, "outputs", [*outputs[:at], record, *outputs[at:]], translate("JcnsEditor", "Add axis"))
        return record

    def remove_card(self, card: Card) -> None:
        table, records = self.tables[card.kind], rows(self.root, self.tables[card.kind])
        root_links, changes = {**self.root.links, table: [row for row in records if row not in card.records]}, []
        if card.kind == "rotation":
            at = records.index(card.records[0])
            root_links["lods"] = [row for number, row in enumerate(rows(self.root, "lods")) if number != at]
        elif card.kind == "cone_input":
            changes = self._renumbered(records.index(card.records[0]))
        self.commit(translate("JcnsEditor", "Delete {0}").format(joint_view()["cards"][card.kind]["title"]),
                    [(self.root, "links", root_links), *changes])

    def _renumbered(self, removed: int) -> list[Replacement]:
        changes = []
        for driver in (driver for output in rows(self.root, "outputs") for driver in rows(output, "cone_drivers")):
            number = driver.values["input_cone_no"]
            if number == removed:
                raise EditRefused(translate(
                    "JcnsEditor", "Driven keys read this cone: delete them or point them at another cone first."))
            if removed < number != NO_CONE:
                changes.append((driver, "values", {**driver.values, "input_cone_no": number - 1}))
        return changes

    def move_card(self, card: Card, step: int) -> None:
        table = self.tables[card.kind]
        groups = card_rows(card.kind, rows(self.root, table))
        at = next(number for number, group in enumerate(groups) if group[0] is card.records[0])
        if not 0 <= at + step < len(groups):
            return
        groups[at], groups[at + step] = groups[at + step], groups[at]
        root_links = {**self.root.links, table: [row for group in groups for row in group]}
        if card.kind == "rotation":
            lods = root_links["lods"] = list(rows(self.root, "lods"))
            lods[at], lods[at + step] = lods[at + step], lods[at]
        self.commit(translate("JcnsEditor", "Move {0}").format(joint_view()["cards"][card.kind]["title"]),
                    [(self.root, "links", root_links)])

    def move_kind(self, kind: int, step: int) -> None:
        order = list(rows(self.root, "constraint_order"))
        at = next(number for number, row in enumerate(order) if row.values["kind"] == kind)
        if 0 <= at + step < len(order):
            order[at], order[at + step] = order[at + step], order[at]
            self.set_rows(self.root, "constraint_order", order, translate("JcnsEditor", "Move constraint kind"))
