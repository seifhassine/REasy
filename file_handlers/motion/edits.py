"""See docs/internals/motion-editor.md."""

from __future__ import annotations

import copy

lazy import numpy as np

from file_handlers.document_commands import ReplaceContents, SetFields

from .document import ENTRIES, MotionDocument
from .keys import Curve, full_track
from .model import (
    JointChannels,
    Motion,
    MotionListEntry,
    MotionPackEntry,
    MotionTree,
    PropertyTrack,
    Sequence,
    Track,
    TreeLink,
    TreeMotion,
    TreeNode,
    TreeParam,
)
from .rules import encoding, mot_profile, tree_classes, tree_rules
from .tree_runtime import motion_choices


def set_fields(document: MotionDocument, target: object, text: str, **values) -> None:
    document.push(SetFields(document, [(target, values)], text))


def add_node(document: MotionDocument, tree: MotionTree, class_name: str) -> TreeNode:
    spec = tree_classes(tree.version)[class_name]
    params = [TreeParam(kind, key, None if value is None else bytes.fromhex(value) if kind == 13 else value,
                        *rest) for kind, key, value, *rest in spec["params"] if value is not None or kind != 13]
    node = TreeNode(class_name, class_hashes=tuple(spec["hashes"]), node_type=spec["type"], flags=spec["flags"],
                    id=_free_id(tree), params=params or None)
    with document.macro("Add Node"):
        document.push(ReplaceContents(document, tree, "nodes", [*tree.nodes, node], "Add Node"))
        if (choices := _choices(tree, node)) is not None:
            document.push(ReplaceContents(document, tree, "motions", [*tree.motions, TreeMotion(node, choices)],
                                          "Add Node"))
        if tree.root is None:
            set_fields(document, tree, "Add Node", root=node)
    return node


def _free_id(tree: MotionTree) -> int:
    """An authored node ID no node of the tree has (every shipped tree's are distinct); 0 from v19 (none kept)."""
    if "id" not in tree_rules(tree.version).records["node"].names:
        return 0
    return min(set(range(len(tree.nodes) + 1)) - {node.id for node in tree.nodes})


def _choices(tree: MotionTree, node: TreeNode) -> list[tuple[int, int]] | None:
    """The motion row shipped trees keep for ``node``: a MotionNode's from v13, a SelectMotionNode's from v19; None: the
    version keeps none."""
    remaps = tree_rules(tree.version).remaps
    held = node.class_name == "MotionNode" and remaps != "pairs" or node.class_name == "SelectMotionNode" and \
        remaps == "groups"
    return motion_choices(node) if held else None


def connect(document: MotionDocument, tree: MotionTree, parent: TreeNode, pin: int, child: TreeNode, kind: int,
            guid: bytes | None = None) -> TreeLink:
    """``child`` into ``parent``'s motion (``kind`` 1) or parameter (2) pin ``pin``, replacing what that pin held; a
    VariablesNode child gives the variable of ``guid`` (shipped links name it by GUID, child pin 0)."""
    link = TreeLink(parent, pin, child, 0, kind, guid)
    kept = [held for held in tree.links if not (held.parent is parent and held.pin == pin and held.type == kind)]
    document.push(ReplaceContents(document, tree, "links", [*kept, link], "Connect"))
    return link


def set_param(document: MotionDocument, param: TreeParam, value) -> None:
    """A motion row that followed its node's parameters follows them on."""
    node = document.parent(param)
    tree = _owner(document, node, MotionTree) if isinstance(node, TreeNode) else None
    row = None if tree is None else next((motion for motion in tree.motions if motion.node is node), None)
    followed = row is not None and row.choices == _choices(tree, node)
    with document.macro("Edit Parameter"):
        set_fields(document, param, "Edit Parameter", value=value)
        if followed and row.choices != (choices := _choices(tree, node)):
            set_fields(document, row, "Edit Parameter", choices=choices)


def curve(document: MotionDocument, owner: JointChannels | PropertyTrack, field: str) -> Curve:
    track = getattr(owner, field)
    return Curve(track, encoding(mot_profile(document.motion_of(owner).version).keys, track.kind, track.encoding))


def keys(document: MotionDocument, owner: JointChannels | PropertyTrack, field: str) -> tuple[np.ndarray, np.ndarray]:
    """Key frames and decoded values (keys x components)."""
    found = curve(document, owner, field)
    return found.frames.astype(np.int64), found.values


def set_keys(document: MotionDocument, owner: JointChannels | PropertyTrack, field: str, frames, values,
             text: str = "Edit Keys", merge_key=None) -> None:
    old = getattr(owner, field)
    frames = np.asarray(frames, np.int64)
    order = np.argsort(frames, kind="stable")
    frames, values = frames[order], np.asarray(values, np.float32)[order]
    last = np.append(frames[1:] != frames[:-1], True)
    motion = document.motion_of(owner)
    max_frame = motion.end_frame if mot_profile(motion.version).rules.tracks == "legacy" else 0.0
    track = full_track(old.kind, frames[last].tolist(), values[last], max_frame)
    document.push(SetFields(document, [(owner, {field: track})], text, merge_key))


