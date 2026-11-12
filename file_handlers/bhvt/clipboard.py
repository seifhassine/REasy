"""See docs/internals/bhvt-editor.md, Clipboard."""

from __future__ import annotations

import bisect
import copy
from dataclasses import dataclass, field

from file_handlers.document_commands import ReplaceContents, SetFields

from .document import BhvtDocument
from .edits import (Objects, _action_layout, _mapped, _new_id, _push, adopted, first_selector, group_of,
                    motion_entry, motion_fsm, set_selector)
from .errors import BhvtFormatError
from .graph_edits import _used_ids, accepts_child, blending
from .hfsm_codec import walk
from .model import Hfsm, Node, Record, Tree
from .object_edits import copied_instances, instantiated
from .references import field_key, field_value, object_place, object_ref, object_value
from .rules import attribute_bit, node_fields
from .sections import signed
from .structure import Structure


@dataclass(eq=False)
class Clip:
    kind: str
    game: str
    version: int
    hfsm: bool
    source: int
    items: list = field(default_factory=list)
    objects: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)


_clip: Clip | None = None


def _game(model: Tree | Hfsm) -> str:
    return next(iter(model.blocks.values())).game_version


def accepts(model: Tree | Hfsm, kind: str) -> bool:
    return _clip is not None and (_clip.kind, _clip.hfsm, _clip.version, _clip.game) == (
        kind, isinstance(model, Hfsm), model.version, _game(model))


def _begin(model: Tree | Hfsm, kind: str) -> Clip:
    global _clip
    _clip = Clip(kind, _game(model), model.version, isinstance(model, Hfsm), id(model))
    return _clip


def _hold(clip: Clip, model: Tree | Hfsm, ref: tuple[str, int] | None) -> None:
    if ref is not None and ref not in clip.objects:
        rsz = model.blocks[ref[0]]
        clip.objects[ref] = copied_instances(rsz, rsz.object_table[ref[1]])


def _taken(pool: set[int]) -> int:
    pool.add(value := _new_id(pool))
    return value


def _placer(model: Tree | Hfsm, clip: Clip, added: dict):
    places = {}

    def placed(ref: tuple[str, int]) -> int:
        if ref not in places:
            entries = added.setdefault(ref[0], [])
            places[ref] = len(model.blocks[ref[0]].object_table) + len(entries)
            entries.append(instantiated(clip.objects[ref], model.blocks[ref[0]]))
        return places[ref]

    return placed


def type_name(rsz, entry: list) -> str:
    return rsz.type_registry.get_type_info(entry[-1][0].type_id)["name"]


def copy_objects(model: Tree | Hfsm, kind: str, refs: list[tuple[str, int]]) -> None:
    clip = _begin(model, kind)
    for ref in refs:
        _hold(clip, model, ref)
    clip.items = list(refs)


def pasted_objects(model: Tree | Hfsm, kind: str) -> list[tuple[str, list]]:
    if not accepts(model, kind):
        raise BhvtFormatError("the clipboard holds nothing for this place")
    return [(type_name(model.blocks[ref[0]], _clip.objects[ref]),
             instantiated(_clip.objects[ref], model.blocks[ref[0]])) for ref in _clip.items]


def copy_nodes(document: BhvtDocument, model: Tree | Hfsm, nodes: list) -> None:
    structure, chosen = Structure(model), {id(node) for node in nodes}
    roots = [node for node in structure.preorder() if id(node) in chosen
             and not any(id(item) in chosen for item in structure.path(node)[:-1])]
    (_copy_states if structure.hfsm else _copy_tree)(document, structure, roots, _begin(model, "nodes"))


def paste_nodes(document: BhvtDocument, model: Tree | Hfsm, parent, index: int | None = None) -> list:
    if not accepts(model, "nodes"):
        raise BhvtFormatError("the clipboard holds no nodes for this file")
    return (_paste_states if isinstance(model, Hfsm) else _paste_tree)(document, model, parent, index)


