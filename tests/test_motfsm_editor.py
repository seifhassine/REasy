"""Native Action editing, live queries, shared references and undo integration."""
import os
from pathlib import Path
import tempfile
import struct
import json
from types import SimpleNamespace
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QComboBox

from file_handlers.motfsm.document import ObjectKey
from file_handlers.motfsm.motfsm_handler import MotfsmHandler
from file_handlers.motfsm.motfsm_file import MotfsmFile
from file_handlers.motfsm.motfsm_viewer import MotfsmViewer


CORPUS = Path(__file__).parent / 'TESTFILE/natives/STM/player/Fsm'


class RszArrayExpansionTests(unittest.TestCase):
    @staticmethod
    def block(raw, registry):
        from file_handlers.motfsm.fields import FieldBindings
        from file_handlers.motfsm.rsz_adapter import RSZBlock
        document = SimpleNamespace(source=raw, type_registry=registry, bindings=FieldBindings(raw))
        return RSZBlock(document, 'actions', 0, len(raw))

    def test_struct_arrays_containing_arrays_use_native_element_spans(self):
        from file_handlers.rsz.rsz_file import RszFile, RszRSZHeader, RszInstanceInfo
        from file_handlers.rsz.rsz_data_types import StructData, ArrayData, U32Data
        from utils.type_registry import TypeRegistry
        # A format fixture exercises nested arrays absent from the current MHR FSM corpus.
        def definition(name, kind, original):
            return dict(name=name, type=kind, original_type=original, array=True, align=4, size=4, native=False)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'schema.json'
            path.write_text(json.dumps({
                '1': {'name': 'Fixture.Owner', 'crc': '1', 'fields': [definition('Rows', 'Struct', 'Fixture.Row')]},
                '2': {'name': 'Fixture.Row', 'crc': '2', 'fields': [definition('Values', 'U32', 'System.UInt32')]},
            }), encoding='utf-8')
            native = RszFile()
            native.type_registry = TypeRegistry(str(path))
            native.rsz_header = RszRSZHeader()
            for key, value in dict(magic=0x005A5352, version=16, object_count=1, instance_count=2,
                                   userdata_count=0, reserved=0, instance_offset=0, data_offset=0, userdata_offset=0).items():
                setattr(native.rsz_header, key, value)
            instance = RszInstanceInfo()
            instance.type_id = instance.crc = 1
            native.instance_infos = [RszInstanceInfo(), instance]
            native.object_table = [1]
            values = list(range(131))
            native.parsed_elements = {0: {}, 1: {'Rows': StructData([
                {'Values': ArrayData([U32Data(v) for v in values], U32Data, 'System.UInt32')},
                {'Values': ArrayData([], U32Data, 'System.UInt32')},
            ], 'Fixture.Row')}}
            raw = native.build_headless()
            block = self.block(raw, native.type_registry)
            rows = block.get_instance(1).fields[0]
            self.assertEqual(len(rows.children), len(native.parsed_elements[1]['Rows'].values))
            nested = rows.children[0].children[0]
            self.assertEqual([c.value for c in nested.children], values)
            self.assertEqual(rows.children[1].children[0].children, [])
            for child in nested.children:
                self.assertEqual(child.instance_index, 1)
                self.assertEqual(struct.unpack_from('<I', raw, child.binding.offset)[0], child.value)
            self.assertEqual(block.file.build_validated(), raw)

    @unittest.skipUnless(CORPUS.is_dir(), 'native corpus is absent')
    def test_native_object_and_string_arrays(self):
        from file_handlers.rsz.rsz_file import RszFile
        from utils.type_registry import TypeRegistry
        path = next((Path(__file__).parent / 'TESTFILE').rglob('epvs-prg*.pfb.17'))
        native = RszFile()
        native.filepath = str(path)
        native.type_registry = TypeRegistry(str(Path(__file__).resolve().parents[1] / 'resources/data/dumps/rszmhrise.json'))
        native.read(path.read_bytes(), validate_type_registry=True)
        raw = native.build_headless()
        block = self.block(raw, native.type_registry)
        kinds = set()
        for index in range(1, block.instance_count):
            for field in block.get_instance(index).fields:
                if not field.is_array:
                    continue
                kinds.add(field.definition['type'])
                self.assertEqual(len(field.children), len(field.data.values))
                for child, original in zip(field.children, field.data.values):
                    self.assertEqual(child.value, original.value.rstrip('\0') if isinstance(original.value, str) else original.value)
                    if child.reference and child.value:
                        self.assertEqual(child.resolve().index, child.value)
                        self.assertTrue(child.resolve().fields)
        self.assertTrue({'Object', 'String'} <= kinds)
        self.assertEqual(block.file.build_validated(), raw)


