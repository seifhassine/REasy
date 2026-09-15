"""Lazy MOTFSM tree. Field edits and reference lookup belong to the document."""
from PySide6.QtCore import Qt, Signal, QTimer, QSignalBlocker
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTreeWidget, QTreeWidgetItem, QHeaderView,
    QMenu, QStyledItemDelegate, QComboBox, QLineEdit, QMessageBox,
)

from .rsz_adapter import BLOCK_NAMES


class _Item(QTreeWidgetItem):
    def __init__(self, parent, columns):
        super().__init__(parent, [str(value) for value in columns])
        self.binding = None
        self.loader = None
        self.loaded = False
        self.resolver = None
        self.reference_kind = None
        self.target = None
        self.summary = None

    def update_summary(self):
        if self.summary is not None:
            try:
                name, value = self.summary()
                self.setText(0, name)
                self.setText(1, value)
            except (ValueError, IndexError, KeyError) as exc:
                self.setText(1, f"Error: {exc}")


class FieldEditorDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        item = self.parent().itemFromIndex(index)
        if index.column() != 1 or item.binding is None:
            return None
        if item.binding.type_name == "bool":
            editor = QComboBox(parent)
            editor.addItems(["True", "False"])
            return editor
        return QLineEdit(parent)

    def setEditorData(self, editor, index):
        if isinstance(editor, QComboBox):
            editor.setCurrentText(index.data(Qt.DisplayRole))
        else:
            super().setEditorData(editor, index)

    def setModelData(self, editor, model, index):
        value = editor.currentText() if isinstance(editor, QComboBox) else editor.text()
        model.setData(index, value, Qt.EditRole)