def duplicate_nodes(document: BhvtDocument, model: Tree | Hfsm, nodes: list) -> list:
    global _clip
    structure, kept = Structure(model), _clip
    parent = structure.parent.get(id(nodes[-1]))
    if parent is None:
        raise BhvtFormatError("the root has no parent to hold a copy")
    last = next(at for at, child in enumerate(structure.children(parent)) if child is nodes[-1])
    copy_nodes(document, model, [node for node in nodes if structure.parent.get(id(node)) is parent])
    try:
        return paste_nodes(document, model, parent, last + 1)
    finally:
        _clip = kept


def _copy_tree(document: BhvtDocument, structure: Structure, roots: list, clip: Clip) -> None:
    tree, graph = structure.model, structure.graph
    nodes, actions, stack = [], {}, roots[::-1]
    for root in roots:
        parent = structure.parent.get(id(root))
        guard = None if parent is None else structure.child_condition(
            parent, next(at for at, child in enumerate(structure.children(parent)) if child is root))
        _hold(clip, tree, guard)
        clip.items.append(((root.id, root.ex_id), guard))
    while stack:
        node = stack.pop()
        nodes.append(copy.deepcopy(vars(node)))
        for key, ref in zip(zip(node.actions, node.actions_ex), structure.actions(node)):
            if ref is not None:
                actions[key] = ref
                _hold(clip, tree, ref)
        for name in graph["indexes"].keys() & node_fields(tree.version, node.attribute):
            _mapped(vars(node)[name], lambda value, name=name: _hold(clip, tree, object_ref(tree, name, value)))
        stack.extend(structure.children(node)[::-1])
    fsm = motion_fsm(document, tree)
    ids = [] if fsm is None else {value for fields in nodes for spec in structure.kinds.values()
                                  if spec.get("id") in node_fields(tree.version, fields["attribute"])
                                  for value in fields[spec["id"]]}
    clip.extra = {"nodes": nodes, "actions": actions,
                  "presets": {value: record.fields() for value in ids
                              if (record := blending(document, tree, value)[0]) is not None}}


def _paste_tree(document: BhvtDocument, tree: Tree, parent: Node, index: int | None) -> list[Node]:
    clip, structure, text = _clip, Structure(tree), document.tr("Paste")
    rules, graph = structure.rules, structure.graph
    fsm_bit = attribute_bit(tree.version, "IsFSMNode")
    roots = {key for key, _guard in clip.items}
    if not all(accepts_child(structure, parent, bool(fields["attribute"] & fsm_bit))
               for fields in clip.extra["nodes"] if (fields["id"], fields["ex_id"]) in roots):
        raise BhvtFormatError(f"the copied nodes cannot be children of {parent.name}")
    used, live = {node.id for node in tree.nodes}, set(structure.by_key) if clip.source == id(tree) else set()
    keys = {(fields["id"], fields["ex_id"]): (_taken(used), 0) for fields in clip.extra["nodes"]}
    id_field = graph["actions"]["id_field"]
    action_ids = {field_value(tree.blocks[block].parsed_elements[instance], id_field)
                  for block in graph["actions"]["blocks"] for instance in tree.blocks[block].object_table}
    taken, actions, ids, added = _used_ids(document, structure), {}, {}, {}
    placed = _placer(tree, clip, added)

    def index_value(name: str, value: int) -> int:
        ref = object_place(tree, name, value)
        return object_value(tree, ref[0], placed(ref)) if ref in clip.objects else value

    nodes = []
    for fields in clip.extra["nodes"]:
        record = copy.deepcopy(fields)
        record["id"], record["ex_id"] = keys[fields["id"], fields["ex_id"]]
        children = [keys[key] for key in zip(fields["children"], fields["children_ex"])]
        record["children"], record["children_ex"] = [key[0] for key in children], [key[1] for key in children]
        written = node_fields(tree.version, record["attribute"])
        for spec in structure.kinds.values():
            if spec["target"] not in written:
                continue
            ex = graph["states"][spec["target"]]
            keep = [at for at, key in enumerate(zip(record[spec["target"]], record[ex])) if key in keys or key in live]
            for name in group_of(rules.node, record, spec["target"]):
                record[name] = [record[name][at] for at in keep]
            targets = [keys.get(key, key) for key in zip(record[spec["target"]], record[ex])]
            record[spec["target"]], record[ex] = [key[0] for key in targets], [key[1] for key in targets]
            if "id" in spec:
                for old in record[spec["id"]]:
                    ids[old] = ids.get(old) or _taken(taken)
                record[spec["id"]] = [ids[old] for old in record[spec["id"]]]
        held = [key for key in zip(record["actions"], record["actions_ex"]) if key in clip.extra["actions"]]
        for key in held:
            if key not in actions:
                actions[key] = _taken(action_ids)
                ref = clip.extra["actions"][key]
                block = tree.blocks[ref[0]]
                entry = instantiated(clip.objects[ref], block)
                entry[-1][1][field_key(entry[-1][1], id_field)].value = actions[key]
                added.setdefault(ref[0], []).append(entry)
        record["actions"], record["actions_ex"] = [actions[key] for key in held], [0] * len(held)
        for name in graph["indexes"].keys() & written:
            record[name] = _mapped(record[name], lambda value, name=name: index_value(name, value))
        nodes.append(Node(record))
    rows = [(*keys[key], -1 if guard is None else object_value(tree, guard[0], placed(guard)))
            for key, guard in clip.items]
    selector = first_selector(tree, parent)
    commands = [ReplaceContents(document, tree, "nodes", [*tree.nodes, *nodes], text),
                SetFields(document, [(parent, adopted(tree, parent, rows, index))], text),
                *(Objects(document, tree, tree.blocks[block],
                          [*range(len(tree.blocks[block].object_table)), *entries], text)
                  for block, entries in added.items()),
                *_pasted_presets(document, tree, clip, ids, text)]
    with document.macro(text):
        for command in commands:
            document.push(command)
        if selector is not None:
            set_selector(document, tree, parent, selector)
    return [node for node in nodes if (node.id, node.ex_id) in {keys[key] for key in roots}]


