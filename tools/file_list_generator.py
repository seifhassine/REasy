import os
import io
import re
import mmap
from collections import defaultdict


VARIATION_SUFFIXES = [
    ".x64",
    ".stm",
    ".ar",
    ".de",
    ".en",
    ".es419",
    ".es",
    ".fr",
    ".it",
    ".ja",
    ".hi",
    ".ko",
    ".th",
    ".pl",
    ".ptbr",
    ".ru",
    ".zhcn",
    ".zhtw",
    ".x64.stm",
    ".x64.ar",
    ".x64.de",
    ".x64.en",
    ".x64.es419",
    ".x64.es",
    ".x64.fr",
    ".x64.it",
    ".x64.ja",
    ".x64.hi",
    ".x64.ko",
    ".x64.th",
    ".x64.pl",
    ".x64.ptbr",
    ".x64.ru",
    ".x64.zhcn",
    ".x64.zhtw",
]

LARGE_FILE_THRESHOLD = 512 * 1024 * 1024
MMAP_WINDOW_SIZE = 64 * 1024 * 1024
MMAP_OVERLAP_SIZE = 1 * 1024 * 1024

TEX_VARIANT_SUFFIXES = [
    "_acot", "_albd", "_nrmr", "_msr", "_mskm", "_scot", "_nrca", "_msk4", "_alb", "_nrma",
    "_nrm", "_albs", "_atos", "_faketex", "_dslut", "_msk3", "_emi", "_msk1", "_colormask.",
    "_selectionmask", "_hgt", "_nrrc", "_hdr", "_mask", "_msk", "_nrra", "_albm", "_albh",
    "_nrro", "_rocm", "_occ", "_lymo", "_alba", "_nrca", "_alp", "_nmr", "_rgh", "_met",
    "_iam", "_lut", "_fbi", "_add", "_emm", "_lym", "_cvt", "_vns", "_lin", "_pos", "_fur", "_im", "_disp",
]
UNIQUE_TEX_VARIANT_SUFFIXES = tuple(dict.fromkeys(TEX_VARIANT_SUFFIXES))
MAX_LAST_NUMBER_VARIANTS = 1000
LINKED_DUAL_TAIL_FIRST_WINDOW = 32
MAX_LINKED_DUAL_TAIL_COMBINATIONS = 5000
POWER_OF_TWO_VALUES = tuple(1 << exp for exp in range(19))


def _is_valid_extracted_char(char):
    return char.isprintable()


def _extract_utf16le_strings(data, min_len):
    strings = []
    data_len = len(data)
    min_bytes = min_len * 2

    for start_parity in (0, 1):
        i = start_parity
        while i <= data_len - min_bytes:
            if not (32 <= data[i] <= 126 and data[i + 1] == 0):
                i += 2
                continue

            j = i
            chars = []

            while j <= data_len - 2:
                codepoint = data[j] | (data[j + 1] << 8)
                if codepoint == 0:
                    break

                char = chr(codepoint)
                if not _is_valid_extracted_char(char):
                    break

                chars.append(char)
                j += 2

            if len(chars) >= min_len:
                strings.append(''.join(chars))
                i = j + 2
            else:
                i += 2

    return strings


_UTF8_CANDIDATE_PATTERN = re.compile(rb'[\x20-\x7E\x80-\xFF]{10,}')


def _extract_utf8_strings(data, min_len):
    strings = []

    for match in _UTF8_CANDIDATE_PATTERN.finditer(data):
        chunk = match.group(0)
        if len(chunk) < min_len:
            continue

        try:
            decoded = chunk.decode('utf-8')
        except UnicodeDecodeError:
            continue

        if all(_is_valid_extracted_char(ch) for ch in decoded):
            strings.append(decoded)

    return strings


def expand_with_variations(paths):
    expanded = set()
    for path in paths:
        normalized = path.lower()
        expanded.add(normalized)
        for suffix in VARIATION_SUFFIXES:
            if not normalized.endswith(suffix):
                expanded.add(f"{normalized}{suffix}")
    return expanded


def expand_with_streaming(paths, path_prefix):
    expanded = set()
    prefix = (path_prefix.rstrip('/') + '/').lower()
    streaming_prefix = f"{prefix}streaming/"
    for path in paths:
        normalized = path.lower()
        expanded.add(normalized)
        if normalized.startswith(prefix) and not normalized.startswith(streaming_prefix):
            expanded.add(f"{streaming_prefix}{normalized[len(prefix):]}")
    return expanded


