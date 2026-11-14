"""See docs/internals/bhvt-editor.md, Document and edits."""

from __future__ import annotations

import bisect
import copy
import random

from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QUndoCommand

from file_handlers.document_commands import Change, ReplaceContents, SetFields

from .document import BhvtDocument
from .errors import BhvtFormatError
from .hfsm_codec import walk
from .model import Hfsm, MotionFsm, MotionFsmV1, Node, Record, Tree
from .references import (action_objects, asset, field_key, field_value, hfsm_action_objects, new_instances,
                         object_ref, object_value, references)
from .rules import attribute_bit, authoring, constructor_values, hfsm_rules, node_fields, rules_of, tree_rules
from .tree_codec import NO_ID

translate = QCoreApplication.translate

READ_ONLY = {"id", "ex_id"}
MANAGED = {"name", "name_offset", "name_hash", "full_name_hash", "reference_tree_path_offset", "parent", "parent_ex",
           "children", "children_ex", "selector_id", "state_node_count"}


def set_fields(document: BhvtDocument, target: object, text: str, **values) -> None:
    document.push(SetFields(document, [(target, values)], text))


def nested_codec(owner: object, field: str):
    return hfsm_rules(owner.version).selector if isinstance(owner, Hfsm) and field == "selector" else None


def zero(codec) -> dict:
    return codec.decode(bytes(codec.size), 0)


def section_blank(model: Tree | Hfsm, kind: str):
    if kind == "conditions":
        fields = zero(hfsm_rules(model.version).condition)
        del fields["name_offset"]
        return {**fields, "name": None}
    return {"game_objects": {"guid": bytes(16), "path": []}, "paths": ""}[kind]


def shown_fields(record: object, codec=None) -> list[str | tuple[str, ...]]:
    if codec is None:
        return [name for name in vars(record) if name not in MANAGED]
    extra = [name for name in ("reference_tree", "selector") if vars(record).get(name) is not None]
    return [entry for entry in [*codec.layout(vars(record)), *extra]
            if isinstance(entry, tuple) or entry not in MANAGED]


def _new_id(used: set[int]) -> int:
    while (value := random.getrandbits(32)) in used or value in (0, NO_ID):
        pass
    return value


def _fresh(codec, defaults: dict) -> dict:
    return {**{name: [] for name, shape in codec.fields.items() if shape == "list"}, **defaults}


def _mapped(value, function):
    return [_mapped(item, function) for item in value] if isinstance(value, list) else function(value)


def _ex_ids(model: Tree | Hfsm, block: str) -> list[int] | None:
    field = tree_rules(model.version).graph["actions"]["blocks"].get(block) if isinstance(model, Tree) else None
    return None if field is None else getattr(model, field)


def _push(document: BhvtDocument, commands: list[QUndoCommand], text: str) -> None:
    with document.macro(text):
        for command in commands:
            document.push(command)


def object_block(model: Tree | Hfsm, slot: str, type_name: str) -> str:
    static = f"static_{slot}"
    return static if static in model.blocks and \
        type_name in authoring(model.blocks[slot].game_version)["static"] else slot


def group_of(codec, record: dict, member: str) -> tuple[str, ...]:
    return next(group for group in codec.layout(record) if isinstance(group, tuple) and member in group)


def appended_row(codec, record: dict, member: str, values: dict) -> dict:
    return {name: [*record[name], values[name] if name in values else copy.deepcopy(codec.blanks[name])]
            for name in group_of(codec, record, member)}


def default_selector(tree: Tree) -> str | None:
    fsm = tree_rules(tree.version).graph["fsm_selector"]
    used = authoring(tree.blocks["selectors"].game_version)["usage"]["tree"].get("selectors", {})
    return next((name for name in used if name != fsm), None)


def transition_ids(tree: Tree, nodes: list[Node], changes: dict) -> set[int]:
    fields = [spec["id"] for spec in tree_rules(tree.version).graph["transitions"].values() if "id" in spec]
    return {value for node in nodes for field in fields if field in node_fields(tree.version, node.attribute)
            for value in changes.get(node, {}).get(field, vars(node)[field])}


