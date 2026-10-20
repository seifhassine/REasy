from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QFormLayout, QLabel, QVBoxLayout, QWidget

from collections.abc import Callable, Mapping

lazy from file_handlers.mmtr.material_preview import PreviewMaterial
lazy from file_handlers.mmtr.shown_image import target_lanes


def preview_material(surface, parameters: Callable[[], Mapping[str, float]] | None = None) -> PreviewMaterial:
    context = surface.instance_context
    offsets = {parameter.name: parameter.byte_offset for parameter in context.parameters}
    values = tuple((offsets[parameter.name], tuple(float(value) for value in parameter.values))
                   for parameter in surface.parameters if parameter.name in offsets)
    animated = None if parameters is None else _animated(surface, offsets, parameters)
    return PreviewMaterial(context.layout, values, {texture.texture_type: texture.texture_path
                                                    for texture in surface.textures},
                           surface.material_flags, 0 if context.layout.shader_lod_redirects else None,
                           buffers={buffer.name: buffer.path for buffer in surface.gpu_buffers}, animated=animated)


def _animated(surface, offsets: Mapping[str, int], parameters: Callable[[], Mapping[str, float]]
              ) -> Callable[[], tuple[tuple[int, tuple[float, ...]], ...]]:
    authored = {parameter.name: tuple(float(value) for value in parameter.values) for parameter in surface.parameters}

    def values() -> tuple[tuple[int, tuple[float, ...]], ...]:
        return tuple((offsets[name], (float(value), *authored.get(name, ())[1:]))
                     for name, value in parameters().items() if name in offsets)
    return values


