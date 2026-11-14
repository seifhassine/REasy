"""See docs/internals/gui-editor.md, Document and edits."""

from __future__ import annotations

import copy
import math
import re
import uuid

from PySide6.QtCore import QCoreApplication

lazy from file_handlers.clip.model import walk_nodes
lazy from file_handlers.clip.rules import PropertyType
from file_handlers.document_commands import ReplaceContents, SetFields
lazy from file_handlers.native_math import f32

from .document import GuiEditDocument
from .errors import GuiEditError, GuiFormatError, GuiWriteError
from .model import (
    NINE_SLICE_CELL_NAMES,
    ZERO_GUID,
    GuiBinding,
    GuiNineSlice,
    GuiNineSliceCell,
    GuiObject,
    GuiProperty,
    GuiSymbol,
    GuiTextureEntry,
    GuiTextureSet,
)
from .rules import gui_layout
from .scene import normalize_gui_resource_path
from .serializer import encode_special, encode_value
from .states import object_node

translate = QCoreApplication.translate


def checked(document: GuiEditDocument, kind: PropertyType, value, name: str):
    layout = gui_layout(document.gui.version)
    storage = layout.values.get(kind, "")
    if isinstance(value, (list, tuple)):
        value = [f32(float(item)) if layout.payloads.get(storage, "").endswith("f") else item for item in value]
        if any(type(item) is float and not math.isfinite(item) for item in value):
            raise GuiEditError(translate("GuiEditDocument", "{0}: a component is not a finite number").format(name))
    elif kind == PropertyType.F32 and type(value) in (int, float):
        value = f32(float(value))
    try:
        encode_value(layout, kind, value, name)
    except GuiWriteError as error:
        raise GuiEditError(str(error)) from error
    return value


def check_name(document: GuiEditDocument, name: str) -> None:
    if not name or "/" in name or "\\" in name or "\0" in name:
        raise GuiEditError(translate("GuiEditDocument", "A name cannot be empty or hold a slash."))


def _ranked(document: GuiEditDocument, obj: GuiObject, held: list[GuiProperty],
            added: list[GuiProperty]) -> list[GuiProperty]:
    rank = {field.name: place for place, field in enumerate(document.authoring.fields(obj.type_name))}
    result = list(held)
    for prop in added:
        mine = rank.get(prop.name, len(rank))
        at = next((index for index, held in enumerate(result) if rank.get(held.name, len(rank)) > mine), len(result))
        result.insert(at, prop)
    return result


def _settle_stored(document: GuiEditDocument, obj: GuiObject, text: str) -> None:
    authoring = document.authoring
    stored = {field.name: field for field in authoring.stored(obj.type_name, obj.values())}
    ruled = {field.name for field in authoring.fields(obj.type_name) if field.when is not None}
    kept = [prop for prop in obj.properties if prop.name not in ruled or prop.name in stored]
    held = {prop.name for prop in kept}
    added = [GuiProperty(name, field.type, copy.deepcopy(field.default)) for name, field in stored.items()
             if name in ruled and name not in held]
    if added or len(kept) != len(obj.properties):
        document.push(ReplaceContents(document, obj, "properties", _ranked(document, obj, kept, added), text))


def set_value(document: GuiEditDocument, obj: GuiObject, name: str, value) -> None:
    authoring, prop = document.authoring, obj.property(name)
    field = authoring.field(obj.type_name, name)
    if prop is None and field is None:
        raise GuiEditError(translate("GuiEditDocument", "{0} has no property {1}.").format(obj.type_name, name))
    value = checked(document, prop.type if prop is not None else field.type, value, name)
    if prop is not None and prop.value == value:
        return
    text = translate("GuiEditDocument", "Set {0}").format(name)
    rules = any(other.when is not None and other.when[0] == name for other in authoring.fields(obj.type_name))
    if prop is not None and not rules:
        document.push(SetFields(document, [(prop, {"value": value})], text))
        return
    with document.macro(text):
        if prop is None:
            document.push(ReplaceContents(document, obj, "properties", _ranked(
                document, obj, obj.properties, [GuiProperty(name, field.type, value)]), text))
        else:
            document.push(SetFields(document, [(prop, {"value": value})], text))
        if rules:
            _settle_stored(document, obj, text)


