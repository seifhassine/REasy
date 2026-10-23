# -*- coding: utf-8 -*-
"""只读解析 .mod3 的材质名表。

维护约束：
- .mrl3 仅保存材质名哈希；调用方可用配套 .mod3 的槽位名称补全显示名。
- 名称条目固定为 128 字节，解析仅依赖 materialCount 与 materialOffset。
"""

import struct


_MAGIC = 4476749
_MATERIAL_NAME_STRIDE = 128


class Mod3ParseError(Exception):
    """.mod3 数据无效或不完整。"""


def read_material_names(data: bytes) -> list:
    """返回按材质槽下标排列的 .mod3 名称。"""
    if len(data) < 72 or struct.unpack_from('<I', data, 0)[0] != _MAGIC:
        raise Mod3ParseError("not a MHW .mod3 file (magic mismatch)")

    # materialCount 与 materialOffset 位于固定 FileHeader 偏移。
    material_count = struct.unpack_from('<H', data, 10)[0]
    material_offset = struct.unpack_from('<Q', data, 64)[0]

    names = []
    for i in range(material_count):
        base = material_offset + i * _MATERIAL_NAME_STRIDE
        end = data.find(b'\x00', base)
        if end < 0:
            break
        names.append(data[base:end].decode('ascii', errors='replace'))
    return names
