# -*- coding: utf-8 -*-
"""
efx_format/schema/attributes.py — 定长 attribute 块的 typed schema 定义

 hash 常量晚于此处导入 → Attribute 定义时 hash 留空，由 structs 装配层导入 hashes 后回填 + register。
"""
from .fields_model import (
    Attribute, Int, UInt, Short, UShort, Byte, SByte, Float, Int64, UInt64,
    Enum, EnumVec3, Bool, Bitmask, Raw,
)
from .enums import (
    ENUM_SHAPE_TYPE3D, ENUM_RANGE_DIVIDE_AXIS, ENUM_RANGE_DIVIDE_AXIS_2D,
    ENUM_ROTATION_CORRECT_TYPE, ENUM_RAYCAST_DEPENDENCY,
    ENUM_SHAPE_TYPE2D, ENUM_COLLISION_PHYSICS, ENUM_IMPACT_PLAY_TRIGGER_MODE, ENUM_PTLIFE_STATUS,
    ENUM_PTLIFE_RELATION,
    ENUM_RAYCAST_DIR, ENUM_RAYCAST_ID, ENUM_HOMING_TARGET, ENUM_HOMING_FORCEFIELD, ENUM_HOMING_VANISH,
    ENUM_PARTICLE_LIGHTING, ENUM_DRAW_MODE, ENUM_DRAW_TARGET, ENUM_BLEND_STATE, BITS_ROTATION_MODE,
    ENUM_TRACKING_POS, ENUM_TRACKING_ANGLE, ENUM_DISTORTION_TYPE,
    ENUM_UNITBOUNDARY_TYPE,
    BITS_ENABLE_VELOCITY, BITS_RANDOMFIX_TABLE,
    BITS_FADEBYANGLE_FLAGS,
    BITS_SPAWN_FLAGS,
    BITS_RAYCAST_ATTR, BITS_RAYCAST_FLAGS,
    BITS_PLEMISSIVE_EMIT_MASK, BITS_EXTERNREF_TRIGGER,
    _AXIS_DIRECTION6, _ROT_ORDER6, _VELOCITY_TYPE, _TRANSFORM_ROT_ORDER,
)
from .codec import _schema_size

# ─────────────────────────────────────────────────────────────────────────────
# ExternTransform3D
# ─────────────────────────────────────────────────────────────────────────────

EXTERN_TRANSFORM3D_ATTR = Attribute(size=228, fields=[
    Int("typeFlag"),
    Raw("translate", ('XYZ', 0), label_zh="平移"),
    Raw("rotate", ('XYZ', 0), label_zh="旋转"),
    Raw("resize", ('XYZ', 0), label_zh="缩放"),
    Enum("rotationOrder", _TRANSFORM_ROT_ORDER, label_zh="旋转顺序"),
    Raw("translation_velocity", ('XYZ', 0), label_zh="平移速度"),
    Raw("translation_velocity_modifier", ('XYZ', 0), label_zh="平移速度修正"),
    Raw("rotation_velocity", ('XYZ', 0), label_zh="旋转速度"),
    Raw("rotation_velocity_modifier", ('XYZ', 0), label_zh="旋转速度修正"),
    Raw("scale_velocity", ('XYZ', 0), label_zh="缩放速度"),
    Raw("scale_velocity_modifier", ('XYZ', 0), label_zh="缩放速度修正"),
    # 只有 bit0/bit1 两位且可混合，故建模为 Bitmask 而非 4 值枚举
    Bitmask("enableVelocityBitflag", BITS_ENABLE_VELOCITY, label_zh="启用速度位标志", strict=True),
])
EXTERN_TRANSFORM3D_SCHEMA = EXTERN_TRANSFORM3D_ATTR.schema
assert _schema_size(EXTERN_TRANSFORM3D_SCHEMA) == 228, \
    f"EXTERN_TRANSFORM3D_SCHEMA size mismatch: {_schema_size(EXTERN_TRANSFORM3D_SCHEMA)}"

TRANSFORM3D_SCHEMA = EXTERN_TRANSFORM3D_SCHEMA


# ─────────────────────────────────────────────────────────────────────────────
# ParentOptions
# ─────────────────────────────────────────────────────────────────────────────

PARENTOPTIONS_ATTR = Attribute(size=60, fields=[
    Int("typeFlag"),
    EnumVec3("relationPos", ENUM_TRACKING_POS, label_zh="平移跟踪"),
    EnumVec3("relationRot", ENUM_TRACKING_ANGLE, label_zh="角度跟踪"),
    EnumVec3("relationScl", ENUM_TRACKING_POS, label_zh="缩放跟踪"),
    Bool("particleUseLocal", label_en="Follow Emitter", label_zh="跟随发射器"),
    Bool("invalidParticleScale", label_en="Ignore Emitter Size", label_zh="大小不随发射器"),
    Int("constRelease", label_zh="停止追踪帧数"),
    Int("constReleaseJitter", label_zh="停止追踪帧数抖动"),
    Int("jointNo", label_zh="绑定骨骼"),
])
PARENTOPTIONS_SCHEMA = PARENTOPTIONS_ATTR.schema
assert _schema_size(PARENTOPTIONS_SCHEMA) == 60, \
    f"PARENTOPTIONS_SCHEMA size mismatch: {_schema_size(PARENTOPTIONS_SCHEMA)}"


# ─────────────────────────────────────────────────────────────────────────────
# ExternSpawn
# ─────────────────────────────────────────────────────────────────────────────

EXTERN_SPAWN_ATTR = Attribute(size=72, fields=[
    Int("typeFlag"),
    Int("maxParticles", label_zh="同时存活上限"),  # 同时存活的软上限，不是终身总量
    Int("spawnNum", label_zh="单批生成数"),
    Int("spawnNumJitter", label_zh="单批生成数抖动"),
    Int("intervalFrame", label_zh="批次间隔（帧）"),
    Int("intervalFrameJitter", label_zh="批次间隔抖动（帧）"),
    Int("loopNum", label_zh="每轮批次数"),
    Int("loopNumJitter", label_zh="每轮批次数抖动"),
    Int("spawnFrame", label_zh="生成时限（帧）"),  # 只在前 N 帧生成，由 spawnFlags 的 UseSpawnFrame 位开启
    Int("spawnFrameJitter", label_zh="生成时限抖动（帧）"),
    Int("emitterDelayFrame", label_zh="发射器启动延迟（帧）"),  # 发射器首次生成前的一次性延迟
    Int("emitterDelayFrameJitter", label_zh="发射器启动延迟抖动（帧）"),
    Int("particleDelayFrame", label_zh="粒子延迟（帧）"),  # 粒子个体各自的出现延迟
    Int("particleDelayFrameJitter", label_zh="粒子延迟抖动（帧）"),
    Int("revivalLoop", label_zh="复活轮数"),  # 总轮数，0 = 无限
    Int("revivalInterval", label_zh="复活间隔（帧）"),  # 一轮最后一批发出后到下一轮开始
    Int("revivalIntervalJitter", label_zh="复活间隔抖动（帧）"),
    Bitmask("spawnFlags", BITS_SPAWN_FLAGS, strict=True, label_zh="生成标志位"),
])
EXTERN_SPAWN_SCHEMA = EXTERN_SPAWN_ATTR.schema
assert _schema_size(EXTERN_SPAWN_SCHEMA) == 72, \
    f"EXTERN_SPAWN_SCHEMA size mismatch: {_schema_size(EXTERN_SPAWN_SCHEMA)}"

SPAWN_SCHEMA = EXTERN_SPAWN_SCHEMA


# ─────────────────────────────────────────────────────────────────────────────
# Life
# ─────────────────────────────────────────────────────────────────────────────
# unknFrame/unknFrameJitter 的名字只是按邻近 keepFrame/keepFrameJitter 的配对惯例取的，
# 未确认是帧数，不要按名字推断语义。

LIFE_ATTR = Attribute(size=48, fields=[
    Int("typeFlag"),
    Int("appearFrame", label_zh="淡入时长"),
    Int("appearFrameJitter", label_zh="淡入时长抖动"),
    Int("keepFrame", label_zh="持续时间"),
    Int("keepFrameJitter", label_zh="持续时间抖动"),
    Int("unknFrame"),
    Int("unknFrameJitter"),
    Int("vanishFrame", label_zh="淡出时长"),
    Int("vanishFrameJitter", label_zh="淡出时长抖动"),
    Int("timeToDeath", label_zh="死亡时间"),
    Int("timeToDeathJitter", label_zh="死亡时间抖动"),
    Bool("indefiniteLifespan", label_zh="无限寿命"),
])
LIFE_SCHEMA = LIFE_ATTR.schema
assert _schema_size(LIFE_SCHEMA) == 48, \
    f"LIFE_SCHEMA size mismatch: {_schema_size(LIFE_SCHEMA)}"


# ─────────────────────────────────────────────────────────────────────────────
# ShaderSettings
# ─────────────────────────────────────────────────────────────────────────────

