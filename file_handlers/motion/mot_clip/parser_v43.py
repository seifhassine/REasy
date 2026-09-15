from __future__ import annotations

from ..binary import align_up
from ..errors import MotionParseError
from .model import (ClipNode, ClipProperty, ClipPropertyType, ClipKey,
                    ClipInterpolation, CompactMotClip, HermiteCurve, Bezier3DCurve)
from .parser import (CompactClipV27Parser, CompactClipV27ParseResult,
                     CompactClipNodeRecord, CompactClipPropertyRecord, CompactClipKeyRecord,
                     CLIP_MAGIC)


class CompactClipV43Parser(CompactClipV27Parser):
    """v43 storage adapter; graph ownership and value types are shared with v27."""

    def __init__(self, profile):
        profile.require_versions(mot_clip=43)
        self.profile = profile

    def parse_result(self, c, clip_offset, following_data_offset=None, *, pointer_base=0, **_):
        c.require(clip_offset, 112, 'compact CLIP v43 header')
        if c.u32(clip_offset) != CLIP_MAGIC or c.u32(clip_offset+4) != 43:
            raise MotionParseError(f'{c.label}: expected compact CLIP v43')
        counts = [c.u32(clip_offset+12+i*4) for i in range(3)]
        names = ('nodes', 'properties', 'keys', 'speed_points', 'hermite_curves',
                 'bezier3d_curves', 'last_keys', 'ascii_strings', 'unicode_strings', 'owords', 'extra_ranges')
        raw = [c.u64(clip_offset+24+i*8) for i in range(11)]
        offsets = [pointer_base+v for v in raw]
        for offset in offsets:
            c.require(offset, 0, 'compact CLIP section')
        n, p, k, s, h, b, last, ascii_offset, wide, oword, extra = offsets
        nodes = self._read_nodes(c, n, counts[0], pointer_base, ascii_offset, wide)
        props = self._read_properties(c, p, counts[1], pointer_base, ascii_offset, wide)
        keys = self._read_keys(c, k, counts[2])
        speeds = self._read_speed_points(c, s, sum(r.speed_count for r in props))
        lasts = self._read_keys(c, last, sum(r.has_last_key for r in props))
        if b < h or (b-h) % 16 or last < b or (last-b) % 32:
            raise MotionParseError(f'{c.label}: invalid compact CLIP interpolation tables')
        hermite = [HermiteCurve(tuple(c.f32(i+j*4) for j in range(4))) for i in range(h, b, 16)]
        bezier = [Bezier3DCurve(tuple(c.f32(i+j*4) for j in range(8))) for i in range(b, last, 32)]
        self._attach_graph(c, nodes, props, keys, lasts, speeds)
        self._attach_curves(c, keys, lasts, speeds, hermite, bezier)
        self._decode_values(c, props, [*keys, *lasts], ascii_offset, wide, oword, frozenset())
        ranges, end = self._read_extra_ranges(c, extra, nodes, pointer_base)
        if not nodes:
            raise MotionParseError(f'{c.label}: compact CLIP has no root')
        if following_data_offset is not None and following_data_offset != align_up(end, 16):
            raise MotionParseError(f'{c.label}: compact CLIP end does not match its wrapper')
        return CompactClipV27ParseResult(
            CompactMotClip(c.f32(clip_offset+8), nodes[0].node, ranges), clip_offset,
            pointer_base, bytes(16), dict(zip(names, raw)), dict(zip(names, offsets)),
            nodes, props, keys, speeds, lasts, hermite, bezier, align_up(end, 16))

    def _read_nodes(self, c, offset, count, pointer_base, ascii_offset, unicode_offset):
        c.require(offset, count*40, 'compact CLIP v43 nodes')
        result = []
        for i in range(count):
            r = offset+i*40
            name_index = c.u64(r+16)
            name = c.utf16_z(unicode_offset+name_index*2)[0]
            result.append(CompactClipNodeRecord(r, i, ClipNode(name), c.u64(r+24), c.u16(r),
                                                c.u64(r+32), c.u16(r+2), 0, name_index))
        return result

    def _read_properties(self, c, offset, count, pointer_base, ascii_offset, unicode_offset):
        c.require(offset, count*72, 'compact CLIP v43 properties')
        result = []
        for i in range(count):
            r = offset+i*72
            name_index = c.u64(r+16)
            flags = c.u8(r+47)
            prop = ClipProperty(c.ascii_z(ascii_offset+name_index)[0], ClipPropertyType(c.u8(r+45)),
                c.f32(r), c.f32(r+4), c.i16(r+42), bool(flags&1), bool(flags&2), bool(flags&8),
                bool(flags&16), bool(flags&32), bool(flags&64), bool(flags&128))
            # v43 retains the previous index when the last-key flag is cleared.
            # The semantic graph only consumes active indexes; storage retains
            # the original field independently for byte-preserving writes.
            last_key_index = c.u64(r+48) if flags&4 else 0
            result.append(CompactClipPropertyRecord(r, i, prop, c.u64(r+32), c.u16(r+40),
                bool(flags&4), last_key_index, c.u64(r+56), c.u8(r+44), name_index, 0, flags))
        return result

    def _read_keys(self, c, offset, count):
        c.require(offset, count*32, 'compact CLIP v43 keys')
        result = []
        for i in range(count):
            r = offset+i*32
            flags = c.u32(r+8)
            key = ClipKey(c.f32(r), c.f32(r+4), ClipInterpolation(flags&255), bool(flags&256))
            result.append(CompactClipKeyRecord(r, i, key, c.u64(r+16), c.u64(r+24), flags))
        return result
