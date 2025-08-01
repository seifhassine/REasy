import os
import json
import uuid
import traceback
from PySide6.QtWidgets import QMessageBox, QLineEdit, QInputDialog
from file_handlers.rsz.rsz_data_types import (
    ObjectData, UserDataData, ArrayData, S32Data, GuidData
)
from file_handlers.rsz.rsz_file import RszInstanceInfo, RszPrefabInfo, RszRSZUserDataInfo, RszUserDataInfo, RszGameObject
from file_handlers.rsz.rsz_array_clipboard import RszArrayClipboard
from file_handlers.rsz.rsz_clipboard_base import RszClipboardBase
from utils.hex_util import guid_le_to_str, is_null_guid
from file_handlers.rsz.rsz_clipboard_utils import RszClipboardUtils

class RszGameObjectClipboard(RszClipboardBase):
    NULL_GUID = bytes(16)
    NULL_GUID_STR = "00000000-0000-0000-0000-000000000000"
    DEFAULT_GO_NAME = "GameObject"
    _instance = None
    
    def get_clipboard_type(self) -> str:
        return "gameobject"
    
    @classmethod
    def get_instance(cls):
        """Get singleton instance"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    @staticmethod
    def get_clipboard_file(viewer):
        """Get clipboard file path for this clipboard type"""
        instance = RszGameObjectClipboard.get_instance()
        return RszClipboardBase.get_clipboard_file(instance, viewer)
    
    @staticmethod
    def has_clipboard_data(viewer):
        """Check if clipboard data exists"""
        instance = RszGameObjectClipboard.get_instance()
        return RszClipboardBase.has_clipboard_data(instance, viewer)
    
    @staticmethod
    def get_clipboard_data(viewer):
        """Get clipboard data from file"""
        instance = RszGameObjectClipboard.get_instance()
        return RszClipboardBase.get_clipboard_data(instance, viewer)
    
    @staticmethod
    def get_json_name(viewer):
        """Get JSON name for the viewer"""
        instance = RszGameObjectClipboard.get_instance()
        return RszClipboardBase.get_json_name(instance, viewer)
    
    @staticmethod
    def copy_gameobject_to_clipboard(viewer, gameobject_id):
        try:
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
            if viewer.scn.is_scn:
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
            
            instance = RszGameObjectClipboard.get_instance()
            instance.save_clipboard_data(viewer, gameobject_data)
                
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
                        from file_handlers.rsz.rsz_instance_operations import RszInstanceOperations
                        nested_objects = RszInstanceOperations.find_nested_objects(
                            viewer.scn.parsed_elements, go_instance_id, viewer.scn.object_table
                        )
                        instance_ids.update(nested_objects)
                        
                        userdata_refs = set()
                        RszInstanceOperations.find_userdata_references(instance_fields, userdata_refs)
                        instance_ids.update(userdata_refs)
                    
                    for comp_id in components:
                        if comp_id in viewer.scn.parsed_elements:
                            comp_fields = viewer.scn.parsed_elements[comp_id]
                            nested_objects = RszInstanceOperations.find_nested_objects(
                                viewer.scn.parsed_elements, comp_id, viewer.scn.object_table
                            )
                            instance_ids.update(nested_objects)
                            
                            userdata_refs = set()
                            RszInstanceOperations.find_userdata_references(comp_fields, userdata_refs)
                            instance_ids.update(userdata_refs)
                            
                            for nested_id in nested_objects:
                                if nested_id in viewer.scn.parsed_elements:
                                    nested_fields = viewer.scn.parsed_elements[nested_id]
                                    nested_nested = RszInstanceOperations.find_nested_objects(
                                        viewer.scn.parsed_elements, nested_id, viewer.scn.object_table
                                    )
                                    instance_ids.update(nested_nested)
                                    
                                    nested_userdata = set()
                                    RszInstanceOperations.find_userdata_references(nested_fields, nested_userdata)
                                    instance_ids.update(nested_userdata)
            
            return instance_ids
        
        root_instance_ids = collect_all_instance_ids(root_id, component_instances)
        
        child_instance_ids = set()
        for child in child_gameobjects:
            child_components = RszGameObjectClipboard._get_components_for_gameobject(viewer, child)
            
            child_ids = collect_all_instance_ids(child.id, child_components)
            child_instance_ids.update(child_ids)
        
        all_instance_ids = root_instance_ids.union(child_instance_ids)
        
        ordered_instance_ids = sorted(all_instance_ids)
        
        instances = {}
        for instance_id in ordered_instance_ids:
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
        hierarchy["instance_order"] = ordered_instance_ids  # Preserve original ordering
        
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
        if viewer.scn.is_scn:
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
    def _serialize_folder_data(viewer, folder_id):
        """Serialize a folder's data for clipboard"""
        try:
            folder = next((f for f in getattr(viewer.scn, "folder_infos", []) if f.id == folder_id), None)
            if not folder:
                return None
                
            instance_id = viewer.scn.object_table[folder_id] if folder_id < len(viewer.scn.object_table) else -1
            folder_name = f"Folder_{folder_id}" 
            fields_data = {}
            
            if instance_id > 0 and instance_id in viewer.scn.parsed_elements:
                fields = viewer.scn.parsed_elements[instance_id]
                folder_name = RszGameObjectClipboard._get_instance_name_from_fields(fields, f"Folder_{folder_id}")
                
                from file_handlers.rsz.rsz_array_clipboard import RszArrayClipboard
                for field_name, field_data in fields.items():
                    try:
                        fields_data[field_name] = RszArrayClipboard._serialize_element(field_data)
                    except Exception as e:
                        print(f"Warning: Could not serialize field '{field_name}' for folder {folder_id}: {e}")
                        
            return {
                "id": folder.id,
                "instance_id": instance_id,
                "name": folder_name,
                "parent_id": folder.parent_id,
                "fields": fields_data
            }
        except Exception as e:
            print(f"Error serializing folder {folder_id}: {e}")
            return None
        
    @staticmethod
    def _paste_folder_from_json(viewer, folder_json, parent_index):
        """Paste a folder from JSON data as a child of parent_index"""
        name = folder_json.get("name", f"Folder_{folder_json.get('id', 0)}")
        parent_id = folder_json.get("parent_id", -1)
        if hasattr(viewer, "object_operations"):
            folder_data = viewer.object_operations.create_folder(name, parent_id)
        else:
            folder_data = None
        if not (folder_data and folder_data.get("success")):
            return None
        if hasattr(viewer.tree, "add_folder_to_ui_direct"):
            return viewer.tree.add_folder_to_ui_direct(folder_data, parent_index)
        return None

    @staticmethod
    def _paste_folder_with_fields(viewer, folder_data, parent_id, randomize_ids=True):
        """Paste a folder with full field data restoration"""
        try:
            name = folder_data.get("name", "Folder")
            
            new_folder = viewer.object_operations.create_folder(name, parent_id)
            if not (new_folder and new_folder.get("success")):
                return None
            
            new_folder_id = new_folder.get("folder_id", new_folder.get("id"))
            if new_folder_id is None:
                return None
                
            fields_data = folder_data.get("fields", {})
            if fields_data:
                instance_id = viewer.scn.object_table[new_folder_id] if new_folder_id < len(viewer.scn.object_table) else -1
                
                if instance_id > 0:
                    instance = RszGameObjectClipboard.get_instance()
                    
                    instance_mapping = {}
                    userdata_mapping = {}  
                    guid_mapping = {}
                    
                    restored_fields = instance.deserialize_fields_with_remapping(
                        fields_data, instance_mapping, userdata_mapping, guid_mapping, randomize_ids
                    )
                    
                    if restored_fields:
                        viewer.scn.parsed_elements[instance_id] = restored_fields
                        viewer.mark_modified()
            
            return new_folder
            
        except Exception as e:
            print(f"Error pasting folder with fields: {e}")
            import traceback
            traceback.print_exc()
            return None

    @staticmethod
    def _paste_gameobject_from_json(viewer, go_json, parent_index):
        """Paste a GameObject from JSON data as a child of parent_index"""
        clipboard_data = {
            "name": go_json.get("name"),
            "object_id": go_json.get("id"),
            "instance_id": go_json.get("instance_id"),
            "component_count": go_json.get("component_count"),
            "components": go_json.get("components"),
            "guid": go_json.get("guid"),
            "prefab_path": go_json.get("prefab_path"),
            "hierarchy": {}
        }
        result = RszGameObjectClipboard.paste_gameobject_from_clipboard(
            viewer, parent_id=-1, new_name=go_json.get("name"), cached_clipboard_data=clipboard_data
        )
        if hasattr(viewer.tree, "add_gameobject_to_ui_direct") and result:
            return viewer.tree.add_gameobject_to_ui_direct(result, parent_index)
        return None

    @staticmethod
    def copy_folder_to_clipboard(viewer, folder_id):
        try:
            if folder_id < 0 or folder_id >= len(viewer.scn.folder_infos):
                print(f"Invalid Folder ID: {folder_id}")
                return False
                
            source_folder = next((f for f in viewer.scn.folder_infos if f.id == folder_id), None)
            if source_folder is None:
                print(f"Folder with ID {folder_id} not found")
                return False
                
            source_instance_id = RszGameObjectClipboard._get_instance_id(viewer, folder_id)
            if source_instance_id <= 0:
                print(f"Invalid instance ID for Folder {folder_id}")
                return False
                
            source_name = RszGameObjectClipboard._get_gameobject_name(viewer, source_instance_id)
            print(f"Copying Folder '{source_name}' (ID: {folder_id})")
            
            child_gameobjects = RszGameObjectClipboard._collect_child_gameobjects(viewer, folder_id)
            
            folder_data = {
                "name": source_name,
                "id": folder_id,
                "instance_id": source_instance_id,
                "children": [child.id for child in child_gameobjects],
                "hierarchy": RszGameObjectClipboard._serialize_gameobject_hierarchy(
                    viewer, folder_id, [], child_gameobjects
                )
            }
            
            instance = RszGameObjectClipboard.get_instance()
            instance.save_clipboard_data(viewer, folder_data)
                
            print(f"Copied Folder '{source_name}' (ID: {folder_id}) to clipboard with {len(child_gameobjects)} children")
            return True
            
        except Exception as e:
            print(f"Error copying Folder to clipboard: {str(e)}")
            traceback.print_exc()
            return False
    
    @staticmethod
    def _paste_hierarchy_from_clipboard(viewer, parent_id=-1, new_name=None, cached_clipboard_data=None, 
                                    is_folder=False, create_prefab=True, randomize_ids=True):
        """
        Unified paste logic for both GameObjects and Folders.
        
        Args:
            viewer: The viewer instance
            parent_id: Parent ID for the pasted object
            new_name: Optional new name for the root object
            cached_clipboard_data: The clipboard data to paste
            is_folder: True if pasting a folder, False for GameObject
            create_prefab: Whether to create prefab entries (only for GameObjects in .scn files)
        """
        try:
            if not cached_clipboard_data:
                print("No clipboard data found")
                return None
            
            source_name = cached_clipboard_data.get("name", "")
            hierarchy_data = cached_clipboard_data.get("hierarchy", {})
            prefab_path = cached_clipboard_data.get("prefab_path") if not is_folder else None
            
            if not hierarchy_data:
                print("No hierarchy data found in clipboard")
                return None
                
            object_type = "Folder" if is_folder else "GameObject"
            default_name = "Folder_Copy" if is_folder else "GameObject_Copy"
            
            if new_name is None or new_name.strip() == "":
                new_name = f"{source_name}" if source_name and source_name.strip() != "" else default_name
            
            root_name = new_name
            
            print(f"Pasting {object_type} '{source_name}' -> '{root_name}' with parent ID {parent_id}")
            
            instance_mapping = {}
            guid_mapping = {}
            userdata_mapping = {}
            
            instance = RszGameObjectClipboard.get_instance()
            
            if randomize_ids:
                import random
                context_id_offset = random.randint(20000, 20000000)
            else:
                context_id_offset = 0
            
            instances_data = hierarchy_data.get("instances", {})
            sorted_instance_ids = hierarchy_data.get("instance_order", sorted([int(id_str) for id_str in instances_data.keys()]))
            print(f"Using preserved instance ordering: {len(sorted_instance_ids)} instances")
            
            insertion_index = len(viewer.scn.instance_infos)
            
            # Create instances phase
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
                    instance.setup_userdata_for_pasted_instance(
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
                new_fields = instance.deserialize_fields_with_remapping(
                    fields_data, instance_mapping, userdata_mapping, guid_mapping, randomize_ids
                )
                
                gameobjects_data = hierarchy_data.get("gameobjects", {})
                root_id = hierarchy_data.get("root_id", -1)
                
                if str(old_instance_id) in gameobjects_data:
                    go_data = gameobjects_data[str(old_instance_id)]
                    go_id = go_data.get("id", -1)
                    
                    if go_id == root_id:
                        display_name = root_name
                        print(f"Using root name '{display_name}' for root {object_type}")
                    else:
                        original_name = go_data.get("name", "")
                        display_name = original_name if original_name else RszGameObjectClipboard.DEFAULT_GO_NAME
                        print(f"Using name '{display_name}' for child {object_type} (original: '{original_name}')")
                    
                RszGameObjectClipboard._update_chainsaw_context_id_group(
                    viewer, old_instance_id, new_fields, context_id_offset, instance_data
                )
                
                viewer.scn.parsed_elements[new_instance_id] = new_fields
            
            gameobjects_data = hierarchy_data.get("gameobjects", {})
            root_id = hierarchy_data.get("root_id", -1)
            
            root_go_data = gameobjects_data.get(str(root_id), {})
            if not root_go_data:
                print(f"Root {object_type} data not found for ID {root_id}")
                return None
                
            root_instance_id = root_go_data.get("instance_id", -1)
            if root_instance_id <= 0 or root_instance_id not in instance_mapping:
                print(f"Root instance ID {root_instance_id} is invalid or not mapped")
                return None
                
            new_root_instance_id = instance_mapping[root_instance_id]
            
            new_root_object_id = len(viewer.scn.object_table)
            viewer.scn.object_table.append(new_root_instance_id)
            
            new_root_object = RszGameObjectClipboard._create_gameobject_entry(viewer, new_root_object_id, parent_id)
            
            if root_go_data.get("guid") and hasattr(new_root_object, 'guid'):
                source_guid_hex = root_go_data.get("guid")
                if source_guid_hex:
                    new_guid = RszGameObjectClipboard._handle_gameobject_guid(
                        source_guid_hex, guid_mapping, new_root_object, randomize=randomize_ids
                    )
                    if new_guid:
                        new_root_object.guid = new_guid
                        RszGameObjectClipboard._add_guid_to_settings(viewer, new_root_instance_id, new_guid)
            
            if not is_folder:
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
                
                new_root_object.component_count = len(mapped_component_instances)
                
                if create_prefab and prefab_path and viewer.scn.is_scn:
                    RszGameObjectClipboard._create_prefab_for_gameobject(
                        viewer, new_root_object, prefab_path
                    )
            else:
                new_root_object.component_count = 0  # Folders have no components
            
            RszGameObjectClipboard._update_gameobject_hierarchy(viewer, new_root_object)
            viewer.scn.gameobjects.append(new_root_object)
            
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
                    
                new_child_go = RszGameObjectClipboard._create_gameobject_entry(viewer, new_child_object_id, new_parent_id)
                
                if go_data.get("guid") and hasattr(new_child_go, 'guid'):
                    child_guid_hex = go_data.get("guid")
                    if child_guid_hex:
                        new_child_guid = RszGameObjectClipboard._handle_gameobject_guid(
                            child_guid_hex, guid_mapping, new_child_go, True, randomize=randomize_ids
                        )
                        if new_child_guid:
                            new_child_go.guid = new_child_guid
                            RszGameObjectClipboard._add_guid_to_settings(viewer, new_go_instance_id, new_child_guid)
                
                if not is_folder:
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
                    
                    if create_prefab and viewer.scn.is_scn:
                        child_prefab_path = go_data.get("prefab_path")
                        if child_prefab_path:
                            RszGameObjectClipboard._create_prefab_for_gameobject(
                                viewer, new_child_go, child_prefab_path
                            )
                else:
                    new_child_go.component_count = 0  # Folders have no components
                
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
                "go_id": new_root_object.id,
                "instance_id": new_root_instance_id,
                "name": RszGameObjectClipboard._get_gameobject_name(viewer, new_root_instance_id, root_name),
                "parent_id": parent_id,
                "reasy_id": viewer.handler.id_manager.get_reasy_id_for_instance(new_root_instance_id),
                "component_count": new_root_object.component_count,
                "children": children_by_parent.get(new_root_object.id, [])
            }
            
            def add_children_recursive(parent_obj):
                if parent_obj["go_id"] in children_by_parent:
                    parent_obj["children"] = children_by_parent[parent_obj["go_id"]]
                    for child in parent_obj["children"]:
                        add_children_recursive(child)
            
            for child in root_result["children"]:
                add_children_recursive(child)
            
            id_adjustment_map = RszGameObjectClipboard._cleanup_duplicate_userdata_after_paste(viewer, set(instance_mapping.values()))
            if id_adjustment_map:
                RszGameObjectClipboard._update_result_with_id_adjustments(root_result, id_adjustment_map)
                
            viewer.mark_modified()
            return root_result
                
        except Exception as e:
            print(f"Error pasting {object_type} from clipboard: {str(e)}")
            traceback.print_exc()
            return None

    @staticmethod
    def paste_gameobject_from_clipboard(viewer, parent_id=-1, new_name=None, cached_clipboard_data=None):
        """Paste a GameObject from clipboard"""
        return RszGameObjectClipboard._paste_hierarchy_from_clipboard(
            viewer, parent_id, new_name, cached_clipboard_data, 
            is_folder=False, create_prefab=True, randomize_ids=True
        )

    @staticmethod
    def paste_folder_from_clipboard(viewer, parent_id=-1, new_name=None, cached_clipboard_data=None):
        """Paste a Folder from clipboard"""
        return RszGameObjectClipboard._paste_hierarchy_from_clipboard(
            viewer, parent_id, new_name, cached_clipboard_data, 
            is_folder=True, create_prefab=False
        )
    _export_override_dir = None

    @staticmethod
    def get_datablock_clipboard_directory(viewer):
        """
        Return the export/import directory when overriding,
        otherwise fall back to the original clipboard path.
        """
        if RszGameObjectClipboard._export_override_dir:
            return RszGameObjectClipboard._export_override_dir

        base_dir = RszClipboardUtils.get_type_clipboard_directory("datablock")
        json_name = RszGameObjectClipboard.get_json_name(viewer)
        base_name = os.path.splitext(json_name)[0]
        return os.path.join(base_dir, f"{base_name}-datablock-clipboard")

    @staticmethod
    def copy_datablock_to_clipboard(viewer):
        """
        Copy the entire Data Block (all GameObjects and Folders) to a folder.
        Each GameObject and Folder is exported as a full template (deep copy).
        """
        try:
            dir_path = RszGameObjectClipboard.get_datablock_clipboard_directory(viewer)
            os.makedirs(dir_path, exist_ok=True)
            
            for filename in os.listdir(dir_path):
                if filename.endswith('.json'):
                    os.remove(os.path.join(dir_path, filename))

            if hasattr(viewer.scn, 'is_usr') and viewer.scn.is_usr:
                return RszGameObjectClipboard._copy_userfile_using_array_clipboard(viewer, dir_path)
            
            folders_exported = 0
            for folder in getattr(viewer.scn, "folder_infos", []):
                folder_id = folder.id
                folder_data = RszGameObjectClipboard._serialize_folder_data(viewer, folder_id)
                if folder_data:
                    with open(os.path.join(dir_path, f"folder_{folder_id}.json"), "w", encoding="utf-8") as f:
                        json.dump(folder_data, f, indent=2)
                    folders_exported += 1

            gameobjects_exported = 0
            for go in viewer.scn.gameobjects:
                go_id = go.id
                success = RszGameObjectClipboard.copy_gameobject_to_clipboard(viewer, go_id)
                if success:
                    clipboard_data = RszGameObjectClipboard.get_clipboard_data(viewer)
                    if clipboard_data:
                        clipboard_data["parent_id"] = go.parent_id
                        with open(os.path.join(dir_path, f"go_{go_id}.json"), "w", encoding="utf-8") as f:
                            json.dump(clipboard_data, f, indent=2)
                        gameobjects_exported += 1
                    else:
                        print(f"Failed to get clipboard data for GameObject {go_id}")
                else:
                    print(f"Failed to export GameObject {go_id}")

            # Create ordering list that preserves relative positions
            mixed_order = []
            for i, instance_id in enumerate(viewer.scn.object_table):
                if instance_id <= 0:
                    continue
                go = next((g for g in viewer.scn.gameobjects if g.id == i), None)
                if go:
                    mixed_order.append({"type": "gameobject", "id": go.id})
                    continue
                folder = next((f for f in getattr(viewer.scn, "folder_infos", []) if f.id == i), None)
                if folder:
                    mixed_order.append({"type": "folder", "id": folder.id})
            
            manifest = {
                "mixed_order": mixed_order,
                "exported_gameobjects": gameobjects_exported,
                "exported_folders": folders_exported
            }
            with open(os.path.join(dir_path, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2)

            print(f"Copied Data Block to clipboard folder: {dir_path}")
            print(f"Exported {gameobjects_exported} GameObjects and {folders_exported} Folders")
            return True
            
        except Exception as e:
            print(f"Error copying Data Block: {e}")
            traceback.print_exc()
            return False

    @staticmethod
    def paste_datablock_from_clipboard(viewer, parent_folder_id=-1, parent_index=None, no_parent_folder=False, randomize_ids=False):
        """
        Paste the Data Block from clipboard folder with preserved ordering.
        
        Args:
            randomize_ids: If True, randomize GUIDs and context IDs. If False, preserve original IDs.
        """
        try:
            dir_path = RszGameObjectClipboard.get_datablock_clipboard_directory(viewer)
            
            if hasattr(viewer.scn, 'is_usr') and viewer.scn.is_usr:
                return RszGameObjectClipboard._paste_userfile_using_array_clipboard(viewer, dir_path, randomize_ids)
            
            manifest_path = os.path.join(dir_path, "manifest.json")
            if not os.path.exists(manifest_path):
                print("No Data Block clipboard manifest found")
                return None

            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)

            tree = getattr(viewer, 'tree', None)
            pasted_indices = []
            mixed_order = manifest.get("mixed_order", [])
            folder_model_map = {}
            folder_ui_map = {}
            
            all_data = {}
            for item in mixed_order:
                item_id = item["id"]
                item_type = item["type"]
                
                if item_type == "folder":
                    file_path = os.path.join(dir_path, f"folder_{item_id}.json")
                elif item_type == "gameobject":
                    file_path = os.path.join(dir_path, f"go_{item_id}.json")
                else:
                    continue
                    
                if os.path.exists(file_path):
                    with open(file_path, 'r', encoding='utf-8') as f:
                        all_data[item_id] = json.load(f)
            
            # Process items in original order and and handling dependencies
            remaining_items = mixed_order.copy()
            
            while remaining_items:
                progress_made = False
                
                for i, item in enumerate(remaining_items):
                    item_id = item["id"]
                    item_type = item["type"]
                    
                    if item_id not in all_data:
                        remaining_items.pop(i)
                        progress_made = True
                        break
                    
                    data = all_data[item_id]
                    parent_old = data.get("parent_id", -1)
                    
                    # Check if parent dependency is satisfied
                    if parent_old == -1 or parent_old in folder_model_map:
                        model_parent = parent_folder_id if parent_old == -1 else folder_model_map[parent_old]
                        ui_parent = parent_index if parent_old == -1 else folder_ui_map.get(parent_old)
                        
                        if item_type == "folder":
                            new_folder = RszGameObjectClipboard._paste_folder_with_fields(
                                viewer, data, model_parent, randomize_ids
                            )
                            if new_folder and new_folder.get("success"):
                                new_model_id = new_folder.get("folder_id", new_folder.get("id"))
                                folder_model_map[item_id] = new_model_id
                                
                                if tree and hasattr(tree, 'add_folder_to_ui_direct'):
                                    new_ui = tree.add_folder_to_ui_direct(new_folder, ui_parent)
                                    if new_ui is not None:
                                        folder_ui_map[item_id] = new_ui
                                        pasted_indices.append(new_ui)
                        
                        elif item_type == "gameobject":
                            result = RszGameObjectClipboard._paste_hierarchy_from_clipboard(
                                viewer, model_parent, data.get("name"), data, 
                                is_folder=False, create_prefab=True, randomize_ids=randomize_ids
                            )
                            if result and result.get("success"):
                                if tree and hasattr(tree, 'add_gameobject_to_ui_direct'):
                                    new_ui = tree.add_gameobject_to_ui_direct(result, ui_parent)
                                    if new_ui is not None:
                                        pasted_indices.append(new_ui)
                        
                        remaining_items.pop(i)
                        progress_made = True
                        break
                
                if not progress_made:
                    print(f"Could not resolve dependencies for {len(remaining_items)} remaining items")
                    break
            
            randomization_mode = "with randomized IDs" if randomize_ids else "preserving original IDs"
            print(f"Data Block pasted with preserved ordering ({randomization_mode}): {len(pasted_indices)} items")
            viewer.mark_modified()
            return pasted_indices

        except Exception as e:
            print(f"Error pasting Data Block: {e}")
            traceback.print_exc()
            return None

    @staticmethod
    def export_datablock(viewer):
        """
        Export the Data Block into exports/<fileType>/<exportName>/.
        Reuses copy_datablock_to_clipboard internally and then writes a manifest.
        """
        try:
            export_name, ok = QInputDialog.getText(
                None,
                "Export Data Block",
                "Enter export name:",
                QLineEdit.Normal,
                "MyExport"
            )
            if not ok or not export_name.strip():
                return False
            export_name = export_name.strip()

            if hasattr(viewer.scn, 'is_usr') and viewer.scn.is_usr:
                file_type = 'usr'
            elif viewer.scn.is_scn:
                file_type = 'scn'
            else:
                file_type = 'pfb'
            
            registry_name = "default"
            if hasattr(viewer, 'handler') and hasattr(viewer.handler, 'app') and hasattr(viewer.handler.app, 'settings'):
                settings_path = viewer.handler.app.settings.get("rcol_json_path", "default")
                if settings_path and settings_path != "default":
                    registry_name = os.path.basename(settings_path)
            
            export_dir = os.path.join('exports', file_type, export_name)
            os.makedirs(export_dir, exist_ok=True)

            RszGameObjectClipboard._export_override_dir = export_dir

            try:
                success = RszGameObjectClipboard.copy_datablock_to_clipboard(viewer)
            finally:
                RszGameObjectClipboard._export_override_dir = None

            if not success:
                QMessageBox.warning(None, "Export Failed", "Failed to export Data Block.")
                return False

            if file_type == 'usr':
                array_files = [f for f in os.listdir(export_dir) if f.endswith('.json') and '_' in f]
                export_manifest = {
                    'file_type': file_type,
                    'type_registry': registry_name,
                    'export_name': export_name,
                    'total_arrays': len(array_files)
                }
            else:
                export_manifest = {
                    'file_type': file_type,
                    'type_registry': registry_name,
                    'export_name': export_name,
                    'folders': [f.id for f in getattr(viewer.scn, 'folder_infos', [])],
                    'gameobjects': [g.id for g in viewer.scn.gameobjects],
                    'total_folders': len(getattr(viewer.scn, 'folder_infos', [])),
                    'total_gameobjects': len(viewer.scn.gameobjects)
                }
            
            with open(os.path.join(export_dir, 'export_manifest.json'), 'w', encoding='utf-8') as m:
                json.dump(export_manifest, m, indent=2)

            QMessageBox.information(None, "Export Complete", f"Exported to:\n{export_dir}")
            return True

        except Exception as e:
            QMessageBox.critical(None, "Export Error", f"Error exporting Data Block:\n{e}")
            traceback.print_exc()
            RszGameObjectClipboard._export_override_dir = None
            return False

    @staticmethod
    def import_datablock(viewer, parent_folder_id=-1, parent_index=None, randomize_ids=False):
        """
        Import from exports/<fileType>/folder—only those matching manifest's file_type
        and registry. Uses the original paste_datablock_from_clipboard method.
        """
        try:
            if hasattr(viewer.scn, 'is_usr') and viewer.scn.is_usr:
                file_type = 'usr'
            elif viewer.scn.is_scn:
                file_type = 'scn'
            else:
                file_type = 'pfb'
            exports_root = os.path.join('exports', file_type)
            
            if not os.path.isdir(exports_root):
                QMessageBox.warning(None, "Import Error", f"No exports directory found for '{file_type}' files")
                return None

            try:
                choices = sorted([d for d in os.listdir(exports_root) 
                                if os.path.isdir(os.path.join(exports_root, d))])
            except OSError as e:
                QMessageBox.warning(None, "Import Error", f"Cannot read exports directory: {e}")
                return None
                
            if not choices:
                QMessageBox.warning(None, "Import Error", f"No exports found for '{file_type}' files")
                return None

            name, ok = QInputDialog.getItem(
                None,
                "Import Data Block",
                "Select export:",
                choices, 0, False
            )
            if not ok or not name:
                return None

            export_dir = os.path.join(exports_root, name)
            
            export_manifest_path = os.path.join(export_dir, 'export_manifest.json')
            manifest_path = os.path.join(export_dir, 'manifest.json')
            
            manifest_file = None
            if os.path.exists(export_manifest_path):
                manifest_file = export_manifest_path
            elif os.path.exists(manifest_path):
                manifest_file = manifest_path
            else:
                QMessageBox.warning(None, "Import Error", "No manifest file found in export")
                return None

            try:
                with open(manifest_file, 'r', encoding='utf-8') as m:
                    manifest = json.load(m)
            except Exception as e:
                QMessageBox.warning(None, "Import Error", f"Cannot read manifest file: {e}")
                return None

            manifest_file_type = manifest.get('file_type')
            if manifest_file_type and manifest_file_type != file_type:
                QMessageBox.warning(None, "Import Error",
                    f"Export is for '{manifest_file_type}' files, current file is '{file_type}'.")
                return None
                
            current_reg = "default"
            if hasattr(viewer, 'handler') and hasattr(viewer.handler, 'app') and hasattr(viewer.handler.app, 'settings'):
                settings_path = viewer.handler.app.settings.get("rcol_json_path", "default")
                if settings_path and settings_path != "default":
                    current_reg = os.path.basename(settings_path)
                    
            manifest_registry = manifest.get('type_registry')
            if manifest_registry and manifest_registry != current_reg:
                reply = QMessageBox.question(None, "Registry Mismatch",
                    f"Export registry: '{manifest_registry}'\nCurrent registry: '{current_reg}'\n\nContinue anyway?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if reply != QMessageBox.Yes:
                    return None

            RszGameObjectClipboard._export_override_dir = export_dir

            try:
                result = RszGameObjectClipboard.paste_datablock_from_clipboard(
                    viewer,
                    parent_folder_id=parent_folder_id,
                    parent_index=parent_index,
                    no_parent_folder=False, 
                    randomize_ids=randomize_ids
                )
            finally:
                RszGameObjectClipboard._export_override_dir = None

            if not result:
                QMessageBox.warning(None, "Import Failed", "No items were imported.")
                return None

            return result

        except Exception as e:
            QMessageBox.critical(None, "Import Error", f"Error importing Data Block:\n{e}")
            traceback.print_exc()
            RszGameObjectClipboard._export_override_dir = None
            return None
        
    @staticmethod
    def _create_gameobject_entry(viewer, object_id, parent_id):
        new_go = RszGameObject()
        new_go.id = object_id
        new_go.parent_id = parent_id
        new_go.component_count = 0
        
        if viewer.scn.is_scn:
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
                if ref_info.object_id >= object_table_index:
                    ref_info.object_id += 1
                if ref_info.target_id >= object_table_index:
                    ref_info.target_id += 1
    
    @staticmethod
    def _setup_userdata_for_pasted_instance(viewer, instance_id, hash_value, string_value):
        """
        Create userdata entries. Reuse existing UserDataInfo if string already exists.
        """
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
        
        print(f"Created userdata instance {instance_id} for string '{string_value}'")

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
    @staticmethod
    def _delete_instances_and_update_references(viewer, instances_to_delete):
        """Delete multiple instances and update all references efficiently"""
        if not instances_to_delete:
            return {}
            
        sorted_instances = sorted(instances_to_delete, reverse=True)
        
        for instance_id in sorted_instances:
            RszGameObjectClipboard._delete_single_instance(viewer, instance_id)
        
        return viewer._create_id_adjustment_map(instances_to_delete)

    @staticmethod
    def _create_prefab_for_gameobject(viewer, gameobject, prefab_path):
        if not viewer.scn.is_scn or not hasattr(viewer.scn, 'prefab_infos') or not prefab_path:
            return False

        if hasattr(viewer.scn, '_prefab_str_map'):
            for existing_prefab, existing_path in viewer.scn._prefab_str_map.items():
                if existing_path == prefab_path:
                    prefab_id = viewer.scn.prefab_infos.index(existing_prefab)
                    gameobject.prefab_id = prefab_id
                    return True

        new_prefab = RszPrefabInfo()
        new_prefab.string_offset = 0

        prefab_id = len(viewer.scn.prefab_infos)
        viewer.scn.prefab_infos.append(new_prefab)
        gameobject.prefab_id = prefab_id

        if hasattr(viewer.scn, '_prefab_str_map'):
            viewer.scn._prefab_str_map[new_prefab] = prefab_path

        return True
    

    
    @staticmethod
    def _update_chainsaw_context_id_group(viewer, old_instance_id, fields, context_id_offset, instance_data):
        type_name = instance_data.get("type_name", "")
        
        if type_name == "chainsaw.ContextID":
            #print("Found chainsaw.ContextID instance, updating _Group field")
            
            if "_Group" in fields and isinstance(fields["_Group"], S32Data):
                original_value = fields["_Group"].value
                new_value = original_value + context_id_offset
                fields["_Group"].value = new_value
                #print(f"  Updated _Group from {original_value} to {new_value}")

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
    def _get_instance_name_from_fields(fields, default_name=None):
        """Extract name from instance fields (used by both GameObjects and Folders)"""
        if not fields:
            return default_name or RszGameObjectClipboard.DEFAULT_GO_NAME
            
        first_field_name = next(iter(fields), None)
        if first_field_name:
            first_field = fields[first_field_name]
            if hasattr(first_field, 'value'):
                try:
                    name = str(first_field.value).strip("\00")
                    if name: 
                        return name
                except Exception:
                    pass
                    
        return default_name or RszGameObjectClipboard.DEFAULT_GO_NAME

    @staticmethod
    def _get_gameobject_name(viewer, instance_id, default_name=None):
        """Get the name of a GameObject from its first field or default name"""
        if not hasattr(viewer, 'scn') or not hasattr(viewer.scn, 'parsed_elements'):
            return default_name or RszGameObjectClipboard.DEFAULT_GO_NAME
            
        if instance_id not in viewer.scn.parsed_elements:
            return default_name or RszGameObjectClipboard.DEFAULT_GO_NAME
            
        fields = viewer.scn.parsed_elements[instance_id]
        return RszGameObjectClipboard._get_instance_name_from_fields(fields, default_name)
    
    @staticmethod
    def _set_gameobject_name(viewer, instance_id, name):
        if not name:
            name = RszGameObjectClipboard.DEFAULT_GO_NAME
            
        if instance_id <= 0 or instance_id not in viewer.scn.parsed_elements:
            print("shit 2")
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
    def _handle_gameobject_guid(guid_hex, guid_mapping, gameobject, is_child=False, randomize=True):
        if not guid_hex or not hasattr(gameobject, 'guid'):
            return None
            
        source_guid = bytes.fromhex(guid_hex)
        
        if is_null_guid(source_guid):
            return source_guid
        
        if source_guid in guid_mapping:
            return guid_mapping[source_guid]
        
        if randomize:
            new_guid = uuid.uuid4().bytes_le
        else:
            new_guid = source_guid  # Keep original GUID
        
        guid_mapping[source_guid] = new_guid
        return new_guid

    @staticmethod
    def _cleanup_duplicate_userdata_after_paste(viewer, pasted_instance_ids):
        """Clean up duplicate userdata entries after paste operations"""
        try:
            if not hasattr(viewer.scn, 'rsz_userdata_infos') or not hasattr(viewer.scn, '_rsz_userdata_str_map'):
                return {}
                
            print("Starting userdata deduplication cleanup...")
            
            # Group RszRSZUserDataInfo by string value
            string_to_ruis = {}
            for rui in viewer.scn.rsz_userdata_infos:
                string_value = viewer.scn._rsz_userdata_str_map.get(rui, "")
                if string_value:
                    string_to_ruis.setdefault(string_value, []).append(rui)
            
            # Find duplicates
            instances_to_delete = []
            instance_remapping = {}
            ruis_to_remove = []
            
            for string_value, rui_list in string_to_ruis.items():
                if len(rui_list) <= 1:
                    continue
                    
                # Keep the first, remove the rest
                rui_list.sort(key=lambda x: x.instance_id)
                kept_rui = rui_list[0]
                
                print(f"String '{string_value}' has {len(rui_list)} RUIs, keeping instance {kept_rui.instance_id}")
                
                for rui in rui_list[1:]:
                    instances_to_delete.append(rui.instance_id)
                    instance_remapping[rui.instance_id] = kept_rui.instance_id
                    ruis_to_remove.append(rui)
                    print(f"  Will delete instance {rui.instance_id}")
            
            if not instances_to_delete:
                print("No duplicate userdata found")
                return {}
            
            # Update references before deletion
            RszGameObjectClipboard._update_all_userdata_references(viewer, instance_remapping)
            
            # Remove duplicate RUIs
            for rui in ruis_to_remove:
                viewer.scn.rsz_userdata_infos.remove(rui)
                if rui in viewer.scn._rsz_userdata_str_map:
                    del viewer.scn._rsz_userdata_str_map[rui]
            
            # Clean tracking structures
            for instance_id in instances_to_delete:
                viewer.scn._rsz_userdata_set.discard(instance_id)
                viewer.scn._rsz_userdata_dict.pop(instance_id, None)
            
            # Delete instances and get adjustment map
            id_adjustment_map = RszGameObjectClipboard._delete_instances_and_update_references(
                viewer, instances_to_delete
            )
            
            # Update remaining RUI instance IDs
            RszGameObjectClipboard._update_rui_instance_ids_after_deletion(viewer, instances_to_delete)
            
            print(f"Cleanup completed: removed {len(ruis_to_remove)} RUIs and {len(instances_to_delete)} instances")
            return id_adjustment_map
            
        except Exception as e:
            print(f"Error during userdata cleanup: {str(e)}")
            import traceback
            traceback.print_exc()
            return {}
    @staticmethod
    def _update_all_userdata_references(viewer, instance_remapping):
        """Update all UserDataData and ObjectData fields to point to kept instances"""
        
        update_count = 0
        
        for instance_id, fields in viewer.scn.parsed_elements.items():
            for field_name, field_data in fields.items():
                if isinstance(field_data, UserDataData):
                    if field_data.value in instance_remapping:
                        old_value = field_data.value
                        new_value = instance_remapping[old_value]
                        field_data.value = new_value
                        update_count += 1
                        print(f"    Updated UserDataData: {old_value} -> {new_value}")
                        
                elif isinstance(field_data, ObjectData):
                    if field_data.value in instance_remapping:
                        old_value = field_data.value
                        new_value = instance_remapping[old_value]
                        field_data.value = new_value
                        update_count += 1
                        print(f"    Updated ObjectData: {old_value} -> {new_value}")
                        
                elif isinstance(field_data, ArrayData):
                    for element in field_data.values:
                        if isinstance(element, UserDataData) and element.value in instance_remapping:
                            old_value = element.value
                            new_value = instance_remapping[old_value]
                            element.value = new_value
                            update_count += 1
                        elif isinstance(element, ObjectData) and element.value in instance_remapping:
                            old_value = element.value
                            new_value = instance_remapping[old_value]
                            element.value = new_value
                            update_count += 1
        
        print(f"  Updated {update_count} references")

    @staticmethod
    def _delete_single_instance(viewer, instance_id):
        """Delete a single instance and update all references"""
        if instance_id < 0 or instance_id >= len(viewer.scn.instance_infos):
            return
            
        # Remove from instance_infos
        viewer.scn.instance_infos.pop(instance_id)
        
        # Remove from parsed_elements
        if instance_id in viewer.scn.parsed_elements:
            del viewer.scn.parsed_elements[instance_id]
        
        # Update parsed_elements keys for instances after the deleted one
        new_parsed_elements = {}
        for inst_id, fields in viewer.scn.parsed_elements.items():
            if inst_id > instance_id:
                new_parsed_elements[inst_id - 1] = fields
            else:
                new_parsed_elements[inst_id] = fields
        viewer.scn.parsed_elements = new_parsed_elements
        
        # Update all references in fields
        
        for inst_id, fields in viewer.scn.parsed_elements.items():
            for field_name, field_data in fields.items():
                if isinstance(field_data, (UserDataData, ObjectData)):
                    if field_data.value > instance_id:
                        field_data.value -= 1
                elif isinstance(field_data, ArrayData):
                    for element in field_data.values:
                        if isinstance(element, (UserDataData, ObjectData)) and element.value > instance_id:
                            element.value -= 1
        
        # Update object table
        for i in range(len(viewer.scn.object_table)):
            if viewer.scn.object_table[i] > instance_id:
                viewer.scn.object_table[i] -= 1
            elif viewer.scn.object_table[i] == instance_id:
                viewer.scn.object_table[i] = 0  # Mark as invalid
        
        # Update instance hierarchy if it exists
        if hasattr(viewer.scn, 'instance_hierarchy'):
            new_hierarchy = {}
            for inst_id, data in viewer.scn.instance_hierarchy.items():
                if inst_id == instance_id:
                    continue  # Skip deleted instance
                elif inst_id > instance_id:
                    new_id = inst_id - 1
                    new_children = []
                    for child_id in data.get("children", []):
                        if child_id > instance_id:
                            new_children.append(child_id - 1)
                        elif child_id != instance_id:
                            new_children.append(child_id)
                    parent_id = data.get("parent")
                    if parent_id and parent_id > instance_id:
                        parent_id = parent_id - 1
                    elif parent_id == instance_id:
                        parent_id = None
                    new_hierarchy[new_id] = {"children": new_children, "parent": parent_id}
                else:
                    # Update children and parent references
                    new_children = []
                    for child_id in data.get("children", []):
                        if child_id > instance_id:
                            new_children.append(child_id - 1)
                        elif child_id != instance_id:
                            new_children.append(child_id)
                    parent_id = data.get("parent")
                    if parent_id and parent_id > instance_id:
                        parent_id = parent_id - 1
                    elif parent_id == instance_id:
                        parent_id = None
                    new_hierarchy[inst_id] = {"children": new_children, "parent": parent_id}
            viewer.scn.instance_hierarchy = new_hierarchy

    @staticmethod
    def _update_rui_instance_ids_after_deletion(viewer, deleted_instance_ids):
        """Update RszRSZUserDataInfo instance IDs after instances were deleted"""
        # Sort deleted IDs to process from lowest to highest
        deleted_sorted = sorted(deleted_instance_ids)
        
        for rui in viewer.scn.rsz_userdata_infos:
            original_id = rui.instance_id
            new_id = original_id
            
            # Subtract 1 for each deleted instance that was before this one
            for deleted_id in deleted_sorted:
                if deleted_id < original_id:
                    new_id -= 1
                else:
                    break
                    
            if new_id != original_id:
                rui.instance_id = new_id
                print(f"  Updated RUI instance_id: {original_id} -> {new_id}")
                
                # Update tracking structures
                if hasattr(viewer.scn, '_rsz_userdata_set'):
                    if original_id in viewer.scn._rsz_userdata_set:
                        viewer.scn._rsz_userdata_set.remove(original_id)
                    viewer.scn._rsz_userdata_set.add(new_id)
                    
                if hasattr(viewer.scn, '_rsz_userdata_dict'):
                    if original_id in viewer.scn._rsz_userdata_dict:
                        viewer.scn._rsz_userdata_dict[new_id] = viewer.scn._rsz_userdata_dict.pop(original_id)

    @staticmethod
    def _update_result_with_id_adjustments(result, id_adjustment_map):
        """
        Update a paste result dictionary with new instance IDs after deletion.
        """
        if not id_adjustment_map or not result:
            return
            
        # Update the root instance ID
        if 'instance_id' in result and result['instance_id'] in id_adjustment_map:
            result['instance_id'] = id_adjustment_map[result['instance_id']]
        
        # Update children recursively
        if 'children' in result:
            for child in result['children']:
                RszGameObjectClipboard._update_result_with_id_adjustments(child, id_adjustment_map)
    
    @staticmethod
    def _copy_userfile_using_array_clipboard(viewer, dir_path):
        """Export user file data using array clipboard multi-item functionality"""
        try:
            if not viewer.scn.object_table:
                print("Error: User file has no root object")
                return False
            
            root_instance_id = viewer.scn.object_table[0]
            root_fields = viewer.scn.parsed_elements.get(root_instance_id, {})
            if not root_fields:
                print("Error: Root object not found in parsed elements")
                return False
            
            from file_handlers.rsz.rsz_array_clipboard import RszArrayClipboard
            from file_handlers.rsz.rsz_data_types import ArrayData
            import json
            
            array_clipboard = RszArrayClipboard()
            exported_count = 0
            
            for field_name, field_data in root_fields.items():
                if not (isinstance(field_data, ArrayData) and hasattr(field_data, 'values') and field_data.values):
                    continue
                
                try:
                    array_type = getattr(field_data, 'orig_type', 'Array')
                    if array_clipboard.copy_multiple_to_clipboard(viewer.tree, field_data.values, array_type):
                        clipboard_data = array_clipboard.get_clipboard_data(viewer.tree)
                        if clipboard_data:
                            filename = f"{field_name}.json"
                            filepath = os.path.join(dir_path, filename)
                            
                            with open(filepath, 'w', encoding='utf-8') as f:
                                json.dump(clipboard_data, f, indent=2, default=RszClipboardUtils.json_serializer)
                            
                            exported_count += 1
                            print(f"Exported array '{field_name}' with {len(field_data.values)} elements")
                        
                except Exception as e:
                    print(f"Warning: Could not export array field '{field_name}': {e}")
                    continue
            
            if exported_count == 0:
                print("Error: No array fields were exported")
                return False
            
            print(f"User file data block exported: {exported_count} array files")
            return True
            
        except Exception as e:
            print(f"Error exporting user file data block: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    @staticmethod
    def _paste_userfile_using_array_clipboard(viewer, dir_path, randomize_ids=False):
        """
        Paste user file data using existing array clipboard functionality.
        Much simpler approach that reuses existing import logic.
        """
        try:
            array_files = [f for f in os.listdir(dir_path) 
                          if f.endswith('.json') and '_' in f and not f.startswith('export_manifest')]
            
            if not array_files:
                print("No user file array data found in clipboard")
                return None
            
            if not viewer.scn.object_table:
                print("Error: Current user file has no root object")
                return None
            
            root_instance_id = viewer.scn.object_table[0]
            root_fields = viewer.scn.parsed_elements.get(root_instance_id, {})
            if not root_fields:
                print("Error: Current root object not found")
                return None
            
            from file_handlers.rsz.rsz_array_clipboard import RszArrayClipboard
            from file_handlers.rsz.rsz_data_types import ArrayData
            import json
            
            array_clipboard = RszArrayClipboard()
            imported_arrays = 0
            skipped_arrays = 0
            
            print(f"Available fields in root object: {list(root_fields.keys())}")
            print()
            
            for array_file in array_files:
                try:
                    file_parts = array_file.rsplit('_', 1)
                    if len(file_parts) == 2:
                        field_name = file_parts[0] 
                    else:
                        field_name = array_file.replace('.json', '') 
                    
                    if field_name not in root_fields:
                        print(f"Warning: Array field '{field_name}' not found in current object, skipping")
                        skipped_arrays += 1
                        continue
                    
                    current_field = root_fields[field_name]
                    
                    if not isinstance(current_field, ArrayData):
                        print(f"Warning: Field '{field_name}' is not an array in current object, skipping")
                        skipped_arrays += 1
                        continue
                    
                    source_path = os.path.join(dir_path, array_file)
                    with open(source_path, 'r', encoding='utf-8') as f:
                        clipboard_data = json.load(f)
                    
                    array_type = getattr(current_field, 'orig_type', 'Array')
                    
                    is_compatible = array_clipboard.is_clipboard_compatible_with_array(viewer, array_type)
                    
                    if is_compatible:
                        try:
                            elements_list = clipboard_data.get('data', [])
                            added_count = 0
                            
                            array_tree_node = viewer.tree.find_user_file_array_node()
                            
                            for element_data in elements_list:
                                try:
                                    element = array_clipboard._paste_single_element(
                                        viewer, element_data, current_field, array_tree_node
                                    )
                                    
                                    if element:
                                        added_count += 1
                                        
                                        if array_tree_node:
                                            array_clipboard._add_element_to_ui_direct(viewer.tree, array_tree_node, element)
                                        
                                except Exception as e:
                                    print(f"Warning: Failed to deserialize element: {e}")
                                    continue
                            
                            if added_count > 0:
                                imported_arrays += 1
                                viewer.mark_modified()
                            else:
                                skipped_arrays += 1
                                
                        except Exception as e:
                            print(f"Warning: Error importing array '{field_name}': {e}")
                            skipped_arrays += 1
                    else:
                        print(f"Warning: Array '{field_name}' is not compatible, skipping")
                        skipped_arrays += 1
                        
                except Exception as e:
                    print(f"Warning: Error importing array file '{array_file}': {e}")
                    skipped_arrays += 1
                    continue
            
            if imported_arrays > 0:
                viewer.mark_modified()
                print(f"User file data imported: {imported_arrays} arrays imported, {skipped_arrays} arrays skipped")
                return {"imported_arrays": imported_arrays, "skipped_arrays": skipped_arrays}
            else:
                print("No arrays were imported")
                return None
                
        except Exception as e:
            print(f"Error importing user file data block: {e}")
            import traceback
            traceback.print_exc()
            return None
    

    


