"""EFX schema codec 的组装层与兼容导入门面。

维护约束：
- ``schema/`` 定义具体 codec 与 schema；本模块负责 re-export、类型分派和字段注册。
- 固定长度类型在 ``ATTR_SCHEMA_MAP`` 登记 schema 与数据长度；可变类型必须以
  ``'_custom'`` 哨兵登记，并在 ``ATTR_CUSTOM_CODEC`` 提供成对 codec。
- Extern 覆盖版复用主属性字段元数据时，必须额外注册到其 Extern hash。
"""

from __future__ import annotations
import struct
from typing import Any, Dict, List, Tuple

from .schema.fields_model import (
    Attribute, EnumDef, BitDef,
    Int, UInt, Short, UShort, Byte, SByte, Float, Int64, UInt64,
    Enum, EnumVec3, Bool, Bitmask, Raw, register, register_alias, ATTR_REGISTRY,
)

from .schema.enums import (
    ENUM_SHAPE_TYPE3D, ENUM_SHAPE_TYPE2D, ENUM_COLLISION_PHYSICS, ENUM_PTLIFE_STATUS,
    ENUM_RAYCAST_DIR, ENUM_HOMING_TARGET, ENUM_HOMING_FORCEFIELD, ENUM_HOMING_VANISH,
    ENUM_PARTICLE_LIGHTING, ENUM_DRAW_MODE, ENUM_DRAW_TARGET, ENUM_BLEND_STATE, BITS_ROTATION_MODE,
    ENUM_TRACKING_POS, ENUM_TRACKING_ANGLE,
    BITS_ENABLE_VELOCITY, BITS_SPIN_AXIS, BITS_RANDOMFIX_TABLE,
    _AXIS_DIRECTION6, _ROT_ORDER6, _VELOCITY_TYPE,
)

# 基础 codec。

from .schema.codec import (
    _SCALAR_SIZE, _XYZ_FMT, _unpack_xyz, _pack_xyz, _xyz_size,
    _EPVCSLOT_FIELDS, _EPVCSLOT_SIZE, _unpack_epvcolorslot, _pack_epvcolorslot,
    unpack, pack, _schema_size,
)


from .schema.attributes import *  # noqa: F401,F403


from .schema.custom_codecs import *  # noqa: F401,F403
from .schema.custom_codecs import _walk_layoutbank_block  # 显式 re-export 的内部 walker。

# 可变类型的 codec 注册表。

ATTR_CUSTOM_CODEC: Dict[int, tuple] = {}  # populated after hash imports below


# 类型 hash 到 schema/数据长度的分派表。

from .hashes import (
    TRANSFORM3D,
    PARENTOPTIONS,
    SPAWN,
    LIFE,
    SHADERSETTINGS,
    VELOCITY3D,
    EMITTERSHAPE3D,
    SCALEANIM,
    FADEBYDEPTH,
    RGBFIRE,
    ROTATEANIM,
    ALPHACORRECTION,
    LUMINANCEBLEED,
    REFRACTION,
    NOISE,
    GUIDE,
    PLEMISSIVE,
    PARENTEMISSIVE,
    PLSNOW,
    PTCOLLISION,
    RANDOMFIX,
    DUMMY,
    EXTERNREFERENCE,
    PTLIFE,
    EMITTERBOUNDARY,
    FADEBYANGLE,
    MASTERONLY,
    BLINK,
    FADEBYEMITTERANGLE,
    RAYCAST,
    HOMING,
    SCREENSPACECOLLISION,
    SHOVEL,
    UVCONTROL,
    EMITTERSHAPE2D,
    VELOCITY2D,
    PATHCHAIN,
    PTTRIGGER,
    LINKPARTSVISIBLE,
    SPAWNBYANGLE,
    CHECKPUREATTRIBUTE,
    SPAWNBYOCCLUSION,
    FADEBYOCCLUSION,
    PARENTMATERIAL,
    TRANSFORM2D,
    COLORCORRECTFILTER,
    PARENTSNOW,
    OTOMOSNOW,
    FAKEPLANE,
    REPEATAREA,
    FAKEDOF,
    UNITBOUNDARY,
    RENDERTARGET,
    UVSEQUENCE,
    BILLBOARD3D,
    MESH,
    RIBBON,
    PLANE,
    RIBBONBLADE,
    STRAINRIBBON,
    TURBULENCE,
    LIGHTNING,
    RGBWATER,
    PTBEHAVIOR,
    MATERIAL,
    TONEMAPFILTER,
    TUBELIGHT,
    EMITTERSHAPEMESH,
    BILLBOARD2D,
    LAYOUT,
    # 复用主属性字段元数据的 Extern 覆盖版。
    EXTERNTRANSFORM3D, EXTERNSPAWN, EXTERNVELOCITY3D, EXTERNEMITTERSHAPE3D,
    EXTERNSCALEANIM, EXTERNRGBFIRE, EXTERNPLEMISSIVE, EXTERNLIFE, EXTERNPLSNOW,
    EXTERNPARENTEMISSIVE, EXTERNROTATEANIM, EXTERNUVSEQUENCE, EXTERNBILLBOARD3D,
    EXTERNRGBWATER, EXTERNMESH, EXTERNTYPERIBBON, EXTERNTYPEPLANE,
    # 待确认的 Extern 覆盖版。
    EXTERNFADEBYANGLE, EXTERNFADEBYDEPTH, EXTERNUVCONTROL, EXTERNGUIDE,
    EXTERNPARENTSNOW, EXTERNOTOMOSNOW, EXTERNSTRAINRIBBON, EXTERNTURBULENCE,
)

