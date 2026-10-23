"""TIML hash 的显示名、字段映射和 game/Blender 坐标换算。

维护约束：
- 未知 TLP 或 DT hash 必须回退为十六进制显示，不能据名称推测字段映射。
- 调色板候选、字段映射和原生 animation 轴是不同置信层的数据；不得用任一表剪除其它表项。
- 新建轨道优先使用静态字段 seed；无 seed 时才使用 DT 中性值。
"""

# timelineParameterHash 显示名
TLP_NAMES = {
    0x3AC1EACA: "Uber_Mt",
    0x65004e2a: "MhEffectDecalBehavior",
    0x6da6e5d1: "MhPointLightBehavior",
    0x75963575: "MhSpotLightBehavior",
    0x540a2572: "Transform2D",
    0x4d111433: "Transform3D",
    0x2bda85f5: "Velocity2D",
    0x32c1b4b4: "Velocity3D",
    0x2b61b0ed: "Billboard2D",
    0x327a81ac: "Billboard3D",
    0x3481666b: "Plane",
    0x538af627: "Mesh",
    0x1436e592: "Ribbon",
    0x1f09850e: "StrainRibbon",
    0x5ac7fc29: "UVSequence",
    0x563c8065: "RotateAnim",
    0x2a62f92e: "ScaleAnim",
    0x2a0363d4: "EmitterShape2D",
    0x33185295: "EmitterShape3D",
    0x4a0d2b6a: "Life",
    0x60ba9117: "RgbFire",
    0x2101c529: "RgbWater",
    0x39c68fb4: "TubeLight",
    0x42e48dde: "PointLightBehavior",
    0x582ba062: "RadialBlurFilterBehavior",
    0x2ed89bcc: "ParentMaterial",
    0x4cdb308a: "Item",
    0x3f2b8294: "EffectEvent",
    0x06e8d4c3: "DecalBehavior",
    0x0235f20e: "LightBehavior",
    0x3de576dc: "SpotLightBehavior",
    0x2c154dca: "FilterBehavior",
    0x13a0f54f: "TonemapFilter",
    0x096cabc4: "ColorCorrectFilter",
    # 材质或其它非 EFX TLP 名称也可用于显示
    0x399db6a9: "VFX_Flood_Mt",
    0x598272e1: "PlEmissive",
    0x66c62149: "VFX_Ice_Mt",
    0x5e8d9ee9: "VFX_EmissiveFog_Mt",
    0x2c78b827: "VFX_Tornado_Mt",
    0x4e64d91c: "Burn_Mt",
    0x09c466dc: "Standard_Mt",
    0x5752ed69: "VFX_DispWave_Mt",
    0x0b8924da: "EM106_Mt",
    0x70c7b1f1: "VFX_SandFall_Mt",
    0x3e880466: "VFX_Water_Mt",
    0x17359e0c: "VFX_Aurora_Mt",
    0x465acf70: "VFX_DistDisp_Mt",
    0x7e51f5bd: "LightTimelineParam",
    0x0fe12549: "VFX_VATDist_Mt",
}

# TLP 全限定类名
TLP_FULLNAMES = {
    0x01739779: 'nTimelineParam::nWwiseTimeline::GameParameter',
    0x03CE7F12: 'nTimelineParam::Em110Motion',
    0x04EBB38D: 'nDraw::MaterialAnimation::EM102_Mt',
    0x053CBDDA: 'nTimelineParam::EffectParameter3',
    0x07758DAF: 'nDraw::MaterialAnimation::EM036_Mt',
    0x08171AF8: 'nDraw::MaterialAnimation::EM032_Mt',
    0x09C466DC: 'nDraw::MaterialAnimation::Standard_Mt',
    0x0A5CFC32: 'nTimelineParam::CollisionSyncUID',
    0x0AC34102: 'nTimelineParam::Em107Motion',
    0x0B8924DA: 'nDraw::MaterialAnimation::EM106_Mt',
    0x0BFA707A: 'nTimelineParam::Em080Motion',
    0x0CFD985C: 'nTimelineParam::nWwiseTimeline::EventCollision00',
    0x0D4178F1: 'nTimelineParam::Em120Motion',
    0x0E556A0F: 'nDraw::MaterialAnimation::EM117_Mt',
    0x0E6D4A92: 'nTimelineParam::Em045Motion',
    0x0FE12549: 'nDraw::MaterialAnimation::VFX_VATDist_Mt',
    0x101C6C94: 'nDraw::MaterialAnimation::EM024_Mt',
    0x141DAC90: 'nTimelineParam::Em113_01Motion',
    0x1436E592: 'nEffect::nTimelineParam::TypeRibbon',
    0x14516E3B: 'nTimelineParam::Em112Motion',
    0x15C60453: 'nDraw::MaterialAnimation::SZK001_Mt',
    0x15F4C9E6: 'nTimelineParam::nWwiseTimeline::EventCollision03',
    0x170FACE6: 'nTimelineParam::AnimalFly',
    0x17359E0C: 'nDraw::MaterialAnimation::VFX_Aurora_Mt',
    0x17EAA6E5: 'nTimelineParam::SpeedTreeWindGenerator',
    0x1830887C: 'nTimelineParam::OtasukeMotion',
    0x18B27374: 'nTimelineParam::Em118_05Motion',
    0x193C8B34: 'nDraw::MaterialAnimation::EM105_Mt',
    0x1A0B3112: 'nDraw::MaterialAnimation::EM080_01_Mt',
    0x1E83FE5F: 'nTimelineParam::EmCharmMountStepObject',
    0x1F09850E: 'nEffect::nTimelineParam::TypeStrainRibbon',
    0x201C7206: 'nTimelineParam::nWwiseTimeline::EventGroup09',
    0x20FC82E0: 'nTimelineParam::ClawMotionVisual',
    0x2101C529: 'nEffect::nTimelineParam::RgbWater',
    0x233BDD27: 'nTimelineParam::NpcCommon',
    0x24006667: 'nTimelineParam::nWwiseTimeline::EventLoop',
    0x245CA284: 'nDraw::MaterialAnimation::EM115_Mt',
    0x255D71CC: 'nTimelineParam::Em013Motion',
    0x25B974A6: 'nTimelineParam::Em111Motion',
    0x2643266F: 'nDraw::MaterialAnimation::FakeSphere_Mt',
    0x29AA3E2D: 'nTimelineParam::nWwiseTimeline::EventGroup05',
    0x2A62F92E: 'nEffect::nTimelineParam::ScaleAnim',
    0x2A68E3BD: 'nDraw::MaterialAnimation::SpeedTree_Mt',
    0x2B3E35D3: 'nDraw::MaterialAnimation::EM111_Mt',
    0x2B61B0ED: 'nEffect::nTimelineParam::TypeBillboard2D',
    0x2BD2762F: 'nTimelineParam::Em023Motion',
    0x2BDA85F5: 'nEffect::nTimelineParam::Velocity2D',
    0x2C78B827: 'nDraw::MaterialAnimation::VFX_Tornado_Mt',
    0x2CB44AB6: 'nTimelineParam::Em106Motion',
    0x2EC7FA34: 'nTimelineParam::nWwiseTimeline::EventGroup01',
    0x2EE27B06: 'nDraw::MaterialAnimation::EM100_Mt',
    0x2F9878D5: 'nTimelineParam::Em063Motion',
    0x30213175: 'nTimelineParam::Em118Motion',
    0x30891F6A: 'nDraw::MaterialAnimation::EM057_Mt',
    0x30A36F97: 'nTimelineParam::nWwiseTimeline::EventGroup06',
    0x327A81AC: 'nEffect::nTimelineParam::TypeBillboard3D',
    0x32C1B4B4: 'nEffect::nTimelineParam::Velocity3D',
    0x32FA69E8: 'nTimelineParam::AnimalSeasonEvent',
    0x33185295: 'nEffect::nTimelineParam::EmitterShape3D',
    0x33C620F4: 'nDraw::MaterialAnimation::FakeRefraction_Mt',
    0x3481666B: 'nEffect::nTimelineParam::TypePlane',
    0x37CEAB8E: 'nTimelineParam::nWwiseTimeline::EventGroup02',
    0x38FF82EA: 'nTimelineParam::CollisionTimelineObject',
    0x399DB6A9: 'nDraw::MaterialAnimation::VFX_Flood_Mt',
    0x39C68FB4: 'nEffect::nTimelineParam::TubeLight',
    0x3AC1EACA: 'nDraw::MaterialAnimation::Uber_Mt',
    0x3B2B5B9F: 'nTimelineParam::Em104Motion',
    0x3BF74053: 'nDraw::MaterialAnimation::BTK001_Mt',
    0x3C57D4E8: 'nDraw::MaterialAnimation::EM103_Mt',
    0x3CA9626C: 'nTimelineParam::Em123Motion',
    0x3E6BDB12: 'nTimelineParam::ModelPartsCtrl',
    0x3E880466: 'nDraw::MaterialAnimation::VFX_Water_Mt',
    0x3F207E3C: 'nDraw::MaterialAnimation::FakeInnerEmit_Mt',
    0x40793BDC: 'nDraw::MaterialAnimation::TMG001_Mt',
    0x40C99B18: 'nTimelineParam::nWwiseTimeline::EventGroup03',
    0x40DBFBE3: 'nTimelineParam::nWwiseTimeline::EventGroup10',
    0x42E48DDE: 'nEffect::nTimelineParam::PointLightBehavior',
    0x45CB6C2B: 'nTimelineParam::Ems005_01Motion',
    0x460700DC: 'nDraw::MaterialAnimation::SKM001_Mt',
    0x465ACF70: 'nDraw::MaterialAnimation::VFX_DistDisp_Mt',
    0x4669419C: 'nTimelineParam::Em117Motion',
    0x46F3E901: 'nTimelineParam::PlMotionVisual',
    0x47A45F01: 'nTimelineParam::nWwiseTimeline::EventGroup07',
    0x48067636: 'nDraw::MaterialAnimation::GenericMaterial',
    0x48D6114F: 'nTimelineParam::PlMotionCommon',
    0x48E6467F: 'nTimelineParam::Em127Motion',
    0x49DFD557: 'nTimelineParam::PugeeMotion',
    0x4BCA741C: 'nTimelineParam::Em042Motion',
    0x4C119332: 'nTimelineParam::cCharaMotion',
    0x4D111433: 'nEffect::nTimelineParam::Transform3D',
    0x4E64D91C: 'nDraw::MaterialAnimation::Burn_Mt',
    0x4FB76028: 'nDraw::MaterialAnimation::EM002_Mt',
    0x4FF9141E: 'nDraw::MaterialAnimation::EM_Mt',
    0x52CCD16F: 'nTimelineParam::Em063_05Motion',
    0x538AF627: 'nEffect::nTimelineParam::TypeMesh',
    0x53EA348C: 'nDraw::MaterialAnimation::EM109_Mt',
    0x540A2572: 'nEffect::nTimelineParam::Transform2D',
    0x54800017: 'nTimelineParam::EmCreateGmMotion',
    0x55585B25: 'nTimelineParam::Em057Motion',
    0x55CEE362: 'nDraw::MaterialAnimation::EM080_Mt',
    0x56367A59: 'nDraw::MaterialAnimation::EM118_Mt',
    0x563C8065: 'nEffect::nTimelineParam::RotateAnim',
    0x566DF526: 'nDraw::MaterialAnimation::Flow_Dir_Mt',
    0x571B4290: 'nTimelineParam::nWwiseTimeline::EventGroup08',
    0x5752ED69: 'nDraw::MaterialAnimation::VFX_DispWave_Mt',
    0x575E6887: 'nTimelineParam::OtomoMotion',
    0x582BA062: 'nEffect::nTimelineParam::RadialBlurFilterBehavior',
    0x58FB6EA5: 'nTimelineParam::Em102Motion',
    0x598272E1: 'nMhEffect::nTimelineParam::PlEmissive',
    0x59C0CAA2: 'nTimelineParam::nWwiseTimeline::EventGroup00',
    0x5AC7FC29: 'nEffect::nTimelineParam::UVSequence',
    0x5AFC3A5F: 'nTimelineParam::Em109Motion',
    0x5B40BF31: 'nDraw::MaterialAnimation::EM124_Mt',
    0x5C648E63: 'nTimelineParam::ShellCreate',
    0x5E8D9EE9: 'nDraw::MaterialAnimation::VFX_EmissiveFog_Mt',
    0x5E953EE4: 'nDraw::MaterialAnimation::EM100_01_Mt',
    0x5EAD0EBB: 'nTimelineParam::nWwiseTimeline::EventGroup04',
    0x5F02205C: 'nTimelineParam::ShellAnimation',
    0x5F32B536: 'nDraw::MaterialAnimation::OZK001_Mt',
    0x5F456B89: 'nDraw::MaterialAnimation::PL_Mt',
    0x5F795756: 'nTimelineParam::Em125Motion',
    0x5F9D523C: 'nTimelineParam::Em027Motion',
    0x601E4A28: 'nTimelineParam::Em116Motion',
    0x60BA9117: 'nEffect::nTimelineParam::RgbFire',
    0x62F3F970: 'nTimelineParam::nWwiseTimeline::EventCollision02',
    0x63FCD854: 'nDraw::MaterialAnimation::EM125_Mt',
    0x64A758BE: 'nTimelineParam::Em111_05Motion',
    0x65004E2A: 'nEffect::nTimelineParam::MhEffectDecalBehavior',
    0x658D8235: 'nTimelineParam::EmClawRejectCollisionObject',
    0x66C62149: 'nDraw::MaterialAnimation::VFX_Ice_Mt',
    0x69137438: 'nTimelineParam::Em101Motion',
    0x6B32DCF6: 'nTimelineParam::EffectParameter1',
    0x6D4CF8C4: 'nTimelineParam::Em115_05Motion',
    0x6DA6E5D1: 'nEffect::nTimelineParam::MhPointLightBehavior',
    0x6DBD7FA8: 'nTimelineParam::Em043Motion',
    0x6DC8D36C: 'nTimelineParam::MatAnimPlayer',
    0x6E914DCB: 'nTimelineParam::Em126Motion',
    0x6FCCD10E: 'nTimelineParam::PlMotionInput',
    0x70C7B1F1: 'nDraw::MaterialAnimation::VFX_SandFall_Mt',
    0x723B8D4C: 'nTimelineParam::EffectParameter2',
    0x75963575: 'nEffect::nTimelineParam::MhSpotLightBehavior',
    0x76D8344E: 'nDraw::MaterialAnimation::EMS_Mt',
    0x77519ACB: 'nTimelineParam::EmMotionCommon',
    0x77815B01: 'nTimelineParam::Em114Motion',
    0x784BF2ED: 'nDraw::MaterialAnimation::EM063_Mt',
    0x790E5CE2: 'nTimelineParam::Em124Motion',
    0x791C240E: 'nTimelineParam::PhotomoCommon',
    0x79746285: 'nTimelineParam::EmMotionVisual',
    0x79EA5988: 'nTimelineParam::Em026Motion',
    0x7BFAA8CA: 'nTimelineParam::nWwiseTimeline::EventCollision01',
    0x7D3BAE11: 'nDraw::MaterialAnimation::FakeEye_Mt',
    0x7E51F5BD: 'nTimelineParam::LightTimelineParam',
    0x7E68607B: 'nTimelineParam::Em001Motion',
    0x7E8C6511: 'nTimelineParam::Em103Motion',
    0x7E9DBB98: 'nTimelineParam::ShellMultiCreate',
    0x7F140A1E: 'nTimelineParam::AnimalCommon',
    0x7F2A1793: 'nTimelineParam::Em110_01Motion',
}

import math

from ..hashes import jamcrc

