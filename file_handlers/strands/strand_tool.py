from __future__ import annotations

lazy import numpy as np
from PySide6.QtCore import QObject, Qt, Signal

lazy from file_handlers.clsp.geometry import outline
lazy from file_handlers.mesh.lit_preview import LitLines
lazy from file_handlers.mmtr.overlay import view_projection

lazy from .colliders import EVERY_TAG, SetColliders
lazy from .editing import StrandEdit

SELECT, PAINT, COMB, SMOOTH, TRANSFORM, COLLIDERS = "select", "paint", "comb", "smooth", "transform", "colliders"
MODES = (SELECT, PAINT, COMB, SMOOTH, TRANSFORM, COLLIDERS)
BRUSHES = (PAINT, COMB, SMOOTH)
# RGBA.
_STRAND, _GUIDE, _CHOSEN = (120, 185, 235, 150), (255, 150, 40, 210), (255, 226, 60, 235)
_BRUSH, _BOX = (255, 255, 255, 200), (255, 255, 255, 220)
_COLLIDER, _IGNORED, _PICKED = (90, 230, 120, 190), (160, 160, 160, 80), (255, 90, 230, 255)
# Widget pixels.
_CLICK, _PICK = 4.0, 12.0
_MODE_KEYS = (Qt.Key_1, Qt.Key_2, Qt.Key_3, Qt.Key_4, Qt.Key_5, Qt.Key_6)


