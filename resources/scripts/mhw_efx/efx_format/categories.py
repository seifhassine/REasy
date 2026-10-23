"""属性类型的分类、推荐顺序与 UI 建议数据。

维护约束：
- 未登记类型归入 ``misc``；未细分的类型不返回 subgroup。
- 规范顺序和缺失属性建议仅用于创建、排序与 UI 提示，不能作为格式合法性验证。
- 分类 slug 同时决定属性预设的相对目录，修改时须保持与预设读写端一致。
"""

from .hashes import (
    # Skeleton
    TRANSFORM3D, PARENTOPTIONS, SPAWN, LIFE, TRANSFORM2D,
    # ExternReference
    EXTERNREFERENCE,
    # Renderer Body
    BILLBOARD3D, RIBBON, PLANE, LIGHTNING, RIBBONBLADE, STRAINRIBBON, BILLBOARD2D,
    MESH, DUMMY,
    # Renderer Modifier
    UVSEQUENCE, RGBFIRE, RGBWATER, ALPHACORRECTION, REFRACTION, BLINK, LUMINANCEBLEED,
    MATERIAL, UVCONTROL,
    PLEMISSIVE, PARENTEMISSIVE, PLSNOW, PARENTSNOW, OTOMOSNOW, PARENTMATERIAL,
    FAKEPLANE, SHADERSETTINGS,
    # Spawn Method
    EMITTERSHAPE3D, EMITTERSHAPEMESH, SPAWNBYANGLE, SPAWNBYOCCLUSION, RAYCAST, EMITTERSHAPE2D,
    # Motion & Visibility
    VELOCITY3D, SCALEANIM, ROTATEANIM, NOISE, TURBULENCE, HOMING, GUIDE, PATHCHAIN,
    VELOCITY2D, REPEATAREA,
    FADEBYDEPTH, FADEBYANGLE, FADEBYEMITTERANGLE, FADEBYOCCLUSION, MASTERONLY,
    EMITTERBOUNDARY, SCREENSPACECOLLISION, LINKPARTSVISIBLE,
    # Action Trigger
    PTCOLLISION, PTLIFE,
    # PtBehavior
    PTBEHAVIOR, TUBELIGHT,
    # Misc
    FAKEDOF, TONEMAPFILTER, COLORCORRECTFILTER,
    RANDOMFIX, CHECKPUREATTRIBUTE, LAYOUT, PTTRIGGER, SHOVEL,
)

# ── Top-level Categories (EN/ZH, ordered) ────────────────────
ATTRIBUTE_CATEGORY_LABELS = {
    "skeleton":          {"EN": "Entry Skeleton",       "ZH": "Entry 骨架"},
    "extern_reference":  {"EN": "ExternReference",      "ZH": "ExternReference"},
    "renderer_body":     {"EN": "Renderer Body",        "ZH": "渲染主体"},
    "renderer_modifier": {"EN": "Renderer Modifier",    "ZH": "渲染修饰"},
    "spawn_method":      {"EN": "Generation Method",    "ZH": "生成方式"},
    "motion_visibility": {"EN": "Motion & Visibility",  "ZH": "运动与可见性"},
    "action_trigger":    {"EN": "Action Trigger",       "ZH": "Action Trigger"},
    "pt_behavior":        {"EN": "PtBehavior",          "ZH": "PtBehavior"},
    "misc":              {"EN": "Misc",                 "ZH": "Misc"},
    "custom":            {"EN": "Custom",                "ZH": "Custom"},
}

# ── Subgroup slugs → bilingual labels (same slug has same meaning across different top-level categories) ─
ATTRIBUTE_SUBGROUP_LABELS = {
    "uvs":          {"EN": "UVS System",          "ZH": "UVS系"},
    "mesh":         {"EN": "Mesh System",         "ZH": "Mesh系"},
    "dummy":        {"EN": "Dummy System",        "ZH": "Dummy系"},
    "generic":      {"EN": "Generic",             "ZH": "通用/跨宿主"},
    "motion":       {"EN": "Motion",              "ZH": "运动"},
    "visibility":   {"EN": "Visibility",          "ZH": "可见性判定"},
    "post_process": {"EN": "Post-process Filters","ZH": "后处理滤镜"},
    "others":       {"EN": "Others",              "ZH": "其他"},
}

