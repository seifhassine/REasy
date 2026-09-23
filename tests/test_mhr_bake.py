"""Inclusive-range baking, native serialization, CLI and editor regressions."""
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import asdict
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from file_handlers.motion.errors import MotionWriteError
from file_handlers.motion.mhr_bake import MotionSegment, bake_segments, sample_segments
from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as CODEC
from file_handlers.motion.mhr_editing import next_motion_id
from file_handlers.motion.mhr_storage import MhrJoint, MhrMotion
from file_handlers.motion.mot.model import AnimationNode, KeyTrack, Skeleton, TrackFamily
from file_handlers.motion.evaluation.sampling import sample_track


CORPUS = Path(__file__).parent / 'TESTFILE/natives/STM/player/mot'


def motion(first, last):
    joint = MhrJoint('root', translation=(3., 4., 5.), binding_hash=1)
    return MhrMotion('test', end_frame=10, skeleton=Skeleton([joint]), animation_nodes=[
        AnimationNode(joint, translation=KeyTrack(TrackFamily.VECTOR3, [0, 10],
                                                 [(first, 0., 0.), (last, 0., 0.)]))])


class SegmentSamplingTests(unittest.TestCase):
    def test_endpoints_survive_on_adjacent_frames_and_each_speed_is_applied(self):
        a, b = motion(0., 10.), motion(100., 120.)
        nodes, ranges, end = sample_segments([a, b], [MotionSegment(1, 2, 6, 2), MotionSegment(2, 0, 2, .5)], a)
        self.assertEqual(end, 7)
        self.assertEqual([(r['output_start'], r['output_end']) for r in ranges], [(0, 2), (3, 7)])
        self.assertEqual(nodes[0].translation.frames, list(range(8)))
        self.assertEqual([v[0] for v in nodes[0].translation.values], [2, 4, 6, 100, 101, 102, 103, 104])
        self.assertEqual(sample_track(nodes[0].translation, 2.5), (53., 0., 0.))
        self.assertEqual(a.animation_nodes[0].translation.frames, [0, 10])

    def test_channel_union_uses_rest_values_and_keeps_rotations(self):
        a, b = motion(0., 10.), motion(100., 120.)
        b.animation_nodes[0].translation = None
        b.animation_nodes[0].rotation = KeyTrack(TrackFamily.QUATERNION, [0, 10],
                                                [(0., 0., 0., 1.), (0., 1., 0., 0.)])
        nodes, _, end = sample_segments([a, b], [MotionSegment(1, 0, 1), MotionSegment(2, 0, 10)], a)
        self.assertEqual(end, 12)
        self.assertEqual(nodes[0].translation.values[2:], [(3., 4., 5.)] * 11)
        self.assertEqual(nodes[0].rotation.values[:3], [(0., 0., 0., 1.)] * 3)
        self.assertAlmostEqual(sum(x*x for x in nodes[0].rotation.values[7]), 1)
        self.assertEqual(nodes[0].rotation.values[-1], (0., 1., 0., 0.))

    def test_single_frame_fractional_ranges_and_fixed_60_fps_source_conversion(self):
        a = motion(0., 10.)
        a.frames_per_second = 120
        target = motion(0., 10.)
        target.frames_per_second = 30
        nodes, ranges, end = sample_segments([a, a],
            [MotionSegment(1, 2.5, 2.5), MotionSegment(1, .5, 8.5)], target)
        self.assertEqual(end, 5)
        self.assertEqual([v[0] for v in nodes[0].translation.values], [2.5, .5, 2.5, 4.5, 6.5, 8.5])
        self.assertEqual(ranges[1]['output_start'], 1)

    def test_invalid_ranges_weights_and_rigs_are_rejected(self):
        a = motion(0., 10.)
        for segment in [MotionSegment(1, -1, 2), MotionSegment(1, 3, 2), MotionSegment(1, 0, 11),
                        MotionSegment(1, 0, 1, 0), MotionSegment(1, 0, 1, float('nan')),
                        MotionSegment(1, 0, float('inf'))]:
            with self.subTest(segment=segment), self.assertRaises(MotionWriteError):
                sample_segments([a], [segment], a)
        with self.assertRaises(MotionWriteError):
            sample_segments([], [], a)
        b = motion(1., 2.)
        b.animation_nodes[0].weight = .5
        with self.assertRaisesRegex(MotionWriteError, 'weights differ'):
            sample_segments([a, b], [MotionSegment(1, 0, 1), MotionSegment(2, 0, 1)], a)
        b.skeleton.joints[0].translation = (0., 0., 0.)
        with self.assertRaisesRegex(MotionWriteError, 'same skeleton'):
            sample_segments([b], [MotionSegment(2, 0, 1)], a)


