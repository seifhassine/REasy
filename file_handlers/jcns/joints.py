"""See docs/internals/jcns-editor.md."""

from __future__ import annotations

from dataclasses import dataclass

lazy from utils.hash_util import murmur3_hash_utf16le

from .derived import Joints, rows, walk
from .model import JointConstraints, Record, Text
from .rules import Enums, joint_view, rules_for


@dataclass(eq=False, slots=True)
class Card:
    """One constraint: a record (an output pack's outputs; a rotation and its LOD row), the targets it is listed
    under (("joint", hash), ("channel", hash), ("material", material, variable), ...) and the joints it reads, each
    with its row's weight (None for a read without one)."""

    kind: str
    records: tuple[Record, ...]
    targets: tuple[tuple, ...]
    reads: tuple[tuple[int | None, float | None], ...]


def card_rows(kind: str, records: list[Record]) -> list[tuple[Record, ...]]:
    if kind != "output":
        return [(record,) for record in records]
    packs, at = [], 0
    while at < len(records):
        size = records[at].values.get("out_pack_num", 0) + 1
        packs.append(tuple(records[at:at + size]))
        at += size
    return packs


def file_names(model: JointConstraints) -> dict[int, str]:
    return {record.values.get(name + "_hash") or murmur3_hash_utf16le(link.value): link.value
            for record in walk(model.root) for name, link in record.links.items() if isinstance(link, Text)}


class JointView:
    def __init__(self, model: JointConstraints, enums: Enums):
        self.model, self.data = model, joint_view()
        self.layouts = rules_for(model.version).records
        self.joints = Joints(model.root)
        self.names = file_names(model)
        self.groups = {value: self.data["groups"][member]
                       for member, value in enums.of_field(self.layouts["output"], "output_type").items()
                       if member in self.data["groups"]}
        self.tables = {link.record: link.name for link in self.layouts[model.root.kind].links
                       if link.record in self.data["cards"]}
        self.cards: list[Card] = []
        self.by_target: dict[tuple, list[Card]] = {}
        self.readers: dict[int, list[Card]] = {}
        root = model.root
        for kind, table in self.tables.items():
            lods = rows(root, "lods") if kind == "rotation" else ()
            for at, records in enumerate(card_rows(kind, rows(root, table))):
                record = records[0]
                if kind == "output":
                    self.add(Card(kind, records, (self.target(record),), self.output_reads(records)))
                elif kind == "blend_shape_group":
                    channels = tuple(("channel", row.values["hash"]) for row in rows(record, "hashes"))
                    self.add(Card(kind, records, channels, ()))
                else:
                    self.add(Card(kind, (record, *lods[at:at + 1]), (self.listed(kind, record),),
                                  self.reads(kind, record)))

    def add(self, card: Card) -> None:
        self.cards.append(card)
        for target in card.targets:
            self.by_target.setdefault(target, []).append(card)
        for joint in dict.fromkeys(joint for joint, _weight in card.reads):
            if joint is not None:
                self.readers.setdefault(joint, []).append(card)

    def target(self, output: Record) -> tuple:
        group, values = self.groups.get(output.values["output_type"], "joint"), output.values
        hashes = (values["joint_name_hash"], values.get("variable_name_hash", 0))
        return group, *hashes[:2 if group in ("material", "property") else 1]

    def listed(self, kind: str, record: Record) -> tuple:
        prefix = self.data["cards"][kind]["target"]
        if prefix == "material":
            return "material", record.values["material_name_hash"], record.values["variable_name_hash"]
        return "joint", self.joints.of(record, prefix)

    def reads(self, kind: str, record: Record) -> tuple[tuple[int | None, float | None], ...]:
        spec = self.data["cards"][kind]
        return (*((self.joints.of(record, prefix), None) for prefix in spec.get("reads", ())),
                *((self.joints.of(row, prefix), row.values.get("weight"))
                  for link, prefix in spec.get("rows", {}).items() for row in rows(record, link)))

    def output_reads(self, pack: tuple[Record, ...]) -> tuple[tuple[int | None, float | None], ...]:
        cones = rows(self.model.root, "cone_inputs")
        found = []
        for output in pack:
            found += self.reads("output", output)
            for driver in rows(output, "cone_drivers"):
                if (number := driver.values["input_cone_no"]) < len(cones):
                    found += (self.joints.of(cones[number]), None), *self.reads("cone_input", cones[number])
        return tuple(found)

    def guides(self, target: tuple | None) -> list[tuple]:
        """What the viewport draws at a joint for its cards (joint_view.json ``axes`` and ``guide``): ("axis", local
        vector, RGBA), ("world_up", aim, RGBA) and ("cone", cone, half angle, RGBA, measured RGBA), aims and cones
        by table row."""
        found, colours = [], self.data["guide_colours"]
        for card in self.by_target.get(target, ()):
            spec, record = self.data["cards"][card.kind], card.records[0]
            found += [("axis", record.values[name], tuple(colour)) for name, colour in spec.get("axes", {}).items()
                      if name in record.values]
            if guide := spec.get("guide"):
                at = next(at for at, row in enumerate(rows(self.model.root, self.tables[card.kind])) if row is record)
                half = (record.values.get(spec["half_angle"]),) if "half_angle" in spec else ()
                found.append((guide, at, *half, *map(tuple, colours[guide])))
        return found

    def joint_hashes(self) -> set[int]:
        return {joint for joint in (*self.readers, *(target[1] for target in self.by_target if target[0] == "joint"))
                if joint is not None}

    def name(self, value: int | None, names: dict[int, str] | None = None) -> str:
        if value is None:
            return "-"
        return (names or {}).get(value) or self.names.get(value) or f"0x{value:08X}"

    def label(self, target: tuple, names: dict[int, str] | None = None) -> str:
        return " / ".join(self.name(value, names) for value in target[1:])
