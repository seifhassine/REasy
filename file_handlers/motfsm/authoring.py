"""Shared native primitives for FSM structural recipes.

The graph editor and offline recipes use the same RSZ graph copier. These
helpers operate on parsed documents/blocks and never load or publish files.
"""
from file_handlers.rsz.graph_copy import copy_graph


def read_document(source, registry=None):
    from .motfsm_file import MotfsmFile
    document = MotfsmFile()
    if registry is not None:
        document.set_rsz_type_info_path(getattr(registry, 'json_path', registry))
    document.read(source)
    return document


def next_identity(used):
    used = set(used)
    value = 1
    while value in used:
        value += 1
    if value >= 0xFFFFFFFF:
        raise ValueError('No free native identity')
    return value


def block_ids(document, block_names, field_index):
    used = set()
    for name in block_names:
        block = document.rsz_blocks.get_block(name)
        if block is None:
            continue
        for index in block.object_table:
            instance = block.get_instance(index)
            if instance is not None and len(instance.fields) > field_index:
                value = instance.fields[field_index].value
                if isinstance(value, int) and not isinstance(value, bool):
                    used.add(value)
    return used


def block_padding_is_zero(block):
    baseline = block.file.build_validated()
    padding = block.end - block.offset - len(baseline)
    return padding >= 0 and block.document.source[block.offset:block.end] == baseline + bytes(padding)


def block_splice(block):
    data = block.file.build_validated()
    return block.offset, block.end, data + bytes(-len(data) % 16)


def verify_block(document, reopened, name):
    from .rsz_adapter import native_value_state
    expected = document.rsz_blocks.get_block(name).file
    actual = reopened.rsz_blocks.get_block(name).file
    if (expected.object_table != actual.object_table or
            native_value_state(expected.parsed_elements) != native_value_state(actual.parsed_elements)):
        raise ValueError(f'{name}: reopened graph differs from the authored instances')


def clone_block_object(block, template_instance_index, new_fields=None, source_block=None):
    """Append an independent graph and object slot; caller owns extension IDs.

    Same-block clones preserve shared roots and external userdata according to
    the native GraphEditor ownership contract. Cross-block imports copy all
    reachable instances, remapping their references into the destination.
    """
    origin = source_block or block
    native = block.file
    if len(native.object_table) > 0xFFFF:
        raise ValueError('BHVT object table exceeds its 16-bit reference range')
    values = new_fields or {}
    fields = origin.file.parsed_elements[template_instance_index]
    if any(name not in fields for name in values):
        raise ValueError(f'Unknown cloned fields: {sorted(set(values) - set(fields))}')
    # Validate and coerce against the source schema before mutating either graph.
    from file_handlers.rsz.utils.rsz_field_utils import coerce_field_value
    converted = {name: coerce_field_value(fields[name], value,
                 instance_infos=origin.file.instance_infos,
                 userdata_strings={i: origin.file.get_rsz_userdata_string(row)
                                   for i, row in origin.file._rsz_userdata_dict.items()},
                 registry=block.document.type_registry, label=name, allow_references=False)
                 for name, value in values.items()}
    copied = copy_graph(origin.file, native, [template_instance_index], owned_only=origin is block)
    index = copied.roots[0]
    native.parsed_elements[index].update(converted)
    native.object_table.append(index)
    block._instances.clear()
    block.document.references.invalidate()
    return index, len(native.object_table) - 1
