"""Native Action -> bank -> motion linkage and shared document preview."""
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QPoint, Qt, QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from file_handlers.motbank.motbank_file import MotbankFile, MotlistItem
from file_handlers.motfsm.motfsm_handler import MotfsmHandler
from file_handlers.motfsm.motion_link import (ActionMotion, MotionLinkResolver, default_bank_path,
                                             linked_context, node_motions, motion_entry_index)
from file_handlers.motfsm.motion_preview import FsmMotionPreview
from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC
from file_handlers.motion.errors import MotionParseError
from file_handlers.motion.motlist_handler import MotListHandler
from file_handlers.motion.preview.clip_timeline import ClipTimelineView, ClipLane
from file_handlers.motion.mot_clip.model import ClipProperty, ClipPropertyType, ClipKey
from file_handlers.motion.preview.mhr_editor import MhrMotListEditor
from utils.resource_file_utils import ResourceResolutionContext

CORPUS = Path(__file__).parent/'TESTFILE'


class LinkResolutionTests(unittest.TestCase):
    def test_project_resource_matching_is_case_insensitive_and_prefers_exact_file(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)/'natives/stm/player/mot'
            folder.mkdir(parents=True)
            (folder/'plw_Mixed_bank.motbank.3.bak').write_bytes(b'backup')
            native = folder/'plw_Mixed_bank.motbank.3'
            native.write_bytes(b'current')
            context = ResourceResolutionContext(project_dir=directory)
            self.assertEqual(context.resolve('player/mot/plw_mixed_BANK.motbank.3')[1], b'current')

    def test_bank_ids_are_native_metadata_and_ambiguous_missing_disabled_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'mapping.motbank.3'
            bank = MotbankFile()
            bank.version = 3
            bank.items = [MotlistItem(bank_id=77, path='player/mot/arbitrary_name.motlist')]
            path.write_bytes(bank.write())
            action = ActionMotion(0, 12345, 2, 77, 900, '', True)
            resolver = MotionLinkResolver(ResourceResolutionContext(), str(path))
            self.assertEqual(resolver.motion_path(action), 'player/mot/arbitrary_name.motlist.528')
            bank.items.append(MotlistItem(bank_id=77, path='player/mot/another.motlist'))
            path.write_bytes(bank.write())
            with self.assertRaisesRegex(ValueError, '2 MOTLIST'):
                MotionLinkResolver(ResourceResolutionContext(), str(path)).motion_path(action)
            with self.assertRaisesRegex(ValueError, 'Missing resource'):
                MotionLinkResolver(ResourceResolutionContext(), 'missing.motbank.3').motion_path(action)
            with self.assertRaisesRegex(ValueError, 'disabled'):
                resolver.motion_path(ActionMotion(0, 12345, 2, 77, 900, '', False))

    def test_motion_id_is_not_slot_index_and_does_not_fall_back(self):
        entries = [SimpleNamespace(motion_id=500), SimpleNamespace(motion_id=20)]
        self.assertEqual(motion_entry_index(entries, 20), 1)
        with self.assertRaisesRegex(ValueError, '0 playable'):
            motion_entry_index(entries, 1)
        with self.assertRaisesRegex(ValueError, '2 playable'):
            motion_entry_index(entries+[entries[1]], 20)

    def test_preview_timeline_seeks_without_editing_keys(self):
        app = QApplication.instance() or QApplication([])
        commits, frames = [], []
        prop = ClipProperty('event', ClipPropertyType.S32, 0, 20, keys=[ClipKey(10, value=123)])
        view = ClipTimelineView(lambda: commits.append(True))
        view.read_only = True
        view.set_lanes([ClipLane('SOUND', 'event', prop, ())], 100)
        view.frame_requested.connect(frames.append)
        view.resize(800, 220); view.show(); app.processEvents()
        try:
            start = QPoint(round(view.x_at(10)), view.RULER_HEIGHT+13)
            end = QPoint(round(view.x_at(15)), view.RULER_HEIGHT+13)
            QTest.mousePress(view.viewport(), Qt.LeftButton, pos=start)
            QTest.mouseMove(view.viewport(), end)
            QTest.mouseRelease(view.viewport(), Qt.LeftButton, pos=end)
            self.assertEqual(prop.keys[0].frame, 10)
            self.assertFalse(commits)
            self.assertTrue(frames)
        finally:
            view.close()


