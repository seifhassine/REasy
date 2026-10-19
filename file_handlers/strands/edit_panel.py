from __future__ import annotations

import dataclasses
import os
import threading
from functools import partial
from pathlib import Path

lazy import numpy as np
from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QProgressBar,
                               QPushButton, QSizePolicy, QSpinBox, QStackedWidget, QToolButton, QVBoxLayout, QWidget)

from file_handlers.clsp.clsp_file import ShapeKind
lazy from file_handlers.clsp.placement import hung_joints
lazy from ui.background_jobs import BackgroundJobs
lazy from utils.hash_util import murmur3_hash_utf16le
lazy from utils.resource_file_utils import resource_context_for_handler, resource_key

lazy from .bindings import StrandBindings
lazy from .chain_constants import COLLIDER_SLOTS, collider_slots
lazy from .colliders import EVERY_TAG, SetColliders
lazy from .controls import plain_label
lazy from .edit_session import EditFile, EditSession, load_session, save_target
lazy from .editing import StrandEdit
lazy from .forms import ColliderForm, LodForm, NewColliderDialog, PresetForm, PresetImportDialog
from .gpus import AnimationCurve, CurveKey, GpuStrandsPreset, gpus_version
lazy from .growing import HEIGHT, LENGTH, GrowSettings, default_fringe, default_height
from .lod import NO_RECORD, lod_records, with_lod_record
lazy from .strand_tool import BRUSHES, COLLIDERS, COMB, MODES, PAINT, SELECT, SMOOTH, TRANSFORM, StrandTool
lazy from .strands_file import StrandsFile

_PREVIEW_DELAY = 700
_ROOTS_DELAY = 300
_LINES = (20_000, 50_000, 80_000, 150_000, 300_000, 0)
_NEW_RECORD = dataclasses.replace(NO_RECORD, curve=AnimationCurve((CurveKey(0.0, 1, 0.0), CurveKey(1.0, 1, 1.0)),
                                                                   minimum=0, maximum=1, flags=1))


def _button(text: str, tip: str, slot) -> QPushButton:
    button = QPushButton(text)
    button.setToolTip(tip)
    button.clicked.connect(slot)
    return button


def _check(text: str, tip: str = "", checked: bool = False, slot=None) -> QCheckBox:
    box = QCheckBox(text)
    box.setToolTip(tip)
    box.setChecked(checked)
    if slot is not None:
        box.toggled.connect(slot)
    return box


def _spin(value: float, tip: str, low: float = 0.0, high: float = 10.0, step: float = 0.05) -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setRange(low, high)
    spin.setDecimals(3)
    spin.setSingleStep(step)
    spin.setValue(value)
    spin.setToolTip(tip)
    return spin


def _row(*widgets) -> QHBoxLayout:
    row = QHBoxLayout()
    for widget in widgets:
        row.addWidget(widget)
    return row


