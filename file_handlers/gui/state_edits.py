"""See docs/internals/gui-editor.md, Document and edits."""

from __future__ import annotations

import copy

from PySide6.QtCore import QCoreApplication

lazy from file_handlers.clip import edits as clip_edits
lazy from file_handlers.clip.evaluation import NUMERIC_TYPES
lazy from file_handlers.clip.model import Key, Node, Property, ResourceKind, Timeline
lazy from file_handlers.clip.rules import PropertyType, property_type
from file_handlers.document_commands import ReplaceContents, SetFields

from .document import GuiEditDocument
from .edits import _used_guids, check_name, checked, new_guid, set_value
from .errors import GuiEditError
from .model import GuiAnimation, GuiObject, GuiSymbol
from .rules import gui_layout
from .states import UNSET, clip_root
from .storage import nodes

translate = QCoreApplication.translate


def add_state(document: GuiEditDocument, symbol: GuiSymbol, name: str | None = None) -> GuiAnimation:
    gui, names = document.gui, document.authoring.names
    taken = {item.name for item in symbol.animations}
    name = name or (names["state"] if names["state"] not in taken
                    else document.authoring.free_name("next_state", taken))
    check_name(document, name)
    guid = new_guid(_used_guids(document))
    clip = Timeline(gui_layout(gui.version).clip_version, ResourceKind.MOT_CLIP, UNSET, guid.bytes_le, [],
                    [clip_root(symbol, guid.bytes_le)])
    animation, text = GuiAnimation(guid, name, False, clip), translate("GuiEditDocument", "Add State {0}").format(name)
    with document.macro(text):
        document.push(ReplaceContents(document, symbol, "animations", [*symbol.animations, animation], text))
        document.push(ReplaceContents(document, gui, "animations", [*gui.animations, animation], text))
    return animation


def remove_states(document: GuiEditDocument, animations: list[GuiAnimation]) -> None:
    gui, doomed = document.gui, {id(item) for item in animations}
    text = translate("GuiEditDocument", "Remove State {0}").format(animations[0].name) if len(animations) == 1 else \
        translate("GuiEditDocument", "Remove {0} States").format(len(animations))
    cut = [(item, {"transition": None}) for item in gui.animations
           if id(item) not in doomed and item.transition is not None and id(item.transition) in doomed]
    with document.macro(text):
        if cut:
            document.push(SetFields(document, cut, text))
        for symbol in gui.symbols:
            if any(id(item) in doomed for item in symbol.animations):
                document.push(ReplaceContents(document, symbol, "animations",
                                              [item for item in symbol.animations if id(item) not in doomed], text))
        document.push(ReplaceContents(document, gui, "animations",
                                      [item for item in gui.animations if id(item) not in doomed], text))


def rename_state(document: GuiEditDocument, animation: GuiAnimation, name: str) -> None:
    check_name(document, name)
    if name != animation.name:
        document.push(SetFields(document, [(animation, {"name": name})],
                                translate("GuiEditDocument", "Rename State {0}").format(animation.name)))


def set_loop(document: GuiEditDocument, animation: GuiAnimation, loop: bool) -> None:
    if bool(loop) != animation.loop:
        document.push(SetFields(document, [(animation, {"loop": bool(loop)})],
                                translate("GuiEditDocument", "Set Loop")))


def set_transition(document: GuiEditDocument, animation: GuiAnimation, target: GuiAnimation | None) -> None:
    symbol = document.structure.state_symbol(animation)
    if target is not None and target not in symbol.animations:
        raise GuiEditError(translate("GuiEditDocument", "A state can only continue in a state of its own symbol."))
    if target is not animation.transition:
        document.push(SetFields(document, [(animation, {"transition": target})],
                                translate("GuiEditDocument", "Set Next State")))


def move_state(document: GuiEditDocument, animation: GuiAnimation, index: int) -> None:
    symbol = document.structure.state_symbol(animation)
    held = [item for item in symbol.animations if item is not animation]
    held.insert(max(0, min(index, len(held))), animation)
    if held != symbol.animations:
        document.push(ReplaceContents(document, symbol, "animations", held,
                                      translate("GuiEditDocument", "Move State {0}").format(animation.name)))


def node_of(document: GuiEditDocument, animation: GuiAnimation, obj: GuiObject) -> Node | None:
    held = document.structure.state_symbol(animation).objects
    if next((item for item in held if item.name == obj.name), None) is not obj:
        return None
    return next((node for node in nodes(animation) if node.name == obj.name), None)


