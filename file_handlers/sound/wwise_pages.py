"""Wwise-style property pages for compiled Wwise objects and bank chunks. See docs/internals/wwise-property-pages.md."""

from __future__ import annotations

from dataclasses import dataclass, field
import re

from .wwise_schema import property_names


@dataclass(slots=True)
class PageControl:
    kind: str
    key: str
    label: str = ""
    hints: dict = field(default_factory=dict)


@dataclass(slots=True)
class PageGroup:
    title: str
    override: str = ""
    controls: list[PageControl] = field(default_factory=list)


@dataclass(slots=True)
class PageTab:
    title: str
    groups: list[PageGroup] = field(default_factory=list)


_UNIT = re.compile(r"\s*\((ms|dB|%|cents|seconds)\)$")
_STEP = {0: "Step", 1: "Continuous"}
_KEYED = re.compile(r"(^|/)(Properties|Randomizers)/")


def split_unit(label: str) -> tuple[str, str]:
    match = _UNIT.search(label)
    if not match:
        return label, ""
    return label[:match.start()], {"seconds": "s"}.get(match[1], match[1])


def keyed_path(path: str) -> bool:
    return bool(_KEYED.search(path))


def _join(*parts: str) -> str:
    return "/".join(part for part in parts if part)


class _Pages:
    def __init__(self, layout, lists, version: int, kind: str = "object"):
        self.fields = {item.path: item for item in layout.fields}
        self.lists = {item.count_path: item for item in lists}
        self.version, self.kind = version, kind
        self.names = property_names(kind, version)
        self.ids = {name: key for key, name in self.names.items()}
        self.claimed: set[str] = set()
        self.tabs: dict[str, PageTab] = {}
        self.placed: dict[str, set[int]] = {}
        self.other_tab, self.addable = "Advanced Settings", set(self.names)

    def value(self, path, default=0):
        item = self.fields.get(path)
        return item.value if item is not None else default

    def group(self, tab: str, title: str, override: str = "") -> PageGroup:
        group = PageGroup(title, override if override in self.fields else "")
        self.claimed.add(group.override)
        self.tabs.setdefault(tab, PageTab(tab)).groups.append(group)
        return group

    def add(self, group, kind, key, label="", claims=(), **hints) -> None:
        group.controls.append(PageControl(kind, key, label, hints))
        self.claimed.update(claims)

    def field(self, group, path, label="", **hints) -> bool:
        if path not in self.fields:
            return False
        claims = [path, *(hints.get("random") or ())]
        self.add(group, "field", path, label, claims, **hints)
        return True

    def fields_under(self, group, base, **hints) -> None:
        for path, item in self.fields.items():
            if item.visible and path.startswith(base + "/") and path not in self.claimed and path not in self.lists and not self.inside_list(path):
                self.field(group, path, **hints)

    def inside_list(self, path) -> bool:
        return any(
            path == root or path.startswith(root + "/")
            for entry in self.lists.values() for lane in entry.lanes for root in lane if root
        )

    def list(self, group, count_path, label="", **hints) -> bool:
        entry = self.lists.get(count_path)
        if entry is None:
            return False
        roots = tuple(root for lane in entry.lanes for root in lane if root)
        claims = [count_path, *(
            path for path in self.fields if any(path == root or path.startswith(root + "/") for root in roots)
        )]
        self.add(group, "list", count_path, label, claims, **hints)
        return True

    def prop_id(self, *names):
        return next((self.ids[name] for name in names if name in self.ids), None)

    def prop(self, group, section, *names, label="", present=False, **hints) -> int | None:
        prop_id = self.prop_id(*names)
        placed = self.placed.setdefault(section, set())
        if prop_id is None or prop_id in placed:
            return None
        if present and not self.has_prop(section, prop_id):
            return None
        placed.add(prop_id)
        self.add(group, "property", section, label, id=prop_id, **hints)
        return prop_id

    def has_prop(self, section, prop_id) -> bool:
        name = self.names.get(prop_id, "")
        return _join(section, "Properties", f"{name} value") in self.fields or _join(
            section, "Randomizers", f"{name} minimum"
        ) in self.fields

    def finish(self) -> list[PageTab]:
        for path in [path for path in self.fields if path.endswith("Properties/Property count")]:
            section = path[:-len("Properties/Property count")].rstrip("/")
            present = {
                int(item.value) for key, item in self.fields.items()
                if key.startswith(_join(section, "Properties") + "/") and key.endswith(" type")
                or key.startswith(_join(section, "Randomizers") + "/") and key.endswith(" type")
            }
            group = self.group(self.other_tab, "Other Properties")
            placed = self.placed.setdefault(section, set())
            for prop_id in sorted(present - placed):
                placed.add(prop_id)
                self.add(group, "property", section, id=prop_id)
            addable = tuple(sorted(self.addable - placed))
            if addable:
                self.add(group, "add_property", section, ids=addable)
            self.claimed.update(
                key for key in self.fields
                if key.startswith(_join(section, "Properties") + "/") or key.startswith(_join(section, "Randomizers") + "/")
                or key in {_join(section, "Properties/Property count"), _join(section, "Randomizers/Randomizer count")}
            )
        rest = [item.path for item in self.fields.values() if item.visible and item.path not in self.claimed]
        if rest:
            group = self.group("Advanced Settings", "Other Settings")
            for path in rest:
                self.field(group, path)
        return [tab for tab in self.tabs.values() if any(group.controls for group in tab.groups)]