class ExtensionAnalyzer:
    def __init__(self, target_extensions=None):
        self.target_extensions = target_extensions or ["scn", "fbxskel", "uvar", "mesh"]
        self.dumped_extensions = {}
        self.list_extensions = defaultdict(set)
        self.combined_extensions = defaultdict(set)
    
    def run_extension_dumper(self, exe_path):
        try:
            from reversing.extension_info import extension_dumper as dumper
            output = dumper.extract_extensions(exe_path, self.target_extensions)
            if "error" in output:
                return False, f"Dumper error: {output['error']}"
            self.dumped_extensions = output
            return True, None
        except Exception as e:
            return False, f"Failed to run extension dumper: {e}"

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


class ExePathExtractor:
    def __init__(
        self,
        extensions,
        extension_versions,
        path_prefix="natives/stm/",
        include_variations=False,
        include_streaming=False
    ):
        self.extensions = [ext.lower() for ext in extensions]
        self.extension_versions = {ext.lower(): {v.lower() for v in versions} 
                                    for ext, versions in extension_versions.items()}
        self.path_prefix = path_prefix.rstrip('/') + '/'
        self.collected_paths = set()
        self.include_variations = include_variations
        self.include_streaming = include_streaming
    
    def _extract_strings(self, data, min_len=10):
        return _extract_utf16le_strings(data, min_len) + _extract_utf8_strings(data, min_len)
    
    def _extract_strings_from_large_mmap(self, mm, progress_callback=None, source_label="binary file"):
        strings = []
        seen = set()
        total = len(mm)
        step = max(1, MMAP_WINDOW_SIZE - MMAP_OVERLAP_SIZE)

        for start in range(0, total, step):
            end = min(total, start + MMAP_WINDOW_SIZE)
            chunk = mm[start:end]

            for value in self._extract_strings(chunk):
                if value not in seen:
                    seen.add(value)
                    strings.append(value)

            if progress_callback and total > 0:
                progress = int((end / total) * 30)
                progress_callback(f"Reading {source_label}...", min(progress, 30), 100)

            if end >= total:
                break

        return strings

    def extract_paths_from_binary_file(self, file_path, progress_callback=None, source_label="binary file"):
        self.collected_paths.clear()

        if progress_callback:
            progress_callback(f"Reading {source_label}...", 0, 100)

        try:
            file_size = os.path.getsize(file_path)
            if file_size >= LARGE_FILE_THRESHOLD:
                with open(file_path, 'rb') as f:
                    with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                        strings = self._extract_strings_from_large_mmap(mm, progress_callback, source_label)
            else:
                with open(file_path, 'rb') as f:
                    strings = self._extract_strings(f.read())
        except Exception:
            return True, None, 0

        if progress_callback:
            progress_callback("Processing strings...", 30, 100)

        base_paths = set()
        for s in strings:
            path = s.replace('\\', '/').lstrip('@')
            if ':' in path:
                path = path.split(':')[-1]
            path = path.lstrip('@')

            parts = path.split('.')
            version_idx = None
            for i in range(len(parts) - 1, 0, -1):
                try:
                    int(parts[i])
                    version_idx = i
                except ValueError:
                    if version_idx:
                        break

            base = '.'.join(parts[:version_idx]) if version_idx and version_idx > 1 else path
            if any(base.lower().endswith(f'.{ext}') for ext in self.extensions):
                base_paths.add(base.lower())

        if progress_callback:
            progress_callback("Generating version combinations...", 60, 100)

        for base in base_paths:
            if not base.startswith('natives/'):
                base = self.path_prefix + base

            parts = base.split('.')
            if len(parts) >= 2:
                ext = parts[-1]
                versions = self.extension_versions.get(ext, set())
                if versions:
                    for v in versions:
                        self.collected_paths.add(f"{base}.{v}")
                else:
                    self.collected_paths.add(base)
            else:
                self.collected_paths.add(base)

        if progress_callback:
            progress_callback("Extraction complete!", 100, 100)

        paths = self.collected_paths
        if self.include_streaming:
            paths = expand_with_streaming(paths, self.path_prefix)
        if self.include_variations:
            paths = expand_with_variations(paths)
        self.collected_paths = paths

        return True, None, len(self.collected_paths)

    def extract_paths_from_exe(self, exe_path, progress_callback=None):
        return self.extract_paths_from_binary_file(exe_path, progress_callback, source_label="executable")
    
    def export_to_file(self, output_path):
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                for path in sorted(self.collected_paths):
                    f.write(path + '\n')
            return True, None
        except Exception as e:
            return False, f"Failed to write output file: {e}"


