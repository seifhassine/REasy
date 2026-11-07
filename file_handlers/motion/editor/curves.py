from __future__ import annotations

lazy import numpy as np
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QWidget

from file_handlers.document_commands import Change
from ui.curve_view import CurveSource, CurveView

from .. import edits
from .state import EditorState


class TrackCurves(CurveView):
    def __init__(self, state: EditorState, parent: QWidget | None = None):
        super().__init__(_MotionCurves(state), parent)
        self.source.setParent(self)


class _MotionCurves(CurveSource):
    def __init__(self, state: EditorState):
        super().__init__()
        self.state = state
        self.tracks: list[tuple] = []
        state.selection_changed.connect(self._selected)
        state.frame_changed.connect(self.moved)
        state.document.changed.connect(self._changed)

    def _selected(self) -> None:
        self.reload(True)

    def _changed(self, change: Change) -> None:
        if change.structural or any(change.target is owner for owner, _field in self.tracks):
            self.reload()

    def curves(self) -> list:
        self.tracks = self.state.tracks()
        return [edits.curve(self.state.document, owner, field) for owner, field in self.tracks]

    def extent(self) -> tuple[float, float]:
        return 0.0, max(self.state.end_frame, 1.0)

    def marker(self) -> float:
        return self.state.frame

    def set_marker(self, frame: float) -> None:
        self.state.set_frame(max(0.0, round(frame)))

    def move_keys(self, track: int, keys: list[int], frames, values, dx: float, dy: float, component: int,
                  merge) -> list[int]:
        """Keys move by whole frames; a key they land on is dropped."""
        owner, field = self.tracks[track]
        frames, values, picked = frames.astype(np.int64), values.copy(), np.array(keys, np.int64)
        frames[picked] = np.maximum(frames[picked] + round(dx), 0)
        values[picked, component] += dy
        kept = np.ones(len(frames), bool)
        kept[[index for index in range(len(frames)) if index not in keys and frames[index] in frames[picked]]] = False
        edits.set_keys(self.state.document, owner, field, frames[kept], values[kept],
                       QCoreApplication.translate("TrackCurves", "Move Keys"), merge)
        moved = set(frames[picked].tolist())
        return [index for index, frame in enumerate(edits.curve(self.state.document, owner, field).frames.tolist())
                if frame in moved]

    def add_key(self, track: int, frame: float) -> None:
        edits.add_key(self.state.document, *self.tracks[track], max(0, round(frame)))

    def delete_keys(self, picked: dict[int, list[int]]) -> None:
        if picked:
            with self.state.document.macro(QCoreApplication.translate("TrackCurves", "Delete Keys")):
                for track, keys in sorted(picked.items()):
                    edits.delete_keys(self.state.document, *self.tracks[track], keys)