def _voice(b, section, *names, title="Voice") -> PageGroup:
    group = b.group("General Settings", title)
    for name in names:
        b.prop(group, section, *(name if isinstance(name, tuple) else (name,)))
    return group


def _output(b, p, section, bus=False) -> None:
    group = b.group("General Settings", "Output Bus")
    b.field(group, p + "Output bus", "Output bus", none_label="Same as parent" if not bus else "None")
    b.field(group, p + "Audio device", "Audio device")
    b.prop(group, section, "Output Bus Volume", label="Volume")
    b.prop(group, section, "Output Bus LPF", label="Low-pass filter")
    b.prop(group, section, "Output Bus HPF", label="High-pass filter")
    b.field(group, p + "Feedback bus", "Feedback bus")
    b.prop(group, section, "Feedback Volume", "Feedback Volume (deprecated)", label="Feedback volume", present=True)
    b.prop(group, section, "Feedback LPF", "Feedback LPF (deprecated)", label="Feedback low-pass filter", present=True)


def _sends(b, p, section) -> None:
    base = p + "Auxiliary sends/"
    game = b.group("General Settings", "Game-Defined Auxiliary Sends", base + "Override game-defined sends")
    b.field(game, base + "Use game-defined sends", "Use game-defined auxiliary sends")
    enabled = {"enabled_by": (base + "Use game-defined sends", (1,))}
    b.prop(game, section, "Game Aux Send", "Game Aux Send Volume", label="Volume", **enabled)
    b.prop(game, section, "Game Aux Send LPF", label="Low-pass filter", **enabled)
    b.prop(game, section, "Game Aux Send HPF", label="High-pass filter", **enabled)
    user = b.group("General Settings", "User-Defined Auxiliary Sends", base + "Override user-defined sends")
    slots = []
    for slot in range(4):
        ids = (
            b.prop_id(f"User Aux Send {slot}", f"User Aux Send Volume {slot}"),
            b.prop_id(f"User Aux Send LPF {slot}"), b.prop_id(f"User Aux Send HPF {slot}"),
        )
        b.placed.setdefault(section, set()).update(item for item in ids if item is not None)
        slots.append(ids)
    b.add(
        user, "sends", base.rstrip("/"), claims=(
            base + "Has user-defined sends", *(f"{base}User send {slot} bus" for slot in range(1, 5)),
        ), section=section, slots=tuple(slots),
    )
    if base + "Override reflections bus" in b.fields:
        early = b.group("General Settings", "Early Reflections", base + "Override reflections bus")
        b.field(early, base + "Reflections bus", "Auxiliary bus")
        b.prop(early, section, "Reflections Bus Volume", label="Volume")


def _effects(b, p, *, node: bool) -> None:
    base = p + "Effects/"
    if base + "Effect count" not in b.lists:
        return
    group = b.group("Effects", "Effects", base + "Override parent effects" if node else "")
    b.field(group, base + "Bypass all effects", "Bypass all")
    b.add(group, "effects", base + "Effect count", claims=(
        *(f"{base}Bypass slot {slot}" for slot in range(4)),
        *(path for path in b.fields if path.startswith(base) and path[len(base):].split("/")[0].isdigit()),
        base + "Effect count",
    ), bypass_base=base.rstrip("/"), columns=("Slot", "Effect", "Bypassed", "Shared effect", "Rendered effect"))
    mixer = b.group("Effects", "Mixer Plug-in")
    b.field(mixer, p + "Mixer plug-in", "Mixer plug-in")
    b.field(mixer, p + "Mixer plug-in is a ShareSet", "ShareSet")
    b.field(mixer, p + "Override attachment parameters", "Override parent mixer input" if node else "Override attachment parameters")
    group = b.group("Metadata", "Metadata", base.replace("Effects/", "Metadata/") + "Override parent metadata")
    b.list(group, p + "Metadata/Metadata count", "Metadata", columns=("Slot", "Effect", "Shared effect"))


