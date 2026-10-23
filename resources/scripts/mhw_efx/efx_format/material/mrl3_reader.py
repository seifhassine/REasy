"""只读解析 .mrl3 材质，供 MATERIAL 槽引用已有材质数据。

维护约束：
- 解析不依赖 bpy，也不导入材质或 mesh。
- MaterialInfo 的 mmtr_hash 是 EFX MATERIAL 使用的材质类型哈希；shader_hash 不替代它。
- 资源哈希右移 12 位后匹配材质类型的编码表；贴图值为字符串表的 1-based 索引。
"""

import io
import struct


_MAGIC = 5001805
_TEXTURE_ENTRY_SIZE = 272
_MATERIAL_INFO_SIZE = 56


class Mrl3ParseError(Exception):
    """.mrl3 数据无效或不完整。"""


def _read_uint(f) -> int:
    data = f.read(4)
    if len(data) != 4:
        raise Mrl3ParseError("unexpected EOF")
    return struct.unpack('<I', data)[0]


def _read_uint64(f) -> int:
    data = f.read(8)
    if len(data) != 8:
        raise Mrl3ParseError("unexpected EOF")
    return struct.unpack('<Q', data)[0]


def _read_ubyte(f) -> int:
    data = f.read(1)
    if len(data) != 1:
        raise Mrl3ParseError("unexpected EOF")
    return struct.unpack('<B', data)[0]


def _read_ushort(f) -> int:
    data = f.read(2)
    if len(data) != 2:
        raise Mrl3ParseError("unexpected EOF")
    return struct.unpack('<H', data)[0]


def _read_header(f):
    """读取 Mrl3Header，返回材质与贴图表的计数及偏移。"""
    magic = _read_uint(f)
    if magic != _MAGIC:
        raise Mrl3ParseError("not a MHW .mrl3 file (magic mismatch)")
    _version = _read_uint(f)
    _timestamp = _read_uint64(f)
    material_count = _read_uint(f)
    texture_count = _read_uint(f)
    texture_offset = _read_uint64(f)
    material_offset = _read_uint64(f)
    return material_count, material_offset, texture_count, texture_offset


def _read_material_info(f):
    """读取 MaterialInfo，返回名称哈希、类型哈希及资源块位置。"""
    _type_id = _read_uint(f)
    material_name_hash = _read_uint(f)
    mmtr_hash = _read_uint(f)
    _shader_hash = _read_uint(f)
    _block_size = _read_uint(f)
    for _ in range(2):
        _read_ubyte(f)
    resource_count = _read_ushort(f)
    for _ in range(4):
        _read_ubyte(f)
    f.seek(20, io.SEEK_CUR)
    block_offset = _read_uint64(f)
    return material_name_hash, mmtr_hash, resource_count, block_offset


def _read_texture_list(data: bytes, texture_count: int, texture_offset: int) -> list:
    """贴图字符串表：每条 entry 272 字节，路径字符串从 entry+16 开始、'\\0' 结尾。"""
    textures = []
    for i in range(texture_count):
        base = texture_offset + i * _TEXTURE_ENTRY_SIZE + 16
        end = data.find(b'\x00', base)
        if end < 0:
            end = base
        textures.append(data[base:end].decode('ascii', errors='replace'))
    return textures


def _decode_property(data: bytes, off: int, type_str: str):
    """按声明类型读取资源块属性；float[N] 补齐为四个槽。"""
    if type_str == 'bbool':
        return bool(struct.unpack_from('<I', data, off)[0])
    if type_str == 'uint':
        return struct.unpack_from('<I', data, off)[0]
    if type_str == 'float':
        return struct.unpack_from('<f', data, off)[0]
    if type_str and type_str.startswith('float['):
        n = int(type_str[6:-1])
        vals = list(struct.unpack_from(f'<{n}f', data, off))
        vals += [0.0] * (4 - n)
        return vals
    return None


def _read_material_resources(data: bytes, mmtr_hash: int, resource_count: int,
                              block_offset: int, texture_list: list):
    """解析资源块中的贴图与 CB 参数；未收录类型返回两个空 dict。

    贴图值是字符串表的 1-based 索引；CB 值是当前资源块内的相对偏移。
    """
    from .resources import texture_resource_codes, cb_resource_codes, cb_field_layout

    tex_lut = texture_resource_codes(mmtr_hash)
    cb_lut = cb_resource_codes(mmtr_hash)
    textures = {}
    params = {}
    if not tex_lut and not cb_lut:
        return textures, params

    n = resource_count // 2
    for k in range(n):
        off = block_offset + k * 16
        try:
            _res_code, res_hash, res_value, _unkn = struct.unpack_from('<4I', data, off)
        except struct.error:
            break
        key = res_hash >> 12

        tex_name = tex_lut.get(key) if tex_lut else None
        if tex_name is not None:
            if 0 < res_value <= len(texture_list):
                textures[tex_name] = texture_list[res_value - 1]
            continue

        cb_name = cb_lut.get(key) if cb_lut else None
        if cb_name is not None:
            layout = cb_field_layout(mmtr_hash, cb_name)
            if not layout:
                continue
            for _t_hash, field_name, type_str, field_offset in layout:
                abs_off = block_offset + res_value + field_offset
                try:
                    params[field_name] = _decode_property(data, abs_off, type_str)
                except struct.error:
                    continue
    return textures, params


def read_materials(data: bytes) -> list:
    """返回 .mrl3 中的合法材质及可解析的贴图、参数。

    material_name_hash 可直接作为 EFX 的 mat_name_hash，mmtr_hash 作为 mat_shader。
    未收录类型仍返回材质基础信息，贴图与参数为空；无效文件抛 Mrl3ParseError。
    """
    f = io.BytesIO(data)
    material_count, material_offset, texture_count, texture_offset = _read_header(f)

    if not material_count or not material_offset:
        return []

    texture_list = _read_texture_list(data, texture_count, texture_offset) if texture_count and texture_offset else []

    f.seek(material_offset)
    results = []
    for _ in range(material_count):
        material_name_hash, mmtr_hash, resource_count, block_offset = _read_material_info(f)
        if resource_count % 2 != 0:
            continue
        textures, params = _read_material_resources(data, mmtr_hash, resource_count, block_offset, texture_list)
        results.append({
            'material_name_hash': material_name_hash,
            'mmtr_hash': mmtr_hash,
            'textures': textures,
            'params': params,
        })
    return results
