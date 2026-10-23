# -*- coding: utf-8 -*-
"""
efx_format/schema/enums.py — 共享枚举 / 位定义
"""
from .fields_model import EnumDef, BitDef, BitEnum

"""属性字段共用的枚举与位标志定义。

维护约束：
- 枚举和位标志只提供 UI 元数据，不改变底层整数 spec 或序列化字节。
- 未建模或越界的整数必须由调用方回退显示为原始值，不能在此处推断其语义。
- 位标志中的 ``?`` 表示语义尚未确认，保留其位位置以避免破坏既有数据。
"""

ENUM_SHAPE_TYPE3D = EnumDef("ShapeType3D", [
    (0, "Box", "立方体"),
    (1, "Sphere", "球体"),
    (2, "Cylinder", "圆柱体"),
    (3, "Point", "点"),
])
# EMITTERSHAPE3D：Box 的细分轴
ENUM_RANGE_DIVIDE_AXIS = EnumDef("RangeDivideAxis", [
    (0, "X-axis", "X 轴"),
    (1, "Z-axis", "Z 轴"),
    (2, "Y-axis", "Y 轴"),
])
# EMITTERSHAPE2D：Box 的细分轴
ENUM_RANGE_DIVIDE_AXIS_2D = EnumDef("RangeDivideAxis2D", [
    (0, "Y-axis", "Y 轴"),
    (1, "X-axis", "X 轴"),
    (2, "Unknown (2)", "未知 (2)"),
])
# EMITTERSHAPE3D：rotationCorrect
ENUM_ROTATION_CORRECT_TYPE = EnumDef("RotationCorrectType", [
    (0, "None", "不修正"),
    (1, "Parallel Camera", "与摄像机平行"),
    (2, "Parallel Camera (Y axis only)", "与摄像机平行（仅 Y 轴）"),
    (3, "To Camera", "朝向摄像机"),
    (4, "To Camera (Y axis only)", "朝向摄像机（仅 Y 轴）"),
    (5, "Unknown (5)", "未知 (5)"),
    (7, "Unknown (7)", "未知 (7)"),
])
# EMITTERSHAPE3D：RayCast 结果与 Offset 的组合模式，不是引用
ENUM_RAYCAST_DEPENDENCY = EnumDef("RayCastDependency", [
    (0, "None", "无"),
    (1, "Equal", "相等（范围由 RayCast 位置决定）"),
    (2, "Multiply", "相乘（范围为 RayCast 位置×Offset 决定）"),
    (3, "Min", "取最小值（范围取 RayCast 位置和 Offset 的较小值）"),
    (4, "Max", "取最大值（范围取 RayCast 位置和 Offset 的较大值）"),
    (5, "Offset", "偏移（范围整体随 RayCast 位置偏移）"),
])
ENUM_SHAPE_TYPE2D = EnumDef("ShapeType2D", [
    (0, "Square", "方形"),
    (1, "Circle", "圆形"),
    (2, "Point", "点"),
])
# PTCOLLISION：达到 bounceCount 后的接触处理
ENUM_COLLISION_PHYSICS = EnumDef("CollisionPhysics", [
    (0, "Fall Through", "穿透坠落"),
    (1, "Bounce Then Kill", "反弹后强制消亡"),
    (2, "Bounce Then Fade", "反弹后渐隐消亡"),
    (3, "Bounce Then Stay", "反弹后停留地面"),
    (4, "Bounce Then Fall Through", "反弹后穿透坠落"),
])
# PTCOLLISION：ieIndex 引用的 Play 的触发时机
ENUM_IMPACT_PLAY_TRIGGER_MODE = EnumDef("ImpactPlayTriggerMode", [
    (0, "Every Impact", "每次触地"),
    (1, "Early Impacts", "前 N 次触地"),
    (2, "Final Impact", "仅最后一次触地"),
])
# LIFE：淡入、持续、淡出阶段
ENUM_PTLIFE_STATUS = EnumDef("PtLifeStatus", [
    (0, "On Spawn", "生成时"),
    (1, "Fade In", "淡入时"),
    (2, "Sustain", "持续时"),
    (3, "Fade Out", "淡出时"),
    (4, "On Death", "死亡时"),
    (-1, "Unknown", "未知"),
])
# PTCOLLISION：碰撞状态
# EXTERNREFERENCE 触发条件：bit0 开启过渡，bit1 改按粒子生命周期（只和 bit0 一起出现）
BITS_EXTERNREF_TRIGGER = [
    (0x1, "Over Lifetime", "随生命周期过渡"),
    (0x2, "Use Particle Lifetime", "按粒子生命周期"),
]
ENUM_PTLIFE_RELATION = EnumDef("PtLifeRelation", [
    (0, "Enabled", "启用"),
    (-1, "Disabled", "停用"),
])
ENUM_HOMING_TARGET = EnumDef("HomingTarget", [
    (0, "Spawn Point", "生成点"),
    (1, "Model Origin", "模型原点"),
    (2, "World Origin", "世界原点"),
    (3, "World Origin", "世界原点"),
])
# HOMING：以目标为中心的作用场区域规则
ENUM_HOMING_FORCEFIELD = EnumDef("HomingForceFieldMode", [
    (0, "None", "无"),
    (1, "Cull Spawn Inside", "内部出生剔除"),
    (2, "Slow Inside", "内部减速"),
    (3, "No Turn Inside", "内部不转向"),
    (4, "Slow Outside", "外部减速"),
])
ENUM_HOMING_VANISH = EnumDef("HomingVanishMode", [
    (0, "None", "不触发"),
    (1, "Cancel Infinite Life", "取消无限寿命"),
    (2, "Vanish Immediately", "立即消失"),
])
# SHADERSETTINGS.particleLighting：粒子受场景光照与阴影的方式。2 语料极少且未测，不列入下拉。
ENUM_PARTICLE_LIGHTING = EnumDef("ParticleLighting", [
    (0, "Unlit", "不受光"),
    (1, "Per Particle", "逐粒子"),
    (2, "Unknown (2)", "未知 (2)", True),
    (3, "Per Pixel", "逐像素"),
])
# SHADERSETTINGS.drawMode：单一枚举，不按位拆分；未列出的取值按默认处理。
# 6～9 与 0 在实机上无可辨差异，暂按光暗过渡的插值方式不同处理。
ENUM_DRAW_MODE = EnumDef("DrawMode", [
    (0, "Default", "默认"),
    (2, "Water Ripple", "水面波纹"),
    (3, "Overlay (No Tone Mapping)", "覆盖层（不受色调处理）"),
    (6, "Lighting Transition 6?", "光暗过渡 6?"),
    (7, "Lighting Transition 7?", "光暗过渡 7?"),
    (8, "Lighting Transition 8?", "光暗过渡 8?"),
    (9, "Lighting Transition 9?", "光暗过渡 9?"),
])
# SHADERSETTINGS.drawTarget：只在 2D 屏幕叠加层上非 0。1～3 的含义按官方层名推断。
ENUM_DRAW_TARGET = EnumDef("DrawTarget", [
    (0, "Screen", "屏幕"),
    (1, "Overlay Color?", "叠加层颜色?"),
    (2, "Overlay Normal?", "叠加层法线?"),
    (3, "Overlay Mask?", "叠加层遮罩?"),
])
# SHADERSETTINGS.blendStateType：渲染项与背景的混合方式，覆盖渲染体自身的设置。
# 4/5/7/9/10 与对应基础模式的画面相同。4/10 语料未出现，9 看不出独立特征，不列入下拉；
# 5 只出现在贴花 PtBehavior 上，6 多用于不显示的承载 Entry。
ENUM_BLEND_STATE = EnumDef("BlendState", [
    (0, "Opaque", "不透明"),
    (1, "Alpha", "Alpha 混合"),
    (2, "Additive", "加法"),
    (3, "Inverse Multiply", "反相乘法"),
    (4, "Opaque (4)", "不透明 (4)", True),
    (5, "PtBehavior (decal)", "PtBehavior (decal)"),
    (6, "NoDraw", "不绘制(dummy)"),
    (7, "Additive (7)", "加法 (7)"),
    (8, "Multiply", "乘法"),
    (9, "Alpha (9)", "Alpha 混合 (9)", True),
    (10, "Opaque (10)", "不透明 (10)", True),
])
# ROTATEANIM：加载时拆成两个独立开关；bit1 在界面上按二选一显示
BITS_ROTATION_MODE = [
    BitEnum(0x2, [(0, "Plane Rotation", "平面旋转"), (1, "Spin Velocity", "自旋速度")],
            "Rotation Type", "旋转方式"),
    BitDef(0x1, "Random Direction", "随机正反"),
]

