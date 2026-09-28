from __future__ import annotations

from pathlib import Path
from typing import Iterator

from .codec import GcfCodec, decode_gcf
from .model import GcfData, GcfLayout, GcfResourceReference
from .profiles import GCF_MAGIC


class GcfFile:
    def __init__(self) -> None:
        self.model: GcfData | None = None
        self.layout: GcfLayout | None = None
        self.codec: GcfCodec | None = None

    @staticmethod
    def can_handle(data: bytes) -> bool:
        return len(data) >= 8 and data[4:8] == GCF_MAGIC

    @classmethod
    def from_bytes(cls, data: bytes) -> "GcfFile":
        result = cls()
        result.read(data)
        return result

    @classmethod
    def from_path(cls, path: str | Path) -> "GcfFile":
        return cls.from_bytes(Path(path).read_bytes())

    def read(self, data: bytes) -> bool:
        self.model, self.layout, self.codec = decode_gcf(bytes(data))
        return True

    def write(self) -> bytes:
        model = self.require_model()
        if self.codec is None:
            raise ValueError("no GCF codec is available for rebuild")
        return self.codec.write(model)

    def require_model(self) -> GcfData:
        if self.model is None:
            raise ValueError("no GCF data is loaded")
        return self.model

    def iter_resource_references(self) -> Iterator[GcfResourceReference]:
        return self.require_model().iter_resource_references()


def parse_gcf(data: bytes) -> GcfData:
    return GcfFile.from_bytes(data).require_model()


def parse_gcf_file(path: str | Path) -> GcfData:
    return GcfFile.from_path(path).require_model()
