"""Selector object references and independent copies in the native selectors table."""
import copy


def resolve_selector(document, raw):
    if raw & 0xFFFFFFFF == 0xFFFFFFFF:
        return None
    if not 0 <= raw <= 0xFFFF:
        raise ValueError(f'Invalid selector object index: {raw}')
    if raw >= len(document.rsz_blocks.get_block('selectors').object_table):
        raise ValueError(f'Invalid selectors object: {raw}')
    instance = document.references.object_instance('selectors', raw)
    if instance.index == 0 or instance.type_id == 0:
        raise ValueError(f'Selector object {raw} resolves to NULL')
    return instance


def validate_selectors(document):
    for index, node in enumerate(document.bhvt.nodes):
        try:
            resolve_selector(document, node.selector_id)
        except (IndexError, ValueError) as exc:
            raise ValueError(f'Node {index} {node.name}: {exc}') from exc


def clone_selector(document, raw):
    """Append a private selector; return its object-table index, never its instance index.

    A node without a selector stays without one. The native writer serializes the
    appended instance; callers include that block in their structural splice.
    """
    template = resolve_selector(document, raw)
    if template is None:
        return -1
    block = document.rsz_blocks.get_block('selectors')
    native = block.file
    object_index = len(native.object_table)
    if object_index > 0xFFFF:
        raise ValueError('Selector object table exceeds the BHVT reference range')
    instance_index = len(native.instance_infos)
    native.instance_infos.append(copy.deepcopy(native.instance_infos[template.index]))
    native.parsed_elements[instance_index] = copy.deepcopy(native.parsed_elements[template.index])
    native.object_table.append(instance_index)
    return object_index
