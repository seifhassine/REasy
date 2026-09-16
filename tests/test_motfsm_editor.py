"""Native Action editing, live queries, shared references and undo integration."""
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit

from file_handlers.motfsm.document import ObjectKey
from file_handlers.motfsm.motfsm_handler import MotfsmHandler
from file_handlers.motfsm.motfsm_file import MotfsmFile


CORPUS = Path(__file__).parent / 'TESTFILE/natives/STM/player/Fsm'


@unittest.skipUnless(CORPUS.is_dir(), 'native FSM corpus is absent')
class FsmEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.path = max(CORPUS.rglob('*.motfsm2.*'), key=lambda p: p.stat().st_size)
        cls.source = cls.path.read_bytes()

    def handler(self, path=None):
        handler = MotfsmHandler()
        handler.filepath = str(path or self.path)
        handler.read(self.source)
        self.addCleanup(handler.deleteLater)
        return handler

    def editable_action(self, access):
        for action in access.actions():
            if access.users(action.key):
                for field in access.instance(action.key).fields:
                    if field.binding is not None and field.name != 'v1_ID' and field.binding.type_name in ('s32', 'u32'):
                        value = field.value + 1 if field.value < 100 else field.value - 1
                        return action, field, value
        self.fail('Native corpus has no editable Action scalar')

    def test_file_and_block_scopes_are_distinct(self):
        first = self.handler()
        second = self.handler(self.path.with_name('other-' + self.path.name))
        action = first.editor_document.actions()[0]
        other = second.editor_document.actions()[0]
        self.assertEqual(action.id_hash, other.id_hash)
        self.assertNotEqual(action.key, other.key)
        with self.assertRaisesRegex(ValueError, 'different FSM document'):
            second.editor_document.instance(action.key)
        self.assertNotEqual(action.key, ObjectKey(action.key.file, 'static_' + action.key.block, action.key.instance))

    def test_queries_observe_unsaved_fields_and_undo(self):
        handler = self.handler()
        access = handler.editor_document
        action, field, value = self.editable_action(access)
        original = field.value
        scope = dict(class_name=action.class_name, identity=f'0x{action.id_hash:08X}', field_name=field.name)
        self.assertEqual([a.key for a in access.query(**scope)], [action.key])
        access.edit_field(field.binding, str(value))
        self.assertEqual(access.fields(action.key)[field.name].value, value)
        self.assertIn(action, access.query(**scope, value=str(value)))
        self.assertTrue(handler.modified)
        access.undo_stack.undo()
        self.assertEqual(access.fields(action.key)[field.name].value, original)
        self.assertEqual(handler.rebuild(), self.source)
        self.assertFalse(handler.modified)
        access.undo_stack.redo()
        self.assertEqual(access.fields(action.key)[field.name].value, value)
        self.assertEqual(self.path.read_bytes(), self.source)

    def test_mixed_reference_and_field_commands_survive_relocation_and_save(self):
        handler = self.handler()
        access = handler.editor_document
        action, field, value = self.editable_action(access)
        original = field.value
        original_users = access.users(action.key)
        node_index = next(i for i, n in enumerate(handler.motfsm.bhvt.nodes)
                          if i not in {r.node_index for r in original_users})
        access.edit_field(field.binding, str(value))
        # Three extra references exercise relocation independently of source padding.
        for _ in range(3):
            access.add_reference(node_index, action.id_hash, action.ex_id)
        self.assertEqual(len(access.users(action.key)), len(original_users)+3)
        self.assertIn(action, access.query(node_index=node_index))
        node = handler.motfsm.get_node_by_index(node_index)
        priority = node.priority
        access.edit_field(handler.motfsm.bindings.get(node, 'priority'), str(priority+1))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / self.path.name
            path.write_bytes(handler.rebuild())
            handler.mark_saved()
            self.assertFalse(handler.modified)
            reopened = MotfsmFile()
            reopened.read(path.read_bytes())
            self.assertEqual(next(f.value for f in reopened.references.action(action.id_hash, action.ex_id).fields
                                  if f.name == field.name), value)
            self.assertEqual(reopened.get_node_by_index(node_index).priority, priority+1)
            access.undo_stack.undo()
            self.assertTrue(handler.modified)
            for _ in range(3):
                access.undo_stack.undo()
            self.assertEqual(access.users(action.key), original_users)
            access.undo_stack.undo()
            self.assertEqual(access.fields(action.key)[field.name].value, original)
            self.assertEqual(handler.rebuild(), self.source)
            self.assertTrue(handler.modified)  # Original differs from the saved edited file.
            while access.undo_stack.canRedo():
                access.undo_stack.redo()
            self.assertEqual(handler.rebuild(), path.read_bytes())
            self.assertFalse(handler.modified)

    def test_reference_removal_and_invalid_scalar_are_transactional(self):
        handler = self.handler()
        access = handler.editor_document
        action, field, _ = self.editable_action(access)
        users = access.users(action.key)
        ref = users[0]
        access.remove_reference(ref.node_index, ref.position)
        self.assertEqual(len(access.users(action.key)), len(users)-1)
        access.undo_stack.undo()
        self.assertEqual(access.users(action.key), users)
        self.assertFalse(handler.modified)
        count = access.undo_stack.count()
        current = access.fields(action.key)[field.name].binding
        with self.assertRaises(ValueError):
            access.edit_field(current, '999999999999999999999999999999999999')
        self.assertEqual(access.undo_stack.count(), count)
        self.assertEqual(handler.rebuild(), self.source)

    def test_direct_properties_table_reference_jump_and_local_graph_updates(self):
        handler = self.handler()
        access = handler.editor_document
        action, field, value = self.editable_action(access)
        workspace = handler.create_viewer()
        self.addCleanup(workspace.close)
        workspace.resize(1400, 900)
        workspace.show()
        panel = workspace.actions
        self.assertIs(workspace.tabs.currentWidget(), panel)
        panel.select_action(action.key)
        self.app.processEvents()
        editor = panel.editors[field.name]
        self.assertIsInstance(editor, QLineEdit)
        graph_items = dict(workspace.view.node_items)
        editor.setFocus()
        QTest.keyClick(editor, Qt.Key_A, Qt.ControlModifier)
        QTest.keyClicks(editor, str(value))
        QTest.keyClick(editor, Qt.Key_Return)
        QTest.qWait(100)
        self.assertEqual(access.fields(action.key)[field.name].value, value)
        self.assertEqual(workspace.view.node_items, graph_items)
        column = panel.table_model.columns.index(('field', field.name))
        row = panel.table.currentIndex().row()
        self.assertEqual(panel.table_model.data(panel.table_model.index(row, column)), str(value))
        self.assertEqual(panel.references.count(), len(access.users(action.key)))
        target = panel.references.currentItem().data(Qt.UserRole)
        panel.jump_button.click()
        self.assertIs(workspace.tabs.currentWidget(), workspace.graph_page)
        self.assertEqual(workspace.selected, target.node_index)
        self.assertTrue(workspace.view.node_items[target.node_index].isSelected())
        workspace.undo_action.trigger()
        QTest.qWait(100)
        self.assertFalse(handler.modified)
        self.assertEqual(handler.rebuild(), self.source)
        workspace.edit_actions_button.click()
        self.assertIs(workspace.tabs.currentWidget(), panel)
        self.assertEqual(panel.node_index, target.node_index)
        self.assertEqual(panel.editors[field.name].text(), str(field.binding.original_value))
        # Mixed types keep identity columns only; selecting a type exposes its schema.
        panel.types.setCurrentRow(0)
        self.assertTrue(all(kind != 'field' for kind, name in panel.table_model.columns))
        panel.set_node(None)
        last = next(a for a in reversed(access.actions()) if a.class_name == action.class_name)
        panel.select_action(last.key)
        self.app.processEvents()
        current = panel.table.currentIndex()
        self.assertEqual(current.data(Qt.UserRole), last.key)
        self.assertTrue(panel.table.selectionModel().isRowSelected(current.row()))
        self.assertTrue(panel.table.visualRect(current).intersects(panel.table.viewport().rect()))
        reference = access.users(last.key)[0]
        panel.set_node(reference.node_index)
        panel.set_node(None)
        panel.select_action(last.key)
        self.app.processEvents()
        current = panel.table.currentIndex()
        self.assertEqual(current.data(Qt.UserRole), last.key)
        self.assertTrue(panel.table.visualRect(current).intersects(panel.table.viewport().rect()))


if __name__ == '__main__':
    unittest.main()
