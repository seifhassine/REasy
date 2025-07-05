import traceback
from PySide6.QtWidgets import (QWidget, QLabel, QTreeView, QCheckBox, 
                               QHeaderView, QSpinBox, QMenu, QDoubleSpinBox, QMessageBox, QStyledItemDelegate,
                               QLineEdit, QInputDialog, QApplication, QDialog)
from PySide6.QtGui import QCursor
from PySide6.QtCore import Qt, QModelIndex, QEvent

from .tree_core import TreeModel
from .component_selector import ComponentSelectorDialog 
from ui.template_manager_dialog import TemplateManagerDialog
from ui.template_export_dialog import TemplateExportDialog
from file_handlers.rsz.rsz_template_manager import RszTemplateManager

from .tree_widget_factory import TreeWidgetFactory

from file_handlers.rsz.rsz_embedded_array_operations import RszEmbeddedArrayOperations
from utils.translate_utils import TranslationManager, show_translation_error, show_translation_result
                        
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
        self.shift_pressed = False
        self.label_width = 150
        # Create translation manager
        self.translation_manager = TranslationManager(self)
        self.translation_manager.translation_completed.connect(self._on_translation_completed)
        # Enable multi-selection for array elements
        self.setSelectionMode(QTreeView.ExtendedSelection)

    def keyPressEvent(self, event):
        """Handle keyboard shortcuts and track shift key"""
        if event.key() == Qt.Key_Delete:
            indexes = self.selectedIndexes()
            if not indexes:
                return
                
            index = indexes[0] 
            if not index.isValid():
                return
                
            item = index.internalPointer()
            if not item:
                return
                
            item_info = self._identify_item_type(item)
            
            if item_info.get('is_embedded', False) and not item_info['is_array_element']:
                return
            
            if item_info['is_gameobject']:
                self.delete_gameobject(index)
            elif item_info['is_folder']:
                self.delete_folder(index)
            elif item_info['is_component'] and item_info['component_instance_id'] > 0:
                self.delete_component(index, item_info['component_instance_id'])
            elif item_info['is_resource'] and item_info['resource_index'] >= 0:
                self.delete_resource(index, item_info['resource_index'])
            elif item_info['is_array_element'] and item_info['element_index'] >= 0:
                self.delete_array_element(item_info['parent_array_item'], item_info['element_index'], index)
                
        # Call the parent's keyPressEvent for default behavior
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        """Track shift key release"""
        super().keyReleaseEvent(event)

    def setModelData(self, rootData):
        """
        Helper to build a TreeModel from the nested dict.
        """
        model = TreeModel(rootData)
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
            if child_index.isValid():
                if model.hasChildren(child_index):
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

    def show_context_menu(self, position):
        """Show context menu for tree items"""
        index = self.indexAt(position)
        if not index.isValid():
            return
            
        item = index.internalPointer()
        if not item:
            return
        
        item_info = self._identify_item_type(item)
        
        menu = QMenu(self)
        
        parent_widget = self.parent()
        has_go_clipboard = False
        
        has_go_clipboard = parent_widget.handler.has_gameobject_clipboard_data(self)
        has_component_clipboard = parent_widget.handler.has_component_clipboard_data(self)

        if item_info['is_resource']:
            # Context menu for a resource entry
            edit_action = menu.addAction("Edit Resource Path")
            delete_action = menu.addAction("Delete Resource")
            action = menu.exec_(QCursor.pos())
            
            if action == edit_action:
                self.edit_resource(index, item_info['resource_index'])
            elif action == delete_action:
                self.delete_resource(index, item_info['resource_index'])
                
        elif item_info['is_resources_section']:
            add_action = menu.addAction("Add New Resource")
            action = menu.exec_(QCursor.pos())
            
            if action == add_action:
                self.add_resource(index)
                
        elif item_info['is_component']:
            copy_action = menu.addAction("Copy Component")
            delete_action = menu.addAction("Delete Component")
            
            action = menu.exec_(QCursor.pos())
            
            if action == delete_action:
                self.delete_component(index, item_info['component_instance_id'])
            elif action == copy_action:
                self.copy_component(index, item_info['component_instance_id'])
        
        elif item_info['is_gameobject']:
            add_component_action = menu.addAction("Add Component")
            paste_as_child_action = None
            paste_component_action = None
            if has_component_clipboard:
                paste_component_action = menu.addAction("Paste Component")
            create_child_go_action = menu.addAction("Create Child GameObject")
            copy_action = menu.addAction("Copy GameObject")
            if has_go_clipboard:
                paste_as_child_action = menu.addAction("Paste GameObject as Child")
            template_manager_action = menu.addAction("Template Manager")
                
            
            export_template_action = menu.addAction("Export as Template")
            
    
            
            parent_widget = self.parent()
            
            translate_action = menu.addAction("Translate Name")
            
            
            go_has_prefab = False
            prefab_path = ""
            
            if hasattr(parent_widget, "scn") and not parent_widget.scn.is_pfb and not parent_widget.scn.is_usr:
                reasy_id = item.raw.get("reasy_id")
                go_instance_id = parent_widget.handler.id_manager.get_instance_id(reasy_id) if reasy_id else None
                
                go_object_id = -1
                for i, instance_id in enumerate(parent_widget.scn.object_table):
                    if instance_id == go_instance_id:
                        go_object_id = i
                        break
                
                if go_object_id >= 0:
                    target_go = None
                    for go in parent_widget.scn.gameobjects:
                        if go.id == go_object_id:
                            target_go = go
                            break
                            
                    if target_go and hasattr(target_go, 'prefab_id'):
                        if target_go.prefab_id >= 0:
                            go_has_prefab = True
                            
                            if (hasattr(parent_widget.scn, 'prefab_infos') and 
                                target_go.prefab_id < len(parent_widget.scn.prefab_infos)):
                                prefab = parent_widget.scn.prefab_infos[target_go.prefab_id]
                                
                                if hasattr(parent_widget.scn, 'get_prefab_string'):
                                    prefab_path = parent_widget.scn.get_prefab_string(prefab)
            
            manage_prefab_action = None
            if go_has_prefab:
                manage_prefab_action = menu.addAction("Modify Prefab Path")
            else:
                manage_prefab_action = menu.addAction("Associate with Prefab")
            
            delete_go_action = menu.addAction("Delete GameObject")
            action = menu.exec_(QCursor.pos())
            
            if action == add_component_action:
                self.add_component_to_gameobject(index)
            elif action == create_child_go_action:
                self.create_child_gameobject(index)
            elif action == translate_action:
                self.translate_node_text(index)
            elif action == copy_action:
                self.copy_gameobject(index)
            elif action == export_template_action:
                self.export_gameobject_as_template(index)
            elif action == template_manager_action:
                self.open_template_manager(index)
            elif paste_as_child_action and action == paste_as_child_action:
                self.paste_gameobject_as_child(index)
            elif paste_component_action and action == paste_component_action:
                self.paste_component(index)
            elif action == delete_go_action:
                self.delete_gameobject(index)
            elif action == manage_prefab_action:
                self.manage_gameobject_prefab(index, go_has_prefab, prefab_path)
                
        elif item_info['is_folder']:
            create_go_action = menu.addAction("Create GameObject")
            paste_go_action = None
            
            if has_go_clipboard:
                paste_go_action = menu.addAction("Paste GameObject")
            
            template_manager_action = menu.addAction("Template Manager")
            
            delete_folder_action = menu.addAction("Delete Folder")
            translate_action = menu.addAction("Translate Name")
            
            action = menu.exec_(QCursor.pos())
            
            if action == create_go_action:
                self.create_gameobject_in_folder(index)
            elif paste_go_action and action == paste_go_action:
                self.paste_gameobject_in_folder(index)
            elif action == template_manager_action:
                self.open_template_manager(index)
            elif action == delete_folder_action:
                self.delete_folder(index)
            elif action == translate_action:
                self.translate_node_text(index)
        
        elif item_info['is_gameobjects_root']:
            create_go_action = menu.addAction("Create GameObject")
            paste_go_action = None
            
            if has_go_clipboard:
                paste_go_action = menu.addAction("Paste GameObject")
            
            template_manager_action = menu.addAction("Template Manager")
            
            action = menu.exec_(QCursor.pos())
            
            if action == create_go_action:
                self.create_root_gameobject(index)
            elif paste_go_action and action == paste_go_action:
                self.paste_gameobject_at_root(index)
            elif action == template_manager_action:
                self.open_template_manager(index)
        
        elif item_info['array_type'] and item_info['is_array']:
            if item_info['data_obj']:
                add_action = menu.addAction("Add Element...")
                add_action.triggered.connect(lambda: self.add_array_element(index, item_info['array_type'], item_info['data_obj'], item))
                
                # Enable paste only if compatible clipboard content exists
                parent = self.parent()
                
                clipboard = parent.handler.get_array_clipboard()
                if clipboard.has_clipboard_data(self) and \
                   clipboard.is_clipboard_compatible_with_array(self, item_info['array_type']):
    
                    elements_count = clipboard.get_elements_count_from_clipboard(self)
    
                    if elements_count > 1:
                        paste_group_action = menu.addAction(
                            f"Paste Group ({elements_count} elements)")
                        paste_group_action.triggered.connect(
                            lambda: self.paste_array_elements(index, item_info['array_type'],
                                                               item_info['data_obj'], item)
                        )
                    else: 
                        paste_action = menu.addAction("Paste Element")
                        paste_action.triggered.connect(
                            lambda: self.paste_array_element(index, item_info['data_obj'], item)
                        )
                menu.exec_(QCursor.pos())
                
        elif item_info['is_array_element'] and item_info['element_index'] >= 0:
            if item_info['parent_array_item'] and hasattr(item_info['parent_array_item'], 'raw'):
                array_data = item_info['parent_array_item'].raw.get('obj')
                if array_data and item_info['element_index'] < len(array_data.values):
                    # Check if multiple elements are selected
                    selected_indices = self.get_selected_array_elements(item_info['parent_array_item'])
                    
                    if len(selected_indices) > 1:
                        copy_group_action = menu.addAction(f"Copy Group ({len(selected_indices)} elements)")
                        copy_group_action.triggered.connect(
                            lambda: self.copy_array_elements(item_info['parent_array_item'], selected_indices, index)
                        )
                        
                        delete_group_action = menu.addAction(f"Delete Group ({len(selected_indices)} elements)")
                        delete_group_action.triggered.connect(
                            lambda: self.delete_array_elements(item_info['parent_array_item'], selected_indices)
                        )
                    else:
                        # Single element operations
                        copy_action = menu.addAction("Copy Element")
                        copy_action.triggered.connect(
                            lambda: self.copy_array_element(item_info['parent_array_item'], item_info['element_index'])
                        )
                        delete_action = menu.addAction("Delete Element")
                        delete_action.triggered.connect(
                            lambda: self.delete_array_element(item_info['parent_array_item'], item_info['element_index'], index)
                        )
                menu.exec_(QCursor.pos())
        else:
            print("No special actions for this node type")

        # For embedded instances, we need to handle differently
        if item_info['is_embedded']:
            # Currently no special menu options for embedded instances
            return

    def export_gameobject_as_template(self, index):
        """Export a GameObject as a template"""
        if not index.isValid():
            return
            
        item = index.internalPointer()
        if not item or not hasattr(item, 'raw') or not isinstance(item.raw, dict):
            QMessageBox.warning(self, "Error", "Invalid GameObject selection")
            return
            
        reasy_id = item.raw.get("reasy_id")
        if not reasy_id:
            QMessageBox.warning(self, "Error", "Could not determine GameObject ID")
            return
            
        parent_widget = self.parent()
        if not parent_widget or not hasattr(parent_widget, "handler"):
            QMessageBox.warning(self, "Error", "Handler not available")
            return
            
        instance_id = parent_widget.handler.id_manager.get_instance_id(reasy_id)
        if not instance_id:
            QMessageBox.warning(self, "Error", "Could not determine GameObject instance ID")
            return
            
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
            if model:
                for i in range(model.rowCount(QModelIndex())):
                    root_child_idx = model.index(i, 0, QModelIndex())
                    parent_index = self._find_node_by_object_id(model, root_child_idx, parent_id)
                    if parent_index and parent_index.isValid():
                        break
                        
        # Add GameObject to UI
        self.add_gameobject_to_ui_direct(template_data, parent_index)

    def _identify_item_type(self, item):
        """
        Identify the type of a tree item and extract relevant information
        
        Returns a dictionary with item type flags and associated data
        """
        result = {
            'is_embedded': False,
            'is_array': False,
            'array_type': "",
            'data_obj': None,
            'is_array_element': False,
            'parent_array_item': None,
            'element_index': -1,
            'is_gameobject': False,
            'is_component': False,
            'component_instance_id': -1,
            'is_folder': False,
            'is_gameobjects_root': False,
            'is_resource': False,
            'resource_index': -1,
            'is_resources_section': False,
        }
        
        if hasattr(item, 'raw') and isinstance(item.raw, dict):
            result['is_embedded'] = item.raw.get("embedded", False)
        
        if hasattr(item, 'raw') and isinstance(item.raw, dict):
            result['is_resource'] = item.raw.get("type") == "resource"
            if result['is_resource']:
                result['resource_index'] = item.raw.get("resource_index", -1)
        
        if item.data and len(item.data) > 0:
            if item.data[0].startswith("Resources"):
                if (item.parent and hasattr(item.parent, 'data') and 
                    item.parent.data and item.parent.data[0] == "Advanced Information"):
                    result['is_resources_section'] = True

        if item.data and len(item.data) > 0:
            if item.data[0] == "Game Objects":
                result['is_gameobjects_root'] = True
        
        if hasattr(item, 'raw') and isinstance(item.raw, dict):
            result['is_gameobject'] = item.raw.get("type") == "gameobject"
            result['is_folder'] = item.raw.get("type") == "folder"
        
        if not result['is_gameobject'] and item.parent and hasattr(item.parent, 'data') and item.parent.data and item.parent.data[0] == "Components":
            if hasattr(item, 'raw') and isinstance(item.raw, dict):
                reasy_id = item.raw.get("reasy_id")
                if reasy_id:
                    component_instance_id = self.parent().handler.id_manager.get_instance_id(reasy_id)
                    if component_instance_id:
                        result['is_component'] = True
                        result['component_instance_id'] = component_instance_id

        if hasattr(item, 'raw') and isinstance(item.raw, dict):
            result['is_array'] = item.raw.get("type") == "array"
            result['data_obj'] = item.raw.get("obj")
            if result['is_array'] and result['data_obj']:
                if hasattr(result['data_obj'], 'orig_type') and result['data_obj'].orig_type:
                    result['array_type'] = result['data_obj'].orig_type
        
        if not result['is_array'] and item.parent and hasattr(item.parent, 'raw') and isinstance(item.parent.raw, dict):
            if item.parent.raw.get("type") == "array":
                result['parent_array_item'] = item.parent
                parent_data_obj = item.parent.raw.get("obj")
                if parent_data_obj and hasattr(parent_data_obj, 'orig_type'):
                    result['array_type'] = parent_data_obj.orig_type
                    result['is_array_element'] = True
                    for i, child in enumerate(result['parent_array_item'].children):
                        if child == item:
                            result['element_index'] = i
                            break
        
        return result

    def add_array_element(self, index, array_type, data_obj, array_item):
        """Add a new element to an array"""
        def parse_complex_array_type(type_string):
            if not type_string.endswith("[]"):
                return None
                
            if "[[" not in type_string or "]][]" not in type_string:
                return None
                
            base_type = type_string[:type_string.find("[[")]
            
            params_section = type_string[type_string.find("[["):type_string.find("]][]") + 2]
            
            params = []
            current_param = ""
            bracket_level = 0
            inside_param = False
            
            for char in params_section:
                if char == '[':
                    bracket_level += 1
                    if bracket_level == 2 and not inside_param:
                        inside_param = True
                        continue
                elif char == ']':
                    bracket_level -= 1
                    if bracket_level == 1 and inside_param:
                        inside_param = False
                        if ',' in current_param:
                            param_type = current_param.split(',', 1)[0].strip()
                        else:
                            param_type = current_param.strip()
                        params.append(param_type)
                        current_param = ""
                        continue
                elif char == ',' and bracket_level == 1 and not inside_param:
                    continue 
                    
                if inside_param:
                    current_param += char
            
            # Format as base_type<param1,param2,...>
            if params:
                return f"{base_type}<{','.join(params)}>"
            return None

        parsed_type = parse_complex_array_type(array_type)
        
        if parsed_type:
            element_type = parsed_type
        elif array_type.endswith("[]"):
            element_type = array_type[:-2]
        elif array_type.startswith("System.Collections.Generic.List`1<") and array_type.endswith(">"):
            element_type = array_type[array_type.index("<") + 1:array_type.rindex(">")]
        else:
            element_type = array_type
        
        # Get the parent widget/handler that can create elements
        parent = self.parent()
        if not hasattr(parent, "create_array_element"):
            QMessageBox.warning(self, "Error", "Cannot find element creator")
            return
        
        # Check if this is in an embedded context
        embedded_context = self._find_embedded_context(array_item)
        
        try:
            if embedded_context:
                # For embedded RSZ, use the specialized operations
                print(f"Creating array element in embedded context (ID: {getattr(embedded_context, 'instance_id', 'unknown')})")
                rsz_operations = RszEmbeddedArrayOperations(parent)  # Use parent as viewer
                new_element = rsz_operations.create_array_element(
                    element_type, data_obj, embedded_context, direct_update=True, array_item=array_item
                )
            else:
                # Normal array in the main RSZ structure
                print("Creating array element in regular context")
                new_element = parent.create_array_element(
                    element_type, data_obj, direct_update=True, array_item=array_item
                )
                
            if new_element:
                if not self.isExpanded(index):
                    self.expand(index)

                model = self.model()
                array_item = index.internalPointer()
                if hasattr(array_item, 'children') and array_item.children:
                    last_child_idx = model.index(len(array_item.children) - 1, 0, index)
                    if last_child_idx.isValid():
                        item = last_child_idx.internalPointer()
                        if item and not TreeWidgetFactory.should_skip_widget(item):
                            name_text = item.data[0] if item.data else ""
                            node_type = item.raw.get("type", "") if isinstance(item.raw, dict) else ""
                            data_obj = item.raw.get("obj", None) if isinstance(item.raw, dict) else None
                            
                            widget = TreeWidgetFactory.create_widget(
                                node_type, data_obj, name_text, self, self.parent_modified_callback
                            )
                            if widget:
                                self.setIndexWidget(last_child_idx, widget)

                        self.scrollTo(last_child_idx)
                        
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to add element: {str(e)}")

    def delete_array_element(self, parent_array_item, element_index, index):
        """Delete an element from an array with proper backend updates"""
        if not parent_array_item or not hasattr(parent_array_item, 'raw'):
            QMessageBox.warning(self, "Error", "Invalid array item")
            return
            
        # Get the array data object
        array_data = parent_array_item.raw.get('obj')
        if not array_data or not hasattr(array_data, 'values'):
            QMessageBox.warning(self, "Error", "Invalid array data")
            return
            
        # Confirm deletion
        msg_box = QMessageBox()
        msg_box.setIcon(QMessageBox.Warning)
        msg_box.setText(f"Delete element {element_index}?")
        msg_box.setInformativeText("This action cannot be undone.")
        msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg_box.setDefaultButton(QMessageBox.No)
        
        if msg_box.exec_() != QMessageBox.Yes:
            return
        
        # Check if this is in an embedded context by finding embedded_context in the tree hierarchy
        embedded_context = self._find_embedded_context(parent_array_item)
        
        parent = self.parent()
        
        if embedded_context:
            # Use specialized embedded operations
            if parent and hasattr(parent, "scn") and hasattr(parent, "type_registry"):
                print(f"Deleting array element in embedded context (ID: {getattr(embedded_context, 'instance_id', 'unknown')})")
                try:
                    rsz_operations = RszEmbeddedArrayOperations(parent)
                    success = rsz_operations.delete_array_element(array_data, element_index, embedded_context)
                    
                    if success:
                        model = self.model()
                        if model:
                            parent_index = model.getIndexFromItem(parent_array_item)
                            if parent_index.isValid():
                                child_index = model.index(element_index, 0, parent_index)
                                if child_index.isValid():
                                    model.removeRow(child_index.row(), parent_index)
                                
                                self._update_remaining_elements_ui(model, parent_index, array_data, element_index)
                    else:
                        QMessageBox.warning(self, "Error", "Failed to delete element in embedded context")
                except Exception as e:
                    QMessageBox.critical(self, "Error", f"Error deleting element: {str(e)}")
            else:
                QMessageBox.warning(self, "Error", "Missing required references in parent")
            return
           
        if parent and hasattr(parent, "delete_array_element"):
            try:
                print("Deleting array element in regular context")
                success = parent.delete_array_element(array_data, element_index)
                
                if success:
                    # Remove UI element 
                    model = self.model()
                    if model:
                        parent_index = model.getIndexFromItem(parent_array_item)
                        if parent_index.isValid():
                            child_index = model.index(element_index, 0, parent_index)
                            if child_index.isValid():
                                model.removeRow(child_index.row(), parent_index)
                            
                            # Update indices of remaining elements
                            self._update_remaining_elements_ui(model, parent_index, array_data, element_index)
                else:
                    QMessageBox.warning(self, "Error", "Failed to delete element")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Error deleting element: {str(e)}")
                import traceback
                traceback.print_exc()
        else:
            # Fallback to simple deletion if parent doesn't support complex delete
            self._simple_delete_element(array_data, element_index, parent_array_item)

    def _update_remaining_elements_ui(self, model, array_index, array_data, deleted_index):
        """Update UI indices for remaining elements after deletion"""
        for i in range(deleted_index, len(array_data.values)):
            child_idx = model.index(i, 0, array_index)
            if child_idx.isValid():
                child_item = child_idx.internalPointer()
                if child_item and hasattr(child_item, 'data'):
                    old_text = child_item.data[0]
                    after_colon = old_text.split(':', 1)[1].strip() if ':' in old_text else ''
                    new_text = f"{i}: {after_colon}"
                    child_item.data[0] = new_text
                    
                    widget = self.indexWidget(child_idx)
                    if widget:
                        for child_widget in widget.findChildren(QLabel):
                            if ':' in child_widget.text():
                                child_widget.setText(new_text)
                                break

    def _simple_delete_element(self, array_data, element_index, array_item):
        """Simple array element deletion used as fallback"""
        try:
            if element_index < len(array_data.values):
                
                del array_data.values[element_index]
                
                parent = self.parent()
                if parent and hasattr(parent, "mark_modified"):
                    parent.mark_modified()
                elif self.parent_modified_callback:
                    self.parent_modified_callback()
                
                model = self.model()
                if model:
                    array_index = model.getIndexFromItem(array_item)
                    if array_index.isValid():
                        model.removeRow(element_index, array_index)
                        
                        self._update_remaining_elements_ui(model, array_index, array_data, element_index)
                
                QMessageBox.information(self, "Element Deleted", f"Element {element_index} deleted successfully.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to delete element: {str(e)}")

    def _find_embedded_context(self, item):
        """
        Find the embedded RSZ context (RSZUserDataInfo) for a tree item
        
        This traverses up the tree hierarchy to find an embedded context
        """
        current = item
        while current:
            if hasattr(current, 'raw') and isinstance(current.raw, dict):
                if 'embedded_context' in current.raw:
                    context = current.raw['embedded_context']
                    return context
                
                obj = current.raw.get('obj')
                if obj:
                    if hasattr(obj, '_owning_context') and obj._owning_context:
                        return obj._owning_context
                    
                    if hasattr(obj, '_container_context') and obj._container_context:
                        return obj._container_context
                
                # Check for domain_id which indicates embedded data
                if 'domain_id' in current.raw and 'embedded' in current.raw and current.raw['embedded']:
                    # Look for context_chain which contains the full chain of contexts
                    if 'context_chain' in current.raw and current.raw['context_chain']:
                        context_chain = current.raw['context_chain']
                        if context_chain and len(context_chain) > 0:
                            context = context_chain[0]  # Use the first (deepest) context
                            return context
                    
                    if 'embedded_context' in current.raw:
                        context = current.raw['embedded_context']
                        return context
            
            current = current.parent
            
        return None

    def delete_component(self, index, component_instance_id):
        """Delete a component from its GameObject"""
        if component_instance_id <= 0:
            QMessageBox.warning(self, "Error", "Invalid component")
            return
        
        parent = self.parent()

        # Get the component node to access its reasy_id
        item = index.internalPointer()
        if item and hasattr(item, 'raw') and isinstance(item.raw, dict):
            # If we have a reasy_id, get the latest instance_id
            reasy_id = item.raw.get("reasy_id")
            if reasy_id:
                updated_instance_id = parent.handler.id_manager.get_instance_id(reasy_id)
                if updated_instance_id:
                    component_instance_id = updated_instance_id
        
        msg_box = QMessageBox()
        msg_box.setIcon(QMessageBox.Warning)
        msg_box.setText("Delete this component?")
        msg_box.setInformativeText("This action cannot be undone.")
        msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg_box.setDefaultButton(QMessageBox.No)
        
        if msg_box.exec_() != QMessageBox.Yes:
            return
        
        if not hasattr(parent, "delete_component_from_gameobject"):
            QMessageBox.warning(self, "Error", "Component deletion not supported")
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
                    QMessageBox.information(self, "Success", "Component deleted successfully")
                else:
                    QMessageBox.information(self, "Success", "Component deleted successfully, but failed to refresh UI. Please save and reload")
            else:
                QMessageBox.warning(self, "Error", "Failed to delete component")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error deleting component: {str(e)}")

    def create_gameobject_in_folder(self, folder_index):
        """Create a new GameObject in a folder"""
        if not folder_index.isValid():
            return
            
        folder_item = folder_index.internalPointer()
        if not folder_item or not hasattr(folder_item, 'raw') or not isinstance(folder_item.raw, dict):
            QMessageBox.warning(self, "Error", "Invalid folder selection")
            return
            
        # Get folder ID using reasy_id for stable reference
        reasy_id = folder_item.raw.get("reasy_id")
        if not reasy_id:
            QMessageBox.warning(self, "Error", "Could not determine folder ID")
            return
            
        parent = self.parent()

        folder_instance_id = parent.handler.id_manager.get_instance_id(reasy_id)
        if not folder_instance_id:
            QMessageBox.warning(self, "Error", "Could not determine folder instance ID")
            return
        
        if not hasattr(parent, "create_gameobject"):
            QMessageBox.warning(self, "Error", "GameObject creation not supported")
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
                QMessageBox.information(self, "Success", f"GameObject '{name}' created successfully")
            else:
                QMessageBox.warning(self, "Error", "Failed to create GameObject")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error creating GameObject: {str(e)}")
    
    def create_child_gameobject(self, parent_go_index):
        """Create a new GameObject as a child of another GameObject"""
        if not parent_go_index.isValid():
            return
            
        parent_item = parent_go_index.internalPointer()
        if not parent_item or not hasattr(parent_item, 'raw') or not isinstance(parent_item.raw, dict):
            QMessageBox.warning(self, "Error", "Invalid GameObject selection")
            return
            
        # Get parent GameObject ID using reasy_id for stable reference
        reasy_id = parent_item.raw.get("reasy_id")
        if not reasy_id:
            QMessageBox.warning(self, "Error", "Could not determine GameObject ID")
            return
        
        parent_widget = self.parent()
            
        parent_instance_id = parent_widget.handler.id_manager.get_instance_id(reasy_id)
        if not parent_instance_id:
            QMessageBox.warning(self, "Error", "Could not determine GameObject instance ID")
            return
        
        if not hasattr(parent_widget, "create_gameobject"):
            QMessageBox.warning(self, "Error", "GameObject creation not supported")
            return
            
        # Get parent GameObject's object table index
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
                QMessageBox.information(self, "Success", f"Child GameObject '{name}' created successfully")
            else:
                QMessageBox.warning(self, "Error", "Failed to create child GameObject")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error creating child GameObject: {str(e)}")

    def create_root_gameobject(self, index):
        """Create a new GameObject at the root level"""
        # Get the parent widget/handler for GameObject creation
        parent = self.parent()
        if not hasattr(parent, "create_gameobject"):
            QMessageBox.warning(self, "Error", "GameObject creation not supported")
            return
            
        # Create dialog to get GameObject name
        name, ok = QInputDialog.getText(self, "New Root GameObject", "GameObject Name:", QLineEdit.Normal, "New GameObject")
        if not ok or not name:
            return
            
        try:
            go_data = parent.object_operations.create_gameobject(name, -1)
            
            if go_data and go_data.get('success', False):
                self.add_gameobject_to_ui_direct(go_data)
                QMessageBox.information(self, "Success", f"GameObject '{name}' created successfully")
            else:
                QMessageBox.warning(self, "Error", "Failed to create GameObject")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error creating GameObject: {str(e)}")

    def get_value_at_index(self, index):
        """Get value from either embedded widget or model data"""
        if not index.isValid():
            return ""
            
        widget = self.indexWidget(index)
        if widget:
            input_widget = None
            for child in widget.findChildren(QWidget):
                if isinstance(child, (QLineEdit, QDoubleSpinBox, QSpinBox, QCheckBox)):
                    input_widget = child
                    break
                    
            if input_widget:
                if isinstance(input_widget, QLineEdit):
                    return input_widget.text()
                elif isinstance(input_widget, (QSpinBox, QDoubleSpinBox)):
                    return str(input_widget.value())
                elif isinstance(input_widget, QCheckBox):
                    return str(input_widget.isChecked())
                    
        # Fallback to model data
        item = index.internalPointer()
        if item and hasattr(item, 'data') and len(item.data) > 1:
            return str(item.data[1])
            
        return str(index.data(Qt.UserRole) or "")

    def add_component_to_gameobject(self, index):
        """Add a new component to a GameObject with autocomplete"""
        if not index.isValid():
            return
            
        item = index.internalPointer()
        if not item or not hasattr(item, 'raw'):
            QMessageBox.warning(self, "Error", "Invalid GameObject selection")
            return
            
        reasy_id = item.raw.get("reasy_id")
        if not reasy_id:
            QMessageBox.warning(self, "Error", "Could not determine GameObject ID")
            return
            
        parent = self.parent()

        instance_id = parent.handler.id_manager.get_instance_id(reasy_id)
        if not instance_id:
            QMessageBox.warning(self, "Error", "Could not determine GameObject instance ID")
            return
        
        if not hasattr(parent, "create_component_for_gameobject") or not hasattr(parent, "type_registry"):
            QMessageBox.warning(self, "Error", "Component creation not supported")
            return
        
        dialog = ComponentSelectorDialog(self, parent.type_registry)
        component_type = dialog.get_selected_component() if dialog.exec_() else None
        
        if not component_type:
            return
            
        try:
            result = parent.create_component_for_gameobject(instance_id, component_type)
            if isinstance(result, dict) and result.get('success', False):
                self.add_component_to_ui_direct(index, result)
                QMessageBox.information(self, "Success", f"Added {component_type} to GameObject")
            elif result:
                QMessageBox.information(self, "Success", f"Added {component_type} to GameObject, but failed to refresh UI. Please save and reload")
            else:
                QMessageBox.warning(self, "Error", f"Failed to add {component_type}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to add component: {str(e)}")

    def delete_gameobject(self, index):
        """Delete a GameObject and all its children and components"""
        if not index.isValid():
            return
            
        item = index.internalPointer()
        if not item or not hasattr(item, 'raw') or not isinstance(item.raw, dict):
            QMessageBox.warning(self, "Error", "Invalid GameObject selection")
            return                  
        
        reasy_id = item.raw.get("reasy_id")
        if not reasy_id:
            QMessageBox.warning(self, "Error", "Could not determine GameObject ID")
            return
        
        parent = self.parent()
            
        instance_id = parent.handler.id_manager.get_instance_id(reasy_id)
        if not instance_id:
            QMessageBox.warning(self, "Error", "Could not determine GameObject instance ID")
            return
        
        if not hasattr(parent, "delete_gameobject"):
            QMessageBox.warning(self, "Error", "GameObject deletion not supported")
            return
        
        go_object_id = -1
        for i, obj_id in enumerate(parent.scn.object_table):
            if obj_id == instance_id:
                go_object_id = i
                break
        
        if go_object_id < 0:
            QMessageBox.warning(self, "Error", "Could not find GameObject in object table")
            return
        
        has_children = False
        for go in parent.scn.gameobjects:
            if go.parent_id == go_object_id:
                has_children = True
                break
        
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Warning)
        msg.setWindowTitle("Confirm GameObject Deletion")
        
        go_name = ""
        if hasattr(item, 'data') and item.data:
            go_name = item.data[0].split(' (ID:')[0]
        
        msg.setText(f"Delete GameObject \"{go_name}\"?")
        
        details = "This will delete the GameObject"
        
        go = next((g for g in parent.scn.gameobjects if g.id == go_object_id), None)
        if go and go.component_count > 0:
            details += f" and its {go.component_count} component(s)"
            
        if has_children:
            details += " and ALL child GameObjects"
            
        details += ".\nThis action cannot be undone."
        msg.setInformativeText(details)
        msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg.setDefaultButton(QMessageBox.No)
        
        if msg.exec_() != QMessageBox.Yes:
            return
            
        try:
            success = parent.delete_gameobject(go_object_id)
            
            if success:
                model = self.model()
                if model:
                    parent_index = index.parent()
                    
                    if not parent_index.isValid() or not self._is_valid_parent_for_go(parent_index):
                        root_index = model.index(0, 0, QModelIndex())  # Root node
                        row_count = model.rowCount(root_index)
                        for row in range(row_count):
                            child_idx = model.index(row, 0, root_index)
                            child = child_idx.internalPointer()
                            if child and hasattr(child, 'data') and child.data and child.data[0] == "Game Objects":
                                parent_index = child_idx
                                break
                    
                    if parent_index.isValid():
                        row = -1
                        parent_item = parent_index.internalPointer()
                        if parent_item and hasattr(parent_item, 'children'):
                            for i, child in enumerate(parent_item.children):
                                if child is item:
                                    row = i
                                    break
                        
                        if row >= 0:
                            model.removeRow(row, parent_index)
                            QMessageBox.information(self, "Success", "GameObject deleted successfully")
                            return
                
                # Fallback to full refresh if direct model update failed
                QMessageBox.information(self, "Success", "GameObject deleted successfully, but failed to refresh UI. Please save and reload")
            else:
                QMessageBox.warning(self, "Error", "Failed to delete GameObject")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error deleting GameObject: {str(e)}")

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
        if not index.isValid():
            return
            
        folder_item = index.internalPointer()
        if not folder_item or not hasattr(folder_item, 'raw') or not isinstance(folder_item.raw, dict):
            QMessageBox.warning(self, "Error", "Invalid folder selection")
            return
            
        reasy_id = folder_item.raw.get("reasy_id")
        if not reasy_id:
            QMessageBox.warning(self, "Error", "Could not determine folder ID")
            return
        
        parent = self.parent()
            
        folder_instance_id = parent.handler.id_manager.get_instance_id(reasy_id)
        if not folder_instance_id:
            QMessageBox.warning(self, "Error", "Could not determine folder instance ID")
            return
        
        if not hasattr(parent, "delete_folder"):
            QMessageBox.warning(self, "Error", "Folder deletion not supported")
            return
            
        folder_object_id = -1
        for i, instance_id in enumerate(parent.scn.object_table):
            if instance_id == folder_instance_id:
                folder_object_id = i
                break
                
        if folder_object_id < 0:
            QMessageBox.warning(self, "Error", "Could not find folder in object table")
            return
            
        gameobjects_in_folder = []
        for go in parent.scn.gameobjects:
            if go.parent_id == folder_object_id:
                gameobjects_in_folder.append(go.id)
        
        child_folders = []
        for folder in parent.scn.folder_infos:
            if folder.id != folder_object_id and folder.parent_id == folder_object_id:
                child_folders.append(folder.id)
                
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Warning)
        msg.setWindowTitle("Confirm Folder Deletion")
        
        folder_name = ""
        if hasattr(folder_item, 'data') and folder_item.data:
            folder_name = folder_item.data[0].split(' (ID:')[0]
            
        msg.setText(f"Delete folder \"{folder_name}\"?")
        
        details = f"This will delete the folder and all {len(gameobjects_in_folder)} GameObject(s)"
        if child_folders:
            details += f" and {len(child_folders)} sub-folder(s)"
        details += " within it.\nThis action cannot be undone."
        msg.setInformativeText(details)
        msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg.setDefaultButton(QMessageBox.No)
        
        if msg.exec_() != QMessageBox.Yes:
            return
            
        try:
            success = parent.delete_folder(folder_object_id)
            
            if success:
                model = self.model()
                if model:
                    parent_index = index.parent()
                    
                    if not parent_index.isValid():
                        root_index = model.index(0, 0, QModelIndex())
                        row_count = model.rowCount(root_index)
                        for row in range(row_count):
                            child_idx = model.index(row, 0, root_index)
                            child = child_idx.internalPointer()
                            if child and hasattr(child, 'data') and child.data and child.data[0] == "Folders":
                                parent_index = child_idx
                                break
                    
                    if parent_index.isValid():
                        row = -1
                        parent_item = parent_index.internalPointer()
                        if parent_item and hasattr(parent_item, 'children'):
                            for i, child in enumerate(parent_item.children):
                                if child is folder_item:
                                    row = i
                                    break
                        
                        if row >= 0:
                            model.removeRow(row, parent_index)
                            QMessageBox.information(self, "Success", f"Folder '{folder_name}' deleted successfully")
                            return
                
                QMessageBox.information(self, "Success", f"Folder '{folder_name}' deleted successfully, but failed to refresh UI directly.")
            else:
                QMessageBox.warning(self, "Error", "Failed to delete folder")
                
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error deleting folder: {str(e)}")

    def manage_gameobject_prefab(self, index, has_prefab, current_path=""):
        """Create or modify prefab association for a GameObject"""
        if not index.isValid():
            return
            
        item = index.internalPointer()
        if not item or not hasattr(item, 'raw') or not isinstance(item.raw, dict):
            QMessageBox.warning(self, "Error", "Invalid GameObject selection")
            return
            
        reasy_id = item.raw.get("reasy_id")
        if not reasy_id:
            QMessageBox.warning(self, "Error", "Could not determine GameObject ID")
            return
        
        parent = self.parent()
            
        instance_id = parent.handler.id_manager.get_instance_id(reasy_id)
        if not instance_id:
            QMessageBox.warning(self, "Error", "Could not determine GameObject instance ID")
            return
        
        if not parent or not hasattr(parent, "scn") or not hasattr(parent, "object_operations"):
            QMessageBox.warning(self, "Error", "Prefab management not supported")
            return
            
        go_object_id = -1
        for i, obj_id in enumerate(parent.scn.object_table):
            if obj_id == instance_id:
                go_object_id = i
                break
                
        if go_object_id < 0:
            QMessageBox.warning(self, "Error", "Could not find GameObject in object table")
            return
            
        dialog_title = "Modify Prefab Path" if has_prefab else "Associate with Prefab"
        prompt_text = "Enter new prefab path:" if has_prefab else "Enter prefab path:"
        
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
                
            if not path or path.strip() == "":
                QMessageBox.warning(self, "Invalid Input", "Prefab path cannot be empty. Please enter a valid path.")
            else:
                break
        
        try:
            if not path.endswith(".pfb"):
                if QMessageBox.question(
                    self, "Add Extension?", 
                    "Prefab paths typically end with .pfb. Do you want to add the .pfb extension?",
                    QMessageBox.Yes | QMessageBox.No
                ) == QMessageBox.Yes:
                    if not path.endswith(".pfb"):
                        path += ".pfb"
            
            success = parent.object_operations.manage_gameobject_prefab(go_object_id, path)
            
            if success:
                action_type = "modified" if has_prefab else "created"
                QMessageBox.information(self, "Success", f"Prefab {action_type} successfully")
            else:
                QMessageBox.warning(self, "Error", "Failed to manage prefab")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error managing prefab: {str(e)}")

    def add_resource(self, index):
        """Add a new resource path directly in the tree view"""
        parent = self.parent()
        if not parent or not hasattr(parent, "scn"):
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
                QMessageBox.information(self, "Success", f"Added resource '{path}'")
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
        
        if not model or not resources_node:
            return False
        
        res_node = {
            "data": [path, ""],
            "type": "resource",
            "resource_index": resource_index
        }
        
        resources_index = model.getIndexFromItem(resources_node)
        if not resources_index.isValid():
            return False
        
        parent = self.parent()
        if parent and hasattr(parent, "scn"):
            resources_node.data[0] = f"Resources {len(parent.scn.resource_infos)} items"
        
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
        if not parent or not hasattr(parent, "scn") or resource_index >= len(parent.scn.resource_infos):
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
            if item and hasattr(item, 'data'):
                item.data[0] = path
                
                model = self.model()
                if model:
                    model.dataChanged.emit(index, index)
                    
                    widget = self.indexWidget(index)
                    if widget:
                        for label in widget.findChildren(QLabel):
                            label.setText(path)
                    else:
                        new_widget = TreeWidgetFactory.create_widget("resource", None, path, self, self.parent_modified_callback)
                        if new_widget:
                            self.setIndexWidget(index, new_widget)
                    
                QMessageBox.information(self, "Success", f"Resource updated to '{path}'")
        except Exception as e:
            self._handle_resource_error("edit", e)

    def delete_resource(self, index, resource_index):
        """Delete a resource path directly from the tree view"""
        if resource_index < 0:
            QMessageBox.warning(self, "Error", "Invalid resource index")
            return
        
        parent = self.parent()
        if not parent or not hasattr(parent, "scn") or resource_index >= len(parent.scn.resource_infos):
            QMessageBox.warning(self, "Error", "Resource deletion not supported or invalid index")
            return
        
        resource_path = self._get_current_resource_path(resource_index)
        
        if not self._confirm_resource_deletion(resource_path):
            return
        
        try:
            success = parent.delete_resource(resource_index)
            if not success:
                QMessageBox.warning(self, "Error", "Failed to delete resource")
                return
            
            model = self.model()
            resources_node = self._find_resources_node()
            
            if resources_node and model:
                resources_index = model.getIndexFromItem(resources_node)
                if resources_index.isValid():
                    resources_node.data[0] = f"Resources {len(parent.scn.resource_infos)} items"
                    
                    row_to_remove = self._find_resource_row(resources_node.children, resource_index)
                    if row_to_remove >= 0:
                        model.removeRow(row_to_remove, resources_index)
                        self._update_remaining_resource_indices(resources_node, resource_index)
                        QMessageBox.information(self, "Success", f"Deleted resource '{resource_path}'")
                        return
                        
            self._update_resources_ui(f"Deleted resource '{resource_path}'")
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
        msg_box = QMessageBox()
        msg_box.setIcon(QMessageBox.Warning)
        msg_box.setText(f"Delete resource '{resource_path}'?")
        msg_box.setInformativeText("This action cannot be undone.")
        msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg_box.setDefaultButton(QMessageBox.No)
        return msg_box.exec_() == QMessageBox.Yes

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
        """Helper to find the resources section node"""
        model = self.model()
        if not model:
            return None
            
        root_index = model.index(0, 0, QModelIndex())
        if not root_index.isValid():
            return None
            
        def find_resources_recursive(parent_index):
            if not parent_index.isValid():
                return None
                
            parent_item = parent_index.internalPointer()
            if (parent_item and hasattr(parent_item, 'data') and 
                parent_item.data and parent_item.data[0].startswith("Resources")):
                return parent_item
            
            rows = model.rowCount(parent_index)
            for row in range(rows):
                child_index = model.index(row, 0, parent_index)
                child_item = child_index.internalPointer()
                
                if (child_item and hasattr(child_item, 'data') and 
                    child_item.data and child_item.data[0].startswith("Resources")):
                    return child_item
                
                result = find_resources_recursive(child_index)
                if result:
                    return result
            
            return None
        
        resources_node = find_resources_recursive(root_index)
        if resources_node:
            return resources_node
            
        advanced_index = None
        
        for i in range(model.rowCount(root_index)):
            node_index = model.index(i, 0, root_index)
            node_item = node_index.internalPointer()
            if node_item and hasattr(node_item, 'data') and node_item.data and node_item.data[0] == "Advanced":
                advanced_index = node_index
                break
        
        if advanced_index:
            for i in range(model.rowCount(advanced_index)):
                node_index = model.index(i, 0, advanced_index)
                node_item = node_index.internalPointer()
                if node_item and hasattr(node_item, 'data') and node_item.data and node_item.data[0].startswith("Resources"):
                    return node_item
        
        return None

    def add_gameobject_to_ui_direct(self, go_data, parent_index=None):
        """
        Add a GameObject directly to the UI tree without refreshing
        
        Args:
            go_data: Dictionary with GameObject data from the operation
            parent_index: Optional parent index for child GameObjects
            
        Returns:
            QModelIndex: The index of the added GameObject node, or None if failed
        """
        # Check if we're dealing with a list of children (this happens during recursive calls)
        if isinstance(go_data, list):
            # If it's a list of children GameObjects, process each one
            for child_data in go_data:
                self.add_gameobject_to_ui_direct(child_data, parent_index)
            return None
        
        parent_widget = self.parent()
        if not parent_widget or not hasattr(parent_widget, "name_helper"):
            return None
        
        model = self.model()
        if not model:
            return None
            
        parent_node = None
        parent_item = None
        
        if parent_index and parent_index.isValid():
            parent_item = parent_index.internalPointer()
            
            if parent_item.data and parent_item.data[0] == "Children":
                parent_node = parent_item
            elif hasattr(parent_item, 'raw') and isinstance(parent_item.raw, dict) and parent_item.raw.get('type') == 'gameobject':
                for child in parent_item.children:
                    if child.data and child.data[0] == "Children":
                        parent_node = child
                        break
                        
                if not parent_node:
                    children_node = {"data": ["Children", ""], "children": []}
                    model.addChild(parent_item, children_node)
                    
                    for child in parent_item.children:
                        if child.data and child.data[0] == "Children":
                            parent_node = child
                            break
            elif hasattr(parent_item, 'raw') and isinstance(parent_item.raw, dict) and parent_item.raw.get('type') == 'folder':
                parent_node = parent_item
        
        if not parent_node:
            parent_node = self._find_gameobjects_node()
            
            if not parent_node:
                print("Failed to find GameObjects node for root GameObject placement")
                return None
        
        instance_id = go_data.get('instance_id', 0)
        name = go_data.get('name', 'GameObject')
        reasy_id = go_data.get('reasy_id', 0)
        go_id = go_data.get('go_id', -1)
        
        display_name = f"{name} (ID: {instance_id})"
        
        go_dict = {
            "data": [display_name, ""],
            "type": "gameobject",
            "instance_id": instance_id,
            "reasy_id": reasy_id,
            "children": [],
        }
        
        settings_node = {"data": ["Settings", ""], "children": []}
        go_dict["children"].append(settings_node)
        
        if instance_id in parent_widget.scn.parsed_elements:
            fields = parent_widget.scn.parsed_elements[instance_id]
            first_field = True
            
            for field_name, field_data in fields.items():
                field_node = parent_widget._create_field_dict(field_name, field_data)
                
                if first_field or (len(settings_node["children"]) == 1 and settings_node["children"][0]["data"][0].startswith("GUID")):
                    field_data.is_gameobject_or_folder_name = go_dict
                    first_field = False
                    
                settings_node["children"].append(field_node)
        
        component_count = go_data.get('component_count', 0)
        if component_count > 0:
            comp_node = {"data": ["Components", ""], "children": []}
            go_dict["children"].append(comp_node)
            
            if go_id >= 0:
                for i in range(1, component_count + 1):
                    component_go_id = go_id + i
                    if component_go_id >= len(parent_widget.scn.object_table):
                        break
                    comp_instance_id = parent_widget.scn.object_table[component_go_id]
                    if comp_instance_id <= 0:
                        continue
                        
                    reasy_id = parent_widget.handler.id_manager.get_reasy_id_for_instance(comp_instance_id)
                    component_name = parent_widget.name_helper.get_instance_name(comp_instance_id)
                    comp_dict = {
                        "data": [f"{component_name} (ID: {comp_instance_id})", ""],
                        "instance_id": comp_instance_id,
                        "reasy_id": reasy_id,
                        "children": [],
                    }
                    
                    if comp_instance_id in parent_widget.scn.parsed_elements:
                        fields = parent_widget.scn.parsed_elements[comp_instance_id]
                        for f_name, f_data in fields.items():
                            comp_dict["children"].append(parent_widget._create_field_dict(f_name, f_data))
                            
                    comp_node["children"].append(comp_dict)
        
        has_children = 'children' in go_data and go_data['children']
        if has_children:
            children_node = {"data": ["Children", ""], "children": []}
            go_dict["children"].append(children_node)
        
        model.addChild(parent_node, go_dict)
        
        parent_node_index = model.getIndexFromItem(parent_node)
        go_index = None
        if parent_node_index.isValid():
            go_index = model.index(len(parent_node.children) - 1, 0, parent_node_index)
            if go_index.isValid():
                self.expand(parent_node_index)
                self.scrollTo(go_index)
                
                go_item = go_index.internalPointer()
                if go_item and hasattr(go_item, 'data'):
                    name_text = go_item.data[0] if go_item.data else ""
                    node_type = go_item.raw.get("type", "") if isinstance(go_item.raw, dict) else ""
                    
                    widget = TreeWidgetFactory.create_widget(
                        node_type, None, name_text, self, self.parent_modified_callback
                    )
                    if widget:
                        self.setIndexWidget(go_index, widget)
        
        if has_children and go_index and go_index.isValid():
            go_item = go_index.internalPointer()
            children_node_index = None
            
            for i, child in enumerate(go_item.children):
                if child.data and child.data[0] == "Children":
                    children_node_index = model.index(i, 0, go_index)
                    break
            
            if children_node_index and children_node_index.isValid():
                self.expand(go_index)
                
                for child_data in go_data['children']:
                    self.add_gameobject_to_ui_direct(child_data, children_node_index)
        
        return go_index

    def _find_gameobjects_node(self):
        """Find the GameObjects node in the tree for root GameObject placement"""

        model = self.model()
        data_index = None

        for i in range(model.rowCount()):
            child_index = model.index(i, 0, QModelIndex())
            if not child_index.isValid():
                continue         
            child_text = model.data(child_index)
            if child_text == "Data Block":
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
            if child_text == "Game Objects":
                return child_index.internalPointer()
        
        print("Could not find Game Objects node under Data node")
        return None

    def _find_node_by_object_id(self, model, start_index, object_id):
        """
        Helper method to find a tree node by GameObject/Folder object_id
        """
        if not start_index.isValid():
            return None
            
        item = start_index.internalPointer()
        if not item:
            return None
            
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

    def add_component_to_ui_direct(self, go_index, component_data):
        """
        Add a component directly to the UI tree without refreshing
        
        Args:
            go_index: QModelIndex of the GameObject
            component_data: Dictionary with component data from creation operation
        """
        if not go_index.isValid():
            return False
            
        go_item = go_index.internalPointer()
        if not go_item or not hasattr(go_item, 'raw'):
            return False
            
        parent = self.parent()
        if not parent or not hasattr(parent, "scn"):
            return False
            
        model = self.model()
        if not model:
            return False
            
        components_node = None
        for child in go_item.children:
            if child.data and child.data[0] == "Components":
                components_node = child
                break
                
        if not components_node:
            components_dict = {"data": ["Components", ""], "children": []}
            model.addChild(go_item, components_dict)
            
            for child in go_item.children:
                if child.data and child.data[0] == "Components":
                    components_node = child
                    break
                    
        if not components_node:
            return False
        
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
        
        if component_instance_id in parent.scn.parsed_elements:
            fields = parent.scn.parsed_elements[component_instance_id]
            for field_name, field_data in fields.items():
                component_dict["children"].append(
                    parent._create_field_dict(field_name, field_data)
                )
        
        model.addChild(components_node, component_dict)
        
        components_index = model.getIndexFromItem(components_node)
        if components_index.isValid():
            self.expand(components_index)
            
            component_index = model.index(len(components_node.children) - 1, 0, components_index)
            if component_index.isValid():
                self.scrollTo(component_index)
                
                item = component_index.internalPointer()
                if item and hasattr(item, 'data'):
                    name_text = item.data[0] if item.data else ""
                    
                    widget = TreeWidgetFactory.create_widget(
                        "component", None, name_text, self, self.parent_modified_callback
                    )
                    if widget:
                        self.setIndexWidget(component_index, widget)
                        
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

    def copy_array_elements(self, parent_array_item, element_indices, index):
        """Copy multiple array elements to clipboard"""
        if not parent_array_item or not hasattr(parent_array_item, 'raw'):
            QMessageBox.warning(self, "Error", "Invalid array item")
            return
            
        array_data = parent_array_item.raw.get('obj')
        if not array_data or not hasattr(array_data, 'values'):
            QMessageBox.warning(self, "Error", "Invalid array data")
            return

        # Check if this array is inside an embedded context
        embedded_context = self._find_embedded_context(parent_array_item)
        if embedded_context:
            QMessageBox.warning(self, "Error", "Copying elements in embedded userdata is currently not supported.")
            return
            
        elements = []
        for idx in element_indices:
            if idx < len(array_data.values):
                elements.append(array_data.values[idx])
        
        if not elements:
            QMessageBox.warning(self, "Error", "No valid elements selected")
            return
            
        array_type = array_data.orig_type if hasattr(array_data, 'orig_type') else ""
        
        parent = self.parent()
        if parent and hasattr(parent, "handler"):
            try:
                clipboard = parent.handler.get_array_clipboard()
                success = clipboard.copy_multiple_to_clipboard(self, elements, array_type)
                if success:
                    QMessageBox.information(self, "Success", f"{len(elements)} elements copied to clipboard")
                else:
                    QMessageBox.warning(self, "Error", "Failed to copy elements")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Error copying elements: {str(e)}")
                traceback.print_exc()
        else:
            QMessageBox.warning(self, "Error", "Cannot access handler")

    def paste_array_elements(self, index, array_type, data_obj, array_item):
        """Paste multiple array elements from clipboard"""
        parent = self.parent()
        if not parent:
            QMessageBox.warning(self, "Error", "Cannot access parent viewer")
            return
        
        embedded_context = self._find_embedded_context(array_item)
            
        try:
            if parent and hasattr(parent, "handler"):
                clipboard = parent.handler.get_array_clipboard()
                elements = clipboard.paste_elements_from_clipboard(
                    self, parent.array_operations, data_obj, array_item, embedded_context)
                
                if elements:
                    if not self.isExpanded(index):
                        self.expand(index)
                    
                    model = self.model()
                    array_item = index.internalPointer()
                    if hasattr(array_item, 'children') and array_item.children:
                        last_child_idx = model.index(len(array_item.children) - 1, 0, index)
                        if last_child_idx.isValid():
                            item = last_child_idx.internalPointer()
                            if item and not TreeWidgetFactory.should_skip_widget(item):
                                name_text = item.data[0] if item.data else ""
                                node_type = item.raw.get("type", "") if isinstance(item.raw, dict) else ""
                                data_obj = item.raw.get("obj", None) if isinstance(item.raw, dict) else None
                                
                                widget = TreeWidgetFactory.create_widget(
                                    node_type, data_obj, name_text, self, self.parent_modified_callback
                                )
                                if widget:
                                    self.setIndexWidget(last_child_idx, widget)
                            
                            self.scrollTo(last_child_idx)

                    QMessageBox.information(self, "Success", f"{len(elements)} elements pasted successfully")
                else:
                    QMessageBox.warning(self, "Error", "Failed to paste elements. Make sure the clipboard contains compatible elements.")
            else:
                QMessageBox.warning(self, "Error", "Cannot access handler")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to paste elements: {str(e)}")
            traceback.print_exc()

    def translate_node_text(self, index):
        """Translate the name of a GameObject or folder using Google Translate API"""
        if not index.isValid():
            return
            
        item = index.internalPointer()
        if not hasattr(item, 'data') or not item.data:
            return
    
        name_text = item.data[0]
        if " (ID:" in name_text:
            name_text = name_text.split(" (ID:")[0]
        
        if not name_text or name_text.strip() == "":
            show_translation_error(self, "No text to translate")
            return
        
        parent_widget = self.parent()
        target_lang = parent_widget.handler.app.settings.get("translation_target_language", "en")
            
        original_id_part = item.data[0].replace(name_text, "") if " (ID:" in item.data[0] else ""
        
        context = {"index": index, "original_id_part": original_id_part}
        
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

    def _on_translation_completed(self, original_text, translated_text, context):
        """Handle translation completion"""
        self.setCursor(Qt.ArrowCursor)
        
        if not translated_text:
            show_translation_error(self, "Unable to translate text")
            return
            
        index = context.get("index")
        original_id_part = context.get("original_id_part", "")
        
        if not index or not index.isValid():
            show_translation_error(self, "Invalid item index")
            return
        
        item = index.internalPointer()
        if not item or not hasattr(item, 'data'):
            show_translation_error(self, "Invalid item data")
            return
        
        item.data[0] = translated_text + original_id_part
        
        model = self.model()
        if model:
            model.dataChanged.emit(index, index)
            
            widget = self.indexWidget(index)
            if widget:
                labels = widget.findChildren(QLabel)
                if len(labels) >= 2:
                    text_label = labels[1]
                    text_label.setText(translated_text + original_id_part)
        
        show_translation_result(self, translated_text)

    def delete_array_elements(self, parent_array_item, element_indices):
        """Delete multiple array elements with proper backend updates"""
        if not parent_array_item or not hasattr(parent_array_item, 'raw'):
            QMessageBox.warning(self, "Error", "Invalid array item")
            return
            
        array_data = parent_array_item.raw.get('obj')
        if not array_data or not hasattr(array_data, 'values'):
            QMessageBox.warning(self, "Error", "Invalid array data")
            return
            
        msg_box = QMessageBox()
        msg_box.setIcon(QMessageBox.Warning)
        msg_box.setText(f"Delete {len(element_indices)} elements?")
        msg_box.setInformativeText("This action cannot be undone.")
        msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg_box.setDefaultButton(QMessageBox.No)
        
        if msg_box.exec_() != QMessageBox.Yes:
            return
        
        element_indices = sorted(element_indices, reverse=True)
        
        embedded_context = self._find_embedded_context(parent_array_item)
        parent = self.parent()
        
        if embedded_context:
            if parent and hasattr(parent, "scn") and hasattr(parent, "type_registry"):
                print("Deleting array elements in embedded context")
                try:
                    rsz_operations = RszEmbeddedArrayOperations(parent)
                    success = True
                    model = self.model()
                    parent_index = None
                    
                    if model:
                        parent_index = model.getIndexFromItem(parent_array_item)
                    
                    for idx in element_indices:
                        if not rsz_operations.delete_array_element(array_data, idx, embedded_context):
                            success = False
                            break
                        
                        if parent_index and parent_index.isValid():
                            child_index = model.index(idx, 0, parent_index)
                            if child_index.isValid():
                                model.removeRow(child_index.row(), parent_index)
                    
                    if success:
                        if model and parent_index and parent_index.isValid():
                            self._update_array_element_indices(model, parent_index, array_data)
                        QMessageBox.information(self, "Success", f"Deleted {len(element_indices)} elements successfully")
                    else:
                        QMessageBox.warning(self, "Error", "Failed to delete all elements in embedded context")
                except Exception as e:
                    QMessageBox.critical(self, "Error", f"Error deleting elements: {str(e)}")
                    traceback.print_exc()
            return
           
        if parent and hasattr(parent, "array_operations"):
            try:
                print("Deleting array elements in regular context")
                success = True
                model = self.model()
                parent_index = None
                
                if model:
                    parent_index = model.getIndexFromItem(parent_array_item)
                
                for idx in element_indices:
                    if not parent.delete_array_element(array_data, idx):
                        success = False
                        break
                    
                    if parent_index and parent_index.isValid():
                        child_index = model.index(idx, 0, parent_index)
                        if child_index.isValid():
                            model.removeRow(child_index.row(), parent_index)
                
                if success:
                    if model and parent_index and parent_index.isValid():
                        self._update_array_element_indices(model, parent_index, array_data)
                    QMessageBox.information(self, "Success", f"Deleted {len(element_indices)} elements successfully")
                else:
                    QMessageBox.warning(self, "Error", "Failed to delete all elements")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Error deleting elements: {str(e)}")
                traceback.print_exc()
        else:
            for idx in element_indices:
                self._simple_delete_element(array_data, idx, parent_array_item)

    def _update_array_element_indices(self, model, array_index, array_data):
        """Update UI indices for all elements in an array"""
        for i in range(len(array_data.values)):
            child_idx = model.index(i, 0, array_index)
            if child_idx.isValid():
                child_item = child_idx.internalPointer()
                if child_item and hasattr(child_item, 'data'):
                    old_text = child_item.data[0]
                    after_colon = old_text.split(':', 1)[1].strip() if ':' in old_text else ''
                    new_text = f"{i}: {after_colon}"
                    child_item.data[0] = new_text
                    
                    widget = self.indexWidget(child_idx)
                    if widget:
                        for child_widget in widget.findChildren(QLabel):
                            if ':' in child_widget.text():
                                child_widget.setText(new_text)
                                break

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
            
        selected_indexes = selection_model.selectedIndexes()
        
        for index in selected_indexes:
            if not index.isValid() or index.parent() != parent_index:
                continue
                
            row = index.row()
            if row >= 0:
                selected_indices.append(row)
                
        return sorted(selected_indices)
        
    def copy_component(self, index, component_instance_id):
        """Copy a component to clipboard for pasting to another GameObject"""
        if component_instance_id <= 0:
            QMessageBox.warning(self, "Error", "Invalid component")
            return
        
        parent = self.parent()
        if not parent or not hasattr(parent, "handler"):
            QMessageBox.warning(self, "Error", "Handler not available")
            return
            
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
        if not item or not hasattr(item, 'raw') or not isinstance(item.raw, dict):
            QMessageBox.warning(self, "Error", "Invalid GameObject selection")
            return
            
        reasy_id = item.raw.get("reasy_id")
        if not reasy_id:
            QMessageBox.warning(self, "Error", "Could not determine GameObject ID")
            return
            
        parent = self.parent()
        if not parent or not hasattr(parent, "handler"):
            QMessageBox.warning(self, "Error", "Handler not available")
            return
            
        instance_id = parent.handler.id_manager.get_instance_id(reasy_id)
        if not instance_id:
            QMessageBox.warning(self, "Error", "Could not determine GameObject instance ID")
            return
            
        clipboard_data = parent.handler.get_component_clipboard_data(self)
        if not clipboard_data:
            QMessageBox.warning(self, "Error", "No component data in clipboard")
            return
            
        type_name = clipboard_data.get("type_name", "Component")
        
        try:
            result = parent.handler.paste_component_from_clipboard(parent, instance_id, clipboard_data)
            
            if result and result.get("success", False):
                self.add_component_to_ui_direct(index, result)
                QMessageBox.information(self, "Success", f"Pasted {type_name} to GameObject")
            else:
                QMessageBox.warning(self, "Error", f"Failed to paste {type_name}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error pasting component: {str(e)}")

    def copy_gameobject(self, index):
        """Copy a GameObject to clipboard"""
        if not index.isValid():
            return
            
        item = index.internalPointer()
        if not item or not hasattr(item, 'raw') or not isinstance(item.raw, dict):
            QMessageBox.warning(self, "Error", "Invalid GameObject selection")
            return
            
        reasy_id = item.raw.get("reasy_id")
        if not reasy_id:
            QMessageBox.warning(self, "Error", "Could not determine GameObject ID")
            return
            
        parent = self.parent()
        if not parent or not hasattr(parent, "handler"):
            QMessageBox.warning(self, "Error", "Handler not available")
            return
            
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
            
        try:
            success = parent.handler.copy_gameobject_to_clipboard(parent, go_object_id)
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
        if not folder_index.isValid():
            return
            
        folder_item = folder_index.internalPointer()
        if not folder_item or not hasattr(folder_item, 'raw') or not isinstance(folder_item.raw, dict):
            QMessageBox.warning(self, "Error", "Invalid folder selection")
            return
            
        reasy_id = folder_item.raw.get("reasy_id")
        if not reasy_id:
            QMessageBox.warning(self, "Error", "Could not determine folder ID")
            return
            
        parent = self.parent()
        if not parent or not hasattr(parent, "handler"):
            QMessageBox.warning(self, "Error", "Handler not available")
            return
            
        folder_instance_id = parent.handler.id_manager.get_instance_id(reasy_id)
        if not folder_instance_id:
            QMessageBox.warning(self, "Error", "Could not determine folder instance ID")
            return
            
        folder_object_id = -1
        for i, instance_id in enumerate(parent.scn.object_table):
            if instance_id == folder_instance_id:
                folder_object_id = i
                break
                
        if folder_object_id < 0:
            QMessageBox.warning(self, "Error", "Could not find folder in object table")
            return
            
        self._paste_gameobject_common(folder_object_id, folder_index)

    def paste_gameobject_at_root(self, index):
        """Paste a GameObject from clipboard at the root level"""
        parent = self.parent()
        if not parent or not hasattr(parent, "handler"):
            QMessageBox.warning(self, "Error", "Handler not available")
            return
            
        self._paste_gameobject_common(-1, None)

    def paste_gameobject_as_child(self, parent_go_index):
        """Paste a GameObject from clipboard as a child of another GameObject"""
        if not parent_go_index.isValid():
            QMessageBox.warning(self, "Error", "Invalid parent GameObject selection")
            return
            
        parent_item = parent_go_index.internalPointer()
        if not parent_item or not hasattr(parent_item, 'raw') or not isinstance(parent_item.raw, dict):
            QMessageBox.warning(self, "Error", "Invalid parent GameObject")
            return
            
        reasy_id = parent_item.raw.get("reasy_id")
        if not reasy_id:
            QMessageBox.warning(self, "Error", "Could not determine parent GameObject ID")
            return
            
        parent_widget = self.parent()
        if not parent_widget or not hasattr(parent_widget, "handler"):
            QMessageBox.warning(self, "Error", "Handler not available")
            return
            
        parent_instance_id = parent_widget.handler.id_manager.get_instance_id(reasy_id)
        if not parent_instance_id:
            QMessageBox.warning(self, "Error", "Could not determine parent GameObject instance ID")
            return
            
        parent_object_id = -1
        for i, instance_id in enumerate(parent_widget.scn.object_table):
            if instance_id == parent_instance_id:
                parent_object_id = i
                break
                
        if parent_object_id < 0:
            QMessageBox.warning(self, "Error", "Could not find parent GameObject in object table")
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
        
        if not parent_widget.handler.has_gameobject_clipboard_data(self):
            QMessageBox.warning(self, "Error", "No GameObject data in clipboard")
            return
            
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
                QMessageBox.information(self, "Success", f"GameObject '{new_name}' pasted successfully")
            else:
                QMessageBox.warning(self, "Error", "Failed to paste GameObject")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error pasting GameObject: {str(e)}")
            traceback.print_exc()

    def copy_array_element(self, parent_array_item, element_index):
        """Copy an array element to clipboard"""
        if not parent_array_item or not hasattr(parent_array_item, 'raw'):
            QMessageBox.warning(self, "Error", "Invalid array item")
            return
            
        array_data = parent_array_item.raw.get('obj')
        if not array_data or not hasattr(array_data, 'values') or element_index >= len(array_data.values):
            QMessageBox.warning(self, "Error", "Invalid array element")
            return

        embedded_context = self._find_embedded_context(parent_array_item)
        if embedded_context:
            QMessageBox.warning(self, "Error", "Copying elements in embedded userdata is currently not supported.")
            return
            
        element = array_data.values[element_index]
        array_type = array_data.orig_type if hasattr(array_data, 'orig_type') else ""
        
        parent = self.parent()
        if parent and hasattr(parent, "handler"):
            try:
                clipboard = parent.handler.get_array_clipboard()
                success = clipboard.copy_to_clipboard(self, element, array_type)
                if success:
                    QMessageBox.information(self, "Success", "Element copied to clipboard")
                else:
                    QMessageBox.warning(self, "Error", "Failed to copy element")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Error copying element: {str(e)}")
                traceback.print_exc()
        else:
            QMessageBox.warning(self, "Error", "Cannot access handler")

    def paste_array_element(self, index, data_obj, array_item):
        """Paste an element from clipboard to an array"""
        parent = self.parent()
        if not parent:
            QMessageBox.warning(self, "Error", "Cannot access parent viewer")
            return
        
        embedded_context = self._find_embedded_context(array_item)
            
        try:
            if parent and hasattr(parent, "handler"):
                clipboard = parent.handler.get_array_clipboard()
                new_element = clipboard.paste_elements_from_clipboard(
                    self, parent.array_operations, data_obj, array_item, embedded_context)
                
                if new_element:
                    if not self.isExpanded(index):
                        self.expand(index)
                    
                    model = self.model()
                    array_item = index.internalPointer()
                    if hasattr(array_item, 'children') and array_item.children:
                        last_child_idx = model.index(len(array_item.children) - 1, 0, index)
                        if last_child_idx.isValid():
                            item = last_child_idx.internalPointer()
                            if item and not TreeWidgetFactory.should_skip_widget(item):
                                name_text = item.data[0] if item.data else ""
                                node_type = item.raw.get("type", "") if isinstance(item.raw, dict) else ""
                                data_obj = item.raw.get("obj", None) if isinstance(item.raw, dict) else None
                                
                                widget = TreeWidgetFactory.create_widget(
                                    node_type, data_obj, name_text, self, self.parent_modified_callback
                                )
                                if widget:
                                    self.setIndexWidget(last_child_idx, widget)
                            
                            self.scrollTo(last_child_idx)
                            
                    QMessageBox.information(self, "Success", "Element pasted successfully.")
                else:
                    QMessageBox.warning(self, "Error", "Failed to paste element. Make sure the clipboard contains a compatible element.")
            else:
                QMessageBox.warning(self, "Error", "Cannot access handler")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to paste element: {str(e)}")
            traceback.print_exc()
