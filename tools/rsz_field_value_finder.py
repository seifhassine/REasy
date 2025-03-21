"""
RSZ Field Value Finder Tool

This tool scans multiple RSZ files to find all possible values for a specific field
of instances with a given type ID.
"""

import os
import sys
import argparse
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

from utils.type_registry import TypeRegistry

def scan_file(filepath, type_id, field_identifier, type_registry):
    if not os.path.isfile(filepath):
        return []
        
    if not os.path.getsize(filepath):
        return []

    try:
        with open(filepath, 'rb') as f:
            data = f.read()
    except Exception:
        return []

    valid_signatures = [b"SCN\x00", b"USR\x00", b"PFB\x00"]
    if len(data) < 4 or data[:4] not in valid_signatures:
        return []

    from file_handlers.rsz.rsz_file import ScnFile
    scn_file = ScnFile()
    scn_file.type_registry = type_registry
    scn_file.filepath = str(filepath)

    try:
        scn_file.read(data)
    except Exception:
        return []
    
    field_name = field_identifier
    if isinstance(field_identifier, int):
        type_info = type_registry.get_type_info(type_id)
        if type_info and "fields" in type_info:
            if 0 <= field_identifier < len(type_info["fields"]):
                field_name = type_info["fields"][field_identifier]["name"]
            else:
                return []
                
    if not field_name:
        return []
    
    found_values = []
    for idx, instance in enumerate(scn_file.instance_infos):
        if instance.type_id == type_id:
            if idx in scn_file.parsed_elements:
                fields = scn_file.parsed_elements[idx]
                if field_name in fields:
                    value = fields[field_name]
                    found_values.append((filepath, idx, value))
    
    return found_values

def scan_directory(directory, type_id, field_identifier, type_registry, recursive=True):
    if not os.path.isdir(directory):
        return []
        
    results = []
    path = Path(directory)
    candidate_files = []
    total_files_found = 0
    matches_found = 0
    
    try:
        file_iter = path.rglob('*') if recursive else path.glob('*')
        for filepath in file_iter:
            if filepath.is_file():
                filename = filepath.name.lower()
                is_match = any(filename.endswith(ext) or ('.' + filename.split('.')[-2]) == ext 
                              for ext in ['.scn', '.pfb', '.usr'])
                if is_match:
                    candidate_files.append(filepath)
    except Exception:
        return []
    
    total_files = len(candidate_files)
    processed = 0
    
    try:
        for filepath in candidate_files:
            processed += 1
            progress = (processed / total_files) * 100 if total_files else 0
            print(f"\rProgress: {progress:.1f}% ({processed}/{total_files}) - Found {matches_found} matches", end="")
            
            try:
                file_results = scan_file(filepath, type_id, field_identifier, type_registry)
                if file_results:
                    matches_found += len(file_results)
                    results.extend(file_results)
            except Exception:
                continue
    except Exception:
        pass
    
    print("")
    
    return results

def format_value(value):
    """Format a field value for display"""
    if hasattr(value, 'value'):
        return str(value.value)
    elif hasattr(value, 'values'):
        if not value.values:
            return "[]"
        if len(value.values) <= 3:
            elements = [format_value(v) for v in value.values]
            return f"[{', '.join(elements)}]"
        else:
            return f"Array[{len(value.values)} items]"
    elif hasattr(value, 'raw_bytes'):
        return '0x' + ''.join(f'{b:02X}' for b in value.raw_bytes)
    elif hasattr(value, 'x') and hasattr(value, 'y'):
        if hasattr(value, 'z'):
            if hasattr(value, 'w'):
                return f"({value.x}, {value.y}, {value.z}, {value.w})"
            return f"({value.x}, {value.y}, {value.z})"
        return f"({value.x}, {value.y})"
    elif hasattr(value, 'r') and hasattr(value, 'g') and hasattr(value, 'b'):
        return f"RGB({value.r}, {value.g}, {value.b})"
    elif hasattr(value, 'guid_str'):
        return value.guid_str
    return str(value)

