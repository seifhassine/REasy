"""See docs/internals/mesh-preview.md, Objects added in view."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

import numpy as np
from PySide6.QtCore import QCoreApplication

lazy from file_handlers.engine_math import BOX_CORNERS
lazy from file_handlers.mmtr.draw_rules import draw_rules_for, drawing_rules
lazy from file_handlers.mmtr.engine_buffers import Camera, TransitionBuffer, TransitionInput, scene_values
lazy from file_handlers.mmtr.pretransform import pretransform_catalog_for
lazy from file_handlers.mmtr.profiles import mmtr_profile_for_game
lazy from file_handlers.sfur.shell_fur import thinned
lazy from file_handlers.stmesh.lit import SpeedTreeLevels, speedtree_picks
lazy from ui.scene.mesh_scene import mesh_lod_parts
lazy from ui.scene.scene_view import SceneGeometry
from utils.native_build import ensure_fastmesh

lazy from .lod_settings import ClassicLevels, LodSettingsError, classic_fractions, classic_picks, classic_table
lazy from .scene_lit import SceneLitMesh, _bone_boxed, _Gathered, _MeshRead, _skinned_world_box

_fastmesh = ensure_fastmesh()
translate = QCoreApplication.translate


def placed_boxes(local: np.ndarray, worlds: np.ndarray, oriented: np.ndarray) -> np.ndarray:
    m = np.asarray(worlds, np.float32)
    low, high = local[:, 0], local[:, 1]
    corners = local[:, BOX_CORNERS, np.arange(3)]
    turned = (m[:, None, :, 0] * corners[..., 0:1] + m[:, None, :, 1] * corners[..., 1:2]) \
        + m[:, None, :, 2] * corners[..., 2:3]
    boxes = np.stack([turned.min(axis=1), turned.max(axis=1)], axis=1) + m[:, None, :, 3]
    half = np.float32(0.5)
    centre, extent = (high + low) * half, (high - low) * half
    anchor = np.zeros((len(m), 4, 4), np.float32)
    anchor[:, np.arange(4), np.arange(4)] = 1
    anchor[:, :3, 3] = centre
    terms = m[:, :, :, None] * anchor[:, None, :, :]
    frame = (terms[:, :, 0] + terms[:, :, 1]) + (terms[:, :, 2] + terms[:, :, 3])
    signed = np.where(BOX_CORNERS.astype(bool), extent[:, None, :], -extent[:, None, :])
    points = ((frame[:, None, :, 0] * signed[..., 0:1] + frame[:, None, :, 1] * signed[..., 1:2])
              + frame[:, None, :, 2] * signed[..., 2:3]) + frame[:, None, :, 3]
    return np.where(np.asarray(oriented, bool)[:, None, None],
                    np.stack([points.min(axis=1), points.max(axis=1)], axis=1), boxes)


def _file_box(item) -> np.ndarray | None:
    if item.of.box is not None:
        return item.of.box.box
    return None if item.levels is None or item.levels.speedtree is None else np.asarray(item.levels.speedtree.bounds)


def _set_lod(fields: Mapping[str, object] | None, levels: SpeedTreeLevels) -> tuple[int, float]:
    manual = levels.rule.manual
    if manual is None or fields is None or fields.get(manual[0]) != manual[1]:
        return -1, 1.0
    count = levels.solid + levels.billboard
    share = np.float32(count - 1) * np.float32(fields.get(manual[2], 0.0))
    return min(max(int(share), 0), count - 1), float(np.float32(1.0) - np.fmod(share, np.float32(1.0)))


def _classic_mode(rules, fields: Mapping[str, object] | None, rule) -> tuple[int, int, int, int]:
    if not rule.mode:
        return -1, rule.offset, 0, -1
    mode = int(rules.value_of(fields, rule.mode))
    if mode == rule.modes.get("manual"):
        found = int(rules.value_of(fields, rule.level)), 0
    else:
        found = 0 if mode == rule.modes.get("follow_parent") else -1, rule.offset
    held = (fields or {}).get(rule.range) if rules.value_of(fields, rule.range_enabled) else None
    return (*found, *((held.s, held.s + held.r) if held is not None else (0, -1)))


def _classic_metric(camera: Camera, view: np.ndarray, centres: np.ndarray, over_p00: np.ndarray,
                    scale: np.ndarray) -> np.ndarray:
    f = np.float32
    eye = (-view[:, :3].T @ view[:, 3]).astype(f)
    forward = (-view[2, :3]).astype(f)
    p11, p15 = (f(0), f(1)) if camera.orthographic else (f(-1), f(0))
    inverse = f(1) / f(camera.scale[0])
    slope = np.where(over_p00, inverse * p11, p11).astype(f)
    base = inverse * p15 - (forward[0] * eye[0] + eye[1] * forward[1] + eye[2] * forward[2]) * slope
    at = centres.astype(f)
    return ((at[:, 1] * forward[1] + at[:, 0] * forward[0] + at[:, 2] * forward[2]) * slope + base) * scale


class LodRules:
    def __init__(self, content, lazy: Sequence[int], levels: Sequence):
        objects = content.objects
        self.speedtree = np.array([row for row, item in enumerate(levels)
                                   if item is not None and item.speedtree is not None], np.int64)
        inputs = [levels[row].speedtree for row in self.speedtree.tolist()]
        self._squares = np.array([item.squares for item in inputs], np.float32).reshape(-1, 4)
        self._solid = np.array([item.solid for item in inputs], np.int64)
        self._billboard = np.array([item.billboard for item in inputs], bool)
        self._bias = np.array([item.rule.bias for item in inputs], np.int64)
        self._nudge = np.array([item.rule.nudge for item in inputs], np.float32)
        self._narrow = np.array([item.rule.narrow or (0.0, item.rule.bias) for item in inputs],
                                np.float64).reshape(-1, 2)
        sets = [_set_lod(objects[lazy[row]].draw_state, item) for row, item in zip(self.speedtree.tolist(), inputs)]
        self._set = np.array([level for level, _transition in sets], np.int64)
        self._set_transition = np.array([transition for _level, transition in sets], np.float32)
        self._centre = np.array([np.mean(item.bounds, axis=0) for item in inputs], np.float64).reshape(-1, 3)
        self.classic = np.array([row for row, item in enumerate(levels)
                                 if item is not None and item.classic is not None], np.int64)
        classic = [levels[row].classic for row in self.classic.tolist()]
        ruled = [item.rules[objects[lazy[row]].decal_receive[0]] for row, item in zip(self.classic.tolist(), classic)]
        self._classic_centre = np.array([item.centre for item in classic], np.float64).reshape(-1, 3)
        self._classic_count = np.array([item.count for item in classic], np.int64)
        self._classic_scale = np.array([item.scale for item in classic], np.float32)
        self._classic_thresholds = np.full((len(classic), max((item.count - 1 for item in classic), default=0)),
                                           np.inf, np.float32)
        for row, (item, (_rule, thresholds)) in enumerate(zip(classic, ruled)):
            self._classic_thresholds[row, :item.count - 1] = thresholds
        self._classic_over_p00 = np.array([rule.depth_over_p00 for rule, _thresholds in ruled], bool)
        rules = drawing_rules(mmtr_profile_for_game(content.game)) if classic else None
        modes = np.array([_classic_mode(rules, objects[lazy[row]].draw_state, rule)
                          for row, (rule, _thresholds) in zip(self.classic.tolist(), ruled)], np.int64).reshape(-1, 4)
        self._classic_set, self._classic_offset, self._classic_range = modes[:, 0], modes[:, 1], modes[:, 2:]
        profile = mmtr_profile_for_game(content.game) if lazy else None
        small = None if profile is None or draw_rules_for(profile) is None else drawing_rules(profile)
        self._small = None
        if small is not None and small.small_objects is not None:
            rule = small.small_objects
            kinds = [(objects[number].decal_receive[0], objects[number]) for number in lazy]
            boxes = [_file_box(item) for _kind, item in kinds]
            tested = [row for row, (kind, _item) in enumerate(kinds)
                      if (kind in rule.cpu or kind in rule.gpu) and boxes[row] is not None]
            if tested:
                factors = np.array([small.value_of(kinds[row][1].draw_state, rule.cpu[kinds[row][0]], kinds[row][0])
                                    if kinds[row][0] in rule.cpu else np.nan for row in tested], np.float32)
                self._small = (rule.ratio, np.array(tested, np.int64), factors,
                               np.array([boxes[row] for row in tested], np.float32),
                               np.array([rule.boxes[kinds[row][0]] == "oriented_box" for row in tested], bool),
                               rule.gpu_bounds)
        self.small_boxes: np.ndarray | None = None
        self._culling: tuple | None = None
        self._count = len(lazy)
        self._centres = np.zeros((len(self.speedtree), 3))
        self._classic_centres = np.zeros((len(self.classic), 3))

    def place(self, moved: np.ndarray, rows: np.ndarray, worlds: np.ndarray, skinned: Mapping[int, np.ndarray]) -> None:
        for picked, local, world in ((self.speedtree, self._centre, self._centres),
                                     (self.classic, self._classic_centre, self._classic_centres)):
            ruled = np.flatnonzero(moved[picked])
            if len(ruled):
                placed = worlds[np.searchsorted(rows, picked[ruled])]
                world[ruled] = np.einsum("oij,oj->oi", placed[:, :, :3], local[ruled]) + placed[:, :, 3]
        for at in np.flatnonzero(np.isin(self.classic, list(skinned))).tolist():
            box = skinned[int(self.classic[at])]
            self._classic_centres[at] = (box[0] + box[1]) * np.float32(0.5)
        if self._small is not None:
            tested, local, oriented = self._small[1], self._small[3], self._small[4]
            if self.small_boxes is None:
                self.small_boxes = np.zeros((len(tested), 2, 3), np.float32)
            changed = np.flatnonzero(moved[tested])
            if len(changed):
                self.small_boxes[changed] = placed_boxes(
                    local[changed], worlds[np.searchsorted(rows, tested[changed])], oriented[changed])
                for at in np.flatnonzero(np.isin(tested, list(skinned))).tolist():
                    self.small_boxes[at] = skinned[int(tested[at])]
                self._culling = None

    def pick(self, camera: Camera, view: np.ndarray, level: np.ndarray, transition: np.ndarray) -> None:
        if len(self.speedtree):
            to = (self._centres + view[:, :3].T @ view[:, 3]).astype(np.float32)
            squared = to[:, 0] * to[:, 0] + to[:, 1] * to[:, 1] + to[:, 2] * to[:, 2]
            # An orthographic view, which the game has not, takes a wide view's bias.
            fov = np.inf if camera.orthographic else np.degrees(2 * np.arctan(1 / camera.scale[0]))
            bias = np.where(fov < self._narrow[:, 0], self._narrow[:, 1], self._bias).astype(np.int64)
            picked, transitions = speedtree_picks(squared, self._squares, self._solid, self._billboard, bias,
                                                  self._nudge)
            level[self.speedtree] = np.where(self._set >= 0, self._set, picked)
            transition[self.speedtree] = np.where(self._set >= 0, self._set_transition, transitions)
        if len(self.classic):
            level[self.classic] = self._classic_lods(camera, view, self._classic_centres)

    def _classic_lods(self, camera: Camera, view: np.ndarray, centres: np.ndarray) -> np.ndarray:
        metric = _classic_metric(camera, view, centres, self._classic_over_p00, self._classic_scale)
        picked = classic_picks(metric, self._classic_thresholds)
        lod = np.where(self._classic_set >= 0, self._classic_set, picked) + self._classic_offset
        lod = np.minimum(np.maximum(lod, 0), self._classic_count - 1)
        first, last = self._classic_range[:, 0], self._classic_range[:, 1]
        lod = np.where(first <= last, np.where(lod < first, first, np.minimum(lod, last)), lod)
        return np.where(lod < self._classic_count, lod, -1)

    def culled(self, camera: Camera) -> np.ndarray | None:
        if self._small is None:
            return None
        key = (np.asarray(camera.view, np.float64).tobytes(), camera.scale, camera.size, camera.orthographic,
               camera.jitter)
        if self._culling is None or self._culling[0] != key:
            f = np.float32
            ratio, tested, factors = self._small[:3]
            matrix = np.asarray(scene_values(replace(camera, previous=None))["viewProjMat"], f)
            threshold = f(max(f(1) / f(camera.size[0]), f(1) / f(camera.size[1]))) * f(ratio)
            half = np.asarray(camera.size, f) * f(0.5)
            out = np.zeros(self._count, bool)
            # The shader's bounds skip NaNs (FMin, FMax).
            out[tested] = np.frombuffer(_fastmesh.small_culled(
                self.small_boxes, factors, matrix, float(threshold), float(f(self._small[5])), float(half[0]),
                float(half[1])), bool)
            self._culling = key, out
        return self._culling[1]


class ShellPicks:
    def __init__(self, content, lazy: np.ndarray):
        objects = content.objects
        self.numbers = np.array([number for number, item in enumerate(objects)
                                 if item.shell_fur is not None and item.shell_fur.levels is not None], np.int64)
        self.rows = np.full(len(objects), -1, np.int64)
        self.rows[self.numbers] = np.arange(len(self.numbers))
        held = [objects[number].shell_fur.levels for number in self.numbers.tolist()]
        self._local = np.array([item.centre for item in held], np.float64).reshape(-1, 3)
        self._scale = np.array([item.scale for item in held], np.float32)
        self._over_p00 = np.array([item.over_p00 for item in held], bool)
        # A mesh of one LOD's thresholds are past every metric.
        self._mesh = np.full((len(held), max((len(item.mesh) for item in held), default=0) or 1), np.inf, np.float32)
        for row, item in enumerate(held):
            self._mesh[row, :len(item.mesh)] = item.mesh
        self._shells = np.array([item.shells for item in held], np.float32).reshape(len(held), -1) if held \
            else np.zeros((0, 1), np.float32)
        added = set(lazy.tolist())
        self._set = np.array([item.level if number in added else 0
                              for number, item in zip(self.numbers.tolist(), held)], np.int64)
        self._placed: tuple | None = None
        self._looked: tuple | None = None

    def place(self, content, placements) -> None:
        if self._placed is not None and self._placed[0] is placements:
            return
        centres = np.zeros((len(self.numbers), 3))
        for row, number in enumerate(self.numbers.tolist()):
            item = content.objects[number]
            placement = placements[item.mesh]
            if item.of.skinned:
                box = _skinned_world_box(item.of, placement)
                centres[row] = (box[0] + box[1]) * np.float32(0.5)
            else:
                world = np.asarray(placement.world, np.float64).reshape(3, 4)
                centres[row] = world[:, :3] @ self._local[row] + world[:, 3]
        self._placed, self._looked = (placements, centres), None

    def look(self, camera: Camera) -> tuple[np.ndarray, np.ndarray]:
        key = np.asarray(camera.view, np.float64).tobytes(), camera.scale, camera.orthographic
        if self._looked is None or self._looked[0] != key:
            metric = _classic_metric(camera, np.asarray(camera.view, np.float64).reshape(3, 4), self._placed[1],
                                     self._over_p00, self._scale)
            self._looked = (key, np.where(self._set >= 0, self._set, classic_picks(metric, self._mesh)),
                            classic_fractions(metric, self._shells))
        return self._looked[1:]


class OwnerLods:
    def __init__(self):
        self.transitions: list[tuple[tuple[str, bytes], ...]] = []
        self.giving = np.zeros(0, np.int64)
        self.given = np.zeros(0, np.float32)
        self.furred: list[int] = []
        self.scanned = 0
        self.thinned: dict[int, tuple[int, ...]] = {}

    def add(self, entries: Sequence) -> None:
        start = len(self.transitions)
        self.transitions += [tuple((name, bytes(value)[4:]) for name, value in entry.of.engine.items()
                                   if isinstance(value, (TransitionBuffer, TransitionInput))) for entry in entries]
        self.giving = np.concatenate([self.giving, [start + number for number, names in
                                                    enumerate(self.transitions[start:]) if names]]).astype(np.int64)
        self.given = np.concatenate([self.given, np.full(len(entries), np.nan, np.float32)])

    def give(self, mesh, streams, held, camera: Camera) -> None:
        self._thin(mesh, streams.shells, held, camera)
        if not len(self.giving):
            return
        _owners, rows, _levels = streams.owned(held)
        giving = self.giving[rows[self.giving] >= 0]
        _key, seen, _level, _distance, transitions = streams.look(camera)
        giving = giving[seen[rows[giving]] & ~streams.hidden(camera, held)[giving]]
        values = transitions[rows[giving]]
        changed = values != self.given[giving]
        for owner, value in zip(giving[changed].tolist(), values[changed]):
            mesh.give(owner, {name: value.tobytes() + tail for name, tail in self.transitions[owner]})
        self.given[giving[changed]] = values[changed]

    def _thin(self, mesh, shells: ShellPicks, held, camera: Camera) -> None:
        self.furred += [owner for owner in range(self.scanned, len(mesh.objects))
                        if (fur := mesh.objects[owner].shell_fur) is not None and fur.levels is not None]
        self.scanned = len(mesh.objects)
        if not self.furred:
            return
        fractions = shells.look(camera)[1]
        for owner in self.furred:
            fur = mesh.objects[owner].shell_fur
            fraction = fractions[shells.rows[held.owners[owner]]]
            drawn = tuple(thinned(record.shells, fraction, len(fur.levels.shells) + 1, record.parameter.bottom_save,
                                  fur.levels.lod_limit) for record in fur.records)
            if self.thinned.get(owner) != drawn:
                mesh.thin(owner, drawn)
                self.thinned[owner] = drawn


class SceneLitPreviewLods:
    def _classic_pieces(self, session, group: _Gathered,
                        read: dict[int, tuple[object, object, _MeshRead]]) -> tuple[dict, tuple, tuple] | str | None:
        profile = mmtr_profile_for_game(group.game)
        rules = None if profile is None else draw_rules_for(profile)
        first, held = group.meshes.get(0), read.get(id(session.handler))
        if rules is None or not rules.lods or held is None or not isinstance(first, SceneLitMesh) \
                or first.skinned and not _bone_boxed(first) or first.level is not None or first.meshlets:
            return None
        mesh = held[2].mesh
        main = mesh.meshes[0] if mesh.meshes else None
        count = 0 if main is None else len(main.lods)
        if count < 2:
            return None
        # The scale depends on the radius alone, so any rule's serves.
        ruled = {}
        for kind, rule in rules.lods.items():
            settings = self._lod_settings_for(session, group, rule)
            if isinstance(settings, str):
                return settings
            try:
                scale, thresholds = classic_table(settings, int(mesh.header.lod_hash), count,
                                                  float(main.bounding_sphere[3]), rule.rate, rule.rate2, rule.size,
                                                  rule.exponent)
            except LodSettingsError as exc:
                return str(exc)
            ruled[kind] = rule, thresholds
        whole = self._read_whole(session.handler, set(range(count)), pretransform_catalog_for(profile), first.skinned,
                                 read)
        if isinstance(whole, str):
            return whole
        names, bounds, pieces = whole.mesh.material_names, {}, []
        for lod in range(1, count):
            lod_read = whole.lods[lod]
            if isinstance(lod_read, str):
                return lod_read
            group.lods[lod] = lod_read
            bounds[lod] = lod_read.bounds
            for number, (_index, submesh) in enumerate(mesh_lod_parts(whole.mesh, lod)):
                material = submesh.material_index
                pieces.append((lod, number, 1, session.material_key(names[material] if 0 <= material < len(names)
                                                                    else "")))
        # The engine's world box is the file's box through the world matrix, the centre its centre's; a skinned
        # mesh's is its bones' (``_Streams.place``).
        box = np.asarray(main.bounding_box, np.float64)[:, :3]
        levels = ClassicLevels(tuple(map(float, (box[0] + box[1]) * 0.5)), count, scale, ruled)
        return bounds, tuple(pieces), (np.inf, np.inf, np.inf, np.inf, None, None, None, levels)

    def _classic_refused(self, group: _Gathered, mesh: SceneGeometry, levels: ClassicLevels) -> str | None:
        held = levels.rules.get(mesh.decal_receive[0])
        if held is None:
            return ""
        rule = held[0]
        if not rule.mode:
            return None
        mode = int(drawing_rules(mmtr_profile_for_game(group.game)).value_of(mesh.draw_state, rule.mode))
        if mode not in rule.modes.values():
            return translate("SceneLitPreview",
                             "A mesh's LOD mode {mode} is not built: it draws its first LOD.").format(mode=mode)
        if mode == rule.modes.get("follow_parent") and mesh.lod_parent_mesh:
            return translate("SceneLitPreview",
                             "A mesh taking its parent mesh's LOD is not built: it draws its first LOD.")
        return None


__all__ = ["LodRules", "OwnerLods", "SceneLitPreviewLods", "ShellPicks", "placed_boxes"]