# 填充 hash 并注册字段模型。
VELOCITY3D_ATTR.hash = VELOCITY3D
register(VELOCITY3D_ATTR)

# 可变块的字段元数据也需注册，但类型分派仍必须保留 ``'_custom'`` 哨兵。
for _catt, _chash in (
    (TUBELIGHT_ATTR,        TUBELIGHT),
    (RGBWATER_ATTR,         RGBWATER),
    (STRAINRIBBON_ATTR,     STRAINRIBBON),
    (TONEMAPFILTER_ATTR,    TONEMAPFILTER),
    (LIGHTNING_ATTR,        LIGHTNING),
    (RIBBONBLADE_ATTR,      RIBBONBLADE),
    (MESH_ATTR,             MESH),
    (RIBBON_ATTR,           RIBBON),
    (EMITTERSHAPEMESH_ATTR, EMITTERSHAPEMESH),
    (BILLBOARD3D_ATTR,      BILLBOARD3D),
    (PLANE_ATTR,            PLANE),
    (UVSEQUENCE_ATTR,       UVSEQUENCE),
    (BILLBOARD2D_ATTR,      BILLBOARD2D),
    (TURBULENCE_ATTR,       TURBULENCE),
    (LAYOUT_ATTR,           LAYOUT),
):
    _catt.hash = _chash
    register(_catt)

