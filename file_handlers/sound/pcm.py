from __future__ import annotations

import wave
from dataclasses import dataclass

lazy import numpy as np


@dataclass(frozen=True, slots=True, eq=False)
class Pcm:
    samples: np.ndarray
    rate: int


def read_wav(path) -> Pcm:
    with wave.open(str(path), "rb") as source:
        samples = np.frombuffer(source.readframes(source.getnframes()), "<i2")
        return Pcm(samples.reshape(-1, source.getnchannels()), source.getframerate())


def write_wav(audio: Pcm, target) -> None:
    with wave.open(target, "wb") as output:
        output.setnchannels(audio.samples.shape[1])
        output.setsampwidth(2)
        output.setframerate(audio.rate)
        output.writeframes(audio.samples)
