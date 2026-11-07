"""See docs/internals/startup-draw-states.md, Time after the start."""
from __future__ import annotations

from dataclasses import dataclass, field, replace

lazy import numpy as np

lazy from ui.scene.scene_view import ScenePose

lazy from .scene_startup import simple_body, skin_bones
lazy from .scene_time import SceneTime, SimpleAnimation, SimplePlay, TimelinePlay, Xorshift128
lazy from .scn_scene_graph import ScnObjectId, ScnRenderableMesh, ScnSceneGraph, parent_of, placed_matrix, turned_worlds


@dataclass(slots=True)
class _TimeTurns:
    time: SceneTime
    document: object
    targets: set[ScnObjectId]
    objects: list
    renderables: list[tuple[ScnRenderableMesh, object]]
    placed: dict = field(default_factory=dict)
    posed: set[ScnObjectId] = field(default_factory=set)
    placed_joints: dict = field(default_factory=dict)


class ScnSceneTimePreview:
    def _start_time(self, graphs: list[ScnSceneGraph]) -> None:
        self._scope_meshes(graphs)
        self._time_targets |= self._constraint_holders(graphs)
        for graph in graphs:
            animations = [animation for document in graph.documents.values()
                          for animation in document.startup_time_animations]
            if not animations and not graph.timelines:
                continue
            time = SceneTime([*animations, *graph.timelines], Xorshift128.from_clock(graph.random))
            self._scene_times.append((graph, time))
            self._time_decals.update(self._timed_decals(time))
            turned: dict[str, set[ScnObjectId]] = {}
            posed: dict[str, set[ScnObjectId]] = {}
            for animation in animations:
                if isinstance(animation, SimpleAnimation):
                    target = animation.body.target
                    if animation.turned:
                        turned.setdefault(target.document_id, set()).add(target)
                    if animation.posed:
                        posed.setdefault(target.document_id, set()).add(target)
                    continue
                for process in animation.processes:
                    if process.variable:
                        self._time_targets.add(process.target)
                    elif process.axis is not None:
                        turned.setdefault(process.target.document_id, set()).add(process.target)
            for document_id in {*turned, *posed}:
                turns = self._turns(graph, time, graph.documents[document_id], turned.get(document_id, set()),
                                    posed.get(document_id, set()))
                if turns.renderables:
                    self._time_turns.append(turns)
        self._time_keys = self._timed_keys()

    def _constraint_holders(self, graphs: list[ScnSceneGraph]) -> set[ScnObjectId]:
        return {object_id for graph in graphs for document in graph.documents.values()
                for object_id in document.constraints} if self.constraint_materials else set()

    @staticmethod
    def _script_processes(time: SceneTime):
        return (process for play in time.plays if not isinstance(play, (TimelinePlay, SimplePlay))
                for process in play.animation.processes)

    @staticmethod
    def _timed_decals(time: SceneTime) -> set[str]:
        return {track.target for play in time.plays if isinstance(play, TimelinePlay)
                for track in play.animation.tracks}

    def _turns(self, graph: ScnSceneGraph, time: SceneTime, document, targets: set[ScnObjectId],
               posed: set[ScnObjectId]) -> _TimeTurns:
        children: dict[ScnObjectId, list] = {}
        for item in document.objects.values():
            if (parent := parent_of(document, item)) is not None:
                children.setdefault(parent.id, []).append(item)
        objects, stack, seen = [], [document.objects[target] for target in {*targets, *posed}], set()
        while stack:
            item = stack.pop()
            if item.id not in seen:
                seen.add(item.id)
                objects.append(item)
                stack.extend(children.get(item.id, ()))
        renderables = []
        for renderable in graph.renderables:
            if renderable.source_object_id not in seen:
                continue
            if np.array_equal(renderable.document_world_matrix, document.objects[renderable.source_object_id]
                              .document_world_matrix):
                renderables.append((renderable, graph.document_instances[renderable.document_instance_id]))
            else:
                graph.diagnostics.append(self._diagnostic(
                    "warning", "time_rotation_not_drawn", "A script turns this object over time; REasy draws this "
                    "instance where the scene starts it.", renderable))
        return _TimeTurns(time, document, targets, objects, renderables, posed=posed)

    def _timed_keys(self) -> dict[tuple[ScnObjectId, str], set[str]]:
        materials: dict[ScnObjectId, set[str]] = {}
        for _graph, time in self._scene_times:
            for process in self._script_processes(time):
                if process.variable:
                    materials.setdefault(process.target, set()).add(process.material)
        keys: dict[tuple[ScnObjectId, str], set[str]] = {}
        for graph, _time in self._scene_times:
            for renderable in graph.renderables:
                for material in materials.get(renderable.source_object_id, ()):
                    keys.setdefault((renderable.source_object_id, material), set()).add(
                        self._material_key(self._material_asset_key(renderable), material))
        return keys

    def _retake_turns(self, documents: set[str]) -> None:
        for turns in self._time_turns:
            if turns.document.document_id not in documents:
                continue
            for play in turns.time.plays:
                if isinstance(play, SimplePlay):
                    self._restart_simple(turns, play)
                    continue
                if isinstance(play, TimelinePlay):
                    continue
                processes = play.animation.processes
                retaken = tuple(process if process.offset is None or process.target not in turns.targets else replace(
                    process, offset=tuple(float(value) for value in turns.document.objects[process.target].transform
                                          .rotation)) for process in processes)
                if retaken != processes:
                    play.animation = replace(play.animation, processes=retaken)
                    turns.placed = {}

    @staticmethod
    def _restart_simple(turns: _TimeTurns, play: SimplePlay) -> None:
        body = play.animation.body
        if body.target not in turns.targets and body.target not in turns.posed:
            return
        document = turns.document
        joints = document.joints.get(body.target) if play.animation.posed else None
        moved, position = simple_body(document, document.objects[body.target], joints)
        play.restart(replace(play.animation, body=moved, position=position))
        frame = turns.time.frame
        turns.time.frame = replace(frame, rotations={key: value for key, value in frame.rotations.items()
                                                     if key != body.target},
                                   joints={key: value for key, value in frame.joints.items() if key[0] != body.target})
        turns.placed, turns.placed_joints = {}, {}

    def _show_time(self) -> None:
        values = {key: {} for keys in self._time_keys.values() for key in keys} | {
            key: dict(changed) for key, changed in self._variation_values.items()}
        for key, variables in self._pose_materials.items():
            values.setdefault(key, {}).update(variables)
        decals = {key: {} for key in self._time_decals}
        for _graph, time in self._scene_times:
            for (target, material, variable), value in time.frame.materials.items():
                for key in self._time_keys.get((target, material), ()):
                    values[key][variable] = value
            for (target, field_name), value in time.frame.fields.items():
                decals[target][field_name] = value
        self.preview.set_time_materials(values)
        self.preview.set_time_decals(decals)
        moved, poses = {}, {}
        for turns in self._time_turns:
            frame = turns.time.frame
            rotations = {target: rotation for target, rotation in frame.rotations.items() if target in turns.targets}
            joints = {key: rotation for key, rotation in frame.joints.items() if key[0] in turns.posed}
            if rotations == turns.placed and joints == turns.placed_joints:
                continue
            turns.placed, turns.placed_joints = rotations, joints
            document = turns.document
            if joints:
                posed = dict(document.joints)
                for (target, index), rotation in joints.items():
                    posed[target] = posed[target].posed(index, rotation=rotation)
                document = replace(document, joints=posed)
            worlds = turned_worlds(document, rotations, turns.objects)
            for renderable, instance in turns.renderables:
                item = document.objects[renderable.source_object_id]
                world = worlds[item.id][0]
                moved[renderable.key] = world if instance.parent_instance_id is None else placed_matrix(
                    instance.base_world_matrix, world)
                if item.id in turns.posed and renderable.source_component_id == item.mesh_component and (
                        bones := skin_bones(document, item)) is not None:
                    poses[renderable.key] = ScenePose(bones)
        if moved:
            self.preview.update_mesh_transforms(moved, recompute_bounds=False)
        for key, pose in poses.items():
            self.preview.set_mesh_pose(key, pose)

    def _step_time(self, seconds: float) -> None:
        # Scripts see the camera where it drew the frame before: in game it moves after them.
        camera = self._camera_position()
        seen, self._time_camera = self._time_camera or camera, camera
        for _graph, time in self._scene_times:
            time.step(seconds, seen, self._camera_stopped)
        self._show_time()

    def _time_running(self) -> bool:
        return any(time.running for _graph, time in self._scene_times)
