from file_handlers.rsz.utils.rsz_field_utils import (
    collect_object_reference_values,
    collect_userdata_reference_values,
    iter_field_reference_entries,
    update_references_with_mapping,
)
from file_handlers.rsz.rsz_data_types import ObjectData, UserDataData


class RszInstanceOperations:
    """
    Utility class for RSZ instance operations that are common across multiple components.
    
    This centralizes operations like:
    - Finding nested objects
    - Finding instances referenced by other instances
    - Managing instance references during deletion
    """

    @staticmethod
    def build_deletion_id_adjustments(instance_count, deleted_ids, include_deleted=False):
        """Build the sparse ID mapping produced by deleting instances.

        Args:
            instance_count: Instance count before the deletions are applied.
            deleted_ids: Collection of instance IDs being deleted.
            include_deleted: Include deleted IDs in the result with a value of -1.

        Returns:
            dict: Old instance IDs mapped to their compacted IDs. Unchanged IDs are
            omitted from the sparse mapping.
        """
        deleted_ids = set(deleted_ids)
        deleted_sorted = sorted(deleted_ids)
        id_adjustments = {}

        for old_id in range(instance_count):
            if old_id in deleted_ids:
                if include_deleted:
                    id_adjustments[old_id] = -1
                continue

            new_id = old_id
            for deleted_id in deleted_sorted:
                if deleted_id < old_id:
                    new_id -= 1
                else:
                    break

            if new_id != old_id:
                id_adjustments[old_id] = new_id

        return id_adjustments
    
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
        userdata_refs.update(collect_userdata_reference_values(fields))

    @staticmethod
    def iter_instance_references(parsed_elements, excluded_instance_ids=()):
        """Yield every RSZ reference together with its source location.

        The RSZ instance namespace is shared by object and userdata references, so
        callers that need type-specific behavior can filter on ``ref_obj`` without
        reimplementing direct-field and array traversal.
        """
        excluded_instance_ids = set(excluded_instance_ids)
        for source_id, fields in parsed_elements.items():
            if source_id in excluded_instance_ids or not isinstance(fields, dict):
                continue
            for field_name, ref_obj, array_index in iter_field_reference_entries(fields):
                yield source_id, field_name, ref_obj, array_index

    @staticmethod
    def update_references_before_deletion(parsed_elements, deleted_ids, id_mapping):
        for instance_id, fields in parsed_elements.items():
            if instance_id not in deleted_ids:
                update_references_with_mapping(fields, id_mapping, deleted_ids)

    @staticmethod
    def remap_instance_fields(
        parsed_elements, id_mapping, deleted_ids=(), update_references=True
    ):
        """Remap instance-keyed fields and every reference they contain."""
        deleted_ids = set(deleted_ids)
        remapped = {}
        for instance_id, fields in parsed_elements.items():
            if instance_id in deleted_ids:
                continue
            if update_references:
                update_references_with_mapping(fields, id_mapping, deleted_ids)
            new_id = id_mapping.get(instance_id, instance_id)
            if new_id >= 0:
                remapped[new_id] = fields
        return remapped

    @staticmethod
    def remap_hierarchy(hierarchy, id_mapping, deleted_ids=()):
        """Remap either supported RSZ hierarchy representation.

        Standard hierarchies store ``{"children": ..., "parent": ...}``; some
        embedded structures store a direct child list. Keeping both shapes here
        prevents insertion/deletion paths from drifting apart.
        """
        deleted_ids = set(deleted_ids)
        remapped = {}
        for instance_id, data in hierarchy.items():
            if instance_id in deleted_ids:
                continue
            new_id = id_mapping.get(instance_id, instance_id)
            if new_id < 0:
                continue

            if isinstance(data, dict):
                children = [
                    id_mapping.get(child_id, child_id)
                    for child_id in data.get("children", [])
                    if child_id not in deleted_ids
                    and id_mapping.get(child_id, child_id) >= 0
                ]
                parent_id = data.get("parent")
                if parent_id in deleted_ids:
                    parent_id = None
                elif parent_id is not None:
                    parent_id = id_mapping.get(parent_id, parent_id)
                    if parent_id < 0:
                        parent_id = None
                remapped[new_id] = {"children": children, "parent": parent_id}
            else:
                remapped[new_id] = [
                    id_mapping.get(child_id, child_id)
                    for child_id in data
                    if child_id not in deleted_ids
                    and id_mapping.get(child_id, child_id) >= 0
                ]
        return remapped

    @staticmethod
    def find_ordered_insertion_boundary(
        parsed_elements,
        instance_infos,
        type_registry,
        parent_instance_id,
        parent_field_name,
    ):
        """Find the first referenced instance belonging to a later parent field."""
        boundary = parent_instance_id
        if parent_instance_id >= len(instance_infos):
            return boundary

        parent_type = type_registry.get_type_info(
            instance_infos[parent_instance_id].type_id
        )
        parent_fields = parent_type.get("fields", []) if parent_type else []
        parent_positions = {
            field["name"]: index for index, field in enumerate(parent_fields)
        }
        target_position = parent_positions.get(parent_field_name, -1)
        if target_position < 0:
            return boundary

        candidates = []
        visited = set()

        def visit(instance_id):
            if instance_id in visited or instance_id not in parsed_elements:
                return
            if instance_id >= len(instance_infos):
                return
            visited.add(instance_id)

            type_info = type_registry.get_type_info(instance_infos[instance_id].type_id)
            fields = type_info.get("fields", []) if type_info else []
            positions = {
                field["name"]: index for index, field in enumerate(fields)
            }
            for field_name, ref_obj, _ in iter_field_reference_entries(
                parsed_elements[instance_id]
            ):
                if (
                    instance_id == parent_instance_id
                    and positions.get(field_name, -1) <= target_position
                ):
                    continue
                if ref_obj.value > 0:
                    candidates.append(ref_obj.value)
                    visit(ref_obj.value)

        visit(parent_instance_id)
        return min(candidates) if candidates else boundary
    
    @staticmethod
    def is_exclusively_referenced_from(
        parsed_elements,
        instance_id,
        source_id,
        object_table=None,
        reference_type=None,
    ):
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
            
        for check_id, _, ref_obj, _ in RszInstanceOperations.iter_instance_references(
            parsed_elements, (source_id,)
        ):
            if reference_type is not None and not isinstance(ref_obj, reference_type):
                continue
            if ref_obj.value == instance_id:
                return False
            
        return True 

    @staticmethod
    def collect_owned_instances(
        parsed_elements,
        root_instance_id,
        *,
        object_table=None,
        include_userdata=False,
        own_all_userdata=False,
        valid_instance_ids=None,
        reference_type_isolation=False,
        include_positional=False,
    ):
        """Collect instances owned by an RSZ reference graph.

        Direct object fields are structural ownership edges. Object references in
        arrays, and all userdata references, are treated as ownership edges only
        when no other instance references the target. ``own_all_userdata`` keeps
        the embedded-RSZ rule where userdata belongs to its containing graph.
        ``reference_type_isolation`` is for embedded object namespaces whose
        object and userdata edges must be considered independently.
        ``include_positional`` preserves legacy ownership of instances located
        between adjacent object-table entries.

        The root is intentionally excluded from the returned set.
        """
        excluded_ids = set(object_table or ())
        excluded_ids.add(0)
        valid_ids = (
            None if valid_instance_ids is None else set(valid_instance_ids)
        )
        owned = set()
        visited = set()

        def explore(source_id):
            if source_id in visited:
                return
            visited.add(source_id)

            fields = parsed_elements.get(source_id)
            if not isinstance(fields, dict):
                return

            for _, ref_obj, array_index in iter_field_reference_entries(fields):
                target_id = ref_obj.value
                if (
                    target_id <= 0
                    or target_id == source_id
                    or target_id in visited
                    or target_id in excluded_ids
                    or (valid_ids is not None and target_id not in valid_ids)
                ):
                    continue

                if isinstance(ref_obj, ObjectData):
                    is_owned = array_index is None or RszInstanceOperations.is_exclusively_referenced_from(
                        parsed_elements,
                        target_id,
                        source_id,
                        object_table,
                        ObjectData if reference_type_isolation else None,
                    )
                elif include_userdata and isinstance(ref_obj, UserDataData):
                    is_owned = own_all_userdata or RszInstanceOperations.is_exclusively_referenced_from(
                        parsed_elements,
                        target_id,
                        source_id,
                        object_table,
                        UserDataData if reference_type_isolation else None,
                    )
                else:
                    is_owned = False

                if is_owned:
                    owned.add(target_id)
                    explore(target_id)

            if include_positional:
                for target_id in RszInstanceOperations.find_nested_objects(
                    parsed_elements, source_id, object_table
                ):
                    if target_id not in visited and target_id not in excluded_ids:
                        owned.add(target_id)
                        explore(target_id)

        explore(root_instance_id)
        owned.discard(root_instance_id)
        return owned
        
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
            for field_name, ref_obj, array_index in iter_field_reference_entries(fields):
                if ref_obj.value == instance_id:
                    if ref_id not in references:
                        references[ref_id] = []
                    if array_index is None:
                        references[ref_id].append((field_name, "direct"))
                    else:
                        references[ref_id].append((f"{field_name}[{array_index}]", "array_object"))
    
        return references

    @staticmethod
    def build_reference_hierarchy(parsed_elements, consolidate_roots=True):
        """Build parent/children metadata from object-reference edges."""
        hierarchy = {
            instance_id: {"children": [], "parent": None}
            for instance_id, fields in parsed_elements.items()
            if isinstance(fields, dict)
        }

        for source_id, _, ref_obj, _ in RszInstanceOperations.iter_instance_references(
            parsed_elements
        ):
            child_id = ref_obj.value
            if (
                not isinstance(ref_obj, ObjectData)
                or child_id == source_id
                or child_id not in hierarchy
                or source_id not in hierarchy
            ):
                continue
            children = hierarchy[source_id]["children"]
            children.append(child_id)
            hierarchy[child_id]["parent"] = source_id

        if consolidate_roots:
            RszInstanceOperations.consolidate_reference_roots(hierarchy)
        return hierarchy

    @staticmethod
    def consolidate_reference_roots(hierarchy):
        """Attach secondary structural roots to the root with the largest graph."""
        roots = [
            instance_id
            for instance_id, data in hierarchy.items()
            if data.get("parent") is None and data.get("children")
        ]
        if len(roots) <= 1:
            return hierarchy

        main_root = max(
            roots,
            key=lambda instance_id: RszInstanceOperations.count_hierarchy_descendants(
                instance_id, hierarchy
            ),
        )
        for root_id in roots:
            if root_id == main_root or hierarchy[root_id].get("parent") is not None:
                continue
            if root_id not in hierarchy[main_root]["children"]:
                hierarchy[main_root]["children"].append(root_id)
            hierarchy[root_id]["parent"] = main_root
        return hierarchy

    @staticmethod
    def count_hierarchy_descendants(instance_id, hierarchy, visited=None):
        """Count unique descendants without recursing forever on corrupt cycles."""
        visited = set() if visited is None else visited
        if instance_id in visited or instance_id not in hierarchy:
            return 0
        visited.add(instance_id)
        children = hierarchy[instance_id].get("children", [])
        return len(children) + sum(
            RszInstanceOperations.count_hierarchy_descendants(
                child_id, hierarchy, visited
            )
            for child_id in children
        )
        
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
        return RszInstanceOperations.collect_owned_instances(
            parsed_elements,
            root_instance_id,
            object_table=object_table,
            include_positional=True,
        )
        
    @staticmethod
    def find_object_references(fields):
        """
        Find all object references in a set of fields
        
        Args:
            fields: Dictionary of fields to search
            
        Returns:
            set: Set of referenced object IDs
        """
        return collect_object_reference_values(fields)
    
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
