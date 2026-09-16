"""Point the imported FSM node at an RCOL request set id.

The BHVT hit action (`snow.player.fsm.PlayerHitAction2._hitIndex`) selects the RCOL
request set *id*, verified against the natives: atk_125 plays the Spirit Roundslash
(気刃大回旋斬) motion 125 and references the RCOL request set 気刃斬りフィニッシュ, whose
id is 32 -- `field0` there is 28, so the id (not field0, not the table position) is
what the engine resolves.

usage: python point_fsm_hit_request.py FSM OUTDIR --node atk_620 --request 72
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
sys.setrecursionlimit(100000)

from file_handlers.motfsm.motfsm_file import MotfsmFile                       # noqa: E402
from file_handlers.motfsm.graph_model import MotfsmGraph                      # noqa: E402
from types import SimpleNamespace                                             # noqa: E402

HIT_ACTION = 'snow.player.fsm.PlayerHitAction2'

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('fsm', type=Path)
parser.add_argument('output_directory', type=Path)
parser.add_argument('--node', default='atk_620')
parser.add_argument('--request', type=int, required=True, help='new RCOL request set id')
parser.add_argument('--expect', type=int, default=None, help='current _hitIndex, asserted when given')
args = parser.parse_args()
SOURCE = args.fsm.resolve()
source = SOURCE.read_bytes()

fsm = MotfsmFile()
fsm.read(source)
graph = MotfsmGraph(SimpleNamespace(bhvt=fsm.bhvt, references=fsm.references))
matches = [index for index, node in enumerate(graph.nodes) if node.name == args.node]
assert len(matches) == 1, f'{args.node} resolves to {len(matches)} nodes'
node = graph.nodes[matches[0]]
hit = None
for position, reference in enumerate(node.actions):
    instance = fsm.references.action(reference.id_hash, reference.ex_id)
    if instance is not None and instance.class_name == HIT_ACTION:
        hit = (position, instance, reference)
assert hit is not None, f'{args.node} has no {HIT_ACTION}'
position, instance, reference = hit
field = next((item for item in instance.fields if item.name == '_hitIndex'), None)
assert field is not None, [item.name for item in instance.fields]
binding = field.binding
assert binding is not None, f'{field.name} is not a fixed-size scalar ({field.type})'
print(f'{args.node} action[{position}] identity=0x{reference.id_hash:08X}:{reference.ex_id} '
      f'_hitIndex {binding.value} -> {args.request}  ({field.type_name}/{binding.type_name} @ 0x{binding.offset:X}, '
      f'{binding.size} bytes, original {binding.original_bytes.hex(" ")})')
if args.expect is not None:
    assert binding.value == args.expect, f'expected _hitIndex {args.expect}, found {binding.value}'
if binding.value == args.request:
    print('already pointing at the requested set')
else:
    binding.set_value(args.request)
output = fsm.rebuild()

diff = [index for index in range(min(len(source), len(output))) if source[index] != output[index]]
ranges = []
for index in diff:
    if ranges and index <= ranges[-1][1] + 1:
        ranges[-1][1] = index
    else:
        ranges.append([index, index])
print(f'rebuild: {len(source):,} -> {len(output):,} B, {len(diff)} byte(s) changed in {len(ranges)} range(s)')
for low, high in ranges:
    print(f'   0x{low:08X}-0x{high:08X} ({high-low+1} B)  {source[low:high+1].hex(" ")} -> {output[low:high+1].hex(" ")}')
assert len(ranges) == 1 and ranges[0][0] == binding.offset, 'the edit must touch only the bound field'
assert source[binding.offset:binding.offset+binding.size] == binding.original_bytes

reopened = MotfsmFile()
reopened.read(output)
reopened_graph = MotfsmGraph(SimpleNamespace(bhvt=reopened.bhvt, references=reopened.references))
node = reopened_graph.nodes[[i for i, n in enumerate(reopened_graph.nodes) if n.name == args.node][0]]
verdict = None
for reference in node.actions:
    instance = reopened.references.action(reference.id_hash, reference.ex_id)
    if instance is not None and instance.class_name == HIT_ACTION:
        verdict = next(item.value for item in instance.fields if item.name == '_hitIndex')
print(f're-parsed {args.node} _hitIndex = {verdict}')
assert verdict == args.request, verdict
assert reopened.rebuild() == output, 'not stable'

folder = args.output_directory.resolve()
folder.mkdir(parents=True, exist_ok=True)
candidate = folder / SOURCE.name
candidate.write_bytes(output)
(folder / 'manifest.json').write_text(json.dumps(dict(
    source=str(SOURCE), source_sha256=hashlib.sha256(source).hexdigest(), source_size=len(source),
    candidate=str(candidate), candidate_sha256=hashlib.sha256(output).hexdigest(), candidate_size=len(output),
    node=args.node, request_set_id=args.request, field=field.name, field_offset=binding.offset,
    field_size=binding.size, changed_bytes=len(diff)), indent=2), encoding='utf-8')
print(f'wrote {candidate}')
