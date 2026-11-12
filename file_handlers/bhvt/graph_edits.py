"""See docs/internals/bhvt-editor.md, Document and edits."""

from __future__ import annotations

import bisect
import copy

from PySide6.QtGui import QUndoCommand

lazy from file_handlers.uvar.expression_nodes import expression_parameter_names
lazy from utils.hex_util import guid_le_to_str

from file_handlers.document_commands import ReplaceContents, SetFields

from .document import BhvtDocument
from .edits import (_action_layout, _new_id, _push, _removal, _transitions, _tree_objects, _unlinked, add_node,
                    add_object, add_reference, add_state, adopted, appended_row, default_entry, detached,
                    first_selector, group_of, map_removal, motion_entry, motion_fsm, object_block, remove_node,
                    remove_reference, remove_state, section_blank, set_fields, set_reference, set_selector,
                    transition_ids, zero)
from .errors import BhvtFormatError
from .model import Hfsm, Record, Tree
from .object_edits import built
from .references import field_key
from .rules import attribute_bit, motion_fsm_rules, motion_fsm_v1_rules
from .sections import signed
from .structure import Structure, Transition


def _used_ids(document: BhvtDocument, structure: Structure) -> set[int]:
    model = structure.model
    if structure.hfsm:
        entry = motion_entry(document, model)
        return {structure.transition_id(item) for node in structure.nodes for item in structure.transitions(node)} | \
            {record.uid for record in model.conditions} | {item.id for item in (entry.transitions if entry else ())}
    fsm = motion_fsm(document, model)
    return transition_ids(model, model.nodes, {}) | {
        item.transition_id for item in (fsm.transition_maps if fsm else ())}


def add_child(document: BhvtDocument, model: Tree | Hfsm, parent, name: str | None = None):
    return (add_state if isinstance(model, Hfsm) else add_node)(document, model, parent, name)


def add_transition(document: BhvtDocument, model: Tree | Hfsm, container, source, target,
                   condition: str | None = None) -> Transition:
    structure, text = Structure(model), document.tr("Add transition")
    kind = source if source in ("start", "any") else "link" if structure.hfsm else "state"
    spec, new_id = structure.kinds[kind], _new_id(_used_ids(document, structure))
    if structure.hfsm:
        holder = structure.holder(kind, container)
        codec = structure.rules.selector if spec.get("in") == "selector" else structure.rules.node
        row = Record({**copy.deepcopy(codec.blanks[spec["rows"]]), "id": new_id, spec["target"]: target.id,
                      **({spec["source"]: source.id} if kind == "link" else {})})
        transition = Transition(kind, container, len(getattr(holder, spec["rows"])))
        commands = [ReplaceContents(document, holder, spec["rows"], [*getattr(holder, spec["rows"]), row], text)]
    else:
        owner = source if kind == "state" else container
        if not structure.is_fsm(owner) or not structure.holds(owner, spec["target"]):
            raise BhvtFormatError(f"{owner.name} holds no {kind} transitions")
        values = {spec["target"]: target.id, structure.graph["states"][spec["target"]]: target.ex_id,
                  spec["condition"]: -1, **({spec["id"]: new_id} if "id" in spec else {})}
        transition = Transition(kind, owner, len(structure.rows(kind, owner)))
        commands = [SetFields(document, [(owner, appended_row(structure.rules.node, vars(owner), spec["target"],
                                                              values))], text)]
        if "id" in spec and (fsm := motion_fsm(document, model)) is not None:
            maps = list(fsm.transition_maps)
            maps.insert(bisect.bisect([item.transition_id for item in maps], new_id),
                        Record({"transition_id": new_id, "data_index": fsm.header.start_transition_data_index}))
            commands.append(ReplaceContents(document, fsm, "transition_maps", maps, text))
    with document.macro(text):
        for command in commands:
            document.push(command)
        if condition is not None:
            set_condition(document, model, transition, condition)
    return transition


