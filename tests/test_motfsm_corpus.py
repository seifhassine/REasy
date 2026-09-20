"""Optional regression corpus: user-supplied MHRise files remain untracked."""
import gc
import hashlib
import math
import os
from pathlib import Path
import struct
import tempfile
import unittest
from collections import defaultdict

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from file_handlers.motfsm.fields import SCALAR_FORMATS
from file_handlers.motfsm.motfsm_file import MotfsmFile
from file_handlers.motfsm.motfsm_handler import MotfsmHandler
from file_handlers.motfsm.rsz_adapter import BLOCK_NAMES
from file_handlers.factory import get_handler_for_data

CORPUS = Path(__file__).parent / 'TESTFILE'


def unique_files():
    seen = set()
    for path in sorted(CORPUS.rglob('*.motfsm2.43')):
        digest = hashlib.sha256(path.read_bytes()).digest()
        if digest not in seen:
            seen.add(digest)
            yield path


@unittest.skipUnless(CORPUS.is_dir(), 'MHRise corpus not installed')
class MotfsmCorpusTests(unittest.TestCase):
    def test_all_files_parse_edit_save_and_reopen(self):
        paths = list(unique_files())
        self.assertTrue(paths, 'Corpus directory has no MHRise FSM files')
        totals = {'files': 0, 'blocks': 0, 'instances': 0, 'scalar_fields': 0}
        edited_blocks = set()
        for path in paths:
            with self.subTest(file=path.name), tempfile.TemporaryDirectory(prefix='reasy-fsm-test-') as folder:
                source = path.read_bytes()
                digest = hashlib.sha256(source).digest()
                doc = MotfsmFile()
                doc.read(source)
                expected = bytearray(source)
                checks = []
                for name in BLOCK_NAMES:
                    block = doc.rsz_blocks.get_block(name)
                    selected = None
                    for index in range(1, block.instance_count):
                        instance = block.get_instance(index)
                        for field in instance.fields:
                            if field.binding is None:
                                continue
                            binding = field.binding
                            value = struct.unpack_from('<' + SCALAR_FORMATS[binding.type_name], source, binding.offset)[0]
                            if isinstance(value, float) and math.isnan(value):
                                self.assertTrue(math.isnan(binding.value))
                            else:
                                self.assertEqual(binding.value, value, (name, index, field.name))
                            totals['scalar_fields'] += 1
                            if selected is None and binding.type_name == 'bool':
                                selected = (index, field.name, binding)
                    totals['instances'] += block.instance_count
                    totals['blocks'] += 1
                    if selected:
                        index, field_name, binding = selected
                        doc.edit_field(binding, not binding.value)
                        struct.pack_into('<?', expected, binding.offset, binding.value)
                        checks.append((name, index, field_name, binding.value))
                        edited_blocks.add(name)
                # Compare the fully parsed document before any node changes too.
                self.assertEqual(doc.rebuild(), bytes(expected))
                node = doc.bhvt.nodes[0]
                for field_name in ('priority', 'work_flags'):
                    binding = doc.bindings.get(node, field_name)
                    doc.edit_field(binding, binding.value ^ 1)
                    struct.pack_into('<' + SCALAR_FORMATS[binding.type_name], expected, binding.offset, binding.value)
                output = doc.rebuild()
                self.assertEqual(output, bytes(expected))
                self.assertEqual(len(output), len(source))
                saved = Path(folder) / path.name
                saved.write_bytes(output)
                reread = MotfsmFile()
                reread.read(saved.read_bytes())
                self.assertEqual(reread.bhvt.nodes[0].priority, node.priority)
                self.assertEqual(reread.bhvt.nodes[0].work_flags, node.work_flags)
                for name, index, field_name, value in checks:
                    instance = reread.rsz_blocks.get_block(name).get_instance(index)
                    field = next(f for f in instance.fields if f.name == field_name)
                    self.assertEqual(field.binding.value, value)
                self.assertEqual(reread.rebuild(), output)
                self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), digest)
                totals['files'] += 1
                print(f"{path.name}: parsed, edited, saved and reopened", flush=True)
                del doc, reread
            gc.collect()
        self.assertIn('expression_tree_conditions', edited_blocks)
        self.assertIn('static_expression_tree_conditions', edited_blocks)
        print('MOTFSM corpus:', totals, flush=True)

    def test_native_reference_tables_and_action_extension_ids(self):
        path = next(CORPUS.rglob('LongSword.motfsm2.43'))
        doc = MotfsmFile()
        doc.read(path.read_bytes())
        self.assertEqual(doc.rsz_blocks.get_block('actions').get_object(37).index, 39)
        condition = doc.references.object_instance('conditions', 0x40000000)
        self.assertEqual(condition.class_name, 'snow.player.fsm.PlayerFsm2ConditionIsAirouMatatabi')
        self.assertIsNone(doc.references.object_instance('conditions', -1))
        self.assertEqual(doc.references.node_index(0x015397CA, 0), 4475)
        self.assertEqual(doc.references.node_index(0x015397CA, 1), 4477)
        first = doc.references.action(0xA8B4AB13, 0)
        second = doc.references.action(0xA8B4AB13, 1)
        self.assertEqual(first.index, 9917)
        self.assertEqual(second.index, 9923)
        self.assertEqual(next(f.value for f in first.fields if f.name == 'v4_MotionID'), 570)
        self.assertEqual(next(f.value for f in second.fields if f.name == 'v4_MotionID'), 133)
        action = doc.references.action(0xBB2EEE95, 1)
        self.assertEqual(next(f.value for f in action.fields if f.name == 'actNo'), 398)
        self.assertEqual(next(f.value for f in action.fields if f.name == '_InterFrame'), 10.0)
        self.assertIsNotNone(doc.references.action(0x1683F002, 0))
        state = doc.bhvt.nodes[3].states[0]
        self.assertEqual(state.mStatesEx, 1)
        self.assertEqual(doc.references.state_target(state.mTransitions), 1)

    def test_all_native_references_resolve(self):
        for path in unique_files():
            with self.subTest(file=path.name):
                doc = MotfsmFile()
                doc.read(path.read_bytes())
                node_refs, state_refs, action_refs = set(), set(), set()
                object_refs = defaultdict(set)
                for node in doc.bhvt.nodes:
                    node_refs.add((node.parent, node.parent_ex))
                    node_refs.update((child.id_hash, child.ex_id) for child in node.children)
                    node_refs.update((tr.mStartState, tr.mStartStateEx) for tr in node.transitions)
                    state_refs.update(state.mTransitions for state in node.states)
                    action_refs.update((action.id_hash, action.ex_id) for action in node.actions)
                    object_refs['selectors'].update((node.selector_id, node.selector_caller_condition_id))
                    object_refs['selector_callers'].update(node.selector_callers)
                    object_refs['conditions'].update(child.condition_id for child in node.children)
                    object_refs['conditions'].update(state.TransitionConditions for state in node.states)
                    object_refs['conditions'].update(tr.mStartStateTransition for tr in node.transitions)
                    for state in node.states:
                        object_refs['transition_events'].update(state.mStates.values)
                    for tr in node.transitions:
                        object_refs['transition_events'].update(tr.mStartTransitionEvent.values)
                for id_hash, ex_id in node_refs:
                    if id_hash not in (0, 0xFFFFFFFF):
                        self.assertIsNotNone(doc.references.node_index(id_hash, ex_id))
                for id_hash in state_refs:
                    if id_hash not in (0, 0xFFFFFFFF):
                        self.assertIsNotNone(doc.references.state_target(id_hash))
                for id_hash, ex_id in action_refs:
                    if id_hash not in (0, 0xFFFFFFFF):
                        self.assertIsNotNone(doc.references.action(id_hash, ex_id))
                for block, values in object_refs.items():
                    for value in values:
                        target = doc.references.object_instance(block, value)
                        if value != -1:
                            self.assertIsNotNone(target)
                del doc
            gc.collect()

    def test_edited_reference_refreshes_expanded_view(self):
        app = QApplication.instance() or QApplication([])
        path = next(CORPUS.rglob('LongSword.motfsm2.43'))
        handler = MotfsmHandler()
        handler.read(path.read_bytes())
        viewer = handler.create_viewer()
        try:
            nodes = viewer.tree.topLevelItem(0).child(0)
            nodes.setExpanded(True)
            wait = nodes.child(1)
            wait.setExpanded(True)
            states = next(wait.child(i) for i in range(wait.childCount()) if wait.child(i).text(0).startswith('States ('))
            state_item = states.child(0)
            state_item.setExpanded(True)
            condition = next(state_item.child(i) for i in range(state_item.childCount()) if state_item.child(i).text(0) == 'TransitionConditions')
            link = condition
            link.setExpanded(True)
            self.assertIn('IsAirouMatatabi', link.text(1))
            condition.setText(1, '1073741825')
            app.processEvents()
            self.assertIn('IsChatAction', link.text(1))
            self.assertTrue(viewer.modified)
        finally:
            viewer.close()
            viewer.deleteLater()