class MotfsmViewer(QWidget):
    modified_changed = Signal(bool)

    def __init__(self, handler):
        super().__init__()
        self.handler = handler
        self.motfsm = handler.motfsm
        self._editing_item = None
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(0)
        self._refresh_timer.timeout.connect(self._refresh_aliases)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Value", "Type"])
        self.tree.setColumnWidth(0, 500)
        self.tree.setColumnWidth(1, 300)
        self.tree.header().setSectionResizeMode(QHeaderView.Interactive)
        self.tree.header().setStretchLastSection(False)
        self.tree.setItemDelegateForColumn(1, FieldEditorDelegate(self.tree))
        self.tree.setEditTriggers(QTreeWidget.NoEditTriggers)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.itemExpanded.connect(self._expand)
        self.tree.itemChanged.connect(self._item_changed)
        layout.addWidget(self.tree)
        self.handler.modified_changed.connect(self.modified_changed.emit)
        with QSignalBlocker(self.tree):
            root = _Item(self.tree, ["BHVT", "", "Structure"])
            self._lazy(root, f"Nodes ({self.motfsm.node_count})", self._load_nodes)
            blocks = _Item(root, ["RSZ Blocks", "", ""])
            for name in BLOCK_NAMES:
                label = ''.join(word.title() for word in name.split('_'))
                self._lazy(blocks, label, lambda item, name=name: self._load_block(item, name))
            root.setExpanded(True)

    @property
    def modified(self):
        return self.handler.modified

    @modified.setter
    def modified(self, value):
        self.handler.modified = value

    def _lazy(self, parent, name, loader):
        item = _Item(parent, [name, "", ""])
        item.loader = loader
        _Item(item, ["Loading...", "", ""])
        return item

    def _expand(self, item):
        if item.loader is None or item.loaded:
            return
        with QSignalBlocker(self.tree):
            item.takeChildren()
            try:
                item.loader(item)
            except (ValueError, IndexError, KeyError) as exc:
                _Item(item, ["Error", str(exc), ""])
            item.loaded = True

    @staticmethod
    def _field_text(binding):
        value = binding.value
        if binding.type_name.startswith("u"):
            return f"0x{value:X}"
        return str(value)

    def _field(self, parent, owner, attribute, *, name=None):
        binding = self.motfsm.bindings.get(owner, attribute)
        if binding is None:
            value = owner[attribute] if isinstance(attribute, int) else getattr(owner, attribute)
            return _Item(parent, [name or attribute, value, ""])
        item = _Item(parent, [name or binding.name, self._field_text(binding), binding.type_name])
        item.binding = binding
        item.setFlags(item.flags() | Qt.ItemIsEditable)
        item.setToolTip(0, f"Offset: 0x{binding.offset:X}, Size: {binding.size}")
        return item

    def _fields(self, parent, owner, names):
        for name in names:
            self._field(parent, owner, name)

    def _list(self, parent, name, values, block_name=None):
        group = _Item(parent, [f"{name} ({len(values)})", "", ""])
        for index in range(len(values)):
            item = self._field(group, values, index, name=f"[{index}]")
            if block_name:
                self._object_link(item, block_name, lambda index=index: values[index])
        return group

    def _link(self, parent, name, resolver, kind):
        item = self._lazy(parent, name, self._load_reference)
        item.resolver = resolver
        item.reference_kind = kind
        return item

    def _node_link(self, parent, owner, hash_name, ex_name):
        return self._link(parent, "→ View Target Node", lambda: self.motfsm.references.node_index(
            getattr(owner, hash_name), getattr(owner, ex_name)), "node")

    def _object_link(self, parent, block_name, getter):
        return self._link(parent, "→ View " + block_name, lambda:
            self.motfsm.references.object_instance(block_name, getter()), "rsz")

    def _load_reference(self, item):
        target = item.resolver()
        item.target = target
        if target is None:
            item.setText(1, "None")
        elif item.reference_kind == "node":
            item.setText(1, f"[{target}] {self.motfsm.get_node_by_index(target).name}")
            self._load_node(item, target)
        else:
            self._load_instance(item, target)

    def _load_nodes(self, parent):
        for index, node in enumerate(self.motfsm.bhvt.nodes):
            item = self._lazy(parent, f"[{index}] {node.name}", lambda item, index=index: self._load_node(item, index))
            item.summary = lambda node=node, index=index: (
                f"[{index}] {node.name}", f"hash=0x{node.id_hash:08X}, ex={node.ex_id}")
            item.update_summary()

    def _node_name(self, index):
        return self.motfsm.get_node_by_index(index).name if index is not None else "None"

    def _action_name(self, action):
        instance = self.motfsm.references.action(action.id_hash, action.ex_id)
        return instance.class_name.split('.')[-1] if instance is not None else "None"

    def _load_node(self, parent, index):
        node = self.motfsm.get_node_by_index(index)
        self._field(parent, node, "id_hash")
        details = _Item(parent, ["Node Details", "", ""])
        self._fields(details, node, ("ex_id", "name", "parent", "parent_ex", "priority",
            "node_attribute", "work_flags", "is_fsm", "has_reference_tree", "reference_tree_index"))
        self._node_link(details, node, "parent", "parent_ex")
        if node.children:
            children = _Item(parent, [f"Children ({len(node.children)})", "", ""])
            for i, child in enumerate(node.children):
                item = _Item(children, [f"[{i}]", "", "ChildNode"])
                item.summary = lambda child=child, i=i: (f"[{i}] " + self._node_name(
                    self.motfsm.references.node_index(child.id_hash, child.ex_id)), "")
                item.update_summary()
                self._fields(item, child, ("id_hash", "ex_id", "condition_id"))
                self._node_link(item, child, "id_hash", "ex_id")
                self._object_link(item, "conditions", lambda child=child: child.condition_id)
        if node.actions:
            actions = _Item(parent, [f"Actions ({len(node.actions)})", "", ""])
            for i, action in enumerate(node.actions):
                item = _Item(actions, [f"[{i}]", "", "Action"])
                item.summary = lambda action=action, i=i: (f"[{i}] {self._action_name(action)}", "")
                item.update_summary()
                self._fields(item, action, ("id_hash", "ex_id"))
                self._link(item, "RSZ Instance Details", lambda action=action:
                    self.motfsm.references.action(action.id_hash, action.ex_id), "rsz")
        selector = _Item(parent, ["Selector", "", ""])
        self._fields(selector, node, ("selector_id", "selector_caller_condition_id"))
        self._object_link(selector, "selectors", lambda: node.selector_id)
        self._object_link(selector, "selectors", lambda: node.selector_caller_condition_id)
        self._list(selector, "SelectorCallers", node.selector_callers, "selector_callers")
        if node.states:
            states = _Item(parent, [f"States ({len(node.states)})", "", ""])
            for i, state in enumerate(node.states):
                item = self._lazy(states, f"[{i}]", lambda item, state=state: self._load_state(item, state))
                item.summary = lambda state=state, i=i: (f"[{i}] → " + self._node_name(
                    self.motfsm.references.state_target(state.mTransitions)), "")
                item.update_summary()
        if node.transitions:
            transitions = _Item(parent, [f"Transitions ({len(node.transitions)})", "", ""])
            for i, transition in enumerate(node.transitions):
                item = _Item(transitions, [f"[{i}]", "", "Transition"])
                item.summary = lambda transition=transition, i=i: (f"[{i}] → " + self._node_name(
                    self.motfsm.references.node_index(transition.mStartState, transition.mStartStateEx)), "")
                item.update_summary()
                if self.motfsm.layout.transition_event_lists is not False:
                    self._list(item, "mStartTransitionEvent", transition.mStartTransitionEvent.values, "transition_events")
                else:
                    self._field(item, transition.mStartTransitionEvent.values, 0, name="mStartTransitionEvent")
                self._fields(item, transition, ("mStartState", "mStartStateTransition"))
                if self.motfsm.layout.transition_state_ex:
                    self._field(item, transition, "mStartStateEx")
                self._node_link(item, transition, "mStartState", "mStartStateEx")
                self._object_link(item, "conditions", lambda transition=transition: transition.mStartStateTransition)
        if node.is_fsm:
            fsm = _Item(parent, ["FSM Fields", "", ""])
            self._fields(fsm, node, ("name_hash", "fullname_hash", "is_branch", "is_end"))
            self._list(fsm, "Tags", node.tags)
        if node.all_states:
            all_states = _Item(parent, [f"AllStates ({len(node.all_states)})", "", ""])
            for i, state in enumerate(node.all_states):
                item = _Item(all_states, [f"[{i}]", "", "AllState"])
                self._fields(item, state, ("mAllState", "mAllTransition", "mAllTransitionID",
                                           "mAllStateEx", "mAllTransitionAttributes"))

    def _load_state(self, parent, state):
        self._list(parent, "mStates", state.mStates.values, "transition_events")
        self._fields(parent, state, ("mTransitions", "TransitionConditions", "TransitionMaps",
                                    "mTransitionAttributes", "mStatesEx"))
        self._link(parent, "→ View Target Node", lambda:
            self.motfsm.references.state_target(state.mTransitions), "node")
        self._object_link(parent, "conditions", lambda: state.TransitionConditions)

    def _load_block(self, parent, name):
        block = self.motfsm.rsz_blocks.get_block(name)
        parent.setText(1, f"{block.instance_count} instances")
        for index in range(1, block.instance_count):
            item = self._lazy(parent, f"[{index}] {block.get_class_name(index).split('.')[-1]}",
                lambda item, index=index: self._load_instance(item, block.get_instance(index)))
            item.setText(2, block.get_class_name(index))

    def _load_instance(self, parent, instance):
        parent.setText(1, f"RSZ[{instance.index}]: {instance.class_name.split('.')[-1]}")
        parent.setText(2, instance.class_name)
        _Item(parent, ["Class", instance.class_name, ""])
        if instance.is_userdata:
            _Item(parent, ["Type", "UserData (external)", ""])
            return
        if instance.start_offset is not None:
            _Item(parent, ["Offset", f"0x{instance.start_offset:X}", ""])
            _Item(parent, ["Size", instance.size, "bytes"])
        fields = _Item(parent, [f"Fields ({len(instance.fields)})", "", ""])
        for field in instance.fields:
            if field.binding is not None:
                self._field(fields, field.binding.owner, field.binding.attribute, name=field.name)
            else:
                item = _Item(fields, [field.name, field.value, field.type_name])
                item.setToolTip(0, f"Offset: 0x{field.offset:X}, Size: {field.size}")

    def _context_menu(self, position):
        item = self.tree.itemAt(position)
        if item is None:
            return
        menu = QMenu(self.tree)
        if item.childCount():
            action = menu.addAction("折叠" if item.isExpanded() else "展开")
            action.triggered.connect(lambda: item.setExpanded(not item.isExpanded()))
        if item.binding is not None:
            menu.addAction("编辑").triggered.connect(lambda: self._start_edit(item))
        if not menu.isEmpty():
            menu.exec(self.tree.viewport().mapToGlobal(position))

    def _start_edit(self, item):
        if self._editing_item is not None:
            self.tree.closePersistentEditor(self._editing_item, 1)
        self._editing_item = item
        self.tree.openPersistentEditor(item, 1)

    def _item_changed(self, item, column):
        if column != 1 or item.binding is None:
            return
        try:
            self.handler.edit_field(item.binding, item.text(1))
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid value", str(exc))
        with QSignalBlocker(self.tree):
            item.setText(1, self._field_text(item.binding))
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def _refresh_aliases(self):
        # Traverse current items rather than retaining pointers to deleted Qt rows.
        with QSignalBlocker(self.tree):
            pending = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
            while pending:
                item = pending.pop()
                item.update_summary()
                if item.binding is not None:
                    item.setText(1, self._field_text(item.binding))
                if item.resolver is not None and item.loaded:
                    try:
                        target = item.resolver()
                        changed = target != item.target if item.reference_kind == "node" else target is not item.target
                    except (ValueError, IndexError, KeyError):
                        changed = True
                    if changed:
                        if self._editing_item is not None:
                            self.tree.closePersistentEditor(self._editing_item, 1)
                            self._editing_item = None
                        item.loaded = False
                        self._expand(item)
                pending.extend(item.child(i) for i in range(item.childCount()))
