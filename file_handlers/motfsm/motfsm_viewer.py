"""Lazy MOTFSM tree. Field edits and reference lookup belong to the document."""
from PySide6.QtCore import Qt, Signal, QTimer, QSignalBlocker
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTreeWidget, QTreeWidgetItem, QHeaderView,
    QMenu, QStyledItemDelegate, QComboBox, QLineEdit, QMessageBox, QInputDialog,
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
        self.action_node_index = None
        self.action_index = None

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

    def __init__(self, handler, *, selection_only=False):
        super().__init__()
        self.handler = handler
        self.motfsm = handler.motfsm
        self.selection_only = selection_only
        self._selection = None
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
        self.tree.setItemDelegate(FieldEditorDelegate(self.tree))
        self.tree.setEditTriggers(QTreeWidget.DoubleClicked | QTreeWidget.SelectedClicked | QTreeWidget.EditKeyPressed)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.itemExpanded.connect(self._expand)
        self.tree.itemChanged.connect(self._item_changed)
        layout.addWidget(self.tree)
        self.handler.modified_changed.connect(self.modified_changed.emit)
        self.handler.document_changed.connect(self._reload_tree)
        self.handler.field_changed.connect(lambda _: self._refresh_timer.start())
        self._build_tree()

    def _build_tree(self):
        with QSignalBlocker(self.tree):
            if self.selection_only:
                self._build_selection()
                return
            root = _Item(self.tree, ["BHVT", "", "Structure"])
            self._lazy(root, f"Nodes ({self.motfsm.node_count})", self._load_nodes)
            blocks = _Item(root, ["RSZ Blocks", "", ""])
            for name in BLOCK_NAMES:
                label = ''.join(word.title() for word in name.split('_'))
                self._lazy(blocks, label, lambda item, name=name: self._load_block(item, name))
            root.setExpanded(True)

    def show_node(self, index):
        self._selection = ('node', index, None)
        self._show_selection()

    def show_edge(self, edge):
        self._selection = (edge.kind, edge.source, edge.position)
        self._show_selection()

    def _show_selection(self):
        self._refresh_timer.stop()
        self._editing_item = None
        with QSignalBlocker(self.tree):
            self.tree.clear()
            self._build_selection()

    def _build_selection(self):
        if self._selection is None:
            return
        kind, index, position = self._selection
        node = self.motfsm.get_node_by_index(index)
        root = _Item(self.tree, [f'[{index}] {node.name}', '', 'Node' if kind == 'node' else kind])
        if kind == 'node':
            self._load_node(root, index)
        elif kind == 'child':
            self._load_child(root, node.children[position])
        elif kind == 'state':
            self._load_state(root, node.states[position])
        elif kind == 'start':
            self._load_transition(root, node.transitions[position])
        elif kind == 'all':
            self._load_all_state(root, node.all_states[position])
        root.setExpanded(True)

    def _reload_tree(self):
        expanded = []
        selected = self.tree.currentItem()
        selected_path = []
        while selected is not None:
            parent = selected.parent() or self.tree.invisibleRootItem()
            selected_path.append(parent.indexOfChild(selected))
            selected = selected.parent()
        horizontal = self.tree.horizontalScrollBar().value()
        vertical = self.tree.verticalScrollBar().value()
        def remember(parent, path):
            for i in range(parent.childCount()):
                item = parent.child(i)
                child_path = (*path, i)
                if item.isExpanded():
                    expanded.append(child_path)
                remember(item, child_path)
        remember(self.tree.invisibleRootItem(), ())
        self._refresh_timer.stop()
        self._editing_item = None
        self.motfsm = self.handler.motfsm
        with QSignalBlocker(self.tree):
            self.tree.clear()
            self._build_tree()
            for path in expanded:
                item = self.tree.invisibleRootItem()
                for i in path:
                    if isinstance(item, _Item):
                        self._expand(item)
                    if i >= item.childCount():
                        break
                    item = item.child(i)
                else:
                    self._expand(item)
                    item.setExpanded(True)
            if selected_path:
                item = self.tree.invisibleRootItem()
                for i in reversed(selected_path):
                    if i >= item.childCount():
                        break
                    item = item.child(i)
                if isinstance(item, _Item):
                    self.tree.setCurrentItem(item)
        self.tree.horizontalScrollBar().setValue(horizontal)
        self.tree.verticalScrollBar().setValue(vertical)

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
        if hash_name == 'parent':
            return self._link(parent, "→ View Parent Node", lambda: self.motfsm.references.parent_index(owner), "node")
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
        self._fields(parent, node, ("name", "priority", "work_flags"))
        details = _Item(parent, ["Advanced", "", ""])
        self._fields(details, node, ("id_hash", "ex_id", "parent", "parent_ex",
            "node_attribute", "is_fsm", "has_reference_tree", "reference_tree_index"))
        self._node_link(details, node, "parent", "parent_ex")
        if node.children:
            children = _Item(parent, [f"Children ({len(node.children)})", "", ""])
            for i, child in enumerate(node.children):
                item = _Item(children, [f"[{i}]", "", "ChildNode"])
                item.summary = lambda child=child, i=i: (f"[{i}] " + self._node_name(
                    self.motfsm.references.node_index(child.id_hash, child.ex_id)), "")
                item.update_summary()
                self._load_child(item, child)
        actions = _Item(parent, [f"Actions ({len(node.actions)})", "", ""])
        actions.action_node_index = index
        for i, action in enumerate(node.actions):
            item = _Item(actions, [f"[{i}]", "", "Action"])
            item.action_node_index = index
            item.action_index = i
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
                self._load_transition(item, transition)
        if node.is_fsm:
            fsm = _Item(parent, ["FSM Fields", "", ""])
            self._fields(fsm, node, ("name_hash", "fullname_hash", "is_branch", "is_end"))
            self._list(fsm, "Tags", node.tags)
        if node.all_states:
            all_states = _Item(parent, [f"AllStates ({len(node.all_states)})", "", ""])
            for i, state in enumerate(node.all_states):
                item = _Item(all_states, [f"[{i}]", "", "AllState"])
                self._load_all_state(item, state)

    def _load_child(self, item, child):
        self._fields(item, child, ("id_hash", "ex_id", "condition_id"))
        self._node_link(item, child, "id_hash", "ex_id")
        self._object_link(item, "conditions", lambda: child.condition_id)

    def _load_transition(self, item, transition):
        if self.motfsm.layout.transition_event_lists is not False:
            self._list(item, "mStartTransitionEvent", transition.mStartTransitionEvent.values, "transition_events")
        else:
            self._field(item, transition.mStartTransitionEvent.values, 0, name="mStartTransitionEvent")
        self._fields(item, transition, ("mStartState", "mStartStateTransition"))
        if self.motfsm.layout.transition_state_ex:
            self._field(item, transition, "mStartStateEx")
        self._node_link(item, transition, "mStartState", "mStartStateEx")
        self._object_link(item, "conditions", lambda: transition.mStartStateTransition)

    def _load_all_state(self, item, state):
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
        if instance.is_userdata:
            _Item(parent, ["Type", "UserData (external)", ""])
            return
        advanced = _Item(parent, ["Advanced", "", ""])
        _Item(advanced, ["Class", instance.class_name, ""])
        _Item(advanced, ["RSZ instance", instance.index, ""])
        if instance.start_offset is not None:
            _Item(advanced, ["Offset", f"0x{instance.start_offset:X}", ""])
            _Item(advanced, ["Size", instance.size, "bytes"])
        for field in instance.fields:
            target = advanced if field.name == 'v1_ID' else parent
            if field.binding is not None:
                self._field(target, field.binding.owner, field.binding.attribute, name=field.name)
            else:
                item = _Item(target, [field.name, field.value, field.type_name])
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
        if item.action_node_index is not None:
            node_index, action_index = item.action_node_index, item.action_index
            menu.addAction("添加已有 Action 引用").triggered.connect(lambda: self._add_action(node_index))
            if action_index is not None:
                menu.addAction("删除 Action 引用").triggered.connect(
                    lambda: self._remove_action(node_index, action_index))
        if not menu.isEmpty():
            menu.exec(self.tree.viewport().mapToGlobal(position))

    def _add_action(self, node_index):
        try:
            choices = {}
            for (id_hash, ex_id), (block_name, instance_index) in self.motfsm.references.action_identities().items():
                block = self.motfsm.rsz_blocks.get_block(block_name)
                label = f'{block.get_class_name(instance_index)} | 0x{id_hash:08X}, ex={ex_id} | {block_name}[{instance_index}]'
                choices[label] = (id_hash, ex_id)
            text, accepted = QInputDialog.getItem(self, '添加已有 Action 引用', 'Action',
                                                sorted(choices), 0, True)
            if accepted:
                if text not in choices:
                    raise ValueError('Select an existing Action from the list')
                self.handler.add_action_reference(node_index, *choices[text])
        except (ValueError, IndexError, KeyError) as exc:
            QMessageBox.warning(self, 'Action reference', str(exc))

    def _remove_action(self, node_index, action_index):
        try:
            self.handler.remove_action_reference(node_index, action_index)
        except (ValueError, IndexError, KeyError) as exc:
            QMessageBox.warning(self, 'Action reference', str(exc))

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