# ── type_hash → top-level category slug (unregistered types default to "misc") ─────────────
ATTRIBUTE_CATEGORY_OF = {
    # ── Entry Skeleton (every entry must have + 2D skeleton is merged) ─────────────────────────
    TRANSFORM3D:       "skeleton",
    PARENTOPTIONS:     "skeleton",
    SPAWN:             "skeleton",
    LIFE:              "skeleton",
    TRANSFORM2D:       "skeleton",   # 2D version of TRANSFORM3D

    # ── ExternReference ─────────────────────────
    EXTERNREFERENCE:   "extern_reference",

    # ── Renderer Body (mutually exclusive; subgroups see ATTRIBUTE_SUBGROUP_OF) ─────────────────────
    BILLBOARD3D:       "renderer_body",
    RIBBON:            "renderer_body",
    PLANE:             "renderer_body",
    LIGHTNING:         "renderer_body",
    RIBBONBLADE:       "renderer_body",
    STRAINRIBBON:      "renderer_body",
    BILLBOARD2D:       "renderer_body",   # 2D version of BILLBOARD3D
    MESH:              "renderer_body",
    DUMMY:             "renderer_body",   # 无视觉输出的功能性宿主（PTLIFE/SHOVEL/PLEMISSIVE 宿主）

    # ── Renderer Modifier (attached to Body, stackable; subgroups see ATTRIBUTE_SUBGROUP_OF) ────────────
    UVSEQUENCE:        "renderer_modifier",
    RGBFIRE:           "renderer_modifier",   # 与 RGBWATER 互斥
    RGBWATER:          "renderer_modifier",   # 与 RGBFIRE 互斥
    ALPHACORRECTION:   "renderer_modifier",   # 与 MESH/UVCONTROL 完全不共存
    REFRACTION:        "renderer_modifier",
    LUMINANCEBLEED:    "renderer_modifier",
    MATERIAL:          "renderer_modifier",   # 99.6% 与 MESH 共存；覆盖 mrl3 材质属性
    UVCONTROL:         "renderer_modifier",   # 100% 与 MESH 共存
    PLEMISSIVE:        "renderer_modifier",   # 宿主 100% 为 DUMMY
    PARENTEMISSIVE:    "renderer_modifier",   # 宿主 100% 为 DUMMY
    PLSNOW:            "renderer_modifier",
    PARENTSNOW:        "renderer_modifier",
    OTOMOSNOW:         "renderer_modifier",
    PARENTMATERIAL:    "renderer_modifier",
    FAKEPLANE:         "renderer_modifier",   # 跨宿主叠加渲染，99.8% 跟某个真渲染体共存，不互斥
    SHADERSETTINGS:    "renderer_modifier",   # 跨宿主：UVS系 100%/MESH 78.6%/DUMMY 43.8% 共现

    # ── Spawn Method (spawn at runtime) ────────────────────────────────────────────
    EMITTERSHAPE3D:    "spawn_method",
    EMITTERSHAPEMESH:  "spawn_method",
    SPAWNBYANGLE:      "spawn_method",
    SPAWNBYOCCLUSION:  "spawn_method",
    RAYCAST:           "spawn_method",
    EMITTERSHAPE2D:    "spawn_method",   # 2D version of EMITTERSHAPE3D

    # ── Motion & Visibility (per-frame behaviors; subgroups see ATTRIBUTE_SUBGROUP_OF) ─────────────────
    VELOCITY3D:        "motion_visibility",
    SCALEANIM:         "motion_visibility",
    ROTATEANIM:        "motion_visibility",
    NOISE:             "motion_visibility",
    TURBULENCE:        "motion_visibility",
    HOMING:            "motion_visibility",
    GUIDE:             "motion_visibility",
    PATHCHAIN:         "motion_visibility",
    VELOCITY2D:        "motion_visibility",   # 2D version of VELOCITY3D
    REPEATAREA:        "motion_visibility",   # 跟 VELOCITY3D 共现 91.9%
    FADEBYDEPTH:       "motion_visibility",
    BLINK:             "motion_visibility",   # 宿主不限 UVS 类：光源、MESH、DUMMY 都在用
    FADEBYANGLE:       "motion_visibility",
    FADEBYEMITTERANGLE:"motion_visibility",
    FADEBYOCCLUSION:   "motion_visibility",
    MASTERONLY:        "motion_visibility",
    EMITTERBOUNDARY:   "motion_visibility",
    SCREENSPACECOLLISION: "motion_visibility",
    LINKPARTSVISIBLE:  "motion_visibility",

    # ── Action Trigger ──────────────────
    PTCOLLISION:       "action_trigger",
    PTLIFE:            "action_trigger",

    # ── PtBehavior（independent behavior system） ────────────────
    PTBEHAVIOR:        "pt_behavior",
    TUBELIGHT:         "pt_behavior",

    # ── Misc ──────────────────────────────
    FAKEDOF:           "misc",
    TONEMAPFILTER:     "misc",
    COLORCORRECTFILTER:"misc",
    RANDOMFIX:         "misc",
    CHECKPUREATTRIBUTE:"misc",
    LAYOUT:            "misc",
    PTTRIGGER:         "misc",   # looks like Action Trigger, but it doesn't call any Action segment
    SHOVEL:            "misc",
}