# ── datatypeHash → 显示名（DT_NAMES）─────────────────────────────────────────────────
# 来源：refs/dti_effect_fields.json 字段 hash 直接对比语料 datatypeHash，83/130 命中。
# 未命中的 47 条由 datatype_name() 回退到 0x 十六进制。
# 注：同名 DT_TRANSFORM 条目优先（transform 九条有 Blender 属性映射，DT_NAMES 只做展示）。
DT_NAMES = {
    # ── Color / 颜色属性 ──────────────────────────────────────────
    0x58689812: "Color",                   # cnt=1645 Color[RGBA]      TLP:Billboard3D,Ribbon
    0x5A8C6820: "SmokeColor",              # cnt=122  Color[RGBA]      TLP:RgbFire
    0x39A1E557: "FireColor",               # cnt=87   Color[RGBA]      TLP:RgbFire
    0xCBDB6622: "EmissiveMapFactorColor",  # cnt=228  Color[RGBA]      TLP:MhEffectDecalBehavior
    0xAFE95AC0: "mBaseMapFactor",              # cnt=157  Color[RGBA]      TLP:unknown
    0x26BD5CC2: "BlendFactor",             # cnt=20   Color[RGBA]      TLP:MhEffectDecalBehavior
    0xFA79B1CD: "Emissive",               # cnt=7    Color[RGBA]      TLP:0x598272E1
    0x608DCF8D: "EmissiveColor",          # cnt=13   Color[RGBA]      TLP:Mesh
    0x3BA67E7C: "HeadColor",              # cnt=1    Color[RGBA]      TLP:TubeLight
    0x2AA40DE9: "TailColor",              # cnt=1    Color[RGBA]      TLP:TubeLight
    0xC216C23D: "ColorRange",             # cnt=1    Color[RGBA]      TLP:Billboard3D
    0x60D69856: "ColorSpecular",          # cnt=1    Color[RGBA]      TLP:RgbWater
    0x0FF5554F: "mDistortionFactor",      # cnt=1    Color[RGBA]      TLP:0x3E880466
    0xA01B7821: "mEmissiveMapFactor",     # cnt=11   Color[RGBA]      TLP:0x09C466DC
    # ── Float / 数值属性 ──────────────────────────────────────────
    0x9F1E012E: "ColorRate",              # cnt=1303 Float            TLP:Ribbon,RgbFire
    # 0xAFE95AC0 未命中（cnt=157, 归属未知 TLP）——留回退
    0x94BCC5CE: "Intensity",             # cnt=701  Float            TLP:0x598272E1,MhPointLightBehavior
    0x7D235C30: "EmissiveMapFactorIntensity",  # cnt=296 Float       TLP:MhEffectDecalBehavior
    0x531B9E44: "SizeY",                 # cnt=241  Float            TLP:Billboard3D,Mesh
    0x0EBAEC37: "SizeScalar",            # cnt=150  Float            TLP:Ribbon,Billboard3D
    0x1383E9BA: "RangeMaxY",             # cnt=141  Float            TLP:EmitterShape3D
    0x8A8AB800: "RangeMaxZ",             # cnt=125  Float            TLP:EmitterShape3D
    0x6484D92C: "RangeMaxX",             # cnt=124  Float            TLP:EmitterShape3D
    0x18C577DE: "EmissiveColorRate",     # cnt=114  Float            TLP:Mesh
    0x98015C6F: "RangeMinZ",             # cnt=113  Float            TLP:EmitterShape3D
    0x760F3D43: "RangeMinX",             # cnt=112  Float            TLP:EmitterShape3D
    0x3775827A: "RangeY",               # cnt=100  Float            TLP:MhEffectDecalBehavior
    0x4072B2EC: "RangeX",               # cnt=99   Float            TLP:MhEffectDecalBehavior
    0x01080DD5: "RangeMinY",             # cnt=96   Float            TLP:EmitterShape3D
    0x435F3054: "EffectiveRadius",       # cnt=92   Float            TLP:PointLightBehavior
    0xE5C92264: "PlaySpeed",             # cnt=91   Float            TLP:UVSequence
    0x31182E0D: "Speed",                # cnt=85   Float            TLP:Velocity2D,Velocity3D
    0xC32F9493: "Radius",               # cnt=75   Float            TLP:PointLightBehavior
    0x241CAED2: "SizeX",                # cnt=69   Float            TLP:Mesh,Billboard3D
    0xBF160652: "AlphaCorrectionMin",    # cnt=68   Float            TLP:MhEffectDecalBehavior
    0x371BBC04: "mRoughness",            # cnt=54   Float            TLP:0x09C466DC
    0xF0DF339B: "WidthSize",            # cnt=54   Float            TLP:Ribbon,StrainRibbon
    0xA7EDA21C: "WaterLerpGtoB",        # cnt=50   Float            TLP:RgbWater
    0x002FF505: "RotationX",            # cnt=40   Float            TLP:Mesh
    0xCA12CFFE: "SizeZ",                # cnt=38   Float            TLP:Mesh
    0xEE219429: "RotationZ",            # cnt=36   Float            TLP:Mesh
    0xC24DF97C: "SizeScalarAdd",        # cnt=35   Float            TLP:ScaleAnim
    0x6A5FE3C4: "Gravity",              # cnt=34   Float            TLP:Velocity3D
    0xF92E647B: "Length",               # cnt=32   Float            TLP:Ribbon,StrainRibbon
    0xC207B8B4: "mFlowStrength",        # cnt=27   Float            TLP:0x5E8D9EE9
    0x2822A722: "SizeYAdd",             # cnt=22   Float            TLP:ScaleAnim
    0xE81961E4: "RotationAdd",          # cnt=21   Float            TLP:RotateAnim
    0x1D95BB54: "BlurWidth",            # cnt=19   Float            TLP:RadialBlurFilterBehavior
    0x909EC047: "SizeXAdd",             # cnt=18   Float            TLP:ScaleAnim
    0x1BB0EB80: "mEmissiveMapFactorIntensity",  # cnt=18 Float      TLP:0x09C466DC
    0x316D89C5: "CoreThickness",        # cnt=17   Float            TLP:TubeLight
    0x7728C593: "RotationY",            # cnt=16   Float            TLP:Mesh
    0x085BC9D5: "LightIntensity",       # cnt=16   Float            TLP:TubeLight
    0xC23FE6C6: "RotationAddX",         # cnt=12   Float            TLP:RotateAnim
    0xAB9D6334: "ColorIntensity",       # cnt=11   Float            TLP:RadialBlurFilterBehavior
    0xB538D650: "RotationAddY",         # cnt=9    Float            TLP:RotateAnim
    0x2C3187EA: "RotationAddZ",         # cnt=7    Float            TLP:RotateAnim
    0xB6C0BDF8: "CoreIntensity",        # cnt=7    Float            TLP:TubeLight
    0x8A565263: "TerminatePositionZ",   # cnt=6    Float            TLP:StrainRibbon
    0x0718D2B3: "LocalRotationY",       # cnt=6    Float            TLP:StrainRibbon
    0x6458334F: "TerminatePositionX",   # cnt=6    Float            TLP:StrainRibbon
    0x135F03D9: "TerminatePositionY",   # cnt=6    Float            TLP:StrainRibbon
    0x33A4A86B: "PlaySpeedCoef",        # cnt=5    Float            TLP:UVSequence
    0x0ECBFA29: "BrightThreshold",      # cnt=4    Float            TLP:RadialBlurFilterBehavior
    0x3A9708CC: "SizeZAdd",             # cnt=4    Float            TLP:ScaleAnim
    0x4E00491F: "IntensitySheet",       # cnt=4    Float            TLP:RgbWater
    0x0EF6ABF4: "BlurStart",            # cnt=3    Float            TLP:RadialBlurFilterBehavior
    0x4279F094: "mMaxIntensityRate",    # cnt=2    Float            TLP:0x7E51F5BD
    0x9E118309: "LocalRotationZ",       # cnt=2    Float            TLP:EmitterShape3D
    0x13804BC9: "mMinIntensityRate",    # cnt=2    Float            TLP:0x7E51F5BD
    0x4D41A06B: "NormalBlendRate",      # cnt=1    Float            TLP:MhEffectDecalBehavior
    0x71BCF0AA: "mFlowSpeed",           # cnt=1    Float            TLP:0x399DB6A9
    0x831B390B: "AlphaCorrectionMax",   # cnt=1    Float            TLP:MhEffectDecalBehavior
    0x16814F1C: "Roughness",            # cnt=1    Float            TLP:MhEffectDecalBehavior
    0x2FF50558: "Rotation",             # cnt=1    Float            TLP:Billboard3D
    0xAE7CD3C0: "RangeZ",              # cnt=1    Float            TLP:MhEffectDecalBehavior
    # ── 以下 102 条来自 RE DTI dump 的 nTimelineParam 类，**全语料 0 例**（官方一次没用过）。
    # hash 由 dump 直接给出，不是反查/合成。类型只取 f32/color 两种——它们与 TIML
    # dataType 的对应已被语料标定（f32→2 n=5647 / color→3 n=2106）；u32/bool/vector2 等
    # 没有任何语料样本可标定 dataType，写错会产出垃圾关键帧，故一律跳过（44 条）。
    0x086567E1: "InitialPositionZ",          # cnt=0    Float
    0x0E24B8A2: "mMultiplyRadiusSquare",     # cnt=0    Float
    0x102FFF8B: "HeadEffectiveRadius",       # cnt=0    Float
    0x1204171A: "OcclusionSphereRadius",     # cnt=0    Float
    0x19DCE197: "IntensityAlpha",            # cnt=0    Float
    0x275F8C25: "UVRangeZ",                  # cnt=0    Float
    0x28342187: "ToneCurveBy4",              # cnt=0    Float
    0x29922775: "ToneCurveGy0",              # cnt=0    Float
    0x2A6477AF: "ToneCurveRx0",              # cnt=0    Float
    0x2AD90E22: "ShadowDepthBias",           # cnt=0    Float
    0x2B0CF983: "CenterW",                   # cnt=0    Float
    0x2D09B3B6: "ToneCurveRx4",              # cnt=0    Float
    0x2EFFE36C: "ToneCurveGy4",              # cnt=0    Float
    0x2F59E59E: "ToneCurveBy0",              # cnt=0    Float
    0x2FCE3B02: "StartH",                    # cnt=0    Float
    0x30891634: "ToneCurveGx0",              # cnt=0    Float
    0x312F10C6: "ToneCurveBx4",              # cnt=0    Float
    0x337F46EE: "ToneCurveRy0",              # cnt=0    Float
    0x341282F7: "ToneCurveRy4",              # cnt=0    Float
    0x34B93272: "VignettingOffset",          # cnt=0    Float
    0x353D6FFC: "Fresnel",                   # cnt=0    Float
    0x3642D4DF: "ToneCurveBx0",              # cnt=0    Float
    0x37E4D22D: "ToneCurveGx4",              # cnt=0    Float
    0x40E3E2BB: "ToneCurveGx5",              # cnt=0    Float
    0x4145E449: "ToneCurveBx1",              # cnt=0    Float
    0x4315B261: "ToneCurveRy5",              # cnt=0    Float
    0x44787678: "ToneCurveRy1",              # cnt=0    Float
    0x46282050: "ToneCurveBx5",              # cnt=0    Float
    0x478E26A2: "ToneCurveGx1",              # cnt=0    Float
    0x49AE09C7: "EndH",                      # cnt=0    Float
    0x55BD853E: "CenterZ",                   # cnt=0    Float
    0x585ED508: "ToneCurveBy1",              # cnt=0    Float
    0x59EEF098: "UVRangeW",                  # cnt=0    Float
    0x59F8D3FA: "ToneCurveGy5",              # cnt=0    Float
    0x5A0E8320: "ToneCurveRx5",              # cnt=0    Float
    0x5D634739: "ToneCurveRx1",              # cnt=0    Float
    0x5E9517E3: "ToneCurveGy1",              # cnt=0    Float
    0x5F331111: "ToneCurveBy5",              # cnt=0    Float
    0x6ACAAAD7: "CoreLength",                # cnt=0    Float
    0x701FE225: "LocalRotationX",            # cnt=0    Float
    0x7516AA5D: "LocalRotation",             # cnt=0    Float
    0x7AF2755D: "ProjectionRotationZ",       # cnt=0    Float
    0x7D0FD331: "TailEffectiveRadius",       # cnt=0    Float
    0x7E6555E0: "FrameInfluence",            # cnt=0    Float
    0x7F2CEB57: "EmissiveColorRange",        # cnt=0    Color[RGBA]
    0x7F2FEA9B: "Falloff",                   # cnt=0    Float
    0x84E65AB1: "Spread",                    # cnt=0    Float
    0x916C365B: "InitialPositionY",          # cnt=0    Float
    0x931E7E65: "TextureScrollSpeed",        # cnt=0    Float
    0x93A7FACA: "VignettingPow",             # cnt=0    Float
    0x994333F2: "VertexInfluence",           # cnt=0    Float
    0x9DB8917C: "Acceleration",              # cnt=0    Float
    0xA2C636F7: "StartW",                    # cnt=0    Float
    0xA826417C: "ToneCurveBx7",              # cnt=0    Float
    0xA980478E: "ToneCurveGx3",              # cnt=0    Float
    0xAA2229C3: "LimitAngle",                # cnt=0    Float
    0xAA761754: "ToneCurveRy3",              # cnt=0    Float
    0xAD1BD34D: "ToneCurveRy7",              # cnt=0    Float
    0xAEED8397: "ToneCurveGx7",              # cnt=0    Float
    0xAF4B8565: "ToneCurveBx3",              # cnt=0    Float
    0xAFB51DF4: "IntensityCubeMap",          # cnt=0    Float
    0xB09B76CF: "ToneCurveGy3",              # cnt=0    Float
    0xB13D703D: "ToneCurveBy7",              # cnt=0    Float
    0xB1F7AEF0: "Metalic",                   # cnt=0    Float
    0xB36D2615: "ToneCurveRx3",              # cnt=0    Float
    0xB400E20C: "ToneCurveRx7",              # cnt=0    Float
    0xB650B424: "ToneCurveBy3",              # cnt=0    Float
    0xB78EE6DA: "BlendRate",                 # cnt=0    Float
    0xB7F6B2D6: "ToneCurveGy7",              # cnt=0    Float
    0xBA17788F: "EndZ",                      # cnt=0    Float
    0xBBB3E412: "CenterX",                   # cnt=0    Float
    0xBE56DD9F: "UVRangeY",                  # cnt=0    Float
    0xC0F18240: "ToneCurveGy6",              # cnt=0    Float
    0xC126B7BD: "MinRoughness",              # cnt=0    Float
    0xC15784B2: "ToneCurveBy2",              # cnt=0    Float
    0xC307D29A: "ToneCurveRx6",              # cnt=0    Float
    0xC46A1683: "ToneCurveRx2",              # cnt=0    Float
    0xC4A60432: "EndW",                      # cnt=0    Float
    0xC63A40AB: "ToneCurveBy6",              # cnt=0    Float
    0xC79C4659: "ToneCurveGy2",              # cnt=0    Float
    0xC893EE17: "OcclusionBlurWidthScale",   # cnt=0    Float
    0xC951ED09: "UVRangeX",                  # cnt=0    Float
    0xCCB4D484: "CenterY",                   # cnt=0    Float
    0xCDBCBB7E: "EmissionColorRange",        # cnt=0    Color[RGBA]
    0xCF5D2EDD: "ShadowSlopedDepthBias",     # cnt=0    Float
    0xD23788D3: "Cone",                      # cnt=0    Float
    0xD62C2891: "IntensitySpecular",         # cnt=0    Float
    0xD6AD0996: "ColorSheet",                # cnt=0    Color[RGBA]
    0xD84CB5F3: "ToneCurveBx2",              # cnt=0    Float
    0xD9EAB301: "ToneCurveGx6",              # cnt=0    Float
    0xDA1CE3DB: "ToneCurveRy6",              # cnt=0    Float
    0xDC774A4A: "StartZ",                    # cnt=0    Float
    0xDD7127C2: "ToneCurveRy2",              # cnt=0    Float
    0xDE877718: "ToneCurveGx2",              # cnt=0    Float
    0xDF2171EA: "ToneCurveBx6",              # cnt=0    Float
    0xE22A3268: "OcclusionBlurWidthOffset",  # cnt=0    Float
    0xE2C6589E: "rot",                       # cnt=0    Float
    0xE609DBFA: "ShadowMaxDepthBias",        # cnt=0    Float
    0xE66B06CD: "InitialPositionX",          # cnt=0    Float
    0xE7DF422D: "NearClipDistance",          # cnt=0    Float
    0xEEBD5618: "EmissionRate",              # cnt=0    Float
    0xF80CE653: "EmissionColor",             # cnt=0    Color[RGBA]
    # ══ 以下 722 条来自官方 TIML datatype dump（哈希 / 名字 / dataType 三列，797 条）══
    # 我们原有 75 条与该表逐条比对：74 条完全一致，1 条冲突（0xAFE95AC0 原记 ColorBase，
    # 表作 mBaseMapFactor，已按表订正——表的 m 前缀惯例自洽，且配对的
    # mBaseMapFactorIntensity(0x8F198226) 也在表内）。
    # 这批名字绝大多数是**材质/着色器参数**（mBaseMapFactor / mOpacityFactor / RimWidth /
    # UVTransformA_* / BurnControl_* / WaveAxis_* …），进一步印证语料里那些「没有独立属性
    # 块的未知 TLP」打的是材质而非特效。
    # 名字**原样保留**，含日文条目（如「バンク[0]」）——那是 dump 里的原文，不臆改。
    0x0074A898: 'HomingRate',                      # dataType 2
    0x00D534F9: 'mAttackAdjustAngleIndex',         # dataType 1
    0x01DBBF61: 'Vpivot_z',                        # dataType 2
    0x02571B8D: 'mMetalic',                        # dataType 2
    0x0297CFC9: 'Group 4',                         # dataType 0
    0x032F9113: 'ListIndex 15',                    # dataType 1
    0x036D877F: 'ステージめり込み対応距離（レイ判定の長さ）',           # dataType 2
    0x03840ECE: 'mDamageHitNo',                    # dataType 1
    0x03A7C305: 'TargetOfsPosY 00',                # dataType 2
    0x03AC6A9E: 'OfsPosY 11',                      # dataType 2
    0x03D46071: 'mOfsGravity 06',                  # dataType 2
    0x0426764B: 'mPartsMaskD',                     # dataType 2
    0x0442550A: 'ListIndex 11',                    # dataType 1
    0x04AF3E55: 'KeyFrame19',                      # dataType 4
    0x04B9A468: 'mOfsGravity 02',                  # dataType 2
    0x04C1AE87: 'OfsPosY 15',                      # dataType 2
    0x04CA071C: 'TargetOfsPosY 04',                # dataType 2
    0x04F09E88: 'mRaftPartsIndex',                 # dataType 1
    0x05FA0BD0: 'Group 0',                         # dataType 0
    0x06A9B550: 'mWpSnowShovelScaleX',             # dataType 2
    0x06D97C03: 'OfsPosZ 09',                      # dataType 2
    0x078C319A: 'FlowControl_y',                   # dataType 2
    0x0802F431: 'OfsPosZ 01',                      # dataType 2
    0x08095DAA: 'TargetOfsPosZ 10',                # dataType 2
    0x08431812: 'ReqNo C',                         # dataType 1
    0x08570195: 'UVTransformC_y',                  # dataType 2
    0x08FD20A6: 'mFlag',                           # dataType 1
    0x09956BA2: 'UVTransformB_y',                  # dataType 2
    0x0A3ECD23: 'ShootOfsDegY 06',                 # dataType 2
    0x0A74B667: 'KeyFrame11',                      # dataType 4
    0x0AD9C602: 'ReleaseTime C',                   # dataType 1
    0x0B001AA6: 'ChangeRightAngX',                 # dataType 2
    0x0B0F41DD: 'JointNo4',                        # dataType 0
    0x0BD3D5FB: 'UVTransformA_y',                  # dataType 2
    0x0CB6228E: '213262990',                       # dataType 2
    0x0CECC08D: 'コンスト部位ID[6]',                     # dataType 0
    0x0CFE3227: 'WeaponEfcValue_1',                # dataType 2
    0x0D19727E: 'KeyFrame15',                      # dataType 4
    0x0D2FAA31: 'WorkNo22',                        # dataType 1
    0x0D53093A: 'ShootOfsDegY 02',                 # dataType 2
    0x0D7C4B37: 'TargetOfsPosY 08',                # dataType 2
    0x0DB4021B: 'ReleaseTime G',                   # dataType 1
    0x0DDE7E84: 'mFakeLightIntensity',             # dataType 2
    0x0E424A70: 'mFilmBlend',                      # dataType 2
    0x0EA20FED: 'mSlingerShellAttach',             # dataType 0
    0x0F2EDC0B: 'ReqNo G',                         # dataType 1
    0x0F6499B3: 'TargetOfsPosZ 14',                # dataType 2
    0x0F6F3028: 'OfsPosZ 05',                      # dataType 2
    0x10272A04: 'mPartsMaskX',                     # dataType 2
    0x11126CEB: 'TargetOfsPosZ 00',                # dataType 2
    0x1119C570: 'OfsPosZ 11',                      # dataType 2
    0x11E4E7B1: 'mLookAtParam',                    # dataType 0
    0x11E90F5A: 'mFootStep',                       # dataType 0
    0x1213267E: 'ReqNo(Loop) R',                   # dataType 1
    0x1308E98E: 'mBlendBaseMapFactorIntensity',    # dataType 2
    0x13495749: 'mCamMarginEndItpTime',            # dataType 2
    0x136F8726: 'KeyFrame01',                      # dataType 4
    0x1382EC79: 'ListIndex 09',                    # dataType 1
    0x1402433F: 'KeyFrame05',                      # dataType 4
    0x1448387B: 'ShootOfsDegY 12',                 # dataType 2
    0x146CD3ED: 'OfsPosY 09',                      # dataType 2
    0x15EC38EA: 'mFlow_Strength',                  # dataType 2
    0x15EF861B: 'ReqNo(On) H',                     # dataType 1
    0x164DB124: 'mFinColor',                       # dataType 3
    0x16740169: 'OfsPosZ 15',                      # dataType 2
    0x167FA8F2: 'TargetOfsPosZ 04',                # dataType 2
    0x1892CC87: '412273799',                       # dataType 2
    0x18CC46B5: 'mFlowColor',                      # dataType 3
    0x1A34A052: 'ListIndex 05',                    # dataType 1
    0x1AB75BDF: 'OfsPosY 01',                      # dataType 2
    0x1ABCF244: 'TargetOfsPosY 10',                # dataType 2
    0x1C32DCCF: 'mVPushSpeed',                     # dataType 2
    0x1C59CA30: 'ReqNo(On) D',                     # dataType 1
    0x1D1A7784: 'BlendMatFactor_z',                # dataType 2
    0x1D59644B: 'ListIndex 01',                    # dataType 1
    0x1D7D5399: 'mCameraResetAngle',               # dataType 2
    0x1DB40F14: 'KeyFrame09',                      # dataType 4
    0x1DD1365D: 'TargetOfsPosY 14',                # dataType 2
    0x1DDA9FC6: 'OfsPosY 05',                      # dataType 2
    0x1E090F48: 'mFlag4',                          # dataType 1
    0x1E73EAB4: 'mViewParamPage',                  # dataType 0
    0x1F0BDACF: 'rot:z',                           # dataType 2
    0x1FC9E4D9: 'TargetOfsPosZ 08',                # dataType 2
    0x20E89CC5: 'JointNo 12',                      # dataType 0
    0x2159E5A4: 'KeyFrame21',                      # dataType 4
    0x216F3DEB: 'WorkNo16',                        # dataType 1
    0x2212F5DA: 'TargetOfsPosX 03',                # dataType 2
    0x22195C41: 'OfsPosX 12',                      # dataType 2
    0x223DB7D7: 'ShootOfsDegX 09',                 # dataType 2
    0x2285686D: 'mDotOpacity',                     # dataType 2
    0x2317001E: 'mBankType',                       # dataType 0
    0x23EDA9F5: '痕跡の種類',                           # dataType 1
    0x241F9852: 'KinkControl_y',                   # dataType 2
    0x24579AAB: 'ListIndex',                       # dataType 1
    0x24C60AC4: 'GameParam D',                     # dataType 2
    0x252961CD: 'mCameraShakeIndex',               # dataType 0
    0x254ECDBF: 'mStaminaRecoveryRate',            # dataType 2
    0x256C8A54: 'ChangeRightOfsZ',                 # dataType 2
    0x257F31C3: 'TargetOfsPosX 07',                # dataType 2
    0x2602F9F2: 'WorkNo12',                        # dataType 1
    0x26119EDA: '乗りぶつけダメージ方向',                     # dataType 0
    0x263421BD: 'KeyFrame25',                      # dataType 4
    0x274D4F11: 'mAttackAdjustGroundSpeedRate',    # dataType 2
    0x27C1934E: 'コンスト部位ID[5]',                     # dataType 0
    0x27D7D87B: 'mOfsSpeed 08',                    # dataType 2
    0x2845C079: 'ChangeRightType',                 # dataType 0
    0x290C5049: 'mOfsSpeed 00',                    # dataType 2
    0x2A83ABD7: '713272279',                       # dataType 0
    0x2AC902E3: 'mMotionSpeed',                    # dataType 2
    0x2B078267: 'mLookAtOfsAngleY',                # dataType 2
    0x2B8BFBFC: 'ShootOfsDegX 05',                 # dataType 2
    0x2BD52F93: 'mId',                             # dataType 1
    0x2BDE22C1: 'UIDの同期ID 03',                     # dataType 0
    0x2BF81C73: 'mCameraResetTime',                # dataType 2
    0x2C07B96F: 'mPowderScaleY',                   # dataType 2
    0x2C426079: 'mViewParamNo',                    # dataType 0
    0x2CB3E6D8: 'UIDの同期ID 07',                     # dataType 0
    0x2CE63FE5: 'ShootOfsDegX 01',                 # dataType 2
    0x2CFC1613: 'mCamBasePosOffsetNo',             # dataType 0
    0x2D7046EF: 'GameParam H',                     # dataType 2
    0x2D7DE338: 'Index 12',                        # dataType 1
    0x2DDB0D26: '押当たり補間（秒）',                       # dataType 2
    0x2E2C1393: 'OfsPosY',                         # dataType 2
    0x2E619450: 'mOfsSpeed 04',                    # dataType 2
    0x2E710D12: 'mAlbedoBlend',                    # dataType 2
    0x2EF52DB5: 'mTurnBaseDir',                    # dataType 0
    0x2F5A867F: 'DisplaceControl_w',               # dataType 2
    0x2F826D96: 'KeyFrame29',                      # dataType 4
    0x2FD81AE9: '持ち物２のMotionNo',                   # dataType 0
    0x2FD8A1F4: 'WaveAxis_z',                      # dataType 2
    0x30176108: 'mOfsSpeed 10',                    # dataType 2
    0x3033C6D8: 'mFingerMotionType',               # dataType 0
    0x310FD720: '特殊採取のタイプ',                        # dataType 0
    0x325FCB10: 'mMummyColor',                     # dataType 3
    0x32C51380: 'UIDの同期ID 13',                     # dataType 0
    0x32F86046: 'イベクエ落し物関節番号',                     # dataType 0
    0x330B1660: 'Index 06',                        # dataType 1
    0x33A4D9DC: '押当たり種類',                          # dataType 0
    0x344C2BE4: 'SkipTime J',                      # dataType 1
    0x3466D279: 'Index 02',                        # dataType 1
    0x35FD0EA4: 'ShootOfsDegX 11',                 # dataType 2
    0x37484700: 'mCharmMountCamSpring',            # dataType 2
    0x377AA511: 'mOfsSpeed 14',                    # dataType 2
    0x3842D4E5: 'KeyFrame31',                      # dataType 4
    0x38740CAA: 'WorkNo06',                        # dataType 1
    0x393BCA4E: 'PosRotateY 05',                   # dataType 2
    0x39F3AD84: 'JointNo 02',                      # dataType 0
    0x3A8C67F8: 'ReqNo(Off) K',                    # dataType 1
    0x3A97A3D6: 'SkipTime B',                      # dataType 1
    0x3B026D00: 'OfsPosX 02',                      # dataType 2
    0x3B09C49B: 'TargetOfsPosX 13',                # dataType 2
    0x3C036342: 'TargetReqNo B',                   # dataType 0
    0x3C6FA919: 'OfsPosX 06',                      # dataType 2
    0x3C81CEA1: 'mParamType4',                     # dataType 0
    0x3CA9C405: 'mFlowDirUVPhaseShift',            # dataType 2
    0x3DFA67CF: 'SkipTime F',                      # dataType 1
    0x3E560E57: 'PosRotateY 01',                   # dataType 2
    0x3E9E699D: 'JointNo 06',                      # dataType 0
    0x3EDAA20F: 'コンスト部位ID[4]',                     # dataType 0
    0x3F19C8B3: 'WorkNo02',                        # dataType 1
    0x3F3D39AE: 'ChangeLeftOfsY',                  # dataType 2
    0x3FC9E0B8: '1070194872',                      # dataType 0
    0x407D9587: 'mOfsSpeed 15',                    # dataType 2
    0x416522B1: 'mWeaponMotionStartFrame',         # dataType 2
    0x41E07A9B: 'mOtRideMuteRotRate',              # dataType 2
    0x41E7D172: 'NULL傾き固定時の回転完了するまでのフレーム数',        # dataType 1
    0x4258D1A6: 'mActionPhaseSub',                 # dataType 0
    0x42FA3E32: 'ShootOfsDegX 10',                 # dataType 2
    0x4313B645: 'mOpacityPow',                     # dataType 2
    0x434B1B72: 'SkipTime K',                      # dataType 1
    0x4361E2EF: 'Index 03',                        # dataType 1
    0x43AC6AB4: 'FlowMatControl_w',                # dataType 2
    0x43AD564A: 'コンスト部位ID[1]',                     # dataType 0
    0x440C26F6: 'Index 07',                        # dataType 1
    0x444D2CFC: 'mWeaponMotionSpeed',              # dataType 2
    0x45C22316: 'UIDの同期ID 12',                     # dataType 0
    0x4625CE84: 'AnimNo A',                        # dataType 0
    0x4710519E: 'mOfsSpeed 11',                    # dataType 2
    0x481EF825: 'WorkNo03',                        # dataType 1
    0x483A0938: 'ChangeLeftOfsX',                  # dataType 2
    0x48EF8D8E: 'mTranslucency',                   # dataType 2
    0x49513EC1: 'PosRotateY 00',                   # dataType 2
    0x4999590B: 'JointNo 07',                      # dataType 0
    0x4A6DD631: 'LeftPartsNo[2]',                  # dataType 0
    0x4AAAF206: 'mToneAlpha',                      # dataType 2
    0x4AFD5759: 'SkipTime G',                      # dataType 1
    0x4B0453D4: 'TargetReqNo C',                   # dataType 0
    0x4B68998F: 'OfsPosX 07',                      # dataType 2
    0x4BD36121: 'mCameraShakePage',                # dataType 0
    0x4C055D96: 'OfsPosX 03',                      # dataType 2
    0x4C0EF40D: 'TargetOfsPosX 12',                # dataType 2
    0x4CEB3A2E: 'mParamType1',                     # dataType 0
    0x4D26ED7B: 'シェーダーのタイプ',                       # dataType 0
    0x4D6470C6: '対象ダメージ部位',                        # dataType 0
    0x4D8B576E: 'ReqNo(Off) J',                    # dataType 1
    0x4D909340: 'SkipTime C',                      # dataType 1
    0x4DA1831A: 'mOtRideAlphaFootOffsetIndex',     # dataType 1
    0x4E3CFAD8: 'PosRotateY 04',                   # dataType 2
    0x4EDC6CE0: 'mAddNormalMaskD',                 # dataType 2
    0x4EF49D12: 'JointNo 03',                      # dataType 0
    0x4F45E473: 'KeyFrame30',                      # dataType 4
    0x4F4B4BA4: 'mWeaponMotionInterpolationTime',  # dataType 2
    0x4F733C3C: 'WorkNo07',                        # dataType 1
    0x5009B3C9: '対象の乗り部位',                         # dataType 0
    0x504F8D1C: 'mFungusDamageRate',               # dataType 2
    0x50D0E8ED: 'mOfsSpeed 09',                    # dataType 2
    0x5105C964: 'WorkNo13',                        # dataType 1
    0x5133112B: 'KeyFrame24',                      # dataType 4
    0x51EBFAC2: 'DisplaceControl_z',               # dataType 2
    0x526AF201: 'トゲ(頭)状態',                         # dataType 0
    0x52780155: 'TargetOfsPosX 06',                # dataType 2
    0x5297382F: 'mFlowTile',                       # dataType 2
    0x52EC2264: '持ち物２のBankNo',                     # dataType 0
    0x5376E770: 'LeftPartsNo[3]',                  # dataType 0
    0x53984EE2: '使用する当たり',                         # dataType 0
    0x53A3F45E: 'MummyMatControl_w',               # dataType 2
    0x53B6808F: 'mVerticalOpacityPowInv',          # dataType 2
    0x53C13A52: 'GameParam E',                     # dataType 2
    0x548DFF15: 'SlotNo A',                        # dataType 0
    0x54ACFE4B: 'GameParam A',                     # dataType 2
    0x5515C54C: 'TargetOfsPosX 02',                # dataType 2
    0x551E6CD7: 'OfsPosX 13',                      # dataType 2
    0x553A8741: 'ShootOfsDegX 08',                 # dataType 2
    0x565ED532: 'KeyFrame20',                      # dataType 4
    0x56680D7D: 'WorkNo17',                        # dataType 1
    0x56B7B840: 'mDistortionFactorIntensity',      # dataType 2
    0x570F638E: 'mTransAdjustDist',                # dataType 2
    0x5782A950: 'ギミックの生成確率(%)',                    # dataType 1
    0x57EFAC53: 'JointNo 13',                      # dataType 0
    0x57F1682F: '(新)スケール指定(0～1)',                  # dataType 2
    0x5846C819: '持ち物１のMotionNo',                   # dataType 0
    0x58855D00: 'KeyFrame28',                      # dataType 4
    0x5966A4C6: 'mOfsSpeed 05',                    # dataType 2
    0x5A7AD3AE: 'Index 13',                        # dataType 1
    0x5AB6670B: 'コンスト部位ID[0]',                     # dataType 0
    0x5AFE86CE: 'Y座標',                             # dataType 2
    0x5B0089F9: 'mPowderScaleX',                   # dataType 2
    0x5BB4D64E: 'UIDの同期ID 06',                     # dataType 0
    0x5BE10F73: 'ShootOfsDegX 00',                 # dataType 2
    0x5C00B2F1: 'mLookAtOfsAngleX',                # dataType 2
    0x5C5DA290: 'mTurnAng',                        # dataType 2
    0x5C8CCB6A: 'ShootOfsDegX 04',                 # dataType 2
    0x5C918AD5: '痕跡の生成位置',                         # dataType 0
    0x5CD91257: 'UIDの同期ID 02',                     # dataType 0
    0x5CE43CEA: 'RightObjMotNo',                   # dataType 1
    0x5D252E24: '部位制御条件',                          # dataType 0
    0x5E0B60DF: 'mOfsSpeed 01',                    # dataType 2
    0x5E6EC814: 'mAngleInterpolationProgress',     # dataType 2
    0x5F8AD46D: 'mPreyShotTiming',                 # dataType 2
    0x5FF25223: '持ち物４のBankNo',                     # dataType 0
    0x605428A3: 'mAddNormalBlend',                 # dataType 2
    0x6069E6F0: 'ReleaseTime R',                   # dataType 1
    0x60849F2A: 'pos:z',                           # dataType 2
    0x614085F2: 'LeftPartsNo[1]',                  # dataType 0
    0x6159A4F6: 'mMotionTransRateZ',               # dataType 2
    0x617331FF: 'OfsPosZ 14',                      # dataType 2
    0x61789864: 'TargetOfsPosZ 05',                # dataType 2
    0x62CEFAB3: '痕跡の生成向き',                         # dataType 0
    0x630573A9: 'KeyFrame04',                      # dataType 4
    0x6324C0C4: 'mEpvIndex',                       # dataType 1
    0x634D2DC4: 'mHeat',                           # dataType 2
    0x634F08ED: 'ShootOfsDegY 13',                 # dataType 2
    0x636BE37B: 'OfsPosY 08',                      # dataType 2
    0x637622C1: 'mFakeLightColor',                 # dataType 3
    0x63AB0B39: 'BlendMatFactor_w',                # dataType 2
    0x64264EA4: 'SpeedRateV',                      # dataType 2
    0x6459658B: 'mEfcElementID',                   # dataType 1
    0x6468B7B0: 'KeyFrame00',                      # dataType 4
    0x6485DCEF: 'ListIndex 08',                    # dataType 1
    0x648CADCE: 'mCamMarginEndEaseType',           # dataType 0
    0x6600C6AB: '首補正の計算軸一律設定',                     # dataType 1
    0x66155C7D: 'TargetOfsPosZ 01',                # dataType 2
    0x661EF5E6: 'OfsPosZ 10',                      # dataType 2
    0x665699CA: 'ChangeLeftAngZ',                  # dataType 2
    0x66DAFF30: 'mAddColorA',                      # dataType 3
    0x670FA57B: 'NULL傾き固定時の回転する最大角度',              # dataType 2
    0x671F5908: 'Tag 2',                           # dataType 0
    0x67201A92: 'mPartsMaskY',                     # dataType 2
    0x67921A5E: 'mWeaponMotionNo',                 # dataType 0
    0x67D78EA5: 'mTotalOpacity',                   # dataType 2
    0x68800589: 'コンスト部位ID[2]',                     # dataType 0
    0x68CED44F: 'TargetOfsPosZ 09',                # dataType 2
    0x690E3FDE: 'mFlag5',                          # dataType 1
    0x69A80037: 'HitIndex',                        # dataType 0
    0x69D119C8: '1775311304',                      # dataType 2
    0x69D5D953: 'ブレスチャージ状態',                       # dataType 0
    0x6A5E54DD: 'ListIndex 00',                    # dataType 1
    0x6AB33F82: 'KeyFrame08',                      # dataType 4
    0x6AD606CB: 'TargetOfsPosY 15',                # dataType 2
    0x6ADDAF50: 'OfsPosY 04',                      # dataType 2
    0x6B169C53: 'Z座標',                             # dataType 2
    0x6B5EFAA6: 'ReqNo(On) E',                     # dataType 1
    0x6C333EBF: 'ReqNo(On) A',                     # dataType 1
    0x6CC0B887: 'シェーダーのかけ具合(0.0~1.0)',             # dataType 2
    0x6D3390C4: 'ListIndex 04',                    # dataType 1
    0x6DB06B49: 'OfsPosY 00',                      # dataType 2
    0x6DBBC2D2: 'TargetOfsPosY 11',                # dataType 2
    0x6DD60D01: 'mMotionCameraCutNo',              # dataType 0
    0x6E63FBC7: 'mFlag1',                          # dataType 1
    0x6F89CC07: 'PosRotateX 07',                   # dataType 2
    0x708B010C: 'FlowControl_x',                   # dataType 2
    0x70D64E7E: 'コリジョンユニークID',                     # dataType 0
    0x7154FE35: 'BurnControl_z',                   # dataType 2
    0x719B34C8: 'コンスト部位ID[3]',                     # dataType 0
    0x71AE85C6: 'mWpSnowShovelScaleY',             # dataType 2
    0x71DE4C95: 'OfsPosZ 08',                      # dataType 2
    0x72A97996: 'mDetailA_ColorIntensity',         # dataType 2
    0x72FD3B46: 'Group 1',                         # dataType 0
    0x7345659C: 'ListIndex 10',                    # dataType 1
    0x736F4F29: '首補正のブレンド率',                       # dataType 2
    0x73A80EC3: 'KeyFrame18',                      # dataType 4
    0x73BE94FE: 'mOfsGravity 03',                  # dataType 2
    0x73C69E11: 'OfsPosY 14',                      # dataType 2
    0x73CD378A: 'TargetOfsPosY 05',                # dataType 2
    0x7428A185: 'ListIndex 14',                    # dataType 1
    0x744C82C4: 'mPartsMaskA',                     # dataType 2
    0x74A0F393: 'TargetOfsPosY 01',                # dataType 2
    0x74AB5A08: 'OfsPosY 10',                      # dataType 2
    0x74D350E7: 'mOfsGravity 07',                  # dataType 2
    0x7590FF5F: 'Group 5',                         # dataType 0
    0x75B22732: 'mPosInterpolationProgress',       # dataType 2
    0x7747F0BC: 'mHyperArmorTime',                 # dataType 2
    0x77E68312: '首補正のブレス関節',                       # dataType 1
    0x7829EC9D: 'ReqNo F',                         # dataType 1
    0x785BB4B3: 'LeftPartsNo[0]',                  # dataType 0
    0x7863A925: 'TargetOfsPosZ 15',                # dataType 2
    0x786800BE: 'OfsPosZ 04',                      # dataType 2
    0x787E9FD5: 'mCameraPhase',                    # dataType 0
    0x7A1E42E8: 'KeyFrame14',                      # dataType 4
    0x7A289AA7: 'WorkNo23',                        # dataType 1
    0x7A5439AC: 'ShootOfsDegY 03',                 # dataType 2
    0x7A7B7BA1: 'TargetOfsPosY 09',                # dataType 2
    0x7A88BE0F: 'scl:z',                           # dataType 2
    0x7AB3328D: 'ReleaseTime F',                   # dataType 1
    0x7B65B552: 'JointNo1',                        # dataType 0
    0x7BE08FA2: 'エラ状態',                            # dataType 0
    0x7BF902B1: 'WeaponEfcValue_0',                # dataType 2
    0x7C072A30: 'ChangeRightAngY',                 # dataType 2
    0x7C692062: 'mAlphaTestControl',               # dataType 2
    0x7CD4E56D: 'UVTransformA_x',                  # dataType 2
    0x7D39FDB5: 'ShootOfsDegY 07',                 # dataType 2
    0x7D6D165D: '部位に対する処理',                        # dataType 0
    0x7D7386F1: 'KeyFrame10',                      # dataType 4
    0x7DDEF694: 'ReleaseTime B',                   # dataType 1
    0x7F05C4A7: 'OfsPosZ 00',                      # dataType 2
    0x7F0E6D3C: 'TargetOfsPosZ 11',                # dataType 2
    0x7F442884: 'ReqNo B',                         # dataType 1
    0x7F503103: 'UVTransformC_x',                  # dataType 2
    0x806D9AEB: 'mFlag3',                          # dataType 1
    0x80983795: 'mPartsMaskW',                     # dataType 2
    0x80A0A8E4: 'ノードID[2]',                        # dataType 1
    0x814568E2: 'ギミックの生成位置',                       # dataType 0
    0x819DDA91: 'RightPartsNo[1]',                 # dataType 0
    0x823D5F93: 'ReqNo(On) C',                     # dataType 1
    0x825101E5: 'mEmbankmentScale',                # dataType 2
    0x833DF1E8: 'ListIndex 06',                    # dataType 1
    0x83B5A3FE: 'TargetOfsPosY 13',                # dataType 2
    0x83BE0A65: 'OfsPosY 02',                      # dataType 2
    0x840FFF51: 'mVerticalOpacityPow',             # dataType 2
    0x8413263E: 'BlendMatFactor_y',                # dataType 2
    0x845035F1: 'ListIndex 02',                    # dataType 1
    0x84745897: 'mActionPhase',                    # dataType 0
    0x84751FE5: 'MaskBlend_B_x',                   # dataType 2
    0x84D3CE7C: 'OfsPosY 06',                      # dataType 2
    0x85509B8A: 'ReqNo(On) G',                     # dataType 1
    0x859AB9C4: 'mLinkMotionPhase',                # dataType 0
    0x85A14934: 'mStaminaParamIndex',              # dataType 1
    0x86028B75: 'rot:y',                           # dataType 2
    0x8633A1BC: 'MaskBlend_A_x',                   # dataType 2
    0x86859DBE: 'mNoSetWeaponMotionNo',            # dataType 0
    0x86C1A86E: 'mCameraSmoothTime',               # dataType 2
    0x87A41BA7: 'LeftObjMotNo',                    # dataType 1
    0x881094CA: 'OfsPosZ 12',                      # dataType 2
    0x881B3D51: 'TargetOfsPosZ 03',                # dataType 2
    0x8858F8E6: 'ChangeLeftAngX',                  # dataType 2
    0x890766C1: '突進シェルの回転速度',                      # dataType 2
    0x89113824: 'Tag 0',                           # dataType 0
    0x89582088: 'PartsNo[2]',                      # dataType 0
    0x8A2CADD8: 'ShootOfsDegY 15',                 # dataType 2
    0x8A3D617C: '斜面補正フレーム',                        # dataType 1
    0x8A66D69C: 'KeyFrame02',                      # dataType 4
    0x8B1A77C4: 'ReqNo(Loop) Q',                   # dataType 1
    0x8BF31826: 'RimPower',                        # dataType 2
    0x8D0B1285: 'KeyFrame06',                      # dataType 4
    0x8D3DCACA: 'WorkNo31',                        # dataType 1
    0x8D4169C1: 'ShootOfsDegY 11',                 # dataType 2
    0x8D65C9C7: 'MatID A',                         # dataType 0
    0x8DA66231: 'バンク[2]',                          # dataType 1
    0x8E7CFC3D: 'Tag 4',                           # dataType 0
    0x8E8AFE06: 'pos:x',                           # dataType 2
    0x8F1479C1: 'mToneEdge',                       # dataType 2
    0x8F198226: 'mBaseMapFactorIntensity',         # dataType 2
    0x8F2A4EA9: 'UIDの同期ID[6]',                     # dataType 0
    0x8F57C5DA: 'mMotionTransRateX',               # dataType 2
    0x8F76F948: 'TargetOfsPosZ 07',                # dataType 2
    0x8F79C6AF: 'mEfcIndexID',                     # dataType 1
    0x91000C10: 'TargetOfsPosZ 13',                # dataType 2
    0x910BA58B: 'OfsPosZ 02',                      # dataType 2
    0x915E502F: 'UVTransformC_z',                  # dataType 2
    0x91B8DFC5: '斜面補正最大角度',                        # dataType 2
    0x91BA4212: 'mNextFootStepFrame',              # dataType 2
    0x92DA8441: 'UVTransformA_z',                  # dataType 2
    0x93315130: 'mFlowDirFlowSpeed',               # dataType 2
    0x93379C99: 'ShootOfsDegY 05',                 # dataType 2
    0x934B3F92: 'WorkNo25',                        # dataType 1
    0x937DE7DD: 'KeyFrame12',                      # dataType 4
    0x93F18F03: 'mDetailEmitIntensity',            # dataType 2
    0x941023C4: 'KeyFrame16',                      # dataType 4
    0x9426FB8B: 'WorkNo21',                        # dataType 1
    0x94526271: '拘束リリースタイプ',                       # dataType 0
    0x945A5880: 'ShootOfsDegY 01',                 # dataType 2
    0x9486DF23: 'scl:x',                           # dataType 2
    0x94BD5370: 'バンク[3]',                          # dataType 1
    0x94BD53A1: 'ReleaseTime D',                   # dataType 1
    0x952B4933: 'mDisplacementFactor',             # dataType 2
    0x956BD47E: 'JointNo3',                        # dataType 0
    0x95A3A1D3: 'Blend',                           # dataType 2
    0x96278DB1: 'ReqNo D',                         # dataType 1
    0x96317FE8: 'UIDの同期ID[7]',                     # dataType 0
    0x965A5CA7: 'mActionState',                    # dataType 1
    0x96666192: 'OfsPosZ 06',                      # dataType 2
    0x9748E46B: 'mMotionCameraMoveAngle',          # dataType 2
    0x97876D34: 'ヒレ状態',                            # dataType 0
    0x979BE3E1: 'ChangeRightJntNo',                # dataType 0
    0x987DC45A: 'mFakeLightColorIntensity',        # dataType 2
    0x9886EBD0: 'RightPartsNo[0]',                 # dataType 0
    0x98D2EEDB: 'Vpivot_y',                        # dataType 2
    0x99897C43: 'mTrigger',                        # dataType 1
    0x99BB99A5: 'ノードID[3]',                        # dataType 1
    0x9A42E3E8: 'mPartsMaskC',                     # dataType 2
    0x9A690877: 'mMummyColorIntensity',            # dataType 2
    0x9A81D0B2: 'ShootOfsDegY 09',                 # dataType 2
    0x9AA53B24: 'OfsPosY 12',                      # dataType 2
    0x9AAE92BF: 'TargetOfsPosY 03',                # dataType 2
    0x9ADD31CB: 'mOfsGravity 05',                  # dataType 2
    0x9AFD73B9: 'WorkNo29',                        # dataType 1
    0x9B446023: 'Mask1',                           # dataType 2
    0x9B50DD3B: '痕跡の生成確率(%)',                      # dataType 1
    0x9BD476A9: 'mRidingState',                    # dataType 0
    0x9BE2D228: 'mColor',                          # dataType 3
    0x9C483C6C: 'AngleY1',                         # dataType 0
    0x9D0B1F8A: 'ReleaseTime H',                   # dataType 1
    0x9D4B04B0: 'ListIndex 12',                    # dataType 1
    0x9D58B92F: 'mNoHitTime',                      # dataType 2
    0x9DB0F5D2: 'mOfsGravity 01',                  # dataType 2
    0x9DC356A6: 'TargetOfsPosY 07',                # dataType 2
    0x9DE5F095: '2649092245',                      # dataType 2
    0x9E2973C7: 'SpeedRateH',                      # dataType 2
    0x9E856020: 'FlowControl_z',                   # dataType 2
    0x9F326571: 'mAngleFade',                      # dataType 2
    0x9F5A9F19: 'BurnControl_x',                   # dataType 2
    0x9F7B1E77: 'mGimmickWaitCamMoveAngle',        # dataType 2
    0x9F91C19A: 'ReqNo H',                         # dataType 1
    0xA0FAFC3E: 'JointNo 01',                      # dataType 0
    0xA17D5D10: 'WorkNo05',                        # dataType 1
    0xA19FD528: 'mInnerOffsetScale',               # dataType 2
    0xA1ED0144: 'mDetailA_Color',                  # dataType 3
    0xA2009521: 'TargetOfsPosX 10',                # dataType 2
    0xA20B3CBA: 'OfsPosX 01',                      # dataType 2
    0xA23E2927: 'mDetailDisplacement',             # dataType 2
    0xA26628EB: 'mWeaponGaugeIndex',               # dataType 0
    0xA267F6E1: 'TargetReqNo E',                   # dataType 0
    0xA275734B: 'PartsNo[1]',                      # dataType 0
    0xA2E55B02: 'mParamType3',                     # dataType 0
    0xA31F6F44: '2736746308',                      # dataType 2
    0xA39EF26C: 'SkipTime A',                      # dataType 1
    0xA3B40BF1: 'Index 09',                        # dataType 1
    0xA4071D6A: 'UIDの同期ID[5]',                     # dataType 0
    0xA48C272D: 'mRideReduceStaminaLv',            # dataType 0
    0xA4C789DC: 'mFinColorB',                      # dataType 3
    0xA4F33675: 'SkipTime E',                      # dataType 1
    0xA50A32F8: 'TargetReqNo A',                   # dataType 0
    0xA566F8A3: 'OfsPosX 05',                      # dataType 2
    0xA56D5138: 'TargetOfsPosX 14',                # dataType 2
    0xA6109909: 'WorkNo01',                        # dataType 1
    0xA6346814: 'ChangeLeftOfsZ',                  # dataType 2
    0xA68B31F2: 'バンク[1]',                          # dataType 1
    0xA75F5FED: 'PosRotateY 02',                   # dataType 2
    0xA7973827: 'JointNo 05',                      # dataType 0
    0xA8CB113B: 'WorkNo09',                        # dataType 1
    0xA91E30B2: 'mOfsSpeed 13',                    # dataType 2
    0xAA0247DA: 'Index 05',                        # dataType 1
    0xAAB08952: 'RightPartsNo[2]',                 # dataType 0
    0xAB1D2239: 'ChangeLeftType',                  # dataType 1
    0xAB3C8B55: 'WeaponEfcType_0',                 # dataType 0
    0xAB8DFB27: 'ノードID[1]',                        # dataType 1
    0xABCC423A: 'UIDの同期ID 10',                     # dataType 0
    0xAC635CA9: 'RimWidth',                        # dataType 2
    0xACA18623: 'UIDの同期ID 14',                     # dataType 0
    0xACD0B488: 'OfsPosX 09',                      # dataType 2
    0xACF45F1E: 'ShootOfsDegX 12',                 # dataType 2
    0xAD457A5E: 'SkipTime I',                      # dataType 1
    0xAD6F83C3: 'Index 01',                        # dataType 1
    0xADC3C03B: 'mOtRideBustUseItemBlendRate',     # dataType 2
    0xADD4B2B7: 'mVPushWave',                      # dataType 2
    0xAE21740C: 'JointNo 09',                      # dataType 0
    0xAEDA6B35: 'mMoveBankType',                   # dataType 0
    0xB00501F3: 'mOfsSpeed 03',                    # dataType 2
    0xB0BBDAC4: 'mEfcJointNo',                     # dataType 0
    0xB1D0207A: 'WorkNo19',                        # dataType 1
    0xB282AA46: 'ShootOfsDegX 06',                 # dataType 2
    0xB28DA0BE: '部位制御値（最大値の割合）',                   # dataType 2
    0xB296CA66: 'ノードID[0]',                        # dataType 1
    0xB2AE1E00: 'mTurnSpeed',                      # dataType 2
    0xB2D7737B: 'UIDの同期ID 00',                     # dataType 0
    0xB319769B: 'Index 15',                        # dataType 1
    0xB3371B60: 'mAimViewParamId',                 # dataType 0
    0xB359AC53: '氷塊シェルNo',                         # dataType 0
    0xB3ABB813: 'RightPartsNo[3]',                 # dataType 0
    0xB458B56D: 'mCamMarginDist',                  # dataType 2
    0xB474B282: 'Index 11',                        # dataType 1
    0xB50EE8D5: 'mPowderScaleZ',                   # dataType 2
    0xB52636D6: 'mIntensity',                      # dataType 2
    0xB5BAB762: 'UIDの同期ID 04',                     # dataType 0
    0xB5C02C52: 'TargetOfsPosX 08',                # dataType 2
    0xB5EF6E5F: 'ShootOfsDegX 02',                 # dataType 2
    0xB7254229: 'OfsPosZ',                         # dataType 2
    0xB768C5EA: 'mOfsSpeed 07',                    # dataType 2
    0xB821F4A8: 'mTransAdjustEndDist',             # dataType 2
    0xB83AF0BE: 'mFinColorBIntensity',             # dataType 2
    0xB83FEC3D: 'mDisableIKInterpolationFrame',    # dataType 2
    0xB850B41E: 'KeyFrame22',                      # dataType 4
    0xB858DE36: 'mFinColorIntensity',              # dataType 2
    0xB8666C51: 'WorkNo15',                        # dataType 1
    0xB8895311: 'mVPushBlend',                     # dataType 2
    0xB8BFBF9E: 'mUV_Blend',                       # dataType 2
    0xB9DB9967: '持ち物１のBankNo',                     # dataType 0
    0xB9E1CD7F: 'JointNo 11',                      # dataType 0
    0xBA3F398C: 'mLookAtSpeedRate',                # dataType 2
    0xBAA29F67: 'GameParam C',                     # dataType 2
    0xBB100DFB: 'OfsPosX 11',                      # dataType 2
    0xBB1BA460: 'TargetOfsPosX 00',                # dataType 2
    0xBB6E420A: 'PartsNo[0]',                      # dataType 0
    0xBBB2C830: '乗りぶつけ蓄積値',                        # dataType 2
    0xBC0CFB49: 'UIDの同期ID 08',                     # dataType 0
    0xBC65DBEE: 'ChangeRightOfsY',                 # dataType 2
    0xBC766079: 'TargetOfsPosX 04',                # dataType 2
    0xBC7DC9E2: 'OfsPosX 15',                      # dataType 2
    0xBCFFC41B: 'TargetUnitNo',                    # dataType 0
    0xBD16C9E8: 'KinkControl_z',                   # dataType 2
    0xBD1C2C2B: 'UIDの同期ID[4]',                     # dataType 0
    0xBDCF5B7E: 'GameParam G',                     # dataType 2
    0xBE4DB7FA: 'Index',                           # dataType 1
    0xBE8C0966: 'JointNo 15',                      # dataType 0
    0xBF0BA848: 'WorkNo11',                        # dataType 1
    0xBF8F9DFD: 'ギミックの種類',                         # dataType 0
    0xBF9000B3: 'バンク[0]',                          # dataType 1
    0xBFD77429: 'ステージめり込み対応の移動速度（1秒/距離）',          # dataType 2
    0xBFE59BEE: 'DisplaceControl_x',               # dataType 2
    0xBFE81475: 'mSaturationColor',                # dataType 3
    0xC06BD86E: 'UIDの同期ID[1]',                     # dataType 0
    0xC06FF57C: 'mOfsSpeed 06',                    # dataType 2
    0xC0E5BF09: '持ち物４のMotionNo',                   # dataType 0
    0xC1D6C0D8: 'WaveAxis_x',                      # dataType 2
    0xC29F20E0: 'WeaponExternValue',               # dataType 2
    0xC2BD87F4: 'UIDの同期ID 05',                     # dataType 0
    0xC2C71CC4: 'TargetOfsPosX 09',                # dataType 2
    0xC2E7F4F6: 'バンク[5]',                          # dataType 1
    0xC2E85EC9: 'ShootOfsDegX 03',                 # dataType 2
    0xC2F1EB55: 'mVolumeBlend',                    # dataType 2
    0xC31CE9CF: 'MummyMatControl_x',               # dataType 2
    0xC3738214: 'Index 10',                        # dataType 1
    0xC39F54D7: 'mBaseColorSaturation',            # dataType 2
    0xC41E460D: 'Index 14',                        # dataType 1
    0xC4774EC6: 'mSuperArmorTime',                 # dataType 2
    0xC56404BB: 'mEmitBlend',                      # dataType 2
    0xC56C2A11: '高い壁ジャンプ(15m以上)時のY方向のスピード倍率(0.0~1.0)', # dataType 2
    0xC580048C: 'PosRotateZ 07',                   # dataType 2
    0xC5859AD0: 'ShootOfsDegX 07',                 # dataType 2
    0xC5D043ED: 'UIDの同期ID 01',                     # dataType 0
    0xC6785402: '3329774594',                      # dataType 2
    0xC6D710EC: 'WorkNo18',                        # dataType 1
    0xC7023165: 'mOfsSpeed 02',                    # dataType 2
    0xC80C98DE: 'WorkNo10',                        # dataType 1
    0xC865FF9E: 'mTurnSpeed2',                     # dataType 2
    0xC8D913CA: 'mFilmBlendB',                     # dataType 2
    0xC98B39F0: 'JointNo 14',                      # dataType 0
    0xC9C0F038: 'mFilmThickness',                  # dataType 2
    0xCB0BCBDF: 'UIDの同期ID 09',                     # dataType 0
    0xCB62EB78: 'ChangeRightOfsX',                 # dataType 2
    0xCB7150EF: 'TargetOfsPosX 05',                # dataType 2
    0xCB7AF974: 'OfsPosX 14',                      # dataType 2
    0xCBAF549E: '鉱石変更数',                           # dataType 0
    0xCC173D6D: 'OfsPosX 10',                      # dataType 2
    0xCC1C94F6: 'TargetOfsPosX 01',                # dataType 2
    0xCD9E0D40: '3449687360',                      # dataType 0
    0xCDA5AFF1: 'GameParam B',                     # dataType 2
    0xCEE6FDE9: 'JointNo 10',                      # dataType 0
    0xCF578488: 'KeyFrame23',                      # dataType 4
    0xCF615CC7: 'WorkNo14',                        # dataType 1
    0xCFE13E23: 'ノードID[5]',                        # dataType 1
    0xD0586F7B: 'PosRotateY 03',                   # dataType 2
    0xD09008B1: 'JointNo 04',                      # dataType 0
    0xD0B8F943: 'mAddNormalMaskC',                 # dataType 2
    0xD1053969: 'mViewParamIdCamColAdj',           # dataType 0
    0xD117A99F: 'WorkNo00',                        # dataType 1
    0xD245B590: 'mParallaxFactor',                 # dataType 2
    0xD25D5A7B: '首補正の高さオフセット',                     # dataType 2
    0xD261C835: 'OfsPosX 04',                      # dataType 2
    0xD26A61AE: 'TargetOfsPosX 15',                # dataType 2
    0xD278F57F: 'Scale',                           # dataType 2
    0xD3137725: 'FlowMatControl_x',                # dataType 2
    0xD326A335: 'mAimViewParamPage',               # dataType 0
    0xD3F406E3: 'SkipTime D',                      # dataType 1
    0xD48206D4: 'ReqNo(Off) I',                    # dataType 1
    0xD48D47B0: 'mClawGimmickState',               # dataType 0
    0xD4B33B67: 'Index 08',                        # dataType 1
    0xD507A5B7: 'TargetOfsPosX 11',                # dataType 2
    0xD50C0C2C: 'OfsPosX 00',                      # dataType 2
    0xD560C677: 'TargetReqNo D',                   # dataType 0
    0xD5D93660: '敵拘束中ダメージ種類',                      # dataType 1
    0xD5E26B94: 'mParamType2',                     # dataType 0
    0xD6010C27: 'mEmitControl',                    # dataType 2
    0xD67A6D86: 'WorkNo04',                        # dataType 1
    0xD6FA0F62: 'ノードID[4]',                        # dataType 1
    0xD7FDCCA8: 'JointNo 00',                      # dataType 0
    0xD8D4462F: '3637790255',                      # dataType 0
    0xD926449A: 'JointNo 08',                      # dataType 0
    0xD970E92F: 'UIDの同期ID[0]',                     # dataType 0
    0xDA424AC8: 'SkipTime H',                      # dataType 1
    0xDA68B355: 'Index 00',                        # dataType 1
    0xDB7DD336: 'mVAnimPosScale',                  # dataType 2
    0xDBA6B6B5: 'UIDの同期ID 15',                     # dataType 0
    0xDBD7841E: 'OfsPosX 08',                      # dataType 2
    0xDBFCC5B7: 'バンク[4]',                          # dataType 1
    0xDCCB72AC: 'UIDの同期ID 11',                     # dataType 0
    0xDD05774C: 'Index 04',                        # dataType 1
    0xDD244F0E: 'mTurnAngFixed',                   # dataType 2
    0xDE190024: 'mOfsSpeed 12',                    # dataType 2
    0xDFCC21AD: 'WorkNo08',                        # dataType 1
    0xE0341C9D: 'FlowControl_w',                   # dataType 2
    0xE120BD27: 'ReqNo E',                         # dataType 1
    0xE1615104: 'OfsPosZ 07',                      # dataType 2
    0xE1C4FA93: 'ChangeLeftJntNo',                 # dataType 0
    0xE2142A34: 'MummyBlend_y',                    # dataType 2
    0xE26CE4E8: 'JointNo2',                        # dataType 0
    0xE2FD723B: 'mWaveAngle',                      # dataType 2
    0xE3171352: 'KeyFrame17',                      # dataType 4
    0xE321CB1D: 'WorkNo20',                        # dataType 1
    0xE35A3690: 'mBlendBaseMapFactor',             # dataType 3
    0xE35D6816: 'ShootOfsDegY 00',                 # dataType 2
    0xE381EFB5: 'scl:y',                           # dataType 2
    0xE3BA6337: 'ReleaseTime E',                   # dataType 1
    0xE430AC0F: 'ShootOfsDegY 04',                 # dataType 2
    0xE44C0F04: 'WorkNo24',                        # dataType 1
    0xE46C4D76: 'mOfsGravity 08',                  # dataType 2
    0xE47AD74B: 'KeyFrame13',                      # dataType 4
    0xE4CC6DE0: 'ノードID[6]',                        # dataType 1
    0xE4D7A72E: 'ReleaseTime A',                   # dataType 1
    0xE50E7B8A: 'ChangeRightAngZ',                 # dataType 2
    0xE540C280: 'mAnimEmitMin',                    # dataType 2
    0xE6073C86: 'TargetOfsPosZ 12',                # dataType 2
    0xE60C951D: 'OfsPosZ 03',                      # dataType 2
    0xE64D793E: 'ReqNo A',                         # dataType 1
    0xE85DAF8F: 'BurnControl_y',                   # dataType 2
    0xE87AFFF5: 'VPushRatio_x',                    # dataType 2
    0xE8A7D47C: 'mWpSnowShovelScaleZ',             # dataType 2
    0xEA4C3426: 'ListIndex 13',                    # dataType 1
    0xEAB7C544: 'mOfsGravity 00',                  # dataType 2
    0xEAC46630: 'TargetOfsPosY 06',                # dataType 2
    0xEB468BAD: 'UIDの同期ID[2]',                     # dataType 0
    0xEBF46AFC: 'Group 2',                         # dataType 0
    0xEC4350B5: 'Mask0',                           # dataType 2
    0xEC6BF8FC: 'UVTransformA_w',                  # dataType 2
    0xECC55CED: 'トゲ(尻尾)状態',                        # dataType 0
    0xED45D37E: 'mPartsMaskB',                     # dataType 2
    0xED86E024: 'ShootOfsDegY 08',                 # dataType 2
    0xEDA20BB2: 'OfsPosY 13',                      # dataType 2
    0xEDA9A229: 'TargetOfsPosY 02',                # dataType 2
    0xEDDA015D: 'mOfsGravity 04',                  # dataType 2
    0xEDFA432F: 'WorkNo28',                        # dataType 1
    0xEEDF77D0: 'mCamViewState',                   # dataType 0
    0xF0076E64: 'mFlag6',                          # dataType 1
    0xF09920EC: 'RimAlpha',                        # dataType 2
    0xF0D19674: 'バンク[7]',                          # dataType 1
    0xF105BBE3: 'rot:x',                           # dataType 2
    0xF134912A: 'MaskBlend_A_y',                   # dataType 2
    0xF1357D01: 'FakeLightPosition_y',             # dataType 2
    0xF15CEF67: 'mHeightCtrlDist',                 # dataType 2
    0xF19AB8A2: '氷塊を落とす高さ',                        # dataType 2
    0xF1ED59A4: 'PosRotateX 00',                   # dataType 2
    0xF257AB1C: 'ReqNo(On) F',                     # dataType 1
    0xF25DBAEC: 'UIDの同期ID[3]',                     # dataType 0
    0xF31416A8: 'BlendMatFactor_x',                # dataType 2
    0xF3570567: 'ListIndex 03',                    # dataType 1
    0xF3658026: '持ち物１のPartsNo',                    # dataType 0
    0xF3722F73: 'MaskBlend_B_y',                   # dataType 2
    0xF3D25004: 'mFlow_Speed',                     # dataType 2
    0xF3D4FEEA: 'OfsPosY 07',                      # dataType 2
    0xF3DA8324: 'mWpSnowShovelCurvePower',         # dataType 2
    0xF4031A77: 'mCamSpring',                      # dataType 2
    0xF43AC17E: 'ListIndex 07',                    # dataType 1
    0xF4B29368: 'TargetOfsPosY 12',                # dataType 2
    0xF4B93AF3: 'OfsPosY 03',                      # dataType 2
    0xF4D12F36: 'mViewParamId',                    # dataType 0
    0xF53A6F05: 'ReqNo(On) B',                     # dataType 1
    0xF73B4573: 'mLerpAlpha_BMtoEM',               # dataType 2
    0xF76AAA7D: 'mFlag2',                          # dataType 1
    0xF7C55D41: 'mSubSurfaceBlend',                # dataType 2
    0xF81DE7F9: 'mRopeHoldState',                  # dataType 0
    0xF850F54C: 'mMotionTransRateY',               # dataType 2
    0xF871C9DE: 'TargetOfsPosZ 06',                # dataType 2
    0xF9120603: 'mVAnimV',                         # dataType 2
    0xF960B74A: 'ReleaseTime Q',                   # dataType 1
    0xF97BCCAB: 'Tag 5',                           # dataType 0
    0xF98DCE90: 'pos:y',                           # dataType 2
    0xFA0C2213: 'KeyFrame07',                      # dataType 4
    0xFA223F53: 'mOpacityFactor',                  # dataType 2
    0xFA3AFA5C: 'WorkNo30',                        # dataType 1
    0xFA465957: 'ShootOfsDegY 10',                 # dataType 2
    0xFC898D7A: 'X座標',                             # dataType 2
    0xFCFE68B9: 'mHeightCtrlType',                 # dataType 0
    0xFD2B9D4E: 'ShootOfsDegY 14',                 # dataType 2
    0xFD480BC4: 'mDispFactor',                     # dataType 2
    0xFD61E60A: 'KeyFrame03',                      # dataType 4
    0xFDD75CA1: 'ノードID[7]',                        # dataType 1
    0xFE1608B2: 'Tag 1',                           # dataType 0
    0xFE294B28: 'mPartsMaskZ',                     # dataType 2
    0xFF17A45C: 'OfsPosZ 13',                      # dataType 2
    0xFF1C0DC7: 'TargetOfsPosZ 02',                # dataType 2
    0xFF5207BD: 'mVPushScale',                     # dataType 2
    0xFF5FC870: 'ChangeLeftAngY',                  # dataType 2
}