SHADERSETTINGS_ATTR = Attribute(size=116, fields=[
    Int("typeFlag"),
    Int("section_length", label_zh="段长度"),  # 不含末尾的 unknBitmask5_1，比块剩余长度少 4
    Int("spacer"),
    Bool("versionRelated", label_en="Version Related", label_zh="版本相关"),
    Float("depthBias", label_en="Depth Bias", label_zh="深度偏移"),
    Float("softParticleDistance", label_en="Soft Particle Distance", label_zh="软粒子距离"),
    Enum("particleLighting", ENUM_PARTICLE_LIGHTING, label_en="Particle Lighting", label_zh="粒子光照"),
    Enum("drawMode", ENUM_DRAW_MODE, label_en="Draw Mode", label_zh="绘制模式"),
    Enum("blendStateType", ENUM_BLEND_STATE, label_en="Blend State Type", label_zh="混合方式"),
    Float("reflectance", label_en="Reflectance", label_zh="反射率"),
    Float("transmittance", label_en="Transmittance", label_zh="透光率"),
    Float("transmittanceJitter", label_en="Transmittance Jitter", label_zh="透光率抖动"),
    Float("envLightFactor", label_en="Env Light Factor", label_zh="环境光系数"),
    Float("envSaturation", label_en="Env Saturation", label_zh="环境光饱和度"),
    Float("lightFactor", label_en="Light Factor", label_zh="直射光系数"),
    Float("shadowFactor", label_en="Shadow Factor", label_zh="阴影系数"),
    Float("unkn4_7"),
# presetId 是外部资源表的引用而非固定枚举，故保留原始 int，不建 Enum。
    Int("presetId", label_en="Preset ID", label_zh="预设 ID"),
    Raw("lightSamplingOffset", ('XYZ', 3), label_en="Light Sampling Offset", label_zh="光照取样偏移"),
    Float("unknFixed4_12"),
    Float("lightSamplingDistance", label_en="Light Sampling Distance", label_zh="光照取样距离"),
    Enum("drawTarget", ENUM_DRAW_TARGET, label_en="Draw Target", label_zh="绘制目标"),
    Int("drawPriority", label_en="Draw Priority", label_zh="绘制优先级"),
    Byte("objectInteractionFlag0", label_zh="物体交互标志0"),
    Byte("objectInteractionFlag1", label_zh="物体交互标志1"),
    Byte("objectInteractionFlag2", label_zh="物体交互标志2"),
    Byte("objectInteractionFlag3", label_zh="物体交互标志3"),
    Bool("unknBool0", backing='B'),  # 由一个 visibleOnPreview 拆成 4 个独立字节，不是单一标志
    Bool("unknBool1", backing='B'),
    Bool("unknBool2", backing='B'),
    Bool("unknBool3", backing='B'),
    Bool("lightSamplingRotateFix", backing='B', label_en="Light Sampling Rotate Fix", label_zh="取样偏移不随旋转"),
    Byte("unknFixed5_0_1"),
    Bool("unknFlag5_0_2", backing='B'),
    Bool("unknFlag5_0_3", backing='B'),
    Int("unknBitmask5_1"),
])
SHADERSETTINGS_SCHEMA = SHADERSETTINGS_ATTR.schema
assert _schema_size(SHADERSETTINGS_SCHEMA) == 116, \
    f"SHADERSETTINGS_SCHEMA size mismatch: {_schema_size(SHADERSETTINGS_SCHEMA)}"


# ─────────────────────────────────────────────────────────────────────────────
# ExternVelocity3D
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# VELOCITY3D
# ─────────────────────────────────────────────────────────────────────────────
# ⚠ baseAxis 的枚举取值未确认，不要按枚举名推断轴向。
# velocityType 官方语料实测出现过 4，已作为 Unknown (4) 补进枚举（见 enums.py）。


VELOCITY3D_ATTR = Attribute(size=108, native_timl_axis=0, fields=[
    Int("typeFlag"),
    Enum("baseAxis", _AXIS_DIRECTION6, label_zh="基准轴"),
    Enum("rotOrder", _ROT_ORDER6, label_zh="旋转顺序"),
    Float("rotationX", label_zh="X 旋转"),
    Float("rotationXJitter", label_zh="X 旋转抖动"),
    Float("rotationY", label_zh="Y 旋转"),
    Float("rotationYJitter", label_zh="Y 旋转抖动"),
    Float("rotationZ", label_zh="Z 旋转"),
    Float("rotationZJitter", label_zh="Z 旋转抖动"),
    Float("speed", label_zh="初速度"),
    Float("speedJitter", label_zh="初速度偏差"),
    Float("speedCoef", label_zh="加速度"),
    Float("speedCoefJitter", label_zh="加速度偏差"),
    Float("offsetX", label_zh="X 基准点偏置"),
    Float("offsetY", label_zh="Y 基准点偏置"),
    Float("offsetZ", label_zh="Z 基准点偏置"),
    Float("sizeX", label_zh="X 基准点伸缩"),
    Float("sizeY", label_zh="Y 基准点伸缩"),
    Float("sizeZ", label_zh="Z 基准点伸缩"),
    Enum("velocityType", _VELOCITY_TYPE, label_zh="速度类型"),
    Float("gravity", label_zh="重力"),
    Float("gravity_jitter", label_zh="重力抖动"),
    Int("movementDelay", label_zh="运动延迟"),
    Int("movementDelayJitter", label_zh="运动延迟抖动"),
    Int("gravityDelay", label_zh="重力延迟"),
    Int("gravityDelayJitter", label_zh="重力延迟抖动"),
    Float("minMovementThreshold", label_zh="最小移动阈值"),
])

EXTERN_VELOCITY3D_SCHEMA = VELOCITY3D_ATTR.schema
assert _schema_size(EXTERN_VELOCITY3D_SCHEMA) == 108, \
    f"EXTERN_VELOCITY3D_SCHEMA size mismatch: {_schema_size(EXTERN_VELOCITY3D_SCHEMA)}"

VELOCITY3D_SCHEMA = EXTERN_VELOCITY3D_SCHEMA


# ─────────────────────────────────────────────────────────────────────────────
# EXTERNLIFE / EXTERNTYPERIBBON / EXTERNPLSNOW / EXTERNPARENTEMISSIVE /
# EXTERNROTATEANIM / EXTERNTYPEPLANE
# ─────────────────────────────────────────────────────────────────────────────
# 这六个是对应主属性的 Extern 覆盖版，直接复用主属性的 schema 与 codec，本文件不另定义：
# 四个定长类型用各自的主属性 schema，RIBBON / PLANE 走 custom_codecs 的成对 codec，
# 尺寸计算见 efxfile.py::_extern_data_size。


# ─────────────────────────────────────────────────────────────────────────────
# ExternEmitterShape3D
# ─────────────────────────────────────────────────────────────────────────────

EXTERN_EMITTERSHAPE3D_ATTR = Attribute(size=88, fields=[
    Int("typeFlag"),
    Raw("rangeXYZ", ('XYZ', 0), label_zh="生成范围"),
    Enum("shapeType", ENUM_SHAPE_TYPE3D, label_zh="形状类型"),
    Enum("rangeDivideAxis", ENUM_RANGE_DIVIDE_AXIS, label_zh="细分轴向"),
    Enum("rotationCorrect", ENUM_ROTATION_CORRECT_TYPE, label_zh="旋转修正方式"),
    Float("localRotationX", label_zh="局部旋转 X"),
    Float("localRotationY", label_zh="局部旋转 Y"),
    Float("localRotationZ", label_zh="局部旋转 Z"),
    Enum("rotationOrder", _TRANSFORM_ROT_ORDER, label_zh="旋转顺序"),
    Float("scanAngleHorizontal", label_zh="横向扫描角度"),
    Float("scanAngleVertical", label_zh="纵向扫描角度"),
    Int("rangeDivideHorizontalNum", label_zh="横向等分数量"),
    Int("rangeDivideVerticalNum", label_zh="纵向等分数量"),
    Float("radiusEnd", label_zh="结束半径"),
    Float("radiusOrigin", label_zh="起始半径"),
    Enum("rayCastDependency", ENUM_RAYCAST_DEPENDENCY, label_zh="射线检测依赖"),
    Bool("unknFlag4"),
])
EXTERN_EMITTERSHAPE3D_SCHEMA = EXTERN_EMITTERSHAPE3D_ATTR.schema
assert _schema_size(EXTERN_EMITTERSHAPE3D_SCHEMA) == 88, \
    f"EXTERN_EMITTERSHAPE3D_SCHEMA size mismatch: {_schema_size(EXTERN_EMITTERSHAPE3D_SCHEMA)}"

EMITTERSHAPE3D_SCHEMA = EXTERN_EMITTERSHAPE3D_SCHEMA


# ─────────────────────────────────────────────────────────────────────────────
# ExternScaleAnim
# ─────────────────────────────────────────────────────────────────────────────

