import json
import sys
import argparse
import re
from typing import Dict, Any

# python template_fields_patcher.py --source "D:\RE Modding\REasy\resources\data\dumps\stripped\rszmhwilds_strip.json" --patch "D:\RE Modding\REasy\resources\data\dumps\stripped\rszre9_strip.json" --crc --force-count-mismatch --only-versioned-source-fields


class TemplatePatcher:
    def __init__(
        self,
        source_path: str,
        patch_path: str,
        crc_mode: bool = False,
        force_count_mismatch: bool = False,
        only_versioned_source_fields: bool = False
    ):
        self.source_path = source_path
        self.patch_path = patch_path
        self.crc_mode = crc_mode
        self.force_count_mismatch = force_count_mismatch
        self.only_versioned_source_fields = only_versioned_source_fields

        self.source_data: Dict[str, Any] = {}
        self.patch_data: Dict[str, Any] = {}
        self.updated_types = 0
        self.skipped_types = 0
        self.warnings: list[str] = []
        self.crc_mismatches = 0
        self.deleted_nonexistent_types = 0
        self.forced_count_mismatches = 0
        self.deleted_empty_field_types = 0
        self.skipped_non_versioned_source_types = 0

        self.versioned_field_pattern = re.compile(r"^v\d+$")

    def load_templates(self) -> bool:
        """Load both source and patch templates."""
        try:
            with open(self.source_path, 'r', encoding='utf-8') as f:
                self.source_data = json.load(f)
            with open(self.patch_path, 'r', encoding='utf-8') as f:
                self.patch_data = json.load(f)
            return True
        except Exception as e:
            print(f"Error loading templates: {e}")
            return False

    def _source_has_any_non_versioned_field_name(self, fields: list[dict]) -> bool:
        """
        Return True if any source field has a 'name' that is not exactly v0, v1, v2, ...
        Fields without a string 'name' are treated as non-versioned.
        """
        for field in fields:
            if not isinstance(field, dict):
                return True

            field_name = field.get("name")
            if not isinstance(field_name, str):
                return True

            if not self.versioned_field_pattern.fullmatch(field_name):
                return True

        return False

    def patch_templates(self) -> bool:
        """Patch the source template with data from the patch template."""
        if not self.source_data or not self.patch_data:
            print("Templates not loaded. Call load_templates() first.")
            return False

        for type_id in list(self.patch_data.keys()):
            patch_type = self.patch_data[type_id]

            if type_id not in self.source_data:
                print(f"Delete non existent type {type_id}")
                del self.patch_data[type_id]
                self.deleted_nonexistent_types += 1
                continue

            source_type = self.source_data[type_id]

            if "fields" not in patch_type or "fields" not in source_type:
                continue

            patch_fields = patch_type["fields"]
            source_fields = source_type["fields"]

            if self.only_versioned_source_fields and self._source_has_any_non_versioned_field_name(source_fields):
                warning = (
                    f"Type {type_id} ({source_type.get('name', 'unnamed')}): "
                    f"Skipped because source contains non-versioned field names"
                )
                self.warnings.append(warning)
                self.skipped_types += 1
                self.skipped_non_versioned_source_types += 1
                continue

            # CRC mode: check CRC agreement and remove mismatched entries
            if self.crc_mode:
                patch_crc = patch_type.get("crc")
                source_crc = source_type.get("crc")

                # mismatch: both present but unequal
                if patch_crc and source_crc and patch_crc != source_crc:
                    warning = (
                        f"Type {type_id} ({source_type.get('name','unnamed')}): "
                        f"CRC mismatch - source={source_crc}, patch={patch_crc}"
                    )
                    self.warnings.append(warning)
                    self.crc_mismatches += 1
                    del self.patch_data[type_id]
                    continue

                # patch has CRC but source missing
                if patch_crc and not source_crc:
                    warning = (
                        f"Type {type_id} ({source_type.get('name','unnamed')}): "
                        f"Source missing CRC, patch has={patch_crc}"
                    )
                    self.warnings.append(warning)
                    self.crc_mismatches += 1
                    del self.patch_data[type_id]
                    continue

                # source has CRC but patch missing
                if source_crc and not patch_crc:
                    warning = (
                        f"Type {type_id} ({source_type.get('name','unnamed')}): "
                        f"Patch missing CRC, source has={source_crc}"
                    )
                    self.warnings.append(warning)
                    self.crc_mismatches += 1
                    del self.patch_data[type_id]
                    continue

            if not patch_fields:
                warning = (
                    f"Type {type_id} ({source_type.get('name','unnamed')}): "
                    f"Removed from patch due to empty fields"
                )
                self.warnings.append(warning)
                self.deleted_empty_field_types += 1
                del self.patch_data[type_id]
                continue

            # If field counts differ, skip but do not remove patch entry
            if len(patch_fields) != len(source_fields):
                # If force_count_mismatch is enabled and we're in CRC mode, allow the mismatch
                if self.force_count_mismatch and self.crc_mode:
                    warning = (
                        f"Type {type_id} ({source_type.get('name','unnamed')}): "
                        f"Forcing field count mismatch - source={len(source_fields)}, patch={len(patch_fields)}"
                    )
                    self.warnings.append(warning)
                    self.forced_count_mismatches += 1

                    source_type["fields"] = patch_fields
                    self.updated_types += 1
                    continue
                else:
                    warning = (
                        f"Type {type_id} ({source_type.get('name','unnamed')}): "
                        f"Field count mismatch - source={len(source_fields)}, patch={len(patch_fields)}"
                    )
                    self.warnings.append(warning)
                    self.skipped_types += 1
                    continue

            for idx in range(len(source_fields)):
                for attr, val in patch_fields[idx].items():
                    source_fields[idx][attr] = val

            self.updated_types += 1

        return True

    def save_patched_template(self, output_path: str = None) -> bool:
        """Save the patched source template to the specified path (or overwrite source)."""
        if not output_path:
            output_path = self.source_path
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(self.source_data, f, indent="\t")
            return True
        except Exception as e:
            print(f"Error saving patched template: {e}")
            return False

    def save_pruned_patch(self) -> bool:
        """Overwrite the patch file, removing any CRC-mismatched types."""
        try:
            with open(self.patch_path, 'w', encoding='utf-8') as f:
                json.dump(self.patch_data, f, indent="\t")
            return True
        except Exception as e:
            print(f"Error saving pruned patch file: {e}")
            return False

    def get_summary(self) -> Dict[str, Any]:
        """Get a summary of the patching operation."""
        summary: Dict[str, Any] = {
            "updated_types": self.updated_types,
            "skipped_types": self.skipped_types,
            "warnings": self.warnings,
            "deleted_nonexistent_types": self.deleted_nonexistent_types,
            "deleted_empty_field_types": self.deleted_empty_field_types,
            "skipped_non_versioned_source_types": self.skipped_non_versioned_source_types,
        }
        if self.crc_mode:
            summary["crc_mismatches"] = self.crc_mismatches
        if self.force_count_mismatch:
            summary["forced_count_mismatches"] = self.forced_count_mismatches
        return summary


