from .clip_file import ClipFile
from .enums import CLIP_MAGIC, PropertyType
from .graph_operations import ClipGraphOperations
from .parser import ParsedClip
from .reader import ClipParserError

__all__ = [
    "ClipFile",
    "ClipHandler",
    "ClipGraphOperations",
    "ClipParserError",
    "ParsedClip",
    "CLIP_MAGIC",
    "PropertyType",
]


def __getattr__(name: str):
    if name == "ClipHandler":
        from .clip_handler import ClipHandler

        return ClipHandler
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