# 两阶段缩放：初始整体扩散（速度+加速度），之后逐轴缩放（X/Y/Z 各有速度/加速度/偏差）。
EXTERN_SCALEANIM_ATTR = Attribute(size=76, fields=[
    Int("typeFlag"),
    Float("sizeScalarAdd", label_zh="整体缩放速度"),
    Float("sizeScalarAddJitter", label_zh="整体缩放速度抖动"),

    Float("sizeScalarAddCoef", label_zh="整体缩放速度系数"),
    Float("sizeScalarAddCoefJitter", label_zh="整体缩放速度系数抖动"),
    Float("sizeXAdd", label_zh="X 缩放速度"),
    Float("sizeXAddJitter", label_zh="X 缩放速度抖动"),
    Float("sizeXAddCoef", label_zh="X 缩放速度系数"),
    Float("sizeXAddCoefJitter", label_zh="X 缩放速度系数抖动"),
    Float("sizeYAdd", label_zh="Y 缩放速度"),
    Float("sizeYAddJitter", label_zh="Y 缩放速度抖动"),
    Float("sizeYAddCoef", label_zh="Y 缩放速度系数"),
    Float("sizeYAddCoefJitter", label_zh="Y 缩放速度系数抖动"),
    Float("sizeZAdd", label_zh="Z 缩放速度"),
    Float("sizeZAddJitter", label_zh="Z 缩放速度抖动"),
    Float("sizeZAddCoef", label_zh="Z 缩放速度系数"),
    Float("sizeZAddCoefJitter", label_zh="Z 缩放速度系数抖动"),
    Int("animUpdateStart", label_zh="缩放延迟"),
    Int("animUpdateStartJitter", label_zh="缩放延迟抖动"),
])
EXTERN_SCALEANIM_SCHEMA = EXTERN_SCALEANIM_ATTR.schema
assert _schema_size(EXTERN_SCALEANIM_SCHEMA) == 76, \
    f"EXTERN_SCALEANIM_SCHEMA size mismatch: {_schema_size(EXTERN_SCALEANIM_SCHEMA)}"

SCALEANIM_SCHEMA = EXTERN_SCALEANIM_SCHEMA


# ─────────────────────────────────────────────────────────────────────────────
# FadeByDepth
# ─────────────────────────────────────────────────────────────────────────────

FADEBYDEPTH_ATTR = Attribute(size=20, fields=[
    Int("typeFlag"),
    Float("nearFadeInStart", label_zh="近处淡入起点"),
    Float("nearFadeInEnd", label_zh="近处淡入终点"),
    Float("farFadeOutStart", label_zh="远处淡出起点"),
    Float("farFadeOutEnd", label_zh="远处淡出终点"),
])
FADEBYDEPTH_SCHEMA = FADEBYDEPTH_ATTR.schema
assert _schema_size(FADEBYDEPTH_SCHEMA) == 20, \
    f"FADEBYDEPTH_SCHEMA size mismatch: {_schema_size(FADEBYDEPTH_SCHEMA)}"


# ─────────────────────────────────────────────────────────────────────────────
# ExternRgbFire
# ─────────────────────────────────────────────────────────────────────────────

EXTERN_RGBFIRE_ATTR = Attribute(size=112, fields=[
    Int("typeFlag"),
    # fire/smoke 分别作用在纹理的绿通道与红通道上，标签里括注 GreenCh/RedCh 是有意为之。
    Raw("fireColor", ('XYZ', 2), label_en="Fire (GreenCh) Color", label_zh="火焰（绿通道）颜色"),
    Float("fireFactor", label_en="Fire (GreenCh) Factor", label_zh="火焰（绿通道）系数"),
    Raw("smokeColor", ('XYZ', 2), label_en="Smoke (RedCh) Color", label_zh="烟雾（红通道）颜色"),
    Float("redChFactor", label_en="Smoke (RedCh) Factor", label_zh="烟雾（红通道）系数"),
    Float("lerpAlphaToBlue", label_en="Lerp Alpha To Blue", label_zh="Alpha 混入蓝通道比例"),
    Float("alphaFactor", label_en="Alpha Factor", label_zh="透明度强度"),  # 效果未接入模拟预览
    Float("colorRate", label_en="Color Rate", label_zh="亮度强度"),
# ⚠ 前缀 fireColorParam_ / smokeColorParam_ 必须保留：color_fields.py 靠它把整块
#   归类为颜色相关字段，改前缀会让 Color Editor 的过滤失效。
    Bool("fireColorParam_useLife", label_en="Fire (GreenCh) Use Life", label_zh="火焰（绿通道）启用生命期"),
    Int("fireColorParam_appearFrame", label_en="Fire (GreenCh) Appear", label_zh="火焰（绿通道）淡入"),
    Int("fireColorParam_appearFrameJitter", label_en="Fire (GreenCh) Appear Jitter", label_zh="火焰（绿通道）淡入抖动"),
    Int("fireColorParam_keepFrame", label_en="Fire (GreenCh) Keep", label_zh="火焰（绿通道）持续"),
    Int("fireColorParam_keepFrameJitter", label_en="Fire (GreenCh) Keep Jitter", label_zh="火焰（绿通道）持续抖动"),
    Int("fireColorParam_vanishFrame", label_en="Fire (GreenCh) Vanish", label_zh="火焰（绿通道）淡出"),
    Int("fireColorParam_vanishFrameJitter", label_en="Fire (GreenCh) Vanish Jitter", label_zh="火焰（绿通道）淡出抖动"),
    Bool("fireColorParam_lighting", label_en="Fire (GreenCh) Lighting", label_zh="火焰（绿通道）受光照"),
    Int("fireColorParam_lifeType", label_en="Fire (GreenCh) Life Type", label_zh="火焰（绿通道）生命期模式"),
    # EPV 槽位覆盖 id，0 表示用本地颜色值；非 0 时本地预览取不到 .epv 数据，颜色显示为空。
    Int("fireColorParam_correctColorNo", label_en="Correct Fire Color No", label_zh="EPV 颜色修正槽位"),
    Bool("smokeColorParam_useLife", label_en="Smoke (RedCh) Use Life", label_zh="烟雾（红通道）启用生命期"),
    Int("smokeColorParam_appearFrame", label_en="Smoke (RedCh) Appear", label_zh="烟雾（红通道）淡入"),
    Int("smokeColorParam_appearFrameJitter", label_en="Smoke (RedCh) Appear Jitter", label_zh="烟雾（红通道）淡入抖动"),
    Int("smokeColorParam_keepFrame", label_en="Smoke (RedCh) Keep", label_zh="烟雾（红通道）持续"),
    Int("smokeColorParam_keepFrameJitter", label_en="Smoke (RedCh) Keep Jitter", label_zh="烟雾（红通道）持续抖动"),
    Int("smokeColorParam_vanishFrame", label_en="Smoke (RedCh) Vanish", label_zh="烟雾（红通道）淡出"),
    Int("smokeColorParam_vanishFrameJitter", label_en="Smoke (RedCh) Vanish Jitter", label_zh="烟雾（红通道）淡出抖动"),
    Bool("smokeColorParam_lighting", label_en="Smoke (RedCh) Lighting", label_zh="烟雾（红通道）受光照"),
    Int("smokeColorParam_lifeType", label_en="Smoke (RedCh) Life Type", label_zh="烟雾（红通道）生命期模式"),
    Int("smokeColorParam_correctColorNo", label_en="Correct Smoke Color No", label_zh="EPV 颜色修正槽位"),
])
EXTERN_RGBFIRE_SCHEMA = EXTERN_RGBFIRE_ATTR.schema
assert _schema_size(EXTERN_RGBFIRE_SCHEMA) == 112, \
    f"EXTERN_RGBFIRE_SCHEMA size mismatch: {_schema_size(EXTERN_RGBFIRE_SCHEMA)}"

RGBFIRE_SCHEMA = EXTERN_RGBFIRE_SCHEMA


# ─────────────────────────────────────────────────────────────────────────────
# RotateAnim
# ─────────────────────────────────────────────────────────────────────────────

ROTATEANIM_ATTR = Attribute(size=80, fields=[
    Int("typeFlag"),
    # 第 1 位选组：开 = 自旋速度组，关 = 平面旋转组；第 0 位 = 随机正反向。
    Bitmask("rotationModeMask", BITS_ROTATION_MODE, strict=True, label_zh="旋转模式"),
    Float("billboardRotation", label_zh="平面旋转"),
    Float("billboardRotationJitter", label_zh="平面旋转抖动"),  # 是 billboardRotation 的 random 分量
    Raw("spin_velocity", ('XYZ', 0), label_zh="自旋速度"),
    Float("billboardRotationCoef", label_zh="平面旋转加速度"),
    Float("billboardRotationCoefJitter", label_zh="平面旋转加速度抖动"),
    Float("spinSpeedCoefX", label_zh="自旋加速度 X"),
    Float("spinSpeedCoefXJitter", label_zh="自旋加速度 X 抖动"),
    Float("spinSpeedCoefY", label_zh="自旋加速度 Y"),
    Float("spinSpeedCoefYJitter", label_zh="自旋加速度 Y 抖动"),
    Float("spinSpeedCoefZ", label_zh="自旋加速度 Z"),
    Float("spinSpeedCoefZJitter", label_zh="自旋加速度 Z 抖动"),
    Int("rotateDelayStart", label_zh="旋转延迟起始帧"),  # 实为 int 帧数，不是 float
    Int("rotateDelayStartJitter", label_zh="旋转延迟起始帧抖动"),
])
ROTATEANIM_SCHEMA = ROTATEANIM_ATTR.schema
assert _schema_size(ROTATEANIM_SCHEMA) == 80, \
    f"ROTATEANIM_SCHEMA size mismatch: {_schema_size(ROTATEANIM_SCHEMA)}"


