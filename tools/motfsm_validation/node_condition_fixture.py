"""Generate the MHRise LongSword Node/Condition fixture verified in a live game.

Usage: python tools/motfsm_validation/node_condition_fixture.py SOURCE OUTPUT_DIRECTORY
SOURCE is the output of new_action_fixture.py. SOURCE is never modified.
Load reasy_fsm_structure_check.lua after installing/reloading the new candidate.
The 11 checks cover instances, links, reference removal, and bool/float values;
they do not establish that the new node ran or its condition was evaluated.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from file_handlers.motfsm.motfsm_file import MotfsmFile, BHVTNode, ChildNode, Action
from file_handlers.motfsm.rebuild import BhvtPointers
from file_handlers.motfsm.rsz_adapter import BLOCK_NAMES
from test_motfsm_rebuild import variable_snapshot
from utils.hash_util import murmur3_hash

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('source', type=Path)
parser.add_argument('output_directory', type=Path)
args = parser.parse_args()
TARGET = args.source.resolve()
folder = args.output_directory.resolve()
assert folder / TARGET.name != TARGET, 'Output must not replace the source'
source = TARGET.read_bytes()
source_hash = hashlib.sha256(source).hexdigest()
assert source_hash == 'de991c2c58b9560a7f6fb797886975e80e01af7f5f4dee0208b08252894f7193', 'Input is not the Action validation fixture'
doc = MotfsmFile()
doc.read(source)

def pack(fmt, *values):
    return struct.pack('<' + fmt, *values)

def array(values, fmt='I'):
    return pack('I', len(values)) + pack(fmt * len(values), *values)

def columns(values, fields):
    return b''.join(pack(fmt * len(values), *(getattr(v, name) for v in values)) for name, fmt in fields)

def serialize_node(n):
    data = pack('5I', n.id_hash, n.ex_id, n.name_index, n.parent, n.parent_ex)
    data += pack('I', len(n.children)) + columns(n.children, [('id_hash','I'),('ex_id','I'),('condition_id','i')])
    data += pack('i', n.selector_id) + array(n.selector_callers, 'i') + pack('i', n.selector_caller_condition_id)
    data += pack('I', len(n.actions)) + columns(n.actions, [('id_hash','I'),('ex_id','I')])
    data += pack('iHH', n.priority, n.node_attribute, n.work_flags)
    if n.is_fsm:
        data += pack('II', n.name_hash, n.fullname_hash) + array(n.tags) + pack('BB', n.is_branch, n.is_end)
    data += pack('I', len(n.states)) + b''.join(array(s.mStates.values, 'i') for s in n.states)
    data += columns(n.states, [('mTransitions','I'),('TransitionConditions','i'),('TransitionMaps','I'),('mTransitionAttributes','I'),('mStatesEx','I')])
    data += pack('I', len(n.transitions))
    for t in n.transitions:
        data += array(t.mStartTransitionEvent.values,'i') if doc.layout.transition_event_lists is not False else pack('I',t.mStartTransitionEvent.values[0])
    data += columns(n.transitions, [('mStartState','I'),('mStartStateTransition','i')])
    if doc.layout.transition_state_ex:
        data += columns(n.transitions, [('mStartStateEx','I')])
    if not n.has_reference_tree:
        data += pack('I', len(n.all_states)) + columns(n.all_states, [('mAllState','I'),('mAllTransition','i'),('mAllTransitionID','I'),('mAllStateEx','I'),('mAllTransitionAttributes','i')])
    return data + pack('i', n.reference_tree_index)

def serialize_nodes():
    return pack('I', len(doc.bhvt.nodes)) + b''.join(map(serialize_node, doc.bhvt.nodes)) + array(doc.bhvt.action_ex_ids) + array(doc.bhvt.static_action_ex_ids)

nodes_start = doc.bhvt.offsets['nodes']
assert serialize_nodes() == source[nodes_start:doc.bhvt.node_data_end], 'Node writer does not reproduce source'
condition_block = doc.rsz_blocks.get_block('conditions')
native = condition_block.file
old_instances, old_objects = len(native.instance_infos), list(native.object_table)
template_index = next(i for i in native.object_table if condition_block.get_class_name(i) == 'snow.player.fsm.PlayerFsm2ConditionCheckMotionFrame')
baseline = native.build_validated()
assert source[condition_block.offset:condition_block.end] == baseline + bytes(condition_block.end-condition_block.offset-len(baseline))
condition_id = zlib.crc32(b'REasy.Longsword.condition.test.20260915')
assert all(fields.get('v0_ID') is None or fields['v0_ID'].value != condition_id for fields in native.parsed_elements.values())
new_instance, new_object = old_instances, len(old_objects)
native.instance_infos.append(copy.deepcopy(native.instance_infos[template_index]))
native.parsed_elements[new_instance] = copy.deepcopy(native.parsed_elements[template_index])
fields = native.parsed_elements[new_instance]
fields['v0_ID'].value = condition_id
fields['v2_Condition'].value = False
fields['_StartFrame'].value = 12.5
fields['_EndFrame'].value = 24.5
native.object_table.append(new_instance)
condition_data = native.build_validated()
condition_data += bytes((-len(condition_data)) % 16)

new_name = 'reasy_test_node'
new_id = zlib.crc32(b'REasy.Longsword.node.test.20260915')
assert all(n.id_hash != new_id for n in doc.bhvt.nodes)
strings_start = doc.bhvt.offsets['strings']
string_count = struct.unpack_from('<I', source, strings_start)[0]
strings_end = strings_start + 4 + string_count * 2
new_string = (new_name + '\0').encode('utf-16le')
assert len(new_string) % 16 == 0
strings_data = pack('I', string_count + len(new_string)//2) + source[strings_start+4:strings_end] + new_string
wait = doc.bhvt.nodes[1]
assert wait.name == 'wait' and wait.actions[-1] == Action(0x7C152E99, 0)
wait.actions.pop()
state = copy.deepcopy(next(s for n in doc.bhvt.nodes for s in n.states if s.mTransitions == wait.id_hash))
state.TransitionConditions = new_object
new_node = BHVTNode(id_hash=new_id, ex_id=0, name_index=string_count, name=new_name,
                    parent=0, parent_ex=0, node_attribute=35, reference_tree_index=0,
                    name_hash=murmur3_hash(new_name.encode('utf-16le')),
                    fullname_hash=murmur3_hash(new_name.encode('utf-16le')),
                    actions=[Action(0x7C152E99,0)], states=[state])
new_node_index = len(doc.bhvt.nodes)
doc.bhvt.nodes.append(new_node)
doc.bhvt.nodes[0].children.append(ChildNode(new_id,0,-1))
nodes_data = serialize_nodes()
node_tail = min(offset for offset in doc.bhvt.offsets.values() if offset >= doc.bhvt.node_data_end)
nodes_data += bytes((node_tail - nodes_start - len(nodes_data)) % 16)
splices = sorted([(nodes_start,node_tail,nodes_data),
                  (condition_block.offset,condition_block.end,condition_data),
                  (strings_start,strings_end,strings_data)])
output = bytearray()
cursor = 0
for start,end,payload in splices:
    assert cursor <= start <= end
    output += source[cursor:start] + payload
    cursor = end
output += source[cursor:]

def relocate(offset):
    delta = 0
    for start,end,payload in splices:
        if offset < start: break
        if offset == start: return offset + delta
        if offset < end: raise ValueError(f'Pointer inside replaced data at 0x{offset:X}')
        delta += len(payload) - (end-start)
    return offset + delta

base = doc.tree_data_offset
for slot in BhvtPointers(doc).collect():
    value = struct.unpack_from('<Q',source,slot)[0]
    struct.pack_into('<Q',output,relocate(slot),relocate(base+value)-relocate(base) if value else 0)
for slot in (16,24,32,40):
    value = struct.unpack_from('<Q',source,slot)[0]
    struct.pack_into('<Q',output,slot,relocate(value) if value else 0)
struct.pack_into('<I',output,relocate(doc.tree_info_ptr),relocate(base+doc.tree_data_size)-relocate(base))
output = bytes(output)
verified, original = MotfsmFile(), MotfsmFile()
verified.read(output)
original.read(source)
assert verified.bhvt.nodes == doc.bhvt.nodes
assert verified.bhvt.action_ex_ids == original.bhvt.action_ex_ids
assert verified.bhvt.static_action_ex_ids == original.bhvt.static_action_ex_ids
for name in BLOCK_NAMES:
    old, new = original.rsz_blocks.get_block(name), verified.rsz_blocks.get_block(name)
    if name != 'conditions':
        assert source[old.offset:old.end] == output[new.offset:new.end], name
    else:
        assert new.object_table == old_objects + [new_instance]
        assert new.instance_count == old_instances + 1
        for i in range(1, old_instances):
            a,b = old.get_instance(i), new.get_instance(i)
            assert a.type_id == b.type_id
            if a.start_offset is not None:
                assert source[a.start_offset:a.end_offset] == output[b.start_offset:b.end_offset], i
    print(name,new.instance_count,'verified',flush=True)
assert variable_snapshot(verified) == variable_snapshot(original)
condition = verified.references.object_instance('conditions',new_object)
assert condition.index == new_instance
assert {f.name:f.value for f in condition.fields} == {
    'v0_ID':condition_id,'v1_GUID':'00000000-0000-0000-0000-000000000000',
    'v2_Condition':False,'_StartFrame':12.5,'_EndFrame':24.5}
for n in verified.bhvt.nodes:
    for child in n.children:
        verified.references.node_index(child.id_hash,child.ex_id)
        verified.references.object_instance('conditions',child.condition_id)
    for a in n.actions: verified.references.action(a.id_hash,a.ex_id)
    for s in n.states:
        verified.references.state_target(s.mTransitions)
        verified.references.object_instance('conditions',s.TransitionConditions)
assert verified.rebuild() == output
assert TARGET.read_bytes() == source
folder.mkdir(parents=True, exist_ok=True)
candidate = folder / TARGET.name
candidate.write_bytes(output)
manifest = dict(target=str(TARGET),candidate=str(candidate),source_sha256=source_hash,
    candidate_sha256=hashlib.sha256(output).hexdigest(),source_size=len(source),candidate_size=len(output),
    node_id=f'0x{new_id:08X}',node_name=new_name,node_index=new_node_index,
    condition_id=f'0x{condition_id:08X}',condition_class=condition.class_name,
    condition_instance=new_instance,condition_object_index=new_object,
    action_id='0x7C152E99',action_count=9786,node_count=len(verified.bhvt.nodes),
    condition_count=len(verified.rsz_blocks.get_block('conditions').object_table),
    wait_action_count=len(wait.actions),new_node_action_count=1,
    condition_values={'Condition':False,'_StartFrame':12.5,'_EndFrame':24.5})
(folder/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
print(json.dumps(manifest,indent=2))
