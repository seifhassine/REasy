import os
import io
import re
import mmap
from collections import defaultdict
from itertools import islice
from enum import Enum

from file_handlers.bounded_cache import BoundedCache
lazy from file_handlers.pak.utils import filepath_hash, normalize_pak_path, read_pak_entry_hashes
lazy from services.file_io import write_files_atomically
from utils.native_build import ensure_fast_path_search, ensure_fast_string_scan
lazy from tools.file_list_search import Search
lazy import ctypes
lazy import msvcrt
lazy import struct


_extract_strings = ensure_fast_string_scan().extract_strings
_render_stems = ensure_fast_path_search().render_stems


NATIVE_PREFIX = "natives/"


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
# Module-level so it outlives each UI run's fresh collector.
PAK_HASH_CACHE_LIMIT = 500_000
_PAK_HASH_CACHE = BoundedCache()

TEX_VARIANT_SUFFIXES = """
_acot _atoc _albd _nrmr _fakemetalliccolora _fakemetalliccolorb _msr _mskm _scot _nrca _msk4 _alb _nrma _nrm _hgh _albs _atos _cm1
_faketex _dslut _msk3 _pupple _emi _msk1 _basecolor _height _alphamaskrange _colormask _selectionmask _hgt _nrrc _hdr _mask _cm2
_msk _nrra _albm _albh _nrro _rocm _occ _lymo _optm _alba _alp _nmr _rgh _met _nrrh _iam
_lut _fbi _add _sphere _colr _emm _lym _cvt _vns _ldoa _talb _ocnm _lin _nrm1 _pos _fur _im _disp _dark _position _albr _detail
_nor _gradientcolormap _cooa _ddov _yellow _blend _albg _wpfx _emfx _teth _roughness _mskinside _dmgr _gradetioncolorc _gradetioncolorb _nroc _after
_gradetioncolora _red _dem _emissivemap _fakeclothmask _albc _emms _emirainbow _fakesphermaskmap _wmask _blendmask
_flw _dmgmask _hbl _ditailmask _mask_mm _addlayer _multiplylayer _tls _cwmask _msb _cmr _dsp _off _dtl _fakesphere _spc _mak1 _alp1 _hight _mtos
_atod _albmsc _fl _sde _fms _vfx _rainbow_map _wam _tala _hair _wpms _mask3ch _mskhair _vec _reindowmap _nram _nrrt _dmask _cmask _nrda
_alpm _shape _scroll _kihoumap _fi _brown _clh _toc _dtal _interior _rcm _emitmaskmap _normal _bml _pn _nrme _array _nrrm _reindowmask _smask _preview _evening _stoc
_earlymorning _msdf _vamask _fxemi _wave _kekkanmap _nm _alb_b _cole _mud _rcto _day _night _event _hit _nrmd _msk2 _ani _sss _dmr _cma _hires _stoc _master
_itm _color _msdf _fvm _fka _sem _mask1 _sr _demi _flowemi _albe _blue _spec _coll _rgba _grooming _gradetionmaska _gradetionmaskb _gradetionmaskc _grd _edit _cc _ver2 _out
_inside _flm _stcf _emd _hm _old _cubemap _eqfx _lava lym0 _auroracol _nroe _wing _ver6 _flow _msa _all _bm _bm2 _fp32 _localcubemap _alpg _msc _flowmap _fm _atex _ocma _hgal _mask4
_reinbowemissivemask _cmv _fvn _demc _cm _ms3 _rem _bde _remi _emipng _bclo _fakesphare _em _ho _cmm _fx _hairover _wind _bam _nml _white _drt _ats _morning _trs _mask24_array _mask12_array _ems
_mask41_array _green _emie _centermask _eyeb _eyec _vt _wemi _cmb _fkb _lensmap _pr _fkot _idm _fcmr _fcm _fxmap2 _vet _glittermask _emifx _nrof _over _specs _direction _pulse _windmask _nrrs _stcm _msk3_array _albo _vecm _ocdh _gradientcolor _gradient e_ssoc _emissive
""".split()
UNIQUE_TEX_VARIANT_SUFFIXES = tuple(dict.fromkeys(TEX_VARIANT_SUFFIXES))
TEX_FAMILY_ENDINGS = ('', *UNIQUE_TEX_VARIANT_SUFFIXES)
TEX_SUFFIX_SET = frozenset(UNIQUE_TEX_VARIANT_SUFFIXES)
TEX_SUFFIX_LENGTHS = tuple(sorted({len(suffix) for suffix in UNIQUE_TEX_VARIANT_SUFFIXES}, reverse=True))
DIGITS = re.compile(r'\d+')
VERSIONED_FILENAME = re.compile(r"^(.+\.([a-z][a-z0-9_]*))\.([0-9]+(?:\.[a-z0-9_]+)*)$")
TEX_MDF2_MESH_SWAP_EXTENSIONS = ("tex", "mdf2", "mesh")
STEM_SUFFIX_PROBE_STRINGS = (
    " -",
    "-",
    " (1)",
    " (2)",
    " (3)",
    " (4)",
    " _",
    "_",
    " ",
)
MAX_LAST_NUMBER_VARIANTS = 1000
LINKED_DUAL_TAIL_FIRST_WINDOW = 32
MAX_LINKED_DUAL_TAIL_COMBINATIONS = 5000
POWER_OF_TWO_VALUES = tuple(1 << exp for exp in range(19))
SMART_NUMERIC_LOCAL_RADIUS = 32
SMART_NUMERIC_OBSERVED_RADIUS = 3
SMART_NUMERIC_DIRECTORY_RADIUS = 8
SMART_NUMERIC_GAP_LIMIT = 12
SMART_NUMERIC_BOUNDARY_SIZE = 24
SMART_NUMERIC_PROBE_STEP = 16
SMART_NUMERIC_EXHAUSTIVE_OBSERVED_THRESHOLD = 24


STEMS_PER_BLOCK = 1024
_ENDINGS_WITHOUT = {}


def endings_without(ending):
    endings = _ENDINGS_WITHOUT.get(ending)
    if endings is None:
        endings = _ENDINGS_WITHOUT[ending] = tuple(other for other in UNIQUE_TEX_VARIANT_SUFFIXES if other != ending)
    return endings


