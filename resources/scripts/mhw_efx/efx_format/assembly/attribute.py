# -*- coding: utf-8 -*-
"""属性块的字段值 ↔ 字节。

维护约束：
- 编码只依赖字段值，不依赖原字节；定长类型编码后必须等于登记长度。
- 字段值与 ``AttrBlock.decode()`` 的形状一致，唯一例外是 LAYOUT：其 ``layoutbank_bytes``
  在这里展开为 ``layoutBank`` 结构，编码前再折回。
- 类型以名字标识；没有名字的 hash 写成 ``0xXXXXXXXX``。
"""
from __future__ import annotations

from ..hashes import HASH_TO_NAME, NAME_TO_HASH, LAYOUT
from ..structs import ATTR_SCHEMA_MAP, ATTR_CUSTOM_CODEC, unpack, pack
from ..schema.custom_codecs import unpack_layoutbank_block, pack_layoutbank_block
from .jsonval import to_json, from_json


def type_key(type_hash: int) -> str:
    return HASH_TO_NAME.get(type_hash) or f'0x{type_hash:08X}'


def type_from_key(key) -> int:
    if isinstance(key, int):
        return key
    if key.startswith('0x'):
        return int(key, 16)
    try:
        return NAME_TO_HASH[key]
    except KeyError:
        raise ValueError(f'未知类型名 {key!r}')


def _expand(type_hash: int, values: dict) -> dict:
    if type_hash == LAYOUT:
        blob = values.pop('layoutbank_bytes')
        bank, end = unpack_layoutbank_block(blob, 0)
        if end != len(blob):
            raise ValueError('LAYOUT: layoutBank 未消费完全部字节')
        values['layoutBank'] = bank
    return values


def _collapse(type_hash: int, values: dict) -> dict:
    if type_hash == LAYOUT:
        values = dict(values)
        values['layoutbank_bytes'] = pack_layoutbank_block(values.pop('layoutBank'))
    return values


def decode_attribute(type_hash: int, data: bytes) -> dict:
    """解出字段值；无 codec 或未恰好消费全部字节时抛 ValueError。"""
    entry = ATTR_SCHEMA_MAP.get(type_hash)
    if entry is None:
        raise ValueError(f'{type_key(type_hash)}: 没有 codec')
    schema, size = entry
    if schema == '_custom':
        unpack_fn, _ = ATTR_CUSTOM_CODEC[type_hash]
        values, end = unpack_fn(data, 0)
    else:
        if len(data) != size:
            raise ValueError(f'{type_key(type_hash)}: 长度 {len(data)} != {size}')
        values, end = unpack(schema, data, 0)
    if end != len(data):
        raise ValueError(f'{type_key(type_hash)}: 消费 {end} 字节，数据 {len(data)} 字节')
    return _expand(type_hash, values)


def encode_attribute(type_hash: int, values: dict) -> bytes:
    entry = ATTR_SCHEMA_MAP.get(type_hash)
    if entry is None:
        raise ValueError(f'{type_key(type_hash)}: 没有 codec')
    schema, size = entry
    values = _collapse(type_hash, values)
    if schema == '_custom':
        _, pack_fn = ATTR_CUSTOM_CODEC[type_hash]
        return pack_fn(values)
    data = pack(schema, values)
    if len(data) != size:
        raise ValueError(f'{type_key(type_hash)}: 编码 {len(data)} 字节，应为 {size}')
    return data


def attribute_to_json(type_hash: int, data: bytes) -> dict:
    return {'type': type_key(type_hash),
            'fields': to_json(decode_attribute(type_hash, data))}


def attribute_from_json(obj: dict):
    """返回 ``(type_hash, data_bytes)``。"""
    type_hash = type_from_key(obj['type'])
    return type_hash, encode_attribute(type_hash, from_json(obj['fields']))
