"""See docs/internals/gui-rendering.md, World views, Reading the game's files."""

from __future__ import annotations

import json
import math
import os
import re
import struct
import weakref
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from functools import cache, cached_property
from hashlib import blake2b
from itertools import batched
from pathlib import Path
from typing import Any

import numpy as np

lazy from app_config import CURRENT_VERSION
lazy from file_handlers.clip.decode import read_timeline
lazy from file_handlers.clip.errors import ClipFormatError
lazy from file_handlers.clip.play import play_node
lazy from file_handlers.clip.rules import game_player
lazy from file_handlers.engine_math import camera_view, make_trs_matrix, quaternion_product
lazy from file_handlers.rsz.game_registry import default_rsz_json_path, game_type_registry
lazy from file_handlers.rsz.plain_values import plain_value
lazy from file_handlers.rsz.rsz_file import RszFile
lazy from file_handlers.rsz.scn_scene_graph import (ScnSceneGraphBuilder, ScnTransform, compose_parented_transform,
                                                    parent_of)
lazy from utils.app_paths import cache_directory, resource_path
lazy from utils.enum_manager import enum_file_path, game_enum_definitions
lazy from utils.hash_util import murmur3_hash_ascii
lazy from utils.resource_file_utils import submit_read

lazy from .codec import parse_gui
lazy from .errors import GuiFormatError, GuiSceneError
lazy from .rules import DATA_DIRECTORY
lazy from .scene import GuiWorkspace, normalize_gui_resource_path

