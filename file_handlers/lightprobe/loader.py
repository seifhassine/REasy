from __future__ import annotations

from .data import LightProbeData
from .lprb_parser import parse_lprb
from .prb_parser import parse_prb_v9


def parse_prb9_light_probe_data(
    *,
    prb_data: bytes,
    lprb_data: bytes,
    lprb_version: int | None = None,
) -> LightProbeData:
    data = LightProbeData(
        prb=parse_prb_v9(prb_data),
        lprb=parse_lprb(lprb_data, version=lprb_version),
    )
    data.validate()
    return data


def parse_prb9_lprb6_light_probe_data(
    *,
    prb_data: bytes,
    lprb_data: bytes,
) -> LightProbeData:
    return parse_prb9_light_probe_data(
        prb_data=prb_data,
        lprb_data=lprb_data,
        lprb_version=6,
    )
