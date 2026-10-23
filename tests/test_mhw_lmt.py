"""MHW x64 layouts, source binding and compression regressions."""
from pathlib import Path
import struct
import tempfile
import unittest

import numpy as np

from file_handlers.motion.binary import ReadContext
from file_handlers.motion.errors import MotionParseError, MotionWriteError
from file_handlers.motion.format_registry import require_motion_format
from file_handlers.motion.lmt_codec import LMT_MOTION_FORMAT_CODEC, LmtMotion
from file_handlers.motion.mhw_lmt_codec import (
    MHW_LMT_MOTION_FORMAT_CODEC as MHW, MhwLmtMotion,
    decode_mhw_track, read_mod3_skeleton,
)
from file_handlers.motion.evaluation.source_adapter import rig_from_motion_skeleton
from file_handlers.motion.evaluation.binding import bind_motion
from file_handlers.motion.evaluation.sampling import MotionEvaluator
from file_handlers.motion.preview.support_registry import MHW_LMT_MOTION_PREVIEW_SUPPORT
from file_handlers.motion.motlist_handler import MotListHandler
from file_handlers.motion.mhw_timeline import property_hash


def mod3(*, parents=(255, 0)):
    data = bytearray(64+2*152)
    struct.pack_into('<4sHH', data, 0, b'MOD\0', 237, 2)
    struct.pack_into('<Q', data, 48, 64)
    for i, (bone, parent) in enumerate(zip((254, 42), parents)):
        struct.pack_into('<HBB', data, 64+i*24, bone, parent, 255)
        matrix = np.eye(4, dtype=np.float32)
        matrix[3, :3] = (10, 0, 0) if i == 0 else (0, 20, 0)
        if i == 0:
            matrix[0, 0], matrix[1, 1], matrix[2, 2] = 2, 3, 4
        struct.pack_into('<16f', data, 112+i*64, *matrix.flat)
        struct.pack_into('<16f', data, 240+i*64, *np.eye(4).flat)
    return data


def lmt(*, bone=42):
    data = bytearray(416)
    struct.pack_into('<4sHH', data, 0, b'LMT\0', 95, 4)
    data[8:16] = bytes.fromhex('0008021800000000')
    struct.pack_into('<4Q', data, 16, 0, 64, 64, 0)
    struct.pack_into('<QIIi', data, 64, 160, 3, 30, 7)
    struct.pack_into('<4f', data, 96, 10, 0, 0, 0)
    struct.pack_into('<4f', data, 112, 0, 0, 0, 1)
    struct.pack_into('<I', data, 128, 0x10000001)
    struct.pack_into('<Q', data, 152, 368)
    # Byte 3 is allocator padding 0xCD, not the bone function ID.
    struct.pack_into('<4BifIQ4fQ', data, 160, 2, 0, 0, 205, bone, 1, 12, 304, 0, 0, 0, 1, 0)
    struct.pack_into('<3f', data, 304, .3, .4, 0)
    struct.pack_into('<4BifIQ4fQ', data, 208, 2, 3, 0, 205, -1, 1, 0, 0, 0, 0, 0, 1, 0)
    struct.pack_into('<4BifIQ4fQ', data, 256, 1, 4, 0, 205, -1, 1, 24, 320, 0, 0, 0, 0, 0)
    struct.pack_into('<6f', data, 320, 1, 2, 3, 4, 5, 6)
    struct.pack_into('<QQiiffiI', data, 368, 0, 0, 4, 6, 30, 7, 0, 0)
    return data