def _positioning(b, p, section) -> None:
    base = p + "Positioning/"
    override = base + "Override parent positioning"
    if override not in b.fields:
        return
    if b.version <= 129:
        group = b.group("Positioning", "Positioning", override)
        for label in ("2D positioning", "Enable panner", "3D positioning available", "Positioning type from RTPC"):
            b.field(group, base + label)
        b.prop(group, section, "Center %")
        b.prop(group, section, "Pan Left/Right", label="Pan left-right")
        b.prop(group, section, "Pan Front/Rear", label="Pan front-rear")
        spatial = b.group("Positioning", "3D", override)
        for label in ("Spatialization", "3D position type", "Follow listener orientation", "Update at each frame", "Attenuation"):
            b.field(spatial, base + label)
    else:
        group = b.group("Positioning", "Listener Relative Routing", override)
        b.field(group, base + "Listener-relative routing", "Listener relative routing")
        routed = {"enabled_by": (base + "Listener-relative routing", (1,))}
        b.field(group, base + "Spatialization", "3D spatialization", **routed)
        b.prop(group, section, "Positioning Type Blend", label="Speaker panning / 3D spatialization mix", **routed)
        b.field(group, base + "Enable attenuation", "Enable attenuation", **routed)
        b.prop(group, section, "Attenuation Enabled", label="Enable attenuation", present=True)
        b.prop(group, section, "Attenuation ShortID", label="Attenuation", **routed)
        b.field(group, base + "Enable diffraction", "Enable diffraction", **routed)
        panning = b.group("Positioning", "Speaker Panning", override)
        b.field(panning, base + "Speaker panner", "Speaker panning")
        b.prop(panning, section, "Center %")
        for names, label in (
            (("Pan Left/Right", "Pan X (2D)"), "Pan left-right"), (("Pan Front/Rear", "Pan Y (2D)"), "Pan front-rear"),
            (("Pan Up/Down", "Pan Z (2D)"), "Pan up-down"),
        ):
            b.prop(panning, section, *names, label=label)
        position = b.group("Positioning", "3D Position", override)
        b.field(position, base + "3D position source", "3D position")
        b.field(position, base + "Hold emitter position and orientation")
        b.field(position, base + "Hold listener orientation")
        for names, label in (("Pan X (3D)", "Pan left-right (3D)"), ("Pan Y (3D)", "Pan front-rear (3D)"), ("Pan Z (3D)", "Pan up-down (3D)")):
            b.prop(position, section, names, label=label)
    if base + "Path mode" in b.fields or base + "Vertex count" in b.fields:
        automation = b.group("Positioning", "3D Automation", override)
        b.field(automation, base + "Path mode", "Play type")
        b.field(automation, base + "Loop 3D automation path", "Loop")
        b.field(automation, base + "Transition time (ms)", "Transition time")
        b.list(automation, base + "Path count", "Paths", lanes=True)
        b.list(automation, base + "Vertex count", "Path vertices")
    else:
        b.field(b.group("Positioning", "3D Automation", override), base + "Loop 3D automation path", "Loop")
    b.prop(b.group("Positioning", "Cone Attenuation"), section, "Cone Attenuation Enabled", present=True)