def add_key(document: MotionDocument, owner: JointChannels | PropertyTrack, field: str, frame: int) -> None:
    frames, values = keys(document, owner, field)
    if frame not in frames:
        value = curve(document, owner, field).sample(frame)
        set_keys(document, owner, field, [*frames, frame], [*values, value], "Add Key")


def delete_keys(document: MotionDocument, owner: JointChannels | PropertyTrack, field: str, indices) -> None:
    frames, values = keys(document, owner, field)
    kept = np.setdiff1d(np.arange(len(frames)), np.asarray(list(indices), np.int64))
    if len(kept):
        set_keys(document, owner, field, frames[kept], values[kept], "Delete Keys")


def replace_tracks(document: MotionDocument, motion: Motion, tracks: dict[int, dict[str, Track]], end_frame: float,
                   joint_ids: dict[int, int], text: str) -> None:
    """``tracks`` and ``joint_ids`` are by joint name hash."""
    with document.macro(text):
        channels = list(motion.channels)
        by_hash = {channel.name_hash: channel for channel in channels}
        for name_hash, given in tracks.items():
            if (channel := by_hash.get(name_hash)) is None:
                channels.append(JointChannels(joint_ids.get(name_hash, 0), name_hash, **given))
            else:
                document.push(SetFields(document, [(channel, given)], text))
        if len(channels) != len(motion.channels):
            document.push(ReplaceContents(document, motion, "channels", channels, text))
        set_fields(document, motion, text, end_frame=end_frame)


def _owner(document: MotionDocument, item: object, kind: type) -> object | None:
    while (item := document.parent(item)) is not None:
        found = item.item if isinstance(item, ENTRIES) else item
        if isinstance(found, kind):
            return found
    return None


def container(document: MotionDocument, item: object) -> tuple[object, str] | None:
    if isinstance(item, ENTRIES):
        return document.root, "entries"
    if isinstance(item, Sequence):
        parent = document.parent(item)
        if isinstance(parent, MotionListEntry) and item in parent.overrides:
            return parent, "overrides"
        return document.motion_of(item), "sequences"
    fields = {JointChannels: (Motion, "channels"), PropertyTrack: (Motion, "properties"),
              TreeNode: (MotionTree, "nodes"), TreeLink: (MotionTree, "links"), TreeParam: (TreeNode, "params")}
    if (spec := fields.get(type(item))) is None:
        return None
    owner = _owner(document, item, spec[0])
    return None if owner is None else (owner, spec[1])


def delete(document: MotionDocument, items: list) -> None:
    groups: dict[tuple[int, str], tuple[object, str, list]] = {}
    for item in items:
        if (found := container(document, item)) is not None:
            groups.setdefault((id(found[0]), found[1]), (*found, []))[2].append(item)
    if not groups:
        return
    with document.macro("Delete"):
        for owner, field, removed in groups.values():
            gone = {id(item) for item in removed}
            document.push(ReplaceContents(document, owner, field,
                                          [item for item in getattr(owner, field) if id(item) not in gone], "Delete"))
            if isinstance(owner, MotionTree) and field == "nodes":
                document.push(ReplaceContents(document, owner, "links", [
                    link for link in owner.links if id(link.parent) not in gone and id(link.child) not in gone],
                    "Delete"))
                document.push(ReplaceContents(document, owner, "motions", [
                    motion for motion in owner.motions if id(motion.node) not in gone], "Delete"))
                if owner.root is not None and id(owner.root) in gone:
                    set_fields(document, owner, "Delete", root=None)


def duplicate(document: MotionDocument, items: list) -> list:
    found = [(item, *spec) for item in items
             if not isinstance(item, TreeLink) and (spec := container(document, item)) is not None]
    if not found:
        return []
    copies = []
    with document.macro("Duplicate"):
        for item, owner, field in found:
            made = copy.deepcopy(item)
            if isinstance(made, ENTRIES):
                made.motion_id = max(entry.motion_id for entry in owner.entries) + 1
            if isinstance(made, TreeNode):
                made.id = _free_id(owner)
            contents = list(getattr(owner, field))
            # A pack's IDs ascend (the game binary-searches them), so its copy goes last.
            contents.insert(len(contents) if isinstance(made, MotionPackEntry) else
                            next(index for index, held in enumerate(contents) if held is item) + 1, made)
            document.push(ReplaceContents(document, owner, field, contents, "Duplicate"))
            if isinstance(made, TreeNode) and (row := next((held for held in owner.motions if held.node is item),
                                                           None)) is not None:
                document.push(ReplaceContents(document, owner, "motions", [
                    *owner.motions, TreeMotion(made, list(row.choices))], "Duplicate"))
            copies.append(made)
    return copies
