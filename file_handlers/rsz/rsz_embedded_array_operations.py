"""
Specialized operations for arrays in embedded RSZ structures.

This module handles array operations (adding/removing elements) specifically for
embedded RSZ data structures found in SCN.19 files.
"""

import traceback
from PySide6.QtWidgets import QMessageBox
from file_handlers.rsz.rsz_data_types import ObjectData, UserDataData, ArrayData
from utils.id_manager import EmbeddedIdManager
from file_handlers.pyside.tree_model import DataTreeBuilder


def _update_rsz_header_counts(rui, skip_instance_count=False):
    """
    Helper to update object_count, instance_count, and userdata_count in embedded_rsz_header.
    If skip_instance_count is True, we do not overwrite instance_count (some operations need
    special instance_count handling).
    """
    if hasattr(rui, 'embedded_rsz_header'):
        if hasattr(rui, 'embedded_object_table'):
            rui.embedded_rsz_header.object_count = len(rui.embedded_object_table)
        if not skip_instance_count and hasattr(rui, 'embedded_instance_infos'):
            rui.embedded_rsz_header.instance_count = len(rui.embedded_instance_infos)
        if hasattr(rui, 'embedded_userdata_infos'):
            rui.embedded_rsz_header.userdata_count = len(rui.embedded_userdata_infos)


