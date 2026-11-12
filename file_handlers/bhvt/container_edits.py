"""See docs/internals/bhvt-editor.md, Document and edits."""

from __future__ import annotations

import itertools
import uuid

lazy from file_handlers.clip.model import ResourceKind, Timeline
lazy from file_handlers.uvar.uvar_file import UVarFile

from file_handlers.document_commands import ReplaceContents, SetFields

from .document import BhvtDocument
from .edits import _fresh, _push, add_node, set_fields
from .errors import BhvtFormatError
from .graph_edits import move_node
from .model import Node, Record, TimelineEntry, TimelineFsm, TimelineTree, Tree
from .rules import timeline_fsm_rules


def unused_name(base: str, taken) -> str:
    taken = set(taken)
    return next(name for name in itertools.chain([base], (f"{base} {count}" for count in itertools.count(2)))
                if name not in taken)


def _blank_tree(document: BhvtDocument, fsm: TimelineFsm) -> Tree:
    rules, like = timeline_fsm_rules(fsm.version), fsm.trees[0].tree
    variable = UVarFile()
    variable.header.version = like.variable.header.version
    root = Node({**_fresh(rules.tree.node, rules.tree.new_node["fsm"]), "id": 0, "ex_id": 0,
                 "name": rules.new["root"], "reference_tree": None})
    return Tree(like.version, Record(dict.fromkeys(like.header.fields(), 0)), [root], [], [],
                {name: document.rsz.blank(block) for name, block in like.blocks.items()}, [], [], variable, [])


def add_tree(document: BhvtDocument, name: str | None = None) -> TimelineTree:
    fsm, text = document.resource.model, document.tr("Add layer")
    new = timeline_fsm_rules(fsm.version).new
    item = TimelineTree(name or unused_name(new["tree"], (other.name for other in fsm.trees)), uuid.uuid4().bytes_le,
                        _blank_tree(document, fsm))
    tree, root = item.tree, item.tree.nodes[0]
    with document.macro(text):
        document.push(ReplaceContents(document, fsm, "trees", [*fsm.trees, item], text))
        add_node(document, tree, root, new["start"])
        end = add_node(document, tree, root, new["end"])
        set_fields(document, end, text, is_end=1)
        move_node(document, tree, end, root, 0)
    return item


def remove_tree(document: BhvtDocument, item: TimelineTree) -> None:
    fsm, text = document.resource.model, document.tr("Remove layer")
    if len(fsm.trees) == 1:
        raise BhvtFormatError(document.tr("{0} is the only layer of the file").format(item.name))
    holds = [(hold, {"targets": [target for target in hold.targets if target != item.layer_id]})
             for hold in fsm.hold_points if item.layer_id in hold.targets]
    _push(document, [ReplaceContents(document, fsm, "trees", [tree for tree in fsm.trees if tree is not item], text),
                     *([SetFields(document, holds, text)] if holds else [])], text)


def rename(document: BhvtDocument, item: TimelineTree | TimelineEntry, name: str) -> None:
    if not name or "\0" in name:
        raise BhvtFormatError(document.tr("A name is not empty and holds no NUL"))
    if name != item.name:
        document.push(SetFields(document, [(item, {"name": name})], document.tr("Rename")))


def add_timeline(document: BhvtDocument, name: str | None = None) -> TimelineEntry:
    fsm = document.resource.model
    rules = timeline_fsm_rules(fsm.version)
    name = name or unused_name(rules.new["timeline"], (entry.name for entry in fsm.timelines))
    entry = TimelineEntry(name, uuid.uuid4().bytes_le, Timeline(rules.timeline, ResourceKind.TIMELINE))
    document.push(ReplaceContents(document, fsm, "timelines", [*fsm.timelines, entry], document.tr("Add timeline")))
    return entry


def remove_timeline(document: BhvtDocument, entry: TimelineEntry) -> None:
    fsm = document.resource.model
    document.push(ReplaceContents(document, fsm, "timelines", [item for item in fsm.timelines if item is not entry],
                                  document.tr("Remove timeline")))


def add_hold_point(document: BhvtDocument) -> Record:
    fsm = document.resource.model
    hold = Record({"hold_point_id": uuid.uuid4().bytes_le, "targets": []})
    document.push(ReplaceContents(document, fsm, "hold_points", [*fsm.hold_points, hold],
                                  document.tr("Add hold point")))
    return hold


def remove_hold_point(document: BhvtDocument, hold: Record) -> None:
    fsm = document.resource.model
    document.push(ReplaceContents(document, fsm, "hold_points", [item for item in fsm.hold_points if item is not hold],
                                  document.tr("Remove hold point")))


def set_hold_targets(document: BhvtDocument, hold: Record, targets: list[bytes]) -> None:
    if targets != hold.targets:
        document.push(SetFields(document, [(hold, {"targets": targets})], document.tr("Hold point layers")))
