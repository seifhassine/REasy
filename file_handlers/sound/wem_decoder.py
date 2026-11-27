from __future__ import annotations

from functools import cache

lazy import fastwem
lazy import numpy as np
lazy from utils.app_paths import resource_path

from .pcm import Pcm


@cache
def _codebooks() -> bytes:
    return resource_path("resources/data/sound/wwise_vorbis_codebooks.bin", required=True).read_bytes()


def decode_wem(wem: bytes) -> Pcm:
    pcm, channels, sample_rate = fastwem.decode(wem, _codebooks())
    if not pcm:
        raise ValueError("This WEM holds no audio.")
    return Pcm(np.frombuffer(pcm, "<i2").reshape(-1, channels), sample_rate)