# ── DT_DATATYPE：官方 dump 给出的每条 DT 的 dataType ──────────────────────────
# 0=int(s32) / 1=uint(u32) / 2=float / 3=color[RGBA] / 4=uint(flag)。
# 有了这张表就不用再靠语料反推 dataType——此前正是因为无法标定，DTI 里 44 条
# u32/bool/vector 字段只能整批跳过不进调色板（见 DTI_EXTRA_PAIRS 注释）。
# 与 DTI 类型的对应（拿两表共有条目校准）：
#   color → 3 (n=27) · f32 → 2 (n=192) · s32 → 0 (n=95) · u32 → 1 (n=235，另 2 例落在 0)
DT_DATATYPE = {
    0x002FF505: 2,   # RotationX
    0x0074A898: 2,   # HomingRate
    0x00D534F9: 1,   # mAttackAdjustAngleIndex
    0x01080DD5: 2,   # RangeMinY
    0x01DBBF61: 2,   # Vpivot_z
    0x02571B8D: 2,   # mMetalic
    0x0297CFC9: 0,   # Group 4
    0x032F9113: 1,   # ListIndex 15
    0x036D877F: 2,   # ステージめり込み対応距離（レイ判定の長さ）
    0x03840ECE: 1,   # mDamageHitNo
    0x03A7C305: 2,   # TargetOfsPosY 00
    0x03AC6A9E: 2,   # OfsPosY 11
    0x03D46071: 2,   # mOfsGravity 06
    0x0426764B: 2,   # mPartsMaskD
    0x0442550A: 1,   # ListIndex 11
    0x04AF3E55: 4,   # KeyFrame19
    0x04B9A468: 2,   # mOfsGravity 02
    0x04C1AE87: 2,   # OfsPosY 15
    0x04CA071C: 2,   # TargetOfsPosY 04
    0x04F09E88: 1,   # mRaftPartsIndex
    0x05FA0BD0: 0,   # Group 0
    0x06A9B550: 2,   # mWpSnowShovelScaleX
    0x06D97C03: 2,   # OfsPosZ 09
    0x0718D2B3: 2,   # LocalRotationY
    0x078C319A: 2,   # FlowControl_y
    0x0802F431: 2,   # OfsPosZ 01
    0x08095DAA: 2,   # TargetOfsPosZ 10
    0x08431812: 1,   # ReqNo C
    0x08570195: 2,   # UVTransformC_y
    0x085BC9D5: 2,   # LightIntensity
    0x08FD20A6: 1,   # mFlag
    0x09956BA2: 2,   # UVTransformB_y
    0x0A3ECD23: 2,   # ShootOfsDegY 06
    0x0A74B667: 4,   # KeyFrame11
    0x0AD9C602: 1,   # ReleaseTime C
    0x0B001AA6: 2,   # ChangeRightAngX
    0x0B0F41DD: 0,   # JointNo4
    0x0BD3D5FB: 2,   # UVTransformA_y
    0x0CB6228E: 2,   # 213262990
    0x0CECC08D: 0,   # コンスト部位ID[6]
    0x0CFE3227: 2,   # WeaponEfcValue_1
    0x0D19727E: 4,   # KeyFrame15
    0x0D2FAA31: 1,   # WorkNo22
    0x0D53093A: 2,   # ShootOfsDegY 02
    0x0D7C4B37: 2,   # TargetOfsPosY 08
    0x0DB4021B: 1,   # ReleaseTime G
    0x0DDE7E84: 2,   # mFakeLightIntensity
    0x0E424A70: 2,   # mFilmBlend
    0x0EA20FED: 0,   # mSlingerShellAttach
    0x0EBAEC37: 2,   # SizeScalar
    0x0ECBFA29: 2,   # BrightThreshold
    0x0EF6ABF4: 2,   # BlurStart
    0x0F2EDC0B: 1,   # ReqNo G
    0x0F6499B3: 2,   # TargetOfsPosZ 14
    0x0F6F3028: 2,   # OfsPosZ 05
    0x0FF5554F: 3,   # mDistortionFactor
    0x10272A04: 2,   # mPartsMaskX
    0x11126CEB: 2,   # TargetOfsPosZ 00
    0x1119C570: 2,   # OfsPosZ 11
    0x11E4E7B1: 0,   # mLookAtParam
    0x11E90F5A: 0,   # mFootStep
    0x1213267E: 1,   # ReqNo(Loop) R
    0x1308E98E: 2,   # mBlendBaseMapFactorIntensity
    0x13495749: 2,   # mCamMarginEndItpTime
    0x135F03D9: 2,   # TerminatePositionY
    0x136F8726: 4,   # KeyFrame01
    0x13804BC9: 2,   # mMinIntensityRate
    0x1382EC79: 1,   # ListIndex 09
    0x1383E9BA: 2,   # RangeMaxY
    0x1402433F: 4,   # KeyFrame05
    0x1448387B: 2,   # ShootOfsDegY 12
    0x146CD3ED: 2,   # OfsPosY 09
    0x15EC38EA: 2,   # mFlow_Strength
    0x15EF861B: 1,   # ReqNo(On) H
    0x164DB124: 3,   # mFinColor
    0x16740169: 2,   # OfsPosZ 15
    0x167FA8F2: 2,   # TargetOfsPosZ 04
    0x16814F1C: 2,   # Roughness
    0x1892CC87: 2,   # 412273799
    0x18C577DE: 2,   # EmissiveColorRate
    0x18CC46B5: 3,   # mFlowColor
    0x1A34A052: 1,   # ListIndex 05
    0x1AB75BDF: 2,   # OfsPosY 01
    0x1ABCF244: 2,   # TargetOfsPosY 10
    0x1BB0EB80: 2,   # mEmissiveMapFactorIntensity
    0x1C32DCCF: 2,   # mVPushSpeed
    0x1C59CA30: 1,   # ReqNo(On) D
    0x1D1A7784: 2,   # BlendMatFactor_z
    0x1D59644B: 1,   # ListIndex 01
    0x1D7D5399: 2,   # mCameraResetAngle
    0x1D95BB54: 2,   # BlurWidth
    0x1DB40F14: 4,   # KeyFrame09
    0x1DD1365D: 2,   # TargetOfsPosY 14
    0x1DDA9FC6: 2,   # OfsPosY 05
    0x1E090F48: 1,   # mFlag4
    0x1E73EAB4: 0,   # mViewParamPage
    0x1F0BDACF: 2,   # rot:z
    0x1FC9E4D9: 2,   # TargetOfsPosZ 08
    0x20E89CC5: 0,   # JointNo 12
    0x2159E5A4: 4,   # KeyFrame21
    0x216F3DEB: 1,   # WorkNo16
    0x2212F5DA: 2,   # TargetOfsPosX 03
    0x22195C41: 2,   # OfsPosX 12
    0x223DB7D7: 2,   # ShootOfsDegX 09
    0x2285686D: 2,   # mDotOpacity
    0x2317001E: 0,   # mBankType
    0x23EDA9F5: 1,   # 痕跡の種類
    0x241CAED2: 2,   # SizeX
    0x241F9852: 2,   # KinkControl_y
    0x24579AAB: 1,   # ListIndex
    0x24C60AC4: 2,   # GameParam D
    0x252961CD: 0,   # mCameraShakeIndex
    0x254ECDBF: 2,   # mStaminaRecoveryRate
    0x256C8A54: 2,   # ChangeRightOfsZ
    0x257F31C3: 2,   # TargetOfsPosX 07
    0x2602F9F2: 1,   # WorkNo12
    0x26119EDA: 0,   # 乗りぶつけダメージ方向
    0x263421BD: 4,   # KeyFrame25
    0x26BD5CC2: 3,   # BlendFactor
    0x274D4F11: 2,   # mAttackAdjustGroundSpeedRate
    0x27C1934E: 0,   # コンスト部位ID[5]
    0x27D7D87B: 2,   # mOfsSpeed 08
    0x2822A722: 2,   # SizeYAdd
    0x2845C079: 0,   # ChangeRightType
    0x290C5049: 2,   # mOfsSpeed 00
    0x2A83ABD7: 0,   # 713272279
    0x2AA40DE9: 3,   # TailColor
    0x2AC902E3: 2,   # mMotionSpeed
    0x2B078267: 2,   # mLookAtOfsAngleY
    0x2B8BFBFC: 2,   # ShootOfsDegX 05
    0x2BD52F93: 1,   # mId
    0x2BDE22C1: 0,   # UIDの同期ID 03
    0x2BF81C73: 2,   # mCameraResetTime
    0x2C07B96F: 2,   # mPowderScaleY
    0x2C3187EA: 2,   # RotationAddZ
    0x2C426079: 0,   # mViewParamNo
    0x2CB3E6D8: 0,   # UIDの同期ID 07
    0x2CE63FE5: 2,   # ShootOfsDegX 01
    0x2CFC1613: 0,   # mCamBasePosOffsetNo
    0x2D7046EF: 2,   # GameParam H
    0x2D7DE338: 1,   # Index 12
    0x2DDB0D26: 2,   # 押当たり補間（秒）
    0x2E2C1393: 2,   # OfsPosY
    0x2E619450: 2,   # mOfsSpeed 04
    0x2E710D12: 2,   # mAlbedoBlend
    0x2EF52DB5: 0,   # mTurnBaseDir
    0x2F5A867F: 2,   # DisplaceControl_w
    0x2F826D96: 4,   # KeyFrame29
    0x2FD81AE9: 0,   # 持ち物２のMotionNo
    0x2FD8A1F4: 2,   # WaveAxis_z
    0x2FF50558: 2,   # Rotation
    0x30176108: 2,   # mOfsSpeed 10
    0x3033C6D8: 0,   # mFingerMotionType
    0x310FD720: 0,   # 特殊採取のタイプ
    0x31182E0D: 2,   # Speed
    0x316D89C5: 2,   # CoreThickness
    0x325FCB10: 3,   # mMummyColor
    0x32C51380: 0,   # UIDの同期ID 13
    0x32F86046: 0,   # イベクエ落し物関節番号
    0x330B1660: 1,   # Index 06
    0x33A4A86B: 2,   # PlaySpeedCoef
    0x33A4D9DC: 0,   # 押当たり種類
    0x344C2BE4: 1,   # SkipTime J
    0x3466D279: 1,   # Index 02
    0x35FD0EA4: 2,   # ShootOfsDegX 11
    0x371BBC04: 2,   # mRoughness
    0x37484700: 2,   # mCharmMountCamSpring
    0x3775827A: 2,   # RangeY
    0x377AA511: 2,   # mOfsSpeed 14
    0x3842D4E5: 4,   # KeyFrame31
    0x38740CAA: 1,   # WorkNo06
    0x393BCA4E: 2,   # PosRotateY 05
    0x39A1E557: 3,   # FireColor
    0x39F3AD84: 0,   # JointNo 02
    0x3A8C67F8: 1,   # ReqNo(Off) K
    0x3A9708CC: 2,   # SizeZAdd
    0x3A97A3D6: 1,   # SkipTime B
    0x3B026D00: 2,   # OfsPosX 02
    0x3B09C49B: 2,   # TargetOfsPosX 13
    0x3BA67E7C: 3,   # HeadColor
    0x3C036342: 0,   # TargetReqNo B
    0x3C6FA919: 2,   # OfsPosX 06
    0x3C81CEA1: 0,   # mParamType4
    0x3CA9C405: 2,   # mFlowDirUVPhaseShift
    0x3DFA67CF: 1,   # SkipTime F
    0x3E560E57: 2,   # PosRotateY 01
    0x3E9E699D: 0,   # JointNo 06
    0x3EDAA20F: 0,   # コンスト部位ID[4]
    0x3F19C8B3: 1,   # WorkNo02
    0x3F3D39AE: 2,   # ChangeLeftOfsY
    0x3FC9E0B8: 0,   # 1070194872
    0x4072B2EC: 2,   # RangeX
    0x407D9587: 2,   # mOfsSpeed 15
    0x416522B1: 2,   # mWeaponMotionStartFrame
    0x41E07A9B: 2,   # mOtRideMuteRotRate
    0x41E7D172: 1,   # NULL傾き固定時の回転完了するまでのフレーム数
    0x4258D1A6: 0,   # mActionPhaseSub
    0x4279F094: 2,   # mMaxIntensityRate
    0x42FA3E32: 2,   # ShootOfsDegX 10
    0x4313B645: 2,   # mOpacityPow
    0x434B1B72: 1,   # SkipTime K
    0x435F3054: 2,   # EffectiveRadius
    0x4361E2EF: 1,   # Index 03
    0x43AC6AB4: 2,   # FlowMatControl_w
    0x43AD564A: 0,   # コンスト部位ID[1]
    0x440C26F6: 1,   # Index 07
    0x444D2CFC: 2,   # mWeaponMotionSpeed
    0x45C22316: 0,   # UIDの同期ID 12
    0x4625CE84: 0,   # AnimNo A
    0x4710519E: 2,   # mOfsSpeed 11
    0x481EF825: 1,   # WorkNo03
    0x483A0938: 2,   # ChangeLeftOfsX
    0x48EF8D8E: 2,   # mTranslucency
    0x49513EC1: 2,   # PosRotateY 00
    0x4999590B: 0,   # JointNo 07
    0x4A6DD631: 0,   # LeftPartsNo[2]
    0x4AAAF206: 2,   # mToneAlpha
    0x4AFD5759: 1,   # SkipTime G
    0x4B0453D4: 0,   # TargetReqNo C
    0x4B68998F: 2,   # OfsPosX 07
    0x4BD36121: 0,   # mCameraShakePage
    0x4C055D96: 2,   # OfsPosX 03
    0x4C0EF40D: 2,   # TargetOfsPosX 12
    0x4CEB3A2E: 0,   # mParamType1
    0x4D26ED7B: 0,   # シェーダーのタイプ
    0x4D41A06B: 2,   # NormalBlendRate
    0x4D6470C6: 0,   # 対象ダメージ部位
    0x4D8B576E: 1,   # ReqNo(Off) J
    0x4D909340: 1,   # SkipTime C
    0x4DA1831A: 1,   # mOtRideAlphaFootOffsetIndex
    0x4E00491F: 2,   # IntensitySheet
    0x4E3CFAD8: 2,   # PosRotateY 04
    0x4EDC6CE0: 2,   # mAddNormalMaskD
    0x4EF49D12: 0,   # JointNo 03
    0x4F45E473: 4,   # KeyFrame30
    0x4F4B4BA4: 2,   # mWeaponMotionInterpolationTime
    0x4F733C3C: 1,   # WorkNo07
    0x5009B3C9: 0,   # 対象の乗り部位
    0x504F8D1C: 2,   # mFungusDamageRate
    0x50D0E8ED: 2,   # mOfsSpeed 09
    0x5105C964: 1,   # WorkNo13
    0x5133112B: 4,   # KeyFrame24
    0x51EBFAC2: 2,   # DisplaceControl_z
    0x526AF201: 0,   # トゲ(頭)状態
    0x52780155: 2,   # TargetOfsPosX 06
    0x5297382F: 2,   # mFlowTile
    0x52EC2264: 0,   # 持ち物２のBankNo
    0x531B9E44: 2,   # SizeY
    0x5376E770: 0,   # LeftPartsNo[3]
    0x53984EE2: 0,   # 使用する当たり
    0x53A3F45E: 2,   # MummyMatControl_w
    0x53B6808F: 2,   # mVerticalOpacityPowInv
    0x53C13A52: 2,   # GameParam E
    0x548DFF15: 0,   # SlotNo A
    0x54ACFE4B: 2,   # GameParam A
    0x5515C54C: 2,   # TargetOfsPosX 02
    0x551E6CD7: 2,   # OfsPosX 13
    0x553A8741: 2,   # ShootOfsDegX 08
    0x565ED532: 4,   # KeyFrame20
    0x56680D7D: 1,   # WorkNo17
    0x56B7B840: 2,   # mDistortionFactorIntensity
    0x570F638E: 2,   # mTransAdjustDist
    0x5782A950: 1,   # ギミックの生成確率(%)
    0x57EFAC53: 0,   # JointNo 13
    0x57F1682F: 2,   # (新)スケール指定(0～1)
    0x5846C819: 0,   # 持ち物１のMotionNo
    0x58689812: 3,   # Color
    0x58855D00: 4,   # KeyFrame28
    0x5966A4C6: 2,   # mOfsSpeed 05
    0x5A7AD3AE: 1,   # Index 13
    0x5A8C6820: 3,   # SmokeColor
    0x5AB6670B: 0,   # コンスト部位ID[0]
    0x5AFE86CE: 2,   # Y座標
    0x5B0089F9: 2,   # mPowderScaleX
    0x5BB4D64E: 0,   # UIDの同期ID 06
    0x5BE10F73: 2,   # ShootOfsDegX 00
    0x5C00B2F1: 2,   # mLookAtOfsAngleX
    0x5C5DA290: 2,   # mTurnAng
    0x5C8CCB6A: 2,   # ShootOfsDegX 04
    0x5C918AD5: 0,   # 痕跡の生成位置
    0x5CD91257: 0,   # UIDの同期ID 02
    0x5CE43CEA: 1,   # RightObjMotNo
    0x5D252E24: 0,   # 部位制御条件
    0x5E0B60DF: 2,   # mOfsSpeed 01
    0x5E6EC814: 2,   # mAngleInterpolationProgress
    0x5F8AD46D: 2,   # mPreyShotTiming
    0x5FF25223: 0,   # 持ち物４のBankNo
    0x605428A3: 2,   # mAddNormalBlend
    0x6069E6F0: 1,   # ReleaseTime R
    0x60849F2A: 2,   # pos:z
    0x608DCF8D: 3,   # EmissiveColor
    0x60D69856: 3,   # ColorSpecular
    0x614085F2: 0,   # LeftPartsNo[1]
    0x6159A4F6: 2,   # mMotionTransRateZ
    0x617331FF: 2,   # OfsPosZ 14
    0x61789864: 2,   # TargetOfsPosZ 05
    0x62CEFAB3: 0,   # 痕跡の生成向き
    0x630573A9: 4,   # KeyFrame04
    0x6324C0C4: 1,   # mEpvIndex
    0x634D2DC4: 2,   # mHeat
    0x634F08ED: 2,   # ShootOfsDegY 13
    0x636BE37B: 2,   # OfsPosY 08
    0x637622C1: 3,   # mFakeLightColor
    0x63AB0B39: 2,   # BlendMatFactor_w
    0x64264EA4: 2,   # SpeedRateV
    0x6458334F: 2,   # TerminatePositionX
    0x6459658B: 1,   # mEfcElementID
    0x6468B7B0: 4,   # KeyFrame00
    0x6484D92C: 2,   # RangeMaxX
    0x6485DCEF: 1,   # ListIndex 08
    0x648CADCE: 0,   # mCamMarginEndEaseType
    0x6600C6AB: 1,   # 首補正の計算軸一律設定
    0x66155C7D: 2,   # TargetOfsPosZ 01
    0x661EF5E6: 2,   # OfsPosZ 10
    0x665699CA: 2,   # ChangeLeftAngZ
    0x66DAFF30: 3,   # mAddColorA
    0x670FA57B: 2,   # NULL傾き固定時の回転する最大角度
    0x671F5908: 0,   # Tag 2
    0x67201A92: 2,   # mPartsMaskY
    0x67921A5E: 0,   # mWeaponMotionNo
    0x67D78EA5: 2,   # mTotalOpacity
    0x68800589: 0,   # コンスト部位ID[2]
    0x68CED44F: 2,   # TargetOfsPosZ 09
    0x690E3FDE: 1,   # mFlag5
    0x69A80037: 0,   # HitIndex
    0x69D119C8: 2,   # 1775311304
    0x69D5D953: 0,   # ブレスチャージ状態
    0x6A5E54DD: 1,   # ListIndex 00
    0x6A5FE3C4: 2,   # Gravity
    0x6AB33F82: 4,   # KeyFrame08
    0x6AD606CB: 2,   # TargetOfsPosY 15
    0x6ADDAF50: 2,   # OfsPosY 04
    0x6B169C53: 2,   # Z座標
    0x6B5EFAA6: 1,   # ReqNo(On) E
    0x6C333EBF: 1,   # ReqNo(On) A
    0x6CC0B887: 2,   # シェーダーのかけ具合(0.0~1.0)
    0x6D3390C4: 1,   # ListIndex 04
    0x6DB06B49: 2,   # OfsPosY 00
    0x6DBBC2D2: 2,   # TargetOfsPosY 11
    0x6DD60D01: 0,   # mMotionCameraCutNo
    0x6E63FBC7: 1,   # mFlag1
    0x6F89CC07: 2,   # PosRotateX 07
    0x708B010C: 2,   # FlowControl_x
    0x70D64E7E: 0,   # コリジョンユニークID
    0x7154FE35: 2,   # BurnControl_z
    0x719B34C8: 0,   # コンスト部位ID[3]
    0x71AE85C6: 2,   # mWpSnowShovelScaleY
    0x71BCF0AA: 2,   # mFlowSpeed
    0x71DE4C95: 2,   # OfsPosZ 08
    0x72A97996: 2,   # mDetailA_ColorIntensity
    0x72FD3B46: 0,   # Group 1
    0x7345659C: 1,   # ListIndex 10
    0x736F4F29: 2,   # 首補正のブレンド率
    0x73A80EC3: 4,   # KeyFrame18
    0x73BE94FE: 2,   # mOfsGravity 03
    0x73C69E11: 2,   # OfsPosY 14
    0x73CD378A: 2,   # TargetOfsPosY 05
    0x7428A185: 1,   # ListIndex 14
    0x744C82C4: 2,   # mPartsMaskA
    0x74A0F393: 2,   # TargetOfsPosY 01
    0x74AB5A08: 2,   # OfsPosY 10
    0x74D350E7: 2,   # mOfsGravity 07
    0x7590FF5F: 0,   # Group 5
    0x75B22732: 2,   # mPosInterpolationProgress
    0x760F3D43: 2,   # RangeMinX
    0x7728C593: 2,   # RotationY
    0x7747F0BC: 2,   # mHyperArmorTime
    0x77E68312: 1,   # 首補正のブレス関節
    0x7829EC9D: 1,   # ReqNo F
    0x785BB4B3: 0,   # LeftPartsNo[0]
    0x7863A925: 2,   # TargetOfsPosZ 15
    0x786800BE: 2,   # OfsPosZ 04
    0x787E9FD5: 0,   # mCameraPhase
    0x7A1E42E8: 4,   # KeyFrame14
    0x7A289AA7: 1,   # WorkNo23
    0x7A5439AC: 2,   # ShootOfsDegY 03
    0x7A7B7BA1: 2,   # TargetOfsPosY 09
    0x7A88BE0F: 2,   # scl:z
    0x7AB3328D: 1,   # ReleaseTime F
    0x7B65B552: 0,   # JointNo1
    0x7BE08FA2: 0,   # エラ状態
    0x7BF902B1: 2,   # WeaponEfcValue_0
    0x7C072A30: 2,   # ChangeRightAngY
    0x7C692062: 2,   # mAlphaTestControl
    0x7CD4E56D: 2,   # UVTransformA_x
    0x7D235C30: 2,   # EmissiveMapFactorIntensity
    0x7D39FDB5: 2,   # ShootOfsDegY 07
    0x7D6D165D: 0,   # 部位に対する処理
    0x7D7386F1: 4,   # KeyFrame10
    0x7DDEF694: 1,   # ReleaseTime B
    0x7F05C4A7: 2,   # OfsPosZ 00
    0x7F0E6D3C: 2,   # TargetOfsPosZ 11
    0x7F442884: 1,   # ReqNo B
    0x7F503103: 2,   # UVTransformC_x
    0x806D9AEB: 1,   # mFlag3
    0x80983795: 2,   # mPartsMaskW
    0x80A0A8E4: 1,   # ノードID[2]
    0x814568E2: 0,   # ギミックの生成位置
    0x819DDA91: 0,   # RightPartsNo[1]
    0x823D5F93: 1,   # ReqNo(On) C
    0x825101E5: 2,   # mEmbankmentScale
    0x831B390B: 2,   # AlphaCorrectionMax
    0x833DF1E8: 1,   # ListIndex 06
    0x83B5A3FE: 2,   # TargetOfsPosY 13
    0x83BE0A65: 2,   # OfsPosY 02
    0x840FFF51: 2,   # mVerticalOpacityPow
    0x8413263E: 2,   # BlendMatFactor_y
    0x845035F1: 1,   # ListIndex 02
    0x84745897: 0,   # mActionPhase
    0x84751FE5: 2,   # MaskBlend_B_x
    0x84D3CE7C: 2,   # OfsPosY 06
    0x85509B8A: 1,   # ReqNo(On) G
    0x859AB9C4: 0,   # mLinkMotionPhase
    0x85A14934: 1,   # mStaminaParamIndex
    0x86028B75: 2,   # rot:y
    0x8633A1BC: 2,   # MaskBlend_A_x
    0x86859DBE: 0,   # mNoSetWeaponMotionNo
    0x86C1A86E: 2,   # mCameraSmoothTime
    0x87A41BA7: 1,   # LeftObjMotNo
    0x881094CA: 2,   # OfsPosZ 12
    0x881B3D51: 2,   # TargetOfsPosZ 03
    0x8858F8E6: 2,   # ChangeLeftAngX
    0x890766C1: 2,   # 突進シェルの回転速度
    0x89113824: 0,   # Tag 0
    0x89582088: 0,   # PartsNo[2]
    0x8A2CADD8: 2,   # ShootOfsDegY 15
    0x8A3D617C: 1,   # 斜面補正フレーム
    0x8A565263: 2,   # TerminatePositionZ
    0x8A66D69C: 4,   # KeyFrame02
    0x8A8AB800: 2,   # RangeMaxZ
    0x8B1A77C4: 1,   # ReqNo(Loop) Q
    0x8BF31826: 2,   # RimPower
    0x8D0B1285: 4,   # KeyFrame06
    0x8D3DCACA: 1,   # WorkNo31
    0x8D4169C1: 2,   # ShootOfsDegY 11
    0x8D65C9C7: 0,   # MatID A
    0x8DA66231: 1,   # バンク[2]
    0x8E7CFC3D: 0,   # Tag 4
    0x8E8AFE06: 2,   # pos:x
    0x8F1479C1: 2,   # mToneEdge
    0x8F198226: 2,   # mBaseMapFactorIntensity
    0x8F2A4EA9: 0,   # UIDの同期ID[6]
    0x8F57C5DA: 2,   # mMotionTransRateX
    0x8F76F948: 2,   # TargetOfsPosZ 07
    0x8F79C6AF: 1,   # mEfcIndexID
    0x909EC047: 2,   # SizeXAdd
    0x91000C10: 2,   # TargetOfsPosZ 13
    0x910BA58B: 2,   # OfsPosZ 02
    0x915E502F: 2,   # UVTransformC_z
    0x91B8DFC5: 2,   # 斜面補正最大角度
    0x91BA4212: 2,   # mNextFootStepFrame
    0x92DA8441: 2,   # UVTransformA_z
    0x93315130: 2,   # mFlowDirFlowSpeed
    0x93379C99: 2,   # ShootOfsDegY 05
    0x934B3F92: 1,   # WorkNo25
    0x937DE7DD: 4,   # KeyFrame12
    0x93F18F03: 2,   # mDetailEmitIntensity
    0x941023C4: 4,   # KeyFrame16
    0x9426FB8B: 1,   # WorkNo21
    0x94526271: 0,   # 拘束リリースタイプ
    0x945A5880: 2,   # ShootOfsDegY 01
    0x9486DF23: 2,   # scl:x
    0x94BCC5CE: 2,   # Intensity
    0x94BD5370: 1,   # バンク[3]
    0x94BD53A1: 1,   # ReleaseTime D
    0x952B4933: 2,   # mDisplacementFactor
    0x956BD47E: 0,   # JointNo3
    0x95A3A1D3: 2,   # Blend
    0x96278DB1: 1,   # ReqNo D
    0x96317FE8: 0,   # UIDの同期ID[7]
    0x965A5CA7: 1,   # mActionState
    0x96666192: 2,   # OfsPosZ 06
    0x9748E46B: 2,   # mMotionCameraMoveAngle
    0x97876D34: 0,   # ヒレ状態
    0x979BE3E1: 0,   # ChangeRightJntNo
    0x98015C6F: 2,   # RangeMinZ
    0x987DC45A: 2,   # mFakeLightColorIntensity
    0x9886EBD0: 0,   # RightPartsNo[0]
    0x98D2EEDB: 2,   # Vpivot_y
    0x99897C43: 1,   # mTrigger
    0x99BB99A5: 1,   # ノードID[3]
    0x9A42E3E8: 2,   # mPartsMaskC
    0x9A690877: 2,   # mMummyColorIntensity
    0x9A81D0B2: 2,   # ShootOfsDegY 09
    0x9AA53B24: 2,   # OfsPosY 12
    0x9AAE92BF: 2,   # TargetOfsPosY 03
    0x9ADD31CB: 2,   # mOfsGravity 05
    0x9AFD73B9: 1,   # WorkNo29
    0x9B446023: 2,   # Mask1
    0x9B50DD3B: 1,   # 痕跡の生成確率(%)
    0x9BD476A9: 0,   # mRidingState
    0x9BE2D228: 3,   # mColor
    0x9C483C6C: 0,   # AngleY1
    0x9D0B1F8A: 1,   # ReleaseTime H
    0x9D4B04B0: 1,   # ListIndex 12
    0x9D58B92F: 2,   # mNoHitTime
    0x9DB0F5D2: 2,   # mOfsGravity 01
    0x9DC356A6: 2,   # TargetOfsPosY 07
    0x9DE5F095: 2,   # 2649092245
    0x9E118309: 2,   # LocalRotationZ
    0x9E2973C7: 2,   # SpeedRateH
    0x9E856020: 2,   # FlowControl_z
    0x9F1E012E: 2,   # ColorRate
    0x9F326571: 2,   # mAngleFade
    0x9F5A9F19: 2,   # BurnControl_x
    0x9F7B1E77: 2,   # mGimmickWaitCamMoveAngle
    0x9F91C19A: 1,   # ReqNo H
    0xA01B7821: 3,   # mEmissiveMapFactor
    0xA0FAFC3E: 0,   # JointNo 01
    0xA17D5D10: 1,   # WorkNo05
    0xA19FD528: 2,   # mInnerOffsetScale
    0xA1ED0144: 3,   # mDetailA_Color
    0xA2009521: 2,   # TargetOfsPosX 10
    0xA20B3CBA: 2,   # OfsPosX 01
    0xA23E2927: 2,   # mDetailDisplacement
    0xA26628EB: 0,   # mWeaponGaugeIndex
    0xA267F6E1: 0,   # TargetReqNo E
    0xA275734B: 0,   # PartsNo[1]
    0xA2E55B02: 0,   # mParamType3
    0xA31F6F44: 2,   # 2736746308
    0xA39EF26C: 1,   # SkipTime A
    0xA3B40BF1: 1,   # Index 09
    0xA4071D6A: 0,   # UIDの同期ID[5]
    0xA48C272D: 0,   # mRideReduceStaminaLv
    0xA4C789DC: 3,   # mFinColorB
    0xA4F33675: 1,   # SkipTime E
    0xA50A32F8: 0,   # TargetReqNo A
    0xA566F8A3: 2,   # OfsPosX 05
    0xA56D5138: 2,   # TargetOfsPosX 14
    0xA6109909: 1,   # WorkNo01
    0xA6346814: 2,   # ChangeLeftOfsZ
    0xA68B31F2: 1,   # バンク[1]
    0xA75F5FED: 2,   # PosRotateY 02
    0xA7973827: 0,   # JointNo 05
    0xA7EDA21C: 2,   # WaterLerpGtoB
    0xA8CB113B: 1,   # WorkNo09
    0xA91E30B2: 2,   # mOfsSpeed 13
    0xAA0247DA: 1,   # Index 05
    0xAAB08952: 0,   # RightPartsNo[2]
    0xAB1D2239: 1,   # ChangeLeftType
    0xAB3C8B55: 0,   # WeaponEfcType_0
    0xAB8DFB27: 1,   # ノードID[1]
    0xAB9D6334: 2,   # ColorIntensity
    0xABCC423A: 0,   # UIDの同期ID 10
    0xAC635CA9: 2,   # RimWidth
    0xACA18623: 0,   # UIDの同期ID 14
    0xACD0B488: 2,   # OfsPosX 09
    0xACF45F1E: 2,   # ShootOfsDegX 12
    0xAD457A5E: 1,   # SkipTime I
    0xAD6F83C3: 1,   # Index 01
    0xADC3C03B: 2,   # mOtRideBustUseItemBlendRate
    0xADD4B2B7: 2,   # mVPushWave
    0xAE21740C: 0,   # JointNo 09
    0xAE7CD3C0: 2,   # RangeZ
    0xAEDA6B35: 0,   # mMoveBankType
    0xAFE95AC0: 3,   # mBaseMapFactor
    0xB00501F3: 2,   # mOfsSpeed 03
    0xB0BBDAC4: 0,   # mEfcJointNo
    0xB1D0207A: 1,   # WorkNo19
    0xB282AA46: 2,   # ShootOfsDegX 06
    0xB28DA0BE: 2,   # 部位制御値（最大値の割合）
    0xB296CA66: 1,   # ノードID[0]
    0xB2AE1E00: 2,   # mTurnSpeed
    0xB2D7737B: 0,   # UIDの同期ID 00
    0xB319769B: 1,   # Index 15
    0xB3371B60: 0,   # mAimViewParamId
    0xB359AC53: 0,   # 氷塊シェルNo
    0xB3ABB813: 0,   # RightPartsNo[3]
    0xB458B56D: 2,   # mCamMarginDist
    0xB474B282: 1,   # Index 11
    0xB50EE8D5: 2,   # mPowderScaleZ
    0xB52636D6: 2,   # mIntensity
    0xB538D650: 2,   # RotationAddY
    0xB5BAB762: 0,   # UIDの同期ID 04
    0xB5C02C52: 2,   # TargetOfsPosX 08
    0xB5EF6E5F: 2,   # ShootOfsDegX 02
    0xB6C0BDF8: 2,   # CoreIntensity
    0xB7254229: 2,   # OfsPosZ
    0xB768C5EA: 2,   # mOfsSpeed 07
    0xB821F4A8: 2,   # mTransAdjustEndDist
    0xB83AF0BE: 2,   # mFinColorBIntensity
    0xB83FEC3D: 2,   # mDisableIKInterpolationFrame
    0xB850B41E: 4,   # KeyFrame22
    0xB858DE36: 2,   # mFinColorIntensity
    0xB8666C51: 1,   # WorkNo15
    0xB8895311: 2,   # mVPushBlend
    0xB8BFBF9E: 2,   # mUV_Blend
    0xB9DB9967: 0,   # 持ち物１のBankNo
    0xB9E1CD7F: 0,   # JointNo 11
    0xBA3F398C: 2,   # mLookAtSpeedRate
    0xBAA29F67: 2,   # GameParam C
    0xBB100DFB: 2,   # OfsPosX 11
    0xBB1BA460: 2,   # TargetOfsPosX 00
    0xBB6E420A: 0,   # PartsNo[0]
    0xBBB2C830: 2,   # 乗りぶつけ蓄積値
    0xBC0CFB49: 0,   # UIDの同期ID 08
    0xBC65DBEE: 2,   # ChangeRightOfsY
    0xBC766079: 2,   # TargetOfsPosX 04
    0xBC7DC9E2: 2,   # OfsPosX 15
    0xBCFFC41B: 0,   # TargetUnitNo
    0xBD16C9E8: 2,   # KinkControl_z
    0xBD1C2C2B: 0,   # UIDの同期ID[4]
    0xBDCF5B7E: 2,   # GameParam G
    0xBE4DB7FA: 1,   # Index
    0xBE8C0966: 0,   # JointNo 15
    0xBF0BA848: 1,   # WorkNo11
    0xBF160652: 2,   # AlphaCorrectionMin
    0xBF8F9DFD: 0,   # ギミックの種類
    0xBF9000B3: 1,   # バンク[0]
    0xBFD77429: 2,   # ステージめり込み対応の移動速度（1秒/距離）
    0xBFE59BEE: 2,   # DisplaceControl_x
    0xBFE81475: 3,   # mSaturationColor
    0xC06BD86E: 0,   # UIDの同期ID[1]
    0xC06FF57C: 2,   # mOfsSpeed 06
    0xC0E5BF09: 0,   # 持ち物４のMotionNo
    0xC1D6C0D8: 2,   # WaveAxis_x
    0xC207B8B4: 2,   # mFlowStrength
    0xC216C23D: 3,   # ColorRange
    0xC23FE6C6: 2,   # RotationAddX
    0xC24DF97C: 2,   # SizeScalarAdd
    0xC29F20E0: 2,   # WeaponExternValue
    0xC2BD87F4: 0,   # UIDの同期ID 05
    0xC2C71CC4: 2,   # TargetOfsPosX 09
    0xC2E7F4F6: 1,   # バンク[5]
    0xC2E85EC9: 2,   # ShootOfsDegX 03
    0xC2F1EB55: 2,   # mVolumeBlend
    0xC31CE9CF: 2,   # MummyMatControl_x
    0xC32F9493: 2,   # Radius
    0xC3738214: 1,   # Index 10
    0xC39F54D7: 2,   # mBaseColorSaturation
    0xC41E460D: 1,   # Index 14
    0xC4774EC6: 2,   # mSuperArmorTime
    0xC56404BB: 2,   # mEmitBlend
    0xC56C2A11: 2,   # 高い壁ジャンプ(15m以上)時のY方向のスピード倍率(0.0~1.0)
    0xC580048C: 2,   # PosRotateZ 07
    0xC5859AD0: 2,   # ShootOfsDegX 07
    0xC5D043ED: 0,   # UIDの同期ID 01
    0xC6785402: 2,   # 3329774594
    0xC6D710EC: 1,   # WorkNo18
    0xC7023165: 2,   # mOfsSpeed 02
    0xC80C98DE: 1,   # WorkNo10
    0xC865FF9E: 2,   # mTurnSpeed2
    0xC8D913CA: 2,   # mFilmBlendB
    0xC98B39F0: 0,   # JointNo 14
    0xC9C0F038: 2,   # mFilmThickness
    0xCA12CFFE: 2,   # SizeZ
    0xCB0BCBDF: 0,   # UIDの同期ID 09
    0xCB62EB78: 2,   # ChangeRightOfsX
    0xCB7150EF: 2,   # TargetOfsPosX 05
    0xCB7AF974: 2,   # OfsPosX 14
    0xCBAF549E: 0,   # 鉱石変更数
    0xCBDB6622: 3,   # EmissiveMapFactorColor
    0xCC173D6D: 2,   # OfsPosX 10
    0xCC1C94F6: 2,   # TargetOfsPosX 01
    0xCD9E0D40: 0,   # 3449687360
    0xCDA5AFF1: 2,   # GameParam B
    0xCEE6FDE9: 0,   # JointNo 10
    0xCF578488: 4,   # KeyFrame23
    0xCF615CC7: 1,   # WorkNo14
    0xCFE13E23: 1,   # ノードID[5]
    0xD0586F7B: 2,   # PosRotateY 03
    0xD09008B1: 0,   # JointNo 04
    0xD0B8F943: 2,   # mAddNormalMaskC
    0xD1053969: 0,   # mViewParamIdCamColAdj
    0xD117A99F: 1,   # WorkNo00
    0xD245B590: 2,   # mParallaxFactor
    0xD25D5A7B: 2,   # 首補正の高さオフセット
    0xD261C835: 2,   # OfsPosX 04
    0xD26A61AE: 2,   # TargetOfsPosX 15
    0xD278F57F: 2,   # Scale
    0xD3137725: 2,   # FlowMatControl_x
    0xD326A335: 0,   # mAimViewParamPage
    0xD3F406E3: 1,   # SkipTime D
    0xD48206D4: 1,   # ReqNo(Off) I
    0xD48D47B0: 0,   # mClawGimmickState
    0xD4B33B67: 1,   # Index 08
    0xD507A5B7: 2,   # TargetOfsPosX 11
    0xD50C0C2C: 2,   # OfsPosX 00
    0xD560C677: 0,   # TargetReqNo D
    0xD5D93660: 1,   # 敵拘束中ダメージ種類
    0xD5E26B94: 0,   # mParamType2
    0xD6010C27: 2,   # mEmitControl
    0xD67A6D86: 1,   # WorkNo04
    0xD6FA0F62: 1,   # ノードID[4]
    0xD7FDCCA8: 0,   # JointNo 00
    0xD8D4462F: 0,   # 3637790255
    0xD926449A: 0,   # JointNo 08
    0xD970E92F: 0,   # UIDの同期ID[0]
    0xDA424AC8: 1,   # SkipTime H
    0xDA68B355: 1,   # Index 00
    0xDB7DD336: 2,   # mVAnimPosScale
    0xDBA6B6B5: 0,   # UIDの同期ID 15
    0xDBD7841E: 2,   # OfsPosX 08
    0xDBFCC5B7: 1,   # バンク[4]
    0xDCCB72AC: 0,   # UIDの同期ID 11
    0xDD05774C: 1,   # Index 04
    0xDD244F0E: 2,   # mTurnAngFixed
    0xDE190024: 2,   # mOfsSpeed 12
    0xDFCC21AD: 1,   # WorkNo08
    0xE0341C9D: 2,   # FlowControl_w
    0xE120BD27: 1,   # ReqNo E
    0xE1615104: 2,   # OfsPosZ 07
    0xE1C4FA93: 0,   # ChangeLeftJntNo
    0xE2142A34: 2,   # MummyBlend_y
    0xE26CE4E8: 0,   # JointNo2
    0xE2FD723B: 2,   # mWaveAngle
    0xE3171352: 4,   # KeyFrame17
    0xE321CB1D: 1,   # WorkNo20
    0xE35A3690: 3,   # mBlendBaseMapFactor
    0xE35D6816: 2,   # ShootOfsDegY 00
    0xE381EFB5: 2,   # scl:y
    0xE3BA6337: 1,   # ReleaseTime E
    0xE430AC0F: 2,   # ShootOfsDegY 04
    0xE44C0F04: 1,   # WorkNo24
    0xE46C4D76: 2,   # mOfsGravity 08
    0xE47AD74B: 4,   # KeyFrame13
    0xE4CC6DE0: 1,   # ノードID[6]
    0xE4D7A72E: 1,   # ReleaseTime A
    0xE50E7B8A: 2,   # ChangeRightAngZ
    0xE540C280: 2,   # mAnimEmitMin
    0xE5C92264: 2,   # PlaySpeed
    0xE6073C86: 2,   # TargetOfsPosZ 12
    0xE60C951D: 2,   # OfsPosZ 03
    0xE64D793E: 1,   # ReqNo A
    0xE81961E4: 2,   # RotationAdd
    0xE85DAF8F: 2,   # BurnControl_y
    0xE87AFFF5: 2,   # VPushRatio_x
    0xE8A7D47C: 2,   # mWpSnowShovelScaleZ
    0xEA4C3426: 1,   # ListIndex 13
    0xEAB7C544: 2,   # mOfsGravity 00
    0xEAC46630: 2,   # TargetOfsPosY 06
    0xEB468BAD: 0,   # UIDの同期ID[2]
    0xEBF46AFC: 0,   # Group 2
    0xEC4350B5: 2,   # Mask0
    0xEC6BF8FC: 2,   # UVTransformA_w
    0xECC55CED: 0,   # トゲ(尻尾)状態
    0xED45D37E: 2,   # mPartsMaskB
    0xED86E024: 2,   # ShootOfsDegY 08
    0xEDA20BB2: 2,   # OfsPosY 13
    0xEDA9A229: 2,   # TargetOfsPosY 02
    0xEDDA015D: 2,   # mOfsGravity 04
    0xEDFA432F: 1,   # WorkNo28
    0xEE219429: 2,   # RotationZ
    0xEEDF77D0: 0,   # mCamViewState
    0xF0076E64: 1,   # mFlag6
    0xF09920EC: 2,   # RimAlpha
    0xF0D19674: 1,   # バンク[7]
    0xF0DF339B: 2,   # WidthSize
    0xF105BBE3: 2,   # rot:x
    0xF134912A: 2,   # MaskBlend_A_y
    0xF1357D01: 2,   # FakeLightPosition_y
    0xF15CEF67: 2,   # mHeightCtrlDist
    0xF19AB8A2: 2,   # 氷塊を落とす高さ
    0xF1ED59A4: 2,   # PosRotateX 00
    0xF257AB1C: 1,   # ReqNo(On) F
    0xF25DBAEC: 0,   # UIDの同期ID[3]
    0xF31416A8: 2,   # BlendMatFactor_x
    0xF3570567: 1,   # ListIndex 03
    0xF3658026: 0,   # 持ち物１のPartsNo
    0xF3722F73: 2,   # MaskBlend_B_y
    0xF3D25004: 2,   # mFlow_Speed
    0xF3D4FEEA: 2,   # OfsPosY 07
    0xF3DA8324: 2,   # mWpSnowShovelCurvePower
    0xF4031A77: 2,   # mCamSpring
    0xF43AC17E: 1,   # ListIndex 07
    0xF4B29368: 2,   # TargetOfsPosY 12
    0xF4B93AF3: 2,   # OfsPosY 03
    0xF4D12F36: 0,   # mViewParamId
    0xF53A6F05: 1,   # ReqNo(On) B
    0xF73B4573: 2,   # mLerpAlpha_BMtoEM
    0xF76AAA7D: 1,   # mFlag2
    0xF7C55D41: 2,   # mSubSurfaceBlend
    0xF81DE7F9: 0,   # mRopeHoldState
    0xF850F54C: 2,   # mMotionTransRateY
    0xF871C9DE: 2,   # TargetOfsPosZ 06
    0xF9120603: 2,   # mVAnimV
    0xF92E647B: 2,   # Length
    0xF960B74A: 1,   # ReleaseTime Q
    0xF97BCCAB: 0,   # Tag 5
    0xF98DCE90: 2,   # pos:y
    0xFA0C2213: 4,   # KeyFrame07
    0xFA223F53: 2,   # mOpacityFactor
    0xFA3AFA5C: 1,   # WorkNo30
    0xFA465957: 2,   # ShootOfsDegY 10
    0xFA79B1CD: 3,   # Emissive
    0xFC898D7A: 2,   # X座標
    0xFCFE68B9: 0,   # mHeightCtrlType
    0xFD2B9D4E: 2,   # ShootOfsDegY 14
    0xFD480BC4: 2,   # mDispFactor
    0xFD61E60A: 4,   # KeyFrame03
    0xFDD75CA1: 1,   # ノードID[7]
    0xFE1608B2: 0,   # Tag 1
    0xFE294B28: 2,   # mPartsMaskZ
    0xFF17A45C: 2,   # OfsPosZ 13
    0xFF1C0DC7: 2,   # TargetOfsPosZ 02
    0xFF5207BD: 2,   # mVPushScale
    0xFF5FC870: 2,   # ChangeLeftAngY
}

