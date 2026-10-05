"""Offline corpus proof for the public native FSM writer.

Usage: python tools/motfsm_validation/native_graph_writer.py OUTPUT SOURCE...
Only OUTPUT receives files. Input resources are never replaced or deployed.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from file_handlers.motfsm.motfsm_file import MotfsmFile, BHVTNode, Action
from file_handlers.motfsm.rsz_adapter import BLOCK_NAMES, native_value_state
from file_handlers.motfsm.validation import variable_snapshot


def fresh_identity(existing):
    used = {value & 0xFFFFFFFF for value in existing} | {0, 0xFFFFFFFF}
    identity = max(used - {0xFFFFFFFF}) + 1
    if identity >= 0xFFFFFFFF:
        raise ValueError('Fixture identity space is exhausted')
    return identity


def exercise(path, output):
    source = path.read_bytes()
    doc = MotfsmFile()
    doc.read(source)
    assert doc.build_validated() == source
    variables = variable_snapshot(doc)
    originals = copy.deepcopy(doc.bhvt.nodes)
    original_blocks = {}
    for name in BLOCK_NAMES:
        native = doc.rsz_blocks.get_block(name).file
        original_blocks[name] = (len(native.instance_infos), list(native.object_table),
                                 native_value_state(native.parsed_elements))
    names = doc.bhvt.string_pool
    editor = doc.graph_editor()
    root = next(i for i, n in enumerate(doc.bhvt.nodes) if n.parent == 0xFFFFFFFF)
    owned = []
    block = doc.rsz_blocks.get_block('actions')
    if not block.object_table:
        raise ValueError('Corpus fixture requires an Action template')
    identity = fresh_identity(key[0] for key in doc.references.action_identities())
    raw = editor.clone_object('actions', 0, identity=identity)
    action = editor.action_reference('actions', raw)
    owned.append(('actions', raw))
    condition = -1
    for name in ('conditions', 'static_conditions'):
        block = doc.rsz_blocks.get_block(name)
        if block.object_table:
            raw = editor.clone_object(name, 0)
            owned.append((name, raw))
            condition = raw
            break
    node = BHVTNode(id_hash=fresh_identity(n.id_hash for n in doc.bhvt.nodes),
                    name='REasy native graph verification', node_attribute=0x20, actions=[action])
    index = editor.add_node(node, root, condition)
    editor.connect_state(index, index, condition=condition)
    editor.connect_all(index, index, condition=condition)
    # An ambiguous event encoding stays empty until an explicit layout is chosen.
    if doc.layout.transition_event_lists is False:
        editor.connect_start(index, index, condition=condition, events=[0])
    else:
        editor.connect_start(index, index, condition=condition)
    candidate = doc.build_validated()
    assert doc.node_count == len(originals) + 1
    assert candidate != source
    reopened = MotfsmFile()
    reopened.read(candidate)
    assert reopened.bhvt.nodes[:-1] == doc.bhvt.nodes[:-1]
    assert variable_snapshot(reopened) == variables
    for name, (count, objects, fields) in original_blocks.items():
        native = reopened.rsz_blocks.get_block(name).file
        assert native.object_table[:len(objects)] == objects, name
        assert native_value_state({i: f for i, f in native.parsed_elements.items() if i < count}) == fields, name
    output.mkdir(parents=True, exist_ok=True)
    destination = output / path.name
    if destination.resolve() == path.resolve():
        raise ValueError('Output must not replace a source')
    destination.write_bytes(candidate)
    editor.remove_node(index)
    for name, raw in reversed(owned):
        editor.remove_object(name, raw)
    doc.bhvt.string_pool = names
    assert doc.bhvt.nodes == originals
    assert doc.build_validated() == source
    # The transactional boundary refuses damage without publishing a candidate.
    try:
        with editor.transaction() as edit:
            edit.document.bhvt.nodes[0].actions.append(Action(fresh_identity(
                key[0] for key in edit.document.references.action_identities()), 0))
    except ValueError as exc:
        assert 'Unknown action identity' in str(exc), str(exc)
        refusal = str(exc)
    else:
        raise AssertionError('Dangling Action identity was accepted')
    assert doc.build_validated() == source
    assert path.read_bytes() == source
    return dict(source=str(path), candidate=str(destination), source_sha256=hashlib.sha256(source).hexdigest(),
                candidate_sha256=hashlib.sha256(candidate).hexdigest(), nodes=len(originals),
                bytes_added=len(candidate)-len(source), baseline_exact=True, candidate_validated=True,
                undo_exact=True, damaged_reference_rejected=refusal, deployed=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('sources', nargs='+', type=Path)
    args = parser.parse_args()
    reports, failures = [], []
    for path in args.sources:
        try:
            report = exercise(path, args.output)
            reports.append(report)
            print(f'{path.name}: {report["nodes"]} nodes, +{report["bytes_added"]} bytes, verified/undo exact', flush=True)
        except (ValueError, IndexError, AssertionError) as exc:
            failures.append(dict(source=str(path), error=str(exc)))
            print(f'{path.name}: FAILED: {exc}', file=sys.stderr, flush=True)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'report.json').write_text(json.dumps(dict(files=reports, failures=failures), indent=2), encoding='utf-8')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
