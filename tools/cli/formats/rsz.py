"""Structured RSZ inspection without presentation-layer parsing."""
from collections import Counter
from pathlib import Path

from file_handlers.rsz.inspection import fields_record, instance_records, load_rsz, type_name
from file_handlers.rsz.scalar_editing import set_scalar_fields
from utils.type_registry import TypeRegistry
from tools.cli.runtime import integer
from tools.cli.runtime import MHR_REGISTRY


def load(path, registry):
    return load_rsz(path, registry)


def configure_query(parser):
    parser.add_argument('--registry', type=Path, default=MHR_REGISTRY)
    parser.add_argument('--class', dest='klass', help='class name substring')
    parser.add_argument('--type-id', type=integer)
    parser.add_argument('--instance', type=integer)
    parser.add_argument('--field', help='exact field name')
    parser.add_argument('--strings', help='regular expression matched against string values')
    parser.add_argument('--limit', type=integer, default=40)


def records(rsz, args, enums):
    if args.limit < 0:
        raise ValueError('--limit must be nonnegative')
    return instance_records(rsz, enums, class_contains=args.klass, type_id=args.type_id, instance=args.instance,
                            field=args.field, strings=args.strings)


def query(args):
    rsz = load(args.source, TypeRegistry(str(args.registry)))
    enums = {}
    found = records(rsz, args, enums)
    return {'source': str(args.source.resolve()), 'instance_count': len(rsz.instance_infos),
            'object_table': list(rsz.object_table),
            'types': dict(Counter(type_name(rsz, i) for i in range(1, len(rsz.instance_infos)))),
            'matched': len(found), 'instances': found[:args.limit], 'enums': enums}


def scan(args):
    from tools.rsz_field_value_finder import is_rsz_path
    if args.source.is_file():
        paths = [args.source]
    elif args.source.is_dir():
        paths = sorted(p for p in (args.source.rglob('*') if args.recursive else args.source.iterdir())
                       if p.is_file() and is_rsz_path(p))
    else:
        raise ValueError(f'Input does not exist: {args.source}')
    registry = TypeRegistry(str(args.registry))
    matched, found, enums = 0, [], {}
    for path in paths:
        rows = records(load(path, registry), args, enums)
        matched += len(rows)
        found.extend(dict(row, file=str(path.resolve())) for row in rows[:max(0, args.limit - len(found))])
    return {'source': str(args.source.resolve()), 'files_scanned': len(paths), 'matched': matched, 'instances': found, 'enums': enums}


def elements(args):
    rsz = load(args.source, TypeRegistry(str(args.registry)))
    roots = [i for i in rsz.parsed_elements if type_name(rsz, i) == 'via.effect.script.EPVStandardData']
    if len(roots) != 1:
        raise ValueError(f'Expected one effect container, found {len(roots)}')
    rows, enums = [], {}
    for position, item in enumerate(rsz.parsed_elements[roots[0]]['Elements'].values):
        fields = rsz.parsed_elements[item.value]
        if args.element_id is None or fields['ID'].value == args.element_id:
            rows.append({'position': position, 'instance': item.value, 'id': fields['ID'].value,
                         'fields': fields_record(fields, rsz.type_registry, enums)})
    return {'source': str(args.source.resolve()), 'root': roots[0], 'elements': rows, 'enums': enums}


def references(args):
    from collections import defaultdict
    from tools.pfb_refinfos_extractor import iter_pfb_like_files, build_property_map_for_file, _serialize_mapping
    registry = TypeRegistry(str(args.registry))
    if not args.source.exists():
        raise ValueError(f'Input does not exist: {args.source}')
    mapping = defaultdict(lambda: defaultdict(lambda: {'files': set(), 'array_ids': set()}))
    files = iter_pfb_like_files(str(args.source))
    for path in files:
        for name, property_id, array_index in build_property_map_for_file(path, registry):
            mapping[name][property_id]['files'].add(path)
            mapping[name][property_id]['array_ids'].add(array_index)
    return {'source': str(args.source.resolve()), 'files_scanned': len(files), 'references': _serialize_mapping(mapping)}


def field_edit(args):
    rsz = load(args.source, TypeRegistry(str(args.registry)))
    return set_scalar_fields(rsz, args.instance, args.assignments)
