# -*- coding: utf-8 -*-
"""LayoutBank_Block 的列规格与结构化编辑。

LAYOUT 属性内嵌的块与 Root LayoutBank 子条目的块同构：``count`` 行，若干可选列，每列在每行上
宽度固定。本模块在 ``unpack_layoutbank_block`` 产出的块字典上做增删改，写回仍走
``pack_layoutbank_block``。

列规格：

    列   每行          存储
    0    3 个值        float32
    1~5  4 个值        float16 位模式（int16 存储）
    6    3 个值        int32
    7    subCount×4 值  float16 位模式（int16 存储）

维护约束：
- 列按 blockType 升序排列且不重复。列 3、5、7 分别从属于列 2、4、6：新增从属列前父列必须存在，
  删除父列时一并删除从属列。
- 块的 ``values`` 始终保存存储值（float16 列为 int16 位模式），换算只发生在读写单元格时，
  未改动的单元格按原位模式写回。
- ``count`` 不大于 0 的块不能带列，因此不允许删除最后一行。
- LAYOUT 前缀的列开关由 ``sync_layout_flags`` 按列的有无写回，只重算改动过的列；六个开关以外的
  前缀字节不动。
"""
from __future__ import annotations

import copy
import math
import struct

#: 全部列类型，按写出顺序
COLUMN_TYPES = (0, 1, 2, 3, 4, 5, 6, 7)

#: 从属列 → 父列
COLUMN_PARENT = {3: 2, 5: 4, 7: 6}

#: LAYOUT 前缀里的列开关 → 由哪一列的有无决定
LAYOUT_COLUMN_FLAGS = {
    'useColumn0': 0,
    'useColumn1': 1,
    'useColumn2': 2,
    'useColumn4': 4,
    'useColumn6': 6,
    'useColumn7': 7,
}

_HALF_MAX = 65504.0


class LayoutBankError(ValueError):
    """块结构不允许的编辑。"""


def column_kind(block_type: int) -> str:
    """列的数值类型：'f32' / 'f16' / 'i32'。"""
    if block_type == 0:
        return 'f32'
    if block_type == 6:
        return 'i32'
    if 1 <= block_type <= 5 or block_type == 7:
        return 'f16'
    raise LayoutBankError(f'未知列类型 {block_type}')


def row_width(block_type: int, sub_count: int = 1) -> int:
    """列在每行上的值个数。"""
    if block_type in (0, 6):
        return 3
    if 1 <= block_type <= 5:
        return 4
    if block_type == 7:
        return 4 * max(1, int(sub_count))
    raise LayoutBankError(f'未知列类型 {block_type}')


def half_from_bits(bits: int) -> float:
    """int16 位模式 → float16 数值。"""
    return struct.unpack('<e', struct.pack('<h', int(bits)))[0]


def bits_from_half(value: float) -> int:
    """数值 → float16 的 int16 位模式；超出范围时截到最大有限值。"""
    v = float(value)
    if math.isnan(v):
        v = 0.0
    v = max(-_HALF_MAX, min(_HALF_MAX, v))
    return struct.unpack('<h', struct.pack('<e', v))[0]


def _to_display(kind, stored):
    if kind == 'f16':
        return half_from_bits(stored)
    if kind == 'i32':
        return int(stored)
    return float(stored)


def _to_stored(kind, shown):
    if kind == 'f16':
        return bits_from_half(shown)
    if kind == 'i32':
        return int(shown)
    return float(shown)


# ── 查询 ────────────────────────────────────────────────────────────────────

def column_types(block: dict) -> list:
    return [int(c['blockType']) for c in block.get('columns', [])]


def find_column(block: dict, block_type: int):
    for c in block.get('columns', []):
        if int(c['blockType']) == block_type:
            return c
    return None


def column_row_width(col: dict) -> int:
    return row_width(int(col['blockType']), col.get('subCount', 1))


def get_row(block: dict, block_type: int, row: int) -> list:
    """某列某行的显示值（float16 已换算为浮点）。"""
    col = find_column(block, block_type)
    if col is None:
        raise LayoutBankError(f'块中没有列 {block_type}')
    _check_row(block, row)
    w = column_row_width(col)
    kind = column_kind(block_type)
    return [_to_display(kind, v) for v in col['values'][row * w:(row + 1) * w]]


def addable_columns(block: dict) -> list:
    """当前可新增的列类型；从属列要求父列已存在。"""
    have = set(column_types(block))
    return [bt for bt in COLUMN_TYPES
            if bt not in have and (bt not in COLUMN_PARENT or COLUMN_PARENT[bt] in have)]