def set_override(document: GuiEditDocument, path: str, type_name: str, name: str, kind: PropertyType, value) -> None:
    gui, value = document.gui, checked(document, kind, value, name)
    text = translate("GuiEditDocument", "Override {0}").format(name)
    held = next((item for item in gui.bindings if item.target_path == path and item.property.name == name), None)
    if held is not None:
        if held.property.value != value:
            document.push(SetFields(document, [(held.property, {"value": value})], text))
        return
    bindings = list(gui.bindings)
    at = max((index + 1 for index, item in enumerate(bindings) if item.target_path == path), default=len(bindings))
    bindings.insert(at, GuiBinding(path, type_name, GuiProperty(name, kind, value)))
    document.push(ReplaceContents(document, gui, "bindings", bindings, text))


def remove_overrides(document: GuiEditDocument, bindings: list[GuiBinding]) -> None:
    gui, doomed = document.gui, {id(item) for item in bindings}
    document.push(ReplaceContents(document, gui, "bindings", [item for item in gui.bindings if id(item) not in doomed],
                                  translate("GuiEditDocument", "Remove Override")))


def _single(document: GuiEditDocument, values) -> tuple:
    found = tuple(f32(float(item)) for item in values)
    if not all(map(math.isfinite, found)):
        raise GuiEditError(translate("GuiEditDocument", "A number of the content is not finite."))
    return found


def set_payload(document: GuiEditDocument, obj: GuiObject, payload) -> None:
    if isinstance(payload, GuiTextureSet):
        payload = GuiTextureSet([GuiTextureEntry(item.sequence, item.pattern, _single(document, item.bounds),
                                                 item.tagged) for item in payload.entries])
    elif isinstance(payload, GuiNineSlice):
        payload = GuiNineSlice(_single(document, payload.borders), [
            GuiNineSliceCell(item.name, item.sequence, item.pattern, _single(document, item.repeat_size))
            for item in payload.cells])
    try:
        encode_special(gui_layout(document.gui.version), obj.type_name, payload)
    except (GuiWriteError, GuiFormatError) as error:
        raise GuiEditError(translate("GuiEditDocument", "{0}: {1}").format(obj.name, error)) from error
    if payload != obj.special_data:
        document.push(SetFields(document, [(obj, {"special_data": payload})],
                                translate("GuiEditDocument", "Edit {0}").format(obj.name)))


def new_payload(document: GuiEditDocument, type_name: str):
    kind = gui_layout(document.gui.version).special.get(type_name)
    if kind is None:
        return None
    spec = document.authoring.payloads[type_name]
    if kind == "texture_set":
        return GuiTextureSet([])
    return GuiNineSlice(tuple(spec["borders"]), [
        GuiNineSliceCell(name, sequence, pattern, (width, height))
        for name, (sequence, pattern, width, height) in zip(NINE_SLICE_CELL_NAMES, spec["cells"])])


def _used_guids(document: GuiEditDocument) -> set[uuid.UUID]:
    gui = document.gui
    return {ZERO_GUID, *(item.instance_guid for item in gui.objects), *(item.guid for item in gui.symbols),
            *(item.guid for item in gui.animations)}


def new_guid(used: set[uuid.UUID]) -> uuid.UUID:
    while (guid := uuid.uuid4()) in used:
        pass
    used.add(guid)
    return guid


def _add_nodes(document: GuiEditDocument, symbol: GuiSymbol, objects: list[GuiObject], text: str) -> None:
    for animation in symbol.animations:
        clip, root = document.clip(animation), animation.clip.roots[0]
        held = list(root.children)
        for obj in objects:
            before = {other.name for other in symbol.objects[:symbol.objects.index(obj)]}
            at = max((index + 1 for index, node in enumerate(held) if node.name in before), default=0)
            held.insert(at, object_node(obj))
        clip.push(ReplaceContents(clip, root, "children", held, text))


