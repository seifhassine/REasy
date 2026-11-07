"""See docs/internals/mottree-runtime.md."""

from __future__ import annotations

import struct
from collections.abc import Callable

lazy import numpy as np

lazy from file_handlers.engine_math import axis_turn, crt, turned_vector

from .layers import MotionLayer, layer_joint_masks, mask_weights
from .model import Motion, MotionTree, SyncPoints, TreeNode
from .pose import MotionSampler, Rig
from .pose_blend import JointPoses, PoseBlend
from .rules import rotation_offset_head, tree_param_kinds, tree_param_names
from .tree_pose import (
    PoseFrames, TreePose, _normalized, blend_add, blend_interpolation, blend_layer_alpha, blend_normal, mirror,
    mirror_rows,
)

F = np.float32
_ONE, _ZERO, _NONE, _HALF, _TWO, _THREE = F(1), F(0), F(-1), F(0.5), F(2), F(3)
_EPSILON, _LOOP_EPSILON = F(1.1920929e-07), F(1e-05)
ONCE, LOOP, TURN_BACK, LOOP_TURN_BACK = 1, 2, 3, 4
# via::vec3::AxisX and AxisY; RotationGlobalOffsetNode's degrees to radians (two steps), the |w| from which a rotation
# counts as none, and the least sine it divides by.
_ACROSS, _UP = (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)
_DEGREE, _PI, _NEAR_NONE, _TINY = F(0.0055555557), F(3.1415927), F(0.99999988), F(1e-10)


class TreeContext:
    def __init__(self, motion_of: Callable[[int, int], Motion | None], variables: dict, bank: int = 0,
                 rest: dict[int, tuple] | None = None, root: int | None = None):
        self.motion_of, self.variables, self.bank = motion_of, variables, bank
        self.slots: dict[int, int] = {}
        self.rest_of, self.root_hash, self.delta, self.sync_time = rest or {}, root, _ZERO, _NONE
        self.mirror_of: Callable[[int, int], bool] = lambda _bank, _motion: False
        self.reverse_of: Callable[[int, int], bool] = lambda _bank, _motion: False
        self.mask_of: Callable[[int, int], tuple[int, str]] = lambda _bank, _motion: (0, "ID")
        self._row_masks: dict = {}
        self.rig: Rig | None = None
        self.mirror_table: tuple | None = None
        self._mirrored: tuple | None = None
        self.parent_of: dict[int, int] = {}
        self.version = 0
        self.parent_rotations: Callable[[], dict[int, tuple]] | None = None
        self.mask: dict[int, tuple[int, float]] | None = None
        self.groups: dict = {}
        self.base: TreePose | None = None
        self._masking: np.ndarray | None = None
        self.random = [123456789, 362436069, 521288629, 88675123]
        self._samplers: dict[int, tuple[MotionSampler, np.ndarray, np.ndarray]] = {}
        self._scratch: dict[int, TreePose] = {}
        self._made: tuple | None = None
        self.joints_of: Callable[[Motion], frozenset] | None = None

    def slot_count(self) -> int:
        return len(self.slots)

    def _built(self) -> tuple:
        count = len(self.slots)
        if self._made is None or self._made[0] != count:
            rest = [np.zeros((count, 3), np.float32), np.tile(np.array([0, 0, 0, 1], np.float32), (count, 1)),
                    np.ones((count, 3), np.float32)]
            for value, slot in self.slots.items():
                if (held := self.rest_of.get(value)) is not None:
                    for component in range(3):
                        rest[component][slot] = held[component]
            root = self.slots.get(self.root_hash, -1)
            rows = np.arange(count)
            slerped = rows == root
            self._made = count, rest, rows, rows[~slerped], slerped
        return self._made

    @property
    def rest(self) -> list[np.ndarray]:
        return self._built()[1]

    def rows(self, skip_root: bool = False) -> np.ndarray:
        return self._built()[3 if skip_root else 2]

    def slerped(self) -> np.ndarray:
        return self._built()[4]

    def masking(self) -> np.ndarray | None:
        if self.mask is not None and (self._masking is None or len(self._masking) != len(self.slots)):
            self._masking = mask_weights(tuple(self.slots), self.mask)
        return None if self.mask is None else self._masking

    def row_masking(self, mask: int, mode: str) -> np.ndarray | None:
        key = mask, mode, len(self.slots)
        if key not in self._row_masks:
            found = layer_joint_masks(self.groups, MotionLayer(mask=mask, mask_mode=mode)) if mask else None
            self._row_masks[key] = None if found is None else mask_weights(tuple(self.slots), found)
        return self._row_masks[key]

    def mirror(self, pose: TreePose) -> None:
        if self.rig is not None:
            if self._mirrored is None or self._mirrored[0] != len(self.slots):
                self._mirrored = len(self.slots), mirror_rows(tuple(self.slots), self.rig, self.mirror_table)
            mirror(pose, *self._mirrored[1], self.rest[1])

    def scratch(self, key: int) -> TreePose:
        held = self._scratch.get(key)
        if held is None or len(held.weights) != len(self.slots):
            held = self._scratch[key] = TreePose(len(self.slots))
        return held

    def sampler(self, motion: Motion) -> tuple[MotionSampler, np.ndarray, np.ndarray]:
        held = self._samplers.get(id(motion))
        if held is None:
            sampler = MotionSampler(motion, None, self.joints_of(motion) if self.joints_of else frozenset())
            slots = np.array([self.slots.setdefault(value, len(self.slots)) for value in sampler.hashes], np.int64)
            held = self._samplers[id(motion)] = sampler, np.arange(len(slots)), slots
        return held

    def draw(self) -> int:
        """The engine's xorshift128 (RandomParameterNode)."""
        x, y, z, w = self.random
        t = (x << 15 ^ x) & 0xFFFFFFFF
        self.random = [y, z, w, (w >> 17 ^ t) >> 4 ^ w ^ t]
        return self.random[3]


class SyncList:
    def __init__(self, frames: list | None = None, blocks: int = 1, points: int = 1, loop: bool = False):
        self.frames, self.block_count, self.point_count = frames, blocks, points
        self.play_block, self.loop, self.dynamic, self.value = 0, loop, False, _ZERO

    @classmethod
    def of(cls, sync: SyncPoints | None, loop: bool) -> SyncList:
        if sync is None:
            return cls(loop=loop)
        return cls([F(frame) for frame in sync.frames], sync.block_count, sync.point_count, loop)

    def has_points(self) -> bool:
        return self.frames is not None

    def count(self) -> int:
        product = self.block_count * self.point_count
        return product + 1 if product else 0

    def point(self, block: int, point: int) -> F:
        if self.frames is None:
            return _ZERO
        at = self.point_count * block + min(point, self.point_count)
        return self.frames[at] if 0 <= at < len(self.frames) else _ZERO

    def move(self) -> None:
        if self.play_block + 1 < self.block_count:
            self.play_block += 1
        else:
            self.play_block = 0 if self.loop else self.block_count

    def frame_at(self, block: int, point: int, rate: F, end: F) -> F:
        if block < self.block_count:
            if not self.has_points():
                return rate * end if point == 0 else end
            first, second = self.point(block, point), self.point(block, point + 1)
            if self.point_count == 1 and point == 0 and first == second:
                frame = rate * end + first
                return frame - end if end < frame else frame
            if first <= second:
                return (second - first) * rate + first
            frame = ((second + end) - first) * rate + first
            return frame - end if end <= frame else frame
        return self.point(self.block_count, 0) if self.has_points() else end


