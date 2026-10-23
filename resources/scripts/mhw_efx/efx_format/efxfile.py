"""MHW .efx 的保真解析与序列化。

维护约束：
- 文件按 Header、标签、Play、Extern、Main、Subselect、End 的顺序读取。
- 已知属性以 schema 或专用 walker 确定边界；未知属性仅作 opaque 字节保留。
- 所有 BT 标量为小端，且 ``long`` 与 ``int`` 均为 4 字节。
"""

from __future__ import annotations
import math
import struct
from dataclasses import dataclass, field
from typing import List, Optional

from .hashes import (
    ROOT_MARKER, PLAYEFX, PLAYEMITTER, ATTR_HASHES,
    HASH_TO_NAME, TIML,
    TRANSFORM3D, PARENTOPTIONS, SPAWN, LIFE, EMITTERSHAPE3D, VELOCITY3D,
    FADEBYDEPTH, BILLBOARD3D, SCALEANIM, UVSEQUENCE, ALPHACORRECTION,
    SHADERSETTINGS, RGBFIRE, MESH, ROTATEANIM, PLEMISSIVE, GUIDE, LIGHTNING,
    PARENTEMISSIVE, PTCOLLISION, PLSNOW, PTBEHAVIOR, PLANE, RGBWATER,
    TURBULENCE, FADEBYEMITTERANGLE, RIBBON, NOISE, UVCONTROL, FADEBYANGLE,
    EMITTERBOUNDARY, PTLIFE, STRAINRIBBON, SCREENSPACECOLLISION, RAYCAST,
    EXTERNREFERENCE, FAKEPLANE, DUMMY, RANDOMFIX, TRANSFORM2D, BILLBOARD2D,
    BLINK, LUMINANCEBLEED, EMITTERSHAPE2D, VELOCITY2D, REFRACTION, MASTERONLY,
    TUBELIGHT, SHOVEL, LAYOUT, FAKEDOF, REPEATAREA, LINKPARTSVISIBLE, PTTRIGGER,
    PATHCHAIN, HOMING, EMITTERSHAPEMESH, SPAWNBYANGLE, CHECKPUREATTRIBUTE,
    TONEMAPFILTER, COLORCORRECTFILTER, SPAWNBYOCCLUSION, FADEBYOCCLUSION,
    PARENTSNOW, OTOMOSNOW, PARENTMATERIAL, RIBBONBLADE, MATERIAL,
)

# 已知属性块的边界计算

def _xyz_size(xyz_type: int) -> int:
    """返回指定 XYZ 类型的结构体字节数。"""
    if xyz_type == 0:
        return 24  # float fixed_x, random_x, fixed_y, random_y, fixed_z, random_z
    elif xyz_type == 1:
        return 12  # int x, y, z
    elif xyz_type == 2:
        return 4   # ubyte x, y, z, pad
    elif xyz_type == 3:
        return 12  # float x, y, z
    return 0

