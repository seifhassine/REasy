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


def parse_prb9_light_probe_data(
    *,
    prb_data: bytes,
    lprb_data: bytes,
    lprb_version: int | None = None,
) -> LightProbeData:
    return parse_light_probe_data(
        prb_data=prb_data,
        lprb_data=lprb_data,
        prb_version=9,
        lprb_version=lprb_version,
    )


def parse_prb10_light_probe_data(
    *,
    prb_data: bytes,
    lprb_data: bytes,
    lprb_version: int | None = None,
) -> LightProbeData:
    return parse_light_probe_data(
        prb_data=prb_data,
        lprb_data=lprb_data,
        prb_version=10,
        lprb_version=lprb_version,
    )


def parse_prb11_light_probe_data(
    *,
    prb_data: bytes,
    lprb_data: bytes,
    lprb_version: int | None = None,
) -> LightProbeData:
    return parse_light_probe_data(
        prb_data=prb_data,
        lprb_data=lprb_data,
        prb_version=11,
        lprb_version=lprb_version,
    )


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
