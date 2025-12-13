"""
MOTFSM Viewer - UI for displaying FSM file contents with lazy loading.
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTreeWidget,
    QTreeWidgetItem, QHeaderView, QMenu,
    QStyledItemDelegate, QComboBox, QLineEdit
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIntValidator, QDoubleValidator

from file_handlers.motfsm.rsz_parser import RSZInstance, RSZFieldValue


class FieldEditorDelegate(QStyledItemDelegate):
    """Custom delegate to provide type-specific editors for fields"""

    def createEditor(self, parent, option, index):
        """Create appropriate editor based on field type"""
        # Get the item's metadata
        tree = parent.parent()  # Get the tree widget
        item = tree.itemFromIndex(index)

        if not item:
            return super().createEditor(parent, option, index)

        # Only create custom editor for Value column (column 1)
        if index.column() != 1:
            return super().createEditor(parent, option, index)

        data = item.data(0, Qt.UserRole)
        if not data or not isinstance(data, dict):
            return super().createEditor(parent, option, index)

        field_type = data.get("field_type")

        if field_type == "bool":
            # Boolean: use combo box with True/False
            combo = QComboBox(parent)
            combo.addItems(["True", "False"])
            return combo

        elif field_type in ("uint32", "uint16", "uint8", "uint64"):
            # Unsigned integer: line edit with custom validator for hex/decimal
            editor = QLineEdit(parent)
            editor.setPlaceholderText("Enter hex (0x...) or decimal")
            # Note: We'll validate in setModelData instead of using QValidator
            # because we need to support both hex and decimal
            return editor

        elif field_type in ("int32", "int16", "int8"):
            # Signed integer: line edit with int validator
            editor = QLineEdit(parent)
            editor.setPlaceholderText("Enter decimal number")

            # Set range based on type
            bits = 8 if "8" in field_type else 16 if "16" in field_type else 32
            min_val = -(1 << (bits - 1))
            max_val = (1 << (bits - 1)) - 1

            validator = QIntValidator(min_val, max_val, editor)
            editor.setValidator(validator)
            return editor

        elif field_type in ("float", "double"):
            # Float/double: line edit with double validator
            editor = QLineEdit(parent)
            editor.setPlaceholderText("Enter decimal number")
            validator = QDoubleValidator(editor)
            editor.setValidator(validator)
            return editor

        else:
            # String or unknown: default line edit
            return super().createEditor(parent, option, index)

    def setEditorData(self, editor, index):
        """Set initial value in editor"""
        if isinstance(editor, QComboBox):
            # For boolean combo box
            current_text = index.data(Qt.DisplayRole)
            # Handle various boolean representations
            if current_text.lower() in ("true", "1", "yes"):
                editor.setCurrentText("True")
            else:
                editor.setCurrentText("False")
        else:
            # For line edits, use default behavior
            super().setEditorData(editor, index)

    def setModelData(self, editor, model, index):
        """Get value from editor and set it in model"""
        if isinstance(editor, QComboBox):
            # For boolean combo box
            value = editor.currentText()
            model.setData(index, value, Qt.EditRole)
        elif isinstance(editor, QLineEdit):
            # For line edits, validate and set
            value = editor.text()
            model.setData(index, value, Qt.EditRole)
        else:
            super().setModelData(editor, model, index)


class MotfsmViewer(QWidget):
    """Viewer widget for MOTFSM files with lazy loading tree"""
    modified_changed = Signal(bool)

    def __init__(self, handler):
        super().__init__()
        self.handler = handler
        self.motfsm = handler.motfsm
        self._modified = False

        # Track expanded items to avoid re-parsing
        self._expanded_items = set()

        # Flag to prevent triggering itemChanged during programmatic updates
        self._updating_tree = False

        # Track currently editing item
        self._editing_item = None

        self._setup_ui()
        self._populate_tree()

    @property
    def modified(self):
        return self._modified

    @modified.setter
    def modified(self, value: bool):
        if self._modified != value:
            self._modified = value
            self.modified_changed.emit(value)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Single tree view - matching native RSZ viewer pattern
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Value", "Type"])

        # Set column widths and make them resizable by user
        self.tree.setColumnWidth(0, 500)
        self.tree.setColumnWidth(1, 300)

        # Allow user to resize all columns
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        header.setSectionResizeMode(2, QHeaderView.Interactive)
        header.setStretchLastSection(False)

        # Install custom delegate for type-specific editors
        self.field_delegate = FieldEditorDelegate(self.tree)
        self.tree.setItemDelegateForColumn(1, self.field_delegate)  # Value column

        # Enable context menu (right-click menu)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)

        # Disable default edit triggers - use context menu instead
        self.tree.setEditTriggers(QTreeWidget.NoEditTriggers)

        # Connect to item clicked to close any open editors
        self.tree.itemClicked.connect(self._on_item_clicked)

        self.tree.itemExpanded.connect(self._on_item_expanded)
        self.tree.itemChanged.connect(self._on_item_changed)

        layout.addWidget(self.tree)

    def _populate_tree(self):
        """Populate tree with MOTFSM structure"""
        self.tree.clear()

        if not self.motfsm:
            return

        # Add BHVT section (main structure)
        if self.motfsm.bhvt:
            bhvt_item = QTreeWidgetItem(self.tree, ["BHVT", "", "Structure"])

            # Add Nodes (lazy loaded)
            nodes_item = QTreeWidgetItem(bhvt_item, [
                f"Nodes ({self.motfsm.node_count})", "", "BHVTNode[]"
            ])
            nodes_item.setData(0, Qt.UserRole, {"type": "nodes_list"})
            # Add placeholder for lazy loading
            QTreeWidgetItem(nodes_item, ["Loading...", "", ""])

            # Add RSZ Blocks (lazy loaded)
            rsz_item = QTreeWidgetItem(bhvt_item, ["RSZ Blocks", "", ""])
            self._add_rsz_block_stubs(rsz_item)

            bhvt_item.setExpanded(True)

    def _create_editable_item(self, parent: QTreeWidgetItem, field_name: str,
                             field_value, field_type: str, field_owner=None) -> QTreeWidgetItem:
        """Create an editable tree widget item with metadata

        Args:
            parent: Parent tree item
            field_name: Display name of the field
            field_value: Current value of the field
            field_type: Type string (uint32, string, bool, etc.)
            field_owner: Object that owns this field (for setattr)

        Returns:
            Created QTreeWidgetItem
        """
        # Format the value for display
        if field_type in ("uint32", "uint16", "uint8", "uint64") and isinstance(field_value, int):
            value_str = f"0x{field_value:08X}" if field_type == "uint32" else f"0x{field_value:X}"
        else:
            value_str = str(field_value) if field_value is not None else ""

        item = QTreeWidgetItem(parent, [field_name, value_str, field_type])

        # Make Value column (column 1) editable and store metadata for editing
        if field_owner is not None:
            # Add ItemIsEditable flag while preserving other default flags (including expansion)
            item.setFlags(item.flags() | Qt.ItemIsEditable)

            item.setData(0, Qt.UserRole, {
                "editable": True,
                "field_name": field_name,
                "field_type": field_type,
                "field_owner": field_owner,
                "old_value": field_value
            })

        return item

    def _add_header_info(self, parent: QTreeWidgetItem):
        """Add header fields to tree"""
        QTreeWidgetItem(parent, ["Version", str(self.motfsm.version), "uint32"])
        QTreeWidgetItem(parent, ["Magic", f"0x{self.motfsm.magic:08X}", "uint32"])
        QTreeWidgetItem(parent, ["TreeDataOffset", f"0x{self.motfsm.tree_data_offset:X}", "uint64"])
        QTreeWidgetItem(parent, ["TransitionMapTblOffset", f"0x{self.motfsm.transition_map_tbl_offset:X}", "uint64"])
        QTreeWidgetItem(parent, ["TransitionDataTblOffset", f"0x{self.motfsm.transition_data_tbl_offset:X}", "uint64"])
        QTreeWidgetItem(parent, ["TreeInfoPtr", f"0x{self.motfsm.tree_info_ptr:X}", "uint64"])
        QTreeWidgetItem(parent, ["TransitionMapCount", str(self.motfsm.transition_map_count), "uint32"])
        QTreeWidgetItem(parent, ["TransitionDataCount", str(self.motfsm.transition_data_count), "uint32"])
        QTreeWidgetItem(parent, ["StartTransitionDataIndex", str(self.motfsm.start_transition_data_index), "uint32"])
        QTreeWidgetItem(parent, ["TreeDataSize", str(self.motfsm.tree_data_size), "uint32"])

    def _add_bhvt_offsets(self, parent: QTreeWidgetItem):
        """Add BHVT offset fields"""
        bhvt = self.motfsm.bhvt
        offsets_item = QTreeWidgetItem(parent, ["Offsets", "", ""])

        offset_fields = [
            ("NodeOffset", bhvt.node_offset),
            ("ActionOffset", bhvt.action_offset),
            ("SelectorOffset", bhvt.selector_offset),
            ("SelectorCallerOffset", bhvt.selector_caller_offset),
            ("ConditionsOffset", bhvt.conditions_offset),
            ("TransitionEventOffset", bhvt.transition_event_offset),
            ("ExpressionTreeConditionsOffset", bhvt.expression_tree_conditions_offset),
            ("StaticActionOffset", bhvt.static_action_offset),
            ("StaticSelectorCallerOffset", bhvt.static_selector_caller_offset),
            ("StaticConditionsOffset", bhvt.static_conditions_offset),
            ("StaticTransitionEventOffset", bhvt.static_transition_event_offset),
            ("StaticExpressionTreeConditionsOffset", bhvt.static_expression_tree_conditions_offset),
            ("StringOffset", bhvt.string_offset),
            ("ResourcePathsOffset", bhvt.resource_paths_offset),
            ("UserDataPathsOffset", bhvt.userdata_paths_offset),
            ("VariableOffset", bhvt.variable_offset),
            ("BaseVariableOffset", bhvt.base_variable_offset),
            ("ReferencePrefabGameObjectsOffset", bhvt.reference_prefab_game_objects_offset),
        ]

        for name, offset in offset_fields:
            QTreeWidgetItem(offsets_item, [name, f"0x{offset:X}", "uint64"])

    def _add_rsz_block_stubs(self, parent: QTreeWidgetItem):
        """Add RSZ block stubs for lazy loading"""
        blocks = [
            ("Actions", "actions"),
            ("Selectors", "selectors"),
            ("SelectorCallers", "selector_callers"),
            ("Conditions", "conditions"),
            ("TransitionEvents", "transition_events"),
            ("ExpressionTreeConditions", "expression_tree_conditions"),
            ("StaticActions", "static_actions"),
            ("StaticSelectorCallers", "static_selector_callers"),
            ("StaticConditions", "static_conditions"),
            ("StaticTransitionEvents", "static_transition_events"),
            ("StaticExpressionTreeConditions", "static_expression_tree_conditions"),
        ]

        for display_name, block_name in blocks:
            item = QTreeWidgetItem(parent, [display_name, "(click to load)", "RSZ Block"])
            item.setData(0, Qt.UserRole, {"type": "rsz_block", "block_name": block_name})
            # Add placeholder
            QTreeWidgetItem(item, ["Loading...", "", ""])

    def _on_item_expanded(self, item: QTreeWidgetItem):
        """Handle lazy loading when item is expanded"""
        data = item.data(0, Qt.UserRole)

        # Debug logging
        item_text = item.text(0)
        print(f"[DEBUG] Item expanded: {item_text}")
        print(f"[DEBUG] Item data: {data}")

        if not data:
            print(f"[DEBUG] No data, returning")
            return

        item_id = id(item)
        if item_id in self._expanded_items:
            print(f"[DEBUG] Already expanded, returning")
            return  # Already expanded

        item_type = data.get("type")

        # Only process lazy-loaded items (those with a "type" field)
        if not item_type:
            print(f"[DEBUG] No type field, this is not a lazy-loaded item")
            return

        print(f"[DEBUG] Processing lazy load for type: {item_type}")

        self._expanded_items.add(item_id)

        # Remove placeholder
        while item.childCount() > 0:
            item.takeChild(0)

        if item_type == "nodes_list":
            self._load_nodes(item)
        elif item_type == "rsz_block":
            self._load_rsz_block(item, data.get("block_name"))
        elif item_type == "node":
            self._load_node_details(item, data.get("node_index"))
        elif item_type == "rsz_instance":
            self._load_rsz_instance(item, data.get("block_name"), data.get("instance_index"))
        elif item_type == "rsz_instance_by_hash":
            self._load_rsz_instance_by_hash(item, data.get("block_name"), data.get("target_hash"))
        elif item_type == "state_with_node":
            self._load_state_details(item, data.get("state"))
        elif item_type == "target_node_from_hash":
            self._load_target_node_from_hash(item, data.get("target_hash"))

    def _show_context_menu(self, position):
        """Show context menu on right-click"""
        item = self.tree.itemAt(position)
        if not item:
            return

        menu = QMenu(self.tree)

        # Get item metadata
        print(f"[DEBUG _show_context_menu] Context menu for: {item.text(0)}")
        print(f"[DEBUG _show_context_menu] Item ID: {id(item)}")
        print(f"[DEBUG _show_context_menu] Parent: {item.parent().text(0) if item.parent() else 'None'}")
        print(f"[DEBUG _show_context_menu] Parent ID: {id(item.parent()) if item.parent() else 'None'}")

        data = item.data(0, Qt.UserRole)
        print(f"[DEBUG _show_context_menu] Raw data from UserRole: {data}")
        print(f"[DEBUG _show_context_menu] Data type: {type(data)}")

        # Try getting data from other columns
        data_col1 = item.data(1, Qt.UserRole)
        data_col2 = item.data(2, Qt.UserRole)
        print(f"[DEBUG _show_context_menu] Column 1 UserRole: {data_col1}")
        print(f"[DEBUG _show_context_menu] Column 2 UserRole: {data_col2}")

        is_editable = data and data.get("editable") if isinstance(data, dict) else False
        has_children = item.childCount() > 0

        print(f"[DEBUG _show_context_menu] is_editable: {is_editable}, has_children: {has_children}")
        print(f"[DEBUG _show_context_menu] Item flags: {item.flags()}")

        # Add "Expand" action if item has children
        if has_children:
            expand_action = menu.addAction("展开" if not item.isExpanded() else "折叠")
            expand_action.triggered.connect(lambda: self._toggle_expand(item))

        # Add "Edit" action if item is editable
        if is_editable:
            edit_action = menu.addAction("编辑")
            edit_action.triggered.connect(lambda: self._start_edit(item))

        # Show menu if there are any actions
        if not menu.isEmpty():
            menu.exec_(self.tree.viewport().mapToGlobal(position))

    def _toggle_expand(self, item: QTreeWidgetItem):
        """Toggle item expansion"""
        if item.isExpanded():
            self.tree.collapseItem(item)
        else:
            self.tree.expandItem(item)

    def _on_item_clicked(self, item: QTreeWidgetItem, _column: int):
        """Handle item click - close any open editors"""
        # If there's an editing item and it's different from the clicked item
        if self._editing_item and self._editing_item != item:
            print(f"[DEBUG] Closing editor for previous item")
            self.tree.closePersistentEditor(self._editing_item, 1)
            self._editing_item = None

    def _start_edit(self, item: QTreeWidgetItem):
        """Start editing the Value column (column 1) of an item"""
        print(f"[DEBUG] Starting edit for: {item.text(0)}")

        # Close any previously open editor
        if self._editing_item:
            self.tree.closePersistentEditor(self._editing_item, 1)

        # Open persistent editor for this item
        self._editing_item = item
        self.tree.openPersistentEditor(item, 1)

        # Set focus to the editor
        editor = self.tree.itemWidget(item, 1)
        if editor:
            editor.setFocus()

    def _load_nodes(self, parent: QTreeWidgetItem):
        """Load all nodes into tree"""
        for i, node in enumerate(self.motfsm.bhvt.nodes):
            name = node.name if node.name else f"Node_{i}"
            item = QTreeWidgetItem(parent, [
                f"[{i}] {name}",
                f"hash=0x{node.id_hash:08X}",
                "BHVTNode"
            ])
            item.setData(0, Qt.UserRole, {"type": "node", "node_index": i})
            # Add placeholder for lazy loading
            QTreeWidgetItem(item, ["Loading...", "", ""])

    def _get_node_name_by_hash(self, hash_value: int) -> str:
        """Get node name by hash for display"""
        if hash_value == 0:
            return "<NULL>"
        # Search for node with matching hash
        if self.motfsm.bhvt:
            for idx, node in enumerate(self.motfsm.bhvt.nodes):
                if node.id_hash == hash_value:
                    name = node.name if node.name else f"Node_{idx}"
                    return f"[{idx}] {name}"
        return f"<hash:0x{hash_value:08X}>"

    def _get_node_name_by_index(self, index: int) -> str:
        """Get node name by index for display"""
        if index < 0:
            return "None"
        target_node = self.motfsm.get_node_by_index(index)
        if target_node:
            name = target_node.name if target_node.name else f"Node_{index}"
            return f"[{index}] {name}"
        return f"[{index}] <unknown>"

    def _get_action_class_name_by_hash(self, action_hash: int) -> str:
        """Get Action RSZ instance class name by matching ID hash"""
        if action_hash == 0:
            return "<NULL>"

        try:
            # Get Actions block
            blocks = self.motfsm.rsz_blocks
            if not blocks or not blocks.actions:
                return "<no RSZ>"

            # Search through Actions RSZ block for matching ID
            action_block = blocks.actions
            for i in range(1, action_block.instance_count):  # Skip index 0 (NULL)
                instance = action_block.get_instance(i)
                if instance and instance.fields:
                    # Check first field - usually ID or v0_ID
                    for field in instance.fields:
                        if 'ID' in field.name.upper() and not field.is_array:
                            if isinstance(field.value, int) and field.value == action_hash:
                                # Found matching action
                                return instance.class_name.split(".")[-1]
                            break  # Only check first ID-like field

            return f"<hash:0x{action_hash:08X}>"
        except Exception as e:
            return f"<error:{type(e).__name__}>"

    def _get_action_class_name(self, action_index: int) -> str:
        """Get Action RSZ instance class name"""
        try:
            instance = self.motfsm.get_action_instance(action_index)
            if instance:
                return instance.class_name.split(".")[-1]
        except:
            pass
        return "<unknown>"

    def _load_node_details(self, parent: QTreeWidgetItem, node_index: int):
        """Load node details into tree"""
        node = self.motfsm.get_node_by_index(node_index)
        if not node:
            return

        # ID Hash - keep visible at top level (editable)
        self._updating_tree = True
        self._create_editable_item(parent, "id_hash", node.id_hash, "uint32", node)
        self._updating_tree = False

        # Collapse basic fields into NodeDetails group (default collapsed)
        node_details = QTreeWidgetItem(parent, ["Node Details", "", ""])

        self._updating_tree = True
        self._create_editable_item(node_details, "ex_id", node.ex_id, "uint32", node)
        self._create_editable_item(node_details, "name", node.name, "string", node)
        self._create_editable_item(node_details, "parent", node.parent, "int32", node)
        self._create_editable_item(node_details, "priority", node.priority, "int32", node)
        self._create_editable_item(node_details, "node_attribute", node.node_attribute, "uint16", node)
        self._create_editable_item(node_details, "work_flags", node.work_flags, "uint16", node)
        self._create_editable_item(node_details, "is_fsm", node.is_fsm, "bool", node)
        self._create_editable_item(node_details, "has_reference_tree", node.has_reference_tree, "bool", node)
        self._updating_tree = False
        # Keep collapsed by default - don't call node_details.setExpanded(True)

        # Children with resolved names (using hash) - expandable to view details
        if node.children:
            children_item = QTreeWidgetItem(parent, [f"Children ({len(node.children)})", "", ""])
            for i, child in enumerate(node.children):
                # Resolve child node by hash
                child_node_name = self._get_node_name_by_hash(child.id_hash)
                child_display = f"[{i}] {child_node_name}"

                child_item = QTreeWidgetItem(children_item, [
                    child_display,
                    f"hash=0x{child.id_hash:08X}, ex_id=0x{child.ex_id:08X}, index={child.index}",
                    "ChildNode (expand to view)"
                ])

                # Store child hash for expandable viewing
                child_item.setData(0, Qt.UserRole, {
                    "type": "target_node_from_hash",
                    "target_hash": child.id_hash
                })
                # Add placeholder for lazy loading
                QTreeWidgetItem(child_item, ["Loading...", "", ""])

        # Actions with RSZ class names (using hash)
        if node.actions:
            actions_item = QTreeWidgetItem(parent, [f"Actions ({len(node.actions)})", "", ""])
            for i, action in enumerate(node.actions):
                # Get RSZ class name by matching hash
                class_name = self._get_action_class_name_by_hash(action.id_hash)
                action_display = f"[{i}] {class_name}"

                action_item = QTreeWidgetItem(actions_item, [
                    action_display,
                    f"hash=0x{action.id_hash:08X}, index={action.index}",
                    "Action"
                ])

                # Add RSZ instance reference (lazy) - use hash to find instance
                rsz_ref = QTreeWidgetItem(action_item, ["RSZ Instance Details", "(expand to load)", ""])
                rsz_ref.setData(0, Qt.UserRole, {
                    "type": "rsz_instance_by_hash",
                    "block_name": "actions",
                    "target_hash": action.id_hash
                })
                QTreeWidgetItem(rsz_ref, ["Loading...", "", ""])

        # Selector
        if node.selector_id >= 0:
            QTreeWidgetItem(parent, ["SelectorID", f"[{node.selector_id}]", "int32"])
        else:
            QTreeWidgetItem(parent, ["SelectorID", str(node.selector_id), "int32"])

        # States with resolved node names
        if node.states:
            states_item = QTreeWidgetItem(parent, [f"States ({len(node.states)})", "", ""])
            for i, state in enumerate(node.states):
                # Resolve target node name for display
                target_node_name = self._get_node_name_by_hash(state.mTransitions)

                # Display state index + target node name
                state_item = QTreeWidgetItem(states_item, [f"[{i}] → {target_node_name}", "", "State"])

                # Store metadata for lazy loading the state details
                state_item.setData(0, Qt.UserRole, {
                    "type": "state_with_node",
                    "state_index": i,
                    "state": state
                })

                # Add placeholder for lazy loading
                QTreeWidgetItem(state_item, ["Loading...", "", ""])

        # Transitions with resolved node names
        if node.transitions:
            trans_item = QTreeWidgetItem(parent, [f"Transitions ({len(node.transitions)})", "", ""])
            for i, trans in enumerate(node.transitions):
                # Resolve target node name for display
                target_node_name = self._get_node_name_by_hash(trans.mStartState)
                t_item = QTreeWidgetItem(trans_item, [f"[{i}] → {target_node_name}", "", "Transition"])

                # mStartTransitionEvent (TransitionEvent indices)
                if trans.mStartTransitionEvent.count > 0:
                    mevents_item = QTreeWidgetItem(t_item, [
                        f"mStartTransitionEvent ({trans.mStartTransitionEvent.count})", "", "TransitionEvent[]"
                    ])
                    for j, event_idx in enumerate(trans.mStartTransitionEvent.values):
                        event_item = QTreeWidgetItem(mevents_item, [
                            f"[{j}] TransitionEvent[{event_idx}]",
                            "(expand to load)",
                            "int32"
                        ])
                        # Add lazy loading for RSZ instance
                        # NOTE: RSZ instance[0] is NULL, so actual index = event_idx + 1
                        event_item.setData(0, Qt.UserRole, {
                            "type": "rsz_instance",
                            "block_name": "transition_events",
                            "instance_index": event_idx + 1
                        })
                        QTreeWidgetItem(event_item, ["Loading...", "", ""])
                else:
                    QTreeWidgetItem(t_item, ["mStartTransitionEvent", "empty", "TransitionEvent[]"])

                # mStartState (editable hash, with expandable target node view)
                self._updating_tree = True
                mStartState_item = self._create_editable_item(t_item, "mStartState", trans.mStartState, "uint32", trans)
                self._updating_tree = False

                # Add expandable child to view target node details
                view_target = QTreeWidgetItem(mStartState_item, [
                    f"→ View Target Node: {target_node_name}",
                    "(expand to load)",
                    ""
                ])
                view_target.setData(0, Qt.UserRole, {
                    "type": "target_node_from_hash",
                    "target_hash": trans.mStartState
                })
                QTreeWidgetItem(view_target, ["Loading...", "", ""])

                # mStartStateTransition (condition hash, -1 means no condition)
                if trans.mStartStateTransition == -1:
                    QTreeWidgetItem(t_item, ["mStartStateTransition (Condition)", "None (unconditional)", "int32"])
                else:
                    cond_item = QTreeWidgetItem(t_item, [
                        f"mStartStateTransition: Condition[{trans.mStartStateTransition}]",
                        "(expand to load)",
                        "int32"
                    ])
                    # Add lazy loading for RSZ instance
                    # NOTE: RSZ instance[0] is NULL, so actual index = condition_idx + 1
                    cond_item.setData(0, Qt.UserRole, {
                        "type": "rsz_instance",
                        "block_name": "conditions",
                        "instance_index": trans.mStartStateTransition + 1
                    })
                    QTreeWidgetItem(cond_item, ["Loading...", "", ""])

                # mStartStateEx (editable)
                self._updating_tree = True
                self._create_editable_item(t_item, "mStartStateEx", trans.mStartStateEx, "uint32", trans)
                self._updating_tree = False

        # FSM-specific fields
        if node.is_fsm:
            fsm_item = QTreeWidgetItem(parent, ["FSM Fields", "", ""])

            self._updating_tree = True
            self._create_editable_item(fsm_item, "name_hash", node.name_hash, "uint32", node)
            self._create_editable_item(fsm_item, "fullname_hash", node.fullname_hash, "uint32", node)
            self._updating_tree = False

            # Tags (editable list)
            if node.tags:
                tags_item = QTreeWidgetItem(fsm_item, [f"Tags ({len(node.tags)})", "", ""])
                self._updating_tree = True
                for j, tag in enumerate(node.tags):
                    # Note: Tags is a list, need special handling for editing
                    QTreeWidgetItem(tags_item, [f"[{j}]", f"0x{tag:08X}", "uint32"])
                    # TODO: Add editing support for list items
                self._updating_tree = False

            self._updating_tree = True
            self._create_editable_item(fsm_item, "is_branch", node.is_branch, "uint8", node)
            self._create_editable_item(fsm_item, "is_end", node.is_end, "uint8", node)
            self._updating_tree = False

    def _load_rsz_block(self, parent: QTreeWidgetItem, block_name: str):
        """Load RSZ block into tree"""
        blocks = self.motfsm.rsz_blocks
        if not blocks:
            QTreeWidgetItem(parent, ["Error: RSZ blocks not available", "", ""])
            return

        block = blocks.get_block(block_name)
        if not block:
            QTreeWidgetItem(parent, ["Error: Block not found", "", ""])
            return

        # Update parent text with count
        parent.setText(1, f"{block.instance_count} instances, {block.object_count} objects")

        # Add header info
        QTreeWidgetItem(parent, ["ObjectCount", str(block.object_count), "uint32"])
        QTreeWidgetItem(parent, ["InstanceCount", str(block.instance_count), "uint32"])

        # Add instances (lazy loaded)
        instances_item = QTreeWidgetItem(parent, [f"Instances ({block.instance_count})", "", ""])
        for i in range(min(block.instance_count, 1000)):  # Limit to first 1000 for performance
            class_name = block.get_class_name(i)
            short_name = class_name.split(".")[-1] if "." in class_name else class_name
            inst_item = QTreeWidgetItem(instances_item, [
                f"[{i}]",
                short_name,
                class_name
            ])
            inst_item.setData(0, Qt.UserRole, {
                "type": "rsz_instance",
                "block_name": block_name,
                "instance_index": i
            })
            # Add placeholder
            QTreeWidgetItem(inst_item, ["Loading...", "", ""])

        if block.instance_count > 1000:
            QTreeWidgetItem(instances_item, [f"... and {block.instance_count - 1000} more", "", ""])

    def _load_rsz_instance(self, parent: QTreeWidgetItem, block_name: str, instance_index: int):
        """Load RSZ instance fields into tree"""
        blocks = self.motfsm.rsz_blocks
        if not blocks:
            return

        block = blocks.get_block(block_name)
        if not block:
            return

        instance = block.get_instance(instance_index)
        if not instance:
            QTreeWidgetItem(parent, ["Error: Instance not found", "", ""])
            return

        # Update parent text
        parent.setText(1, instance.class_name.split(".")[-1])
        parent.setText(2, instance.class_name)

        # Add metadata
        QTreeWidgetItem(parent, ["Class", instance.class_name, "string"])
        QTreeWidgetItem(parent, ["Offset", f"0x{instance.start_offset:X}", ""])
        QTreeWidgetItem(parent, ["Size", f"{instance.size} bytes", ""])

        if instance.is_userdata:
            QTreeWidgetItem(parent, ["Type", "UserData (external)", ""])
            return

        # Add fields
        if instance.fields:
            fields_item = QTreeWidgetItem(parent, [f"Fields ({len(instance.fields)})", "", ""])
            self._updating_tree = True  # Prevent itemChanged during loading
            for field in instance.fields:
                self._add_field_item(fields_item, field)
            self._updating_tree = False

    def _add_field_item(self, parent: QTreeWidgetItem, field: RSZFieldValue):
        """Add field value to tree (editable for simple types)"""
        value_str = str(field.value) if field.value is not None else ""

        # Truncate long values
        if len(value_str) > 100:
            value_str = value_str[:100] + "..."

        type_str = field.type_name
        if field.is_array:
            type_str = f"{field.type_name}[{field.array_count}]"

        item = QTreeWidgetItem(parent, [field.name, value_str, type_str])

        # Make editable for simple, non-array types
        # Note: RSZ type names are capitalized (U32, Bool, etc.), so convert to lowercase for comparison
        type_lower = field.type_name.lower()
        if not field.is_array and type_lower in ("u32", "s32", "u16", "s16", "u8", "s8", "f32", "f64", "bool", "string"):
            print(f"[DEBUG _add_field_item] Making field editable: {field.name}, type: {field.type_name}, value: {field.value}")
            print(f"[DEBUG _add_field_item] Item ID: {id(item)}, Parent ID: {id(parent)}")

            item.setFlags(item.flags() | Qt.ItemIsEditable)

            # Map RSZ types to our type system
            mapped_type = {
                "u32": "uint32", "s32": "int32",
                "u16": "uint16", "s16": "int16",
                "u8": "uint8", "s8": "int8",
                "f32": "float", "f64": "double",
                "bool": "bool", "string": "string"
            }.get(type_lower, type_lower)

            data_to_set = {
                "editable": True,
                "field_name": "value",  # RSZFieldValue stores data in 'value' attribute
                "field_type": mapped_type,
                "field_owner": field,
                "old_value": field.value
            }

            print(f"[DEBUG _add_field_item] Setting data: {data_to_set}")
            item.setData(0, Qt.UserRole, data_to_set)

            # Immediately verify the data was stored
            retrieved_data = item.data(0, Qt.UserRole)
            print(f"[DEBUG _add_field_item] Item flags after setting: {item.flags()}")
            print(f"[DEBUG _add_field_item] Data immediately after setting: {retrieved_data}")
            print(f"[DEBUG _add_field_item] Data type: {type(retrieved_data)}")

            # Check if parent has data
            parent_data = parent.data(0, Qt.UserRole)
            print(f"[DEBUG _add_field_item] Parent UserRole data: {parent_data}")

        # Add offset as tooltip
        item.setToolTip(0, f"Offset: 0x{field.offset:X}, Size: {field.size}")

    def _load_rsz_instance_by_hash(self, parent: QTreeWidgetItem, block_name: str, target_hash: int):
        """Load RSZ instance by finding it through hash match"""
        print(f"[DEBUG _load_rsz_instance_by_hash] Called with block_name={block_name}, target_hash=0x{target_hash:08X}")

        blocks = self.motfsm.rsz_blocks
        if not blocks:
            print(f"[DEBUG _load_rsz_instance_by_hash] No RSZ blocks available")
            QTreeWidgetItem(parent, ["Error: RSZ blocks not available", "", ""])
            return

        block = blocks.get_block(block_name)
        if not block:
            print(f"[DEBUG _load_rsz_instance_by_hash] Block not found: {block_name}")
            QTreeWidgetItem(parent, ["Error: Block not found", "", ""])
            return

        print(f"[DEBUG _load_rsz_instance_by_hash] Searching for instance with hash 0x{target_hash:08X} in {block.instance_count} instances")

        # Search for matching instance by hash
        found_index = -1
        for i in range(1, block.instance_count):  # Skip index 0 (NULL)
            instance = block.get_instance(i)
            if instance and instance.fields:
                # Check first ID field
                for field in instance.fields:
                    if 'ID' in field.name.upper() and not field.is_array:
                        if isinstance(field.value, int) and field.value == target_hash:
                            found_index = i
                            print(f"[DEBUG _load_rsz_instance_by_hash] Found matching instance at index {i}")
                            break
                if found_index >= 0:
                    break

        if found_index < 0:
            print(f"[DEBUG _load_rsz_instance_by_hash] No instance found with hash 0x{target_hash:08X}")
            QTreeWidgetItem(parent, [f"Error: No instance found with ID hash 0x{target_hash:08X}", "", ""])
            return

        # Load the found instance
        instance = block.get_instance(found_index)
        if not instance:
            print(f"[DEBUG _load_rsz_instance_by_hash] Failed to get instance at index {found_index}")
            QTreeWidgetItem(parent, ["Error: Instance not found", "", ""])
            return

        print(f"[DEBUG _load_rsz_instance_by_hash] Loading instance {found_index}: {instance.class_name}")
        print(f"[DEBUG _load_rsz_instance_by_hash] Instance has {len(instance.fields) if instance.fields else 0} fields")

        # Update parent text
        parent.setText(1, f"RSZ[{found_index}]: {instance.class_name.split('.')[-1]}")

        # Add metadata
        QTreeWidgetItem(parent, ["RSZ Index", str(found_index), ""])
        QTreeWidgetItem(parent, ["Class", instance.class_name, "string"])
        QTreeWidgetItem(parent, ["Offset", f"0x{instance.start_offset:X}", ""])
        QTreeWidgetItem(parent, ["Size", f"{instance.size} bytes", ""])

        if instance.is_userdata:
            print(f"[DEBUG _load_rsz_instance_by_hash] Instance is userdata, skipping fields")
            QTreeWidgetItem(parent, ["Type", "UserData (external)", ""])
            return

        # Add fields
        if instance.fields:
            print(f"[DEBUG _load_rsz_instance_by_hash] Creating Fields group with {len(instance.fields)} fields")
            fields_item = QTreeWidgetItem(parent, [f"Fields ({len(instance.fields)})", "", ""])
            self._updating_tree = True  # Prevent itemChanged during loading
            for idx, field in enumerate(instance.fields):
                print(f"[DEBUG _load_rsz_instance_by_hash] Processing field {idx}: {field.name}, type: {field.type_name}, is_array: {field.is_array}")
                self._add_field_item(fields_item, field)
            self._updating_tree = False
            print(f"[DEBUG _load_rsz_instance_by_hash] Finished adding all fields")
        else:
            print(f"[DEBUG _load_rsz_instance_by_hash] Instance has no fields")

    def _load_state_details(self, parent: QTreeWidgetItem, state):
        """Load state details"""
        if not state:
            QTreeWidgetItem(parent, ["Error: State not available", "", ""])
            return

        # Display state metadata fields

        # 1. mStates (TransitionEvent indices)
        if state.mStates.count > 0:
            mstates_item = QTreeWidgetItem(parent, [
                f"mStates ({state.mStates.count})", "", "TransitionEvent[]"
            ])
            for j, event_idx in enumerate(state.mStates.values):
                event_item = QTreeWidgetItem(mstates_item, [
                    f"[{j}] TransitionEvent[{event_idx}]",
                    "(expand to load)",
                    "int32"
                ])
                # Add lazy loading for RSZ instance
                # NOTE: RSZ instance[0] is NULL, so actual index = event_idx + 1
                event_item.setData(0, Qt.UserRole, {
                    "type": "rsz_instance",
                    "block_name": "transition_events",
                    "instance_index": event_idx + 1
                })
                QTreeWidgetItem(event_item, ["Loading...", "", ""])
        else:
            QTreeWidgetItem(parent, ["mStates", "empty", "TransitionEvent[]"])

        # 2. mTransitions (editable hash, with expandable target node view)
        self._updating_tree = True
        mTransitions_item = self._create_editable_item(parent, "mTransitions", state.mTransitions, "uint32", state)
        self._updating_tree = False

        # Add expandable child to view target node details
        target_node_name = self._get_node_name_by_hash(state.mTransitions)
        view_target = QTreeWidgetItem(mTransitions_item, [
            f"→ View Target Node: {target_node_name}",
            "(expand to load)",
            ""
        ])
        view_target.setData(0, Qt.UserRole, {
            "type": "target_node_from_hash",
            "target_hash": state.mTransitions
        })
        QTreeWidgetItem(view_target, ["Loading...", "", ""])

        # 3. TransitionConditions (condition index)
        if state.TransitionConditions >= 0:
            cond_item = QTreeWidgetItem(parent, [
                f"TransitionConditions: Condition[{state.TransitionConditions}]",
                "(expand to load)",
                "int32"
            ])
            # Add lazy loading for RSZ instance
            # NOTE: RSZ instance[0] is NULL, so actual index = condition_idx + 1
            cond_item.setData(0, Qt.UserRole, {
                "type": "rsz_instance",
                "block_name": "conditions",
                "instance_index": state.TransitionConditions + 1
            })
            QTreeWidgetItem(cond_item, ["Loading...", "", ""])
        else:
            QTreeWidgetItem(parent, ["TransitionConditions", "None", "int32"])

        # 4-6. Other state fields (editable)
        self._updating_tree = True
        self._create_editable_item(parent, "TransitionMaps", state.TransitionMaps, "uint32", state)
        self._create_editable_item(parent, "mTransitionAttributes", state.mTransitionAttributes, "uint32", state)
        self._create_editable_item(parent, "mStatesEx", state.mStatesEx, "uint32", state)
        self._updating_tree = False

    def _load_target_node_from_hash(self, parent: QTreeWidgetItem, target_hash: int):
        """Load target node details by hash (used for state/transition navigation)"""
        # Find node index by hash
        target_index = -1
        if self.motfsm.bhvt:
            for idx, node in enumerate(self.motfsm.bhvt.nodes):
                if node.id_hash == target_hash:
                    target_index = idx
                    break

        if target_index < 0:
            QTreeWidgetItem(parent, [f"Error: No node found with hash 0x{target_hash:08X}", "", ""])
            return

        # Load the node details (reuse existing method)
        self._load_node_details(parent, target_index)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int):
        """Handle item value changes for editing"""
        # Debug logging
        item_text = item.text(0)
        print(f"[DEBUG] Item changed: {item_text}, column: {column}")

        # Ignore changes during programmatic updates
        if self._updating_tree:
            print(f"[DEBUG] Ignoring change - updating_tree is True")
            return

        # Only allow editing the Value column (column 1)
        if column != 1:
            print(f"[DEBUG] Ignoring change - not value column")
            return

        # Disable edit triggers after editing completes
        self.tree.setEditTriggers(QTreeWidget.NoEditTriggers)

        # Get metadata
        data = item.data(0, Qt.UserRole)
        print(f"[DEBUG] Item data: {data}")

        if not data or not data.get("editable"):
            print(f"[DEBUG] Not editable or no data")
            return

        field_owner = data.get("field_owner")
        field_name = data.get("field_name")
        field_type = data.get("field_type")
        old_value = data.get("old_value")

        print(f"[DEBUG] Editing field: {field_name}, type: {field_type}, old value: {old_value}")

        if not field_owner or not field_name:
            print(f"[DEBUG] No field owner or field name")
            return

        new_text = item.text(1)
        print(f"[DEBUG] New text: {new_text}")

        try:
            # Parse and validate new value based on type
            if field_type in ("uint32", "uint16", "uint8", "uint64"):
                # Support hex (0x...) or decimal
                if new_text.startswith("0x") or new_text.startswith("0X"):
                    new_value = int(new_text, 16)
                else:
                    new_value = int(new_text, 10)

                # Validate range
                bits = 8 if "8" in field_type else 16 if "16" in field_type else 64 if "64" in field_type else 32
                max_val = (1 << bits) - 1
                if new_value < 0 or new_value > max_val:
                    raise ValueError(f"Value out of range for {field_type}")

            elif field_type in ("int32", "int16", "int8"):
                new_value = int(new_text, 10)
                # Validate range
                bits = 8 if "8" in field_type else 16 if "16" in field_type else 32
                min_val = -(1 << (bits - 1))
                max_val = (1 << (bits - 1)) - 1
                if new_value < min_val or new_value > max_val:
                    raise ValueError(f"Value out of range for {field_type}")

            elif field_type == "bool":
                # Bool: accept "True"/"False" strings from combo box, or legacy formats
                new_value = new_text in ("True", "true", "1", "yes", "Yes")

            elif field_type == "string":
                new_value = new_text

            elif field_type in ("float", "double"):
                new_value = float(new_text)

            else:
                # Unknown type, treat as string
                new_value = new_text

            # Write back to the data structure
            setattr(field_owner, field_name, new_value)

            # Update the stored old_value
            data["old_value"] = new_value

            # Mark as modified
            self.modified = True

            print(f"[DEBUG] Successfully updated {field_name} to {new_value}")

        except ValueError as e:
            # Revert to old value on error
            self._updating_tree = True
            if field_type in ("uint32", "uint16", "uint8", "uint64"):
                item.setText(1, f"0x{old_value:X}" if isinstance(old_value, int) else str(old_value))
            elif field_type == "bool":
                item.setText(1, "True" if old_value else "False")
            else:
                item.setText(1, str(old_value))
            self._updating_tree = False

            print(f"[ERROR] Value error updating field {field_name}: {e}")

        except Exception as e:
            # Revert to old value on any other error
            self._updating_tree = True
            if field_type in ("uint32", "uint16", "uint8", "uint64"):
                item.setText(1, f"0x{old_value:X}" if isinstance(old_value, int) else str(old_value))
            elif field_type == "bool":
                item.setText(1, "True" if old_value else "False")
            else:
                item.setText(1, str(old_value))
            self._updating_tree = False

            print(f"[ERROR] Unexpected error updating field {field_name}: {e}")
