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
    
    found_values = []
    
    type_info = type_registry.get_type_info(type_id)
    
    for idx, instance in enumerate(scn_file.instance_infos):
        if instance.type_id == type_id:
            if idx in scn_file.parsed_elements:
                fields = scn_file.parsed_elements[idx]
                
                if field_identifier is None:
                    if type_info and "fields" in type_info:
                        for field in type_info["fields"]:
                            field_name = field["name"]
                            if field_name in fields:
                                value = fields[field_name]
                                found_values.append((filepath, idx, field_name, value))
                else:
                    field_name = field_identifier
                    if isinstance(field_identifier, int):
                        if type_info and "fields" in type_info:
                            if 0 <= field_identifier < len(type_info["fields"]):
                                field_name = type_info["fields"][field_identifier]["name"]
                            else:
                                return []
                                
                    if not field_name:
                        return []
                        
                    if field_name in fields:
                        value = fields[field_name]
                        found_values.append((filepath, idx, field_name, value))
    
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
                              for ext in ['.scn', '.pfb', '.user'])
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
        if hasattr(value, 'a'):
            return f"RGBA({value.r}, {value.g}, {value.b}, {value.a})"
        return f"RGB({value.r}, {value.g}, {value.b})"
    elif hasattr(value, 'guid_str'):
        return value.guid_str
    elif hasattr(value, 'min') and hasattr(value, 'max'):
        if isinstance(value.min, (float, int)) and isinstance(value.max, (float, int)):
            return f"Range({value.min}, {value.max})"
        else:
            min_value = format_value(value.min)
            max_value = format_value(value.max)
            return f"Range({min_value}, {max_value})"
    elif hasattr(value, 'width') and hasattr(value, 'height'):
        return f"Size({value.width}, {value.height})"
    elif hasattr(value, 'min_x') and hasattr(value, 'max_x'):
        return f"Rect({value.min_x}, {value.min_y}, {value.max_x}, {value.max_y})"
    elif hasattr(value, 'center') and hasattr(value, 'radius'):
        center_str = format_value(value.center)
        return f"Sphere({center_str}, {value.radius})"
    elif hasattr(value, 'start') and hasattr(value, 'end'):
        start_str = format_value(value.start)
        end_str = format_value(value.end)
        if hasattr(value, 'radius'): 
            return f"Capsule(start:{start_str}, end:{end_str}, radius:{value.radius})"
        return f"LineSegment(start:{start_str}, end:{end_str})"
    elif hasattr(value, 'position') and hasattr(value, 'direction'):
        pos_str = format_value(value.position)
        dir_str = format_value(value.direction)
        if hasattr(value, 'angle') and hasattr(value, 'distance'):  
            return f"Cone(pos:{pos_str}, dir:{dir_str}, angle:{value.angle}, distance:{value.distance})"
        return f"Direction(pos:{pos_str}, dir:{dir_str})"
    elif hasattr(value, 'center') and hasattr(value, 'radius') and hasattr(value, 'height'): 
        center_str = format_value(value.center)
        return f"Cylinder(center:{center_str}, radius:{value.radius}, height:{value.height})"
    elif hasattr(value, 'type_name'): 
        return f"Type({value.type_name})"
    return str(value)