def _advanced(b, p, section, *, bus=False) -> None:
    base = p + ("Bus playback/" if bus else "Playback limits and virtualization/")
    limit = b.group("Advanced Settings", "Playback Limit")
    b.field(limit, base + "Ignore parent instance limit", "Ignore parent")
    b.add(limit, "limit", base + "Maximum instances", claims=(base + "Maximum instances",))
    b.field(limit, base + "Global playback limit", "Limit scope", choices={0: "Per game object", 1: "Globally"})
    b.field(limit, base + "When limit reached: kill newest", "When priority is equal", choices={
        0: "Discard oldest instance", 1: "Discard newest instance",
    })
    b.field(limit, base + "Use virtual behavior", "When limit is reached", choices={0: "Kill voice", 1: "Use virtual voice settings"})
    b.prop(limit, section, "Maximum Instances", present=True)
    if bus:
        return
    virtual = b.group("Advanced Settings", "Virtual Voice", base + "Override virtual voice behavior")
    b.field(virtual, base + "Below-threshold behavior", "Virtual voice behavior")
    b.field(virtual, base + "Virtual queue behavior", "On return to physical voice")
    priority = b.group("Advanced Settings", "Playback Priority", p + "Override parent priority")
    b.prop(priority, section, "Priority")
    b.field(priority, p + "Apply distance to priority", "Use priority distance factor")
    b.prop(priority, section, "Priority Distance Offset", label="Offset priority at max distance",
           enabled_by=(p + "Apply distance to priority", (1,)))
    hdr = b.group("HDR", "Envelope Tracking", base + "Override HDR envelope")
    b.field(hdr, base + "Enable envelope")
    b.prop(hdr, section, "HDR Active Range", label="Active range")
    loudness = b.group("HDR", "Loudness Normalization", base + "Override analysis")
    b.field(loudness, base + "Normalize loudness")


def _states(b, p) -> None:
    b.list(b.group("States", "State Groups"), p + "State groups/Group count", "State groups", summary=("State group", "Synchronization"))
    b.list(b.group("States", "State Properties"), p + "State properties/Property count", "Properties that states change")


def _rtpc(b, p) -> None:
    b.list(b.group("RTPC", "RTPC"), p + "RTPC curves/Curve count", "Curves", summary=("Target property", "Input ID"))


def _midi(b, section, *, music=False, track=False) -> None:
    p = "Node/"
    events = b.group("MIDI", "MIDI Events", p + "Override MIDI event behavior")
    b.prop(events, section, "MIDI Play-on-note Type", label="Play on")
    b.field(events, p + "Break MIDI loop on note-off", "Break on note-off")
    tracking = b.group("MIDI", "Note Tracking", p + "Override MIDI note tracking")
    b.field(tracking, p + "Enable MIDI note tracking", "Enable note tracking")
    b.prop(tracking, section, "MIDI Root Note", label="Root note")
    transform = b.group("MIDI", "Transformation")
    b.prop(transform, section, "MIDI Transposition", label="Transposition")
    b.prop(transform, section, "MIDI Velocity Offset", label="Velocity offset")
    filters = b.group("MIDI", "Filters")
    for name, label in (
        ("MIDI Key Minimum", "Key range minimum"), ("MIDI Key Maximum", "Key range maximum"),
        ("MIDI Velocity Minimum", "Velocity minimum"), ("MIDI Velocity Maximum", "Velocity maximum"),
        ("MIDI Channel Mask", "Channel filter"),
    ):
        b.prop(filters, section, name, label=label, present=music)
    if music or track:
        target = b.group("MIDI", "MIDI Target", "Override parent MIDI target")
        b.prop(target, section, "MIDI Target Node", label="Target")
        b.field(target, "MIDI target is a bus", "Target is a bus")
        tempo = b.group("MIDI", "MIDI Clip Tempo", "Override parent MIDI tempo")
        b.prop(tempo, section, "MIDI Tempo Source", label="Tempo source")


def _node(b, name: str) -> None:
    music = name in {"Music Segment", "Music Switch", "Music Random/Sequence"}
    track = name == "Music Track"
    playable = name in {"Sound", "Random/Sequence Container", "Switch Container", "Layer Container", "Feedback Node"}
    section = "Node"
    voice = _voice(
        b, section, "Volume (dB)", *(() if music or track else ("Pitch (cents)",)), "LPF (%)", "HPF (%)",
        "Make-up Gain (dB)", *(("Initial Delay (seconds)",) if playable else ()),
        *(("Playback Speed",) if music else ()),
    )
    b.prop(voice, section, "LFE (dB)", present=True)
    b.claimed.add("Node/Parent")
    _output(b, "Node/", section)
    _sends(b, "Node/", section)
    if name == "Sound":
        b.add(b.group("General Settings", "Loop"), "loop", section, id=b.prop_id("Loop count (0 = infinite)"))
        b.placed.setdefault(section, set()).add(b.prop_id("Loop count (0 = infinite)"))
    _effects(b, "Node/", node=True)
    _positioning(b, "Node/", section)
    _rtpc(b, "Node/")
    _states(b, "Node/")
    _advanced(b, "Node/", section)
    _midi(b, section, music=music, track=track)
    b.list(b.group("Contents", "Children"), "Children/Child count", "Children", readonly=True)


