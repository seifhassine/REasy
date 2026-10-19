"""See docs/internals/strand-editing.md."""
from __future__ import annotations

import os
from collections.abc import Callable, Sequence
lazy from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

lazy import numpy as np

lazy from file_handlers.clsp.clsp_file import CollisionShape, ShapeKind
lazy from file_handlers.clsp.geometry import box_axes, capsule_ends, bounds, depth
from utils.native_build import ensure_fastmesh, ensure_fastsdftex

from . import fitting

_fastmesh, _baker = ensure_fastmesh(), ensure_fastsdftex()

LENGTH, HEIGHT = "length", "height"
# Drape chain points (kept, grown), step limit, growing steps and starting length share.
_KEPT, _GROWN = 8, 28
_STEPS, _GROWING, _START = 1500, 130, 0.02
# Grown point spacing, fall per step and push limit per step (m).
_SPREAD, _GRAVITY, _REACH = 0.0005, 0.0025, 0.01
# Smoothing per step, and the kept part's hold on its styled shape.
_SMOOTH, _HOLD = 0.25, 0.85
# Hold-off past colliders (m): base, growth with hang, cap; and the nape's.
_BASE, _THICKENS, _THICKEST, _LOWER, _TAPER = 0.004, 0.12, 0.026, 0.3, 0.03
# Per-strand depth: hold-off scale range, extra depth and its ramp, and the hold-off cap.
_SCALES, _EXTRA, _DEEPENS = (0.7, 1.5), 0.010, 0.03
_ROOM = (_BASE + _THICKEST) * _SCALES[1] + _EXTRA
# Shoulders: inner share kept, slide per step, front-slide limit and contact distance.
_SHOULDER, _SLIDE, _SLIDE_FRONT, _ON_SHOULDER = 0.55, 0.0012, 0.03, 0.04
_FACE = 0.75
_FLY, _FLY_KEEP = 0.5, 0.02
# Combing the hair off the face; see docs/internals/strand-editing.md.
_FRONT, _FRONT_TURN, _FRONT_KEEP, _TUCK_TURN, _TUCK_SIDE = 0.2, 65.0, 0.015, 121.0, 0.3
_ON_HEAD, _COMB, _COMB_BACK, _COMB_LOW, _COMB_REAR = 0.03, 1.3, 0.8, 0.01, 0.93
_PART_BACK, _PART_LEAVE, _PART_RAMP, _PART_MIX = 1.6, 0.2, 0.05, 0.25
# Drawing the back hair toward the middle, against the seam the game's chain opens.
_BACK_BEHIND, _BACK_LOW, _BACK_CLOSE, _BACK_RAMP = 1.18, 0.14, 0.12, 0.15
_RISING, _FRINGE_FRONT, _FRINGE_TURN, _CHEEKS, _FRINGE_UNEVEN = 1.6, 0.58, 40.0, 0.42, 0.6
_CAP, _CAP_PLUS, _HOOK = 1.4, 0.08, 0.005
_SETTLE, _WINDOW, _CALM, _FOLD = 1e-3, 50, 2, 120.0
_BEND, _EASES = 90.0, 40
_ALONG, _MASS, _SPARSE, _MODELS = 0.01, 0.015, 0.01, 32
_CELL, _COARSE, _BLOCK, _PIECE = 0.0025, 4, 4, 0.01
_SAMPLES = 8192

# fastmesh.drape_strands' params, in order.
_PARAMS = ("kept", "steps", "growing", "start", "gravity_x", "gravity_y", "gravity_z", "smooth", "reach", "slide",
           "slide_front", "on_shoulder", "base", "thickens", "thickest", "deepens", "lower", "taper", "nape", "part",
           "face_middle", "face_side", "face_low", "face_high", "face_front", "comb", "on_head", "comb_low",
           "comb_rear", "comb_leave", "grid_x", "grid_y", "grid_z", "origin_x", "origin_y", "origin_z", "cell", "floor",
           "settle", "window", "calm", "fold", "block")
# A drape shape row (``DRAPE_WIDTH``), its kinds and the shoulder flag column.
_WIDTH, _CAPSULE, _BOX, _PLANE, _SLIDES = 24, 0, 1, 2, 23


class GrowCancelled(Exception):
    pass


@dataclass(frozen=True, slots=True)
class GrowSettings:
    """In m: ``target`` is the strand length (LENGTH) or the height above the mesh origin (HEIGHT); ``keep`` is the
    styled length kept."""
    mode: str = LENGTH
    target: float = 0.4
    keep: float = 0.05
    uneven: float = 0.018
    part: bool = True
    rise: float = 0.0
    fringe: float | None = None
    comb: bool = True
    centre_part: bool = False
    part_mix: bool = True
    close_back: bool = False


