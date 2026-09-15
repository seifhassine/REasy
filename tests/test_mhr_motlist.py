from __future__ import annotations

import math
from dataclasses import asdict
from pathlib import Path
import struct
import unittest

import numpy as np

from file_handlers.motion.binary import ReadContext
from file_handlers.motion.errors import MotionParseError, MotionWriteError
from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as CODEC
from file_handlers.motion.mhr_tracks import decode_values, decode_track, encode_track
from file_handlers.motion.mot.model import TrackFamily, KeyTrack
from file_handlers.motion.motlist_handler import MotListHandler
from file_handlers.motion.evaluation import rig_from_motion_skeleton
from file_handlers.motion.evaluation.mhr import MHR_EVALUATION_PROFILE
from file_handlers.motion.preview.controller import MotionPreviewController
from file_handlers.motion.mhr_editing import duplicate_slot, next_motion_id
from file_handlers.motion.preview.clip_timeline import sequence_lanes


CORPUS = Path(__file__).parent / 'TESTFILE' / 'natives' / 'STM' / 'player' / 'mot'


def sequences_value(sequences):
    # ClipNode intentionally uses object identity inside a document; compare
    # every serialized semantic value across independently parsed documents.
    return [asdict(sequence) for sequence in sequences]


def contains_motion_tree(data):
    pointers = struct.unpack_from('<Q', data, 16)[0]
    count = struct.unpack_from('<I', data, 48)[0]
    offsets = struct.unpack_from(f'<{count}Q', data, pointers)
    return any(offset and data[offset+4:offset+8] == b'mtre' for offset in offsets)


def find_corpus_slot(predicate):
    for path in sorted(CORPUS.glob('*.motlist.528'), key=lambda path: path.stat().st_size):
        source = path.read_bytes()
        if contains_motion_tree(source):
            continue
        model = CODEC.parse(source, label=path.name)
        pointers = struct.unpack_from('<Q', source, 16)[0]
        shared = {start for start, _, is_shared in model.motion_spans if is_shared}
        for index, slot in enumerate(model.slots):
            if slot.payload is None:
                continue
            is_shared = struct.unpack_from('<Q', source, pointers+index*8)[0] in shared
            if predicate(slot, is_shared):
                return model, index
    raise unittest.SkipTest('The local corpus has no motion exercising this structural case')


