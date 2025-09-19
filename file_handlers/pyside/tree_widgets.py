import traceback
from PySide6.QtWidgets import (QLabel, QTreeView,
                               QHeaderView, QMenu, QMessageBox, QStyledItemDelegate,
                               QLineEdit, QInputDialog, QApplication, QDialog)
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QWidget, QHBoxLayout
from PySide6.QtCore import Qt, QModelIndex, QEvent


from .tree_core import TreeModel
from .component_selector import ComponentSelectorDialog 
from ui.template_manager_dialog import TemplateManagerDialog
from ui.template_export_dialog import TemplateExportDialog
from file_handlers.rsz.rsz_template_manager import RszTemplateManager

import file_handlers.rsz.rsz_array_clipboard as _rsz_array_cb

from .tree_widget_factory import TreeWidgetFactory

from file_handlers.rsz.rsz_embedded_array_operations import RszEmbeddedArrayOperations
from utils.translate_utils import (
    TranslationBatcher,
    TranslationManager,
    show_translation_error,
    show_translation_result,
)
                        
class AdvancedStyledDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.default_row_height = 24 
        
    def sizeHint(self, option, index):
        """Ensure consistent row height for all items"""
        size = super().sizeHint(option, index)
        
        tree_view = self.parent()
        if tree_view and hasattr(tree_view, 'default_row_height'):
            self.default_row_height = tree_view.default_row_height
            
        size.setHeight(self.default_row_height)
        return size