def motion_fsm(document: BhvtDocument, tree: Tree) -> MotionFsm | None:
    fsm = document.resource.model
    return fsm if isinstance(fsm, MotionFsm) and fsm.tree is tree else None


def map_removal(document: BhvtDocument, tree: Tree, ids: set[int], text: str) -> list[QUndoCommand]:
    if (fsm := motion_fsm(document, tree)) is None:
        return []
    maps = [item for item in fsm.transition_maps if item.transition_id not in ids]
    if len(maps) == len(fsm.transition_maps):
        return []
    start = fsm.header.start_transition_data_index
    dropped = sorted({item.data_index for item in fsm.transition_maps} - {item.data_index for item in maps} - {start})
    commands: list[QUndoCommand] = [ReplaceContents(document, fsm, "transition_maps", maps, text)]
    if dropped:
        commands.append(ReplaceContents(document, fsm, "transition_data", [
            preset for index, preset in enumerate(fsm.transition_data) if index not in dropped], text))
        moved = [(item, {"data_index": item.data_index - bisect.bisect_left(dropped, item.data_index)})
                 for item in maps if item.data_index > dropped[0]]
        if start > dropped[0]:
            moved.append((fsm.header, {"start_transition_data_index": start - bisect.bisect_left(dropped, start)}))
        commands += [SetFields(document, moved, text)] if moved else []
    return commands


def staged_roots(block, entries: list) -> list[int]:
    roots, start = [], len(block.instance_infos)
    for entry in entries:
        start += len(entry)
        roots.append(start - 1)
    return roots


def _held(name: str) -> property:
    return property(lambda self: getattr(self.user, name), lambda self, value: setattr(self.user, name, value))


class Embedded:
    instance_infos = _held("embedded_instance_infos")
    parsed_elements = _held("embedded_instances")
    object_table = _held("embedded_object_table")
    rsz_userdata_infos = _held("embedded_userdata_infos")
    rsz_header = _held("embedded_rsz_header")

    def __init__(self, block, user):
        self.user, self.top = user, block.top if isinstance(block, Embedded) else block
        self.type_registry, self.game_version, self.has_embedded_rsz = block.type_registry, block.game_version, True
        self.instance_hierarchy: dict = {}
        self._rsz_userdata_dict = {info.instance_id: info for info in user.embedded_userdata_infos}
        self._rsz_userdata_set = set(self._rsz_userdata_dict)


