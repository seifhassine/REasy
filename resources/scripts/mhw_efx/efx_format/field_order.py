# -*- coding: utf-8 -*-
"""字段显示顺序的局部订正表。

维护约束：
- 未登记的字段保持字节序；本表仅影响显示，不参与二进制导出。
- 键为类型名，值为 ``{字段: 前置锚点}``。移动以显示单元进行，value/jitter 配对不可拆开。
- 锚点必须是显示单元的 lead 字段；多个字段不能直接锚定同一字段，应改为链式锚定。
"""

FIELD_ORDER_ANCHORS = {
    # 开关应紧邻其控制字段。流动贴图组的位置由 blender_efx/field_groups.py 决定。
    "BILLBOARD2D":  {
        "correctColorNo":          "applicationRule",
        "useColorRange":           "color",
        "colorRangeCorrectColorNo": "useColorRange",
        "blendMode":               "colorRange",
    },
    "BILLBOARD3D":  {
        "correctColorNo":          "applicationRule",
        "useColorRange":           "color",
        "colorRangeCorrectColorNo": "useColorRange",
        "blendMode":               "colorRange",
    },
    "PLANE":        {
        "correctColorNo":          "applicationRule",
        "useColorRange":           "color",
        "colorRangeCorrectColorNo": "useColorRange",
        "blendMode":               "colorRange",
    },
    "RIBBON":       {
        # 两个 EPV 色槽分别紧邻 color 与 colorRange。
        "epvcolor_0": "spacer0",
        "epvcolor_1": "spacer1",
        # 这两个字段在拉伸参数之后，但属于物理组
        "unkn21":     "springiness",
        "unkn22_0":   "unkn21",
    },
    "STRAINRIBBON": {
        # 两个 EPV 色槽分别紧邻 color 与 colorRange。
        "epv_color_slot1": "spacer00",
        "epv_color_slot2": "useColorRange",
        "unknEnum11":      "displacementToggle",
        "unknEnum12_00":   "unknEnum11",
    },
    "TURBULENCE": {"unknFlag3_4": "unkn1_0"},
    "EXTERNREFERENCE": {"unknFlag1_6": "trigger_condition"},
    "PTCOLLISION": {"bounceCount": "horizontalBounce"},
    # PLEMISSIVE：子类独有的部位掩码排在基类字段之后，前面几段与 PARENTEMISSIVE 相同
    "PLEMISSIVE": {"body_p": "unknFixed5_4", "wp_p": "body_p"},
    # MESH 的颜色、发光和旋转控制按其所属字段组显示。
    "MESH": {
        "useColorRange":         "color",
        "colorRate":             "colorRange",
        "useEmissiveColor":      "colorRate",
        "useEmissiveColorRange": "emissiveColor",
        "emissiveColorRate":     "emissiveColorRange",
        "rotationOrder":         "rotation",
    },

    # 以下类型按 blender_efx/field_groups.py 的 TYPE_SECTIONS 分段排列。
    "LIFE": {"indefiniteLifespan": "vanishFrame", "unknFrame": "timeToDeath"},
    "BLINK": {"unkn1_0": "highFrequencyWidth"},
    "UVSEQUENCE": {"uvsPath": "typeFlag"},
    "EMITTERSHAPE2D": {
        "shapeType":       "typeFlag",
        "rangeDivideAxis": "rangeY",
    },
    "RAYCAST": {
        "spacer1":       "spacer0",
        "spacer2":       "spacer1",
        "spacer3":       "spacer2",
        "direction":     "spacer3",
        "startOffset":   "direction",
        "startDistance": "startOffset",
        "maxDistance":   "startDistance",
        "speed":         "maxDistance",
        "prop2":         "rayCastFlags",
    },
    "HOMING": {
        "homingTarget":         "spacer",
        "vanishMode":           "maxSpeed",
        "vanishRadius":         "vanishMode",
        "forceFieldMode":       "vanishRadius",
        "forceFieldRadius":     "forceFieldMode",
        "forceFieldSpeedScale": "forceFieldRadius",
    },
    "ROTATEANIM": {"spin_velocity": "billboardRotationCoef"},
    "VELOCITY3D": {
        "speed":                "typeFlag",
        "speedCoef":            "speed",
        "minMovementThreshold": "speedCoef",
        "velocityType":         "minMovementThreshold",
        "movementDelay":        "sizeZ",
        "gravity":              "movementDelay",
    },
    "VELOCITY2D": {
        "speed":         "typeFlag",
        "speedCoef":     "speed",
        "velocityType":  "speedCoef",
        "rotation":      "velocityType",
        "movementDelay": "divergenceY",
        "gravity":       "movementDelay",
    },
    "FADEBYANGLE": {"rotation": "rotOrder"},
    # RIBBONBLADE：形状段夹着的未知字段挪到尾部段之后
    "RIBBONBLADE": {
        "uvRepetition":  "contractionSpeed",
        "useEmissiveRange":      "emissiveStrength",
        "emissiveStrengthRange": "useEmissiveRange",
        "unknFixed0_1":  "tailUnkn18_0",
        "spacer1":       "unknFixed0_1",
        "unknFlag07_0":  "spacer1",
        "spacer2b":      "unknFlag07_0",
        "unknEnum10":    "spacer2b",
        "unknFlag12_0":  "unknEnum10",
        "unknFlag12_1":  "unknFlag12_0",
        "unknFixed12_2": "unknFlag12_1",
        "spacer3":       "unknFixed12_2",
    },
    "SPAWN": {
        "loopNum":         "spawnNum",
        "revivalLoop":     "spawnFrame",
        "revivalInterval": "revivalLoop",
    },
    "EMITTERSHAPE3D": {
        "shapeType":                "typeFlag",
        "rangeDivideHorizontalNum": "rangeDivideAxis",
        "rangeDivideVerticalNum":   "rangeDivideHorizontalNum",
        "radiusOrigin":             "scanAngleVertical",
    },
    # 末端形状沿用 EMITTERSHAPE3D 的显示顺序。
    "LIGHTNING": {
        "terminal_shapeType":                "useTerminalShape",
        "terminal_rangeDivideHorizontalNum": "terminal_rangeDivideAxis",
        "terminal_rangeDivideVerticalNum":   "terminal_rangeDivideHorizontalNum",
        "terminal_radiusOrigin":             "terminal_scanAngleVertical",
    },
    # RGBFIRE 按全局、火焰、烟雾与插值字段分组显示。
    "RGBFIRE": {
        "colorRate":                       "typeFlag",
        "alphaFactor":                     "colorRate",
        "fireColorParam_correctColorNo":   "alphaFactor",
        "fireColor":                       "fireColorParam_correctColorNo",
        "fireFactor":                      "fireColor",
        "fireColorParam_lighting":         "fireFactor",
        "fireColorParam_useLife":          "fireColorParam_lighting",
        "fireColorParam_appearFrame":      "fireColorParam_useLife",
        "fireColorParam_keepFrame":        "fireColorParam_appearFrame",
        "fireColorParam_vanishFrame":      "fireColorParam_keepFrame",
        "fireColorParam_lifeType":         "fireColorParam_vanishFrame",
        "smokeColorParam_correctColorNo":  "fireColorParam_lifeType",
        "smokeColor":                      "smokeColorParam_correctColorNo",
        "redChFactor":                     "smokeColor",
        "smokeColorParam_lighting":        "redChFactor",
        "smokeColorParam_useLife":         "smokeColorParam_lighting",
        "smokeColorParam_appearFrame":     "smokeColorParam_useLife",
        "smokeColorParam_keepFrame":       "smokeColorParam_appearFrame",
        "smokeColorParam_vanishFrame":     "smokeColorParam_keepFrame",
        "smokeColorParam_lifeType":        "smokeColorParam_vanishFrame",
        "lerpAlphaToBlue":                 "smokeColorParam_lifeType",
    },

    # RGBWATER 按全局、高光、水膜、反射与插值字段分组显示。
    "RGBWATER": {
        "colorRate":                       "typeFlag",
        "intensityAlpha":                  "colorRate",
        "normalSharpness":                 "intensityAlpha",
        "specularColorParam_correctColorNo": "normalSharpness",
        "colorSpecular":                   "specularColorParam_correctColorNo",
        "intensitySpecular":               "colorSpecular",
        "specularColorParam_lighting":     "intensitySpecular",
        "specularColorParam_useLife":      "specularColorParam_lighting",
        "specularColorParam_appearFrame":  "specularColorParam_useLife",
        "specularColorParam_keepFrame":    "specularColorParam_appearFrame",
        "specularColorParam_vanishFrame":  "specularColorParam_keepFrame",
        "specularColorParam_lifeType":     "specularColorParam_vanishFrame",
        "sheetColorParam_correctColorNo":  "specularColorParam_lifeType",
        "colorSheet":                      "sheetColorParam_correctColorNo",
        "intensitySheet":                  "colorSheet",
        "sheetColorParam_lighting":        "intensitySheet",
        "sheetColorParam_useLife":         "sheetColorParam_lighting",
        "sheetColorParam_appearFrame":     "sheetColorParam_useLife",
        "sheetColorParam_keepFrame":       "sheetColorParam_appearFrame",
        "sheetColorParam_vanishFrame":     "sheetColorParam_keepFrame",
        "sheetColorParam_lifeType":        "sheetColorParam_vanishFrame",
        "cubemapPath":                     "sheetColorParam_lifeType",
        "intensityCubeMap":                "cubemapPath",
        "waterLerpGtoB":                   "intensityCubeMap",
        "waterLerpParam_lighting":         "waterLerpGtoB",
        "waterLerpParam_useLife":          "waterLerpParam_lighting",
        "waterLerpParam_appearFrame":      "waterLerpParam_useLife",
        "waterLerpParam_keepFrame":        "waterLerpParam_appearFrame",
        "waterLerpParam_vanishFrame":      "waterLerpParam_keepFrame",
        "waterLerpParam_lifeType":         "waterLerpParam_vanishFrame",
    },

    # TRANSFORM3D 将速度与修正量分别分组，供对应位开关控制显示。
    "TRANSFORM3D": {
        "rotation_velocity":             "translation_velocity",
        "scale_velocity":                "rotation_velocity",
        "translation_velocity_modifier": "scale_velocity",
        "rotation_velocity_modifier":    "translation_velocity_modifier",
        "scale_velocity_modifier":       "rotation_velocity_modifier",
    },
}


def display_anchors(type_name: str) -> dict:
    """某属性类型的顺序订正表；没登记的类型返回空字典（=保持字节序）。"""
    return FIELD_ORDER_ANCHORS.get(type_name) or {}
