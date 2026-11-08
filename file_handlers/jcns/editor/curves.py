"""See docs/internals/jcns-editor.md."""

from __future__ import annotations

lazy import numpy as np
from PySide6.QtCore import QCoreApplication, QObject

from ui.curve_view import CURVE_COLORS, CurveSource

from ..derived import rows
from ..edits import JointEdits
from ..evaluate import program
from ..joints import JointView
from ..model import Record
from ..rules import evaluation_rules, stored

MIN_MAX = (("in_min", "out_min"), ("in_mid", "out_mid"), ("in_max", "out_max"))


translate = QCoreApplication.translate


class _Curve:
    def __init__(self, source: DriverCurves, driver: Record, cone: bool, index: int, color: str):
        self.source, self.driver, self.cone, self.index, self.colors = source, driver, cone, index, (color,)
        self.keyframes = rows(driver, "keyframes") if source.keyed(driver, cone) else None
        if self.keyframes is not None:
            points = [(key.values["frame"], key.values["value"]) for key in self.keyframes]
        else:
            points = [(float(at) if cone else driver.values[x], driver.values[y]) for at, (x, y) in self.points()]
        self.frames = np.array([x for x, _ in points], np.float32)
        self.values = np.array([y for _, y in points], np.float32).reshape(-1, 1)

    def points(self) -> list[tuple[int, tuple[str, str]]]:
        values = self.driver.values
        if self.cone:
            return [(0, MIN_MAX[0]), (1, MIN_MAX[2])]
        if values.get("curve_type", self.source.min_max) != self.source.min_max:
            return []
        middle = values["flags"] & self.source.mid_point
        return [(at, pair) for at, pair in enumerate(MIN_MAX) if at != 1 or middle]

    def sample_many(self, inputs) -> np.ndarray:
        return np.array(self.source.program.sample(self.cone, self.index, np.asarray(inputs, np.float64).tolist()),
                        np.float32).reshape(-1, 1)

    def handles(self, key: int) -> list[tuple[float, float, float, float]]:
        """A keyframe's in and out tangents (Hermite, over the segment), a third as Bezier handles."""
        if self.keyframes is None:
            return []
        values = self.keyframes[key].values
        x, y = values["frame"], values["value"]
        return [(x, y, x - values["ix"] / 3, y - values["iy"] / 3), (x, y, x + values["ox"] / 3, y + values["oy"] / 3)]


