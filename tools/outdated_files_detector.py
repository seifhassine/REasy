import os
import traceback
from file_handlers.rsz.rsz_file import RszFile
from utils.registry_manager import RegistryManager

class OutdatedFilesDetector:
    def __init__(self, type_registry_path=None):
        self.type_registry_path = type_registry_path
        self.type_registry = None
        self._load_type_registry()
    
    def _load_type_registry(self):
        if self.type_registry_path and os.path.exists(self.type_registry_path):
            try:
                self.type_registry = RegistryManager.instance().get_registry(self.type_registry_path)
            except Exception as e:
                print(f"Error loading type registry: {str(e)}")
                traceback.print_exc()
                self.type_registry = None
    
    def set_type_registry_path(self, path):
        self.type_registry_path = path
        self._load_type_registry()
    
    def _is_rsz_file(self, filename):
        lower_filename = filename.lower()
        return ('.pfb' in lower_filename or '.scn' in lower_filename or '.user' in lower_filename)
    
    def scan_directory(self, directory_path):
        if not self.type_registry:
            return [("ERROR", ["Type registry not loaded"])]
        
        results = []
        
        for root, _, files in os.walk(directory_path):
            for file in files:
                if self._is_rsz_file(file):
                    file_path = os.path.join(root, file)
                    try:
                        mismatched_types = self.check_file_for_outdated_types(file_path)
                        if mismatched_types:
                            results.append((file_path, mismatched_types))
                    except Exception as e:
                        print(f"Error checking file {file_path}: {str(e)}")
        
        return results
    
    def check_file_for_outdated_types(self, file_path):
        mismatched_types = []
        
        try:
            with open(file_path, 'rb') as f:
                data = f.read()
            
            rsz_file = RszFile()
            rsz_file.type_registry = self.type_registry
            rsz_file.filepath = file_path
            try:
                rsz_file.read(data)
            except Exception as e:
                print(f"Error reading RSZ file {file_path}: {e}")
                
            for i, instance_info in enumerate(rsz_file.instance_infos):
                if i == 0:
                    continue
                    
                if instance_info.type_id > 0:
                    type_info = self.type_registry.get_type_info(instance_info.type_id)
                    
                    if type_info:
                        crc_value = type_info.get("crc", 0)
                        registry_crc = 0

                        try:
                            registry_crc = int(crc_value, 16)
                                
                            file_crc = instance_info.crc & 0xFFFFFFFF
                            registry_crc = registry_crc & 0xFFFFFFFF
                            
                            type_name = type_info.get("name", f"TypeID_0x{instance_info.type_id:08X}")
                            if file_crc != registry_crc:
                                mismatched_types.append({
                                    "name": type_name,
                                    "id": instance_info.type_id,
                                    "file_crc": file_crc,
                                    "registry_crc": registry_crc
                                })
                        except (ValueError, TypeError):
                            pass
                    else:
                        type_name = f"TypeID_0x{instance_info.type_id:08X}"
                        mismatched_types.append({
                            "name": type_name,
                            "id": instance_info.type_id,
                            "file_crc": instance_info.crc,
                            "registry_crc": None
                        })
    
        except Exception as e:
            print(f"Error processing file {file_path}: {str(e)}")
            traceback.print_exc()
        
        return mismatched_types

def delete_files(file_list):
    success = []
    errors = []
    
    for file_path in file_list:
        try:
            os.remove(file_path)
            success.append(file_path)
        except Exception as e:
            errors.append((file_path, str(e)))
    
    return success, errors