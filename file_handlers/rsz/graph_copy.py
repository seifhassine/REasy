"""Copy native instance graphs without a viewer or a second field serializer."""
from copy import deepcopy
from dataclasses import dataclass

from .rsz_instance_operations import RszInstanceOperations as Instances
from .utils.rsz_field_utils import update_references_with_mapping


@dataclass(frozen=True)
class GraphCopy:
    roots: tuple[int, ...]
    instances: dict[int, int]
    shifted: dict[int, int]


def copy_graph(source, target, roots, *, insertion=None, owned_only=False, shared=None):
    """Copy roots and their references, preserving aliasing within the copied graph.

    ``owned_only`` uses the editor's existing ownership rules and is valid only
    within one instance namespace. Full copies keep external userdata shared
    within that namespace; imports copy its native resource records as well.
    Cross-file references can instead be bound explicitly with ``shared``.
    Insertion shifts existing instances and references together; object-table
    slots are retained. Callers choose which copied roots become new slots.
    """
    roots = tuple(roots)
    same = source is target
    shared = dict(shared or {})
    if owned_only and not same:
        raise ValueError('Owned graph copies require one instance namespace')
    insertion = len(target.instance_infos) if insertion is None else insertion
    if not 1 <= insertion <= len(target.instance_infos):
        raise ValueError('Graph insertion is outside the instance table')
    for old, new in shared.items():
        if not 0 < old < len(source.instance_infos) or not 0 < new < len(target.instance_infos):
            raise ValueError('Shared graph mapping contains an invalid instance')
        if (source.instance_infos[old].type_id, source.instance_infos[old].crc) != (
                target.instance_infos[new].type_id, target.instance_infos[new].crc):
            raise ValueError('Shared graph mapping has incompatible instance types')
    external = source._rsz_userdata_dict
    selected = set()
    if owned_only:
        for root in roots:
            selected.update(Instances.collect_owned_instances(
                source.parsed_elements, root, object_table=source.object_table,
                valid_instance_ids=range(len(source.instance_infos))))
        selected.update(roots)
        selected.difference_update(set(external) - set(roots))
    else:
        pending = list(roots)
        while pending:
            index = pending.pop()
            if not index or index in selected or index in shared:
                continue
            if not 0 < index < len(source.instance_infos):
                raise ValueError(f'Dangling graph reference: {index}')
            if same and index in external and index not in roots:
                continue
            selected.add(index)
            for _, _, ref, _ in Instances.iter_instance_references(
                    {index: source.parsed_elements.get(index, {})}):
                pending.append(ref.value)
    if not roots or any(root not in selected for root in roots):
        raise ValueError('Graph roots must be valid, independently copied instances')
    selected.difference_update(set(shared) - set(roots))
    originals = sorted(selected)
    for index in originals:
        if not 0 < index < len(source.instance_infos):
            raise ValueError(f'Invalid graph instance: {index}')
        info = source.instance_infos[index]
        if not same:
            definition = target.type_registry.get_type_info(info.type_id)
            source_definition = source.type_registry.get_type_info(info.type_id)
            if not definition or definition != source_definition:
                raise ValueError(f'Incompatible graph type registry: 0x{info.type_id:08X}')
        if index in external and hasattr(external[index], 'embedded_instance_infos'):
            raise ValueError('Embedded userdata imports require the native embedded editor')
    shifted = {i: i + len(originals) for i in range(insertion, len(target.instance_infos))}
    mapping = {old: insertion + i for i, old in enumerate(originals)}
    references = (dict(shifted) if same else {0: 0})
    references.update({old: shifted.get(new, new) for old, new in shared.items()})
    references.update(mapping)
    fields = {mapping[i]: deepcopy(dict(source.parsed_elements.get(i, {}))) for i in originals}
    for data in fields.values():
        for _, _, ref, _ in Instances.iter_instance_references({0: data}):
            if not 0 <= ref.value < len(source.instance_infos):
                raise ValueError(f'Dangling graph reference: {ref.value}')
            if not same and ref.value not in references:
                raise ValueError(f'Unmapped imported graph reference: {ref.value}')
        update_references_with_mapping(data, references)
    infos = [deepcopy(source.instance_infos[i]) for i in originals]
    userdata = [(deepcopy(external[i]), source.get_rsz_userdata_string(external[i]))
                for i in originals if i in external]
    hierarchy = deepcopy({i: source.instance_hierarchy[i] for i in originals if i in source.instance_hierarchy})

    # All source data has been captured before shifting a same-file insertion.
    target.parsed_elements = Instances.remap_instance_fields(target.parsed_elements, shifted)
    target.instance_hierarchy = Instances.remap_hierarchy(target.instance_hierarchy, shifted)
    target.object_table[:] = [shifted.get(i, i) for i in target.object_table]
    for row in target.rsz_userdata_infos:
        row.instance_id = shifted.get(row.instance_id, row.instance_id)
    for name in ('_gameobject_instance_ids', '_folder_instance_ids'):
        values = getattr(target, name, set())
        setattr(target, name, {shifted.get(i, i) for i in values})
    target.instance_infos[insertion:insertion] = infos
    target.parsed_elements.update(fields)
    target.instance_hierarchy.update(Instances.remap_hierarchy(hierarchy, references))
    for row, path in userdata:
        row.instance_id = mapping[row.instance_id]
        target.rsz_userdata_infos.append(row)
        target.set_rsz_userdata_string(row, path)
    target._rsz_userdata_dict = {row.instance_id: row for row in target.rsz_userdata_infos}
    target._rsz_userdata_set = set(target._rsz_userdata_dict)
    return GraphCopy(tuple(mapping[i] for i in roots), mapping, shifted)