def _locate(merged: SyncList, frame: F, end: F, one_shot: bool) -> tuple[int, F, int | None]:
    if not one_shot:
        if merged.play_block >= merged.block_count:
            return 0, _ZERO, None
        first = merged.point(merged.play_block, 0)
        target = first + frame
        for index in range(merged.point_count):
            second = merged.point(merged.play_block, index + 1)
            if first < target <= second:
                return index, (target - first) / (second - first), None
            first = second
        return 0, _ZERO, None
    if frame >= end:
        return 0, _ZERO, merged.block_count
    points, count = merged.point_count, merged.count()
    first, found, rate = _ZERO, count - 1, _ZERO
    for at in range(1, count):
        second = merged.point(at // points, at % points)
        if first < frame:
            if frame == second and at + 1 != count:
                found, rate = at, _ZERO
                break
            if frame <= second:
                found, rate = at - 1, (frame - first) / (second - first)
                break
        first = second
    return found % points, rate, found // points


class SyncBlendFrames:
    def __init__(self):
        self.pair: tuple = (None, None)
        self.merged = SyncList()
        self.frame = self.prev = self.over = _ZERO
        self.end, self.rate = _NONE, _ZERO
        self.index, self.through, self.loops, self.blocks, self.state = -1, _ZERO, 0, 0, 0
        self.valid = self.one_shot = self.dynamic = False
        self.looping = True

    def reset(self) -> None:
        self.frame, self.index, self.through, self.blocks, self.loops, self.over, self.state = (
            _ZERO, 0, _ZERO, 0, 0, _ZERO, 0)

    def set_frame(self, children: tuple, frame: F) -> None:
        self.frame = F(frame)
        if not self.valid:
            for child in children:
                if child is not None:
                    child.set_frame(frame)

    def update(self, children: tuple, rate: F) -> None:
        self.pair = children if self.core(children, rate) else (None, None)

    def core(self, children: tuple, rate: F) -> bool:
        self.end, self.rate, self.valid, self.dynamic = _NONE, F(rate), False, False
        first, second = children
        if first is None:
            return False
        if second is None:
            self.end = first.end()
            return False
        if first.end() < 0 or second.end() < 0:
            return False
        keep = _ONE - self.rate
        lists = first.sync_table(), second.sync_table()
        if lists[0] is None or lists[1] is None:
            return False
        blocks = [lists[0].play_block, lists[1].play_block]
        one_shot, looping, start, dynamic = False, True, 0, False
        count = lists[0].block_count
        if not self.merged.loop and count == lists[1].block_count and blocks[0] == blocks[1]:
            if not first.dynamic_sync() and not second.dynamic_sync():
                if first.is_loop():
                    count, dynamic = 1, True
                else:
                    start, blocks, looping, one_shot = blocks[0], [0, 0], False, True
            else:
                dynamic = True
        else:
            count, dynamic = 1, True
        bases = [lists[0].point(blocks[0], 0), lists[1].point(blocks[1], 0)]
        points = max(lists[0].point_count, lists[1].point_count)
        table = [_ZERO] * (count * points + 1)
        for block in range(count):
            for point in range(points + 1 if block == count - 1 else points):
                if block == 0 and point == 0:
                    continue
                values, held = [_ZERO, _ZERO], [True, True]
                for side, (listed, child) in enumerate(zip(lists, children)):
                    if not listed.has_points() and not listed.loop:
                        value = child.end()
                    else:
                        value = listed.point((blocks[side] + block) % listed.block_count,
                                             min(point, listed.point_count)) - bases[side]
                    if value <= 0 and listed.loop:
                        value = child.end() + value
                    if value < 0:
                        held[side], value = False, _ZERO
                    values[side] = value
                value = (values[0] * keep + values[1] * self.rate if all(held) else values[0] if held[0] else
                         values[1] if held[1] else _ZERO)
                table[block * points + point] = _ZERO if value <= _EPSILON else value
        self.merged = SyncList(table, count, points, looping)
        self.merged.play_block = start
        if not one_shot:
            at = min(start, count - 1)
            self.end = max(table[at * points + points] - table[at * points], _ZERO)
        else:
            self.end = table[self.merged.count() - 1]
        self.valid, self.one_shot, self.looping, self.dynamic = True, one_shot, looping, dynamic
        frame = self.merged.frame_at(start, max(self.index, 0), self.through, self.end)
        self.frame = frame if one_shot else frame - self.merged.point(self.merged.play_block, 0)
        for child in children:
            if child not in self.pair:
                child.set_sync_time(F(max(self.index, 0)) + self.through)
                child.add(_ZERO)
                child.tick()
        return True

    def add(self, children: tuple, delta: F) -> None:
        self.over, self.state = _ZERO, 0
        first, second = children
        if not self.valid or first is None or second is None:
            self.frame += delta
            for child in children:
                if child is not None:
                    child.add(delta)
            self.index, self.through = -1, _ZERO
            return
        loops, self.loops = self.loops, 0
        self.frame += delta
        if first.sync_table() is None or second.sync_table() is None:
            self._tail(delta, loops)
            return
        if self.end > 0:
            if self.end <= self.frame:
                self.state |= 1
                if not self.looping:
                    self.over = self.frame - self.end if not self.dynamic else self.over
                    self.frame = self.end
                else:
                    while self.end > 0 and self.end <= self.frame:
                        loops += 1
                        self.frame -= self.end
                        self._by_frame()
                        self._next_block(children)
        else:
            self.frame = _ZERO
        index, through = 0, _ZERO
        if self.frame != 0 and self.end > 0:
            index, through, block = _locate(self.merged, self.frame, self.end, self.one_shot)
            if block is not None:
                for listed in (first.sync_table(), second.sync_table(), self.merged):
                    listed.play_block = block
        for child in children:
            listed, end = child.sync_table(), child.end()
            frame = listed.frame_at(listed.play_block, index, through, end)
            listed.value = F(index) + through
            wrapped = int(_LOOP_EPSILON < child.prev() - frame)
            child.set_frame(frame)
            child.set_loop_count(wrapped if loops < 2 else loops - 1 + wrapped)
            child.add(_ZERO)
        self.through, self.index, self.prev = through, index, self.frame
        self.blocks = self.blocks + loops if not self.one_shot else self.merged.play_block
        self._tail(delta, loops)

    def _tail(self, delta: F, loops: int) -> None:
        left = self.end - self.frame
        if left != 0 and left <= delta:
            self.state |= 2
        if loops:
            self.state |= 4

    def _by_frame(self) -> None:
        if self.frame == 0:
            self.index, self.through = 0, _ZERO
            return
        self.index, self.through, block = _locate(self.merged, self.frame, self.end, self.one_shot)
        if block is not None:
            self.merged.play_block = block

    def _next_block(self, children: tuple) -> None:
        lists = [None if child is None else child.sync_table() for child in children]
        if lists[0] is None or lists[1] is None:
            return
        if self.one_shot:
            self.merged.move()
            return
        moved = False
        for child in children:
            child.next_block()
            moved |= child.dynamic_sync()
        moved = moved or lists[0].block_count != lists[1].block_count
        if not self.merged.dynamic and not moved:
            if self.merged.block_count != 1:
                self.merged.move()
                at = self.merged.play_block
                self.end = self.merged.point(at + 1, 0) - self.merged.point(at, 0)
        else:
            self.merged.move()
            frame = self.frame
            self.update(children, self.rate)
            self.frame = frame


class ParamNode:
    def __init__(self, node: TreeNode, params: dict, context: TreeContext):
        self.node, self.params, self.context = node, params, context
        self.pins: dict[int, tuple] = {}

    def value(self, out) -> F:
        return _ZERO

    def int_value(self, out) -> int:
        return int(self.value(out))

    def bool_value(self, out) -> bool:
        return self.value(out) != 0

    def pin(self, index: int) -> tuple | None:
        return self.pins.get(index)


def _read(pin: tuple | None, kind: str = "value"):
    if pin is None:
        return None
    node, out = pin
    return getattr(node, kind)(out)


class ParameterNode(ParamNode):
    def value(self, out) -> F:
        return F(self.params.get("Value", 0.0))


class VariablesNode(ParamNode):
    def raw(self, out):
        return self.context.variables.get(out, 0)

    def value(self, out) -> F:
        return F(float(self.raw(out)))

    def int_value(self, out) -> int:
        return int(self.raw(out))

    def bool_value(self, out) -> bool:
        return bool(self.raw(out))


class RangeNormalizeNode(ParamNode):
    def __init__(self, *args):
        super().__init__(*args)
        self.last = _ZERO

    def value(self, out) -> F:
        """The engine scales by (OutMax - OutMin) + OutMin."""
        given = _read(self.pin(0))
        if given is None:
            return self.last
        p = self.params
        high, low = F(p.get("InMax", 1.0)), F(p.get("InMin", 0.0))
        if p.get("ClampMode"):
            self.last = low if low > given else high if high <= given else F(given)
            return self.last
        if high == low:
            return self.last
        rate = (F(given) - low) / (high - low)
        out_high, out_low = F(p.get("OutMax", 1.0)), F(p.get("OutMin", 0.0))
        if out_high > out_low:
            top, bottom = out_high, out_low
        else:
            top, bottom, rate = out_low, out_high, -rate
        rate = rate * ((out_high - out_low) + out_low)
        self.last = bottom if bottom > rate else top if rate > top else rate
        return self.last


class StepRangeNormalizeNode(ParamNode):
    def __init__(self, *args):
        super().__init__(*args)
        blob = self.params.get("Blob") or b""
        count = struct.unpack_from("<I", blob)[0] if len(blob) >= 4 else 0
        self.steps = [F(value) for value in struct.unpack_from(f"<{count}f", blob, 4)] if count else []
        self.last = _ZERO

    def value(self, out) -> F:
        given = _read(self.pin(0))
        if given is None or len(self.steps) < 2:
            return self.last
        steps, given, self.last = self.steps, F(given), _ZERO
        if not given > steps[0]:
            return self.last
        top = len(steps) - 1
        at = next((index for index in range(top, -1, -1) if given - steps[index] >= 0), -1)
        if at >= top or at < 0:
            self.last = _ONE
            return self.last
        span = steps[at + 1] - steps[at]
        if span == 0:
            return self.last
        part = _ONE / F(top)
        self.last = min(max(((given - steps[at]) / span) * part + F(at) * part, _ZERO), _ONE)
        return self.last


class RangeSwitchCaseNode(ParamNode):
    def __init__(self, *args):
        super().__init__(*args)
        blob = self.params.get("Blob") or bytes(8)
        count, self.default = struct.unpack_from("<If", blob)
        self.cases = [struct.unpack_from("<Ifff", blob, 8 + 16 * index) for index in range(count)]

    def value(self, out) -> F:
        given = _read(self.pin(0))
        if given is not None:
            given = F(given)
            for mode, low, high, value in self.cases:
                low, high = F(low), F(high)
                if ((low < given < high) if mode == 0 else (low <= given < high) if mode == 1 else
                        (low < given <= high) if mode == 2 else (low <= given <= high) if mode == 3 else False):
                    return F(value)
        return F(self.default)


class CompareNode(ParamNode):
    def value(self, out) -> F:
        a, b = _read(self.pin(0)), _read(self.pin(1))
        if a is None or b is None:
            return _ZERO
        a, b, operation = F(a), F(b), self.params.get("Operation", 0)
        held = (a == b, a != b, not a < b, not b < a, not (a < b or a == b), not (b < a or a == b))
        return _ONE if operation < len(held) and held[operation] else _ZERO


class CalculateNode(ParamNode):
    def value(self, out) -> F:
        a, b = _read(self.pin(0)), _read(self.pin(1))
        if a is None or b is None:
            return _ZERO
        a, b, operation = F(a), F(b), self.params.get("Operation", 0)
        if operation == 0:
            return b + a
        if operation == 1:
            return a - b
        if operation == 2:
            return b * a
        if operation == 3:
            return a / b if b != 0 else _ZERO
        if operation == 4:
            return F(np.fmod(a, b))
        if operation == 5:
            return F(np.power(a, b))
        if operation in (6, 7):
            return (b if b <= a else a) if operation == 6 else (a if b <= a else b)
        return _ZERO


class AbsNode(ParamNode):
    def value(self, out) -> F:
        given = _read(self.pin(0))
        return _ZERO if given is None else abs(F(given))

    def int_value(self, out) -> int:
        given = _read(self.pin(0), "int_value")
        return 0 if given is None else abs(given)

    def bool_value(self, out) -> bool:
        return self.int_value(out) != 0


class NotNode(ParamNode):
    def bool_value(self, out) -> bool:
        given = _read(self.pin(0), "bool_value")
        return True if given is None else not given

    def value(self, out) -> F:
        return _ONE if self.bool_value(out) else _ZERO

    def int_value(self, out) -> int:
        return int(self.bool_value(out))


class RoundingNode(ParamNode):
    def __init__(self, *args):
        super().__init__(*args)
        self.last = 0

    def int_value(self, out) -> int:
        given = _read(self.pin(0))
        if given is not None:
            given, mode = F(given), self.params.get("RoundingMode", 0)
            self.last = (int((given + F(0.5)) - F(given < 0)) if mode == 0 else int(np.floor(given)) if mode == 1
                         else int(np.ceil(given)) if mode == 2 else self.last)
        return self.last

    def value(self, out) -> F:
        return F(self.int_value(out))

    def bool_value(self, out) -> bool:
        return self.int_value(out) != 0


class InitialCacheNode(ParamNode):
    def __init__(self, *args):
        super().__init__(*args)
        self.cached = None

    def value(self, out) -> F:
        if self.cached is None and (given := _read(self.pin(0))) is not None:
            self.cached = F(given)
        return _ZERO if self.cached is None else self.cached


class DynamicSwitchParameterNode(ParamNode):
    def _chosen(self) -> tuple | None:
        switch = _read(self.pin(2), "bool_value")
        return self.pin(1 if switch else 0)

    def value(self, out) -> F:
        given = _read(self._chosen())
        return _ZERO if given is None else F(given)

    def int_value(self, out) -> int:
        given = _read(self._chosen(), "int_value")
        return 0 if given is None else given

    def bool_value(self, out) -> bool:
        return bool(_read(self._chosen(), "bool_value"))


class SwitchParameterNode(ParamNode):
    def __init__(self, *args):
        super().__init__(*args)
        self.last = _ZERO

    def value(self, out) -> F:
        index = _read(self.pin(0), "int_value")
        if index is not None:
            self.last = F(self.params.get(f"No{index:02d}_Parameter" if 0 <= index < 16 else "No00_Parameter", 0.0))
        return self.last


class RandomParameterNode(ParamNode):
    def _draw(self, whole: bool) -> F:
        low, high = F(self.params.get("InMin", 0.0)), F(self.params.get("InMax", 1.0))
        span = (high - low) + _ONE if whole else high - low
        value = (span * F(self.context.draw())) * F(2.3283064e-10) + low
        return high if whole and high < value else value

    def value(self, out) -> F:
        return F(np.floor(self._draw(True))) if out else self._draw(False)

    def int_value(self, out) -> int:
        return int(self._draw(bool(out)))


PARAM_NODES = {cls.__name__: cls for cls in (
    ParameterNode, VariablesNode, RangeNormalizeNode, StepRangeNormalizeNode, RangeSwitchCaseNode, CompareNode,
    CalculateNode, AbsNode, NotNode, RoundingNode, InitialCacheNode, DynamicSwitchParameterNode, SwitchParameterNode,
    RandomParameterNode)}


class Node:
    def __init__(self, node: TreeNode, params: dict, context: TreeContext):
        self.node, self.params, self.context = node, params, context
        self.children: list = []
        self.pins: dict[int, tuple] = {}
        self.weight = _ZERO

    def child(self, index: int):
        return self.children[index] if 0 <= index < len(self.children) else None

    def pin(self, index: int) -> tuple | None:
        return self.pins.get(index)

    def present(self) -> list:
        return [child for child in self.children if child is not None]

    def init(self) -> None:
        pass

    def update_state(self) -> None:
        for child in self.present():
            child.update_state()

    def control(self) -> None:
        for child in self.present():
            child.control()

    def coef(self, index: int) -> F:
        return _ONE

    def update_weight(self, weight: F) -> None:
        self.weight = F(weight)
        for index, child in enumerate(self.children):
            if child is not None:
                child.update_weight(self.weight * self.coef(index))

    def add(self, delta: F) -> None:
        pass

    def set_frame(self, frame: F) -> None:
        pass

    def frame(self) -> F:
        return _ZERO

    def prev(self) -> F:
        return _ZERO

    def end(self) -> F:
        return _ZERO

    def over(self) -> F:
        return _ZERO

    def added(self) -> F:
        return _ZERO

    def loop_count(self) -> int:
        return 0

    def set_loop_count(self, count: int) -> None:
        pass

    def state(self) -> int:
        return 0

    def is_loop(self) -> bool:
        return False

    def sync_table(self) -> SyncList | None:
        return None

    def sync_time(self) -> F:
        return _NONE

    def set_sync_time(self, time: F) -> None:
        pass

    def dynamic_sync(self) -> bool:
        return False

    def next_block(self) -> None:
        pass

    def reset_frame(self) -> None:
        for child in self.present():
            child.reset_frame()

    def reset_motion(self) -> None:
        for child in self.present():
            child.reset_motion()

    def tick(self) -> None:
        for child in self.present():
            child.tick()

    def pose(self, out: TreePose) -> None:
        out.clear()

    def motion_nodes(self):
        for child in self.present():
            yield from child.motion_nodes()


class MotionNode(Node):
    def __init__(self, *args):
        super().__init__(*args)
        self.motion = None
        self.list = SyncList()
        self._frame = self._prev = self._over = self._added = self.length = _ZERO
        self.loops, self._state, self.wrap, self.turned = 0, 0, ONCE, False

    def init(self) -> None:
        bank = self.context.bank if self.params.get("UseOwnerBankID") else self.params.get("BankID", 0)
        motion_id = self.params.get("MotionID", 0)
        self.set_motion(self.context.motion_of(bank, motion_id), self.context.mirror_of(bank, motion_id),
                        self.context.reverse_of(bank, motion_id), self.context.mask_of(bank, motion_id))

    def set_motion(self, motion: Motion | None, mirrored: bool = False, reverse: bool = False,
                   mask: tuple[int, str] = (0, "ID")) -> None:
        self.motion, self.mirrored, self.reverse = motion, mirrored and motion is not None, reverse
        self.row_mask = mask
        self.length = F(motion.end_frame) if motion is not None else _ZERO
        self.wrap = LOOP if motion is not None and motion.loop_frame >= 0 else ONCE
        self.list = SyncList.of(motion.sync[0] if motion is not None and motion.sync else None, self.wrap == LOOP)
        self._frame = self._prev = self._over = self._added = _ZERO
        self.loops, self._state, self.turned = 0, 0, False

    def add(self, delta: F) -> None:
        frame = self._frame + F(delta)
        self._over, self._frame = _ZERO, frame
        added, mode, end = frame - self._prev, self.wrap, self.length
        loop, turning, pending = mode == LOOP, mode == LOOP_TURN_BACK, self.loops
        if turning or pending or loop:
            self.loops = 0
            extra = F(pending) * end
            added, self._frame = added + extra, frame + extra
        if mode != TURN_BACK and not turning:
            self.turned = False
        self._state = 0
        if end != 0:
            if end - self._frame < added:
                self._state |= 2
            if end <= self._frame:
                self._state |= 1
                self._over = self._frame - end
                if pending == 0 and not loop:
                    if not turning:
                        if mode != TURN_BACK or self.turned:
                            self._frame = end
                        else:
                            self.turned, self._frame = True, self._frame - end
                    else:
                        while end <= self._frame:
                            self.turned, self._frame = not self.turned, self._frame - end
                else:
                    self._state |= 4
                    while end <= self._frame:
                        self._frame -= end
            elif self._frame < 0:
                if pending or loop:
                    while self._frame < 0:
                        self._frame += end
                elif turning:
                    while self._frame < 0:
                        self.turned, self._frame = not self.turned, self._frame + end
                else:
                    self._frame = _ZERO
        else:
            self._frame = _ZERO
        self._added = added

    def data_frame(self) -> F:
        frame = self.length - self._frame if self.turned else self._frame
        return self.length - frame if self.reverse else frame

    def set_frame(self, frame: F) -> None:
        self._frame = F(frame)

    def frame(self) -> F:
        return self._frame

    def prev(self) -> F:
        return self._prev

    def end(self) -> F:
        return self.length

    def over(self) -> F:
        return self._over

    def added(self) -> F:
        return self._added

    def loop_count(self) -> int:
        return self.loops

    def set_loop_count(self, count: int) -> None:
        self.loops = count

    def state(self) -> int:
        return self._state

    def is_loop(self) -> bool:
        return self.wrap == LOOP

    def sync_table(self) -> SyncList:
        return self.list

    def sync_time(self) -> F:
        listed, frame, end = self.list, self._frame, self.length
        if not listed.has_points():
            return frame / end if end else _ZERO
        count, points = listed.count(), listed.point_count
        first = listed.point(0, 0)
        if count == 1:
            rate = frame - first
            return (rate + end if frame < first else rate) / end
        found, rate = count - 1, _ZERO
        for at in range(1, count):
            second = listed.point(at // points, at % points)
            if second < first or (first == second and count == 2):
                if frame <= first:
                    first -= end
                elif frame <= second + end:
                    rate, found = (frame - first) / ((second + end) - first), at - 1
                    break
            if first < frame <= second:
                rate, found = (frame - first) / (second - first), at - 1
                break
            first = second
        value = F(found % points) + rate
        return _ZERO if self.wrap == LOOP and value == F(points) else value

    def set_sync_time(self, time: F) -> None:
        time = F(time)
        if self.list.has_points():
            self._frame = self.list.frame_at(self.list.play_block, int(time), time - F(int(time)), self.length)
        else:
            self._frame = self.length * (_ZERO if time < 0 else _ONE if _ONE <= time else time)

    def next_block(self) -> None:
        self.list.move()

    def reset_frame(self) -> None:
        self._frame = self._prev = self._over = _ZERO
        self.loops, self.list.play_block = 0, 0

    def reset_motion(self) -> None:
        pass

    def tick(self) -> None:
        self._prev, self.loops = self._frame, 0

    def pose(self, out: TreePose) -> None:
        out.clear()
        if self.motion is not None:
            sampler, own, slots = self.context.sampler(self.motion)
            values, keyed = sampler.sample(float(self.data_frame()))
            masking, row = self.context.masking(), self.context.row_masking(*self.row_mask)
            masking = row if masking is None else masking if row is None else masking * row
            for component in range(3):
                held = keyed[component]
                out.values[component][slots[held]] = values[component][own[held]]
                out.weights[slots[held], component] = sampler.weights[own[held]] if masking is None else \
                    sampler.weights[own[held]] * masking[slots[held], component]
            if self.mirrored:
                self.context.mirror(out)
        self.tick()

    def motion_nodes(self):
        yield self


class SelectMotionNode(MotionNode):
    def __init__(self, *args):
        super().__init__(*args)
        self.chosen = False

    def control(self) -> None:
        if self.chosen:
            return
        self.chosen = True
        index, p = _read(self.pin(0), "int_value") or 0, self.params
        held = min(max(index, 0), 15)
        motion_id = index if p.get("DirectID") else p.get(f"No{held:02d}_MotionID", 0xFFFFFFFF)
        bank = p.get(f"No{held:02d}_BankID", 0) if p.get("BankSelectMode") == 1 else p.get("BankID", 0)
        if (motion := self.context.motion_of(bank, motion_id)) is not None:
            self.set_motion(motion, self.context.mirror_of(bank, motion_id), self.context.reverse_of(bank, motion_id),
                            self.context.mask_of(bank, motion_id))

    def reset_motion(self) -> None:
        self.chosen = False


class FilterNode(Node):
    def _ask(self, name: str, default):
        child = self.child(0)
        return default if child is None else getattr(child, name)()

    def add(self, delta: F) -> None:
        if (child := self.child(0)) is not None:
            child.add(delta)

    def set_frame(self, frame: F) -> None:
        if (child := self.child(0)) is not None:
            child.set_frame(frame)

    def frame(self) -> F:
        return self._ask("frame", _ZERO)

    def prev(self) -> F:
        return self._ask("prev", _ZERO)

    def end(self) -> F:
        return self._ask("end", _ZERO)

    def over(self) -> F:
        return self._ask("over", _ZERO)

    def added(self) -> F:
        return self._ask("added", _ZERO)

    def loop_count(self) -> int:
        return self._ask("loop_count", 0)

    def set_loop_count(self, count: int) -> None:
        if (child := self.child(0)) is not None:
            child.set_loop_count(count)

    def state(self) -> int:
        return self._ask("state", 0)

    def is_loop(self) -> bool:
        return self._ask("is_loop", False)

    def sync_table(self):
        return self._ask("sync_table", None)

    def sync_time(self) -> F:
        return self._ask("sync_time", _NONE)

    def set_sync_time(self, time: F) -> None:
        if (child := self.child(0)) is not None:
            child.set_sync_time(time)

    def dynamic_sync(self) -> bool:
        return self._ask("dynamic_sync", False)

    def next_block(self) -> None:
        if self.sync_table() is not None and (child := self.child(0)) is not None:
            child.next_block()

    def pose(self, out: TreePose) -> None:
        if (child := self.child(0)) is not None:
            child.pose(out)
        else:
            out.clear()


def _offset(rotation, half: F) -> np.ndarray:
    x, y, z, w = (F(value) for value in rotation)
    angle, axis = _ZERO, _ACROSS
    if abs(w) < _NEAR_NONE and (x * x + y * y) + z * z > _EPSILON:
        angle = F(crt("acosf")(float(w)))
        sine = F(crt("sinf")(float(angle)))
        if abs(sine) > _TINY:
            inverse = _ONE / sine
            x, y, z = x * inverse, y * inverse, z * inverse
        angle, axis = angle * _TWO, (x, y, z)
    turned = turned_vector(axis, axis_turn(_UP, half))
    half = angle * _HALF
    sine = F(crt("sinf")(float(half)))
    return _normalized(np.array([[*(turned * sine), F(crt("cosf")(float(half)))]], np.float32))[0]


class RotationGlobalOffsetNode(FilterNode):
    def init(self) -> None:
        super().init()
        head, blob, joints = rotation_offset_head(self.context.version), self.params.get("Blob"), self.context.rest_of
        self.chain, self.weights, self.root_joint, self.angle, self.ratio = [], (), None, _ZERO, _ZERO
        if head is None or not blob or not joints:
            return
        fields = head.decode(blob, 0)
        self.angle, self.ratio = F(fields["angle"]), F(fields["ratio"])
        joint = fields["joint"] if fields["joint"] in joints else None
        while joint is not None and len(self.chain) < min(fields["count"], 16):
            self.chain.append(joint)
            joint = self.context.parent_of.get(joint)
        self.weights = struct.unpack_from(f"<{len(self.chain)}f", blob, head.size)
        self.root_joint = fields.get("root") if fields.get("root") in joints else None

    def pose(self, out: TreePose) -> None:
        super().pose(out)
        if not self.params.get("Blob") or not self.context.rest_of:
            return
        if (angle := _read(self.pin(0))) is not None:
            self.angle = (F(angle) * _DEGREE) * _PI
        if (ratio := _read(self.pin(1))) is not None:
            self.ratio = F(ratio)
        turn, slots = self.ratio * self.angle, self.context.slots
        for joint, weight in zip(self.chain, self.weights):
            if (slot := slots.get(joint)) is not None:
                out.values[1][slot] = _offset(out.values[1][slot], (turn * F(weight)) * _HALF)
        if (slot := slots.get(self.root_joint)) is not None:
            out.values[0][slot] = turned_vector(out.values[0][slot], axis_turn(_UP, turn * _HALF))


class ParentLocalConstraintsNode(Node):
    def init(self) -> None:
        if (rotations := self.context.parent_rotations) is not None:
            for value in rotations():
                self.context.slots.setdefault(value, len(self.context.slots))

    def pose(self, out: TreePose) -> None:
        out.clear()
        for value, rotation in (self.context.parent_rotations or dict)().items():
            if (slot := self.context.slots.get(value)) is not None:
                out.values[1][slot], out.weights[slot, 1] = rotation, _ONE


class PlaySpeedNode(FilterNode):
    def control(self) -> None:
        super().control()
        if (speed := _read(self.pin(0))) is not None:
            self.params["Speed"] = F(speed)

    def add(self, delta: F) -> None:
        super().add(F(delta) * F(self.params.get("Speed", 1.0)))


class PlaySpeedScalingNode(PlaySpeedNode):
    def _speed(self) -> F:
        speed = F(self.params.get("Speed", 1.0))
        return _ZERO if speed <= _EPSILON else speed

    def add(self, delta: F) -> None:
        FilterNode.add(self, F(delta) * self._speed())

    def set_frame(self, frame: F) -> None:
        super().set_frame(F(frame) * self._speed())

    def _scaled(self, value: F) -> F:
        speed = self._speed()
        return value / speed if speed != 0 else _ZERO

    def frame(self) -> F:
        return self._scaled(super().frame())

    def prev(self) -> F:
        return self._scaled(super().prev())

    def end(self) -> F:
        return self._scaled(super().end())

    def added(self) -> F:
        return self._scaled(super().added())

    def sync_table(self):
        table = super().sync_table()
        return table if table is not None and self._speed() != 0 else None


class FrameNode(FilterNode):
    def __init__(self, *args):
        super().__init__(*args)
        self.target = F(self.params.get("TargetFrame", 0.0))

    def control(self) -> None:
        super().control()
        if (target := _read(self.pin(0))) is not None:
            target = F(target)
            self.target = min(max(target, _ZERO), _ONE) if self.params.get("NormalizeTimeMode") else target

    def add(self, delta: F) -> None:
        if (child := self.child(0)) is None:
            return
        if not self.params.get("NormalizeTimeMode"):
            child.set_frame(self.target)
        else:
            end = child.end()
            if not end > 0:
                return
            frame = end * self.target
            frame = end - F(1e-04) if end <= frame else frame
            child.set_frame(frame if frame >= 0 else _ZERO)
        child.add(_ZERO)

    def set_frame(self, frame: F) -> None:
        pass

    def frame(self) -> F:
        return _ZERO

    def prev(self) -> F:
        return _ZERO

    def end(self) -> F:
        return _ZERO

    def loop_count(self) -> int:
        return 0

    def state(self) -> int:
        return super().state() | 1


class BlendRateNode(FilterNode):
    def pose(self, out: TreePose) -> None:
        super().pose(out)
        rate = _read(self.pin(0))
        out.weights *= F(self.params.get("BlendRate", 1.0)) if rate is None else F(rate)


class MirrorNode(FilterNode):
    def pose(self, out: TreePose) -> None:
        super().pose(out)
        enabled = _read(self.pin(0), "bool_value")
        if enabled is None or enabled:
            self.context.mirror(out)


class SyncNormalizeTimeNode(FilterNode):
    def add(self, delta: F) -> None:
        child, time = self.child(0), self.context.sync_time
        if child is None:
            return
        if time < 0:
            child.add(delta)
            return
        end = child.end()
        target, current = time * end, child.frame()
        if delta < 0:
            change = (target - end if current < target else target) - current
        else:
            change = target - current if current <= target else (end - current) + target
        child.add(change)


class SwitchNode(Node):
    def __init__(self, *args):
        super().__init__(*args)
        self.active = None

    def init(self) -> None:
        self.active = self.child(1 if self.params.get("Flag") else 0)

    def control(self) -> None:
        super().control()
        if (flag := _read(self.pin(0), "bool_value")) is not None:
            self.active = self.child(1 if flag else 0)

    def coef(self, index: int) -> F:
        return _ONE if self.active is not None and self.child(index) is self.active else _ZERO

    def _ask(self, name: str, default):
        return default if self.active is None else getattr(self.active, name)()

    def add(self, delta: F) -> None:
        if self.active is not None:
            self.active.add(delta)

    def set_frame(self, frame: F) -> None:
        if self.active is not None:
            self.active.set_frame(frame)

    def frame(self) -> F:
        return self._ask("frame", _NONE)

    def prev(self) -> F:
        return self._ask("prev", _NONE)

    def end(self) -> F:
        return self._ask("end", _NONE)

    def over(self) -> F:
        return self._ask("over", _ZERO)

    def added(self) -> F:
        return self._ask("added", _ZERO)

    def loop_count(self) -> int:
        return self._ask("loop_count", 0)

    def set_loop_count(self, count: int) -> None:
        if self.active is not None:
            self.active.set_loop_count(count)

    def state(self) -> int:
        return self._ask("state", 0)

    def is_loop(self) -> bool:
        return self._ask("is_loop", False)

    def sync_table(self):
        return self._ask("sync_table", None)

    def sync_time(self) -> F:
        return self._ask("sync_time", _NONE)

    def set_sync_time(self, time: F) -> None:
        if self.active is not None:
            self.active.set_sync_time(time)

    def pose(self, out: TreePose) -> None:
        if self.active is not None:
            self.active.pose(out)
        else:
            out.clear()


class NumberSwitchNode(SwitchNode):
    def init(self) -> None:
        self.number = -1
        self.switch(self.params.get("No", 0))

    def switch(self, number: int) -> None:
        number = min(max(number, 0), 7)
        if number != self.number:
            self.number = number
            if (child := self.child(number)) is None:
                return
            child.reset_frame()
            child.reset_motion()
        self.active = self.child(self.number)

    def control(self) -> None:
        Node.control(self)
        if (number := _read(self.pin(0), "int_value")) is not None:
            self.switch(number)


class SequentialNode(SwitchNode):
    def init(self) -> None:
        self.at = 0
        self.length = next((index for index, child in enumerate(self.children) if child is None), len(self.children))
        self.active = self.child(0)

    def control(self) -> None:
        Node.control(self)

    def add(self, delta: F) -> None:
        if (child := self.child(self.at)) is None:
            return
        child.add(delta)
        if child.state() & 1 and self.at < self.length - 1:
            self.at += 1
            self.active = self.child(self.at)
            self.active.reset_frame()
            self.active.reset_motion()

    def reset_motion(self) -> None:
        self.at = 0
        self.active = self.child(0)
        super().reset_motion()


class FadeSwitchNode(SwitchNode):
    def init(self) -> None:
        self.index, self.left, self.started, self.flip, self.poses = -1, _ZERO, False, 0, None

    def control(self) -> None:
        Node.control(self)
        number = _read(self.pin(0), "int_value") or 0
        if self.index == number:
            return
        number = 0 if number < 0 else min(number, len(self.children) - 1)
        time = self.active.sync_time() if self.active is not None else _ZERO
        if (child := self.child(number)) is not None:
            child.set_sync_time(time)
            self.started = True
        self.active = child
        frames = F(self.params.get("InterpolationFrame", 0.0))
        self.left = frames if frames > 0 and self.index >= 0 else _ZERO
        self.flip, self.index = self.flip + 1, number

    def add(self, delta: F) -> None:
        if self.started:
            if self.active is not None:
                self.active.add(_ZERO)
                self.active.tick()
            self.started = False
        super().add(delta)
        if self.left != 0:
            self.left = max(self.left - self.context.delta, _ZERO)

    def pose(self, out: TreePose) -> None:
        count = self.context.slot_count()
        if self.poses is None or len(self.poses[0].weights) != count:
            self.poses = TreePose(count), TreePose(count)
        current, previous = self.poses[self.flip & 1], self.poses[(self.flip - 1) & 1]
        current.clear()
        if self.active is not None:
            self.active.pose(current)
        if self.left != 0:
            rate = self.left / F(self.params.get("InterpolationFrame", 1.0))
            blend_interpolation(current, previous, min(max(rate, _ZERO), _ONE), self.context.rows(True),
                                self.context.slerped())
        out.copy_from(current)


class LayerChangeNode(Node):
    def init(self) -> None:
        self.changes, self.clock, self.at, self.left = self.params.get("Changes", ()), _ZERO, -1, _ZERO
        self.old = self.snapshot = None

    def coef(self, index: int) -> F:
        return _ONE if index == self.at else _ZERO

    def _advance(self, delta: F) -> None:
        for node in (self.child(self.at), self.old):
            if node is not None:
                node.add(delta)
        self.left = max(self.left - delta, _ZERO)

    def add(self, delta: F) -> None:
        end = self.clock + F(delta)
        while self.at + 1 < len(self.changes) and F(self.changes[self.at + 1][0]) <= end:
            frame, frames, mode, _curve, *wrap = self.changes[self.at + 1]
            self._advance(max(F(frame) - self.clock, _ZERO))
            self.clock, self.old, self.snapshot = max(F(frame), self.clock), None, None
            if mode and frames > 0 and self.at >= 0:
                if mode in (2, 3, 4):
                    self.old = self.child(self.at)
                else:
                    for node in self.motion_nodes():
                        if node.motion is not None:
                            self.context.sampler(node.motion)
                    self.snapshot = TreePose(self.context.slot_count())
                    self.pose(self.snapshot)
            self.at, self.left = self.at + 1, F(frames) if mode else _ZERO
            if wrap and wrap[0] and (node := self.child(self.at)) is not None:
                node.wrap = wrap[0]
        self._advance(end - self.clock)
        self.clock = end

    def pose(self, out: TreePose) -> None:
        if (active := self.child(self.at)) is None:
            out.clear()
            return
        active.pose(out)
        source = self.snapshot
        if self.old is not None:
            self.old.pose(source := self.context.scratch(id(self)))
        if source is not None and self.left > 0:
            _frame, frames, _mode, curve, *_wrap = self.changes[self.at]
            x = _ONE - (F(frames) - self.left) / (F(frames) + _EPSILON)
            rate = (x * _ZERO - x * x * x * _TWO) + x * x * _THREE if curve == 1 else \
                (x * x * x + x * _THREE) - x * x * _THREE if curve == 2 else x * x * x if curve == 3 else x
            blend_interpolation(out, source, rate, self.context.rows(), self.context.slerped())


class BlendNode(Node):
    def rate(self) -> F:
        return min(max(F(self.params.get("BlendRate", 0.0)), _ZERO), _ONE)

    def control(self) -> None:
        super().control()
        if (rate := _read(self.pin(0))) is not None:
            self.params["BlendRate"] = min(max(F(rate), _ZERO), _ONE)

    def coef(self, index: int) -> F:
        return _ONE - self.rate() if index == 0 else self.rate()

    def add(self, delta: F) -> None:
        for child in self.present():
            child.add(delta)

    def _active(self):
        if self.child(0) is None:
            return self.child(1)
        return self.child(1) if self.child(1) is not None and self.rate() >= F(0.5) else self.child(0)

    def _ask(self, name: str, default):
        active = self._active()
        return default if active is None else getattr(active, name)()

    def frame(self) -> F:
        return self._ask("frame", _ZERO)

    def prev(self) -> F:
        return self._ask("prev", _ZERO)

    def end(self) -> F:
        return self._ask("end", _NONE)

    def loop_count(self) -> int:
        return self._ask("loop_count", 0)

    def state(self) -> int:
        return self._ask("state", 0)

    def is_loop(self) -> bool:
        return any(child.is_loop() for child in self.present())

    def sync_time(self) -> F:
        end = self.end()
        return self.frame() / end if end > 0 else _NONE

    def pose(self, out: TreePose) -> None:
        rate, first, second = self.rate(), self.child(0), self.child(1)
        if rate == 0 or second is None:
            if first is not None:
                first.pose(out)
            return
        if rate == 1 or first is None:
            second.pose(out)
            return
        first.pose(out)
        other = self.context.scratch(id(self))
        second.pose(other)
        blend_normal(out, other, rate, self.context.rows(), self.context.slerped(),
                     bool(self.params.get("RootTransPolarBlend")))


class SyncBlendNode(BlendNode):
    def init(self) -> None:
        self.count = next((index for index, child in enumerate(self.children) if child is None), len(self.children))
        self.frames, self.pair, self.indices, self.segment = SyncBlendFrames(), (None, None), (-1, -1), _ZERO
        self.blended = None
        self.update_blend()

    def update_blend(self) -> None:
        if self.count == 0:
            return
        if self.count == 1:
            self.pair, self.indices, self.segment = (self.child(0), None), (0, -1), _ZERO
        else:
            value = self.rate() * F(self.count if self.params.get("CircledBlend") else self.count - 1)
            index = 0
            while value > _ONE:
                value, index = value - _ONE, index + 1
            second = (index + 1) % self.count
            self.pair, self.indices, self.segment = (self.child(index), self.child(second)), (index, second), value
        self.blended = self.rate()
        self.frames.update(self.pair, self.segment)

    def control(self) -> None:
        super().control()
        if self.blended != self.rate():
            self.update_blend()

    def coef(self, index: int) -> F:
        return (_ONE - self.segment if index == self.indices[0] else self.segment if index == self.indices[1]
                else _ZERO)

    def add(self, delta: F) -> None:
        self.frames.add(self.pair, F(delta))

    def set_frame(self, frame: F) -> None:
        self.frames.set_frame(self.pair, frame)

    def frame(self) -> F:
        return self.frames.frame

    def prev(self) -> F:
        return self.frames.prev

    def end(self) -> F:
        return self.frames.end

    def over(self) -> F:
        return self.frames.over

    def loop_count(self) -> int:
        return self.frames.loops

    def set_loop_count(self, count: int) -> None:
        self.frames.loops = count

    def state(self) -> int:
        return self.frames.state

    def sync_table(self):
        return self.frames.merged if self.frames.valid else None

    def sync_time(self) -> F:
        return F(self.frames.index) + self.frames.through

    def set_sync_time(self, time: F) -> None:
        time = F(time)
        self.frames.index, self.frames.through = int(time), time - F(int(time))
        self.frames.frame = self.frames.merged.frame_at(self.frames.merged.play_block, self.frames.index,
                                                        self.frames.through, self.frames.end)

    def dynamic_sync(self) -> bool:
        return self.frames.merged.dynamic or any(child.dynamic_sync() for child in self.pair if child is not None)

    def reset_frame(self) -> None:
        super().reset_frame()
        self.frames.reset()

    def tick(self) -> None:
        for child in self.present():
            child.tick()
        self.frames.prev = self.frames.frame

    def pose(self, out: TreePose) -> None:
        first, second = self.pair
        rate = _ONE if first is None and second is not None else self.segment
        if (first is not None and second is None) or rate == 0:
            if first is not None:
                first.pose(out)
            if second is not None:
                second.tick()
        elif rate == 1:
            second.pose(out)
            if first is not None:
                first.tick()
        else:
            first.pose(out)
            other = self.context.scratch(id(self))
            second.pose(other)
            blend_normal(out, other, rate, self.context.rows(), self.context.slerped(),
                         bool(self.params.get("RootTransPolarBlend")))
        self.frames.prev = self.frames.frame


class AddBlendNode(BlendNode):
    def coef(self, index: int) -> F:
        return _ONE if index == 0 else self.rate()

    def _ask(self, name: str, default):
        base = self.child(self.params.get("BaseNo", 0))
        return default if base is None else getattr(base, name)()

    def pose(self, out: TreePose) -> None:
        first, second, rate = self.child(0), self.child(1), self.rate()
        if first is not None:
            first.pose(out)
        else:
            out.clear()
        if rate != 0:
            other = self.context.scratch(id(self))
            if second is not None:
                second.pose(other)
            else:
                other.clear()
            blend_add(out, other, rate, self.context.rows(True), self.context.rest)


class SyncAddBlendNode(AddBlendNode):
    def init(self) -> None:
        self.frames = SyncBlendFrames()
        self.frames.update((self.child(0), self.child(1)), _ZERO)

    def add(self, delta: F) -> None:
        self.frames.add((self.child(0), self.child(1)), F(delta))

    def set_frame(self, frame: F) -> None:
        self.frames.set_frame((self.child(0), self.child(1)), frame)


class LayerBlendNode(AddBlendNode):
    def coef(self, index: int) -> F:
        return _ONE if index == self.params.get("BaseNo", 0) else self.rate()

    def pose(self, out: TreePose) -> None:
        first, second = self.child(0), self.child(1)
        if first is None or second is None:
            out.clear()
            return
        first.pose(out)
        if self.rate() != 0:
            other = self.context.scratch(id(self))
            second.pose(other)
            blend_layer_alpha(out, other, self.rate(), self.context.rows())


class MultiLayerBlendNode(BlendNode):
    def _weight(self, index: int) -> F:
        given = _read(self.pin(index))
        return _ZERO if given is None else min(max(F(given), _ZERO), _ONE)

    def coef(self, index: int) -> F:
        return self._weight(index) if index < 16 else _ZERO

    def control(self) -> None:
        Node.control(self)

    def _ask(self, name: str, default):
        first = next(iter(self.present()), None)
        return default if first is None else getattr(first, name)()

    def is_loop(self) -> bool:
        return self._ask("is_loop", False)

    def pose(self, out: TreePose) -> None:
        other = self.context.scratch(id(self))
        for index, child in enumerate(self.children):
            if child is None:
                continue
            weight = self._weight(index)
            if weight <= 0:
                child.tick()
                continue
            child.pose(other)
            if self.params.get("BlendMode") == 1:
                blend_add(out, other, weight, self.context.rows(True), self.context.rest)
            else:
                blend_layer_alpha(out, other, weight, self.context.rows())


class BasePoseNode(Node):
    """On the first layer it sets nothing: the engine leaves its pooled buffer as it was."""

    def pose(self, out: TreePose) -> None:
        out.clear()
        base, rest = self.context.base, self.context.rest
        if base is None:
            return
        group = self.context.groups.get(self.params.get("JointMaskID", 0))
        bits = np.full(len(out.weights), 7)
        if group is not None:
            held = dict(zip(group[0].tolist(), group[1].tolist()))
            bits = np.array([held.get(value, 0) for value in self.context.slots], np.int64)
        for component in range(3):
            rows = np.flatnonzero(bits & (1 << component))
            out.values[component][rows] = np.where((base.weights[rows, component] != 0)[:, None],
                                                   base.values[component][rows], rest[component][rows])
            out.weights[rows, component] = _ONE


class SequentialPoseBlendNode(Node):
    """Frames weighed by node parameters (blob mode 1) need the game to set them, so none add here; the class's own
    control-joint mode ships in no tree."""

    def init(self) -> None:
        blob = self.params.get("Blob")
        self.blend = PoseBlend.of(blob) if blob else None

    def pose(self, out: TreePose) -> None:
        if self.context.base is None:
            for mine, rest in zip(out.values, self.context.rest):
                mine[:] = rest
            out.weights[:] = 0
        else:
            out.copy_from(self.context.base)
        self.add_frames(out)

    def add_frames(self, out: TreePose) -> None:
        pass


class FastSequentialPoseBlendNode(SequentialPoseBlendNode):
    def init(self) -> None:
        super().init()
        found = None if self.blend is None or self.blend.parameters else self.context.motion_of(self.blend.bank,
                                                                                               self.blend.motion)
        self.frames = None
        self.mirrored = found is not None and self.context.mirror_of(self.blend.bank, self.blend.motion)
        if found is not None:
            self.context.sampler(found)
            self.controls = np.array([self.context.slots.setdefault(value, len(self.context.slots))
                                      for value in self.blend.controls], np.int64)
            self.frames = PoseFrames.of_motion(found, tuple(self.context.slots))

    def control_values(self) -> np.ndarray:
        source = self.context.base
        if (child := self.child(0)) is not None:
            source = self.context.scratch(id(self))
            child.pose(source)
        values = np.zeros((len(self.controls), 3), np.float32)
        if source is not None:
            values[:] = source.values[0][self.controls]
        return values

    def add_frames(self, out: TreePose) -> None:
        if self.frames is None:
            return
        self.frames.add(out, self.blend.frames(self.control_values()), self.blend.limit)
        if self.mirrored:
            self.context.mirror(out)


class InnerFacialBlendNode(FastSequentialPoseBlendNode):
    def init(self) -> None:
        SequentialPoseBlendNode.init(self)
        self.frames, self.mirrored = None, False
        if self.blend is None or self.blend.parameters or not self.blend.poses:
            return
        poses = JointPoses.of(self.params["Blob"], self.blend.poses)
        mode, mask_id = self.params.get("JointMaskIDMode", 0), self.params.get("JointMaskID", 0)
        groups = [self.context.groups.get(mask_id) if mode == 0 else None,
                  self.context.groups.get(0) if mode != 0 or mask_id else None]
        keep, masks = [], []
        for joint, value in enumerate(poses.hashes):
            if (bits := _joint_mask(value, groups)) and value in self.context.rest_of:
                keep.append(joint)
                masks.append([bool(bits & 1 << component) for component in range(3)])
        self.frames = PoseFrames(
            np.array([self.context.slots.setdefault(poses.hashes[joint], len(self.context.slots)) for joint in keep],
                     np.int64),
            [part[:, keep] for part in (poses.translations, poses.quaternions, poses.scales)],
            [poses.held[:, keep, component] for component in range(3)],
            None if poses.vectors is None else poses.vectors[:, keep], np.array(masks, bool).reshape(-1, 3))
        self.adds = poses.adds
        self.controls = np.array([self.context.slots.setdefault(value, len(self.context.slots))
                                  for value in self.blend.controls], np.int64)

    def add_frames(self, out: TreePose) -> None:
        if self.frames is None:
            return
        limit = _read(self.pin(0), "int_value")
        limit = self.blend.limit if limit is None else limit
        adds = self.adds
        if adds == "Switch":
            adds = "ExponentialMap" if _read(self.pin(1), "bool_value") else None
        self.frames.add(out, self.blend.frames(self.control_values(), limit), limit, adds)


def _joint_mask(value: int, groups: list) -> int:
    if groups[0] is None and groups[1] is None:
        return 7
    bits, weight = 0xFF, 1.0
    for index, group in enumerate(groups):
        if group is None:
            continue
        found = np.flatnonzero(group[0] == value)
        if not len(found):
            return 0
        bits, weight = bits & int(group[1][found[0]]), weight * float(group[2][found[0]])
        if index == 0 and not bits:
            return 0
    return bits & 7 if bits and weight > 0 else 0


MOTION_NODES = {cls.__name__: cls for cls in (
    MotionNode, SelectMotionNode, PlaySpeedNode, PlaySpeedScalingNode, FrameNode, BlendRateNode, MirrorNode,
    SyncNormalizeTimeNode, SwitchNode, NumberSwitchNode, SequentialNode, BlendNode, SyncBlendNode, AddBlendNode,
    SyncAddBlendNode, LayerBlendNode, MultiLayerBlendNode, BasePoseNode, RotationGlobalOffsetNode,
    ParentLocalConstraintsNode, SequentialPoseBlendNode, FastSequentialPoseBlendNode, InnerFacialBlendNode,
    LayerChangeNode)} | {
    "RootNode": FilterNode, "OctSyncBlendNode": SyncBlendNode, "HexSyncBlendNode": SyncBlendNode,
    "SyncFadeSwitchNode": FadeSwitchNode, "OctSyncFadeSwitchNode": FadeSwitchNode,
    # A filter whose C# method then gets the pose: game code, not run.
    "ScriptCallNode": FilterNode,
    # FastSequentialPoseBlendNode's vtable but for its type info.
    "FastFacialBlendNode": FastSequentialPoseBlendNode}


def node_params(node: TreeNode) -> dict:
    kinds, names = tree_param_kinds(), tree_param_names()
    return {"Blob" if kinds[param.type] == "blob" else param.name or names.get(param.key, f"0x{param.key:08X}"):
            param.value for param in node.params or ()}


def motion_choices(node: TreeNode) -> list[tuple[int, int]]:
    params = node_params(node)
    if node.class_name != "SelectMotionNode":
        return [(params.get("BankID", 0), params.get("MotionID", 0))]
    table = params.get("BankSelectMode") == 1
    return [(params.get(f"No{index:02d}_BankID", 0) if table else params.get("BankID", 0),
             params[f"No{index:02d}_MotionID"]) for index in range(16) if f"No{index:02d}_MotionID" in params]


class TreeRuntime:
    """A class it has no rules for (``unknown``) makes no node, as the engine does for classes it lacks (DMC5's
    MotionTreeNode, UpperBodyBlendNode)."""

    def __init__(self, tree: MotionTree, context: TreeContext):
        self.tree, self.context = tree, context
        context.version = tree.version
        made: dict[int, ParamNode | Node] = {}
        self.unknown = sorted({node.class_name for node in tree.nodes
                               if node.class_name not in MOTION_NODES and node.class_name not in PARAM_NODES})
        for node in tree.nodes:
            if kind := MOTION_NODES.get(node.class_name) or PARAM_NODES.get(node.class_name):
                made[id(node)] = kind(node, node_params(node), context)
        for link in tree.links:
            parent, child = made.get(id(link.parent)), made.get(id(link.child))
            if link.type == 1 and isinstance(parent, Node) and isinstance(child, Node):
                parent.children.extend([None] * (link.pin + 1 - len(parent.children)))
                parent.children[link.pin] = child
            elif link.type == 2 and parent is not None and isinstance(child, ParamNode):
                parent.pins[link.pin] = child, link.guid if isinstance(child, VariablesNode) and link.guid \
                    else link.child_pin
        self.root = FilterNode(tree.root, {}, context)
        top = None if tree.root is None else made.get(id(tree.root))
        self.root.children = [top] if isinstance(top, Node) else []
        self.nodes = made
        self._init(self.root, set())
        self.time = _ZERO

    def _init(self, node: Node, done: set) -> None:
        """Children first: a parent reads their end frames."""
        if id(node) in done:
            return
        done.add(id(node))
        for child in node.present():
            self._init(child, done)
        node.init()

    def step(self, delta: float) -> None:
        delta = F(delta)
        self.context.delta = delta
        self.root.update_state()
        self.root.control()
        self.root.update_weight(_ONE)
        self.root.add(delta)
        self.time += delta

    def pose(self) -> TreePose:
        for node in self.root.motion_nodes():
            if node.motion is not None:
                self.context.sampler(node.motion)
        out = TreePose(self.context.slot_count())
        self.root.pose(out)
        return out

    def leading(self) -> MotionNode | None:
        best = None
        for node in self.root.motion_nodes():
            if node.motion is not None and (best is None or node.weight > best.weight):
                best = node
        return best
