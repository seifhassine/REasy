"""MOTFSM integration checks using an explicit synthetic binary fixture."""
import contextlib
import io
import os
import struct
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from file_handlers.factory import get_handler_for_data
from file_handlers.motfsm.motfsm_file import BHVTNode, MotfsmFile
from file_handlers.motfsm.motfsm_handler import MotfsmHandler
from file_handlers.motfsm.rsz_parser import RSZFieldValue
from utils.binary_handler import BinaryHandler


def make_fixture():
    # One FSM node, child, action, state and transition, with empty RSZ blocks.
    node = b"".join([
        struct.pack("<IIIiI", 0x1234, 0, 0, -1, 0),
        struct.pack("<iIIi", 1, 0x1111, 2, 0),
        struct.pack("<iiii", -1, 1, 0, -1),
        struct.pack("<iIi", 1, 0x2222, 0),
        struct.pack("<iHH", 7, 0x20, 0),
        struct.pack("<IIiBB", 0x3333, 0x4444, 0, 1, 0),
        struct.pack("<iiiIiIII", 1, 1, 0, 22, -1, 33, 44, 55),
        struct.pack("<iiiIiI", 1, 1, 0, 66, -1, 77),
        struct.pack("<ii", 0, -1),
    ])
    offsets = [0] * 18
    offsets[0] = 152
    offsets[12] = 152 + 4 + len(node)
    name = "Fixture\0".encode("utf-16le")
    rsz_offset = (offsets[12] + len(name) + 15) & ~15
    offsets[1:12] = [rsz_offset] * 11
    tree = struct.pack("<II18Q", 0x54564842, 0, *offsets)
    tree += struct.pack("<I", 1) + node + name
    tree += bytes(rsz_offset - len(tree))
    tree += struct.pack("<4s5I3Q", b"RSZ\0", 16, 0, 0, 0, 0, 48, 48, 48)
    header = struct.pack("<II8x4Q3I", 1, 0x3273666D, 64, 0, 0, 60, 0, 0, 0)
    return header + struct.pack("<I", len(tree)) + tree, node


class MotfsmIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_scalar_writes_preserve_surrounding_bytes_and_cursor(self):
        cases = [
            ("u32", 0xFEDCBA98, "I"), ("s32", -123456, "i"),
            ("u16", 65535, "H"), ("s16", -32768, "h"),
            ("u8", 255, "B"), ("s8", -128, "b"),
            ("f32", 1.25, "f"), ("f64", -2.5, "d"),
            ("bool", True, "?"), ("bool", False, "?"),
        ]
        for type_name, value, fmt in cases:
            with self.subTest(type_name=type_name, value=value):
                original = bytes(range(32))
                handler = BinaryHandler(original)
                handler.seek(29)
                field = RSZFieldValue(type_name=type_name, value=value, offset=8)
                field.write_value_to_buffer(handler)
                self.assertEqual(bytes(handler.data), original)
                field._modified = True
                field.write_value_to_buffer(handler)
                expected = bytearray(original)
                struct.pack_into("<" + fmt, expected, 8, value)
                self.assertEqual(bytes(handler.data), bytes(expected))
                self.assertEqual(handler.tell, 29)

    def test_node_writer_matches_explicit_binary_layout(self):
        _, node_bytes = make_fixture()
        node = BHVTNode()
        node.read(BinaryHandler(node_bytes), 0)
        output = BinaryHandler(bytearray())
        node.write(output)
        self.assertEqual(output.get_bytes(), node_bytes)

    def test_reference_edits_only_change_target_bytes(self):
        original, _ = make_fixture()
        motfsm = MotfsmFile()
        motfsm.read(original)
        self.assertEqual(motfsm.rebuild(), original)
        node = motfsm.bhvt.nodes[0]
        edits = [
            (node, "id_hash", 0xAABBCCDD, "I"),
            (node, "priority", 42, "i"),
            (node, "selector_id", 3, "i"),
            (node.children[0], "id_hash", 0x55667788, "I"),
            (node.actions[0], "index", 5, "i"),
            (node.states[0], "TransitionConditions", 6, "i"),
            (node.states[0], "TransitionMaps", 0x99887766, "I"),
            (node.transitions[0], "mStartState", 0x11223344, "I"),
            (node.transitions[0], "mStartStateTransition", 8, "i"),
        ]
        expected = bytearray(original)
        for owner, name, value, fmt in edits:
            setattr(owner, name, value)
            struct.pack_into("<" + fmt, expected, getattr(owner, "_" + name + "_offset"), value)
        output = motfsm.rebuild()
        self.assertEqual(output, bytes(expected))
        reread = MotfsmFile()
        reread.read(output)
        self.assertEqual(reread.bhvt.nodes[0].states[0].TransitionConditions, 6)
        self.assertEqual(reread.bhvt.nodes[0].transitions[0].mStartStateTransition, 8)

    def test_factory_viewer_edit_and_rebuild(self):
        original, _ = make_fixture()
        handler = get_handler_for_data(original, "fixture.motfsm2.1")
        self.assertIsInstance(handler, MotfsmHandler)
        handler.read(original)
        with contextlib.redirect_stdout(io.StringIO()):
            viewer = handler.create_viewer()
            self.assertIsNotNone(viewer)
            try:
                changes = []
                viewer.modified_changed.connect(changes.append)
                nodes = viewer.tree.topLevelItem(0).child(0)
                nodes.setExpanded(True)
                node_item = nodes.child(0)
                node_item.setExpanded(True)
                pending = [node_item]
                priority = None
                while pending:
                    item = pending.pop()
                    if item.text(0) == "priority":
                        priority = item
                        break
                    pending.extend(item.child(i) for i in range(item.childCount()))
                self.assertIsNotNone(priority)
                priority.setText(1, "99")
                self.assertIn(True, changes)
                reread = MotfsmFile()
                reread.read(handler.rebuild())
                self.assertEqual(reread.bhvt.nodes[0].priority, 99)
            finally:
                viewer.close()
                viewer.deleteLater()


if __name__ == "__main__":
    unittest.main()
