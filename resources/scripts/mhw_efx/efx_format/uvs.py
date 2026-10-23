"""MHW UV Sequence（``.uvs``）的解析与保真序列化。

维护约束：
- 所有偏移均为绝对偏移，所有数值按小端读写。
- 空组的偏移写法、帧索引与索引区间隙、字符串别名均须保留，以维持未编辑文件的字节保真。
"""

from __future__ import annotations
import struct
from dataclasses import dataclass, field
from typing import List


MAGIC   = b'UVS\x00'
IB_SIG  = bytes([0, 7, 18, 22])

_HDR_SZ  = 0x28
_HDR_PAD = 0x30   # header padded to 16-byte boundary
_GRP_SZ  = 0x40
_PRI_SZ  = 0x20
_MAP_SZ  = 0x10   # 4 × int32
_STR_HD  = 0x14   # StringHead without inter-entry padding
_STR_PAD = 0x04   # padding between StringHead entries (not after last)


# 数据模型

@dataclass
class UVSFrame:
    """UV 动画的一帧矩形区域。"""
    uv0: tuple
    uv1: tuple
    # 未命名帧字段须保留以支持保真序列化。
    _unkn: tuple = field(default=(0.5, 0.5, 0.0, 0.0), repr=False)


@dataclass
class UVSGroup:
    """一组 UV 帧与纹理路径引用。"""
    frames: List[UVSFrame]
    # 指向共享字符串表；输出时补齐为四项。
    path_indices: List[int]
    map_count: int
    dynamic: int
    unkn32_0: float = 32.0
    unkn32_1: float = 32.0
    # 帧索引必须原样保留。
    _frame_indices: List[int] = field(default_factory=list, repr=False)
    # 帧索引与 map 索引间的原始间隙；新组以 16 字节对齐生成。
    _fi_dat_gap: int = field(default=-1, repr=False)
    # 空组的零偏移写法须保留；新组默认写当前游标。
    _zero_empty_offsets: bool = field(default=False, repr=False)


@dataclass
class UVSString:
    """共享字符串表的一项。"""
    path: str
    type: int
    # 多个表项可共享同一字符串副本；别名关系须保留。
    _alias_of: int = field(default=-1, repr=False)


@dataclass
class UVSFile:
    """``.uvs`` 文件模型。"""
    groups:  List[UVSGroup]
    strings: List[UVSString]

    @classmethod
    def parse(cls, data: bytes) -> 'UVSFile':
        magic, ibsig = data[:4], data[4:8]
        if magic != MAGIC or ibsig != IB_SIG:
            raise ValueError(f'Not a UVS file (magic={magic!r} ibsig={ibsig!r})')

        grp_off, grp_cnt, str_off, str_cnt = struct.unpack_from('<qqqq', data, 8)

        groups  = _parse_groups(data, grp_off, grp_cnt)
        strings = _parse_strings(data, str_off, str_cnt)
        return cls(groups=groups, strings=strings)

    def serialize(self) -> bytes:
        return _serialize(self)


# 解析

def _parse_groups(data: bytes, grp_off: int, grp_cnt: int) -> List[UVSGroup]:
    groups = []
    pos = grp_off
    for _ in range(grp_cnt):
        (fd_off, fd_cnt, fi_off, fi_cnt,
         dat_off, map_cnt, u0, u1, unkn3) = struct.unpack_from('<qqqqqqffq', data, pos)
        pos += _GRP_SZ

        frames = _parse_frames(data, fd_off, fd_cnt)

        if fi_cnt > 0:
            fi = list(struct.unpack_from(f'<{fi_cnt}i', data, fi_off))
        else:
            fi = []

        if fd_cnt != 0:
            raw = list(struct.unpack_from('<4i', data, dat_off))
        else:
            raw = [0, 0, 0, 0]
        path_indices = raw[:map_cnt]

        fi_end = fi_off + fi_cnt * 4
        fi_dat_gap = dat_off - fi_end if fd_cnt != 0 else 0
        # 空组的零偏移写法须原样保留。
        zero_empty = (fd_cnt == 0 and fi_cnt == 0 and map_cnt == 0
                      and fd_off == 0 and fi_off == 0 and dat_off == 0)

        groups.append(UVSGroup(
            frames=frames,
            path_indices=path_indices,
            map_count=map_cnt,
            dynamic=unkn3,
            unkn32_0=u0,
            unkn32_1=u1,
            _frame_indices=fi,
            _fi_dat_gap=fi_dat_gap,
            _zero_empty_offsets=zero_empty,
        ))
    return groups