def _known_attr_size(data: bytes, pos: int, type_hash: int) -> Optional[int]:
    """返回已知属性块（含 type hash）的总字节数。

    无法可靠定界时返回 ``None``，由调用方执行 opaque 回退。
    """
    def rd_i(offset: int) -> int:
        return struct.unpack_from('<i', data, pos + offset)[0]

    h = type_hash

    # 固定大小 schema
    from .structs import ATTR_SCHEMA_MAP
    _entry = ATTR_SCHEMA_MAP.get(h)
    if _entry is not None and _entry[1] is not None:
        return 4 + _entry[1]

    # 自定义 codec 负责带显式长度或 NUL 结尾字符串的可变块。
    from .structs import custom_on_disk_size, custom_nullstr_size
    _s = custom_on_disk_size(h, data, pos)
    if _s is None:
        _s = custom_nullstr_size(h, data, pos)
    if _s is not None:
        return _s

    # PTBEHAVIOR 通过内部长度、参数数量和参数类型遍历可变负载。
    if h == PTBEHAVIOR:
        behav_type_len = rd_i(4 + 4)  # type(4) + unkn0(4) + behav_type_len(4) = at pos+8
        para_count = rd_i(4 + 8)      # at pos+12
        if behav_type_len < 0 or behav_type_len > 200 or para_count < 0 or para_count > 200:
            return None
        p = 4 + 12 + behav_type_len
        for _ in range(para_count):
            t = rd_i(p + 8)
            base = 12
            if t == 0x03:
                extra = 4
            elif t == 0x05:
                extra = 2
            elif t == 0x06:
                extra = 4
            elif t == 0x0C:
                extra = 4
            elif t == 0x0F:
                extra = 4
            elif t == 0x14:
                extra = 12
            elif t == 0x15:
                extra = 16
            elif t in (0x36, 0x37):
                extra = 8
            elif t == 0x40:
                extra = 8
            elif t == 0x80:
                path_len_val = rd_i(p + 16)
                extra = 8 + path_len_val
            else:
                extra = 4
            p += base + extra
        return p

    # MATERIAL 通过 block/set 计数及 set 类型遍历可变负载
    if h == MATERIAL:
        block_count = rd_i(12)
        p = 16
        for _ in range(block_count):
            set_count = rd_i(p + 12)
            p += 16
            for _ in range(set_count):
                type_ = rd_i(p + 12)
                p += 16
                if type_ == 0x80:
                    path_len_val = rd_i(p + 8)
                    p += 4 + 4 + 4 + path_len_val
                elif type_ == 0x06:
                    p += 12
                elif type_ in (0x03, 0x0A, 0x0C):
                    p += 12
                elif type_ == 0x15:
                    p += 24
                else:
                    p += 4
        return p

    # LAYOUT 的可变 LayoutBank 块由专用 walker 定界。
    if h == LAYOUT:
        from .structs import _walk_layoutbank_block
        try:
            p = pos + 4 + 8 + 16
            end = _walk_layoutbank_block(data, p)
        except (struct.error, ValueError, IndexError):
            return None
        return end - pos

    return None

# 数据模型

@dataclass
class EFXHeader:
    """已解析的文件头。"""
    signature: bytes        # b"EFX\x00"
    version: int
    constant: tuple
    efxr: bytes             # b"efxr"
    is_3d: int              # 必须与 Entry 类型一致
    unkn1: int
    count_body: int
    label_size: int
    count_play: int
    count_extern: int
    count_subselect: int
    subselect_size: int
    count_eof: int
    double_buffer: int

    STRUCT = struct.Struct('<4s i 5i 4s 10I')
    SIZE = 72

    def serialize(self) -> bytes:
        return self.STRUCT.pack(
            self.signature, self.version,
            *self.constant,
            self.efxr,
            self.is_3d, self.unkn1,
            self.count_body, self.label_size,
            self.count_play, self.count_extern,
            self.count_subselect, self.subselect_size,
            self.count_eof, self.double_buffer,
        )