# transform 九条 hash（jamcrc）。MHW Y-up → Blender Z-up：游戏 Y↔Z 轴**置换**
# （game Y→blender Z[index2]、game Z→blender Y[index1]），位置/旋转适用、缩放不置换。
# 这里直接存**置换后的 blender array_index** + kind（loc/rot/scl，决定单位/符号换算）。
# 元组：(label, bl_prop, bl_index, kind)
DT_TRANSFORM = {
    0x8E8AFE06: ("pos:X", "location", 0, "loc"),
    0xF98DCE90: ("pos:Y", "location", 2, "loc"),   # game Y → blender Z
    0x60849F2A: ("pos:Z", "location", 1, "loc"),   # game Z → blender Y
    0xF105BBE3: ("rot:X", "rotation_euler", 0, "rot"),
    0x86028B75: ("rot:Y", "rotation_euler", 2, "rot"),
    0x1F0BDACF: ("rot:Z", "rotation_euler", 1, "rot"),
    0x9486DF23: ("scl:X", "scale", 0, "scl"),
    0xE381EFB5: ("scl:Y", "scale", 1, "scl"),
    0x7A88BE0F: ("scl:Z", "scale", 2, "scl"),
}

# ── DT_NEUTRAL：新增 TIML 轨道的首帧兜底值 ────────────────────────────────────────
# 新增一条轨道时首帧应当是**中性值**——加轨道本身不改变特效当下的外观，之后由用户
# 编关键帧。dataclass 式的全 0 默认在乘算类属性上是错的：亮度/强度/缩放系数填 0
# 等于让特效直接消失，根本不能作为编辑起点。
#
# ⚠ 不要用"语料里这个 DT 的首帧值分布"来定这张表——会被 TIML 轨道做动画的字段，
#   恰恰是那些要动的字段，其首帧是别人动画的起点而非中性值（SizeY 语料首帧最常见
#   130.0 就是这么来的）。选择偏差，测的是错的总体。
#
# 优先级：add_transform 的 seed 参数（调用方从该属性当前静态字段值取，见
#   blender_efx/timl_tracks.py::_field_seed_values）> 本表 > 0.0。
# Color（data_type==3）不走本表，恒 255,255,255,255（白色不透明）。
DT_NEUTRAL = {
    # ── 纯乘算系数 / 比率：1.0 就是语义上的恒等值 ──────────────────────────
    0x9F1E012E: 1.0,   # ColorRate
    0x18C577DE: 1.0,   # EmissiveColorRate
    0x94BCC5CE: 1.0,   # Intensity
    0x7D235C30: 1.0,   # EmissiveMapFactorIntensity
    0x1BB0EB80: 1.0,   # mEmissiveMapFactorIntensity
    0xAB9D6334: 1.0,   # ColorIntensity
    0xB6C0BDF8: 1.0,   # CoreIntensity
    0x085BC9D5: 1.0,   # LightIntensity
    0x4E00491F: 1.0,   # IntensitySheet
    0x4279F094: 1.0,   # mMaxIntensityRate
    0x13804BC9: 1.0,   # mMinIntensityRate
    0x4D41A06B: 1.0,   # NormalBlendRate
    0x0EBAEC37: 1.0,   # SizeScalar
    0x9486DF23: 1.0,   # scl:X
    0xE381EFB5: 1.0,   # scl:Y
    0x7A88BE0F: 1.0,   # scl:Z
    0xE5C92264: 1.0,   # PlaySpeed
    0x33A4A86B: 1.0,   # PlaySpeedCoef
    0x831B390B: 1.0,   # AlphaCorrectionMax（配对的 Min 中性值是 0.0，不入表）
    # ── 绝对量级（游戏单位下的实际尺寸/半径/速度）───────────────────────────
    # 这些字段没有"恒等值"——真实特效里 SizeY≈130、Radius≈350、EffectiveRadius≈1000，
    # 1.0 只是"1 个单位"，视觉上几乎看不见。正常路径由 seed 从属性当前字段值取到
    # 正确量级；这里给 1.0 仅作调色板按钮（只给 raw hash、拿不到字段）的兜底，
    # 取非 0 是为了不让特效直接消失。
    0x241CAED2: 1.0,   # SizeX
    0x531B9E44: 1.0,   # SizeY
    0xCA12CFFE: 1.0,   # SizeZ
    0xF0DF339B: 1.0,   # WidthSize
    0xF92E647B: 1.0,   # Length
    0x316D89C5: 1.0,   # CoreThickness
    0xC32F9493: 1.0,   # Radius
    0x435F3054: 1.0,   # EffectiveRadius
    # 未列出的一律 0.0：加法偏移（*Add）、位置（pos:*）、角度（rot:*/Rotation*）、
    # Gravity/Speed/Blur*/AlphaCorrectionMin/Roughness 等，以及 45 条未具名 hex DT
    # ——没有语义依据，猜不如不猜。
}