ATTR_SCHEMA_MAP: Dict[int, Tuple[list, int]] = {
    TRANSFORM3D:    (TRANSFORM3D_SCHEMA,    228),
    PARENTOPTIONS:  (PARENTOPTIONS_SCHEMA,   60),
    SPAWN:          (SPAWN_SCHEMA,           72),
    LIFE:           (LIFE_SCHEMA,            48),
    SHADERSETTINGS: (SHADERSETTINGS_SCHEMA, 116),
    VELOCITY3D:     (VELOCITY3D_SCHEMA,     108),
    EMITTERSHAPE3D: (EMITTERSHAPE3D_SCHEMA,  88),
    SCALEANIM:      (SCALEANIM_SCHEMA,       76),
    FADEBYDEPTH:    (FADEBYDEPTH_SCHEMA,     20),
    RGBFIRE:        (RGBFIRE_SCHEMA,        112),
    ROTATEANIM:     (ROTATEANIM_SCHEMA,      80),
    ALPHACORRECTION:(ALPHACORRECTION_SCHEMA, 20),
    LUMINANCEBLEED: (LUMINANCEBLEED_SCHEMA,  16),
    REFRACTION:     (REFRACTION_SCHEMA,      12),
    NOISE:          (NOISE_SCHEMA,           44),
    GUIDE:              (GUIDE_SCHEMA,               112),
    PLEMISSIVE:         (PLEMISSIVE_SCHEMA,           76),
    PARENTEMISSIVE:     (PARENTEMISSIVE_SCHEMA,       72),
    PLSNOW:             (PLSNOW_SCHEMA,               84),
    PTCOLLISION:        (PTCOLLISION_SCHEMA,         112),
    RANDOMFIX:          (RANDOMFIX_SCHEMA,            40),
    DUMMY:              (DUMMY_SCHEMA,                 9),
    EXTERNREFERENCE:    (EXTERNREFERENCE_SCHEMA,      36),
    PTLIFE:             (PTLIFE_SCHEMA,               20),
    EMITTERBOUNDARY:    (EMITTERBOUNDARY_SCHEMA,      40),
    FADEBYANGLE:        (FADEBYANGLE_SCHEMA,          40),
    MASTERONLY:         (MASTERONLY_SCHEMA,            4),
    BLINK:              (BLINK_SCHEMA,                52),
    FADEBYEMITTERANGLE: (FADEBYEMITTERANGLE_SCHEMA,   28),
    RAYCAST:            (RAYCAST_SCHEMA,              78),
    HOMING:             (HOMING_SCHEMA,               52),
    SCREENSPACECOLLISION:(SCREENSPACECOLLISION_SCHEMA,36),
    SHOVEL:             (SHOVEL_SCHEMA,               70),
    UVCONTROL:          (UVCONTROL_SCHEMA,           236),
    EMITTERSHAPE2D:     (EMITTERSHAPE2D_SCHEMA,       36),
    VELOCITY2D:         (VELOCITY2D_SCHEMA,           72),
    PATHCHAIN:          (PATHCHAIN_SCHEMA,            77),
    PTTRIGGER:          (PTTRIGGER_SCHEMA,            16),
    LINKPARTSVISIBLE:   (LINKPARTSVISIBLE_SCHEMA,     12),
    SPAWNBYANGLE:       (SPAWNBYANGLE_SCHEMA,         22),
    CHECKPUREATTRIBUTE: (CHECKPUREATTRIBUTE_SCHEMA,   40),
    SPAWNBYOCCLUSION:   (SPAWNBYOCCLUSION_SCHEMA,     20),
    FADEBYOCCLUSION:    (FADEBYOCCLUSION_SCHEMA,      24),
    PARENTMATERIAL:     (PARENTMATERIAL_SCHEMA,       12),
    TRANSFORM2D:        (TRANSFORM2D_SCHEMA,          24),
    COLORCORRECTFILTER: (COLORCORRECTFILTER_SCHEMA,  688),
    PARENTSNOW:         (PARENTSNOW_SCHEMA,           80),
    OTOMOSNOW:          (OTOMOSNOW_SCHEMA,            84),
    FAKEPLANE:          (FAKEPLANE_SCHEMA,            60),
    REPEATAREA:         (REPEATAREA_SCHEMA,           52),
    FAKEDOF:            (FAKEDOF_SCHEMA,              32),
    # Root 专属子条目。
    UNITBOUNDARY:       (UNITBOUNDARY_SCHEMA,         40),
    # 可变/分派类型使用 ``'_custom'`` 哨兵。
    UVSEQUENCE:  ('_custom', None),
    BILLBOARD3D: ('_custom', None),
    MESH:        ('_custom', None),
    RIBBON:      ('_custom', None),
    PLANE:       ('_custom', None),
    RIBBONBLADE: ('_custom', None),
    STRAINRIBBON:('_custom', None),
    TURBULENCE:  ('_custom', None),
    LIGHTNING:   ('_custom', None),
    RGBWATER:    ('_custom', None),
    PTBEHAVIOR:  ('_custom', None),
    MATERIAL:    ('_custom', None),
    TONEMAPFILTER:('_custom', None),
    TUBELIGHT:        ('_custom', None),
    EMITTERSHAPEMESH: ('_custom', None),
    BILLBOARD2D:      ('_custom', None),
    LAYOUT:           ('_custom', None),
    # Root 专属子条目。
    RENDERTARGET:     ('_custom', None),
}

# 注册可变类型 codec。
ATTR_CUSTOM_CODEC = {
    UVSEQUENCE:  (unpack_uvsequence,  pack_uvsequence),
    BILLBOARD3D: (unpack_billboard3d, pack_billboard3d),
    MESH:        (unpack_mesh,        pack_mesh),
    RIBBON:      (unpack_ribbon,      pack_ribbon),
    PLANE:       (unpack_plane,       pack_plane),
    RIBBONBLADE: (unpack_ribbonblade, pack_ribbonblade),
    STRAINRIBBON:(unpack_strainribbon,pack_strainribbon),
    TURBULENCE:  (unpack_turbulence,  pack_turbulence),
    LIGHTNING:   (unpack_lightning,   pack_lightning),
    RGBWATER:    (unpack_rgbwater,    pack_rgbwater),
    PTBEHAVIOR:   (unpack_ptbehavior,   pack_ptbehavior),
    MATERIAL:     (unpack_material,     pack_material),
    TONEMAPFILTER:(unpack_tonemapfilter,pack_tonemapfilter),
    TUBELIGHT:        (unpack_tubelight,        pack_tubelight),
    EMITTERSHAPEMESH: (unpack_emittershapemesh, pack_emittershapemesh),
    BILLBOARD2D:      (unpack_billboard2d,      pack_billboard2d),
    LAYOUT:           (unpack_layout,           pack_layout),
    RENDERTARGET:     (unpack_rendertarget,     pack_rendertarget),
}


