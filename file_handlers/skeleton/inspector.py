"""See docs/internals/skeleton-editor.md, Inspector."""
from __future__ import annotations

import math
import re
from functools import partial

lazy import numpy as np
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLineEdit, QWidget

lazy from file_handlers.engine_math import euler_rotation
lazy from utils.number_format import format_float_sequence

from ui.field_form import Field, FieldPanel

from .document import SkeletonDocument
from .pose import euler_degrees, rules
from .skeleton_file import Joint

_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _single(value) -> float:
    return float(str(np.float32(value)))


class JointInspector(FieldPanel):
    noted = Signal(str)
    pick_requested = Signal(object)

    def __init__(self, document: SkeletonDocument, parent: QWidget | None = None):
        super().__init__(parent)
        self.document = document

    def show_joints(self, joints: list[Joint]) -> None:
        if not joints:
            self.show_fields(None, self.tr("No joint selected"), [])
            return
        title = joints[-1].name if len(joints) == 1 else self.tr("{0} and {1} more").format(
            joints[-1].name, len(joints) - 1)
        self.show_fields((id(joints[-1]), len(joints)), title, self._fields(joints[-1]))
        # A number longer than its field shows its leading digits.
        for edit in self.findChildren(QLineEdit):
            if not edit.hasFocus():
                edit.home(False)

    def hang(self, joint: Joint, parent: Joint) -> None:
        try:
            self.document.reparent(joint, parent)
        except ValueError as error:
            self.noted.emit(str(error))

    def _set(self, edit, *values) -> None:
        try:
            edit(*values)
        except ValueError as error:
            self.noted.emit(str(error))
            raise

    def _vector(self, label: str, names: str, read, write, tooltip: str = "", shown=_single) -> Field:
        def typed(at: int, value: float) -> None:
            values = list(read())
            values[at] = value
            self._set(write, values)
        return Field(label, "row", None, options={"fields": [
            Field(name, "float", lambda at=at: shown(read()[at]), partial(typed, at))
            for at, name in enumerate(names)]}, tooltip=tooltip)

    def _picked(self, label: str, field: Field, chosen) -> Field:
        return Field(label, "row", None, options={"fields": [field, Field(
            "", "button", lambda: self.tr("Pick in view"), lambda _: self.pick_requested.emit(chosen))]},
            tooltip=field.tooltip)

    def _fields(self, joint: Joint) -> list[Field]:
        document, tr = self.document, self.tr
        below = set(map(id, document.under([joint])))

        def set_length(length: float) -> None:
            if not (now := math.hypot(*joint.position)):
                raise ValueError(tr("The joint sits on its parent, so it has no direction to lengthen along: move "
                                    "it first."))
            document.place(joint, "position", [value * length / now for value in joint.position])

        def set_quaternion(text: str) -> None:
            if len(parts := text.replace(",", " ").split()) != 4 or not all(map(_NUMBER.fullmatch, parts)):
                raise ValueError(tr("Type the four numbers x, y, z, w."))
            document.place(joint, "rotation", [float(part) for part in parts])
        fields = [Field(tr("Name"), "text", lambda: joint.name, partial(self._set, document.rename, joint)),
                  Field(tr("Name hash"), "readonly", lambda: f"0x{joint.name_hash:08X}"),
                  Field(tr("Index"), "readonly", lambda: document.skeleton.rows()[id(joint)])]
        if parents := {held: held.name for held in document.joints if id(held) not in below}:
            fields.append(self._picked(tr("Parent"), Field(
                "", "choice", lambda: joint.parent, partial(self.hang, joint), parents,
                tr("The joint this one hangs on. Changing it keeps the joint where it stands in the model.")),
                partial(self.hang, joint)))
        fields += [
            self._picked(tr("Mirror"), Field(
                "", "choice", lambda: joint.mirror or "", lambda partner: document.pair(joint, partner or None),
                {"": tr("None")} | {held: held.name for held in document.joints},
                tr("The joint a mirrored motion swaps this one with, set on both: itself on the centre line, None for "
                   "a joint mirrored motions leave alone.")), partial(document.pair, joint)),
            Field(tr("On its parent"), "section", None),
            self._vector(tr("Position"), "XYZ", lambda: joint.position, partial(document.place, joint, "position"),
                         tr("In the parent's axes, in metres.")),
            self._vector(tr("Rotation"), "XYZ", lambda: euler_degrees(joint.rotation), lambda values: document.place(
                joint, "rotation", euler_rotation([math.radians(angle) for angle in values])),
                tr("Degrees, in the order of the engine's LocalEulerAngle: turned about Z, then X, then Y."),
                lambda angle: round(angle, rules()["angles"]["digits"]) + 0.0),
            Field(tr("Quaternion"), "text", lambda: format_float_sequence(map(_single, joint.rotation)),
                  partial(self._set, set_quaternion),
                  tooltip=tr("The stored rotation as x, y, z, w; it is kept at length 1.")),
            self._vector(tr("Scale"), "XYZ", lambda: joint.scale, partial(document.place, joint, "scale"),
                         tr("On the joint's own axes; the joints under it take it too.")),
            Field(tr("Length"), "float", lambda: _single(math.hypot(*joint.position)),
                  partial(self._set, set_length), tooltip=tr("The distance to the parent, in metres.")),
            Field(tr("In the model"), "section", None),
            self._vector(tr("Position"), "XYZ", lambda: document.worlds()[document.skeleton.rows()[id(joint)], :3, 3],
                         partial(document.move_to, joint),
                         tr("Where the joint stands in the skeleton's own space, in metres."))]
        if document.skeleton.segment_scales:
            fields.append(Field(
                tr("Segment scale"), "bool", lambda: joint.segment_scale,
                lambda on: document.set_values(joint, tr("Set segment scale"), segment_scale=on), tooltip=tr(
                    "On: a scaled parent stretches this joint's offset but not the joint itself, as Maya's Segment "
                    "Scale Compensate. The game reads it only on objects whose Transform has Joint Segment Scale on.")))
        return fields
