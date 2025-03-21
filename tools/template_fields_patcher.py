import json
import os
import sys
import argparse
from typing import Dict, List, Any, Tuple

class TemplatePatcher:
    def __init__(self, source_path: str, patch_path: str):
        """
        Initialize the template patcher with source and patch file paths.
        
        Args:
            source_path: Path to the source JSON template to be updated
            patch_path: Path to the patch JSON template containing updates
        """
        self.source_path = source_path
        self.patch_path = patch_path
        self.source_data = {}
        self.patch_data = {}
        self.updated_types = 0
        self.skipped_types = 0
        self.warnings = []
        
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
            for i in range(len(source_fields)):
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
        return {
            "updated_types": self.updated_types,
            "skipped_types": self.skipped_types,
            "warnings": self.warnings
        }


def main():
    parser = argparse.ArgumentParser(description="Patch JSON templates by updating field attributes")
    parser.add_argument("--source", help="Path to the source template JSON file")
    parser.add_argument("--patch", help="Path to the patch template JSON file")
    parser.add_argument( "--output", help="Output path for patched template (default: overwrite source)")
    parser.add_argument("--verbose", action="store_true", help="Show detailed warnings")
    
    args = parser.parse_args()
    
    patcher = TemplatePatcher(args.source, args.patch)
    
    print(f"Loading templates...")
    if not patcher.load_templates():
        sys.exit(1)
        
    print(f"Patching templates...")
    if not patcher.patch_templates():
        sys.exit(1)
    
    output_path = args.output or args.source
    print(f"Saving patched template to {output_path}...")
    if not patcher.save_patched_template(output_path):
        sys.exit(1)
        
    summary = patcher.get_summary()
    print(f"\nPatching complete!")
    print(f"- Updated types: {summary['updated_types']}")
    print(f"- Skipped types: {summary['skipped_types']}")
    
    if args.verbose and summary['warnings']:
        print("\nWarnings:")
        for warning in summary['warnings']:
            print(f"- {warning}")
    elif summary['warnings']:
        print(f"\nSkipped {len(summary['warnings'])} types due to field count mismatches.")
        print("Use --verbose to see detailed warnings.")
    
    
if __name__ == "__main__":
    main()