# PARENTOPTIONS：逐轴跟随模式；前三项共享，第四项按字段区分
_TRACKING_BASE = [
    (0, "Track Map Center Absolutely", "绝对追踪地图中心"),
    (1, "Track Player Movement", "追踪玩家移动"),
    (2, "Do not track further", "不再追踪后续移动"),
]
ENUM_TRACKING_POS = EnumDef("TrackingModePos",
    _TRACKING_BASE + [(3, "Ignore Basic Transform", "忽略基础变换")])   # translation / scale
ENUM_TRACKING_ANGLE = EnumDef("TrackingModeAngle",
    _TRACKING_BASE + [(3, "Snap to Angle And Track", "对齐到角度并追踪")])  # angle

# MESH.tracking_flags 是不可组合的枚举，未知值保留原整数；3/5/7 不列入下拉
ENUM_MESH_TRACKING_FLAGS = EnumDef("MeshTrackingFlags", [
    (0, "Face Source", "面向源"),
    (1, "Face Away From Source", "背对源"),
    (2, "Look Away From Camera", "背对摄像机"),
    (3, "WTF Occupies Entire Map", "WTF 占满整张地图", True),
    (4, "Face Camera", "面向摄像机"),
    (5, "Disappears", "消失", True),
    (6, "Don't Track Rotation At All", "完全不追踪旋转"),
    (7, "Disappears", "消失", True),
    (8, "Perpendicular to Ground, Don't Track", "垂直于地面且不追踪"),
    (10, "Unknown (10)", "未知 (10)"),
])

