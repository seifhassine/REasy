"""See docs/internals/startup-draw-states.md, Time after the start."""
from __future__ import annotations

import time as _clock
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field

lazy import numpy as np

lazy from file_handlers.engine_math import (
    CurveRule, axis_turn, chain_rotation, chain_world, crt, curve_value, make_trs_matrix, normalized,
    quaternion_product, transform_product, turned_vector,
)

# A curve animator group's routines (chainsaw.ParamCurveAnimator.GroupData.RoutineType).
_INIT, _STOPPED, _START, _LOOP = 0, 1, 2, 3
_WORD = 0xFFFFFFFF


def _linear(start: np.float32, end: np.float32, t: np.float32, _held: Mapping) -> np.float32:
    return (end - start) * t + start


def _sine_in(start: np.float32, end: np.float32, t: np.float32, held: Mapping) -> np.float32:
    f = np.float32
    change = end - start
    return change - f(crt("cosf")(float(t * f(held["quarter turn"])))) * change + start


def _bounce_out(start: np.float32, end: np.float32, t: np.float32, held: Mapping) -> np.float32:
    f = np.float32
    change, scale = end - start, f(held["scale"])
    if f(held["first"]) > t:
        return t * scale * t * change + start
    _bound, back, lift = next(band for band in held["bands"] if band[0] is None or f(band[0]) > t)
    t = t + f(back)
    return (t * scale * t + f(lift)) * change + start


def _bounce_in_out(start: np.float32, end: np.float32, t: np.float32, held: Mapping) -> np.float32:
    f = np.float32
    change, twice = end - start, t * f(2)
    if f(0.5) > t:
        return (change - _bounce_out(f(0), change - f(0), f(1) - twice, held) + f(0)) * f(0.5) + start
    return _bounce_out(f(0), change, twice - f(1), held) * f(0.5) + change * f(0.5) + start


# The rules' eases as via.curve.EaseType's, each (start, end, t, constants), in single precision in the game's order of
# operations.
EASES: Mapping[str, Callable] = {"linear": _linear, "sine in": _sine_in, "bounce in out": _bounce_in_out}


@dataclass(frozen=True, slots=True)
class Ease:
    name: str
    constants: frozendict = frozendict()

    @classmethod
    def of(cls, name: str, constants: Mapping) -> Ease:
        def frozen(value):
            return tuple(map(frozen, value)) if isinstance(value, list) else value
        return cls(name, frozendict({key: frozen(value) for key, value in constants.items()}))

    def __call__(self, start: np.float32, end: np.float32, t: np.float32) -> np.float32:
        return EASES[self.name](start, end, t, self.constants)


@dataclass(frozen=True, slots=True)
class RandomRule:
    key: int
    start: int
    mix: int
    mask: int
    mix_shifts: tuple[int, int]
    rounds: int
    shifts: tuple[int, int, int]
    unit: float

    @classmethod
    def of(cls, held: Mapping) -> RandomRule:
        return cls(*(int(held[name], 0) for name in ("seed key", "seed start", "seed mix")), int(held["seed mask"]),
                   tuple(held["seed shifts"]), int(held["seed rounds"]), tuple(held["step shifts"]),
                   float(held["unit"]))


class Xorshift128:
    """The engine's random state, which every draw of every system steps."""
    __slots__ = ("words", "rule")

    def __init__(self, words: Iterable[int], rule: RandomRule):
        self.words, self.rule = [int(word) & _WORD for word in words], rule

    @classmethod
    def seeded(cls, seed: int, rule: RandomRule) -> Xorshift128:
        """As the game seeds it from one word (its time-stamp counter's low word at start)."""
        one, two = rule.mix_shifts
        mixed, last, words = (seed ^ rule.key) & _WORD, rule.start, []
        for _ in range(rule.rounds):
            mixed ^= rule.mix >> (mixed & rule.mask)
            mixed = (mixed ^ ((mixed >> one ^ mixed) >> two) ^ ((mixed << one ^ mixed) << two)) & _WORD
            last = _step(last, mixed, rule.shifts)
            words.append(last)
        return cls(words[-4:], rule)

    @classmethod
    def from_clock(cls, rule: RandomRule) -> Xorshift128:
        return cls.seeded(_clock.perf_counter_ns() & _WORD, rule)

    def unit(self) -> np.float32:
        first, *rest = self.words
        self.words = [*rest, _step(rest[-1], first, self.rule.shifts)]
        return np.float32(self.words[-1]) * np.float32(self.rule.unit)

    def between(self, low: float, high: float) -> np.float32:
        f = np.float32
        return self.unit() * (f(high) - f(low)) + f(low)