class MhrCompressionTests(unittest.TestCase):
    def test_same_frame_keys_preserve_order_and_sample_the_last_authored_value(self):
        from file_handlers.motion.evaluation.sampling import sample_track
        track = KeyTrack(TrackFamily.VECTOR3, [0, 5, 5, 10],
                         [(0., 0., 0.), (1., 2., 3.), (4., 5., 6.), (7., 8., 9.)])
        flags, frames, values = encode_track(track)
        data = struct.pack('<5I', flags, len(track.frames), 20, 20+len(frames), 0)+frames+values
        parsed = decode_track(ReadContext.from_bytes(data), 0, 0, track.family)
        self.assertEqual(parsed, track)
        self.assertEqual(sample_track(parsed, 5), track.values[2])
        self.assertEqual(sample_track(parsed, 7.5), (5.5, 6.5, 7.5))

    def test_inactive_last_key_index_does_not_create_a_semantic_key(self):
        from file_handlers.motion.mot_clip.parser_v43 import CompactClipV43Parser
        from file_handlers.motion.mot_clip.model import ClipPropertyType
        data = bytearray(80)
        data[45] = ClipPropertyType.S32
        struct.pack_into('<Q', data, 48, 7)
        data[72:77] = b'Test\0'
        parser = CompactClipV43Parser(CODEC.profile)
        inactive = parser._read_properties(ReadContext.from_bytes(data), 0, 1, 0, 72, 0)[0]
        self.assertFalse(inactive.has_last_key)
        self.assertEqual(inactive.last_key_index, 0)
        self.assertEqual(struct.unpack_from('<Q', data, 48)[0], 7)
        data[47] = 4
        active = parser._read_properties(ReadContext.from_bytes(data), 0, 1, 0, 72, 0)[0]
        self.assertTrue(active.has_last_key)
        self.assertEqual(active.last_key_index, 7)

    def test_uniform_weapon_scale_tracks(self):
        data = struct.pack('<5I', 0x2440F2, 2, 20, 24, 0)+bytes((0, 10, 0, 0))+struct.pack('<2f', .5, 1.25)
        track = decode_track(ReadContext.from_bytes(data), 0, 0, TrackFamily.VECTOR3)
        self.assertEqual(track.frames, [0, 10])
        self.assertEqual(track.values, [(.5, .5, .5), (1.25, 1.25, 1.25)])
        data = bytes(16)+struct.pack('<4f', 2, .5, 7, 9)+struct.pack('<2H', 0, 65535)
        self.assertEqual(decode_values(ReadContext.from_bytes(data), 32, 2, TrackFamily.VECTOR3, 0x24, 16),
                         [(.5, .5, .5), (2.5, 2.5, 2.5)])

    def test_packed_component_width_and_byte_order(self):
        # Independent known codewords exercise the formats whose 010 template
        # comments differ from the native controller layouts in RevilLib.
        for mode, width, bits, order in ((0x20, 2, 5, 'little'), (0x30, 3, 8, 'little'),
                (0x40, 4, 10, 'little'), (0x50, 5, 13, 'big'), (0x60, 6, 16, 'little'),
                (0x70, 7, 18, 'big'), (0x80, 8, 21, 'little')):
            with self.subTest(mode=hex(mode)):
                mask = (1 << bits)-1
                codes = (mask//8, mask//4, mask//2)
                payload = sum(value << (i*bits) for i, value in enumerate(codes)).to_bytes(width, order)
                data = bytes(16)+struct.pack('<8f', .5, .5, .5, 0, -.1, -.2, -.3, 0)+payload
                result = decode_values(ReadContext.from_bytes(data), 48, 1, TrackFamily.QUATERNION, mode, 16)[0]
                xyz = [codes[i]/mask*.5+(-.1, -.2, -.3)[i] for i in range(3)]
                np.testing.assert_allclose(result, (*xyz, math.sqrt(1-sum(x*x for x in xyz))), atol=2e-8)

    def test_translation_bounds_are_six_consecutive_floats(self):
        codes = (17, 5, 26)
        data = bytes(16)+struct.pack('<8f', 2, 3, 4, -1, -2, -3, 0, 0)
        data += sum(v << (i*5) for i, v in enumerate(codes)).to_bytes(2, 'little')
        values = decode_values(ReadContext.from_bytes(data), 48, 1, TrackFamily.VECTOR3, 0x20, 16)
        np.testing.assert_allclose(values[0], [codes[i]/31*(i+2)-(i+1) for i in range(3)])

    def test_axis_rotation_bounds_and_uncompressed_signed_w(self):
        data = bytes(16)+struct.pack('<2f', .4, -.2)+struct.pack('<H', 65535)
        result = decode_values(ReadContext.from_bytes(data), 24, 1, TrackFamily.QUATERNION, 0x22, 16)[0]
        np.testing.assert_allclose(result, (0, .2, 0, math.sqrt(.96)), atol=2e-8)
        track = KeyTrack(TrackFamily.QUATERNION, [0, 300], [(0., 0., 0., -1.), (.5, .5, .5, -.5)])
        flags, frames, values = encode_track(track)
        self.assertEqual(flags, 0x400112)
        self.assertEqual(struct.unpack('<2H', frames), (0, 300))
        decoded = decode_values(ReadContext.from_bytes(values), 0, 2, TrackFamily.QUATERNION, 0, 0)
        self.assertEqual(decoded, track.values)

    def test_invalid_tracks_are_rejected(self):
        with self.assertRaises(MotionParseError):
            decode_values(ReadContext.from_bytes(bytes(32)), 0, 1, TrackFamily.QUATERNION, 0xFF, 0)
        with self.assertRaises(MotionWriteError):
            encode_track(KeyTrack(TrackFamily.VECTOR3, [2, 1], [(0., 0., 0.)]*2))


@unittest.skipUnless(CORPUS.is_dir(), 'local MHR corpus is not installed')
class MhrCorpusTests(unittest.TestCase):
    def test_editor_duplicate_save_edit_and_duplicate_again(self):
        from PySide6.QtWidgets import QApplication
        from file_handlers.motion.preview.mhr_editor import MhrMotListEditor
        app = QApplication.instance() or QApplication(['motion-editor-test', '-platform', 'offscreen'])
        model, slot_index = find_corpus_slot(lambda slot, shared:
            any(lane.prop.keys for lane in sequence_lanes(slot.payload.value.sequences)))
        handler = MotListHandler()
        handler.read(model.source)
        editor = MhrMotListEditor(handler)
        try:
            index = next(i for i, entry in enumerate(editor.preview._motions) if entry.slot_index == slot_index)
            editor.preview.motion_browser.animation_list.setCurrentRow(index)
            editor.preview.motion_browser.filter_edit.setText(editor.preview.current_motion.name)
            first_id = next_motion_id(editor.document)
            editor.duplicate_current_motion(first_id, editor.preview.current_motion.name+'_copy')
            self.assertTrue(handler.modified)
            self.assertEqual(editor.preview.current_entry.motion_id, first_id)
            self.assertEqual(editor.preview.motion_browser.filter_edit.text(), '')
            first_output = handler.rebuild()
            lane = next(lane for lane in editor.timeline.all_lanes if lane.prop.keys)
            editor._inspect_event(lane, None)
            fields = editor.timeline_inspector.model()
            parent = fields.index(0, 0)
            name_index = next(fields.index(row, 1, parent) for row in range(fields.rowCount(parent))
                              if fields.index(row, 1, parent).internalPointer().attribute == 'name'
                              and fields.index(row, 1, parent).internalPointer().editable)
            self.assertTrue(fields.setData(name_index, lane.prop.name+'_edited'))
            second_output = handler.rebuild()
            self.assertNotEqual(first_output, second_output)
            previous = editor.document
            with self.assertRaises(ValueError):
                editor.duplicate_current_motion(first_id, 'duplicate_id')
            self.assertIs(editor.document, previous)
            second_id = next_motion_id(editor.document)
            editor.duplicate_current_motion(second_id, editor.preview.current_motion.name+'_copy')
            self.assertEqual(editor.preview.current_entry.motion_id, second_id)
            reopened = CODEC.parse(handler.rebuild(), label='editor copies')
            self.assertEqual(len(reopened.slots), len(model.slots)+2)
            self.assertEqual(sequences_value(reopened.slots[-1].payload.value.sequences),
                             sequences_value(reopened.slots[-2].payload.value.sequences))
            self.assertEqual(sequences_value(reopened.slots[slot_index].payload.value.sequences),
                             sequences_value(model.slots[slot_index].payload.value.sequences))
        finally:
            editor.close()
            app.processEvents()

    def test_duplicate_slot_has_independent_motion_skeleton_and_clip(self):
        model, index = find_corpus_slot(lambda slot, shared: shared and
            any(lane.prop.keys for lane in sequence_lanes(slot.payload.value.sequences)))
        source = model.source
        motion_id = next_motion_id(model)
        result = duplicate_slot(model, index, motion_id, name=model.slots[index].payload.value.name+'_copy')
        self.assertEqual(len(result.slots), len(model.slots)+1)
        self.assertEqual(result.slots[-1].motion_id, motion_id)
        self.assertIsNot(result.slots[-1].payload, result.slots[index].payload)
        for old, new in zip(model.motion_spans, result.motion_spans):
            self.assertEqual(source[old[0]:old[1]], result.source[new[0]:new[1]])
        original, copied = model.slots[index].payload.value, result.slots[-1].payload.value
        self.assertEqual(sequences_value(original.sequences), sequences_value(copied.sequences))
        for before, after in zip(original.animation_nodes, copied.animation_nodes, strict=True):
            self.assertEqual(before.joint.name, after.joint.name)
            for attr in ('translation', 'rotation', 'scale'):
                self.assertEqual(getattr(before, attr), getattr(after, attr))
        # Any editable field owned by the copied CLIP is independent of its source.
        lane = next(lane for lane in sequence_lanes(copied.sequences) if lane.prop.keys)
        lane.prop.keys[0].frame += 1
        reopened = CODEC.parse(CODEC.write(result), label='copied CLIP edit')
        self.assertEqual(sequences_value(original.sequences), sequences_value(reopened.slots[index].payload.value.sequences))
        self.assertEqual(sequences_value(copied.sequences), sequences_value(reopened.slots[-1].payload.value.sequences))
        self.assertEqual(CODEC.write(reopened), reopened.source)
        another = duplicate_slot(reopened, len(reopened.slots)-1, next_motion_id(reopened))
        self.assertEqual(len(another.slots), len(model.slots)+2)
        self.assertEqual(sequences_value(another.slots[-1].payload.value.sequences), sequences_value(copied.sequences))
        self.assertIsNot(another.slots[-1].payload, another.slots[-2].payload)
        with self.assertRaises(MotionWriteError):
            duplicate_slot(model, index, model.slots[index].motion_id)

    def test_duplicate_slot_copies_overrides(self):
        model, index = find_corpus_slot(lambda slot, shared: bool(slot.overrides) and not shared)
        result = duplicate_slot(model, index, next_motion_id(model))
        self.assertEqual(sequences_value(model.slots[index].overrides), sequences_value(result.slots[-1].overrides))
        self.assertIsNot(result.slots[index].overrides[0].clip, result.slots[-1].overrides[0].clip)
        self.assertEqual(CODEC.write(result), result.source)

    def test_all_files_roundtrip_and_track_edits_preserve_sequences(self):
        paths = sorted(CORPUS.glob('*.motlist.528'))
        self.assertTrue(paths, 'No local MOTLIST samples were found')
        for path in paths:
            with self.subTest(file=path.name):
                source = path.read_bytes()
                self.assertTrue(MotListHandler.can_handle(source))
                if contains_motion_tree(source):
                    with self.assertRaisesRegex(MotionParseError, 'Motion Tree .* is not supported'):
                        CODEC.parse(source, label=path.name)
                    continue
                model = CODEC.parse(source, label=path.name)
                self.assertEqual(CODEC.write(model), source)
                if not model.tracks:
                    self.assertTrue(model.base_motion_list_path)
                    continue
                chosen = {0, len(model.tracks)//2, len(model.tracks)-1}
                expected = {}
                for index in chosen:
                    track = model.tracks[index].track
                    values = list(track.values[0])
                    if track.family == TrackFamily.QUATERNION:
                        values = [0., 0., 0., -1.]
                    else:
                        values[0] += 1.25
                    track.values[0] = tuple(values)
                    expected[index] = values
                output = CODEC.write(model)
                reopened = CODEC.parse(output, label='edited '+path.name)
                self.assertEqual(CODEC.write(reopened), output)
                self.assertEqual(len(model.tracks), len(reopened.tracks))
                for index, (before, after) in enumerate(zip(model.tracks, reopened.tracks)):
                    self.assertEqual(before.track.frames, after.track.frames)
                    if index in chosen:
                        np.testing.assert_allclose(after.track.values, before.track.values, atol=6e-8, rtol=6e-8)
                    else:
                        self.assertEqual(before.track.values, after.track.values)
                # All authored fields survive relocations; only structural
                # addresses and lengths are allowed to move.
                before_fields = [(f.name, f.get()) for f in model.fields if f.editable]
                after_fields = [(f.name, f.get()) for f in reopened.fields if f.editable]
                self.assertEqual(before_fields, after_fields)
                self.assertEqual([sequences_value(s.overrides) for s in model.slots],
                                 [sequences_value(s.overrides) for s in reopened.slots])
                for old_slot, new_slot in zip(model.slots, reopened.slots):
                    if old_slot.payload:
                        self.assertEqual(sequences_value(old_slot.payload.value.sequences),
                                         sequences_value(new_slot.payload.value.sequences))
                self.assertEqual(path.read_bytes(), source)

    def test_string_growth_hashes_aliasing_and_edit_after_save(self):
        path = CORPUS / 'plw_GunLance_100.motlist.528'
        handler = MotListHandler()
        handler.read(path.read_bytes())
        model = handler.model
        model.name += '_extended_list'
        motion = model.slots[0].payload.value
        motion.name += '_extended_motion'
        motion.skeleton.joints[0].name += '_extended_bone'
        motion.sequences[0].clip.root.name += '_extended_clip'
        field = next(f for f in model.fields if f.name == 'Name' and f.format == 'ascii')
        field.set(field.get()+'_extended_property')
        # Edits inside multiple MOTs and the absolute-pointer override tail.
        override = next(s.overrides[0] for s in model.slots if s.overrides)
        override.clip.root.name += '_extended_override'
        model.tracks[0].track.values[0] = (1., 2., 3.)
        output = handler.rebuild()
        self.assertEqual(handler.rebuild(), output)
        rebuilt = handler.model
        self.assertEqual(rebuilt.name, model.name)
        self.assertEqual(rebuilt.slots[0].payload.value.name, motion.name)
        self.assertEqual(sequences_value(rebuilt.slots[0].payload.value.sequences), sequences_value(motion.sequences))
        self.assertEqual([sequences_value(s.overrides) for s in rebuilt.slots],
                         [sequences_value(s.overrides) for s in model.slots])
        for i, a in enumerate(model.slots):
            for j, b in enumerate(model.slots):
                self.assertEqual(a.payload is b.payload, rebuilt.slots[i].payload is rebuilt.slots[j].payload)
        rebuilt.tracks[0].track.values[0] = (4., 5., 6.)
        handler.rebuild()
        self.assertEqual(handler.model.tracks[0].track.values[0], (4., 5., 6.))

    def test_real_motion_sampling_and_authored_frame_rate(self):
        path = CORPUS / 'plw_DualBlades_100.motlist.528'
        model = CODEC.parse(path.read_bytes(), label=path.name)
        motion = model.slots[0].payload.value
        motion.frames_per_second = 30
        motion = CODEC.parse(CODEC.write(model), label='30 fps').slots[0].payload.value
        rig = rig_from_motion_skeleton(motion, scale=(1, 1, 1), joint_binding=MHR_EVALUATION_PROFILE.joint_binding)
        controller = MotionPreviewController(MHR_EVALUATION_PROFILE)
        self.assertTrue(controller.load(motion, rig), controller.error_message)
        self.assertEqual(controller.frames_per_second, 30)
        poses = [controller._evaluator.sample_frame(f, wrap_looping=False) for f in (0, 50, 100)]
        self.assertNotEqual(poses[0], poses[1])
        self.assertNotEqual(poses[1], poses[2])


if __name__ == '__main__':
    unittest.main()