@unittest.skipUnless((CORPUS / 'pl0100.motfsm2.31').is_file(), 'DMC5 sample not installed')
class Dmc5DetectionTests(unittest.TestCase):
    def test_detects_layout_and_registry_without_filename_hint(self):
        source = (CORPUS / 'pl0100.motfsm2.31').read_bytes()
        handler = get_handler_for_data(source, 'renamed.bin')
        handler.read(source)
        doc = handler.motfsm
        self.assertEqual(doc.node_count, 1543)
        self.assertFalse(doc.layout.transition_event_lists)
        self.assertFalse(doc.layout.transition_state_ex)
        self.assertEqual(Path(doc.type_registry.json_path).name, 'rszdmc5.json')
        self.assertEqual(doc.rebuild(), source)

    def test_affected_blocks_report_type_details(self):
        doc = MotfsmFile()
        doc.read((CORPUS / 'pl0100.motfsm2.31').read_bytes())
        for name in ('actions', 'conditions', 'expression_tree_conditions'):
            with self.subTest(block=name), self.assertRaisesRegex(ValueError, 'RSZ type/CRC mismatches.*rszdmc5.json'):
                doc.rsz_blocks.get_block(name).get_instance(1)

    def test_parseable_blocks_and_node_edit_roundtrip(self):
        path = CORPUS / 'pl0100.motfsm2.31'
        source = path.read_bytes()
        doc = MotfsmFile()
        doc.read(source)
        expected = bytearray(source)
        checks = []
        for name in BLOCK_NAMES:
            if name in ('actions', 'conditions', 'expression_tree_conditions'):
                continue  # Their incompatible type definitions are tested separately.
            block = doc.rsz_blocks.get_block(name)
            selected = None
            for index in range(1, block.instance_count):
                instance = block.get_instance(index)
                for field in instance.fields:
                    if field.binding is not None and field.binding.type_name == 'bool' and selected is None:
                        selected = (index, field.name, field.binding)
            if selected:
                index, field_name, binding = selected
                doc.edit_field(binding, not binding.value)
                struct.pack_into('<?', expected, binding.offset, binding.value)
                checks.append((name, index, field_name, binding.value))
        priority = doc.bindings.get(doc.bhvt.nodes[0], 'priority')
        doc.edit_field(priority, priority.value ^ 1)
        struct.pack_into('<i', expected, priority.offset, priority.value)
        output = doc.rebuild()
        self.assertEqual(output, bytes(expected))
        with tempfile.TemporaryDirectory(prefix='reasy-dmc5-test-') as folder:
            saved = Path(folder) / 'renamed.bin'
            saved.write_bytes(output)
            reread = MotfsmFile()
            reread.read(saved.read_bytes())
            self.assertEqual(reread.bhvt.nodes[0].priority, priority.value)
            for name, index, field_name, value in checks:
                instance = reread.rsz_blocks.get_block(name).get_instance(index)
                field = next(f for f in instance.fields if f.name == field_name)
                self.assertEqual(field.value, value)
            self.assertEqual(reread.rebuild(), output)
        self.assertEqual(path.read_bytes(), source)


if __name__ == '__main__':
    unittest.main()
