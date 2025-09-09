"""
Helper class for handling object operations in RSZ files.

This file contains utility methods for:
- Creating GameObjects
- Deleting GameObjects
- Handling components 
- Managing folders
"""

from PySide6.QtWidgets import QMessageBox
from .rsz_data_types import (
    get_type_class, ObjectData, ArrayData, 
    UserDataData, S32Data
)
from file_handlers.rsz.rsz_instance_operations import RszInstanceOperations
from utils.id_manager import EmbeddedIdManager
from file_handlers.rsz.utils.rsz_embedded_utils import copy_embedded_rsz_header


class RszObjectOperations:
    """Helper class for handling object creation and deletion in RSZ files"""
    
    def __init__(self, viewer):
        """Initialize with a reference to the RSZ viewer"""
        self.viewer = viewer
        self.scn = viewer.scn
        self.type_registry = viewer.type_registry

    def create_folder(self, name: str, parent_id: int):
        """
        Create a FolderInfo (SCN-18/19) entry and a backing RSZ instance.
        """
        if not self.scn:
            return {"success": False, "error": "Scene missing"}

        type_info, type_id = self.type_registry.find_type_by_name("via.Folder")
        if not type_info or not type_id:
            return {"success": False, "error": "via.Folder not in registry"}

        insertion_index = len(self.scn.instance_infos)
        new_instance = self.viewer._initialize_new_instance(type_id, type_info)
        if not new_instance:
            return {"success": False, "error": "Could not init folder instance"}

        self.viewer._insert_instance_and_update_references(insertion_index, new_instance)
        reasy_id = self.viewer.handler.id_manager.register_instance(insertion_index)

        folder_fields = {}
        self.viewer._initialize_fields_from_type_info(folder_fields, type_info)
        first_field = next(iter(folder_fields.values()), None)
        if hasattr(first_field, "value"):
            first_field.value = name

        self.scn.parsed_elements[insertion_index] = folder_fields

        obj_table_idx = len(self.scn.object_table)
        self.scn.object_table.append(insertion_index)

        fi = self.viewer.create_scn_folder_object()
        fi.id = obj_table_idx
        fi.parent_id = parent_id
        self.scn.folder_infos.append(fi)

        self._update_instance_hierarchy(insertion_index,
                                        None if parent_id < 0 else self.scn.object_table[parent_id])

        self.viewer.mark_modified()

        return {
            "success": True,
            "instance_id": insertion_index,
            "reasy_id": reasy_id,
            "name": name,
            "folder_id": obj_table_idx,
            "parent_id": parent_id
        }
    def create_gameobject(self, name, parent_id):
        """Create a new GameObject with the given name and parent ID"""
        if not self.scn:
            return False
            
        insertion_index = self._calculate_gameobject_insertion_index()
        type_info, type_id = self.type_registry.find_type_by_name("via.GameObject")
        
        if not type_info or type_id == 0:
            QMessageBox.warning(self.viewer, "Error", "Cannot find GameObject type in registry")
            return False
            
        new_instance = self.viewer._initialize_new_instance(type_id, type_info)
        if not new_instance:
            QMessageBox.warning(self.viewer, "Error", "Failed to create GameObject instance")
            return False
            
        self.viewer._insert_instance_and_update_references(insertion_index, new_instance)
        self.viewer.handler.id_manager.register_instance(insertion_index)
        
        gameobject_fields = {}
        self.viewer._initialize_fields_from_type_info(gameobject_fields, type_info)
        
        fields = list(gameobject_fields.values())
        first_field = fields[0] if len(fields) else None
        
        if hasattr(first_field, "__class__") and first_field.__class__.__name__ == "StringData":
            first_field.value = name
            print(f"Setting GameObject name '{name}' in first field")
            
            go_dict = {
                "data": [f"{name} (ID: {insertion_index})", ""],
                "type": "gameobject",
                "instance_id": insertion_index,
                "reasy_id": self.viewer.handler.id_manager.get_reasy_id_for_instance(insertion_index),
                "children": [],
            }
            first_field.is_gameobject_or_folder_name = go_dict

        self.scn.parsed_elements[insertion_index] = gameobject_fields
        object_table_index = len(self.scn.object_table)
        self.scn.object_table.append(insertion_index)
        
        new_gameobject = self._create_gameobject_entry(
            object_table_index, parent_id
        )
        
        self._update_gameobject_hierarchy(new_gameobject)
        self.scn.gameobjects.append(new_gameobject)
        self.viewer.mark_modified()
        
        return {
            "success": True,
            "go_id": object_table_index,
            "instance_id": insertion_index,
            "name": name,
            "parent_id": parent_id,
            "reasy_id": self.viewer.handler.id_manager.get_reasy_id_for_instance(insertion_index)
        }

    def _calculate_gameobject_insertion_index(self):
        """Calculate the best insertion index for a new GameObject instance"""
        return len(self.scn.instance_infos)

    def _create_gameobject_entry(self, object_id, parent_id):
        """Create a new GameObject entry for the scene"""
        from file_handlers.rsz.rsz_file import RszGameObject
        new_go = RszGameObject()
        new_go.id = object_id
        new_go.parent_id = parent_id
        new_go.component_count = 0
        
        if self.scn.is_scn:
            from .utils.rsz_guid_utils import create_new_guid
            guid_bytes = create_new_guid()
            new_go.guid = guid_bytes
            new_go.prefab_id = -1 
            
        return new_go

    def _update_gameobject_hierarchy(self, gameobject):
        """Update instance hierarchy with parent-child relationship for GameObjects"""
        instance_id = self.scn.object_table[gameobject.id]
        self.scn.instance_hierarchy[instance_id] = {"children": [], "parent": None}
        
        if gameobject.parent_id >= 0 and gameobject.parent_id < len(self.scn.object_table):
            parent_instance_id = self.scn.object_table[gameobject.parent_id]
            
            if parent_instance_id > 0:
                self.scn.instance_hierarchy[instance_id]["parent"] = parent_instance_id
                
                if parent_instance_id in self.scn.instance_hierarchy:
                    if "children" not in self.scn.instance_hierarchy[parent_instance_id]:
                        self.scn.instance_hierarchy[parent_instance_id]["children"] = []
                    
                    self.scn.instance_hierarchy[parent_instance_id]["children"].append(instance_id)
    
    def _find_gameobject_by_id(self, gameobject_id):
        for go in self.scn.gameobjects:
            if go.id == gameobject_id:
                return go
        return None
        
    def delete_gameobject(self, gameobject_id):
        """Delete a GameObject with the given ID"""
        if gameobject_id < 0 or gameobject_id >= len(self.scn.object_table):
            QMessageBox.warning(self.viewer, "Error", f"Invalid GameObject ID: {gameobject_id}")
            return False
            
        target_go = self._find_gameobject_by_id(gameobject_id)
        
        if target_go is None:
            QMessageBox.warning(self.viewer, "Error", f"GameObject with ID {gameobject_id} not found")
            return False
            
        success = self._delete_gameobject_directly(target_go)
        
        if success:
            self.viewer.mark_modified()
            
        return success
    
    def _collect_gameobject_hierarchy_by_reference(self, root_go):
        """Collect all GameObjects in the hierarchy starting from the root"""
        go_objects = {go.id: go for go in self.scn.gameobjects}
        
        child_map = {}
        for go in self.scn.gameobjects:
            if go.parent_id >= 0 and go.parent_id in go_objects:
                parent = go_objects[go.parent_id]
                if parent not in child_map:
                    child_map[parent] = []
                child_map[parent].append(go)
        
        gameobjects = []
        
        def collect_recursive(go):
            gameobjects.append(go)
            if go in child_map:
                for child in child_map[go]:
                    collect_recursive(child)
        
        collect_recursive(root_go)
        return gameobjects
    
    def should_delete_prefab(self, prefab_id, gameobjects):
        for other_go in self.scn.gameobjects:
            if other_go not in gameobjects and other_go.prefab_id == prefab_id:
                print(f"  Skipping prefab deletion: prefab_id {prefab_id} is still used by other GameObjects")
                return False
        return True
    
    def _delete_gameobject_directly(self, gameobject):
        """Delete a GameObject and its hierarchy directly"""
        prefabs_to_delete = set()
        gameobject_refs_to_delete = self._collect_gameobject_hierarchy_by_reference(gameobject)

        for go in reversed(gameobject_refs_to_delete):
            if self.scn.is_scn:
                if go.prefab_id >= 0 and self.should_delete_prefab(go.prefab_id, gameobject_refs_to_delete):
                    prefabs_to_delete.add(go.prefab_id)
            
            self._delete_all_components_of_gameobject(go)
            
            go_instance_id = self.scn.object_table[go.id]
            
            #print(f"  Deleting GameObject instance {go_instance_id} (object_id: {go.id})")
        
            nested_objects = RszInstanceOperations.find_nested_objects(
                self.scn.parsed_elements, go_instance_id, self.scn.object_table
            )
            nested_objects.add(go_instance_id)
            
            for instance_id in sorted(nested_objects, reverse=True):
                self.viewer._remove_instance_references(instance_id)
            
            id_mapping = self.viewer._update_instance_references_after_deletion(go_instance_id, nested_objects)
            
            if id_mapping:
                self.viewer.handler.id_manager.update_all_mappings(id_mapping, nested_objects)
        
            self._remove_from_object_table(go.id)
            self.scn.gameobjects.remove(go)

        for prefab_id in sorted(prefabs_to_delete, reverse=True):
            self._delete_prefab_for_object(prefab_id)
        return True
    
    def _delete_all_components_of_gameobject(self, gameobject):
        """Delete all components attached to a GameObject"""
        if gameobject.component_count <= 0:
            return
            
        #print(f"GameObject {gameobject.id} has {gameobject.component_count} components")
        
        if gameobject.id >= len(self.scn.object_table):
            print(f"Warning: GameObject ID {gameobject.id} is out of bounds, cannot delete components")
            gameobject.component_count = 0
            return
        
        initial_component_count = gameobject.component_count
        deleted_count = 0
        
        max_iterations = initial_component_count * 2
        iteration = 0
        
        while gameobject.component_count > 0 and iteration < max_iterations:
            iteration += 1
            
            component_object_id = gameobject.id + 1
            
            if component_object_id >= len(self.scn.object_table):
                print(f"  Warning: Component object ID {component_object_id} is out of bounds")
                gameobject.component_count = 0
                break
                
            component_instance_id = self.scn.object_table[component_object_id]
            
            if component_instance_id <= 1:
                print(f"  Skipping invalid component with instance_id={component_instance_id}")
                gameobject.component_count -= 1
                continue
                
            try:
                self.delete_component_from_gameobject(component_instance_id, gameobject.id)
                deleted_count += 1
            except Exception as e:
                print(f"  Error deleting component {component_instance_id}: {str(e)}")
        
        if deleted_count != initial_component_count:
            print(f"  Warning: Expected to delete {initial_component_count} components, but deleted {deleted_count}")
            gameobject.component_count = 0

    def _delete_prefab_for_object(self, prefab_id):
        if prefab_id == -1:
            print("  No prefab to delete (prefab_id is -1)")
            return False
            
        if prefab_id < 0 or not hasattr(self.scn, 'prefab_infos') or not self.scn.is_scn:
            print(f"  Cannot delete prefab: invalid conditions (prefab_id={prefab_id})")
            return False
        
        if prefab_id >= len(self.scn.prefab_infos):
            print(f"  Warning: Invalid prefab index {prefab_id}")
            return False
            
        prefab_to_delete = self.scn.prefab_infos[prefab_id]
        
        #print(f"  Removing prefab {prefab_id} with path: {path_str}")
        
        if prefab_to_delete in self.scn._prefab_str_map:
            del self.scn._prefab_str_map[prefab_to_delete]
            #print(f"  Removed string map entry for prefab {prefab_id}")
        else:
            print(f"  Warning: Prefab {prefab_id} not found in string map")
        
        for i, prefab in enumerate(self.scn.prefab_infos):
            if (i != prefab_id and 
                prefab.string_offset == prefab_to_delete.string_offset and
                prefab in self.scn._prefab_str_map):
                print(f"  Cleaning up duplicate prefab string reference at index {i}")
                del self.scn._prefab_str_map[prefab]

        self.scn.prefab_infos = [e for e in self.scn.prefab_infos if e != prefab_to_delete]
        #print(f"  Prefab {prefab_id} removed from prefab_infos array")
        
        print("deleting prefab number:", prefab_id)
        for go in self.scn.gameobjects:
            if go.prefab_id > prefab_id:
                go.prefab_id -= 1
    
        #print(f"  Updated {updated_count} objects referencing prefabs")

        return True

    def _update_instance_hierarchy(self, instance_id, parent_instance_id=None):
        """
        Update instance hierarchy with parent-child relationship
        
        Args:
            instance_id: The instance ID to update
            parent_instance_id: Optional parent instance ID
        """
        if instance_id not in self.scn.instance_hierarchy:
            self.scn.instance_hierarchy[instance_id] = {"children": [], "parent": None}
        
        current_parent = self.scn.instance_hierarchy[instance_id].get("parent")
        
        if current_parent is not None and current_parent != parent_instance_id:
            if current_parent in self.scn.instance_hierarchy:
                if instance_id in self.scn.instance_hierarchy[current_parent]["children"]:
                    self.scn.instance_hierarchy[current_parent]["children"].remove(instance_id)
        
        self.scn.instance_hierarchy[instance_id]["parent"] = parent_instance_id
        
        if parent_instance_id is not None:
            if parent_instance_id not in self.scn.instance_hierarchy:
                self.scn.instance_hierarchy[parent_instance_id] = {"children": [], "parent": None}
                
            if "children" not in self.scn.instance_hierarchy[parent_instance_id]:
                self.scn.instance_hierarchy[parent_instance_id]["children"] = []
                
            if instance_id not in self.scn.instance_hierarchy[parent_instance_id]["children"]:
                self.scn.instance_hierarchy[parent_instance_id]["children"].append(instance_id)

    def _analyze_instance_fields_for_nested_objects(self, temp_elements, type_info, nested_objects, parent_id, visited_types=None, userdata_fields=None):
        if visited_types is None:
            visited_types = set()
        
        if not type_info or "fields" not in type_info:
            return {}
            
        type_name = type_info.get("name", "")
        if not type_name:
            return {}
            
        visited_types.add(type_name)
        
        fields_dict = {}
        for field_def in type_info.get("fields", []):
            field_name = field_def.get("name", "")
            if not field_name:
                continue
                
            field_type = field_def.get("type", "unknown").lower()
            field_size = field_def.get("size", 4)
            field_native = field_def.get("native", False)
            field_array = field_def.get("array", False)
            field_align = field_def.get("align", 4)
            field_orig_type = field_def.get("original_type", "")
            
            field_class = get_type_class(field_type, field_size, field_native, field_array, field_align, field_orig_type, field_name)
            field_obj = self.viewer._create_default_field(field_class, field_orig_type, field_array, field_size)
            
            if field_obj:
                temp_elements[field_name] = field_obj
                fields_dict[field_name] = field_obj
                
                if userdata_fields is not None and isinstance(field_obj, UserDataData) and field_orig_type:
                    userdata_fields.append((field_name, field_orig_type, field_array))
                elif field_array and field_class == UserDataData and field_orig_type and userdata_fields is not None:
                    userdata_fields.append((field_name, field_orig_type, field_array))
                
                if not field_array and isinstance(field_obj, ObjectData) and field_orig_type:
                    if field_orig_type in visited_types:
                        continue
                        
                    nested_type_info, nested_type_id = self.type_registry.find_type_by_name(field_orig_type)
                    
                    if not nested_type_info or not nested_type_id or nested_type_id == 0:
                        continue
                        
                    nested_objects.append((nested_type_info, nested_type_id))
                    
                    nested_temp = {}
                    self._analyze_instance_fields_for_nested_objects(
                        nested_temp,
                        nested_type_info,
                        nested_objects,
                        parent_id,
                        visited_types.copy(),
                        userdata_fields
                    )
        
        return fields_dict

    def _update_object_references(self, target_fields, temp_fields, main_instance_index, nested_objects):
        offset_start = main_instance_index - len(nested_objects)
        
        if offset_start < 0:
            return
        
        type_to_indices = {}
        for i, (nested_type_info, _) in enumerate(nested_objects):
            type_name = nested_type_info.get("name", "")
            if type_name not in type_to_indices:
                type_to_indices[type_name] = []
            type_to_indices[type_name].append(i)
        
        for field_name, field_data in temp_fields.items():
            if field_name not in target_fields:
                continue
                
            if isinstance(field_data, ObjectData) and field_data.value == 0:
                type_name = field_data.orig_type
                if type_name in type_to_indices and type_to_indices[type_name]:
                    # Pop the first available index for this type
                    nested_index = type_to_indices[type_name].pop(0)
                    target_fields[field_name].value = offset_start + nested_index
                    
            elif isinstance(field_data, ArrayData) and field_data.element_class == ObjectData:
                array_data = target_fields[field_name]
                array_data.values = []
                
                for element in field_data.values:
                    if isinstance(element, ObjectData):
                        new_obj = ObjectData(element.value, element.orig_type)
                        
                        if element.value == 0 and element.orig_type:
                            type_name = element.orig_type
                            if type_name in type_to_indices and type_to_indices[type_name]:
                                nested_index = type_to_indices[type_name].pop(0)
                                new_obj.value = offset_start + nested_index
                        array_data.values.append(new_obj)
                    else:
                        array_data.values.append(element)

    def _insert_into_object_table(self, object_table_index, instance_id):
        if object_table_index >= len(self.scn.object_table):
            self.scn.object_table.extend(
                [0] * (object_table_index - len(self.scn.object_table) + 1)
            )
            self.scn.object_table[object_table_index] = instance_id
        else:
            self.scn.object_table.insert(object_table_index, instance_id)
        for go in self.scn.gameobjects:
            if go.id >= object_table_index:
                go.id += 1
            if go.parent_id >= object_table_index:
                go.parent_id += 1
        for folder in self.scn.folder_infos:
            if folder.id >= object_table_index:
                folder.id += 1
            if folder.parent_id >= object_table_index:
                folder.parent_id += 1
        if self.scn.is_pfb:
            for ref_info in self.scn.gameobject_ref_infos:
                if ref_info.object_id >= object_table_index:
                    ref_info.object_id += 1
                if ref_info.target_id >= object_table_index:
                    ref_info.target_id += 1

    def _calculate_component_insertion_index(self, gameobject):
        """Calculate the best insertion index for a new component"""
        existing_component_instance_ids = []
        for comp_offset in range(1, gameobject.component_count + 1):
            comp_object_id = gameobject.id + comp_offset
            if comp_object_id < len(self.scn.object_table):
                inst_id = self.scn.object_table[comp_object_id]
                if inst_id > 0:
                    existing_component_instance_ids.append(inst_id)

        if existing_component_instance_ids:
            return max(max(existing_component_instance_ids) + 1, len(self.scn.instance_infos))

        go_instance_id = self.scn.object_table[gameobject.id]
        if go_instance_id > 0:
            return max(go_instance_id + 1, len(self.scn.instance_infos))

        # Fallback: append at end
        return len(self.scn.instance_infos)
    
    def _remove_from_object_table(self, object_table_index):
        _ = self.scn.object_table[object_table_index]
            
        self.scn.object_table.pop(object_table_index)
        
        for go in self.scn.gameobjects:
            if go.id > object_table_index:
                go.id -= 1
            if go.parent_id > object_table_index:
                go.parent_id -= 1
                
        for folder in self.scn.folder_infos:
            if folder.id > object_table_index:
                folder.id -= 1
            if folder.parent_id > object_table_index:
                folder.parent_id -= 1
                
        if self.scn.is_pfb:
            for ref_info in self.scn.gameobject_ref_infos:
                if ref_info.object_id > object_table_index:
                    ref_info.object_id -= 1
                if ref_info.target_id > object_table_index:
                    ref_info.target_id -= 1


    def delete_folder(self, folder_id):
        """Delete a folder with the given ID and all its contents"""
        target_folder = None
        for folder in self.scn.folder_infos:
            if folder.id == folder_id:
                target_folder = folder
                break

        folder_objects = {f.id: f for f in self.scn.folder_infos}
        
        folder_child_map = {}
        for folder in self.scn.folder_infos:
            if folder.parent_id in folder_objects:
                parent_folder = folder_objects[folder.parent_id]
                if parent_folder not in folder_child_map:
                    folder_child_map[parent_folder] = []
                folder_child_map[parent_folder].append(folder)

        folders_to_delete = []
        
        def collect_folders_recursive(folder):
            folders_to_delete.append(folder)
            if folder in folder_child_map:
                for child in folder_child_map[folder]:
                    collect_folders_recursive(child)
        
        collect_folders_recursive(target_folder)
        
        #print(f"Found {len(folders_to_delete)} folders to delete")
        
        folder_ids = {f.id for f in folders_to_delete}
        
        gameobjects_to_delete = []
        for go in self.scn.gameobjects:
            if go.parent_id in folder_ids:
                gameobjects_to_delete.append(go)
        
        #print(f"Found {len(gameobjects_to_delete)} GameObjects to delete")
        
        for go in reversed(gameobjects_to_delete):
            self._delete_gameobject_directly(go)

        for folder in reversed(folders_to_delete):
            if folder is target_folder:
                continue
                
            print(f"  Deleting subfolder with ID {folder.id}")
            folder_instance_id = self.scn.object_table[folder.id] if folder.id < len(self.scn.object_table) else 0
            
            if folder_instance_id > 0:
                nested_objects = RszInstanceOperations.find_nested_objects(
                    self.scn.parsed_elements, folder_instance_id, self.scn.object_table
                )
                nested_objects.add(folder_instance_id)
                
                for inst_id in sorted(nested_objects, reverse=True):
                    self.viewer._remove_instance_references(inst_id)
                
                id_mapping = self.viewer._update_instance_references_after_deletion(folder_instance_id, nested_objects)
                
                if id_mapping:
                    self.viewer.handler.id_manager.update_all_mappings(id_mapping, nested_objects)
            
            if folder.id < len(self.scn.object_table):
                self._remove_from_object_table(folder.id)
            
            if folder in self.scn.folder_infos:
                self.scn.folder_infos.remove(folder)

        print(f"  Deleting target folder with ID {target_folder.id}")
        folder_instance_id = self.scn.object_table[target_folder.id] if target_folder.id < len(self.scn.object_table) else 0
        
        if folder_instance_id > 0:
            nested_objects = RszInstanceOperations.find_nested_objects(
                self.scn.parsed_elements, folder_instance_id, self.scn.object_table
            )
            nested_objects.add(folder_instance_id)
            
            for inst_id in sorted(nested_objects, reverse=True):
                self.viewer._remove_instance_references(inst_id)
            
            id_mapping = self.viewer._update_instance_references_after_deletion(folder_instance_id, nested_objects)
            
            if id_mapping:
                self.viewer.handler.id_manager.update_all_mappings(id_mapping, nested_objects)
        
        if target_folder.id < len(self.scn.object_table):
            self._remove_from_object_table(target_folder.id)
        
        if target_folder in self.scn.folder_infos:
            self.scn.folder_infos.remove(target_folder)
        
        self.viewer.mark_modified()
        return True

    def create_component_for_gameobject(self, gameobject_instance_id, component_type):
        """Create a new component on the specified GameObject"""
        # Find the target GameObject
        target_go = next(
            (go for go in self.scn.gameobjects
             if self.scn.object_table[go.id] == gameobject_instance_id),
            None
        )
        if not target_go:
            return {"success": False, "error": "GameObject not found"}

        type_info, type_id = self.type_registry.find_type_by_name(component_type)
        if not type_info or not type_id:
            return {"success": False, "error": "Component type not found"}

        object_table_insertion_index = target_go.id + target_go.component_count + 1

        next_insertion_index = self._calculate_component_insertion_index(target_go)

        component_fields = {}
        self.viewer._initialize_fields_from_type_info(component_fields, type_info)

        def _create_instance_for_type(current_type_info):
            nonlocal next_insertion_index

            instance_fields = {}
            self.viewer._initialize_fields_from_type_info(instance_fields, current_type_info)

            for field_def in current_type_info.get("fields", []):
                field_name = field_def.get("name", "")
                if not field_name:
                    continue
                field_obj = instance_fields.get(field_name)
                if field_obj is None:
                    continue

                if isinstance(field_obj, UserDataData) and getattr(field_obj, 'orig_type', ''):
                    if hasattr(self.scn, 'has_embedded_rsz') and self.scn.has_embedded_rsz:
                        userdata_id = self._create_userdata_instance_for_field(field_obj.orig_type, next_insertion_index, None)
                        if userdata_id is not None:
                            field_obj.value = userdata_id
                            field_obj.string = field_obj.orig_type
                            next_insertion_index += 1

                elif isinstance(field_obj, ArrayData) and getattr(field_obj, 'element_class', None) == UserDataData and getattr(field_obj, 'orig_type', ''):
                    field_obj._needs_userdata_creation = True
                    field_obj._userdata_type = field_obj.orig_type

                elif isinstance(field_obj, ObjectData) and getattr(field_obj, 'orig_type', ''):
                    child_type_info, child_type_id = self.type_registry.find_type_by_name(field_obj.orig_type)
                    if child_type_info and child_type_id:
                        child_id = _create_instance_for_type(child_type_info)
                        field_obj.value = child_id

            _, cur_type_id = self.type_registry.find_type_by_name(current_type_info.get("name", ""))
            if not cur_type_id:
                return 0
            new_inst = self.viewer._initialize_new_instance(cur_type_id, current_type_info)
            if not new_inst:
                return 0

            self.viewer._insert_instance_and_update_references(next_insertion_index, new_inst)
            self.viewer.handler.id_manager.register_instance(next_insertion_index)
            this_id = next_insertion_index
            self.scn.parsed_elements[this_id] = instance_fields
            next_insertion_index += 1
            return this_id

        for field_def in type_info.get("fields", []):
            field_name = field_def.get("name", "")
            if not field_name:
                continue
            field_obj = component_fields.get(field_name)
            if field_obj is None:
                continue

            if isinstance(field_obj, UserDataData) and getattr(field_obj, 'orig_type', ''):
                if hasattr(self.scn, 'has_embedded_rsz') and self.scn.has_embedded_rsz:
                    userdata_id = self._create_userdata_instance_for_field(field_obj.orig_type, next_insertion_index, None)
                    if userdata_id is not None:
                        field_obj.value = userdata_id
                        field_obj.string = field_obj.orig_type
                        next_insertion_index += 1

            elif isinstance(field_obj, ArrayData) and getattr(field_obj, 'element_class', None) == UserDataData and getattr(field_obj, 'orig_type', ''):
                field_obj._needs_userdata_creation = True
                field_obj._userdata_type = field_obj.orig_type

            elif isinstance(field_obj, ObjectData) and getattr(field_obj, 'orig_type', ''):
                nested_type_info, nested_type_id = self.type_registry.find_type_by_name(field_obj.orig_type)
                if nested_type_info and nested_type_id:
                    child_id = _create_instance_for_type(nested_type_info)
                    field_obj.value = child_id

        new_instance = self.viewer._initialize_new_instance(type_id, type_info)
        if not new_instance:
            return {"success": False, "error": f"Failed to initialize component instance for {component_type}"}

        self.viewer._insert_instance_and_update_references(next_insertion_index, new_instance)
        component_instance_id = next_insertion_index
        component_reasy_id = self.viewer.handler.id_manager.register_instance(component_instance_id)

        if "parent" in component_fields and hasattr(component_fields["parent"], "value"):
            component_fields["parent"].value = gameobject_instance_id

        self.scn.parsed_elements[component_instance_id] = component_fields

        target_go.component_count += 1
        self._insert_into_object_table(object_table_insertion_index, component_instance_id)

        self.viewer.mark_modified()

        return {
            "success": True,
            "instance_id": component_instance_id,
            "reasy_id": component_reasy_id,
            "type_name": component_type,
            "go_id": target_go.id
        }

    def delete_component_from_gameobject(self, component_instance_id, owner_go_id=None):
        """Delete a component from its GameObject"""
        if component_instance_id <= 0 or component_instance_id >= len(self.scn.instance_infos):
            raise ValueError(f"Invalid component instance ID: {component_instance_id}")
        
        object_table_index = -1
        owner_go = None
    
        for i, instance_id in enumerate(self.scn.object_table):
            if instance_id == component_instance_id:
                object_table_index = i
                break

        if object_table_index < 0:
            raise ValueError(f"Component {component_instance_id} not found in object table")
        
        for go in self.scn.gameobjects:
            if go.id < object_table_index and object_table_index <= go.id + go.component_count:
                owner_go = go
                break
        
        if not owner_go:
            raise ValueError(f"Could not find GameObject owning component {component_instance_id}")
        
        original_component_count = owner_go.component_count
        
        nested_objects = RszInstanceOperations.find_nested_objects(
            self.scn.parsed_elements, component_instance_id, self.scn.object_table, is_component_deletion=True
        )
        nested_objects.add(component_instance_id)
        
        to_delete_instances = sorted(nested_objects, reverse=True)
           
        # Find and collect additional UserData instances referenced by the instances being deleted
        const_to_delete_instances = to_delete_instances.copy()
        for instance_id in const_to_delete_instances:
            for field_name, field in self.scn.parsed_elements.get(instance_id, {}).items():
                if isinstance(field, UserDataData) and field.value not in to_delete_instances and field.value:
                    to_delete_instances.append(field.value)
                elif isinstance(field, ArrayData):
                    for element in field.values:
                        if isinstance(element, UserDataData) and element.value not in to_delete_instances and element.value:
                            to_delete_instances.append(element.value)
        
        userdata_reference_map = {}
        for key, instance in self.scn.parsed_elements.items():
            if key in to_delete_instances:
                continue  # Skip instances that are already being deleted
                
            for field in instance.values():
                if isinstance(field, UserDataData) and field.value in to_delete_instances:
                    if field.value not in userdata_reference_map:
                        userdata_reference_map[field.value] = 0
                    userdata_reference_map[field.value] += 1
                elif isinstance(field, ArrayData):
                    for element in field.values:
                        if isinstance(element, UserDataData) and element.value in to_delete_instances:
                            if element.value not in userdata_reference_map:
                                userdata_reference_map[element.value] = 0
                            userdata_reference_map[element.value] += 1
        
        const_to_delete_instances = to_delete_instances.copy()
        to_delete_instances = []
        for instance_id in const_to_delete_instances:
            if instance_id in userdata_reference_map and userdata_reference_map[instance_id] > 0:
                #print(f"  Excluding UserData instance {instance_id} from deletion as it's referenced {userdata_reference_map[instance_id]} times elsewhere")
                continue
            to_delete_instances.append(instance_id)

        owner_go.component_count -= 1
        
        self.scn.object_table.pop(object_table_index)
        
        for go in self.scn.gameobjects:
            if go.id > object_table_index:
                go.id -= 1
            if go.parent_id > object_table_index:
                go.parent_id -= 1
                
        for folder in self.scn.folder_infos:
            if folder.id > object_table_index:
                folder.id -= 1
            if folder.parent_id > object_table_index:
                folder.parent_id -= 1
                
        if self.scn.is_pfb:
            for ref_info in self.scn.gameobject_ref_infos:
                if hasattr(ref_info, 'object_id') and ref_info.object_id > object_table_index:
                    ref_info.object_id -= 1
                if hasattr(ref_info, 'target_id') and ref_info.target_id > object_table_index:
                    ref_info.target_id -= 1
        
        for instance_id in to_delete_instances:
            self.viewer._remove_instance_references(instance_id)
        
        all_deleted = set(to_delete_instances)
        id_mapping = self.viewer._update_instance_references_after_deletion(component_instance_id, all_deleted)
        deleted_instance_ids = set(to_delete_instances)
        self.viewer.handler.id_manager.update_all_mappings(id_mapping, deleted_instance_ids)
        
        expected_component_indices = original_component_count - 1
        actual_component_indices = 0
        for i in range(1, expected_component_indices + 1):
            comp_object_id = owner_go.id + i
            if comp_object_id < len(self.scn.object_table) and self.scn.object_table[comp_object_id] > 0:
                actual_component_indices += 1
                
        if actual_component_indices != expected_component_indices:
            print(f"Warning: Component count mismatch after deletion - expected {expected_component_indices}, got {actual_component_indices}")
        
        self.viewer.mark_modified()
        
        return True
    
    def manage_gameobject_prefab(self, gameobject, new_prefab_path):
        """Create or modify a prefab association for a GameObject
        
        Args:
            gameobject_id: The GameObject's ID in the object table
            new_prefab_path: Path string for the prefab
            
        Returns:
            bool: True if the operation was successful
        """
        used_elsewhere = False
        for go in self.scn.gameobjects:
            if go.prefab_id == gameobject.prefab_id:
                used_elsewhere = True
                
        
        if gameobject.prefab_id < 0 or used_elsewhere:
            if not hasattr(self.scn, 'prefab_infos'):
                print("No prefab_infos array in scene")
                return False
                
            from file_handlers.rsz.rsz_file import RszPrefabInfo
            new_prefab = RszPrefabInfo()
            
            prefab_id = len(self.scn.prefab_infos)
            
            new_prefab.string_offset = 0
            self.scn.prefab_infos.append(new_prefab)
            gameobject.prefab_id = prefab_id
            
            if hasattr(self.scn, '_prefab_str_map'):
                self.scn._prefab_str_map[new_prefab] = new_prefab_path
                
            print(f"Created new prefab (ID: {prefab_id}) for GameObject {gameobject.id} with path: {new_prefab_path}")
            
        else:
            prefab_id = gameobject.prefab_id
            
            if prefab_id >= len(self.scn.prefab_infos):
                print(f"Invalid prefab ID {prefab_id}")
                return False
                
            prefab = self.scn.prefab_infos[prefab_id]
            
            if hasattr(self.scn, '_prefab_str_map'):
                self.scn._prefab_str_map[prefab] = new_prefab_path
                print(f"Updated prefab {prefab_id} path to: {new_prefab_path}")
            else:
                print("No prefab string map available")
                return False
        
        self.viewer.mark_modified()
        return True

    def _process_userdata_fields_in_component(self, component_fields, type_info):
        """
        Process UserDataData fields in a newly created component for SCN.18/19 files.
        Creates RSZUserDataInfo entries for each UserDataData field.
        """
        for field_name, field_data in component_fields.items():
            if isinstance(field_data, UserDataData) and field_data.value == 0:
                field_type = None
                for field_def in type_info.get("fields", []):
                    if field_def.get("name") == field_name:
                        field_type = field_def.get("original_type", "")
                        break
                
                if field_type:
                    self._create_userdata_info_for_field(field_data, field_type)
                    
            elif isinstance(field_data, ArrayData) and hasattr(field_data, 'values'):
                element_type = getattr(field_data, 'orig_type', '')
                for element in field_data.values:
                    if isinstance(element, UserDataData) and element.value == 0 and element_type:
                        self._create_userdata_info_for_field(element, element_type)
    
    def _create_userdata_instance_for_field(self, field_type, current_insertion_index, userdata_string=None):
        """
        Create a UserDataData instance for a field.
        - If the current RSZ file format supports embedded RSZ (SCN.19 style), create a
          Scn19RSZUserDataInfo entry with embedded structures.
        - Otherwise, create a standard RSZUserDataInfo (16-byte) entry.
        This is called BEFORE the component instance is created to ensure proper ordering.
        Returns the instance ID of the created UserDataData instance.
        """
        from file_handlers.rsz.scn_19.scn_19_structure import Scn19RSZUserDataInfo

        type_info, type_id = self.type_registry.find_type_by_name(field_type)
        if not type_info:
            print(f"Warning: Type not found for UserDataData field: {field_type}")
            return None
        instance_info = self._create_instance_info(
            type_id,
            int(type_info.get("crc", "0"), 16)
        )
        self.viewer._insert_instance_and_update_references(current_insertion_index, instance_info)

        self.scn.parsed_elements[current_insertion_index] = {}
        
        if not hasattr(self.scn, 'instance_hierarchy'):
            self.scn.instance_hierarchy = {}
        self.scn.instance_hierarchy[current_insertion_index] = {"children": [], "parent": None}
        
        self.viewer.handler.id_manager.register_instance(current_insertion_index)
        if self.scn.has_embedded_rsz:
            userdata_info = Scn19RSZUserDataInfo()
            userdata_info.instance_id = current_insertion_index
            userdata_info.type_id = type_id
            userdata_info.crc = int(type_info.get("crc", "0"), 16)
            userdata_info.name = field_type
            userdata_info.value = field_type
            userdata_info.data = b""
            userdata_info.data_size = 0
    
            userdata_info.embedded_rsz_header = type(self.scn.rsz_header)()
            copy_embedded_rsz_header(self.scn.rsz_header, userdata_info.embedded_rsz_header)
            userdata_info.embedded_instances = {}
            userdata_info.embedded_instance_infos = []
            userdata_info.embedded_userdata_infos = []
            userdata_info.embedded_object_table = []
            userdata_info.parsed_elements = {}
        
            userdata_info.id_manager = EmbeddedIdManager(current_insertion_index)
            userdata_info._rsz_userdata_dict = {}
            userdata_info._rsz_userdata_set = set()
            userdata_info._rsz_userdata_str_map = {}
            userdata_info.embedded_instance_hierarchy = {}
            userdata_info._array_counters = {}
            userdata_info.modified = False
        
            def mark_modified_func():
                userdata_info.modified = True
                self.viewer.mark_modified()
            userdata_info.mark_modified = mark_modified_func
        
            self._create_embedded_instance_with_nested_objects(
                userdata_info, type_info, type_id, field_type
            )
        else:
            from file_handlers.rsz.rsz_file import RszRSZUserDataInfo

            userdata_info = RszRSZUserDataInfo()
            userdata_info.instance_id = current_insertion_index
            
            final_string = userdata_string if (userdata_string is not None) else field_type
            
            userdata_info.string_offset = 0

        if not hasattr(self.scn, 'rsz_userdata_infos'):
            self.scn.rsz_userdata_infos = []
        self.scn.rsz_userdata_infos.append(userdata_info)
        if hasattr(self.scn, '_rsz_userdata_dict'):
            self.scn._rsz_userdata_dict[current_insertion_index] = userdata_info
        if hasattr(self.scn, '_rsz_userdata_set'):
            self.scn._rsz_userdata_set.add(current_insertion_index)
        if hasattr(self.scn, '_rsz_userdata_str_map'):
            final_string = userdata_string if (userdata_string is not None) else field_type
            self.scn._rsz_userdata_str_map[userdata_info] = final_string
        
        self.scn.rsz_header.userdata_count = len(self.scn.rsz_userdata_infos)
        self.scn.rsz_header.instance_count = len(self.scn.instance_infos)
        
        return current_insertion_index

    def _create_userdata_info_for_field(self, userdata_field, field_type):
        """
        Create an RSZUserDataInfo entry for a UserDataData field.
        This is used for SCN.18/19 files where UserDataData contains embedded RSZ data.
        """
        from file_handlers.rsz.scn_19.scn_19_structure import Scn19RSZUserDataInfo
        
        next_id = len(self.scn.instance_infos) if hasattr(self.scn, 'instance_infos') else 0

        type_info, type_id = self.type_registry.find_type_by_name(field_type)
        if not type_info:
            print(f"Warning: Type not found for UserDataData field: {field_type}")
            return
        
        instance_info = self._create_instance_info(type_id, 
                                                    int(type_info.get("crc", "0"), 16))
        
        if hasattr(self.scn, 'instance_infos'):
            self.scn.instance_infos.append(instance_info)
        else:
            self.scn.instance_infos = [instance_info]
        
        self.scn.parsed_elements[next_id] = {}
        
        if not hasattr(self.scn, 'instance_hierarchy'):
            self.scn.instance_hierarchy = {}
        self.scn.instance_hierarchy[next_id] = {"children": [], "parent": None}
        
        self.viewer.handler.id_manager.register_instance(next_id)
        
        userdata_info = Scn19RSZUserDataInfo()
        userdata_info.instance_id = next_id
        userdata_info.type_id = type_id
        userdata_info.crc = int(type_info.get("crc", "0"), 16)
        userdata_info.name = field_type
        userdata_info.value = field_type
        userdata_info.data = b""
        userdata_info.data_size = 0
    
        userdata_info.embedded_rsz_header = type(self.scn.rsz_header)()
        copy_embedded_rsz_header(self.scn.rsz_header, userdata_info.embedded_rsz_header)
        
        userdata_info.embedded_instances = {}
        userdata_info.embedded_instance_infos = []
        userdata_info.embedded_userdata_infos = []
        userdata_info.embedded_object_table = []
        userdata_info.parsed_elements = {}
        
        userdata_info.id_manager = EmbeddedIdManager(next_id)
        
        userdata_info._rsz_userdata_dict = {}
        userdata_info._rsz_userdata_set = set()
        userdata_info._rsz_userdata_str_map = {}
        userdata_info.embedded_instance_hierarchy = {}
        userdata_info._array_counters = {}
        
        userdata_info.modified = False
        
        def mark_modified_func():
            userdata_info.modified = True
            self.viewer.mark_modified()
        
        userdata_info.mark_modified = mark_modified_func
        
        if type_info:
            self._create_embedded_instance_with_nested_objects(
                userdata_info, type_info, type_id, field_type
            )
        
        if not hasattr(self.scn, 'rsz_userdata_infos'):
            self.scn.rsz_userdata_infos = []
        self.scn.rsz_userdata_infos.append(userdata_info)
        
        if hasattr(self.scn, '_rsz_userdata_dict'):
            self.scn._rsz_userdata_dict[next_id] = userdata_info
        if hasattr(self.scn, '_rsz_userdata_set'):
            self.scn._rsz_userdata_set.add(next_id)
        if hasattr(self.scn, '_rsz_userdata_str_map'):
            self.scn._rsz_userdata_str_map[userdata_info] = field_type
        
        userdata_field.value = next_id
        userdata_field.string = field_type
        
        self.scn.rsz_header.userdata_count = len(self.scn.rsz_userdata_infos)
        self.scn.rsz_header.instance_count = len(self.scn.instance_infos)
        
    def _create_embedded_instance_with_nested_objects(self, userdata_info, type_info, type_id, type_name):
        """
        Create an embedded instance with all its nested objects properly instantiated.
        This follows the same pattern as create_component_for_gameobject to ensure proper field ordering.
        """
        if not hasattr(userdata_info, 'embedded_instance_infos'):
            userdata_info.embedded_instance_infos = []
        if not hasattr(userdata_info, 'embedded_instances'):
            userdata_info.embedded_instances = {}
        if not hasattr(userdata_info, 'embedded_instance_hierarchy'):
            userdata_info.embedded_instance_hierarchy = {}
            
        null_info = self._create_instance_info(0, 0)
        userdata_info.embedded_instance_infos = [null_info]
        
        main_instance_fields = {}
        self.viewer._initialize_fields_from_type_info(main_instance_fields, type_info, userdata_info, 1)
        
        instance_insertion_index = 1
        nested_instances = [] 
        
        for field_def in type_info.get("fields", []):
            field_name = field_def.get("name", "")
            if not field_name or field_name not in main_instance_fields:
                continue
                
            field_obj = main_instance_fields[field_name]
            field_orig_type = field_def.get("original_type", "")
            field_array = field_def.get("array", False)
            
            if not field_array and isinstance(field_obj, ObjectData) and field_orig_type:
                nested_type_info, nested_type_id = self.type_registry.find_type_by_name(field_orig_type)
                
                if nested_type_info and nested_type_id and nested_type_id != 0:
                    nested_instance_id = instance_insertion_index
                    field_obj.value = nested_instance_id
                    
                    nested_instances.append((nested_type_info, nested_type_id, nested_instance_id, None))
                    instance_insertion_index += 1
            
            elif not field_array and isinstance(field_obj, UserDataData) and field_orig_type:
                nested_userdata = self._create_embedded_userdata_in_context(
                    field_orig_type, userdata_info, instance_insertion_index
                )
                if nested_userdata:
                    main_instance_fields[field_name] = nested_userdata
                    instance_insertion_index += 1
                    
            elif field_array and isinstance(field_obj, ArrayData):
                field_obj._owning_context = userdata_info
                field_obj._owning_instance_id = instance_insertion_index  # Will be updated when main instance is created
                field_obj._owning_field = field_name
                field_obj._container_context = userdata_info
                field_obj._container_parent_id = instance_insertion_index  # Will be updated when main instance is created
                field_obj._container_field = field_name
                
                if hasattr(field_obj, 'element_class') and field_obj.element_class == UserDataData:
                    element_type = getattr(field_obj, 'orig_type', '')
                    if element_type:
                        for idx, element in enumerate(field_obj.values):
                            if isinstance(element, UserDataData) and element.value == 0:
                                new_userdata = self._create_embedded_userdata_in_context(
                                    element_type, userdata_info, instance_insertion_index
                                )
                                if new_userdata:
                                    field_obj.values[idx] = new_userdata
                                    instance_insertion_index += 1
        
        while nested_instances:
            current_type_info, current_type_id, current_instance_id, parent_id = nested_instances.pop(0)
            
            instance_info = self._create_instance_info(current_type_id, 
                                                      int(current_type_info.get("crc", "0"), 16))
            
            while len(userdata_info.embedded_instance_infos) <= current_instance_id:
                dummy_info = self._create_instance_info(0, 0)
                userdata_info.embedded_instance_infos.append(dummy_info)
            userdata_info.embedded_instance_infos[current_instance_id] = instance_info
            
            if hasattr(userdata_info, 'id_manager') and userdata_info.id_manager:
                userdata_info.id_manager.register_instance(current_instance_id)
            
            instance_fields = {}
            self.viewer._initialize_fields_from_type_info(instance_fields, current_type_info, userdata_info, 1)
            
            for field_def in current_type_info.get("fields", []):
                field_name = field_def.get("name", "")
                if not field_name or field_name not in instance_fields:
                    continue
                    
                field_obj = instance_fields[field_name]
                field_orig_type = field_def.get("original_type", "")
                field_array = field_def.get("array", False)
                
                if not field_array and isinstance(field_obj, ObjectData) and field_orig_type:
                    nested_type_info, nested_type_id = self.type_registry.find_type_by_name(field_orig_type)
                    
                    if nested_type_info and nested_type_id and nested_type_id != 0:
                        nested_instance_id = instance_insertion_index
                        field_obj.value = nested_instance_id
                        
                        nested_instances.append((nested_type_info, nested_type_id, nested_instance_id, current_instance_id))
                        instance_insertion_index += 1
                
                elif not field_array and isinstance(field_obj, UserDataData) and field_orig_type:
                    nested_userdata = self._create_embedded_userdata_in_context(
                        field_orig_type, userdata_info, instance_insertion_index
                    )
                    if nested_userdata:
                        instance_fields[field_name] = nested_userdata
                        instance_insertion_index += 1
                        
                elif field_array and isinstance(field_obj, ArrayData):
                    field_obj._owning_context = userdata_info
                    field_obj._owning_instance_id = current_instance_id
                    field_obj._owning_field = field_name
                    field_obj._container_context = userdata_info
                    field_obj._container_parent_id = current_instance_id
                    field_obj._container_field = field_name
                    
                    if hasattr(field_obj, 'element_class') and field_obj.element_class == UserDataData:
                        element_type = getattr(field_obj, 'orig_type', '')
                        if element_type:
                            for idx, element in enumerate(field_obj.values):
                                if isinstance(element, UserDataData) and element.value == 0:
                                    new_userdata = self._create_embedded_userdata_in_context(
                                        element_type, userdata_info, instance_insertion_index
                                    )
                                    if new_userdata:
                                        field_obj.values[idx] = new_userdata
                                        instance_insertion_index += 1
            
            userdata_info.embedded_instances[current_instance_id] = instance_fields
            
            children = []
            for field_name, field_obj in instance_fields.items():
                if isinstance(field_obj, ObjectData) and field_obj.value > 0:
                    children.append(field_obj.value)
                elif isinstance(field_obj, UserDataData) and field_obj.value > 0:
                    children.append(field_obj.value)
                    
            userdata_info.embedded_instance_hierarchy[current_instance_id] = {
                "children": children,
                "parent": parent_id
            }
        
        main_instance_id = instance_insertion_index
        main_instance_info = self._create_instance_info(type_id, 
                                                       int(type_info.get("crc", "0"), 16))
        
        while len(userdata_info.embedded_instance_infos) <= main_instance_id:
            dummy_info = self._create_instance_info(0, 0)
            userdata_info.embedded_instance_infos.append(dummy_info)
        userdata_info.embedded_instance_infos[main_instance_id] = main_instance_info
        
        if hasattr(userdata_info, 'id_manager') and userdata_info.id_manager:
            userdata_info.id_manager.register_instance(main_instance_id)
        
        for field_name, field_obj in main_instance_fields.items():
            if isinstance(field_obj, ArrayData):
                field_obj._owning_instance_id = main_instance_id
                field_obj._container_parent_id = main_instance_id
        
        userdata_info.embedded_instances[main_instance_id] = main_instance_fields
        
        children = []
        for field_name, field_obj in main_instance_fields.items():
            if isinstance(field_obj, ObjectData) and field_obj.value > 0:
                children.append(field_obj.value)
            elif isinstance(field_obj, UserDataData) and field_obj.value > 0:
                children.append(field_obj.value)
                
        userdata_info.embedded_instance_hierarchy[main_instance_id] = {
            "children": children,
            "parent": None
        }
        
        userdata_info.embedded_object_table = [main_instance_id]
        
        userdata_info.embedded_rsz_header.object_count = 1 
        userdata_info.embedded_rsz_header.instance_count = len(userdata_info.embedded_instance_infos)
        userdata_info.embedded_rsz_header.userdata_count = len(getattr(userdata_info, 'embedded_userdata_infos', []))
    
    def _create_instance_info(self, type_id, crc):
        """Create an instance info structure"""
        class InstanceInfo:
            def __init__(self):
                self.type_id = 0
                self.crc = 0
                
        info = InstanceInfo()
        info.type_id = type_id
        info.crc = crc
        return info
    
    def _create_embedded_userdata_in_context(self, field_type, parent_rui, parent_instance_id=None, array_data=None):
        """
        Create a UserDataData with embedded RSZ within the parent's embedded context.
        This is for nested embedded RSZ - the instance and RSZUserDataInfo are created
        in the parent's embedded structures.
        
        Args:
            field_type: The type name for the UserDataData
            parent_rui: The parent RSZUserDataInfo context
            parent_instance_id: The instance ID that owns the array (for field ordering)
            array_data: The array that will contain this UserData (for field ordering)
        """
        type_info, type_id = self.type_registry.find_type_by_name(field_type)
        if not type_info:
            print(f"Warning: Type not found for nested UserDataData field: {field_type}")
            return None
        
        if not hasattr(parent_rui, 'embedded_instance_infos'):
            parent_rui.embedded_instance_infos = []
        if not hasattr(parent_rui, 'embedded_instances'):
            parent_rui.embedded_instances = {}
        
        if parent_instance_id is not None:
            next_id = parent_instance_id
            
            id_shift = {}
            for old_id in sorted(parent_rui.embedded_instances.keys()):
                if old_id >= next_id:
                    id_shift[old_id] = old_id + 1
            
            if id_shift:
                new_instances = {}
                for old_id, fields in parent_rui.embedded_instances.items():
                    new_id = id_shift.get(old_id, old_id)
                    new_instances[new_id] = fields
                parent_rui.embedded_instances = new_instances
                
                if hasattr(parent_rui, 'embedded_instance_infos'):
                    max_new_id = max(id_shift.values())
                    while len(parent_rui.embedded_instance_infos) <= max_new_id:
                        from file_handlers.rsz.utils.rsz_embedded_utils import create_embedded_instance_info
                        parent_rui.embedded_instance_infos.append(create_embedded_instance_info(0, self.type_registry))
                    
                    for old_id, new_id in sorted(id_shift.items(), reverse=True):
                        if old_id < len(parent_rui.embedded_instance_infos):
                            parent_rui.embedded_instance_infos[new_id] = parent_rui.embedded_instance_infos[old_id]
                            parent_rui.embedded_instance_infos[old_id] = None
                
                from file_handlers.rsz.utils.rsz_embedded_utils import update_embedded_references_for_shift
                update_embedded_references_for_shift(id_shift, parent_rui)
                
                if hasattr(parent_rui, 'embedded_userdata_infos'):
                    for ud in parent_rui.embedded_userdata_infos:
                        if hasattr(ud, 'instance_id') and ud.instance_id in id_shift:
                            ud.instance_id = id_shift[ud.instance_id]
                
                if hasattr(parent_rui, 'embedded_object_table'):
                    parent_rui.embedded_object_table = [
                        id_shift.get(x, x) for x in parent_rui.embedded_object_table
                    ]
        else:
            raise ValueError("Parent RUI is not valid")
        
        from file_handlers.rsz.scn_19.scn_19_structure import Scn19RSZUserDataInfo
        from file_handlers.rsz.rsz_data_types import UserDataData
        userdata_info = Scn19RSZUserDataInfo()
        userdata_info.instance_id = next_id
        userdata_info.type_id = type_id
        userdata_info.crc = int(type_info.get("crc", "0"), 16)
        userdata_info.name = field_type
        userdata_info.value = field_type
        userdata_info.parent_userdata_rui = parent_rui
        userdata_info.data = b""
        userdata_info.data_size = 0
        
        if hasattr(parent_rui, 'embedded_rsz_header'):
            if hasattr(parent_rui, 'embedded_rsz_header'):
                userdata_info.embedded_rsz_header = type(parent_rui.embedded_rsz_header)()
                copy_embedded_rsz_header(parent_rui.embedded_rsz_header, userdata_info.embedded_rsz_header)
                
                userdata_info.embedded_rsz_header.object_count = 1
                userdata_info.embedded_rsz_header.instance_count = 2
                userdata_info.embedded_rsz_header.userdata_count = 0

            userdata_info.embedded_instances = {}
            userdata_info.embedded_instance_infos = []
            userdata_info.embedded_userdata_infos = []
            userdata_info.embedded_object_table = []
            userdata_info.parsed_elements = {}
            
            from utils.id_manager import EmbeddedIdManager
            userdata_info.id_manager = EmbeddedIdManager(next_id)
            
            userdata_info._rsz_userdata_dict = {}
            userdata_info._rsz_userdata_set = set()
            userdata_info._rsz_userdata_str_map = {}
            userdata_info.embedded_instance_hierarchy = {}
            userdata_info._array_counters = {}
            userdata_info.modified = False
            
            def mark_modified_func():
                userdata_info.modified = True
                if hasattr(parent_rui, 'mark_modified'):
                    parent_rui.mark_modified()
            
            userdata_info.mark_modified = mark_modified_func
            
            if type_info:
                self._create_embedded_instance_with_nested_objects(
                    userdata_info, type_info, type_id, field_type
                )
        
        if not hasattr(parent_rui, 'embedded_userdata_infos'):
            parent_rui.embedded_userdata_infos = []
        parent_rui.embedded_userdata_infos.append(userdata_info)
        
        parent_rui.embedded_instances[next_id] = {}
        
        if not hasattr(parent_rui, 'embedded_instance_infos'):
            parent_rui.embedded_instance_infos = []
        
        while len(parent_rui.embedded_instance_infos) <= next_id:
            from file_handlers.rsz.utils.rsz_embedded_utils import create_embedded_instance_info
            null_info = create_embedded_instance_info(0, self.type_registry)
            parent_rui.embedded_instance_infos.append(null_info)
        
        from file_handlers.rsz.utils.rsz_embedded_utils import create_embedded_instance_info
        instance_info = create_embedded_instance_info(type_id, self.type_registry)
        parent_rui.embedded_instance_infos[next_id] = instance_info
        
        if hasattr(parent_rui, 'id_manager') and parent_rui.id_manager:
            parent_rui.id_manager.register_instance(next_id)
        
        if hasattr(parent_rui, '_rsz_userdata_dict'):
            parent_rui._rsz_userdata_dict[next_id] = userdata_info
        if hasattr(parent_rui, '_rsz_userdata_set'):
            parent_rui._rsz_userdata_set.add(next_id)
        if hasattr(parent_rui, '_rsz_userdata_str_map'):
            parent_rui._rsz_userdata_str_map[userdata_info] = field_type
        
        if hasattr(parent_rui, 'embedded_rsz_header'):
            parent_rui.embedded_rsz_header.instance_count = len(parent_rui.embedded_instance_infos)
            parent_rui.embedded_rsz_header.userdata_count = len(parent_rui.embedded_userdata_infos)
        
        userdata = UserDataData(next_id, "", field_type)
        userdata._container_context = parent_rui
        userdata._owning_userdata = userdata_info
            
        return userdata
