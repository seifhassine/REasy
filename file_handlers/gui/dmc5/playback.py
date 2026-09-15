"""Runtime-only evaluation of GUI compact clips for the live preview."""

from __future__ import annotations

import copy
import math
import struct
from collections.abc import Callable
from dataclasses import dataclass

from file_handlers.clip.enums import PropertyType
from file_handlers.motion.mot_clip.model import (
    ClipInterpolation,
    ClipKey,
    ClipProperty,
    HermiteCurve,
)

from ..errors import GuiSceneError
from ..model import (
    COMPONENT_NAMES,
    GuiAnimation,
    GuiSymbol,
    apply_property,
    iter_clip_nodes,
    resolve_properties,
)
from ..native_math import f32 as _f32
from ..native_math import fadd as _fadd
from ..native_math import fdiv as _fdiv
from ..native_math import fsub as _fsub
from ..scene import GuiSceneNode
from .scene import Dmc5GuiScene


_NUMERIC_TYPES = {
    PropertyType.S8, PropertyType.U8, PropertyType.S16, PropertyType.U16,
    PropertyType.S32, PropertyType.U32, PropertyType.S64, PropertyType.U64,
    PropertyType.F32, PropertyType.F64,
}
_INTEGER_TYPES = {
    PropertyType.S8, PropertyType.U8, PropertyType.S16, PropertyType.U16,
    PropertyType.S32, PropertyType.U32, PropertyType.S64, PropertyType.U64,
}
_CONTROL_FIELDS = {
    "Play", "PlayFrame", "PlayState", "Segment", "StatePattern",
    "NextStateEnable",
}
DMC5_SEGMENT_VALUES = {
    "Keep": -1,
    **{f"Segment{index:02d}": index for index in range(61)},
}


def dmc5_control_defaults(node: GuiSceneNode) -> dict[str, object]:
    """Return the exact constructor/loader state for an animated Control."""

    symbol = node.prototype.symbol if node.prototype is not None else None
    if symbol is None or not symbol.animations:
        return {}
    properties = resolve_properties(node.object.properties)
    return {
        "Play": bool(properties.get("Play", True)),
        "PlayFrame": 0.0,
        "PlayState": symbol.animations[0].name,
        "Segment": properties.get("Segment", "Keep"),
        "StatePattern": int(properties.get("StatePattern", 0)),
        "NextStateEnable": bool(properties.get("NextStateEnable", True)),
    }


@dataclass(frozen=True, slots=True)
class GuiActionEvent:
    """One native EVENT/PASS_EVENT setter dispatch."""

    path: str
    field_name: str
    frame: float
    interpolation: ClipInterpolation
    value: object


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
    segment: object = "Keep"
    next_state_enable: bool = True
    completed: bool = False
    completion_callback: (
        Callable[["Dmc5GuiPlayback", GuiCompletionEvent], None] | None
    ) = None

    @property
    def play_state(self) -> str:
        return self.animation.name if self.animation is not None else ""


