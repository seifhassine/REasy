# -*- coding: utf-8 -*-
"""Action 段的字段值 ↔ 字节。

维护约束：
- ``playType`` 是 Action 标签名的 jamcrc，改名时由调用方同步。
- PlayEmitter 的 ``targets`` 是 Entry 段局部索引，跨文件复用时由调用方重定位。
"""
from __future__ import annotations

from ..efxfile import ActionData, ActionEntry
from ..schema.action import ACTION_ENTRY_CODEC
from .attribute import type_key, type_from_key
from .jsonval import to_json, from_json


def action_to_json(pd: ActionData) -> dict:
    entries = []
    for e in pd.entries:
        codec = ACTION_ENTRY_CODEC.get(e.type_hash)
        if codec is None:
            raise ValueError(f'{type_key(e.type_hash)}: 没有 Action codec')
        entries.append({'type': type_key(e.type_hash), 'fields': to_json(codec[0](e.raw))})
    return {'playType': pd.play_type, 'entries': entries}


def action_from_json(obj: dict) -> ActionData:
    entries = []
    for e in obj['entries']:
        type_hash = type_from_key(e['type'])
        codec = ACTION_ENTRY_CODEC.get(type_hash)
        if codec is None:
            raise ValueError(f'{type_key(type_hash)}: 没有 Action codec')
        entries.append(ActionEntry(type_hash=type_hash, raw=codec[1](from_json(e['fields']))))
    return ActionData(play_type=obj['playType'], entries=entries)
