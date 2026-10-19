"""See docs/internals/strand-editing.md, Fitting and guides."""
from __future__ import annotations

lazy import numpy as np

lazy from .strands_file import NO_GUIDE, unpacked

SPACED, _ROUNDING = 0.9, 1e-6
_CANDIDATES, _PLACES, _CHUNK = 64, 16, 512
# Smallest distance a guide's weight is measured at (m).
_NEAREST = 1e-5
# Under 1 so a strand's keys stay below the next strand's.
KEY_SPAN = 0.999


def lengths(positions: np.ndarray, starts: np.ndarray) -> np.ndarray:
    steps = np.r_[np.linalg.norm(np.diff(positions.astype(np.float64), axis=0), axis=1), 0.0]
    steps[starts[1:] - 1] = 0
    return np.add.reduceat(steps, starts[:-1]) if len(starts) > 1 else np.zeros(0)


def fractions(positions: np.ndarray, starts: np.ndarray) -> np.ndarray:
    strand = np.repeat(np.arange(len(starts) - 1), np.diff(starts))
    steps = np.r_[0, np.linalg.norm(np.diff(positions.astype(np.float64), axis=0), axis=1)]
    steps[starts[:-1]] = 0
    total = np.cumsum(steps)
    base = total[starts[:-1]]
    length = total[starts[1:] - 1] - base
    return np.divide(total - base[strand], length[strand], out=np.zeros(len(total)), where=length[strand] > 0)


def keyed(fractions_: np.ndarray, starts: np.ndarray) -> np.ndarray:
    return np.repeat(np.arange(len(starts) - 1), np.diff(starts)) + fractions_ * KEY_SPAN


def spacings(positions: np.ndarray, starts: np.ndarray, counts: list[np.ndarray]) -> list[float]:
    length = lengths(positions, starts)
    out = []
    for count in counts:
        used = (count > 1) & (length > 0)
        if not used.any():
            out.append(np.inf)
            continue
        spacing = length[used] / (count[used] - 1)
        order = np.argsort(spacing, kind="stable")
        weight = np.cumsum(length[used][order])
        out.append(float(spacing[order][np.searchsorted(weight, SPACED * weight[-1])]))
    return out


def counts(length: np.ndarray, had: np.ndarray, spacing: float) -> np.ndarray:
    segments = np.ceil(np.asarray(length, np.float64) / spacing - _ROUNDING).astype(np.int64)
    return np.maximum(had, np.maximum(segments, 0) + 1)


def trimmed(length: np.ndarray, had: np.ndarray, spacing: float) -> np.ndarray:
    segments = np.ceil(np.asarray(length, np.float64) / spacing - _ROUNDING).astype(np.int64)
    return np.minimum(had, np.maximum(segments, 1) + 1)


def on(points: np.ndarray, first: np.ndarray, following: np.ndarray, share: np.ndarray,
       offset: np.ndarray | None = None) -> np.ndarray:
    out = points[first] + (points[following] - points[first]) * share[:, None]
    if offset is not None:
        out += offset
    return out.astype(np.float32)