def dt_neutral_value(h: int) -> float:
    """新增轨道首帧的兜底中性值（无 seed 时用）。未收录的 DT → 0.0。"""
    return DT_NEUTRAL.get(h & 0xFFFFFFFF, 0.0)


# game↔Blender 数值换算（MHW Y-up↔Blender Z-up 标准换算，互为精确逆）。
# AXIS_SIGN 按 blender 轴：blender Y(index1) 取负（game Z → blender -Y）。
_AXIS_SIGN = (1.0, -1.0, 1.0)
_LOC_UNIT = 100.0   # 游戏单位(cm) / 米


def game_to_blender(kind: str, bl_index: int, v: float) -> float:
    if kind == "loc":
        return v * _AXIS_SIGN[bl_index] / _LOC_UNIT
    if kind == "rot":
        return math.radians(v * _AXIS_SIGN[bl_index])
    return v   # scl 原样


def blender_to_game(kind: str, bl_index: int, v: float) -> float:
    if kind == "loc":
        return v * _AXIS_SIGN[bl_index] * _LOC_UNIT
    if kind == "rot":
        return math.degrees(v) * _AXIS_SIGN[bl_index]
    return v


# ── PTBEHAVIOR：TLP / DT 运行时计算（不需要静态映射表）────────────────────────────
# 语料实证（2026-08）：MhEffectDecalBehavior / MhPointLightBehavior /
# MhSpotLightBehavior / PointLightBehavior / RadialBlurFilterBehavior 这五个 TLP
# 与 PTBEHAVIOR 100% 共现（lift 15.5x）——它们不是独立的属性块，而是 PTBEHAVIOR
# 的**类型变体**：PTBEHAVIOR 的 b_type 字段直接存 DTI 类名字符串
# （"nEffect::PointLightBehavior" 或裸 "MhPointLightBehavior"）。
#
# 两条哈希规则都已在语料上验证，因此这一整类不需要枚举映射表：
#   TLP = jamcrc("nEffect::nTimelineParam::<b_type 短名>") & 0x7FFFFFFF
#   DT  = jamcrc(<参数名去掉前导 m>)   —— PTBEHAVIOR 的参数 key 是 jamcrc("mFoo")，
#         而 TIML 的 datatypeHash 是 jamcrc("Foo")，同一命名空间、差一个 m 前缀。
#         18/18 抽样命中（mIntensity→Intensity、mEmissiveMapFactorIntensity→…等）。

def _short_class(b_type: str) -> str:
    """b_type 字符串 → DTI 短类名（去掉 nEffect:: 之类的命名空间前缀和尾部 NUL）。"""
    s = (b_type or "").split("\x00")[0].strip()
    return s.split("::")[-1]


def ptbehavior_tlp_candidates(b_type: str) -> list:
    """b_type → 候选 TLP hash 列表（按优先级）。

    首选自身类名推出的 TLP；若是 Mh* 子类，再追加去掉 Mh 的基类 TLP —— 实测
    b_type=MhPointLightBehavior 的 PTBEHAVIOR 会二选一地把轨道挂在
    MhPointLightBehavior(327 entry) 或基类 PointLightBehavior(222 entry) 下，
    从不并存。调用方应优先复用该 TIML 里已存在的那个，避免同一属性被拆到两个 TLP。
    """
    short = _short_class(b_type)
    if not short:
        return []
    out = [jamcrc(("nEffect::nTimelineParam::" + short).encode()) & 0x7FFFFFFF]
    if short.startswith("Mh") and len(short) > 2:
        base = short[2:]
        h = jamcrc(("nEffect::nTimelineParam::" + base).encode()) & 0x7FFFFFFF
        if h not in out:
            out.append(h)
    return out


def ptbehavior_param_channels(b_type: str, param_name: str, n_components: int = 1):
    """PTBEHAVIOR 的某个参数 → (tlp_hash, [(dt_hash, data_type), ...])；不可动画则 None。

    判据不再是「名字算得出 DT 就给按钮」，而是**算出来的 DT 必须在该 TLP 的调色板里**
    （DT_PALETTE = 语料实测 ∪ DTI 补充 ∪ 官方 dump）。这样标量、颜色、向量都能统一处理，
    也不会给一个引擎压根不认的哈希开按钮。

    向量参数（vector2/3/4）在 TIML 侧是**逐分量各一条轨道**：mUVRange → UVRangeX/Y/Z/W、
    mCenter → CenterX/Y/Z/W。所以整名查不到时，再按 X/Y/Z/W 后缀查一遍，
    全部命中才算数（缺一条说明这个拆法不对，宁可不给按钮）。

    n_components：1=标量/颜色（整名直查），2/3/4=向量（按分量查）。
    """
    bare = _ptb_bare_name(param_name)
    if not bare:
        return None
    comps = ("X", "Y", "Z", "W")[:n_components] if n_components > 1 else ()
    for tlp in ptbehavior_tlp_candidates(b_type):
        pal = {dt & 0xFFFFFFFF: dtype for dt, dtype in DT_PALETTE.get(tlp, ())}
        if not pal:
            continue
        h = jamcrc(bare.encode()) & 0xFFFFFFFF
        if h in pal:
            return tlp, [(h, pal[h])]
        if comps:
            hs = [jamcrc((bare + c).encode()) & 0xFFFFFFFF for c in comps]
            if all(x in pal for x in hs):
                return tlp, [(x, pal[x]) for x in hs]
    return None


def _ptb_bare_name(param_name: str) -> str:
    """'mIntensity' → 'Intensity'；未知名（0x 形式）/空 → ''。"""
    s = (param_name or "").strip()
    if not s or s.startswith("0x"):
        return ""
    return s[1:] if (len(s) > 1 and s[0] == "m" and s[1].isupper()) else s


def ptbehavior_param_dt(param_name: str):
    """PTBEHAVIOR 参数名（如 'mIntensity'）→ TIML datatypeHash；名字未知则 None。

    未知名在 UI 里是 '0x%08X' 形式（PTBEHAVIOR_NAMES 没收录），此时算不出 DT——
    真名未知就没法去 m 前缀，返回 None 让调用方不显示 +TIML 按钮。
    """
    s = (param_name or "").strip()
    if not s or s.startswith("0x"):
        return None
    bare = s[1:] if (len(s) > 1 and s[0] == "m" and s[1].isupper()) else s
    return jamcrc(bare.encode()) & 0xFFFFFFFF


def timeline_param_name(h: int) -> str:
    """timelineParameterHash → 短显示名，未知回退 0x 十六进制。

    两张表分工：`TLP_NAMES` 是**短名覆盖**（沿用多年的社区叫法，如 Billboard3D 而非
    官方的 TypeBillboard3D）；`TLP_FULLNAMES` 是官方 dump 的全限定类名。查不到短名
    就取全名的末段——⚠ 漏了这条回退会让 119 个**已经有名字**的 TLP 在下拉里显示成
    十六进制，看着像"没破解"，其实只是没接上显示层。
    """
    h &= 0xFFFFFFFF
    nm = TLP_NAMES.get(h)
    if nm:
        return nm
    full = TLP_FULLNAMES.get(h)
    if full:
        return full.rsplit("::", 1)[-1]
    return "0x%08X" % h


# ── TLP 分类：按命名空间分，用于 UI 里筛选那 162 个 TLP ────────────────────────
# 调色板里绝大多数 TLP 跟 efx 特效无关（材质动画 / 怪物玩家动作 / 音频事件），
# 编特效时全列出来是噪声，故按命名空间分四类。
#   EFX        nEffect::nTimelineParam::  +  nMhEffect::nTimelineParam::
#   MATERIAL   nDraw::MaterialAnimation::            —— TLP 就是主材质类型
#   AUDIO      nTimelineParam::nWwiseTimeline::
#   ANIMATION  nTimelineParam::（其余）              —— 动作 / 碰撞 / 模型部件 / 事件
# ⚠ 没有 TLP_FULLNAMES 的那几个是我们自己按公式反推确认的，全部落在
#   nEffect::nTimelineParam:: 命名空间，故归 EFX。
TLP_CATEGORIES = ("EFX", "MATERIAL", "ANIMATION", "AUDIO")

_TLP_CATEGORY_PREFIXES = (
    ("nEffect::nTimelineParam::",       "EFX"),
    ("nMhEffect::nTimelineParam::",     "EFX"),
    ("nDraw::MaterialAnimation::",      "MATERIAL"),
    ("nTimelineParam::nWwiseTimeline::", "AUDIO"),
    ("nTimelineParam::",                "ANIMATION"),   # 必须排在 Wwise 之后
)


def timeline_param_category(h: int) -> str:
    """timelineParameterHash → 分类（TLP_CATEGORIES 之一）。未知一律归 EFX。"""
    full = TLP_FULLNAMES.get(h & 0xFFFFFFFF)
    if full is None:
        return "EFX"
    for prefix, cat in _TLP_CATEGORY_PREFIXES:
        if full.startswith(prefix):
            return cat
    return "EFX"


def timeline_param_fullname(h: int) -> str:
    """timelineParameterHash → 官方全限定类名（含命名空间）；没有则退短名/十六进制。

    命名空间本身是有效信息——`nDraw::MaterialAnimation::` 打材质、
    `nTimelineParam::nWwiseTimeline::` 打音频、`nEffect::nTimelineParam::` 才是特效。
    列表里位置紧张只显示短名，全名放进 tooltip / description。
    """
    h &= 0xFFFFFFFF
    return TLP_FULLNAMES.get(h) or timeline_param_name(h)


def datatype_name(h: int) -> str:
    """datatypeHash → 友好属性名，未知回退 0x 十六进制。
    优先级：DT_TRANSFORM(transform 九条，含 Blender 映射) > DT_NAMES(DTI 字段名) > hex 回退。"""
    h &= 0xFFFFFFFF
    if h in DT_TRANSFORM:
        return DT_TRANSFORM[h][0]
    if h in DT_NAMES:
        return DT_NAMES[h]
    return "0x%08X" % h


def transform_mapping(h: int):
    """若 datatypeHash 是 transform 九条之一，返回 (bl_prop, bl_index, kind)，否则 None。
    供 transform3d 原生播放映射到真实 location/rotation_euler/scale。"""
    info = DT_TRANSFORM.get(h & 0xFFFFFFFF)
    if info is None:
        return None
    return info[1], info[2], info[3]


def channel_label(tlp_hash: int, dt_hash: int) -> str:
    """组合通道友好名：'Transform3D · pos:X'。"""
    return "%s · %s" % (timeline_param_name(tlp_hash), datatype_name(dt_hash))


# ── BLOCK_TO_TLP：块类型名（大写）→ timelineParameterHash ─────────────────────────
# 用于 T2 字段侧 +TIML 按钮按块类型查 TLP hash。只列已知映射（TLP 名与块类型名对应）。
BLOCK_TO_TLP = {
    "TRANSFORM3D":    0x4D111433,
    "TRANSFORM2D":    0x540A2572,
    "BILLBOARD3D":    0x327A81AC,
    "BILLBOARD2D":    0x2B61B0ED,
    "PLANE":          0x3481666B,
    "MESH":           0x538AF627,
    "RIBBON":         0x1436E592,
    "STRAINRIBBON":   0x1F09850E,
    "VELOCITY3D":     0x32C1B4B4,
    "VELOCITY2D":     0x2BDA85F5,
    "EMITTERSHAPE3D": 0x33185295,
    "EMITTERSHAPE2D": 0x2A0363D4,
    "RGBFIRE":        0x60BA9117,
    "RGBWATER":       0x2101C529,
    "SCALEANIM":      0x2A62F92E,
    "ROTATEANIM":     0x563C8065,
    "UVSEQUENCE":     0x5AC7FC29,
    "TUBELIGHT":      0x39C68FB4,
    "LIFE":           0x4A0D2B6A,
}

