"""Public CLI contracts exercised against native FSMs, plus selector/format regression cases."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from file_handlers.motfsm.fields import FieldBinding
from file_handlers.motfsm.motfsm_file import MotfsmFile, BHVTNode
from file_handlers.motfsm.motfsm_viewer import MotfsmViewer
from file_handlers.motfsm.references import References
from file_handlers.motfsm.serialization import serialize_nodes
from tools.fsm.cli import main, build_parser, STRUCTURAL, SCALAR
from tools.fsm.common import resolve_node, identity, integer
from test_motfsm_corpus import CORPUS, unique_files

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    doc = MotfsmFile()
    doc.read(path.read_bytes())
    return doc


class SelectorAndDisplayTests(unittest.TestCase):
    def test_selectors_disambiguate_name_hash_extension_and_path(self):
        nodes = [BHVTNode(name='root', id_hash=0, parent=0xFFFFFFFF),
                 BHVTNode(name='same', id_hash=0x81234567, ex_id=1),
                 BHVTNode(name='same', id_hash=0x81234567, ex_id=2),
                 BHVTNode(name='leaf', id_hash=7, parent=0x81234567, parent_ex=2)]
        doc = SimpleNamespace(bhvt=SimpleNamespace(nodes=nodes))
        doc.references = References(doc)
        for selector in ('same', '0x81234567'):
            with self.assertRaisesRegex(ValueError, 'resolved to 2'):
                resolve_node(doc, selector)
        self.assertEqual(resolve_node(doc, '0x81234567:2'), 2)
        self.assertEqual(resolve_node(doc, 'index:3'), 3)
        self.assertEqual(resolve_node(doc, 'path:root.same.leaf'), 3)
        with self.assertRaises(ValueError):
            resolve_node(doc, 'index:-1')

    def test_unsigned_values_are_decimal_except_identity_hashes(self):
        for name, kind, value, expected in [
            ('v4_MotionID', 'u32', 620, '620'), ('_weaponBankID', 'u32', 100, '100'),
            ('TransitionMaps', 'u32', 123, '123'), ('ex_id', 'u32', 2, '2'),
            ('priority', 's32', -10, '-10'), ('node_attribute', 'u16', 32, '32'),
            ('id_hash', 'u32', 0xDEADBEEF, '0xDEADBEEF'),
            ('mTransitions', 'u32', 0x1234, '0x00001234'),
            ('v1_ID', 'u32', 0x1234, '0x00001234'),
        ]:
            import struct
            from file_handlers.motfsm.fields import SCALAR_FORMATS
            owner = SimpleNamespace(value=value)
            binding = FieldBinding(name, kind, owner, 'value', 0, struct.pack('<' + SCALAR_FORMATS[kind], value))
            with self.subTest(field=name):
                self.assertEqual(MotfsmViewer._field_text(binding), expected)
                self.assertEqual(binding.parse(expected), value)
        self.assertEqual(integer('0010'), 10)
        self.assertEqual(integer('0x10'), 16)

    def test_every_command_has_help_without_reading_a_file(self):
        parser = build_parser()
        for name in (*STRUCTURAL, *SCALAR, 'query', 'dump', 'batch'):
            with self.subTest(command=name), redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    parser.parse_args([name, '--help'])
                self.assertEqual(caught.exception.code, 0)


@unittest.skipUnless(CORPUS.is_dir(), 'MHRise corpus not installed')
class NativeCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = next(unique_files())
        cls.doc = read(cls.path)
        for i, node in enumerate(cls.doc.bhvt.nodes):
            for position, ref in enumerate(node.actions):
                instance = cls.doc.references.action(ref.id_hash, ref.ex_id)
                if instance and instance.class_name == 'snow.PlayerPlayMotion2':
                    cls.node_index, cls.position, cls.instance = i, position, instance
                    cls.selector = identity(node)
                    return
        raise AssertionError('Native corpus has no motion Action')

    def invoke(self, arguments, expected=0):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([*map(str, arguments), '--json'])
        self.assertEqual(code, expected, err.getvalue() + out.getvalue())
        return json.loads(out.getvalue())

    def test_all_native_node_tables_roundtrip_shared_serializer(self):
        for path in unique_files():
            with self.subTest(file=path.name):
                doc = read(path)
                self.assertEqual(serialize_nodes(doc), doc.source[doc.bhvt.offsets['nodes']:doc.bhvt.node_data_end])

    def test_query_json_stdout_and_action_fields(self):
        result = subprocess.run([sys.executable, '-m', 'tools.fsm', 'dump', str(self.path),
                                 '--node', self.selector, '--json'], cwd=ROOT, capture_output=True,
                                text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)['nodes'][0]
        self.assertEqual(record['identity'], self.selector)
        action = record['actions'][self.position]['instance']
        self.assertIsInstance(action['fields']['v4_MotionID'], int)
        self.assertIn('v4_MotionID', action['editable_fields'])
        self.assertIn('incoming', record)

    def test_scalar_edit_changes_only_native_binding_and_noop_succeeds(self):
        original = self.path.read_bytes()
        field = next(f for f in self.instance.fields if f.name == 'v4_MotionID')
        value = int(field.value) + 1
        with tempfile.TemporaryDirectory() as folder:
            candidate = Path(folder) / self.path.name
            argv = ['action-edit', self.path, '-o', candidate, '--node', self.selector,
                    '--class', self.instance.class_name, '--exact', '--position', self.position,
                    '--set', f'v4_MotionID={value}']
            result = self.invoke(argv)
            self.assertTrue(result['changed'])
            expected = bytearray(original)
            expected[field.offset:field.offset + field.size] = field.binding.encode(value)
            self.assertEqual(candidate.read_bytes(), expected)
            noop = Path(folder) / 'noop' / self.path.name
            argv[1], argv[3] = candidate, noop
            result = self.invoke(argv)
            self.assertFalse(result['changed'])
            self.assertEqual(candidate.read_bytes(), noop.read_bytes())
        self.assertEqual(self.path.read_bytes(), original)

    def test_structural_action_clone_uses_native_reference_and_preserves_nodes(self):
        node = self.doc.bhvt.nodes[self.node_index]
        with tempfile.TemporaryDirectory() as folder:
            candidate = Path(folder) / self.path.name
            self.invoke(['action-add', self.path, '-o', candidate, '--node', self.selector,
                         '--from', self.selector, '--class', 'PlayerPlayMotion2', '--exact',
                         '--set', 'v4_MotionID=620'])
            doc = read(candidate)
            added = doc.bhvt.nodes[self.node_index].actions[-1]
            self.assertNotIn((added.id_hash, added.ex_id), self.doc.references.action_identities())
            instance = doc.references.action(added.id_hash, added.ex_id)
            fields = {f.name: f.value for f in instance.fields}
            self.assertEqual(fields['v4_MotionID'], 620)
            self.assertEqual(doc.bhvt.nodes[self.node_index].actions[:-1], node.actions)
            for i, previous in enumerate(self.doc.bhvt.nodes):
                if i != self.node_index:
                    self.assertEqual(doc.bhvt.nodes[i], previous)
            self.assertEqual(doc.rebuild(), candidate.read_bytes())

    def test_batch_dry_run_and_failure_publish_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / self.path.name
            plan = Path(folder) / 'plan.json'
            edit = ['action-edit', '--node', self.selector, '--class', self.instance.class_name,
                    '--exact', '--position', str(self.position), '--set', 'v4_MotionID=620']
            plan.write_text(json.dumps([edit]), encoding='utf-8')
            argv = ['batch', self.path, '-o', output, '--plan', plan]
            self.assertTrue(self.invoke([*argv, '--dry-run'])['dry_run'])
            self.assertFalse(output.exists())
            self.invoke(argv)
            saved = output.read_bytes()
            plan.write_text(json.dumps([edit, ['action-edit', '--node', self.selector,
                                             '--class', self.instance.class_name, '--set', 'missing=1']]), encoding='utf-8')
            self.assertEqual(self.invoke(argv, 2)['status'], 'error')
            self.assertEqual(output.read_bytes(), saved)
            self.assertEqual(sorted(p.name for p in Path(folder).iterdir()), sorted([output.name, plan.name]))

    def test_source_overwrite_and_invalid_assignments_fail(self):
        args = ['action-edit', self.path, '-o', self.path, '--node', self.selector,
                '--class', self.instance.class_name, '--set', 'v4_MotionID=620']
        self.assertIn('replace the source', self.invoke(args, 2)['error'])
        args[-1] = 'missing-separator'
        self.assertEqual(self.invoke(args, 2)['status'], 'error')

    def test_native_state_commands_and_event_reference(self):
        doc = self.doc
        choices = []
        event_choice = None
        for index, node in enumerate(doc.bhvt.nodes):
            for position, state in enumerate(node.states):
                target = doc.references.state_target(state.mTransitions)
                condition = doc.references.object_instance('conditions', state.TransitionConditions)
                if target is not None and condition is not None:
                    if sum(s.mTransitions == state.mTransitions for s in node.states) == 1:
                        choices.append((index, position, target, condition))
                if state.mStates.values and event_choice is None:
                    event_choice = (index, position)
        self.assertTrue(choices)
        index, position, target, condition = choices[0]
        selector = identity(doc.bhvt.nodes[index])
        target_selector = identity(doc.bhvt.nodes[target])
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / self.path.name
            self.invoke(['state-own-map', self.path, '-o', output, '--node', selector, '--target', target_selector])
            saved = read(output)
            self.assertNotEqual(saved.bhvt.nodes[index].states[position].TransitionMaps,
                                doc.bhvt.nodes[index].states[position].TransitionMaps)
            self.invoke(['state-condition', self.path, '-o', output, '--node', selector,
                         '--target', target_selector, '--template-node', selector, '--template-state', position])
            saved = read(output)
            new_condition = saved.references.object_instance('conditions', saved.bhvt.nodes[index].states[position].TransitionConditions)
            self.assertEqual(new_condition.class_name, condition.class_name)
            other_target = next(t for _, _, t, _ in choices if t != target and
                                all(s.mTransitions != doc.bhvt.nodes[t].id_hash for s in doc.bhvt.nodes[index].states))
            self.invoke(['state-target', self.path, '-o', output, '--node', selector,
                         '--state', position, '--target', identity(doc.bhvt.nodes[other_target])])
            self.assertEqual(read(output).bhvt.nodes[index].states[position].mTransitions,
                             doc.bhvt.nodes[other_target].id_hash)
            self.invoke(['state-add', self.path, '-o', output, '--node', selector,
                         '--target', identity(doc.bhvt.nodes[other_target]), '--template-state', position, '--events', 'drop'])
            saved = read(output)
            self.assertEqual(len(saved.bhvt.nodes[index].states), len(doc.bhvt.nodes[index].states) + 1)
            self.assertEqual(saved.transition_map_count, doc.transition_map_count + 1)
            self.assertIsNotNone(event_choice)
            owner, state_position = event_choice
            original_raw = doc.bhvt.nodes[owner].states[state_position].mStates.values[0]
            original_event = doc.references.object_instance('transition_events', original_raw)
            self.invoke(['state-add-event', self.path, '-o', output, '--node', selector, '--state', position,
                         '--from-node', identity(doc.bhvt.nodes[owner]), '--from-state', state_position])
            saved = read(output)
            new_raw = saved.bhvt.nodes[index].states[position].mStates.values[-1]
            new_event = saved.references.object_instance('transition_events', new_raw)
            self.assertEqual(new_event.class_name, original_event.class_name)
            self.assertNotEqual(new_raw, original_raw)

    def test_node_clone_recipes_preserve_fields_and_map_ownership(self):
        node = self.doc.bhvt.nodes[self.node_index]
        parent_index = self.doc.references.parent_index(node)
        self.assertIsNotNone(parent_index)
        with tempfile.TemporaryDirectory() as folder:
            for command in ('node-clone', 'attack-clone'):
                with self.subTest(command=command):
                    output = Path(folder) / command / self.path.name
                    args = [command, self.path, '-o', output, '--parent', identity(self.doc.bhvt.nodes[parent_index]),
                            '--name', 'cli_clone_candidate', '--motion', 620]
                    if command == 'node-clone':
                        args += ['--copy-node', self.selector, '--set-effect', 0]
                    else:
                        args += ['--template', self.selector, '--keep-hit-index', 0]
                    self.invoke(args)
                    doc = read(output)
                    clone = doc.bhvt.nodes[-1]
                    self.assertEqual(clone.name, 'cli_clone_candidate')
                    self.assertEqual(len(doc.bhvt.nodes), len(self.doc.bhvt.nodes) + 1)
                    self.assertEqual([s.mTransitions for s in clone.states], [s.mTransitions for s in node.states])
                    before_maps = {s.TransitionMaps for n in self.doc.bhvt.nodes for s in n.states}
                    new_maps = [s.TransitionMaps for s in clone.states]
                    self.assertEqual(len(set(new_maps)), len(new_maps))
                    self.assertTrue(set(new_maps).isdisjoint(before_maps))
                    self.assertEqual(doc.transition_map_count, self.doc.transition_map_count + len(clone.states))
                    self.assertEqual(doc.rebuild(), output.read_bytes())

    def test_condition_edit_resolves_static_block_and_changes_only_bound_field(self):
        for index, node in enumerate(self.doc.bhvt.nodes):
            for position, state in enumerate(node.states):
                if (state.TransitionConditions & 0xFFFFFFFF) >> 24 != 0x40:
                    continue
                instance = self.doc.references.object_instance('conditions', state.TransitionConditions)
                binding = next((f.binding for f in instance.fields if f.binding and f.binding.type_name == 'bool'), None)
                if binding is None:
                    continue
                with tempfile.TemporaryDirectory() as folder:
                    output = Path(folder) / self.path.name
                    self.invoke(['condition-edit', self.path, '-o', output, '--node', identity(node),
                                 '--states', position, '--allow-shared', '--set', f'{binding.name}={not binding.value}'])
                    expected = bytearray(self.path.read_bytes())
                    expected[binding.offset:binding.offset + binding.size] = binding.encode(not binding.value)
                    self.assertEqual(output.read_bytes(), expected)
                return
        self.fail('Native corpus has no static condition with a boolean field')

    def test_chainsaw_recipe_and_command_argument_do_not_override_cli_dispatch(self):
        path = next(p for p in unique_files() if p.name.startswith('ChargeAxe.'))
        doc = read(path)
        template = next(node for node in doc.bhvt.nodes
                        if {'chainsaw_run', 'chainsaw_end', 'normal'} <= {
                            doc.bhvt.nodes[doc.references.node_index(c.id_hash, c.ex_id)].name for c in node.children})
        target = next(node for node in doc.bhvt.nodes if not node.children and node.is_fsm and
                      any(doc.references.action(a.id_hash, a.ex_id).class_name == 'snow.PlayerPlayMotion2'
                          for a in node.actions if a.id_hash not in (0, 0xFFFFFFFF)))
        target_index = resolve_node(doc, identity(target))
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / path.name
            result = self.invoke(['node-chainsaw', path, '-o', output, '--node', identity(target),
                                  '--template-node', identity(template), '--command', 0])
            self.assertEqual(result['command'], 'node-chainsaw')
            saved = read(output)
            after = saved.bhvt.nodes[target_index]
            self.assertNotEqual(after.selector_id, -1)
            children = {saved.bhvt.nodes[saved.references.node_index(c.id_hash, c.ex_id)].name
                        for c in after.children}
            self.assertTrue({'chainsaw_run', 'chainsaw_end'} <= children)
            self.assertEqual(saved.transition_map_count, doc.transition_map_count + 1)


if __name__ == '__main__':
    unittest.main()