class RszEmbeddedArrayOperations:
    
    def __init__(self, viewer):
        self.viewer = viewer
        self.type_registry = viewer.type_registry

    def delete_array_element(self, array_data, element_index, fallback_rui):
        if not array_data or not hasattr(array_data, 'values') or element_index >= len(array_data.values):
            return False
            
        element = array_data.values[element_index]
        instance_id = 0
        ref_type = None
        if isinstance(element, ObjectData) and element.value > 0:
            instance_id = element.value
            ref_type = "object"
        elif isinstance(element, UserDataData) and element.value > 0:
            instance_id = element.value
            ref_type = "userdata"
            
        if instance_id > 0 and ref_type:
            print(f"[DEBUG] Deleting array element with {ref_type} reference to instance {instance_id}")

            target_context = getattr(array_data, '_owning_context', None)
            if not target_context:
                target_context = getattr(element, '_container_context', None)
            if not target_context:
                target_context = fallback_rui

            print(f"[DEBUG] Using context: {repr(target_context)}")

            instance_exists = False
            if ref_type == "object":
                instance_exists = (
                    hasattr(target_context, 'embedded_instances')
                    and instance_id in getattr(target_context, 'embedded_instances', {})
                )
            else:
                instance_exists = (
                    hasattr(target_context, 'embedded_userdata_infos')
                    and any(ud.instance_id == instance_id for ud in target_context.embedded_userdata_infos)
                )

            if (not instance_exists
                and hasattr(target_context, 'embedded_userdata_infos')
                and target_context.embedded_userdata_infos):
                for ud_info in target_context.embedded_userdata_infos:
                    if hasattr(ud_info, 'embedded_instances') and instance_id in ud_info.embedded_instances:
                        instance_exists = True
                        target_context = ud_info
                        break

            if not instance_exists:
                print(f"[WARNING] {ref_type} instance {instance_id} not found in context. Removing reference only.")
                del array_data.values[element_index]
                self._mark_parent_chain_modified(target_context)
                return True

            #is_referenced_elsewhere = self._check_embedded_instance_referenced_elsewhere(
            #    instance_id, array_data, element_index, ref_type, target_context
            #)
            #if is_referenced_elsewhere:
            #    print(f"[DEBUG] Instance {instance_id} is referenced elsewhere => just remove array element reference.")
            #    del array_data.values[element_index]
            #    self._mark_parent_chain_modified(target_context)
            #    return True

            del array_data.values[element_index]

            if ref_type == "userdata":
                success = self._delete_embedded_userdata(instance_id, target_context)
                if success:
                    self._mark_parent_chain_modified(target_context)
                else:
                    print(f"[ERROR] Failed to delete UserData {instance_id}")
                return success
            else:
                success = self._delete_embedded_instance(instance_id, ref_type, target_context)
                if success:
                    self._mark_parent_chain_modified(target_context)
                return success
        else:
            del array_data.values[element_index]
            self._mark_parent_chain_modified(fallback_rui)
            return True

    def _delete_embedded_userdata(self, userdata_id, rui):
        if not hasattr(rui, 'embedded_userdata_infos'):
            return False
        
        target_ud = None
        target_index = -1
        for i, ud_info in enumerate(rui.embedded_userdata_infos):
            if ud_info.instance_id == userdata_id:
                target_ud = ud_info
                target_index = i
                break
        
        if not target_ud:
            return False

        try:
            print(f"[DEBUG] Deleting embedded UserData {userdata_id}")
            
            self._delete_nested_userdata_structures(target_ud)
            
            if target_index >= 0 and target_index < len(rui.embedded_userdata_infos):
                rui.embedded_userdata_infos.pop(target_index)

            if hasattr(rui, '_rsz_userdata_dict'):
                rui._rsz_userdata_dict.pop(userdata_id, None)
            if hasattr(rui, '_rsz_userdata_set'):
                rui._rsz_userdata_set.discard(userdata_id)
            if hasattr(rui, '_rsz_userdata_str_map') and target_ud in rui._rsz_userdata_str_map:
                rui._rsz_userdata_str_map.pop(target_ud, None)

            self._cleanup_references_to_userdata(userdata_id, rui)

            # Special handling for instance_count if userData's instance_id < len(rui.embedded_instance_infos).
            # We do NOT unify this logic because of the special "None" usage.
            if hasattr(rui, 'embedded_rsz_header'):
                if hasattr(rui, 'embedded_userdata_infos'):
                    rui.embedded_rsz_header.userdata_count = len(rui.embedded_userdata_infos)
                if hasattr(rui, 'embedded_instance_infos') and userdata_id < len(rui.embedded_instance_infos):
                    rui.embedded_instance_infos[userdata_id] = None
                    rui.embedded_rsz_header.instance_count = len(
                        [x for x in rui.embedded_instance_infos if x is not None]
                    )

                # Just update object_count and userData again normally:
                if hasattr(rui, 'embedded_object_table'):
                    rui.embedded_rsz_header.object_count = len(rui.embedded_object_table)

            if hasattr(rui, 'id_manager') and hasattr(rui.id_manager, 'unregister_instance'):
                rui.id_manager.unregister_instance(userdata_id)
            
            self._validate_userdata_removal(userdata_id, rui)

            if hasattr(rui, 'embedded_userdata_infos'):
                rui.embedded_userdata_infos = [ud_info for ud_info in rui.embedded_userdata_infos 
                                              if ud_info.instance_id > 0]

            rui.modified = True
            return True
        except Exception as e:
            print(f"[ERROR] delete_embedded_userdata: {e}")
            traceback.print_exc()
            return False

    def _delete_nested_userdata_structures(self, userdata_info):
        if hasattr(userdata_info, 'embedded_userdata_infos'):
            nested_userdata = list(userdata_info.embedded_userdata_infos)
            for nested_ud in nested_userdata:
                self._delete_nested_userdata_structures(nested_ud)
                
            userdata_info.embedded_userdata_infos.clear()
        
        if hasattr(userdata_info, 'embedded_instances'):
            for inst_id, fields in userdata_info.embedded_instances.items():
                if isinstance(fields, dict):
                    for field_name, field_data in fields.items():
                        if isinstance(field_data, ArrayData):
                            if hasattr(field_data, '_owning_context'):
                                delattr(field_data, '_owning_context')
                            if hasattr(field_data, '_container_context'):
                                delattr(field_data, '_container_context')
                            if hasattr(field_data, 'values'):
                                field_data.values.clear()
            
            userdata_info.embedded_instances.clear()
        
        if hasattr(userdata_info, 'embedded_instance_infos'):
            userdata_info.embedded_instance_infos.clear()
        if hasattr(userdata_info, 'embedded_object_table'):
            userdata_info.embedded_object_table.clear()
        if hasattr(userdata_info, '_rsz_userdata_dict'):
            userdata_info._rsz_userdata_dict.clear()
        if hasattr(userdata_info, '_rsz_userdata_set'):
            userdata_info._rsz_userdata_set.clear()
        if hasattr(userdata_info, '_rsz_userdata_str_map'):
            userdata_info._rsz_userdata_str_map.clear()
        if hasattr(userdata_info, 'parsed_elements'):
            userdata_info.parsed_elements.clear()
        
        if hasattr(userdata_info, 'parent_userdata_rui'):
            userdata_info.parent_userdata_rui = None

    def _cleanup_references_to_userdata(self, userdata_id, rui):
        if hasattr(rui, 'embedded_instances'):
            if userdata_id in rui.embedded_instances:
                del rui.embedded_instances[userdata_id]
                
            for instance_id, fields in rui.embedded_instances.items():
                if isinstance(fields, dict):
                    for field_name, field_data in fields.items():
                        if isinstance(field_data, UserDataData) and field_data.value == userdata_id:
                            field_data.value = 0  
                            
                        elif isinstance(field_data, ArrayData) and hasattr(field_data, 'values'):
                            for element in field_data.values:
                                if isinstance(element, UserDataData) and element.value == userdata_id:
                                    element.value = 0  

        if hasattr(rui, 'embedded_instance_hierarchy'):
            if userdata_id in rui.embedded_instance_hierarchy:
                rui.embedded_instance_hierarchy.pop(userdata_id, None)
            
            for parent_id, data in rui.embedded_instance_hierarchy.items():
                if 'children' in data and userdata_id in data['children']:
                    data['children'].remove(userdata_id)

        if hasattr(rui, '_rsz_userdata_dict') and userdata_id in rui._rsz_userdata_dict:
            del rui._rsz_userdata_dict[userdata_id]
        
        if hasattr(rui, '_rsz_userdata_set') and userdata_id in rui._rsz_userdata_set:
            rui._rsz_userdata_set.discard(userdata_id)

    def _validate_userdata_removal(self, userdata_id, rui):
        validation_errors = []
        
        if hasattr(rui, 'embedded_userdata_infos'):
            for ud_info in rui.embedded_userdata_infos:
                if ud_info.instance_id == userdata_id:
                    validation_errors.append(f"UserData ID {userdata_id} still exists in embedded_userdata_infos")
        
        if hasattr(rui, '_rsz_userdata_dict') and userdata_id in rui._rsz_userdata_dict:
            validation_errors.append(f"UserData ID {userdata_id} still exists in _rsz_userdata_dict")
    
        if hasattr(rui, '_rsz_userdata_set') and userdata_id in rui._rsz_userdata_set:
            validation_errors.append(f"UserData ID {userdata_id} still exists in _rsz_userdata_set")
        
        if validation_errors:
            print("[WARNING] UserData removal validation failed:")
            for error in validation_errors:
                print(f"  - {error}")
        else:
            print(f"[DEBUG] UserData ID {userdata_id} successfully removed from all collections")

    def _delete_embedded_instance(self, instance_id, ref_type, rui):
        if not hasattr(rui, 'embedded_instances') or instance_id not in rui.embedded_instances:
            return False

        nested = self._collect_embedded_nested_objects(instance_id, rui)
        nested.add(instance_id)

        try:
            print(f"[DEBUG] Deleting {ref_type} instance={instance_id}, plus {len(nested)-1} nested objects")
            for nid in sorted(nested):
                print(f"   -> child {nid}")
            max_id = max(rui.embedded_instances.keys()) + 1 if rui.embedded_instances else 0
            id_map = {}
            for i in range(max_id):
                if i in nested:
                    id_map[i] = -1
                else:
                    offset = sum(1 for d in nested if d < i)
                    if offset > 0:
                        id_map[i] = i - offset
                    else:
                        id_map[i] = i

            self._update_embedded_references(nested, id_map, rui)

            for d in nested:
                if d in rui.embedded_instances:
                    del rui.embedded_instances[d]
                if hasattr(rui, 'parsed_elements') and d in rui.parsed_elements:
                    del rui.parsed_elements[d]

            updated = {}
            for old_i, fields in rui.embedded_instances.items():
                new_i = id_map.get(old_i, old_i)
                if new_i >= 0:
                    updated[new_i] = fields
            rui.embedded_instances = updated

            if hasattr(rui, 'embedded_instance_infos'):
                new_count = max_id - len(nested)
                new_infos = [None]*new_count
                for i, info in enumerate(rui.embedded_instance_infos):
                    if i not in nested and i < len(rui.embedded_instance_infos):
                        new_i = id_map[i]
                        if 0 <= new_i < new_count:
                            new_infos[new_i] = info
                rui.embedded_instance_infos = new_infos

            if hasattr(rui, 'embedded_object_table'):
                new_obj_table = []
                for ref_id in rui.embedded_object_table:
                    if ref_id in nested:
                        new_obj_table.append(0)
                    elif ref_id in id_map:
                        mapped = id_map[ref_id]
                        new_obj_table.append(mapped if mapped>=0 and mapped<(max_id-len(nested)) else 0)
                    else:
                        new_obj_table.append(ref_id)
                rui.embedded_object_table = new_obj_table

            if hasattr(rui, 'id_manager') and isinstance(rui.id_manager, EmbeddedIdManager):
                rui.id_manager.reset()
                for ex_id in rui.embedded_instances.keys():
                    rui.id_manager.register_instance(ex_id)

            if hasattr(rui, 'parsed_elements'):
                updated_pe = {}
                for old_id, val in rui.parsed_elements.items():
                    if old_id not in nested:
                        new_id = id_map.get(old_id, old_id)
                        if new_id>=0:
                            updated_pe[new_id] = val
                rui.parsed_elements = updated_pe

            # The repeated "if hasattr(...)" blocks are replaced with one call:
            _update_rsz_header_counts(rui)

            if hasattr(rui, 'embedded_userdata_infos'):
                rui.embedded_userdata_infos = [ud for ud in rui.embedded_userdata_infos 
                                              if ud.instance_id > 0 and ud.instance_id not in nested]
                
                if hasattr(rui, '_rsz_userdata_dict'):
                    for deleted_id in nested:
                        if deleted_id in rui._rsz_userdata_dict:
                            del rui._rsz_userdata_dict[deleted_id]
                
                if hasattr(rui, '_rsz_userdata_set'):
                    rui._rsz_userdata_set = rui._rsz_userdata_set - nested

            self._validate_embedded_references(rui)
            rui.modified = True
            return True
        except Exception as e:
            print(f"[ERROR] _delete_embedded_instance: {str(e)}")
            traceback.print_exc()
            return False

    def _update_embedded_references(self, deleted_ids, id_adjustments, rui):
        if not hasattr(rui, 'embedded_instances'):
            return
        for instance_id, fields in list(rui.embedded_instances.items()):
            if instance_id in deleted_ids or not isinstance(fields, dict):
                continue
            for field_name, field_data in fields.items():
                if isinstance(field_data, ObjectData) or isinstance(field_data, UserDataData):
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
                elif isinstance(field_data, ArrayData) and hasattr(field_data, 'values'):
                    updated_values = []
                    for element in field_data.values:
                        if isinstance(element, ObjectData) or isinstance(element, UserDataData):
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
                        updated_values.append(element)
                    field_data.values = updated_values
                    
        if hasattr(rui, 'embedded_userdata_infos'):
            new_userdata_infos = []
            for userdata_info in rui.embedded_userdata_infos:
                if userdata_info.instance_id in deleted_ids:
                    if hasattr(rui, '_rsz_userdata_str_map') and userdata_info in rui._rsz_userdata_str_map:
                        del rui._rsz_userdata_str_map[userdata_info]
                    continue
                elif userdata_info.instance_id in id_adjustments:
                    new_id = id_adjustments[userdata_info.instance_id]
                    if new_id >= 0:
                        userdata_info.instance_id = new_id
                        new_userdata_infos.append(userdata_info)
                else:
                    new_userdata_infos.append(userdata_info)
            
            rui.embedded_userdata_infos = new_userdata_infos
            
            if hasattr(rui, '_rsz_userdata_dict'):
                deleted_keys = [k for k in rui._rsz_userdata_dict if k in deleted_ids]
                for k in deleted_keys:
                    del rui._rsz_userdata_dict[k]
            
            if hasattr(rui, '_rsz_userdata_set'):
                rui._rsz_userdata_set = rui._rsz_userdata_set - deleted_ids

    def _validate_embedded_references(self, rui):
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
                elif isinstance(field_data, UserDataData):
                    ref_id = field_data.value
                    if ref_id > 0:
                        is_valid = False
                        if hasattr(rui, 'embedded_userdata_infos'):
                            is_valid = any(userdata.instance_id == ref_id 
                                          for userdata in rui.embedded_userdata_infos)
                        if not is_valid:
                            print(f"Warning: Invalid UserData reference {ref_id} in field {field_name} (instance {instance_id})")
                            field_data.value = 0
                elif isinstance(field_data, ArrayData) and hasattr(field_data, 'values'):
                    for i, element in enumerate(field_data.values):
                        if isinstance(element, ObjectData):
                            ref_id = element.value
                            if ref_id > 0 and ref_id >= instance_count:
                                print(f"Warning: Invalid reference {ref_id} in array {field_name}[{i}] (instance {instance_id})")
                                element.value = 0
                        elif isinstance(element, UserDataData):
                            ref_id = element.value
                            if ref_id > 0:
                                is_valid = False
                                if hasattr(rui, 'embedded_userdata_infos'):
                                    is_valid = any(userdata.instance_id == ref_id 
                                                  for userdata in rui.embedded_userdata_infos)
                                if not is_valid:
                                    print(f"Warning: Invalid UserData reference {ref_id} in array {field_name}[{i}] (instance {instance_id})")
                                    element.value = 0

    def _check_embedded_instance_referenced_elsewhere(self, instance_id, current_array, current_index, ref_type, rui):
        if not hasattr(rui, 'embedded_instances') or not rui.embedded_instances:
            return False
        reference_count = 0
        for i, item in enumerate(current_array.values):
            if i == current_index:
                continue
            if ref_type == "object" and isinstance(item, ObjectData) and item.value == instance_id:
                reference_count += 1
            elif ref_type == "userdata" and isinstance(item, UserDataData) and item.value == instance_id:
                reference_count += 1
        for inst_id, fields in rui.embedded_instances.items():
            if not isinstance(fields, dict):
                continue
            for fname, fdata in fields.items():
                if fdata is current_array:
                    continue
                if (isinstance(fdata, ObjectData) or isinstance(fdata, UserDataData)) and fdata.value == instance_id:
                    reference_count += 1
                elif isinstance(fdata, ArrayData):
                    for elem in fdata.values:
                        if ref_type == "object" and isinstance(elem, ObjectData) and elem.value == instance_id:
                            reference_count += 1
                        elif ref_type == "userdata" and isinstance(elem, UserDataData) and elem.value == instance_id:
                            reference_count += 1
        return (reference_count > 0)
    
    def _collect_embedded_nested_objects(self, root_instance_id, rui):
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
                elif isinstance(field_data, UserDataData) and field_data.value > 0:
                    index_id = field_data.value
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
                        elif isinstance(element, UserDataData) and element.value > 0:
                            index_id = element.value
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
        try:
            if hasattr(rui, 'modified'):
                rui.modified = True
            if hasattr(rui, 'parent_userdata_rui') and rui.parent_userdata_rui:
                if hasattr(rui.parent_userdata_rui, 'modified'):
                    rui.parent_userdata_rui.modified = True
                if hasattr(rui.parent_userdata_rui, 'parent_userdata_rui') and rui.parent_userdata_rui.parent_userdata_rui:
                    g = rui.parent_userdata_rui.parent_userdata_rui
                    if hasattr(g, 'modified'):
                        g.modified = True
            self.viewer.mark_modified()
        except Exception as ex:
            print(f"[WARNING] Error in _mark_parent_chain_modified: {ex}")
   
    def create_array_element(self, element_type, array_data, top_rui, direct_update=False, array_item=None):
        parent_context = getattr(array_data, '_owning_context', None)
        parent_instance_id = getattr(array_data, '_owning_instance_id', None)

        if not parent_context or parent_instance_id is None:
            parent_context, parent_instance_id = self._find_deep_owner_of_array(top_rui, array_data)
            print("parent_context", parent_context, "parent_instance_id", parent_instance_id)
            if not parent_context or parent_instance_id is None:
                print(f"[ERROR] No parent found for array {array_data.orig_type}.")
                return None

        print(f"[DEBUG] Creating new element '{element_type}' in context={parent_context.instance_id}, parent_instance={parent_instance_id}")

        element_class = getattr(array_data, 'element_class', None)
        if not element_class:
            QMessageBox.warning(self.viewer, "Error", "Cannot create array element: missing element_class.")
            return None

        type_info, type_id = self.viewer.type_registry.find_type_by_name(element_type)
        if not type_info:
            QMessageBox.warning(self.viewer, "Error", f"Type not found: {element_type}")
            return None

        original_values = list(array_data.values)
        try:
            if element_class == ObjectData:
                new_elem = self._create_new_embedded_object_instance_for_array(
                    type_id, type_info, element_type, array_data, parent_context, parent_instance_id
                )
            elif element_class == UserDataData:
                new_elem = self._create_new_embedded_userdata_for_array(
                    type_id, type_info, element_type, array_data, parent_context, parent_instance_id
                )
            else:
                new_elem = self.viewer._create_default_field(element_class, array_data.orig_type)
                if new_elem:
                    new_elem._container_array = array_data
                    new_elem._container_context = parent_context
                    new_elem._container_parent_id = parent_instance_id

            if not new_elem:
                return None

            array_data.values.append(new_elem)
            self._mark_parent_chain_modified(parent_context)

            if direct_update and array_item and hasattr(self.viewer.tree, 'model'):
                self._add_element_to_ui_direct(array_item, new_elem)

            print(f"[DEBUG] Successfully created new {element_type}, array size now {len(array_data.values)}")
            QMessageBox.information(self.viewer, "Element Added", f"New {element_type} created.")
            return new_elem
        except Exception as e:
            print(f"[ERROR] create_array_element: {e}")
            traceback.print_exc()
            array_data.values = original_values
            return None

    def _find_deep_owner_of_array(self, rui, array_data):
        if not hasattr(rui, 'embedded_instances'):
            return (None, None)

        for inst_id, fields in rui.embedded_instances.items():
            if self._contains_array(fields, array_data):
                return (rui, inst_id)

        if hasattr(rui, 'embedded_userdata_infos'):
            for child_rui in rui.embedded_userdata_infos:
                found_rui, found_inst_id = self._find_deep_owner_of_array(child_rui, array_data)
                if found_rui and (found_inst_id is not None):
                    return (found_rui, found_inst_id)

        return (None, None)

    def _contains_array(self, obj, target_array):
        if obj is target_array:
            return True
        if isinstance(obj, dict):
            for v in obj.values():
                if self._contains_array(v, target_array):
                    return True
        elif isinstance(obj, ArrayData):
            if obj is target_array:
                return True
            for elem in obj.values:
                if self._contains_array(elem, target_array):
                    return True
        return False
        
    def _create_new_embedded_userdata_for_array(self, type_id, type_info, element_type, array_data, rui, parent_id):
        if not hasattr(rui, 'embedded_userdata_infos'):
            print("Error: RSZUserDataInfo doesn't have embedded_userdata_infos")
            return None
        
        original_userdata_infos = getattr(rui, 'embedded_userdata_infos', [])[:]
        original_instances = {}
        if hasattr(rui, 'embedded_instances'):
            for k, v in rui.embedded_instances.items():
                original_instances[k] = v
        
        try:
            parent_id = self._find_parent_id_for_array(array_data, rui)
            if parent_id is None:
                print("Could not determine parent ID for array")
                return None
                
            print(f"Found parent ID: {parent_id} for array")
            
            next_id = parent_id
            
            id_mapping = {}
            for old_id in sorted(rui.embedded_instances.keys()):
                if old_id >= parent_id:
                    id_mapping[old_id] = old_id + 1
            
            print(f"Shifting IDs: {id_mapping}")
            
            self._update_embedded_references_for_shift(id_mapping, rui)
            
            parent_id += 1
            print(f"Parent ID shifted to: {parent_id}")
            
            new_embedded_instances = {}
            for old_id, fields in rui.embedded_instances.items():
                new_id = id_mapping.get(old_id, old_id)
                new_embedded_instances[new_id] = fields
            rui.embedded_instances = new_embedded_instances
            
            if hasattr(rui, 'embedded_instance_infos'):
                max_id = max(id_mapping.values()) if id_mapping else parent_id
                while len(rui.embedded_instance_infos) <= max_id:
                    dummy_info = self._create_embedded_instance_info(0)
                    rui.embedded_instance_infos.append(dummy_info)
                
                for old_id, new_id in sorted(id_mapping.items(), reverse=True):
                    if old_id < len(rui.embedded_instance_infos):
                        rui.embedded_instance_infos[new_id] = rui.embedded_instance_infos[old_id]
            
            if hasattr(rui, 'embedded_object_table') and rui.embedded_object_table:
                for i in range(len(rui.embedded_object_table)):
                    ref_id = rui.embedded_object_table[i]
                    if ref_id in id_mapping:
                        rui.embedded_object_table[i] = id_mapping[ref_id]
            
            if hasattr(rui, 'embedded_userdata_infos'):
                for userdata_info in rui.embedded_userdata_infos:
                    if userdata_info.instance_id in id_mapping:
                        userdata_info.instance_id = id_mapping[userdata_info.instance_id]
            
            if hasattr(rui, 'embedded_instance_hierarchy'):
                new_hierarchy = {}
                for old_id, data in rui.embedded_instance_hierarchy.items():
                    new_id = id_mapping.get(old_id, old_id)
                    new_children = []
                    for child_id in data["children"]:
                        new_children.append(id_mapping.get(child_id, child_id))
                    
                    parent = data["parent"]
                    if parent in id_mapping:
                        parent = id_mapping[parent]
                        
                    new_hierarchy[new_id] = {"children": new_children, "parent": parent}
                rui.embedded_instance_hierarchy = new_hierarchy
            
            print(f"Creating new embedded UserData with ID {next_id} of type {element_type}")
            print(f"Parent ID: {parent_id} (Child ID {next_id} < Parent ID)")
            
            class EmbeddedUserDataInfo:
                def __init__(self):
                    self.instance_id = 0
                    self.name_offset = 0
                    self.data_offset = 0
                    self.data_size = 0
                    self.type_id = 0
                    self.name = ""
                    self.modified = False
                    self.value = ""
                    self.hash = 0
            
            userdata_info = EmbeddedUserDataInfo()
            userdata_info.instance_id = next_id
            userdata_info.type_id = type_id
            userdata_info.name = element_type
            userdata_info.value = element_type
            
            userdata_info._container_array = array_data
            userdata_info._container_parent_id = parent_id
            userdata_info._container_context = rui
            
            userdata_info.parent_userdata_rui = rui
            
            userdata_info.data = b""
            userdata_info.data_size = 0
            
            if hasattr(rui, 'embedded_rsz_header'):
                userdata_info.embedded_rsz_header = type(rui.embedded_rsz_header)()
                for attr in dir(rui.embedded_rsz_header):
                    if not attr.startswith('_') and not callable(getattr(rui.embedded_rsz_header, attr)):
                        setattr(userdata_info.embedded_rsz_header, attr, getattr(rui.embedded_rsz_header, attr))
                
                userdata_info.embedded_rsz_header.object_count = 1
                userdata_info.embedded_rsz_header.instance_count = 2
                userdata_info.embedded_rsz_header.userdata_count = 0
                
                userdata_info.embedded_instances = {}
                userdata_info.embedded_instance_infos = []
                userdata_info.embedded_userdata_infos = []
                userdata_info.embedded_object_table = [1]
                userdata_info.parsed_elements = {}
                userdata_info.id_manager = EmbeddedIdManager(next_id)
                
                userdata_info._rsz_userdata_dict = {}
                userdata_info._rsz_userdata_set = set()
                userdata_info._rsz_userdata_str_map = {}
                userdata_info.embedded_instance_hierarchy = {1: {"children": [], "parent": None}}
                
                userdata_info.modified = False
                
                def mark_modified_func():
                    userdata_info.modified = True
                    if hasattr(userdata_info, 'parent_userdata_rui') and userdata_info.parent_userdata_rui:
                        parent = userdata_info.parent_userdata_rui
                        if hasattr(parent, 'modified'):
                            parent.modified = True
                        if hasattr(parent, 'parent_userdata_rui') and parent.parent_userdata_rui:
                            top_parent = parent.parent_userdata_rui
                            if hasattr(top_parent, 'modified'):
                                top_parent.modified = True
                    self.viewer.mark_modified()
                
                userdata_info.mark_modified = mark_modified_func
            
            if hasattr(rui, '_rsz_userdata_str_map'):
                rui._rsz_userdata_str_map[userdata_info] = element_type
            
            if hasattr(userdata_info, 'embedded_instances'):
                fields = {}
                self.viewer._initialize_fields_from_type_info(fields, type_info)
                
                for field_name, field_data in fields.items():
                    if isinstance(field_data, ArrayData) and field_data.values:
                        field_data._container_path = [next_id]
                        field_data._owning_instance_id = 1
                        field_data._owning_field = field_name
                        field_data._owning_context = userdata_info
                        
                        for idx, element in enumerate(field_data.values):
                            if isinstance(element, (ObjectData, UserDataData)):
                                element._container_array = field_data
                                element._container_index = idx
                                element._container_field = field_name
                                element._container_instance = 1
                    elif isinstance(field_data, (ObjectData, UserDataData)):
                        field_data._container_field = field_name
                        field_data._container_instance = 1
                        field_data._container_context = userdata_info
                null_instance_info = self._create_embedded_instance_info(0)
                root_instance_info = self._create_embedded_instance_info(type_id)
                if self.type_registry:
                    type_info = self.type_registry.get_type_info(type_id)
                    if type_info and "crc" in type_info:
                        root_instance_info.crc = int(type_info["crc"], 16)
                userdata_info.embedded_instance_infos = [null_instance_info, root_instance_info]
                userdata_info.embedded_instances[1] = fields
                userdata_info.id_manager.register_instance(1)
            
            if hasattr(rui, 'embedded_instance_infos'):
                instance_info = self._create_embedded_instance_info(type_id)
                while len(rui.embedded_instance_infos) <= next_id:
                    dummy_info = self._create_embedded_instance_info(0)
                    rui.embedded_instance_infos.append(dummy_info)
                rui.embedded_instance_infos[next_id] = instance_info
            
            rui.embedded_userdata_infos.append(userdata_info)
            
            if hasattr(rui, '_rsz_userdata_dict'):
                rui._rsz_userdata_dict[next_id] = userdata_info
            if hasattr(rui, '_rsz_userdata_set'):
                rui._rsz_userdata_set.add(next_id)
            
            empty_fields = {}
            rui.embedded_instances[next_id] = empty_fields
            
            if not hasattr(rui, 'embedded_instance_hierarchy'):
                rui.embedded_instance_hierarchy = {}
            
            if next_id not in rui.embedded_instance_hierarchy:
                rui.embedded_instance_hierarchy[next_id] = {"children": [], "parent": parent_id}
            
            if parent_id not in rui.embedded_instance_hierarchy:
                rui.embedded_instance_hierarchy[parent_id] = {"children": [next_id], "parent": None}
            else:
                if next_id not in rui.embedded_instance_hierarchy[parent_id]["children"]:
                    rui.embedded_instance_hierarchy[parent_id]["children"].append(next_id)
            
            if hasattr(rui, 'embedded_rsz_header'):
                # Instead of repeating each count manually:
                _update_rsz_header_counts(rui)

            if hasattr(rui, 'id_manager'):
                rui.id_manager.register_instance(next_id)
            
            userdata = UserDataData(element_type, next_id, element_type)
            userdata._container_context = rui
            userdata._container_array = array_data
            userdata._container_parent_id = parent_id
            userdata._owning_userdata = userdata_info
            
            print(f"Created UserDataData with index {userdata.value} of type {element_type}")
            print(f"Child ID {next_id} is lower than parent ID {parent_id}")
            
            if hasattr(rui, 'mark_modified'):
                rui.mark_modified()
            
            return userdata
        except Exception as e:
            print(f"ERROR creating embedded UserData: {str(e)}")
            import traceback
            traceback.print_exc()
            if hasattr(rui, 'embedded_userdata_infos'):
                rui.embedded_userdata_infos = original_userdata_infos
            if hasattr(rui, 'embedded_instances'):
                rui.embedded_instances = original_instances
            return None

    def _find_parent_id_for_array(self, array_data, rui):
        print(f"[DEBUG] Finding parent ID for array with {len(array_data.values) if hasattr(array_data, 'values') else 0} elements")
        
        if hasattr(array_data, '_owning_instance_id') and hasattr(array_data, '_owning_context'):
            if array_data._owning_context is rui:
                print(f"[DEBUG] Using stored owning instance ID: {array_data._owning_instance_id}")
                return array_data._owning_instance_id
        
        if hasattr(array_data, '_container_instance') and hasattr(array_data, '_container_context'):
            if array_data._container_context is rui:
                print(f"[DEBUG] Using stored container instance ID: {array_data._container_instance}")
                return array_data._container_instance
        
        array_id = getattr(array_data, '_array_id', id(array_data))
        if not hasattr(array_data, '_array_id'):
            array_data._array_id = array_id
        print(f"[DEBUG] Array identifier: {array_id}")
        
        def search_container(container, instance_id, path=""):
            if container is array_data:
                print(f"[DEBUG] Found array at path: {path} in instance {instance_id}")
                return True, instance_id, path
            if isinstance(container, dict):
                for field_name, field_value in container.items():
                    found, found_id, subpath = search_container(
                        field_value, instance_id, f"{path}.{field_name}" if path else field_name
                    )
                    if found:
                        return True, found_id, subpath
            elif isinstance(container, ArrayData) and hasattr(container, 'values'):
                for i, element in enumerate(container.values):
                    found, found_id, subpath = search_container(
                        element, instance_id, f"{path}[{i}]" if path else f"[{i}]"
                    )
                    if found:
                        return True, found_id, subpath
                if hasattr(container, '_array_id') and container._array_id == array_id:
                    print(f"[DEBUG] Found array by ID at path: {path} in instance {instance_id}")
                    return True, instance_id, path
            return False, None, path
        
        if hasattr(rui, 'embedded_instances'):
            for instance_id, fields in rui.embedded_instances.items():
                if isinstance(fields, dict):
                    found, found_id, path = search_container(fields, instance_id)
                    if found:
                        print(f"[DEBUG] Array belongs to instance {found_id}, path {path}")
                        return found_id
        
        if hasattr(rui, 'embedded_userdata_infos'):
            for userdata_info in rui.embedded_userdata_infos:
                if hasattr(userdata_info, 'embedded_instances'):
                    if not hasattr(userdata_info, 'parent_userdata_rui'):
                        userdata_info.parent_userdata_rui = rui
                    parent_id = self._find_parent_id_for_array(array_data, userdata_info)
                    if parent_id is not None:
                        print(f"[DEBUG] Found array in nested UserData {userdata_info.instance_id}, parent ID {parent_id}")
                        return parent_id
        print("[DEBUG] Could not find parent ID for array")
        return None

    def _shift_embedded_instances(self, insertion_index, rui):
        print(f"Shifting embedded instances to make space at index {insertion_index}")
        
        id_mapping = {}
        for old_id in sorted(rui.embedded_instances.keys(), reverse=True):
            if old_id >= insertion_index:
                id_mapping[old_id] = old_id + 1
        
        print(f"ID mapping for shift: {id_mapping}")
        
        self._update_embedded_references_for_shift(id_mapping, rui)
        
        new_embedded_instances = {}
        for old_id, fields in rui.embedded_instances.items():
            new_id = id_mapping.get(old_id, old_id)
            new_embedded_instances[new_id] = fields
        rui.embedded_instances = new_embedded_instances
        
        if hasattr(rui, 'embedded_object_table') and rui.embedded_object_table:
            for i in range(len(rui.embedded_object_table)):
                ref_id = rui.embedded_object_table[i]
                if ref_id in id_mapping:
                    rui.embedded_object_table[i] = id_mapping[ref_id]
                    print(f"Updated object_table[{i}] from {ref_id} to {id_mapping[ref_id]}")
        
        if hasattr(rui, 'embedded_instance_infos'):
            while len(rui.embedded_instance_infos) < max(id_mapping.values()) + 1:
                dummy_info = self._create_embedded_instance_info(0)
                rui.embedded_instance_infos.append(dummy_info)
            for old_id, new_id in sorted(id_mapping.items(), reverse=True):
                if old_id < len(rui.embedded_instance_infos):
                    rui.embedded_instance_infos[new_id] = rui.embedded_instance_infos[old_id]
    
        if hasattr(rui, 'embedded_userdata_infos'):
            for userdata_info in rui.embedded_userdata_infos:
                if userdata_info.instance_id in id_mapping:
                    userdata_info.instance_id = id_mapping[userdata_info.instance_id]
    
    def _update_embedded_references_for_shift(self, id_mapping, rui):
        for instance_id, fields in rui.embedded_instances.items():
            if not isinstance(fields, dict):
                continue
            for field_name, field_data in fields.items():
                if isinstance(field_data, ObjectData) or isinstance(field_data, UserDataData):
                    if field_data.value in id_mapping:
                        field_data.value = id_mapping[field_data.value]
                elif isinstance(field_data, ArrayData) and hasattr(field_data, 'values'):
                    for element in field_data.values:
                        if isinstance(element, ObjectData) or isinstance(element, UserDataData):
                            if element.value in id_mapping:
                                element.value = id_mapping[element.value]
        if hasattr(rui, '_array_registry'):
            for array_id, owner_id in list(rui._array_registry.items()):
                if owner_id in id_mapping:
                    rui._array_registry[array_id] = id_mapping[owner_id]

    def _create_new_embedded_object_instance_for_array(
        self, type_id, type_info, element_type, array_data, rui, parent_id
    ):
        if not hasattr(rui, "embedded_instances"):
            print("[ERROR] rui has no embedded_instances.")
            return None

        class NestedNode:
            def __init__(self, t_id, t_info, type_name, is_userdata=False):
                self.type_id = t_id
                self.type_info = t_info
                self.type_name = type_name
                self.is_userdata = is_userdata
                self.fields = {}   
                self.children = [] 
                self.instance_id = -1
                self.parent_object_field = None   
                self.parent_userdata_field = None 

        def _make_node_from_field(field, field_name):
            if isinstance(field, ObjectData):
                return _make_object_node_from_field(field, field_name)
            elif isinstance(field, UserDataData):
                return _make_userdata_node_from_field(field, field_name)
            return None

        def analyze_fields(node: NestedNode):
            node.fields = {}
            self.viewer._initialize_fields_from_type_info(node.fields, node.type_info)
            for field_name, field_obj in node.fields.items():
                if isinstance(field_obj, ArrayData) and field_obj.values:
                    if field_obj.element_class in (ObjectData, UserDataData):
                        for elem in field_obj.values:
                            child_node = _make_node_from_field(elem, field_name)
                            if child_node:
                                node.children.append(child_node)
                                analyze_fields(child_node)
                elif isinstance(field_obj, ObjectData) or isinstance(field_obj, UserDataData):
                    child_node = _make_node_from_field(field_obj, field_name)
                    if child_node:
                        node.children.append(child_node)
                        analyze_fields(child_node)

        def _make_object_node_from_field(obj_field, field_name):
            sub_type_str = obj_field.orig_type
            if not sub_type_str:
                sub_type_str = f"UnknownObject_{field_name}"
            sub_info, sub_tid = self.type_registry.find_type_by_name(sub_type_str)
            if not sub_info or not sub_tid:
                print(f"[WARN] Cannot find type info for nested object field '{field_name}' => {sub_type_str}")
                return None
            child = NestedNode(sub_tid, sub_info, sub_type_str, is_userdata=False)
            child.parent_object_field = obj_field
            return child

        def _make_userdata_node_from_field(ud_field, field_name):
            sub_type_str = ud_field.orig_type
            if not sub_type_str:
                sub_type_str = _try_infer_userdata_type(ud_field, field_name)
            sub_info, sub_tid = self.type_registry.find_type_by_name(sub_type_str)
            if not sub_info or not sub_tid:
                print(f"[WARN] Cannot find type info for nested user-data field '{field_name}' => {sub_type_str}")
                return None
            child = NestedNode(sub_tid, sub_info, sub_type_str, is_userdata=True)
            child.parent_userdata_field = ud_field
            return child

        def _try_infer_userdata_type(ud_field, field_name):
            if hasattr(ud_field, 'type_name') and ud_field.type_name:
                return ud_field.type_name
            if hasattr(ud_field, 'name') and ud_field.name:
                return ud_field.name
            return f"UnknownUserData_{field_name}"

        root_node = NestedNode(type_id, type_info, element_type, is_userdata=False)
        analyze_fields(root_node)

        all_nodes = []
        def gather_nodes(n: NestedNode):
            for c in n.children:
                gather_nodes(c)
            all_nodes.append(n)

        gather_nodes(root_node)
        count_new = len(all_nodes)
        print(f"[DEBUG] Found {count_new} total node(s) in the new object hierarchy.")
        if count_new == 0:
            print("[WARN] No nested object/userdata nodes discovered, only root.")
            count_new = 1
            all_nodes = [root_node]

        id_mapping = {}
        existing_ids = sorted(rui.embedded_instances.keys())
        for old_i in existing_ids:
            if old_i >= parent_id:
                id_mapping[old_i] = old_i + count_new

        if id_mapping:
            print(f"[DEBUG] Shifting existing IDs >= {parent_id} by {count_new}: {id_mapping}")
            self._update_embedded_references_for_shift(id_mapping, rui)
            new_map = {}
            for old_i, fields in list(rui.embedded_instances.items()):
                new_i = id_mapping.get(old_i, old_i)
                new_map[new_i] = fields
            rui.embedded_instances = new_map
            if hasattr(rui, 'embedded_instance_infos'):
                max_after_shift = max(rui.embedded_instances.keys()) if rui.embedded_instances else 0
                while len(rui.embedded_instance_infos) <= max_after_shift:
                    rui.embedded_instance_infos.append(self._create_embedded_instance_info(0))
                for old_i, new_i in sorted(id_mapping.items(), reverse=True):
                    if old_i < len(rui.embedded_instance_infos):
                        rui.embedded_instance_infos[new_i] = rui.embedded_instance_infos[old_i]
                        rui.embedded_instance_infos[old_i] = None
            if hasattr(rui, 'embedded_object_table'):
                for i, ref_id in enumerate(rui.embedded_object_table):
                    if ref_id in id_mapping:
                        rui.embedded_object_table[i] = id_mapping[ref_id]
            if hasattr(rui, 'embedded_userdata_infos'):
                for ud in rui.embedded_userdata_infos:
                    if ud.instance_id in id_mapping:
                        ud.instance_id = id_mapping[ud.instance_id]
            if hasattr(rui, 'embedded_instance_hierarchy'):
                new_hier = {}
                for old_i, data in rui.embedded_instance_hierarchy.items():
                    new_i = id_mapping.get(old_i, old_i)
                    new_kids = []
                    for c in data["children"]:
                        new_kids.append(id_mapping.get(c, c))
                    new_par = data["parent"]
                    if new_par in id_mapping:
                        new_par = id_mapping[new_par]
                    new_hier[new_i] = {"children": new_kids, "parent": new_par}
                rui.embedded_instance_hierarchy = new_hier

        if not hasattr(rui, 'id_manager') or not rui.id_manager:
            rui.id_manager = EmbeddedIdManager(getattr(rui, 'instance_id', 0))
            for e_id in rui.embedded_instances.keys():
                rui.id_manager.register_instance(e_id)

        curr_id = parent_id
        for node in all_nodes:
            node.instance_id = curr_id
            curr_id += 1

        for node in all_nodes:
            i_id = node.instance_id
            if hasattr(rui, 'embedded_instance_infos'):
                while len(rui.embedded_instance_infos) <= i_id:
                    rui.embedded_instance_infos.append(self._create_embedded_instance_info(0))
            if node.is_userdata:
                class EmbeddedUserDataInfo:
                    def __init__(self):
                        self.instance_id = 0
                        self.name_offset = 0
                        self.data_offset = 0
                        self.data_size = 0
                        self.type_id = 0
                        self.name = ""
                        self.value = ""
                        self.modified = False
                        self.hash = 0
                uinfo = EmbeddedUserDataInfo()
                uinfo.instance_id = i_id
                uinfo.type_id = node.type_id
                uinfo.name = node.type_name
                uinfo.value = node.type_name
                if hasattr(rui, 'embedded_rsz_header'):
                    uinfo.embedded_rsz_header = type(rui.embedded_rsz_header)()
                    for attr in dir(rui.embedded_rsz_header):
                        if not attr.startswith('_') and not callable(getattr(rui.embedded_rsz_header, attr)):
                            setattr(uinfo.embedded_rsz_header, attr, getattr(rui.embedded_rsz_header, attr))
                    uinfo.embedded_rsz_header.object_count = 1
                    uinfo.embedded_rsz_header.instance_count = 2
                    uinfo.embedded_rsz_header.userdata_count = 0
                    uinfo.embedded_instances = {}
                    uinfo.embedded_instance_infos = []
                    uinfo.embedded_object_table = [1]
                    uinfo.embedded_userdata_infos = []
                    uinfo.parsed_elements = {}
                    uinfo.id_manager = EmbeddedIdManager(i_id)
                    uinfo._rsz_userdata_dict = {}
                    uinfo._rsz_userdata_set = set()
                    uinfo._rsz_userdata_str_map = {}
                    uinfo.embedded_instance_hierarchy = {1: {"children": [], "parent": None}}
                if not hasattr(rui, 'embedded_instances'):
                    rui.embedded_instances = {}
                rui.embedded_instances[i_id] = {}
                if not hasattr(rui, 'embedded_userdata_infos'):
                    rui.embedded_userdata_infos = []
                rui.embedded_userdata_infos.append(uinfo)
                if hasattr(rui, 'embedded_instance_infos'):
                    inst_info = self._create_embedded_instance_info(node.type_id)
                    rui.embedded_instance_infos[i_id] = inst_info
                rui.id_manager.register_instance(i_id)
                if hasattr(rui, '_rsz_userdata_dict'):
                    rui._rsz_userdata_dict[i_id] = uinfo
                if hasattr(rui, '_rsz_userdata_set'):
                    rui._rsz_userdata_set.add(i_id)
                if hasattr(uinfo, 'embedded_instances'):
                    uinfo.embedded_instances[1] = node.fields
                    null_ii = self._create_embedded_instance_info(0)
                    main_ii = self._create_embedded_instance_info(node.type_id)
                    if hasattr(uinfo, 'embedded_instance_infos'):
                        uinfo.embedded_instance_infos = [null_ii, main_ii]
                        if self.type_registry:
                            st = self.type_registry.get_type_info(node.type_id)
                            if st and "crc" in st:
                                main_ii.crc = int(st["crc"], 16)
                if node.parent_userdata_field:
                    node.parent_userdata_field.value = i_id
                if node.parent_object_field:
                    node.parent_object_field.value = i_id
            else:
                rui.embedded_instances[i_id] = node.fields
                if hasattr(rui, 'embedded_instance_infos'):
                    inst_info = self._create_embedded_instance_info(node.type_id)
                    rui.embedded_instance_infos[i_id] = inst_info
                rui.id_manager.register_instance(i_id)
                if node.parent_object_field:
                    node.parent_object_field.value = i_id
                if node.parent_userdata_field:
                    node.parent_userdata_field.value = i_id

        # Instead of multiple separate blocks, update all counts at once:
        _update_rsz_header_counts(rui)

        if hasattr(rui, "mark_modified"):
            rui.mark_modified()

        root_iid = all_nodes[-1].instance_id
        print(f"[DEBUG] Created root object ID={root_iid} with {count_new-1} nested child node(s). (Child < parent)")

        new_ref = ObjectData(root_iid, element_type)
        new_ref._container_array = array_data
        new_ref._container_context = rui
        new_ref._container_parent_id = parent_id
        return new_ref

    def _create_embedded_instance_info(self, type_id):
        class EmbeddedInstanceInfo:
            def __init__(self):
                self.type_id = 0
                self.crc = 0
        instance_info = EmbeddedInstanceInfo()
        instance_info.type_id = type_id
        if self.type_registry:
            type_info = self.type_registry.get_type_info(type_id)
            if type_info and "crc" in type_info:
                instance_info.crc = int(type_info["crc"], 16)
        return instance_info

    def _add_element_to_ui_direct(self, array_item, element):
        model = self.viewer.tree.model()
        if not model or not hasattr(array_item, 'raw'):
            return False
        array_data = array_item.raw.get('obj') if isinstance(array_item.raw, dict) else None
        if not array_data or not hasattr(array_data, 'values'):
            return False
        
        if not hasattr(array_data, '_array_id'):
            array_data._array_id = id(array_data)
        
        element_index = len(array_data.values) - 1
        base_embedded_context = None
        if hasattr(element, '_container_context'):
            base_embedded_context = element._container_context
        if not base_embedded_context:
            base_embedded_context = self._get_embedded_context_from_item(array_item)
        if not base_embedded_context:
            print("[ERROR] Could not find embedded context for array item")
            return False
        parent_id = None
        if hasattr(element, '_container_parent_id'):
            parent_id = element._container_parent_id
        if parent_id is None and hasattr(array_item.raw, 'parent_instance_id'):
            parent_id = array_item.raw.get('parent_instance_id')
        if parent_id is None:
            parent_id = self._find_parent_id_for_array(array_data, base_embedded_context)
            if parent_id:
                array_item.raw['parent_instance_id'] = parent_id

        if isinstance(element, ObjectData):
            ref_id = element.value
            embedded_context = base_embedded_context
            if hasattr(element, '_owning_context'):
                embedded_context = element._owning_context
            node_data = self._create_embedded_object_node_data(ref_id, element_index, element, array_item, embedded_context)
        elif isinstance(element, UserDataData):
            ref_id = element.value
            embedded_context = base_embedded_context
            if hasattr(element, '_owning_context'):
                embedded_context = element._owning_context
            if hasattr(element, '_owning_userdata'):
                userdata_info = element._owning_userdata
                embedded_context = userdata_info.parent_userdata_rui
            node_data = self._create_embedded_userdata_node_data(ref_id, element_index, element, array_item, embedded_context)
        else:
            node_data = DataTreeBuilder.create_data_node(
                f"{element_index}: ", "", element.__class__.__name__, element
            )
        model.addChild(array_item, node_data)
        array_index = model.getIndexFromItem(array_item)
        self.viewer.tree.expand(array_index)
        if len(array_item.children) > 0:
            child_index = model.getIndexFromItem(array_item.children[-1])
            self.viewer.tree.expand(child_index)
        return True
    
    def _create_embedded_userdata_node_data(self, ref_id, index, element, array_item, embedded_context):
        type_name = f"UserData[{ref_id}]"
        userdata_rui = None
        if hasattr(embedded_context, 'embedded_userdata_infos'):
            for userdata_info in embedded_context.embedded_userdata_infos:
                if userdata_info.instance_id == ref_id:
                    userdata_rui = userdata_info
                    if hasattr(userdata_info, 'name') and userdata_info.name:
                        type_name = userdata_info.name
                    break
        node_data = DataTreeBuilder.create_data_node(
            f"{index}: ({type_name})",
            "",
            element.__class__.__name__,
            element
        )
        context_chain = self._build_context_chain(embedded_context)
        node_data["embedded"] = True
        node_data["domain_id"] = getattr(embedded_context, 'instance_id', 0)
        node_data["embedded_context"] = embedded_context
        node_data["context_chain"] = context_chain
        if userdata_rui and hasattr(userdata_rui, 'embedded_instances'):
            root_instance_id = None
            if hasattr(userdata_rui, 'embedded_object_table') and userdata_rui.embedded_object_table:
                root_instance_id = userdata_rui.embedded_object_table[0]
            if root_instance_id and root_instance_id in userdata_rui.embedded_instances:
                root_fields = userdata_rui.embedded_instances[root_instance_id]
                if isinstance(root_fields, dict):
                    for field_name, field_data in root_fields.items():
                        if hasattr(field_data, 'set_callback'):
                            original_callback = getattr(field_data, '_callback', None)
                            userdata_chain = [userdata_rui] + context_chain
                            field_data.set_callback(lambda *args, **kwargs: self._track_nested_modifications(
                                field_data, userdata_rui, userdata_chain, original_callback, *args, **kwargs))
                        field_node = self.viewer._create_field_dict(field_name, field_data, userdata_rui)
                        node_data["children"].append(field_node)
        return node_data

    def _create_embedded_object_node_data(self, ref_id, index, element, array_item, embedded_context):
        type_name = f"Object[{ref_id}]"
        if hasattr(embedded_context, 'embedded_instance_infos') and ref_id < len(embedded_context.embedded_instance_infos):
            instance_info = embedded_context.embedded_instance_infos[ref_id]
            if instance_info:
                type_id = instance_info.type_id
                if self.viewer.type_registry:
                    type_info = self.viewer.type_registry.get_type_info(type_id)
                    if type_info and "name" in type_info:
                        type_name = type_info["name"]
        print(f"[DEBUG] Creating node for embedded object {ref_id} of type '{type_name}'")
        context_id = getattr(embedded_context, 'instance_id', 'unknown')
        print(f"[DEBUG] Using embedded context with instance_id={context_id}")
        node_data = DataTreeBuilder.create_data_node(
            f"{index}: ({type_name})",
            "",
            element.__class__.__name__, 
            element
        )
        context_chain = self._build_context_chain(embedded_context)
        print(f"[DEBUG] Context chain length: {len(context_chain)}")
        node_data["embedded"] = True
        node_data["domain_id"] = getattr(embedded_context, 'instance_id', 0)
        node_data["embedded_context"] = embedded_context
        node_data["context_chain"] = context_chain
        node_data["instance_id"] = ref_id
        node_data["children"] = []
        if hasattr(embedded_context, 'embedded_instances'):
            print(f"[DEBUG] Looking for fields for instance {ref_id} in embedded_instances")
            instance_keys = sorted(embedded_context.embedded_instances.keys()) if hasattr(embedded_context, 'embedded_instances') else []
            print(f"[DEBUG] Available instance IDs in context {context_id}: {instance_keys}")
            if ref_id in embedded_context.embedded_instances:
                fields = embedded_context.embedded_instances[ref_id]
                field_count = len(fields) if isinstance(fields, dict) else 0
                print(f"[DEBUG] Found fields for instance {ref_id}: {field_count} items, type={type(fields)}")
                if isinstance(fields, dict):
                    for field_name, field_data in fields.items():
                        if field_name == "embedded_rsz":
                            continue
                        print(f"[DEBUG] Adding field '{field_name}' of type {type(field_data).__name__}")
                        try:
                            if hasattr(field_data, 'set_callback'):
                                original_callback = getattr(field_data, '_callback', None)
                                field_data.set_callback(lambda *args, **kwargs: self._track_nested_modifications(
                                    field_data, embedded_context, context_chain, original_callback, *args, **kwargs))
                            if isinstance(field_data, UserDataData) and field_data.value > 0:
                                userdata_rui = self._find_userdata_info_by_id(field_data.value, embedded_context)
                                if userdata_rui:
                                    field_node = self._create_field_with_userdata_children(
                                        field_name, field_data, userdata_rui, embedded_context, context_chain
                                    )
                                    node_data["children"].append(field_node)
                                    continue
                            field_node = self.viewer._create_field_dict(field_name, field_data, embedded_context)
                            node_data["children"].append(field_node)
                        except Exception as e:
                            print(f"[ERROR] Failed to add field '{field_name}': {str(e)}")
                            traceback.print_exc()
                            error_node = DataTreeBuilder.create_data_node(f"{field_name}: ERROR", str(e))
                            node_data["children"].append(error_node)
                    if not node_data["children"]:
                        node_data["children"].append(DataTreeBuilder.create_data_node("(All fields filtered)", ""))
                else:
                    node_data["children"].append(
                        DataTreeBuilder.create_data_node(f"(Found non-dict fields of type {type(fields).__name__})", "")
                    )
            else:
                print(f"[DEBUG] Instance {ref_id} not found in context {context_id}")
                nested_context = None
                if hasattr(embedded_context, 'embedded_userdata_infos'):
                    for userdata_info in embedded_context.embedded_userdata_infos:
                        if hasattr(userdata_info, 'embedded_instances') and ref_id in userdata_info.embedded_instances:
                            nested_context = userdata_info
                            break
                if nested_context:
                    print(f"[DEBUG] Found instance {ref_id} in nested context {getattr(nested_context, 'instance_id', 'unknown')}")
                    nested_fields = nested_context.embedded_instances[ref_id]
                    if isinstance(nested_fields, dict):
                        for field_name, field_data in nested_fields.items():
                            if field_name == "embedded_rsz": 
                                continue
                            try:
                                field_node = self.viewer._create_field_dict(field_name, field_data, nested_context)
                                node_data["children"].append(field_node)
                            except Exception as e:
                                print(f"[ERROR] Failed to add nested field '{field_name}': {str(e)}")
                    else:
                        node_data["children"].append(
                            DataTreeBuilder.create_data_node("(Found in nested context but not a dict)", "")
                        )
                else:
                    node_data["children"].append(
                        DataTreeBuilder.create_data_node(f"(Instance {ref_id} not found in any context)", "")
                    )
        else:
            print("[DEBUG] embedded_context has no embedded_instances attribute")
            node_data["children"].append(
                DataTreeBuilder.create_data_node("(No embedded_instances found in context)", "")
            )
        return node_data

    def _find_userdata_info_by_id(self, userdata_id, context):
        if hasattr(context, 'embedded_userdata_infos'):
            for userdata_info in context.embedded_userdata_infos:
                if userdata_info.instance_id == userdata_id:
                    return userdata_info
            for userdata_info in context.embedded_userdata_infos:
                if hasattr(userdata_info, 'embedded_userdata_infos'):
                    nested_result = self._find_userdata_info_by_id(userdata_id, userdata_info)
                    if nested_result:
                        return nested_result
        return None

    def _create_field_with_userdata_children(self, field_name, userdata_field, userdata_rui, parent_context, context_chain):
        type_name = f"UserData[{userdata_field.value}]"
        if hasattr(userdata_rui, 'name') and userdata_rui.name:
            type_name = userdata_rui.name
        elif hasattr(userdata_field, 'orig_type') and userdata_field.orig_type:
            type_name = userdata_field.orig_type
        node_data = DataTreeBuilder.create_data_node(f"{field_name}: ({type_name})", "", "UserDataData", userdata_field)
        node_data["children"] = []
        node_data["embedded"] = True
        node_data["domain_id"] = getattr(parent_context, 'instance_id', 0)
        node_data["embedded_context"] = parent_context
        node_data["context_chain"] = context_chain
        root_instance_id = None
        if hasattr(userdata_rui, 'embedded_object_table') and userdata_rui.embedded_object_table:
            root_instance_id = userdata_rui.embedded_object_table[0]
        if root_instance_id and hasattr(userdata_rui, 'embedded_instances') and root_instance_id in userdata_rui.embedded_instances:
            root_fields = userdata_rui.embedded_instances[root_instance_id]
            if isinstance(root_fields, dict):
                userdata_context_chain = [userdata_rui] + context_chain
                for child_field_name, child_field_data in root_fields.items():
                    if hasattr(child_field_data, 'set_callback'):
                        original_callback = getattr(child_field_data, '_callback', None)
                        child_field_data.set_callback(lambda *args, **kwargs: self._track_nested_modifications(
                            child_field_data, userdata_rui, userdata_context_chain, original_callback, *args, **kwargs))
                    try:
                        field_node = self.viewer._create_field_dict(child_field_name, child_field_data, userdata_rui)
                        node_data["children"].append(field_node)
                    except Exception as e:
                        print(f"[ERROR] Failed to add UserData child field '{child_field_name}': {str(e)}")
                        error_node = DataTreeBuilder.create_data_node(f"{child_field_name}: ERROR", str(e))
                        node_data["children"].append(error_node)
                if not node_data["children"]:
                    node_data["children"].append(DataTreeBuilder.create_data_node("(No fields)", ""))
            else:
                node_data["children"].append(
                    DataTreeBuilder.create_data_node(f"(Found non-dict fields of type {type(root_fields).__name__})", "")
                )
        else:
            node_data["children"].append(DataTreeBuilder.create_data_node("(No fields found)", ""))
        return node_data

    def _build_context_chain(self, context):
        chain = [context]
        current = context
        while hasattr(current, 'parent_userdata_rui') and current.parent_userdata_rui:
            chain.append(current.parent_userdata_rui)
            current = current.parent_userdata_rui
        return chain

    def _track_nested_modifications(self, field_obj, direct_context, context_chain, original_callback=None, *args, **kwargs):
        if original_callback:
            try:
                original_callback(*args, **kwargs)
            except Exception as e:
                print(f"[ERROR] Exception in original callback: {e}")
        if hasattr(field_obj, '_forbidden_parent_ids') and hasattr(field_obj, 'value'):
            if field_obj.value in field_obj._forbidden_parent_ids:
                print("[WARNING] Prevented circular reference")
                field_obj.value = 0
        for ctx in context_chain:
            if hasattr(ctx, 'modified'):
                ctx.modified = True
        self.viewer.mark_modified()