def slot_snapshot(slot):
    m = slot.payload.value if slot.payload else None
    return (slot.motion_id, slot.tag_hash, repr([asdict(s) for s in slot.overrides]), None if m is None else (
        m.name, m.end_frame, m.raw_start_frame, m.raw_end_frame, m.looping, m.frames_per_second,
        [(j.binding_hash, j.name, j.parent.name if j.parent else None, repr(j.translation), repr(j.rotation))
         for j in m.skeleton.joints],
        [(n.joint.binding_hash, n.weight, n.translation, n.rotation, n.scale) for n in m.animation_nodes],
        repr([asdict(s) for s in m.sequences])))


@unittest.skipUnless(CORPUS.is_dir(), 'Native motion corpus unavailable')
class NativeBakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = CORPUS / 'plw_LongSword_100.motlist.528'
        cls.document = CODEC.parse(cls.path.read_bytes())
        cls.ids = [s.motion_id for s in cls.document.slots if s.payload and s.payload.value.end_frame > 40][:3]

    def test_native_replace_source_slot_preserves_others_events_and_seam(self):
        a, b, _ = self.ids
        before = [slot_snapshot(s) for s in self.document.slots]
        result, ranges = bake_segments(self.document, [MotionSegment(a, 10, 30, 2), MotionSegment(b, 20, 40, .5)],
                                      a, replace_existing=True)
        target = next(s for s in result.slots if s.motion_id == a)
        self.assertEqual(target.payload.value.end_frame, 51)
        pointer_table = struct.unpack_from('<Q', result.source, 16)[0]
        slot_index = result.slots.index(target)
        base = struct.unpack_from('<Q', result.source, pointer_table + slot_index * 8)[0]
        self.assertEqual(struct.unpack_from('<Q', result.source, base + 24)[0], 0x80)
        for old, new, snapshot in zip(self.document.slots, result.slots, before, strict=True):
            self.assertEqual(slot_snapshot(old), snapshot)
            if old.motion_id != a:
                self.assertEqual(slot_snapshot(new), snapshot)
            else:
                self.assertEqual([asdict(s) for s in new.overrides], [asdict(s) for s in old.overrides])
                self.assertEqual([asdict(s) for s in new.payload.value.sequences],
                                 [asdict(s) for s in old.payload.value.sequences])
        source = [next(s.payload.value for s in self.document.slots if s.motion_id == i) for i in (a, b)]
        for index, source_frame, output_frame in ((0, 10, 0), (0, 30, 10), (1, 20, 11), (1, 40, 51)):
            baked = {n.joint.binding_hash: n for n in target.payload.value.animation_nodes}
            for node in source[index].animation_nodes:
                for channel in ('translation', 'rotation', 'scale'):
                    track = getattr(node, channel)
                    if track is None:
                        continue
                    expected = sample_track(track, source_frame)
                    actual = sample_track(getattr(baked[node.joint.binding_hash], channel), output_frame)
                    error = lambda sign: max(abs(x-sign*y) for x, y in zip(expected, actual))
                    self.assertLess(min(error(1), error(-1)) if channel == 'rotation' else error(1), 3e-5)
        self.assertEqual(CODEC.write(result), result.source)
        self.assertEqual(ranges[1]['output_start'], 11)

    def test_new_slot_and_repeated_bake_roundtrip(self):
        a, b, _ = self.ids
        new_id = next_motion_id(self.document)
        result, _ = bake_segments(self.document, [MotionSegment(a, 0, 5), MotionSegment(b, 5, 10)], new_id, name='baked')
        self.assertEqual(len(result.slots), len(self.document.slots)+1)
        for old in self.document.slots:
            self.assertEqual(slot_snapshot(old), slot_snapshot(next(s for s in result.slots if s.motion_id == old.motion_id)))
        result, _ = bake_segments(result, [MotionSegment(new_id, 2, 8, 2)], new_id, replace_existing=True)
        m = next(s.payload.value for s in result.slots if s.motion_id == new_id)
        self.assertEqual((m.name, m.end_frame, m.raw_start_frame, m.raw_end_frame, m.looping), ('baked', 3, 0, 3, False))
        self.assertEqual(CODEC.write(CODEC.parse(result.source)), result.source)

    def test_shared_target_is_detached(self):
        _, a, b = self.ids
        raw = bytearray(self.document.source)
        table = struct.unpack_from('<Q', raw, 16)[0]
        source_index = next(i for i, s in enumerate(self.document.slots) if s.motion_id == a)
        target_index = next(i for i, s in enumerate(self.document.slots) if s.motion_id == b)
        struct.pack_into('<Q', raw, table + target_index * 8, struct.unpack_from('<Q', raw, table + source_index * 8)[0])
        aliased = CODEC.parse(bytes(raw))
        original = slot_snapshot(aliased.slots[source_index])
        result, _ = bake_segments(aliased, [MotionSegment(b, 0, 10, 2)], b, replace_existing=True)
        self.assertIsNot(result.slots[source_index].payload, result.slots[target_index].payload)
        self.assertEqual(slot_snapshot(result.slots[source_index]), original)
        self.assertEqual(result.slots[target_index].payload.value.end_frame, 5)

    def test_cli_dry_run_write_and_error_preserve_source(self):
        from tools.cli.main import main
        a, b, target = self.ids
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'baked.motlist.528'
            args = ['motion', 'bake', str(self.path), '--target', str(target), '--replace',
                    '--segment', str(a), '10', '30', '2', '--segment', str(b), '20', '40', '.5',
                    '-o', str(output), '--json']
            def invoke(argv, code):
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(main(argv), code)
                return json.loads(stdout.getvalue())
            dry = invoke([*args, '--dry-run'], 0)
            self.assertFalse(output.exists())
            written = invoke(args, 0)
            self.assertEqual(dry['details'], written['details'])
            self.assertEqual(written['details']['end_frame'], 51)
            saved = output.read_bytes()
            baked = next(s.payload.value for s in CODEC.parse(saved).slots if s.motion_id == target)
            root = next(n for n in baked.animation_nodes if n.joint.name == 'Root')
            for value in sample_track(root.translation, 0):
                self.assertAlmostEqual(value, 0, places=6)
            rotation = sample_track(root.rotation, 0)
            for value in rotation[:3]:
                self.assertAlmostEqual(value, 0, places=6)
            self.assertAlmostEqual(abs(rotation[3]), 1, places=6)
            for value in sample_track(root.scale, 0):
                self.assertAlmostEqual(value, 1, places=6)
            invalid = args.copy()
            invalid[invalid.index('--segment') + 2] = '-1'
            invoke(invalid, 2)
            self.assertEqual(output.read_bytes(), saved)
            self.assertEqual(self.path.read_bytes(), self.document.source)

    def test_editor_bake_updates_preview_and_normal_save(self):
        from tools.cli.runtime import qt_application
        from file_handlers.motion.motlist_handler import MotListHandler
        from file_handlers.motion.preview.mhr_editor import MhrMotListEditor
        from file_handlers.motion.preview.mhr_bake_dialog import MotionBakeDialog
        app = qt_application()
        handler = MotListHandler()
        handler.read(self.document.source)
        a, b, target = self.ids
        with patch.object(MhrMotListEditor, '_load_default_target'):
            editor = MhrMotListEditor(handler)
            try:
                app.processEvents()
                index = next(i for i, e in enumerate(editor.preview._motions) if e.motion_id == target)
                editor.preview.motion_browser.animation_list.setCurrentRow(index)
                dialog = MotionBakeDialog(editor.document, target, editor.bake_current_motion, editor)
                self.assertEqual(len(dialog.segments()), 2)
                self.assertEqual(dialog.segments()[0].root_transform, 'relative')
                dialog.table.cellWidget(0, 4).setCurrentIndex(1)
                self.assertEqual(dialog.segments()[0].root_transform, 'identity')
                dialog.table.cellWidget(1, 5).setValue(.3)
                self.assertEqual(dialog.segments()[1].root_translation_scale, .3)
                dialog.table.setCurrentCell(1, 0)
                dialog.remove_segment()
                self.assertEqual(len(dialog.segments()), 1)
                dialog.deleteLater()
                editor.bake_current_motion([MotionSegment(a, 10, 30, 2), MotionSegment(b, 20, 40, .5)])
                self.assertTrue(handler.modified)
                self.assertEqual(editor.preview.current_entry.motion_id, target)
                self.assertEqual(editor.preview.current_motion.end_frame, 51)
                reopened = CODEC.parse(handler.rebuild())
                self.assertEqual(next(s.payload.value.end_frame for s in reopened.slots if s.motion_id == target), 51)
                previous = editor.document
                with self.assertRaises(MotionWriteError):
                    editor.bake_current_motion([MotionSegment(a, -1, 30)])
                self.assertIs(editor.document, previous)
            finally:
                editor.cleanup()
                editor.deleteLater()
                app.processEvents()