_PATH_END = re.compile(rb"\.\x00(?:[gG]\x00[uU]\x00[iI]|[sS]\x00[cC]\x00[nN]|[pP]\x00[fF]\x00[bB])\x00")
_PATH_BYTES = frozenset(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-/\x5c.@")
_ZERO = (0.0, 0.0, 0.0)
_SCHEMA = 2
_SEEN: dict[tuple, tuple] = {}
_READ: dict[tuple, tuple] = {}
_VIEWS: dict[tuple, tuple] = {}
_CLIPS: dict[tuple, tuple] = {}
_STORES: dict[str, dict[str, Any]] = {}
_UNSAVED: set[str] = set()


def _store_path(game: str) -> Path:
    return cache_directory() / "gui_world" / f"{game.casefold()}.json"


@cache
def _signature(game: str) -> str:
    code = Path(__file__).parents[1]
    watched = [*code.glob("*.py"), *code.glob("gui/**/*.py"), *code.glob("rsz/**/*.py"), default_rsz_json_path(game),
               enum_file_path(game), *resource_path(DATA_DIRECTORY).iterdir()]
    return f"{_SCHEMA} {CURRENT_VERSION} {max(os.stat(path).st_mtime_ns for path in watched)}"


@dataclass(frozen=True, slots=True)
class Lens:
    view: tuple[float, ...]
    fov: float
    near: float
    far: float


@dataclass(frozen=True, slots=True, eq=False)
class Held:
    kind: str
    fields: Mapping[str, Any]
    names: tuple[str, ...]
    world: np.ndarray
    rotation: tuple[float, float, float, float]
    chain: tuple
    at: tuple[int, ...]
    draws: tuple[bool, ...]
    scale: float

    @property
    def name(self) -> str:
        return self.names[-1]

    @property
    def draw(self) -> bool:
        return all(self.draws)


def resource_key(path: str) -> str:
    return path.replace("\\", "/").lstrip("@/").casefold()


def _stamp(location) -> tuple:
    if location.pak is not None:
        return location.path, weakref.ref(location.pak)
    held = os.stat(location.path)
    return location.path, held.st_mtime_ns, held.st_size


def _mentioned(data: bytes) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    guis, scenes, prefabs = set(), set(), set()
    for hit in _PATH_END.finditer(data):
        start = hit.start()
        while start > 1 and not data[start - 1] and data[start - 2] in _PATH_BYTES:
            start -= 2
        text = data[start:hit.end()].decode("utf-16-le")
        if text[-1] in "iI":
            guis.add(normalize_gui_resource_path(text))
        else:
            (scenes if text[-1] in "nN" else prefabs).add(resource_key(text))
    return frozenset(guis), frozenset(scenes), frozenset(prefabs)


def _parsed(game: str, source: str, data: bytes, fields: Mapping[str, tuple[str, ...]], scale: str) -> list[list]:
    rsz = RszFile()
    rsz.filepath, rsz.type_registry, rsz.game_version = str(source), game_type_registry(game), game
    rsz.read(bytes(data))
    graph, rows = ScnSceneGraphBuilder(rsz.type_registry, game_version=game).build(rsz, root_path=str(source)), []
    for document in graph.documents.values():
        for item in document.objects.values():
            for component in (graph.components[at] for at in item.components):
                if component.type_name not in fields:
                    continue
                chain = [item]
                while (above := parent_of(document, chain[-1])) is not None:
                    chain.append(above)
                rows.append([component.type_name, {
                    name: plain_value(component.fields[name], rsz.parsed_elements.__getitem__)
                    for name in fields[component.type_name]}, [node.name for node in reversed(chain)],
                    np.asarray(item.world_matrix, np.float32).ravel().tolist(), list(item.document_world_rotation),
                    [node.transform and [node.transform.position, node.transform.rotation, node.transform.scale,
                                         np.asarray(node.transform.local_matrix, np.float32).ravel().tolist(),
                                         node.transform.absolute_scaling] for node in reversed(chain)],
                    [node.instance_id for node in reversed(chain)], [node.draw_self for node in reversed(chain)],
                    plain_value(item.fields[scale], rsz.parsed_elements.__getitem__)])
    return rows


def _object_ids(game: str, source: str, data: bytes) -> dict[str, int]:
    rsz = RszFile()
    rsz.filepath, rsz.type_registry, rsz.game_version = str(source), game_type_registry(game), game
    rsz.read(bytes(data), skip_data=True)
    return {item.guid.hex(): rsz.object_table[item.id] for item in rsz.gameobjects}


def _held(row: list) -> Held:
    kind, fields, names, world, rotation, chain, at, draws, scale = row
    return Held(kind, fields, tuple(names), np.float32(world).reshape(4, 4), tuple(rotation), tuple(
        node and ScnTransform(tuple(node[0]), tuple(node[1]), tuple(node[2]), np.float32(node[3]).reshape(4, 4),
                              absolute_scaling=node[4]) for node in chain), tuple(at), tuple(draws), scale)


def _composed(chain, position) -> tuple[np.ndarray, tuple]:
    root, *below = chain
    world, rotation = make_trs_matrix(position, root.rotation, root.scale), root.rotation
    for transform in below:
        world = compose_parented_transform(world, transform, rotation)
        rotation = quaternion_product(transform.rotation, rotation)
    return world, rotation


def _placed(held: Held, position) -> tuple[np.ndarray, tuple]:
    return _composed(held.chain, position)


def _lens(world: np.ndarray, fields: Mapping[str, Any], rule: Mapping[str, str]) -> Lens:
    return Lens(tuple(float(v) for v in camera_view(np.asarray(world, np.float32).T).ravel()),
                fields[rule["fov"]], fields[rule["near"]], fields[rule["far"]])


@dataclass(frozen=True, slots=True, eq=False)
class Played:
    camera: Held
    rest: tuple[float, float, float]
    rig: Mapping[int, tuple]
    scene: Mapping[int, tuple]
    rules: Mapping[str, Any]
    player: Mapping[str, Any]
    mode: int
    start: float
    end: float
    rate: float
    clip: str

    def _set(self, tracks, part: str, frame: float) -> dict[tuple, Any]:
        back = self.start - self.rules["timeline"]["first_back"]
        return {path: value for name, node in tracks if name == part
                for path, value in play_node(node, back, min(frame, self.end), {}, self.player, mode=self.mode)}

    def lens(self, frame: float) -> Lens:
        timeline, chain, root = self.rules["timeline"], list(self.camera.chain), self.rest
        for place, tracks in self.rig.items():
            held = chain[place]
            position, rotation = list(held.position if place else self.rest), held.rotation
            for path, value in self._set(tracks, timeline["transform"], frame).items():
                if path[0] == timeline["rotation"]:
                    rotation = tuple(value)
                elif path == (timeline["position"],):
                    position = list(value)
                elif path[0] == timeline["position"]:
                    position["xyz".index(path[1])] = value
            chain[place] = replace(held, position=tuple(position), rotation=rotation,
                                   local_matrix=make_trs_matrix(position, rotation, held.scale))
            root = root if place else tuple(position)
        return _lens(_composed(chain, root)[0], self.camera.fields, self.rules["camera"])

    def drawn(self, held: Held | None, frame: float) -> bool:
        name = (self.rules["object"]["draw"],)
        return held is None or all(self._set(self.scene.get(at, ()), "", frame).get(name, draw)
                                   for at, draw in zip(held.at, held.draws))

    def light(self, held: Held, frame: float) -> dict[str, Any]:
        rule = self.rules["light"]
        return {"Draw": self.drawn(held, frame), **{path[0]: value for path, value in self._set(
            self.scene.get(held.at[-1], ()), rule["class"].rsplit(".", 1)[-1], frame).items()
            if len(path) == 1 and path[0] in rule["fields"]}}

    def shown(self, held: Held | None, world: np.ndarray, output: tuple[float, float]) -> float:
        rule, runs, start = self.rules["camera"], [], None
        slope = math.tan(math.radians(self.camera.fields[rule["fov"]]) / 2)
        for frame in range(int(self.start), int(self.end) + 2):
            x, y, z, _w = np.append(world[3, :3], 1.0) @ np.array(self.lens(frame).view).reshape(4, 4)
            seen = frame <= self.end and self.drawn(held, frame) and -z > self.camera.fields[rule["near"]] and (
                abs(x) <= -z * slope and abs(y) <= -z * slope * output[1] / output[0])
            if seen and start is None:
                start = frame
            elif not seen and start is not None:
                runs, start = [*runs, (frame - start, start)], None
        length, start = max(runs, default=(1, int(self.start)))
        return float(start + (length - 1) // 2)


class GuiWorld:
    def __init__(self, locate: Callable[[str], Any], profile, errors: list[str]) -> None:
        self._locate, self._profile, self._rules, self.errors = locate, profile, profile.world, errors
        gui, light, lens, holder, supplier, timeline = (self._rules[name] for name in (
            "gui", "light", "camera", "holders", "supplier", "timeline"))
        self._fields = {
            timeline["class"]: tuple(timeline[name] for name in ("resource", "start", "end", "speed", "bind")),
            gui["class"]: (gui["asset"], gui["segment"]), light["class"]: tuple(light["fields"]),
            lens["class"]: (lens["enabled"], lens["fov"], lens["near"], lens["far"]),
            holder["class"]: (holder["label"], holder["entries"]), supplier["class"]: tuple(supplier["instanced"]),
            self._rules["commands"]["set"]: (self._rules["commands"]["list"],),
            **{self._rules[name]["class"]: (self._rules[name]["field"],) for name in (
                "messages", "camera_list", "camera_rest", "commands")}}
        self._at: dict[str, tuple | None] = {}
        self._got: dict[str, tuple] = {}
        self._views: dict[str, tuple] = {}
        self._clips: dict[str, Any] = {}
        self._moves: dict[str, Played | None] = {}
        self._sets: dict[str, tuple] = {}
        self._lit_by: dict[str, str] = {}
        self.asked: dict[str, tuple[str, ...]] = {}
        self._placements: dict[tuple, tuple] = {}
        self._places: dict[tuple, tuple] = {}
        self._shown: dict[str, tuple[str, ...]] = {}
        self._closures: dict[str, dict[str, tuple]] = {}

    @cached_property
    def _needles(self) -> dict[str, bytes]:
        return {name: struct.pack("<I", murmur3_hash_ascii(name)) for name in self._fields}

    def _file(self, path: str) -> tuple | None:
        if path not in self._at:
            located = self._locate(path)
            self._at[path] = None if located is None else (located, (self._profile.game, *_stamp(located)))
        return self._at[path]

    def _fact(self, key: str, make: Callable[[], Any]):
        game = self._profile.game
        if game not in _STORES:
            try:
                held = json.loads(_store_path(game).read_bytes())
                _STORES[game] = held["facts"] if held["signature"] == _signature(game) else {}
            except (OSError, ValueError, KeyError, TypeError):
                _STORES[game] = {}
        if key not in _STORES[game]:
            _STORES[game][key] = json.loads(json.dumps(make(), default=float))
            _UNSAVED.add(game)
        return _STORES[game][key]

    def _keep(self) -> None:
        game = self._profile.game
        if game in _UNSAVED:
            _UNSAVED.discard(game)
            path = _store_path(game)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                scratch = path.with_name(f"{path.name}.{os.getpid()}")
                scratch.write_text(json.dumps({"signature": _signature(game), "facts": _STORES[game]},
                                              separators=(",", ":")), encoding="utf-8")
                os.replace(scratch, path)
            except OSError as exc:
                self.errors.append(f"{path}: {exc}")

    def _look(self, paths) -> None:
        new = {file[1]: file[0] for path in paths if (file := self._file(path)) and file[1] not in _SEEN}
        for chunk in batched(new.items(), 64):
            for (stamp, _location), reading in zip(chunk, [submit_read(location.read) for _stamp, location in chunk]):
                data = (reading.result() or ("", b""))[1]
                guis, scenes, prefabs = _mentioned(data)
                _SEEN[stamp] = (frozenset(name for name, needle in self._needles.items() if needle in data), guis,
                                scenes, blake2b(data, digest_size=16).hexdigest(), prefabs)

    def _seen(self, path: str) -> tuple[frozenset[str], frozenset[str], frozenset[str], str, frozenset[str]] | None:
        if (file := self._file(path)) is None:
            return None
        if file[1] not in _SEEN:
            self._look([path])
        return _SEEN[file[1]]

    def _read(self, path: str) -> tuple[Held, ...]:
        if path not in self._got:
            location, stamp = self._file(path)
            if stamp not in _READ:
                try:
                    _READ[stamp] = tuple(map(_held, self._fact(self._seen(path)[3], lambda: _parsed(
                        self._profile.game, *location.read(), self._fields, self._rules["object"]["time_scale"])))), ()
                except Exception as exc:
                    _READ[stamp] = (), (f"{location.path}: {exc}",)
            self._got[path], errors = _READ[stamp]
            self.errors += errors
        return self._got[path]

    def _holding(self, kind: str, paths) -> Iterator[tuple[str, Held]]:
        for path in paths:
            if (seen := self._seen(path)) is not None and kind in seen[0]:
                yield from ((path, held) for held in self._read(path) if held.kind == kind)

    def _walk(self, links: Callable[[int, tuple], frozenset[str]]) -> tuple[str, ...]:
        found, level, depth = set(), list(self._rules["entry_scenes"]), 0
        while level:
            self._look(level)
            seen = {path: held for path in level if path not in found and (held := self._seen(path)) is not None}
            found |= seen.keys()
            level = sorted({link for held in seen.values() for link in links(depth, held)})
            depth += 1
        return tuple(sorted(found))

    @cached_property
    def scenes(self) -> tuple[str, ...]:
        follow = {self._rules[name]["class"] for name in ("holders", "supplier", "camera_list")}
        return self._walk(lambda depth, held: held[2] if depth < 2 or follow & held[0] else frozenset())

    @cached_property
    def _reached(self) -> tuple[str, ...]:
        return self._walk(lambda _depth, held: held[2] | held[4])

    @cached_property
    def _makers(self) -> tuple[dict[str, list], dict[str, tuple], list]:
        holder, supplier = self._rules["holders"], self._rules["supplier"]
        makers, hud, mission, queue, done = {}, {}, [], list(self.scenes), set()
        while queue:
            path = queue.pop(0)
            if path in done:
                continue
            done.add(path)
            for _path, held in self._holding(holder["class"], [path]):
                for entry in held.fields[holder["entries"]]:
                    prefab = resource_key(entry[holder["prefab"][0]][holder["prefab"][1]])
                    makers.setdefault(prefab, []).append(
                        (path, held, f"{held.fields[holder['label']]}/{entry[holder['name']]}"))
                    queue.append(prefab)
            for _path, held in self._holding(supplier["class"], [path]):
                lists = {name: [resource_key(entry[supplier["path"]]) for entry in held.fields[name]]
                         for name in (supplier["common"], supplier["players"])}
                for name, count in supplier["instanced"].items():
                    hud[f"{held.name}/{name}"] = name, lists
                    for entry in held.fields[name][:count]:
                        makers.setdefault(prefab := resource_key(entry[supplier["path"]]), []).append(
                            (path, None, f"{held.name}/{name}"))
                        queue.append(prefab)
                mission.append(((path, None), (lists[supplier["common"]][0], _ZERO)))
        return makers, hud, mission

    @cached_property
    def _files(self) -> tuple[str, ...]:
        return tuple(sorted({*self.scenes, *self._makers[0]}))

    def _positions(self, prefab: str, seen: tuple[str, ...] = ()) -> Iterator[tuple[tuple, list]]:
        for path, held, label in self._makers[0].get(prefab, ()):
            if held is None:
                yield _ZERO, [(path, "", label, None)]
            elif path.endswith(".scn"):
                yield tuple(float(v) for v in held.world[:3, 3]), [(path, held.name, label, None)]
            elif path not in seen:
                for position, chain in self._positions(path, (*seen, prefab)):
                    yield (tuple(float(v) for v in _placed(held, position)[0][:3, 3]),
                           [*chain, (path, held.name, label, position)])

    def _mentioning(self, gui: str, files: tuple[str, ...]) -> list[str]:
        return [path for path in files if (seen := self._seen(path)) is not None and gui in seen[1]]

    def _named(self, gui: str, files: tuple[str, ...]) -> list[tuple[str, Held]]:
        rule = self._rules["gui"]
        return [(path, held) for path in self._mentioning(gui, files) for held in self._read(path)
                if held.kind == rule["class"] and held.fields[rule["asset"]]
                and normalize_gui_resource_path(held.fields[rule["asset"]]) == gui]

    def segment(self, gui: str) -> int | None:
        named = self._named(gui, self._files) or self._named(gui, self._reached)
        found = {held.fields[self._rules["gui"]["segment"]] for _path, held in named}
        self.asked["segment"] = tuple(dict.fromkeys(path for path, _held in named)) if len(found) == 1 else ()
        self._keep()
        return found.pop() if len(found) == 1 else None

    @cached_property
    def message_paths(self) -> tuple[str, ...]:
        rule = self._rules["messages"]
        lists = tuple(self._holding(rule["class"], self._files))
        self.asked["messages"] = tuple(dict.fromkeys(scene for scene, _held in lists))
        self._keep()
        return tuple(path for _scene, held in lists for path in held.fields[rule["field"]])

    @cached_property
    def commands(self) -> dict[str, tuple[int, ...]]:
        rule = self._rules["commands"]
        kinds = {item["value"] for item in game_enum_definitions(self._profile.game, namespace=rule["kinds"])[
            rule["kinds"]] if item["name"] in rule["buttons"]}
        found: dict[str, dict[int, None]] = {}
        files: dict[str, None] = {}
        for path, updater in self._holding(rule["class"], self.scenes):
            for entry in updater.fields[rule["field"]]:
                for prefab, held in self._holding(rule["set"], [resource_key(entry[rule["path"]])]):
                    files |= dict.fromkeys((path, prefab))
                    for command in held.fields[rule["list"]]:
                        for name in command[rule["names"]]:
                            found.setdefault(name, {}).update(dict.fromkeys(
                                item[rule["button"]] for item in command[rule["conditions"]]
                                if item[rule["kind"]] in kinds))
        self._keep()
        self.asked["commands"] = tuple(files)
        return {name: tuple(buttons) for name, buttons in found.items()}

    @cached_property
    def _rigs(self) -> dict[str, tuple[Held, tuple]]:
        listed, rest, rule = (self._rules[name] for name in ("camera_list", "camera_rest", "camera"))
        names = {item["value"]: item["name"] for item in game_enum_definitions(
            self._profile.game, namespace=listed["names"])[listed["names"]]}
        found = {}
        for _path, manager in self._holding(listed["class"], self.scenes):
            for at, entry in enumerate(manager.fields[listed["field"]]):
                held = self._read(prefab) if self._seen(prefab := resource_key(entry[listed["path"]])) else ()
                for camera in held:
                    if camera.kind == rule["class"] and camera.fields[rule["enabled"]]:
                        root = camera.chain[0]
                        z = next((item.fields[rest["field"]] for item in held
                                  if item.kind == rest["class"] and item.at[-1] == camera.at[0]), root.position[2])
                        found[names[at]] = camera, (*root.position[:2], z)
        return found

    @cached_property
    def _fixed(self) -> int:
        rule = self._rules["timeline"]
        return next(item["value"] for item in game_enum_definitions(self._profile.game, namespace=rule["binds"])[
            rule["binds"]] if item["name"] == rule["fixed"])

    @cached_property
    def cameras(self) -> dict[str, Lens]:
        return {name: _lens(_placed(camera, rest)[0], camera.fields, self._rules["camera"])
                for name, (camera, rest) in self._rigs.items()}

    def _clip(self, path: str):
        if path not in self._clips:
            self._clips[path] = None
            if (file := self._file(resource_key(path))) is not None:
                if file[1] not in _CLIPS:
                    try:
                        _CLIPS[file[1]] = read_timeline(bytes(file[0].read()[1])), ()
                    except ClipFormatError as exc:
                        _CLIPS[file[1]] = None, (f"{file[0].path}: {exc}",)
                self._clips[path], errors = _CLIPS[file[1]]
                self.errors += errors
        return self._clips[path]

    def _bound(self, scene: str, clip) -> tuple[str, dict, dict] | None:
        rule, light, rows = self._rules["timeline"], self._rules["light"]["class"], self._read(scene)
        held, rigs, ids = {}, {}, self._fact(f"{self._seen(scene)[3]} ids", lambda: _object_ids(
            self._profile.game, *self._file(scene)[0].read()))
        below = {(above, name): at for row in rows for above, name, at in zip(row.at, row.names[1:], row.at[1:])}
        lit = {row.at[-1] for row in rows if row.kind == light}

        def bind(node, key, tracks, step, parts) -> None:
            tracks.setdefault(key, []).append(("", node))
            for child in node.children:
                if not child.children and child.name in parts(key):
                    tracks[key].append((child.name, child))
                elif (under := step(key, child.name)) is not None:
                    bind(child, under, tracks, step, parts)
        for node in clip.roots:
            if (at := ids.get(bytes(node.guid).hex())) is not None:
                bind(node, at, held, lambda key, name: below.get((key, name)), lambda key: (
                    rule["transform"], light.rsplit(".", 1)[-1] if key in lit else ""))
            for name in [] if at is not None else [
                    name for name, (camera, _rest) in self._rigs.items() if node.name in camera.names]:
                names = self._rigs[name][0].names
                bind(node, names.index(node.name), rigs.setdefault(name, {}), lambda key, child: (
                    key + 1 if names[key + 1:key + 2] == (child,) else None), lambda key: (rule["transform"],))
        return (*rigs.popitem(), held) if len(rigs) == 1 else None

    def _played(self, scene: str) -> Played | None:
        if scene not in self._moves:
            rule, self._moves[scene] = self._rules["timeline"], None
            for _path, held in self._holding(rule["class"], [scene] if scene.endswith(".scn") else ()):
                clip = self._clip(held.fields[rule["resource"]]) if held.fields[rule["bind"]] == self._fixed else None
                if clip is not None and (bound := self._bound(scene, clip)) is not None:
                    name, rig, tracks = bound
                    self._moves[scene] = Played(
                        *self._rigs[name], rig, tracks, self._rules, game_player(clip.version, self._profile.game),
                        self._profile.clip_mode, held.fields[rule["start"]], held.fields[rule["end"]],
                        rule["base_fps"] * held.fields[rule["speed"]] * (
                            rule["scene_time_scale"] if held.scale < 0 else held.scale), held.fields[rule["resource"]])
                    break
        return self._moves[scene]

    def _own(self, scene: str) -> Lens | None:
        rule = self._rules["camera"]
        return next((_lens(held.world, held.fields, rule)
                     for held in reversed(self._read(scene) if scene.endswith(".scn") else ())
                     if held.kind == rule["class"] and held.fields[rule["enabled"]]), None)

    def _lensed(self, gui: str, world: np.ndarray, scene: str) -> tuple[Lens | Played, ...]:
        if (own := self._own(scene) or self._played(scene)) is not None:
            return (own,)
        if gui in self._rules["drawn"]:
            return (self.cameras[self._rules["drawn"][gui]],)
        return tuple(lens for lens in self.cameras.values()
                     if (np.append(world[3, :3], 1.0) @ np.array(lens.view).reshape(4, 4))[2] < 0)

    def _through(self, placed: dict[str, Any], camera: Lens | Played) -> dict[str, Any]:
        if isinstance(camera, Lens):
            return {**placed, "lens": camera, "moves": None, "drawn": True}
        placed = {**placed, "moves": camera, "moment": camera.shown(placed["held"], np.array(placed["world"]).reshape(
            4, 4), self._profile.world_view_output)}
        return {**placed, **self.at(placed, placed["moment"])}

    def at(self, placed: Mapping[str, Any], frame: float) -> dict[str, Any]:
        moves, scene = placed["moves"], placed["scene"]
        lit = tuple(light for name in placed["sets"] for light in (
            self._lights(scene, None, moves, frame) if name == scene else self._sets[name]))
        return {"lens": moves.lens(frame), "drawn": moves.drawn(placed["held"], frame), "lit": lit,
                "lights": (*placed["sets"], str(lit))}

    def lights(self, name: str) -> tuple[Mapping[str, Any], ...]:
        return self._sets[name]

    def _lights(self, path: str, position, moves: Played | None = None,
                frame: float = 0.0) -> tuple[Mapping[str, Any], ...]:
        rule = self._rules["light"]
        return tuple({"Position": [float(v) for v in (held.world if position is None else _placed(
            held, position)[0])[:3, 3]], "Draw": held.draw, **{name: held.fields[name] for name in rule["fields"]},
            **({} if moves is None else moves.light(held, frame))}
                     for _path, held in self._holding(rule["class"], [path]))

    def _lit(self, chain: list) -> list[tuple[str, list]]:
        _makers, hud, mission = self._makers
        files = [(path, position) for path, _name, _label, position in chain]
        if chain[0][2] not in hud:
            return [("", [*(mission[0] if mission and chain[0][0].startswith(self._rules["mission_time"]) else ()),
                          *files])]
        supplier = self._rules["supplier"]
        name, lists = hud[chain[0][2]]
        common = lists[supplier["common"]][0], _ZERO
        if name == supplier["players"]:
            return [("", [files[0], common, *files[1:]])]
        return [(f" + {player.rsplit('/', 1)[-1]}", [
            files[0], *(() if name == supplier["common"] else (common,)),
            *((files[1], (player, _ZERO)) if name == supplier["common"] else ((player, _ZERO), files[1])), *files[2:]])
            for player in lists[supplier["players"]][:supplier["instanced"][supplier["players"]]]]

    def _sets_of(self, files: list) -> tuple[str, ...]:
        names = []
        for path, position in files:
            name = path if position is None or not any(position) else (
                f"{path} at {', '.join(f'{v:g}' for v in position)}")
            if name not in self._sets:
                self._sets[name], self._lit_by[name] = self._lights(path, position), path
            if self._sets[name]:
                names.append(name)
        return tuple(names)

    def placements(self, gui: str, meshes: bool) -> tuple[Mapping[str, Any], ...]:
        if (gui, meshes) not in self._placements:
            distinct = {}
            for path, held in self._named(gui, self._files):
                made = [(held.world, held.rotation, [(path, held.name, "", None)])] if path.endswith(".scn") else [
                    (*_placed(held, position), [*chain, (path, held.name, "", position)])
                    for position, chain in self._positions(path)]
                for world, rotation, chain in made:
                    world = np.asarray(world, np.float32).T
                    seen = self._lensed(gui, world, chain[0][0])
                    for suffix, files in self._lit(chain) if meshes else [("", [])]:
                        distinct.setdefault((tuple(world.ravel().tolist()), seen, suffix, self._sets_of(files), tuple(
                            np.float32(rotation).tolist())), (chain, held if path == chain[0][0] else None))
            self._placements[gui, meshes] = tuple(self._through({
                "name": (label or f"{scene.rsplit('/', 1)[-1]} {name}") + suffix, "world": world, "rotation": rotation,
                "scene": scene, "held": held, "sets": lights, "lights": lights,
                "lit": tuple(light for name in lights for light in self._sets[name]),
                "chain": (scene, *(item[0] for item in below))}, seen[0])
                for (world, seen, suffix, lights, rotation), (((scene, name, label, _position), *below), held)
                in distinct.items() if len(seen) == 1)
        return self._placements[gui, meshes]

    def behind(self, placed: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
        scene, moves = placed["scene"], placed["moves"]
        rig = None if self._own(scene) is not None else moves.camera if moves is not None else next(
            self._rigs[name][0] for name, lens in self.cameras.items() if lens is placed["lens"])
        return {"placement": placed["chain"], "camera": (scene if rig is None else next(
            path for path, rows in self._got.items() if any(row is rig for row in rows)),),
            "timeline": () if moves is None else (moves.clip,),
            "lights": tuple(dict.fromkeys(self._lit_by[name] for name in placed["sets"]))}

    def found(self, path: str) -> str:
        return self._at[resource_key(path)][0].path

    def _files_of(self, gui: str) -> dict[str, tuple]:
        if gui not in self._closures:
            files, queue = {}, [gui]
            while queue:
                if (path := queue.pop()) not in files and (file := self._file(path)) is not None:
                    files[path] = file
                    queue += self._seen(path)[1]
            self._closures[gui] = files
        return self._closures[gui]

    def _viewed(self, gui: str, files: Mapping[str, tuple]) -> list:
        source, data = files[gui][0].read()
        scene = GuiWorkspace.from_document(gui, parse_gui(bytes(data), str(source)), lambda path: (
            found := self._locate(path)) and found.read(), profile=self._profile).instantiate()
        view = scene.runtime_properties(scene.root)
        return ["screen" if self._profile.enum_label(view, "ViewType") == "Screen" else
                "camera" if view["UseGUICamera"] else "world",
                any("mesh" in self._profile.runtime_type(node.object.type_name).facts for node in scene.tree_nodes),
                sorted({node.resource.key for node in scene.tree_nodes})]

    def _view(self, gui: str) -> tuple[str, bool, frozenset[str]]:
        if gui not in self._views:
            files = self._files_of(gui)
            stamp = gui, *(file[1] for _path, file in sorted(files.items()))
            if stamp not in _VIEWS:
                key = " ".join(("view", gui, *(f"{path} {self._seen(path)[3]}" for path in sorted(files))))
                try:
                    kind, meshes, held = self._fact(blake2b(key.encode(), digest_size=16).hexdigest(),
                                                    lambda: self._viewed(gui, files))
                    _VIEWS[stamp] = (kind, meshes, frozenset(held)), ()
                except (GuiFormatError, GuiSceneError) as exc:
                    _VIEWS[stamp] = ("", False, frozenset()), (f"{files[gui][0].path}: {exc}",)
            self._views[gui], errors = _VIEWS[stamp]
            self.errors += errors
        return self._views[gui]

    def _holders_of(self, gui: str) -> list[str]:
        named = {found for path in self._files if (seen := self._seen(path)) for found in seen[1]}
        return sorted(item for item in named - {gui} if gui in self._files_of(item) and gui in self._view(item)[2])

    def shown(self, gui: str) -> tuple[str, ...]:
        if gui not in self._shown:
            self._shown[gui] = ()
            if not self._mentioning(gui, self._files) and gui in self._files_of(gui):
                own, views = self._view(gui), {item: self._view(item) for item in self._holders_of(gui)}
                if not (own[0] == "screen" and any(view[0] == "screen" and self._mentioning(item, self._files)
                                                   for item, view in views.items())):
                    self._shown[gui] = tuple(item for item, view in views.items() if view[0] == "camera"
                                             and self.placements(item, view[1]))
        return self._shown[gui]

    def places(self, gui: str, camera: bool, meshes: bool) -> tuple[Mapping[str, Any], ...]:
        if (gui, camera, meshes) not in self._places:
            self._places[gui, camera, meshes] = (self.placements(gui, meshes) if camera else ()) or tuple(
                {**item, "name": f"{view.rsplit('/', 1)[-1].removesuffix('.gui')}: {item['name']}", "of": view,
                 "chain": (*item["chain"], view)}
                for view in self.shown(gui) for item in self.placements(view, self._view(view)[1]))
            self._keep()
        return self._places[gui, camera, meshes]