def remove_transitions(document: BhvtDocument, model: Tree | Hfsm, transitions: list[Transition]) -> None:
    structure, text, gone = Structure(model), document.tr("Remove transition"), {}
    for transition in transitions:
        gone.setdefault((transition.kind, transition.owner), set()).add(transition.index)
    _push(document, (_hfsm_removal if structure.hfsm else _tree_removal)(document, structure, gone, {}, text), text)


def _tree_removal(document: BhvtDocument, structure: Structure, gone: dict, changes: dict,
                  text: str) -> list[QUndoCommand]:
    tree, objects = structure.model, set()
    for (kind, owner), indexes in gone.items():
        for index in indexes:
            objects.update(structure.events(Transition(kind, owner, index)))
            objects.add(structure.condition(Transition(kind, owner, index)))
        record = {**vars(owner), **changes.get(owner, {})}
        changes.setdefault(owner, {}).update({
            name: [value for at, value in enumerate(record[name]) if at not in indexes]
            for name in group_of(structure.rules.node, record, structure.kinds[kind]["target"])})
    ids = transition_ids(tree, tree.nodes, {}) - transition_ids(tree, tree.nodes, changes)
    return [*_removal(document, tree, objects - {None} - _tree_objects(tree, tree.nodes, changes), tree.nodes,
                      changes, text), *map_removal(document, tree, ids, text)]


def _hfsm_removal(document: BhvtDocument, structure: Structure, gone: dict, changes: dict,
                  text: str) -> list[QUndoCommand]:
    for (kind, owner), indexes in gone.items():
        holder, rows = structure.holder(kind, owner), structure.kinds[kind]["rows"]
        changes.setdefault(holder, {})[rows] = [row for at, row in enumerate(getattr(holder, rows))
                                                if at not in indexes]
    return [SetFields(document, list(changes.items()), text),
            *_unlinked(document, structure.model, structure.nodes, changes, text)]


def remove_items(document: BhvtDocument, model: Tree | Hfsm, nodes: list, transitions: list[Transition]) -> None:
    structure, chosen = Structure(model), {id(node) for node in nodes}
    roots = [node for node in structure.preorder() if id(node) in chosen and node is not structure.root
             and not any(id(item) in chosen for item in structure.path(node)[:-1])]
    with document.macro(document.tr("Delete")):
        if transitions:
            remove_transitions(document, model, transitions)
        for node in roots:
            if structure.hfsm:
                remove_state(document, model, structure.parent[id(node)], node)
            else:
                remove_node(document, model, node)


def add_callback_object(document: BhvtDocument, tree: Tree, node) -> tuple[str, int]:
    graph, names = Structure(tree).graph["callback"], expression_parameter_names()
    params = {names.get(parameter.name_hash): parameter.value for parameter in node.parameters}
    info = tree.blocks[graph["blocks"][0]].type_registry.get_type_info(params[graph["type_parameter"]] & 0xFFFFFFFF)
    if info is None:
        raise BhvtFormatError("the called condition class is not in the RSZ dump")
    block = object_block(tree, graph["blocks"][0], info["name"])
    created = built(tree.blocks[block], info["name"])
    field = created[-1][1][field_key(created[-1][1], graph["guid_field"])]
    field.raw_bytes = params[graph["guid_parameter"]].bytes
    field.guid_str = guid_le_to_str(field.raw_bytes)
    return block, add_object(document, tree, block, info["name"], created=created)


def set_target(document: BhvtDocument, model: Tree | Hfsm, transition: Transition, target) -> None:
    structure, text = Structure(model), document.tr("Retarget transition")
    spec = structure.kinds[transition.kind]
    if structure.hfsm:
        set_fields(document, structure.rows(transition.kind, transition.owner)[transition.index], text,
                   **{spec["target"]: target.id})
        return
    values = {name: list(getattr(transition.owner, name)) for name in (spec["target"],
                                                                         structure.graph["states"][spec["target"]])}
    for name, value in zip(values, (target.id, target.ex_id)):
        values[name][transition.index] = value
    set_fields(document, transition.owner, text, **values)