def _step(last: int, word: int, shifts: tuple[int, int, int]) -> int:
    first, second, third = shifts
    drawn = (word << first ^ word) & _WORD
    return ((last >> second ^ drawn) >> third) ^ last ^ drawn


def _quotient(time: np.float32, duration: np.float32) -> np.float32:
    return np.float32(float(time) / float(duration))


def radians(degrees, factors) -> np.float32:
    angle = np.float32(degrees)
    for factor in factors:
        angle = np.float32(angle * np.float32(factor))
    return angle


@dataclass(frozen=True, slots=True)
class CurveWork:
    time: float
    keys: tuple = ()
    clamp: bool = False
    minimum: float = 0.0
    maximum: float = 1.0
    ease: Ease | None = None
    start: float = 0.0
    end: float = 0.0
    random_time: tuple[float, float] | None = None
    spans: tuple[float, float] | None = None
    curve: CurveRule | None = None

    def rate(self, counter: np.float32, time: np.float32 | None = None) -> np.float32:
        f = np.float32
        duration = f(self.time if time is None else time)
        if self.ease is not None:
            return self.ease(f(self.start), f(self.end), _quotient(counter, duration)) if duration > 0 \
                else f(self.start)
        return self.curve_rate(_quotient(counter, duration) if duration > 0 else f(0))

    def curve_rate(self, t: np.float32) -> np.float32:
        f = np.float32
        t = f(0) if f(0) > t else t if f(1) > t else f(1)
        span = self.keys[-1][2] if len(self.keys) > 1 else f(0)
        return curve_value(self.keys, f(float(span) * float(t)), self.clamp, self.minimum, self.maximum, self.curve)


@dataclass(frozen=True, slots=True)
class CurveProcess:
    target: object
    minimum: float
    maximum: float
    material: str = ""
    variable: str = ""
    axis: tuple[float, float, float] | None = None
    offset: tuple[float, float, float, float] | None = None
    radians: tuple[float, ...] | None = None

    def value(self, rate: np.float32) -> np.float32:
        low, high = float(np.float32(self.minimum)), float(np.float32(self.maximum))
        return np.float32((high - low) * float(rate) + low)

    def rotation(self, rate: np.float32) -> tuple[float, float, float, float]:
        turn = axis_turn(self.axis, radians(self.value(rate), self.radians) * np.float32(0.5))
        return turn if self.offset is None else quaternion_product(self.offset, turn)


@dataclass(frozen=True, slots=True)
class UnitProcess(CurveProcess):
    """Its span is held in single precision first."""

    def value(self, rate: np.float32) -> np.float32:
        f = np.float32
        low = float(f(self.minimum))
        return f(float(rate) * float(f(float(f(self.maximum)) - low)) + low)


@dataclass(frozen=True, slots=True)
class CurveAnimation:
    owner: object
    start: CurveWork | None
    loop: CurveWork | None
    processes: tuple[CurveProcess, ...]
    least_time: float = 0.0

    def play(self, random: Xorshift128 | None) -> CurvePlay:
        return CurvePlay(self, random)


@dataclass(frozen=True, slots=True)
class LightRise:
    owner: object
    processes: tuple[UnitProcess]
    time: float
    ease: Ease
    delay: int = 0
    before: float | None = None

    def play(self, _random: Xorshift128 | None) -> RisePlay:
        return RisePlay(self)


@dataclass(frozen=True, slots=True)
class LightBlink:
    owner: object
    processes: tuple[CurveProcess]
    cycle: float
    full_turn: float
    quarter_turn: float
    delay: int = 0
    before: float | None = None

    def play(self, _random: Xorshift128 | None) -> BlinkPlay:
        return BlinkPlay(self)


