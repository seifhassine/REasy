from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
import struct
import unittest

from file_handlers.motion.errors import MotionWriteError
from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as RISE
from file_handlers.motion.wilds_codec import WILDS_MOTION_FORMAT_CODEC as WILDS
from file_handlers.motion.mhr_editing import next_motion_id, duplicate_slot
from file_handlers.motion.mhr_import import (import_motion, verify_imported_motion, verify_native_contract,
                                             default_motion_name, idle_template, IK_GOAL_SOURCES, _hold_sequence)
from file_handlers.motion.motlist_handler import MotListHandler
from file_handlers.motion.preview.mhr_assets import MhrPreviewAssets, preview_context, find_rise_installation
from file_handlers.motion.preview.mhr_attachments import weapon_hold_properties
from tools.import_wilds_motion import motion_mapping


CORPUS = Path(__file__).parent/'TESTFILE'
RISE_PATH = CORPUS/'natives/STM/player/mot/plw_LongSword_100.motlist.528'
WILDS_PATH = CORPUS/'Weapon/Wp03/wp03_00/wp03_00.motlist.992'


def motion_value(motion):
    def track_value(track):
        return None if track is None else (track.family, tuple(track.frames), tuple(track.values))
    return (motion.name, motion.end_frame, motion.looping, motion.raw_start_frame, motion.raw_end_frame,
            motion.frames_per_second,
            tuple((j.name, j.parent.name if j.parent else None, j.translation, j.rotation) for j in motion.skeleton.joints),
            tuple((n.joint.name, n.weight, *(track_value(getattr(n, attr)) for attr in ('translation', 'rotation', 'scale')))
                  for n in motion.animation_nodes),
            [asdict(sequence) for sequence in motion.sequences])


class MappingTests(unittest.TestCase):
    def test_decimal_padded_and_hexadecimal_motion_ids(self):
        self.assertEqual(motion_mapping('001:113'), (1, 113))
        self.assertEqual(motion_mapping('0xCB:0x71'), (203, 113))
        self.assertEqual(motion_mapping('203'), (203, None))


