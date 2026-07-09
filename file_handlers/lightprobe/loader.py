from __future__ import annotations

from .data import LightProbeData
from .lprb_parser import parse_lprb_v6
from .prb_parser import parse_prb_v9


def parse_prb9_lprb6_light_probe_data(
    *,
    prb_data: bytes,
    lprb_data: bytes,
) -> LightProbeData:
    data = LightProbeData(
        prb=parse_prb_v9(prb_data),
        lprb=parse_lprb_v6(lprb_data),
    )
    data.validate()
    return data