def main():
    parser = argparse.ArgumentParser(
        description="Patch JSON templates by updating field attributes")
    parser.add_argument("--source", required=True,
                        help="Path to the source template JSON file")
    parser.add_argument("--patch", required=True,
                        help="Path to the patch template JSON file")
    parser.add_argument("--output",
                        help="Output path for patched source template (default: overwrite source)")
    parser.add_argument("--verbose", action="store_true",
                        help="Show detailed warnings")
    parser.add_argument("--crc", action="store_true",
                        help="Enable CRC mode - only patch when CRC values match and prune mismatches")
    parser.add_argument("--force-count-mismatch", action="store_true",
                        help="Force patching even when field counts differ (requires --crc)")
    parser.add_argument("--only-versioned-source-fields", action="store_true",
                        help="Only patch types whose source field names are exclusively v0, v1, v2, ...")

    args = parser.parse_args()

    if args.force_count_mismatch and not args.crc:
        print("Error: --force-count-mismatch requires --crc mode to be enabled")
        sys.exit(1)

    patcher = TemplatePatcher(
        args.source,
        args.patch,
        args.crc,
        args.force_count_mismatch,
        args.only_versioned_source_fields
    )

    print("Loading templates...")
    if not patcher.load_templates():
        sys.exit(1)

    print("Patching templates...")
    if not patcher.patch_templates():
        sys.exit(1)

    output_path = args.output or args.source
    print(f"Saving patched source template to {output_path}...")
    if not patcher.save_patched_template(output_path):
        sys.exit(1)

    # Prune the patch file if any types were deleted (either CRC or non-existent)
    need_prune = False
    if patcher.crc_mode and patcher.crc_mismatches > 0:
        need_prune = True
        print(f"Pruning {patcher.crc_mismatches} CRC-mismatched types from patch file...")
    if patcher.deleted_nonexistent_types > 0:
        need_prune = True
        print(f"Pruning {patcher.deleted_nonexistent_types} types not present in source from patch file...")
    if patcher.deleted_empty_field_types > 0:
        need_prune = True
        print(f"Pruning {patcher.deleted_empty_field_types} types with empty fields from patch file...")

    if need_prune:
        if not patcher.save_pruned_patch():
            sys.exit(1)
        print("Patch file updated.")

    summary = patcher.get_summary()
    print("\nPatching complete!")
    print(f"- Updated types: {summary['updated_types']}")
    print(f"- Skipped types: {summary['skipped_types']}")
    if patcher.crc_mode:
        print(f"- CRC mismatches  : {summary['crc_mismatches']}")
    if patcher.force_count_mismatch and summary.get('forced_count_mismatches', 0) > 0:
        print(f"- Forced count mismatches: {summary['forced_count_mismatches']}")
    if summary['deleted_nonexistent_types']:
        print(f"- Types deleted from patch (not in source): {summary['deleted_nonexistent_types']}")
    if summary['deleted_empty_field_types']:
        print(f"- Types deleted from patch (empty fields): {summary['deleted_empty_field_types']}")
    if summary['skipped_non_versioned_source_types']:
        print(f"- Types skipped due to non-versioned source fields: {summary['skipped_non_versioned_source_types']}")

    if summary['warnings']:
        if args.verbose:
            print("\nWarnings:")
            for w in summary['warnings']:
                print(f"- {w}")
        else:
            print(f"\n{len(summary['warnings'])} warnings generated. Use --verbose to see details.")


if __name__ == "__main__":
    main()
