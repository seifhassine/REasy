"""
Specialized operations for arrays in embedded RSZ structures.

This module handles array operations (adding/removing elements) specifically for
embedded RSZ data structures found in SCN.19 files.
"""

import traceback
from file_handlers.rsz.rsz_data_types import ObjectData, UserDataData, ArrayData, is_reference_type, is_array_type
from utils.id_manager import EmbeddedIdManager
from file_handlers.pyside.tree_model import DataTreeBuilder
from file_handlers.rsz.rsz_embedded_utils import (
    update_rsz_header_counts,
    create_embedded_instance_info,
    copy_embedded_rsz_header,
    create_embedded_userdata_info,
    initialize_embedded_rsz_structures,
    mark_parent_chain_modified,
    build_context_chain,
    update_embedded_references_for_shift
)
from file_handlers.rsz.rsz_field_utils import update_references_with_mapping
from file_handlers.rsz.scn_19.scn_19_structure import Scn19RSZUserDataInfo
from file_handlers.pyside.tree_widget_factory import TreeWidgetFactory


class RszEmbeddedArrayOperations:
    
    def __init__(self, viewer):
        self.viewer = viewer
        self.type_registry = viewer.type_registry

    def delete_array_element(self, array_data, element_index, fallback_rui):
        if not array_data or not hasattr(array_data, 'values') or element_index >= len(array_data.values):
            return False
            
        for i in range(element_index + 1, len(array_data.values)):
            element = array_data.values[i]
            if hasattr(element, '_container_array'):
                element._container_index = i - 1
            
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
            target_context = getattr(array_data, '_owning_context', None)
            if not target_context:
                target_context = getattr(element, '_container_context', None)
            if not target_context:
                target_context = fallback_rui

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
                del array_data.values[element_index]
                self._update_array_counters(array_data, target_context)
                mark_parent_chain_modified(target_context, self.viewer)
                return True

            del array_data.values[element_index]
            self._update_array_counters(array_data, target_context)
            
            if ref_type == "userdata":
                success = self._delete_embedded_userdata(instance_id, target_context)
                if success:
                    mark_parent_chain_modified(target_context, self.viewer)
                return success
            else:
                success = self._delete_embedded_instance(instance_id, ref_type, target_context)
                if success:
                    mark_parent_chain_modified(target_context, self.viewer)
                return success
        else:
            del array_data.values[element_index]
            self._update_array_counters(array_data, fallback_rui)
            mark_parent_chain_modified(fallback_rui, self.viewer)
            return True



    def _update_array_counters(self, array_data, rui):
        """Update array counters in embedded context to match current array length"""
        if hasattr(rui, '_array_counters') and array_data:
            array_id = id(array_data)
            if array_id in rui._array_counters:
                current_length = len(array_data.values) if hasattr(array_data, 'values') else 0
                rui._array_counters[array_id] = current_length

    def _delete_embedded_userdata(self, userdata_id, rui):
        target_ud = None
        target_index = -1
        for i, ud_info in enumerate(rui.embedded_userdata_infos):
            if ud_info.instance_id == userdata_id:
                target_ud = ud_info
                target_index = i
                break
        
        if not target_ud:
            return False

        self._delete_nested_userdata_structures(target_ud)
        
        if userdata_id in rui.embedded_instances:
            nested_instances = self._collect_embedded_nested_objects(userdata_id, rui)
            nested_instances.add(userdata_id)
            
            self._cleanup_references_to_deleted_instances(nested_instances, rui)
            
            for inst_id in nested_instances:
                if inst_id in rui.embedded_instances:
                    del rui.embedded_instances[inst_id]
                
                if hasattr(rui, 'embedded_instance_infos') and inst_id < len(rui.embedded_instance_infos):
                    rui.embedded_instance_infos[inst_id] = create_embedded_instance_info(0, self.type_registry)
        
        if target_index >= 0 and target_index < len(rui.embedded_userdata_infos):
            rui.embedded_userdata_infos.pop(target_index)

        if hasattr(rui, '_rsz_userdata_dict'):
            rui._rsz_userdata_dict.pop(userdata_id, None)
        if hasattr(rui, '_rsz_userdata_set'):
            rui._rsz_userdata_set.discard(userdata_id)
        if hasattr(rui, '_rsz_userdata_str_map') and target_ud in rui._rsz_userdata_str_map:
            rui._rsz_userdata_str_map.pop(target_ud, None)

        self._cleanup_references_to_userdata(userdata_id, rui)

        self._shift_embedded_instances_down(userdata_id, rui)

        if hasattr(rui, 'embedded_userdata_infos'):
            rui.embedded_rsz_header.userdata_count = len(rui.embedded_userdata_infos)
        if hasattr(rui, 'embedded_instance_infos'):
            rui.embedded_rsz_header.instance_count = len(
                [x for x in rui.embedded_instance_infos if x is not None]
            )
        rui.embedded_rsz_header.object_count = len(rui.embedded_object_table)
        
        self._validate_userdata_removal(userdata_id, rui)

        rui.modified = True
        return True
        
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
                
            for _, fields in rui.embedded_instances.items():
                if isinstance(fields, dict):
                    for _, field_data in fields.items():
                        if isinstance(field_data, UserDataData) and field_data.value == userdata_id:
                            field_data.value = 0  
                            
                        elif is_array_type(field_data):
                            for element in field_data.values:
                                if isinstance(element, UserDataData) and element.value == userdata_id:
                                    element.value = 0  

        if hasattr(rui, 'embedded_instance_hierarchy'):
            if userdata_id in rui.embedded_instance_hierarchy:
                rui.embedded_instance_hierarchy.pop(userdata_id, None)
            
            for parent_id, data in rui.embedded_instance_hierarchy.items():
                if isinstance(data, dict) and 'children' in data and userdata_id in data['children']:
                    data['children'].remove(userdata_id)
                elif isinstance(data, list) and userdata_id in data:
                    data.remove(userdata_id)

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

    def _delete_embedded_instance(self, instance_id, ref_type, rui):
        """Delete an embedded instance and shift remaining instances down."""
        if not hasattr(rui, 'embedded_instances') or instance_id not in rui.embedded_instances:
            return False

        nested = self._collect_embedded_nested_objects(instance_id, rui)
        nested.add(instance_id)

        self._cleanup_references_to_deleted_instances(nested, rui)
        
        min_deleted_id = min(nested)
        
        for d in nested:
            if d in rui.embedded_instances:
                del rui.embedded_instances[d]
            if hasattr(rui, 'parsed_elements') and d in rui.parsed_elements:
                del rui.parsed_elements[d]
        
        if hasattr(rui, 'embedded_instance_infos'):
            for d in nested:
                if d < len(rui.embedded_instance_infos):
                    rui.embedded_instance_infos[d] = create_embedded_instance_info(0, self.type_registry)
        
        if hasattr(rui, 'embedded_object_table'):
            for i in range(len(rui.embedded_object_table)):
                if rui.embedded_object_table[i] in nested:
                    rui.embedded_object_table[i] = 0
        
        # Update id_manager
        if hasattr(rui, 'id_manager') and isinstance(rui.id_manager, EmbeddedIdManager):
            if hasattr(rui.id_manager, '_instance_to_reasy'):
                for d in nested:
                    if d in rui.id_manager._instance_to_reasy:
                        reasy_id = rui.id_manager._instance_to_reasy[d]
                        del rui.id_manager._instance_to_reasy[d]
                        if reasy_id in rui.id_manager._reasy_to_instance:
                            del rui.id_manager._reasy_to_instance[reasy_id]
        
        if hasattr(rui, 'embedded_instance_hierarchy'):
            for d in nested:
                if d in rui.embedded_instance_hierarchy:
                    del rui.embedded_instance_hierarchy[d]
                    
            for parent_id, children in rui.embedded_instance_hierarchy.items():
                if isinstance(children, list):
                    rui.embedded_instance_hierarchy[parent_id] = [c for c in children if c not in nested]
        
        self._shift_embedded_instances_down(min_deleted_id, rui)

        update_rsz_header_counts(rui)
        
        rui.modified = True
        return True
    
    def _cleanup_references_to_deleted_instances(self, deleted_ids, rui):
        """Clean up all references to instances that are being deleted."""
        if hasattr(rui, 'embedded_instances'):
            for instance_id, fields in rui.embedded_instances.items():
                if instance_id in deleted_ids or not isinstance(fields, dict):
                    continue
                
                update_references_with_mapping(fields, {}, deleted_ids)


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
                
            for field_name, field_data in fields.items():
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
                            pass
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
                                    pass

    def _check_embedded_instance_referenced_elsewhere(self, instance_id, current_array, current_index, ref_type, rui):
        if not hasattr(rui, 'embedded_instances') or not rui.embedded_instances:
            return False
        reference_count = 0
        for i, item in enumerate(current_array.values):
            if i == current_index:
                continue
            if ((ref_type == "object" and isinstance(item, ObjectData) and item.value == instance_id) or 
                (ref_type == "userdata" and isinstance(item, UserDataData) and item.value == instance_id)):
                reference_count += 1
        for inst_id, fields in rui.embedded_instances.items():
            if not isinstance(fields, dict):
                continue
            for fname, fdata in fields.items():
                if fdata is current_array:
                    continue
                if is_reference_type(fdata) and fdata.value == instance_id:
                    reference_count += 1
                elif is_array_type(fdata):
                    for elem in fdata.values:
                        if ((ref_type == "object" and isinstance(elem, ObjectData) and elem.value == instance_id) or 
                            (ref_type == "userdata" and isinstance(elem, UserDataData) and elem.value == instance_id)):
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
                    pass
                elif is_array_type(field_data):
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
                            pass
        
        explore_instance(root_instance_id)
        return nested_objects
    
    def _is_exclusively_referenced_from(self, instance_id, source_id, rui):
        if not hasattr(rui, 'embedded_instances'):
            return True
        
        for check_id, fields in rui.embedded_instances.items():
            if check_id == source_id or not isinstance(fields, dict):
                continue
            for _, field_data in fields.items():
                if isinstance(field_data, ObjectData) and field_data.value == instance_id:
                    return False
                elif is_array_type(field_data):
                    for element in field_data.values:
                        if isinstance(element, ObjectData) and element.value == instance_id:
                            return False
        return True

    def create_array_element(self, element_type, array_data, top_rui, direct_update=False, array_item=None):
        """Create array element with proper instance ordering in embedded contexts."""
        parent_context = getattr(array_data, '_owning_context', None)
        parent_instance_id = getattr(array_data, '_owning_instance_id', None)

        if not parent_context or parent_instance_id is None:
            parent_context, parent_instance_id = self._find_deep_owner_of_array(top_rui, array_data)
            if not parent_context or parent_instance_id is None:
                print(f"[ERROR] No parent found for array {array_data.orig_type}.")
                return None
        
        element_class = getattr(array_data, 'element_class', None)
        if not element_class:
            raise RuntimeError("Cannot create array element: missing element_class.")
            return None
        
        if element_class == UserDataData:
            new_elem = self._create_userdata_element_fixed(element_type, array_data, parent_context)
        elif element_class == ObjectData:
            new_elem = self._create_new_embedded_object_instance_for_array(
                self.type_registry.find_type_by_name(element_type)[1],
                self.type_registry.find_type_by_name(element_type)[0],
                element_type,
                array_data,
                parent_context,
                self._find_parent_id_for_array(array_data, parent_context)
            )
        else:
            new_elem = self.viewer._create_default_field(element_class, array_data.orig_type)
            if new_elem is None:
                return None
            
            new_elem._container_array = array_data
            new_elem._container_index = len(array_data.values)
            if hasattr(array_data, '_owning_context') and array_data._owning_context:
                new_elem._container_context = parent_context
            array_data.values.append(new_elem)
            self._update_array_counters(array_data, parent_context)
            mark_parent_chain_modified(parent_context, self.viewer)
        
        if new_elem and direct_update and array_item and hasattr(self.viewer.tree, 'model'):
            self._add_element_to_ui_direct(array_item, new_elem)
        
        if new_elem:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(self.viewer, "Element Added", f"New {element_type} created.")
        
        return new_elem

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
        
    def _create_new_embedded_userdata_for_array(self, type_id, type_info, element_type, array_data, parent_rui, parent_instance_id):
        """
        Create a new UserData element following the same pattern as ObjectData creation.
        This ensures proper instance ordering in the parent embedded context.
        """
        from file_handlers.rsz.rsz_object_operations import RszObjectOperations
        from file_handlers.rsz.rsz_data_types import UserDataData
        from file_handlers.rsz.scn_19.scn_19_structure import Scn19RSZUserDataInfo
        from utils.id_manager import EmbeddedIdManager
        
        object_ops = RszObjectOperations(self.viewer)
        
        insertion_index = self._calculate_field_order_insertion_index(array_data, parent_rui, parent_instance_id)
        
        # For UserDataData, we need to:
        # 1. Reserve space in the parent context for the UserDataData instance itself
        # 2. Create the UserDataData's own embedded RSZ structure
        
        count_new = 1  
        
        id_shift = {}
        for old_id in sorted(parent_rui.embedded_instances.keys()):
            if old_id >= insertion_index:
                id_shift[old_id] = old_id + count_new
        
        if id_shift:
            new_instances = {}
            for old_id, fields in parent_rui.embedded_instances.items():
                new_id = id_shift.get(old_id, old_id)
                new_instances[new_id] = fields
            parent_rui.embedded_instances = new_instances
            
            if hasattr(parent_rui, 'embedded_instance_infos'):
                max_new_id = max(id_shift.values()) if id_shift else insertion_index
                while len(parent_rui.embedded_instance_infos) <= max_new_id:
                    from file_handlers.rsz.rsz_embedded_utils import create_embedded_instance_info
                    parent_rui.embedded_instance_infos.append(create_embedded_instance_info(0, self.type_registry))
                
                for old_id, new_id in sorted(id_shift.items(), reverse=True):
                    if old_id < len(parent_rui.embedded_instance_infos):
                        parent_rui.embedded_instance_infos[new_id] = parent_rui.embedded_instance_infos[old_id]
                        parent_rui.embedded_instance_infos[old_id] = None
            
            from file_handlers.rsz.rsz_embedded_utils import update_embedded_references_for_shift
            update_embedded_references_for_shift(id_shift, parent_rui)
            
            if hasattr(parent_rui, 'embedded_userdata_infos'):
                for ud in parent_rui.embedded_userdata_infos:
                    if hasattr(ud, 'instance_id') and ud.instance_id in id_shift:
                        ud.instance_id = id_shift[ud.instance_id]
            
            if hasattr(parent_rui, 'embedded_object_table'):
                parent_rui.embedded_object_table = [
                    id_shift.get(x, x) for x in parent_rui.embedded_object_table
                ]
        
        userdata_info = Scn19RSZUserDataInfo()
        userdata_info.instance_id = insertion_index
        userdata_info.type_id = type_id
        userdata_info.crc = int(type_info.get("crc", "0"), 16)
        userdata_info.name = element_type
        userdata_info.value = element_type
        userdata_info.parent_userdata_rui = parent_rui
        userdata_info.data = b""
        userdata_info.data_size = 0
        
        if hasattr(parent_rui, 'embedded_rsz_header'):
            userdata_info.embedded_rsz_header = type(parent_rui.embedded_rsz_header)()
            copy_embedded_rsz_header(parent_rui.embedded_rsz_header, userdata_info.embedded_rsz_header)
            
            userdata_info.embedded_instances = {}
            userdata_info.embedded_instance_infos = []
            userdata_info.embedded_userdata_infos = []
            userdata_info.embedded_object_table = []
            userdata_info.parsed_elements = {}
            

            userdata_info.id_manager = EmbeddedIdManager(insertion_index)
            
            userdata_info._rsz_userdata_dict = {}
            userdata_info._rsz_userdata_set = set()
            userdata_info._rsz_userdata_str_map = {}
            userdata_info.embedded_instance_hierarchy = {}
            userdata_info._array_counters = {}
            userdata_info.modified = False
            
            if type_info:
                object_ops._create_embedded_instance_with_nested_objects(
                    userdata_info, type_info, type_id, element_type
                )
        
        if not hasattr(parent_rui, 'embedded_userdata_infos'):
            parent_rui.embedded_userdata_infos = []
        parent_rui.embedded_userdata_infos.append(userdata_info)
        
        parent_rui.embedded_instances[insertion_index] = {}
        
        if not hasattr(parent_rui, 'embedded_instance_infos'):
            parent_rui.embedded_instance_infos = []
        
        while len(parent_rui.embedded_instance_infos) <= insertion_index:
            from file_handlers.rsz.rsz_embedded_utils import create_embedded_instance_info
            parent_rui.embedded_instance_infos.append(create_embedded_instance_info(0, self.type_registry))
        
        from file_handlers.rsz.rsz_embedded_utils import create_embedded_instance_info
        instance_info = create_embedded_instance_info(type_id, self.type_registry)
        parent_rui.embedded_instance_infos[insertion_index] = instance_info
        
        if hasattr(parent_rui, 'id_manager') and parent_rui.id_manager:
            parent_rui.id_manager.register_instance(insertion_index)
        
        if hasattr(parent_rui, '_rsz_userdata_dict'):
            parent_rui._rsz_userdata_dict[insertion_index] = userdata_info
        if hasattr(parent_rui, '_rsz_userdata_set'):
            parent_rui._rsz_userdata_set.add(insertion_index)
        if hasattr(parent_rui, '_rsz_userdata_str_map'):
            parent_rui._rsz_userdata_str_map[userdata_info] = element_type
        
        if hasattr(parent_rui, 'embedded_rsz_header'):
            parent_rui.embedded_rsz_header.instance_count = len(parent_rui.embedded_instance_infos)
            parent_rui.embedded_rsz_header.userdata_count = len(parent_rui.embedded_userdata_infos)
        
        userdata = UserDataData(insertion_index, "", element_type)
        userdata._container_array = array_data
        userdata._container_context = parent_rui
        userdata._owning_userdata = userdata_info
            
        return userdata

    def _calculate_field_order_insertion_index(self, array_data, rui, parent_id):
        """
        Calculate the correct insertion index for new instances based on field declaration order.
        This ensures that instances are created in the same order as fields are declared in the type definition.
        """
        if not hasattr(array_data, '_owning_field') or not hasattr(array_data, '_owning_instance_id'):
            return parent_id
            
        owning_instance_id = array_data._owning_instance_id
        
        parent_fields = rui.embedded_instances.get(owning_instance_id)
        if not parent_fields:
            return parent_id
            
        parent_instance_info = None
        if hasattr(rui, 'embedded_instance_infos') and owning_instance_id < len(rui.embedded_instance_infos):
            parent_instance_info = rui.embedded_instance_infos[owning_instance_id]
        
        if not parent_instance_info:
            return parent_id
            
        parent_type_info = self.type_registry.get_type_info(parent_instance_info.type_id)
        if not parent_type_info:
            return parent_id
            
        array_field_index = -1
        field_defs = parent_type_info.get("fields", [])
        
        for idx, field_def in enumerate(field_defs):
            field_name = field_def.get("name", "")
            if field_name in parent_fields:
                if parent_fields[field_name] is array_data:
                    array_field_index = idx
                    break
        
        if array_field_index == -1:
            max_id = parent_id 
            for elem in array_data.values:
                if is_reference_type(elem) and elem.value > max_id:
                    max_id = elem.value
            return max_id + 1
        
        max_before_array = parent_id 
        
        for idx in range(array_field_index):
            if idx < len(field_defs):
                field_name = field_defs[idx].get("name", "")
                if field_name in parent_fields:
                    field_obj = parent_fields[field_name]
                    if is_reference_type(field_obj) and field_obj.value > 0:
                        max_before_array = max(max_before_array, field_obj.value)
                    elif isinstance(field_obj, ArrayData):
                        for elem in field_obj.values:
                            if is_reference_type(elem) and elem.value > 0:
                                max_before_array = max(max_before_array, elem.value)
        
        for elem in array_data.values:
            if is_reference_type(elem) and elem.value > 0:
                max_before_array = max(max_before_array, elem.value)
        
        min_after_array = None
        
        for idx in range(array_field_index + 1, len(field_defs)):
            field_name = field_defs[idx].get("name", "")
            if field_name in parent_fields:
                field_obj = parent_fields[field_name]
                if is_reference_type(field_obj) and field_obj.value > 0:
                    if min_after_array is None or field_obj.value < min_after_array:
                        min_after_array = field_obj.value
                elif isinstance(field_obj, ArrayData):
                    for elem in field_obj.values:
                        if is_reference_type(elem) and elem.value > 0:
                            if min_after_array is None or elem.value < min_after_array:
                                min_after_array = elem.value
        
        insertion_index = max_before_array + 1
        
        if min_after_array is not None and insertion_index >= min_after_array:
            insertion_index = min_after_array
       
        return insertion_index
    
    def _find_instances_belonging_to_element(self, element_id, rui, boundary_id):
        """
        Find all instances that belong to a specific element.
        This includes the element itself and any instances created between it and the boundary.
        """
        instances = [element_id]
        
        for inst_id in sorted(rui.embedded_instances.keys()):
            if inst_id > element_id:
                if boundary_id is None or inst_id < boundary_id:
                    is_referenced_elsewhere = False
                    
                    for check_id, check_fields in rui.embedded_instances.items():
                        if check_id == inst_id:
                            continue
                            
                        for field_name, field_obj in check_fields.items():
                            if is_reference_type(field_obj) and field_obj.value == inst_id:
                                is_referenced_elsewhere = True
                                break
                            elif isinstance(field_obj, ArrayData):
                                for elem in field_obj.values:
                                    if is_reference_type(elem) and elem.value == inst_id:
                                        is_referenced_elsewhere = True
                                        break
                        
                        if is_referenced_elsewhere:
                            break
                    
                    if not is_referenced_elsewhere:
                        instances.append(inst_id)
                else:
                    break  # We've reached the boundary
        
        return instances

    def _find_parent_id_for_array(self, array_data, rui):
        
        if hasattr(array_data, '_owning_instance_id') and hasattr(array_data, '_owning_context'):
            if array_data._owning_context is rui:
                return array_data._owning_instance_id
        
        if hasattr(array_data, '_container_instance') and hasattr(array_data, '_container_context'):
            if array_data._container_context is rui:
                return array_data._container_instance
        
        array_id = getattr(array_data, '_array_id', id(array_data))
        if not hasattr(array_data, '_array_id'):
            array_data._array_id = array_id
        
        def search_container(container, instance_id, path=""):
            if container is array_data:
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
                    return True, instance_id, path
            return False, None, path
        
        if hasattr(rui, 'embedded_instances'):
            for instance_id, fields in rui.embedded_instances.items():
                if isinstance(fields, dict):
                    found, found_id, path = search_container(fields, instance_id)
                    if found:
                        return found_id
        
        if hasattr(rui, 'embedded_userdata_infos'):
            for userdata_info in rui.embedded_userdata_infos:
                if hasattr(userdata_info, 'embedded_instances'):
                    if not hasattr(userdata_info, 'parent_userdata_rui'):
                        userdata_info.parent_userdata_rui = rui
                    parent_id = self._find_parent_id_for_array(array_data, userdata_info)
                    if parent_id is not None:
                        return parent_id
        return None

    def _shift_embedded_instances(self, insertion_index, rui, parent_instance_id=None):
        """Shift instances >= insertion_index by 1, following the non-embedded pattern."""
        id_shift = {}
        for old_id in sorted(rui.embedded_instances.keys()):
            if old_id >= insertion_index:
                id_shift[old_id] = old_id + 1
        
        if id_shift:
            new_instances = {}
            for old_id, fields in rui.embedded_instances.items():
                new_id = id_shift.get(old_id, old_id)
                new_instances[new_id] = fields
            rui.embedded_instances = new_instances
            
            if hasattr(rui, 'embedded_instance_infos'):
                max_new_id = max(id_shift.values())
                while len(rui.embedded_instance_infos) <= max_new_id:
                    rui.embedded_instance_infos.append(create_embedded_instance_info(0, self.type_registry))
                
                new_instance_infos = rui.embedded_instance_infos[:]
                
                for old_id in sorted(id_shift.keys()):
                    if old_id < len(new_instance_infos):
                        new_instance_infos[old_id] = create_embedded_instance_info(0, self.type_registry)
                
                for old_id, new_id in id_shift.items():
                    if old_id < len(rui.embedded_instance_infos):
                        new_instance_infos[new_id] = rui.embedded_instance_infos[old_id]
                
                rui.embedded_instance_infos = new_instance_infos
            
            update_embedded_references_for_shift(id_shift, rui)
            
            if hasattr(rui, 'embedded_userdata_infos'):
                for ud_info in rui.embedded_userdata_infos:
                    if hasattr(ud_info, 'instance_id') and ud_info.instance_id in id_shift:
                        ud_info.instance_id = id_shift[ud_info.instance_id]
                
                for ud_info in rui.embedded_userdata_infos:
                    if hasattr(ud_info, 'embedded_instances'):
                        ud_instance_ids = sorted(ud_info.embedded_instances.keys())
                        if ud_instance_ids:
                            min_internal = min(ud_instance_ids)
                            max_internal = max(ud_instance_ids)
                            
                            for inst_id, fields in ud_info.embedded_instances.items():
                                if isinstance(fields, dict):
                                    for field_name, field_data in fields.items():
                                        if hasattr(field_data, 'value') and isinstance(field_data.value, int) and field_data.value > 0:
                                            if field_data.value < min_internal or field_data.value > max_internal:
                                                if field_data.value in id_shift:
                                                    field_data.value = id_shift[field_data.value]
                                        elif isinstance(field_data, ArrayData) and hasattr(field_data, 'values'):
                                            for i, element in enumerate(field_data.values):
                                                if hasattr(element, 'value') and isinstance(element.value, int) and element.value > 0:
                                                    if element.value < min_internal or element.value > max_internal:
                                                        if element.value in id_shift:
                                                            element.value = id_shift[element.value]
            
            if hasattr(rui, 'embedded_object_table'):
                rui.embedded_object_table = [
                    id_shift.get(x, x) for x in rui.embedded_object_table
                ]
            
            if hasattr(rui, 'embedded_instance_hierarchy'):
                new_hierarchy = {}
                for parent_id, children in rui.embedded_instance_hierarchy.items():
                    new_parent = id_shift.get(parent_id, parent_id)
                    new_children = [id_shift.get(c, c) for c in children]
                    new_hierarchy[new_parent] = new_children
                rui.embedded_instance_hierarchy = new_hierarchy
            
            if hasattr(rui, '_array_counters'):
                new_counters = {}
                for instance_id, counter in rui._array_counters.items():
                    new_id = id_shift.get(instance_id, instance_id)
                    new_counters[new_id] = counter
                rui._array_counters = new_counters
            
            if hasattr(rui, 'id_manager') and rui.id_manager:
                if hasattr(rui.id_manager, '_instance_to_reasy'):
                    new_instance_to_reasy = {}
                    new_reasy_to_instance = {}
                    
                    for instance_id, reasy_id in rui.id_manager._instance_to_reasy.items():
                        new_instance_id = id_shift.get(instance_id, instance_id)
                        new_instance_to_reasy[new_instance_id] = reasy_id
                        new_reasy_to_instance[reasy_id] = new_instance_id
                    
                    rui.id_manager._instance_to_reasy = new_instance_to_reasy
                    rui.id_manager._reasy_to_instance = new_reasy_to_instance

    def _create_new_embedded_object_instance_for_array(
        self, type_id, type_info, element_type, array_data, rui, parent_id):
        """
        Build a new object (with all nested children) and splice it into *rui*.
        """

        class _NestedNode:
            __slots__ = ("type_id", "type_info", "type_name", "is_userdata",
                        "fields", "children", "instance_id",
                        "parent_object_field", "parent_userdata_field", "field_order")

            def __init__(self, tid, tinfo, tname, is_userdata=False):
                self.type_id, self.type_info, self.type_name = tid, tinfo, tname
                self.is_userdata = is_userdata
                self.fields: dict = {}
                self.children: list = []
                self.instance_id = -1
                self.parent_object_field = None
                self.parent_userdata_field = None
                self.field_order = [] 

        def _lookup_type(type_str):
            return self.type_registry.find_type_by_name(type_str or "")

        def _node_from_complex_field(field, field_name):
            
            sub_type = field.orig_type
            sub_info, sub_tid = _lookup_type(sub_type)

            if not sub_info:
                return None
            if isinstance(field, ObjectData):
                n = _NestedNode(sub_tid, sub_info, sub_type, is_userdata=False)
                n.parent_object_field = field
                return n

            if isinstance(field, UserDataData):
                n = _NestedNode(sub_tid, sub_info, sub_type, is_userdata=True)
                n.parent_userdata_field = field
                return n

            return None

        def _collect_fields(node: _NestedNode):
            """Recursively walk *node* and fill its .fields / .children."""
            self.viewer._initialize_fields_from_type_info(node.fields, node.type_info)
            
            for fname, fobj in node.fields.items():
                if isinstance(fobj, ArrayData):
                    fobj._owning_context = rui
                    fobj._owning_field = fname
                    fobj._temp_node = node
            
            for field_def in node.type_info.get("fields", []):
                field_name = field_def.get("name", "")
                if not field_name or field_name not in node.fields:
                    continue
                    
                field_type = field_def.get("type", "unknown").lower()
                field_orig_type = field_def.get("original_type", "")
                fobj = node.fields[field_name]
                
                if is_reference_type(fobj) and field_orig_type:
                    node.field_order.append((field_name, field_type, field_orig_type, fobj))
                
                if isinstance(fobj, ArrayData) and fobj.values and \
                fobj.element_class in (ObjectData, UserDataData):
                    for elem in fobj.values:
                        if (child := _node_from_complex_field(elem, field_name)):
                            node.children.append(child)
                            _collect_fields(child)
                elif is_reference_type(fobj):
                    if (child := _node_from_complex_field(fobj, field_name)):
                        node.children.append(child)
                        _collect_fields(child)

        root = _NestedNode(type_id, type_info, element_type, is_userdata=False)
        _collect_fields(root)

        all_nodes: list[_NestedNode] = []
        created_nodes = set()
        
        def _gather_in_field_order(node):
            """Gather nodes respecting field declaration order"""
            if node in created_nodes:
                return
                
            for field_name, field_type, field_orig_type, field_obj in node.field_order:
                if isinstance(field_obj, ObjectData):
                    child_node = None
                    for child in node.children:
                        if (hasattr(child, 'parent_object_field') and 
                            child.parent_object_field == field_obj):
                            child_node = child
                            break
                    
                    if child_node and child_node not in created_nodes:
                        _gather_in_field_order(child_node)
                        
                elif isinstance(field_obj, UserDataData):
                    child_node = None
                    for child in node.children:
                        if (hasattr(child, 'parent_userdata_field') and 
                            child.parent_userdata_field == field_obj):
                            child_node = child
                            break
                    
                    if child_node and child_node not in created_nodes:
                        _gather_in_field_order(child_node)
            
            all_nodes.append(node)
            created_nodes.add(node)
        
        _gather_in_field_order(root)

        if not all_nodes:
            all_nodes = [root]

        insertion_index = self._calculate_field_order_insertion_index(array_data, rui, parent_id)
        
        count_new = len(all_nodes)
        id_shift = {old: old + count_new
                    for old in sorted(rui.embedded_instances)
                    if old >= insertion_index}

        if id_shift:
            update_embedded_references_for_shift(id_shift, rui)

            def _remap_dict(d):
                return {id_shift.get(k, k): v for k, v in d.items()}

            rui.embedded_instances = _remap_dict(rui.embedded_instances)

            for table_name in ("embedded_object_table",
                            "embedded_instance_hierarchy"):
                if hasattr(rui, table_name):
                    t = getattr(rui, table_name)
                    if isinstance(t, list):
                        setattr(rui, table_name,
                                [id_shift.get(x, x) for x in t])
                    elif isinstance(t, dict):
                        setattr(rui, table_name, _remap_dict(t))

            if hasattr(rui, "embedded_userdata_infos"):
                for ud in rui.embedded_userdata_infos:
                    ud.instance_id = id_shift.get(ud.instance_id, ud.instance_id)

            if hasattr(rui, "embedded_instance_infos"):
                infos = rui.embedded_instance_infos
                max_after = max(rui.embedded_instances) if rui.embedded_instances else 0
                while len(infos) <= max_after:
                    infos.append(create_embedded_instance_info(0, self.type_registry))
                for old, new in sorted(id_shift.items(), reverse=True):
                    infos[new] = infos[old]
                    infos[old] = None

        if not getattr(rui, "id_manager", None):
            rui.id_manager = EmbeddedIdManager(getattr(rui, "instance_id", 0))
            for e_id in rui.embedded_instances:
                rui.id_manager.register_instance(e_id)

        for n, new_id in zip(all_nodes, range(insertion_index, insertion_index + count_new)):
            n.instance_id = new_id

        def _register_instance(node: _NestedNode):
            i_id = node.instance_id
            rui.embedded_instances[i_id] = node.fields
            rui.id_manager.register_instance(i_id)
            
            for fname, fobj in node.fields.items():
                if isinstance(fobj, ArrayData) and hasattr(fobj, '_temp_node'):
                    fobj._owning_instance_id = i_id
                    delattr(fobj, '_temp_node') 

            if hasattr(rui, "embedded_instance_infos"):
                while len(rui.embedded_instance_infos) <= i_id:
                    rui.embedded_instance_infos.append(create_embedded_instance_info(0, self.type_registry))
                
                rui.embedded_instance_infos[i_id] = \
                    create_embedded_instance_info(node.type_id, self.type_registry)

            if node.parent_object_field:
                node.parent_object_field.value = i_id
            if node.parent_userdata_field:
                node.parent_userdata_field.value = i_id

        def _register_userdata(node: _NestedNode):
            uinfo = create_embedded_userdata_info(node.instance_id, node.type_id, node.type_name)

            if hasattr(rui, "embedded_rsz_header"):
                initialize_embedded_rsz_structures(uinfo, rui.embedded_rsz_header, node.instance_id)

                main_instance_id = 1
                uinfo.embedded_instances[main_instance_id] = node.fields
                uinfo.embedded_instance_infos = [
                    create_embedded_instance_info(0, self.type_registry),
                    create_embedded_instance_info(node.type_id, self.type_registry)
                ]
                uinfo.embedded_object_table = [main_instance_id]

            rui.embedded_instances[node.instance_id] = {}
            rui.embedded_userdata_infos.append(uinfo)

            if hasattr(rui, "_rsz_userdata_dict"):
                rui._rsz_userdata_dict[node.instance_id] = uinfo
            if hasattr(rui, "_rsz_userdata_set"):
                rui._rsz_userdata_set.add(node.instance_id)
            if hasattr(rui, "_rsz_userdata_str_map"):
                rui._rsz_userdata_str_map[uinfo] = node.type_name

        for node in all_nodes:
            if node.is_userdata:
                _register_userdata(node)
            else:
                _register_instance(node)

        update_rsz_header_counts(rui)
        if hasattr(rui, "mark_modified"):
            rui.mark_modified()

        root_iid = all_nodes[-1].instance_id
        
        new_ref = ObjectData(root_iid, element_type)
        new_ref._container_array = array_data
        new_ref._container_context = rui
        new_ref._container_parent_id = parent_id
        
        array_data.values.append(new_ref)
        
        self._update_array_counters(array_data, rui)
        
        mark_parent_chain_modified(rui, self.viewer)
        
        return new_ref

    def _add_element_to_ui_direct(self, array_item, element, embedded_context=None):
        model = self.viewer.tree.model()
        if not model or not hasattr(array_item, 'raw'):
            return False
        array_data = array_item.raw.get('obj') if isinstance(array_item.raw, dict) else None
        if not array_data or not hasattr(array_data, 'values'):
            return False
        
        if not hasattr(array_data, '_array_id'):
            array_data._array_id = id(array_data)
        
        element_index = len(array_data.values) - 1
        base_embedded_context = embedded_context
        if not base_embedded_context and hasattr(element, '_container_context'):
            base_embedded_context = element._container_context
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
            if hasattr(embedded_context, 'embedded_instances') and ref_id in embedded_context.embedded_instances:
                fields = embedded_context.embedded_instances.get(ref_id)
                if isinstance(fields, dict) and 'embedded_rsz' in fields and fields.get('embedded_rsz') is not None:
                    nested_rui = fields.get('embedded_rsz')
                    node_data = self.viewer._create_direct_embedded_usr_node(f"{element_index}", nested_rui)
                else:
                    node_data = self._create_embedded_object_node_data(ref_id, element_index, element, embedded_context)
            else:
                node_data = self._create_embedded_object_node_data(ref_id, element_index, element, embedded_context)
        elif isinstance(element, UserDataData):
            ref_id = element.value
            embedded_context = base_embedded_context
            if hasattr(element, '_owning_context'):
                embedded_context = element._owning_context
            if hasattr(element, '_owning_userdata'):
                userdata_info = element._owning_userdata
                embedded_context = userdata_info.parent_userdata_rui
            node_data = self._create_embedded_userdata_node_data(ref_id, element_index, element, embedded_context)
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
            if child_index.isValid():
                child_item = child_index.internalPointer()
                if child_item:
                    if not TreeWidgetFactory.should_skip_widget(child_item):
                        name_text = child_item.data[0] if hasattr(child_item, 'data') and child_item.data else ""
                        node_type = child_item.raw.get("type", "") if isinstance(child_item.raw, dict) else ""
                        data_obj = child_item.raw.get("obj", None) if isinstance(child_item.raw, dict) else None
                        widget_container = TreeWidgetFactory.create_widget(
                            node_type, data_obj, name_text, self.viewer.tree,
                            self.viewer.tree.parent_modified_callback if hasattr(self.viewer.tree, 'parent_modified_callback') else None
                        )
                        if widget_container:
                            self.viewer.tree.setIndexWidget(child_index, widget_container)
        return True
    
    def _create_embedded_userdata_node_data(self, ref_id, index, element, embedded_context):
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
        context_chain = build_context_chain(embedded_context)
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
                                field_data, userdata_chain, original_callback, *args, **kwargs))
                        field_node = self.viewer._create_field_dict(field_name, field_data, userdata_rui)
                        node_data["children"].append(field_node)
        return node_data

    def _create_embedded_object_node_data(self, ref_id, index, element, embedded_context):
        type_name = f"Object[{ref_id}]"
        if hasattr(embedded_context, 'embedded_instance_infos') and ref_id < len(embedded_context.embedded_instance_infos):
            instance_info = embedded_context.embedded_instance_infos[ref_id]
            if instance_info:
                type_id = instance_info.type_id
                if self.viewer.type_registry:
                    type_info = self.viewer.type_registry.get_type_info(type_id)
                    if type_info and "name" in type_info:
                        type_name = type_info["name"]
        node_data = DataTreeBuilder.create_data_node(
            f"{index}: ({type_name})",
            "",
            element.__class__.__name__, 
            element
        )
        context_chain = build_context_chain(embedded_context)
        node_data["embedded"] = True
        node_data["domain_id"] = getattr(embedded_context, 'instance_id', 0)
        node_data["embedded_context"] = embedded_context
        node_data["context_chain"] = context_chain
        node_data["instance_id"] = ref_id
        node_data["children"] = []
        if hasattr(embedded_context, 'embedded_instances'):
            if ref_id in embedded_context.embedded_instances:
                fields = embedded_context.embedded_instances[ref_id]
                if isinstance(fields, dict):
                    for field_name, field_data in fields.items():
                        if field_name == "embedded_rsz":
                            continue
                        try:
                            if hasattr(field_data, 'set_callback'):
                                original_callback = getattr(field_data, '_callback', None)
                                field_data.set_callback(lambda *args, **kwargs: self._track_nested_modifications(
                                    field_data, context_chain, original_callback, *args, **kwargs))
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
                nested_context = None
                if hasattr(embedded_context, 'embedded_userdata_infos'):
                    for userdata_info in embedded_context.embedded_userdata_infos:
                        if hasattr(userdata_info, 'embedded_instances') and ref_id in userdata_info.embedded_instances:
                            nested_context = userdata_info
                            break
                if nested_context:
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
                            child_field_data, userdata_context_chain, original_callback, *args, **kwargs))
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

    def _track_nested_modifications(self, field_obj, context_chain, original_callback=None, *args, **kwargs):
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

    def _create_userdata_element_fixed(self, element_type, array_data, parent_rui):
        """Create UserDataData element with proper ordering."""
        type_info, type_id = self.type_registry.find_type_by_name(element_type)
        if not type_info:
            return None
        
        parent_instance_id = self._find_parent_id_for_array(array_data, parent_rui)
        if parent_instance_id is None:
            parent_context, parent_instance_id = self._find_deep_owner_of_array(parent_rui, array_data)
            if parent_instance_id is None:
                return None
            parent_rui = parent_context
        
        if hasattr(array_data, '_owning_instance_id') and array_data._owning_instance_id != parent_instance_id:
            array_data._owning_instance_id = parent_instance_id
        if hasattr(array_data, '_owning_context') and array_data._owning_context != parent_rui:
            array_data._owning_context = parent_rui
        
        if parent_instance_id in parent_rui.embedded_instances:
            parent_fields = parent_rui.embedded_instances[parent_instance_id]
            for field_name, field_obj in parent_fields.items():
                if field_obj is array_data:
                    array_data._owning_field_name = field_name
                    break
        
        insertion_index = self._calculate_insertion_index(array_data, parent_rui, parent_instance_id)

        self._shift_embedded_instances(insertion_index, parent_rui, parent_instance_id)
        
        userdata_info = Scn19RSZUserDataInfo()
        userdata_info.instance_id = insertion_index
        userdata_info.type_id = type_id
        userdata_info.name = element_type
        userdata_info.value = element_type
        userdata_info.parent_userdata_rui = parent_rui
        userdata_info.data = b""
        userdata_info.data_size = 0
        
        if hasattr(parent_rui, 'embedded_rsz_header'):
            userdata_info.embedded_rsz_header = type(parent_rui.embedded_rsz_header)()
            copy_embedded_rsz_header(parent_rui.embedded_rsz_header, userdata_info.embedded_rsz_header)
            
            userdata_info.embedded_instances = {}
            userdata_info.embedded_instance_infos = []
            userdata_info.embedded_userdata_infos = []
            userdata_info.embedded_object_table = []
            userdata_info.embedded_instance_hierarchy = {}
            userdata_info.id_manager = EmbeddedIdManager(insertion_index)
            
            self._populate_embedded_rsz_fixed(userdata_info, type_info, type_id)
            
            from file_handlers.rsz.scn_19.scn_19_structure import build_embedded_rsz
            userdata_info.data = build_embedded_rsz(userdata_info, self.type_registry)
            userdata_info.data_size = len(userdata_info.data)
        
        parent_rui.embedded_instances[insertion_index] = {}
        
        if not hasattr(parent_rui, 'embedded_instance_infos'):
            parent_rui.embedded_instance_infos = []
        
        while len(parent_rui.embedded_instance_infos) <= insertion_index:
            parent_rui.embedded_instance_infos.append(create_embedded_instance_info(0, self.type_registry))
        
        if insertion_index < len(parent_rui.embedded_instance_infos):
            parent_rui.embedded_instance_infos[insertion_index] = create_embedded_instance_info(type_id, self.type_registry)
        else:
            parent_rui.embedded_instance_infos.append(create_embedded_instance_info(type_id, self.type_registry))
        
        if not hasattr(parent_rui, 'embedded_userdata_infos'):
            parent_rui.embedded_userdata_infos = []
        parent_rui.embedded_userdata_infos.append(userdata_info)
        
        if hasattr(parent_rui, 'id_manager'):
            parent_rui.id_manager.register_instance(insertion_index)
        
        if hasattr(parent_rui, '_rsz_userdata_dict'):
            parent_rui._rsz_userdata_dict[insertion_index] = userdata_info
        if hasattr(parent_rui, '_rsz_userdata_set'):
            parent_rui._rsz_userdata_set.add(insertion_index)
        
        element = UserDataData(insertion_index, "", element_type)
        element._owning_userdata = userdata_info
        
        element._container_context = parent_rui
        
        array_data.values.append(element)
        element._container_array = array_data
        element._container_index = len(array_data.values) - 1 
        
        if hasattr(parent_rui, 'embedded_rsz_header'):
            parent_rui.embedded_rsz_header.instance_count = len(parent_rui.embedded_instance_infos)
            parent_rui.embedded_rsz_header.userdata_count = len(parent_rui.embedded_userdata_infos)
        
        self._verify_and_fix_references(parent_rui, parent_instance_id)
        
        mark_parent_chain_modified(parent_rui, self.viewer)
        
        return element

    def _calculate_insertion_index(self, array_data, parent_rui, parent_instance_id):
        """Calculate where to insert new element based on field order."""
        insertion_point, min_after, array_field_name = self._analyze_instance_layout(parent_rui, parent_instance_id, array_data)
        
        if insertion_point is None:
            # Fallback to simple calculation
            max_id = 0  # Start from 0, not parent_instance_id
            for elem in array_data.values:
                if is_reference_type(elem) and elem.value > max_id:
                    max_id = elem.value
            # If no elements yet, start after any existing instances (but not parent)
            if max_id == 0:
                for inst_id in sorted(parent_rui.embedded_instances.keys()):
                    if inst_id != parent_instance_id and inst_id > max_id:
                        max_id = inst_id
                # Find the first gap or use max_id + 1
                for i in range(1, max_id + 2):
                    if i not in parent_rui.embedded_instances or i == parent_instance_id:
                        if i != parent_instance_id:
                            return i
            return max_id + 1
        
        # If we would collide with instances from later fields, we need to shift them
        if min_after is not None and insertion_point >= min_after:
            return min_after
        
        return insertion_point

    def _populate_embedded_rsz_fixed(self, userdata_info, type_info, type_id):
        """Populate embedded RSZ with main instance LAST (highest ID)."""
        userdata_info.embedded_instance_infos = [create_embedded_instance_info(0, self.type_registry)]
        userdata_info.embedded_instances = {}
        
        main_fields = {}
        self.viewer._initialize_fields_from_type_info(main_fields, type_info)
        
        next_id = 1
        for field_def in type_info.get("fields", []):
            field_name = field_def.get("name", "")
            
            if field_name in main_fields:
                field_obj = main_fields[field_name]
                
                if isinstance(field_obj, ObjectData) and field_def.get("original_type"):
                    nested_type = field_def["original_type"]
                    nested_info, nested_id = self.type_registry.find_type_by_name(nested_type)
                    if nested_info and nested_id:
                        inst_info = create_embedded_instance_info(nested_id, self.type_registry)
                        userdata_info.embedded_instance_infos.append(inst_info)
                        
                        inst_fields = {}
                        self.viewer._initialize_fields_from_type_info(inst_fields, nested_info)
                        userdata_info.embedded_instances[next_id] = inst_fields
                        
                        field_obj.value = next_id
                        
                        if userdata_info.id_manager:
                            userdata_info.id_manager.register_instance(next_id)
                        
                        next_id += 1
        
        main_id = next_id
        main_info = create_embedded_instance_info(type_id, self.type_registry)
        userdata_info.embedded_instance_infos.append(main_info)
        userdata_info.embedded_instances[main_id] = main_fields
        
        if userdata_info.id_manager:
            userdata_info.id_manager.register_instance(main_id)
        
        userdata_info.embedded_object_table = [main_id]
        
        userdata_info.embedded_rsz_header.instance_count = len(userdata_info.embedded_instance_infos)
        userdata_info.embedded_rsz_header.object_count = 1

    def paste_array_element(self, elem_data, array_data, parent_rui):
        """Paste array element - handle UserDataData with full content."""
        element_type = elem_data.get("type")
        
        if element_type == "UserDataData" and elem_data.get("has_full_content"):
            return self._paste_userdata_with_full_content(elem_data, array_data, parent_rui)
        else:
            orig_type = elem_data.get("orig_type", "")
            if orig_type and element_type == "UserDataData":
                element = self._create_userdata_element_fixed(orig_type, array_data, parent_rui)
                if element:
                    element._container_array = array_data
                    element._container_context = parent_rui
                    mark_parent_chain_modified(parent_rui, self.viewer)
                return element
            elif orig_type and element_type == "ObjectData":
                return self._create_new_embedded_object_instance_for_array(
                    self.type_registry.find_type_by_name(orig_type)[1],
                    self.type_registry.find_type_by_name(orig_type)[0],
                    orig_type,
                    array_data,
                    parent_rui,
                    self._find_parent_id_for_array(array_data, parent_rui)
                )
            
            return None

    def _paste_userdata_with_full_content(self, elem_data, array_data, parent_rui):
        """Paste UserDataData with full embedded content."""
        embedded_data = elem_data.get("embedded_data", {})
        
        parent_instance_id = self._find_parent_id_for_array(array_data, parent_rui)
        if parent_instance_id is None:
            return None
        
        if hasattr(array_data, '_owning_instance_id') and array_data._owning_instance_id != parent_instance_id:
            array_data._owning_instance_id = parent_instance_id
        if hasattr(array_data, '_owning_context') and array_data._owning_context != parent_rui:
            array_data._owning_context = parent_rui
        
        if parent_instance_id in parent_rui.embedded_instances:
            parent_fields = parent_rui.embedded_instances[parent_instance_id]
            for field_name, field_obj in parent_fields.items():
                if field_obj is array_data:
                    array_data._owning_field_name = field_name
                    break
        
        insertion_index = self._calculate_insertion_index(array_data, parent_rui, parent_instance_id)
        
        self._shift_embedded_instances(insertion_index, parent_rui, parent_instance_id)
        
        userdata_info = Scn19RSZUserDataInfo()
        userdata_info.instance_id = insertion_index
        userdata_info.type_id = embedded_data["type_id"]
        userdata_info.name = embedded_data["name"]
        userdata_info.value = embedded_data["name"]
        userdata_info.parent_userdata_rui = parent_rui
        userdata_info.data = b""
        userdata_info.data_size = 0
        
        if hasattr(parent_rui, 'embedded_rsz_header'):
            userdata_info.embedded_rsz_header = type(parent_rui.embedded_rsz_header)()
            copy_embedded_rsz_header(parent_rui.embedded_rsz_header, userdata_info.embedded_rsz_header)
            
            userdata_info.embedded_instances = {}
            userdata_info.embedded_instance_infos = []
            userdata_info.embedded_userdata_infos = []
            userdata_info.embedded_object_table = []
            userdata_info.embedded_instance_hierarchy = {}
            userdata_info.id_manager = EmbeddedIdManager(insertion_index)
            
            self._restore_embedded_content(userdata_info, embedded_data)
            
            from file_handlers.rsz.scn_19.scn_19_structure import build_embedded_rsz
            userdata_info.data = build_embedded_rsz(userdata_info, self.type_registry)
            userdata_info.data_size = len(userdata_info.data)
        
        parent_rui.embedded_instances[insertion_index] = {}
        
        if not hasattr(parent_rui, 'embedded_instance_infos'):
            parent_rui.embedded_instance_infos = []
            
        while len(parent_rui.embedded_instance_infos) <= insertion_index:
            parent_rui.embedded_instance_infos.append(create_embedded_instance_info(0, self.type_registry))
        
        if insertion_index < len(parent_rui.embedded_instance_infos):
            parent_rui.embedded_instance_infos[insertion_index] = create_embedded_instance_info(userdata_info.type_id, self.type_registry)
        else:
            parent_rui.embedded_instance_infos.append(create_embedded_instance_info(userdata_info.type_id, self.type_registry))
        
        if not hasattr(parent_rui, 'embedded_userdata_infos'):
            parent_rui.embedded_userdata_infos = []
        parent_rui.embedded_userdata_infos.append(userdata_info)
        
        if hasattr(parent_rui, 'id_manager'):
            parent_rui.id_manager.register_instance(insertion_index)
        
        if hasattr(parent_rui, '_rsz_userdata_dict'):
            parent_rui._rsz_userdata_dict[insertion_index] = userdata_info
        if hasattr(parent_rui, '_rsz_userdata_set'):
            parent_rui._rsz_userdata_set.add(insertion_index)
        
        element = UserDataData(insertion_index, "", elem_data.get("orig_type", ""))
        element._owning_userdata = userdata_info
        
        array_data.values.append(element)
        element._container_array = array_data
        element._container_context = parent_rui
        
        self._verify_and_fix_references(parent_rui, parent_instance_id)
        
        mark_parent_chain_modified(parent_rui, self.viewer)
        
        return element
    
    def _restore_embedded_content(self, userdata_info, embedded_data):
        """Restore embedded content from serialized data."""
        instance_infos = embedded_data.get("instance_infos", [])
        instances = embedded_data.get("instances", {})
        
        userdata_info.embedded_instance_infos = [create_embedded_instance_info(0, self.type_registry)]
        userdata_info.embedded_instances = {}
        
        for info_data in instance_infos:
            rel_id = info_data.get("relative_id", 0)
            type_id = info_data["type_id"]
            
            actual_id = rel_id + 1
            
            while len(userdata_info.embedded_instance_infos) <= actual_id:
                userdata_info.embedded_instance_infos.append(create_embedded_instance_info(0, self.type_registry))
            
            userdata_info.embedded_instance_infos[actual_id] = create_embedded_instance_info(type_id, self.type_registry)
        
        for rel_id_str, fields_data in instances.items():
            rel_id = int(rel_id_str)
            actual_id = rel_id + 1
            fields = {}
            
            for field_name, field_data in fields_data.items():
                from file_handlers.rsz.rsz_array_clipboard import RszArrayClipboard
                field_obj = RszArrayClipboard._deserialize_element(field_data, None, {})
                if field_obj:
                    if isinstance(field_data, dict) and field_data.get("is_relative"):
                        if is_reference_type(field_obj) and hasattr(field_obj, 'value'):
                            field_obj.value = field_obj.value + 1
                    elif isinstance(field_obj, ArrayData) and hasattr(field_obj, 'values'):
                        if isinstance(field_data, dict) and "values" in field_data:
                            for i, elem in enumerate(field_obj.values):
                                elem_data = field_data["values"][i] if i < len(field_data["values"]) else {}
                                if isinstance(elem_data, dict) and elem_data.get("is_relative"):
                                    if is_reference_type(elem) and hasattr(elem, 'value'):
                                        elem.value = elem.value + 1
                    fields[field_name] = field_obj
            
            userdata_info.embedded_instances[actual_id] = fields
            
            if userdata_info.id_manager:
                userdata_info.id_manager.register_instance(actual_id)
        
        object_table = embedded_data.get("object_table", [])
        userdata_info.embedded_object_table = [oid + 1 for oid in object_table]
        
        userdata_info.embedded_rsz_header.instance_count = len(userdata_info.embedded_instance_infos)
        userdata_info.embedded_rsz_header.userdata_count = 0
        userdata_info.embedded_rsz_header.object_count = len(userdata_info.embedded_object_table)

    def _verify_and_fix_references(self, parent_rui, parent_instance_id):
        """Verify and fix any reference inconsistencies in the parent instance."""
        if not hasattr(parent_rui, 'embedded_instances'):
            return
            
        parent_fields = parent_rui.embedded_instances.get(parent_instance_id, {})
        if not parent_fields:
            return
            
        # Check each field for valid references
        for field_name, field_obj in parent_fields.items():
            if is_reference_type(field_obj):
                if field_obj.value > 0:
                    if field_obj.value not in parent_rui.embedded_instances:
                        if isinstance(field_obj, UserDataData):
                            valid_userdata = any(
                                ud.instance_id == field_obj.value 
                                for ud in getattr(parent_rui, 'embedded_userdata_infos', [])
                            )
                            if not valid_userdata:
                                field_obj.value = 0
                        else:
                            field_obj.value = 0
                            
            elif isinstance(field_obj, ArrayData):
                if not hasattr(field_obj, '_owning_instance_id') or field_obj._owning_instance_id != parent_instance_id:
                    field_obj._owning_instance_id = parent_instance_id
                if not hasattr(field_obj, '_owning_context'):
                    field_obj._owning_context = parent_rui
                if not hasattr(field_obj, '_owning_field'):
                    field_obj._owning_field = field_name
                    
                for i, elem in enumerate(field_obj.values):
                    if is_reference_type(elem):
                        if elem.value > 0 and elem.value not in parent_rui.embedded_instances:
                            elem.value = 0

    def _analyze_instance_layout(self, parent_rui, parent_instance_id, array_data):
        """Analyze the current instance layout to understand where array elements should go."""
        parent_instance_info = parent_rui.embedded_instance_infos[parent_instance_id] if parent_instance_id < len(parent_rui.embedded_instance_infos) else None
        if not parent_instance_info:
            print("ERROR: No parent instance info")
            return None, None, None
            
        parent_type_info = self.type_registry.get_type_info(parent_instance_info.type_id)
        if not parent_type_info:
            print("ERROR: No parent type info")
            return None, None, None
            
        instance_to_field = self._map_instances_to_fields(parent_rui, parent_instance_id, parent_type_info)
        
        our_array_field = None
        our_array_field_index = -1
        
        parent_fields = parent_rui.embedded_instances.get(parent_instance_id, {})
        field_defs = parent_type_info.get("fields", [])
        
        for field_idx, field_def in enumerate(field_defs):
            field_name = field_def.get("name", "")
            if field_name in parent_fields:
                field_obj = parent_fields[field_name]
                if field_obj is array_data:
                    our_array_field = field_name
                    our_array_field_index = field_idx
                    break
            else:
                print(f"Field #{field_idx}: {field_name} - not in parent_fields")
        
        if our_array_field_index == -1:
            print("ERROR: Could not find array in parent fields")
            return None, None, None
        
        
        max_before = 0 
        for inst_id, (field_name, field_idx) in sorted(instance_to_field.items()):
            if field_idx <= our_array_field_index and inst_id != parent_instance_id:  # Exclude parent
                max_before = max(max_before, inst_id)
        
        min_after = None
        for inst_id, (field_name, field_idx) in sorted(instance_to_field.items()):
            if field_idx > our_array_field_index:
                if min_after is None or inst_id < min_after:
                    min_after = inst_id
        
        return max_before + 1, min_after, our_array_field

    def _map_instances_to_fields(self, parent_rui, parent_instance_id, parent_type_info):
        instance_to_field = {}
        instance_to_field[parent_instance_id] = ("_parent_", -1)
        
        parent_fields = parent_rui.embedded_instances.get(parent_instance_id, {})
        field_defs = parent_type_info.get("fields", [])
        
        all_instance_ids = sorted(parent_rui.embedded_instances.keys())
        
        last_mapped_id = parent_instance_id
        
        for field_idx, field_def in enumerate(field_defs):
            field_name = field_def.get("name", "")
            if field_name not in parent_fields:
                continue
                
            field_obj = parent_fields[field_name]
            
            if is_reference_type(field_obj) and field_obj.value > 0:
                instance_to_field[field_obj.value] = (field_name, field_idx)
                
                for id in all_instance_ids:
                    if last_mapped_id < id < field_obj.value and id not in instance_to_field:
                        prev_field_idx = field_idx - 1 if field_idx > 0 else -1
                        instance_to_field[id] = (f"implicit_from_field_{prev_field_idx}", prev_field_idx)
                
                last_mapped_id = field_obj.value
                
            elif isinstance(field_obj, ArrayData):
                array_instance_ids = []
                for elem in field_obj.values:
                    if is_reference_type(elem) and elem.value > 0:
                        instance_to_field[elem.value] = (field_name, field_idx)
                        array_instance_ids.append(elem.value)
                
                if array_instance_ids:
                    min_array_id = min(array_instance_ids)
                    max_array_id = max(array_instance_ids)
                    
                    for id in all_instance_ids:
                        if min_array_id <= id <= max_array_id and id not in instance_to_field:
                            instance_to_field[id] = (field_name + "_implicit", field_idx)
                    
                    last_mapped_id = max_array_id
        
        for id in all_instance_ids:
            if id > last_mapped_id and id not in instance_to_field:
                instance_to_field[id] = ("_trailing_", len(field_defs))
        
        return instance_to_field

    def _shift_embedded_instances_down(self, deleted_id, rui):
        """Shift instances > deleted_id down by 1 to fill the gap."""
        id_shift = {}
        for old_id in sorted(rui.embedded_instances.keys()):
            if old_id > deleted_id:
                id_shift[old_id] = old_id - 1
        
        new_instances = {}
        for old_id, fields in rui.embedded_instances.items():
            if old_id == deleted_id:
                continue 
            new_id = id_shift.get(old_id, old_id)
            new_instances[new_id] = fields
        rui.embedded_instances = new_instances
        
        if hasattr(rui, 'embedded_instance_infos') and deleted_id < len(rui.embedded_instance_infos):
            new_instance_infos = []
            for i in range(len(rui.embedded_instance_infos)):
                if i < deleted_id:
                    new_instance_infos.append(rui.embedded_instance_infos[i])
                elif i > deleted_id:
                    new_instance_infos.append(rui.embedded_instance_infos[i])
            
            rui.embedded_instance_infos = new_instance_infos
        
        update_embedded_references_for_shift(id_shift, rui)
        
        if hasattr(rui, 'embedded_userdata_infos'):
            for ud in rui.embedded_userdata_infos:
                if hasattr(ud, 'instance_id') and ud.instance_id in id_shift:
                    ud.instance_id = id_shift[ud.instance_id]
                
                if hasattr(ud, 'embedded_instances'):
                    ud_instance_ids = sorted(ud.embedded_instances.keys())
                    if ud_instance_ids:
                        min_internal = min(ud_instance_ids)
                        max_internal = max(ud_instance_ids)
                        parent_instance_ids = set(rui.embedded_instances.keys())
                        
                        for nested_id, nested_fields in ud.embedded_instances.items():
                            if isinstance(nested_fields, dict):
                                for field_name, field_data in nested_fields.items():
                                    if hasattr(field_data, 'value') and isinstance(field_data.value, int) and field_data.value > 0:
                                        if field_data.value < min_internal or field_data.value > max_internal:
                                            if field_data.value in parent_instance_ids:
                                                if field_data.value in id_shift:
                                                    field_data.value = id_shift[field_data.value]
                                                elif field_data.value == deleted_id:
                                                    field_data.value = 0
                                    elif isinstance(field_data, ArrayData) and hasattr(field_data, 'values'):
                                        for i, element in enumerate(field_data.values):
                                            if hasattr(element, 'value') and isinstance(element.value, int) and element.value > 0:
                                                if element.value < min_internal or element.value > max_internal:
                                                    if element.value in parent_instance_ids:
                                                        if element.value in id_shift:
                                                            element.value = id_shift[element.value]
                                                        elif element.value == deleted_id:
                                                            element.value = 0
        
        if hasattr(rui, 'embedded_object_table'):
            rui.embedded_object_table = [
                id_shift.get(x, x) if x != deleted_id else 0 for x in rui.embedded_object_table
            ]
        
        if hasattr(rui, 'embedded_instance_hierarchy'):
            new_hierarchy = {}
            for parent_id, children in rui.embedded_instance_hierarchy.items():
                if parent_id == deleted_id:
                    continue
                new_parent = id_shift.get(parent_id, parent_id)
                new_children = [id_shift.get(c, c) for c in children if c != deleted_id]
                new_hierarchy[new_parent] = new_children
            rui.embedded_instance_hierarchy = new_hierarchy
        
        if hasattr(rui, 'id_manager') and rui.id_manager:
            if hasattr(rui.id_manager, '_instance_to_reasy'):
                new_instance_to_reasy = {}
                new_reasy_to_instance = {}
                
                for instance_id, reasy_id in rui.id_manager._instance_to_reasy.items():
                    if instance_id == deleted_id:
                        continue 
                    new_instance_id = id_shift.get(instance_id, instance_id)
                    new_instance_to_reasy[new_instance_id] = reasy_id
                    new_reasy_to_instance[reasy_id] = new_instance_id
                
                rui.id_manager._instance_to_reasy = new_instance_to_reasy
                rui.id_manager._reasy_to_instance = new_reasy_to_instance