def track_node(document: GuiEditDocument, animation: GuiAnimation, obj: GuiObject) -> Node:
    found = node_of(document, animation, obj)
    if found is None:
        raise GuiEditError(translate(
            "GuiEditDocument", "{0} is not an object this state animates: a state animates the objects of its symbol, "
            "and of two with one name the first.").format(obj.name))
    return found


def track(document: GuiEditDocument, animation: GuiAnimation, obj: GuiObject, name: str) -> Property | None:
    node = node_of(document, animation, obj)
    return next((item for item in node.properties if item.name == name), None) if node is not None else None


def can_animate(document: GuiEditDocument, obj: GuiObject, name: str) -> bool:
    authoring = document.authoring
    field = authoring.field(obj.type_name, name)
    return field is not None and not field.unread and field.type in authoring.track_types and (
        obj.property(name) is not None or field in authoring.stored(obj.type_name, obj.values()))


def _animated(document: GuiEditDocument, obj: GuiObject, name: str):
    if not can_animate(document, obj, name):
        raise GuiEditError(translate(
            "GuiEditDocument", "{0} cannot be animated: no file of the game animates such a property, or the object's "
            "other values rule it out.").format(name))
    return document.authoring.field(obj.type_name, name)


def add_track(document: GuiEditDocument, animation: GuiAnimation, obj: GuiObject, name: str,
              members: list[str] | None = None) -> Property:
    field = _animated(document, obj, name)
    node = track_node(document, animation, obj)
    if any(track.name == name for track in node.properties):
        raise GuiEditError(translate("GuiEditDocument", "{0} is already animated in this state.").format(name))
    clip, text = document.clip(animation), translate("GuiEditDocument", "Animate {0}").format(name)
    with document.macro(text):
        if obj.property(name) is None:
            set_value(document, obj, name, field.default)
        value = obj.property(name).value
        track = clip_edits.add_property(clip, node, name, int(field.type))
        parts = property_type(int(field.type)).members
        for place, (member, _kind) in enumerate(parts):
            if members is None or member in members:
                _first_key(clip, clip_edits.add_member(clip, track, member), value[place], text, field.keys)
        if not parts:
            _first_key(clip, track, value, text, field.keys)
    return track


def add_member(document: GuiEditDocument, animation: GuiAnimation, obj: GuiObject, track: Property,
               member: str) -> Property:
    field = _animated(document, obj, track.name)
    clip, text = document.clip(animation), translate("GuiEditDocument", "Animate {0}.{1}").format(track.name, member)
    place = [name for name, _kind in property_type(track.type).members].index(member)
    with document.macro(text):
        if obj.property(track.name) is None:
            set_value(document, obj, track.name, field.default)
        child = clip_edits.add_member(clip, track, member)
        _first_key(clip, child, obj.property(track.name).value[place], text, field.keys)
    return child


def _first_key(clip, track: Property, value, text: str, mode: int | None) -> None:
    values = {"value": value} if mode is None else {"value": value, "interpolation": mode}
    clip_edits.set_fields(clip, clip_edits.add_key(clip, track, 0.0), text, **values)


def remove_items(document: GuiEditDocument, animation: GuiAnimation, items: list) -> None:
    clip, text = document.clip(animation), translate("GuiEditDocument", "Remove Keys")
    doomed = {id(item) for item in items}
    doomed.update(id(track) for item in items if isinstance(item, Node) for track in item.properties)

    def kept(track: Property) -> bool:
        keys = [key for key in track.keys if id(key) not in doomed]
        if keys and len(keys) != len(track.keys):
            clip.push(ReplaceContents(clip, track, "keys", keys, text))
        return bool(keys) or not track.keys

    with document.macro(text):
        for node in nodes(animation):
            tracks = []
            for track in node.properties:
                if id(track) in doomed:
                    continue
                members = [member for member in track.children if id(member) not in doomed and kept(member)]
                if not (members if track.children else kept(track)):
                    continue
                if len(members) != len(track.children):
                    clip.push(ReplaceContents(clip, track, "children", members, text))
                tracks.append(track)
            if len(tracks) != len(node.properties):
                clip.push(ReplaceContents(clip, node, "properties", tracks, text))


def add_keys(document: GuiEditDocument, animation: GuiAnimation, tracks: list[Property], frame: float) -> list[Key]:
    if frame < 0.0:
        raise GuiEditError(translate("GuiEditDocument", "A key cannot lie before frame 0."))
    clip, text, found = document.clip(animation), translate("GuiEditDocument", "Add Key"), []
    with document.macro(text):
        for track in tracks:
            key = next((held for held in track.keys if held.frame == frame), None)
            if key is None:
                key = clip_edits.make_key(clip, track, frame)
                key.value = checked(document, PropertyType(track.type), key.value, track.name)
                clip.push(ReplaceContents(clip, track, "keys", sorted([*track.keys, key], key=lambda held: held.frame),
                                          text))
            found.append(key)
    return found


