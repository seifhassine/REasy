"""
Utility functions for embedded RSZ operations.
"""

from utils.id_manager import EmbeddedIdManager
from file_handlers.rsz.rsz_data_types import ArrayData


def update_rsz_header_counts(rui, skip_instance_count=False):
    """
    Helper to update object_count, instance_count, and userdata_count in embedded_rsz_header.
    If skip_instance_count is True, we do not overwrite instance_count (some operations need
    special instance_count handling).
    """
    if hasattr(rui, 'embedded_rsz_header'):
        if hasattr(rui, 'embedded_object_table'):
            rui.embedded_rsz_header.object_count = len(rui.embedded_object_table)
        if not skip_instance_count and hasattr(rui, 'embedded_instance_infos'):
            rui.embedded_rsz_header.instance_count = len(rui.embedded_instance_infos)
        if hasattr(rui, 'embedded_userdata_infos'):
            rui.embedded_rsz_header.userdata_count = len(rui.embedded_userdata_infos)


def create_embedded_instance_info(type_id, type_registry=None):
    """Create an embedded instance info structure with the given type ID."""
    class EmbeddedInstanceInfo:
        def __init__(self):
            self.type_id = 0
            self.crc = 0
    
    instance_info = EmbeddedInstanceInfo()
    instance_info.type_id = type_id
    
    if type_registry:
        type_info = type_registry.get_type_info(type_id)
        if type_info and "crc" in type_info:
            instance_info.crc = int(type_info["crc"], 16)
    
    return instance_info


def copy_embedded_rsz_header(source_header, target_header):
    """Copy attributes from source embedded RSZ header to target header."""
    # List of attributes to copy
    attrs_to_copy = [
        'magic', 'version', 'object_count', 'instance_count',
        'userdata_count', 'instance_offset', 'type_info_offset',
        'userdata_offset', 'string_table_offset', 'unknown_offset'
    ]
    
    for attr in attrs_to_copy:
        if hasattr(source_header, attr):
            try:
                setattr(target_header, attr, getattr(source_header, attr))
            except AttributeError:
                pass
    
    if hasattr(target_header, 'version') and target_header.version >= 4:
        if hasattr(source_header, 'reserved'):
            target_header.reserved = source_header.reserved
        else:
            target_header.reserved = 0


def create_scn19_userdata_info(instance_id, type_id, type_name, parent_rui=None, crc=None):
    """Create a Scn19RSZUserDataInfo with the common identity fields populated."""
    from file_handlers.rsz.scn_19.scn_19_structure import Scn19RSZUserDataInfo

    userdata_info = Scn19RSZUserDataInfo()
    userdata_info.instance_id = instance_id
    userdata_info.type_id = type_id
    if crc is not None:
        userdata_info.crc = crc
    userdata_info.name = type_name
    userdata_info.value = type_name
    if parent_rui is not None:
        userdata_info.parent_userdata_rui = parent_rui
    userdata_info.data = b""
    userdata_info.data_size = 0
    return userdata_info


def initialize_scn19_userdata_runtime(
    userdata_info,
    source_header,
    instance_id=None,
    *,
    include_parsed_elements=False,
    include_maps=False,
    include_array_counters=False,
    include_hierarchy=True,
    modified=None,
):
    """Initialize optional embedded-RSZ runtime containers on a userdata object."""
    if source_header is not None:
        userdata_info.embedded_rsz_header = type(source_header)()
        copy_embedded_rsz_header(source_header, userdata_info.embedded_rsz_header)

    userdata_info.embedded_instances = {}
    userdata_info.embedded_instance_infos = []
    userdata_info.embedded_userdata_infos = []
    userdata_info.embedded_object_table = []

    if include_parsed_elements:
        userdata_info.parsed_elements = {}
    if instance_id is not None:
        userdata_info.id_manager = EmbeddedIdManager(instance_id)
    if include_maps:
        userdata_info._rsz_userdata_dict = {}
        userdata_info._rsz_userdata_set = set()
        userdata_info._rsz_userdata_str_map = {}
    if include_hierarchy:
        userdata_info.embedded_instance_hierarchy = {}
    if include_array_counters:
        userdata_info._array_counters = {}
    if modified is not None:
        userdata_info.modified = modified


