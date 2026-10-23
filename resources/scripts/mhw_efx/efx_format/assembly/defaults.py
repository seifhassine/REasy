# -*- coding: utf-8 -*-
"""各类型的结构化默认值。

维护约束：
- 数据在同目录 ``defaults.json``，由 ``tools/gen_default_instances.py`` 从官方语料生成，
  不手工编辑；每份都是一个真实实例，跨段引用已重置为无目标。
- 取值函数每次返回新的 codec 形态字段值，调用方可以直接修改。
- 没有默认值的类型抛 KeyError，不回退到全 0。
"""
from __future__ import annotations

import json
import os

from ..efxfile import RootBody
from .attribute import type_key, decode_attribute, encode_attribute
from .extern import decode_extern_sets, encode_extern_sets
from .jsonval import to_json, from_json

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'defaults.json')
_CACHE = None


def _load() -> dict:
    global _CACHE
    if _CACHE is None:
        with open(_PATH, encoding='utf-8') as f:
            _CACHE = json.load(f)
    return _CACHE


def _get(category: str, type_hash: int):
    table = _load()[category]
    name = type_key(type_hash)
    if name not in table:
        raise KeyError(f'{name}: 没有默认值')
    return from_json(table[name])


def has_default(category: str, type_hash: int) -> bool:
    return type_key(type_hash) in _load()[category]


def default_attribute(type_hash: int) -> dict:
    return _get('attributes', type_hash)


def default_extern_set(type_hash: int) -> dict:
    return _get('externSets', type_hash)


def default_action_entry(type_hash: int) -> dict:
    return _get('actionEntries', type_hash)


def default_root_entry(type_hash: int) -> dict:
    return _get('rootEntries', type_hash)


def default_attribute_bytes(type_hash: int) -> bytes:
    return encode_attribute(type_hash, default_attribute(type_hash))


def default_extern_set_bytes(type_hash: int) -> bytes:
    return encode_extern_sets(type_hash, [default_extern_set(type_hash)])


def default_action_entry_bytes(type_hash: int) -> bytes:
    """Action 条目的默认字节（不含 type hash）。"""
    from ..schema.action import ACTION_ENTRY_CODEC
    return ACTION_ENTRY_CODEC[type_hash][1](default_action_entry(type_hash))


def verify_defaults(table: dict = None) -> list:
    """逐项检查默认值能编码并解回同一结构；返回问题列表，空列表表示全部通过。"""
    from ..schema.action import ACTION_ENTRY_CODEC
    from . import entry as _entry
    from .attribute import type_from_key

    table = _load() if table is None else table
    problems = []

    def check(where, value, roundtrip):
        try:
            expect = json.loads(json.dumps(value))
            got = json.loads(json.dumps(roundtrip(from_json(expect))))
            if got != expect:
                problems.append(f'{where}: 解回结果不一致')
        except Exception as e:
            problems.append(f'{where}: {type(e).__name__}: {e}')

    for name, value in table.get('attributes', {}).items():
        h = type_from_key(name)
        check(f'attributes.{name}', value,
              lambda v, h=h: to_json(decode_attribute(h, encode_attribute(h, v))))
    for name, value in table.get('externSets', {}).items():
        h = type_from_key(name)
        check(f'externSets.{name}', value,
              lambda v, h=h: to_json(decode_extern_sets(h, 1, encode_extern_sets(h, [v]))[0]))
    for name, value in table.get('actionEntries', {}).items():
        unpack_fn, pack_fn = ACTION_ENTRY_CODEC[type_from_key(name)]
        check(f'actionEntries.{name}', value,
              lambda v, u=unpack_fn, p=pack_fn: to_json(u(p(v))))
    for name, value in table.get('rootEntries', {}).items():
        def root_cycle(v, name=name):
            obj = ({'type': name, **to_json(v)} if type_from_key(name) == RootBody.LAYOUTBANK
                   else {'type': name, 'fields': to_json(v)})
            back = _entry._root_sub_to_json(_entry._root_sub_from_json(obj))
            back.pop('type')
            return back.get('fields', back)
        check(f'rootEntries.{name}', value, root_cycle)
    return problems
