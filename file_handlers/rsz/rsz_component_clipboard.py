import traceback
from file_handlers.rsz.rsz_data_types import ObjectData
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
            
            all_instances = [component_instance_id] + list(nested_instances) + list(userdata_refs)
            hierarchy = self.serialize_hierarchy(viewer, all_instances)
            
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
            
            hierarchy_data = clipboard_data.get("hierarchy", {})
            
            if not hierarchy_data:
                print("No hierarchy data in clipboard")
                return None
            
            highest_nested_instance_id = go_instance_id
            
            if target_go.component_count > 0:
                for i in range(1, target_go.component_count + 1):
                    comp_object_id = go_object_id + i
                    if comp_object_id < len(viewer.scn.object_table):
                        comp_instance_id = viewer.scn.object_table[comp_object_id]
                        if comp_instance_id > 0 and comp_instance_id < len(viewer.scn.instance_infos):
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
            
            insertion_index = highest_nested_instance_id + 1
            print(f"Component insertion index set to: {insertion_index}")
            
            main_component_id = clipboard_data.get("instance_id", 0)
            
            created_instances = self.paste_instances_from_hierarchy(
                viewer, hierarchy_data, instance_mapping, userdata_mapping, 
                guid_mapping, randomize_guids=False
            )

            if not created_instances:
                print("No instances were created")
                return None
            
            main_component_new_id = instance_mapping.get(main_component_id)
            if not main_component_new_id:
                print(f"Main component ID {main_component_id} not found in mapping")
                return None
            

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
            
            id_adjustment_map = RszGameObjectClipboard._cleanup_duplicate_userdata_after_paste(
                viewer, set(created_instances)
            )
            if id_adjustment_map and main_component_new_id in id_adjustment_map:
                main_component_new_id = id_adjustment_map[main_component_new_id]
            viewer.mark_modified()
            final_reasy_id = viewer.handler.id_manager.get_reasy_id_for_instance(main_component_new_id)
            print(f"Final check: main component {main_component_new_id} has reasy_id {final_reasy_id}")
            
            nested_count = len([id for id in created_instances 
                               if id != main_component_new_id and id not in userdata_mapping.values()])
        
            return {
                "success": True,
                "instance_id": main_component_new_id,
                "reasy_id": final_reasy_id,
                "type_name": type_name,
                "type_id": type_id,
                "component_object_id": component_object_id,
                "go_id": go_object_id,
                "nested_instances": nested_count,
                "userdata_instances": len(userdata_mapping)
            }
        
        except Exception as e:
            print(f"Error pasting component: {str(e)}")
            traceback.print_exc()
            return None

