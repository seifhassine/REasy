from file_handlers.rsz.rsz_data_types import is_reference_type
class RszInstanceOperations:
    """
    Utility class for RSZ instance operations that are common across multiple components.
    
    This centralizes operations like:
    - Finding nested objects
    - Finding instances referenced by other instances
    - Managing instance references during deletion
    """
    
    @staticmethod
    def find_nested_objects(parsed_elements, instance_id, object_table=None, is_component_deletion = False):
        """
        Find instance IDs of nested objects that aren't in the object table.
        
        Args:
            parsed_elements: Dictionary of all parsed elements
            instance_id: The base instance ID to find nested objects for
            object_table: Optional object table to exclude IDs from
            
        Returns:
            set: Set of nested object instance IDs
        """
        nested_objects = set()
        
        if object_table is None:
            object_table = []
            
        object_table_ids = set(object_table)
        
        prev_instance_id = 0
        if(is_component_deletion):
            for id_ in object_table:
                if id_ > 0 and id_ < instance_id and id_ > prev_instance_id:
                    prev_instance_id = id_
        else:      
            base_object_idx = -1
            
            for i, id_ in enumerate(object_table):
                if id_ == instance_id:
                    base_object_idx = i
                    break
                    
            if base_object_idx <= 0:
                return nested_objects
                
            prev_instance_id = next((id_ for id_ in reversed(object_table[:base_object_idx]) if id_ > 0), 0)
        
        for potential_nested_id in range(prev_instance_id + 1, instance_id):
            if (potential_nested_id > 0 and 
                potential_nested_id not in object_table_ids):
                nested_objects.add(potential_nested_id)
                
        return nested_objects
        
    @staticmethod
    def find_userdata_references(fields, userdata_refs):
        """
        Find all UserDataData references in fields
        
        Args:
            fields: Dictionary of fields to search
            userdata_refs: Set to collect UserDataData references
        """
        from file_handlers.rsz.utils.rsz_field_utils import collect_field_references
        
        def collector(ref_obj):
            if ref_obj.__class__.__name__ == 'UserDataData' and ref_obj.value > 0:
                userdata_refs.add(ref_obj.value)
        
        collect_field_references(fields, collector)
                        
    @staticmethod
    def update_references_before_deletion(parsed_elements, deleted_ids, id_adjustments):
        """
        Update all references in remaining instances before deletion.
        This ensures object references remain valid after deletion.
        
        Args:
            parsed_elements: Dictionary of all parsed elements
            deleted_ids: Set of instance IDs to be deleted
            id_adjustments: Dict mapping old_instance_id -> new_instance_id
        """
        from file_handlers.rsz.utils.rsz_field_utils import update_references_with_mapping
        
        for instance_id, fields in parsed_elements.items():
            if instance_id in deleted_ids:
                continue
            
            update_references_with_mapping(fields, id_adjustments, deleted_ids)
    
    @staticmethod
    def is_exclusively_referenced_from(parsed_elements, instance_id, source_id, object_table=None):
        """
        Check if an instance is exclusively referenced from the given source instance.
        
        Args:
            parsed_elements: Dictionary of all parsed elements
            instance_id: The instance ID to check references for
            source_id: The source instance ID that should be the only referencer
            object_table: Optional object table to check if the instance is in it
            
        Returns:
            bool: True if instance_id is only referenced from source_id, False otherwise
        """
        if instance_id <= 0:
            return False
            
        if object_table and instance_id in object_table:
            return False
            
        for check_id, fields in parsed_elements.items():
            if check_id == source_id:
                continue 
                
            for _, field_data in fields.items():
                class_name = field_data.__class__.__name__
                if class_name in ('ObjectData', 'UserDataData') and field_data.value == instance_id:
                    return False
                elif class_name == 'ArrayData':
                    for item in field_data.values:
                        if is_reference_type(item) and item.value == instance_id:
                            return False
            
        return True 
        
    @staticmethod
    def find_all_instance_references(parsed_elements, instance_id):
        """
        Find all instances that reference the given instance ID.
        
        Args:
            parsed_elements: Dictionary of all parsed elements
            instance_id: The instance ID to find references for
            
        Returns:
            dict: Dictionary mapping referencing instance IDs to fields that reference the target
        """
        references = {}
        
        for ref_id, fields in parsed_elements.items():
            for field_name, field_data in fields.items():
                class_name = field_data.__class__.__name__
                
                if class_name in ('ObjectData', 'UserDataData') and field_data.value == instance_id:
                    if ref_id not in references:
                        references[ref_id] = []
                    references[ref_id].append((field_name, "direct"))
                
                elif class_name == 'ArrayData':
                    for i, item in enumerate(field_data.values):
                        
                        if is_reference_type(item) and item.value == instance_id:
                            if ref_id not in references:
                                references[ref_id] = []
                            references[ref_id].append((f"{field_name}[{i}]", "array_object"))
    
        return references
        
    @staticmethod
    def collect_all_nested_objects(parsed_elements, root_instance_id, object_table=None):
        """
        Collect ALL nested objects that are owned exclusively by the given instance.
        Uses hierarchical ownership rules to identify true nested objects.
        
        Args:
            parsed_elements: Dictionary of all parsed elements
            root_instance_id: The root instance ID to collect nested objects for
            object_table: Optional object table to exclude IDs from
            
        Returns:
            set: Set of nested object instance IDs
        """
        nested_objects = set()
        processed_ids = set()
        
        object_table_ids = set() if object_table is None else set(object_table)
        object_table_ids.add(0)
        
        def explore_instance(instance_id):
            """Recursively explore an instance to find truly nested objects"""
            if instance_id in processed_ids:
                return
            processed_ids.add(instance_id)
            
            if instance_id not in parsed_elements:
                return
                
            fields = parsed_elements[instance_id]
            
            position_based_nested = RszInstanceOperations.find_nested_objects(
                parsed_elements, instance_id, object_table
            )
            
            for _, field_data in fields.items():
                if field_data.__class__.__name__ == 'ObjectData' and field_data.value > 0:
                    ref_id = field_data.value
                    
                    if ref_id in object_table_ids:
                        continue
                        
                    # Check if this is a nested object:
                    # 1. Not already processed
                    # 2. Not a reference to itself
                    # 3. Valid reference
                    if (ref_id != instance_id and 
                            ref_id not in processed_ids):
                        
                        nested_objects.add(ref_id)
                        
                        explore_instance(ref_id)
                        
                elif field_data.__class__.__name__ == 'ArrayData':
                    for element in field_data.values:
                        if element.__class__.__name__ == 'ObjectData' and element.value > 0:
                            ref_id = element.value
                            
                            if ref_id in object_table_ids:
                                continue
                                
                            if (ref_id != instance_id and 
                                    ref_id not in processed_ids):
                                
                                is_exclusive = RszInstanceOperations.is_exclusively_referenced_from(
                                    parsed_elements, ref_id, instance_id, object_table
                                )
                                
                                if is_exclusive:
                                    nested_objects.add(ref_id)
                                    
                                    explore_instance(ref_id)
            
            for nested_id in position_based_nested:
                if nested_id not in processed_ids and nested_id not in object_table_ids:
                    nested_objects.add(nested_id)
                    explore_instance(nested_id)
        
        explore_instance(root_instance_id)
        return nested_objects
        
    @staticmethod
    def find_object_references(fields):
        """
        Find all object references in a set of fields
        
        Args:
            fields: Dictionary of fields to search
            
        Returns:
            set: Set of referenced object IDs
        """
        from file_handlers.rsz.utils.rsz_field_utils import collect_field_references
        
        references = set()
        
        def collector(ref_obj):
            if ref_obj.__class__.__name__ == 'ObjectData' and ref_obj.value > 0:
                references.add(ref_obj.value)
        
        collect_field_references(fields, collector)
        
        return references
    
    @staticmethod
    def topological_sort(dependency_graph):
        """
        Perform topological sort on a dependency graph.
        
        Args:
            dependency_graph: Dictionary mapping node -> set of dependent nodes
            
        Returns:
            list: Nodes in topological order (dependencies first)
        """
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
            for neighbor in dependency_graph.get(node, set()):
                visit(neighbor)
            temp.remove(node)
            visited.add(node)
            order.append(node)
        
        for node in dependency_graph:
            if node not in visited:
                visit(node)
                
        return order