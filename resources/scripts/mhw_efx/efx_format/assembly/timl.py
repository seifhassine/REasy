# -*- coding: utf-8 -*-
"""TIML 块的结构 ↔ 字节。

维护约束：
- 字节一律经 ``Timl`` 结构化重建产生，不保留原始字节；布局按 16 字节对齐重排。
- 关键帧按所属 transform 的 dataType 用 ``decode_keyframe`` / ``encode_keyframe`` 拆成
  值、左右控制量、帧与插值；``kfDtype`` 是关键帧自带的类型字段，与 transform 的 dataType
  分开保存。
- ``header`` 是魔数之后、animation 数之前的 5 个 int32。
"""
from __future__ import annotations

import struct

from ..timl import (
    Timl, TimlData, TimlType, TimlTransform, TimlKeyframe,
    parse_timl, decode_keyframe, encode_keyframe, _MAGIC,
)
from .jsonval import to_json, from_json


def timl_to_json(data: bytes) -> dict:
    timl = parse_timl(data)
    if timl is None:
        raise ValueError('不是 TIML 数据')
    animations = []
    for d in timl.animations:
        if d is None:
            animations.append(None)
            continue
        types = []
        for t in d.types:
            transforms = []
            for f in t.transforms:
                keyframes = []
                for kf in f.keyframes:
                    k = decode_keyframe(kf.raw, f.data_type, f.datatype_hash)
                    keyframes.append({'frame': k['frame'], 'transition': k['transition'],
                                      'kfDtype': k['kf_dtype'], 'subs': k['subs']})
                transforms.append({'datatype': f.datatype_hash, 'dataType': f.data_type,
                                   'keyframes': keyframes})
            types.append({'timelineParam': t.timeline_param_hash, 'null': t.null,
                          'transforms': transforms})
        animations.append({'dataIx0': d.data_ix0, 'dataIx1': d.data_ix1,
                           'length': d.animation_length, 'loopStart': d.loop_start_point,
                           'loopControl': d.loop_control, 'labelHash': d.label_hash,
                           'types': types})
    return to_json({'header': list(struct.unpack_from('<5i', timl.header, 4)),
                    'animations': animations})


def timl_from_json(obj: dict) -> bytes:
    obj = from_json(obj)
    header = _MAGIC + struct.pack('<5i', *obj['header'])
    animations = []
    for ai, a in enumerate(obj['animations']):
        if a is None:
            animations.append(None)
            continue
        d = TimlData(anim_index=ai, data_ix0=a['dataIx0'], data_ix1=a['dataIx1'],
                     animation_length=a['length'], loop_start_point=a['loopStart'],
                     loop_control=a['loopControl'], label_hash=a['labelHash'])
        for t in a['types']:
            tt = TimlType(timeline_param_hash=t['timelineParam'], null=t['null'])
            for f in t['transforms']:
                tf = TimlTransform(datatype_hash=f['datatype'], data_type=f['dataType'])
                for k in f['keyframes']:
                    raw = encode_keyframe(tf.data_type, tf.datatype_hash, k['frame'],
                                          k['transition'], k['kfDtype'], k['subs'])
                    tf.keyframes.append(TimlKeyframe(raw=raw, frame_timing=k['frame'],
                                                     transition=k['transition'],
                                                     data_type=k['kfDtype']))
                tt.transforms.append(tf)
            d.types.append(tt)
        animations.append(d)
    timl = Timl(raw=b'', header=header + struct.pack('<I', len(animations)),
                count=len(animations), animations=animations, dirty=True)
    return timl.serialize()