class RandomCount:
    def __init__(self, work: CurveWork, random: Xorshift128):
        self.ease, self.span_range, self.random = work.ease, work.spans, random
        self.values = [random.unit() for _ in range(3)]
        self.spans = [random.between(*work.spans) for _ in range(2)]
        self.counter = np.float32(0)

    def count(self, seconds: float) -> np.float32:
        f = np.float32
        self.counter = f(seconds) + self.counter
        first = self.spans[0]
        if not first < self.counter:
            return self.ease(self.values[0], self.values[1], self.counter / first)
        t = (self.counter - first) / self.spans[1]
        if f(1) < t:
            self.counter, t = first, f(0)
        rate = self.ease(self.values[1], self.values[2], t)
        self.counter = self.counter - first
        self.values = [self.values[1], self.values[2], self.random.unit()]
        self.spans = [self.spans[1], self.random.between(*self.span_range)]
        return rate


class _Made:
    __slots__ = ("work", "time", "random")

    def __init__(self, work: CurveWork, random: Xorshift128 | None):
        self.work = work
        self.time = np.float32(work.time) if work.random_time is None else random.between(*work.random_time)
        self.random = None if work.spans is None else RandomCount(work, random)

    def rate(self, counter: np.float32, seconds: float) -> np.float32:
        return self.work.rate(counter, self.time) if self.random is None else self.random.count(seconds)


class CurvePlay:
    def __init__(self, animation: CurveAnimation, random: Xorshift128 | None = None):
        self.animation = animation
        self.routine = _INIT
        self.counter = np.float32(0)
        self.works: dict[str, _Made] = {}
        for phase, work in (("start", animation.start), ("loop", animation.loop)):
            if work is not None:
                made = _Made(work, random)
                if made.time > np.float32(animation.least_time):
                    self.works[phase] = made

    @property
    def running(self) -> bool:
        return self.routine != _STOPPED

    def write(self, seconds: float, writes: FrameWrites) -> None:
        rate = self.step(seconds)
        if rate is None:
            return
        for process in self.animation.processes:
            if process.variable:
                writes.materials[process.target, process.material, process.variable] = float(process.value(rate))
            elif process.axis is not None:
                writes.rotations[process.target] = process.rotation(rate)

    def step(self, seconds: float) -> np.float32 | None:
        f = np.float32
        works = self.works
        self.counter = f(float(self.counter) + float(f(seconds)))
        if self.routine == _INIT:
            self.counter = f(0)
            self.routine = _START if "start" in works else _LOOP if "loop" in works else _STOPPED
            return None
        if self.routine == _STOPPED:
            self.counter = f(0)
            return None
        made = works["start" if self.routine == _START else "loop"]
        if not float(made.time) > float(self.counter):
            self.counter = f(float(self.counter) - float(made.time))
            if self.routine == _START:
                if "loop" not in works:
                    self.routine = _STOPPED
                    return None
                self.routine, made = _LOOP, works["loop"]
        return made.rate(self.counter, seconds)


class RisePlay:
    def __init__(self, animation: LightRise):
        self.animation = animation
        self.waited = 0
        self.counter = np.float32(0)
        self.written = np.float32(0)

    @property
    def running(self) -> bool:
        return self.counter < np.float32(self.animation.time)

    def write(self, seconds: float, writes: FrameWrites) -> None:
        if (value := self.step(seconds)) is not None:
            process, = self.animation.processes
            writes.materials[process.target, process.material, process.variable] = float(value)

    def step(self, seconds: float) -> np.float32 | None:
        if self.waited < self.animation.delay:
            self.waited += 1
            return np.float32(self.animation.before) if self.waited == 1 else None
        value = self.move(seconds)
        if value == self.written:
            return None
        self.written = value
        return value

    def move(self, seconds: float) -> np.float32:
        f = np.float32
        time = f(self.animation.time)
        self.counter = min(f(seconds) + self.counter, time)
        rate = self.animation.ease(f(0), f(1), _quotient(self.counter, time))
        return self.animation.processes[0].value(rate)


