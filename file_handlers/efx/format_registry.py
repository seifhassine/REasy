"""Select an EFX codec by container signature and exact resource version."""
from dataclasses import dataclass
import struct

from utils.resource_file_utils import resource_version_from_path
from .efx_file import EFX_MAGIC, EfxFile, EfxParseError, schema_for_version
from .mhw_efx_file import MHW_EFX_MAGIC, MHW_EFX_VERSIONS, MhwEfxFile


@dataclass(frozen=True)
class EfxProfile:
    format: str
    version: int


def identify_efx(data: bytes, filepath: str = "") -> EfxProfile:
    filename_version = resource_version_from_path(filepath, "efx")
    if data[:4] == MHW_EFX_MAGIC:
        if len(data) < 72:
            raise EfxParseError("Truncated MHW EFX header")
        version = struct.unpack_from("<i", data, 4)[0]
        if filename_version is not None and filename_version != version:
            raise EfxParseError(f"EFX filename version {filename_version} differs from MHW header version {version}")
        if version not in MHW_EFX_VERSIONS:
            raise EfxParseError(f"Unsupported MHW EFX version {version}")
        return EfxProfile("MHW_EFX", version)
    if data[:4] == EFX_MAGIC:
        if filename_version is None:
            raise EfxParseError("RE Engine EFX requires a numeric file version: name.efx.<version>")
        schema_for_version(filename_version)
        return EfxProfile("RE_EFX", filename_version)
    raise EfxParseError("Invalid EFX container signature")


def read_efx(data: bytes, filepath: str = ""):
    profile = identify_efx(data, filepath)
    codec = MhwEfxFile if profile.format == "MHW_EFX" else EfxFile
    return codec.from_bytes(data, version=profile.version)
