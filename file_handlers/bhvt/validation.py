"""See docs/internals/bhvt-editor.md, Validation."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import partial

from PySide6.QtCore import QCoreApplication

lazy from file_handlers.rsz.utils.rsz_field_utils import is_type_assignable
lazy from utils.hex_util import guid_le_to_str

from .model import Hfsm, MotionFsm, MotionFsmV1, TimelineFsm, Tree
from .references import asset, callbacks, field_value, object_ref, reached
from .rules import attribute_bit, motion_fsm_rules, motion_fsm_v1_rules, node_fields
from .sections import signed
from .structure import Structure, Transition
from .tree_codec import NO_ID

translate = QCoreApplication.translate
ERROR, WARNING = "error", "warning"


@dataclass(frozen=True, slots=True)
class Problem:
    severity: str
    code: str
    text: str
    model: object = None
    target: object = None


def graphs(resource) -> list[Tree | Hfsm]:
    model = resource.model
    if isinstance(model, MotionFsm):
        return [model.tree]
    if isinstance(model, TimelineFsm):
        return [item.tree for item in model.trees]
    return [entry.hfsm for entry in model.fsms] if isinstance(model, MotionFsmV1) else [model]


def _add(found: list, model, severity: str, code: str, text: str, target=None) -> None:
    found.append(Problem(severity, code, text, model, target))


def problems(resource) -> list[Problem]:
    found: list[Problem] = []
    for model in graphs(resource):
        (_hfsm if isinstance(model, Hfsm) else _tree)(model, partial(_add, found, model))
    if isinstance(resource.model, MotionFsm):
        _motion(resource.model, partial(_add, found, resource.model.tree))
    if isinstance(resource.model, TimelineFsm):
        _timelines(resource.model, found)
    if isinstance(resource.model, MotionFsmV1):
        _motion_v1(resource.model, found)
    return found


def errors(resource) -> list[Problem]:
    return [item for item in problems(resource) if item.severity == ERROR]


def _doubled(users: list, path: str, top=None):
    held = defaultdict(list)
    for user in users:
        if key := asset(user, path):
            held[key[1]].append(user)
        yield from _doubled(user.embedded_userdata_infos, path, top or user)
    yield from ((top or same[-1], name, len(same)) for name, same in held.items() if len(same) > 1)


def _objects(model: Tree | Hfsm, structure: Structure, report) -> None:
    for name, rsz in model.blocks.items():
        base = structure.graph["slots"][name]
        for place, instance in enumerate(rsz.object_table):
            type_name = rsz.type_registry.get_type_info(rsz.instance_infos[instance].type_id)["name"]
            if not is_type_assignable(rsz.type_registry, type_name, base):
                report(ERROR, "slot", translate("BhvtValidation", "{0} object {1} is a {2}, which is not a {3}").format(
                    name, place, type_name, base))
        for user, path, count in _doubled(rsz.rsz_userdata_infos, structure.graph["user_path"]) \
                if rsz.has_embedded_rsz else ():
            report(WARNING, "asset", translate("BhvtValidation", "{0} holds {1} user data of {2}").format(
                name, count, path.rsplit("/", 1)[-1]), next((
                    node for node in structure.nodes for ref in structure.actions(node)
                    if ref is not None and ref[0] == name
                    and user.instance_id in reached(rsz, rsz.object_table[ref[1]])), None))


def _tree(tree: Tree, report) -> None:
    keys = Counter((node.id, node.ex_id) for node in tree.nodes)
    for key, count in keys.items():
        if count > 1:
            report(ERROR, "node-id", translate("BhvtValidation", "{0} nodes share the id {1}").format(count, key[0]))
    if any(count > 1 for count in keys.values()):
        return
    missing = [(node, key) for node in tree.nodes for key in zip(node.children, node.children_ex) if key not in keys]
    for node, key in missing:
        report(ERROR, "child", translate("BhvtValidation", "{0} lists a child that does not exist ({1})").format(
            node.name, key[0]), node)
    if missing:
        return
    structure = Structure(tree)
    if (tree.nodes[0].id, tree.nodes[0].ex_id) != (0, 0):
        report(ERROR, "root", translate("BhvtValidation", "The first node is not the root (id 0)"), tree.nodes[0])
    parents = Counter(key for node in tree.nodes for key in zip(node.children, node.children_ex))
    reference = attribute_bit(tree.version, "HasReferenceTree")
    for node in tree.nodes:
        key = (node.id, node.ex_id)
        if parents[key] > 1:
            report(ERROR, "parents", translate("BhvtValidation", "{0} is a child of {1} nodes").format(
                node.name, parents[key]), node)
        if node is not tree.nodes[0] and not parents[key]:
            report(ERROR, "orphan", translate("BhvtValidation", "{0} has no parent").format(node.name), node)
        if "\0" in node.name:
            report(ERROR, "name", translate("BhvtValidation", "{0} has a NUL in its name").format(node.name), node)
        if node.attribute & reference and not node.reference_tree:
            report(ERROR, "reference", translate("BhvtValidation", "{0} runs a reference tree without a path").format(
                node.name), node)
        _tree_node(tree, structure, node, report)
    _objects(tree, structure, report)
    for node, found in callbacks(tree):
        if not found:
            report(WARNING, "callback", translate(
                "BhvtValidation", "A variable expression calls a condition object this tree does not hold"), node)


def _tree_node(tree: Tree, structure: Structure, node, report) -> None:
    graph, record = structure.graph, vars(node)
    for group in (entry for entry in structure.rules.node.layout(record) if isinstance(entry, tuple)):
        if len({len(record[name]) for name in group}) > 1:
            report(ERROR, "rows", translate("BhvtValidation", "{0}: {1} are not the same length").format(
                node.name, ", ".join(group)), node)
            return
    for field in graph["indexes"].keys() & node_fields(tree.version, node.attribute):
        values = record[field] if isinstance(record[field], list) else [record[field]]
        for value in (item for value in values for item in (value if isinstance(value, list) else [value])):
            if value not in (-1, NO_ID) and object_ref(tree, field, value) is None:
                report(ERROR, "object", translate("BhvtValidation", "{0}: {1} names object {2}, which does not "
                                                  "exist").format(node.name, field, value), node)
    for index, found in enumerate(structure.actions(node)):
        if found is None:
            report(ERROR, "action", translate("BhvtValidation", "{0}: action {1} has no object").format(
                node.name, node.actions[index]), node)
    if not structure.is_fsm(node):
        if node.children and node.selector_id < 0:
            report(WARNING, "selector", translate("BhvtValidation", "{0} has children but no selector").format(
                node.name), node)
        return
    for kind in structure.kinds:
        for index in range(len(structure.rows(kind, node))):
            transition = Transition(kind, node, index)
            target = structure.target(transition)
            if target is None:
                report(ERROR, "target", translate("BhvtValidation", "{0}: a transition goes to a node that does not "
                                                  "exist").format(node.name), transition)
            elif not structure.is_fsm(target):
                report(ERROR, "target-kind", translate("BhvtValidation", "{0}: a transition goes to {1}, which is not "
                                                       "a state").format(node.name, target.name), transition)
    if node.children and not node.attribute & attribute_bit(tree.version, "HasReferenceTree"):
        if not structure.rows("start", node):
            report(ERROR, "entry", translate("BhvtValidation", "{0} has states but no entry state").format(node.name),
                   node)
        if node.selector_id < 0:
            report(ERROR, "fsm-selector", translate("BhvtValidation", "{0} has states but no FSM selector").format(
                node.name), node)


def _motion(fsm: MotionFsm, report) -> None:
    ids = [item.transition_id for item in fsm.transition_maps]
    if ids != sorted(set(ids)):
        report(ERROR, "maps", translate("BhvtValidation", "The blending maps are not in transition id order, or "
                                        "repeat an id"))
    count = len(fsm.transition_data)
    if any(not 0 <= item.data_index < count for item in fsm.transition_maps) or \
            fsm.header.start_transition_data_index >= max(count, 1):
        report(ERROR, "preset", translate("BhvtValidation", "A blending map names a preset that does not exist"))
    extras = "transition_extra_table" in motion_fsm_rules(fsm.version).header.fields
    used = max((item.extra_index + item.extra_count for item in fsm.transition_data if extras), default=0)
    if used != len(fsm.transition_extras):
        report(ERROR, "extras", translate("BhvtValidation", "The presets use {0} extra records, the file holds "
                                          "{1}").format(used, len(fsm.transition_extras)))


def _motion_v1(fsm: MotionFsmV1, found: list) -> None:
    read = motion_fsm_v1_rules(fsm.version).fsms_read
    for entry in fsm.fsms[read:]:
        _add(found, entry.hfsm, WARNING, "unread", translate(
            "BhvtValidation", "The game reads {0} of the {1} FSMs of this file: layer {2} is never read").format(
                read, len(fsm.fsms), entry.info.layer_no))
    for entry in fsm.fsms:
        ids = [signed(item.id) for item in entry.transitions]
        if ids != sorted(set(ids)):
            _add(found, entry.hfsm, ERROR, "blending", translate(
                "BhvtValidation", "The blending records of layer {0} are not in id order, or repeat an "
                "id").format(entry.info.layer_no))


def _timelines(fsm: TimelineFsm, found: list) -> None:
    first = fsm.trees[0].tree
    for values, text in (
            ([item.layer_id for item in fsm.trees], translate("BhvtValidation", "{0} layers share the id {1}")),
            ([entry.timeline_id for entry in fsm.timelines],
             translate("BhvtValidation", "{0} timelines share the id {1}")),
            ([hold.hold_point_id for hold in fsm.hold_points],
             translate("BhvtValidation", "{0} hold points share the id {1}"))):
        for value, count in Counter(values).items():
            if count > 1:
                _add(found, first, ERROR, "container-id", text.format(count, guid_le_to_str(value)))
    held, holds = {entry.timeline_id for entry in fsm.timelines}, {hold.hold_point_id for hold in fsm.hold_points}
    layers, reaching = {item.layer_id: item for item in fsm.trees}, defaultdict(set)
    for item in fsm.trees:
        structure = Structure(item.tree)
        for node, value in structure.naming(structure.graph["timeline"]):
            if value not in held:
                _add(found, item.tree, WARNING, "timeline", translate(
                    "BhvtValidation", "{0} plays a timeline this file does not hold").format(node.name), node)
        for node, value in structure.naming(structure.graph["hold_point"]):
            reaching[value].add(item.layer_id)
            if value not in holds:
                _add(found, item.tree, WARNING, "hold", translate(
                    "BhvtValidation", "{0} reaches a hold point this file does not hold").format(node.name), node)
    for number, hold in enumerate(fsm.hold_points, 1):
        for target in hold.targets:
            if target not in layers:
                _add(found, first, WARNING, "hold-layer", translate(
                    "BhvtValidation", "Hold point {0} waits for a layer this file does not hold").format(number))
            elif target not in reaching[hold.hold_point_id]:
                _add(found, layers[target].tree, WARNING, "hold-wait", translate(
                    "BhvtValidation", "Hold point {0} waits for {1}, where no action reaches it").format(
                        number, layers[target].name))


def _hfsm(hfsm: Hfsm, report) -> None:
    structure = Structure(hfsm)
    ids = Counter(node.id for node in structure.nodes)
    for value, count in ids.items():
        if count > 1:
            report(ERROR, "node-id", translate("BhvtValidation", "{0} states share the id {1}").format(count, value))
    uids = Counter(record.uid for record in hfsm.conditions)
    for value, count in uids.items():
        if count > 1:
            report(ERROR, "condition-id", translate("BhvtValidation", "{0} condition records share the id {1}").format(
                count, value))
    base = structure.graph["objects"]["instance"][1]
    for record in hfsm.conditions:
        if not 0 <= record.instance < base and object_ref(hfsm, "instance", record.instance) is None:
            report(ERROR, "object", translate("BhvtValidation", "Condition {0} names an object that does not "
                                              "exist").format(record.uid))
    names, actions = structure.graph["action_fields"], hfsm.blocks["actions"]
    held = Counter((field_value(actions.parsed_elements[instance], names["id"]),
                    field_value(actions.parsed_elements[instance], names["list_no"]))
                   for instance in actions.object_table)
    used = set()
    for state in structure.nodes:
        if "\0" in state.name:
            report(ERROR, "name", translate("BhvtValidation", "{0} has a NUL in its name").format(state.name), state)
        children = {child.id for child in state.children}
        if state.children and state.default_start_child_id not in children:
            report(ERROR, "entry", translate("BhvtValidation", "{0} has states but its entry state is not one of "
                                             "them").format(state.name), state)
        for action in state.actions:
            used.add((action.id, action.list_no))
            if held[action.id, action.list_no] != 1:
                report(ERROR, "action", translate("BhvtValidation", "{0}: action {1} has no object of its "
                                                  "own").format(state.name, action.id), state)
        for transition in structure.transitions(state):
            row = structure.rows(transition.kind, state)[transition.index]
            ends = [row.from_id, row.to_id] if transition.kind == "link" else \
                [row.to_node_id] if transition.kind == "any" else []
            if any(end not in children for end in ends):
                report(ERROR, "target", translate("BhvtValidation", "{0}: a transition joins states that are not its "
                                                  "children").format(state.name), transition)
            if transition.kind == "start" and structure.target(transition) is None:
                report(WARNING, "start-target", translate("BhvtValidation", "{0}: a start transition goes to a state "
                                                          "that does not exist").format(state.name), transition)
            if "id" not in vars(row):
                continue
            if None in structure.events(transition):
                report(ERROR, "object", translate("BhvtValidation", "{0}: a transition runs an event that does not "
                                                  "exist").format(state.name), transition)
            if row.id not in uids:
                report(WARNING, "never", translate("BhvtValidation", "{0}: a transition has no condition and is "
                                                   "never taken").format(state.name), transition)
    if len(used) != len(actions.object_table):
        report(ERROR, "action-count", translate("BhvtValidation", "The states use {0} actions, the action block holds "
                                                "{1}").format(len(used), len(actions.object_table)))
    _objects(hfsm, structure, report)
