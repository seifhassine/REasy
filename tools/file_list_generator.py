import os
import json
import subprocess
import sys
import io
from pathlib import Path
from collections import defaultdict


class ExtensionAnalyzer:
    def __init__(self, target_extensions=None):
        self.target_extensions = target_extensions or ["scn", "fbxskel", "uvar", "mesh"]
        self.dumped_extensions = {}
        self.list_extensions = defaultdict(set)
        self.combined_extensions = defaultdict(set)
    
    def run_extension_dumper(self, exe_path):
        try:
            script_dir = Path(__file__).parent.parent
            dumper_path = script_dir / "reversing" / "extension_info" / "extension_dumper.py"
            
            if not dumper_path.exists():
                return False, f"Extension dumper not found at: {dumper_path}"
            
            cmd = [sys.executable, str(dumper_path), exe_path]
            for ext in self.target_extensions:
                cmd.extend(["-ext", ext])
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            
            if result.returncode != 0:
                return False, f"Extension dumper failed: {result.stderr}"
            
            try:
                output = json.loads(result.stdout)
                if "error" in output:
                    return False, f"Dumper error: {output['error']}"
                self.dumped_extensions = output
                return True, None
            except json.JSONDecodeError as e:
                return False, f"Failed to parse dumper output: {e}\nOutput: {result.stdout}"
                
        except subprocess.TimeoutExpired:
            return False, "Extension dumper timed out after 60 seconds"
        except Exception as e:
            return False, f"Unexpected error: {e}"
    
    def parse_list_file(self, list_file_path):
        if not os.path.exists(list_file_path):
            return False, f"List file not found: {list_file_path}"
        
        self.list_extensions.clear()
        
        try:
            with open(list_file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or not line.startswith('natives/'):
                        continue
                    
                    last_slash_idx = line.rfind('/')
                    if last_slash_idx == -1:
                        continue
                    
                    filename = line[last_slash_idx + 1:]
                    parts = filename.split('.')
                    if len(parts) < 3:
                        continue
                    
                    version_start_idx = None
                    for i in range(len(parts) - 1, 0, -1):
                        try:
                            int(parts[i])
                            version_start_idx = i
                        except ValueError:
                            if version_start_idx is not None:
                                break
                    
                    if version_start_idx is None or version_start_idx <= 1:
                        continue
                    
                    extension = parts[version_start_idx - 1].lower()
                    version_str = '.'.join(parts[version_start_idx:])
                    
                    if extension:
                        self.list_extensions[extension].add(version_str.lower())
            
            return True, None
        except Exception as e:
            return False, f"Failed to parse list file: {e}"
    
    def combine_extensions(self):
        self.combined_extensions.clear()
        for ext, version in self.dumped_extensions.items():
            self.combined_extensions[ext.lower()].add(str(version))
        for ext, versions in self.list_extensions.items():
            self.combined_extensions[ext].update(versions)
    
    def get_extension_source(self, extension):
        ext_lower = extension.lower()
        dumped_keys_lower = {k.lower() for k in self.dumped_extensions.keys()}
        in_dumper = ext_lower in dumped_keys_lower
        in_list = ext_lower in self.list_extensions
        
        if in_dumper and not in_list:
            return "Dumper"
        elif in_list and not in_dumper:
            return "List"
        elif not in_dumper and not in_list:
            return "Unknown"
        
        dumper_version = None
        for key in self.dumped_extensions.keys():
            if key.lower() == ext_lower:
                dumper_version = self.dumped_extensions[key]
                break
        
        if dumper_version is None:
            return "List"
        
        dumper_versions = {str(dumper_version)}
        list_versions = self.list_extensions.get(ext_lower, set())
        if not list_versions:
            return "Dumper"
        
        new_versions = list_versions - dumper_versions
        return "Dumper + List" if new_versions else "Dumper"
    
    def get_sorted_extensions(self):
        return sorted(self.combined_extensions.items())
    
    def get_statistics(self):
        dumped_keys_lower = {k.lower() for k in self.dumped_extensions.keys()}
        total = len(self.combined_extensions)
        dumper_only = sum(1 for ext in self.combined_extensions if ext in dumped_keys_lower and ext not in self.list_extensions)
        list_only = sum(1 for ext in self.combined_extensions if ext not in dumped_keys_lower and ext in self.list_extensions)
        both = sum(1 for ext in self.combined_extensions if ext in dumped_keys_lower and ext in self.list_extensions)
        return {'total': total, 'dumper_only': dumper_only, 'list_only': list_only, 'both': both}
    
    def get_list_only_extensions(self):
        dumped_keys_lower = {k.lower() for k in self.dumped_extensions.keys()}
        return sorted([ext for ext in self.list_extensions.keys() if ext not in dumped_keys_lower])
    
    def update_extension_versions(self, extension, versions):
        self.combined_extensions[extension.lower()] = versions
    
    def clear(self):
        self.dumped_extensions.clear()
        self.list_extensions.clear()
        self.combined_extensions.clear()


def validate_game_executable(exe_path):
    if not exe_path:
        return False, "No executable path provided"
    if not os.path.exists(exe_path):
        return False, f"File does not exist: {exe_path}"
    if not os.path.isfile(exe_path):
        return False, f"Path is not a file: {exe_path}"
    if not exe_path.lower().endswith('.exe'):
        return False, "File must be a .exe executable"
    return True, None


def validate_list_file(list_path):
    if not list_path:
        return False, "No list file path provided"
    if not os.path.exists(list_path):
        return False, f"File does not exist: {list_path}"
    if not os.path.isfile(list_path):
        return False, f"Path is not a file: {list_path}"
    return True, None


class PathCollector:
    def __init__(self, extensions, extension_versions=None, path_prefix="natives/stm/"):
        self.extensions = [ext.lower() for ext in extensions]
        self.extension_versions = {}
        if extension_versions:
            for ext, versions in extension_versions.items():
                self.extension_versions[ext.lower()] = {v.lower() for v in versions}
        self.path_prefix = path_prefix.rstrip('/') + '/'
        self.collected_paths = set()
    
    def filter_path_by_extensions(self, path):
        path_lower = path.lower()
        for ext in self.extensions:
            if path_lower.endswith(f'.{ext}'):
                return True
        return False
    
    def should_skip_entry(self, entry):
        return entry.decompressed_size > 50 * 1024 * 1024 or entry.decompressed_size < 100
    
    def extract_strings_from_data(self, data):
        strings = []
        min_length = 10
        i = 0
        data_len = len(data)
        min_bytes = min_length * 2
        
        while i < data_len - min_bytes:
            if data[i] != 0 and 32 <= data[i] <= 126 and data[i+1] == 0:
                string_bytes = bytearray()
                j = i
                while j < data_len - 1:
                    byte1, byte2 = data[j], data[j+1]
                    if byte1 != 0 and 32 <= byte1 <= 126 and byte2 == 0:
                        string_bytes.append(byte1)
                        j += 2
                    else:
                        break
                
                if len(string_bytes) >= min_length:
                    try:
                        strings.append(string_bytes.decode('ascii'))
                    except:
                        pass
                i = j
            else:
                i += 2
        
        return strings
    
    def _process_entry(self, entry, f, entry_idx):
        from file_handlers.pak.pakfile import _read_entry_raw
        
        if self.should_skip_entry(entry):
            return 0, 0
        
        try:
            buf = io.BytesIO()
            _read_entry_raw(entry, f, buf)
            data = buf.getvalue()
            
            extracted_strings = self.extract_strings_from_data(data)
            strings_extracted = len(extracted_strings)
            strings_matched = 0
            
            for string in extracted_strings:
                if self.filter_path_by_extensions(string):
                    strings_matched += 1
                    path_normalized = string.replace('\\', '/')
                    if path_normalized.startswith('@'):
                        path_normalized = path_normalized[1:]
                    if not path_normalized.lower().startswith('natives/'):
                        path_normalized = self.path_prefix + path_normalized
                    
                    parts = path_normalized.split('.')
                    if len(parts) >= 2:
                        extension = parts[-1].lower()
                        versions = self.extension_versions.get(extension, set())
                        if versions:
                            for version in versions:
                                self.collected_paths.add(f"{path_normalized}.{version}".lower())
                        else:
                            self.collected_paths.add(path_normalized.lower())
            
            return strings_extracted, strings_matched
        except:
            return 0, 0
    
    def collect_from_pak_files(self, pak_directory, progress_callback=None):
        try:
            from file_handlers.pak import scan_pak_files, PakFile
            
            if not os.path.exists(pak_directory):
                return False, f"Directory not found: {pak_directory}", 0
            if not os.path.isdir(pak_directory):
                return False, f"Not a directory: {pak_directory}", 0
            
            if progress_callback:
                should_stop = progress_callback("Scanning for PAK files...", 0, 1)
                if should_stop:
                    return True, None, len(self.collected_paths)
            
            pak_files = scan_pak_files(pak_directory, ignore_mod_paks=False)
            if not pak_files:
                return False, "No .pak files found in directory", 0
            
            total_strings_extracted = 0
            total_strings_matched = 0
            stopped = False
            
            for pak_idx, pak_path in enumerate(pak_files, 1):
                if stopped:
                    break
                
                pak_name = os.path.basename(pak_path)
                
                try:
                    pak = PakFile()
                    with open(pak_path, 'rb') as f:
                        pak.read_contents(f, None)
                    
                    total_entries = len(pak.entries)
                    if total_entries == 0:
                        continue
                    
                    print(f"\nPAK {pak_idx}/{len(pak_files)}: {pak_name} ({total_entries} entries)")
                    pak_strings_extracted = 0
                    pak_strings_matched = 0
                    
                    with open(pak_path, 'rb') as f:
                        for entry_idx, entry in enumerate(pak.entries, 1):
                            if entry_idx % 100 == 0 or entry_idx == total_entries:
                                if progress_callback:
                                    should_stop = progress_callback(
                                        f"PAK {pak_idx}/{len(pak_files)}: {pak_name}\nProcessing entry {entry_idx}/{total_entries}\nPaths found: {len(self.collected_paths)}",
                                        entry_idx, total_entries
                                    )
                                    if should_stop:
                                        stopped = True
                                        break
                            
                            extracted, matched = self._process_entry(entry, f, entry_idx)
                            pak_strings_extracted += extracted
                            pak_strings_matched += matched
                    
                    total_strings_extracted += pak_strings_extracted
                    total_strings_matched += pak_strings_matched
                    print(f"PAK {pak_name}: Extracted {pak_strings_extracted} strings, matched {pak_strings_matched} with extensions")
                except Exception as e:
                    print(f"Warning: Failed to scan {pak_name}: {e}")
                    continue
            
            if progress_callback:
                status = "Collection stopped" if stopped else "Collection complete"
                progress_callback(status, len(pak_files), len(pak_files))
            
            print(f"\n=== Collection Summary ===")
            print(f"Total strings extracted: {total_strings_extracted}")
            print(f"Strings matching extensions: {total_strings_matched}")
            print(f"Unique paths collected: {len(self.collected_paths)}")
            
            return True, None, len(self.collected_paths)
        except Exception as e:
            return False, f"Unexpected error: {e}", 0
    
    def add_from_list_file(self, list_file_path):
        if not os.path.exists(list_file_path):
            return False, f"List file not found: {list_file_path}"
        
        try:
            with open(list_file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or not line.startswith('natives/'):
                        continue
                    
                    parts = line.split('.')
                    if len(parts) < 2:
                        continue
                    
                    path_without_version = None
                    extension = None
                    
                    for i in range(len(parts) - 1, 0, -1):
                        try:
                            int(parts[i])
                            path_without_version = '.'.join(parts[:i])
                            if i > 0:
                                extension = parts[i - 1].lower()
                            break
                        except ValueError:
                            if i > 1:
                                try:
                                    int(parts[i-1])
                                    path_without_version = '.'.join(parts[:i-1])
                                    if i > 1:
                                        extension = parts[i - 2].lower()
                                    break
                                except ValueError:
                                    pass
                    
                    if path_without_version:
                        versions = self.extension_versions.get(extension, set())
                        if versions:
                            for version in versions:
                                self.collected_paths.add(f"{path_without_version}.{version}".lower())
                        else:
                            self.collected_paths.add(path_without_version.lower())
            
            return True, None
        except Exception as e:
            return False, f"Failed to process list file: {e}"
    
    def validate_paths_against_paks(self, pak_directory, progress_callback=None):
        try:
            from file_handlers.pak import scan_pak_files, PakFile
            from file_handlers.pak.utils import filepath_hash
            
            if not os.path.exists(pak_directory):
                return False, f"Directory not found: {pak_directory}", set()
            
            if progress_callback:
                progress_callback("Scanning PAK files for hashes...", 0, 1)
            
            pak_files = scan_pak_files(pak_directory, ignore_mod_paks=False)
            if not pak_files:
                return False, "No .pak files found in directory", set()
            
            pak_hashes = set()
            
            for pak_idx, pak_path in enumerate(pak_files, 1):
                pak_name = os.path.basename(pak_path)
                if progress_callback:
                    progress_callback(f"Scanning PAK {pak_idx}/{len(pak_files)}: {pak_name}\nCollecting file hashes...", pak_idx, len(pak_files))
                
                try:
                    pak = PakFile()
                    with open(pak_path, 'rb') as f:
                        pak.read_contents(f, None)
                    for entry in pak.entries:
                        pak_hashes.add(entry.combined_hash)
                except Exception as e:
                    print(f"Warning: Failed to read PAK {pak_name}: {e}")
                    continue
            
            if not pak_hashes:
                return False, "No hashes found in PAK files", set()
            
            if progress_callback:
                progress_callback("Validating collected paths...", 0, len(self.collected_paths))
            
            validated_paths = set()
            total_paths = len(self.collected_paths)
            
            for idx, path in enumerate(self.collected_paths, 1):
                if progress_callback and idx % 1000 == 0:
                    progress_callback(f"Validating paths...\nChecked: {idx}/{total_paths}\nValid: {len(validated_paths)}", idx, total_paths)
                
                path_hash = filepath_hash(path)
                if path_hash in pak_hashes:
                    validated_paths.add(path.lower())
            
            if progress_callback:
                progress_callback(f"Validation complete!\nValid paths: {len(validated_paths)}/{total_paths}", total_paths, total_paths)
            
            return True, None, validated_paths
        except Exception as e:
            return False, f"Validation error: {e}", set()
    
    def export_to_file(self, output_path, paths=None):
        try:
            paths_to_export = paths if paths is not None else self.collected_paths
            sorted_paths = sorted(p.lower() for p in paths_to_export)
            with open(output_path, 'w', encoding='utf-8') as f:
                for path in sorted_paths:
                    f.write(path + '\n')
            return True, None
        except Exception as e:
            return False, f"Failed to write output file: {e}"
    
    def get_path_count(self):
        return len(self.collected_paths)
    
    def clear(self):
        self.collected_paths.clear()