# ── type_hash → subgroup slug (only types that are further subdivided within their category have entries) ─
ATTRIBUTE_SUBGROUP_OF = {
    # ── Renderer Body Subgroups ────────────────────────────────────────────────────
    BILLBOARD3D:  "uvs",
    RIBBON:       "uvs",
    PLANE:        "uvs",
    LIGHTNING:    "uvs",
    RIBBONBLADE:  "uvs",
    STRAINRIBBON: "uvs",
    BILLBOARD2D:  "uvs",
    MESH:         "mesh",
    DUMMY:        "dummy",

    # ── Renderer Modifier Subgroups ────────────────────────────────────────────────
    UVSEQUENCE:      "uvs",
    RGBFIRE:         "uvs",
    RGBWATER:        "uvs",
    ALPHACORRECTION: "uvs",
    REFRACTION:      "uvs",
    LUMINANCEBLEED:  "uvs",
    MATERIAL:        "mesh",
    UVCONTROL:       "mesh",
    PLEMISSIVE:      "dummy",
    PARENTEMISSIVE:  "dummy",
    PLSNOW:          "dummy",
    PARENTSNOW:      "dummy",
    OTOMOSNOW:       "dummy",
    PARENTMATERIAL:  "dummy",
    FAKEPLANE:       "generic",
    SHADERSETTINGS:  "generic",

    # ── Motion and Visibility Subgroups ─────────────────────────────────────────────────────────
    VELOCITY3D:            "motion",
    SCALEANIM:             "motion",
    ROTATEANIM:            "motion",
    NOISE:                 "motion",
    TURBULENCE:            "motion",
    HOMING:                "motion",
    GUIDE:                 "motion",
    PATHCHAIN:             "motion",
    VELOCITY2D:            "motion",
    REPEATAREA:            "motion",
    SCREENSPACECOLLISION:  "motion",
    FADEBYDEPTH:           "visibility",
    BLINK:                 "visibility",
    FADEBYANGLE:           "visibility",
    FADEBYEMITTERANGLE:    "visibility",
    FADEBYOCCLUSION:       "visibility",
    MASTERONLY:            "visibility",
    EMITTERBOUNDARY:       "visibility",
    LINKPARTSVISIBLE:      "visibility",

    # ── Misc Subgroups ────────────────────────────────────────────────────────────────
    FAKEDOF:            "post_process",
    TONEMAPFILTER:      "post_process",
    COLORCORRECTFILTER: "post_process",
    RANDOMFIX:          "others",
    CHECKPUREATTRIBUTE: "others",
    LAYOUT:             "others",
    PTTRIGGER:          "others",
    SHOVEL:             "others",
}