def set_source(document: BhvtDocument, model: Tree | Hfsm, transition: Transition, source) -> Transition:
    structure, text = Structure(model), document.tr("Reconnect transition")
    if structure.hfsm:
        set_fields(document, structure.rows("link", transition.owner)[transition.index], text,
                   **{structure.kinds["link"]["source"]: source.id})
        return transition
    if source is transition.owner:
        return transition
    if not structure.is_fsm(source):
        raise BhvtFormatError(f"{source.name} is not an FSM node")
    old, target = vars(transition.owner), structure.kinds["state"]["target"]
    group = group_of(structure.rules.node, old, target)
    moved = {name: old[name][transition.index] for name in group}
    document.push(SetFields(document, [
        (transition.owner, {name: [value for at, value in enumerate(old[name]) if at != transition.index]
                            for name in group}),
        (source, appended_row(structure.rules.node, vars(source), target, moved))], text))
    return Transition("state", source, len(vars(source)[target]) - 1)


def move_transition(document: BhvtDocument, model: Tree | Hfsm, transition: Transition, to: int) -> Transition:
    structure, text = Structure(model), document.tr("Reorder transition")
    if structure.hfsm:
        holder, field = structure.holder(transition.kind, transition.owner), structure.kinds[transition.kind]["rows"]
        rows = list(getattr(holder, field))
        places = [at for at, row in enumerate(rows) if transition.kind != "link" or vars(row)["from_id"] ==
                  vars(rows[transition.index])["from_id"]]
        rows.insert(places[to], rows.pop(transition.index))
        set_fields(document, holder, text, **{field: rows})
        return Transition(transition.kind, transition.owner, places[to])
    record = vars(transition.owner)
    values = {}
    for name in group_of(structure.rules.node, record, structure.kinds[transition.kind]["target"]):
        items = list(record[name])
        items.insert(to, items.pop(transition.index))
        values[name] = items
    set_fields(document, transition.owner, text, **values)
    return Transition(transition.kind, transition.owner, to)


def set_transition_value(document: BhvtDocument, model: Tree | Hfsm, transition: Transition, role: str,
                         value) -> None:
    structure, text = Structure(model), document.tr("Edit transition")
    if structure.hfsm:
        set_fields(document, structure.rows(transition.kind, transition.owner)[transition.index], text,
                   **{role: value})
        return
    name = structure.kinds[transition.kind][role]
    values = list(getattr(transition.owner, name))
    values[transition.index] = value
    set_fields(document, transition.owner, text, **{name: values})


def set_condition(document: BhvtDocument, model: Tree | Hfsm, transition: Transition, type_name: str | None,
                  created=None) -> None:
    structure, text = Structure(model), document.tr("Set condition")
    if not structure.hfsm:
        set_reference(document, model, transition.owner, structure.kinds[transition.kind]["condition"],
                      transition.index, type_name, created, text)
        return
    old, record = structure.condition(transition), structure.condition_record(transition)
    with document.macro(text):
        if type_name is None:
            if record is not None:
                document.push(ReplaceContents(document, model, "conditions",
                                              [item for item in model.conditions if item is not record], text))
        else:
            block, base = structure.graph["objects"]["instance"]
            instance = add_object(document, model, block, type_name, created=created) + base
            if record is None:
                document.push(ReplaceContents(document, model, "conditions", [*model.conditions, Record({
                    **section_blank(model, "conditions"), **structure.rules.new_condition,
                    "uid": structure.transition_id(transition), "instance": instance, "name": type_name})], text))
            else:
                set_fields(document, record, text, instance=instance, name=type_name)
        if old is not None:
            _push(document, _removal(document, model, {old}, [*model.conditions, *_transitions(structure.nodes, {})],
                                     {}, text), text)


def add_event(document: BhvtDocument, model: Tree | Hfsm, transition: Transition, type_name: str,
              created=None) -> None:
    structure, text = Structure(model), document.tr("Add event")
    if not structure.hfsm:
        field = structure.kinds[transition.kind].get("events")
        if not structure.holds(transition.owner, field):
            raise BhvtFormatError(f"{transition.kind} transitions of tree version {model.version} run no events")
        add_reference(document, model, transition.owner, field, type_name, transition.index, created, text)
        return
    block, base = structure.graph["objects"]["transition_events"]
    row = structure.rows(transition.kind, transition.owner)[transition.index]
    with document.macro(text):
        place = add_object(document, model, block, type_name, created=created)
        set_fields(document, row, text, transition_events=[*row.transition_events, place + base])