# ─────────────────────────────────────────────────────────────────────────────
# AlphaCorrection
# ─────────────────────────────────────────────────────────────────────────────

ALPHACORRECTION_ATTR = Attribute(size=20, fields=[
    Int("typeFlag"),
    Float("lowPass", label_zh="低通阈值"),  # 硬阈值裁切：低于此值的 alpha 直接归 0，取 0 表示不裁切
    Float("contrast_gamma", label_zh="对比度/伽马修正"),  # 越大则低/中 alpha 越快变透明，高 alpha 核心保留；无上限
    Float("unkn3"),
    Bool("unknFlag2"),
])
ALPHACORRECTION_SCHEMA = ALPHACORRECTION_ATTR.schema
assert _schema_size(ALPHACORRECTION_SCHEMA) == 20, \
    f"ALPHACORRECTION_SCHEMA size mismatch: {_schema_size(ALPHACORRECTION_SCHEMA)}"


LUMINANCEBLEED_ATTR = Attribute(size=16, fields=[
    Int("typeFlag"),  # 官方文件里恒为未初始化的 0xCDCDCDCD
    Float("bleed"),
    Float("colorScaler"),
    Float("texelScaler"),
])
LUMINANCEBLEED_SCHEMA = LUMINANCEBLEED_ATTR.schema
assert _schema_size(LUMINANCEBLEED_SCHEMA) == 16, \
    f"LUMINANCEBLEED_SCHEMA size mismatch: {_schema_size(LUMINANCEBLEED_SCHEMA)}"


# ─────────────────────────────────────────────────────────────────────────────
# Refraction
# ─────────────────────────────────────────────────────────────────────────────

REFRACTION_ATTR = Attribute(size=12, fields=[
    Int("typeFlag"),
    Enum("distortionType", ENUM_DISTORTION_TYPE, label_en="Distortion Type",
         label_zh="畸变方式"),
    Float("alphaBlend", label_en="Alpha Blend", label_zh="Alpha 混合"),
])
REFRACTION_SCHEMA = REFRACTION_ATTR.schema
assert _schema_size(REFRACTION_SCHEMA) == 12, \
    f"REFRACTION_SCHEMA size mismatch: {_schema_size(REFRACTION_SCHEMA)}"


# ─────────────────────────────────────────────────────────────────────────────
# Noise
# ─────────────────────────────────────────────────────────────────────────────

NOISE_ATTR = Attribute(size=44, fields=[
    Int("typeFlag"),
    Int("section_length", label_zh="段长度"),
    Int("spacer"),
    Float("lowFrequency", label_zh="低频"),
    Float("lowFrequencyJitter", label_zh="低频抖动"),
    Float("lowFrequencyWidth", label_zh="低频振幅"),
    Float("lowFrequencyWidthJitter", label_zh="低频振幅抖动"),
    Float("highFrequency", label_zh="高频"),
    Float("highFrequencyJitter", label_zh="高频抖动"),
    Float("highFrequencyWidth", label_zh="高频振幅"),
    Float("highFrequencyWidthJitter", label_zh="高频振幅抖动"),
])
NOISE_SCHEMA = NOISE_ATTR.schema
assert _schema_size(NOISE_SCHEMA) == 44, \
    f"NOISE_SCHEMA size mismatch: {_schema_size(NOISE_SCHEMA)}"


# ─────────────────────────────────────────────────────────────────────────────
# Guide
# ─────────────────────────────────────────────────────────────────────────────

GUIDE_ATTR = Attribute(size=112, fields=[
    Int("typeFlag"),  # 原按 float 解读的 initialPosition，官方文件里恒为 0
    Int("unkn1"),  # 游戏不读取，加载后恒为 1
    Float("speed", label_zh="初速度"),
    Float("speedJitter", label_zh="初速度偏差"),
    Float("accel", label_zh="加速度"),
    Float("accelJitter", label_zh="加速度抖动"),
    Float("innerRadius", label_zh="内半径"),
    Float("innerRadiusJitter", label_zh="内半径抖动"),
    Float("outerRadius", label_zh="外半径"),
    Float("outerRadiusJitter", label_zh="外半径抖动"),
    Float("restitutionDelay", label_zh="回弹延迟"),
    Float("restitutionDelayJitter", label_zh="回弹延迟抖动"),
    Float("restitutionEccentricity", label_zh="回弹偏心率"),
    Float("restitutionEccentricityJitter", label_zh="回弹偏心率抖动"),
    Float("restitutionElasticity", label_zh="回弹弹性"),
    Float("restitutionElasticityJitter", label_zh="回弹弹性抖动"),
    Float("unkn16"),
    Float("unkn17"),
    Float("unkn18"),
    Float("unkn19"),
    Float("unknFixed20"),
    Float("unkn21"),
    Float("unkn22"),
    Int("int_unkn1_0"),
    Int("int_unkn1_1"),
    Float("float_unkn2_0"),
    Float("float_unkn2_1"),
    Float("float_unkn2_2"),
])
GUIDE_SCHEMA = GUIDE_ATTR.schema
assert _schema_size(GUIDE_SCHEMA) == 112, \
    f"GUIDE_SCHEMA size mismatch: {_schema_size(GUIDE_SCHEMA)}"


# ─────────────────────────────────────────────────────────────────────────────
# PlEmissive
# ─────────────────────────────────────────────────────────────────────────────

PLEMISSIVE_ATTR = Attribute(size=76, fields=[
    Int("typeFlag"),
    Int("priority", label_zh="优先级"),
    Float("blend", label_zh="混合"),  # TIML DT 0x95A3A1D3("Blend")
    Byte("body_p", label_zh="关联 Body"),
    Byte("wp_p", label_zh="关联武器"),
    Short("NULL"),
    Int("correctColorNo", label_zh="EPV 颜色修正槽位"),  # EPV 槽位覆盖
    Raw("emissive", ('XYZ', 2), label_zh="自发光颜色"),  # TIML DT 0xFA79B1CD("Emissive")
    Float("intensity", label_zh="强度"),  # TIML DT 0x94BCC5CE("Intensity")
    Float("rimWidth", label_zh="边缘光宽度"),  # TIML DT 0xAC635CA9("RimWidth")
    Float("rimPower", label_zh="边缘光强度"),  # TIML DT 0x8BF31826("RimPower")
    Float("rimAlpha", label_zh="边缘光透明度"),  # TIML DT 0xF09920EC("RimAlpha")
    Bitmask("emitMaskFlags", BITS_PLEMISSIVE_EMIT_MASK, strict=True, label_zh="发光遮罩标志"),
    Float("mask0", label_zh="遮罩阈值 0"),  # TIML DT 0xEC4350B5("Mask0")
    Float("mask1", label_zh="遮罩阈值 1"),  # TIML DT 0x9B446023("Mask1")
    Bool("enableUseEmitMask", label_zh="启用发光遮罩"),  # Mask0/Mask1 的总开关
    Float("unknFixed5_0"),
    Float("addMask0"),  # 按位置推名，证据较弱
    Float("addMask1"),  # 按位置推名，证据较弱
    Float("unknFixed5_3"),
    Float("unknFixed5_4"),
])
PLEMISSIVE_SCHEMA = PLEMISSIVE_ATTR.schema
assert _schema_size(PLEMISSIVE_SCHEMA) == 76, \
    f"PLEMISSIVE_SCHEMA size mismatch: {_schema_size(PLEMISSIVE_SCHEMA)}"


# ─────────────────────────────────────────────────────────────────────────────
# ParentEmissive
# ─────────────────────────────────────────────────────────────────────────────
# ⚠ rimParam / blendParam 由数组拆成独立字段，属于拆分而非改名，别名表无法兼容。

PARENTEMISSIVE_ATTR = Attribute(size=72, fields=[
    Int("typeFlag"),
    Int("priority", label_zh="优先级"),
    Float("blend", label_zh="混合"),
    Int("correctColorNo", label_zh="EPV 颜色修正槽位"),  # EPV 槽位覆盖
    Raw("emissive", ('XYZ', 2), label_zh="自发光颜色"),
    Float("intensity", label_zh="强度"),  # 身份证据较弱
    Float("rimWidth", label_zh="边缘光宽度"),
    Float("rimPower", label_zh="边缘光强度"),
    Float("rimAlpha", label_zh="边缘光透明度"),
    Bitmask("emitMaskFlags", BITS_PLEMISSIVE_EMIT_MASK, strict=True, label_zh="发光遮罩标志"),
    Float("mask0", label_zh="遮罩阈值 0"),  # 身份证据较弱
    Float("mask1", label_zh="遮罩阈值 1"),  # 身份证据较弱
    Float("unkn7_2"),
    Float("unknFixed8_0"),
    Float("addMask0"),
    Float("addMask1"),
    Float("unknFixed8_3"),
    Float("unknFixed8_4"),
])
PARENTEMISSIVE_SCHEMA = PARENTEMISSIVE_ATTR.schema
assert _schema_size(PARENTEMISSIVE_SCHEMA) == 72, \
    f"PARENTEMISSIVE_SCHEMA size mismatch: {_schema_size(PARENTEMISSIVE_SCHEMA)}"


