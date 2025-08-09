"""
Array operations for RSZ files.

This file contains operations for adding and removing elements from arrays in RSZ files.
"""

from PySide6.QtWidgets import QMessageBox
from file_handlers.rsz.rsz_data_types import ObjectData, ArrayData, UserDataData, ResourceData, get_type_class, is_reference_type
from file_handlers.pyside.tree_model import DataTreeBuilder
from file_handlers.rsz.rsz_instance_operations import RszInstanceOperations

class RszArrayOperations:
    """
    Class for handling array operations in RSZ files.
    This consolidates array element creation and deletion functionality.
    """
    
    def __init__(self, viewer):
        """Initialize with a reference to the viewer"""
        self.viewer = viewer
        self.scn = viewer.scn
        self.type_registry = viewer.type_registry

    def create_array_element(self, element_type, array_data, direct_update=False, array_item=None):
        """Create a new element for an array based on type information"""
        element_class = getattr(array_data, 'element_class', None) if array_data else None
        
        if not self.type_registry or not array_data or not element_class:
            QMessageBox.warning(self.viewer, "Error", "Missing required data for array element creation")
            return None
            
        type_info, type_id = self.type_registry.find_type_by_name(element_type)
        if not type_info and array_data.element_class != ResourceData:
            QMessageBox.warning(self.viewer, "Error", f"Type not found in registry: {element_type}")
            return None
        
        if element_class == ObjectData:
            new_element = self._create_new_object_instance_for_array(type_id, type_info, element_type, array_data)
        elif element_class == UserDataData:
            new_element = self._create_new_userdata_instance_for_array(type_id, type_info, element_type, array_data)
        else:
            new_element = self.viewer._create_default_field(element_class, array_data.orig_type)
        
        if new_element:
            array_data.values.append(new_element)
            self.viewer.mark_modified()
            
            if direct_update and array_item:
                self._add_element_to_ui_direct(array_item, new_element)
            QMessageBox.information(self.viewer, "Element Added", f"New {element_type} element added successfully.")
            
        return new_element


    def _create_new_userdata_instance_for_array(self, type_id, type_info, element_type, array_data):
        """Create a new UserDataData instance with associated RSZUserDataInfo for an array element"""
        from file_handlers.rsz.rsz_object_operations import RszObjectOperations
        
        object_ops = RszObjectOperations(self.viewer)
        
        parent_data = self._find_array_parent_data(array_data)
        if not parent_data:
            raise RuntimeError("Warning: Could not find parent data for array, using fallback insertion")
        else:
            parent_instance_id, parent_field_name = parent_data
            next_instance_id = self._calculate_array_element_insertion_index(parent_instance_id, parent_field_name)

        # Ensure we never use 0 as instance_id
        if next_instance_id == 0:
            next_instance_id = 1
            
        instance_id = object_ops._create_userdata_instance_for_field(element_type, next_instance_id)
        
        if instance_id is not None and instance_id >= 0:
            userdata = UserDataData(instance_id, "", element_type)
            userdata._container_array = array_data
            if hasattr(userdata, '_container_array') and userdata._container_array:
                userdata._container_array.modified = True
            return userdata
        else:
            print("Failed to create UserDataData, returning empty")
            return UserDataData(0, "", element_type)
                

    def _create_new_object_instance_for_array(self, type_id, type_info, element_type, array_data):
        """Create a new object instance for an array element"""
        parent_data = self._find_array_parent_data(array_data) 
        if not parent_data:
            return None
        parent_instance_id, parent_field_name = parent_data
        
        # Calculate insertion index for the new instance, considering parent location
        insertion_index = self._calculate_array_element_insertion_index(parent_instance_id, parent_field_name)
        
        # Create and initialize the instance
        new_instance = self.viewer._initialize_new_instance(type_id, type_info)
        if not new_instance or new_instance.type_id == 0:
            QMessageBox.warning(self.viewer, "Error", f"Failed to create valid instance with type {element_type}")
            return None
        
        # We'll use this to hold all nested objects in a hierarchical structure
        class NestedObjectNode:
            def __init__(self, type_info, type_id, field_name=None, parent=None):
                self.type_info = type_info
                self.type_id = type_id
                self.field_name = field_name  # Field name in parent that references this
                self.parent = parent
                self.children = []
                self.instance_id = 0  # Will be set when instance is created
                self.fields = {}  # Fields will be initialized later
                self.nested_objects = set()  # To hold nested objects
                self.field_order = []  # Track field order for proper instance creation
                
            def add_child(self, child):
                self.children.append(child)
                
        # Root node for our hierarchy
        root_node = NestedObjectNode(type_info, type_id)
        
        # Step 1: Build a complete hierarchy of all nested objects recursively
        def analyze_nested_objects(node, visited_types=None):
            """Recursively analyze and collect nested objects at all levels"""
            if visited_types is None:
                visited_types = set()
                
            # Avoid infinite recursion with circular references
            type_name = node.type_info.get("name", "")
            if type_name in visited_types:
                return
            visited_types.add(type_name)
            
            # Initialize temporary fields for analysis
            node.fields = {}
            
            for field_def in node.type_info.get("fields", []):
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
                    node.fields[field_name] = field_obj
                    
                    if is_reference_type(field_obj) and field_orig_type and not field_array:
                        node.field_order.append((field_name, field_type, field_orig_type, field_obj))
                    
                    # Process direct object references
                    if isinstance(field_obj, ObjectData) and field_orig_type and not field_array:
                        # Find the nested type
                        nested_type_info, nested_type_id = self.type_registry.find_type_by_name(field_orig_type)
                        if nested_type_info and nested_type_id:
                            # Create a child node for this nested object
                            child_node = NestedObjectNode(nested_type_info, nested_type_id, field_name, node)
                            node.add_child(child_node)
                            
                            # Recursively analyze this nested object
                            analyze_nested_objects(child_node, visited_types.copy())
            
        # Start recursive analysis from root node
        analyze_nested_objects(root_node)
        
        # Step 2: Create instances respecting field order
        
        # First, create a mapping of all nodes by their type name for quick lookup
        node_by_type = {}
        def map_nodes(node):
            type_name = node.type_info.get("name", "")
            if type_name:
                node_by_type[type_name] = node
            for child in node.children:
                map_nodes(child)
        map_nodes(root_node)
        
        # Track which nodes have been created
        created_nodes = set()
        instance_map = {}  # Maps node to instance ID
        current_insertion_index = insertion_index
        
        def create_node_instances(node):
            """Create instances for a node and its dependencies in field order"""
            if node in created_nodes:
                return
                
            for field_name, field_type, field_orig_type, field_obj in node.field_order:
                if isinstance(field_obj, ObjectData):
                    child_node = None
                    for child in node.children:
                        if child.field_name == field_name:
                            child_node = child
                            break
                    
                    if child_node and child_node not in created_nodes:
                        create_node_instances(child_node)
                        
                elif isinstance(field_obj, UserDataData) and hasattr(self.scn, 'has_embedded_rsz') and self.scn.has_embedded_rsz:
                    nonlocal current_insertion_index
                    userdata_id = self.viewer.object_operations._create_userdata_instance_for_field(
                        field_orig_type, current_insertion_index
                    )
                    if userdata_id is not None:
                        field_obj.value = userdata_id
                        field_obj.string = field_orig_type
                        current_insertion_index += 1
            
            instance = self.viewer._initialize_new_instance(node.type_id, node.type_info)
            if instance and instance.type_id != 0:
                # Insert the instance
                self.viewer._insert_instance_and_update_references(current_insertion_index, instance)
                
                # Register with ID manager
                self.viewer.handler.id_manager.register_instance(current_insertion_index)
                
                # Store the instance ID
                node.instance_id = current_insertion_index
                instance_map[node] = current_insertion_index
                
                # Store the fields in parsed_elements
                self.scn.parsed_elements[current_insertion_index] = node.fields
                
                current_insertion_index += 1
            
            created_nodes.add(node)
        
        create_node_instances(root_node)
            
        # Now update all object references to point to the correct instances
        def update_references(node):
            for field_name, field_value in node.fields.items():
                if isinstance(field_value, ObjectData) and field_value.orig_type:
                    # Find the child node for this field
                    for child in node.children:
                        if child.field_name == field_name and child in instance_map:
                            field_value.value = instance_map[child]
                            break
            for child in node.children:
                update_references(child)
        
        update_references(root_node)
                    
        # Update the hierarchy in the SCN file - root node is the last node in all_nodes
        root_node_id = root_node.instance_id
        if root_node_id > 0:
            self.viewer.object_operations._update_instance_hierarchy(root_node_id, parent_instance_id)
            
            obj_data = ObjectData(root_node_id, element_type)
            self.viewer.object_operations._last_added_object = obj_data
            
            return obj_data
            
        return None


    def _add_element_to_ui_direct(self, array_item, element):
        """Add a new element directly to the tree using the provided array item"""
        model = getattr(self.viewer.tree, 'model', lambda: None)()
        if not model or not hasattr(array_item, 'raw'):
            return False
            
        array_data = array_item.raw.get('obj') if isinstance(array_item.raw, dict) else None
        if not array_data or not hasattr(array_data, 'values'):
            return False
        
        element_index = len(array_data.values) - 1
        
        if isinstance(element, (ObjectData, UserDataData)):
            embedded_context = None
            if hasattr(array_item, 'raw') and isinstance(array_item.raw, dict):
                embedded_context = array_item.raw.get('embedded_context')
            
            node_data = self.viewer._handle_reference_in_array(
                element_index, element, embedded_context, 
                getattr(embedded_context, 'instance_id', None) if embedded_context else None
            )
        else:
            # For non-reference types
            node_data = DataTreeBuilder.create_data_node(
                f"{element_index}: ", "", element.__class__.__name__, element
            )
        
        model.addChild(array_item, node_data)
        
        array_index = model.getIndexFromItem(array_item)
        self.viewer.tree.expand(array_index)
        
        return True

    def _calculate_insertion_index(self, parent_instance_id, parent_field_name):
        """Calculate the best insertion index for a new instance based on field positioning"""
        # Default to placing after parent
        insertion_index = parent_instance_id  # Default to right after the parent
        
        # Early exit for invalid parent
        if parent_instance_id >= len(self.scn.instance_infos):
            return insertion_index
            
        # Get parent type information
        parent_type_id = self.scn.instance_infos[parent_instance_id].type_id
        parent_type_info = self.type_registry.get_type_info(parent_type_id)
        
        # Early exit if type info missing
        if not parent_type_info or "fields" not in parent_type_info:
            return insertion_index
        
        # Get field position information
        field_indices = {field["name"]: idx for idx, field in enumerate(parent_type_info["fields"])}
        target_pos = field_indices.get(parent_field_name, -1)
        
        if target_pos < 0:
            return insertion_index
        
        # Find minimum reference value in fields after target position and all their nested objects
        min_later_ref = float('inf')
        
        processed_ids = set()
        
        # Recursively find all references in object fields
        def collect_references(instance_id, target_pos = -1):
            nonlocal min_later_ref, processed_ids
            
            # Avoid infinite recursion
            if instance_id in processed_ids:
                return
            processed_ids.add(instance_id)
            
            if instance_id not in self.scn.parsed_elements:
                return
                
            # Get instance type info
            if instance_id >= len(self.scn.instance_infos):
                return
            inst_type_id = self.scn.instance_infos[instance_id].type_id
            inst_type_info = self.type_registry.get_type_info(inst_type_id)
            if not inst_type_info or "fields" not in inst_type_info:
                return
                
            # Build field position mapping for this instance
            inst_field_indices = {field["name"]: idx for idx, field in enumerate(inst_type_info["fields"])}
            
            # Process all fields in this instance
            for field_name, field_data in self.scn.parsed_elements[instance_id].items():
                field_pos = inst_field_indices.get(field_name, -1)
                
                # For parent instance, only check fields after target position
                if instance_id == parent_instance_id and field_pos <= target_pos:
                    continue
                    
                # Check direct object reference
                if isinstance(field_data, ObjectData):
                    ref_id = field_data.value
                    if ref_id > 0:  # valid reference
                        min_later_ref = min(min_later_ref, ref_id)
                        # Recursively process this object's fields too
                        collect_references(ref_id)
                        
                elif isinstance(field_data, UserDataData):
                    ref_id = field_data.value
                    if ref_id > 0: 
                        min_later_ref = min(min_later_ref, ref_id)
                        collect_references(ref_id)
                        
                # Check array elements
                elif isinstance(field_data, ArrayData):
                    for elem in field_data.values:
                        if isinstance(elem, ObjectData):
                            ref_id = elem.value
                            if ref_id > 0:  # valid reference
                                min_later_ref = min(min_later_ref, ref_id)
                                # Recursively process this object's fields too
                                collect_references(ref_id)
                        elif isinstance(elem, UserDataData):
                            ref_id = elem.value
                            if ref_id > 0:
                                min_later_ref = min(min_later_ref, ref_id)
                                collect_references(ref_id)
        
        # Start collection from parent instance
        collect_references(parent_instance_id, target_pos)
        
        # Update index if we found a valid reference
        if min_later_ref != float('inf'):
            insertion_index = min_later_ref
        
        return insertion_index

    def _calculate_array_element_insertion_index(self, parent_instance_id, parent_field_name):
        """
        Calculate the correct insertion index for new array element instances.
        This ensures instances are created in the proper position relative to the parent's field order.
        
        For arrays that come between other fields in the type definition, this method ensures
        that array element instances are inserted at the correct position to maintain field ordering.
        """
        base_insertion_index = self._calculate_insertion_index(parent_instance_id, parent_field_name)
        
        array_data = None
        if parent_instance_id in self.scn.parsed_elements:
            array_data = self.scn.parsed_elements[parent_instance_id].get(parent_field_name)
            
        if not array_data or not isinstance(array_data, ArrayData):
            return base_insertion_index
            
        if array_data.values:
            max_instance_id = 0
            
            for elem in array_data.values:
                if is_reference_type(elem) and elem.value > 0:
                    max_instance_id = max(max_instance_id, elem.value)
                    
                    nested_ids = self._collect_all_nested_objects(elem.value)
                    if nested_ids:
                        max_instance_id = max(max_instance_id, max(nested_ids))
            
            if max_instance_id > 0:
                return max_instance_id + 1
                
        return base_insertion_index

    def _find_array_parent_data(self, array_data):
        """Find parent instance and field for an array"""
        for instance_id, fields in self.scn.parsed_elements.items():
            for field_name, field_data in fields.items():
                if field_data is array_data:
                    return instance_id, field_name
        
        if hasattr(array_data, '_owning_instance_id') and hasattr(array_data, '_owning_field'):
            return array_data._owning_instance_id, array_data._owning_field
        
        if hasattr(self.viewer, 'embedded_contexts'):
            for context in self.viewer.embedded_contexts:
                if hasattr(context, 'embedded_instances'):
                    for instance_id, fields in context.embedded_instances.items():
                        if isinstance(fields, dict):
                            for field_name, field_data in fields.items():
                                if field_data is array_data:
                                    return instance_id, field_name
        
        for instance_id, fields in self.scn.parsed_elements.items():
            if isinstance(fields, dict):
                for field_name, field_data in fields.items():
                    if hasattr(field_data, 'embedded_instances'):
                        for emb_id, emb_fields in field_data.embedded_instances.items():
                            if isinstance(emb_fields, dict):
                                for emb_field_name, emb_field_data in emb_fields.items():
                                    if emb_field_data is array_data:
                                        return emb_id, emb_field_name
        
        QMessageBox.warning(self.viewer, "Error", "Could not find array's parent instance")
        return None


    def delete_array_element(self, array_data, element_index):
        """Handle complex deletion of an array element with reference updates"""
        if not array_data or not hasattr(array_data, 'values') or element_index >= len(array_data.values):
            return False
            
        # Get the element to delete
        element = array_data.values[element_index]
        
        # Get instance ID and type of reference based on element type
        instance_id = 0
        ref_type = None
        if isinstance(element, ObjectData) and element.value > 0:
            instance_id = element.value
            ref_type = "object"
        elif isinstance(element, UserDataData) and element.value > 0:
            instance_id = element.value
            ref_type = "userdata"
        
        # If we have a reference to delete
        if instance_id > 0 and ref_type:
            # First verify the instance exists
            if instance_id >= len(self.scn.instance_infos):
                print(f"Warning: Invalid {ref_type} instance ID {instance_id}")
                del array_data.values[element_index]
                self.viewer.mark_modified()
                return True
                
            # Check if this instance is referenced elsewhere
            is_referenced_elsewhere = self._check_instance_referenced_elsewhere(
                instance_id, array_data, element_index, ref_type
            )
            
            if is_referenced_elsewhere:
                print(f"{ref_type.capitalize()} {instance_id} is referenced elsewhere - only removing reference")
                # Just remove the reference, not the actual instance
                del array_data.values[element_index]
                self.viewer.mark_modified()
                return True
            
            print(f"Deleting {ref_type} instance {instance_id}")
            
            # First remove the array element to ensure it doesn't interfere with deletion
            del array_data.values[element_index]
            
            # Delete the instance and all its nested objects
            if self._delete_instance_for_reference(instance_id, ref_type):
                self.viewer.mark_modified()
                return True
            
            return False
        else:
            # For basic element types, just remove from the array
            del array_data.values[element_index]
            self.viewer.mark_modified()
            return True
    
    def _delete_instance_for_reference(self, instance_id, ref_type):
        """
        Generic method to delete an instance referenced from an array.
        Works for both ObjectData and UserDataData references.
        
        Args:
            instance_id: The instance ID to delete
            ref_type: 'object' or 'userdata'
        
        Returns:
            bool: Success or failure
        """
        # For ObjectData with nested children, use the full deletion method
        if ref_type == "object":
            return self._delete_instance_and_children(instance_id)
        
        # For UserDataData or other simpler elements, use a streamlined approach
        all_nested_objects = {instance_id}
        
        try:
            # 1. Prepare ID adjustments
            id_adjustments = {}  # old_id -> new_id mapping
            max_instance_id = len(self.scn.instance_infos)
            
            for i in range(max_instance_id):
                if i in all_nested_objects:
                    # This instance will be deleted
                    id_adjustments[i] = -1  # -1 indicates deletion
                else:
                    # Calculate how many deleted instances are before this one
                    offset = sum(1 for deleted_id in all_nested_objects if deleted_id < i)
                    if offset > 0:
                        id_adjustments[i] = i - offset
            
            # 2. Update references first
            self._update_references_before_deletion(all_nested_objects, id_adjustments)
            
            # 3. Remove from instance_infos
            new_instance_infos = []
            for i, info in enumerate(self.scn.instance_infos):
                if i not in all_nested_objects:
                    new_instance_infos.append(info)
            self.scn.instance_infos = new_instance_infos
            
            # 4. Update object table
            for i, idx in enumerate(self.scn.object_table):
                if idx in all_nested_objects:
                    self.scn.object_table[i] = 0
                elif idx in id_adjustments:
                    self.scn.object_table[i] = id_adjustments[idx]
            
            # 5. Clean up the instance data
            if instance_id in self.scn.parsed_elements:
                del self.scn.parsed_elements[instance_id]
            
            # 6. For UserData, clean up userdata specifically
            if ref_type == "userdata":
                self.viewer._cleanup_userdata_for_instance(instance_id)
            
            # 7. Update remaining parsed elements
            updated_parsed_elements = {}
            for idx, fields in self.scn.parsed_elements.items():
                if idx in all_nested_objects:
                    continue  # Skip deleted instances
                new_id = id_adjustments.get(idx, idx)
                if new_id >= 0:  # Only include non-deleted instances
                    updated_parsed_elements[new_id] = fields
            self.scn.parsed_elements = updated_parsed_elements
            
            # 8. Update instance hierarchy
            updated_hierarchy = {}
            for idx, data in self.scn.instance_hierarchy.items():
                if idx in all_nested_objects:
                    continue  # Skip deleted instances
                
                new_id = id_adjustments.get(idx, idx)
                if new_id < 0:
                    continue  # Skip if this gets deleted
                    
                new_children = []
                for child_id in data["children"]:
                    if child_id in all_nested_objects:
                        continue  # Skip deleted children
                    new_child_id = id_adjustments.get(child_id, child_id)
                    if new_child_id >= 0:
                        new_children.append(new_child_id)
                
                parent_id = data["parent"]
                if parent_id in all_nested_objects:
                    parent_id = None  # Parent is being deleted
                elif parent_id in id_adjustments:
                    parent_id = id_adjustments[parent_id]
                
                updated_hierarchy[new_id] = {"children": new_children, "parent": parent_id}
            self.scn.instance_hierarchy = updated_hierarchy
            
            # 9. Update userdata references
            self.viewer._update_userdata_references(all_nested_objects, id_adjustments)
            
            # 10. Update ID manager
            for deleted_id in all_nested_objects:
                self.viewer.handler.id_manager.remove_instance(deleted_id)
                
            for old_id, new_id in id_adjustments.items():
                if new_id >= 0 and old_id != new_id:
                    self.viewer.handler.id_manager.update_instance_id(old_id, new_id)
            
            return True
        except Exception as e:
            print(f"Error during {ref_type} deletion: {str(e)}")
            import traceback
            traceback.print_exc()
            return False
    
    def _is_reference_match(self, ref_obj, instance_id, ref_type):
        """Helper to check if a reference object matches the criteria"""
        if ref_type == "object":
            return isinstance(ref_obj, ObjectData) and ref_obj.value == instance_id
        elif ref_type == "userdata":
            return isinstance(ref_obj, UserDataData) and ref_obj.value == instance_id
        return False
    
    def _check_instance_referenced_elsewhere(self, instance_id, current_array, current_index=None, ref_type="object"):
        """
        Unified method to check if an instance is referenced from places outside current context.
        Works for both ObjectData and UserDataData references.
        
        Args:
            instance_id: The instance ID to check
            current_array: The array containing the reference we're deleting
            current_index: Index of the element we're deleting (optional)
            ref_type: 'object' or 'userdata'
            
        Returns:
            bool: True if referenced elsewhere, False if only referenced within current element
        """
        # First find the parent component of the array
        parent_data = self._find_array_parent_data(current_array)
        if not parent_data:
            # If we can't find the parent, be conservative and assume referenced elsewhere
            return True
            
        parent_instance_id, _ = parent_data
        
        # Define range bounds for reference checking
        min_id = 0  # Default lower bound
        max_id = parent_instance_id  # Default upper bound is current component
        
        # Find the component ID in object table
        component_object_id = -1
        for i, obj_id in enumerate(self.scn.object_table):
            if obj_id == parent_instance_id:
                component_object_id = i
                break
        
        # If we found the component in the object table, check if it's a GameObject
        if component_object_id >= 0:
            # Find if this is a component of a GameObject
            for go in self.scn.gameobjects:
                if go.id < component_object_id and component_object_id <= go.id + go.component_count:
                    # This is a component of a GameObject
                    
                    # Calculate which component index this is (1-based from GameObject)
                    component_index = component_object_id - go.id
                    
                    # If this isn't the first component, find the previous component's instance ID
                    if component_index > 1:  # If we're not the first component
                        prev_comp_obj_id = go.id + (component_index - 1)
                        if prev_comp_obj_id < len(self.scn.object_table):
                            prev_comp_instance_id = self.scn.object_table[prev_comp_obj_id]
                            if prev_comp_instance_id > 0:
                                # Previous component's ID becomes our lower bound
                                min_id = prev_comp_instance_id
                                # Current component's ID is our upper bound
                                max_id = parent_instance_id
                    break
        
        print(f"Checking if {ref_type} {instance_id} is referenced outside range: {min_id}-{max_id}")
        
        # Check all instances for references to our target instance
        for check_id, fields in self.scn.parsed_elements.items():
            # Skip if this instance is in our component range
            if min_id < check_id <= max_id:  # Exclusive range check
                continue
                
            for field_name, field_data in fields.items():
                # Check direct references based on reference type
                if self._is_reference_match(field_data, instance_id, ref_type):
                    print(f"  Found external reference from instance {check_id}, field {field_name}")
                    return True
                    
                # Check references in arrays
                elif isinstance(field_data, ArrayData):
                    for i, item in enumerate(field_data.values):
                        if self._is_reference_match(item, instance_id, ref_type):
                            print(f"  Found external reference from instance {check_id}, field {field_name}[{i}]")
                            return True
                            
                # Check embedded instances within UserData
                if hasattr(field_data, 'embedded_instances'):
                    for emb_id, emb_fields in field_data.embedded_instances.items():
                        if isinstance(emb_fields, dict):
                            for emb_field_name, emb_field_data in emb_fields.items():
                                if self._is_reference_match(emb_field_data, instance_id, ref_type):
                                    return True
                                    
                                elif isinstance(emb_field_data, ArrayData):
                                    for i, item in enumerate(emb_field_data.values):
                                        if self._is_reference_match(item, instance_id, ref_type):
                                            return True
        
        # Special additional check for arrays: 
        # If this is the array we're modifying, check other indices for references
        if current_index is not None and isinstance(current_array, ArrayData):
            for i, item in enumerate(current_array.values):
                if i != current_index:  # Skip the item we're deleting
                    if self._is_reference_match(item, instance_id, ref_type):
                        print(f"  Found another reference in same array at index {i}")
                        return True
        
        # No references found outside our context
        return False

    def _delete_instance_and_children(self, instance_id):
        """
        Completely delete an instance and all its children, updating all related data structures.
        """
        if instance_id <= 0 or instance_id >= len(self.viewer.scn.instance_infos):
            return False
            
        # First, collect ALL nested objects recursively 
        all_nested_objects = self._collect_all_nested_objects(instance_id)
        
        # Add main instance to the deletion set
        all_nested_objects.add(instance_id)
        
        print(f"Direct deletion of instance {instance_id} with {len(all_nested_objects)-1} nested objects")
        for nested_id in sorted(all_nested_objects):
            print(f"  - {nested_id}: {self._get_instance_type_name(nested_id)}")
        
        try:
            # Remove all instances from all data structures
            # Process in reverse order (deepest nested objects first)
            deleted_ids = sorted(all_nested_objects, reverse=True)
            
            # 1. First, prepare a list of adjustments for higher instance IDs
            # For each deleted instance, instances with higher IDs need to be decreased
            id_adjustments = {}  # old_id -> new_id mapping
            
            # Calculate adjustments for each instance ID
            max_instance_id = len(self.scn.instance_infos)
            for i in range(max_instance_id):
                if i in all_nested_objects:
                    id_adjustments[i] = -1 
                else:
                    offset = sum(1 for deleted_id in all_nested_objects if deleted_id < i)
                    if offset > 0:
                        id_adjustments[i] = i - offset
            
            #  Diagnostic purposes
            #print("ID Adjustments:")
            #for old_id, new_id in sorted(id_adjustments.items()):
            #    if new_id >= 0: 
            #        print(f"  {old_id} -> {new_id}")
            
            # 2. Delete instances and update data structures
            
            # Update all object references in the remaining elements first
            self._update_references_before_deletion(all_nested_objects, id_adjustments)
            
            # Now actually remove the instances from instance_infos
            # We need to do this in reverse order to avoid index issues
            new_instance_infos = []
            for i, info in enumerate(self.scn.instance_infos):
                if i not in all_nested_objects:
                    new_instance_infos.append(info)
            
            self.scn.instance_infos = new_instance_infos
            
            # 3. Remove from object table (replacing with 0)
            for i, instance_id in enumerate(self.scn.object_table):
                if instance_id in all_nested_objects:
                    self.scn.object_table[i] = 0
                elif instance_id in id_adjustments:
                    self.scn.object_table[i] = id_adjustments[instance_id]
            
            # 4. Update other data structures
            # Remove deleted instances from parsed_elements
            for deleted_id in deleted_ids:
                if deleted_id in self.scn.parsed_elements:
                    del self.scn.parsed_elements[deleted_id]
                
            # Update keys in parsed_elements for shifted instances
            updated_parsed_elements = {}
            for instance_id, fields in self.scn.parsed_elements.items():
                if instance_id in id_adjustments and id_adjustments[instance_id] >= 0:
                    updated_parsed_elements[id_adjustments[instance_id]] = fields
                else:
                    updated_parsed_elements[instance_id] = fields
            self.scn.parsed_elements = updated_parsed_elements
            
            # Similar updates for instance_hierarchy
            updated_hierarchy = {}
            for instance_id, data in self.scn.instance_hierarchy.items():
                if instance_id in all_nested_objects:
                    continue 
                
                # Calculate new ID
                new_id = id_adjustments.get(instance_id, instance_id)
                if new_id < 0:
                    continue  
                    
                # Update children list
                new_children = []
                for child_id in data["children"]:
                    if child_id in all_nested_objects:
                        continue 
                    new_child_id = id_adjustments.get(child_id, child_id)
                    if new_child_id >= 0:
                        new_children.append(new_child_id)
                
                # Update parent
                parent_id = data["parent"]
                if parent_id in all_nested_objects:
                    parent_id = None
                elif parent_id in id_adjustments:
                    parent_id = id_adjustments[parent_id]
                
                updated_hierarchy[new_id] = {"children": new_children, "parent": parent_id}
            
            self.scn.instance_hierarchy = updated_hierarchy
            
            # 5. Update userdata - reuse methods from RszViewer
            # First, clean up userdata for all deleted instances
            for deleted_id in deleted_ids:
                #  Viewer's method to to clean up userdata
                self.viewer._cleanup_userdata_for_instance(deleted_id)
                
            # Then update remaining userdata references based on id_adjustments
            # Create a mapping for userdata references
            userdata_mapping = {}
            for old_id, new_id in id_adjustments.items():
                if new_id >= 0:  # Only include non-deleted instances
                    userdata_mapping[old_id] = new_id
                    
            # Update all userdata references using the RszViewer method
            # Pass empty deleted_ids since we've already handled deletions
            self.viewer._update_userdata_references(set(), userdata_mapping)
            
            
            # Final step - update ID manager
            for deleted_id in all_nested_objects:
                self.viewer.handler.id_manager.remove_instance(deleted_id)
                
            for old_id, new_id in id_adjustments.items():
                if new_id >= 0 and old_id != new_id:
                    self.viewer.handler.id_manager.update_instance_id(old_id, new_id)
            
            return True
        except Exception as e:
            print(f"Error during instance deletion: {str(e)}")
            import traceback
            traceback.print_exc()
            return False

    def _collect_all_nested_objects(self, root_instance_id):
        """
        Collect ALL nested objects that are owned exclusively by the given instance.
        Uses hierarchical ownership rules to identify true nested objects.
        
        Returns:
            set: Set of nested object instance IDs
        """
        nested_objects = set()
        
        processed_ids = set()
        
        # Get metadata about the instance we're deleting
        root_type_id = 0
        if 0 <= root_instance_id < len(self.scn.instance_infos):
            root_type_id = self.scn.instance_infos[root_instance_id].type_id
            
        root_type_name = ""
        type_info = self.type_registry.get_type_info(root_type_id) if root_type_id > 0 else None
        if type_info:
            root_type_name = type_info.get("name", "")
            
        print(f"Analyzing nested objects for instance {root_instance_id} (type: {root_type_name})")
        
        # Exclude these instances from being considered as nested
        excluded_ids = set(self.scn.object_table) 
        excluded_ids.add(0)
        
        # First, get all objects that are exclusively referenced from this instance
        def explore_instance(instance_id):
            """Recursively explore an instance to find truly nested objects"""
            if instance_id in processed_ids:
                return
            processed_ids.add(instance_id)
            
            if instance_id not in self.scn.parsed_elements:
                return
                
            # Get instance fields
            fields = self.scn.parsed_elements[instance_id]
            
            # Try position-based detection first (most reliable for adjacent objects)
            position_based_nested = set()
            
            # Find where this instance is in the object table (if it is)
            object_table_index = -1
            for i, obj_id in enumerate(self.scn.object_table):
                if obj_id == instance_id:
                    object_table_index = i
                    break
            
            # Only use position detection if this instance isn't in the object table or it's
            # at a position where we can determine adjacent objects
            if object_table_index < 0:
                # This object itself isn't in the object table - safe to use positioned-based detection
                from file_handlers.rsz.rsz_instance_operations import RszInstanceOperations
                position_based_nested = RszInstanceOperations.find_nested_objects(
                    self.scn.parsed_elements, instance_id, self.scn.object_table
                )
            
            # Check each field for object references
            for field_name, field_data in fields.items():
                if isinstance(field_data, ObjectData) and field_data.value > 0:
                    ref_id = field_data.value
                    
                    if ref_id in excluded_ids:
                        continue
                        
                    # Check if this is a nested object:
                    # 1. Not already processed
                    # 2. Not a reference to itself
                    # 3. Valid reference within instance range
                    if (ref_id != instance_id and 
                            ref_id not in processed_ids and 
                            ref_id < len(self.scn.instance_infos)):
                        
                        # Add as potential nested object - this is a direct reference so likely a nested object
                        nested_objects.add(ref_id)
                        
                        # Recursively explore this reference to find its nested objects
                        explore_instance(ref_id)
                        
                # Check array elements
                elif isinstance(field_data, ArrayData):
                    for element in field_data.values:
                        if isinstance(element, ObjectData) and element.value > 0:
                            ref_id = element.value
                            
                            # Skip excluded IDs
                            if ref_id in excluded_ids:
                                continue
                                
                            # Similar checks as above
                            if (ref_id != instance_id and 
                                    ref_id not in processed_ids and 
                                    ref_id < len(self.scn.instance_infos)):
                                
                                # Array elements might reference shared objects, so we need to verify ownership
                                # Check if this object is referenced only from here
                                is_exclusive = self._is_exclusively_referenced_from(ref_id, instance_id)
                                
                                if is_exclusive:
                                    nested_objects.add(ref_id)
                                    
                                    # Recursively explore this reference
                                    explore_instance(ref_id)
            
            # Add position-based nested objects last (after checking fields)
            # but only if they haven't been processed already
            for nested_id in position_based_nested:
                if nested_id not in processed_ids and nested_id not in excluded_ids:
                    # Extra check: only add position-based objects when they're close
                    # to the parent (adjacent IDs) for safety
                    if abs(nested_id - instance_id) <= 2:
                        nested_objects.add(nested_id)
                        explore_instance(nested_id)
        
        # Start exploration from the root instance
        explore_instance(root_instance_id)
        
        print(f"Found {len(nested_objects)} nested objects for instance {root_instance_id}:")
        for nested_id in sorted(nested_objects):
            nested_type_id = self.scn.instance_infos[nested_id].type_id if nested_id < len(self.scn.instance_infos) else 0
            nested_type = self.type_registry.get_type_info(nested_type_id) if nested_type_id > 0 else None
            nested_type_name = nested_type.get("name", "Unknown") if nested_type else "Unknown"
            print(f"  - Nested object {nested_id}: {nested_type_name}")
            
        return nested_objects
    def _get_instance_type_name(self, instance_id):
        """Get the type name for an instance ID for debugging"""
        if instance_id < 0 or instance_id >= len(self.scn.instance_infos):
            return "Invalid ID"
        
        type_id = self.scn.instance_infos[instance_id].type_id
        type_info = self.type_registry.get_type_info(type_id) if self.type_registry else None
        
        return type_info.get("name", f"Unknown (ID: {type_id})") if type_info else f"Unknown Type (ID: {type_id})"

    def _update_references_before_deletion(self, deleted_ids, id_adjustments):
        RszInstanceOperations.update_references_before_deletion(
            self.scn.parsed_elements, deleted_ids, id_adjustments
        )

    def _is_exclusively_referenced_from(self, instance_id, source_id):
        return RszInstanceOperations.is_exclusively_referenced_from(
            self.scn.parsed_elements, instance_id, source_id, self.scn.object_table
        )