def remove_event(document: BhvtDocument, model: Tree | Hfsm, transition: Transition, position: int) -> None:
    structure, text = Structure(model), document.tr("Remove event")
    if not structure.hfsm:
        remove_reference(document, model, transition.owner, structure.kinds[transition.kind]["events"], position,
                         transition.index, text)
        return
    row = structure.rows(transition.kind, transition.owner)[transition.index]
    changes = {row: {"transition_events": row.transition_events[:position] + row.transition_events[position + 1:]}}
    _push(document, _removal(document, model, {structure.events(transition)[position]} - {None},
                             [*model.conditions, *_transitions(structure.nodes, {})], changes, text), text)


def move_event(document: BhvtDocument, model: Tree | Hfsm, transition: Transition, index: int, to: int) -> None:
    structure = Structure(model)
    role = "transition_events" if structure.hfsm else "events"
    values = list(structure.value(transition, role))
    values.insert(to, values.pop(index))
    set_transition_value(document, model, transition, role, values)


def set_entry(document: BhvtDocument, model: Tree | Hfsm, container, child) -> None:
    structure, text = Structure(model), document.tr("Set entry state")
    if structure.hfsm:
        set_fields(document, container, text, default_start_child_id=child.id)
        return
    spec = structure.kinds["start"]
    rows = structure.rows("start", container)
    if rows and vars(container)[spec["condition"]][-1] == -1:
        set_target(document, model, Transition("start", container, len(rows) - 1), child)
    else:
        add_transition(document, model, container, "start", child)


def blending(document: BhvtDocument, model: Tree | Hfsm, transition_id: int | None) -> tuple[Record | None, int]:
    if isinstance(model, Tree):
        if (fsm := motion_fsm(document, model)) is None:
            return None, 0
        found, start = _map(fsm, transition_id), fsm.header.start_transition_data_index
        index = start if found is None else found.data_index
        if not 0 <= index < len(fsm.transition_data):
            return None, 0
        users = sum(item.data_index == index for item in fsm.transition_maps)
        return fsm.transition_data[index], users + (index == start) + (found is None)
    entry = motion_entry(document, model)
    return next((item for item in (entry.transitions if entry else ()) if item.id == transition_id), None), 1


def _map(fsm, transition_id: int | None) -> Record | None:
    maps = fsm.transition_maps
    at = bisect.bisect_left([item.transition_id for item in maps], transition_id) if transition_id else len(maps)
    return maps[at] if at < len(maps) and maps[at].transition_id == transition_id else None


def set_blending(document: BhvtDocument, model: Tree | Hfsm, transition_id: int, **values) -> None:
    text = document.tr("Edit blending")
    if isinstance(model, Hfsm):
        entry = motion_entry(document, model)
        record = next((item for item in entry.transitions if item.id == transition_id), None)
        if record is not None:
            set_fields(document, record, text, **values)
            return
        rows = list(entry.transitions)
        rows.insert(bisect.bisect([signed(item.id) for item in rows], signed(transition_id)), Record({
            **zero(motion_fsm_v1_rules(document.resource.version).transition_data), "id": transition_id, **values}))
        document.push(ReplaceContents(document, entry, "transitions", rows, text))
        return
    fsm = motion_fsm(document, model)
    record, shared = blending(document, model, transition_id)
    if shared == 1:
        set_fields(document, record, text, **values)
        return
    maps = list(fsm.transition_maps)
    at = bisect.bisect_left([item.transition_id for item in maps], transition_id)
    preset = Record({**(zero(motion_fsm_rules(document.resource.version).transition_data) if record is None
                        else copy.deepcopy(record.fields())), "id": transition_id, **values})
    with document.macro(text):
        document.push(ReplaceContents(document, fsm, "transition_data", [*fsm.transition_data, preset], text))
        if at < len(maps) and maps[at].transition_id == transition_id:
            set_fields(document, maps[at], text, data_index=len(fsm.transition_data) - 1)
        else:
            maps.insert(at, Record({"transition_id": transition_id, "data_index": len(fsm.transition_data) - 1}))
            document.push(ReplaceContents(document, fsm, "transition_maps", maps, text))


