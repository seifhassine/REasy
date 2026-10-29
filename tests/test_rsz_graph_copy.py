"""Instance copies retain graph ownership, aliases and native userdata records."""
import tempfile
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from file_handlers.rsz.graph_copy import copy_graph
from file_handlers.motfsm.motfsm_file import MotfsmFile, Action
from test_motfsm_native_writer import fixture


class GraphCopyTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.doc = fixture(self.folder.name, userdata=True)

    def test_cross_block_import_copies_owned_and_external_references(self):
        editor = self.doc.graph_editor()
        raw = editor.import_object('static_actions', 0, source_name='actions',
                                   identity=0x999, extension=7)
        target = self.doc.rsz_blocks.get_block('static_actions').file
        source = self.doc.rsz_blocks.get_block('actions').file
        imported = target.parsed_elements[target.object_table[raw & 0xFFFF]]
        self.assertNotEqual(imported['Child'].value, source.parsed_elements[1]['Child'].value)
        self.assertEqual(target.parsed_elements[imported['Child'].value]['Value'].value, 77)
        external = target._rsz_userdata_dict[imported['External'].value]
        self.assertEqual(target.get_rsz_userdata_string(external), 'fixture/shared.user')
        self.assertEqual(self.doc.bhvt.static_action_ex_ids, [0, 7])
        self.doc.build_validated()

    def test_insert_shifts_existing_references_and_keeps_copied_aliases(self):
        native = self.doc.rsz_blocks.get_block('actions').file
        copied = copy_graph(native, native, [1, 2], insertion=1)
        root, child = copied.roots
        self.assertEqual(native.parsed_elements[root]['Child'].value, child)
        self.assertEqual(len(copied.instances), 2)
        original = native.object_table[0]
        self.assertEqual(native.parsed_elements[original]['Child'].value, copied.shifted[2])
        self.assertEqual(native.parsed_elements[root]['External'].value, copied.shifted[3])
        self.assertEqual(native._rsz_userdata_dict[copied.shifted[3]].instance_id, copied.shifted[3])
        native.build_validated()

    def test_owned_copy_keeps_external_userdata_shared(self):
        slot = self.doc.graph_editor().clone_object('actions', 0, identity=0x999)
        native = self.doc.rsz_blocks.get_block('actions').file
        self.assertEqual(native.parsed_elements[native.object_table[slot]]['External'].value, 3)
        self.assertEqual(len(native.rsz_userdata_infos), 1)
        self.doc.build_validated()

    def test_invalid_import_is_rejected_before_mutating_destination(self):
        source = self.doc.rsz_blocks.get_block('actions').file
        target = self.doc.rsz_blocks.get_block('static_actions').file
        before = target.build_validated()
        source.parsed_elements[1]['Child'].value = 9999
        with self.assertRaisesRegex(ValueError, 'Dangling'):
            copy_graph(source, target, [1])
        self.assertEqual(target.build_validated(), before)

    def _reader(self):
        doc = MotfsmFile()
        doc.set_rsz_type_info_path(self.doc._registry_path)
        return doc

    def test_action_add_command_copies_nested_instances(self):
        from tools.fsm.operations import action_add
        self.doc.bhvt.nodes[0].actions = [Action(0x100, 0)]
        path = Path(self.folder.name) / 'source.motfsm2.43'
        path.write_bytes(self.doc.build_validated())
        args = SimpleNamespace(source=path, node='root', source_node='root',
                               klass='Action', exact=True, set=['Frame=12.5'])
        with patch.object(action_add, 'MotfsmFile', side_effect=self._reader):
            output = action_add.run(args)
        saved = self._reader(); saved.read(output)
        native = saved.rsz_blocks.get_block('actions').file
        reference = saved.bhvt.nodes[0].actions[-1]
        added = saved.references.action(reference.id_hash, reference.ex_id)
        self.assertNotEqual(native.parsed_elements[added.index]['Child'].value, 2)
        self.assertEqual(native.parsed_elements[added.index]['External'].value, 3)
        self.assertEqual(native.parsed_elements[added.index]['Frame'].value, 12.5)
        self.assertEqual(saved.build_validated(), output)

    def test_rcol_clone_preserves_other_requests_and_private_userdata(self):
        from file_handlers.rcol.editing import clone_request_set, resolve_requests
        from file_handlers.rcol.rcol_file import RcolFile
        from file_handlers.rcol.rcol_structures import RcolGroup, RcolShape
        from file_handlers.rcol.request_set import RequestSet
        from file_handlers.rcol.shape_types import Sphere, ShapeType
        for version in (20, 25):
            with self.subTest(version=version):
                doc = fixture(self.folder.name, userdata=True)
                rcol = RcolFile(); rcol.file_version = version
                rcol.type_registry = doc.type_registry
                rcol.rsz = doc.rsz_blocks.get_block('actions').file
                rcol.rsz.object_table[:] = [1, 2]
                group = RcolGroup(); shape = RcolShape()
                group.info.guid = group.info.layer_guid = shape.info.guid = bytes(16)
                shape.info.user_data_index = 1
                shape.info.shape_type = ShapeType.Sphere
                shape.shape = Sphere()
                group.shapes = [shape]; rcol.groups = [group]
                request = RequestSet(); request.info.name = 'template'
                request.info.field0 = 10; request.info.group_userdata_index_start = 1
                rcol.request_sets = [request]; rcol.setup_references(version)
                before = resolve_requests(rcol)
                index = clone_request_set(rcol, 0, 'new', field0=99)
                after = resolve_requests(rcol)
                self.assertEqual(after[0], before[0])
                self.assertEqual(after[1]['shapes'], before[0]['shapes'])
                self.assertEqual(after[1]['root'], before[0]['root'])
                self.assertNotEqual(rcol.request_sets[index].instance, 1)
                data = rcol.write(file_version=version)
                saved = RcolFile(); saved.type_registry = doc.type_registry
                saved.read(data, file_version=version)
                self.assertEqual(resolve_requests(saved), after)
                self.assertEqual(saved.write(file_version=version), data)

    def test_effect_element_clone_keeps_root_last_and_shared_children_private(self):
        from file_handlers.rsz.effect_elements import clone_element, find_root
        from file_handlers.rsz.rsz_file import RszFile, RszInstanceInfo
        from file_handlers.rsz.rsz_data_types import ArrayData, ObjectData, U32Data, Vec3Data, GuidData
        from file_handlers.rsz.inspection import graph_value
        from utils.type_registry import TypeRegistry
        def field(name, kind, original=None, array=False, size=4, align=4):
            return dict(name=name, type=kind, original_type=original or kind,
                        array=array, native=False, size=size, align=align)
        types = {
            '1': dict(name='via.effect.script.EPVStandardData', crc='1', fields=[field('Elements', 'Object', 'Element', True)]),
            '2': dict(name='Element', crc='2', fields=[field('ID', 'U32'), field('Rotation', 'Vec3', size=16, align=16),
                field('GUID', 'Guid', size=16), field('Children', 'Object', 'Child', True)]),
            '3': dict(name='Child', crc='3', fields=[field('Value', 'U32')]),
        }
        path = Path(self.folder.name) / 'effects.json'; path.write_text(json.dumps(types), encoding='utf-8')
        native = self.doc.rsz_blocks.get_block('actions').file
        native.type_registry = TypeRegistry(str(path))
        native.instance_infos = []
        for kind in (0, 3, 2, 1):
            info = RszInstanceInfo(); info.type_id = info.crc = kind
            native.instance_infos.append(info)
        native.rsz_userdata_infos = []; native._rsz_userdata_dict = {}; native._rsz_userdata_set = set()
        native.parsed_elements = {0: {}, 1: {'Value': U32Data(42)},
            2: {'ID': U32Data(15), 'Rotation': Vec3Data(1, 2, 3), 'GUID': GuidData(raw_bytes=bytes(16)),
                'Children': ArrayData([ObjectData(1, 'Child'), ObjectData(1, 'Child')], ObjectData, 'Child')},
            3: {'Elements': ArrayData([ObjectData(2, 'Element')], ObjectData, 'Element')}}
        native.object_table = [3]
        native.instance_hierarchy = {}
        source = native.build_validated()
        rsz = RszFile(); rsz.type_registry = native.type_registry; rsz.read_headless(source)
        before = graph_value(rsz, 2)
        added = clone_element(rsz, 15, 99, rotation_delta=(0, 0, 10))
        root = find_root(rsz)
        self.assertEqual(root, len(rsz.instance_infos) - 1)
        self.assertEqual(graph_value(rsz, 2), before)
        children = rsz.parsed_elements[added]['Children'].values
        self.assertEqual(children[0].value, children[1].value)
        self.assertNotEqual(children[0].value, 1)
        self.assertEqual(rsz.parsed_elements[added]['Rotation'].z, 13)
        data = rsz.build_validated()
        saved = RszFile(); saved.type_registry = rsz.type_registry; saved.read_headless(data)
        self.assertEqual(graph_value(saved, root), graph_value(rsz, root))
        self.assertEqual(saved.build_validated(), data)

    def test_child_recipe_preserves_static_action_extension_namespace(self):
        from services.mhr.fsm.node_action_child import NodeActionChildPlan, build
        self.doc.bhvt.static_action_ex_ids[0] = 9
        self.doc.bhvt.nodes[0].actions = [Action(0x200, 9)]
        source = self.doc.build_validated()
        output = build(source, NodeActionChildPlan(parent='root', name='child', klass='Fixture.Action',
                       exact=True, no_selector=True), registry=self.doc.type_registry)
        saved = self._reader(); saved.read(output)
        self.assertEqual(saved.bhvt.action_ex_ids, [0])
        self.assertEqual(saved.bhvt.static_action_ex_ids, [9, 9])
        reference = saved.bhvt.nodes[-1].actions[0]
        self.assertEqual(reference.ex_id, 9)
        self.assertEqual(saved.references.action(reference.id_hash, 9).class_name, 'Fixture.Action')
        self.assertEqual(saved.build_validated(), output)

    def test_static_event_shared_by_two_states_on_one_node_is_detached(self):
        from tools.fsm.operations import event_copy
        from test_motfsm_map_ownership import mapped_fixture
        self.doc = mapped_fixture(self.folder.name)
        for state in self.doc.bhvt.nodes[0].states:
            state.mStates.values = [0x40000000]
        source = self.doc.build_validated()
        path = Path(self.folder.name) / 'events.motfsm2.43'; path.write_bytes(source)
        args = SimpleNamespace(source=path, node='root', states='0', klass='Fixture.Event', set=[], clear=False)
        with patch.object(event_copy, 'MotfsmFile', side_effect=self._reader):
            output = event_copy.run(args)
        saved = self._reader(); saved.read(output)
        self.assertEqual(saved.bhvt.nodes[0].states[1].mStates.values, [0x40000000])
        copied = saved.bhvt.nodes[0].states[0].mStates.values[0]
        self.assertNotEqual(copied, 0x40000000)
        self.assertEqual(saved.references.object_instance('transition_events', copied).class_name, 'Fixture.Event')
        self.assertEqual(saved.build_validated(), output)

    def test_action_import_command_remaps_cross_file_userdata_and_static_extensions(self):
        from tools.fsm.operations import action_import
        source = self._reader(); source.read(self.doc.source)
        source.bhvt.static_action_ex_ids[0] = 7
        source.bhvt.nodes[0].actions = [Action(0x200, 7)]
        donor = Path(self.folder.name) / 'donor.motfsm2.43'
        target = Path(self.folder.name) / 'target.motfsm2.43'
        donor.write_bytes(source.build_validated()); target.write_bytes(self.doc.source)
        args = SimpleNamespace(source=target, from_fsm=donor, from_node='root', node='root',
                               klass='Fixture.Action', exact=True, position=None, allow_uvar_drift=False)
        with patch.object(action_import, 'MotfsmFile', side_effect=self._reader):
            output = action_import.run(args)
        saved = self._reader(); saved.read(output)
        self.assertEqual(saved.bhvt.static_action_ex_ids, [0, 7])
        reference = saved.bhvt.nodes[0].actions[-1]
        added = saved.references.action(reference.id_hash, reference.ex_id)
        native = saved.rsz_blocks.get_block('static_actions').file
        self.assertNotEqual(native.parsed_elements[added.index]['Child'].value, 2)
        row = native._rsz_userdata_dict[native.parsed_elements[added.index]['External'].value]
        self.assertEqual(native.get_rsz_userdata_string(row), 'fixture/shared.user')
        self.assertEqual(saved.build_validated(), output)


if __name__ == '__main__':
    unittest.main()
