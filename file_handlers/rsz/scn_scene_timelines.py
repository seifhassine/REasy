"""See docs/internals/startup-draw-states.md, Time after the start."""
from __future__ import annotations

lazy from file_handlers.clip.decode import read_timeline

lazy from .scene_time import TimelineAnimation, TimelineTrack
lazy from .scn_scene_graph import ScnObjectId, ScnSceneGraph, ScnTimeline, normalize_scene_path


class ScnSceneTimelines:
    def _clip(self, path: str, document_id: str):
        key = normalize_scene_path(path)
        if key not in self._clip_cache:
            self._clip_cache[key] = None
            resolved = self._resolved(path, document_id)
            if resolved is not None and resolved.data is not None:
                try:
                    self._clip_cache[key] = read_timeline(resolved.data)
                except Exception as exc:
                    self._unread(path, document_id, exc)
                    return None
        return self._clip_cache[key]

    def _bind_timelines(self, graph: ScnSceneGraph) -> None:
        timed = [instance for instance in graph.document_instances.values()
                 if graph.documents[instance.document_id].startup_timelines
                 or graph.documents[instance.document_id].startup_timelines_refused]
        if not timed:
            return
        decals = {(decal.document_instance_id, decal.source_component_id): decal for decal in graph.volume_decals}
        holders = self._guid_holders(graph)
        named: dict[str, list[tuple[str, ScnObjectId]]] = {}
        for instance in graph.document_instances.values():
            document = graph.documents[instance.document_id]
            for go in document.rsz_file.gameobjects:
                object_id = document.object_by_local_id.get(go.id)
                if object_id is not None:
                    named.setdefault(document.objects[object_id].name, []).append((instance.instance_id, object_id))
        tracks: dict[tuple[str, str], list[tuple[str, ScnTimeline, TimelineTrack]]] = {}
        for instance in timed:
            document = graph.documents[instance.document_id]
            for object_id, reason in document.startup_timelines_refused:
                self._warn(graph, "timeline_not_played", f"REasy does not play this timeline: {reason}.",
                           document.document_id, instance.instance_id, object_id)
            for timeline in document.startup_timelines:
                bound = []
                for node in timeline.nodes:
                    target = self._timeline_object(graph, holders.get(node.guid, []) if any(node.guid) else [],
                                                   named.get(node.name, []))
                    if target is None:
                        self._warn(graph, "timeline_not_played", "REasy does not play this timeline: which object "
                                   f"named {node.name} it binds is not known.", document.document_id,
                                   instance.instance_id, timeline.owner)
                        bound = []
                        break
                    if target is False:
                        continue
                    target_instance, target_id = target
                    held = graph.documents[target_id.document_id]
                    for type_name, field_name, components in node.tracks:
                        component_id = next((other for other in held.objects[target_id].components
                                             if held.components[other].type_name == type_name), None)
                        decal = decals.get((target_instance, component_id))
                        if decal is not None:
                            bound.append(TimelineTrack(decal.key, field_name, tuple(
                                float(value) for value in decal.fields[field_name]), components))
                for track in bound:
                    tracks.setdefault((track.target, track.field), []).append((instance.instance_id, timeline, track))
        plays: dict[ScnTimeline, list[TimelineTrack]] = {}
        for setting in tracks.values():
            # Like ones set the same values every frame; of unlike ones the last to update wins, which is not known.
            if len({(timeline.clip, timeline.start, timeline.end, timeline.speed, timeline.event, timeline.scale,
                     track) for _instance, timeline, track in setting}) > 1:
                for instance_id, timeline, track in setting:
                    self._warn(graph, "timeline_not_played", f"REasy does not play this timeline: other timelines "
                               f"set {track.field} on the same component, and which sets it last is not known.",
                               timeline.owner.document_id, instance_id, timeline.owner)
                continue
            _instance, timeline, track = setting[0]
            plays.setdefault(timeline, []).append(track)
        graph.timelines.extend(TimelineAnimation(timeline.owner, timeline.start, timeline.end, timeline.speed,
                                                 timeline.event, timeline.fps, timeline.scale, tuple(played),
                                                 timeline.first_back, timeline.zero_back)
                               for timeline, played in plays.items())

    @staticmethod
    def _timeline_object(graph: ScnSceneGraph, by_guid: list[tuple[str, ScnObjectId]],
                         by_name: list[tuple[str, ScnObjectId]]) -> tuple[str, ScnObjectId] | bool | None:
        if by_guid:
            return by_guid[0] if len(by_guid) == 1 else None
        if len(by_name) < 2:
            return by_name[0] if by_name else False
        if len({instance for instance, _object_id in by_name}) > 1:
            return None
        document = graph.documents[by_name[0][1].document_id]
        for _instance, object_id in by_name:
            parent = document.object_by_local_id.get(document.objects[object_id].parent_id)
            if parent is not None and document.objects[parent].kind != "folder":
                return None
        order = {object_id: index for index, object_id in enumerate(document.objects)}
        return max(by_name, key=lambda found: order[found[1]])