# ─────────────────────────────────────────────────────────────────────────────
# PlSnow
# ─────────────────────────────────────────────────────────────────────────────

PLSNOW_ATTR = Attribute(size=84, fields=[
    Int("typeFlag"),
    Int("unknFixed0_1"),
    Int("spacer"),
    Int("body_part_id", label_zh="身体部位 ID"),
    Int("weapon_id", label_zh="武器 ID"),
    Raw("color", 'colour', label_zh="颜色"),
    Int("epvcolorslot", label_zh="EPV 颜色修正槽位"),
    Int("alpha_effect", label_zh="透明度效果"),
    Float("normal_map_strength", label_zh="法线贴图强度"),
    Float("alpha_threshold", label_zh="透明度阈值"),
    Float("unkn4_0"),
    Float("unkn4_1"),
    Int("unkn5"),
    Float("roughness_multiplier", label_zh="粗糙度倍率"),
    Float("metallicness_multiplier", label_zh="金属度倍率"),
    Float("subsurface_multipler", label_zh="次表面倍率"),
    Float("unkn6_0"),
    Float("craquelure_effect_diffumination", label_zh="裂纹效果扩散"),
    Float("craquelure_threshold", label_zh="裂纹阈值"),
    Float("unkn6_1"),
    Float("craquelure_smoothing_threshold"),
])
PLSNOW_SCHEMA = PLSNOW_ATTR.schema
assert _schema_size(PLSNOW_SCHEMA) == 84, \
    f"PLSNOW_SCHEMA size mismatch: {_schema_size(PLSNOW_SCHEMA)}"


# ─────────────────────────────────────────────────────────────────────────────
# PtCollision
# ─────────────────────────────────────────────────────────────────────────────

PTCOLLISION_ATTR = Attribute(size=112, fields=[
    Int("typeFlag"),
    Enum("physicsEnum", ENUM_COLLISION_PHYSICS, label_zh="物理类型"),
    Int("unkn02"),
    Int("unkn03"),
    Int("unknEnum04"),
    Int("unknFixed05"),
    # 碰撞面沿 -Y 轴的投影偏移，正值向下、负值向上。
    Float("projectionOffset", label_zh="投影偏移"),
    Float("projectionDist", label_zh="投影距离"),
    Float("unkn1_0"),
    Float("unkn1_1"),
    Float("unkn1_2"),
    Int("bounceCount", label_zh="反弹次数"),  # 反弹满该次数后触发 physicsEnum 的收尾行为
    Int("bounceCountJitter", label_zh="反弹次数抖动"),
    Float("bounceElasticity", label_zh="弹跳弹性"),
    Float("bounceElasticityJitter", label_zh="弹跳弹性抖动"),
    # 与 bounceElasticity 叠加生效，不是倍率关系。
    Float("bounceElasticityMultiplier", label_zh="弹跳弹性倍率"),
    Float("horizontalBounce", label_zh="水平弹跳"),
    Float("unkn34"),
    Float("unkn35"),
    Float("unkn36"),
    Float("unkn37"),
    Enum("impactPlayTriggerMode", ENUM_IMPACT_PLAY_TRIGGER_MODE, label_zh="触地触发模式"),
    Int("impactPlayTriggerCount", label_zh="触地触发次数"),  # 不是位掩码，是次数 N，配合 impactPlayTriggerMode=1 使用
    Int("impactPlayTriggerCountJitter", label_zh="触地触发次数抖动"),
    Int("ieIndex", label_zh="碰撞触发 Play"),
    Int("unknEnum6_0"),
    Int("unknEnum6_1"),
    Int("unknFixed6_2"),
])
PTCOLLISION_SCHEMA = PTCOLLISION_ATTR.schema
assert _schema_size(PTCOLLISION_SCHEMA) == 112, \
    f"PTCOLLISION_SCHEMA size mismatch: {_schema_size(PTCOLLISION_SCHEMA)}"


# ─────────────────────────────────────────────────────────────────────────────
# RandomFix
# ─────────────────────────────────────────────────────────────────────────────

RANDOMFIX_ATTR = Attribute(size=40, fields=[
    Int("typeFlag"),
    Int("randomSeedTable0", label_zh="随机种子表 0"),
    Int("randomSeedTable1", label_zh="随机种子表 1"),
    Int("randomSeedTable2", label_zh="随机种子表 2"),
    Int("randomSeedTable3", label_zh="随机种子表 3"),
    Int("randomSeedTable4", label_zh="随机种子表 4"),
    Int("randomSeedTable5", label_zh="随机种子表 5"),
    Int("randomSeedTable6", label_zh="随机种子表 6"),
    Int("randomSeedTable7", label_zh="随机种子表 7"),
    Bitmask("tableSelectionGroup", BITS_RANDOMFIX_TABLE, label_zh="种子表选择组"),
])
RANDOMFIX_SCHEMA = RANDOMFIX_ATTR.schema
assert _schema_size(RANDOMFIX_SCHEMA) == 40, \
    f"RANDOMFIX_SCHEMA size mismatch: {_schema_size(RANDOMFIX_SCHEMA)}"


# ─────────────────────────────────────────────────────────────────────────────
# Dummy
# ─────────────────────────────────────────────────────────────────────────────

DUMMY_ATTR = Attribute(size=9, fields=[
    Int("typeFlag"),
    Int("section_length", label_zh="段长度"),
    Byte("unknFixed1"),
])
DUMMY_SCHEMA = DUMMY_ATTR.schema
assert _schema_size(DUMMY_SCHEMA) == 9, \
    f"DUMMY_SCHEMA size mismatch: {_schema_size(DUMMY_SCHEMA)}"


# ─────────────────────────────────────────────────────────────────────────────
# ExternReference
# ─────────────────────────────────────────────────────────────────────────────

EXTERNREFERENCE_ATTR = Attribute(size=36, fields=[
    Int("typeFlag"),
    Int("referenceIndex", label_zh="Extern 引用"),
    Bitmask("trigger_condition", BITS_EXTERNREF_TRIGGER, strict=True, gate_first=True,
            label_zh="触发条件"),
    Int("index0", label_zh="索引 0"),
    Int("index1", label_zh="索引 1"),
    Float("lerp", label_zh="插值系数"),
    Int("transitionDuration", label_zh="过渡时长（帧）"),
    Int("triggerDelay", label_zh="触发延迟（帧）"),
    Bool("unknFlag1_6"),
])
EXTERNREFERENCE_SCHEMA = EXTERNREFERENCE_ATTR.schema
assert _schema_size(EXTERNREFERENCE_SCHEMA) == 36, \
    f"EXTERNREFERENCE_SCHEMA size mismatch: {_schema_size(EXTERNREFERENCE_SCHEMA)}"


# ─────────────────────────────────────────────────────────────────────────────
# PtLife
# ─────────────────────────────────────────────────────────────────────────────

PTLIFE_ATTR = Attribute(size=20, fields=[
    Short("typeFlag"),
    Short("unknFixed1"),
    Enum("status", ENUM_PTLIFE_STATUS, backing='h', label_en="Trigger On", label_zh="触发条件"),
    Short("unknEnum3"),
    Short("relationIndex", label_zh="关联 Play"),
    # -1 时 relationIndex 不生效
    Enum("useRelation", ENUM_PTLIFE_RELATION, backing='h', label_en="Relation", label_zh="关联开关"),
    # unknFrame0/1 及其 Jitter 的名字按配对惯例取的，未确认是帧数，不要按名字推断语义。
    Short("unknFrame0"),
    Short("unknFrame0Jitter"),
    Short("unknFrame1"),
    Short("unknFrame1Jitter"),
])
PTLIFE_SCHEMA = PTLIFE_ATTR.schema
assert _schema_size(PTLIFE_SCHEMA) == 20, \
    f"PTLIFE_SCHEMA size mismatch: {_schema_size(PTLIFE_SCHEMA)}"


# ─────────────────────────────────────────────────────────────────────────────
# EmitterBoundary
# ─────────────────────────────────────────────────────────────────────────────

# 与 UnitBoundary 同构：两者各 float 随 boundaryType 取用的模式一致，
# 但文件内的 UnitBoundary 不是 EmitterBoundary 的汇总，各自独立填值。
EMITTERBOUNDARY_ATTR = Attribute(size=40, fields=[
    Int("typeFlag"),
    Enum("boundaryType", ENUM_UNITBOUNDARY_TYPE, label_zh="边界类型?"),
    Float("radius", label_zh="半径?"),
    Raw("boundaryMin", ('XYZ', 3), label_zh="边界最小角?"),
    Raw("boundaryMax", ('XYZ', 3), label_zh="边界最大角?"),
    Float("radius2", label_zh="次级半径?"),
])
EMITTERBOUNDARY_SCHEMA = EMITTERBOUNDARY_ATTR.schema
assert _schema_size(EMITTERBOUNDARY_SCHEMA) == 40, \
    f"EMITTERBOUNDARY_SCHEMA size mismatch: {_schema_size(EMITTERBOUNDARY_SCHEMA)}"


# ─────────────────────────────────────────────────────────────────────────────
# FadeByAngle
# ─────────────────────────────────────────────────────────────────────────────

