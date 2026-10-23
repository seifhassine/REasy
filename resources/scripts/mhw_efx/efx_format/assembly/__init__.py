# -*- coding: utf-8 -*-
"""字段值与字节之间的结构化组装层。

维护约束：
- 本层只组合 codec，不定义字节布局；布局属于 ``schema/`` 与 ``efxfile``。
- 所有 ``*_to_json`` 的结果可直接 ``json.dumps``，``*_from_json`` 只依赖其中的字段值，
  不读取任何原始字节。
"""
from .jsonval import to_json, from_json
from .attribute import (
    type_key, type_from_key, decode_attribute, encode_attribute,
    attribute_to_json, attribute_from_json,
)
from .extern import (
    EXTERN_FIXED_SCHEMA, EXTERN_VARLEN_MAIN, extern_main_type, extern_set_from_main,
    decode_extern_sets, encode_extern_sets,
    extern_to_json, extern_from_json,
)
from .action import action_to_json, action_from_json
from .timl import timl_to_json, timl_from_json
from .entry import entry_to_json, entry_from_json
from .efx import efx_to_json, efx_from_json
from .defaults import (
    has_default, default_attribute, default_extern_set, default_action_entry,
    default_root_entry, default_attribute_bytes, default_extern_set_bytes,
    default_action_entry_bytes,
)
from .preset import (
    FORMAT_VERSION, DERIVED_KEYS, PresetError, strip_derived, normalize_fields,
    attribute_preset, entry_preset, extern_preset, action_preset,
    build_attribute, build_entry, build_extern, build_action, build_preset,
    preset_meta, upgrade_preset, is_v1_preset, attribute_preset_type,
)
