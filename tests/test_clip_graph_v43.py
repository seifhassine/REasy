from dataclasses import asdict
import struct
import unittest

from file_handlers.motion.binary import ReadContext, align_up
from file_handlers.motion.mhr_codec import MHR_PROFILE
from file_handlers.motion.mot_clip.edit_v43 import ClipGraph
from file_handlers.motion.mot_clip.model import ClipPropertyType
from file_handlers.motion.mot_clip.parser_v43 import CompactClipV43Parser
from utils.hash_util import murmur3_hash


def fixture():
    nodes = bytearray(80)
    struct.pack_into('<HHIQQQ', nodes, 0, 1, 0, 0x1234, 0x55667788, 0, 1)
    struct.pack_into('<HHIQQQQ', nodes, 40, 0, 1, 0x9876, 0x12345678, 5, 0, 0)
    prop = bytearray(72)
    struct.pack_into('<ff', prop, 0, 0, 20)
    struct.pack_into('<QHhBBBBQQQ', prop, 32, 0, 2, -1, 1, ClipPropertyType.STR8, 0x5a, 4, 0, 0, 0xface)
    prop[8:16] = bytes.fromhex('1234567890abcdef')
    keys = struct.pack('<ffIIQQ', 2, 1, 5, 0xdead, 5, 0) + struct.pack('<ffIIQQ', 4, 2, 1, 0xbeef, 7, 0)
    speed = struct.pack('<ffIIQ', 3, 2, 5, 0, 0)
    curve = struct.pack('<4f', 1, 2, 3, 4)
    last = struct.pack('<ffIIQQ', 20, 1, 5, 0xcafe, 9, 0)
    data = bytearray(struct.pack('<4sI f III', b'CLIP', 43, 20, 2, 1, 2) + bytes(88))
    offsets = []
    for payload in (nodes, prop, keys, speed, curve, b'', last, b'Name\0a\0b\0z\0'):
        offsets.append(len(data))
        data.extend(payload)
    data.extend(bytes(align_up(len(data), 8)-len(data)))
    offsets.append(len(data))
    data.extend('Root\0Track\0'.encode('utf-16le'))
    data.extend(bytes(align_up(len(data), 8)-len(data)))
    offsets.append(len(data))
    data.extend(bytes(align_up(len(data), 16)-len(data)))
    offsets.append(len(data))
    extra = len(data)
    data.extend(struct.pack('<IIQ', 1, 0, extra+16))
    data.extend(struct.pack('<IhhQ', murmur3_hash('Track'.encode('utf-16le')), 0, 1, extra+32))
    data.extend(struct.pack('<fI', 2, 8))
    data.extend(bytes(align_up(len(data), 16)-len(data)))
    struct.pack_into('<11Q', data, 24, *offsets)
    return bytes(data)


def parse(data, origin=0, base=0):
    return CompactClipV43Parser(MHR_PROFILE).parse_result(ReadContext.from_bytes(data), origin, pointer_base=base)


def container_fixture():
    original = fixture()
    parsed = parse(original)
    start = parsed.properties[0].offset
    parent = bytearray(original[start:start + 72])
    struct.pack_into('<QH', parent, 32, 1, 1)
    parent[44], parent[45], parent[47] = 0, int(ClipPropertyType.CLASS), 0
    raw = bytearray(original[:start]) + parent + original[start:]
    struct.pack_into('<I', raw, 16, 2)
    offsets = list(parsed.section_absolute_offsets.values())
    struct.pack_into('<11Q', raw, 24, *(offset + (72 if i >= 2 else 0) for i, offset in enumerate(offsets)))
    extra = offsets[-1] + 72
    struct.pack_into('<Q', raw, extra + 8, extra + 16)
    struct.pack_into('<Q', raw, extra + 16 + 8, extra + 32)
    raw.extend(bytes(-len(raw) % 16))
    return bytes(raw)