class AdvancedTreeView(QTreeView):
    TRANSLATION_CHAR_LIMIT = 2500
    """
    A QTreeView that uses an advanced delegate or a simpler delegate,
    and supports expansions.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setItemDelegate(AdvancedStyledDelegate()) 
        self.setUniformRowHeights(False)
        self.setExpandsOnDoubleClick(True)
        self.setHeaderHidden(True)  
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)
        self.parent_modified_callback = None
        self.resources_outdated = False
        _rsz_array_cb.RszArrayClipboard.on_resource_data_deserialized = self._mark_resources_outdated
        self.shift_pressed = False
        self.label_width = 150
        self.translation_manager = TranslationManager(self)
        self.translation_manager.translation_completed.connect(self._on_translation_completed)
        self._batch_translator = TranslationBatcher(
            self.translation_manager,
            parent=self,
            char_limit=self.TRANSLATION_CHAR_LIMIT,
        )
        self._translation_in_progress = False
        self.setSelectionMode(QTreeView.ExtendedSelection)

    def keyPressEvent(self, event):
        """Handle keyboard shortcuts and track shift key"""
        if event.key() == Qt.Key_Delete:
            indexes = self.selectedIndexes()
            if not indexes:
                return
                
            index = indexes[0]
            item = index.internalPointer()
            item_info = self._identify_item_type(item)

            if item_info.get('is_array_group'):
                return

            if item_info.get('is_embedded', False) and not item_info['is_array_element']:
                return
            
            if item_info['is_gameobject']:
                self.delete_gameobject(index)
            elif item_info['is_folder']:
                self.delete_folder(index)
            elif item_info['is_component'] and item_info['component_instance_id'] > 0:
                self.delete_component(index, item_info['component_instance_id'])
            elif item_info['is_resource']:
                resources_node = self._find_resources_node()
                selected = self.get_selected_resources(resources_node)
                self.delete_resources(selected)
            elif item_info['is_array_element'] and item_info['element_index'] >= 0:
                parent_array = item_info['parent_array_item']
                sel_idxs = self.get_selected_array_elements(parent_array)
                if len(sel_idxs) > 1:
                    self.delete_array_elements(parent_array, sel_idxs)
                else:
                    self.delete_array_element(parent_array, item_info['element_index'])
                
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        """Track shift key release"""
        super().keyReleaseEvent(event)

    def setModelData(self, root_data):
        """
        Helper to build a TreeModel from the nested dict.
        """
        model = TreeModel(root_data)
        self.setModel(model)
        header = self.header()
        header.setSectionResizeMode(0, QHeaderView.Stretch) 

    def embed_forms(self, parent_modified_callback=None):
        """Use TreeWidgetFactory for embedding widgets consistently"""
        self.parent_modified_callback = parent_modified_callback
        model = self.model()
        if not model:
            return

        def embed_children_on_expand(parent_index):
            modifiers = QApplication.keyboardModifiers()
            self.shift_pressed = bool(modifiers & Qt.ShiftModifier)
            
            # Create widgets for immediate children
            self.create_widgets_for_children(parent_index)
            
            if self.shift_pressed:
                self.expand_all_children(parent_index)
            if self.resources_outdated:
                self._apply_outdated_marker()

        # Only connect the signal
        self.expanded.connect(embed_children_on_expand)
        
        self.installEventFilter(self)

    def eventFilter(self, obj, event):
        """Reset shift_pressed when widget loses focus"""
        if event.type() == QEvent.FocusOut:
            self.shift_pressed = False
        return super().eventFilter(obj, event)

    def expand_all_children(self, parent_index):
        """Recursively expand all children of the given parent"""
        modifiers = QApplication.keyboardModifiers()
        self.shift_pressed = bool(modifiers & Qt.ShiftModifier)
        
        if not self.shift_pressed:
            return
            
        model = self.model()
        if not model:
            return

        rows = model.rowCount(parent_index)
        for row in range(rows):
            child_index = model.index(row, 0, parent_index)
            if child_index.isValid() and model.hasChildren(child_index):
                    self.expand(child_index)
                    self.create_widgets_for_children(child_index)
                    self.expand_all_children(child_index)

    def create_widgets_for_children(self, parent_index):
        """Create widgets for all children of the given parent index"""
        model = self.model()
        if not model:
            return

        rows = model.rowCount(parent_index)
        for row in range(rows):
            index0 = model.index(row, 0, parent_index)
            if not index0.isValid():
                continue
                
            item = index0.internalPointer()
            if not item or TreeWidgetFactory.should_skip_widget(item):
                continue

            name_text = item.data[0] if item.data else ""
            node_type = item.raw.get("type", "") if isinstance(item.raw, dict) else ""
            data_obj = item.raw.get("obj", None) if isinstance(item.raw, dict) else None
            
            widget = TreeWidgetFactory.create_widget(
                node_type, data_obj, name_text, self, self.parent_modified_callback
            )
            if widget:
                self.setIndexWidget(index0, widget)
                if self.parent().handler.auto_resource_management and node_type == "ResourceData":
                    from file_handlers.pyside.value_widgets import StringInput
                    widget.findChild(StringInput).valueChanged.connect(
                        lambda _=index0: self._on_resource_name_changed()
                    )

    def _on_resource_name_changed(self):
        self.resources_outdated = True
        self._apply_outdated_marker()

    def _apply_outdated_marker(self):
        model = self.model()
        resources_node = self._find_resources_node()
        idx = model.getIndexFromItem(resources_node)
        self.setIndexWidget(idx, None)
        container = QWidget(self)
        layout = QHBoxLayout(container)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(5)
        marker = QLabel("[OUTDATED]", container)
        marker.setStyleSheet("color: red;")
        layout.addWidget(marker)
        header = QLabel(resources_node.data[0], container)
        layout.addWidget(header)
        layout.addStretch()
        self.setIndexWidget(idx, container)

    def _mark_resources_outdated(self, _):
        if self.parent().handler.auto_resource_management and self.parent().handler.show_advanced: 
            self.resources_outdated = True
            self._apply_outdated_marker()

    def show_context_menu(self, position):
        """Show context menu for tree items"""
        index = self.indexAt(position)
        if not index.isValid():
            return
        item = index.internalPointer()
        item_info = self._identify_item_type(item)

        if item_info.get('is_array_group'):
            return

        menu = QMenu(self)
        parent_widget = self.parent()
        has_go_clipboard = parent_widget.handler.has_gameobject_clipboard_data(self)
        has_component_clipboard = parent_widget.handler.has_component_clipboard_data(self)


        if item.data and item.data[0] == "Data Block":
            exp_act = menu.addAction("Export Data Block")
            imp_act = menu.addAction("Import Data from Exports folder")
            translate_all_act = menu.addAction("Translate All GameObject Names")
            action = menu.exec_(QCursor.pos())
            if action == exp_act:
                from file_handlers.rsz.rsz_gameobject_clipboard import RszGameObjectClipboard
                RszGameObjectClipboard.export_datablock(self.parent())
            elif action == imp_act:
                self._show_import_randomization_dialog(index)
            elif action == translate_all_act:
                self.translate_all_gameobject_names()
            return

        handlers = {
            'is_resource': lambda: self._handle_resource_menu(menu, index, item_info),
            'is_resources_section': lambda: self._handle_resources_section_menu(menu),
            'is_component': lambda: self._handle_component_menu(menu, index, item_info),
            'is_gameobject': lambda: self._handle_gameobject_menu(menu, index, item_info, has_go_clipboard, has_component_clipboard, item),
            'is_folder': lambda: self._handle_folder_menu(menu, index, has_go_clipboard),
            'is_gameobjects_root': lambda: self._handle_root_menu(menu, index, has_go_clipboard),
            'is_folders_root': lambda: self._handle_root_folders_menu(menu, index, has_go_clipboard),
            'is_array': lambda: self._handle_array_menu(menu, index, item_info, item),
            'is_array_element': lambda: self._handle_array_element_menu(menu, index, item_info)
        }

        for key, handler in handlers.items():
            if item_info.get(key):
                handler()
                return

        if item_info.get('is_embedded'):
            return

    def _handle_resource_menu(self, menu, index, item_info):
        edit_action = menu.addAction("Edit Resource Path")
        delete_sel    = menu.addAction("Delete Selected Resources…")
        action = menu.exec_(QCursor.pos())
        if action == edit_action:
            self.edit_resource(index, item_info['resource_index'])
        elif action == delete_sel:
            resources_node = self._find_resources_node()
            sel = self.get_selected_resources(resources_node)
            self.delete_resources(sel)

    def _handle_resources_section_menu(self, menu):
        add_action = menu.addAction("Add New Resource")
        rebuild_action = None
        if(self.parent().handler.auto_resource_management):
            rebuild_action = menu.addAction("Refresh Resources List")
        action = menu.exec_(QCursor.pos())
        if action == add_action:
            self.add_resource()
        elif action == rebuild_action and action is not None:
            self._rebuild_resources_list()

    def _handle_component_menu(self, menu, index, item_info):
        copy_action = menu.addAction("Copy Component")
        delete_action = menu.addAction("Delete Component")
        action = menu.exec_(QCursor.pos())
        if action == delete_action:
            self.delete_component(index, item_info['component_instance_id'])
        elif action == copy_action:
            self.copy_component(item_info['component_instance_id'])

    def _handle_gameobject_menu(self, menu, index, item_info, has_go_clipboard, has_component_clipboard, item):
        menu.addAction("Add Component")
        if has_component_clipboard:
            menu.addAction("Paste Component")
        menu.addAction("Create Child GameObject")
        menu.addAction("Copy GameObject")
        if has_go_clipboard:
            menu.addAction("Paste GameObject as Child")
        menu.addAction("Template Manager")
        menu.addAction("Export as Template")
        menu.addAction("Translate Name")
        
        # Prefab handling
        parent_widget = self.parent()
        go_has_prefab = self._get_prefab_info(parent_widget, item_info, item)
        menu.addAction("Modify Prefab Path" if go_has_prefab else "Associate with Prefab")
        
        menu.addAction("Delete GameObject")
        action = menu.exec_(QCursor.pos())
        if(action): 
            self._process_gameobject_action(action, index)

    def _handle_folder_menu(self, menu, index, has_go_clipboard):
        menu.addAction("Create GameObject")
        if has_go_clipboard:
            menu.addAction("Paste GameObject")
        menu.addAction("Template Manager")
        menu.addAction("Delete Folder")
        menu.addAction("Translate Name")
        action = menu.exec_(QCursor.pos())
        if(action): 
            self._process_folder_action(action, index)

    def _handle_root_menu(self, menu, index, has_go_clipboard):
        menu.addAction("Create GameObject")
        if has_go_clipboard:
            menu.addAction("Paste GameObject")
        menu.addAction("Template Manager")
        action = menu.exec_(QCursor.pos())
        if action:
            self._process_root_action(action, index)

    def _handle_root_folders_menu(self, menu, index, has_go_clipboard):
        menu.addAction("Create Folder")          
        action = menu.exec_(QCursor.pos())
        if action:
            self._process_root_action(action, index)

    def _handle_folder_menu(self, menu, index, has_go_clipboard):
        menu.addAction("Create GameObject")
        menu.addAction("Create Sub-Folder")         
        if has_go_clipboard:
            menu.addAction("Paste GameObject")
        menu.addAction("Delete Folder")
        menu.addAction("Translate Name")
        action = menu.exec_(QCursor.pos())
        if action:
            self._process_folder_action(action, index)
            
    def _handle_array_menu(self, menu, index, item_info, item):
        if not item_info.get('data_obj'):
            return
            
        actions = {}
        add_action = menu.addAction("Add Element...")
        actions[add_action] = lambda: self.add_array_element(
            index, item_info['array_type'], item_info['data_obj'], item
        )

        parent = self.parent()
        clipboard = parent.handler.get_array_clipboard()
        if (clipboard.has_clipboard_data(self) and 
            clipboard.is_clipboard_compatible_with_array(self, item_info['array_type'])):
            
            elements_count = clipboard.get_elements_count_from_clipboard(self)
            if elements_count > 1:
                paste_action = menu.addAction(f"Paste Group ({elements_count} elements)")
                actions[paste_action] = lambda: self.paste_array_elements(
                    index, item_info['array_type'], item_info['data_obj'], item
                )
            else: 
                paste_action = menu.addAction("Paste Element")
                actions[paste_action] = lambda: self.paste_array_element(
                    index, item_info['data_obj'], item
                )
        
        action = menu.exec_(QCursor.pos())
        if action in actions:
            actions[action]()

    def _handle_array_element_menu(self, menu, index, item_info):
        if not (item_info['parent_array_item'] and 
                hasattr(item_info['parent_array_item'], 'raw')):
            return
            
        array_data = item_info['parent_array_item'].raw.get('obj')
        if not (array_data and 
                0 <= item_info['element_index'] < len(array_data.values)):
            return

        actions = {}
        selected_indices = self.get_selected_array_elements(item_info['parent_array_item'])
        
        if len(selected_indices) > 1:
            copy_action = menu.addAction(f"Copy Group ({len(selected_indices)} elements)")
            actions[copy_action] = lambda: self.copy_array_elements(
                item_info['parent_array_item'], selected_indices, index
            )
            
            delete_action = menu.addAction(f"Delete Group ({len(selected_indices)} elements)")
            actions[delete_action] = lambda: self.delete_array_elements(
                item_info['parent_array_item'], selected_indices
            )
        else:
            item = index.internalPointer()
            copy_action = menu.addAction("Copy Element")
            actions[copy_action] = lambda: self.copy_array_element(item)
            
            delete_action = menu.addAction("Delete Element")
            actions[delete_action] = lambda: self.delete_array_element(
                item_info['parent_array_item'], item_info['element_index']
            )
        
        action = menu.exec_(QCursor.pos())
        if action in actions:
            actions[action]()

    def _get_prefab_info(self, parent_widget, item_info, item):
        if not parent_widget.scn.is_scn:
            return False

        reasy_id = item.raw.get("reasy_id")
        go_instance_id = parent_widget.handler.id_manager.get_instance_id(reasy_id) if reasy_id else None
        if not go_instance_id:
            return False
        
        for i, instance_id in enumerate(parent_widget.scn.object_table):
            if instance_id == go_instance_id:
                for go in parent_widget.scn.gameobjects:
                    if go.id == i and go.prefab_id >= 0:
                        return True
        return False

    def _process_gameobject_action(self, action, index):
        # Map action text to handler methods
        action_handlers = {
            "Add Component": lambda: self.add_component_to_gameobject(index),
            "Paste Component": lambda: self.paste_component(index),
            "Create Child GameObject": lambda: self.create_child_gameobject(index),
            "Copy GameObject": lambda: self.copy_gameobject(index),
            "Paste GameObject as Child": lambda: self.paste_gameobject_as_child(index),
            "Export as Template": lambda: self.export_gameobject_as_template(index),
            "Template Manager": lambda: self.open_template_manager(index),
            "Translate Name": lambda: self.translate_node_text(index),
            "Delete GameObject": lambda: self.delete_gameobject(index),
            "Modify Prefab Path": lambda: self.manage_gameobject_prefab(index, True, ""),
            "Associate with Prefab": lambda: self.manage_gameobject_prefab(index, False, "")
        }
        handler = action_handlers.get(action.text())
        if handler:
            handler()

    def _process_folder_action(self, action, index):
        action_handlers = {
            "Create GameObject": self.create_gameobject_in_folder,
            "Create Sub-Folder": self.create_subfolder,
            "Paste GameObject": self.paste_gameobject_in_folder,
            "Delete Folder": self.delete_folder,
            "Translate Name": self.translate_node_text
        }
        handler = action_handlers.get(action.text())
        if handler:
            handler(index)

    def _process_root_action(self, action, index):
        action_handlers = {
            "Create GameObject": self.create_root_gameobject,
            "Create Folder": self.create_root_folder,     
            "Paste GameObject": self.paste_gameobject_at_root,
            "Template Manager": self.open_template_manager
        }
        handler = action_handlers.get(action.text())
        if handler:
            handler(index)
    
    def export_gameobject_as_template(self, index):
        """Export a GameObject as a template"""
            
        item = index.internalPointer()
        reasy_id = item.raw.get("reasy_id")
        parent_widget = self.parent()
        instance_id = parent_widget.handler.id_manager.get_instance_id(reasy_id)
        go_object_id = -1
        for i, obj_id in enumerate(parent_widget.scn.object_table):
            if obj_id == instance_id:
                go_object_id = i
                break
                
        if go_object_id < 0:
            QMessageBox.warning(self, "Error", "Could not find GameObject in object table")
            return
            
        go_name = ""
        if hasattr(item, 'data') and item.data:
            name_parts = item.data[0].split(' (ID:')
            go_name = name_parts[0]
        
        if not go_name:
            go_name = "GameObject"
            
        export_dialog = TemplateExportDialog(self, go_name)
        if export_dialog.exec() != QDialog.Accepted:
            return
            
        template_info = export_dialog.get_template_info()
        
        result = RszTemplateManager.export_gameobject_to_template(
            parent_widget,
            go_object_id,
            template_info["name"],
            template_info["tags"],
            template_info["description"]
        )
        
        if result["success"]:
            QMessageBox.information(self, "Success", result["message"])
        else:
            QMessageBox.warning(self, "Error", result["message"])

    def open_template_manager(self, index=None):
        """Open the template manager dialog"""
        parent_widget = self.parent()
        if not parent_widget:
            QMessageBox.warning(self, "Error", "Parent widget not available")
            return
            
        dialog = TemplateManagerDialog(self, parent_widget)
        
        dialog.template_imported.connect(self._on_template_imported)
        
        dialog.exec()
    
    def _on_template_imported(self, template_data):
        """Handle imported template - update UI if needed"""
        if not template_data or not template_data.get("success", False):
            return
            
        parent_id = template_data.get("parent_id", -1)
        parent_index = None
        
        if parent_id >= 0:
            model = self.model()
            for i in range(model.rowCount(QModelIndex())):
                root_child_idx = model.index(i, 0, QModelIndex())
                parent_index = self._find_node_by_object_id(model, root_child_idx, parent_id)
                if parent_index and parent_index.isValid():
                    break
                        
        # Add GameObject to UI
        self.add_gameobject_to_ui_direct(template_data, parent_index)

    def _find_node_by_object_id(self, model, start_index, object_id):
        """
        Helper method to find a tree node by GameObject/Folder object_id
        """
        item = start_index.internalPointer()

        if hasattr(item, 'raw') and isinstance(item.raw, dict):
            instance_id = item.raw.get('instance_id')
            if instance_id:
                for i, obj_id in enumerate(self.parent().scn.object_table):
                    if obj_id == instance_id and i == object_id:
                        return start_index
        
        for row in range(model.rowCount(start_index)):
            child_index = model.index(row, 0, start_index)
            result = self._find_node_by_object_id(model, child_index, object_id)
            if result:
                return result
                
        return None
    
    def _identify_item_type(self, item):
        """Identify item type and extract relevant information"""
        result = {
            'is_embedded': False, 'is_array': False, 'array_type': "", 'data_obj': None,
            'is_array_element': False, 'parent_array_item': None, 'element_index': -1,
            'is_gameobject': False, 'is_component': False, 'component_instance_id': -1,
            'is_folder': False, 'is_gameobjects_root': False, 'is_resource': False,
            'resource_index': -1, 'is_resources_section': False,
            'is_array_group': False,
        }
        
        result.update({
            "is_embedded": item.raw.get("embedded", False),
            "is_gameobject": item.raw.get("type") == "gameobject",
            "is_folder": item.raw.get("type") == "folder",
            "is_resource": item.raw.get("type") == "resource",
            "is_array": item.raw.get("type") == "array",
            "data_obj": item.raw.get("obj"),
            "resource_index": item.raw.get("resource_index", -1) if item.raw.get("type") == "resource" else -1,
            "is_array_group": item.raw.get("type") == "array_group",
        })
        
        if result['is_array'] and result['data_obj'] and hasattr(result['data_obj'], 'orig_type'):
            result['array_type'] = result['data_obj'].orig_type
        
        if not result['is_gameobject'] and item.parent and hasattr(item.parent, 'data') and item.parent.data[0] == "Components":
            if reasy_id := item.raw.get("reasy_id"):
                result['is_component'] = True
                result['component_instance_id'] = self.parent().handler.id_manager.get_instance_id(reasy_id)
        
        if item.data and item.data[0] == "Resources" and item.parent and item.parent.data[0] == "Advanced Information":
            result['is_resources_section'] = True
        elif item.data and item.data[0] == "Game Objects":
            result['is_gameobjects_root'] = True
        elif item.data and item.data[0] == "Folders":
            result['is_folders_root'] = True

        if (not result['is_array'] and not result['is_array_group']
                and item.parent and hasattr(item.parent, 'raw') and isinstance(item.parent.raw, dict)):
            parent_item = item.parent
            # Walk up through any array_group wrappers
            while parent_item and isinstance(parent_item.raw, dict) and parent_item.raw.get("type") == "array_group":
                parent_item = parent_item.parent

            if parent_item and isinstance(parent_item.raw, dict) and parent_item.raw.get("type") == "array":
                elem_obj = item.raw.get("obj") if isinstance(item.raw, dict) else None
                elem_index = item.raw.get("element_index")
                if elem_index is None:
                    elem_index = getattr(elem_obj, "_container_index", item.row())
                result.update({
                    'parent_array_item': parent_item,
                    'array_type': parent_item.raw['obj'].orig_type,
                    'is_array_element': True,
                    'element_index': elem_index,
                })
        
        return result
    
    @staticmethod
    def _get_element_type(array_type):
        """Extract element type from array type string"""
        if parsed := AdvancedTreeView.parse_complex_array_type(array_type):
            return parsed
        if array_type.endswith("[]"):
            return array_type[:-2]
        if array_type.startswith("System.Collections.Generic.List`1<") and array_type.endswith(">"):
            return array_type[array_type.index("<") + 1:array_type.rindex(">")]
        return array_type

    @staticmethod
    def _parse_params(inner):
        params = []
        current = []
        level = 0
        for char in inner:
            if char == '[':
                level += 1
            elif char == ']':
                level -= 1

            if char == ',' and level == 0:
                param = ''.join(current).strip()
                params.append(param.split(',', 1)[0].strip() if ',' in param else param)
                current = []
            else:
                current.append(char)
        if current:
            param = ''.join(current).strip()
            params.append(param.split(',', 1)[0].strip() if ',' in param else param)
        return params
    
    @staticmethod
    def parse_complex_array_type(type_string):
        """Parse complex array types like BaseType[[ParamType]][]"""
        if not type_string.endswith("[]") or "[[" not in type_string or "]][]" not in type_string:
            return None

        base_type, _, inner = type_string.partition("[[")
        inner = inner.rpartition("]]")[0]


        params = AdvancedTreeView._parse_params(inner)
        return f"{base_type}<{','.join(params)}>" if params else None

    def add_array_element(self, index, array_type, data_obj, array_item):
        """Add a new element to an array"""
        element_type = AdvancedTreeView._get_element_type(array_type)
        parent = self.parent()
        embedded_context = self._find_embedded_context(array_item)
        userdata_string = None
        
        if embedded_context == "userdata_array_needs_embedded":
            creator = parent.create_array_element
            print_context = "regular (UserData array)"
            embedded_context = None
        else:
            creator = RszEmbeddedArrayOperations(parent).create_array_element if embedded_context else parent.create_array_element
            print_context = "embedded" if embedded_context else "regular"
        
        if not embedded_context:
            try:
                from file_handlers.rsz.rsz_data_types import UserDataData
                is_userdata_array = getattr(data_obj, 'element_class', None) == UserDataData
            except Exception:
                is_userdata_array = False
            is_normal_rsz = not parent.scn.has_embedded_rsz
            if is_userdata_array and is_normal_rsz:
                from PySide6.QtWidgets import QInputDialog, QLineEdit
                from file_handlers.pyside.component_selector import ComponentSelectorDialog
                default_text = element_type or ""
                text, ok = QInputDialog.getText(
                    self,
                    "New UserData String",
                    "Enter UserData string:",
                    QLineEdit.Normal,
                    default_text
                )
                if ok:
                    userdata_string = text
                else: 
                    return
                
                type_dialog = ComponentSelectorDialog(self, parent.type_registry, required_parent_name="via.UserData")
                type_dialog.setWindowTitle("Select UserData Instance Type")
                
                if element_type:
                    try:
                        type_dialog.search_input.setText(element_type)
                    except Exception:
                        pass
                
                if not type_dialog.exec_():
                    return
                    
                selected_type = type_dialog.get_selected_component()
                if not selected_type:
                    return
                
                element_type = selected_type

        new_element = (
            creator(
                element_type, data_obj, embedded_context,
                direct_update=True, array_item=array_item
            )
            if embedded_context
            else creator(
                element_type,
                data_obj,
                direct_update=True,
                array_item=array_item,
                userdata_string=userdata_string,
            )
        )

        if not new_element:
            return

        self._refresh_array_node(array_item)
        self._scroll_to_array_end(array_item)

    def delete_array_element(self, parent_array_item, element_index):
        """Delete an element from an array with proper backend updates"""
            
        array_data = parent_array_item.raw.get('obj')

        if not self._display_confirmation(f"Delete element {element_index}?"):
            return
        
        embedded_context = self._find_embedded_context(parent_array_item)
        parent = self.parent()
        success = False

        if embedded_context == "userdata_array_needs_embedded":
            success = parent.array_operations.delete_array_element(array_data, element_index)
        elif embedded_context:
            success = RszEmbeddedArrayOperations(parent).delete_array_element(
                array_data, element_index, embedded_context
            )
        else:
            success = parent.array_operations.delete_array_element(array_data, element_index)
    
        if success:
            self._refresh_array_node(parent_array_item)
        else:
            QMessageBox.warning(self, "Error", "Failed to delete element in embedded context")

    def _find_embedded_context(self, item):
        from file_handlers.rsz.utils.rsz_embedded_utils import find_embedded_context
        return find_embedded_context(item)

    def _display_confirmation(self, message):
        if(not self.parent().handler.confirmation_prompt):
            return True
        msg_box = QMessageBox()
        msg_box.setIcon(QMessageBox.Warning)
        msg_box.setText(message)
        msg_box.setInformativeText("This action cannot be undone.")
        msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg_box.setDefaultButton(QMessageBox.No)
        return msg_box.exec_() == QMessageBox.Yes
    
    def delete_component(self, index, component_instance_id):
        """Delete a component from its GameObject"""
        if component_instance_id <= 0:
            QMessageBox.warning(self, "Error", "Invalid component")
            return
        
        parent = self.parent()

        item = index.internalPointer()
        reasy_id = item.raw.get("reasy_id")
        updated_instance_id = parent.handler.id_manager.get_instance_id(reasy_id)
        if updated_instance_id:
            component_instance_id = updated_instance_id
        
        if not self._display_confirmation("Delete Component"):
            return
        try:
            go_node = None
            components_node = index.parent()
            if components_node.isValid():
                go_index = components_node.parent()
                if go_index.isValid():
                    go_node = go_index
                    
            result = parent.delete_component_from_gameobject(component_instance_id)
            if result:
                if go_node and go_node.isValid():
                    self.remove_component_from_ui_direct(go_node, index)
                    QApplication.beep()
                    #QMessageBox.information(self, "Success", "Component deleted successfully")
                else:
                    QMessageBox.information(self, "Success", "Component deleted successfully, but failed to refresh UI. Please save and reload")
            else:
                QMessageBox.warning(self, "Error", "Failed to delete component")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error deleting component: {str(e)}")

    def create_gameobject_in_folder(self, folder_index):
        """Create a new GameObject in a folder"""
            
        folder_item = folder_index.internalPointer()
        reasy_id = folder_item.raw.get("reasy_id")
        parent = self.parent()

        folder_instance_id = parent.handler.id_manager.get_instance_id(reasy_id)
        if not folder_instance_id:
            QMessageBox.warning(self, "Error", "Could not determine folder instance ID")
            return
        # Get folder's object table index
        folder_object_id = -1
        for i, instance_id in enumerate(parent.scn.object_table):
            if instance_id == folder_instance_id:
                folder_object_id = i
                break
                
        if folder_object_id < 0:
            QMessageBox.warning(self, "Error", "Could not find folder in object table")
            return
            
        # Create dialog to get GameObject name
        name, ok = QInputDialog.getText(self, "New GameObject", "GameObject Name:", QLineEdit.Normal, "New GameObject")
        if not ok or not name:
            return
            
        try:
            # Create GameObject with folder as parent
            go_data = parent.object_operations.create_gameobject(name, folder_object_id)
            
            if go_data and go_data.get('success', False):
                self.add_gameobject_to_ui_direct(go_data, folder_index)
                QApplication.beep()
                #QMessageBox.information(self, "Success", f"GameObject '{name}' created successfully")
            else:
                QMessageBox.warning(self, "Error", "Failed to create GameObject")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error creating GameObject: {str(e)}")
    
    def create_child_gameobject(self, parent_go_index):
        """Create a new GameObject as a child of another GameObject"""
            
        parent_item = parent_go_index.internalPointer()
        reasy_id = parent_item.raw.get("reasy_id")
        parent_widget = self.parent()
        parent_instance_id = parent_widget.handler.id_manager.get_instance_id(reasy_id)
        parent_object_id = -1
        for i, instance_id in enumerate(parent_widget.scn.object_table):
            if instance_id == parent_instance_id:
                parent_object_id = i
                break
                
        if parent_object_id < 0:
            QMessageBox.warning(self, "Error", "Could not find GameObject in object table")
            return
        
        # Create dialog to get GameObject name
        name, ok = QInputDialog.getText(self, "New Child GameObject", "GameObject Name:", QLineEdit.Normal, "New GameObject")
        if not ok or not name:
            return
            
        try:
            # Create GameObject with parent GameObject as parent
            go_data = parent_widget.object_operations.create_gameobject(name, parent_object_id)
            
            if go_data and go_data.get('success', False):
                # Use the new direct UI update method
                self.add_gameobject_to_ui_direct(go_data, parent_go_index)
                QApplication.beep()
                #QMessageBox.information(self, "Success", f"Child GameObject '{name}' created successfully")
            else:
                QMessageBox.warning(self, "Error", "Failed to create child GameObject")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error creating child GameObject: {str(e)}")

    def create_root_folder(self, _):
        self._create_folder_ui(name_default="New Folder", parent_id=-1, parent_index=None)

    def create_subfolder(self, parent_folder_index):
        folder_item = parent_folder_index.internalPointer()
        reasy_id = folder_item.raw.get("reasy_id")
        parent_widget = self.parent()
        parent_instance_id = parent_widget.handler.id_manager.get_instance_id(reasy_id)

        parent_object_id = next((i for i, x in enumerate(parent_widget.scn.object_table)
                                if x == parent_instance_id), -1)
        if parent_object_id < 0:
            QMessageBox.warning(self, "Error", "Could not find folder in object table")
            return

        self._create_folder_ui("New Folder", parent_object_id, parent_folder_index)

    def _create_folder_ui(self, name_default: str, parent_id: int, parent_index):
        parent_widget = self.parent()
        name, ok = QInputDialog.getText(self, "New Folder", "Folder Name:",
                                        QLineEdit.Normal, name_default)
        if not ok or not name:
            return

        folder_data = parent_widget.object_operations.create_folder(name, parent_id)
        if not (folder_data and folder_data.get("success")):
            QMessageBox.warning(self, "Error", "Failed to create folder")
            return

        self.add_folder_to_ui_direct(folder_data, parent_index)
        QApplication.beep()
        #QMessageBox.information(self, "Success", f"Folder '{name}' created successfully")
    
    def _populate_folder_nodes(self, folder_dict, folder_data, viewer):
        """
        Fill the folder’s Settings node with its parsed RSZ fields,
        just like we already do for GameObjects.
        """
        settings_node = next((c for c in folder_dict["children"]
                            if c["data"][0] == "Settings"), None)
        if not settings_node:
            return

        instance_id = folder_data["instance_id"]
        fields      = viewer.scn.parsed_elements.get(instance_id, {})
        first = True
        for fname, fdata in fields.items():
            field_node = viewer._create_field_dict(fname, fdata)
            if first: 
                fdata.is_gameobject_or_folder_name = folder_dict
                first = False
            settings_node["children"].append(field_node)

    def add_folder_to_ui_direct(self, folder_data, parent_index=None):
        parent_widget = self.parent()
        model = self.model()

        parent_node = self._resolve_parent_node(parent_index, model)
        if not parent_node:
            parent_node = self._find_root_node_child("Data Block", "Folders")
            if not parent_node:
                print("Failed to find Folders node")
                return None

        folder_dict = {
            "data": [f"{folder_data['name']} (ID: {folder_data['instance_id']})", ""],
            "type": "folder",
            "instance_id": folder_data['instance_id'],
            "reasy_id": folder_data['reasy_id'],
            "children": [{"data": ["Settings", ""], "children": []},
                        {"data": ["Children", ""], "children": []}]
        }
        self._populate_folder_nodes(folder_dict, folder_data, parent_widget)
        model.addChild(parent_node, folder_dict)

        folder_index = self._get_new_node_index(parent_node, model)
        widget = TreeWidgetFactory.create_widget("folder", None,
                                                folder_dict["data"][0],
                                                self, self.parent_modified_callback)
        if widget:
            self.setIndexWidget(folder_index, widget)
        self.expand(model.getIndexFromItem(parent_node))
        self.scrollTo(folder_index)
        return folder_index

    def create_root_gameobject(self, index):
        """Create a new GameObject at the root level"""
        # Get the parent widget/handler for GameObject creation
        parent = self.parent()
        # Create dialog to get GameObject name
        name, ok = QInputDialog.getText(self, "New Root GameObject", "GameObject Name:", QLineEdit.Normal, "New GameObject")
        if not ok or not name:
            return
            
        try:
            go_data = parent.object_operations.create_gameobject(name, -1)
            
            if go_data and go_data.get('success', False):
                self.add_gameobject_to_ui_direct(go_data)
                QApplication.beep()
                #QMessageBox.information(self, "Success", f"GameObject '{name}' created successfully")
            else:
                QMessageBox.warning(self, "Error", "Failed to create GameObject")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error creating GameObject: {str(e)}")

    def add_component_to_gameobject(self, index):
        """Add a new component to a GameObject with autocomplete"""
        if not index.isValid():
            return
            
        item = index.internalPointer()
        reasy_id = item.raw.get("reasy_id")
        parent = self.parent()
        instance_id = parent.handler.id_manager.get_instance_id(reasy_id)
        if not instance_id:
            QMessageBox.warning(self, "Error", "Could not determine GameObject instance ID")
            return
        
        dialog = ComponentSelectorDialog(self, parent.type_registry, required_parent_name="via.Component")
        component_type = dialog.get_selected_component() if dialog.exec_() else None
        
        if not component_type:
            return
            
        try:
            result = parent.create_component_for_gameobject(instance_id, component_type)
            if isinstance(result, dict) and result.get('success', False):
                self.add_component_to_ui_direct(index, result)
                QApplication.beep()
                #QMessageBox.information(self, "Success", f"Added {component_type} to GameObject")
            elif result:
                QMessageBox.information(self, "Success", f"Added {component_type} to GameObject, but failed to refresh UI. Please save and reload")
            else:
                QMessageBox.warning(self, "Error", f"Failed to add {component_type}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to add component: {str(e)}")

    def delete_gameobject(self, index):
        """Delete a GameObject and all its children and components"""
            
        item = index.internalPointer()
        reasy_id = item.raw.get("reasy_id")
        parent = self.parent()
        instance_id = parent.handler.id_manager.get_instance_id(reasy_id)
        
        go_object_id = -1
        for i, obj_id in enumerate(parent.scn.object_table):
            if obj_id == instance_id:
                go_object_id = i
                break
        
        if go_object_id < 0:
            QMessageBox.warning(self, "Error", "Could not find GameObject in object table")
            return
        
        
        go_name = item.data[0].split(' (ID:')[0]
        
        details = f"Delete GameObject \"{go_name}\"?"
        
        details += "\nThis will delete the GameObject"
        
        go = next((g for g in parent.scn.gameobjects if g.id == go_object_id), None)
        details += f", {go.component_count} component(s) and all child GameObjects"

        if not self._display_confirmation(details):
            return
        
        success = parent.delete_gameobject(go_object_id)
        
        if success:
            model = self.model()
            parent_index = index.parent()

            row = -1
            parent_item = parent_index.internalPointer()
            for i, child in enumerate(parent_item.children):
                if child is item:
                    row = i
                    break
            
            if row != -1:
                model.removeRow(row, parent_index)
                QApplication.beep()
                #QMessageBox.information(self, "Success", "GameObject deleted successfully")
                return
        
            QMessageBox.information(self, "Success", "GameObject deleted successfully, but failed to refresh UI. Please save and reload")
        else:
            QMessageBox.warning(self, "Error", "Failed to delete GameObject")

    def _is_valid_parent_for_go(self, index):
        """Check if index is a valid parent for a GameObject node"""
        if not index.isValid():
            return False
        
        item = index.internalPointer()
        if not item or not hasattr(item, 'data') or not item.data:
            return False
        
        # Valid parents are "Game Objects", "Children" nodes, or folders
        if item.data[0] == "Game Objects" or item.data[0] == "Children":
            return True
        
        if hasattr(item, 'raw') and isinstance(item.raw, dict) and item.raw.get("type") == "folder":
            return True
        
        return False

    def delete_folder(self, index):
        """Delete a folder and all GameObjects and sub-folders within it"""
            
        folder_item = index.internalPointer()
        reasy_id = folder_item.raw.get("reasy_id")
        parent = self.parent()
        folder_instance_id = parent.handler.id_manager.get_instance_id(reasy_id)
        folder_object_id = -1
        for i, instance_id in enumerate(parent.scn.object_table):
            if instance_id == folder_instance_id:
                folder_object_id = i
                break
                
        
        folder_name = ""
        folder_name = folder_item.data[0].split(' (ID:')[0]
            
        details = f"Delete folder \"{folder_name}\"?"
        details += "\nThis will delete the folder and all sub-folder(s) and GameObject(s) within it"

        if not self._display_confirmation(details):
            return
        success = parent.delete_folder(folder_object_id)
        
        if success:
            model = self.model()
            parent_index = index.parent()
            parent_item = parent_index.internalPointer()
            for i, child in enumerate(parent_item.children):
                if child is folder_item:
                    model.removeRow(i, parent_index)
                    QApplication.beep()
                    #QMessageBox.information(self, "Success", f"Folder '{folder_name}' deleted successfully")
                    return
                
            QMessageBox.information(self, "Success", f"Folder '{folder_name}' deleted successfully, but failed to refresh UI directly.")
            return
        
        QMessageBox.warning(self, "Error", "Failed to delete folder")
                

    def manage_gameobject_prefab(self, index, has_prefab, current_path=""):
        """Create or modify prefab association for a GameObject"""

        item = index.internalPointer()
        reasy_id = item.raw.get("reasy_id")
        parent = self.parent()
        instance_id = parent.handler.id_manager.get_instance_id(reasy_id)
        go_object_id = -1
        for i, obj_id in enumerate(parent.scn.object_table):
            if obj_id == instance_id:
                go_object_id = i
                break
        target_go = None
        for go in parent.scn.gameobjects:
            if go.id == go_object_id:
                target_go = go
                break
        if target_go is None:
            QMessageBox.warning(self, "Error", "Could not find GameObject in object table")
            return
        if getattr(target_go, 'prefab_id', -1) >= 0:
            current_path = parent.scn._prefab_str_map[parent.scn.prefab_infos[target_go.prefab_id]]
        
        
        
        prefab_actions = {
            True: {
            "dialog_title": "Modify Prefab Path",
            "prompt_text": "Enter new prefab path:",
            "action_type": "modified"
            },
            False: {
            "dialog_title": "Associate with Prefab",
            "prompt_text": "Enter prefab path:",
            "action_type": "created"
            }
        }
        action_info = prefab_actions[has_prefab]
        dialog_title = action_info["dialog_title"]
        prompt_text = action_info["prompt_text"]

        while True:
            dialog = QInputDialog(self)
            dialog.setWindowTitle(dialog_title)
            dialog.setLabelText(prompt_text)
            dialog.setTextValue(current_path)
            dialog.setInputMode(QInputDialog.TextInput)
            dialog.resize(500, dialog.height())
            ok = dialog.exec_()
            path = dialog.textValue()
            if not ok:
                return
            if (path.strip() != ""):
                break
            QMessageBox.warning(self, "Invalid Input", "Prefab path cannot be empty. Please enter a valid path.")

        if not path.endswith(".pfb") and QMessageBox.question(
                self, "Add Extension?", 
                "Prefab paths typically end with .pfb. Do you want to add the .pfb extension?",
                QMessageBox.Yes | QMessageBox.No
            ) == QMessageBox.Yes:
                    path += ".pfb"
        
        if(parent.object_operations.manage_gameobject_prefab(target_go, path)):
            QApplication.beep()
            #QMessageBox.information(self, "Success", f"Prefab {action_type} successfully")
            return

        QMessageBox.warning(self, "Error", "Failed to manage prefab")

    def _rebuild_resources_list(self):
        viewer  = self.parent()
        viewer.handler.rsz_file.rebuild_resources()
        model    = self.model()
        res_node = self._find_resources_node()
        res_idx  = model.getIndexFromItem(res_node)

        new_section = viewer._create_resources_info()
        self.setIndexWidget(res_idx, None)

        while model.rowCount(res_idx) > 0:
            model.removeRow(0, res_idx)

        self.resources_outdated = False

        for child_dict in new_section.get("children", []):
            model.addChild(res_node, child_dict)

        self.create_widgets_for_children(res_idx)

        QMessageBox.information(
            self,
            "Rebuilt",
            f"Refreshed {len(new_section['children'])} resources.\n\nNote that this step is not necessary, as resources are automatically rebuilt on save."
        )

    def add_resource(self):
        """Add a new resource path directly in the tree view"""
        parent = self.parent()
        if not parent:
            QMessageBox.warning(self, "Error", "Resource management not supported")
            return
            
        path = self._get_resource_path_from_dialog("Add New Resource", "Enter resource path:", "")
        if not path:
            return
            
        try:
            resource_index = parent.add_resource(path)
            if resource_index < 0:
                QMessageBox.warning(self, "Error", "Failed to add resource")
                return
                
            if self.add_resource_to_ui_direct(path, resource_index):
                QApplication.beep()
                #QMessageBox.information(self, "Success", f"Added resource '{path}'")
            else:
                QMessageBox.information(self, "Success", f"Added resource '{path}', but failed to refresh UI. Please save and reload")
        except Exception as e:
            self._handle_resource_error("add", e)
    
    def add_resource_to_ui_direct(self, path, resource_index):
        """
        Add a resource directly to the UI tree without refreshing
        
        Args:
            path: Resource path string
            resource_index: Index of the resource in the resource_infos array
        
        Returns:
            bool: True if the UI was updated successfully
        """
        model = self.model()
        resources_node = self._find_resources_node()
        
        res_node = {
            "data": [path, ""],
            "type": "resource",
            "resource_index": resource_index
        }
        
        resources_index = model.getIndexFromItem(resources_node)
        if not resources_index.isValid():
            return False
        
        
        model.addChild(resources_node, res_node)
        
        child_index = model.index(len(resources_node.children) - 1, 0, resources_index)
        if child_index.isValid():
            widget = TreeWidgetFactory.create_widget("resource", None, path, self, self.parent_modified_callback)
            if widget:
                self.setIndexWidget(child_index, widget)
            
            self.expand(resources_index)
            self.scrollTo(child_index)
            return True
        
        return False

    def edit_resource(self, index, resource_index):
        """Edit a resource path directly in the tree view"""
        if resource_index < 0:
            QMessageBox.warning(self, "Error", "Invalid resource index")
            return
        
        parent = self.parent()
        if resource_index >= len(parent.scn.resource_infos):
            QMessageBox.warning(self, "Error", "Resource editing not supported or invalid index")
            return
        
        current_path = self._get_current_resource_path(resource_index)       
        path = self._get_resource_path_from_dialog("Edit Resource Path", "Update resource path:", current_path)
        if not path:
            return
        
        try:
            success = parent.manage_resource(resource_index, path)
            if not success:
                QMessageBox.warning(self, "Error", "Failed to update resource")
                return
            
            item = index.internalPointer()
            item.data[0] = path
            
            model = self.model()
            model.dataChanged.emit(index, index)
            
            widget = self.indexWidget(index)
            for label in widget.findChildren(QLabel):
                label.setText(path)
            QApplication.beep()
            #QMessageBox.information(self, "Success", f"Resource updated to '{path}'")
        except Exception as e:
            self._handle_resource_error("edit", e)
            
    def get_selected_resources(self, resources_node):
            """Return sorted list of resource_index values for all selected resource items."""
            selected = []
            model = self.model()
            if not model or not resources_node:
                return selected

            resources_index = model.getIndexFromItem(resources_node)
            if not resources_index.isValid():
                return selected

            sel = self.selectionModel().selectedIndexes()
            for idx in sel:
                if idx.parent() != resources_index:
                    continue
                item = idx.internalPointer()
                raw = getattr(item, 'raw', {})
                if raw.get('type') == 'resource':
                    ri = raw.get('resource_index', -1)
                    if ri >= 0:
                        selected.append(ri)
            return sorted(set(selected))
            
    def _remove_resource_ui(self, resource_index):
        """
        Remove the given resource row from the tree and
        update sibling indices and header count.
        Returns True on success.
        """
        model = self.model()
        resources_node = self._find_resources_node()
        if not (model and resources_node):
            return False

        resources_index = model.getIndexFromItem(resources_node)
        if not resources_index.isValid():
            return False

        row = next(
            (i for i, c in enumerate(resources_node.children)
             if getattr(c, 'raw', {}).get('resource_index') == resource_index),
            None
        )
        if row is None:
            return False

        model.removeRow(row, resources_index)
        self._update_remaining_resource_indices(resources_node, resource_index)
        return True
    
    def delete_resources(self, resource_indices):
        """Bulk‐delete resources and reuse the same UI helper for each."""
        if not resource_indices:
            return
        if not self._display_confirmation(f"Delete {len(resource_indices)} resources?"):
            return
        try:
            parent = self.parent()
            for ri in sorted(resource_indices, reverse=True):
                if not parent.delete_resource(ri):
                    QMessageBox.warning(self, "Error", f"Failed to delete resource #{ri}")
                    return

            for ri in sorted(resource_indices, reverse=True):
                self._remove_resource_ui(ri)
            QApplication.beep()
            #QMessageBox.information(self, "Success", f"Deleted {len(resource_indices)} resources")
        except Exception as e:
            self._handle_resource_error("delete", e)

    def _get_resource_path_from_dialog(self, title, label, default_text=""):
        """Show dialog to get resource path from user"""
        dialog = QInputDialog(self)
        dialog.setWindowTitle(title)
        dialog.setLabelText(label)
        dialog.setTextValue(default_text)
        dialog.setInputMode(QInputDialog.TextInput)
        dialog.resize(500, dialog.height())
        
        if not dialog.exec_():
            return None
        
        path = dialog.textValue()
        if not path or path.strip() == "":
            QMessageBox.warning(self, "Invalid Input", "Resource path cannot be empty.")
            return None
            
        return path

    def _get_current_resource_path(self, resource_index):
        """Get the current path for a resource"""
        parent = self.parent()
        try:
            if (hasattr(parent.scn, 'is_pfb16') and parent.scn.is_pfb16 and 
                hasattr(parent.scn, '_pfb16_direct_strings')):
                
                if resource_index < len(parent.scn._pfb16_direct_strings):
                    return parent.scn._pfb16_direct_strings[resource_index]
            
            resource = parent.scn.resource_infos[resource_index]
            if hasattr(parent.scn, 'get_resource_string'):
                return parent.scn.get_resource_string(resource) or "[Unknown]"
        except Exception as e:
            print(f"Error getting current resource path: {e}")
        return "[Unknown]"

    def _confirm_resource_deletion(self, resource_path):
        """Show confirmation dialog for resource deletion"""
        return self._display_confirmation(f"Delete resource '{resource_path}'?")
    
    def _update_resources_ui(self, success_message):
        """Update resources UI with success message"""
        QMessageBox.information(self, "Success", success_message)

    def _handle_resource_error(self, operation, error):
        """Handle resource operation error"""
        QMessageBox.critical(self, "Error", f"Failed to {operation} resource: {str(error)}")
        print(f"Exception details: {traceback.format_exc()}")
        
    def _find_resource_row(self, children, resource_index):
        """Find the row index for a resource by its resource_index"""
        for i, child in enumerate(children):
            if (hasattr(child, 'raw') and isinstance(child.raw, dict) and 
                child.raw.get("type") == "resource" and 
                child.raw.get("resource_index") == resource_index):
                return i
        return -1
        
    def _update_remaining_resource_indices(self, resources_node, deleted_index):
        """Update indices for remaining resources after deletion"""
        for child in resources_node.children:
            if (hasattr(child, 'raw') and isinstance(child.raw, dict) and 
                child.raw.get("type") == "resource"):
                current_index = child.raw.get("resource_index", -1)
                if current_index > deleted_index:
                    child.raw["resource_index"] = current_index - 1

    def _find_resources_node(self):
        """Helper to find the resources section node under Advanced Information"""
        return self._find_root_node_child("Advanced Information", "Resources")

    
    def add_gameobject_to_ui_direct(self, go_data, parent_index=None):
        if isinstance(go_data, list):
            for child_data in go_data:
                self.add_gameobject_to_ui_direct(child_data, parent_index)
            return None

        parent_widget = self.parent()
        model = self.model()
        parent_node = self._resolve_parent_node(parent_index, model)
        
        if not parent_node:
            parent_node = self._find_root_node_child("Data Block", "Game Objects")
            if not parent_node:
                print("Failed to find GameObjects node for root GameObject placement")
                return None

        go_dict = self._create_base_gameobject_dict(go_data)
        self._populate_gameobject_nodes(go_dict, go_data, parent_widget)
        model.addChild(parent_node, go_dict)
        
        go_index = self._get_new_node_index(parent_node, model)
        self._create_node_widget(go_index)
        
        if go_index and go_index.isValid() and go_data.get('children'):
            self._add_children_recursively(go_index, go_data['children'], model)
        
        return go_index

    def _resolve_parent_node(self, parent_index, model):
        if not parent_index:
            return None

        parent_item = parent_index.internalPointer()
        
        if self._is_children_node(parent_item):
            return parent_item
        
        if self._is_folder_node(parent_item):
           return self._find_or_create_children_node(parent_item, model)
        
        if self._is_gameobject_node(parent_item):
            return self._find_or_create_children_node(parent_item, model)
        
        return None

    def _is_children_node(self, item):
        return item.data and item.data[0] == "Children"

    def _is_folder_node(self, item):
        return (hasattr(item, 'raw') and 
                isinstance(item.raw, dict) and 
                item.raw.get('type') == 'folder')

    def _is_gameobject_node(self, item):
        return (hasattr(item, 'raw') and 
                isinstance(item.raw, dict) and 
                item.raw.get('type') == 'gameobject')

    def _find_or_create_children_node(self, parent_item, model):
        for child in parent_item.children:
            if self._is_children_node(child):
                return child
        
        children_node = {"data": ["Children", ""], "children": []}
        model.addChild(parent_item, children_node)
        
        for child in parent_item.children:
            if self._is_children_node(child):
                return child
        return None

    def _create_base_gameobject_dict(self, go_data):
        instance_id = go_data.get('instance_id', 0)
        name = go_data.get('name', 'GameObject')
        display_name = f"{name} (ID: {instance_id})"
        
        return {
            "data": [display_name, ""],
            "type": "gameobject",
            "instance_id": instance_id,
            "reasy_id": go_data.get('reasy_id', 0),
            "children": [],
        }

    def _populate_gameobject_nodes(self, go_dict, go_data, parent_widget):
        settings_node = {"data": ["Settings", ""], "children": []}
        go_dict["children"].append(settings_node)
        
        instance_id = go_dict["instance_id"]
        fields = parent_widget.scn.parsed_elements[instance_id]
        
        for field_name, field_data in fields.items():
            field_node = parent_widget._create_field_dict(field_name, field_data)
            if field_name == ("Name"):
                field_data.is_gameobject_or_folder_name = go_dict
            settings_node["children"].append(field_node)
        
        component_count = go_data.get('component_count', 0)
        go_id = go_data.get('go_id', -1)
        
        if component_count > 0 and go_id >= 0:
            comp_node = {"data": ["Components", ""], "children": []}
            go_dict["children"].append(comp_node)
            
            for i in range(1, component_count + 1):
                component_go_id = go_id + i
                    
                comp_instance_id = parent_widget.scn.object_table[component_go_id]
                    
                reasy_id = parent_widget.handler.id_manager.get_reasy_id_for_instance(comp_instance_id)
                component_name = parent_widget.name_helper.get_instance_name(comp_instance_id)
                comp_dict = {
                    "data": [f"{component_name} (ID: {comp_instance_id})", ""],
                    "instance_id": comp_instance_id,
                    "reasy_id": reasy_id,
                    "children": [],
                }
                
                for f_name, f_data in parent_widget.scn.parsed_elements[comp_instance_id].items():
                    comp_dict["children"].append(parent_widget._create_field_dict(f_name, f_data))
                        
                comp_node["children"].append(comp_dict)
        
        if go_data.get('children'):
            go_dict["children"].append({"data": ["Children", ""], "children": []})

    def _get_new_node_index(self, parent_node, model):
        parent_index = model.getIndexFromItem(parent_node)
        if not parent_index.isValid():
            return None
            
        return model.index(len(parent_node.children) - 1, 0, parent_index)

    def _create_node_widget(self, go_index):
        if not go_index or not go_index.isValid():
            return
            
        go_item = go_index.internalPointer()
        if not (go_item and hasattr(go_item, 'data')):
            return
            
        name_text = go_item.data[0] if go_item.data else ""
        node_type = go_item.raw.get("type", "") if isinstance(go_item.raw, dict) else ""
        
        widget = TreeWidgetFactory.create_widget(
            node_type, None, name_text, self, self.parent_modified_callback
        )
        if widget:
            self.setIndexWidget(go_index, widget)

    def _add_children_recursively(self, go_index, children_data, model):
        go_item = go_index.internalPointer()
        for i, child in enumerate(go_item.children):
            if child.data and child.data[0] == "Children":
                children_node_index = model.index(i, 0, go_index)
                if children_node_index.isValid():
                    for child_data in children_data:
                        self.add_gameobject_to_ui_direct(child_data, children_node_index)
                break
    
    def _find_root_node_child(self, root_node_name, node_name):

        model = self.model()
        data_index = None

        for i in range(model.rowCount()):
            child_index = model.index(i, 0, QModelIndex())
            if not child_index.isValid():
                continue         
            child_text = model.data(child_index)
            if child_text.startswith(root_node_name):
                data_index = child_index
                break
        
        if not data_index or not data_index.isValid():
            print("Could not find Data node")
            return None
        
        data_count = model.rowCount(data_index)
        for i in range(data_count):
            child_index = model.index(i, 0, data_index)
            if not child_index.isValid():
                continue
                
            child_text = model.data(child_index)
            if child_text.startswith(node_name):
                return child_index.internalPointer()

        print(f"Could not find {node_name} node under {root_node_name}")
        return None
    
    def find_user_file_array_node(self):
        """Find the target array node in user file: Data Block → first child → first child"""
        model = self.model()
        if not model:
            return None
        
        try:
            from PySide6.QtCore import QModelIndex
            
            # Find Data Block node
            data_block_item = None
            for row in range(model.rowCount(QModelIndex())):
                index = model.index(row, 0, QModelIndex())
                item = index.internalPointer()
                if item and hasattr(item, 'data') and item.data and item.data[0].startswith("Data Block"):
                    data_block_item = item
                    break
            
            if not data_block_item or not data_block_item.children:
                return None
            
            first_child = data_block_item.children[0]
            if not first_child or not hasattr(first_child, 'children') or not first_child.children:
                return None
            
            target_array_node = first_child.children[0]
            return target_array_node
        except Exception as e:
            print(f"Error finding user file array node: {e}")
            return None
    
    def add_component_to_ui_direct(self, go_index, component_data):
        """
        Add a component directly to the UI tree without refreshing
        
        Args:
            go_index: QModelIndex of the GameObject
            component_data: Dictionary with component data from creation operation
        """
        go_item = go_index.internalPointer()
        parent = self.parent()
        model = self.model()
        
        def find_components_node(item):
            return next(
                (child for child in item.children
                if child.data and child.data[0] == "Components"),
                None
            )

        components_node = find_components_node(go_item)
        if not components_node:
            components_dict = {"data": ["Components", ""], "children": []}
            model.addChild(go_item, components_dict)
            components_node = find_components_node(go_item)

        component_instance_id = component_data.get('instance_id', 0)
        component_type = component_data.get('type_name', 'Unknown')
        reasy_id = component_data.get('reasy_id', 0)
        
        component_name = component_type
                
        display_name = component_name
        
        component_dict = {
            "data": [display_name, ""], 
            "type": "component",
            "instance_id": component_instance_id,
            "reasy_id": reasy_id,
            "children": [],
        }
        
        fields = parent.scn.parsed_elements[component_instance_id]
        for field_name, field_data in fields.items():
            component_dict["children"].append(
                parent._create_field_dict(field_name, field_data)
            )
        
        model.addChild(components_node, component_dict)
        
        components_index = model.getIndexFromItem(components_node)
        component_index = model.index(len(components_node.children) - 1, 0, components_index)
        item = component_index.internalPointer()
        name_text = item.data[0] if item.data else ""
        
        widget = TreeWidgetFactory.create_widget(
            "component", None, name_text, self, self.parent_modified_callback
        )
        self.setIndexWidget(component_index, widget)
                        
        self.expand(components_index)
        self.scrollTo(component_index)
        return True

    def remove_component_from_ui_direct(self, go_index, component_index):
        """
        Remove a component directly from the UI tree without refreshing
        
        Args:
            go_index: QModelIndex of the GameObject
            component_index: QModelIndex of the component to remove
        """
        if not go_index.isValid() or not component_index.isValid():
            return False
            
        go_item = go_index.internalPointer()
        if not go_item or not hasattr(go_item, 'raw'):
            return False
            
        model = self.model()
        if not model:
            return False
        
        components_index = component_index.parent()
        if not components_index.isValid():
            return False
        
        components_item = components_index.internalPointer()
        if not components_item or not hasattr(components_item, 'data') or components_item.data[0] != "Components":
            return False
        
        component_row = component_index.row()
        
        if model.removeRow(component_row, components_index):
            return True
        
        return False
    
    def copy_data_block(self):
        from file_handlers.rsz.rsz_gameobject_clipboard import RszGameObjectClipboard
        parent_widget = self.parent()
        ok = RszGameObjectClipboard.copy_datablock_to_clipboard(parent_widget)
        if ok:
            QMessageBox.information(self, "Success", "Copied Data Block to clipboard folder")
        else:
            QMessageBox.warning(self, "Error", "Failed to copy Data Block")

    def paste_data_block(self, parent_index):
        from file_handlers.rsz.rsz_gameobject_clipboard import RszGameObjectClipboard
        parent_widget = self.parent()
        pasted_nodes = RszGameObjectClipboard.paste_datablock_from_clipboard(parent_widget, parent_folder_id=-1, parent_index=parent_index, no_parent_folder=True)
        if pasted_nodes:
            QApplication.beep()
        else:
            QMessageBox.warning(self, "Error", "Failed to paste Data Block")

    def copy_array_elements(self, parent_array_item, element_indices, index):
        """Copy multiple array elements to clipboard"""
        embedded_context = self._find_embedded_context(parent_array_item)
        
        # Handle special case for userdata arrays
        if embedded_context == "userdata_array_needs_embedded":
            embedded_context = None
            
        array_data = parent_array_item.raw.get('obj')
        elements = []
        for idx in element_indices:
            if idx < len(array_data.values):
                elements.append(array_data.values[idx])
        
        if not elements:
            QMessageBox.warning(self, "Error", "No valid elements selected")
            return
            
        array_type = array_data.orig_type if hasattr(array_data, 'orig_type') else ""
        
        parent = self.parent()
        try:
            clipboard = parent.handler.get_array_clipboard()
            success = clipboard.copy_multiple_to_clipboard(self, elements, array_type, embedded_context)
            if success:
                QMessageBox.information(self, "Success", f"{len(elements)} elements copied to clipboard")
            else:
                QMessageBox.warning(self, "Error", "Failed to copy elements")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error copying elements: {str(e)}")
            traceback.print_exc()

    def paste_array_elements(self, index, array_type, data_obj, array_item):
        """Paste multiple array elements from clipboard"""
        parent = self.parent()
        embedded_context = self._find_embedded_context(array_item)
        
        if embedded_context == "userdata_array_needs_embedded":
            array_operations = parent.array_operations
            embedded_context = None
        else:
            array_operations = RszEmbeddedArrayOperations(parent) if embedded_context else parent.array_operations
            
        try:
            clipboard = parent.handler.get_array_clipboard()
            elements = clipboard.paste_elements_from_clipboard(
                self, array_operations, data_obj, array_item, embedded_context)

            if elements:
                self._refresh_array_node(array_item)
                self._scroll_to_array_end(array_item)
                QApplication.beep()
            else:
                QMessageBox.warning(self, "Error", "Failed to paste elements. Make sure the clipboard contains compatible elements.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to paste elements: {str(e)}")
            traceback.print_exc()

    def translate_node_text(self, index):
        """Translate the name of a GameObject or folder using Google Translate API"""
        if self._translation_in_progress:
            QMessageBox.information(self, "Translation", "A translation is already in progress.")
            return

        item = index.internalPointer()
        name_text = item.data[0]
        if " (ID:" in name_text:
            name_text = name_text.split(" (ID:")[0]

        if not name_text or name_text.strip() == "":
            show_translation_error(self, "No text to translate")
            return

        parent_widget = self.parent()
        target_lang = parent_widget.handler.app.settings.get("translation_target_language", "en")

        original_id_part = item.data[0].replace(name_text, "") if " (ID:" in item.data[0] else ""

        context = {
            "index": index,
            "original_id_part": original_id_part,
            "batch": False,
            "show_result": True,
        }

        self.setCursor(Qt.WaitCursor)

        success = self.translation_manager.translate_text(
            text=name_text,
            source_lang="auto",
            target_lang=target_lang,
            context=context
        )

        if not success:
            self.setCursor(Qt.ArrowCursor)
            show_translation_error(self, "Failed to start translation")
            return

        self._translation_in_progress = True

    def _apply_translated_text_to_item(self, index, translated_text, original_id_part):
        if not index or not index.isValid():
            return False

        item = index.internalPointer()
        if not item or not hasattr(item, "data") or not item.data:
            return False

        new_text = translated_text + original_id_part
        item.data[0] = new_text

        model = self.model()
        if model:
            model.dataChanged.emit(index, index)

            widget = self.indexWidget(index)
            if widget:
                labels = widget.findChildren(QLabel)
                if len(labels) >= 2:
                    text_label = labels[1]
                    text_label.setText(new_text)

        return True

    def _on_translation_completed(self, translated_text, context):
        """Handle translation completion"""
        context = context or {}

        if self._batch_translator.handle_response(translated_text, context):
            return

        self.setCursor(Qt.ArrowCursor)

        if not translated_text:
            self._translation_in_progress = False
            show_translation_error(self, "Unable to translate text")
            return

        index = context.get("index")
        original_id_part = context.get("original_id_part", "")

        if not index or not index.isValid():
            self._translation_in_progress = False
            show_translation_error(self, "Invalid item index")
            return

        if not self._apply_translated_text_to_item(index, translated_text, original_id_part):
            self._translation_in_progress = False
            show_translation_error(self, "Invalid item")
            return

        if context.get("show_result", True):
            show_translation_result(self, translated_text)

        self._translation_in_progress = False

    def translate_all_gameobject_names(self):
        """Translate all GameObject names under the Data Block."""
        if self._translation_in_progress:
            QMessageBox.information(self, "Translation", "A translation is already in progress.")
            return

        model = self.model()
        if not model:
            return

        parent_widget = self.parent()
        if not parent_widget or not hasattr(parent_widget, "handler"):
            return

        target_lang = parent_widget.handler.app.settings.get("translation_target_language", "en")
        if not target_lang:
            target_lang = self.translation_manager.default_target_language

        game_objects_root = self._find_root_node_child("Data Block", "Game Objects")
        if not game_objects_root:
            QMessageBox.warning(self, "Translation", "Could not locate the Game Objects node.")
            return
        entries = []
        skipped = 0

        for display_text, index in self._iter_gameobject_nodes(model, game_objects_root):
            name_text, original_id_part = self._split_display_name(display_text)
            cleaned = name_text.strip()
            if not cleaned or len(cleaned) > self.TRANSLATION_CHAR_LIMIT:
                skipped += 1
                continue
            entries.append({"index": index, "original_id_part": original_id_part, "text": cleaned})

        if not entries:
            message = "No GameObject names available for translation."
            if skipped:
                message += f"\nSkipped {skipped} entries due to missing, invalid or oversized names."
            QMessageBox.information(self, "Translation", message)
            return

        def apply_entry(entry, translated_value):
            index = entry.get("index")
            if not index or not index.isValid():
                return False
            return self._apply_translated_text_to_item(index, translated_value, entry.get("original_id_part", ""))

        def finish_batch(stats):
            stats = stats or {}
            summary = [f"Translated {stats.get('success', 0)} of {stats.get('total', 0)} GameObject names."]
            if stats.get("failed"):
                summary.append(f"Failed: {stats['failed']}")
            skipped_total = stats.get("skipped", 0)
            if skipped_total:
                summary.append(f"Skipped: {skipped_total} (missing, invalid or oversized names)")
            if stats.get("requests"):
                summary.append(f"Requests sent: {stats['requests']}")

            QMessageBox.information(self, "Translation", "\n".join(summary))
            self._translation_in_progress = False
            self.setCursor(Qt.ArrowCursor)

        started, info = self._batch_translator.start(
            entries,
            target_lang,
            apply_entry,
            finish_batch,
            initial_skipped=skipped,
        )

        total_skipped = info.get("skipped", skipped)
        if not started:
            message = info.get("error") or "Unable to start translation."
            if total_skipped:
                message += f"\nSkipped: {total_skipped} (missing, invalid or oversized names)"
            QMessageBox.warning(self, "Translation", message)
            return

        if self._batch_translator.is_running():
            self._translation_in_progress = True
            self.setCursor(Qt.WaitCursor)

    def _iter_gameobject_nodes(self, model, root_item):
        stack = [root_item]

        while stack:
            item = stack.pop()
            for row in range(item.child_count()):
                child = item.child(row)
                if not child:
                    continue

                raw = child.raw if isinstance(child.raw, dict) else {}
                node_type = raw.get("type", "")
                label = child.data[0] if child.data else ""

                if node_type == "gameobject":
                    index = model.getIndexFromItem(child)
                    if index and index.isValid():
                        yield label, index

                if node_type in {"gameobject", "folder"} or label in {"Game Objects", "Children"}:
                    stack.append(child)

    @staticmethod
    def _split_display_name(display_text):
        if not display_text:
            return "", ""

        if " (ID:" in display_text:
            name, delim, rest = display_text.partition(" (ID:")
            return name, delim + rest

        return display_text, ""

    def delete_array_elements(self, parent_array_item, element_indices):
        """Delete multiple array elements with proper backend updates"""

        array_data = parent_array_item.raw.get('obj')
        
        if not self._display_confirmation(f"Delete {len(element_indices)} elements?"):
            return
        
        element_indices = sorted(element_indices, reverse=True)
        
        embedded_context = self._find_embedded_context(parent_array_item)
        parent = self.parent()
        success = True
        
        if embedded_context == "userdata_array_needs_embedded":
            embedded_context = None

        if embedded_context:
            rsz_operations = RszEmbeddedArrayOperations(parent)
            for idx in element_indices:
                if not rsz_operations.delete_array_element(array_data, idx, embedded_context):
                    success = False
                    break
        else:
            for idx in element_indices:
                if not parent.delete_array_element(array_data, idx):
                    success = False
                    break
        if success:
            self._refresh_array_node(parent_array_item)
            QApplication.beep()
            #QMessageBox.information(self, "Success", f"Deleted {len(element_indices)} elements successfully")
        else:
            QMessageBox.warning(self, "Error", "Failed to delete all elements")

    def _refresh_array_node(self, array_item):
        """Rebuild UI nodes for an array after modifications"""
        model = self.model()
        if not model:
            return

        array_index = model.getIndexFromItem(array_item)
        if not array_index.isValid():
            return

        count = array_item.child_count()
        if count > 0:
            model.removeRows(0, count, array_index)

        parent_widget = self.parent()
        builder = getattr(parent_widget, "lazy_builder", None)
        if not builder:
            return

        data_obj = array_item.raw.get('obj')
        embedded_context = self._find_embedded_context(array_item)
        builder_context = (
            None if embedded_context == "userdata_array_needs_embedded" else embedded_context
        )
        node = builder.create_lazy_array_node(
            array_item.data[0].split(':')[0], data_obj, builder_context
        )
        children_raw = []
        if node.get("deferred_builder"):
            children_raw = node["deferred_builder"].build()

        widget = self.indexWidget(array_index)
        label = widget.findChild(QLabel)
        label.setText(f"{array_item.data[0]} <span style='color: #666;'>(Array: {len(data_obj.values)} items)</span>")

        model.addChildren(array_item, children_raw)
        self.expand(array_index)
        self.create_widgets_for_children(array_index)

    def _scroll_to_array_end(self, array_item):
        """Scroll view to the last actual element of an array"""
        model = self.model()
        if not model:
            return

        array_index = model.getIndexFromItem(array_item)
        if not array_index.isValid() or array_item.child_count() == 0:
            return

        last_child_idx = model.index(array_item.child_count() - 1, 0, array_index)
        last_child_item = last_child_idx.internalPointer()
        raw = last_child_item.raw if isinstance(last_child_item.raw, dict) else {}

        if raw.get("type") == "array_group":
            self.expand(last_child_idx)
            self.create_widgets_for_children(last_child_idx)
            if last_child_item.child_count() > 0:
                final_idx = model.index(
                    last_child_item.child_count() - 1, 0, last_child_idx
                )
                self.scrollTo(final_idx)
        else:
            self.scrollTo(last_child_idx)

    def get_selected_array_elements(self, parent_array_item):
        """Get indices of selected array elements"""
        selected_indices = []
        model = self.model()
        if not model:
            return selected_indices

        parent_index = model.getIndexFromItem(parent_array_item)
        if not parent_index.isValid():
            return selected_indices

        selection_model = self.selectionModel()
        if not selection_model:
            return selected_indices

        for index in selection_model.selectedIndexes():
            if not index.isValid():
                continue

            # Verify the selected item belongs to the target array
            ancestor = index
            belongs = False
            while ancestor.isValid():
                if ancestor == parent_index:
                    belongs = True
                    break
                ancestor = ancestor.parent()
            if not belongs:
                continue

            item = index.internalPointer()
            if not item or not isinstance(item.raw, dict):
                continue
            if "element_index" in item.raw:
                selected_indices.append(item.raw["element_index"])
                continue
            elem_obj = item.raw.get("obj")
            if elem_obj is None:
                continue
            elem_idx = getattr(elem_obj, "_container_index", -1)
            if elem_idx >= 0:
                selected_indices.append(elem_idx)

        return sorted(set(selected_indices))
        
    def copy_component(self, component_instance_id):
        """Copy a component to clipboard for pasting to another GameObject"""
        if component_instance_id <= 0:
            QMessageBox.warning(self, "Error", "Invalid component")
            return
        
        parent = self.parent()
        try:
            success = parent.handler.copy_component_to_clipboard(parent, component_instance_id)
            if success:
                QMessageBox.information(self, "Success", "Component copied to clipboard")
            else:
                QMessageBox.warning(self, "Error", "Failed to copy component")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error copying component: {str(e)}")
            
    def paste_component(self, index):
        """Paste a component from clipboard to a GameObject"""
        if not index.isValid():
            return
            
        item = index.internalPointer()
        reasy_id = item.raw.get("reasy_id")
        parent = self.parent()
        instance_id = parent.handler.id_manager.get_instance_id(reasy_id)
        clipboard_data = parent.handler.get_component_clipboard_data(self)
        if not clipboard_data:
            QMessageBox.warning(self, "Error", "No component data in clipboard")
            return
            
        type_name = clipboard_data.get("type_name", "Component")
        
        try:
            result = parent.handler.paste_component_from_clipboard(parent, instance_id, clipboard_data)
            
            if result and result.get("success", False):
                self.add_component_to_ui_direct(index, result)
                QApplication.beep()
                #QMessageBox.information(self, "Success", f"Pasted {type_name} to GameObject")
            else:
                QMessageBox.warning(self, "Error", f"Failed to paste {type_name}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error pasting component: {str(e)}")

    def copy_gameobject(self, index):
        """Copy a GameObject to clipboard"""
        item = index.internalPointer()
        reasy_id = item.raw.get("reasy_id")
        parent = self.parent()
        instance_id = parent.handler.id_manager.get_instance_id(reasy_id)
        if not instance_id:
            QMessageBox.warning(self, "Error", "Could not determine GameObject instance ID")
            return
            
        go_object_id = -1
        for i, obj_id in enumerate(parent.scn.object_table):
            if obj_id == instance_id:
                go_object_id = i
                break
                
        if go_object_id < 0:
            QMessageBox.warning(self, "Error", "Could not find GameObject in object table")
            return
        embedded_context = self._find_embedded_context(item)      
        
        if embedded_context == "userdata_array_needs_embedded":
            embedded_context = None
            
        try:
            success = parent.handler.copy_gameobject_to_clipboard(parent, go_object_id, embedded_context)
            if success:
                go_name = ""
                if hasattr(item, 'data') and item.data:
                    go_name = item.data[0].split(' (ID:')[0]
                    
                msg = f"GameObject '{go_name}' copied to clipboard"
                QMessageBox.information(self, "Success", msg)
            else:
                QMessageBox.warning(self, "Error", "Failed to copy GameObject to clipboard")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error copying GameObject: {str(e)}")
            traceback.print_exc()

    def paste_gameobject_in_folder(self, folder_index):
        """Paste a GameObject from clipboard into a folder"""
        folder_item = folder_index.internalPointer()
        reasy_id = folder_item.raw.get("reasy_id")
        parent = self.parent()
        folder_instance_id = parent.handler.id_manager.get_instance_id(reasy_id)
        folder_object_id = -1
        for i, instance_id in enumerate(parent.scn.object_table):
            if instance_id == folder_instance_id:
                folder_object_id = i
                break
                
        if folder_object_id < 0:
            QMessageBox.warning(self, "Error", "Could not find folder in object table")
            return
            
        self._paste_gameobject_common(folder_object_id, folder_index)

    def paste_gameobject_at_root(self, _):
        """Paste a GameObject from clipboard at the root level"""
        self._paste_gameobject_common(-1, None)

    def paste_gameobject_as_child(self, parent_go_index):
        """Paste a GameObject from clipboard as a child of another GameObject"""
        parent_item = parent_go_index.internalPointer()
        # Get parent GameObject ID using reasy_id for stable reference
        reasy_id = parent_item.raw.get("reasy_id")
        parent_widget = self.parent()
        parent_instance_id = parent_widget.handler.id_manager.get_instance_id(reasy_id)
        parent_object_id = -1
        for i, instance_id in enumerate(parent_widget.scn.object_table):
            if instance_id == parent_instance_id:
                parent_object_id = i
                break
                
        if parent_object_id < 0:
            QMessageBox.warning(self, "Error", "Could not find GameObject in object table")
            return
            
        self._paste_gameobject_common(parent_object_id, parent_go_index)
            
    def _paste_gameobject_common(self, parent_object_id, parent_index=None):
        """
        Common logic for pasting a GameObject from clipboard
        
        Args:
            parent_object_id: Object table index of the parent (-1 for root)
            parent_index: QModelIndex of the parent node for UI updating (None for root)
        """
        parent_widget = self.parent()
        
        clipboard_data = parent_widget.handler.get_gameobject_clipboard_data(self)
        if not clipboard_data:
            QMessageBox.warning(self, "Error", "Failed to load GameObject data from clipboard")
            return
            
        default_name = clipboard_data.get("name", "GameObject")
        if default_name.strip() == "":
            default_name = "GameObject"
            
        new_name, ok = QInputDialog.getText(self, "Paste GameObject", 
                                          "Enter name for the pasted GameObject:", 
                                          QLineEdit.Normal, default_name)
        if not ok:
            return
            
        try:
            go_data = parent_widget.handler.paste_gameobject_from_clipboard(
                parent_widget, parent_object_id, new_name, clipboard_data
            )
            if go_data and go_data.get('success', False):
                self.add_gameobject_to_ui_direct(go_data, parent_index)
                QApplication.beep()
                #QMessageBox.information(self, "Success", f"GameObject '{new_name}' pasted successfully")
            else:
                QMessageBox.warning(self, "Error", "Failed to paste GameObject")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error pasting GameObject: {str(e)}")

    def copy_array_element(self, element_item):
        """Copy an array element to clipboard"""

        if not element_item or not hasattr(element_item, 'raw'):
            return

        parent_array_item = element_item.parent
        while parent_array_item and isinstance(parent_array_item.raw, dict) \
                and parent_array_item.raw.get("type") == "array_group":
            parent_array_item = parent_array_item.parent

        if not parent_array_item or parent_array_item.raw.get("type") != "array":
            QMessageBox.warning(self, "Error", "Invalid array element selection")
            return

        array_data = parent_array_item.raw.get('obj')
        elem_obj = element_item.raw.get('obj') if isinstance(element_item.raw, dict) else None
        if not array_data:
            QMessageBox.warning(self, "Error", "Failed to access array element")
            return

        element_index = element_item.raw.get("element_index")
        if element_index is None:
            if elem_obj is None:
                QMessageBox.warning(self, "Error", "Failed to access array element")
                return
            element_index = getattr(elem_obj, '_container_index', element_item.row())
        embedded_context = self._find_embedded_context(parent_array_item)
        if embedded_context == "userdata_array_needs_embedded":
            embedded_context = None

        element = array_data.values[element_index]
        array_type = array_data.orig_type if hasattr(array_data, 'orig_type') else ""

        parent = self.parent()
        try:
            clipboard = parent.handler.get_array_clipboard()
            success = clipboard.copy_to_clipboard(self, element, array_type, embedded_context)
            if success:
                QMessageBox.information(self, "Success", "Element copied to clipboard")
            else:
                QMessageBox.warning(self, "Error", "Failed to copy element")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error copying element: {str(e)}")

    def paste_array_element(self, index, data_obj, array_item):
        """Paste an element from clipboard to an array"""
        parent = self.parent()
        embedded_context = self._find_embedded_context(array_item)
        
        if embedded_context == "userdata_array_needs_embedded":
            array_operations = parent.array_operations
            embedded_context = None
        else:
            array_operations = RszEmbeddedArrayOperations(parent) if embedded_context else parent.array_operations
            
        try:
            clipboard = parent.handler.get_array_clipboard()
            _ = clipboard.paste_elements_from_clipboard(
                self, array_operations, data_obj, array_item, embedded_context)

            if not self.isExpanded(index):
                self.expand(index)
            self._refresh_array_node(array_item)
            self._scroll_to_array_end(array_item)
            QApplication.beep()
            #QMessageBox.information(self, "Success", "Element pasted successfully.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to paste element: {str(e)}")

    def _show_import_randomization_dialog(self, parent_index):
        """Show dialog for import randomization options"""
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QCheckBox, QPushButton, QLabel
        
        dialog = QDialog(self)
        dialog.setWindowTitle("Import Options")
        dialog.setModal(True)
        dialog.resize(400, 200)
        
        layout = QVBoxLayout(dialog)
        
        desc_label = QLabel("Choose whether to randomize IDs during import:")
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)
        
        randomize_ids = QCheckBox("Randomize GUIDs, Context IDs..")
        randomize_ids.setChecked(True)
        randomize_ids.setToolTip("Generate new IDs for GameObjects, instances, and userdata instead of preserving original ones")
        layout.addWidget(randomize_ids)
        
        note_label = QLabel("Note: Internal references and relationships will be preserved regardless of ID randomization.")
        note_label.setWordWrap(True)
        note_label.setStyleSheet("color: gray; font-size: 10px;")
        layout.addWidget(note_label)
        
        button_layout = QHBoxLayout()
        cancel_button = QPushButton("Cancel")
        import_button = QPushButton("Import")
        import_button.setDefault(True)
        
        button_layout.addWidget(cancel_button)
        button_layout.addStretch()
        button_layout.addWidget(import_button)
        layout.addLayout(button_layout)
        
        cancel_button.clicked.connect(dialog.reject)
        import_button.clicked.connect(dialog.accept)
        
        if dialog.exec_() == QDialog.Accepted:
            from file_handlers.rsz.rsz_gameobject_clipboard import RszGameObjectClipboard
            result = RszGameObjectClipboard.import_datablock(
                self.parent(),
                parent_folder_id=-1,
                parent_index=parent_index,
                randomize_ids=randomize_ids.isChecked()
            )
            
            if result:
                QApplication.beep()
            else:
                QMessageBox.warning(self, "Import Failed", "No items were imported or import was cancelled.")