@unittest.skipUnless(CORPUS.is_dir(), 'native FSM corpus is absent')
class FsmStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.path = next(CORPUS.rglob('ChargeAxe.motfsm2.43'))
        cls.source = cls.path.read_bytes()

    def setUp(self):
        self.handler = MotfsmHandler()
        self.handler.filepath = str(self.path)
        self.handler.read(self.source)
        self.access = self.handler.editor_document
        self.action = next(a for a in self.access.actions() if a.class_name.endswith('RequestBottleAttack'))
        self.addCleanup(self.handler.deleteLater)

    def hit_field(self):
        return self.access.fields(self.action.key)['_HitId']

    def test_native_array_element_edit_is_exact_and_undoable(self):
        field = self.hit_field()
        element = field.children[0]
        self.assertEqual(element.size, 4)
        value = element.value+1
        expected = bytearray(self.source)
        struct.pack_into('<I', expected, element.offset, value)
        self.access.edit_field(element.binding, str(value))
        self.assertEqual(self.handler.rebuild(), bytes(expected))
        reopened = MotfsmFile()
        reopened.read(expected)
        saved = reopened.rsz_blocks.get_block(field.block.name).get_instance(field.instance_index)
        self.assertEqual(next(f for f in saved.fields if f.name == '_HitId').children[0].value, value)
        self.access.undo_stack.undo()
        self.assertFalse(self.handler.modified)
        self.assertEqual(self.handler.rebuild(), self.source)

    def test_enum_properties_and_array_elements_use_native_choices(self):
        from file_handlers.motfsm.formatting import rsz_value_text
        from file_handlers.motfsm.motfsm_viewer import FieldEditorDelegate
        field = next(f for f in self.access.instance(self.action.key).fields if f.enum_values)
        original = field.value
        member = next(m for m in field.enum_values if m['value'] != original)
        workspace = self.handler.create_viewer()
        self.addCleanup(workspace.close)
        panel = workspace.actions
        panel.select_action(self.action.key)
        editor = panel.editors[field.name]
        self.assertIsInstance(editor, QComboBox)
        self.assertEqual(editor.currentText(), rsz_value_text(field))
        editor.setCurrentIndex(editor.findData(member['value']))
        editor.activated.emit(editor.currentIndex())
        self.assertEqual(self.access.fields(self.action.key)[field.name].value, member['value'])
        self.access.undo_stack.undo()
        self.assertEqual(self.handler.rebuild(), self.source)
        # Unknown native values remain selectable and are never coerced to the first member.
        unused = next(i for i in range(1000) if i not in {m['value'] for m in field.enum_values})
        self.access.edit_rsz_field(field, str(unused))
        self.assertEqual(editor.currentData(), unused)
        self.access.undo_stack.undo()
        array = next(f for a in self.access.actions() for f in self.access.instance(a.key).fields
                     if f.is_array and f.children and f.children[0].enum_values)
        viewer = MotfsmViewer(self.handler, selection_only=True)
        self.addCleanup(viewer.close)
        viewer.show_rsz_field(array)
        item = viewer.tree.topLevelItem(0).child(0)
        self.assertEqual(item.text(1), rsz_value_text(array.children[0]))
        self.assertEqual(FieldEditorDelegate.members(item), array.children[0].enum_values)

    def test_transition_settings_detach_shared_data_and_preserve_other_states(self):
        from file_handlers.motfsm.transitions import TransitionTables
        doc = self.handler.motfsm
        tables = TransitionTables(doc)
        grouped = {}
        for n, node in enumerate(doc.bhvt.nodes):
            for s, state in enumerate(node.states):
                index = tables.resolve(state.TransitionMaps)
                if index is not None:
                    grouped.setdefault(index, []).append((n, s))
        index, users = next((i, u) for i, u in grouped.items() if len(u) > 1)
        n, s = users[0]
        state = doc.bhvt.nodes[n].states[s]
        address = self.access.address(doc.bindings.get(state, 'TransitionMaps'))
        original = tables.record(index)
        self.access.edit_transition(address, 'InterpolationMode', 'CrossFade')
        self.access.edit_transition(address, 'interpolationFrame', 17.25)
        saved = self.handler.motfsm
        after = TransitionTables(saved)
        new_index, fields = self.access.transition_fields(address)
        self.assertNotEqual(new_index, index)
        values = {f.name: f.value for f in fields}
        self.assertEqual(values['interpolationFrame'], 17.25)
        self.assertEqual(values['InterpolationMode'], 2)
        self.assertEqual(next(f for f in fields if f.name == 'StartType').enum_values[0], {'name': 'Frame', 'value': 0})
        for node, position in users[1:]:
            self.assertEqual(after.record(after.resolve(saved.bhvt.nodes[node].states[position].TransitionMaps)), original)
        # Only the selected mode bits and frame float differ within the copied record.
        expected = bytearray(original)
        flags = struct.unpack_from('<I', expected, 4)[0]
        struct.pack_into('<I', expected, 4, (flags & ~0xF0) | 0x20)
        struct.pack_into('<f', expected, 16, 17.25)
        self.assertEqual(after.record(new_index)[4:], bytes(expected)[4:])
        self.access.undo_stack.undo()
        self.access.undo_stack.undo()
        self.assertEqual(self.handler.rebuild(), self.source)
        self.assertFalse(self.handler.modified)

    def test_transition_tree_fields_and_dropdown(self):
        from file_handlers.motfsm.motfsm_viewer import FieldEditorDelegate
        doc = self.handler.motfsm
        n, s = next((n, s) for n, node in enumerate(doc.bhvt.nodes) for s, state in enumerate(node.states)
                    if state.TransitionMaps)
        viewer = MotfsmViewer(self.handler, selection_only=True)
        self.addCleanup(viewer.close)
        viewer._selection = ('state', n, s)
        viewer._show_selection()
        root = viewer.tree.topLevelItem(0)
        item = next(root.child(i) for i in range(root.childCount()) if root.child(i).text(0) == 'TransitionMaps')
        viewer._expand(item)
        rows = {item.child(i).text(0): item.child(i) for i in range(item.childCount())}
        self.assertIn('interpolationFrame', rows)
        members = FieldEditorDelegate.members(rows['InterpolationMode'])
        self.assertIn({'name': 'CrossFade', 'value': 2}, members)
        rows['interpolationFrame'].setText(1, '19.5')
        self.app.processEvents()
        address = ('bhvt', 'nodes', n, 'states', s, 'TransitionMaps')
        fields = dict((f.name, f.value) for f in self.access.transition_fields(address)[1])
        self.assertEqual(fields['interpolationFrame'], 19.5)
        self.access.undo_stack.undo()
        self.assertEqual(self.handler.rebuild(), self.source)

    def test_transition_settings_detach_shared_map(self):
        from file_handlers.motfsm.transitions import TransitionTables
        doc = self.handler.motfsm
        n, s, first = next((n, s, state) for n, node in enumerate(doc.bhvt.nodes)
                           for s, state in enumerate(node.states) if state.TransitionMaps)
        second = next(state for node in doc.bhvt.nodes for state in node.states
                      if state is not first and state.TransitionMaps)
        identity = first.TransitionMaps
        # Exercise a shared native map, independently of sharing its data record.
        doc.edit_field(doc.bindings.get(second, 'TransitionMaps'), identity)
        source = doc.rebuild()
        self.handler.read(source)
        access = self.handler.editor_document
        before = TransitionTables(self.handler.motfsm)
        address = ('bhvt', 'nodes', n, 'states', s, 'TransitionMaps')
        old_record = before.record(before.resolve(identity))
        access.edit_transition(address, 'interpolationFrame', 17.25)
        saved = self.handler.motfsm
        after = TransitionTables(saved)
        self.assertEqual(saved.transition_map_count, before.document.transition_map_count+1)
        self.assertNotEqual(access.binding(address).value, identity)
        self.assertEqual(after.record(after.resolve(identity)), old_record)
        self.assertEqual(list(after.maps), sorted(after.maps))
        self.assertEqual(access.transition_fields(address)[1][0].value, 17.25)
        self.assertEqual(saved.rebuild(), saved.source)
        access.undo_stack.undo()
        self.assertEqual(self.handler.rebuild(), source)

    def test_arrays_across_native_actions_and_storage_types(self):
        from file_handlers.motfsm.fields import SCALAR_FORMATS
        from file_handlers.motfsm.rsz_adapter import BLOCK_NAMES
        seen = set()
        for filename in ('Bow.motfsm2.43', 'ChargeAxe.motfsm2.43', 'DualBlades.motfsm2.43', 'GreatSword.motfsm2.43'):
            source = next(CORPUS.rglob(filename)).read_bytes()
            document = MotfsmFile()
            document.read(source)
            expected = bytearray(source)
            for name in BLOCK_NAMES:
                block = document.rsz_blocks.get_block(name)
                for index in range(1, block.instance_count):
                    for field in block.get_instance(index).fields:
                        if not field.is_array:
                            continue
                        self.assertEqual(len(field.children), len(field.data.values))
                        for element in field.children:
                            self.assertIsNotNone(element.binding)
                            fmt = SCALAR_FORMATS[element.binding.type_name]
                            self.assertEqual(element.value, struct.unpack_from('<'+fmt, source, element.offset)[0])
                            if element.binding.type_name not in seen:
                                seen.add(element.binding.type_name)
                                new = not element.value if fmt == '?' else element.value+1
                                document.edit_field(element.binding, new)
                                struct.pack_into('<'+fmt, expected, element.offset, new)
            self.assertEqual(document.rebuild(), bytes(expected))
            reopened = MotfsmFile()
            reopened.read(expected)
            self.assertEqual(reopened.rebuild(), bytes(expected))
        self.assertTrue({'bool', 's32', 'u32', 'f32'} <= seen)

    def test_array_resize_mixed_edits_save_undo_redo(self):
        original = [f.value for f in self.hit_field().children]
        self.access.edit_field(self.hit_field().children[0].binding, str(original[0]+1))
        self.access.change_array(self.hit_field(), 'duplicate', 0)
        self.assertEqual([f.value for f in self.hit_field().children], [original[0]+1, original[0]+1, *original[1:]])
        self.access.edit_field(self.hit_field().children[1].binding, str(original[0]+2))
        self.access.change_array(self.hit_field(), 'remove', 0)
        saved = self.handler.rebuild()
        self.handler.mark_saved()
        self.assertFalse(self.handler.modified)
        for _ in range(4):
            self.access.undo_stack.undo()
        self.assertEqual(self.handler.rebuild(), self.source)
        self.assertTrue(self.handler.modified)
        for _ in range(4):
            self.access.undo_stack.redo()
        self.assertEqual(self.handler.rebuild(), saved)
        self.assertFalse(self.handler.modified)
        for _ in range(len(self.hit_field().children)):
            self.access.change_array(self.hit_field(), 'remove', 0)
        self.assertEqual(len(self.hit_field().children), 0)
        self.access.change_array(self.hit_field(), 'add')
        self.assertEqual([f.value for f in self.hit_field().children], [0])

    def test_property_tree_edits_and_reference_details(self):
        workspace = self.handler.create_viewer()
        self.addCleanup(workspace.close)
        workspace.actions.select_action(self.action.key)
        panel = workspace.actions.editors['_HitId']
        self.assertIsInstance(panel, MotfsmViewer)
        root = panel.tree.topLevelItem(0)
        self.assertEqual(root.childCount(), len(self.hit_field().children))
        value = self.hit_field().children[0].value+1
        root.child(0).setText(1, str(value))
        self.app.processEvents()
        self.assertEqual(self.hit_field().children[0].value, value)
        panel._change_array(self.hit_field(), 'duplicate', 0)
        self.app.processEvents()
        self.assertEqual(panel.tree.topLevelItem(0).childCount(), len(self.hit_field().children))
        # State references show their native targets before expansion.
        doc = self.handler.motfsm
        owner, state = next((n, s) for n in doc.bhvt.nodes for s in n.states
                            if s.mStates.values and doc.references.state_target(s.mTransitions) is not None)
        viewer = MotfsmViewer(self.handler, selection_only=True)
        self.addCleanup(viewer.close)
        viewer.show_node(doc.bhvt.nodes.index(owner))
        top = viewer.tree.topLevelItem(0)
        group = next(top.child(i) for i in range(top.childCount()) if top.child(i).text(0).startswith('States ('))
        item = group.child(owner.states.index(state))
        viewer._expand(item)
        target = next(item.child(i) for i in range(item.childCount()) if item.child(i).text(0) == 'mTransitions')
        target_name = doc.bhvt.nodes[doc.references.state_target(state.mTransitions)].name
        self.assertIn(target_name, target.text(1))
        viewer._expand(target)
        self.assertGreater(target.childCount(), 1)
        events = item.child(0)
        self.assertIn('TransitionEvents', events.text(0))
        event = events.child(0)
        instance = doc.references.object_instance('transition_events', state.mStates.values[0])
        self.assertIn(instance.class_name.rsplit('.', 1)[-1], event.text(1))
        viewer._expand(event)
        self.assertTrue(any(event.child(i).binding is not None for i in range(event.childCount())))

    def test_nested_object_resolution_and_invalid_reference(self):
        block = self.handler.motfsm.rsz_blocks.get_block('actions')
        field = next(f for index in range(1, block.instance_count) for f in block.get_instance(index).fields
                     if f.reference and f.value and not f.resolve().is_userdata)
        target = field.resolve()
        viewer = MotfsmViewer(self.handler, selection_only=True)
        self.addCleanup(viewer.close)
        viewer.show_rsz_field(field)
        root = viewer.tree.topLevelItem(0)
        self.assertIn(target.class_name.rsplit('.', 1)[-1], root.text(1))
        self.assertGreater(root.childCount(), 1)
        count = self.access.undo_stack.count()
        with self.assertRaisesRegex(ValueError, 'outside'):
            self.access.edit_field(field.binding, str(block.instance_count))
        self.assertEqual(self.access.undo_stack.count(), count)
        self.assertEqual(self.handler.rebuild(), self.source)

    def test_native_variable_length_and_vector_fields_use_shared_writer(self):
        chosen = {}
        for action in self.access.actions():
            for field in self.access.instance(action.key).fields:
                if field.data.__class__.__name__ in ('StringData', 'ResourceData') and 'text' not in chosen:
                    chosen['text'] = self.access.field_address(field)
                if field.data.__class__.__name__ in ('Vec3Data', 'Float3Data') and 'vector' not in chosen:
                    chosen['vector'] = self.access.field_address(field)
            if len(chosen) == 2:
                break
        self.assertEqual(set(chosen), {'text', 'vector'})
        for kind, address in chosen.items():
            field = self.access.rsz_field(address)
            expected = field.value+'edited' if kind == 'text' else [float(v)+1 for v in field.value]
            self.access.edit_rsz_field(field, expected if kind == 'text' else json.dumps(expected))
            after = self.access.rsz_field(address).value
            if kind == 'text':
                self.assertEqual(after, expected)
            else:
                for actual, wanted in zip(after, expected):
                    self.assertAlmostEqual(actual, wanted, places=4)
            self.access.undo_stack.undo()
            self.assertEqual(self.handler.rebuild(), self.source)


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
                    if field.binding is not None and not field.enum_values and field.name != 'v1_ID' and field.binding.type_name in ('s32', 'u32'):
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
        self.assertIs(workspace.tabs.currentWidget(), workspace.graph_page)
        self.assertEqual(workspace.graph_page.orientation(), Qt.Horizontal)
        self.assertEqual(workspace.node_details.orientation(), Qt.Vertical)
        self.assertEqual(workspace.node_details.count(), 2)
        workspace.tabs.setCurrentWidget(panel)
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
        root = workspace.inspector.tree.topLevelItem(0)
        groups = {root.child(i).text(0).split(' (', 1)[0]: root.child(i)
                  for i in range(root.childCount())}
        for name in ('Actions', 'States', 'Children', 'Transitions', 'AllStates'):
            if name in groups:
                self.assertFalse(groups[name].isExpanded(), name)
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