# ── FIELD_TO_DT：(块类型名大写, schema ori_name) → [(dt_hash, data_type), ...] ────
# 只收录"确认"级字段（FIELD_OFFICIAL_NAMES 中 "确认" 置信度的映射）。
# 向量字段按 X/Y/Z 顺序；data_type: 0=SInt 1=Int 2=Float 3=Color(RGBA) 4=Bool。
FIELD_TO_DT = {
    ("TRANSFORM3D", "translate"): [(0x8E8AFE06, 2), (0xF98DCE90, 2), (0x60849F2A, 2)],
    ("TRANSFORM3D", "rotate"):    [(0xF105BBE3, 2), (0x86028B75, 2), (0x1F0BDACF, 2)],
    ("TRANSFORM3D", "resize"):    [(0x9486DF23, 2), (0xE381EFB5, 2), (0x7A88BE0F, 2)],
    ("BILLBOARD3D", "color"):      [(0x58689812, 3)],
    ("BILLBOARD3D", "colorRange"): [(0xC216C23D, 3)],
    ("BILLBOARD3D", "rotation"):   [(0x2FF50558, 2)],
    ("BILLBOARD3D", "brightness"): [(0x9F1E012E, 2)],
    ("BILLBOARD3D", "height"):     [(0x531B9E44, 2)],
    ("BILLBOARD3D", "scale"):      [(0x0EBAEC37, 2)],
    ("BILLBOARD3D", "width"):      [(0x241CAED2, 2)],
    # Color/ColorRange/ColorRate/Rotation/SizeScalar/rotation/scale 五个哈希跟 BILLBOARD3D 完全同名同值；
    ("BILLBOARD2D", "color"):      [(0x58689812, 3)],
    ("BILLBOARD2D", "colorRange"): [(0xC216C23D, 3)],
    ("BILLBOARD2D", "brightness"): [(0x9F1E012E, 2)],
    ("BILLBOARD2D", "rotation"):   [(0x2FF50558, 2)],
    ("BILLBOARD2D", "scale"):      [(0x0EBAEC37, 2)],
    ("PLANE", "color"):           [(0x58689812, 3)],
    ("PLANE", "colorRange"):      [(0xC216C23D, 3)],
    ("PLANE", "brightness"):      [(0x9F1E012E, 2)],
    ("PLANE", "scale"):           [(0x0EBAEC37, 2)],
    ("PLANE", "height"):          [(0x531B9E44, 2)],
    ("MESH", "scale"):            [(0x241CAED2, 2), (0x531B9E44, 2), (0xCA12CFFE, 2)],
    ("MESH", "rotation"):         [(0x002FF505, 2), (0x7728C593, 2), (0xEE219429, 2)],
    ("MESH", "emissiveColorRate"): [(0x18C577DE, 2)],
    # ColorRate 归属 colorRate（原 emissive_saturation）：该字段由 enableIntensity2
    # 门控、对发光通道三个开关零响应，是 color 通道的强度系数，详见
    # custom_codecs.py::_MOD3_PROPERTIES_SCHEMA 注释
    ("MESH", "colorRate"): [(0x9F1E012E, 2)],
    ("MESH", "color"):               [(0x58689812, 3)],
    ("MESH", "colorRange"):          [(0xC216C23D, 3)],
    ("MESH", "emissiveColor"):       [(0x608DCF8D, 3)],
    ("MESH", "emissiveColorRange"):  [(0x7F2CEB57, 3)],
    ("MESH", "global_scale"):        [(0x0EBAEC37, 2)],
    ("RIBBON", "brightness"):     [(0x9F1E012E, 2)],
    ("RIBBON", "width"):          [(0xF0DF339B, 2)],
    ("RIBBON", "scale"):          [(0x0EBAEC37, 2)],
    ("RIBBON", "length"):         [(0xF92E647B, 2)],
    # ── 2026-09-03 用户实机逐条确认的一批 ───────────────────────────────────
    ("RIBBON", "colorRange"):        [(0xC216C23D, 3)],
    ("PLANE", "width"):              [(0x241CAED2, 2)],   # DT 名 SizeX
    ("TRANSFORM2D", "rotation"):     [(0xE2C6589E, 2)],   # DT 名 rot（官方就是小写）
    ("VELOCITY2D", "gravity"):       [(0x6A5FE3C4, 2)],
    ("STRAINRIBBON", "color"):       [(0x58689812, 3)],
    ("STRAINRIBBON", "colorRange"):  [(0xC216C23D, 3)],
    ("STRAINRIBBON", "emissionStrength"): [(0x9F1E012E, 2)],   # DT ColorRate
    # displacement 是 XYZ type 0（FLOAT6），背板按游戏序逐轴成对交错
    # [staticX,randomX, staticY,randomY, staticZ,randomZ]；官方名的 Min/Max 就是
    # 我们的 static/random，与 EMITTERSHAPE3D.rangeXYZ 同一套读法，故挂 6 条。
    ("STRAINRIBBON", "displacement"): [
        (0x760F3D43, 2), (0x6484D92C, 2),   # RangeMinX(static X), RangeMaxX(random X)
        (0x01080DD5, 2), (0x1383E9BA, 2),   # RangeMinY / RangeMaxY
        (0x98015C6F, 2), (0x8A8AB800, 2),   # RangeMinZ / RangeMaxZ
    ],
    # startPosition 是 XYZ type 3，同 endPosition 那条：背板按游戏分量序逐轴排，挂 3 条
    ("STRAINRIBBON", "startPosition"): [
        (0xE66B06CD, 2), (0x916C365B, 2), (0x086567E1, 2),  # InitialPosition X / Y / Z
    ],
    # EMITTERSHAPE2D 与 3D 逐字段同构：官方名的 RangeMin/Max 就是我们实测的 offset/size
    # （Min=offset、Max=size），与 EMITTERSHAPE3D.rangeXYZ 同一套读法。2D 把两轴存成
    # 四个独立标量（*Jitter 后缀是历史命名，实为 size），故逐个挂。
    ("EMITTERSHAPE2D", "rangeX"):       [(0x760F3D43, 2)],   # RangeMinX = offsetX
    ("EMITTERSHAPE2D", "rangeXJitter"): [(0x6484D92C, 2)],   # RangeMaxX = sizeX
    ("EMITTERSHAPE2D", "rangeY"):       [(0x01080DD5, 2)],   # RangeMinY = offsetY
    ("EMITTERSHAPE2D", "rangeYJitter"): [(0x1383E9BA, 2)],   # RangeMaxY = sizeY
    ("STRAINRIBBON", "length"):   [(0xF92E647B, 2)],
    ("STRAINRIBBON", "width"):    [(0xF0DF339B, 2)],
    ("VELOCITY3D", "gravity"):    [(0x6A5FE3C4, 2)],
    ("RGBFIRE", "fireColor"):     [(0x39A1E557, 3)],
    ("RGBFIRE", "smokeColor"):    [(0x5A8C6820, 3)],
    ("RGBFIRE", "colorRate"):     [(0x9F1E012E, 2)],  # 2026-09-20 订正：原挂在 brightness2 上是错的，实机改值测出是 brightness4
    # alphaFactor：2026-09-20 试过按裸名 jamcrc 现算的两个候选（"AlphaFactor"→0xCD5F1BDD、
    # "AlphaRate"→0xA55C051B），用户在曲线编辑器手动建轨道实机测试，两个都不生效——已撤回，
    # 不留在 FIELD_TO_DT/DTI_EXTRA_PAIRS 里。负结果记着：这个字段目前没有已知能驱动它的
    # DT 哈希，别重复猜同一批候选。
    # ── RGBWATER（2026-09-03 用户实机逐条确认；轨道只在 A0 生效，见 BLOCK_NATIVE_AXIS）──
    # 官方 8 个 TimelineParam 与本块头部 8 个字段**全部实机逐条确认**，无遗留。
    # 头部的第 9 个 float（normalSharpness，原 unknownFloat）没有对应 DT——引擎只
    # 声明 6 个 float 参数，这里有 7 个 float，多出来的那个不可动画。
    ("RGBWATER", "colorRate"):         [(0x9F1E012E, 2)],
    ("RGBWATER", "waterLerpGtoB"):     [(0xA7EDA21C, 2)],
    ("RGBWATER", "intensitySheet"):    [(0x4E00491F, 2)],
    ("RGBWATER", "intensityCubeMap"):  [(0xAFB51DF4, 2)],
    ("RGBWATER", "intensitySpecular"): [(0xD62C2891, 2)],
    ("RGBWATER", "intensityAlpha"):    [(0x19DCE197, 2)],
    ("RGBWATER", "colorSpecular"):     [(0x60D69856, 3)],
    ("RGBWATER", "colorSheet"):        [(0xD6AD0996, 3)],
    ("SCALEANIM", "sizeScalarAdd"): [(0xC24DF97C, 2)],
    ("SCALEANIM", "sizeYAdd"):       [(0x2822A722, 2)],
    ("SCALEANIM", "sizeXAdd"):       [(0x909EC047, 2)],
    ("SCALEANIM", "sizeZAdd"):       [(0x3A9708CC, 2)],
    ("TUBELIGHT", "headColor"):   [(0x3BA67E7C, 3)],
    ("TUBELIGHT", "tailColor"):   [(0x2AA40DE9, 3)],
    # ── 2026-08 补漏：已有 BLOCK_TO_TLP 映射、但字段表漏掉的 DT ────────────────
    # 缺口来源：官方语料按 (TLP, DT) 统计，+TIML 按钮原先只覆盖 47.3% 的轨道。
    # 只补"字段名与 DT 官方名精确一致"或"与已映射块结构完全同构"的条目；
    # 语义拿不准的见本表下方 FIELD_TO_DT_UNRESOLVED。
    # TRANSFORM2D：与 TRANSFORM3D 同构，但 2D 是拆开的标量字段而非 XYZ 向量
    ("TRANSFORM2D", "offsetX"):   [(0x8E8AFE06, 2)],   # pos:X
    ("TRANSFORM2D", "offsetY"):   [(0xF98DCE90, 2)],   # pos:Y
    ("TRANSFORM2D", "scaleX"):    [(0x9486DF23, 2)],   # scl:X
    ("TRANSFORM2D", "scaleY"):    [(0xE381EFB5, 2)],   # scl:Y
    # BILLBOARD2D：width/height ↔ SizeX/SizeY，与 BILLBOARD3D 同名同哈希
    ("BILLBOARD2D", "width"):     [(0x241CAED2, 2)],   # SizeX
    ("BILLBOARD2D", "height"):    [(0x531B9E44, 2)],   # SizeY
    # UVSEQUENCE：DTI 改名后字段名与 DT 名精确一致
    ("UVSEQUENCE", "playSpeed"):     [(0xE5C92264, 2)],
    ("UVSEQUENCE", "playSpeedCoef"): [(0x33A4A86B, 2)],
    # VELOCITY2D/3D：speed ↔ Speed（两块共用同一 DT hash）
    ("VELOCITY2D", "speed"):      [(0x31182E0D, 2)],
    ("VELOCITY3D", "speed"):      [(0x31182E0D, 2)],
    # EMITTERSHAPE3D：localRotationY/Z 与 DT 名精确一致（X 在语料里未出现）
    ("EMITTERSHAPE3D", "localRotationX"): [(0x701FE225, 2)],
    ("EMITTERSHAPE3D", "localRotationY"): [(0x0718D2B3, 2)],
    ("EMITTERSHAPE3D", "localRotationZ"): [(0x9E118309, 2)],
    # EMITTERSHAPE3D.rangeXYZ ↔ RangeMin/Max XYZ：官方名的 Min/Max 就是我们实测
    # 改判出的 offset/size（Min=offset、Max=size），两种叫法指同一对，此前记为
    # 矛盾是误会。rangeXYZ 是 XYZ type 0（FLOAT6），背板按游戏序逐轴成对交错
    # [offsetX,sizeX, offsetY,sizeY, offsetZ,sizeZ]，故 DT 也按这个顺序排——
    # 挂 6 条而非 3 条，_field_seed_values 对 6 条走逐位对齐。
    ("EMITTERSHAPE3D", "rangeXYZ"): [
        (0x760F3D43, 2), (0x6484D92C, 2),   # RangeMinX(offsetX), RangeMaxX(sizeX)
        (0x01080DD5, 2), (0x1383E9BA, 2),   # RangeMinY(offsetY), RangeMaxY(sizeY)
        (0x98015C6F, 2), (0x8A8AB800, 2),   # RangeMinZ(offsetZ), RangeMaxZ(sizeZ)
    ],
    # RIBBON：color ↔ Color（XYZ type 2 = RGBA），该块 361 条轨道里 136 条是它
    ("RIBBON", "color"):          [(0x58689812, 3)],
    # ── TUBELIGHT（2026-09-04 用户实机逐条测出；⚠ 此前把 DT LightIntensity 挂在
    # off20 那个字段上是错的，那一格实际由 CoreThickness 驱动，见 custom_codecs 注释）
    ("TUBELIGHT", "lightIntensity"):      [(0x085BC9D5, 2)],   # off16，原 unknFixed1_1
    ("TUBELIGHT", "coreIntensity"):       [(0xB6C0BDF8, 2)],   # off20（0.6.6 曾误定为 CoreThickness）
    ("TUBELIGHT", "columnRadius"):        [(0x316D89C5, 2)],   # off32，DT CoreThickness
    ("TUBELIGHT", "columnLengthModifier"):[(0x6ACAAAD7, 2)],   # off28，DT CoreLength
    ("TUBELIGHT", "textureScrollSpeed"):  [(0x931E7E65, 2)],   # off60，原 unkn2_1
    ("TUBELIGHT", "effectiveRadius"):     [(0x435F3054, 2)],   # off44，原 unkn1_8
    ("TUBELIGHT", "tailEffectiveRadius"): [(0x7D0FD331, 2)],   # off92，原 backFaceTintMode
    ("TUBELIGHT", "headEffectiveRadius"): [(0x102FFF8B, 2)],   # off120，原 frontFaceTintMode
    # STRAINRIBBON：endPosition(XYZ type 3) ↔ TerminatePosition X/Y/Z，游戏分量序
    ("STRAINRIBBON", "endPosition"): [(0x6458334F, 2), (0x135F03D9, 2), (0x8A565263, 2)],
    # ROTATEANIM：官方名里的 "Add" 是**速度**不是加速度，按语料量级判定（2026-08）——
    #   加速度/系数类字段被结构性钉在 1 附近（billboardRotationCoef 中位 1.000/最大
    #   1.20，spinSpeedCoef* 最大 1.10，SCALEANIM 的 scaleAccel* 最大 2.0），而
    #   Add 类 DT 的关键帧值 routinely 冲到 6/10/20（RotationAdd 最大 20、
    #   RotationAddX 最大 10、SizeScalarAdd 最大 6），给"最大只到 1.2 的系数"做的
    #   动画不可能跑到 20。速度类字段则是小中位+宽范围（billboardRotation 中位
    #   0.65/最大 500，spin_velocity 中位 2~3/最大 300），与 DT 分布吻合。
    #   同一判据也确认了 SCALEANIM 既有的 SizeXAdd↔sizeXAdd 等四条是对的。
    ("ROTATEANIM", "billboardRotation"): [(0xE81961E4, 2)],                          # RotationAdd
    ("ROTATEANIM", "spin_velocity"): [(0xC23FE6C6, 2), (0xB538D650, 2), (0x2C3187EA, 2)],  # RotationAdd X/Y/Z
}

# ── FIELD_TO_DT_UNRESOLVED：语料里有轨道、但字段归属没定论的 DT ─────────────────
# 不进 FIELD_TO_DT（+TIML 按钮不显示），登记在此免得反复重新调查。要加进主表
# 必须先坐实"这条 DT 对应哪个字段"，光看名字像不够——错的映射会让用户以为在给
# A 字段做动画、实际写进了 B 字段的通道。
# ── DT_NO_EFFECT：**实机测过、看不出任何变化**的 (块, DT) ─────────────────────
# 负结果也是结果。不记下来的话，下一轮清点又会把它们当"待测"列出来，白测第二遍。
#
# ⚠ 「看不出变化」不等于「一定无效」——可能是效果太细微、需要配合别的开关、或者
#   测试场景不对。所以只记录"测过、当时没看出来"，不宣称废弃。它们仍留在调色板里
#   （用户想再试随时能建轨道），只是从未验证清单里挪走。
DT_NO_EFFECT = {
    # Emission 三件套：横跨 5 个渲染主体全都有、官方一次没用过。用户 2026-09-03 在
    # BILLBOARD3D 上逐条测过三条，**都看不到变化**；RIBBON / PLANE / BILLBOARD2D 复测
    # 情况相同。看着像共用 shader 基类留下的死槽位。
    # （STRAINRIBBON 也有这三条 + EmissionRate，未测，大概率同理。）
    ("BILLBOARD3D",  0xEEBD5618): "EmissionRate",
    ("BILLBOARD3D",  0xCDBCBB7E): "EmissionColorRange",
    ("BILLBOARD3D",  0xF80CE653): "EmissionColor",
    ("RIBBON",       0xEEBD5618): "EmissionRate",
    ("RIBBON",       0xCDBCBB7E): "EmissionColorRange",
    ("RIBBON",       0xF80CE653): "EmissionColor",
    ("PLANE",        0xEEBD5618): "EmissionRate",
    ("PLANE",        0xCDBCBB7E): "EmissionColorRange",
    ("PLANE",        0xF80CE653): "EmissionColor",
    ("BILLBOARD2D",  0xEEBD5618): "EmissionRate",
    ("BILLBOARD2D",  0xCDBCBB7E): "EmissionColorRange",
    ("BILLBOARD2D",  0xF80CE653): "EmissionColor",
    # EmitterShape2D 的 LocalRotation：块里只剩 unknFixed22_1（全语料恒 0）能当宿主，
    # 改它看不到变化。要么是死槽位，要么宿主字段还没被我们解出来。
    ("EMITTERSHAPE2D", 0x7516AA5D): "LocalRotation",
}

# ── ⚠ 2026-09-04：STRAINRIBBON.SizeScalar 推翻了"DT 必有对应字段"的默认假设 ──────
# 用户实测：给 SizeScalar 建轨道，关键帧写 0 无变化、写 >0 有变化——**这条 DT 确实生效**。
# 但把该块字段表里所有为 0 的浮点都改成 1，游戏里看不到任何对应变化。
#
# 也就是说：**这条 DT 操纵的量根本不在我们解出来的字段里**。可能是块里还没被识别出来的
# 位，也可能是引擎侧的运行时量（压根不存在于 efx 字节里）。
#
# 这动摇了两条一直在用的默认前提：
#   ① "每条 DT 都对应某个静态字段" —— 不成立，至少有反例。
#   ② "改 DT 没看到变化 = 这条 DT 是死的" —— 也可能是**某个开关没开**，而开关不一定
#      在字段里。DT_NO_EFFECT 里的 Emission 三件套因此不能直接判死：先在字段里找相关性
#      （哪个开关一开、Emission 就有反应），找不到才谈废弃。
# 保留 SizeScalar 在待测清单里，不进 DT_NO_EFFECT——它是**有效果**的，只是宿主未知。


FIELD_TO_DT_UNRESOLVED = {
    # RGBWATER 原在此（4 条全未定）——2026-09-03 用户实机逐条测出归属，已迁进 FIELD_TO_DT。
    # TUBELIGHT：CoreThickness（17 条）候选 columnRadius / columnEdgeSoftness；
    #   CoreIntensity（7 条）无对应字段。columnRadius 已实机确认过语义，正因如此
    #   不能凭"thickness≈radius"就把 DT 挂上去。
    ("TUBELIGHT", "?"): [(0x316D89C5, 2), (0xB6C0BDF8, 2)],
    # STRAINRIBBON：ColorRate 候选 emissionStrength（RIBBON 那边是 brightness，
    #   本块没有同名字段）；LocalRotationY 在本块字段表里没有对应项。
    ("STRAINRIBBON", "?"): [(0x9F1E012E, 2), (0x0718D2B3, 2)],
}

# ── BLOCK_NATIVE_AXIS：块类型名(大写) → 该块 TIML 动画在真实语料里锁定的轴 slot ──────
# 绝大多数块类型的动画几乎只出现在单一轴上（A0=发射轴/系统时间线，A1=寿命轴/每粒子）。
# 强烈线索：游戏似乎只在母轴上应用对应块的动画——把轨道加到另一条轴会静默无效。
# 实测佐证：TubeLight/Transform3D/RgbFire（均 A0 锁定）加到 A1 全不生效，
# 而 Billboard3D 加哪条都生效。
#   0 = A0 母轴 / 1 = A1 母轴 / None = 两轴都常见（不限制，如 Billboard3D）
# 锁定率（>90% 的视为"母轴"）：
#   A0: Transform3D 98% / RgbFire 100% / RgbWater 98% / TubeLight 100% /
#       EmitterShape3D 93% / Transform2D 100% / Velocity2D 100%
#   A1: Mesh 98% / Ribbon 96% / Plane 91% / StrainRibbon 100% / UVSequence 99% /
#       RotateAnim 97% / ScaleAnim 92% / Velocity3D 95% / Billboard2D 100%
BLOCK_NATIVE_AXIS = {
    "TRANSFORM3D":    0,
    "TRANSFORM2D":    0,
    "RGBFIRE":        0,
    "RGBWATER":       0,
    "TUBELIGHT":      0,
    "EMITTERSHAPE3D": 0,
    "VELOCITY2D":     0,
    "MESH":           1,
    "RIBBON":         1,
    "PLANE":          1,
    "STRAINRIBBON":   1,
    "UVSEQUENCE":     1,
    "ROTATEANIM":     1,
    "SCALEANIM":      1,
    "VELOCITY3D":     1,
    "BILLBOARD2D":    1,
    "BILLBOARD3D":    None,   # 两轴都常见（350:1657），不限制
}


def block_native_axis(block_type: str):
    """块类型的 TIML 母轴 slot（0=A0/1=A1），两轴都常见或未知 → None。"""
    return BLOCK_NATIVE_AXIS.get((block_type or "").upper())


# official 语料各块类型 TIML 轨道数 (A0, A1)。用于判定是否存在绝对优势轴。
BLOCK_AXIS_COUNTS = {
    "TRANSFORM3D":    (955, 9),
    "TRANSFORM2D":    (72, 0),
    "RGBFIRE":        (454, 2),
    "RGBWATER":       (58, 2),
    "TUBELIGHT":      (42, 0),
    "EMITTERSHAPE3D": (665, 37),
    "VELOCITY2D":     (1, 0),
    "MESH":           (8, 418),
    "RIBBON":         (18, 343),
    "PLANE":          (2, 20),
    "STRAINRIBBON":   (0, 66),
    "UVSEQUENCE":     (1, 95),
    "ROTATEANIM":     (1, 48),
    "SCALEANIM":      (4, 76),
    "VELOCITY3D":     (5, 113),
    "BILLBOARD2D":    (0, 2),
    "BILLBOARD3D":    (364, 2056),
}

#: 优势轴门槛：占比至少这么高、且样本不少于 DOMINANT_AXIS_MIN_TRACKS 条才算绝对优势
DOMINANT_AXIS_SHARE = 0.90
DOMINANT_AXIS_MIN_TRACKS = 10


def dominant_axis(block_type: str):
    """块类型的绝对优势轴 slot（0/1）；没有绝对优势或样本太少 → None。"""
    counts = BLOCK_AXIS_COUNTS.get((block_type or "").upper())
    if not counts:
        return None
    total = counts[0] + counts[1]
    if total < DOMINANT_AXIS_MIN_TRACKS:
        return None
    slot = 0 if counts[0] >= counts[1] else 1
    return slot if counts[slot] >= DOMINANT_AXIS_SHARE * total else None


# ── CORPUS_PAIRS：TLP hash → [(dt_hash, data_type), ...] ─────────────────────────
# 只收录有命名的 DT hash（DT_NAMES 或 DT_TRANSFORM 覆盖的条目）。
CORPUS_PAIRS = {
    0x327A81AC: [  # Billboard3D，1645次最多
        (0x58689812, 3), (0x9F1E012E, 2), (0x531B9E44, 2), (0x0EBAEC37, 2),
        (0x241CAED2, 2), (0x2FF50558, 2), (0xC216C23D, 3),
    ],
    0x4D111433: [  # Transform3D，pos/rot/scl 9条
        (0xF98DCE90, 2), (0x60849F2A, 2), (0x8E8AFE06, 2),
        (0xE381EFB5, 2), (0x9486DF23, 2), (0x7A88BE0F, 2),
        (0x86028B75, 2), (0xF105BBE3, 2), (0x1F0BDACF, 2),
    ],
    0x65004E2A: [  # MhEffectDecalBehavior
        (0x7D235C30, 2), (0xCBDB6622, 3), (0x3775827A, 2), (0x4072B2EC, 2),
        (0xBF160652, 2), (0x26BD5CC2, 3), (0xAE7CD3C0, 2), (0x831B390B, 2),
        (0x4D41A06B, 2), (0x16814F1C, 2),
    ],
    0x538AF627: [  # Mesh
        (0x18C577DE, 2), (0x531B9E44, 2), (0x58689812, 3), (0x241CAED2, 2),
        (0x002FF505, 2), (0xCA12CFFE, 2), (0xEE219429, 2), (0x9F1E012E, 2),
        (0x7728C593, 2), (0x608DCF8D, 3), (0x0EBAEC37, 2),
    ],
    0x33185295: [  # EmitterShape3D
        (0x1383E9BA, 2), (0x8A8AB800, 2), (0x6484D92C, 2),
        (0x98015C6F, 2), (0x760F3D43, 2), (0x01080DD5, 2),
        (0x0718D2B3, 2), (0x9E118309, 2),
    ],
    0x1436E592: [  # Ribbon
        (0x9F1E012E, 2), (0x58689812, 3), (0xF0DF339B, 2), (0x0EBAEC37, 2), (0xF92E647B, 2),
    ],
    0x60BA9117: [  # RgbFire
        (0x9F1E012E, 2), (0x5A8C6820, 3), (0x39A1E557, 3),
    ],
    0x6DA6E5D1: [  # MhPointLightBehavior
        (0x94BCC5CE, 2), (0x58689812, 3), (0x435F3054, 2), (0xC32F9493, 2),
    ],
    0x42E48DDE: [  # PointLightBehavior
        (0x94BCC5CE, 2), (0x435F3054, 2), (0xC32F9493, 2), (0x58689812, 3),
    ],
    0x598272E1: [  # 未知（PARENTEMISSIVE/PLEMISSIVE 共现）
        (0x94BCC5CE, 2), (0xFA79B1CD, 3),
    ],
    0x32C1B4B4: [  # Velocity3D
        (0x31182E0D, 2), (0x6A5FE3C4, 2),
    ],
    0x09C466DC: [  # 未知（PTCOLLISION 共现）
        (0x371BBC04, 2), (0xAFE95AC0, 3), (0xA01B7821, 3), (0x1BB0EB80, 2),
    ],
    0x5AC7FC29: [  # UVSequence
        (0xE5C92264, 2), (0x33A4A86B, 2),
    ],
    0x2A62F92E: [  # ScaleAnim
        (0xC24DF97C, 2), (0x2822A722, 2), (0x909EC047, 2), (0x3A9708CC, 2),
    ],
    0x540A2572: [  # Transform2D
        (0x8E8AFE06, 2), (0xF98DCE90, 2), (0x9486DF23, 2), (0xE381EFB5, 2),
    ],
    0x1F09850E: [  # StrainRibbon
        (0xF92E647B, 2), (0xF0DF339B, 2), (0x6458334F, 2), (0x135F03D9, 2),
        (0x8A565263, 2), (0x9F1E012E, 2), (0x0718D2B3, 2),
    ],
    0x5E8D9EE9: [  # 未知（EMITTERSHAPEMESH 共现）
        (0xC207B8B4, 2), (0xAFE95AC0, 3), (0x1BB0EB80, 2), (0xA01B7821, 3),
    ],
    0x2101C529: [  # RgbWater
        (0xA7EDA21C, 2), (0x9F1E012E, 2), (0x4E00491F, 2), (0x60D69856, 3),
    ],
    0x563C8065: [  # RotateAnim
        (0xE81961E4, 2), (0xC23FE6C6, 2), (0xB538D650, 2), (0x2C3187EA, 2),
    ],
    0x582BA062: [  # RadialBlurFilterBehavior
        (0x1D95BB54, 2), (0xAB9D6334, 2), (0x58689812, 3), (0x0ECBFA29, 2), (0x0EF6ABF4, 2),
    ],
    0x39C68FB4: [  # TubeLight
        (0x316D89C5, 2), (0x085BC9D5, 2), (0xB6C0BDF8, 2), (0x3BA67E7C, 3), (0x2AA40DE9, 3),
    ],
    0x3481666B: [  # Plane
        (0x9F1E012E, 2), (0x58689812, 3), (0x0EBAEC37, 2), (0x531B9E44, 2),
    ],
    0x75963575: [  # MhSpotLightBehavior
        (0x94BCC5CE, 2), (0x58689812, 3),
    ],
    0x399DB6A9: [  # 未知
        (0x71BCF0AA, 2), (0xC207B8B4, 2),
    ],
    0x7E51F5BD: [  # 未知（EMITTERBOUNDARY 共现）
        (0x4279F094, 2), (0x13804BC9, 2),
    ],
    0x2B61B0ED: [  # Billboard2D
        (0x241CAED2, 2), (0x531B9E44, 2),
    ],
    0x66C62149: [  # 未知
        (0xA01B7821, 3), (0x1BB0EB80, 2),
    ],
    0x2C78B827: [  # 未知
        (0xA01B7821, 3), (0x1BB0EB80, 2),
    ],
    0x4E64D91C: [  # 未知（PLANE 共现）
        (0xAFE95AC0, 3), (0x1BB0EB80, 2),
    ],
    0x2BDA85F5: [  # Velocity2D
        (0x31182E0D, 2),
    ],
    0x0B8924DA: [  # 未知（PTTRIGGER 共现）
        (0xAFE95AC0, 3),
    ],
    0x3E880466: [  # 未知
        (0x0FF5554F, 3),
    ],
    0x5752ED69: [  # 未知
        (0xAFE95AC0, 3),
    ],
}

# ── DTI_EXTRA_PAIRS：DTI 里声明、但全语料 0 例的 (TLP → [(DT, dataType)]) ────────
# CORPUS_PAIRS 只收语料实测到的组合，是「官方确实这么用过」的证据表，语义要保持干净
# （tools/lib/timl_engine.py、tools/scan_tubelight.py 按这个含义在用）。但「官方没用过」
# 不等于「引擎不支持」——不把这些放进调色板，用户就永远没法给它们建轨道做实机测试，
# 而这正是往下推语义的主要手段（RgbWater 的 4 条字段映射就是这么测出来的）。
# 故另立此表，仅供 UI 调色板 DT_PALETTE 合并使用。
#
# ⚠ 这里的每一条都**未经任何验证**：既没有官方用例，也没有实机确认。加了轨道游戏里
#   是否生效、对应哪个字段，都要自己测。（多数块还受 BLOCK_NATIVE_AXIS 的母轴限制。）
DTI_EXTRA_PAIRS = {
    0x2B61B0ED: [  # Billboard2D（DTI: TypeBillboard2D），+8
        (0x0EBAEC37, 2), (0x2FF50558, 2), (0xEEBD5618, 2), (0xCDBCBB7E, 3), (0xF80CE653, 3),
        (0x9F1E012E, 2), (0xC216C23D, 3), (0x58689812, 3)
    ],
    0x327A81AC: [  # Billboard3D（DTI: TypeBillboard3D），+3
        (0xEEBD5618, 2), (0xCDBCBB7E, 3), (0xF80CE653, 3)
    ],
    0x096CABC4: [  # ColorCorrectFilter，+51
        (0x93A7FACA, 2), (0x34B93272, 2), (0xB13D703D, 2), (0xA826417C, 2), (0xC63A40AB, 2),
        (0xDF2171EA, 2), (0x5F331111, 2), (0x46282050, 2), (0x28342187, 2), (0x312F10C6, 2),
        (0xB650B424, 2), (0xAF4B8565, 2), (0xC15784B2, 2), (0xD84CB5F3, 2), (0x585ED508, 2),
        (0x4145E449, 2), (0x2F59E59E, 2), (0x3642D4DF, 2), (0xB7F6B2D6, 2), (0xAEED8397, 2),
        (0xC0F18240, 2), (0xD9EAB301, 2), (0x59F8D3FA, 2), (0x40E3E2BB, 2), (0x2EFFE36C, 2),
        (0x37E4D22D, 2), (0xB09B76CF, 2), (0xA980478E, 2), (0xC79C4659, 2), (0xDE877718, 2),
        (0x5E9517E3, 2), (0x478E26A2, 2), (0x29922775, 2), (0x30891634, 2), (0xAD1BD34D, 2),
        (0xB400E20C, 2), (0xDA1CE3DB, 2), (0xC307D29A, 2), (0x4315B261, 2), (0x5A0E8320, 2),
        (0x341282F7, 2), (0x2D09B3B6, 2), (0xAA761754, 2), (0xB36D2615, 2), (0xDD7127C2, 2),
        (0xC46A1683, 2), (0x44787678, 2), (0x5D634739, 2), (0x337F46EE, 2), (0x2A6477AF, 2),
        (0xB78EE6DA, 2)
    ],
    0x2A0363D4: [  # EmitterShape2D，+5
        (0x7516AA5D, 2), (0x1383E9BA, 2), (0x01080DD5, 2), (0x6484D92C, 2), (0x760F3D43, 2)
    ],
    0x33185295: [  # EmitterShape3D，+1
        (0x701FE225, 2)
    ],
    0x538AF627: [  # Mesh（DTI: TypeMesh），+3
        (0xE5C92264, 2), (0x7F2CEB57, 3), (0xC216C23D, 3)
    ],
    0x65004E2A: [  # MhEffectDecalBehavior，+7
        (0xAA2229C3, 2), (0x59EEF098, 2), (0x275F8C25, 2), (0xBE56DD9F, 2), (0xC951ED09, 2),
        (0x353D6FFC, 2), (0xB1F7AEF0, 2)
    ],
    0x6DA6E5D1: [  # MhPointLightBehavior，+6
        (0x0E24B8A2, 2), (0xE609DBFA, 2), (0xCF5D2EDD, 2), (0x2AD90E22, 2), (0xE7DF422D, 2),
        (0xC126B7BD, 2)
    ],
    0x75963575: [  # MhSpotLightBehavior，+12
        (0x0E24B8A2, 2), (0x7AF2755D, 2), (0x7F2FEA9B, 2), (0x84E65AB1, 2), (0xD23788D3, 2),
        (0x435F3054, 2), (0xC32F9493, 2), (0xE609DBFA, 2), (0xCF5D2EDD, 2), (0x2AD90E22, 2),
        (0xE7DF422D, 2), (0xC126B7BD, 2)
    ],
    0x2ED89BCC: [  # ParentMaterial，+2
        (0x94BCC5CE, 2), (0xFA79B1CD, 3)
    ],
    0x3481666B: [  # Plane（DTI: TypePlane），+5
        (0x241CAED2, 2), (0xEEBD5618, 2), (0xCDBCBB7E, 3), (0xF80CE653, 3), (0xC216C23D, 3)
    ],
    0x42E48DDE: [  # PointLightBehavior，+5
        (0xE609DBFA, 2), (0xCF5D2EDD, 2), (0x2AD90E22, 2), (0xE7DF422D, 2), (0xC126B7BD, 2)
    ],
    0x582BA062: [  # RadialBlurFilterBehavior，+13
        (0xE22A3268, 2), (0xC893EE17, 2), (0x1204171A, 2), (0xBA17788F, 2), (0xDC774A4A, 2),
        (0x49AE09C7, 2), (0x2FCE3B02, 2), (0xC4A60432, 2), (0xA2C636F7, 2), (0x2B0CF983, 2),
        (0x55BD853E, 2), (0xCCB4D484, 2), (0xBBB3E412, 2)
    ],
    # RgbFire.alphaFactor：2026-09-20 试过 jamcrc("AlphaFactor")/jamcrc("AlphaRate") 两个
    # 候选哈希，实机测试都不生效，已撤回（详见 FIELD_TO_DT 旁的同一条记录）。
    # ⚠ RgbWater 这 4 条已被实机验证**确实可用**（用户 2026-09-03 建轨道逐条测出对应字段）
    #   —— 这是「DTI 有、语料 0 例」的条目并非死数据的直接证据，本表其余条目同理可测。
    0x2101C529: [  # RgbWater，+4
        (0x19DCE197, 2), (0xD62C2891, 2), (0xAFB51DF4, 2), (0xD6AD0996, 3)
    ],
    0x1436E592: [  # Ribbon（DTI: TypeRibbon），+7
        (0x994333F2, 2), (0x7E6555E0, 2), (0x9DB8917C, 2), (0xEEBD5618, 2), (0xCDBCBB7E, 3),
        (0xF80CE653, 3), (0xC216C23D, 3)
    ],
    0x3DE576DC: [  # SpotLightBehavior，+13
        (0x7AF2755D, 2), (0x7F2FEA9B, 2), (0x84E65AB1, 2), (0xD23788D3, 2), (0x435F3054, 2),
        (0xC32F9493, 2), (0xE609DBFA, 2), (0xCF5D2EDD, 2), (0x2AD90E22, 2), (0xE7DF422D, 2),
        (0xC126B7BD, 2), (0x94BCC5CE, 2), (0x58689812, 3)
    ],
    0x1F09850E: [  # StrainRibbon（DTI: TypeStrainRibbon），+20
        (0x9E118309, 2), (0x701FE225, 2), (0x8A8AB800, 2), (0x1383E9BA, 2), (0x6484D92C, 2),
        (0x98015C6F, 2), (0x01080DD5, 2), (0x760F3D43, 2), (0x086567E1, 2), (0x916C365B, 2),
        (0xE66B06CD, 2), (0x994333F2, 2), (0x7E6555E0, 2), (0x9DB8917C, 2), (0x0EBAEC37, 2),
        (0xEEBD5618, 2), (0xCDBCBB7E, 3), (0xF80CE653, 3), (0xC216C23D, 3), (0x58689812, 3)
    ],
    0x13A0F54F: [  # TonemapFilter，+1
        (0x94BCC5CE, 2)
    ],
    0x540A2572: [  # Transform2D，+1
        (0xE2C6589E, 2)
    ],
    0x39C68FB4: [  # TubeLight，+5
        (0x7D0FD331, 2), (0x102FFF8B, 2), (0x931E7E65, 2), (0x435F3054, 2), (0x6ACAAAD7, 2)
    ],
    0x2BDA85F5: [  # Velocity2D，+1
        (0x6A5FE3C4, 2)
    ],
}

