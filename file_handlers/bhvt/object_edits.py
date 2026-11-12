"""See docs/internals/bhvt-editor.md, Object fields."""

from __future__ import annotations

import copy
import ctypes
import math

lazy from file_handlers.rsz.rsz_data_types import ArrayData, F64Data, ObjectData, StructData, UserDataData, _DataValue
lazy from file_handlers.rsz.rsz_file import RSZUserDataInfo, RszInstanceInfo
lazy from file_handlers.rsz.scn_19.scn_19_structure import (EmbeddedInstanceInfo, EmbeddedRSZHeader,
                                                            Scn19RSZUserDataInfo, build_embedded_rsz,
                                                            parse_embedded_rsz)
lazy from file_handlers.rsz.utils.rsz_field_utils import create_default_field_value

from file_handlers.document_commands import SetFields

from .document import BhvtDocument
from .edits import Embedded, Objects, staged_roots
from .model import Hfsm, Tree
from .references import blank_fields, blank_value, new_instances, references
from .rules import constructor_values


def snapshot(data) -> dict[tuple[int, str], tuple[object, str, object]]:
    found, stack = {}, [data]
    while stack:
        item = stack.pop()
        for name, value in vars(item).items():
            if isinstance(value, _DataValue):
                stack.append(value)
            elif not name.startswith("_") and name != "orig_type":
                found[id(item), name] = (item, name, list(value) if isinstance(value, list) else value)
    return found


def restore(state: dict) -> None:
    for target, name, value in state.values():
        setattr(target, name, list(value) if isinstance(value, list) else value)


def _stored(target, value):
    if isinstance(value, float) and not isinstance(target, F64Data):
        return ctypes.c_float(value).value
    return [_stored(target, item) for item in value] if isinstance(value, list) else value


def commit(document: BhvtDocument, data, before: dict, text: str, merge_key=None) -> bool:
    edits: dict[int, tuple[object, dict]] = {}
    for key, (target, name, value) in snapshot(data).items():
        old, value = before[key][2], _stored(target, value)
        if old != value and not (isinstance(old, float) and math.isnan(old) and math.isnan(value)):
            edits.setdefault(id(target), (target, {}))[1][name] = value
    restore(before)
    if edits:
        document.push(SetFields(document, list(edits.values()), text, merge_key))
    return bool(edits)


def relayout(document: BhvtDocument, model: Tree | Hfsm, rsz, text: str, staged=(), changes=()) -> None:
    document.push(Objects(document, model, rsz, list(range(len(rsz.object_table))), text, staged, changes))


def classed(rsz, field) -> bool:
    kind = field.element_class if isinstance(field, ArrayData) else type(field)
    return kind is ObjectData or kind is UserDataData and rsz.has_embedded_rsz


def built(rsz, type_name: str, kind: type = ObjectData) -> list:
    entry = new_instances(rsz, type_name, constructor_values(rsz.game_version).get(type_name, {}))
    if kind is ObjectData:
        return entry
    draft, user = Scn19RSZUserDataInfo(), Scn19RSZUserDataInfo()
    header = draft.embedded_rsz_header = EmbeddedRSZHeader()
    header.magic, header.version = rsz.rsz_header.magic, rsz.rsz_header.version
    draft.embedded_instance_infos = [EmbeddedInstanceInfo(), *(info for info, _fields in entry)]
    draft.embedded_instances = dict(enumerate([{}, *(fields for _info, fields in entry)]))
    draft.embedded_object_table = [len(entry)]
    user.type_id, user.data = entry[-1][0].type_id, memoryview(build_embedded_rsz(draft, rsz.type_registry))
    parse_embedded_rsz(user, rsz.type_registry)
    return [(entry[-1][0], {"embedded_rsz": user} if isinstance(rsz, Embedded) else frozendict(), user)]


def chosen(rsz, picked, kind: type) -> tuple[int, list]:
    if not isinstance(picked, str):
        return 0 if picked is None else picked.instance_id, []
    staged = [built(rsz, picked, kind)]
    return staged_roots(rsz, staged)[0], staged


def set_object(document: BhvtDocument, model: Tree | Hfsm, rsz, field, picked, text: str) -> None:
    value, staged = chosen(rsz, picked, type(field))
    relayout(document, model, rsz, text, staged, [(field, "value", value)])