class BlinkPlay(RisePlay):
    @property
    def running(self) -> bool:
        return True

    def move(self, seconds: float) -> np.float32:
        f, animation = np.float32, self.animation
        cycle = f(animation.cycle)
        self.counter = f(seconds) + self.counter
        if cycle < self.counter:
            self.counter = self.counter - cycle
        process, = animation.processes
        low = float(f(process.minimum))
        angle = float(self.counter) / float(cycle) * animation.full_turn - animation.quarter_turn
        sine = f(crt("sinf")(float(f(angle))))
        return f((float(sine) + 1.0) * (float(f(process.maximum)) - low) * 0.5 + low)


@dataclass(frozen=True, slots=True)
class TimelineKeys:
    begin: float
    end: float
    keys: tuple[tuple[float, float], ...]


@dataclass(frozen=True, slots=True)
class TimelineTrack:
    target: str
    field: str
    start: tuple[float, ...]
    components: tuple[TimelineKeys | None, ...]


@dataclass(frozen=True, slots=True)
class TimelineAnimation:
    owner: object
    start: float
    end: float
    speed: float
    event: bool
    fps: float
    scale: float
    tracks: tuple[TimelineTrack, ...]
    first_back: float
    zero_back: float

    def play(self, _random: Xorshift128 | None) -> TimelinePlay:
        return TimelinePlay(self)


class TimelinePlay:
    def __init__(self, animation: TimelineAnimation):
        self.animation = animation
        self.state = 0
        self.frame = self.previous = np.float32(animation.start)
        self.values = {track: [np.float32(value) for value in track.start] for track in animation.tracks}

    @property
    def running(self) -> bool:
        return self.state in (0, 1)

    def write(self, seconds: float, writes: FrameWrites) -> None:
        for track in self.step(seconds):
            writes.fields[track.target, track.field] = tuple(float(value) for value in self.values[track])

    def step(self, seconds: float) -> list[TimelineTrack]:
        f, animation = np.float32, self.animation
        frames = f(seconds) * f(animation.fps) * f(animation.scale)
        start, end = f(animation.start), f(animation.end)
        if self.state == 0:
            self.frame = self.previous = start
            self.add(frames)
            self.previous = self.previous - f(animation.first_back)
            written = self.evaluate()
            self.previous, self.state = self.frame, 1
            return written
        if self.state != 1:
            return []
        if self.frame < end:
            self.add(frames)
        else:
            self.frame = self.previous = start
        written = []
        if self.previous <= self.frame:
            if self.previous == 0:
                self.previous = self.previous - f(animation.zero_back)
            written = self.evaluate()
        self.previous = self.frame
        if end <= start:
            self.state = 4
        return written

    def add(self, frames: np.float32) -> None:
        f, animation = np.float32, self.animation
        start, end = f(animation.start), f(animation.end)
        if start >= end:
            self.frame, self.state = start, 4
            return
        self.frame = frames * f(animation.speed) + self.frame
        if self.frame >= end:
            self.frame, self.state = end, 6
        elif start > self.frame:
            self.frame, self.state = start, 3

    def evaluate(self) -> list[TimelineTrack]:
        written = []
        for track in self.animation.tracks:
            values, changed = self.values[track], False
            for index, keys in enumerate(track.components):
                if keys is not None and (value := self.event(keys)) is not None:
                    values[index], changed = value, True
            if changed:
                written.append(track)
        return written

    def event(self, keys: TimelineKeys) -> np.float32 | None:
        f = np.float32
        previous, frame, begin, end = self.previous, self.frame, f(keys.begin), f(keys.end)
        if previous >= frame:
            if frame < begin or end < frame:
                return None
        elif begin > previous and begin > frame or previous > end and frame > end:
            return None
        at = frame if frame <= end else end
        reached = [key for key in keys.keys if f(key[0]) <= at]
        if not self.animation.event or not reached:
            return None
        key_frame, value = f(reached[-1][0]), reached[-1][1]
        return f(value) if previous < key_frame or previous == 0 or at == key_frame else None


