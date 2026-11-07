"""See docs/internals/rtex-format.md."""
from .rtex_file import (
    DATA_DIRECTORY, MAGIC, TEXTURE_2D, RtexFile, RtexFormatError, field_kinds, is_rtex, read_rtex, value_names,
)

__all__ = ["DATA_DIRECTORY", "MAGIC", "TEXTURE_2D", "RtexFile", "RtexFormatError", "field_kinds", "is_rtex",
           "read_rtex", "value_names"]
