# -*- coding: utf-8 -*-
"""Action 段 PlayEmitter / PlayEFX 条目的字段 codec。

维护约束：
- 输入输出都不含 4 字节 type hash；``ActionEntry.raw`` 与这里的字节一一对应。
- PlayEFX 的 ``path_len`` 位于偏移 4，路径本体位于固定段之后，``path_len`` 由路径长度推出，
  路径字节（含结尾 null 与填充）原样保留。
- PlayEmitter 的 ``targets`` 是 Entry 段局部索引，由调用方负责重定位。
"""
from __future__ import annotations

import struct

from ..hashes import PLAYEMITTER, PLAYEFX

_EMITTER_HEAD = struct.Struct('<4i3f3f3f')   # 52 字节，之后是 target 数与 targets
_EFX_HEAD = struct.Struct('<iiI3i3fi3f3f')    # 64 字节，之后是路径


def unpack_playemitter(raw: bytes) -> dict:
    v = _EMITTER_HEAD.unpack_from(raw, 0)
    (n,) = struct.unpack_from('<i', raw, 52)
    targets = list(struct.unpack_from(f'<{n}i', raw, 56))
    if 56 + 4 * n != len(raw):
        raise ValueError(f'PlayEmitter: target 数 {n} 与长度 {len(raw)} 不符')
    return {
        'typeFlag': v[0],
        'setLandAttribute': v[1],
        'setLandDirection': v[2],
        'rotationOrder': v[3],
        'rotation': list(v[4:7]),
        'scale': list(v[7:10]),
        'translate': list(v[10:13]),
        'targets': targets,
    }


def pack_playemitter(values: dict) -> bytes:
    targets = [int(t) for t in values['targets']]
    return (_EMITTER_HEAD.pack(values['typeFlag'], values['setLandAttribute'],
                               values['setLandDirection'], values['rotationOrder'],
                               *values['rotation'], *values['scale'], *values['translate'])
            + struct.pack(f'<i{len(targets)}i', len(targets), *targets))


def unpack_playefx(raw: bytes) -> dict:
    v = _EFX_HEAD.unpack_from(raw, 0)
    path_len = v[1]
    if 64 + path_len != len(raw):
        raise ValueError(f'PlayEFX: path_len {path_len} 与长度 {len(raw)} 不符')
    return {
        'typeFlag': v[0],
        'efxType': v[2],
        'setLandAttribute': v[3],
        'setLandDirection': v[4],
        'unkn2': v[5],
        'rotation': list(v[6:9]),
        'rotationOrder': v[9],
        'scale': list(v[10:13]),
        'translate': list(v[13:16]),
        'path': raw[64:],
    }


def pack_playefx(values: dict) -> bytes:
    path = bytes(values['path'])
    return _EFX_HEAD.pack(values['typeFlag'], len(path), values['efxType'],
                          values['setLandAttribute'], values['setLandDirection'],
                          values['unkn2'], *values['rotation'], values['rotationOrder'],
                          *values['scale'], *values['translate']) + path


ACTION_ENTRY_CODEC = {
    PLAYEMITTER: (unpack_playemitter, pack_playemitter),
    PLAYEFX:     (unpack_playefx,     pack_playefx),
}
