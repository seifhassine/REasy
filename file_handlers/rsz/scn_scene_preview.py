from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping

import numpy as np
from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from file_handlers.lightprobe.loader import parse_light_probe_data
from file_handlers.mesh.material_session import (
    MeshMaterialCollection,
    MeshMaterialSession,
    scoped_material_key,
)
from file_handlers.mesh.mesh_handler import MeshHandler
from ui.scene.lightprobe_preview import SceneLightProbeInstance, SceneLightProbeSet
from ui.scene.mesh_scene import build_mesh_scene
from ui.scene.scene_model import SceneDrawBatch, SceneDrawMesh
from ui.scene.scene_preview import ScenePreviewWidget

from .scn_scene_loader import ScnSceneLoader, ScnSceneSource
from .scn_document_store import ScnDocumentStore
from .scn_scene_commands import RawTransformFieldCommand, TransformEditResult, TransformSelectionCommand
from .scn_scene_graph import (
    ScnSceneDiagnostic,
    ScnLightProbeBinding,
    ScnRenderableMesh,
    ScnSceneGraph,
    normalize_scene_path,
)


@dataclass(slots=True)
class _RenderableQueueItem:
    graph: ScnSceneGraph
    renderable: ScnRenderableMesh


@dataclass(frozen=True, slots=True)
class ScnLoadedMesh:
    renderable: ScnRenderableMesh
    handler: MeshHandler
    bind_mesh: SceneDrawMesh

    @property
    def key(self) -> str:
        return self.renderable.key


def _resource_version(path: str, extension: str) -> int | None:
    suffixes = Path(normalize_scene_path(path)).suffixes
    marker = f".{extension.lower()}"
    for current, following in zip(suffixes, suffixes[1:]):
        value = following.removeprefix(".")
        if current.lower() == marker and value.isdecimal():
            return int(value)
    return None


