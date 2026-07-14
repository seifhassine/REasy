from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from file_handlers.lightprobe.loader import parse_prb9_light_probe_data
from file_handlers.mesh.material_resolver import MdfSurfaceProfile, MeshMaterialBinding, MeshMaterialResolver
from file_handlers.mesh.mesh_handler import MeshHandler
from file_handlers.tex.qt_image_utils import TexPreviewUpload, build_tex_preview_upload, parse_tex_bytes
from file_handlers.tex.texture_quality import choose_texture_mip, texture_quality_profile
from ui.scene.lightprobe_preview import SceneLightProbeSet
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
class _MaterialQueueItem:
    material_key: str
    resource_scope: str
    handler: MeshHandler
    binding: MeshMaterialBinding


@dataclass(slots=True)
class _RenderableQueueItem:
    graph: ScnSceneGraph
    renderable: ScnRenderableMesh


class ScnScenePreviewWidget(QWidget):
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
        self._resolved_texture_cache: dict[str, dict[tuple[bool, str], tuple[str, bytes] | None]] = {}
        self._texture_cache: dict[tuple[str, str], TexPreviewUpload | None] = {}
        self._light_probe_cache: dict[str, SceneLightProbeSet | None] = {}
        self._material_queue: deque[_MaterialQueueItem] = deque()
        self._material_images: dict[str, tuple[str, TexPreviewUpload]] = {}
        self._material_profiles: dict[str, MdfSurfaceProfile] = {}
        self._queued_material_assets: set[str] = set()
        self._shown_diagnostics: set[tuple] = set()
        self._pending_renderables: deque[_RenderableQueueItem] = deque()
        self._pending_material_renderables: deque[ScnRenderableMesh] = deque()
        self._draw_meshes: list[SceneDrawMesh] = []
        self._hidden_renderables: set[str] = set()
        self._retired_renderables: set[str] = set()
        self._loading = False
        self._refresh_queued = False
        self._camera_initialized = False
        self._last_asset_counts = (0, 0, 0)

        self._texture_timer = QTimer(self)
        self._texture_timer.setSingleShot(True)
        self._texture_timer.timeout.connect(self._warm_texture_step)

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
        self._material_queue.clear()
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

    def _on_texture_quality_changed(self, _quality: str) -> None:
        self._material_timer.stop()
        self._texture_timer.stop()
        self._pending_material_renderables.clear()
        self._material_queue.clear()
        self._material_images.clear()
        self._texture_cache.clear()
        self._queued_material_assets.clear()
        self.preview.set_material_images({})
        self._pending_material_renderables.extend(
            renderable
            for graph in self.graphs
            for renderable in graph.renderables
            if (cached := self._mesh_cache.get(self._mesh_cache_key(renderable))) and cached[0] is not None
        )
        self._update_status()
        if self._pending_material_renderables:
            self._material_timer.start(0)

    def sync_raw_transform_field(self, document_ids: set[str], changed_field: object) -> TransformEditResult:
        result = RawTransformFieldCommand(self.graphs, self.loader.document_store, document_ids, changed_field).execute()
        if result.handled:
            self._apply_transform_result(result, force_upload=True)
        return result

    def _stop_timers(self) -> None:
        self._mesh_timer.stop()
        self._material_timer.stop()
        self._texture_timer.stop()

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
        self._retired_renderables.clear()
        self._material_queue.clear()
        self._material_images.clear()
        self._material_profiles.clear()
        self._queued_material_assets.clear()
        self._mesh_cache.clear()
        self._batch_cache.clear()
        self._resolved_texture_cache.clear()
        self._texture_cache.clear()
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
        self._load_light_probe_set()
        self._queue_renderables(graphs)
        self._update_status()
        self._update_diagnostics()
        self._notify_graphs_changed()
        if self._pending_renderables:
            self._mesh_timer.start(0)

    def set_hidden_renderables(self, keys: set[str]) -> None:
        self._hidden_renderables = set(keys)
        self._apply_hidden_renderables()

    def set_selection(self, keys: set[str], *, focus: bool = False) -> None:
        self.preview.set_selected_keys(set(keys), focus=focus)

    def _sync_preview_materials(self) -> None:
        self.preview.set_material_images(self._material_images)
        self.preview.set_material_profiles(self._material_profiles)

    def _apply_hidden_renderables(self, *, refresh: bool = True) -> None:
        self.preview.set_hidden_keys(self._hidden_renderables | self._retired_renderables, refresh=refresh)

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
        self._retired_renderables.update(removed_keys)
        self._load_light_probe_set()
        remaining_assets = {self._material_asset_key(renderable) for graph in self.graphs for renderable in graph.renderables}
        unused_prefixes = tuple(f"{asset}:" for asset in removed_assets - remaining_assets)
        if unused_prefixes:
            self._material_queue = deque(item for item in self._material_queue if not item.material_key.startswith(unused_prefixes))
            self._material_images = {key: value for key, value in self._material_images.items() if not key.startswith(unused_prefixes)}
            self._material_profiles = {key: value for key, value in self._material_profiles.items() if not key.startswith(unused_prefixes)}
            self._queued_material_assets.difference_update(removed_assets - remaining_assets)
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
        if self._pending_renderables:
            self._mesh_timer.start(0)
        elif self._pending_material_renderables:
            self._material_timer.start(0)
        elif self._material_queue:
            self._texture_timer.start(0)

    def _document_ids(self) -> set[str]:
        return {document_id for graph in self.graphs for document_id in graph.documents}

    def refresh(self) -> None:
        self._refresh_queued = False
        self._stop_timers()
        self._clear_runtime_state(keep_hidden=True)

        self.status_label.setText(
            self.tr("Building source-aware SCN scene graph...")
        )
        self.graphs = self.loader.build_graphs(self.scene_sources(), max_depth=8)
        if not self.graphs:
            self.status_label.setText(self.tr("No SCN is loaded."))
            self.preview.set_scene([])
            self._sync_preview_materials()
            self._update_diagnostics()
            self._notify_graphs_changed()
            return

        self._load_light_probe_set()
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

    def _load_light_probe_set(self) -> None:
        binding = self._first_light_probe_binding()
        if binding is None:
            self.preview.set_light_probe_set(None, "", [], "")
            return

        cache_key = (
            f"{binding.source_object_id.document_id}|"
            f"{normalize_scene_path(binding.lprb_path).lower()}|"
            f"{normalize_scene_path(binding.prb_path).lower()}"
        )
        if cache_key in self._light_probe_cache:
            probe_set = self._light_probe_cache[cache_key]
            self.preview.set_light_probe_set(
                probe_set,
                self._light_probe_status(binding, probe_set is not None),
                binding.obbs,
                binding.key,
            )
            return

        try:
            lprb = self.loader.resolve_resource(binding.lprb_path, binding.source_object_id.document_id)
            prb = self.loader.resolve_resource(binding.prb_path, binding.source_object_id.document_id)
            if lprb is None or lprb.data is None:
                raise FileNotFoundError(f"Unable to resolve LPRB resource: {binding.lprb_path}")
            if prb is None or prb.data is None:
                raise FileNotFoundError(f"Unable to resolve PRB resource: {binding.prb_path}")
            probe_data = parse_prb9_light_probe_data(
                prb_data=prb.data,
                lprb_data=lprb.data,
            )
            probe_set = SceneLightProbeSet.from_data(probe_data)
        except Exception as exc:
            probe_set = None
            self._diagnose_light_probe(binding, str(exc))
        self._light_probe_cache[cache_key] = probe_set
        self.preview.set_light_probe_set(
            probe_set,
            self._light_probe_status(binding, probe_set is not None),
            binding.obbs,
            binding.key,
        )

    def _first_light_probe_binding(self) -> ScnLightProbeBinding | None:
        for graph in self.graphs:
            if graph.light_probes:
                return graph.light_probes[0]
        return None

    @staticmethod
    def _light_probe_status(binding: ScnLightProbeBinding, loaded: bool) -> str:
        state = "loaded" if loaded else "missing"
        obb_count = len(getattr(binding, "obbs", ()) or ())
        suffix = f" | OBBs: {obb_count}" if obb_count else ""
        return f"Light probes {state}: {Path(binding.lprb_path).name} + {Path(binding.prb_path).name}{suffix}"

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
            if self._pending_material_renderables:
                self._material_timer.start(0)

    def _resolve_material_step(self) -> None:
        deadline = time.perf_counter() + 0.02
        while self._pending_material_renderables and time.perf_counter() < deadline:
            self._queue_materials(self._pending_material_renderables.popleft())
        if self._material_queue and not self._texture_timer.isActive():
            self._texture_timer.start(0)
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

        mesh_handler = MeshHandler()
        mesh_handler.filepath = resolved.path
        mesh_handler.app = getattr(getattr(source, "handler", None) or self.handler, "app", None)
        mesh_handler._resource_context = self.loader.resource_context_for_source(source)
        mesh_handler.read(resolved.data)
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
            batches = [SceneDrawBatch(indices=batch.indices, material_name=self._material_key(material_asset_key, batch.material_name)) for batch in base_mesh.batches]
            self._batch_cache[material_asset_key] = (material_name, batches)
        return SceneDrawMesh(
            key=key,
            vertices=base_mesh.vertices,
            indices=base_mesh.indices,
            color=base_mesh.color,
            force_solid=base_mesh.force_solid,
            ignore_highlight_filter=base_mesh.ignore_highlight_filter,
            normals=base_mesh.normals,
            uvs=base_mesh.uvs,
            colors=base_mesh.colors,
            material_name=material_name,
            batches=batches,
            transform_matrix=renderable.world_matrix,
            geometry_key=material_asset_key,
        )

    @staticmethod
    def _material_key(asset_key: str, material_name: str) -> str:
        return f"{asset_key}:{material_name or '<default>'}"

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

    def _quality_profile(self):
        return texture_quality_profile(self.preview.texture_quality)

    def _queue_materials(self, renderable: ScnRenderableMesh) -> None:
        material_asset_key = self._material_asset_key(renderable)
        if material_asset_key in self._queued_material_assets:
            return
        self._queued_material_assets.add(material_asset_key)
        cache_key = self._mesh_cache_key(renderable)
        cached = self._mesh_cache.get(cache_key)
        if cached is None:
            return
        mesh_handler, _base_mesh = cached
        if mesh_handler is None:
            return
        _resolved_mdf, bindings = MeshMaterialResolver.resolve_for_handler(
            mesh_handler,
            explicit_mdf_path=renderable.mdf_path,
            prefer_streaming=self._quality_profile().prefer_streaming,
            resolve_textures=False,
            parse_in_subprocess=True,
        )
        profiles = {}
        for binding in bindings:
            material_key = self._material_key(material_asset_key, binding.mesh_material_name)
            if binding.surface is not None:
                profiles[material_key] = binding.surface
            if not binding.texture_path:
                continue
            self._material_queue.append(
                _MaterialQueueItem(
                    material_key=material_key,
                    resource_scope=self._asset_scope(renderable),
                    handler=mesh_handler,
                    binding=binding,
                )
            )
        if profiles:
            self._material_profiles.update(profiles)
            self.preview.update_material_profiles(profiles)

    def _warm_texture_step(self) -> None:
        deadline = time.perf_counter() + 0.02
        loaded = {}
        processed = 0
        while self._material_queue and (processed == 0 or time.perf_counter() < deadline):
            processed += 1
            item = self._material_queue.popleft()
            texture = self._load_texture_image(item)
            if texture is None or not item.binding.resolved_texture_path:
                continue
            source_key = f"{item.resource_scope}|{item.binding.resolved_texture_path}"
            self._material_images[item.material_key] = (source_key, texture)
            loaded[item.material_key] = self._material_images[item.material_key]
        if loaded:
            self.preview.update_material_images(loaded)
            self._update_status()
        if self._material_queue:
            self._texture_timer.start(0)

    def _load_texture_image(self, item: _MaterialQueueItem) -> TexPreviewUpload | None:
        handler, binding = item.handler, item.binding
        if not binding.resolved_texture_path:
            resolved = MeshMaterialResolver.resolve_texture_path(
                handler,
                binding.texture_path,
                prefer_streaming=self._quality_profile().prefer_streaming,
                resource_cache=self._resolved_texture_cache.setdefault(item.resource_scope, {}),
            )
            if resolved is None:
                binding.status = "Texture not found"
                print(
                    f"Texture resolution failed: scope={item.resource_scope!r}, "
                    f"material={binding.mesh_material_name!r}, "
                    f"path={binding.texture_path!r}, quality={self._quality_profile().label}"
                )
                return None
            binding.resolved_texture_path, binding.resolved_texture_data = resolved
            binding.status = "Resolved"
        path = binding.resolved_texture_path
        cache_key = item.resource_scope, path
        if cache_key in self._texture_cache:
            return self._texture_cache[cache_key]
        try:
            tex = (
                parse_tex_bytes(binding.resolved_texture_data, raise_errors=True)
                if binding.resolved_texture_data
                else None
            )
            upload = build_tex_preview_upload(tex, mip_selector=self._choose_preview_mip)
        except Exception as exc:
            print(f"Texture preparation failed: scope={item.resource_scope!r}, path={path!r}: {exc}")
            upload = None
        self._texture_cache[cache_key] = upload
        return self._texture_cache[cache_key]

    def _choose_preview_mip(self, tex) -> int:
        return choose_texture_mip(tex, self.preview.texture_quality)

    def _update_status(self) -> None:
        if not self.graphs:
            return
        loaded, missing, failed = self._last_asset_counts
        progress = loaded + missing + failed
        documents = sum(len(graph.documents) for graph in self.graphs)
        instances = sum(len(graph.document_instances) for graph in self.graphs)
        links = sum(len(graph.links) for graph in self.graphs)
        renderables = sum(len(graph.renderables) for graph in self.graphs)
        diagnostics = sum(len(graph.diagnostics) for graph in self.graphs)
        light_probe_count = sum(len(graph.light_probes) for graph in self.graphs)
        light_probe_status = getattr(self.preview, "_light_probe_status", "") or (
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
        self.status_label.setText(
            " | ".join(
                [
                    self.tr("Sources: {count}").format(count=len(self.scene_sources())),
                    self.tr("Documents: {count}").format(count=documents),
                    self.tr("Instances: {count}").format(count=instances),
                    self.tr("Links: {count}").format(count=links),
                    self.tr("Renderables: {count}").format(count=renderables),
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
