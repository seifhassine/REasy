import struct
import uuid
from typing import Optional, Any, Dict

from PySide6.QtWidgets import (
    QMenu, QInputDialog, QMessageBox, 
    QTreeWidget, QTreeWidgetItem, QWidget, QVBoxLayout
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QBrush, QColor

from file_handlers.base_handler import FileHandler as BaseFileHandler
from file_handlers.uvar import (
    UVarFile, Variable, TypeKind, UvarFlags, 
    FileHandler as BinaryHandler, UVAR_MAGIC
)


class LazyTreeWidget(QTreeWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.itemExpanded.connect(self._on_item_expanded)
        self._metadata_map = {}
        self._handler = None
        
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        
    def set_metadata_map(self, metadata_map):
        self._metadata_map = metadata_map
        
    def set_handler(self, handler):
        self._handler = handler
        
    def _show_context_menu(self, position):
        item = self.itemAt(position)
        if not item:
            return
        if not self._handler:
            return
            
        meta = self._metadata_map.get(id(item))
        if not meta:
            return
        
        menu = self._handler.get_context_menu(self, item, meta)
        if menu:
            menu.exec_(self.mapToGlobal(position))
        
    def _on_item_expanded(self, item):
        if item.childCount() == 1:
            child = item.child(0)
            child_meta = self._metadata_map.get(id(child), {})
            if child_meta.get("type") == "placeholder":
                item.removeChild(child)
                
                item_meta = self._metadata_map.get(id(item), {})
                embed = item_meta.get("embedded")
                if embed and self._handler:
                    self._handler._populate_embedded_contents(item, embed, self._metadata_map)

class UvarViewer(QWidget):
    modified_changed = Signal(bool)
    
    def __init__(self, handler, parent=None):
        super().__init__(parent)
        self.handler = handler
        self._modified = False
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        self.tree = LazyTreeWidget()
        layout.addWidget(self.tree)
        
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Name", "Value"])
        
        self.tree.setStyleSheet("""
            QTreeWidget {
                background-color: #1e1e1e;
                color: #d4d4d4;
                border: none;
                font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
                font-size: 12px;
                outline: none;
            }
            
            QTreeWidget::item {
                padding: 4px;
                border-bottom: 1px solid #2d2d2d;
            }
            
            QTreeWidget::item:hover {
                background-color: #2d2d2d;
            }
            
            QTreeWidget::item:selected {
                background-color: #094771;
                color: #ffffff;
            }
            
            QTreeWidget::branch:has-siblings:!adjoins-item {
                border-image: url(none.png) 0;
            }
            
            QTreeWidget::branch:has-siblings:adjoins-item {
                border-image: url(none.png) 0;
            }
            
            QTreeWidget::branch:!has-children:!has-siblings:adjoins-item {
                border-image: url(none.png) 0;
            }
            
            QTreeWidget::branch:has-children:!has-siblings:closed,
            QTreeWidget::branch:closed:has-children:has-siblings {
                border-image: none;
                image: none;
                padding-left: 10px;
            }
            
            QTreeWidget::branch:open:has-children:!has-siblings,
            QTreeWidget::branch:open:has-children:has-siblings {
                border-image: none;
                image: none;
            }
            
            QTreeWidget::branch:has-children:!has-siblings:closed::indicator,
            QTreeWidget::branch:closed:has-children:has-siblings::indicator {
                image: none;
                width: 0px;
                height: 0px;
            }
            
            QTreeWidget::branch:has-children:!has-siblings:closed::indicator::hover,
            QTreeWidget::branch:closed:has-children:has-siblings::indicator::hover {
                border: none;
            }
            
            QTreeWidget::branch:open:has-children:!has-siblings::indicator,
            QTreeWidget::branch:open:has-children:has-siblings::indicator {
                image: none;
                width: 0px;
                height: 0px;
            }
            
            QHeaderView::section {
                background-color: #252526;
                color: #cccccc;
                padding: 6px;
                border: none;
                border-right: 1px solid #3c3c3c;
                font-weight: bold;
                font-size: 13px;
            }
            
            QHeaderView::section:last {
                border-right: none;
            }
            
            QScrollBar:vertical {
                background-color: #1e1e1e;
                width: 14px;
                border: none;
            }
            
            QScrollBar::handle:vertical {
                background-color: #424242;
                min-height: 30px;
                border-radius: 7px;
                margin: 2px;
            }
            
            QScrollBar::handle:vertical:hover {
                background-color: #4f4f4f;
            }
            
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                border: none;
                background: none;
                height: 0px;
            }
            
            QScrollBar:horizontal {
                background-color: #1e1e1e;
                height: 14px;
                border: none;
            }
            
            QScrollBar::handle:horizontal {
                background-color: #424242;
                min-width: 30px;
                border-radius: 7px;
                margin: 2px;
            }
            
            QScrollBar::handle:horizontal:hover {
                background-color: #4f4f4f;
            }
            
            QScrollBar::add-line:horizontal,
            QScrollBar::sub-line:horizontal {
                border: none;
                background: none;
                width: 0px;
            }
        """)
        
        self.tree.setColumnWidth(0, 300)
        
    @property
    def modified(self):
        return self._modified
        
    @modified.setter
    def modified(self, value):
        if self._modified != value:
            self._modified = value
            self.modified_changed.emit(value)
            
    def populate_tree(self):
        if self.handler:
            metadata_map = {}
            self.handler.populate_treeview(self.tree, None, metadata_map)
            self.tree.set_metadata_map(metadata_map)
            self.tree.set_handler(self.handler)

class UvarHandler(BaseFileHandler):
    
    def __init__(self):
        super().__init__()
        self.uvar_file: Optional[UVarFile] = None
        self.raw_data: Optional[bytearray] = None
        
    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        if len(data) < 8:
            return False
        magic = struct.unpack_from('<I', data, 4)[0]
        return magic == UVAR_MAGIC
        
    def read(self, data: bytes):
        self.raw_data = bytearray(data)
        self.uvar_file = UVarFile()
        
        if not self.uvar_file.read(data):
            raise ValueError("Failed to read UVAR file")
            
        self.modified = False
        
    def rebuild(self) -> bytes:
        if self.uvar_file is None:
            return b''
        
        self.uvar_file.update_strings()
        result = self.uvar_file.write()
        if result:
            self.raw_data = bytearray(result)
            self.modified = False
        return result
        
    def populate_treeview(self, tree: 'QTreeWidget', parent_item: Optional['QTreeWidgetItem'], metadata_map: Dict):
        if self.uvar_file is None:
            return
            
        if hasattr(tree, '__class__') and tree.__class__.__name__ == 'QTreeView':
            print("Warning: populate_treeview received QTreeView instead of QTreeWidget")
            return
            
        if parent_item:
            header_item = QTreeWidgetItem(parent_item)
        else:
            header_item = QTreeWidgetItem(tree)
        header_item.setText(0, "📋 Header")
        header_item.setText(1, f"Version: {self.uvar_file.header.version}")
        header_item.setForeground(1, QBrush(QColor("#888888")))
        metadata_map[id(header_item)] = {"type": "header", "file": self.uvar_file}
        
        name_item = QTreeWidgetItem(header_item)
        name_item.setText(0, "📝 Name")
        name_item.setText(1, self.uvar_file.header.name or "(unnamed)")
        if not self.uvar_file.header.name:
            name_item.setForeground(1, QBrush(QColor("#666666")))
        metadata_map[id(name_item)] = {"type": "file_name", "file": self.uvar_file}
        
        vars_item = QTreeWidgetItem(parent_item or tree)
        vars_item.setText(0, "📦 Variables")
        vars_item.setText(1, f"{len(self.uvar_file.variables)} items")
        vars_item.setForeground(1, QBrush(QColor("#4EC9B0")))
        metadata_map[id(vars_item)] = {"type": "variables_section", "file": self.uvar_file}
        
        for i, var in enumerate(self.uvar_file.variables):
            self._create_variable_item(
                vars_item,
                self.uvar_file,
                var,
                i,
                metadata_map
            )
                
        if self.uvar_file.embedded_uvars:
            embeds_item = QTreeWidgetItem(parent_item or tree)
            embeds_item.setText(0, "📁 Embedded Files")
            embeds_item.setText(1, f"{len(self.uvar_file.embedded_uvars)} files")
            embeds_item.setForeground(1, QBrush(QColor("#4EC9B0")))
            metadata_map[id(embeds_item)] = {"type": "embedded_section", "file": self.uvar_file}
            
            for i, embed in enumerate(self.uvar_file.embedded_uvars):
                embed_item = QTreeWidgetItem(embeds_item)
                embed_name = embed.header.name or f"Embedded_{i}"
                embed_item.setText(0, f"📄 {embed_name}")
                embed_item.setText(1, f"{len(embed.variables)} variables")
                embed_item.setForeground(1, QBrush(QColor("#888888")))
                metadata_map[id(embed_item)] = {
                    "type": "embedded_file",
                    "file": self.uvar_file,
                    "embedded": embed,
                    "index": i
                }

                placeholder = QTreeWidgetItem(embed_item)
                placeholder.setText(0, "⏳ Click to expand...")
                placeholder.setForeground(0, QBrush(QColor("#666666")))
                metadata_map[id(placeholder)] = {
                    "type": "placeholder",
                    "embedded": embed
                }
                
        if not self.uvar_file.embedded_uvars:
            embeds_item = QTreeWidgetItem(parent_item or tree)
            embeds_item.setText(0, "📁 Embedded Files")
            embeds_item.setText(1, "0 files")
            embeds_item.setForeground(1, QBrush(QColor("#4EC9B0")))
            metadata_map[id(embeds_item)] = {"type": "embedded_section", "file": self.uvar_file}

    def _get_type_icon(self, var_type) -> str:
        type_icons = {
            "Boolean": "🔘",
            "Uint8": "🔢",
            "Uint16": "🔢",
            "Uint32": "🔢",
            "Uint64": "🔢",
            "Int8": "🔢",
            "Int16": "🔢",
            "Int32": "🔢",
            "Int64": "🔢",
            "Float": "🔵",
            "Double": "🔵",
            "String": "📝",
            "Vec2": "📐",
            "Vec3": "📐",
            "Vec4": "📐",
            "Int3": "📏",
            "Uint3": "📏",
            "Guid": "🔑",
            "Object": "📦",
            "List": "📋",
            "C8": "🔤",
            "C16": "🔤",
            "C32": "🔤",
        }
        return type_icons.get(var_type.name if hasattr(var_type, 'name') else str(var_type), "❓")
    
    def _get_type_color(self, var_type) -> str:
        type_colors = {
            "Boolean": "#569CD6",  # Blue
            "Uint8": "#B5CEA8",    # Light green
            "Uint16": "#B5CEA8",   # Light green
            "Uint32": "#B5CEA8",   # Light green
            "Uint64": "#B5CEA8",   # Light green
            "Int8": "#9CDCFE",     # Light blue
            "Int16": "#9CDCFE",    # Light blue
            "Int32": "#9CDCFE",    # Light blue
            "Int64": "#9CDCFE",    # Light blue
            "Float": "#4EC9B0",    # Teal
            "Double": "#4EC9B0",   # Teal
            "String": "#CE9178",   # Orange
            "Vec2": "#C586C0",     # Purple
            "Vec3": "#C586C0",     # Purple
            "Vec4": "#C586C0",     # Purple
            "Int3": "#DCDCAA",     # Yellow
            "Uint3": "#DCDCAA",    # Yellow
            "Guid": "#F44747",     # Red
            "Object": "#608B4E",   # Green
            "List": "#646695",     # Dark blue
            "C8": "#CE9178",       # Orange
            "C16": "#CE9178",      # Orange
            "C32": "#CE9178",      # Orange
        }
        return type_colors.get(var_type.name if hasattr(var_type, 'name') else str(var_type), "#D4D4D4")
                     
    def _populate_embedded_contents(self, parent_item: 'QTreeWidgetItem', embed: UVarFile, metadata_map: Dict):
        if embed.variables:
            vars_item = QTreeWidgetItem(parent_item)
            vars_item.setText(0, "📦 Variables")
            vars_item.setText(1, f"{len(embed.variables)} items")
            vars_item.setForeground(1, QBrush(QColor("#4EC9B0")))
            metadata_map[id(vars_item)] = {"type": "variables_section", "file": embed}
            
            for i, var in enumerate(embed.variables):
                self._create_variable_item(
                    vars_item,
                    embed,
                    var,
                    i,
                    metadata_map
                )

    def _create_variable_item(self, parent_item: 'QTreeWidgetItem', file: UVarFile,
                              var: Variable, index: int, metadata_map: Optional[Dict]) -> 'QTreeWidgetItem':
        """Create a tree item for a variable and populate metadata."""
        var_item = QTreeWidgetItem(parent_item)

        type_icon = self._get_type_icon(var.type)
        display_name = var.name or f'Variable_{index}'
        var_item.setText(0, f"{type_icon} {display_name}")
        var_item.setText(1, self._format_variable_value(var))

        color = self._get_type_color(var.type)
        var_item.setForeground(1, QBrush(QColor(color)))

        if metadata_map is not None:
            metadata_map[id(var_item)] = {
                "type": "variable",
                "file": file,
                "variable": var,
                "index": index
            }

        self._add_variable_details(var_item, var, metadata_map if metadata_map is not None else {})

        if var_item.childCount() > 0:
            var_item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)

        return var_item
                
    def _format_variable_value(self, var: Variable) -> str:
        if var.value is None:
            return "∅ (null)"
            
        if hasattr(var.value, 'x') and hasattr(var.value, 'y'):
            if hasattr(var.value, 'z'):
                return f"({var.value.x}, {var.value.y}, {var.value.z})"
            else:
                return f"({var.value.x}, {var.value.y})"
        elif isinstance(var.value, (list, tuple)):
            if len(var.value) == 0:
                return "[ ] empty"
            elif len(var.value) <= 4:
                return f"[{', '.join(str(v) for v in var.value)}]"
            else:
                return f"[...] {len(var.value)} items"
        elif isinstance(var.value, uuid.UUID):
            return str(var.value)
        elif isinstance(var.value, bool):
            return "✓ True" if var.value else "✗ False"
        elif isinstance(var.value, float):
            formatted = f"{var.value:.6f}".rstrip('0').rstrip('.')
            return formatted
        elif isinstance(var.value, str):
            if not var.value:
                return '""'
            if len(var.value) > 50:
                return f'"{var.value[:47]}..."'
            return f'"{var.value}"'
        elif isinstance(var.value, int):
            if abs(var.value) >= 1000:
                return f"{var.value:,}"
            return str(var.value)
        else:
            return str(var.value)
            
    def _add_variable_details(self, parent: 'QTreeWidgetItem', var: Variable, metadata_map: Dict):
        type_item = QTreeWidgetItem(parent)
        type_item.setText(0, "🏷️ Type")
        type_text = f"{var.type.name}"
        if var.flags:
            type_text += f" [flags: 0x{var.flags:02x}]"
        type_item.setText(1, type_text)
        type_item.setForeground(1, QBrush(QColor("#9CDCFE")))
        metadata_map[id(type_item)] = {"type": "variable_type", "variable": var}
        
        guid_item = QTreeWidgetItem(parent)
        guid_item.setText(0, "🔑 GUID")
        guid_bytes = var.guid.bytes_le
        guid_mem_str = '-'.join([
            guid_bytes[0:4].hex(),
            guid_bytes[4:6].hex(),
            guid_bytes[6:8].hex(),
            guid_bytes[8:10].hex(),
            guid_bytes[10:16].hex()
        ])
        guid_item.setText(1, guid_mem_str)
        guid_item.setForeground(1, QBrush(QColor("#CE9178")))
        metadata_map[id(guid_item)] = {"type": "variable_guid", "variable": var}
        
        hash_item = QTreeWidgetItem(parent)
        hash_item.setText(0, "#️⃣ Name Hash")
        hash_item.setText(1, str(var.name_hash))
        hash_item.setForeground(1, QBrush(QColor("#B5CEA8"))) 
        metadata_map[id(hash_item)] = {"type": "variable_hash", "variable": var}
        
        if var.expression:
            expr_item = QTreeWidgetItem(parent)
            expr_item.setText(0, "🔗 Expression")
            expr_item.setText(1, f"{len(var.expression.nodes)} nodes, {len(var.expression.relations)} relations")
            expr_item.setForeground(1, QBrush(QColor("#C586C0"))) 
            metadata_map[id(expr_item)] = {"type": "expression", "variable": var}
 
            out_item = QTreeWidgetItem(expr_item)
            out_item.setText(0, "Output Node")
            out_item.setText(1, str(var.expression.output_node_id))
            out_item.setForeground(1, QBrush(QColor("#B5CEA8")))
            metadata_map[id(out_item)] = {"type": "expression_output_node", "variable": var}
 
            if var.expression.nodes:
                nodes_group = QTreeWidgetItem(expr_item)
                nodes_group.setText(0, "🧩 Nodes")
                nodes_group.setText(1, str(len(var.expression.nodes)))
                nodes_group.setForeground(1, QBrush(QColor("#4EC9B0")))
                metadata_map[id(nodes_group)] = {"type": "expression_nodes", "variable": var}

                outputs_by_node = {}
                inputs_by_node = {}
                for rel in var.expression.relations:
                    outputs_by_node.setdefault(rel.src_node, []).append(rel)
                    inputs_by_node.setdefault(rel.dst_node, []).append(rel)

                for idx, node in enumerate(var.expression.nodes):
                    node_item = QTreeWidgetItem(nodes_group)
                    node_label = node.name or f"Node_{idx}"
                    node_item.setText(0, f"📦 {node_label}")
                    node_item.setText(1, f"id {getattr(node,'node_id', idx)}, {len(getattr(node,'parameters',[]) )} params")
                    node_item.setForeground(1, QBrush(QColor("#888888")))
                    metadata_map[id(node_item)] = {"type": "expression_node", "node": node, "variable": var}

                    if getattr(node, 'parameters', None):
                        for p in node.parameters:
                            p_item = QTreeWidgetItem(node_item)
                            p_item.setText(0, f"🔹 0x{p.name_hash:08x}")
                            p_item.setText(1, f"{p.type.name}: {p.value}")
                            p_item.setForeground(1, QBrush(QColor("#9CDCFE")))
                            metadata_map[id(p_item)] = {"type": "expression_node_param", "param": p, "node": node, "variable": var}

                    outs = outputs_by_node.get(getattr(node, 'node_id', idx), [])
                    outs_item = QTreeWidgetItem(node_item)
                    outs_item.setText(0, "➡️ Outputs")
                    outs_item.setText(1, str(len(outs)))
                    outs_item.setForeground(1, QBrush(QColor("#B5CEA8")))
                    metadata_map[id(outs_item)] = {"type": "expression_node_outputs", "node": node, "variable": var}
                    for rel in outs:
                        rel_item = QTreeWidgetItem(outs_item)
                        rel_item.setText(0, f"{rel.src_node} [port: {rel.src_port}] → {rel.dst_node} [port: {rel.dst_port}]")
                        rel_item.setForeground(0, QBrush(QColor("#B5CEA8")))
                        metadata_map[id(rel_item)] = {"type": "expression_relation", "relation": rel, "variable": var}

                    ins = inputs_by_node.get(getattr(node, 'node_id', idx), [])
                    ins_item = QTreeWidgetItem(node_item)
                    ins_item.setText(0, "⬅️ Inputs")
                    ins_item.setText(1, str(len(ins)))
                    ins_item.setForeground(1, QBrush(QColor("#B5CEA8")))
                    metadata_map[id(ins_item)] = {"type": "expression_node_inputs", "node": node, "variable": var}
                    for rel in ins:
                        rel_item = QTreeWidgetItem(ins_item)
                        rel_item.setText(0, f"{rel.src_node} [port: {rel.src_port}] → {rel.dst_node} [port: {rel.dst_port}]")
                        rel_item.setForeground(0, QBrush(QColor("#B5CEA8")))
                        metadata_map[id(rel_item)] = {"type": "expression_relation", "relation": rel, "variable": var}
                
        parent.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
        
    def get_context_menu(self, tree: 'QTreeWidget', item: 'QTreeWidgetItem', meta: Dict) -> Optional['QMenu']:
        if not meta:
            return None
            
        menu = QMenu(tree)
        meta_type = meta.get("type")
        
        if meta_type == "file_name":
            action = QAction("Edit Name", menu)
            action.triggered.connect(lambda: self._edit_file_name(tree, item, meta))
            menu.addAction(action)
            
        elif meta_type == "variables_section":
            action = QAction("Add Variable", menu)
            action.triggered.connect(lambda: self._add_variable(tree, item, meta))
            menu.addAction(action)
            
            action = QAction("Add Many Variables", menu)
            action.triggered.connect(lambda: self._add_many_variables(tree, item, meta))
            menu.addAction(action)
            
        elif meta_type == "variable":
            action = QAction("Edit Name", menu)
            action.triggered.connect(lambda: self._edit_variable_name(tree, item, meta))
            menu.addAction(action)
            
            action = QAction("Edit Value", menu)
            action.triggered.connect(lambda: self._edit_variable_value(tree, item, meta))
            menu.addAction(action)
            
            action = QAction("Edit Type", menu)
            action.triggered.connect(lambda: self._edit_variable_type(tree, item, meta))
            menu.addAction(action)
            
            if meta.get("variable") and getattr(meta["variable"], "expression", None) is None:
                action = QAction("Add Expression", menu)
                action.triggered.connect(lambda: self._expr_create_expression(tree, item, meta))
                menu.addAction(action)
            
            menu.addSeparator()

            action = QAction("Delete Variable", menu)
            action.triggered.connect(lambda: self._delete_variable(tree, item, meta))
            menu.addAction(action)
            
        elif meta_type == "expression":
            action = QAction("Add Node", menu)
            action.triggered.connect(lambda: self._expr_add_node(tree, item, meta))
            menu.addAction(action)

            action = QAction("Add Relation", menu)
            action.triggered.connect(lambda: self._expr_add_relation(tree, item, meta))
            menu.addAction(action)

            action = QAction("Set Output Node", menu)
            action.triggered.connect(lambda: self._expr_set_output_node(tree, item, meta))
            menu.addAction(action)

            action = QAction("Remove Expression", menu)
            action.triggered.connect(lambda: self._expr_remove_expression(tree, item, meta))
            menu.addAction(action)

        elif meta_type == "expression_node":
            action = QAction("Rename Node", menu)
            action.triggered.connect(lambda: self._expr_rename_node(tree, item, meta))
            menu.addAction(action)

            action = QAction("Change Node Id", menu)
            action.triggered.connect(lambda: self._expr_change_node_id(tree, item, meta))
            menu.addAction(action)

            action = QAction("Add Parameter", menu)
            action.triggered.connect(lambda: self._expr_add_param(tree, item, meta))
            menu.addAction(action)

            action = QAction("Delete Node", menu)
            action.triggered.connect(lambda: self._expr_delete_node(tree, item, meta))
            menu.addAction(action)

        elif meta_type == "expression_node_param":
            action = QAction("Edit Parameter", menu)
            action.triggered.connect(lambda: self._expr_edit_param(tree, item, meta))
            menu.addAction(action)

            action = QAction("Change Name Hash", menu)
            action.triggered.connect(lambda: self._expr_change_param_namehash(tree, item, meta))
            menu.addAction(action)

            action = QAction("Delete Parameter", menu)
            action.triggered.connect(lambda: self._expr_delete_param(tree, item, meta))
            menu.addAction(action)

        elif meta_type == "expression_relation":
            action = QAction("Edit Relation", menu)
            action.triggered.connect(lambda: self._expr_edit_relation(tree, item, meta))
            menu.addAction(action)

            action = QAction("Delete Relation", menu)
            action.triggered.connect(lambda: self._expr_delete_relation(tree, item, meta))
            menu.addAction(action)

        elif meta_type == "expression_output_node":
            action = QAction("Set Output Node", menu)
            action.triggered.connect(lambda: self._expr_set_output_node(tree, item, meta))
            menu.addAction(action)

        elif meta_type == "embedded_section":
            action = QAction("Add Embedded File", menu)
            action.triggered.connect(lambda: self._add_embedded_file(tree, item, meta))
            menu.addAction(action)

        elif meta_type == "embedded_file":
            action = QAction("Delete Embedded File", menu)
            action.triggered.connect(lambda: self._delete_embedded_file(tree, item, meta))
            menu.addAction(action)

        elif meta_type == "variable_guid":
            action = QAction("Copy GUID", menu)
            action.triggered.connect(lambda: self._copy_to_clipboard(item.text(1)))
            menu.addAction(action)

            action = QAction("Generate New GUID", menu)
            action.triggered.connect(lambda: self._generate_new_guid(tree, item, meta))
            menu.addAction(action)
            
        elif meta_type == "variable_hash":
            action = QAction("Copy Name Hash", menu)
            action.triggered.connect(lambda: self._copy_to_clipboard(item.text(1)))
            menu.addAction(action)
            
        elif meta_type == "variable_type":
            action = QAction("Change Type", menu)
            action.triggered.connect(lambda: self._edit_variable_type(tree, item, meta))
            menu.addAction(action)
            
        return menu if menu.actions() else None
        
    def handle_edit(self, meta: Dict, new_val: Any, old_val: Any, item: 'QTreeWidgetItem'):
        pass
        
    def add_variables(self, target: Any, prefix: str, count: int):
        if isinstance(target, UVarFile):
            self.modified = True
            
    def update_strings(self):
        if self.uvar_file:
            self.uvar_file.update_strings()
            
    def _edit_file_name(self, tree: QTreeWidget, item: QTreeWidgetItem, meta: Dict):
        file = meta["file"]
        old_name = file.header.name or ""
        
        new_name, ok = QInputDialog.getText(
            tree, "Edit Name", "Enter new file name:", text=old_name
        )
        
        if ok and new_name != old_name:
            file.header.name = new_name
            item.setText(1, new_name)
            self.modified = True
            
    def _add_variable(self, tree: QTreeWidget, item: QTreeWidgetItem, meta: Dict):
        file = meta["file"]
        
        name, ok = QInputDialog.getText(
            tree, "Add Variable", "Enter variable name:"
        )
        
        if ok and name:
            types = [t.name for t in TypeKind if t != TypeKind.Unknown]
            type_name, ok = QInputDialog.getItem(
                tree, "Variable Type", "Select variable type:",
                types, 0, False
            )
            
            if ok:
                var_type = TypeKind[type_name]
                var = file.add_variable(name, var_type)

                self._create_variable_item(
                    item,
                    file,
                    var,
                    len(file.variables) - 1,
                    tree._metadata_map
                )
                item.setText(1, f"{len(file.variables)} items")
                parent_embed = item.parent()
                if parent_embed is not None and hasattr(tree, '_metadata_map'):
                    pmeta = tree._metadata_map.get(id(parent_embed), {})
                    if pmeta.get('type') == 'embedded_file':
                        parent_embed.setText(1, f"{len(file.variables)} variables")
                 
                self.modified = True
                tree.viewport().update()
                    
    def _add_many_variables(self, tree: QTreeWidget, item: QTreeWidgetItem, meta: Dict):
        import re
        file = meta["file"]
        
        count, ok = QInputDialog.getInt(
            tree, "Add Many Variables", "Number of variables to add:",
            10, 1, 1000
        )
        
        if not ok or count <= 0:
            return
            
        types = [t.name for t in TypeKind if t != TypeKind.Unknown]
        type_name, ok = QInputDialog.getItem(
            tree, "Variable Type", "Select variable type:",
            types, 0, False
        )
        
        if not ok:
            return
            
        var_type = TypeKind[type_name]
        
        auto_base_name = "Variable"
        auto_start_num = 0
        
        if file.variables:
            last_var = file.variables[-1]
            if last_var.name:
                match = re.match(r'^(.+?)(\d+)$', last_var.name)
                if match:
                    auto_base_name = match.group(1)
                    auto_start_num = int(match.group(2)) + 1
                else:
                    match = re.match(r'^(.+_)(\d+)$', last_var.name)
                    if match:
                        auto_base_name = match.group(1)
                        auto_start_num = int(match.group(2)) + 1
                    else:
                        auto_base_name = last_var.name + "_"
                        auto_start_num = 0
        
        suggested_name = f"{auto_base_name}{auto_start_num}"
        base_name_input, ok = QInputDialog.getText(
            tree, "Base Name (Optional)", 
            f"Enter base name (leave empty for auto: {suggested_name}):",
            text=""
        )
        
        if not ok:
            return
        
        if base_name_input:
            match = re.match(r'^(.+?)(\d+)$', base_name_input)
            if match:
                base_name = match.group(1)
                start_num = int(match.group(2))
            else:
                base_name = base_name_input
                if not base_name.endswith('_'):
                    base_name += "_"
                start_num = 0
        else:
            base_name = auto_base_name
            start_num = auto_start_num
        
        for i in range(count):
            num = start_num + i
            if start_num > 0 or (file.variables and re.search(r'\d+$', file.variables[-1].name or "")):
                if file.variables and file.variables[-1].name:
                    last_num_match = re.search(r'(\d+)$', file.variables[-1].name)
                    if last_num_match:
                        padding = len(last_num_match.group(1))
                    else:
                        padding = len(str(start_num + count - 1))
                else:
                    padding = len(str(count))
                name = f"{base_name}{str(num).zfill(padding)}"
            else:
                name = f"{base_name}{num}"
                
            var = file.add_variable(name, var_type)

            self._create_variable_item(
                item,
                file,
                var,
                len(file.variables) - 1,
                tree._metadata_map
            )
        
        item.setText(1, f"{len(file.variables)} items")
        
        self.modified = True
        tree.viewport().update()
                    
    def _edit_variable_name(self, tree: QTreeWidget, item: QTreeWidgetItem, meta: Dict):
        var = meta["variable"]
        old_name = var.name
        
        new_name, ok = QInputDialog.getText(
            tree, "Edit Name", "Enter new variable name:", text=old_name
        )
        
        if ok and new_name != old_name:
            var.name = new_name
            item.setText(0, new_name)
            self.modified = True
            
    def _edit_variable_value(self, tree: QTreeWidget, item: QTreeWidgetItem, meta: Dict):
        from file_handlers.pyside.uvar_value_dialog import UvarValueEditDialog
        
        file = meta.get("file")
        var_index = meta.get("index")
        
        if file and var_index is not None and 0 <= var_index < len(file.variables):
            var = file.variables[var_index]
        else:
            var = meta["variable"]
            
        dialog = UvarValueEditDialog(
            var_name=var.name or f"Variable_{var_index}",
            var_type=var.type,
            current_value=var.value,
            var_flags=var.flags,
            parent=tree
        )
        
        if dialog.exec_():
            try:
                new_value = dialog.get_value()
                
                var.value = new_value
                
                item.setText(1, self._format_variable_value(var))
                
                self.modified = True
                
                tree.viewport().update()
                
                if hasattr(tree, '_metadata_map'):
                    meta_id = id(item)
                    if meta_id in tree._metadata_map:
                        tree._metadata_map[meta_id]['variable'] = var
                
                if item.parent():
                    item.parent().setExpanded(True)
                    
            except Exception as e:
                QMessageBox.warning(tree, "Invalid Value", str(e))
            
    def _edit_variable_type(self, tree: QTreeWidget, item: QTreeWidgetItem, meta: Dict):
        var = meta["variable"]
        
        types = [t.name for t in TypeKind if t != TypeKind.Unknown]
        current_index = types.index(var.type.name) if var.type.name in types else 0
        
        type_name, ok = QInputDialog.getItem(
            tree, "Variable Type", "Select variable type:",
            types, current_index, False
        )
        
        if ok:
            new_type = TypeKind[type_name]
            if new_type != var.type:
                var.type = new_type
                var.reset_value()
                
                meta_type = meta.get("type")
                
                if meta_type == "variable":
                    var_item = item
                elif meta_type == "variable_type":
                    var_item = item.parent()
                else:
                    self.modified = True
                    return
                
                if var_item:
                    type_icon = self._get_type_icon(var.type)
                    var_item.setText(0, f"{type_icon} {var.name or f'Variable_{meta.get("index", 0)}'}")
                    var_item.setText(1, self._format_variable_value(var))
                    
                    color = self._get_type_color(var.type)
                    var_item.setForeground(1, QBrush(QColor(color)))
                    
                    if meta_type == "variable_type":
                        item.setText(1, f"{var.type.name}")
                    else:
                        for i in range(var_item.childCount()):
                            child = var_item.child(i)
                            if child.text(0) == "🏷️ Type":
                                child.setText(1, f"{var.type.name}")
                                break
                    
                    tree.viewport().update()
                
                self.modified = True
                
    def _delete_variable(self, tree: QTreeWidget, item: QTreeWidgetItem, meta: Dict):
        file = meta["file"]
        index = meta["index"]
        
        reply = QMessageBox.question(
            tree, "Delete Variable",
            f"Are you sure you want to delete '{file.variables[index].name}'?",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            if file.remove_variable(index):
                parent = item.parent()
                if parent:
                    parent.removeChild(item)
                    parent.setText(1, f"{len(file.variables)} items")
                    grand = parent.parent()
                    if grand is not None and hasattr(tree, '_metadata_map'):
                        grand_meta = tree._metadata_map.get(id(grand), {})
                        if grand_meta.get('type') == 'embedded_file':
                            grand.setText(1, f"{len(file.variables)} variables")
                     
                    if hasattr(tree, '_metadata_map'):
                        for i in range(parent.childCount()):
                            child = parent.child(i)
                            child_id = id(child)
                            if child_id in tree._metadata_map:
                                child_meta = tree._metadata_map[child_id]
                                if child_meta.get('type') == 'variable' and child_meta.get('index', -1) > index:
                                    child_meta['index'] -= 1
                                    
                self.modified = True
                tree.viewport().update()
            else:
                QMessageBox.warning(tree, "Delete Failed", "Failed to delete variable")
            
    def _copy_to_clipboard(self, text: str):
        from PySide6.QtGui import QGuiApplication
        clipboard = QGuiApplication.clipboard()
        clipboard.setText(text)
        
    def _generate_new_guid(self, tree: QTreeWidget, item: QTreeWidgetItem, meta: Dict):
        var = meta["variable"]
        var.guid = uuid.uuid4()
        guid_bytes = var.guid.bytes_le
        guid_mem_str = '-'.join([
            guid_bytes[0:4].hex(),
            guid_bytes[4:6].hex(),
            guid_bytes[6:8].hex(),
            guid_bytes[8:10].hex(),
            guid_bytes[10:16].hex()
        ])
        item.setText(1, guid_mem_str)
        self.modified = True
        
    def create_viewer(self):
        viewer = UvarViewer(self)
        viewer.populate_tree()
        return viewer

    def _add_embedded_file(self, tree: 'QTreeWidget', item: 'QTreeWidgetItem', meta: Dict):
        main_file = meta["file"]
        default_name = f"Embedded_{len(main_file.embedded_uvars)}"
        name, ok = QInputDialog.getText(tree, "Add Embedded File", "Enter embedded file name:", text=default_name)
        if not ok:
            return
        from file_handlers.uvar.uvar_file import UVarFile as _UVarFile
        new_embed = _UVarFile()
        new_embed.is_embedded = True
        new_embed.header.version = main_file.header.version
        new_embed.header.magic = main_file.header.magic
        new_embed.header.name = name
        main_file.embedded_uvars.append(new_embed)
        embed_item = QTreeWidgetItem(item)
        embed_item.setText(0, f"📄 {name}")
        embed_item.setText(1, "0 variables")
        embed_item.setForeground(1, QBrush(QColor("#888888")))
        if hasattr(tree, '_metadata_map'):
            tree._metadata_map[id(embed_item)] = {
                "type": "embedded_file",
                "file": main_file,
                "embedded": new_embed,
                "index": len(main_file.embedded_uvars) - 1
            }
            placeholder = QTreeWidgetItem(embed_item)
            placeholder.setText(0, "⏳ Click to expand...")
            placeholder.setForeground(0, QBrush(QColor("#666666")))
            tree._metadata_map[id(placeholder)] = {"type": "placeholder", "embedded": new_embed}
        item.setText(1, f"{len(main_file.embedded_uvars)} files")
        self.modified = True
        tree.viewport().update()

    def _delete_embedded_file(self, tree: 'QTreeWidget', item: 'QTreeWidgetItem', meta: Dict):
        main_file = meta["file"]
        index = meta["index"]
        reply = QMessageBox.question(
            tree, "Delete Embedded File",
            f"Are you sure you want to delete '{(meta.get('embedded') or {}).header.name if meta.get('embedded') else 'Embedded'}'?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        if 0 <= index < len(main_file.embedded_uvars):
            del main_file.embedded_uvars[index]
            parent = item.parent()
            if parent:
                parent.removeChild(item)
                if hasattr(tree, '_metadata_map'):
                    for i in range(parent.childCount()):
                        child = parent.child(i)
                        cid = id(child)
                        meta_child = tree._metadata_map.get(cid)
                        if meta_child and meta_child.get('type') == 'embedded_file':
                            if meta_child.get('index', -1) > index:
                                meta_child['index'] -= 1
                parent.setText(1, f"{len(main_file.embedded_uvars)} files")
            self.modified = True
            tree.viewport().update()

    def _refresh_tree(self, tree: 'QTreeWidget'):
        self.modified = True

    def _update_expr_item_labels(self, expr_item: 'QTreeWidgetItem', var):
        if var.expression:
            expr_item.setText(1, f"{len(var.expression.nodes)} nodes, {len(var.expression.relations)} relations")

    def _find_parent_expr_item(self, item: 'QTreeWidgetItem') -> 'QTreeWidgetItem':
        cur = item
        while cur and cur.text(0) != "🔗 Expression":
            cur = cur.parent()
        return cur

    def _get_expr(self, variable):
        if variable.expression is None:
            from file_handlers.uvar.uvar_expression import UvarExpression
            variable.expression = UvarExpression()
        return variable.expression

    def _expr_create_expression(self, tree: 'QTreeWidget', item: 'QTreeWidgetItem', meta: Dict):
        """Create an empty expression on a variable and add its UI item."""
        var = meta["variable"]
        expr = self._get_expr(var)
        expr_item = QTreeWidgetItem(item)
        expr_item.setText(0, "🔗 Expression")
        expr_item.setText(1, f"{len(expr.nodes)} nodes, {len(expr.relations)} relations")
        expr_item.setForeground(1, QBrush(QColor("#C586C0")))
        if hasattr(tree, '_metadata_map'):
            tree._metadata_map[id(expr_item)] = {"type": "expression", "variable": var}
        out_item = QTreeWidgetItem(expr_item)
        out_item.setText(0, "Output Node")
        out_item.setText(1, str(expr.output_node_id))
        out_item.setForeground(1, QBrush(QColor("#B5CEA8")))
        if hasattr(tree, '_metadata_map'):
            tree._metadata_map[id(out_item)] = {"type": "expression_output_node", "variable": var}
        item.setExpanded(True)
        self._refresh_tree(tree)

    def _find_output_node_item(self, expr_item: 'QTreeWidgetItem') -> 'QTreeWidgetItem | None':
        for i in range(expr_item.childCount()):
            child = expr_item.child(i)
            if child.text(0) == "Output Node":
                return child
        return None

    def _update_output_node_label(self, expr_item: 'QTreeWidgetItem', expr):
        out_item = self._find_output_node_item(expr_item)
        if out_item is not None:
            out_item.setText(1, str(expr.output_node_id))

    def _expr_set_output_node(self, tree: 'QTreeWidget', item: 'QTreeWidgetItem', meta: Dict):
        var = meta["variable"]
        expr = self._get_expr(var)
        max_id = len(expr.nodes) - 1
        if max_id < 0:
            QMessageBox.warning(tree, "No Nodes", "There are no nodes to select as output.")
            return
        value, ok = QInputDialog.getInt(tree, "Set Output Node", "Node id:", expr.output_node_id, 0, max_id)
        if not ok:
            return
        expr.output_node_id = value
        expr_item = self._find_parent_expr_item(item)
        if expr_item:
            self._update_output_node_label(expr_item, expr)
        self._refresh_tree(tree)

    def _expr_remove_expression(self, tree: 'QTreeWidget', item: 'QTreeWidgetItem', meta: Dict):
        """Remove the expression from a variable and delete its UI subtree."""
        var = meta["variable"]
        var.expression = None
        parent = item.parent()
        if parent is not None:
            idx = parent.indexOfChild(item)
            parent.takeChild(idx)
        self._refresh_tree(tree)

    def _expr_add_node(self, tree: 'QTreeWidget', item: 'QTreeWidgetItem', meta: Dict):
        var = meta["variable"]
        expr = self._get_expr(var)
        from file_handlers.uvar.uvar_node import UvarNode
        node = UvarNode()
        name, ok = QInputDialog.getText(tree, "Add Node", "Node name:")
        if not ok:
            return
        node.name = name
        node.node_id = len(expr.nodes)
        node.ukn_offset = 0
        node.ukn_count = 0
        expr.nodes.append(node)
        expr_item = self._find_parent_expr_item(item)
        if expr_item:
            nodes_group = None
            for i in range(expr_item.childCount()):
                grp = expr_item.child(i)
                if grp.text(0) == "🧩 Nodes":
                    nodes_group = grp
                    break
            if nodes_group is None:
                nodes_group = QTreeWidgetItem(expr_item)
                nodes_group.setText(0, "🧩 Nodes")
                nodes_group.setForeground(1, QBrush(QColor("#4EC9B0")))
                if hasattr(tree, '_metadata_map'):
                    tree._metadata_map[id(nodes_group)] = {"type": "expression_nodes", "variable": var}

            node_item = QTreeWidgetItem(nodes_group)
            node_item.setText(0, f"📦 {node.name or f'Node_{node.node_id}'}")
            node_item.setText(1, f"id {node.node_id}, 0 params")
            node_item.setForeground(1, QBrush(QColor("#888888")))

            if hasattr(tree, '_metadata_map'):
                tree._metadata_map[id(node_item)] = {"type": "expression_node", "node": node, "variable": var}

            outs_item = QTreeWidgetItem(node_item)
            outs_item.setText(0, "➡️ Outputs")
            outs_item.setText(1, "0")
            outs_item.setForeground(1, QBrush(QColor("#B5CEA8")))
            if hasattr(tree, '_metadata_map'):
                tree._metadata_map[id(outs_item)] = {"type": "expression_node_outputs", "node": node, "variable": var}

            ins_item = QTreeWidgetItem(node_item)
            ins_item.setText(0, "⬅️ Inputs")
            ins_item.setText(1, "0")
            ins_item.setForeground(1, QBrush(QColor("#B5CEA8")))
            if hasattr(tree, '_metadata_map'):
                tree._metadata_map[id(ins_item)] = {"type": "expression_node_inputs", "node": node, "variable": var}

            nodes_group.setText(1, str(len(expr.nodes)))
            expr_item.setExpanded(True)
            nodes_group.setExpanded(True)
            self._update_expr_item_labels(expr_item, var)
        self._refresh_tree(tree)

    def _expr_delete_node(self, tree: 'QTreeWidget', item: 'QTreeWidgetItem', meta: Dict):
        var = meta["variable"]
        node = meta["node"]
        expr = self._get_expr(var)
        deleted_id = node.node_id
        expr_item = self._find_parent_expr_item(item)
        if expr_item:
            to_remove = [r for r in expr.relations if r.src_node == deleted_id or r.dst_node == deleted_id]
            for rel in to_remove:
                self._expr_remove_relation_ui(tree, expr_item, var, rel)
        expr.relations = [r for r in expr.relations if r.src_node != deleted_id and r.dst_node != deleted_id]
        expr.nodes = [n for n in expr.nodes if n is not node]
        for i, n in enumerate(expr.nodes):
            n.node_id = i
        for r in expr.relations:
            if r.src_node > deleted_id:
                r.src_node -= 1
            if r.dst_node > deleted_id:
                r.dst_node -= 1
        if expr.output_node_id == deleted_id:
            if expr.nodes:
                value, ok = QInputDialog.getInt(tree, "Select New Output Node", "Node id:", 0, 0, len(expr.nodes) - 1)
                if not ok:
                    value = 0
                expr.output_node_id = value
            else:
                expr.output_node_id = 0
        elif expr.output_node_id > deleted_id:
            expr.output_node_id -= 1
        if expr_item:
            self._update_output_node_label(expr_item, expr)
            nodes_group = self._find_nodes_group(expr_item)
            if nodes_group is not None:
                for i in range(nodes_group.childCount()):
                    node_item = nodes_group.child(i)
                    for j in range(node_item.childCount()):
                        group_item = node_item.child(j)
                        for k in range(group_item.childCount()):
                            rel_item = group_item.child(k)
                            meta_map = getattr(tree, '_metadata_map', {})
                            meta_rel = meta_map.get(id(rel_item))
                            if meta_rel and meta_rel.get('type') == 'expression_relation':
                                rel_obj = meta_rel.get('relation')
                                rel_item.setText(0, self._get_relation_label(rel_obj))
        parent_item = item.parent()
        if parent_item:
            idx = parent_item.indexOfChild(item)
            parent_item.takeChild(idx)
            parent_item.setText(1, str(len(expr.nodes)))
            expr_item = self._find_parent_expr_item(parent_item)
            if expr_item:
                self._update_expr_item_labels(expr_item, var)
                self._refresh_all_io_counts(tree, expr_item, var)
        self._refresh_tree(tree)

    def _expr_rename_node(self, tree: 'QTreeWidget', item: 'QTreeWidgetItem', meta: Dict):
        node = meta["node"]
        old = node.name or ""
        name, ok = QInputDialog.getText(tree, "Rename Node", "Node name:", text=old)
        if not ok:
            return
        node.name = name
        item.setText(0, f"📦 {name}")
        self._refresh_tree(tree)

    def _expr_change_node_id(self, tree: 'QTreeWidget', item: 'QTreeWidgetItem', meta: Dict):
        var = meta["variable"]
        node = meta["node"]
        expr = self._get_expr(var)
        new_id, ok = QInputDialog.getInt(tree, "Change Node Id", "Node id:", value=node.node_id, min=0, max=100000)
        if not ok:
            return
        for r in expr.relations:
            if r.src_node == node.node_id:
                r.src_node = new_id
            if r.dst_node == node.node_id:
                r.dst_node = new_id
        node.node_id = new_id
        self._refresh_tree(tree)

    def _expr_add_param(self, tree: 'QTreeWidget', item: 'QTreeWidgetItem', meta: Dict):
        node = meta["node"]
        from file_handlers.uvar.uvar_types import NodeValueType
        types = [t.name for t in NodeValueType]
        type_name, ok = QInputDialog.getItem(tree, "Add Parameter", "Type:", types, 0, False)
        if not ok:
            return
        t = NodeValueType[type_name]
        name_hash_str, ok = QInputDialog.getText(tree, "Parameter Name Hash", "Hex (e.g., 0x1234abcd):", text="0x00000000")
        if not ok:
            return
        try:
            name_hash = int(name_hash_str, 16)
        except Exception:
            QMessageBox.warning(tree, "Invalid", "Invalid hex value")
            return
        value = None
        if t == NodeValueType.Int32:
            v, ok = QInputDialog.getInt(tree, "Parameter Value", "Int32:", 0)
            if not ok: 
                return
            value = v
        elif t == NodeValueType.UInt32Maybe:
            v, ok = QInputDialog.getInt(tree, "Parameter Value", "UInt32:", 0, 0, 0xFFFFFFFF)
            if not ok: 
                return
            value = v
        elif t == NodeValueType.Single:
            v, ok = QInputDialog.getDouble(tree, "Parameter Value", "Float:", 0.0)
            if not ok: 
                return
            value = v
        elif t == NodeValueType.Guid:
            v, ok = QInputDialog.getText(tree, "Parameter Value", "GUID:", text="00000000-0000-0000-0000-000000000000")
            if not ok: 
                return
            import uuid
            try:
                value = uuid.UUID(v)
            except Exception:
                QMessageBox.warning(tree, "Invalid", "Invalid GUID")
                return
        from file_handlers.uvar.node_parameter import NodeParameter
        p = NodeParameter()
        p.name_hash = name_hash
        p.type = t
        p.value = value
        node.parameters.append(p)
        p_item = QTreeWidgetItem(item)
        p_item.setText(0, f"🔹 0x{p.name_hash:08x}")
        p_item.setText(1, f"{p.type.name}: {p.value}")
        if hasattr(tree, '_metadata_map'):
            tree._metadata_map[id(p_item)] = {"type": "expression_node_param", "param": p, "node": node, "variable": meta["variable"]}
        item.setText(1, f"id {node.node_id}, {len(node.parameters)} params")
        self._refresh_tree(tree)

    def _expr_edit_param(self, tree: 'QTreeWidget', item: 'QTreeWidgetItem', meta: Dict):
        p = meta["param"]
        from file_handlers.uvar.uvar_types import NodeValueType
        if p.type == NodeValueType.Int32:
            v, ok = QInputDialog.getInt(tree, "Edit Parameter", "Int32:", int(p.value or 0))
            if ok: 
                p.value = v
        elif p.type == NodeValueType.UInt32Maybe:
            v, ok = QInputDialog.getInt(tree, "Edit Parameter", "UInt32:", int(p.value or 0), 0, 0xFFFFFFFF)
            if ok: 
                p.value = v
        elif p.type == NodeValueType.Single:
            v, ok = QInputDialog.getDouble(tree, "Edit Parameter", "Float:", float(p.value or 0.0))
            if ok: 
                p.value = v
        elif p.type == NodeValueType.Guid:
            v, ok = QInputDialog.getText(tree, "Edit Parameter", "GUID:", text=str(p.value) if p.value else "00000000-0000-0000-0000-000000000000")
            if ok:
                import uuid
                try:
                    p.value = uuid.UUID(v)
                except Exception:
                    QMessageBox.warning(tree, "Invalid", "Invalid GUID")
                    return
        self._refresh_tree(tree)

    def _expr_delete_param(self, tree: 'QTreeWidget', item: 'QTreeWidgetItem', meta: Dict):
        node = meta["node"]
        p = meta["param"]
        node.parameters = [x for x in node.parameters if x is not p]
        parent = item.parent()
        if parent:
            idx = parent.indexOfChild(item)
            parent.takeChild(idx)
            parent.setText(1, f"id {node.node_id}, {len(node.parameters)} params")
        self._refresh_tree(tree)

    def _expr_change_param_namehash(self, tree: 'QTreeWidget', item: 'QTreeWidgetItem', meta: Dict):
        p = meta["param"]
        old_text = f"0x{p.name_hash:08x}"
        new_text, ok = QInputDialog.getText(tree, "Change Parameter Name Hash", "Hex (e.g., 0x1234abcd):", text=old_text)
        if not ok:
            return
        try:
            new_hash = int(new_text, 16)
        except Exception:
            QMessageBox.warning(tree, "Invalid", "Invalid hex value")
            return
        p.name_hash = new_hash
        item.setText(0, f"🔹 0x{p.name_hash:08x}")
        self._refresh_tree(tree)

    def _expr_add_relation(self, tree: 'QTreeWidget', item: 'QTreeWidgetItem', meta: Dict):
        var = meta["variable"]
        expr = self._get_expr(var)
        sn, ok = QInputDialog.getInt(tree, "Add Relation", "Source node id:", 0)
        if not ok: 
            return
        sp, ok = QInputDialog.getInt(tree, "Add Relation", "Source port:", 0)
        if not ok:
            return
        dn, ok = QInputDialog.getInt(tree, "Add Relation", "Destination node id:", 0)
        if not ok:
            return
        dp, ok = QInputDialog.getInt(tree, "Add Relation", "Destination port:", 0)
        if not ok: 
            return
        existing_ids = {n.node_id for n in expr.nodes}
        if sn not in existing_ids or dn not in existing_ids:
            QMessageBox.warning(tree, "Invalid Relation", "Source or destination node id does not exist.")
            return
        if sn == dn:
            QMessageBox.warning(tree, "Invalid Relation", "Self-connections are not allowed.")
            return
        from file_handlers.uvar.uvar_types import NodeConnection
        rel = NodeConnection(src_node=sn, src_port=sp, dst_node=dn, dst_port=dp)
        expr.relations.append(rel)
        expr_item = self._find_parent_expr_item(item)
        if expr_item:
            self._expr_add_relation_ui(tree, expr_item, var, rel)
            self._update_expr_item_labels(expr_item, var)
        self._refresh_tree(tree)

    def _expr_edit_relation(self, tree: 'QTreeWidget', item: 'QTreeWidgetItem', meta: Dict):
        rel = meta["relation"]
        sn, ok = QInputDialog.getInt(tree, "Edit Relation", "Source node id:", rel.src_node)
        if not ok:
            return
        sp, ok = QInputDialog.getInt(tree, "Edit Relation", "Source port:", rel.src_port)
        if not ok:
            return
        dn, ok = QInputDialog.getInt(tree, "Edit Relation", "Destination node id:", rel.dst_node)
        if not ok:
            return
        dp, ok = QInputDialog.getInt(tree, "Edit Relation", "Destination port:", rel.dst_port)
        if not ok:
            return
        var = meta.get("variable")
        if var is not None:
            expr = self._get_expr(var)
            existing_ids = {n.node_id for n in expr.nodes}
            if sn not in existing_ids or dn not in existing_ids:
                QMessageBox.warning(tree, "Invalid Relation", "Source or destination node id does not exist.")
                return
            if sn == dn:
                QMessageBox.warning(tree, "Invalid Relation", "Self-connections are not allowed.")
                return
        rel.src_node, rel.src_port, rel.dst_node, rel.dst_port = sn, sp, dn, dp
        item.setText(0, f"{rel.src_node} [port: {rel.src_port}] → {rel.dst_node} [port: {rel.dst_port}]")
        self._refresh_tree(tree)

    def _expr_delete_relation(self, tree: 'QTreeWidget', item: 'QTreeWidgetItem', meta: Dict):
        var = meta["variable"]
        rel = meta["relation"]
        expr = self._get_expr(var)
        expr.relations = [r for r in expr.relations if r is not rel]
        parent = item.parent()
        if parent:
            idx = parent.indexOfChild(item)
            parent.takeChild(idx)
        expr_item = self._find_parent_expr_item(parent or item)
        if expr_item:
            self._expr_remove_relation_ui(tree, expr_item, var, rel)
            self._update_expr_item_labels(expr_item, var)
            self._refresh_all_io_counts(tree, expr_item, var)
        self._refresh_tree(tree)

    def _get_relation_label(self, rel):
        return f"{rel.src_node} [port: {rel.src_port}] → {rel.dst_node} [port: {rel.dst_port}]"

    def _find_nodes_group(self, expr_item: 'QTreeWidgetItem'):
        for i in range(expr_item.childCount()):
            grp = expr_item.child(i)
            if grp.text(0) == "🧩 Nodes":
                return grp
        return None

    def _find_node_item(self, tree: 'QTreeWidget', expr_item: 'QTreeWidgetItem', node_id: int):
        nodes_group = self._find_nodes_group(expr_item)
        if nodes_group is None:
            return None
        for i in range(nodes_group.childCount()):
            node_item = nodes_group.child(i)
            meta = getattr(tree, '_metadata_map', {}).get(id(node_item))
            if meta and meta.get('type') == 'expression_node':
                node = meta.get('node')
                if getattr(node, 'node_id', -1) == node_id:
                    return node_item
        return None

    def _find_io_groups(self, node_item: 'QTreeWidgetItem'):
        outs_item = None
        ins_item = None
        for i in range(node_item.childCount()):
            child = node_item.child(i)
            if child.text(0) == "➡️ Outputs":
                outs_item = child
            elif child.text(0) == "⬅️ Inputs":
                ins_item = child
        return outs_item, ins_item

    def _update_io_counts(self, expr, node_item: 'QTreeWidgetItem', node_id: int):
        outs_item, ins_item = self._find_io_groups(node_item)
        if outs_item is not None:
            outs = sum(1 for r in expr.relations if r.src_node == node_id)
            outs_item.setText(1, str(outs))
        if ins_item is not None:
            ins = sum(1 for r in expr.relations if r.dst_node == node_id)
            ins_item.setText(1, str(ins))

    def _refresh_all_io_counts(self, tree: 'QTreeWidget', expr_item: 'QTreeWidgetItem', var):
        expr = self._get_expr(var)
        nodes_group = self._find_nodes_group(expr_item)
        if nodes_group is None:
            return
        for i in range(nodes_group.childCount()):
            node_item = nodes_group.child(i)
            meta = getattr(tree, '_metadata_map', {}).get(id(node_item))
            if meta and meta.get('type') == 'expression_node':
                node = meta.get('node')
                if node is not None:
                    self._update_io_counts(expr, node_item, node.node_id)

    def _expr_add_relation_ui(self, tree: 'QTreeWidget', expr_item: 'QTreeWidgetItem', var, rel):
        expr = self._get_expr(var)
        src_node_item = self._find_node_item(tree, expr_item, rel.src_node)
        if src_node_item is not None:
            outs_item, _ = self._find_io_groups(src_node_item)
            if outs_item is None:
                outs_item = QTreeWidgetItem(src_node_item)
                outs_item.setText(0, "➡️ Outputs")
                outs_item.setForeground(1, QBrush(QColor("#B5CEA8")))
                if hasattr(tree, '_metadata_map'):
                    node_meta = getattr(tree, '_metadata_map', {}).get(id(src_node_item), {})
                    tree._metadata_map[id(outs_item)] = {"type": "expression_node_outputs", "node": node_meta.get('node'), "variable": var}
            rel_item = QTreeWidgetItem(outs_item)
            rel_item.setText(0, self._get_relation_label(rel))
            rel_item.setForeground(0, QBrush(QColor("#B5CEA8")))
            if hasattr(tree, '_metadata_map'):
                tree._metadata_map[id(rel_item)] = {"type": "expression_relation", "relation": rel, "variable": var}
            self._update_io_counts(expr, src_node_item, rel.src_node)
        dst_node_item = self._find_node_item(tree, expr_item, rel.dst_node)
        if dst_node_item is not None:
            _, ins_item = self._find_io_groups(dst_node_item)
            if ins_item is None:
                ins_item = QTreeWidgetItem(dst_node_item)
                ins_item.setText(0, "⬅️ Inputs")
                ins_item.setForeground(1, QBrush(QColor("#B5CEA8")))
                if hasattr(tree, '_metadata_map'):
                    node_meta = getattr(tree, '_metadata_map', {}).get(id(dst_node_item), {})
                    tree._metadata_map[id(ins_item)] = {"type": "expression_node_inputs", "node": node_meta.get('node'), "variable": var}
            rel_item = QTreeWidgetItem(ins_item)
            rel_item.setText(0, self._get_relation_label(rel))
            rel_item.setForeground(0, QBrush(QColor("#B5CEA8")))
            if hasattr(tree, '_metadata_map'):
                tree._metadata_map[id(rel_item)] = {"type": "expression_relation", "relation": rel, "variable": var}
            self._update_io_counts(expr, dst_node_item, rel.dst_node)

    def _expr_remove_relation_ui(self, tree: 'QTreeWidget', expr_item: 'QTreeWidgetItem', var, rel):
        expr = self._get_expr(var)
        src_node_item = self._find_node_item(tree, expr_item, rel.src_node)
        if src_node_item is not None:
            outs_item, _ = self._find_io_groups(src_node_item)
            if outs_item is not None:
                for i in reversed(range(outs_item.childCount())):
                    child = outs_item.child(i)
                    meta = getattr(tree, '_metadata_map', {}).get(id(child))
                    if meta and meta.get('type') == 'expression_relation' and meta.get('relation') is rel:
                        outs_item.takeChild(i)
                        break
                self._update_io_counts(expr, src_node_item, rel.src_node)
        dst_node_item = self._find_node_item(tree, expr_item, rel.dst_node)
        if dst_node_item is not None:
            _, ins_item = self._find_io_groups(dst_node_item)
            if ins_item is not None:
                for i in reversed(range(ins_item.childCount())):
                    child = ins_item.child(i)
                    meta = getattr(tree, '_metadata_map', {}).get(id(child))
                    if meta and meta.get('type') == 'expression_relation' and meta.get('relation') is rel:
                        ins_item.takeChild(i)
                        break
                self._update_io_counts(expr, dst_node_item, rel.dst_node)