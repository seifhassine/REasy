import os
import json
import traceback
from file_handlers.rsz.rsz_data_types import ObjectData, ArrayData, UserDataData
from file_handlers.rsz.rsz_file import RszInstanceInfo
from file_handlers.rsz.rsz_clipboard_utils import RszClipboardUtils
from file_handlers.rsz.rsz_array_clipboard import RszArrayClipboard
from file_handlers.rsz.rsz_gameobject_clipboard import RszGameObjectClipboard

class RszComponentClipboard:
    """Clipboard handling for copying and pasting components."""
    
    @staticmethod
    def get_clipboard_directory():
        return RszClipboardUtils.get_type_clipboard_directory("component")
    
    @staticmethod
    def get_json_name(viewer):
        return RszClipboardUtils.get_json_name(viewer)
    
    @staticmethod
    def get_clipboard_file(viewer):
        json_name = RszComponentClipboard.get_json_name(viewer)
        base_name = os.path.splitext(json_name)[0]
        
        return os.path.join(
            RszComponentClipboard.get_clipboard_directory(),
            f"{base_name}-component-clipboard.json"
        )
    
    @staticmethod
    def copy_component_to_clipboard(viewer, component_instance_id):
        """Copy a component to clipboard
        
        Args:
            viewer: The viewer instance
            component_instance_id: Instance ID of the component to copy
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            instance_info = viewer.scn.instance_infos[component_instance_id]
            print(f"Copying component with instance ID: {component_instance_id}")
            
            component_type_name = ""
            if hasattr(viewer, "type_registry") and viewer.type_registry:
                type_info = viewer.type_registry.get_type_info(instance_info.type_id)
                if type_info and "name" in type_info:
                    component_type_name = type_info["name"]
            
            nested_instances = set()
            userdata_refs = set()
            
            if component_instance_id in viewer.scn.parsed_elements:
                fields = viewer.scn.parsed_elements[component_instance_id]
                
                nested_objects = RszGameObjectClipboard._find_nested_objects(viewer, component_instance_id)
                nested_instances.update(nested_objects)
                
                RszGameObjectClipboard._find_userdata_references(fields, userdata_refs)
            
            hierarchy = {
                "instances": {},
                "gameobjects": {},
                "root_id": None
            }
            
            hierarchy["instances"][str(component_instance_id)] = {
                "id": component_instance_id,
                "type_id": instance_info.type_id,
                "crc": instance_info.crc,
                "type_name": component_type_name,
                "fields": {}
            }
            
            if component_instance_id in viewer.scn.parsed_elements:
                fields = viewer.scn.parsed_elements[component_instance_id]
                for field_name, field_data in fields.items():
                    hierarchy["instances"][str(component_instance_id)]["fields"][field_name] = RszArrayClipboard._serialize_element(field_data)
            
            for nested_id in nested_instances:
                if nested_id in viewer.scn.parsed_elements:
                    nested_instance_info = viewer.scn.instance_infos[nested_id]
                    nested_type_name = ""
                    
                    if hasattr(viewer, "type_registry") and viewer.type_registry:
                        nested_type_info = viewer.type_registry.get_type_info(nested_instance_info.type_id)
                        if nested_type_info and "name" in nested_type_info:
                            nested_type_name = nested_type_info["name"]
                    
                    nested_fields = {}
                    nested_fields_data = viewer.scn.parsed_elements[nested_id]
                    
                    deeper_nested = RszGameObjectClipboard._find_nested_objects(viewer, nested_id)
                    nested_instances.update(deeper_nested)
                    
                    RszGameObjectClipboard._find_userdata_references(nested_fields_data, userdata_refs)
                    
                    for field_name, field_data in nested_fields_data.items():
                        nested_fields[field_name] = RszArrayClipboard._serialize_element(field_data)
                    
                    hierarchy["instances"][str(nested_id)] = {
                        "id": nested_id,
                        "type_id": nested_instance_info.type_id,
                        "crc": nested_instance_info.crc,
                        "type_name": nested_type_name,
                        "fields": nested_fields
                    }
            
            for userdata_id in userdata_refs:
                if userdata_id > 0 and userdata_id < len(viewer.scn.instance_infos):
                    if hasattr(viewer.scn, '_rsz_userdata_set') and userdata_id in viewer.scn._rsz_userdata_set:
                        userdata_instance_info = viewer.scn.instance_infos[userdata_id]
                        userdata_hash = 0
                        userdata_string = ""
                        
                        for rui in viewer.scn.rsz_userdata_infos:
                            if rui.instance_id == userdata_id:
                                userdata_hash = rui.hash
                                
                                if hasattr(viewer.scn, '_rsz_userdata_str_map') and rui in viewer.scn._rsz_userdata_str_map:
                                    userdata_string = viewer.scn._rsz_userdata_str_map[rui]
                                break
                        
                        hierarchy["instances"][str(userdata_id)] = {
                            "id": userdata_id,
                            "type_id": userdata_instance_info.type_id,
                            "crc": userdata_instance_info.crc,
                            "fields": {},  
                            "is_userdata": True,
                            "userdata_hash": userdata_hash,
                            "userdata_string": userdata_string
                        }
            
            component_data = {
                "instance_id": component_instance_id,
                "type_id": instance_info.type_id,
                "crc": instance_info.crc,
                "type_name": component_type_name,
                "nested_instances": list(nested_instances),
                "userdata_refs": list(userdata_refs),
                "hierarchy": hierarchy
            }
            
            clipboard_file = RszComponentClipboard.get_clipboard_file(viewer)
            with open(clipboard_file, 'w') as f:
                json.dump(component_data, f, indent=2, default=RszArrayClipboard._json_serializer)
            
            print(f"Component '{component_type_name}' copied to clipboard with {len(nested_instances)} nested objects and {len(userdata_refs)} userdata references")
            return True
            
        except Exception as e:
            print(f"Error copying component to clipboard: {str(e)}")
            traceback.print_exc()
            return False
    
    @staticmethod
    def _json_serializer(obj):
        """Custom JSON serializer for handling non-serializable objects"""
        return RszArrayClipboard._json_serializer(obj)
    
    @staticmethod
    def get_clipboard_data(viewer):
        """Get component data from clipboard
        
        Args:
            viewer: The viewer instance
            
        Returns:
            dict: The component data from clipboard or None if not available
        """
        clipboard_file = RszComponentClipboard.get_clipboard_file(viewer)
        return RszClipboardUtils.load_clipboard_data(clipboard_file)
    
    @staticmethod
    def has_clipboard_data(viewer):
        """Check if component clipboard data exists
        
        Args:
            viewer: The viewer instance
            
        Returns:
            bool: True if clipboard data exists, False otherwise
        """
        clipboard_file = RszComponentClipboard.get_clipboard_file(viewer)
        return os.path.exists(clipboard_file)
    
    @staticmethod
    def _deserialize_element(element_data, element_class=None):
        """Convert serialized data back to appropriate data object"""
        return RszArrayClipboard._deserialize_element(element_data, element_class)
    
    @staticmethod
    def paste_component_from_clipboard(viewer, go_instance_id, clipboard_data=None):
        """Paste a component from clipboard to a GameObject with full support for nested objects

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
                clipboard_data = RszComponentClipboard.get_clipboard_data(viewer)
                
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
                            def find_nested_instances(fields, nested_ids):
                                for field_name, field_data in fields.items():
                                    if isinstance(field_data, ObjectData) and field_data.value > 0:
                                        nested_ids.add(field_data.value)
                                        if field_data.value in viewer.scn.parsed_elements:
                                            find_nested_instances(viewer.scn.parsed_elements[field_data.value], nested_ids)
                                    elif isinstance(field_data, ArrayData):
                                        for element in field_data.values:
                                            if isinstance(element, ObjectData) and element.value > 0:
                                                nested_ids.add(element.value)
                                                if element.value in viewer.scn.parsed_elements:
                                                    find_nested_instances(viewer.scn.parsed_elements[element.value], nested_ids)
                            
                            component_nested_ids = set()
                            find_nested_instances(viewer.scn.parsed_elements[comp_instance_id], component_nested_ids)
                            
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
                
                def topological_sort(graph): #Experimenting with this currently, will probably apply it to the rest of the project
                    visited = set()
                    temp = set()
                    order = []
                    
                    def visit(node):
                        if node in temp:
                            print(f"Cyclic dependency detected at node {node}")
                            return
                        if node in visited:
                            return
                        
                        temp.add(node)
                        for neighbor in graph.get(node, set()):
                            visit(neighbor)
                        temp.remove(node)
                        visited.add(node)
                        order.append(node)
                    
                    for node in graph:
                        if node not in visited:
                            visit(node)
                            
                    return order
                
                # Get topologically sorted instances (dependencies first)
                sorted_nested_ids = topological_sort(dependency_graph)
                
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
                        RszGameObjectClipboard._setup_userdata_for_pasted_instance(
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
                    
                    new_fields = RszGameObjectClipboard._deserialize_fields_with_remapping(
                        fields_data, instance_mapping, userdata_mapping, guid_mapping
                    )
                    RszComponentClipboard._update_userdata_references(new_fields, userdata_mapping)
                    
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
    
    @staticmethod
    def _update_userdata_references(fields, userdata_mapping):
        """Update UserDataData references with correct userdata mapping
        
        Args:
            fields: The fields dictionary to update
            userdata_mapping: Mapping from old userdata IDs to new userdata IDs
        """
        for _, field_data in fields.items():
            if isinstance(field_data, UserDataData) and field_data.value in userdata_mapping:
                field_data.value = userdata_mapping[field_data.value]
            
            elif isinstance(field_data, ArrayData):
                for i, element in enumerate(field_data.values):
                    if isinstance(element, UserDataData) and element.value in userdata_mapping:
                        element.value = userdata_mapping[element.value]
