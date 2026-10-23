# -*- coding: utf-8 -*-
"""schema 字段的中英文显示标签与回退查询。

维护约束：
- typed Attribute 的 ``Field.label_zh`` 优先；本模块只补充 custom-codec 字段和按类型覆盖。
- 查询顺序为 Field、类型专属表、全局表；未命中返回 ``None``，由 Blender 层生成友好英文名。
- 冻结英文标签只用于内部字段改名后保持既有 UI 文案，不能用于表达新的字段语义。
"""

from .fields_model import FIELD_REGISTRY
from ..hashes import NAME_TO_HASH


# custom-codec 字段的跨类型通用中文名
_LABELS_GLOBAL = {
    'rotationOrder': '旋转顺序',
    'direction': '方向',
    'rotation': '旋转',
    'rotationJitter': '旋转抖动',
    'color': '颜色',
    'color1': '颜色1',
    'color2': '颜色2',
    'colorRange': '颜色范围',
    'useColorRange': '启用颜色范围',
    'emissiveColor': '自发光颜色',
    'emissiveColorRange': '自发光颜色范围',
    'useEmissiveColor': '启用自发光颜色',
    'useEmissiveColorRange': '启用自发光颜色范围',
    'useEmission': '启用自发光',
    'enableIntensity1': '亮度增强1',
    'enableIntensity2': '亮度增强2',
    'enableEmissiveIntensity': '自发光亮度增强',
    'disableAllColorRange': '禁用所有颜色范围',
    'colorRate': '颜色强度',
    'colorRateJitter': '颜色强度抖动',
    'emissiveColorRate': '自发光强度',
    'emissiveColorRateJitter': '自发光强度抖动',
    'brightness': '亮度',
    'opacity': '不透明度',
    # 共用字段的通用标签；类型专属表和 Field 标签可覆盖
    'enableFlowmap': '启用流动贴图',
    'flowSpeed': '流动贴图速度',
    'flowSpeedJitter': '流动贴图速度抖动',
    'flowSpeedCoef': '流动贴图加速度',
    'flowSpeedCoefJitter': '流动贴图加速度抖动',
    'flowStrength': '流动贴图强度',
    'flowStrengthJitter': '流动贴图强度抖动',
    'flowStrengthCoef': '流动贴图强度加速度',
    'flowStrengthCoefJitter': '流动贴图强度加速度抖动',
    'playSpeed': '动画速度',
    'width': '宽度',
    'widthJitter': '宽度抖动',
    'height': '高度',
    'heightJitter': '高度抖动',
    'length': '长度',
    'lengthJitter': '长度抖动',
    'section_length': '段长度',
    'visiblePreview': '可见性修正',
    'blendMode': '混合模式',
    'subdivisionCount': '细分数量',
    'restoreStrength': '归位强度',
    'restoreStrengthJitter': '归位强度抖动',
    'inertia': '惯性',
    'inertiaJitter': '惯性抖动',
    'springiness': '弹性',
    'springiness_jitter': '弹性抖动',
    'brightnessJitter': '亮度抖动',
    # RIBBON 的两组可叠加抖动参数
    'flap1Frequency': '抖动1 频率',
    'flap1FrequencyJitter': '抖动1 频率抖动',
    'flap1Amount': '抖动1 幅度',
    'flap1AmountJitter': '抖动1 幅度抖动',
    'flap2Frequency': '抖动2 频率',
    'flap2FrequencyJitter': '抖动2 频率抖动',
    'flap2Amount': '抖动2 幅度',
    'flap2AmountJitter': '抖动2 幅度抖动',
    # RIBBON 两端的宽度与渐隐参数
    'base_width_multiplier': '后端宽度乘数',
    'base_opacity': '后端不透明度',
    'base_fade_length': '后端渐隐长度',
    'tip_width_multiplier': '前端宽度乘数',
    'tip_opacity': '前端不透明度',
    'tip_fade_length': '前端渐隐长度',
    # RIBBON 柔体链的重力
    'gravityX': '重力 X',
    'gravityY': '重力 Y（竖直）',
    'gravityZ': '重力 Z',
    'loopingOrientation': '贴图朝向',
    'loopingPad': '保留',

    # MESH / STRAINRIBBON 共用的 EPV 颜色修正槽位
    'epv_color_slot1': 'EPV 颜色修正槽位',
    'epv_color_slot2': 'EPV 颜色修正槽位',

    # 路径槽由 Blender 层按位置读写，名称在全项目唯一
    'flowmapPath':  '流动贴图',
    'cubemapPath':  '环境反射贴图',
    'albedoPath':   '基础色贴图',
    'lutPath':      '色彩查找表',
    'uvsPath':      'UVS 序列',
    'tfaPath':      '噪声场（.tfa）',
    'mod3Path':     '模型（.mod3）',
    'plPath':       '摆位表（.pl）',
}

