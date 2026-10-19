"""See docs/internals/strands-rendering.md, Colliders."""
from __future__ import annotations

import dataclasses

import numpy as np

from file_handlers.clsp.clsp_file import EVERY_TAG, TAG_BITS, CollisionShape, ShapeKind, read_clsp, write_clsp
from file_handlers.clsp.geometry import bounds, depth, made, moved
from file_handlers.clsp.placement import hung_joints, place
from utils.hash_util import murmur3_hash_utf16le

from .chain_scene import ChainScene, Holding, Settled, settle
from .editing import StrandEdit, ranges
from .placement import PrefabPlacement

_SLACK = 1e-6


class SetColliders:
    def __init__(self, placement, shapes, files, joints, record, unplaced=(), lost=(), holdings=()):
        self.placement, self.joints = placement, {murmur3_hash_utf16le(name): name for name in joints}
        self.read = {file.found: file.data for file in files}
        self.files = {file.found: list(read_clsp(file.data, file.found)) for file in files}
        self.entries = [(owner, shape.read_from) for owner, shape in (*shapes, *lost)]
        self.owners = {path: owner for owner, (path, _index) in reversed(self.entries)}
        self.unplaced = [shape.read_from for shape in unplaced if shape.read_from[0] in self.files]
        self.fixed = tuple(shape for shape in unplaced if shape.read_from[0] not in self.files)
        self.holdings: tuple[Holding, ...] = tuple(holdings)
        self._record, self._held, self.revision = record, None, 0
        self._placed, self._settled = (None, ()), (None, None, {})

    @classmethod
    def of(cls, scene: ChainScene, files, world: np.ndarray, record) -> SetColliders:
        """``world`` is 4 x 4, column vectors."""
        placement = PrefabPlacement(scene.document, scene.character, scene.holder, scene.catalog, scene.viewed,
                                    scene.meshes.get, scene.skeletons.get)
        placement.pose(np.asarray(world, np.float32).T)
        return cls(placement, scene.colliders, files, scene.viewed.names, record, scene.unplaced, scene.lost,
                   scene.holdings)

    def shape(self, number: int) -> CollisionShape:
        path, index = self.entries[number][1]
        return self.files[path][index]

    def placed(self) -> tuple[CollisionShape, ...]:
        """In world space."""
        if self._placed[0] != self.revision:
            self._placed = self.revision, tuple(
                place(dataclasses.replace(self.shape(number), tag=self.reaching(number)), *self.placement.hung(owner))
                for number, (owner, _at) in enumerate(self.entries))
        return self._placed[1]

    def taken(self) -> list[CollisionShape]:
        return [*(dataclasses.replace(self.shape(number), tag=self.reaching(number))
                  for number in range(len(self.entries))), *self.away()]

    def away(self) -> list[CollisionShape]:
        return [dataclasses.replace(shape, tag=self._reach(shape))
                for shape in (*(self.files[path][index] for path, index in self.unplaced), *self.fixed)]

    def settled(self) -> tuple[Settled | None, dict[tuple[str, int], int]]:
        if self._settled[0] != self.revision:
            shapes = [self.files.get(holding.path, holding.shapes) for holding in self.holdings]
            weighed = settle(self.holdings, shapes) if self.holdings else None
            reached: dict[tuple[str, int], int] = {}
            for held, tags in zip(shapes, weighed.reached if weighed else ()):
                for shape, tag in zip(held, tags):
                    reached[tuple(shape.read_from)] = reached.get(tuple(shape.read_from), 0) | tag
            self._settled = self.revision, weighed, reached
        return self._settled[1], self._settled[2]

    def _reach(self, shape: CollisionShape) -> int:
        if not any(holding.path == shape.read_from[0] for holding in self.holdings):
            return shape.tag
        return self.settled()[1].get(tuple(shape.read_from), 0)

    def reaching(self, number: int) -> int:
        return self._reach(self.shape(number))

    def lost_to(self, at: tuple[str, int]) -> list[tuple[int, Holding | None]]:
        shape = self.files[at[0]][at[1]]
        lost = shape.tag & ~self._reach(shape)
        weighed, _reached = self.settled()
        own = next((holding for holding in self.holdings if holding.path == at[0] and not holding.everywhere), None)
        taken: dict[int | None, int] = {}
        for bit in range(TAG_BITS):
            if lost >> bit & 1:
                taker = None if weighed is None or own is None else weighed.taker(own, bit)
                taken[taker] = taken.get(taker, 0) | 1 << bit
        return [(bits, None if taker is None else self.holdings[taker])
                for taker, bits in sorted(taken.items(), key=lambda item: (item[0] is None, item[0] or 0))]

    def lost(self) -> list[int]:
        return [number for number in range(len(self.entries)) if self.shape(number).tag and not self.reaching(number)]

    def free_bit(self) -> int | None:
        used = 0
        for shapes in (*self.files.values(), self.fixed,
                       *(holding.shapes for holding in self.holdings if holding.path not in self.files)):
            for shape in shapes:
                used |= shape.tag
        return next((1 << bit for bit in range(TAG_BITS) if not used >> bit & 1), None)

    def placeable(self, shape: CollisionShape) -> bool:
        return all(key in self.joints for key in hung_joints(shape))

    def changed_files(self) -> list[tuple[str, bytes]]:
        made = ((path, write_clsp(shapes)) for path, shapes in self.files.items())
        return [(path, data) for path, data in made if data != self.read[path]]

    def _state(self):
        return {path: list(shapes) for path, shapes in self.files.items()}, list(self.entries), list(self.unplaced)

    def _restore(self, state):
        now = self._state()
        self.files, self.entries, self.unplaced = state
        self.revision += 1
        return lambda: self._restore(now)

    def _changed(self, before) -> None:
        self.revision += 1
        self._record(lambda: self._restore(before))

    def begin(self) -> None:
        self._held = self._state()

    def end(self, keep: bool) -> None:
        held, self._held = self._held, None
        if held is not None:
            if keep:
                self._changed(held)
            else:
                self._restore(held)

    def put(self, number: int, shape: CollisionShape) -> None:
        before = self._held or self._state()
        path, index = self.entries[number][1]
        self.files[path][index] = dataclasses.replace(shape, read_from=(path, index))
        if self._held is None:
            self._changed(before)
        else:
            self.revision += 1

    def transform(self, number: int, matrix: np.ndarray, start: CollisionShape) -> None:
        """``matrix`` is in world space."""
        hung, _scale = self.placement.hung(self.entries[number][0])
        turned = np.asarray(matrix, np.float64).T

        def local(key: int) -> np.ndarray:
            joint = np.asarray(hung(key), np.float64)
            return joint @ turned @ np.linalg.inv(joint)
        self.put(number, moved(start, local(start.joint), local(start.end_joint)))

    def rejoint(self, number: int, joint: int, end_joint: int) -> None:
        shape = self.shape(number)
        hung, _scale = self.placement.hung(self.entries[number][0])

        def between(old: int, new: int) -> np.ndarray:
            return np.asarray(hung(old), np.float64) @ np.linalg.inv(np.asarray(hung(new), np.float64))
        if shape.kind not in (ShapeKind.CAPSULE, ShapeKind.TAPERED_CAPSULE):
            end_joint = joint if shape.end_joint == shape.joint else shape.end_joint
        kept = moved(shape, between(shape.joint, joint), between(shape.end_joint, end_joint))
        self.put(number, dataclasses.replace(kept, joint=joint, end_joint=end_joint))

    def add(self, kind: int, path: str, joint: int, end_joint: int, tag: int, size: float) -> int | None:
        before = self._state()
        shapes = self.files[path]
        shape = dataclasses.replace(made(kind, joint, end_joint, tag, size), read_from=(path, len(shapes)))
        shapes.append(shape)
        number = None
        if self.placeable(shape):
            self.entries.append((self.owners[path], shape.read_from))
            number = len(self.entries) - 1
        else:
            self.unplaced.append(shape.read_from)
        self._changed(before)
        return number

    def copy(self, number: int) -> int:
        before = self._state()
        owner, (path, _index) = self.entries[number]
        shapes = self.files[path]
        shapes.append(dataclasses.replace(self.shape(number), read_from=(path, len(shapes))))
        self.entries.append((owner, (path, len(shapes) - 1)))
        self._changed(before)
        return len(self.entries) - 1

    def remove(self, number: int) -> None:
        before = self._state()
        _owner, (path, index) = self.entries.pop(number)
        shapes = self.files[path]
        del shapes[index]
        shapes[index:] = [dataclasses.replace(shape, read_from=(path, at))
                          for at, shape in enumerate(shapes[index:], index)]
        self.entries = [(owner, (held, at - (held == path and at > index))) for owner, (held, at) in self.entries]
        self.unplaced = [(held, at - (held == path and at > index)) for held, at in self.unplaced]
        self._changed(before)

    def through(self, edit: StrandEdit, world: np.ndarray, strands: np.ndarray, tag: int) -> np.ndarray:
        return self._inside(edit, world, strands, tag, 0.0)[0]

    def rooted(self, edit: StrandEdit, world: np.ndarray, strands: np.ndarray, tag: int) -> dict[int, np.ndarray]:
        world = np.asarray(world, np.float64)
        roots = edit.roots(strands).astype(np.float64) @ world[:3, :3].T + world[:3, 3]
        held = {}
        for number, shape in enumerate(self.placed()):
            if shape.tag & tag and (holds := _holds(roots, shape)).any():
                held[number] = holds
        return held

    def thrown(self, edit: StrandEdit, world: np.ndarray, strands: np.ndarray, tag: int) -> dict[int, np.ndarray]:
        """The game's chain throws these strands out."""
        world, strands = np.asarray(world, np.float64), np.asarray(strands, np.int64)
        counts = np.diff(edit.lod0.starts)[strands]
        thrown = {}
        for number, holds in self.rooted(edit, world, strands, tag).items():
            held = np.flatnonzero(holds)
            points = (edit.positions[edit.lod0.rows(strands[held])].astype(np.float64) @ world[:3, :3].T
                      + world[:3, 3])
            inside = depth(points, self.placed()[number])[0] > _SLACK
            inside[np.cumsum(counts[held]) - counts[held]] = False
            thrown[number] = np.zeros(len(strands), bool)
            thrown[number][held[np.repeat(np.arange(len(held)), counts[held])[inside]]] = True
        return thrown

    def push_out(self, edit: StrandEdit, world: np.ndarray, strands: np.ndarray, tag: int, margin: float,
                 rounds: int = 16) -> None:
        strands = np.asarray(strands, np.int64)
        edit.begin_stroke()
        for _ in range(rounds):
            hit, points = self._inside(edit, world, strands, tag, margin)
            if not hit.any():
                break
            strands, points = strands[hit], points[np.repeat(hit, np.diff(edit.lod0.starts)[strands])]
            edit.move(edit.lod0.rows(strands), points, keep_lengths=True)
        edit.end_stroke()

    def tags(self, numbers=None) -> int:
        tag = 0
        for number in range(len(self.entries)) if numbers is None else numbers:
            tag |= self.reaching(number)
        return tag

    def _inside(self, edit: StrandEdit, world, strands, tag: int, margin: float) -> tuple[np.ndarray, np.ndarray]:
        world, strands = np.asarray(world, np.float64), np.asarray(strands, np.int64)
        counts = np.diff(edit.lod0.starts)[strands]
        number, heads = np.repeat(np.arange(len(strands)), counts), np.cumsum(counts) - counts
        points = edit.positions[edit.lod0.rows(strands)].astype(np.float64) @ world[:3, :3].T + world[:3, 3]
        roots, root_rows = points[heads], np.zeros(len(points), bool)
        root_rows[heads] = True
        hit = np.zeros(len(strands), bool)
        if not len(strands):
            return hit, points
        low, high = np.minimum.reduceat(points, heads), np.maximum.reduceat(points, heads)
        for shape in self.placed():
            if not shape.tag & tag:
                continue
            box = bounds(shape)
            near = np.arange(len(strands)) if box is None else np.flatnonzero(
                np.all((low <= box[1] + margin) & (high >= box[0] - margin), axis=1))
            near = near[~_holds(roots[near], shape)]
            rows = ranges(heads[near], counts[near])
            inside, way = depth(points[rows], shape)
            caught = (inside > _SLACK - margin) & ~root_rows[rows]
            rows, owners = rows[caught], number[rows[caught]]
            hit[owners] = True
            points[rows] += way[caught] * (inside[caught] + margin)[:, None]
            np.minimum.at(low, owners, points[rows])
            np.maximum.at(high, owners, points[rows])
        return hit, (points - world[:3, 3]) @ np.linalg.inv(world[:3, :3]).T


def _holds(roots: np.ndarray, shape: CollisionShape) -> np.ndarray:
    box = bounds(shape)
    near = np.arange(len(roots)) if box is None else np.flatnonzero(np.all((roots >= box[0]) & (roots <= box[1]),
                                                                           axis=1))
    holds = np.zeros(len(roots), bool)
    holds[near] = depth(roots[near], shape)[0] > 0
    return holds


__all__ = ["EVERY_TAG", "SetColliders"]
