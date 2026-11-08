"""See docs/internals/mesh-preview.md."""
from __future__ import annotations

import time
from collections import OrderedDict, deque
from collections.abc import Iterable
from concurrent.futures import Future
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Callable

lazy import numpy as np
from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

lazy from file_handlers.lightprobe.loader import parse_light_probe_data
lazy from file_handlers.mesh.material_session import (
    MeshMaterialCollection,
    MeshMaterialSession,
    scoped_material_key,
)
lazy from file_handlers.mesh.mesh_handler import MeshHandler
lazy from file_handlers.stmesh.stmesh_handler import StmeshHandler
lazy from file_handlers.mesh.mesh_file import MeshLoadMode
lazy from file_handlers.mesh.scene_decals import SceneDecalSource
lazy from file_handlers.mesh.scene_primitives import ScenePrimitiveSource
lazy from file_handlers.mesh.scene_lit import SceneDevicePreview
lazy from file_handlers.mesh.scene_render_targets import SceneRenderTargetSource
lazy from file_handlers.mesh.scene_shallow_water import SceneShallowWaterFluid, SceneShallowWaterSource
lazy from file_handlers.mesh.scene_wrinkles import scene_wrinkles
lazy from file_handlers.mesh.material_resolver import MeshMaterialResolver
lazy from file_handlers.mmtr.draw_rules import MESH_COMPONENT, drawing_rules
lazy from file_handlers.mmtr.profiles import mmtr_profile_for_game
lazy from file_handlers.mmtr.wrinkle import wrinkle_catalog_for
lazy from file_handlers.sfur.scene import scene_shell_furs
lazy from file_handlers.sfur.shell_fur import shell_fur_catalog_for
lazy from ui.qt_timers import call_later
lazy from ui.scene.lightprobe_preview import SceneLightProbeInstance, SceneLightProbeSet
lazy from ui.scene.mesh_scene import build_mesh_scene, group_submesh_numbers
lazy from ui.scene.scene_model import SceneDrawBatch, SceneDrawMesh
lazy from ui.scene.scene_preview import ScenePreviewWidget
lazy from ui.scene.scene_view import ScenePose
from utils.resource_file_utils import held_folder_lookups, resource_version_from_path, submit_read

lazy from .campaign_state import CampaignChoice, CampaignChoices
lazy from .scn_scene_loader import ScnSceneLoader, ScnSceneSource, read_scene_resource, scene_source_file
lazy from .scn_document_store import ScnDocumentStore
lazy from .material_variations import VariationEntry, set_entry
lazy from .spawn_rules import SpawnChoice
lazy from .spawned_parts import MaterialVariation, PartColours
lazy from .scn_scene_commands import RawTransformFieldCommand, TransformEditResult, TransformSelectionCommand
lazy from .scn_scene_graph import (
    ScnObjectId,
    ScnSceneDiagnostic,
    ScnLightProbeBinding,
    ScnRenderableMesh,
    ScnSceneGraph,
    camera_ranges_shown,
    camera_shows,
    normalize_scene_path,
    parent_of,
    renderable_visible,
)
lazy from .scene_time import SceneTime
from .scene_time_preview import ScnSceneTimePreview, _TimeTurns


_resource_version = resource_version_from_path


def _track_shown(graph: ScnSceneGraph, renderable: ScnRenderableMesh, drawn: dict) -> bool | None:
    document = graph.documents.get(renderable.source_object_id.document_id)
    item, seen, named = None if document is None else document.objects.get(renderable.source_object_id), set(), False
    while item is not None and item.id not in seen:
        if (value := drawn.get(item.id)) is False:
            return False
        named |= value is not None
        seen.add(item.id)
        item = parent_of(document, item)
    instance = renderable.document_instance_id
    if not named or not graph.document_instances[instance].drawn or (component := document.components.get(
            renderable.source_component_id)) is None:
        return None
    source, startup = document.objects[renderable.source_object_id], graph.startup_drawn.get(instance, {})
    return True if renderable_visible(graph.field_names, document, source, component, startup | drawn) and not \
        renderable_visible(graph.field_names, document, source, component, startup) else None


def _switched(own, switch: dict) -> dict:
    parts = dict.fromkeys(range(len(own or ())), switch[None]) if None in switch else dict(enumerate(own or ()))
    return parts | {index: enabled for index, enabled in switch.items() if index is not None}


@dataclass(slots=True)
class _RenderableQueueItem:
    graph: ScnSceneGraph
    renderable: ScnRenderableMesh


@dataclass(frozen=True, slots=True)
class ScnLoadedMesh:
    renderable: ScnRenderableMesh
    handler: MeshHandler

    @property
    def key(self) -> str:
        return self.renderable.key


