import os
import json

import traceback
from abc import ABC, abstractmethod
from typing import Dict, Set, Any, Tuple, List
from file_handlers.rsz.utils.rsz_clipboard_utils import RszClipboardUtils
from file_handlers.rsz.rsz_data_types import (
    ObjectData, UserDataData, ArrayData
)
from file_handlers.rsz.rsz_file import RSZUserDataInfo, RszInstanceInfo
from file_handlers.rsz.utils.rsz_guid_utils import process_gameobject_ref_data


class RszClipboardBase(ABC):
    """Base class for all RSZ clipboard operations with common functionality"""
    
    @abstractmethod
    def get_clipboard_type(self) -> str:
        pass
    
    def get_clipboard_directory(self):
        return RszClipboardUtils.get_type_clipboard_directory(self.get_clipboard_type())
    
    def get_json_name(self, viewer):
        if hasattr(viewer, 'parent') and callable(viewer.parent):
            return RszClipboardUtils.get_json_name(viewer)
        elif hasattr(viewer, 'handler') and hasattr(viewer.handler, 'type_registry') and hasattr(viewer.handler.type_registry, 'json_path'):
            import os
            return os.path.basename(viewer.handler.type_registry.json_path).split(".")[0]
        return None
    
    def get_clipboard_file(self, viewer):
        """Get the clipboard file path for this clipboard type"""
        json_name = self.get_json_name(viewer)
        base_name = os.path.splitext(json_name)[0] if json_name else "default"
        
        return os.path.join(
            self.get_clipboard_directory(),
            f"{base_name}-{self.get_clipboard_type()}-clipboard.json"
        )
    
    def has_clipboard_data(self, viewer):
        """Check if clipboard data exists"""
        clipboard_file = self.get_clipboard_file(viewer)
        return os.path.exists(clipboard_file)
    
    def get_clipboard_data(self, viewer):
        clipboard_file = self.get_clipboard_file(viewer)
        return RszClipboardUtils.load_clipboard_data(clipboard_file)
    
    def save_clipboard_data(self, viewer, data):
        clipboard_file = self.get_clipboard_file(viewer)
        try:
            with open(clipboard_file, 'w') as f:
                json.dump(data, f, indent=2, default=RszClipboardUtils.json_serializer)
            return True
        except Exception as e:
            print(f"Error saving clipboard data: {str(e)}")
            traceback.print_exc()
            return False

    def create_instance_mappings(self) -> Tuple[Dict[int, int], Dict[int, int], Dict[bytes, bytes]]:
        """Create empty mappings for instance IDs, userdata IDs, and GUIDs"""
        instance_mapping = {}
        userdata_mapping = {}
        guid_mapping = {}
        return instance_mapping, userdata_mapping, guid_mapping
    
    def serialize_hierarchy(self, viewer, root_instances: List[int], additional_instances: Set[int] = None, 
                          embedded_context=None) -> Dict[str, Any]:
        """Serialize a hierarchy of instances with their dependencies
        
        Args:
            viewer: The viewer instance
            root_instances: List of root instance IDs to serialize
            additional_instances: Additional instance IDs to include
            embedded_context: Optional embedded context
            
        Returns:
            Dict containing serialized hierarchy
        """
        hierarchy = {
            "instances": {},
            "instance_order": [],
            "userdata_referenced_instances": set() 
        }
        
        all_instances = set(root_instances)
        
        for instance_id in root_instances:
            nested, userdata = self.collect_all_references(viewer, instance_id)
            all_instances.update(nested)
            all_instances.update(userdata)
            hierarchy["userdata_referenced_instances"].update(userdata)
        
        if additional_instances:
            all_instances.update(additional_instances)
        
        ordered_instances = sorted(all_instances)
        
        for instance_id in ordered_instances:
            if instance_id <= 0 or instance_id >= len(viewer.scn.instance_infos):
                continue
                
            instance_data = self.serialize_instance_with_metadata(
                viewer, instance_id, None, embedded_context
            )
            if instance_data:
                if (instance_id in hierarchy["userdata_referenced_instances"] and 
                    not instance_data.get("is_userdata") and 
                    not instance_data.get("is_embedded_rsz_userdata")):
                    instance_data["is_userdata_referenced"] = True
                    print(f"Marking instance {instance_id} as UserDataData-referenced")
                
                hierarchy["instances"][str(instance_id)] = instance_data
                hierarchy["instance_order"].append(instance_id)
        
        hierarchy["userdata_referenced_instances"] = list(hierarchy["userdata_referenced_instances"])
        
        return hierarchy
    
    def paste_instances_from_hierarchy(self, viewer, hierarchy_data: Dict[str, Any], 
                                     instance_mapping: Dict[int, int] = None,
                                     userdata_mapping: Dict[int, int] = None,
                                     guid_mapping: Dict[bytes, bytes] = None,
                                     randomize_guids: bool = True,
                                     shared_userdata_keys: Set[Tuple[int, int]] = None,
                                     global_shared_mapping: Dict[Tuple[int, int], int] = None,
                                     context_id_offset: int = 0) -> List[int]:
        """Paste instances from hierarchy data
        
        Args:
            viewer: The viewer instance
            hierarchy_data: Serialized hierarchy data
            instance_mapping: Existing instance mapping to update
            userdata_mapping: Existing userdata mapping to update
            guid_mapping: Existing GUID mapping to update
            randomize_guids: Whether to randomize GUIDs
            shared_userdata_keys: Set of shared userdata keys
            global_shared_mapping: Global shared userdata mapping
            context_id_offset: Offset to apply to context IDs when randomizing
            
        Returns:
            List of created instance IDs
        """
        if instance_mapping is None:
            instance_mapping = {}
        if userdata_mapping is None:
            userdata_mapping = {}
        if guid_mapping is None:
            guid_mapping = {}
        
        context_id_mapping = {}
        created_instances = []
        instances_data = hierarchy_data.get("instances", {})
        instance_order = hierarchy_data.get("instance_order", [])
        
        if context_id_offset > 0:
            for old_id in instance_order:
                instance_data = instances_data.get(str(old_id))
                if not instance_data:
                    continue
                    
                type_name = instance_data.get("type_name", "")
                if type_name == "chainsaw.ContextID":
                    fields_data = instance_data.get("fields", {})
                    if "_Group" in fields_data:
                        group_data = fields_data.get("_Group", {})
                        if group_data.get("type") == "S32Data":
                            original_value = group_data.get("value", 0)
                            if original_value not in context_id_mapping:
                                context_id_mapping[original_value] = original_value + context_id_offset
        for old_id in instance_order:
            instance_data = instances_data.get(str(old_id))
            if not instance_data:
                continue
                
            embedded_context_id = instance_data.get("embedded_context_id", 0)
            is_shared = (shared_userdata_keys and 
                        (old_id, embedded_context_id) in shared_userdata_keys and
                        (instance_data.get("is_userdata") or instance_data.get("is_embedded_rsz_userdata")))
            
            if is_shared and global_shared_mapping and (old_id, embedded_context_id) in global_shared_mapping:
                existing_id = global_shared_mapping[(old_id, embedded_context_id)]
                instance_mapping[old_id] = existing_id
                userdata_mapping[old_id] = existing_id
                continue
            
            new_id = self.create_instance(viewer, instance_data, instance_mapping, userdata_mapping)
            if new_id:
                created_instances.append(new_id)
                
                if is_shared and global_shared_mapping is not None:
                    global_shared_mapping[(old_id, embedded_context_id)] = new_id
        
        for old_id in instance_order:
            if old_id not in instance_mapping:
                continue
                
            new_id = instance_mapping[old_id]
            instance_data = instances_data.get(str(old_id))
            
            if instance_data.get("is_embedded_rsz_userdata"):
                continue 
                
            fields_data = instance_data.get("fields", {})
            if fields_data:
                type_name = instance_data.get("type_name", "")
                new_fields = self.deserialize_fields_with_remapping(
                    fields_data, instance_mapping, userdata_mapping, 
                    guid_mapping, randomize_guids, viewer,
                    context_id_mapping, context_id_offset, type_name
                )
                
                self.process_embedded_rsz_fields(viewer, new_fields, None, userdata_mapping)
                
                viewer.scn.parsed_elements[new_id] = new_fields
            else:
                if new_id not in viewer.scn.parsed_elements:
                    viewer.scn.parsed_elements[new_id] = {}
        
        if hasattr(viewer.scn, 'has_embedded_rsz') and viewer.scn.has_embedded_rsz:
            self._ensure_userdata_info_for_references(viewer, created_instances, instance_mapping)
        
        if hasattr(self, '_processed_embedded_rsz'):
            del self._processed_embedded_rsz
        
        return created_instances
    
    def _ensure_userdata_info_for_references(self, viewer, created_instances, instance_mapping=None):
        """Ensure all UserDataData-referenced instances have SCN19RSZUserDataInfo
        
        This prevents missing SCN19RSZUserDataInfo for instances that are referenced
        by UserDataData fields but weren't marked as userdata during serialization.
        Also removes duplicate SCN19RSZUserDataInfo entries.
        """
        from file_handlers.rsz.rsz_data_types import UserDataData, ArrayData, ObjectData
        from file_handlers.rsz.scn_19.scn_19_structure import Scn19RSZUserDataInfo
        
        if hasattr(viewer.scn, 'rsz_userdata_infos'):
            seen_instance_ids = set()
            cleaned_infos = []
            for rui in viewer.scn.rsz_userdata_infos:
                if rui.instance_id not in seen_instance_ids:
                    seen_instance_ids.add(rui.instance_id)
                    cleaned_infos.append(rui)
                else:
                    print(f"Removing duplicate SCN19RSZUserDataInfo for instance {rui.instance_id}")
            viewer.scn.rsz_userdata_infos = cleaned_infos
            
            if hasattr(viewer.scn, '_rsz_userdata_dict'):
                new_dict = {}
                for rui in cleaned_infos:
                    new_dict[rui.instance_id] = rui
                viewer.scn._rsz_userdata_dict = new_dict
        
        userdata_referenced_instances = set()
        
        # When importing data blocks, we need to scan ALL instances, not just created ones
        # because ObjectData fields might reference existing instances that have UserDataData fields
        instances_to_scan = set(created_instances)
        
        # Also scan instances referenced by ObjectData fields in created instances
        for instance_id in created_instances:
            if instance_id not in viewer.scn.parsed_elements:
                continue
            fields = viewer.scn.parsed_elements[instance_id]
            for field_name, field_data in fields.items():
                if isinstance(field_data, ObjectData) and field_data.value > 0:
                    instances_to_scan.add(field_data.value)
                elif isinstance(field_data, ArrayData):
                    for element in field_data.values:
                        if isinstance(element, ObjectData) and element.value > 0:
                            instances_to_scan.add(element.value)
        
        #  scan all relevant instances for UserDataData references
        for instance_id in instances_to_scan:
            if instance_id not in viewer.scn.parsed_elements:
                continue
                
            fields = viewer.scn.parsed_elements[instance_id]
            for field_name, field_data in fields.items():
                if isinstance(field_data, UserDataData) and field_data.value > 0:
                    userdata_referenced_instances.add(field_data.value)
                elif isinstance(field_data, ArrayData):
                    for element in field_data.values:
                        if isinstance(element, UserDataData) and element.value > 0:
                            userdata_referenced_instances.add(element.value)
                elif isinstance(field_data, ObjectData) and field_data.value > 0:
                    self._check_nested_userdata_refs(viewer, field_data.value, userdata_referenced_instances, set())
        
        # Ensure each UserDataData-referenced instance has a SCN19RSZUserDataInfo
        for ref_instance_id in userdata_referenced_instances:
            if hasattr(viewer.scn, '_rsz_userdata_set') and ref_instance_id in viewer.scn._rsz_userdata_set:
                count = sum(1 for rui in viewer.scn.rsz_userdata_infos if rui.instance_id == ref_instance_id)
                if count > 1:
                    print(f"Found {count} duplicate SCN19RSZUserDataInfo entries for instance {ref_instance_id}")
                continue  # Already has userdata info
            
            # Check if instance exists
            if ref_instance_id >= len(viewer.scn.instance_infos):
                print(f"Warning: UserDataData references non-existent instance {ref_instance_id}")
                if instance_mapping:
                    for old_id, new_id in instance_mapping.items():
                        if old_id == ref_instance_id:
                            print(f"  Found in instance_mapping: {old_id} -> {new_id}")
                            ref_instance_id = new_id
                            break
                    else:
                        continue
                else:
                    continue
            
            # Create SCN19RSZUserDataInfo for this instance
            print(f"Creating SCN19RSZUserDataInfo for UserDataData-referenced instance {ref_instance_id}")
            
            instance_info = viewer.scn.instance_infos[ref_instance_id]
            
            if not hasattr(viewer.scn, '_rsz_userdata_set'):
                viewer.scn._rsz_userdata_set = set()
            if not hasattr(viewer.scn, '_rsz_userdata_dict'):
                viewer.scn._rsz_userdata_dict = {}
            if not hasattr(viewer.scn, '_rsz_userdata_str_map'):
                viewer.scn._rsz_userdata_str_map = {}
            
            userdata_info = Scn19RSZUserDataInfo()
            userdata_info.instance_id = ref_instance_id
            userdata_info.type_id = instance_info.type_id
            userdata_info.json_path_hash = 0  # Default hash
            userdata_info.data_size = 0
            userdata_info.rsz_offset = 0
            userdata_info.data = b""
            userdata_info.original_data = None
            userdata_info.modified = True
            
            type_name = ""
            if viewer.type_registry:
                type_info = viewer.type_registry.get_type_info(instance_info.type_id)
                if type_info and "name" in type_info:
                    type_name = type_info["name"]
            
            userdata_info.value = type_name
            
            userdata_info.embedded_rsz_header = None
            userdata_info.embedded_object_table = []
            userdata_info.embedded_instance_infos = []
            userdata_info.embedded_userdata_infos = []
            userdata_info.embedded_instances = {}
            

            
            viewer.scn._rsz_userdata_set.add(ref_instance_id)
            viewer.scn._rsz_userdata_dict[ref_instance_id] = userdata_info
            viewer.scn.rsz_userdata_infos.append(userdata_info)
            viewer.scn._rsz_userdata_str_map[userdata_info] = type_name
        
        self._cleanup_orphaned_userdata_infos(viewer)
    
    def _check_nested_userdata_refs(self, viewer, instance_id, userdata_refs, visited):
        """Recursively check nested objects for UserDataData references"""
        from file_handlers.rsz.rsz_data_types import UserDataData, ArrayData, ObjectData
        
        if instance_id in visited or instance_id not in viewer.scn.parsed_elements:
            return
        
        visited.add(instance_id)
        fields = viewer.scn.parsed_elements[instance_id]
        
        for field_name, field_data in fields.items():
            if isinstance(field_data, UserDataData) and field_data.value > 0:
                userdata_refs.add(field_data.value)
            elif isinstance(field_data, ArrayData):
                for element in field_data.values:
                    if isinstance(element, UserDataData) and element.value > 0:
                        userdata_refs.add(element.value)
                    elif isinstance(element, ObjectData) and element.value > 0:
                        self._check_nested_userdata_refs(viewer, element.value, userdata_refs, visited)
            elif isinstance(field_data, ObjectData) and field_data.value > 0:
                self._check_nested_userdata_refs(viewer, field_data.value, userdata_refs, visited)
    
    def _cleanup_orphaned_userdata_infos(self, viewer):
        """Remove orphaned SCN19RSZUserDataInfo entries that aren't referenced by any UserDataData"""
        from file_handlers.rsz.rsz_data_types import UserDataData, ArrayData
        
        if not hasattr(viewer.scn, 'rsz_userdata_infos'):
            return
        
        referenced_instances = set()
        for instance_id, fields in viewer.scn.parsed_elements.items():
            for field_name, field_data in fields.items():
                if isinstance(field_data, UserDataData) and field_data.value > 0:
                    referenced_instances.add(field_data.value)
                elif isinstance(field_data, ArrayData):
                    for element in field_data.values:
                        if isinstance(element, UserDataData) and element.value > 0:
                            referenced_instances.add(element.value)
        
        cleaned_infos = []
        removed_count = 0
        for rui in viewer.scn.rsz_userdata_infos:
            instance_id = rui.instance_id
            
            if instance_id >= len(viewer.scn.instance_infos):
                print(f"Removing SCN19RSZUserDataInfo for non-existent instance {instance_id}")
                removed_count += 1
                continue
            
            if instance_id not in referenced_instances:
                if hasattr(rui, 'embedded_instances') and rui.embedded_instances:
                    cleaned_infos.append(rui)
                else:
                    print(f"Removing orphaned SCN19RSZUserDataInfo for unreferenced instance {instance_id}")
                    removed_count += 1
            else:
                cleaned_infos.append(rui)
        
        if removed_count > 0:
            viewer.scn.rsz_userdata_infos = cleaned_infos
            
            if hasattr(viewer.scn, '_rsz_userdata_set'):
                viewer.scn._rsz_userdata_set = {rui.instance_id for rui in cleaned_infos}
            if hasattr(viewer.scn, '_rsz_userdata_dict'):
                viewer.scn._rsz_userdata_dict = {rui.instance_id: rui for rui in cleaned_infos}
            
    def collect_all_references(self, viewer, instance_id) -> Tuple[Set[int], Set[int]]:
        """Collect all nested objects and userdata references for an instance"""
        from file_handlers.rsz.rsz_instance_operations import RszInstanceOperations
        
        nested_instances = set()
        userdata_refs = set()
        visited_instances = set() 
        
        instances_to_process = [instance_id]
        
        while instances_to_process:
            current_id = instances_to_process.pop(0)
            
            if current_id in visited_instances:
                continue
            visited_instances.add(current_id)
            
            if current_id not in viewer.scn.parsed_elements:
                continue
                
            fields = viewer.scn.parsed_elements[current_id]
            
            object_refs = RszInstanceOperations.find_object_references(fields)
            
            for ref_id in object_refs:
                if ref_id != instance_id and ref_id not in nested_instances:
                    nested_instances.add(ref_id)
                    if ref_id not in visited_instances:
                        instances_to_process.append(ref_id)
            
            current_userdata = set()
            RszInstanceOperations.find_userdata_references(fields, current_userdata)
         
            userdata_refs.update(current_userdata)
        
        return nested_instances, userdata_refs
    
    def serialize_instance_data(self, viewer, instance_id, relative_id_mapping=None) -> Dict[str, Any]:
        """Serialize instance data including type info and fields
        
        Args:
            viewer: The viewer instance
            instance_id: Instance ID to serialize
            relative_id_mapping: Optional mapping for relative IDs (used by array clipboard)
        """
        if instance_id <= 0 or instance_id >= len(viewer.scn.instance_infos):
            return None
            
        instance_info = viewer.scn.instance_infos[instance_id]
        
        mapped_id = relative_id_mapping.get(instance_id, -1) if relative_id_mapping else instance_id
        
        instance_data = {
            "id": mapped_id,
            "type_id": instance_info.type_id,
            "crc": instance_info.crc,
            "fields": {}
        }
        
        type_name = RszClipboardUtils.get_type_info_name(viewer, instance_info.type_id)
        if type_name:
            instance_data["type_name"] = type_name
        
        if instance_id in viewer.scn.parsed_elements:
            fields = viewer.scn.parsed_elements[instance_id]
            from file_handlers.rsz.rsz_array_clipboard import RszArrayClipboard
            from file_handlers.rsz.rsz_data_types import UserDataData
            
            if relative_id_mapping:
                for field_name, field_data in fields.items():
                    if isinstance(field_data, UserDataData) and field_data.value > 0:
                        userdata_rui = RszArrayClipboard._find_userdata_info_by_instance_id(viewer, field_data.value)
                        
                        has_embedded_rsz = False
                        if userdata_rui:
                            if hasattr(userdata_rui, 'embedded_instances') and userdata_rui.embedded_instances:
                                has_embedded_rsz = True
                        
                        if has_embedded_rsz and hasattr(viewer.scn, 'has_embedded_rsz') and viewer.scn.has_embedded_rsz:
                            instance_data["fields"][field_name] = RszArrayClipboard._serialize_userdata_with_graph(
                                field_data, viewer, None
                            )
                            continue
                    elif isinstance(field_data, ArrayData):
                        has_embedded_userdata = False
                        
                        for element in field_data.values:
                            if isinstance(element, UserDataData) and element.value > 0:
                                userdata_rui = RszArrayClipboard._find_userdata_info_by_instance_id(viewer, element.value)
                                if userdata_rui and hasattr(userdata_rui, 'embedded_instances') and userdata_rui.embedded_instances:
                                    has_embedded_userdata = True
                                    break
                        
                        if has_embedded_userdata and hasattr(viewer.scn, 'has_embedded_rsz') and viewer.scn.has_embedded_rsz:
                            array_data = {
                                "type": "ArrayData",
                                "values": [],
                                "orig_type": field_data.orig_type,
                                "element_type": field_data.element_class.__name__ if field_data.element_class else ""
                            }
                            
                            for element in field_data.values:
                                if isinstance(element, UserDataData) and element.value > 0:
                                    userdata_rui = RszArrayClipboard._find_userdata_info_by_instance_id(viewer, element.value)
                                    if userdata_rui and hasattr(userdata_rui, 'embedded_instances') and userdata_rui.embedded_instances:
                                        array_data["values"].append(RszArrayClipboard._serialize_userdata_with_graph(element, viewer, None))
                                    else:
                                        array_data["values"].append(RszArrayClipboard._serialize_field_with_mapping(
                                            element, relative_id_mapping, set(relative_id_mapping.keys()), set()
                                        ))
                                else:
                                    array_data["values"].append(RszArrayClipboard._serialize_field_with_mapping(
                                        element, relative_id_mapping, set(relative_id_mapping.keys()), set()
                                    ))
                            
                            instance_data["fields"][field_name] = array_data
                            continue
                    
                    instance_data["fields"][field_name] = RszArrayClipboard._serialize_field_with_mapping(
                        field_data, relative_id_mapping, set(relative_id_mapping.keys()), set()
                    )
            else:
                for field_name, field_data in fields.items():
                    if isinstance(field_data, UserDataData) and field_data.value > 0:
                        userdata_rui = RszArrayClipboard._find_userdata_info_by_instance_id(viewer, field_data.value)
                        
                        has_embedded_rsz = False
                        if userdata_rui:
                            if hasattr(userdata_rui, 'embedded_instances') and userdata_rui.embedded_instances:
                                has_embedded_rsz = True

                        if has_embedded_rsz and hasattr(viewer.scn, 'has_embedded_rsz') and viewer.scn.has_embedded_rsz:
                            instance_data["fields"][field_name] = RszArrayClipboard._serialize_userdata_with_graph(
                                field_data, viewer, None
                            )
                            continue
                    elif isinstance(field_data, ArrayData):
                        has_embedded_userdata = False
                        
                        for element in field_data.values:
                            if isinstance(element, UserDataData) and element.value > 0:
                                userdata_rui = RszArrayClipboard._find_userdata_info_by_instance_id(viewer, element.value)
                                if userdata_rui and hasattr(userdata_rui, 'embedded_instances') and userdata_rui.embedded_instances:
                                    has_embedded_userdata = True
                                    break
                        
                        if has_embedded_userdata and hasattr(viewer.scn, 'has_embedded_rsz') and viewer.scn.has_embedded_rsz:
                            array_data = {
                                "type": "ArrayData",
                                "values": [],
                                "orig_type": field_data.orig_type,
                                "element_type": field_data.element_class.__name__ if field_data.element_class else ""
                            }
                            
                            for element in field_data.values:
                                if isinstance(element, UserDataData) and element.value > 0:
                                    userdata_rui = RszArrayClipboard._find_userdata_info_by_instance_id(viewer, element.value)
                                    if userdata_rui and hasattr(userdata_rui, 'embedded_instances') and userdata_rui.embedded_instances:
                                        array_data["values"].append(RszArrayClipboard._serialize_userdata_with_graph(element, viewer, None))
                                    else:
                                        array_data["values"].append(RszArrayClipboard._serialize_element(element))
                                else:
                                    array_data["values"].append(RszArrayClipboard._serialize_element(element))
                            
                            instance_data["fields"][field_name] = array_data
                            continue
                    
                    instance_data["fields"][field_name] = RszArrayClipboard._serialize_element(field_data)
        
        userdata_info = RszClipboardUtils.check_userdata_info(viewer, instance_id)
        if userdata_info:
            is_embedded_rsz_userdata = False
            if hasattr(viewer.scn, 'has_embedded_rsz') and viewer.scn.has_embedded_rsz:
                for rui in viewer.scn.rsz_userdata_infos:
                    if (rui.instance_id == instance_id and hasattr(rui, 'embedded_instances') and 
                        rui.embedded_instances):
                        is_embedded_rsz_userdata = True
                        break
            
            if is_embedded_rsz_userdata:
                instance_data["is_embedded_rsz_userdata"] = True
                instance_data["userdata_hash"] = userdata_info["userdata_hash"]
                if userdata_info["userdata_string"] is not None:
                    instance_data["userdata_string"] = userdata_info["userdata_string"]
            else:
                instance_data["is_userdata"] = True
                instance_data["userdata_hash"] = userdata_info["userdata_hash"]
                if userdata_info["userdata_string"] is not None:
                    instance_data["userdata_string"] = userdata_info["userdata_string"]
        
        return instance_data
    
    def create_instance(self, viewer, instance_data, instance_mapping, userdata_mapping):
        """Create a new instance from instance_data and update mappings
        
        Handles regular instances, string-based userdata, and embedded RSZ userdata
        """
        type_id = instance_data.get("type_id", 0)
        crc = instance_data.get("crc", 0)
        
        if type_id <= 0:
            print(f"Warning: Invalid type ID for instance {instance_data.get('id', 'unknown')}")
            return None
        
        insertion_index = len(viewer.scn.instance_infos)
        new_instance = RszInstanceInfo()
        new_instance.type_id = type_id
        new_instance.crc = crc
        
        viewer._insert_instance_and_update_references(insertion_index, new_instance)
        new_instance_id = insertion_index
        viewer.handler.id_manager.register_instance(new_instance_id)
        
        old_instance_id = instance_data.get("id")
        if old_instance_id:
            instance_mapping[old_instance_id] = new_instance_id
        
        if instance_data.get("is_userdata", False):
            self.setup_userdata_for_pasted_instance(
                viewer, 
                new_instance_id, 
                instance_data.get("userdata_hash", 0),
                instance_data.get("userdata_string", "")
            )
            userdata_mapping[old_instance_id] = new_instance_id
            
        elif instance_data.get("is_userdata_referenced", False):
            if hasattr(viewer.scn, 'has_embedded_rsz') and viewer.scn.has_embedded_rsz:
                self.setup_userdata_for_pasted_instance(
                    viewer,
                    new_instance_id,
                    0,  # Default hash
                    ""  # Will be set based on type
                )
                userdata_mapping[old_instance_id] = new_instance_id
            
        elif instance_data.get("is_embedded_rsz_userdata", False):
            userdata_mapping[old_instance_id] = new_instance_id
            
        return new_instance_id
    
    def track_userdata_references(self, viewer, scan_gameobjects=True, scan_folders=True):
        """Track all UserData references across the data block
        
        Returns: (global_userdata_refs, userdata_contexts, visited_instances)
        """
        global_userdata_refs = {}  # (instance_id, context_id) -> reference_count
        userdata_contexts = {}  # (instance_id, context_id) -> context info
        visited_instances = set()  # (instance_id, context_id) tuples
        
        def track_userdata_in_fields(fields, parent_instance_id, embedded_context=None):
            """Track UserData references with their embedded context"""
            context_id = getattr(embedded_context, 'instance_id', 0) if embedded_context else 0
            
            for field_name, field_data in fields.items():
                if isinstance(field_data, UserDataData) and field_data.value > 0:
                    key = (field_data.value, context_id)
                    if key not in global_userdata_refs:
                        global_userdata_refs[key] = 0
                        userdata_contexts[key] = {
                            'context_id': context_id,
                            'context_type': 'embedded' if embedded_context else 'main'
                        }
                    global_userdata_refs[key] += 1
                    
                elif isinstance(field_data, ArrayData):
                    for element in field_data.values:
                        if isinstance(element, UserDataData) and element.value > 0:
                            key = (element.value, context_id)
                            if key not in global_userdata_refs:
                                global_userdata_refs[key] = 0
                                userdata_contexts[key] = {
                                    'context_id': context_id,
                                    'context_type': 'embedded' if embedded_context else 'main'
                                }
                            global_userdata_refs[key] += 1
        
        def scan_instance_tree(instance_id, embedded_context=None):
            """Recursively scan instance tree for UserData references"""
            context_id = getattr(embedded_context, 'instance_id', 0) if embedded_context else 0
            instance_key = (instance_id, context_id)
            
            if instance_key in visited_instances:
                return
            
            visited_instances.add(instance_key)
            
            if instance_id not in viewer.scn.parsed_elements:
                return
                
            fields = viewer.scn.parsed_elements[instance_id]
            track_userdata_in_fields(fields, instance_id, embedded_context)
            
            from file_handlers.rsz.rsz_array_clipboard import RszArrayClipboard
            for field_name, field_data in fields.items():
                if isinstance(field_data, UserDataData) and field_data.value > 0:
                    userdata_rui = RszArrayClipboard._find_userdata_info_by_instance_id(
                        viewer, field_data.value, embedded_context
                    )
                    if (userdata_rui and hasattr(userdata_rui, 'embedded_instances') and 
                        userdata_rui.embedded_instances and hasattr(viewer.scn, 'has_embedded_rsz') and 
                        viewer.scn.has_embedded_rsz):
                        for embedded_inst_id in userdata_rui.embedded_instances:
                            if embedded_inst_id > 0:  # Skip null instance
                                scan_instance_tree(embedded_inst_id, None)
                
                elif isinstance(field_data, ObjectData) and field_data.value > 0:
                    scan_instance_tree(field_data.value, embedded_context)
                    
                elif isinstance(field_data, ArrayData):
                    for element in field_data.values:
                        if isinstance(element, ObjectData) and element.value > 0:
                            scan_instance_tree(element.value, embedded_context)
        
        if scan_gameobjects and hasattr(viewer.scn, 'gameobjects'):
            for go in viewer.scn.gameobjects:
                go_instance_id = self._get_gameobject_instance_id(viewer, go.id)
                if go_instance_id > 0:
                    scan_instance_tree(go_instance_id)
                    
                components = self._get_gameobject_components(viewer, go)
                for comp_id in components:
                    scan_instance_tree(comp_id)
        
        if scan_folders and hasattr(viewer.scn, 'folder_infos'):
            for folder in viewer.scn.folder_infos:
                folder_instance_id = self._get_folder_instance_id(viewer, folder.id)
                if folder_instance_id > 0:
                    scan_instance_tree(folder_instance_id)
        
        return global_userdata_refs, userdata_contexts, visited_instances
    
    def _get_gameobject_instance_id(self, viewer, gameobject_id):
        """Get instance ID for a GameObject"""
        if gameobject_id < 0 or gameobject_id >= len(viewer.scn.object_table):
            return -1
        return viewer.scn.object_table[gameobject_id]
    
    def _get_folder_instance_id(self, viewer, folder_id):
        """Get instance ID for a Folder"""
        if folder_id < 0 or folder_id >= len(viewer.scn.object_table):
            return -1
        return viewer.scn.object_table[folder_id]
    
    def _get_gameobject_components(self, viewer, gameobject):
        """Get component instance IDs for a GameObject"""
        components = []
        for i in range(1, gameobject.component_count + 1):
            comp_object_id = gameobject.id + i
            if comp_object_id < len(viewer.scn.object_table):
                comp_instance_id = viewer.scn.object_table[comp_object_id]
                if comp_instance_id > 0:
                    components.append(comp_instance_id)
        return components
    
    def setup_userdata_for_pasted_instance(self, viewer, instance_id, hash_value, string_value):
        """Create userdata entries for a pasted instance"""
        if not hasattr(viewer.scn, '_rsz_userdata_set'):
            viewer.scn._rsz_userdata_set = set()
        if not hasattr(viewer.scn, '_rsz_userdata_dict'):
            viewer.scn._rsz_userdata_dict = {}
        if not hasattr(viewer.scn, '_rsz_userdata_str_map'):
            viewer.scn._rsz_userdata_str_map = {}

        if instance_id in viewer.scn._rsz_userdata_set:
            print(f"RSZUserDataInfo for instance {instance_id} already exists, skipping duplicate creation")
            return True

        if hasattr(viewer.scn, 'has_embedded_rsz') and viewer.scn.has_embedded_rsz:
            from file_handlers.rsz.scn_19.scn_19_structure import Scn19RSZUserDataInfo
            new_rui = Scn19RSZUserDataInfo()
            new_rui.instance_id = instance_id
            new_rui.type_id = 0 
            new_rui.json_path_hash = hash_value
            new_rui.data_size = 0
            new_rui.rsz_offset = 0
            new_rui.data = b""
            new_rui.value = string_value
            new_rui.modified = True
        else:
            new_rui = RSZUserDataInfo()
            new_rui.instance_id = instance_id
            if 0 <= instance_id < len(viewer.scn.instance_infos):
                new_rui.hash = viewer.scn.instance_infos[instance_id].type_id
            else:
                new_rui.hash = hash_value
            new_rui.string_offset = 0

        viewer.scn._rsz_userdata_set.add(instance_id)
        viewer.scn._rsz_userdata_dict[instance_id] = new_rui
        viewer.scn.rsz_userdata_infos.append(new_rui)
        viewer.scn._rsz_userdata_str_map[new_rui] = string_value
        
        if hasattr(viewer.scn, 'userdata_infos') and hasattr(viewer.scn, '_userdata_str_map'):
            existing_ui = None
            for ui in viewer.scn.userdata_infos:
                if viewer.scn._userdata_str_map.get(ui) == string_value:
                    existing_ui = ui
                    break
            
            if not existing_ui:
                new_ui = RSZUserDataInfo()
                if 0 <= instance_id < len(viewer.scn.instance_infos):
                    new_ui.hash = viewer.scn.instance_infos[instance_id].type_id
                else:
                    new_ui.hash = hash_value
                new_ui.string_offset = 0
                viewer.scn.userdata_infos.append(new_ui)
                viewer.scn._userdata_str_map[new_ui] = string_value

        return True
    
    def deserialize_fields_with_remapping(self, fields_data, instance_mapping, userdata_mapping, guid_mapping, randomize_guids=True, viewer=None, context_id_mapping=None, context_id_offset=0, type_name=""):
        """Deserialize fields with instance/userdata/GUID remapping"""
        new_fields = {}
        
        from file_handlers.rsz.rsz_array_clipboard import RszArrayClipboard
        
        for field_name, field_data in fields_data.items():
            field_type = field_data.get("type", "")
            
            if field_type == "ObjectData":
                value = field_data.get("value", 0)
                orig_type = field_data.get("orig_type", "")
                
                if value in userdata_mapping:
                    new_value = userdata_mapping.get(value)
                    new_fields[field_name] = ObjectData(new_value, orig_type)
                else:
                    new_value = instance_mapping.get(value, value)
                    new_fields[field_name] = ObjectData(new_value, orig_type)
                    
            elif field_type == "UserDataData":
                if "object_graph" in field_data and field_data.get("object_graph", {}).get("context_type") == "embedded_rsz":
                    
                    original_userdata_value = field_data.get("value", 0)
                    
                    pre_allocated_instance_id = userdata_mapping.get(original_userdata_value) if userdata_mapping else None
                    
                    if pre_allocated_instance_id:
                        userdata_element = RszArrayClipboard._create_rsz_userdata_info_for_existing_instance(
                            viewer, field_data, pre_allocated_instance_id
                        )
                    else:
                        userdata_element = RszArrayClipboard._deserialize_complete_embedded_rsz_userdata(
                            viewer, field_data, None  # No embedded context - create standalone
                        )
                    
                    if userdata_element:
                        new_fields[field_name] = userdata_element
                        if original_userdata_value > 0 and not pre_allocated_instance_id:
                            userdata_mapping[original_userdata_value] = userdata_element.value
                    else:
                        raise RuntimeError(f"Failed to process embedded RSZ for field '{field_name}' - this would corrupt the file")
                else:
                    value = field_data.get("value", 0)
                    string = field_data.get("string", "")
                    orig_type = field_data.get("orig_type", "")
                    
                    if value in userdata_mapping:
                        new_index = userdata_mapping.get(value)
                        new_fields[field_name] = UserDataData(new_index, string, orig_type)
                    else:
                        if value in instance_mapping:
                            new_value = instance_mapping[value]
                            new_fields[field_name] = UserDataData(new_value, string, orig_type)
                        else:
                            print(f"WARNING: UserDataData field '{field_name}' references instance {value} which wasn't created during paste")
                            print(f"  Available in instance_mapping: {value in instance_mapping}")
                            print(f"  Available in userdata_mapping: {value in userdata_mapping}")
                            # Use the original value, which will likely be invalid
                            new_fields[field_name] = UserDataData(value, string, orig_type)
                        
                        final_value = new_fields[field_name].value
                        if final_value in viewer.scn._rsz_userdata_set if hasattr(viewer.scn, '_rsz_userdata_set') else False:
                            if not string and hasattr(viewer.scn, 'rsz_userdata_infos'):
                                for rui in viewer.scn.rsz_userdata_infos:
                                    if rui.instance_id == final_value:
                                        if hasattr(rui, 'value') and rui.value:
                                            new_fields[field_name].string = rui.value
                                        break
                    
            elif field_type == "GameObjectRefData":
                new_fields[field_name] = self._deserialize_gameobject_ref(field_data, guid_mapping, randomize_guids)
                    
            elif field_type == "ArrayData":
                new_fields[field_name] = self._deserialize_array_with_remapping(
                    field_data, instance_mapping, userdata_mapping, guid_mapping, randomize_guids, viewer
                )
            else:
                element = RszArrayClipboard._deserialize_element(field_data, None, guid_mapping, randomize_guids)
                if element:
                    new_fields[field_name] = element
        
        if type_name == "chainsaw.ContextID" and context_id_mapping and "_Group" in new_fields:
            from file_handlers.rsz.rsz_data_types import S32Data
            group_field = new_fields["_Group"]
            if isinstance(group_field, S32Data):
                original_value = group_field.value
                if original_value in context_id_mapping:
                    group_field.value = context_id_mapping[original_value]
                    
        return new_fields
    
    def process_embedded_rsz_fields(self, viewer, fields_dict, embedded_context=None, userdata_mapping=None):
        """Process fields that need embedded RSZ deserialization"""
        from file_handlers.rsz.rsz_array_clipboard import RszArrayClipboard
        from file_handlers.rsz.rsz_data_types import UserDataData
        
        if not hasattr(self, '_processed_embedded_rsz'):
            self._processed_embedded_rsz = set()
        
        for field_name, field_data in fields_dict.items():
            if isinstance(field_data, dict) and field_data.get("_needs_embedded_rsz_deserialization"):

                original_userdata_value = field_data.get("value", 0)
                
                if original_userdata_value in self._processed_embedded_rsz:
                    if original_userdata_value in userdata_mapping:
                        existing_value = userdata_mapping[original_userdata_value]
                        userdata_element = UserDataData(existing_value, field_data.get("string", ""), field_data.get("orig_type", ""))
                        userdata_element._container_context = None
                        userdata_element._owning_context = None  
                        userdata_element._owning_userdata = None
                        fields_dict[field_name] = userdata_element
                        continue
                    else:
                        print(f"WARNING: Embedded RSZ {original_userdata_value} was processed but not in userdata_mapping")
                
                self._processed_embedded_rsz.add(original_userdata_value)
                
                pre_allocated_instance_id = userdata_mapping.get(original_userdata_value) if userdata_mapping else None
                
                if pre_allocated_instance_id:
                    userdata_element = RszArrayClipboard._create_rsz_userdata_info_for_existing_instance(
                        viewer, field_data, pre_allocated_instance_id
                    )
                else:
                    if self.get_clipboard_type() in ["gameobject", "component"]:
                        userdata_element = RszArrayClipboard._deserialize_complete_embedded_rsz_userdata(
                            viewer, field_data, None  # None = main file context
                        )
                    else:
                        userdata_element = RszArrayClipboard._deserialize_complete_embedded_rsz_userdata(
                            viewer, field_data, embedded_context
                        )
                
                if userdata_element:
                    fields_dict[field_name] = userdata_element
 
                    if userdata_mapping is not None and original_userdata_value > 0 and not pre_allocated_instance_id:
                        userdata_mapping[original_userdata_value] = userdata_element.value
                else:
                    error_msg = f"Failed to deserialize embedded RSZ for field '{field_name}' - this would corrupt the file if we fallback to simple UserData"
                    print(error_msg)
                    raise RuntimeError(error_msg)
    
    def _deserialize_gameobject_ref(self, field_data, guid_mapping, randomize_guids=True):
        """Deserialize a GameObjectRefData field"""
        guid_str = field_data.get("guid_str", "")
        guid_hex = field_data.get("raw_bytes", "")
        orig_type = field_data.get("orig_type", "")
        
        return process_gameobject_ref_data(guid_hex, guid_str, orig_type, guid_mapping, randomize_guids)
    
    def _deserialize_array_with_remapping(self, field_data, instance_mapping, userdata_mapping, guid_mapping, randomize_guids=True, viewer=None):
        """Deserialize an ArrayData field with remapping"""
        from file_handlers.rsz.rsz_data_types import (
            ArrayData, ObjectData, UserDataData, GameObjectRefData, 
            get_type_class
        )
        from file_handlers.rsz.rsz_array_clipboard import RszArrayClipboard
        
        values = field_data.get("values", [])
        orig_type = field_data.get("orig_type", "")
        element_type_name = field_data.get("element_type", "")
        
        element_class = None
        if element_type_name:
            import file_handlers.rsz.rsz_data_types as data_types
            element_class = getattr(data_types, element_type_name, None)
            
        new_array = ArrayData([], element_class, orig_type)
        
        for value_data in values:
            value_type = value_data.get("type", "")
            
            if value_type == "ObjectData":
                ref_id = value_data.get("value", 0)
                value_orig_type = value_data.get("orig_type", "")
                
                if ref_id in userdata_mapping:
                    new_ref_id = userdata_mapping.get(ref_id)
                    new_array.values.append(ObjectData(new_ref_id, value_orig_type))
                else:
                    new_ref_id = instance_mapping.get(ref_id, ref_id)
                    new_array.values.append(ObjectData(new_ref_id, value_orig_type))
                    
            elif value_type == "UserDataData":
                if "object_graph" in value_data and value_data.get("object_graph", {}).get("context_type") == "embedded_rsz":
                    
                    original_userdata_value = value_data.get("value", 0)
                    pre_allocated_instance_id = userdata_mapping.get(original_userdata_value) if userdata_mapping else None
                    
                    if pre_allocated_instance_id:
                        userdata_element = RszArrayClipboard._create_rsz_userdata_info_for_existing_instance(
                            viewer, value_data, pre_allocated_instance_id
                        )
                    else:
                        userdata_element = RszArrayClipboard._deserialize_complete_embedded_rsz_userdata(
                            viewer, value_data, None
                        )
                    
                    if userdata_element:
                        new_array.values.append(userdata_element)
                        if original_userdata_value > 0 and not pre_allocated_instance_id:
                            userdata_mapping[original_userdata_value] = userdata_element.value
                    else:
                        raise ValueError("Failed to deserialize embedded RSZ UserData")
                else:
                    # Simple UserDataData without embedded RSZ
                    value = value_data.get("value", 0)
                    string = value_data.get("string", "")
                    value_orig_type = value_data.get("orig_type", "")
                    
                    if value in userdata_mapping:
                        new_index = userdata_mapping.get(value)
                        new_array.values.append(UserDataData(new_index, string, value_orig_type))
                    else:
                        new_index = instance_mapping.get(value, value)
                        new_array.values.append(UserDataData(new_index, string, value_orig_type))
                  
            elif value_type == "GameObjectRefData":
                new_array.values.append(self._deserialize_gameobject_ref(value_data, guid_mapping, randomize_guids))
            else:
                element = RszArrayClipboard._deserialize_element(value_data, element_class)
                if element:
                    new_array.values.append(element)
                    
        return new_array
    
    def update_userdata_references(self, fields, userdata_mapping):
        """Update UserDataData references with correct userdata mapping"""
        for _, field_data in fields.items():
            if isinstance(field_data, UserDataData) and field_data.value in userdata_mapping:
                field_data.value = userdata_mapping[field_data.value]
            
            elif isinstance(field_data, ArrayData):
                for element in field_data.values:
                    if isinstance(element, UserDataData) and element.value in userdata_mapping:
                        element.value = userdata_mapping[element.value]
    
    def _collect_embedded_rsz_instances(self, rui, viewer):
        """Recursively collect all instance IDs from embedded RSZ structures"""
        instance_ids = set()
        
        if hasattr(rui, 'embedded_instances') and rui.embedded_instances:
            for inst_id in rui.embedded_instances.keys():
                if inst_id > 0:
                    instance_ids.add(inst_id)
        
        if hasattr(rui, 'embedded_userdata_infos'):
            for nested_ui in rui.embedded_userdata_infos:
                nested_instance_id = getattr(nested_ui, 'instance_id', 0)
                if nested_instance_id > 0:
                    instance_ids.add(nested_instance_id)
                
                if hasattr(nested_ui, 'embedded_instances') and nested_ui.embedded_instances:
                    nested_instances = self._collect_embedded_rsz_instances(nested_ui, viewer)
                    instance_ids.update(nested_instances)
        
        return instance_ids
    
    def collect_instance_tree(self, viewer, instance_id, embedded_context=None, visited_instances=None):
        """Collect all instances in a tree starting from the given instance
    
        """
        if visited_instances is None:
            visited_instances = set()
            
        instance_ids = set()
        
        context_id = getattr(embedded_context, 'instance_id', 0) if embedded_context else 0
        instance_key = (instance_id, context_id)
        
        if instance_key in visited_instances:
            return instance_ids
            
        visited_instances.add(instance_key)
        
        if instance_id <= 0 or instance_id >= len(viewer.scn.instance_infos):
            return instance_ids
            
        instance_ids.add(instance_id)
        
        nested_objects, userdata_refs = self.collect_all_references(viewer, instance_id)
        instance_ids.update(nested_objects)
        instance_ids.update(userdata_refs)
        
        for nested_id in nested_objects:
            if nested_id > 0:
                nested_tree = self.collect_instance_tree(viewer, nested_id, embedded_context, visited_instances)
                instance_ids.update(nested_tree)
                
        return instance_ids
    
    def serialize_instance_with_metadata(self, viewer, instance_id, instance_info=None, embedded_context=None):
        """Serialize instance with full metadata including embedded RSZ UserData handling
        
        This extends serialize_instance_data with embedded context tracking and
        embedded RSZ UserData metadata serialization.
        """
        if instance_info is None:
            if instance_id <= 0 or instance_id >= len(viewer.scn.instance_infos):
                return None
            instance_info = viewer.scn.instance_infos[instance_id]
            
        instance_data = self.serialize_instance_data(viewer, instance_id)
        if not instance_data:
            return None
            
        instance_data["embedded_context_id"] = getattr(embedded_context, 'instance_id', 0) if embedded_context else 0
        instance_data["embedded_context_type"] = "embedded" if embedded_context else "main"
        
        if hasattr(viewer.scn, '_rsz_userdata_set') and instance_id in viewer.scn._rsz_userdata_set:
            is_embedded_rsz_userdata = False
            if hasattr(viewer.scn, 'has_embedded_rsz') and viewer.scn.has_embedded_rsz:
                for rui in viewer.scn.rsz_userdata_infos:
                    if (rui.instance_id == instance_id and hasattr(rui, 'embedded_instances') and 
                        rui.embedded_instances):
                        is_embedded_rsz_userdata = True
                        break
            
            if is_embedded_rsz_userdata:
                instance_data["is_embedded_rsz_userdata"] = True
                for rui in viewer.scn.rsz_userdata_infos:
                    if rui.instance_id == instance_id:
                        userdata_info = {}
                        
                        if hasattr(rui, 'hash'):
                            userdata_info["userdata_hash"] = rui.hash
                        elif hasattr(rui, 'json_path_hash'):
                            userdata_info["userdata_hash"] = rui.json_path_hash
                        else:
                            userdata_info["userdata_hash"] = 0
                        
                        if hasattr(rui, 'type_id'):
                            userdata_info["type_id"] = rui.type_id
                        if hasattr(rui, 'data_size'):
                            userdata_info["data_size"] = rui.data_size
                        if hasattr(rui, 'rsz_offset'):
                            userdata_info["rsz_offset"] = rui.rsz_offset
                        
                        if hasattr(viewer.scn, '_rsz_userdata_str_map') and rui in viewer.scn._rsz_userdata_str_map:
                            userdata_info["userdata_string"] = viewer.scn._rsz_userdata_str_map[rui]
                        
                        instance_data["userdata_info"] = userdata_info
                        break
                        
        return instance_data