def main():
    parser = argparse.ArgumentParser(description='Find all possible values of a specific field in RSZ files')
    parser.add_argument('--dir', '-d', required=True, help='Directory to scan')
    parser.add_argument('--type-id', '-t', required=True, help='Type ID to search for (hex or decimal)')
    parser.add_argument('--field', '-f', help='Field name or index to extract (if not specified, will scan all fields)')
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
    
    field_identifier = None
    if args.field:
        try:
            field_identifier = int(args.field)
            print(f"Using field index: {field_identifier}")
        except ValueError:
            field_identifier = args.field
            print(f"Using field name: {field_identifier}")
    else:
        print("No field specified. Will scan all fields.")
    
    try:
        type_registry = TypeRegistry(args.json_dir)
    except Exception as e:
        print(f"Error loading type data: {str(e)}")
        return 1
    
    type_info = type_registry.get_type_info(type_id)
    if type_info:
        field_msg = f"field '{field_identifier}'" if field_identifier is not None else "all fields"
        print(f"Searching for {field_msg} in type: {type_info.get('name', 'Unknown')} (ID: 0x{type_id:08X})")
    else:
        print(f"Warning: Type ID 0x{type_id:08X} not found in registry")
    
    print(f"Scanning directory: {args.dir} {'(recursively)' if args.recursive else ''}")
    results = scan_directory(args.dir, type_id, field_identifier, type_registry, args.recursive)
    
    if not results:
        print("No matching instances found.")
        return 0
    
    field_results = {}
    for filepath, instance_id, field_name, value in results:
        if field_name not in field_results:
            field_results[field_name] = []
        field_results[field_name].append((filepath, instance_id, value))
    
    print(f"\nFound data for {len(field_results)} fields")
    
    if args.output:
        try:
            with open(args.output, 'w') as f:
                f.write(f"Results for Type ID 0x{type_id:08X}\n")
                if type_info:
                    f.write(f"Type Name: {type_info.get('name', 'Unknown')}\n")
                f.write(f"Total instances found: {len(results)}\n\n")
                
                for field_name, field_instances in field_results.items():
                    f.write(f"Field: {field_name}\n")
                    f.write(f"Instances: {len(field_instances)}\n\n")
                    
                    value_dict = {}
                    for filepath, instance_id, value in field_instances:
                        formatted_value = format_value(value)
                        if formatted_value not in value_dict:
                            value_dict[formatted_value] = []
                        value_dict[formatted_value].append((filepath, instance_id))
                    
                    f.write(f"Unique values: {len(value_dict)}\n")
                    for value, occurrences in sorted(value_dict.items(), key=lambda x: len(x[1]), reverse=True):
                        count = len(occurrences)
                        percentage = (count / len(field_instances)) * 100
                        f.write(f"- {value}: {count} ({percentage:.1f}%)\n")
                    
                    if args.verbose:
                        f.write("\nDetailed occurrences:\n")
                        for value, occurrences in sorted(value_dict.items(), key=lambda x: len(x[1]), reverse=True):
                            f.write(f"\nValue: {value}\n")
                            for filepath, instance_id in occurrences[:args.limit]:
                                f.write(f"  - File: {filepath}, Instance ID: {instance_id}\n")
                            if len(occurrences) > args.limit:
                                f.write(f"  - ... and {len(occurrences) - args.limit} more instances\n")
                    
                    f.write("\n" + "-"*50 + "\n\n")
                    
            print(f"\nResults written to {args.output}")
        except Exception as e:
            print(f"Error writing to output file: {str(e)}")
    
    for field_name, field_instances in field_results.items():
        print(f"\nField: {field_name}")
        print(f"Instances: {len(field_instances)}")
        
        value_dict = {}
        for filepath, instance_id, value in field_instances:
            formatted_value = format_value(value)
            if formatted_value not in value_dict:
                value_dict[formatted_value] = []
            value_dict[formatted_value].append((filepath, instance_id))
        
        print(f"Unique values: {len(value_dict)}")
        for value, occurrences in sorted(value_dict.items(), key=lambda x: len(x[1]), reverse=True):
            count = len(occurrences)
            percentage = (count / len(field_instances)) * 100
            print(f"- {value}: {count} ({percentage:.1f}%)")
            
            if args.verbose:
                shown = 0
                for filepath, instance_id in occurrences:
                    if shown < args.limit:
                        print(f"  - File: {os.path.basename(filepath)}, Instance ID: {instance_id}")
                        shown += 1
                if shown < count:
                    print(f"  - ... and {count - shown} more instances")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
