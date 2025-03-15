"""
Specialized operations for arrays in embedded RSZ structures.

This module handles array operations (adding/removing elements) specifically for
embedded RSZ data structures found in SCN.19 files.
"""

import traceback

from PySide6.QtWidgets import QMessageBox
from file_handlers.rsz.rsz_data_types import *
from utils.id_manager import EmbeddedIdManager
from file_handlers.pyside.tree_model import DataTreeBuilder


class RszEmbeddedArrayOperations:
    """
    Specialized class for handling array operations in embedded RSZ structures.
    This is particularly important for SCN.19 files with nested embedded structures.
    """
    
    def __init__(self, viewer):
        """
        Initialize with a reference to the viewer
        
        Args:
            viewer: The parent RszViewer instance
        """
        self.viewer = viewer
        self.type_registry = viewer.type_registry
        
    def delete_array_element(self, array_data, element_index, rui):
        """
        Handle deletion of an array element in an embedded RSZ structure
        
        Args:
            array_data: The ArrayData object containing the element
            element_index: The index of the element to delete
            rui: The parent RSZUserDataInfo that contains this embedded structure
            
        Returns:
            bool: True if deletion was successful, False otherwise
        """
        if not array_data or not hasattr(array_data, 'values') or element_index >= len(array_data.values):
            return False
            
        element = array_data.values[element_index]
        
        instance_id = 0
        ref_type = None
        
        if isinstance(element, ObjectData) and element.value > 0:
            instance_id = element.value
            ref_type = "object"
        elif isinstance(element, UserDataData) and hasattr(element, "index") and element.index > 0:
            instance_id = element.index
            ref_type = "userdata"
        
        if instance_id > 0 and ref_type:
            print(f"Deleting array element with {ref_type} reference to instance {instance_id}")
            
            instance_exists = False
            if ref_type == "object" and hasattr(rui, 'embedded_instances'):
                instance_exists = instance_id in rui.embedded_instances
            elif ref_type == "userdata" and hasattr(rui, 'embedded_userdata_infos'):
                instance_exists = any(ud_info.instance_id == instance_id for ud_info in rui.embedded_userdata_infos)
                
            if not instance_exists:
                print(f"Warning: Referenced {ref_type} instance {instance_id} doesn't exist - cleaning up reference")
                del array_data.values[element_index]
                self._mark_parent_chain_modified(rui)
                return True
            
            is_referenced_elsewhere = self._check_embedded_instance_referenced_elsewhere(
                instance_id, array_data, element_index, ref_type, rui
            )
            
            if is_referenced_elsewhere:
                print(f"Instance {instance_id} is referenced elsewhere - just removing array element reference")
                del array_data.values[element_index]
                self._mark_parent_chain_modified(rui)
                return True
            
            del array_data.values[element_index]
            
            if ref_type == "userdata":
                if self._delete_embedded_userdata(instance_id, rui):
                    self._mark_parent_chain_modified(rui)
                    return True
                else:
                    print(f"Failed to delete UserData {instance_id}")
            else:
                if self._delete_embedded_instance(instance_id, ref_type, rui):
                    self._mark_parent_chain_modified(rui)
                    return True
            
            return False
        else:
            del array_data.values[element_index]
            self._mark_parent_chain_modified(rui)
            return True
    
    def _delete_embedded_userdata(self, userdata_id, rui):
        """
        Delete a UserData instance from an embedded RSZ structure
        
        Args:
            userdata_id: The ID of the UserData to delete
            rui: The RSZUserDataInfo containing the embedded structure
            
        Returns:
            bool: Success or failure
        """
        if not hasattr(rui, 'embedded_userdata_infos'):
            return False
            
        userdata_to_delete = None
        userdata_index = -1
        
        for i, userdata_info in enumerate(rui.embedded_userdata_infos):
            if userdata_info.instance_id == userdata_id:
                userdata_to_delete = userdata_info
                userdata_index = i
                break
                
        if not userdata_to_delete:
            return False
            
        try:
            print(f"Deleting embedded UserData with ID {userdata_id} at index {userdata_index}")
            
            rui.embedded_userdata_infos.pop(userdata_index)
            
            if hasattr(userdata_to_delete, 'embedded_instances') and userdata_to_delete.embedded_instances:
                if hasattr(rui, 'embedded_instance_infos'):
                    instance_info_index = next((i for i, info in enumerate(rui.embedded_instance_infos) 
                                             if i == userdata_id), -1)
                    if instance_info_index >= 0:
                        rui.embedded_instance_infos.pop(instance_info_index)
                        print(f"Removed instance info at index {instance_info_index}")
                
                if hasattr(rui, 'embedded_instances') and userdata_id in rui.embedded_instances:
                    del rui.embedded_instances[userdata_id]
            
            if hasattr(rui, 'embedded_rsz_header'):
                rui.embedded_rsz_header.userdata_count = len(rui.embedded_userdata_infos)
            
            rui.modified = True
            
            return True
        except Exception as e:
            print(f"Error deleting UserData: {str(e)}")
            traceback.print_exc()
            return False
    
    def _delete_embedded_instance(self, instance_id, ref_type, rui):
        """
        Delete an instance from an embedded RSZ structure
        
        Args:
            instance_id: The instance ID to delete
            ref_type: 'object' or 'userdata'
            rui: The RSZUserDataInfo containing the embedded structure
            
        Returns:
            bool: Success or failure
        """
        if not hasattr(rui, 'embedded_instances') or instance_id not in rui.embedded_instances:
            return False
            
        all_nested_objects = self._collect_embedded_nested_objects(instance_id, rui)
        all_nested_objects.add(instance_id)
        
        try:
            print(f"Deleting embedded {ref_type} {instance_id} with {len(all_nested_objects)-1} nested objects")
            for nested_id in sorted(all_nested_objects):
                print(f"  - Deleting embedded ID: {nested_id}")
            
            id_adjustments = {}
            
            max_instance_id = 0
            if hasattr(rui, 'embedded_instances') and rui.embedded_instances:
                max_instance_id = max(rui.embedded_instances.keys()) + 1
            
            for i in range(max_instance_id):
                if i in all_nested_objects:
                    id_adjustments[i] = -1
                else:
                    offset = sum(1 for deleted_id in all_nested_objects if deleted_id < i)
                    if offset > 0:
                        id_adjustments[i] = i - offset
                    else:
                        id_adjustments[i] = i
            
            print("ID Adjustments:")
            for old_id, new_id in sorted(id_adjustments.items()):
                if new_id >= 0: 
                    print(f"  {old_id} -> {new_id}")
            
            self._update_embedded_references(all_nested_objects, id_adjustments, rui)
            
            for deleted_id in all_nested_objects:
                if deleted_id in rui.embedded_instances:
                    del rui.embedded_instances[deleted_id]
            
            updated_instances = {}
            for old_id, fields in list(rui.embedded_instances.items()):
                new_id = id_adjustments.get(old_id, old_id)
                if new_id >= 0:
                    updated_instances[new_id] = fields
                
            rui.embedded_instances = updated_instances
            
            if hasattr(rui, 'embedded_instance_infos'):
                new_instance_infos = []
                for i, info in enumerate(rui.embedded_instance_infos):
                    if i not in all_nested_objects:
                        new_instance_infos.append(info)
                rui.embedded_instance_infos = new_instance_infos
                
            if hasattr(rui, 'embedded_object_table'):
                new_object_table = []
                for i, ref_id in enumerate(rui.embedded_object_table):
                    if ref_id in all_nested_objects:
                        new_object_table.append(0)
                    elif ref_id in id_adjustments:
                        new_id = id_adjustments[ref_id]
                        if new_id >= 0 and new_id < max_instance_id - len(all_nested_objects):
                            new_object_table.append(new_id)
                        else:
                            new_object_table.append(0)
                    else:
                        new_object_table.append(ref_id)
                        
                rui.embedded_object_table = new_object_table
                
            if hasattr(rui, 'id_manager') and isinstance(rui.id_manager, EmbeddedIdManager):
                rui.id_manager.reset()
                
                for instance_id in rui.embedded_instances.keys():
                    rui.id_manager.register_instance(instance_id)
                    
            rui.modified = True
            
            if hasattr(rui, 'embedded_rsz_header'):
                if hasattr(rui, 'embedded_object_table'):
                    rui.embedded_rsz_header.object_count = len(rui.embedded_object_table)
                
                if hasattr(rui, 'embedded_instance_infos'):
                    rui.embedded_rsz_header.instance_count = len(rui.embedded_instance_infos)
                
                if hasattr(rui, 'embedded_userdata_infos'):
                    rui.embedded_rsz_header.userdata_count = len(rui.embedded_userdata_infos)
            
            self._validate_embedded_references(rui)
            
            return True
        except Exception as e:
            print(f"Error during embedded {ref_type} deletion: {str(e)}")
            traceback.print_exc()
            return False

    def _update_embedded_references(self, deleted_ids, id_adjustments, rui):
        """
        Update references in all embedded instances after deletion
        
        Args:
            deleted_ids: Set of instance IDs being deleted
            id_adjustments: Mapping of old IDs to new IDs after deletion
            rui: The RSZUserDataInfo containing the embedded structure
        """
        if not hasattr(rui, 'embedded_instances'):
            return
            
        for instance_id, fields in list(rui.embedded_instances.items()):
            if instance_id in deleted_ids or not isinstance(fields, dict):
                continue
                
            for field_name, field_data in fields.items():
                if isinstance(field_data, ObjectData):
                    ref_id = field_data.value
                    if ref_id > 0:
                        if ref_id in deleted_ids:
                            field_data.value = 0
                        elif ref_id in id_adjustments:
                            new_id = id_adjustments[ref_id]
                            if new_id >= 0:
                                field_data.value = new_id
                            else:
                                field_data.value = 0
                
                elif isinstance(field_data, UserDataData) and hasattr(field_data, "index"):
                    ref_id = field_data.index
                    if ref_id > 0:
                        if ref_id in deleted_ids:
                            field_data.index = 0
                        elif ref_id in id_adjustments:
                            new_id = id_adjustments[ref_id]
                            if new_id >= 0:
                                field_data.index = new_id
                            else:
                                field_data.index = 0
                
                elif isinstance(field_data, ArrayData) and hasattr(field_data, 'values'):
                    updated_values = []
                    for element in field_data.values:
                        if isinstance(element, ObjectData):
                            ref_id = element.value
                            if ref_id > 0:
                                if ref_id in deleted_ids:
                                    element.value = 0
                                elif ref_id in id_adjustments:
                                    new_id = id_adjustments[ref_id] 
                                    if new_id >= 0:
                                        element.value = new_id
                                    else:
                                        element.value = 0
                        elif isinstance(element, UserDataData) and hasattr(element, "index"):
                            ref_id = element.index
                            if ref_id > 0:
                                if ref_id in deleted_ids:
                                    element.index = 0
                                elif ref_id in id_adjustments:
                                    new_id = id_adjustments[ref_id]
                                    if new_id >= 0:
                                        element.index = new_id
                                    else:
                                        element.index = 0
                        
                        updated_values.append(element)
                    
                    field_data.values = updated_values
                    
        if hasattr(rui, 'embedded_userdata_infos'):
            for userdata_info in rui.embedded_userdata_infos:
                if userdata_info.instance_id in deleted_ids:
                    userdata_info.instance_id = 0
                elif userdata_info.instance_id in id_adjustments:
                    new_id = id_adjustments[userdata_info.instance_id]
                    if new_id >= 0:
                        userdata_info.instance_id = new_id

    def _validate_embedded_references(self, rui):
        """
        Validate all references in embedded instances to ensure they are valid
        
        Args:
            rui: The RSZUserDataInfo containing the embedded structure
        """
        if not hasattr(rui, 'embedded_instances'):
            return
            
        instance_count = max(rui.embedded_instances.keys()) + 1 if rui.embedded_instances else 0
        
        if hasattr(rui, 'embedded_object_table'):
            for i, ref_id in enumerate(rui.embedded_object_table):
                if ref_id >= instance_count:
                    print(f"Warning: Invalid reference {ref_id} in embedded object table entry {i} (max valid: {instance_count-1})")
                    rui.embedded_object_table[i] = 0
        
        for instance_id, fields in rui.embedded_instances.items():
            if not isinstance(fields, dict):
                continue
                
            for field_name, field_data in list(fields.items()):
                if isinstance(field_data, ObjectData):
                    ref_id = field_data.value
                    if ref_id > 0 and ref_id >= instance_count:
                        print(f"Warning: Invalid instance reference {ref_id} in field {field_name} (instance {instance_id})")
                        field_data.value = 0
                
                elif isinstance(field_data, UserDataData) and hasattr(field_data, "index"):
                    ref_id = field_data.index
                    if ref_id > 0:
                        is_valid = False
                        if hasattr(rui, 'embedded_userdata_infos'):
                            is_valid = any(userdata.instance_id == ref_id 
                                          for userdata in rui.embedded_userdata_infos)
                        
                        if not is_valid:
                            print(f"Warning: Invalid UserData reference {ref_id} in field {field_name} (instance {instance_id})")
                            field_data.index = 0
                
                elif isinstance(field_data, ArrayData) and hasattr(field_data, 'values'):
                    for i, element in enumerate(field_data.values):
                        if isinstance(element, ObjectData):
                            ref_id = element.value
                            if ref_id > 0 and ref_id >= instance_count:
                                print(f"Warning: Invalid reference {ref_id} in array {field_name}[{i}] (instance {instance_id})")
                                element.value = 0
                        elif isinstance(element, UserDataData) and hasattr(element, "index"):
                            ref_id = element.index
                            if ref_id > 0:
                                is_valid = False
                                if hasattr(rui, 'embedded_userdata_infos'):
                                    is_valid = any(userdata.instance_id == ref_id 
                                                  for userdata in rui.embedded_userdata_infos)
                                
                                if not is_valid:
                                    print(f"Warning: Invalid UserData reference {ref_id} in array {field_name}[{i}] (instance {instance_id})")
                                    element.index = 0

    def _check_embedded_instance_referenced_elsewhere(self, instance_id, current_array, current_index, ref_type, rui):
        """
        Check if an instance within an embedded structure is referenced from elsewhere
        
        Args:
            instance_id: The instance ID to check
            current_array: The array containing the reference we're deleting
            current_index: Index of the element we're deleting
            ref_type: 'object' or 'userdata'
            rui: The RSZUserDataInfo containing the embedded structure
            
        Returns:
            bool: True if referenced elsewhere, False if only referenced from current element
        """
        if not hasattr(rui, 'embedded_instances') or not rui.embedded_instances:
            return False
            
        is_userdata_context = hasattr(rui, 'instance_id') and rui.instance_id > 0
        print(f"Checking references in {'UserData' if is_userdata_context else 'standard'} context")
        
        reference_count = 0
        
        if hasattr(current_array, 'values'):
            for i, item in enumerate(current_array.values):
                if i == current_index:
                    continue
                    
                if ref_type == "object" and isinstance(item, ObjectData) and item.value == instance_id:
                    reference_count += 1
                    print(f"Found reference to instance {instance_id} in same array at index {i}")
                elif ref_type == "userdata" and isinstance(item, UserDataData) and hasattr(item, "index") and item.index == instance_id:
                    reference_count += 1
                    print(f"Found reference to userdata {instance_id} in same array at index {i}")
        
        for check_id, fields in rui.embedded_instances.items():
            if not isinstance(fields, dict):
                continue
                
            for field_name, field_data in fields.items():
                if field_data is current_array:
                    continue
                    
                if isinstance(field_data, ObjectData) and field_data.value == instance_id:
                    reference_count += 1
                    print(f"Found reference to instance {instance_id} in field {field_name} of instance {check_id}")
                elif isinstance(field_data, UserDataData) and hasattr(field_data, "index") and field_data.index == instance_id:
                    reference_count += 1
                    print(f"Found reference to userdata {instance_id} in field {field_name} of instance {check_id}")
                    
                elif isinstance(field_data, ArrayData):
                    for i, item in enumerate(field_data.values):
                        if ref_type == "object" and isinstance(item, ObjectData) and item.value == instance_id:
                            reference_count += 1
                            print(f"Found reference to instance {instance_id} in array {field_name}[{i}] of instance {check_id}")
                        elif ref_type == "userdata" and isinstance(item, UserDataData) and hasattr(item, "index") and item.index == instance_id:
                            reference_count += 1
                            print(f"Found reference to userdata {instance_id} in array {field_name}[{i}] of instance {check_id}")
        
        return reference_count > 0
    
    def _collect_embedded_nested_objects(self, root_instance_id, rui):
        """
        Collect all nested objects owned by a specific instance in an embedded structure
        
        Args:
            root_instance_id: The ID of the root object to analyze
            rui: The RSZUserDataInfo containing the embedded structure
            
        Returns:
            set: Set of nested object instance IDs
        """
        nested_objects = set()
        
        if not hasattr(rui, 'embedded_instances'):
            return nested_objects
            
        processed_ids = set()
        
        def explore_instance(instance_id):
            if instance_id in processed_ids:
                return
                
            processed_ids.add(instance_id)
            
            if instance_id not in rui.embedded_instances or not isinstance(rui.embedded_instances[instance_id], dict):
                return
                
            fields = rui.embedded_instances[instance_id]
            
            for field_name, field_data in fields.items():
                if isinstance(field_data, ObjectData) and field_data.value > 0:
                    ref_id = field_data.value
                    
                    if ref_id != instance_id and ref_id not in processed_ids:
                        if ref_id in rui.embedded_instances:
                            nested_objects.add(ref_id)
                            explore_instance(ref_id)
                            
                elif isinstance(field_data, UserDataData) and hasattr(field_data, "index") and field_data.index > 0:
                    index_id = field_data.index
                    if index_id != instance_id and index_id not in processed_ids:
                        if index_id in rui.embedded_instances:
                            nested_objects.add(index_id)
                            explore_instance(index_id)
                
                elif isinstance(field_data, ArrayData):
                    for element in field_data.values:
                        if isinstance(element, ObjectData) and element.value > 0:
                            ref_id = element.value
                            
                            if ref_id != instance_id and ref_id not in processed_ids:
                                if ref_id in rui.embedded_instances:
                                    is_exclusive = self._is_exclusively_referenced_from(
                                        ref_id, instance_id, rui
                                    )
                                    
                                    if is_exclusive:
                                        nested_objects.add(ref_id)
                                        explore_instance(ref_id)
                        elif isinstance(element, UserDataData) and hasattr(element, "index") and element.index > 0:
                            index_id = element.index
                            if index_id != instance_id and index_id not in processed_ids:
                                if index_id in rui.embedded_instances:
                                    is_exclusive = self._is_exclusively_referenced_from(
                                        index_id, instance_id, rui
                                    )
                                    
                                    if is_exclusive:
                                        nested_objects.add(index_id)
                                        explore_instance(index_id)
        
        explore_instance(root_instance_id)
        return nested_objects
    
    def _is_exclusively_referenced_from(self, instance_id, source_id, rui):
        """
        Check if an instance is exclusively referenced from a specific source in the embedded structure
        
        Args:
            instance_id: The instance ID to check
            source_id: The source instance ID that should be the only referencer
            rui: The RSZUserDataInfo containing the embedded structure
            
        Returns:
            bool: True if exclusively referenced from source, False otherwise
        """
        if not hasattr(rui, 'embedded_instances'):
            return True
        
        for check_id, fields in rui.embedded_instances.items():
            if check_id == source_id or not isinstance(fields, dict):
                continue
                
            for field_name, field_data in fields.items():
                if isinstance(field_data, ObjectData) and field_data.value == instance_id:
                    return False
                    
                elif isinstance(field_data, ArrayData):
                    for element in field_data.values:
                        if isinstance(element, ObjectData) and element.value == instance_id:
                            return False
        
        return True
    
    def _mark_parent_chain_modified(self, rui):
        """
        Mark the entire parent chain as modified
        
        Args:
            rui: The RSZUserDataInfo containing the embedded structure
        """
        if hasattr(rui, 'mark_modified'):
            rui.mark_modified()
            
        if hasattr(rui, 'parent_userdata_rui') and hasattr(rui.parent_userdata_rui, 'mark_modified'):
            rui.parent_userdata_rui.mark_modified()
            
        self.viewer.mark_modified()

    def create_array_element(self, element_type, array_data, rui, direct_update=False, array_item=None):
        """
        Create a new element for an array in an embedded RSZ structure
        
        This method is similar to RszArrayOperations.create_array_element but adapted for embedded structures
        
        Args:
            element_type: The type of element to create
            array_data: The array to add the element to
            rui: The RSZUserDataInfo containing the embedded structure
            direct_update: Whether to update the UI directly (optional)
            array_item: The tree item representing the array (if direct_update is True)
            
        Returns:
            object: The created element or None if creation failed
        """
        element_class = getattr(array_data, 'element_class', None) if array_data else None
        
        if not self.type_registry or not array_data or not element_class:
            QMessageBox.warning(self.viewer, "Error", "Missing required data for array element creation")
            return None
            
        type_info, type_id = self.type_registry.find_type_by_name(element_type)
        if not type_info:
            QMessageBox.warning(self.viewer, "Error", f"Type not found in registry: {element_type}")
            return None
        
        if element_class == ObjectData:
            QMessageBox.warning(self.viewer, "Error", 
                                "Creating object instances in embedded arrays is not yet supported")
            return None
        else:
            new_element = self.viewer._create_default_field(element_class, array_data.orig_type)
        
        if new_element:
            array_data.values.append(new_element)
            self._mark_parent_chain_modified(rui)
            
            if direct_update and array_item and hasattr(self.viewer.tree, 'model'):
                self._add_element_to_ui_direct(array_item, new_element)
                
            QMessageBox.information(self.viewer, "Element Added", 
                                    f"New {element_type} element added successfully.")
            
        return new_element
        
    def _add_element_to_ui_direct(self, array_item, element):
        """
        Add a new element directly to the UI
        
        Args:
            array_item: The tree item representing the array
            element: The element that was added
            
        Returns:
            bool: Success or failure
        """
        model = self.viewer.tree.model()
        if not model or not hasattr(array_item, 'raw'):
            return False
            
        array_data = array_item.raw.get('obj') if isinstance(array_item.raw, dict) else None
        if not array_data or not hasattr(array_data, 'values'):
            return False
        
        element_index = len(array_data.values) - 1
        
        node_data = DataTreeBuilder.create_data_node(
            f"{element_index}: ", "", element.__class__.__name__, element
        )
        
        model.addChild(array_item, node_data)
        
        array_index = model.getIndexFromItem(array_item)
        self.viewer.tree.expand(array_index)
        
        return True
