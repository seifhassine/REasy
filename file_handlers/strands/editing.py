"""See docs/internals/strand-editing.md."""
from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial

import numpy as np

from utils.native_build import ensure_fastmesh

from . import fitting, growing
from .bindings import StrandBindings
from .fitting import KEY_SPAN
from .strands_file import (NO_GUIDE, SEGMENT_FLAG_END, SEGMENT_FLAG_START, SEGMENT_VERTEX_MASK, VERTEX, StrandsBounds,
                           StrandsFile, StrandsLodLevel, guide_data, packed, unpacked)

_fastmesh = ensure_fastmesh()

_UNDO_STEPS = 64
_WINDOW, _ON_LINE = 3, 1e-6
# How far from its guides' blend a strand stands out in the game (m).
FAR = 0.05
# Rounding tolerance on length gains (m).
_LONGER = 1e-4


@dataclass(slots=True)
class _Level:
    """``starts`` holds each strand's first vertex, plus the end."""
    index: int
    vertices: np.ndarray
    starts: np.ndarray
    strand_of: np.ndarray
    guides: np.ndarray | None
    segments: np.ndarray

    def rows(self, strands: np.ndarray) -> np.ndarray:
        starts, counts = self.starts[strands], np.diff(self.starts)[strands]
        return ranges(starts, counts)


@dataclass(slots=True)
class _State:
    """``origin`` is each strand's number in the file as read; ``stale`` marks guide rows in-place edits may have
    outdated."""
    levels: list[_Level]
    root_uvs: np.ndarray
    groom: np.ndarray | None
    skin: tuple[np.ndarray, np.ndarray] | None
    tables: list[np.ndarray]
    origin: np.ndarray
    maps: list[_Map | None]
    fitted: np.ndarray
    guided: np.ndarray
    widths_changed: bool = False
    stale: bool = False
    sharpness: float = 1.0
    selected: np.ndarray | None = None


@dataclass(slots=True)
class _Rows:
    rows: list[np.ndarray]
    values: list[np.ndarray]
    widths_changed: bool


@dataclass(slots=True)
class _Map:
    """``share`` is the fraction along the LOD 0 ``segment``."""
    segment: np.ndarray
    share: np.ndarray
    offset: np.ndarray


@dataclass(slots=True)
class _Part:
    """``guides`` is None when the guide rows are still to be made."""
    counts: np.ndarray
    records: np.ndarray
    mapped: _Map | None
    guides: np.ndarray | None


@dataclass(slots=True)
class Held:
    strands: np.ndarray
    rows: np.ndarray
    points: np.ndarray
    weight: np.ndarray


@dataclass(slots=True)
class Grown:
    strands: np.ndarray
    positions: np.ndarray
    counts: np.ndarray
    revision: int
    state: _State | None = None
    conformed: int = 0


@dataclass(slots=True)
class Fitted:
    strands: np.ndarray
    revision: int
    state: _State | None = None


@dataclass(slots=True)
class Trimmed:
    strands: np.ndarray
    revision: int
    state: _State | None = None


@dataclass(slots=True)
class Reguided:
    strands: np.ndarray
    revision: int
    state: _State | None = None


