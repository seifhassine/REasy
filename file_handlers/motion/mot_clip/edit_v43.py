"""Structural CLIP 43 edits backed by the original native record bytes."""
from __future__ import annotations

from bisect import bisect_right
from copy import deepcopy
import math
import struct

from ..binary import align_up
from .model import (ASCII_VALUE_PROPERTY_TYPES, UTF16_VALUE_PROPERTY_TYPES,
                    CONTAINER_PROPERTY_TYPES, ClipPropertyType, HermiteCurve)
from .parser import CompactClipV27ParseResult


def _frame(value):
    if not math.isfinite(value):
        raise ValueError('CLIP frames must be finite')
    try:
        result = struct.unpack('<f', struct.pack('<f', value))[0]
    except OverflowError as exc:
        raise ValueError('CLIP frame exceeds float32 range') from exc
    if not math.isfinite(result):
        raise ValueError('CLIP frame exceeds float32 range')
    return result


class ClipGraph:
    """Indices select the original parsed tables; mutations retain source templates."""

    def __init__(self, raw: bytes, parsed: CompactClipV27ParseResult):
        if struct.unpack_from('<I', raw, parsed.clip_offset + 4)[0] != 43:
            raise ValueError('Structural graph editing requires CLIP 43')
        memo = {}
        copied = deepcopy(parsed, memo)
        self.clip = copied.clip
        self.nodes = [r.node for r in copied.nodes]
        self.properties = [r.prop for r in copied.properties]
        self._header = raw[parsed.clip_offset:parsed.clip_offset + 112]
        self._templates = {}
        self._values = {}
        for records, attr, size in ((parsed.nodes, 'node', 40),
                                    (parsed.properties, 'prop', 72),
                                    (parsed.keys, 'key', 32),
                                    (parsed.last_keys, 'key', 32),
                                    (parsed.speed_points, 'point', 24)):
            for r in records:
                self._templates[id(memo[id(getattr(r, attr))])] = bytes(raw[r.offset:r.offset + size])
        sections = parsed.section_absolute_offsets
        last_end = sections['last_keys'] + len(parsed.last_keys) * 32
        self._inactive_last_tail = bytes(raw[last_end:sections['ascii_strings']])
        for name, curves, size in (('hermite_curves', parsed.hermite_curves, 16),
                                    ('bezier3d_curves', parsed.bezier3d_curves, 32)):
            for i, curve in enumerate(curves):
                self._templates[id(memo[id(curve)])] = bytes(raw[sections[name]+i*size:sections[name]+(i+1)*size])
        record_by_key = {id(r.key): r for r in [*parsed.keys, *parsed.last_keys]}
        for r in parsed.properties:
            if r.prop.property_type == ClipPropertyType.PATH_POINT3D:
                for key in [*r.prop.keys, *([r.prop.last_key] if r.prop.last_key else [])]:
                    start = sections['owords'] + record_by_key[id(key)].payload * 16
                    self._values[id(memo[id(key)])] = bytes(raw[start:start + 16])

    @staticmethod
    def _select(items, index, label):
        if not isinstance(index, int) or index < 0 or index >= len(items):
            raise ValueError(f'{label} index {index!r} is out of range')
        return items[index]

    def _clone(self, donor, value):
        memo = {}
        result = deepcopy(value, memo)
        for source, target in memo.items():
            if source in donor._templates:
                self._templates[id(target)] = donor._templates[source]
            if source in donor._values:
                self._values[id(target)] = donor._values[source]
        return result

    @staticmethod
    def _shift_property(prop, shift):
        prop.start_frame = _frame(prop.start_frame + shift)
        if prop.end_frame >= 0:
            prop.end_frame = _frame(prop.end_frame + shift)
        for item in [*prop.keys, *prop.speed_points, *([prop.last_key] if prop.last_key else [])]:
            item.frame = _frame(item.frame + shift)
        for child in prop.children:
            ClipGraph._shift_property(child, shift)

    def copy_node(self, donor, node_index, parent_index=0, shift=0):
        _frame(shift)
        if node_index == 0:
            raise ValueError('The CLIP root cannot be copied as a child')
        source = self._select(donor.nodes, node_index, 'Node')
        parent = self._select(self.nodes, parent_index, 'Parent node')
        # Clone owner references and ranges in the same operation.
        descendants = set()
        def visit(node):
            descendants.add(id(node))
            for child in node.children:
                visit(child)
        visit(source)
        node, ranges = self._clone(donor, (source, [r for r in donor.clip.extra_ranges if id(r.owner) in descendants]))
        def shift_node(item):
            for prop in item.properties:
                self._shift_property(prop, shift)
            for child in item.children:
                shift_node(child)
        shift_node(node)
        for extra in ranges:
            for interval in extra.intervals:
                if interval.begin_frame is not None:
                    interval.begin_frame = _frame(interval.begin_frame + shift)
        parent.children.append(node)
        self.clip.extra_ranges.extend(ranges)

    def delete_node(self, node_index):
        if node_index == 0:
            raise ValueError('The CLIP root cannot be deleted')
        node = self._select(self.nodes, node_index, 'Node')
        removed = set()
        def visit(item):
            removed.add(id(item))
            for child in item.children:
                visit(child)
        visit(node)
        for parent in self.nodes:
            parent.children[:] = [item for item in parent.children if item is not node]
        self.clip.extra_ranges[:] = [r for r in self.clip.extra_ranges if id(r.owner) not in removed]

    def copy_property(self, donor, property_index, *, node_index=None, parent_property_index=None, shift=0):
        _frame(shift)
        if (node_index is None) == (parent_property_index is None):
            raise ValueError('Select exactly one destination node or container property')
        source = self._select(donor.properties, property_index, 'Property')
        if node_index is not None:
            destination = self._select(self.nodes, node_index, 'Node').properties
        else:
            parent = self._select(self.properties, parent_property_index, 'Parent property')
            if parent.property_type not in CONTAINER_PROPERTY_TYPES:
                raise ValueError('Destination property is not a container')
            destination = parent.children
        prop = self._clone(donor, source)
        self._shift_property(prop, shift)
        destination.append(prop)

    def delete_property(self, property_index):
        prop = self._select(self.properties, property_index, 'Property')
        for node in self.nodes:
            node.properties[:] = [item for item in node.properties if item is not prop]
        for parent in self.properties:
            parent.children[:] = [item for item in parent.children if item is not prop]

    def copy_key(self, donor, property_index, key_index, target_property_index, *, frame=None, shift=0):
        _frame(shift)
        source = self._select(donor.properties, property_index, 'Property')
        target = self._select(self.properties, target_property_index, 'Target property')
        if source.property_type in CONTAINER_PROPERTY_TYPES or source.property_type != target.property_type:
            raise ValueError('Key copy requires matching non-container property types')
        key = self._clone(donor, self._select(source.keys, key_index, 'Key'))
        key.frame = _frame((key.frame if frame is None else frame) + shift)
        target.keys.insert(bisect_right([k.frame for k in target.keys], key.frame), key)

    def delete_key(self, property_index, key_index):
        prop = self._select(self.properties, property_index, 'Property')
        self._select(prop.keys, key_index, 'Key')
        del prop.keys[key_index]

    def build(self, *, origin_offset: int, pointer_base: int) -> bytes:
        nodes = [self.clip.root]
        child_ranges = {}
        for node in nodes:
            child_ranges[id(node)] = len(nodes)
            nodes.extend(node.children)
        props = []
        prop_ranges = {}
        for node in nodes:
            prop_ranges[id(node)] = len(props)
            props.extend(node.properties)
        for prop in props:
            prop_ranges[id(prop)] = len(props)
            props.extend(prop.children)
        keys, lasts, speeds = [], [], []
        member_ranges, speed_ranges, last_indices = {}, {}, {}
        for prop in props:
            frames = [_frame(k.frame) for k in prop.keys]
            if frames != sorted(frames):
                raise ValueError(f'Property {prop.name!r} keys must have nondecreasing frames')
            member_ranges[id(prop)] = len(keys)
            keys.extend((k, prop.property_type) for k in prop.keys)
            speed_ranges[id(prop)] = len(speeds)
            speeds.extend(prop.speed_points)
            if prop.last_key is not None:
                last_indices[id(prop)] = len(lasts)
                lasts.append((prop.last_key, prop.property_type))
        ascii_pool, wide_pool, owords = bytearray(), bytearray(), bytearray()
        def string(value, wide=False):
            pool = wide_pool if wide else ascii_pool
            index = len(pool) // (2 if wide else 1)
            pool.extend(value.encode('utf-16le' if wide else 'ascii') + (b'\0\0' if wide else b'\0'))
            return index
        def template(item):
            return bytearray(self._templates[id(item)])
        node_bytes, prop_bytes = bytearray(), bytearray()
        for node in nodes:
            record = template(node)
            struct.pack_into('<HH', record, 0, len(node.children), len(node.properties))
            struct.pack_into('<QQQ', record, 16, string(node.name, True), child_ranges[id(node)], prop_ranges[id(node)])
            node_bytes.extend(record)
        for prop in props:
            record = template(prop)
            struct.pack_into('<ff', record, 0, _frame(prop.start_frame), _frame(prop.end_frame))
            struct.pack_into('<Q', record, 16, string(prop.name))
            container = prop.property_type in CONTAINER_PROPERTY_TYPES
            struct.pack_into('<QH', record, 32, prop_ranges[id(prop)] if container else member_ranges[id(prop)], len(prop.children) if container else len(prop.keys))
            struct.pack_into('<B', record, 44, len(prop.speed_points))
            record[47] = (record[47] & ~4) | (4 if prop.last_key is not None else 0)
            if prop.last_key is not None:
                struct.pack_into('<Q', record, 48, last_indices[id(prop)])
            struct.pack_into('<Q', record, 56, speed_ranges[id(prop)])
            prop_bytes.extend(record)
        hermite, bezier = bytearray(), bytearray()
        curve_indices = {}
        def curve_index(item):
            if item.curve is None:
                return 0
            curve = item.curve
            if id(curve) not in curve_indices:
                pool, size = (hermite, 16) if isinstance(curve, HermiteCurve) else (bezier, 32)
                curve_indices[id(curve)] = len(pool) // size
                pool.extend(self._templates[id(curve)])
            return curve_indices[id(curve)]
        def key_bytes(items):
            result = bytearray()
            for key, kind in items:
                record = template(key)
                struct.pack_into('<f', record, 0, _frame(key.frame))
                if kind in ASCII_VALUE_PROPERTY_TYPES:
                    struct.pack_into('<Q', record, 16, string(key.value))
                elif kind in UTF16_VALUE_PROPERTY_TYPES:
                    struct.pack_into('<Q', record, 16, string(key.value, True))
                elif kind == ClipPropertyType.PATH_POINT3D:
                    struct.pack_into('<Q', record, 16, len(owords) // 16)
                    owords.extend(self._values[id(key)])
                struct.pack_into('<Q', record, 24, curve_index(key))
                result.extend(record)
            return result
        key_data, last_data = key_bytes(keys), key_bytes(lasts)
        speed_data = bytearray()
        for speed in speeds:
            record = template(speed)
            struct.pack_into('<f', record, 0, _frame(speed.frame))
            struct.pack_into('<Q', record, 16, curve_index(speed))
            speed_data.extend(record)
        output = bytearray(self._header)
        struct.pack_into('<III', output, 12, len(nodes), len(props), len(keys))
        sections = []
        def append(data, alignment=1):
            output.extend(bytes(align_up(origin_offset + len(output), alignment) - origin_offset - len(output)))
            sections.append(origin_offset + len(output) - pointer_base)
            output.extend(data)
        for data in (node_bytes, prop_bytes, key_data, speed_data, hermite, bezier,
                     last_data + self._inactive_last_tail, ascii_pool):
            append(data)
        append(wide_pool, 8)
        append(owords, 8)
        append(b'', 16)
        node_indices = {id(node): i for i, node in enumerate(nodes)}
        self.clip.extra_ranges.sort(key=lambda r: node_indices[id(r.owner)])
        ranges = self.clip.extra_ranges
        table = origin_offset + len(output) + 16
        output.extend(struct.pack('<IIQ', len(ranges), 0, table - pointer_base))
        values_at = table + 16 * len(ranges)
        values = bytearray()
        from utils.hash_util import murmur3_hash
        for extra in ranges:
            track = node_indices[id(extra.owner)] - 1
            if track < 0:
                raise ValueError('CLIP root cannot own an extra range')
            output.extend(struct.pack('<IhhQ', murmur3_hash(extra.owner.name.encode('utf-16le')), track, len(extra.intervals), values_at + len(values) - pointer_base))
            for interval in extra.intervals:
                values.extend(b'\xff' * 4 if interval.begin_frame is None else struct.pack('<f', _frame(interval.begin_frame)))
                values.extend(struct.pack('<I', interval.frame_span))
        output.extend(values)
        output.extend(bytes(align_up(origin_offset + len(output), 16) - origin_offset - len(output)))
        struct.pack_into('<11Q', output, 24, *sections)
        return bytes(output)
