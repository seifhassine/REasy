"""See docs/internals/clip-format.md and clip-editor.md."""

from .decode import read_timeline
from .encode import write_timeline
from .layout import ClipFormatError
from .model import Clip, Key, Node, Property, ResourceKind, SpeedPoint, Timeline, Track
from .rules import InterpolationType, NodeType, PropertyType

__all__ = [
    "Clip",
    "ClipFormatError",
    "InterpolationType",
    "Key",
    "Node",
    "NodeType",
    "Property",
    "PropertyType",
    "ResourceKind",
    "SpeedPoint",
    "Timeline",
    "Track",
    "read_timeline",
    "write_timeline",
]
