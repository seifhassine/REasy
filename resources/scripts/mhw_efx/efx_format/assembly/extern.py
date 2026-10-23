# -*- coding: utf-8 -*-
"""Extern 段的字段值 ↔ 字节。

维护约束：
- 一个 Extern item 的数据是 ``attr_count`` 个 Set 元素首尾相接；``attr_count`` 由 Set 数推出。
- 定长类型每个 Set 用 Extern 专用 schema；变长类型每个 Set 复用对应主属性 codec，
  并用 ``decode_attribute`` 的值形状。
- ``attrType`` 是 Extern 标签名的 jamcrc，改名时由调用方同步。
"""
from __future__ import annotations

import copy

from .. import hashes as H
from ..structs import (
    ATTR_CUSTOM_CODEC, ATTR_SCHEMA_MAP, EXTERN_HASH_ALIASES, unpack, pack, _schema_size,
    EXTERN_SPAWN_SCHEMA, EXTERN_VELOCITY3D_SCHEMA, EXTERN_SCALEANIM_SCHEMA,
    EXTERN_EMITTERSHAPE3D_SCHEMA, EXTERN_RGBFIRE_SCHEMA, EXTERN_TRANSFORM3D_SCHEMA,
    PLEMISSIVE_SCHEMA, LIFE_SCHEMA, PLSNOW_SCHEMA, PARENTEMISSIVE_SCHEMA,
    ROTATEANIM_SCHEMA, FADEBYANGLE_SCHEMA, FADEBYDEPTH_SCHEMA, UVCONTROL_SCHEMA,
    GUIDE_SCHEMA, PARENTSNOW_SCHEMA, OTOMOSNOW_SCHEMA,
)
from ..efxfile import ExternAttribute, ExternDataItem
from .attribute import type_key, type_from_key
from .jsonval import to_json, from_json

EXTERN_FIXED_SCHEMA = {
    H.EXTERNSPAWN:          EXTERN_SPAWN_SCHEMA,
    H.EXTERNVELOCITY3D:     EXTERN_VELOCITY3D_SCHEMA,
    H.EXTERNSCALEANIM:      EXTERN_SCALEANIM_SCHEMA,
    H.EXTERNEMITTERSHAPE3D: EXTERN_EMITTERSHAPE3D_SCHEMA,
    H.EXTERNRGBFIRE:        EXTERN_RGBFIRE_SCHEMA,
    H.EXTERNTRANSFORM3D:    EXTERN_TRANSFORM3D_SCHEMA,
    H.EXTERNPLEMISSIVE:     PLEMISSIVE_SCHEMA,
    H.EXTERNLIFE:           LIFE_SCHEMA,
    H.EXTERNPLSNOW:         PLSNOW_SCHEMA,
    H.EXTERNPARENTEMISSIVE: PARENTEMISSIVE_SCHEMA,
    H.EXTERNROTATEANIM:     ROTATEANIM_SCHEMA,
    H.EXTERNFADEBYANGLE:    FADEBYANGLE_SCHEMA,
    H.EXTERNFADEBYDEPTH:    FADEBYDEPTH_SCHEMA,
    H.EXTERNUVCONTROL:      UVCONTROL_SCHEMA,
    H.EXTERNGUIDE:          GUIDE_SCHEMA,
    H.EXTERNPARENTSNOW:     PARENTSNOW_SCHEMA,
    H.EXTERNOTOMOSNOW:      OTOMOSNOW_SCHEMA,
}

# 变长 Extern 类型 → 提供 Set 元素 codec 的主属性类型。
EXTERN_VARLEN_MAIN = {
    H.EXTERNMESH:         H.MESH,
    H.EXTERNPTBEHAVIOR:   H.PTBEHAVIOR,
    H.EXTERNUVSEQUENCE:   H.UVSEQUENCE,
    H.EXTERNBILLBOARD3D:  H.BILLBOARD3D,
    H.EXTERNRGBWATER:     H.RGBWATER,
    H.EXTERNSTRAINRIBBON: H.STRAINRIBBON,
    H.EXTERNTURBULENCE:   H.TURBULENCE,
    H.EXTERNTYPERIBBON:   H.RIBBON,
    H.EXTERNTYPEPLANE:    H.PLANE,
}


