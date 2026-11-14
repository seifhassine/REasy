"""See docs/internals/clip-editor.md."""

from __future__ import annotations

from PySide6.QtCore import QObject
from PySide6.QtGui import QUndoStack

from file_handlers.document_commands import Change, EditDocument

from .decode import read_timeline
from .encode import write_timeline
from .model import Clip, ResourceKind, Timeline, walk_nodes
from .rules import FormatRules, VersionProfile, profile
from .bake import bake_changes
from .sequences import derived_ranges, sequence_leaves
from .validation import Issue, validate


class ClipDocument(EditDocument):
    def __init__(self, timeline: Timeline, parent: QObject | None = None, undo_stack: QUndoStack | None = None):
        super().__init__(parent, undo_stack)
        self.timeline = timeline
        self.profile: VersionProfile = profile(timeline.version)
        self._issues: list[Issue] | None = None
        self._parents: dict[int, tuple[object, str]] | None = None
        self.changed.connect(self._forget)
        self.undo_stack.indexChanged.connect(self._follow)

    @classmethod
    def read(cls, data: bytes, kind: ResourceKind | None = None, parent: QObject | None = None) -> ClipDocument:
        return cls(read_timeline(data, kind), parent)

    def write(self) -> bytes:
        return write_timeline(self.timeline)

    @property
    def rules(self) -> FormatRules:
        return self.profile.rules

    def issues(self) -> list[Issue]:
        if self._issues is None:
            self._issues = validate(self.timeline)
        return self._issues

    def parent(self, item: object) -> tuple[object, str] | None:
        if self._parents is None:
            parents: dict[int, tuple[object, str]] = {}
            tracks = self.timeline.tracks
            for track in tracks:
                parents[id(track)] = (self.timeline, "tracks")
                for clip in track.clips:
                    parents[id(clip)] = (track, "clips")
                    parents.update((id(node), (clip, "nodes")) for node in clip.nodes)
                parents.update((id(node), (track, "nodes")) for node in track.nodes)
            for node in self.timeline.roots:
                parents.setdefault(id(node), (self.timeline, "root_order"))
            for node in walk_nodes(self.timeline.roots):
                parents.update((id(child), (node, "children")) for child in node.children)
                stack = [(node, "properties", node.properties)]
                while stack:
                    owner, field, props = stack.pop()
                    for prop in props:
                        parents[id(prop)] = (owner, field)
                        parents.update((id(key), (prop, "keys")) for key in prop.keys)
                        parents.update((id(key), (prop, "extra_keys")) for key in prop.extra_keys.values())
                        parents.update((id(point), (prop, "speed_points")) for point in prop.speed_points)
                        stack.append((prop, "children", prop.children))
            stack = [(self.timeline, "sequences", self.timeline.sequences)]
            while stack:
                owner, field, infos = stack.pop()
                for info in infos:
                    parents[id(info)] = (owner, field)
                    stack.append((info, "children", info.children))
            self._parents = parents
        return self._parents.get(id(item))

    def owner(self, item: object, kind: type) -> object | None:
        while (found := self.parent(item)) is not None:
            item = found[0]
            if isinstance(item, kind):
                return item
        return None

    def time_offset(self, item: object) -> float:
        clip = self.owner(item, Clip)
        return 0.0 if clip is None else clip.frame_in - clip.source_in

    def _forget(self, change: Change) -> None:
        self._issues = None
        if change.structural:
            self._parents = None

    def _follow(self, _index: int) -> None:
        timeline = self.timeline
        if timeline.kind is not ResourceKind.DIALOGUE_TIMELINE:
            return
        leaves = list(sequence_leaves(timeline))
        moved = [(info, ranges) for info, derived in derived_ranges(timeline, leaves)
                 if info.follows and (ranges := sorted([*derived, *info.kept])) != info.ranges]
        rebaked = [(target, values) for prop, changes, keys in bake_changes(timeline, leaves) if prop.baked
                   for target, values in [(prop, changes), *keys] if values]
        for info, ranges in moved:
            info.ranges[:] = ranges
        for target, values in rebaked:
            for name, value in values.items():
                setattr(target, name, value)
        for change in [*(Change(info, "ranges") for info, _ranges in moved),
                       *(Change(target, next(iter(values))) for target, values in rebaked)]:
            self.changed.emit(change)
