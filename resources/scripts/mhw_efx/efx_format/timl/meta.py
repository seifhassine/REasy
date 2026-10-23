"""TIML animation 元字段的轻量解析与等长原地编辑。

维护约束：
- 本模块只定位 TIML_Data，不结构化编辑关键帧树。
- animationLength、loopStartPoint 与 loopControl 均为固定 4 字节字段；set 操作不得改变
  总长度、偏移或对齐。
- last_keyframe_time 只读遍历关键帧，用于 animationLength 的 grow-only 调整。
"""

import struct
from dataclasses import dataclass
from typing import List, Optional

_TIML_MAGIC = b"timl"

# loopControl 显示名
LOOP_CONTROL_VALUES = {
    0: "No Loop",
    1: "Loop",
    2: "Unkn",
    3: "Unkn Loop",
}

# TIML_Data 内字段偏移
_DATA_ANIMLEN_OFF = 24   # float
_DATA_LOOPSTART_OFF = 28  # float
_DATA_LOOPCTRL_OFF = 32   # int32

_KEYFRAME_SIZE = 20
_KEYFRAME_FRAMETIMING_OFF = 12  # float


@dataclass
class TimlAnimation:
    """一条 animation 的 TIML_Data 元信息。"""
    index: int
    data_offset: int         # 0 表示空 animation
    animation_length: float
    loop_start_point: float
    loop_control: int
    label_hash: int


def is_timl(data: bytes) -> bool:
    return len(data) >= 4 and data[:4] == _TIML_MAGIC


def _header_count_and_anim_base(data: bytes):
    """返回 animation 数量及其偏移；非法或空数据返回 ``(0, None)``。"""
    if not is_timl(data) or len(data) < 28:
        return 0, None
    count = struct.unpack_from("<i", data, 24)[0]
    if count <= 0:
        return 0, None
    return count, 32


def parse_animations(data: bytes) -> List[TimlAnimation]:
    """解析所有 animation 的元字段，不展开关键帧树。"""
    count, anim_base = _header_count_and_anim_base(data)
    if anim_base is None:
        return []
    out = []
    for i in range(count):
        ptr_off = anim_base + i * 8
        if ptr_off + 8 > len(data):
            break
        data_offset = struct.unpack_from("<q", data, ptr_off)[0]
        if data_offset <= 0 or data_offset + 40 > len(data):
            # 空动画或越界数据保留为占位项
            out.append(TimlAnimation(i, 0, 0.0, 0.0, 0, 0))
            continue
        anim_len = struct.unpack_from("<f", data, data_offset + _DATA_ANIMLEN_OFF)[0]
        loop_start = struct.unpack_from("<f", data, data_offset + _DATA_LOOPSTART_OFF)[0]
        loop_ctrl = struct.unpack_from("<i", data, data_offset + _DATA_LOOPCTRL_OFF)[0]
        label_hash = struct.unpack_from("<I", data, data_offset + 36)[0]
        out.append(TimlAnimation(i, data_offset, anim_len, loop_start, loop_ctrl, label_hash))
    return out


def _animation_data_offset(data: bytes, anim_index: int) -> Optional[int]:
    """返回 animation 的 TIML_Data 偏移；空或越界时返回 ``None``。"""
    count, anim_base = _header_count_and_anim_base(data)
    if anim_base is None or not (0 <= anim_index < count):
        return None
    ptr_off = anim_base + anim_index * 8
    if ptr_off + 8 > len(data):
        return None
    data_offset = struct.unpack_from("<q", data, ptr_off)[0]
    if data_offset <= 0 or data_offset + 40 > len(data):
        return None
    return data_offset


# 等长原地写

def set_animation_length(data: bytes, anim_index: int, value: float) -> bytes:
    """原地写 animationLength。返回等长 bytes；无法定位则原样返回。"""
    off = _animation_data_offset(data, anim_index)
    if off is None:
        return data
    buf = bytearray(data)
    struct.pack_into("<f", buf, off + _DATA_ANIMLEN_OFF, float(value))
    return bytes(buf)


def set_loop_start_point(data: bytes, anim_index: int, value: float) -> bytes:
    off = _animation_data_offset(data, anim_index)
    if off is None:
        return data
    buf = bytearray(data)
    struct.pack_into("<f", buf, off + _DATA_LOOPSTART_OFF, float(value))
    return bytes(buf)


def set_loop_control(data: bytes, anim_index: int, value: int) -> bytes:
    off = _animation_data_offset(data, anim_index)
    if off is None:
        return data
    buf = bytearray(data)
    struct.pack_into("<i", buf, off + _DATA_LOOPCTRL_OFF, int(value))
    return bytes(buf)


# 关键帧时间读取

def auto_grow_lengths(data: bytes) -> bytes:
    """将短于最后关键帧的 animationLength 增长至该帧。

    只增不减，以保留动画末尾的 hold 区间。
    """
    anims = parse_animations(data)
    out = data
    for a in anims:
        if a.data_offset == 0:
            continue
        lk = last_keyframe_time(out, a.index)
        if lk is not None and lk > a.animation_length:
            out = set_animation_length(out, a.index, lk)
    return out


def last_keyframe_time(data: bytes, anim_index: int) -> Optional[float]:
    """返回 animation 下关键帧的最大时间；无有效关键帧时返回 ``None``。"""
    data_off = _animation_data_offset(data, anim_index)
    if data_off is None:
        return None
    n = len(data)
    best = None

    def _read_children(struct_off):
        if struct_off < 0 or struct_off + 16 > n:
            return None
        rel = struct.unpack_from("<q", data, struct_off)[0]
        cnt = struct.unpack_from("<q", data, struct_off + 8)[0]
        if rel <= 0 or cnt <= 0:
            return None
        return rel, cnt

    types = _read_children(data_off)
    if types is None:
        return None
    type_base, type_count = types
    for ti in range(type_count):
        type_off = type_base + ti * 24   # TIML_Type = 24 字节
        transforms = _read_children(type_off)
        if transforms is None:
            continue
        tf_base, tf_count = transforms
        for fi in range(tf_count):
            tf_off = tf_base + fi * 24   # TIML_Transform = 24 字节
            kfs = _read_children(tf_off)
            if kfs is None:
                continue
            kf_base, kf_count = kfs
            for ki in range(kf_count):
                ft_off = kf_base + ki * _KEYFRAME_SIZE + _KEYFRAME_FRAMETIMING_OFF
                if ft_off + 4 > n:
                    continue
                ft = struct.unpack_from("<f", data, ft_off)[0]
                if best is None or ft > best:
                    best = ft
    return best