def extern_main_type(type_hash: int):
    """返回 Extern 类型对应的主属性类型；没有对应时返回 None。"""
    return EXTERN_VARLEN_MAIN.get(type_hash) or EXTERN_HASH_ALIASES.get(type_hash)


def extern_set_from_main(type_hash: int, main_values: dict) -> dict:
    """由主属性字段值生成一个 Set。

    变长类型与主属性共用 codec，直接复制；定长类型从该 Extern 类型的默认值出发，只覆盖
    同名且 spec 相同的字段，其余保持默认值。
    """
    if type_hash in EXTERN_VARLEN_MAIN:
        return copy.deepcopy(main_values)
    from .defaults import default_extern_set
    values = default_extern_set(type_hash)
    main_schema = ATTR_SCHEMA_MAP.get(EXTERN_HASH_ALIASES.get(type_hash), (None,))[0]
    main_specs = dict(main_schema) if isinstance(main_schema, list) else {}
    for name, spec in EXTERN_FIXED_SCHEMA[type_hash]:
        if name in main_values and main_specs.get(name) == spec:
            values[name] = copy.deepcopy(main_values[name])
    return values


def decode_extern_sets(type_hash: int, attr_count: int, data: bytes) -> list:
    """把 item 数据拆成 Set 字段值列表；必须恰好消费全部字节。"""
    sets = []
    off = 0
    if type_hash in EXTERN_FIXED_SCHEMA:
        schema = EXTERN_FIXED_SCHEMA[type_hash]
        for _ in range(attr_count):
            values, off = unpack(schema, data, off)
            sets.append(values)
    elif type_hash in EXTERN_VARLEN_MAIN:
        unpack_fn, _ = ATTR_CUSTOM_CODEC[EXTERN_VARLEN_MAIN[type_hash]]
        for _ in range(attr_count):
            values, off = unpack_fn(data, off)
            sets.append(values)
    else:
        raise ValueError(f'{type_key(type_hash)}: 没有 Extern codec')
    if off != len(data):
        raise ValueError(f'{type_key(type_hash)}: 消费 {off} 字节，数据 {len(data)} 字节')
    return sets


def encode_extern_sets(type_hash: int, sets: list) -> bytes:
    if type_hash in EXTERN_FIXED_SCHEMA:
        schema = EXTERN_FIXED_SCHEMA[type_hash]
        size = _schema_size(schema)
        out = b''
        for values in sets:
            data = pack(schema, values)
            if len(data) != size:
                raise ValueError(f'{type_key(type_hash)}: Set 编码 {len(data)} 字节，应为 {size}')
            out += data
        return out
    if type_hash in EXTERN_VARLEN_MAIN:
        _, pack_fn = ATTR_CUSTOM_CODEC[EXTERN_VARLEN_MAIN[type_hash]]
        return b''.join(pack_fn(values) for values in sets)
    raise ValueError(f'{type_key(type_hash)}: 没有 Extern codec')


def extern_to_json(ea: ExternAttribute) -> dict:
    return {
        'attrType': ea.attr_type,
        'null0': ea.null0,
        'null1': ea.null1,
        'items': [
            {'type': type_key(it.type_hash),
             'unkn': it.unkn,
             'sets': to_json(decode_extern_sets(it.type_hash, it.attr_count, it.data_bytes))}
            for it in ea.items
        ],
    }


def extern_from_json(obj: dict) -> ExternAttribute:
    items = []
    for it in obj['items']:
        type_hash = type_from_key(it['type'])
        sets = from_json(it['sets'])
        items.append(ExternDataItem(type_hash=type_hash, unkn=it['unkn'],
                                    attr_count=len(sets),
                                    data_bytes=encode_extern_sets(type_hash, sets)))
    return ExternAttribute(attr_type=obj['attrType'], null0=obj['null0'],
                           null1=obj['null1'], items=items)