FADEBYANGLE_ATTR = Attribute(size=40, fields=[
    Int("typeFlag"),
    Bitmask("coneVisibilityFlags", BITS_FADEBYANGLE_FLAGS, label_zh="锥体可见性标志", strict=True),
    Float("cutoffConeAngle", label_zh="完全消失锥角"),
    Float("fadeConeAngle", label_zh="渐隐锥角"),
    Float("minAlpha", label_zh="最小透明度"),
    Raw("rotation", ('XYZ', 3), label_zh="旋转"),
    Enum("baseAxis", _AXIS_DIRECTION6, label_zh="基准轴"),
    Enum("rotOrder", _ROT_ORDER6, label_zh="旋转顺序"),
])
FADEBYANGLE_SCHEMA = FADEBYANGLE_ATTR.schema
assert _schema_size(FADEBYANGLE_SCHEMA) == 40, \
    f"FADEBYANGLE_SCHEMA size mismatch: {_schema_size(FADEBYANGLE_SCHEMA)}"


# ─────────────────────────────────────────────────────────────────────────────
# MasterOnly
# ─────────────────────────────────────────────────────────────────────────────

MASTERONLY_ATTR = Attribute(size=4, fields=[
    Int("typeFlag"),
])
MASTERONLY_SCHEMA = MASTERONLY_ATTR.schema
assert _schema_size(MASTERONLY_SCHEMA) == 4, \
    f"MASTERONLY_SCHEMA size mismatch: {_schema_size(MASTERONLY_SCHEMA)}"


# ─────────────────────────────────────────────────────────────────────────────
# Blink
# ─────────────────────────────────────────────────────────────────────────────

BLINK_ATTR = Attribute(size=52, fields=[
    Int("typeFlag"),
    Int("section_length", label_zh="段长度"),
    Float("unkn1_0"),  # 低字节是 bool，其余三字节是保留填充
    Float("minAlphaRate", label_zh="最小透明度强度"),
    Float("maxAlphaRate", label_zh="最大透明度强度"),
    Float("lowFrequency", label_zh="低频"),
    Float("lowFrequencyJitter", label_zh="低频抖动"),
    Float("lowFrequencyWidth", label_zh="低频振幅"),
    Float("lowFrequencyWidthJitter", label_zh="低频振幅抖动"),
    Float("highFrequency", label_zh="高频"),
    Float("highFrequencyJitter", label_zh="高频抖动"),
    Float("highFrequencyWidth", label_zh="高频振幅"),
    Float("highFrequencyWidthJitter", label_zh="高频振幅抖动"),
])
BLINK_SCHEMA = BLINK_ATTR.schema
assert _schema_size(BLINK_SCHEMA) == 52, \
    f"BLINK_SCHEMA size mismatch: {_schema_size(BLINK_SCHEMA)}"


# ─────────────────────────────────────────────────────────────────────────────
# FadeByEmitterAngle
# ─────────────────────────────────────────────────────────────────────────────
# ⚠ outerConeAngle / innerConeAngle 的身份未经确认，不要按名字推断行为。

FADEBYEMITTERANGLE_ATTR = Attribute(size=28, fields=[
    Int("typeFlag"),
    Int("section_length", label_zh="段长度"),
    Int("unkn"),  # 低字节是 bool，其余三字节是保留填充
    Float("outerConeAngle", label_zh="外锥角"),
    Float("innerConeAngle", label_zh="内锥角"),
    Float("fadeInStart", label_zh="淡入起点"),
    Float("fadeInEnd", label_zh="淡入终点"),
])
FADEBYEMITTERANGLE_SCHEMA = FADEBYEMITTERANGLE_ATTR.schema
assert _schema_size(FADEBYEMITTERANGLE_SCHEMA) == 28, \
    f"FADEBYEMITTERANGLE_SCHEMA size mismatch: {_schema_size(FADEBYEMITTERANGLE_SCHEMA)}"


# ─────────────────────────────────────────────────────────────────────────────
# RayCast
# ─────────────────────────────────────────────────────────────────────────────
# rayCastID 只观测到单一取值，保留原始 int，不建 Enum。

RAYCAST_ATTR = Attribute(size=78, fields=[
    Int("typeFlag"),
    Int("section_length", label_zh="段长度"),
    Int("spacer0"),
    Float("maxDistance", label_zh="最大距离"),
    Float("maxDistanceJitter", label_zh="最大距离抖动"),
    Float("startDistance", label_zh="起始距离"),
    Float("startDistanceJitter", label_zh="起始距离抖动"),
    Int("spacer1"),
    Int("spacer2"),
    Int("spacer3"),
    Float("prop2", label_zh="属性2"),
    Raw("startOffset", ('XYZ', 3), label_zh="起始偏移"),
    Enum("direction", ENUM_RAYCAST_DIR, label_zh="方向"),
    Float("speed", label_zh="速度"),
    Float("speedJitter", label_zh="速度抖动"),
    Bitmask("rayCastAttr", BITS_RAYCAST_ATTR, strict=True, label_zh="射线属性"),
    Enum("rayCastID", ENUM_RAYCAST_ID, label_zh="RayCast ID"),  # 只列已观测到的取值
    Bitmask("rayCastFlags", BITS_RAYCAST_FLAGS, backing='h', strict=True, label_zh="射线标志位"),
])
RAYCAST_SCHEMA = RAYCAST_ATTR.schema
assert _schema_size(RAYCAST_SCHEMA) == 78, \
    f"RAYCAST_SCHEMA size mismatch: {_schema_size(RAYCAST_SCHEMA)}"


# ─────────────────────────────────────────────────────────────────────────────
# Homing
# ─────────────────────────────────────────────────────────────────────────────

HOMING_ATTR = Attribute(size=52, fields=[
    Int("typeFlag"),
    Int("section_length", label_zh="段长度"),
    Int("spacer"),
    Float("turnRate", label_zh="转向速率"),  # 单位为度/秒
    Float("acceleration", label_zh="加速度"),  # 每秒增加的速度
    Float("maxSpeed", label_zh="最大速度"),
    Float("forceFieldSpeedScale", label_zh="作用场速度倍率"),
    Float("vanishRadius", label_zh="消失半径"),
    Float("forceFieldRadius", label_zh="作用场半径"),
    Enum("homingTarget", ENUM_HOMING_TARGET, label_zh="归航目标"),
    Enum("vanishMode", ENUM_HOMING_VANISH, label_zh="消失模式"),
    Enum("forceFieldMode", ENUM_HOMING_FORCEFIELD, label_zh="作用场模式"),
    Int("unknownEnum1"),
])
HOMING_SCHEMA = HOMING_ATTR.schema
assert _schema_size(HOMING_SCHEMA) == 52, \
    f"HOMING_SCHEMA size mismatch: {_schema_size(HOMING_SCHEMA)}"


# ─────────────────────────────────────────────────────────────────────────────
# ScreenSpaceCollision
# ─────────────────────────────────────────────────────────────────────────────

SCREENSPACECOLLISION_ATTR = Attribute(size=36, fields=[
    Int("typeFlag"),
    Int("section_length", label_zh="段长度"),
    Int("spacer"),
    Int("unknEnum1"),
    Float("bounce", label_zh="弹跳"),
    Float("bounceJitter", label_zh="弹跳抖动"),
    Int("lifespan", label_zh="寿命"),
    Int("lifespanJitter", label_zh="寿命抖动"),
    Float("bounceConditional", label_zh="条件弹跳"),
])
SCREENSPACECOLLISION_SCHEMA = SCREENSPACECOLLISION_ATTR.schema
assert _schema_size(SCREENSPACECOLLISION_SCHEMA) == 36, \
    f"SCREENSPACECOLLISION_SCHEMA size mismatch: {_schema_size(SCREENSPACECOLLISION_SCHEMA)}"


# ─────────────────────────────────────────────────────────────────────────────
# Shovel
# ─────────────────────────────────────────────────────────────────────────────

SHOVEL_ATTR = Attribute(size=70, fields=[
    Int("typeFlag"),
    Int("section_length", label_zh="段长度"),
    Int("spacer"),
    Float("width", label_zh="宽度"),
    Float("widthJitter", label_zh="宽度抖动"),
    Float("height", label_zh="高度"),
    Float("heightJitter", label_zh="高度抖动"),
    Float("length", label_zh="长度"),
    Float("lengthJitter", label_zh="长度抖动"),
    Float("unkn09"),
    Float("unkn10"),
    Float("unkn11"),
    Int("unknFixed12"),
    Int("unknEnum13"),
    Int("unknEnum14"),
    Int("pattern", label_zh="图案"),
    Int("unknBitmask16"),
    Short("unknEnum17"),
])
SHOVEL_SCHEMA = SHOVEL_ATTR.schema
assert _schema_size(SHOVEL_SCHEMA) == 70, \
    f"SHOVEL_SCHEMA size mismatch: {_schema_size(SHOVEL_SCHEMA)}"


# ─────────────────────────────────────────────────────────────────────────────
# UVControl
# ─────────────────────────────────────────────────────────────────────────────

