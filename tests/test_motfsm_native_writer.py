"""Complete graph edits use valid native tables, without a game-file dependency."""
import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from file_handlers.motfsm.motfsm_file import MotfsmFile, BHVTNode, BHVT, NodeLayout, Action, State, AllState, Transition, IndexList
from file_handlers.motfsm.rsz_adapter import BLOCK_NAMES
from file_handlers.motfsm.serialization import serialize_nodes
from file_handlers.rsz.rsz_file import RszFile, RszRSZHeader, RszInstanceInfo, RSZUserDataInfo
from file_handlers.rsz.rsz_data_types import U32Data, BoolData, ObjectData, F32Data, UserDataData
from utils.type_registry import TypeRegistry


def fixture(folder, compact=False, unknown_section=False, empty_events=False, userdata=False):
    def fd(name, kind, original=None):
        return dict(name=name, type=kind, original_type=original or kind, array=False,
                    align=4 if kind != 'Bool' else 1, size=4 if kind != 'Bool' else 1, native=False)
    types = {
        '1': dict(name='Fixture.Action', crc='1', fields=[fd('v0_Enabled', 'Bool'), fd('v1_ID', 'U32'),
                    fd('Child', 'Object', 'Fixture.Child'), fd('Frame', 'F32')]),
        '2': dict(name='Fixture.Child', crc='2', fields=[fd('Value', 'U32')]),
        '3': dict(name='Fixture.Condition', crc='3', fields=[fd('v0_ID', 'U32'), fd('Child', 'Object', 'Fixture.Child')]),
        '4': dict(name='Fixture.Selector', crc='4', fields=[fd('Late', 'Bool')]),
        '5': dict(name='Fixture.Caller', crc='5', fields=[fd('Value', 'U32')]),
        '6': dict(name='Fixture.Event', crc='6', fields=[fd('v0_UID', 'U32'), fd('Enabled', 'Bool')]),
        '7': dict(name='Fixture.Expression', crc='7', fields=[fd('v0_ID', 'U32')]),
    }
    if userdata:
        types['1']['fields'].append(fd('External', 'UserData', 'Fixture.External'))
        types['8'] = dict(name='Fixture.External', crc='8', fields=[])
    registry_path = Path(folder) / 'registry.json'
    registry_path.write_text(json.dumps(types), encoding='utf-8')
    registry = TypeRegistry(str(registry_path))
    root = BHVTNode(id_hash=0, name='root', parent=0xFFFFFFFF)
    root.transitions = [Transition(IndexList([0x400 if compact else 0]))]
    if empty_events:
        root.transitions[0].mStartTransitionEvent.values = []
    stub = SimpleNamespace(bhvt=BHVT(64, {}, [root], [0], [0]), layout=NodeLayout(not compact, not compact))
    nodes = serialize_nodes(stub)
    header_size = (144 if compact else 152) + (8 if unknown_section else 0)
    offsets = [header_size]
    tree = bytearray(bytes(header_size) + nodes)
    tree.extend(bytes((-len(tree)) % 16))
    for name in BLOCK_NAMES:
        offsets.append(len(tree))
        kind = name.removeprefix('static_')
        native = RszFile()
        native.type_registry = registry
        native.rsz_header = RszRSZHeader()
        for key, value in dict(magic=0x005A5352, version=16, object_count=1, instance_count=2,
                              userdata_count=0, reserved=0, instance_offset=0, data_offset=0, userdata_offset=0).items():
            setattr(native.rsz_header, key, value)
        type_id = {'actions': 1, 'conditions': 3, 'selectors': 4, 'selector_callers': 5,
                   'transition_events': 6, 'expression_tree_conditions': 7}[kind]
        info = RszInstanceInfo()
        info.type_id = info.crc = type_id
        native.instance_infos = [RszInstanceInfo(), info]
        native.object_table = [1]
        if kind in ('actions', 'conditions'):
            child = RszInstanceInfo()
            child.type_id = child.crc = 2
            native.instance_infos.append(child)
        fields = {
            'actions': {'v0_Enabled': BoolData(True), 'v1_ID': U32Data(0x200 if name.startswith('static_') else 0x100),
                        'Child': ObjectData(2, 'Fixture.Child'), 'Frame': F32Data(2.5)},
            'conditions': {'v0_ID': U32Data(0x300), 'Child': ObjectData(2, 'Fixture.Child')},
            'selectors': {'Late': BoolData(False)}, 'selector_callers': {'Value': U32Data(3)},
            'transition_events': {'v0_UID': U32Data(0x400), 'Enabled': BoolData(True)},
            'expression_tree_conditions': {'v0_ID': U32Data(0)},
        }[kind]
        native.parsed_elements = {0: {}, 1: fields}
        if kind in ('actions', 'conditions'):
            native.parsed_elements[2] = {'Value': U32Data(77)}
        if kind == 'actions' and userdata:
            info = RszInstanceInfo()
            info.type_id = info.crc = 8
            native.instance_infos.append(info)
            external = RSZUserDataInfo()
            external.instance_id = 3
            external.hash = 42
            native.rsz_userdata_infos = [external]
            native._rsz_userdata_dict = {3: external}
            native._rsz_userdata_set = {3}
            native.set_rsz_userdata_string(external, 'fixture/shared.user')
            native.parsed_elements[3] = {}
            fields['External'] = UserDataData(3, 'fixture/shared.user', 'Fixture.External')
        payload = native.build_validated()
        tree.extend(payload + bytes((-len(payload)) % 16))
    offsets += [len(tree)] + [0] * (4 if compact else 5)
    if unknown_section:
        offsets.append(len(tree))
    pool = 'root\0'.encode('utf-16le')
    tree.extend(struct.pack('<I', len(pool)//2) + pool)
    struct.pack_into(f'<II{len(offsets)}Q', tree, 0, 0x54564842, 0, *offsets)
    source = struct.pack('<II8x4Q3I', 31 if compact else 43, 0x3273666D, 64, 0, 0, 60, 0, 0, 0)
    source += struct.pack('<I', len(tree)) + tree
    doc = MotfsmFile()
    doc.set_rsz_type_info_path(registry_path)
    doc.read(source)
    return doc


class NativeGraphWriterTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.doc = fixture(self.folder.name)

    def test_nodes_owned_objects_all_columns_and_byte_exact_undo(self):
        doc, source = self.doc, self.doc.source
        original_pool = doc.bhvt.string_pool
        editor = doc.graph_editor()
        action = editor.clone_object('actions', 0, identity=0x101, extension=7)
        condition = editor.clone_object('conditions', 0, identity=0x301)
        static_condition = editor.clone_object('static_conditions', 0, identity=0x302)
        selector = editor.clone_object('selectors', 0)
        caller = editor.clone_object('static_selector_callers', 0)
        event = editor.clone_object('transition_events', 0, identity=0x401)
        node_index = editor.add_node(BHVTNode(id_hash=0x111, name='new state \U0001F409', node_attribute=0x20,
                     selector_id=selector, selector_callers=[caller], selector_caller_condition_id=selector,
                     actions=[editor.action_reference('actions', action)], tags=[3, 7], name_hash=9, fullname_hash=10),
                     parent_index=0, condition=static_condition)
        editor.connect_state(node_index, node_index, condition=condition, events=[event], metadata=999)
        editor.connect_start(node_index, node_index, condition=static_condition, events=[event])
        editor.connect_all(node_index, node_index, condition=condition, attributes=-1)
        editor.set_object_fields('actions', action, {'Frame': 12.3})
        block = doc.rsz_blocks.get_block('actions')
        old_child = block.file.parsed_elements[block.object_table[0]]['Child'].value
        new_child = block.file.parsed_elements[block.object_table[action]]['Child'].value
        self.assertNotEqual(new_child, old_child)
        block.file.parsed_elements[new_child]['Value'].value = 123
        output = doc.build_validated()
        self.assertNotEqual(output, source)
        saved = MotfsmFile()
        saved.set_rsz_type_info_path(doc._registry_path)
        saved.read(output)
        self.assertEqual(saved.node_count, 2)
        self.assertEqual(saved.bhvt.nodes, doc.bhvt.nodes)
        self.assertEqual(saved.bhvt.action_ex_ids, [0, 7])
        self.assertEqual(saved.references.action(0x101, 7).index, block.object_table[action])
        self.assertEqual(block.file.parsed_elements[old_child]['Value'].value, 77)
        editor.remove_node(node_index)
        for name, raw in [('actions', action), ('conditions', condition), ('static_conditions', static_condition),
                          ('selectors', selector), ('static_selector_callers', caller), ('transition_events', event)]:
            editor.remove_object(name, raw)
        doc.bhvt.string_pool = original_pool
        self.assertEqual(doc.build_validated(), source)

    def test_object_slot_compaction_remaps_surviving_references(self):
        editor = self.doc.graph_editor()
        unused = editor.clone_object('conditions', 0, identity=0x301)
        used = editor.clone_object('conditions', 0, identity=0x302)
        index = editor.add_node(BHVTNode(id_hash=10, name='child'), 0, used)
        editor.connect_state(index, index, condition=used)
        editor.remove_object('conditions', unused)
        self.assertEqual(self.doc.bhvt.nodes[0].children[0].condition_id, used - 1)
        self.assertEqual(self.doc.bhvt.nodes[index].states[0].TransitionConditions, used - 1)
        self.assertEqual(self.doc.references.object_instance('conditions', used - 1).fields[0].value, 0x302)
        self.doc.build_validated()
        with self.assertRaisesRegex(ValueError, 'still referenced'):
            editor.remove_object('conditions', used - 1)

    def test_invalid_references_duplicates_and_cycles_are_rejected_transactionally(self):
        source = self.doc.source
        mutations = [lambda e: e.document.bhvt.nodes[0].actions.append(Action(0xDEAD, 0)),
                     lambda e: e.document.bhvt.nodes[0].selector_callers.append(99),
                     lambda e: e.document.bhvt.nodes[0].states.append(State(mTransitions=0xDEAD)),
                     lambda e: e.document.bhvt.nodes.append(copy.deepcopy(e.document.bhvt.nodes[0])),
                     lambda e: e.document.rsz_blocks.get_block('actions').file.parsed_elements[1]['Child'].__setattr__('value', 99)]
        for mutate in mutations:
            with self.assertRaises((ValueError, IndexError)):
                with self.doc.graph_editor().transaction() as e:
                    mutate(e)
            self.assertEqual(self.doc.build_validated(), source)
        e = self.doc.graph_editor()
        index = e.add_node(BHVTNode(id_hash=10, name='child'), 0)
        with self.assertRaisesRegex(ValueError, 'Cyclic'):
            e.reparent_node(0, index)
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            e.clone_object('actions', 0, identity=0x100)
        with self.assertRaisesRegex(ValueError, 'Invalid .* slot'):
            e.clone_object('conditions', 0x80000000)

    def test_duplicate_state_hashes_resolve_by_exact_extension(self):
        e = self.doc.graph_editor()
        groups = [e.add_node(BHVTNode(id_hash=i, name=f'group{i}'), 0) for i in (10, 20)]
        states = [e.add_node(BHVTNode(id_hash=30, ex_id=i, name='state'), parent) for i, parent in enumerate(groups)]
        for index in states:
            e.connect_state(index, index, metadata=0)
        self.doc.build_validated()
        self.assertEqual(self.doc.references.state_target(30, 0), states[0])
        self.assertEqual(self.doc.references.state_target(30, 1), states[1])
        with self.assertRaisesRegex(IndexError, 'ex=2'):
            self.doc.references.state_target(30, 2)

    def test_state_target_extension_survives_write_and_wrong_extension_blocks_save(self):
        e = self.doc.graph_editor()
        source = e.add_node(BHVTNode(id_hash=10, name='source'), 0)
        target = e.add_node(BHVTNode(id_hash=20, ex_id=9, name='target'), 0)
        state = e.connect_state(source, target, metadata=999)
        self.assertEqual(state.mTransitionAttributes, 9)
        raw = self.doc.build_validated()
        reopened = MotfsmFile(); reopened.set_rsz_type_info_path(self.doc._registry_path); reopened.read(raw)
        saved = reopened.bhvt.nodes[source].states[0]
        self.assertEqual(saved.mStatesEx, 999)
        self.assertEqual(reopened.references.state_target(saved.mTransitions, saved.mTransitionAttributes), target)
        self.assertEqual(reopened.build_validated(), raw)
        reopened.edit_field(reopened.bindings.get(saved, 'mTransitionAttributes'), 0)
        with self.assertRaisesRegex(ValueError, 'Unknown node identity.*ex=0'):
            reopened.build_validated()

    def test_shared_userdata_survives_owned_graph_clone_and_instance_compaction(self):
        doc = fixture(self.folder.name, userdata=True)
        e = doc.graph_editor()
        raw = e.clone_object('actions', 0, identity=0x101)
        native = doc.rsz_blocks.get_block('actions').file
        root = native.object_table[raw]
        self.assertEqual(native.parsed_elements[root]['External'].value, 3)
        e.remove_object('actions', 0)
        root = native.object_table[0]
        self.assertEqual(native.parsed_elements[root]['External'].value, 1)
        self.assertEqual(native.rsz_userdata_infos[0].instance_id, 1)
        self.assertEqual(native.get_rsz_userdata_string(native.rsz_userdata_infos[0]), 'fixture/shared.user')
        doc.build_validated()

    def test_reparent_and_incoming_node_references(self):
        e = self.doc.graph_editor()
        first = e.add_node(BHVTNode(id_hash=10, name='first'), 0)
        second = e.add_node(BHVTNode(id_hash=20, name='second'), 0)
        e.connect_state(first, second)
        with self.assertRaisesRegex(ValueError, 'still references'):
            e.remove_node(second)
        e.reparent_node(second, first)
        self.assertEqual(self.doc.bhvt.nodes[second].parent, 10)
        self.assertEqual([c.id_hash for c in self.doc.bhvt.nodes[0].children], [10])
        self.doc.build_validated()

    def test_replaced_rsz_values_cannot_be_overwritten_by_stale_bindings(self):
        block = self.doc.rsz_blocks.get_block('actions')
        binding = next(f.binding for f in block.get_object(0).fields if f.name == 'Frame')
        self.doc.edit_field(binding, 5.5)
        self.doc.graph_editor().set_object_fields('actions', 0, {'Frame': 2.5})
        self.assertEqual(self.doc.build_validated(), self.doc.source)

    def test_native_layout_changes_can_be_checkpointed(self):
        self.doc.bhvt.nodes[0].node_attribute = 0x20
        output = self.doc.build_validated()
        self.assertNotEqual(output, self.doc.source)
        self.doc.accept_changes()
        self.assertEqual(self.doc.build_validated(), output)

    def test_compact_scalar_events_and_missing_extension_columns(self):
        doc = fixture(self.folder.name, compact=True)
        e = doc.graph_editor()
        index = e.add_node(BHVTNode(id_hash=10, name='scalar child'), 0)
        e.connect_start(index, index, events=[0x400])
        doc.build_validated()
        doc.bhvt.nodes[index].transitions[0].mStartStateEx = 1
        with self.assertRaisesRegex((ValueError, IndexError), 'Unknown node|extension'):
            doc.build_validated()

    def test_create_schema_objects_and_reference_tree_shape_rejection(self):
        e = self.doc.graph_editor()
        raw = e.create_object('static_actions', 'Fixture.Action', identity=0x201, extension=9,
                              values={'Frame': 3.5, 'Child': 2})
        index = e.add_node(BHVTNode(id_hash=10, name='created', actions=[e.action_reference('static_actions', raw)]), 0)
        self.doc.build_validated()
        self.doc.bhvt.nodes[index].node_attribute |= 4
        e.document.bhvt.nodes[index].all_states = [AllState()]
        with self.assertRaisesRegex(ValueError, 'cannot store AllStates'):
            self.doc.rebuild()

    def test_unknown_section_is_preserved_but_relocation_is_explicitly_rejected(self):
        doc = fixture(self.folder.name, unknown_section=True)
        self.assertEqual(doc.rebuild(), doc.source)
        original = doc.source
        with self.assertRaisesRegex(ValueError, 'section_18.*not implemented'):
            with doc.graph_editor().transaction() as e:
                e.add_node(BHVTNode(id_hash=10, name='new'), 0)
        self.assertEqual(doc.source, original)
        self.assertEqual(doc.node_count, 1)

    def test_ambiguous_empty_events_require_an_explicit_encoding_for_new_events(self):
        doc = fixture(self.folder.name, empty_events=True)
        self.assertIsNone(doc.layout.transition_event_lists)
        doc.bhvt.nodes[0].transitions[0].mStartTransitionEvent.values = [0]
        with self.assertRaisesRegex(ValueError, 'explicit TransitionEvent'):
            doc.build_validated()
        doc.layout = replace(doc.layout, transition_event_lists=True)
        doc.build_validated()

    def test_undoable_handler_edit_and_failed_edit_preserve_checkpoint(self):
        from PySide6.QtWidgets import QApplication
        from file_handlers.motfsm.motfsm_handler import MotfsmHandler
        app = QApplication.instance() or QApplication([])
        handler = MotfsmHandler()
        handler.motfsm = self.doc
        handler._saved_source = self.doc.source
        from file_handlers.motfsm.document import FsmDocument
        handler.editor_document = FsmDocument(handler)
        before = handler.rebuild()
        handler.change_graph(lambda e: e.add_node(BHVTNode(id_hash=10, name='undo'), 0))
        after = handler.rebuild()
        self.assertNotEqual(after, before)
        self.assertTrue(handler.modified)
        handler.editor_document.undo_stack.undo()
        self.assertEqual(handler.rebuild(), before)
        self.assertFalse(handler.modified)
        handler.editor_document.undo_stack.redo()
        self.assertEqual(handler.rebuild(), after)
        count = handler.editor_document.undo_stack.count()
        with self.assertRaisesRegex(ValueError, 'Unknown action'):
            handler.change_graph(lambda e: e.document.bhvt.nodes[0].actions.append(Action(0xDEAD, 0)))
        self.assertEqual(handler.rebuild(), after)
        self.assertEqual(handler.editor_document.undo_stack.count(), count)
        handler.deleteLater()


if __name__ == '__main__':
    unittest.main()