class Objects(QUndoCommand):
    def __init__(self, document: BhvtDocument, model: Tree | Hfsm, block, table: list, text: str, staged=(),
                 changes=()):
        super().__init__(text)
        ex_ids = _ex_ids(model, next((name for name, held in model.blocks.items() if held is block), None))
        self.document, self.block, self.ex_ids = document, block, ex_ids
        infos, fields, added = dict(enumerate(block.instance_infos)), dict(block.parsed_elements), []
        path = rules_of(model).graph["user_path"]
        assets = {key: info.instance_id for info in block.rsz_userdata_infos if (key := asset(info, path))} \
            if block.has_embedded_rsz else {}

        def pooled(entry: list) -> int:
            places, new = [], []
            for info, values, *userdata in entry:
                key = asset(userdata[0], path) if userdata and block.has_embedded_rsz else None
                at = assets.get(key, len(infos))
                if at == len(infos):
                    infos[at], fields[at] = info, values
                    new.append(values)
                    for item in userdata:
                        item.instance_id = at
                        added.append(item)
                    if key:
                        assets[key] = at
                places.append(at)
            for values in new:
                for ref in references(values):
                    if ref.value > 0:
                        ref.value = places[ref.value - 1]
            return places[-1]

        for entry in staged:
            pooled(entry)
        roots = [block.object_table[entry] if isinstance(entry, int) else pooled(entry) for entry in table]
        self.changes = [(target, name, getattr(target, name), value) for target, name, value in changes]
        for target, name, _before, value in self.changes:
            setattr(target, name, value)
        order, seen = [0], {0}

        def visit(index: int) -> None:
            if index not in seen:
                seen.add(index)
                for ref in references(fields[index]):
                    if ref.value > 0:
                        visit(ref.value)
                order.append(index)

        for root in roots:
            visit(root)
        at = {index: new for new, index in enumerate(order)}
        users = sorted((info for info in (*block.rsz_userdata_infos, *added) if info.instance_id in at),
                       key=lambda info: at[info.instance_id])
        hierarchy = {new: {"children": [], "parent": None} for new in range(len(order))}
        refs = []
        for new, index in enumerate(order):
            for ref in references(fields[index]):
                if ref.value > 0:
                    refs.append(ref)
                    hierarchy[new]["children"].append(at[ref.value])
                    hierarchy[at[ref.value]]["parent"] = new
        self.states = ({"instance_infos": block.instance_infos, "parsed_elements": block.parsed_elements,
                        "object_table": block.object_table, "rsz_userdata_infos": block.rsz_userdata_infos,
                        "instance_hierarchy": block.instance_hierarchy},
                       {"instance_infos": [infos[index] for index in order],
                        "parsed_elements": {new: fields[index] for new, index in enumerate(order)},
                        "object_table": [at[root] for root in roots], "rsz_userdata_infos": users,
                        "instance_hierarchy": hierarchy})
        self.values = ([(ref, "value", ref.value) for ref in refs] +
                       [(info, "instance_id", info.instance_id) for info in block.rsz_userdata_infos],
                       [(ref, "value", at[ref.value]) for ref in refs] +
                       [(info, "instance_id", at[info.instance_id]) for info in users])
        self.ex = None if ex_ids is None else (list(ex_ids), [ex_ids[entry] if isinstance(entry, int) else 0
                                                              for entry in table])
        for target, name, before, _value in self.changes:
            setattr(target, name, before)

    def redo(self) -> None:
        self._apply(1)

    def undo(self) -> None:
        self._apply(0)

    def _apply(self, side: int) -> None:
        for target, name, *values in self.changes if side else ():
            setattr(target, name, values[1])
        for name, value in self.states[side].items():
            setattr(self.block, name, value.copy())
        for target, name, value in self.values[side]:
            setattr(target, name, value)
        for target, name, *values in () if side else self.changes:
            setattr(target, name, values[0])
        self.block._rsz_userdata_dict = {info.instance_id: info for info in self.block.rsz_userdata_infos}
        self.block._rsz_userdata_set = set(self.block._rsz_userdata_dict)
        if self.ex is not None:
            self.ex_ids[:] = self.ex[side]
        self.document.changed.emit(Change(self.block.top if isinstance(self.block, Embedded) else self.block,
                                          "object_table", True))


def _removal(document: BhvtDocument, model: Tree | Hfsm, objects: set[tuple[str, int]], records: list,
             changes: dict, text: str) -> list[QUndoCommand]:
    gone = {block: sorted(at for name, at in objects if name == block) for block, _at in objects}

    def shifted(block: str, index: int) -> int:
        if (block, index) in objects:
            raise BhvtFormatError(f"{block} object {index} is still referenced")
        return index - bisect.bisect_left(gone.get(block, ()), index)

    edits = []
    for record in records:
        current, values = {**vars(record), **changes.get(record, {})}, dict(changes.get(record, {}))
        for field in tree_rules(model.version).graph["indexes"].keys() & node_fields(
                model.version, current["attribute"]) if isinstance(model, Tree) else \
                hfsm_rules(model.version).graph["objects"].keys() & current.keys():
            mapped = _mapped(current[field], lambda value, field=field: value if (
                ref := object_ref(model, field, value)) is None else object_value(model, ref[0], shifted(*ref)))
            if mapped != current[field]:
                values[field] = mapped
        if values:
            edits.append((record, values))
    return [*([SetFields(document, edits, text)] if edits else []),
            *(Objects(document, model, model.blocks[block],
                      [at for at in range(len(model.blocks[block].object_table)) if at not in positions], text)
              for block, positions in gone.items())]