UVCONTROL_ATTR = Attribute(size=236, fields=[
    Int("typeFlag"),
    # uv1 Material_Animation_Data
    Raw("uv1_offset", ('f', 4), label_zh="UV1 初始位置"),
    Raw("uv1_offsetAdd", ('f', 4), label_zh="UV1 速度"),
    Raw("uv1_offsetCoef", ('f', 4), label_zh="UV1 加速度"),
    Raw("uv1_scale", ('f', 4), label_zh="UV1 缩放"),
    Raw("uv1_scaleAdd", ('f', 4), label_zh="UV1 缩放速度"),
    Raw("uv1_scaleCoef", ('f', 4), label_zh="UV1 缩放加速度"),
    # uv2 Material_Animation_Data
    Bool("uv2_enable"),
    Raw("uv2_offset", ('f', 4), label_zh="UV2 初始位置"),
    Raw("uv2_offsetAdd", ('f', 4), label_zh="UV2 速度"),
    Raw("uv2_offsetCoef", ('f', 4), label_zh="UV2 加速度"),
    Raw("uv2_scale", ('f', 4), label_zh="UV2 缩放"),
    Raw("uv2_scaleAdd", ('f', 4), label_zh="UV2 缩放速度"),
    Raw("uv2_scaleCoef", ('f', 4), label_zh="UV2 缩放加速度"),
    # flowmap 组，与 RIBBON / BILLBOARD3D / PLANE 等同款
    Bool("enableFlowmap", label_zh="启用流动贴图"),
    Float("flowSpeed", label_zh="流动贴图速度"),
    Float("flowSpeedJitter", label_zh="流动贴图速度抖动"),
    Float("flowSpeedCoef", label_zh="流动贴图加速度"),
    Float("flowSpeedCoefJitter", label_zh="流动贴图加速度抖动"),
    Float("flowStrength", label_zh="流动贴图强度"),
    Float("flowStrengthJitter", label_zh="流动贴图强度抖动"),
    Float("flowStrengthCoef", label_zh="流动贴图强度加速度"),
    Float("flowStrengthCoefJitter", label_zh="流动贴图强度加速度抖动"),
])
UVCONTROL_SCHEMA = UVCONTROL_ATTR.schema
assert _schema_size(UVCONTROL_SCHEMA) == 236, \
    f"UVCONTROL_SCHEMA size mismatch: {_schema_size(UVCONTROL_SCHEMA)}"


EMITTERSHAPE2D_ATTR = Attribute(size=36, fields=[
    Int("typeFlag"),
    # rangeX/Y 与其 *Jitter 实为 offset/size 一对（外边界=offset+size），不是固定/随机。
    # *Jitter 这个 ori_name 不能改（会波及预设与已导入的 .blend），UI 措辞由
    # panels.py::_OFFSET_SIZE_PAIRS 覆盖成 偏移/尺寸。
    Float("rangeX", label_zh="生成范围 X"),
    Float("rangeXJitter", label_zh="生成范围 X 抖动"),
    Float("rangeY", label_zh="生成范围 Y"),
    Float("rangeYJitter", label_zh="生成范围 Y 抖动"),
    Enum("shapeType", ENUM_SHAPE_TYPE2D, label_zh="形状类型"),
    Int("rangeDivideHorizontalNum", label_zh="横向等分数量"),
    # 必须用 2D 专属枚举：编号与 3D 版不同（3D 是 0=X/1=Z/2=Y，2D 是 0=Y/1=X）。
    # 官方语料实测出现过取值 2，已作为 Unknown (2) 补进枚举（见 enums.py）。
    Enum("rangeDivideAxis", ENUM_RANGE_DIVIDE_AXIS_2D, label_zh="细分轴向"),
    Int("unknFixed22_1"),
])
EMITTERSHAPE2D_SCHEMA = EMITTERSHAPE2D_ATTR.schema
assert _schema_size(EMITTERSHAPE2D_SCHEMA) == 36, \
    f"EMITTERSHAPE2D_SCHEMA size mismatch: {_schema_size(EMITTERSHAPE2D_SCHEMA)}"

VELOCITY2D_ATTR = Attribute(size=72, fields=[
    Int("typeFlag"),
    Float("rotation", label_zh="旋转"),
    Float("rotationJitter", label_zh="旋转抖动"),
    Float("speed", label_zh="初速度"),
    Float("speedJitter", label_zh="初速度偏差"),
    Float("speedCoef", label_zh="加速度"),
    Float("speedCoefJitter", label_zh="加速度偏差"),
    Float("velocityX", label_zh="X 基准点偏置"),
    Float("velocityY", label_zh="Y 基准点偏置"),
    Float("divergenceX", label_zh="X 基准点伸缩"),
    Float("divergenceY", label_zh="Y 基准点伸缩"),
    Enum("velocityType", _VELOCITY_TYPE, label_zh="速度类型"),  # 枚举语义见 VELOCITY3D
    Float("gravity", label_zh="重力"),
    Float("gravityJitter", label_zh="重力抖动"),
    Int("movementDelay", label_zh="运动延迟"),
    Int("movementDelayJitter", label_zh="运动延迟抖动"),
    Int("gravityDelay", label_zh="重力延迟"),
    Int("gravityDelayJitter", label_zh="重力延迟抖动"),
])
VELOCITY2D_SCHEMA = VELOCITY2D_ATTR.schema
assert _schema_size(VELOCITY2D_SCHEMA) == 72, \
    f"VELOCITY2D_SCHEMA size mismatch: {_schema_size(VELOCITY2D_SCHEMA)}"


# ─────────────────────────────────────────────────────────────────────────────
# 以下类型此前按 opaque 处理，现已定长 schema 化；多数字段语义未知，名字保持 unknN。
# ─────────────────────────────────────────────────────────────────────────────

# PathChain
PATHCHAIN_ATTR = Attribute(size=77, fields=[
    Int("typeFlag"),
    Int("section_length", label_zh="段长度"),
    Int("unkn1"),
    Float("unkn2"),
    Int("unknEnum3"),
    Raw("unkn4", ('XYZ', 0)),
    Enum("baseAxis", _AXIS_DIRECTION6, label_zh="基准轴?"),
    Float("rotationX", label_zh="X 旋转?"),
    Float("rotationXJitter", label_zh="X 旋转抖动?"),
    Float("rotationY", label_zh="Y 旋转?"),
    Float("rotationYJitter", label_zh="Y 旋转抖动?"),
    Float("rotationZ", label_zh="Z 旋转?"),
    Float("rotationZJitter", label_zh="Z 旋转抖动?"),
    Int("unknEnum5_7"),
    Bool("unknFlag6", backing='b'),
])
PATHCHAIN_SCHEMA = PATHCHAIN_ATTR.schema
assert _schema_size(PATHCHAIN_SCHEMA) == 77, \
    f"PATHCHAIN_SCHEMA size mismatch: {_schema_size(PATHCHAIN_SCHEMA)}"

# PtTrigger
PTTRIGGER_ATTR = Attribute(size=16, fields=[
    Int("typeFlag"),
    Int("section_length", label_zh="段长度"),
    Int("unkn1"),
    Int("unknEnum2"),
])
PTTRIGGER_SCHEMA = PTTRIGGER_ATTR.schema
assert _schema_size(PTTRIGGER_SCHEMA) == 16, \
    f"PTTRIGGER_SCHEMA size mismatch: {_schema_size(PTTRIGGER_SCHEMA)}"

# LinkPartsVisible
LINKPARTSVISIBLE_ATTR = Attribute(size=12, fields=[
    Int("typeFlag"),
    Int("unknFixed0_1"),
    Int("unknEnum0_2"),
])
LINKPARTSVISIBLE_SCHEMA = LINKPARTSVISIBLE_ATTR.schema
assert _schema_size(LINKPARTSVISIBLE_SCHEMA) == 12, \
    f"LINKPARTSVISIBLE_SCHEMA size mismatch: {_schema_size(LINKPARTSVISIBLE_SCHEMA)}"

# SpawnByAngle
SPAWNBYANGLE_ATTR = Attribute(size=22, fields=[
    Int("typeFlag"),
    Int("section_length", label_zh="段长度"),
    Int("unkn1"),
    Float("unkn2"),
    Int("unknEnum3"),
    Short("unknFixed4"),
])
SPAWNBYANGLE_SCHEMA = SPAWNBYANGLE_ATTR.schema
assert _schema_size(SPAWNBYANGLE_SCHEMA) == 22, \
    f"SPAWNBYANGLE_SCHEMA size mismatch: {_schema_size(SPAWNBYANGLE_SCHEMA)}"

# CheckPureAttribute
CHECKPUREATTRIBUTE_ATTR = Attribute(size=40, fields=[
    Int("typeFlag"),
    Int("section_length", label_zh="段长度"),
    Int("unkn1"),
    Int("unknEnum2_0"),
    Int("unknEnum2_1"),
    Int("unknEnum2_2"),
    Int("unknEnum2_3"),
    Int("unknEnum2_4"),
    Int("unknEnum2_5"),
    Int("unknFixed2_6"),
])
CHECKPUREATTRIBUTE_SCHEMA = CHECKPUREATTRIBUTE_ATTR.schema
assert _schema_size(CHECKPUREATTRIBUTE_SCHEMA) == 40, \
    f"CHECKPUREATTRIBUTE_SCHEMA size mismatch: {_schema_size(CHECKPUREATTRIBUTE_SCHEMA)}"

