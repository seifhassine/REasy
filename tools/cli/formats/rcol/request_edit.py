"""Set scalar parameters on one RCOL request set (in place, fixed-size fields only).

"""
import argparse
import contextlib
import io
from pathlib import Path


from file_handlers.rcol.rcol_handler import RcolHandler
from file_handlers.rsz.rsz_data_types import ArrayData, ObjectData, UserDataData

from tools.cli.runtime import MHR_REGISTRY as REGISTRY
from .selection import configure as configure_selection, resolve


def assignment(text):
    name, separator, value = text.partition('=')
    if not separator or not name:
        raise argparse.ArgumentTypeError('Use FIELD=VALUE')
    try:
        return name, int(value, 0)
    except ValueError:
        return name, float(value)


def configure(parser):
    configure_selection(parser, required=True)
    parser.add_argument('--set', dest='assignments', action='append', type=assignment, required=True, metavar='FIELD=VALUE')


def run(args):
    SOURCE = args.source.resolve()
    source_bytes = SOURCE.read_bytes()

    handler = RcolHandler()
    handler.filepath = str(SOURCE)
    handler.init_type_registry(str(REGISTRY))
    handler.read(source_bytes)
    rcol, rsz = handler.rcol, handler.rcol.rsz
    index = resolve(rcol, args)
    instance = rsz.object_table[index]
    fields = rsz.parsed_elements[instance]
    print(f'request id {args.request_id} (table index {index}) instance #{instance} name={rcol.request_sets[index].info.name!r}')
    for name, value in args.assignments:
        field = fields.get(name)
        assert field is not None, f'{name} is not a field of this userdata'
        assert hasattr(field, 'value') and not isinstance(field, (ObjectData, UserDataData, ArrayData)), name
        print(f'  {name}: {field.value!r} -> {value!r}')
        field.value = value
    with contextlib.redirect_stdout(io.StringIO()):
        output = handler.rebuild()

    reopened = RcolHandler()
    reopened.filepath = str(SOURCE)
    reopened.init_type_registry(str(REGISTRY))
    reopened.read(output)
    assert reopened.rebuild() == output, 'not stable'
    new_fields = reopened.rcol.rsz.parsed_elements[reopened.rcol.rsz.object_table[index]]
    for name, value in args.assignments:
        assert new_fields[name].value == value, (name, new_fields[name].value)
        print(f'  verified {name} = {new_fields[name].value!r}')
    # nothing else may move: compare every other request's raw userdata bytes through the resolver
    changed = 0
    for other, request in enumerate(reopened.rcol.request_sets):
        before = rsz.parsed_elements[rsz.object_table[other]]
        after = reopened.rcol.rsz.parsed_elements[reopened.rcol.rsz.object_table[other]]
        for name in before:
            if other == index and name in {item[0] for item in args.assignments}:
                continue
            old, new = before[name], after[name]
            if isinstance(old, (ObjectData, UserDataData)):
                assert old.value == new.value, (request.info.id, name)
            elif isinstance(old, ArrayData):
                assert len(old.values) == len(new.values), (request.info.id, name)
            else:
                assert old.value == new.value, (request.info.id, name)
            changed += 1
    print(f'parameters compared across all {len(reopened.rcol.request_sets)} request sets: only the requested fields moved')


    return bytes(output)