# ── OFFICIAL_TLP_DT：官方 dump 给出的 (TLP → 该 TLP 支持的 DT) 对照表 ──────────
# 157 个 TLP / 1421 条映射，dataType 取自 DT_DATATYPE。
#
# ⚠ **这张表不是「什么能用」的完整清单，别拿它剪枝。** 实测反例：
#   · FIELD_TO_DT 里 33 条已实机确认可用的映射不在本表内（RGBWATER 的
#     IntensityAlpha/CubeMap/Specular/ColorSheet、TUBELIGHT 五条、STRAINRIBBON
#     的 displacement/startPosition、PLANE.SizeX、TRANSFORM2D.rot …）。
#   · 反向倒是很准：DT_NO_EFFECT 里实测无效的 12 条 Emission 全部不在本表内。
#   结论：**在表内 → 大概率可用；不在表内 → 说明不了什么**。所以与
#   CORPUS_PAIRS / DTI_EXTRA_PAIRS 取并集，不做删减。
OFFICIAL_TLP_DT = {
    0x01739779: [  # nTimelineParam::nWwiseTimeline::GameParameter
        (0x24C60AC4, 2), (0x2D7046EF, 2), (0x3C036342, 0), (0x4B0453D4, 0), (0x53C13A52, 2),
        (0x54ACFE4B, 2), (0xA267F6E1, 0), (0xA50A32F8, 0), (0xBAA29F67, 2), (0xBDCF5B7E, 2),
        (0xCDA5AFF1, 2), (0xD560C677, 0)
    ],
    0x03CE7F12: [  # nTimelineParam::Em110Motion
        (0x99897C43, 1)
    ],
    0x04EBB38D: [  # nDraw::MaterialAnimation::EM102_Mt
        (0x1BB0EB80, 2), (0x2BD52F93, 1), (0x2E710D12, 2), (0x84751FE5, 2), (0x8633A1BC, 2),
        (0x8F198226, 2), (0xA01B7821, 3), (0xAFE95AC0, 3), (0xF134912A, 2), (0xF3722F73, 2),
        (0xF7C55D41, 2)
    ],
    0x053CBDDA: [  # nTimelineParam::EffectParameter3
        (0x136F8726, 4), (0x3F19C8B3, 1), (0x630573A9, 4), (0x6468B7B0, 4), (0x8A66D69C, 4),
        (0xD117A99F, 1), (0xD67A6D86, 1)
    ],
    0x07758DAF: [  # nDraw::MaterialAnimation::EM036_Mt
        (0x0426764B, 2), (0x10272A04, 2), (0x2BD52F93, 1), (0x67201A92, 2), (0x744C82C4, 2),
        (0x80983795, 2), (0x9A42E3E8, 2), (0xED45D37E, 2), (0xFE294B28, 2)
    ],
    0x08171AF8: [  # nDraw::MaterialAnimation::EM032_Mt
        (0x1BB0EB80, 2), (0x2BD52F93, 1), (0xA01B7821, 3)
    ],
    0x09C466DC: [  # nDraw::MaterialAnimation::Standard_Mt
        (0x0BD3D5FB, 2), (0x1BB0EB80, 2), (0x2BD52F93, 1), (0x371BBC04, 2), (0x7CD4E56D, 2),
        (0x8F198226, 2), (0xA01B7821, 3), (0xAFE95AC0, 3)
    ],
    0x0A5CFC32: [  # nTimelineParam::CollisionSyncUID
        (0x08FD20A6, 1)
    ],
    0x0AC34102: [  # nTimelineParam::Em107Motion
        (0x57F1682F, 2), (0x99897C43, 1)
    ],
    0x0B8924DA: [  # nDraw::MaterialAnimation::EM106_Mt
        (0x1BB0EB80, 2), (0x2BD52F93, 1), (0x72A97996, 2), (0xA01B7821, 3), (0xA1ED0144, 3),
        (0xAFE95AC0, 3)
    ],
    0x0BFA707A: [  # nTimelineParam::Em080Motion
        (0x08FD20A6, 1), (0x53984EE2, 0), (0x99897C43, 1)
    ],
    0x0CFD985C: [  # nTimelineParam::nWwiseTimeline::EventCollision00
        (0x08FD20A6, 1), (0x15EF861B, 1), (0x1C59CA30, 1), (0x344C2BE4, 1), (0x3A8C67F8, 1),
        (0x3A97A3D6, 1), (0x3DFA67CF, 1), (0x434B1B72, 1), (0x4AFD5759, 1), (0x4D8B576E, 1),
        (0x4D909340, 1), (0x6B5EFAA6, 1), (0x6C333EBF, 1), (0x823D5F93, 1), (0x85509B8A, 1),
        (0xA39EF26C, 1), (0xA4F33675, 1), (0xAD457A5E, 1), (0xBCFFC41B, 0), (0xD3F406E3, 1),
        (0xD48206D4, 1), (0xDA424AC8, 1), (0xF257AB1C, 1), (0xF53A6F05, 1)
    ],
    0x0D4178F1: [  # nTimelineParam::Em120Motion
        (0x08FD20A6, 1), (0x99897C43, 1)
    ],
    0x0E556A0F: [  # nDraw::MaterialAnimation::EM117_Mt
        (0x1BB0EB80, 2), (0x2BD52F93, 1), (0xA01B7821, 3), (0xD3137725, 2), (0xE0341C9D, 2),
        (0xF7C55D41, 2)
    ],
    0x0E6D4A92: [  # nTimelineParam::Em045Motion
        (0x08FD20A6, 1)
    ],
    0x0FE12549: [  # nDraw::MaterialAnimation::VFX_VATDist_Mt
        (0x09956BA2, 2), (0x2BD52F93, 1)
    ],
    0x101C6C94: [  # nDraw::MaterialAnimation::EM024_Mt
        (0x1BB0EB80, 2), (0x2BD52F93, 1), (0x84751FE5, 2), (0x8633A1BC, 2), (0xA01B7821, 3),
        (0xF134912A, 2), (0xF3722F73, 2)
    ],
    0x141DAC90: [  # nTimelineParam::Em113_01Motion
        (0x99897C43, 1)
    ],
    0x1436E592: [  # nEffect::nTimelineParam::TypeRibbon
        (0x0EBAEC37, 2), (0x58689812, 3), (0x9F1E012E, 2), (0xF0DF339B, 2), (0xF92E647B, 2)
    ],
    0x14516E3B: [  # nTimelineParam::Em112Motion
        (0x08FD20A6, 1), (0x7BE08FA2, 0)
    ],
    0x15C60453: [  # nDraw::MaterialAnimation::SZK001_Mt
        (0x2BD52F93, 1), (0x2E710D12, 2), (0x4EDC6CE0, 2), (0xD0B8F943, 2)
    ],
    0x15F4C9E6: [  # nTimelineParam::nWwiseTimeline::EventCollision03
        (0x08FD20A6, 1), (0x1213267E, 1), (0x1C59CA30, 1), (0x344C2BE4, 1), (0x3A97A3D6, 1),
        (0x3DFA67CF, 1), (0x4D8B576E, 1), (0x4D909340, 1), (0x6069E6F0, 1), (0x6B5EFAA6, 1),
        (0x6C333EBF, 1), (0x823D5F93, 1), (0x8B1A77C4, 1), (0xA39EF26C, 1), (0xA4F33675, 1),
        (0xBCFFC41B, 0), (0xD3F406E3, 1), (0xF257AB1C, 1), (0xF53A6F05, 1), (0xF960B74A, 1)
    ],
    0x170FACE6: [  # nTimelineParam::AnimalFly
        (0x5AFE86CE, 2), (0x6B169C53, 2), (0xFC898D7A, 2)
    ],
    0x17359E0C: [  # nDraw::MaterialAnimation::VFX_Aurora_Mt
        (0x09956BA2, 2), (0x2BD52F93, 1), (0xFD480BC4, 2)
    ],
    0x17EAA6E5: [  # nTimelineParam::SpeedTreeWindGenerator
        (0x0B0F41DD, 0), (0x3C81CEA1, 0), (0x4CEB3A2E, 0), (0x7B65B552, 0), (0x956BD47E, 0),
        (0x99897C43, 1), (0x9C483C6C, 0), (0xA2E55B02, 0), (0xD5E26B94, 0), (0xE26CE4E8, 0)
    ],
    0x1830887C: [  # nTimelineParam::OtasukeMotion
        (0x08FD20A6, 1), (0x6324C0C4, 1), (0x6459658B, 1), (0x8F79C6AF, 1), (0x99897C43, 1),
        (0xB0BBDAC4, 0)
    ],
    0x18B27374: [  # nTimelineParam::Em118_05Motion
        (0x08FD20A6, 1), (0x99897C43, 1)
    ],
    0x193C8B34: [  # nDraw::MaterialAnimation::EM105_Mt
        (0x0426764B, 2), (0x10272A04, 2), (0x1BB0EB80, 2), (0x1C32DCCF, 2), (0x2BD52F93, 1),
        (0x67201A92, 2), (0x744C82C4, 2), (0x80983795, 2), (0x84751FE5, 2), (0x8633A1BC, 2),
        (0x9A42E3E8, 2), (0xA01B7821, 3), (0xADD4B2B7, 2), (0xB858DE36, 2), (0xED45D37E, 2),
        (0xF134912A, 2), (0xF3722F73, 2), (0xFE294B28, 2), (0xFF5207BD, 2)
    ],
    0x1A0B3112: [  # nDraw::MaterialAnimation::EM080_01_Mt
        (0x2BD52F93, 1), (0x2E710D12, 2), (0x7C692062, 2), (0xF9120603, 2)
    ],
    0x1E83FE5F: [  # nTimelineParam::EmCharmMountStepObject
        (0x11E90F5A, 0), (0x91BA4212, 2)
    ],
    0x1F09850E: [  # nEffect::nTimelineParam::TypeStrainRibbon
        (0x0718D2B3, 2), (0x135F03D9, 2), (0x6458334F, 2), (0x8A565263, 2), (0x9F1E012E, 2),
        (0xF0DF339B, 2), (0xF92E647B, 2)
    ],
    0x201C7206: [  # nTimelineParam::nWwiseTimeline::EventGroup09
        (0x08431812, 1), (0x0F2EDC0B, 1), (0x7829EC9D, 1), (0x7F442884, 1), (0x96278DB1, 1),
        (0x99897C43, 1), (0x9F91C19A, 1), (0xBCFFC41B, 0), (0xE120BD27, 1), (0xE64D793E, 1)
    ],
    0x20FC82E0: [  # nTimelineParam::ClawMotionVisual
        (0x99897C43, 1)
    ],
    0x2101C529: [  # nEffect::nTimelineParam::RgbWater
        (0x4E00491F, 2), (0x60D69856, 3), (0x9F1E012E, 2), (0xA7EDA21C, 2)
    ],
    0x233BDD27: [  # nTimelineParam::NpcCommon
        (0x08FD20A6, 1), (0x0B001AA6, 2), (0x256C8A54, 2), (0x2845C079, 0), (0x3F3D39AE, 2),
        (0x483A0938, 2), (0x4A6DD631, 0), (0x5376E770, 0), (0x5CE43CEA, 1), (0x614085F2, 0),
        (0x665699CA, 2), (0x785BB4B3, 0), (0x7C072A30, 2), (0x819DDA91, 0), (0x87A41BA7, 1),
        (0x8858F8E6, 2), (0x89582088, 0), (0x979BE3E1, 0), (0x9886EBD0, 0), (0x99897C43, 1),
        (0xA275734B, 0), (0xA6346814, 2), (0xAAB08952, 0), (0xAB1D2239, 1), (0xB3ABB813, 0),
        (0xBB6E420A, 0), (0xBC65DBEE, 2), (0xCB62EB78, 2), (0xE1C4FA93, 0), (0xE50E7B8A, 2),
        (0xFF5FC870, 2)
    ],
    0x24006667: [  # nTimelineParam::nWwiseTimeline::EventLoop
        (0x08431812, 1), (0x08FD20A6, 1), (0x0AD9C602, 1), (0x0DB4021B, 1), (0x0F2EDC0B, 1),
        (0x7829EC9D, 1), (0x7AB3328D, 1), (0x7DDEF694, 1), (0x7F442884, 1), (0x94BD53A1, 1),
        (0x96278DB1, 1), (0x9D0B1F8A, 1), (0x9F91C19A, 1), (0xBCFFC41B, 0), (0xE120BD27, 1),
        (0xE3BA6337, 1), (0xE4D7A72E, 1), (0xE64D793E, 1)
    ],
    0x245CA284: [  # nDraw::MaterialAnimation::EM115_Mt
        (0x0426764B, 2), (0x078C319A, 2), (0x10272A04, 2), (0x1BB0EB80, 2), (0x2BD52F93, 1),
        (0x4EDC6CE0, 2), (0x67201A92, 2), (0x708B010C, 2), (0x744C82C4, 2), (0x80983795, 2),
        (0x9A42E3E8, 2), (0x9E856020, 2), (0xA01B7821, 3), (0xD0B8F943, 2), (0xE0341C9D, 2),
        (0xED45D37E, 2), (0xFE294B28, 2)
    ],
    0x255D71CC: [  # nTimelineParam::Em013Motion
        (0x036D877F, 2), (0x08FD20A6, 1), (0x2DDB0D26, 2), (0x33A4D9DC, 0), (0x5C5DA290, 2),
        (0x6600C6AB, 1), (0x736F4F29, 2), (0x77E68312, 1), (0x99897C43, 1), (0xBFD77429, 2),
        (0xD25D5A7B, 2)
    ],
    0x25B974A6: [  # nTimelineParam::Em111Motion
        (0x08FD20A6, 1), (0x99897C43, 1)
    ],
    0x2643266F: [  # nDraw::MaterialAnimation::FakeSphere_Mt
        (0x1BB0EB80, 2), (0x2BD52F93, 1), (0xA01B7821, 3)
    ],
    0x29AA3E2D: [  # nTimelineParam::nWwiseTimeline::EventGroup05
        (0x08431812, 1), (0x0F2EDC0B, 1), (0x7829EC9D, 1), (0x7F442884, 1), (0x96278DB1, 1),
        (0x99897C43, 1), (0x9F91C19A, 1), (0xBCFFC41B, 0), (0xE120BD27, 1), (0xE64D793E, 1)
    ],
    0x2A62F92E: [  # nEffect::nTimelineParam::ScaleAnim
        (0x2822A722, 2), (0x3A9708CC, 2), (0x909EC047, 2), (0xC24DF97C, 2)
    ],
    0x2A68E3BD: [  # nDraw::MaterialAnimation::SpeedTree_Mt
        (0x1BB0EB80, 2), (0x2BD52F93, 1), (0xA01B7821, 3)
    ],
    0x2B3E35D3: [  # nDraw::MaterialAnimation::EM111_Mt
        (0x0426764B, 2), (0x10272A04, 2), (0x2BD52F93, 1), (0x67201A92, 2), (0x744C82C4, 2),
        (0x9A42E3E8, 2), (0xED45D37E, 2)
    ],
    0x2B61B0ED: [  # nEffect::nTimelineParam::TypeBillboard2D
        (0x241CAED2, 2), (0x531B9E44, 2)
    ],
    0x2BD2762F: [  # nTimelineParam::Em023Motion
        (0x08FD20A6, 1), (0x99897C43, 1), (0xC56C2A11, 2)
    ],
    0x2BDA85F5: [  # nEffect::nTimelineParam::Velocity2D
        (0x31182E0D, 2)
    ],
    0x2C78B827: [  # nDraw::MaterialAnimation::VFX_Tornado_Mt
        (0x1BB0EB80, 2), (0x2BD52F93, 1), (0x4313B645, 2), (0x53B6808F, 2), (0x67D78EA5, 2),
        (0x840FFF51, 2), (0x9F326571, 2), (0xA01B7821, 3), (0xA19FD528, 2), (0xFA223F53, 2),
        (0xFD480BC4, 2)
    ],
    0x2CB44AB6: [  # nTimelineParam::Em106Motion
        (0x08FD20A6, 1), (0x99897C43, 1)
    ],
    0x2EC7FA34: [  # nTimelineParam::nWwiseTimeline::EventGroup01
        (0x08431812, 1), (0x0F2EDC0B, 1), (0x7829EC9D, 1), (0x7F442884, 1), (0x96278DB1, 1),
        (0x99897C43, 1), (0x9F91C19A, 1), (0xBCFFC41B, 0), (0xE120BD27, 1), (0xE64D793E, 1)
    ],
    0x2EE27B06: [  # nDraw::MaterialAnimation::EM100_Mt
        (0x1BB0EB80, 2), (0x2BD52F93, 1), (0x84751FE5, 2), (0x8633A1BC, 2), (0xA01B7821, 3),
        (0xF134912A, 2), (0xF3722F73, 2)
    ],
    0x2F9878D5: [  # nTimelineParam::Em063Motion
        (0x08FD20A6, 1), (0x504F8D1C, 2), (0x99897C43, 1)
    ],
    0x30213175: [  # nTimelineParam::Em118Motion
        (0x99897C43, 1)
    ],
    0x30891F6A: [  # nDraw::MaterialAnimation::EM057_Mt
        (0x0426764B, 2), (0x1BB0EB80, 2), (0x241F9852, 2), (0x2BD52F93, 1), (0x9A42E3E8, 2),
        (0xA01B7821, 3), (0xBD16C9E8, 2), (0xF9120603, 2)
    ],
    0x30A36F97: [  # nTimelineParam::nWwiseTimeline::EventGroup06
        (0x08431812, 1), (0x0F2EDC0B, 1), (0x7829EC9D, 1), (0x7F442884, 1), (0x96278DB1, 1),
        (0x99897C43, 1), (0x9F91C19A, 1), (0xBCFFC41B, 0), (0xE120BD27, 1), (0xE64D793E, 1)
    ],
    0x327A81AC: [  # nEffect::nTimelineParam::TypeBillboard3D
        (0x0EBAEC37, 2), (0x241CAED2, 2), (0x2FF50558, 2), (0x531B9E44, 2), (0x58689812, 3),
        (0x9F1E012E, 2), (0xC216C23D, 3)
    ],
    0x32C1B4B4: [  # nEffect::nTimelineParam::Velocity3D
        (0x31182E0D, 2), (0x6A5FE3C4, 2)
    ],
    0x32FA69E8: [  # nTimelineParam::AnimalSeasonEvent
        (0x08FD20A6, 1)
    ],
    0x33185295: [  # nEffect::nTimelineParam::EmitterShape3D
        (0x01080DD5, 2), (0x0718D2B3, 2), (0x1383E9BA, 2), (0x6484D92C, 2), (0x760F3D43, 2),
        (0x8A8AB800, 2), (0x98015C6F, 2), (0x9E118309, 2)
    ],
    0x33C620F4: [  # nDraw::MaterialAnimation::FakeRefraction_Mt
        (0x1BB0EB80, 2), (0x2BD52F93, 1), (0xA01B7821, 3)
    ],
    0x3481666B: [  # nEffect::nTimelineParam::TypePlane
        (0x0EBAEC37, 2), (0x531B9E44, 2), (0x58689812, 3), (0x9F1E012E, 2)
    ],
    0x37CEAB8E: [  # nTimelineParam::nWwiseTimeline::EventGroup02
        (0x08431812, 1), (0x0F2EDC0B, 1), (0x7829EC9D, 1), (0x7F442884, 1), (0x96278DB1, 1),
        (0x99897C43, 1), (0x9F91C19A, 1), (0xBCFFC41B, 0), (0xE120BD27, 1), (0xE64D793E, 1)
    ],
    0x38FF82EA: [  # nTimelineParam::CollisionTimelineObject
        (0x08FD20A6, 1), (0x80A0A8E4, 1), (0x8DA66231, 1), (0x8F2A4EA9, 0), (0x94BD5370, 1),
        (0x96317FE8, 0), (0x99897C43, 1), (0x99BB99A5, 1), (0xA4071D6A, 0), (0xA68B31F2, 1),
        (0xAB8DFB27, 1), (0xB296CA66, 1), (0xBD1C2C2B, 0), (0xBF9000B3, 1), (0xC06BD86E, 0),
        (0xC2E7F4F6, 1), (0xCFE13E23, 1), (0xD6FA0F62, 1), (0xD970E92F, 0), (0xDBFCC5B7, 1),
        (0xE4CC6DE0, 1), (0xEB468BAD, 0), (0xF0D19674, 1), (0xF25DBAEC, 0), (0xFDD75CA1, 1)
    ],
    0x399DB6A9: [  # nDraw::MaterialAnimation::VFX_Flood_Mt
        (0x09956BA2, 2), (0x0BD3D5FB, 2), (0x2BD52F93, 1), (0x634D2DC4, 2), (0x71BCF0AA, 2),
        (0x7F503103, 2), (0x8F198226, 2), (0x915E502F, 2), (0xC207B8B4, 2), (0xFA223F53, 2),
        (0xFD480BC4, 2)
    ],
    0x39C68FB4: [  # nEffect::nTimelineParam::TubeLight
        (0x085BC9D5, 2), (0x2AA40DE9, 3), (0x316D89C5, 2), (0x3BA67E7C, 3), (0xB6C0BDF8, 2)
    ],
    0x3AC1EACA: [  # nDraw::MaterialAnimation::Uber_Mt
        (0x1BB0EB80, 2), (0x2BD52F93, 1), (0x66DAFF30, 3), (0x8F198226, 2), (0xA01B7821, 3),
        (0xAFE95AC0, 3)
    ],
    0x3B2B5B9F: [  # nTimelineParam::Em104Motion
        (0x08FD20A6, 1), (0x6600C6AB, 1), (0x736F4F29, 2), (0x99897C43, 1), (0xD25D5A7B, 2)
    ],
    0x3BF74053: [  # nDraw::MaterialAnimation::BTK001_Mt
        (0x0426764B, 2), (0x10272A04, 2), (0x1BB0EB80, 2), (0x2BD52F93, 1), (0x2E710D12, 2),
        (0x67201A92, 2), (0x744C82C4, 2), (0x8F198226, 2), (0x9A42E3E8, 2), (0xAFE95AC0, 3),
        (0xED45D37E, 2)
    ],
    0x3C57D4E8: [  # nDraw::MaterialAnimation::EM103_Mt
        (0x02571B8D, 2), (0x0426764B, 2), (0x10272A04, 2), (0x1D1A7784, 2), (0x2BD52F93, 1),
        (0x2E710D12, 2), (0x48EF8D8E, 2), (0x63AB0B39, 2), (0x67201A92, 2), (0x744C82C4, 2),
        (0x80983795, 2), (0x8413263E, 2), (0x9A42E3E8, 2), (0xAFE95AC0, 3), (0xC9C0F038, 2),
        (0xE35A3690, 3), (0xED45D37E, 2), (0xF31416A8, 2), (0xF9120603, 2), (0xFE294B28, 2)
    ],
    0x3CA9626C: [  # nTimelineParam::Em123Motion
        (0x08FD20A6, 1), (0x890766C1, 2), (0x99897C43, 1)
    ],
    0x3E6BDB12: [  # nTimelineParam::ModelPartsCtrl
        (0x0297CFC9, 0), (0x05FA0BD0, 0), (0x08FD20A6, 1), (0x671F5908, 0), (0x72FD3B46, 0),
        (0x7590FF5F, 0), (0x89113824, 0), (0x8E7CFC3D, 0), (0xEBF46AFC, 0), (0xF97BCCAB, 0),
        (0xFE1608B2, 0)
    ],
    0x3E880466: [  # nDraw::MaterialAnimation::VFX_Water_Mt
        (0x0FF5554F, 3), (0x2BD52F93, 1), (0x56B7B840, 2), (0xFA223F53, 2)
    ],
    0x3F207E3C: [  # nDraw::MaterialAnimation::FakeInnerEmit_Mt
        (0x1BB0EB80, 2), (0x2BD52F93, 1), (0xA01B7821, 3)
    ],
    0x40793BDC: [  # nDraw::MaterialAnimation::TMG001_Mt
        (0x2BD52F93, 1), (0x84751FE5, 2), (0x8633A1BC, 2), (0xF134912A, 2), (0xF3722F73, 2)
    ],
    0x40C99B18: [  # nTimelineParam::nWwiseTimeline::EventGroup03
        (0x08431812, 1), (0x0F2EDC0B, 1), (0x7829EC9D, 1), (0x7F442884, 1), (0x96278DB1, 1),
        (0x99897C43, 1), (0x9F91C19A, 1), (0xBCFFC41B, 0), (0xE120BD27, 1), (0xE64D793E, 1)
    ],
    0x40DBFBE3: [  # nTimelineParam::nWwiseTimeline::EventGroup10
        (0x08431812, 1), (0x0F2EDC0B, 1), (0x7829EC9D, 1), (0x7F442884, 1), (0x96278DB1, 1),
        (0x99897C43, 1), (0xBCFFC41B, 0), (0xE120BD27, 1), (0xE64D793E, 1)
    ],
    0x42E48DDE: [  # nEffect::nTimelineParam::PointLightBehavior
        (0x435F3054, 2), (0x58689812, 3), (0x94BCC5CE, 2), (0xC32F9493, 2)
    ],
    0x45CB6C2B: [  # nTimelineParam::Ems005_01Motion
        (0x08FD20A6, 1)
    ],
    0x460700DC: [  # nDraw::MaterialAnimation::SKM001_Mt
        (0x0426764B, 2), (0x0E424A70, 2), (0x10272A04, 2), (0x1BB0EB80, 2), (0x2BD52F93, 1),
        (0x2F5A867F, 2), (0x67201A92, 2), (0x80983795, 2), (0x84751FE5, 2), (0x8633A1BC, 2),
        (0x9A42E3E8, 2), (0xA01B7821, 3), (0xC8D913CA, 2), (0xE540C280, 2), (0xF134912A, 2),
        (0xF3722F73, 2), (0xF7C55D41, 2), (0xFE294B28, 2)
    ],
    0x465ACF70: [  # nDraw::MaterialAnimation::VFX_DistDisp_Mt
        (0x15EC38EA, 2), (0x2BD52F93, 1), (0x56B7B840, 2), (0x952B4933, 2), (0xC2F1EB55, 2),
        (0xF3D25004, 2)
    ],
    0x4669419C: [  # nTimelineParam::Em117Motion
        (0x08FD20A6, 1), (0x0CB6228E, 2), (0x1892CC87, 2), (0x2A83ABD7, 0), (0x3FC9E0B8, 0),
        (0x69D119C8, 2), (0x99897C43, 1), (0x9DE5F095, 2), (0xA31F6F44, 2), (0xC6785402, 2),
        (0xCD9E0D40, 0), (0xD8D4462F, 0)
    ],
    0x46F3E901: [  # nTimelineParam::PlMotionVisual
        (0x08FD20A6, 1), (0x0CFE3227, 2), (0x7BF902B1, 2), (0x99897C43, 1), (0xAB3C8B55, 0),
        (0xC29F20E0, 2)
    ],
    0x47A45F01: [  # nTimelineParam::nWwiseTimeline::EventGroup07
        (0x08431812, 1), (0x0F2EDC0B, 1), (0x7829EC9D, 1), (0x7F442884, 1), (0x96278DB1, 1),
        (0x99897C43, 1), (0x9F91C19A, 1), (0xBCFFC41B, 0), (0xE120BD27, 1), (0xE64D793E, 1)
    ],
    0x48067636: [  # nDraw::MaterialAnimation::GenericMaterial
        (0x1BB0EB80, 2), (0x2BD52F93, 1), (0x8F198226, 2), (0xA01B7821, 3), (0xAFE95AC0, 3)
    ],
    0x48D6114F: [  # nTimelineParam::PlMotionCommon
        (0x00D534F9, 1), (0x03840ECE, 1), (0x06A9B550, 2), (0x08FD20A6, 1), (0x0EA20FED, 0),
        (0x11E4E7B1, 0), (0x13495749, 2), (0x1D7D5399, 2), (0x1E090F48, 1), (0x1E73EAB4, 0),
        (0x252961CD, 0), (0x254ECDBF, 2), (0x274D4F11, 2), (0x2AC902E3, 2), (0x2B078267, 2),
        (0x2BF81C73, 2), (0x2C07B96F, 2), (0x2CFC1613, 0), (0x3033C6D8, 0), (0x416522B1, 2),
        (0x41E07A9B, 2), (0x4258D1A6, 0), (0x444D2CFC, 2), (0x4BD36121, 0), (0x4DA1831A, 1),
        (0x4F4B4BA4, 2), (0x5B0089F9, 2), (0x5C00B2F1, 2), (0x5E6EC814, 2), (0x6159A4F6, 2),
        (0x648CADCE, 0), (0x67921A5E, 0), (0x6DD60D01, 0), (0x6E63FBC7, 1), (0x71AE85C6, 2),
        (0x75B22732, 2), (0x7747F0BC, 2), (0x806D9AEB, 1), (0x84745897, 0), (0x859AB9C4, 0),
        (0x85A14934, 1), (0x86859DBE, 0), (0x86C1A86E, 2), (0x8F57C5DA, 2), (0x9748E46B, 2),
        (0x99897C43, 1), (0x9D58B92F, 2), (0x9F7B1E77, 2), (0xA26628EB, 0), (0xADC3C03B, 2),
        (0xAEDA6B35, 0), (0xB2AE1E00, 2), (0xB3371B60, 0), (0xB458B56D, 2), (0xB50EE8D5, 2),
        (0xB83FEC3D, 2), (0xBA3F398C, 2), (0xC4774EC6, 2), (0xC865FF9E, 2), (0xD1053969, 0),
        (0xD326A335, 0), (0xD48D47B0, 0), (0xE8A7D47C, 2), (0xEEDF77D0, 0), (0xF3DA8324, 2),
        (0xF4031A77, 2), (0xF4D12F36, 0), (0xF76AAA7D, 1), (0xF81DE7F9, 0), (0xF850F54C, 2)
    ],
    0x48E6467F: [  # nTimelineParam::Em127Motion
        (0x08FD20A6, 1), (0x8A3D617C, 1), (0x91B8DFC5, 2), (0x99897C43, 1)
    ],
    0x49DFD557: [  # nTimelineParam::PugeeMotion
        (0x08FD20A6, 1)
    ],
    0x4BCA741C: [  # nTimelineParam::Em042Motion
        (0x08FD20A6, 1)
    ],
    0x4C119332: [  # nTimelineParam::cCharaMotion
        (0x08FD20A6, 1)
    ],
    0x4D111433: [  # nEffect::nTimelineParam::Transform3D
        (0x1F0BDACF, 2), (0x60849F2A, 2), (0x7A88BE0F, 2), (0x86028B75, 2), (0x8E8AFE06, 2),
        (0x9486DF23, 2), (0xE381EFB5, 2), (0xF105BBE3, 2), (0xF98DCE90, 2)
    ],
    0x4E64D91C: [  # nDraw::MaterialAnimation::Burn_Mt
        (0x1BB0EB80, 2), (0x2BD52F93, 1), (0x7154FE35, 2), (0x9F5A9F19, 2), (0xAFE95AC0, 3),
        (0xE85DAF8F, 2)
    ],
    0x4FB76028: [  # nDraw::MaterialAnimation::EM002_Mt
        (0x1BB0EB80, 2), (0x2BD52F93, 1), (0x84751FE5, 2), (0x8633A1BC, 2), (0xA01B7821, 3),
        (0xF134912A, 2), (0xF3722F73, 2)
    ],
    0x4FF9141E: [  # nDraw::MaterialAnimation::EM_Mt
        (0x1BB0EB80, 2), (0x2BD52F93, 1), (0x4EDC6CE0, 2), (0xA01B7821, 3), (0xD0B8F943, 2),
        (0xF7C55D41, 2)
    ],
    0x52CCD16F: [  # nTimelineParam::Em063_05Motion
        (0x99897C43, 1)
    ],
    0x538AF627: [  # nEffect::nTimelineParam::TypeMesh
        (0x002FF505, 2), (0x0EBAEC37, 2), (0x18C577DE, 2), (0x241CAED2, 2), (0x531B9E44, 2),
        (0x58689812, 3), (0x608DCF8D, 3), (0x7728C593, 2), (0x9F1E012E, 2), (0xCA12CFFE, 2),
        (0xEE219429, 2)
    ],
    0x53EA348C: [  # nDraw::MaterialAnimation::EM109_Mt
        (0x1BB0EB80, 2), (0x2BD52F93, 1), (0x51EBFAC2, 2), (0xBFE59BEE, 2), (0xDB7DD336, 2),
        (0xF9120603, 2)
    ],
    0x540A2572: [  # nEffect::nTimelineParam::Transform2D
        (0x8E8AFE06, 2), (0x9486DF23, 2), (0xE381EFB5, 2), (0xF98DCE90, 2)
    ],
    0x54800017: [  # nTimelineParam::EmCreateGmMotion
        (0x825101E5, 2), (0x99897C43, 1)
    ],
    0x55585B25: [  # nTimelineParam::Em057Motion
        (0x08FD20A6, 1), (0x99897C43, 1)
    ],
    0x55CEE362: [  # nDraw::MaterialAnimation::EM080_Mt
        (0x0426764B, 2), (0x10272A04, 2), (0x2BD52F93, 1), (0x2E710D12, 2), (0x67201A92, 2),
        (0x744C82C4, 2), (0x8F198226, 2), (0x9A42E3E8, 2), (0xAFE95AC0, 3), (0xED45D37E, 2)
    ],
    0x56367A59: [  # nDraw::MaterialAnimation::EM118_Mt
        (0x0426764B, 2), (0x1BB0EB80, 2), (0x2BD52F93, 1), (0x744C82C4, 2), (0x7C692062, 2),
        (0x84751FE5, 2), (0x8633A1BC, 2), (0x93F18F03, 2), (0x9A42E3E8, 2), (0xA01B7821, 3),
        (0xC56404BB, 2), (0xD6010C27, 2), (0xE0341C9D, 2), (0xE540C280, 2), (0xED45D37E, 2),
        (0xF9120603, 2)
    ],
    0x563C8065: [  # nEffect::nTimelineParam::RotateAnim
        (0x2C3187EA, 2), (0xB538D650, 2), (0xC23FE6C6, 2), (0xE81961E4, 2)
    ],
    0x566DF526: [  # nDraw::MaterialAnimation::Flow_Dir_Mt
        (0x1BB0EB80, 2), (0x2BD52F93, 1), (0x3CA9C405, 2), (0x8F198226, 2), (0x93315130, 2),
        (0xA01B7821, 3), (0xAFE95AC0, 3)
    ],
    0x571B4290: [  # nTimelineParam::nWwiseTimeline::EventGroup08
        (0x08431812, 1), (0x0F2EDC0B, 1), (0x7829EC9D, 1), (0x7F442884, 1), (0x96278DB1, 1),
        (0x99897C43, 1), (0xBCFFC41B, 0), (0xE120BD27, 1), (0xE64D793E, 1)
    ],
    0x5752ED69: [  # nDraw::MaterialAnimation::VFX_DispWave_Mt
        (0x2BD52F93, 1), (0x2FD8A1F4, 2), (0x48EF8D8E, 2), (0xA23E2927, 2), (0xAFE95AC0, 3),
        (0xC1D6C0D8, 2), (0xE2FD723B, 2), (0xFA223F53, 2)
    ],
    0x575E6887: [  # nTimelineParam::OtomoMotion
        (0x04F09E88, 1), (0x08FD20A6, 1), (0x965A5CA7, 1), (0x99897C43, 1)
    ],
    0x582BA062: [  # nEffect::nTimelineParam::RadialBlurFilterBehavior
        (0x0ECBFA29, 2), (0x0EF6ABF4, 2), (0x1D95BB54, 2), (0x58689812, 3), (0xAB9D6334, 2)
    ],
    0x58FB6EA5: [  # nTimelineParam::Em102Motion
        (0x99897C43, 1)
    ],
    0x598272E1: [  # nMhEffect::nTimelineParam::PlEmissive
        (0x8BF31826, 2), (0x94BCC5CE, 2), (0x95A3A1D3, 2), (0x9B446023, 2), (0xAC635CA9, 2),
        (0xEC4350B5, 2), (0xF09920EC, 2), (0xFA79B1CD, 3)
    ],
    0x59C0CAA2: [  # nTimelineParam::nWwiseTimeline::EventGroup00
        (0x08431812, 1), (0x0F2EDC0B, 1), (0x7829EC9D, 1), (0x7F442884, 1), (0x96278DB1, 1),
        (0x99897C43, 1), (0x9F91C19A, 1), (0xE120BD27, 1), (0xE64D793E, 1)
    ],
    0x5AC7FC29: [  # nEffect::nTimelineParam::UVSequence
        (0x33A4A86B, 2), (0xE5C92264, 2)
    ],
    0x5AFC3A5F: [  # nTimelineParam::Em109Motion
        (0x526AF201, 0), (0xECC55CED, 0)
    ],
    0x5B40BF31: [  # nDraw::MaterialAnimation::EM124_Mt
        (0x0426764B, 2), (0x10272A04, 2), (0x2BD52F93, 1), (0x67201A92, 2), (0x744C82C4, 2),
        (0x9A42E3E8, 2), (0xED45D37E, 2)
    ],
    0x5C648E63: [  # nTimelineParam::ShellCreate
        (0x08FD20A6, 1), (0x24579AAB, 1), (0x2E2C1393, 2), (0x99897C43, 1), (0xB7254229, 2),
        (0xBE4DB7FA, 1)
    ],
    0x5E8D9EE9: [  # nDraw::MaterialAnimation::VFX_EmissiveFog_Mt
        (0x1BB0EB80, 2), (0x2285686D, 2), (0x2BD52F93, 1), (0x4AAAF206, 2), (0x8F1479C1, 2),
        (0xA01B7821, 3), (0xAFE95AC0, 3), (0xC207B8B4, 2), (0xF73B4573, 2)
    ],
    0x5E953EE4: [  # nDraw::MaterialAnimation::EM100_01_Mt
        (0x01DBBF61, 2), (0x0426764B, 2), (0x10272A04, 2), (0x2BD52F93, 1), (0x67201A92, 2),
        (0x744C82C4, 2), (0x98D2EEDB, 2), (0x9A42E3E8, 2), (0xB8895311, 2), (0xE87AFFF5, 2),
        (0xED45D37E, 2)
    ],
    0x5EAD0EBB: [  # nTimelineParam::nWwiseTimeline::EventGroup04
        (0x08431812, 1), (0x0F2EDC0B, 1), (0x7829EC9D, 1), (0x7F442884, 1), (0x96278DB1, 1),
        (0x99897C43, 1), (0xBCFFC41B, 0), (0xE120BD27, 1), (0xE64D793E, 1)
    ],
    0x5F02205C: [  # nTimelineParam::ShellAnimation
        (0x0074A898, 2), (0x08FD20A6, 1), (0x64264EA4, 2), (0x69A80037, 0), (0x99897C43, 1),
        (0x9E2973C7, 2), (0xD278F57F, 2)
    ],
    0x5F32B536: [  # nDraw::MaterialAnimation::OZK001_Mt
        (0x1BB0EB80, 2), (0x2BD52F93, 1), (0xA01B7821, 3)
    ],
    0x5F456B89: [  # nDraw::MaterialAnimation::PL_Mt
        (0x1BB0EB80, 2), (0x2BD52F93, 1)
    ],
    0x5F795756: [  # nTimelineParam::Em125Motion
        (0x08FD20A6, 1)
    ],
    0x5F9D523C: [  # nTimelineParam::Em027Motion
        (0x99897C43, 1)
    ],
    0x601E4A28: [  # nTimelineParam::Em116Motion
        (0x08FD20A6, 1), (0x99897C43, 1), (0xCBAF549E, 0)
    ],
    0x60BA9117: [  # nEffect::nTimelineParam::RgbFire
        (0x39A1E557, 3), (0x5A8C6820, 3), (0x9F1E012E, 2)
    ],
    0x62F3F970: [  # nTimelineParam::nWwiseTimeline::EventCollision02
        (0x08FD20A6, 1), (0x15EF861B, 1), (0x1C59CA30, 1), (0x3A97A3D6, 1), (0x3DFA67CF, 1),
        (0x4AFD5759, 1), (0x4D909340, 1), (0x6B5EFAA6, 1), (0x6C333EBF, 1), (0x823D5F93, 1),
        (0x85509B8A, 1), (0xA39EF26C, 1), (0xA4F33675, 1), (0xD3F406E3, 1), (0xDA424AC8, 1),
        (0xF257AB1C, 1), (0xF53A6F05, 1)
    ],
    0x63FCD854: [  # nDraw::MaterialAnimation::EM125_Mt
        (0x02571B8D, 2), (0x164DB124, 3), (0x18CC46B5, 3), (0x2BD52F93, 1), (0x371BBC04, 2),
        (0x43AC6AB4, 2), (0x48EF8D8E, 2), (0x5297382F, 2), (0x605428A3, 2), (0x708B010C, 2),
        (0x744C82C4, 2), (0x84751FE5, 2), (0x8633A1BC, 2), (0xA01B7821, 3), (0xA4C789DC, 3),
        (0xB83AF0BE, 2), (0xBFE81475, 3), (0xC39F54D7, 2), (0xC9C0F038, 2), (0xD3137725, 2),
        (0xE0341C9D, 2), (0xED45D37E, 2)
    ],
    0x64A758BE: [  # nTimelineParam::Em111_05Motion
        (0x5F8AD46D, 2)
    ],
    0x65004E2A: [  # nEffect::nTimelineParam::MhEffectDecalBehavior
        (0x16814F1C, 2), (0x26BD5CC2, 3), (0x3775827A, 2), (0x4072B2EC, 2), (0x4D41A06B, 2),
        (0x7D235C30, 2), (0x831B390B, 2), (0xAE7CD3C0, 2), (0xBF160652, 2), (0xCBDB6622, 3)
    ],
    0x658D8235: [  # nTimelineParam::EmClawRejectCollisionObject
        (0x08FD20A6, 1), (0x0CECC08D, 0), (0x27C1934E, 0), (0x3EDAA20F, 0), (0x43AD564A, 0),
        (0x5AB6670B, 0), (0x68800589, 0), (0x70D64E7E, 0), (0x719B34C8, 0)
    ],
    0x66C62149: [  # nDraw::MaterialAnimation::VFX_Ice_Mt
        (0x08570195, 2), (0x0DDE7E84, 2), (0x1BB0EB80, 2), (0x2BD52F93, 1), (0x637622C1, 3),
        (0x987DC45A, 2), (0xA01B7821, 3), (0xF1357D01, 2), (0xFD480BC4, 2)
    ],
    0x69137438: [  # nTimelineParam::Em101Motion
        (0x32F86046, 0), (0x99897C43, 1)
    ],
    0x6B32DCF6: [  # nTimelineParam::EffectParameter1
        (0x04AF3E55, 4), (0x0A74B667, 4), (0x0D19727E, 4), (0x0D2FAA31, 1), (0x136F8726, 4),
        (0x1402433F, 4), (0x1DB40F14, 4), (0x2159E5A4, 4), (0x216F3DEB, 1), (0x2602F9F2, 1),
        (0x263421BD, 4), (0x2F826D96, 4), (0x3842D4E5, 4), (0x38740CAA, 1), (0x3F19C8B3, 1),
        (0x481EF825, 1), (0x4F45E473, 4), (0x4F733C3C, 1), (0x5105C964, 1), (0x5133112B, 4),
        (0x565ED532, 4), (0x56680D7D, 1), (0x58855D00, 4), (0x630573A9, 4), (0x6468B7B0, 4),
        (0x6AB33F82, 4), (0x73A80EC3, 4), (0x7A1E42E8, 4), (0x7A289AA7, 1), (0x7D7386F1, 4),
        (0x8A66D69C, 4), (0x8D0B1285, 4), (0x8D3DCACA, 1), (0x934B3F92, 1), (0x937DE7DD, 4),
        (0x941023C4, 4), (0x9426FB8B, 1), (0x9AFD73B9, 1), (0xA17D5D10, 1), (0xA6109909, 1),
        (0xA8CB113B, 1), (0xB1D0207A, 1), (0xB850B41E, 4), (0xB8666C51, 1), (0xBF0BA848, 1),
        (0xC6D710EC, 1), (0xC80C98DE, 1), (0xCF578488, 4), (0xCF615CC7, 1), (0xD117A99F, 1),
        (0xD67A6D86, 1), (0xDFCC21AD, 1), (0xE3171352, 4), (0xE321CB1D, 1), (0xE44C0F04, 1),
        (0xE47AD74B, 4), (0xEDFA432F, 1), (0xFA0C2213, 4), (0xFA3AFA5C, 1), (0xFD61E60A, 4)
    ],
    0x6D4CF8C4: [  # nTimelineParam::Em115_05Motion
        (0x08FD20A6, 1)
    ],
    0x6DA6E5D1: [  # nEffect::nTimelineParam::MhPointLightBehavior
        (0x435F3054, 2), (0x58689812, 3), (0x94BCC5CE, 2), (0xC32F9493, 2)
    ],
    0x6DBD7FA8: [  # nTimelineParam::Em043Motion
        (0x08FD20A6, 1), (0x99897C43, 1), (0xD5D93660, 1)
    ],
    0x6DC8D36C: [  # nTimelineParam::MatAnimPlayer
        (0x4625CE84, 0), (0x548DFF15, 0), (0x8D65C9C7, 0), (0x99897C43, 1)
    ],
    0x6E914DCB: [  # nTimelineParam::Em126Motion
        (0x08FD20A6, 1), (0x99897C43, 1)
    ],
    0x6FCCD10E: [  # nTimelineParam::PlMotionInput
        (0x08FD20A6, 1), (0x1E090F48, 1), (0x690E3FDE, 1), (0x6E63FBC7, 1), (0x806D9AEB, 1),
        (0xF0076E64, 1), (0xF76AAA7D, 1)
    ],
    0x70C7B1F1: [  # nDraw::MaterialAnimation::VFX_SandFall_Mt
        (0x0BD3D5FB, 2), (0x2BD52F93, 1), (0x7CD4E56D, 2), (0x92DA8441, 2), (0xEC6BF8FC, 2),
        (0xFD480BC4, 2)
    ],
    0x723B8D4C: [  # nTimelineParam::EffectParameter2
        (0x04AF3E55, 4), (0x0A74B667, 4), (0x0D19727E, 4), (0x0D2FAA31, 1), (0x136F8726, 4),
        (0x1402433F, 4), (0x1DB40F14, 4), (0x2159E5A4, 4), (0x216F3DEB, 1), (0x2602F9F2, 1),
        (0x38740CAA, 1), (0x3F19C8B3, 1), (0x481EF825, 1), (0x4F733C3C, 1), (0x5105C964, 1),
        (0x565ED532, 4), (0x56680D7D, 1), (0x630573A9, 4), (0x6468B7B0, 4), (0x6AB33F82, 4),
        (0x73A80EC3, 4), (0x7A1E42E8, 4), (0x7A289AA7, 1), (0x7D7386F1, 4), (0x8A66D69C, 4),
        (0x8D0B1285, 4), (0x937DE7DD, 4), (0x941023C4, 4), (0x9426FB8B, 1), (0xA17D5D10, 1),
        (0xA6109909, 1), (0xA8CB113B, 1), (0xB1D0207A, 1), (0xB850B41E, 4), (0xB8666C51, 1),
        (0xBF0BA848, 1), (0xC6D710EC, 1), (0xC80C98DE, 1), (0xCF578488, 4), (0xCF615CC7, 1),
        (0xD117A99F, 1), (0xD67A6D86, 1), (0xDFCC21AD, 1), (0xE3171352, 4), (0xE321CB1D, 1),
        (0xE47AD74B, 4), (0xFA0C2213, 4), (0xFD61E60A, 4)
    ],
    0x75963575: [  # nEffect::nTimelineParam::MhSpotLightBehavior
        (0x58689812, 3), (0x94BCC5CE, 2)
    ],
    0x76D8344E: [  # nDraw::MaterialAnimation::EMS_Mt
        (0x1BB0EB80, 2), (0x2BD52F93, 1), (0x325FCB10, 3), (0x53A3F45E, 2), (0x9A690877, 2),
        (0xA01B7821, 3), (0xAFE95AC0, 3), (0xC31CE9CF, 2), (0xE2142A34, 2)
    ],
    0x77519ACB: [  # nTimelineParam::EmMotionCommon
        (0x08FD20A6, 1), (0x2317001E, 0), (0x23EDA9F5, 1), (0x252961CD, 0), (0x26119EDA, 0),
        (0x2AC902E3, 2), (0x2C426079, 0), (0x2EF52DB5, 0), (0x310FD720, 0), (0x37484700, 2),
        (0x41E7D172, 1), (0x4D26ED7B, 0), (0x4D6470C6, 0), (0x5009B3C9, 0), (0x570F638E, 2),
        (0x5782A950, 1), (0x5C5DA290, 2), (0x5C918AD5, 0), (0x5D252E24, 0), (0x6159A4F6, 2),
        (0x62CEFAB3, 0), (0x670FA57B, 2), (0x6CC0B887, 2), (0x6E63FBC7, 1), (0x787E9FD5, 0),
        (0x7D6D165D, 0), (0x814568E2, 0), (0x8F57C5DA, 2), (0x94526271, 0), (0x99897C43, 1),
        (0x9B50DD3B, 1), (0x9BD476A9, 0), (0xA48C272D, 0), (0xB28DA0BE, 2), (0xB2AE1E00, 2),
        (0xB821F4A8, 2), (0xBBB2C830, 2), (0xBF8F9DFD, 0), (0xDD244F0E, 2), (0xF15CEF67, 2),
        (0xF850F54C, 2), (0xFCFE68B9, 0)
    ],
    0x77815B01: [  # nTimelineParam::Em114Motion
        (0x08FD20A6, 1)
    ],
    0x784BF2ED: [  # nDraw::MaterialAnimation::EM063_Mt
        (0x0426764B, 2), (0x1308E98E, 2), (0x1BB0EB80, 2), (0x2BD52F93, 1), (0x744C82C4, 2),
        (0x9A42E3E8, 2), (0xA01B7821, 3), (0xE35A3690, 3), (0xED45D37E, 2)
    ],
    0x790E5CE2: [  # nTimelineParam::Em124Motion
        (0x99897C43, 1), (0xB359AC53, 0), (0xF19AB8A2, 2)
    ],
    0x791C240E: [  # nTimelineParam::PhotomoCommon
        (0x08FD20A6, 1), (0x2FD81AE9, 0), (0x52EC2264, 0), (0x5846C819, 0), (0x5FF25223, 0),
        (0x99897C43, 1), (0xB9DB9967, 0), (0xC0E5BF09, 0), (0xF3658026, 0)
    ],
    0x79746285: [  # nTimelineParam::EmMotionVisual
        (0x08FD20A6, 1)
    ],
    0x79EA5988: [  # nTimelineParam::Em026Motion
        (0x08FD20A6, 1)
    ],
    0x7BFAA8CA: [  # nTimelineParam::nWwiseTimeline::EventCollision01
        (0x08FD20A6, 1), (0x1213267E, 1), (0x1C59CA30, 1), (0x3A97A3D6, 1), (0x3DFA67CF, 1),
        (0x4AFD5759, 1), (0x4D909340, 1), (0x6069E6F0, 1), (0x6B5EFAA6, 1), (0x6C333EBF, 1),
        (0x823D5F93, 1), (0x85509B8A, 1), (0x8B1A77C4, 1), (0xA39EF26C, 1), (0xA4F33675, 1),
        (0xD3F406E3, 1), (0xF257AB1C, 1), (0xF53A6F05, 1), (0xF960B74A, 1)
    ],
    0x7D3BAE11: [  # nDraw::MaterialAnimation::FakeEye_Mt
        (0x02571B8D, 2), (0x1BB0EB80, 2), (0x2BD52F93, 1), (0x371BBC04, 2), (0x8F198226, 2),
        (0xA01B7821, 3), (0xAFE95AC0, 3), (0xB8BFBF9E, 2), (0xD245B590, 2)
    ],
    0x7E51F5BD: [  # nTimelineParam::LightTimelineParam
        (0x13804BC9, 2), (0x4279F094, 2), (0x9BE2D228, 3), (0xB52636D6, 2)
    ],
    0x7E68607B: [  # nTimelineParam::Em001Motion
        (0x08FD20A6, 1), (0x69D5D953, 0), (0x97876D34, 0)
    ],
    0x7E8C6511: [  # nTimelineParam::Em103Motion
        (0x99897C43, 1)
    ],
    0x7E9DBB98: [  # nTimelineParam::ShellMultiCreate
        (0x032F9113, 1), (0x03A7C305, 2), (0x03AC6A9E, 2), (0x03D46071, 2), (0x0442550A, 1),
        (0x04B9A468, 2), (0x04C1AE87, 2), (0x04CA071C, 2), (0x06D97C03, 2), (0x0802F431, 2),
        (0x08095DAA, 2), (0x08FD20A6, 1), (0x0A3ECD23, 2), (0x0D53093A, 2), (0x0D7C4B37, 2),
        (0x0F6499B3, 2), (0x0F6F3028, 2), (0x11126CEB, 2), (0x1119C570, 2), (0x1382EC79, 1),
        (0x1448387B, 2), (0x146CD3ED, 2), (0x16740169, 2), (0x167FA8F2, 2), (0x1A34A052, 1),
        (0x1AB75BDF, 2), (0x1ABCF244, 2), (0x1D59644B, 1), (0x1DD1365D, 2), (0x1DDA9FC6, 2),
        (0x1FC9E4D9, 2), (0x20E89CC5, 0), (0x2212F5DA, 2), (0x22195C41, 2), (0x223DB7D7, 2),
        (0x257F31C3, 2), (0x27D7D87B, 2), (0x290C5049, 2), (0x2B8BFBFC, 2), (0x2BDE22C1, 0),
        (0x2CB3E6D8, 0), (0x2CE63FE5, 2), (0x2D7DE338, 1), (0x2E619450, 2), (0x30176108, 2),
        (0x32C51380, 0), (0x330B1660, 1), (0x3466D279, 1), (0x35FD0EA4, 2), (0x377AA511, 2),
        (0x393BCA4E, 2), (0x39F3AD84, 0), (0x3B026D00, 2), (0x3B09C49B, 2), (0x3C6FA919, 2),
        (0x3E560E57, 2), (0x3E9E699D, 0), (0x407D9587, 2), (0x42FA3E32, 2), (0x4361E2EF, 1),
        (0x440C26F6, 1), (0x45C22316, 0), (0x4710519E, 2), (0x49513EC1, 2), (0x4999590B, 0),
        (0x4B68998F, 2), (0x4C055D96, 2), (0x4C0EF40D, 2), (0x4E3CFAD8, 2), (0x4EF49D12, 0),
        (0x50D0E8ED, 2), (0x52780155, 2), (0x5515C54C, 2), (0x551E6CD7, 2), (0x553A8741, 2),
        (0x57EFAC53, 0), (0x5966A4C6, 2), (0x5A7AD3AE, 1), (0x5BB4D64E, 0), (0x5BE10F73, 2),
        (0x5C8CCB6A, 2), (0x5CD91257, 0), (0x5E0B60DF, 2), (0x617331FF, 2), (0x61789864, 2),
        (0x634F08ED, 2), (0x636BE37B, 2), (0x6485DCEF, 1), (0x66155C7D, 2), (0x661EF5E6, 2),
        (0x68CED44F, 2), (0x6A5E54DD, 1), (0x6AD606CB, 2), (0x6ADDAF50, 2), (0x6D3390C4, 1),
        (0x6DB06B49, 2), (0x6DBBC2D2, 2), (0x6F89CC07, 2), (0x71DE4C95, 2), (0x7345659C, 1),
        (0x73BE94FE, 2), (0x73C69E11, 2), (0x73CD378A, 2), (0x7428A185, 1), (0x74A0F393, 2),
        (0x74AB5A08, 2), (0x74D350E7, 2), (0x7863A925, 2), (0x786800BE, 2), (0x7A5439AC, 2),
        (0x7A7B7BA1, 2), (0x7D39FDB5, 2), (0x7F05C4A7, 2), (0x7F0E6D3C, 2), (0x833DF1E8, 1),
        (0x83B5A3FE, 2), (0x83BE0A65, 2), (0x845035F1, 1), (0x84D3CE7C, 2), (0x881094CA, 2),
        (0x881B3D51, 2), (0x8A2CADD8, 2), (0x8D4169C1, 2), (0x8F76F948, 2), (0x91000C10, 2),
        (0x910BA58B, 2), (0x93379C99, 2), (0x945A5880, 2), (0x96666192, 2), (0x99897C43, 1),
        (0x9A81D0B2, 2), (0x9AA53B24, 2), (0x9AAE92BF, 2), (0x9ADD31CB, 2), (0x9D4B04B0, 1),
        (0x9DB0F5D2, 2), (0x9DC356A6, 2), (0xA0FAFC3E, 0), (0xA2009521, 2), (0xA20B3CBA, 2),
        (0xA3B40BF1, 1), (0xA566F8A3, 2), (0xA56D5138, 2), (0xA75F5FED, 2), (0xA7973827, 0),
        (0xA91E30B2, 2), (0xAA0247DA, 1), (0xABCC423A, 0), (0xACA18623, 0), (0xACD0B488, 2),
        (0xACF45F1E, 2), (0xAD6F83C3, 1), (0xAE21740C, 0), (0xB00501F3, 2), (0xB282AA46, 2),
        (0xB2D7737B, 0), (0xB319769B, 1), (0xB474B282, 1), (0xB5BAB762, 0), (0xB5C02C52, 2),
        (0xB5EF6E5F, 2), (0xB768C5EA, 2), (0xB9E1CD7F, 0), (0xBB100DFB, 2), (0xBB1BA460, 2),
        (0xBC0CFB49, 0), (0xBC766079, 2), (0xBC7DC9E2, 2), (0xBE8C0966, 0), (0xC06FF57C, 2),
        (0xC2BD87F4, 0), (0xC2C71CC4, 2), (0xC2E85EC9, 2), (0xC3738214, 1), (0xC41E460D, 1),
        (0xC580048C, 2), (0xC5859AD0, 2), (0xC5D043ED, 0), (0xC7023165, 2), (0xC98B39F0, 0),
        (0xCB0BCBDF, 0), (0xCB7150EF, 2), (0xCB7AF974, 2), (0xCC173D6D, 2), (0xCC1C94F6, 2),
        (0xCEE6FDE9, 0), (0xD0586F7B, 2), (0xD09008B1, 0), (0xD261C835, 2), (0xD26A61AE, 2),
        (0xD4B33B67, 1), (0xD507A5B7, 2), (0xD50C0C2C, 2), (0xD7FDCCA8, 0), (0xD926449A, 0),
        (0xDA68B355, 1), (0xDBA6B6B5, 0), (0xDBD7841E, 2), (0xDCCB72AC, 0), (0xDD05774C, 1),
        (0xDE190024, 2), (0xE1615104, 2), (0xE35D6816, 2), (0xE430AC0F, 2), (0xE46C4D76, 2),
        (0xE6073C86, 2), (0xE60C951D, 2), (0xEA4C3426, 1), (0xEAB7C544, 2), (0xEAC46630, 2),
        (0xED86E024, 2), (0xEDA20BB2, 2), (0xEDA9A229, 2), (0xEDDA015D, 2), (0xF1ED59A4, 2),
        (0xF3570567, 1), (0xF3D4FEEA, 2), (0xF43AC17E, 1), (0xF4B29368, 2), (0xF4B93AF3, 2),
        (0xF871C9DE, 2), (0xFA465957, 2), (0xFD2B9D4E, 2), (0xFF17A45C, 2), (0xFF1C0DC7, 2)
    ],
    0x7F140A1E: [  # nTimelineParam::AnimalCommon
        (0x08FD20A6, 1)
    ],
    0x7F2A1793: [  # nTimelineParam::Em110_01Motion
        (0x08FD20A6, 1), (0x99897C43, 1)
    ],
}


