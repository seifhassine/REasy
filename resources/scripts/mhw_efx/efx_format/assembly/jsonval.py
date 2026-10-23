# -*- coding: utf-8 -*-
"""codec 字段值与 JSON 值的双向转换。

维护约束：
- codec 值里的 bytes 只来自路径和类型名等字符串槽，codec 不产生 Python ``str``；因此 JSON
  中的字符串一律按 UTF-8 还原为 bytes，非 UTF-8 内容用 ``{"$hex": ...}`` 表示。
- 全部浮点槽都是 float32：有限值写成按 float32 打包后比特不变的最短十进制（如 ``0.3``），
  因此 JSON 里的浮点与 codec 解出的 Python float 不一定相等，但打包结果相同；
  NaN / Inf 用 ``{"$f32": 位模式}`` 表示，避免写出非标准 JSON。
- dict 键必须是 ``str``；tuple 按 list 写出。
"""
from __future__ import annotations

import math
import struct

_HEX = '$hex'
_F32 = '$f32'


def _short_f32(x: float) -> float:
    bits = struct.pack('<f', x)
    for digits in range(1, 10):
        y = float(f'{x:.{digits}g}')
        if struct.pack('<f', y) == bits:
            return y
    return x


def to_json(value):
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise TypeError(f'to_json: dict 键必须是 str，实际为 {type(k).__name__}')
            out[k] = to_json(v)
        return out
    if isinstance(value, (list, tuple)):
        return [to_json(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        b = bytes(value)
        try:
            s = b.decode('utf-8')
        except UnicodeDecodeError:
            return {_HEX: b.hex()}
        return s
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return _short_f32(value)
        return {_F32: struct.pack('<f', value).hex()}
    if isinstance(value, str):
        raise TypeError('to_json: codec 值不应含 str（字符串槽应为 bytes）')
    raise TypeError(f'to_json: 不支持的值类型 {type(value).__name__}')


def from_json(value):
    if isinstance(value, dict):
        if len(value) == 1:
            if _HEX in value:
                return bytes.fromhex(value[_HEX])
            if _F32 in value:
                return struct.unpack('<f', bytes.fromhex(value[_F32]))[0]
        return {k: from_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_json(v) for v in value]
    if isinstance(value, str):
        return value.encode('utf-8')
    return value