def main():
    parser = argparse.ArgumentParser(description='Find all possible values of a specific field in RSZ files')
    parser.add_argument('--dir', '-d', required=True, help='Directory to scan')
    parser.add_argument('--type-id', '-t', required=True, help='Type ID to search for (hex or decimal)')
    parser.add_argument('--field', '-f', required=True, help='Field name or index to extract')
    parser.add_argument('--json-dir', '-j', help='Path to JSON type data (file or directory)', default='res/type_data')
    parser.add_argument('--recursive', '-r', action='store_true', help='Scan directories recursively')
    parser.add_argument('--limit', '-l', type=int, default=10, help='Maximum examples to show for each value')
    parser.add_argument('--output', '-o', help='Output file for results (default: stdout)')
    parser.add_argument('--verbose', '-v', action='store_true', help='Show detailed information')
    
    args = parser.parse_args()
    
    if args.type_id.lower().startswith('0x'):
        type_id = int(args.type_id, 16)
    else:
        try:
            type_id = int(args.type_id)
        except ValueError:
            print(f"Error: Invalid type ID format: {args.type_id}")
            return 1
    
    try:
        field_identifier = int(args.field)
        print(f"Using field index: {field_identifier}")
    except ValueError:
        field_identifier = args.field
        print(f"Using field name: {field_identifier}")
    
    try:
        type_registry = TypeRegistry(args.json_dir)
    except Exception as e:
        print(f"Error loading type data: {str(e)}")
        return 1
    
    type_info = type_registry.get_type_info(type_id)
    if type_info:
        print(f"Searching for field '{field_identifier}' in type: {type_info.get('name', 'Unknown')} (ID: 0x{type_id:08X})")
    else:
        print(f"Warning: Type ID 0x{type_id:08X} not found in registry")
    
    print(f"Scanning directory: {args.dir} {'(recursively)' if args.recursive else ''}")
    results = scan_directory(args.dir, type_id, field_identifier, type_registry, args.recursive)
    
    if not results:
        print("No matching instances found.")
        return 0
    
    print(f"\nFound {len(results)} instances with field '{field_identifier}'")
    
    value_dict = {}
    for filepath, instance_id, value in results:
        formatted_value = format_value(value)
        if formatted_value not in value_dict:
            value_dict[formatted_value] = []
        value_dict[formatted_value].append((filepath, instance_id))
    
    print(f"\nUnique values found: {len(value_dict)}")
    for value, occurrences in sorted(value_dict.items(), key=lambda x: len(x[1]), reverse=True):
        count = len(occurrences)
        percentage = (count / len(results)) * 100
        print(f"\n  Value: {value}")
        print(f"  Occurrences: {count} ({percentage:.1f}%)")
        
        if args.verbose:
            shown = 0
            for filepath, instance_id in occurrences:
                if shown < args.limit:
                    print(f"    - File: {os.path.basename(filepath)}, Instance ID: {instance_id}")
                    shown += 1
            if shown < count:
                print(f"    ... and {count - shown} more instances")
    
    if args.output:
        try:
            with open(args.output, 'w') as f:
                f.write(f"Results for Type ID 0x{type_id:08X}, Field '{field_identifier}'\n")
                f.write(f"Total instances found: {len(results)}\n\n")
                
                for value, occurrences in sorted(value_dict.items(), key=lambda x: len(x[1]), reverse=True):
                    count = len(occurrences)
                    percentage = (count / len(results)) * 100
                    f.write(f"Value: {value}\n")
                    f.write(f"Occurrences: {count} ({percentage:.1f}%)\n")
                    
                    for filepath, instance_id in occurrences:
                        f.write(f"  - File: {filepath}, Instance ID: {instance_id}\n")
                    f.write("\n")
                    
            print(f"\nResults written to {args.output}")
        except Exception as e:
            print(f"Error writing to output file: {str(e)}")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
