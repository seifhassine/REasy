import os
import json
from file_handlers.rsz.rsz_data_types import (
    ObjectData, UserDataData, F32Data, U16Data, S16Data, S32Data, U32Data, U64Data, S64Data, S8Data, U8Data, BoolData,
    StringData, ResourceData, RuntimeTypeData, Vec2Data, Vec3Data, Vec3ColorData, Vec4Data, Float4Data, QuaternionData,
    ColorData, RangeData, RangeIData, GuidData, GameObjectRefData, ArrayData, CapsuleData, OBBData, Mat4Data, Int2Data,
    Int3Data, Int4Data, Float2Data, Float3Data, AABBData, SphereData, CylinderData, AreaData, RectData, LineSegmentData,
    PointData, StructData, RawBytesData
)
from file_handlers.rsz.rsz_clipboard_utils import RszClipboardUtils

class RszArrayClipboard:
    
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
    def copy_to_clipboard(widget, element, array_type):
        parent_viewer = widget.parent()
        serialised = (RszArrayClipboard._serialize_object_with_graph(element, parent_viewer)
                    if isinstance(element, ObjectData) and element.value > 0
                    else RszArrayClipboard._serialize_element(element))

        RszArrayClipboard._write_clipboard(
            array_type,
            [serialised],
            RszArrayClipboard.get_clipboard_file(widget)
        )
        return True

    @staticmethod
    def _json_serializer(obj):
        return RszClipboardUtils.json_serializer(obj)

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
                
                if orig_id in viewer.scn.parsed_elements:
                    fields = viewer.scn.parsed_elements[orig_id]
                    for field_name, field_data in fields.items():
                        instance_data["fields"][field_name] = RszArrayClipboard._serialize_field_with_mapping(
                            field_data, id_mapping, filtered_graph_ids, external_refs
                        )
                        
                result["object_graph"]["instances"].append(instance_data)
        
        return result

    @staticmethod
    def _collect_nested_objects(viewer, root_id):
        all_ids = set()
        processed = set()
        to_process = [root_id]
        
        while to_process:
            current_id = to_process.pop(0)
            
            if current_id in processed:
                continue
                
            processed.add(current_id)
            
            if current_id <= 0 or current_id >= len(viewer.scn.instance_infos):
                continue
                
            if current_id not in viewer.scn.parsed_elements:
                continue
                
            fields = viewer.scn.parsed_elements[current_id]
            for field_name, field_data in fields.items():
                if isinstance(field_data, ObjectData) and field_data.value > 0:
                    ref_id = field_data.value
                    if ref_id not in processed:
                        all_ids.add(ref_id)
                        to_process.append(ref_id)
                        
                elif isinstance(field_data, ArrayData):
                    for element in field_data.values:
                        if isinstance(element, ObjectData) and element.value > 0:
                            ref_id = element.value
                            if ref_id not in processed:
                                all_ids.add(ref_id)
                                to_process.append(ref_id)
        
        return all_ids

    @staticmethod
    def _serialize_field_with_mapping(field_data, id_mapping, nested_ids, external_refs):
        if isinstance(field_data, ObjectData) or isinstance(field_data, UserDataData):
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
                if (isinstance(element, ObjectData) or isinstance(element, UserDataData)) and element.value > 0:
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
                        result["values"].append(RszArrayClipboard._serialize_element(element))
                else:
                    result["values"].append(RszArrayClipboard._serialize_element(element))
                    
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
        else: #if isinstance(element, RawBytesData):
            return {
                "type": "RawBytesData",
                "bytes": element.raw_bytes.hex() if element.raw_bytes else "",
                "size": element.field_size,
                "orig_type": element.orig_type
            }
            
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

        for elem_data in items_data:
            if elem_data.get("type") == "ObjectData" and "object_graph" in elem_data:
                ins_idx = RszArrayClipboard._calculate_insertion_index(
                    array_data, array_item, viewer
                )
                element = RszArrayClipboard._paste_object_graph(
                    viewer, elem_data, array_data, ins_idx
                )
            else:
                element = RszArrayClipboard._deserialize_element(
                    elem_data, array_data.element_class, {}
                )
                if element:
                    array_data.values.append(element)
            if element:
                if viewer and hasattr(viewer, "mark_modified"):
                    viewer.mark_modified()
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
    def copy_multiple_to_clipboard(widget, elements, array_type):
        parent_viewer = widget.parent()
        serialised_items = [
            (RszArrayClipboard._serialize_object_with_graph(el, parent_viewer)
            if isinstance(el, ObjectData) and el.value > 0
            else RszArrayClipboard._serialize_element(el))
            for el in elements
        ]

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
            if hasattr(viewer.array_operations, "_calculate_insertion_index"):
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
                    field_data, relative_to_new_id, guid_mapping
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
        else:
            print(f"Failed to create root instance (ID: {root_relative_id})")
            return None
            
    
    @staticmethod
    def _deserialize_field_with_relative_mapping(field_data, id_mapping, guid_mapping=None):
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
            
            is_null_ref = not guid_hex or guid_hex == "00000000000000000000000000000000"
            
            if is_null_ref:
                return GameObjectRefData("", None, field_data.get("orig_type", ""))
            else:
                if guid_hex in guid_mapping:
                    return guid_mapping[guid_hex].copy()
                    
                print(f"Creating new GameObjectRefData for GUID: {guid_hex}")
                guid_bytes = bytes.fromhex(guid_hex) if guid_hex else None
                ref = GameObjectRefData(guid_str, guid_bytes, field_data.get("orig_type", ""))
                
                guid_mapping[guid_hex] = ref
                return ref
            
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
                    
                    print(f"Creating new GameObjectRefData for GUID: {guid_hex}")
                    is_null_ref = not guid_hex or guid_hex == "00000000000000000000000000000000"
                    
                    if is_null_ref:
                        array.values.append(GameObjectRefData("", None, value_data.get("orig_type", "")))
                    else:
                        if guid_hex in guid_mapping:
                            array.values.append(guid_mapping[guid_hex])
                        else:
                            guid_bytes = bytes.fromhex(guid_hex) if guid_hex else None
                            ref = GameObjectRefData(guid_str, guid_bytes, value_data.get("orig_type", ""))
                            guid_mapping[guid_hex] = ref
                            array.values.append(ref)
                else:
                    element = RszArrayClipboard._deserialize_element(value_data, element_class, guid_mapping)
                    if element:
                        array.values.append(element)
                    
            return array
            
        return RszArrayClipboard._deserialize_element(field_data, None, guid_mapping)
    
    @staticmethod
    def _deserialize_element(element_data, element_class, guid_mapping=None):
        if guid_mapping is None:
            guid_mapping = {}
            
        element_type = element_data.get("type", "")
        orig_type = element_data.get("orig_type", "")
        
        if element_type == "ObjectData":
            return ObjectData(
                element_data.get("value", 0), 
                orig_type
            )
            
        elif element_type == "UserDataData":
            ud = UserDataData(
                element_data.get("value", ""),
                element_data.get("string", 0),
                orig_type
            )
            return ud
            
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
                element = RszArrayClipboard._deserialize_element(value_data, nested_element_class, guid_mapping)
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
        
        elif element_type == "StructData":
            values = element_data.get("values", [])
            return StructData(values, orig_type)
            
        else: #if element_type == "RawBytesData":
            bytes_hex = element_data.get("bytes", "")
            bytes_val = bytes.fromhex(bytes_hex) if bytes_hex else b''
            size = element_data.get("size", len(bytes_val))
            
            return RawBytesData(bytes_val, size, orig_type)
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
