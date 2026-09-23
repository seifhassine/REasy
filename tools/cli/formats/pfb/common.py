"""Shared native PFB loading and effect-element snapshots."""
import argparse
from types import SimpleNamespace
from file_handlers.rsz.rsz_handler import RszHandler
from file_handlers.rsz.rsz_data_types import ArrayData, ObjectData, UserDataData
from tools.cli.runtime import MHR_REGISTRY

ROOT_TYPE = 'via.effect.script.EPVStandardData'


def open_pfb(path, data=None):
    handler = RszHandler()
    handler.game_version = 'MHRise'
    handler.app = SimpleNamespace(settings={'rcol_json_path': str(MHR_REGISTRY)}, _rsz_type_registry_override=None)
    handler.filepath = str(path)
    handler.init_type_registry()
    handler.read(path.read_bytes() if data is None else data)
    return handler


def scalar_value(field):
    for attribute in ('value', 'guid_str', 'raw_bytes'):
        if hasattr(field, attribute):
            return getattr(field, attribute)
    if all(hasattr(field, attribute) for attribute in ('x', 'y', 'z')):
        return (round(float(field.x), 4), round(float(field.y), 4), round(float(field.z), 4))
    if all(hasattr(field, attribute) for attribute in ('r', 'g', 'b', 'a')):
        return (field.r, field.g, field.b, field.a)
    return repr(field)


def snapshot(rsz, instance_id, depth=2):
    out = {}
    for name, field in (rsz.parsed_elements.get(instance_id) or {}).items():
        if isinstance(field, (ObjectData, UserDataData)):
            out[name] = snapshot(rsz, field.value, depth - 1) if field.value and depth else f'ref:{field.value}'
        elif isinstance(field, ArrayData):
            items = []
            for item in field.values:
                if isinstance(item, (ObjectData, UserDataData)) and getattr(item, 'value', 0):
                    items.append(snapshot(rsz, item.value, depth - 1) if depth else f'ref:{item.value}')
                else:
                    items.append(scalar_value(item))
            out[name] = items
        else:
            out[name] = scalar_value(field)
    return out


def find_root(rsz, registry):
    for instance in range(1, len(rsz.instance_infos)):
        definition = registry.get_type_info(rsz.instance_infos[instance].type_id)
        if definition and definition['name'] == ROOT_TYPE:
            return instance
    raise SystemExit(f'{ROOT_TYPE} not found')


def triple(text):
    parts = [float(value) for value in text.split(',')]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError('expected X,Y,Z')
    return parts
