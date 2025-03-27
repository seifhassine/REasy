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
        try:
            parent = widget.parent()
            json_path = parent.handler.type_registry.json_path
        except Exception as e:
            parent = widget
            json_path = parent.handler.type_registry.json_path
        finally:
            if json_path:
                json_name = os.path.basename(json_path)
                return json_name.split(".")[0]
            else:
                print("No JSON path found for the widget.")
                return None
        return "default"
    
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
    def is_null_guid(guid_bytes, guid_str=None):
        NULL_GUID = bytes(16)
        NULL_GUID_STR = "00000000-0000-0000-0000-000000000000"
        
        if guid_bytes == NULL_GUID:
            return True
        if guid_str and guid_str == NULL_GUID_STR:
            return True
        return False
