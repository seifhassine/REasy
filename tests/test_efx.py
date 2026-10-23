"""Golden EFX layouts, file-version dispatch and editing/save contracts."""
import os
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QTreeWidgetItemIterator

from file_handlers.efx.efx_file import (
    EfxFile, EfxParseError, Reader, read_record, write_record,
)
from file_handlers.efx.efx_handler import EfxHandler
from file_handlers.factory import get_handler_for_data
from tools.cli.formats.efx import document_data
from utils.hash_util import murmur3_hash_utf16le


def wide(text):
    raw = (text + "\0").encode("utf-16le")
    return struct.pack("<i", len(raw)) + raw


def golden(version=2818730, *, embedded=False):
    bone = "L_Nose_00"
    strings = (b"" if embedded else bone.encode() + b"\0" + (bone + "\0").encode("utf-16le")) + b"flame\0"
    parent = struct.pack("<9i", *([1] * 9))
    if version == 2818730:
        parent += struct.pack("<B2f4if", 1, 0.5, 1, 2, 3, 4, 5, 0.25)
    else:
        parent += struct.pack("<I", 1)
    parent += wide(bone)
    shader = struct.pack("<3I", 0x11223344, 0x55667788, 9) + wide("vfx/火焰.vsdf")
    transform = struct.pack("<9fi", 0, 1, 2, 0, 0, 0, 1, 1, 1, 0)
    attributes = struct.pack("<ii", 2, 101) + shader
    attributes += struct.pack("<ii", 10, 102) + transform
    attributes += struct.pack("<ii", 15, 103) + parent
    body = (b"" if embedded else struct.pack("<II", 0xAABBCCDD, 0))
    body += struct.pack("<h", 0) + struct.pack("<iIii", 0, 0x12345678, 1, 3) + attributes + bytes(8)
    header = struct.pack("<4s11i", b"efxr", 1, 1, len(strings), 0, 0, 0, 0, 0, int(not embedded), 1, 0)
    return header + strings + body


