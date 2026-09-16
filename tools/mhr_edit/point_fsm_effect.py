"""Point an FSM node's SetEffect action at an RCOL/PFB element and frame.

``snow.player.fsm.PlayerFsm2ActionSetEffect`` carries ``containerID``, ``_ElementID`` (the effect
element id inside the *prg* provider PFB) and ``_Frame`` (motion-relative trigger frame).  The
fields are fixed-size, so the edit is a handful of bytes; the tool asserts that only those bytes
changed.

usage: python point_fsm_effect.py FSM OUTDIR --node atk_620 [--element 99] [--frame 40]
                                           [--container 150] [--expect-element 100]
"""
import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
sys.setrecursionlimit(100000)

from file_handlers.motfsm.motfsm_file import MotfsmFile                       # noqa: E402
from file_handlers.motfsm.graph_model import MotfsmGraph                      # noqa: E402

SET_EFFECT = 'snow.player.fsm.PlayerFsm2ActionSetEffect'

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('fsm', type=Path)
parser.add_argument('output_directory', type=Path)
parser.add_argument('--node', default='atk_620')
parser.add_argument('--element', type=int, help='new _ElementID')
parser.add_argument('--frame', type=float, help='new _Frame')
parser.add_argument('--container', type=int, help='new containerID')
parser.add_argument('--expect-element', type=int)
parser.add_argument('--position', type=int, help='action index on the node (default: first SetEffect)')
args = parser.parse_args()
SOURCE = args.fsm.resolve()
source = SOURCE.read_bytes()

fsm = MotfsmFile()
fsm.read(source)
graph = MotfsmGraph(SimpleNamespace(bhvt=fsm.bhvt, references=fsm.references))
matches = [index for index, node in enumerate(graph.nodes) if node.name == args.node]
assert len(matches) == 1, f'{args.node} resolves to {len(matches)} nodes'
node = graph.nodes[matches[0]]
found = [(position, fsm.references.action(reference.id_hash, reference.ex_id))
         for position, reference in enumerate(node.actions)
         if fsm.references.action(reference.id_hash, reference.ex_id) is not None
         and fsm.references.action(reference.id_hash, reference.ex_id).class_name == SET_EFFECT]
assert found, f'{args.node} has no {SET_EFFECT}'
if args.position is None:
    assert len(found) == 1, f'{args.node} has {len(found)} SetEffect actions; pass --position'
    position, instance = found[0]
else:
    position, instance = next(item for item in found if item[0] == args.position)
fields = {field.name: field for field in instance.fields}
print(f'{args.node} action[{position}]: containerID={fields["containerID"].value} '
      f'_ElementID={fields["_ElementID"].value} _Frame={fields["_Frame"].value}')
if args.expect_element is not None:
    assert fields['_ElementID'].value == args.expect_element, fields['_ElementID'].value

edits = []
for name, value in (('_ElementID', args.element), ('containerID', args.container)):
    if value is None:
        continue
    field = fields[name]
    binding = field.binding
    assert binding is not None, name
    if binding.value != value:
        edits.append((name, binding, binding.value, value))
if args.frame is not None:
    binding = fields['_Frame'].binding
    assert binding is not None, '_Frame'
    if abs(binding.value - args.frame) > 1e-6:
        edits.append(('_Frame', binding, binding.value, args.frame))
if not edits:
    print('nothing to change')
    raise SystemExit(0)
for name, binding, old, new in edits:
    print(f'  {name}: {old!r} -> {new!r}   ({binding.type_name} @0x{binding.offset:X}, {binding.size} B)')
    binding.set_value(new)
output = fsm.rebuild()

diff = [index for index in range(min(len(source), len(output))) if source[index] != output[index]]
ranges = []
for index in diff:
    if ranges and index <= ranges[-1][1] + 1:
        ranges[-1][1] = index
    else:
        ranges.append([index, index])
print(f'rebuild: {len(source):,} -> {len(output):,} B, {len(diff)} byte(s) in {len(ranges)} range(s)')
spans = sorted((binding.offset, binding.offset + binding.size - 1) for _name, binding, _old, _new in edits)
assert all(any(low <= position <= high for low, high in spans) for position in diff), (ranges, spans)

reopened = MotfsmFile()
reopened.read(output)
reopened_graph = MotfsmGraph(SimpleNamespace(bhvt=reopened.bhvt, references=reopened.references))
node = reopened_graph.nodes[[i for i, n in enumerate(reopened_graph.nodes) if n.name == args.node][0]]
for reference in node.actions:
    instance = reopened.references.action(reference.id_hash, reference.ex_id)
    if instance is not None and instance.class_name == SET_EFFECT:
        values = {field.name: field.value for field in instance.fields}
        print(f're-parsed: containerID={values["containerID"]} _ElementID={values["_ElementID"]} '
              f'_Frame={values["_Frame"]}')
        if args.element is not None:
            assert values['_ElementID'] == args.element
        if args.frame is not None:
            assert abs(values['_Frame'] - args.frame) < 1e-6
        if args.container is not None:
            assert values['containerID'] == args.container
        break
assert reopened.rebuild() == output, 'not stable'

folder = args.output_directory.resolve()
folder.mkdir(parents=True, exist_ok=True)
candidate = folder / SOURCE.name
candidate.write_bytes(output)
(folder / 'manifest.json').write_text(json.dumps(dict(
    source=str(SOURCE), source_sha256=hashlib.sha256(source).hexdigest(), source_size=len(source),
    candidate=str(candidate), candidate_sha256=hashlib.sha256(output).hexdigest(), candidate_size=len(output),
    node=args.node, action_position=position,
    container_id=args.container, element_id=args.element, frame=args.frame,
    changed=[[name, old, new] for name, _binding, old, new in edits]), indent=2), encoding='utf-8')
print(f'wrote {candidate}')