@unittest.skipUnless(RISE_PATH.is_file() and WILDS_PATH.is_file() and find_rise_installation(), 'native motion corpus/Rise assets absent')
class ImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rise_bytes = RISE_PATH.read_bytes()
        cls.wilds_bytes = WILDS_PATH.read_bytes()
        cls.target = RISE.parse(cls.rise_bytes)
        cls.source = WILDS.parse(cls.wilds_bytes)
        cls.assets = MhrPreviewAssets(preview_context(MotListHandler()))
        _, cls.rig = cls.assets.mesh('player/mod/m/bone/m_shadow.mesh.2109148288')
        cls.motion = next(s.payload.value for s in cls.source.slots if s.motion_id == 203)
        cls.short_motion = min((s.payload.value for s in cls.source.slots if s.payload and s.payload.value.end_frame > 0),
                               key=lambda motion: motion.end_frame)

    def assert_others_unchanged(self, output, replaced=()):
        by_id = {slot.motion_id: slot for slot in output.slots}
        row_before = struct.unpack_from('<Q', self.rise_bytes, 24)[0]
        row_after = struct.unpack_from('<Q', output.source, 24)[0]
        self.assertEqual(output.name, self.target.name)
        self.assertEqual(output.base_motion_list_path, self.target.base_motion_list_path)
        for index, slot in enumerate(self.target.slots):
            if slot.motion_id in replaced:
                continue
            actual = by_id[slot.motion_id]
            self.assertEqual(self.rise_bytes[row_before+index*72+8:row_before+(index+1)*72],
                             output.source[row_after+index*72+8:row_after+(index+1)*72])
            self.assertEqual(slot.tag_hash, actual.tag_hash)
            self.assertEqual([asdict(s) for s in slot.overrides], [asdict(s) for s in actual.overrides])
            if slot.payload is None:
                self.assertIsNone(actual.payload)
            else:
                self.assertTrue(motion_value(slot.payload.value) == motion_value(actual.payload.value),
                                f'Unrelated MotionID {slot.motion_id} changed')
        self.assertEqual(RISE.write(self.target), self.rise_bytes)
        self.assertEqual(self.source.source, self.wilds_bytes)

    def test_append_bakes_every_frame_and_only_native_weapon_hold(self):
        motion_id = next_motion_id(self.target)
        result = import_motion(self.target, self.motion, self.rig, self.target, motion_id)
        reopened = RISE.parse(RISE.write(result))
        self.assertEqual(len(reopened.slots), len(self.target.slots)+1)
        self.assert_others_unchanged(reopened)
        imported = reopened.slots[-1].payload.value
        error = verify_imported_motion(self.motion, imported, self.rig)
        self.assertLess(error, 2e-5)
        self.assertEqual(imported.frames_per_second, self.motion.frames_per_second)
        self.assertEqual((imported.raw_start_frame, imported.raw_end_frame),
                         (self.motion.raw_start_frame, self.motion.raw_end_frame))
        self.assertEqual([node.name for seq in imported.sequences for node in seq.clip.root.children], ['snow.player.WeaponHold'])
        native = weapon_hold_properties(self.target.slots[idle_template(self.target)].payload.value)
        actual = weapon_hold_properties(imported)
        self.assertEqual(set(native), set(actual))
        for hand in native:
            self.assertEqual([k.value for k in actual[hand].keys], [k.value for k in native[hand].keys])
            self.assertEqual(actual[hand].keys[0].frame, 0)
            self.assertEqual(actual[hand].keys[-1].frame, imported.end_frame)
        # The produced document remains compatible with native editing/relocation.
        duplicated = duplicate_slot(reopened, len(reopened.slots)-1, next_motion_id(reopened), name='imported_copy')
        self.assertEqual(duplicated.slots[-1].payload.value.animation_nodes[0].translation.values,
                         imported.animation_nodes[0].translation.values)

    def test_replacement_detaches_a_shared_payload(self):
        groups = defaultdict(list)
        for slot in self.target.slots:
            if slot.payload:
                groups[id(slot.payload)].append(slot.motion_id)
        aliases = next(ids for ids in groups.values() if len(ids) > 1)
        target_id = aliases[0]
        result = import_motion(self.target, self.short_motion, self.rig, self.target, target_id, replace_existing=True)
        self.assertEqual(len(result.slots), len(self.target.slots))
        self.assert_others_unchanged(result, (target_id,))
        slots = {s.motion_id: s for s in result.slots}
        self.assertIsNot(slots[target_id].payload, slots[aliases[1]].payload)
        verify_imported_motion(self.short_motion, slots[target_id].payload.value, self.rig)

    def test_replacing_the_last_skeleton_anchor_reference_preserves_followers(self):
        spans = self.target.motion_spans
        anchor = next(start for (start, _, shared), following in zip(spans, spans[1:]) if not shared and following[2])
        pointers = struct.unpack_from('<Q', self.rise_bytes, 16)[0]
        aliases = [slot.motion_id for i, slot in enumerate(self.target.slots)
                   if struct.unpack_from('<Q', self.rise_bytes, pointers+i*8)[0] == anchor]
        result = self.target
        for motion_id in aliases:
            result = import_motion(result, self.short_motion, self.rig, self.target, motion_id, replace_existing=True)
        self.assertEqual(len(result.slots), len(self.target.slots))
        self.assert_others_unchanged(RISE.parse(RISE.write(result)), aliases)

    def test_loop_and_frame_rate_are_preserved(self):
        loops = [s.payload.value for s in self.source.slots if s.payload and s.payload.value.looping and s.payload.value.end_frame > 0]
        motion = min(loops, key=lambda motion: motion.end_frame)
        result = import_motion(self.target, motion, self.rig, self.target, next_motion_id(self.target))
        imported = result.slots[-1].payload.value
        self.assertTrue(imported.looping)
        self.assertEqual(imported.frames_per_second, motion.frames_per_second)
        verify_imported_motion(motion, imported, self.rig)

    def test_collision_and_missing_replace_target_fail_before_writing(self):
        existing = self.target.slots[0].motion_id
        with self.assertRaisesRegex(MotionWriteError, 'already exists'):
            import_motion(self.target, self.motion, self.rig, self.target, existing)
        with self.assertRaisesRegex(MotionWriteError, 'does not exist'):
            import_motion(self.target, self.motion, self.rig, self.target, next_motion_id(self.target), replace_existing=True)
        with self.assertRaisesRegex(MotionWriteError, 'unsigned 16-bit'):
            import_motion(self.target, self.motion, self.rig, self.target, 65536)

    def payload_layout(self, document, motion_id):
        pointers, rows = struct.unpack_from('<QQ', document.source, 16)
        index = [slot.motion_id for slot in document.slots].index(motion_id)
        base = struct.unpack_from('<Q', document.source, pointers+index*8)[0]
        return index, base, struct.unpack_from('<10Q', document.source, base+16),             struct.unpack_from('<I', document.source, base+12)[0]

    def test_import_follows_the_native_payload_layout(self):
        motion_id = next_motion_id(self.target)
        result = import_motion(self.target, self.short_motion, self.rig, self.target, motion_id)
        index, base, ptr, size = self.payload_layout(result, motion_id)
        self.assertEqual(ptr[1], 0x80)                     # animation block first, as in every native payload
        self.assertGreaterEqual(ptr[0], size)              # shares the file rig instead of embedding a copy
        self.assertGreaterEqual(ptr[9], size*9//10)        # name block last
        self.assertFalse(any(ptr[field] for field in (2, 3, 5, 6, 7)))
        self.assertEqual(size % 16, 0)
        self.assertEqual(result.slots[index].payload.value.name, f'{self.target.name}_{motion_id}')

    def test_import_does_not_inherit_the_template_private_slot_field(self):
        template_index = idle_template(self.target)
        template_id = self.target.slots[template_index].motion_id
        _, _, _, _ = self.payload_layout(self.target, template_id)
        pointers, rows = struct.unpack_from('<QQ', self.target.source, 16)
        inherited = struct.unpack_from('<I', self.target.source, rows+template_index*72+12)[0]
        self.assertNotEqual(inherited, 0, 'the 001_Loop template must expose the trap this guards against')
        motion_id = next_motion_id(self.target)
        result = import_motion(self.target, self.short_motion, self.rig, self.target, motion_id)
        index, _, _, _ = self.payload_layout(result, motion_id)
        pointers, rows = struct.unpack_from('<QQ', result.source, 16)
        self.assertEqual(struct.unpack_from('<I', result.source, rows+index*72+12)[0], 0)
        # duplicating must not reintroduce another motion private value either
        duplicated = duplicate_slot(result, index, next_motion_id(result), name='copy')
        pointers, rows = struct.unpack_from('<QQ', duplicated.source, 16)
        self.assertEqual(struct.unpack_from('<I', duplicated.source, rows+len(duplicated.slots)*72-72+12)[0], 0)

    def test_import_bakes_the_native_ik_goal_convention(self):
        motion_id = next_motion_id(self.target)
        result = import_motion(self.target, self.motion, self.rig, self.target, motion_id)
        imported = next(slot.payload.value for slot in result.slots if slot.motion_id == motion_id)
        worst = verify_native_contract(result, motion_id, imported, self.rig)
        self.assertLess(worst, 1e-4)
        # Goals are leaf control joints: baking them cannot move any deform bone.
        goal_names = set(IK_GOAL_SOURCES)
        for joint in self.rig.joints:
            parent = joint.parent_index
            while parent is not None:
                self.assertNotIn(self.rig.joints[parent].name, goal_names,
                                 f'{joint.name} is driven by IK goal {self.rig.joints[parent].name}')
                parent = self.rig.joints[parent].parent_index
        error = verify_imported_motion(self.motion, imported, self.rig)
        self.assertLess(error, 2e-5)

    def test_default_motion_name_follows_native_convention(self):
        self.assertEqual(default_motion_name(self.target, 777), f'{self.target.name}_777')

    def test_contract_rejects_a_dirty_slot_field_or_swapped_layout(self):
        motion_id = next_motion_id(self.target)
        result = import_motion(self.target, self.short_motion, self.rig, self.target, motion_id)
        imported = next(slot.payload.value for slot in result.slots if slot.motion_id == motion_id)
        pointers, rows = struct.unpack_from('<QQ', result.source, 16)
        index = [slot.motion_id for slot in result.slots].index(motion_id)
        dirty = bytearray(result.source)
        struct.pack_into('<I', dirty, rows+index*72+12, 0x1A)
        with self.assertRaisesRegex(MotionWriteError, r'slot \+0x0C'):
            verify_native_contract(RISE.parse(bytes(dirty)), motion_id, imported, self.rig)
        renamed = RISE.parse(bytes(result.source))
        next(slot.payload.value for slot in renamed.slots if slot.motion_id == motion_id).name = 'wp03_00_293'
        with self.assertRaisesRegex(MotionWriteError, 'convention'):
            verify_native_contract(renamed, motion_id,
                                   next(s.payload.value for s in renamed.slots if s.motion_id == motion_id), self.rig)

    def test_native_idle_hold_templates_export_for_each_weapon_family(self):
        from file_handlers.motion.binary import ReadContext
        from file_handlers.motion.mhr_codec import MhrParser
        from file_handlers.motion.mhr_storage import Group
        for path in sorted((CORPUS/'natives/STM/player/mot').glob('plw_*_100.motlist.528')):
            with self.subTest(path=path.name):
                document = RISE.parse(path.read_bytes())
                data = _hold_sequence(document, idle_template(document), 0, 197)
                parser = MhrParser(data, 'exported hold')
                sequence = parser.sequence(ReadContext.from_bytes(data), 0, 0, Group('hold'))
                self.assertEqual([n.name for n in sequence.clip.root.children], ['snow.player.WeaponHold'])


if __name__ == '__main__':
    unittest.main()