class MaterialShaderControls(QWidget):
    shading_shown = Signal(str)

    def __init__(self, lit, textures, parent=None, *, game_lighting: bool = True, chosen: str = "game",
                 remember: Callable[[str], None] | None = None):
        super().__init__(parent)
        self._lit, self._textures = lit, textures
        self._remember = remember
        self._view_refusal = self._mesh_refusal = self._refusal = ""
        self._refused_lines: list[str] = []
        self._formats: dict[int, int] = {}
        self._form = form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        self.mode = QComboBox(self)
        if game_lighting:
            self.mode.addItem(self.tr("Game lighting"), "game")
            self.mode.addItem(self.tr("G-buffer"), "gbuffer")
        self.mode.addItem(self.tr("Textures"), "textures")
        self.mode.setCurrentIndex(max(self.mode.findData(chosen), 0))
        self._chosen = self.mode.currentData()
        self.mode.setToolTip(self.tr(
            "G-buffer: draws the mesh as the game does, with its materials' own shaders as edited, on your graphics "
            "card, and shows one G-buffer target.\nGame lighting: the same, lit by the game's light passes from the "
            "viewer's light. No surrounding light, shadows or fog; rest pose only."))
        form.addRow(self.tr("Shading"), self.mode)
        self.intensity = QDoubleSpinBox(self)
        self.intensity.setRange(0.0, 1000.0)
        self.intensity.setDecimals(2)
        self.intensity.setSingleStep(0.5)
        self.intensity.setValue(lit.intensity)
        self.intensity.setToolTip(self.tr("The brightness of the viewer's light, in the game's units."))
        form.addRow(self.tr("Light intensity"), self.intensity)
        self.output = QComboBox(self)
        self.output.setToolTip(self.tr("Which of the G-buffer's targets to show; names come from the game's "
                                       "G-buffer where REasy has proven them."))
        form.addRow(self.tr("Output"), self.output)
        self.lanes = QComboBox(self)
        self.lanes.setToolTip(self.tr("Each channel's value is clamped to 0 to 1 and shown as that much light."))
        self._lane_names = {"rgb": self.tr("Color (RGB)"), "r": self.tr("Red"), "g": self.tr("Green"),
                            "b": self.tr("Blue"), "a": self.tr("Alpha")}
        form.addRow(self.tr("Channels"), self.lanes)
        self.status = QLabel(self)
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(form)
        layout.addWidget(self.status)
        self.mode.currentIndexChanged.connect(self._on_mode_chosen)
        self.output.currentIndexChanged.connect(self._on_output_changed)
        self.lanes.currentIndexChanged.connect(self._show_target)
        self.intensity.valueChanged.connect(lit.set_intensity)
        lit.status_changed.connect(self._check_lit)
        lit.status_changed.connect(self._show_status)
        textures.status_changed.connect(self._show_status)
        self._on_mode_changed()

    def shading(self) -> str:
        return self.mode.currentData()

    def set_refusal(self, refusal: str) -> None:
        self._view_refusal = refusal
        self._refuse()

    def retry(self) -> None:
        """Call when what Game lighting draws changed after it drew none of the mesh."""
        self._mesh_refusal, self._refused_lines = "", []
        self._refuse()

    def _check_lit(self) -> None:
        if not self._mesh_refusal and (refusal := self._lit.unavailable()):
            self._mesh_refusal, self._refused_lines = refusal, self._lit.failure_lines()
            self._refuse()

    def _refuse(self) -> None:
        refusal = self._view_refusal or self._mesh_refusal
        if refusal == self._refusal:
            return
        self._refusal = refusal
        model = self.mode.model()
        for mode in ("game", "gbuffer"):
            index = self.mode.findData(mode)
            if index >= 0:
                model.item(index).setEnabled(not refusal)
                self.mode.setItemData(index, refusal or None, Qt.ItemDataRole.ToolTipRole)
        self.mode.blockSignals(True)
        self.mode.setCurrentIndex(max(self.mode.findData("textures" if refusal else self._chosen), 0))
        self.mode.blockSignals(False)
        self._on_mode_changed()

    def _on_mode_chosen(self) -> None:
        self._chosen = self.mode.currentData()
        if self._remember is not None:
            self._remember(self._chosen)
        self._on_mode_changed()

    def _on_mode_changed(self) -> None:
        mode = self.mode.currentData()
        self._list_targets(self._lit.targets() if mode == "gbuffer" else ())
        for widget in (self.output, self.lanes):
            self._form.setRowVisible(widget, mode == "gbuffer")
        self._form.setRowVisible(self.intensity, mode == "game")
        self._lit.set_target(self._target())
        lit = mode == "game" or mode == "gbuffer" and bool(self._formats)
        # Emitted before the previews switch: turning Game lighting on may fall back to Textures, which must be reported
        # after it.
        self.shading_shown.emit(mode)
        # Turn the other preview off first, so the viewport only ever shows one preview's frames.
        for preview, chosen in sorted(((self._lit, lit), (self._textures, mode == "textures")),
                                      key=lambda item: item[1]):
            preview.set_active(chosen)
        self._show_status()

    def _list_targets(self, targets: tuple[tuple[int, str, int], ...]) -> None:
        chosen = self.output.currentData()
        self._formats = {target: form for target, _slot, form in targets}
        self.output.blockSignals(True)
        self.output.clear()
        for target, slot, _form in targets:
            self.output.addItem(slot or self.tr("Target {index}").format(index=target), target)
        self.output.setCurrentIndex(max(self.output.findData(chosen), 0) if targets else -1)
        self.output.blockSignals(False)
        self._list_lanes()

    def _list_lanes(self) -> None:
        chosen = self.lanes.currentData()
        self.lanes.blockSignals(True)
        self.lanes.clear()
        if self.output.currentIndex() >= 0:
            for value in ("rgb", *target_lanes(self._formats[self.output.currentData()])):
                self.lanes.addItem(self._lane_names[value], value)
            self.lanes.setCurrentIndex(max(self.lanes.findData(chosen), 0))
        self.lanes.blockSignals(False)

    def _on_output_changed(self) -> None:
        self._list_lanes()
        self._show_target()

    def _target(self) -> tuple[int, str] | None:
        if self.mode.currentData() != "gbuffer" or self.output.currentIndex() < 0:
            return None
        return self.output.currentData(), self.lanes.currentData()

    def _show_target(self) -> None:
        self._lit.set_target(self._target())

    def _show_status(self) -> None:
        mode = self.mode.currentData()
        if mode == "gbuffer" and not self._formats:
            self.status.setText(self.tr("REasy's catalog of this game build records no G-buffer targets."))
            return
        shown = self._textures if mode == "textures" else self._lit
        told = [self.tr("{reason} The mesh is shaded with its textures.").format(reason=self._refusal),
                *self._refused_lines] if self._refusal and self._chosen != "textures" else []
        self.status.setText("\n".join(told + shown.status() + shown.failure_lines()))


__all__ = ["MaterialShaderControls", "preview_material"]