# custom-codec 字段的类型专属中文名，优先于全局表
_LABELS_BY_TYPE = {
    # ── MESH ──
    ('MESH', 'playSpeed'): '播放速度',
    ('MESH', 'playSpeedJitter'): '播放速度抖动',
    ('MESH', 'rotation2'): '附加旋转',
    ('MESH', 'rotation2Jitter'): '附加旋转抖动',
    ('MESH', 'scale'): '逐轴缩放',
    ('MESH', 'global_scale'): '整体缩放',
    ('MESH', 'global_scale_jitter'): '整体缩放抖动',
    ('MESH', 'visconIndexJitter'): '可见条件索引抖动',

    # ── LIGHTNING ──
    ('LIGHTNING', 'unkn05_11'): '闪电透明度等级B',
    ('LIGHTNING', 'EPVColorSlot1'): 'EPV 颜色修正槽位',
    ('LIGHTNING', 'EPVColorSlot2'): 'EPV 颜色修正槽位',
    # ── 渲染体尺寸组：scale 为整体缩放 ──
    ('BILLBOARD3D', 'scale'): '整体缩放',
    ('BILLBOARD2D', 'scale'): '整体缩放',
    ('PLANE', 'scale'): '整体缩放',
    ('RIBBON', 'scale'): '整体缩放',

    # ── STRAINRIBBON：两个修正槽位分别对应颜色与颜色范围（后者为结构类推）──
    ('STRAINRIBBON', 'epv_color_slot1'): '颜色修正槽位',
    ('STRAINRIBBON', 'epv_color_slot2'): '颜色范围修正槽位?',

    # ── RIBBONBLADE ──
    ('RIBBONBLADE', 'maxLengthLimit'): '最大长度',
    ('RIBBONBLADE', 'contractionSpeed'): '回缩速度',
    ('RIBBONBLADE', 'colourTransitionPoint'): '颜色过渡点',
    ('RIBBONBLADE', 'emissiveStrength'): '自发光强度',
    ('RIBBONBLADE', 'uvRepetition'): 'UV 重复次数',
    ('RIBBONBLADE', 'widthDirection'): '宽度延伸方向',
    ('RIBBONBLADE', 'flowSpeed'): '流光贴图速度',
    ('RIBBONBLADE', 'flowSpeedJitter'): '流光贴图速度抖动',
    ('RIBBONBLADE', 'flowSpeedCoef'): '流光贴图加速度',
    ('RIBBONBLADE', 'flowSpeedCoefJitter'): '流光贴图加速度抖动',
    ('RIBBONBLADE', 'flowStrength'): '流光贴图强度',
    ('RIBBONBLADE', 'flowStrengthJitter'): '流光贴图强度抖动',
    ('RIBBONBLADE', 'flowStrengthCoef'): '流光贴图强度加速度',
    ('RIBBONBLADE', 'flowStrengthCoefJitter'): '流光贴图强度加速度抖动',
    # ── RGBWATER ──
    ('RGBWATER', 'colorSpecular'): '颜色',
    ('RGBWATER', 'colorSheet'): '颜色',
    ('RGBWATER', 'waterLerpGtoB'): 'Alpha 混入蓝通道比例',
    ('RGBWATER', 'intensityCubeMap'): '环境反射强度',
    ('RGBWATER', 'intensitySpecular'): '强度',
    ('RGBWATER', 'intensitySheet'): '强度',
    ('RGBWATER', 'intensityAlpha'): '总体透明度',
    ('RGBWATER', 'colorRate'): '总体亮度',
    ('RGBWATER', 'normalSharpness'): '法线锐度',
    ('RGBWATER', 'specularColorParam_useLife'): '启用生命周期',
    ('RGBWATER', 'specularColorParam_lifeType'): '生命期模式',
    ('RGBWATER', 'specularColorParam_appearFrame'): '淡入',
    ('RGBWATER', 'specularColorParam_appearFrameJitter'): '淡入抖动',
    ('RGBWATER', 'specularColorParam_keepFrame'): '持续',
    ('RGBWATER', 'specularColorParam_keepFrameJitter'): '持续抖动',
    ('RGBWATER', 'specularColorParam_vanishFrame'): '淡出',
    ('RGBWATER', 'specularColorParam_vanishFrameJitter'): '淡出抖动',
    ('RGBWATER', 'specularColorParam_lighting'): '受光照影响',
    ('RGBWATER', 'specularColorParam_correctColorNo'): 'EPV 颜色修正槽位',
    ('RGBWATER', 'sheetColorParam_useLife'): '启用生命周期',
    ('RGBWATER', 'sheetColorParam_lifeType'): '生命期模式',
    ('RGBWATER', 'sheetColorParam_appearFrame'): '淡入',
    ('RGBWATER', 'sheetColorParam_appearFrameJitter'): '淡入抖动',
    ('RGBWATER', 'sheetColorParam_keepFrame'): '持续',
    ('RGBWATER', 'sheetColorParam_keepFrameJitter'): '持续抖动',
    ('RGBWATER', 'sheetColorParam_vanishFrame'): '淡出',
    ('RGBWATER', 'sheetColorParam_vanishFrameJitter'): '淡出抖动',
    ('RGBWATER', 'sheetColorParam_lighting'): '受光照影响',
    ('RGBWATER', 'sheetColorParam_correctColorNo'): 'EPV 颜色修正槽位',
    ('RGBWATER', 'waterLerpParam_useLife'): '启用生命周期',
    ('RGBWATER', 'waterLerpParam_lifeType'): '生命期模式',
    ('RGBWATER', 'waterLerpParam_appearFrame'): '淡入',
    ('RGBWATER', 'waterLerpParam_appearFrameJitter'): '淡入抖动',
    ('RGBWATER', 'waterLerpParam_keepFrame'): '持续',
    ('RGBWATER', 'waterLerpParam_keepFrameJitter'): '持续抖动',
    ('RGBWATER', 'waterLerpParam_vanishFrame'): '淡出',
    ('RGBWATER', 'waterLerpParam_vanishFrameJitter'): '淡出抖动',
    ('RGBWATER', 'waterLerpParam_lighting'): '受光照影响',
}