def _drop_nodes(document: GuiEditDocument, symbol: GuiSymbol, names: set[str], text: str) -> None:
    for animation in symbol.animations:
        clip, root = document.clip(animation), animation.clip.roots[0]
        if any(node.name in names for node in root.children):
            clip.push(ReplaceContents(clip, root, "children", [node for node in root.children
                                                                if node.name not in names], text))


def _insert(document: GuiEditDocument, symbol: GuiSymbol, objects: list[GuiObject], index: int | None,
            text: str) -> None:
    gui, held = document.gui, list(symbol.objects)
    at = len(held) if index is None else max(0, min(index, len(held)))
    held[at:at] = objects
    document.push(ReplaceContents(document, symbol, "objects", held, text))
    document.push(ReplaceContents(document, gui, "objects", [
        *gui.objects, *(obj for obj in objects if obj not in gui.objects)], text))
    _add_nodes(document, symbol, objects, text)


def new_object(document: GuiEditDocument, symbol: GuiSymbol, type_name: str,
               prototype: uuid.UUID = ZERO_GUID) -> GuiObject:
    authoring = document.authoring
    name = authoring.free_name("object", {obj.name for obj in symbol.objects},
                               **{"class": type_name.rsplit(".", 1)[-1]})
    return GuiObject(new_guid(_used_guids(document)), prototype, name, type_name,
                     authoring.new_properties(type_name), new_payload(document, type_name))


def add_object(document: GuiEditDocument, symbol: GuiSymbol, type_name: str, index: int | None = None,
               prototype: GuiSymbol | None = None) -> GuiObject:
    authoring = document.authoring
    if type_name not in authoring.palette:
        raise GuiEditError(translate("GuiEditDocument", "{0} is not a class this game's GUI files hold.")
                           .format(type_name))
    if prototype is not None and (not authoring.is_control(type_name) or prototype.type_name != type_name):
        raise GuiEditError(translate("GuiEditDocument", "A {0} cannot hold symbol {1}.")
                           .format(type_name.rsplit(".", 1)[-1], prototype.name))
    local = prototype is not None and prototype in document.gui.symbols
    if local and document.structure.contains(prototype, symbol):
        raise GuiEditError(translate("GuiEditDocument", "{0} holds {1}: it cannot be placed inside it.")
                           .format(prototype.name, symbol.name))
    obj = new_object(document, symbol, type_name, prototype.guid if prototype is not None else ZERO_GUID)
    with document.macro(translate("GuiEditDocument", "Add {0}").format(obj.name)):
        _insert(document, symbol, [obj], index, translate("GuiEditDocument", "Add {0}").format(obj.name))
    return obj


def add_symbol(document: GuiEditDocument, type_name: str, name: str | None = None) -> GuiSymbol:
    gui, authoring = document.gui, document.authoring
    if not authoring.is_control(type_name):
        raise GuiEditError(translate("GuiEditDocument", "A {0} holds no symbol.").format(type_name.rsplit(".", 1)[-1]))
    symbol = GuiSymbol(new_guid(_used_guids(document)),
                       name or authoring.free_name("symbol", {item.name for item in gui.symbols}), type_name)
    check_name(document, symbol.name)
    document.push(ReplaceContents(document, gui, "symbols", [*gui.symbols, symbol],
                                  translate("GuiEditDocument", "Add {0}").format(symbol.name)))
    return symbol


def add_control(document: GuiEditDocument, symbol: GuiSymbol, type_name: str,
                index: int | None = None) -> GuiObject:
    with document.macro(translate("GuiEditDocument", "Add {0}").format(type_name.rsplit(".", 1)[-1])):
        return add_object(document, symbol, type_name, index, add_symbol(document, type_name))


