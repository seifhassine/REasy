# -*- coding: utf-8 -*-
"""预设格式 v2：attribute / entry / extern / action 四类预设的写出、读取与 v1 升级。

维护约束：
- 预设只含字段值，不含原始字节。写出时剔除 ``DERIVED_KEYS`` 中可由内容推出的长度 / 计数键；
  读取时忽略它们，由 pack 按实际内容重算。清单须与 ``tools/scan_derived_keys.py`` 的结果一致。
- 读取时逐个字段块规整顶层字段：按字节拆分的旧字段按 ``FIELD_BYTE_SPLITS`` 换算，旧名按改名表
  映射（默认 ``FIELD_RENAME_ALIASES``），缺失字段取
  ``defaults.json`` 的同类默认值，未知字段抛 ``PresetError``。嵌套结构（列表、子字典）原样使用。
- 改名表以主属性类型名为键；Extern Set 按对应主属性名查表。
- 跨段引用字段按原值保存，重定位由调用方按 ``source_counts`` 等元数据处理。
- 预设顶层除 ``efx_preset_kind`` / ``format_version`` 与载荷键外的键都是元数据，原样保留。
"""
from __future__ import annotations

import base64
import copy
import json

from .. import hashes as H
from ..efxfile import EFXFile, EntryData, AttrBlock, RootBody
from ..structs import EXTERN_HASH_ALIASES
from ..schema.field_rename_aliases import (FIELD_RENAME_ALIASES, FIELD_BYTE_SPLITS,
                                           FIELD_NESTED_HOISTS, FIELD_VECTOR_MERGES,
                                           split_int_bytes)
from .attribute import type_key, type_from_key, attribute_to_json, attribute_from_json
from .extern import EXTERN_VARLEN_MAIN, extern_to_json, extern_from_json
from .action import action_to_json, action_from_json
from .entry import entry_to_json, entry_from_json
from .defaults import _load as _load_defaults

FORMAT_VERSION = 2

_PATH_LEN = ('path_len',)
DERIVED_KEYS = {
    H.BILLBOARD2D:   _PATH_LEN,
    H.BILLBOARD3D:   _PATH_LEN,
    H.LIGHTNING:     _PATH_LEN,
    H.PLANE:         _PATH_LEN,
    H.RGBWATER:      _PATH_LEN,
    H.RIBBONBLADE:   _PATH_LEN,
    H.STRAINRIBBON:  _PATH_LEN,
    H.TONEMAPFILTER: _PATH_LEN,
    H.TUBELIGHT:     _PATH_LEN,
    H.TURBULENCE:    _PATH_LEN,
    H.UVSEQUENCE:    _PATH_LEN,
    H.MATERIAL:      ('block_count', 'blocks.[].set_count', 'blocks.[].sets.[].path_len'),
    H.PTBEHAVIOR:    ('behav_type_len', 'para_count', 'params.[].path_len'),
}

_PAYLOAD_KEY = {'attribute': 'attribute', 'entry': 'entry', 'extern': 'extern', 'action': 'action'}


class PresetError(ValueError):
    pass


# ── 可推出键 ────────────────────────────────────────────────────────────────

def _codec_hash(category: str, type_hash: int):
    """返回决定可推出键的 codec 类型；没有可推出键时返回 None。"""
    if category == 'attributes':
        return type_hash
    if category == 'externSets':
        return EXTERN_VARLEN_MAIN.get(type_hash)
    return None


def _alias_type_name(category: str, type_hash: int):
    if category == 'attributes':
        return type_key(type_hash)
    if category == 'externSets':
        main = EXTERN_VARLEN_MAIN.get(type_hash) or EXTERN_HASH_ALIASES.get(type_hash)
        return type_key(main) if main is not None else None
    return None


def _delete_path(value, parts):
    head, rest = parts[0], parts[1:]
    if head == '[]':
        if isinstance(value, list):
            for v in value:
                _delete_path(v, rest)
        return
    if isinstance(value, dict) and head in value:
        if rest:
            _delete_path(value[head], rest)
        else:
            del value[head]


def strip_derived(category: str, type_hash: int, fields: dict) -> dict:
    fields = copy.deepcopy(fields)
    for path in DERIVED_KEYS.get(_codec_hash(category, type_hash), ()):
        _delete_path(fields, path.split('.'))
    return fields


