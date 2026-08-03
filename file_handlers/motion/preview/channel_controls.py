from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..runtime import MotionChannelActivation, MotionChannelKind
from .entity_session import (
    PreviewChannelChoice,
    PreviewMotionChannel,
    ResolvedMotionTarget,
    build_preview_motion_layer,
)


@dataclass(slots=True)
class MotionChannelControl:
    channel: PreviewMotionChannel
    label: QLabel
    combo: QComboBox


class MotionChannelPanel(QScrollArea):
    """Arbitrary semantic channel controls for one selected Motion target."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._active = False
        self.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        self.sync_normalized_time = QCheckBox(
            self.tr("Sync normalized layer time")
        )
        self.sync_normalized_time.setToolTip(
            self.tr(
                "Manually request SyncBaseLayerNormalizeTime using each "
                "layer's authored source. The PFB does not store whether "
                "gameplay requested this mode."
            )
        )
        self.sync_normalized_time.toggled.connect(self.changed)
        layout.addWidget(self.sync_normalized_time)
        self.form = QFormLayout()
        self.form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        layout.addLayout(self.form)
        self.setWidget(body)
        self.controls: list[MotionChannelControl] = []

    def set_target(self, target: ResolvedMotionTarget | None) -> None:
        while self.form.rowCount():
            self.form.removeRow(0)
        self.controls.clear()
        self._active = False
        channels = tuple(
            channel
            for channel in (target.channels if target is not None else ())
            if channel.choices
        )
        with QSignalBlocker(self.sync_normalized_time):
            self.sync_normalized_time.setChecked(False)
        self.sync_normalized_time.setVisible(
            any(channel.normalized_time_source is not None for channel in channels)
        )
        selected_layers: set[int] = set()
        for channel in sorted(channels, key=self._sort_key):
            definition = channel.definition
            layer = (
                self.tr("runtime layer")
                if definition.layer_index is None
                else self.tr("Layer {index}").format(
                    index=definition.layer_index
                )
            )
            label = QLabel(
                self.tr("{channel} · {layer}").format(
                    channel=definition.label,
                    layer=layer,
                )
            )
            combo = QComboBox()
            combo.addItem(self.tr("Not applied"), None)
            selected = 0
            for index, choice in enumerate(channel.choices):
                combo.addItem(
                    self.tr(
                        "{choice} · Bank {bank} / ID {motion_id} · {motion}"
                    ).format(
                        choice=choice.label,
                        bank=definition.bank_id,
                        motion_id=choice.motion.motion_id,
                        motion=choice.motion.name,
                    ),
                    choice.key,
                )
                if (
                    definition.activation is MotionChannelActivation.ACTIVE
                    and definition.default_choice_key == choice.key
                    and definition.layer_index not in selected_layers
                    and not channel.preview_blocker
                ):
                    selected = index + 1
            if selected and definition.layer_index is not None:
                selected_layers.add(definition.layer_index)
            combo.setCurrentIndex(selected)
            combo.setToolTip(
                "\n".join(
                    item
                    for item in (
                        self.tr("Activation: {state}").format(
                            state=definition.activation.label
                        ),
                        definition.provider_type,
                        channel.preview_blocker,
                        *channel.diagnostics,
                    )
                    if item
                )
            )
            control = MotionChannelControl(channel, label, combo)
            combo.currentIndexChanged.connect(
                lambda _index, current=control: self._on_changed(current)
            )
            self.form.addRow(label, combo)
            self.controls.append(control)
        if not channels:
            label = QLabel(
                self.tr("No semantic channels bind to this Motion target.")
            )
            label.setWordWrap(True)
            self.form.addRow(label)
        self._sync_enabled_state()

    @property
    def has_options(self) -> bool:
        return bool(self.controls)

    def selected_choices(
        self,
    ) -> tuple[tuple[PreviewMotionChannel, PreviewChannelChoice], ...]:
        if not self._active:
            return ()
        result = []
        for control in self.controls:
            key = control.combo.currentData()
            if not isinstance(key, str):
                continue
            choice = next(
                (
                    item
                    for item in control.channel.choices
                    if item.key == key
                ),
                None,
            )
            if choice is not None:
                result.append((control.channel, choice))
        return tuple(result)

    def selected_layers(self):
        synchronize = self.sync_normalized_time.isChecked()
        return tuple(
            build_preview_motion_layer(
                channel,
                choice,
                synchronize_normalized_time=synchronize,
            )
            for channel, choice in self.selected_choices()
        )

    def _on_changed(self, changed: MotionChannelControl) -> None:
        layer = changed.channel.definition.layer_index
        if changed.combo.currentData() is not None and layer is not None:
            for control in self.controls:
                if (
                    control is not changed
                    and control.channel.definition.layer_index == layer
                    and control.combo.currentData() is not None
                ):
                    with QSignalBlocker(control.combo):
                        control.combo.setCurrentIndex(0)
        self.changed.emit()

    def set_active(self, active: bool) -> None:
        active = active and bool(self.controls)
        if active == self._active:
            return
        self._active = active
        self._sync_enabled_state()
        self.changed.emit()

    def _sync_enabled_state(self) -> None:
        self.sync_normalized_time.setEnabled(self._active)
        for control in self.controls:
            control.combo.setEnabled(
                self._active
                and bool(control.channel.choices)
                and not control.channel.preview_blocker
            )

    @staticmethod
    def _sort_key(channel: PreviewMotionChannel) -> tuple[int, int, str]:
        priority = {
            MotionChannelKind.FACE_EXPRESSION: 0,
            MotionChannelKind.LIP_SYNC: 1,
            MotionChannelKind.BLINK: 2,
            MotionChannelKind.AUTHORED_SOURCE: 3,
            MotionChannelKind.MOTION_FSM: 4,
            MotionChannelKind.ACTOR_MOTION: 5,
        }
        layer = channel.definition.layer_index
        return (
            layer if layer is not None else 1 << 30,
            priority[channel.definition.kind],
            channel.definition.label,
        )
