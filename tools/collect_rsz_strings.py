import os
import argparse
import time
import sys
import traceback
import json
from pathlib import Path

# RSZ-specific import no longer required for new mode; keep for compatibility (can remove later)
# from file_handlers.rsz.rsz_file import RszFile

def _build_extension_variants(ext_json_path, verbose=False):
    """
    Load the extensions JSON and return a dict:
      ext -> { 'version': int, 'variants': [ '.ext.version', '.ext.version.x64', '.ext.version.x64.en', ... ] }
    Mirrors (approx) the C# Workspace.FindPossibleFilepaths logic.
    """
    with open(ext_json_path, 'r', encoding='utf-8') as jf:
        data = json.load(jf)
    versions = data.get("Versions", {})
    info = data.get("Info", {})
    # Known language codes (exclude platform markers like 'stm')
    known_langs = {'en','ar','de','es','fr','it','ja','ko','ptbr','ru','zhcn','zhtw', 'es419', 'pl'}
    result = {}
    for ext, ver in versions.items():
        meta = info.get(ext, {})
        variants = set()
        base = f".{ext}.{ver}"
        can_have_x64 = meta.get("CanHaveX64", False)
        can_not_have_x64 = meta.get("CanNotHaveX64", False) or not can_have_x64
        # Some JSONs omit CanHaveStm; infer if both x64 flags exist (fallback false)
        can_have_stm = meta.get("CanHaveStm", False)
        can_not_have_stm = meta.get("CanNotHaveStm", False) or not can_have_stm
        can_have_lang = meta.get("CanHaveLang", False)
        can_not_have_lang = meta.get("CanNotHaveLang", False) or not can_have_lang
        locales = [l for l in meta.get("Locales", []) if l in known_langs] if can_have_lang else []

        # Base (no arch/lang) if allowed
        if can_not_have_x64 and can_not_have_lang:
            variants.add(base)

        # Arch only (no lang)
        if can_have_x64 and can_not_have_lang:
            variants.add(base + ".x64")
        if can_have_stm and can_not_have_lang:
            variants.add(base + ".stm")

        # Lang variants
        if can_have_lang and locales:
            if can_have_x64:
                for loc in locales:
                    variants.add(base + f".x64.{loc}")
            if can_have_stm:
                for loc in locales:
                    variants.add(base + f".stm.{loc}")

        # Fallback: ensure at least the plain base
        if not variants:
            variants.add(base)

        result[ext] = {
            "version": ver,
            "variants": sorted(variants)
        }
    if verbose:
        print(f"Loaded {len(result)} extensions from JSON ({ext_json_path})")
    return result

def _looks_extended(s, ext_variants):
    """Return True if s already contains .ext.version for any known ext (prevents re-expansion)."""
    for ext, data in ext_variants.items():
        token = f".{ext}.{data['version']}"
        if token in s:
            return True
    return False

def _expand_all_extensions(strings, ext_variants):
    """
    For each collected string (expected to end with '.ext'), remove the final extension
    and generate every possible combination for every known extension:
      base + (.ext.version[.x64|.stm][.locale])
    Returns dict: base_path -> set[expanded_paths] (deduplicated)
    """
    expansions = {}
    for s in strings:
        if '.' not in s:
            continue
        base = s.rsplit('.', 1)[0]
        bucket = expansions.setdefault(base, set())
        for ext, data in ext_variants.items():
            for variant in data['variants']:
                bucket.add(base + variant)
    return expansions

def _process_list_file(list_path, verbose=False):
    """
    Read a text file of paths, strip whitespace, drop empty/comment lines,
    remove a leading '@' if present, and ensure each line is prefixed with 'natives/stm/'.
    Returns a set of normalized paths using forward slashes.
    """
    processed = set()
    prefix = "natives/stm/"
    try:
        with open(list_path, 'r', encoding='utf-8', errors='ignore') as f:
            for idx, line in enumerate(f, 1):
                raw = line.strip()
                if not raw or raw.startswith('#'):
                    continue
                if raw.startswith('@'):
                    raw = raw[1:]
                # normalize separators
                raw = raw.replace('\\', '/').lstrip('/')
                if not raw.startswith(prefix):
                    raw = prefix + raw
                processed.add(raw)
        if verbose:
            print(f"Loaded {len(processed)} entries from list file: {list_path}")
    except Exception as e:
        print(f"Failed to process list file '{list_path}': {e}")
    return processed