@dataclass(frozen=True, slots=True)
class SimpleBody:
    target: object
    position: tuple[float, float, float]
    rotation: tuple[float, float, float, float]
    scale: tuple[float, float, float]
    parent_world: np.ndarray | None = None
    parent_rotation: tuple[float, float, float, float] | None = None
    joint_parents: tuple[int, ...] = ()
    joint_positions: tuple[tuple[float, float, float], ...] = ()
    joint_rotations: tuple[tuple[float, float, float, float], ...] = ()
    joint_scales: tuple[tuple[float, float, float], ...] = ()


@dataclass(frozen=True, slots=True)
class FanParam:
    """Speeds are in degrees a second."""
    joint: int | None
    axis: tuple[float, float, float]
    speed: float
    ease: float
    random: bool = False
    speed_range: tuple[float, float] = (0.0, 0.0)
    cycle_range: tuple[float, float] = (0.0, 0.0)


@dataclass(frozen=True, slots=True)
class PendulumParam:
    joint: int | None
    axis: tuple[float, float, float]
    length: float
    degree: float
    gravity: float
    random: bool = False
    degree_range: tuple[float, float] = (0.0, 0.0)
    cycle_range: tuple[float, float] = (0.0, 0.0)
    infinite: bool = False
    per_second: float = 0.0
    twist: bool = False
    twist_axis: tuple[float, float, float] = (0.0, 0.0, 0.0)
    twist_start: float = 0.0
    twist_range: tuple[float, float] = (0.0, 0.0)
    twist_period_range: tuple[float, float] = (0.0, 0.0)


@dataclass(frozen=True, slots=True)
class SimpleAnimation:
    owner: object
    body: SimpleBody
    position: tuple[float, float, float]
    params: tuple[FanParam | PendulumParam, ...]
    distance: float
    powers: tuple[float, float]
    ease: float
    least: float
    start_power: float
    near_power: float
    far_power: float
    full_turn: float
    radians: tuple[float, ...]

    @property
    def turned(self) -> bool:
        return any(param.joint is None for param in self.params)

    @property
    def posed(self) -> bool:
        return any(param.joint is not None for param in self.params)

    def play(self, random: Xorshift128 | None) -> SimplePlay:
        return SimplePlay(self, random)


@dataclass(slots=True)
class FrameWrites:
    materials: dict
    rotations: dict
    fields: dict
    joints: dict = field(default_factory=dict)
    camera: tuple[float, float, float] | None = None


_IDENTITY_TURN = (0.0, 0.0, 0.0, 1.0)


def _frequency(period: np.float32, turn: float) -> np.float32:
    return np.float32(turn / float(period)) if period > 0 else np.float32(turn)


def _unit_turn(rotation, order) -> tuple[float, float, float, float]:
    """A rotation over its length, its squares summed in ``order``'s order (indices of x, y, z, w)."""
    f = np.float32
    values = [f(value) for value in rotation]
    a, b, c, d = (values[index] for index in order)
    inverse = f(1) / np.sqrt(((a * a + b * b) + c * c) + d * d)
    return tuple(float(value * inverse) for value in values)


_YX = (1, 0, 2, 3)


def _inverse_turn(rotation, order) -> tuple[float, float, float, float]:
    """The inverse, its squares summed in ``order``'s order; the identity for none."""
    f = np.float32
    values = [f(value) for value in rotation]
    a, b, c, d = (values[index] for index in order)
    squared = ((a * a + b * b) + c * c) + d * d
    if not squared > 0:
        return _IDENTITY_TURN
    inverse = f(1) / squared
    x, y, z, w = values
    return float(-x * inverse), float(-y * inverse), float(-z * inverse), float(w * inverse)


def _conjugate(rotation) -> tuple[float, float, float, float]:
    x, y, z, w = rotation
    return -x, -y, -z, w


def _normal_through(matrix: np.ndarray, vector) -> np.ndarray:
    """``via.MathEx.multiplyNormal`` (column vectors here)."""
    f = np.float32
    m = np.asarray(matrix, f)
    x, y, z = (f(value) for value in vector)
    return np.array([(x * m[row, 0] + y * m[row, 1]) + z * m[row, 2] for row in range(3)], f)


