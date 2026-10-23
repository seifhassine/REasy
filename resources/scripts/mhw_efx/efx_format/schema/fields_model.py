# -*- coding: utf-8 -*-
"""带语义的字段模型与注册表。

维护约束：
- Field 是元数据层，``Attribute.schema`` 必须降级为 codec 兼容的 ``(name, spec)`` 序列，
  且保持字节等价。
- ``name`` 是字段元数据、预设和 TIML 映射的稳定键，重命名前必须同步全部消费者。
- 新旧 tuple schema 可共存；typed Attribute 是逐步迁移的派生视图。
"""

from __future__ import annotations
from typing import Any, Callable, List, Optional, Tuple


# 枚举与位定义

class EnumOption(object):
    """一个枚举取值。`hidden` 的取值不列入下拉，仅在当前值恰为它时显示。"""
    __slots__ = ("value", "en", "zh", "hidden")

    def __init__(self, value, en, zh="", hidden=False):
        self.value = int(value)
        self.en = en
        self.zh = zh or en
        self.hidden = bool(hidden)


class EnumDef(object):
    """可复用的枚举定义。"""
    __slots__ = ("name", "options")

    def __init__(self, name, options):
        self.name = name
        self.options = [
            o if isinstance(o, EnumOption) else EnumOption(*o)
            for o in options
        ]

    def label(self, value, zh=False):
        """返回取值标签；未知值返回 ``None``。"""
        for o in self.options:
            if o.value == value:
                return o.zh if zh else o.en
        return None


class BitDef(object):
    """可组合的单个位定义。"""
    __slots__ = ("bit", "en", "zh")

    def __init__(self, bit, en, zh=""):
        self.bit = int(bit)
        self.en = en
        self.zh = zh or en


class BitEnum(object):
    """由 mask 编码的互斥位组。"""
    __slots__ = ("mask", "shift", "en", "zh", "options")

    def __init__(self, mask, options, en="", zh=""):
        self.mask = int(mask)
        self.shift = (self.mask & -self.mask).bit_length() - 1 if self.mask else 0
        self.en = en
        self.zh = zh or en
        self.options = [
            o if isinstance(o, EnumOption) else EnumOption(*o)
            for o in options
        ]


# Field 基类与便捷子类

class Field(object):
    """带语义的字段声明，可降级为 codec 使用的 ``(name, spec)``。"""
    widget = "raw"

    def __init__(self, name, spec, *, label_en=None, label_zh=None,
                 tip_en=None, tip_zh=None, readonly=False, widget=None):
        self.name = name
        self.spec = spec
        self.label_en = label_en
        self.label_zh = label_zh
        self.tip_en = tip_en
        self.tip_zh = tip_zh
        self.readonly = readonly
        if widget is not None:
            self.widget = widget

    def __repr__(self):
        return "%s(%r, spec=%r)" % (type(self).__name__, self.name, self.spec)


class _Scalar(Field):
    """标量字段基类。"""
    _SPEC = None
    _WIDGET = "raw"

    def __init__(self, name, **kw):
        super().__init__(name, self._SPEC, widget=self._WIDGET, **kw)


class Int(_Scalar):    _SPEC = 'i';  _WIDGET = "int"
class UInt(_Scalar):   _SPEC = 'I';  _WIDGET = "uint"
class Short(_Scalar):  _SPEC = 'h';  _WIDGET = "int"
class UShort(_Scalar): _SPEC = 'H';  _WIDGET = "int"
class Byte(_Scalar):   _SPEC = 'B';  _WIDGET = "int"
class SByte(_Scalar):  _SPEC = 'b';  _WIDGET = "int"
class Float(_Scalar):  _SPEC = 'f';  _WIDGET = "float"
class Int64(_Scalar):  _SPEC = 'q';  _WIDGET = "uint"
class UInt64(_Scalar): _SPEC = 'Q';  _WIDGET = "uint"


class Enum(Field):
    """底层整数的枚举字段。"""
    def __init__(self, name, enum_def, *, backing='i', **kw):
        super().__init__(name, backing, widget="enum", **kw)
        self.enum = enum_def