def _source(b, base, tab="General Settings") -> None:
    source = b.group(tab, "Source")
    b.field(source, base + "Codec / source plug-in", "Codec / plug-in")
    b.field(source, base + "Source ID", "Source ID")
    b.field(source, base + "In-memory media bytes", "In-memory media bytes")
    b.field(source, base + "Language-specific", "Language-specific (voice)")
    b.field(source, base + "Has source", "Has source")
    stream = b.group(tab, "Stream")
    b.add(stream, "storage", base + "Storage type", claims=(base + "Storage type",))
    b.field(stream, base + "Prefetch", "Prefetch")
    b.field(stream, base + "Non-cacheable", "Non-cachable")
    b.claimed.add(base + "Plug-in parameter bytes")
    _plugin(b, "Source Plug-in", base + "Plug-in parameters")


def _plugin(b, tab, base) -> None:
    if base in b.fields:
        b.field(b.group(tab, "Plug-in"), base, "Parameter bytes")
        return
    groups = {}
    for path, item in b.fields.items():
        if not item.visible or not path.startswith(base + "/") or path in b.claimed or path in b.lists or b.inside_list(path):
            continue
        rest = path[len(base) + 1:]
        title, _, label = rest.rpartition("/")
        if not title and ": " in label:
            title, label = label.split(": ", 1)
        group = groups.get(title) or groups.setdefault(title, b.group(tab, title or "Parameters"))
        b.field(group, path, label)
    for entry in [entry for count, entry in b.lists.items() if count.startswith(base + "/")]:
        if entry.count_path not in b.claimed:
            title = entry.count_path[len(base) + 1:].rsplit("/", 1)[0] or "Parameters"
            b.list(b.group(tab, title), entry.count_path, title)


def _sound(b, name) -> None:
    _source(b, "Source/")
    for label in ("Loop Start", "Loop End", "Trim In Time", "Trim Out Time", "Fade In Time", "Fade Out Time",
                  "Fade In Curve", "Fade Out Curve", "Loop Crossfade Duration", "Crossfade Up Curve", "Crossfade Down Curve"):
        b.prop(b.group("General Settings", "Source"), "Node", label, present=True)
    _node(b, name)
    b.tabs["General Settings"].groups.sort(key=lambda group: group.title not in {"Voice", "Output Bus"})


def _random(b, name) -> None:
    base = "Playlist behavior/"
    mode = base + "Container mode"
    play = b.group("General Settings", "Play Type")
    b.field(play, mode, "Play type", radio=True)
    b.field(play, base + "Random mode", "Random type", choices={0: "Standard", 1: "Shuffle"}, enabled_by=(mode, (0,)))
    b.field(play, base + "Avoid-repeat count", "Avoid repeating last", enabled_by=(mode, (0,)))
    b.field(play, base + "Use weights", "Use weights", enabled_by=(mode, (0,)))
    b.field(play, base + "Restart backwards", "At end of playlist", choices={0: "Restart", 1: "Play in reverse order"},
            enabled_by=(mode, (1,)))
    continuous = base + "Continuous"
    looping = b.group("General Settings", "Play Mode")
    b.field(looping, continuous, "Play mode", choices=_STEP, radio=True)
    b.field(looping, base + "Reset playlist on play", "Always reset playlist", enabled_by=(continuous, (1,)))
    b.add(looping, "loop_field", base + "Loop count", claims=(
        base + "Loop count", base + "Loop random minimum", base + "Loop random maximum",
    ), random=(base + "Loop random minimum", base + "Loop random maximum"), enabled_by=(continuous, (1,)))
    b.field(looping, base + "Transition mode", "Transitions", enabled_by=(continuous, (1,)))
    b.field(looping, base + "Transition time (ms)", "Duration", random=(
        base + "Transition random minimum (ms)", base + "Transition random maximum (ms)",
    ), enabled_by=(continuous, (1,)))
    b.field(b.group("General Settings", "Scope"), base + "Global playlist", "Scope", choices={0: "Game object", 1: "Global"}, radio=True)
    b.list(b.group("Playlist", "Playlist"), "Playlist/Item count", "Playlist", children=True, columns=("Object", "Weight"))
    _node(b, name)


def _switch(b, name) -> None:
    group = b.group("General Settings", "Switch")
    b.field(group, "Group type", "Group type", radio=True)
    b.field(group, "Group", "Group")
    b.field(group, "Default value", "Default switch / state")
    b.field(group, "Continuous validation", "Play mode", choices=_STEP, radio=True)
    b.list(b.group("Assigned Objects", "Assigned Objects"), "Value assignments/Assignment count", "Assignments", children=True, summary=("Value",))
    b.list(b.group("Assigned Objects", "Playback Behavior"), "Switch transitions/Transition count", "Children", children=True)
    _node(b, name)


