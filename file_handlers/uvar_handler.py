import struct
import uuid
from typing import Optional, List, Any, Dict, Tuple

from PySide6.QtWidgets import (
    QMenu, QInputDialog, QMessageBox, 
    QTreeWidget, QTreeWidgetItem, QTreeView,
    QApplication
)
from PySide6.QtCore import Qt, QModelIndex
from PySide6.QtGui import QClipboard, QAction, QBrush, QColor
HAS_PYSIDE6 = True

from file_handlers.base_handler import FileHandler as BaseFileHandler
from file_handlers.uvar import (
    UVarFile, Variable, TypeKind, UvarFlags, 
    FileHandler as BinaryHandler, UVAR_MAGIC
)
from utils.hash_util import murmur3_hash

if HAS_PYSIDE6:
    from PySide6.QtCore import Signal
    from PySide6.QtWidgets import QWidget, QVBoxLayout
    
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
        if not HAS_PYSIDE6 or self.uvar_file is None:
            return
            
        if hasattr(tree, '__class__') and tree.__class__.__name__ == 'QTreeView':
            print(f"Warning: populate_treeview received QTreeView instead of QTreeWidget")
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
        
        if self.uvar_file.variables:
            vars_item = QTreeWidgetItem(parent_item or tree)
            vars_item.setText(0, f"📦 Variables")
            vars_item.setText(1, f"{len(self.uvar_file.variables)} items")
            vars_item.setForeground(1, QBrush(QColor("#4EC9B0")))
            metadata_map[id(vars_item)] = {"type": "variables_section", "file": self.uvar_file}
            
            for i, var in enumerate(self.uvar_file.variables):
                var_item = QTreeWidgetItem(vars_item)
                
                type_icon = self._get_type_icon(var.type)
                var_item.setText(0, f"{type_icon} {var.name or f'Variable_{i}'}")
                var_item.setText(1, self._format_variable_value(var))
                
                color = self._get_type_color(var.type)
                var_item.setForeground(1, QBrush(QColor(color)))
                
                metadata_map[id(var_item)] = {
                    "type": "variable",
                    "file": self.uvar_file,
                    "variable": var,
                    "index": i
                }
            
                self._add_variable_details(var_item, var, metadata_map)
                
                if var_item.childCount() > 0:
                    var_item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
                
        if self.uvar_file.embedded_uvars:
            embeds_item = QTreeWidgetItem(parent_item or tree)
            embeds_item.setText(0, f"📁 Embedded Files")
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
            vars_item.setText(0, f"📦 Variables")
            vars_item.setText(1, f"{len(embed.variables)} items")
            vars_item.setForeground(1, QBrush(QColor("#4EC9B0")))
            metadata_map[id(vars_item)] = {"type": "variables_section", "file": embed}
            
            for i, var in enumerate(embed.variables):
                var_item = QTreeWidgetItem(vars_item)
                
                type_icon = self._get_type_icon(var.type)
                var_item.setText(0, f"{type_icon} {var.name or f'Variable_{i}'}")
                var_item.setText(1, self._format_variable_value(var))
                
                color = self._get_type_color(var.type)
                var_item.setForeground(1, QBrush(QColor(color)))
                
                metadata_map[id(var_item)] = {
                    "type": "variable",
                    "file": embed,
                    "variable": var,
                    "index": i
                }
                
                self._add_variable_details(var_item, var, metadata_map)
                
                if var_item.childCount() > 0:
                    var_item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
                
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
        
        guid_mem_item = QTreeWidgetItem(parent)
        guid_mem_item.setText(0, "🔐 GUID (Memory)")
        guid_bytes = var.guid.bytes_le
        guid_mem_str = '-'.join([
            guid_bytes[0:4].hex(),
            guid_bytes[4:6].hex(),
            guid_bytes[6:8].hex(),
            guid_bytes[8:10].hex(),
            guid_bytes[10:16].hex()
        ])
        guid_mem_item.setText(1, guid_mem_str)
        guid_mem_item.setForeground(1, QBrush(QColor("#CE9178"))) 
        metadata_map[id(guid_mem_item)] = {"type": "variable_guid_memory", "variable": var}
        
        guid_item = QTreeWidgetItem(parent)
        guid_item.setText(0, "🔑 GUID (Normal)")
        guid_item.setText(1, str(var.guid))
        guid_item.setForeground(1, QBrush(QColor("#F44747")))
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
            
        parent.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
        
    def get_context_menu(self, tree: 'QTreeWidget', item: 'QTreeWidgetItem', meta: Dict) -> Optional['QMenu']:
        if not HAS_PYSIDE6 or not meta:
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
            
            menu.addSeparator()

            action = QAction("Delete Variable", menu)
            action.triggered.connect(lambda: self._delete_variable(tree, item, meta))
            menu.addAction(action)
            
        elif meta_type == "variable_guid":
            action = QAction("Copy GUID", menu)
            action.triggered.connect(lambda: self._copy_to_clipboard(item.text(1)))
            menu.addAction(action)
            
            action = QAction("Generate New GUID", menu)
            action.triggered.connect(lambda: self._generate_new_guid(tree, item, meta))
            menu.addAction(action)
            
        elif meta_type == "variable_guid_memory":
            action = QAction("Copy GUID (Memory)", menu)
            action.triggered.connect(lambda: self._copy_to_clipboard(item.text(1)))
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
            for i in range(count):
                name = f"{prefix}_{i}"
                var = target.add_variable(name, TypeKind.Single, 0.0)
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
                
                var_item = QTreeWidgetItem(item)
                type_icon = self._get_type_icon(var.type)
                var_item.setText(0, f"{type_icon} {name}")
                var_item.setText(1, self._format_variable_value(var))
                
                color = self._get_type_color(var.type)
                var_item.setForeground(1, QBrush(QColor(color)))
                
                tree._metadata_map[id(var_item)] = {
                    "type": "variable",
                    "file": file,
                    "variable": var,
                    "index": len(file.variables) - 1
                }
                
                self._add_variable_details(var_item, var, tree._metadata_map)
                
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
            
            var_item = QTreeWidgetItem(item)
            type_icon = self._get_type_icon(var.type)
            var_item.setText(0, f"{type_icon} {name}")
            var_item.setText(1, self._format_variable_value(var))
            
            color = self._get_type_color(var.type)
            var_item.setForeground(1, QBrush(QColor(color)))
            
            tree._metadata_map[id(var_item)] = {
                "type": "variable",
                "file": file,
                "variable": var,
                "index": len(file.variables) - 1
            }
            
            self._add_variable_details(var_item, var, tree._metadata_map)
        
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
        clipboard = QApplication.clipboard()
        clipboard.setText(text)
        
    def _generate_new_guid(self, tree: QTreeWidget, item: QTreeWidgetItem, meta: Dict):
        var = meta["variable"]
        var.guid = uuid.uuid4()
        item.setText(1, str(var.guid))
        self.modified = True
        
    def create_viewer(self):
        if not HAS_PYSIDE6:
            return None
        viewer = UvarViewer(self)
        viewer.populate_tree()
        return viewer