def _pasted_presets(document: BhvtDocument, tree: Tree, clip: Clip, ids: dict[int, int], text: str) -> list:
    fsm = motion_fsm(document, tree)
    if fsm is None or not ids:
        return []
    data, maps = list(fsm.transition_data), list(fsm.transition_maps)
    known = {tuple(item.fields().items()): place for place, item in reversed(list(enumerate(data)))}
    for old, new in ids.items():
        preset, place = clip.extra["presets"].get(old), fsm.header.start_transition_data_index
        if preset is not None:
            place = known.setdefault(tuple(preset.items()), len(data))
            if place == len(data):
                data.append(Record(preset))
        maps.insert(bisect.bisect([item.transition_id for item in maps], new),
                    Record({"transition_id": new, "data_index": place}))
    return [*([ReplaceContents(document, fsm, "transition_data", data, text)]
              if len(data) > len(fsm.transition_data) else []),
            ReplaceContents(document, fsm, "transition_maps", maps, text)]


def _rows(state: Record) -> list[Record]:
    selector = state.selector
    return [*state.start_transitions, *(() if selector is None else (*selector.all_state_transitions,
                                                                       *selector.links))]


def _copy_states(document: BhvtDocument, structure: Structure, roots: list, clip: Clip) -> None:
    hfsm = structure.model
    parents = {id(structure.parent.get(id(root))): structure.parent.get(id(root)) for root in roots}
    parent, ids = next(iter(parents.values())) if len(parents) == 1 else None, {root.id for root in roots}
    links = [link for link in (parent.selector.links if parent is not None else ())
             if link.from_id in ids and link.to_id in ids]
    states = [state for root in roots for state in walk(root)]
    rows = [row for row in (*links, *(row for state in states for row in _rows(state))) if "id" in vars(row)]
    records = {record.uid: record for record in hfsm.conditions}
    entry = motion_entry(document, hfsm)
    blends = {item.id: item for item in (entry.transitions if entry is not None else ())}
    actions = {}
    for state in states:
        for action, ref in zip(state.actions, structure.actions(state)):
            if ref is not None:
                actions[action.id, action.list_no] = ref
                _hold(clip, hfsm, ref)
    for row in rows:
        if (record := records.get(row.id)) is not None:
            _hold(clip, hfsm, object_ref(hfsm, "instance", record.instance))
        for value in row.transition_events:
            _hold(clip, hfsm, object_ref(hfsm, "transition_events", value))
    clip.items = copy.deepcopy(roots)
    clip.extra = {"links": copy.deepcopy(links), "actions": actions,
                  "conditions": {row.id: copy.deepcopy(records[row.id]) for row in rows if row.id in records},
                  "records": {row.id: copy.deepcopy(blends[row.id]) for row in rows if row.id in blends}}


