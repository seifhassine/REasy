"""Add the LongSword RCOL request set that the imported atk_620 node points at.

The new request set reuses an existing 中央/根元/先端 three-box composite judgment
(the one the Spirit Roundslash 気刃大回旋斬 uses, so no shape is created) and copies
every attack parameter from that move's request set (id 32, `PlHitAttackRSData`).
Only then does the RCOL gain one extra request set; nothing else changes, which is
asserted by resolving the whole userdata graph before and after.

usage: python add_rcol_request.py RCOL OUTDIR --name atk_620 [--template 気刃斬りフィニッシュ]
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
import sys
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

from PySide6.QtWidgets import QApplication                                          # noqa: E402
from file_handlers.rcol.rcol_handler import RcolHandler                             # noqa: E402
from file_handlers.rsz.rsz_data_types import ArrayData, ObjectData, UserDataData     # noqa: E402

REGISTRY = ROOT / 'resources/data/dumps/rszmhrise.json'
BINDING_JOINT = 'L_Weapon_00'
SHAPE_NAMES = ('中央', '根元', '先端')
SKIP_FIELDS = frozenset({'Name'})          # the userdata name is ours, not the move's


def resolve_requests(rcol):
    """Resolve every request set's userdata graph so comparisons ignore indices."""
    rsz = rcol.rsz
    cache = {}

    def instance(index):
        if not index:
            return None
        if index not in cache:
            fields = rsz.parsed_elements.get(index)
            cache[index] = None if fields is None else (
                rsz.instance_infos[index].type_id,
                tuple((name, value(field)) for name, field in fields.items()))
        return cache[index]

    def value(field):
        if isinstance(field, (ObjectData, UserDataData)):
            return instance(field.value)
        if isinstance(field, ArrayData):
            return tuple(value(item) for item in field.values)
        return getattr(field, 'value', None)

    result = {}
    for index, request in enumerate(rcol.request_sets):
        group = rcol.groups[request.info.group_index]
        shapes = []
        for shape in group.shapes:
            slot = shape.info.user_data_index + request.info.shape_offset
            shapes.append((str(shape.info.guid), instance(rsz.object_table[slot])))
        result[request.info.id] = {
            'name': request.info.name, 'group': str(group.info.guid),
            'root': instance(rsz.object_table[index]), 'shapes': shapes}
    return result


def type_name(handler, rsz, instance_id):
    info = rsz.instance_infos[instance_id]
    definition = handler.type_registry.get_type_info(info.type_id)
    assert definition, f'unknown type {info.type_id}'
    return definition['name']


def copy_fields(rsz, source_id, target_id, skipped, seen=None):
    """Copy scalar/array/nested-object fields between two same-type instances."""
    seen = seen or set()
    if not source_id or not target_id or (source_id, target_id) in seen:
        return 0
    seen.add((source_id, target_id))
    source = rsz.parsed_elements.get(source_id)
    target = rsz.parsed_elements.get(target_id)
    if source is None or target is None:
        return 0
    copied = 0
    for name, field in source.items():
        if name in skipped:
            continue
        twin = target.get(name)
        if twin is None:
            continue
        if isinstance(field, (ObjectData, UserDataData)):
            if field.value and getattr(twin, 'value', 0):
                copied += copy_fields(rsz, field.value, twin.value, frozenset(), seen)
        elif isinstance(field, ArrayData):
            if not isinstance(twin, ArrayData):
                continue
            if len(field.values) != len(twin.values):
                raise AssertionError(f'{name}: array length {len(twin.values)} != template {len(field.values)}')
            for source_item, target_item in zip(field.values, twin.values):
                if isinstance(source_item, (ObjectData, UserDataData)):
                    copied += copy_fields(rsz, source_item.value, target_item.value, frozenset(), seen)
                else:
                    target_item.value = source_item.value
                    copied += 1
        elif hasattr(field, 'value') and hasattr(twin, 'value'):
            twin.value = field.value
            copied += 1
    return copied


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('rcol', type=Path)
parser.add_argument('output_directory', type=Path)
parser.add_argument('--name', default='atk_620')
parser.add_argument('--template', default='気刃斬りフィニッシュ')
args = parser.parse_args()
SOURCE = args.rcol.resolve()
source_bytes = SOURCE.read_bytes()