def _derived_top(category: str, type_hash: int) -> set:
    return {p for p in DERIVED_KEYS.get(_codec_hash(category, type_hash), ()) if '.' not in p}


# ── 字段规整 ────────────────────────────────────────────────────────────────

def _hoist_nested(alias_name, fields):
    """把旧版嵌套结构里已移到顶层的子字段提出来；显式给出的顶层值优先。"""
    out = None
    for (t, key), moves in FIELD_NESTED_HOISTS.items():
        sub = fields.get(key)
        if t != alias_name or not isinstance(sub, dict) or not any(m in sub for m in moves):
            continue
        if out is None:
            out = dict(fields)
        rest = {k: v for k, v in sub.items() if k not in moves}
        for k, top in moves.items():
            if k in sub and top is not None and top not in fields:
                out[top] = sub[k]
        out[key] = rest
    return fields if out is None else out


def _merge_vectors(alias_name, fields, default):
    """把旧版拆开存的标量分量合并成向量字段；显式给出的向量优先。"""
    out = None
    for (t, key), comps in FIELD_VECTOR_MERGES.items():
        if t != alias_name or key in fields:
            continue
        olds = [next((o for o in names if o in fields), None) for names in comps]
        if not any(olds):
            continue
        if out is None:
            out = dict(fields)
        vec = list(default.get(key, [0.0] * len(comps)))
        for i, o in enumerate(olds):
            if o is not None:
                vec[i] = out.pop(o)
        for names in comps:
            for o in names:
                out.pop(o, None)
        out[key] = vec
    return fields if out is None else out


def normalize_fields(category: str, type_hash: int, fields: dict, aliases=None):
    """规整一个字段块的顶层字段；返回 ``(fields, filled)``，filled 为补默认值的字段名。"""
    name = type_key(type_hash)
    default = _load_defaults()[category].get(name)
    if default is None:
        raise PresetError(f'{name}: 没有默认值，无法校验字段')
    derived = _derived_top(category, type_hash)
    canonical = [k for k in default if k not in derived]
    canonical_set = set(canonical)
    alias_name = _alias_type_name(category, type_hash)
    if aliases is None:
        aliases = FIELD_RENAME_ALIASES

    given = {}
    unknown = []
    fields = _hoist_nested(alias_name, fields)
    fields = _merge_vectors(alias_name, fields, default)
    for k, v in fields.items():
        parts = FIELD_BYTE_SPLITS.get((alias_name, k)) if alias_name else None
        if parts is not None and k not in canonical_set:
            for part, b in zip(parts, split_int_bytes(v)):
                if part in canonical_set and part not in fields:
                    given[part] = b
            continue
        if k in canonical_set:
            given[k] = v
        elif k in derived:
            continue
        else:
            new = aliases.get((alias_name, k)) if alias_name else None
            if new in canonical_set and new not in fields:
                given[new] = v
            else:
                unknown.append(k)
    if unknown:
        raise PresetError(f'{name}: 未知字段 {", ".join(unknown)}')

    out, filled = {}, []
    for k in canonical:
        if k in given:
            out[k] = given[k]
        else:
            out[k] = copy.deepcopy(default[k])
            filled.append(k)
    return out, filled


def _norm_block(category, obj, aliases, filled_log, where):
    type_hash = type_from_key(obj['type'])
    fields, filled = normalize_fields(category, type_hash, obj['fields'], aliases)
    if filled:
        filled_log.append((where, type_key(type_hash), filled))
    return {'type': obj['type'], 'fields': fields}


# ── 写出 ────────────────────────────────────────────────────────────────────

def _wrap(kind: str, payload: dict, meta: dict) -> dict:
    out = {'efx_preset_kind': kind, 'format_version': FORMAT_VERSION}
    out.update({k: v for k, v in meta.items() if v is not None})
    out[_PAYLOAD_KEY[kind]] = payload
    return out


def _strip_attr_json(obj: dict, category='attributes') -> dict:
    return {'type': obj['type'],
            'fields': strip_derived(category, type_from_key(obj['type']), obj['fields'])}


def _strip_entry_json(entry: dict) -> dict:
    entry = dict(entry)
    if entry['kind'] == 'standard':
        entry['attributes'] = [_strip_attr_json(a) for a in entry['attributes']]
    return entry