class StrandTool(QObject):
    """``world`` places the strands (4 x 4, column vectors)."""

    changed = Signal()
    selection_changed = Signal()
    mode_changed = Signal(str)
    options_changed = Signal()
    collider_picked = Signal(object)
    delete_requested = Signal()

    def __init__(self, viewport, edit: StrandEdit, world: np.ndarray, colliders: SetColliders | None = None,
                 parent=None):
        super().__init__(parent)
        self.viewport, self.edit, self.colliders = viewport, edit, colliders
        self.world = np.asarray(world, np.float64).reshape(4, 4)
        self._inverse = np.linalg.inv(self.world)
        self.mode = SELECT
        # Radius in widget pixels; strength 0 to 1.
        self.radius, self.strength = 40.0, 0.5
        self.keep_lengths, self.pin_roots, self.chosen_only = True, True, False
        self.lines_shown, self.xray, self.isolate, self.guides_shown = 80_000, False, False, False
        self.colliders_shown, self.all_colliders, self.collision_tag = True, False, EVERY_TAG
        self.picked: int | None = None
        self._selection_version = 0
        self._cursor: np.ndarray | None = None
        self._drag: dict | None = None
        self._projected = (None, None)
        self._lines = (None, ())
        self._overlay = (None, ())
        self._outlines = (None, ())
        self._drawn_shapes: dict = {}
        self._gizmo = (None, None)
        self._marks_key, self._marks = None, ()

    def marks(self) -> tuple:
        key = (self._line_marks(), self._collider_marks(), self._overlay_marks())
        if self._marks_key != key:
            self._marks_key, self._marks = key, sum(key, ())
        return self._marks

    def gizmo(self):
        if self.mode == COLLIDERS and self.picked is not None:
            key = ("collider", self.colliders.revision, self.picked)
        elif self.mode == TRANSFORM and self.edit.selected.any():
            key = ("strands", self.edit.revision, self._selection_version)
        else:
            return None
        if self._gizmo[0] != key:
            if key[0] == "collider":
                points = self._outline(self.colliders.placed()[self.picked]).reshape(-1, 3)
            else:
                points = self._to_world(self.edit.positions[self.edit.lod0.rows(np.flatnonzero(self.edit.selected))])
            low, high = points.min(0), points.max(0)
            self._gizmo = key, ((low + high) / 2, max(float((high - low).max()), 1e-3))
        return self._gizmo[1]

    def gizmo_begin(self) -> None:
        if self.mode == COLLIDERS:
            self.colliders.begin()
            self._drag = {"kind": "gizmo", "collider": self.picked, "start": self.colliders.shape(self.picked)}
            return
        self.edit.begin_stroke()
        self._drag = {"kind": "gizmo", "held": self.edit.hold(np.flatnonzero(self.edit.selected))}

    def gizmo_drag(self, matrix) -> None:
        drag = self._drag
        if drag is None or drag["kind"] != "gizmo":
            return
        if "collider" in drag:
            self.colliders.transform(drag["collider"], matrix, drag["start"])
            self._redraw()
            return
        local = self._inverse @ np.asarray(matrix, np.float64) @ self.world
        self.edit.transform(drag["held"], local, pin_roots=self.pin_roots)
        self._edited()

    def gizmo_end(self, commit: bool) -> None:
        drag, self._drag = self._drag, None
        if drag is None or drag["kind"] != "gizmo":
            self._drag = drag
            return
        if "collider" in drag:
            self.colliders.end(commit)
        else:
            self.edit.end_stroke()
            if not commit:
                self.edit.undo()
        self._edited()

    def press(self, event, pos) -> bool:
        if event.button() != Qt.LeftButton:
            return False
        self.viewport.setFocus(Qt.MouseFocusReason)
        self._cursor = pos
        modifiers = event.modifiers()
        if self.mode == COLLIDERS:
            self.pick(self._nearest_collider(pos))
            return True
        if self.mode in (SELECT, TRANSFORM):
            self._drag = {"kind": "box", "start": pos, "end": pos, "modifiers": modifiers}
        else:
            self._drag = {"kind": self.mode, "last": pos, "modifiers": modifiers}
            if self.mode != PAINT:
                self.edit.begin_stroke()
            self._brush(pos, pos)
        self._redraw()
        return True

    def move(self, event, pos) -> bool:
        if event.buttons() & (Qt.RightButton | Qt.MiddleButton):
            return False
        self._cursor = pos
        drag = self._drag
        if drag is None:
            if self.mode in BRUSHES:
                self._redraw()
            return False
        if drag["kind"] == "box":
            drag["end"] = pos
        elif drag["kind"] != "gizmo":
            self._brush(drag["last"], pos)
            drag["last"] = pos
        self._redraw()
        return True

    def release(self, event, pos) -> bool:
        drag = self._drag
        if event.button() != Qt.LeftButton or drag is None or drag["kind"] == "gizmo":
            return False
        self._drag = None
        if drag["kind"] == "box":
            self._box(drag["start"], pos, drag["modifiers"])
        elif drag["kind"] != PAINT:
            self.edit.end_stroke()
            self._edited()
        self._redraw()
        return True

    def wheel(self, event) -> bool:
        if not event.modifiers() & Qt.ControlModifier or self.mode not in BRUSHES:
            return False
        self.set_radius(self.radius * (1.15 ** (event.angleDelta().y() / 120.0)))
        return True

    def key_press(self, event) -> bool:
        action = self._key_action(event)
        if action is None:
            return False
        action()
        return True

    def takes_key(self, event) -> bool:
        return self._key_action(event) is not None

    def _key_action(self, event):
        key, modifiers = event.key(), event.modifiers() & ~Qt.KeypadModifier
        control, shift = modifiers == Qt.ControlModifier, modifiers == (Qt.ControlModifier | Qt.ShiftModifier)
        count = self.edit.strand_count
        if (control or shift) and key == Qt.Key_Z:
            return self.redo if shift else self.undo
        if control and key == Qt.Key_Y:
            return self.redo
        if control and key == Qt.Key_A:
            return lambda: self.select(np.ones(count, bool))
        if control and key == Qt.Key_I:
            return lambda: self.select(~self.edit.selected)
        if control and key == Qt.Key_D:
            return lambda: self.select(np.zeros(count, bool))
        if modifiers:
            return None
        if key == Qt.Key_Delete:
            return self.delete_requested.emit
        if key in _MODE_KEYS:
            return lambda: (self.set_mode(MODES[_MODE_KEYS.index(key)]), self.mode_changed.emit(self.mode))
        if key in (Qt.Key_BracketLeft, Qt.Key_BracketRight):
            return lambda: self.set_radius(self.radius * (1.15 if key == Qt.Key_BracketRight else 1 / 1.15))
        return None

    def set_radius(self, radius: float) -> None:
        self.radius = float(np.clip(radius, 4, 600))
        self.options_changed.emit()
        self._redraw()

    def leave(self) -> None:
        if self._drag is None and self._cursor is not None:
            self._cursor = None
            self._redraw()

    def set_mode(self, mode: str) -> None:
        if mode in MODES and mode != self.mode and (mode != COLLIDERS or self.colliders is not None):
            self.mode = mode
            self._redraw()

    def pick(self, number: int | None) -> None:
        if number != self.picked:
            self.picked = number
            self.collider_picked.emit(number)
            self._redraw()

    def select(self, mask: np.ndarray) -> None:
        self.edit.selected[:] = mask
        self._selection_version += 1
        self.selection_changed.emit()
        self._redraw()

    def undo(self) -> None:
        if self.edit.undo():
            self._edited()

    def redo(self) -> None:
        if self.edit.redo():
            self._edited()

    def refresh(self) -> None:
        self._lines = (None, ())
        self._redraw()

    def edited(self) -> None:
        self._edited()

    def selected_world_points(self) -> np.ndarray:
        chosen = np.flatnonzero(self.edit.selected)
        rows = self.edit.lod0.rows(chosen) if len(chosen) else np.arange(len(self.edit.positions))
        return self._to_world(self.edit.positions[rows])

    def _edited(self) -> None:
        self._projected = (None, None)
        self.changed.emit()
        self._redraw()

    def _redraw(self) -> None:
        self.viewport.edit_tool_changed()

    def _box(self, start, end, modifiers) -> None:
        screen, seen = self._screen()
        if float(np.abs(np.subtract(end, start)).max()) < _CLICK:
            distance = np.linalg.norm(screen - np.asarray(end, np.float64), axis=1)
            distance[~seen] = np.inf
            nearest = int(np.argmin(distance)) if len(distance) else -1
            hit = np.zeros(self.edit.strand_count, bool)
            if nearest >= 0 and distance[nearest] <= _PICK:
                hit[self.edit.lod0.strand_of[nearest]] = True
        else:
            low, high = np.minimum(start, end), np.maximum(start, end)
            inside = seen & np.all((screen >= low) & (screen <= high), axis=1)
            hit = np.zeros(self.edit.strand_count, bool)
            hit[np.unique(self.edit.lod0.strand_of[inside])] = True
        self.select(self._combined(hit, modifiers))

    def _combined(self, hit: np.ndarray, modifiers) -> np.ndarray:
        if modifiers & Qt.ControlModifier:
            return self.edit.selected & ~hit
        if modifiers & Qt.ShiftModifier:
            return self.edit.selected | hit
        return hit

    def _brush(self, last, now) -> None:
        drag = self._drag
        screen, seen = self._screen()
        offset = screen - np.asarray(now, np.float64)
        squared = np.einsum("ij,ij->i", offset, offset)
        under = seen & (squared <= self.radius * self.radius)
        if self.chosen_only and drag["kind"] != PAINT:
            under &= self.edit.selected[self.edit.lod0.strand_of]
        vertices = np.flatnonzero(under)
        if not len(vertices):
            return
        distance = np.sqrt(squared[vertices])
        if drag["kind"] == PAINT:
            hit = np.zeros(self.edit.strand_count, bool)
            hit[np.unique(self.edit.lod0.strand_of[vertices])] = True
            if drag["modifiers"] & Qt.ControlModifier:
                self.select(self.edit.selected & ~hit)
            else:
                self.select(self.edit.selected | hit)
            return
        falloff = (1 - (distance / self.radius) ** 2) ** 2 * self.strength
        if drag["kind"] == COMB:
            step = np.subtract(now, last)
            if not step.any():
                return
            self.edit.comb(vertices, self._screen_step(vertices, step) * falloff[:, None],
                           keep_lengths=self.keep_lengths)
        else:
            self.edit.smooth(vertices, falloff * 0.5)
        self._reproject(self.edit.lod0.rows(self.edit.moved))
        self.changed.emit()
        self._redraw()

    def _screen_step(self, vertices: np.ndarray, step) -> np.ndarray:
        """``step`` in widget pixels; the result in strand space."""
        camera, ratio = self.viewport.view_camera()
        width, height = camera.size
        clip = self._clip_matrix(camera)
        points = self.edit.positions[vertices].astype(np.float64)
        w = points @ clip[:3, 3] + clip[3, 3]
        image = np.array((2 * step[0] * ratio / width, -2 * step[1] * ratio / height, 0, 0)) @ np.linalg.inv(clip)
        moved = w[:, None] * image[:3]
        moved += points
        moved /= (1 + w * image[3])[:, None]
        moved -= points
        return moved

    def _clip_matrix(self, camera) -> np.ndarray:
        return self.world.T @ view_projection(camera)

    def _screen(self) -> tuple[np.ndarray, np.ndarray]:
        camera, ratio = self.viewport.view_camera()
        key = (self.edit.revision, *self._camera_key(camera, ratio))
        if self._projected[0] != key:
            self._projected = key, self._project(self.edit.positions, self._clip_matrix(camera), camera, ratio)
        return self._projected[1]

    def _reproject(self, rows: np.ndarray) -> None:
        key, held = self._projected
        if key is None:
            return
        camera, ratio = self.viewport.view_camera()
        if key[1:] != self._camera_key(camera, ratio):
            self._projected = (None, None)
            return
        screen, seen = held
        screen[rows], seen[rows] = self._project(self.edit.positions[rows], self._clip_matrix(camera), camera, ratio)
        self._projected = (self.edit.revision, *key[1:]), held

    @staticmethod
    def _camera_key(camera, ratio) -> tuple:
        return np.asarray(camera.view, np.float64).tobytes(), tuple(camera.scale), tuple(camera.size), ratio

    @staticmethod
    def _project(points: np.ndarray, clip: np.ndarray, camera, ratio) -> tuple[np.ndarray, np.ndarray]:
        """Positions in widget pixels."""
        clip = np.asarray(clip, np.float32)
        at = np.asarray(points, np.float32) @ clip[:3] + clip[3]
        w = at[:, 3]
        seen = w > camera.near * 0.5
        safe = np.where(seen, w, 1)
        width, height = camera.size
        screen = np.column_stack(((at[:, 0] / safe + 1) * 0.5 * width, (1 - at[:, 1] / safe) * 0.5 * height))
        return screen / ratio, seen

    def _to_world(self, points: np.ndarray) -> np.ndarray:
        return np.asarray(points, np.float64) @ self.world[:3, :3].T + self.world[:3, 3]

    def _shown(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        edit = self.edit
        chosen = edit.selected
        guides = edit.guide_mask() & ~chosen if self.guides_shown and not self.isolate else np.zeros_like(chosen)
        rest = np.zeros_like(chosen) if self.isolate else ~chosen & ~guides
        segments = np.diff(edit.lod0.starts) - 1
        budget = max(int(self.lines_shown) - int(segments[guides].sum()), 0)
        picked = []
        for mask in (chosen, rest):
            strands = np.flatnonzero(mask)
            total = int(segments[strands].sum())
            strands = strands[::max(1, -(-total // budget))] if budget else strands[:0]
            budget = max(budget - int(segments[strands].sum()), 0)
            picked.append(strands)
        return np.flatnonzero(guides), picked[0], picked[1]

    def _line_marks(self) -> tuple:
        key = (self.edit.revision, self._selection_version, self.lines_shown, self.xray, self.isolate,
               self.guides_shown)
        if self._lines[0] != key:
            guides, chosen, rest = self._shown()
            marks = []
            for strands, colour, width in ((rest, _STRAND, 1.0), (guides, _GUIDE, 1.0), (chosen, _CHOSEN, 1.0)):
                ends = self._segments(strands)
                if len(ends):
                    marks.append(LitLines(ends, width, colour, not self.xray))
            self._lines = key, tuple(marks)
        return self._lines[1]

    def _segments(self, strands: np.ndarray) -> np.ndarray:
        if not len(strands):
            return np.zeros((0, 2, 3))
        level = self.edit.lod0
        rows = level.rows(strands)
        points = self._to_world(level.vertices["position"][rows])
        joined = level.strand_of[rows[:-1]] == level.strand_of[rows[1:]]
        return np.stack((points[:-1][joined], points[1:][joined]), axis=1)

    def _collider_marks(self) -> tuple:
        if self.colliders is None or not self.colliders_shown:
            return ()
        key = (self.colliders.revision, self.picked, self.collision_tag, self.all_colliders)
        if self._outlines[0] != key:
            groups = {_COLLIDER: [], _IGNORED: [], _PICKED: []}
            placed = self.colliders.placed()
            for number, shape in enumerate(placed):
                colour = (_PICKED if number == self.picked else _COLLIDER if shape.tag & self.collision_tag
                          else _IGNORED if self.all_colliders else None)
                if colour is not None:
                    groups[colour].append(self._outline(shape))
            self._outlines = key, tuple(LitLines(np.concatenate(lines), 2.0 if colour == _PICKED else 1.0, colour,
                                                 False) for colour, lines in groups.items() if lines)
            kept = {_shape_key(shape) for shape in placed}
            self._drawn_shapes = {shape: lines for shape, lines in self._drawn_shapes.items() if shape in kept}
        return self._outlines[1]

    def _outline(self, shape) -> np.ndarray:
        key = _shape_key(shape)
        if key not in self._drawn_shapes:
            self._drawn_shapes[key] = outline(shape)
        return self._drawn_shapes[key]

    def _nearest_collider(self, pos) -> int | None:
        if self.colliders is None:
            return None
        camera, ratio = self.viewport.view_camera()
        best, found = _PICK, None
        for number, shape in enumerate(self.colliders.placed()):
            if not (self.all_colliders or shape.tag & self.collision_tag or number == self.picked):
                continue
            ends = self._outline(shape)
            screen, seen = self._project(ends.reshape(-1, 3), view_projection(camera), camera, ratio)
            a, b = screen[0::2], screen[1::2]
            span = b - a
            share = np.clip(np.einsum("ij,ij->i", pos - a, span) / np.maximum(np.einsum("ij,ij->i", span, span),
                                                                               1e-9), 0, 1)
            distance = np.linalg.norm(a + span * share[:, None] - pos, axis=1)[seen[0::2] & seen[1::2]]
            if len(distance) and distance.min() < best:
                best, found = float(distance.min()), number
        return found

    def _overlay_marks(self) -> tuple:
        drag = self._drag
        key = (self.mode, None if self._cursor is None else tuple(np.round(self._cursor, 1)), self.radius,
               None if drag is None or drag["kind"] != "box" else tuple(np.round(drag["end"], 1)))
        if self._overlay[0] == key:
            return self._overlay[1]
        marks = ()
        if drag is not None and drag["kind"] == "box":
            (x0, y0), (x1, y1) = drag["start"], drag["end"]
            corners = np.array([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], np.float64)
            marks = self._screen_lines(corners, _BOX)
        elif self.mode in BRUSHES and self._cursor is not None:
            angles = np.linspace(0, 2 * np.pi, 49)[:-1]
            ring = np.asarray(self._cursor) + self.radius * np.column_stack((np.cos(angles), np.sin(angles)))
            marks = self._screen_lines(ring, _BRUSH)
        self._overlay = key, marks
        return marks

    def _screen_lines(self, points: np.ndarray, colour) -> tuple:
        camera, ratio = self.viewport.view_camera()
        width, height = camera.size
        clip = view_projection(camera)
        w = camera.near * 4
        device = points * ratio
        ndc = np.column_stack((device[:, 0] / width * 2 - 1, 1 - device[:, 1] / height * 2))
        z = (camera.projection()[2, 2] * -w + camera.projection()[3, 2])
        at = np.column_stack((ndc * w, np.full(len(ndc), z), np.full(len(ndc), w)))
        world = at @ np.linalg.inv(clip)
        world = world[:, :3] / world[:, 3:]
        ends = np.stack((world, np.roll(world, -1, axis=0)), axis=1)
        return (LitLines(ends, 1.0, colour, False),)


def _shape_key(shape) -> tuple:
    return shape.kind, shape.values.tobytes()


__all__ = ["BRUSHES", "COLLIDERS", "COMB", "MODES", "PAINT", "SELECT", "SMOOTH", "StrandTool", "TRANSFORM"]