def _layer(b, name) -> None:
    b.field(b.group("General Settings", "Blend"), "Continuous validation", "Play mode", choices=_STEP, radio=True)
    b.list(b.group("Blend Tracks", "Blend Tracks"), "Layers/Layer count", "Blend tracks", children=True, summary=("Layer ID", "Crossfade input"))
    _node(b, name)


def _meter(b) -> None:
    group = b.group("General Settings", "Time Settings", "Meter/Override meter")
    b.field(group, "Meter/Tempo", "Tempo", suffix="BPM")
    b.field(group, "Meter/Beats per bar", "Time signature (beats per bar)")
    b.field(group, "Meter/Beat value", "Time signature (beat value)")
    b.field(group, "Meter/Grid period", "Grid period", suffix="ms")
    b.field(group, "Meter/Grid offset", "Grid offset", suffix="ms")
    b.list(b.group("Stingers", "Stingers"), "Stingers/Stinger count", "Stingers")


def _transitions(b) -> None:
    b.list(b.group("Transitions", "Transitions"), "Transition rules/Rule count", "Rules", children=True, summary="rule")


def _segment(b, name) -> None:
    _meter(b)
    b.field(b.group("General Settings", "Segment"), "Duration (ms)", "Duration")
    b.list(b.group("Cues", "Cues"), "Cues/Cue count", "Cues", cues=True)
    _node(b, name)


def _track(b, name) -> None:
    group = b.group("General Settings", "Music Track")
    b.field(group, "Track type", "Track type")
    b.field(group, "Look-ahead time (ms)", "Look-ahead time")
    switch = b.group("General Settings", "Switch Track")
    b.field(switch, "Switch group type", "Group type", radio=True)
    b.field(switch, "Switch group", "Group")
    b.field(switch, "Default switch", "Default switch / state")
    b.list(switch, "Switch association count", "Sub-track switches")
    fade = b.group("General Settings", "Track Transition")
    b.fields_under(fade, "Track transition")
    clips = b.group("Clips", "Clips")
    b.field(clips, "Clips/Subtrack count", "Sub-track count")
    b.list(clips, "Clips/Clip count", "Clips")
    b.list(b.group("Clips", "Clip Automation"), "Clip automation/Automation count", "Automation", summary=("Clip index", "Automation type"))
    b.list(b.group("Sources", "Sources"), "Source count", "Sources", summary=("Source ID", "Codec / source plug-in"))
    _node(b, name)


def _music_switch(b, name) -> None:
    group = b.group("General Settings", "Switch")
    b.field(group, "Continue playback", "Continue to play on switch change")
    b.field(group, "Decision mode", "Mode")
    _decision(b, "Association")
    _meter(b)
    _transitions(b)
    _node(b, name)


def _decision(b, tab) -> None:
    b.add(b.group(tab, "Association"), "decision", "Decision tree", claims=[
        path for path in b.fields if path.startswith(("Decision tree/", "Decision arguments/"))
        or path in {"Decision depth", "Decision-tree bytes"}
    ])


def _music_playlist(b, name) -> None:
    b.add(b.group("Playlist", "Playlist"), "playlist", "Music playlist", claims=[
        path for path in b.fields if path.startswith("Music playlist") or path == "Playlist node count"
    ])
    _meter(b)
    _transitions(b)
    _node(b, name)


def _bus(b, name) -> None:
    section = ""
    _voice(b, section, "Bus Volume (dB)", ("Volume (dB)",), "Pitch (cents)", "LPF (%)", "HPF (%)", "Make-up Gain (dB)")
    _output(b, "", section, bus=True)
    _sends(b, "", section)
    group = b.group("General Settings", "Bus")
    b.add(group, "channels", "Bus playback/Channel count", "Channel configuration", claims=(
        "Bus playback/Channel count", "Bus playback/Configuration type", "Bus playback/Channel mask",
    ))
    b.field(group, "Bus playback/Background music", "Mute for background music")
    ducking = b.group("General Settings", "Auto-ducking")
    b.field(ducking, "Recovery time (ms)", "Recovery time")
    b.field(ducking, "Maximum duck volume", "Maximum ducking volume", suffix="dB")
    b.list(ducking, "Ducking/Duck count", "Ducked busses")
    _effects(b, "", node=False)
    _positioning(b, "", section)
    _rtpc(b, "")
    _states(b, "")
    hdr = b.group("HDR", "HDR")
    b.field(hdr, "Bus playback/HDR bus", "Enable HDR")
    b.field(hdr, "Bus playback/Exponential HDR release", "Release mode", choices={0: "Linear", 1: "Exponential"})
    for prop, label in (
        ("HDR Bus Threshold", "Threshold"), ("HDR Bus Ratio", "Ratio"), ("HDR Bus Release Time", "Release time"),
        ("HDR Bus Game Parameter", "Window top output game parameter"), ("HDR Game Parameter Minimum", "Output minimum"),
        ("HDR Game Parameter Maximum", "Output maximum"), ("HDR Active Range", "Active range"),
    ):
        b.prop(hdr, section, prop, label=label)
    _advanced(b, "", section, bus=True)