def _tree_objects(tree: Tree, nodes: list[Node], changes: dict) -> set[tuple[str, int]]:
    graph, actions, found = tree_rules(tree.version).graph, action_objects(tree), set()
    for node in nodes:
        record = {**vars(node), **changes.get(node, {})}
        found.update(actions.get(key) for key in zip(record["actions"], record["actions_ex"]))
        for field in graph["indexes"].keys() & node_fields(tree.version, record["attribute"]):
            _mapped(record[field], lambda value, field=field: found.add(object_ref(tree, field, value)))
    found.discard(None)
    return found


def default_entry(rules, record: dict) -> dict:
    start = rules.graph["transitions"]["start"]
    return appended_row(rules.node, record, start["target"], {
        start["target"]: record["children"][0], rules.graph["states"][start["target"]]: record["children_ex"][0],
        start["condition"]: -1})


def adopted(tree: Tree, parent: Node, rows: list[tuple[int, int, int]], index: int | None = None,
            changes: dict | None = None) -> dict:
    rules, record = tree_rules(tree.version), {**vars(parent), **(changes or {})}
    at = len(record["children"]) if index is None else index
    values = {name: [*record[name][:at], *(row[place] for row in rows), *record[name][at:]]
              for place, name in enumerate(("children", "children_ex", "conditions"))}
    start = rules.graph["transitions"]["start"]["target"]
    held = start in node_fields(tree.version, record["attribute"])
    if rows and held and not record["children"] and not record[start]:
        values.update(default_entry(rules, {**record, **values}))
    return values


def first_selector(tree: Tree, parent: Node) -> str | None:
    if parent.children or parent.selector_id >= 0:
        return None
    fsm = parent.attribute & attribute_bit(tree.version, "IsFSMNode")
    return tree_rules(tree.version).graph["fsm_selector"] if fsm else default_selector(tree)


def add_node(document: BhvtDocument, tree: Tree, parent: Node, name: str | None = None) -> Node:
    rules, text = tree_rules(tree.version), translate("BhvtDocument", "Add node")
    if parent.attribute & attribute_bit(tree.version, "HasReferenceTree"):
        raise BhvtFormatError(f"{parent.name} runs a reference tree, which holds its children")
    fsm = bool(parent.attribute & attribute_bit(tree.version, "IsFSMNode"))
    fresh, new_id = _fresh(rules.node, rules.new_node["fsm" if fsm else "plain"]), _new_id({
        item.id for item in tree.nodes})
    node = Node({**fresh, "id": new_id, "ex_id": 0, "name": name or fresh["name"].format(new_id),
                 "reference_tree": None})
    selector = first_selector(tree, parent)
    with document.macro(text):
        document.push(ReplaceContents(document, tree, "nodes", [*tree.nodes, node], text))
        set_fields(document, parent, text, **adopted(tree, parent, [(node.id, 0, -1)]))
        if selector is not None:
            set_selector(document, tree, parent, selector)
    return node


def set_selector(document: BhvtDocument, tree: Tree, node: Node, type_name: str | None) -> None:
    set_reference(document, tree, node, "selector_id", None, type_name, text=translate("BhvtDocument", "Set selector"))


def _referenced(document: BhvtDocument, tree: Tree, node: Node, field: str, value, old: list, text: str) -> None:
    changes = {node: {field: value}}
    gone = {ref for ref in old if ref is not None} - _tree_objects(tree, tree.nodes, changes)
    _push(document, _removal(document, tree, gone, tree.nodes, changes, text), text)


def _made(document: BhvtDocument, tree: Tree, field: str, type_name: str, created) -> int:
    block = object_block(tree, tree_rules(tree.version).graph["indexes"][field][0], type_name)
    return object_value(tree, block, add_object(document, tree, block, type_name, created=created))


def set_reference(document: BhvtDocument, tree: Tree, node: Node, field: str, index: int | None,
                  type_name: str | None, created=None, text: str | None = None) -> None:
    text, current = text or translate("BhvtDocument", "Set object"), getattr(node, field)
    old = object_ref(tree, field, current if index is None else current[index])
    with document.macro(text):
        value = -1 if type_name is None else _made(document, tree, field, type_name, created)
        _referenced(document, tree, node, field, value if index is None else [
            value if at == index else item for at, item in enumerate(current)], [old], text)