def _strip_extern_json(ext: dict) -> dict:
    ext = dict(ext)
    items = []
    for it in ext['items']:
        h = type_from_key(it['type'])
        items.append(dict(it, sets=[strip_derived('externSets', h, s) for s in it['sets']]))
    ext['items'] = items
    return ext


def attribute_preset(type_hash: int, data: bytes, **meta) -> dict:
    return _wrap('attribute', _strip_attr_json(attribute_to_json(type_hash, data)), meta)


def entry_preset(body, **meta) -> dict:
    return _wrap('entry', _strip_entry_json(entry_to_json(body)), meta)


def extern_preset(ea, **meta) -> dict:
    return _wrap('extern', _strip_extern_json(extern_to_json(ea)), meta)


def action_preset(pd, **meta) -> dict:
    return _wrap('action', action_to_json(pd), meta)


# ── 读取与组装 ──────────────────────────────────────────────────────────────

def build_attribute(obj: dict, aliases=None, filled_log=None):
    """由 ``{"type", "fields"}`` 组装属性，返回 ``(type_hash, data_bytes)``。"""
    log = [] if filled_log is None else filled_log
    return attribute_from_json(_norm_block('attributes', obj, aliases, log, 'attribute'))


def build_entry(obj: dict, aliases=None, filled_log=None):
    log = [] if filled_log is None else filled_log
    obj = dict(obj)
    if obj['kind'] == 'standard':
        obj['attributes'] = [_norm_block('attributes', a, aliases, log, f'attributes[{i}]')
                             for i, a in enumerate(obj['attributes'])]
    else:
        subs = []
        for i, e in enumerate(obj['entries']):
            h = type_from_key(e['type'])
            if 'fields' in e:
                subs.append(_norm_block('rootEntries', e, aliases, log, f'entries[{i}]'))
            else:
                body = {k: v for k, v in e.items() if k != 'type'}
                norm, filled = normalize_fields('rootEntries', h, body, aliases)
                if filled:
                    log.append((f'entries[{i}]', type_key(h), filled))
                subs.append({'type': e['type'], **norm})
        obj['entries'] = subs
    return entry_from_json(obj)


def build_extern(obj: dict, aliases=None, filled_log=None):
    log = [] if filled_log is None else filled_log
    obj = dict(obj)
    items = []
    for i, it in enumerate(obj['items']):
        h = type_from_key(it['type'])
        sets = []
        for j, s in enumerate(it['sets']):
            norm, filled = normalize_fields('externSets', h, s, aliases)
            if filled:
                log.append((f'items[{i}].sets[{j}]', type_key(h), filled))
            sets.append(norm)
        items.append(dict(it, sets=sets))
    obj['items'] = items
    return extern_from_json(obj)


def build_action(obj: dict, aliases=None, filled_log=None):
    log = [] if filled_log is None else filled_log
    obj = dict(obj)
    entries = []
    for i, e in enumerate(obj['entries']):
        h = type_from_key(e['type'])
        norm, filled = normalize_fields('actionEntries', h, e['fields'], aliases)
        if filled:
            log.append((f'entries[{i}]', type_key(h), filled))
        entries.append({'type': e['type'], 'fields': norm})
    obj['entries'] = entries
    return action_from_json(obj)


def build_preset(preset: dict, aliases=None, filled_log=None):
    """按预设种类组装：attribute 返回 ``(type_hash, bytes)``，其余返回对应段对象。"""
    preset = upgrade_preset(preset)
    kind = preset['efx_preset_kind']
    payload = preset[_PAYLOAD_KEY[kind]]
    builder = {'attribute': build_attribute, 'entry': build_entry,
               'extern': build_extern, 'action': build_action}[kind]
    return builder(payload, aliases, filled_log)


def attribute_preset_type(preset: dict):
    """不升级预设，直接取属性预设的 ``(type_hash, type_name)``；读不出时返回 ``(None, '')``。"""
    try:
        if preset.get('format_version') == FORMAT_VERSION:
            h = type_from_key(preset['attribute']['type'])
        else:
            h = int(str(preset['type_hash']))
    except (KeyError, TypeError, ValueError):
        return None, ''
    return h, H.HASH_TO_NAME.get(h, f'0x{h:08X}')


