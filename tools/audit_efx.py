"""Verify EFX read/write against a named PAK corpus without modifying inputs."""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from file_handlers.efx.efx_file import EfxFile
from file_handlers.pak.reader import CachedPakReader


def audit(paths, reader, version, *, exercise_edits=False, exercise_construction=False, exercise_imports=False):
    passed = 0
    missing, failures = [], []
    types = set()
    rebuilt_types = set()
    imported_types = set()
    for path in paths:
        stream = reader.get_file(path)
        if stream is None:
            missing.append(path)
            continue
        data = stream.getvalue()
        try:
            document = EfxFile.from_bytes(data, version=version)
            output = document.build_validated()
            if output != data:
                offset = next((i for i, (a, b) in enumerate(zip(data, output)) if a != b), min(len(data), len(output)))
                raise ValueError(f"Roundtrip differs at 0x{offset:X}: {len(data)} -> {len(output)} bytes")
            if exercise_construction:
                from file_handlers.efx.editing import data as record_data, record_from_data
                from file_handlers.efx.efx_file import write_record
                from file_handlers.efx.metadata import preserve_unchanged
                from file_handlers.efx.channels import validate_channels
                validate_channels(document)
                for attribute in document.all_attributes(recursive=True):
                    if attribute.name in rebuilt_types:
                        continue
                    original = attribute.body
                    replacement = record_from_data(version, original.classname, record_data(original), original.definition)
                    preserve_unchanged(original, replacement)
                    if write_record(replacement) != write_record(original):
                        raise ValueError(f'{attribute.name}: explicit reconstruction changed native bytes')
                    rebuilt_types.add(attribute.name)
            if exercise_imports:
                from file_handlers.efx.editing import EfxEditor, empty_file
                from file_handlers.efx.graph import scopes, pointer, references
                for prefix, scope, _ in scopes(document):
                    for category in ('entries', 'actions'):
                        for position, entry in enumerate(getattr(scope, category)):
                            new_types = {a.name for a in entry.attributes} - imported_types
                            if not new_types:
                                continue
                            target = EfxEditor(empty_file(version))
                            target.apply({'op': 'import', 'path': category, 'source': pointer(prefix + (category, position))}, donor=document)
                            if not all(r['valid'] for r in references(target.document)):
                                raise ValueError('Imported entry has invalid dependencies')
                            target.document.build_validated()
                            imported_types.update(a.name for a in target.document.all_attributes(recursive=True))
            if exercise_edits and document.entries:
                from file_handlers.efx.editing import EfxEditor
                editor = EfxEditor(document)
                index = len(document.entries)
                source = next((i for i, entry in enumerate(document.entries) if entry.assignment == 2), 0)
                editor.apply({"op": "copy", "source": f"entries/{source}", "path": "entries"})
                editor.apply({"op": "delete", "path": f"entries/{index}"})
                if editor.document.build_validated() != data:
                    raise ValueError("Copy/delete changed unrelated file data")
                editor.undo()
                editor.undo()
                if editor.document.build_validated() != data:
                    raise ValueError("Undo did not restore original bytes")
            types.update(attribute.name for attribute in document.all_attributes(recursive=True))
            passed += 1
        except (ValueError, KeyError, TypeError, UnicodeError) as error:
            failures.append({"path": path, "sha256": hashlib.sha256(data).hexdigest(), "error": str(error)})
    return {"version": version, "selected": len(paths), "passed": passed,
            "missing": missing, "failures": failures, "attribute_types": sorted(types), 'rebuilt_types': sorted(rebuilt_types), 'imported_types': sorted(imported_types)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", type=Path, required=True)
    parser.add_argument("--pak", type=Path, action="append", required=True)
    parser.add_argument("--version", type=int, required=True)
    parser.add_argument("--sample", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--exercise-edits", action="store_true", help="also copy/delete an entry and undo back to the original")
    parser.add_argument('--exercise-construction', action='store_true', help='reconstruct one native instance of every encountered attribute type and validate channels')
    parser.add_argument('--exercise-imports', action='store_true', help='import native entries covering each observed type into empty EFX documents with dependencies')
    args = parser.parse_args()
    suffix = f".efx.{args.version}"
    paths = sorted({line.strip().replace("\\", "/").lower()
                    for line in args.list.read_text(encoding="utf-8-sig").splitlines()
                    if line.strip().lower().endswith(suffix)})
    if args.sample is not None:
        paths = random.Random(0).sample(paths, min(args.sample, len(paths)))
    reader = CachedPakReader.from_paks([str(path) for path in args.pak], game="MHRise").prepare(paths)
    result = audit(paths, reader, args.version, exercise_edits=args.exercise_edits, exercise_construction=args.exercise_construction, exercise_imports=args.exercise_imports)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"EFX {args.version}: {result['passed']} exact roundtrips, {len(result['failures'])} failures, {len(result['missing'])} unavailable resources")
    if args.exercise_construction:
        print(f"Reconstructed {len(result['rebuilt_types'])} native attribute types")
    if args.exercise_imports:
        print(f"Imported entries covering {len(result['imported_types'])} native attribute types")
    for failure in result["failures"][:20]:
        print(f"{failure['path']}: {failure['error']}")
    return 1 if result["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
