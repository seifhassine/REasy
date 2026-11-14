from __future__ import annotations

import copy
import math
from dataclasses import dataclass

lazy from file_handlers.clip.model import Property
lazy from file_handlers.clip.play import play
from file_handlers.clip.rules import PropertyType, game_player

lazy from ..model import GuiAnimation, GuiSymbol
lazy from ..storage import Stored, baselines, masked, nodes, places
lazy from file_handlers.native_math import f32 as _f32
lazy from file_handlers.native_math import fadd as _fadd
lazy from ..scene import GuiSceneNode
lazy from .scene import Dmc5GuiScene


def dmc5_control_defaults(scene: Dmc5GuiScene, node: GuiSceneNode) -> dict[str, object]:
    symbol = node.prototype.symbol if node.prototype is not None else None
    if symbol is None or not symbol.animations:
        return {}
    properties = scene.type_properties(node.object.type_name, node.object.values())
    fields = scene.workspace.profile.control_fields
    return {
        fields["play"]: bool(properties[fields["play"]]),
        fields["frame"]: 0.0,
        fields["state"]: symbol.animations[0].name,
        fields["segment"]: properties[fields["segment"]],
        fields["pattern"]: int(properties[fields["pattern"]]),
        fields["next_state"]: bool(properties[fields["next_state"]]),
    }


@dataclass(frozen=True, slots=True)
class GuiCompletionEvent:
    """The ``Control.StateFinished`` delegate boundary."""

    path: str
    state: str
    pattern: int
    clip_name: str


@dataclass(frozen=True, slots=True)
class GuiTransitionEvent:
    """One descriptor link followed after ``StateFinished``."""

    path: str
    source_state: str
    source_pattern: int
    target_state: str
    target_pattern: int


@dataclass(frozen=True, slots=True)
class GuiPropertyAssignment:
    """One baseline or compact-CLIP setter in native dispatch order."""

    path: str
    field_name: str
    value: object
    source: str


@dataclass(slots=True)
class _Controller:
    node: GuiSceneNode
    symbol: GuiSymbol
    animation: GuiAnimation | None = None
    frame: float = 0.0
    playing: bool = True
    pattern: int = 0
    segment: object = None
    next_state_enable: bool = True
    completed: bool = False

    @property
    def play_state(self) -> str:
        return self.animation.name if self.animation is not None else ""


