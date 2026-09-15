"""Semantic codecs and runtime-facing adapters for RE Engine motion.

The package intentionally does not reuse the standalone CLIP/TML codec.  A
MOT-owned compact MotClip has different roots, pointer bases, and layout rules.

Binary codecs depend only on semantic format models and version profiles.
``evaluation`` consumes those models to bind and sample a target rig. Runtime
preview concerns never feed storage choices back into a writer.
"""

from .dmc5_codec import DMC5_MOTION_FORMAT_CODEC
from .format_codec import MotionFormatCodec
from .format_registry import find_motion_format
from .motlist_file import MotListFile
from .profiles import DMC5_PROFILE, MotionFormatProfile

__all__ = [
    "DMC5_MOTION_FORMAT_CODEC",
    "DMC5_PROFILE",
    "MotionFormatCodec",
    "MotionFormatProfile",
    "MotListFile",
    "find_motion_format",
]
