class RszInstanceOperations:
    """
    Utility class for RSZ instance operations that are common across multiple components.
    
    This centralizes operations like:
    - Finding nested objects
    - Finding instances referenced by other instances
    - Managing instance references during deletion
    """
    
    @staticmethod
    def find_nested_objects(parsed_elements, instance_id, object_table=None):
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
        for field_name, field_data in fields.items():
            if hasattr(field_data, '__class__') and field_data.__class__.__name__ == 'UserDataData' and field_data.value > 0:
                userdata_refs.add(field_data.value)
            elif hasattr(field_data, '__class__') and field_data.__class__.__name__ == 'ArrayData':
                for element in field_data.values:
                    if hasattr(element, '__class__') and element.__class__.__name__ == 'UserDataData' and element.value > 0:
                        userdata_refs.add(element.value)
                        
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
        for instance_id, fields in parsed_elements.items():
            if instance_id in deleted_ids:
                continue
                
            for field_name, field_data in fields.items():
                
                if hasattr(field_data, '__class__') and (field_data.__class__.__name__ == 'ObjectData' or field_data.__class__.__name__ == 'UserDataData'):
                    ref_id = field_data.value
                    if ref_id > 0:
                        if ref_id in deleted_ids:
                            field_data.value = 0
                        elif ref_id in id_adjustments:
                            field_data.value = id_adjustments[ref_id]
                
                elif hasattr(field_data, '__class__') and field_data.__class__.__name__ == 'ArrayData':
                    for element in field_data.values:
                        if hasattr(element, '__class__') and (element.__class__.__name__ == 'ObjectData' or element.__class__.__name__ == 'UserDataData'):
                            ref_id = element.value
                            if ref_id > 0:
                                if ref_id in deleted_ids:
                                    element.value = 0
                                elif ref_id in id_adjustments:
                                    element.value = id_adjustments[ref_id]
    
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
                
            for field_name, field_data in fields.items():
                if hasattr(field_data, '__class__'):
                    class_name = field_data.__class__.__name__
                    
                    if (class_name == 'ObjectData' or class_name == 'UserDataData') and field_data.value == instance_id:
                        return False
                        
                    elif class_name == 'ArrayData':
                        for item in field_data.values:
                            if hasattr(item, '__class__'):
                                item_class = item.__class__.__name__
                                
                                if (item_class == 'ObjectData' or item_class == 'UserDataData') and item.value == instance_id:
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
                if hasattr(field_data, '__class__'):
                    class_name = field_data.__class__.__name__
                    
                    if (class_name == 'ObjectData' or class_name == 'UserDataData') and field_data.value == instance_id:
                        if ref_id not in references:
                            references[ref_id] = []
                        references[ref_id].append((field_name, "direct"))
                    
                    elif class_name == 'ArrayData':
                        for i, item in enumerate(field_data.values):
                            if hasattr(item, '__class__'):
                                item_class = item.__class__.__name__
                                
                                if (item_class == 'ObjectData' or item_class == 'UserDataData') and item.value == instance_id:
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
            
            for field_name, field_data in fields.items():
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
        references = set()
        
        for field_name, field_data in fields.items():
            if hasattr(field_data, '__class__'):
                class_name = field_data.__class__.__name__
                
                if class_name == 'ObjectData' and field_data.value > 0:
                    references.add(field_data.value)
                
                elif class_name == 'ArrayData':
                    for element in field_data.values:
                        if hasattr(element, '__class__') and element.__class__.__name__ == 'ObjectData' and element.value > 0:
                            references.add(element.value)
        
        return references