def _action(b, name) -> None:
    b.other_tab, b.addable = "General", set()
    group = b.group("General", "Action")
    b.add(group, "action", "Action type", "Type", claims=("Action type",))
    target = b.fields.get("Target")
    if "State" in b.fields or "Switch" in b.fields:
        b.claimed.add("Target")
    else:
        b.field(group, "Target", "Target")
    if target is not None and target.id_kind == "hirc":
        b.field(group, "Target is a bus", "Target is a bus")
    b.prop(group, "", "Delay Time (ms)", label="Delay")
    b.prop(group, "", "Transition Time (ms)", label="Fade time")
    b.prop(group, "", "Probability (%)", label="Probability", present=int(b.value("Action type")) >> 8 not in {0x04, 0x05})
    settings = b.group("General", "Settings")
    for path in (
        "Fade curve", "Required bank", "Required bank type", "Include pending resume", "Master resume",
        "Apply to state transitions", "Apply to dynamic sequences", "Bypass transition", "Value meaning", "Value",
        "Random minimum", "Random maximum", "State group", "State", "Switch group", "Switch", "Bypass",
        "Target effect mask", "Target is an audio-device element", "Effect slot", "Effect", "Shared effect",
        "Relative to duration", "Seek value", "Snap to nearest marker",
    ):
        b.field(settings, path)
    b.list(b.group("General", "Exceptions"), "Exceptions/Exception count", "Exceptions")


def _attenuation(b, name) -> None:
    cone = b.group("Attenuation", "Cone Attenuation")
    b.field(cone, "Directional cone enabled", "Cone attenuation")
    for label, suffix in (("Inside angle", "°"), ("Outside angle", "°"), ("Outside volume", "dB"), ("Outside LPF", ""), ("Outside HPF", "")):
        b.field(cone, f"Directional cone/{label}", label, suffix=suffix)
    b.field(b.group("Attenuation", "Spread"), "Height spread enabled", "Height spread")
    b.add(b.group("Attenuation", "Curves"), "attenuation", "Attenuation curves", claims=[
        path for path in b.fields if path.startswith(("Curve assignments/", "Attenuation curves/"))
    ])
    _rtpc(b, "")


def _dialogue(b, name) -> None:
    group = b.group("General Settings", "Dialogue Event")
    b.field(group, "Probability (%)", "Probability")
    b.field(group, "Decision mode", "Mode")
    _decision(b, "Arguments")


def _fx(b, name) -> None:
    group = b.group("Effect Settings", "Plug-in")
    b.field(group, "Plug-in", "Plug-in", readonly=True)
    b.claimed.add("Plug-in parameter bytes")
    _plugin(b, "Effect Settings", "Plug-in parameters")
    b.list(b.group("Effect Settings", "Media"), "Plug-in media/Media count", "Media")
    _rtpc(b, "")
    _states(b, "")
    values = b.group("Effect Settings", "Property Values")
    b.list(values, "Plug-in property values/Value count", "Values")
    b.list(values, "RTPC initial values/Value count", "RTPC initial values")
    if name == "Audio Device":
        _effects(b, "", node=False)


_MODULATOR_PREFIX = {"LFO": "LFO: ", "Envelope": "Envelope: ", "Time Modulator": "Time: "}


def _modulator(b, name) -> None:
    prefix = _MODULATOR_PREFIX.get(name, "")
    group = b.group("General Settings", name)
    b.other_tab = "General Settings"
    b.addable = {prop_id for prop_id, prop in b.names.items() if prop.startswith(prefix) or prop == "Scope"}
    for prop_id, prop in sorted(b.names.items()):
        if prop.startswith(prefix):
            b.prop(group, "", prop, label=prop[len(prefix):].capitalize(), present=True)
    b.prop(group, "", "Scope", present=True)
    _rtpc(b, "")