app = QApplication.instance() or QApplication([])
with contextlib.redirect_stdout(io.StringIO()):
    handler = RcolHandler()
    handler.filepath = str(SOURCE)
    handler.init_type_registry(str(REGISTRY))
    handler.read(source_bytes)
    handler.rcol.rsz.validate_type_registry_state()
    before = resolve_requests(handler.rcol)
    viewer = handler.create_viewer()
assert viewer is not None

rcol = handler.rcol
rsz = rcol.rsz
templates = [index for index, request in enumerate(rcol.request_sets)
             if request.info.name == args.template]
assert len(templates) == 1, f'template {args.template!r} resolves to {len(templates)} request sets'
template_index = templates[0]
template = rcol.request_sets[template_index]
group_index = template.info.group_index
group = rcol.groups[group_index]
shape_names = tuple(shape.info.name for shape in group.shapes)
joints = {shape.info.primary_joint_name_str for shape in group.shapes}
print(f'template: request id={template.info.id} name={template.info.name!r} '
      f'group={group_index} {group.info.name!r} shapes={shape_names} joints={joints} '
      f'shape_offset={template.info.shape_offset}')
assert shape_names == SHAPE_NAMES, shape_names
assert joints == {BINDING_JOINT}, joints
assert template.info.id == template_index, 'this file must keep request id == table index for the FSM hit index'

request_type = type_name(handler, rsz, rsz.object_table[template_index])
shape_type = type_name(handler, rsz, rsz.object_table[group.shapes[0].info.user_data_index
                                                     + template.info.shape_offset])
print(f'userdata types: request={request_type!r} shape={shape_type!r}')

viewer._prompt_request_set_type = lambda: request_type
viewer._prompt_shape_userdata_type = lambda: shape_type
viewer._prompt_request_set_group_index = lambda: group_index
with contextlib.redirect_stdout(io.StringIO()):
    viewer._add_request_set()
assert len(rcol.request_sets) == len(before) + 1
new_index = len(rcol.request_sets) - 1
new = rcol.request_sets[new_index]
print(f'added request set: table index={new_index} id={new.info.id} shape_offset={new.info.shape_offset} '
      f'group={new.info.group_index}')

copied = copy_fields(rsz, rsz.object_table[template_index], rsz.object_table[new_index], SKIP_FIELDS)
for position, shape in enumerate(group.shapes):
    source_slot = shape.info.user_data_index + template.info.shape_offset
    target_slot = shape.info.user_data_index + new.info.shape_offset
    copied += copy_fields(rsz, rsz.object_table[source_slot], rsz.object_table[target_slot], frozenset())
new.info.name = args.name
rsz.parsed_elements[rsz.object_table[new_index]]['Name'].value = args.name
print(f'copied {copied} parameter fields from request id {template.info.id} '
      f'(skipping {sorted(SKIP_FIELDS)}), name -> {args.name!r}')

# Native userdata instances are one-to-one with an empty via.physics.UserData container
# (284 instances, 284 containers, one child each).  The add flow only creates that
# container when the parsed field carries an orig_type string, so the new request and
# its shape userdata come out with a null ParentUserData; create and link the missing
# containers so the new entries sit in the same graph shape as every native entry.
registry = viewer._resolve_type_registry()
object_ops = viewer._get_headless_object_operations()
assert registry and object_ops
container_info, container_type = registry.find_type_by_name('via.physics.UserData')
assert container_info and container_type, 'via.physics.UserData must exist in the registry'
slots = [new_index]
for shape in group.shapes:
    slots.append(shape.info.user_data_index + new.info.shape_offset)
linked = []
for slot in slots:
    owner = rsz.object_table[slot]
    field = rsz.parsed_elements[owner].get('ParentUserData')
    assert field is not None
    if field.value:
        continue
    container_id = object_ops._create_object_instance_with_nested_objects(
        container_info, container_type, len(rsz.instance_infos))
    assert container_id > 0
    owner = rsz.object_table[slot]
    rsz.parsed_elements[owner]['ParentUserData'].value = container_id
    linked.append((slot, owner, container_id))
print('linked userdata containers: ' + ', '.join(f'slot {s} -> instance #{o} parent #{c}' for s, o, c in linked))
assert len(linked) == len(slots), linked

with contextlib.redirect_stdout(io.StringIO()):
    output = handler.rebuild()
try:
    rcol.user_data_bytes = rsz.build_headless()
except Exception:
    pass