def recompute_double_buffer(data: bytes, factor: float = 2.0):
    """按文件长度重算 header 的 doubleBuffer，返回 ``(新字节, 原值, 新值)``。

    新值取原值与「文件长度 × factor，向上取整到 16 字节」中的较大者，只增不减。
    """
    fields = list(EFXHeader.STRUCT.unpack_from(data, 0))
    old = fields[-1]
    new = max(old, (math.ceil(factor * len(data)) + 15) // 16 * 16)
    if new == old:
        return data, old, new
    fields[-1] = new
    return EFXHeader.STRUCT.pack(*fields) + data[EFXHeader.SIZE:], old, new


@dataclass
class ActionEntry:
    """Action 内的 PlayEFX 或 PlayEmitter 条目。"""
    type_hash: int
    raw: bytes  # 不含 4 字节 type hash

    def serialize(self) -> bytes:
        return struct.pack('<I', self.type_hash) + self.raw

@dataclass
class ActionData:
    """Play 段中的一个 Action。"""
    play_type: int
    entries: List[ActionEntry]

    def serialize(self) -> bytes:
        out = struct.pack('<Ii', self.play_type, len(self.entries))
        for e in self.entries:
            out += e.serialize()
        return out

@dataclass
class ExternDataItem:
    """ExternAttribute 中的一个数据项。"""
    type_hash: int
    unkn: int
    attr_count: int
    data_bytes: bytes   # 按类型解释或 opaque 保留

    def serialize(self) -> bytes:
        return struct.pack('<Iii', self.type_hash, self.unkn, self.attr_count) + self.data_bytes

@dataclass
class ExternAttribute:
    """Extern 段的一个属性条目。"""
    attr_type: int
    null0: int
    null1: int
    items: List[ExternDataItem]

    def serialize(self) -> bytes:
        out = struct.pack('<IiIi', self.attr_type, self.null0, len(self.items), self.null1)
        for item in self.items:
            out += item.serialize()
        return out

@dataclass
class AttrBlock:
    """Main 条目中的一个属性块。"""
    type_hash: int
    data_bytes: bytes   # 不含 4 字节 type hash

    @property
    def name(self) -> str:
        return HASH_TO_NAME.get(self.type_hash, f'0x{self.type_hash:08X}')

    def serialize(self) -> bytes:
        return struct.pack('<I', self.type_hash) + self.data_bytes

    def decode(self) -> Optional[dict]:
        """用已注册的 schema 解码；无 schema 时返回 ``None`` 保持 opaque。"""
        from .structs import ATTR_SCHEMA_MAP, ATTR_CUSTOM_CODEC, unpack
        entry = ATTR_SCHEMA_MAP.get(self.type_hash)
        if entry is None:
            return None
        schema, expected_size = entry

        # 可变属性由专用 codec 解码，且必须恰好消费全部字节
        if schema == '_custom':
            custom = ATTR_CUSTOM_CODEC.get(self.type_hash)
            if custom is None:
                return None
            unpack_fn, _ = custom
            values, consumed = unpack_fn(self.data_bytes, 0)
            if consumed != len(self.data_bytes):
                raise ValueError(
                    f'AttrBlock.decode (custom): {self.name} '
                    f'consumed {consumed} bytes but data_bytes is {len(self.data_bytes)} bytes'
                )
            return values

        if len(self.data_bytes) != expected_size:
            raise ValueError(
                f'AttrBlock.decode: {self.name} '
                f'data_bytes length {len(self.data_bytes)} '
                f'!= expected {expected_size}'
            )
        values, consumed = unpack(schema, self.data_bytes, 0)
        if consumed != expected_size:
            raise ValueError(
                f'AttrBlock.decode: {self.name} '
                f'schema consumed {consumed} bytes but data_bytes is {expected_size} bytes'
            )
        return values

    def encode(self, values: dict) -> None:
        """将字段值编码回本块，且不得改变原有字节长度。"""
        from .structs import ATTR_SCHEMA_MAP, ATTR_CUSTOM_CODEC, pack
        entry = ATTR_SCHEMA_MAP.get(self.type_hash)
        if entry is None:
            raise ValueError(
                f'AttrBlock.encode: no schema for {self.name} '
                f'(0x{self.type_hash:08X})'
            )
        schema, expected_size = entry

        if schema == '_custom':
            custom = ATTR_CUSTOM_CODEC.get(self.type_hash)
            if custom is None:
                raise ValueError(
                    f'AttrBlock.encode: no custom codec for {self.name} '
                    f'(0x{self.type_hash:08X})'
                )
            _, pack_fn = custom
            encoded = pack_fn(values)
            if len(encoded) != len(self.data_bytes):
                raise ValueError(
                    f'AttrBlock.encode (custom): {self.name} '
                    f'encoded {len(encoded)} bytes but original data_bytes is '
                    f'{len(self.data_bytes)} bytes'
                )
            self.data_bytes = encoded
            return

        encoded = pack(schema, values)
        if len(encoded) != expected_size:
            raise ValueError(
                f'AttrBlock.encode: {self.name} '
                f'encoded {len(encoded)} bytes but expected {expected_size}'
            )
        self.data_bytes = encoded

@dataclass
class EntryData:
    """非 Root 的 Main 条目主体。"""
    body_type: int
    unkn0: int
    attr_count: int
    null: int
    timl_length: int
    timl_bytes: bytes       # opaque 保留
    attr_blocks: List[AttrBlock]

    def serialize(self) -> bytes:
        # attr_count 可为负值；此时不读取属性，但必须保留原值
        count = self.attr_count if (not self.attr_blocks and self.attr_count != 0) else len(self.attr_blocks)
        head = struct.pack('<IiiiI', self.body_type, self.unkn0,
                           count, self.null, self.timl_length)
        out = head + self.timl_bytes
        for blk in self.attr_blocks:
            out += blk.serialize()
        return out

@dataclass
class RootUnitBoundary:
    """Root 中可编辑的 UnitBoundary 子条目。"""
    ints: tuple
    floats: tuple

    def serialize(self) -> bytes:
        return (struct.pack('<i', RootBody.UNITBOUNDARY)
                + struct.pack('<2i', *self.ints)
                + struct.pack('<8f', *self.floats))

@dataclass
class RootOpaqueEntry:
    """无法结构化的 Root 子条目。"""
    raw: bytes   # 含前导 type

    def serialize(self) -> bytes:
        return self.raw

@dataclass
class RootBody:
    """Root 主体。

    无法结构化的主体以 ``raw`` 原样序列化。
    """
    # Root 子条目类型
    UNITBOUNDARY = 1413509420
    RENDERTARGET = 2083659062
    LAYOUTBANK   = 2050487542

    root_type: int = ROOT_MARKER
    const0: int = 1
    const1: int = 0
    entries: list = field(default_factory=list)
    raw: bytes = None   # 无法结构化时的保真回退

    def serialize(self) -> bytes:
        if self.raw is not None:
            return self.raw
        out = struct.pack('<iiii', self.root_type, self.const0,
                          len(self.entries), self.const1)
        for e in self.entries:
            out += e.serialize()
        return out


# Root 子条目类型供 Blender 导出端过滤误放的属性对象
ROOT_SUBENTRY_HASHES = frozenset({
    RootBody.UNITBOUNDARY, RootBody.RENDERTARGET, RootBody.LAYOUTBANK,
})

@dataclass
class SubselectTable:
    """Subselect 段的一张表。"""
    table_type: int
    unkn0: tuple
    entries: List[int]

    def serialize(self) -> bytes:
        out = struct.pack('<I3I', self.table_type, *self.unkn0)
        out += struct.pack('<i', len(self.entries))
        for e in self.entries:
            out += struct.pack('<i', e)
        return out

# 主 EFX 解析与序列化

class EFXFile:
    """MHW ``.efx`` 的解析与保真序列化模型。"""

    def __init__(self):
        self.header: EFXHeader = None
        self.label_bytes: bytes = b''         # 原始标签表
        self.labels: List[str] = []
        self.play: List[ActionData] = []
        self.extern: List[ExternAttribute] = []
        self.main: List = []
        self.subselect: List[SubselectTable] = []
        self.eof_ints: List[int] = []
        self.eof_tail: bytes = b''     # EOF 后的原始尾部字节

        # 无法划定 Main 边界时，保留 Main 起至 EOF 的原始字节
        # 此回退路径不支持逐块编辑
        self.main_opaque: bool = False
        self.opaque_main_tail: bytes = b''

    @classmethod
    def parse(cls, data: bytes) -> 'EFXFile':
        obj = cls()
        pos = 0

        raw = EFXHeader.STRUCT.unpack_from(data, pos)
        pos += EFXHeader.SIZE
        sig, ver, c0, c1, c2, c3, c4, efxr, u0, u1, cb, ls, cp, ce, cs, ss, ceof, db = raw
        obj.header = EFXHeader(
            signature=sig, version=ver,
            constant=(c0, c1, c2, c3, c4),
            efxr=efxr,
            is_3d=u0, unkn1=u1,
            count_body=cb, label_size=ls,
            count_play=cp, count_extern=ce,
            count_subselect=cs, subselect_size=ss,
            count_eof=ceof, double_buffer=db,
        )
        hdr = obj.header

        obj.label_bytes = data[pos:pos + hdr.label_size]
        obj.labels = [
            s.decode('utf-8', errors='replace')
            for s in obj.label_bytes.rstrip(b'\x00').split(b'\x00')
            if s
        ]
        pos += hdr.label_size

        obj.play, pos = cls._parse_play(data, pos, hdr.count_play)

        obj.extern, pos = cls._parse_extern(data, pos, hdr.count_extern)

        main_start = pos
        try:
            obj.main, pos = cls._parse_main(data, pos, hdr.count_body)

            if hdr.subselect_size > 0:
                obj.subselect, pos = cls._parse_subselect(data, pos, hdr.count_subselect)
            else:
                obj.subselect = []

            obj.eof_ints = list(struct.unpack_from(f'<{hdr.count_eof}I', data, pos))
            pos += hdr.count_eof * 4

            # EOF 后的尾部字节必须原样保留
            obj.eof_tail = data[pos:]
        except Exception:
            # Main 解析失败时，整个后续分段作为 opaque 数据原样输出
            obj.main = []
            obj.subselect = []
            obj.eof_ints = []
            obj.eof_tail = b''
            obj.main_opaque = True
            obj.opaque_main_tail = data[main_start:]

        return obj

    def serialize(self) -> bytes:
        out = self.header.serialize()
        out += self.label_bytes

        for pd in self.play:
            out += pd.serialize()

        for ea in self.extern:
            out += ea.serialize()

        # Main 无法解析时直接输出保留的原始尾段
        if self.main_opaque:
            out += self.opaque_main_tail
            return out

        for body in self.main:
            out += body.serialize()

        for tbl in self.subselect:
            out += tbl.serialize()

        for v in self.eof_ints:
            out += struct.pack('<I', v)

        out += self.eof_tail

        return out

    @staticmethod
    def _parse_play(data: bytes, pos: int, count: int):
        """解析 Play 段。"""
        results = []
        for _ in range(count):
            play_type = struct.unpack_from('<I', data, pos)[0]
            entry_count = struct.unpack_from('<i', data, pos + 4)[0]
            pos += 8
            entries = []
            for _ in range(entry_count):
                type_hash = struct.unpack_from('<I', data, pos)[0]
                pos += 4
                if type_hash == PLAYEFX:
                    # PlayEFX 以 path_len 定界其路径尾部
                    path_len = struct.unpack_from('<i', data, pos + 4)[0]
                    entry_size = 64 + path_len
                    entry_raw = data[pos:pos + entry_size]
                    pos += entry_size
                elif type_hash == PLAYEMITTER:
                    # PlayEmitter 以 target_count 定界目标数组
                    target_count = struct.unpack_from('<i', data, pos + 52)[0]
                    entry_size = 56 + 4 * target_count
                    entry_raw = data[pos:pos + entry_size]
                    pos += entry_size
                else:
                    raise ValueError(
                        f'Unknown Play typeHash 0x{type_hash:08X} at offset {pos-4}'
                    )
                entries.append(ActionEntry(type_hash=type_hash, raw=entry_raw))
            results.append(ActionData(play_type=play_type, entries=entries))
        return results, pos

    @staticmethod
    def _parse_extern(data: bytes, pos: int, count: int):
        """解析 Extern 段。"""
        results = []
        for _ in range(count):
            attr_type = struct.unpack_from('<I', data, pos)[0]
            null0 = struct.unpack_from('<i', data, pos + 4)[0]
            item_count = struct.unpack_from('<i', data, pos + 8)[0]
            null1 = struct.unpack_from('<i', data, pos + 12)[0]
            pos += 16

            items = []
            for _ in range(item_count):
                t = struct.unpack_from('<I', data, pos)[0]
                unkn = struct.unpack_from('<i', data, pos + 4)[0]
                attri_count = struct.unpack_from('<i', data, pos + 8)[0]
                pos += 12
                item_size = EFXFile._extern_data_size(t, attri_count, data, pos)
                item_bytes = data[pos:pos + item_size]
                pos += item_size
                items.append(ExternDataItem(
                    type_hash=t, unkn=unkn, attr_count=attri_count, data_bytes=item_bytes
                ))
            results.append(ExternAttribute(
                attr_type=attr_type, null0=null0, null1=null1, items=items
            ))
        return results, pos

    @staticmethod
    def _efx_behavior_size(data: bytes, pos: int) -> int:
        """返回一个 EFX_Behavior 的字节数。"""
        behav_type_len = struct.unpack_from('<i', data, pos + 4)[0]
        para_count = struct.unpack_from('<i', data, pos + 8)[0]
        p = pos + 12 + behav_type_len
        for _ in range(para_count):
            t = struct.unpack_from('<i', data, p + 8)[0]
            base = 12
            if t == 0x03:
                extra = 4
            elif t == 0x05:
                extra = 2
            elif t == 0x06:
                extra = 4
            elif t == 0x0C:
                extra = 4
            elif t == 0x0F:
                extra = 4
            elif t == 0x14:
                extra = 12
            elif t == 0x15:
                extra = 16
            elif t in (0x36, 0x37):
                extra = 8
            elif t == 0x40:
                extra = 8
            elif t == 0x80:
                path_len_val = struct.unpack_from('<i', data, p + 12 + 4)[0]
                extra = 8 + path_len_val
            else:
                extra = 4
            p += base + extra
        return p - pos

    @staticmethod
    def _extern_data_size(type_hash: int, attri_count: int,
                          data: bytes = b'', pos: int = 0) -> int:
        """返回 Extern_Data 头之后的数据长度。"""
        # 每项固定字节数
        FIXED = {
            500644368: 228,   # EXTERNTRANSFORM3D
            351887441: 108,   # EXTERNVELOCITY3D
            786529163: 76,    # EXTERNSCALEANIM
            2069124466: 112,  # EXTERNRGBFIRE
            28559457: 72,     # EXTERNSPAWN
            1880343637: 88,   # EXTERNEMITTERSHAPE3D
            725249589: 76,    # EXTERNPLEMISSIVE
            1338793878: 48,   # EXTERNLIFE
            283026906: 84,    # EXTERNPLSNOW
            705591903: 72,    # EXTERNPARENTEMISSIVE
            1879331968: 80,   # EXTERNROTATEANIM


            # 以下类型从未出现过，仅通过类型特征外推
            1415485201: 40,   # EXTERNFADEBYANGLE
            779931249:  20,   # EXTERNFADEBYDEPTH
            1243935109: 236,  # EXTERNUVCONTROL
            766474541:  112,  # EXTERNGUIDE
            74649634:   80,   # EXTERNPARENTSNOW
            1181241355: 84,   # EXTERNOTOMOSNOW
        }
        if type_hash in FIXED:
            return attri_count * FIXED[type_hash]

        # EXTERNMESH 每项含两个 NUL 结尾字符串
        if type_hash == 1850314036:  # EXTERNMESH
            p = pos
            for _ in range(attri_count):
                path1_start = p + 175
                null1 = data.index(b'\x00', path1_start)
                null2 = data.index(b'\x00', null1 + 1)
                p = null2 + 1
            return p - pos

        # EXTERNTYPERIBBON 每项以 NUL 结尾路径定界
        if type_hash == 0x320E3177:  # EXTERNTYPERIBBON
            p = pos
            for _ in range(attri_count):
                path_start = p + 360
                null = data.index(b'\x00', path_start)
                p = null + 1
            return p - pos

        # EXTERNTYPEPLANE 每项以显式 path_len 定界
        if type_hash == 0x3002E4CE:  # EXTERNTYPEPLANE
            p = pos
            for _ in range(attri_count):
                (path_len,) = struct.unpack_from('<i', data, p + 104)
                p += 156 + path_len
            return p - pos

        # 这些 Extern 项沿用对应主属性 codec，以内嵌可变字段定界
        _MAIN_CODEC_EXTERN = {
            0x7CFF28CC: "unpack_uvsequence",    # EXTERNUVSEQUENCE
            0x295D488A: "unpack_billboard3d",   # EXTERNBILLBOARD3D
            0x1CC2BE3A: "unpack_rgbwater",      # EXTERNRGBWATER
            167781675:  "unpack_strainribbon",  # EXTERNSTRAINRIBBON（待确认）
            777721399:  "unpack_turbulence",    # EXTERNTURBULENCE（待确认）
        }
        if type_hash in _MAIN_CODEC_EXTERN:
            from . import structs as _structs
            _un = getattr(_structs, _MAIN_CODEC_EXTERN[type_hash])
            p = pos
            for _ in range(attri_count):
                _vals, p = _un(data, p)
            return p - pos

        # EXTERNPTBEHAVIOR 与主属性使用相同的可变行为结构
        if type_hash == 0x5FFC3E36:  # EXTERNPTBEHAVIOR
            p = pos
            for _ in range(attri_count):
                p += EFXFile._efx_behavior_size(data, p)
            return p - pos

        # 未实现的 Extern 类型不能猜测其边界（但是可以试试）
        raise ValueError(
            f'Cannot compute size for unknown Extern_Data type 0x{type_hash:08X} '
            f'({HASH_TO_NAME.get(type_hash, "UNKNOWN")}). '
            f'attri_count={attri_count}. '
            f'This type needs manual size implementation.'
        )

    @staticmethod
    def _parse_main(data: bytes, pos: int, count: int):
        """解析 Main 段。"""
        results = []
        for body_idx in range(count):
            if pos + 4 > len(data):
                raise ValueError(f'EOF reached while reading Main body {body_idx} at pos {pos}')
            first_int = struct.unpack_from('<I', data, pos)[0]
            if first_int == ROOT_MARKER:
                body, pos = EFXFile._parse_root_body(data, pos)
            else:
                body, pos = EFXFile._parse_main_data_body(data, pos)
            results.append(body)
        return results, pos

    @staticmethod
    def _parse_root_body(data: bytes, start_pos: int):
        """解析 Root 主体。

        仅 UnitBoundary 结构化；其余已识别子项以原始字节保留。
        """
        pos = start_pos
        root_type = struct.unpack_from('<I', data, pos)[0]
        const0 = struct.unpack_from('<i', data, pos + 4)[0]
        count = struct.unpack_from('<i', data, pos + 8)[0]
        const1 = struct.unpack_from('<i', data, pos + 12)[0]
        pos += 16

        UNITBOUNDARY = RootBody.UNITBOUNDARY
        RENDERTARGET = RootBody.RENDERTARGET
        LAYOUTBANK   = RootBody.LAYOUTBANK

        entries = []
        for i in range(count):
            sub_type = struct.unpack_from('<i', data, pos)[0]
            ent_start = pos
            if sub_type == UNITBOUNDARY:
                ints = struct.unpack_from('<2i', data, pos + 4)
                floats = struct.unpack_from('<8f', data, pos + 12)
                entries.append(RootUnitBoundary(ints=ints, floats=floats))
                pos += 44
            elif sub_type == RENDERTARGET:
                # RenderTarget 有固定数量的路径，逐项以 path_len 定界
                pos += 4 + 4
                for _ in range(6):
                    p_len = struct.unpack_from('<i', data, pos)[0]
                    pos += 4 + p_len
                pos += 4 + 24 + 36
                entries.append(RootOpaqueEntry(raw=data[ent_start:pos]))
            elif sub_type == LAYOUTBANK:
                pos = EFXFile._parse_layout_bank(data, pos)
                entries.append(RootOpaqueEntry(raw=data[ent_start:pos]))
            else:
                raise ValueError(
                    f'Unknown Root sub-entry type 0x{sub_type:08X} at pos {pos}'
                )

        return RootBody(root_type=root_type, const0=const0, const1=const1,
                        entries=entries), pos

    @staticmethod
    def _parse_layout_bank(data: bytes, pos: int) -> int:
        """遍历 LayoutBank 并返回结束位置。"""
        from .structs import _walk_layoutbank_block
        block_count = struct.unpack_from('<i', data, pos + 8)[0]
        pos += 12

        for _ in range(block_count):
            pos = _walk_layoutbank_block(data, pos)
        return pos

    @staticmethod
    def _parse_main_data_body(data: bytes, start_pos: int):
        """解析 Main 条目主体。"""
        pos = start_pos
        body_type = struct.unpack_from('<I', data, pos)[0]

        unkn0 = struct.unpack_from('<i', data, pos + 4)[0]
        attr_count = struct.unpack_from('<i', data, pos + 8)[0]
        null = struct.unpack_from('<i', data, pos + 12)[0]
        timl_length = struct.unpack_from('<i', data, pos + 16)[0]
        pos += 20


        timl_bytes = data[pos:pos + timl_length]
        pos += timl_length

        attr_blocks, pos = EFXFile._parse_attr_blocks(data, pos, attr_count)

        return EntryData(
            body_type=body_type, unkn0=unkn0, attr_count=attr_count,
            null=null, timl_length=timl_length,
            timl_bytes=timl_bytes, attr_blocks=attr_blocks,
        ), pos

    @staticmethod
    def _parse_attr_blocks(data: bytes, pos: int, attr_count: int):
        """解析属性块；未知类型通过前向扫描估计边界。"""
        blocks = []
        for blk_idx in range(attr_count):
            if pos + 4 > len(data):
                raise ValueError(
                    f'EOF reached reading attr block {blk_idx}/{attr_count} at pos {pos}'
                )
            type_hash = struct.unpack_from('<I', data, pos)[0]
            block_size = _known_attr_size(data, pos, type_hash)

            if block_size is not None:
                block_data = data[pos + 4:pos + block_size]
                blocks.append(AttrBlock(type_hash=type_hash, data_bytes=block_data))
                pos += block_size
            else:
                # 未知块依赖后续已知 hash 定界
                scan_start = pos + 4
                remaining_blocks = attr_count - blk_idx - 1
                end_pos = EFXFile._forward_scan(
                    data, scan_start, remaining_blocks
                )
                block_data = data[pos + 4:end_pos]
                blocks.append(AttrBlock(type_hash=type_hash, data_bytes=block_data))
                pos = end_pos

        return blocks, pos

    @staticmethod
    def _forward_scan(data: bytes, scan_start: int, remaining_blocks: int) -> int:
        """以 4 字节对齐的已知 hash 估计未知属性的结束位置。

        该启发式没有显式的 body 结束标记；无法定界时会延伸至 EOF，并由上层 opaque
        回退保证保真。
        """
        if remaining_blocks > 0:
            i = scan_start
            while i + 4 <= len(data):
                candidate = struct.unpack_from('<I', data, i)[0]
                if candidate in ATTR_HASHES:
                    return i
                i += 4
            return len(data)
        else:
            # 最后一个未知块同样只能寻找后续已知 hash 或 Root 标记。
            i = scan_start
            while i + 4 <= len(data):
                candidate = struct.unpack_from('<I', data, i)[0]
                if candidate in ATTR_HASHES or candidate == ROOT_MARKER:
                    return i
                i += 4
            return len(data)

    @staticmethod
    def _parse_subselect(data: bytes, pos: int, count: int):
        """解析 Subselect 段。"""
        results = []
        for _ in range(count):
            tbl_type = struct.unpack_from('<I', data, pos)[0]
            unkn0 = struct.unpack_from('<3I', data, pos + 4)
            entry_count = struct.unpack_from('<i', data, pos + 16)[0]
            pos += 20
            entries = list(struct.unpack_from(f'<{entry_count}i', data, pos))
            pos += 4 * entry_count
            results.append(SubselectTable(table_type=tbl_type, unkn0=unkn0, entries=entries))
        return results, pos