def new_element(rsz, array, size: int, picked=None) -> tuple[object, list]:
    if isinstance(array, StructData):
        return blank_fields(rsz, rsz.type_registry.find_type_by_name(array.orig_type)[0]), []
    if classed(rsz, array):
        value, staged = chosen(rsz, picked, array.element_class)
        return array.element_class(value, orig_type=array.orig_type), staged
    return blank_value(create_default_field_value(array.element_class, array.orig_type, False, size,
                                                  has_embedded_rsz=rsz.has_embedded_rsz), size), []


def _linked(item) -> bool:
    return isinstance(item, (ObjectData, UserDataData)) or isinstance(item, dict) and any(references(item))


def set_elements(document: BhvtDocument, model: Tree | Hfsm, rsz, array, values: list, text: str, staged=()) -> None:
    if staged or any(_linked(item) for item in (*array.values, *values)):
        relayout(document, model, rsz, text, staged, [(array, "values", values)])
    else:
        document.push(SetFields(document, [(array, {"values": values})], text))


def set_userdata(document: BhvtDocument, model: Tree | Hfsm, rsz, field, path: str, text: str) -> None:
    staged = []
    if path:
        current = rsz._rsz_userdata_dict.get(field.value)
        info = RszInstanceInfo()
        info.type_id = current.hash if current is not None else rsz.type_registry.find_type_by_name(field.orig_type)[1]
        info.crc = int(rsz.type_registry.get_type_info(info.type_id)["crc"], 16)
        staged = [instantiated([(info, frozendict(), path)], rsz)]
    relayout(document, model, rsz, text, staged,
             [(field, "value", staged_roots(rsz, staged)[0] if staged else 0), (field, "string", path)])


def copied(field):
    if isinstance(field, ArrayData):
        return ArrayData([copied(item) for item in field.values], field.element_class, field.orig_type)
    if isinstance(field, StructData):
        return StructData([{name: copied(value) for name, value in item.items()} for item in field.values],
                          field.orig_type)
    clone = copy.copy(field)
    for name, value in list(vars(clone).items()):
        if name.startswith("_"):
            del vars(clone)[name]
        elif isinstance(value, _DataValue):
            setattr(clone, name, copied(value))
        elif isinstance(value, list):
            setattr(clone, name, list(value))
    return clone


def copied_user(user):
    memo, stack = {}, [user]
    while stack:
        item = stack.pop()
        memo.update({id(value): memoryview(bytes(value)) for value in (item.data, item.original_data)
                     if isinstance(value, memoryview)})
        stack.extend(item.embedded_userdata_infos)
    return copy.deepcopy(user, memo)


def copied_instances(rsz, root: int) -> list[tuple]:
    order, seen = [], set()

    def visit(index: int) -> None:
        if index not in seen:
            seen.add(index)
            for ref in references(rsz.parsed_elements[index]):
                if ref.value > 0:
                    visit(ref.value)
            order.append(index)

    visit(root)
    place, entry = {index: at for at, index in enumerate(order, 1)}, []
    for index in order:
        fields = type(rsz.parsed_elements[index])(
            {name: copied(value) for name, value in rsz.parsed_elements[index].items()})
        for ref in references(fields):
            if ref.value > 0:
                ref.value = place[ref.value]
        user = rsz._rsz_userdata_dict.get(index)
        entry.append((rsz.instance_infos[index], fields) if user is None else (
            rsz.instance_infos[index], fields,
            rsz.get_rsz_userdata_string(user) if isinstance(user, RSZUserDataInfo) else copied_user(user)))
    return entry


def instantiated(entry: list[tuple], rsz) -> list[tuple]:
    out = []
    for info, fields, *user in entry:
        clone = RszInstanceInfo()
        clone.type_id, clone.crc = info.type_id, info.crc
        users = [copied_user(item) for item in user if not isinstance(item, str)]
        for path in (item for item in user if isinstance(item, str)):
            users.append(made := RSZUserDataInfo())
            made.hash = info.type_id
            rsz._rsz_userdata_str_map[made] = path
        out.append((clone, type(fields)({name: copied(value) for name, value in fields.items()}), *users))
    return out
