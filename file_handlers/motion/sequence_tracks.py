"""See docs/internals/sequence-tracks.md."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field

from file_handlers.clip.play import play_node
from file_handlers.clip.rules import game_player
lazy from file_handlers.engine_math import crt
from file_handlers.native_math import f32, fadd, fdiv, fmul, fsub
from utils.hash_util import murmur3_hash_utf16le

from .model import Motion, Sequence

# The engine pulls a first update's start back by this much, so keys on it play.
EARLY = f32(-0.01)
# Game updates a second: an update steps a motion by its frame rate over this.
UPDATES = 60.0
# RE9 MaterialRateController: an update's seconds, Application DeltaTime (frames) over BaseFps.
RATE_STEP = fdiv(1.0, UPDATES)
# PRAGMATA's TimerBase: a timer of at most this many seconds has progressed in full.
SHORTEST = f32(2.0 ** -23)


@dataclass(slots=True)
class TrackState:
    drawn: dict = field(default_factory=dict)
    parts: dict = field(default_factory=dict)
    meshes: dict = field(default_factory=dict)
    flags: dict = field(default_factory=dict)
    materials: dict = field(default_factory=dict)
    buffers: dict = field(default_factory=dict)
    gravity: GravitySet | None = None
    scales: dict = field(default_factory=dict)
    shapes: dict = field(default_factory=dict)
    fields: dict = field(default_factory=dict)
    changes: dict = field(default_factory=dict)
    held: dict = field(default_factory=dict)
    joints: dict = field(default_factory=dict)
    variables: dict = field(default_factory=dict)
    rates: dict = field(default_factory=dict)
    updates: int = 0


@dataclass(frozen=True, slots=True)
class Fade:
    steps: tuple[tuple[object, float | None], ...]

    def of(self, value):
        for destination, progress in self.steps:
            value = tuple(old if new is None else _faded(old, new, progress) for old, new in zip(value, destination)) \
                if isinstance(destination, tuple) else _faded(value, destination, progress)
        return value


def _faded(start, destination, progress):
    return f32(destination) if progress is None else fadd(fmul(fsub(destination, start), progress), start)


@dataclass(frozen=True, slots=True)
class GravitySet:
    rate: float
    progress: float | None = None

    def __call__(self, base) -> list[float]:
        held = [fmul(part, self.rate) for part in base]
        return held if self.progress is None else [_faded(start, part, self.progress)
                                                    for start, part in zip(held, base)]


def frame_plays(previous: float, frame: float, end: float, first: bool) -> list[tuple[float, float, bool, int]]:
    """Forward only: the preview starts a looped motion over."""
    plays = []
    if first and previous > 0:
        plays.append((0.0, previous, False, 1))
    if first:
        previous = fadd(previous, EARLY)
    events = previous != frame
    plays.append((previous, frame, events, 1))
    if frame >= end:
        plays.append((0.0, 0.0, events, 2))
    return plays


class TrackTargets:
    def __init__(self, document, holder, parts=()):
        self.document, self.holder, self.parts = document, holder, tuple(parts)
        self.children: dict = {}
        for item in document.objects.values():
            parent = document.object_by_local_id.get(item.parent_id)
            if parent is not None:
                self.children.setdefault(parent, []).append(item)

    def find(self, value, rule: dict):
        if (refs := rule.get("refs")) is not None:
            field, prop = refs["fields"].get(str(value), (None, None))
            component = next((found for key in self.document.objects[self.holder].components if (
                found := self.document.components.get(key)) is not None and found.type_name == refs["component"]), None)
            return None if field is None or component is None else self.referenced(
                getattr(component.fields.get(field), "raw_bytes", None), component.object_table_id, prop)
        if (kind := rule.get("child")) is not None:
            return next((item.id for item in self.children.get(self.holder, ()) if any(
                self.document.components[key].type_name == kind for key in item.components
                if key in self.document.components)), None)
        if value is None:
            return self.holder if rule.get("default") == "owner" else None
        if not isinstance(value, str) or not value.strip():
            return self.holder if rule.get("empty") == "owner" else None
        if rule.get("owner_name") and value == self.document.objects[self.holder].name or value == rule.get("self"):
            return self.holder
        pending = list(self.children.get(self.holder, ()))
        while pending:
            item = pending.pop(0)
            if item.name == value:
                return item.id
            if rule.get("find") == "recursive":
                pending[:0] = self.children.get(item.id, ())
        return None

    def referenced(self, raw: bytes | None, table: int | None = None, prop: int | None = None):
        scn = self.document.rsz_file
        if getattr(scn, "is_pfb", False):
            return None if table is None else self.document.object_by_local_id.get(next((
                info.target_id for info in scn.gameobject_ref_infos
                if (info.object_id, info.property_id, info.array_index) == (table, prop, 0)), None))
        return next((self.document.object_by_local_id.get(item.id) for item in getattr(scn, "gameobjects", ())
                     if raw and bytes(item.guid) == bytes(raw)), None)

    def below(self):
        pending = [self.document.objects[self.holder]]
        while pending:
            yield (item := pending.pop(0))
            pending[:0] = self.children.get(item.id, ())

    def holding(self, kind: str, flag: str | None = None):
        return next(((item.id, found, document) for document, item in (
            *((self.document, item) for item in self.below()), *self.parts) for key in item.components
                     if (found := document.components.get(key)) is not None and found.type_name == kind
                     and (flag is None or getattr(found.fields.get(flag), "value", False))), None)

    def hashed(self, name_hash: int):
        return next((item.id for item in self.children.get(self.holder, ())
                     if murmur3_hash_utf16le(item.name) == name_hash), None)


class TrackPlayer:
    def __init__(self, motion: Motion, rules: dict, targets: TrackTargets, game: str, reset: bool = True,
                 filters: list[int] | None = None, sequences: list[Sequence] | None = None):
        self.rules, self.targets, self.reset = rules, targets, reset
        self.nodes = []
        for sequence in motion.sequences if sequences is None else sequences:
            nodes = [node for root in sequence.clip.roots for node in root.children]
            rows = sequence.tracks if filters is not None and len(sequence.tracks) == len(nodes) \
                else [None] * len(nodes)
            player = game_player(sequence.clip.version, game)
            self.nodes += [(rules[node.name], node, player) for node, row in zip(nodes, rows)
                           if node.name in rules and (row is None or passes(row, filters))]
        self.fps, self.step, self.end = motion.fps, f32(motion.fps / UPDATES), f32(motion.end_frame)
        self.eyes = next((rule["eyes"] for rule in rules.values() if "eyes" in rule), None)
        self.lips = next((rule["lips"] for rule in rules.values() if "lips" in rule), None)
        self._last: tuple[float, TrackState] | None = None
        self._mark: tuple[float, TrackState] | None = None
        self._partial: tuple[float, TrackState] | None = None
        self._variables: dict[float, dict] = {}

    def changes(self) -> dict:
        found = {}
        for (target, layer), rows in self.at(self.end).changes.items():
            found.setdefault(target, {})[layer] = rows
        return found

    def _fresh(self) -> TrackState:
        state = TrackState()
        for track, rule in self.rules.items():
            if (base := rule.get("base")) and (target := self.targets.find(None, rule["play"])) is not None:
                key = target, rule["play"]["layer"]
                state.changes.setdefault(key, [(0.0, base["bank"], base["motion"], 0.0, 0, 0)])
                state.held.setdefault(key, base["priority"])
            if (request := rule.get("request") or rule.get("set")) is not None and rule.get("target") is not None:
                state.changes.setdefault((rule["target"], request["layer"]), [(0.0, *rule["start"], 0.0, 0, 0)])
            if (face := rule.get("expression")) and rule.get("target") is not None:
                data = rule["data"]
                key, kind = (rule["target"], data[face["layer"]]), data[face["default"]]
                state.changes.setdefault(key, [(0.0, *_expression(face, data, kind, -1)[:2], 0.0, 0, 0)])
                state.held.setdefault(key, kind)
            if (face := rule.get("receiver")) and rule.get("target") is not None:
                state.changes.setdefault((rule["target"], 0), [(0.0, rule["bank"], face["start"], 0.0, 0, 0)])
                state.held.setdefault((rule["target"], 0), [face["start"], None, -1, None])
            for target, *layers in rule.get("faces", ()):
                for (layer, bank), motion in zip(layers, _facial_motions(rule["emotion"], rule["start"], 0)):
                    if layer >= 0:
                        state.changes.setdefault((target, layer), [(0.0, bank, motion, 0.0, 0, 0)])
            if (switch := rule.get("switch")) is not None:
                for row in rule["data"][switch["rows"]]:
                    if (target := self.targets.hashed(row[switch["hash"]])) is not None:
                        state.drawn[target] = bool(row[switch["default"]])
                        if row[switch["default"]]:
                            state.held[self.targets.holder, switch["part"]] = row[switch["id"]]
                for row in rule["data"].get(switch.get("materials")) or ():
                    if row[switch["default"]]:
                        self._material(rule, row[switch["id"]], state)
            if "screen" in rule:
                state.held[rule["target"], "screen"] = rule["start"]
            if (modes := rule.get("condition", {}).get("modes")) is not None:
                holder = self.targets.holder
                state.held[holder, "mode"], state.held[holder, "body"] = modes["start"], modes["body"]
                self._condition(rule, rule["ids"][modes["body"]], state)
            if (held := rule.get("condition", {}).get("held")) is not None:
                state.held[self.targets.holder, track, "held"] = held["start"]
            if "ratio" in rule and "rows" in rule:
                state.held[self.targets.holder, "ratio"] = rule["start"]
            if "hair" in rule:
                state.held[self.targets.holder, "hair"] = {"copied": deepcopy(rule["defaults"]), "rate": None,
                                                           **{part: [0.0, 0.0, False, False] for part in rule["hair"]}}
            if (face := rule.get("sequence")) is not None and rule.get("target") is not None:
                for layer, bank, motion in (face["common"], face["eye"], face["base"][:3]):
                    state.changes[rule["target"], layer] = [(0.0, bank, motion, 0.0, 0, 0)]
                state.held[rule["target"], "sequence"] = {
                    "track": None, "seq": (0, face["none"]), "values": [], "rate": 0.0, "now": set(), "next": set(),
                    "order": None, "busy": 0.0, "hashes": [murmur3_hash_utf16le(face["joints"].format(index))
                                                           for index in range(len(rule["defaults"][face["values"]]))]}
        return state

    def joints(self, frame: float) -> dict:
        return self.at(frame).joints

    def variables(self, frame: float) -> dict:
        frame = f32(frame)
        if frame not in self._variables:
            self._variables[frame] = dict(self.at(frame).variables)
        return self._variables[frame]

    def masks(self) -> dict:
        return {rule["target"]: dict(rule["sequence"]["masks"]) for rule in self.rules.values()
                if "sequence" in rule and rule.get("target") is not None}

    def at(self, frame: float) -> TrackState:
        frame = f32(frame)
        if self._partial is not None and self._partial[0] == frame:
            return self._partial[1]
        if self._last is None or frame < self._last[0]:
            if self._mark is not None and self._mark[0] <= frame:
                self._last = self._mark[0], deepcopy(self._mark[1])
            else:
                self._last, self._variables = (f32(0.0), self._fresh()), {}
                self._update(f32(0.0), f32(0.0), True, self._last[1])
        previous, state = self._last
        if fadd(fadd(previous, self.step), self.step) <= frame:
            self._mark = previous, deepcopy(state)
        while (reached := fadd(previous, self.step)) <= frame:
            self._update(previous, reached, False, state)
            previous = reached
        self._last = previous, state
        if previous < frame:
            state = deepcopy(state)
            self._update(previous, frame, False, state)
            self._partial = frame, state
        return state

    def _update(self, previous: float, frame: float, first: bool, state: TrackState) -> None:
        state.materials, state.joints, state.scales = {}, {}, {}
        state.updates += 1
        for track, rule in self.rules.items():
            if (modes := rule.get("condition", {}).get("modes")) is not None:
                self._release(rule, modes["alpha"], state)
            if (ident := state.held.pop((self.targets.holder, track, "requested"), None)) is not None:
                self._condition(rule, ident, state)
            if (floats := rule.get("floats")) is not None:
                self._floats(track, rule, floats, state)
            if (drawn := state.held.pop((self.targets.holder, track, "draw"), None)) is not None:
                state.drawn.update(dict.fromkeys(rule["targets"], drawn))
            if "hair" in rule:
                state.held[self.targets.holder, "hair"]["copied"].update(
                    dict.fromkeys((part["override"] for part in rule["hair"].values()), False))
            if "faces" in rule:
                self._emotions(track, rule, state, previous)
        for index, (rule, node, player) in enumerate(self.nodes):
            fields = deepcopy(rule["defaults"]) if self.reset else state.fields.setdefault(
                index, deepcopy(rule["defaults"]))
            for start, end, events, mode in frame_plays(previous, frame, self.end, first):
                play_node(node, start, end, fields, player, events=events, mode=mode)
            for name, values in rule.get("enums", {}).items():
                fields[name] = values.get(fields[name], fields[name]) if isinstance(fields.get(name), str) else \
                    fields.get(name)
            self._apply(rule, fields, state, frame, node.name, index)
        for track, rule in self.rules.items():
            if (held := rule.get("condition", {}).get("held")) is not None:
                self._held(track, rule, held, state)
            if (screen := rule.get("screen")) is not None:
                state.materials.setdefault(rule["target"], {}).setdefault(screen["material"], {}).update(
                    _screen(screen, *state.held[rule["target"], "screen"], rule["sprites"]))
            if "sequence" in rule and rule.get("target") is not None:
                self._sequence(rule, state, frame)
            if "hair" in rule:
                self._hair(rule, state)
            if "normals" in rule:
                self._normals(track, rule, state.held.pop((self.targets.holder, track, "requests"), []), state)
            if "glow" in rule:
                self._glow(rule, state)
            for name, variable in rule.get("values", {}).items():
                value = f32(state.held.get((self.targets.holder, track, "played"), rule["defaults"])[name])
                state.materials.setdefault(self.targets.holder, {}).setdefault(None, {})[variable] = max(
                    value, rule.get("least", value))
            if "warp" in rule:
                self._warp(track, rule, state)
            for target, material, variable in rule.get("rows", ()) if "ratio" in rule else ():
                state.materials.setdefault(target, {}).setdefault(material, {})[variable] = state.held[
                    self.targets.holder, "ratio"]
            if (request := rule.get("request")) is not None and (row := state.held.get((
                    rule.get("target"), request["layer"], "request"))) is not None:
                _change(state, (rule["target"], request["layer"]), *row, frame, request["change"])
        if self.eyes is not None:
            self._blink(self.eyes, state, frame)
        if self.lips is not None:
            self._lip(self.lips, state, frame)
        spec = next((rule["rate"] for rule in self.rules.values() if "rate" in rule), None)
        for (target, _key), held in state.rates.items():
            held[0] = fadd(held[0], RATE_STEP)
            for param in held[1]:
                state.materials.setdefault(target, {}).setdefault(param[spec["material"]] or None, {})[
                    param[spec["variable"]]] = _rate_value(param, held[0], spec)
        for rule in self.rules.values():
            if "condition" in rule:
                self._conditions(rule, state)

    def _conditions(self, rule: dict, state: TrackState) -> None:
        works: dict = {}
        for at, ident in state.held.get((self.targets.holder, "conditions"), ()):
            for target, _shown, values in rule["objects"].get(ident, ()):
                for materials, variable, kind, value, frames in values:
                    for material in materials or (None,):
                        works.setdefault((target, material, variable), []).append((at, kind, value, f32(frames)))
        for (target, material, variable), listed in works.items():
            ends = [at for at, *_rest in listed[1:]] + [state.updates]
            if listed[0][1] == "texture":
                done = [value for (at, _kind, value, frames), end in zip(listed, ends) if end - at >= frames]
                value = done[-1] if done else None
            else:
                steps = tuple((value, None if frames <= 0 else fdiv(min(frames, f32(end - at)), frames))
                              for (at, _kind, value, frames), end in zip(listed, ends))
                value = steps[-1][0] if steps[-1][1] is None and None not in (
                    steps[-1][0] if isinstance(steps[-1][0], tuple) else ()) else Fade(steps)
            if value is not None:
                state.materials.setdefault(target, {}).setdefault(material, {})[variable] = value

    def _condition(self, rule: dict, ident, state: TrackState) -> None:
        if rule["condition"].get("materials", True):
            state.held.setdefault((self.targets.holder, "conditions"), []).append((state.updates, ident))
        for target, shown, _values in rule["objects"].get(ident, ()):
            state.parts.setdefault(target, {}).update(shown)

    def _held(self, track: str, rule: dict, spec: dict, state: TrackState) -> None:
        key = self.targets.holder, track, "held"
        member = state.held.pop((self.targets.holder, track, "played"), rule["defaults"][rule["condition"]["field"]])
        if member != state.held[key] and [member, state.held[key]] not in spec.get("blocked", ()):
            if (ident := rule["ids"].get(member)) is not None:
                self._condition(rule, ident, state)
            state.held[key] = member

    def _normals(self, track: str, rule: dict, requests: list, state: TrackState) -> None:
        spec, written = rule["muscle"], state.held.setdefault((self.targets.holder, track, "written"), {})
        for material, shape, value in sorted(requests, key=lambda request: request[:2]):
            if 0 <= shape < len(spec["slots"]):
                variable = murmur3_hash_utf16le(spec["variables"][spec["slots"][shape]])
                for name in rule["materials"].get(material, ()):
                    if (component := spec["components"][shape]) < 0:
                        written[name, variable] = value
                    else:
                        held = written.setdefault((name, variable), [None] * 4)
                        held[component] = value
        for (name, variable), value in written.items():
            state.materials.setdefault(rule["target"], {}).setdefault(name, {})[variable] = value if isinstance(
                value, float) else Fade(((tuple(value), None),))

    def _floats(self, track: str, rule: dict, spec: dict, state: TrackState) -> None:
        key = self.targets.holder, track
        played = state.held.get((*key, "played"), rule["defaults"])
        active = any(f32(played[name]) > 0 for name in spec["variables"])
        if active or state.held.get((*key, "active")):
            state.held[*key, "written"] = {variable: 0.0 if f32(played[name]) < 0 else min(f32(played[name]), f32(
                spec["limit"])) for name, variable in spec["variables"].items()}
        state.held[*key, "active"] = active
        for variable, value in state.held.get((*key, "written"), {}).items():
            state.materials.setdefault(self.targets.holder, {}).setdefault(None, {})[variable] = value

    def _range(self, node: tuple, rule: dict, spec: dict, fields: dict, state: TrackState) -> None:
        value, out = fields.get(spec["field"]), spec["range"]["out"]
        key, inside = (self.targets.holder, node, "range"), value != out
        if inside == state.held.get(key, False):
            return
        state.held[key] = inside
        if "holder" not in spec:
            if (ident := rule["ids"].get(str(value if inside else out))) is not None:
                self._condition(rule, ident, state)
            return
        if inside:
            state.held[*key, "fields"] = dict(fields)
        held, source = state.held[*key, "fields"], spec.get("enter" if inside else "exit")
        guids = held.get(source) if isinstance(source, str) else source.get(str(held.get(spec.get("by")))) \
            if isinstance(source, dict) else source
        for guid in [guids] if isinstance(guids, str) else guids or ():
            self._condition(rule, (guid, spec["holder"]), state)

    def _mode(self, rule: dict, modes: dict, value, state: TrackState) -> None:
        holder, action = self.targets.holder, modes["table"].get(value)
        if action is None or state.held[holder, "mode"] == value:
            return
        if "body" in action and (body := action["body"] or modes["body"]) != state.held[holder, "body"]:
            state.held[holder, "body"] = body
            self._condition(rule, rule["ids"][body], state)
        if action.get("alpha"):
            state.flags.update(dict.fromkeys(rule["forced"], frozenset(modes["alpha"]["flags"])))
            state.held[holder, "release"] = None
        if "condition" in action:
            self._condition(rule, rule["ids"][action["condition"]], state)
        if action.get("release") and state.held[holder, "mode"] != modes["released"]:
            state.held[holder, "mode"], state.held[holder, "release"] = modes["released"], state.updates
        elif not action.get("release") and not action.get("kept"):
            state.held[holder, "mode"] = value

    def _release(self, rule: dict, alpha: dict, state: TrackState) -> None:
        key = self.targets.holder, "release"
        if (start := state.held.get(key)) is not None and fsub(f32(state.updates), f32(start)) >= alpha["limit"]:
            for target in rule["forced"]:
                state.flags.pop(target, None)
            state.held[key] = None

    def _apply(self, rule: dict, fields: dict, state: TrackState, frame: float, track: str, index: int) -> None:
        self._apply_hair(rule, fields, state)
        self._apply_parts(rule, fields, state)
        self._apply_ratio(rule, fields, state)
        self._apply_condition(rule, fields, state, track, index)
        self._apply_scale(rule, fields, state)
        self._apply_blink(rule, fields, state)
        self._apply_lip(rule, fields, state)
        self._apply_glow(rule, fields, state)
        self._apply_played(rule, fields, state, track)
        self._apply_lights(rule, fields, state, track)
        self._apply_shapes(rule, fields, state)
        self._apply_normals(rule, fields, state, track)
        self._apply_states(rule, fields, state)
        self._apply_material(rule, fields, state)
        self._apply_draw(rule, fields, state)
        if self._apply_play(rule, fields, state, frame):
            return
        self._apply_request(rule, fields, state)
        self._apply_rate(rule, fields, state)
        self._apply_set(rule, fields, state, frame)
        self._apply_expression(rule, fields, state, frame)
        self._apply_emotion(rule, fields, state, track)
        self._apply_receiver(rule, fields, state, frame)
        self._apply_switch(rule, fields, state)
        self._apply_screen(rule, fields, state)
        self._apply_mode(rule, fields, state)
        self._apply_sequence(rule, fields, state)

    def _apply_hair(self, rule: dict, fields: dict, state: TrackState) -> None:
        if "hair" in rule:
            state.held[self.targets.holder, "hair"]["copied"] = dict(fields)

    def _apply_parts(self, rule: dict, fields: dict, state: TrackState) -> None:
        if (parts := rule.get("parts")) is not None and "when" in parts:
            if fields.get(parts["when"]) and "on" in parts:
                state.parts[self.targets.holder] = {None: False} | dict.fromkeys(parts["on"], True)
            elif fields.get(parts["when"]):
                state.parts.setdefault(self.targets.holder, {}).update(
                    (part, enabled) for name, enabled in parts["masks"] for bit, part in parts["bits"]
                    if fields.get(name, 0) & bit)
        elif parts is not None:
            part, name = fields.get(parts["part"]), fields.get(parts.get("object"))
            if isinstance(part, int) and part >= parts.get("least", 0):
                key, kept = (self.targets.holder, parts.get("object")), parts.get("empty") == "keep"
                target = state.held.get(key) if kept and not (isinstance(name, str) and name.strip()) else \
                    self.targets.find(name, parts)
                if kept:
                    state.held[key] = target
                if target is not None:
                    shown = state.parts.setdefault(target, {})
                    shown.update(dict.fromkeys(rule.get("off", ()), False))
                    shown[part] = bool(fields.get(parts["enabled"])) if "enabled" in parts else True

    def _apply_ratio(self, rule: dict, fields: dict, state: TrackState) -> None:
        if (ratio := rule.get("ratio")) is not None and "rows" in rule:
            state.held[self.targets.holder, "ratio"] = float(fields[ratio["value"]])

    def _apply_condition(self, rule: dict, fields: dict, state: TrackState, track: str, index: int) -> None:
        if (condition := rule.get("condition")) is not None and "modes" in condition:
            self._mode(rule, condition["modes"], fields.get(condition["field"]), state)
        elif condition is not None and "held" in condition:
            state.held[self.targets.holder, track, "played"] = fields.get(condition["field"])
        elif condition is not None and "range" in condition:
            self._range((track, index), rule, condition, fields, state)
        elif condition is not None and (pairs := condition.get("pairs")) is not None:
            held = fields.get(condition["field"]) or {}
            for ident, info in pairs["list"] if held.get(pairs["on"]) else ():
                if held.get(ident, pairs["none"]) != pairs["none"]:
                    self._condition(rule, (held[ident], int(held.get(info, 0))), state)
        elif condition is not None and (ident := rule["ids"].get(fields.get(condition["field"]))) is not None:
            if condition.get("delay"):
                state.held[self.targets.holder, track, "requested"] = ident
            elif not (applied := state.held.get((self.targets.holder, "conditions"))) or applied[-1][1] != ident:
                self._condition(rule, ident, state)

    def _apply_scale(self, rule: dict, fields: dict, state: TrackState) -> None:
        if (scale := rule.get("scale")) is not None and (target := self.targets.find(None, scale)) is not None:
            state.scales[target] = f32(fields[scale["field"]])

    def _apply_blink(self, rule: dict, fields: dict, state: TrackState) -> None:
        if (blink := rule.get("blink")) is not None and fields.get(blink["field"]):
            state.held[self.targets.holder, "blink", blink["flag"]] = True

    def _apply_lip(self, rule: dict, fields: dict, state: TrackState) -> None:
        if (lip := rule.get("lip")) is not None and fields.get(lip["field"], -1) >= 0:
            state.held[self.targets.holder, "lip", lip["kind"]] = fields[lip["field"]]

    def _apply_glow(self, rule: dict, fields: dict, state: TrackState) -> None:
        for kind in ("direct", "cycle") if "glow" in rule else ():
            name, used, *values = rule["glow"][kind]
            if (track := fields.get(name) or {}).get(used):
                state.held[self.targets.holder, "glow", kind] = tuple(f32(track[value]) for value in values)

    def _apply_played(self, rule: dict, fields: dict, state: TrackState, track: str) -> None:
        if "floats" in rule or "values" in rule or "warp" in rule:
            state.held[self.targets.holder, track, "played"] = dict(fields)

    def _apply_lights(self, rule: dict, fields: dict, state: TrackState, track: str) -> None:
        if (lights := rule.get("lights")) is not None:
            state.held[self.targets.holder, track, "draw"] = lights["before"] if fields.get(lights["default"]) else \
                bool(fields.get(lights["enabled"]))

    def _apply_shapes(self, rule: dict, fields: dict, state: TrackState) -> None:
        if (shapes := rule.get("shapes")) is not None and fields.get(shapes["player"][0]) == shapes["player"][1]:
            for value in rule["channels"].get(fields.get(shapes["id"]), ()):
                state.shapes.setdefault(rule["target"], {})[value] = f32(fields[shapes["value"]])

    def _apply_normals(self, rule: dict, fields: dict, state: TrackState, track: str) -> None:
        if (normals := rule.get("normals")) is not None and fields.get(normals["player"][0]) == normals["player"][1]:
            state.held.setdefault((self.targets.holder, track, "requests"), []).append(
                (fields.get(normals["material"]), fields.get(normals["id"]), f32(fields[normals["value"]])))

    def _apply_states(self, rule: dict, fields: dict, state: TrackState) -> None:
        if (states := rule.get("states")) is not None:
            (name, value, part), shown = states["first"], {}
            if fields[name] >= 0:
                shown[part] = fields[name] == value
            elif any(fields[pair[0]] >= 0 for pair in states["pairs"]):
                for name, value, equal, other in states["pairs"]:
                    shown[equal], shown[other] = fields[name] == value, fields[name] != value
            if shown:
                state.parts.setdefault(self.targets.holder, {}).update(shown)

    def _apply_material(self, rule: dict, fields: dict, state: TrackState) -> None:
        if (material := rule.get("material")) is not None:
            variable = fields.get(material["variable"])
            if isinstance(variable, str) and variable.strip() and (target := self.targets.find(
                    fields.get(material.get("object")), material)) is not None:
                state.materials.setdefault(target, {}).setdefault(fields.get(material["material"]), {})[variable] = \
                    float(fields.get(material["value"]))

    def _apply_draw(self, rule: dict, fields: dict, state: TrackState) -> None:
        if (draw := rule.get("draw")) is not None and (target := self.targets.find(
                draw["name"] if "name" in draw else fields.get(draw.get("object")), draw)) is not None:
            if "when" not in draw:
                state.drawn[target] = bool(fields.get(draw["drawn"]))
            elif fields.get(draw["when"]):
                state.drawn[target] = draw["drawn"]

    def _apply_play(self, rule: dict, fields: dict, state: TrackState, frame: float) -> bool | None:
        if (play := rule.get("play")) is not None and (target := self.targets.find(None, play)) is not None:
            key, priority = (target, play["layer"]), fields.get(play["priority"], 0)
            if priority != play.get("none") and priority >= state.held.get(key, 0):
                back, base, transition = fields.get(play.get("to_base")) or {}, rule.get("base"), rule["transition"]
                if not (back.get("_HasValue") and back.get("_Value")):
                    row = fields[play["bank"]], fields[play["motion"]], float(fields[play["interp"]])
                elif base:
                    row, priority = (base["bank"], base["motion"], base["interp"]), base["priority"]
                else:
                    return True
                state.changes.setdefault(key, []).append((float(frame), *row, *transition))
                state.held[key] = priority

    def _apply_request(self, rule: dict, fields: dict, state: TrackState) -> None:
        if (request := rule.get("request")) is not None and rule.get("target") is not None:
            key = rule["target"], request["layer"], "request"
            if request.get("stop") and fields.get(request["stop"]):
                state.held[key] = None
            elif "bank" in request and request["none"] not in (row := (fields[request["bank"]],
                                                                       fields[request["motion"]])):
                state.held[key] = row
            elif request.get("otherwise") == "stop":
                state.held[key] = None

    def _apply_rate(self, rule: dict, fields: dict, state: TrackState) -> None:
        if (rate := rule.get("rate")) is not None and (key := fields.get(rate["key"])):
            for name in (fields.get(rate["parts"]) or {}).values():
                target, groups = rule["groups"].get(name, (None, {}))
                if key in groups:
                    state.rates[target, key] = [f32(0.0), groups[key]]

    def _apply_set(self, rule: dict, fields: dict, state: TrackState, frame: float) -> None:
        if (direct := rule.get("set")) is not None and rule.get("target") is not None and (row := (
                direct["row"] if fields.get(direct["stop"]) else None) if "stop" in direct else None if fields[
                    direct["motion"]] == direct["none"] else (direct["bank"], fields[direct["motion"]])):
            state.changes.setdefault((rule["target"], direct["layer"]), []).append((float(frame), *row,
                                                                                    *direct["change"]))

    def _apply_expression(self, rule: dict, fields: dict, state: TrackState, frame: float) -> None:
        if (face := rule.get("expression")) is not None and rule.get("target") is not None:
            data, motion = rule["data"], fields.get(face["motion"], -1)
            key, kind = (rule["target"], data[face["layer"]]), fields.get(face["type"], face["empty"])
            if kind == face["empty"]:
                kind = face["manual"] if motion >= 0 else data[face["default"]]
            rows = state.changes.setdefault(key, [])
            if kind != state.held.get(key) or kind == face["manual"] and rows and rows[-1][2] != motion:
                rows.append((float(frame), *_expression(face, data, kind, motion)))
                state.held[key] = kind

    def _apply_emotion(self, rule: dict, fields: dict, state: TrackState, track: str) -> None:
        if (face := rule.get("emotion")) is not None and fields[face["emotion"]] != face["empty"][0] and \
                fields[face["index"]] < len(rule.get("faces", ())):
            state.held[self.targets.holder, track, fields[face["index"]]] = fields[face["emotion"]], fields[
                face["mouth"]]

    def _apply_receiver(self, rule: dict, fields: dict, state: TrackState, frame: float) -> None:
        if (face := rule.get("receiver")) is not None and rule.get("target") is not None:
            key, data, bank = (rule["target"], 0), rule["data"], rule["bank"]
            rows, held = state.changes[key], state.held[key]
            if face.get("default") in fields:
                held[0] = fields[face["default"]]
            motion = fields.get(face.get("motion"), -1)
            if motion >= 0 and rows[-1][2] != motion and not (fields[face["once"]] and held[2] == motion):
                interp, loop, found = fields[face["interp"]], fields[face["loop"]], rule["motion_of"](bank, motion)
                if found is not None:
                    rows.append((float(frame), bank, motion, float(interp if interp >= 0 else data[face["in"]]),
                                 *face["transition"], face["wrap"][bool(loop)]))
                held[1:] = motion, motion, None if found is None or loop or found.loop_frame >= 0 else \
                    frame + self.step + found.end_frame * self.fps / found.fps
            elif held[1] is None and rows[-1][2] != held[0] or held[3] is not None and frame >= held[3]:
                rows.append((float(frame), bank, held[0], float(data[face["out"]]), *face["transition"]))
                held[1] = held[3] = None

    def _apply_switch(self, rule: dict, fields: dict, state: TrackState) -> None:
        if (switch := rule.get("switch")) is not None:
            part, key = fields.get(switch["part"], -1), (self.targets.holder, switch["part"])
            rows = {row[switch["id"]]: row for row in rule["data"][switch["rows"]]}
            if part >= 0 and part != state.held.get(key):
                for value, shown in ((state.held.get(key), False), (part, True)):
                    if (row := rows.get(value)) and (target := self.targets.hashed(row[switch["hash"]])) is not None:
                        state.drawn[target] = shown
                        if shown:
                            state.held[key] = part
            if "material" in switch:
                self._material(rule, fields.get(switch["material"], -1), state)

    def _apply_screen(self, rule: dict, fields: dict, state: TrackState) -> None:
        if (screen := rule.get("screen")) is not None:
            state.held[rule["target"], "screen"] = fields[screen["monitor"]["field"]], fields[screen["parts"]["field"]]

    def _apply_mode(self, rule: dict, fields: dict, state: TrackState) -> None:
        if (mode := rule.get("mode")) is not None and rule.get("face") is not None:
            held = state.held.setdefault((self.targets.holder, "mode"), [None, 0, None])
            npc, process, rate = held
            if process in (1, 3):
                state.parts[rule["face"]] = dict(enumerate(mode["faces"][mode["blend"] if process == 1 else int(npc)]))
                process, rate = (2, rate) if process == 1 else (0, mode["rates"][int(npc)])
            for target, material in () if rate is None else rule["materials"]:
                state.materials.setdefault(target, {}).setdefault(material, {})[rule["variable"]] = rate
            if fields[mode["enemy"]] and npc is not False:
                npc, process = False, 1
            if fields[mode["npc"]] and npc is not True:
                npc, process = True, 1
            if fields[mode["rate"]] >= 0:
                rate = float(fields[mode["rate"]])
            elif process in (1, 2):
                process = 3
            held[:] = npc, process, rate

    def _apply_sequence(self, rule: dict, fields: dict, state: TrackState) -> None:
        if (face := rule.get("sequence")) is not None and rule.get("order") is not None:
            held = state.held[rule["target"], "sequence"]
            if held["order"] is None or rule["order"] <= held["order"]:
                values = fields[face["values"]]
                held |= {"track": (fields[face["id"]], [values[index] for index in sorted(values)]),
                         "seq": (fields[face["bank"]], fields[face["motion"]]), "order": rule["order"]}
                _flag(held, 0xB, False)
                _flag(held, 0xC, True)

    def _emotions(self, track: str, rule: dict, state: TrackState, frame: float) -> None:
        face = rule["emotion"]
        for index, (target, *layers) in enumerate(rule["faces"]):
            request = state.held.pop((self.targets.holder, track, index), (rule["start"], 0))
            for kind, (layer, bank), motion in zip(("emotion", "mouth"), layers, _facial_motions(face, *request)):
                rows = state.changes.get((target, layer))
                if rows is None or rows[-1][1:3] == (bank, motion):
                    continue
                frames, curve = _facial_interp(face["interp"], rule["interp"], kind, motion)
                if kind == "emotion":
                    guard = state.held.setdefault((target, layer, "guard"), [0, 0.0, None, 0.0])
                    if not _change_allowed(guard, frame, face["wait"]):
                        continue
                    guard[2:] = frame, frames
                rows.append((float(frame), bank, motion, frames, face["transition"], curve))

    def _blink(self, eyes: dict, state: TrackState, frame: float) -> None:
        holder = self.targets.holder
        kept = state.held.pop((holder, "blink", "close"), False)
        if state.held.pop((holder, "blink", "ignore"), False):
            return
        if "part" in eyes:
            if kept:
                state.parts.setdefault(holder, {})[eyes["part"]] = True
            return
        held = state.held.setdefault((holder, "blink"), [0, 0.0, 0.0])
        mode, ended = held[0], fsub(f32(frame), held[1]) >= held[2]
        play = "close" if kept and mode not in (2, 3) else "loop" if kept and mode == 2 and ended else \
            "open" if not kept and (mode == 3 or mode == 2 and ended) else None
        if play is None:
            held[0] = 0 if mode == 4 and ended else mode
            return
        if _change(state, (holder, eyes["layer"]), *eyes[play][:2], frame, eyes[play][2:5]):
            held[1:] = f32(frame), eyes[play][5]
        held[0] = {"close": 2, "loop": 3, "open": 4}[play]

    def _lip(self, lips: dict, state: TrackState, frame: float) -> None:
        holder, key = self.targets.holder, (lips["target"], lips["layer"])
        held = state.held.setdefault((holder, "lip"), {"rates": [0.0] * lips["samples"], "follow": 0.0})
        index, rate = (state.held.pop((holder, "lip", kind), none) for kind, none in (("index", -1), ("rate", 0.0)))
        held["rates"] = [*held["rates"], min(max(f32(rate), 0.0), lips["most"])][-lips["samples"]:]
        count = len(held["rates"])
        blend = fdiv(f32(sum(fmul(f32(at + 1), value) for at, value in enumerate(held["rates"]))),
                     f32(count * (count + 1) // 2))
        if blend > 0.0:
            _change(state, key, lips["bank"], lips["motions"][index != 0], frame, lips["start"])
        elif (rows := state.changes.get(key)) and rows[-1][2] != -1:
            _change(state, key, lips["bank"], -1, frame, lips["stop"])
        step, most = fsub(blend, held["follow"]), f32(lips["follow"])
        held["follow"] = fadd(held["follow"], step if abs(step) <= most else most if step > 0.0 else -most)
        state.variables[key] = dict(zip(lips["variables"], (blend, held["follow"])))

    def _glow(self, rule: dict, state: TrackState) -> None:
        glow, holder = rule["glow"], self.targets.holder
        held = state.held.setdefault((holder, "glow"), [0.0, True, False, 0.0, 0.0, 0.0, 0.0])
        direct, cycle = (state.held.pop((holder, "glow", kind), None) for kind in ("direct", "cycle"))
        if cycle is None:
            held[1:3], held[5:] = [True, False], [0.0, 0.0]
        else:
            if not held[2] or abs(fsub(held[4], cycle[2])) >= glow["epsilon"]:
                ratio = 0.0 if held[1] else 1.0 if held[4] <= 0.0 else fdiv(held[3], held[4])
                held[1:5] = [False, True, fmul(cycle[2], ratio), cycle[2]]
            held[5:] = cycle[:2]
        if held[2] and held[1]:
            held[1], held[3] = False, 0.0
        elif held[2] and held[4] <= (seconds := fadd(RATE_STEP, held[3])):
            held[1], held[3] = True, held[4]
        elif held[2]:
            held[3] = seconds
        aim = direct[0] if direct is not None else 0.0 if cycle is None else fadd(fmul(f32((1.0 - crt("cosf")(f32(
            (1.0 if held[1] or held[4] <= 0.0 else fdiv(held[3], held[4])) * glow["turn"]))) * 0.5), fsub(
            held[6], held[5])), held[5])
        step = f32(f32(rule["data"][glow["delta"]]) * glow["rate"] * RATE_STEP)
        held[0] = min(max(aim, fsub(held[0], step)), fadd(held[0], step))
        for name in glow["objects"]:
            if (target := self.targets.find(name, glow)) is not None:
                state.materials.setdefault(target, {}).setdefault(None, {})[glow["variable"]] = held[0]

    def _warp(self, track: str, rule: dict, state: TrackState) -> None:
        warp, holder = rule["warp"], self.targets.holder
        fields = state.held.get((holder, track, "played"), rule["defaults"])
        matrix = self.targets.document.objects[holder].document_world_matrix
        column = [[float(matrix[row][at]) for row in range(3)] for at in range(4)]
        centre = state.held.setdefault((holder, "warp"), column[3])
        offset, moved = [0.0, 0.0, 0.0], False
        if (front := f32(fields[warp["front"]])) != warp["unset"]:
            offset, moved = [fmul(axis, front) for axis in column[2]], True
        if (height := f32(fields[warp["height"]])) != warp["unset"]:
            offset[1], moved = fadd(height, offset[1]), True
        if (side := f32(fields[warp["side"]])) != warp["unset"]:
            offset, moved = [fadd(value, fmul(axis, side)) for value, axis in zip(offset, column[0])], True
        if moved:
            centre[:] = [fadd(base, value) for base, value in zip(column[3], offset)]
        radius = 0.0 if (size := f32(fields[warp["radius"]])) == warp["none"] else size
        for name in warp["objects"]:
            if (target := self.targets.find(name, warp)) is not None:
                state.materials.setdefault(target, {}).setdefault(None, {}).update(
                    {warp["position"]: (*centre, 0.0), warp["size"]: radius})

    def _sequence(self, rule: dict, state: TrackState, frame: float) -> None:
        face, target = rule["sequence"], rule["target"]
        held = state.held[target, "sequence"]
        now, sequenced = held["now"], {8, 9, 0xB}
        if frame >= held["busy"]:
            layer, bank, plain, sequence = face["base"]
            _change(state, (target, layer), bank, sequence if now & sequenced and not rule["wait"] else plain, frame,
                    face["change"])
        bank, motion = held["seq"]
        if motion == face["none"]:
            _layer, bank, motion = face["common"]
        if _change(state, (target, face["common"][0]), bank, motion, frame, face["change"]):
            held["busy"] = frame + face["change"][0]
        if 9 in now and 8 not in now:
            _flag(held, 0xB, True)
        if 0xB in now:
            _facial_values(held, 0, [0.0] * len(held["hashes"]), True)
        if 8 in now:
            _flag(held, 9, True)
        now &= held["next"]
        held["next"].clear()
        if 0xC not in now:
            held["track"], held["seq"] = None, (0, face["none"])
        held["order"] = None
        if held["track"] is not None:
            _facial_values(held, *held["track"], False)
        if now & sequenced:
            state.joints[target, face["eye"][0]] = {value: (0.0, 0.0, float(f32(amount * held["rate"])))
                                                    for value, amount in zip(held["hashes"], held["values"])}

    def _hair(self, rule: dict, state: TrackState) -> None:
        held = state.held[self.targets.holder, "hair"]
        copied, spec = held["copied"], rule["hair"]["buffer"]
        if rule.get("buffer") is not None:
            rates = [f32(copied[name]) for name in spec["values"]]
            if copied[spec["override"]]:
                state.buffers[rule["buffer"]] = dict(enumerate(rates))
            elif (progress := _timed(held["buffer"], copied[spec["return"]])) is not None:
                factor = fsub(1.0, progress)
                factor = 0.0 if factor < 0 else min(factor, 1.0)
                state.buffers[rule["buffer"]] = {index: fmul(factor, rate) for index, rate in enumerate(rates)}
            held["buffer"][3] = bool(copied[spec["override"]])
        spec = rule["hair"]["gravity"]
        if copied[spec["override"]]:
            held["rate"] = f32(copied[spec["rate"]])
            state.gravity = GravitySet(held["rate"])
        elif (progress := _timed(held["gravity"], copied[spec["return"]])) is not None:
            state.gravity = None if held["rate"] is None else GravitySet(held["rate"], progress)
        held["gravity"][3] = bool(copied[spec["override"]])

    def _material(self, rule: dict, material: int, state: TrackState) -> None:
        switch, data = rule["switch"], rule["data"]
        key = self.targets.holder, switch["material"]
        row = next((row for row in data[switch["materials"]] if row[switch["id"]] == material), None) \
            if material >= 0 and material != state.held.get(key) else None
        if row is None:
            return
        for part in data[switch["rows"]]:
            mdf = next((item[switch["mdf"]] for item in row[switch["parts"]] or ()
                        if item[switch["parts_id"]] == part[switch["id"]]), None)
            if mdf and (target := self.targets.hashed(part[switch["hash"]])) is not None:
                state.meshes[target] = mdf
        state.held[key] = material


def _timed(timer: list, seconds: float) -> float | None:
    if timer[3]:
        timer[:3] = f32(seconds), 0.0, False
    if timer[2]:
        return None
    limit, seconds = timer[0], fadd(timer[1], RATE_STEP)
    timer[1:3] = (limit, True) if seconds >= limit else (seconds, False)
    return 1.0 if limit <= SHORTEST else min(fdiv(timer[1], limit), 1.0)


def condition_pairs(spec: dict, nodes) -> set[tuple[str, int]]:
    found = set()
    if "holder" in spec:
        for source in filter(None, (spec.get("enter"), spec.get("exit"))):
            if isinstance(source, dict):
                found.update(guid for value in source.values()
                             for guid in ([value] if isinstance(value, str) else value))
            elif isinstance(source, list):
                found.update(source)
            else:
                found.update(key.value for node in nodes for prop in node.properties if prop.name == source
                             for key in prop.keys if key.value)
        return {(guid, spec["holder"]) for guid in found}
    for prop in (prop for node in nodes for prop in node.properties if prop.name == spec["field"]) \
            if "pairs" in spec else ():
        keys = {child.name: {key.value for key in child.keys} for child in prop.children}
        found.update((ident, int(holder)) for name, info in spec["pairs"]["list"] for ident in keys.get(name, ())
                     if ident and ident != spec["pairs"]["none"] for holder in keys.get(info) or (0,))
    return found


def passes(row, filters: list[int]) -> bool:
    return all((not wanted or wanted & mask) and not invalid & mask
               for wanted, invalid, mask in zip(row.filters, row.invalid, filters))


def _screen(screen: dict, monitor: dict, parts, sprites: dict) -> dict:
    names, (width, height), (left, top), turn = screen["fields"], screen["size"], screen["position"], screen["turn"]

    def place(item: dict, drawn: bool = True) -> tuple[float, float, float, float]:
        (x, y), (u, v) = _xy(item[names["size"]]), _xy(item[names["position"]])
        return (x / width, y / height, u / left, v / top) if drawn else (0.0, 0.0, u / left, v / top)

    first, each = screen["monitor"], screen["parts"]
    values = {first["alpha"]: monitor[names["alpha"]], first["brightness"]: monitor[names["brightness"]],
              first["rotation"]: monitor[names["rotation"]] / turn, first["place"]: place(monitor)}
    for index, part in enumerate([parts[index] for index in sorted(parts)] if isinstance(parts, dict) else parts):
        prefix, texture = each["prefix"].format(index), sprites.get(part[names["texture"]]) or None
        values |= {prefix + each["place"]: place(part, texture is not None and part[names["visible"]]),
                   prefix + each["rotation"]: part[names["rotation"]] / turn,
                   prefix + each["alpha"]: part[names["alpha"]]}
        if texture is not None:
            values[prefix + each["texture"]] = texture
    return values


def _flag(held: dict, bit: int, on: bool) -> None:
    for bits in (held["now"], held["next"]):
        if on:
            bits.add(bit)
        else:
            bits.discard(bit)


def _change(state: TrackState, key: tuple, bank: int, motion: int, frame: float, change: list) -> bool:
    rows = state.changes.setdefault(key, [])
    if rows and rows[-1][1:3] == (bank, motion):
        return False
    rows.append((float(frame), bank, motion, *change))
    return True


def _facial_motions(face: dict, emotion: int, mouth: int) -> tuple[int, int]:
    mouths = face["mouths"]
    return -1 if emotion == face["none"][0] else emotion, mouths[mouth - 1] if 0 < mouth <= len(mouths) else -1


def _facial_interp(spec: dict, interp: dict | None, kind: str, motion: int) -> tuple[float, int]:
    if not interp:
        return tuple(spec["missing"])
    listed, key, frames, curve = spec["listed"]
    if kind == "emotion" and (entry := next((item for item in interp.get(listed) or () if item[key] == motion), None)):
        return float(entry[frames]), int(entry[curve])
    frames, curve = spec[kind]
    return float(interp[frames]), int(interp[curve])


def _change_allowed(guard: list, frame: float, wait: float) -> bool:
    count, waited, changed, frames = guard
    rate = 0.0 if changed is None else max(fsub(1.0, fdiv(fsub(frame, changed), fadd(f32(frames), SHORTEST))), 0.0)
    if not rate:
        guard[:2] = 0, 0.0
        return True
    if waited > (1.0 - rate) * frames:
        return False
    guard[0] = count + 1
    if count > 0:
        guard[1] = fadd(waited, wait)
    return True


def _facial_values(held: dict, identity: int, values: list, release: bool) -> None:
    held |= {"values": values, "rate": 0.0 if release else 1.0}
    if identity:
        _flag(held, 8, True)


def _rate_value(param: dict, seconds: float, spec: dict) -> float | tuple[float, ...]:
    duration = f32(param[spec["duration"]])
    rate = 1.0 if duration <= 1.401298464324817e-45 else max(min(fdiv(seconds, duration), 1.0), 0.0)
    pairs = zip(param[spec["start4"]], param[spec["end4"]]) if param[spec["four"]] else (
        (param[spec["start"]], param[spec["end"]]),)
    values = tuple(fadd(fmul(fsub(f32(end), f32(start)), rate), f32(start)) for start, end in pairs)
    return values if param[spec["four"]] else values[0]


def _xy(value) -> tuple[float, float]:
    return (value["x"], value["y"]) if isinstance(value, dict) else tuple(value)


def _expression(face: dict, data: dict, kind: int, motion: int) -> tuple:
    if kind != face["manual"]:
        listed, choices = data.get(face["list"]) or (), data.get(face["choices"][0]) or ()
        motion = (choices[kind][face["choices"][1]] or [-1])[0] if data.get(face["random"]) and 0 <= kind < len(
            choices) else listed[kind] if 0 <= kind < len(listed) else -1
    if motion < 0:
        return data[face["bank"]], -1, float(data[face["out"]]), *face["out_transition"]
    return data[face["bank"]], motion, float(data[face["in"]]), *face["transition"]


def driven(rule: dict, fields: dict, user) -> dict | None:
    driver = rule["driver"]

    def walk(value, path):
        for key in path:
            value = (user(value) if isinstance(value, str) else value) or {}
            value = value.get(key) if isinstance(value, dict) else None
        return user(value) if isinstance(value, str) and value.casefold().endswith(".user") else value
    settings = walk(fields, driver.get("settings", ()))
    if not isinstance(settings, dict) or "enabled" in driver and not settings.get(driver["enabled"]) or \
            "when" in driver and walk(fields, driver["when"][0]) != driver["when"][1]:
        return None
    data, base = walk(settings, driver.get("data", ())), driver.get("base")
    row = next((row for row in walk(data, base["rows"]) or () if all(
        row.get(key) == value for key, value in base["where"].items())), None) if base else None
    return {**rule, "data": data, "transition": tuple(int(walk(data, driver[name]) or 0) for name in (
        "mode", "curve")) if "mode" in driver else None, "base": row and {
        "bank": row[base["bank"]], "motion": row[base["motion"]], "interp": float(row[base["interp"]]),
        "priority": base["priority"]}}
