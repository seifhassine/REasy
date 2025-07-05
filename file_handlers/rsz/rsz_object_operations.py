"""
Helper class for handling object operations in RSZ files.

This file contains utility methods for:
- Creating GameObjects
- Deleting GameObjects
- Handling components 
- Managing folders
"""

import uuid
from PySide6.QtWidgets import QMessageBox
from .rsz_data_types import (
    get_type_class, ObjectData, ArrayData, 
    UserDataData, S32Data
)
from file_handlers.rsz.rsz_instance_operations import RszInstanceOperations


class RszObjectOperations:
    """Helper class for handling object creation and deletion in RSZ files"""
    
    def __init__(self, viewer):
        """Initialize with a reference to the RSZ viewer"""
        self.viewer = viewer
        self.scn = viewer.scn
        self.type_registry = viewer.type_registry
    
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
        
        if not self.scn.is_pfb and not self.scn.is_usr:
            guid_bytes = uuid.uuid4().bytes_le
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
    
    def _delete_gameobject_directly(self, gameobject):
        """Delete a GameObject and its hierarchy directly"""
        try:
            if gameobject not in self.scn.gameobjects:
                return False
            
            gameobject_id = gameobject.id
            
            if gameobject_id >= len(self.scn.object_table):
                return False
            
            def should_delete_prefab(gameobject):
                for other_go in self.scn.gameobjects:
                    if other_go != gameobject and other_go.prefab_id == gameobject.prefab_id:
                        print(f"  Skipping prefab deletion: prefab_id {gameobject.prefab_id} is still used by other GameObjects")
                        return False
                return True
            
            if not self.scn.is_pfb and not self.scn.is_usr and hasattr(gameobject, 'prefab_id'):
                if gameobject.prefab_id >= 0 and should_delete_prefab(gameobject):
                    _ = self._delete_prefab_for_object(gameobject.prefab_id)
            
            gameobject_refs_to_delete = self._collect_gameobject_hierarchy_by_reference(gameobject)
            
            if not gameobject_refs_to_delete:
                return False
                
            for go in reversed(gameobject_refs_to_delete):
                if go != gameobject and not self.scn.is_pfb and not self.scn.is_usr and hasattr(go, 'prefab_id'):
                    if go.prefab_id >= 0 and should_delete_prefab(go):
                        _ = self._delete_prefab_for_object(go.prefab_id)
                
                self._delete_all_components_of_gameobject(go)
                
                if go.id < len(self.scn.object_table):
                    go_instance_id = self.scn.object_table[go.id]
                    
                    if go_instance_id > 0:
                        print(f"  Deleting GameObject instance {go_instance_id} (object_id: {go.id})")
                    
                        nested_objects = self._find_nested_objects(go_instance_id)
                        nested_objects.add(go_instance_id)
                        
                        for instance_id in sorted(nested_objects, reverse=True):
                            self.viewer._remove_instance_references(instance_id)
                        
                        id_mapping = self.viewer._update_instance_references_after_deletion(go_instance_id, nested_objects)
                        
                        if id_mapping:
                            self.viewer.handler.id_manager.update_all_mappings(id_mapping, nested_objects)
                
                if go.id < len(self.scn.object_table):
                    self._remove_from_object_table(go.id)
                
                if go in self.scn.gameobjects:
                    self.scn.gameobjects.remove(go)
            
            return True
            
        except Exception as e:
            print(f"Error deleting GameObject: {str(e)}")
            import traceback
            traceback.print_exc()
            return False
    
    def _delete_all_components_of_gameobject(self, gameobject):
        """Delete all components attached to a GameObject"""
        if gameobject.component_count <= 0:
            return
            
        print(f"GameObject {gameobject.id} has {gameobject.component_count} components")
        
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
            
        if prefab_id < 0 or not hasattr(self.scn, 'prefab_infos') or self.scn.is_pfb or self.scn.is_usr:
            print(f"  Cannot delete prefab: invalid conditions (prefab_id={prefab_id})")
            return False
        
        if prefab_id >= len(self.scn.prefab_infos):
            print(f"  Warning: Invalid prefab index {prefab_id}")
            return False
            
        prefab_to_delete = self.scn.prefab_infos[prefab_id]
        
        path_str = ""
        if hasattr(self.scn, 'get_prefab_string'):
            path_str = self.scn.get_prefab_string(prefab_to_delete)
        
        print(f"  Removing prefab {prefab_id} with path: {path_str}")
        
        if hasattr(self.scn, '_prefab_str_map'):
            if prefab_to_delete in self.scn._prefab_str_map:
                del self.scn._prefab_str_map[prefab_to_delete]
                print(f"  Removed string map entry for prefab {prefab_id}")
            else:
                print(f"  Warning: Prefab {prefab_id} not found in string map")
            
            for i, prefab in enumerate(self.scn.prefab_infos):
                if (i != prefab_id and 
                    prefab.string_offset == prefab_to_delete.string_offset and
                    prefab.string_offset != 0 and 
                    prefab in self.scn._prefab_str_map):
                    print(f"  Cleaning up duplicate prefab string reference at index {i}")
                    del self.scn._prefab_str_map[prefab]
        
        self.scn.prefab_infos.pop(prefab_id)
        print(f"  Prefab {prefab_id} removed from prefab_infos array")
        
        updated_count = 0
        for go in self.scn.gameobjects:
            if hasattr(go, 'prefab_id'):
                if go.prefab_id == prefab_id:
                    go.prefab_id = -1
                    updated_count += 1
                elif go.prefab_id > prefab_id:
                    go.prefab_id -= 1
                    updated_count += 1
        
        for folder in self.scn.folder_infos:
            if hasattr(folder, 'prefab_id'):
                if folder.prefab_id == prefab_id:
                    folder.prefab_id = -1
                    updated_count += 1
                elif folder.prefab_id > prefab_id:
                    folder.prefab_id -= 1
                    updated_count += 1
        
        print(f"  Updated {updated_count} objects referencing prefabs")

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

    def _analyze_instance_fields_for_nested_objects(self, temp_elements, type_info, nested_objects, parent_id, visited_types=None):
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
                        visited_types.copy()
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
                if hasattr(ref_info, "object_id") and ref_info.object_id >= object_table_index:
                    ref_info.object_id += 1
                if hasattr(ref_info, "target_id") and ref_info.target_id >= object_table_index:
                    ref_info.target_id += 1

    def _calculate_component_insertion_index(self, gameobject):
        """Calculate the best insertion index for a new component"""
        insertion_index = len(self.scn.instance_infos)
        if gameobject.component_count > 0:
            last_component_go_id = gameobject.id + gameobject.component_count
            if last_component_go_id < len(self.scn.object_table):
                last_component_instance_id = self.scn.object_table[last_component_go_id]
                if last_component_instance_id > 0:
                    insertion_index = last_component_instance_id + 1
        if insertion_index == len(self.scn.instance_infos):
            go_instance_id = self.scn.object_table[gameobject.id]
            if go_instance_id > 0:
                insertion_index = go_instance_id + 1
        return insertion_index
    
    def _remove_from_object_table(self, object_table_index):
        if object_table_index < 0 or object_table_index >= len(self.scn.object_table):
            print(f"Warning: Invalid object table index {object_table_index}")
            return
            
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
                if hasattr(ref_info, 'object_id') and ref_info.object_id > object_table_index:
                    ref_info.object_id -= 1
                if hasattr(ref_info, 'target_id') and ref_info.target_id > object_table_index:
                    ref_info.target_id -= 1

    def _find_nested_objects(self, base_instance_id):
        """Find instance IDs of nested objects that aren't in the object table."""
        return RszInstanceOperations.find_nested_objects(
            self.scn.parsed_elements, base_instance_id, self.scn.object_table
        )

    def delete_folder(self, folder_id):
        """Delete a folder with the given ID and all its contents"""
        if folder_id < 0 or folder_id >= len(self.scn.object_table):
            QMessageBox.warning(self.viewer, "Error", f"Invalid Folder ID: {folder_id}")
            return False

        target_folder = None
        for folder in self.scn.folder_infos:
            if folder.id == folder_id:
                target_folder = folder
                break

        if target_folder is None:
            print(f"Warning: Folder with exact ID {folder_id} not found, attempting recovery...")
            
            instance_id = self.scn.object_table[folder_id] if folder_id < len(self.scn.object_table) else 0
            if instance_id > 0:
                for folder in self.scn.folder_infos:
                    if folder.id < len(self.scn.object_table) and self.scn.object_table[folder.id] == instance_id:
                        target_folder = folder
                        folder_id = folder.id
                        print(f"  Found Folder at adjusted ID {folder_id}")
                        break

        if target_folder is None:
            QMessageBox.warning(self.viewer, "Error", f"Folder with ID {folder_id} not found")
            return False

        folder_instance_id = self.scn.object_table[folder_id] if folder_id < len(self.scn.object_table) else 0
        print(f"Deleting folder ID={folder_id} (instance_id={folder_instance_id})")

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
        
        print(f"Found {len(folders_to_delete)} folders to delete")
        
        folder_ids = {f.id for f in folders_to_delete}
        
        gameobjects_to_delete = []
        for go in self.scn.gameobjects:
            if go.parent_id in folder_ids:
                gameobjects_to_delete.append(go)
        
        print(f"Found {len(gameobjects_to_delete)} GameObjects to delete")
        
        deletion_errors = 0
        
        for go in reversed(gameobjects_to_delete):
            try:
                success = self._delete_gameobject_directly(go)
                if not success:
                    print(f"  Warning: Failed to delete GameObject with ID {go.id}")
                    deletion_errors += 1
            except Exception as e:
                print(f"  Error deleting GameObject with ID {go.id}: {str(e)}")
                deletion_errors += 1

        for folder in reversed(folders_to_delete):
            try:
                if folder is target_folder:
                    continue
                    
                if not self.scn.is_pfb and not self.scn.is_usr and hasattr(folder, 'prefab_id') and folder.prefab_id > 0:
                    prefab_deleted = self._delete_prefab_for_object(folder.prefab_id)
                    if prefab_deleted:
                        print(f"  Deleted prefab {folder.prefab_id} associated with folder {folder.id}")
                
                print(f"  Deleting subfolder with ID {folder.id}")
                folder_instance_id = self.scn.object_table[folder.id] if folder.id < len(self.scn.object_table) else 0
                
                if folder_instance_id > 0:
                    nested_objects = self._find_nested_objects(folder_instance_id)
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
            except Exception as e:
                print(f"  Error deleting subfolder with ID {folder.id}: {str(e)}")
                deletion_errors += 1

        try:
            print(f"  Deleting target folder with ID {target_folder.id}")
            folder_instance_id = self.scn.object_table[target_folder.id] if target_folder.id < len(self.scn.object_table) else 0
            
            if folder_instance_id > 0:
                nested_objects = self._find_nested_objects(folder_instance_id)
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
        except Exception as e:
            print(f"  Error deleting target folder: {str(e)}")
            deletion_errors += 1
        
        if deletion_errors > 0:
            print(f"Warning: Encountered {deletion_errors} errors during folder deletion")
        
        self.viewer.mark_modified()
        return True

    def create_component_for_gameobject(self, gameobject_instance_id, component_type):
        """Create a new component on the specified GameObject"""
        if gameobject_instance_id <= 0 or gameobject_instance_id >= len(self.scn.instance_infos):
            raise ValueError(f"Invalid GameObject instance ID: {gameobject_instance_id}")
            
        target_go = None
        for i, go in enumerate(self.scn.gameobjects):
            if (
                go.id < len(self.scn.object_table)
                and self.scn.object_table[go.id] == gameobject_instance_id
            ):
                target_go = go
                break
                
        if not target_go:
            raise ValueError(f"GameObject with instance ID {gameobject_instance_id} not found")
            
        type_info, type_id = self.type_registry.find_type_by_name(component_type)
        if not type_info:
            raise ValueError(f"Component type '{component_type}' not found in registry")
            
        object_table_insertion_index = target_go.id + target_go.component_count + 1
        new_instance = self.viewer._initialize_new_instance(type_id, type_info)
        if not new_instance:
            raise ValueError(f"Failed to create instance of type {component_type}")
            
        instance_insertion_index = self._calculate_component_insertion_index(target_go)
        temp_parsed_elements = {}
        nested_objects = []
        
        self._analyze_instance_fields_for_nested_objects(
            temp_parsed_elements, type_info, nested_objects, instance_insertion_index
        )
        
        valid_nested_objects = []
        for nested_obj in nested_objects:
            
            nested_type_info, nested_type_id = nested_obj[0], nested_obj[1]
            
            field_name = nested_obj[2] if len(nested_obj) >= 3 else None
            
            nested_instance = self.viewer._initialize_new_instance(nested_type_id, nested_type_info)
            if not nested_instance or nested_instance.type_id == 0:
                continue
                
            self.viewer._insert_instance_and_update_references(instance_insertion_index, nested_instance)
            nested_object_fields = {}
            self.viewer._initialize_fields_from_type_info(nested_object_fields, nested_type_info)
            self.scn.parsed_elements[instance_insertion_index] = nested_object_fields
            
            if field_name:
                valid_nested_objects.append((nested_type_info, nested_type_id, field_name))
            else:
                valid_nested_objects.append((nested_type_info, nested_type_id))
                
            instance_insertion_index += 1
            self.viewer.handler.id_manager.register_instance(instance_insertion_index)
            
        self.viewer._insert_instance_and_update_references(instance_insertion_index, new_instance)
        component_reasy_id = self.viewer.handler.id_manager.register_instance(instance_insertion_index)
        
        component_fields = {}
        self.viewer._initialize_fields_from_type_info(component_fields, type_info)
        
        self._update_object_references(
            component_fields,
            temp_parsed_elements,
            instance_insertion_index,
            valid_nested_objects,
        )
        
        if "parent" in component_fields:
            parent_obj = component_fields["parent"]
            if hasattr(parent_obj, "value"):
                parent_obj.value = gameobject_instance_id
                
        self.scn.parsed_elements[instance_insertion_index] = component_fields
        target_go.component_count += 1
        
        self._insert_into_object_table(object_table_insertion_index, instance_insertion_index)
        self.viewer.mark_modified()
        
        return {
            "success": True,
            "instance_id": instance_insertion_index,
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
        
        if owner_go_id is not None:
            owner_go = next((go for go in self.scn.gameobjects if go.id == owner_go_id), None)
            if owner_go:
                for comp_index in range(1, owner_go.component_count + 1):
                    comp_object_id = owner_go.id + comp_index
                    if (comp_object_id < len(self.scn.object_table) and 
                        self.scn.object_table[comp_object_id] == component_instance_id):
                        object_table_index = comp_object_id
                        break
        
        if object_table_index < 0:
            for i, instance_id in enumerate(self.scn.object_table):
                if instance_id == component_instance_id:
                    object_table_index = i
                    break
                    
            if object_table_index < 0:
                raise ValueError(f"Component {component_instance_id} not found in object table")
            
            if not owner_go:
                for go in self.scn.gameobjects:
                    if go.id < object_table_index and object_table_index <= go.id + go.component_count:
                        owner_go = go
                        break
        
        if not owner_go:
            raise ValueError(f"Could not find GameObject owning component {component_instance_id}")
        
        original_component_count = owner_go.component_count
        
        nested_objects = self._find_nested_objects(component_instance_id)
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
                print(f"  Excluding UserData instance {instance_id} from deletion as it's referenced {userdata_reference_map[instance_id]} times elsewhere")
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
    
    def manage_gameobject_prefab(self, gameobject_id, new_prefab_path):
        """Create or modify a prefab association for a GameObject
        
        Args:
            gameobject_id: The GameObject's ID in the object table
            new_prefab_path: Path string for the prefab
            
        Returns:
            bool: True if the operation was successful
        """
        if self.scn.is_pfb or self.scn.is_usr:
            print("Prefabs can't be modified in PFB/USR files")
            return False
            
        if gameobject_id < 0 or gameobject_id >= len(self.scn.object_table):
            print(f"Invalid GameObject ID {gameobject_id}")
            return False
            
        target_go = None
        for go in self.scn.gameobjects:
            if go.id == gameobject_id:
                target_go = go
                break
                
        if not target_go:
            print(f"GameObject with ID {gameobject_id} not found")
            return False
        
        if target_go.prefab_id < 0:
            if not hasattr(self.scn, 'prefab_infos'):
                print("No prefab_infos array in scene")
                return False
                
            from file_handlers.rsz.rsz_file import RszPrefabInfo
            new_prefab = RszPrefabInfo()
            
            prefab_id = len(self.scn.prefab_infos)
            
            new_prefab.string_offset = 0
            self.scn.prefab_infos.append(new_prefab)
            target_go.prefab_id = prefab_id
            
            if hasattr(self.scn, '_prefab_str_map'):
                self.scn._prefab_str_map[new_prefab] = new_prefab_path
                
            print(f"Created new prefab (ID: {prefab_id}) for GameObject {gameobject_id} with path: {new_prefab_path}")
            
        else:
            prefab_id = target_go.prefab_id
            
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
       
    def _find_userdata_references(self, fields, userdata_refs):
        RszInstanceOperations.find_userdata_references(fields, userdata_refs)
   
    def _update_chainsaw_context_id_group(self, instance_id, fields, context_id_offset):
        """Update the _Group field for chainsaw.ContextID instances"""
        if instance_id >= len(self.scn.instance_infos) or instance_id <= 0:
            return
        
        type_id = self.scn.instance_infos[instance_id].type_id
        type_info = self.type_registry.get_type_info(type_id)
        
        if not type_info or "name" not in type_info:
            return
        
        if type_info["name"] == "chainsaw.ContextID":
            #print("Found chainsaw.ContextID instance, updating _Group field")
            
            if "_Group" in fields and isinstance(fields["_Group"], S32Data):
                original_value = fields["_Group"].value
                new_value = original_value + context_id_offset
                fields["_Group"].value = new_value
                #print(f"  Updated _Group from {original_value} to {new_value}")

