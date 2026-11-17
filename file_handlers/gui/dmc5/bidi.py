"""See docs/internals/gui-rendering.md, Right-to-left text."""
from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence

from .text import TextLayoutGap

_FAILS = "the game's bidi reorder fails on its control characters (it crashes or writes past its level array)"


class _Run:
    __slots__ = ("pos", "len", "type", "level", "prev", "next")

    def __init__(self, pos: int = 0, length: int = 0, kind: int = 0x20, level: int = -1):
        self.pos, self.len, self.type, self.level = pos, length, kind, level
        self.prev = self.next = self


def _link(run: _Run, before: _Run) -> None:
    run.prev, run.next = before.prev, before
    before.prev.next = run
    before.prev = run


def _unlink(run: _Run) -> None:
    run.prev.next, run.next.prev = run.next, run.prev


def _side(run: _Run, other: _Run) -> int:
    return other.type if other.level == run.level else max(other.level, run.level) & 1 | 0x44


class _Runs:

    def __init__(self, size: int):
        self.free = size

    def take(self, pos: int = 0, length: int = 0, kind: int = 0x20, level: int = -1) -> _Run:
        if not self.free:
            raise TextLayoutGap(_FAILS)
        self.free -= 1
        return _Run(pos, length, kind, level)

    def drop(self, run: _Run) -> None:
        _unlink(run)
        self.free += 1

    def merge(self, run: _Run) -> _Run:
        run.prev.len += run.len
        self.drop(run)
        return run.prev

    def compact(self, head: _Run, neutrals: bool = False) -> None:
        run = head.next
        while run is not head:
            if run.prev.level == run.level and (
                    run.prev.type == run.type or neutrals and run.prev.type & run.type & 0x10):
                run = self.merge(run)
            run = run.next

    def explicit(self, head: _Run, removed: _Run, level: int) -> None:
        stack, override, run = [(0, 0)] * 63, 0x10, head.next
        while run is not head:
            kind, after = run.type, run.next
            if not kind & 0x40400:
                if kind == 0x80A10:
                    break
                run.level = level
                if not override & 0x10:
                    run.type = override
            else:
                for _ in range(run.len if kind & 0x04 or kind == 0x408 else 0):
                    if kind & 0x04:
                        stack.append((override, level))
                        level = (level + 2 + (kind & 1) & ~1) - (kind & 1)
                        override = kind & 1 | 0x44 if kind & 0x1000 else 0x10
                    elif len(stack) > 1:
                        stack.pop()
                        override, level = stack[-1]
                    elif stack:
                        raise TextLayoutGap("its bidi embeddings end more often than the engine's stack allows")
                run.level = -1
                _unlink(run)
                _link(run, removed)
            run = after
        self.compact(head)

    def weak(self, head: _Run, base: int) -> None:
        run, strong = head.next, base
        while run is not head:
            before, after = _side(run, run.prev), _side(run, run.next)
            strong = before if before & 0x04 else strong
            if run.type == 0x20008:
                if run.prev.level == run.level:
                    run = self.merge(run)
                else:
                    run.type = before
                if before == after and run.level == run.next.level:
                    self.merge(run.next)
            elif run.type == 0x88 and strong == 0x47:
                run.type = 0x8A
                if after == 0x20008:
                    run.next.type = 0x8A
            run = run.next
        run, strong, split, previous = head.next, base, True, 0x10
        while run is not head:
            before, after, kind = _side(run, run.prev), _side(run, run.next), run.type
            strong = before if before & 0x04 else strong
            if kind == 0x47:
                run.type, split, previous = 0x45, True, 0x10
            else:
                if split and run.len == 1 and kind & 0x14000 and previous & 0x80 and previous == after and (
                        previous == 0x88 or kind == 0x10108):
                    run.type = before
                split = True
                if run.type == 0x8108 and 0x88 in (previous, after):
                    run.type, split = 0x88, False
                if run.type & 0x100:
                    run.type = 0x10
                if run.type == 0x88 and strong == 0x44:
                    run.type, previous = 0x44, 0x88 if run.level == run.next.level else 0x10
                else:
                    previous = _side(run.next, run)
            run = run.next
        self.compact(head, True)

    def shadow(self, head: _Run, over: _Run, keep_length: bool) -> None:
        q, last = over.next, 0
        while q is not over:
            after = q.next
            if q.len and last <= q.pos:
                pos, end = q.pos, q.pos + q.len
                p = head.next
                while p.next.type != 0x20 and p.next.pos <= pos:
                    p = p.next
                r = p
                while r.next.type != 0x20 and r.next.pos < end:
                    r = r.next
                if keep_length:
                    r.len += q.len
                left = p
                if p is r:
                    if end < p.pos + p.len:
                        r = self.take(end, p.pos + p.len - end, p.type, p.level)
                        _link(r, p.next)
                    else:
                        r = r.next
                    if pos <= p.pos + p.len:
                        if p.pos < pos:
                            p.len = pos - p.pos
                        elif p is head:
                            raise TextLayoutGap(_FAILS)
                        else:
                            left = p.prev
                            self.drop(p)
                else:
                    if pos <= p.pos + p.len:
                        if p.pos < pos:
                            p.len = pos - p.pos
                        else:
                            left = p.prev
                    if end < r.pos + r.len:
                        r.pos, r.len = end, r.pos + r.len - end
                    else:
                        r = r.next
                    while left.next is not r:
                        self.drop(left.next)
                _unlink(q)
                q.prev, q.next, left.next, r.prev = left, r, q, q
                last = pos
            q = after
        while over.next is not over:
            self.drop(over.next)


