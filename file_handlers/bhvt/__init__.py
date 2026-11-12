"""See docs/internals/bhvt-format.md and bhvt-editor.md."""

from .errors import BhvtFormatError
from .files import MAGICS, Resource, file_kind, read_resource, write_resource
from .rsz_blocks import RszBlocks

__all__ = [
    "BhvtFormatError",
    "MAGICS",
    "Resource",
    "RszBlocks",
    "file_kind",
    "read_resource",
    "write_resource",
]