# ── Entry suffix display: UI/UX, for quickly seeing the key features of this entry ──
SUFFIX_DISPLAY_TYPES = frozenset({
    # Renderer Body
    BILLBOARD3D, RIBBON, PLANE, LIGHTNING, RIBBONBLADE, STRAINRIBBON, BILLBOARD2D,
    MESH, DUMMY,
    # PtBehavior
    PTBEHAVIOR, TUBELIGHT,
    # Action Trigger
    PTCOLLISION, PTLIFE,
    # ExternReference
    EXTERNREFERENCE,
})


# Entry 内属性的推荐显示/插入顺序，不是格式约束
ATTRIBUTE_CANONICAL_ORDER = {
    EXTERNREFERENCE:         0,
    RANDOMFIX:               1,
    TRANSFORM2D:             2,
    TRANSFORM3D:             3,
    PARENTOPTIONS:           4,
    LINKPARTSVISIBLE:        5,
    RAYCAST:                 6,
    SPAWN:                   7,
    SPAWNBYOCCLUSION:        8,
    SPAWNBYANGLE:            9,
    LIFE:                   10,
    EMITTERSHAPE2D:         11,
    FADEBYANGLE:            12,
    FADEBYDEPTH:            13,
    RIBBONBLADE:            14,
    VELOCITY2D:             15,
    FADEBYOCCLUSION:        16,
    CHECKPUREATTRIBUTE:     17,
    BILLBOARD2D:            18,
    FADEBYEMITTERANGLE:     19,
    EMITTERSHAPEMESH:       20,
    PARENTSNOW:             21,
    TUBELIGHT:              22,
    FAKEPLANE:              23,
    DUMMY:                  24,
    EMITTERSHAPE3D:         25,
    PARENTEMISSIVE:         26,
    VELOCITY3D:             27,
    UVCONTROL:              28,
    OTOMOSNOW:              29,
    PLSNOW:                 30,
    TONEMAPFILTER:          31,
    MESH:                   32,
    PTTRIGGER:              33,
    LIGHTNING:              34,
    REPEATAREA:             35,
    PLANE:                  36,
    BILLBOARD3D:            37,
    RIBBON:                 38,
    ROTATEANIM:             39,
    SCALEANIM:              40,
    UVSEQUENCE:             41,
    STRAINRIBBON:           42,
    PARENTMATERIAL:         43,
    ALPHACORRECTION:        44,
    SHADERSETTINGS:         45,
    PLEMISSIVE:             46,
    REFRACTION:             47,
    EMITTERBOUNDARY:        48,
    PTLIFE:                 49,
    PTBEHAVIOR:             50,
    RGBFIRE:                51,
    MATERIAL:               52,
    RGBWATER:               53,
    PTCOLLISION:            54,
    SHOVEL:                 55,
    TURBULENCE:             56,
    SCREENSPACECOLLISION:   57,
    NOISE:                  58,
    BLINK:                  59,
    MASTERONLY:             60,
    PATHCHAIN:              61,
    LAYOUT:                 62,
    HOMING:                 63,
    COLORCORRECTFILTER:     64,
    FAKEDOF:                65,
    LUMINANCEBLEED:         66,
    GUIDE:                  67,
}

# 未登记类型始终排在末尾
CANONICAL_ORDER_DEFAULT = 999


def canonical_rank(type_hash: int) -> int:
    """返回属性的推荐顺序；未知类型使用末尾默认值。"""
    return ATTRIBUTE_CANONICAL_ORDER.get(type_hash, CANONICAL_ORDER_DEFAULT)


