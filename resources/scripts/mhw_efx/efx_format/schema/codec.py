# -*- coding: utf-8 -*-
"""Schema 字段的核心编解码。

维护约束：
- schema 按字段顺序读写；固定 schema 必须满足 ``pack(unpack(data)) == data``。
- 所有标量按小端处理；动态 ``path`` 只能以原始 bytes 表示，不能参与静态大小计算。
- 本模块是底层 codec，不依赖 attributes、custom_codecs 或 structs。
"""
from __future__ import annotations
import struct
from typing import Any, Dict, List, Tuple

_SCALAR_SIZE: Dict[str, int] = {
    'i': 4, 'I': 4,
    'f': 4,
    'h': 2, 'H': 2,
    'b': 1, 'B': 1,
    'q': 8, 'Q': 8,
}

# XYZ 编解码

_XYZ_FMT = {
    0: ('<6f', 24),   # float fixed_x random_x fixed_y random_y fixed_z random_z
    1: ('<3i', 12),   # int x y z
    2: ('<4B',  4),   # ubyte x y z pad
    3: ('<3f', 12),   # float x y z
}


def _unpack_xyz(t: int, data: bytes, off: int) -> Tuple[list, int]:
    fmt, size = _XYZ_FMT[t]
    vals = list(struct.unpack_from(fmt, data, off))
    return vals, off + size


def _pack_xyz(t: int, vals: list) -> bytes:
    fmt, _ = _XYZ_FMT[t]
    return struct.pack(fmt, *vals)


def _xyz_size(t: int) -> int:
    return _XYZ_FMT[t][1]


# EPVColorSlot 编解码

_EPVCSLOT_FIELDS = [
    ('epvColorSlot', 'i'),
    ('color1', ('XYZ', 2)),
    ('null2', 'i'),
    ('color2', ('XYZ', 2)),
    ('spacer4', 'i'),
    ('unkn15', 'i'),
    ('size', 'f'),
    ('unkn17', 'i'),
    ('unkn18_0', 'B'),
    ('unkn18_1', 'B'),
    ('spacer5', 'h'),
]
_EPVCSLOT_SIZE = 36

# 'EPVColorSlotTail'：去掉末 4 字节的 EPVColorSlot。RIBBONBLADE 尾端之后的 4 字节是独立字段
_EPVCSLOT_TAIL_FIELDS = _EPVCSLOT_FIELDS[:8]
_EPVCSLOT_TAIL_SIZE = 32

#: 定长嵌套结构 spec → (子字段表, 字节数)
STRUCT_SPECS = {
    'EPVColorSlot': (_EPVCSLOT_FIELDS, _EPVCSLOT_SIZE),
    'EPVColorSlotTail': (_EPVCSLOT_TAIL_FIELDS, _EPVCSLOT_TAIL_SIZE),
}


def _unpack_epvcolorslot(data: bytes, off: int) -> Tuple[dict, int]:
    """解码一个 EPVColorSlot。"""
    d, new_off = unpack(_EPVCSLOT_FIELDS, data, off)
    return d, new_off


def _pack_epvcolorslot(vals: dict) -> bytes:
    return pack(_EPVCSLOT_FIELDS, vals)


# 核心 codec

