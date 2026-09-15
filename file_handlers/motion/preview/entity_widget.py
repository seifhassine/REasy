from __future__ import annotations

from dataclasses import replace
from typing import Callable

import numpy as np
from PySide6.QtCore import QSignalBlocker, QTimer, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QLabel,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from file_handlers.rsz.scn_scene_attachments import (
    ScnJointAttachmentResult,
    same_joints_pose_space_matrix,
)
from file_handlers.rsz.scn_scene_graph import (
    ScnObjectId,
    ScnRenderableMesh,
    normalize_document_id,
)
from file_handlers.rsz.scn_scene_preview import (
    ScnLoadedMesh,
    ScnScenePreviewWidget,
)
from ui.editor_widgets import EmbeddedPopupComboBox
from ui.scene.scn_visibility_panel import ScnGameObjectVisibilityPanel

from ..evaluation import DeformationTarget, Rig
from ..mot import Motion
from ..runtime import JointMapAttributes
from .animation_browser import MotionAnimationBrowser
from .attachments import MotionSceneAttachmentResolver
from .blend_shapes import mesh_blend_shape_targets
from .channel_controls import MotionChannelPanel
from .controller import MotionPreviewController
from .controls import MotionPlaybackControls
from .editor_layout import MotionEditorPane, MotionEditorWorkspace
from .entity_session import (
    EntityMotionSession,
    EntityMotionSessionResolver,
    ResolvedMotionTarget,
)
from .model import (
    MotionPreviewError,
    is_static_skeletal_pose,
    snapshot_diagnostic_messages,
    snapshot_status_messages,
)
from .entity_scene import EntitySceneCoordinator, MeshBinding
from .material_animation import (
    MaterialAnimationBinding,
    SceneMaterialAnimationResolver,
    evaluate_material_parameters,
    material_deformation_targets,
)
from .resolution import PreviewMotionEntry
from .renderer import MotionRenderState
from .runtime_details import MotionRuntimeDebugDialog
from .scene_state import resolve_renderable_scene_state
from .shared_pose import (
    resolve_motion_display_scope,
    resolve_motion_variant_visibility,
    resolve_same_joints_pose_targets,
)
from .skinning import (
    build_shared_rig_deformer,
    build_skinned_mesh_deformer,
    shared_rig_model_space_transform,
)
from .support import EntityMotionSupport
from .target import RigPreviewTarget, motion_target_from_mesh_handler


SceneFactory = Callable[..., ScnScenePreviewWidget]
TargetFactory = Callable[[ScnLoadedMesh], RigPreviewTarget]
DeformerFactory = Callable[[ScnLoadedMesh, RigPreviewTarget], object]
SharedDeformerFactory = Callable[
    [ScnLoadedMesh, RigPreviewTarget, Rig, np.ndarray],
    object,
]