class EnumVec3(Field):
    """每个分量独立取值的三轴枚举字段。"""
    def __init__(self, name, enum_def, *, spec=('XYZ', 1), **kw):
        super().__init__(name, spec, widget="enum_vec3", **kw)
        self.enum = enum_def


class Bool(Field):
    """底层整数的布尔字段。"""
    def __init__(self, name, *, backing='i', **kw):
        super().__init__(name, backing, widget="bool", **kw)


class Bitmask(Field):
    """位掩码字段。

    未建模的残留位必须原样保留；``strict`` 只限制 UI 编辑入口，不能清零残留位。
    """
    def __init__(self, name, bits, *, backing='i', strict=False, all_value=None,
                 gate_first=False, **kw):
        super().__init__(name, backing, widget="bitmask", **kw)
        self.bits = [
            b if isinstance(b, (BitDef, BitEnum)) else BitDef(*b)
            for b in bits
        ]
        self.strict = strict
        self.all_value = all_value
        self.gate_first = gate_first


class Raw(Field):
    """原样包装任意 legacy spec，供渐进迁移使用。"""
    def __init__(self, name, spec, **kw):
        super().__init__(name, spec, widget="raw", **kw)


# Attribute 模型

class Attribute(object):
    """一个 EFX attribute 块的 typed 声明。"""
    __slots__ = ("hash", "size", "fields", "label_zh",
                 "native_timl_axis", "validate", "_schema_cache")

    def __init__(self, size, fields, *, hash=None, label_zh=None,
                 native_timl_axis=None, validate=None):
        self.hash = hash
        self.size = size
        self.fields = list(fields)
        self.label_zh = label_zh
        self.native_timl_axis = native_timl_axis
        self.validate = validate
        self._schema_cache = None

    @property
    def schema(self):
        """返回 codec schema，并缓存单一 list 对象供 identity 反查。"""
        if self._schema_cache is None:
            self._schema_cache = [(f.name, f.spec) for f in self.fields]
        return self._schema_cache

    def field_by_name(self, name):
        for f in self.fields:
            if f.name == name:
                return f
        return None


# 全局注册表

ATTR_REGISTRY = {}
FIELD_REGISTRY = {}


def register(attr):
    """将已设置 hash 的 Attribute 登记到全局注册表。"""
    if attr.hash is None:
        raise ValueError("register(): Attribute.hash 未回填，无法登记")
    ATTR_REGISTRY[attr.hash] = attr
    for f in attr.fields:
        FIELD_REGISTRY[(attr.hash, f.name)] = f
    return attr


def register_alias(hash_value, attr):
    """将 Attribute 的字段元数据注册到复用 schema 的另一 hash。

    该操作不得修改原 Attribute 的 ``hash``。
    """
    ATTR_REGISTRY[hash_value] = attr
    for f in attr.fields:
        FIELD_REGISTRY[(hash_value, f.name)] = f
    return attr


# legacy schema 的机械降级

_SPEC_TO_FIELD = {
    'i': Int, 'I': UInt, 'h': Short, 'H': UShort,
    'B': Byte, 'b': SByte, 'f': Float, 'q': Int64, 'Q': UInt64,
}


def attr_from_legacy(size, schema, *, labels=None, overrides=None, hash=None):
    """将 legacy tuple schema 机械降级为 typed Attribute。

    override 的 spec 必须与原 schema 相同，以保持字节等价。
    """
    labels = labels or {}
    overrides = overrides or {}
    fields = []
    for name, spec in schema:
        ov = overrides.get(name)
        if ov is not None:
            if ov.spec != spec:
                raise ValueError(
                    "attr_from_legacy: override %r spec %r != schema spec %r"
                    % (name, ov.spec, spec))
            fields.append(ov)
            continue
        lz = labels.get(name)
        cls = _SPEC_TO_FIELD.get(spec) if isinstance(spec, str) else None
        if cls is not None:
            fields.append(cls(name, label_zh=lz))
        else:
            fields.append(Raw(name, spec, label_zh=lz))
    return Attribute(size=size, fields=fields, hash=hash)
