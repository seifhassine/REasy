"""TIML 完整树的解析、编辑与保真序列化。

维护约束：
- TIML 由 animation、data、type、transform 与关键帧组成；偏移均相对 TIML 起点并按小端读写。
- 未编辑的 TIML 必须直接输出 ``raw``；编辑后才按 16 字节对齐布局重建。
- 关键帧固定为 20 字节：值、起点切线、终点切线、帧、插值、dataType；Color 与 BIG_FLAGS 使用多子通道编码。
- 两个切线只在插值 3（三次 Hermite）下生效，是以该关键帧为起点那一段的两端切线，单位是本段平均斜率。
- 本模块保持纯 Python，不能导入 bpy。
"""

import struct
from dataclasses import dataclass, field
from typing import List, Optional

from ..hashes import jamcrc
from .names import DT_TRANSFORM, dt_neutral_value

_MAGIC = b"timl"

# 固定结构大小
_HEADER_SIZE = 28
_DATA_SIZE = 40
_TYPE_SIZE = 24
_TRANSFORM_SIZE = 24
_KEYFRAME_SIZE = 20

# dataType 显示名
DATATYPE_NAMES = {0: "SInt", 1: "Int", 2: "Float", 3: "Color", 4: "Bool"}
# 插值显示名：0/1 阶跃（两者相同）、2 线性、3 三次 Hermite（读两个切线）、4 smoothstep；
# 5/6 在浮点轨道上是退化行为，7 以上不显示
INTERP_NAMES = ["STUCK", "CONSTANT", "LINEAR", "HERMITE", "SMOOTH", "UNK5", "UNK6"]

# BIG_FLAGS 的 value/controlL/controlR 分为高、低 16 位子通道。
BIG_FLAGS = frozenset({
    150806694, 2575924291, 4027018852, 2154666731, 4150962813,
    1852046279, 503910216, 1762541534, 2768909048, 3787782803,
})


def channel_sublabels(data_type: int, datatype_hash: int) -> List[str]:
    """返回 transform 的可编辑子通道标签。"""
    if data_type == 3:
        return ["R", "G", "B", "A"]
    if datatype_hash in BIG_FLAGS:
        return ["lo", "hi"]
    return [""]


#: 关键帧两个控制值的键名；旧 JSON 用 back / period
TANGENT_KEYS = ("startTangent", "endTangent")
_OLD_TANGENT_KEYS = ("back", "period")


def make_sub(value, t0, t1) -> dict:
    """一个子通道：值 + 起点 / 终点切线。"""
    return {"value": value, TANGENT_KEYS[0]: t0, TANGENT_KEYS[1]: t1}


def sub_tangents(sub: dict):
    """读子通道的两个切线；兼容旧键名，缺省为 0。"""
    return tuple(sub.get(k, sub.get(old, 0)) for k, old in zip(TANGENT_KEYS, _OLD_TANGENT_KEYS))


def _val_fmt(data_type: int) -> str:
    """返回标量通道的 struct 格式。"""
    return {0: "<i", 1: "<I", 2: "<f", 4: "<I"}.get(data_type, "<i")


def decode_keyframe(raw: bytes, data_type: int, datatype_hash: int) -> dict:
    """将 20 字节关键帧解码为可编辑的子通道值。"""
    frame = struct.unpack_from("<f", raw, 12)[0]
    transition = struct.unpack_from("<h", raw, 16)[0]
    kf_dtype = struct.unpack_from("<h", raw, 18)[0]
    vraw, lraw, rraw = raw[0:4], raw[4:8], raw[8:12]
    if data_type == 3:
        t0 = struct.unpack("<f", lraw)[0]
        t1 = struct.unpack("<f", rraw)[0]
        subs = [make_sub(vraw[i], t0, t1) for i in range(4)]
    elif datatype_hash in BIG_FLAGS:
        v = struct.unpack("<I", vraw)[0]
        cl = struct.unpack("<I", lraw)[0]
        cr = struct.unpack("<I", rraw)[0]
        subs = [
            make_sub(v & 0xFFFF, cl & 0xFFFF, cr & 0xFFFF),
            make_sub((v >> 16) & 0xFFFF, (cl >> 16) & 0xFFFF, (cr >> 16) & 0xFFFF),
        ]
    else:
        fmt = _val_fmt(data_type)
        v = struct.unpack(fmt, vraw)[0]
        cl = struct.unpack(fmt, lraw)[0]
        cr = struct.unpack(fmt, rraw)[0]
        subs = [make_sub(v, cl, cr)]
    return {"frame": frame, "transition": transition, "kf_dtype": kf_dtype, "subs": subs}