def _paste_states(document: BhvtDocument, hfsm: Hfsm, parent: Record, index: int | None) -> list[Record]:
    clip, structure, text = _clip, Structure(hfsm), document.tr("Paste")
    graph, names = structure.graph, structure.graph["action_fields"]
    block, base = graph["objects"]["instance"]
    events, first = graph["objects"]["transition_events"]
    used, taken = {state.id for state in structure.nodes}, _used_ids(document, structure)
    action_ids = {action.id for state in structure.nodes for action in state.actions}
    live = set(structure.by_key) if clip.source == id(hfsm) else set()
    roots, links = copy.deepcopy(clip.items), copy.deepcopy(clip.extra["links"])
    states = [state for root in roots for state in walk(root)]
    keys, actions, laid, added, records, blends = {}, {}, {}, {}, [], []
    placed = _placer(hfsm, clip, added)

    def renumber(row: Record) -> None:
        if "id" not in vars(row):
            return
        old, row.id = row.id, _taken(taken)
        row.transition_events = [placed(ref) + first if (ref := (events, value - first)) in clip.objects else value
                                 for value in row.transition_events]
        if (record := clip.extra["conditions"].get(old)) is not None:
            records.append(record := copy.deepcopy(record))
            record.uid = row.id
            if (ref := (block, record.instance - base)) in clip.objects:
                record.instance = placed(ref) + base
        if (blend := clip.extra["records"].get(old)) is not None:
            blends.append(blend := copy.deepcopy(blend))
            blend.id = row.id

    for state in states:
        keys[state.id] = _taken(used)
    for state in states:
        state.id = keys[state.id]
        state.default_start_child_id = keys.get(state.default_start_child_id, 0)
        state.actions = [action for action in state.actions if (action.id, action.list_no) in clip.extra["actions"]]
        for action in state.actions:
            key = (action.id, action.list_no)
            if key not in actions:
                actions[key] = _taken(action_ids)
                entry = instantiated(clip.objects[clip.extra["actions"][key]], hfsm.blocks["actions"])
                entry[-1][1][field_key(entry[-1][1], names["id"])].value = actions[key]
                laid[actions[key], action.list_no] = entry
            action.id = actions[key]
        state.start_transitions = [row for row in state.start_transitions
                                   if row.to_node_id in keys or row.to_node_id in live]
        for row in _rows(state):
            for name in ("to_node_id", "from_id", "to_id"):
                if name in vars(row):
                    setattr(row, name, keys.get(getattr(row, name), getattr(row, name)))
            renumber(row)
    for link in links:
        link.from_id, link.to_id = keys[link.from_id], keys[link.to_id]
        renumber(link)
    selector = parent.selector or Record({**copy.deepcopy(structure.rules.new_selector), "state_attributes": []})
    at = len(parent.children) if index is None else index
    changes = {parent: {"children": [*parent.children[:at], *roots, *parent.children[at:]], "selector": selector,
                        "default_start_child_id": parent.default_start_child_id if parent.children else roots[0].id},
               selector: {"state_attributes": [*selector.state_attributes[:at], *[0] * len(roots),
                                               *selector.state_attributes[at:]],
                          "links": [*selector.links, *links]}}
    entry = motion_entry(document, hfsm)
    commands = [SetFields(document, list(changes.items()), text),
                *([ReplaceContents(document, hfsm, "conditions", [*hfsm.conditions, *records], text)]
                  if records else []),
                *(Objects(document, hfsm, hfsm.blocks[name],
                          [*range(len(hfsm.blocks[name].object_table)), *entries], text)
                  for name, entries in added.items()),
                *_action_layout(document, hfsm, changes, text, laid),
                *([ReplaceContents(document, entry, "transitions", sorted(
                    [*entry.transitions, *blends], key=lambda item: signed(item.id)), text)]
                  if entry is not None and blends else [])]
    _push(document, commands, text)
    return roots
