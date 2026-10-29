"""Native CLIP event edits on generated MOTLIST resources, without a game corpus."""
import struct
import unittest

from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as CODEC
from file_handlers.motion.mhr_events import set_bool_window, extend_property_range, set_effect, silence_events, move_event
from file_handlers.motion.mhr_generate import build_motion
from file_handlers.motion.mhr_storage import MhrMotion, MhrJoint
from file_handlers.motion.mot.model import Skeleton
from file_handlers.motion.mot_clip.edit_v43 import ClipGraph
from file_handlers.motion.mot_clip.model import ClipPropertyType
from file_handlers.motion.mhr_structure import Owner
from test_clip_graph_v43 import fixture as clip_fixture, parse


def document(kind, name, field, values, category, *, shared=False):
    raw = bytearray(clip_fixture())
    raw[parse(raw).properties[0].offset + 45] = int(kind)
    raw = bytes(raw); graph = ClipGraph(raw, parse(raw))
    graph.nodes[1].name = name
    graph.clip.extra_ranges = []
    prop = graph.properties[0]
    prop.name, prop.property_type = field, kind
    prop.last_key = None; prop.speed_points = []
    while len(prop.keys) < len(values):
        graph.copy_key(graph, 0, 1, 0, frame=len(prop.keys) * 2.)
    for index, (key, value) in enumerate(zip(prop.keys, values, strict=True)):
        key.frame, key.value, key.curve = float(index * 2), value, None
        key.interpolation = 1
    motion = MhrMotion('Fixture', end_frame=20, raw_start_frame=0, raw_end_frame=20,
                       skeleton=Skeleton([MhrJoint('Root')]), frames_per_second=60)
    payload = bytearray(build_motion(motion, translation_w=[0], joint_unknown=[0]))
    table = len(payload); wrapper = table + 16; clip = wrapper + 64
    data = graph.build(origin_offset=clip, pointer_base=0)
    tracks = clip + len(data)
    payload.extend(struct.pack('<Q', wrapper) + bytes(8))
    payload.extend(struct.pack('<3Q4I', 0, clip, tracks, 0, 1, 0, category) + bytes(24))
    payload.extend(data); payload.extend(bytes(32))
    struct.pack_into('<Q', payload, 48, table)
    payload[116] = 1
    struct.pack_into('<I', payload, 12, len(payload))
    count = 2 if shared else 1
    base = 80; rows = base + len(payload); label = rows + count * 72
    output = bytearray(base) + payload + bytearray(count * 72) + 'fixture\0'.encode('utf-16le')
    struct.pack_into('<I4sQ4QI', output, 0, 528, b'mlst', 0, 64, rows, label, 0, count)
    for i in range(count):
        struct.pack_into('<Q', output, 64 + i * 8, base)
        struct.pack_into('<H', output, rows + i * 72 + 8, i + 1)
        output[rows + i * 72 + 20] = 1
    result = CODEC.parse(bytes(output))
    if CODEC.write(result) != bytes(output):
        raise AssertionError('Synthetic MOTLIST baseline is not byte exact')
    return result


class EventEditingTests(unittest.TestCase):
    def property(self, raw):
        return Owner(CODEC.parse(raw), 1, 'motion').records[0].parsed.properties[0].prop

    def test_bool_window_and_property_extension_keep_values_and_size(self):
        source = document(ClipPropertyType.BOOL, 'FlagTrack', '_Flag', [False, True, True, False], 5)
        raw = set_bool_window(source, 1, node='FlagTrack', property_name='_Flag', on=2.25, off=8.5)
        prop = self.property(raw)
        self.assertEqual([(key.frame, key.value) for key in prop.keys], [(0., False), (2.25, True), (8.5, True), (9.5, False)])
        self.assertEqual(len(raw), len(source.source))
        stretched = extend_property_range(CODEC.parse(raw), 1, node='FlagTrack', end_frame=30.125)
        self.assertEqual(self.property(stretched).keys[-1].frame, 30.125)
        self.assertEqual(extend_property_range(CODEC.parse(stretched), 1, node='FlagTrack', end_frame=10), stretched)

    def test_sound_move_and_silence_keep_unselected_keys(self):
        source = document(ClipPropertyType.U32, 'snow.wwise.SoundBakeTriggerTracks', '_TriggerId', [123, 456], 1)
        moved = move_event(source, 1, trigger=123, to=5.5)
        self.assertEqual([(key.frame, key.value) for key in self.property(moved).keys], [(5.5, 123), (2., 456)])
        silenced = silence_events(source, 1, from_frame=1.)
        self.assertEqual([key.value for key in self.property(silenced).keys], [123, 0])
        self.assertEqual(len(silenced), len(source.source))

    def test_effect_replacement_can_match_an_existing_value_without_losing_other_keys(self):
        source = document(ClipPropertyType.STR8, 'VFXRangeTrack', 'EffectId', ['a', 'b'], 2)
        changed = set_effect(source, 1, old='a', new='b')
        self.assertEqual([key.value for key in self.property(changed).keys], ['b', 'b'])
        with self.assertRaisesRegex(ValueError, 'encoded byte length'):
            set_effect(source, 1, old='a', new='longer')

    def test_shared_payload_edit_fails_without_changing_source(self):
        source = document(ClipPropertyType.U32, 'snow.wwise.SoundBakeTriggerTracks', '_TriggerId', [123, 456], 1, shared=True)
        original = source.source
        with self.assertRaises(ValueError):
            move_event(source, 1, trigger=123, delta=1.)
        self.assertEqual(CODEC.write(source), original)


if __name__ == '__main__':
    unittest.main()