class DriverCurves(CurveSource):
    def __init__(self, edits: JointEdits, parent: QObject | None = None):
        super().__init__(parent)
        self.edits = edits
        model, enums = edits.document.model, edits.enums
        self.by_keys = evaluation_rules(model.version, enums.game).get("function_curves") == "by_keyframes"
        self.min_max = enums.of_field(edits.layouts["joint_driver"], "curve_type").get("MinMax", 0)
        self.mid_point = enums.of_field(edits.layouts["joint_driver"], "flags").get("MidPoint", 0)
        self.program = None
        self.shown: list[_Curve] = []
        self.live: tuple[list, list] = ([], [])
        self._merge = self._index = None

    def keyed(self, driver: Record, cone: bool) -> bool:
        if self.by_keys and not cone:
            return bool(rows(driver, "keyframes"))
        return "keyframes" in driver.links and driver.values.get("curve_type", self.min_max) != self.min_max

    def show(self, view: JointView, target: tuple | None, fit: bool = True) -> None:
        model = self.edits.document.model
        self.program = program(model, self.edits.enums)
        numbers, counts = {}, [0, 0]
        for output in rows(model.root, "outputs"):
            for cone, link in ((True, "cone_drivers"), (False, "joint_drivers")):
                for driver in rows(output, link):
                    numbers[id(driver)] = cone, counts[cone]
                    counts[cone] += 1
        drivers = [(driver, *numbers[id(driver)]) for card in view.by_target.get(target, ()) if card.kind == "output"
                   for output in card.records for link in ("joint_drivers", "cone_drivers")
                   for driver in rows(output, link)]
        self.shown = [_Curve(self, driver, cone, index, CURVE_COLORS[at % len(CURVE_COLORS)])
                      for at, (driver, cone, index) in enumerate(drivers)]
        self.reload(fit)

    def set_live(self, values: tuple[list, list]) -> None:
        self.live = values
        self.moved.emit()

    def curves(self) -> list[_Curve]:
        return self.shown

    def extent(self) -> tuple[float, float]:
        frames = [curve.frames for curve in self.shown if len(curve.frames)]
        return (float(min(map(np.min, frames))), float(max(map(np.max, frames)))) if frames else (-1.0, 1.0)

    def points(self) -> list[tuple[float, float, str]]:
        found = []
        for curve in self.shown:
            values = self.live[curve.cone]
            if curve.index < len(values) and values[curve.index] is not None:
                found.append((*values[curve.index], curve.colors[0]))
        return found

    def _merged(self, merge, apply) -> None:
        """Each step of one drag replaces the last: the drag is one undo step."""
        stack = self.edits.document.undo_stack
        if merge is not None and merge == self._merge and stack.index() == self._index and stack.canUndo():
            stack.undo()
        apply()
        self._merge, self._index = merge, stack.index()

    def move_keys(self, track: int, keys: list[int], frames, values, dx: float, dy: float, component: int,
                  merge) -> list[int]:
        curve = self.shown[track]
        frames, values = frames.astype(np.float64), values[:, 0].astype(np.float64)
        frames[keys] += dx
        values[keys] += dy
        if curve.keyframes is None:
            fields = {}
            for key in keys:
                _at, (x, y) = curve.points()[key]
                fields.update({y: values[key]} if curve.cone else {x: frames[key], y: values[key]})
            self._merged(merge, lambda: self.edits.set_fields(curve.driver, fields,
                                                              translate("JcnsEditor", "Move points")))
            return keys
        order = sorted(range(len(frames)), key=lambda at: frames[at])

        def moved() -> None:
            original = rows(curve.driver, "keyframes")
            self.edits.set_rows(curve.driver, "keyframes", [self._key(original[at], frame=frames[at], value=values[at])
                                                            for at in order], translate("JcnsEditor", "Move keys"))
        self._merged(merge, moved)
        return [order.index(key) for key in keys]

    def move_handle(self, track: int, key: int, handle: int, x: float, y: float, merge) -> None:
        keyframe = self.shown[track].keyframes[key]
        (frame, value), sign = (keyframe.values["frame"], keyframe.values["value"]), 1 if handle else -1
        fields = dict(zip(("ox", "oy") if handle else ("ix", "iy"), ((x - frame) * 3 * sign, (y - value) * 3 * sign)))
        self._merged(merge, lambda: self.edits.set_fields(keyframe, fields, translate("JcnsEditor", "Move tangent")))

    def _key(self, key: Record, **values) -> Record:
        layout = self.edits.layouts[key.kind]
        return Record(key.kind, {**key.values, **{name: stored(layout, name, value) for name, value in values.items()}},
                      dict(key.links))

    def add_key(self, track: int, x: float) -> None:
        curve = self.shown[track]
        if curve.keyframes is None:
            return
        keys, at = list(curve.keyframes), int(np.searchsorted(curve.frames, x))
        before = x - keys[at - 1].values["frame"] if at else 1.0
        after = keys[at].values["frame"] - x if at < len(keys) else 1.0
        added = self.edits.new("keyframe", frame=x, value=float(curve.sample_many([x])[0, 0]), ix=before, ox=after)
        if at:
            keys[at - 1] = self._key(keys[at - 1], ox=before)
        if at < len(keys):
            keys[at] = self._key(keys[at], ix=after)
        self.edits.set_rows(curve.driver, "keyframes", [*keys[:at], added, *keys[at:]],
                            translate("JcnsEditor", "Add key"))

    def delete_keys(self, picked: dict[int, list[int]]) -> None:
        with self.edits.document.macro(translate("JcnsEditor", "Delete keys")):
            for track, keys in picked.items():
                curve = self.shown[track]
                if curve.keyframes is not None:
                    self.edits.set_rows(curve.driver, "keyframes", [key for at, key in enumerate(curve.keyframes)
                                                                    if at not in keys],
                                        translate("JcnsEditor", "Delete keys"))
