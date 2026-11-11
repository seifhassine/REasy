"""See docs/internals/clip-editor.md."""

from __future__ import annotations

import math

lazy import numpy as np
from PySide6.QtWidgets import QWidget

from file_handlers.document_commands import Change
from ui.curve_view import CURVE_COLORS, CurveSource, CurveView

from .. import edits
from ..evaluation import NUMERIC_TYPES, sample
from ..model import Clip, Key, Node, Property, SpeedPoint, Track, walk_nodes, walk_properties
from ..rules import InterpolationType, property_type
from ..values import coerce
from .lanes import MARGIN
from .state import EditorState

LIMIT = 12


class ClipCurves(CurveView):
    def __init__(self, state: EditorState, parent: QWidget | None = None):
        super().__init__(_PropertyCurves(state), parent)
        self.source.setParent(self)

    def properties(self) -> list[Property]:
        return self.source.properties


class _Curve:
    def __init__(self, prop: Property, offset: float, color: str, rules: dict):
        self.prop, self.offset, self.colors, self.rules = prop, offset, (color,), rules
        self.frames = np.array([key.frame + offset for key in prop.keys], np.float64)
        self.values = np.array([float(key.value) for key in prop.keys], np.float64).reshape(-1, 1)

    def sample_many(self, frames) -> np.ndarray:
        found = (sample(self.prop, frame - self.offset, self.rules)
                 for frame in np.asarray(frames, np.float64).tolist())
        return np.array([math.nan if value is None else float(value) for value in found]).reshape(-1, 1)

    def handles(self, at: int) -> list[tuple[float, float, float, float]]:
        keys = self.prop.keys
        key = keys[at]
        if key.control is None or at + 1 >= len(keys) or key.interpolation not in (InterpolationType.HERMITE,
                                                                                 InterpolationType.BEZIER):
            return []
        nxt = keys[at + 1]
        start, end = (key.frame + self.offset, float(key.value)), (nxt.frame + self.offset, float(nxt.value))
        if not (math.isfinite(start[1]) and math.isfinite(end[1])):
            return []
        out_x, out_y, in_x, in_y = key.control[:4]
        if key.interpolation == InterpolationType.HERMITE:
            first, second = (start[0] + out_x / 3, start[1] + out_y / 3), (end[0] + in_x / 3, end[1] + in_y / 3)
        else:
            span = (nxt.frame - key.frame) / 3
            first, second = (start[0] + span, start[1] + out_y), (end[0] - span, end[1] - in_y)
        return [(*start, *first), (*end, *second)]


class _PropertyCurves(CurveSource):
    def __init__(self, state: EditorState):
        super().__init__()
        self.state = state
        self.properties: list[Property] = []
        self._moving: tuple = (None, [])
        state.frame_changed.connect(self.moved)
        state.view_changed.connect(self.reload)
        state.selection_changed.connect(self._selected)
        state.document.changed.connect(self._changed)

    def _selected(self) -> None:
        selection, document = self.state.selection, self.state.document
        kept = [prop for prop in self.properties
                if prop.type in NUMERIC_TYPES and prop.keys and document.parent(prop) is not None]
        if selection and all(isinstance(item, (Key, SpeedPoint)) and document.owner(item, Property) in kept
                             for item in selection):
            found = kept
        else:
            found = self._collect(selection)
        fit = [id(prop) for prop in found] != [id(prop) for prop in self.properties]
        self.properties = found
        self.reload(fit)

    def _changed(self, change: Change) -> None:
        if change.structural or change.field == "type":
            self._selected()
        else:
            self.reload()

    def _collect(self, selection: list) -> list[Property]:
        found: list[Property] = []
        for item in selection:
            if isinstance(item, (Key, SpeedPoint)):
                item = self.state.document.owner(item, Property)
            if isinstance(item, (Track, Clip)):
                roots = item.roots if isinstance(item, Track) else item.nodes
                props = [prop for node in walk_nodes(roots) for prop in node.properties]
            elif isinstance(item, Node):
                props = [prop for node in walk_nodes([item]) for prop in node.properties]
            else:
                props = [item] if isinstance(item, Property) else []
            for prop in walk_properties(props):
                if prop.type in NUMERIC_TYPES and prop.keys and prop not in found:
                    found.append(prop)
                if len(found) >= LIMIT:
                    return found
        return found

    def curves(self) -> list[_Curve]:
        document = self.state.document
        return [_Curve(prop, document.time_offset(prop), CURVE_COLORS[at % len(CURVE_COLORS)], document.profile.player)
                for at, prop in enumerate(self.properties)]

    def picked(self) -> set[tuple[int, int]]:
        selected = {id(item) for item in self.state.selection}
        return {(track, at) for track, prop in enumerate(self.properties) for at, key in enumerate(prop.keys)
                if id(key) in selected}

    def pick(self, picks: set[tuple[int, int]]) -> None:
        self.state.select([self.properties[track].keys[at] for track, at in sorted(picks)] or list(self.properties))

    def x_range(self, width: int) -> tuple[float, float]:
        low = self.state.view_start - MARGIN / self.state.view_scale
        return low, low + max(width, 1) / self.state.view_scale

    def set_x_range(self, low: float, high: float, width: int) -> bool:
        """Past the lanes' zoom limits the axis stays as it is."""
        scale = width / (high - low)
        if 0.02 <= scale <= 400.0:
            self.state.set_view(low + MARGIN / scale, scale)
        return True

    def marker(self) -> float:
        return self.state.frame

    def move_keys(self, track: int, keys: list[int], frames, values, dx: float, dy: float, component: int,
                  merge) -> None:
        prop = self.properties[track]
        if self._moving[0] != merge:
            self._moving = merge, [(prop.keys[at], prop.keys[at].frame, float(prop.keys[at].value)) for at in keys]
        kind, step = property_type(prop.type).value, self.state.snapped(dx)
        edits.set_keys(self.state.document, [(key, float(frame + step), coerce(kind, value + dy))
                                             for key, frame, value in self._moving[1]], "Edit Curve", merge)

    def move_handle(self, track: int, at: int, handle: int, x: float, y: float, merge) -> None:
        """Handle 0 is the key's out tangent, 1 the next key's in tangent (both stored on the key)."""
        prop = self.properties[track]
        key, anchor = prop.keys[at], prop.keys[at + handle]
        control = list(key.control)
        if key.interpolation == InterpolationType.HERMITE:
            control[2 * handle] = (x - self.state.document.time_offset(prop) - anchor.frame) * 3
            control[2 * handle + 1] = (y - float(anchor.value)) * 3
        else:
            control[2 * handle + 1] = (y - float(anchor.value)) * (1.0 if handle == 0 else -1.0)
        edits.set_fields(self.state.document, key, "Edit Curve", merge,
                         control=tuple(float(value) for value in control))

    def add_key(self, track: int, x: float) -> None:
        prop = self.properties[track]
        frame = self.state.snapped(x - self.state.document.time_offset(prop))
        self.state.select([edits.add_key(self.state.document, prop, frame)])

    def delete_keys(self, picked: dict[int, list[int]]) -> None:
        edits.remove(self.state.document, [self.properties[track].keys[at] for track, keys in picked.items()
                                           for at in keys])
