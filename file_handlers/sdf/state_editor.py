from __future__ import annotations

from PySide6.QtCore import QCoreApplication, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

lazy from .sdf_file import (
    BLEND_NAMES,
    BLEND_OPERATION_NAMES,
    COMPARISON_FUNCTION_NAMES,
    CULL_MODE_NAMES,
    DEPTH_WRITE_MASK_NAMES,
    FILL_MODE_NAMES,
    STENCIL_OPERATION_NAMES,
)
lazy from ui.styles import set_style_role


def _combo_value(combo: QComboBox) -> int:
    return int(combo.currentData())


def select_preserved(combo: QComboBox, value: int, text: str) -> None:
    index = combo.findData(value)
    if index < 0:
        combo.addItem(text, value)
        index = combo.count() - 1
    combo.setCurrentIndex(index)


def _populate_enum(combo: QComboBox, table, names, current: int) -> None:
    combo.clear()
    seen: set[int] = set()
    for public_value in table:
        value = int(public_value)
        if value in seen:
            continue
        seen.add(value)
        combo.addItem(names.get(value, f"value-{value}"), value)
    select_preserved(combo, int(current), QCoreApplication.translate(
        "SdfStateEditor", "unrecognized ({value}; preserved)").format(value=int(current)))


class SdfStateEditor(QWidget):
    state_edit_requested = Signal(str, object, bool)
    state_preview_changed = Signal(str, object)
    blend_target_preview_changed = Signal(int)

    def __init__(self, parent=None, *, scoped: bool = True):
        super().__init__(parent)
        self._program = None
        self._profile = None
        self._syncing = False
        self._blend_values: list[dict[str, object]] = []
        self._active_blend_target = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        self.scope = QComboBox(self)
        self.scope.addItem(self.tr("This pass only"), True)
        self.scope.addItem(self.tr("All passes using this preset"), False)
        if scoped:
            help_label = QLabel(
                self.tr(
                    "Each pass normally receives its own render-state preset. Choose the shared "
                    "preset only when every linked pass should change together."
                )
            )
            help_label.setWordWrap(True)
            set_style_role(help_label, tone="muted")
            root.addWidget(help_label)
            scope_row = QHBoxLayout()
            scope_row.addWidget(QLabel(self.tr("Edit scope:")))
            scope_row.addWidget(self.scope, 1)
            root.addLayout(scope_row)
        else:
            self.scope.hide()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        host = QWidget()
        self.forms = QVBoxLayout(host)
        self.forms.setContentsMargins(4, 4, 4, 4)
        self._build_rasterizer()
        self._build_blend()
        self._build_depth()
        self._connect_preview_signals()
        self.forms.addStretch(1)
        scroll.setWidget(host)
        root.addWidget(scroll, 1)

    def _connect_preview_signals(self) -> None:
        for combo in (self.fill_mode, self.cull_mode):
            combo.currentIndexChanged.connect(self._preview_rasterizer)
        for checkbox in (
            self.front_ccw,
            self.depth_clip,
            self.scissor,
            self.multisample,
            self.aa_line,
        ):
            checkbox.toggled.connect(self._preview_rasterizer)
        for editor in (self.depth_bias, self.depth_bias_clamp, self.slope_bias):
            editor.valueChanged.connect(self._preview_rasterizer)

        for checkbox in (
            self.alpha_to_coverage,
            self.independent_blend,
            self.blend_enable,
            *self.write_channels,
        ):
            checkbox.toggled.connect(self._preview_blend)
        for combo in (
            self.source_blend,
            self.destination_blend,
            self.blend_operation,
            self.source_alpha,
            self.destination_alpha,
            self.alpha_operation,
        ):
            combo.currentIndexChanged.connect(self._preview_blend)

        self.depth_enable.toggled.connect(self._preview_depth)
        self.depth_write.currentIndexChanged.connect(self._preview_depth)
        self.depth_function.currentIndexChanged.connect(self._preview_depth)
        self.depth_bounds.toggled.connect(self._preview_depth)
        self.stencil_enable.toggled.connect(self._preview_depth)
        self.stencil_read.valueChanged.connect(self._preview_depth)
        self.stencil_write.valueChanged.connect(self._preview_depth)

    def _preview_rasterizer(self, *_args) -> None:
        if self._syncing or self._program is None:
            return
        self.state_preview_changed.emit(
            "rasterizer",
            {
                "fill_mode": self.fill_mode.currentText(),
                "cull_mode": self.cull_mode.currentText(),
                "front_counter_clockwise": self.front_ccw.isChecked(),
                "depth_clip_enable": self.depth_clip.isChecked(),
                "scissor_enable": self.scissor.isChecked(),
                "multisample_enable": self.multisample.isChecked(),
                "antialiased_line_enable": self.aa_line.isChecked(),
                "depth_bias": self.depth_bias.value(),
            },
        )

    def _preview_blend(self, *_args) -> None:
        if self._syncing or self._program is None or not self._blend_values:
            return
        self._save_blend_target()
        self.state_preview_changed.emit(
            "blend",
            {
                "target_index": self._active_blend_target,
                "blend_enable": self.blend_enable.isChecked(),
                "source_blend": self.source_blend.currentText(),
                "destination_blend": self.destination_blend.currentText(),
                "blend_operation": self.blend_operation.currentText(),
                "source_alpha_blend": self.source_alpha.currentText(),
                "destination_alpha_blend": self.destination_alpha.currentText(),
                "alpha_blend_operation": self.alpha_operation.currentText(),
                "write_mask": sum(
                    1 << bit
                    for bit, checkbox in enumerate(self.write_channels)
                    if checkbox.isChecked()
                ),
            },
        )

    def _preview_depth(self, *_args) -> None:
        if self._syncing or self._program is None:
            return
        self.state_preview_changed.emit(
            "depth",
            {
                "depth_enable": self.depth_enable.isChecked(),
                "depth_write_mask": self.depth_write.currentText(),
                "depth_function": self.depth_function.currentText(),
                "depth_bounds_test_enable": self.depth_bounds.isChecked(),
                "stencil_enable": self.stencil_enable.isChecked(),
                "stencil_read_mask": self.stencil_read.value(),
                "stencil_write_mask": self.stencil_write.value(),
            },
        )

    def _spin(self, minimum: int, maximum: int, *, hexadecimal=False) -> QSpinBox:
        widget = QSpinBox()
        widget.setRange(minimum, maximum)
        if hexadecimal:
            widget.setDisplayIntegerBase(16)
            widget.setPrefix("0x")
        return widget

    def _double(self) -> QDoubleSpinBox:
        widget = QDoubleSpinBox()
        widget.setDecimals(6)
        widget.setRange(-1.0e9, 1.0e9)
        widget.setSingleStep(0.05)
        return widget

    def _shared_label(self) -> QLabel:
        label = QLabel()
        label.setWordWrap(True)
        set_style_role(label, tone="muted")
        return label

    def _build_rasterizer(self) -> None:
        self.raster_group = QGroupBox(self.tr("Rasterizer"))
        form = QFormLayout(self.raster_group)
        self.raster_shared = self._shared_label()
        form.addRow(self.tr("Ownership"), self.raster_shared)
        self.fill_mode, self.cull_mode = QComboBox(), QComboBox()
        form.addRow(self.tr("Fill mode"), self.fill_mode)
        form.addRow(self.tr("Cull mode"), self.cull_mode)
        self.front_ccw = QCheckBox(self.tr("Counter-clockwise triangles are front-facing"))
        self.depth_clip = QCheckBox(self.tr("Clip against near/far depth planes"))
        self.scissor = QCheckBox(self.tr("Apply scissor rectangle"))
        self.multisample = QCheckBox(self.tr("Multisample rasterization"))
        self.aa_line = QCheckBox(self.tr("Antialiased lines"))
        self.conservative = QCheckBox(self.tr("Conservative rasterization"))
        self.binner_control = QLabel()
        self.binner_control.setToolTip(self.tr(
            "Read-only BinnerControl: written as this build's sampled value. The engine's Direct3D rasterizer setup "
            "ignores these bits; other effects are unverified."
        ))
        form.addRow(self.tr("Primitive binning (BinnerControl)"), self.binner_control)
        for checkbox in (self.front_ccw, self.depth_clip, self.scissor, self.multisample, self.aa_line, self.conservative):
            form.addRow(checkbox)
        self.depth_bias = self._spin(-0x80000000, 0x7FFFFFFF)
        self.depth_bias_clamp = self._double()
        self.slope_bias = self._double()
        form.addRow(self.tr("Constant depth bias"), self.depth_bias)
        form.addRow(self.tr("Depth-bias clamp"), self.depth_bias_clamp)
        form.addRow(self.tr("Slope-scaled bias"), self.slope_bias)
        button = QPushButton(self.tr("Apply rasterizer state"))
        set_style_role(button, variant="primary")
        button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        button.clicked.connect(self._apply_rasterizer)
        form.addRow(button)
        self.forms.addWidget(self.raster_group)

    def _build_blend(self) -> None:
        self.blend_group = QGroupBox(self.tr("Blend / render targets"))
        form = QFormLayout(self.blend_group)
        self.blend_shared = self._shared_label()
        form.addRow(self.tr("Ownership"), self.blend_shared)
        self.alpha_to_coverage = QCheckBox(self.tr("Alpha-to-coverage"))
        self.independent_blend = QCheckBox(self.tr("Independent state per render target"))
        form.addRow(self.alpha_to_coverage)
        form.addRow(self.independent_blend)
        self.blend_target = QComboBox()
        for index in range(8):
            self.blend_target.addItem(self.tr("Render target {index}").format(index=index), index)
        self.blend_target.currentIndexChanged.connect(self._blend_target_changed)
        form.addRow(self.tr("Target"), self.blend_target)
        self.blend_enable = QCheckBox(self.tr("Enable blending"))
        form.addRow(self.blend_enable)
        self.source_blend, self.destination_blend, self.blend_operation = QComboBox(), QComboBox(), QComboBox()
        self.source_alpha, self.destination_alpha, self.alpha_operation = QComboBox(), QComboBox(), QComboBox()
        form.addRow(self.tr("Source color factor"), self.source_blend)
        form.addRow(self.tr("Destination color factor"), self.destination_blend)
        form.addRow(self.tr("Color operation"), self.blend_operation)
        form.addRow(self.tr("Source alpha factor"), self.source_alpha)
        form.addRow(self.tr("Destination alpha factor"), self.destination_alpha)
        form.addRow(self.tr("Alpha operation"), self.alpha_operation)
        write_row = QWidget()
        write_layout = QHBoxLayout(write_row)
        write_layout.setContentsMargins(0, 0, 0, 0)
        self.write_channels: list[QCheckBox] = []
        for channel in "RGBA":
            checkbox = QCheckBox(channel)
            self.write_channels.append(checkbox)
            write_layout.addWidget(checkbox)
        write_layout.addStretch(1)
        form.addRow(self.tr("Write channels"), write_row)
        button = QPushButton(self.tr("Apply complete blend state"))
        set_style_role(button, variant="primary")
        button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        button.clicked.connect(self._apply_blend)
        form.addRow(button)
        self.forms.addWidget(self.blend_group)

    def _face_editor(self):
        page = QWidget()
        form = QFormLayout(page)
        controls = {
            "fail": QComboBox(),
            "depth_fail": QComboBox(),
            "pass": QComboBox(),
            "function": QComboBox(),
        }
        form.addRow(self.tr("Stencil fail"), controls["fail"])
        form.addRow(self.tr("Depth fail"), controls["depth_fail"])
        form.addRow(self.tr("Pass"), controls["pass"])
        form.addRow(self.tr("Comparison"), controls["function"])
        return page, controls

    def _build_depth(self) -> None:
        self.depth_group = QGroupBox(self.tr("Depth / stencil"))
        form = QFormLayout(self.depth_group)
        self.depth_shared = self._shared_label()
        form.addRow(self.tr("Ownership"), self.depth_shared)
        self.depth_enable = QCheckBox(self.tr("Enable depth test"))
        self.depth_write = QComboBox()
        self.depth_function = QComboBox()
        form.addRow(self.depth_enable)
        form.addRow(self.tr("Depth write"), self.depth_write)
        form.addRow(self.tr("Depth comparison"), self.depth_function)
        self.depth_bounds = QCheckBox(self.tr("Enable depth-bounds test"))
        self.stencil_enable = QCheckBox(self.tr("Enable stencil test"))
        form.addRow(self.depth_bounds)
        form.addRow(self.stencil_enable)
        self.stencil_read = self._spin(0, 0xFF, hexadecimal=True)
        self.stencil_write = self._spin(0, 0xFF, hexadecimal=True)
        form.addRow(self.tr("Stencil read mask"), self.stencil_read)
        form.addRow(self.tr("Stencil write mask"), self.stencil_write)
        faces = QTabWidget()
        front_page, self.front_face = self._face_editor()
        back_page, self.back_face = self._face_editor()
        faces.addTab(front_page, self.tr("Front face"))
        faces.addTab(back_page, self.tr("Back face"))
        form.addRow(faces)
        button = QPushButton(self.tr("Apply depth / stencil state"))
        set_style_role(button, variant="primary")
        button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        button.clicked.connect(self._apply_depth)
        form.addRow(button)
        self.forms.addWidget(self.depth_group)

    @staticmethod
    def _ownership(count: int) -> str:
        if count <= 1:
            return "Private to this pass"
        return f"Shared by {count} passes"

    def set_program(self, program, profile, shared_counts: dict[str, int]) -> None:
        self._syncing = True
        try:
            self._program, self._profile = program, profile
            raster, blend, depth = program.rasterizer_state, program.blend_state, program.depth_stencil_state
            self.raster_group.setEnabled(raster is not None)
            self.blend_group.setEnabled(blend is not None)
            self.depth_group.setEnabled(depth is not None)
            self.raster_shared.setText(self._ownership(shared_counts.get("rasterizer", 0)))
            self.blend_shared.setText(self._ownership(shared_counts.get("blend", 0)))
            self.depth_shared.setText(self._ownership(shared_counts.get("depth", 0)))

            if raster is not None:
                _populate_enum(self.fill_mode, profile.fill_mode_table, FILL_MODE_NAMES, raster.fill_mode_id)
                _populate_enum(self.cull_mode, profile.cull_mode_table, CULL_MODE_NAMES, raster.cull_mode_id)
                self.front_ccw.setChecked(raster.front_counter_clockwise)
                self.depth_clip.setChecked(raster.depth_clip_enable)
                self.scissor.setChecked(raster.scissor_enable)
                self.multisample.setChecked(raster.multisample_enable)
                self.aa_line.setChecked(raster.antialiased_line_enable)
                self.conservative.setChecked(raster.conservative_rasterization)
                self.conservative.setEnabled(profile.conservative_rasterization_supported)
                self.binner_control.setText(profile.rasterizer_binner_control_names[raster.binner_control]
                    if raster.binner_control else self.tr("Not specified"))
                self.depth_bias.setValue(raster.depth_bias)
                self.depth_bias_clamp.setValue(raster.depth_bias_clamp)
                self.slope_bias.setValue(raster.slope_scaled_depth_bias)

            if blend is not None:
                self.alpha_to_coverage.setChecked(blend.alpha_to_coverage_enable)
                self.independent_blend.setChecked(blend.independent_blend_enable)
                self._blend_values = [
                    {
                        "blend_enable": target.blend_enable,
                        "source_blend_id": target.source_blend_id,
                        "destination_blend_id": target.destination_blend_id,
                        "blend_operation_id": target.blend_operation_id,
                        "source_alpha_blend_id": target.source_alpha_blend_id,
                        "destination_alpha_blend_id": target.destination_alpha_blend_id,
                        "alpha_blend_operation_id": target.alpha_blend_operation_id,
                        "write_mask": target.write_mask,
                    }
                    for target in blend.render_targets
                ]
                current = int(self.blend_target.currentData() or 0)
                self._active_blend_target = min(current, len(self._blend_values) - 1)
                self.blend_target.setCurrentIndex(self._active_blend_target)
                self._load_blend_target(self._active_blend_target)

            if depth is not None:
                self.depth_enable.setChecked(depth.depth_enable)
                _populate_enum(
                    self.depth_write,
                    profile.depth_write_mask_table,
                    DEPTH_WRITE_MASK_NAMES,
                    depth.depth_write_mask_id,
                )
                _populate_enum(
                    self.depth_function,
                    profile.comparison_function_table,
                    COMPARISON_FUNCTION_NAMES,
                    depth.depth_function_id,
                )
                self.depth_bounds.setChecked(depth.depth_bounds_test_enable)
                self.stencil_enable.setChecked(depth.stencil_enable)
                self.stencil_read.setValue(depth.stencil_read_mask)
                self.stencil_write.setValue(depth.stencil_write_mask)
                self._load_face(self.front_face, depth.front_face)
                self._load_face(self.back_face, depth.back_face)
        finally:
            self._syncing = False
        self.state_preview_changed.emit("reset", None)
        if blend is not None:
            self.blend_target_preview_changed.emit(self._active_blend_target)

    def _load_blend_target(self, index: int) -> None:
        if not self._blend_values or self._profile is None:
            return
        value = self._blend_values[index]
        self.blend_enable.setChecked(bool(value["blend_enable"]))
        _populate_enum(
            self.source_blend,
            self._profile.blend_factor_table,
            BLEND_NAMES,
            int(value["source_blend_id"]),
        )
        _populate_enum(
            self.destination_blend,
            self._profile.blend_factor_table,
            BLEND_NAMES,
            int(value["destination_blend_id"]),
        )
        _populate_enum(
            self.blend_operation,
            self._profile.blend_operation_table,
            BLEND_OPERATION_NAMES,
            int(value["blend_operation_id"]),
        )
        _populate_enum(
            self.source_alpha,
            self._profile.blend_factor_table,
            BLEND_NAMES,
            int(value["source_alpha_blend_id"]),
        )
        _populate_enum(
            self.destination_alpha,
            self._profile.blend_factor_table,
            BLEND_NAMES,
            int(value["destination_alpha_blend_id"]),
        )
        _populate_enum(
            self.alpha_operation,
            self._profile.blend_operation_table,
            BLEND_OPERATION_NAMES,
            int(value["alpha_blend_operation_id"]),
        )
        mask = int(value["write_mask"])
        for bit, checkbox in enumerate(self.write_channels):
            checkbox.setChecked(bool(mask & (1 << bit)))

    def _save_blend_target(self) -> None:
        if not self._blend_values:
            return
        self._blend_values[self._active_blend_target] = {
            "blend_enable": self.blend_enable.isChecked(),
            "source_blend_id": _combo_value(self.source_blend),
            "destination_blend_id": _combo_value(self.destination_blend),
            "blend_operation_id": _combo_value(self.blend_operation),
            "source_alpha_blend_id": _combo_value(self.source_alpha),
            "destination_alpha_blend_id": _combo_value(self.destination_alpha),
            "alpha_blend_operation_id": _combo_value(self.alpha_operation),
            "write_mask": sum(1 << bit for bit, checkbox in enumerate(self.write_channels) if checkbox.isChecked()),
        }

    def _blend_target_changed(self, _index: int) -> None:
        if self._syncing or not self._blend_values:
            return
        self._save_blend_target()
        self._active_blend_target = int(self.blend_target.currentData())
        self._syncing = True
        try:
            self._load_blend_target(self._active_blend_target)
        finally:
            self._syncing = False
        self.blend_target_preview_changed.emit(self._active_blend_target)
        self._preview_blend()

    def _load_face(self, controls, face) -> None:
        _populate_enum(
            controls["fail"], self._profile.stencil_operation_table, STENCIL_OPERATION_NAMES, face.stencil_fail_operation_id
        )
        _populate_enum(
            controls["depth_fail"], self._profile.stencil_operation_table, STENCIL_OPERATION_NAMES, face.stencil_depth_fail_operation_id
        )
        _populate_enum(
            controls["pass"], self._profile.stencil_operation_table, STENCIL_OPERATION_NAMES, face.stencil_pass_operation_id
        )
        _populate_enum(
            controls["function"], self._profile.comparison_function_table, COMPARISON_FUNCTION_NAMES, face.stencil_function_id
        )

    @staticmethod
    def _face_values(controls):
        return {
            "stencil_fail_operation_id": _combo_value(controls["fail"]),
            "stencil_depth_fail_operation_id": _combo_value(controls["depth_fail"]),
            "stencil_pass_operation_id": _combo_value(controls["pass"]),
            "stencil_function_id": _combo_value(controls["function"]),
        }

    def _isolate(self) -> bool:
        return bool(self.scope.currentData())

    def _apply_rasterizer(self) -> None:
        values = {
            "fill_mode_id": _combo_value(self.fill_mode),
            "cull_mode_id": _combo_value(self.cull_mode),
            "front_counter_clockwise": self.front_ccw.isChecked(),
            "depth_clip_enable": self.depth_clip.isChecked(),
            "scissor_enable": self.scissor.isChecked(),
            "multisample_enable": self.multisample.isChecked(),
            "antialiased_line_enable": self.aa_line.isChecked(),
            "conservative_rasterization": self.conservative.isChecked(),
            "depth_bias": self.depth_bias.value(),
            "depth_bias_clamp": self.depth_bias_clamp.value(),
            "slope_scaled_depth_bias": self.slope_bias.value(),
        }
        self.state_edit_requested.emit("rasterizer", values, self._isolate())

    def _apply_blend(self) -> None:
        self._save_blend_target()
        self.state_edit_requested.emit(
            "blend",
            {
                "alpha_to_coverage_enable": self.alpha_to_coverage.isChecked(),
                "independent_blend_enable": self.independent_blend.isChecked(),
                "render_targets": list(self._blend_values),
            },
            self._isolate(),
        )

    def _apply_depth(self) -> None:
        self.state_edit_requested.emit(
            "depth",
            {
                "depth_enable": self.depth_enable.isChecked(),
                "depth_write_mask_id": _combo_value(self.depth_write),
                "depth_function_id": _combo_value(self.depth_function),
                "depth_bounds_test_enable": self.depth_bounds.isChecked(),
                "stencil_enable": self.stencil_enable.isChecked(),
                "stencil_read_mask": self.stencil_read.value(),
                "stencil_write_mask": self.stencil_write.value(),
                "front_face": self._face_values(self.front_face),
                "back_face": self._face_values(self.back_face),
            },
            self._isolate(),
        )


__all__ = ["SdfStateEditor"]
