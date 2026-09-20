"""Structural saves, with native UVAR readers as an independent relocation oracle."""
import gc
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox, QTabWidget

from file_handlers.motfsm.motfsm_file import MotfsmFile, Action
from file_handlers.motfsm.motfsm_handler import MotfsmHandler
from file_handlers.motfsm.rsz_adapter import BLOCK_NAMES
from test_motfsm_integration import make_fixture
from test_motfsm_corpus import CORPUS, unique_files


from file_handlers.motfsm.validation import variable_snapshot


class StructuralGoldenTests(unittest.TestCase):
    def test_columns_and_counts_grow_shrink_and_revert_without_version_gate(self):
        for compact in (False, True):
            data = bytearray(make_fixture(compact=compact, version=999))
            # This fixture has no variable or prefab sections.
            for index in range(15, 17 if compact else 18):
                struct.pack_into('<Q', data, 64 + 8 + 8 * index, 0)
            doc = MotfsmFile()
            doc.read(data)
            node = doc.bhvt.nodes[0]
            original = node.actions[:]
            node.actions += [Action(0x11223344, 3), Action(0x55667788, 9), Action(0xABCDEF, 7)]
            doc.edit_field(doc.bindings.get(node, 'priority'), 123)
            output = doc.rebuild()
            self.assertGreater(len(output), len(data))
            start = node._action_span[0]
            self.assertEqual(struct.unpack_from('<9I', output, start),
                             (4, 0x2222, 0x11223344, 0x55667788, 0xABCDEF, 0, 3, 9, 7))
            reopened = MotfsmFile()
            reopened.read(output)
            self.assertEqual(reopened.bhvt.nodes, doc.bhvt.nodes)
            doc.accept_changes()
            self.assertEqual(doc.rebuild(), output)
            node = doc.bhvt.nodes[0]
            node.actions.clear()
            shorter = doc.rebuild()
            self.assertLess(len(shorter), len(output))
            reopened.read(shorter)
            self.assertEqual(reopened.bhvt.nodes[0].actions, [])
            node.actions = original
            doc.edit_field(doc.bindings.get(node, 'priority'), 7)
            self.assertEqual(doc.rebuild(), bytes(data))