BITS_ENABLE_VELOCITY = [(0x1, "Enable Velocity", "启用速度"), (0x2, "Enable Acceleration", "启用加速度")]

# RAYCAST：仅 bit0/bit1 已建模；其余残留位必须由位掩码保留
BITS_RAYCAST_ATTR = [(0x1, "SCR", "SCR"), (0x2, "OBJ", "OBJ")]

# RAYCAST：两个独立开关
BITS_RAYCAST_FLAGS = [(0x1, "SyncSpawnFrame", "同步生成帧"), (0x100, "RayCastOnce", "仅一次")]

# SPAWN：bit3/bit4 的名称仍待确认
BITS_SPAWN_FLAGS = [
    (0x01, "RingBufferMode", "RingBufferMode"),
    (0x02, "RayCastHitOnly?", "RayCastHitOnly?"),
    (0x04, "RayCastDependency?", "RayCastDependency?"),
    (0x08, "InitializeFull?", "InitializeFull?"),
    (0x10, "Interpolate?", "Interpolate?"),
    (0x20, "UseSpawnFrame", "UseSpawnFrame"),
]

# PLEMISSIVE：bit0 的名称为弱假设，bit3 未确认
BITS_PLEMISSIVE_EMIT_MASK = [
    (0x1, "EnableMaskAnim", "启用遮罩动画（弱假设，也可能是 bit3）"),
    (0x2, "OverrideSecondaryEmitControl", "覆盖次级发光控制"),
    (0x4, "RimAlphaCorrect", "边缘光透明度修正"),
    (0x8, "unkn3", "未知（可能是 EnableMaskAnim）"),
]