class Dmc5GuiPlayback:

    def __init__(self, scene: Dmc5GuiScene) -> None:
        self.scene = scene
        profile = scene.workspace.profile
        self._fields, self._mode = profile.control_fields, profile.clip_mode
        self._control_fields = frozenset(self._fields.values())
        self._baselines: dict[int, dict[int, list[Stored]]] = {}
        self.controllers: dict[str, _Controller] = {}
        self.overrides: dict[str, dict[str, object]] = {}
        self.selected_paths: tuple[str, ...] = ()
        self.selected_path: str | None = None
        self.selected_symbol: GuiSymbol | None = None
        self.selected_animation: GuiAnimation | None = None
        self.last_events: tuple[object, ...] = ()
        self.last_assignments: tuple[GuiPropertyAssignment, ...] = ()
        self._event_sink: list[object] | None = None
        self._assignment_sink: list[GuiPropertyAssignment] | None = None
        self._propagation_stack: set[tuple[str, str, str, str]] | None = None
        self._update_paths: tuple[str, ...] = ()
        self._start()

    @property
    def frame(self) -> float:
        controller = self._selected_controller()
        return controller.frame if controller is not None else 0.0

    def _start(self) -> None:
        self.scene.drop_clones()
        held = self.scene.workspace.profile.list_values.get(self.scene.workspace.root_key, {})
        self.overrides = {node.path: {**node.object.values(), **held.get(node.path, {})} for node in self.scene.nodes}
        fields = self._fields
        for node in self.scene.nodes:
            symbol = node.prototype.symbol if node.prototype is not None else None
            if symbol is None or not symbol.animations:
                continue
            properties = dmc5_control_defaults(self.scene, node)
            self.controllers[node.path] = _Controller(
                node=node, symbol=symbol, playing=properties[fields["play"]], pattern=properties[fields["pattern"]],
                segment=properties[fields["segment"]], next_state_enable=properties[fields["next_state"]])
        events: list[object] = []
        assignments: list[GuiPropertyAssignment] = []
        stack: set[tuple[str, str, str, str]] = set()
        for controller in self.controllers.values():
            self._sync_controller(controller)
        update_paths: list[str] = []

        def initialize(node: GuiSceneNode) -> None:
            if node.path in self.controllers:
                update_paths.append(node.path)
            for child in self.scene.native_children(node, self.overrides):
                initialize(child)
            controller = self.controllers.get(node.path)
            if controller is not None:
                self._activate_name(controller, controller.symbol.animations[0].name, True, events, assignments, stack)

        initialize(self.scene.root)
        self._update_paths = tuple(update_paths)
        for binding, target in self.scene.bindings:
            self._assign(target.path, binding.property.name, binding.property.value, "binding", events, assignments,
                         stack)
        self._finish_mutation(events, assignments)

    def select_animation(
        self,
        symbol: GuiSymbol | None,
        animation: GuiAnimation | None,
        path: str | None = None,
    ) -> None:
        self.selected_symbol = symbol
        self.selected_animation = animation
        self.selected_path = path
        if (
            path is not None
            and path in self.controllers
            and symbol is not None
            and self.controllers[path].symbol is symbol
        ):
            self.selected_paths = (path,)
        else:
            self.selected_paths = tuple(
                candidate
                for candidate, item in self.controllers.items()
                if symbol is not None and item.symbol is symbol
            )
        if animation is None:
            return
        self._run_mutation(
            lambda events, assignments, stack: [
                self._activate_animation(
                    self.controllers[candidate], animation, True,
                    events, assignments, stack,
                )
                for candidate in self.selected_paths
            ]
        )

    def set_selected_frame(self, frame: float) -> None:
        self._run_mutation(
            lambda events, assignments, stack: [
                self._set_frame(
                    self.controllers[path], float(frame), events, assignments, stack
                )
                for path in self.selected_paths
            ]
        )

    def play_state(
        self,
        path: str,
        name: str,
    ) -> bool:
        """Invoke DMC5's reflected ``Control.PlayState`` setter."""

        controller = self.controllers.get(path)
        if controller is None:
            return False
        valid = self._state(controller, str(name)) is not None
        self._run_mutation(
            lambda events, assignments, stack: self._activate_name(
                controller, str(name), False, events, assignments, stack
            )
        )
        return valid

    def assign(self, path: str, name: str, value: object) -> None:
        self._run_mutation(lambda events, assignments, stack: self._assign(
            path, name, value, "script", events, assignments, stack))

    def spawn(self, node: GuiSceneNode) -> None:
        def start(item: GuiSceneNode, events, assignments, stack) -> None:
            self.overrides[item.path] = copy.deepcopy(self.overrides[item.source.path])
            for child in item.children:
                start(child, events, assignments, stack)
            held = self.controllers.get(item.source.path)
            if held is not None:
                made = self.controllers[item.path] = _Controller(
                    node=item, symbol=held.symbol, playing=held.playing, pattern=held.pattern, segment=held.segment,
                    next_state_enable=held.next_state_enable)
                self._activate_name(made, held.play_state, True, events, assignments, stack)

        def ordered(item: GuiSceneNode):
            if item.path in self.controllers:
                yield item.path
            for child in self.scene.native_children(item, self.overrides):
                yield from ordered(child)

        self._run_mutation(lambda events, assignments, stack: start(node, events, assignments, stack))
        self._update_paths = tuple(ordered(self.scene.root))

    def forget(self, node: GuiSceneNode) -> None:
        for item in node.walk():
            del self.overrides[item.path]
            self.controllers.pop(item.path, None)
        self.selected_paths = tuple(path for path in self.selected_paths if path in self.controllers)

    def advance(self, delta_frames: float) -> None:
        def step(events, assignments, stack) -> None:
            for path in self._update_paths:
                controller = self.controllers.get(path)
                if (
                    controller is None
                    or not controller.playing
                    or controller.animation is None
                ):
                    continue
                self._set_frame(
                    controller,
                    _fadd(controller.frame, _f32(delta_frames)),
                    events,
                    assignments,
                    stack,
                )

        self._run_mutation(step)

    def _selected_controller(self) -> _Controller | None:
        return next(
            (self.controllers[path] for path in self.selected_paths if path in self.controllers),
            None,
        )

    @staticmethod
    def _state(controller: _Controller, name: str) -> GuiAnimation | None:
        variants = [item for item in controller.symbol.animations if item.name == name]
        return variants[controller.pattern] if 0 <= controller.pattern < len(variants) else None

    def _activate_name(
        self,
        controller: _Controller,
        name: str,
        force: bool,
        events: list[object],
        assignments: list[GuiPropertyAssignment],
        stack: set[tuple[str, str, str, str]],
        automatic_source: tuple[str, int] | None = None,
    ) -> bool:
        if not name:
            controller.animation = None
            controller.frame = 0.0
            controller.completed = True
            self._sync_controller(controller)
            return False
        animation = self._state(controller, name)
        if animation is None:
            # Failed native lookup clears the descriptor and frame.
            controller.animation = None
            controller.frame = 0.0
            controller.completed = True
            self._sync_controller(controller)
            return False
        if not force and animation is controller.animation:
            return True
        return self._activate_animation(
            controller, animation, force, events, assignments, stack,
            automatic_source,
        )

    def _activate_animation(
        self,
        controller: _Controller,
        animation: GuiAnimation,
        force: bool,
        events: list[object],
        assignments: list[GuiPropertyAssignment],
        stack: set[tuple[str, str, str, str]],
        automatic_source: tuple[str, int] | None = None,
    ) -> bool:
        if animation not in controller.symbol.animations:
            animation = self._state(controller, animation.name)
            if animation is None:
                controller.animation = None
                controller.frame = 0.0
                controller.completed = True
                self._sync_controller(controller)
                return False
        if not force and animation is controller.animation:
            return True
        if automatic_source is not None:
            events.append(
                GuiTransitionEvent(
                    controller.node.path,
                    automatic_source[0],
                    automatic_source[1],
                    animation.name,
                    controller.pattern,
                )
            )
        controller.animation = animation
        controller.frame = 0.0
        controller.completed = False
        self._sync_controller(controller)
        symbol = controller.symbol
        if id(symbol) not in self._baselines:
            self._baselines[id(symbol)] = baselines(symbol)
        for child in self.scene.native_children(controller.node, self.overrides):
            for record in self._baselines[id(symbol)].get(id(child.object), ()):
                target = self.overrides.setdefault(child.path, {})
                self._assign(
                    child.path, record.name, masked(target.get(record.name), record), "baseline",
                    events, assignments, stack,
                )
        self._evaluate_intervals(
            controller, ((-1.0, 0.0),), events, assignments, stack
        )
        return True

    def _set_frame(
        self,
        controller: _Controller,
        frame: float,
        events: list[object],
        assignments: list[GuiPropertyAssignment],
        stack: set[tuple[str, str, str, str]],
    ) -> None:
        animation = controller.animation
        if animation is None:
            return
        source_state = animation.name
        source_pattern = controller.pattern
        was_completed = controller.completed
        play_frame, completed, intervals = _play_frame_transition(
            animation, frame, controller.frame
        )
        controller.frame = play_frame
        controller.completed = completed
        self._sync_controller(controller)
        self._evaluate_intervals(
            controller, intervals, events, assignments, stack,
            animation=animation,
        )
        if not completed or was_completed:
            return
        node = controller.node
        for name, cases in self.scene.workspace.profile.runtime_type(node.object.type_name).facts.get(
                "finished", {}).items():
            held = self.scene.runtime_properties(node, self.overrides[node.path])
            follows = cases["true" if held[name] else "false"].get(source_state)
            if follows is not None:
                self._activate_name(controller, follows, False, events, assignments, stack)
        completion = GuiCompletionEvent(
            controller.node.path,
            source_state,
            source_pattern,
            animation.name,
        )
        events.append(completion)
        # Native compares the descriptor after the delegate returns.
        if (
            controller.animation is animation
            and controller.next_state_enable
            and animation.transition is not None
        ):
            self._activate_name(
                controller,
                animation.transition.name,
                True,
                events,
                assignments,
                stack,
                (source_state, source_pattern),
            )

    def _evaluate_intervals(
        self,
        controller: _Controller,
        intervals: tuple[tuple[float, float], ...],
        events: list[object],
        assignments: list[GuiPropertyAssignment],
        stack: set[tuple[str, str, str, str]],
        *,
        animation: GuiAnimation | None = None,
    ) -> None:
        animation = animation or controller.animation
        if animation is None:
            return
        rules = game_player(animation.clip.version, self.scene.workspace.profile.game)
        for start, end in intervals:
            for target, track in self._tracks(controller, animation):
                for value in self._played(target, track, start, end, rules):
                    self._assign(target.path, track.name, value, "track", events, assignments, stack)

    def _tracks(self, controller: _Controller, animation: GuiAnimation):
        children = self.scene.native_children(controller.node, self.overrides)
        for clip_node in nodes(animation):
            target = next((child for child in children if child.object.name == clip_node.name), None)
            yield from (() if target is None else ((target, track) for track in clip_node.properties))

    def _played(self, target: GuiSceneNode, track: Property, start: float, end: float, rules: dict) -> list:
        if track.type in rules["key_reads"].no_case:
            return []
        current = self.overrides.get(target.path, {}).get(track.name, target.properties.get(track.name))
        if current is None:
            current = self.scene.workspace.profile.runtime_type(target.object.type_name).defaults.get(track.name)
        if not track.children or track.type == PropertyType.QUATERNION:
            return play(track, start, end, rules, current, mode=self._mode)
        members = places(track.type)
        result = list(current) if isinstance(current, (list, tuple)) else [0.0] * len(members)
        written, last = False, None
        for child in track.children:
            index = members.get(child.name)
            for value in () if index is None or index >= len(result) else play(
                    child, start, end, rules, result[index], mode=self._mode, reader="member",
                    unset=result[index] if rules["element_unset"] == "current" else last):
                result[index], written, last = value, True, value
        return [result] if written else []

    def _assign(
        self,
        path: str,
        name: str,
        value: object,
        source: str,
        events: list[object],
        assignments: list[GuiPropertyAssignment],
        stack: set[tuple[str, str, str, str]],
    ) -> None:
        copied = copy.deepcopy(value)
        self.overrides.setdefault(path, {})[name] = copied
        assignments.append(GuiPropertyAssignment(path, name, copied, source))
        if name not in self._control_fields:
            return
        token = (path, name, repr(value), source)
        if token in stack:
            return
        stack.add(token)
        try:
            self._apply_control(
                path, name, value, events, assignments, stack
            )
        finally:
            stack.remove(token)

    def _apply_control(
        self,
        path: str,
        name: str,
        value: object,
        events: list[object],
        assignments: list[GuiPropertyAssignment],
        stack: set[tuple[str, str, str, str]],
    ) -> None:
        controller, fields = self.controllers.get(path), self._fields
        if controller is None:
            return
        if name == fields["play"]:
            controller.playing = bool(value)
        elif name == fields["state"]:
            self._activate_name(
                controller, str(value or ""), False,
                events, assignments, stack,
            )
        elif name == fields["frame"]:
            self._set_frame(
                controller, float(value), events, assignments, stack
            )
        elif name == fields["pattern"]:
            self._set_pattern(
                controller, int(value), events, assignments, stack
            )
        elif name == fields["next_state"]:
            controller.next_state_enable = bool(value)
        elif name == fields["segment"]:
            controller.segment = value
        self._sync_controller(controller)

    def _set_pattern(
        self,
        controller: _Controller,
        pattern: int,
        events: list[object],
        assignments: list[GuiPropertyAssignment],
        stack: set[tuple[str, str, str, str]],
    ) -> None:
        if pattern == controller.pattern:
            return
        name = controller.play_state
        controller.pattern = pattern
        if name:
            self._activate_name(
                controller, name, True, events, assignments, stack
            )
        else:
            self._sync_controller(controller)

    def _sync_controller(self, controller: _Controller) -> None:
        fields = self._fields
        self.overrides.setdefault(controller.node.path, {}).update(
            {
                fields["play"]: controller.playing,
                fields["frame"]: controller.frame,
                fields["state"]: controller.play_state,
                fields["segment"]: controller.segment,
                fields["pattern"]: controller.pattern,
                fields["next_state"]: controller.next_state_enable,
            }
        )

    def _run_mutation(self, operation):
        root = self._event_sink is None
        if root:
            self._event_sink = []
            self._assignment_sink = []
            self._propagation_stack = set()
        assert self._event_sink is not None
        assert self._assignment_sink is not None
        assert self._propagation_stack is not None
        try:
            return operation(
                self._event_sink,
                self._assignment_sink,
                self._propagation_stack,
            )
        finally:
            if root:
                events = self._event_sink
                assignments = self._assignment_sink
                self._event_sink = None
                self._assignment_sink = None
                self._propagation_stack = None
                self._finish_mutation(events, assignments)

    def _finish_mutation(self, events: list[object], assignments: list[GuiPropertyAssignment]) -> None:
        self.last_events, self.last_assignments = tuple(events), tuple(assignments)