def combined_heads(stem, ending, texture):
    """``fast_path_search.c`` and ``file_list_gpu.py`` build the same heads."""
    heads = [stem, stem[:-len(ending)]] if ending else [stem]
    if texture:
        base = heads[-1]
        heads.extend(base + other for other in endings_without(ending))
    return heads


class CombinedStems:
    __slots__ = ('stems', 'ending', 'textures', 'endings', 'plain', 'per_stem')

    def __init__(self, stems, ending, textures):
        self.stems, self.ending, self.textures = stems, ending, textures
        self.endings = endings_without(ending)
        self.plain = 2 if ending else 1
        self.per_stem = sum(self.plain + len(self.endings) if texture else self.plain for texture in textures)

    def expand(self, tails):
        return [head + tail for stem in self.stems for tail, texture in zip(tails, self.textures)
                for head in combined_heads(stem, self.ending, texture)]

    def candidate(self, tails, index):
        stem = self.stems[index // self.per_stem]
        index %= self.per_stem
        for tail, texture in zip(tails, self.textures):
            heads = combined_heads(stem, self.ending, texture)
            if index < len(heads):
                return heads[index] + tail
            index -= len(heads)
        raise IndexError(index)


def block_size(heads, tails):
    return len(heads.stems) * heads.per_stem if isinstance(heads, CombinedStems) else len(heads) * len(tails)


def expand_block(heads, tails, tails_outer, first=0, count=None):
    if isinstance(heads, CombinedStems):
        paths = heads.expand(tails)
    elif tails_outer:
        paths = [head + tail for tail in tails for head in heads]
    else:
        paths = [head + tail for head in heads for tail in tails]
    return paths[first:None if count is None else first + count] if first or count is not None else paths


class ImproverMode(Enum):
    NUMERIC = "numeric"
    TEX_VARIANTS = "tex_variants"
    EXTENSION_SWAPS = "extension_swaps"
    TEX_MDF2_MESH_SWAPS = "tex_mdf2_mesh_swaps"
    STEM_SUFFIX_PROBES = "stem_suffix_probes"
    CROSS_GAME_VERSION_SWAPS = "cross_game_version_swaps"
    COMBINED = "combined"


def numeric_mode(mode):
    return mode in (ImproverMode.NUMERIC, ImproverMode.COMBINED)


def gpu_helps(mode, advanced_numeric):
    """Elsewhere the GPU's setup costs more than it saves."""
    return mode is ImproverMode.COMBINED or (mode is ImproverMode.NUMERIC and advanced_numeric)


def budget_applies(mode, compute_backend, advanced_numeric):
    return compute_backend == 'gpu' or (advanced_numeric and numeric_mode(mode)) or mode is ImproverMode.COMBINED


def _pak_signature(pak_path):
    with open(pak_path, 'rb') as stream:
        stat = os.fstat(stream.fileno())
        change_time = stat.st_ctime_ns
        if os.name == 'nt':
            # Windows st_ctime is creation time, not the NTFS metadata change time.
            info = ctypes.create_string_buffer(40)  # FILE_BASIC_INFO
            handle = ctypes.c_void_p(msvcrt.get_osfhandle(stream.fileno()))
            if not ctypes.windll.kernel32.GetFileInformationByHandleEx(handle, 0, info, len(info)):
                raise ctypes.WinError()
            change_time = struct.unpack_from('<q', info.raw, 24)[0]
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, change_time)


def _read_pak_hashes(pak_path):
    path = os.path.normcase(os.path.realpath(pak_path))
    signature = _pak_signature(path)
    cached = _PAK_HASH_CACHE.get(path)
    if cached is not None and cached[0] == signature:
        return cached[1]

    hashes = frozenset(read_pak_entry_hashes(path, strict=True).tolist())
    if _pak_signature(path) != signature:
        raise OSError("PAK changed while reading its index; retry the scan")
    _PAK_HASH_CACHE.put(path, (signature, hashes), len(hashes), 32, PAK_HASH_CACHE_LIMIT)
    return hashes


def normalize_path(path):
    return normalize_pak_path(str(path).strip().strip('"'), lowercase=True)


def normalize_prefix(prefix):
    prefix = normalize_path(prefix).strip('/')
    return prefix + '/' if prefix else ''


def split_file_path(path):
    path = normalize_path(path)
    directory, _, filename = path.rpartition('/')
    match = VERSIONED_FILENAME.fullmatch(filename)
    if match:
        base, extension, version = match.groups()
    else:
        stem, separator, extension = filename.rpartition('.')
        if not separator or not stem or not extension:
            return None, None, None
        base, version = filename, None
    return (directory + '/' if directory else '') + base, extension, version


def expand_with_variations(paths):
    expanded = set()
    suffixes = sorted(VARIATION_SUFFIXES, key=len, reverse=True)
    for path in paths:
        normalized = normalize_path(path)
        expanded.add(normalized)
        for suffix in suffixes:
            if normalized.endswith(suffix) and split_file_path(normalized[:-len(suffix)])[0]:
                normalized = normalized[:-len(suffix)]
                break
        expanded.add(normalized)
        expanded.update(normalized + suffix for suffix in VARIATION_SUFFIXES)
    return expanded


def expand_with_streaming(paths, path_prefix):
    expanded = set()
    prefix = normalize_prefix(path_prefix)
    streaming_prefix = f"{prefix}streaming/"
    for path in paths:
        normalized = normalize_path(path)
        expanded.add(normalized)
        if normalized.startswith(prefix) and not normalized.startswith(streaming_prefix):
            expanded.add(f"{streaming_prefix}{normalized[len(prefix):]}")
    return expanded


def version_sort_key(value):
    try:
        return (0, int(value))
    except ValueError:
        return (1, value)


def iter_list_paths(list_file_path):
    with open(list_file_path, "r", encoding="utf-8-sig") as stream:
        for line in stream:
            path = normalize_path(line)
            if path and not path.startswith('#'):
                yield path


def read_list_paths(list_file_path):
    return set(iter_list_paths(list_file_path))


def export_paths(output_path, paths):
    try:
        listed = ''.join(path + '\n' for path in sorted({normalize_path(path) for path in paths}))
        write_files_atomically({os.path.abspath(output_path): listed.encode('utf-8')})
        return True, None
    except Exception as error:
        return False, f"Failed to write output file: {error}"