def add_reference(document: BhvtDocument, tree: Tree, node: Node, field: str, type_name: str, row: int | None = None,
                  created=None, text: str | None = None) -> None:
    text = text or translate("BhvtDocument", "Add object")
    with document.macro(text):
        value = _made(document, tree, field, type_name, created)
        items = [list(item) if isinstance(item, list) else item for item in getattr(node, field)]
        (items if row is None else items[row]).append(value)
        set_fields(document, node, text, **{field: items})


def remove_reference(document: BhvtDocument, tree: Tree, node: Node, field: str, position: int,
                     row: int | None = None, text: str | None = None) -> None:
    items = [list(item) if isinstance(item, list) else item for item in getattr(node, field)]
    old = object_ref(tree, field, (items if row is None else items[row]).pop(position))
    _referenced(document, tree, node, field, items, [old], text or translate("BhvtDocument", "Remove object"))


def remove_node(document: BhvtDocument, tree: Tree, node: Node) -> None:
    rules = tree_rules(tree.version)
    targets = {"children": "children_ex", **rules.graph["states"]}
    by_key, gone, stack = {(item.id, item.ex_id): item for item in tree.nodes}, set(), [node]
    while stack:
        item = stack.pop()
        gone.add((item.id, item.ex_id))
        stack.extend(by_key[key] for key in zip(item.children, item.children_ex))
    kept, changes = [item for item in tree.nodes if (item.id, item.ex_id) not in gone], {}
    for item in kept:
        record = vars(item)
        for group in (group for group in rules.node.layout(record) if isinstance(group, tuple)):
            for target in targets.keys() & set(group):
                keep = [at for at, key in enumerate(zip(record[target], record[targets[target]])) if key not in gone]
                if len(keep) < len(record[target]):
                    changes.setdefault(item, {}).update({name: [record[name][at] for at in keep] for name in group})
    start = rules.graph["transitions"]["start"]["target"]
    for item, values in changes.items():
        record = {**vars(item), **values}
        if start in values and not values[start] and record["children"]:
            values.update(default_entry(rules, record))
    objects = _tree_objects(tree, tree.nodes, {}) - _tree_objects(tree, kept, changes)
    text = translate("BhvtDocument", "Remove node")
    ids = transition_ids(tree, tree.nodes, {}) - transition_ids(tree, kept, changes)
    _push(document, [ReplaceContents(document, tree, "nodes", kept, text),
                     *_removal(document, tree, objects, kept, changes, text),
                     *map_removal(document, tree, ids, text)], text)


def set_attribute(document: BhvtDocument, tree: Tree, node: Node, attribute: int) -> None:
    rules = tree_rules(tree.version)
    kind = "fsm" if attribute & attribute_bit(tree.version, "IsFSMNode") else "plain"
    vars(node).update({name: value for name, value in _fresh(rules.node, rules.new_node[kind]).items()
                       if name not in vars(node)})
    referenced = bool(attribute & attribute_bit(tree.version, "HasReferenceTree"))
    reference = (node.reference_tree or "") if referenced else None
    set_fields(document, node, translate("BhvtDocument", "Node attributes"), attribute=attribute,
               reference_tree=reference)


def add_state(document: BhvtDocument, hfsm: Hfsm, parent: Record, name: str | None = None) -> Record:
    rules, text = hfsm_rules(hfsm.version), translate("BhvtDocument", "Add state")
    fresh, new_id = _fresh(rules.node, rules.new_node), _new_id({item.id for item in walk(hfsm.root)})
    state = Record({**fresh, "id": new_id, "name": name or fresh["name"].format(new_id), "selector": None,
                    "children": []})
    selector = parent.selector or Record({**copy.deepcopy(rules.new_selector), "state_attributes": []})
    with document.macro(text):
        set_fields(document, selector, text, state_attributes=[*selector.state_attributes, 0])
        set_fields(document, parent, text, children=[*parent.children, state], selector=selector,
                   default_start_child_id=parent.default_start_child_id if parent.children else state.id)
    return state