class EfxTests(unittest.TestCase):
    def test_golden_roundtrip_and_typed_fields(self):
        for version in (2621987, 2818730):
            with self.subTest(version=version):
                raw = golden(version)
                document = EfxFile.from_bytes(raw, version=version)
                self.assertEqual(document.build_validated(), raw)
                shader, transform, parent = document.entries[0].attributes
                self.assertEqual(shader.body["shaderPath"], "vfx/火焰.vsdf")
                self.assertEqual(transform.body["LocalPosition"], (0, 1, 2))
                self.assertIs(parent.parent_bone, document.bones[0])
                self.assertEqual(parent.body["BoneName"], "L_Nose_00")

    def test_version_is_explicit_and_layout_specific(self):
        handler = get_handler_for_data(golden(), "effect.efx.2818730")
        self.assertIsInstance(handler, EfxHandler)
        handler.app = SimpleNamespace(settings={"game_version": "RE4"})
        handler.filepath = "effect.efx.2818730.x64"
        handler.read(golden())
        self.assertEqual(handler.efx.version, 2818730)
        self.assertIn("ConstFrame", handler.efx.entries[0].attributes[-1].body.fields)
        handler.filepath = "effect.efx.2621987"
        handler.read(golden(2621987))
        self.assertNotIn("ConstFrame", handler.efx.entries[0].attributes[-1].body.fields)
        for path in ("effect.efx", "effect.efx.9999999"):
            handler.filepath = path
            with self.assertRaises(EfxParseError):
                handler.read(golden())
        with self.assertRaises(EfxParseError):
            EfxFile.from_bytes(golden(), version=2621987)

    def test_position_edit_only_changes_its_float(self):
        raw = golden()
        document = EfxFile.from_bytes(raw, version=2818730)
        transform = document.entries[0].attributes[1].body
        field = transform.fields["LocalPosition"]
        transform["LocalPosition"] = (0, -0.2, 2)
        expected = bytearray(raw)
        struct.pack_into("<f", expected, field.offset + 4, -0.2)
        output = document.build_validated()
        self.assertEqual(output, expected)
        reopened = EfxFile.from_bytes(output, version=2818730)
        self.assertAlmostEqual(reopened.entries[0].attributes[1].body["LocalPosition"][1], -0.2)

    def test_sunbreak_no_draw_axis_pairs_and_fixed_yaw_offset(self):
        definition = Reader(b"", 2818730).schema["AttributeTypes"]["TypeNoDraw"]
        raw = struct.pack("<IIIi12f", 7, 0xFFFFFFFF, 0, 0,
                          0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 10, 0, 1, 0, 10, 0)
        record = read_record(Reader(raw, 2818730), definition["Classname"], definition=definition)
        self.assertEqual(write_record(record), raw)
        for index, name in enumerate(("RotationX", "RotationXRandom", "RotationY", "RotationYRandom",
                                      "RotationZ", "RotationZRandom")):
            self.assertEqual(record.fields[name].offset, 16 + index * 4)
            self.assertAlmostEqual(record[name], (index + 1) / 10)
        self.assertEqual(tuple(record[f"Size{axis}"] for axis in "XYZ"), (10, 1, 10))
        self.assertEqual(tuple(record[f"Size{axis}Random"] for axis in "XYZ"), (0, 0, 0))
        record["RotationY"] = -1.5
        expected = bytearray(raw)
        struct.pack_into("<f", expected, 24, -1.5)
        self.assertEqual(write_record(record), expected)

    def test_binding_edit_updates_table_hash_and_relation(self):
        document = EfxFile.from_bytes(golden(), version=2818730)
        document.entries[0].attributes[-1].body["BoneName"] = "Head_00"
        reopened = EfxFile.from_bytes(document.build_validated(), version=2818730)
        self.assertEqual([bone.name for bone in reopened.bones], ["L_Nose_00", "Head_00"])
        self.assertEqual(reopened.bones[1].name_hash, murmur3_hash_utf16le("Head_00"))
        self.assertIs(reopened.entries[0].attributes[-1].parent_bone, reopened.bones[1])
        document.entries[0].attributes[-1].body["BoneName"] = "L_Nose_00"
        reverted = EfxFile.from_bytes(document.build_validated(), version=2818730)
        self.assertIs(reverted.entries[0].attributes[-1].parent_bone, reverted.bones[0])

    def test_embedded_effect_inherits_and_updates_bones(self):
        child_bytes = golden(embedded=True)
        strings = b"L_Nose_00\0" + "L_Nose_00\0".encode("utf-16le") + b"spawn\0"
        raw = struct.pack("<4s11i", b"efxr", 1, 0, len(strings), 1, 0, 0, 0, 0, 1, 0, 0)
        raw += strings + struct.pack("<IIiIiiiI", 0xAABBCCDD, 0, 0, 0x12345678, 1, 141, 201, len(child_bytes)) + child_bytes + bytes(8)
        root = EfxFile.from_bytes(raw, version=2818730)
        self.assertEqual(root.build_validated(), raw)
        child = root.actions[0].attributes[0].body["efxrData"]
        self.assertIs(child.entries[0].attributes[-1].parent_bone, root.bones[0])
        child.entries[0].attributes[-1].body["BoneName"] = "Head_00"
        reopened = EfxFile.from_bytes(root.build_validated(), version=2818730)
        self.assertEqual(len(reopened.bones), 2)
        self.assertEqual(len(reopened.actions[0].attributes[0].body["efxrData"].bones), 0)
        self.assertIs(reopened.actions[0].attributes[0].body["efxrData"].entries[0].attributes[-1].parent_bone, reopened.bones[1])

    def test_unknown_attributes_and_truncation_are_errors(self):
        raw = golden()
        document = EfxFile.from_bytes(raw, version=2818730)
        offset = document.entries[0].attributes[0].body.fields["shaderCRCHash0"].offset - 8
        changed = bytearray(raw)
        struct.pack_into("<i", changed, offset, 9999)
        with self.assertRaisesRegex(EfxParseError, "attribute ID 9999"):
            EfxFile.from_bytes(changed, version=2818730)
        for size in (3, 47, len(raw) - 1):
            with self.subTest(size=size), self.assertRaises(EfxParseError):
                EfxFile.from_bytes(raw[:size], version=2818730)
        with self.assertRaisesRegex(EfxParseError, "unconsumed"):
            EfxFile.from_bytes(raw + b"\0", version=2818730)

    def test_expression_solver_length_is_bytes(self):
        # One expression with no parameters and one float component. The solver
        # declares 16 bytes, not a count of 16 expressions.
        raw = struct.pack("<Iiii f", 16, 0, 1, 0, 2.5)
        record = read_record(Reader(raw, 2818730), "ReeLib.Efx.Structs.Common.EFXExpressionList")
        self.assertEqual(len(record["expressions"]), 1)
        self.assertEqual(write_record(record), raw)

    def test_array_and_vector_edits_preserve_other_nan_bits(self):
        for kind, array in (("Vec2", False), ("F32", True)):
            definition = {"Fields": [{"Name": "values", "FieldType": kind,
                                      **({"IsArray": True, "FixedLength": 2} if array else {})}]}
            raw = struct.pack("<II", 0x7F812345, 0x3F800000)
            record = read_record(Reader(raw, 2818730), "Fixture", definition=definition)
            record["values"] = [record["values"][0], 3.0]
            self.assertEqual(write_record(record), raw[:4] + struct.pack("<f", 3))

    def test_cli_query_exposes_file_version_and_bindings(self):
        data = document_data(EfxFile.from_bytes(golden(), version=2818730))
        self.assertEqual(data["version"], 2818730)
        self.assertEqual(data["dimension"], 1)
        self.assertEqual(data["entries"][0]["attributes"][-1]["parent_bone"], "L_Nose_00")


class EfxViewerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_gui_edit_rebuild_and_invalid_value(self):
        handler = EfxHandler()
        handler.filepath = "fixture.efx.2818730"
        handler.read(golden())
        viewer = handler.create_viewer()
        iterator = QTreeWidgetItemIterator(viewer.tree)
        rows = {}
        while iterator.value():
            row = iterator.value()
            if row.text(0) == "BoneName":
                rows["bone"] = row
            if row.text(0) == "1" and row.parent() and row.parent().text(0) == "LocalPosition":
                rows["position"] = row
            iterator += 1
        rows["position"].setText(1, "-0.2")
        rows["bone"].setText(1, "Head_00")
        self.assertTrue(handler.modified)
        reopened = EfxFile.from_bytes(handler.rebuild(), version=2818730)
        self.assertAlmostEqual(reopened.entries[0].attributes[1].body["LocalPosition"][1], -0.2)
        self.assertEqual(reopened.entries[0].attributes[-1].parent_bone.name, "Head_00")
        with patch("file_handlers.efx.efx_viewer.QMessageBox.warning") as warning:
            rows["position"].setText(1, "wrong")
            warning.assert_called_once()
        self.assertEqual(rows["position"].text(1), "-0.2")
        self.assertFalse(viewer.tree.topLevelItem(0).flags() & Qt.ItemFlag.ItemIsEditable)
        viewer.close()


if __name__ == "__main__":
    unittest.main()
