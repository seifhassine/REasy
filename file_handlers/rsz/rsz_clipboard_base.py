import os
import json
import uuid
import traceback
from abc import ABC, abstractmethod
from typing import Dict, Set, Any, Tuple
from file_handlers.rsz.rsz_clipboard_utils import RszClipboardUtils
from file_handlers.rsz.rsz_data_types import (
    ObjectData, UserDataData, GameObjectRefData, ArrayData
)
from file_handlers.rsz.rsz_file import RszRSZUserDataInfo, RszUserDataInfo
from utils.hex_util import guid_le_to_str, is_null_guid


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
    

    
    def collect_all_references(self, viewer, instance_id) -> Tuple[Set[int], Set[int]]:
        """Collect all nested objects and userdata references for an instance"""
        from file_handlers.rsz.rsz_instance_operations import RszInstanceOperations
        
        nested_instances = set()
        userdata_refs = set()
        
        if instance_id in viewer.scn.parsed_elements:
            fields = viewer.scn.parsed_elements[instance_id]
            
            nested_objects = RszInstanceOperations.find_nested_objects(
                viewer.scn.parsed_elements, instance_id, viewer.scn.object_table
            )
            nested_instances.update(nested_objects)
            
            RszInstanceOperations.find_userdata_references(fields, userdata_refs)
            
            for nested_id in list(nested_instances):
                if nested_id in viewer.scn.parsed_elements:
                    deeper_nested = RszInstanceOperations.find_nested_objects(
                        viewer.scn.parsed_elements, nested_id, viewer.scn.object_table
                    )
                    nested_instances.update(deeper_nested)
                    
                    nested_fields = viewer.scn.parsed_elements[nested_id]
                    RszInstanceOperations.find_userdata_references(nested_fields, userdata_refs)
        
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
            
            if relative_id_mapping:
                for field_name, field_data in fields.items():
                    instance_data["fields"][field_name] = RszArrayClipboard._serialize_field_with_mapping(
                        field_data, relative_id_mapping, set(relative_id_mapping.keys()), set()
                    )
            else:
                for field_name, field_data in fields.items():
                    instance_data["fields"][field_name] = RszArrayClipboard._serialize_element(field_data)
        
        userdata_info = RszClipboardUtils.check_userdata_info(viewer, instance_id)
        if userdata_info:
            instance_data["is_userdata"] = True
            instance_data["userdata_hash"] = userdata_info["userdata_hash"]
            if userdata_info["userdata_string"] is not None:
                instance_data["userdata_string"] = userdata_info["userdata_string"]
        
        return instance_data
    
    def setup_userdata_for_pasted_instance(self, viewer, instance_id, hash_value, string_value):
        """Create userdata entries for a pasted instance"""
        if not hasattr(viewer.scn, '_rsz_userdata_set'):
            viewer.scn._rsz_userdata_set = set()
        if not hasattr(viewer.scn, '_rsz_userdata_dict'):
            viewer.scn._rsz_userdata_dict = {}
        if not hasattr(viewer.scn, '_rsz_userdata_str_map'):
            viewer.scn._rsz_userdata_str_map = {}

        new_rui = RszRSZUserDataInfo()
        new_rui.instance_id = instance_id
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
                new_ui = RszUserDataInfo()
                new_ui.hash = hash_value
                new_ui.string_offset = 0
                viewer.scn.userdata_infos.append(new_ui)
                viewer.scn._userdata_str_map[new_ui] = string_value

        return True
    
    def deserialize_fields_with_remapping(self, fields_data, instance_mapping, userdata_mapping, guid_mapping, randomize_guids=True):
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
                value = field_data.get("value", 0)
                string = field_data.get("string", "")
                orig_type = field_data.get("orig_type", "")
                
                if value in userdata_mapping:
                    new_index = userdata_mapping.get(value)
                    new_fields[field_name] = UserDataData(new_index, string, orig_type)
                else:
                    new_index = instance_mapping.get(value, value)
                    new_fields[field_name] = UserDataData(new_index, string, orig_type)
                    
            elif field_type == "GameObjectRefData":
                new_fields[field_name] = self._deserialize_gameobject_ref(field_data, guid_mapping, randomize_guids)
                    
            elif field_type == "ArrayData":
                new_fields[field_name] = self._deserialize_array_with_remapping(
                    field_data, instance_mapping, userdata_mapping, guid_mapping, randomize_guids
                )
            else:
                element = RszArrayClipboard._deserialize_element(field_data, None, guid_mapping, randomize_guids)
                if element:
                    new_fields[field_name] = element
                    
        return new_fields
    
    def _deserialize_gameobject_ref(self, field_data, guid_mapping, randomize_guids=True):
        """Deserialize a GameObjectRefData field"""
        guid_str = field_data.get("guid_str", "")
        guid_hex = field_data.get("raw_bytes", "")
        orig_type = field_data.get("orig_type", "")
        
        if guid_hex:
            try:
                guid_bytes = bytes.fromhex(guid_hex)
                
                if is_null_guid(guid_bytes, guid_str):
                    return GameObjectRefData(guid_str, guid_bytes, orig_type)
                else:
                    new_guid_bytes = self._handle_guid_mapping(guid_bytes, guid_mapping, randomize_guids)
                    new_guid_str = guid_le_to_str(new_guid_bytes)
                    return GameObjectRefData(new_guid_str, new_guid_bytes, orig_type)
            except Exception as e:
                print(f"Error processing GameObjectRefData: {str(e)}")
                return GameObjectRefData(guid_str, None, orig_type)
        else:
            return GameObjectRefData(guid_str, None, orig_type)
    
    def _deserialize_array_with_remapping(self, field_data, instance_mapping, userdata_mapping, guid_mapping, randomize_guids=True):
        """Deserialize an ArrayData field with remapping"""
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
                from file_handlers.rsz.rsz_array_clipboard import RszArrayClipboard
                element = RszArrayClipboard._deserialize_element(value_data, element_class)
                if element:
                    new_array.values.append(element)
                    
        return new_array
    
    def _handle_guid_mapping(self, original_guid, guid_mapping, randomize=True):
        """Handle GUID mapping for pasted objects"""
        if original_guid in guid_mapping:
            return guid_mapping[original_guid]
            
        if randomize:
            new_guid = uuid.uuid4().bytes_le
        else:
            new_guid = original_guid
            
        guid_mapping[original_guid] = new_guid
        return new_guid
    
    def update_userdata_references(self, fields, userdata_mapping):
        """Update UserDataData references with correct userdata mapping"""
        for _, field_data in fields.items():
            if isinstance(field_data, UserDataData) and field_data.value in userdata_mapping:
                field_data.value = userdata_mapping[field_data.value]
            
            elif isinstance(field_data, ArrayData):
                for element in field_data.values:
                    if isinstance(element, UserDataData) and element.value in userdata_mapping:
                        element.value = userdata_mapping[element.value]