class _Pose:
    __slots__ = ("body", "writes")

    def __init__(self, body: SimpleBody, writes: FrameWrites):
        self.body, self.writes = body, writes

    def local(self) -> tuple[float, float, float, float]:
        return self.writes.rotations.get(self.body.target, self.body.rotation)

    def joint_local(self, index: int) -> tuple[float, float, float, float]:
        return self.writes.joints.get((self.body.target, index), self.body.joint_rotations[index])

    def rotation(self) -> tuple[float, float, float, float]:
        body = self.body
        return self.local() if body.parent_rotation is None else quaternion_product(self.local(), body.parent_rotation)

    def world(self) -> np.ndarray:
        body = self.body
        local = make_trs_matrix(body.position, self.local(), body.scale)
        return local if body.parent_world is None else transform_product(body.parent_world, local)

    def set_rotation(self, rotation) -> None:
        body = self.body
        self.writes.rotations[body.target] = rotation if body.parent_rotation is None else _unit_turn(
            quaternion_product(rotation, _conjugate(body.parent_rotation)), _YX)

    def chain(self, index: int) -> list[int]:
        parents, chain = self.body.joint_parents, []
        while 0 <= index < len(parents) and index not in chain:
            chain.append(index)
            index = parents[index]
        return chain

    def joint_rotation(self, index: int) -> tuple[float, float, float, float]:
        return chain_rotation([self.joint_local(joint) for joint in self.chain(index)], self.rotation())

    def joint_world(self, index: int) -> np.ndarray:
        body = self.body
        return chain_world([make_trs_matrix(body.joint_positions[joint], self.joint_local(joint),
                                            body.joint_scales[joint]) for joint in self.chain(index)], self.world())

    def parent_joint(self, index: int) -> int | None:
        parent = self.body.joint_parents[index]
        return parent if 0 <= parent < len(self.body.joint_parents) else None

    def set_joint_rotation(self, index: int, rotation) -> None:
        parent = self.parent_joint(index)
        under = self.rotation() if parent is None else self.joint_rotation(parent)
        self.writes.joints[self.body.target, index] = _unit_turn(quaternion_product(rotation, _conjugate(under)), _YX)


class _FanTurn:
    __slots__ = ("param", "axis", "speed", "target", "timer", "cycle", "random", "radians")

    def __init__(self, param: FanParam, animation: SimpleAnimation, pose: _Pose, random: Xorshift128 | None):
        f = np.float32
        self.param, self.random, self.radians = param, random, animation.radians
        start = pose.world() if param.joint is None else pose.joint_world(param.joint)
        self.axis = normalized(_normal_through(start, param.axis))
        self.speed, self.target, self.timer, self.cycle = radians(param.speed, self.radians), f(0), f(0), f(0)
        if param.random:
            self.draw()
            self.timer = random.unit() * self.cycle
            self.speed = self.target

    def draw(self) -> None:
        self.cycle = self.random.between(*self.param.cycle_range)
        self.target = radians(self.random.between(*self.param.speed_range), self.radians)

    def update(self, seconds: np.float32, power: np.float32, pose: _Pose) -> None:
        f, param = np.float32, self.param
        if param.random:
            self.timer = seconds + self.timer
            if self.cycle < self.timer:
                self.timer = self.timer - self.cycle
                self.draw()
            self.speed = f((float(self.target) - float(self.speed)) * param.ease * float(seconds) + float(self.speed))
        turn = axis_turn(self.axis, self.speed * seconds * power * f(0.5))
        if param.joint is None:
            pose.set_rotation(_unit_turn(quaternion_product(pose.rotation(), turn), _YX))
        else:
            pose.set_joint_rotation(param.joint, _unit_turn(
                quaternion_product(pose.joint_rotation(param.joint), turn), _YX))