def unpack(schema: list, data: bytes, off: int = 0) -> Tuple[Dict[str, Any], int]:
    """从偏移处按 schema 解码，返回字段值及新偏移。"""
    values: Dict[str, Any] = {}
    for name, spec in schema:
        if isinstance(spec, str):
            if spec in _SCALAR_SIZE:
                size = _SCALAR_SIZE[spec]
                (val,) = struct.unpack_from('<' + spec, data, off)
                values[name] = val
                off += size
            elif spec == 'colour':
                vals = list(struct.unpack_from('<4B', data, off))
                values[name] = vals
                off += 4
            elif spec in STRUCT_SPECS:
                d, off = unpack(STRUCT_SPECS[spec][0], data, off)
                values[name] = d
            else:
                raise ValueError(f'Unknown scalar spec {spec!r} for field {name!r}')
        elif isinstance(spec, tuple):
            tag = spec[0]
            if tag == 'XYZ':
                xyz_type = spec[1]
                vals, off = _unpack_xyz(xyz_type, data, off)
                values[name] = vals
            elif tag == 'XYZ[]':
                xyz_type, count = spec[1], spec[2]
                arr = []
                for _ in range(count):
                    vals, off = _unpack_xyz(xyz_type, data, off)
                    arr.append(vals)
                values[name] = arr
            elif tag == 'colour[]':
                count = spec[1]
                arr = []
                for _ in range(count):
                    vals = list(struct.unpack_from('<4B', data, off))
                    arr.append(vals)
                    off += 4
                values[name] = arr
            elif tag == 'EPVColorSlot[]':
                count = spec[1]
                arr = []
                for _ in range(count):
                    d, off = _unpack_epvcolorslot(data, off)
                    arr.append(d)
                values[name] = arr
            elif tag == 'path':
                (path_len,) = struct.unpack_from('<i', data, off)
                off += 4
                values[name] = data[off:off + path_len]
                off += path_len
            elif tag in _SCALAR_SIZE or (len(tag) == 1 and tag in 'iIfFhHbBqQ'):
                scalar, count = spec
                size = _SCALAR_SIZE[scalar]
                vals = list(struct.unpack_from(f'<{count}{scalar}', data, off))
                values[name] = vals
                off += size * count
            else:
                raise ValueError(f'Unknown compound spec {spec!r} for field {name!r}')
        else:
            raise ValueError(f'Bad spec type {type(spec)} for field {name!r}')
    return values, off


def pack(schema: list, values: Dict[str, Any]) -> bytes:
    """按 schema 编码字段值。"""
    parts: List[bytes] = []
    for name, spec in schema:
        val = values[name]
        if isinstance(spec, str):
            if spec in _SCALAR_SIZE:
                parts.append(struct.pack('<' + spec, val))
            elif spec == 'colour':
                parts.append(struct.pack('<4B', *val))
            elif spec in STRUCT_SPECS:
                parts.append(pack(STRUCT_SPECS[spec][0], val))
            else:
                raise ValueError(f'Unknown scalar spec {spec!r} for field {name!r}')
        elif isinstance(spec, tuple):
            tag = spec[0]
            if tag == 'XYZ':
                parts.append(_pack_xyz(spec[1], val))
            elif tag == 'XYZ[]':
                xyz_type, count = spec[1], spec[2]
                for item in val:
                    parts.append(_pack_xyz(xyz_type, item))
            elif tag == 'colour[]':
                for item in val:
                    parts.append(struct.pack('<4B', *item))
            elif tag == 'EPVColorSlot[]':
                for item in val:
                    parts.append(_pack_epvcolorslot(item))
            elif tag == 'path':
                path_bytes = val
                parts.append(struct.pack('<i', len(path_bytes)))
                parts.append(path_bytes)
            elif tag in _SCALAR_SIZE or (len(tag) == 1 and tag in 'iIfFhHbBqQ'):
                scalar, count = spec
                parts.append(struct.pack(f'<{count}{scalar}', *val))
            else:
                raise ValueError(f'Unknown compound spec {spec!r} for field {name!r}')
        else:
            raise ValueError(f'Bad spec type {type(spec)} for field {name!r}')
    return b''.join(parts)


def _schema_size(schema: list) -> int:
    """返回固定 schema 的总字节数。"""
    total = 0
    for _name, spec in schema:
        if isinstance(spec, str):
            if spec in _SCALAR_SIZE:
                total += _SCALAR_SIZE[spec]
            elif spec == 'colour':
                total += 4
            elif spec in STRUCT_SPECS:
                total += STRUCT_SPECS[spec][1]
            else:
                raise ValueError(f'Cannot compute size for dynamic spec {spec!r}')
        elif isinstance(spec, tuple):
            tag = spec[0]
            if tag == 'XYZ':
                total += _xyz_size(spec[1])
            elif tag == 'XYZ[]':
                total += _xyz_size(spec[1]) * spec[2]
            elif tag == 'colour[]':
                total += 4 * spec[1]
            elif tag == 'EPVColorSlot[]':
                total += _EPVCSLOT_SIZE * spec[1]
            elif tag == 'path':
                raise ValueError('Cannot compute static size for variable-length path spec')
            else:
                scalar, count = spec
                total += _SCALAR_SIZE[scalar] * count
        else:
            raise ValueError(f'Bad spec type {type(spec)}')
    return total