def _transitions(states: list[Record], changes: dict) -> list[Record]:
    rows = []
    for state in states:
        record = {**vars(state), **changes.get(state, {})}
        rows += record["start_transitions"]
        if (selector := record["selector"]) is not None:
            own = {**vars(selector), **changes.get(selector, {})}
            rows += own["all_state_transitions"] + own["links"]
    return [row for row in rows if "id" in vars(row)]


def _hfsm_objects(hfsm: Hfsm, records: list[Record]) -> set[tuple[str, int]]:
    found = set()
    for record in records:
        for field in hfsm_rules(hfsm.version).graph["objects"]:
            value = vars(record).get(field, [])
            found.update(object_ref(hfsm, field, item) for item in (value if isinstance(value, list) else [value]))
    return found - {None}


def detached(parent: Record, state: Record) -> tuple[set[int], dict]:
    gone, selector, start = {item.id for item in walk(state)}, parent.selector, parent.default_start_child_id
    index = next(index for index, child in enumerate(parent.children) if child is state)
    children = parent.children[:index] + parent.children[index + 1:]
    return gone, {
        parent: {"children": children, "selector": selector if children else None,
                 "default_start_child_id": (start if start != state.id else children[0].id) if children else 0},
        selector: {"state_attributes": selector.state_attributes[:index] + selector.state_attributes[index + 1:],
                   "links": [link for link in selector.links if not {link.from_id, link.to_id} & gone],
                   "all_state_transitions": [row for row in selector.all_state_transitions
                                             if row.to_node_id not in gone]}}


def remove_state(document: BhvtDocument, hfsm: Hfsm, parent: Record, state: Record) -> None:
    gone, changes = detached(parent, state)
    states = [item for item in walk(hfsm.root) if item.id not in gone]
    for item in states:
        rows = [row for row in item.start_transitions if row.to_node_id not in gone]
        if len(rows) < len(item.start_transitions):
            changes.setdefault(item, {})["start_transitions"] = rows
    text = translate("BhvtDocument", "Remove state")
    _push(document, [SetFields(document, list(changes.items()), text),
                     *_unlinked(document, hfsm, states, changes, text),
                     *_action_layout(document, hfsm, changes, text)], text)


def _unlinked(document: BhvtDocument, hfsm: Hfsm, states: list[Record], changes: dict,
              text: str) -> list[QUndoCommand]:
    before, after = _transitions(list(walk(hfsm.root)), {}), _transitions(states, changes)
    unused = {row.id for row in before} - {row.id for row in after}
    records = [record for record in hfsm.conditions if record.uid not in unused]
    objects = _hfsm_objects(hfsm, [*hfsm.conditions, *before]) - _hfsm_objects(hfsm, [*records, *after])
    return [*([ReplaceContents(document, hfsm, "conditions", records, text)]
              if len(records) < len(hfsm.conditions) else []),
            *_removal(document, hfsm, objects, [*records, *after], {}, text),
            *transition_record_removal(document, hfsm, unused, text)]


def motion_entry(document: BhvtDocument, hfsm: Hfsm):
    container = document.resource.model
    return next((entry for entry in container.fsms if entry.hfsm is hfsm), None) \
        if isinstance(container, MotionFsmV1) else None


def transition_record_removal(document: BhvtDocument, hfsm: Hfsm, ids: set[int], text: str) -> list[QUndoCommand]:
    entry = motion_entry(document, hfsm)
    kept = [] if entry is None else [item for item in entry.transitions if item.id not in ids]
    return [] if entry is None or len(kept) == len(entry.transitions) else \
        [ReplaceContents(document, entry, "transitions", kept, text)]