def _extract_utf16le_strings(data, min_len=5):
    """
    Iterate 16-bit little-endian code units, accumulate printable ASCII-range sequences.
    Yield strings meeting min_len.
    """
    current = []
    for i in range(0, len(data) - 1, 2):
        code_unit = data[i] | (data[i+1] << 8)
        # Terminate on NUL or non-printable (excluding common path punctuation)
        if 0x20 <= code_unit <= 0x7E:
            ch = chr(code_unit)
            current.append(ch)
        else:
            if current:
                if len(current) >= min_len:
                    yield ''.join(current)
                current = []
    if current and len(current) >= min_len:
        yield ''.join(current)

def _scan_file_for_utf16le_extensions(filepath, max_bytes, extensions_set, results_set, min_len, verbose=False):
    try:
        size = os.path.getsize(filepath)
        if size > max_bytes:
            return
        with open(filepath, 'rb') as f:
            data = f.read()
        for s in _extract_utf16le_strings(data, min_len=min_len):
            # Check case-sensitive or insensitive? Use case-sensitive first, then lowercase compare
            lower = s.lower()
            for ext in extensions_set:
                if lower.endswith(ext):
                    results_set.add(s)
                    break
    except Exception as e:
        if verbose:
            print(f"[scan error] {filepath}: {e}")

def collect_strings_from_rsz_files(directory, output_file, extensions=None, exclude_dirs=None,
                                   max_depth=None, verbose=False, ext_json=None, expand=False,
                                   list_file=None, max_file_mb=8, min_len=5):
    """
    Scan all files under 'directory' (if provided) whose size <= max_file_mb.
    If no directory is supplied, only the --list-file contents (if any) are used.
    Extract UTF-16-LE printable strings ending with any of the provided extensions.
    """
    if not extensions:
        extensions = ['.pfb', '.scn', '.user', '.tex', '.mot', '.mesh', '.mdf2']
    # Normalize extensions: ensure leading dot, compare lowercase
    norm_exts = set(e if e.startswith('.') else f'.{e}' for e in extensions)
    # We'll match endings; convert to lowercase for comparison
    lowercase_exts = {e.lower() for e in norm_exts}

    exclude_dirs_set = set(exclude_dirs) if exclude_dirs else set()
    max_bytes = int(max_file_mb * 1024 * 1024)

    files_processed = 0
    dirs_visited = 0
    skipped_dirs = 0
    inaccessible_dirs = []

    all_files = []
    # Added nested directory scanner (was missing)
    def scan_directory(current_dir, current_depth=0):
        nonlocal dirs_visited, skipped_dirs, all_files, inaccessible_dirs
        if max_depth is not None and current_depth > max_depth:
            skipped_dirs += 1
            return
        try:
            with os.scandir(current_dir) as it:
                dirs_visited += 1
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name in exclude_dirs_set:
                                skipped_dirs += 1
                                continue
                            scan_directory(entry.path, current_depth + 1)
                        elif entry.is_file(follow_symlinks=False):
                            all_files.append(entry.path)
                            if verbose and len(all_files) % 5000 == 0:
                                print(f"Queued {len(all_files)} files...")
                    except PermissionError:
                        skipped_dirs += 1
                        inaccessible_dirs.append(entry.path)
                        if verbose:
                            print(f"Permission denied: {entry.path}")
        except PermissionError:
            skipped_dirs += 1
            inaccessible_dirs.append(current_dir)
            if verbose:
                print(f"Permission denied: {current_dir}")

    # Only scan filesystem if directory specified
    if directory:
        print(f"Scanning directory {directory} (max file size {max_file_mb} MB)...")
        scan_directory(directory)
        total_files = len(all_files)
        print(f"Collected {total_files} candidate files. Processing...")
        if inaccessible_dirs and verbose:
            print(f"Inaccessible directories/files: {len(inaccessible_dirs)}")
    else:
        total_files = 0
        if not list_file:
            print("Error: provide a directory or --list-file")
            return

    unique_strings = set()

    # Optional list file merge first (normalized via previous helper if desired)
    if list_file:
        # Reuse old helper for normalization + prefix if user still wants it
        list_entries = _process_list_file(list_file, verbose)
        unique_strings.update(list_entries)

    last_progress_update = 0
    for i, fp in enumerate(all_files):
        current_time = time.time()
        if i == 0 or i == total_files - 1 or current_time - last_progress_update > 1:
            pct = (i + 1) / total_files * 100
            print(f"Progress: {pct:.1f}% ({i+1}/{total_files})")
            last_progress_update = current_time
        _scan_file_for_utf16le_extensions(
            fp,
            max_bytes=max_bytes,
            extensions_set=lowercase_exts,
            results_set=unique_strings,
            min_len=min_len,
            verbose=verbose
        )
        files_processed += 1

    # Expansion (applies only to collected strings; same logic as before)
    expanded = {}
    if ext_json and expand and unique_strings:
        try:
            ext_variants = _build_extension_variants(ext_json, verbose)
            expanded = _expand_all_extensions(unique_strings, ext_variants)
        except Exception as ex:
            print(f"Failed to expand variants: {ex}")

    print(f"\nWriting results to {output_file}...")
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write("=== FOUND STRINGS (UTF-16-LE endings) ===\n")
        for s in sorted(unique_strings, key=str.lower):
            f.write(s + "\n")
        if expanded:
            f.write("\n=== EXPANDED STRINGS ===\n")
            for base, variants in expanded.items():
                f.write(f"# {base}\n")
                for v in sorted(variants, key=str.lower):
                    f.write(v + "\n")

    print("\nSummary:")
    print(f"- Directories scanned: {dirs_visited}")
    print(f"- Directories skipped: {skipped_dirs}")
    print(f"- Files processed: {files_processed}")
    print(f"- Unique strings: {len(unique_strings)}")
    if expanded:
        print(f"- Bases expanded: {len(expanded)}")
    print(f"Results written to: {output_file}")