def preset_meta(preset: dict) -> dict:
    """返回预设元数据（去掉种类、版本与载荷）。"""
    kind = preset.get('efx_preset_kind')
    skip = {'efx_preset_kind', 'format_version', _PAYLOAD_KEY.get(kind)}
    return {k: v for k, v in preset.items() if k not in skip}


# ── v1 升级 ─────────────────────────────────────────────────────────────────

_V1_ATTRIBUTE_META = ('display_name', 'category', 'subgroup')
_V1_ENTRY_META = ('display_name', 'source_label', 'source_counts', 'in_eof')


def _normalize_legacy_body(preset: dict) -> dict:
    """3.0 改名前的 ``body`` 预设只改键名，值不变。"""
    if preset.get('efx_preset_kind') != 'body':
        return preset
    out = dict(preset)
    out['efx_preset_kind'] = 'entry'
    if 'body_kind' in out:
        out['entry_kind'] = out.pop('body_kind')
    if 'blocks' in out:
        out['attributes'] = out.pop('blocks')
    sc = out.get('source_counts')
    if isinstance(sc, dict):
        sc = dict(sc)
        if 'body' in sc:
            sc['entry'] = sc.pop('body')
        if 'play' in sc:
            sc['action'] = sc.pop('play')
        out['source_counts'] = sc
    return out


def _v1_entry_body(preset: dict):
    kind = preset.get('entry_kind', 'standard')
    if kind == 'standard':
        props = preset.get('props', {})
        timl = base64.b64decode(preset.get('timl_bytes') or '')
        blocks = [AttrBlock(type_hash=int(str(a['type_hash'])),
                            data_bytes=base64.b64decode(a['data_bytes']))
                  for a in preset.get('attributes', [])]
        attr_count = len(blocks) or int(str(props.get('attr_count') or 0))
        return EntryData(body_type=int(str(props['body_type'])) & 0xFFFFFFFF,
                         unkn0=int(str(props.get('unkn0') or 0)), attr_count=attr_count,
                         null=int(str(props.get('null') or 0)), timl_length=len(timl),
                         timl_bytes=timl, attr_blocks=blocks)
    if kind == 'root':
        raw = base64.b64decode(preset['raw'])
        body, end = EFXFile._parse_root_body(raw, 0)
        if end != len(raw):
            raise PresetError('Root 预设：原始数据未消费完')
        return body
    raise PresetError(f'未知 entry_kind {kind!r}')


def is_v1_preset(preset) -> bool:
    return (isinstance(preset, dict) and preset.get('format_version') is None
            and preset.get('efx_preset_kind') in ('attribute', 'entry', 'body'))


def upgrade_preset(preset: dict) -> dict:
    """返回 v2 预设；已是 v2 时返回副本。

    v1 预设中的字节在此转为字段值；转换结果重新组装后必须与原字节一致，否则抛 ``PresetError``。
    """
    if preset.get('format_version') == FORMAT_VERSION:
        return copy.deepcopy(preset)
    if preset.get('format_version') is not None:
        raise PresetError(f'不支持的预设版本 {preset.get("format_version")!r}')
    preset = _normalize_legacy_body(preset)
    kind = preset.get('efx_preset_kind')
    if kind == 'attribute':
        meta = {k: preset[k] for k in _V1_ATTRIBUTE_META if k in preset}
        type_hash = int(str(preset['type_hash']))
        data = base64.b64decode(preset['data_bytes'])
        out = attribute_preset(type_hash, data, **meta)
        if build_attribute(out['attribute']) != (type_hash, data):
            raise PresetError(f'{type_key(type_hash)}：转换后字节与原预设不一致')
        return out
    if kind == 'entry':
        meta = {k: preset[k] for k in _V1_ENTRY_META if k in preset}
        body = _v1_entry_body(preset)
        out = entry_preset(body, **meta)
        if build_entry(out['entry']).serialize() != body.serialize():
            raise PresetError('Entry：转换后字节与原预设不一致')
        return out
    raise PresetError(f'未知预设种类 {kind!r}')


def dumps(preset: dict) -> str:
    return json.dumps(preset, ensure_ascii=False, indent=1, allow_nan=False)
