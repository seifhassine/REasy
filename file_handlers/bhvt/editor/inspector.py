"""See docs/internals/bhvt-editor.md, Inspector."""

from __future__ import annotations

from functools import partial

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QScrollArea, QVBoxLayout, QWidget

lazy from ui.editor_widgets import styled_label
lazy from ui.styles import set_style_role

from ui.field_form import Field, FieldForm

from .. import clipboard, edits, graph_edits
from ..model import Record
from ..references import callbacks, object_ref, slot_base, tag_names
from ..rules import authoring, display_names, enum_names, field_enums, motion_fsm_rules, motion_fsm_v1_rules
from ..structure import Transition
from ..summaries import short_name
from .pickers import class_entries, custom_tag, node_entries, tag_entries
from .rows import Row, Rows

_F32 = 3.4028234663852886e38


class Inspector(QScrollArea):
    raw_requested = Signal(str, object)

    def __init__(self, state, popup, parent: QWidget | None = None):
        super().__init__(parent)
        self.state, self.popup = state, popup
        self.expanded: set = set()
        self._signature, self._stale, self._scroll = None, True, 0
        self.parts: list[QWidget] = []
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget(self)
        set_style_role(body, surface="view")
        self.setWidget(body)
        self._column = QVBoxLayout(body)
        self._column.setContentsMargins(0, 0, 0, 0)
        self._column.setSpacing(0)
        self.title = styled_label("", body, heading="panel")
        self.title.setContentsMargins(8, 6, 8, 2)
        self._column.addWidget(self.title)
        self._column.addStretch(1)
        self._refresh = QTimer(self)
        self._refresh.setSingleShot(True)
        self._refresh.timeout.connect(self.sync)
        self._restore = QTimer(self)
        self._restore.setSingleShot(True)
        self._restore.timeout.connect(self._scrolled)
        for signal in (state.selection_changed, state.scope_changed):
            signal.connect(self._refresh.start)
        state.changed.connect(self._changed)
        state.copied.connect(self._copied)
        self.sync()

    def _changed(self, reshaped: bool) -> None:
        self._stale |= reshaped
        self._refresh.start()

    def _copied(self) -> None:
        self._changed(True)

    def _scrolled(self) -> None:
        self.verticalScrollBar().setValue(self._scroll)

    def sync(self) -> None:
        if self.state.settling:
            return
        subject = self.state.current or self.state.scope
        signature = (id(self.state.model), subject if isinstance(subject, Transition) else id(subject))
        title, parts = self._describe(subject)
        if signature == self._signature and not self._stale:
            for widget, part in zip(self.parts, parts):
                widget.refresh(*part[2:3])
            return
        self._scroll = self.verticalScrollBar().value() if signature == self._signature else 0
        for widget in self.parts:
            if isinstance(widget, FieldForm):
                widget.retire()
            widget.hide()
            widget.deleteLater()
        body = self.widget()
        self.parts = [FieldForm(part[1], body) if part[0] == "fields" else
                       Rows(self.state, self.popup, self.expanded, part[1], part[2], body, part[3], **part[4])
                       for part in parts]
        for at, widget in enumerate(self.parts, 1):
            self._column.insertWidget(at, widget)
        self.title.setText(title)
        self._signature, self._stale = signature, False
        self._restore.start()

    def _describe(self, subject) -> tuple[str, list[tuple]]:
        if isinstance(subject, Transition):
            return self._transition(subject)
        if not isinstance(subject, Record):
            return self._variable(subject)
        return (self._state if self.state.structure.hfsm else self._node)(subject)

    def _set(self, target, text: str, name: str, value) -> None:
        edits.set_fields(self.state.document, target, text, **{name: type(getattr(target, name))(value)})

    def _text(self, label: str, kind: str, target, name: str, text: str) -> Field:
        return Field(label, kind, partial(getattr, target, name), partial(self._set, target, text, name))

    @staticmethod
    def _fitted(codec, name: str, value):
        low, high = codec.limits.get(name, (-_F32, _F32))
        if not low <= value <= high:
            raise ValueError(value)
        return value

    def _set_number(self, target, text: str, name: str, codec, value) -> None:
        self._set(target, text, name, self._fitted(codec, name, value))

    @staticmethod
    def _bit(target, name: str, mask: int) -> bool:
        return bool(getattr(target, name) & mask)

    def _set_bit(self, target, text: str, name: str, mask: int, on: bool) -> None:
        value = getattr(target, name)
        edits.set_fields(self.state.document, target, text, **{name: value | mask if on else value & ~mask})

    def _set_attribute(self, node, mask: int, on: bool) -> None:
        edits.set_attribute(self.state.document, self.state.model, node,
                            node.attribute | mask if on else node.attribute & ~mask)

    def _objects(self, title: str, refs: list, slot: str, kind: str, *, single=None, add=None, remove=None, move=None,
                 empty: str = "", skip=frozenset(), exclude=()) -> tuple:
        summaries, model = self.state.summaries, self.state.model
        fixed = frozenset(self.state.structure.graph["managed"].get(slot_base(model, slot), ()))
        held = [ref for ref in refs if ref is not None] if single is not None or add is None else refs
        rows = [Row(self.tr("Missing object")) if ref is None else
                Row(short_name(summaries.type_name(*ref)), summaries.detail(*ref, 3), ref, skip, fixed)
                for ref in held]
        receiver = single or add
        ops = {"copy": partial(self._copy, kind, held), "move": move,
               "remove": remove if single is None else partial(self._clear, single)}
        if receiver is not None:
            ops["add"] = partial(self._pick_class, slot, receiver, exclude)
            if clipboard.accepts(model, kind):
                ops["paste"] = partial(self._paste, kind, receiver, single is not None)
        return "rows", title, rows, empty, ops if receiver is not None else {}

    def _pick_class(self, slot: str, receiver, exclude) -> None:
        model = self.state.model
        self.popup.pick([entry for entry in class_entries(model, slot, slot_base(model, slot))
                         if entry[2] not in exclude], QCursor.pos(), self.tr("Search classes"), receiver)

    @staticmethod
    def _clear(single, _index: int) -> None:
        single(None)

    def _copy(self, kind: str, refs: list, index: int) -> None:
        clipboard.copy_objects(self.state.model, kind, [refs[index]])
        self.state.copied.emit()

    def _paste(self, kind: str, receiver, single: bool) -> None:
        pasted = clipboard.pasted_objects(self.state.model, kind)[:1 if single else None]
        with self.state.document.macro(self.tr("Paste")):
            for name, created in pasted:
                receiver(name, created)

    def _add_reference(self, node, field: str, type_name: str, created=None) -> None:
        edits.add_reference(self.state.document, self.state.model, node, field, type_name, None, created)

    def _remove_item(self, target, name: str, text: str, index: int) -> None:
        items = getattr(target, name)
        edits.set_fields(self.state.document, target, text, **{name: items[:index] + items[index + 1:]})

    def _tags(self, node) -> tuple:
        names = tag_names(self.state.summaries.game)
        rows = [Row(*names.get(tag, f".0x{tag:08X}").rpartition(".")[::-2]) for tag in node.tags]
        return "rows", self.tr("Tags"), rows, "", {
            "add": partial(self._pick_tag, node), "remove": partial(self._remove_item, node, "tags",
                                                                    self.tr("Remove tag"))}

    def _pick_tag(self, node) -> None:
        self.popup.pick(tag_entries(self.state.summaries.game), QCursor.pos(), self.tr("Search tags, or Enum.Member"),
                        partial(self._add_tag, node), custom_tag)

    def _add_tag(self, node, value: int) -> None:
        edits.set_fields(self.state.document, node, self.tr("Add tag"), tags=[*node.tags, value])

    def _links(self, title: str, transitions: list[Transition], container, source, empty: str = "") -> tuple:
        structure, summaries = self.state.structure, self.state.summaries
        rows = []
        for item in transitions:
            target = structure.target(item)
            rows.append(Row(self.tr("to {0}").format("?" if target is None else target.name),
                            summaries.condition_text(structure.condition(item), structure.condition_record(item))))
        return "rows", title, rows, empty, {
            "open": partial(self._open_link, transitions), "add": partial(self._pick_target, container, source),
            "remove": partial(self._remove_link, transitions), "move": partial(self._move_link, transitions)}

    def _open_link(self, transitions: list[Transition], index: int) -> None:
        self.state.select([transitions[index]])

    def _remove_link(self, transitions: list[Transition], index: int) -> None:
        graph_edits.remove_transitions(self.state.document, self.state.model, [transitions[index]])

    def _move_link(self, transitions: list[Transition], index: int, to: int) -> None:
        graph_edits.move_transition(self.state.document, self.state.model, transitions[index], to)

    def add_transition(self) -> None:
        node = self.state.nodes()[-1]
        self._pick_target(self.state.structure.parent[id(node)], node)

    def _pick_target(self, container, source) -> None:
        structure = self.state.structure
        kind = source if isinstance(source, str) else "link" if structure.hfsm else "state"
        self.popup.pick(node_entries(structure, structure.targets(kind, container), container), QCursor.pos(),
                        self.tr("Search states"), partial(self._add_link, container, source))

    def _add_link(self, container, source, target) -> None:
        self.state.select([graph_edits.add_transition(self.state.document, self.state.model, container, source,
                                                      target)])

    def _children(self, node) -> tuple:
        structure, summaries = self.state.structure, self.state.summaries
        children = structure.children(node)
        rows = [Row(child.name, summaries.condition_text(structure.child_condition(node, index)))
                for index, child in enumerate(children)]
        return "rows", self.tr("States") if structure.is_fsm(node) else self.tr("Children"), rows, "", {
            "open": partial(self._open_child, children), "add": partial(self._add_child, node),
            "remove": partial(self._remove_child, children), "move": partial(self._move_child, node, children)}

    def _open_child(self, children: list, index: int) -> None:
        self.state.reveal(children[index])

    def _add_child(self, node) -> None:
        if (child := graph_edits.add_child(self.state.document, self.state.model, node)) is not None:
            self.state.reveal(child)

    def _remove_child(self, children: list, index: int) -> None:
        graph_edits.remove_items(self.state.document, self.state.model, [children[index]], [])

    def _move_child(self, node, children: list, index: int, to: int) -> None:
        graph_edits.move_node(self.state.document, self.state.model, children[index], node, to)

    def _node(self, node) -> tuple[str, list[tuple]]:
        state = self.state
        structure, tree, document = state.structure, state.model, state.document
        parent, fsm, text = structure.parent.get(id(node)), structure.is_fsm(node), self.tr("Edit node")
        fields = [self._text(self.tr("Name"), "text", node, "name", self.tr("Rename"))]
        locked = {"IsFSMNode": not self._converts(node), "HasReferenceTree": bool(node.children)}
        for mask, name in display_names("NodeAttribute", tree.version).items():
            fields.append(Field(name, "readonly", partial(self._bit, node, "attribute", mask)) if locked.get(name)
                          else Field(name, "bool", partial(self._bit, node, "attribute", mask),
                                     partial(self._set_attribute, node, mask)))
        fields.append(Field(self.tr("Priority"), "int", partial(getattr, node, "priority"),
                            partial(self._set_number, node, text, "priority", structure.rules.node)))
        if fsm:
            fields += [self._text(self.tr("Branch"), "bool", node, "is_branch", text),
                       self._text(self.tr("End"), "bool", node, "is_end", text)]
        if node.reference_tree is not None:
            fields.append(self._text(self.tr("Reference tree"), "text", node, "reference_tree", text))
        parts = [("fields", fields)]
        if parent is not None and not structure.is_fsm(parent):
            index = next(at for at, child in enumerate(structure.children(parent)) if child is node)
            parts.append(self._objects(
                self.tr("Entered when"), [structure.child_condition(parent, index)], "conditions", "conditions",
                single=partial(edits.set_reference, document, tree, parent, "conditions", index),
                empty=self.tr("Always: no condition")))
        if node.children or node.selector_id >= 0:
            parts.append(self._objects(
                self.tr("Selector"), [structure.selector(node)], "selectors", "selectors",
                single=None if fsm else partial(edits.set_reference, document, tree, node, "selector_id", None),
                exclude=(structure.graph["fsm_selector"],)))
        callers = "selector_callers" in authoring(state.summaries.game)["usage"]["tree"] or node.selector_callers
        if node.selector_id >= 0 and not fsm and callers:
            parts.append(self._objects(
                self.tr("Selector callers"),
                [object_ref(tree, "selector_callers", value) for value in node.selector_callers], "selector_callers",
                "selector_callers", add=partial(self._add_reference, node, "selector_callers"),
                remove=partial(edits.remove_reference, document, tree, node, "selector_callers")))
            parts.append(self._objects(
                self.tr("Callers run when"),
                [object_ref(tree, "selector_call_condition_id", node.selector_call_condition_id)], "conditions",
                "conditions", single=partial(edits.set_reference, document, tree, node, "selector_call_condition_id",
                                             None), empty=self.tr("Always: no condition")))
        if fsm:
            parts.append(self._tags(node))
        if node.children:
            parts.append(self._children(node))
        parts.append(self._objects(self.tr("Actions"), structure.actions(node), "actions", "actions",
                                   add=partial(edits.add_action, document, tree, node),
                                   remove=partial(edits.remove_action, document, tree, node),
                                   move=partial(edits.move_action, document, tree, node)))
        if fsm and parent is not None:
            parts.append(self._links(self.tr("Transitions"), structure.outgoing(node), parent, node))
        if fsm and not node.reference_tree:
            parts += [self._links(title, [Transition(kind, node, index)
                                          for index in range(len(structure.rows(kind, node)))], node, kind)
                      for kind, title in (("start", self.tr("Entry states")), ("any", self.tr("From any state")))
                      if node.children and structure.holds(node, structure.kinds[kind]["target"])]
        return self.tr("State") if fsm else self.tr("Node"), parts

    def _converts(self, node) -> bool:
        structure = self.state.structure
        parent = structure.parent.get(id(node))
        if node.children or parent is None or structure.is_fsm(parent):
            return False
        return not structure.is_fsm(node) or not (structure.outgoing(node) or any(
            structure.target(item) is node for owner in structure.nodes for item in structure.transitions(owner)))

    def _state(self, node) -> tuple[str, list[tuple]]:
        state = self.state
        structure, hfsm, document = state.structure, state.model, state.document
        parent, text = structure.parent.get(id(node)), self.tr("Edit state")
        fields = [self._text(self.tr("Name"), "text", node, "name", self.tr("Rename"))]
        fields += [Field(name, "bool", partial(self._bit, node, "attribute", mask),
                         partial(self._set_bit, node, text, "attribute", mask))
                   for name, mask in structure.graph["attributes"]["node"].items()]
        if node.children:
            fields.append(Field(self.tr("Entry state"), "enum", partial(getattr, node, "default_start_child_id"),
                                partial(self._set, node, self.tr("Set entry state"), "default_start_child_id"),
                                {child.id: child.name for child in node.children}))
        parts = [("fields", fields), self._tags(node), *([self._children(node)] if node.children else []),
                 self._objects(self.tr("Actions"), structure.actions(node), "actions", "actions",
                               add=partial(edits.add_state_action, document, hfsm, node),
                               remove=partial(edits.remove_action, document, hfsm, node),
                               move=partial(edits.move_action, document, hfsm, node))]
        if parent is not None:
            parts.append(self._links(self.tr("Transitions"), structure.outgoing(node), parent, node))
        if node.children:
            parts += [self._links(title, [Transition(kind, node, index)
                                          for index in range(len(structure.rows(kind, node)))], node, kind)
                      for kind, title in (("start", self.tr("Entry transitions")),
                                          ("any", self.tr("From any state")))]
        return self.tr("State"), parts

    def _end_name(self, transition: Transition, source: bool) -> str:
        structure = self.state.structure
        if source and transition.kind in ("start", "any"):
            return self.tr("Entry") if transition.kind == "start" else self.tr("Any state")
        node = structure.source(transition) if source else structure.target(transition)
        return "?" if node is None else self.fontMetrics().elidedText(node.name, Qt.TextElideMode.ElideMiddle, 200)

    def _pick_end(self, transition: Transition, change, _value) -> None:
        structure = self.state.structure
        container, source = structure.container(transition), change is graph_edits.set_source
        held = (structure.source if source else structure.target)(transition)
        nodes = structure.children(container) if source else structure.targets(transition.kind, container)
        self.popup.pick(node_entries(structure, [node for node in nodes if node is not held and structure.is_fsm(node)],
                                     container),
                        QCursor.pos(), self.tr("Search states"), partial(self._set_end, transition, change))

    def _set_end(self, transition: Transition, change, node) -> None:
        moved = change(self.state.document, self.state.model, transition, node)
        self.state.select([moved or transition])

    def _flag(self, transition: Transition, mask: int) -> bool:
        return bool(self.state.structure.value(transition, "attribute") & mask)

    def _set_flag(self, transition: Transition, mask: int, on: bool) -> None:
        value = self.state.structure.value(transition, "attribute")
        graph_edits.set_transition_value(self.state.document, self.state.model, transition, "attribute",
                                         value | mask if on else value & ~mask)

    def _transition(self, transition: Transition) -> tuple[str, list[tuple]]:
        state = self.state
        structure, model, document = state.structure, state.model, state.document
        graph, spec, hfsm = structure.graph, structure.kinds[transition.kind], structure.hfsm
        fields = [Field(self.tr("From"), "button" if structure.source(transition) is not None else "readonly",
                        partial(self._end_name, transition, True),
                        partial(self._pick_end, transition, graph_edits.set_source)),
                  Field(self.tr("To"), "button", partial(self._end_name, transition, False),
                        partial(self._pick_end, transition, graph_edits.set_target))]
        if structure.value(transition, "attribute") is not None:
            fields += [Field(name, "bool", partial(self._flag, transition, mask),
                             partial(self._set_flag, transition, mask))
                       for name, mask in graph["attributes"]["transition"].items()]
        identity = structure.transition_id(transition)
        if identity is not None:
            fields.append(Field(self.tr("ID"), "readonly", partial(structure.transition_id, transition)))
        record = structure.condition_record(transition)
        slots = graph["objects"] if hfsm else graph["indexes"]
        named = record is not None and structure.condition(transition) is None
        parts = [("fields", fields), self._objects(
            self.tr("Condition"), [structure.condition(transition)],
            slots["instance" if hfsm else spec["condition"]][0], "conditions",
            single=partial(graph_edits.set_condition, document, model, transition),
            skip=frozenset(graph.get("condition_record", ())),
            empty=self.tr("{0}, made by name").format(short_name(record.name or "")) if named else
            self.tr("Never taken: no condition") if hfsm else self.tr("Always: no condition"))]
        if record is not None:
            enums = graph["record_enums"]
            parts.append(("fields", [
                Field(label, "enum", partial(getattr, record, name),
                      partial(self._set, record, self.tr("Edit condition"), name),
                      {value: text for text, value in enums[name].items()}) if name in enums else
                self._text(label, "guid" if isinstance(getattr(record, name), bytes) else "bool", record, name,
                           self.tr("Edit condition"))
                for label, name in graph["condition_record"].items() if name != "uid"]))
        events = identity is not None if hfsm else structure.holds(transition.owner, spec.get("events"))
        if events:
            parts.append(self._objects(
                self.tr("Events"), structure.events(transition),
                slots["transition_events" if hfsm else spec["events"]][0], "events",
                add=partial(graph_edits.add_event, document, model, transition),
                remove=partial(graph_edits.remove_event, document, model, transition),
                move=partial(graph_edits.move_event, document, model, transition)))
        if identity and (edits.motion_entry(document, model) if hfsm else
                         edits.motion_fsm(document, model)) is not None:
            parts.append(("fields", self._blending(identity)))
        return self.tr("Transition"), parts

    def _blending(self, identity: int) -> list[Field]:
        structure = self.state.structure
        codec = (motion_fsm_v1_rules if structure.hfsm else motion_fsm_rules)(
            self.state.document.resource.version).transition_data
        games = structure.rules.games
        fields = [Field(self.tr("Blending"), "section", None),
                  Field(self.tr("Preset"), "readonly", partial(self._preset, identity),
                        tooltip=self.tr("An edit gives a transition that shares its preset one of its own"))]
        for name, value in edits.zero(codec).items():
            if name == "id":
                continue
            enum = field_enums().get(name)
            fields.append(Field(name.replace("_", " ").capitalize(),
                                "enum" if enum else "bool" if codec.limits.get(name) == (0, 1) else
                                "float" if isinstance(value, float) else "int",
                                partial(self._blend, identity, name, value),
                                partial(self._set_blend, identity, codec, name),
                                enum_names(enum, games) if enum else None))
        return fields

    def _preset(self, identity: int) -> str:
        shared = graph_edits.blending(self.state.document, self.state.model, identity)[1]
        return self.tr("Shared by {0} transitions").format(shared) if shared > 1 else self.tr("Own")

    def _blend(self, identity: int, name: str, blank):
        record = graph_edits.blending(self.state.document, self.state.model, identity)[0]
        return blank if record is None else getattr(record, name)

    def _set_blend(self, identity: int, codec, name: str, value) -> None:
        value = self._fitted(codec, name, value if isinstance(value, float) else int(value))
        graph_edits.set_blending(self.state.document, self.state.model, identity, **{name: value})

    def _variable(self, variable) -> tuple[str, list[tuple]]:
        summaries, model = self.state.summaries, self.state.model
        source = next(found[0] for found in summaries.found.values() if found[1] is variable)
        fields = [Field(self.tr("Name"), "readonly", partial(getattr, variable, "name")),
                  Field(self.tr("Type"), "readonly", partial(getattr, variable.type, "name")),
                  Field(self.tr("Value"), "readonly", partial(getattr, variable, "value"))]
        if id(source) in summaries.own:
            fields.append(Field("", "button", partial(str, self.tr("Open in the variable editor")),
                                partial(self._edit_variables, source)))
        parts = [("fields", fields)]
        nodes = [] if variable.expression is None or self.state.structure.hfsm else variable.expression.nodes
        for node, found in callbacks(model) if nodes else ():
            if any(node is item for item in nodes):
                parts.append(("rows", self.tr("Calls"), [
                    Row(short_name(summaries.type_name(*ref)), summaries.detail(*ref, 3), ref) for ref in found[:1]],
                    self.tr("No condition object"), {} if found else {
                        "add": partial(graph_edits.add_callback_object, self.state.document, model, node)}))
        return self.tr("Variable"), parts

    def _edit_variables(self, source, _value) -> None:
        self.raw_requested.emit("uvar", source)