class StrandEditPanel(QWidget):
    """``grow_progress`` (done, total) is emitted from the growing thread."""

    editing_changed = Signal(bool)
    grow_progress = Signal(int, int)

    def __init__(self, controls, parent=None):
        super().__init__(parent)
        self.controls = controls
        self.session: EditSession | None = None
        self.edit: StrandEdit | None = None
        self.tool: StrandTool | None = None
        self.colliders: SetColliders | None = None
        self.presets: list[tuple[EditFile, GpuStrandsPreset]] = []
        self.lod_data: bytes | None = None
        # Keyed by ``resource_key``.
        self.preview_files: dict[str, tuple[str, bytes]] = {}
        self._saved, self._forms, self._game_strands, self._folder, self._presets_folder = (0, 0), 0, True, "", ""
        self._roots_held = (None, None), {}
        self._jobs = {name: BackgroundJobs(self) for name in ("load", "preview", "save", "grow", "trim", "fit",
                                                               "guide")}
        self._growing: threading.Event | None = None
        self._trimming = self._fitting = self._guiding = False
        self.grow_progress.connect(self._grow_told)
        self._preview_timer = QTimer(self, singleShot=True, interval=_PREVIEW_DELAY)
        self._preview_timer.timeout.connect(self.update_preview)
        self._roots_timer = QTimer(self, singleShot=True, interval=_ROOTS_DELAY)
        self._roots_timer.timeout.connect(self._show_roots_held)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.toggle = _check(self.tr("Edit strands"), self.tr(
            "Edit the set's strands in the viewport, its binding, colliders, physics presets and LOD settings; the "
            "edits are saved as the set's files."), slot=self._toggled)
        layout.addWidget(self.toggle)
        self.status = plain_label("")
        layout.addWidget(self.status)
        self.body = QWidget(self)
        layout.addWidget(self.body)
        self._build(QVBoxLayout(self.body))
        self.body.hide()

    def _build(self, layout: QVBoxLayout) -> None:
        layout.setContentsMargins(0, 0, 0, 0)
        grid = QGridLayout(tools := QGroupBox(self.tr("Tool")))
        self.modes = QButtonGroup(self)
        names = {SELECT: (self.tr("Select"), self.tr("Drag a box, or click a strand, to choose strands: Shift adds, "
                                                      "Ctrl takes away.")),
                 PAINT: (self.tr("Paint"), self.tr("Paint the strands under the brush chosen; Ctrl takes them away.")),
                 COMB: (self.tr("Comb"), self.tr("Drag the strands under the brush along, the roots held.")),
                 SMOOTH: (self.tr("Smooth"), self.tr("Smooth the bends of the strands under the brush.")),
                 TRANSFORM: (self.tr("Move"), self.tr("Move, turn or scale the strands chosen with the gizmo (its "
                                                      "mode is the viewport's); click or drag a box to choose.")),
                 COLLIDERS: (self.tr("Colliders"), self.tr("Click a collider to pick it, then move it with the gizmo "
                                                           "or edit it below."))}
        for number, mode in enumerate(MODES):
            text, tip = names[mode]
            button = QToolButton()
            button.setText(text)
            button.setToolTip(self.tr("{tip} Key {key}.").format(tip=tip, key=number + 1))
            button.setCheckable(True)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.modes.addButton(button, number)
            grid.addWidget(button, number // 3, number % 3)
        self.modes.button(0).setChecked(True)
        self.modes.idClicked.connect(lambda number: self._set_mode(MODES[number]))
        form = QFormLayout()
        self.radius = QSpinBox()
        self.radius.setRange(4, 600)
        self.radius.setSuffix(self.tr(" px"))
        self.radius.setToolTip(self.tr("The brush's radius on screen. Ctrl+wheel or [ and ] in the viewport."))
        self.radius.valueChanged.connect(lambda value: self.tool and self.tool.set_radius(value))
        self.strength = _spin(0.5, "", 0.05, 1.0)
        self.strength.valueChanged.connect(lambda value: self.tool and setattr(self.tool, "strength", value))
        form.addRow(self.tr("Brush radius"), self.radius)
        form.addRow(self.tr("Brush strength"), self.strength)
        grid.addLayout(form, 2, 0, 1, 3)
        self.options = {
            "keep_lengths": _check(self.tr("Keep lengths"), self.tr(
                "Combing and moving keep each strand's segments as long as they were."), True),
            "pin_roots": _check(self.tr("Pin roots"), self.tr(
                "Moving, turning and scaling bend the strands from their roots, which stay where they are."), True),
            "chosen_only": _check(self.tr("Chosen strands only"), self.tr(
                "The comb and smooth brushes touch only the strands chosen."))}
        for row, (name, box) in enumerate(self.options.items(), 3):
            box.toggled.connect(lambda on, name=name: self.tool and setattr(self.tool, name, on))
            grid.addWidget(box, row, 0, 1, 3)
        layout.addWidget(tools)

        box = QVBoxLayout(chosen := QGroupBox(self.tr("Strands chosen")))
        self.chosen = plain_label("")
        box.addWidget(self.chosen)
        box.addLayout(_row(*(_button(text, tip, partial(self._choose, which)) for text, tip, which in (
            (self.tr("All"), self.tr("Ctrl+A"), "all"), (self.tr("None"), self.tr("Ctrl+D"), "none"),
            (self.tr("Invert"), self.tr("Ctrl+I"), "invert"),
            (self.tr("Guides"), self.tr("The guide strands, which drive the others' physics"), "guides")))))
        self.group = QSpinBox()
        self.group.setRange(0, 2 ** 31 - 1)
        self.group.setToolTip(self.tr("A groom group of the strands file."))
        self.group_button = _button(self.tr("Choose groom group"), "", partial(self._choose, "group"))
        box.addLayout(_row(self.group, self.group_button))
        self.sparse = QSpinBox()
        self.sparse.setRange(2, 9999)
        self.sparse.setValue(6)
        self.sparse.setToolTip(self.tr("The most vertices at LOD 0 a strand chosen had in the file read."))
        box.addLayout(_row(self.sparse, _button(self.tr("Choose strands of few vertices"), self.tr(
            "Choose the strands that had at most this many vertices at LOD 0 in the file read. Grooms often make "
            "their fly-aways of such sparse strands."),
            partial(self._choose, "sparse"))))
        self.clipping = _button(self.tr("Choose strands through colliders"), self.tr(
            "Choose the strands passing inside colliders they collide with. Strands inside a collider holding their "
            "root are included: the game's chain throws them out, and pushing out can't fix them."),
            partial(self._choose, "clipping"))
        box.addWidget(self.clipping)
        self.far = _button(self.tr("Choose strands far from their guides"), self.tr(
            "Choose the strands lying, somewhere along them, more than 5 cm from the blend of their guides. The game "
            "draws them at that blend, so they stand out."),
            partial(self._choose, "far"))
        box.addWidget(self.far)
        box.addWidget(_button(self.tr("Frame chosen strands"), "", self._frame_chosen))
        layout.addWidget(chosen)

        grid = QGridLayout(operations := QGroupBox(self.tr("Edit the strands chosen")))
        self.length = _spin(0.9, self.tr("How many times as long the strands chosen become, their shape along them "
                                         "kept."))
        self.root_width = _spin(1.0, self.tr("How many times as wide the strands chosen become at their roots."))
        self.tip_width = _spin(1.0, self.tr("How many times as wide the strands chosen become at their tips; in "
                                            "between, the factor blends along the strand."))
        self.margin = _spin(2.0, self.tr("How far past the colliders' surfaces the strands are pushed, in "
                                         "millimetres."), 0.0, 50.0, 0.5)
        rows = ((self.tr("Length ×"), (self.length,), self.tr("Scale lengths"), self._scale_lengths),
                (self.tr("Width × root, tip"), (self.root_width, self.tip_width), self.tr("Scale widths"),
                 self._scale_widths),
                (self.tr("Margin (mm)"), (self.margin,), self.tr("Push out of colliders"), self._push_out))
        for row, (label, spins, text, slot) in enumerate(rows):
            grid.addWidget(QLabel(label), row, 0)
            grid.addLayout(_row(*spins), row, 1)
            grid.addWidget(_button(text, "", slot), row, 2)
        self.push = grid.itemAtPosition(2, 2).widget()
        self.push.setToolTip(self.tr("Move the vertices of the strands chosen out of the colliders they collide "
                                     "with, past the margin, keeping roots and lengths. A collider holding a "
                                     "strand's root is left out for that strand."))
        self.spacing = plain_label("")
        self.fit_button = _button(self.tr("Fit vertex counts"), self.tr(
            "Give each strand chosen, at every LOD, enough vertices to keep the file's own spacing (shown) along its "
            "length, never fewer than it has. Hair made longer needs this, or the far LODs cut across the scalp."),
            self._fit)
        grid.addWidget(QLabel(self.tr("Vertex spacing")), 3, 0)
        grid.addWidget(self.spacing, 3, 1)
        grid.addWidget(self.fit_button, 3, 2)
        self.guide_button = _button(self.tr("Guide anew"), self.tr(
            "Give each strand chosen the guides running closest to it now, weighed by how close. Reshaped strands "
            "are guided anew anyway when the files are made."),
            self._reguide)
        self.guide_sharpness = _spin(1.0, self.tr(
            "The power guide weights are raised to when strands are guided anew. 1 matches the game's files; above 1 "
            "a strand follows its closest guide more, so long hair parting to both sides cuts through the head less."),
            0.0, 8.0, 0.5)
        self.guide_sharpness.setDecimals(2)
        self.guide_sharpness.valueChanged.connect(self._sharpness_changed)
        grid.addWidget(QLabel(self.tr("Guide sharpness")), 4, 0)
        grid.addWidget(self.guide_sharpness, 4, 1)
        grid.addWidget(self.guide_button, 4, 2)
        self.snap = _button(self.tr("Snap roots to mesh"), self.tr(
            "Move each strand chosen whole so its root lies on the nearest point of the mesh it is bound to."),
            partial(self._operate, lambda edit, strands: edit.snap_roots(strands, self.session.surfaces[0])))
        grid.addLayout(_row(self.snap, _button(self.tr("Reset"), self.tr(
            "Give the strands chosen back the shape, widths, vertex counts and guides the file read had."),
            partial(self._operate, StrandEdit.reset)), _button(self.tr("Delete"), self.tr(
                "Delete the strands chosen (Delete); the guides are kept, as the other strands follow them."),
                self._delete)), 5, 0, 1, 3)
        self.operations = operations
        layout.addWidget(operations)

        grid = QGridLayout(grow := QGroupBox(self.tr("Grow or trim the strands chosen")))
        self.grow_mode = QComboBox()
        self.grow_mode.addItem(self.tr("To a length"), LENGTH)
        self.grow_mode.addItem(self.tr("Down to a height"), HEIGHT)
        self.grow_mode.setToolTip(self.tr("Cut each strand chosen to a length along it, or down to a height above the "
                                          "origin of the mesh it is drawn over."))
        self.grow_targets = QStackedWidget()
        self.grow_targets.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        for value, tip in ((40.0, self.tr("The length the strands are cut to.")),
                           (140.0, self.tr("The height the strands are cut down to, above the mesh's origin, at the "
                                           "back of the head."))):
            target = _spin(value, tip + " " + self.tr("Their ends are a little uneven, alike for neighbouring roots."),
                           1.0, 1000.0, 1.0)
            target.setDecimals(1)
            target.setSuffix(self.tr(" cm"))
            self.grow_targets.addWidget(target)
        self.grow_mode.currentIndexChanged.connect(self._grow_mode_changed)
        self.grow_rise = _spin(0.0, self.tr(
            "How much higher the cut is at the front of the head than at its back, rising round the head, so the hair "
            "frames the face (down to a height)."), 0.0, 100.0, 1.0)
        self.grow_rise.setDecimals(1)
        self.grow_rise.setSuffix(self.tr(" cm"))
        self.grow_fringe = _check(self.tr("Fringe at"), self.tr(
            "The front hairline's strands end at their own height, by default the cheekbones' (down to a height)."))
        self.grow_fringe_height = _spin(160.0, self.tr("The height the fringe ends at, above the mesh's origin."),
                                        1.0, 1000.0, 0.5)
        self.grow_fringe_height.setDecimals(1)
        self.grow_fringe_height.setSuffix(self.tr(" cm"))
        self.grow_fringe.toggled.connect(self.grow_fringe_height.setEnabled)
        self.grow_fringe_height.setEnabled(False)
        self.grow_keep = _spin(5.0, self.tr("How much of each strand, from its root, keeps the shape it was styled "
                                            "in; the rest falls."), 0.0, 100.0, 0.5)
        self.grow_keep.setDecimals(1)
        self.grow_keep.setSuffix(self.tr(" cm"))
        self.grow_part = _check(self.tr("Part the hair beside the face"), self.tr(
            "Hair falling in front of the face moves to the side its styled shape sweeps to."), True)
        self.grow_comb = _check(self.tr("Comb the hair off the face"), self.tr(
            "Comb the front hairline's strands over the forehead to their side and back, and the temples' back over "
            "the ears, so long hair doesn't hang or swing in front of the face."), True)
        self.grow_centre = _check(self.tr("Part in the middle"), self.tr(
            "Part the front hairline down the head's middle: each strand goes to its own root's side and, combed off "
            "the face, further back, leaving the head behind the ears."))
        self.grow_part_mix = _check(self.tr("Mix the part line"), self.tr(
            "With Part in the middle, front strands near the middle take a side at random, so strands cross the part "
            "and it reads as a line, not a strip of bare scalp."), True)
        self.grow_centre.toggled.connect(self.grow_part_mix.setEnabled)
        self.grow_part_mix.setEnabled(False)
        self.grow_close_back = _check(self.tr("Draw the back to the middle"), self.tr(
            "Draw the hair hanging down the back a little toward the middle; otherwise the game's chain opens a seam "
            "down the back as the hair settles."))
        self.grow_button = _button(self.tr("Grow"), self.tr(
            "Grow the strands chosen longer, as hair hangs: each keeps its styled shape near the root, falls under "
            "gravity onto the colliders and the head, and is cut to the length or height. Vertex counts are fitted "
            "too."), self._grow)
        self.trim_button = _button(self.tr("Trim"), self.tr(
            "Cut the strands chosen that reach past the cut back to it. A strand only gets shorter, keeping only the "
            "vertices its new length needs, and its tip stays as thin."), self._trim)
        self.grow_bar = QProgressBar()
        self.grow_bar.setRange(0, 1000)
        self.grow_bar.setTextVisible(False)
        self.grow_bar.hide()
        grid.addWidget(self.grow_mode, 0, 0)
        grid.addWidget(self.grow_targets, 0, 1)
        grid.addWidget(QLabel(self.tr("Rising to the front")), 1, 0)
        grid.addWidget(self.grow_rise, 1, 1)
        grid.addWidget(self.grow_fringe, 2, 0)
        grid.addWidget(self.grow_fringe_height, 2, 1)
        grid.addWidget(QLabel(self.tr("Kept as styled")), 3, 0)
        grid.addWidget(self.grow_keep, 3, 1)
        grid.addWidget(self.grow_part, 4, 0, 1, 2)
        grid.addWidget(self.grow_comb, 5, 0, 1, 2)
        grid.addWidget(self.grow_centre, 6, 0)
        grid.addWidget(self.grow_part_mix, 6, 1)
        grid.addWidget(self.grow_close_back, 7, 0, 1, 2)
        grid.addLayout(_row(self.grow_button, self.trim_button), 8, 0, 1, 2)
        grid.addWidget(self.grow_bar, 9, 0, 1, 2)
        self.grow_box = grow
        layout.addWidget(grow)
        self._grow_mode_changed(self.grow_mode.currentIndex())

        self.undo = _button(self.tr("Undo"), self.tr("Ctrl+Z in the viewport"), lambda: self.tool.undo())
        self.redo = _button(self.tr("Redo"), self.tr("Ctrl+Y or Ctrl+Shift+Z in the viewport"),
                            lambda: self.tool.redo())
        layout.addLayout(_row(self.undo, self.redo))

        form = QFormLayout(shown := QGroupBox(self.tr("Drawn")))
        self.lines = QComboBox()
        for count in _LINES:
            self.lines.addItem(self.tr("All") if not count else f"{count:,}", count)
        self.lines.setCurrentIndex(_LINES.index(80_000))
        self.lines.setToolTip(self.tr("How many segments are drawn at most: the guides, then as many of the other "
                                      "strands, evenly spread, as fit. The brushes and the box reach every strand, "
                                      "drawn or not."))
        self.lines.currentIndexChanged.connect(self._display)
        form.addRow(self.tr("Segments"), self.lines)
        self.shown = {"xray": _check(self.tr("Through the mesh")),
                      "isolate": _check(self.tr("Chosen strands alone")),
                      "guides_shown": _check(self.tr("Guides in orange"), self.tr(
                          "Draw the guide strands, which drive the others' physics, all of them and in their own "
                          "colour.")),
                      "colliders_shown": _check(self.tr("Colliders"), self.tr(
                          "Draw the colliders the strands collide with (green), through the mesh."), True),
                      "all_colliders": _check(self.tr("Colliders they miss"), self.tr(
                          "Draw the colliders whose tag shares no bit with the base physics preset's, or whose bits "
                          "a higher priority's preset takes (grey)."))}
        for check in self.shown.values():
            check.toggled.connect(self._display)
            form.addRow(check)
        layout.addWidget(shown)

        self.collider_box = QGroupBox(self.tr("Collider picked"))
        box = QVBoxLayout(self.collider_box)
        self.picked = plain_label("")
        box.addWidget(self.picked)
        self.collider_form = QWidget()
        box.addWidget(self.collider_form)
        self.collider_buttons = (
            _button(self.tr("Copy"), self.tr("Add a copy of the collider to its file, and pick it."), self._copy),
            _button(self.tr("Delete"), self.tr("Remove the collider from its file."), self._remove_collider))
        self.new_collider = _button(self.tr("New collider…"), self.tr(
            "Add a sphere, capsule, tapered capsule, box or plane to a collider file, on a joint, at the joint: then "
            "move it with the gizmo or edit it here."), self._new_collider)
        box.addLayout(_row(self.new_collider, *self.collider_buttons))
        self.collide_all = _button(self.tr("Collide with all colliders"), self.tr(
            "Give the base physics preset's collision tag the bits of every collider except those holding strands' "
            "roots, so long hair collides with the body too."), self._collide_with_all)
        box.addWidget(self.collide_all)
        self.roots_held = plain_label("")
        box.addWidget(self.roots_held)
        self.slots_left = plain_label("")
        box.addWidget(self.slots_left)
        self.lost = plain_label("")
        box.addWidget(self.lost)
        layout.addWidget(self.collider_box)

        box = QVBoxLayout(preview := QGroupBox(self.tr("Game lighting")))
        box.addWidget(plain_label(self.tr("While editing, the strands the game's chain draws are hidden until the "
                                          "preview is updated: they show the files as they were. Show strands, above, "
                                          "shows them.")))
        self.auto_preview = _check(self.tr("Update after each edit"), self.tr(
            "Draw the edited strands by the game's chain after each edit; drawing them again takes a moment."))
        self.preview_button = _button(self.tr("Update game preview"), "", self.update_preview)
        box.addWidget(self.auto_preview)
        box.addWidget(self.preview_button)
        layout.addWidget(preview)

        self.presets_box = QGroupBox(self.tr("Physics presets"))
        box = QVBoxLayout(self.presets_box)
        self.preset_choice = QComboBox()
        self.preset_choice.setToolTip(self.tr("The physics presets the set's physics component names (.gpus); the "
                                              "first is its base preset, whose collision tag picks its colliders."))
        self.preset_forms = QStackedWidget()
        self.preset_choice.currentIndexChanged.connect(self.preset_forms.setCurrentIndex)
        self.preset_import = _button(self.tr("Import preset…"), self.tr(
            "Replace the preset chosen whole by another physics preset file (.gpus, version 13, of any game), keeping "
            "the set's collision tag or setting one: it is saved at the set's preset path."), self._import_preset)
        box.addLayout(_row(self.preset_choice, self.preset_import))
        box.addWidget(self.preset_forms)
        layout.addWidget(self.presets_box)

        self.lod_box = QGroupBox(self.tr("LOD settings"))
        box = QVBoxLayout(self.lod_box)
        self.lod_box.setToolTip(self.tr("The set's record in the build's strands LOD settings (.stdlod), by its "
                                        "strands file's preset key. The file is the build's: saving it changes "
                                        "every set using the record."))
        self.lod_label = plain_label("")
        self.lod_add = _button(self.tr("Add a record for this set"), self.tr(
            "A record giving what the game takes without one, to edit."), self._add_lod_record)
        self.lod_form = QWidget()
        for widget in (self.lod_label, self.lod_add, self.lod_form):
            box.addWidget(widget)
        layout.addWidget(self.lod_box)

        self.save = _button(self.tr("Save edits"), self.tr(
            "Write the edited files: into the open project at their game paths, or into a folder chosen."), self._save)
        layout.addWidget(self.save)
        self.saved = plain_label("")
        layout.addWidget(self.saved)

    @property
    def editing(self) -> bool:
        return self.tool is not None

    def dirty(self) -> bool:
        return self.edit is not None and (self.edit.revision, self._forms) != self._saved

    def _set_toggle(self, on: bool) -> None:
        self.toggle.blockSignals(True)
        self.toggle.setChecked(on)
        self.toggle.blockSignals(False)

    def _toggled(self, on: bool) -> None:
        if on and not self.editing:
            self._start()
        elif not on and self.editing and not self.stop():
            self._set_toggle(True)

    def _start(self) -> None:
        controls = self.controls
        loaded, prefab = controls.loaded, controls.prefab
        world = controls.viewer.strands_world()
        if loaded is None or prefab is None or world is None:
            self.status.setText(self.tr("Show a set over its mesh first: the strands are edited where they are "
                                        "drawn."))
            self._set_toggle(False)
            return
        handler = controls.viewer.handler
        self.status.setText(self.tr("Reading the set's files…"))
        self.toggle.setEnabled(False)
        self._jobs["load"].start(partial(_opened, handler, prefab.path, loaded.entry, loaded.character[0],
                                         handler.game_version), partial(self._started, world=world), self._failed)

    def _failed(self, error: str) -> None:
        self.toggle.setEnabled(True)
        self._set_toggle(False)
        self.status.setText(self.tr("The set cannot be edited: {reason}").format(reason=error))

    def _started(self, opened, world) -> None:
        self.toggle.setEnabled(True)
        session, edit, presets = opened
        strands = edit.source
        viewport = self.controls.viewer.viewport
        self.session, self.edit, self.presets = session, edit, presets
        edit.guide_sharpness = self.guide_sharpness.value()
        self.colliders = SetColliders.of(session.scene, session.colliders, world, edit.record)
        self._roots_held = (None, None), {}
        self.lod_data = None if session.lod_settings is None else session.lod_settings.data
        self._saved, self._forms, self.preview_files = (0, 0), 0, {}
        tool = self.tool = StrandTool(viewport, edit, world, self.colliders, self)
        tool.changed.connect(self._edited)
        tool.selection_changed.connect(self._chosen_changed)
        tool.mode_changed.connect(lambda mode: self.modes.button(MODES.index(mode)).setChecked(True))
        tool.options_changed.connect(lambda: self._quietly(self.radius.setValue, round(tool.radius)))
        tool.delete_requested.connect(self._delete)
        tool.collider_picked.connect(self._picked)
        for name, box in self.options.items():
            setattr(tool, name, box.isChecked())
        tool.strength = self.strength.value()
        self._quietly(self.radius.setValue, round(tool.radius))
        self._set_mode(MODES[max(self.modes.checkedId(), 0)])
        self._display()
        viewport.set_edit_tool(tool)
        self._game_strands = self.controls.enabled.isChecked()
        self.controls.enabled.setChecked(False)
        self._fill_presets()
        self._fill_lod()
        self._picked(None)
        self.group_button.setEnabled(bool(strands.groom_groups))
        self.group.setEnabled(bool(strands.groom_groups))
        self._grow_target(HEIGHT).setValue(round(default_height(edit.roots(), world) * 100, 1))
        self.grow_fringe_height.setValue(round(default_fringe(edit.roots(), world) * 100, 1))
        spacings = [(level.index, spacing * 1000) for level, spacing in zip(edit.levels, edit.spacing)]
        self.spacing.setText(" · ".join(f"{mm:.1f}" if np.isfinite(mm) else "–" for _, mm in spacings)
                             + self.tr(" mm"))
        self.spacing.setToolTip(self.tr(
            "How close a strand fitted keeps its vertices at each LOD: nine tenths of the file's own hair, by length, "
            "keeps them at least that close.") + "\n" + "\n".join(
                self.tr("LOD {lod}: {mm} mm").format(lod=lod, mm=f"{mm:.1f}" if np.isfinite(mm) else "–")
                for lod, mm in spacings))
        lines = [self.tr("{count:,} strands, {guides:,} of them guides; each LOD follows LOD 0's edits. {colliders} "
                         "colliders.").format(count=edit.strand_count, guides=min(edit.protected, edit.strand_count),
                                              colliders=len(self.colliders.entries))]
        if session.surfaces is None:
            lines.append(self.tr("The mesh it is bound to cannot be read ({reason}): strands whose roots move cannot "
                                 "be saved.").format(reason=session.surfaces_refused))
        self.status.setText("\n".join(lines))
        self.body.show()
        self._chosen_changed()
        self._edited(False)
        self._show_roots_held()
        self.editing_changed.emit(True)

    def stop(self, ask: bool = True) -> bool:
        if ask and self.dirty() and QMessageBox.question(
                self, self.tr("Edit strands"), self.tr("Discard the edits not saved?"),
                QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Cancel) != QMessageBox.Discard:
            return False
        self._preview_timer.stop()
        self._roots_timer.stop()
        if self._growing is not None:
            self._growing.set()
            self._grow_ended()
        self._trimming = self._fitting = self._guiding = False
        for name in ("load", "preview", "grow", "trim", "fit", "guide"):
            self._jobs[name].close()
            self._jobs[name] = BackgroundJobs(self)
        viewport = self.controls.viewer.viewport
        if viewport is not None and viewport.edit_tool is self.tool:
            viewport.set_edit_tool(None)
        self.tool = self.edit = self.session = self.colliders = self.lod_data = None
        self.presets, self._roots_held = [], ((None, None), {})
        had_preview, self.preview_files = bool(self.preview_files), {}
        self._fill_presets()
        self.body.hide()
        self.status.setText("")
        self.saved.setText("")
        self._set_toggle(False)
        self.editing_changed.emit(False)
        if self.controls.enabled.isChecked() != self._game_strands:
            self.controls.enabled.setChecked(self._game_strands)
        elif had_preview:
            self.controls.refresh()
        return True

    def cleanup(self) -> None:
        self.stop(ask=False)
        for jobs in self._jobs.values():
            jobs.close()

    @staticmethod
    def _quietly(setter, value) -> None:
        widget = setter.__self__
        widget.blockSignals(True)
        setter(value)
        widget.blockSignals(False)

    def _set_mode(self, mode: str) -> None:
        if self.tool is not None:
            self.tool.set_mode(mode)
        self.radius.setEnabled(mode in BRUSHES)
        self.strength.setEnabled(mode in (COMB, SMOOTH))

    def _display(self, *_) -> None:
        if self.tool is not None:
            self.tool.lines_shown = self.lines.currentData() or 10 ** 9
            for name, check in self.shown.items():
                setattr(self.tool, name, check.isChecked())
            self.tool.collision_tag = self._collision_tag()
            self.tool.refresh()

    def _collision_tag(self) -> int:
        arguments = self.session.scene.catalog.constants.arguments
        based = self.session.scene.source.dynamics[arguments["preset"]] and self.presets
        return self.presets[0][1].collision_tag if based else EVERY_TAG

    def _choose(self, which: str) -> None:
        edit = self.edit
        if edit is None:
            return
        count = edit.strand_count
        if which == "clipping":
            strands = np.arange(count)
            mask = self.colliders.through(edit, self.tool.world, strands, self._collision_tag())
            lines = [self.tr("{count:,} strands pass through colliders.").format(count=int(mask.sum()))]
            if thrown := self._thrown(strands):
                out = np.logical_or.reduce(list(thrown.values()))
                mask |= out
                lines.append(self.tr("{count:,} strands pass inside colliders holding their roots: the game's chain "
                                     "throws them out, and pushing out can't fix them. They are chosen too:\n"
                                     "{colliders}").format(count=int(out.sum()), colliders=self._listed(
                                         self.tr("{collider} · tag 0x{tag:X} · {count:,} strands"), thrown)))
            self.saved.setText("\n".join(lines))
        elif which == "far":
            mask = edit.far_from_guides()
            self.saved.setText(self.tr("{count:,} strands lie more than 5 cm from the blend of their guides.").format(
                count=int(mask.sum())))
        elif which == "group":
            groups = edit.groom_groups
            mask = groups == self.group.value() if groups is not None else np.zeros(count, bool)
        elif which == "sparse":
            mask = edit.read_counts() <= self.sparse.value()
        else:
            mask = {"all": lambda: np.ones(count, bool), "none": lambda: np.zeros(count, bool),
                    "invert": lambda: ~edit.selected, "guides": edit.guide_mask}[which]()
        self.tool.select(mask)

    def _chosen_changed(self) -> None:
        edit = self.edit
        if edit is None:
            return
        chosen = int(edit.selected.sum())
        self.chosen.setText(self.tr("{chosen:,} of {count:,} strands chosen ({guides:,} guides).").format(
            chosen=chosen, count=edit.strand_count, guides=int((edit.selected & edit.guide_mask()).sum())))
        self.operations.setEnabled(bool(chosen))
        self.fit_button.setEnabled(not self._fitting)
        self.guide_button.setEnabled(not self._guiding)
        self.grow_box.setEnabled(bool(chosen) or self._growing is not None)
        self.trim_button.setEnabled(not self._trimming and self._growing is None)
        self.snap.setEnabled(self.session.surfaces is not None)
        self.push.setEnabled(bool(self.colliders.entries))
        self.clipping.setEnabled(bool(self.colliders.entries))

    def _frame_chosen(self) -> None:
        if self.tool is not None:
            self.controls.viewer.viewport.fit_points(self.tool.selected_world_points().astype(np.float32))

    def _operate(self, operation) -> None:
        strands = np.flatnonzero(self.edit.selected)
        if len(strands):
            self.edit.end_stroke()
            operation(self.edit, strands)
            self.tool.edited()

    def _scale_lengths(self) -> None:
        factor = self.length.value()
        if factor > 0:
            self._operate(lambda edit, strands: edit.scale_lengths(strands, factor))

    def _scale_widths(self) -> None:
        root, tip = self.root_width.value(), self.tip_width.value()
        self._operate(lambda edit, strands: edit.scale_widths(strands, root, tip))

    def _push_out(self) -> None:
        margin, tag = self.margin.value() / 1000, self._collision_tag()
        self._operate(lambda edit, strands: self.colliders.push_out(edit, self.tool.world, strands, tag, margin))
        if thrown := self._thrown(np.flatnonzero(self.edit.selected)):
            self.saved.setText(self.tr("Pushing out leaves the strands chosen passing inside colliders holding their "
                                       "roots: the game's chain throws them out of those.\n{colliders}").format(
                colliders=self._listed(self.tr("{collider} · tag 0x{tag:X} · {count:,} strands"), thrown)))

    def _delete(self) -> None:
        edit = self.edit
        if edit is None or not edit.selected.any():
            return
        if not (edit.selected & ~edit.guide_mask()).any():
            self.saved.setText(self.tr("Only guides are chosen, and guides are not deleted."))
            return
        deleted = edit.delete(np.flatnonzero(edit.selected))
        self.tool.edited()
        self._chosen_changed()
        self.saved.setText(self.tr("{count:,} strands deleted.").format(count=deleted))

    def _fit(self) -> None:
        strands = np.flatnonzero(self.edit.selected) if self.edit is not None else ()
        if not len(strands) or self._fitting:
            return
        self.edit.end_stroke()
        self._fitting = True
        self.fit_button.setEnabled(False)
        self.saved.setText(self.tr("Fitting the vertex counts of {count:,} strands…").format(count=len(strands)))
        self._jobs["fit"].start(self.edit.fitting(strands), self._fit_done, self._fit_failed)

    def _fit_done(self, fitted) -> None:
        self._fit_ended()
        if not self.edit.put(fitted):
            self.saved.setText(self.tr("The strands changed while they were fitted: fit them again."))
        elif not len(fitted.strands):
            self.saved.setText(self.tr("The strands chosen already have the vertices their lengths need."))
        else:
            self.tool.edited()
            self.saved.setText(self.tr("{count:,} strands given the vertices their lengths need.").format(
                count=len(fitted.strands)))

    def _fit_failed(self, error: str) -> None:
        self._fit_ended()
        self.saved.setText(self.tr("The vertex counts cannot be fitted: {reason}").format(reason=error))

    def _fit_ended(self) -> None:
        self._fitting = False
        self._chosen_changed()

    def _reguide(self) -> None:
        strands = np.flatnonzero(self.edit.selected) if self.edit is not None else ()
        if not len(strands) or self._guiding:
            return
        self.edit.end_stroke()
        self._guiding = True
        self.guide_button.setEnabled(False)
        self.saved.setText(self.tr("Guiding {count:,} strands anew…").format(count=len(strands)))
        self._jobs["guide"].start(self.edit.reguiding(strands), self._reguided, self._reguide_failed)

    def _reguided(self, guided) -> None:
        self._guide_ended()
        if not self.edit.put(guided):
            self.saved.setText(self.tr("The strands changed while they were guided: guide them again."))
            return
        self.tool.edited()
        self.saved.setText(self.tr("{count:,} strands guided anew; {far:,} strands lie more than 5 cm from the blend "
                                   "of their guides.").format(count=len(guided.strands),
                                                              far=int(self.edit.far_from_guides().sum())))

    def _reguide_failed(self, error: str) -> None:
        self._guide_ended()
        self.saved.setText(self.tr("The strands cannot be guided anew: {reason}").format(reason=error))

    def _guide_ended(self) -> None:
        self._guiding = False
        self._chosen_changed()

    def _sharpness_changed(self, value: float) -> None:
        if self.edit is None:
            return
        self.edit.guide_sharpness = value
        # Sharpness reweights the rows, so the saved files change.
        self._forms += 1
        self._edited()

    def _grow_target(self, mode: str) -> QDoubleSpinBox:
        return self.grow_targets.widget(self.grow_mode.findData(mode))

    def _grow_mode_changed(self, index: int) -> None:
        self.grow_targets.setCurrentIndex(index)
        height = self.grow_mode.itemData(index) == HEIGHT
        self.grow_rise.setEnabled(height)
        self.grow_fringe.setEnabled(height)
        self.grow_fringe_height.setEnabled(height and self.grow_fringe.isChecked())

    def _cut_settings(self) -> GrowSettings:
        mode = self.grow_mode.currentData()
        return GrowSettings(mode, self._grow_target(mode).value() / 100, self.grow_keep.value() / 100,
                            part=self.grow_part.isChecked(), comb=self.grow_comb.isChecked(),
                            centre_part=self.grow_centre.isChecked(), part_mix=self.grow_part_mix.isChecked(),
                            close_back=self.grow_close_back.isChecked(), rise=self.grow_rise.value() / 100,
                            fringe=self.grow_fringe_height.value() / 100 if self.grow_fringe.isChecked() else None)

    def _trim(self) -> None:
        strands = np.flatnonzero(self.edit.selected) if self.edit is not None else ()
        if not len(strands) or self._trimming:
            return
        self.edit.end_stroke()
        self._trimming = True
        self.trim_button.setEnabled(False)
        self.saved.setText(self.tr("Trimming {count:,} strands…").format(count=len(strands)))
        self._jobs["trim"].start(self.edit.trimming(strands, self.tool.world, self._cut_settings()), self._trimmed,
                                 self._trim_failed)

    def _trimmed(self, trimmed) -> None:
        self._trim_ended()
        if not self.edit.put(trimmed):
            self.saved.setText(self.tr("The strands changed while they were trimmed: trim them again."))
        elif not len(trimmed.strands):
            self.saved.setText(self.tr("No strand chosen reaches past the cut."))
        else:
            self.tool.edited()
            self.saved.setText(self.tr("{count:,} strands trimmed.").format(count=len(trimmed.strands)))

    def _trim_failed(self, error: str) -> None:
        self._trim_ended()
        self.saved.setText(self.tr("The strands cannot be trimmed: {reason}").format(reason=error))

    def _trim_ended(self) -> None:
        self._trimming = False
        self._chosen_changed()

    def _grow(self) -> None:
        if self._growing is not None:
            self._growing.set()
            return
        strands = np.flatnonzero(self.edit.selected) if self.edit is not None else ()
        if not len(strands):
            return
        self.edit.end_stroke()
        tag, settings = self._collision_tag(), self._cut_settings()
        shapes = [shape for shape in self.colliders.placed() if shape.tag & tag]
        surface = None if self.session.surfaces is None else self.session.surfaces[0]
        grow = self.edit.growth(strands, self.tool.world, shapes, surface, settings)
        self._growing = stop = threading.Event()
        self.grow_button.setText(self.tr("Stop growing"))
        self.grow_bar.setValue(0)
        self.grow_bar.show()
        self.saved.setText(self.tr("Growing {count:,} strands…").format(count=len(strands)))
        self._jobs["grow"].start(partial(grow, self.grow_progress.emit, stop.is_set), self._grown, self._grow_failed)

    def _grow_told(self, done: int, total: int) -> None:
        if self._growing is not None:
            self.grow_bar.setRange(0, total)
            self.grow_bar.setValue(done)

    def _grown(self, grown) -> None:
        self._grow_ended()
        if not self.edit.put(grown):
            self.saved.setText(self.tr("The strands changed while they grew: grow them again."))
            return
        self.tool.edited()
        lines = [self.tr("{count:,} strands grown.").format(count=len(grown.strands))]
        if grown.conformed:
            lines.append(self.tr("{count:,} of them ran far from the guides grown with them, so they take the "
                                 "guides' blend, as the game's chain draws them.").format(
                count=grown.conformed))
        if self._collidable() & ~self._collision_tag():
            lines.append(self.tr("The base physics preset's tag misses some colliders, so the grown hair falls "
                                 "through them. Use Collide with all colliders, then grow again."))
        self.saved.setText("\n".join(lines))

    def _grow_failed(self, error: str) -> None:
        stopped = self._growing is not None and self._growing.is_set()
        self._grow_ended()
        self.saved.setText(self.tr("Growing stopped.") if stopped else
                           self.tr("The strands cannot grow: {reason}").format(reason=error))

    def _grow_ended(self) -> None:
        self._growing = None
        self.grow_button.setText(self.tr("Grow"))
        self.grow_bar.hide()
        self._chosen_changed()

    def _picked(self, number) -> None:
        self.collider_box.setVisible(bool(self.colliders and self.colliders.files))
        form = None
        if number is not None:
            shape, text = self.colliders.shape(number), self._named(number)
            if not self.colliders.reaching(number) & self._collision_tag():
                text += self.tr(" · the strands miss it")
            if lost := self.colliders.lost_to(shape.read_from):
                text += self.tr(" · its bits do not reach the hair: {losses}").format(losses=self._losses(lost))
            if (held := self._held().get(number)) is not None:
                text += self.tr(" · holds the roots of {count:,} strands").format(count=int(held.sum()))
            self.picked.setText(text)
            form = ColliderForm(shape, self.colliders.joints)
            form.changed.connect(partial(self._collider_edited, number))
            form.rejointed.connect(partial(self._rejointed, number))
        else:
            self.picked.setText(self.tr("Pick a collider with the Colliders tool (key 6) to move it with the gizmo "
                                        "or edit it here, in its joint's space."))
        self.collider_form = self._replace(self.collider_form, form or QWidget())
        for button in self.collider_buttons:
            button.setEnabled(number is not None)

    def _named(self, number: int) -> str:
        shape, colliders = self.colliders.shape(number), self.colliders
        joints = [colliders.joints.get(key, f"{key:08X}") for key in hung_joints(shape)]
        return self.tr("{kind} on {joints} · {file} #{index}").format(
            kind=ShapeKind(shape.kind).name.replace("_", " ").capitalize(), joints=", ".join(joints),
            file=Path(shape.read_from[0].replace("\\", "/")).name, index=shape.read_from[1])

    def _holding_named(self, holding) -> str:
        if holding is None:
            return self.tr("no preset")
        file = Path(holding.path.replace("\\", "/")).name
        if not holding.holder:
            return self.tr("{file}, which the character's user files name directly, below every preset").format(
                file=file)
        return self.tr("{file}, of the preset of priority {priority} in {prefab}").format(
            file=file, priority=holding.priority, prefab=Path(holding.holder.replace("\\", "/")).name)

    def _losses(self, lost) -> str:
        return "; ".join(self.tr("0x{bits:X} to {holding}").format(bits=bits, holding=self._holding_named(holding))
                         for bits, holding in lost)

    def _show_lost(self) -> None:
        colliders = self.colliders
        lost = colliders.lost()
        self.lost.setText(self.tr(
            "These colliders reach no strand: a higher-priority preset takes all their tag bits. Give them a bit no "
            "loaded file holds.\n{colliders}").format(colliders="\n".join(
                self.tr("{collider} · tag 0x{tag:X} · {losses}").format(
                    collider=self._named(number), tag=colliders.shape(number).tag,
                    losses=self._losses(colliders.lost_to(colliders.shape(number).read_from)))
                for number in lost)) if lost else "")
        self.lost.setVisible(bool(lost))

    def _warn_lost(self, at: tuple[str, int], bits: int) -> None:
        lost = [(taken & bits, holding) for taken, holding in self.colliders.lost_to(at) if taken & bits]
        if not lost:
            return
        free = self.colliders.free_bit()
        lines = [self.saved.text(), self.tr(
            "A higher priority's preset takes tag bits of the shape: {losses}. The game gives each bit to the shapes "
            "of the highest priority's presets holding a shape on it, so the shape does not reach the hair on "
            "them.").format(losses=self._losses(lost)),
            self.tr("No file loaded holds bit 0x{bit:X}: give the shape that bit, and the base physics preset's "
                    "collision tag too.").format(bit=free) if free is not None else
            self.tr("Every tag bit is held by a file loaded.")]
        self.saved.setText("\n".join(line for line in lines if line))

    def _held(self) -> dict[int, np.ndarray]:
        roots = self.edit.roots()
        (revision, measured), held = self._roots_held
        if revision != self.colliders.revision or not np.array_equal(measured, roots):
            held = self.colliders.rooted(self.edit, self.tool.world, np.arange(self.edit.strand_count), EVERY_TAG)
            self._roots_held = (self.colliders.revision, roots), held
        return held

    def _collidable(self) -> int:
        """Root-holding colliders are left out: the game would throw strands out of them."""
        colliders = self.colliders
        return colliders.tags() & ~colliders.tags(self._held())

    def _thrown(self, strands: np.ndarray) -> dict[int, np.ndarray]:
        thrown = self.colliders.thrown(self.edit, self.tool.world, strands, self._collision_tag())
        return {number: out for number, out in thrown.items() if out.any()}

    def _listed(self, line: str, colliders: dict[int, np.ndarray]) -> str:
        """``line`` must already be translated."""
        held = self._held()
        return "\n".join(line.format(collider=self._named(number), tag=self.colliders.shape(number).tag,
                                     count=int(strands.sum()), held=int(held[number].sum()))
                         for number, strands in colliders.items())

    def _show_roots_held(self) -> None:
        self._roots_timer.stop()
        if self.edit is None:
            return
        thrown = self.colliders.thrown(self.edit, self.tool.world, np.arange(self.edit.strand_count),
                                       self._collision_tag())
        self.roots_held.setText(self.tr(
            "The strands collide with colliders holding their roots, and the game's chain throws the strands inside "
            "them out. Move or shrink these colliders off the roots, or take their bits off the base physics "
            "preset's collision tag.\n{colliders}").format(colliders=self._listed(
                self.tr("{collider} · tag 0x{tag:X} · holds the roots of {held:,} strands, {count:,} of them passing "
                        "inside it"), thrown)) if thrown else "")
        self.roots_held.setVisible(bool(thrown))

    def _collider_edited(self, number: int, shape) -> None:
        was = self.colliders.shape(number).tag
        self.colliders.put(number, shape)
        self.tool.edited()
        if shape.tag & ~was:
            self._warn_lost(self.colliders.entries[number][1], shape.tag & ~was)

    def _rejointed(self, number: int, joint: int, end_joint: int) -> None:
        self.colliders.rejoint(number, joint, end_joint)
        self.tool.edited()

    def _show_slots(self) -> None:
        scene, colliders, tag = self.session.scene, self.colliders, self._collision_tag()
        values = {name: scene.source.dynamics[field] for name, field in scene.catalog.constants.arguments.items()}
        used = collider_slots(values, colliders.taken(), tag)
        unplaced = collider_slots(values, colliders.away(), tag)
        kinds = {ShapeKind.SPHERE: self.tr("Spheres"), ShapeKind.CAPSULE: self.tr("Capsules and tapered capsules"),
                 ShapeKind.BOX: self.tr("Boxes"), ShapeKind.PLANE: self.tr("Planes")}
        lines = [self.tr("{kinds}: the strands' collision tag hits {hit} ({unplaced} of them on joints REasy does not "
                         "load) and the set's own colliders take {own} of the {slots} slots: {left} are left out.")
                 .format(kinds=kinds[kind], hit=hit, unplaced=unplaced[kind][1], own=own, slots=COLLIDER_SLOTS,
                         left=own + hit - COLLIDER_SLOTS)
                 for kind, (own, hit) in used.items() if own + hit > COLLIDER_SLOTS]
        self.slots_left.setText("\n".join([self.tr(
            "The game's chain takes at most {slots} colliders of each kind for a set, its own first, then those its "
            "tag hits in the order their presets are registered; those past them it leaves out. Take bits off the "
            "tag, or colliders off its bits.").format(slots=COLLIDER_SLOTS), *lines]) if lines else "")
        self.slots_left.setVisible(bool(lines))

    def _new_collider(self) -> None:
        colliders, picked = self.colliders, self.tool.picked
        shape = None if picked is None else colliders.shape(picked)
        files = list(colliders.files)
        joints = list(colliders.joints.values())
        joint = colliders.joints.get(shape.joint, "") if shape is not None else ""
        dialog = NewColliderDialog(files, joints, files[0] if shape is None else shape.read_from[0],
                                   joint or (joints[0] if joints else ""),
                                   self._collision_tag() if shape is None else shape.tag, self)
        made = dialog.made if dialog.exec() == QDialog.Accepted else None
        dialog.deleteLater()
        if made is not None:
            self.add_collider(*made)

    def add_collider(self, kind: int, path: str, joint: str, end_joint: str, tag: int, size: float) -> int | None:
        colliders = self.colliders
        number = colliders.add(kind, path, murmur3_hash_utf16le(joint), murmur3_hash_utf16le(end_joint), tag, size)
        self.tool.edited()
        shown = ShapeKind(kind).name.replace("_", " ").lower()
        at = self.tr("{file} #{index}").format(file=Path(path.replace("\\", "/")).name,
                                                index=len(colliders.files[path]) - 1)
        if number is None:
            missing = ", ".join(named for named in dict.fromkeys((joint, end_joint))
                                if murmur3_hash_utf16le(named) not in colliders.joints)
            self.saved.setText(self.tr(
                "A {kind} on {joints} is added to {at}. The mesh the strands are bound to has no joint {missing}: "
                "REasy can't place the shape, but it is saved, and the game hangs it on the character's joint of "
                "that name.").format(
                    kind=shown, joints=", ".join(dict.fromkeys((joint, end_joint))), at=at, missing=missing))
            self._warn_lost((path, len(colliders.files[path]) - 1), tag)
            return None
        self.modes.button(MODES.index(COLLIDERS)).setChecked(True)
        self._set_mode(COLLIDERS)
        self.tool.pick(number)
        self.saved.setText(self.tr("A {kind} on {joints} is added to {at}, at its joint: move it with the gizmo or "
                                   "edit it below.").format(kind=shown, joints=", ".join(dict.fromkeys(
                                       (joint, end_joint))), at=at))
        self._warn_lost((path, len(colliders.files[path]) - 1), tag)
        return number

    @staticmethod
    def _replace(old: QWidget, new: QWidget) -> QWidget:
        old.parentWidget().layout().replaceWidget(old, new)
        old.deleteLater()
        return new

    def _collide_with_all(self) -> None:
        preset, colliders = self.presets[0][1], self.colliders
        left = {number: strands for number, strands in self._held().items()
                if not colliders.shape(number).tag & preset.collision_tag}
        preset.collision_tag |= self._collidable()
        self._fill_presets()
        self._form_edited()
        self._picked(self.tool.picked)
        if left:
            self.saved.setText(self.tr("The bits of colliders holding strands' roots are left out: the game's chain "
                                       "would throw the strands inside them out.\n{colliders}")
                               .format(colliders=self._listed(self.tr("{collider} · tag 0x{tag:X} · holds the roots "
                                                                      "of {count:,} strands"), left)))

    def _copy(self) -> None:
        self.tool.pick(self.colliders.copy(self.tool.picked))
        self.tool.edited()

    def _remove_collider(self) -> None:
        number = self.tool.picked
        self.tool.pick(None)
        self.colliders.remove(number)
        self.tool.edited()

    def _form_edited(self) -> None:
        self._forms += 1
        self._display()
        self._edited()
        self._show_roots_held()

    def _import_preset(self) -> None:
        index = self.preset_choice.currentIndex()
        if index < 0:
            return
        path, _filter = QFileDialog.getOpenFileName(self, self.tr("Import a physics preset"), self._presets_folder,
                                                    self.tr("Physics presets (*.gpus *.gpus.*);;All files (*)"))
        if not path:
            return
        self._presets_folder = str(Path(path).parent)
        try:
            preset = self.read_preset(path)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, self.tr("Import preset"), self.tr("{name} cannot be imported: {reason}").format(
                name=Path(path).name, reason=error))
            return
        dialog = PresetImportDialog(Path(path).name, self.presets[index][1].collision_tag, preset.collision_tag, self)
        tag = dialog.tag if dialog.exec() == QDialog.Accepted else None
        dialog.deleteLater()
        if tag is not None:
            self.import_preset(index, preset, tag)

    @staticmethod
    def read_preset(path: str) -> GpuStrandsPreset:
        return GpuStrandsPreset.read(Path(path).read_bytes(), gpus_version(path))

    def import_preset(self, index: int, preset: GpuStrandsPreset, tag: int) -> None:
        file = self.presets[index][0]
        preset.collision_tag = tag
        self.presets[index] = (file, preset)
        self._fill_presets()
        self.preset_choice.setCurrentIndex(index)
        self._form_edited()
        self.saved.setText(self.tr("The preset imported is the set's {name} now: Save edits writes it over that "
                                   "file.").format(name=Path(file.found.replace("\\", "/")).name))

    def _fill_presets(self) -> None:
        self.preset_choice.clear()
        while self.preset_forms.count():
            # ``removeWidget`` leaves the form parented to the stack; unparent it so Python alone owns it.
            widget = self.preset_forms.widget(0)
            self.preset_forms.removeWidget(widget)
            widget.setParent(None)
        for file, preset in self.presets:
            form = PresetForm(preset)
            form.changed.connect(self._form_edited)
            self.preset_forms.addWidget(form)
            self.preset_choice.addItem(Path(file.found.replace("\\", "/")).name, file.found)
        self.presets_box.setVisible(bool(self.presets))

    def _fill_lod(self) -> None:
        self.lod_box.setVisible(self.lod_data is not None)
        if self.lod_data is None:
            return
        key = self.edit.source.preset_name_hash
        record = lod_records(self.lod_data).get(key)
        self.lod_label.setText(self.tr("The record of key {key:08X}.").format(key=key) if record is not None else
                               self.tr("The file holds no record of the set's key {key:08X}: the game takes its "
                                       "defaults.").format(key=key))
        self.lod_add.setVisible(record is None)
        form = None
        if record is not None:
            form = LodForm(record)
            form.changed.connect(self._lod_edited)
        self.lod_form = self._replace(self.lod_form, form or QWidget())

    def _lod_edited(self, record) -> None:
        self.lod_data = with_lod_record(self.lod_data, self.edit.source.preset_name_hash, record)
        self._form_edited()

    def _add_lod_record(self) -> None:
        self._lod_edited(_NEW_RECORD)
        self._fill_lod()

    def _edited(self, schedule: bool = True) -> None:
        edit = self.edit
        if edit is None:
            return
        self.undo.setEnabled(edit.can_undo())
        self.redo.setEnabled(edit.can_redo())
        self.save.setEnabled(self.dirty())
        self.saved.setText(self.tr("Edits not saved.") if self.dirty() else "")
        picked = self.tool.picked
        if picked is not None and (picked >= len(self.colliders.entries) or not _same(
                getattr(self.collider_form, "shape", None), self.colliders.shape(picked))):
            if picked < len(self.colliders.entries):
                self._picked(picked)
            else:
                self.tool.pick(None)
        self.collide_all.setEnabled(bool(self._collidable() & ~self._collision_tag()))
        self._show_slots()
        self._show_lost()
        self._roots_timer.start()
        if schedule and self.auto_preview.isChecked():
            self._preview_timer.start()

    def _export(self):
        edit, session = self.edit, self.session
        found = {file.found: file for file in session.colliders}
        made = [*((file, preset.to_bytes()) for file, preset in self.presets),
                *((found[path], data) for path, data in self.colliders.changed_files())]
        if self.lod_data is not None:
            made.append((session.lod_settings, self.lod_data))
        files = [(file, data) for file, data in made if data != file.data]
        base, moved = edit.binding_base(), edit.moved_roots()
        if len(moved) and session.surfaces is None:
            self.saved.setText(self.tr("Roots moved, and the mesh they are bound to cannot be read: {reason}")
                               .format(reason=session.surfaces_refused))
            return None
        return partial(_with_binding, (session.strands, edit.writing()), files, session.binding, base,
                       session.surfaces, edit.roots(moved).copy(), moved)

    def update_preview(self) -> None:
        finish = None if self.edit is None else self._export()
        if finish is not None:
            self.preview_button.setEnabled(False)
            self._jobs["preview"].start(finish, self._previewed, self._preview_failed)

    def _previewed(self, files) -> None:
        self.preview_button.setEnabled(True)
        self.preview_files = {resource_key(file.found): (file.found, data) for file, data in files}
        if self.controls.enabled.isChecked():
            self.controls.refresh()
        else:
            self.controls.enabled.setChecked(True)

    def _preview_failed(self, error: str) -> None:
        self.preview_button.setEnabled(True)
        self.saved.setText(error)

    def edited_files(self) -> dict[str, tuple[str, bytes]]:
        return dict(self.preview_files)

    def _save(self) -> None:
        finish = None if self.edit is None else self._export()
        if finish is None:
            return
        revision = (self.edit.revision, self._forms)
        self.save.setEnabled(False)
        self.saved.setText(self.tr("Making the files…"))
        self._jobs["save"].start(finish, partial(self._write, revision=revision), self._preview_failed)

    def _write(self, files, revision) -> None:
        self.save.setEnabled(self.dirty())
        project = str(getattr(resource_context_for_handler(self.controls.viewer.handler), "project_dir", "") or "")
        targets = [save_target(file.found, project, self._folder) for file, _ in files]
        if None in targets and not project:
            folder = QFileDialog.getExistingDirectory(self, self.tr("Save the edited files into (their natives/ paths "
                                                                    "are kept)"), self._folder)
            if not folder:
                self.saved.setText(self.tr("Not saved."))
                self.save.setEnabled(True)
                return
            self._folder = folder
            targets = [save_target(file.found, project, folder) for file, _ in files]
        written = []
        try:
            for (file, data), target in zip(files, targets):
                name = Path(file.found.replace("\\", "/")).name
                target = target or QFileDialog.getSaveFileName(self, self.tr("Save {name}").format(name=name),
                                                               name)[0]
                if target:
                    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                    Path(target).write_bytes(data)
                    written.append(target)
        except OSError as error:
            self.save.setEnabled(True)
            self.saved.setText(self.tr("Saving failed: {reason}").format(reason=error))
            return
        if len(written) == len(files):
            self._saved = revision
        self.preview_files = {resource_key(file.found): (file.found, data) for file, data in files}
        self._edited(False)
        self.saved.setText("\n".join([self.tr("Saved:"), *written]) if files else
                           self.tr("Nothing differs from the files read."))
        self.controls.refresh()


def _same(shape, other) -> bool:
    return (shape is not None and shape.tag == other.tag and shape.kind == other.kind
            and (shape.joint, shape.end_joint) == (other.joint, other.end_joint)
            and np.array_equal(shape.values, other.values))


def _opened(handler, prefab_path: str, entry, character_path: str, game: str):
    """Runs on a worker thread."""
    session = load_session(handler, prefab_path, entry, character_path, game)
    strands = StrandsFile()
    strands.read(session.strands.data)
    presets = [(file, GpuStrandsPreset.read(file.data, gpus_version(file.found)))
               for file in session.presets]
    return session, StrandEdit(strands, StrandBindings.read(session.binding.data)), presets


def _with_binding(strands, files, file: EditFile, base: StrandBindings, surfaces, roots, moved):
    strands_file, write = strands
    made = write().to_bytes()
    data = (base.rebound(surfaces, roots, moved) if len(moved) else base).to_bytes()
    return ([(strands_file, made)] if made != strands_file.data else []) + files + (
        [(file, data)] if data != file.data else [])


__all__ = ["StrandEditPanel"]