class Dmc5GuiPlayback:
    """History-sensitive preview state, kept separate from editable GUI data.

    A symbol clip targets its instantiated Control and direct children.  Setter
    assignments are consumed in serialized order, including setters that start
    or scrub a nested Control.  The resulting dictionary is only a runtime
    overlay; neither scene properties nor serialized animation data are changed.
    """

    def __init__(self, scene: Dmc5GuiScene) -> None:
        self.scene = scene
        self.controllers: dict[str, _Controller] = {}
        self.overrides: dict[str, dict[str, object]] = {}
        self.selected_paths: tuple[str, ...] = ()
        self.selected_path: str | None = None
        self.selected_symbol: GuiSymbol | None = None
        self.selected_animation: GuiAnimation | None = None
        self.initial_events: tuple[object, ...] = ()
        self.initial_action_events: tuple[GuiActionEvent, ...] = ()
        self.initial_completion_events: tuple[GuiCompletionEvent, ...] = ()
        self.initial_transitions: tuple[GuiTransitionEvent, ...] = ()
        self.initial_assignments: tuple[GuiPropertyAssignment, ...] = ()
        self.last_events: tuple[object, ...] = ()
        self.last_action_events: tuple[GuiActionEvent, ...] = ()
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
            properties = dmc5_control_defaults(node)
            previous = retained.get(node.path)
            self.controllers[node.path] = _Controller(
                node=node,
                symbol=symbol,
                playing=bool(properties.get("Play", True)),
                pattern=int(properties.get("StatePattern", 0)),
                segment=properties.get("Segment", "Keep"),
                next_state_enable=bool(properties.get("NextStateEnable", True)),
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
        self.initial_action_events = tuple(
            item for item in events if isinstance(item, GuiActionEvent)
        )
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
        """Recreate native loader state without retaining editor playback state."""

        self.controllers.clear()
        self.rebuild()

    def apply_runtime_snapshot(
        self,
        values_by_path: dict[str, dict[str, object]],
    ) -> int:
        """Restore one coherent Control snapshot and its evaluated properties."""

        captured = {
            str(path): dict(values)
            for path, values in values_by_path.items()
            if path in self.controllers
        }
        if not captured:
            return 0
        order = (
            "StatePattern",
            "Segment",
            "NextStateEnable",
            "PlayState",
            "PlayFrame",
            "Play",
        )

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
                if "StatePattern" in values:
                    controller.pattern = int(values["StatePattern"])
                if "Segment" in values:
                    controller.segment = values["Segment"]
                if "NextStateEnable" in values:
                    controller.next_state_enable = bool(values["NextStateEnable"])
                if "PlayState" in values:
                    state = str(values["PlayState"] or "")
                    controller.animation = self._state(controller, state) if state else None
                if "PlayFrame" in values:
                    controller.frame = float(values["PlayFrame"])
                if "Play" in values:
                    controller.playing = bool(values["Play"])
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
        for target, track in self._targeted_tracks(controller, animation):
            assignment = self._track_value(target, track, controller.frame)
            if assignment is None or assignment[0] in _CONTROL_FIELDS:
                continue
            name, value = assignment
            copied = copy.deepcopy(value)
            self.overrides.setdefault(target.path, {})[name] = copied
            assignments.append(GuiPropertyAssignment(
                target.path,
                name,
                copied,
                "runtime scenario",
            ))

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
        if animation is None or not intervals:
            return
        targeted_tracks = self._targeted_tracks(controller, animation)
        for interval_start, interval_end in intervals:
            for target, track in targeted_tracks:
                assignment = self._track_value(target, track, interval_end)
                if assignment is not None:
                    name, value = assignment
                    self._assign(
                        target.path, name, value, "track",
                        events, assignments, stack,
                    )
                for event in _action_events(
                    track, interval_start, interval_end
                ):
                    events.append(
                        GuiActionEvent(
                            target.path,
                            track.name,
                            event.frame,
                            event.interpolation,
                            copy.deepcopy(event.value),
                        )
                    )

    @staticmethod
    def _targeted_tracks(
        controller: _Controller,
        animation: GuiAnimation,
    ) -> tuple[tuple[GuiSceneNode, ClipProperty], ...]:
        candidates = (controller.node, *controller.node.children)
        by_guid: dict[bytes, list[GuiSceneNode]] = {}
        by_name: dict[str, list[GuiSceneNode]] = {}
        for item in candidates:
            by_guid.setdefault(item.object.instance_guid.bytes_le, []).append(item)
            by_name.setdefault(item.object.name, []).append(item)
        result: list[tuple[GuiSceneNode, ClipProperty]] = []
        for clip_node in iter_clip_nodes(animation.clip.root):
            targets = (
                by_guid.get(clip_node.root_guid)
                or by_guid.get(clip_node.extra_guid)
                or by_name.get(clip_node.name)
                or ()
            )
            result.extend(
                (target, track)
                for target in targets
                for track in clip_node.properties
            )
        return tuple(result)

    def _track_value(
        self,
        target: GuiSceneNode,
        track: ClipProperty,
        frame: float,
    ) -> tuple[str, object] | None:
        if not track.children:
            value = _sample(track, frame)
            return (track.name, value) if value is not None else None

        labels = COMPONENT_NAMES.get(track.property_type, ())
        aliases = {name.casefold(): index for index, name in enumerate(labels)}
        if track.property_type == PropertyType.SIZE:
            aliases.update({"w": 0, "h": 1})
        current = self.overrides.get(target.path, {}).get(
            track.name, target.properties.get(track.name)
        )
        result = list(current) if isinstance(current, (list, tuple)) else [0.0] * len(labels)
        changed = False
        for child in track.children:
            index = aliases.get(child.name.casefold())
            value = _sample(child, frame)
            if index is None or value is None or index >= len(result):
                continue
            result[index] = value
            changed = True
        return (track.name, result) if changed else None

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
        if name not in _CONTROL_FIELDS:
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
        controller = self.controllers.get(path)
        if controller is None:
            return
        if name == "Play":
            controller.playing = bool(value)
        elif name == "PlayState":
            self._activate_name(
                controller, str(value or ""), False,
                events, assignments, stack,
            )
        elif name == "PlayFrame":
            self._set_frame(
                controller, float(value), events, assignments, stack
            )
        elif name == "StatePattern":
            self._set_pattern(
                controller, int(value), events, assignments, stack
            )
        elif name == "NextStateEnable":
            controller.next_state_enable = bool(value)
        elif name == "Segment":
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
        self.overrides.setdefault(controller.node.path, {}).update(
            {
                "Play": controller.playing,
                "PlayFrame": controller.frame,
                "PlayState": controller.play_state,
                "Segment": controller.segment,
                "StatePattern": controller.pattern,
                "NextStateEnable": controller.next_state_enable,
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
        self.last_action_events = tuple(
            item for item in events if isinstance(item, GuiActionEvent)
        )
        self.last_completion_events = tuple(
            item for item in events if isinstance(item, GuiCompletionEvent)
        )
        self.last_transitions = tuple(
            item for item in events if isinstance(item, GuiTransitionEvent)
        )


def _action_events(
    track: ClipProperty,
    previous_frame: float,
    current_frame: float,
) -> list[ClipKey]:
    keys = track.keys
    if (
        track.property_type != PropertyType.ENUM
        or not keys
        or any(
            key.interpolation not in (
                ClipInterpolation.EVENT,
                ClipInterpolation.PASS_EVENT,
            )
            for key in keys
        )
    ):
        return []
    previous = _f32(previous_frame)
    current = _f32(current_frame)
    if math.isnan(previous) or math.isnan(current) or current < previous:
        return []
    mode = keys[0].interpolation
    if mode == ClipInterpolation.PASS_EVENT and previous == current:
        return []
    result = []
    for key in keys:
        frame = _f32(key.frame)
        if math.isnan(frame):
            continue
        if mode == ClipInterpolation.EVENT:
            crossed = frame == current if previous == current else previous < frame <= current
        else:
            crossed = previous <= frame < current
        if crossed:
            result.append(key)
    return result


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


def _sample(track: ClipProperty, frame: float) -> object | None:
    keys = track.keys
    if not keys:
        return None
    if track.property_type == PropertyType.ENUM and all(
        key.interpolation in (ClipInterpolation.EVENT, ClipInterpolation.PASS_EVENT)
        for key in keys
    ):
        # These are setter events (Effect.Action in the DMC5 corpus), not a
        # continuously assigned reflected property.
        return None
    frame = _f32(frame)
    left, right = _surrounding_keys(keys, frame)
    if left is None:
        return copy.deepcopy(right.value) if right is not None else None
    if right is None or left is right or _ordered_equal(frame, left.frame):
        return copy.deepcopy(left.value)
    if left.interpolation == ClipInterpolation.DISCRETE:
        return copy.deepcopy(left.value)
    if left.interpolation in (ClipInterpolation.EVENT, ClipInterpolation.PASS_EVENT):
        return None
    if track.property_type == PropertyType.BOOL or track.property_type not in _NUMERIC_TYPES:
        if left.interpolation == ClipInterpolation.LINEAR:
            return copy.deepcopy(left.value)
        raise GuiSceneError(
            f"animation mode {left.interpolation.name} is unsupported for "
            f"{track.property_type.name}"
        )
    if not isinstance(left.value, (int, float)) or not isinstance(right.value, (int, float)):
        raise GuiSceneError(f"numeric track {track.name!r} contains a nonnumeric key")

    if left.interpolation == ClipInterpolation.OFFSET_FRAME:
        value = _fadd(_interpolation_input(track.property_type, left.value), _fsub(frame, left.frame))
    elif left.interpolation == ClipInterpolation.HERMITE:
        value = _native_hermite_value(track.property_type, left, right, frame)
    elif left.interpolation == ClipInterpolation.BEZIER:
        if track.property_type not in (PropertyType.F32, PropertyType.F64):
            raise GuiSceneError("Bezier animation is native-supported only for F32/F64 tracks")
        value = _native_bezier_value(left, right, frame)
    elif left.interpolation == ClipInterpolation.LINEAR:
        span = _fsub(right.frame, left.frame)
        ratio = _fdiv(_fsub(frame, left.frame), span) if span else 0.0
        inverse = _fsub(1.0, ratio)
        start = _interpolation_input(track.property_type, left.value)
        end = _interpolation_input(track.property_type, right.value)
        value = _fadd(_f32(inverse * start), _f32(ratio * end))
    else:
        raise GuiSceneError(f"unsupported animation evaluation mode {left.interpolation.name}")
    return _convert_number(track.property_type, value)


def _surrounding_keys(
    keys: list[ClipKey],
    frame: float,
) -> tuple[ClipKey | None, ClipKey | None]:
    """Reproduce DMC5's midpoint search, including duplicate-frame policy."""

    first, last = _f32(keys[0].frame), _f32(keys[-1].frame)
    if _ordered_greater(first, frame):
        return None, keys[0]
    if _ordered_greater_equal(frame, last):
        return keys[-1], None
    lower, upper = 0, len(keys) - 1
    while lower < upper - 1:
        middle = lower + (upper - lower) // 2
        middle_frame = _f32(keys[middle].frame)
        if _ordered_equal(middle_frame, frame):
            return keys[middle], keys[middle]
        if math.isnan(middle_frame) or math.isnan(frame) or middle_frame <= frame:
            lower = middle
        else:
            upper = middle
    return keys[lower], keys[upper]


def _native_hermite_value(
    kind: PropertyType,
    start: ClipKey,
    end: ClipKey,
    frame: float,
) -> float:
    curve = start.curve
    if not isinstance(curve, HermiteCurve):
        raise GuiSceneError("Hermite animation key has no Hermite curve")
    out_x, out_y, in_x, in_y = curve.values
    parameter = _native_hermite_parameter(start.frame, end.frame, frame, out_x, in_x)
    squared = _f32(parameter * parameter)
    cubed = _f32(squared * parameter)
    tangent0, tangent1 = _f32(out_y), _f32_negate(in_y)
    tangent_term = _f32(tangent0 * parameter)
    if kind == PropertyType.F64:
        value0, value1 = float(start.value), float(end.value)
        coefficient_a = value0 * 2.0 - value1 * 2.0
        coefficient_a += float(tangent0) + float(tangent1)
        coefficient_b = value1 * 3.0 - value0 * 3.0
        coefficient_b -= float(_f32(tangent0 * 2.0)) + float(tangent1)
        return coefficient_a * float(cubed) + coefficient_b * float(squared) + float(tangent_term) + value0

    value0 = _interpolation_input(kind, start.value)
    value1 = _interpolation_input(kind, end.value)
    coefficient_a = _fadd(_fsub(_f32(value0 * 2.0), _f32(value1 * 2.0)), tangent0)
    coefficient_a = _fadd(coefficient_a, tangent1)
    coefficient_b = _fsub(_f32(value1 * 3.0), _f32(value0 * 3.0))
    coefficient_b = _fsub(coefficient_b, _f32(tangent0 * 2.0))
    coefficient_b = _fsub(coefficient_b, tangent1)
    value = _fadd(_f32(coefficient_a * cubed), _f32(coefficient_b * squared))
    return _fadd(_fadd(value, tangent_term), value0)


def _native_hermite_parameter(
    start_frame: float,
    end_frame: float,
    frame: float,
    outgoing_tangent: float,
    incoming_tangent: float,
) -> float:
    """DMC5's binary32 Newton solve for compact-CLIP mode 5."""

    x0, x1, target = _f32(start_frame), _f32(end_frame), _f32(frame)
    tangent0 = _f32(outgoing_tangent)
    tangent1 = _f32_negate(incoming_tangent)
    if math.isnan(target) or math.isnan(x0) or math.isnan(x1):
        return math.nan
    if target <= x0:
        return 0.0
    if target >= x1:
        return 1.0
    parameter = _fdiv(_fsub(target, x0), _fsub(x1, x0))
    coefficient_a = _fadd(_fadd(_fsub(_f32(x0 * 2.0), _f32(x1 * 2.0)), tangent0), tangent1)
    if not coefficient_a or math.isnan(coefficient_a):
        return parameter
    coefficient_b = _fsub(_fsub(_fsub(_f32(x1 * 3.0), _f32(x0 * 3.0)), _f32(tangent0 * 2.0)), tangent1)
    b_over_a = _fdiv(coefficient_b, coefficient_a)
    c_over_a = _fdiv(tangent0, coefficient_a)
    constant = _fdiv(_fsub(x0, target), coefficient_a)
    denominator = _fadd(_f32(_f32(parameter * 3.0) * parameter), _f32(_f32(b_over_a * 2.0) * parameter))
    denominator = _fadd(denominator, c_over_a)
    if denominator and not math.isnan(denominator):
        tolerance = _f32(1.0e-5)
        for _ in range(100):
            previous = parameter
            numerator = _f32(_f32(parameter * 2.0) * parameter)
            numerator = _f32(numerator * parameter)
            numerator = _fadd(numerator, _f32(_f32(parameter * b_over_a) * parameter))
            numerator = _fsub(numerator, constant)
            denominator = _fadd(
                _f32(_f32(parameter * 3.0) * parameter),
                _f32(_f32(b_over_a * 2.0) * parameter),
            )
            denominator = _fadd(denominator, c_over_a)
            parameter = _fdiv(numerator, denominator)
            if tolerance >= _f32(abs(_fsub(parameter, previous))):
                break
    if not 0.0 <= parameter <= 1.0:
        total = _fadd(parameter, b_over_a)
        expression = _fadd(_fadd(_f32(parameter * b_over_a), c_over_a), _f32(parameter * parameter))
        discriminant = _fsub(_f32(total * total), _f32(expression * 4.0))
        candidate_a = _f32(_fsub(discriminant, total) * 0.5)
        candidate_b = _f32(_fsub(_f32_negate(total), discriminant) * 0.5)
        if 0.0 <= candidate_a <= 1.0:
            parameter = candidate_a
        elif 0.0 <= candidate_b <= 1.0:
            parameter = candidate_b
    return parameter


def _native_bezier_value(start: ClipKey, end: ClipKey, frame: float) -> float:
    curve = start.curve
    if not isinstance(curve, HermiteCurve):
        raise GuiSceneError("Bezier animation key has no Hermite curve")
    _out_x, out_y, _in_x, in_y = curve.values
    span = _fsub(end.frame, start.frame)
    parameter = _fdiv(_fsub(frame, start.frame), span) if span else 0.0
    squared = _f32(parameter * parameter)
    cubed = _f32(squared * parameter)
    basis0 = _fadd(_fadd(_fsub(_f32(cubed * -1.0), _f32(squared * -3.0)), _f32(parameter * -3.0)), 1.0)
    basis1 = _fadd(_fadd(_f32(cubed * 3.0), _f32(squared * -6.0)), _f32(parameter * 3.0))
    basis2 = _fadd(_f32(cubed * -3.0), _f32(squared * 3.0))
    basis3 = cubed
    value0, value1 = _f32(float(start.value)), _f32(float(end.value))
    outgoing = _fadd(value0, out_y)
    incoming = _fsub(value1, in_y)
    value = _fadd(_f32(basis0 * value0), _f32(basis1 * value1))
    return _fadd(_fadd(value, _f32(basis2 * incoming)), _f32(basis3 * outgoing))


def _convert_number(kind: PropertyType, value: float) -> int | float:
    if kind == PropertyType.F32:
        return _f32(value)
    if kind == PropertyType.F64:
        return float(value)
    if kind not in _INTEGER_TYPES:
        return value
    signed = kind in {PropertyType.S8, PropertyType.S16, PropertyType.S32, PropertyType.S64}
    width = {PropertyType.S8: 8, PropertyType.U8: 8, PropertyType.S16: 16,
             PropertyType.U16: 16, PropertyType.S32: 32, PropertyType.U32: 32,
             PropertyType.S64: 64, PropertyType.U64: 64}[kind]
    rounded = _fadd(value, 0.5)
    if kind == PropertyType.U64:
        raw = _native_cvttss2ui64(rounded)
    else:
        conversion_width = 64 if width == 64 or kind == PropertyType.U32 else 32
        raw = _native_cvttss2si(rounded, conversion_width)
    mask = (1 << width) - 1
    raw &= mask
    return raw - (1 << width) if signed and raw & (1 << (width - 1)) else raw


def _interpolation_input(kind: PropertyType, value: int | float) -> float:
    if kind == PropertyType.S64:
        return _native_integer_to_f32(int(value))
    if kind == PropertyType.U64:
        return _native_u64_to_f32(int(value))
    return _f32(float(value))


def _native_cvttss2si(value: float, width: int) -> int:
    value = _f32(value)
    indefinite = 1 << (width - 1)
    if not math.isfinite(value):
        return indefinite
    converted = math.trunc(value)
    if not -(1 << (width - 1)) <= converted < 1 << (width - 1):
        return indefinite
    return converted & ((1 << width) - 1)


def _native_integer_to_f32(value: int) -> float:
    integer = int(value)
    if integer == 0:
        return 0.0
    sign, magnitude = (1, -integer) if integer < 0 else (0, integer)
    exponent = magnitude.bit_length() - 1
    if exponent <= 23:
        significand = magnitude << (23 - exponent)
    else:
        shift = exponent - 23
        significand = magnitude >> shift
        remainder = magnitude & ((1 << shift) - 1)
        halfway = 1 << (shift - 1)
        if remainder > halfway or remainder == halfway and significand & 1:
            significand += 1
            if significand == 1 << 24:
                significand >>= 1
                exponent += 1
    bits = (sign << 31) | ((exponent + 127) << 23) | (significand & 0x7FFFFF)
    return struct.unpack("<f", struct.pack("<I", bits))[0]


def _native_u64_to_f32(value: int) -> float:
    raw = int(value) & 0xFFFF_FFFF_FFFF_FFFF
    signed = raw - (1 << 64) if raw & (1 << 63) else raw
    result = _native_integer_to_f32(signed)
    return _fadd(result, _f32(float(1 << 64))) if raw & (1 << 63) else result


def _native_cvttss2ui64(value: float) -> int:
    adjusted = _f32(value)
    threshold = _f32(float(1 << 63))
    bias = 0
    if not math.isnan(adjusted) and adjusted >= threshold:
        adjusted = _fsub(adjusted, threshold)
        if not math.isnan(adjusted) and adjusted < threshold:
            bias = 1 << 63
    return (_native_cvttss2si(adjusted, 64) + bias) & 0xFFFF_FFFF_FFFF_FFFF


def _f32_negate(value: float) -> float:
    bits = struct.unpack("<I", struct.pack("<f", _f32(value)))[0]
    return struct.unpack("<f", struct.pack("<I", bits ^ 0x80000000))[0]


def _ordered_equal(left: float, right: float) -> bool:
    left, right = _f32(left), _f32(right)
    return not (math.isnan(left) or math.isnan(right)) and left == right


def _ordered_greater(left: float, right: float) -> bool:
    return not (math.isnan(left) or math.isnan(right)) and left > right


def _ordered_greater_equal(left: float, right: float) -> bool:
    return not (math.isnan(left) or math.isnan(right)) and left >= right