def _merge_pairs():
    """CORPUS_PAIRS ∪ DTI_EXTRA_PAIRS ∪ OFFICIAL_TLP_DT ∪ DTI_TLP_EXTRA —— 调色板全表。

    语料条目在前、DTI 补充在后，所以下拉里官方用过的排前面。最后那张
    `dti_tlp_extra.DTI_TLP_EXTRA` 补的是官方 dump 里被类型筛掉的参数（bool/u32/向量），
    合进来之后调色板 = 「DTI 认识的全部 TLP 参数」，不再按类型预筛。

    ⚠ 2026-09-20 修复：原来 `seen` 只在每张源表合并前算一次，不随合并过程更新，
    只挡得住"跨表重复"，挡不住"单张源表自己内部就重复"——`dti_tlp_extra.py` 里
    90/98 个 TLP 条目本身就有内部重复哈希（如 EffectParameter3/4 里同一个哈希连续
    出现 8~23 次），导致 TIML Tracks 面板同一行显示几十遍。改成逐条去重，不动原始
    数据文件（保留 dump 原始计数，只是调色板展示层去重）。"""
    from .dti_tlp_extra import DTI_TLP_EXTRA
    out = {}
    for src in (CORPUS_PAIRS, DTI_EXTRA_PAIRS, OFFICIAL_TLP_DT, DTI_TLP_EXTRA):
        for h, v in src.items():
            cur = out.setdefault(h, [])
            seen = {x[0] for x in cur}
            for x in v:
                if x[0] not in seen:
                    seen.add(x[0])
                    cur.append(x)
    return out


DT_PALETTE = _merge_pairs()