def _u32_bits(data_type: int, x) -> int:
    """将标量转换为 32 位字节模式。"""
    if data_type == 2:
        return struct.unpack("<I", struct.pack("<f", float(x)))[0]
    return int(round(x)) & 0xFFFFFFFF


def encode_keyframe(data_type: int, datatype_hash: int, frame: float,
                    transition: int, kf_dtype: int, subs: List[dict]) -> bytes:
    """将子通道值编码为固定 20 字节关键帧。"""
    if data_type == 3:
        vb = bytes(int(round(subs[i]["value"])) & 0xFF for i in range(4))
        t0, t1 = sub_tangents(subs[0])
        lraw = struct.pack("<f", float(t0))
        rraw = struct.pack("<f", float(t1))
        vraw = vb
    elif datatype_hash in BIG_FLAGS:
        lo, hi = subs[0], subs[1]
        (lo0, lo1), (hi0, hi1) = sub_tangents(lo), sub_tangents(hi)
        v = (int(round(lo["value"])) & 0xFFFF) | ((int(round(hi["value"])) & 0xFFFF) << 16)
        cl = (int(round(lo0)) & 0xFFFF) | ((int(round(hi0)) & 0xFFFF) << 16)
        cr = (int(round(lo1)) & 0xFFFF) | ((int(round(hi1)) & 0xFFFF) << 16)
        vraw = struct.pack("<I", v); lraw = struct.pack("<I", cl); rraw = struct.pack("<I", cr)
    else:
        s = subs[0]
        t0, t1 = sub_tangents(s)
        vraw = struct.pack("<I", _u32_bits(data_type, s["value"]))
        lraw = struct.pack("<I", _u32_bits(data_type, t0))
        rraw = struct.pack("<I", _u32_bits(data_type, t1))
    return (vraw + lraw + rraw + struct.pack("<f", float(frame))
            + struct.pack("<h", int(transition)) + struct.pack("<h", int(kf_dtype)))


def _align16(pos: int) -> int:
    return (pos + 15) & ~15


# 数据模型

@dataclass
class TimlKeyframe:
    """一个关键帧（20 字节定长）。value 按所属 transform 的 dataType 解释。"""
    raw: bytes                 # 原始 20 字节（byte-perfect 兜底 + 未解码字段保真）
    frame_timing: float = 0.0
    transition: int = 0        # 插值类型（见 INTERP_NAMES）
    data_type: int = 0         # 与所属 transform.dataType 一致

    def serialize(self) -> bytes:
        return self.raw


@dataclass
class TimlTransform:
    """一条属性子通道（datatypeHash，如 pos:X / 颜色 / flag）。"""
    datatype_hash: int = 0
    data_type: int = 0
    keyframes: List[TimlKeyframe] = field(default_factory=list)


@dataclass
class TimlType:
    """一个 timelineParameter 通道（影响哪个对象）。"""
    timeline_param_hash: int = 0
    null: int = 0
    transforms: List[TimlTransform] = field(default_factory=list)


@dataclass
class TimlData:
    """一条 animation 的数据（animation0=发射轴 / animation1=更新寿命轴）。"""
    anim_index: int = 0        # 在 dataHeaders 中的槽位（animation id）
    data_ix0: int = 0
    data_ix1: int = 0
    animation_length: float = 0.0
    loop_start_point: float = 0.0
    loop_control: int = 0
    label_hash: int = 0
    types: List[TimlType] = field(default_factory=list)


