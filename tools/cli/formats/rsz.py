"""Structured RSZ inspection without presentation-layer parsing."""
from collections import Counter
from pathlib import Path
import re
import struct

from file_handlers.rsz.rsz_file import RszFile
from file_handlers.rsz.rsz_data_types import ArrayData, ObjectData, UserDataData, F32Data
from utils.type_registry import TypeRegistry
from tools.fsm.common import integer
from tools.cli.runtime import MHR_REGISTRY


def scalar(field):
    if isinstance(field, ArrayData):
        return [scalar(item) for item in field.values]
    for attribute in ('value', 'guid_str'):
        if hasattr(field, attribute):
            value = getattr(field, attribute)
            return value.rstrip('\0') if isinstance(value, str) else value
    if hasattr(field, 'raw_bytes'):
        return field.raw_bytes.hex()
    from file_handlers.rsz.utils.rsz_field_utils import VALUE_COMPONENTS
    components = VALUE_COMPONENTS.get(type(field).__name__)
    if components:
        return {name: getattr(field, name) for name in components}
    raise ValueError(f'No structured value representation for {type(field).__name__}')


def load(path, registry):
    rsz = RszFile()
    rsz.filepath = str(path)
    rsz.type_registry = registry
    rsz.read(path.read_bytes(), validate_type_registry=True)
    return rsz


def type_name(rsz, index):
    type_id = rsz.instance_infos[index].type_id
    definition = rsz.type_registry.get_type_info(type_id)
    return definition['name'] if definition else f'0x{type_id:08X}'


def fields_record(fields):
    return {name: {'type': type(value).__name__, 'value': scalar(value),
                   'reference': isinstance(value, (ObjectData, UserDataData)) or
                   (isinstance(value, ArrayData) and value.element_class in (ObjectData, UserDataData))}
            for name, value in fields.items()}


def configure_query(parser):
    parser.add_argument('--registry', type=Path, default=MHR_REGISTRY)
    parser.add_argument('--class', dest='klass', help='class name substring')
    parser.add_argument('--type-id', type=integer)
    parser.add_argument('--instance', type=integer)
    parser.add_argument('--field', help='exact field name')
    parser.add_argument('--strings', help='regular expression matched against string values')
    parser.add_argument('--limit', type=integer, default=40)


def records(rsz, args):
    if args.limit < 0:
        raise ValueError('--limit must be nonnegative')
    pattern = re.compile(args.strings) if args.strings else None
    found = []
    for index in range(1, len(rsz.instance_infos)):
        name = type_name(rsz, index)
        if args.klass and args.klass.casefold() not in name.casefold():
            continue
        if args.type_id is not None and rsz.instance_infos[index].type_id != args.type_id:
            continue
        if args.instance is not None and args.instance != index:
            continue
        fields = rsz.parsed_elements.get(index, {})
        if args.field:
            fields = {k: v for k, v in fields.items() if k == args.field}
            if not fields:
                continue
        row = fields_record(fields)
        if pattern:
            row = {k: v for k, v in row.items() if any(isinstance(item, str) and pattern.search(item)
                   for item in (v['value'] if isinstance(v['value'], list) else [v['value']]))}
            if not row:
                continue
        found.append({'instance': index, 'class': name, 'type_id': f'0x{rsz.instance_infos[index].type_id:08X}',
                      'fields': row})
    return found


def query(args):
    rsz = load(args.source, TypeRegistry(str(args.registry)))
    found = records(rsz, args)
    return {'source': str(args.source.resolve()), 'instance_count': len(rsz.instance_infos),
            'object_table': list(rsz.object_table),
            'types': dict(Counter(type_name(rsz, i) for i in range(1, len(rsz.instance_infos)))),
            'matched': len(found), 'instances': found[:args.limit]}


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
    matched, found = 0, []
    for path in paths:
        rows = records(load(path, registry), args)
        matched += len(rows)
        found.extend(dict(row, file=str(path.resolve())) for row in rows[:max(0, args.limit - len(found))])
    return {'source': str(args.source.resolve()), 'files_scanned': len(paths), 'matched': matched, 'instances': found}


def elements(args):
    rsz = load(args.source, TypeRegistry(str(args.registry)))
    roots = [i for i in rsz.parsed_elements if type_name(rsz, i) == 'via.effect.script.EPVStandardData']
    if len(roots) != 1:
        raise ValueError(f'Expected one effect container, found {len(roots)}')
    rows = []
    for position, item in enumerate(rsz.parsed_elements[roots[0]]['Elements'].values):
        fields = rsz.parsed_elements[item.value]
        if args.element_id is None or fields['ID'].value == args.element_id:
            rows.append({'position': position, 'instance': item.value, 'id': fields['ID'].value,
                         'fields': fields_record(fields)})
    return {'source': str(args.source.resolve()), 'root': roots[0], 'elements': rows}


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
    registry = TypeRegistry(str(args.registry))
    rsz = load(args.source, registry)
    if args.instance not in rsz.parsed_elements:
        raise ValueError(f'Unknown instance: {args.instance}')
    fields = rsz.parsed_elements[args.instance]
    for assignment in args.assignments:
        name, separator, text = assignment.partition('=')
        name = name.strip()
        if not separator or name not in fields:
            raise ValueError(f'Expected an existing FIELD=VALUE, got {assignment!r}')
        item = fields[name]
        if isinstance(item, (ArrayData, ObjectData, UserDataData)) or not hasattr(item, 'value'):
            raise ValueError(f'{name} is not an editable scalar')
        value = item.value
        if isinstance(value, bool):
            if text.casefold() not in ('true', 'false'):
                raise ValueError(f'{name} expects true or false')
            item.value = text.casefold() == 'true'
        elif isinstance(value, int):
            item.value = integer(text)
        elif isinstance(value, float):
            item.value = float(text)
            if isinstance(item, F32Data):
                item.value = struct.unpack('<f', struct.pack('<f', item.value))[0]
        elif isinstance(value, str):
            item.value = text
        else:
            raise ValueError(f'Unsupported scalar type for {name}')
    expected = {index: fields_record(values) for index, values in rsz.parsed_elements.items()}
    output = rsz.build_validated()
    reopened = RszFile()
    reopened.filepath = str(args.source)
    reopened.type_registry = registry
    reopened.read(output, validate_type_registry=True)
    actual = {index: fields_record(values) for index, values in reopened.parsed_elements.items()}
    if actual != expected or reopened.object_table != rsz.object_table:
        raise ValueError('Reopened RSZ graph differs from the authored fields')
    return output