def field_label_zh(type_name, field_name):
    """返回字段中文标签；无则 None（调用方回退英文友好名）。"""
    if type_name:
        h = NAME_TO_HASH.get(type_name)
        if h is not None:
            f = FIELD_REGISTRY.get((h, field_name))
            if f is not None and f.label_zh:
                return f.label_zh
        bt = _LABELS_BY_TYPE.get((type_name, field_name))
        if bt is not None:
            return bt
    return _LABELS_GLOBAL.get(field_name)


# 冻结内部名改动前的派生英文标签；值为用户可见文案

_LABELS_EN_GLOBAL = {
    'flowSpeedCoef':              'Flowmap Acceleration',
    'flowSpeedCoefJitter':        'Flowmap Acceleration Jitter',
    'flowStrengthCoef':           'Flowmap Strength Acceleration',
    'flowStrengthCoefJitter':     'Flowmap Strength Acceleration Jitter',

    # 路径缩写使用固定大写
    'uvsPath':   'UVS Path',
    'tfaPath':   'TFA Path',
    'mod3Path':  'Mod3 Path',
    'plPath':    'PL Path',
    'lutPath':   'LUT Path',
}

_LABELS_EN_BY_TYPE = {
    # ── UVCONTROL：Offset/Add/Coef 体系 ──
    ('UVCONTROL', 'uv1_offset'):       'Uv1 initial Position',
    ('UVCONTROL', 'uv1_offsetAdd'):    'Uv1 speed',
    ('UVCONTROL', 'uv1_offsetCoef'):   'Uv1 acceleration',
    ('UVCONTROL', 'uv1_scaleAdd'):     'Uv1 scale Speed',
    ('UVCONTROL', 'uv1_scaleCoef'):    'Uv1 scale Acceleration',
    ('UVCONTROL', 'uv2_offset'):       'Uv2 initial Position',
    ('UVCONTROL', 'uv2_offsetAdd'):    'Uv2 speed',
    ('UVCONTROL', 'uv2_offsetCoef'):   'Uv2 acceleration',
    ('UVCONTROL', 'uv2_scaleAdd'):     'Uv2 scale Speed',
    ('UVCONTROL', 'uv2_scaleCoef'):    'Uv2 scale Acceleration',
    # ── UVSEQUENCE：PlaySpeed/PlaySpeedCoef + SequenceNo/PatternNo ──
    ('UVSEQUENCE', 'playSpeed'):           'Animation Speed',
    ('UVSEQUENCE', 'playSpeedJitter'):     'Animation Speed Jitter',
    ('UVSEQUENCE', 'playSpeedCoef'):       'Animation Acceleration',
    ('UVSEQUENCE', 'playSpeedCoefJitter'): 'Animation Acceleration Jitter',
    ('UVSEQUENCE', 'sequenceNo'):          'Uvs Index',
    ('UVSEQUENCE', 'sequenceNoJitter'):    'Uvs Index Jitter',
    ('UVSEQUENCE', 'patternNo'):           'Starting Frame',
    ('UVSEQUENCE', 'patternNoJitter'):     'Starting Frame Jitter',
    # ── ROTATEANIM ──
    ('ROTATEANIM', 'billboardRotationCoef'):       'Billboard Rotation Accel',
    ('ROTATEANIM', 'billboardRotationCoefJitter'): 'Billboard Rotation Accel Jitter',
    ('ROTATEANIM', 'spinSpeedCoefX'):              'Spin Acceleration X',
    ('ROTATEANIM', 'spinSpeedCoefXJitter'):        'Spin Acceleration XJitter',
    ('ROTATEANIM', 'spinSpeedCoefY'):              'Spin Acceleration Y',
    ('ROTATEANIM', 'spinSpeedCoefYJitter'):        'Spin Acceleration YJitter',
    ('ROTATEANIM', 'spinSpeedCoefZ'):              'Spin Acceleration Z',
    ('ROTATEANIM', 'spinSpeedCoefZJitter'):        'Spin Acceleration ZJitter',
    # ── VELOCITY2D / VELOCITY3D ──
    ('VELOCITY3D', 'speedCoef'):       'Acceleration',
    ('VELOCITY3D', 'speedCoefJitter'): 'Acceleration Jitter',
    ('VELOCITY2D', 'speedCoef'):       'Acceleration',
    ('VELOCITY2D', 'speedCoefJitter'): 'Acceleration Jitter',
    # ── PARENTOPTIONS ──
    ('PARENTOPTIONS', 'relationPos'):        'Translation tracking',
    ('PARENTOPTIONS', 'relationRot'):        'Angle tracking',
    ('PARENTOPTIONS', 'relationScl'):        'Scale tracking',
    ('PARENTOPTIONS', 'constRelease'):       'Lock To Position Frame',
    ('PARENTOPTIONS', 'constReleaseJitter'): 'Lock To Position Frame Jitter',
    ('PARENTOPTIONS', 'jointNo'):            'Bone lim',
    # ── RGBWATER（custom-codec 无 Field.label_en）──
    ('RGBWATER', 'colorSpecular'):                       'Color',
    ('RGBWATER', 'colorSheet'):                          'Color',
    ('RGBWATER', 'waterLerpGtoB'):                       'Lerp Alpha To Blue',
    ('RGBWATER', 'intensityCubeMap'):                    'CubeMap Factor',
    ('RGBWATER', 'intensitySpecular'):                   'Factor',
    ('RGBWATER', 'intensitySheet'):                      'Factor',
    ('RGBWATER', 'intensityAlpha'):                      'Overall Alpha',
    ('RGBWATER', 'colorRate'):                           'Overall Brightness',
    ('RGBWATER', 'normalSharpness'):                     'Normal Sharpness',
    ('RGBWATER', 'specularColorParam_useLife'):          'Use Life',
    ('RGBWATER', 'specularColorParam_lifeType'):         'Life Type',
    ('RGBWATER', 'specularColorParam_appearFrame'):      'Appear',
    ('RGBWATER', 'specularColorParam_appearFrameJitter'): 'Appear Jitter',
    ('RGBWATER', 'specularColorParam_keepFrame'):        'Keep',
    ('RGBWATER', 'specularColorParam_keepFrameJitter'):  'Keep Jitter',
    ('RGBWATER', 'specularColorParam_vanishFrame'):      'Vanish',
    ('RGBWATER', 'specularColorParam_vanishFrameJitter'): 'Vanish Jitter',
    ('RGBWATER', 'specularColorParam_lighting'):         'Lighting',
    ('RGBWATER', 'specularColorParam_correctColorNo'):   'EPV Color Slot',
    ('RGBWATER', 'sheetColorParam_useLife'):             'Use Life',
    ('RGBWATER', 'sheetColorParam_lifeType'):            'Life Type',
    ('RGBWATER', 'sheetColorParam_appearFrame'):         'Appear',
    ('RGBWATER', 'sheetColorParam_appearFrameJitter'):   'Appear Jitter',
    ('RGBWATER', 'sheetColorParam_keepFrame'):           'Keep',
    ('RGBWATER', 'sheetColorParam_keepFrameJitter'):     'Keep Jitter',
    ('RGBWATER', 'sheetColorParam_vanishFrame'):         'Vanish',
    ('RGBWATER', 'sheetColorParam_vanishFrameJitter'):   'Vanish Jitter',
    ('RGBWATER', 'sheetColorParam_lighting'):            'Lighting',
    ('RGBWATER', 'sheetColorParam_correctColorNo'):      'EPV Color Slot',
    ('RGBWATER', 'waterLerpParam_useLife'):              'Use Life',
    ('RGBWATER', 'waterLerpParam_lifeType'):             'Life Type',
    ('RGBWATER', 'waterLerpParam_appearFrame'):          'Appear',
    ('RGBWATER', 'waterLerpParam_appearFrameJitter'):    'Appear Jitter',
    ('RGBWATER', 'waterLerpParam_keepFrame'):            'Keep',
    ('RGBWATER', 'waterLerpParam_keepFrameJitter'):      'Keep Jitter',
    ('RGBWATER', 'waterLerpParam_vanishFrame'):          'Vanish',
    ('RGBWATER', 'waterLerpParam_vanishFrameJitter'):    'Vanish Jitter',
    ('RGBWATER', 'waterLerpParam_lighting'):             'Lighting',
}


def field_label_en(type_name, field_name):
    """返回字段英文标签；无则 None（调用方回退 _friendly_name 派生）。

    查找顺序与 field_label_zh 对称：Field.label_en → 按类型表 → 全局表。
    """
    if type_name:
        h = NAME_TO_HASH.get(type_name)
        if h is not None:
            f = FIELD_REGISTRY.get((h, field_name))
            if f is not None and f.label_en:
                return f.label_en
        bt = _LABELS_EN_BY_TYPE.get((type_name, field_name))
        if bt is not None:
            return bt
    return _LABELS_EN_GLOBAL.get(field_name)