@dataclass
class Timl:
    """整个 TIML 块；未编辑时以 ``raw`` 原样序列化。"""
    raw: bytes
    header: bytes              # 原始 header，重建时仅更新 count
    count: int = 0
    animations: List[Optional[TimlData]] = field(default_factory=list)  # 空槽为 None。
    dirty: bool = False        # 结构变更后必须重建

    def serialize(self) -> bytes:
        """未编辑时原样输出；编辑后结构化重建。"""
        if not self.dirty:
            return self.raw
        return self._rebuild()

    def _header_bytes(self) -> bytes:
        """返回 count 与 animation 槽位数一致的 header。"""
        return self.header[:24] + struct.pack("<I", self.count & 0xFFFFFFFF)

    def _rebuild(self) -> bytes:
        """按 16 字节对齐布局重建。"""
        datas = [d for d in self.animations if d is not None]
        if self.count == 0 or not datas:
            return self._header_bytes() + b"\x00" * (_align16(_HEADER_SIZE) - _HEADER_SIZE)

        # 第一阶段计算所有结构的偏移
        items = []
        items.append(("hdr", None))
        items.append(("pad", None))
        items.append(("dh", None))
        for d in self.animations:
            if d is None:
                continue
            items.append(("pad", None))
            items.append(("pad", None))
            items.append(("data", d))
            items.append(("pad", None))
            for t in d.types:
                items.append(("type", t))
            items.append(("pad", None))
            for t in d.types:
                for f in t.transforms:
                    items.append(("tf", f))
                items.append(("pad", None))
            for t in d.types:
                for f in t.transforms:
                    items.append(("kfg", f))
                    items.append(("pad", None))
            if items and items[-1][0] == "pad":
                items.pop()

        pos = 0
        offmap = {}
        for kind, obj in items:
            if kind == "pad":
                pos = _align16(pos)
                continue
            if kind == "hdr":
                pos += _HEADER_SIZE
            elif kind == "dh":
                pos += self.count * 8
            elif kind == "data":
                offmap[("data", id(obj))] = pos; pos += _DATA_SIZE
            elif kind == "type":
                offmap[("type", id(obj))] = pos; pos += _TYPE_SIZE
            elif kind == "tf":
                offmap[("tf", id(obj))] = pos; pos += _TRANSFORM_SIZE
            elif kind == "kfg":
                offmap[("kfg", id(obj))] = pos; pos += _KEYFRAME_SIZE * len(obj.keyframes)

        # 第二阶段输出字节并回填偏移
        out = bytearray()
        for kind, obj in items:
            if kind == "pad":
                out += b"\x00" * (_align16(len(out)) - len(out))
            elif kind == "hdr":
                out += self._header_bytes()
            elif kind == "dh":
                for d in self.animations:
                    out += struct.pack("<q", offmap[("data", id(d))] if d is not None else 0)
            elif kind == "data":
                first_type_off = offmap[("type", id(obj.types[0]))] if obj.types else 0
                lbl = obj.label_hash if obj.types else 0
                out += struct.pack("<qqiiffiI", first_type_off, len(obj.types),
                                   obj.data_ix0, obj.data_ix1, obj.animation_length,
                                   obj.loop_start_point, obj.loop_control, lbl)
            elif kind == "type":
                first_tf_off = offmap[("tf", id(obj.transforms[0]))] if obj.transforms else 0
                out += struct.pack("<qqIi", first_tf_off, len(obj.transforms),
                                   obj.timeline_param_hash, obj.null)
            elif kind == "tf":
                kf_off = offmap[("kfg", id(obj))] if obj.keyframes else 0
                out += struct.pack("<qqIi", kf_off, len(obj.keyframes),
                                   obj.datatype_hash, obj.data_type)
            elif kind == "kfg":
                for kf in obj.keyframes:
                    out += kf.serialize()
        return bytes(out)


# 解析

def make_blank_timl() -> bytes:
    """生成最小合法 TIML，供从零创建动画轴和轨道。"""
    header = (
        _MAGIC
        + struct.pack("<3i", 402786304, 402786304, 0)
        + struct.pack("<i", 32)
        + struct.pack("<i", 0)
        + struct.pack("<I", 0)
    )
    return header + b"\x00" * (_align16(_HEADER_SIZE) - _HEADER_SIZE)


def is_timl(data: bytes) -> bool:
    return len(data) >= 4 and data[:4] == _MAGIC


def parse_timl(data: bytes) -> Optional[Timl]:
    """解析完整 TIML 树；非 TIML 返回 ``None``。

    不可达数据不会结构化，但未编辑时仍由 ``raw`` 保留。
    """
    if not is_timl(data):
        return None
    count = struct.unpack_from("<I", data, 24)[0]
    timl = Timl(raw=bytes(data), header=bytes(data[:_HEADER_SIZE]), count=count)
    if count == 0:
        return timl

    dh_base = _align16(_HEADER_SIZE)         # 32
    n = len(data)
    for ai in range(count):
        ptr = dh_base + ai * 8
        if ptr + 8 > n:
            timl.animations.append(None); continue
        data_off = struct.unpack_from("<q", data, ptr)[0]
        if data_off <= 0 or data_off + _DATA_SIZE > n:
            timl.animations.append(None); continue
        timl.animations.append(_parse_data(data, data_off, ai, n))
    return timl


