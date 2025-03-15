from PySide6.QtWidgets import (QWidget, QHBoxLayout, QLabel, QTreeView, 
                               QHeaderView, QSpinBox, QMenu, QDoubleSpinBox, QMessageBox, QStyledItemDelegate,
                               QLineEdit, QInputDialog)
from PySide6.QtGui import QIcon, QCursor
from PySide6.QtCore import Qt, QModelIndex

from .tree_core import TreeModel
from .component_selector import ComponentSelectorDialog 

from .value_widgets import *

from utils.enum_manager import EnumManager
from utils.id_manager import IdManager, EmbeddedIdManager
from file_handlers.rsz.rsz_embedded_array_operations import RszEmbeddedArrayOperations
                
import traceback
                    
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

    def keyPressEvent(self, event):
        """Handle keyboard shortcuts like Delete key for GameObject deletion"""
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
                
            # Check if this is a GameObject
            if hasattr(item, 'raw') and isinstance(item.raw, dict):
                is_gameobject = item.raw.get("type") == "gameobject"
                
                if is_gameobject:
                    # Use reasy_id to get the current instance_id
                    reasy_id = item.raw.get("reasy_id")
                    if reasy_id:
                        if item.raw.get("embedded", False):
                            return
                        
                        instance_id = IdManager.instance().get_instance_id(reasy_id)
                        if instance_id:
                            # Find GameObject's object table index
                            parent = self.parent()
                            if hasattr(parent, "scn") and hasattr(parent.scn, "object_table"):
                                go_object_id = -1
                                for i, obj_id in enumerate(parent.scn.object_table):
                                    if obj_id == instance_id:
                                        go_object_id = i
                                        break
                                        
                                if go_object_id >= 0:
                                    self.delete_gameobject(index)
                                    return
        # Call the parent's keyPressEvent for default behavior
        super().keyPressEvent(event)

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
            self.expand(parent_index)
            self.create_widgets_for_children(parent_index)

        # Only connect the signal
        self.expanded.connect(embed_children_on_expand)

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
        
        is_embedded = False
        if hasattr(item, 'raw') and isinstance(item.raw, dict):
            is_embedded = item.raw.get("embedded", False)
        
        is_array = False
        array_type = ""
        data_obj = None
        is_array_element = False
        parent_array_item = None
        element_index = -1
        is_gameobject = False
        is_component = False
        component_instance_id = -1
        is_folder = False
        is_gameobjects_root = False
        is_resource = False
        resource_index = -1
        is_resources_section = False
        
        # Check if this is a resource node or the resources section itself
        if hasattr(item, 'raw') and isinstance(item.raw, dict):
            is_resource = item.raw.get("type") == "resource"
            if is_resource:
                resource_index = item.raw.get("resource_index", -1)
        
        # Check if this is the Resources header node
        if item.data and len(item.data) > 0:
            if item.data[0].startswith("Resources"):
                is_resources_section = True
        
        # Check if this is the GameObjects root node
        if item.data and len(item.data) > 0:
            if item.data[0] == "GameObjects":
                is_gameobjects_root = True
        
        # Check if this is a gameobject or folder
        if hasattr(item, 'raw') and isinstance(item.raw, dict):
            is_gameobject = item.raw.get("type") == "gameobject"
            is_folder = item.raw.get("type") == "folder"
        
        # Check if this is a component - use reasy_id instead of direct instance_id
        if not is_gameobject and item.parent and hasattr(item.parent, 'data') and item.parent.data and item.parent.data[0] == "Components":
            if hasattr(item, 'raw') and isinstance(item.raw, dict):
                # First check for reasy_id
                reasy_id = item.raw.get("reasy_id")
                if reasy_id:
                    component_instance_id = IdManager.instance().get_instance_id(reasy_id)
                    if component_instance_id:
                        is_component = True
                else:
                    raise("Could not fetch reasy_id for component")

        # Check if this is an array
        if hasattr(item, 'raw') and isinstance(item.raw, dict):
            is_array = item.raw.get("type") == "array"
            data_obj = item.raw.get("obj")
            if is_array and data_obj:
                if hasattr(data_obj, 'orig_type') and data_obj.orig_type:
                    array_type = data_obj.orig_type
        
        # Check if this is an array element
        if not is_array and item.parent and hasattr(item.parent, 'raw') and isinstance(item.parent.raw, dict):
            if item.parent.raw.get("type") == "array":
                parent_array_item = item.parent
                parent_data_obj = item.parent.raw.get("obj")
                if parent_data_obj and hasattr(parent_data_obj, 'orig_type'):
                    array_type = parent_data_obj.orig_type
                    is_array_element = True
                    # Get index of current element
                    for i, child in enumerate(parent_array_item.children):
                        if child == item:
                            element_index = i
                            break

        menu = QMenu(self)

        if is_resource:
            # Context menu for a resource entry
            edit_action = menu.addAction("Edit Resource Path")
            delete_action = menu.addAction("Delete Resource")
            action = menu.exec_(QCursor.pos())
            
            if action == edit_action:
                self.edit_resource(index, resource_index)
            elif action == delete_action:
                self.delete_resource(index, resource_index)
                
        elif is_resources_section:
            add_action = menu.addAction("Add New Resource")
            action = menu.exec_(QCursor.pos())
            
            if action == add_action:
                self.add_resource(index)
                
        elif is_gameobject:
            add_component_action = menu.addAction("Add Component")
            create_child_go_action = menu.addAction("Create Child GameObject")
            
            # Only show duplicate action if not a file with embedded RSZ
            duplicate_go_action = None
            parent_widget = self.parent()
            has_embedded_rsz = False
            
            if hasattr(parent_widget, "scn") and hasattr(parent_widget.scn, "rsz_userdata_infos"):
                # Check if any userdata has embedded RSZ
                for rui in parent_widget.scn.rsz_userdata_infos:
                    if (hasattr(rui, 'embedded_rsz_header') and 
                        hasattr(rui, 'embedded_instances') and 
                        rui.embedded_instances):
                        has_embedded_rsz = True
                        break
            
            if not has_embedded_rsz:
                duplicate_go_action = menu.addAction("Duplicate GameObject")
            
            go_has_prefab = False
            prefab_path = ""
            
            if hasattr(parent_widget, "scn") and not parent_widget.scn.is_pfb and not parent_widget.scn.is_usr:
                reasy_id = item.raw.get("reasy_id")
                go_instance_id = IdManager.instance().get_instance_id(reasy_id) if reasy_id else None
                
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
                manage_prefab_action = menu.addAction(f"Modify Prefab Path")
            else:
                manage_prefab_action = menu.addAction("Associate with Prefab")
            
            delete_go_action = menu.addAction("Delete GameObject")
            action = menu.exec_(QCursor.pos())
            
            if action == add_component_action:
                self.add_component_to_gameobject(index)
            elif action == create_child_go_action:
                self.create_child_gameobject(index)
            elif duplicate_go_action and action == duplicate_go_action:
                self.duplicate_gameobject(index)
            elif action == delete_go_action:
                self.delete_gameobject(index)
            elif action == manage_prefab_action:
                self.manage_gameobject_prefab(index, go_has_prefab, prefab_path)
                
        elif is_folder:
            create_go_action = menu.addAction("Create GameObject")
            delete_folder_action = menu.addAction("Delete Folder")
            action = menu.exec_(QCursor.pos())
            
            if action == create_go_action:
                self.create_gameobject_in_folder(index)
            elif action == delete_folder_action:
                self.delete_folder(index)
        
        elif is_gameobjects_root:
            create_go_action = menu.addAction("Create GameObject")
            action = menu.exec_(QCursor.pos())
            
            if action == create_go_action:
                self.create_root_gameobject(index)
        
        elif is_component:
            delete_action = menu.addAction("Delete Component")
            action = menu.exec_(QCursor.pos())
            
            if action == delete_action:
                self.delete_component(index, component_instance_id)
        
        elif array_type and is_array:
            add_action = menu.addAction("Add New Element")
            action = menu.exec_(QCursor.pos())
            
            if action == add_action:
                self.add_array_element(index, array_type, data_obj, item)
                
        elif is_array_element and element_index >= 0:
            delete_action = menu.addAction("Delete Element")
            action = menu.exec_(QCursor.pos())
            
            if action == delete_action:
                self.delete_array_element(parent_array_item, element_index, index)
        else:
            print(f"No special actions for this node type")

        # For embedded instances, we need to handle differently
        if is_embedded:
            # Currently no special menu options for embedded instances
            return

    def add_array_element(self, index, array_type, data_obj, array_item):
        """Add a new element to an array"""
        if not array_type.endswith("[]"):
            QMessageBox.warning(self, "Error", f"Invalid array type format: {array_type}")
            return
            
        element_type = array_type[:-2]  # Remove the [] suffix
        
        # Get the parent widget/handler that can create elements
        parent = self.parent()
        if not hasattr(parent, "create_array_element"):
            QMessageBox.warning(self, "Error", "Cannot find element creator")
            return
        
        # Check if this is in an embedded context
        embedded_context = self._find_embedded_context(array_item)
        
        try:
            if embedded_context:
                embedded_ops = RszEmbeddedArrayOperations(parent)
                new_element = embedded_ops.create_array_element(element_type, data_obj, embedded_context, 
                                                               direct_update=True, array_item=array_item)
            else:
                new_element = parent.create_array_element(element_type, data_obj, direct_update=True, array_item=array_item)
                
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
                try:
                    embedded_ops = RszEmbeddedArrayOperations(parent)
                    success = embedded_ops.delete_array_element(array_data, element_index, embedded_context)
                    
                    if success:
                        # Update the UI - remove the item from the tree
                        model = self.model()
                        if model:
                            array_index = model.getIndexFromItem(parent_array_item)
                            if array_index.isValid():
                                # Force model to reload this branch
                                model.removeRow(element_index, array_index)
                                
                                # Update indices in the UI for remaining elements
                                self._update_remaining_elements_ui(model, array_index, array_data, element_index)
                                
                        QMessageBox.information(self, "Element Deleted", f"Element {element_index} deleted successfully.")
                    else:
                        QMessageBox.critical(self, "Error", "Failed to delete element. See logs for details.")
                except Exception as e:
                    QMessageBox.critical(self, "Error", f"Failed to delete embedded element: {str(e)}")
                    traceback.print_exc()
            else:
                QMessageBox.warning(self, "Error", "Cannot access required components for embedded deletion")
            return
           
        if parent and hasattr(parent, "delete_array_element"):
            try:
                success = parent.delete_array_element(array_data, element_index)
                
                if success:
                    # Update the UI - remove the item from the tree
                    model = self.model()
                    if model:
                        array_index = model.getIndexFromItem(parent_array_item)
                        if array_index.isValid():
                            # Force model to reload this branch
                            model.removeRow(element_index, array_index)
                            
                            # Update indices in the UI for remaining elements
                            self._update_remaining_elements_ui(model, array_index, array_data, element_index)
                                    
                    if parent and hasattr(parent, "mark_modified"):
                        parent.mark_modified()
                    elif self.parent_modified_callback:
                        self.parent_modified_callback()
                
                    QMessageBox.information(self, "Element Deleted", f"Element {element_index} deleted successfully.")
                else:
                    QMessageBox.critical(self, "Error", "Failed to delete element. See logs for details.")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to delete element: {str(e)}")
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
                element = array_data.values[element_index]
                
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
        
        Args:
            item: The tree item to check
            
        Returns:
            object: The embedded context (RSZUserDataInfo) or None
        """
        current = item
        while current:
            if hasattr(current, 'raw') and isinstance(current.raw, dict):
                if current.raw.get("embedded", False):
                    domain_id = current.raw.get("domain_id")
                    if domain_id:
                        parent = self.parent()
                        if hasattr(parent, "scn") and hasattr(parent.scn, "_rsz_userdata_dict"):
                            if domain_id in parent.scn._rsz_userdata_dict:
                                rui = parent.scn._rsz_userdata_dict[domain_id]
                                if hasattr(rui, 'embedded_instances'):
                                    return rui
                                    
                    ancestor = current
                    while ancestor:
                        if (hasattr(ancestor, 'raw') and 
                            isinstance(ancestor.raw, dict) and
                            "embedded_context" in ancestor.raw):
                            return ancestor.raw["embedded_context"]
                        ancestor = ancestor.parent
            
            current = current.parent
            
        return None

    def delete_component(self, index, component_instance_id):
        """Delete a component from its GameObject"""
        if component_instance_id <= 0:
            QMessageBox.warning(self, "Error", "Invalid component")
            return
        
        # Get the component node to access its reasy_id
        item = index.internalPointer()
        if item and hasattr(item, 'raw') and isinstance(item.raw, dict):
            # If we have a reasy_id, get the latest instance_id
            reasy_id = item.raw.get("reasy_id")
            if reasy_id:
                updated_instance_id = IdManager.instance().get_instance_id(reasy_id)
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
        
        # Get the parent widget/handler that can perform the deletion
        parent = self.parent()
        if not hasattr(parent, "delete_component_from_gameobject"):
            QMessageBox.warning(self, "Error", "Component deletion not supported")
            return
        
        try:
            success = parent.delete_component_from_gameobject(component_instance_id)
            if success:
                QMessageBox.information(self, "Success", "Component deleted successfully")
                parent.populate_tree()
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
            
        folder_instance_id = IdManager.instance().get_instance_id(reasy_id)
        if not folder_instance_id:
            QMessageBox.warning(self, "Error", "Could not determine folder instance ID")
            return
        
        # Get the parent widget/handler for GameObject creation
        parent = self.parent()
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
            success = parent.create_gameobject(name, folder_object_id)
            
            if success:
                QMessageBox.information(self, "Success", f"GameObject '{name}' created successfully")
                # Refresh the tree to show the new GameObject
                parent.populate_tree()
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
            
        parent_instance_id = IdManager.instance().get_instance_id(reasy_id)
        if not parent_instance_id:
            QMessageBox.warning(self, "Error", "Could not determine GameObject instance ID")
            return
        
        # Get the parent widget/handler for GameObject creation
        parent_widget = self.parent()
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
            success = parent_widget.create_gameobject(name, parent_object_id)
            
            if success:
                QMessageBox.information(self, "Success", f"Child GameObject '{name}' created successfully")
                parent_widget.populate_tree()
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
            success = parent.create_gameobject(name, -1)
            
            if success:
                QMessageBox.information(self, "Success", f"GameObject '{name}' created successfully")
                parent.populate_tree()
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
            
        instance_id = IdManager.instance().get_instance_id(reasy_id)
        if not instance_id:
            QMessageBox.warning(self, "Error", "Could not determine GameObject instance ID")
            return
        
        parent = self.parent()
        if not hasattr(parent, "create_component_for_gameobject") or not hasattr(parent, "type_registry"):
            QMessageBox.warning(self, "Error", "Component creation not supported")
            return
        
        dialog = ComponentSelectorDialog(self, parent.type_registry)
        component_type = dialog.get_selected_component() if dialog.exec_() else None
        
        if not component_type:
            return
            
        try:
            success = parent.create_component_for_gameobject(instance_id, component_type)
            if success:
                QMessageBox.information(self, "Success", f"Added {component_type} to GameObject")
                parent.populate_tree() 
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
            
        instance_id = IdManager.instance().get_instance_id(reasy_id)
        if not instance_id:
            QMessageBox.warning(self, "Error", "Could not determine GameObject instance ID")
            return
        
        parent = self.parent()
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
                            if child and hasattr(child, 'data') and child.data and child.data[0] == "GameObjects":
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
                QMessageBox.information(self, "Success", "GameObject deleted successfully")
                parent.populate_tree()
            else:
                QMessageBox.warning(self, "Error", "Failed to delete GameObject")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error deleting GameObject: {str(e)}")

    def duplicate_gameobject(self, index):
        """Duplicate a GameObject and all its children and components"""
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
            
        instance_id = IdManager.instance().get_instance_id(reasy_id)
        if not instance_id:
            QMessageBox.warning(self, "Error", "Could not determine GameObject instance ID")
            return
        
        parent = self.parent()
        if not hasattr(parent, "object_operations") or not hasattr(parent.object_operations, "duplicate_gameobject"):
            QMessageBox.warning(self, "Error", "GameObject duplication not supported")
            return
        
        go_object_id = -1
        for i, obj_id in enumerate(parent.scn.object_table):
            if obj_id == instance_id:
                go_object_id = i
                break
        
        if go_object_id < 0:
            QMessageBox.warning(self, "Error", "Could not find GameObject in object table")
            return
        
        name = ""
        
        if hasattr(item, 'data') and item.data:
            name_parts = item.data[0].split(' (ID:')
            name = name_parts[0]
            
        if not name or name == "":
            name = "GameObject"
        
        new_name, ok = QInputDialog.getText(self, "Duplicate GameObject", 
                                          "Enter name for the duplicate:", 
                                          QLineEdit.Normal, f"{name}_Copy")
        if not ok:
            return
        
        if not new_name or new_name.strip() == "":
            new_name = f"{name}_Copy"
            
        try:
            # Find parent ID of the GameObject
            parent_id = -1
            for go in parent.scn.gameobjects:
                if go.id == go_object_id:
                    parent_id = go.parent_id
                    break

            success = parent.object_operations.duplicate_gameobject(go_object_id, new_name, parent_id)
            
            if success:
                QMessageBox.information(self, "Success", f"GameObject '{name}' duplicated successfully")
                parent.populate_tree()
            else:
                QMessageBox.warning(self, "Error", "Failed to duplicate GameObject")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error duplicating GameObject: {str(e)}")
            traceback.print_exc()

    def _is_valid_parent_for_go(self, index):
        """Check if index is a valid parent for a GameObject node"""
        if not index.isValid():
            return False
        
        item = index.internalPointer()
        if not item or not hasattr(item, 'data') or not item.data:
            return False
        
        # Valid parents are "GameObjects", "Children" nodes, or folders
        if item.data[0] == "GameObjects" or item.data[0] == "Children":
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
            
        folder_instance_id = IdManager.instance().get_instance_id(reasy_id)
        if not folder_instance_id:
            QMessageBox.warning(self, "Error", "Could not determine folder instance ID")
            return
        
        parent = self.parent()
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
                QMessageBox.information(self, "Success", f"Folder '{folder_name}' deleted successfully")
                parent.populate_tree()  # Refresh the tree view
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
            
        instance_id = IdManager.instance().get_instance_id(reasy_id)
        if not instance_id:
            QMessageBox.warning(self, "Error", "Could not determine GameObject instance ID")
            return
        
        parent = self.parent()
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
            if not path.endswith(".pfb") and not path.lower().startswith("pfb/"):
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
                
            self._update_resources_ui(f"Added resource '{path}'")
        except Exception as e:
            self._handle_resource_error("add", e)

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
            resource = parent.scn.resource_infos[resource_index]
            if hasattr(parent.scn, 'get_resource_string'):
                return parent.scn.get_resource_string(resource) or "[Unknown]"
        except Exception:
            pass
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
        parent = self.parent()
        QMessageBox.information(self, "Success", success_message)
        parent.populate_tree()

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

class TreeWidgetFactory:
    """Factory class for creating tree node widgets"""
    
    # Centralized widget type mapping - used by RszViewer currently
    WIDGET_TYPES = {
        "Vec2Data": Vec2Input,
        "Vec4Data": Vec4Input,
        "Float4Data": Vec4Input,
        "QuaternionData": Vec4Input,
        "Vec3Data": Vec3Input,
        "GameObjectRefData": GuidInput,
        "GuidData": GuidInput,
        "OBBData": OBBInput,
        "Mat4Data": Mat4Input,
        "RawBytesData": HexBytesInput,
        "StringData": StringInput,
        "BoolData": BoolInput,
        "F32Data": F32Input,
        "S32Data": S32Input,
        "U32Data": U32Input,
        "U64Data": U64Input,
        "S8Data": S8Input,
        "UserDataData": UserDataInput,
        "U8Data": U8Input, 
        "RangeData": RangeInput,
        "RangeIData": RangeIInput,
        "EnumInput": EnumInput,
        "ColorData": ColorInput,
        "CapsuleData": CapsuleInput,
    }

    @staticmethod
    def create_widget(node_type, data_obj, name_text, widget_parent, on_modified=None):
        """Create appropriate widget based on node type"""
        widget = QWidget(widget_parent)
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(4)
        
        is_enum = False
        enum_type = None
        
        if data_obj and hasattr(data_obj, 'orig_type') and data_obj.orig_type:
            enum_type = data_obj.orig_type
            enum_values = EnumManager.instance().get_enum_values(enum_type)
            if enum_values:
                is_enum = True
        
        if is_enum:
            label = QLabel(name_text)
            layout.addWidget(label)
            
            input_widget = EnumInput(parent=widget)
            input_widget.set_data(data_obj)
            input_widget.set_enum_values(EnumManager.instance().get_enum_values(enum_type))
            layout.addWidget(input_widget)
            
            if on_modified:
                input_widget.modified_changed.connect(on_modified)
        
        elif node_type in TreeWidgetFactory.WIDGET_TYPES and data_obj:
            label = QLabel(name_text)
            layout.addWidget(label)
            
            input_class = TreeWidgetFactory.WIDGET_TYPES[node_type]
            input_widget = input_class(parent=widget)
            input_widget.set_data(data_obj)
            layout.addWidget(input_widget)
            
            if on_modified:
                input_widget.modified_changed.connect(on_modified)
                
            if node_type == "OBBData" or node_type == "Mat4Data":
                widget.setFixedHeight(150)
                
        # Icon widgets
        elif node_type in ("gameobject", "folder"):
            icon = QIcon(f"resources/icons/{node_type}.png")
            icon_label = QLabel()
            icon_label.setFixedWidth(16)
            icon_label.setPixmap(icon.pixmap(16, 16))
            
            text_label = QLabel(name_text)
            text_label.setStyleSheet("padding-left: 2px;")
            
            layout.addWidget(icon_label)
            layout.addWidget(text_label, 1)
            widget.setFixedHeight(30)
            
        # Default text widgets
        else:
            layout.addWidget(QLabel(name_text))
            widget.setFixedHeight(24)
            
        return widget

    @staticmethod
    def should_create_widget(item):
        """Check if we should create a widget for this item"""
        if not isinstance(item.raw, dict):
            return True
            
        # Skip array nodes and array elements
        if item.raw.get("type") == "array":
            return False
            
        parent = item.parent
        if parent and isinstance(parent.raw, dict) and parent.raw.get("type") == "array":
            return False
            
        return True

    @staticmethod
    def should_skip_widget(item):
        """Only create widgets for editable data objects"""
        if not isinstance(item.raw, dict):
            return True

        # Trash, needs rework
        name = item.raw.get("data", [""])[0]
        if name in ("Header", "GameObjects", "RSZHeader", "Folder Infos", "Object Table", "Instance Infos", "RSZUserData Infos"):
            return True
            
        # Don't skip array element widgets if they have a supported type
        node_type = item.raw.get("type")
        if node_type in TreeWidgetFactory.WIDGET_TYPES:
            return False
        return False
