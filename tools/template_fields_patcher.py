import json
import sys
import argparse
from typing import Dict, Any

# python template_fields_patcher.py --source "D:\RE Modding\REasy\resources\data\dumps\rszre4_reasy.json" --patch "D:\RE Modding\REasy\resources\patches\rszre4_patch.json"

class TemplatePatcher:
    def __init__(self, source_path: str, patch_path: str, crc_mode: bool = False):
        """
        Initialize the template patcher with source and patch file paths.
        
        Args:
            source_path: Path to the source JSON template to be updated
            patch_path: Path to the patch JSON template containing updates
            crc_mode: If True, only patch when CRC values also match
        """
        self.source_path = source_path
        self.patch_path = patch_path
        self.crc_mode = crc_mode
        self.source_data = {}
        self.patch_data = {}
        self.updated_types = 0
        self.skipped_types = 0
        self.warnings = []
        self.crc_mismatches = 0
        
    def load_templates(self) -> bool:
        """Load both source and patch templates."""
        try:
            with open(self.source_path, 'r') as f:
                self.source_data = json.load(f)
            
            with open(self.patch_path, 'r') as f:
                self.patch_data = json.load(f)
                
            return True
        except Exception as e:
            print(f"Error loading templates: {e}")
            return False
    
    def patch_templates(self) -> bool:
        """Patch the source template with data from the patch template."""
        if not self.source_data or not self.patch_data:
            print("Templates not loaded. Call load_templates() first.")
            return False
        
        for type_id, patch_type in self.patch_data.items():
            if type_id not in self.source_data:
                continue
                
            source_type = self.source_data[type_id]
            
            # Check if both have fields
            if "fields" not in patch_type or "fields" not in source_type:
                continue
            
            # In CRC mode, check if CRC values match
            if self.crc_mode:
                patch_crc = patch_type.get("crc")
                source_crc = source_type.get("crc")
                
                if patch_crc and source_crc and patch_crc != source_crc:
                    warning = f"Type {type_id} ({source_type.get('name', 'unnamed')}): " \
                              f"CRC mismatch - source: {source_crc}, patch: {patch_crc}"
                    self.warnings.append(warning)
                    self.crc_mismatches += 1
                    continue
                
                if patch_crc and not source_crc:
                    warning = f"Type {type_id} ({source_type.get('name', 'unnamed')}): " \
                              f"Source missing CRC, patch has: {patch_crc}"
                    self.warnings.append(warning)
                    self.crc_mismatches += 1
                    continue
                
                if not patch_crc and source_crc:
                    warning = f"Type {type_id} ({source_type.get('name', 'unnamed')}): " \
                              f"Patch missing CRC, source has: {source_crc}"
                    self.warnings.append(warning)
                    self.crc_mismatches += 1
                    continue
                
            patch_fields = patch_type["fields"]
            source_fields = source_type["fields"]
            
            # Check if they have the same number of fields
            if len(patch_fields) != len(source_fields):
                warning = f"Type {type_id} ({source_type.get('name', 'unnamed')}): " \
                          f"Field count mismatch - source: {len(source_fields)}, patch: {len(patch_fields)}"
                self.warnings.append(warning)
                
                self.skipped_types += 1
                continue
            
            # Update fields from patch
            field_count = min(len(source_fields), len(patch_fields))
            for i in range(field_count):
                for attr, value in patch_fields[i].items():
                    source_fields[i][attr] = value
            
            self.updated_types += 1
        
        return True
    
    def save_patched_template(self, output_path: str = None) -> bool:
        """Save the patched template to the specified path or overwrite source."""
        if not output_path:
            output_path = self.source_path
            
        try:
            with open(output_path, 'w') as f:
                json.dump(self.source_data, f, indent="\t")
            return True
        except Exception as e:
            print(f"Error saving patched template: {e}")
            return False
    
    def get_summary(self) -> Dict[str, Any]:
        """Get a summary of the patching operation."""
        summary = {
            "updated_types": self.updated_types,
            "skipped_types": self.skipped_types,
            "warnings": self.warnings
        }
        
        if self.crc_mode:
            summary["crc_mismatches"] = self.crc_mismatches
            
        return summary


def main():
    parser = argparse.ArgumentParser(description="Patch JSON templates by updating field attributes")
    parser.add_argument("--source", help="Path to the source template JSON file")
    parser.add_argument("--patch", help="Path to the patch template JSON file")
    parser.add_argument( "--output", help="Output path for patched template (default: overwrite source)")
    parser.add_argument("--verbose", action="store_true", help="Show detailed warnings")
    parser.add_argument("--crc", action="store_true", help="Enable CRC mode - only patch when CRC values match")
   
    args = parser.parse_args()
    
    patcher = TemplatePatcher(args.source, args.patch, args.crc)
    
    print("Loading templates...")
    if not patcher.load_templates():
        sys.exit(1)
        
    print("Patching templates...")
    if not patcher.patch_templates():
        sys.exit(1)
    
    output_path = args.output or args.source
    print(f"Saving patched template to {output_path}...")
    if not patcher.save_patched_template(output_path):
        sys.exit(1)
        
    summary = patcher.get_summary()
    print("\nPatching complete!")
    print(f"- Updated types: {summary['updated_types']}")
    print(f"- Skipped types: {summary['skipped_types']}")
    '''
    if patcher.crc_mode:
        print(f"- CRC mismatches: {summary['crc_mismatches']}")
    
    if args.verbose and summary['warnings']:
        print("\nWarnings:")
        for warning in summary['warnings']:
            print(f"- {warning}")
    
    elif summary['warnings']:
        print(f"\nSkipped {len(summary['warnings'])} types due to mismatches.")
        print("Use --verbose to see detailed warnings.")
    '''
    
if __name__ == "__main__":
    main()
