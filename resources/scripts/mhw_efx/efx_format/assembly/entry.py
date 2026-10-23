# -*- coding: utf-8 -*-
"""Entry 段条目（普通 Entry 与 Root）的结构 ↔ 字节。

维护约束：
- 普通 Entry 的 ``attr_count`` 与 ``timl_length`` 由属性列表和 TIML 推出；只有「无属性但
  计数非零」这种无法由列表推出的计数才写成 ``attrCount``。
- 属性里的跨段引用（EXTERNREFERENCE、PTLIFE、PTCOLLISION 的索引字段）按原值保存，
  重定位由调用方负责。
- Root 子条目也可以是 ``AttrBlock``（导出端的表示）。UnitBoundary 与 RenderTarget 按同 hash 的属性 codec 编码（数据不含前导 type），
  LayoutBank 为 ``unkn`` + 若干 LayoutBank_Block。
"""
from __future__ import annotations

import struct

from ..efxfile import (
    EntryData, AttrBlock, RootBody, RootUnitBoundary, RootOpaqueEntry,
)
from ..schema.custom_codecs import unpack_layoutbank_block, pack_layoutbank_block
from .attribute import attribute_to_json, attribute_from_json, type_key, type_from_key
from .jsonval import to_json, from_json
from .timl import timl_to_json, timl_from_json


def _entry_to_json(body: EntryData) -> dict:
    out = {
        'kind': 'standard',
        'bodyType': body.body_type,
        'unkn0': body.unkn0,
        'null': body.null,
        'timl': timl_to_json(body.timl_bytes) if body.timl_length else None,
        'attributes': [attribute_to_json(b.type_hash, b.data_bytes) for b in body.attr_blocks],
    }
    if len(body.timl_bytes) != body.timl_length:
        raise ValueError('Entry: timl_length 与 TIML 字节数不符')
    if not body.attr_blocks and body.attr_count != 0:
        out['attrCount'] = body.attr_count
    return out


def _entry_from_json(obj: dict) -> EntryData:
    timl_bytes = timl_from_json(obj['timl']) if obj.get('timl') is not None else b''
    blocks = []
    for a in obj['attributes']:
        type_hash, data = attribute_from_json(a)
        blocks.append(AttrBlock(type_hash=type_hash, data_bytes=data))
    attr_count = obj.get('attrCount', len(blocks))
    return EntryData(body_type=obj['bodyType'], unkn0=obj['unkn0'], attr_count=attr_count,
                     null=obj['null'], timl_length=len(timl_bytes), timl_bytes=timl_bytes,
                     attr_blocks=blocks)


def _root_sub_to_json(e) -> dict:
    if isinstance(e, AttrBlock):
        e = RootOpaqueEntry(raw=e.serialize())
    if isinstance(e, RootUnitBoundary):
        data = struct.pack('<2i', *e.ints) + struct.pack('<8f', *e.floats)
        return attribute_to_json(RootBody.UNITBOUNDARY, data)
    (sub_type,) = struct.unpack_from('<I', e.raw, 0)
    if sub_type == RootBody.LAYOUTBANK:
        unkn, n = struct.unpack_from('<ii', e.raw, 4)
        pos = 12
        blocks = []
        for _ in range(n):
            block, pos = unpack_layoutbank_block(e.raw, pos)
            blocks.append(block)
        if pos != len(e.raw):
            raise ValueError('LayoutBank: 未消费完全部字节')
        return {'type': type_key(sub_type), 'unkn': unkn, 'blocks': to_json(blocks)}
    return attribute_to_json(sub_type, e.raw[4:])


def _root_sub_from_json(obj: dict):
    sub_type = type_from_key(obj['type'])
    if sub_type == RootBody.LAYOUTBANK:
        blocks = from_json(obj['blocks'])
        raw = struct.pack('<Iii', sub_type, obj['unkn'], len(blocks))
        raw += b''.join(pack_layoutbank_block(b) for b in blocks)
        return RootOpaqueEntry(raw=raw)
    type_hash, data = attribute_from_json(obj)
    if type_hash == RootBody.UNITBOUNDARY:
        return RootUnitBoundary(ints=struct.unpack('<2i', data[:8]),
                                floats=struct.unpack('<8f', data[8:]))
    return RootOpaqueEntry(raw=struct.pack('<I', type_hash) + data)


def _root_to_json(body: RootBody) -> dict:
    if body.raw is not None:
        raise ValueError('Root: 主体未拆分为子条目')
    return {'kind': 'root', 'rootType': body.root_type, 'const0': body.const0,
            'const1': body.const1, 'entries': [_root_sub_to_json(e) for e in body.entries]}


def _root_from_json(obj: dict) -> RootBody:
    return RootBody(root_type=obj['rootType'], const0=obj['const0'], const1=obj['const1'],
                    entries=[_root_sub_from_json(e) for e in obj['entries']])


def entry_to_json(body) -> dict:
    if isinstance(body, RootBody):
        return _root_to_json(body)
    return _entry_to_json(body)


def entry_from_json(obj: dict):
    if obj['kind'] == 'root':
        return _root_from_json(obj)
    return _entry_from_json(obj)
