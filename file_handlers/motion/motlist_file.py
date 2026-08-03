from __future__ import annotations

from .errors import MotionParseError, MotionWriteError
from .format_codec import MotionFormatCodec
from .format_registry import find_motion_format, require_motion_format
from .mot_list import MotList


class MotListFile:
    """Detect a codec when reading; require one for authored models."""

    def __init__(
        self,
        codec: MotionFormatCodec | None = None,
    ):
        self.codec = codec
        self.profile = codec.profile if codec is not None else None
        self._model: MotList | None = None

    @classmethod
    def can_handle(cls, data: bytes | bytearray | memoryview) -> bool:
        return find_motion_format(data) is not None

    def read(
        self,
        data: bytes | bytearray | memoryview,
        *,
        label: str = "MOTLIST",
    ) -> bool:
        codec = self.codec or require_motion_format(data)
        if not codec.matches(data):
            raise MotionParseError(
                f"{label}: data does not match {codec.profile.name}"
            )
        self.codec = codec
        self.profile = codec.profile
        self._model = codec.parse(data, label=label)
        return True

    def write(self) -> bytes:
        if self._model is None:
            raise MotionWriteError("no parsed MOTLIST model is available")
        if self.codec is None:
            raise MotionWriteError("no MOTLIST format codec is selected")
        return self.codec.write(self._model)

    @property
    def model(self) -> MotList:
        if self._model is None:
            raise MotionWriteError("MOTLIST file has not been parsed")
        return self._model

    @model.setter
    def model(self, value: MotList) -> None:
        self._model = value
