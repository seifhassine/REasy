from __future__ import annotations

import copy
import math
from collections.abc import Callable
from dataclasses import dataclass

lazy from file_handlers.clip.model import Property
lazy from file_handlers.clip.play import play
from file_handlers.clip.rules import PropertyType, display_names, game_player

lazy from ..model import (
    COMPONENT_NAMES,
    GuiAnimation,
    GuiSymbol,
    apply_property,
    resolve_properties,
)
lazy from file_handlers.native_math import f32 as _f32
lazy from file_handlers.native_math import fadd as _fadd
lazy from ..scene import GuiSceneNode
lazy from .scene import Dmc5GuiScene


def dmc5_control_defaults(scene: Dmc5GuiScene, node: GuiSceneNode) -> dict[str, object]:
    symbol = node.prototype.symbol if node.prototype is not None else None
    if symbol is None or not symbol.animations:
        return {}
    properties = scene.type_properties(node.object.type_name, resolve_properties(node.object.properties))
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
    completion_callback: (
        Callable[["Dmc5GuiPlayback", GuiCompletionEvent], None] | None
    ) = None

    @property
    def play_state(self) -> str:
        return self.animation.name if self.animation is not None else ""


class Dmc5GuiPlayback:

    def __init__(self, scene: Dmc5GuiScene) -> None:
        self.scene = scene
        profile = scene.workspace.profile
        self._fields, self._mode = profile.control_fields, profile.clip_mode
        self._control_fields = frozenset(self._fields.values())
        self._track_aliases = {code: profile.track_aliases[name] for code, name in display_names("PropertyType").items()
                               if name in profile.track_aliases}
        self.controllers: dict[str, _Controller] = {}
        self.overrides: dict[str, dict[str, object]] = {}
        self.selected_paths: tuple[str, ...] = ()
        self.selected_path: str | None = None
        self.selected_symbol: GuiSymbol | None = None
        self.selected_animation: GuiAnimation | None = None
        self.initial_events: tuple[object, ...] = ()
        self.initial_completion_events: tuple[GuiCompletionEvent, ...] = ()
        self.initial_transitions: tuple[GuiTransitionEvent, ...] = ()
        self.initial_assignments: tuple[GuiPropertyAssignment, ...] = ()
        self.last_events: tuple[object, ...] = ()
        self.last_completion_events: tuple[GuiCompletionEvent, ...] = ()
        self.last_transitions: tuple[GuiTransitionEvent, ...] = ()
        self.last_assignments: tuple[GuiPropertyAssignment, ...] = ()
        self._event_sink: list[object] | None = None
        self._assignment_sink: list[GuiPropertyAssignment] | None = None
        self._propagation_stack: set[tuple[str, str, str, str]] | None = None
        self._update_paths: tuple[str, ...] = ()
        self.rebuild()

    @property
    def frame(self) -> float:
        controller = self._selected_controller()
        return controller.frame if controller is not None else 0.0

    @property
    def duration(self) -> float:
        controller = self._selected_controller()
        if controller is not None and controller.animation is not None:
            return max(0.0, float(controller.animation.clip.total_frame))
        return 0.0

    def rebuild(self) -> None:
        retained = {
            path: (
                item.animation, item.frame, item.playing, item.pattern,
                item.segment, item.next_state_enable, item.completion_callback,
            )
            for path, item in self.controllers.items()
        }
        self.controllers.clear()
        self.overrides = {
            node.path: resolve_properties(node.object.properties)
            for node in self.scene.nodes
        }
        for node in self.scene.nodes:
            symbol = node.prototype.symbol if node.prototype is not None else None
            if symbol is None or not symbol.animations:
                continue
            properties, fields = dmc5_control_defaults(self.scene, node), self._fields
            previous = retained.get(node.path)
            self.controllers[node.path] = _Controller(
                node=node,
                symbol=symbol,
                playing=properties[fields["play"]],
                pattern=properties[fields["pattern"]],
                segment=properties[fields["segment"]],
                next_state_enable=properties[fields["next_state"]],
                completion_callback=previous[6] if previous is not None else None,
            )

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
            if controller is None:
                return
            # GUI loader applies authored properties before assigning the
            # animation table, then selects the first serialized state name.
            first_name = controller.symbol.animations[0].name
            self._activate_name(
                controller, first_name, True, events, assignments, stack
            )

        initialize(self.scene.root)
        self._update_paths = tuple(update_paths)

        # Root bindings are dispatched after the complete symbol tree exists.
        for binding in self.scene.resource.document.bindings:
            target = self.scene.binding_target(binding.target_path)
            if target is None:
                continue
            values = self.overrides.setdefault(target.path, {})
            value = apply_property(values.get(binding.property.name), binding.property)
            self._assign(
                target.path, binding.property.name, value, "binding",
                events, assignments, stack,
            )

        # An editor rebuild preserves runtime selection/scrub state, but the
        # first construction above always follows the native loader path.
        for path, previous in retained.items():
            controller = self.controllers.get(path)
            animation = previous[0]
            if controller is None or animation not in controller.symbol.animations:
                continue
            controller.playing = previous[2]
            controller.pattern = previous[3]
            controller.segment = previous[4]
            controller.next_state_enable = previous[5]
            self._activate_name(
                controller, animation.name, True, events, assignments, stack
            )
            self._set_frame(
                controller, previous[1], events, assignments, stack
            )

        self.initial_events = tuple(events)
        self.initial_assignments = tuple(assignments)
        self.initial_completion_events = tuple(
            item for item in events if isinstance(item, GuiCompletionEvent)
        )
        self.initial_transitions = tuple(
            item for item in events if isinstance(item, GuiTransitionEvent)
        )
        self._finish_mutation(events, assignments)

        if self.selected_symbol is not None and self.selected_animation is not None:
            if (
                self.selected_path in self.controllers
                and self.controllers[self.selected_path].symbol is self.selected_symbol
            ):
                self.selected_paths = (self.selected_path,)
            else:
                self.selected_paths = tuple(
                    path
                    for path, item in self.controllers.items()
                    if item.symbol is self.selected_symbol
                )

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

    def restart_selected(self) -> None:
        def restart(events, assignments, stack) -> None:
            for path in self.selected_paths:
                controller = self.controllers[path]
                if controller.animation is not None:
                    self._activate_animation(
                        controller, controller.animation, True,
                        events, assignments, stack,
                    )

        self._run_mutation(restart)

    def reset_runtime(self) -> None:
        self.controllers.clear()
        self.rebuild()

    def apply_runtime_snapshot(
        self,
        values_by_path: dict[str, dict[str, object]],
    ) -> int:
        captured = {
            str(path): dict(values)
            for path, values in values_by_path.items()
            if path in self.controllers
        }
        if not captured:
            return 0
        fields = self._fields
        order = tuple(fields[role] for role in ("pattern", "segment", "next_state", "state", "frame", "play"))

        def apply(events, assignments, stack) -> None:
            # Dispatch captured setters by property across the whole graph.
            # This mirrors one simultaneous sample more closely than restoring
            # every object to completion before visiting the next one.
            for name in order:
                for path, values in captured.items():
                    if name not in values:
                        continue
                    self._assign(
                        path,
                        name,
                        values[name],
                        "runtime scenario",
                        events,
                        assignments,
                        stack,
                    )

            # Setter tracks above may legitimately affect another controller.
            # The capture is the final observed state, so make every sampled
            # descriptor authoritative once propagation has settled.
            for path, values in captured.items():
                controller = self.controllers[path]
                if fields["pattern"] in values:
                    controller.pattern = int(values[fields["pattern"]])
                if fields["segment"] in values:
                    controller.segment = values[fields["segment"]]
                if fields["next_state"] in values:
                    controller.next_state_enable = bool(values[fields["next_state"]])
                if fields["state"] in values:
                    state = str(values[fields["state"]] or "")
                    controller.animation = self._state(controller, state) if state else None
                if fields["frame"] in values:
                    controller.frame = float(values[fields["frame"]])
                if fields["play"] in values:
                    controller.playing = bool(values[fields["play"]])
                animation = controller.animation
                controller.completed = (
                    animation is None
                    or (
                        not animation.loop
                        and controller.frame >= float(animation.clip.total_frame)
                    )
                )
                self._sync_controller(controller)

            # Re-sample non-controller tracks at the restored frames without
            # replaying setter/action events a second time.
            for path in self._update_paths:
                if path in captured:
                    self._sample_runtime_controller(
                        self.controllers[path],
                        assignments,
                    )

        self._run_mutation(apply)
        return len(captured)

    def _sample_runtime_controller(
        self,
        controller: _Controller,
        assignments: list[GuiPropertyAssignment],
    ) -> None:
        animation = controller.animation
        if animation is None:
            return
        rules = game_player(animation.clip.version, self.scene.workspace.profile.game)
        for target, track in self._tracks(controller, animation):
            if track.name in self._control_fields:
                continue
            for value in self._played(target, track, -1.0, controller.frame, rules, events=False):
                copied = copy.deepcopy(value)
                self.overrides.setdefault(target.path, {})[track.name] = copied
                assignments.append(GuiPropertyAssignment(target.path, track.name, copied, "runtime scenario"))

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

    def state_remaining(self, path: str) -> float:
        controller = self.controllers.get(path)
        if controller is None or controller.animation is None:
            return 0.0
        return max(
            0.0,
            float(controller.animation.clip.total_frame) - controller.frame,
        )

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
        for child in self.scene.native_children(controller.node, self.overrides):
            for prop in child.object.animation_defaults:
                target = self.overrides.setdefault(child.path, {})
                value = apply_property(target.get(prop.name), prop)
                self._assign(
                    child.path, prop.name, value, "baseline",
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
        completion = GuiCompletionEvent(
            controller.node.path,
            source_state,
            source_pattern,
            animation.name,
        )
        events.append(completion)
        if controller.completion_callback is not None:
            controller.completion_callback(self, completion)
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
        for clip_node in (node for root in animation.clip.roots[:1] for node in root.children):
            target = next((child for child in children if child.object.name == clip_node.name), None)
            yield from (() if target is None else ((target, track) for track in clip_node.properties))

    def _played(self, target: GuiSceneNode, track: Property, start: float, end: float, rules: dict,
                events: bool = True) -> list:
        if track.type in rules["key_reads"].no_case:
            return []
        current = self.overrides.get(target.path, {}).get(track.name, target.properties.get(track.name))
        if current is None:
            current = self.scene.workspace.profile.runtime_type(target.object.type_name).defaults.get(track.name)
        if not track.children or track.type == PropertyType.QUATERNION:
            return play(track, start, end, rules, current, events=events, mode=self._mode)
        labels = COMPONENT_NAMES.get(track.type, ())
        aliases = {name.casefold(): index for index, name in enumerate(labels)}
        aliases.update(self._track_aliases.get(track.type, {}))
        result = list(current) if isinstance(current, (list, tuple)) else [0.0] * len(labels)
        written, last = False, None
        for child in track.children:
            index = aliases.get(child.name.casefold())
            for value in () if index is None or index >= len(result) else play(
                    child, start, end, rules, result[index], events=events, mode=self._mode, reader="member",
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

    def _finish_mutation(
        self,
        events: list[object],
        assignments: list[GuiPropertyAssignment],
    ) -> None:
        self.last_events = tuple(events)
        self.last_assignments = tuple(assignments)
        self.last_completion_events = tuple(
            item for item in events if isinstance(item, GuiCompletionEvent)
        )
        self.last_transitions = tuple(
            item for item in events if isinstance(item, GuiTransitionEvent)
        )


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