def _ucomiss_equal(left: float, right: float) -> bool:
    return not (math.isnan(left) or math.isnan(right)) and left == right


def _minss(destination: float, source: float) -> float:
    if math.isnan(destination) or math.isnan(source) or destination == source:
        return source
    return destination if destination < source else source


def _play_frame_transition(
    animation: GuiAnimation,
    frame: float,
    previous_frame: float,
) -> tuple[float, bool, tuple[tuple[float, float], ...]]:
    """Transcribe DMC5 ``Control.set_PlayFrame`` (RVA 0x278C1B0)."""

    requested = _f32(frame)
    previous = _f32(previous_frame)
    duration = _f32(animation.clip.total_frame)
    intervals: list[tuple[float, float]] = []
    if math.isnan(requested) or math.isnan(duration) or requested < duration:
        play_frame = requested
        completed = False
        comparison_frame = previous
    else:
        comparison_frame = _minss(requested, previous)
        above_endpoint = not (
            math.isnan(comparison_frame) or math.isnan(duration)
        ) and comparison_frame > duration
        if animation.loop and not above_endpoint:
            intervals.append((comparison_frame, duration))
            play_frame = 0.0
            completed = False
            comparison_frame = duration
        else:
            play_frame = duration
            completed = True
    if not _ucomiss_equal(comparison_frame, play_frame):
        start = comparison_frame
        if _ucomiss_equal(start, 0.0):
            start = -1.0
        intervals.append((start, play_frame))
    return play_frame, completed, tuple(intervals)


