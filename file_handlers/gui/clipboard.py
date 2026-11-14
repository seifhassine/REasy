"""See docs/internals/gui-editor.md, Copy and paste."""

from __future__ import annotations

import copy
import re
import uuid

from PySide6.QtCore import QCoreApplication

lazy from file_handlers.clip.model import ResourceKind, Timeline
from file_handlers.document_commands import ReplaceContents

from .codec import parse_gui
from .document import GuiEditDocument
from .edits import _insert, _used_guids, new_guid, renew
from .errors import GuiEditError, GuiFormatError
from .model import GuiAnimation, GuiBinding, GuiDocument, GuiObject, GuiSymbol
from .rules import gui_layout
from .serializer import serialize_gui
from .state_edits import node_of
from .states import UNSET, clip_root, shape
from .storage import nodes
from .structure import join

translate = QCoreApplication.translate

_held: bytes | None = None


def hold(document: GuiEditDocument, objects: list[GuiObject]) -> None:
    global _held
    _held = copied(document, objects)


def held() -> bytes | None:
    return _held


def copied(document: GuiEditDocument, objects: list[GuiObject]) -> bytes:
    gui, structure = document.gui, document.structure
    if any(obj is gui.root_object for obj in objects):
        raise GuiEditError(translate("GuiEditDocument", "The root object cannot be copied."))
    reached: dict[uuid.UUID, GuiSymbol] = {}
    queue = [inner for obj in objects if (inner := structure.symbol_of(obj)) is not None]
    while queue:
        symbol = queue.pop(0)
        if symbol.guid not in reached:
            reached[symbol.guid] = symbol
            queue += [inner for obj in symbol.objects if (inner := structure.symbol_of(obj)) is not None]
    objects = [obj for obj in dict.fromkeys(objects) if structure.owner[id(obj)].guid not in reached]
    held, symbols, stand = copy.deepcopy((objects, list(reached.values()), gui.root_object))
    root = GuiSymbol(uuid.uuid4(), gui.symbols[0].name, gui.symbols[0].type_name, held)
    stand.prototype_guid = root.guid
    names = [obj.name for obj in objects]
    bindings = [GuiBinding(join("/", obj.name) + binding.target_path[len(prefix):], binding.target_type,
                           copy.deepcopy(binding.property))
                for obj in objects if names.count(obj.name) == 1 and len(structure.paths(obj)) == 1
                for binding, prefix in structure.through(obj)]
    tracks: dict[str, dict[str, list]] = {}
    for obj in objects:
        for animation in structure.owner[id(obj)].animations if names.count(obj.name) == 1 else ():
            node = node_of(document, animation, obj)
            if node is not None and node.properties:
                tracks.setdefault(animation.name, {})[obj.name] = copy.deepcopy(node.properties)
    root.animations = [_state(gui.version, root, name, found) for name, found in tracks.items()]
    return serialize_gui(GuiDocument(
        gui.version, stand, [root, *symbols], bindings, [], "clipboard",
        [stand, *held, *(obj for symbol in symbols for obj in symbol.objects)],
        [animation for symbol in (root, *symbols) for animation in symbol.animations]))


def _state(version: int, root: GuiSymbol, name: str, tracks: dict[str, list]) -> GuiAnimation:
    guid = uuid.uuid4()
    held = clip_root(root, guid.bytes_le)
    for node in held.children:
        node.properties = tracks.get(node.name, [])
    animation = GuiAnimation(guid, name, False, Timeline(gui_layout(version).clip_version, ResourceKind.MOT_CLIP, UNSET,
                                                          guid.bytes_le, [], [held]))
    for target, values in shape(animation):
        for field, value in values.items():
            setattr(target, field, value)
    return animation


def _reaches(start: GuiObject, target: uuid.UUID, symbols: dict[uuid.UUID, GuiSymbol]) -> bool:
    seen, queue = set(), [start.prototype_guid]
    while queue:
        guid = queue.pop()
        if guid == target:
            return True
        if guid not in seen and guid in symbols:
            seen.add(guid)
            queue += [obj.prototype_guid for obj in symbols[guid].objects]
    return False


def paste(document: GuiEditDocument, data: bytes, symbol: GuiSymbol, index: int | None = None) -> list[GuiObject]:
    gui, structure, authoring = document.gui, document.structure, document.authoring
    try:
        fragment = parse_gui(data, "clipboard")
    except GuiFormatError as error:
        raise GuiEditError(translate("GuiEditDocument", "The clipboard holds no GUI objects: {0}")
                           .format(error)) from error
    if fragment.version != gui.version:
        raise GuiEditError(translate("GuiEditDocument", "The copied objects are of GUI version {0}; this file is {1}.")
                           .format(fragment.version, gui.version))
    top = fragment.symbols[0].objects
    added = [item for item in fragment.symbols[1:] if item.guid not in structure.symbols]
    known = {**{item.guid: item for item in added}, **structure.symbols}
    for obj in top:
        if obj.type_name not in authoring.palette:
            raise GuiEditError(translate("GuiEditDocument", "{0} is not a class this game's GUI files hold.")
                               .format(obj.type_name))
        if _reaches(obj, symbol.guid, known):
            raise GuiEditError(translate("GuiEditDocument", "{0} holds {1}: it cannot be pasted inside it.")
                               .format(obj.name, symbol.name))
    used, taken, first = _used_guids(document), {obj.name for obj in symbol.objects}, {}
    for item in added:
        renew(item, used, False)
    for obj in top:
        first[obj.name] = obj
        obj.instance_guid = new_guid(used)
        if obj.name in taken:
            obj.name = authoring.free_name("copy", taken, stem=re.sub(r"\d+$", "", obj.name))
        taken.add(obj.name)
    text = translate("GuiEditDocument", "Paste {0}").format(top[0].name) if len(top) == 1 else \
        translate("GuiEditDocument", "Paste {0} Objects").format(len(top))
    with document.macro(text):
        if added:
            document.push(ReplaceContents(document, gui, "symbols", [*gui.symbols, *added], text))
            document.push(ReplaceContents(document, gui, "objects", [
                *gui.objects, *(obj for item in added for obj in item.objects)], text))
            document.push(ReplaceContents(document, gui, "animations", [
                *gui.animations, *(animation for item in added for animation in item.animations)], text))
        _insert(document, symbol, top, index, text)
        for carried in fragment.symbols[0].animations:
            state = next((item for item in symbol.animations if item.name == carried.name), None)
            for node in nodes(carried) if state is not None else ():
                if node.properties:
                    clip = document.clip(state)
                    clip.push(ReplaceContents(clip, next(item for item in nodes(state)
                                                         if item.name == first[node.name].name),
                                              "properties", node.properties, text))
        held = {(item.target_path, item.property.name) for item in gui.bindings}
        bindings = []
        for binding in fragment.bindings:
            name, _slash, rest = binding.target_path[1:].partition("/")
            paths = document.structure.paths(first[name])
            path = paths[0] + ("/" + rest if rest else "") if len(paths) == 1 else None
            if path is not None and (path, binding.property.name) not in held:
                bindings.append(GuiBinding(path, binding.target_type, binding.property))
        if bindings:
            document.push(ReplaceContents(document, gui, "bindings", [*gui.bindings, *bindings], text))
    return top