def _parse_data(data: bytes, off: int, anim_index: int, n: int) -> TimlData:
    type_off, type_count, ix0, ix1, animlen, loopstart, loopctrl, lblhash = \
        struct.unpack_from("<qqiiffiI", data, off)
    d = TimlData(anim_index=anim_index, data_ix0=ix0, data_ix1=ix1,
                 animation_length=animlen, loop_start_point=loopstart,
                 loop_control=loopctrl, label_hash=lblhash)
    if type_off <= 0 or type_count <= 0:
        return d
    for ti in range(type_count):
        toff = type_off + ti * _TYPE_SIZE
        if toff + _TYPE_SIZE > n:
            break
        d.types.append(_parse_type(data, toff, n))
    return d


def _parse_type(data: bytes, off: int, n: int) -> TimlType:
    tf_off, tf_count, tlp_hash, null = struct.unpack_from("<qqIi", data, off)
    t = TimlType(timeline_param_hash=tlp_hash, null=null)
    if tf_off <= 0 or tf_count <= 0:
        return t
    for fi in range(tf_count):
        foff = tf_off + fi * _TRANSFORM_SIZE
        if foff + _TRANSFORM_SIZE > n:
            break
        t.transforms.append(_parse_transform(data, foff, n))
    return t


def _parse_transform(data: bytes, off: int, n: int) -> TimlTransform:
    kf_off, kf_count, dt_hash, dtype = struct.unpack_from("<qqIi", data, off)
    f = TimlTransform(datatype_hash=dt_hash, data_type=dtype)
    if kf_off <= 0 or kf_count <= 0:
        return f
    for ki in range(kf_count):
        koff = kf_off + ki * _KEYFRAME_SIZE
        if koff + _KEYFRAME_SIZE > n:
            break
        raw = bytes(data[koff:koff + _KEYFRAME_SIZE])
        ft = struct.unpack_from("<f", raw, 12)[0]
        trans, kdt = struct.unpack_from("<hh", raw, 16)
        f.keyframes.append(TimlKeyframe(raw=raw, frame_timing=ft, transition=trans, data_type=kdt))
    return f


# ─────────────────────────────────────────────────────────────────────────────
# 轨道增删复制（供 Blender 胶水层调用；timl.dirty=True 门控序列化走重建）
# 新建动画轴使用非零元数据标识，避免 dataclass 的零值默认值。
_NEW_LABEL_HASH = {0: jamcrc(b"EFX_EDITOR_NEW_TIMELINE_A0"), 1: jamcrc(b"EFX_EDITOR_NEW_TIMELINE_A1")}
_NEW_DATA_IX = (1, 2)


def make_blank_animdata(slot: int) -> "TimlData":
    """新建一条带非零元数据标识的空动画数据。"""
    lbl = _NEW_LABEL_HASH.get(slot, _NEW_LABEL_HASH[0])
    return TimlData(anim_index=slot, animation_length=30.0,
                    data_ix0=_NEW_DATA_IX[0], data_ix1=_NEW_DATA_IX[1], label_hash=lbl)


def _make_default_keyframes(data_type: int, dt_hash: int,
                            anim_length: float = 30.0,
                            seed=None) -> "List[TimlKeyframe]":
    """生成覆盖动画长度的两个默认关键帧。

    初值优先使用 ``seed``，否则回退至 DT 中性值；TIML 值保持游戏单位。
    """
    frames = [0.0, max(1.0, anim_length)]
    kfs = []
    for fr in frames:
        if data_type == 3:
            if seed is not None:
                try:
                    chans = [max(0, min(255, int(round(float(c))))) for c in seed][:4]
                except (TypeError, ValueError):
                    chans = []
                while len(chans) < 4:
                    chans.append(255)
            else:
                chans = [255, 255, 255, 255]
            subs = [make_sub(chans[i], 0.0, 0.0) for i in range(4)]
        else:
            if seed is not None:
                try:
                    v = float(seed[0] if isinstance(seed, (list, tuple)) else seed)
                except (TypeError, ValueError, IndexError):
                    v = dt_neutral_value(dt_hash)
            else:
                v = dt_neutral_value(dt_hash)
            subs = [make_sub(v, 0.0, 0.0)]
        raw = encode_keyframe(data_type, dt_hash, fr, 2, data_type, subs)
        kfs.append(TimlKeyframe(raw=raw, frame_timing=fr, transition=2, data_type=data_type))
    return kfs


