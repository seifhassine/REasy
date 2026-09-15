"""Reproduce the MHRise atk_101 collision fixture verified in a live game.

Usage: python tools/rcol_validation/atk101_fixture.py RCOL_SOURCE FSM_SOURCE OUTPUT_DIRECTORY
The source checksums identify the original LongSword RCOL and the previously
validated Node/Condition FSM fixture. Neither source is modified.
The output adds request 72 with copied attack userdata and a separate 10x-length
capsule, then points both atk_101 entrances at it. Install/reload both outputs
for the gameplay test; long-range hits were confirmed on 2026-09-15.
"""
import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from file_handlers.motfsm.motfsm_file import MotfsmFile
from file_handlers.rcol.rcol_handler import RcolHandler
from file_handlers.rcol.request_set import RequestSet
from file_handlers.rsz.rsz_instance_operations import RszInstanceOperations
from test_rcol_editing import request_values

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('rcol_source', type=Path)
parser.add_argument('fsm_source', type=Path)
parser.add_argument('output_directory', type=Path)
args = parser.parse_args()
RCOL_SOURCE = args.rcol_source.resolve()
FSM_SOURCE = args.fsm_source.resolve()
REGISTRY = ROOT / 'resources/data/dumps/rszmhrise.json'
folder = args.output_directory.resolve()
assert folder / 'LongSword.rcol.20' != RCOL_SOURCE, 'Output must not replace RCOL source'
assert folder / 'LongSword.motfsm2.43' != FSM_SOURCE, 'Output must not replace FSM source'
folder.mkdir(parents=True, exist_ok=True)
app = QApplication([])

def read_rcol(data):
    handler = RcolHandler()
    handler.filepath = str(RCOL_SOURCE)
    handler.init_type_registry(str(REGISTRY))
    handler.read(data)
    handler.rcol.rsz.validate_type_registry_state()
    return handler

rcol_source = RCOL_SOURCE.read_bytes()
fsm_source = FSM_SOURCE.read_bytes()
assert hashlib.sha256(rcol_source).hexdigest() == 'c2ac01f37b258d84fc3afa99e5b68a190e93e67a58a7f4781be194088d5c5b0f'
assert hashlib.sha256(fsm_source).hexdigest() == '890b17adea0c95479450f478e8baef5b5a0cb45569b6fdc35a9e4cb7095bee26'
handler = read_rcol(rcol_source)
original = read_rcol(rcol_source).rcol
before = request_values(original)
rcol = handler.rcol
native = rcol.rsz
viewer = handler.create_viewer()
assert viewer is not None
template_request = next(rs for rs in original.request_sets if rs.info.id == 1)
assert template_request.info.name == '\u524d\u65ac\u308a'
template_group = original.groups[template_request.info.group_index]
assert template_group.info.name == '\u592a\u5200'
template_shape = template_group.shapes[0]
assert template_shape.info.name == '\u4e2d\u592e'
shape_slot = template_shape.info.user_data_index + template_request.info.shape_offset

def clone_graph(source_rsz, root_id):
    ids = sorted({root_id} | RszInstanceOperations.collect_owned_instances(
        source_rsz.parsed_elements, root_id, object_table=source_rsz.object_table,
        include_userdata=True, valid_instance_ids=range(len(source_rsz.instance_infos))))
    assert not (set(ids) & source_rsz._rsz_userdata_set)
    mapping = {old: len(native.instance_infos) + i for i, old in enumerate(ids)}
    for old in ids:
        native.instance_infos.append(copy.deepcopy(source_rsz.instance_infos[old]))
    fields = copy.deepcopy({old: dict(source_rsz.parsed_elements[old]) for old in ids})
    native.parsed_elements.update(RszInstanceOperations.remap_instance_fields(fields, mapping))
    hierarchy = copy.deepcopy({old: source_rsz.instance_hierarchy[old] for old in ids})
    native.instance_hierarchy.update(RszInstanceOperations.remap_hierarchy(hierarchy, mapping))
    return mapping[root_id], mapping

new_request_root, request_mapping = clone_graph(original.rsz, template_request.instance)
native.object_table.append(new_request_root)
new_request_index = len(rcol.request_sets)
viewer._insert_root_object_id_at(new_request_index, len(native.object_table) - 1)
new_shape_root, shape_mapping = clone_graph(original.rsz, original.rsz.object_table[shape_slot])
new_shape_slot = len(native.object_table)
native.object_table.append(new_shape_root)