class _PendulumSwing:
    __slots__ = ("param", "random", "radians", "turn", "start", "matrix", "radian", "timer", "swing", "cycle",
                 "frequency", "random_timer", "twist_start", "twist_radian", "twist_period", "twist_frequency",
                 "twist_timer")

    def __init__(self, param: PendulumParam, animation: SimpleAnimation, pose: _Pose, random: Xorshift128):
        f = np.float32
        self.param, self.random, self.radians, self.turn = param, random, animation.radians, animation.full_turn
        if param.joint is None:
            self.start, self.matrix = pose.local(), pose.world()
        else:
            self.start, self.matrix = pose.joint_local(param.joint), pose.joint_world(param.joint)
        self.radian = radians(param.degree, self.radians)
        self.timer = random.unit() * f(self.turn / float(self.omega()))
        self.swing = self.cycle = self.frequency = self.random_timer = f(0)
        if param.random:
            self.draw()
            self.random_timer = random.unit() * self.cycle
        self.twist_start, self.twist_radian, self.twist_period = radians(param.twist_start, self.radians), f(0), f(0)
        self.twist_frequency = self.twist_timer = f(0)
        if param.twist:
            self.draw_twist()
            self.twist_timer = random.unit() * self.twist_period

    def omega(self) -> np.float32:
        f = np.float32
        length, gravity = f(self.param.length), f(self.param.gravity)
        return np.sqrt(gravity / length) if length > 0 else np.sqrt(gravity)

    def draw(self) -> None:
        self.swing = radians(self.random.between(*self.param.degree_range), self.radians)
        self.cycle = self.random.between(*self.param.cycle_range)
        self.frequency = _frequency(self.cycle, self.turn)

    def draw_twist(self) -> None:
        self.twist_radian = radians(self.random.between(*self.param.twist_range), self.radians)
        self.twist_period = self.random.between(*self.param.twist_period_range)
        self.twist_frequency = _frequency(self.twist_period, self.turn)

    def update(self, seconds: np.float32, power: np.float32, pose: _Pose) -> None:
        f, param, sinf = np.float32, self.param, crt("sinf")
        joint = param.joint
        if joint is None:
            pose.writes.rotations[pose.body.target] = self.start
        else:
            pose.writes.joints[pose.body.target, joint] = self.start
        axis = tuple(f(value) for value in param.axis)
        if param.random:
            self.random_timer = self.random_timer + seconds
            if self.cycle < self.random_timer:
                self.random_timer = self.random_timer - self.cycle
                self.draw()
            # In double, rounded once.
            angle = f(float(f(sinf(float(self.frequency * self.random_timer)))) * float(self.swing) * float(power))
            sine, cosine = f(sinf(float(angle))), f(crt("cosf")(float(angle)))
            x, y, z = axis
            axis = ((x * cosine + y * f(0)) + z * sine, (x * f(0) + y * f(1)) + z * f(0),
                    (x * -sine + y * f(0)) + z * cosine)
        omega = self.omega()
        if joint is not None:
            self.matrix = pose.joint_world(joint)
        axis = normalized(_normal_through(self.matrix, axis))
        if param.infinite:
            cycle = f(self.turn)
            # In double, rounded once.
            self.timer = f(float(f(param.per_second)) * float(seconds) + float(self.timer))
        else:
            cycle = f(self.turn / float(omega))
            self.timer = self.timer + seconds
        if cycle < self.timer:
            self.timer = self.timer - cycle
        angle = self.timer if param.infinite else f(sinf(float(self.timer * omega))) * self.radian
        half = angle * power * f(0.5)
        if joint is None:
            pose.writes.rotations[pose.body.target] = _unit_turn(
                quaternion_product(self.start, axis_turn(axis, half)), _YX)
        else:
            parent = pose.parent_joint(joint)
            if parent is not None:
                axis = turned_vector(axis, _inverse_turn(pose.joint_rotation(parent), (0, 1, 2, 3)))
            pose.writes.joints[pose.body.target, joint] = _unit_turn(
                quaternion_product(self.start, axis_turn(axis, half)), _YX)
        if param.twist:
            self.twist(seconds, power, pose)

    def twist(self, seconds: np.float32, power: np.float32, pose: _Pose) -> None:
        f, param = np.float32, self.param
        joint = param.joint
        matrix = pose.world() if joint is None else pose.joint_world(joint)
        axis = normalized(_normal_through(matrix, param.twist_axis))
        self.twist_timer = seconds + self.twist_timer
        if self.twist_period < self.twist_timer:
            self.twist_timer = self.twist_timer - self.twist_period
            self.draw_twist()
        wave = f(crt("sinf")(float(self.twist_frequency * self.twist_timer)))
        half = (wave * self.twist_radian * power + self.twist_start) * f(0.5)
        if joint is None:
            target = pose.body.target
            pose.writes.rotations[target] = _unit_turn(quaternion_product(pose.local(), axis_turn(axis, half)), _YX)
            return
        parent = pose.parent_joint(joint)
        if parent is not None:
            axis = turned_vector(axis, _inverse_turn(pose.joint_rotation(parent), _YX))
        pose.writes.joints[pose.body.target, joint] = _unit_turn(
            quaternion_product(pose.joint_local(joint), axis_turn(axis, half)), _YX)