@dataclass(frozen=True, slots=True)
class Head:
    middle: tuple[float, float]
    half: float
    lowest: float
    highest: float
    hairline: float
    jaw: float

    @classmethod
    def of(cls, roots: np.ndarray) -> Head:
        low, high = roots.min(0), roots.max(0)
        middle, half = (low + high) / 2, max(float(high[0] - low[0]) / 2, 1e-3)
        front = roots[(np.abs(roots[:, 0] - middle[0]) < half / 2) & (roots[:, 2] > middle[2])]
        hairline = float(front[:, 1].min()) if len(front) else float(low[1])
        return cls((float(middle[0]), float(middle[2])), half, float(low[1]), float(high[1]), hairline,
                   hairline - 2 * half)

    @property
    def cheekbones(self) -> float:
        return self.jaw + _CHEEKS * (self.hairline - self.jaw)


class Frame:
    """``to``/``back`` act on row vectors."""

    def __init__(self, world: np.ndarray):
        world = np.asarray(world, np.float64)
        up = np.array((0.0, 1.0, 0.0))
        forward = world[:3, 2] - (world[:3, 2] @ up) * up
        if np.linalg.norm(forward) < 1e-6:
            forward = np.array((0.0, 0.0, 1.0))
        forward /= np.linalg.norm(forward)
        self.turn = np.array((np.cross(up, forward), up, forward))
        self.origin = world[:3, 3]
        self.to = (self.turn @ world[:3, :3]).T
        self.back = np.linalg.inv(self.to)

    def from_world(self, points: np.ndarray) -> np.ndarray:
        return (np.asarray(points, np.float64) - self.origin) @ self.turn.T

    def to_world(self, points: np.ndarray) -> np.ndarray:
        return np.asarray(points, np.float64) @ self.turn + self.origin


def default_height(roots: np.ndarray, world: np.ndarray) -> float:
    """Height to grow down to (m)."""
    return Head.of(np.asarray(roots, np.float64) @ Frame(world).to).jaw - 0.2


def default_fringe(roots: np.ndarray, world: np.ndarray) -> float:
    return Head.of(np.asarray(roots, np.float64) @ Frame(world).to).cheekbones