def shares(count: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    line = np.repeat(np.arange(len(count)), count)
    number = np.arange(int(count.sum())) - np.repeat(np.cumsum(count) - count, count)
    return line, number, np.divide(number, (count - 1)[line], out=np.zeros(len(line)), where=(count > 1)[line])


def placed(points: np.ndarray, starts: np.ndarray, count: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    count = np.asarray(count, np.int64)
    own = np.diff(starts)
    line, number, wanted = shares(count)
    have = fractions(points, starts)
    found = np.searchsorted(keyed(have, starts), line + wanted * KEY_SPAN, "right") - 1
    segment = np.clip(found - starts[line], 0, np.maximum(own - 2, 0)[line])
    first = starts[line] + segment
    following = np.minimum(first + 1, starts[line + 1] - 1)
    span = have[following] - have[first]
    share = np.clip(np.divide(wanted - have[first], span, out=np.zeros(len(line)), where=span > 0), 0, 1)
    root, tip = number == 0, (number == (count - 1)[line]) & (count > 1)[line]
    segment[root], share[root] = 0, 0
    segment[tip], share[tip] = (own - 1)[line[tip]], 0
    first = starts[line] + segment
    following = np.minimum(first + 1, starts[line + 1] - 1)
    return on(points, first, following, share), segment, share


def points_at(points: np.ndarray, fractions_: np.ndarray, keys: np.ndarray, starts: np.ndarray, lines: np.ndarray,
              wanted: np.ndarray) -> np.ndarray:
    """``fractions_`` and ``keys`` must be ``points``' own; ``points`` float64."""
    count = np.diff(starts)[lines]
    found = np.searchsorted(keys, lines + wanted * KEY_SPAN, "right") - 1
    first = starts[lines] + np.clip(found - starts[lines], 0, np.maximum(count - 2, 0))
    following = np.minimum(first + 1, starts[lines + 1] - 1)
    span = fractions_[following] - fractions_[first]
    share = np.clip(np.divide(wanted - fractions_[first], span, out=np.zeros(len(first)), where=span > 0), 0, 1)
    return points[first] + (points[following] - points[first]) * share[:, None]


def places(fractions_: np.ndarray, keys: np.ndarray, starts: np.ndarray, rows: np.ndarray,
           guides: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    wanted = fractions_[rows]
    count = np.diff(starts)[guides]
    found = np.searchsorted(keys, guides + wanted * KEY_SPAN, "left") - 1
    offset = np.clip(found - starts[guides], 0, np.maximum(count - 2, 0))
    first = starts[guides] + offset
    following = np.minimum(first + 1, starts[guides + 1] - 1)
    span = fractions_[following] - fractions_[first]
    share = np.clip(np.divide(wanted - fractions_[first], span, out=np.zeros(len(first)), where=span > 0), 0, 1)
    return offset, share


def chosen(positions: np.ndarray, starts: np.ndarray, strands: np.ndarray, pool: np.ndarray,
           count: int, *, sharpness: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """Weights: see docs/internals/strand-editing.md."""
    strands, pool = np.asarray(strands, np.int64), np.asarray(pool, np.int64)
    guides = np.full((len(strands), 3), NO_GUIDE, np.int64)
    weights = np.zeros((len(strands), 3))
    count = min(int(count), 3, len(pool))
    if not len(strands) or count <= 0:
        return guides, weights
    points = positions.astype(np.float64)
    have = fractions(points, starts)
    keys = keyed(have, starts)
    roots = points[starts[:-1]]
    along = np.linspace(0, 1, _PLACES)

    def sampled(lines):
        return points_at(points, have, keys, starts, np.repeat(lines, _PLACES), np.tile(along, len(lines))).reshape(
            len(lines), _PLACES, 3)
    guided, guiding = sampled(strands), sampled(pool)
    nearest = min(_CANDIDATES, len(pool))
    for begin in range(0, len(strands), _CHUNK):
        part = strands[begin:begin + _CHUNK]
        gap = ((roots[part][:, None] - roots[pool][None]) ** 2).sum(2)
        near = np.argpartition(gap, nearest - 1, axis=1)[:, :nearest] if nearest < len(pool) else np.broadcast_to(
            np.arange(len(pool)), gap.shape)
        candidates = pool[near]
        mean = np.sqrt(((guiding[near] - guided[begin:begin + len(part), None]) ** 2).sum(3).mean(2))
        best = np.lexsort((candidates, mean), axis=1)[:, :count]
        rows = np.arange(len(part))[:, None]
        inverse = 1 / np.maximum(mean[rows, best], _NEAREST) ** 2
        weight = inverse / inverse.sum(1, keepdims=True)
        if sharpness != 1:
            weight **= sharpness
            weight /= weight.sum(1, keepdims=True)
        guides[begin:begin + len(part), :count] = candidates[rows, best]
        weights[begin:begin + len(part), :count] = weight
    return guides, weights


def blends(positions: np.ndarray, starts: np.ndarray, words: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Blends as the game's chain makes them."""
    guides, offsets, weights, shares = unpacked(words)
    counts = np.diff(starts)
    used = (guides != NO_GUIDE) & (weights > 0)
    some = np.where(used, guides, 0)
    share = (offsets[:, 0] + shares[:, 0]) / np.maximum(counts[some[:, 0]] - 1, 1)
    index = share[:, None] * (counts[some] - 1)
    first = np.minimum(np.floor(index).astype(np.int64), np.maximum(counts[some] - 2, 0))
    lerp = np.clip(index - first, 0, 1)[..., None]
    following = np.minimum(first + 1, counts[some] - 1)
    points = positions.astype(np.float64)
    sampled = points[starts[some] + first] * (1 - lerp) + points[starts[some] + following] * lerp
    weights = np.where(used, weights, 0)
    total = weights.sum(1)
    blend = (sampled * weights[..., None]).sum(1) / np.maximum(total, 1e-9)[:, None]
    return blend, total > 0


def gaps(positions: np.ndarray, starts: np.ndarray, words: np.ndarray, first: int = 0, *,
         rooted: bool = False) -> np.ndarray:
    blend, guided = blends(positions, starts, words)
    off = positions.astype(np.float64) - blend
    if rooted:
        off -= np.repeat(off[starts[:-1]], np.diff(starts), axis=0)
    distance = np.where(guided, np.linalg.norm(off, axis=1), 0)
    out = np.maximum.reduceat(distance, starts[:-1]) if len(starts) > 1 else np.zeros(0)
    out[:first] = 0
    return out


__all__ = ["SPACED", "blends", "chosen", "counts", "fractions", "gaps", "keyed", "lengths", "on", "placed", "places",
           "points_at", "shares", "spacings", "trimmed"]