class ExtensionAnalyzer:
    def __init__(self, target_extensions=None):
        self.target_extensions = target_extensions or ["scn", "fbxskel", "uvar", "mesh"]
        self.dumped_extensions = {}
        self.list_extensions = defaultdict(set)
        self.combined_extensions = defaultdict(set)
    
    def run_extension_dumper(self, exe_path):
        self.dumped_extensions.clear()
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
        self.list_extensions.clear()
        if not os.path.exists(list_file_path):
            return False, f"List file not found: {list_file_path}"

        parsed_extensions = defaultdict(set)
        try:
            for path in iter_list_paths(list_file_path):
                if not path.startswith(NATIVE_PREFIX):
                    continue
                _, extension, version = split_file_path(path)
                if version:
                    parsed_extensions[extension].add(version)

            self.list_extensions.update(parsed_extensions)
            return True, None
        except Exception as e:
            return False, f"Failed to parse list file: {e}"
    
    def combine_extensions(self):
        self.combined_extensions.clear()
        for ext, version in self.dumped_extensions.items():
            self.combined_extensions[ext.lower()].add(str(version).lower())
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
        
        dumper_versions = {str(dumper_version).lower()}
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
        self.combined_extensions[extension.lower()] = set(versions)
    
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




class _GatheredPaths:
    def __init__(self, extensions, extension_versions=None, path_prefix="natives/stm/", include_variations=False,
                 include_streaming=False):
        self.extensions = list(dict.fromkeys(ext.lower().lstrip('.') for ext in extensions))
        self.extension_versions = {ext.lower().lstrip('.'): {str(v).lower() for v in versions}
                                   for ext, versions in (extension_versions or {}).items()}
        self.path_prefix = normalize_prefix(path_prefix)
        self.collected_paths = set()
        self.include_variations = include_variations
        self.include_streaming = include_streaming

    def _add_reference(self, string, keep_found_version):
        base, extension, version = split_file_path(normalize_path(string.rsplit(':', 1)[-1].lstrip('@')))
        if extension not in self.extensions:
            return False
        if not base.startswith(NATIVE_PREFIX):
            base = self.path_prefix + base.lstrip('/')
        versions = self.extension_versions.get(extension, ())
        self.collected_paths.update(f"{base}.{value}" for value in versions)
        if version and keep_found_version:
            self.collected_paths.add(f"{base}.{version}")
        elif not versions:
            self.collected_paths.add(base)
        return True

    def paths(self):
        paths = self.collected_paths
        if self.include_streaming:
            paths = expand_with_streaming(paths, self.path_prefix)
        if self.include_variations:
            paths = expand_with_variations(paths)
        return paths

    def get_path_count(self):
        return len(self.paths())

    def export_to_file(self, output_path, paths=None):
        return export_paths(output_path, paths if paths is not None else self.paths())

    def clear(self):
        self.collected_paths.clear()


class ExePathExtractor(_GatheredPaths):
    def _extract_strings(self, data, min_len=10):
        return _extract_strings(data, min_len)
    
    def _extract_strings_from_large_mmap(self, mm, progress_callback=None, source_label="binary file"):
        strings = []
        seen = set()
        total = len(mm)
        step = max(1, MMAP_WINDOW_SIZE - MMAP_OVERLAP_SIZE)

        for start in range(0, total, step):
            end = min(total, start + MMAP_WINDOW_SIZE)
            with memoryview(mm)[start:end] as chunk:
                for value in self._extract_strings(chunk):
                    if value not in seen:
                        seen.add(value)
                        strings.append(value)

            if progress_callback and total > 0:
                progress = int((end / total) * 30)
                if progress_callback(f"Reading {source_label}...", min(progress, 30), 100):
                    return None

            if end >= total:
                break

        return strings

    def extract_paths_from_binary_file(self, file_path, progress_callback=None, source_label="binary file"):
        self.collected_paths.clear()

        if progress_callback and progress_callback(f"Reading {source_label}...", 0, 100):
            return True, None, 0

        try:
            file_size = os.path.getsize(file_path)
            if file_size >= LARGE_FILE_THRESHOLD:
                with open(file_path, 'rb') as f:
                    with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                        strings = self._extract_strings_from_large_mmap(mm, progress_callback, source_label)
            else:
                with open(file_path, 'rb') as f:
                    strings = self._extract_strings(f.read())
        except (OSError, ValueError) as error:
            return False, f"Failed to read {source_label}: {error}", 0

        if strings is None:
            return True, None, 0
        if progress_callback and progress_callback("Processing strings...", 30, 100):
            return True, None, 0

        for index, string in enumerate(strings):
            if progress_callback and index % 1000 == 0:
                if progress_callback("Processing strings...", 30 + int(65 * index / max(1, len(strings))), 100):
                    return True, None, 0
            self._add_reference(string, keep_found_version=False)

        if progress_callback:
            progress_callback("Extraction complete!", 100, 100)
        return True, None, self.get_path_count()