def shift_embedded_instances_for_insertion(rui, insertion_index, count, type_registry):
    """Shift embedded instance ids up to make room at insertion_index."""
    id_shift = {}
    for old_id in sorted(rui.embedded_instances.keys()):
        if old_id >= insertion_index:
            id_shift[old_id] = old_id + count

    if not id_shift:
        return {}

    new_instances = {}
    for old_id, fields in rui.embedded_instances.items():
        new_id = id_shift.get(old_id, old_id)
        new_instances[new_id] = fields
    rui.embedded_instances = new_instances

    if hasattr(rui, 'embedded_instance_infos'):
        max_new_id = max(id_shift.values())
        while len(rui.embedded_instance_infos) <= max_new_id:
            rui.embedded_instance_infos.append(create_embedded_instance_info(0, type_registry))

        for old_id, new_id in sorted(id_shift.items(), reverse=True):
            if old_id < len(rui.embedded_instance_infos):
                rui.embedded_instance_infos[new_id] = rui.embedded_instance_infos[old_id]
                rui.embedded_instance_infos[old_id] = None

    update_embedded_references_for_shift(id_shift, rui)

    if hasattr(rui, 'embedded_userdata_infos'):
        for ud in rui.embedded_userdata_infos:
            if hasattr(ud, 'instance_id') and ud.instance_id in id_shift:
                ud.instance_id = id_shift[ud.instance_id]

    if hasattr(rui, 'embedded_object_table'):
        rui.embedded_object_table = [
            id_shift.get(x, x) for x in rui.embedded_object_table
        ]

    return id_shift


def create_embedded_userdata_info(instance_id, type_id, type_name):
    """Create a new embedded UserData info structure."""
    class EmbeddedUserDataInfo:
        def __init__(self):
            self.instance_id = 0
            self.name_offset = 0
            self.data_offset = 0
            self.data_size = 0
            self.type_id = 0
            self.name = ""
            self.modified = False
            self.value = ""
            self.hash = 0
    
    userdata_info = EmbeddedUserDataInfo()
    userdata_info.instance_id = instance_id
    userdata_info.type_id = type_id
    userdata_info.name = type_name
    userdata_info.value = type_name
    userdata_info.data = b""
    userdata_info.data_size = 0
    
    return userdata_info


def initialize_embedded_rsz_structures(userdata_info, source_header=None, instance_id=None):
    """Initialize embedded RSZ structures for a UserData info object."""
    if source_header:
        userdata_info.embedded_rsz_header = type(source_header)()
        copy_embedded_rsz_header(source_header, userdata_info.embedded_rsz_header)
        
        userdata_info.embedded_rsz_header.object_count = 1
        userdata_info.embedded_rsz_header.instance_count = 2
        userdata_info.embedded_rsz_header.userdata_count = 0
    
    userdata_info.embedded_instances = {}
    userdata_info.embedded_instance_infos = []
    userdata_info.embedded_userdata_infos = []
    userdata_info.embedded_object_table = []  # Will be set by caller based on instance layout
    userdata_info.parsed_elements = {}
    userdata_info._rsz_userdata_dict = {}
    userdata_info._rsz_userdata_set = set()
    userdata_info._rsz_userdata_str_map = {}
    userdata_info.embedded_instance_hierarchy = {}
    userdata_info.modified = False
    
    if instance_id is not None:
        userdata_info.id_manager = EmbeddedIdManager(instance_id)