def set_key_frame(document: GuiEditDocument, animation: GuiAnimation, key: Key, frame: float) -> None:
    clip = document.clip(animation)
    track = clip.owner(key, Property)
    if frame < 0.0:
        raise GuiEditError(translate("GuiEditDocument", "A key cannot lie before frame 0."))
    if any(held is not key and held.frame == frame for held in track.keys):
        raise GuiEditError(translate("GuiEditDocument", "{0} already holds a key at frame {1:g}.")
                           .format(track.name, frame))
    if frame != key.frame:
        clip_edits.set_fields(clip, key, translate("GuiEditDocument", "Move Key"), frame=float(frame))


def set_key_value(document: GuiEditDocument, animation: GuiAnimation, track: Property, key: Key, value) -> None:
    value = checked(document, PropertyType(track.type), value, track.name)
    if value != key.value:
        clip_edits.set_fields(document.clip(animation), key, translate("GuiEditDocument", "Set Key"), value=value)


def key_value(document: GuiEditDocument, animation: GuiAnimation, track: Property, frame: float, shown, value,
              stored):
    names = [name for name, _kind in property_type(track.type).members]
    held = {child.name: child for child in track.children}
    with document.macro(translate("GuiEditDocument", "Key {0}").format(track.name)):
        for leaf, was, now in [(held[name], shown[at], value[at]) for at, name in enumerate(names) if name in held] \
                if names else [(track, shown, value)]:
            if now != was:
                set_key_value(document, animation, leaf, add_keys(document, animation, [leaf], frame)[0], now)
    return [stored[at] if name in held else value[at] for at, name in enumerate(names)] if names else stored


def set_key_control(document: GuiEditDocument, animation: GuiAnimation, key: Key, control: tuple) -> None:
    if control != key.control:
        clip_edits.set_fields(document.clip(animation), key, translate("GuiEditDocument", "Set Tangent"),
                              control=control)


def paste_keys(document: GuiEditDocument, animation: GuiAnimation, track: Property, keys: list[Key],
               start: float) -> list[Key]:
    clip, text = document.clip(animation), translate("GuiEditDocument", "Paste Keys")
    with document.macro(text):
        pasted = clip_edits.paste_keys(clip, track, keys, start)
        taken = {key.frame for key in pasted}
        kept = [key for key in track.keys if key.frame not in taken or any(key is item for item in pasted)]
        if len(kept) != len(track.keys):
            clip.push(ReplaceContents(clip, track, "keys", kept, text))
    return pasted


def paste_tracks(document: GuiEditDocument, animation: GuiAnimation, obj: GuiObject,
                 tracks: list[Property]) -> list[Property]:
    node, clip = track_node(document, animation, obj), document.clip(animation)
    text = translate("GuiEditDocument", "Paste Tracks")
    for track in tracks:
        if int(_animated(document, obj, track.name).type) != track.type:
            raise GuiEditError(translate("GuiEditDocument", "{0} of {1} is not the type of the copied track.")
                               .format(track.name, obj.name))
    made, held = copy.deepcopy(tracks), {track.name: track for track in node.properties}
    with document.macro(text):
        for track in made:
            if obj.property(track.name) is None:
                set_value(document, obj, track.name, document.authoring.field(obj.type_name, track.name).default)
            if track.children and track.name in held:
                kept = {child.name: child for child in (*held[track.name].children, *track.children)}
                track.children = [kept[name] for name, _kind in property_type(track.type).members if name in kept]
        fresh = {track.name: track for track in made}
        clip.push(ReplaceContents(clip, node, "properties", [
            *(fresh.pop(track.name, track) for track in node.properties), *fresh.values()], text))
    return made


def interpolations(document: GuiEditDocument, animation: GuiAnimation, keys: list[Key]) -> dict[int, str]:
    clip, offered = document.clip(animation), document.authoring.interpolations
    allowed = None
    for key in keys:
        family = "numeric" if clip.owner(key, Property).type in NUMERIC_TYPES else "other"
        mine = offered[family] | {key.interpolation}
        allowed = mine if allowed is None else allowed & mine
    return {code: name for code, name in clip_edits.interpolations_for(clip, keys).items() if code in allowed}


def set_interpolation(document: GuiEditDocument, animation: GuiAnimation, keys: list[Key], code: int) -> None:
    if code not in interpolations(document, animation, keys):
        raise GuiEditError(translate("GuiEditDocument", "No state of the game's files moves such a value that way."))
    if any(key.interpolation != code for key in keys):
        clip_edits.set_interpolation(document.clip(animation), keys, code)
