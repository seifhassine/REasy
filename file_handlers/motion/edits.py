"""See docs/internals/motion-editor.md."""

from __future__ import annotations

import copy

lazy import numpy as np

from file_handlers.document_commands import ReplaceContents, SetFields

from .document import MotionDocument
from .keys import Curve, full_track
from .model import (
    JointChannels,
    Motion,
    MotionListEntry,
    MotionTree,
    PropertyTrack,
    Sequence,
    Track,
    TreeLink,
    TreeNode,
    TreeParam,
)
from .rules import encoding, mot_profile


def set_fields(document: MotionDocument, target: object, text: str, **values) -> None:
    document.push(SetFields(document, [(target, values)], text))


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
        found = item.item if isinstance(item, MotionListEntry) else item
        if isinstance(found, kind):
            return found
    return None


def container(document: MotionDocument, item: object) -> tuple[object, str] | None:
    if isinstance(item, MotionListEntry):
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
            if isinstance(made, MotionListEntry):
                made.motion_id = max(entry.motion_id for entry in owner.entries) + 1
            contents = list(getattr(owner, field))
            contents.insert(next(index for index, held in enumerate(contents) if held is item) + 1, made)
            document.push(ReplaceContents(document, owner, field, contents, "Duplicate"))
            copies.append(made)
    return copies
