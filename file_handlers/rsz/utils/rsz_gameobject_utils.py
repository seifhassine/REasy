"""
Shared GameObject and object-table helpers for RSZ operations.
"""

from file_handlers.rsz.rsz_data_types import StringData
from file_handlers.rsz.utils.rsz_guid_utils import create_guid_data, create_new_guid


DEFAULT_GAMEOBJECT_NAME = "GameObject"


def create_gameobject_entry(scn, object_id, parent_id):
    """Create a GameObject entry matching the current RSZ container type."""
    if getattr(scn, "is_pfb", False):
        from file_handlers.rsz.rsz_file import PfbGameObject

        new_go = PfbGameObject()
    else:
        from file_handlers.rsz.rsz_file import RszGameObject

        new_go = RszGameObject()

    new_go.id = object_id
    new_go.parent_id = parent_id
    new_go.component_count = 0

    if getattr(scn, "is_scn", False):
        new_go.guid = create_new_guid()
        new_go.prefab_id = -1

    return new_go


def update_gameobject_hierarchy(scn, gameobject):
    """Update instance hierarchy with a GameObject parent-child relationship."""
    if gameobject.id >= len(scn.object_table):
        return

    instance_id = scn.object_table[gameobject.id]
    scn.instance_hierarchy[instance_id] = {"children": [], "parent": None}

    if 0 <= gameobject.parent_id < len(scn.object_table):
        parent_instance_id = scn.object_table[gameobject.parent_id]
        if parent_instance_id > 0:
            scn.instance_hierarchy[instance_id]["parent"] = parent_instance_id
            if parent_instance_id in scn.instance_hierarchy:
                parent_entry = scn.instance_hierarchy[parent_instance_id]
                parent_entry.setdefault("children", []).append(instance_id)


def find_gameobject_by_id(scn, gameobject_id):
    """Return the GameObject with the given object-table ID, if present."""
    for gameobject in getattr(scn, "gameobjects", []):
        if gameobject.id == gameobject_id:
            return gameobject
    return None


def insert_into_object_table(scn, object_table_index, instance_id):
    """Insert an object-table entry and shift object/folder/PFB reference IDs."""
    if object_table_index >= len(scn.object_table):
        scn.object_table.extend([0] * (object_table_index - len(scn.object_table) + 1))
        scn.object_table[object_table_index] = instance_id
    else:
        scn.object_table.insert(object_table_index, instance_id)

    for gameobject in getattr(scn, "gameobjects", []):
        if gameobject.id >= object_table_index:
            gameobject.id += 1
        if gameobject.parent_id >= object_table_index:
            gameobject.parent_id += 1

    for folder in getattr(scn, "folder_infos", []):
        if folder.id >= object_table_index:
            folder.id += 1
        if folder.parent_id >= object_table_index:
            folder.parent_id += 1

    if getattr(scn, "is_pfb", False):
        for ref_info in getattr(scn, "gameobject_ref_infos", []):
            if hasattr(ref_info, "object_id") and ref_info.object_id >= object_table_index:
                ref_info.object_id += 1
            if hasattr(ref_info, "target_id") and ref_info.target_id >= object_table_index:
                ref_info.target_id += 1


def remove_from_object_table(scn, object_table_index):
    """Remove an object-table entry and shift object/folder/PFB reference IDs."""
    scn.object_table.pop(object_table_index)

    for gameobject in getattr(scn, "gameobjects", []):
        if gameobject.id > object_table_index:
            gameobject.id -= 1
        if gameobject.parent_id > object_table_index:
            gameobject.parent_id -= 1

    for folder in getattr(scn, "folder_infos", []):
        if folder.id > object_table_index:
            folder.id -= 1
        if folder.parent_id > object_table_index:
            folder.parent_id -= 1

    if getattr(scn, "is_pfb", False):
        for ref_info in getattr(scn, "gameobject_ref_infos", []):
            if hasattr(ref_info, "object_id") and ref_info.object_id > object_table_index:
                ref_info.object_id -= 1
            if hasattr(ref_info, "target_id") and ref_info.target_id > object_table_index:
                ref_info.target_id -= 1


def find_name_field(fields):
    """Find the most likely name field for a GameObject or Folder instance."""
    if "Name" in fields:
        return fields["Name"]

    for field_name, field_data in fields.items():
        if isinstance(field_data, StringData) and "name" in field_name.lower():
            return field_data

    for field_data in fields.values():
        if isinstance(field_data, StringData):
            return field_data

    return None


def apply_name_to_instance(scn, instance_id, new_name):
    """Apply a name to the best matching string field for an instance."""
    fields = scn.parsed_elements[instance_id]
    name_field = find_name_field(fields)
    if name_field is None:
        return False
    name_field.value = new_name
    return True


def get_instance_name_from_fields(fields, default_name=DEFAULT_GAMEOBJECT_NAME):
    """Resolve an instance display name from its field data."""
    name_field = find_name_field(fields)
    if name_field is None:
        return (default_name or DEFAULT_GAMEOBJECT_NAME).strip("\00")
    return (name_field.value or default_name or DEFAULT_GAMEOBJECT_NAME).strip("\00")


def add_guid_to_settings(scn, instance_id, guid_bytes):
    """Add or update the display-only GUID field for a GameObject instance."""
    if instance_id not in scn.parsed_elements:
        return

    fields = scn.parsed_elements[instance_id]
    guid_data = create_guid_data(guid_bytes)
    guid_data._display_only = True

    for gameobject in getattr(scn, "gameobjects", []):
        if gameobject.id < len(scn.object_table) and scn.object_table[gameobject.id] == instance_id:
            guid_data.gameobject = gameobject
            break

    if "GUID" not in fields:
        new_fields = {"GUID": guid_data}
        new_fields.update(fields)
        scn.parsed_elements[instance_id] = new_fields
    else:
        fields["GUID"] = guid_data
