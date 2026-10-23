"""DTI 声明但未纳入常规表的 ``TLP → DT`` 映射。

维护约束：
- 本表并入调色板，扩展其可选参数范围，不表示相关轨道已验证可用。
- 标量 dataType 映射为 TIML 类型；向量参数目前以 Float 表示，其通道布局待确认。
"""

DTI_TLP_EXTRA = {
    0x4A0D2B6A: [  # nEffect::nTimelineParam::Life，+1
        (0xBD8D5203, 1)
    ],
    0x6DA6E5D1: [  # nEffect::nTimelineParam::MhPointLightBehavior，+6
        (0x6F9CBFFB, 1), (0xEBCB0929, 4), (0x27072CE5, 4), (0xD9DC442F, 4), (0x642F418E, 1),
        (0x53FE943E, 1)
    ],
    0x75963575: [  # nEffect::nTimelineParam::MhSpotLightBehavior，+9
        (0xD0A18282, 2), (0x9B4FE73B, 2), (0xB224CB11, 1), (0x6F9CBFFB, 1), (0xEBCB0929, 4),
        (0x27072CE5, 4), (0xD9DC442F, 4), (0x642F418E, 1), (0x53FE943E, 1)
    ],
    0x42E48DDE: [  # nEffect::nTimelineParam::PointLightBehavior，+6
        (0x6F9CBFFB, 1), (0xEBCB0929, 4), (0x27072CE5, 4), (0xD9DC442F, 4), (0x642F418E, 1),
        (0x53FE943E, 1)
    ],
    0x582BA062: [  # nEffect::nTimelineParam::RadialBlurFilterBehavior，+12
        (0x7CEFC343, 2), (0x9B82855B, 4), (0x2041145D, 4), (0x1617730B, 4), (0x74FE2DCA, 2),
        (0x546BA744, 2), (0xCFB46EA6, 2), (0x29D09114, 1), (0xC807176A, 1), (0xF248E184, 1),
        (0x642F418E, 1), (0x91BD84E5, 1)
    ],
    0x3DE576DC: [  # nEffect::nTimelineParam::SpotLightBehavior，+9
        (0xD0A18282, 2), (0x9B4FE73B, 2), (0xB224CB11, 1), (0x6F9CBFFB, 1), (0xEBCB0929, 4),
        (0x27072CE5, 4), (0xD9DC442F, 4), (0x642F418E, 1), (0x53FE943E, 1)
    ],
    0x7F140A1E: [  # nTimelineParam::AnimalCommon，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x170FACE6: [  # nTimelineParam::AnimalFly，+19
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xB15BCB52, 2), (0xC65CFBC4, 2), (0x5F55AA7E, 2)
    ],
    0x32FA69E8: [  # nTimelineParam::AnimalSeasonEvent，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x20FC82E0: [  # nTimelineParam::ClawMotionVisual，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x0A5CFC32: [  # nTimelineParam::CollisionSyncUID，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x38FF82EA: [  # nTimelineParam::CollisionTimelineObject，+25
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xE9CAA735, 1), (0xF6CC7B92, 0), (0xEFD74AD3, 0), (0xC4FA1910, 0),
        (0xDDE12851, 0), (0x92A0BE96, 0), (0x8BBB8FD7, 0), (0xA096DC14, 0), (0xB98DED55, 0)
    ],
    0x54035342: [  # nTimelineParam::EffectBank，+2
        (0xA99225B2, 4), (0x7297D41B, 1)
    ],
    0x6B32DCF6: [  # nTimelineParam::EffectParameter1，+48
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1),
        (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1),
        (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1),
        (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1),
        (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1),
        (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1),
        (0x96391491, 1), (0x96391491, 1), (0x96391491, 1)
    ],
    0x723B8D4C: [  # nTimelineParam::EffectParameter2，+48
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1),
        (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1),
        (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1),
        (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1),
        (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1),
        (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1),
        (0x96391491, 1), (0x96391491, 1), (0x96391491, 1)
    ],
    0x053CBDDA: [  # nTimelineParam::EffectParameter3，+48
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1),
        (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1),
        (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1),
        (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1),
        (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1),
        (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1),
        (0x96391491, 1), (0x96391491, 1), (0x96391491, 1)
    ],
    0x1B582879: [  # nTimelineParam::EffectParameter4，+48
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1),
        (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1),
        (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1),
        (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1),
        (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1),
        (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1), (0x96391491, 1),
        (0x96391491, 1), (0x96391491, 1), (0x96391491, 1)
    ],
    0x7E68607B: [  # nTimelineParam::Em001Motion，+18
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xF3EF1393, 0), (0x60C6D7FF, 0)
    ],
    0x1DB85541: [  # nTimelineParam::Em007Motion，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x255D71CC: [  # nTimelineParam::Em013Motion，+24
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xAA8B5CF2, 2), (0x49C1939E, 0), (0xE75C401C, 2), (0x1D87DAD0, 2),
        (0xE5BD1D53, 1), (0xE09D3A5D, 1), (0xC8ADC4D9, 2), (0xE0EE074D, 2)
    ],
    0x2BD2762F: [  # nTimelineParam::Em023Motion，+17
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xA85E3EFD, 2)
    ],
    0x79EA5988: [  # nTimelineParam::Em026Motion，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x5F9D523C: [  # nTimelineParam::Em027Motion，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x4BCA741C: [  # nTimelineParam::Em042Motion，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x6DBD7FA8: [  # nTimelineParam::Em043Motion，+17
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xB49F8054, 1)
    ],
    0x0E6D4A92: [  # nTimelineParam::Em045Motion，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x55585B25: [  # nTimelineParam::Em057Motion，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x2F9878D5: [  # nTimelineParam::Em063Motion，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x52CCD16F: [  # nTimelineParam::Em063_05Motion，+17
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0x504F8D1C, 2)
    ],
    0x0BFA707A: [  # nTimelineParam::Em080Motion，+17
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xC4045404, 0)
    ],
    0x69137438: [  # nTimelineParam::Em101Motion，+17
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0x65637508, 0)
    ],
    0x58FB6EA5: [  # nTimelineParam::Em102Motion，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x3A51186E: [  # nTimelineParam::Em102_01Motion，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x7E8C6511: [  # nTimelineParam::Em103Motion，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x3B2B5B9F: [  # nTimelineParam::Em104Motion，+19
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xE75C401C, 2), (0x1D87DAD0, 2), (0xE5BD1D53, 1)
    ],
    0x2CB44AB6: [  # nTimelineParam::Em106Motion，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x0AC34102: [  # nTimelineParam::Em107Motion，+17
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0x1560F174, 2)
    ],
    0x5AFC3A5F: [  # nTimelineParam::Em109Motion，+18
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0x172C9CF3, 0), (0x271DCC9F, 0)
    ],
    0x03CE7F12: [  # nTimelineParam::Em110Motion，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x7F2A1793: [  # nTimelineParam::Em110_01Motion，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x25B974A6: [  # nTimelineParam::Em111Motion，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x64A758BE: [  # nTimelineParam::Em111_05Motion，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x14516E3B: [  # nTimelineParam::Em112Motion，+17
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xDA6B2E45, 0)
    ],
    0x141DAC90: [  # nTimelineParam::Em113_01Motion，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x77815B01: [  # nTimelineParam::Em114Motion，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x6D4CF8C4: [  # nTimelineParam::Em115_05Motion，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x601E4A28: [  # nTimelineParam::Em116Motion，+17
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0x4BC86013, 0)
    ],
    0x4669419C: [  # nTimelineParam::Em117Motion，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x30213175: [  # nTimelineParam::Em118Motion，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x18B27374: [  # nTimelineParam::Em118_05Motion，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x0D4178F1: [  # nTimelineParam::Em120Motion，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x3CA9626C: [  # nTimelineParam::Em123Motion，+17
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0x94E1233B, 2)
    ],
    0x790E5CE2: [  # nTimelineParam::Em124Motion，+18
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xE51E6C7E, 0), (0xBEB009A4, 2)
    ],
    0x5F795756: [  # nTimelineParam::Em125Motion，+18
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xFC8FEE16, 2), (0xBEA91798, 2)
    ],
    0x6E914DCB: [  # nTimelineParam::Em126Motion，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x48E6467F: [  # nTimelineParam::Em127Motion，+18
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0x1CF4021F, 1), (0x49BC7F9E, 2)
    ],
    0x1E83FE5F: [  # nTimelineParam::EmCharmMountStepObject，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x658D8235: [  # nTimelineParam::EmClawRejectCollisionObject，+25
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0x88377382, 0), (0x6C8AD191, 0), (0x6C8AD191, 0), (0x6C8AD191, 0),
        (0x6C8AD191, 0), (0x6C8AD191, 0), (0x6C8AD191, 0), (0x6C8AD191, 0), (0x6C8AD191, 0)
    ],
    0x54800017: [  # nTimelineParam::EmCreateGmMotion，+17
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0x65FE16A3, 0)
    ],
    0x77519ACB: [  # nTimelineParam::EmMotionCommon，+37
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xFB7A60E1, 2), (0x924E2ECA, 2), (0xCDF59E99, 0), (0x32383A91, 0),
        (0x0CD126D3, 0), (0x370EE2E4, 0), (0xE707016F, 1), (0x5441583F, 0), (0x14C6D9F3, 1),
        (0xC4CF3A78, 1), (0x451DF0C8, 0), (0x7F6F6C74, 0), (0x2C8CA4F4, 0), (0x03EB2065, 0),
        (0x55837D89, 2), (0x0F9B4945, 0), (0x42661269, 2), (0x1FCA0A8D, 0), (0xFC8FEE16, 1),
        (0xBEA91798, 2), (0xFFBE0540, 0)
    ],
    0x79746285: [  # nTimelineParam::EmMotionVisual，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x45CB6C2B: [  # nTimelineParam::Ems005_01Motion，+17
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0x8BF38DDE, 0)
    ],
    0x7E51F5BD: [  # nTimelineParam::LightTimelineParam，+23
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xC6996477, 3), (0x3E8A26F4, 2), (0x0849B8BF, 2), (0xA0E0C79D, 2),
        (0x5380ADAC, 2), (0x6BC3DC74, 2), (0xE4A7326D, 2)
    ],
    0x6DC8D36C: [  # nTimelineParam::MatAnimPlayer，+28
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xAA491F31, 1), (0xD5442650, 0), (0xF32AD15F, 0), (0x24E70B9A, 0),
        (0xBA839E39, 0), (0xCD84AEAF, 0), (0xFD0F3D48, 0), (0x636BA8EB, 0), (0x146C987D, 0),
        (0x364F3A0B, 0), (0xA82BAFA8, 0), (0xDF2C9F3E, 0)
    ],
    0x3E6BDB12: [  # nTimelineParam::ModelPartsCtrl，+42
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xF99F07B2, 1), (0x5A0DE711, 1), (0xFA25C3BC, 1), (0xCA145DDA, 1),
        (0x0F776DDF, 0), (0x78705D49, 0), (0xE1790CF3, 0), (0x967E3C65, 0), (0x75913B41, 0),
        (0x02960BD7, 0), (0x9B9F5A6D, 0), (0xEC986AFB, 0), (0x72FCFF58, 0), (0x05FBCFCE, 0),
        (0x9CF29E74, 0), (0xEBF5AEE2, 0), (0x1775AD87, 0), (0x60729D11, 0), (0x1018699E, 0),
        (0x081AA9C6, 0), (0x9B9E9E73, 0), (0xEC99AEE5, 0), (0x9CF35A6A, 0), (0x7F1D9950, 0),
        (0xE614C8EA, 0), (0x9113F87C, 0)
    ],
    0x233BDD27: [  # nTimelineParam::NpcCommon，+24
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0x904311C9, 0), (0x2FD9ABEC, 2), (0x92D29DBD, 2), (0x1C1E3AAE, 2),
        (0x52E4F54C, 2), (0x6EE9CA15, 2), (0x384E29E0, 2), (0x044316B9, 2)
    ],
    0x1830887C: [  # nTimelineParam::OtasukeMotion，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x575E6887: [  # nTimelineParam::OtomoMotion，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x791C240E: [  # nTimelineParam::PhotomoCommon，+21
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xB47D5686, 0), (0xBD2E495A, 0), (0x82322C64, 0), (0xAB0939E7, 0),
        (0x6A87E627, 0)
    ],
    0x48D6114F: [  # nTimelineParam::PlMotionCommon，+18
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0x5D65F6FA, 0), (0xCE103825, 1)
    ],
    0x6FCCD10E: [  # nTimelineParam::PlMotionInput，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x46F3E901: [  # nTimelineParam::PlMotionVisual，+23
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0x3D34EC7A, 0), (0x242FDD3B, 0), (0x0F028EF8, 0), (0x81B0505C, 2),
        (0x98AB611D, 2), (0xB38632DE, 2), (0x780B19AB, 2)
    ],
    0x49DFD557: [  # nTimelineParam::PugeeMotion，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x5F02205C: [  # nTimelineParam::ShellAnimation，+23
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0x11F2BF45, 2), (0x8D647162, 2), (0x776B4C01, 2), (0xE0DF00FA, 2),
        (0x97D8306C, 2), (0x47AFAD03, 0), (0x1339AA3D, 2)
    ],
    0x5C648E63: [  # nTimelineParam::ShellCreate，+30
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0x05CD69B3, 1), (0x7DC7FDC0, 1), (0x673E4986, 0), (0xEA8B9678, 2),
        (0x9D8CA6EE, 2), (0x0485F754, 2), (0xCE081C66, 2), (0xB90F2CF0, 2), (0x20067D4A, 2),
        (0x5470E3F1, 2), (0x2377D367, 2), (0xFF84A8F4, 2), (0x88839862, 2), (0x118AC9D8, 2)
    ],
    0x7E9DBB98: [  # nTimelineParam::ShellMultiCreate，+32
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0x8EA18C57, 1), (0x8EA18C57, 1), (0x8EA18C57, 1), (0x8EA18C57, 1),
        (0x8EA18C57, 1), (0x8EA18C57, 1), (0x8EA18C57, 1), (0x8EA18C57, 1), (0x8EA18C57, 1),
        (0x8EA18C57, 1), (0x8EA18C57, 1), (0x8EA18C57, 1), (0x8EA18C57, 1), (0x8EA18C57, 1),
        (0x8EA18C57, 1), (0x8EA18C57, 1)
    ],
    0x0886EDEB: [  # nTimelineParam::ShellMultiCreate::Shell，+17
        (0x05CD69B3, 1), (0x7DC7FDC0, 1), (0x673E4986, 0), (0xF7B710A3, 0), (0xEA8B9678, 2),
        (0x9D8CA6EE, 2), (0x0485F754, 2), (0xCE081C66, 2), (0xB90F2CF0, 2), (0x20067D4A, 2),
        (0x5470E3F1, 2), (0x2377D367, 2), (0xFF84A8F4, 2), (0x88839862, 2), (0x118AC9D8, 2),
        (0x65E36419, 2), (0x29727DBC, 2)
    ],
    0x17EAA6E5: [  # nTimelineParam::SpeedTreeWindGenerator，+28
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0x902650A5, 0), (0x893D61E4, 0), (0xA2103227, 0), (0xBB0B0366, 0),
        (0x663311AB, 0), (0x7F2820EA, 0), (0x54057329, 0), (0x4D1E4268, 0), (0x85503B7A, 0),
        (0x9C4B0A3B, 0), (0xB76659F8, 0), (0xAE7D68B9, 0)
    ],
    0x4C119332: [  # nTimelineParam::cCharaMotion，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x0CFD985C: [  # nTimelineParam::nWwiseTimeline::EventCollision00，+42
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0x80BC0FEE, 1), (0x1003127F, 1), (0x670422E9, 1), (0xFE0D7353, 1),
        (0x890A43C5, 1), (0x176ED666, 1), (0x6069E6F0, 1), (0xF960B74A, 1), (0xC92ED29E, 1),
        (0x4426DF6B, 1), (0x3321EFFD, 1), (0xAA28BE47, 1), (0xDD2F8ED1, 1), (0xF2C6CF60, 1),
        (0x6279D2F1, 1), (0x157EE267, 1), (0x8C77B3DD, 1), (0xFB70834B, 1), (0x651416E8, 1),
        (0x1213267E, 1), (0x8B1A77C4, 1), (0xB0E9AE14, 1), (0x3DE1A3E1, 1), (0x4AE69377, 1),
        (0xD3EFC2CD, 1), (0xA4E8F25B, 1)
    ],
    0x7BFAA8CA: [  # nTimelineParam::nWwiseTimeline::EventCollision01，+47
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xBCFFC41B, 0), (0x80BC0FEE, 1), (0x1003127F, 1), (0x670422E9, 1),
        (0xFE0D7353, 1), (0x890A43C5, 1), (0x176ED666, 1), (0xC92ED29E, 1), (0x4426DF6B, 1),
        (0x3321EFFD, 1), (0xAA28BE47, 1), (0xDD2F8ED1, 1), (0x434B1B72, 1), (0x344C2BE4, 1),
        (0xAD457A5E, 1), (0xDA424AC8, 1), (0xF2C6CF60, 1), (0x6279D2F1, 1), (0x157EE267, 1),
        (0x8C77B3DD, 1), (0xFB70834B, 1), (0x651416E8, 1), (0xB0E9AE14, 1), (0x3DE1A3E1, 1),
        (0x4AE69377, 1), (0xD3EFC2CD, 1), (0xA4E8F25B, 1), (0x3A8C67F8, 1), (0x4D8B576E, 1),
        (0xD48206D4, 1), (0x15EF861B, 1)
    ],
    0x62F3F970: [  # nTimelineParam::nWwiseTimeline::EventCollision02，+49
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xBCFFC41B, 0), (0x80BC0FEE, 1), (0x1003127F, 1), (0x670422E9, 1),
        (0xFE0D7353, 1), (0x890A43C5, 1), (0x176ED666, 1), (0x6069E6F0, 1), (0xF960B74A, 1),
        (0xC92ED29E, 1), (0x4426DF6B, 1), (0x3321EFFD, 1), (0xAA28BE47, 1), (0xDD2F8ED1, 1),
        (0x434B1B72, 1), (0x344C2BE4, 1), (0xAD457A5E, 1), (0xF2C6CF60, 1), (0x6279D2F1, 1),
        (0x157EE267, 1), (0x8C77B3DD, 1), (0xFB70834B, 1), (0x651416E8, 1), (0x1213267E, 1),
        (0x8B1A77C4, 1), (0xB0E9AE14, 1), (0x3DE1A3E1, 1), (0x4AE69377, 1), (0xD3EFC2CD, 1),
        (0xA4E8F25B, 1), (0x3A8C67F8, 1), (0x4D8B576E, 1), (0xD48206D4, 1)
    ],
    0x15F4C9E6: [  # nTimelineParam::nWwiseTimeline::EventCollision03，+46
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0x80BC0FEE, 1), (0x1003127F, 1), (0x670422E9, 1), (0xFE0D7353, 1),
        (0x890A43C5, 1), (0x176ED666, 1), (0xC92ED29E, 1), (0x4426DF6B, 1), (0x3321EFFD, 1),
        (0xAA28BE47, 1), (0xDD2F8ED1, 1), (0x434B1B72, 1), (0xAD457A5E, 1), (0xDA424AC8, 1),
        (0x4AFD5759, 1), (0xF2C6CF60, 1), (0x6279D2F1, 1), (0x157EE267, 1), (0x8C77B3DD, 1),
        (0xFB70834B, 1), (0x651416E8, 1), (0xB0E9AE14, 1), (0x3DE1A3E1, 1), (0x4AE69377, 1),
        (0xD3EFC2CD, 1), (0xA4E8F25B, 1), (0x3A8C67F8, 1), (0xD48206D4, 1), (0x15EF861B, 1),
        (0x85509B8A, 1)
    ],
    0x4BB01F7E: [  # nTimelineParam::nWwiseTimeline::EventGround，+25
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xBCFFC41B, 0), (0x0354825D, 1), (0x3A333306, 1), (0x7BBD42D2, 1),
        (0x42DAF389, 1), (0xCCDA87A2, 1), (0xA8D81E41, 1), (0x22D3AAEA, 1), (0x46D13309, 1)
    ],
    0x59C0CAA2: [  # nTimelineParam::nWwiseTimeline::EventGroup00，+17
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xBCFFC41B, 0)
    ],
    0x2EC7FA34: [  # nTimelineParam::nWwiseTimeline::EventGroup01，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x37CEAB8E: [  # nTimelineParam::nWwiseTimeline::EventGroup02，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x40C99B18: [  # nTimelineParam::nWwiseTimeline::EventGroup03，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x5EAD0EBB: [  # nTimelineParam::nWwiseTimeline::EventGroup04，+17
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0x9F91C19A, 1)
    ],
    0x29AA3E2D: [  # nTimelineParam::nWwiseTimeline::EventGroup05，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x30A36F97: [  # nTimelineParam::nWwiseTimeline::EventGroup06，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x47A45F01: [  # nTimelineParam::nWwiseTimeline::EventGroup07，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x571B4290: [  # nTimelineParam::nWwiseTimeline::EventGroup08，+17
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0x9F91C19A, 1)
    ],
    0x201C7206: [  # nTimelineParam::nWwiseTimeline::EventGroup09，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x40DBFBE3: [  # nTimelineParam::nWwiseTimeline::EventGroup10，+17
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0x9F91C19A, 1)
    ],
    0x24006667: [  # nTimelineParam::nWwiseTimeline::EventLoop，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
    0x01739779: [  # nTimelineParam::nWwiseTimeline::GameParameter，+20
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xDCD68A5C, 0), (0x4C6997CD, 0), (0x3B6EA75B, 0), (0xCAC86BE8, 2)
    ],
    0x0BD98B96: [  # nTimelineParam::nWwiseTimeline::Switch，+16
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1),
        (0xF71AAE71, 1), (0xF71AAE71, 1), (0xF71AAE71, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1), (0xD31238C4, 1),
        (0xD31238C4, 1)
    ],
}
