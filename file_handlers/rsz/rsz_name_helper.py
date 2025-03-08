"""
Helper class for managing instance names in RSZ files.

This file contains utility methods to determine display names for:
- GameObjects
- Components
- Folders
- UserData
- Any RSZ instance
"""

from file_handlers.rsz.rsz_data_types import StringData


class RszViewerNameHelper:
    """Helper class for handling instance name resolution in RSZ files"""
    
    def __init__(self, scn, type_registry):
        self.scn = scn
        self.type_registry = type_registry
        self._last_added_object = None
    
    def get_gameobject_name(self, instance_index, default_name):
        """Get display name for a GameObject"""
        if instance_index is None:
            return default_name
            
        v0_name = self.get_instance_v0_name(instance_index)
        return v0_name if v0_name else default_name
            
    def get_folder_name(self, folder_id, default_name):
        """Get display name for a Folder"""
        if folder_id >= len(self.scn.object_table):
            return default_name
            
        instance_id = self.scn.object_table[folder_id]
        v0_name = self.get_instance_v0_name(instance_id)
        return v0_name if v0_name else default_name

    def get_instance_v0_name(self, instance_id):
        """Get name from v0 field if available."""
        if instance_id in self.scn.parsed_elements:
            fields = self.scn.parsed_elements[instance_id]
            if "v0" in fields and isinstance(fields["v0"], StringData):
                return fields["v0"].value.rstrip("\x00")
        return None
        
    def get_instance_name(self, instance_id):
        """Fallback name from type info or instance id."""
        if instance_id >= len(self.scn.instance_infos):
            return f"Instance[{instance_id}]"
        inst_info = self.scn.instance_infos[instance_id]
        type_info = self.type_registry.get_type_info(inst_info.type_id)
        if type_info and "name" in type_info:
            return type_info["name"]
        return f"Instance[{instance_id}]"
        
    def get_type_name_for_instance(self, instance_id):
        """Get type name for an instance ID with optimized lookup"""
        if instance_id >= len(self.scn.instance_infos):
            return "Invalid ID"
        if (
            self._last_added_object is not None
            and self._last_added_object.value == instance_id
            and self._last_added_object.orig_type
        ):
            return self._last_added_object.orig_type
        inst_info = self.scn.instance_infos[instance_id]
        type_info = self.type_registry.get_type_info(inst_info.type_id)
        return (
            type_info.get("name", f"Type 0x{inst_info.type_id:08X}")
            if type_info
            else f"Type 0x{inst_info.type_id:08X}"
        )
        
    def get_userdata_display_value(self, ref_id):
        """Get display value for UserData reference"""
        for rui in self.scn.rsz_userdata_infos:
            if rui.instance_id == ref_id:
                return self.scn.get_rsz_userdata_string(rui)
        return ""
        
    def set_last_added_object(self, obj):
        """Set the last added object for type name optimization"""
        self._last_added_object = obj