def _state(b, name) -> None:
    b.add(b.group("State", "Values"), "keyed", "State values/Value count", "Property values", claims=[
        path for path in b.fields if path.startswith("State values/")
    ])


def _event(b, name) -> None:
    b.list(b.group("Actions", "Actions"), "Actions/Action count", "Actions")


def _feedback_node(b, name) -> None:
    b.list(b.group("Feedback", "Feedback Sources"), "Sources/Source count", "Sources")
    _node(b, name)


_BUILDERS = {
    "Sound": _sound, "Action": _action, "Event": _event, "State": _state,
    "Random/Sequence Container": _random, "Switch Container": _switch, "Actor-Mixer": _node,
    "Layer Container": _layer, "Music Segment": _segment, "Music Track": _track,
    "Music Switch": _music_switch, "Music Random/Sequence": _music_playlist,
    "Attenuation": _attenuation, "Dialogue Event": _dialogue, "Feedback Node": _feedback_node,
    "Bus": _bus, "Audio Bus": _bus, "Auxiliary Bus": _bus, "Feedback Bus": _bus,
    "FX Share Set": _fx, "FX Custom": _fx, "Audio Device": _fx,
    "LFO": _modulator, "Envelope": _modulator, "Time Modulator": _modulator,
}


def object_pages(type_name: str, layout, lists, version: int) -> list[PageTab]:
    kind = "modulator" if type_name in _MODULATOR_PREFIX else "state" if type_name == "State" else "object"
    builder = _Pages(layout, lists, version, kind)
    if type_name in _BUILDERS:
        _BUILDERS[type_name](builder, type_name)
    return builder.finish()


def _chunk_bkhd(b) -> None:
    group = b.group("Bank Header", "SoundBank")
    for path in (
        "SoundBank ID", "Language ID", "Project ID", "SoundBank type", "Media alignment", "Device allocated",
        "Feedback in bank", "Bank generator version", "SoundBank hash",
    ):
        b.field(group, path)


def _chunk_stmg(b) -> None:
    group = b.group("Global Settings", "Voices")
    for path in ("Volume threshold (dB)", "Maximum voices", "Maximum dangerous virtual voices", "Filter behavior"):
        b.field(group, path)
    for tab, count in (
        ("States", "State group count"), ("Switches", "Switch group count"),
        ("Game Parameters", "Game parameter count"), ("Acoustic Textures", "Acoustic texture count"),
    ):
        b.list(b.group(tab, tab), count, tab)


def _chunk_envs(b) -> None:
    for side in ("Obstruction", "Occlusion"):
        for curve in ("Volume", "Low-pass filter", "High-pass filter"):
            group = b.group(side, curve)
            b.field(group, f"{side}/{curve}/Enabled", "Enabled")
            b.field(group, f"{side}/{curve}/Scaling", "Scaling")
            b.list(group, f"{side}/{curve}/Point count", "Curve")


def _chunk_didx(b) -> None:
    b.add(b.group("Embedded Media", "Embedded Media"), "readonly_table", "Embedded media index", claims=[
        path for path in b.fields if path.startswith("Embedded media index/")
    ])


def _chunk_init(b) -> None:
    b.list(b.group("Plug-ins", "Registered Plug-ins"), "Plug-in count", "Plug-ins")


def _chunk_plat(b) -> None:
    b.field(b.group("Platform", "Platform"), "Platform")


def _chunk_stid(b) -> None:
    group = b.group("Bank Names", "SoundBank File Names")
    b.field(group, "String type")
    b.list(group, "Entry count", "SoundBanks")


_CHUNK_BUILDERS = {
    "BKHD": _chunk_bkhd, "DIDX": _chunk_didx, "INIT": _chunk_init, "STMG": _chunk_stmg,
    "ENVS": _chunk_envs, "PLAT": _chunk_plat, "STID": _chunk_stid,
}


def chunk_pages(chunk_id: str, layout, lists, version: int) -> list[PageTab]:
    builder = _Pages(layout, lists, version)
    if chunk_id in _CHUNK_BUILDERS:
        _CHUNK_BUILDERS[chunk_id](builder)
    return builder.finish()


def other_settings(tabs) -> list[str]:
    return [
        control.key for tab in tabs for group in tab.groups if group.title == "Other Settings"
        for control in group.controls
    ]