def _action_layout(document: BhvtDocument, hfsm: Hfsm, changes: dict, text: str,
                   added: dict | None = None) -> list[QUndoCommand]:
    positions, order, stack = {**hfsm_action_objects(hfsm), **(added or {})}, [], [hfsm.root]
    while stack:
        record = {**vars(state := stack.pop()), **changes.get(state, {})}
        order += [(action.id, action.list_no) for action in record["actions"]]
        stack.extend(reversed(record["children"]))
    table = [positions[key] for key in dict.fromkeys(order) if key in positions]
    return [] if table == list(range(len(hfsm.blocks["actions"].object_table))) else \
        [Objects(document, hfsm, hfsm.blocks["actions"], table, text)]


def add_object(document: BhvtDocument, model: Tree | Hfsm, block: str, type_name: str, position: int | None = None,
               created=None, **fields) -> int:
    rsz = model.blocks[block]
    created = created or new_instances(rsz, type_name, constructor_values(rsz.game_version).get(type_name, {}))
    for name, value in fields.items():
        created[-1][1][field_key(created[-1][1], name)].value = value
    position = len(rsz.object_table) if position is None else position
    table = list(range(len(rsz.object_table)))
    table.insert(position, created)
    document.push(Objects(document, model, rsz, table, translate("BhvtDocument", "Add object")))
    return position


def add_action(document: BhvtDocument, tree: Tree, node: Node, type_name: str, created=None) -> int:
    graph = tree_rules(tree.version).graph["actions"]
    used = {field_value(tree.blocks[block].parsed_elements[instance], graph["id_field"])
            for block in graph["blocks"] for instance in tree.blocks[block].object_table}
    action_id, text = _new_id(used), translate("BhvtDocument", "Add action")
    with document.macro(text):
        add_object(document, tree, object_block(tree, "actions", type_name), type_name, created=created,
                   **{graph["id_field"]: action_id})
        set_fields(document, node, text, actions=[*node.actions, action_id], actions_ex=[*node.actions_ex, 0])
    return action_id


def remove_action(document: BhvtDocument, model: Tree | Hfsm, node: Node | Record, index: int) -> None:
    text = translate("BhvtDocument", "Remove action")
    if isinstance(model, Hfsm):
        changes = {node: {"actions": node.actions[:index] + node.actions[index + 1:]}}
        _push(document, [SetFields(document, list(changes.items()), text),
                         *_action_layout(document, model, changes, text)], text)
        return
    key = (node.actions[index], node.actions_ex[index])
    changes = {node: {"actions": node.actions[:index] + node.actions[index + 1:],
                      "actions_ex": node.actions_ex[:index] + node.actions_ex[index + 1:]}}
    used = any(key in zip(record["actions"], record["actions_ex"])
               for record in ({**vars(item), **changes.get(item, {})} for item in model.nodes))
    _push(document, _removal(document, model, set() if used else {action_objects(model).get(key)} - {None},
                             model.nodes, changes, text), text)


def move_action(document: BhvtDocument, model: Tree | Hfsm, node: Node | Record, index: int, to: int) -> None:
    text = translate("BhvtDocument", "Move action")
    names = ("actions",) if isinstance(model, Hfsm) else ("actions", "actions_ex")
    values = {}
    for name in names:
        items = list(getattr(node, name))
        items.insert(to, items.pop(index))
        values[name] = items
    changes = {node: values}
    _push(document, [SetFields(document, list(changes.items()), text),
                     *(_action_layout(document, model, changes, text) if isinstance(model, Hfsm) else [])], text)


def add_state_action(document: BhvtDocument, hfsm: Hfsm, state: Record, type_name: str, created=None) -> int:
    names = hfsm_rules(hfsm.version).graph["action_fields"]
    held = hfsm_action_objects(hfsm)
    action_id, text, keys = _new_id({key[0] for key in held}), translate("BhvtDocument", "Add action"), set()
    for node in walk(hfsm.root):
        keys.update(key for action in node.actions if (key := (action.id, action.list_no)) in held)
        if node is state:
            break
    with document.macro(text):
        add_object(document, hfsm, "actions", type_name, len(keys), created,
                   **{names["id"]: action_id, names["list_no"]: 0})
        set_fields(document, state, text, actions=[*state.actions, Record({"id": action_id, "list_no": 0})])
    return action_id
