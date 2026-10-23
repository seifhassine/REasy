"""Inspect the native EFX container and attributes using its file version."""
import json
from pathlib import Path

from file_handlers.efx.efx_file import EfxFile, EfxRecord
from file_handlers.efx.mhw_efx_file import MhwEfxFile
from file_handlers.efx.format_registry import read_efx
from file_handlers.efx.editing import EfxEditor, empty_file, references
from tools.cli.runtime import EditResult, mutation_parser, read_parser


def record_data(value):
    if isinstance(value, EfxFile):
        return document_data(value)
    if isinstance(value, EfxRecord):
        result = {name: record_data(item.value) for name, item in value.fields.items()}
        if value.name is not None:
            result['name'] = value.name
        return result
    if isinstance(value, (tuple, list)):
        return [record_data(item) for item in value]
    return value


def document_data(document):
    if isinstance(document, MhwEfxFile):
        return document.document
    def entry_data(position, entry):
        return {"position": position, "index": entry.index, "name": entry.name, "assignment": entry.assignment,
                "attributes": [{"position": ai, "type_id": attribute.type_id, "type": attribute.name,
                                "unique_id": attribute.unique_id,
                                "parent_bone": attribute.parent_bone.name if attribute.parent_bone else None,
                                "fields": record_data(attribute.body)} for ai, attribute in enumerate(entry.attributes)]}

    return {"format": "RE_EFX", "version": document.version, "dimension": document.header[1],
            "header_u32": [value & 0xFFFFFFFF for value in document.header],
            "bones": [{"name": bone.name, "hash": bone.name_hash, "value": bone.value} for bone in document.bones],
            "expression_parameters": record_data(document.expression_parameters),
            "field_parameters": record_data(document.field_parameters),
            "entries": [entry_data(i, entry) for i, entry in enumerate(document.entries)],
            "actions": [entry_data(i, entry) for i, entry in enumerate(document.actions)],
            "groups": record_data(document.groups), "user_variables": record_data(document.uvar_groups)}


def query(args):
    if args.game_dir:
        from file_handlers.motion.motlist_handler import MotListHandler
        from file_handlers.motion.preview.mhr_assets import MhrPreviewAssets, preview_context
        assets = MhrPreviewAssets(preview_context(MotListHandler(), args.game_dir))
        source, data = assets.resource(str(args.source).replace('\\', '/'))
        if not data:
            raise ValueError(f'Game resource not found: {args.source}')
    else:
        source, data = str(args.source.resolve()), args.source.read_bytes()
    document = read_efx(data, source)
    return {"source": source, "size": len(data), **document_data(document)}