class ClipGraphTests(unittest.TestCase):
    def graph(self):
        raw = fixture()
        return ClipGraph(raw, parse(raw))

    def reopen(self, graph):
        data = bytes(128) + graph.build(origin_offset=128, pointer_base=64)
        parsed = parse(data, 128, 64)
        self.assertEqual(asdict(parsed.clip), asdict(graph.clip))
        return data, parsed

    def test_noop_relocation_preserves_unknown_records(self):
        graph = self.graph()
        raw, parsed = self.reopen(graph)
        self.assertEqual(raw[parsed.properties[0].offset+46], 0x5a)
        self.assertEqual(struct.unpack_from('<Q', raw, parsed.properties[0].offset+64)[0], 0xface)
        self.assertEqual(struct.unpack_from('<I', raw, parsed.keys[0].offset+12)[0], 0xdead)

    def test_node_copy_owns_curves_strings_speeds_last_and_ranges(self):
        graph, donor = self.graph(), self.graph()
        graph.copy_node(donor, 1, shift=5)
        self.assertIsNot(graph.clip.root.children[-1], donor.clip.root.children[0])
        _, parsed = self.reopen(graph)
        self.assertEqual(len(parsed.nodes), 3)
        self.assertEqual(len(parsed.last_keys), 2)
        self.assertEqual(parsed.clip.extra_ranges[-1].intervals[0].begin_frame, 7)
        self.assertEqual(parsed.properties[-1].prop.speed_points[0].frame, 8)

    def test_node_delete_removes_all_dependents(self):
        graph = self.graph()
        graph.delete_node(1)
        _, parsed = self.reopen(graph)
        self.assertEqual(len(parsed.nodes), 1)
        self.assertEqual(parsed.properties, [])
        self.assertEqual(parsed.keys, [])
        self.assertEqual(parsed.last_keys, [])
        self.assertEqual(parsed.speed_points, [])
        self.assertEqual(parsed.clip.extra_ranges, [])

    def test_property_and_key_edits(self):
        graph, donor = self.graph(), self.graph()
        graph.copy_property(donor, 0, node_index=1, shift=1)
        self.reopen(graph)
        graph = self.graph()
        graph.delete_property(0)
        self.assertEqual(self.reopen(graph)[1].properties, [])
        graph = self.graph()
        graph.copy_key(donor, 0, 0, 0, frame=4)
        _, parsed = self.reopen(graph)
        self.assertEqual([k.value for k in parsed.properties[0].prop.keys], ['a', 'b', 'a'])
        graph = self.graph()
        graph.delete_key(0, 0)
        self.assertEqual(len(self.reopen(graph)[1].keys), 1)
        with self.assertRaises(ValueError):
            graph.copy_key(donor, 0, 0, 0, frame=float('inf'))

    def test_inactive_last_index_retains_native_value(self):
        raw = bytearray(fixture())
        parsed = parse(raw)
        # Keep the inactive stored key bytes in the unconsumed gap before strings.
        raw[parsed.properties[0].offset+47] &= ~4
        struct.pack_into('<Q', raw, parsed.properties[0].offset+48, 0xabc)
        graph = ClipGraph(raw, parse(raw))
        out, reopened = self.reopen(graph)
        self.assertEqual(struct.unpack_from('<Q', out, reopened.properties[0].offset+48)[0], 0xabc)
        self.assertEqual(out[reopened.section_absolute_offsets['last_keys']:reopened.section_absolute_offsets['ascii_strings']],
                         raw[parsed.section_absolute_offsets['last_keys']:parsed.section_absolute_offsets['ascii_strings']])

    def test_nested_nodes_and_container_properties_remap_ownership(self):
        graph, donor = self.graph(), self.graph()
        graph.copy_node(donor, 1, parent_index=1)
        raw, parsed = self.reopen(graph)
        self.assertEqual(len(parsed.clip.root.children[0].children), 1)
        graph = ClipGraph(raw, parsed)
        graph.delete_node(2)
        self.assertEqual(self.reopen(graph)[1].clip.root.children[0].children, [])

        raw = container_fixture()
        parsed = parse(raw)
        donor = ClipGraph(raw, parsed)
        graph = ClipGraph(raw, parsed)
        graph.copy_property(donor, 1, parent_property_index=0)
        _, reopened = self.reopen(graph)
        self.assertEqual(len(reopened.properties[0].prop.children), 2)
        self.assertEqual(len(reopened.keys), 4)
        from tools.cli.formats.motion import node_record
        tree = node_record(reopened.clip.root, reopened)
        children = tree['children'][0]['properties'][0]['children']
        self.assertEqual([p['property_index'] for p in children], [1, 2])
        graph = ClipGraph(raw, parsed)
        graph.copy_property(donor, 0, node_index=1)
        self.assertEqual(len(self.reopen(graph)[1].properties), 4)
        graph = ClipGraph(raw, parsed)
        graph.delete_property(0)
        self.assertEqual(self.reopen(graph)[1].properties, [])


if __name__ == '__main__':
    unittest.main()