# ROTATEANIM：不是 XYZ 轴掩码；模拟层仅使用 spin_velocity
BITS_ROTATEANIM_SPIN_FLAGS = [
    (1 << _i, "Bit %d" % _i, "位 %d" % _i) for _i in range(7)
]
#: 历史名称；保留别名兼容外部引用
BITS_SPIN_AXIS = BITS_ROTATEANIM_SPIN_FLAGS
BITS_RANDOMFIX_TABLE = [(1 << _i, "Table %d" % _i, "表 %d" % _i) for _i in range(8)]

# FADEBYANGLE：bit2 尚未确认
BITS_FADEBYANGLE_FLAGS = [
    (0x1, "Enable Double Cone", "启用双锥（镜像对立角）"),
    (0x2, "Exclude Cone", "排除锥体（反转可见性）"),
    (0x4, "Unknown", "未知"),
]

# MESH BeginMod3：5 个独立位，作用均未知
BITS_MESH_BEGINMOD3 = [(1 << _i, "Unknown Bit %d" % _i, "未知位 %d" % _i) for _i in range(5)]

# PLANE：bit0 为总开关，bit1/bit2 为其子模式
BITS_PLANE_UNKN5_1 = [
    (0x1, "Enable", "启用（总开关）"),
    (0x2, "Unknown", "未知"),
    (0x4, "Unknown", "未知"),
]

# BILLBOARD3D / PLANE：bit2/bit3 可组合，bit4/bit5 互斥
BITS_APPLICATION_RULE = [
    BitDef(0x04, "Enable Flowmap", "启用流动贴图"),
    BitDef(0x08, "Freeze After One Play", "播放一次后冻结"),
    BitDef(0x10, "Reverse Playback", "逆向播放"),
    BitDef(0x20, "Unknown Bit 0x20", "未知位 0x20"),
]

# UVSEQUENCE：四个互斥的 2 位组
_FLIP_OPTS = [(0, "No Flip", "不翻转"), (1, "Flip", "固定翻转"), (2, "Random Flip", "随机翻转")]
BITS_LOOPING_MODE = [
    BitEnum(0x03, [
        (0, "Start Frame Only", "只显示起始帧"),
        (1, "Loop", "循环"),
        (2, "Play Once Then Vanish", "播放一次后消亡"),
        (3, "Play Once Then Hold", "播放一次后定格"),
    ], "Playback Mode", "播放模式"),
    BitEnum(0x0C, _FLIP_OPTS, "Flip Horizontal", "水平翻转"),
    BitEnum(0x30, _FLIP_OPTS, "Flip Vertical", "垂直翻转"),
    BitEnum(0xC0, [
        (0, "Forward", "正向"),
        (1, "Reverse", "倒放"),
        (2, "Random Direction", "随机正倒"),
    ], "Direction", "播放方向"),
]

# 渲染主体共用的 LightGroup 位序；0xFF 是独立的全选哨兵
# MESH.shadowCastBitflag：全部关闭时模型和阴影都不绘制
BITS_MESH_DRAW = [
    (0x01, "Draw Model", "绘制模型"),
    (0x02, "Draw Shadow", "绘制阴影"),
    (0x04, "Unknown (bit 2)", "未知（位 2）"),
    (0x08, "Unknown (bit 3)", "未知（位 3）"),
    (0x10, "Unknown (bit 4)", "未知（位 4）"),
    (0x20, "Unknown (bit 5)", "未知（位 5）"),
    (0x40, "Unknown (bit 6)", "未知（位 6）"),
    (0x80, "Unknown (bit 7)", "未知（位 7）"),
]