# SpawnByOcclusion
SPAWNBYOCCLUSION_ATTR = Attribute(size=20, fields=[
    Int("typeFlag"),
    Int("section_length", label_zh="段长度"),
    Int("unkn1"),
    Float("unknFixed2"),
    Int("unknFixed3"),
])
SPAWNBYOCCLUSION_SCHEMA = SPAWNBYOCCLUSION_ATTR.schema
assert _schema_size(SPAWNBYOCCLUSION_SCHEMA) == 20, \
    f"SPAWNBYOCCLUSION_SCHEMA size mismatch: {_schema_size(SPAWNBYOCCLUSION_SCHEMA)}"

# FadeByOcclusion
FADEBYOCCLUSION_ATTR = Attribute(size=24, fields=[
    Int("typeFlag"),
    Int("section_length", label_zh="段长度"),
    Int("spacer0"),
    Float("occlusionRadius", label_zh="遮挡判定半径"),
    Float("minScale", label_zh="最小缩放比例"),
    Float("minAlpha", label_zh="最小透明度"),
])
FADEBYOCCLUSION_SCHEMA = FADEBYOCCLUSION_ATTR.schema
assert _schema_size(FADEBYOCCLUSION_SCHEMA) == 24, \
    f"FADEBYOCCLUSION_SCHEMA size mismatch: {_schema_size(FADEBYOCCLUSION_SCHEMA)}"

# ParentMaterial
PARENTMATERIAL_ATTR = Attribute(size=12, fields=[
    Int("typeFlag"),
    Int("unknFixed0_1"),
    Float("unknFixed1"),
])
PARENTMATERIAL_SCHEMA = PARENTMATERIAL_ATTR.schema
assert _schema_size(PARENTMATERIAL_SCHEMA) == 12, \
    f"PARENTMATERIAL_SCHEMA size mismatch: {_schema_size(PARENTMATERIAL_SCHEMA)}"

# Transform2D
TRANSFORM2D_ATTR = Attribute(size=24, fields=[
    Int("typeFlag"),
    Float("offsetX", label_zh="X 偏移"),
    Float("offsetY", label_zh="Y 偏移"),
    Float("rotation", label_zh="旋转"),
    Float("scaleX"),
    Float("scaleY"),
])
TRANSFORM2D_SCHEMA = TRANSFORM2D_ATTR.schema
assert _schema_size(TRANSFORM2D_SCHEMA) == 24, \
    f"TRANSFORM2D_SCHEMA size mismatch: {_schema_size(TRANSFORM2D_SCHEMA)}"

# ColorCorrectFilter
COLORCORRECTFILTER_ATTR = Attribute(size=688, fields=[
    Int("typeFlag"),
    Int("unknEnum0_1"),
    Int("unknFixed0_2"),
    Int("unknFixed0_3"),
    Raw("unkn1", ('f', 168)),
])
COLORCORRECTFILTER_SCHEMA = COLORCORRECTFILTER_ATTR.schema
assert _schema_size(COLORCORRECTFILTER_SCHEMA) == 688, \
    f"COLORCORRECTFILTER_SCHEMA size mismatch: {_schema_size(COLORCORRECTFILTER_SCHEMA)}"

# ParentSnow
PARENTSNOW_ATTR = Attribute(size=80, fields=[
    Int("typeFlag"),
    Int("section_length", label_zh="段长度"),
    Int("unkn1"),
    Int("unknFixed2"),
    Raw("color", ('XYZ', 2), label_zh="颜色"),
    Int("unknEnum3_0"),
    Int("unkn3_1"),
    Float("unkn4_0"),
    Float("unkn4_1"),
    Float("unkn4_2"),
    Float("unkn4_3"),
    Float("unkn4_4"),
    Float("unkn4_5"),
    Float("unknFlag4_6"),
    Float("unkn4_7"),
    Float("unkn4_8"),
    Float("unkn4_9"),
    Float("unkn4_10"),
    Float("unknFixed4_11"),
    Float("unkn4_12"),
])
PARENTSNOW_SCHEMA = PARENTSNOW_ATTR.schema
assert _schema_size(PARENTSNOW_SCHEMA) == 80, \
    f"PARENTSNOW_SCHEMA size mismatch: {_schema_size(PARENTSNOW_SCHEMA)}"

OTOMOSNOW_ATTR = Attribute(size=84, fields=[
    Int("typeFlag"),
    Int("section_length", label_zh="段长度"),
    Int("unkn1"),
    Int("unknFixed2_0"),
    Int("unknFixed2_1"),
    Raw("color", ('XYZ', 2), label_zh="颜色"),
    Int("unknEnum3"),
    Int("unkn4"),
    Float("unknFixed5_0"),
    Float("unknFixed5_1"),
    Float("unknFixed5_2"),
    Float("unknFixed5_3"),
    Int("unkn6"),
    Float("unknFixed7_0"),
    Float("unkn7_1"),
    Float("unknFixed7_2"),
    Float("unkn7_3"),
    Float("unkn7_4"),
    Float("unknFixed7_5"),
    Float("unknFixed7_6"),
    Float("unkn7_7"),
])
OTOMOSNOW_SCHEMA = OTOMOSNOW_ATTR.schema
assert _schema_size(OTOMOSNOW_SCHEMA) == 84, \
    f"OTOMOSNOW_SCHEMA size mismatch: {_schema_size(OTOMOSNOW_SCHEMA)}"

# FakePlane
FAKEPLANE_ATTR = Attribute(size=60, fields=[
    Int("typeFlag"),
    Int("section_length", label_zh="段长度"),
    SByte("unknFixed1_0"),
    Bool("unknFlag1_1", backing='b'),
    Bool("unknFlag1_2", backing='b'),
    Bool("unknFlag1_3", backing='b'),
    Float("unkn2"),
    Int("unknEnum3"),
    Int("unkn4"),
    Float("unkn5_0"),
    Float("unkn5_1"),
    Float("unkn5_2"),
    Float("unknFixed5_3"),
    Float("unkn5_4"),
    Float("unkn5_5"),
    Float("unkn5_6"),
    Float("unknFixed5_7"),
    Int("unknEnum5_8"),
])
FAKEPLANE_SCHEMA = FAKEPLANE_ATTR.schema
assert _schema_size(FAKEPLANE_SCHEMA) == 60, \
    f"FAKEPLANE_SCHEMA size mismatch: {_schema_size(FAKEPLANE_SCHEMA)}"

# RepeatArea
REPEATAREA_ATTR = Attribute(size=52, fields=[
    Int("typeFlag"),
    Int("section_length", label_zh="段长度"),  # 自描述的剩余字节数标记，不是可调参数
    Raw("unkn2", ('b', 16)),  # 保留填充（0xCD 占位）
    Float("unkn3_0"),
    Float("unkn3_1"),
    Float("unknFixed3_2"),
    Float("unknFixed3_3"),
    Float("unkn3_4"),
    Float("unkn3_5"),
    Int("unknEnum4"),
])
REPEATAREA_SCHEMA = REPEATAREA_ATTR.schema
assert _schema_size(REPEATAREA_SCHEMA) == 52, \
    f"REPEATAREA_SCHEMA size mismatch: {_schema_size(REPEATAREA_SCHEMA)}"


# FakeDoF：定长，无可选尾巴。
FAKEDOF_ATTR = Attribute(size=32, fields=[
    Int("typeFlag"),
    Int("section_length", label_zh="段长度"),  # 自描述的剩余字节数标记，不是可调参数
    Int("unkn2"),  # 保留填充（0xCD 占位）
    Float("unkn3_0"),
    Float("unkn3_1"),
    Float("unkn4"),
    Int("unknBitmask5"),
    Int("unknFixed6"),
])
FAKEDOF_SCHEMA = FAKEDOF_ATTR.schema
assert _schema_size(FAKEDOF_SCHEMA) == 32, \
    f"FAKEDOF_SCHEMA size mismatch: {_schema_size(FAKEDOF_SCHEMA)}"


# UnitBoundary
# Root 专属子条目，被包成 AttrBlock 才能套用这份 schema（见 io_tree.py）。
# ⚠ 字段名与枚举取值均未确认，标签保留 ? 标记。
UNITBOUNDARY_ATTR = Attribute(size=40, fields=[
    Int("unkn0"),  # 含义未知，与 boundaryType 无关联
    Enum("boundaryType", ENUM_UNITBOUNDARY_TYPE, label_zh="边界类型?"),
    Float("radius", label_zh="半径?"),
    Raw("boundaryMin", ('XYZ', 3), label_zh="边界最小角?"),
    Raw("boundaryMax", ('XYZ', 3), label_zh="边界最大角?"),
    Float("radius2", label_zh="次级半径?"),
])
UNITBOUNDARY_SCHEMA = UNITBOUNDARY_ATTR.schema
assert _schema_size(UNITBOUNDARY_SCHEMA) == 40, \
    f"UNITBOUNDARY_SCHEMA size mismatch: {_schema_size(UNITBOUNDARY_SCHEMA)}"
