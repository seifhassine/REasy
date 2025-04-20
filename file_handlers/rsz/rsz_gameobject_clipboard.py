import os
import json
import uuid
import traceback
from file_handlers.rsz.rsz_data_types import *
from file_handlers.rsz.rsz_file import RszInstanceInfo, RszPrefabInfo, RszRSZUserDataInfo, RszUserDataInfo, RszGameObject
from file_handlers.rsz.rsz_array_clipboard import RszArrayClipboard
from file_handlers.rsz.rsz_clipboard_utils import RszClipboardUtils
from utils.hex_util import guid_le_to_str, is_null_guid
from file_handlers.rsz.rsz_instance_operations import RszInstanceOperations

class RszGameObjectClipboard:
    NULL_GUID = bytes(16)
    NULL_GUID_STR = "00000000-0000-0000-0000-000000000000"
    DEFAULT_GO_NAME = "GameObject"
    
    @staticmethod
    def get_clipboard_directory():
        return RszClipboardUtils.get_type_clipboard_directory("gameobject")
        
    @staticmethod
    def get_json_name(viewer):
        return RszClipboardUtils.get_json_name(viewer)
        
    @staticmethod
    def get_clipboard_file(viewer):
        json_name = RszGameObjectClipboard.get_json_name(viewer)
        base_name = os.path.splitext(json_name)[0]
        
        return os.path.join(
            RszGameObjectClipboard.get_clipboard_directory(),
            f"{base_name}-go-clipboard.json"
        )
    
    @staticmethod
    def copy_gameobject_to_clipboard(viewer, gameobject_id):
        try:
            if not hasattr(viewer, "scn") or not hasattr(viewer, "type_registry"):
                print("Viewer is missing required attributes")
                return False
                
            if gameobject_id < 0 or gameobject_id >= len(viewer.scn.object_table):
                print(f"Invalid GameObject ID: {gameobject_id}")
                return False
                
            source_go = RszGameObjectClipboard._find_gameobject_by_id(viewer, gameobject_id)
            if source_go is None:
                print(f"GameObject with ID {gameobject_id} not found")
                return False
                
            source_instance_id = RszGameObjectClipboard._get_instance_id(viewer, gameobject_id)
            if source_instance_id <= 0:
                print(f"Invalid instance ID for GameObject {gameobject_id}")
                return False
                
            source_name = RszGameObjectClipboard._get_gameobject_name(viewer, source_instance_id)
            print(f"Copying GameObject '{source_name}' (ID: {gameobject_id})")
            
            prefab_path = None
            if not viewer.scn.is_pfb and not viewer.scn.is_usr and hasattr(source_go, 'prefab_id'):
                if source_go.prefab_id >= 0 and source_go.prefab_id < len(viewer.scn.prefab_infos):
                    source_prefab = viewer.scn.prefab_infos[source_go.prefab_id]
                    if hasattr(viewer.scn, '_prefab_str_map') and source_prefab in viewer.scn._prefab_str_map:
                        prefab_path = viewer.scn._prefab_str_map[source_prefab]
            
            component_instances = RszGameObjectClipboard._get_components_for_gameobject(viewer, source_go)
            
            child_gameobjects = RszGameObjectClipboard._collect_child_gameobjects(viewer, gameobject_id)
            
            gameobject_data = {
                "name": source_name,
                "object_id": gameobject_id,
                "instance_id": source_instance_id,
                "guid": source_go.guid.hex() if hasattr(source_go, 'guid') and source_go.guid else None,
                "prefab_path": prefab_path,
                "component_count": source_go.component_count,
                "components": component_instances,
                "children": [child.id for child in child_gameobjects],
                "hierarchy": RszGameObjectClipboard._serialize_gameobject_hierarchy(
                    viewer, gameobject_id, component_instances, child_gameobjects
                )
            }
            
            clipboard_file = RszGameObjectClipboard.get_clipboard_file(viewer)
            with open(clipboard_file, 'w') as f:
                json.dump(gameobject_data, f, indent=2, default=RszArrayClipboard._json_serializer)
                
            print(f"Copied GameObject '{source_name}' (ID: {gameobject_id}) to clipboard with {len(component_instances)} components and {len(child_gameobjects)} children")
            return True
            
        except Exception as e:
            print(f"Error copying GameObject to clipboard: {str(e)}")
            traceback.print_exc()
            return False
    
    @staticmethod
    def _collect_child_gameobjects(viewer, parent_id):
        children = []
        
        direct_children = []
        for go in viewer.scn.gameobjects:
            if go.parent_id == parent_id:
                direct_children.append(go)
                children.append(go)
        
        for child in direct_children:
            nested_children = RszGameObjectClipboard._collect_child_gameobjects(viewer, child.id)
            for nested in nested_children:
                if nested not in children:
                    children.append(nested)
        
        return children
    
    @staticmethod
    def _serialize_gameobject_hierarchy(viewer, root_id, component_instances, child_gameobjects):
        hierarchy = {}
        
        def collect_all_instance_ids(go_id, components):
            instance_ids = set()
            
            if go_id < len(viewer.scn.object_table):
                go_instance_id = viewer.scn.object_table[go_id]
                if go_instance_id > 0:
                    instance_ids.add(go_instance_id)
                    
                    for comp_id in components:
                        instance_ids.add(comp_id)
                        
                    if go_instance_id in viewer.scn.parsed_elements:
                        instance_fields = viewer.scn.parsed_elements[go_instance_id]
                        nested_objects = RszGameObjectClipboard._find_nested_objects(viewer, instance_fields, go_instance_id)
                        instance_ids.update(nested_objects)
                        
                        userdata_refs = set()
                        RszGameObjectClipboard._find_userdata_references(instance_fields, userdata_refs)
                        instance_ids.update(userdata_refs)
                    
                    for comp_id in components:
                        if comp_id in viewer.scn.parsed_elements:
                            comp_fields = viewer.scn.parsed_elements[comp_id]
                            nested_objects = RszGameObjectClipboard._find_nested_objects(viewer, comp_fields, comp_id)
                            instance_ids.update(nested_objects)
                            
                            userdata_refs = set()
                            RszGameObjectClipboard._find_userdata_references(comp_fields, userdata_refs)
                            instance_ids.update(userdata_refs)
                            
                            for nested_id in nested_objects:
                                if nested_id in viewer.scn.parsed_elements:
                                    nested_fields = viewer.scn.parsed_elements[nested_id]
                                    nested_nested = RszGameObjectClipboard._find_nested_objects(viewer, nested_fields, nested_id)
                                    instance_ids.update(nested_nested)
                                    
                                    nested_userdata = set()
                                    RszGameObjectClipboard._find_userdata_references(nested_fields, nested_userdata)
                                    instance_ids.update(nested_userdata)
            
            return instance_ids
        
        root_instance_ids = collect_all_instance_ids(root_id, component_instances)
        
        child_instance_ids = set()
        for child in child_gameobjects:
            child_components = RszGameObjectClipboard._get_components_for_gameobject(viewer, child)
            
            child_ids = collect_all_instance_ids(child.id, child_components)
            child_instance_ids.update(child_ids)
        
        all_instance_ids = root_instance_ids.union(child_instance_ids)
        
        instances = {}
        for instance_id in all_instance_ids:
            if instance_id <= 0 or instance_id >= len(viewer.scn.instance_infos):
                continue
                
            instance_info = viewer.scn.instance_infos[instance_id]
            
            if instance_info.type_id <= 0:
                continue
                
            instance_data = {
                "id": instance_id,
                "type_id": instance_info.type_id,
                "crc": instance_info.crc,
                "fields": {}
            }
            
            if hasattr(viewer, "type_registry") and viewer.type_registry:
                type_info = viewer.type_registry.get_type_info(instance_info.type_id)
                if type_info and "name" in type_info:
                    instance_data["type_name"] = type_info["name"]
            
            if instance_id in viewer.scn.parsed_elements:
                fields = viewer.scn.parsed_elements[instance_id]
                for field_name, field_data in fields.items():
                    instance_data["fields"][field_name] = RszArrayClipboard._serialize_element(field_data)
            
            if hasattr(viewer.scn, '_rsz_userdata_set') and instance_id in viewer.scn._rsz_userdata_set:
                instance_data["is_userdata"] = True
                
                for rui in viewer.scn.rsz_userdata_infos:
                    if rui.instance_id == instance_id:
                        instance_data["userdata_hash"] = rui.hash
                        
                        if hasattr(viewer.scn, '_rsz_userdata_str_map') and rui in viewer.scn._rsz_userdata_str_map:
                            instance_data["userdata_string"] = viewer.scn._rsz_userdata_str_map[rui]
                        break
            
            instances[str(instance_id)] = instance_data
        
        gameobjects = {}
        
        go_data = RszGameObjectClipboard._serialize_gameobject_data(viewer, root_id)
        if go_data:
            direct_children = []
            for child in child_gameobjects:
                if child.parent_id == root_id:
                    direct_children.append(child.id)
            go_data["direct_children"] = direct_children
            gameobjects[str(root_id)] = go_data
        
        for child in child_gameobjects:
            child_data = RszGameObjectClipboard._serialize_gameobject_data(viewer, child.id)
            if child_data:
                direct_children = []
                for go in child_gameobjects:
                    if go.parent_id == child.id:
                        direct_children.append(go.id)
                child_data["direct_children"] = direct_children
                gameobjects[str(child.id)] = child_data
        
        hierarchy["instances"] = instances
        hierarchy["gameobjects"] = gameobjects
        hierarchy["root_id"] = root_id
        
        if hasattr(viewer, '_temp_child_map'):
            delattr(viewer, '_temp_child_map')
        
        return hierarchy
    
    @staticmethod
    def _serialize_gameobject_data(viewer, gameobject_id):
        if gameobject_id < 0 or gameobject_id >= len(viewer.scn.object_table):
            return None
            
        go = RszGameObjectClipboard._find_gameobject_by_id(viewer, gameobject_id)
        if not go:
            return None
            
        instance_id = RszGameObjectClipboard._get_instance_id(viewer, gameobject_id)
        if instance_id <= 0:
            return None
            
        components = RszGameObjectClipboard._get_components_for_gameobject(viewer, go)
        
        go_name = RszGameObjectClipboard._get_gameobject_name(viewer, instance_id)
        
        prefab_path = None
        if not viewer.scn.is_pfb and not viewer.scn.is_usr and hasattr(go, 'prefab_id'):
            if go.prefab_id >= 0 and go.prefab_id < len(viewer.scn.prefab_infos):
                prefab = viewer.scn.prefab_infos[go.prefab_id]
                if hasattr(viewer.scn, '_prefab_str_map') and prefab in viewer.scn._prefab_str_map:
                    prefab_path = viewer.scn._prefab_str_map[prefab]
        
        return {
            "id": go.id,
            "instance_id": instance_id,
            "name": go_name,
            "parent_id": go.parent_id,
            "components": components,
            "component_count": go.component_count,
            "prefab_path": prefab_path,
            "guid": go.guid.hex() if hasattr(go, 'guid') and go.guid else None
        }
    
    @staticmethod
    def _find_nested_objects(viewer, fields, base_instance_id):
        """Find instance IDs of nested objects that aren't in the object table."""
        return RszInstanceOperations.find_nested_objects(
            viewer.scn.parsed_elements, base_instance_id, viewer.scn.object_table
        )
    
    @staticmethod
    def _find_userdata_references(fields, userdata_refs):
        """Find all UserDataData references in fields"""
        RszInstanceOperations.find_userdata_references(fields, userdata_refs)
    
    @staticmethod
    def get_clipboard_data(viewer):
        clipboard_file = RszGameObjectClipboard.get_clipboard_file(viewer)
        return RszClipboardUtils.load_clipboard_data(clipboard_file)
    
    @staticmethod
    def paste_gameobject_from_clipboard(viewer, parent_id=-1, new_name=None, cached_clipboard_data=None):
        try:
            if not cached_clipboard_data:
                print("No clipboard data found")
                return None
            
            source_name = cached_clipboard_data.get("name", "")
            prefab_path = cached_clipboard_data.get("prefab_path")
            hierarchy_data = cached_clipboard_data.get("hierarchy", {})
            
            if not hierarchy_data:
                print("No hierarchy data found in clipboard")
                return None
                
            if new_name is None or new_name.strip() == "":
                if source_name and source_name.strip() != "":
                    new_name = f"{source_name}"
                else:
                    new_name = "GameObject_Copy"
            
            root_go_name = new_name
            
            print(f"Pasting GameObject '{source_name}' -> '{root_go_name}' with parent ID {parent_id}")
            
            instance_mapping = {}
            guid_mapping = {}
            userdata_mapping = {}
            
            import random
            context_id_offset = random.randint(20000, 20000000)
            
            instances_data = hierarchy_data.get("instances", {})
            sorted_instance_ids = sorted([int(id_str) for id_str in instances_data.keys()])
            
            insertion_index = len(viewer.scn.instance_infos)
            
            for old_instance_id in sorted_instance_ids:
                instance_data = instances_data.get(str(old_instance_id), {})
                
                type_id = instance_data.get("type_id", 0)
                crc = instance_data.get("crc", 0)
                
                if type_id <= 0:
                    print(f"Warning: Invalid type ID for instance {old_instance_id}")
                    continue
                
                new_instance = RszInstanceInfo()
                new_instance.type_id = type_id
                new_instance.crc = crc
                
                viewer._insert_instance_and_update_references(insertion_index, new_instance)
                new_instance_id = insertion_index
                instance_mapping[old_instance_id] = new_instance_id
                viewer.handler.id_manager.register_instance(new_instance_id)
                
                if instance_data.get("is_userdata", False):
                    RszGameObjectClipboard._setup_userdata_for_pasted_instance(
                        viewer, 
                        new_instance_id, 
                        instance_data.get("userdata_hash", 0),
                        instance_data.get("userdata_string", "")
                    )
                    userdata_mapping[old_instance_id] = new_instance_id
                
                insertion_index = len(viewer.scn.instance_infos)
            
            for old_instance_id in sorted_instance_ids:
                if old_instance_id not in instance_mapping:
                    continue
                    
                new_instance_id = instance_mapping[old_instance_id]
                instance_data = instances_data.get(str(old_instance_id), {})
                
                fields_data = instance_data.get("fields", {})
                new_fields = RszGameObjectClipboard._deserialize_fields_with_remapping(
                    fields_data, instance_mapping, userdata_mapping, guid_mapping
                )
                
                gameobjects_data = hierarchy_data.get("gameobjects", {})
                root_id = hierarchy_data.get("root_id", -1)
                
                if str(old_instance_id) in gameobjects_data:
                    go_data = gameobjects_data[str(old_instance_id)]
                    go_id = go_data.get("id", -1)
                    
                    if go_id == root_id:
                        display_name = root_go_name
                        print(f"Using root name '{display_name}' for root GameObject")
                    else:
                        original_name = go_data.get("name", "")
                        display_name = original_name if original_name else RszGameObjectClipboard.DEFAULT_GO_NAME
                        print(f"Using name '{display_name}' for child GameObject (original: '{original_name}')")
                    
                    if not RszGameObjectClipboard._set_gameobject_name(viewer, new_instance_id, display_name):
                        print(f"WARNING: Failed to set GameObject name to '{display_name}', UI might show incorrect name")
                
                RszGameObjectClipboard._update_chainsaw_context_id_group(
                    viewer, old_instance_id, new_fields, context_id_offset, instance_data
                )
                
                viewer.scn.parsed_elements[new_instance_id] = new_fields
            
            gameobjects_data = hierarchy_data.get("gameobjects", {})
            root_id = hierarchy_data.get("root_id", -1)
            
            root_go_data = gameobjects_data.get(str(root_id), {})
            if not root_go_data:
                print(f"Root GameObject data not found for ID {root_id}")
                return None
                
            root_instance_id = root_go_data.get("instance_id", -1)
            if root_instance_id <= 0 or root_instance_id not in instance_mapping:
                print(f"Root instance ID {root_instance_id} is invalid or not mapped")
                return None
                
            new_root_instance_id = instance_mapping[root_instance_id]
            
            new_root_object_id = len(viewer.scn.object_table)
            viewer.scn.object_table.append(new_root_instance_id)
            
            new_gameobject = RszGameObjectClipboard._create_gameobject_entry(
                viewer, new_root_object_id, parent_id, new_root_instance_id
            )
            
            if root_go_data.get("guid"):
                if not hasattr(new_gameobject, 'guid'):
                    print("Warning: Target GameObject has no GUID field")
                else:
                    source_guid_hex = root_go_data.get("guid")
                    if source_guid_hex:
                        new_guid = RszGameObjectClipboard._handle_gameobject_guid(
                            source_guid_hex, guid_mapping, new_gameobject
                        )
                        if new_guid:
                            new_gameobject.guid = new_guid
                            RszGameObjectClipboard._add_guid_to_settings(viewer, new_root_instance_id, new_guid)
            
            component_instances = root_go_data.get("components", [])
            mapped_component_instances = []
            
            for i, comp_instance_id in enumerate(component_instances):
                if comp_instance_id in instance_mapping:
                    new_comp_instance_id = instance_mapping[comp_instance_id]
                    mapped_component_instances.append(new_comp_instance_id)
                    
                    new_component_object_id = new_root_object_id + i + 1
                    RszGameObjectClipboard._insert_into_object_table(
                        viewer, new_component_object_id, new_comp_instance_id
                    )
            
            new_gameobject.component_count = len(mapped_component_instances)
            
            if prefab_path and not viewer.scn.is_pfb and not viewer.scn.is_usr:
                RszGameObjectClipboard._create_prefab_for_gameobject(
                    viewer, new_gameobject, prefab_path
                )
            
            RszGameObjectClipboard._update_gameobject_hierarchy(viewer, new_gameobject)
            viewer.scn.gameobjects.append(new_gameobject)
            
            pasted_children = []
            go_ids = sorted([int(id_str) for id_str in gameobjects_data.keys()])
            
            go_id_mapping = {}
            
            for go_id in go_ids:
                if go_id == root_id:
                    go_id_mapping[go_id] = new_root_object_id
                    continue
                    
                go_data = gameobjects_data.get(str(go_id), {})
                go_instance_id = go_data.get("instance_id", -1)
                
                if go_instance_id <= 0 or go_instance_id not in instance_mapping:
                    continue
                    
                new_go_instance_id = instance_mapping[go_instance_id]
                
                original_parent_id = go_data.get("parent_id", -1)
                
                if original_parent_id in go_id_mapping:
                    new_parent_id = go_id_mapping[original_parent_id]
                else:
                    new_parent_id = new_root_object_id
                
                new_child_object_id = len(viewer.scn.object_table)
                viewer.scn.object_table.append(new_go_instance_id)
                
                go_id_mapping[go_id] = new_child_object_id
                
                child_name = go_data.get("name", "")
                    
                new_child_go = RszGameObjectClipboard._create_gameobject_entry(
                    viewer, new_child_object_id, new_parent_id, new_go_instance_id
                )
                
                if go_data.get("guid"):
                    child_guid_hex = go_data.get("guid")
                    if child_guid_hex and hasattr(new_child_go, 'guid'):
                        new_child_guid = RszGameObjectClipboard._handle_gameobject_guid(
                            child_guid_hex, guid_mapping, new_child_go, True
                        )
                        if new_child_guid:
                            new_child_go.guid = new_child_guid
                            RszGameObjectClipboard._add_guid_to_settings(viewer, new_go_instance_id, new_child_guid)
                
                child_component_instances = go_data.get("components", [])
                mapped_child_component_instances = []
                
                for i, comp_instance_id in enumerate(child_component_instances):
                    if comp_instance_id in instance_mapping:
                        new_comp_instance_id = instance_mapping[comp_instance_id]
                        mapped_child_component_instances.append(new_comp_instance_id)
                        
                        new_component_object_id = new_child_object_id + i + 1
                        RszGameObjectClipboard._insert_into_object_table(
                            viewer, new_component_object_id, new_comp_instance_id
                        )
                
                new_child_go.component_count = len(mapped_child_component_instances)
                
                child_prefab_path = go_data.get("prefab_path")
                if child_prefab_path and not viewer.scn.is_pfb and not viewer.scn.is_usr:
                    RszGameObjectClipboard._create_prefab_for_gameobject(
                        viewer, new_child_go, child_prefab_path
                    )
                
                RszGameObjectClipboard._update_gameobject_hierarchy(viewer, new_child_go)
                viewer.scn.gameobjects.append(new_child_go)
                
                child_result = {
                    "go_id": new_child_go.id,
                    "instance_id": new_go_instance_id,
                    "name": RszGameObjectClipboard._get_gameobject_name(viewer, new_go_instance_id, child_name),
                    "reasy_id": viewer.handler.id_manager.get_reasy_id_for_instance(new_go_instance_id),
                    "component_count": new_child_go.component_count,
                    "parent_id": new_parent_id,
                    "children": []
                }
                pasted_children.append(child_result)
            
            children_by_parent = {}
            for child in pasted_children:
                parent_go_id = child.pop("parent_id")
                if parent_go_id not in children_by_parent:
                    children_by_parent[parent_go_id] = []
                children_by_parent[parent_go_id].append(child)
            
            root_result = {
                "success": True,
                "go_id": new_gameobject.id,
                "instance_id": new_root_instance_id,
                "name": RszGameObjectClipboard._get_gameobject_name(viewer, new_root_instance_id, root_go_name),
                "parent_id": parent_id,
                "reasy_id": viewer.handler.id_manager.get_reasy_id_for_instance(new_root_instance_id),
                "component_count": new_gameobject.component_count,
                "children": children_by_parent.get(new_gameobject.id, [])
            }
            
            def add_children_recursive(parent_obj):
                if parent_obj["go_id"] in children_by_parent:
                    parent_obj["children"] = children_by_parent[parent_obj["go_id"]]
                    for child in parent_obj["children"]:
                        add_children_recursive(child)
            
            for child in root_result["children"]:
                add_children_recursive(child)
            
            viewer.mark_modified()
            return root_result
                
        except Exception as e:
            print(f"Error pasting GameObject from clipboard: {str(e)}")
            traceback.print_exc()
            return None
    
    @staticmethod
    def _create_gameobject_entry(viewer, object_id, parent_id, instance_id):
        new_go = RszGameObject()
        new_go.id = object_id
        new_go.parent_id = parent_id
        new_go.component_count = 0
        
        if not viewer.scn.is_pfb and not viewer.scn.is_usr:
            guid_bytes = uuid.uuid4().bytes_le
            new_go.guid = guid_bytes
            new_go.prefab_id = -1 
            
        return new_go
    
    @staticmethod
    def _update_gameobject_hierarchy(viewer, gameobject):
        instance_id = viewer.scn.object_table[gameobject.id]
        viewer.scn.instance_hierarchy[instance_id] = {"children": [], "parent": None}
        
        if gameobject.parent_id >= 0 and gameobject.parent_id < len(viewer.scn.object_table):
            parent_instance_id = viewer.scn.object_table[gameobject.parent_id]
            
            if parent_instance_id > 0:
                viewer.scn.instance_hierarchy[instance_id]["parent"] = parent_instance_id
                
                if parent_instance_id in viewer.scn.instance_hierarchy:
                    if "children" not in viewer.scn.instance_hierarchy[parent_instance_id]:
                        viewer.scn.instance_hierarchy[parent_instance_id]["children"] = []
                    
                    viewer.scn.instance_hierarchy[parent_instance_id]["children"].append(instance_id)
    
    @staticmethod
    def _insert_into_object_table(viewer, object_table_index, instance_id):
        if object_table_index >= len(viewer.scn.object_table):
            viewer.scn.object_table.extend(
                [0] * (object_table_index - len(viewer.scn.object_table) + 1)
            )
            viewer.scn.object_table[object_table_index] = instance_id
        else:
            viewer.scn.object_table.insert(object_table_index, instance_id)
            
        for go in viewer.scn.gameobjects:
            if go.id >= object_table_index:
                go.id += 1
            if go.parent_id >= object_table_index:
                go.parent_id += 1
                
        for folder in viewer.scn.folder_infos:
            if folder.id >= object_table_index:
                folder.id += 1
            if folder.parent_id >= object_table_index:
                folder.parent_id += 1
                
        if viewer.scn.is_pfb:
            for ref_info in viewer.scn.gameobject_ref_infos:
                if hasattr(ref_info, "object_id") and ref_info.object_id >= object_table_index:
                    ref_info.object_id += 1
                if hasattr(ref_info, "target_id") and ref_info.target_id >= object_table_index:
                    ref_info.target_id += 1
    
    @staticmethod
    def _setup_userdata_for_pasted_instance(viewer, instance_id, hash_value, string_value):
        new_rui = RszRSZUserDataInfo()
        
        if not hasattr(viewer.scn, '_rsz_userdata_set'):
            viewer.scn._rsz_userdata_set = set()
            
        if not hasattr(viewer.scn, '_rsz_userdata_dict'):
            viewer.scn._rsz_userdata_dict = {}
            
        viewer.scn._rsz_userdata_set.add(instance_id)
        
        new_rui.instance_id = instance_id
        new_rui.hash = hash_value
        new_rui.string_offset = 0
        
        viewer.scn._rsz_userdata_dict[instance_id] = new_rui
        viewer.scn.rsz_userdata_infos.append(new_rui)
        
        if hasattr(viewer.scn, '_rsz_userdata_str_map'):
            viewer.scn._rsz_userdata_str_map[new_rui] = string_value

        if hasattr(viewer.scn, 'userdata_infos'):
            new_ui = RszUserDataInfo()
            new_ui.hash = hash_value
            new_ui.string_offset = 0
            viewer.scn.userdata_infos.append(new_ui)
            
            if hasattr(viewer.scn, '_userdata_str_map'):
                viewer.scn._userdata_str_map[new_ui] = string_value
        
        return True
    
    @staticmethod
    def _create_prefab_for_gameobject(viewer, gameobject, prefab_path):
        if viewer.scn.is_pfb or viewer.scn.is_usr or not hasattr(viewer.scn, 'prefab_infos') or not prefab_path:
            return False
            
        new_prefab = RszPrefabInfo()
        new_prefab.string_offset = 0
        
        prefab_id = len(viewer.scn.prefab_infos)
        viewer.scn.prefab_infos.append(new_prefab)
        gameobject.prefab_id = prefab_id
        
        if hasattr(viewer.scn, '_prefab_str_map'):
            viewer.scn._prefab_str_map[new_prefab] = prefab_path
            
        print(f"Created prefab (ID: {prefab_id}) for GameObject with path: {prefab_path}")
        return True
    
    @staticmethod
    def _deserialize_fields_with_remapping(fields_data, instance_mapping, userdata_mapping, guid_mapping):
        new_fields = {}
        
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
                guid_str = field_data.get("guid_str", "")
                guid_hex = field_data.get("raw_bytes", "")
                orig_type = field_data.get("orig_type", "")
                
                if guid_hex:
                    try:
                        guid_bytes = bytes.fromhex(guid_hex)
                        
                        is_null_guid = is_null_guid(guid_bytes, guid_str)
                        
                        if is_null_guid:
                            print(f"Preserving null GameObjectRef GUID for {field_name}")
                            new_fields[field_name] = GameObjectRefData(guid_str, guid_bytes, orig_type)
                        else:
                            new_guid_bytes = RszGameObjectClipboard._handle_guid_mapping(guid_bytes, guid_mapping)
                            new_guid_str = guid_le_to_str(new_guid_bytes)
                                
                            new_fields[field_name] = GameObjectRefData(new_guid_str, new_guid_bytes, orig_type)
                    except Exception as e:
                        print(f"Error processing GameObjectRefData (using original): {str(e)}")
                        try:
                            guid_bytes = bytes.fromhex(guid_hex)
                            new_fields[field_name] = GameObjectRefData(guid_str, guid_bytes, orig_type)
                        except:
                            new_fields[field_name] = GameObjectRefData(guid_str, None, orig_type)
                else:
                    new_fields[field_name] = GameObjectRefData(guid_str, None, orig_type)
                    
            elif field_type == "ArrayData":
                values = field_data.get("values", [])
                orig_type = field_data.get("orig_type", "")
                element_type_name = field_data.get("element_type", "")
                
                element_class = None
                if element_type_name:
                    element_class = globals().get(element_type_name)
                    
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
                        guid_str = value_data.get("guid_str", "")
                        guid_hex = value_data.get("raw_bytes", "")
                        value_orig_type = value_data.get("orig_type", "")
                        
                        if guid_hex:
                            try:
                                guid_bytes = bytes.fromhex(guid_hex)
                                
                                is_null_guid = is_null_guid(guid_bytes, guid_str)
                                
                                if is_null_guid:
                                    print(f"Preserving null GameObjectRef GUID in array {field_name}")
                                    new_array.values.append(GameObjectRefData(guid_str, guid_bytes, value_orig_type))
                                else:
                                    if guid_bytes in guid_mapping:
                                        new_guid_bytes = guid_mapping[guid_bytes]
                                        new_guid_str = guid_le_to_str(new_guid_bytes)
                                    else:
                                        new_guid_bytes = uuid.uuid4().bytes_le
                                        new_guid_str = guid_le_to_str(new_guid_bytes)
                                        guid_mapping[guid_bytes] = new_guid_bytes
                                        
                                    new_array.values.append(GameObjectRefData(new_guid_str, new_guid_bytes, value_orig_type))
                            except Exception as e:
                                print(f"Error processing array GameObjectRefData: {str(e)}")
                                try:
                                    guid_bytes = bytes.fromhex(guid_hex)
                                    new_array.values.append(GameObjectRefData(guid_str, guid_bytes, value_orig_type))
                                except:
                                    new_array.values.append(GameObjectRefData(guid_str, None, value_orig_type))
                        else:
                            new_array.values.append(GameObjectRefData(guid_str, None, value_orig_type))
                    else:
                        element = RszArrayClipboard._deserialize_element(value_data, element_class)
                        if element:
                            new_array.values.append(element)
                            
                new_fields[field_name] = new_array
            else:
                element = RszArrayClipboard._deserialize_element(field_data, None, guid_mapping)
                if element:
                    new_fields[field_name] = element
                    
        return new_fields
    
    @staticmethod
    def _update_chainsaw_context_id_group(viewer, old_instance_id, fields, context_id_offset, instance_data):
        type_name = instance_data.get("type_name", "")
        
        if type_name == "chainsaw.ContextID":
            print(f"Found chainsaw.ContextID instance, updating _Group field")
            
            if "_Group" in fields and isinstance(fields["_Group"], S32Data):
                original_value = fields["_Group"].value
                new_value = original_value + context_id_offset
                fields["_Group"].value = new_value
                print(f"  Updated _Group from {original_value} to {new_value}")

    @staticmethod
    def has_clipboard_data(viewer):
        clipboard_file = RszGameObjectClipboard.get_clipboard_file(viewer)
        return os.path.exists(clipboard_file)

    @staticmethod
    def _add_guid_to_settings(viewer, instance_id, guid_bytes):
        if instance_id in viewer.scn.parsed_elements:
            fields = viewer.scn.parsed_elements[instance_id]
            
            guid_str = guid_le_to_str(guid_bytes)
            guid_data = GuidData(guid_str, guid_bytes)
            
            guid_data._display_only = True
            
            for go in viewer.scn.gameobjects:
                if go.id < len(viewer.scn.object_table) and viewer.scn.object_table[go.id] == instance_id:
                    guid_data.gameobject = go
                    break
            
            if "GUID" not in fields:
                new_fields = {"GUID": guid_data}
                for key, value in fields.items():
                    new_fields[key] = value
                viewer.scn.parsed_elements[instance_id] = new_fields
            else:
                fields["GUID"] = guid_data
    
    @staticmethod
    def _find_gameobject_by_id(viewer, gameobject_id):
        for go in viewer.scn.gameobjects:
            if go.id == gameobject_id:
                return go
        return None
    
    @staticmethod
    def _get_instance_id(viewer, gameobject_id):
        if gameobject_id < 0 or gameobject_id >= len(viewer.scn.object_table):
            return -1
        return viewer.scn.object_table[gameobject_id]
    
    @staticmethod
    def _get_gameobject_name(viewer, instance_id, default_name=None):
        """Get the name of a GameObject from its first field or default name"""
        if not hasattr(viewer, 'scn') or not hasattr(viewer.scn, 'parsed_elements'):
            return default_name or RszGameObjectClipboard.DEFAULT_GO_NAME
            
        if instance_id not in viewer.scn.parsed_elements:
            return default_name or RszGameObjectClipboard.DEFAULT_GO_NAME
            
        fields = viewer.scn.parsed_elements[instance_id]
        if not fields:
            return default_name or RszGameObjectClipboard.DEFAULT_GO_NAME
            
        if fields:
            first_field_name = next(iter(fields), None)
            if first_field_name:
                first_field = fields[first_field_name]
                if hasattr(first_field, 'value'):
                    return str(first_field.value.strip("\00")) or default_name or RszGameObjectClipboard.DEFAULT_GO_NAME
        
        return default_name or RszGameObjectClipboard.DEFAULT_GO_NAME

    @staticmethod
    def _set_gameobject_name(viewer, instance_id, name):
        if not name:
            name = RszGameObjectClipboard.DEFAULT_GO_NAME
            
        if instance_id <= 0 or instance_id not in viewer.scn.parsed_elements:
            return False
            
        fields = viewer.scn.parsed_elements[instance_id]
        if not fields:
            return False
        
        display_field = None
    
        first_field = fields.get(1)
        
        first_field.value = name
        display_field = first_field
        
        if display_field:
            go_dict = {
                "data": [f"{name} (ID: {instance_id})", ""],
                "type": "gameobject",
                "instance_id": instance_id,
                "reasy_id": viewer.handler.id_manager.get_reasy_id_for_instance(instance_id),
                "children": [],
            }
            display_field.is_gameobject_or_folder_name = go_dict
            return True
        
        return False
    
    @staticmethod
    def _handle_guid_mapping(original_guid, guid_mapping, is_null_guid=False):
        if is_null_guid:
            return original_guid
            
        if original_guid in guid_mapping:
            return guid_mapping[original_guid]
            
        new_guid = uuid.uuid4().bytes_le
        guid_mapping[original_guid] = new_guid
        return new_guid
    
    @staticmethod
    def _get_components_for_gameobject(viewer, gameobject):
        components = []
        for i in range(1, gameobject.component_count + 1):
            comp_object_id = gameobject.id + i
            if comp_object_id < len(viewer.scn.object_table):
                comp_instance_id = viewer.scn.object_table[comp_object_id]
                if comp_instance_id > 0:
                    components.append(comp_instance_id)
        return components
    
    @staticmethod
    def _handle_gameobject_guid(guid_hex, guid_mapping, gameobject, is_child=False):
        if not guid_hex or not hasattr(gameobject, 'guid'):
            return None
            
        source_guid = bytes.fromhex(guid_hex)
        
        if is_null_guid(source_guid):
            return source_guid
        
        if source_guid in guid_mapping:
            return guid_mapping[source_guid]
            
        new_guid = uuid.uuid4().bytes_le
        guid_mapping[source_guid] = new_guid
        return new_guid

