from __future__ import annotations

from collections import Counter
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QInputDialog,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.scene.scene_preview import ScenePreviewWidget
from utils.resource_file_utils import resolve_handler_resource_data
from file_handlers.mesh.material_session import (
    MeshMaterialCollection,
    MeshMaterialSession,
)

from ..evaluation import (
    rig_from_motion_skeleton,
)
from ..mot.model import Motion
from .catalog import MotionPreviewCatalog
from .blend_shapes import mesh_blend_shape_targets
from .animation_browser import MotionEntryList
from .controller import MotionPreviewController
from .controls import MotionPlaybackControls
from .editor_layout import MotionEditorPane, MotionEditorWorkspace
from .model import (
    MotionPreviewError,
    snapshot_diagnostic_messages,
    snapshot_status_messages,
)
from .resolution import (
    MotionListDocument,
    PreviewMotionEntry,
    PreviewMotionOrigin,
)
from .renderer import MotionPreviewRenderer
from .support_registry import entity_motion_support_for_format
from .target import RigPreviewTarget, load_re_engine_mesh_target


ViewportFactory = Callable[..., QWidget]


class MotListPreviewWidget(QWidget):
    """Interactive MOT preview backed by a game-specific evaluation profile."""

    modified_changed = Signal(bool)

    def __init__(
        self,
        handler,
        *,
        viewport_factory: ViewportFactory = ScenePreviewWidget,
    ):
        super().__init__()
        self.handler = handler
        format_codec = handler.motlist_file.codec
        support = entity_motion_support_for_format(format_codec)
        if support is None:
            raise ValueError(
                f"no preview support is registered for "
                f"{format_codec.profile.name}"
            )
        self.evaluation_profile = support.evaluation
        self.controller = MotionPreviewController(self.evaluation_profile)
        self.playback = MotionPlaybackControls(self.controller, parent=self)
        self.playback.render_requested.connect(self._render)
        self._motions: list[PreviewMotionEntry] = []
        self._target: RigPreviewTarget | None = None
        self._target_material_session: MeshMaterialSession | None = None
        self._using_source_rig = True
        self._cleaned = False
        root_path = str(getattr(handler, "filepath", "") or handler.model.name)
        self._catalog = MotionPreviewCatalog(
            MotionListDocument(root_path, handler.model),
            support.tree_references,
            format_codec,
            app=getattr(handler, "app", None),
            selection_parent=self,
            resource_context=getattr(handler, "resource_context", None),
        )
        self._build_ui(viewport_factory)
        self.playback.set_frame_driver(
            self.viewport.set_frame_callback,
        )
        self.viewport.render_failure.connect(
            self._on_render_failure,
            Qt.ConnectionType.QueuedConnection,
        )
        self._materials = MeshMaterialCollection(self.viewport, parent=self)
        self.viewport.texture_quality_changed.connect(
            self._materials.set_texture_quality
        )
        self._scene_renderer = MotionPreviewRenderer(self.viewport)
        self._populate_motions()

    def _build_ui(self, viewport_factory: ViewportFactory) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        settings = getattr(getattr(self.handler, "app", None), "settings", None)
        self.workspace = MotionEditorWorkspace(
            self.tr("MOTLIST Editor  ·  {name}").format(
                name=self.handler.model.name or self.tr("Untitled")
            ),
            settings=settings if isinstance(settings, dict) else None,
            parent=self,
        )
        root.addWidget(self.workspace)

        self.animation_pane = MotionEditorPane(self.tr("Animations"), self)
        self.animation_pane.setMinimumWidth(300)
        self.animation_pane.setMaximumWidth(470)
        self.motion_browser = MotionEntryList(self.animation_pane)
        self.motion_browser.selection_changed.connect(self._on_motion_changed)
        self.motion_browser.entry_activated.connect(
            self._play_selected_animation
        )
        self.animation_pane.add_widget(self.motion_browser, 1)
        self.animation_pane.add_widget(self.playback)

        self.viewport_pane = MotionEditorPane(self.tr("Viewport"), self)
        self.viewport_pane.setProperty("role", "viewport")
        self.status_label = QLabel()
        self.status_label.setObjectName("motionStatusBar")
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        self.rig_pane = MotionEditorPane(self.tr("Preview rig"), self)
        self.rig_pane.setMinimumWidth(250)
        self.rig_pane.setMaximumWidth(390)
        source_title = QLabel(self.tr("MOTION SOURCE"))
        source_title.setObjectName("motionInspectorLabel")
        self.rig_pane.add_widget(source_title)
        self.motion_source_label = QLabel()
        self.motion_source_label.setWordWrap(True)
        self.motion_source_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.rig_pane.add_widget(self.motion_source_label)
        rig_title = QLabel(self.tr("ACTIVE RIG"))
        rig_title.setObjectName("motionInspectorLabel")
        self.rig_pane.add_widget(rig_title)
        self.rig_label = QLabel(self.tr("MOT source skeleton"))
        self.rig_label.setWordWrap(True)
        self.rig_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.rig_pane.add_widget(self.rig_label)
        self.load_resource_button = QPushButton(self.tr("Load Mesh Resource…"))
        self.load_resource_button.clicked.connect(self._load_mesh_resource)
        self.rig_pane.add_widget(self.load_resource_button)
        self.source_rig_button = QPushButton(self.tr("Use MOT Skeleton"))
        self.source_rig_button.clicked.connect(self.use_source_rig)
        self.rig_pane.add_widget(self.source_rig_button)
        self.target_rig_button = QPushButton(self.tr("Use Loaded Mesh"))
        self.target_rig_button.setEnabled(False)
        self.target_rig_button.clicked.connect(self.use_target_rig)
        self.rig_pane.add_widget(self.target_rig_button)
        self.blend_shapes_toggle = QCheckBox(self.tr("Blend shapes"))
        self.blend_shapes_toggle.setChecked(True)
        self.blend_shapes_toggle.setEnabled(False)
        self.blend_shapes_toggle.hide()
        self.blend_shapes_toggle.setToolTip(
            self.tr("Enable MOT-driven mesh blend shapes")
        )
        self.blend_shapes_toggle.toggled.connect(
            self._on_blend_shapes_toggled
        )
        self.rig_pane.add_widget(self.blend_shapes_toggle)
        self.rig_pane.body_layout.addStretch(1)

        self.viewport = viewport_factory(
            self.viewport_pane,
            controls="rcol",
            settings=settings if isinstance(settings, dict) else None,
        )
        self.viewport.setMinimumHeight(320)
        self.viewport_pane.add_widget(self.viewport, 1)
        self.viewport_pane.add_widget(self.status_label)

        self.workspace.add_pane(self.animation_pane, 0)
        self.workspace.add_pane(self.viewport_pane, 1)
        self.workspace.add_pane(self.rig_pane, 0)
        self.workspace.splitter.setSizes([330, 900, 290])

    def _populate_motions(self) -> None:
        previous = self.current_entry
        previous_key = self._entry_key(previous) if previous is not None else None
        resolution = self._catalog.refresh()
        self._motions = list(resolution.entries)
        selected = next(
            (
                index
                for index, entry in enumerate(self._motions)
                if self._entry_key(entry) == previous_key
            ),
            0,
        )
        self.motion_browser.set_entries(self._motions, selected)
        enabled = bool(self._motions)
        self.source_rig_button.setEnabled(enabled)
        self.load_resource_button.setEnabled(enabled)
        origins = Counter(entry.origin for entry in self._motions)
        summary = self.tr(
            "{total} playable · {embedded} embedded · {inherited} inherited"
        ).format(
            total=len(self._motions),
            embedded=origins[PreviewMotionOrigin.EMBEDDED],
            inherited=origins[PreviewMotionOrigin.INHERITED],
        )
        self.motion_source_label.setText(summary)
        if not enabled:
            if resolution.unresolved_bank_ids:
                banks = ", ".join(str(value) for value in resolution.unresolved_bank_ids)
                message = self.tr(
                    "This MOTLIST has no embedded or explicitly inherited MOT payloads. "
                    "Its MotTree references BankID value(s) {banks}; resolve those "
                    "through the owning MOTBANK/PFB preview."
                ).format(banks=banks)
            else:
                message = self.tr("This MOTLIST contains no resolvable MOT payloads to preview.")
            if self._catalog.messages:
                message = f"{message}  {'  '.join(self._catalog.messages)}"
            self._clear_scene(message)
        else:
            self._load_current_motion(reset_camera=True)

    @staticmethod
    def _entry_key(entry: PreviewMotionEntry) -> tuple[str, int, int | None]:
        path = entry.source_path.replace("\\", "/").lower()
        marker = path.find("natives/")
        if marker >= 0:
            path = path[marker:]
        return path, entry.motion_id, entry.bank_id

    @property
    def current_motion(self) -> Motion | None:
        entry = self.current_entry
        return entry.resolve_motion() if entry is not None else None

    @property
    def current_entry(self) -> PreviewMotionEntry | None:
        index = self.motion_browser.current_index
        if index < 0 or index >= len(self._motions):
            return None
        return self._motions[index]

    def _on_motion_changed(self, _index: int) -> None:
        self.playback.stop()
        self._load_current_motion(reset_camera=True)

    def _play_selected_animation(self) -> None:
        if self.controller.ready and not self.controller.playing:
            self.playback.toggle()

    def _on_blend_shapes_toggled(self, enabled: bool) -> None:
        self.controller.set_deformation_enabled(enabled)
        self._render()

    def _load_current_motion(self, *, reset_camera: bool) -> None:
        deformation_targets = self._mesh_deformation_targets()
        self.blend_shapes_toggle.setVisible(bool(deformation_targets))
        self.blend_shapes_toggle.setEnabled(
            not self._using_source_rig and bool(deformation_targets)
        )
        motion = self.current_motion
        if motion is None:
            self._clear_scene(self.tr("No motion is selected."))
            return
        self.controller.clear()
        try:
            if self._using_source_rig or self._target is None:
                rig = rig_from_motion_skeleton(
                    motion,
                    scale=self.evaluation_profile.source_preview_scale,
                )
                scale = ", ".join(f"{value:g}" for value in self.evaluation_profile.source_preview_scale)
                rig_description = self.tr("MOT source skeleton (profile scale {scale})").format(scale=scale)
            else:
                rig = self._target.rig
                rig_description = self._target.label
            self.rig_label.setText(rig_description)
            if not self.controller.load(
                motion,
                rig,
                deformation_targets=(
                    deformation_targets if not self._using_source_rig else ()
                ),
            ):
                self._clear_scene(self.controller.error_message)
                return
            self.playback.configure()
            self._render(reset_camera=reset_camera)
        except (MotionPreviewError, ValueError) as exc:
            self._clear_scene(str(exc))

    def set_target(self, target: RigPreviewTarget) -> None:
        self._materials.clear()
        self._target_material_session = None
        self._target = target
        self._using_source_rig = False
        if target.handler is not None:
            self._target_material_session = MeshMaterialSession(
                target.handler,
                texture_quality=self._materials.texture_quality,
                parent=self._materials,
            )
            self._materials.add("target", self._target_material_session)
        self.target_rig_button.setEnabled(bool(self._motions))
        self.playback.stop()
        self._load_current_motion(reset_camera=True)

    def use_source_rig(self) -> None:
        self._using_source_rig = True
        if self._target_material_session is not None:
            self._materials.set_enabled("target", False)
        self.playback.stop()
        self._load_current_motion(reset_camera=True)

    def use_target_rig(self) -> None:
        if self._target is None:
            return
        self._using_source_rig = False
        if self._target_material_session is not None:
            self._materials.set_enabled("target", True)
        self.playback.stop()
        self._load_current_motion(reset_camera=True)

    def load_target_mesh(self, filepath: str, data: bytes) -> None:
        target = load_re_engine_mesh_target(
            filepath,
            data,
            app=getattr(self.handler, "app", None),
            resource_context=getattr(self.handler, "resource_context", None),
        )
        self.set_target(target)

    def _load_mesh_resource(self) -> None:
        resource_path, accepted = QInputDialog.getText(
            self,
            self.tr("Load Target Mesh from Project or PAK"),
            self.tr("Mesh resource path (including numeric version suffix):"),
        )
        if not accepted or not resource_path.strip():
            return
        hit = resolve_handler_resource_data(
            self.handler,
            resource_path.strip(),
            self,
        )
        if hit is None:
            self._show_error(self.tr("The target mesh was not found in the project, PAKs, or unpacked files."))
            return
        try:
            filepath, data = hit
            self.load_target_mesh(filepath, data)
        except ValueError as exc:
            self._show_error(self.tr("Could not load target mesh: {error}").format(error=exc))

    def _render(self, reset_camera: bool = False) -> None:
        if not self.controller.ready:
            return
        try:
            snapshot = self.controller.sample()
        except MotionPreviewError as exc:
            self._clear_scene(str(exc))
            return
        target = None if self._using_source_rig else self._target
        try:
            self._scene_renderer.present(
                snapshot,
                target,
                reset_camera=reset_camera,
            )
        except (ValueError, RuntimeError) as exc:
            self._clear_scene(str(exc))
            return
        status = self._status_text(snapshot)
        if status != self.status_label.text():
            self.status_label.setText(status)

    def _status_text(self, snapshot) -> str:
        messages = snapshot_status_messages(
            snapshot,
            self.controller.frames_per_second,
            self.tr,
        )
        if any(abs(weight - 1.0) > 1e-4 for weight in snapshot.node_weights):
            messages.append(self.tr(
                "Orange joints have non-unit MOT weights."
            ))
        motion = self.current_motion
        entry = self.current_entry
        if entry is not None and entry.origin is PreviewMotionOrigin.INHERITED:
            messages.append(self.tr("Motion payload inherited from {path}.").format(path=entry.source_path))
        if motion is not None and motion.character_path:
            messages.append(self.tr("Character/JMAP expressions are not evaluated in this skeleton preview."))
        messages.extend(snapshot_diagnostic_messages(snapshot))
        messages.extend(self._catalog.messages)
        return "  ".join(messages)

    def _mesh_deformation_targets(self):
        if self._target is None or self._target.mesh is None:
            return ()
        return mesh_blend_shape_targets(
            self._target.mesh,
            self.evaluation_profile.property_name_hash,
            motion_name_key=(
                self.evaluation_profile.joint_binding.motion_name_key
            ),
        )

    def _show_error(self, message: str) -> None:
        self.status_label.setText(message)

    def _clear_scene(self, message: str) -> None:
        self.playback.clear()
        self._scene_renderer.clear(reset_camera=True)
        self.status_label.setText(message)

    def _on_render_failure(self, message: str) -> None:
        if not self._cleaned:
            self._clear_scene(
                self.tr("Material preview failed: {error}").format(error=message)
            )

    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        self.playback.cleanup()
        self._materials.clear()
        self.viewport.cleanup()

    def closeEvent(self, event) -> None:
        self.cleanup()
        super().closeEvent(event)