def remove_objects(document: GuiEditDocument, objects: list[GuiObject]) -> None:
    gui, structure = document.gui, document.structure
    if any(obj is gui.root_object for obj in objects):
        raise GuiEditError(translate("GuiEditDocument", "The root object cannot be removed."))
    doomed = {id(obj) for obj in objects}
    lost = {id(binding) for obj in objects for binding, _path in structure.through(obj)}
    text = translate("GuiEditDocument", "Remove {0}").format(objects[0].name) if len(objects) == 1 else \
        translate("GuiEditDocument", "Remove {0} Objects").format(len(objects))
    with document.macro(text):
        if lost:
            document.push(ReplaceContents(document, gui, "bindings",
                                          [item for item in gui.bindings if id(item) not in lost], text))
        for symbol in gui.symbols:
            names = {obj.name for obj in symbol.objects if id(obj) in doomed}
            if names:
                _drop_nodes(document, symbol, names, text)
                document.push(ReplaceContents(document, symbol, "objects",
                                              [obj for obj in symbol.objects if id(obj) not in doomed], text))
        document.push(ReplaceContents(document, gui, "objects",
                                      [obj for obj in gui.objects if id(obj) not in doomed], text))


def rename_object(document: GuiEditDocument, obj: GuiObject, name: str) -> None:
    check_name(document, name)
    if name == obj.name:
        return
    structure, old = document.structure, obj.name
    owner = structure.owner.get(id(obj))
    if owner is not None and any(other.name == name for other in owner.objects if other is not obj):
        raise GuiEditError(translate("GuiEditDocument", "{0} already holds an object named {1}.")
                           .format(owner.name, name))
    moved = [(binding, f"{path.rsplit('/', 1)[0]}/{name}{binding.target_path[len(path):]}")
             for binding, path in structure.through(obj)] if obj is not document.gui.root_object else []
    text = translate("GuiEditDocument", "Rename {0}").format(old)
    with document.macro(text):
        document.push(SetFields(document, [(obj, {"name": name}),
                                           *((binding, {"target_path": path}) for binding, path in moved)], text))
        for animation in owner.animations if owner is not None else ():
            clip = document.clip(animation)
            renamed = [(node, {"name": name}) for node in animation.clip.roots[0].children if node.name == old]
            if renamed:
                clip.push(SetFields(clip, renamed, text))


def move_objects(document: GuiEditDocument, objects: list[GuiObject], symbol: GuiSymbol, index: int | None) -> None:
    gui, structure = document.gui, document.structure
    moving = {id(obj) for obj in objects}
    if any(obj is gui.root_object or id(obj) not in structure.owner for obj in objects):
        raise GuiEditError(translate("GuiEditDocument", "The root object cannot be moved."))
    for obj in objects:
        inner = structure.symbol_of(obj)
        if inner is not None and structure.contains(inner, symbol):
            raise GuiEditError(translate("GuiEditDocument", "{0} cannot be moved into itself.").format(obj.name))
    names = [obj.name for obj in symbol.objects if id(obj) not in moving] + [obj.name for obj in objects]
    if len(set(names)) != len(names):
        raise GuiEditError(translate("GuiEditDocument", "{0} already holds an object of that name.")
                           .format(symbol.name))
    text = translate("GuiEditDocument", "Move {0}").format(objects[0].name) if len(objects) == 1 else \
        translate("GuiEditDocument", "Move {0} Objects").format(len(objects))
    held = list(symbol.objects)
    at = len(held) if index is None else max(0, min(index, len(held)))
    at -= sum(1 for obj in held[:at] if id(obj) in moving)
    guests = [obj for obj in objects if structure.owner[id(obj)] is not symbol]
    old = {id(obj): structure.through(obj) for obj in guests}
    was = {id(obj): structure.paths(obj) for obj in guests}
    with document.macro(text):
        for source in gui.symbols:
            leaving = [obj for obj in guests if structure.owner[id(obj)] is source]
            if leaving:
                _drop_nodes(document, source, {obj.name for obj in leaving}, text)
                document.push(ReplaceContents(document, source, "objects",
                                              [obj for obj in source.objects if id(obj) not in moving], text))
        stay = [obj for obj in held if id(obj) not in moving]
        stay[at:at] = objects
        document.push(ReplaceContents(document, symbol, "objects", stay, text))
        _add_nodes(document, symbol, guests, text)
        for animation in symbol.animations if len(guests) < len(objects) else ():
            clip, root = document.clip(animation), animation.clip.roots[0]
            place = {obj.name: at for at, obj in enumerate(stay)}
            ordered = sorted(root.children, key=lambda node: place.get(node.name, len(place)))
            if ordered != list(root.children):
                clip.push(ReplaceContents(clip, root, "children", ordered, text))
        gone, edits = set(), []
        for obj in guests:
            now = document.structure.paths(obj)
            for binding, path in old[id(obj)]:
                if len(was[id(obj)]) == 1 and len(now) == 1:
                    edits.append((binding, {"target_path": now[0] + binding.target_path[len(path):]}))
                else:
                    gone.add(id(binding))
        if edits:
            document.push(SetFields(document, edits, text))
        if gone:
            document.push(ReplaceContents(document, gui, "bindings",
                                          [item for item in gui.bindings if id(item) not in gone], text))