def add_transform(timl: "Timl", slot: int, tlp_hash: int,
                  dt_hash: int, data_type: int, seed=None) -> bool:
    """新增一条 transform；已有同 hash 的通道时返回 ``False``。

    ``seed`` 用作初始关键帧值，缺失时回退至 DT 中性值。
    """
    tlp_hash &= 0xFFFFFFFF
    dt_hash &= 0xFFFFFFFF
    # 补齐动画槽位
    while len(timl.animations) <= slot:
        timl.animations.append(None)
    timl.count = max(timl.count, slot + 1)

    if timl.animations[slot] is None:
        timl.animations[slot] = make_blank_animdata(slot)
    anim = timl.animations[slot]
    anim.anim_index = slot

    tlp = None
    for t in anim.types:
        if (t.timeline_param_hash & 0xFFFFFFFF) == tlp_hash:
            tlp = t
            break
    if tlp is None:
        tlp = TimlType(timeline_param_hash=tlp_hash)
        anim.types.append(tlp)

    for tf in tlp.transforms:
        if (tf.datatype_hash & 0xFFFFFFFF) == dt_hash:
            return False

    anim_len = anim.animation_length if anim.animation_length > 0.0 else 30.0
    kfs = _make_default_keyframes(data_type, dt_hash, anim_len, seed=seed)
    tlp.transforms.append(TimlTransform(datatype_hash=dt_hash, data_type=data_type, keyframes=kfs))
    timl.dirty = True
    return True


def delete_transform(timl: "Timl", slot: int, tlp_hash: int, dt_hash: int) -> bool:
    """删除 slot 轴的 (tlp_hash, dt_hash) 通道；空 type 一并删除。返回是否找到。"""
    tlp_hash &= 0xFFFFFFFF
    dt_hash &= 0xFFFFFFFF
    if slot >= len(timl.animations) or timl.animations[slot] is None:
        return False
    anim = timl.animations[slot]
    for t in list(anim.types):
        if (t.timeline_param_hash & 0xFFFFFFFF) != tlp_hash:
            continue
        before = len(t.transforms)
        t.transforms = [tf for tf in t.transforms
                        if (tf.datatype_hash & 0xFFFFFFFF) != dt_hash]
        if len(t.transforms) < before:
            if not t.transforms:
                anim.types.remove(t)
            timl.dirty = True
            return True
    return False


def copy_transform(timl: "Timl", src_slot: int, dst_slot: int,
                   tlp_hash: int, dt_hash: int) -> bool:
    """把 src_slot 的 (tlp_hash, dt_hash) 通道（含关键帧）复制到 dst_slot；已有则覆盖。
    src 不存在返回 False。"""
    import copy as _copy
    tlp_hash &= 0xFFFFFFFF
    dt_hash &= 0xFFFFFFFF
    if src_slot >= len(timl.animations) or timl.animations[src_slot] is None:
        return False
    src_tf = None
    for t in timl.animations[src_slot].types:
        if (t.timeline_param_hash & 0xFFFFFFFF) != tlp_hash:
            continue
        for tf in t.transforms:
            if (tf.datatype_hash & 0xFFFFFFFF) == dt_hash:
                src_tf = tf
                break
        if src_tf is not None:
            break
    if src_tf is None:
        return False

    # 先移除目标中已有的同名通道
    delete_transform(timl, dst_slot, tlp_hash, dt_hash)

    # 补齐动画槽位
    while len(timl.animations) <= dst_slot:
        timl.animations.append(None)
    timl.count = max(timl.count, dst_slot + 1)
    if timl.animations[dst_slot] is None:
        timl.animations[dst_slot] = make_blank_animdata(dst_slot)
    dst_anim = timl.animations[dst_slot]
    dst_anim.anim_index = dst_slot

    dst_t = None
    for t in dst_anim.types:
        if (t.timeline_param_hash & 0xFFFFFFFF) == tlp_hash:
            dst_t = t
            break
    if dst_t is None:
        dst_t = TimlType(timeline_param_hash=tlp_hash)
        dst_anim.types.append(dst_t)

    dst_t.transforms.append(_copy.deepcopy(src_tf))
    timl.dirty = True
    return True
