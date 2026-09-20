"""Lazy MOTFSM tree. Field edits and reference lookup belong to the document."""
from PySide6.QtCore import Qt, Signal, QTimer, QSignalBlocker
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTreeWidget, QTreeWidgetItem, QHeaderView,
    QMenu, QStyledItemDelegate, QComboBox, QLineEdit, QMessageBox, QInputDialog,
)

from .rsz_adapter import BLOCK_NAMES
from .formatting import value_text, rsz_value_text
from .value_editor import choices, combo, fill_combo


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
        self.rsz_field = None
        self.instance_identity = None
        self.transition_field = None
        self.transition_address = None

    def update_summary(self):
        if self.summary is not None:
            try:
                name, value = self.summary()
                self.setText(0, name)
                self.setText(1, value)
            except (ValueError, IndexError, KeyError) as exc:
                self.setText(1, f"Error: {exc}")


class FieldEditorDelegate(QStyledItemDelegate):
    @staticmethod
    def members(item):
        field = item.rsz_field or item.transition_field
        if field:
            return choices(field)
        if item.binding and item.binding.type_name == 'bool':
            return [{'name': 'True', 'value': True}, {'name': 'False', 'value': False}]
        return []

    @staticmethod
    def value(item):
        field = item.rsz_field or item.transition_field
        return field.value if field else item.binding.value

    def createEditor(self, parent, option, index):
        item = self.parent().itemFromIndex(index)
        if index.column() != 1 or not item.flags() & Qt.ItemIsEditable:
            return None
        if self.members(item):
            return combo(parent, self.members(item), self.value(item))
        return QLineEdit(parent)

    def setEditorData(self, editor, index):
        if isinstance(editor, QComboBox):
            item = self.parent().itemFromIndex(index)
            fill_combo(editor, self.members(item), self.value(item))
        elif isinstance(editor, QLineEdit):
            item = self.parent().itemFromIndex(index)
            editor.setText(MotfsmViewer._field_text(item.binding) if item.binding else
                           rsz_value_text(item.rsz_field or item.transition_field))
        else:
            super().setEditorData(editor, index)

    def setModelData(self, editor, model, index):
        value = editor.currentData() if isinstance(editor, QComboBox) else editor.text()
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
        self.tree.header().setStretchLastSection(True)
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

    def show_rsz_field(self, field):
        self._selection = ('rsz_field', field.block.name, (field.instance_index, field.path))
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
        if kind == 'rsz_field':
            instance, path = position
            field = self.handler.editor_document.rsz_field(('rsz', index, instance, *path))
            item = self._load_rsz_field(self.tree, field)
            self._expand(item)
            item.setExpanded(True)
            return
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
            item.update_summary()

    @staticmethod
    def _field_text(binding):
        return value_text(binding.name, binding.value)

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
                self._attach_reference(item, lambda index=index:
                    self.motfsm.references.object_instance(block_name, values[index]), 'rsz')
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

    def _attach_reference(self, item, resolver, kind):
        item.resolver, item.reference_kind = resolver, kind
        item.loader = self._load_reference
        _Item(item, ['Loading...', '', ''])
        name = item.text(0)
        def summary():
            target = resolver()
            label = ('None' if target is None else self._node_name(target) if kind == 'node'
                     else f'RSZ[{target.index}] {target.class_name.rsplit(".", 1)[-1]}')
            raw = self._field_text(item.binding) if item.binding else ''
            item.setToolTip(1, label)
            return name, f'{raw} → {label}' if raw else label
        item.summary = summary
        item.update_summary()

    def _load_reference(self, item):
        target = item.resolver()
        item.target = target
        if target is None:
            item.setText(1, "None")
        elif item.reference_kind == "node":
            item.setText(1, f"[{target}] {self.motfsm.get_node_by_index(target).name}")
            self._load_node(item, target)
        else:
            identity = (target.block.name, target.index)
            ancestor = item.parent()
            while ancestor:
                if ancestor.instance_identity == identity:
                    _Item(item, ['↩', f'RSZ[{target.index}] {target.class_name}', 'Reference'])
                    return
                ancestor = ancestor.parent()
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
        target = self._field(item, child, 'id_hash')
        self._attach_reference(target, lambda: self.motfsm.references.node_index(child.id_hash, child.ex_id), 'node')
        self._field(item, child, 'ex_id')
        condition = self._field(item, child, 'condition_id')
        self._attach_reference(condition, lambda: self.motfsm.references.object_instance('conditions', child.condition_id), 'rsz')

    def _load_transition(self, item, transition):
        if self.motfsm.layout.transition_event_lists is not False:
            self._list(item, "mStartTransitionEvent", transition.mStartTransitionEvent.values, "transition_events")
        else:
            self._field(item, transition.mStartTransitionEvent.values, 0, name="mStartTransitionEvent")
        target = self._field(item, transition, 'mStartState')
        self._attach_reference(target, lambda: self.motfsm.references.node_index(transition.mStartState, transition.mStartStateEx), 'node')
        condition = self._field(item, transition, 'mStartStateTransition')
        self._attach_reference(condition, lambda: self.motfsm.references.object_instance('conditions', transition.mStartStateTransition), 'rsz')
        if self.motfsm.layout.transition_state_ex:
            self._field(item, transition, "mStartStateEx")

    def _load_all_state(self, item, state):
        target = self._field(item, state, 'mAllState')
        self._attach_reference(target, lambda: self.motfsm.references.node_index(state.mAllState, state.mAllStateEx), 'node')
        condition = self._field(item, state, 'mAllTransition')
        self._attach_reference(condition, lambda: self.motfsm.references.object_instance('conditions', state.mAllTransition), 'rsz')
        self._transition_settings(item, state, 'mAllTransitionID')
        self._fields(item, state, ('mAllStateEx', 'mAllTransitionAttributes'))

    def _load_state(self, parent, state):
        self._list(parent, 'mStates · TransitionEvents', state.mStates.values, 'transition_events')
        target = self._field(parent, state, 'mTransitions')
        self._attach_reference(target, lambda: self.motfsm.references.state_target(state.mTransitions), 'node')
        condition = self._field(parent, state, 'TransitionConditions')
        self._attach_reference(condition, lambda: self.motfsm.references.object_instance('conditions', state.TransitionConditions), 'rsz')
        self._transition_settings(parent, state, 'TransitionMaps')
        self._fields(parent, state, ('mTransitionAttributes', 'mStatesEx'))

    def _transition_settings(self, parent, state, name):
        item = self._field(parent, state, name)
        address = self.handler.editor_document.address(item.binding)
        def summary():
            index, _ = self.handler.editor_document.transition_fields(address)
            return name, f'{item.binding.value} → TransitionData[{index}]' if index is not None else 'None'
        def load(root):
            index, fields = self.handler.editor_document.transition_fields(address)
            root.target = index
            for field in fields:
                child = _Item(root, [field.name, rsz_value_text(field), field.type_name])
                child.transition_address = address
                child.transition_field = field
                child.setFlags(child.flags() | Qt.ItemIsEditable)
                child.setToolTip(1, str(field.value))
        item.summary = summary
        item.loader = load
        item.resolver = lambda: self.handler.editor_document.transition_fields(address)[0]
        item.reference_kind = 'transition'
        _Item(item, ['Loading...', '', ''])
        item.update_summary()

    def _load_block(self, parent, name):
        block = self.motfsm.rsz_blocks.get_block(name)
        parent.setText(1, f"{block.instance_count} instances")
        for index in range(1, block.instance_count):
            item = self._lazy(parent, f"[{index}] {block.get_class_name(index).split('.')[-1]}",
                lambda item, index=index: self._load_instance(item, block.get_instance(index)))
            item.setText(2, block.get_class_name(index))

    def _load_instance(self, parent, instance):
        parent.instance_identity = (instance.block.name, instance.index)
        parent.setText(1, f"RSZ[{instance.index}]: {instance.class_name.split('.')[-1]}")
        parent.setText(2, instance.class_name)
        parent.setToolTip(2, instance.class_name)
        if instance.is_userdata:
            native = instance.block.file
            info = native._rsz_userdata_dict[instance.index]
            _Item(parent, ['Path', native._rsz_userdata_str_map[info], 'UserData'])
            return
        advanced = _Item(parent, ["Advanced", "", ""])
        _Item(advanced, ["Class", instance.class_name, ""])
        _Item(advanced, ["RSZ instance", instance.index, ""])
        if instance.start_offset is not None:
            _Item(advanced, ["Offset", f"0x{instance.start_offset:X}", ""])
            _Item(advanced, ["Size", instance.size, "bytes"])
        for field in instance.fields:
            target = advanced if field.name == 'v1_ID' else parent
            self._load_rsz_field(target, field)

    def _load_rsz_field(self, parent, field):
        name = f'[{field.path[-1]}]' if isinstance(field.path[-1], int) else field.name
        if field.binding:
            item = self._field(parent, field.binding.owner, field.binding.attribute, name=name)
        else:
            item = _Item(parent, [name, rsz_value_text(field), field.type_name])
            if not field.is_array and not field.children:
                item.setFlags(item.flags() | Qt.ItemIsEditable)
        item.rsz_field = field
        if item.binding:
            item.setText(1, rsz_value_text(field))
        item.setText(2, field.native_type)
        item.setToolTip(2, field.native_type)
        if field.reference:
            self._attach_reference(item, field.resolve, 'rsz')
        elif field.is_array or field.children:
            item.loader = lambda root: [self._load_rsz_field(root, child) for child in field.children]
            _Item(item, ['Loading...', '', ''])
        return item

    def _context_menu(self, position):
        item = self.tree.itemAt(position)
        if item is None:
            return
        menu = QMenu(self.tree)
        if item.childCount():
            action = menu.addAction("折叠" if item.isExpanded() else "展开")
            action.triggered.connect(lambda: item.setExpanded(not item.isExpanded()))
        if item.flags() & Qt.ItemIsEditable:
            menu.addAction("编辑").triggered.connect(lambda: self._start_edit(item))
        array = item if item.rsz_field and item.rsz_field.is_array else item.parent()
        if isinstance(array, _Item) and array.rsz_field and array.rsz_field.is_array:
            field = array.rsz_field
            menu.addAction('添加元素').triggered.connect(lambda: self._change_array(field, 'add'))
            if item is not array and item.rsz_field:
                index = item.rsz_field.path[-1]
                menu.addAction('复制元素').triggered.connect(lambda: self._change_array(field, 'duplicate', index))
                menu.addAction('删除元素').triggered.connect(lambda: self._change_array(field, 'remove', index))
        if item.action_node_index is not None:
            node_index, action_index = item.action_node_index, item.action_index
            menu.addAction("添加已有 Action 引用").triggered.connect(lambda: self._add_action(node_index))
            if action_index is not None:
                menu.addAction("删除 Action 引用").triggered.connect(
                    lambda: self._remove_action(node_index, action_index))
        if not menu.isEmpty():
            menu.exec(self.tree.viewport().mapToGlobal(position))

    def _change_array(self, field, operation, position=None):
        try:
            self.handler.editor_document.change_array(field, operation, position)
        except (ValueError, IndexError, KeyError) as exc:
            QMessageBox.warning(self, 'Array', str(exc))

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
        if column != 1:
            return
        if item.transition_field:
            address, name, text = item.transition_address, item.transition_field.name, item.text(1)
            QTimer.singleShot(0, lambda: self._edit_transition(address, name, text))
            return
        if item.binding is None:
            if item.rsz_field and item.flags() & Qt.ItemIsEditable:
                field, text = item.rsz_field, item.text(1)
                QTimer.singleShot(0, lambda: self._edit_rsz_value(field, text))
            return
        try:
            self.handler.edit_field(item.binding, item.text(1))
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid value", str(exc))
        with QSignalBlocker(self.tree):
            item.setText(1, rsz_value_text(item.rsz_field) if item.rsz_field else self._field_text(item.binding))
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()

    def _edit_rsz_value(self, field, text):
        try:
            self.handler.editor_document.edit_rsz_field(field, text)
        except (ValueError, IndexError, KeyError) as exc:
            QMessageBox.warning(self, 'Invalid value', str(exc))
            self._reload_tree()

    def _edit_transition(self, address, name, text):
        try:
            self.handler.editor_document.edit_transition(address, name, text)
        except (ValueError, IndexError, KeyError) as exc:
            QMessageBox.warning(self, 'Transition data', str(exc))
            self._reload_tree()

    def _refresh_aliases(self):
        # Traverse current items rather than retaining pointers to deleted Qt rows.
        with QSignalBlocker(self.tree):
            pending = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
            while pending:
                item = pending.pop()
                item.update_summary()
                if item.binding is not None and item.summary is None:
                    item.setText(1, rsz_value_text(item.rsz_field) if item.rsz_field else self._field_text(item.binding))
                if item.resolver is not None and item.loaded:
                    try:
                        target = item.resolver()
                        changed = target != item.target if item.reference_kind in ('node', 'transition') else target is not item.target
                    except (ValueError, IndexError, KeyError):
                        changed = True
                    if changed:
                        if self._editing_item is not None:
                            self.tree.closePersistentEditor(self._editing_item, 1)
                            self._editing_item = None
                        item.loaded = False
                        self._expand(item)
                pending.extend(item.child(i) for i in range(item.childCount()))