def storage_words(values, widths, word_bits):
    """Independent stream fixture: fields straddling words concatenate chunks."""
    raw, cursor = 0, 0
    for value, width in zip(values, widths):
        remaining = width
        while remaining:
            take = min(remaining, word_bits-cursor % word_bits)
            remaining -= take
            raw |= ((value >> remaining) & ((1 << take)-1)) << cursor
            cursor += take
    return raw.to_bytes(cursor//8, 'little')


class MhwTrackTests(unittest.TestCase):
    def decode(self, codec, raw, *, kind=0, bounds=None):
        data = bytearray(80)
        struct.pack_into('<4BifIQ4fQ', data, 0, codec, kind, 0, 205, 42,
                         1, len(raw), 80, 0, 0, 0, 1, 48 if bounds else 0)
        if bounds:
            struct.pack_into('<8f', data, 48, *bounds)
        data.extend(raw)
        return decode_mhw_track(ReadContext.from_bytes(data), 0, 30)[3]

    def test_rotated_word_quaternions_and_outgoing_delta(self):
        for codec, bits, word_bits in ((14, 11, 16), (15, 9, 8)):
            mask = (1 << bits)-1
            codes = (8, 130, 255, mask-7)
            raw = storage_words((*codes, 9), (bits, bits, bits, bits, 4), word_bits)
            raw += storage_words((8, 8, 8, mask-7, 0), (bits, bits, bits, bits, 4), word_bits)
            track = self.decode(codec, raw, bounds=(2, 3, 4, 5, .1, .2, .3, .4))
            self.assertEqual(track.frames, [0, 1, 10])
            expected = np.array(codes, dtype=float)
            expected = (expected-8)/(mask-15)*np.array((2, 3, 4, 5))+np.array((.1, .2, .3, .4))
            np.testing.assert_allclose(track.values[1], expected, atol=1e-7)

    def test_sparse_z_quaternion_preserves_inactive_axis_offsets(self):
        track = self.decode(13, (8 | ((16383-7) << 14)).to_bytes(4, 'little'),
                            bounds=(2, 3, 4, 5, .1, .2, .3, .4))
        np.testing.assert_allclose(track.values[1], (.1, .2, .3, 5.4), atol=1e-7)

    def test_vector_quantization_and_explicit_frame_delta(self):
        for codec, fmt, mask in ((4, '<4H', 65535), (5, '<4B', 255)):
            track = self.decode(codec, struct.pack(fmt, 8, mask-7, 8, 0), kind=1,
                                bounds=(2, 3, 4, 0, 10, 20, 30, 0))
            np.testing.assert_allclose(track.values[1], (10, 23, 30))
        track = self.decode(3, struct.pack('<3fI3fI', 1, 2, 3, 8, 4, 5, 6, 0), kind=1)
        self.assertEqual(track.frames, [0, 1, 9])
        self.assertEqual(track.values[2], (4, 5, 6))

    def test_unknown_codec_and_truncated_buffer_rejected(self):
        for codec, data in ((8, b''), (15, bytes(4)), (14, bytes(6))):
            with self.assertRaises(MotionParseError):
                self.decode(codec, data)


class MhwDocumentTests(unittest.TestCase):
    def test_embedded_timl_effect_work_number_and_activation_frames(self):
        data = lmt()
        data.extend(bytes(184))
        struct.pack_into('<QQiiffiI', data, 368, 416, 1, 4, 6, 30, 7, 0, 0)
        struct.pack_into('<QQIi', data, 416, 448, 2,
                         property_hash('nTimelineParam::EffectParameter2') & 0x7fffffff, 0)
        struct.pack_into('<QQIi', data, 448, 496, 1, property_hash('WorkNo00'), 1)
        struct.pack_into('<QQIi', data, 472, 528, 3, property_hash('KeyFrame00'), 4)
        struct.pack_into('<IIIfhh', data, 496, 5009, 0, 0, 0, 1, 1)
        for offset, value, frame in ((528, 0, 0), (548, 1, 20), (568, 0, 27)):
            struct.pack_into('<IIIfhh', data, offset, value, 0, 0, frame, 1, 4)
        doc = MHW.parse(data, skeleton_data=mod3())
        timeline = doc.slots[0].payload.value.timeline
        self.assertEqual(timeline.data_indices, (4, 6))
        call, = timeline.effect_calls
        self.assertEqual((call.type_name, call.slot), ('nTimelineParam::EffectParameter2', 0))
        self.assertEqual([(k.frame, k.value) for k in call.work_numbers], [(0, 5009)])
        self.assertEqual([(k.frame, k.value) for k in call.activation], [(0, 0), (20, 1), (27, 0)])
        self.assertEqual(doc.source, data)
        bad = bytearray(data)
        struct.pack_into('<Q', bad, 472, len(data))
        with self.assertRaises(MotionParseError):
            MHW.parse(bad, skeleton_data=mod3())

    def test_x64_aliases_vacancies_padding_and_scene_channels(self):
        data = lmt()
        self.assertIs(require_motion_format(data), MHW)
        self.assertFalse(LMT_MOTION_FORMAT_CODEC.matches(data))
        doc = MHW.parse(data, skeleton_data=mod3())
        self.assertEqual(doc.slot_count, 4)
        self.assertEqual([s.motion_id for s in doc.slots], [1, 2])
        self.assertIs(doc.slots[0].payload, doc.slots[1].payload)
        self.assertEqual(doc.source, data)
        motion = doc.slots[0].payload.value
        self.assertIsInstance(motion, MhwLmtMotion)
        self.assertNotIsInstance(motion, LmtMotion)
        self.assertEqual((motion.flags, motion.timl_offset, motion.loop_start_frame), (0x10000001, 368, 7))
        self.assertEqual(motion.source_tracks[0].bone_id, 42)
        self.assertEqual(motion.source_tracks[0].keys.frames, [0, 1])
        np.testing.assert_allclose(motion.source_tracks[0].keys.values[1], (.3, .4, 0, np.sqrt(.75)))
        root = next(n for n in motion.animation_nodes if n.joint.name == 'MHW_Root')
        self.assertEqual(root.translation.frames, [0, 1, 2])
        self.assertEqual(root.rotation.values, [(0, 0, 0, 1)])
        with self.assertRaises(MotionWriteError):
            MHW.write(doc)

    def test_mod3_function_ids_parents_and_authored_scale(self):
        skeleton = read_mod3_skeleton(mod3())
        root, a, b = skeleton.joints
        self.assertEqual([j.name for j in skeleton.joints], ['MHW_Root', 'MHW_254', 'MHW_42'])
        self.assertIs(a.parent, root)
        self.assertIs(b.parent, a)
        self.assertEqual(a.rest_scale, (2, 3, 4))
        motion = MHW.parse(lmt(), skeleton_data=mod3()).slots[0].payload.value
        profile = MHW_LMT_MOTION_PREVIEW_SUPPORT.evaluation
        rig = rig_from_motion_skeleton(motion, scale=profile.source_preview_scale, joint_binding=profile.joint_binding)
        self.assertEqual(rig.joints[1].rest.scale, (2, 3, 4))
        binding = bind_motion(motion, rig, profile.joint_binding)
        self.assertFalse(binding.has_errors)
        pose = MotionEvaluator(binding, profile.sampling_policy, profile.pose_composition_policy).sample_frame(1)
        self.assertEqual(pose.local_transforms[0].translation, (1, 2, 3))
        for parents in ((0, 0), (1, 0), (255, 2)):
            with self.assertRaises(MotionParseError):
                read_mod3_skeleton(mod3(parents=parents))

    def test_unbound_auxiliary_tracks_remain_parsed(self):
        motion = MHW.parse(lmt(bone=30), skeleton_data=mod3()).slots[0].payload.value
        self.assertEqual(len(motion.source_tracks), 3)
        self.assertEqual([t.bone_id for t in motion.unbound_tracks], [30])
        self.assertIsNone(motion.unbound_tracks[0].joint)
        self.assertEqual(len(motion.unbound_tracks[0].keys.values), 2)

    def test_extracted_directory_and_adjacent_mod3_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            variant = Path(tmp)/'em/em080/00'
            path = variant/'mot/em080_02/em080_02.lmt'
            path.parent.mkdir(parents=True)
            (variant/'mod').mkdir()
            model = variant/'mod/em080_00.mod3'
            model.write_bytes(mod3())
            path.write_bytes(lmt())
            self.assertEqual(Path(MHW.parse(lmt(), label=str(path)).skeleton_path), model)
            # Auxiliary IDs do not become MOD3 array-index bindings.
            self.assertTrue(MHW.parse(lmt(bone=30), label=str(path)).slots[0].payload.value.unbound_tracks)
            adjacent = path.with_suffix('.mod3')
            adjacent.write_bytes(mod3())
            self.assertEqual(Path(MHW.parse(lmt(), label=str(path)).skeleton_path), adjacent)

    def test_missing_skeleton_empty_list_and_invalid_pointers(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(MotionParseError, 'no compatible MOD3'):
                MHW.parse(lmt(), label=str(Path(tmp)/'source.lmt'))
        empty = bytearray(16)
        struct.pack_into('<4sHH', empty, 0, b'LMT\0', 95, 0)
        self.assertEqual(MHW.parse(empty).slots, [])
        for offset, value in ((16, 10000), (64, 10000), (152, 10000)):
            bad = lmt()
            struct.pack_into('<Q', bad, offset, value)
            with self.assertRaises(MotionParseError):
                MHW.parse(bad, skeleton_data=mod3())

    def test_handler_recognizes_read_only_mhw(self):
        from file_handlers.motion.motlist_file import MotListFile
        facade = MotListFile(MHW)
        facade.model = MHW.parse(lmt(), skeleton_data=mod3())
        handler = MotListHandler()
        handler.adopt_document(facade, lmt(), notify=False)
        self.assertFalse(handler.supports_editing())


if __name__ == '__main__':
    unittest.main()
