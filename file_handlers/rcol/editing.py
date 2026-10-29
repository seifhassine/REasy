"""Native request-set authoring shared by command-line and GUI consumers."""
from copy import deepcopy

from file_handlers.rsz.graph_copy import copy_graph
from .request_set import RequestSet


def insert_request_root(rcol, index, instance):
    """Insert an object-table root, preserving every effective shape binding."""
    if not 0 <= index <= len(rcol.rsz.object_table):
        raise ValueError('Request root insertion is outside the object table')
    legacy = []
    if rcol.file_version < 25:
        for request in rcol.request_sets:
            group = rcol.groups[request.info.group_index]
            if group.shapes:
                base = min(shape.info.user_data_index for shape in group.shapes)
                legacy.append((request, base, base + request.info.shape_offset))
    rcol.rsz.object_table.insert(index, instance)
    if rcol.file_version < 25:
        for group in rcol.groups:
            for shape in group.shapes:
                if shape.info.user_data_index >= index:
                    shape.info.user_data_index += 1
        for request, base, start in legacy:
            if base < index <= start:
                request.info.shape_offset += 1
    else:
        for request in rcol.request_sets:
            if request.info.request_set_userdata_index >= index:
                request.info.request_set_userdata_index += 1
            if request.info.group_userdata_index_start >= index:
                request.info.group_userdata_index_start += 1


def clone_request_set(rcol, template_index, name, *, field0=None):
    """Copy request/shape userdata independently, retaining the native shape group."""
    if not 0 <= template_index < len(rcol.request_sets):
        raise ValueError('Request template index is outside the table')
    template = rcol.request_sets[template_index]
    group = rcol.groups[template.info.group_index]
    native = rcol.rsz
    used = {request.info.field0 for request in rcol.request_sets}
    field0 = max(used, default=-1) + 1 if field0 is None else field0
    identity = max((request.info.id for request in rcol.request_sets), default=-1) + 1
    if type(field0) is not int or not 0 <= field0 <= 0xFFFFFFFF or field0 in used:
        raise ValueError('New field0 must be an unused unsigned 32-bit hit reference')
    if identity > 0xFFFFFFFF:
        raise ValueError('Request identities are exhausted')
    root_slot = template_index if rcol.file_version < 25 else template.info.request_set_userdata_index
    bases = [shape.info.user_data_index for shape in group.shapes]
    if rcol.file_version < 25:
        shape_slots = list(range(min(bases) + template.info.shape_offset,
                                 max(bases) + template.info.shape_offset + 1)) if bases else []
    else:
        start = template.info.group_userdata_index_start
        shape_slots = list(range(start, start + len(group.shapes)))
    slots = [root_slot, *shape_slots]
    if any(not 0 <= slot < len(native.object_table) for slot in slots):
        raise ValueError('Request userdata references an invalid object slot')
    roots = [native.object_table[slot] for slot in slots]
    if any(not root for root in roots):
        raise ValueError('Request template has a null userdata root')
    copied = copy_graph(native, native, roots)
    request = RequestSet(len(rcol.request_sets), deepcopy(template.info))
    request.info.id = request.info.request_set_index = identity
    request.info.field0 = field0
    request.info.name = name
    request.instance = copied.roots[0]
    request.group = group
    request.shape_userdata = [deepcopy(item) for item in template.shape_userdata if isinstance(item, (bytes, bytearray))]
    fields = native.parsed_elements[request.instance]
    if 'Name' in fields:
        fields['Name'].value = name
    if rcol.file_version < 25:
        insert_request_root(rcol, request.index, request.instance)
        request.info.shape_offset = (len(native.object_table) - min(shape.info.user_data_index for shape in group.shapes)
                                     if group.shapes else 0)
    else:
        request.info.request_set_userdata_index = len(native.object_table)
        native.object_table.append(request.instance)
        request.info.group_userdata_index_start = len(native.object_table)
    native.object_table.extend(copied.roots[1:])
    rcol.request_sets.append(request)
    rcol.setup_references(rcol.file_version)
    return request.index


def resolve_requests(rcol):
    """Snapshot requests and their complete graphs independently of instance IDs."""
    from file_handlers.rsz.inspection import graph_value
    native, result = rcol.rsz, {}
    for index, request in enumerate(rcol.request_sets):
        group = rcol.groups[request.info.group_index]
        root = index if rcol.file_version < 25 else request.info.request_set_userdata_index
        shapes = []
        for position, shape in enumerate(group.shapes):
            slot = (shape.info.user_data_index + request.info.shape_offset if rcol.file_version < 25
                    else request.info.group_userdata_index_start + position)
            shapes.append((str(shape.info.guid), graph_value(native, native.object_table[slot])))
        if request.info.id in result:
            raise ValueError(f'Duplicate request authoring ID: {request.info.id}')
        result[request.info.id] = {
            'name': request.info.name, 'field0': request.info.field0, 'group': str(group.info.guid),
            'root': graph_value(native, native.object_table[root]), 'shapes': shapes}
    return result
