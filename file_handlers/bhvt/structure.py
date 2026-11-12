"""See docs/internals/bhvt-graph.md, Authoring."""

from __future__ import annotations

from dataclasses import dataclass

lazy from file_handlers.rsz.utils.rsz_field_utils import is_type_assignable

from .hfsm_codec import walk
from .model import Hfsm, Tree
from .references import action_objects, field_value, hfsm_action_objects, object_ref
from .rules import attribute_bit, node_fields, rules_of


@dataclass(frozen=True, slots=True)
class Transition:
    kind: str
    owner: object
    index: int


class Structure:
    def __init__(self, model: Tree | Hfsm):
        self.model, self.hfsm, self.rules = model, isinstance(model, Hfsm), rules_of(model)
        self.graph = self.rules.graph
        self.kinds = self.graph["transitions"]
        if self.hfsm:
            self.nodes = list(walk(model.root))
            self.by_key = {node.id: node for node in self.nodes}
            self.records = {}
            for record in model.conditions:
                self.records.setdefault(record.uid, record)
        else:
            self.nodes = model.nodes
            self.by_key = {(node.id, node.ex_id): node for node in self.nodes}
            self.fsm_bit = attribute_bit(model.version, "IsFSMNode")
        self.root = self.nodes[0]
        self.parent = {id(child): node for node in self.nodes for child in self.children(node)}
        self._actions = None

    def key(self, node) -> object:
        return node.id if self.hfsm else (node.id, node.ex_id)

    def children(self, node) -> list:
        return list(node.children) if self.hfsm else \
            [self.by_key[key] for key in zip(node.children, node.children_ex)]

    def preorder(self, node=None):
        stack = [node or self.root]
        while stack:
            yield (item := stack.pop())
            stack.extend(self.children(item)[::-1])

    def is_fsm(self, node) -> bool:
        return self.hfsm or bool(node.attribute & self.fsm_bit)

    def holds(self, node, field: str | None) -> bool:
        return field in node_fields(self.model.version, node.attribute)

    def is_state(self, node) -> bool:
        parent = self.parent.get(id(node))
        return parent is not None and self.is_fsm(parent) and self.is_fsm(node)

    def path(self, node) -> list:
        chain = [node]
        while (parent := self.parent.get(id(chain[-1]))) is not None:
            chain.append(parent)
        return chain[::-1]

    def holder(self, kind: str, owner):
        return owner.selector if self.kinds[kind].get("in") == "selector" else owner

    def rows(self, kind: str, owner) -> list:
        spec = self.kinds[kind]
        if self.hfsm:
            holder = self.holder(kind, owner)
            return [] if holder is None else getattr(holder, spec["rows"])
        return vars(owner)[spec["target"]] if self.holds(owner, spec["target"]) else []

    def transitions(self, container) -> list[Transition]:
        owners = [(kind, container) for kind in self.kinds] if self.hfsm else \
            [*(("state", child) for child in self.children(container) if self.is_fsm(child)),
             ("start", container), ("any", container)]
        return [Transition(kind, owner, index) for kind, owner in owners
                for index in range(len(self.rows(kind, owner)))]

    def outgoing(self, state) -> list[Transition]:
        if not self.hfsm:
            return [Transition("state", state, index) for index in range(len(self.rows("state", state)))]
        parent = self.parent.get(id(state))
        return [] if parent is None else [Transition("link", parent, index)
                                          for index, row in enumerate(self.rows("link", parent))
                                          if getattr(row, self.kinds["link"]["source"]) == state.id]

    def value(self, transition: Transition, role: str):
        spec = self.kinds[transition.kind]
        if self.hfsm:
            row = self.rows(transition.kind, transition.owner)[transition.index]
            return vars(row).get(spec.get(role, role))
        name = spec.get(role)
        return vars(transition.owner)[name][transition.index] if self.holds(transition.owner, name) else None

    def target(self, transition: Transition):
        target = self.value(transition, "target")
        if not self.hfsm:
            target = (target, vars(transition.owner)[self.graph["states"][self.kinds[transition.kind]["target"]]][
                transition.index])
        return self.by_key.get(target)

    def source(self, transition: Transition):
        if transition.kind == "state":
            return transition.owner
        return self.by_key.get(self.value(transition, "source")) if transition.kind == "link" else None

    def transition_id(self, transition: Transition) -> int | None:
        return self.value(transition, "id")

    def targets(self, kind: str, container) -> list:
        reach = self.kinds[kind].get("targets")
        nodes = self.nodes if reach is None else self.children(container) if reach == "children" else list(
            self.preorder(container))[1:]
        return [node for node in nodes if self.is_fsm(node) and node is not self.root]

    def container(self, transition: Transition):
        return self.parent.get(id(transition.owner)) if transition.kind == "state" else transition.owner

    def token(self, transition: Transition) -> object:
        if self.hfsm:
            return self.rows(transition.kind, transition.owner)[transition.index]
        value = self.transition_id(transition)
        return transition.kind, id(transition.owner), transition.index if value is None else -1, value

    def found(self, transition: Transition, token) -> Transition | None:
        if not (transition.owner is self.root or id(transition.owner) in self.parent):
            return None
        count = len(self.rows(transition.kind, transition.owner))
        return next((item for item in (Transition(transition.kind, transition.owner, index)
                                       for index in (transition.index, *range(count)) if index < count)
                     if self.token(item) == token), None)

    def condition(self, transition: Transition) -> tuple[str, int] | None:
        if not self.hfsm:
            return object_ref(self.model, self.kinds[transition.kind]["condition"],
                              self.value(transition, "condition"))
        record = self.records.get(self.transition_id(transition))
        return None if record is None else object_ref(self.model, "instance", record.instance)

    def condition_record(self, transition: Transition):
        return self.records.get(self.transition_id(transition)) if self.hfsm else None

    def events(self, transition: Transition) -> list[tuple[str, int] | None]:
        role = "transition_events" if self.hfsm else "events"
        return [object_ref(self.model, role if self.hfsm else self.kinds[transition.kind][role], value)
                for value in self.value(transition, role) or []]

    def child_condition(self, container, index: int) -> tuple[str, int] | None:
        return None if self.hfsm else object_ref(self.model, "conditions", container.conditions[index])

    def selector(self, node) -> tuple[str, int] | None:
        return None if self.hfsm else object_ref(self.model, "selector_id", node.selector_id)

    def actions(self, node) -> list[tuple[str, int] | None]:
        if self._actions is None:
            self._actions = hfsm_action_objects(self.model) if self.hfsm else action_objects(self.model)
        if self.hfsm:
            return [("actions", place) if (place := self._actions.get((action.id, action.list_no))) is not None
                    else None for action in node.actions]
        return [self._actions.get(key) for key in zip(node.actions, node.actions_ex)]

    def naming(self, rule: list):
        base, name = rule
        for node in self.nodes:
            for block, place in filter(None, self.actions(node)):
                rsz = self.model.blocks[block]
                index = rsz.object_table[place]
                kind = rsz.type_registry.get_type_info(rsz.instance_infos[index].type_id)["name"]
                if is_type_assignable(rsz.type_registry, kind, base):
                    yield node, field_value(rsz.parsed_elements[index], name)
