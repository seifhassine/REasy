import traceback
from file_handlers.rsz.rsz_data_types import ObjectData
from file_handlers.rsz.rsz_file import RszInstanceInfo
from file_handlers.rsz.rsz_clipboard_base import RszClipboardBase
from file_handlers.rsz.rsz_gameobject_clipboard import RszGameObjectClipboard

class RszComponentClipboard(RszClipboardBase):
    """Clipboard handling for copying and pasting components."""
    
    def get_clipboard_type(self) -> str:
        """Return the clipboard type identifier"""
        return "component"
    
    def copy_component_to_clipboard(self, viewer, component_instance_id):
        """Copy a component to clipboard
        
        Args:
            viewer: The viewer instance
            component_instance_id: Instance ID of the component to copy
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            if component_instance_id <= 0 or component_instance_id >= len(viewer.scn.instance_infos):
                print(f"Invalid component instance ID: {component_instance_id}")
                return False
                
            instance_info = viewer.scn.instance_infos[component_instance_id]
            print(f"Copying component with instance ID: {component_instance_id}")
            
            component_type_name = ""
            if hasattr(viewer, "type_registry") and viewer.type_registry:
                type_info = viewer.type_registry.get_type_info(instance_info.type_id)
                if type_info and "name" in type_info:
                    component_type_name = type_info["name"]
            
            nested_instances, userdata_refs = self.collect_all_references(viewer, component_instance_id)
            
            hierarchy = {
                "instances": {},
                "gameobjects": {},
                "root_id": None
            }
            
            main_data = self.serialize_instance_data(viewer, component_instance_id)
            if main_data:
                hierarchy["instances"][str(component_instance_id)] = main_data
            
            for nested_id in nested_instances:
                nested_data = self.serialize_instance_data(viewer, nested_id)
                if nested_data:
                    hierarchy["instances"][str(nested_id)] = nested_data
            
            for userdata_id in userdata_refs:
                userdata_data = self.serialize_instance_data(viewer, userdata_id)
                if userdata_data:
                    hierarchy["instances"][str(userdata_id)] = userdata_data
            
            component_data = {
                "instance_id": component_instance_id,
                "type_id": instance_info.type_id,
                "crc": instance_info.crc,
                "type_name": component_type_name,
                "nested_instances": list(nested_instances),
                "userdata_refs": list(userdata_refs),
                "hierarchy": hierarchy
            }
            
            success = self.save_clipboard_data(viewer, component_data)
            if success:
                print(f"Component '{component_type_name}' copied to clipboard with {len(nested_instances)} nested objects and {len(userdata_refs)} userdata references")
            
            return success
            
        except Exception as e:
            print(f"Error copying component to clipboard: {str(e)}")
            traceback.print_exc()
            return False
    
    def paste_component_from_clipboard(self, viewer, go_instance_id, clipboard_data=None):
        """Paste a component from clipboard to a GameObject with ffull support for nested objects

        Args:
            viewer: The viewer instance
            go_instance_id: Instance ID of the target GameObject
            clipboard_data: Optional pre-loaded clipboard data
            
        Returns:
            dict: Result information, or None if failed
        """
        try:
            if not clipboard_data:
                print("No clipboard data provided, loading from file")
                clipboard_data = self.get_clipboard_data(viewer)
                
            if not clipboard_data:
                print("No component clipboard data available")
                return None
            
            if go_instance_id <= 0 or go_instance_id >= len(viewer.scn.instance_infos):
                print(f"Invalid GameObject instance ID: {go_instance_id}")
                return None
            
            go_object_id = -1
            for i, instance_id in enumerate(viewer.scn.object_table):
                if instance_id == go_instance_id:
                    go_object_id = i
                    break
            
            if go_object_id < 0:
                print(f"Could not find GameObject with instance ID {go_instance_id} in object table")
                return None
            
            target_go = None
            for go in viewer.scn.gameobjects:
                if go.id == go_object_id:
                    target_go = go
                    break
            
            if not target_go:
                print(f"Could not find GameObject with object ID {go_object_id}")
                return None
            
            type_id = clipboard_data.get("type_id", 0)
            if type_id <= 0:
                print("Invalid component type ID in clipboard data")
                return None
            
            type_name = clipboard_data.get("type_name", "Unknown")
            print(f"Pasting component of type {type_name} to GameObject")
            
            instance_mapping = {}
            guid_mapping = {}
            userdata_mapping = {}
            
            highest_nested_instance_id = 0
            
            if target_go.component_count > 0:
                for i in range(1, target_go.component_count + 1):
                    comp_object_id = go_object_id + i
                    if comp_object_id < len(viewer.scn.object_table):
                        comp_instance_id = viewer.scn.object_table[comp_object_id]
                        if comp_instance_id <= 0 or comp_instance_id >= len(viewer.scn.instance_infos):
                            continue
                        
                        highest_nested_instance_id = max(highest_nested_instance_id, comp_instance_id)
                        
                        if comp_instance_id in viewer.scn.parsed_elements:
                            from file_handlers.rsz.rsz_instance_operations import RszInstanceOperations
                            
                            component_nested_ids = RszInstanceOperations.find_nested_objects(
                                viewer.scn.parsed_elements, comp_instance_id, viewer.scn.object_table
                            )
                            
                            component_fields = viewer.scn.parsed_elements[comp_instance_id]
                            object_refs = RszInstanceOperations.find_object_references(component_fields)
                            component_nested_ids.update(object_refs)
                            
                            if component_nested_ids:
                                highest_nested_instance_id = max(highest_nested_instance_id, max(component_nested_ids))
            else:
                highest_nested_instance_id = go_instance_id
                print(f"No existing components - setting highest_nested_instance_id to GameObject ID: {go_instance_id}")

            insertion_index = highest_nested_instance_id + 1
            print(f"Component insertion index set to: {insertion_index}")
            
            hierarchy_data = clipboard_data.get("hierarchy", {})
            
            if hierarchy_data and "instances" in hierarchy_data:
                instances_data = hierarchy_data.get("instances", {})
                
                main_component_id = clipboard_data.get("instance_id", 0)
                if str(main_component_id) not in instances_data:
                    print(f"Main component ID {main_component_id} not found in hierarchy")
                    return None
                
                def get_instance_references(fields_data):
                    references = set()
                    for field_name, field_data in fields_data.items():
                        if field_data.get("type") == "object" and field_data.get("value", 0) > 0:
                            references.add(field_data.get("value"))
                        elif field_data.get("type") == "array" and "values" in field_data:
                            for element in field_data["values"]:
                                if isinstance(element, dict) and element.get("type") == "object" and element.get("value", 0) > 0:
                                    references.add(element.get("value"))
                    return references
                
                dependency_graph = {}
                for id_str, instance_data in instances_data.items():
                    instance_id = int(id_str)
                    dependency_graph[instance_id] = set()
                    
                    if instance_data.get("is_userdata", False):
                        continue
                        
                    fields_data = instance_data.get("fields", {})
                    references = get_instance_references(fields_data)
                    for ref_id in references:
                        if str(ref_id) in instances_data:
                            dependency_graph[instance_id].add(ref_id)
                
                def find_userdata_refs(fields_data):
                    userdata_refs = set()
                    for field_name, field_data in fields_data.items():
                        if field_data.get("type") == "userdata" and field_data.get("index", 0) > 0:
                            userdata_refs.add(field_data.get("index"))
                        elif field_data.get("type") == "array" and "values" in field_data:
                            for element in field_data["values"]:
                                if isinstance(element, dict) and element.get("type") == "userdata" and element.get("index", 0) > 0:
                                    userdata_refs.add(element.get("index"))
                    return userdata_refs
                
                userdata_to_instance = {}
                for id_str, instance_data in instances_data.items():
                    if instance_data.get("is_userdata", False):
                        continue
                        
                    instance_id = int(id_str)
                    fields_data = instance_data.get("fields", {})
                    refs = find_userdata_refs(fields_data)
                    
                    for ref_id in refs:
                        if str(ref_id) in instances_data and instances_data[str(ref_id)].get("is_userdata", False):
                            userdata_to_instance[ref_id] = instance_id
                
                nested_ids = []
                
                for id_str, instance_data in instances_data.items():
                    instance_id = int(id_str)
                    if not instance_data.get("is_userdata", False) and instance_id != main_component_id:
                        nested_ids.append(instance_id)
                
                # Get topologically sorted instances (dependencies first)
                sorted_nested_ids = RszInstanceOperations.topological_sort(dependency_graph)
                
                if main_component_id in sorted_nested_ids:
                    sorted_nested_ids.remove(main_component_id)
                
                creation_order = []
                
                for instance_id in sorted_nested_ids + [main_component_id]:
                    instance_userdata_refs = set()
                    
                    instance_data = instances_data.get(str(instance_id), {})
                    if not instance_data.get("is_userdata", False):
                        fields_data = instance_data.get("fields", {})
                        instance_userdata_refs = find_userdata_refs(fields_data)
                    
                    for userdata_id in instance_userdata_refs:
                        if str(userdata_id) in instances_data and instances_data[str(userdata_id)].get("is_userdata", False):
                            if userdata_id not in creation_order:
                                creation_order.append(userdata_id)
                                print(f"Adding userdata {userdata_id} before instance {instance_id}")
                    
                    creation_order.append(instance_id)
                
                print(f"Creation order: {creation_order}")
                
                main_component_position = creation_order.index(main_component_id)
                print(f"Main component is at position {main_component_position} in creation order")
                
                reserved_reasy_ids = {}
                next_reasy_id = viewer.handler.id_manager._next_id
                
                for i, old_instance_id in enumerate(creation_order):
                    new_instance_id = insertion_index + i
                    if old_instance_id == main_component_id:
                        main_component_reasy_id = next_reasy_id
                        reserved_reasy_ids[new_instance_id] = main_component_reasy_id
                        print(f"Reserved reasy_id {main_component_reasy_id} for main component at instance ID {new_instance_id}")
                        next_reasy_id += 1
                
                current_insertion_index = insertion_index
                all_created_instances = []
                main_component_new_id = None
                
                for old_instance_id in creation_order:
                    instance_data = instances_data.get(str(old_instance_id), {})
                    
                    type_id = instance_data.get("type_id", 0)
                    crc = instance_data.get("crc", 0)
                    
                    if type_id <= 0:
                        print(f"Warning: Invalid type ID for instance {old_instance_id}")
                        continue
                    
                    new_instance = RszInstanceInfo()
                    new_instance.type_id = type_id
                    new_instance.crc = crc
                    
                    viewer._insert_instance_and_update_references(current_insertion_index, new_instance)
                    new_instance_id = current_insertion_index
                    instance_mapping[old_instance_id] = new_instance_id
                    
                    if old_instance_id == main_component_id:
                        main_component_new_id = new_instance_id
                        
                        if new_instance_id in reserved_reasy_ids:
                            reasy_id = reserved_reasy_ids[new_instance_id]
                            
                            viewer.handler.id_manager.force_register_instance(new_instance_id, reasy_id)
                            
                            print(f"Main component assigned instance ID: {new_instance_id} with forced reasy_id: {reasy_id}")
                            
                            mapped_instance = viewer.handler.id_manager.get_instance_id(reasy_id)
                            if mapped_instance != new_instance_id:
                                print(f"WARNING: Mapping inconsistency detected and fixed: reasy_id {reasy_id} points to instance {mapped_instance}, not {new_instance_id}")
                                viewer.handler.id_manager.force_register_instance(new_instance_id, reasy_id)
                        else:
                            reasy_id = viewer.handler.id_manager.register_instance(new_instance_id)
                            print(f"Main component assigned instance ID: {new_instance_id} with regular reasy_id: {reasy_id}")
                    else:
                        reasy_id = viewer.handler.id_manager.register_instance(new_instance_id)
                        print(f"Created instance {new_instance_id} with reasy_id {reasy_id}")
                    
                        if instance_data.get("is_userdata", False):
                            self.setup_userdata_for_pasted_instance(
                                viewer, 
                                new_instance_id, 
                                instance_data.get("userdata_hash", 0),
                                instance_data.get("userdata_string", "")
                            )
                            userdata_mapping[old_instance_id] = new_instance_id
                    
                    all_created_instances.append(new_instance_id)
                    current_insertion_index += 1
                
                if main_component_new_id is None:
                    print("ERROR: Failed to assign ID to main component")
                    return None
                
                main_component_reasy_id = viewer.handler.id_manager.get_reasy_id_for_instance(main_component_new_id)
                
                if main_component_reasy_id in viewer.handler.id_manager._reasy_to_instance:
                    mapped_instance = viewer.handler.id_manager._reasy_to_instance[main_component_reasy_id]
                    if mapped_instance != main_component_new_id:
                        print(f"CRITICAL ERROR: reasy_id {main_component_reasy_id} points to instance {mapped_instance}, not {main_component_new_id}")
                        viewer.handler.id_manager._reasy_to_instance[main_component_reasy_id] = main_component_new_id
                
                # Second pass: Fill in fields for all instances
                for old_instance_id in creation_order:
                    if old_instance_id not in instance_mapping:
                        continue
                        
                    new_instance_id = instance_mapping[old_instance_id]
                    instance_data = instances_data.get(str(old_instance_id), {})
                    
                    if instance_data.get("is_userdata", False):
                        continue
                    
                    fields_data = instance_data.get("fields", {})
                    
                    # For single component paste, preserve original GUIDs (no randomization)
                    new_fields = self.deserialize_fields_with_remapping(
                        fields_data, instance_mapping, userdata_mapping, guid_mapping, randomize_guids=False
                    )
                    self.update_userdata_references(new_fields, userdata_mapping)
                    
                    viewer.scn.parsed_elements[new_instance_id] = new_fields
                
                if main_component_new_id in viewer.scn.parsed_elements:
                    component_fields = viewer.scn.parsed_elements[main_component_new_id]
                    if "parent" in component_fields and isinstance(component_fields["parent"], ObjectData):
                        component_fields["parent"].value = go_instance_id
                        print(f"Updated component's parent field to point to GO {go_instance_id}")
                
                component_object_id = go_object_id + target_go.component_count + 1
                RszGameObjectClipboard._insert_into_object_table(
                    viewer, component_object_id, main_component_new_id
                )
                
                target_go.component_count += 1
                
                if hasattr(viewer.scn, 'instance_hierarchy'):
                    if main_component_new_id not in viewer.scn.instance_hierarchy:
                        viewer.scn.instance_hierarchy[main_component_new_id] = {"children": [], "parent": None}
                    
                    viewer.scn.instance_hierarchy[main_component_new_id]["parent"] = go_instance_id
                    
                    if go_instance_id not in viewer.scn.instance_hierarchy:
                        viewer.scn.instance_hierarchy[go_instance_id] = {"children": [], "parent": None}
                    if main_component_new_id not in viewer.scn.instance_hierarchy[go_instance_id]["children"]:
                        viewer.scn.instance_hierarchy[go_instance_id]["children"].append(main_component_new_id)
                
                #one final verification of all reasy_ids to ensure consistency
                for instance_id in all_created_instances:
                    reasy_id = viewer.handler.id_manager.get_reasy_id_for_instance(instance_id)
                    if reasy_id:
                        mapped_instance = viewer.handler.id_manager._reasy_to_instance.get(reasy_id)
                        if mapped_instance != instance_id:
                            print(f"WARNING: reasy_id {reasy_id} points to instance {mapped_instance}, not {instance_id}")
                
                id_adjustment_map = RszGameObjectClipboard._cleanup_duplicate_userdata_after_paste(
                    viewer, set(all_created_instances)
                )
                if id_adjustment_map and main_component_new_id in id_adjustment_map:
                    main_component_new_id = id_adjustment_map[main_component_new_id]
                viewer.mark_modified()
                final_reasy_id = viewer.handler.id_manager.get_reasy_id_for_instance(main_component_new_id)
                print(f"Final check: main component {main_component_new_id} has reasy_id {final_reasy_id}")
                
                return {
                    "success": True,
                    "instance_id": main_component_new_id,
                    "reasy_id": final_reasy_id,
                    "type_name": type_name,
                    "type_id": type_id,
                    "component_object_id": component_object_id,
                    "go_id": go_object_id,
                    "nested_instances": len([id for id in all_created_instances if id != main_component_new_id and id not in userdata_mapping.values()]),
                    "userdata_instances": len(userdata_mapping)
                }
        
        except Exception as e:
            print(f"Error pasting component: {str(e)}")
            traceback.print_exc()
            return None