BITS_LIGHT_GROUP = [
    (0x01, "VFX", "VFX"),
    (0x02, "Player/Otomo", "玩家/艾露猫"),
    (0x04, "NPC", "NPC"),
    (0x08, "Enemy", "敌人"),
    (0x10, "Light Object", "光照物体"),
    (0x20, "SCR", "SCR"),
    (0x40, "Group 6", "组 6"),
    (0x80, "Eye/Lens", "眼睛/镜头"),
]

_AXIS_DIRECTION6 = EnumDef("AxisDirection6", [
    (0, "Left", "左"),  # +X
    (1, "Up", "上"),    # +Y
    (2, "Front", "前"), # +Z
    (3, "Right", "右"), # -X
    (4, "Down", "下"),  # -Y
    (5, "Back", "后"),  # -Z
])
# RAYCAST 复用 AxisDirection6；若有反证则恢复独立表
ENUM_RAYCAST_DIR = _AXIS_DIRECTION6

# RAYCAST：已知 ID 只作快捷选择，未知 ID 保持原整数
ENUM_RAYCAST_ID = EnumDef("RayCastID", [
    (-1, "None", "无"),
    (0, "0", "0"),
    (1, "1", "1"),
    (2, "2", "2"),
])

_ROT_ORDER6 = EnumDef("RotOrder", [
    (0, "XYZ", "XYZ"), (1, "XZY", "XZY"), (2, "YXZ", "YXZ"),
    (3, "YZX", "YZX"), (4, "ZXY", "ZXY"), (5, "ZYX", "ZYX"),
])
# TRANSFORM3D / EMITTERSHAPE3D / RIBBON 的顺序表不同于 VELOCITY3D
_TRANSFORM_ROT_ORDER = EnumDef("TransformRotOrder", [
    (0, "XYZ", "XYZ"), (1, "YZX", "YZX"), (2, "YXZ", "YXZ"),
    (3, "ZYX", "ZYX"), (4, "ZXY", "ZXY"), (5, "XZY", "XZY"),
])
# VELOCITY3D：发射方向模式
# VELOCITY2D 复用同一枚举，但不取 4。
_VELOCITY_TYPE = EnumDef("VelocityType", [
    (0, "Direction", "定向"),
    (1, "Normal", "定向扩散"),
    (2, "Radial", "径向"),
    (3, "Emitter Move", "发射器运动"),
    (4, "Unknown (4)", "未知 (4)"),
])

# RIBBON：条带形态
ENUM_RIBBON_MODE = EnumDef("RibbonMode", [
    (0, "Ribbon Follow", "轨迹跟随"),
    (1, "Ribbon Length", "定长面片"),
    (2, "Ribbon Chain", "柔体链"),
])

# RIBBON：长度方向贴图缩放方式
ENUM_RIBBON_UV_SCALE_MODE = EnumDef("RibbonUVScaleMode", [
    (0, "Off", "不缩放"),
    (1, "Fixed Count", "固定次数"),
    (2, "By Aspect Ratio", "按长宽比"),
])

# UVSEQUENCE：贴图朝向，与水平/垂直翻转独立
ENUM_LOOPING_ORIENTATION = EnumDef("LoopingOrientation", [
    (0, "Normal", "正常"),
    (1, "Rotate 90° CW", "顺时针90°"),
    (2, "Rotate 90° CCW", "逆时针90°"),
    (3, "Random", "随机"),
])

# REFRACTION：distortionType
ENUM_DISTORTION_TYPE = EnumDef("DistortionType", [
    (0, "Light Refraction", "轻度折射"),
    (1, "Refraction", "折射"),
    (2, "Directional Blur", "方向模糊"),
])

# Root.UnitBoundary：语义仍待确认
ENUM_UNITBOUNDARY_TYPE = EnumDef("UnitBoundaryType", [
    (0, "Sphere?", "球形?"),
    (1, "Box?", "长方体?"),
    (2, "None?", "无?"),
])
