from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..profiles import MotionFormatProfile
from .entity_session import EntityMotionSession, ResolvedMotionTarget
from .resolution import PreviewMotionEntry


class MotionRuntimeDebugDialog(QDialog):
    """Optional plain-text inspection of the resolved animation runtime."""

    def __init__(
        self,
        profile: MotionFormatProfile,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.profile = profile
        self.setWindowTitle(self.tr("Advanced animation info"))
        self.setModal(False)
        self.resize(760, 620)

        layout = QVBoxLayout(self)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.text.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        )
        layout.addWidget(self.text, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.hide)
        layout.addWidget(buttons)

    def set_runtime(
        self,
        session: EntityMotionSession | None,
        current: ResolvedMotionTarget | None,
        entry: PreviewMotionEntry | None = None,
        *,
        preview_messages: Iterable[str] = (),
        scene_message: str = "",
    ) -> None:
        self.text.setPlainText(
            build_motion_runtime_debug_report(
                self.profile,
                session,
                current,
                entry,
                preview_messages=preview_messages,
                scene_message=scene_message,
            )
        )


def build_motion_runtime_debug_report(
    profile: MotionFormatProfile,
    session: EntityMotionSession | None,
    current: ResolvedMotionTarget | None,
    entry: PreviewMotionEntry | None = None,
    *,
    preview_messages: Iterable[str] = (),
    scene_message: str = "",
) -> str:
    lines = [
        "FORMAT",
        f"  Profile: {profile.name}",
        (
            f"  MOTLIST v{profile.motlist.version} · MOT v{profile.mot.version} "
            f"· MotTree v{profile.mot_tree.version} · "
            f"MotClip v{profile.mot_clip.version}"
        ),
    ]
    if session is None:
        lines.extend(("", "RUNTIME", "  No PFB motion session is loaded."))
        return "\n".join(lines)

    lines.extend(
        (
            "",
            "RUNTIME",
            f"  Source: {session.source_path}",
            f"  Motion targets: {len(session.targets)}",
            (
                "  Material controllers: "
                f"{len(session.definition.material_controllers)}"
            ),
            (
                "  Scene-state bindings: "
                f"{len(session.definition.scene_state_bindings)}"
            ),
        )
    )
    for target in session.targets:
        lines.extend(_target_lines(session, target, target is current))

    if entry is not None:
        motion = entry.loaded_motion
        lines.extend(
            (
                "",
                "SELECTED ANIMATION",
                f"  Name: {entry.name or '(unnamed)'}",
                f"  Motion ID: {entry.motion_id}",
                f"  Bank ID: {entry.bank_id if entry.bank_id is not None else 'None'}",
                f"  Source: {entry.source_path}",
                f"  Origin: {entry.origin.value}",
            )
        )
        if motion is None:
            lines.append("  Content: deferred until this animation is previewed")
        else:
            lines.extend((
                f"  Frames: 0–{motion.end_frame:g}",
                (
                    "  Content: "
                    f"{len(motion.animation_nodes)} joint nodes, "
                    f"{len(motion.property_tracks)} property tracks, "
                    f"{len(motion.sequences)} sequences"
                ),
            ))
            if motion.character_path:
                lines.append(f"  Character/JMAP path: {motion.character_path}")

    preview = tuple(dict.fromkeys(
        (
            *preview_messages,
            *(line for line in (scene_message.strip(),) if line),
        )
    ))
    if preview:
        lines.extend(("", "PREVIEW STATE"))
        lines.extend(f"  {message}" for message in preview)

    diagnostics = tuple(dict.fromkeys(
        (
            *session.diagnostics,
            *(message for target in session.targets for message in target.diagnostics),
        )
    ))
    lines.extend(("", "DIAGNOSTICS"))
    lines.extend(
        (f"  - {message}" for message in diagnostics)
        if diagnostics
        else ("  None",)
    )
    return "\n".join(lines)


def _target_lines(
    session: EntityMotionSession,
    target: ResolvedMotionTarget,
    selected: bool,
) -> list[str]:
    target_definition = target.definition
    marker = "  *" if selected else "  -"
    lines = [
        "",
        (
            f"{marker} {target_definition.name or 'Motion target'} "
            f"(Motion #{target_definition.id.component_instance_id})"
        ),
        f"      State: {'enabled' if target_definition.enabled else 'disabled'}",
        f"      GameObject ID: {target_definition.id.object_id}",
        (
            "      MOTBANK: "
            f"{target.motion_bank_path or target_definition.motion_bank_path or 'None'}"
        ),
        f"      Resolved animations: {len(target.motions)}",
        (
            "      JMAP: "
            f"{target.joint_map_path or target_definition.joint_map_path or 'None'}"
        ),
        (
            f"      Playback: {target_definition.play_speed:g}× · "
            f"root motion {target_definition.root_motion_mode.label}"
        ),
    ]
    if target.joint_map is not None:
        joint_map = target.joint_map
        lines.append(
            f"      JMAP model: v{joint_map.version} · "
            f"{joint_map.bone_count} bones · "
            f"{len(joint_map.mask_groups)} masks · "
            f"{joint_map.extra_joint_count} extra joints"
        )
    lines.append(f"      Layers: {len(target_definition.layers)}")
    for slot in target_definition.layers:
        layer = slot.definition
        lines.append(
            (
                f"        [{slot.index}] {layer.blend_mode.label} · "
                f"weight {layer.weight:g} · mask {layer.joint_mask_id}"
                if layer is not None
                else f"        [{slot.index}] {slot.diagnostic or 'unresolved'}"
            )
        )
    lines.append(f"      Animation channels: {len(target.channels)}")
    for channel in target.channels:
        channel_definition = channel.definition
        layer = (
            str(channel_definition.layer_index)
            if channel_definition.layer_index is not None
            else "runtime"
        )
        lines.append(
            f"        {channel_definition.label}: {len(channel.choices)} choices · "
            f"layer {layer} · {channel_definition.activation.label}"
        )
        if channel.preview_blocker:
            lines.append(f"          Blocked: {channel.preview_blocker}")
    observers = session.definition.observers_for(target_definition.id)
    lines.append(f"      Layer observers: {len(observers)}")
    return lines