# 从 schema 反查 hash，并注册字段元数据。
_MIGRATED_ATTRS = [
    EXTERN_TRANSFORM3D_ATTR,
    PARENTOPTIONS_ATTR,
    EXTERN_SPAWN_ATTR,
    LIFE_ATTR,
    SHADERSETTINGS_ATTR,
    EXTERN_EMITTERSHAPE3D_ATTR,
    EXTERN_SCALEANIM_ATTR,
    FADEBYDEPTH_ATTR,
    EXTERN_RGBFIRE_ATTR,
    ROTATEANIM_ATTR,
    ALPHACORRECTION_ATTR,
    LUMINANCEBLEED_ATTR,
    REFRACTION_ATTR,
    NOISE_ATTR,
    GUIDE_ATTR,
    PLEMISSIVE_ATTR,
    PARENTEMISSIVE_ATTR,
    PLSNOW_ATTR,
    PTCOLLISION_ATTR,
    RANDOMFIX_ATTR,
    DUMMY_ATTR,
    EXTERNREFERENCE_ATTR,
    PTLIFE_ATTR,
    EMITTERBOUNDARY_ATTR,
    FADEBYANGLE_ATTR,
    MASTERONLY_ATTR,
    BLINK_ATTR,
    FADEBYEMITTERANGLE_ATTR,
    RAYCAST_ATTR,
    HOMING_ATTR,
    SCREENSPACECOLLISION_ATTR,
    SHOVEL_ATTR,
    UVCONTROL_ATTR,
    EMITTERSHAPE2D_ATTR,
    VELOCITY2D_ATTR,
    PATHCHAIN_ATTR,
    PTTRIGGER_ATTR,
    LINKPARTSVISIBLE_ATTR,
    SPAWNBYANGLE_ATTR,
    CHECKPUREATTRIBUTE_ATTR,
    SPAWNBYOCCLUSION_ATTR,
    FADEBYOCCLUSION_ATTR,
    PARENTMATERIAL_ATTR,
    TRANSFORM2D_ATTR,
    COLORCORRECTFILTER_ATTR,
    PARENTSNOW_ATTR,
    OTOMOSNOW_ATTR,
    FAKEPLANE_ATTR,
    REPEATAREA_ATTR,
    FAKEDOF_ATTR,
    UNITBOUNDARY_ATTR,
]
_schema_id_to_hash = {id(_sch): _h for _h, (_sch, _sz) in ATTR_SCHEMA_MAP.items() if isinstance(_sch, list)}
for _a in _MIGRATED_ATTRS:
    _h = _schema_id_to_hash.get(id(_a.schema))
    if _h is not None:
        _a.hash = _h
        register(_a)

# Extern 覆盖版须额外登记字段元数据，不能修改主属性的 ``.hash``。
EXTERN_HASH_ALIASES = {
    EXTERNTRANSFORM3D:    TRANSFORM3D,
    EXTERNSPAWN:          SPAWN,
    EXTERNVELOCITY3D:     VELOCITY3D,
    EXTERNEMITTERSHAPE3D: EMITTERSHAPE3D,
    EXTERNSCALEANIM:      SCALEANIM,
    EXTERNRGBFIRE:        RGBFIRE,
    EXTERNPLEMISSIVE:     PLEMISSIVE,
    EXTERNLIFE:           LIFE,
    EXTERNPLSNOW:         PLSNOW,
    EXTERNPARENTEMISSIVE: PARENTEMISSIVE,
    EXTERNROTATEANIM:     ROTATEANIM,
    EXTERNUVSEQUENCE:     UVSEQUENCE,
    EXTERNBILLBOARD3D:    BILLBOARD3D,
    EXTERNRGBWATER:       RGBWATER,
    EXTERNMESH:           MESH,
    EXTERNTYPERIBBON:     RIBBON,
    EXTERNTYPEPLANE:      PLANE,

    # 待确认的固定长度覆盖版。
    EXTERNFADEBYANGLE:    FADEBYANGLE,
    EXTERNFADEBYDEPTH:    FADEBYDEPTH,
    EXTERNUVCONTROL:      UVCONTROL,
    EXTERNGUIDE:          GUIDE,
    EXTERNPARENTSNOW:     PARENTSNOW,
    EXTERNOTOMOSNOW:      OTOMOSNOW,
}
for _extern_hash, _main_hash in EXTERN_HASH_ALIASES.items():
    _main_attr = ATTR_REGISTRY.get(_main_hash)
    if _main_attr is not None:
        register_alias(_extern_hash, _main_attr)

# 这两个待确认覆盖版使用独立字段模型，不能作为主属性别名。
EXTERN_STRAINRIBBON_ATTR.hash = EXTERNSTRAINRIBBON
register(EXTERN_STRAINRIBBON_ATTR)
EXTERN_TURBULENCE_ATTR.hash = EXTERNTURBULENCE
register(EXTERN_TURBULENCE_ATTR)

# UI 用此集合提示待确认的 Extern 编辑支持。
FABRICATED_EXTERN_HASHES = frozenset({
    EXTERNFADEBYANGLE, EXTERNFADEBYDEPTH, EXTERNUVCONTROL, EXTERNGUIDE,
    EXTERNPARENTSNOW, EXTERNOTOMOSNOW, EXTERNSTRAINRIBBON, EXTERNTURBULENCE,
})
