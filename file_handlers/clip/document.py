"""See docs/internals/clip-editor.md."""

from __future__ import annotations

from PySide6.QtCore import QObject

from file_handlers.document_commands import Change, EditDocument

from .decode import read_timeline
from .encode import write_timeline
from .model import Clip, ResourceKind, Timeline, walk_nodes
from .rules import FormatRules, VersionProfile, profile
from .validation import Issue, validate


class ClipDocument(EditDocument):
    def __init__(self, timeline: Timeline, parent: QObject | None = None):
        super().__init__(parent)
        self.timeline = timeline
        self.profile: VersionProfile = profile(timeline.version)
        self._issues: list[Issue] | None = None
        self._parents: dict[int, tuple[object, str]] | None = None
        self.changed.connect(self._forget)

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
            for node in walk_nodes(root for track in tracks for root in track.roots):
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
