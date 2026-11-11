"""See docs/internals/gui-rendering.md, BlurFilter."""
from __future__ import annotations

import numpy as np

from file_handlers.engine_math import crt

F = np.float32


def level_sizes(pixels: tuple[int, int], rate: float, count: int) -> list[tuple[int, int]]:
    first = [F(int(crt("ceilf")(F(F(rate) * F(side))))) for side in pixels]
    return [tuple(int(max(F(1), F(crt("floorf")(F(side / F(1 << level)))))) for side in first)
            for level in range(count)]


def kernel_table(kernel, slots: int, last: float) -> list[F]:
    first, second = (F(value) for value in kernel["factors"])
    weights = [F(F(crt("expf")(F(F(F(at * at) * first) * second))) * F(kernel["scale"]))
               for at in range(kernel["taps"])]
    total = F(0)
    for at, weight in enumerate(weights):
        total = F(total + (F(weight * F(kernel["side"])) if at else weight))
    kept = [F(weight / total) for weight in weights[:kernel["kept"]]]
    return [*kept, *[F(0)] * (slots - len(kept) - 1), F(last)]


def chain_values(pixels: tuple[int, int], system, chain, slots: int) -> dict:
    scale = F(system["BlurFilterScale"])
    return {"blurSize": tuple(F(scale / F(side)) for side in pixels), "guiFilterBaseLuminance": chain["luminance"],
            "guiFilterGaussianKernelTbl": kernel_table(chain["kernel"], slots,
                                                       system["BlurFilterCuttingLuminanceLevel"])}
