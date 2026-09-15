import os
import json
import traceback

class RszClipboardUtils:
    """Shared utilities for clipboard operations across different clipboard types"""
    
    @staticmethod
    def get_base_clipboard_directory():
        """Get the base clipboard directory for all clipboard types"""
        base_dir = os.getcwd()
        clipboard_dir = os.path.join(base_dir, ".clipboard")
        
        if not os.path.exists(clipboard_dir):
            os.makedirs(clipboard_dir, exist_ok=True)
            
        return clipboard_dir
    
    @staticmethod
    def get_type_clipboard_directory(clipboard_type):
        """Get a clipboard directory for a specific type"""
        base_dir = RszClipboardUtils.get_base_clipboard_directory()
        type_dir = os.path.join(base_dir, clipboard_type)
        
        if not os.path.exists(type_dir):
            os.makedirs(type_dir, exist_ok=True)
            
        return type_dir
    
    @staticmethod
    def get_json_name(widget):
        parent_getter = getattr(widget, 'parent', None)
        parent = parent_getter() if callable(parent_getter) else None

        for candidate in (parent, widget):
            handler = getattr(candidate, 'handler', None)
            type_registry = getattr(handler, 'type_registry', None)
            if type_registry is None or not hasattr(type_registry, 'json_path'):
                continue

            json_path = type_registry.json_path
            if json_path is not None:
                return os.path.basename(json_path).split(".")[0]

        return None

    @staticmethod
    def format_clipboard_file(
        directory,
        json_name,
        clipboard_type,
        filename_template="{name}-{type}-clipboard.json",
        default_name=None,
    ):
        if not json_name and default_name is not None:
            json_name = default_name
        base_name = os.path.splitext(json_name)[0]
        return os.path.join(
            directory,
            filename_template.format(name=base_name, type=clipboard_type),
        )

    @staticmethod
    def has_clipboard_data(clipboard_file):
        return os.path.exists(clipboard_file)

    @staticmethod
    def write_clipboard_data(clipboard_file, data):
        """Write JSON using the legacy RSZ clipboard encoding and error behavior."""
        with open(clipboard_file, "w") as stream:
            json.dump(
                data,
                stream,
                indent=2,
                default=RszClipboardUtils.json_serializer,
            )

    @staticmethod
    def load_clipboard_data(clipboard_file):
        if not os.path.exists(clipboard_file):
            print(f"Clipboard file does not exist: {clipboard_file}")
            return None
        try:
            with open(clipboard_file, 'r') as f:
                data = json.load(f)
                print(f"Successfully loaded clipboard data from {clipboard_file}")
                return data
        except Exception as e:
            print(f"Error reading clipboard: {str(e)}")
            traceback.print_exc()
            return None
    
    @staticmethod
    def json_serializer(obj):
        if isinstance(obj, bytes):
            return obj.hex()
            
        if hasattr(obj, '__dict__'):
            return obj.__dict__
            
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")
    
    @staticmethod
    def get_type_info_name(viewer, type_id):
        if hasattr(viewer, "type_registry") and viewer.type_registry:
            type_info = viewer.type_registry.get_type_info(type_id)
            if type_info and "name" in type_info:
                return type_info["name"]
        return None
    
    @staticmethod
    def check_userdata_info(viewer, instance_id):
        """Check if an instance is userdata and get its info"""
        if not (hasattr(viewer.scn, '_rsz_userdata_set') and instance_id in viewer.scn._rsz_userdata_set):
            return None
            
        for rui in viewer.scn.rsz_userdata_infos:
            if rui.instance_id == instance_id:
                userdata_hash = 0
                if hasattr(rui, 'hash'):
                    userdata_hash = rui.hash
                elif hasattr(rui, 'json_path_hash'):
                    userdata_hash = rui.json_path_hash
                
                userdata_info = {
                    "userdata_hash": userdata_hash,
                    "userdata_string": None
                }
                
                if hasattr(viewer.scn, '_rsz_userdata_str_map') and rui in viewer.scn._rsz_userdata_str_map:
                    userdata_info["userdata_string"] = viewer.scn._rsz_userdata_str_map[rui]
                
                return userdata_info

        return None
