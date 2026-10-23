"""World EFX version dispatch, typed parsing, Qt inspection and native corpus."""
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QTreeWidgetItemIterator

from file_handlers.efx.efx_file import EfxParseError, EfxFile
from file_handlers.efx.format_registry import identify_efx, read_efx
from file_handlers.efx.mhw_efx_file import MhwEfxFile
from file_handlers.factory import get_handler_for_data
from tools.cli.formats.efx import document_data, query
from types import SimpleNamespace


def golden(*, version=711800, unknown_attribute=False):
    labels = b"mouth\0"
    # World 711800 has its version at offset 4, a secondary efxr marker at
    # offset 28, and a 72-byte header. ParentOptions is a 60-byte attribute.
    header = struct.pack("<4si5i4s10I", b"EFX\0", version,
                         0x18020800, 0, 0x4AC16F23, 0x18020800, 0x18020800,
                         b"efxr", 1, 0xFFFFFFFF, 1, len(labels), 0, 0, 0, 0, 0, 0)
    entry = struct.pack("<IiiiI", 1540471583, 0, 1, 0, 0)
    parent = struct.pack("<15i", 1, *([0] * 13), 7)
    return header + labels + entry + struct.pack("<I", 0x11223344 if unknown_attribute else 368199626) + parent


class MhwEfxTests(unittest.TestCase):
    def test_dispatch_uses_internal_version_and_distinct_container(self):
        raw = golden()
        profile = identify_efx(raw, "mouth.efx")
        self.assertEqual((profile.format, profile.version), ("MHW_EFX", 711800))
        self.assertEqual(identify_efx(raw, "mouth.efx.711800.x64"), profile)
        handler = get_handler_for_data(raw, "mouth.efx")
        handler.app = SimpleNamespace(settings={"game_version": "MHRise"})
        handler.filepath = "mouth.efx"
        handler.read(raw)
        self.assertIsInstance(handler.efx, MhwEfxFile)
        self.assertFalse(handler.supports_editing())
        self.assertEqual(handler.rebuild(), raw)

    def test_version_mismatches_and_unregistered_versions_fail(self):
        for data, path in ((golden(), "mouth.efx.2818730"),
                           (golden(version=2818730), "mouth.efx"),
                           (golden(version=711799), "mouth.efx")):
            with self.subTest(path=path), self.assertRaises(EfxParseError):
                read_efx(data, path)
        with self.assertRaisesRegex(EfxParseError, "numeric file version"):
            read_efx(b"efxr" + bytes(68), "mouth.efx")
        with self.assertRaisesRegex(EfxParseError, "Unsupported EFX version 711800"):
            read_efx(b"efxr" + bytes(68), "mouth.efx.711800")

    def test_fields_labels_and_joint_are_decoded(self):
        document = read_efx(golden(), "mouth.efx").document
        self.assertEqual(document["entries"][0]["name"], "mouth")
        attribute = document["entries"][0]["attributes"][0]
        self.assertEqual(attribute["type"], "PARENTOPTIONS")
        self.assertEqual(attribute["fields"]["jointNo"], 7)
        self.assertEqual(attribute["fields"]["relationPos"], [0, 0, 0])
        self.assertTrue(document["roundtrip_exact"])
        json.dumps(document, allow_nan=False)

    def test_parse_failures_cannot_be_reported_as_opaque_success(self):
        with self.assertRaises(EfxParseError):
            read_efx(golden(unknown_attribute=True), "mouth.efx")
        for size in (3, 71, len(golden()) - 1):
            with self.subTest(size=size), self.assertRaises(EfxParseError):
                read_efx(golden()[:size], "mouth.efx")
        bad_marker = bytearray(golden())
        bad_marker[28:32] = b"bad!"
        with self.assertRaisesRegex(EfxParseError, "signature"):
            read_efx(bad_marker, "mouth.efx")

    def test_cli_reads_unsuffixed_world_efx(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mouth.efx"
            path.write_bytes(golden())
            result = query(SimpleNamespace(source=path, game_dir=None))
        self.assertEqual(result["format"], "MHW_EFX")
        self.assertEqual(result["version"], 711800)
        self.assertEqual(result["entries"][0]["attributes"][0]["fields"]["jointNo"], 7)

    def test_inspection_snapshot_cannot_silently_change_serialized_data(self):
        document = read_efx(golden(), "mouth.efx")
        snapshot = document.document
        snapshot["entries"][0]["attributes"][0]["fields"]["jointNo"] = 9
        self.assertEqual(document.document["entries"][0]["attributes"][0]["fields"]["jointNo"], 7)
        self.assertEqual(document.build_validated(), golden())


class MhwEfxViewerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_world_viewer_shows_fields_without_editing_flags(self):
        handler = get_handler_for_data(golden(), "mouth.efx")
        handler.filepath = "mouth.efx"
        handler.read(golden())
        viewer = handler.create_viewer()
        found = False
        iterator = QTreeWidgetItemIterator(viewer.tree)
        while iterator.value():
            row = iterator.value()
            self.assertFalse(row.flags() & Qt.ItemFlag.ItemIsEditable)
            if row.text(0) == "jointNo":
                self.assertEqual(row.text(1), "7")
                found = True
            iterator += 1
        self.assertTrue(found)
        viewer.close()


@unittest.skipUnless(os.environ.get("REASY_MHW_EFX_CORPUS"), "Set REASY_MHW_EFX_CORPUS to a World EFX corpus")
class MhwEfxCorpusTests(unittest.TestCase):
    def test_native_files_have_full_typed_parse_and_exact_roundtrip(self):
        files = sorted(Path(os.environ["REASY_MHW_EFX_CORPUS"]).rglob("*.efx"))
        self.assertTrue(files)
        types = set()
        timelines = 0
        for path in files:
            with self.subTest(path=path):
                raw = path.read_bytes()
                document = read_efx(raw, str(path))
                self.assertEqual(document.build_validated(), raw)
                value = document_data(document)
                self.assertEqual(value["version"], 711800)
                for entry in value["entries"]:
                    types.update(attribute["type"] for attribute in entry.get("attributes", []))
                    if entry.get("timl"):
                        timelines += 1
        self.assertTrue({"UVSEQUENCE", "RGBFIRE", "RIBBON", "MATERIAL", "PTBEHAVIOR"} <= types)
        self.assertGreater(timelines, 0)


if __name__ == "__main__":
    unittest.main()