class StrandEdit:
    """The first ``protected`` strands are the guides. ``spacing`` is each level's vertex spacing (m);
    ``guide_sharpness`` is the power new guide weights are raised to (1 matches shipped files)."""

    def __init__(self, strands: StrandsFile, binding: StrandBindings | None = None):
        if any(strands.lod_web_unique_vertex_counts) or any(strands.lod_web_connection_counts):
            raise ValueError("REasy does not edit strands files holding web-solver data")
        levels = sorted(strands.lods, key=lambda level: level.index)
        if not levels or levels[0].index != 0:
            raise ValueError("The strands file has no LOD 0")
        if binding is not None and any(len(indices) != strands.strand_count for indices, _ in binding.levels):
            raise ValueError("The binding does not bind the strands file's strands")
        self.source, self.binding = strands, binding
        self.protected = max(strands.guide_count, strands.secondary_guide_count)
        made = [_level(level) for level in levels]
        if any(len(level.starts) - 1 != strands.strand_count for level in made):
            raise ValueError("Every LOD of the strands file must hold every strand to be edited")
        extras = strands.guide_extras
        skin = None
        if extras is not None and len(extras.vertex_joint_indices):
            skin = (np.frombuffer(extras.vertex_joint_indices, "<u2").reshape(-1, 4).copy(),
                    np.frombuffer(extras.vertex_joint_weights, "<f4").reshape(-1, 4).copy())
        stride = 16 if binding is not None and binding.quantized else 20
        tables = [] if binding is None else [np.frombuffer(table, np.uint8).reshape(-1, stride).copy()
                                             for table in binding.tables]
        # Never changed in place.
        self._read = [_level(level) for level in levels]
        self._read_maps: list[_Map | None] = [None] + [_measured(self._read[0], read) for read in self._read[1:]]
        self._read_skin = skin
        self._state = _State(made, np.asarray(strands.root_uvs, np.float32).reshape(-1, 2).copy(),
                             np.asarray(strands.groom_groups, np.uint32) if strands.groom_groups else None, skin,
                             tables, np.arange(strands.strand_count), list(self._read_maps),
                             np.zeros(strands.strand_count, bool), np.zeros(strands.strand_count, bool))
        read0 = self._read[0]
        self.spacing = fitting.spacings(read0.vertices["position"], read0.starts,
                                        [np.diff(read.starts) for read in self._read])
        self._read_lengths = fitting.lengths(read0.vertices["position"], read0.starts)
        self._named = _named(self._read, strands.guide_count, self.protected)
        self.guide_sharpness = 1.0
        self.selected = np.zeros(strands.strand_count, bool)
        self.moved = np.zeros(0, np.int64)
        self.revision = 0
        self._undo: list = []
        self._redo: list = []
        self._stroke: dict | None = None

    @property
    def strand_count(self) -> int:
        return len(self._state.origin)

    @property
    def levels(self) -> Sequence[_Level]:
        return self._state.levels

    @property
    def lod0(self) -> _Level:
        return self._state.levels[0]

    @property
    def positions(self) -> np.ndarray:
        return self.lod0.vertices["position"]

    def roots(self, strands=None) -> np.ndarray:
        level = self.lod0
        starts = level.starts[:-1] if strands is None else level.starts[np.asarray(strands, np.int64)]
        return level.vertices["position"][starts]

    def shares(self, rows: np.ndarray, level: _Level | None = None) -> np.ndarray:
        return (level or self.lod0).vertices["root_tip"][rows].astype(np.float32) / 255

    def guide_mask(self) -> np.ndarray:
        mask = np.zeros(self.strand_count, bool)
        mask[:min(self.protected, self.strand_count)] = True
        return mask

    def read_counts(self) -> np.ndarray:
        return np.diff(self._read[0].starts)[self._state.origin]

    def moved_roots(self) -> np.ndarray:
        read = self._read[0]
        original = read.vertices["position"][read.starts[self._state.origin]]
        return np.flatnonzero(np.any(self.roots() != original, axis=1))

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> bool:
        return self._step(self._undo, self._redo)

    def redo(self) -> bool:
        return self._step(self._redo, self._undo)

    def _step(self, taken: list, kept: list) -> bool:
        if not taken:
            return False
        self.end_stroke()
        kept.append(self._swap(taken.pop()))
        self.revision += 1
        return True

    def _swap(self, step):
        if callable(step):
            return step()
        if isinstance(step, _State):
            now, self._state = self._state, step
            now.selected = self.selected.copy()
            self._carry_selection(now, step)
            return now
        back = _Rows([], [], self._state.widths_changed)
        for level, rows, values in zip(self._state.levels, step.rows, step.values):
            back.rows.append(rows)
            back.values.append(level.vertices[rows].copy())
            level.vertices[rows] = values
        self._state.widths_changed = step.widths_changed
        self._state.stale = True
        return back

    def record(self, step) -> None:
        """``step()`` undoes the edit and returns its redo."""
        self.end_stroke()
        self._push(step)

    def _push(self, step) -> None:
        self._undo.append(step)
        del self._undo[:-_UNDO_STEPS]
        self._redo.clear()
        self.revision += 1

    def begin_stroke(self) -> None:
        """Other LODs follow only when the stroke ends."""
        self.end_stroke()
        self._stroke = {"touched": np.zeros(self.strand_count, bool), "rows": [[] for _ in self.levels],
                        "values": [[] for _ in self.levels], "widths": self._state.widths_changed}

    def end_stroke(self) -> None:
        stroke, self._stroke = self._stroke, None
        if stroke is None or not stroke["touched"].any():
            return
        self._follow(np.flatnonzero(stroke["touched"]))
        self._push(_Rows([np.concatenate(rows) for rows in stroke["rows"]],
                         [np.concatenate(values) for values in stroke["values"]], stroke["widths"]))

    def _keep(self, strands: np.ndarray) -> None:
        strands = np.unique(np.asarray(strands, np.int64))
        if self._stroke is not None:
            touched = self._stroke["touched"]
            strands = strands[~touched[strands]]
            touched[strands] = True
            for level, rows, values in zip(self.levels, self._stroke["rows"], self._stroke["values"]):
                kept = level.rows(strands)
                rows.append(kept)
                values.append(level.vertices[kept].copy())
            return
        rows = [level.rows(strands) for level in self.levels]
        self._push(_Rows(rows, [level.vertices[kept].copy() for level, kept in zip(self.levels, rows)],
                         self._state.widths_changed))

    def move(self, vertices, positions, *, keep_lengths: bool = False) -> None:
        """``keep_lengths`` restores segment lengths from the root out."""
        vertices = np.ascontiguousarray(vertices, np.int64)
        if not len(vertices):
            return
        hit = np.zeros(self.strand_count, bool)
        hit[self.lod0.strand_of[vertices]] = True
        self._move(np.flatnonzero(hit), vertices, positions, keep_lengths)

    def _move(self, strands: np.ndarray, vertices: np.ndarray, positions, keep_lengths: bool) -> None:
        """``strands`` are the strands holding ``vertices``."""
        level = self.lod0
        self._keep(strands)
        positions = np.ascontiguousarray(positions, np.float32).reshape(-1, 3)
        if keep_lengths:
            _fastmesh.move_keeping_lengths(level.vertices, level.vertices.itemsize, level.starts, strands, vertices,
                                           positions)
        else:
            level.vertices["position"][vertices] = positions
        self._state.stale = True
        if self._stroke is None:
            self._follow(strands)
        self.moved = strands
        self.revision += 1

    def hold(self, strands) -> Held:
        strands = np.unique(np.asarray(strands, np.int64))
        rows = self.lod0.rows(strands)
        return Held(strands, rows, self.positions[rows].astype(np.float64), _smooth(self.shares(rows))[:, None])

    def transform(self, held: Held, matrix: np.ndarray, *, pin_roots: bool = False) -> None:
        """``matrix`` is the whole transform since the drag started, not a step."""
        if not len(held.strands):
            return
        matrix = np.asarray(matrix, np.float64)
        moved = held.points @ matrix[:3, :3].T
        moved += matrix[:3, 3]
        if pin_roots:
            moved -= held.points
            moved *= held.weight
            moved += held.points
        self._move(held.strands, held.rows, moved, pin_roots)

    def comb(self, vertices, deltas, *, keep_lengths: bool = True) -> None:
        vertices = np.asarray(vertices, np.int64)
        weight = _smooth(self.shares(vertices))[:, None]
        self.move(vertices, self.positions[vertices] + np.asarray(deltas, np.float32) * weight,
                  keep_lengths=keep_lengths)

    def smooth(self, vertices, amount) -> None:
        level, vertices = self.lod0, np.asarray(vertices, np.int64)
        amount = np.broadcast_to(np.asarray(amount, np.float32), vertices.shape)
        positions = level.vertices["position"]
        first = level.starts[level.strand_of[vertices]]
        last = level.starts[level.strand_of[vertices] + 1] - 1
        is_inner, is_tip = (vertices > first) & (vertices < last), (vertices == last) & (vertices - 2 >= first)
        inner, tips = vertices[is_inner], vertices[is_tip]
        target = positions[inner] + amount[is_inner][:, None] * (
            (positions[inner - 1] + positions[inner + 1]) / 2 - positions[inner])
        line = 2 * positions[tips - 1] - positions[tips - 2]
        tip_target = positions[tips] + amount[is_tip][:, None] * 0.5 * (line - positions[tips])
        self.move(np.r_[inner, tips], np.r_[target, tip_target], keep_lengths=True)

    def scale_lengths(self, strands, factor: float) -> None:
        strands = np.asarray(strands, np.int64)
        if not len(strands) or factor <= 0:
            return
        level = self.lod0
        rows = level.rows(strands)
        self.move(rows, _rescaled(level.vertices["position"][rows].astype(np.float64),
                                  np.diff(level.starts)[strands], factor))

    def growth(self, strands, world: np.ndarray, shapes, surface,
               settings: growing.GrowSettings) -> Callable[..., Grown]:
        """A job run off the edit; its result goes to ``put``."""
        strands = np.unique(np.asarray(strands, np.int64))
        state = self._state
        level = state.levels[0]
        rows = level.rows(strands)
        points, counts = level.vertices["position"][rows].astype(np.float64), np.diff(level.starts)[strands]
        roots, revision = self.roots().astype(np.float64), self.revision
        fly = growing.fly_aways(np.diff(self._read[0].starts))[state.origin[strands]]

        def grow(progress=None, cancelled=None) -> Grown:
            grew, grown_counts, positions = growing.grown(points, counts, roots, world, shapes, surface, settings,
                                                          self.spacing[0], progress, cancelled, fly)
            # The exact roots (the grow frame changes their low bits).
            positions[np.cumsum(grown_counts) - grown_counts] = points[(np.cumsum(counts) - counts)[grew]]
            positions = positions.astype(np.float32)
            made, conformed = None, 0
            if len(grew):
                made = self._fitted(state, strands[grew], (positions, grown_counts))[0]
                conformed = len(self._conformed(made, strands[grew]))
                # A strand given its neighbour's shape or its guides' blend may be longer than its own cut.
                made = self._fitted(made, strands[grew])[0]
                lod0 = made.levels[0]
                positions = lod0.vertices["position"][lod0.rows(strands[grew])]
                grown_counts = np.diff(lod0.starts)[strands[grew]]
            return Grown(strands[grew], positions, grown_counts, revision, made, conformed)
        return grow

    def trimming(self, strands, world: np.ndarray, settings: growing.GrowSettings) -> Callable[[], Trimmed]:
        """A job run off the edit; its result goes to ``put``."""
        strands = np.unique(np.asarray(strands, np.int64))
        state = self._state
        level = state.levels[0]
        rows = level.rows(strands)
        points, counts = level.vertices["position"][rows].astype(np.float64), np.diff(level.starts)[strands]
        roots, revision = self.roots().astype(np.float64), self.revision

        def trim() -> Trimmed:
            cut, kept, positions = growing.trimmed(points, counts, roots, world, settings, self.spacing[0])
            # The exact roots (the cut frame changes their low bits).
            positions[np.cumsum(kept) - kept] = points[(np.cumsum(counts) - counts)[cut]]
            made = None
            if len(cut):
                made = self._fitted(state, strands[cut], (positions.astype(np.float32), kept), trimmed=True)[0]
            return Trimmed(strands[cut], revision, made)
        return trim

    def fitting(self, strands) -> Callable[[], Fitted]:
        """A job run off the edit; its result goes to ``put``."""
        strands = np.unique(np.asarray(strands, np.int64))
        state, revision = self._state, self.revision

        def fit() -> Fitted:
            made, fitted = self._fitted(state, strands)
            return Fitted(fitted, revision, made if len(fitted) else None)
        return fit

    def reguiding(self, strands) -> Callable[[], Reguided]:
        """A job run off the edit; its result goes to ``put``."""
        strands = np.unique(np.asarray(strands, np.int64))
        state, revision = _copied(self._state), self.revision
        state.guided[strands] = True

        def guide() -> Reguided:
            self._guide(state)
            return Reguided(strands, revision, state)
        return guide

    def far_from_guides(self, limit: float = FAR) -> np.ndarray:
        state = self._state
        level = state.levels[0]
        if level.guides is None:
            return np.zeros(self.strand_count, bool)
        if state.stale or state.sharpness != self.guide_sharpness:
            self._guide(state)
        return fitting.gaps(level.vertices["position"], level.starts, level.guides, self.protected) > limit

    def put(self, made: Grown | Trimmed | Fitted | Reguided) -> bool:
        if made.revision != self.revision or self._stroke is not None:
            return False
        if made.state is not None:
            self._restate(made.state)
        return True

    def snap_roots(self, strands, surface) -> None:
        strands = np.asarray(strands, np.int64)
        if not len(strands):
            return
        roots = self.roots(strands).astype(np.float64)
        triangles, weights, _distance = surface.nearest(roots)
        delta = surface.point(triangles, weights) - roots
        level = self.lod0
        rows = level.rows(strands)
        self.move(rows, level.vertices["position"][rows] + np.repeat(delta, np.diff(level.starts)[strands], axis=0))

    def scale_widths(self, strands, root: float, tip: float = None) -> None:
        strands = np.asarray(strands, np.int64)
        if not len(strands):
            return
        tip = root if tip is None else tip
        self._keep(strands)
        for level in self.levels:
            rows = level.rows(strands)
            share = self.shares(rows, level)
            widths = level.vertices["width"][rows] * (root + (tip - root) * share)
            level.vertices["width"][rows] = np.clip(np.rint(widths), 0, 65535).astype(np.uint16)
        self._state.widths_changed = True
        self.revision += 1

    def reset(self, strands) -> None:
        strands = np.unique(np.asarray(strands, np.int64))
        if not len(strands):
            return
        if self._state.fitted[strands].any() or self._state.guided[strands].any():
            self.end_stroke()
            self._restate(self._restored(self._state, strands))
            return
        self._keep(strands)
        source = self._state.origin[strands]
        for level, read in zip(self.levels, self._read):
            level.vertices[level.rows(strands)] = read.vertices[read.rows(source)]
        self._state.stale = True
        self.revision += 1

    def delete(self, strands) -> int:
        gone = np.zeros(self.strand_count, bool)
        gone[np.asarray(strands, np.int64)] = True
        gone[:self.protected] = False
        if not gone.any():
            return 0
        self.end_stroke()
        before = self._state
        before.selected = self.selected.copy()
        keep = ~gone
        levels, maps = [], []
        for level, mapped in zip(before.levels, before.maps):
            kept_vertices = keep[level.strand_of]
            maps.append(None if mapped is None else _Map(mapped.segment[kept_vertices], mapped.share[kept_vertices],
                                                         mapped.offset[kept_vertices]))
            counts = np.diff(level.starts)[keep]
            starts = np.r_[0, np.cumsum(counts)]
            renumber = np.cumsum(kept_vertices) - 1
            first = level.segments & SEGMENT_VERTEX_MASK
            kept_segments = kept_vertices[first]
            segments = (level.segments[kept_segments] & ~np.uint32(SEGMENT_VERTEX_MASK)) | renumber[
                first[kept_segments]].astype(np.uint32)
            levels.append(_Level(level.index, level.vertices[kept_vertices].copy(), starts,
                                 np.repeat(np.arange(len(counts), dtype=np.int64), counts),
                                 None if level.guides is None else level.guides[kept_vertices], segments))
        skin = before.skin
        if skin is not None:
            kept = keep[before.levels[0].strand_of]
            skin = (skin[0][kept], skin[1][kept])
        self._state = _State(levels, before.root_uvs[keep], None if before.groom is None else before.groom[keep],
                             skin, [table[keep] for table in before.tables], before.origin[keep], maps,
                             before.fitted[keep], before.guided[keep], before.widths_changed, before.stale,
                             before.sharpness)
        self._carry_selection(before, self._state)
        self._push(before)
        return int(gone.sum())

    def _restate(self, state: _State) -> None:
        """``state`` must hold the same strands."""
        before = self._state
        before.selected = self.selected.copy()
        self._state = state
        self._push(before)
        self.moved = np.zeros(0, np.int64)

    def _carry_selection(self, before: _State, after: _State) -> None:
        if len(self.selected) == len(after.origin):
            return
        where = np.full(self.source.strand_count, -1, np.int64)
        where[before.origin] = np.arange(len(before.origin))
        strands = where[after.origin]
        present = np.flatnonzero(strands >= 0)
        selected = np.zeros(len(after.origin), bool) if after.selected is None else after.selected.copy()
        selected[present] = self.selected[strands[present]]
        self.selected = selected

    def _follow(self, strands: np.ndarray) -> None:
        _followed(self._state, strands)

    def _fitted(self, state: _State, strands: np.ndarray, shapes=None, trimmed: bool = False) -> tuple[_State,
                                                                                                       np.ndarray]:
        """Must not modify ``state``: jobs run it off the edit."""
        levels = state.levels
        lod0 = levels[0]
        if shapes is None:
            line_counts = np.diff(lod0.starts)[strands]
            lines = lod0.vertices["position"][lod0.rows(strands)]
        else:
            lines, line_counts = shapes
        line_starts = np.r_[0, np.cumsum(line_counts)]
        length = fitting.lengths(lines, line_starts)
        had = [np.diff(level.starts)[strands] for level in levels]
        rule = fitting.trimmed if trimmed else fitting.counts
        need = [rule(length, count, spacing) for count, spacing in zip(had, self.spacing)]
        if shapes is None:
            more = np.flatnonzero(np.any([n > h for n, h in zip(need, had)], axis=0))
            if not len(more):
                return state, strands[more]
            if len(more) < len(strands):
                strands, length = strands[more], length[more]
                had, need = [h[more] for h in had], [n[more] for n in need]
                line_counts = had[0]
                lines = lod0.vertices["position"][lod0.rows(strands)]
                line_starts = np.r_[0, np.cumsum(line_counts)]
            sampled = need[0] > had[0]
            positions = lines.copy()
            if sampled.any():
                at = ranges(line_starts[:-1][sampled], line_counts[sampled])
                resampled = fitting.placed(lines[at], np.r_[0, np.cumsum(line_counts[sampled])], need[0][sampled])[0]
                counts0 = np.where(sampled, need[0], line_counts)
                positions = np.empty((int(counts0.sum()), 3), np.float32)
                starts0 = np.r_[0, np.cumsum(counts0)]
                positions[ranges(starts0[:-1][sampled], need[0][sampled])] = resampled
                positions[ranges(starts0[:-1][~sampled], line_counts[~sampled])] = lines[
                    ranges(line_starts[:-1][~sampled], line_counts[~sampled])]
                line_counts, line_starts = counts0, starts0
            old = lod0.vertices[lod0.rows(strands)]
            records = _records(old, had[0], line_counts, positions)
            kept = np.repeat(~sampled, line_counts)
            records[kept] = old[np.repeat(~sampled, had[0])]
        else:
            records = _records(lod0.vertices[lod0.rows(strands)], had[0], line_counts, lines)
            positions = lines
        parts = [_Part(line_counts, records, None, None)]
        for level, count in zip(levels[1:], need[1:]):
            placed, segment, share = fitting.placed(positions, line_starts, count)
            parts.append(_Part(count, _records(level.vertices[level.rows(strands)], np.diff(level.starts)[strands],
                                               count, placed), _Map(segment, share, np.zeros((len(segment), 3))),
                               None))
        skin = None
        if state.skin is not None:
            at = _nearest_by_share(lod0.starts[strands], had[0], line_counts)
            skin = (state.skin[0][at], state.skin[1][at])
        made = self._spliced(state, strands, parts, skin)
        made.fitted[strands] = True
        self._guide(made)
        return made, strands

    def _restored(self, state: _State, strands: np.ndarray) -> _State:
        source = state.origin[strands]
        parts = []
        for read, mapped in zip(self._read, self._read_maps):
            rows = read.rows(source)
            parts.append(_Part(np.diff(read.starts)[source], read.vertices[rows].copy(),
                               None if mapped is None else _Map(mapped.segment[rows], mapped.share[rows],
                                                                mapped.offset[rows]),
                               None if read.guides is None else read.guides[rows]))
        skin = None
        if self._read_skin is not None:
            rows = self._read[0].rows(source)
            skin = (self._read_skin[0][rows], self._read_skin[1][rows])
        made = self._spliced(state, strands, parts, skin)
        made.fitted[strands] = made.guided[strands] = False
        self._guide(made)
        return made

    def _spliced(self, state: _State, strands: np.ndarray, parts: list[_Part], skin) -> _State:
        kept = np.ones(len(state.origin), bool)
        kept[strands] = False
        kept = np.flatnonzero(kept)
        levels, maps, splices = [], [], []
        for level, mapped, part in zip(state.levels, state.maps, parts):
            counts = np.diff(level.starts)
            counts[strands] = part.counts
            starts = np.r_[0, np.cumsum(counts)]
            splice = _Splice(ranges(level.starts[kept], counts[kept]), ranges(starts[kept], counts[kept]),
                             ranges(starts[strands], part.counts), int(starts[-1]))
            splices.append(splice)
            guides = None
            if level.guides is not None:
                guides = splice(level.guides, 0 if part.guides is None else part.guides)
            levels.append(_Level(level.index, splice(level.vertices, part.records), starts,
                                 np.repeat(np.arange(len(counts), dtype=np.int64), counts), guides, _segments(starts)))
            maps.append(None if mapped is None else _Map(splice(mapped.segment, part.mapped.segment),
                                                         splice(mapped.share, part.mapped.share),
                                                         splice(mapped.offset, part.mapped.offset)))
        if state.skin is not None:
            skin = tuple(splices[0](old, new) for old, new in zip(state.skin, skin))
        return _State(levels, state.root_uvs, state.groom, skin, state.tables, state.origin, maps, state.fitted.copy(),
                      state.guided.copy(), state.widths_changed, state.stale, state.sharpness)

    def _guide(self, state: _State) -> None:
        """Safe to run off the edit."""
        state.stale, state.sharpness = False, self.guide_sharpness
        read = next((level for level in self._read if level.guides is not None), None)
        if read is None:
            return
        lod0, read0, origin = state.levels[0], self._read[0], state.origin
        changed = state.fitted.copy()
        same = np.flatnonzero(~changed)
        rows = lod0.rows(same)
        moved = np.any(lod0.vertices["position"][rows] != read0.vertices["position"][read0.rows(origin[same])], axis=1)
        changed[lod0.strand_of[rows[moved]]] = True
        names = unpacked(read.guides[read.starts[origin]])[0]
        used = names != NO_GUIDE
        anew = state.guided | changed | (used & changed[np.where(used, names, 0)]).any(1)
        strands, kept = np.flatnonzero(anew), np.flatnonzero(~anew)
        guides, weights = self._chosen(state, strands, names[strands])
        for level, read_level in zip(state.levels, self._read):
            if level.guides is None:
                continue
            level.guides[level.rows(kept)] = read_level.guides[read_level.rows(origin[kept])]
            if not len(strands):
                continue
            rows = level.rows(strands)
            number = np.repeat(np.arange(len(strands)), np.diff(level.starts)[strands])
            have = fitting.fractions(level.vertices["position"], level.starts)
            keys = fitting.keyed(have, level.starts)
            offsets, shares = np.zeros((len(rows), 3), np.int64), np.zeros((len(rows), 3))
            for slot in range(3):
                guide = guides[number, slot]
                on = guide != NO_GUIDE
                offsets[on, slot], shares[on, slot] = fitting.places(have, keys, level.starts, rows[on], guide[on])
            level.guides[rows] = packed(guides[number], offsets, weights[number], shares)

    def _chosen(self, state: _State, strands: np.ndarray, names: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        guides, weights = names.copy(), np.zeros((len(strands), 3))
        primaries, secondaries = self.source.guide_count, self.protected
        lod0 = state.levels[0]
        own = strands < primaries
        guides[own, 0], weights[own, 0] = strands[own], 1
        for these, pool, count in ((~own & (strands < secondaries), primaries, self._named[0]),
                                   (strands >= secondaries, secondaries, self._named[1])):
            guides[these], weights[these] = fitting.chosen(lod0.vertices["position"], lod0.starts, strands[these],
                                                           np.arange(pool), count, sharpness=state.sharpness)
        return guides, weights

    def _conformed(self, state: _State, strands: np.ndarray) -> np.ndarray:
        """The game's chain places strands straying past ``FAR`` by their guides."""
        lod0 = state.levels[0]
        grown = np.zeros(len(state.origin), bool)
        grown[strands] = True
        strands = strands[strands >= self.protected]
        if lod0.guides is None or not len(strands):
            return strands[:0]
        positions = lod0.vertices["position"]
        guides, _offsets, weights, _shares = unpacked(lod0.guides[lod0.starts[strands]])
        used = (guides != NO_GUIDE) & (weights > 0)
        with_guides = np.all(~used | grown[np.where(used, guides, 0)], axis=1) & used.any(1)
        far = fitting.gaps(positions, lod0.starts, lod0.guides, self.protected, rooted=True)[strands] > FAR
        astray = strands[far & with_guides]
        if len(astray):
            blend = fitting.blends(positions, lod0.starts, lod0.guides)[0]
            rows = lod0.rows(astray)
            roots = np.repeat(lod0.starts[astray], np.diff(lod0.starts)[astray])
            positions[rows] = (blend[rows] - blend[roots]) + positions[roots]
            _followed(state, astray)
            self._guide(state)
        return astray

    def writing(self) -> Callable[[], StrandsFile]:
        return partial(self._file, _copied(self._state))

    def _file(self, state: _State) -> StrandsFile:
        state = self._outgrown(state)
        if state.stale or state.sharpness != self.guide_sharpness:
            self._guide(state)
        source = self.source
        planar = source.lods[0].planar_guides
        levels = []
        for level in state.levels:
            levels.append(StrandsLodLevel(level.index, len(level.vertices), len(level.segments),
                                          len(level.starts) - 1, level.vertices.tobytes(), level.segments.tobytes(),
                                          tuple(int(start) for start in level.starts[:-1]),
                                          level.strand_of.astype("<u4").tobytes(), guide_data(level.guides, planar),
                                          planar))
        out = dataclasses.replace(source, strand_count=len(state.origin), lods=levels,
                                  root_uvs=tuple(map(tuple, state.root_uvs.tolist())),
                                  groom_groups=() if state.groom is None else tuple(state.groom.tolist()))
        positions = state.levels[0].vertices["position"]
        read = self._read[0].vertices["position"]
        if len(positions) != len(read) or not np.array_equal(positions, read):
            low, high = positions.min(0).astype(np.float32), positions.max(0).astype(np.float32)
            out.bounds = StrandsBounds(tuple(map(float, low)), tuple(map(float, high)))
            if source.movement_bounds is not None:
                held, was = source.movement_bounds, source.bounds
                below = np.asarray(was.min, np.float32) - np.asarray(held.min, np.float32)
                above = np.asarray(held.max, np.float32) - np.asarray(was.max, np.float32)
                out.movement_bounds = StrandsBounds(
                    tuple(map(float, np.minimum(np.asarray(held.min, np.float32), low - below))),
                    tuple(map(float, np.maximum(np.asarray(held.max, np.float32), high + above))))
        if state.widths_changed:
            widths = state.levels[0].vertices["width"].astype(np.float64) / 65535
            out.strand_width_min, out.strand_width_max, out.strand_width_ave = (
                float(np.float32(widths.min())), float(np.float32(widths.max())), float(np.float32(widths.mean())))
        extras = source.guide_extras
        if extras is not None and state.skin is not None:
            out.guide_extras = dataclasses.replace(extras, vertex_joint_indices=state.skin[0].tobytes(),
                                                   vertex_joint_weights=state.skin[1].tobytes())
        out.lod_guide_vertex_counts = _guide_counts(source.lod_guide_vertex_counts, source.guide_count,
                                                    state.levels)
        out.lod_secondary_guide_vertex_counts = _guide_counts(source.lod_secondary_guide_vertex_counts,
                                                              source.secondary_guide_count, state.levels)
        return out

    def _outgrown(self, state: _State) -> _State:
        lod0 = state.levels[0]
        longer = fitting.lengths(lod0.vertices["position"], lod0.starts) > self._read_lengths[state.origin] + _LONGER
        return self._fitted(state, np.flatnonzero(longer))[0] if longer.any() else state

    @property
    def groom_groups(self) -> np.ndarray | None:
        return self._state.groom

    def binding_base(self) -> StrandBindings | None:
        if self.binding is None or self.strand_count == self.source.strand_count:
            return self.binding
        return self.binding.with_tables(table.tobytes() for table in self._state.tables)

def _level(level: StrandsLodLevel) -> _Level:
    vertices = np.frombuffer(level.vertex_records, VERTEX).copy()
    starts = np.r_[np.asarray(level.strand_offsets, np.int64), level.vertex_count]
    guides = level.guide_rows().copy() if level.closest_guide_data else None
    return _Level(level.index, vertices, starts, np.frombuffer(level.vertex_strands, "<u4").astype(np.int64),
                  guides, np.frombuffer(level.segment_records, "<u4").copy())


def _copied(state: _State) -> _State:
    """Copies only what edits change in place."""
    levels = [dataclasses.replace(level, vertices=level.vertices.copy(),
                                  guides=None if level.guides is None else level.guides.copy())
              for level in state.levels]
    return dataclasses.replace(state, levels=levels, guided=state.guided.copy())


def _followed(state: _State, strands: np.ndarray) -> None:
    lod0 = state.levels[0]
    p = lod0.vertices["position"]
    for level, mapped in zip(state.levels[1:], state.maps[1:]):
        rows = level.rows(strands)
        strand = level.strand_of[rows]
        first = lod0.starts[strand] + mapped.segment[rows]
        following = np.minimum(first + 1, lod0.starts[strand + 1] - 1)
        level.vertices["position"][rows] = fitting.on(p, first, following, mapped.share[rows], mapped.offset[rows])


def _measured(lod0: _Level, level: _Level) -> _Map:
    strand = level.strand_of
    kept = level.vertices["position"].astype(np.float64)
    along = _along(level)
    starts, counts = lod0.starts[strand], np.diff(lod0.starts)[strand]
    segments = np.maximum(counts - 1, 1)
    mapped = _Map(np.zeros(len(strand), np.int64), np.zeros(len(strand)), np.zeros((len(strand), 3)))
    middle = np.searchsorted(lod0.strand_of + _along(lod0) * KEY_SPAN, strand + along * KEY_SPAN) - starts - 1
    window = np.arange(-_WINDOW, _WINDOW + 1)
    local = np.clip(middle[:, None] + window, 0, segments[:, None] - 1)
    rows = np.arange(len(strand))
    off = np.flatnonzero(_nearest(lod0, mapped, rows, local, kept, along, starts, counts, segments) > _ON_LINE)
    if len(off):
        longest = int(segments[off].max())
        step = max(1, (1 << 20) // longest)
        for begin in range(0, len(off), step):
            part = off[begin:begin + step]
            _nearest(lod0, mapped, part, np.minimum(np.arange(longest), segments[part][:, None] - 1), kept[part],
                     along[part], starts[part], counts[part], segments[part])
    return mapped


def _nearest(lod0: _Level, mapped: _Map, rows, local, kept, along, starts, counts, segments) -> np.ndarray:
    polyline = lod0.vertices["position"]
    a_index = starts[:, None] + local
    b_index = np.minimum(a_index + 1, (starts + counts - 1)[:, None])
    a, b = polyline[a_index].astype(np.float64), polyline[b_index].astype(np.float64)
    point = kept[:, None]
    ab = b - a
    length = (ab * ab).sum(2)
    share = np.clip(np.divide(((point - a) * ab).sum(2), length, out=np.zeros(length.shape), where=length > 0), 0, 1)
    near = a + ab * share[..., None]
    distance = np.linalg.norm(point - near, axis=2)
    further = np.abs((local + share) / segments[:, None] - along[:, None])
    nearest = distance.min(1)
    best = np.where(distance == nearest[:, None], further, np.inf).argmin(1)
    line = np.arange(len(rows))
    mapped.segment[rows], mapped.share[rows] = local[line, best], share[line, best]
    mapped.offset[rows] = point[:, 0] - near[line, best]
    return nearest


def _along(level: _Level) -> np.ndarray:
    return fitting.fractions(level.vertices["position"], level.starts)


@dataclass(frozen=True, slots=True)
class _Splice:
    kept: np.ndarray
    moved: np.ndarray
    put: np.ndarray
    size: int

    def __call__(self, old: np.ndarray, new) -> np.ndarray:
        out = np.zeros((self.size, *old.shape[1:]), old.dtype)
        out[self.moved] = old[self.kept]
        out[self.put] = new
        return out


def _segments(starts: np.ndarray) -> np.ndarray:
    counts = np.diff(starts)
    first = ranges(starts[:-1], np.maximum(counts - 1, 0))
    strand = np.repeat(np.arange(len(counts)), np.maximum(counts - 1, 0))
    out = first.astype(np.uint32)
    out[first == starts[strand]] |= np.uint32(SEGMENT_FLAG_START)
    out[first + 2 == starts[strand + 1]] |= np.uint32(SEGMENT_FLAG_END)
    return out


def _records(old: np.ndarray, had: np.ndarray, count: np.ndarray, positions: np.ndarray) -> np.ndarray:
    line, number, share = fitting.shares(count)
    old_line, _old_number, old_share = fitting.shares(had)
    first = np.cumsum(had) - had
    found = np.searchsorted(old_line + old_share * KEY_SPAN, line + share * KEY_SPAN, "right") - 1
    a = np.clip(found, first[line], first[line] + np.maximum(had - 2, 0)[line])
    b = np.minimum(a + 1, first[line] + had[line] - 1)
    span = old_share[b] - old_share[a]
    blend = np.clip(np.divide(share - old_share[a], span, out=np.zeros(len(line)), where=span > 0), 0, 1)
    widths = old["width"][a] * (1 - blend) + old["width"][b] * blend
    out = np.zeros(len(line), VERTEX)
    out["position"] = positions
    out["width"] = np.clip(np.rint(widths), 0, 65535)
    out["root_tip"] = np.where(count[line] > 1, 255 * number // np.maximum(count - 1, 1)[line], 0)
    out["random"] = old["random"][first[line]]
    return out


def _nearest_by_share(first: np.ndarray, had: np.ndarray, count: np.ndarray) -> np.ndarray:
    line, _number, share = fitting.shares(count)
    return first[line] + np.rint(share * (had - 1)[line]).astype(np.int64)


def _named(levels: Sequence[_Level], guides: int, secondaries: int) -> tuple[int, int]:
    level = next((level for level in levels if level.guides is not None), None)
    out = []
    for low, high in ((guides, secondaries), (secondaries, len(levels[0].starts) - 1)):
        if level is None or high <= low:
            out.append(3)
            continue
        named = (unpacked(level.guides[level.starts[low:high]])[0] != NO_GUIDE).sum(1)
        out.append(int(np.bincount(named).argmax()))
    return out[0], out[1]


def _guide_counts(held: Sequence[int], guides: int, levels) -> tuple[int, ...]:
    out = list(held)
    for level in levels:
        out[level.index] = int(level.starts[min(guides, len(level.starts) - 1)])
    return tuple(out)


def _smooth(share: np.ndarray) -> np.ndarray:
    share = np.clip(share, 0, 1)
    return share * share * (3 - 2 * share) * 0.5 + share * 0.5


def _rescaled(points: np.ndarray, counts: np.ndarray, factor: float) -> np.ndarray:
    heads = np.cumsum(counts) - counts
    tails = heads + counts - 1
    line = np.repeat(np.arange(len(counts)), counts)
    grid = heads[:, None] + np.minimum(np.arange(int(counts.max(initial=1))), counts[:, None] - 1)
    distances = np.zeros(grid.shape)
    distances[:, 1:] = np.cumsum(np.linalg.norm(np.diff(points[grid], axis=1), axis=2), axis=1)
    along, total = distances[line, np.arange(len(points)) - heads[line]], distances[:, -1]
    wanted = along * factor
    # Offsets lay the polylines end to end so one ``searchsorted`` finds every vertex's segment.
    offset = (np.cumsum(total + 1) - (total + 1))[line]
    first, last = heads[line], np.maximum(tails[line] - 1, heads[line])
    start = np.clip(np.searchsorted(along + offset, wanted + offset, "right") - 1, first, last)
    start += (start < last) & (along[np.minimum(start + 1, tails[line])] <= wanted)
    start -= (start > first) & (along[start] > wanted)
    following = np.minimum(start + 1, tails[line])
    with np.errstate(divide="ignore", invalid="ignore"):
        slope = (points[following] - points[start]) / (along[following] - along[start])[:, None]
        out = slope * (wanted - along[start])[:, None] + points[start]
    end = wanted >= total[line]
    out[end] = points[tails[line[end]]]
    tip = points[tails] - points[np.maximum(tails - 1, heads)]
    norm = np.linalg.norm(tip, axis=1)
    direction = np.divide(tip, norm[:, None], out=np.zeros_like(tip), where=norm[:, None] > 0)
    beyond = wanted > total[line]
    out[beyond] = points[tails[line[beyond]]] + direction[line[beyond]] * (wanted - total[line])[beyond][:, None]
    still = (total <= 0)[line]
    out[still] = points[still]
    return out


def ranges(starts: np.ndarray, counts: np.ndarray) -> np.ndarray:
    starts, counts = np.asarray(starts, np.int64), np.asarray(counts, np.int64)
    before = np.cumsum(counts) - counts
    return np.arange(int(counts.sum()), dtype=np.int64) + np.repeat(starts - before, counts)


__all__ = ["FAR", "Fitted", "Grown", "Reguided", "StrandEdit", "Trimmed", "ranges"]