def register_edits(commands):
    from .efx_sequence import configure
    configure(commands)
    channel_commands = ('curve-set', 'curve-delete', 'expression-set', 'expression-delete')
    for command in ('set', 'insert', 'copy', 'delete', 'move', 'replace', 'parameter-value', 'material-value', 'import', 'preset-export', 'preset-import', 'apply', 'new', *channel_commands):
        parser = mutation_parser(commands, command, f'{command.title()} EFX data with verified structural editing')
        if command in ('set', 'insert', 'copy', 'delete', 'move', 'replace', 'parameter-value', 'material-value', 'import', 'preset-import', *channel_commands):
            parser.add_argument('--path', required=True, help='list-position path, e.g. entries/0/attributes/1/fields/LocalPosition')
        if command in ('set', 'insert', 'replace', 'parameter-value', 'material-value', 'curve-set', 'expression-set'):
            parser.add_argument('--value', required=True, type=json.loads, help='JSON scalar, vector, or object')
        if command in ('insert', 'copy', 'move', 'import', 'preset-import'):
            parser.add_argument('--index', type=int, required=command == 'move')
        if command in ('copy', 'import', 'preset-export'):
            parser.add_argument('--from', dest='copy_source', required=True, help='source path in this EFX document')
        if command in ('copy', 'import', 'preset-import'):
            parser.add_argument('--name', help='name for the new entry, group, or bone')
        if command in ('import', 'preset-import'):
            parser.add_argument('--donor', type=Path, required=True)
            parser.add_argument('--conflicts', choices=('rename', 'error'), default='rename')
            parser.set_defaults(extra_inputs=lambda args: (args.donor,))
        if command == 'apply':
            parser.add_argument('--plan', type=Path, required=True, help='JSON array of EFX operation objects')
            parser.set_defaults(extra_inputs=plan_inputs)
        if command == 'new':
            parser.add_argument('--dimension', type=int, choices=(0, 1), default=1)
        if command in channel_commands:
            parser.add_argument('--channel', type=int, required=True, help='zero-based channel bit or material slot')
            parser.add_argument('--indices', type=json.loads, help='explicit native material indices table')
        if command.startswith('expression-'):
            parser.add_argument('--material', action='store_true')
            parser.add_argument('--headers', type=json.loads, help='explicit shader-specific material header values')
        if command.startswith('curve-'):
            parser.add_argument('--bits', type=json.loads, help='explicit material curve flag words')
        parser.set_defaults(run=edit)
    parser = read_parser(commands, 'references', 'Inspect EFX group membership and particle action references')
    parser.set_defaults(inspect=query_references)


def load_editable(path):
    document = read_efx(path.read_bytes(), str(path))
    if not isinstance(document, EfxFile):
        raise ValueError('EFX editing is currently available for RE Engine containers, not MHW')
    return document


def plan_inputs(args):
    operations = json.loads(args.plan.read_text(encoding='utf-8-sig'))
    paths = [args.plan]
    if not isinstance(operations, list) or not all(isinstance(op, dict) for op in operations):
        raise ValueError('An EFX plan must contain operation objects')
    paths.extend(args.plan.parent / op['donor'] for op in operations if 'donor' in op)
    return paths


def query_references(args):
    document = load_editable(args.source)
    return {'source': str(args.source.resolve()), 'version': document.version, 'references': references(document)}


def edit(args):
    document = load_editable(args.source)
    if args.command == 'preset-export':
        from file_handlers.efx.templates import export_template
        return EditResult(export_template(document, args.copy_source).encode('utf-8'), {'version': document.version, 'source_path': args.copy_source})
    if args.command == 'new':
        return EditResult(empty_file(document.version, args.dimension).build_validated(), {'version': document.version})
    editor = EfxEditor(document)
    if args.command == 'apply':
        operations = json.loads(args.plan.read_text(encoding='utf-8-sig'))
        if not isinstance(operations, list) or not operations or not all(isinstance(op, dict) for op in operations):
            raise ValueError('An EFX plan must be a nonempty array of operation objects')
    else:
        op = {'op': args.command, 'path': args.path}
        for key in ('value', 'index', 'name', 'conflicts', 'channel', 'indices', 'material', 'bits', 'headers'):
            value = getattr(args, key, None)
            if value is not None:
                op[key] = value
        if args.command in ('copy', 'import'):
            op['source'] = args.copy_source
        if args.command in ('import', 'preset-import'):
            op['donor'] = str(args.donor.resolve())
        operations = [op]
    results = []
    for original in operations:
        operation = dict(original)
        donor = None
        if operation['op'] in ('import', 'preset-import'):
            path = Path(operation.pop('donor'))
            if not path.is_absolute() and args.command == 'apply':
                path = args.plan.parent / path
            if operation['op'] == 'preset-import':
                from file_handlers.efx.templates import import_template
                donor, source = import_template(path.read_text(encoding='utf-8-sig'))
                operation.update(op='import', source=source)
            else:
                donor = load_editable(path)
        results.append(editor.apply(operation, donor=donor))
    return EditResult(editor.document.build_validated(), {'version': document.version, 'operations': operations, 'paths': results})
