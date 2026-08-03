from __future__ import annotations

import struct

from .mot_list import MotList, MotListV85Parser, MotListV85Writer
from .profiles import DMC5_PROFILE


class Dmc5MotionFormatCodec:
    """DMC5 MOTLIST v85 with embedded MOT v65, MotTree v4, and MotClip v27."""

    profile = DMC5_PROFILE

    def matches(self, data: bytes | bytearray | memoryview) -> bool:
        return (
            len(data) >= self.profile.motlist.header_size
            and struct.unpack_from("<I", data)[0] == self.profile.motlist.version
            and bytes(data[4:8]) == b"mlst"
        )

    def parse(
        self,
        data: bytes | bytearray | memoryview,
        *,
        label: str,
    ) -> MotList:
        return MotListV85Parser(self.profile).parse(data, label=label)

    def write(self, model: MotList) -> bytes:
        return MotListV85Writer(self.profile).build(model)


DMC5_MOTION_FORMAT_CODEC = Dmc5MotionFormatCodec()