def main():
    parser = argparse.ArgumentParser(description='Scan files (<= size) for UTF-16-LE strings ending with given extensions, or use only --list-file.')
    parser.add_argument('directory', nargs='?', help='Root directory to scan (omit to use only --list-file)')
    parser.add_argument('--output', '-o', default='rsz_strings.txt', help='Output file path')
    parser.add_argument('--extensions', '-e', nargs='+', help='Extensions to match (default: .pfb .scn .user .tex .mot .mesh .mdf2)')
    parser.add_argument('--exclude-dirs', '-x', nargs='+', help='Directory names to exclude')
    parser.add_argument('--max-depth', '-d', type=int, help='Maximum depth (default: unlimited)')
    parser.add_argument('--max-file-mb', type=float, default=8, help='Maximum file size (MB) to scan (default: 8)')
    parser.add_argument('--min-len', type=int, default=5, help='Minimum UTF-16 string length (default: 5)')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose logging')
    parser.add_argument('--ext-json', help='Path to extensions JSON (for expansion)')
    parser.add_argument('--expand', action='store_true', help='Expand collected base strings into all extension/version combinations')
    parser.add_argument('--list-file', help='Optional list file to include (normalized/prefixed)')
    args = parser.parse_args()

    if args.directory and not os.path.isdir(args.directory):
        print(f"Error: '{args.directory}' is not a valid directory")
        return
    if not args.directory and not args.list_file:
        print("Error: provide a directory or --list-file")
        return

    collect_strings_from_rsz_files(
        directory=args.directory,
        output_file=args.output,
        extensions=args.extensions,
        exclude_dirs=args.exclude_dirs,
        max_depth=args.max_depth,
        verbose=args.verbose,
        ext_json=args.ext_json,
        expand=args.expand,
        list_file=args.list_file,
        max_file_mb=args.max_file_mb,
        min_len=args.min_len
    )

if __name__ == "__main__":
    main()