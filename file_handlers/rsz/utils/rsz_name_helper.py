from file_handlers.rsz.rsz_data_types import StringData


class RszViewerNameHelper:
    def __init__(self, scn, type_registry):
        self.scn = scn
        self.type_registry = type_registry
        self._last_added_object = None
    
    def get_gameobject_name(self, instance_index, default_name):
        if instance_index is None:
            return default_name
            
        name = self.get_instance_first_field_name(instance_index)
        return name if name else default_name
            
    def get_folder_name(self, folder_id, default_name):
        if folder_id >= len(self.scn.object_table):
            return default_name
            
        instance_id = self.scn.object_table[folder_id]
        name = self.get_instance_first_field_name(instance_id)
        return name if name else default_name

    def get_instance_first_field_name(self, instance_id):
        if instance_id in self.scn.parsed_elements:
            fields = self.scn.parsed_elements[instance_id]
            for field_value in fields.values():
                if isinstance(field_value, StringData):
                    return field_value.value.rstrip("\x00")
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
        for rui in self.scn.rsz_userdata_infos:
            if rui.instance_id == ref_id:
                return self.scn.get_rsz_userdata_string(rui)
        return ""
        