group = copy.deepcopy(template_group)
group.info.name = 'REasy_atk101_10x'
group.info.guid = uuid.UUID('a49623eb-350b-4642-a0d0-13bf3c046c35').bytes
assert all(g.info.guid != group.info.guid for g in rcol.groups)
shape = copy.deepcopy(template_shape)
shape.info.name = 'REasy_blade_10x'
shape.info.guid = uuid.UUID('0bb41437-4c44-44a0-9b20-844c25b9bb6d').bytes
assert all(s.info.guid != shape.info.guid for g in rcol.groups for s in g.shapes)
shape.info.user_data_index = new_shape_slot
shape.instance = new_shape_root
shape.shape.end = [a + (b-a)*10 for a,b in zip(shape.shape.start,shape.shape.end)]
group.shapes = [shape]
group.extra_shapes = []
new_group_index = len(rcol.groups)
rcol.groups.append(group)
new_id = max(rs.info.id for rs in rcol.request_sets) + 1
assert new_id == 72
request = RequestSet(new_request_index, copy.deepcopy(template_request.info))
request.info.id = request.info.field0 = request.info.request_set_index = new_id
request.info.name = 'REasy_atk101_10x'
request.info.group_index = new_group_index
request.info.shape_offset = 0
request.group, request.instance = group, new_request_root
rcol.request_sets.append(request)
rcol.setup_references(handler.file_version)
rcol_output = handler.rebuild()
verified = read_rcol(rcol_output).rcol
after = request_values(verified)
for key,value in before.items():
    assert after[key] == value, ('Changed existing request',key)
assert after[new_id]['root'] == before[1]['root'], 'Attack parameters were not copied completely'
assert after[new_id]['shapes'][0][1] == before[1]['shapes'][0][1], 'Shape userdata graph differs from source'
assert len(verified.groups) == 9 and len(verified.request_sets) == 73
assert sum(len(g.shapes) for g in verified.groups) == 19
new_shape = verified.groups[new_group_index].shapes[0]
old_length = math.dist(template_shape.shape.start,template_shape.shape.end)
new_length = math.dist(new_shape.shape.start,new_shape.shape.end)
assert math.isclose(new_length / old_length,10,rel_tol=1e-6)
assert new_shape.shape.start == template_shape.shape.start
assert new_shape.shape.radius == template_shape.shape.radius
assert new_shape.info.primary_joint_name_str == template_shape.info.primary_joint_name_str
assert verified.write(file_version=20) == rcol_output

fsm = MotfsmFile()
fsm.read(fsm_source)
expected_fsm = bytearray(fsm_source)
edits = []
for i,node in enumerate(fsm.bhvt.nodes):
    if node.name != 'atk_101': continue
    for reference in node.actions:
        action = fsm.references.action(reference.id_hash,reference.ex_id)
        if action.class_name != 'snow.player.fsm.PlayerHitAction2': continue
        users = [j for j,n in enumerate(fsm.bhvt.nodes) if any(
            a.id_hash == reference.id_hash and a.ex_id == reference.ex_id for a in n.actions)]
        assert users == [i], 'Attack Action is shared with other nodes'
        field = next(f for f in action.fields if f.name == '_hitIndex')
        assert field.value == 1
        fsm.edit_field(field.binding,new_id)
        struct.pack_into('<I',expected_fsm,field.binding.offset,new_id)
        edits.append(dict(node_index=i,node_id=f'0x{node.id_hash:08X}',action_instance=action.index,
                          action_id=f'0x{reference.id_hash:08X}',old_hit_index=1,new_hit_index=new_id,
                          field_offset=field.binding.offset))
assert {e['node_index'] for e in edits} == {4006,4069}
fsm_output = fsm.rebuild()
assert fsm_output == bytes(expected_fsm)
reopened = MotfsmFile()
reopened.read(fsm_output)
for edit in edits:
    action = reopened.rsz_blocks.get_block('actions').get_instance(edit['action_instance'])
    assert next(f.value for f in action.fields if f.name == '_hitIndex') == new_id
assert reopened.rebuild() == fsm_output
assert FSM_SOURCE.read_bytes() == fsm_source and RCOL_SOURCE.read_bytes() == rcol_source
(folder/'LongSword.rcol.20').write_bytes(rcol_output)
(folder/'LongSword.motfsm2.43').write_bytes(fsm_output)
manifest = dict(
    fsm_source=str(FSM_SOURCE),
    rcol_source=str(RCOL_SOURCE),rcol_source_sha256=hashlib.sha256(rcol_source).hexdigest(),
    rcol_candidate_sha256=hashlib.sha256(rcol_output).hexdigest(),rcol_before_size=len(rcol_source),rcol_after_size=len(rcol_output),
    fsm_source_sha256=hashlib.sha256(fsm_source).hexdigest(),fsm_candidate_sha256=hashlib.sha256(fsm_output).hexdigest(),
    fsm_edits=edits,new_request_id=new_id,new_request_index=new_request_index,new_group_index=new_group_index,
    new_request_root=new_request_root,new_shape_root=new_shape_root,request_instance_mapping=request_mapping,shape_instance_mapping=shape_mapping,
    shape_type=int(new_shape.info.shape_type),joint=new_shape.info.primary_joint_name_str,
    start=new_shape.shape.start,end=new_shape.shape.end,radius=new_shape.shape.radius,
    old_axis_length=old_length,new_axis_length=new_length,length_factor=new_length/old_length,
    groups=9,shapes=19,requests=73,old_request_graphs_preserved=True,new_attack_parameters_equal_source=True)
(folder/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
viewer.close()
print(json.dumps(manifest,indent=2))
