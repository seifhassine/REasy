"""Resolve RCOL request IDs through their native object-table slots."""
from file_handlers.rcol.rcol_handler import RcolHandler
from ..rsz import fields_record
from utils.hex_util import guid_le_to_str
from .selection import select


def run(args):
    handler = RcolHandler()
    handler.filepath = str(args.source)
    handler.init_type_registry(str(args.registry))
    handler.read(args.source.read_bytes())
    rcol, rows = handler.rcol, []
    for index in select(rcol, args):
        request = rcol.request_sets[index]
        info = request.info
        group = rcol.groups[info.group_index]
        rows.append({'index': index, 'id': info.id, 'field0': info.field0, 'name': info.name,
                     'group_index': info.group_index, 'group_name': group.info.name,
                     'shape_offset': info.shape_offset,
                     'fields': fields_record(rcol.rsz.parsed_elements[rcol.rsz.object_table[index]]),
                     'shapes': [{'name': shape.info.name, 'guid': guid_le_to_str(shape.info.guid),
                                 'joint': shape.info.primary_joint_name_str} for shape in group.shapes]})
    return {'source': str(args.source.resolve()), 'request_count': len(rcol.request_sets), 'requests': rows}
