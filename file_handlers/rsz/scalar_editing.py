"""Scalar field edits on a loaded RSZ graph, verified by reopening the candidate."""
import struct

from file_handlers.rsz.inspection import fields_record
from file_handlers.rsz.rsz_data_types import ArrayData, ObjectData, UserDataData, F32Data
from file_handlers.rsz.rsz_file import RszFile
from utils.number_format import parse_integer


def set_scalar_fields(rsz: RszFile, instance: int, assignments) -> bytes:
    """Apply ``FIELD=VALUE`` assignments to one instance and return verified bytes.

    Arrays and object/userdata references need their native structural operations.
    """
    if instance not in rsz.parsed_elements:
        raise ValueError(f'Unknown instance: {instance}')
    fields = rsz.parsed_elements[instance]
    for assignment in assignments:
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
            item.value = parse_integer(text)
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
    reopened.filepath = rsz.filepath
    reopened.type_registry = rsz.type_registry
    reopened.read(output, validate_type_registry=True)
    actual = {index: fields_record(values) for index, values in reopened.parsed_elements.items()}
    if actual != expected or reopened.object_table != rsz.object_table:
        raise ValueError('Reopened RSZ graph differs from the authored fields')
    return output