def canonical_insert_index(existing_hashes, new_hash: int) -> int:
    """返回按推荐顺序插入属性的位置。

    同 rank 的属性追加至现有同级之后，保证重复添加的稳定顺序。
    """
    rank = canonical_rank(new_hash)
    pos = len(existing_hashes)
    for i, h in enumerate(existing_hashes):
        if canonical_rank(h) > rank:
            pos = i
            break
    return pos

# Entry 主模块仅用于 Inspector 显示归类
ENTRY_MAIN_MODULE = (
    TRANSFORM3D,
    TRANSFORM2D,
    PARENTOPTIONS,
    SPAWN,
    LIFE,
)


def is_main_module(type_hash: int) -> bool:
    """判断属性是否归入 Inspector 的主模块显示区。"""
    return type_hash in ENTRY_MAIN_MODULE


# 渲染主体对应的属性建议；仅供 UI 提示和模板构建
ATTRIBUTE_LINE_RECIPES = {
    BILLBOARD3D: (  # n=62785
        (TRANSFORM3D, 100),
        (PARENTOPTIONS, 100),
        (SPAWN, 100),
        (SHADERSETTINGS, 100),
        (UVSEQUENCE, 100),
        (LIFE, 100),
        (VELOCITY3D, 88),
        (EMITTERSHAPE3D, 84),
        (SCALEANIM, 78),
        (RGBFIRE, 67),
        (FADEBYDEPTH, 51),
        (ROTATEANIM, 29),
        (RANDOMFIX, 25),
    ),
    RIBBON: (  # n=14673
        (TRANSFORM3D, 100),
        (PARENTOPTIONS, 100),
        (SPAWN, 100),
        (UVSEQUENCE, 100),
        (SHADERSETTINGS, 100),
        (LIFE, 100),
        (VELOCITY3D, 84),
        (EMITTERSHAPE3D, 83),
        (SCALEANIM, 72),
        (RGBFIRE, 60),
        (FADEBYDEPTH, 52),
        (RGBWATER, 23),
    ),
    MESH: (  # n=13624
        (TRANSFORM3D, 100),
        (PARENTOPTIONS, 100),
        (SPAWN, 100),
        (LIFE, 99),
        (VELOCITY3D, 84),
        (SHADERSETTINGS, 79),
        (ROTATEANIM, 78),
        (EMITTERSHAPE3D, 77),
        (MATERIAL, 43),
        (SCALEANIM, 34),
        (RANDOMFIX, 26),
        (PTCOLLISION, 23),
    ),
    None: (  # n=9087
        (TRANSFORM3D, 100),
        (PARENTOPTIONS, 100),
        (SPAWN, 100),
        (LIFE, 98),
        (PTBEHAVIOR, 80),
        (SHADERSETTINGS, 30),
    ),
    DUMMY: (  # n=6578
        (TRANSFORM3D, 100),
        (PARENTOPTIONS, 100),
        (SPAWN, 100),
        (LIFE, 96),
        (PTLIFE, 80),
        (EMITTERSHAPE3D, 46),
        (SHADERSETTINGS, 44),
        (VELOCITY3D, 21),
    ),
    PLANE: (  # n=4503
        (TRANSFORM3D, 100),
        (PARENTOPTIONS, 100),
        (SPAWN, 100),
        (SHADERSETTINGS, 100),
        (UVSEQUENCE, 100),
        (LIFE, 99),
        (SCALEANIM, 65),
        (EMITTERSHAPE3D, 57),
        (RGBFIRE, 43),
        (VELOCITY3D, 41),
        (RANDOMFIX, 29),
        (ROTATEANIM, 28),
        (FADEBYDEPTH, 24),
        (RGBWATER, 22),
    ),
    BILLBOARD2D: (  # n=580
        (TRANSFORM2D, 100),
        (PARENTOPTIONS, 100),
        (SPAWN, 100),
        (LIFE, 100),
        (UVSEQUENCE, 100),
        (SHADERSETTINGS, 100),
        (SCALEANIM, 63),
        (EMITTERSHAPE2D, 50),
        (VELOCITY2D, 48),
        (ROTATEANIM, 21),
    ),
    LIGHTNING: (  # n=483
        (TRANSFORM3D, 100),
        (PARENTOPTIONS, 100),
        (SPAWN, 100),
        (LIFE, 100),
        (VELOCITY3D, 100),
        (UVSEQUENCE, 100),
        (SHADERSETTINGS, 100),
        (RGBFIRE, 96),
        (EMITTERSHAPE3D, 86),
        (ALPHACORRECTION, 52),
        (FADEBYDEPTH, 51),
    ),
    STRAINRIBBON: (  # n=181，样本少，仅供参考
        (TRANSFORM3D, 100),
        (PARENTOPTIONS, 100),
        (SPAWN, 100),
        (UVSEQUENCE, 100),
        (SHADERSETTINGS, 100),
        (LIFE, 97),
        (EMITTERSHAPE3D, 80),
        (SCALEANIM, 44),
        (RANDOMFIX, 32),
        (RGBWATER, 20),
        (VELOCITY3D, 20),
    ),
    RIBBONBLADE: (  # n=50，样本少，仅供参考
        (TRANSFORM3D, 100),
        (PARENTOPTIONS, 100),
        (SPAWN, 100),
        (LIFE, 100),
        (UVSEQUENCE, 100),
        (SHADERSETTINGS, 100),
        (ALPHACORRECTION, 82),
        (REFRACTION, 80),
        (RGBFIRE, 20),
    ),
}


