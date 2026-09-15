"""Generate the MHRise LongSword Action fixture verified in a live game.

Usage: python tools/motfsm_validation/new_action_fixture.py SOURCE OUTPUT_DIRECTORY
SOURCE is the original LongSword sample identified by the SHA-256 below.
The output directory receives the candidate and its manifest; SOURCE is unchanged.
Load reasy_longsword_action_check.lua after installing/reloading the candidate.
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
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from file_handlers.motfsm.motfsm_file import MotfsmFile, Action
from file_handlers.motfsm.rebuild import BhvtPointers
from file_handlers.motfsm.rsz_adapter import BLOCK_NAMES
from test_motfsm_rebuild import variable_snapshot

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('source', type=Path)
parser.add_argument('output_directory', type=Path)
args = parser.parse_args()
target = args.source.resolve()
folder = args.output_directory.resolve()
assert folder / target.name != target, 'Output must not replace the source'
source = target.read_bytes()
digest = hashlib.sha256(source).hexdigest()
assert digest == 'a47dda25415b4bba349b36d39cb807428edd6e32dfa33db81a15d96cc7abe048', 'Input is not the original LongSword validation sample'
doc = MotfsmFile()
doc.read(source)
node_index = 1
node = doc.bhvt.nodes[node_index]
assert node.name == 'wait'
original_actions = node.actions[:]
template = doc.references.action(node.actions[0].id_hash, node.actions[0].ex_id)
assert template.class_name == 'snow.player.fsm.PlayerFsm2ActionDogRideAccess'
assert [(f.name, f.type_name) for f in template.fields] == [('v0_Enabled', 'Bool'), ('v1_ID', 'U32')]
new_id = zlib.crc32(b'REasy.Longsword.wait.extraDogRideAccess.20260915')
assert new_id not in (0, 0xFFFFFFFF)
assert all(key[0] != new_id for key in doc.references.action_identities())
block = doc.rsz_blocks.get_block('actions')
native = block.file
original_instance_count = len(native.instance_infos)
original_object_table = list(native.object_table)
baseline = native.build_validated()
assert source[block.offset:block.end] == baseline + bytes(block.end - block.offset - len(baseline)), 'Baseline native rebuild changed data'
new_index = len(native.instance_infos)
native.instance_infos.append(copy.deepcopy(native.instance_infos[template.index]))
native.parsed_elements[new_index] = copy.deepcopy(native.parsed_elements[template.index])
native.parsed_elements[new_index]['v1_ID'].value = new_id
native.object_table.append(new_index)
new_rsz = native.build_validated()
new_rsz += bytes((-len(new_rsz)) % 16)

# Rewrite the two Action columns and the parallel global extension-ID table.
actions = original_actions + [Action(new_id, 0)]
action_data = struct.pack('<I', len(actions))
action_data += struct.pack(f'<{len(actions)}I', *(a.id_hash for a in actions))
action_data += struct.pack(f'<{len(actions)}I', *(a.ex_id for a in actions))
extensions = doc.bhvt.action_ex_ids
ext_start = doc.bindings.get(extensions, 0).offset - 4
ext_end = ext_start + 4 + len(extensions) * 4
assert struct.unpack_from('<I', source, ext_start)[0] == len(original_object_table)
ext_data = struct.pack(f'<{len(extensions) + 2}I', len(extensions) + 1, *extensions, 0)
node_end = doc.bhvt.node_data_end
tail = min(offset for offset in doc.bhvt.offsets.values() if offset >= node_end)
assert tail == block.offset
growth = len(action_data) - (node._action_span[1] - node._action_span[0]) + len(ext_data) - (ext_end - ext_start)
padding = bytes((tail - node_end - growth) % 16)
splices = [(*node._action_span, action_data), (ext_start, ext_end, ext_data),
           (node_end, tail, padding), (block.offset, block.end, new_rsz)]
splices.sort()
output = bytearray()
cursor = 0
for start, end, payload in splices:
    assert cursor <= start <= end
    output.extend(source[cursor:start])
    output.extend(payload)
    cursor = end
output.extend(source[cursor:])

def relocate(offset):
    delta = 0
    for start, end, payload in splices:
        if offset < start:
            break
        if offset == start and start != end:
            return offset + delta
        if offset < end:
            raise ValueError(f'Pointer inside replaced data at 0x{offset:X}')
        delta += len(payload) - (end - start)
    return offset + delta

base = doc.tree_data_offset
for slot in BhvtPointers(doc).collect():
    value = struct.unpack_from('<Q', source, slot)[0]
    struct.pack_into('<Q', output, relocate(slot), relocate(base + value) - relocate(base) if value else 0)
for slot in (16, 24, 32, 40):
    value = struct.unpack_from('<Q', source, slot)[0]
    struct.pack_into('<Q', output, slot, relocate(value) if value else 0)
struct.pack_into('<I', output, relocate(doc.tree_info_ptr), relocate(base + doc.tree_data_size) - relocate(base))
output = bytes(output)

# Reparse the candidate independently and compare all existing native data.
original = MotfsmFile()
original.read(source)
verified = MotfsmFile()
verified.read(output)
assert len(output) > len(source)
expected_nodes = copy.deepcopy(original.bhvt.nodes)
expected_nodes[node_index].actions = actions
assert verified.bhvt.nodes == expected_nodes
assert verified.bhvt.action_ex_ids == original.bhvt.action_ex_ids + [0]
assert verified.bhvt.static_action_ex_ids == original.bhvt.static_action_ex_ids
for name in BLOCK_NAMES:
    old_block = original.rsz_blocks.get_block(name)
    new_block = verified.rsz_blocks.get_block(name)
    if name != 'actions':
        assert source[old_block.offset:old_block.end] == output[new_block.offset:new_block.end], name
    else:
        assert new_block.object_table == original_object_table + [new_index]
        assert new_block.instance_count == original_instance_count + 1
        for i in range(1, original_instance_count):
            old_instance, new_instance = old_block.get_instance(i), new_block.get_instance(i)
            assert old_instance.type_id == new_instance.type_id
            assert old_instance.class_name == new_instance.class_name
            if old_instance.start_offset is not None:
                assert source[old_instance.start_offset:old_instance.end_offset] == output[new_instance.start_offset:new_instance.end_offset], i
    print(name, new_block.instance_count, 'verified', flush=True)
assert variable_snapshot(verified) == variable_snapshot(original)
new_action = verified.references.action(new_id, 0)
assert new_action.index == new_index and new_action.class_name == template.class_name
assert [(f.name, f.value) for f in new_action.fields] == [('v0_Enabled', True), ('v1_ID', new_id)]
for identity in {(a.id_hash, a.ex_id) for n in verified.bhvt.nodes for a in n.actions}:
    assert verified.references.action(*identity) is not None
assert verified.rebuild() == output
assert target.read_bytes() == source, 'Live file changed during preparation'
folder.mkdir(parents=True, exist_ok=True)
candidate = folder / target.name
candidate.write_bytes(output)
manifest = dict(target=str(target), candidate=str(candidate), original_sha256=digest,
                candidate_sha256=hashlib.sha256(output).hexdigest(), original_size=len(source),
                candidate_size=len(output), action_id=f'0x{new_id:08X}', action_ex_id=0,
                new_instance_index=new_index, new_object_index=len(original_object_table),
                original_instances=original_instance_count, new_instances=original_instance_count+1,
                action_class=template.class_name, node_index=node_index, node_name=node.name,
                original_node_actions=len(original_actions), new_node_actions=len(actions))
(folder / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
print(json.dumps(manifest, indent=2))