# --- verification
reopened = RcolHandler()
reopened.filepath = str(SOURCE)
reopened.init_type_registry(str(REGISTRY))
reopened.read(output)
after = resolve_requests(reopened.rcol)
assert set(after) == set(before) | {new.info.id}, sorted(set(after) ^ (set(before) | {new.info.id}))
for key, original in before.items():
    assert after[key] == original, f'request id {key} changed'
print(f'existing request sets unchanged: {len(before)} resolved graphs identical')

new_id = new.info.id
template_values = dict(before[template.info.id])
expected = dict(template_values)
expected['name'] = args.name
STRUCTURAL = {'Name', 'ParentUserData'}
expected['root'] = tuple((name, None if name in STRUCTURAL else value)
                         for name, value in template_values['root'][1])
actual_root = tuple((name, None if name in STRUCTURAL else value) for name, value in after[new_id]['root'][1])
assert after[new_id]['name'] == args.name
assert after[new_id]['group'] == template_values['group'], 'the new request must reuse the template composite'
if actual_root != expected['root']:
    for (name, want), (_, got) in zip(expected['root'], actual_root):
        if want != got:
            print(f'  DIFF root.{name}: template={want!r} new={got!r}')
if after[new_id]['shapes'] != template_values['shapes']:
    for (guid, want), (_, got) in zip(template_values['shapes'], after[new_id]['shapes']):
        if want != got:
            for (name, w), (_, g) in zip(want[1], got[1]):
                if w != g:
                    print(f'  DIFF shape {guid[:8]} {name}: template={w!r} new={g!r}')
assert actual_root == expected['root'], 'copied request parameters differ from the template'
def normalize_shapes(shapes):
    out = []
    for guid, resolved in shapes:
        if resolved is None:
            out.append((guid, None))
            continue
        type_id, fields = resolved
        out.append((guid, type_id, tuple((name, None if name in STRUCTURAL else value)
                                        for name, value in (fields or ()))))
    return tuple(out)

assert normalize_shapes(after[new_id]['shapes']) == normalize_shapes(template_values['shapes']),     'copied shape userdata differ from the template'
assert len(after[new_id]['shapes']) == len(SHAPE_NAMES)
print(f'new request id {new_id} reuses group {after[new_id]["group"][:8]}… and matches the template parameters')
assert reopened.rcol.header.num_request_sets == len(before) + 1
assert reopened.rcol.header.max_request_set_id == new_id, reopened.rcol.header.max_request_set_id
parents = {}
for instance, fields in reopened.rcol.rsz.parsed_elements.items():
    for name, field in (fields or {}).items():
        if name == 'ParentUserData' and getattr(field, 'value', 0):
            parents[instance] = field.value
children = {}
for child, parent in parents.items():
    children.setdefault(parent, []).append(child)
assert children and max(len(v) for v in children.values()) == 1, 'a container gained several children'
for slot in slots:
    owner = reopened.rcol.rsz.object_table[slot]
    assert owner in parents, f'slot {slot} instance #{owner} still has no userdata container'
    assert reopened.rcol.rsz.instance_infos[parents[owner]].type_id == container_type
print(f'userdata containers: {len(parents)} linked instances, one child per container (native shape preserved)')
assert reopened.rebuild() == output, 'not stable'

folder = args.output_directory.resolve()
folder.mkdir(parents=True, exist_ok=True)
candidate = folder / SOURCE.name
candidate.write_bytes(output)
(folder / 'manifest.json').write_text(json.dumps(dict(
    source=str(SOURCE), source_sha256=hashlib.sha256(source_bytes).hexdigest(), source_size=len(source_bytes),
    candidate=str(candidate), candidate_sha256=hashlib.sha256(output).hexdigest(), candidate_size=len(output),
    new_request_set_id=new_id, new_request_set_index=new_index, name=args.name,
    template_request_set_id=template.info.id, template_name=template.info.name,
    group_index=group_index, group_name=group.info.name, group_guid=str(group.info.guid),
    shapes=[dict(name=shape.info.name, guid=str(shape.info.guid),
                 joint=shape.info.primary_joint_name_str) for shape in group.shapes],
    request_userdata_type=request_type, shape_userdata_type=shape_type,
    copied_fields=copied, size_delta=len(output) - len(source_bytes)), indent=2, ensure_ascii=False),
    encoding='utf-8')
print(f'wrote {candidate}')