def set_symbol(document: GuiEditDocument, obj: GuiObject, symbol: GuiSymbol | None, twin: bool = False) -> None:
    gui, structure = document.gui, document.structure
    if not document.authoring.is_control(obj.type_name):
        raise GuiEditError(translate("GuiEditDocument", "A {0} holds no symbol.")
                           .format(obj.type_name.rsplit(".", 1)[-1]))
    if obj is gui.root_object:
        raise GuiEditError(translate("GuiEditDocument", "The root object always holds the file's first symbol."))
    owner = structure.owner.get(id(obj))
    if symbol is not None and symbol in gui.symbols and owner is not None and structure.contains(symbol, owner):
        raise GuiEditError(translate("GuiEditDocument", "{0} holds {1}: it cannot be placed inside it.")
                           .format(symbol.name, owner.name))
    below = set() if twin else {id(binding) for binding, path in structure.through(obj)
                                if binding.target_path != path}
    text = translate("GuiEditDocument", "Set Symbol of {0}").format(obj.name)
    with document.macro(text):
        if below:
            document.push(ReplaceContents(document, gui, "bindings",
                                          [item for item in gui.bindings if id(item) not in below], text))
        document.push(SetFields(document, [(obj, {"prototype_guid": symbol.guid if symbol is not None else ZERO_GUID})],
                                text))


def remove_symbol(document: GuiEditDocument, symbol: GuiSymbol) -> None:
    gui, uses = document.gui, document.structure.uses.get(symbol.guid, ())
    if symbol is gui.symbols[0]:
        raise GuiEditError(translate("GuiEditDocument", "The root symbol cannot be removed."))
    if uses:
        raise GuiEditError(translate("GuiEditDocument", "{0} is placed {1} times: remove those objects first.")
                           .format(symbol.name, len(uses)))
    text = translate("GuiEditDocument", "Remove {0}").format(symbol.name)
    doomed = {id(item) for item in (*symbol.objects, *symbol.animations)}
    with document.macro(text):
        document.push(ReplaceContents(document, gui, "symbols", [item for item in gui.symbols if item is not symbol],
                                      text))
        document.push(ReplaceContents(document, gui, "objects",
                                      [item for item in gui.objects if id(item) not in doomed], text))
        document.push(ReplaceContents(document, gui, "animations",
                                      [item for item in gui.animations if id(item) not in doomed], text))


def rename_symbol(document: GuiEditDocument, symbol: GuiSymbol, name: str) -> None:
    check_name(document, name)
    if name == symbol.name:
        return
    old, text = symbol.name, translate("GuiEditDocument", "Rename {0}").format(symbol.name)
    with document.macro(text):
        document.push(SetFields(document, [(symbol, {"name": name})], text))
        for animation in symbol.animations:
            clip, root = document.clip(animation), animation.clip.roots[0]
            if root.name == old:
                clip.push(SetFields(clip, [(root, {"name": name})], text))