def accepts_child(structure: Structure, parent, fsm: bool) -> bool:
    if structure.hfsm:
        return True
    reference = parent.attribute & attribute_bit(structure.model.version, "HasReferenceTree")
    return not reference and (fsm or not structure.is_fsm(parent))


def move_node(document: BhvtDocument, model: Tree | Hfsm, node, parent, index: int) -> None:
    structure = Structure(model)
    old = structure.parent.get(id(node))
    if old is None or any(item is node for item in structure.path(parent)) or \
            not accepts_child(structure, parent, structure.is_fsm(node)):
        raise BhvtFormatError(f"{node.name} cannot move under {parent.name}")
    (_move_state if structure.hfsm else _move_tree_node)(document, structure, node, old, parent, index)


def _move_tree_node(document: BhvtDocument, structure: Structure, node, old, parent, index: int) -> None:
    tree, rules, text = structure.model, structure.rules, document.tr("Move node")
    group = group_of(rules.node, vars(old), "children")
    at = next(at for at, key in enumerate(zip(old.children, old.children_ex)) if key == (node.id, node.ex_id))
    row = tuple(vars(old)[name][at] for name in group)
    changes = {old: {name: [value for place, value in enumerate(vars(old)[name]) if place != at] for name in group}}
    selector, gone, start = first_selector(tree, parent), {}, structure.kinds["start"]["target"]
    changes[parent] = {**changes.get(parent, {}), **adopted(tree, parent, [row], index, changes.get(parent))}
    if old is not parent:
        for kind in ("start", "any"):
            gone[kind, old] = {item.index for item in structure.transitions(old)
                               if item.kind == kind and structure.target(item) is node}
        record = {**vars(old), **changes[old]}
        starts = record[start] if structure.holds(old, start) else ()
        if starts and len(gone["start", old]) == len(starts) and record["children"]:
            changes[old].update(default_entry(rules, record))
    with document.macro(text):
        _push(document, _tree_removal(document, structure, {key: value for key, value in gone.items() if value},
                                      changes, text), text)
        if selector is not None:
            set_selector(document, tree, parent, selector)


def _move_state(document: BhvtDocument, structure: Structure, state, old, parent, index: int) -> None:
    hfsm, rules, text = structure.model, structure.rules, document.tr("Move state")
    if old is parent:
        at = next(at for at, child in enumerate(old.children) if child is state)
        children, attributes = list(old.children), list(old.selector.state_attributes)
        children.insert(index, children.pop(at))
        attributes.insert(index, attributes.pop(at))
        changes = {old: {"children": children}, old.selector: {"state_attributes": attributes}}
        _push(document, [SetFields(document, list(changes.items()), text),
                         *_action_layout(document, hfsm, changes, text)], text)
        return
    moved, changes = detached(old, state)
    selector = parent.selector or Record({**copy.deepcopy(rules.new_selector), "state_attributes": []})
    changes[selector] = {**changes.get(selector, {}), "state_attributes": [
        *selector.state_attributes[:index], 0, *selector.state_attributes[index:]]}
    changes[parent] = {**changes.get(parent, {}), "children": [*parent.children[:index], state,
                                                               *parent.children[index:]],
                       "selector": selector,
                       "default_start_child_id": parent.default_start_child_id if parent.children else state.id}
    kept = moved | {item.id for item in structure.path(parent)}
    for item in structure.nodes:
        rows = [row for row in item.start_transitions if row.to_node_id not in moved or item.id in kept]
        if len(rows) < len(item.start_transitions):
            changes.setdefault(item, {})["start_transitions"] = rows
    _push(document, [SetFields(document, list(changes.items()), text),
                     *_unlinked(document, hfsm, structure.nodes, changes, text),
                     *_action_layout(document, hfsm, changes, text)], text)
