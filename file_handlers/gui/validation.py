"""See docs/internals/gui-editor.md, Validation."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from PySide6.QtCore import QCoreApplication

lazy from file_handlers.clip.rules import PropertyType

from .errors import GuiWriteError
from .model import ZERO_GUID, GuiDocument, GuiObject, GuiSymbol
from .rules import gui_layout
from .serializer import encode_value
from .storage import nodes, places
from .structure import Structure

ERROR, WARNING, NOTE = "error", "warning", "note"


@dataclass(frozen=True, slots=True)
class Problem:
    severity: str
    text: str
    target: object = None


translate = QCoreApplication.translate


def problems(document) -> list[Problem]:
    gui, structure, found, workspace = document.gui, document.structure, [], document.workspace
    if workspace is not None:
        workspace.invalidate()
    _shape(gui, structure, found)
    for symbol in gui.symbols:
        _symbol(document, symbol, structure, found)
    for obj in gui.objects:
        _object(document, obj, structure, workspace, found)
    _overrides(document, structure, workspace, found)
    if workspace is not None:
        _imports(gui, workspace, found)
    return sorted(found, key=lambda item: (ERROR, WARNING, NOTE).index(item.severity))


def errors(document) -> list[Problem]:
    return [item for item in problems(document) if item.severity == ERROR]


def _shape(gui: GuiDocument, structure: Structure, found: list[Problem]) -> None:
    root = gui.root_object
    if root is None or not gui.symbols or root.prototype_guid != gui.symbols[0].guid:
        found.append(Problem(ERROR, translate("GuiValidation", "The root object must name the file's first symbol: the "
                                                               "game builds the GUI from that symbol."), root))
    for label, guids in ((translate("GuiValidation", "objects"), [item.instance_guid for item in gui.objects]),
                         (translate("GuiValidation", "symbols"), [item.guid for item in gui.symbols]),
                         (translate("GuiValidation", "states"), [item.guid for item in gui.animations])):
        if len(set(guids)) != len(guids):
            found.append(Problem(ERROR, translate("GuiValidation", "Two {0} share a GUID.").format(label)))
    for name, count in Counter(symbol.name for symbol in gui.symbols).items():
        if count > 1:
            found.append(Problem(WARNING,
                                 translate("GuiValidation", "{0} symbols are named \"{1}\": a script that creates one "
                                                            "by name gets the first.").format(count, name),
                                 next(symbol for symbol in gui.symbols if symbol.name == name)))
    for obj in gui.objects:
        if obj is not root and id(obj) not in structure.owner:
            found.append(Problem(WARNING,
                                 translate("GuiValidation", "\"{0}\" belongs to no symbol: the game never creates it.")
                                 .format(obj.name), obj))
    for symbol in gui.symbols[1:]:
        if symbol.guid not in structure.uses:
            found.append(Problem(NOTE, translate("GuiValidation", "Symbol \"{0}\" is not placed in this file; another "
                                                                  "file or a script can still create it.")
                                 .format(symbol.name), symbol))


def _symbol(document, symbol: GuiSymbol, structure: Structure, found: list[Problem]) -> None:
    if not symbol.name:
        found.append(Problem(ERROR, translate("GuiValidation", "A symbol has no name."), symbol))
    for name, count in Counter(obj.name for obj in symbol.objects).items():
        if count > 1:
            found.append(Problem(ERROR,
                                 translate("GuiValidation", "\"{0}\" holds {1} objects named \"{2}\": the game finds "
                                                            "only the first by name.").format(symbol.name, count, name),
                                 next(obj for obj in symbol.objects if obj.name == name)))
    if any(structure.contains(inner, symbol) for inner in map(structure.symbol_of, symbol.objects)
           if inner is not None):
        found.append(Problem(ERROR, translate("GuiValidation", "Symbol \"{0}\" contains itself.").format(symbol.name),
                             symbol))
    held = {obj.name: obj for obj in reversed(symbol.objects)}
    for animation in symbol.animations:
        if not animation.name:
            found.append(Problem(ERROR, translate("GuiValidation", "A state of \"{0}\" has no name.")
                                 .format(symbol.name), animation))
        if animation.transition is not None and animation.transition not in symbol.animations:
            found.append(Problem(ERROR, translate("GuiValidation", "State \"{0}\" of \"{1}\" continues in a state of "
                                                                   "another symbol.")
                                 .format(animation.name, symbol.name), animation))
        for issue in document.clip(animation).issues():
            found.append(Problem(ERROR if issue.error else WARNING,
                                 translate("GuiValidation", "State \"{0}\" of \"{1}\": {2}")
                                 .format(animation.name, symbol.name, issue.message), animation))
        for node in nodes(animation):
            target = held.get(node.name)
            if target is None:
                if node.properties:
                    found.append(Problem(WARNING, translate("GuiValidation", "State \"{0}\" of \"{1}\" animates "
                                                                             "\"{2}\", which the symbol does not hold.")
                                         .format(animation.name, symbol.name, node.name), animation))
                continue
            for track in node.properties:
                _track(document, symbol, animation, target, track, found)


def _track(document, symbol, animation, target: GuiObject, track, found: list[Problem]) -> None:
    where = translate("GuiValidation", "State \"{0}\" of \"{1}\", {2}.{3}").format(animation.name, symbol.name,
                                                                                   target.name, track.name)
    if any(member.name not in places(track.type) for member in track.children):
        found.append(Problem(ERROR, translate("GuiValidation", "{0}: a member the type does not have.").format(where),
                             track))
        return
    held, field = target.property(track.name), document.authoring.field(target.type_name, track.name)
    if field is None:
        found.append(Problem(WARNING, translate("GuiValidation", "{0}: the class has no such property.").format(where),
                             track))
    elif field.type != track.type:
        found.append(Problem(ERROR, translate("GuiValidation", "{0}: the track is {1}, the property {2}.")
                             .format(where, PropertyType(track.type).name, field.type.name), track))
    elif held is None:
        found.append(Problem(WARNING,
                             translate("GuiValidation", "{0}: the object stores no value, so the property is not reset "
                                                        "when a state starts.").format(where), track))
    for item in (track, *track.children):
        if item.keys and item.keys[0].frame:
            found.append(Problem(WARNING, translate("GuiValidation", "{0}: the first key is not at frame 0.")
                                 .format(where), track))
            break


def _object(document, obj: GuiObject, structure: Structure, workspace, found: list[Problem]) -> None:
    authoring, layout = document.authoring, gui_layout(document.gui.version)
    if not obj.name or "/" in obj.name:
        found.append(Problem(ERROR, translate("GuiValidation", "\"{0}\": a name cannot be empty or hold \"/\".")
                             .format(obj.name), obj))
    control = authoring.is_control(obj.type_name)
    if obj.prototype_guid != ZERO_GUID and not control:
        found.append(Problem(ERROR, translate("GuiValidation", "\"{0}\" names a symbol, but a {1} holds none.")
                             .format(obj.name, obj.type_name.rsplit(".", 1)[-1]), obj))
    symbol = structure.symbol_of(obj)
    if symbol is not None and symbol.type_name != obj.type_name:
        found.append(Problem(WARNING, translate("GuiValidation", "\"{0}\" is a {1}, its symbol \"{2}\" a {3}.").format(
            obj.name, obj.type_name.rsplit(".", 1)[-1], symbol.name, symbol.type_name.rsplit(".", 1)[-1]), obj))
    if control and obj.prototype_guid != ZERO_GUID and symbol is None and workspace is not None \
            and workspace.resolve_symbol(workspace.root_key, obj.prototype_guid) is None:
        missing = bool(workspace.missing_dependencies)
        found.append(Problem(WARNING if missing else ERROR, (
            translate("GuiValidation", "\"{0}\" names a symbol that cannot be checked: an imported file was not found "
                                       "or cannot be read.")
            if missing else
            translate("GuiValidation", "\"{0}\" names a symbol neither this file nor its imports hold: the game does "
                                       "not create the GUI.")
        ).format(obj.name), obj))
    for prop in obj.properties:
        try:
            encode_value(layout, prop.type, prop.value, prop.name)
        except GuiWriteError as error:
            found.append(Problem(ERROR, f"\"{obj.name}\": {error}", obj))
            continue
        field = authoring.field(obj.type_name, prop.name)
        if field is None:
            continue
        if field.type != prop.type:
            found.append(Problem(WARNING,
                                 translate("GuiValidation", "\"{0}\".{1} is stored as {2}; the game's files store {3}.")
                                 .format(obj.name, prop.name, prop.type.name, field.type.name), obj))
        elif field.type == PropertyType.ENUM and not field.unread and prop.value not in authoring.labels(field):
            found.append(Problem(WARNING, translate("GuiValidation", "\"{0}\".{1}: \"{2}\" is not a value of {3}; the "
                                                                     "game reads it as 0.")
                                 .format(obj.name, prop.name, prop.value, (field.enum or "").rsplit(".", 1)[-1]), obj))


def _target(workspace, path: str) -> GuiObject | None:
    resource = workspace.resources[workspace.root_key]
    obj = resource.document.root_object
    for name in filter(None, path.split("/")):
        held = workspace.resolve_symbol(resource.key, obj.prototype_guid)
        obj = next((item for item in held.symbol.objects if item.name == name), None) if held is not None else None
        if obj is None:
            return None
        resource = held.resource
    return obj


def _overrides(document, structure: Structure, workspace, found: list[Problem]) -> None:
    seen = Counter((item.target_path, item.property.name) for item in document.gui.bindings)
    for binding in document.gui.bindings:
        where = f"{binding.target_path}.{binding.property.name}"
        if seen[binding.target_path, binding.property.name] > 1:
            found.append(Problem(WARNING, translate("GuiValidation", "{0} is overridden twice.").format(where),
                                 binding))
        target = structure.target(binding)
        if target is None and workspace is not None:
            target = _target(workspace, binding.target_path)
        type_name = target.type_name if target is not None else None
        if type_name is None:
            chain = structure.chain(binding.target_path)
            known = workspace is not None and not workspace.missing_dependencies
            if known or len(chain) == 1 or structure.symbol_of(chain[-1]) is not None:
                found.append(Problem(WARNING, translate("GuiValidation", "The override of {0} names no object: the "
                                                                         "game skips it.").format(where), binding))
            continue
        if type_name != binding.target_type:
            found.append(Problem(ERROR, translate(
                "GuiValidation", "The override of {0} is written for a {1}; the object is a {2}.").format(
                where, binding.target_type.rsplit(".", 1)[-1], type_name.rsplit(".", 1)[-1]), binding))
        elif (field := document.authoring.field(type_name, binding.property.name)) is not None \
                and field.type != binding.property.type:
            found.append(Problem(ERROR, translate("GuiValidation", "The override of {0} is {1}; the property is {2}.")
                                 .format(where, binding.property.type.name, field.type.name), binding))


def _imports(gui: GuiDocument, workspace, found: list[Problem]) -> None:
    for path, count in Counter(gui.imported_gui_paths).items():
        if count > 1:
            found.append(Problem(WARNING, translate("GuiValidation", "{0} is imported {1} times.").format(path, count),
                                 path))
    for key, (path, owner, error) in workspace.missing_dependencies.items():
        text = translate("GuiValidation", "An imported file cannot be read: {0} (imported by {1}): {2}") if error else \
            translate("GuiValidation", "An imported file was not found: {0} (imported by {1}).")
        found.append(Problem(WARNING, text.format(path, owner, error), key))
