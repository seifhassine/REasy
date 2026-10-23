# -*- coding: utf-8 -*-
"""MHW .efx 的纯 Python 解析、序列化与语义模型。

维护约束：
- 本包不得依赖 bpy；Blender 适配归属 blender_efx 与 blender_epv。
- 未知结构必须以原始字节保留，确保 ``serialize(parse(x)) == x``。
- structs.py 是 schema 的完整导入门面；本模块的 re-export 仅为兼容子集。
"""


from .efxfile import EFXFile
from .structs import (
    unpack, pack, _schema_size,
    ATTR_SCHEMA_MAP,
    TRANSFORM3D_SCHEMA,
    PARENTOPTIONS_SCHEMA,
    SPAWN_SCHEMA,
    LIFE_SCHEMA,
    SHADERSETTINGS_SCHEMA,
    VELOCITY3D_SCHEMA,
    EMITTERSHAPE3D_SCHEMA,
    SCALEANIM_SCHEMA,
    FADEBYDEPTH_SCHEMA,
    RGBFIRE_SCHEMA,
    ROTATEANIM_SCHEMA,
    ALPHACORRECTION_SCHEMA,
    LUMINANCEBLEED_SCHEMA,
    REFRACTION_SCHEMA,
    NOISE_SCHEMA,
    # Shared ExternXxx variants (same schema, reused by Main and Extern)
    EXTERN_TRANSFORM3D_SCHEMA,
    EXTERN_SPAWN_SCHEMA,
    EXTERN_VELOCITY3D_SCHEMA,
    EXTERN_EMITTERSHAPE3D_SCHEMA,
    EXTERN_SCALEANIM_SCHEMA,
    EXTERN_RGBFIRE_SCHEMA,
)

__all__ = [
    "EFXFile",
    "unpack", "pack", "_schema_size",
    "ATTR_SCHEMA_MAP",
    "TRANSFORM3D_SCHEMA",
    "PARENTOPTIONS_SCHEMA",
    "SPAWN_SCHEMA",
    "LIFE_SCHEMA",
    "SHADERSETTINGS_SCHEMA",
    "VELOCITY3D_SCHEMA",
    "EMITTERSHAPE3D_SCHEMA",
    "SCALEANIM_SCHEMA",
    "FADEBYDEPTH_SCHEMA",
    "RGBFIRE_SCHEMA",
    "ROTATEANIM_SCHEMA",
    "ALPHACORRECTION_SCHEMA",
    "LUMINANCEBLEED_SCHEMA",
    "REFRACTION_SCHEMA",
    "NOISE_SCHEMA",
    "EXTERN_TRANSFORM3D_SCHEMA",
    "EXTERN_SPAWN_SCHEMA",
    "EXTERN_VELOCITY3D_SCHEMA",
    "EXTERN_EMITTERSHAPE3D_SCHEMA",
    "EXTERN_SCALEANIM_SCHEMA",
    "EXTERN_RGBFIRE_SCHEMA",
]