def _parse_frames(data: bytes, off: int, cnt: int) -> List[UVSFrame]:
    frames = []
    pos = off
    for _ in range(cnt):
        vals = struct.unpack_from('<8f', data, pos)
        frames.append(UVSFrame(
            uv0=(vals[0], vals[1]),
            uv1=(vals[2], vals[3]),
            _unkn=(vals[4], vals[5], vals[6], vals[7]),
        ))
        pos += _PRI_SZ
    return frames


def _parse_strings(data: bytes, str_off: int, str_cnt: int) -> List[UVSString]:
    strings = []
    seen_offsets = {}
    pos = str_off
    for i in range(str_cnt):
        blank, s_off, s_type = struct.unpack_from('<qqi', data, pos)
        pos += _STR_HD
        if i < str_cnt - 1:
            pos += _STR_PAD

        end = data.index(b'\x00', s_off)
        path = data[s_off:end].decode('utf-8')
        # 记录共享字符串副本的最早引用。
        alias = seen_offsets.get(s_off, -1)
        if alias < 0:
            seen_offsets[s_off] = i
        strings.append(UVSString(path=path, type=s_type, _alias_of=alias))
    return strings


# 序列化

def _serialize(uvs: UVSFile) -> bytes:
    groups  = uvs.groups
    strings = uvs.strings
    grp_cnt = len(groups)
    str_cnt = len(strings)

    # 计算绝对偏移。
    grp_off = _HDR_PAD
    data_off = grp_off + grp_cnt * _GRP_SZ

    fd_offsets  = []
    fi_offsets  = []
    map_offsets = []
    cursor = data_off
    for g in groups:
        n = len(g.frames)
        fd_offsets.append(cursor)
        cursor += n * _PRI_SZ

        fi_offsets.append(cursor)
        cursor += n * 4

        # 保留原始间隙；新组以 16 字节对齐。
        gap = g._fi_dat_gap
        if gap < 0:
            rem = cursor % 16
            gap = (16 - rem) % 16

        cursor += gap
        map_offsets.append(cursor)
        if n != 0:
            cursor += _MAP_SZ

    str_head_off = cursor
    str_heads_bytes = str_cnt * _STR_HD + (str_cnt - 1) * _STR_PAD if str_cnt else 0
    str_data_base = str_head_off + str_heads_bytes

    # 字符串副本按 4 字节对齐。
    str_data_offsets = []
    scursor = str_data_base
    for s in strings:
        str_data_offsets.append(scursor)
        scursor += len(s.path.encode('utf-8')) + 1
        rem = scursor % 4
        if rem:
            scursor += 4 - rem

    out = bytearray()

    out += MAGIC
    out += IB_SIG
    out += struct.pack('<q', grp_off)
    out += struct.pack('<q', grp_cnt)
    # 空字符串表的偏移必须写 0。
    out += struct.pack('<q', str_head_off if str_cnt else 0)
    out += struct.pack('<q', str_cnt)
    while len(out) < _HDR_PAD:
        out += b'\x00'

    for i, g in enumerate(groups):
        n = len(g.frames)
        # 空组保留其原始零偏移写法。
        if n == 0 and g.map_count == 0 and g._zero_empty_offsets:
            fdo = fio = mpo = 0
        else:
            fdo, fio, mpo = fd_offsets[i], fi_offsets[i], map_offsets[i]
        out += struct.pack('<qqqqqqffq',
            fdo, n,
            fio, n,
            mpo, g.map_count,
            g.unkn32_0, g.unkn32_1,
            g.dynamic,
        )

    for i, g in enumerate(groups):
        n = len(g.frames)
        for f in g.frames:
            out += struct.pack('<8f',
                f.uv0[0], f.uv0[1],
                f.uv1[0], f.uv1[1],
                f._unkn[0], f._unkn[1], f._unkn[2], f._unkn[3],
            )
        fi = g._frame_indices if g._frame_indices else list(range(n))
        out += struct.pack(f'<{n}i', *fi)

        # map 索引前保留原始间隙或使用新组对齐。
        gap = g._fi_dat_gap if g._fi_dat_gap >= 0 else (16 - (len(out) % 16)) % 16
        out += b'\x00' * gap

        if n != 0:
            padded = (g.path_indices + [0, 0, 0, 0])[:4]
            out += struct.pack('<4i', *padded)

    # 别名表项指向共享副本，但所有字符串副本仍须写出。
    for i, (st, s_off) in enumerate(zip(strings, str_data_offsets)):
        tgt = st._alias_of
        eff_off = str_data_offsets[tgt] if 0 <= tgt < len(str_data_offsets) else s_off
        out += struct.pack('<qqi', 0, eff_off, st.type)
        if i < str_cnt - 1:
            out += b'\x00' * _STR_PAD

    for i, s in enumerate(strings):
        out += s.path.encode('utf-8') + b'\x00'
        if i < str_cnt - 1:
            while len(out) % 4:
                out += b'\x00'

    return bytes(out)
