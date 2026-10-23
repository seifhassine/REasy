"""PTBEHAVIOR 目录的 DTI 补项。

补项使用已知的 DTI 类型到 EFX_Behav.t 映射；class、方法与未知编码类型不纳入。补项
按 anchor 插入目录，默认值由 edit.py 提供，且不应被视为已有实例的推荐配置。
"""

# ``anchor`` 表示插在该已知 key 之后。
DTI_EXTRA_FIELDS = {
    'nEffect::PointLightBehavior': [
        (0x21BA894E, 0x0C, 0xA3D00CD9),   # mShadowDepthBias           f32
        (0x966B3A72, 0x0C, 0xA3D00CD9),   # mShadowSlopedDepthBias     f32
        (0x07BDD7DB, 0x0C, 0xA3D00CD9),   # mShadowMaxDepthBias        f32
    ],
    'MhPointLightBehavior': [
        (0xA3D00CD9, 0x0C, 0x8A747FCC),   # mNearClipDistance          f32
        (0x21BA894E, 0x0C, 0x8A747FCC),   # mShadowDepthBias           f32
        (0x966B3A72, 0x0C, 0x8A747FCC),   # mShadowSlopedDepthBias     f32
        (0x07BDD7DB, 0x0C, 0x8A747FCC),   # mShadowMaxDepthBias        f32
    ],
    'MhSpotLightBehavior': [
        (0x4A28ECBA, 0x06, 0x9074DE04),   # mPriority                  u32
        (0x94A79998, 0x03, 0x5F18DFD2),   # mPrimary                   bool
        (0x8A747FCC, 0x06, 0xF8860B8C),   # mShadowMapSize             u32
        (0x21BA894E, 0x0C, 0xA3D00CD9),   # mShadowDepthBias           f32
        (0x966B3A72, 0x0C, 0xA3D00CD9),   # mShadowSlopedDepthBias     f32
        (0x07BDD7DB, 0x0C, 0xA3D00CD9),   # mShadowMaxDepthBias        f32
        (0xEB5E1675, 0x03, 0xA3D00CD9),   # mDoLPVInjection            bool
        (0x94AECC76, 0x40, 0x902C6057),   # mProjectionOffset          vector2
    ],
    'nEffect::MhEffectDecalBehavior': [
        (0x12399297, 0x36, 0x8CC3EEAD),   # mSpecularAppearFrame       range
        (0x86743996, 0x36, 0x8CC3EEAD),   # mSpecularKeepFrame         range
        (0x3C3989BF, 0x36, 0x8CC3EEAD),   # mSpecularVanishFrame       range
        (0x5E05FE41, 0x03, 0x8CC3EEAD),   # mSpecularKeepHoldFlag      bool
        (0xEF5D9053, 0x06, 0x8CC3EEAD),   # mSpecularLifeType          u32
        (0x57941CDC, 0x36, 0x100DE642),   # mSheetAppearFrame          range
        (0x532D9150, 0x36, 0x100DE642),   # mSheetKeepFrame            range
        (0x799407F4, 0x36, 0x100DE642),   # mSheetVanishFrame          range
        (0xBF4ECBD7, 0x03, 0x100DE642),   # mSheetKeepHoldFlag         bool
        (0xD6B4B271, 0x06, 0x100DE642),   # mSheetLifeType             u32
        (0x6CEB1104, 0x03, 0x0ABA2F93),   # mGtoBKeepHoldFlag          bool
    ],
    'nEffect::RadialBlurFilterBehavior': [
        (0x20067C8E, 0x40, None),   # lt                         float2
        (0x0093F600, 0x40, None),   # rb                         float2
    ],
}

# 仅由 DTI 提供字段表的 b_type。
DTI_EXTRA_BTYPES = {
    'nEffect::SpotLightBehavior': [
        (0x9074DE04, 0x06),   # mGroup                   u32
        (0x4A28ECBA, 0x06),   # mPriority                u32
        (0xD5089E67, 0x06),   # mColorCorrectNo          u32
        (0x9BE2D228, 0x0F),   # mColor                   color
        (0xB52636D6, 0x0C),   # mIntensity               f32
        (0xFC588D70, 0x0C),   # mMinRoughness            f32
        (0x5F18DFD2, 0x03),   # mDisp                    bool
        (0x94A79998, 0x03),   # mPrimary                 bool
        (0x577D419C, 0x03),   # mSyncEmitterColor        bool
        (0xF8860B8C, 0x03),   # mShadowCast              bool
        (0x8A747FCC, 0x06),   # mShadowMapSize           u32
        (0xA3D00CD9, 0x0C),   # mNearClipDistance        f32
        (0x21BA894E, 0x0C),   # mShadowDepthBias         f32
        (0x966B3A72, 0x0C),   # mShadowSlopedDepthBias   f32
        (0x07BDD7DB, 0x0C),   # mShadowMaxDepthBias      f32
        (0xEB5E1675, 0x03),   # mDoLPVInjection          bool
        (0x05E0C76B, 0x0C),   # mRadius                  f32
        (0x483CB738, 0x0C),   # mEffectiveRadius         f32
        (0x54F3132E, 0x0C),   # mCone                    f32
        (0x42290949, 0x0C),   # mSpread                  f32
        (0xCC8F5FE6, 0x0C),   # mFalloff                 f32
        (0x902C6057, 0x40),   # mProjectionScale         vector2
        (0x94AECC76, 0x40),   # mProjectionOffset        vector2
        (0x367AD10F, 0x0C),   # mProjectionRotationZ     f32
        (0xC83DF7A2, 0x80),   # mpTexture                custom
    ],
}


def _build():
    """合并目录与 DTI 补项，并返回 DTI-only key 集合。"""
    from .catalog import PTBEHAVIOR_CATALOG
    merged = {}
    dti_only = set()
    for b_type, params in PTBEHAVIOR_CATALOG.items():
        rows = list(params)
        for key, t, anchor in DTI_EXTRA_FIELDS.get(b_type, ()):
            pos = len(rows)
            if anchor is not None:
                for i, (k, _t, _f) in enumerate(rows):
                    if k == anchor:
                        pos = i + 1
                        break
            rows.insert(pos, (key, t, 0))
            dti_only.add((b_type, key))
        merged[b_type] = rows
    for b_type, params in DTI_EXTRA_BTYPES.items():
        merged[b_type] = [(k, t, 0) for k, t in params]
        for k, _t in params:
            dti_only.add((b_type, k))
    return merged, dti_only


PTBEHAVIOR_CATALOG_FULL, DTI_ONLY_KEYS = _build()


def is_dti_only(b_type: str, key: int) -> bool:
    """返回该键是否仅由 DTI 补项提供。"""
    return (b_type, key & 0xFFFFFFFF) in DTI_ONLY_KEYS
