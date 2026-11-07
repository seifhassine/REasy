"""See docs/internals/jcns-editor.md."""

from __future__ import annotations

lazy import numpy as np
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QToolTip

lazy from file_handlers.mesh.lit_preview import LitLines, LitPoints
lazy from file_handlers.mmtr.overlay import view_projection
lazy from ui.scene.screen_points import nearest, project

_JOINT, _CHOSEN = (190, 195, 205, 140), (255, 226, 60, 255)
# Widget pixels.
_PICK = 10.0
# A guide's length as a share of the skeleton's largest extent.
_GUIDE = 0.08


def _ray(origin: np.ndarray, direction, length: float, colour: tuple) -> list:
    direction = np.asarray(direction, np.float64)
    if not (norm := np.linalg.norm(direction)) > 1e-9:
        return []
    return [LitLines(np.array([[origin, origin + direction / norm * length]]), 2.0, colour, False)]


def _cone(apex: np.ndarray, axis, half: float, length: float, colour: tuple, sides: int = 32) -> list:
    """A cone of ``half`` radians around ``axis``: its rim and four sides."""
    axis = np.asarray(axis, np.float64)
    if not np.linalg.norm(axis) > 1e-9:
        return []
    axis = axis / np.linalg.norm(axis)
    side = np.cross(axis, (0.0, 1.0, 0.0) if abs(axis[1]) < 0.9 else (1.0, 0.0, 0.0))
    side /= np.linalg.norm(side)
    turns = np.linspace(0.0, 2.0 * np.pi, sides + 1)[:, None]
    rim = apex + length * (np.cos(half) * axis + np.sin(half) * (np.cos(turns) * side +
                                                                  np.sin(turns) * np.cross(axis, side)))
    ends = np.concatenate([np.stack([rim[:-1], rim[1:]], 1),
                           np.stack([np.broadcast_to(apex, (4, 3)), rim[:-1:sides // 4]], 1)])
    return [LitLines(ends, 1.0, colour, False)]


class JcnsTool(QObject):
    picked = Signal(int)
    picking = Signal(bool)

    def __init__(self, viewport, parent: QObject | None = None):
        super().__init__(parent)
        self.viewport = viewport
        self.hashes: tuple[int, ...] = ()
        self.names: dict[int, str] = {}
        self.worlds = np.zeros((0, 4, 4))
        self.evaluated: tuple = ([], [])
        self.chosen: int | None = None
        self.links: list[tuple[int, tuple[int, ...], float | None]] = []
        self.guides: list[tuple] = []
        self.armed = None
        self._marks: tuple | None = None

    def set_pose(self, hashes, worlds: np.ndarray | None, names: dict[int, str], evaluated: tuple = ([], [])) -> None:
        """``worlds``: the joints' world matrices (n x 4 x 4, column vectors), None hiding the marks; ``evaluated``:
        the evaluator's ``Program.guides``."""
        self.hashes, self.names = tuple(hashes) if worlds is not None else (), names
        self.worlds = np.zeros((0, 4, 4)) if worlds is None else np.asarray(worlds, np.float64)
        self.evaluated = evaluated
        self._changed()

    def set_links(self, chosen: int | None, links: list[tuple[int, tuple[int, ...], float | None]],
                  guides: list[tuple] = ()) -> None:
        """``links``: (joint, RGBA, weight or None) per joint the chosen joint is linked to; ``guides``: the chosen
        joint's ``JointView.guides``."""
        self.chosen, self.links, self.guides = chosen, links, list(guides)
        self._changed()

    def arm(self, chosen) -> None:
        if chosen is None and self.armed is None:
            return
        self.armed = chosen
        if chosen is None:
            self.viewport.unsetCursor()
        else:
            self.viewport.setCursor(Qt.CrossCursor)
        self.picking.emit(chosen is not None)

    def _changed(self) -> None:
        self._marks = None
        self.viewport.update()

    def marks(self) -> tuple:
        if self._marks is None:
            self._marks = self._made()
        return self._marks

    def _made(self) -> tuple:
        if not self.hashes:
            return ()
        positions = self.worlds[:, :3, 3]
        rows = {value: row for row, value in enumerate(self.hashes)}
        marks = [LitPoints(positions, 5.0, _JOINT, False)]
        if (chosen := rows.get(self.chosen)) is None:
            return tuple(marks)
        for joint, colour, weight in self.links:
            if (other := rows.get(joint)) is not None:
                width = 2.0 if weight is None else 1.0 + 3.0 * min(max(weight, 0.0), 1.0)
                marks.append(LitLines(positions[[[chosen, other]]], width, colour, False))
                marks.append(LitPoints(positions[[other]], 8.0, colour, False))
        origin, length = positions[chosen], _GUIDE * max(float(np.ptp(positions, axis=0).max()), 1e-3)
        ups, cones = self.evaluated
        for kind, *values in self.guides:
            if kind == "axis":
                marks += _ray(origin, self.worlds[chosen, :3, :3] @ np.asarray(values[0], np.float64), length,
                              values[1])
            elif kind == "world_up" and values[0] < len(ups) and ups[values[0]] is not None:
                marks += _ray(origin, ups[values[0]], length, values[1])
            elif kind == "cone" and values[0] < len(cones) and cones[values[0]] is not None and values[1]:
                (axis, measured), half = np.reshape(cones[values[0]], (2, 3)), values[1]
                marks += _cone(origin, axis, half, 1.5 * length, values[2])
                marks += _ray(origin, measured, 1.5 * length, values[3])
        marks.append(LitPoints(positions[[chosen]], 10.0, _CHOSEN, False))
        return tuple(marks)

    def _at(self, pos) -> int | None:
        if not self.hashes:
            return None
        camera, ratio = self.viewport.view_camera()
        found = nearest(*project(self.worlds[:, :3, 3], view_projection(camera), camera, ratio), pos, _PICK)
        return None if found is None else self.hashes[found]

    def press(self, event, pos) -> bool:
        if event.button() != Qt.LeftButton or (joint := self._at(pos)) is None:
            return False
        if (chosen := self.armed) is not None:
            self.arm(None)
            chosen(joint)
        else:
            self.picked.emit(joint)
        return True

    def move(self, event, pos) -> bool:
        if not event.buttons() and (joint := self._at(pos)) is not None:
            QToolTip.showText(event.globalPosition().toPoint(), self.names.get(joint, f"0x{joint:08X}"), self.viewport)
        else:
            QToolTip.hideText()
        return False

    def release(self, event, pos) -> bool:
        return False

    def wheel(self, event) -> bool:
        return False

    def key_press(self, event) -> bool:
        if self.takes_key(event):
            self.arm(None)
            return True
        return False

    def takes_key(self, event) -> bool:
        return self.armed is not None and event.key() == Qt.Key_Escape

    def leave(self) -> None:
        QToolTip.hideText()

    def gizmo(self):
        return None