def find_embedded_context(item):
    """Find the embedded context for a tree item."""
    if not item:
        print("  Item is None")
        return None
    
    
    # Check if the item itself has embedded context
    if hasattr(item, 'raw') and isinstance(item.raw, dict):
        raw = item.raw
        if 'embedded_context' in raw:
            context = raw.get('embedded_context')
            return context
    
    # Check parent items
    parent = item.parent if hasattr(item, 'parent') else None
    level = 1
    while parent:
        if hasattr(parent, 'raw') and isinstance(parent.raw, dict):
            raw = parent.raw
            if 'embedded_context' in raw:
                context = raw.get('embedded_context')
                return context
        parent = parent.parent if hasattr(parent, 'parent') else None
        level += 1
    
    # special case: if this is a userdata array that needs embedded RSZ
    # but is in a non-embedded context, we we need to check if it's a userdata array
    if hasattr(item, 'raw') and isinstance(item.raw, dict):
        raw = item.raw
        if raw.get('type') == 'array' and 'obj' in raw:
            array_obj = raw['obj']
            if hasattr(array_obj, 'orig_type') and array_obj.orig_type:
                array_type = array_obj.orig_type
                if 'UserData' in array_type or 'userdata' in array_type.lower():
                    return "userdata_array_needs_embedded"
    
    print("  No embedded context found")
    return None


def get_embedded_context_info(embedded_context):
    """Get information about an embedded context."""
    if not embedded_context:
        return {
            'domain_id': 0,
            'has_instances': False,
            'has_userdata': False,
            'instance_count': 0,
            'userdata_count': 0
        }
    
    return {
        'domain_id': getattr(embedded_context, 'instance_id', 0),
        'has_instances': hasattr(embedded_context, 'embedded_instances'),
        'has_userdata': hasattr(embedded_context, 'embedded_userdata_infos'),
        'instance_count': len(getattr(embedded_context, 'embedded_instances', {})),
        'userdata_count': len(getattr(embedded_context, 'embedded_userdata_infos', []))
    }


def mark_parent_chain_modified(rui, viewer=None):
    """Mark the entire parent chain as modified."""
    try:
        if hasattr(rui, 'modified'):
            rui.modified = True
        
        # Walk up the parent chain
        if hasattr(rui, 'parent_userdata_rui') and rui.parent_userdata_rui:
            if hasattr(rui.parent_userdata_rui, 'modified'):
                rui.parent_userdata_rui.modified = True
            
            # Check grandparent
            if hasattr(rui.parent_userdata_rui, 'parent_userdata_rui') and rui.parent_userdata_rui.parent_userdata_rui:
                grandparent = rui.parent_userdata_rui.parent_userdata_rui
                if hasattr(grandparent, 'modified'):
                    grandparent.modified = True
        
        # Mark viewer as modified if provided
        if viewer and hasattr(viewer, 'mark_modified'):
            viewer.mark_modified()
            
    except Exception as ex:
        print(f"[WARNING] Error in mark_parent_chain_modified: {ex}")


def build_context_chain(context):
    """Build a chain of parent contexts."""
    chain = [context]
    current = context
    
    while hasattr(current, 'parent_userdata_rui') and current.parent_userdata_rui:
        chain.append(current.parent_userdata_rui)
        current = current.parent_userdata_rui
    
    return chain


def update_embedded_references_for_shift(id_mapping, rui):
    """Update all embedded references when IDs are shifted."""
    if not hasattr(rui, 'embedded_instances'):
        return
    
    for instance_id, fields in rui.embedded_instances.items():
        if not isinstance(fields, dict):
            continue
            
        for field_name, field_data in fields.items():
            if hasattr(field_data, 'value') and field_data.value in id_mapping:
                field_data.value = id_mapping[field_data.value]
            
            elif hasattr(field_data, 'values') and hasattr(field_data.values, '__iter__'):
                for element in field_data.values:
                    if hasattr(element, 'value') and element.value in id_mapping:
                        element.value = id_mapping[element.value]
                
                if isinstance(field_data, ArrayData) and hasattr(field_data, '_owning_instance_id'):
                    if field_data._owning_instance_id in id_mapping:
                        field_data._owning_instance_id = id_mapping[field_data._owning_instance_id]
    
    if hasattr(rui, '_array_registry'):
        for array_id, owner_id in list(rui._array_registry.items()):
            if owner_id in id_mapping:
                rui._array_registry[array_id] = id_mapping[owner_id]