class SimplePlay:
    def __init__(self, animation: SimpleAnimation, random: Xorshift128 | None):
        self.random = random
        self.restart(animation)

    def restart(self, animation: SimpleAnimation) -> None:
        self.animation = animation
        self.power = np.float32(animation.start_power)
        pose = _Pose(animation.body, FrameWrites({}, {}, {}))
        self.turns = [(_FanTurn if isinstance(param, FanParam) else _PendulumSwing)(param, animation, pose, self.random)
                      for param in animation.params]

    @property
    def running(self) -> bool:
        return True

    def write(self, seconds: float, writes: FrameWrites) -> None:
        f, animation = np.float32, self.animation
        seconds = f(seconds)
        x, y, z = (f(own) - f(seen) for own, seen in zip(animation.position, writes.camera or (0.0, 0.0, 0.0)))
        far = f(animation.distance) * f(animation.distance) < (y * y + x * x) + z * z
        # In double, rounded once.
        power = float(self.power)
        self.power = f(((animation.far_power if far else animation.near_power) - power) * float(f(animation.ease))
                       * float(seconds) + power)
        if not self.power >= f(animation.least):
            return
        first, second = (float(f(value)) for value in animation.powers)
        power = f(first * second * float(self.power))
        pose = _Pose(animation.body, writes)
        for turn in self.turns:
            turn.update(seconds, power, pose)


@dataclass(frozen=True, slots=True)
class SceneTimeFrame:
    materials: Mapping[tuple[object, str, str], float]
    rotations: Mapping[object, tuple[float, float, float, float]]
    fields: Mapping[tuple[str, str], tuple[float, ...]] = frozendict()
    joints: Mapping[tuple[object, int], tuple[float, float, float, float]] = frozendict()


class SceneTime:
    def __init__(self, animations: Iterable[CurveAnimation | LightRise | LightBlink | TimelineAnimation
                                            | SimpleAnimation], random: Xorshift128):
        self.plays = [animation.play(random) for animation in animations]
        self.seconds = 0.0
        self.frame = SceneTimeFrame({}, {})

    @property
    def running(self) -> bool:
        return any(play.running for play in self.plays)

    def step(self, seconds: float, camera: tuple[float, float, float] | None = None,
             stopped: Collection[object] = frozenset()) -> SceneTimeFrame:
        frame = self.frame
        writes = FrameWrites(dict(frame.materials), dict(frame.rotations), dict(frame.fields), dict(frame.joints),
                             camera)
        for play in self.plays:
            if play.animation.owner not in stopped:
                play.write(seconds, writes)
        self.seconds += seconds
        self.frame = SceneTimeFrame(writes.materials, writes.rotations, writes.fields, writes.joints)
        return self.frame


__all__ = ["BlinkPlay", "CurveAnimation", "CurvePlay", "CurveProcess", "CurveWork", "EASES", "Ease", "FanParam",
           "FrameWrites", "LightBlink", "LightRise", "PendulumParam", "RandomCount", "RandomRule", "RisePlay",
           "SceneTime", "SceneTimeFrame",
           "SimpleAnimation", "SimpleBody", "SimplePlay", "TimelineAnimation", "TimelineKeys", "TimelinePlay",
           "TimelineTrack", "UnitProcess", "Xorshift128", "radians"]
