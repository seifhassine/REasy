from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from .mot_list.model import MotList

if TYPE_CHECKING:
    from .profiles import MotionFormatProfile


class MotionFormatCodec(Protocol):
    """A complete MOTLIST format family over shared semantic models."""

    profile: MotionFormatProfile

    def matches(self, data: bytes | bytearray | memoryview) -> bool: ...

    def parse(
        self,
        data: bytes | bytearray | memoryview,
        *,
        label: str,
    ) -> MotList: ...

    def write(self, model: MotList) -> bytes: ...