def renew(symbol: GuiSymbol, used: set[uuid.UUID], every: bool = True) -> None:
    renewed = {}
    for obj in symbol.objects:
        if every or obj.instance_guid in used:
            old, obj.instance_guid = obj.instance_guid.bytes_le, new_guid(used)
            renewed[old] = obj.instance_guid.bytes_le
        used.add(obj.instance_guid)
    for animation in symbol.animations:
        if every or animation.guid in used:
            old, animation.guid = animation.guid.bytes_le, new_guid(used)
            renewed[old] = animation.guid.bytes_le
        used.add(animation.guid)
    for animation in symbol.animations:
        clip = animation.clip
        clip.header_guid = renewed.get(clip.header_guid, clip.header_guid)
        for node in walk_nodes(clip.roots):
            node.guid, node.node_id = renewed.get(node.guid, node.guid), renewed.get(node.node_id, node.node_id)
        clip.roots[0].name = symbol.name


def duplicate_symbol(document: GuiEditDocument, symbol: GuiSymbol, name: str | None = None) -> GuiSymbol:
    gui, used = document.gui, _used_guids(document)
    clone = copy.deepcopy(symbol)
    clone.guid = new_guid(used)
    clone.name = name or document.authoring.free_name("copy", {item.name for item in gui.symbols},
                                                       stem=re.sub(r"\d+$", "", symbol.name))
    renew(clone, used)
    text = translate("GuiEditDocument", "Add {0}").format(clone.name)
    with document.macro(text):
        document.push(ReplaceContents(document, gui, "symbols", [*gui.symbols, clone], text))
        document.push(ReplaceContents(document, gui, "objects", [*gui.objects, *clone.objects], text))
        document.push(ReplaceContents(document, gui, "animations", [*gui.animations, *clone.animations], text))
    return clone


def make_unique(document: GuiEditDocument, obj: GuiObject, symbol: GuiSymbol) -> GuiSymbol:
    with document.macro(translate("GuiEditDocument", "Make {0} Unique").format(obj.name)):
        clone = duplicate_symbol(document, symbol)
        set_symbol(document, obj, clone, True)
    return clone


def add_import(document: GuiEditDocument, path: str) -> None:
    gui, key = document.gui, path.casefold()
    if not key.endswith(".gui") or normalize_gui_resource_path(path) != key:
        raise GuiEditError(translate("GuiEditDocument", "{0} is not a GUI file's path as the game writes it: "
                                                        "UI/GUI/.../name.gui.")
                           .format(path))
    if any(normalize_gui_resource_path(item) == key for item in gui.imported_gui_paths):
        raise GuiEditError(translate("GuiEditDocument", "{0} is already imported.").format(path))
    document.push(ReplaceContents(document, gui, "imported_gui_paths", [*gui.imported_gui_paths, path],
                                  translate("GuiEditDocument", "Import {0}").format(path.rsplit("/", 1)[-1])))


def _brings(workspace, paths: list[str], guid: uuid.UUID) -> bool:
    return any((key := normalize_gui_resource_path(path)) in workspace.resources
               and workspace.resolve_symbol(key, guid) is not None for path in paths)


def remove_import(document: GuiEditDocument, path: str) -> None:
    gui, workspace = document.gui, document.workspace
    kept = [item for item in gui.imported_gui_paths if item != path]
    used = [obj.name for obj in gui.objects if workspace is not None and gui.symbol(obj.prototype_guid) is None
            and _brings(workspace, [path], obj.prototype_guid) and not _brings(workspace, kept, obj.prototype_guid)]
    if used:
        raise GuiEditError(translate("GuiEditDocument", "{0} places a symbol of {1}: remove it or give it another "
                                                        "symbol first.").format(used[0], path.rsplit("/", 1)[-1]))
    document.push(ReplaceContents(document, gui, "imported_gui_paths", kept,
                                  translate("GuiEditDocument", "Remove Import {0}").format(path.rsplit("/", 1)[-1])))
