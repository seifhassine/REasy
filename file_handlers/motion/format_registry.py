from __future__ import annotations

from .dmc5_codec import DMC5_MOTION_FORMAT_CODEC
from .errors import MotionParseError
from .format_codec import MotionFormatCodec


MOTION_FORMAT_CODECS: tuple[MotionFormatCodec, ...] = (
    DMC5_MOTION_FORMAT_CODEC,
)


def find_motion_format(
    data: bytes | bytearray | memoryview,
) -> MotionFormatCodec | None:
    return next(
        (codec for codec in MOTION_FORMAT_CODECS if codec.matches(data)),
        None,
    )


def require_motion_format(
    data: bytes | bytearray | memoryview,
) -> MotionFormatCodec:
    codec = find_motion_format(data)
    if codec is None:
        raise MotionParseError("unsupported MOTLIST format")
    return codec
