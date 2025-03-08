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
from utils.id_manager import IdManager


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
        IdManager.instance().register_instance(insertion_index)
        
        gameobject_fields = {}
        self.viewer._initialize_fields_from_type_info(gameobject_fields, type_info)
        
        if "v0" in gameobject_fields and hasattr(gameobject_fields["v0"], "set_value"):
            gameobject_fields["v0"].set_value(name)
            
        self.scn.parsed_elements[insertion_index] = gameobject_fields
        object_table_index = len(self.scn.object_table)
        self.scn.object_table.append(insertion_index)
        
        new_gameobject = self._create_gameobject_entry(
            object_table_index, parent_id, insertion_index
        )
        
        self._update_gameobject_hierarchy(new_gameobject)
        self.scn.gameobjects.append(new_gameobject)
        self.viewer.mark_modified()
        return True

    def _calculate_gameobject_insertion_index(self):
        """Calculate the best insertion index for a new GameObject instance"""
        return len(self.scn.instance_infos)

    def _create_gameobject_entry(self, object_id, parent_id, instance_id):
        """Create a new GameObject entry for the scene"""
        from file_handlers.rsz.rsz_file import ScnGameObject
        new_go = ScnGameObject()
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
    
    def delete_gameobject(self, gameobject_id):
        """Delete a GameObject with the given ID"""
        if gameobject_id < 0 or gameobject_id >= len(self.scn.object_table):
            QMessageBox.warning(self.viewer, "Error", f"Invalid GameObject ID: {gameobject_id}")
            return False
            
        target_go = None
        for go in self.scn.gameobjects:
            if go.id == gameobject_id:
                target_go = go
                break
        
        if target_go is None:
            print(f"Warning: GameObject with exact ID {gameobject_id} not found, attempting recovery...")
            
            instance_id = self.scn.object_table[gameobject_id] if gameobject_id < len(self.scn.object_table) else 0
            if instance_id > 0:
                for go in self.scn.gameobjects:
                    if go.id < len(self.scn.object_table) and self.scn.object_table[go.id] == instance_id:
                        target_go = go
                        gameobject_id = go.id
                        print(f"  Found GameObject at adjusted ID {gameobject_id}")
                        break
        
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
            
            if not self.scn.is_pfb and not self.scn.is_usr and hasattr(gameobject, 'prefab_id'):
                if gameobject.prefab_id >= 0:
                    _ = self._delete_prefab_for_object(gameobject.prefab_id)
            
            gameobject_refs_to_delete = self._collect_gameobject_hierarchy_by_reference(gameobject)
            
            if not gameobject_refs_to_delete:
                return False
                
            for go in reversed(gameobject_refs_to_delete):
                if go != gameobject and not self.scn.is_pfb and not self.scn.is_usr and hasattr(go, 'prefab_id'):
                    if go.prefab_id >= 0:
                        _ = self._delete_prefab_for_object(go.prefab_id)
                
                self._delete_all_components_of_gameobject(go)
                
                if go.id < len(self.scn.object_table):
                    go_instance_id = self.scn.object_table[go.id]
                    
                    if go_instance_id > 0:
                        print(f"  Deleting GameObject instance {go_instance_id} (object_id: {go.id})")
                        
                        instance_fields = self.scn.parsed_elements.get(go_instance_id, {})
                        nested_objects = self.viewer._find_nested_objects(instance_fields, go_instance_id)
                        nested_objects.add(go_instance_id)
                        
                        for instance_id in sorted(nested_objects, reverse=True):
                            self.viewer._remove_instance_references(instance_id)
                        
                        id_mapping = self.viewer._update_instance_references_after_deletion(go_instance_id, nested_objects)
                        
                        if id_mapping:
                            IdManager.instance().update_all_mappings(id_mapping, nested_objects)
                
                if go.id < len(self.scn.object_table):
                    self.viewer._remove_from_object_table(go.id)
                
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
                self.viewer.delete_component_from_gameobject(component_instance_id, gameobject.id)
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