def levels(types: Sequence[int], base: int) -> list[int]:
    runs, head, level = _Runs(len(types) + 3), _Run(), base & 1
    for pos, kind in enumerate(types):
        if head.prev is head or head.prev.type != kind:
            _link(runs.take(pos, 0, kind, 0), head)
        head.prev.len += 1
    removed = runs.take()
    runs.explicit(head, removed, level)
    runs.weak(head, level | 0x44)
    run = head.next
    while run is not head:
        if not run.type & 0x80 and run.type & 0x10:
            before, after = (_side(run, side) for side in (run.prev, run.next))
            before, after = (0x45 if side & 0x80 else side for side in (before, after))
            run.type = before if before == after else run.level & 1 | 0x44
        run = run.next
    runs.compact(head)
    run = head.next
    while run is not head:
        run.level = (run.level + 2 & ~1) if run.type & 0x80 else run.level + ((run.type ^ run.level) & 1)
        run = run.next
    runs.compact(head)
    if removed.next is not removed:
        runs.shadow(head, removed, True)
        run = head.next
        while run is not head:
            run.level = (level if run.prev is head else run.prev.level) if run.level == -1 else run.level
            run = run.next
    trailing, open_, end = runs.take(), True, len(types) - 1
    for at in range(len(types) - 1, -2, -1):
        kind = types[at] if at >= 0 else 0
        if open_ and not kind & 0x240C00:
            open_ = False
            _link(runs.take(at + 1, end - at, level | 0x44, level), trailing.next)
        elif not open_ and kind & 0x800:
            open_, end = True, at
    runs.shadow(head, trailing, False)
    out = []
    run = head.next
    while run is not head:
        out += [run.level] * run.len
        run = run.next
    return out


def reorder(codes: Sequence[int], rules) -> list[int]:
    starts, kinds, mirrors = rules.bidi
    types = [kinds[bisect_right(starts, code) - 1] for code in codes]
    base = next((kind & 1 | 0x44 for kind in types if kind & 0x40), 0x10)
    found, out = levels(types, base), list(codes)
    if len(found) != len(types):
        raise TextLayoutGap(_FAILS)
    for at in range(len(types) - 1, -1, -1):
        if types[at] & 0x240400:
            found[at] = base & 1
        else:
            break
    at = len(types) - 1
    while at >= 0:
        level = found[at]
        if level & 1 and types[at] == 0x20008:
            end, at = at, at - 1
            while at >= 0 and types[at] & 0x60400 and found[at] == level:
                at -= 1
            if at < 0 or found[at] != level:
                at += 1
            out[at:end + 1] = out[at:end + 1][::-1]
        at -= 1
    out = [mirrors.get(code, code) if level & 1 else code for code, level in zip(out, found)]
    for level in range(max(found, default=0), 0, -1):
        at = len(found) - 1
        while at >= 0:
            start = at
            while start >= 0 and found[start] >= level:
                start -= 1
            out[start + 1:at + 1] = out[start + 1:at + 1][::-1]
            at = start - 1
    return out
