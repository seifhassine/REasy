import os
from pathlib import Path
from typing import Optional, Tuple, List


def find_resource_in_paks(resource_path: str, pak_cached_reader, pak_selected_paks) -> Optional[Tuple[str, bytes]]:
    if not pak_cached_reader or not pak_selected_paks:
        return None
    
    try:
        from file_handlers.pak.reader import PakReader
        from file_handlers.pak.utils import guess_extension_from_header
        
        found_entries = []
        all_cached = pak_cached_reader.cached_paths(include_unknown=False)
        
        resource_lower = resource_path.lower()
        for cached_path in all_cached:
            if resource_lower in cached_path.lower():
                found_entries.append(cached_path)
        
        if found_entries:
            r = PakReader()
            r.pak_file_priority = list(pak_selected_paks)
            r.add_files(found_entries[0])
            
            found = None
            for pth, stream in r.find_files():
                if pth.lower() == found_entries[0].lower():
                    found = (pth, stream)
                    break
            
            if found:
                pth, stream = found
                data = stream.read()
                
                try:
                    ext = guess_extension_from_header(data[:64])
                except Exception:
                    ext = None
                    
                name = pth if ('.' in os.path.basename(pth)) else (pth + ('.' + ext.lower() if ext else ''))
                return (name, data)
                
    except Exception as e:
        print(f"PAK search error: {e}")
    
    return None


def find_resource_in_filesystem(resource_path: str, unpacked_dir: str, path_prefix: str) -> Optional[Tuple[str, bytes]]:
    if not unpacked_dir or not os.path.isdir(unpacked_dir):
        return None
    
    try:
        normalized_resource = resource_path.replace("\\", "/")
        
        if normalized_resource.lower().startswith(path_prefix.lower()):
            full_path = normalized_resource
        else:
            full_path = f"{path_prefix}/{normalized_resource}"
        
        base_file_path = os.path.join(unpacked_dir, full_path.replace("/", os.sep))
        
        dir_path = os.path.dirname(base_file_path)
        base_name = os.path.basename(base_file_path)
        
        if not os.path.isdir(dir_path):
            return None
        
        matching_files = []
        for file in os.listdir(dir_path):
            if file == base_name or file.startswith(base_name + "."):
                matching_files.append(os.path.join(dir_path, file))
        
        if not matching_files:
            return None
        
        target_file = matching_files[0]
        with open(target_file, "rb") as f:
            data = f.read()
        
        return (target_file, data)
        
    except Exception as e:
        print(f"Filesystem search error: {e}")
    
    return None


def get_path_prefix_for_game(game: str) -> str:
    try:
        from ui.project_manager.constants import EXPECTED_NATIVE
        
        if game and game in EXPECTED_NATIVE:
            native_parts = EXPECTED_NATIVE[game]
            return "/".join(native_parts)
    except Exception:
        pass
    
    return "natives/stm"


def copy_resource_to_project(resource_path: str, project_dir: str, unpacked_dir: str, 
                             path_prefix: str, pak_cached_reader=None, pak_selected_paks=None) -> Optional[str]:
    import shutil
    
    file_data = None
    source_path = None
    
    pak_result = find_resource_in_paks(resource_path, pak_cached_reader, pak_selected_paks)
    if pak_result:
        source_path, file_data = pak_result
    
    if not file_data:
        fs_result = find_resource_in_filesystem(resource_path, unpacked_dir, path_prefix)
        if fs_result:
            source_path, file_data = fs_result
    
    if not file_data:
        return None
    
    normalized_resource = resource_path.replace("\\", "/")
    
    if normalized_resource.lower().startswith(path_prefix.lower()):
        relative_path = normalized_resource[len(path_prefix):].lstrip("/")
    else:
        relative_path = normalized_resource
    
    dest_path = os.path.join(project_dir, path_prefix.replace("/", os.sep), 
                            relative_path.replace("/", os.sep))
    
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    
    with open(dest_path, "wb") as f:
        f.write(file_data)
    
    return dest_path