class ScnScenePreviewWidget(QWidget):
    renderables_changed = Signal()

    def __init__(
        self,
        owner,
        sources_getter: Callable[[], list[ScnSceneSource]] | None = None,
        graphs_changed_callback: Callable[[], None] | None = None,
        edits_changed_callback: Callable[[set[str], list[object]], None] | None = None,
        document_store: ScnDocumentStore | None = None,
        settings: dict | None = None,
    ):
        super().__init__(owner)
        self.handler = getattr(owner, "handler", None)
        self._sources_getter = sources_getter
        self._graphs_changed_callback = graphs_changed_callback
        self._edits_changed_callback = edits_changed_callback
        self.graphs: list[ScnSceneGraph] = []
        self.loader = ScnSceneLoader(document_store)
        self._loaded = False
        self._stale = False
        self._mesh_cache: dict[str, tuple[MeshHandler | None, SceneDrawMesh | None]] = {}
        self._batch_cache: dict[str, tuple[str, list[SceneDrawBatch]]] = {}
        self._light_probe_cache: dict[str, SceneLightProbeSet | None] = {}
        self._shown_diagnostics: set[tuple] = set()
        self._pending_renderables: deque[_RenderableQueueItem] = deque()
        self._pending_material_renderables: deque[ScnRenderableMesh] = deque()
        self._draw_meshes: list[SceneDrawMesh] = []
        self._hidden_renderables: set[str] = set()
        self._visibility_overrides: dict[str, bool] = {}
        self._user_visibility_overrides: dict[str, bool] = {}
        self._part_visibility_overrides: dict[str, tuple[bool, ...]] = {}
        self._focused_renderables: set[str] | None = None
        self._retired_renderables: set[str] = set()
        self._loading = False
        self._refresh_queued = False
        self._camera_initialized = False
        self._last_asset_counts = (0, 0, 0)

        self._mesh_timer = QTimer(self)
        self._mesh_timer.setSingleShot(True)
        self._mesh_timer.timeout.connect(self._load_mesh_step)

        self._material_timer = QTimer(self)
        self._material_timer.setSingleShot(True)
        self._material_timer.timeout.connect(self._resolve_material_step)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.status_label = QLabel(self.tr("Scene preview has not been built."), self)
        self.status_label.setWordWrap(True)

        self.preview = ScenePreviewWidget(self, settings=settings)
        self._materials = MeshMaterialCollection(self.preview, parent=self)
        self._materials.changed.connect(self._update_status)
        self.preview.gizmo_transform_committed.connect(self._commit_gizmo_transforms)
        self.preview.texture_quality_changed.connect(self._on_texture_quality_changed)
        self.preview.texture_upload_status_changed.connect(self._update_status)
        self.preview.setMinimumHeight(320)
        layout.addWidget(self.preview, 1)

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self.request_refresh()

    def request_refresh(self) -> None:
        if self._refresh_queued:
            return
        self._stop_timers()
        self._pending_renderables.clear()
        self._pending_material_renderables.clear()
        self._loading = False
        self._refresh_queued = True
        self.status_label.setText(self.tr("Preparing scene preview..."))
        QTimer.singleShot(0, self, self.refresh)

    def set_stale(self) -> None:
        self._stale = True
        if self._loaded:
            self.status_label.setText(
                self.tr(
                    "Scene preview is stale. Reload the scene to rebuild from the edited SCN."
                )
            )

    def _on_texture_quality_changed(self, quality: str) -> None:
        self._materials.set_texture_quality(quality)
        self._update_status()

    def sync_raw_transform_field(self, document_ids: set[str], changed_field: object) -> TransformEditResult:
        result = RawTransformFieldCommand(self.graphs, self.loader.document_store, document_ids, changed_field).execute()
        if result.handled:
            self._apply_transform_result(result, force_upload=True)
        return result

    def _stop_timers(self) -> None:
        self._mesh_timer.stop()
        self._material_timer.stop()

    def cleanup(self) -> None:
        self._stop_timers()
        self._clear_runtime_state()
        self.preview.cleanup()
        self._sources_getter = self._graphs_changed_callback = self._edits_changed_callback = None
        self.handler = None

    def _clear_runtime_state(self, *, keep_hidden: bool = False) -> None:
        self._pending_renderables.clear()
        self._pending_material_renderables.clear()
        self._draw_meshes.clear()
        if not keep_hidden:
            self._hidden_renderables.clear()
            self._user_visibility_overrides.clear()
        self._visibility_overrides.clear()
        self._part_visibility_overrides.clear()
        self._focused_renderables = None
        self._retired_renderables.clear()
        self._materials.clear()
        self._mesh_cache.clear()
        self._batch_cache.clear()
        self._light_probe_cache.clear()
        self._shown_diagnostics.clear()
        self._loaded = self._loading = self._refresh_queued = self._stale = False
        self._camera_initialized = False
        self._last_asset_counts = (0, 0, 0)
        self.graphs.clear()

    def scene_sources(self) -> list[ScnSceneSource]:
        if self._sources_getter is not None:
            return list(self._sources_getter())
        if self.handler is None:
            return []
        path = str(getattr(self.handler, "filepath", "") or "")
        return [ScnSceneSource(path=path, handler=self.handler, label=Path(path).name)]

    def add_source(self, source: ScnSceneSource) -> None:
        if not self._loaded or self._stale or self._loading:
            self.request_refresh()
            return
        graphs = self.loader.build_graphs([source], max_depth=8, skip_document_ids=self._document_ids())
        if not graphs:
            return
        self.graphs.extend(graphs)
        self._load_light_probe_instances()
        self._queue_renderables(graphs)
        self._update_status()
        self._update_diagnostics()
        self._notify_graphs_changed()
        self.renderables_changed.emit()
        if self._pending_renderables:
            self._mesh_timer.start(0)

    def set_hidden_renderables(self, keys: set[str]) -> None:
        self._hidden_renderables = set(keys)
        self._apply_hidden_renderables()

    def set_renderable_visibility_overrides(
        self,
        overrides: Mapping[str, bool],
    ) -> None:
        normalized = {
            str(key): bool(visible)
            for key, visible in overrides.items()
        }
        if normalized == self._visibility_overrides:
            return
        self._visibility_overrides = normalized
        self._apply_hidden_renderables()

    def set_user_renderable_visibility_overrides(
        self,
        overrides: Mapping[str, bool],
    ) -> None:
        normalized = {
            str(key): bool(visible)
            for key, visible in overrides.items()
        }
        if normalized == self._user_visibility_overrides:
            return
        self._user_visibility_overrides = normalized
        self._apply_hidden_renderables()

    def set_renderable_part_overrides(
        self,
        overrides: Mapping[str, tuple[bool, ...]],
    ) -> None:
        normalized = {
            str(key): tuple(bool(enabled) for enabled in parts)
            for key, parts in overrides.items()
        }
        if normalized == self._part_visibility_overrides:
            return
        self._part_visibility_overrides = normalized
        self._apply_hidden_renderables()

    def set_focused_renderables(self, keys: set[str] | None) -> None:
        normalized = (
            None if keys is None else {str(key) for key in keys}
        )
        if normalized == self._focused_renderables:
            return
        self._focused_renderables = normalized
        self._apply_hidden_renderables()

    def set_selection(
        self,
        keys: set[str],
        *,
        preferred_keys: set[str] | None = None,
        focus: bool = False,
    ) -> None:
        self.preview.set_selected_keys(
            set(keys),
            preferred_keys=set(preferred_keys or ()),
            focus=focus,
        )

    def loaded_meshes(self) -> tuple[ScnLoadedMesh, ...]:
        """Expose already loaded scene assets without resolving or parsing again."""
        draw_by_key = {mesh.key: mesh for mesh in self._draw_meshes}
        result = []
        for renderable in (
            item for graph in self.graphs for item in graph.renderables
        ):
            cached = self._mesh_cache.get(self._mesh_cache_key(renderable))
            draw_mesh = draw_by_key.get(renderable.key)
            if cached is None or draw_mesh is None:
                continue
            handler, bind_mesh = cached
            if handler is not None and bind_mesh is not None:
                result.append(
                    ScnLoadedMesh(renderable, handler, bind_mesh)
                )
        return tuple(result)

    def restore_mesh_geometry(self, key: str) -> None:
        loaded = next(
            (item for item in self.loaded_meshes() if item.key == key),
            None,
        )
        if loaded is None:
            return
        self.preview.update_mesh_geometry(
            key,
            loaded.bind_mesh.vertices,
            loaded.bind_mesh.normals,
            recompute_bounds=False,
        )

    def restore_mesh_transforms(self, keys: set[str]) -> None:
        if not keys:
            return
        matrices = {
            renderable.key: renderable.world_matrix
            for graph in self.graphs
            for renderable in graph.renderables
            if renderable.key in keys
        }
        self.preview.update_mesh_transforms(
            matrices,
            recompute_bounds=False,
        )

    def _sync_preview_materials(self) -> None:
        self._materials.refresh()

    def _apply_hidden_renderables(self, *, refresh: bool = True) -> None:
        authored_hidden = {
            renderable.key
            for graph in self.graphs
            for renderable in graph.renderables
            if not renderable.visible_by_default
        }
        for overrides in (
            self._visibility_overrides,
            self._user_visibility_overrides,
        ):
            for key, visible in overrides.items():
                if visible:
                    authored_hidden.discard(key)
                else:
                    authored_hidden.add(key)
        if self._focused_renderables is not None:
            authored_hidden.update(
                renderable.key
                for graph in self.graphs
                for renderable in graph.renderables
                if renderable.key not in self._focused_renderables
            )
        self.preview.set_hidden_keys(
            self._hidden_renderables
            | self._retired_renderables
            | authored_hidden,
            refresh=refresh,
        )
        self.preview.set_hidden_parts(
            {
                renderable.key: {
                    index
                    for index, enabled in enumerate(
                        self._part_visibility_overrides.get(
                            renderable.key,
                            renderable.enabled_parts or (),
                        )
                    )
                    if not enabled
                }
                for graph in self.graphs
                for renderable in graph.renderables
            },
            refresh=refresh,
        )

    def remove_sources(self, rows: set[int]) -> None:
        rows = {row for row in rows if 0 <= row < len(self.graphs)}
        if not rows:
            self._notify_graphs_changed()
            return
        self._stop_timers()
        removed = [renderable for row in rows for renderable in self.graphs[row].renderables]
        removed_keys = {renderable.key for renderable in removed}
        removed_assets = {self._material_asset_key(renderable) for renderable in removed}
        self.graphs = [graph for index, graph in enumerate(self.graphs) if index not in rows]
        self._pending_renderables = deque(item for item in self._pending_renderables if item.renderable.key not in removed_keys)
        self._pending_material_renderables = deque(renderable for renderable in self._pending_material_renderables if renderable.key not in removed_keys)
        self._draw_meshes = [mesh for mesh in self._draw_meshes if mesh.key not in removed_keys]
        self._hidden_renderables.difference_update(removed_keys)
        for overrides in (
            self._visibility_overrides,
            self._user_visibility_overrides,
        ):
            for key in removed_keys:
                overrides.pop(key, None)
        for key in removed_keys:
            self._part_visibility_overrides.pop(key, None)
        if self._focused_renderables is not None:
            self._focused_renderables.difference_update(removed_keys)
        self._retired_renderables.update(removed_keys)
        self._load_light_probe_instances()
        remaining_assets = {self._material_asset_key(renderable) for graph in self.graphs for renderable in graph.renderables}
        for asset in removed_assets - remaining_assets:
            self._materials.remove(asset)
            self._batch_cache.pop(asset, None)
        missing = sum(d.code == "missing_mesh" for graph in self.graphs for d in graph.diagnostics)
        failed = sum(d.code == "mesh_preview_error" for graph in self.graphs for d in graph.diagnostics)
        self._last_asset_counts = (len(self._draw_meshes), missing, failed)
        self._loading = bool(self._pending_renderables)
        if not self.graphs:
            self._loaded = False
            self._camera_initialized = False
            self._retired_renderables.clear()
            self.status_label.setText(self.tr("No SCN is loaded."))
            self.preview.set_scene([], reset_camera=False)
        else:
            self._update_status()
            self._apply_hidden_renderables()
        self._sync_preview_materials()
        self._update_diagnostics()
        self._notify_graphs_changed()
        self.renderables_changed.emit()
        if self._pending_renderables:
            self._mesh_timer.start(0)
        elif self._pending_material_renderables:
            self._material_timer.start(0)

    def _document_ids(self) -> set[str]:
        return {document_id for graph in self.graphs for document_id in graph.documents}

    def refresh(self) -> None:
        self._refresh_queued = False
        self._stop_timers()
        self._clear_runtime_state(keep_hidden=True)
        self.renderables_changed.emit()

        self.status_label.setText(
            self.tr("Building source-aware SCN scene graph...")
        )
        self.graphs = self.loader.build_graphs(self.scene_sources(), max_depth=8)
        if not self.graphs:
            self.status_label.setText(self.tr("No SCN is loaded."))
            self.preview.set_light_probe_instances([])
            self.preview.set_scene([])
            self._sync_preview_materials()
            self._update_diagnostics()
            self._notify_graphs_changed()
            return

        self._load_light_probe_instances()
        self._queue_renderables(self.graphs)
        self._loaded = True
        self.preview.set_scene([])
        self._sync_preview_materials()
        self._update_status()
        self._update_diagnostics()
        self._notify_graphs_changed()
        self._mesh_timer.start(0)

    def _notify_graphs_changed(self) -> None:
        if self._graphs_changed_callback is not None:
            self._graphs_changed_callback()

    def _commit_gizmo_transforms(self, payload) -> None:
        matrices, whole_scene = payload if isinstance(payload, tuple) else (payload, False)
        result = TransformSelectionCommand(self.graphs, self.loader.document_store, matrices).execute()
        self._apply_transform_result(result, matrices, whole_scene)

    def _apply_transform_result(self, result: TransformEditResult, source_matrices: dict[str, object] | None = None, whole_scene: bool = False, *, force_upload: bool = False) -> None:
        if result.matrices and (force_upload or self._needs_transform_upload(result.matrices, source_matrices or {}, whole_scene)):
            self.preview.update_mesh_transforms(result.matrices)
        if result.skipped and self.graphs:
            self.graphs[0].diagnostics.extend(
                self._diagnostic_for_key("warning", "transform_edit_skipped", reason, key)
                for key, reason in result.skipped.items()
            )
        self._update_diagnostics()
        if result.dirty_documents and self._edits_changed_callback is not None:
            self._edits_changed_callback(result.dirty_documents, result.changed_fields)

    @staticmethod
    def _needs_transform_upload(result: dict[str, object], source: dict[str, object], whole_scene: bool) -> bool:
        return whole_scene or set(result) != set(source) or any(not np.allclose(result[key], source[key]) for key in result)

    def _load_light_probe_instances(self) -> None:
        instances = [
            self._load_light_probe_instance(binding)
            for graph in self.graphs
            for binding in graph.light_probes
        ]
        self.preview.set_light_probe_instances(instances)

    def _load_light_probe_instance(self, binding: ScnLightProbeBinding) -> SceneLightProbeInstance:
        cache_key = (
            f"{binding.source_object_id.document_id}|"
            f"{normalize_scene_path(binding.lprb_path).lower()}|"
            f"{normalize_scene_path(binding.prb_path).lower()}"
        )
        if cache_key in self._light_probe_cache:
            probe_set = self._light_probe_cache[cache_key]
        else:
            try:
                lprb = self.loader.resolve_resource(binding.lprb_path, binding.source_object_id.document_id)
                prb = self.loader.resolve_resource(binding.prb_path, binding.source_object_id.document_id)
                if lprb is None or lprb.data is None:
                    raise FileNotFoundError(f"Unable to resolve LPRB resource: {binding.lprb_path}")
                if prb is None or prb.data is None:
                    raise FileNotFoundError(f"Unable to resolve PRB resource: {binding.prb_path}")
                probe_data = parse_light_probe_data(
                    prb_data=prb.data,
                    lprb_data=lprb.data,
                    prb_version=(
                        _resource_version(prb.path, "prb")
                        or _resource_version(binding.prb_path, "prb")
                    ),
                    lprb_version=(
                        _resource_version(lprb.path, "lprb")
                        or _resource_version(binding.lprb_path, "lprb")
                    ),
                )
                probe_set = SceneLightProbeSet.from_data(probe_data)
            except Exception as exc:
                probe_set = None
                self._diagnose_light_probe(binding, str(exc))
            self._light_probe_cache[cache_key] = probe_set
        return SceneLightProbeInstance(
            key=binding.key,
            probe_set=probe_set,
            obbs=binding.obbs,
            priority=binding.priority,
            intensity=binding.intensity,
        )

    def _diagnose_light_probe(self, binding: ScnLightProbeBinding, message: str) -> None:
        for graph in self.graphs:
            if binding in graph.light_probes:
                graph.diagnostics.append(
                    ScnSceneDiagnostic(
                        severity="warning",
                        code="light_probe_preview_error",
                        message=f"Unable to load scene light probes: {message}",
                        document_id=binding.source_object_id.document_id,
                        document_instance_id=binding.document_instance_id,
                        object_id=binding.source_object_id,
                        component_id=binding.source_component_id,
                        path=f"{binding.lprb_path} | {binding.prb_path}",
                    )
                )
                return

    def _queue_renderables(self, graphs: list[ScnSceneGraph]) -> None:
        self._pending_renderables.extend(
            _RenderableQueueItem(graph, renderable)
            for graph in graphs
            for renderable in graph.renderables
        )
        self._loading = bool(self._pending_renderables)

    def _load_mesh_step(self) -> None:
        if not self.graphs:
            return
        loaded, missing, failed = self._last_asset_counts
        deadline = time.perf_counter() + 0.05
        while self._pending_renderables and time.perf_counter() < deadline:
            item = self._pending_renderables.popleft()
            renderable = item.renderable
            try:
                base_mesh = self._mesh_for_renderable(renderable, item.graph)
                if base_mesh is None:
                    missing += 1
                    continue
                self._draw_meshes.append(self._instance_draw_mesh(base_mesh, renderable))
                self._pending_material_renderables.append(renderable)
                loaded += 1
            except Exception as exc:
                failed += 1
                item.graph.diagnostics.append(
                    self._diagnostic(
                        "warning",
                        "mesh_preview_error",
                        f"Failed to build mesh preview for {renderable.mesh_path}: {exc}",
                        renderable,
                    )
                )
        self._last_asset_counts = (loaded, missing, failed)
        self._update_status()
        self._update_diagnostics()
        if self._pending_renderables:
            self._mesh_timer.start(0)
        else:
            self._loading = False
            self._retired_renderables.clear()
            self._apply_hidden_renderables(refresh=False)
            self.preview.set_scene(self._draw_meshes, reset_camera=not self._camera_initialized)
            self._camera_initialized = True
            self.renderables_changed.emit()
            if self._pending_material_renderables:
                self._material_timer.start(0)

    def _resolve_material_step(self) -> None:
        deadline = time.perf_counter() + 0.02
        while self._pending_material_renderables and time.perf_counter() < deadline:
            self._queue_materials(self._pending_material_renderables.popleft())
        if self._pending_material_renderables:
            self._material_timer.start(0)

    def _mesh_for_renderable(self, renderable: ScnRenderableMesh, graph: ScnSceneGraph) -> SceneDrawMesh | None:
        cache_key = self._mesh_cache_key(renderable)
        cached = self._mesh_cache.get(cache_key)
        if cached is not None:
            return cached[1]

        source = self.loader.source_for_graph(graph)
        resolved = self.loader.resolve_resource_for_source(source, renderable.mesh_path)
        if resolved is None or resolved.data is None:
            self._mesh_cache[cache_key] = (None, None)
            graph.diagnostics.append(
                self._diagnostic(
                    "warning",
                    "missing_mesh",
                    f"Unable to resolve mesh resource: {renderable.mesh_path}",
                    renderable,
                )
            )
            return None

        mesh_handler = MeshHandler.from_bytes(
            resolved.path,
            resolved.data,
            app=getattr(getattr(source, "handler", None) or self.handler, "app", None),
            resource_context=self.loader.resource_context_for_source(source),
            game_version=(
                getattr(source, "game_version", "")
                or getattr(self.handler, "game_version", "")
            ),
        )
        mesh = getattr(mesh_handler, "mesh", None)
        base_mesh = None
        if mesh is not None:
            scene_meshes = build_mesh_scene(mesh, key="scn_mesh", include_vertex_colors=False)
            base_mesh = scene_meshes[0] if scene_meshes else None
        self._mesh_cache[cache_key] = (mesh_handler, base_mesh)
        return base_mesh

    def _instance_draw_mesh(self, base_mesh: SceneDrawMesh, renderable: ScnRenderableMesh) -> SceneDrawMesh:
        key = renderable.key
        material_asset_key = self._material_asset_key(renderable)
        material_name, batches = self._batch_cache.get(material_asset_key, (None, None))
        if batches is None:
            material_name = self._material_key(material_asset_key, base_mesh.material_name)
            batches = [
                SceneDrawBatch(
                    indices=batch.indices,
                    material_name=self._material_key(
                        material_asset_key,
                        batch.material_name,
                    ),
                    part_index=batch.part_index,
                )
                for batch in base_mesh.batches
            ]
            self._batch_cache[material_asset_key] = (material_name, batches)
        return replace(
            base_mesh,
            key=key,
            material_name=material_name,
            batches=batches,
            transform_matrix=renderable.world_matrix,
            geometry_key=material_asset_key,
        )

    @staticmethod
    def _material_key(asset_key: str, material_name: str) -> str:
        return scoped_material_key(asset_key, material_name)

    def material_scope_for(self, renderable: ScnRenderableMesh) -> str:
        return self._material_asset_key(renderable)

    def set_material_parameter_values(
        self,
        values: dict[str, dict[str, float]],
    ) -> None:
        self._materials.set_parameter_values(values)

    @staticmethod
    def _material_asset_key(renderable: ScnRenderableMesh) -> str:
        mesh = normalize_scene_path(renderable.mesh_path).lower()
        mdf = normalize_scene_path(renderable.mdf_path).lower()
        return f"{ScnScenePreviewWidget._asset_scope(renderable)}|{mesh}|{mdf}"

    @staticmethod
    def _asset_scope(renderable: ScnRenderableMesh) -> str:
        document_id = renderable.source_object_id.document_id
        return (
            document_id.split("|", 1)[0]
            if "|" in document_id
            else normalize_scene_path(document_id).lower()
        )

    @classmethod
    def _mesh_cache_key(cls, renderable: ScnRenderableMesh) -> str:
        return f"{cls._asset_scope(renderable)}|{normalize_scene_path(renderable.mesh_path).lower()}"

    def _queue_materials(self, renderable: ScnRenderableMesh) -> None:
        material_asset_key = self._material_asset_key(renderable)
        if material_asset_key in self._materials:
            return
        cache_key = self._mesh_cache_key(renderable)
        cached = self._mesh_cache.get(cache_key)
        if cached is None:
            return
        mesh_handler, _base_mesh = cached
        if mesh_handler is None:
            return
        session = MeshMaterialSession(
            mesh_handler,
            explicit_mdf_path=renderable.mdf_path,
            material_scope=material_asset_key,
            resource_scope=self._asset_scope(renderable),
            texture_quality=self._materials.texture_quality,
            parent=self._materials,
        )
        self._materials.add(material_asset_key, session)

    def _update_status(self) -> None:
        if not self.graphs:
            return
        loaded, missing, failed = self._last_asset_counts
        progress = loaded + missing + failed
        documents = sum(len(graph.documents) for graph in self.graphs)
        instances = sum(len(graph.document_instances) for graph in self.graphs)
        links = sum(len(graph.links) for graph in self.graphs)
        renderables = sum(len(graph.renderables) for graph in self.graphs)
        visible_renderables = sum(
            renderable.visible_by_default
            for graph in self.graphs
            for renderable in graph.renderables
        )
        diagnostics = sum(len(graph.diagnostics) for graph in self.graphs)
        light_probe_count = sum(len(graph.light_probes) for graph in self.graphs)
        light_probe_status = self.preview.light_probe_status or (
            self.tr("LightProbes: {count}").format(count=light_probe_count)
            if light_probe_count
            else self.tr("LightProbes: none")
        )
        prepared_textures, gpu_textures, failed_textures = self.preview.texture_upload_counts()
        texture_status = self.tr("Textures GPU: {gpu}/{prepared}").format(
            gpu=gpu_textures, prepared=prepared_textures
        )
        if failed_textures:
            texture_status += self.tr(" | Upload failed: {count}").format(
                count=failed_textures
            )
        effect_errors = self.preview.material_effect_errors()
        if effect_errors:
            texture_status += self.tr(" | Material effects failed: {count}").format(
                count=len(effect_errors)
            )
        self.status_label.setText(
            " | ".join(
                [
                    self.tr("Sources: {count}").format(count=len(self.scene_sources())),
                    self.tr("Documents: {count}").format(count=documents),
                    self.tr("Instances: {count}").format(count=instances),
                    self.tr("Links: {count}").format(count=links),
                    self.tr("Authored visible: {visible}/{total}").format(
                        visible=visible_renderables,
                        total=renderables,
                    ),
                    self.tr("Preview: {progress}/{total}").format(
                        progress=progress, total=renderables
                    )
                    if self._loading
                    else self.tr("Preview ready"),
                    self.tr("Meshes loaded: {count}").format(count=loaded),
                    self.tr("Missing: {count}").format(count=missing),
                    self.tr("Failed: {count}").format(count=failed),
                    texture_status,
                    light_probe_status,
                    self.tr("Diagnostics: {count}").format(count=diagnostics),
                ]
            )
        )
        self.status_label.setToolTip("\n".join(effect_errors))

    def _update_diagnostics(self) -> None:
        new = []
        for diagnostic in (item for graph in self.graphs for item in graph.diagnostics):
            key = (diagnostic.severity, diagnostic.code, diagnostic.message, diagnostic.document_id, diagnostic.document_instance_id, diagnostic.path)
            if key not in self._shown_diagnostics:
                self._shown_diagnostics.add(key)
                new.append(diagnostic)
        for diagnostic in new:
            print(f"Scene {self._diagnostic_text(diagnostic)}")

    @staticmethod
    def _diagnostic_text(diagnostic: ScnSceneDiagnostic) -> str:
        source = diagnostic.document_instance_id or diagnostic.document_id
        if diagnostic.path:
            source = f"{source} :: {diagnostic.path}" if source else diagnostic.path
        return f"[{diagnostic.severity}] {diagnostic.code}: {diagnostic.message} {source}".strip()

    @staticmethod
    def _diagnostic(severity: str, code: str, message: str, renderable: ScnRenderableMesh):
        return ScnSceneDiagnostic(
            severity=severity,
            code=code,
            message=message,
            document_id=renderable.source_object_id.document_id,
            document_instance_id=renderable.document_instance_id,
            object_id=renderable.source_object_id,
            component_id=renderable.source_component_id,
            path=renderable.mesh_path,
        )

    def _diagnostic_for_key(self, severity: str, code: str, message: str, key: str):
        for graph in self.graphs:
            for renderable in graph.renderables:
                if renderable.key == key:
                    return self._diagnostic(severity, code, message, renderable)
        return ScnSceneDiagnostic(severity, code, message)
