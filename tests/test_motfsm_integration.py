"""Golden byte layouts and Qt editing contracts; no game files required."""
import contextlib
import io
import json
import os
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QTabWidget, QMessageBox

from file_handlers.factory import get_handler_for_data
from file_handlers.motfsm.fields import FieldBindings
from file_handlers.motfsm.motfsm_file import MotfsmFile
from file_handlers.motfsm.motfsm_handler import MotfsmHandler
from file_handlers.rsz.rsz_file import RszFile
from utils.type_registry import TypeRegistry


def make_fixture(*, all_states=False):
    node = b"".join([
        struct.pack("<IIIiI", 0x1234, 0, 0, -1, 0),
        struct.pack("<iIIi", 1, 0x1111, 2, 0),
        struct.pack("<iiii", -1, 1, 0, -1),
        struct.pack("<iII", 1, 0x2222, 0),
        struct.pack("<iHH", 7, 0x20, 0),
        struct.pack("<IIiBB", 0x3333, 0x4444, 0, 1, 0),
        struct.pack("<iiiIiIII", 1, 1, 0, 22, -1, 33, 44, 55),
        struct.pack("<iiiIiI", 1, 1, 0, 66, -1, 77),
        (struct.pack("<iIiIIii", 1, 0x1234, -1, 0x80000001, 2, -1, -1)
         if all_states else struct.pack("<ii", 0, -1)),
    ])
    nodes = struct.pack("<I", 1) + node + struct.pack("<II", 0, 0)
    block_offset = (152 + len(nodes) + 15) & ~15
    # Empty RSZ views share one physical block, including its NULL instance.
    rsz = struct.pack("<4s5I3Q", b"RSZ\0", 16, 0, 1, 0, 0, 48, 64, 56) + bytes(16)
    name = "Fixture\0".encode("utf-16le")
    strings_offset = block_offset + len(rsz)
    other_offset = strings_offset + 4 + len(name)
    offsets = [152] + [block_offset] * 11 + [strings_offset] + [other_offset] * 5
    tree = struct.pack("<II18Q", 0x54564842, 0, *offsets) + nodes
    tree += bytes(block_offset - len(tree)) + rsz
    tree += struct.pack("<I", len(name) // 2) + name + bytes(16)
    return struct.pack("<II8x4Q3I", 43, 0x3273666D, 64, 0, 0, 60, 0, 0, 0) + struct.pack("<I", len(tree)) + tree


class FieldTests(unittest.TestCase):
    def test_scalar_writes_are_exact_and_reversible(self):
        for kind, fmt, value in [
            ("u8", "B", 255), ("s8", "b", -128), ("u16", "H", 65535),
            ("s16", "h", -32768), ("u32", "I", 0xFEDCBA98), ("s32", "i", -123456),
            ("u64", "Q", 2**64-1), ("s64", "q", -2**63),
            ("f32", "f", 0.1), ("f64", "d", -2.5), ("bool", "?", True),
        ]:
            with self.subTest(kind=kind):
                original = bytearray(range(32))
                struct.pack_into("<" + fmt, original, 8, 0)
                fields = FieldBindings(bytes(original))
                owner = SimpleNamespace(value=0)
                binding = fields.bind(owner, "value", 8, kind)
                self.assertEqual(fields.rebuild(), bytes(original))
                binding.set_value(value)
                expected = bytearray(original)
                struct.pack_into("<" + fmt, expected, 8, value)
                self.assertEqual(fields.rebuild(), bytes(expected))
                binding.set_value(0)
                self.assertFalse(fields.modified)
                self.assertEqual(fields.rebuild(), bytes(original))

    def test_noncanonical_bool_and_nan_keep_original_bits(self):
        for kind, raw, value in [("bool", b"\x02", True),
                                  ("f32", bytes.fromhex("4523a17f"), float("nan"))]:
            with self.subTest(kind=kind):
                fields = FieldBindings(raw)
                binding = fields.bind(SimpleNamespace(value=value), "value", 0, kind)
                binding.set_value(value)
                self.assertEqual(fields.rebuild(), raw)

    def test_invalid_value_does_not_change_model_or_bytes(self):
        fields = FieldBindings(bytes(1))
        binding = fields.bind(SimpleNamespace(value=0), "value", 0, "s8")
        with self.assertRaises(ValueError):
            binding.set_value(128)
        self.assertEqual(binding.value, 0)
        self.assertEqual(fields.rebuild(), bytes(1))


class MotfsmIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_golden_node_spans_and_exact_roundtrip(self):
        data = make_fixture()
        doc = MotfsmFile()
        doc.read(data)
        node = doc.bhvt.nodes[0]
        self.assertEqual(node.name, "Fixture")
        self.assertEqual(node.children[0].condition_id, 0)
        self.assertEqual(node.actions[0].ex_id, 0)
        self.assertEqual(node.states[0].TransitionMaps, 33)
        self.assertEqual(node.transitions[0].mStartStateEx, 77)
        priority = doc.bindings.get(node, "priority")
        self.assertEqual(priority.offset, 284)
        self.assertEqual(doc.rebuild(), data)
        doc.edit_field(priority, 42)
        expected = bytearray(data)
        struct.pack_into("<i", expected, 284, 42)
        self.assertEqual(doc.rebuild(), bytes(expected))
        reread = MotfsmFile()
        reread.read(doc.rebuild())
        self.assertEqual(reread.bhvt.nodes[0].priority, 42)

    def test_rejects_unsupported_version_and_truncation(self):
        data = bytearray(make_fixture())
        struct.pack_into("<I", data, 0, 42)
        with self.assertRaisesRegex(ValueError, "version 42"):
            MotfsmFile().read(data)
        with self.assertRaises(ValueError):
            MotfsmFile().read(make_fixture()[:-1])

    def test_all_states_native_columns(self):
        data = make_fixture(all_states=True)
        doc = MotfsmFile()
        doc.read(data)
        state = doc.bhvt.nodes[0].all_states[0]
        self.assertEqual((state.mAllState, state.mAllTransition, state.mAllTransitionID,
                          state.mAllStateEx, state.mAllTransitionAttributes),
                         (0x1234, -1, 0x80000001, 2, -1))
        self.assertEqual(doc.rebuild(), data)

    def test_layout_flags_require_structural_rebuild(self):
        doc = MotfsmFile()
        doc.read(make_fixture())
        binding = doc.bindings.get(doc.bhvt.nodes[0], "node_attribute")
        with self.assertRaisesRegex(ValueError, "layout"):
            doc.edit_field(binding, binding.value ^ 0x20)
        self.assertEqual(doc.rebuild(), make_fixture())
        doc.edit_field(binding, binding.value ^ 1)
        self.assertTrue(doc.bindings.modified)

    def test_viewer_edit_save_rebase_and_aliases(self):
        data = make_fixture()
        handler = get_handler_for_data(data, "fixture.motfsm2.43")
        self.assertIsInstance(handler, MotfsmHandler)
        handler.read(data)
        viewer = handler.create_viewer()
        try:
            changes = []
            viewer.modified_changed.connect(changes.append)
            nodes = viewer.tree.topLevelItem(0).child(0)
            nodes.setExpanded(True)
            nodes.child(0).setExpanded(True)
            details = nodes.child(0).child(1)
            rows = {details.child(i).text(0): details.child(i) for i in range(details.childCount())}
            self.assertFalse(rows['name'].flags() & Qt.ItemIsEditable)
            self.assertFalse(rows['is_fsm'].flags() & Qt.ItemIsEditable)
            rows['work_flags'].setText(1, '1')
            self.app.processEvents()
            self.assertTrue(viewer.modified)
            self.assertIn(True, changes)
            saved = handler.rebuild()
            self.assertNotEqual(saved, data)
            handler.mark_saved()
            self.assertFalse(viewer.modified)
            rows['work_flags'].setText(1, '0')
            self.app.processEvents()
            self.assertTrue(viewer.modified)
            self.assertEqual(handler.rebuild(), data)
        finally:
            viewer.close()
            viewer.deleteLater()

    def test_native_file_tab_save_checkpoints_fields(self):
        from ui.file_tab import FileTab
        data = make_fixture()
        notebook = QTabWidget()
        tab = FileTab(notebook, filename='fixture.motfsm2.43', data=data)
        notebook.addTab(tab.notebook_widget, 'Fixture')
        try:
            self.assertTrue(tab.initial_load_complete)
            binding = tab.handler.motfsm.bindings.get(tab.handler.motfsm.bhvt.nodes[0], 'work_flags')
            tab.handler.edit_field(binding, '1')
            self.assertTrue(tab.modified)
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'fixture.motfsm2.43'
                self.assertTrue(tab.handle_file_save(str(path)))
                self.assertFalse(tab.modified)
                self.assertFalse(tab.handler.modified)
                self.assertFalse(tab.handler.motfsm.bindings.modified)
                self.assertNotEqual(path.read_bytes(), data)
                tab.handler.edit_field(binding, '0')
                self.assertTrue(tab.modified)
                self.assertTrue(tab.handle_file_save(str(path)))
                self.assertEqual(path.read_bytes(), data)
        finally:
            notebook.close()
            notebook.deleteLater()

    def test_native_save_failure_keeps_pending_edits(self):
        from ui.file_tab import FileTab
        data = make_fixture()
        notebook = QTabWidget()
        tab = FileTab(notebook, filename='fixture.motfsm2.43', data=data)
        notebook.addTab(tab.notebook_widget, 'Fixture')
        try:
            binding = tab.handler.motfsm.bindings.get(tab.handler.motfsm.bhvt.nodes[0], 'work_flags')
            tab.handler.edit_field(binding, '1')
            def dismiss_error():
                for widget in self.app.topLevelWidgets():
                    if isinstance(widget, QMessageBox):
                        widget.accept()
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'missing-parent' / 'fixture.motfsm2.43'
                QTimer.singleShot(0, dismiss_error)
                self.assertFalse(tab.handle_file_save(str(path)))
            self.assertTrue(tab.modified)
            self.assertTrue(tab.handler.modified)
            self.assertTrue(tab.handler.motfsm.bindings.modified)
            self.assertEqual(tab.handler.motfsm.source, data)
        finally:
            notebook.close()
            notebook.deleteLater()


class SharedRszTests(unittest.TestCase):
    def test_string_count_and_field_spans_use_shared_cursor(self):
        fields = [
            {"name": name, "type": kind, "size": size, "align": align,
             "array": False, "native": False, "original_type": ""}
            for name, kind, size, align in [("text", "String", 4, 4), ("flag", "Bool", 1, 1), ("signed", "S8", 1, 1)]
        ]
        schema = {"1": {"name": "Fixture", "crc": "1", "fields": fields}}
        payload = struct.pack("<4s5I3Q", b"RSZ\0", 16, 1, 2, 0, 0, 56, 80, 72)
        payload += struct.pack("<I", 1) + bytes(4) + struct.pack("<4I", 0, 0, 1, 1) + bytes(8)
        payload += struct.pack("<I", 2) + "A\0".encode("utf-16le") + b"\x01\x80"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.json"
            path.write_text(json.dumps(schema), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                registry = TypeRegistry(str(path))
            parsed = RszFile()
            parsed.type_registry = registry
            spans = []
            parsed.field_observer = lambda idx, fd, obj, start, end: spans.append((fd['name'], start, end))
            parsed.read_headless(payload, validate_type_registry=True)
            self.assertEqual(parsed.parsed_elements[1]['text'].value.rstrip('\0'), 'A')
            self.assertTrue(parsed.parsed_elements[1]['flag'].value)
            self.assertEqual(parsed.parsed_elements[1]['signed'].value, -128)
            self.assertEqual(spans, [('text', 0, 8), ('flag', 8, 9), ('signed', 9, 10)])


if __name__ == '__main__':
    unittest.main()