def grown(points: np.ndarray, counts: np.ndarray, roots: np.ndarray, world: np.ndarray,
          shapes: Sequence[CollisionShape], surface, settings: GrowSettings, spacing: float,
          progress: Callable[[int, int], None] | None = None, cancelled: Callable[[], bool] | None = None,
          fly: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``points`` are in file space; ``roots`` are all the set's roots; ``shapes`` the placed colliders; ``surface`` the
    binding mesh's LOD 0 or None."""
    _check(settings)
    frame, work = Frame(world), _Work(progress, cancelled)
    points, counts = np.asarray(points, np.float64) @ frame.to, np.asarray(counts, np.int64)
    head = Head.of(np.asarray(roots, np.float64) @ frame.to)
    heads = np.cumsum(counts) - counts
    lengths, bases = fitting.lengths(points, np.r_[0, np.cumsum(counts)]), points[heads]
    ends = _targets(bases, head, settings)
    if settings.mode == HEIGHT:
        tips = ends
        caps = _CAP * (bases[:, 1] - tips) + _CAP_PLUS
        wanted = np.where(bases[:, 1] > tips, caps, 0)
    else:
        wanted = ends
    reach = np.maximum(wanted, lengths)
    growing = np.flatnonzero((reach > lengths + 1e-4) & (counts > 1) & (lengths > 0))
    if not len(growing):
        return growing, np.zeros(0, np.int64), np.zeros((0, 3))
    grid = None
    if surface is not None:
        grid = _grid(np.asarray(surface.positions, np.float64) @ frame.to, np.asarray(surface.triangles, np.int64),
                     head.jaw, work)
    work.tell(0.1)
    styled = np.minimum(lengths[growing], settings.keep)
    swept = _at(points, counts, heads, growing, styled[:, None])[:, 0, 0]
    sides = _sides(swept, bases[growing, 0], head.middle[0])
    if settings.centre_part:
        sides = _centred(bases[growing], head, sides, settings.part_mix)
    combed, ways = (_combs(bases[growing], head, sides, settings.centre_part) if settings.comb
                    else (np.zeros(len(growing), bool), np.zeros((len(growing), 3))))
    flies = np.zeros(len(growing), bool) if fly is None else np.asarray(fly, bool)[growing]
    keep = np.minimum(styled, np.where(combed, _FRONT_KEEP, np.where(flies, _FLY_KEEP, np.inf)))
    chains = _chains(points, counts, heads, growing, keep)
    strands = np.column_stack((keep / (_KEPT - 1), (reach[growing] - keep) / _GROWN, sides, *_depths(bases[growing]),
                               ways))
    rows, kept = _rows(shapes, frame, head)
    use = _use(rows, kept, shapes, frame, bases[growing], reach[growing] + _ROOM)
    _drape(chains, strands, use, rows, grid, head, settings, work)
    if settings.close_back:
        _back_closed(chains, head)
    if settings.mode == HEIGHT:
        cut, turned = _cut(chains, lengths[growing], tips=tips[growing], caps=caps[growing])
    else:
        cut, turned = _cut(chains, lengths[growing], targets=wanted[growing])
    grew = cut > lengths[growing] + 1e-4
    if np.count_nonzero(grew) <= _MODELS:
        # Too few strands for turned-back ones to borrow a shape from.
        grew &= ~turned
    growing, chains, cut, turned = growing[grew], chains[grew], cut[grew], turned[grew]
    fitted = fitting.counts(cut, counts[growing], spacing)
    shaped = _resampled(chains, cut, fitted)
    shaped = _eased(shaped, fitted)
    shaped = _tidied(shaped, fitted, _through(shaped, fitted, shapes, frame) | turned | _bent(shaped, fitted))
    work.tell(1.0)
    return growing, fitted, shaped @ frame.back


def trimmed(points: np.ndarray, counts: np.ndarray, roots: np.ndarray, world: np.ndarray, settings: GrowSettings,
            spacing: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    _check(settings)
    frame = Frame(world)
    points, counts = np.asarray(points, np.float64) @ frame.to, np.asarray(counts, np.int64)
    head = Head.of(np.asarray(roots, np.float64) @ frame.to)
    heads = np.cumsum(counts) - counts
    lengths = fitting.lengths(points, np.r_[0, np.cumsum(counts)])
    ends = _targets(points[heads], head, settings)
    cut = _crossing(points, counts, ends) if settings.mode == HEIGHT else ends
    trimming = np.flatnonzero(np.isfinite(cut) & (cut > 0) & (cut < lengths - 1e-4) & (counts > 1))
    if not len(trimming):
        return trimming, np.zeros(0, np.int64), np.zeros((0, 3))
    kept = fitting.trimmed(cut[trimming], counts[trimming], spacing)
    shaped = np.empty((int(kept.sum()), 3))
    starts = np.cumsum(kept) - kept
    for count in np.unique(kept):
        these = np.flatnonzero(kept == count)
        placed = _at(points, counts, heads, trimming[these], cut[trimming[these], None] * np.linspace(0, 1, count))
        shaped[(starts[these][:, None] + np.arange(count)).ravel()] = placed.reshape(-1, 3)
    return trimming, kept, shaped @ frame.back


def fly_aways(counts: np.ndarray) -> np.ndarray:
    counts = np.asarray(counts)
    return counts <= _FLY * np.median(counts) if len(counts) else np.zeros(0, bool)


def _hairline(roots: np.ndarray, head: Head) -> np.ndarray:
    return ((roots[:, 2] - head.middle[1] > _FRONT * head.half)
            & (np.abs(np.degrees(_azimuth(roots, head))) < _FRONT_TURN))


def _temples(roots: np.ndarray, head: Head) -> np.ndarray:
    return ~_hairline(roots, head) & (np.abs(np.degrees(_azimuth(roots, head))) < _TUCK_TURN)


def _combs(roots: np.ndarray, head: Head, sides: np.ndarray, centred: bool = False) -> tuple[np.ndarray, np.ndarray]:
    front, temples = _hairline(roots, head), _temples(roots, head)
    own = np.where(roots[:, 0] >= head.middle[0], 1.0, -1.0)
    ways = np.zeros((len(roots), 3))
    count = int(front.sum())
    ways[front] = np.column_stack((sides[front], np.full(count, _PART_BACK if centred else _COMB_BACK),
                                   np.full(count, _PART_RAMP if centred else 0.0)))
    ways[temples, :2] = np.column_stack((own[temples] * _TUCK_SIDE, np.ones(int(temples.sum()))))
    return front, ways


def _centred(roots: np.ndarray, head: Head, sides: np.ndarray, mixed: bool) -> np.ndarray:
    front, off = _hairline(roots, head), roots[:, 0] - head.middle[0]
    out = np.where(front, np.where(off >= 0, 1.0, -1.0), sides)
    if not mixed:
        return out
    band = _PART_MIX * head.half
    chance = 1 / (1 + np.exp(np.clip(-off / (band / 3), -50, 50)))
    draw = np.modf(np.abs(np.sin(roots @ np.array((41.3719, 59.1153, 23.8471))) * 43758.5453))[0]
    return np.where(front & (np.abs(off) < band), np.where(draw < chance, 1.0, -1.0), out)


def _back_closed(chains: np.ndarray, head: Head) -> None:
    points = chains[:, 1:]
    low = head.jaw + _BACK_LOW * (head.hairline - head.jaw)
    behind = (points[..., 2] < head.middle[1] - _BACK_BEHIND * head.half) & (points[..., 1] < low)
    share = _BACK_CLOSE * np.clip((low - points[..., 1]) / _BACK_RAMP, 0, 1)
    points[..., 0] -= np.where(behind, (points[..., 0] - head.middle[0]) * share, 0.0)


def _check(settings: GrowSettings) -> None:
    if settings.mode not in (LENGTH, HEIGHT):
        raise ValueError(f"Strands are cut to a {LENGTH} or a {HEIGHT}, not {settings.mode!r}")


def _targets(roots: np.ndarray, head: Head, settings: GrowSettings) -> np.ndarray:
    clump, own = _uneven(roots)
    out = settings.target + settings.uneven * (0.7 * clump + 0.3 * own)
    if settings.mode == HEIGHT:
        out += settings.rise * _front(roots, head)
        if settings.fringe is not None:
            fringe = _fringe(roots, head)
            out[fringe] = settings.fringe + settings.uneven * _FRINGE_UNEVEN * clump[fringe]
    return out


def _azimuth(roots: np.ndarray, head: Head) -> np.ndarray:
    """0 at the front."""
    return np.arctan2(roots[:, 0] - head.middle[0], roots[:, 2] - head.middle[1])


def _front(roots: np.ndarray, head: Head) -> np.ndarray:
    return ((1 + np.cos(_azimuth(roots, head))) / 2) ** _RISING


def _fringe(roots: np.ndarray, head: Head) -> np.ndarray:
    return ((roots[:, 2] - head.middle[1] > _FRINGE_FRONT * head.half)
            & (np.abs(np.degrees(_azimuth(roots, head))) < _FRINGE_TURN))


def _crossing(points: np.ndarray, counts: np.ndarray, tips: np.ndarray) -> np.ndarray:
    strand = np.repeat(np.arange(len(counts)), counts)
    heads = np.cumsum(counts) - counts
    below = points[:, 1] <= tips[strand]
    first = np.minimum.reduceat(np.where(below, np.arange(len(points)), len(points)), heads) if len(counts) else heads
    out = np.full(len(counts), np.nan)
    crossing = np.flatnonzero((first < heads + counts) & ~below[heads])
    if not len(crossing):
        return out
    steps = np.r_[0.0, np.linalg.norm(np.diff(points, axis=0), axis=1)]
    steps[heads] = 0
    along = np.cumsum(steps)
    along -= np.repeat(along[heads], counts)
    k = first[crossing]
    high, low = points[k - 1, 1], points[k, 1]
    share = np.clip((high - tips[crossing]) / np.maximum(high - low, 1e-12), 0, 1)
    out[crossing] = along[k - 1] + share * (along[k] - along[k - 1])
    return out


class _Work:
    def __init__(self, progress, cancelled):
        self.progress, self.cancelled = progress, cancelled

    def tell(self, share: float) -> None:
        if self.cancelled is not None and self.cancelled():
            raise GrowCancelled("Growing was stopped")
        if self.progress is not None:
            self.progress(round(1000 * share), 1000)

    def run(self, jobs: Sequence[Callable[[], object]], first: float, last: float) -> list:
        results = [None] * len(jobs)
        pool = ThreadPoolExecutor(max(1, min(os.process_cpu_count() or 1, len(jobs))))
        try:
            futures = {pool.submit(job): number for number, job in enumerate(jobs)}
            for done, future in enumerate(as_completed(futures), 1):
                results[futures[future]] = future.result()
                self.tell(first + (last - first) * done / len(jobs))
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        return results


def _uneven(roots: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    clump = np.sin(roots @ np.array((137.0, 91.0, 113.0))) * np.cos(roots @ np.array((71.0, 157.0, 59.0)))
    own = np.modf(np.abs(np.sin(roots @ np.array((12.9898, 78.233, 37.719))) * 43758.5453))[0] * 2 - 1
    return clump, own


def _depths(roots: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    draws = [np.modf(np.abs(np.sin(roots @ np.array(way)) * 43758.5453))[0]
             for way in ((63.7264, 10.873, 92.1817), (27.3194, 81.0491, 15.6231))]
    return _SCALES[0] + (_SCALES[1] - _SCALES[0]) * draws[0], _EXTRA * draws[1]


def _sides(ends: np.ndarray, roots: np.ndarray, middle: float) -> np.ndarray:
    off = np.where(ends != middle, ends - middle, roots - middle)
    return np.where(off >= 0, 1.0, -1.0)


def _chains(points, counts, heads, strands, keep) -> np.ndarray:
    kept = _at(points, counts, heads, strands, keep[:, None] * np.linspace(0, 1, _KEPT))
    way = kept[:, -1] - kept[:, -2]
    norm = np.linalg.norm(way, axis=1)
    way = np.where(norm[:, None] > 1e-12, way / np.maximum(norm, 1e-12)[:, None], (0.0, -1.0, 0.0))
    grown = kept[:, -1:] + way[:, None] * (_SPREAD * np.arange(1, _GROWN + 1))[:, None]
    return np.ascontiguousarray(np.concatenate((kept, grown), axis=1))


def _at(points, counts, heads, strands, wanted) -> np.ndarray:
    longest = int(counts[strands].max())
    grid = heads[strands][:, None] + np.minimum(np.arange(longest), counts[strands][:, None] - 1)
    line = points[grid]
    along = np.zeros(grid.shape)
    along[:, 1:] = np.cumsum(np.linalg.norm(np.diff(line, axis=1), axis=2), axis=1)
    return _along(line, along, wanted)


def _along(line: np.ndarray, along: np.ndarray, wanted: np.ndarray) -> np.ndarray:
    index = np.clip((along[:, None, :] <= wanted[:, :, None]).sum(2) - 1, 0, line.shape[1] - 2)
    rows = np.arange(len(line))[:, None]
    start, end = along[rows, index], along[rows, index + 1]
    span = end - start
    share = np.divide(wanted - start, span, out=np.zeros(span.shape), where=span > 1e-12)
    a, b = line[rows, index], line[rows, index + 1]
    return a + (b - a) * share[..., None]


def _rows(shapes: Sequence[CollisionShape], frame: Frame, head: Head) -> tuple[np.ndarray, np.ndarray]:
    rows, kept = np.zeros((len(shapes), _WIDTH)), np.ones(len(shapes), bool)
    for number, shape in enumerate(shapes):
        row, values = rows[number], np.asarray(shape.values, np.float64)
        if shape.kind == ShapeKind.PLANE:
            normal = values[0:3] / max(float(np.linalg.norm(values[0:3])), 1e-12)
            row[0], row[1:4], row[4:7] = _PLANE, -np.inf, np.inf
            row[7:10] = frame.turn @ normal
            row[10] = values[3] / max(float(np.linalg.norm(values[0:3])), 1e-12) + normal @ frame.origin
            continue
        if shape.kind == ShapeKind.BOX:
            centre, axes, extents = box_axes(shape)
            centre, axes = frame.from_world(centre), axes @ frame.turn.T
            half = (np.abs(axes) * extents[:, None]).sum(0)
            row[0], row[1:4], row[4:7] = _BOX, centre - half, centre + half
            row[7:10], row[10:19], row[19:22] = centre, axes.ravel(), extents
            continue
        a, ra, b, rb = capsule_ends(shape)
        a, b = frame.from_world(np.array((a, b)))
        span = b - a
        sideways = shape.kind != ShapeKind.SPHERE and abs(span[0]) >= 0.7 * np.linalg.norm(span)
        off = np.array((a[0], b[0])) - head.middle[0]
        if sideways and off[0] * off[1] >= 0 and np.abs(off).max() > head.half and max(a[1], b[1]) < head.jaw:
            inner = int(np.argmin(np.abs(off)))
            if abs(off[inner]) >= (ra, rb)[inner]:
                kept[number] = False
                continue
            if inner:
                a, ra, b, rb = b, rb, a, ra
            b, rb = a + (b - a) * _SHOULDER, ra + (rb - ra) * _SHOULDER
            row[_SLIDES] = 1
        row[0], row[7:10], row[10], row[11:14], row[14] = _CAPSULE, a, ra, b, rb
        row[1:4], row[4:7] = np.minimum(a - ra, b - rb), np.maximum(a + ra, b + rb)
    return rows, kept


def _use(rows, kept, shapes, frame: Frame, roots, reach) -> np.ndarray:
    use = np.zeros((len(roots), len(rows)), np.uint8)
    world = frame.to_world(roots)
    for number in np.flatnonzero(kept):
        row = rows[number]
        near = np.all((roots + reach[:, None] >= row[1:4]) & (roots - reach[:, None] <= row[4:7]), axis=1)
        use[near, number] = depth(world[near], shapes[number])[0] <= 0
    return use


def _drape(chains, strands, use, rows, grid, head: Head, settings: GrowSettings, work: _Work) -> np.ndarray:
    styled = np.zeros(_KEPT + _GROWN)
    styled[:_KEPT] = _HOLD * (1 - np.arange(_KEPT) / _KEPT) ** 1.5
    styled[0] = 1.0
    values, origin, cell = (np.zeros((0, 0, 0), np.float32), np.zeros(3), _CELL) if grid is None else grid
    blocks = np.zeros(0, np.float32) if grid is None else np.ascontiguousarray(_blocks(values, _BLOCK))
    params = dict(kept=_KEPT, steps=_STEPS, growing=_GROWING, start=_START, gravity_x=0.0, gravity_y=-_GRAVITY,
                  gravity_z=0.0, smooth=_SMOOTH, reach=_REACH, slide=_SLIDE, slide_front=_SLIDE_FRONT,
                  on_shoulder=_ON_SHOULDER, base=_BASE,
                  thickens=_THICKENS, thickest=_THICKEST, deepens=_DEEPENS, lower=_LOWER, taper=_TAPER, nape=head.jaw,
                  part=float(settings.part), face_middle=head.middle[0],
                  face_side=_FACE * head.half, face_low=head.jaw, face_high=head.hairline, face_front=head.middle[1],
                  comb=_COMB * _GRAVITY, on_head=_ON_HEAD, comb_low=head.jaw + _COMB_LOW * (head.hairline - head.jaw),
                  comb_rear=head.middle[1] - _COMB_REAR * head.half, comb_leave=_PART_LEAVE,
                  grid_x=values.shape[0], grid_y=values.shape[1], grid_z=values.shape[2], origin_x=origin[0],
                  origin_y=origin[1], origin_z=origin[2], cell=cell, floor=head.jaw, settle=_SETTLE, window=_WINDOW,
                  calm=_CALM, fold=np.cos(np.radians(_FOLD)), block=_BLOCK)
    packed = np.array([params[name] for name in _PARAMS], np.float64)
    rows, values = np.ascontiguousarray(rows), np.ascontiguousarray(values, np.float32)
    step = max(16, -(-len(chains) // (8 * (os.process_cpu_count() or 1))))

    def job(part):
        return lambda: _fastmesh.drape_strands(chains[part], strands[part], use[part], rows, styled, values, blocks,
                                               packed)
    taken = work.run([job(slice(begin, begin + step)) for begin in range(0, len(chains), step)], 0.1, 0.95)
    return np.concatenate([np.frombuffer(steps, np.int64) for steps in taken]) if taken else np.zeros(0, np.int64)


def _cut(chains, lengths, *, tips=None, caps=None, targets=None) -> tuple[np.ndarray, np.ndarray]:
    along = np.zeros(chains.shape[:2])
    along[:, 1:] = np.cumsum(np.linalg.norm(np.diff(chains, axis=1), axis=2), axis=1)
    rows = np.arange(len(chains))
    if tips is None:
        cut = targets
    else:
        below = chains[:, 1:, 1] <= tips[:, None]
        k = below.argmax(1) + 1
        high, low = chains[rows, k - 1, 1], chains[rows, k, 1]
        share = np.clip((high - tips) / np.maximum(high - low, 1e-9), 0, 1)
        crossing = along[rows, k - 1] + share * (along[rows, k] - along[rows, k - 1])
        cut = np.minimum(np.where(below.any(1), crossing, along[:, -1]), caps)
    cut = np.minimum(cut, along[:, -1])
    grown = (along <= cut[:, None]) & (np.arange(chains.shape[1]) >= _KEPT - 1)
    heights = np.where(grown, chains[..., 1], np.inf)
    lowest = heights.argmin(1)
    hooked = grown.any(1) & (_along(chains, along, cut[:, None])[:, 0, 1] - heights[rows, lowest] > _HOOK)
    turned = hooked & (along[rows, lowest] <= lengths + 1e-4)
    cut = np.where(hooked & ~turned, along[rows, lowest], cut)
    return np.minimum(np.maximum(cut, lengths), along[:, -1]), turned


def _resampled(chains, cut, counts) -> np.ndarray:
    along = np.zeros(chains.shape[:2])
    along[:, 1:] = np.cumsum(np.linalg.norm(np.diff(chains, axis=1), axis=2), axis=1)
    tip = _along(chains, along, cut[:, None])[:, 0]
    line = np.concatenate((np.where((along <= cut[:, None])[..., None], chains, tip[:, None]), tip[:, None]), axis=1)
    distance = np.zeros(line.shape[:2])
    distance[:, 1:] = np.cumsum(np.linalg.norm(np.diff(line, axis=1), axis=2), axis=1)
    shaped = np.empty((int(counts.sum()), 3))
    heads = np.cumsum(counts) - counts
    for count in np.unique(counts):
        strands = np.flatnonzero(counts == count)
        placed = _along(line[strands], distance[strands], distance[strands, -1:] * np.linspace(0, 1, count))
        placed[:, -1] = tip[strands]
        shaped[(heads[strands][:, None] + np.arange(count)).ravel()] = placed.reshape(-1, 3)
    return shaped


def _sharp(lines: np.ndarray) -> np.ndarray:
    way = np.diff(lines, axis=1)
    unit = way / np.maximum(np.linalg.norm(way, axis=2), 1e-12)[..., None]
    return (unit[:, :-1] * unit[:, 1:]).sum(2) < np.cos(np.radians(_BEND))


def _bent(shaped: np.ndarray, counts: np.ndarray) -> np.ndarray:
    heads = np.cumsum(counts) - counts
    out = np.zeros(len(counts), bool)
    for count in np.unique(counts[counts > 2]):
        strands = np.flatnonzero(counts == count)
        out[strands] = _sharp(shaped[heads[strands][:, None] + np.arange(count)]).any(1)
    return out


def _eased(shaped: np.ndarray, counts: np.ndarray) -> np.ndarray:
    heads = np.cumsum(counts) - counts
    out = shaped.copy()
    for count in np.unique(counts[counts > 2]):
        strands = np.flatnonzero(counts == count)
        lines = out[heads[strands][:, None] + np.arange(count)]
        segments = np.linalg.norm(np.diff(lines, axis=1), axis=2)
        for _ in range(_EASES):
            sharp = _sharp(lines)
            bent = sharp.any(1)
            if not bent.any():
                break
            lines, segments, strands, sharp = lines[bent], segments[bent], strands[bent], sharp[bent]
            near = sharp.copy()
            near[:, 1:] |= sharp[:, :-1]
            near[:, :-1] |= sharp[:, 1:]
            lines[:, 1:-1] += np.where(near[..., None], 0.5 * (0.5 * (lines[:, :-2] + lines[:, 2:]) - lines[:, 1:-1]), 0)
            for k in range(1, count):
                way = lines[:, k] - lines[:, k - 1]
                lines[:, k] = lines[:, k - 1] + way * (segments[:, k - 1] / np.maximum(np.linalg.norm(way, axis=1),
                                                                                       1e-12))[:, None]
            out[heads[strands][:, None] + np.arange(count)] = lines
    return out


def _through(shaped: np.ndarray, counts: np.ndarray, shapes: Sequence[CollisionShape], frame: Frame) -> np.ndarray:
    heads = np.cumsum(counts) - counts
    strand = np.repeat(np.arange(len(counts)), counts)
    world = frame.to_world(shaped)
    past_root = np.ones(len(shaped), bool)
    past_root[heads] = False
    through = np.zeros(len(counts), bool)
    for shape in shapes:
        box = bounds(shape)
        rows = np.flatnonzero(past_root if box is None else past_root & np.all((world >= box[0]) & (world <= box[1]), 1))
        rows = rows[depth(world[rows], shape)[0] > 0]
        rows = rows[depth(world[heads[strand[rows]]], shape)[0] <= 0]
        through[strand[rows]] = True
    return through


def _tidied(shaped: np.ndarray, counts: np.ndarray, marked: np.ndarray) -> np.ndarray:
    if len(counts) <= _MODELS:
        return shaped
    heads = np.cumsum(counts) - counts
    lengths = fitting.lengths(shaped, np.r_[0, np.cumsum(counts)])
    marks = np.arange(int(lengths.max() // _ALONG) + 1) * _ALONG
    held = marks <= lengths[:, None]
    samples = _at(shaped, counts, heads, np.arange(len(counts)), np.minimum(marks, lengths[:, None]))[held]
    cells = np.floor((samples - samples.min(0)) / _MASS).astype(np.int64) + 1
    size = cells.max(0) + 2
    around = np.bincount(np.ravel_multi_index(tuple(cells.T), size), minlength=int(np.prod(size))).reshape(size)
    for axis in range(3):
        moved = np.moveaxis(around, axis, 0)
        summed = moved.copy()
        summed[1:] += moved[:-1]
        summed[:-1] += moved[1:]
        around = np.moveaxis(summed, 0, axis)
    around = around[tuple(cells.T)]
    astray = marked.copy()
    astray[np.nonzero(held)[0][around < _SPARSE * np.median(around)]] = True
    if not astray.any():
        return shaped
    roots = shaped[heads]
    near = np.frombuffer(_fastmesh.nearest_points(np.ascontiguousarray(roots), _MODELS), np.int64).reshape(-1, _MODELS)
    fine = ~astray[near]
    model = np.where(fine.any(1), near[np.arange(len(near)), fine.argmax(1)], -1)
    strays = np.flatnonzero(astray & (model >= 0))
    out = shaped.copy()
    for count in np.unique(counts[model[strays]]):
        these = strays[counts[model[strays]] == count]
        lines = shaped[heads[model[these]][:, None] + np.arange(count)] - roots[model[these]][:, None] + roots[these][
            :, None]
        placed = _resampled(lines, lengths[model[these]], counts[these])
        start = np.cumsum(counts[these]) - counts[these]
        for strand, begin in zip(these.tolist(), start.tolist()):
            out[heads[strand]:heads[strand] + counts[strand]] = placed[begin:begin + counts[strand]]
    return out


def _grid(positions: np.ndarray, triangles: np.ndarray, floor: float, work: _Work):
    corners = _closed(positions, triangles)
    if not len(corners):
        return None
    room = _ROOM
    pad = room + 2 * _CELL
    points = corners.reshape(-1, 3)
    points = points[points[:, 1] > floor - pad]
    if not len(points):
        return None
    low, high = points.min(0) - pad, points.max(0) + pad
    low[1] = max(low[1], floor - 2 * _CELL)
    coarse = _CELL * _COARSE
    size = np.ceil((high - low) / coarse).astype(np.int64) + 1
    tree = _baker.build(np.ascontiguousarray(corners.reshape(-1, 9)))
    nodes = _nodes(low, coarse, size, np.arange(int(np.prod(size))))
    values = np.concatenate(work.run([_sample(tree, nodes[begin:begin + _SAMPLES])
                                      for begin in range(0, len(nodes), _SAMPLES)], 0.0, 0.02)).reshape(size)
    fine = (size - 1) * _COARSE + 1
    for axis in range(3):
        values = _finer(values, axis)
    # Exact near the surface; the coarse grid's blend elsewhere is off by at most half a coarse cell's diagonal.
    off = coarse * np.sqrt(3) / 2
    values = values.ravel()
    band = np.flatnonzero((values > -(_REACH + off)) & (values < room + off))
    exact = _nodes(low, _CELL, fine, band)
    values[band] = np.concatenate(work.run([_sample(tree, exact[begin:begin + _SAMPLES])
                                            for begin in range(0, len(exact), _SAMPLES)], 0.02, 0.1))
    return values.reshape(fine).astype(np.float32), low, _CELL


def _nodes(low: np.ndarray, cell: float, size: np.ndarray, numbers: np.ndarray) -> np.ndarray:
    return low + np.column_stack(np.unravel_index(numbers, tuple(size))) * cell


def _sample(tree, points: np.ndarray):
    return lambda: np.frombuffer(_baker.sample(tree, np.ascontiguousarray(points), True), np.float32)


def _finer(values: np.ndarray, axis: int) -> np.ndarray:
    values = np.moveaxis(values, axis, 0)
    count = (len(values) - 1) * _COARSE + 1
    at = np.arange(count) / _COARSE
    base = np.minimum(at.astype(np.int64), len(values) - 2)
    share = (at - base).reshape(-1, *([1] * (values.ndim - 1)))
    return np.moveaxis(values[base] * (1 - share) + values[base + 1] * share, 0, axis)


def _blocks(values: np.ndarray, size: int) -> np.ndarray:
    for axis in range(3):
        moved = np.moveaxis(values, axis, 0)
        starts = np.arange(0, len(moved) - 1, size)
        least = np.minimum(np.minimum.reduceat(moved, starts, axis=0), moved[np.minimum(starts + size, len(moved) - 1)])
        values = np.moveaxis(least, 0, axis)
    return values


def _closed(positions: np.ndarray, triangles: np.ndarray) -> np.ndarray:
    if not len(triangles):
        return np.zeros((0, 3, 3))
    welded, inverse = np.unique(positions, axis=0, return_inverse=True)
    triangles = inverse.ravel()[triangles]
    part = _connected(len(welded), np.concatenate((triangles[:, :2], triangles[:, 1:])))[triangles[:, 0]]
    sizes = np.bincount(part, minlength=len(welded))
    triangles = triangles[sizes[part] >= _PIECE * sizes.max()]
    edges = np.concatenate((triangles[:, :2], triangles[:, 1:], triangles[:, ::-2]))
    _, inverse, counts = np.unique(np.sort(edges, axis=1), axis=0, return_inverse=True, return_counts=True)
    rims = edges[counts[inverse.ravel()] == 1]
    corners = welded[triangles]
    if len(rims):
        _, loop = np.unique(_connected(len(welded), rims)[rims[:, 0]], return_inverse=True)
        middles = np.zeros((loop.max() + 1, 3))
        np.add.at(middles, loop, welded[rims[:, 0]])
        middles /= np.bincount(loop)[:, None]
        corners = np.concatenate((corners, np.stack((welded[rims[:, 1]], welded[rims[:, 0]], middles[loop]), 1)))
    normal = np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
    area = np.einsum("ij,ij->i", normal, normal)
    return np.ascontiguousarray(corners[np.isfinite(area) & (area > 0)])


def _connected(count: int, pairs: np.ndarray) -> np.ndarray:
    label = np.arange(count)
    while True:
        low = np.minimum(label[pairs[:, 0]], label[pairs[:, 1]])
        joined = label.copy()
        np.minimum.at(joined, pairs[:, 0], low)
        np.minimum.at(joined, pairs[:, 1], low)
        joined = joined[joined]
        if np.array_equal(joined, label):
            return label
        label = joined


__all__ = ["GrowCancelled", "GrowSettings", "HEIGHT", "LENGTH", "default_fringe", "default_height", "fly_aways",
           "grown", "trimmed"]
