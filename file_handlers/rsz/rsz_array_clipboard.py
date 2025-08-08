import os
import json
from typing import Callable, Optional
from file_handlers.rsz.rsz_data_types import (
    ObjectData, UserDataData, F32Data, U16Data, S16Data, S32Data, U32Data, U64Data, S64Data, S8Data, U8Data, BoolData,
    StringData, ResourceData, RuntimeTypeData, Vec2Data, Vec3Data, Vec3ColorData, Vec4Data, Float4Data, QuaternionData,
    ColorData, RangeData, RangeIData, GuidData, GameObjectRefData, ArrayData, CapsuleData, OBBData, Mat4Data, Int2Data,
    Int3Data, Int4Data, Float2Data, Float3Data, AABBData, SphereData, CylinderData, AreaData, RectData, LineSegmentData,
    PointData, StructData, RawBytesData, PositionData, is_reference_type
)
from file_handlers.rsz.rsz_clipboard_utils import RszClipboardUtils
from file_handlers.rsz.rsz_embedded_utils import (
    update_rsz_header_counts,
    create_embedded_instance_info
)


class RszArrayClipboard:
    
    on_resource_data_deserialized: Optional[Callable[[str], None]] = None
    @staticmethod
    def get_clipboard_directory():
        return RszClipboardUtils.get_type_clipboard_directory("arrayelement")
        
    @staticmethod
    def get_json_name(widget):
        return RszClipboardUtils.get_json_name(widget)
        
    @staticmethod
    def get_clipboard_file(widget):
        json_name = RszArrayClipboard.get_json_name(widget)
        base_name = os.path.splitext(json_name)[0]
        clipboard_file = f"{base_name}-clipboard.json"
        return os.path.join(RszArrayClipboard.get_clipboard_directory(), clipboard_file)
        
    @staticmethod
    def copy_to_clipboard(widget, element, array_type, embedded_context=None):
        parent_viewer = widget.parent()
        
        if not embedded_context and hasattr(widget, 'embedded_context'):
            embedded_context = widget.embedded_context
        
        if isinstance(element, ObjectData) and element.value > 0:
            serialised = RszArrayClipboard._serialize_object_with_graph(element, parent_viewer)
        elif isinstance(element, UserDataData):
            serialised = RszArrayClipboard._serialize_userdata_with_graph(element, parent_viewer, embedded_context)
        else:
            serialised = RszArrayClipboard._serialize_element(element)
        
        if hasattr(element, '_container_context') and element._container_context:
            serialised["_embedded_context_info"] = {
                "domain_id": getattr(element._container_context, 'instance_id', 0),
                "type": "embedded_rsz"
            }

        RszArrayClipboard._write_clipboard(
            array_type,
            [serialised],
            RszArrayClipboard.get_clipboard_file(widget)
        )
        return True


    
    @staticmethod
    def _serialize_userdata_with_graph(element, viewer, embedded_context=None):
        """Serialize UserDataData element from array - includes full content for embedded contexts"""
        result = {
            "type": "UserDataData",
            "value": element.value,
            "string": element.string,
            "orig_type": getattr(element, "orig_type", "")
        }
        
        if element.value > 0:
            userdata_info = None
            
            if embedded_context:
                if hasattr(embedded_context, 'embedded_userdata_infos'):
                    for rui in embedded_context.embedded_userdata_infos:
                        if getattr(rui, 'instance_id', -1) == element.value:
                            userdata_info = rui
                            break
            
            if not userdata_info:
                userdata_info = RszArrayClipboard._find_userdata_info_by_instance_id(viewer, element.value)
            
            if userdata_info and hasattr(userdata_info, 'embedded_instances') and userdata_info.embedded_instances:        
                object_graph = RszArrayClipboard._create_embedded_rsz_object_graph(userdata_info, viewer, embedded_context)
                
                if object_graph:
                    result["object_graph"] = object_graph
                    result["embedded_data"] = RszArrayClipboard._convert_object_graph_to_embedded_data(userdata_info, object_graph)
                    result["has_full_content"] = True
                else:
                    print(f"Failed to create object graph for UserData instance {element.value}")
        
        return result
    
    @staticmethod
    def _find_userdata_info_by_instance_id(viewer, instance_id, embedded_context=None):
        """Find userdata info by instance_id, searching comprehensively"""
        if hasattr(viewer.scn, '_rsz_userdata_dict') and instance_id in viewer.scn._rsz_userdata_dict:
            return viewer.scn._rsz_userdata_dict[instance_id]
        
        if hasattr(viewer.scn, 'rsz_userdata_infos'):
            for rui in viewer.scn.rsz_userdata_infos:
                if getattr(rui, 'instance_id', -1) == instance_id:
                    return rui
        
        if embedded_context:
            result = RszArrayClipboard._find_userdata_in_embedded_context(embedded_context, instance_id)
            if result:
                return result
        
        if hasattr(viewer.scn, 'rsz_userdata_infos'):
            for rui in viewer.scn.rsz_userdata_infos:
                if hasattr(rui, 'embedded_userdata_infos'):
                    nested_result = RszArrayClipboard._find_userdata_in_embedded_context(rui, instance_id)
                    if nested_result:
                        return nested_result
        
        return None
    
    @staticmethod
    def _find_userdata_in_embedded_context(context, instance_id):
        """Recursively search for userdata in embedded context"""
        if hasattr(context, 'embedded_userdata_infos'):
            for rui in context.embedded_userdata_infos:
                rui_id = getattr(rui, 'instance_id', -1)
                if rui_id == instance_id:
                    return rui
                if hasattr(rui, 'embedded_userdata_infos'):
                    nested_result = RszArrayClipboard._find_userdata_in_embedded_context(rui, instance_id)
                    if nested_result:
                        return nested_result
        
        if hasattr(context, '_rsz_userdata_dict') and instance_id in context._rsz_userdata_dict:
            return context._rsz_userdata_dict[instance_id]
            
        return None
    
    @staticmethod
    def _create_embedded_rsz_object_graph(rui, viewer, parent_embedded_context=None):
        """Create object graph for embedded RSZ structure with relative IDs"""

        all_instance_ids = set()
        
        if hasattr(rui, 'embedded_instances') and rui.embedded_instances:
            for inst_id in rui.embedded_instances.keys():
                if inst_id > 0:
                    all_instance_ids.add(inst_id)
        
        if hasattr(rui, 'embedded_userdata_infos'):
            for nested_ui in rui.embedded_userdata_infos:
                nested_instance_id = getattr(nested_ui, 'instance_id', 0)
                if nested_instance_id > 0:
                    all_instance_ids.add(nested_instance_id)
                
                if hasattr(nested_ui, 'embedded_instances'):
                    for nested_inst_id in nested_ui.embedded_instances.keys():
                        if nested_inst_id > 0:
                            all_instance_ids.add(nested_inst_id)
        
        if not all_instance_ids:
            return None
        
        sorted_ids = sorted(all_instance_ids)
        relative_id_mapping = {orig_id: idx for idx, orig_id in enumerate(sorted_ids)}
        
        object_graph = {
            "instances": [],
            "userdata_infos": [],
            "embedded_object_table": getattr(rui, 'embedded_object_table', []),
            "userdata_relative_id": 0,  # Will be set to the main userdata's relative ID
            "context_type": "embedded_rsz"
        }
        
        for orig_inst_id in sorted_ids:
            if orig_inst_id in rui.embedded_instances:
                inst_data = rui.embedded_instances[orig_inst_id]
                
                inst_info = None
                if (hasattr(rui, 'embedded_instance_infos') and 
                    orig_inst_id < len(rui.embedded_instance_infos)):
                    inst_info = rui.embedded_instance_infos[orig_inst_id]
                
                instance_entry = {
                    "id": relative_id_mapping[orig_inst_id],
                    "original_id": orig_inst_id,
                    "type_id": getattr(inst_info, 'type_id', 0) if inst_info else 0,
                    "crc": getattr(inst_info, 'crc', 0) if inst_info else 0,
                    "fields": {}
                }
                
                if isinstance(inst_data, dict):
                    for field_name, field_data in inst_data.items():
                        if (field_name == "embedded_rsz" and 
                            hasattr(field_data, 'instance_id') and hasattr(field_data, 'type_id')):
                            continue
                        
                        if isinstance(field_data, UserDataData) and field_data.value > 0:
                            userdata_rui = RszArrayClipboard._find_userdata_info_by_instance_id(viewer, field_data.value, rui)
                            
                            if userdata_rui:
                                if (hasattr(userdata_rui, 'embedded_instances') and 
                                    userdata_rui.embedded_instances and hasattr(viewer.scn, 'has_embedded_rsz') and 
                                    viewer.scn.has_embedded_rsz):
                                    instance_entry["fields"][field_name] = RszArrayClipboard._serialize_userdata_with_graph(
                                        field_data, viewer, rui
                                    )
                                    continue
                            else:
                                print(f"    No userdata_rui found for instance {field_data.value}")
                        
                        instance_entry["fields"][field_name] = RszArrayClipboard._serialize_field_with_mapping(
                            field_data, relative_id_mapping, all_instance_ids, set(), rui
                        )
                
                object_graph["instances"].append(instance_entry)
        
        if hasattr(rui, 'embedded_userdata_infos'):
            for nested_ui in rui.embedded_userdata_infos:
                nested_instance_id = getattr(nested_ui, 'instance_id', 0)
                ui_entry = {
                    "instance_id": relative_id_mapping.get(nested_instance_id, nested_instance_id),
                    "original_instance_id": nested_instance_id,
                    "type_id": getattr(nested_ui, 'type_id', 0),
                    "hash": getattr(nested_ui, 'hash', 0),
                    "json_path_hash": getattr(nested_ui, 'json_path_hash', 0),
                    "is_userdata": True
                }
                
                str_map = getattr(rui, '_rsz_userdata_str_map', {})
                if nested_ui in str_map:
                    ui_entry["userdata_string"] = str_map[nested_ui]
                
                if hasattr(nested_ui, 'embedded_instances') and nested_ui.embedded_instances:
                    nested_graph = RszArrayClipboard._create_embedded_rsz_object_graph(nested_ui, viewer, rui)
                    if nested_graph:
                        ui_entry["nested_object_graph"] = nested_graph
                
                object_graph["userdata_infos"].append(ui_entry)
        
        print(f"Successfully created embedded RSZ object graph with {len(object_graph.get('instances', []))} instances")
        return object_graph
            
    @staticmethod
    def _serialize_object_with_graph(element, viewer):
        result = {
            "type": "ObjectData",
            "value": element.value,
            "orig_type": element.orig_type
        }
        
        if element.value <= 0 or not viewer or not hasattr(viewer, "array_operations"):
            return result
            
        root_object_id = element.value
        
        if root_object_id in viewer.scn.object_table:
            print(f"Root object {root_object_id} is in object table - serializing as external reference only")
            result["is_external_ref"] = True
            return result
            
        if hasattr(viewer.array_operations, "_collect_all_nested_objects"):
            component_min_id, component_max_id = 0, float('inf')
            
            object_table_ids = set(viewer.scn.object_table)
            
            object_table_sorted = sorted(viewer.scn.object_table)
            prev_id = 0
            next_id = float('inf')
            
            for id in object_table_sorted:
                if id < root_object_id and id > prev_id:
                    prev_id = id
                elif id > root_object_id:
                    next_id = id
                    break
            
            component_min_id = prev_id
            component_max_id = next_id
            
            print(f"Strict component range: {component_min_id}-{component_max_id}")
            
            all_ids = viewer.array_operations._collect_all_nested_objects(root_object_id)
            all_ids.add(root_object_id)
            
            from file_handlers.rsz.rsz_instance_operations import RszInstanceOperations
            for instance_id in list(all_ids):
                if instance_id in viewer.scn.parsed_elements:
                    instance_fields = viewer.scn.parsed_elements[instance_id]
                    userdata_refs = set()
                    RszInstanceOperations.find_userdata_references(instance_fields, userdata_refs)
                    all_ids.update(userdata_refs)
            
            filtered_ids = set()
            for id in all_ids:
                if id == root_object_id:
                    filtered_ids.add(id)
                    continue
                    
                if component_min_id < id < component_max_id:
                    if id not in object_table_ids:
                        filtered_ids.add(id)
                        print(f"Including ID {id} (within component range)")
                    else:
                        print(f"Excluding ID {id} (in object table)")
                else:
                    print(f"Excluding ID {id} (outside component range {component_min_id}-{component_max_id})")
            
            all_ids = filtered_ids
            
            external_refs = set()
            for instance_id in all_ids.copy():
                if instance_id == root_object_id:
                    continue
                    
                if instance_id in object_table_ids:
                    external_refs.add(instance_id)
                    print(f"Instance {instance_id} is in the object table - treating as external reference")
                    continue
                    
                if instance_id <= component_min_id or instance_id >= component_max_id:
                    external_refs.add(instance_id)
                    print(f"Instance {instance_id} is outside component range - treating as external reference")
            
            print(f"Copying object graph with {len(all_ids)} objects (including {len(external_refs)} external references)")
            
            filtered_graph_ids = all_ids - external_refs
            
            id_list = sorted(filtered_graph_ids)
            
            id_mapping = {orig_id: idx for idx, orig_id in enumerate(id_list)}
            
            result["object_graph"] = {
                "root_id": id_mapping.get(root_object_id, -1),
                "instances": [],
                "external_refs": sorted(external_refs)
            }
            
            for orig_id in id_list:
                if orig_id <= 0 or orig_id >= len(viewer.scn.instance_infos):
                    continue
                    
                instance_info = viewer.scn.instance_infos[orig_id]
                
                instance_data = {
                    "id": id_mapping[orig_id],
                    "type_id": instance_info.type_id,
                    "crc": instance_info.crc,
                    "fields": {}
                }
                
                if hasattr(viewer, "type_registry") and viewer.type_registry:
                    type_info = viewer.type_registry.get_type_info(instance_info.type_id)
                    if type_info and "name" in type_info:
                        instance_data["type_name"] = type_info["name"]
                
                if hasattr(viewer.scn, '_rsz_userdata_set') and orig_id in viewer.scn._rsz_userdata_set:
                    instance_data["is_userdata"] = True
                    
                    for rui in viewer.scn.rsz_userdata_infos:
                        if rui.instance_id == orig_id:
                            if hasattr(rui, 'hash'):
                                instance_data["userdata_hash"] = rui.hash
                            elif hasattr(rui, 'json_path_hash'):
                                instance_data["userdata_hash"] = rui.json_path_hash
                            else:
                                instance_data["userdata_hash"] = 0
                            
                            if hasattr(viewer.scn, '_rsz_userdata_str_map') and rui in viewer.scn._rsz_userdata_str_map:
                                instance_data["userdata_string"] = viewer.scn._rsz_userdata_str_map[rui]
                            break
                
                if orig_id in viewer.scn.parsed_elements:
                    fields = viewer.scn.parsed_elements[orig_id]
                    for field_name, field_data in fields.items():
                        embedded_context = None
                        if isinstance(field_data, UserDataData) and field_data.value > 0:
                            userdata_rui = RszArrayClipboard._find_userdata_info_by_instance_id(viewer, field_data.value)
                            if (userdata_rui and hasattr(userdata_rui, 'embedded_instances') and 
                                userdata_rui.embedded_instances and hasattr(viewer.scn, 'has_embedded_rsz') and 
                                viewer.scn.has_embedded_rsz):
                                instance_data["fields"][field_name] = RszArrayClipboard._serialize_userdata_with_graph(
                                    field_data, viewer, None
                                )
                                continue
                        
                        instance_data["fields"][field_name] = RszArrayClipboard._serialize_field_with_mapping(
                            field_data, id_mapping, filtered_graph_ids, external_refs, embedded_context
                        )
                        
                result["object_graph"]["instances"].append(instance_data)
        
        return result
    
    @staticmethod
    def _serialize_instance_info(viewer, instance_id, relative_id_mapping):
        """Serialize instance info with relative ID mapping - delegates to base class unified method"""
        from file_handlers.rsz.rsz_clipboard_base import RszClipboardBase
        
        temp_instance = type('TempClipboard', (RszClipboardBase,), {
            'get_clipboard_type': lambda self: 'temp'
        })()
        
        return temp_instance.serialize_instance_data(viewer, instance_id, relative_id_mapping)
    
    @staticmethod
    def _paste_single_element(viewer, elem_data, array_data, array_item, embedded_context=None):
        """
        Paste a single element with proper object graph handling.
        Returns the created element or None if failed.
        """
        element_type = elem_data.get("type")
        has_graph = "object_graph" in elem_data
        element = None
        
        source_embedded_info = elem_data.get("_embedded_context_info", {})
        is_from_embedded = source_embedded_info.get("type") == "embedded_rsz"
        
        if element_type == "ObjectData":
            if embedded_context:
                from file_handlers.rsz.rsz_embedded_array_operations import RszEmbeddedArrayOperations
                embedded_ops = RszEmbeddedArrayOperations(viewer)
                element = embedded_ops.paste_array_element(elem_data, array_data, embedded_context)
                if element:
                    return element
            elif has_graph:
                ins_idx = RszArrayClipboard._calculate_insertion_index(
                    array_data, array_item, viewer
                )
                element = RszArrayClipboard._paste_object_graph(
                    viewer, elem_data, array_data, ins_idx
                )
            else:
                element = RszArrayClipboard._deserialize_element(elem_data, None, {}, randomize_guids=False)
                if element:
                    array_data.values.append(element)
        elif element_type == "UserDataData":
            if embedded_context:
                from file_handlers.rsz.rsz_embedded_array_operations import RszEmbeddedArrayOperations
                embedded_ops = RszEmbeddedArrayOperations(viewer)
                element = embedded_ops.paste_array_element(elem_data, array_data, embedded_context)
                if element:
                    return element
            elif has_graph and elem_data.get("object_graph", {}).get("context_type") == "embedded_rsz":
                element = RszArrayClipboard._deserialize_complete_embedded_rsz_userdata(
                    viewer, elem_data, embedded_context
                )
                if element:
                    array_data.values.append(element)
            elif has_graph:
                ins_idx = RszArrayClipboard._calculate_insertion_index(
                    array_data, array_item, viewer
                )
                element = RszArrayClipboard._paste_userdata_graph(
                    viewer, elem_data, array_data, ins_idx
                )
            elif "embedded_rsz" in elem_data:
                element = RszArrayClipboard._deserialize_complete_embedded_rsz_userdata(
                    viewer, elem_data, embedded_context
                )
                if element:
                    array_data.values.append(element)
            elif is_from_embedded and embedded_context:
                element = RszArrayClipboard._paste_embedded_userdata(
                    viewer, elem_data, array_data, embedded_context
                )
            else:
                element = RszArrayClipboard._deserialize_element(elem_data, None, {}, randomize_guids=False)
                if element:
                    array_data.values.append(element)
        else:
            element = RszArrayClipboard._deserialize_field_with_relative_mapping(
                elem_data, {}, {}, randomize_guids=False
            )
            if element:
                array_data.values.append(element)
        
        return element
    
    @staticmethod
    def _deserialize_complete_embedded_rsz_userdata(viewer, elem_data, target_embedded_context=None):
        """Deserialize embedded RSZ UserData by adding to existing RUI structure (following embedded operations)"""
        object_graph = elem_data.get("object_graph", {})
        if not object_graph or object_graph.get("context_type") != "embedded_rsz":
            return RszArrayClipboard._deserialize_field_with_relative_mapping(
                elem_data, {}, {}, randomize_guids=False
            )
        
        target_rui = target_embedded_context
        if not target_rui:
            pasted_instance_id = RszArrayClipboard._create_self_contained_embedded_rsz(
                object_graph, viewer
            )
            
            if pasted_instance_id > 0:
                userdata_element = UserDataData(pasted_instance_id, elem_data.get("string", ""), elem_data.get("orig_type", ""))
                userdata_element._container_context = None
                userdata_element._owning_context = None  
                userdata_element._owning_userdata = None
                return userdata_element
            else:
                print("Failed to create standalone embedded RSZ")
                return None
        
        pasted_instance_id = RszArrayClipboard._add_instances_to_existing_rui(
            object_graph, viewer, target_rui
        )
        
        if pasted_instance_id is None:
            return None
        
        userdata_element = UserDataData(
            pasted_instance_id,
            elem_data.get("string", f"Embedded Binary Data (ID: {pasted_instance_id})"),
            elem_data.get("orig_type", "")
        )
        
        userdata_element._container_context = target_rui
        userdata_element._owning_context = target_rui
        
        for rui in (viewer.scn.rsz_userdata_infos if target_rui is None or target_rui == viewer.scn else target_rui.embedded_userdata_infos):
            if getattr(rui, 'instance_id', -1) == pasted_instance_id:
                userdata_element._owning_userdata = rui
                break
        
        return userdata_element

    @staticmethod
    def _create_rsz_userdata_info_for_existing_instance(viewer, elem_data, existing_instance_id):
        """Create RszUserDataInfo for an existing instance (used when instance was pre-allocated)"""
        object_graph = elem_data.get("object_graph", {})
        if not object_graph or object_graph.get("context_type") != "embedded_rsz":
            return None
        
        instances = object_graph.get("instances", [])
        if not instances:
            return None
        
        instance_type_id = 0
        if existing_instance_id < len(viewer.scn.instance_infos):
            instance_info = viewer.scn.instance_infos[existing_instance_id]
            instance_type_id = instance_info.type_id
        else:
            print(f"Warning: existing_instance_id {existing_instance_id} not found in instance_infos, using type_id 0")
        
        from file_handlers.rsz.scn_19.scn_19_structure import Scn19RSZUserDataInfo, EmbeddedRSZHeader
        
        userdata_info = Scn19RSZUserDataInfo()
        userdata_info.instance_id = existing_instance_id
        userdata_info.type_id = instance_type_id 
        userdata_info.json_path_hash = 0
        userdata_info.data_size = 0
        userdata_info.rsz_offset = 0
        userdata_info.data = b""
        userdata_info.original_data = None
        userdata_info.modified = True
        
        root_instance = instances[0] if instances else {}
        type_name = root_instance.get("type_name", "")
        userdata_info.value = type_name
        userdata_info.parent_userdata_rui = None 
        
        if not hasattr(userdata_info, 'name'):
            userdata_info.name = type_name
        
        userdata_info.embedded_rsz_header = EmbeddedRSZHeader()
        userdata_info.embedded_rsz_header.magic = viewer.scn.rsz_header.magic
        userdata_info.embedded_rsz_header.version = viewer.scn.rsz_header.version 
        userdata_info.embedded_object_table = []
        userdata_info.embedded_instance_infos = []
        userdata_info.embedded_userdata_infos = []
        userdata_info.embedded_instances = {}
        
        if not hasattr(userdata_info, 'embedded_instance_hierarchy'):
            userdata_info.embedded_instance_hierarchy = {}
        
        from utils.id_manager import EmbeddedIdManager
        userdata_info.id_manager = EmbeddedIdManager(existing_instance_id)

        id_mapping = {}
        next_relative_id = 1
        for instance in instances:
            clipboard_id = instance.get("id", -1)
            id_mapping[clipboard_id] = next_relative_id
            next_relative_id += 1
        
        null_instance_info = create_embedded_instance_info(0, viewer.type_registry)
        userdata_info.embedded_instance_infos = [null_instance_info]
        
        root_instance = instances[0] if instances else {}
        root_type_id = root_instance.get("type_id", 0)
        
        root_instance_info = create_embedded_instance_info(root_type_id, viewer.type_registry)
        userdata_info.embedded_instance_infos.append(root_instance_info)
        
        userdata_info.embedded_instance_hierarchy = {1: {"children": [], "parent": None}}
        
        for instance in instances:
            clipboard_id = instance.get("id", -1)
            new_relative_id = id_mapping.get(clipboard_id, -1)
            
            if new_relative_id <= 0:
                continue
            
            while len(userdata_info.embedded_instance_infos) <= new_relative_id:
                dummy_info = create_embedded_instance_info(0, viewer.type_registry)
                userdata_info.embedded_instance_infos.append(dummy_info)
            
            if new_relative_id != 1:
                instance_info = create_embedded_instance_info(instance.get("type_id", 0), viewer.type_registry)
                userdata_info.embedded_instance_infos[new_relative_id] = instance_info
            
            fields_data = instance.get("fields", {})
            embedded_fields = {}
            
            from file_handlers.rsz.rsz_array_clipboard import RszArrayClipboard
            from file_handlers.rsz.rsz_data_types import UserDataData, ArrayData
            
            for field_name, field_data in fields_data.items():
                deserialized_field = RszArrayClipboard._deserialize_field_with_relative_mapping(
                    field_data, id_mapping
                )
                
                if isinstance(deserialized_field, ArrayData) and hasattr(deserialized_field, 'values'):
                    deserialized_field._container_path = [existing_instance_id]
                    deserialized_field._owning_instance_id = new_relative_id
                    deserialized_field._owning_field = field_name
                    deserialized_field._owning_context = userdata_info
                    
                    for idx, element in enumerate(deserialized_field.values):
                        if is_reference_type(element):
                            element._container_array = deserialized_field
                            element._container_index = idx
                            element._container_field = field_name
                            element._container_instance = new_relative_id
                elif is_reference_type(deserialized_field):
                    deserialized_field._container_field = field_name
                    deserialized_field._container_instance = new_relative_id
                    deserialized_field._container_context = userdata_info
                
                embedded_fields[field_name] = deserialized_field
            
            userdata_info.embedded_instances[new_relative_id] = embedded_fields
            
            if new_relative_id != 1:
                userdata_info.embedded_instance_hierarchy[new_relative_id] = {
                    "children": [], 
                    "parent": 0  # All instances are children of the root (0)
                }
            
            if hasattr(userdata_info, 'id_manager'):
                userdata_info.id_manager.register_instance(new_relative_id)
        
        userdata_info.parsed_elements = {}
        userdata_info._rsz_userdata_dict = {}
        userdata_info._rsz_userdata_set = set()
        userdata_info._rsz_userdata_str_map = {}
        userdata_info.modified = False
        
        def mark_modified_func():
            userdata_info.modified = True
            if hasattr(userdata_info, 'parent_userdata_rui') and userdata_info.parent_userdata_rui:
                parent = userdata_info.parent_userdata_rui
                if hasattr(parent, 'modified'):
                    parent.modified = True
                if hasattr(parent, 'parent_userdata_rui') and parent.parent_userdata_rui:
                    top_parent = parent.parent_userdata_rui
                    if hasattr(top_parent, 'modified'):
                        top_parent.modified = True
            viewer.mark_modified()
        
        userdata_info.mark_modified = mark_modified_func
        
        target_instance_relative_id = len(instances)  # Last instance position
        userdata_info.embedded_object_table = [target_instance_relative_id]
        
        userdata_infos = object_graph.get("userdata_infos", [])
        if userdata_infos:
            for ui_data in userdata_infos:
                nested_instance_id = ui_data.get("instance_id", 0)
                nested_type_id = ui_data.get("type_id", 0)
                nested_hash = ui_data.get("hash", 0)
                nested_json_path_hash = ui_data.get("json_path_hash", 0)
                
                from file_handlers.rsz.scn_19.scn_19_structure import Scn19RSZUserDataInfo
                nested_userdata_info = Scn19RSZUserDataInfo()
                mapped_instance_id = id_mapping.get(nested_instance_id, nested_instance_id)
                nested_userdata_info.instance_id = mapped_instance_id
                nested_userdata_info.type_id = nested_type_id
                nested_userdata_info.hash = nested_hash
                nested_userdata_info.json_path_hash = nested_json_path_hash
                nested_userdata_info.data_size = 0
                nested_userdata_info.rsz_offset = 0
                nested_userdata_info.data = b""
                nested_userdata_info.modified = True
                
                nested_graph = ui_data.get("nested_object_graph")
                if nested_graph:
                    nested_userdata_info = RszArrayClipboard._populate_userdata_from_object_graph(
                        nested_userdata_info, nested_graph, viewer
                    )
                
                userdata_info.embedded_userdata_infos.append(nested_userdata_info)
                
                nested_string = ui_data.get("userdata_string")
                if nested_string:
                    if not hasattr(userdata_info, '_rsz_userdata_str_map'):
                        userdata_info._rsz_userdata_str_map = {}
                    userdata_info._rsz_userdata_str_map[nested_userdata_info] = nested_string
        
        update_rsz_header_counts(userdata_info)
        
        if not hasattr(viewer.scn, '_rsz_userdata_set'):
            viewer.scn._rsz_userdata_set = set()
        if not hasattr(viewer.scn, '_rsz_userdata_dict'):
            viewer.scn._rsz_userdata_dict = {}
        if not hasattr(viewer.scn, '_rsz_userdata_str_map'):
            viewer.scn._rsz_userdata_str_map = {}
        
        if existing_instance_id in viewer.scn._rsz_userdata_set:
            from file_handlers.rsz.rsz_data_types import UserDataData
            userdata_element = UserDataData(existing_instance_id, elem_data.get("string", ""), elem_data.get("orig_type", ""))
            userdata_element._container_context = None
            userdata_element._owning_context = None  
            userdata_element._owning_userdata = None
            return userdata_element
        
        viewer.scn._rsz_userdata_set.add(existing_instance_id)
        viewer.scn._rsz_userdata_dict[existing_instance_id] = userdata_info
        viewer.scn.rsz_userdata_infos.append(userdata_info)
        
        userdata_desc = f"Embedded Binary Data (ID: {existing_instance_id})"
        viewer.scn._rsz_userdata_str_map[userdata_info] = userdata_desc
        
        from file_handlers.rsz.rsz_data_types import UserDataData
        userdata_element = UserDataData(existing_instance_id, elem_data.get("string", ""), elem_data.get("orig_type", ""))
        userdata_element._container_context = None
        userdata_element._owning_context = None  
        userdata_element._owning_userdata = None
        
        return userdata_element
    
    @staticmethod  
    def _create_self_contained_embedded_rsz(object_graph, viewer):
        """Create a completely self-contained embedded RSZ userdata without any main file instance leakage"""

        instances = object_graph.get("instances", [])
        if not instances:
            return None
        
        used_ids = {getattr(rui, 'instance_id', -1) for rui in viewer.scn.rsz_userdata_infos}
        next_userdata_id = len(viewer.scn.instance_infos)
        while next_userdata_id in used_ids:
            next_userdata_id += 1
        
        root_instance = instances[0] if instances else {}
        root_type_id = root_instance.get("type_id", 0)
        
        from file_handlers.rsz.scn_19.scn_19_structure import Scn19RSZUserDataInfo, EmbeddedRSZHeader
        
        userdata_info = Scn19RSZUserDataInfo()
        userdata_info.instance_id = next_userdata_id
        userdata_info.type_id = root_type_id
        userdata_info.json_path_hash = 0
        userdata_info.data_size = 0
        userdata_info.rsz_offset = 0
        userdata_info.data = b""
        userdata_info.original_data = None
        userdata_info.modified = True
        userdata_info.value = root_instance.get("type_name", "")
        userdata_info.parent_userdata_rui = None
        
        if not hasattr(userdata_info, 'name'):
            userdata_info.name = root_instance.get("type_name", "")
        
        userdata_info.embedded_rsz_header = EmbeddedRSZHeader()
        userdata_info.embedded_rsz_header.magic = viewer.scn.rsz_header.magic
        userdata_info.embedded_rsz_header.version = viewer.scn.rsz_header.version 
        userdata_info.embedded_object_table = []
        userdata_info.embedded_instance_infos = []
        userdata_info.embedded_userdata_infos = []
        userdata_info.embedded_instances = {}
        
        if not hasattr(userdata_info, 'embedded_instance_hierarchy'):
            userdata_info.embedded_instance_hierarchy = {}
        
        from utils.id_manager import EmbeddedIdManager
        userdata_info.id_manager = EmbeddedIdManager(next_userdata_id)
        
        instance_by_relative_id = {inst.get("id", -1): inst for inst in instances}
        
        parent_child_map = {}  # Maps parent relative ID to list of (field_name, child_relative_id)
        for instance_data in instances:
            relative_id = instance_data.get("id", -1)
            fields = instance_data.get("fields", {})
            
            for field_name, field_data in fields.items():
                if isinstance(field_data, dict):
                    field_type = field_data.get("type")
                    if field_type == "ObjectData":
                        child_relative_id = field_data.get("value", 0)
                        if child_relative_id > 0 and child_relative_id in instance_by_relative_id:
                            if relative_id not in parent_child_map:
                                parent_child_map[relative_id] = []
                            parent_child_map[relative_id].append((field_name, child_relative_id))
                    elif field_type == "UserDataData":
                        child_relative_id = field_data.get("value", 0)
                        if child_relative_id > 0 and child_relative_id in instance_by_relative_id:
                            if relative_id not in parent_child_map:
                                parent_child_map[relative_id] = []
                            parent_child_map[relative_id].append((field_name, child_relative_id))
        
        id_mapping = {}
        next_embedded_id = 1
        processed_ids = set()
        
        def assign_ids_respecting_field_order(relative_id):
            """Recursively assign IDs respecting field declaration order"""
            nonlocal next_embedded_id
            
            if relative_id in processed_ids:
                return
                
            processed_ids.add(relative_id)
            instance_data = instance_by_relative_id.get(relative_id)
            if not instance_data:
                return
            
            type_id = instance_data.get("type_id", 0)
            type_info = viewer.type_registry.get_type_info(type_id) if hasattr(viewer, 'type_registry') else None
            
            if type_info and relative_id in parent_child_map:
                field_order = {field["name"]: idx for idx, field in enumerate(type_info.get("fields", []))}
                
                children = parent_child_map[relative_id]
                sorted_children = sorted(children, key=lambda x: field_order.get(x[0], 999))
                
                for field_name, child_id in sorted_children:
                    assign_ids_respecting_field_order(child_id)
            
            id_mapping[relative_id] = next_embedded_id
            next_embedded_id += 1
        
        root_relative_id = object_graph.get("root_id", 1)
        assign_ids_respecting_field_order(root_relative_id)
        
        for instance_data in instances:
            relative_id = instance_data.get("id", -1)
            if relative_id not in id_mapping:
                id_mapping[relative_id] = next_embedded_id
                next_embedded_id += 1
        
        for instance_data in instances:
            relative_id = instance_data.get("id", 0)
            new_absolute_id = id_mapping[relative_id]
            
            inst_info = create_embedded_instance_info(instance_data.get("type_id", 0), viewer.type_registry)
            if "crc" in instance_data:
                inst_info.crc = instance_data.get("crc", 0)
            
            while len(userdata_info.embedded_instance_infos) <= new_absolute_id:
                dummy_info = create_embedded_instance_info(0, viewer.type_registry)
                userdata_info.embedded_instance_infos.append(dummy_info)
            
            userdata_info.embedded_instance_infos[new_absolute_id] = inst_info
            
            fields_data = instance_data.get("fields", {})
            reconstructed_fields = {}
            
            for field_name, field_data in fields_data.items():
                reconstructed_fields[field_name] = RszArrayClipboard._reconstruct_field_with_new_ids(
                    field_data, id_mapping
                )
            
            userdata_info.embedded_instances[new_absolute_id] = reconstructed_fields
            
            userdata_info.embedded_instance_hierarchy[new_absolute_id] = {"children": [], "parent": None}
            
            if hasattr(userdata_info, 'id_manager') and userdata_info.id_manager:
                userdata_info.id_manager.register_instance(new_absolute_id)
        
        target_instance_relative_id = len(instances)
        userdata_info.embedded_object_table = [target_instance_relative_id]
        
        if not hasattr(viewer.scn, 'rsz_userdata_infos'):
            viewer.scn.rsz_userdata_infos = []
        
        if next_userdata_id in viewer.scn._rsz_userdata_set:
            print(f"Warning: RSZUserDataInfo for instance {next_userdata_id} already exists - this should not happen")
            return None
        
        viewer.scn.rsz_userdata_infos.append(userdata_info)
        
        if not hasattr(viewer.scn, '_rsz_userdata_dict'):
            viewer.scn._rsz_userdata_dict = {}
        if not hasattr(viewer.scn, '_rsz_userdata_set'):
            viewer.scn._rsz_userdata_set = set()
        if not hasattr(viewer.scn, '_rsz_userdata_str_map'):
            viewer.scn._rsz_userdata_str_map = {}
        
        viewer.scn._rsz_userdata_dict[next_userdata_id] = userdata_info
        viewer.scn._rsz_userdata_set.add(next_userdata_id)
        
        type_name = root_instance.get("type_name") or userdata_info.value or f"UserData_{next_userdata_id}"
        viewer.scn._rsz_userdata_str_map[userdata_info] = type_name
        
        userdata_infos = object_graph.get("userdata_infos", [])
        if userdata_infos:
            for ui_data in userdata_infos:
                nested_instance_id = ui_data.get("instance_id", 0)
                nested_type_id = ui_data.get("type_id", 0)
                nested_hash = ui_data.get("hash", 0)
                nested_json_path_hash = ui_data.get("json_path_hash", 0)
                
                from file_handlers.rsz.scn_19.scn_19_structure import Scn19RSZUserDataInfo
                nested_userdata_info = Scn19RSZUserDataInfo()
                mapped_instance_id = id_mapping.get(nested_instance_id, nested_instance_id)
                nested_userdata_info.instance_id = mapped_instance_id
                nested_userdata_info.type_id = nested_type_id
                nested_userdata_info.hash = nested_hash
                nested_userdata_info.json_path_hash = nested_json_path_hash
                nested_userdata_info.data_size = 0
                nested_userdata_info.rsz_offset = 0
                nested_userdata_info.data = b""
                nested_userdata_info.modified = True
                
                nested_graph = ui_data.get("nested_object_graph")
                if nested_graph:
                    nested_userdata_info = RszArrayClipboard._populate_userdata_from_object_graph(
                        nested_userdata_info, nested_graph, viewer
                    )
                
                userdata_info.embedded_userdata_infos.append(nested_userdata_info)
                
                nested_string = ui_data.get("userdata_string")
                if nested_string:
                    if not hasattr(userdata_info, '_rsz_userdata_str_map'):
                        userdata_info._rsz_userdata_str_map = {}
                    userdata_info._rsz_userdata_str_map[nested_userdata_info] = nested_string
        
        update_rsz_header_counts(userdata_info)
    
        from file_handlers.rsz.scn_19.scn_19_structure import build_embedded_rsz
        userdata_info.data = build_embedded_rsz(userdata_info, viewer.type_registry)
        userdata_info.data_size = len(userdata_info.data)
        
        if hasattr(viewer, 'mark_modified'):
            viewer.mark_modified()
        
        return next_userdata_id
    
    @staticmethod
    def _populate_userdata_from_object_graph(userdata_info, object_graph, viewer):
        """Populate userdata info structure from object graph (for nested embedded RSZ)"""
        instances = object_graph.get("instances", [])
        if not instances:
            return userdata_info
        
        from file_handlers.rsz.scn_19.scn_19_structure import EmbeddedRSZHeader
        userdata_info.embedded_rsz_header = EmbeddedRSZHeader()
        userdata_info.embedded_rsz_header.magic = viewer.scn.rsz_header.magic
        userdata_info.embedded_rsz_header.version = viewer.scn.rsz_header.version 
        userdata_info.embedded_object_table = []
        userdata_info.embedded_instance_infos = []
        userdata_info.embedded_userdata_infos = []
        userdata_info.embedded_instances = {}
        
        id_mapping = {}
        next_relative_id = 1
        for instance in instances:
            clipboard_id = instance.get("id", -1)
            id_mapping[clipboard_id] = next_relative_id
            next_relative_id += 1
        
        null_instance_info = create_embedded_instance_info(0, viewer.type_registry)
        userdata_info.embedded_instance_infos = [null_instance_info]
        
        for instance in instances:
            clipboard_id = instance.get("id", -1)
            new_relative_id = id_mapping.get(clipboard_id, -1)
            
            if new_relative_id <= 0:
                continue
            
            while len(userdata_info.embedded_instance_infos) <= new_relative_id:
                dummy_info = create_embedded_instance_info(0, viewer.type_registry)
                userdata_info.embedded_instance_infos.append(dummy_info)
            
            instance_info = create_embedded_instance_info(instance.get("type_id", 0), viewer.type_registry)
            userdata_info.embedded_instance_infos[new_relative_id] = instance_info
            
            fields_data = instance.get("fields", {})
            embedded_fields = {}
            
            for field_name, field_data in fields_data.items():
                deserialized_field = RszArrayClipboard._deserialize_field_with_relative_mapping(
                    field_data, id_mapping
                )
                embedded_fields[field_name] = deserialized_field
            
            userdata_info.embedded_instances[new_relative_id] = embedded_fields
        
        target_instance_relative_id = len(instances)  # Last instance position
        userdata_info.embedded_object_table = [target_instance_relative_id]
        
        userdata_infos = object_graph.get("userdata_infos", [])
        for ui_data in userdata_infos:
            nested_instance_id = ui_data.get("instance_id", 0)
            nested_type_id = ui_data.get("type_id", 0)
            nested_hash = ui_data.get("hash", 0)
            nested_json_path_hash = ui_data.get("json_path_hash", 0)
            
            from file_handlers.rsz.scn_19.scn_19_structure import Scn19RSZUserDataInfo
            nested_userdata_info = Scn19RSZUserDataInfo()
            mapped_instance_id = id_mapping.get(nested_instance_id, nested_instance_id)
            nested_userdata_info.instance_id = mapped_instance_id
            nested_userdata_info.type_id = nested_type_id
            nested_userdata_info.hash = nested_hash
            nested_userdata_info.json_path_hash = nested_json_path_hash
            nested_userdata_info.data_size = 0
            nested_userdata_info.rsz_offset = 0
            nested_userdata_info.data = b""
            nested_userdata_info.modified = True
            
            nested_graph = ui_data.get("nested_object_graph")
            if nested_graph:
                nested_userdata_info = RszArrayClipboard._populate_userdata_from_object_graph(
                    nested_userdata_info, nested_graph, viewer
                )
            
            userdata_info.embedded_userdata_infos.append(nested_userdata_info)
            
            nested_string = ui_data.get("userdata_string")
            if nested_string:
                if not hasattr(userdata_info, '_rsz_userdata_str_map'):
                    userdata_info._rsz_userdata_str_map = {}
                userdata_info._rsz_userdata_str_map[nested_userdata_info] = nested_string
        
        return userdata_info
        
    @staticmethod  
    def _add_instances_to_existing_rui(object_graph, viewer, target_rui):
        """Add clipboard instances directly to existing RUI structure by creating new userdata info (following embedded operations pattern)""" 
        instances = object_graph.get("instances", [])
        if not instances:
            return None

        if target_rui is None:
            used_ids = {getattr(rui, 'instance_id', -1) for rui in viewer.scn.rsz_userdata_infos}
            next_userdata_id = len(viewer.scn.instance_infos)
            while next_userdata_id in used_ids:
                next_userdata_id += 1
        elif hasattr(target_rui, 'embedded_instances') and target_rui.embedded_instances:
            next_userdata_id = max(target_rui.embedded_instances.keys()) + 1
        else:
            next_userdata_id = 0
            if not hasattr(target_rui, 'embedded_instances'):
                target_rui.embedded_instances = {}
        
        root_instance = instances[0] if instances else {}
        root_type_id = root_instance.get("type_id", 0)
        
        from file_handlers.rsz.scn_19.scn_19_structure import Scn19RSZUserDataInfo, EmbeddedRSZHeader
        
        userdata_info = Scn19RSZUserDataInfo()
        userdata_info.instance_id = next_userdata_id
        userdata_info.type_id = root_type_id
        userdata_info.json_path_hash = 0  # Will be calculated later
        userdata_info.data_size = 0  # Will be set after building RSZ data
        userdata_info.rsz_offset = 0  # Will be set during main file build
        userdata_info.data = b""  # Will be set after building RSZ data
        userdata_info.original_data = None
        userdata_info.modified = True 
        userdata_info.value = root_instance.get("type_name", "")
        userdata_info.parent_userdata_rui = target_rui
        
        if not hasattr(userdata_info, 'name'):
            userdata_info.name = root_instance.get("type_name", "")
        
        userdata_info.embedded_rsz_header = EmbeddedRSZHeader()
        userdata_info.embedded_rsz_header.magic = viewer.scn.rsz_header.magic
        userdata_info.embedded_rsz_header.version = viewer.scn.rsz_header.version 
        userdata_info.embedded_object_table = []  
        userdata_info.embedded_instance_infos = []
        userdata_info.embedded_userdata_infos = []
        userdata_info.embedded_instances = {}
        
        if not hasattr(userdata_info, 'embedded_instance_hierarchy'):
            userdata_info.embedded_instance_hierarchy = {}

        from utils.id_manager import EmbeddedIdManager
        userdata_info.id_manager = EmbeddedIdManager(next_userdata_id)

        instance_by_relative_id = {inst.get("id", -1): inst for inst in instances}
        
        parent_child_map = {}
        for instance_data in instances:
            relative_id = instance_data.get("id", -1)
            fields = instance_data.get("fields", {})
            
            for field_name, field_data in fields.items():
                if isinstance(field_data, dict):
                    field_type = field_data.get("type")
                    if field_type == "ObjectData":
                        child_relative_id = field_data.get("value", 0)
                        if child_relative_id > 0 and child_relative_id in instance_by_relative_id:
                            if relative_id not in parent_child_map:
                                parent_child_map[relative_id] = []
                            parent_child_map[relative_id].append((field_name, child_relative_id))
                    elif field_type == "UserDataData":
                        child_relative_id = field_data.get("value", 0)
                        if child_relative_id > 0 and child_relative_id in instance_by_relative_id:
                            if relative_id not in parent_child_map:
                                parent_child_map[relative_id] = []
                            parent_child_map[relative_id].append((field_name, child_relative_id))
        
        id_mapping = {}
        next_embedded_id = 1
        processed_ids = set()
        
        def assign_ids_respecting_field_order(relative_id):
            """Recursively assign IDs respecting field declaration order"""
            nonlocal next_embedded_id
            
            if relative_id in processed_ids:
                return
                
            processed_ids.add(relative_id)
            instance_data = instance_by_relative_id.get(relative_id)
            if not instance_data:
                return
            
            type_id = instance_data.get("type_id", 0)
            type_info = viewer.type_registry.get_type_info(type_id) if hasattr(viewer, 'type_registry') else None
            
            if type_info and relative_id in parent_child_map:
                field_order = {field["name"]: idx for idx, field in enumerate(type_info.get("fields", []))}
                
                children = parent_child_map[relative_id]
                sorted_children = sorted(children, key=lambda x: field_order.get(x[0], 999))
                
                for field_name, child_id in sorted_children:
                    assign_ids_respecting_field_order(child_id)
            
            id_mapping[relative_id] = next_embedded_id
            next_embedded_id += 1
        
        root_relative_id = object_graph.get("root_id", 1)
        assign_ids_respecting_field_order(root_relative_id)
        
        for instance_data in instances:
            relative_id = instance_data.get("id", -1)
            if relative_id not in id_mapping:
                id_mapping[relative_id] = next_embedded_id
                next_embedded_id += 1
                        
        for instance_data in instances:
            relative_id = instance_data.get("id", 0)
            new_absolute_id = id_mapping[relative_id]
            
            inst_info = create_embedded_instance_info(instance_data.get("type_id", 0), viewer.type_registry)
            if "crc" in instance_data:
                inst_info.crc = instance_data.get("crc", 0)
            
            while len(userdata_info.embedded_instance_infos) <= new_absolute_id:
                dummy_info = create_embedded_instance_info(0, viewer.type_registry)
                userdata_info.embedded_instance_infos.append(dummy_info)
            
            userdata_info.embedded_instance_infos[new_absolute_id] = inst_info
            
            fields_data = instance_data.get("fields", {})
            reconstructed_fields = {}
            
            for field_name, field_data in fields_data.items():
                reconstructed_fields[field_name] = RszArrayClipboard._reconstruct_field_with_new_ids(
                    field_data, id_mapping
                )
            
            userdata_info.embedded_instances[new_absolute_id] = reconstructed_fields
            
            userdata_info.embedded_instance_hierarchy[new_absolute_id] = {"children": [], "parent": None}
            
            if hasattr(userdata_info, 'id_manager') and userdata_info.id_manager:
                userdata_info.id_manager.register_instance(new_absolute_id)
        
        target_instance_relative_id = len(instances) 
        userdata_info.embedded_object_table = [target_instance_relative_id]

        if target_rui is None or target_rui == viewer.scn:
            if not hasattr(viewer.scn, 'rsz_userdata_infos'):
                viewer.scn.rsz_userdata_infos = []
            
            if hasattr(viewer.scn, '_rsz_userdata_set') and next_userdata_id in viewer.scn._rsz_userdata_set:
                print(f"Warning: RSZUserDataInfo for instance {next_userdata_id} already exists - this should not happen")
                return None
        
            viewer.scn.rsz_userdata_infos.append(userdata_info)

            if not hasattr(viewer.scn, '_rsz_userdata_dict'):
                viewer.scn._rsz_userdata_dict = {}
            if not hasattr(viewer.scn, '_rsz_userdata_set'):
                viewer.scn._rsz_userdata_set = set()
            if not hasattr(viewer.scn, '_rsz_userdata_str_map'):
                viewer.scn._rsz_userdata_str_map = {}
            
            viewer.scn._rsz_userdata_dict[next_userdata_id] = userdata_info
            viewer.scn._rsz_userdata_set.add(next_userdata_id)
            
            type_name = root_instance.get("type_name") or userdata_info.value or f"UserData_{next_userdata_id}"
            viewer.scn._rsz_userdata_str_map[userdata_info] = type_name
        else:
            if not hasattr(target_rui, 'embedded_userdata_infos'):
                target_rui.embedded_userdata_infos = []
            
            if hasattr(target_rui, '_rsz_userdata_set') and next_userdata_id in target_rui._rsz_userdata_set:
                print(f"Warning: RSZUserDataInfo for instance {next_userdata_id} already exists in embedded context - this should not happen")
                return None
            
            target_rui.embedded_userdata_infos.append(userdata_info)

            if not hasattr(target_rui, '_rsz_userdata_dict'):
                target_rui._rsz_userdata_dict = {}
            if not hasattr(target_rui, '_rsz_userdata_set'):
                target_rui._rsz_userdata_set = set()
            if not hasattr(target_rui, '_rsz_userdata_str_map'):
                target_rui._rsz_userdata_str_map = {}
            
            target_rui._rsz_userdata_dict[next_userdata_id] = userdata_info
            target_rui._rsz_userdata_set.add(next_userdata_id)
            
            type_name = root_instance.get("type_name") or userdata_info.value or f"UserData_{next_userdata_id}"
            if target_rui is not None:
                target_rui._rsz_userdata_str_map[userdata_info] = type_name
        
        if target_rui is not None:
            target_rui.embedded_instances[next_userdata_id] = {}
            
            if not hasattr(target_rui, 'embedded_instance_hierarchy'):
                target_rui.embedded_instance_hierarchy = {}
            target_rui.embedded_instance_hierarchy[next_userdata_id] = {"children": [], "parent": None}
            
            if not hasattr(target_rui, 'embedded_instance_infos'):
                target_rui.embedded_instance_infos = []
            
            parent_inst_info = create_embedded_instance_info(root_type_id, viewer.type_registry)
            while len(target_rui.embedded_instance_infos) <= next_userdata_id:
                dummy_info = create_embedded_instance_info(0, viewer.type_registry)
                target_rui.embedded_instance_infos.append(dummy_info)
            target_rui.embedded_instance_infos[next_userdata_id] = parent_inst_info
            
            if hasattr(target_rui, 'id_manager') and target_rui.id_manager:
                target_rui.id_manager.register_instance(next_userdata_id)
        
        if target_rui is None or target_rui == viewer.scn:
            if next_userdata_id < len(viewer.scn.instance_infos):
                main_inst_info = viewer.scn.instance_infos[next_userdata_id]
                main_inst_info.type_id = root_type_id
                if "crc" in root_instance:
                    main_inst_info.crc = root_instance.get("crc", 0)

            else:
                while len(viewer.scn.instance_infos) <= next_userdata_id:
                    dummy_info = create_embedded_instance_info(0, viewer.type_registry)
                    viewer.scn.instance_infos.append(dummy_info)
                
                main_inst_info = create_embedded_instance_info(root_type_id, viewer.type_registry)
                if "crc" in root_instance:
                    main_inst_info.crc = root_instance.get("crc", 0)
                viewer.scn.instance_infos[next_userdata_id] = main_inst_info

            if not hasattr(viewer.scn, 'parsed_elements'):
                viewer.scn.parsed_elements = {}
            viewer.scn.parsed_elements[next_userdata_id] = {}

        else:
            # Embedded context - instance_infos are handled by the embedded RUI only
            pass
        
        userdata_infos = object_graph.get("userdata_infos", [])
        if userdata_infos:
            for ui_data in userdata_infos:
                nested_instance_id = ui_data.get("instance_id", 0)
                nested_type_id = ui_data.get("type_id", 0)
                nested_hash = ui_data.get("hash", 0)
                nested_json_path_hash = ui_data.get("json_path_hash", 0)
                
                from file_handlers.rsz.scn_19.scn_19_structure import Scn19RSZUserDataInfo
                nested_userdata_info = Scn19RSZUserDataInfo()
                mapped_instance_id = id_mapping.get(nested_instance_id, nested_instance_id)
                nested_userdata_info.instance_id = mapped_instance_id
                nested_userdata_info.type_id = nested_type_id
                nested_userdata_info.hash = nested_hash
                nested_userdata_info.json_path_hash = nested_json_path_hash
                nested_userdata_info.data_size = 0
                nested_userdata_info.rsz_offset = 0
                nested_userdata_info.data = b""
                nested_userdata_info.modified = True
                
                nested_graph = ui_data.get("nested_object_graph")
                if nested_graph:
                    nested_userdata_info = RszArrayClipboard._populate_userdata_from_object_graph(
                        nested_userdata_info, nested_graph, viewer
                    )
                
                userdata_info.embedded_userdata_infos.append(nested_userdata_info)
                
                nested_string = ui_data.get("userdata_string")
                if nested_string:
                    if not hasattr(userdata_info, '_rsz_userdata_str_map'):
                        userdata_info._rsz_userdata_str_map = {}
                    userdata_info._rsz_userdata_str_map[nested_userdata_info] = nested_string
        
        update_rsz_header_counts(target_rui)
        update_rsz_header_counts(userdata_info)
        
        from file_handlers.rsz.scn_19.scn_19_structure import build_embedded_rsz 
        userdata_info.data = build_embedded_rsz(userdata_info, viewer.type_registry)
        userdata_info.data_size = len(userdata_info.data)

        if hasattr(viewer, 'mark_modified'):
            viewer.mark_modified()

        return next_userdata_id
    
    @staticmethod
    def _reconstruct_field_with_new_ids(field_data, id_mapping):
        """Reconstruct field data with updated IDs (following embedded operations pattern)"""
        field_type = field_data.get("type", "")
        
        if field_type == "ObjectData":
            value = field_data.get("value", 0)
            orig_type = field_data.get("orig_type", "")
            
            if field_data.get("in_graph", False) and value in id_mapping:
                value = id_mapping[value]

            return ObjectData(value, orig_type)
            
        elif field_type == "UserDataData":
            value = field_data.get("value", 0)
            string = field_data.get("string", "")
            orig_type = field_data.get("orig_type", "")
            
            if field_data.get("in_graph", False) and value in id_mapping:
                value = id_mapping[value]

            return UserDataData(value, string, orig_type)
            
        elif field_type == "ArrayData":
            result_values = []
            for elem_data in field_data.get("values", []):
                result_values.append(RszArrayClipboard._reconstruct_field_with_new_ids(elem_data, id_mapping))
            
            array_data = ArrayData()
            array_data.orig_type = field_data.get("orig_type", "")
            array_data.values = result_values
            return array_data
        
        else:
            return RszArrayClipboard._deserialize_element(field_data, None, {}, randomize_guids=False)

    @staticmethod
    def _paste_embedded_userdata(viewer, elem_data, array_data, embedded_context):
        """Handle pasting userdata in embedded context"""
        if not embedded_context:
            return None
        
        from file_handlers.rsz.rsz_data_types import UserDataData
        from file_handlers.rsz.rsz_object_operations import RszObjectOperations
        
        if "object_graph" in elem_data:
            element = RszArrayClipboard._deserialize_complete_embedded_rsz_userdata(
                viewer, elem_data, embedded_context
            )
            if element:
                array_data.values.append(element)
            return element
        
        orig_type = elem_data.get("orig_type", "")
        if orig_type:
            parent_instance_id = getattr(array_data, '_owning_instance_id', None)
            
            # Calculate proper insertion index based on field order
            from file_handlers.rsz.rsz_embedded_array_operations import RszEmbeddedArrayOperations
            embedded_ops = RszEmbeddedArrayOperations(viewer)
            insertion_index = embedded_ops._calculate_field_order_insertion_index(array_data, embedded_context, parent_instance_id)
            
            object_ops = RszObjectOperations(viewer)
            element = object_ops._create_embedded_userdata_in_context(orig_type, embedded_context, insertion_index, array_data)
            
            if element:
                element._container_array = array_data
                element._container_context = embedded_context
                if not hasattr(element, '_owning_context'):
                    element._owning_context = embedded_context
                

                    
                array_data.values.append(element)
                return element
        
        # Fallback: create simple userdata element
        print(f"Fallback: Creating simple UserDataData with value={elem_data.get('value', 0)}")
        element = UserDataData(
            value=elem_data.get("value", 0),
            string=elem_data.get("string", ""),
            orig_type=orig_type
        )
        
        element._container_context = embedded_context
        if element:
            array_data.values.append(element)
        return element

    
    @staticmethod
    def _collect_nested_objects(viewer, root_id):
        from file_handlers.rsz.rsz_instance_operations import RszInstanceOperations
        return RszInstanceOperations.collect_all_nested_objects(
            viewer.scn.parsed_elements, root_id, viewer.scn.object_table
        )

    @staticmethod
    def _serialize_field_with_mapping(field_data, id_mapping, nested_ids, external_refs, embedded_context=None):
        if isinstance(field_data, ObjectData) or isinstance(field_data, UserDataData):
            if isinstance(field_data, UserDataData) and embedded_context:
                result = RszArrayClipboard._serialize_element(field_data)
                nested_ui = None
                if hasattr(embedded_context, 'embedded_userdata_infos'):
                    for ui in embedded_context.embedded_userdata_infos:
                        if getattr(ui, 'instance_id', -1) == field_data.value:
                            nested_ui = ui
                            break
                if nested_ui:
                    if hasattr(nested_ui, 'embedded_instances') and nested_ui.embedded_instances:
                        result["has_nested_embedded_rsz"] = True
                        result["embedded_context_id"] = getattr(embedded_context, 'instance_id', 0)
                        result["nested_userdata_instance_id"] = field_data.value
                
                return result
            else:
                result = RszArrayClipboard._serialize_element(field_data)
            
            if field_data.value > 0 and field_data.value in external_refs:
                result["is_external_ref"] = True
                return result
                
            if field_data.value > 0 and field_data.value in nested_ids:
                if field_data.value in id_mapping:
                    result["value"] = id_mapping[field_data.value]
                    result["in_graph"] = True
                
            return result
        
        elif isinstance(field_data, ArrayData):
            result = {
                "type": "ArrayData",
                "values": [],
                "orig_type": field_data.orig_type,
                "element_type": field_data.element_class.__name__ if field_data.element_class else ""
            }
            
            for element in field_data.values:
                if is_reference_type(element) and element.value > 0:
                    if element.value in external_refs:
                        elem_result = RszArrayClipboard._serialize_element(element)
                        elem_result["is_external_ref"] = True
                        result["values"].append(elem_result)
                    elif element.value in nested_ids and element.value in id_mapping:
                        elem_result = RszArrayClipboard._serialize_element(element)
                        elem_result["value"] = id_mapping[element.value]
                        elem_result["in_graph"] = True
                        result["values"].append(elem_result)
                    else:
                        elem_result = RszArrayClipboard._serialize_field_with_mapping(
                            element, id_mapping, nested_ids, external_refs, embedded_context
                        )
                        result["values"].append(elem_result)
                else:
                    elem_result = RszArrayClipboard._serialize_field_with_mapping(
                        element, id_mapping, nested_ids, external_refs, embedded_context
                    )
                    result["values"].append(elem_result)
                    
            return result
            
        return RszArrayClipboard._serialize_element(field_data)

    @staticmethod
    def _serialize_field(field_data, nested_ids=None):
        if isinstance(field_data, ObjectData):
            result = RszArrayClipboard._serialize_element(field_data)
            if nested_ids and field_data.value in nested_ids:
                result["in_graph"] = True
            return result
        
        elif isinstance(field_data, ArrayData):
            values = []
            for element in field_data.values:
                if isinstance(element, ObjectData) and nested_ids and element.value in nested_ids:
                    elem_result = RszArrayClipboard._serialize_element(element)
                    elem_result["in_graph"] = True
                    values.append(elem_result)
                else:
                    values.append(RszArrayClipboard._serialize_element(element))
                    
            return {
                "type": "ArrayData",
                "values": values,
                "orig_type": field_data.orig_type,
                "element_type": field_data.element_class.__name__ if field_data.element_class else ""
            }
            
        return RszArrayClipboard._serialize_element(field_data)

    @staticmethod
    def _serialize_element(element):
        if isinstance(element, ObjectData):
            return {
                "type": "ObjectData",
                "value": element.value,
                "orig_type": element.orig_type
            }
        elif isinstance(element, UserDataData):
            return {
                "type": "UserDataData",
                "value": element.value,
                "string": element.string,
                "orig_type": getattr(element, "orig_type", "")
            }
        elif isinstance(element, F32Data):
            return {
                "type": "F32Data",
                "value": element.value,
                "orig_type": element.orig_type
            }
        elif isinstance(element, U16Data):
            return {
                "type": "U16Data",
                "value": element.value,
                "orig_type": element.orig_type
            }
        elif isinstance(element, S16Data):
            return {
                "type": "S16Data",
                "value": element.value,
                "orig_type": element.orig_type
            }
        elif isinstance(element, S32Data):
            return {
                "type": "S32Data",
                "value": element.value,
                "orig_type": element.orig_type
            }
        elif isinstance(element, U32Data):
            return {
                "type": "U32Data",
                "value": element.value,
                "orig_type": element.orig_type
            }
        elif isinstance(element, U64Data):
            return {
                "type": "U64Data",
                "value": element.value,
                "orig_type": element.orig_type
            }
        elif isinstance(element, S64Data):
            return {
                "type": "S64Data",
                "value": element.value,
                "orig_type": element.orig_type
            }
        elif isinstance(element, S8Data):
            return {
                "type": "S8Data",
                "value": element.value,
                "orig_type": element.orig_type
            }
        elif isinstance(element, U8Data):
            return {
                "type": "U8Data",
                "value": element.value,
                "orig_type": element.orig_type
            }
        elif isinstance(element, BoolData):
            return {
                "type": "BoolData",
                "value": element.value,
                "orig_type": element.orig_type
            }
        elif isinstance(element, StringData):
            return {
                "type": "StringData",
                "value": element.value,
                "orig_type": element.orig_type
            }
        elif isinstance(element, ResourceData):
            return {
                "type": "ResourceData",
                "value": element.value,
                "orig_type": element.orig_type
            }
        elif isinstance(element, RuntimeTypeData):
            return {
                "type": "RuntimeTypeData",
                "value": element.value,
                "orig_type": element.orig_type
            }
        elif isinstance(element, Vec2Data):
            return {
                "type": "Vec2Data",
                "x": element.x,
                "y": element.y,
                "orig_type": element.orig_type
            }
        elif isinstance(element, Vec3Data):
            return {
                "type": "Vec3Data",
                "x": element.x,
                "y": element.y,
                "z": element.z,
                "orig_type": element.orig_type
            }
        elif isinstance(element, Vec3ColorData):
            return {
                "type": "Vec3ColorData",
                "x": element.x,
                "y": element.y,
                "z": element.z,
                "orig_type": element.orig_type
            }
        elif isinstance(element, Vec4Data):
            return {
                "type": "Vec4Data",
                "x": element.x,
                "y": element.y,
                "z": element.z,
                "w": element.w,
                "orig_type": element.orig_type
            }
        elif isinstance(element, Float4Data):
            return {
                "type": "Float4Data",
                "x": element.x,
                "y": element.y,
                "z": element.z,
                "w": element.w,
                "orig_type": element.orig_type
            }
        elif isinstance(element, QuaternionData):
            return {
                "type": "QuaternionData",
                "x": element.x,
                "y": element.y,
                "z": element.z,
                "w": element.w,
                "orig_type": element.orig_type
            }
        elif isinstance(element, ColorData):
            return {
                "type": "ColorData",
                "r": element.r,
                "g": element.g,
                "b": element.b,
                "a": element.a,
                "orig_type": element.orig_type
            }
        elif isinstance(element, RangeData):
            return {
                "type": "RangeData",
                "min": element.min,
                "max": element.max,
                "orig_type": element.orig_type
            }
        elif isinstance(element, RangeIData):
            return {
                "type": "RangeIData",
                "min": element.min,
                "max": element.max,
                "orig_type": element.orig_type
            }
        elif isinstance(element, GuidData):
            return {
                "type": "GuidData",
                "guid_str": element.guid_str,
                "value": element.raw_bytes.hex() if hasattr(element, 'raw_bytes') and element.raw_bytes else "",
                "orig_type": element.orig_type
            }
        elif isinstance(element, GameObjectRefData):
            hex_string = element.raw_bytes.hex() if hasattr(element, 'raw_bytes') and element.raw_bytes else ""
            return {
                "type": "GameObjectRefData",
                "guid_str": element.guid_str,
                "raw_bytes": hex_string,
                "orig_type": element.orig_type
            }
        elif isinstance(element, ArrayData):
            return {
                "type": "ArrayData",
                "values": [RszArrayClipboard._serialize_element(e) for e in element.values],
                "orig_type": element.orig_type,
                "element_type": element.element_class.__name__ if element.element_class else ""
            }
        elif isinstance(element, CapsuleData):
            return {
                "type": "CapsuleData",
                "radius": element.radius,
                "start": RszArrayClipboard._serialize_element(element.start) if hasattr(element, 'start') else None,
                "end": RszArrayClipboard._serialize_element(element.end) if hasattr(element, 'end') else None,
                "orig_type": element.orig_type
            }
        elif isinstance(element, OBBData):
            if hasattr(element, 'values') and isinstance(element.values, list):
                return {
                    "type": "OBBData",
                    "values": element.values,
                    "orig_type": element.orig_type
                }
            else:
                return {
                    "type": "OBBData",
                    "center": {
                        "x": element.center_x if hasattr(element, 'center_x') else 0.0,
                        "y": element.center_y if hasattr(element, 'center_y') else 0.0,
                        "z": element.center_z if hasattr(element, 'center_z') else 0.0
                    },
                    "size": {
                        "x": element.size_x if hasattr(element, 'size_x') else 0.0,
                        "y": element.size_y if hasattr(element, 'size_y') else 0.0,
                        "z": element.size_z if hasattr(element, 'size_z') else 0.0
                    },
                    "orientation": {
                        "x": element.orient_x if hasattr(element, 'orient_x') else 0.0,
                        "y": element.orient_y if hasattr(element, 'orient_y') else 0.0,
                        "z": element.orient_z if hasattr(element, 'orient_z') else 0.0,
                        "w": element.orient_w if hasattr(element, 'orient_w') else 1.0
                    },
                    "orig_type": element.orig_type
                }
        elif isinstance(element, Mat4Data):
            if hasattr(element, 'values') and isinstance(element.values, list):
                return {
                    "type": "Mat4Data",
                    "values": element.values,
                    "orig_type": element.orig_type
                }
            else:
                return {
                    "type": "Mat4Data",
                    "values": [
                        element.m00 if hasattr(element, 'm00') else 0.0, 
                        element.m01 if hasattr(element, 'm01') else 0.0, 
                        element.m02 if hasattr(element, 'm02') else 0.0,
                        element.m03 if hasattr(element, 'm03') else 0.0,
                        element.m10 if hasattr(element, 'm10') else 0.0, 
                        element.m11 if hasattr(element, 'm11') else 0.0, 
                        element.m12 if hasattr(element, 'm12') else 0.0,
                        element.m13 if hasattr(element, 'm13') else 0.0,
                        element.m20 if hasattr(element, 'm20') else 0.0, 
                        element.m21 if hasattr(element, 'm21') else 0.0, 
                        element.m22 if hasattr(element, 'm22') else 0.0,
                        element.m23 if hasattr(element, 'm23') else 0.0,
                        element.m30 if hasattr(element, 'm30') else 0.0, 
                        element.m31 if hasattr(element, 'm31') else 0.0, 
                        element.m32 if hasattr(element, 'm32') else 0.0,
                        element.m33 if hasattr(element, 'm33') else 0.0
                    ],
                    "orig_type": element.orig_type
                }
        elif isinstance(element, Int2Data):
            return {
                "type": "Int2Data",
                "x": element.x,
                "y": element.y,
                "orig_type": element.orig_type
            }
        elif isinstance(element, Int3Data):
            return {
                "type": "Int3Data",
                "x": element.x,
                "y": element.y,
                "z": element.z,
                "orig_type": element.orig_type
            }
        elif isinstance(element, Int4Data):
            return {
                "type": "Int4Data",
                "x": element.x,
                "y": element.y,
                "z": element.z,
                "w": element.w,
                "orig_type": element.orig_type
            }
        elif isinstance(element, Float2Data):
            return {
                "type": "Float2Data",
                "x": element.x,
                "y": element.y,
                "orig_type": element.orig_type
            }
        elif isinstance(element, Float3Data):
            return {
                "type": "Float3Data",
                "x": element.x,
                "y": element.y,
                "z": element.z,
                "orig_type": element.orig_type
            }
        elif isinstance(element, AABBData):
            return {
                "type": "AABBData",
                "min": RszArrayClipboard._serialize_element(element.min),
                "max": RszArrayClipboard._serialize_element(element.max),
                "orig_type": element.orig_type
            }
        elif isinstance(element, SphereData):
            return {
                "type": "SphereData",
                "center": RszArrayClipboard._serialize_element(element.center),
                "radius": element.radius,
                "orig_type": element.orig_type
            }
        elif isinstance(element, CylinderData):
            return {
                "type": "CylinderData",
                "center": RszArrayClipboard._serialize_element(element.center),
                "radius": element.radius,
                "height": element.height,
                "orig_type": element.orig_type
            }
        elif isinstance(element, AreaData):
            return {
                "type": "AreaData",
                "p0": RszArrayClipboard._serialize_element(element.p0),
                "p1": RszArrayClipboard._serialize_element(element.p1),
                "p2": RszArrayClipboard._serialize_element(element.p2),
                "p3": RszArrayClipboard._serialize_element(element.p3),
                "height": element.height,
                "bottom": element.bottom,
                "orig_type": element.orig_type
            }
        elif isinstance(element, RectData):
            return {
                "type": "RectData",
                "min_x": element.min_x,
                "min_y": element.min_y,
                "max_x": element.max_x,
                "max_y": element.max_y,
                "orig_type": element.orig_type
            }
        elif isinstance(element, LineSegmentData):
            return {
                "type": "LineSegmentData",
                "start": RszArrayClipboard._serialize_element(element.start),
                "end": RszArrayClipboard._serialize_element(element.end),
                "orig_type": element.orig_type
            }
        elif isinstance(element, PointData):
            return {
                "type": "PointData",
                "x": element.x,
                "y": element.y,
                "z": element.z,
                "orig_type": element.orig_type
            }
        elif isinstance(element, PositionData):
            return {
                "type": "PositionData",
                "x": element.x,
                "y": element.y,
                "z": element.z,
                "orig_type": element.orig_type
            }
        elif isinstance(element, StructData):
            struct_result = {
                "type": "StructData",
                "orig_type": element.orig_type,
                "values": []
            }
            for struct_item in element.values:
                if isinstance(struct_item, dict):
                    item_fields = {}
                    for field_key, field_val in struct_item.items():
                        item_fields[field_key] = RszArrayClipboard._serialize_element(field_val)
                    struct_result["values"].append(item_fields)
            return struct_result
        elif isinstance(element, RawBytesData):
            return {
                "type": "RawBytesData",
                "bytes": element.raw_bytes.hex() if element.raw_bytes else "",
                "size": element.field_size,
                "orig_type": element.orig_type
            }
        else:
            raise ValueError(f"Unsupported element type: {element.__class__.__name__}. Please open a github issue to add support for it.")
            
    @staticmethod
    def get_clipboard_data(widget):
        clipboard_file = RszArrayClipboard.get_clipboard_file(widget)
        return RszClipboardUtils.load_clipboard_data(clipboard_file)
            
    @staticmethod
    def is_compatible(target_type, source_type):
        if not target_type or not source_type:
            return False
            
        if target_type == source_type:
            return True
            
        target_base = target_type.split('<')[-1].strip('>')
        source_base = source_type.split('<')[-1].strip('>')
        
        if target_base == source_base:
            return True
            
        if target_type.endswith('[]') and source_type.endswith('[]'):
            target_elem = target_type[:-2]
            source_elem = source_type[:-2]
            return target_elem == source_elem
            
        return False
    @staticmethod
    def _write_clipboard(array_type: str, items: list, file_path: str):
        """
        Dump *items* (already serialised dicts) to *file_path*.
        Creates a multi-element payload automatically when len(items) > 1.
        """
        with open(file_path, "w") as f:
            json.dump(
                {"type": array_type,
                "data": items,
                "is_multi": len(items) > 1},
                f,
                indent=2,
                default=RszClipboardUtils.json_serializer,
            )

    @staticmethod
    def _read_clipboard(file_path: str):
        """
        Return (array_type, list_of_serialised_items).  
        Guarantees the second item is always a *list* – single-element
        clipboards come back as a one-item list for uniform handling.
        """
        payload = RszClipboardUtils.load_clipboard_data(file_path)
        if not payload:
            return None, []
        items = payload.get("data", [])
        if not isinstance(items, list):
            items = [items]
        return payload.get("type", ""), items

    @staticmethod
    def paste_elements_from_clipboard(widget, array_operations,
                                    array_data, array_item,
                                    embedded_context=None):
        """
        Returns the element (single behaviour) or the list (group behaviour).
        UI-update & modification flags are handled internally.
        """
        _, items_data = RszArrayClipboard._read_clipboard(
            RszArrayClipboard.get_clipboard_file(widget)
        )
        if not items_data:
            return None

        viewer = widget.parent()
        added = []
        if not embedded_context and hasattr(widget, 'embedded_context'):
            embedded_context = widget.embedded_context
            
        for elem_data in items_data:
            element = RszArrayClipboard._paste_single_element(
                viewer, elem_data, array_data, array_item, embedded_context
            )
            if element:
                viewer.mark_modified()
                
                if embedded_context and hasattr(array_operations, "_add_element_to_ui_direct"):
                    array_operations._add_element_to_ui_direct(array_item, element, embedded_context)
                else:
                    RszArrayClipboard._add_element_to_ui_direct(widget, array_item, element)
                    
                added.append(element)

        if not added:
            return None
        return added[0] if len(added) == 1 else added
    
    @staticmethod
    def paste_from_clipboard(widget, array_operations, array_data,
                            array_item, embedded_context=None):
        result = RszArrayClipboard.paste_elements_from_clipboard(
            widget, array_operations, array_data, array_item, embedded_context
        )
        if not isinstance(result, list):
            return result
        else:
            return result[0] if result else None
    
    @staticmethod
    def copy_multiple_to_clipboard(widget, elements, array_type, embedded_context=None):
        parent_viewer = widget.parent()
        serialised_items = []
        
        if not embedded_context and hasattr(widget, 'embedded_context'):
            embedded_context = widget.embedded_context
        
        for el in elements:
            if isinstance(el, ObjectData) and el.value > 0:
                serialised = RszArrayClipboard._serialize_object_with_graph(el, parent_viewer)
            elif isinstance(el, UserDataData) and el.value > 0:
                serialised = RszArrayClipboard._serialize_userdata_with_graph(el, parent_viewer, embedded_context)
                
                if hasattr(parent_viewer.scn, 'instance_infos') and 0 < el.value < len(parent_viewer.scn.instance_infos):
                    instance_info = parent_viewer.scn.instance_infos[el.value]
                    serialised["instance_info"] = {
                        "type_id": instance_info.type_id,
                        "crc": instance_info.crc
                    }
                    
                    if hasattr(parent_viewer.scn, '_rsz_userdata_dict') and el.value in parent_viewer.scn._rsz_userdata_dict:
                        rui = parent_viewer.scn._rsz_userdata_dict[el.value]
                        serialised["has_rsz_userdata"] = True
                        serialised["userdata_hash"] = getattr(rui, 'hash', 0)
                    
                    if hasattr(parent_viewer.scn, 'parsed_elements') and el.value in parent_viewer.scn.parsed_elements:
                        fields = parent_viewer.scn.parsed_elements[el.value]
                        serialised["instance_fields"] = RszArrayClipboard._serialize_fields_for_userdata(fields, parent_viewer)
            else:
                serialised = RszArrayClipboard._serialize_element(el)
            
            if embedded_context:
                serialised["_embedded_context_info"] = {
                    "domain_id": getattr(embedded_context, 'instance_id', 0),
                    "type": "embedded_rsz"
                }
            
            serialised_items.append(serialised)

        RszArrayClipboard._write_clipboard(
            array_type,
            serialised_items,
            RszArrayClipboard.get_clipboard_file(widget)
        )
        return True

    @staticmethod
    def paste_multiple_from_clipboard(widget, array_operations, array_data,
                                    array_item, embedded_context=None):
        return RszArrayClipboard.paste_elements_from_clipboard(
            widget, array_operations, array_data, array_item, embedded_context
        )
            
    @staticmethod
    def has_clipboard_data(widget):
        """Check if clipboard data exists"""
        clipboard_file = RszArrayClipboard.get_clipboard_file(widget)
        return os.path.exists(clipboard_file)
            
    @staticmethod
    def get_elements_count_from_clipboard(widget):
        """Get number of elements in clipboard"""
        clipboard_data = RszArrayClipboard.get_clipboard_data(widget)
        if not clipboard_data:
            return 0
            
        if clipboard_data.get("is_multi", False):
            return len(clipboard_data.get("data", []))
        else:
            return 1 if "data" in clipboard_data else 0
            
    @staticmethod
    def is_clipboard_compatible_with_array(widget, array_type):
        """Check if clipboard data (either single or multi-element) is compatible with a target array type"""
        clipboard_data = RszArrayClipboard.get_clipboard_data(widget)
        if not clipboard_data:
            return False
            
        # For single element clipboard
        if not clipboard_data.get("is_multi", False):
            clipboard_type = clipboard_data.get("type", "")
            return RszArrayClipboard.is_compatible(array_type, clipboard_type)
        
        # For for multi-element clipboard
        elements_data = clipboard_data.get("data", [])
        if not elements_data:
            return False
        
        # Check if all elements are compatible with target array
        clipboard_type = clipboard_data.get("type", "")
        return RszArrayClipboard.is_compatible(array_type, clipboard_type)

    @staticmethod
    def _calculate_insertion_index(array_data, array_item, viewer):
        parent_instance_id = None
        parent_field_name = None
        
        for instance_id, fields in viewer.scn.parsed_elements.items():
            for field_name, field_data in fields.items():
                if field_data is array_data:
                    parent_instance_id = instance_id
                    parent_field_name = field_name
                    break
            if parent_instance_id is not None:
                break
        
        if parent_instance_id is not None and parent_field_name is not None and hasattr(viewer, "array_operations"):
            if hasattr(viewer.array_operations, "_calculate_array_element_insertion_index"):
                return viewer.array_operations._calculate_array_element_insertion_index(parent_instance_id, parent_field_name)
            elif hasattr(viewer.array_operations, "_calculate_insertion_index"):
                # Fallback to the basic method
                return viewer.array_operations._calculate_insertion_index(parent_instance_id, parent_field_name)

        raise RuntimeError("Cannot calculate insertion index for object graph paste")

    @staticmethod
    def _paste_object_graph(viewer, element_data, array_data, insertion_index):
        object_graph = element_data.get("object_graph", {})
        instances = object_graph.get("instances", [])
        root_relative_id = object_graph.get("root_id", -1)
        
        if not instances or root_relative_id < 0:
            print("Invalid object graph data")
            return None
            
        print(f"Pasting object graph with {len(instances)} instances")
        
        from file_handlers.rsz.rsz_file import RszInstanceInfo
        
        relative_to_new_id = {}
        guid_mapping = {}
        
        current_index = insertion_index
        for instance_data in instances:
            relative_id = instance_data.get("id", -1)
            type_id = instance_data.get("type_id", 0)
            crc = instance_data.get("crc", 0)
            
            if relative_id < 0 or type_id <= 0 or crc <= 0:
                print(f"Skipping invalid instance: rel_id={relative_id}, type_id={type_id}")
                continue
                
            new_instance = RszInstanceInfo()
            new_instance.type_id = type_id
            new_instance.crc = crc
            
            viewer._insert_instance_and_update_references(current_index, new_instance)
                
            viewer.handler.id_manager.register_instance(current_index)
            
            relative_to_new_id[relative_id] = current_index
            
            if instance_data.get("is_userdata", False):
                from file_handlers.rsz.rsz_clipboard_base import RszClipboardBase
                base_clipboard = type('TempClipboard', (RszClipboardBase,), {
                    'get_clipboard_type': lambda self: 'temp'
                })()
                
                base_clipboard.setup_userdata_for_pasted_instance(
                    viewer,
                    current_index,
                    instance_data.get("userdata_hash", 0),
                    instance_data.get("userdata_string", "")
                )
            
            viewer.scn.parsed_elements[current_index] = {}
            
            current_index += 1
        
        for instance_data in instances:
            relative_id = instance_data.get("id", -1)
            if relative_id < 0 or relative_id not in relative_to_new_id:
                continue
            
            new_id = relative_to_new_id[relative_id]
            fields_data = instance_data.get("fields", {})
            
            for field_name, field_data in fields_data.items():
                field_obj = RszArrayClipboard._deserialize_field_with_relative_mapping(
                    field_data, relative_to_new_id, guid_mapping, randomize_guids=True
                )
                if field_obj:
                    viewer.scn.parsed_elements[new_id][field_name] = field_obj
        
        if root_relative_id in relative_to_new_id:
            new_root_id = relative_to_new_id[root_relative_id]
            
            element = ObjectData(new_root_id, element_data.get("orig_type", ""))
            
            array_data.values.append(element)
            
            viewer.mark_modified()
            
            print(f"Successfully created object graph with root at ID {new_root_id}")
            return element
    
    @staticmethod
    def _paste_userdata_graph(viewer, element_data, array_data, insertion_index):
        """Paste UserDataData with its nested object graph"""
        object_graph = element_data.get("object_graph", {})
        instances = object_graph.get("instances", [])
        root_relative_id = object_graph.get("root_id", -1)
        
        if not instances or root_relative_id < 0:
            print("Invalid UserDataData graph data")
            return None
            
        from file_handlers.rsz.rsz_file import RszInstanceInfo
        
        relative_to_new_id = {}
        guid_mapping = {}
        sorted_instances = sorted(instances, key=lambda x: x.get("id", -1))
        
        current_index = insertion_index
        for instance_data in sorted_instances:
            relative_id = instance_data.get("id", -1)
            type_id = instance_data.get("type_id", 0)
            crc = instance_data.get("crc", 0)
            
            if relative_id < 0 or type_id <= 0 or crc <= 0:
                print(f"Skipping invalid instance: rel_id={relative_id}, type_id={type_id}")
                continue
                
            new_instance = RszInstanceInfo()
            new_instance.type_id = type_id
            new_instance.crc = crc
            
            viewer._insert_instance_and_update_references(current_index, new_instance)
                
            viewer.handler.id_manager.register_instance(current_index)
            
            relative_to_new_id[relative_id] = current_index
            
            if instance_data.get("is_userdata", False):
                from file_handlers.rsz.rsz_clipboard_base import RszClipboardBase
                base_clipboard = type('TempClipboard', (RszClipboardBase,), {
                    'get_clipboard_type': lambda self: 'temp'
                })()
                
                base_clipboard.setup_userdata_for_pasted_instance(
                    viewer,
                    current_index,
                    instance_data.get("userdata_hash", 0),
                    instance_data.get("userdata_string", "")
                )
            
            viewer.scn.parsed_elements[current_index] = {}
            current_index += 1
        
        for instance_data in instances:
            relative_id = instance_data.get("id", -1)
            if relative_id not in relative_to_new_id:
                continue
                
            new_instance_id = relative_to_new_id[relative_id]
            instance_fields = instance_data.get("fields", {})
            
            if instance_fields and new_instance_id in viewer.scn.parsed_elements:
                for field_name, field_data in instance_fields.items():
                    if field_name in viewer.scn.parsed_elements[new_instance_id]:
                        try:
                            from file_handlers.rsz.rsz_clipboard_base import RszClipboardBase
                            base_clipboard = type('TempClipboard', (RszClipboardBase,), {
                                'get_clipboard_type': lambda self: 'temp'
                            })()
                            
                            deserialized = base_clipboard.deserialize_fields_with_remapping(
                                field_data, relative_to_new_id, guid_mapping, randomize_guids=False
                            )
                            viewer.scn.parsed_elements[new_instance_id][field_name] = deserialized
                            
                        except Exception as e:
                            print(f"Warning: Failed to deserialize field {field_name}: {e}")
        
        root_new_id = relative_to_new_id.get(root_relative_id)
        if root_new_id is not None:
            element = UserDataData(
                root_new_id,
                element_data.get("string", ""),
                element_data.get("orig_type", "")
            )
            array_data.values.append(element)
            return element
        
        return None
            
    
    @staticmethod
    def _deserialize_field_with_relative_mapping(field_data, id_mapping, guid_mapping=None, randomize_guids=True):
        if guid_mapping is None:
            guid_mapping = {}
            
        field_type = field_data.get("type", "")
        
        if field_type == "ObjectData":
            value = field_data.get("value", 0)
            orig_type = field_data.get("orig_type", "")
            
            if field_data.get("is_external_ref", False):
                return ObjectData(value, orig_type)
                
            if field_data.get("in_graph", False) and value in id_mapping:
                value = id_mapping[value]
                
            return ObjectData(value, orig_type)
            
        elif field_type == "UserDataData":
            value = field_data.get("value", 0)
            string = field_data.get("string", "")
            orig_type = field_data.get("orig_type", "")
            
            if field_data.get("in_graph", False) and value in id_mapping:
                value = id_mapping[value]
                
            return UserDataData(value, string, orig_type)
            
        elif field_type == "GameObjectRefData":
            guid_str = field_data.get("guid_str", "")
            guid_hex = field_data.get("raw_bytes", "")
            orig_type = field_data.get("orig_type", "")
            
            if guid_hex:
                try:
                    guid_bytes = bytes.fromhex(guid_hex)
                    
                    from file_handlers.rsz.rsz_guid_utils import process_gameobject_ref_data
                    return process_gameobject_ref_data(guid_hex, guid_str, orig_type, guid_mapping, randomize_guids)
                except Exception as e:
                    print(f"Error processing GameObjectRefData: {str(e)}")
                    return GameObjectRefData(guid_str, None, orig_type)
            else:
                return GameObjectRefData(guid_str, None, orig_type)
            
        elif field_type == "ArrayData":
            values = field_data.get("values", [])
            orig_type = field_data.get("orig_type", "")
            element_type = field_data.get("element_type", "")
            
            element_class = None
            if element_type:
                element_class = globals().get(element_type)
                
            array = ArrayData([], element_class, orig_type)
            
            for value_data in values:
                value_type = value_data.get("type", "")
                
                if value_type == "ObjectData" and value_data.get("in_graph", False):
                    relative_value = value_data.get("value", 0)
                    orig_type = value_data.get("orig_type", "")
                    
                    if relative_value in id_mapping:
                        new_value = id_mapping[relative_value]
                        array.values.append(ObjectData(new_value, orig_type))
                    else:
                        array.values.append(ObjectData(relative_value, orig_type))
                elif value_type == "UserDataData" and value_data.get("in_graph", False):
                    relative_index = value_data.get("value", 0)
                    string = value_data.get("string", "")
                    orig_type = value_data.get("orig_type", "")
                    
                    if relative_index in id_mapping:
                        new_index = id_mapping[relative_index]
                        array.values.append(UserDataData(new_index, string, orig_type))
                    else:
                        array.values.append(UserDataData(relative_index, string, orig_type))
                elif value_type == "GameObjectRefData":
                    guid_str = value_data.get("guid_str", "")
                    guid_hex = value_data.get("raw_bytes", "")
                    orig_type = value_data.get("orig_type", "")
                    
                    if guid_hex:
                        try:
                            
                            from file_handlers.rsz.rsz_guid_utils import process_gameobject_ref_data
                            ref_data = process_gameobject_ref_data(guid_hex, guid_str, orig_type, guid_mapping, randomize_guids)
                            if ref_data:
                                array.values.append(ref_data)
                        except Exception as e:
                            print(f"Error processing GameObjectRefData: {str(e)}")
                            array.values.append(GameObjectRefData(guid_str, None, orig_type))
                    else:
                        array.values.append(GameObjectRefData(guid_str, None, orig_type))
                else:
                    element = RszArrayClipboard._deserialize_element(value_data, element_class, guid_mapping, randomize_guids)
                    if element:
                        array.values.append(element)
                    
            return array
            
        return RszArrayClipboard._deserialize_element(field_data, None, guid_mapping, randomize_guids)
    
    @staticmethod
    def _deserialize_element(element_data, element_class, guid_mapping=None, randomize_guids=True):
        if guid_mapping is None:
            guid_mapping = {}
            
        element_type = element_data.get("type", "")
        orig_type = element_data.get("orig_type", "")
        
        if element_type == "ObjectData":
            od = ObjectData(
                element_data.get("value", 0),
                orig_type
            )
            return od
            
        elif element_type == "UserDataData":
            raise NotImplementedError("Unexpected UserDataData deserialization.")

        elif element_type == "F32Data":
            return F32Data(element_data.get("value", 0.0), orig_type)
            
        elif element_type == "U16Data":
            return U16Data(element_data.get("value", 0), orig_type)
            
        elif element_type == "S16Data":
            return S16Data(element_data.get("value", 0), orig_type)
            
        elif element_type == "S32Data":
            return S32Data(element_data.get("value", 0), orig_type)
            
        elif element_type == "U32Data":
            return U32Data(element_data.get("value", 0), orig_type)
            
        elif element_type == "U64Data":
            return U64Data(element_data.get("value", 0), orig_type)
            
        elif element_type == "S64Data":
            return S64Data(element_data.get("value", 0), orig_type)
            
        elif element_type == "S8Data":
            return S8Data(element_data.get("value", 0), orig_type)
            
        elif element_type == "U8Data":
            return U8Data(element_data.get("value", 0), orig_type)
            
        elif element_type == "BoolData":
            return BoolData(element_data.get("value", False), orig_type)
            
        elif element_type == "StringData":
            string_value = element_data.get("value", "")
            return StringData(string_value, orig_type)
            
        elif element_type == "ResourceData":
            string_value = element_data.get("value", "")
            if string_value and RszArrayClipboard.on_resource_data_deserialized:
                RszArrayClipboard.on_resource_data_deserialized(string_value)
            return ResourceData(string_value, orig_type)
        
        elif element_type == "RuntimeTypeData":
            return RuntimeTypeData(element_data.get("value", ""), orig_type)
            
        elif element_type == "Vec2Data":
            return Vec2Data(
                element_data.get("x", 0.0),
                element_data.get("y", 0.0),
                orig_type
            )
            
        elif element_type == "Vec3Data":
            return Vec3Data(
                element_data.get("x", 0.0),
                element_data.get("y", 0.0),
                element_data.get("z", 0.0),
                orig_type
            )
            
        elif element_type == "Vec3ColorData":
            return Vec3ColorData(
                element_data.get("x", 0.0),
                element_data.get("y", 0.0),
                element_data.get("z", 0.0),
                orig_type
            )
            
        elif element_type in ["Vec4Data", "Float4Data", "QuaternionData"]:
            cls = globals().get(element_type, Vec4Data)
            return cls(
                element_data.get("x", 0.0),
                element_data.get("y", 0.0),
                element_data.get("z", 0.0),
                element_data.get("w", 0.0),
                orig_type
            )
            
        elif element_type == "ColorData":
            return ColorData(
                element_data.get("r", 0),
                element_data.get("g", 0),
                element_data.get("b", 0),
                element_data.get("a", 0),
                orig_type
            )
            
        elif element_type == "RangeData":
            return RangeData(
                element_data.get("min", 0.0),
                element_data.get("max", 0.0),
                orig_type
            )
            
        elif element_type == "RangeIData":
            return RangeIData(
                element_data.get("min", 0),
                element_data.get("max", 0),
                orig_type
            )
            
        elif element_type == "GuidData":
            guid_str = element_data.get("guid_str", "")
            guid_hex = element_data.get("value", "")
            guid_bytes = bytes.fromhex(guid_hex) if guid_hex else None
            return GuidData(guid_str, guid_bytes, orig_type)
            

        elif element_type == "ArrayData":
            values = element_data.get("values", [])
            element_type_name = element_data.get("element_type", "")
            
            nested_element_class = None
            if element_type_name:
                nested_element_class = globals().get(element_type_name)
            
            array = ArrayData([], nested_element_class, orig_type)
            
            for value_data in values:
                element = RszArrayClipboard._deserialize_element(value_data, nested_element_class, guid_mapping, randomize_guids)
                if element:
                    array.values.append(element)
                    
            return array
            
        elif element_type == "CapsuleData":
            if "start" in element_data and element_data["start"]:
                start = RszArrayClipboard._deserialize_element(element_data["start"], Vec3Data)
                end = RszArrayClipboard._deserialize_element(element_data["end"], Vec3Data)
                return CapsuleData(start, end, element_data.get("radius", 0.0), orig_type)
            else:
                return CapsuleData(
                    Vec3Data(0, 0, 0, ""), 
                    Vec3Data(0, 0, 0, ""),
                    element_data.get("radius", 0.0), 
                    orig_type
                )
            
        elif element_type == "OBBData":
            if "values" in element_data and isinstance(element_data["values"], list):
                values = element_data["values"]
                return OBBData(values, orig_type)
            else:
                center = element_data.get("center", {})
                size = element_data.get("size", {})
                orientation = element_data.get("orientation", {})
                
                values = [
                    center.get("x", 0.0), center.get("y", 0.0), center.get("z", 0.0),
                    size.get("x", 0.0), size.get("y", 0.0), size.get("z", 0.0),
                    orientation.get("x", 0.0), orientation.get("y", 0.0), 
                    orientation.get("z", 0.0), orientation.get("w", 1.0)
                ]
                
                return OBBData(values, orig_type)
            
        elif element_type == "Mat4Data":
            values = element_data.get("values", [0.0] * 16)
            if len(values) < 16:
                values.extend([0.0] * (16 - len(values)))
                
            return Mat4Data(values, orig_type)

        elif element_type == "Int2Data":
            return Int2Data(
                element_data.get("x", 0),
                element_data.get("y", 0),
                orig_type
            )
            
        elif element_type == "Int3Data":
            return Int3Data(
                element_data.get("x", 0),
                element_data.get("y", 0),
                element_data.get("z", 0),
                orig_type
            )
            
        elif element_type == "Int4Data":
            return Int4Data(
                element_data.get("x", 0),
                element_data.get("y", 0),
                element_data.get("z", 0),
                element_data.get("w", 0),
                orig_type
            )
            
        elif element_type == "Float2Data":
            return Float2Data(
                element_data.get("x", 0.0),
                element_data.get("y", 0.0),
                orig_type
            )
            
        elif element_type == "Float3Data":
            return Float3Data(
                element_data.get("x", 0.0),
                element_data.get("y", 0.0),
                element_data.get("z", 0.0),
                orig_type
            )
        
        elif element_type == "AABBData":
            min_data = element_data.get("min", {})
            max_data = element_data.get("max", {})
            
            min_vec = RszArrayClipboard._deserialize_element(min_data, Vec3Data) if isinstance(min_data, dict) else Vec3Data()
            max_vec = RszArrayClipboard._deserialize_element(max_data, Vec3Data) if isinstance(max_data, dict) else Vec3Data()
            
            return AABBData(min_vec.x, min_vec.y, min_vec.z, max_vec.x, max_vec.y, max_vec.z, orig_type)
        
        elif element_type == "SphereData":
            center_data = element_data.get("center", {})
            radius = element_data.get("radius", 0.0)
            
            center = RszArrayClipboard._deserialize_element(center_data, Vec3Data) if isinstance(center_data, dict) else Vec3Data()
            
            return SphereData(center, radius, orig_type)
        
        elif element_type == "CylinderData":
            center_data = element_data.get("center", {})
            radius = element_data.get("radius", 0.0)
            height = element_data.get("height", 0.0)
            
            center = RszArrayClipboard._deserialize_element(center_data, Vec3Data) if isinstance(center_data, dict) else Vec3Data()
            
            return CylinderData(center, radius, height, orig_type)
        
        elif element_type == "AreaData":
            p0 = element_data.get("p0", Float2Data())
            p1 = element_data.get("p1", Float2Data())
            p2 = element_data.get("p2", Float2Data())
            p3 = element_data.get("p3", Float2Data())
            height = element_data.get("height", 0.0)
            bottom = element_data.get("bottom", 0.0)
            
            p0_deserialized = RszArrayClipboard._deserialize_element(p0, Float2Data) if isinstance(p0, dict) else Float2Data()
            p1_deserialized = RszArrayClipboard._deserialize_element(p1, Float2Data) if isinstance(p1, dict) else Float2Data()
            p2_deserialized = RszArrayClipboard._deserialize_element(p2, Float2Data) if isinstance(p2, dict) else Float2Data()
            p3_deserialized = RszArrayClipboard._deserialize_element(p3, Float2Data) if isinstance(p3, dict) else Float2Data()
            
            return AreaData(p0_deserialized, p1_deserialized, p2_deserialized, p3_deserialized, height, bottom, orig_type)

        elif element_type == "RectData":
            return RectData(
                element_data.get("min_x", 0.0),
                element_data.get("min_y", 0.0),
                element_data.get("max_x", 0.0),
                element_data.get("max_y", 0.0),
                orig_type
            )
        
        elif element_type == "LineSegmentData":
            start_data = element_data.get("start", {})
            end_data = element_data.get("end", {})
            
            start = RszArrayClipboard._deserialize_element(start_data, Vec3Data) if isinstance(start_data, dict) else Vec3Data()
            end = RszArrayClipboard._deserialize_element(end_data, Vec3Data) if isinstance(end_data, dict) else Vec3Data()
            
            return LineSegmentData(start, end, orig_type)
        
        elif element_type == "PointData":
            return PointData(
                element_data.get("x", 0.0),
                element_data.get("y", 0.0),
                element_data.get("z", 0.0),
                orig_type
            )
        
        elif element_type == "PositionData":
            return PositionData(
                element_data.get("x", 0.0),
                element_data.get("y", 0.0),
                element_data.get("z", 0.0),
                orig_type
            )
        
        elif element_type == "StructData":
            values = element_data.get("values", [])
            return StructData(values, orig_type)
            
        elif element_type == "RawBytesData":
            bytes_hex = element_data.get("bytes", "")
            bytes_val = bytes.fromhex(bytes_hex) if bytes_hex else b''
            size = element_data.get("size", len(bytes_val))
            
            return RawBytesData(bytes_val, size, orig_type)
        else:
            raise ValueError(f"Unsupported element type: {element_type}. Please open a github issue to add support for it.")
        
    @staticmethod
    def _add_element_to_ui_direct(widget, array_item, element):
        """Add a new element directly to the tree using the provided array item"""
        from file_handlers.pyside.tree_model import DataTreeBuilder
        
        parent_viewer = widget.parent()
        if not parent_viewer:
            return False
            
        model = getattr(widget, 'model', lambda: None)()
        if not model or not hasattr(array_item, 'raw'):
            return False
            
        array_data = array_item.raw.get('obj') if isinstance(array_item.raw, dict) else None
        if not array_data or not hasattr(array_data, 'values'):
            return False
        
        element_index = len(array_data.values) - 1
        
        if isinstance(element, ObjectData) and hasattr(parent_viewer, "name_helper"):
            type_name = parent_viewer.name_helper.get_type_name_for_instance(element.value)
            
            node_data = DataTreeBuilder.create_data_node(
                f"{element_index}: ({type_name})",
                "",
                None,
                element
            )
            
            if hasattr(parent_viewer, "scn") and element.value in parent_viewer.scn.parsed_elements:
                fields = parent_viewer.scn.parsed_elements[element.value]
                node_data["children"] = []
                for field_name, field_data in fields.items():
                    field_dict = parent_viewer._create_field_dict(field_name, field_data)
                    node_data["children"].append(field_dict)
        else:
            node_data = DataTreeBuilder.create_data_node(
                f"{element_index}: ",
                "",
                element.__class__.__name__,
                element
            )
        
        model.addChild(array_item, node_data)
        
        array_index = model.getIndexFromItem(array_item)
        widget.expand(array_index)
        
        if hasattr(widget, 'create_widgets_for_children'):
            child_index = model.index(element_index, 0, array_index)
            if child_index.isValid():
                widget.scrollTo(child_index)
                child_item = child_index.internalPointer()
                if child_item:
                    from file_handlers.pyside.tree_widgets import TreeWidgetFactory
                    if not TreeWidgetFactory.should_skip_widget(child_item):
                        name_text = child_item.data[0] if hasattr(child_item, 'data') and child_item.data else ""
                        node_type = child_item.raw.get("type", "") if isinstance(child_item.raw, dict) else ""
                        data_obj = child_item.raw.get("obj", None) if isinstance(child_item.raw, dict) else None
                        
                        widget_container = TreeWidgetFactory.create_widget(
                            node_type, data_obj, name_text, widget, 
                            widget.parent_modified_callback if hasattr(widget, 'parent_modified_callback') else None
                        )
                        if widget_container:
                            widget.setIndexWidget(child_index, widget_container)
        
        return True

    @staticmethod
    def _serialize_fields_for_userdata(fields, viewer):
        """Serialize instance fields, handling nested references"""
        serialised_fields = {}
        
        for field_name, field_value in fields.items():
            if is_reference_type(field_value):
                serialised = RszArrayClipboard._serialize_element(field_value)
                serialised["field_type"] = "reference"
                serialised_fields[field_name] = serialised
            elif isinstance(field_value, ArrayData):
                serialised = {
                    "type": "ArrayData",
                    "values": [],
                    "orig_type": field_value.orig_type,
                    "element_type": field_value.element_class.__name__ if field_value.element_class else ""
                }
                for element in field_value.values:
                    serialised["values"].append(RszArrayClipboard._serialize_element(element))
                serialised_fields[field_name] = serialised
            else:
                serialised_fields[field_name] = RszArrayClipboard._serialize_element(field_value)
        
        return serialised_fields

    @staticmethod
    def _convert_object_graph_to_embedded_data(userdata_info, object_graph):
        """Convert the richer 'object_graph' structure into the simpler 'embedded_data' expected by
        RszEmbeddedArrayOperations._paste_userdata_with_full_content."""
        embedded_data = {
            "type_id": getattr(userdata_info, 'type_id', 0),
            "name": getattr(userdata_info, 'name', ""),
            "instance_infos": [],
            "instances": {},
            # object_table will be filled after we have the orig→rel mapping
            "object_table": []
        }

        # build a quick lookup: original_id -> relative_id for later remapping
        orig_to_rel = {}

        def _mark_relative(d):
            """Recursively walk a serialized field structure and mark internal references
            (those that were part of the copied object graph) with the flag 'is_relative'.
            We detect these by the presence of the helper key 'in_graph' that was set during
            serialization. This extra flag is what _restore_embedded_content relies on to
            shift IDs by +1 when rebuilding the embedded RSZ."""
            if isinstance(d, dict):
                if d.get("in_graph"):
                    d["is_relative"] = True
                if "in_graph" in d:
                    del d["in_graph"]
                if d.get("type") == "ArrayData" and "values" in d:
                    for elem in d["values"]:
                        _mark_relative(elem)
                else:
                    for key, val in list(d.items()):
                        if isinstance(val, dict) or isinstance(val, list):
                            _mark_relative(val)
            elif isinstance(d, list):
                for item in d:
                    _mark_relative(item)

        for inst_entry in object_graph.get("instances", []):
            rel_id = inst_entry.get("id", 0)
            orig_id = inst_entry.get("original_id", rel_id)
            embedded_data["instance_infos"].append({
                "relative_id": rel_id,
                "type_id": inst_entry.get("type_id", 0)
            })
            import copy
            fields_copy = copy.deepcopy(inst_entry.get("fields", {}))
            _mark_relative(fields_copy)
            embedded_data["instances"][str(rel_id)] = fields_copy

            orig_to_rel[orig_id] = rel_id

        # remap object_table (original IDs) to relative IDs used in embedded_data
        for oid in object_graph.get("embedded_object_table", []):
            embedded_data["object_table"].append(orig_to_rel.get(oid, 0))
        return embedded_data