"""编辑 PTBEHAVIOR 的稀疏参数覆盖。

维护约束：params 必须是对应 b_type 合并目录的有序子序列，每个 key 至多一次；新增项
按目录顺序插入，const0 复用已有值或由 b_type 计算。
"""

import zlib



def jamcrc(s: str) -> int:
    """MHW/RE Engine 字段哈希：标准 CRC-32 但 xorout=0（即取反）。"""
    return (zlib.crc32(s.encode('ascii')) ^ 0xFFFFFFFF) & 0xFFFFFFFF


def _to_signed32(v: int) -> int:
    """无符号 → 有符号 int32（pack_ptbehavior 用 '<i'，与 unpack 一致存有符号值）。"""
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v >= 0x80000000 else v


def _btype_str(values: dict) -> str:
    """从 values['b_type']（bytes，含末尾 null）取规范字符串（去 null）。"""
    return values['b_type'].decode('latin-1').rstrip('\x00')


def catalog_for(values: dict):
    """返回该实例 b_type 的属性目录 [(key, t, freq), ...]；未知 b_type 返回 []。

    用的是合并表（语料推出的实际用过项 + DTI 补项，见 dti_extra.py），所以"能加什么"
    比"官方文件里出现过什么"更宽——freq=0 的就是 DTI 补项。
    """
    return catalog_for_btype(_btype_str(values))


def catalog_for_btype(b_type: str):
    """按 b_type 字符串取合并后的属性目录；未知 b_type 返回 []。"""
    from .dti_extra import PTBEHAVIOR_CATALOG_FULL
    return PTBEHAVIOR_CATALOG_FULL.get(b_type, [])


def known_btypes():
    """所有已知 b_type（语料见过的 + DTI 补的），供 UI 下拉用。"""
    from .dti_extra import PTBEHAVIOR_CATALOG_FULL
    return sorted(PTBEHAVIOR_CATALOG_FULL)


def _canonical_index(values: dict):
    """key_hash → 规范顺序下标；不在目录内的 key 给一个超大下标（排末尾，稳定）。"""
    cat = catalog_for(values)
    idx = {k: i for i, (k, _t, _f) in enumerate(cat)}
    return idx


def present_keys(values: dict) -> set:
    """当前已覆盖的 key 集合。"""
    return {p['unkn'] & 0xFFFFFFFF for p in values['params']}


def addable_catalog(values: dict):
    """可新增的属性（目录中尚未覆盖的项），保持规范顺序。"""
    have = present_keys(values)
    return [(k, t, f) for (k, t, f) in catalog_for(values)
            if (k & 0xFFFFFFFF) not in have]


# 新增覆盖项的默认字段。
def _default_param_fields(t: int, key: int = 0) -> dict:
    if t == 0x03:
        return {'NULL': 0}
    if t == 0x05:
        return {'unkn0': 0}
    if t == 0x06:
        return {'decal_epv_color_slot': 0}
    if t == 0x0C:
        return {'unkn0': 0.0}
    if t == 0x0F:
        return {'color': [0, 0, 0, 0]}
    if t == 0x14:
        return {'unkn1': [0.0, 0.0, 0.0]}
    if t == 0x15:
        # 颜色参数的第四分量为 alpha，默认不透明。
        from .names import is_color_param
        a = 1.0 if is_color_param(key) else 0.0
        return {'unkn0': 0.0, 'unkn1': 0.0, 'unkn2': 0.0, 'unkn3': a}
    if t == 0x36:
        return {'unkn1': [0, 0]}
    if t == 0x37:
        return {'unkn1': [0.0, 0.0]}
    if t == 0x40:
        return {'unkn0': 0}
    if t == 0x80:
        return {'file_type': 0, 'path_len': 0, 'path': b''}
    return {'unkn_type': 0}


def _const0_for(values: dict) -> int:
    """新增项的 const0 = jamcrc(b_type)；优先复用同块现有项的 const0。"""
    for p in values['params']:
        return p['const0']
    return jamcrc(_btype_str(values))


def add_override(values: dict, key: int) -> bool:
    """
    新增一条覆盖项（key 取自 catalog）。按规范顺序插入以保持子序列不变量。
    返回 True 成功；False 表示 key 已存在或不在目录内。
    """
    key &= 0xFFFFFFFF
    if key in present_keys(values):
        return False
    cat = {k & 0xFFFFFFFF: t for (k, t, _f) in catalog_for(values)}
    if key not in cat:
        return False
    t = cat[key]
    # unkn 与 const0 以有符号 int32 存储。
    param = {'unkn': _to_signed32(key), 'const0': _to_signed32(_const0_for(values)), 't': t}
    param.update(_default_param_fields(t, key))

    # 在首个规范顺序位于 key 之后的现有项之前插入。
    idx = _canonical_index(values)
    key_ci = idx.get(key, 1 << 30)
    params = values['params']
    pos = len(params)
    for i, p in enumerate(params):
        if idx.get(p['unkn'] & 0xFFFFFFFF, 1 << 30) > key_ci:
            pos = i
            break
    params.insert(pos, param)
    values['para_count'] = len(params)
    return True


def remove_override(values: dict, key: int) -> bool:
    """删除指定 key 的覆盖项。返回 True 成功；False 表示未找到。"""
    key &= 0xFFFFFFFF
    params = values['params']
    for i, p in enumerate(params):
        if (p['unkn'] & 0xFFFFFFFF) == key:
            del params[i]
            values['para_count'] = len(params)
            return True
    return False
