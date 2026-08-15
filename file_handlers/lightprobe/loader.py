from __future__ import annotations

from .data import LightProbeData
from .lprb_parser import parse_lprb
from .prb_parser import parse_prb


def parse_light_probe_data(
    *,
    prb_data: bytes,
    lprb_data: bytes,
    prb_version: int | None = None,
    lprb_version: int | None = None,
) -> LightProbeData:
    data = LightProbeData(
        prb=parse_prb(prb_data, version=prb_version),
        lprb=parse_lprb(lprb_data, version=lprb_version),
    )
    data.validate()
    return data