class PathCollector(_GatheredPaths):
    def __init__(
        self,
        extensions,
        extension_versions=None,
        path_prefix="natives/stm/",
        include_variations=False,
        include_streaming=False,
        improver_mode=ImproverMode.NUMERIC,
        compute_backend='cpu',
        advanced_numeric=False,
        numeric_maximum=9999,
        numeric_depth=2,
        candidate_budget=10_000_000,
    ):
        super().__init__(extensions, extension_versions, path_prefix, include_variations, include_streaming)
        self.improver_mode = ImproverMode(improver_mode)
        if compute_backend not in ('cpu', 'gpu'):
            raise ValueError('Compute backend must be cpu or gpu')
        if not (1 <= numeric_maximum <= 999_999 and 1 <= numeric_depth <= 3 and 1 <= candidate_budget <= 100_000_000_000):
            raise ValueError('Numeric search settings are outside supported bounds')
        self.compute_backend = compute_backend
        self.advanced_numeric = advanced_numeric
        self.numeric_maximum = numeric_maximum
        self.numeric_depth = numeric_depth
        self.candidate_budget = candidate_budget
        self._linked_dual_tail_values = {}
        self._linked_dual_tail_context_cache = {}
        self._linked_repeat_values = {}
        self._processed_numeric_replacement_families = None
        self._processed_power_of_two_families = None
        self._processed_linked_dual_tail_families = None
        self._processed_linked_repeat_families = None
        self._numeric_structure_values = {}
        self._numeric_structure_context_cache = {}
        self._smart_number_values_cache = {}
        self._numeric_matches_cache = {}
        self._numeric_layout_cache = {}
        self._numeric_plans = {}
        self._numeric_groups_cache = {}
        self._linked_key_cache = {}

    @property
    def search_budget(self):
        if budget_applies(self.improver_mode, self.compute_backend, self.advanced_numeric):
            return self.candidate_budget
        return None

    @property
    def uses_numeric_generation(self):
        return numeric_mode(self.improver_mode)

    def should_skip_entry(self, entry):
        return entry.decompressed_size > 50 * 1024 * 1024 or entry.decompressed_size < 100
    
    def _parse_list_path_components(self, line):
        base, extension, _ = split_file_path(line)
        return (base.rsplit('.', 1)[0], base, extension) if base else (None, None, None)

    def _rewrite_stem_prefix(self, stem):
        stem = stem.lower()
        if not stem.startswith(NATIVE_PREFIX):
            return f"{self.path_prefix}{stem.lstrip('/')}"

        parts = stem.split('/', 2)
        if len(parts) < 3:
            return f"{self.path_prefix}{parts[-1]}"

        return f"{self.path_prefix}{parts[2]}"

    def _collect_improver_entries(self, list_file_path, override_prefix=False):
        known_extensions = set(self.extensions) | set(self.extension_versions.keys())
        entries = set()
        for path in iter_list_paths(list_file_path):
            if not path.startswith(NATIVE_PREFIX):
                continue
            stem, _, extension = self._parse_list_path_components(path)
            if extension in known_extensions:
                entries.add((self._rewrite_stem_prefix(stem) if override_prefix else stem, extension))
        return entries

    def _iter_extension_suffixes(self, extension):
        versions = self.extension_versions.get(extension, set())
        if versions:
            for version in sorted(versions, key=version_sort_key):
                yield f"{extension}.{version}"
        else:
            yield extension

    def _suffix_extension(self, suffix):
        return suffix.split('.', 1)[0].lower()

    def _iter_improver_suffixes(self, source_extension):
        if self.improver_mode not in (ImproverMode.EXTENSION_SWAPS, ImproverMode.COMBINED):
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

    def _get_numeric_replacement_groups(self, stem, source_extension=None):
        key = (stem, source_extension == 'tex')
        groups = self._numeric_groups_cache.get(key)
        if groups is None:
            groups = self._numeric_groups_cache[key] = tuple(self._numeric_replacement_groups(stem, source_extension))
        return groups

    def _numeric_replacement_groups(self, stem, source_extension):
        matches = self._iter_numeric_matches(stem, source_extension)
        if not matches:
            return []

        basename_start = stem.rfind('/') + 1
        parent_end = basename_start - 1
        parent_start = stem.rfind('/', 0, parent_end) + 1 if parent_end >= 0 else 0
        parent_segment = stem[parent_start:parent_end] if parent_end >= 0 else ''
        basename = stem[basename_start:]

        parent_matches_by_span = {
            (match.start() - parent_start, match.end() - parent_start): (index, match)
            for index, match in enumerate(matches)
            if parent_start <= match.start() < parent_end
        }

        used_indices = set()
        groups = []
        for index, match in enumerate(matches):
            if index in used_indices or match.start() < basename_start:
                continue

            grouped_matches = [match]
            relative_span = (match.start() - basename_start, match.end() - basename_start)
            parent_match_info = parent_matches_by_span.get(relative_span)
            if (
                parent_match_info is not None
                and parent_segment
                and basename.startswith(parent_segment)
                and parent_match_info[1].group(0) == match.group(0)
            ):
                parent_index, parent_match = parent_match_info
                grouped_matches.insert(0, parent_match)
                used_indices.add(parent_index)

            used_indices.add(index)
            groups.append((tuple(grouped_matches), True))

        for index, match in enumerate(matches):
            if index not in used_indices:
                groups.append(((match,), False))

        groups.sort(key=lambda item: item[0][-1].start())
        return groups

    @staticmethod
    def _match_pieces(stem, matches):
        pieces, previous_end = [], 0
        for match in matches:
            pieces.append(stem[previous_end:match.start()])
            previous_end = match.end()
        pieces.append(stem[previous_end:])
        return pieces

    @staticmethod
    def _token_width(token):
        return len(token) if token.startswith('0') and len(token) > 1 else 0

    def _number_run(self, pieces, token, values):
        return len(values), (pieces, self._token_width(token), values)

    def _numeric_replacement_group_family_key(self, pieces, token):
        capped_max = min(10 ** len(token) - 1, MAX_LAST_NUMBER_VARIANTS - 1)
        return ('{N}'.join(pieces), capped_max, self._token_width(token))

    def _numeric_structure_keys(self, stem, extension):
        cache_key = (stem, extension)
        cached = self._numeric_structure_context_cache.get(cache_key)
        if cached is not None:
            return cached

        canonical_stem = self._split_tex_stem_variant(stem)[0] if extension == 'tex' else stem
        groups = self._get_numeric_replacement_groups(stem, extension)
        template = re.sub(r'\d+', lambda match: f"{{d{len(match.group(0))}}}", canonical_stem)
        keys = tuple(
            (extension, template, group_index, is_filename_group)
            for group_index, (_, is_filename_group) in enumerate(groups)
        )
        self._numeric_structure_context_cache[cache_key] = keys
        return keys

    def _build_numeric_structure_values(self, entries):
        values = defaultdict(set)
        self._numeric_structure_context_cache.clear()
        for stem, extension in entries:
            groups = self._get_numeric_replacement_groups(stem, extension)
            structure_keys = self._numeric_structure_keys(stem, extension)
            for group_index, (matches, _) in enumerate(groups):
                if group_index < len(structure_keys):
                    values[structure_keys[group_index]].add(int(matches[-1].group(0)))
        return values

    def _smart_number_values(self, token, observed_values, is_filename_group):
        observed = tuple(sorted(observed_values or (int(token),)))
        cache_key = (token, observed, is_filename_group)
        cached = self._smart_number_values_cache.get(cache_key)
        if cached is not None:
            return cached

        digit_count = len(token)
        original_value = int(token)
        max_value = (10 ** digit_count) - 1
        values = set()

        def add_range(start, end):
            values.update(range(max(0, start), min(max_value, end) + 1))

        if is_filename_group and digit_count <= 2:
            values.update(range(max_value + 1))
        elif is_filename_group:
            add_range(
                original_value - SMART_NUMERIC_LOCAL_RADIUS,
                original_value + SMART_NUMERIC_LOCAL_RADIUS
            )
            for value in observed:
                values.add(value)
                add_range(
                    value - SMART_NUMERIC_OBSERVED_RADIUS,
                    value + SMART_NUMERIC_OBSERVED_RADIUS
                )

            for first, second in zip(observed, observed[1:]):
                if second - first <= SMART_NUMERIC_GAP_LIMIT:
                    add_range(first, second)

            probe_max = min(max_value, MAX_LAST_NUMBER_VARIANTS - 1)
            if len(observed) >= SMART_NUMERIC_EXHAUSTIVE_OBSERVED_THRESHOLD:
                values.update(range(probe_max + 1))
            else:
                add_range(0, SMART_NUMERIC_BOUNDARY_SIZE - 1)
                add_range(
                    probe_max - SMART_NUMERIC_BOUNDARY_SIZE + 1,
                    probe_max
                )
                values.update(range(0, probe_max + 1, SMART_NUMERIC_PROBE_STEP))
            values.update(value for value in POWER_OF_TWO_VALUES if value <= max_value)
        else:
            add_range(
                original_value - SMART_NUMERIC_DIRECTORY_RADIUS,
                original_value + SMART_NUMERIC_DIRECTORY_RADIUS
            )
            for value in observed:
                values.add(value)
                add_range(value - 2, value + 2)

            if digit_count <= 2:
                add_range(0, min(max_value, 15))
            if original_value in POWER_OF_TWO_VALUES:
                values.update(value for value in POWER_OF_TWO_VALUES if value <= max_value)

        values.discard(original_value)
        result = tuple(sorted(values))
        self._smart_number_values_cache[cache_key] = result
        return result

    def _get_linked_dual_tail_context(self, stem, source_extension=None):
        cache_key = (stem, source_extension)
        if cache_key in self._linked_dual_tail_context_cache:
            return self._linked_dual_tail_context_cache[cache_key]

        matches = self._iter_numeric_matches(stem, source_extension)
        if len(matches) < 2:
            self._linked_dual_tail_context_cache[cache_key] = None
            return None

        first = matches[-2]
        second = matches[-1]

        if stem[first.end():second.start()] != '_':
            self._linked_dual_tail_context_cache[cache_key] = None
            return None

        first_token = first.group(0)
        second_token = second.group(0)
        if len(first_token) != 3 or len(second_token) != 2:
            self._linked_dual_tail_context_cache[cache_key] = None
            return None

        first_pattern = re.compile(rf'(?<=[_/]){re.escape(first_token)}(?=[_/]|$)')
        if len(list(first_pattern.finditer(stem))) < 2:
            self._linked_dual_tail_context_cache[cache_key] = None
            return None

        key_with_second_placeholder = f"{stem[:second.start()]}{{B}}"
        family_key = first_pattern.sub('{A}', key_with_second_placeholder)
        generation_key_with_tail = (
            f"{stem[:second.start()]}{{B}}{stem[second.end():]}"
        )
        generation_family_key = first_pattern.sub('{A}', generation_key_with_tail)

        context = {
            'first_token': first_token,
            'second_token': second_token,
            'first_pattern': first_pattern,
            'second_span': (second.start(), second.end()),
            'family_key': family_key,
            'generation_family_key': generation_family_key,
        }
        self._linked_dual_tail_context_cache[cache_key] = context
        return context

    def _build_linked_dual_tail_values(self, entries):
        values = {}
        for stem, extension in entries:
            context = self._get_linked_dual_tail_context(stem, extension)
            if not context:
                continue

            family_key = (extension, context['family_key'])
            family = values.setdefault(family_key, {'first': set(), 'second': set()})
            family['first'].add(int(context['first_token']))
            family['second'].add(int(context['second_token']))
        return values

    def _linked_dual_tail_run(self, stem, context, source_extension):
        first_token = context['first_token']
        second_token = context['second_token']
        observed = self._linked_dual_tail_values.get((source_extension, context['family_key']))
        if observed:
            first_values = sorted(observed['first'])
            second_values = sorted(observed['second'])
        else:
            first_original = int(first_token)
            first_values = range(max(0, first_original - LINKED_DUAL_TAIL_FIRST_WINDOW),
                                 min(999, first_original + LINKED_DUAL_TAIL_FIRST_WINDOW) + 1)
            second_values = self._iter_number_values(second_token)
        count = min(len(first_values) * len(second_values), MAX_LINKED_DUAL_TAIL_COMBINATIONS)
        second_start, second_end = context['second_span']
        seconds = [str(value).zfill(len(second_token)) for value in second_values]

        def stems():
            for first_value in first_values:
                with_first = context['first_pattern'].sub(str(first_value).zfill(len(first_token)), stem)
                pieces = (with_first[:second_start], with_first[second_end:])
                yield from (second.join(pieces) for second in seconds)
        return count, islice(stems(), count)

    def _iter_numeric_matches(self, stem, source_extension=None):
        key = (stem, source_extension == 'tex')
        matches = self._numeric_matches_cache.get(key)
        if matches is None:
            numeric_search_stem = self._split_tex_stem_variant(stem)[0] if key[1] else stem
            matches = self._numeric_matches_cache[key] = tuple(DIGITS.finditer(numeric_search_stem))
        return matches

    def _segment_start(self, stem, match):
        return stem.rfind('/', 0, match.start()) + 1

    def _segment_shape(self, stem, match):
        start = self._segment_start(stem, match)
        end = stem.find('/', start)
        segment = stem[start:] if end == -1 else stem[start:end]
        return re.sub(r'\d+', lambda item: f"{{d{len(item.group(0))}}}", segment)

    def _get_linked_repeat_groups(self, stem, source_extension=None):
        matches = self._iter_numeric_matches(stem, source_extension)
        if len(matches) < 2:
            return []

        matches_by_signature = defaultdict(list)
        for match in matches:
            signature = (match.group(0), match.start() - self._segment_start(stem, match))
            matches_by_signature[signature].append(match)

        groups = [
            (tuple(group_matches), group_matches[0].group(0))
            for group_matches in matches_by_signature.values()
            if len(group_matches) > 1
        ]
        groups.sort(key=lambda item: item[0][0].start())
        return groups

    def _linked_repeat_value_key(self, stem, match, extension):
        cache_key = (stem, match.start(), match.end(), extension)
        key = self._linked_key_cache.get(cache_key)
        if key is None:
            key = self._linked_key_cache[cache_key] = (
                extension,
                self._segment_shape(stem, match),
                match.start() - self._segment_start(stem, match),
                len(match.group(0)),
            )
        return key

    def _build_linked_repeat_values(self, entries):
        values = defaultdict(set)
        for stem, extension in entries:
            for match in self._iter_numeric_matches(stem, extension):
                key = self._linked_repeat_value_key(stem, match, extension)
                values[key].add(int(match.group(0)))
        return values

    def _remove_match_group_number(self, stem, matches):
        candidate = stem
        for match in reversed(matches):
            candidate = f"{candidate[:match.start() - 1]}{candidate[match.end():]}"
        return candidate

    def _linked_repeat_observed(self, stem, matches, source_extension):
        observed_values = set()
        for match in matches:
            observed_values.update(self._linked_repeat_values.get(
                self._linked_repeat_value_key(stem, match, source_extension), ()))
        return observed_values

    def _processed_families(self):
        return (self._processed_numeric_replacement_families, self._processed_linked_repeat_families,
                self._processed_linked_dual_tail_families, self._processed_power_of_two_families)

    def _numeric_layout(self, stem, source_extension):
        key = (stem, source_extension)
        layout = self._numeric_layout_cache.get(key)
        if layout is not None:
            return layout
        replacement_groups = self._get_numeric_replacement_groups(stem, source_extension)
        structure_keys = (self._numeric_structure_keys(stem, source_extension)
                          if source_extension and replacement_groups else ())
        groups = []
        for group_index, (matches, is_filename_group) in enumerate(replacement_groups):
            token = matches[-1].group(0)
            pieces = self._match_pieces(stem, matches)
            groups.append((token, is_filename_group, pieces, self._numeric_replacement_group_family_key(pieces, token),
                           structure_keys[group_index] if group_index < len(structure_keys) else None,
                           self._is_power_of_two_token(token)))
        linked = []
        for matches, token in self._get_linked_repeat_groups(stem, source_extension):
            pieces = self._match_pieces(stem, matches)
            # The same id also appears without the extra component (``gm001_000_commonparam`` and
            # ``gm001_commonparam``), so probe dropping it everywhere at once.
            removed = (self._remove_match_group_number(stem, matches)
                       if all(match.start() > 0 and stem[match.start() - 1] == '_' for match in matches) else None)
            linked.append((token, pieces, self._numeric_replacement_group_family_key(pieces, token),
                           self._linked_repeat_observed(stem, matches, source_extension), removed))
        layout = self._numeric_layout_cache[key] = (
            tuple(groups), tuple(linked), self._get_linked_dual_tail_context(stem, source_extension))
        return layout

    def _numeric_stem_plan(self, stem, family_scope, source_extension, processed):
        """Counts include stems other runs also give."""
        groups, linked, context = self._numeric_layout(stem, source_extension)
        if not groups:
            return []
        numeric_families, linked_repeat_families, dual_tail_families, power_families = processed
        runs = []

        def take(families, key):
            if families is None:
                return True
            if key in families:
                return False
            families.add(key)
            return True

        for token, is_filename_group, pieces, family, structure_key, _ in groups:
            # Smart probes depend on the source value's local window; another source may cover a different one.
            if not take(numeric_families, (family_scope, family, token, is_filename_group)):
                continue
            observed_values = None if structure_key is None else self._numeric_structure_values.get(structure_key)
            runs.append(self._number_run(pieces, token, self._smart_number_values(
                token, observed_values, is_filename_group)))

        for token, pieces, family, observed_values, removed in linked:
            if not take(linked_repeat_families, (source_extension, family_scope, family, token)):
                continue
            runs.append(self._number_run(pieces, token, self._smart_number_values(token, observed_values, True)))
            if removed is not None:
                runs.append((1, [removed]))

        if context and take(dual_tail_families, (family_scope, context['generation_family_key'])):
            runs.append(self._linked_dual_tail_run(stem, context, source_extension))

        for token, _, pieces, family, _, power in groups:
            if power and take(power_families, (family_scope, family)):
                # Smart generation is sparse, so its capped range doesn't prove every power of two in it was tested.
                runs.append(self._number_run(pieces, token, POWER_OF_TWO_VALUES))
        return runs

    def _numeric_stem_combinations(self, stem, family_scope=None, source_extension=None):
        plan = self._numeric_plans.pop((stem, source_extension), None)
        if plan is None:
            plan = self._numeric_stem_plan(stem, family_scope, source_extension, self._processed_families())
        if not plan:
            return [stem]
        return [stem, *_render_stems(stem, [run if type(run) is tuple else list(run) for _, run in plan])]

    def _split_tex_stem_variant(self, stem):
        filename = stem.lower().rsplit('/', 1)[-1]
        for length in TEX_SUFFIX_LENGTHS:
            variant = filename[-length:]
            if variant in TEX_SUFFIX_SET:
                return stem[:-length], variant
        return stem, None


    def _process_entry(self, entry, f):
        from file_handlers.pak.pakfile import _read_entry_raw

        if self.should_skip_entry(entry):
            return 0, 0
        
        try:
            buf = io.BytesIO()
            _read_entry_raw(entry, f, buf)
            data = buf.getvalue()
            
            extracted_strings = _extract_strings(data, 10)
            strings_matched = sum(self._add_reference(string, keep_found_version=True) for string in extracted_strings)
            return len(extracted_strings), strings_matched
        except Exception:
            return 0, 0
    
    def collect_from_pak_files(self, pak_directory, progress_callback=None):
        try:
            from file_handlers.pak import scan_all_pak_files, PakFile
            
            if not os.path.exists(pak_directory):
                return False, f"Directory not found: {pak_directory}", 0
            if not os.path.isdir(pak_directory):
                return False, f"Not a directory: {pak_directory}", 0
            
            if progress_callback:
                should_stop = progress_callback("Scanning for PAK files...", 0, 1)
                if should_stop:
                    return True, None, len(self.collected_paths)
            
            pak_files = scan_all_pak_files(pak_directory)
            if not pak_files:
                return False, "No .pak files found in directory", 0
            
            total_strings_extracted = 0
            total_strings_matched = 0
            readable_paks = 0
            scan_errors = []
            stopped = False
            
            for pak_idx, pak_path in enumerate(pak_files, 1):
                if stopped:
                    break
                
                pak_name = os.path.basename(pak_path)
                
                try:
                    pak = PakFile()
                    with open(pak_path, 'rb') as f:
                        pak.read_contents(f, None)
                    
                    readable_paks += 1
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
                                        int(10_000 * (pak_idx - 1 + entry_idx / total_entries) / len(pak_files)), 10_000
                                    )
                                    if should_stop:
                                        stopped = True
                                        break
                            
                            extracted, matched = self._process_entry(entry, f)
                            pak_strings_extracted += extracted
                            pak_strings_matched += matched
                    
                    total_strings_extracted += pak_strings_extracted
                    total_strings_matched += pak_strings_matched
                    print(f"PAK {pak_name}: Extracted {pak_strings_extracted} strings, matched {pak_strings_matched} with extensions")
                except Exception as e:
                    scan_errors.append(f"{pak_name}: {e}")
                    print(f"Warning: Failed to scan {pak_name}: {e}")
                    continue
            
            if not readable_paks and not stopped:
                return False, "Could not read any PAK files:\n" + '\n'.join(scan_errors), 0
            if progress_callback:
                status = "Collection stopped" if stopped else "Collection complete"
                progress_callback(status, len(pak_files), len(pak_files))
            
            print("\n=== Collection Summary ===")
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
            added = set()
            for path in iter_list_paths(list_file_path):
                if not path.startswith(NATIVE_PREFIX):
                    continue
                base, extension, _ = split_file_path(path)
                if not base:
                    continue
                added.add(path)
                added.update(f"{base}.{version}" for version in self.extension_versions.get(extension, ()))
            self.collected_paths.update(added)
            return True, None
        except Exception as error:
            return False, f"Failed to process list file: {error}"
    

    def _collect_pak_hashes(self, pak_directory, progress_callback=None):
        from file_handlers.pak import scan_all_pak_files

        if not os.path.isdir(pak_directory):
            return False, f"Not a directory: {pak_directory}", set()

        if progress_callback and progress_callback("Scanning PAK files for hashes...", 0, 1):
            return False, "PAK scan stopped", set()

        pak_files = scan_all_pak_files(pak_directory)
        if not pak_files:
            return False, "No .pak files found in directory", set()

        pak_hashes = set()
        read_errors = []

        for pak_idx, pak_path in enumerate(pak_files, 1):
            pak_name = os.path.basename(pak_path)
            if progress_callback and progress_callback(f"Scanning PAK {pak_idx}/{len(pak_files)}: {pak_name}\nCollecting file hashes...", pak_idx - 1, len(pak_files)):
                return False, "PAK scan stopped", set()

            try:
                pak_hashes.update(_read_pak_hashes(pak_path))
            except Exception as e:
                read_errors.append(f"{pak_name}: {e}")

        if read_errors:
            return False, "Cannot fully validate paths; unreadable PAK files:\n" + '\n'.join(read_errors), set()
        if not pak_hashes:
            return False, "No hashes found in PAK files", set()

        return True, None, pak_hashes

    def _family_scope(self, extension, suffix_infos):
        return (extension, suffix_infos) if self.improver_mode is ImproverMode.COMBINED else suffix_infos

    def _iter_improver_blocks(self, stem, extension, suffix_infos):
        if self.improver_mode is ImproverMode.TEX_VARIANTS:
            base = self._split_tex_stem_variant(stem)[0]
            stems = [base + ending for ending in TEX_FAMILY_ENDINGS] if extension == 'tex' else []
        elif self.uses_numeric_generation:
            stems = self._numeric_stem_combinations(
                stem, family_scope=self._family_scope(extension, suffix_infos), source_extension=extension)
        elif self.improver_mode is ImproverMode.STEM_SUFFIX_PROBES:
            stems = [stem + probe for probe in STEM_SUFFIX_PROBE_STRINGS]
        else:
            stems = [stem]

        dotted = ['.' + suffix for suffix, _ in suffix_infos]
        if self.improver_mode is ImproverMode.COMBINED:
            textures = tuple(target_extension == 'tex' for _, target_extension in suffix_infos)
            group, ending = [], ''
            for candidate_stem in stems:
                own = self._own_ending(candidate_stem, extension)
                if group and (own != ending or len(group) >= STEMS_PER_BLOCK):
                    yield CombinedStems(group, ending, textures), dotted, False
                    group = []
                ending = own
                group.append(candidate_stem)
            if group:
                yield CombinedStems(group, ending, textures), dotted, False
        else:
            for start in range(0, len(stems), STEMS_PER_BLOCK):
                yield stems[start:start + STEMS_PER_BLOCK], dotted, False

    def _candidate_estimate(self, stem, extension, suffix_info_cache, processed):
        """Must match what ``_iter_mode_blocks`` makes, before dedup."""
        if self.improver_mode is ImproverMode.TEX_MDF2_MESH_SWAPS:
            return sum(len(TEX_FAMILY_ENDINGS) if target == 'tex' else 1
                       for _, target in self._get_tex_mdf2_mesh_suffix_infos(extension, suffix_info_cache))
        suffix_infos = self._get_improver_suffix_infos(extension, suffix_info_cache)
        if self.improver_mode is ImproverMode.TEX_VARIANTS:
            stems = len(TEX_FAMILY_ENDINGS) if extension == 'tex' else 0
        elif self.uses_numeric_generation:
            plan = self._numeric_stem_plan(stem, self._family_scope(extension, suffix_infos), extension, processed)
            self._numeric_plans[stem, extension] = plan
            stems = 1 + sum(count for count, _ in plan)
        elif self.improver_mode is ImproverMode.STEM_SUFFIX_PROBES:
            stems = len(STEM_SUFFIX_PROBE_STRINGS)
        else:
            stems = 1
        if self.improver_mode is not ImproverMode.COMBINED:
            return stems * len(suffix_infos)
        key = ('combined', extension, extension == 'tex' and self._split_tex_stem_variant(stem)[1] is not None)
        per_stem = suffix_info_cache.get(key)
        if per_stem is None:
            per_stem = suffix_info_cache[key] = sum(
                len(self._iter_combined_stems(stem, extension, target)) for _, target in suffix_infos)
        return stems * per_stem

    def _own_ending(self, stem, source_extension):
        return stem[len(self._split_tex_stem_variant(stem)[0]):] if source_extension == 'tex' else ''

    def _iter_combined_stems(self, stem, source_extension, target_extension):
        return combined_heads(stem, self._own_ending(stem, source_extension), target_extension == 'tex')

    def _get_improver_suffix_infos(self, extension, suffix_info_cache):
        suffix_infos = suffix_info_cache.get(extension)
        if suffix_infos is None:
            suffix_infos = tuple(
                (suffix, self._suffix_extension(suffix))
                for suffix in self._iter_improver_suffixes(extension)
            )
            suffix_info_cache[extension] = suffix_infos
        return suffix_infos

    def _get_tex_mdf2_mesh_suffix_infos(self, source_extension, suffix_info_cache):
        cache_key = ('tex_mdf2_mesh', source_extension)
        suffix_infos = suffix_info_cache.get(cache_key)
        if suffix_infos is not None:
            return suffix_infos

        known_extensions = set(self.extensions) | set(self.extension_versions.keys())
        suffix_infos = []
        yielded = set()
        if source_extension in TEX_MDF2_MESH_SWAP_EXTENSIONS:
            for target_extension in TEX_MDF2_MESH_SWAP_EXTENSIONS:
                if target_extension == source_extension or target_extension not in known_extensions:
                    continue
                for suffix in self._iter_extension_suffixes(target_extension):
                    if suffix in yielded:
                        continue
                    yielded.add(suffix)
                    suffix_infos.append((suffix, self._suffix_extension(suffix)))

        suffix_infos = tuple(suffix_infos)
        suffix_info_cache[cache_key] = suffix_infos
        return suffix_infos

    def _iter_tex_mdf2_mesh_candidates(self, stem, extension, suffix_infos):
        base_stem = self._split_tex_stem_variant(stem)[0] if extension == 'tex' else stem
        for suffix, suffix_extension in suffix_infos:
            for ending in TEX_FAMILY_ENDINGS if suffix_extension == 'tex' else ('',):
                yield f"{base_stem}{ending}.{suffix}"

    def _iter_mode_blocks(self, stem, extension, suffix_info_cache):
        if self.improver_mode is ImproverMode.TEX_MDF2_MESH_SWAPS:
            suffix_infos = self._get_tex_mdf2_mesh_suffix_infos(extension, suffix_info_cache)
            paths = list(self._iter_tex_mdf2_mesh_candidates(stem, extension, suffix_infos))
            if paths:
                yield paths, [''], False
            return

        suffix_infos = self._get_improver_suffix_infos(extension, suffix_info_cache)
        yield from self._iter_improver_blocks(stem, extension, suffix_infos)


    def prepare_search(self, list_file_path, pak_directory, progress_callback=None):
        """The caller runs or ``close``s the returned ``Search``."""
        try:
            if not os.path.exists(list_file_path):
                return None, (False, f"List file not found: {list_file_path}", None, set())

            override_prefix = self.improver_mode is ImproverMode.CROSS_GAME_VERSION_SWAPS
            entries = sorted(self._collect_improver_entries(list_file_path, override_prefix=override_prefix))

            if not entries:
                return None, (False, "No valid list entries found to improve", None, set())

            stopped = False
            def scan_progress(message, current, total):
                nonlocal stopped
                stopped = stopped or bool(progress_callback and progress_callback(
                    message, int(500 * current / max(1, total)), 10_000))
                return stopped
            ok, error, pak_hashes = self._collect_pak_hashes(pak_directory, scan_progress)
            if stopped:
                return None, (True, None, dict(source_entries=len(entries), generated_candidates=0,
                                               validated_paths=0, stopped=True), set())
            if not ok:
                return None, (False, error, None, set())

            named = {filepath_hash(path) for path in iter_list_paths(list_file_path)}
            search = Search(self, entries, pak_hashes, unfound=len(pak_hashes - named))
            result = search.prepare(progress_callback)
            if result is not None:
                search.close()
                return None, result
            return search, None
        except Exception as e:
            return None, (False, f"List improver error: {e}", None, set())

    def improve_list_with_chunked_validation(self, list_file_path, pak_directory, progress_callback=None):
        search, result = self.prepare_search(list_file_path, pak_directory, progress_callback)
        return result if search is None else search.run(progress_callback)

    def _sampling_copy(self):
        copy = object.__new__(type(self))
        copy.__dict__.update(self.__dict__)
        copy.collected_paths = set()
        copy._numeric_plans = {}
        for name in ('_processed_numeric_replacement_families', '_processed_power_of_two_families',
                     '_processed_linked_dual_tail_families', '_processed_linked_repeat_families'):
            setattr(copy, name, set() if self.uses_numeric_generation else None)
        return copy

    def validate_paths_against_paks(self, pak_directory, progress_callback=None):
        try:
            ok, error, pak_hashes = self._collect_pak_hashes(pak_directory, progress_callback)
            if not ok:
                return False, error, set()

            paths_to_validate = self.paths()

            if progress_callback:
                progress_callback("Validating collected paths...", 0, len(paths_to_validate))

            validated_paths = set()
            total_paths = len(paths_to_validate)

            for idx, path in enumerate(sorted(paths_to_validate), 1):
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