def line_recipe(body_hash):
    """返回渲染主体的属性建议；未知主体返回空元组。"""
    return ATTRIBUTE_LINE_RECIPES.get(body_hash, ())


def suggest_missing_attributes(body_hash, present_hashes, min_rate=40):
    """返回当前主体常见但尚未存在的属性建议。

    结果仅供建议，不能作为验证错误。
    """
    present = set(present_hashes)
    return [(h, r) for h, r in line_recipe(body_hash)
            if r >= min_rate and h not in present]


def category_of(type_hash: int) -> str:
    """返回顶层分类 slug；未知类型归入 ``misc``。"""
    return ATTRIBUTE_CATEGORY_OF.get(type_hash, "misc")


def subgroup_of(type_hash: int) -> str:
    """返回子分类 slug；未细分或未知类型返回空字符串。"""
    return ATTRIBUTE_SUBGROUP_OF.get(type_hash, "")


def attribute_preset_relpath(type_hash: int) -> tuple:
    """返回属性预设在 ``__attributes__`` 下的相对路径片段。"""
    cat = category_of(type_hash)
    sub = subgroup_of(type_hash)
    return (cat, sub) if sub else (cat,)


def category_label(slug: str, lang: str = "ZH") -> str:
    """返回分类显示名；未知 slug 原样返回。"""
    entry = ATTRIBUTE_CATEGORY_LABELS.get(slug)
    if entry is None:
        return slug
    return entry.get(lang) or entry.get("EN") or slug


def subgroup_label(slug: str, lang: str = "ZH") -> str:
    """返回子分类显示名；未知 slug 原样返回。"""
    entry = ATTRIBUTE_SUBGROUP_LABELS.get(slug)
    if entry is None:
        return slug
    return entry.get(lang) or entry.get("EN") or slug

def renderer_suffix(type_hashes) -> str:
    """从条目中的关键类型生成显示后缀。"""
    from .hashes import HASH_TO_NAME, pretty_type_name
    names = []
    for type_hash in type_hashes:
        if type_hash not in SUFFIX_DISPLAY_TYPES:
            continue
        raw_name = HASH_TO_NAME.get(type_hash)
        if raw_name:
            names.append(pretty_type_name(raw_name))
    return " (%s)" % ", ".join(names) if names else ""