# ── 编辑（原地修改并返回 block）────────────────────────────────────────────

def _check_row(block, row):
    if not 0 <= row < int(block['count']):
        raise LayoutBankError(f'行号 {row} 超出范围 0~{int(block["count"]) - 1}')


def set_row(block: dict, block_type: int, row: int, shown: list) -> dict:
    """写入某列某行；与原显示值相同的单元格保留原位模式。"""
    col = find_column(block, block_type)
    if col is None:
        raise LayoutBankError(f'块中没有列 {block_type}')
    _check_row(block, row)
    w = column_row_width(col)
    if len(shown) != w:
        raise LayoutBankError(f'列 {block_type} 每行需要 {w} 个值，实际 {len(shown)} 个')
    kind = column_kind(block_type)
    base = row * w
    for k, v in enumerate(shown):
        old = col['values'][base + k]
        if _to_display(kind, old) == v:
            continue
        col['values'][base + k] = _to_stored(kind, v)
    return block


def add_row(block: dict, index=None, copy_from=None) -> dict:
    """在 `index`（默认末尾）插入一行；`copy_from` 给定时复制该行，否则各值取 0。"""
    count = int(block['count'])
    if index is None:
        index = count
    if not 0 <= index <= count:
        raise LayoutBankError(f'插入位置 {index} 超出范围 0~{count}')
    if copy_from is not None:
        _check_row(block, copy_from)
    for col in block.get('columns', []):
        w = column_row_width(col)
        if copy_from is not None:
            row_vals = list(col['values'][copy_from * w:(copy_from + 1) * w])
        else:
            row_vals = [_to_stored(column_kind(int(col['blockType'])), 0)] * w
        col['values'][index * w:index * w] = row_vals
    block['count'] = count + 1
    return block


def remove_row(block: dict, row: int) -> dict:
    """删除一行；不允许删除最后一行。"""
    _check_row(block, row)
    count = int(block['count'])
    if count <= 1:
        raise LayoutBankError('不能删除最后一行')
    for col in block.get('columns', []):
        w = column_row_width(col)
        del col['values'][row * w:(row + 1) * w]
    block['count'] = count - 1
    return block


def add_column(block: dict, block_type: int, sub_count: int = 1) -> dict:
    """新增一列，各值取 0；按列类型升序插入。"""
    if int(block['count']) <= 0:
        raise LayoutBankError('空块不能带列，请先新增行')
    if block_type not in addable_columns(block):
        if find_column(block, block_type) is not None:
            raise LayoutBankError(f'列 {block_type} 已存在')
        raise LayoutBankError(f'列 {block_type} 需要先有列 {COLUMN_PARENT[block_type]}')
    col = {'blockType': block_type}
    if block_type == 7:
        col['subCount'] = max(1, int(sub_count))
    n = int(block['count']) * column_row_width(col)
    col['values'] = [_to_stored(column_kind(block_type), 0)] * n
    cols = block.setdefault('columns', [])
    pos = next((i for i, c in enumerate(cols) if int(c['blockType']) > block_type), len(cols))
    cols.insert(pos, col)
    return block


def remove_column(block: dict, block_type: int) -> dict:
    """删除一列；它的从属列一并删除。"""
    if find_column(block, block_type) is None:
        raise LayoutBankError(f'块中没有列 {block_type}')
    drop = {block_type} | {c for c, p in COLUMN_PARENT.items() if p == block_type}
    block['columns'] = [c for c in block['columns'] if int(c['blockType']) not in drop]
    return block


def sync_layout_flags(prefix: dict, block: dict, columns=None) -> dict:
    """按列的有无写回 LAYOUT 前缀的列开关，返回新的前缀字典。

    `columns` 给定时只更新这些列（及其父列）对应的开关：官方文件里列 2/3、4/5 的开关偶有与
    列的有无不一致，只改动过的列才重算，其余保持原值。
    """
    out = dict(prefix)
    have = set(column_types(block))
    touched = None
    if columns is not None:
        touched = set()
        for bt in columns:
            touched.add(bt)
            if bt in COLUMN_PARENT:
                touched.add(COLUMN_PARENT[bt])
    for name, bt in LAYOUT_COLUMN_FLAGS.items():
        if name in out and (touched is None or bt in touched):
            out[name] = 1 if bt in have else 0
    return out


def copy_block(block: dict) -> dict:
    return copy.deepcopy(block)
