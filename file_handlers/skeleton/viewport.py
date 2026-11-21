"""See docs/internals/skeleton-editor.md, Viewport."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QToolTip

lazy from file_handlers.mesh.lit_preview import LitLines, LitPoints
lazy from file_handlers.mmtr.overlay import view_projection
lazy from ui.scene.screen_points import nearest, project

MODES = ("position", "rotation", "scale")
_BONE, _JOINT = (190, 195, 205, 130), (215, 220, 230, 230)
_CHOSEN, _PARENT, _MIRROR = (255, 226, 60, 255), (255, 150, 40, 255), (90, 215, 255, 255)

_PICK = 10.0

_WAIST, _GIRTH, _WIDEST = 0.15, 0.07, 0.012
_MODE_KEYS = (Qt.Key_1, Qt.Key_2, Qt.Key_3)

EDGES = np.array([(0, 2), (0, 3), (0, 4), (0, 5), (1, 2), (1, 3), (1, 4), (1, 5), (2, 3), (3, 4), (4, 5), (5, 2)])
FACES = np.array([(0, 3, 2), (0, 4, 3), (0, 5, 4), (0, 2, 5), (1, 2, 3), (1, 3, 4), (1, 4, 5), (1, 5, 2)])


def octahedra(heads: np.ndarray, tails: np.ndarray, waist: float, radius: np.ndarray) -> np.ndarray:
    along = tails - heads
    unit = along / np.linalg.norm(along, axis=1, keepdims=True)
    side = np.cross(unit, np.where(np.abs(unit[:, 1:2]) < 0.9, (0.0, 1.0, 0.0), (1.0, 0.0, 0.0)))
    side *= radius / np.linalg.norm(side, axis=1, keepdims=True)
    other, middle = np.cross(unit, side), heads + along * waist
    return np.stack((heads, tails, middle + side, middle + other, middle - side, middle - other), 1)


def bones(points: np.ndarray, parents: np.ndarray, rows: np.ndarray) -> np.ndarray:
    """An octahedron per joint of ``rows`` that stands off a parent: the bone from its parent to it."""
    rows = rows[parents[rows] >= 0]
    length = np.linalg.norm(points[rows] - points[parents[rows]], axis=1, keepdims=True)
    rows, length = rows[length[:, 0] > 1e-9], length[length[:, 0] > 1e-9]
    return octahedra(points[parents[rows]], points[rows], _WAIST, np.minimum(length * _GIRTH, widest(points)))


def widest(points: np.ndarray) -> float:
    return _WIDEST * max(float(np.ptp(points, axis=0).max()), 1e-3)


class SkeletonTool(QObject):

    picked = Signal(object, object)
    picking = Signal(bool)
    mode_keyed = Signal(int)
    delete_requested = Signal()
    frame_requested = Signal()
    noted = Signal(str)

    def __init__(self, viewport, document, parent: QObject | None = None):
        super().__init__(parent)
        self.viewport, self.document = viewport, document
        self.place = self._inverse = np.eye(4)
        self.selection: list = []
        self.mode, self.local, self.shown = "position", True, True
        self.armed = None
        self._grab = self._marks = self._worlds = self._axes = None
        self._dragged = False
        document.changed.connect(self.refresh)

    @property
    def dragging(self) -> bool:
        return self._grab is not None

    def set_place(self, place: np.ndarray) -> None:
        self.place, self._inverse = place, np.linalg.inv(place)
        self.refresh()

    def set_selection(self, joints: list) -> None:
        self.selection = joints
        self.refresh()

    def set_options(self, **options) -> None:
        for name, value in options.items():
            setattr(self, name, value)
        self.refresh()

    def arm(self, chosen) -> None:
        if chosen is None and self.armed is None:
            return
        self.armed = chosen
        if chosen is None:
            self.viewport.unsetCursor()
        else:
            self.viewport.setCursor(Qt.CrossCursor)
        self.picking.emit(chosen is not None)
        self.refresh()

    def refresh(self, _change=None) -> None:
        self._marks = self._worlds = None
        rows = self.document.skeleton.rows()
        self.selection = [joint for joint in self.selection if id(joint) in rows]
        self.viewport.edit_tool_changed()

    def worlds(self) -> np.ndarray:
        if self._worlds is None:
            self._worlds = self.place @ self.document.worlds().astype(np.float64)
        return self._worlds

    def points(self, joints: list) -> np.ndarray:
        rows = self.document.skeleton.rows()
        return self.worlds()[[rows[id(joint)] for joint in joints], :3, 3]

    def marks(self) -> tuple:
        if self._marks is None:
            self._marks = self._made() if self.shown and self.document.joints else ()
        return self._marks

    def _made(self) -> tuple:
        joints, rows, points = self.document.joints, self.document.skeleton.rows(), self.worlds()[:, :3, 3]
        parents = np.array([rows[id(joint.parent)] for joint in joints])
        marks, chosen = [], set(map(id, self.selection))
        for colour, width, size, boned, shown in (
                (_BONE, 1.0, 5.0, True, joints),
                (_MIRROR, 2.0, 8.0, True, [joint.mirror for joint in self.selection if id(joint.mirror) not in chosen]),
                (_PARENT, 2.0, 8.0, False,
                 [joint.parent for joint in self.selection if id(joint.parent) not in chosen]),
                (_CHOSEN, 2.0, 10.0, True, self.selection)):
            at = np.array([rows[id(joint)] for joint in shown if joint is not None], np.int64)
            if boned and len(wires := bones(points, parents, at)[:, EDGES].reshape(-1, 2, 3)):
                marks.append(LitLines(wires, width, colour, False))
            if len(at):
                marks.append(LitPoints(points[at], size, _JOINT if colour == _BONE else colour, False))
        return tuple(marks)

    def _at(self, pos):
        camera, ratio = self.viewport.view_camera()
        found = nearest(*project(self.worlds()[:, :3, 3], view_projection(camera), camera, ratio), pos, _PICK)
        return None if found is None else self.document.joints[found]

    def press(self, event, pos) -> bool:
        if event.button() != Qt.LeftButton or not self.shown or not self.document.joints:
            return False
        joint = self._at(pos)
        if (chosen := self.armed) is None:
            self.picked.emit(joint, event.modifiers())
        elif joint is not None:
            self.arm(None)
            chosen(joint)
        return joint is not None or chosen is not None

    def move(self, event, pos) -> bool:
        if self.shown and self.document.joints and not event.buttons() and (joint := self._at(pos)) is not None:
            QToolTip.showText(event.globalPosition().toPoint(), joint.name, self.viewport)
        else:
            QToolTip.hideText()
        return False

    def release(self, event, pos) -> bool:
        return False

    def wheel(self, event) -> bool:
        return False

    def key_press(self, event) -> bool:
        if (action := self._key_action(event)) is None:
            return False
        action()
        return True

    def takes_key(self, event) -> bool:
        return self._key_action(event) is not None

    def _key_action(self, event):
        key = event.key()
        if event.modifiers() & ~Qt.KeypadModifier or not self.shown:
            return None
        if key == Qt.Key_Escape and self.armed is not None:
            return lambda: self.arm(None)
        if key in _MODE_KEYS:
            return lambda: self.mode_keyed.emit(_MODE_KEYS.index(key))
        return {Qt.Key_Delete: self.delete_requested.emit, Qt.Key_F: self.frame_requested.emit}.get(key)

    def leave(self) -> None:
        QToolTip.hideText()

    def gizmo(self):
        if not self.shown or not self.selection or self.armed is not None:
            return None
        rows, worlds = self.document.skeleton.rows(), self.worlds()
        points = self.points(self.document.under(self.selection))
        low, high = (chosen := self.points(self.selection)).min(0), chosen.max(0)
        last = self.selection[-1]
        reach = 0.0 if last.parent is None else float(np.linalg.norm(chosen[-1] - worlds[rows[id(last.parent)], :3, 3]))
        axes = np.eye(3)
        if self.local or self.mode == "scale":
            axes = worlds[rows[id(last)], :3, :3].T / np.linalg.norm(worlds[rows[id(last)], :3, :3], axis=0)[:, None]
        return (chosen[0] if len(chosen) == 1 else (low + high) / 2,
                max(float(np.ptp(points, axis=0).max()), reach, 1e-3), axes)

    def gizmo_begin(self) -> None:
        try:
            self._grab = self.document.grab(self.selection, self.mode)
        except ValueError as error:
            self.noted.emit(str(error))
            return
        self._dragged, self._axes = False, self.gizmo()[2]

    def gizmo_drag(self, matrix) -> None:
        if self._grab is None:
            return
        matrix = np.asarray(matrix, np.float64)
        self._dragged = True
        self.document.drag(self._grab, np.diag([*np.diag(self._axes @ matrix[:3, :3] @ self._axes.T), 1.0])
                           if self.mode == "scale" else self._inverse @ matrix @ self.place, self.mode)

    def gizmo_end(self, commit: bool) -> None:
        grab, self._grab = self._grab, None
        if grab is not None and self._dragged and not commit:
            self.document.undo_stack.undo()
        self.refresh()