class PathCollector:
    def __init__(
        self,
        extensions,
        extension_versions=None,
        path_prefix="natives/stm/",
        include_variations=False,
        include_streaming=False,
        include_tex_variants=False,
        include_extension_swaps=False
    ):
        self.extensions = [ext.lower() for ext in extensions]
        self.extension_versions = {}
        if extension_versions:
            for ext, versions in extension_versions.items():
                self.extension_versions[ext.lower()] = {v.lower() for v in versions}
        self.path_prefix = path_prefix.rstrip('/') + '/'
        self.collected_paths = set()
        self.include_variations = include_variations
        self.include_streaming = include_streaming
        self.include_tex_variants = include_tex_variants
        self.include_extension_swaps = include_extension_swaps
        self._linked_dual_tail_values = {}
    
    def filter_path_by_extensions(self, path):
        path_lower = path.lower()
        for ext in self.extensions:
            if path_lower.endswith(f'.{ext}'):
                return True
        return False
    
    def should_skip_entry(self, entry):
        return entry.decompressed_size > 50 * 1024 * 1024 or entry.decompressed_size < 100
    
    def _parse_list_path_components(self, line):
        parts = line.split('.')
        if len(parts) < 2:
            return None, None, None

        path_without_version = None
        extension = None

        for i in range(len(parts) - 1, 0, -1):
            try:
                int(parts[i])
                path_without_version = '.'.join(parts[:i])
                extension = parts[i - 1].lower() if i > 0 else None
                break
            except ValueError:
                if i > 1:
                    try:
                        int(parts[i - 1])
                        path_without_version = '.'.join(parts[:i - 1])
                        extension = parts[i - 2].lower() if i > 1 else None
                        break
                    except ValueError:
                        pass

        if not path_without_version or not extension:
            return None, None, None

        base_parts = path_without_version.split('.')
        if len(base_parts) < 2:
            return None, None, None

        stem = '.'.join(base_parts[:-1]).lower()
        extension = base_parts[-1].lower()
        return stem, path_without_version.lower(), extension

    def _rewrite_stem_prefix(self, stem):
        stem = stem.lower()
        if not stem.startswith('natives/'):
            return f"{self.path_prefix}{stem.lstrip('/')}"

        parts = stem.split('/', 2)
        if len(parts) < 3:
            return f"{self.path_prefix}{parts[-1]}"

        return f"{self.path_prefix}{parts[2]}"

    def _collect_improver_entries(self, list_file_path, override_prefix=False):
        known_extensions = set(self.extensions) | set(self.extension_versions.keys())
        entries = set()
        with open(list_file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or not line.startswith('natives/'):
                    continue

                stem, _, extension = self._parse_list_path_components(line)
                if not stem or not extension or extension not in known_extensions:
                    continue

                normalized_stem = self._rewrite_stem_prefix(stem) if override_prefix else stem
                entries.add((normalized_stem, extension))
        return entries

    def _iter_extension_suffixes(self, extension):
        versions = self.extension_versions.get(extension, set())
        if versions:
            for version in versions:
                yield f"{extension}.{version}"
        else:
            yield extension

    def _suffix_extension(self, suffix):
        return suffix.split('.', 1)[0].lower()

    def _iter_improver_suffixes(self, source_extension):
        if not self.include_extension_swaps:
            yield from self._iter_extension_suffixes(source_extension)
            return

        candidate_extensions = dict.fromkeys([source_extension, *self.extensions, *self.extension_versions.keys()])
        yielded = set()
        for extension in candidate_extensions:
            for suffix in self._iter_extension_suffixes(extension):
                if suffix not in yielded:
                    yielded.add(suffix)
                    yield suffix


    def _iter_number_values(self, token):
        digit_count = len(token)
        if digit_count <= 0:
            return [0]

        max_value = (10 ** digit_count) - 1
        capped_max = min(max_value, MAX_LAST_NUMBER_VARIANTS - 1)
        return range(0, capped_max + 1)

    def _is_power_of_two_token(self, token):
        try:
            value = int(token)
        except ValueError:
            return False
        return value > 0 and (value & (value - 1)) == 0

    def _replace_match_number(self, stem, match, value):
        token = match.group(0)
        is_zero_padded = token.startswith('0') and len(token) > 1
        rendered = str(value).zfill(len(token)) if is_zero_padded else str(value)
        return f"{stem[:match.start()]}{rendered}{stem[match.end():]}"

    def _get_linked_dual_tail_context(self, stem):
        matches = list(re.finditer(r'\d+', stem))
        if len(matches) < 2:
            return None

        first = matches[-2]
        second = matches[-1]

        if stem[first.end():second.start()] != '_':
            return None

        first_token = first.group(0)
        second_token = second.group(0)
        if len(first_token) != 3 or len(second_token) != 2:
            return None

        first_pattern = re.compile(rf'(?<=[_/]){re.escape(first_token)}(?=[_/]|$)')
        if len(list(first_pattern.finditer(stem))) < 2:
            return None

        key_with_second_placeholder = f"{stem[:second.start()]}{{B}}"
        family_key = first_pattern.sub('{A}', key_with_second_placeholder)

        return {
            'first_token': first_token,
            'second_token': second_token,
            'first_pattern': first_pattern,
            'second_span': (second.start(), second.end()),
            'family_key': family_key,
        }

    def _build_linked_dual_tail_values(self, entries):
        values = {}
        for stem, _ in entries:
            context = self._get_linked_dual_tail_context(stem)
            if not context:
                continue

            family = values.setdefault(context['family_key'], {'first': set(), 'second': set()})
            family['first'].add(int(context['first_token']))
            family['second'].add(int(context['second_token']))
        return values

    def _iter_linked_dual_tail_candidates(self, stem):
        context = self._get_linked_dual_tail_context(stem)
        if not context:
            return

        first_token = context['first_token']
        second_token = context['second_token']
        first_pattern = context['first_pattern']
        second_start, second_end = context['second_span']

        observed = self._linked_dual_tail_values.get(context['family_key'])
        if observed:
            first_values = sorted(observed['first'])
            second_values = sorted(observed['second'])
        else:
            first_original = int(first_token)
            first_values = list(range(max(0, first_original - LINKED_DUAL_TAIL_FIRST_WINDOW), min(999, first_original + LINKED_DUAL_TAIL_FIRST_WINDOW) + 1))
            second_values = list(self._iter_number_values(second_token))

        max_combinations = MAX_LINKED_DUAL_TAIL_COMBINATIONS
        generated = 0
        for first_value in first_values:
            first_rendered = str(first_value).zfill(len(first_token))
            stem_with_first = first_pattern.sub(first_rendered, stem)
            for second_value in second_values:
                second_rendered = str(second_value).zfill(len(second_token))
                yield f"{stem_with_first[:second_start]}{second_rendered}{stem_with_first[second_end:]}"
                generated += 1
                if generated >= max_combinations:
                    return

    def _iter_numeric_stem_combinations(self, stem):
        number_matches = list(re.finditer(r'\d+', stem))
        if not number_matches:
            yield stem
            return

        seen = set()

        seen.add(stem)
        yield stem

        for match in number_matches:
            for value in self._iter_number_values(match.group(0)):
                candidate = self._replace_match_number(stem, match, value)
                if candidate not in seen:
                    seen.add(candidate)
                    yield candidate

        for linked_candidate in self._iter_linked_dual_tail_candidates(stem):
            if linked_candidate not in seen:
                seen.add(linked_candidate)
                yield linked_candidate

        for match in number_matches:
            if not self._is_power_of_two_token(match.group(0)):
                continue
            for value in POWER_OF_TWO_VALUES:
                candidate = self._replace_match_number(stem, match, value)
                if candidate not in seen:
                    seen.add(candidate)
                    yield candidate

    def _split_tex_stem_variant(self, stem):
        lower_stem = stem.lower()
        for variant in UNIQUE_TEX_VARIANT_SUFFIXES:
            if lower_stem.endswith(variant):
                return stem[:-len(variant)], variant
        return stem, None

    def _iter_tex_stem_variants(self, stem, extension):
        if extension != 'tex' or not self.include_tex_variants:
            yield stem
            return

        base_stem, used_variant = self._split_tex_stem_variant(stem)

        for suffix in UNIQUE_TEX_VARIANT_SUFFIXES:
            if used_variant and suffix == used_variant:
                continue
            yield f"{base_stem}{suffix}"
    
    def extract_strings_from_data(self, data):
        min_length = 10
        return _extract_utf16le_strings(data, min_length) + _extract_utf8_strings(data, min_length)
    
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

                    _, path_without_version, extension = self._parse_list_path_components(line)
                    if not path_without_version:
                        continue

                    versions = self.extension_versions.get(extension, set())
                    if versions:
                        for version in versions:
                            self.collected_paths.add(f"{path_without_version}.{version}".lower())
                    else:
                        self.collected_paths.add(path_without_version.lower())
            
            return True, None
        except Exception as e:
            return False, f"Failed to process list file: {e}"
    
    def generate_improved_paths_from_list(self, list_file_path, progress_callback=None, override_prefix=False):
        if not os.path.exists(list_file_path):
            return False, f"List file not found: {list_file_path}", 0, 0

        try:
            entries = sorted(self._collect_improver_entries(list_file_path, override_prefix=override_prefix))

            if not entries:
                return False, "No valid list entries found to improve", 0, 0

            self.collected_paths.clear()

            for idx, (stem, extension) in enumerate(entries, 1):
                if progress_callback:
                    should_stop = progress_callback(
                        f"Generating combinations for list entry {idx}/{len(entries)}", idx, len(entries)
                    )
                    if should_stop:
                        break

                for suffix in self._iter_extension_suffixes(extension):
                    self.collected_paths.add(f"{stem}.{suffix}".lower())

            if progress_callback:
                progress_callback("List improver combination generation complete", len(entries), len(entries))

            return True, None, len(entries), len(self.collected_paths)
        except Exception as e:
            return False, f"Failed to build improved list combinations: {e}", 0, 0

    def _collect_pak_hashes(self, pak_directory, progress_callback=None):
        from file_handlers.pak import scan_pak_files, PakFile

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

        return True, None, pak_hashes

    def _iter_improver_candidates(self, stem, extension, suffixes, on_base_candidate=None):
        stem_iter = self._iter_tex_stem_variants(stem, extension)

        for variant_stem in stem_iter:
            if self.include_tex_variants or self.include_extension_swaps:
                numeric_iter = [variant_stem]
            else:
                numeric_iter = self._iter_numeric_stem_combinations(variant_stem)
            for numeric_stem in numeric_iter:
                for suffix in suffixes:
                    if on_base_candidate:
                        on_base_candidate(numeric_stem, self._suffix_extension(suffix))
                    base_candidate = f"{numeric_stem}.{suffix}"
                    for candidate in self._iter_expanded_variants(base_candidate):
                        yield candidate

    def _iter_expanded_variants(self, base_candidate):
        candidates = [base_candidate]

        if self.include_streaming:
            prefix = self.path_prefix.lower()
            streaming_prefix = f"{prefix}streaming/"
            if base_candidate.startswith(prefix) and not base_candidate.startswith(streaming_prefix):
                candidates.append(f"{streaming_prefix}{base_candidate[len(prefix):]}")

        for candidate in candidates:
            yield candidate
            if self.include_variations:
                for variation_suffix in VARIATION_SUFFIXES:
                    if not candidate.endswith(variation_suffix):
                        yield f"{candidate}{variation_suffix}"

    def improve_list_with_chunked_validation(self, list_file_path, pak_directory, progress_callback=None, override_prefix=False):
        try:
            from file_handlers.pak.utils import filepath_hash

            if not os.path.exists(list_file_path):
                return False, f"List file not found: {list_file_path}", None, set()

            entries = sorted(self._collect_improver_entries(list_file_path, override_prefix=override_prefix))

            if not entries:
                return False, "No valid list entries found to improve", None, set()

            self._linked_dual_tail_values = self._build_linked_dual_tail_values(entries)

            ok, error, pak_hashes = self._collect_pak_hashes(pak_directory, progress_callback)
            if not ok:
                return False, error, None, set()

            validated_paths = set()
            generated_candidates = 0
            remaining_entries = set(entries)

            progress_update_interval = 5000

            for idx, (stem, extension) in enumerate(entries, 1):
                current_entry = (stem, extension)
                if current_entry not in remaining_entries:
                    continue

                remaining_entries.discard(current_entry)
                suffixes = list(self._iter_improver_suffixes(extension))
                generated_in_entry = 0

                def consume_matching_entry(candidate_stem, candidate_extension):
                    remaining_entries.discard((candidate_stem, candidate_extension))

                for candidate in self._iter_improver_candidates(stem, extension, suffixes, on_base_candidate=consume_matching_entry):
                    generated_candidates += 1
                    generated_in_entry += 1
                    candidate_hash = filepath_hash(candidate)
                    if candidate_hash in pak_hashes:
                        validated_paths.add(candidate)
                        pak_hashes.discard(candidate_hash)

                    if progress_callback and generated_in_entry % progress_update_interval == 0:
                        progress_callback(
                            f"Improving list entries...\nEntry {idx}/{len(entries)} ({extension})\n"
                            f"Generated in current entry: {generated_in_entry}\n"
                            f"Generated so far: {generated_candidates}\n"
                            f"Valid so far: {len(validated_paths)}",
                            idx,
                            len(entries)
                        )

                if progress_callback:
                    progress_callback(
                        f"Improving list entries...\nEntry {idx}/{len(entries)} ({extension}) complete\n"
                        f"Generated in current entry: {generated_in_entry}\n"
                        f"Generated so far: {generated_candidates}\n"
                        f"Valid so far: {len(validated_paths)}",
                        idx,
                        len(entries)
                    )

                if not pak_hashes:
                    break

            stats = {
                'source_entries': len(entries),
                'generated_candidates': generated_candidates,
                'validated_paths': len(validated_paths),
            }
            return True, None, stats, validated_paths
        except Exception as e:
            return False, f"List improver error: {e}", None, set()

    def validate_paths_against_paks(self, pak_directory, progress_callback=None):
        try:
            from file_handlers.pak.utils import filepath_hash

            ok, error, pak_hashes = self._collect_pak_hashes(pak_directory, progress_callback)
            if not ok:
                return False, error, set()

            paths_to_validate = self._get_paths_for_validation()

            if progress_callback:
                progress_callback("Validating collected paths...", 0, len(paths_to_validate))

            validated_paths = set()
            total_paths = len(paths_to_validate)

            for idx, path in enumerate(paths_to_validate, 1):
                if progress_callback and idx % 1000 == 0:
                    progress_callback(f"Validating paths...\nChecked: {idx}/{total_paths}\nValid: {len(validated_paths)}", idx, total_paths)

                path_hash = filepath_hash(path)
                if path_hash in pak_hashes:
                    validated_paths.add(path.lower())
                    pak_hashes.discard(path_hash)

                if not pak_hashes:
                    break

            if progress_callback:
                progress_callback(f"Validation complete!\nValid paths: {len(validated_paths)}/{total_paths}", total_paths, total_paths)

            return True, None, validated_paths
        except Exception as e:
            return False, f"Validation error: {e}", set()

    def export_to_file(self, output_path, paths=None):
        try:
            paths_to_export = paths if paths is not None else self._get_paths_for_validation()
            sorted_paths = sorted(p.lower() for p in paths_to_export)
            with open(output_path, 'w', encoding='utf-8') as f:
                for path in sorted_paths:
                    f.write(path + '\n')
            return True, None
        except Exception as e:
            return False, f"Failed to write output file: {e}"
    
    def get_path_count(self):
        return len(self._get_paths_for_validation())

    def _expand_paths_for_validation(self, paths):
        expanded_paths = paths
        if self.include_streaming:
            expanded_paths = expand_with_streaming(expanded_paths, self.path_prefix)
        if self.include_variations:
            expanded_paths = expand_with_variations(expanded_paths)
        return expanded_paths

    def _get_paths_for_validation(self):
        return self._expand_paths_for_validation(self.collected_paths)
    
    def clear(self):
        self.collected_paths.clear()