class ScnScenePreviewWidget(QWidget, ScnSceneTimePreview):
    renderables_changed = Signal()
    documents_edited = Signal(object, object)
    user_edits_changed = Signal()
    MESH_READS_AHEAD = 64

    def __init__(
        self,
        owner,
        sources_getter: Callable[[], list[ScnSceneSource]] | None = None,
        graphs_changed_callback: Callable[[], None] | None = None,
        document_store: ScnDocumentStore | None = None,
        settings: dict | None = None,
    ):
        super().__init__(owner)
        self.handler = getattr(owner, "handler", None)
        self._sources_getter = sources_getter
        self._graphs_changed_callback = graphs_changed_callback
        self.graphs: list[ScnSceneGraph] = []
        self.loader = ScnSceneLoader(document_store)
        self.campaign: CampaignChoice | None = None
        self.variations: dict[str, int] = {}
        self.spawn_choices: dict[str, int] = {}
        self._stale = False
        self._mesh_cache: dict[str, tuple[MeshHandler | None, SceneDrawMesh | None]] = {}
        # By document id: the scope of its source's meshes.
        self._mesh_scopes: dict[str, str] = {}
        # By mesh cache key: the key of the renderable it was read for, and the read.
        self._mesh_reads: dict[str, tuple[str, Future]] = {}
        # Reads not started yet, in queue order: by mesh cache key, the renderable key and the read's arguments.
        self._unread_meshes: OrderedDict[str, tuple[str, tuple]] = OrderedDict()
        self._mesh_failures: dict[str, Exception] = {}
        self._batch_cache: dict[str, tuple[str, list[SceneDrawBatch]]] = {}
        self._light_probe_cache: dict[str, SceneLightProbeSet | None] = {}
        self._shown_diagnostics: set[tuple] = set()
        self._pending_renderables: deque[_RenderableQueueItem] = deque()
        self._pending_material_renderables: OrderedDict[str, _RenderableQueueItem] = OrderedDict()
        self._draw_meshes: list[SceneDrawMesh] = []
        self._hidden_renderables: set[str] = set()
        self._camera_states: dict[int, bool] = {}
        self._range_states: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        self._camera_visibility: dict[str, bool] = {}
        self._track_state: tuple[dict, dict] = ({}, {})
        self._camera_stopped: frozenset[ScnObjectId] = frozenset()
        self._camera_eye: tuple | None = None
        self._retired_renderables: set[str] = set()
        # An SCN added to the scene shown loads.
        self._adding = False
        self._refresh_queued = False
        self._camera_initialized = False
        self._last_asset_counts = (0, 0, 0, 0)
        self._scene_times: list[tuple[ScnSceneGraph, SceneTime]] = []
        self._time_camera: tuple[float, float, float] | None = None
        self._time_targets: set[ScnObjectId] = set()
        self._time_keys: dict[tuple[ScnObjectId, str], set[str]] = {}
        self._time_turns: list[_TimeTurns] = []
        self._time_decals: set[str] = set()
        self._varied: set[str] = set()
        self._variation_values: dict[str, dict[str, tuple[float, ...]]] = {}
        # With constraint_materials (set before loading), the objects holding JointConstraints layers get their own
        # material keys, and set_pose_materials gives by those keys what the layers set.
        self.constraint_materials = False
        self._pose_materials: dict[str, dict[str, float | tuple[float, ...]]] = {}
        self._mesh_mdfs: dict[str, str] = {}
        self._mesh_flags: dict[str, frozenset] = {}

        self._mesh_timer = QTimer(self)
        self._mesh_timer.setSingleShot(True)
        self._mesh_timer.timeout.connect(self._load_mesh_step)

        self._material_timer = QTimer(self)
        self._material_timer.setSingleShot(True)
        self._material_timer.timeout.connect(self._resolve_material_step)

        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self._update_status)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.status_label = QLabel(self.tr("Scene preview has not been built."), self)
        self.status_label.setWordWrap(True)

        self.preview = ScenePreviewWidget(self, settings=settings)
        self._materials = MeshMaterialCollection(self.preview, parent=self)
        self._device_preview = SceneDevicePreview(self.preview, self._materials, self)
        self._device_preview.status_changed.connect(self._schedule_status_update)
        self.preview.component_sources.update(
            wrinkles=self._wrinkles, shell_furs=self._shell_furs, decals=self._volume_decals,
            primitives=self._primitives, render_targets=self._render_targets, shallow_waters=self._shallow_waters)
        self.preview.gizmo_transform_committed.connect(self._commit_gizmo_transforms)
        self.preview.texture_quality_changed.connect(self._on_texture_quality_changed)
        self.preview.device_view_changed.connect(self._follow_camera)
        self.preview.time_step, self.preview.time_running = self._step_time, self._time_running
        self.preview.setMinimumHeight(320)
        layout.addWidget(self.preview, 1)

    def ensure_loaded(self) -> None:
        if not self.graphs:
            self.request_refresh()

    def request_refresh(self) -> None:
        if self._refresh_queued:
            return
        self._stop_timers()
        self._pending_renderables.clear()
        self._pending_material_renderables.clear()
        self._refresh_queued = True
        self.status_label.setText(self.tr("Preparing scene preview..."))
        call_later(self, 0, self.refresh)

    def campaign_choices(self) -> CampaignChoices | None:
        return next(filter(None, map(self.loader.campaign_choices, self.graphs)), None)

    def set_campaign(self, campaign: CampaignChoice | None) -> None:
        if campaign == self.campaign:
            return
        self.campaign = campaign
        self.request_refresh()

    def material_variations(self) -> list[MaterialVariation]:
        return [variation for graph in self.graphs for variation in graph.variations]

    def spawner_choices(self) -> list[SpawnChoice]:
        return [choice for graph in self.graphs for choice in graph.spawn_choices]

    def set_spawn_choices(self, choices: dict[str, int]) -> None:
        if choices == self.spawn_choices:
            return
        self.spawn_choices = choices
        self.request_refresh()

    def variation_colours(self, key: str) -> PartColours | None:
        mesh = next((mesh for mesh in self._draw_meshes if mesh.key == key), None)
        if mesh is None:
            return None
        for graph in self.graphs:
            item = next((item for item in graph.renderables if item.key == key), None)
            spawned = None if item is None else graph.spawned_meshes.get(
                (item.document_instance_id, item.source_component_id))
            if spawned is None or not spawned.entries:
                continue
            component, materials = item.decal_receive[0], {}
            last = {(entry.material, entry.parameter): entry for entry in spawned.entries if entry.applied}
            for name, surface, rules in self._mesh_profiles(mesh):
                params = rules.material_params.get(component)
                written = surface if params is None else rules.overridden(surface, component,
                                                                           {params.list: spawned.writes})
                drawn = {parameter.name: parameter.values for parameter in written.parameters}
                listed = [entry for entry in spawned.entries if entry.kind == "colour"
                          and entry.material == surface.material_name and entry.parameter in drawn]
                parameters = [entry.parameter for entry in listed]
                materials[name] = surface.material_name, tuple(
                    (entry, last.get((entry.material, entry.parameter)) is entry
                     and drawn[entry.parameter] == entry.values)
                    for entry in sorted(listed, key=lambda entry: parameters.index(entry.parameter)))
            return PartColours(next(variation for variation in graph.variations
                                    if variation.key == item.document_instance_id), spawned.part, spawned.mesh,
                               materials)
        return None

    def set_variations(self, variations: dict[str, int], *, reload: bool = False) -> None:
        if reload:
            self.loader.user_reads.clear()
        elif variations == self.variations:
            return
        self.variations = variations
        self._redraw_variations()

    def set_variation_entry(self, key: str, entry: VariationEntry, raw: tuple, applied: bool) -> None:
        graph = next((graph for graph in self.graphs if any(item.key == key for item in graph.renderables)), None)
        document = None if graph is None else self.loader.user_document(graph, entry.file)
        if document is None:
            return
        set_entry(document.rsz_file, entry, raw, applied)
        self._redraw_variations()
        self.user_edits_changed.emit()

    def user_edited(self, path: str) -> bool:
        return normalize_scene_path(path) in self.loader.user_documents

    def discard_user_edits(self, path: str | None = None) -> None:
        documents = self.loader.user_documents
        for key in [normalize_scene_path(path)] if path else list(documents):
            documents.pop(key, None)
        self._redraw_variations()
        self.user_edits_changed.emit()

    def save_user_edits(self) -> int:
        documents = self.loader.user_documents
        self.loader.user_reads.clear()
        count = len(documents)
        for key, document in list(documents.items()):
            target = Path(document.save_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(document.rsz_file.build())
            del documents[key]
        if count:
            self.user_edits_changed.emit()
        return count

    def _redraw_variations(self) -> None:
        if not self.graphs or self._pending_renderables:
            self.request_refresh()
            return
        meshes, changed = {mesh.key: mesh for mesh in self._draw_meshes}, {}
        for graph in self.graphs:
            if not graph.variations:
                continue
            picked, states = self.loader.variation_states(graph, self.variations)
            graph.variations = picked.variations
            graph.spawned_meshes.update(picked.spawned_meshes)
            for item in graph.renderables:
                state, mesh = states.get(item.key), meshes.get(item.key)
                if state is None or mesh is None:
                    continue
                for name, surface, rules in self._mesh_profiles(mesh):
                    built, now = (rules.overridden(surface, item.decal_receive[0], fields)
                                  for fields in (item.draw_state, state))
                    if (built.textures, built.gpu_buffers) != (now.textures, now.gpu_buffers):
                        self.request_refresh()
                        return
                    changed[name] = {old.name: new.values for old, new in zip(built.parameters, now.parameters)
                                     if old.values != new.values}
        self._variation_values = {name: changed.get(name, {}) for name in self._variation_values}
        self._show_time()

    def _mesh_profiles(self, mesh: SceneDrawMesh):
        for name in dict.fromkeys(batch.material_name for batch in mesh.batches):
            session = self._materials.owner(name)
            if session is not None and name in session.profiles:
                yield name, session.profiles[name], drawing_rules(mmtr_profile_for_game(
                    MeshMaterialResolver.material_profile(session.handler).game))

    def set_stale(self) -> None:
        self._stale = True
        self._update_status()

    def _on_texture_quality_changed(self, quality: str) -> None:
        self._materials.set_texture_quality(quality)
        self._update_status()

    def sync_raw_transform_field(self, document_ids: set[str], changed_field: object) -> TransformEditResult:
        result = RawTransformFieldCommand(self.graphs, self.loader.document_store, document_ids, changed_field).execute()
        if result.handled:
            self._apply_transform_result(result, force_upload=True)
        return result

    def _catalogued(self, catalog_for: Callable) -> Iterable[tuple[object, object]]:
        for graph in self.graphs:
            source = self.loader.source_for_graph(graph)
            profile = mmtr_profile_for_game(getattr(source, "game_version", "")
                                            or getattr(self.handler, "game_version", ""))
            catalog = None if profile is None else catalog_for(profile)
            if catalog is not None:
                yield graph, catalog

    def _wrinkles(self) -> list:
        return [found for graph, catalog in self._catalogued(wrinkle_catalog_for)
                for found in scene_wrinkles(graph, catalog)]

    def _shell_furs(self) -> list:
        return [found for graph, catalog in self._catalogued(shell_fur_catalog_for)
                for found in scene_shell_furs(graph, catalog, MESH_COMPONENT)]

    def _resolver(self, document_id: str) -> Callable[[str], tuple[str, bytes] | None]:
        def resolve(path: str) -> tuple[str, bytes] | None:
            found = self.loader.resolve_resource(path, document_id)
            return None if found is None or found.data is None else (found.path, found.data)
        return resolve

    def _volume_decals(self) -> list:
        return [SceneDecalSource(decal.key, np.asarray(decal.world_matrix, np.float32).T, decal.fields,
                                 decal.visible_by_default, self._resolver(decal.source_object_id.document_id))
                for graph in self.graphs for decal in graph.volume_decals]

    def _primitives(self) -> list:
        return [ScenePrimitiveSource(primitive.key, np.asarray(primitive.world_matrix, np.float64)[:3],
                                     primitive.fields, primitive.visible_by_default)
                for graph in self.graphs for primitive in graph.primitives]

    def _render_targets(self) -> list:
        return [SceneRenderTargetSource(target.key, target.fields, target.visible_by_default,
                                        self._resolver(target.source_object_id.document_id))
                for graph in self.graphs for target in graph.render_targets]

    def _shallow_waters(self) -> list:
        return [SceneShallowWaterSource(
            water.key, water.component, np.asarray(water.world_matrix, np.float32).T, water.scale,
            None if water.point_world is None else np.asarray(water.point_world, np.float32).T, water.point_scale,
            water.point_missing, water.fields, water.visible_by_default,
            self._resolver(water.source_object_id.document_id), water.controller,
            None if water.fluid is None else SceneShallowWaterFluid(
                np.asarray(water.fluid.world_matrix, np.float32).T, water.fluid.rotation, water.fluid.scale,
                water.fluid.resolution, water.fluid.drawn), water.fluid_children)
            for graph in self.graphs for water in graph.shallow_waters]

    def strands_changed(self) -> None:
        self._device_preview.strands_changed()
        self.preview.update()

    def _stop_timers(self) -> None:
        self._mesh_timer.stop()
        self._material_timer.stop()
        self._status_timer.stop()

    def cleanup(self) -> None:
        self._stop_timers()
        self._clear_runtime_state()
        self.preview.cleanup()
        self._sources_getter = self._graphs_changed_callback = None
        self.handler = None

    def _clear_runtime_state(self, *, keep_hidden: bool = False) -> None:
        self._pending_renderables.clear()
        self._pending_material_renderables.clear()
        self._draw_meshes.clear()
        if not keep_hidden:
            self._hidden_renderables.clear()
        self._camera_states.clear()
        self._range_states.clear()
        self._camera_visibility.clear()
        self._track_state = ({}, {})
        self._camera_stopped = frozenset()
        self._camera_eye = None
        self._retired_renderables.clear()
        self._materials.clear()
        self._mesh_cache.clear()
        for _owner, read in self._mesh_reads.values():
            read.cancel()
        self._mesh_reads.clear()
        self._unread_meshes.clear()
        self._mesh_failures.clear()
        self._mesh_scopes.clear()
        self._batch_cache.clear()
        self._light_probe_cache.clear()
        self._shown_diagnostics.clear()
        self._refresh_queued = self._stale = self._adding = False
        self._camera_initialized = False
        self._last_asset_counts = (0, 0, 0, 0)
        self.graphs.clear()
        self._scene_times.clear()
        self._time_camera = None
        self._time_targets.clear()
        self._time_keys.clear()
        self._time_turns.clear()
        self._time_decals.clear()
        self._varied.clear()
        self._variation_values = {}
        self._pose_materials, self._mesh_mdfs, self._mesh_flags = {}, {}, {}
        self.preview.set_time_materials({})
        self.preview.set_time_textures({})
        self.preview.set_time_decals({})
        self._status_timer.stop()
        self._show_device_frames()

    def _show_device_frames(self) -> None:
        """Off while loading: each new mesh and material would make new device content. While an added SCN loads, the
        device draws on what it drew, and adds the new meshes once they are all loaded."""
        loading = bool(self._pending_renderables or self._pending_material_renderables)
        held = loading and self._adding and self._device_preview.active
        self._device_preview.hold(held)
        if not held:
            self._device_preview.set_active(bool(self.graphs) and not loading)
        if not loading:
            self._adding = False
            self._materials.stop_parses()

    def scene_sources(self) -> list[ScnSceneSource]:
        if self._sources_getter is not None:
            return list(self._sources_getter())
        if self.handler is None:
            return []
        path = str(getattr(self.handler, "filepath", "") or "")
        return [ScnSceneSource(path=path, handler=self.handler, label=Path(path).name)]

    def add_source(self, source: ScnSceneSource) -> None:
        if not self.graphs or self._stale or self._pending_renderables:
            self.request_refresh()
            return
        graphs = self.loader.build_graphs([source], max_depth=16, kept=self.graphs, campaign=self.campaign,
                                          variations=self.variations, choices=self.spawn_choices)
        if not graphs:
            return
        self.graphs.extend(graphs)
        self._load_light_probe_instances()
        self._adding = True
        self._start_time(graphs)
        self._show_time()
        self._queue_renderables(graphs)
        self._show_device_frames()
        self._update_status()
        self._update_diagnostics()
        self._notify_graphs_changed()
        self.renderables_changed.emit()
        if self._pending_renderables:
            self._mesh_timer.start(0)

    def set_hidden_renderables(self, keys: set[str]) -> None:
        self._hidden_renderables = set(keys)
        self._apply_hidden_renderables()

    def set_track_state(self, drawn: dict[ScnObjectId, bool], parts: dict[ScnObjectId, dict[int, bool]]) -> None:
        if (drawn, parts) != self._track_state:
            self._track_state = dict(drawn), {key: dict(value) for key, value in parts.items()}
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
            *graph.renderables for graph in self.graphs
        ):
            cached = self._mesh_cache.get(self._mesh_cache_key(renderable))
            draw_mesh = draw_by_key.get(renderable.key)
            if cached is None or draw_mesh is None:
                continue
            handler, bind_mesh = cached
            if handler is not None and bind_mesh is not None:
                result.append(ScnLoadedMesh(renderable, handler))
        return tuple(result)

    def _sync_preview_materials(self) -> None:
        self._materials.refresh()

    def _follow_camera(self, view, _overlays=None) -> None:
        switches = [switch for graph in self.graphs for switch in graph.camera_switches]
        ranges = [graph.camera_ranges for graph in self.graphs if graph.camera_ranges is not None]
        if isinstance(view, str) or not (switches or ranges):
            return
        eye = (view.eye, tuple(map(id, self.graphs)))
        if eye == self._camera_eye:
            return
        self._camera_eye = eye
        changed = False
        for switch in switches:
            shown = camera_shows(switch, view.eye, view.scale, view.orthographic)
            if shown is not None and self._camera_states.get(id(switch)) != shown:
                self._camera_states[id(switch)] = shown
                changed = True
        for held in () if view.orthographic else ranges:
            flags, requested = self._range_states.setdefault(
                id(held), (held.initially.copy(), np.zeros(len(held.keys), bool)))
            moved = camera_ranges_shown(held, view.eye) != flags
            if moved.any():
                flags[moved] = ~flags[moved]
                requested |= moved
                changed = True
        if not changed:
            return
        self._camera_visibility = {}
        stopped = []
        for switch in switches:
            shown = self._camera_states.get(id(switch))
            if shown is not None:
                self._camera_visibility.update(dict.fromkeys(switch.inside, shown)
                                               | dict.fromkeys(switch.far, not shown))
                # The folder it hides stops updating too (its UpdateSelf goes with its DrawSelf).
                stopped.append(switch.far_objects if shown else switch.inside_objects)
        self._camera_stopped = frozenset().union(*stopped)
        for held in ranges:
            flags, requested = self._range_states.get(id(held), (None, None))
            for index in () if flags is None else np.flatnonzero(requested & ~flags):
                self._camera_visibility.update(dict.fromkeys(held.keys[index], False))
        self._apply_hidden_renderables(parts=False)

    def _apply_hidden_renderables(self, *, refresh: bool = True, parts: bool = True) -> None:
        authored_hidden = {
            renderable.key
            for graph in self.graphs
            for renderable in graph.renderables
            if not renderable.visible_by_default
        }
        for key, visible in self._camera_visibility.items():
            if visible:
                authored_hidden.discard(key)
            else:
                authored_hidden.add(key)
        drawn, switched = self._track_state
        for graph in self.graphs if drawn else ():
            for renderable in graph.renderables:
                if (shown := _track_shown(graph, renderable, drawn)) is False:
                    authored_hidden.add(renderable.key)
                elif shown and renderable.key not in self._camera_visibility:
                    authored_hidden.discard(renderable.key)
        self.preview.set_hidden_keys(
            self._hidden_renderables
            | self._retired_renderables
            | authored_hidden,
            refresh=refresh,
        )
        if not parts:
            return
        self.preview.set_hidden_parts(
            {
                renderable.key: {
                    index
                    for index, enabled in _switched(renderable.enabled_parts,
                                                    switched.get(renderable.source_object_id, {})).items()
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
        removed = [*self.graphs[row].renderables for row in rows]
        removed_keys = {renderable.key for renderable in removed}
        removed_assets = {self._material_asset_key(renderable) for renderable in removed}
        self.graphs = [graph for index, graph in enumerate(self.graphs) if index not in rows]
        self.loader.keep(self.graphs)
        kept = {id(graph) for graph in self.graphs}
        self._scene_times = [(graph, time) for graph, time in self._scene_times if id(graph) in kept]
        times = {id(time) for _graph, time in self._scene_times}
        self._time_turns = [turns for turns in self._time_turns if id(turns.time) in times]
        self._time_targets = {process.target for _graph, time in self._scene_times
                              for process in self._script_processes(time) if process.variable}
        self._time_targets |= self._constraint_holders(self.graphs)
        self._time_keys = self._timed_keys()
        self._pose_materials = {}
        self._time_decals = {key for _graph, time in self._scene_times for key in self._timed_decals(time)}
        self._show_time()
        self._pending_renderables = deque(item for item in self._pending_renderables if item.renderable.key not in removed_keys)
        for key in [key for key, (owner, _read) in self._mesh_reads.items() if owner in removed_keys]:
            self._mesh_reads.pop(key)[1].cancel()
        for key in [key for key, (owner, _args) in self._unread_meshes.items() if owner in removed_keys]:
            del self._unread_meshes[key]
        self._plan_mesh_reads(self._pending_renderables)
        pending_materials = self._pending_material_renderables
        removed_pending = {
            asset for asset, item in pending_materials.items()
            if item.renderable.key in removed_keys
        }
        replacements = {}
        for graph in self.graphs:
            for renderable in graph.renderables:
                asset = self._material_asset_key(renderable)
                if asset in removed_pending:
                    cached = self._mesh_cache.get(self._mesh_cache_key(renderable))
                    if cached is not None and cached[0] is not None:
                        replacements.setdefault(asset, _RenderableQueueItem(graph, renderable))
        self._pending_material_renderables = OrderedDict(
            (asset, replacements.get(asset, item))
            for asset, item in pending_materials.items()
            if asset not in removed_pending or asset in replacements
        )
        self._draw_meshes = [mesh for mesh in self._draw_meshes if mesh.key not in removed_keys]
        self._hidden_renderables.difference_update(removed_keys)
        for key in removed_keys:
            self._camera_visibility.pop(key, None)
        self._retired_renderables.update(removed_keys)
        self._load_light_probe_instances()
        remaining_assets = {self._material_asset_key(renderable) for graph in self.graphs for renderable in graph.renderables}
        for asset in removed_assets - remaining_assets:
            self._materials.remove(asset)
            self._batch_cache.pop(asset, None)
        missing = sum(d.code == "missing_mesh" for graph in self.graphs for d in graph.diagnostics)
        failed = sum(d.code == "mesh_preview_error" for graph in self.graphs for d in graph.diagnostics)
        empty = sum(d.code == "mesh_without_geometry" for graph in self.graphs for d in graph.diagnostics)
        self._last_asset_counts = (len(self._draw_meshes), missing, failed, empty)
        if not self.graphs:
            self._camera_initialized = False
            self._retired_renderables.clear()
            self.status_label.setText(self.tr("No SCN is loaded."))
            self.preview.set_scene([], reset_camera=False)
        else:
            self._update_status()
            self._apply_hidden_renderables()
        self._sync_preview_materials()
        self._show_device_frames()
        self._update_diagnostics()
        self._notify_graphs_changed()
        self.renderables_changed.emit()
        if self._pending_renderables:
            self._mesh_timer.start(0)
        if self._pending_material_renderables:
            self._material_timer.start(0)

    def refresh(self) -> None:
        self._refresh_queued = False
        self._stop_timers()
        # They start while the graphs are built, before the first materials need them.
        self._materials.start_parses()
        self._clear_runtime_state(keep_hidden=True)
        self.renderables_changed.emit()

        self.status_label.setText(
            self.tr("Building source-aware SCN scene graph...")
        )
        self.graphs = self.loader.build_graphs(self.scene_sources(), max_depth=16, campaign=self.campaign,
                                               variations=self.variations, choices=self.spawn_choices)
        if not self.graphs:
            self.status_label.setText(self.tr("No SCN is loaded."))
            self.preview.set_light_probe_instances([])
            self.preview.set_scene([])
            self._sync_preview_materials()
            self._update_diagnostics()
            self._notify_graphs_changed()
            return

        self._load_light_probe_instances()
        self._start_time(self.graphs)
        self._queue_renderables(self.graphs)
        self.preview.set_scene([])
        self._show_time()
        self._sync_preview_materials()
        self._show_device_frames()
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
        self._retake_turns(result.dirty_documents)
        if result.skipped and self.graphs:
            self.graphs[0].diagnostics.extend(
                self._diagnostic_for_key("warning", "transform_edit_skipped", reason, key)
                for key, reason in result.skipped.items()
            )
        self._update_diagnostics()
        if result.dirty_documents:
            self.documents_edited.emit(result.dirty_documents, result.changed_fields)

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
        self._scope_meshes(graphs)
        queued = [_RenderableQueueItem(graph, renderable) for graph in graphs for renderable in graph.renderables]
        self._pending_renderables.extend(queued)
        self._plan_mesh_reads(queued)

    def _scope_meshes(self, graphs: list[ScnSceneGraph]) -> None:
        """Every document of a graph shares its source's meshes; a document already shown keeps its source's."""
        for graph in graphs:
            source = self.loader.source_for_graph(graph)
            scope = f"{source.project_dir}|{scene_source_file(source)}|{source.game_version}"
            for document_id in graph.documents:
                self._mesh_scopes.setdefault(document_id, scope)
            varied = {variation.key for variation in graph.variations}
            self._varied.update(item.key for item in graph.renderables if item.document_instance_id in varied
                                and (item.document_instance_id, item.source_component_id) in graph.spawned_meshes)

    def _plan_mesh_reads(self, items: Iterable[_RenderableQueueItem]) -> None:
        """Queues one read per mesh the items need, in queue order, in its source's context made here: the readers
        only read files."""
        contexts = {}
        for item in items:
            key = self._mesh_cache_key(item.renderable)
            if key in self._mesh_cache or key in self._mesh_reads or key in self._mesh_failures \
                    or key in self._unread_meshes:
                continue
            source = self.loader.source_for_graph(item.graph)
            if id(source) not in contexts:
                contexts[id(source)] = self.loader.resource_context_for_source(source), scene_source_file(source)
            context, source_file = contexts[id(source)]
            self._unread_meshes[key] = item.renderable.key, (
                context, normalize_scene_path(item.renderable.mesh_path), source_file)
        self._read_ahead()

    def _read_ahead(self) -> None:
        """Keeps at most ``MESH_READS_AHEAD`` reads started and not parsed yet."""
        while self._unread_meshes and len(self._mesh_reads) < self.MESH_READS_AHEAD:
            key, (owner, args) = self._unread_meshes.popitem(last=False)
            self._mesh_reads[key] = owner, submit_read(read_scene_resource, *args)

    def _mesh_read(self, key: str) -> Future:
        """The mesh's read, started now if a removal queued it again behind the reads ahead."""
        if key not in self._mesh_reads:
            owner, args = self._unread_meshes.pop(key)
            self._mesh_reads[key] = owner, submit_read(read_scene_resource, *args)
        read = self._mesh_reads.pop(key)[1]
        self._read_ahead()
        return read

    def _load_mesh_step(self) -> None:
        if not self.graphs:
            return
        loaded, missing, failed, empty = self._last_asset_counts
        deadline = time.perf_counter() + 0.05
        with held_folder_lookups():
            while self._pending_renderables and time.perf_counter() < deadline:
                item = self._pending_renderables.popleft()
                renderable = item.renderable
                try:
                    base_mesh = self._mesh_for_renderable(renderable, item.graph)
                    if base_mesh is None:
                        if self._mesh_cache.get(self._mesh_cache_key(renderable), (None,))[0] is None:
                            missing += 1
                        else:
                            empty += 1
                        continue
                    self._draw_meshes.append(self._instance_draw_mesh(base_mesh, renderable))
                    material_asset = self._material_asset_key(renderable)
                    pending = self._pending_material_renderables
                    if material_asset not in self._materials and material_asset not in pending:
                        pending[material_asset] = item
                        handler = self._mesh_cache.get(self._mesh_cache_key(renderable), (None,))[0]
                        self._materials.prefetch(handler, renderable.mdf_path)
                    loaded += 1
                except Exception as exc:
                    # A failed mesh's kept exception is raised again for each of its renderables; its traceback would
                    # hold this widget's frames.
                    exc.__traceback__ = None
                    failed += 1
                    item.graph.diagnostics.append(
                        self._diagnostic(
                            "warning",
                            "mesh_preview_error",
                            f"Failed to build mesh preview for {renderable.mesh_path}: {exc}",
                            renderable,
                        )
                    )
        self._last_asset_counts = (loaded, missing, failed, empty)
        self._update_status()
        self._update_diagnostics()
        if self._pending_renderables:
            self._mesh_timer.start(0)
        else:
            self._retired_renderables.clear()
            self._apply_hidden_renderables(refresh=False)
            self._variation_values = {name: self._variation_values.get(name, {}) for mesh in self._draw_meshes
                                      if mesh.key in self._varied
                                      for name in {batch.material_name for batch in mesh.batches}}
            self.preview.set_scene(self._draw_meshes, reset_camera=not self._camera_initialized)
            self._pose_started_meshes()
            self._camera_initialized = True
            self.renderables_changed.emit()
        if self._pending_material_renderables and not self._material_timer.isActive():
            self._material_timer.start(0)
        self._show_device_frames()

    def _pose_started_meshes(self) -> None:
        drawn = {mesh.key for mesh in self._draw_meshes}
        for graph in self.graphs:
            for renderable in graph.renderables:
                if renderable.start_pose is not None and renderable.key in drawn:
                    self.preview.set_mesh_pose(renderable.key, ScenePose(renderable.start_pose))
        for turns in self._time_turns:
            turns.placed, turns.placed_joints = {}, {}
        self._show_time()

    def _resolve_material_step(self) -> None:
        deadline = time.perf_counter() + 0.02
        with held_folder_lookups():
            while self._pending_material_renderables and time.perf_counter() < deadline:
                _asset, item = self._pending_material_renderables.popitem(last=False)
                try:
                    self._queue_materials(item.renderable)
                except Exception as exc:
                    item.graph.diagnostics.append(
                        self._diagnostic(
                            "warning",
                            "material_preview_error",
                            f"Failed to resolve materials for {item.renderable.mesh_path}: {exc}",
                            item.renderable,
                        )
                    )
                    self._update_diagnostics()
                    self._schedule_status_update()
        if self._pending_material_renderables:
            self._material_timer.start(0)
        self._show_device_frames()

    def _mesh_for_renderable(self, renderable: ScnRenderableMesh, graph: ScnSceneGraph) -> SceneDrawMesh | None:
        cache_key = self._mesh_cache_key(renderable)
        cached = self._mesh_cache.get(cache_key)
        if cached is not None:
            return cached[1]
        failure = self._mesh_failures.get(cache_key)
        if failure is not None:
            raise failure
        read = self._mesh_read(cache_key)
        try:
            return self._parsed_mesh(cache_key, read.result(), renderable, graph)
        except Exception as exc:
            # Raised again for the mesh's other renderables.
            self._mesh_failures[cache_key] = exc
            raise

    def _parsed_mesh(self, cache_key: str, resolved: tuple[str, bytes] | None, renderable: ScnRenderableMesh,
                     graph: ScnSceneGraph) -> SceneDrawMesh | None:
        source = self.loader.source_for_graph(graph)
        if resolved is None:
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

        # Game lighting draws the whole mesh; the textures view needs only the light read.
        game = self.preview.lighting_mode == "game"
        path, data = resolved
        handler_class = StmeshHandler if StmeshHandler.can_handle(data) else MeshHandler
        mesh_handler = handler_class.from_bytes(
            path,
            data,
            app=getattr(getattr(source, "handler", None) or self.handler, "app", None),
            resource_context=self.loader.resource_context_for_source(source),
            game_version=(
                getattr(source, "game_version", "")
                or getattr(self.handler, "game_version", "")
            ),
            load_mode=MeshLoadMode.SCENE if game else MeshLoadMode.LIGHT,
        )
        if not game:
            mesh_handler.reread = partial(self._resource_bytes, source, renderable.mesh_path)
        mesh = getattr(mesh_handler, "mesh", None)
        base_mesh = None
        if mesh is not None:
            # Stored normals, as a light read (which holds no normal-recalculation data) gives in either lighting.
            scene_meshes = build_mesh_scene(
                mesh,
                key="scn_mesh",
                include_vertex_colors=False,
                # No scene view reads tangents.
                include_tangents=False,
                normal_recalculation_enabled=False,
                normal_recalc_edge_exclusion=(
                    renderable.normal_recalc_edge_exclusion
                ),
                join_meshlet_runs=True,
            )
            base_mesh = scene_meshes[0] if scene_meshes else None
            # The scene draws this mesh, the GPU the meshlet blob: the payload's planes are decoded again if asked for.
            mesh.release_payload_planes()
        if base_mesh is None:
            # RE4's ``*_occ.mesh`` hold an occluder mesh alone, which the game culls with and does not draw.
            occluder = bool(getattr(getattr(mesh, "header", None), "occluder_mesh_offset", 0))
            graph.diagnostics.append(
                self._diagnostic(
                    "info",
                    "mesh_without_geometry",
                    f"Mesh resource holds {'an occluder mesh alone' if occluder else 'nothing'} to draw: "
                    f"{renderable.mesh_path}",
                    renderable,
                )
            )
        self._mesh_cache[cache_key] = (mesh_handler, base_mesh)
        return base_mesh

    def _resource_bytes(self, source, path: str) -> bytes | None:
        resolved = self.loader.resolve_resource_for_source(source, path)
        return None if resolved is None else resolved.data

    def _instance_draw_mesh(self, base_mesh: SceneDrawMesh, renderable: ScnRenderableMesh) -> SceneDrawMesh:
        key = renderable.key
        material_asset_key = self._material_asset_key(renderable)
        material_name, batches = self._batch_cache.get(material_asset_key, (None, None))
        if batches is None:
            material_name = self._material_key(material_asset_key, base_mesh.material_name)
            drawn = None if renderable.drawn_group is None else group_submesh_numbers(
                self._mesh_cache[self._mesh_cache_key(renderable)][0].mesh, renderable.drawn_group)
            batches = [
                SceneDrawBatch(
                    indices=batch.indices,
                    material_name=self._material_key(
                        material_asset_key,
                        batch.material_name,
                    ),
                    part_index=batch.part_index,
                    alpha_test=batch.alpha_test,
                    transparent=batch.transparent,
                    submesh=batch.submesh,
                    submeshes=batch.submeshes,
                )
                for batch in base_mesh.batches
                if drawn is None or batch.submesh is not None
                and batch.submesh[1] in drawn and batch.submesh[1] + batch.submeshes - 1 in drawn
            ]
            self._batch_cache[material_asset_key] = (material_name, batches)
        return replace(
            base_mesh,
            key=key,
            material_name=material_name,
            batches=batches,
            transform_matrix=renderable.world_matrix,
            decal_receive=renderable.decal_receive,
            transparent_bias=renderable.transparent_bias,
            speedtree_placement=renderable.speedtree_placement,
            blend_normal=renderable.blend_shape_normal,
            chain_flags=(renderable.normal_recalculation_enabled, renderable.blend_shape_real_time_lod),
            draw_state=renderable.draw_state,
            lod_parent_mesh=renderable.lod_parent_mesh,
        )

    @staticmethod
    def _material_key(asset_key: str, material_name: str) -> str:
        return scoped_material_key(asset_key, material_name)

    def _material_asset_key(self, renderable: ScnRenderableMesh) -> str:
        mesh = normalize_scene_path(renderable.mesh_path).lower()
        mdf = normalize_scene_path(self._mesh_mdfs.get(renderable.key, renderable.mdf_path)).lower()
        drawn = "" if renderable.mdf_material is None and renderable.drawn_group is None else \
            f"|material={renderable.mdf_material}|group={renderable.drawn_group}"
        owner = renderable.source_object_id
        timed = f"|time={owner.document_id}:{owner.local_object_id}" \
            if self._time_targets and owner in self._time_targets else ""
        varied = f"|variation={renderable.document_instance_id}" if renderable.key in self._varied else ""
        forced = f"|flags={','.join(sorted(flags))}" if (flags := self._mesh_flags.get(renderable.key)) else ""
        return f"{self._asset_scope(renderable)}|{mesh}|{mdf}{drawn}{timed}{varied}{forced}"

    def set_pose_materials(self, values: dict[ScnObjectId, dict[str, dict[str, float | tuple[float, ...] | str]]]
                           ) -> None:
        keyed: dict[str, dict] = {key: {} for key in self._pose_materials}
        textures: dict[str, dict[str, str]] = {}
        for graph in self.graphs:
            for renderable in graph.renderables:
                for material, variables in values.get(renderable.source_object_id, {}).items():
                    key = self._material_key(self._material_asset_key(renderable), material)
                    for name, value in variables.items():
                        (textures.setdefault(key, {}) if isinstance(value, str) else keyed.setdefault(key, {}))[
                            name] = value
        self.preview.set_time_textures(textures)
        if keyed != self._pose_materials:
            self._pose_materials = keyed
            self._show_time()

    def set_mesh_materials(self, paths: dict[ScnObjectId, str], flags: dict[ScnObjectId, frozenset] | None = None
                           ) -> None:
        owners = {renderable.key: renderable.source_object_id for graph in self.graphs
                  for renderable in graph.renderables if renderable.source_component_id == graph.documents[
                      renderable.source_object_id.document_id].objects[renderable.source_object_id].mesh_component}
        swapped = {key: paths[owner] for key, owner in owners.items() if owner in paths}
        forced = {key: frozenset(flags[owner]) for key, owner in owners.items() if (flags or {}).get(owner)}
        changed = {key for key in {*swapped, *self._mesh_mdfs, *forced, *self._mesh_flags}
                   if (swapped.get(key), forced.get(key)) != (self._mesh_mdfs.get(key), self._mesh_flags.get(key))}
        if not changed:
            return
        self._mesh_mdfs, self._mesh_flags = swapped, forced
        meshes = {mesh.key: mesh for mesh in self._draw_meshes}
        for graph in self.graphs:
            for renderable in graph.renderables:
                if renderable.key not in changed or (mesh := meshes.get(renderable.key)) is None or (
                        base := self._mesh_cache.get(self._mesh_cache_key(renderable), (None, None))[1]) is None:
                    continue
                self._queue_materials(renderable, parse_in_subprocess=False)
                drawn = self._instance_draw_mesh(base, renderable)
                mesh.material_name, mesh.batches = drawn.material_name, drawn.batches
        self.preview.meshes_rebound()

    def material_surfaces(self, key: str) -> list:
        mesh = next((mesh for mesh in self._draw_meshes if mesh.key == key), None)
        return [] if mesh is None else [surface for _name, surface, _rules in self._mesh_profiles(mesh)]

    def _camera_position(self) -> tuple[float, float, float]:
        view = np.asarray(self.preview.view_camera()[0].view, np.float64).reshape(3, 4)
        return tuple(float(value) for value in -view[:, :3].T @ view[:, 3])

    def _asset_scope(self, renderable: ScnRenderableMesh) -> str:
        """A project document's project, or the source of a document in the game's files: its documents share its
        materials, as they share its meshes."""
        document_id = renderable.source_object_id.document_id
        return document_id.split("|", 1)[0] if "|" in document_id else self._mesh_scopes[document_id]

    def _mesh_cache_key(self, renderable: ScnRenderableMesh) -> str:
        """One per mesh file of a source, whichever of its documents places it."""
        return (
            f"{self._mesh_scopes[renderable.source_object_id.document_id]}|"
            f"{normalize_scene_path(renderable.mesh_path).lower()}|"
            f"nr={int(renderable.normal_recalculation_enabled)}|"
            f"nr-edge={int(renderable.normal_recalc_edge_exclusion)}"
        )

    def _queue_materials(self, renderable: ScnRenderableMesh, *, parse_in_subprocess: bool = True) -> None:
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
            explicit_mdf_path=self._mesh_mdfs.get(renderable.key, renderable.mdf_path),
            forced_flags=self._mesh_flags.get(renderable.key, frozenset()),
            mdf_material=renderable.mdf_material,
            material_scope=material_asset_key,
            resource_scope=self._asset_scope(renderable),
            texture_quality=self._materials.texture_quality,
            parse_in_subprocess=parse_in_subprocess,
            parent=self._materials,
        )
        self._materials.add(material_asset_key, session)

    def _schedule_status_update(self) -> None:
        if self.graphs and not self._status_timer.isActive():
            self._status_timer.start(50)

    def _update_status(self) -> None:
        self._status_timer.stop()
        if not self.graphs:
            return
        loaded, missing, failed, empty = self._last_asset_counts
        progress = loaded + missing + failed + empty
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
        self.status_label.setText(
            " | ".join(
                [
                    *([self.tr("Scene preview is stale. Reload the scene to rebuild from the edited SCN.")]
                      if self._stale else []),
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
                    if self._pending_renderables
                    else self.tr("Preview ready"),
                    self.tr("Meshes loaded: {count}").format(count=loaded),
                    self.tr("Missing: {count}").format(count=missing),
                    self.tr("Failed: {count}").format(count=failed + len(self._device_preview.refused())),
                    light_probe_status,
                    self.tr("Diagnostics: {count}").format(count=diagnostics),
                    *self._device_preview.status(),
                ]
            )
        )

    def _update_diagnostics(self) -> None:
        new = []
        for diagnostic in (*graph.diagnostics for graph in self.graphs):
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
