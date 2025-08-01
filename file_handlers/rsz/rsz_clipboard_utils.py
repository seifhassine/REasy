import os
import json
import traceback

class RszClipboardUtils:
    """Shared utilities for clipboard operations across different clipboard types"""
    
    @staticmethod
    def get_base_clipboard_directory():
        """Get the base clipboard directory for all clipboard types"""
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
        parent = widget.parent()
        if hasattr(parent, 'handler') and hasattr(parent.handler, 'type_registry') and hasattr(parent.handler.type_registry, 'json_path'):
            return os.path.basename(parent.handler.type_registry.json_path).split(".")[0]
        elif hasattr(widget, 'handler') and hasattr(widget.handler, 'type_registry') and hasattr(widget.handler.type_registry, 'json_path'):
            return os.path.basename(widget.handler.type_registry.json_path).split(".")[0]
        return None

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
                userdata_info = {
                    "userdata_hash": rui.hash,
                    "userdata_string": None
                }
                
                if hasattr(viewer.scn, '_rsz_userdata_str_map') and rui in viewer.scn._rsz_userdata_str_map:
                    userdata_info["userdata_string"] = viewer.scn._rsz_userdata_str_map[rui]
                
                return userdata_info
        
        return None
    