class PfbMotionPreviewWidget(QWidget):
    """Entity preview over shared scene, motion, and game-runtime backends."""

    def __init__(
        self,
        handler,
        *,
        support: EntityMotionSupport,
        scene_factory: SceneFactory = ScnScenePreviewWidget,
        target_factory: TargetFactory | None = None,
        deformer_factory: DeformerFactory | None = None,
        shared_deformer_factory: SharedDeformerFactory | None = None,
        auto_initialize: bool = True,
    ):
        super().__init__()
        self.handler = handler
        self.support = support
        self.controller = MotionPreviewController(support.evaluation)
        self.session: EntityMotionSession | None = None
        self._scene_binding = EntitySceneCoordinator(
            target_factory or self._target_from_asset,
            support.evaluation.joint_binding,
        )
        self._deformers: dict[str, object] = {}
        self._gpu_skinned_keys: set[str] = set()
        self._material_animation_bindings: tuple[
            MaterialAnimationBinding, ...
        ] = ()
        self._material_animation_messages: tuple[str, ...] = ()
        self._material_resolver = SceneMaterialAnimationResolver(
            support.backend.material_controllers
        )
        self._material_parameter_values: dict[str, dict[str, float]] = {}
        self._attachment_resolver: MotionSceneAttachmentResolver | None = None
        self._active_attachment_keys: set[str] = set()
        self._render_state = MotionRenderState()
        self._variant_visibility_overrides: dict[str, bool] = {}
        self._scene_state_visibility_overrides: dict[str, bool] = {}
        self._scene_state_part_overrides: dict[str, tuple[bool, ...]] = {}
        self._motion_focus_keys: set[str] = set()
        self._active_target_key = ""
        self._runtime_target_index = -1
        self._target_mismatch = False
        self._initialized = False
        self._auto_initialize = auto_initialize
        self._cleaned = False
        self._debug_status = ""
        self._advanced_dialog: MotionRuntimeDebugDialog | None = None
        self._deformer_factory = deformer_factory or self._deformer_from_asset
        self._shared_deformer_factory = (
            shared_deformer_factory or self._shared_deformer_from_asset
        )

        settings = getattr(getattr(handler, "app", None), "settings", None)
        self.scene = scene_factory(
            self,
            settings=settings if isinstance(settings, dict) else None,
        )
        self.playback = MotionPlaybackControls(
            self.controller,
            frame_driver=self.scene.preview.set_frame_callback,
            parent=self,
        )
        # A window-state transition must not stop playback; the containing
        # preview owns the actual visible/hidden lifecycle.
        self.playback.set_stop_on_hide(False)
        self.playback.render_requested.connect(self._render)
        self.scene.renderables_changed.connect(self._sync_mesh_targets)
        self.scene.preview.render_failure.connect(
            self._on_render_failure,
            Qt.ConnectionType.QueuedConnection,
        )
        self._build_ui()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._auto_initialize and not self._initialized and not self._cleaned:
            QTimer.singleShot(0, self, self.initialize)

    def hideEvent(self, event) -> None:
        if not self.scene.preview.is_fullscreen_transitioning():
            self.playback.stop()
        super().hideEvent(event)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        settings = getattr(getattr(self.handler, "app", None), "settings", None)
        self.workspace = MotionEditorWorkspace(
            self.tr("Motion Editor"),
            settings=settings if isinstance(settings, dict) else None,
            parent=self,
        )
        root.addWidget(self.workspace)

        self.target_label = QLabel(self.tr("Model"))
        self.workspace.toolbar.addWidget(self.target_label)
        self.target_combo = EmbeddedPopupComboBox()
        self.target_combo.setToolTip(
            self.tr(
                "Alternative same-joints objects with equivalent motion-joint bindings."
            )
        )
        self.target_combo.currentIndexChanged.connect(self._on_mesh_changed)
        self.target_combo.setMinimumWidth(180)
        self.workspace.toolbar.addWidget(self.target_combo)
        self.target_label.hide()
        self.target_combo.hide()
        self.composition_toggle = QCheckBox(
            self.tr("Layer composition")
        )
        self.composition_toggle.setToolTip(
            self.tr(
                "Advanced: combine the PFB's optional runtime layers with "
                "the selected animation. Leave off to preview the MOT alone."
            )
        )
        self.composition_toggle.toggled.connect(
            self._on_composition_toggled
        )
        self.composition_toggle.hide()
        self.workspace.toolbar.addWidget(self.composition_toggle)
        self.advanced_button = QPushButton(self.tr("Debug…"))
        self.advanced_button.setToolTip(
            self.tr(
                "Show resolved runtime data and diagnostics for debugging."
            )
        )
        self.advanced_button.clicked.connect(self._show_advanced_info)
        self.workspace.toolbar.addWidget(self.advanced_button)

        self.notice_label = QLabel()
        self.notice_label.setObjectName("motionNotice")
        self.notice_label.setWordWrap(True)
        self.notice_label.hide()

        self.animation_pane = MotionEditorPane(self.tr("Animations"), self)
        self.animation_pane.setMinimumWidth(235)
        self.animation_pane.setMaximumWidth(360)
        self.animation_browser = MotionAnimationBrowser()
        self.animation_browser.selection_changed.connect(
            self._on_animation_selected
        )
        self.animation_browser.animation_activated.connect(
            self._play_selected_animation
        )
        self.animation_pane.add_widget(self.animation_browser, 1)
        self.animation_pane.add_widget(self.playback)

        self.viewport_pane = MotionEditorPane(self.tr("Viewport"), self)
        self.viewport_pane.setProperty("role", "viewport")
        self.viewport_pane.add_widget(self.notice_label)
        self.viewport_pane.add_widget(self.scene, 1)

        self.scene_pane = MotionEditorPane(self.tr("Scene objects"), self)
        self.scene_pane.setMinimumWidth(195)
        self.scene_pane.setMaximumWidth(300)
        self.object_visibility = ScnGameObjectVisibilityPanel()
        self.object_visibility.visibility_changed.connect(
            self._on_object_visibility_changed
        )
        self.object_visibility.focus_keys_changed.connect(
            self.scene.set_focused_renderables
        )

        self.channel_panel = MotionChannelPanel()
        self.channel_panel.changed.connect(self._load_animation)
        self.channel_panel.hide()
        self.scene_details_splitter = QSplitter(
            Qt.Orientation.Vertical,
            self.scene_pane,
        )
        self.scene_details_splitter.setChildrenCollapsible(False)
        self.scene_details_splitter.addWidget(self.object_visibility)
        self.scene_details_splitter.addWidget(self.channel_panel)
        self.scene_details_splitter.setStretchFactor(0, 3)
        self.scene_details_splitter.setStretchFactor(1, 2)
        self.scene_pane.add_widget(self.scene_details_splitter, 1)

        self.workspace.add_pane(self.animation_pane, 0)
        self.workspace.add_pane(self.viewport_pane, 1)
        self.workspace.add_pane(self.scene_pane, 0)
        self.workspace.splitter.setSizes([250, 900, 210])
        viewport = self.scene.preview
        set_fullscreen_content = getattr(viewport, "set_fullscreen_content", None)
        if callable(set_fullscreen_content):
            set_fullscreen_content(self.workspace.splitter)
        hud = getattr(viewport, "overlay", None)
        if hud is not None:
            hud.viewport_anchor = "top"
            viewport.set_viewport_overlay_folded(hud, True)
        place_overlays = getattr(viewport, "place_viewport_overlays", None)
        if callable(place_overlays):
            QTimer.singleShot(0, viewport, place_overlays)

    def initialize(self) -> None:
        if self._initialized or self._cleaned:
            return
        self._initialized = True
        self.scene.ensure_loaded()
        try:
            resolver = EntityMotionSessionResolver(
                self._load_resource_data,
                support=self.support,
            )
            self.set_session(
                resolver.load(
                    str(getattr(self.handler, "filepath", "") or "<PFB>"),
                    self.handler.rsz_file,
                )
            )
        except (OSError, ValueError, RuntimeError) as exc:
            self._clear_animation(
                self.tr("Motion runtime context is unavailable: {error}").format(
                    error=exc
                )
            )

    def set_session(self, session: EntityMotionSession) -> None:
        self.session = session
        self._scene_binding.reset_session()
        self._material_resolver.clear()
        self.animation_browser.set_session(session)
        self._runtime_target_index = -1
        self._refresh_runtime_target()
        self._sync_mesh_targets()

    def _load_resource_data(self, path: str) -> tuple[str, bytes] | None:
        sources = self.scene.scene_sources()
        source = sources[0] if sources else None
        resolved = self.scene.loader.resolve_resource_for_source(source, path)
        if resolved is None or resolved.data is None:
            return None
        return resolved.path, resolved.data

    def _refresh_runtime_target(self) -> None:
        self._runtime_target_index = self.animation_browser.target_index
        current = self.current_motion_target
        definition = current.definition if current is not None else None
        title = self.tr("Motion Editor")
        if definition is not None and definition.name:
            title = f"{title}  ·  {definition.name}"
        self.workspace.title_label.setText(title)
        self.playback.set_authored_defaults(
            speed=definition.play_speed if definition is not None else 1.0,
            stop_at_motion_end=(
                definition.stop_at_motion_end
                if definition is not None
                else False
            ),
        )
        self.channel_panel.set_target(current)
        with QSignalBlocker(self.composition_toggle):
            self.composition_toggle.setChecked(False)
        self.composition_toggle.setVisible(self.channel_panel.has_options)
        self.channel_panel.hide()
        self._refresh_advanced_info()
        self._select_matching_mesh()

    def _sync_mesh_targets(self) -> None:
        if self._cleaned:
            return
        self.object_visibility.set_graphs(self.scene.graphs)
        self.scene.set_user_renderable_visibility_overrides(
            self.object_visibility.user_overrides
        )
        previous_key = (
            self.current_target.asset.key
            if self.current_target is not None
            else self._active_target_key
        )
        errors = self._scene_binding.bind_assets(self.scene.loaded_meshes())
        try:
            self._sync_material_animation_bindings()
        except (ValueError, RuntimeError) as exc:
            self._clear_animation(
                self.tr("Material animation resolution failed: {error}").format(
                    error=exc
                )
            )
            return

        runtime_target = self.current_motion_target
        preferred_key = (
            self._scene_binding.preferred_variant(runtime_target, previous_key)
        )
        try:
            entry = self.current_entry
            motion = (
                entry.resolve_motion()
                if entry is not None and self._scene_binding.bindings
                else None
            )
            selected = self._set_mesh_choices(
                runtime_target,
                self._scene_binding.default_index(
                    self.session,
                    self.scene.graphs,
                    runtime_target,
                    previous_key,
                    motion,
                ),
                preferred_key,
                motion,
            )
        except (ValueError, RuntimeError) as exc:
            self._fail_scene_binding(exc)
            return
        self._refresh_advanced_info()
        if selected is None:
            self._clear_animation(
                self.tr("Waiting for a skinnable mesh from this PFB.")
                + (f"  {'; '.join(errors)}" if errors else "")
            )
        else:
            self._load_animation()

    def _select_matching_mesh(self) -> None:
        runtime_target = self.current_motion_target
        if runtime_target is None:
            self._load_animation()
            return
        try:
            motion = (
                self.current_entry.resolve_motion()
                if self.current_entry is not None and self._scene_binding.bindings
                else None
            )
            reference = self._scene_binding.matching_index(
                self.session,
                self.scene.graphs,
                runtime_target,
                motion,
            )
            if reference is None:
                reference = self._scene_binding.default_index(
                    self.session,
                    self.scene.graphs,
                    runtime_target,
                    motion=motion,
                )
            self._set_mesh_choices(
                runtime_target,
                reference,
                self._scene_binding.preferred_variant(runtime_target),
                motion,
            )
        except (ValueError, RuntimeError) as exc:
            self._fail_scene_binding(exc)
            return
        self._load_animation()

    def _fail_scene_binding(self, error: Exception) -> None:
        self._clear_animation(
            self.tr("Scene motion binding failed: {error}").format(
                error=error
            )
        )

    def _set_mesh_choices(
        self,
        runtime_target: ResolvedMotionTarget | None,
        reference: int | None,
        preferred_key: str,
        motion: Motion | None = None,
    ) -> int | None:
        indices = (
            self._scene_binding.compatible_indices(
                self.scene.graphs,
                runtime_target,
                reference,
                motion,
            )
            if runtime_target is not None and reference is not None
            else (() if reference is None else (reference,))
        )
        self._target_mismatch = bool(
            runtime_target is not None
            and self.current_entry is not None
            and reference is not None
            and not indices
        )
        selected = next(
            (
                index
                for index in indices
                if self._scene_binding.bindings[index].asset.key == preferred_key
            ),
            reference,
        )
        has_choice = len(indices) > 1
        self.target_label.setVisible(has_choice)
        self.target_combo.setVisible(has_choice)
        with QSignalBlocker(self.target_combo):
            self.target_combo.clear()
            for index in indices:
                binding = self._scene_binding.bindings[index]
                self.target_combo.addItem(
                    self._scene_binding.choice_label(self.scene.graphs, binding),
                    index,
                )
                self.target_combo.setItemData(
                    self.target_combo.count() - 1,
                    binding.asset.renderable.mesh_path,
                    Qt.ItemDataRole.ToolTipRole,
                )
            if selected in indices:
                self.target_combo.setCurrentIndex(indices.index(selected))
        return selected if selected in indices else None

    @property
    def current_motion_target(self) -> ResolvedMotionTarget | None:
        if self.session is None:
            return None
        index = self.animation_browser.target_index
        if not 0 <= index < len(self.session.targets):
            return None
        return self.session.targets[index]

    @property
    def current_entry(self) -> PreviewMotionEntry | None:
        target = self.current_motion_target
        if target is None:
            return None
        index = self.animation_browser.motion_index
        if not 0 <= index < len(target.motions):
            return None
        return target.motions[index]

    @property
    def current_target(self) -> MeshBinding | None:
        index = self.target_combo.currentData()
        if (
            not isinstance(index, int)
            or not 0 <= index < len(self._scene_binding.bindings)
        ):
            return None
        return self._scene_binding.bindings[index]

    def _selected_layers(self):
        return self.channel_panel.selected_layers()

    def _on_composition_toggled(self, enabled: bool) -> None:
        self.channel_panel.setVisible(enabled)
        self.channel_panel.set_active(enabled)
        if enabled:
            QTimer.singleShot(0, self._size_layer_composition_panel)

    def _size_layer_composition_panel(self) -> None:
        height = self.scene_details_splitter.height()
        if self.channel_panel.isVisible() and height > 0:
            self.scene_details_splitter.setSizes(
                [height * 3 // 5, height * 2 // 5]
            )

    def _on_mesh_changed(self, _index: int) -> None:
        binding = self.current_target
        runtime_target = self.current_motion_target
        if binding is not None and runtime_target is not None:
            self._scene_binding.select_variant(runtime_target, binding.asset.key)
        self._load_animation()

    def _on_object_visibility_changed(
        self,
        overrides: dict[str, bool],
    ) -> None:
        self.scene.set_user_renderable_visibility_overrides(overrides)
        if self.controller.ready:
            self._load_animation()

    def _on_animation_selected(
        self,
        target_index: int,
        _motion_index: int,
    ) -> None:
        if target_index != self._runtime_target_index:
            self._refresh_runtime_target()
        else:
            self._refresh_advanced_info()
            self._select_matching_mesh()

    def _play_selected_animation(self) -> None:
        if self.controller.ready and not self.controller.playing:
            self.playback.toggle()

    def _load_animation(self) -> None:
        self.playback.stop()
        self._restore_active_target()
        entry = self.current_entry
        binding = self.current_target
        runtime_target = self.current_motion_target
        if runtime_target is None:
            self._clear_animation(self.tr("This PFB has no Motion target."))
            return
        if entry is None or binding is None:
            reason = (
                self.tr(
                    "This animation belongs to a different skeleton; no "
                    "compatible object is loaded in this PFB."
                )
                if entry is not None and self._target_mismatch
                else
                self.tr(
                    "The selected Motion target has no resolvable base motions; "
                    "see Advanced info."
                )
                if not runtime_target.motions
                else self.tr(
                    "Select an animation and a skinnable target mesh."
                )
            )
            self._clear_animation(reason)
            return
        try:
            layers = self._selected_layers()
            if not self.controller.load(
                entry.resolve_motion(),
                binding.target.rig,
                layers=layers,
                deformation_targets=self._deformation_targets(runtime_target),
                root_motion_mode=runtime_target.definition.root_motion_mode,
            ):
                self._clear_animation(self.controller.error_message)
                return
            self._apply_target_visibility(binding)
            self._deformers = self._build_pose_deformers(
                binding,
                runtime_target,
            )
            self._enable_gpu_skinning()
            self._sync_animated_visibility_scope()
            self._attachment_resolver = MotionSceneAttachmentResolver(
                self.scene.graphs,
                binding.asset.renderable,
                binding.target.rig,
                pose_source_object_id=ScnObjectId(
                    binding.asset.renderable.source_object_id.document_id,
                    runtime_target.definition.id.object_id,
                ),
            )
            self._active_target_key = binding.asset.key
            self.playback.configure()
            self._show_notice("")
            self._render()
        except (MotionPreviewError, ValueError, RuntimeError) as exc:
            self._clear_animation(str(exc))

    def _render(self, _reset_camera: bool = False) -> None:
        binding = self.current_target
        if (
            not self.controller.ready
            or binding is None
            or not self._deformers
        ):
            return
        try:
            snapshot = self.controller.sample()
            self._apply_scene_state(snapshot.frame)
            pose_changed, deformation_changed = self._render_state.changes(
                snapshot
            )
            if deformation_changed:
                material_values = evaluate_material_parameters(
                    self._material_animation_bindings,
                    snapshot.deformation_weights,
                )
                if material_values != self._material_parameter_values:
                    self.scene.set_material_parameter_values(material_values)
                    self._material_parameter_values = material_values
            for key, deformer in self._deformers.items():
                if key in self._gpu_skinned_keys:
                    if deformation_changed:
                        positions, normals = deformer.source_geometry(
                            snapshot.deformation_weights
                        )
                        self.scene.preview.update_mesh_skinning_source(
                            key,
                            positions,
                            normals,
                        )
                    if pose_changed:
                        self.scene.preview.update_mesh_skinning(
                            key,
                            deformer.skin_matrices(snapshot),
                        )
                    continue
                if not pose_changed and not deformation_changed:
                    continue
                vertices, normals = deformer.deform(snapshot)
                self.scene.preview.update_mesh_geometry(
                    key,
                    vertices,
                    normals,
                    recompute_bounds=False,
                )
            if pose_changed and self._attachment_resolver is not None:
                self._apply_attachments(
                    self._attachment_resolver.resolve(snapshot)
                )
            self._render_state.accept(snapshot)
        except (MotionPreviewError, ValueError, RuntimeError) as exc:
            self._clear_animation(str(exc))
            return
        if (
            self._advanced_dialog is not None
            and self._advanced_dialog.isVisible()
        ):
            status = self._status_text(snapshot)
            if status != self._debug_status:
                self._debug_status = status
                self._refresh_advanced_info()

    def _status_text(self, snapshot) -> str:
        entry = self.current_entry
        messages = snapshot_status_messages(
            snapshot,
            self.controller.frames_per_second,
            self.tr,
        )
        if entry is not None:
            messages.append(self._motion_label(entry))
            if is_static_skeletal_pose(entry.resolve_motion()):
                messages.append(self.tr("Static skeletal pose"))
        if len(self._deformers) > 1:
            messages.append(
                self.tr("Shared pose: {count} meshes").format(
                    count=len(self._deformers)
                )
            )
        if self._material_parameter_values:
            values = [
                value
                for parameters in self._material_parameter_values.values()
                for value in parameters.values()
            ]
            messages.append(
                self.tr("Material animation: {active}/{total} active").format(
                    active=sum(abs(value) > 1e-6 for value in values),
                    total=len(values),
                )
            )
        for channel, choice in self.channel_panel.selected_choices():
            messages.append(
                self.tr("{channel}: {choice} on layer {layer}").format(
                    channel=channel.definition.label,
                    choice=choice.label,
                    layer=channel.definition.layer_index,
                )
            )
        messages.extend(snapshot_diagnostic_messages(snapshot))
        if self.session is not None and self.session.diagnostics:
            messages.extend(self.session.diagnostics[:4])
            if len(self.session.diagnostics) > 4:
                messages.append(
                    self.tr(
                        "{count} additional diagnostics are in Details."
                    ).format(count=len(self.session.diagnostics) - 4)
                )
        messages.extend(self._material_animation_messages)
        return "  ".join(dict.fromkeys(messages))

    def _deformation_targets(
        self,
        runtime_target: ResolvedMotionTarget,
    ) -> tuple[DeformationTarget, ...]:
        targets = {
            target.name: target
            for target in self._source_deformation_targets()
        }
        joint_map = runtime_target.joint_map
        if (
            joint_map is None
            or not joint_map.attributes & JointMapAttributes.DEFORM
        ):
            return tuple(targets.values())
        for joint in dict.fromkeys(joint_map.extra_joints):
            target = targets.get(joint.joint_name)
            if target is None:
                continue
            if target.binding_key not in (None, joint.joint_hash):
                raise ValueError(
                    f"blendshape {target.name!r} has conflicting JMAP keys"
                )
            targets[target.name] = replace(
                target,
                binding_key=joint.joint_hash,
            )
        return tuple(targets.values())

    def _mesh_deformation_targets(self) -> tuple[DeformationTarget, ...]:
        by_name: dict[str, DeformationTarget] = {}
        by_hash: dict[int, str] = {}
        for binding in self._scene_binding.bindings:
            for target in mesh_blend_shape_targets(
                binding.target.mesh,
                self.support.evaluation.property_name_hash,
                motion_name_key=(
                    self.support.evaluation.joint_binding.motion_name_key
                ),
            ):
                assert target.property_hash is not None
                previous_name = by_hash.setdefault(
                    target.property_hash,
                    target.name,
                )
                if previous_name != target.name:
                    raise ValueError(
                        f"blendshape names {previous_name!r} and "
                        f"{target.name!r} share property hash "
                        f"0x{target.property_hash:08X}"
                    )
                by_name.setdefault(target.name, target)
        return tuple(by_name.values())

    def _source_deformation_targets(self) -> tuple[DeformationTarget, ...]:
        return (
            *self._mesh_deformation_targets(),
            *material_deformation_targets(
                self._material_animation_bindings,
                self.support.evaluation.property_name_hash,
            ),
        )

    def _sync_material_animation_bindings(self) -> None:
        documents = {
            document_id: document
            for graph in self.scene.graphs
            for document_id, document in getattr(
                graph,
                "documents",
                {},
            ).items()
        }
        resolution = self._material_resolver.resolve(
            documents,
            self._scene_binding.bindings,
            lambda renderable: self.scene.material_scope_for(renderable),
            root_rsz=getattr(self.handler, "rsz_file", None),
            root_controllers=(
                self.session.definition.material_controllers
                if self.session is not None
                else ()
            ),
        )
        self._material_animation_bindings = resolution.bindings
        self._material_animation_messages = resolution.diagnostics

    def _refresh_advanced_info(self) -> None:
        if self._advanced_dialog is None:
            return
        self._advanced_dialog.set_runtime(
            self.session,
            self.current_motion_target,
            self.current_entry,
            preview_messages=(self._debug_status,) if self._debug_status else (),
            scene_message=self.scene.status_label.text(),
        )

    def _show_advanced_info(self) -> None:
        if self._advanced_dialog is None:
            self._advanced_dialog = MotionRuntimeDebugDialog(
                self.support.format_codec.profile,
                self,
            )
        self._refresh_advanced_info()
        self._advanced_dialog.show()
        self._advanced_dialog.raise_()
        self._advanced_dialog.activateWindow()

    def _build_pose_deformers(
        self,
        owner: MeshBinding,
        runtime_target: ResolvedMotionTarget,
    ) -> dict[str, object]:
        graph, group = self._scene_binding.same_joints_group(
            self.scene.graphs,
            owner.asset.renderable,
        )
        if self.session is None:
            raise ValueError("animated mesh has no resolved Motion session")

        pose_space_matrix = same_joints_pose_space_matrix(
            graph,
            owner.asset.renderable,
        )
        pose_targets = resolve_same_joints_pose_targets(
            graph,
            group,
            owner.asset.renderable,
            runtime_target=runtime_target.definition,
        )
        if not any(
            renderable.key == owner.asset.key
            for renderable in pose_targets
        ):
            raise ValueError(
                f"animated mesh {owner.asset.renderable.key!r} is not in its "
                "resolved pose target set"
            )

        visible_target_keys = {
            renderable.key
            for renderable in pose_targets
            if self._renderable_visible_for_motion(renderable)
        }
        bindings = {
            binding.asset.key: binding
            for binding in self._scene_binding.bindings
        }
        deformers: dict[str, object] = {}
        for renderable in pose_targets:
            key = renderable.key
            if key != owner.asset.key and key not in visible_target_keys:
                continue
            if key not in bindings:
                if key not in self._scene_binding.binding_errors:
                    raise ValueError(
                        f"same-joints mesh {renderable.mesh_path!r} has no "
                        "skinning binding or binding diagnostic"
                    )
                raise ValueError(
                    f"same-joints mesh {renderable.mesh_path!r} cannot "
                    "receive the animated pose: "
                    f"{self._scene_binding.binding_errors[key]}"
                )
            binding = bindings[key]

            model_matrix = np.asarray(
                binding.asset.renderable.world_matrix,
                dtype=np.float32,
            ).reshape(4, 4)
            if key == owner.asset.key and np.array_equal(
                pose_space_matrix,
                model_matrix,
            ):
                deformers[key] = self._deformer_factory(
                    binding.asset,
                    binding.target,
                )
                continue
            deformers[key] = self._shared_deformer_factory(
                binding.asset,
                binding.target,
                owner.target.rig,
                shared_rig_model_space_transform(
                    pose_space_matrix,
                    model_matrix,
                ),
            )
        return deformers

    def _enable_gpu_skinning(self) -> None:
        for key, deformer in self._deformers.items():
            if deformer.requires_post_skin_normals:
                continue
            self.scene.preview.set_mesh_skinning(key, deformer.binding)
            self._gpu_skinned_keys.add(key)

    def _apply_target_visibility(self, owner: MeshBinding) -> None:
        graph, group = self._scene_binding.same_joints_group(
            self.scene.graphs,
            owner.asset.renderable,
        )
        self._variant_visibility_overrides = resolve_motion_variant_visibility(
            graph,
            group,
            owner.asset.renderable,
        )
        runtime_visibility = self._runtime_visibility_overrides()
        self.object_visibility.set_runtime_visibility_overrides(
            runtime_visibility
        )
        self.scene.set_renderable_visibility_overrides(
            runtime_visibility
        )
        self._motion_focus_keys = set(
            resolve_motion_display_scope(
                group,
                self._variant_visibility_overrides,
            )
        )

    def _renderable_visible_for_motion(
        self,
        renderable: ScnRenderableMesh,
    ) -> bool:
        return self.object_visibility.resolve_visibility(
            renderable.key,
            self._scene_state_visibility_overrides.get(
                renderable.key,
                self._variant_visibility_overrides.get(
                    renderable.key,
                    renderable.visible_by_default,
                ),
            ),
        )

    def _apply_scene_state(self, frame: float) -> None:
        if self.session is None or self.current_entry is None:
            return
        visibility_overrides, part_overrides = resolve_renderable_scene_state(
            self.current_entry.resolve_motion(),
            self.session.definition.scene_state_bindings,
            frame,
            normalize_document_id(self.session.source_path),
            (
                renderable
                for graph in self.scene.graphs
                for renderable in getattr(graph, "renderables", ())
            ),
        )
        if (
            visibility_overrides == self._scene_state_visibility_overrides
            and part_overrides == self._scene_state_part_overrides
        ):
            return
        self._scene_state_visibility_overrides = visibility_overrides
        self._scene_state_part_overrides = part_overrides
        runtime_visibility = self._runtime_visibility_overrides()
        self.object_visibility.set_runtime_visibility_overrides(
            runtime_visibility
        )
        self.scene.set_renderable_visibility_overrides(runtime_visibility)
        self.scene.set_renderable_part_overrides(part_overrides)

    def _runtime_visibility_overrides(self) -> dict[str, bool]:
        return {
            **self._variant_visibility_overrides,
            **self._scene_state_visibility_overrides,
        }

    def _apply_attachments(self, result: ScnJointAttachmentResult) -> None:
        keys = set(result.matrices)
        self.scene.preview.set_mesh_draw_transforms(result.matrices)
        self._active_attachment_keys = keys
        self._sync_animated_visibility_scope()

    def _sync_animated_visibility_scope(self) -> None:
        self.object_visibility.set_animated_keys(
            self._motion_focus_keys | self._active_attachment_keys
        )

    def _clear_animation(self, message: str) -> None:
        self._restore_active_target()
        self.playback.clear()
        self.controller.clear()
        self._attachment_resolver = None
        self._debug_status = message
        self._show_notice(message)
        self._refresh_advanced_info()

    def _on_render_failure(self, message: str) -> None:
        if not self._cleaned:
            self._clear_animation(
                self.tr("Material preview failed: {error}").format(
                    error=message
                )
            )

    def _show_notice(self, message: str) -> None:
        self.notice_label.setText(message)
        self.notice_label.setVisible(bool(message))

    def _restore_active_target(self) -> None:
        self.object_visibility.set_runtime_visibility_overrides({})
        self.scene.set_renderable_visibility_overrides({})
        self.scene.set_renderable_part_overrides({})
        self._variant_visibility_overrides.clear()
        self._scene_state_visibility_overrides.clear()
        self._scene_state_part_overrides.clear()
        self.scene.preview.clear_mesh_draw_transforms()
        self._active_attachment_keys.clear()
        self._render_state.clear()
        geometry_keys = set(self._deformers)
        if self._active_target_key:
            geometry_keys.add(self._active_target_key)
        gpu_keys = geometry_keys & self._gpu_skinned_keys
        if gpu_keys:
            self.scene.preview.clear_mesh_skinning(gpu_keys)
        for key in geometry_keys - gpu_keys:
            self.scene.restore_mesh_geometry(key)
        self._gpu_skinned_keys.clear()
        if self._material_parameter_values:
            self.scene.set_material_parameter_values({})
            self._material_parameter_values.clear()
        self._active_target_key = ""
        self._deformers.clear()
        self._motion_focus_keys.clear()
        self._sync_animated_visibility_scope()

    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        self.playback.cleanup()
        self._restore_active_target()
        self._material_resolver.clear()
        if self._advanced_dialog is not None:
            self._advanced_dialog.close()
            self._advanced_dialog = None
        self.scene.cleanup()

    def closeEvent(self, event) -> None:
        self.cleanup()
        super().closeEvent(event)

    @staticmethod
    def _motion_label(entry: PreviewMotionEntry) -> str:
        name = entry.name or "(unnamed)"
        return (
            f"Bank {entry.bank_id} · ID {entry.motion_id} · {name}"
            if entry.bank_id is not None
            else f"ID {entry.motion_id} · {name}"
        )

    @staticmethod
    def _target_from_asset(asset: ScnLoadedMesh) -> RigPreviewTarget:
        return motion_target_from_mesh_handler(
            asset.renderable.mesh_path,
            asset.handler,
        )

    @staticmethod
    def _deformer_from_asset(
        asset: ScnLoadedMesh,
        target: RigPreviewTarget,
    ):
        return build_skinned_mesh_deformer(
            target.mesh,
            target.rig,
            asset.bind_mesh.vertices,
            asset.bind_mesh.normals,
            asset.bind_mesh.indices,
            handler=asset.handler,
            explicit_mdf_path=asset.renderable.mdf_path,
        )

    @staticmethod
    def _shared_deformer_from_asset(
        asset: ScnLoadedMesh,
        target: RigPreviewTarget,
        owner_rig: Rig,
        pose_to_constrained_matrix: np.ndarray,
    ):
        return build_shared_rig_deformer(
            target.mesh,
            target.rig,
            owner_rig,
            asset.bind_mesh.vertices,
            asset.bind_mesh.normals,
            asset.bind_mesh.indices,
            pose_to_constrained_matrix=pose_to_constrained_matrix,
            handler=asset.handler,
            explicit_mdf_path=asset.renderable.mdf_path,
        )
