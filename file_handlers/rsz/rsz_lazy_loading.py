"""
RSZ Lazy Loading Support
"""

from ..pyside.tree_core import DeferredChildBuilder
from ..pyside.tree_model import DataTreeBuilder
from .rsz_data_types import (
    StructData,
    ArrayData,
    ObjectData,
    UserDataData,
    is_reference_type,
    is_array_type,
)
CHUNK_SIZE = 100


class RszLazyNodeBuilder:
    
    def __init__(self, viewer):
        self.viewer = viewer
        self.scn = viewer.scn
        self.name_helper = viewer.name_helper
        self.type_registry = viewer.type_registry
    
    def create_lazy_struct_node(self, field_name: str, data_obj: StructData, embedded_context=None) -> dict:
        original_type = f"{data_obj.orig_type}" if hasattr(data_obj, 'orig_type') and data_obj.orig_type else ""
        
        struct_node = DataTreeBuilder.create_data_node(
            f"{field_name}: {original_type}", "", "struct", data_obj
        )
        
        if hasattr(data_obj, 'values') and data_obj.values:
            def build_struct_children():
                children = []
                
                struct_type_info = None
                field_definitions = {}
                if self.type_registry and original_type:
                    struct_type_info, _ = self.type_registry.find_type_by_name(original_type)
                    if struct_type_info and "fields" in struct_type_info:
                        field_definitions = {
                            field_def["name"]: field_def 
                            for field_def in struct_type_info["fields"] 
                            if "name" in field_def
                        }
                
                for i, struct_value in enumerate(data_obj.values):
                    if not isinstance(struct_value, dict):
                        continue
                        
                    instance_label = f"{i}: {original_type}"
                    
                    if "name" in struct_value and hasattr(struct_value["name"], 'value') and struct_value["name"].value:
                        instance_label = f"{i}: {struct_value['name'].value}"
                    
                    struct_instance_node = DataTreeBuilder.create_data_node(
                        instance_label, "", "struct_instance", None
                    )
                    
                    instance_fields_builder = self._create_struct_instance_fields_builder(
                        struct_value, field_definitions, embedded_context
                    )
                    struct_instance_node["deferred_builder"] = instance_fields_builder
                    struct_instance_node["expandable"] = len(struct_value) > 0
                    
                    children.append(struct_instance_node)
                
                return children
            
            struct_node["deferred_builder"] = DeferredChildBuilder(build_struct_children)
            struct_node["expandable"] = len(data_obj.values) > 0
        
        return struct_node
    
    def _create_struct_instance_fields_builder(self, struct_value: dict, field_definitions: dict, embedded_context) -> DeferredChildBuilder:
        def build_fields():
            children = []
            for field_key, field_value in struct_value.items():
                if field_key in field_definitions:
                    field_def = field_definitions[field_key]
                    display_name = field_def["name"]
                    display_type = field_def["type"]
                    
                    field_node = self.viewer._create_field_dict(display_name, field_value, embedded_context, use_lazy=True)
                    field_node["data"][0] = f"{display_name} ({display_type})"
                else:
                    field_node = self.viewer._create_field_dict(field_key, field_value, embedded_context, use_lazy=True)
                    
                children.append(field_node)
            return children
        
        return DeferredChildBuilder(build_fields)
    
    def create_lazy_array_node(self, field_name: str, data_obj, embedded_context=None) -> dict:
        if embedded_context == "userdata_array_needs_embedded":
            embedded_context = None
        original_type = f"{data_obj.orig_type}" if data_obj.orig_type else ""
        
        array_node = DataTreeBuilder.create_data_node(
            f"{field_name}: {original_type}", "", "array", data_obj
        )
        
        if embedded_context:
            array_node["embedded_context"] = embedded_context
            if hasattr(data_obj, '_owning_context') and data_obj._owning_context is None:
                data_obj._owning_context = embedded_context
            if hasattr(data_obj, '_owning_instance_id') and data_obj._owning_instance_id is None:
                if hasattr(embedded_context, 'embedded_object_table') and embedded_context.embedded_object_table:
                    data_obj._owning_instance_id = embedded_context.embedded_object_table[0]
        
        if hasattr(data_obj, 'values') and data_obj.values is not None:
            def build_array_children():
                total = len(data_obj.values)
                # For very large arrays, build child nodes in manageable chunks.
                if total > CHUNK_SIZE:
                    return self._build_array_chunks(data_obj, embedded_context)
                return self._build_array_elements_range(
                    data_obj, 0, total, embedded_context
                )

            array_node["deferred_builder"] = DeferredChildBuilder(build_array_children)
            array_node["expandable"] = len(data_obj.values) > 0
        
        return array_node

    def _build_array_chunks(self, data_obj, embedded_context):
        """Create group nodes for large arrays to defer element creation."""
        children = []
        total = len(data_obj.values)
        for start in range(0, total, CHUNK_SIZE):
            end = min(start + CHUNK_SIZE, total)
            group_label = f"{start}-{end - 1}"
            group_node = DataTreeBuilder.create_data_node(group_label, "", "array_group", None)

            # Capture start/end for the builder using default arguments
            group_node["deferred_builder"] = DeferredChildBuilder(
                lambda s=start, e=end: self._build_array_elements_range(
                    data_obj, s, e, embedded_context
                )
            )
            group_node["expandable"] = True
            children.append(group_node)

        return children

    def _build_array_elements_range(self, data_obj, start, end, embedded_context):
        """Build actual element nodes for a slice of an array."""
        children = []
        for i in range(start, end):
            element = data_obj.values[i]
            if isinstance(element, (ArrayData, ObjectData, UserDataData)):
                if not hasattr(element, '_container_array') or element._container_array is None:
                    element._container_array = data_obj
                # Always refresh the container index so it stays in sync after edits
                element._container_index = i
                if embedded_context:
                    if not hasattr(element, '_container_context') or element._container_context is None:
                        element._container_context = embedded_context

            if is_reference_type(element):
                child_node = self.viewer._handle_reference_in_array(
                    i, element, embedded_context, None
                )
                if child_node:
                    if (
                        isinstance(child_node, dict)
                        and "children" in child_node
                        and child_node["children"]
                    ):
                        child_node = self._make_node_lazy(child_node)
                    # Ensure the element reference carries its source object and index
                    if isinstance(child_node, dict):
                        child_node.setdefault("obj", element)
                        child_node["element_index"] = i
                    children.append(child_node)
            else:
                element_type = element.__class__.__name__
                elem_node = DataTreeBuilder.create_data_node(
                    str(i) + ": ", "", element_type, element
                )

                if isinstance(element, (ArrayData, StructData)):
                    elem_node = self._make_element_lazy(
                        str(i), element, embedded_context
                    )

                # Store the element's index for primitives that can't hold attributes
                if isinstance(elem_node, dict):
                    elem_node["element_index"] = i

                children.append(elem_node)

        return children
    
    def create_lazy_reference_node(self, field_name: str, ref_id: int, ref_type: str, embedded_context=None) -> dict:
        if ref_type == "UserData":
            display_value = self.name_helper.get_userdata_display_value(ref_id) if self.name_helper else f"UserData (ID: {ref_id})"
        else:
            type_name = self.name_helper.get_type_name_for_instance(ref_id) if self.name_helper else f"Object (ID: {ref_id})"
            display_value = f"({type_name})"
        
        ref_node = DataTreeBuilder.create_data_node(
            f"{field_name}: ({display_value})", "", None, None
        )
        
        has_content = False
        if embedded_context and hasattr(embedded_context, 'embedded_instances'):
            has_content = ref_id in embedded_context.embedded_instances
        elif self.scn and ref_id in self.scn.parsed_elements:
            has_content = True
        elif self.scn and hasattr(self.scn, '_rsz_userdata_dict'):
            rui = self.scn._rsz_userdata_dict.get(ref_id)
            has_content = rui and hasattr(rui, 'embedded_instances') and rui.embedded_instances
        
        if has_content:
            def build_reference_children():
                children = []
                if embedded_context and hasattr(embedded_context, 'embedded_instances'):
                    if ref_id in embedded_context.embedded_instances:
                        instance_data = embedded_context.embedded_instances[ref_id]
                        if isinstance(instance_data, dict) and "embedded_rsz" not in instance_data:
                            for sub_field_name, sub_field_data in instance_data.items():
                                field_node = self.viewer._create_field_dict(sub_field_name, sub_field_data, embedded_context, use_lazy=True)
                                children.append(field_node)
                elif self.scn and ref_id in self.scn.parsed_elements:
                    for fn, fd in self.scn.parsed_elements[ref_id].items():
                        children.append(self.viewer._create_field_dict(fn, fd, None, use_lazy=True))
                return children
            
            ref_node["deferred_builder"] = DeferredChildBuilder(build_reference_children)
            ref_node["expandable"] = True
        
        return ref_node
    
    def _make_node_lazy(self, node: dict) -> dict:
        if "children" in node and node["children"]:
            children = node["children"]
            
            def build_children():
                return [self._make_node_lazy(child) if isinstance(child, dict) else child 
                        for child in children]
            
            node["deferred_builder"] = DeferredChildBuilder(build_children)
            node["expandable"] = True
            node["children"] = []
        
        return node
    
    def _make_element_lazy(self, field_name: str, element, embedded_context) -> dict:
        if isinstance(element, StructData):
            return self.create_lazy_struct_node(field_name, element, embedded_context)
        elif isinstance(element, ArrayData):
            return self.create_lazy_array_node(field_name, element, embedded_context)
        else:
            element_type = element.__class__.__name__
            return DataTreeBuilder.create_data_node(field_name + ": ", "", element_type, element)
    
    def should_use_lazy_loading(self, data_obj) -> bool:
        if isinstance(data_obj, StructData):
            return hasattr(data_obj, 'values') and len(data_obj.values) > 0
        
        if is_array_type(data_obj):
            return hasattr(data_obj, 'values') and len(data_obj.values) > 3
        
        if is_reference_type(data_obj):
            return True
        return False