@unittest.skipUnless(CORPUS.is_dir(), 'MHRise corpus not installed')
class StructuralCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_all_files_structural_growth_and_shrink_preserve_native_data(self):
        total_blocks = total_instances = 0
        for path in unique_files():
            source = path.read_bytes()
            doc = MotfsmFile()
            doc.read(source)
            variables = variable_snapshot(doc)
            nodes = [n for n in doc.bhvt.nodes if len(n.actions) >= 2][:2]
            self.assertTrue(nodes, f'{path.name} has no node with editable Action references')
            originals = [n.actions[:] for n in nodes]
            # Exercise independent splices when the file has multiple Action owners.
            added = 3 if len(nodes) > 1 else 4
            nodes[0].actions += [Action(a.id_hash, a.ex_id) for a in originals[0][:1] * added]
            if len(nodes) > 1:
                del nodes[1].actions[0]
            growth_output = doc.rebuild()
            growth = added*8 - (8 if len(nodes) > 1 else 0)
            self.assertEqual(len(growth_output) - len(source), growth)
            for mode in ('growth', 'shrink'):
                with self.subTest(file=path.name, mode=mode):
                    if mode == 'shrink':
                        nodes[0].actions = originals[0][2:]
                        if len(nodes) > 1:
                            nodes[1].actions = originals[1]
                    output = doc.rebuild()
                    self.assertEqual(len(output) - len(source), growth if mode == 'growth' else -16)
                    reread = MotfsmFile()
                    reread.read(output)
                    self.assertEqual(reread.bhvt.nodes, doc.bhvt.nodes)
                    self.assertEqual(reread.bhvt.action_ex_ids, doc.bhvt.action_ex_ids)
                    self.assertEqual(reread.bhvt.static_action_ex_ids, doc.bhvt.static_action_ex_ids)
                    self.assertEqual(variable_snapshot(reread), variables)
                    for name in BLOCK_NAMES:
                        original = doc.rsz_blocks.get_block(name)
                        block = reread.rsz_blocks.get_block(name)
                        self.assertEqual(output[block.offset:block.end], source[original.offset:original.end])
                        # Force the shared RSZ parser through the entire shifted block.
                        total_instances += block.instance_count
                        total_blocks += 1
                    for identity in {(a.id_hash, a.ex_id) for n in reread.bhvt.nodes for a in n.actions}:
                        target = reread.references.action(*identity)
                        if identity[0] in (0, 0xFFFFFFFF):
                            self.assertIsNone(target)
                        else:
                            self.assertIsNotNone(target)
                    self.assertEqual(reread.rebuild(), output)
                    with tempfile.TemporaryDirectory() as folder:
                        saved = Path(folder) / path.name
                        saved.write_bytes(output)
                        self.assertEqual(saved.read_bytes(), output)
            for node, actions in zip(nodes, originals):
                node.actions = actions
            self.assertEqual(doc.rebuild(), source)
            self.assertEqual(path.read_bytes(), source)
            print(f'{path.name}: grew/shrank, RSZ and UVAR preserved', flush=True)
            del doc, reread
            gc.collect()
        print(f'Structural corpus: {total_blocks} blocks, {total_instances} instances', flush=True)

    def test_ui_add_remove_save_failure_and_continue_editing(self):
        from ui.file_tab import FileTab
        source = next(CORPUS.rglob('LongSword.motfsm2.43')).read_bytes()
        notebook = QTabWidget()
        tab = FileTab(notebook, filename='LongSword.motfsm2.43', data=source)
        notebook.addTab(tab.notebook_widget, 'LongSword')
        handler = tab.handler
        tab.viewer.tabs.setCurrentWidget(tab.viewer.fields)
        viewer = tab.viewer.fields
        try:
            doc = handler.motfsm
            index = next(i for i, n in enumerate(doc.bhvt.nodes) if len(n.actions) == 2)
            node = doc.bhvt.nodes[index]
            original_count = len(node.actions)
            identity = (node.actions[0].id_hash, node.actions[0].ex_id)
            empty_index = next(i for i, n in enumerate(doc.bhvt.nodes) if not n.actions)
            handler.add_action_reference(empty_index, *identity)
            self.assertEqual(handler.motfsm.bhvt.nodes[empty_index].actions, [Action(*identity)])
            handler.remove_action_reference(empty_index, 0)
            self.assertEqual(handler.rebuild(), source)
            nodes = viewer.tree.topLevelItem(0).child(0)
            nodes.setExpanded(True)
            nodes.child(index).setExpanded(True)
            actions = next(nodes.child(index).child(i) for i in range(nodes.child(index).childCount())
                           if nodes.child(index).child(i).action_node_index == index)
            actions.setExpanded(True)
            def select_existing(parent, title, label, choices, current, editable):
                return next(c for c in choices if f'0x{identity[0]:08X}, ex={identity[1]} |' in c), True
            with patch.object(QInputDialog, 'getItem', side_effect=select_existing):
                viewer._add_action(index)
            self.assertEqual(len(handler.motfsm.bhvt.nodes[index].actions), original_count + 1)
            self.assertTrue(tab.modified)
            # Removing the same reference restores both bytes and the saved-state flag.
            viewer._remove_action(index, original_count)
            self.assertEqual(handler.rebuild(), source)
            self.assertFalse(tab.modified)
            # Mix a native RSZ scalar edit with two additions (true size growth).
            instance = handler.motfsm.references.action(*identity)
            scalar = next(f.binding for f in instance.fields if f.binding and f.binding.type_name == 'bool')
            field_name, field_value = scalar.name, not scalar.value
            handler.edit_field(scalar, str(field_value))
            for _ in range(2):
                handler.add_action_reference(index, *identity)
            pending = handler.rebuild()
            self.assertGreater(len(pending), len(source))
            def dismiss_error():
                for widget in self.app.topLevelWidgets():
                    if isinstance(widget, QMessageBox):
                        widget.accept()
            with tempfile.TemporaryDirectory() as folder:
                QTimer.singleShot(0, dismiss_error)
                self.assertFalse(tab.handle_file_save(str(Path(folder) / 'missing' / 'out.motfsm2.43')))
                self.assertTrue(tab.modified)
                self.assertEqual(handler.rebuild(), pending)
                saved = Path(folder) / 'out.motfsm2.43'
                self.assertTrue(tab.handle_file_save(str(saved)))
                self.assertFalse(tab.modified)
                reread = MotfsmFile()
                reread.read(saved.read_bytes())
                self.assertEqual(len(reread.bhvt.nodes[index].actions), original_count + 2)
                field = next(f for f in reread.references.action(*identity).fields if f.name == field_name)
                self.assertEqual(field.value, field_value)
                # Bindings produced after structural edits remain usable after saving.
                edited_node = handler.motfsm.bhvt.nodes[index]
                handler.edit_field(handler.motfsm.bindings.get(edited_node, 'priority'), '123')
                viewer._remove_action(index, original_count)
                self.assertTrue(tab.handle_file_save(str(saved)))
                reread.read(saved.read_bytes())
                self.assertEqual(reread.bhvt.nodes[index].priority, 123)
                self.assertEqual(len(reread.bhvt.nodes[index].actions), original_count + 1)
        finally:
            notebook.close()
            notebook.deleteLater()

    def test_failed_relocation_is_transactional(self):
        source = bytearray(next(CORPUS.rglob('LongSword.motfsm2.43')).read_bytes())
        handler = MotfsmHandler()
        handler.read(source)
        root = handler.motfsm.bhvt.offsets['variables']
        struct.pack_into('<Q', source, root + 16, len(source) + 1)
        handler.read(source)
        node_index = next(i for i, n in enumerate(handler.motfsm.bhvt.nodes) if n.actions)
        node = handler.motfsm.bhvt.nodes[node_index]
        actions = node.actions[:]
        # Three additions force relocation regardless of the original padding.
        with self.assertRaisesRegex(ValueError, 'targets outside tree'):
            handler._replace_actions(node_index, node.actions + actions[:1] * 3)
        self.assertEqual(handler.motfsm.bhvt.nodes[node_index].actions, actions)
        self.assertEqual(handler.rebuild(), source)
        self.assertFalse(handler.modified)


if __name__ == '__main__':
    unittest.main()
