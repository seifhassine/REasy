"""Dump an RE Engine PFB/SCN/USR (RSZ) file: type histogram, effect structures and string search.

usage: python dump_pfb.py FILE [FILE ...] [--strings REGEX] [--depth N] [--max N] [-o OUT]
"""
import argparse
import os
import re
import sys
from collections import Counter
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT)]

from file_handlers.rsz.rsz_data_types import ArrayData, ObjectData, UserDataData     # noqa: E402
from file_handlers.rsz.rsz_file import RszFile                                        # noqa: E402
from utils.type_registry import TypeRegistry                                          # noqa: E402

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('files', nargs='+', type=Path)
parser.add_argument('--registry', default=str(ROOT / 'resources/data/dumps/rszmhrise.json'))
parser.add_argument('--strings', default=r'\d+-\d+', help='regex searched in string fields (default: id-like strings)')
parser.add_argument('--depth', type=int, default=3)
parser.add_argument('--max', type=int, default=40, help='max instances shown per type')
parser.add_argument('-o', '--output', type=Path)
args = parser.parse_args()
registry = TypeRegistry(args.registry)
pattern = re.compile(args.strings)
lines = []
out = lines.append


def type_name(rsz, instance_id):
    definition = rsz.type_registry.get_type_info(rsz.instance_infos[instance_id].type_id)
    return definition.get('name') if definition else f'<type {rsz.instance_infos[instance_id].type_id}>'


def describe(rsz, instance_id, depth, seen):
    fields = rsz.parsed_elements.get(instance_id)
    if fields is None:
        return f'#{instance_id} <no fields>'
    prefix = type_name(rsz, instance_id)
    parts = []
    for name, field in fields.items():
        if isinstance(field, (ObjectData, UserDataData)):
            if depth <= 0 or not getattr(field, 'value', 0):
                parts.append(f'{name}=->#{getattr(field, "value", 0)}')
            elif field.value in seen:
                parts.append(f'{name}=->#{field.value}(seen)')
            else:
                seen = seen | {field.value}
                parts.append(f'{name}=>{{ ' + describe(rsz, field.value, depth - 1, seen) + ' }')
        elif isinstance(field, ArrayData):
            values = [getattr(item, 'value', item) for item in field.values]
            elements = getattr(field, 'element_class', None)
            parts.append(f'{name}[{len(values)}]' + (f'={elements.__name__}' if elements else '')
                         + (f' {values[:6]}' if values and not isinstance(values[0], int) else f' {values[:6]}'))
        else:
            parts.append(f'{name}={getattr(field, "value", None)!r}')
    return f'#{instance_id} {prefix} ' + ', '.join(parts)


for path in args.files:
    data = path.read_bytes()
    rsz = RszFile()
    rsz.filepath = str(path)
    rsz.type_registry = registry
    rsz.read(data)
    counts = Counter(type_name(rsz, i) for i in range(1, len(rsz.instance_infos)))
    out(f'\n==== {path.name}  {len(data):,} B  instances={len(rsz.instance_infos) - 1} objects={len(rsz.object_table)}')
    out('  type histogram:')
    for name, count in counts.most_common(18):
        out(f'    {count:4d}  {name}')
    interesting = [i for i in range(1, len(rsz.instance_infos))
                   if pattern.search(json_safe := str(path)) or True]
    # string/field search
    hits = []
    for instance in range(1, len(rsz.instance_infos)):
        for name, field in (rsz.parsed_elements.get(instance) or {}).items():
            values = []
            if isinstance(field, ArrayData):
                values = [getattr(item, 'value', item) for item in field.values]
            elif not isinstance(field, (ObjectData, UserDataData)):
                values = [getattr(field, 'value', None)]
            for value in values:
                if isinstance(value, str) and pattern.search(value):
                    hits.append((instance, name, value))
    out(f'  string fields matching /{args.strings}/: {len(hits)}')
    for instance, name, value in hits[:30]:
        out(f'    #{instance} {type_name(rsz, instance)}.{name} = {value!r}')
    # effect structures
    for base in ('effect', 'Effect'):
        instances = [i for i in range(1, len(rsz.instance_infos)) if base in type_name(rsz, i)]
        if not instances:
            continue
        out(f'  instances whose type contains {base!r}: {len(instances)}')
        for instance in instances[:args.max]:
            out('    ' + describe(rsz, instance, args.depth, frozenset()))
        if len(instances) > args.max:
            out(f'    … {len(instances) - args.max} more')

text = '\n'.join(lines)
if args.output:
    args.output.write_text(text, encoding='utf-8')
    print(f'wrote {args.output} ({len(lines)} lines)')
else:
    print(text)