@unittest.skipUnless(CORPUS.is_dir(), 'local native corpus is absent')
class NativeLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        for path in sorted((CORPUS/'natives/STM/player/Fsm').rglob('*.motfsm2.*')):
            if not default_bank_path(path):
                continue
            handler = MotfsmHandler()
            handler.filepath = str(path.resolve())
            handler.read(path.read_bytes())
            resolver = MotionLinkResolver(linked_context(handler), default_bank_path(path))
            for index, node in enumerate(handler.motfsm.bhvt.nodes):
                for action in node_motions(handler.motfsm, index):
                    if not action.enabled:
                        continue
                    resource = resolver.motion_path(action)
                    source, data = resolver.load_resource(resource)
                    try:
                        model = MHR_MOTION_FORMAT_CODEC.parse(data, label=source)
                    except MotionParseError as exc:
                        if 'Motion Tree' in str(exc):
                            continue
                        raise
                    matches = [i for i, s in enumerate(model.slots) if s.motion_id == action.motion_id and s.payload]
                    if len(matches) == 1 and matches[0] != action.motion_id and model.slots[matches[0]].payload.value.sequences:
                        cls.path, cls.index, cls.action = path, index, action
                        cls.source, cls.data = source, data
                        return
        raise AssertionError('Native corpus has no playback Action with CLIP and a distinct slot index')

    def make_handler(self):
        handler = MotfsmHandler()
        handler.filepath = str(self.path.resolve())
        handler.read(self.path.read_bytes())
        return handler

    def test_native_action_field_edit_retargets_preview_and_roundtrips(self):
        handler = self.make_handler()
        panel = FsmMotionPreview(handler)
        try:
            panel.set_node(self.index)
            self.assertIsNotNone(panel.editor, panel.status.text())
            self.assertEqual(panel.editor.preview.current_entry.motion_id, self.action.motion_id)
            self.assertTrue(panel.editor.timeline.all_lanes)
            self.assertEqual(panel.editor.handler.rebuild(), self.data)
            reference = handler.motfsm.bhvt.nodes[self.index].actions[self.action.action_index]
            instance = handler.motfsm.references.action(reference.id_hash, reference.ex_id)
            field = next(field for field in instance.fields if field.name == 'v4_MotionID')
            replacement = next(s.motion_id for s in panel.editor.document.slots if s.payload and s.motion_id != self.action.motion_id)
            handler.edit_field(field.binding, str(replacement))
            panel.set_node(self.index)
            self.assertEqual(panel.editor.preview.current_entry.motion_id, replacement)
            self.assertEqual(panel.actions.currentData().action_id, self.action.action_id)
            reopened = MotfsmHandler(); reopened.read(handler.rebuild())
            reopened_action = next(a for a in node_motions(reopened.motfsm, self.index)
                                   if (a.action_id, a.ex_id) == (self.action.action_id, self.action.ex_id))
            self.assertEqual(reopened_action.motion_id, replacement)
            used = {s.motion_id for s in panel.editor.document.slots}
            missing = next(i for i in range(0x10000) if i not in used)
            handler.edit_field(field.binding, str(missing))
            panel.set_node(self.index)
            self.assertTrue(panel.editor.isHidden())
            self.assertIn('0 playable matches', panel.status.text())
            self.assertEqual(Path(self.source).read_bytes(), self.data)
        finally:
            panel.cleanup(); panel.close()

    def test_open_motion_document_is_shared_and_saved_edits_refresh_both_views(self):
        handler = self.make_handler()
        motion = MotListHandler()
        motion.filepath = self.source
        motion.resource_context = linked_context(handler)
        motion.read(self.data)
        editor = MhrMotListEditor(motion)
        tab = SimpleNamespace(filename=self.source, handler=motion)
        tabs = [tab]
        handler.app = SimpleNamespace(settings={}, project_workspace=SimpleNamespace(
            sessions=SimpleNamespace(active_tabs=lambda: tabs)))
        panel = FsmMotionPreview(handler)
        try:
            panel.set_node(self.index)
            self.assertIs(panel.editor.handler, motion)
            self.assertIs(panel.editor.document, editor.document)
            index = motion_entry_index(editor.preview._motions, self.action.motion_id)
            editor.preview.motion_browser.animation_list.setCurrentRow(index)
            field = next(f for f in editor.document.fields if f.editable
                         and f.owner is editor.preview.current_motion and f.attribute == 'end_frame')
            original = field.get()
            field.set(original+1)
            editor._commit_timeline()
            self.assertTrue(motion.modified)
            self.assertIs(panel.editor.document, editor.document)
            self.assertEqual(panel.editor.preview.current_motion.end_frame, original+1)
            saved = motion.rebuild()
            self.assertIs(panel.editor.document, motion.model)
            self.assertIs(editor.document, motion.model)
            self.assertFalse(motion.modified)
            self.assertNotEqual(saved, self.data)
            self.assertEqual(MHR_MOTION_FORMAT_CODEC.write(motion.model), saved)
            self.assertEqual(panel.editor.preview.current_entry.motion_id, self.action.motion_id)
            self.assertEqual(panel.editor.preview.current_motion.end_frame, original+1)
            self.assertEqual(Path(self.source).read_bytes(), self.data)
            # Closing the source editor discards its in-memory document; the
            # remaining FSM preview must reload the actual disk resource.
            editor.cleanup()
            tabs.clear()
            motion.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            self.assertIsNone(panel.editor)
            panel._load_selected()
            self.assertIsNotNone(panel.editor, panel.status.text())
            self.assertIsNot(panel.editor.handler, motion)
            self.assertEqual(panel.editor.preview.current_motion.end_frame, original)
        finally:
            panel.cleanup(); panel.close(); editor.cleanup(); editor.close()


if __name__ == '__main__':
    unittest.main()
