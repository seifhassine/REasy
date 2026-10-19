from dataclasses import dataclass
from pathlib import PurePosixPath
import struct
from typing import Callable

from utils.resource_file_utils import resource_version_from_path


class MmtrsFormatError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MmtrsFile:
    material_paths: tuple[str, ...]
    resource_version: int

    def __post_init__(self):
        if self.resource_version not in {221102761, 250109895}:
            raise MmtrsFormatError(f"Unverified MMTRS revision {self.resource_version}.")
        if not self.material_paths or len(self.material_paths) > 0xFFFFFFFF:
            raise MmtrsFormatError("An MMTRS needs an explicit nonempty LOD list.")
        for path in self.material_paths:
            normalized = path.replace("\\", "/")
            if (not normalized.casefold().endswith(".mmtr") or "\0" in path
                    or normalized.startswith("/") or ":" in normalized
                    or any(p in {"", ".", ".."} for p in normalized.split("/"))):
                raise MmtrsFormatError(f"Invalid MMTRS material resource path {path!r}.")

    @classmethod
    def from_bytes(cls, data: bytes, *, resource_version: int):
        if len(data) < 8:
            raise MmtrsFormatError("Truncated MMTRS header.")
        count, reserved = struct.unpack_from("<II", data)
        if reserved or count == 0 or 8 + count * 8 > len(data):
            raise MmtrsFormatError("Invalid MMTRS LOD count, pointer table or reserved field.")
        paths = []
        for index in range(count):
            offset = struct.unpack_from("<Q", data, 8 + index * 8)[0]
            if offset < 8 + count * 8 or offset % 2 or offset >= len(data):
                raise MmtrsFormatError(f"MMTRS LOD {index} has an invalid path pointer.")
            end = offset
            while end + 2 <= len(data) and data[end:end + 2] != b"\0\0":
                end += 2
            if end + 2 > len(data):
                raise MmtrsFormatError(f"MMTRS LOD {index} has an unterminated path.")
            try:
                paths.append(data[offset:end].decode("utf-16le"))
            except UnicodeError as exc:
                raise MmtrsFormatError(f"MMTRS LOD {index} is not UTF-16.") from exc
        result = cls(tuple(paths), resource_version)
        if result.write() != data:
            raise MmtrsFormatError("MMTRS string order, pointer interning or duplicate string copies differ from the verified layout.")
        return result

    def write(self) -> bytes:
        result = bytearray(struct.pack("<II", len(self.material_paths), 0))
        result.extend(b"\0" * (8 * len(self.material_paths)))
        offsets = {}
        for index, path in enumerate(self.material_paths):
            offset = offsets.setdefault(path, len(result))
            struct.pack_into("<Q", result, 8 + index * 8, offset)
            result.extend(path.encode("utf-16le") + b"\0\0")
        return bytes(result)

    def path_for_lod(self, shader_lod: int) -> str:
        if not 0 <= shader_lod < len(self.material_paths):
            raise MmtrsFormatError(f"Shader LOD {shader_lod} is outside this MMTRS.")
        return self.material_paths[shader_lod]


def is_mmtrs_resource(resource_path: str, actual_path: str) -> bool:
    return (resource_version_from_path(actual_path, "mmtrs") is not None
            or PurePosixPath(resource_path.replace("\\", "/")).name.casefold().endswith(".mmtrs"))


def mmtrs_family(found: tuple[str, bytes]) -> MmtrsFile:
    path, data = found
    return MmtrsFile.from_bytes(data, resource_version=resource_version_from_path(path, "mmtrs"))


def family_member(family: MmtrsFile, shader_lod: int,
                  loader: Callable[[str], tuple[str, bytes] | None]) -> tuple[str, bytes]:
    path = family.path_for_lod(shader_lod)
    result = loader(path)
    if result is None:
        raise FileNotFoundError(f"MMTRS shader LOD {shader_lod} was not found: {path}")
    return result


def resolve_mmtr_resource(resource_path: str,
                          loader: Callable[[str], tuple[str, bytes] | None]) -> tuple[str, bytes] | None:
    result = loader(resource_path)
    if result is None or not is_mmtrs_resource(resource_path, result[0]):
        return result
    return family_member(mmtrs_family(result), 0, loader)
