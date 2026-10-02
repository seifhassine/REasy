"""LMT-only multi-file segment selection, identity and native output."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import tempfile
import io
import json
import math
from contextlib import redirect_stdout, redirect_stderr

from file_handlers.motion.errors import MotionWriteError
from file_handlers.motion.lmt_codec import LmtDocument, LmtMotion, LMT_MOTION_FORMAT_CODEC as XX
from file_handlers.motion.mot_list.model import MotionSlot, MotionSlotType, EmbeddedPayload
from file_handlers.motion.mhr_bake import MotionSegment
from file_handlers.motion.wilds_bake import segment_timeline, bake_lmt_segments
from file_handlers.motion.mhr_editing import next_motion_id


def source(name):
    return LmtDocument(name, [MotionSlot(3, MotionSlotType.MOT, EmbeddedPayload(LmtMotion(name+'_003', end_frame=20)))])


class SourceTests(unittest.TestCase):
    def test_equal_motion_ids_in_different_files_do_not_merge_boundaries(self):
        a, b = source('a'), source('b')
        segments = [MotionSegment(3, 0, 5, source='a'), MotionSegment(3, 5, 10, source='b')]
        _, samples, ranges = segment_timeline(a, segments, sources={'a': a, 'b': b})
        self.assertEqual(len(samples), 12)
        self.assertEqual(ranges[1]['join'], 'adjacent')
        self.assertEqual(ranges[1]['source'], 'b')
        self.assertIs(samples[5][0], a.slots[0].payload.value)
        self.assertIs(samples[6][0], b.slots[0].payload.value)
        self.assertEqual(samples[5][1], samples[6][1])
        same = [segments[0], MotionSegment(3, 5, 10, source='a')]
        _, samples, ranges = segment_timeline(a, same, sources={'a': a})
        self.assertEqual(len(samples), 11)
        self.assertEqual(ranges[1]['join'], 'continuous')

    def test_missing_source_and_other_formats_are_rejected(self):
        with self.assertRaisesRegex(MotionWriteError, 'Unknown segment source'):
            segment_timeline(source('a'), [MotionSegment(3, 0, 5, source='missing')], sources={})
        with self.assertRaisesRegex(MotionWriteError, 'LMT source'):
            bake_lmt_segments(None, {'bad': object()}, [], None, None, 100)


class TransitionTests(unittest.TestCase):
    def test_appends_exact_frame_count_with_root_fixed_and_exact_target_pose(self):
        from file_handlers.motion.evaluation.model import Rig, RigJoint, Transform
        from file_handlers.motion.evaluation.composition import compose_evaluated_pose
        from file_handlers.motion.motion_bake_blend import append_pose_transition
        rig = Rig([RigJoint('Root'), RigJoint('Waist_00', parent_index=0)])
        root = Transform((5., 2., 7.), (0., math.sin(.2), 0., math.cos(.2)))
        start = compose_evaluated_pose(rig, 1, (root, Transform((0., 1., 0.))), (1., 1.))
        first = compose_evaluated_pose(rig, 0, (root, Transform((0., 0., 0.))), (1., 1.))
        end = Transform((2., 3., 4.), (math.sqrt(.5), 0., 0., math.sqrt(.5)), (2., 1., 1.))
        target = compose_evaluated_pose(rig, 0, (Transform((1000., 0., 0.)), end), (1., 1.))
        output = append_pose_transition([first, start], rig, target, 10)
        self.assertEqual(len(output), 12)
        self.assertEqual([pose.frame for pose in output], list(range(12)))
        self.assertIs(output[0], first)
        self.assertIs(output[1], start)
        for pose in output[2:]:
            self.assertEqual(pose.local_transforms[0], root)
        self.assertEqual(output[-1].local_transforms[1], end)
        midpoint = output[6].local_transforms[1]
        self.assertEqual(midpoint.translation, (1., 2., 2.))
        self.assertEqual(midpoint.scale, (1.5, 1., 1.))
        self.assertAlmostEqual(midpoint.rotation[0], math.sin(math.pi/8))
        self.assertAlmostEqual(midpoint.rotation[3], math.cos(math.pi/8))
        for invalid in (0, -1, 1.5, True):
            with self.assertRaises(MotionWriteError):
                append_pose_transition([start], rig, target, invalid)


class DialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication(['lmt-dialog-test', '-platform', 'offscreen'])

    def test_file_selection_reorder_and_destination_are_committed(self):
        from file_handlers.motion.preview.lmt_bake_dialog import LmtBakeDialog
        a, b = source('a'), source('b')
        target = SimpleNamespace(slots=[MotionSlot(42, MotionSlotType.MOT, EmbeddedPayload(LmtMotion('target')))])
        calls = []
        dialog = LmtBakeDialog(target, 42, 'a.lmt', a, lambda *args, **kw: calls.append((args, kw)))
        try:
            self.assertEqual(dialog.target_id.value(), next_motion_id(target))
            with patch('file_handlers.motion.preview.lmt_bake_dialog.load_lmt_source', return_value=('b.lmt', b)):
                dialog.add_sources(['b.lmt'])
            choice = dialog.table.cellWidget(1, 0)
            choice.setCurrentIndex(choice.findData('b.lmt'))
            for row, start, end, speed in ((0, 2, 6, 1), (1, 7, 12, 2)):
                for column, value in ((2, start), (3, end), (4, speed)):
                    dialog.table.cellWidget(row, column).setValue(value)
            before = dialog.segments()
            dialog.table.setCurrentCell(1, 0)
            dialog.move_segment(-1)
            self.assertEqual(dialog.segments(), list(reversed(before)))
            dialog.mode.setCurrentIndex(1)
            self.assertEqual(dialog.target_id.value(), 42)
            dialog.bake()
            self.assertEqual(len(calls), 1)
            args, kwargs = calls[0]
            self.assertEqual(args[1], list(reversed(before)))
            self.assertEqual(args[2], 42)
            self.assertTrue(kwargs['replace_existing'])
            self.assertEqual(set(args[0]), {'a.lmt', 'b.lmt'})
        finally:
            dialog.close()


class CliTests(unittest.TestCase):
    def args(self, options):
        from tools.cli.main import build_parser
        return build_parser().parse_args(['motion', 'bake', 'target.528', '--target', '630', '-o', 'out.528', *options])

    def test_aliases_of_one_file_share_source_identity(self):
        from tools.cli.formats.motion_bake import lmt_sources
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'source.lmt'
            path.write_bytes(b'LMT')
            args = self.args(['--lmt-source', 'base', str(path), '--lmt-source', 'same', str(path),
                              '--segment', 'base:3', '0', '5', '1', '--segment', 'same:3', '5', '10', '1'])
            with patch.object(XX, 'parse', return_value=source('a')) as parse:
                aliases, documents = lmt_sources(args)
            self.assertEqual(aliases['base'], aliases['same'])
            self.assertEqual(len(documents), 1)
            self.assertEqual(parse.call_count, 1)

    def test_next_id_is_append_only(self):
        from tools.cli.formats.motion_bake import run, target_id
        args = self.args(['--target', 'next', '--segment', '3', '0', 'end', '1'])
        self.assertEqual(target_id(args, source('a')), next_motion_id(source('a')))
        args.replace = True
        with self.assertRaisesRegex(ValueError, 'cannot be used with --replace'):
            run(args)

    def test_invalid_or_ambiguous_sources_fail_before_loading(self):
        from tools.cli.formats.motion_bake import lmt_sources
        for options, message in (
            (['--lmt-source', 'base', 'a.lmt', '--donor', 'b.lmt'], 'cannot be combined'),
            (['--lmt-source', 'base', 'a.lmt', '--lmt-source', 'base', 'b.lmt'], 'Duplicate'),
            (['--lmt-source', '1bad', 'a.lmt'], 'names must'),
            (['--lmt-source', 'other', 'a.lmt'], 'declared NAME:MOTION'),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                lmt_sources(self.args([*options, '--segment', 'base:3', '0', 'end', '1']))

    def test_output_cannot_overwrite_any_named_source(self):
        from tools.cli.main import build_parser
        from tools.cli.runtime import main
        with tempfile.TemporaryDirectory() as folder:
            target, first, second = (Path(folder)/name for name in ('target.528', 'a.lmt', 'b.lmt'))
            for path in (target, first, second):
                path.write_bytes(path.name.encode())
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                code = main(build_parser, ['motion', 'bake', str(target), '--target', '630',
                                           '--lmt-source', 'a', str(first), '--lmt-source', 'b', str(second),
                                           '--segment', 'a:3', '0', '5', '1', '-o', str(second), '--json'])
            self.assertNotEqual(code, 0)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report['status'], 'error')
            self.assertIn('Output must not replace', report['error'])
            self.assertEqual(second.read_bytes(), b'b.lmt')


ROOT = Path(__file__).parent/'TESTFILE'
TARGET = ROOT/'natives/STM/player/mot/plw_SlashAxe_100.motlist.528'


@unittest.skipUnless(TARGET.is_file() and (ROOT/'xx/slashaxe_hunterart.lmt').is_file(), 'native corpus absent')
class NativeBakeTests(unittest.TestCase):
    def test_cli_multifile_dry_run_reports_ranges_without_publishing(self):
        from tools.cli.main import build_parser
        from tools.cli.runtime import main
        from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as RISE
        from file_handlers.motion.preview.mhr_assets import find_rise_installation
        if not find_rise_installation():
            self.skipTest('Rise installation absent')
        before = TARGET.read_bytes()
        motion_id = next_motion_id(RISE.parse(before))
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)/'baked.motlist.528'
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = main(build_parser, ['motion', 'bake', str(TARGET), '--target', 'next',
                    '--lmt-source', 'base', str(ROOT/'xx/slashaxe.lmt'),
                    '--lmt-source', 'arts', str(ROOT/'xx/slashaxe_hunterart.lmt'),
                    '--segment', 'base:122', '32', '36', '1', '--segment', 'arts:1', '0', '4', '2',
                    '--hold-template', str(TARGET), '--blend-frames', '1', '-o', str(output), '--dry-run', '--json'])
            self.assertEqual(status, 0, stdout.getvalue()+stderr.getvalue())
            report = json.loads(stdout.getvalue())
            self.assertTrue(report['dry_run'])
            self.assertEqual(set(report['details']['lmt_sources']), {'base', 'arts'})
            self.assertEqual(report['details']['motion_id'], motion_id)
            self.assertEqual(report['details']['end_frame'], 7)
            self.assertEqual([r['output_start'] for r in report['details']['segments']], [0, 5])
            self.assertFalse(output.exists())
            self.assertEqual(TARGET.read_bytes(), before)

    def test_two_lmt_files_bake_with_weaponhold_and_stable_roundtrip(self):
        from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as RISE
        from file_handlers.motion.preview.mhr_assets import MhrPreviewAssets, preview_context, find_rise_installation
        from file_handlers.motion.motlist_handler import MotListHandler
        from file_handlers.motion.mhr_import import verify_native_contract
        from file_handlers.motion.lmt_events import sampled_weapon_hold_keys
        from file_handlers.motion.lmt_import import verify_hold_keys
        from file_handlers.motion.preview.mhr_attachments import weapon_hold_properties
        from file_handlers.motion.mhr_import import idle_template
        if not find_rise_installation():
            self.skipTest('Rise installation absent')
        sources = {}
        for name in ('slashaxe', 'slashaxe_hunterart'):
            path = ROOT/'xx'/f'{name}.lmt'
            sources[name] = XX.parse(path.read_bytes(), label=str(path))
        target = RISE.parse(TARGET.read_bytes())
        original = RISE.write(target)
        assets = MhrPreviewAssets(preview_context(MotListHandler()))
        _, rig = assets.mesh('player/mod/m/bone/m_shadow.mesh.2109148288')
        other_id = sources['slashaxe_hunterart'].slots[0].motion_id
        segments = [MotionSegment(122, 32, 36, source='slashaxe'),
                    MotionSegment(other_id, 0, 4, 2, source='slashaxe_hunterart')]
        motion_id = next_motion_id(target)
        result, report = bake_lmt_segments(target, sources, segments, rig, target, motion_id, blend_frames=1)
        raw = RISE.write(result)
        reopened = RISE.parse(raw)
        self.assertEqual(RISE.write(reopened), raw)
        actual = next(s.payload.value for s in reopened.slots if s.motion_id == motion_id)
        _, samples, ranges = segment_timeline(sources['slashaxe'], segments, sources=sources)
        self.assertEqual(actual.end_frame, len(samples)-1)
        self.assertEqual(len(reopened.slots), len(target.slots)+1)
        self.assertEqual(RISE.write(target), original)
        hold = target.slots[idle_template(target)].payload.value
        verify_hold_keys(actual, sampled_weapon_hold_keys(samples, weapon_hold_properties(hold)))
        self.assertLess(verify_native_contract(reopened, motion_id, actual, rig), 4e-3)
        self.assertLess(report['max_matrix_error'], 2e-5)
        self.assertEqual([r['source'] for r in ranges], ['slashaxe', 'slashaxe_hunterart'])


if __name__ == '__main__':
    unittest.main()
