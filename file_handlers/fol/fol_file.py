from __future__ import annotations

import struct
from dataclasses import dataclass, field


FOL_MAGIC = 0x004C4F46
MAX_FOL_GROUPS = 100_000

COMPACT_PROPERTY_VERSION = 230_913_255
MIXED_LAYOUT_VERSION = 240_718_001

V0_HEADER_SIZE = 0x18
MODERN_HEADER_SIZE = 0x30
V0_GROUP_SIZE = 0x20
COMPACT_GROUP_SIZE = 0x38
EXTENDED_GROUP_SIZE = 0x40
TRANSFORM_SIZE = 0x30


@dataclass(slots=True)
class FolAabb:
    min: tuple[float, float, float] = (0.0, 0.0, 0.0)
    max: tuple[float, float, float] = (0.0, 0.0, 0.0)


@dataclass(slots=True)
class FolTransform:
    position: tuple[float, float, float]
    rotation: tuple[float, float, float, float]
    scale: tuple[float, float, float]


@dataclass(slots=True)
class ExtendedFoliageUnitProperties:
    raw: int = 0
    unknownRenderGate0: int = 0
    unknownRenderGate1: int = 0
    unknownRenderGate2: int = 0
    unknownRenderGate3: int = 0
    unknownLodFlag: int = 0
    shadowCastMode: int = 0
    beautyMaskEnabled: int = 0
    beautyMaskChannel: int = 0
    allowDensityCulling: int = 0
    densityCullingRangeOverride: int = 0
    densityCullingRangeMultiply: int = 0
    ignoreGlobalDensity: int = 0
    reserved15: int = 0

    @classmethod
    def from_word(cls, raw: int) -> "ExtendedFoliageUnitProperties":
        return cls(
            raw=raw,
            unknownRenderGate0=raw & 1,
            unknownRenderGate1=(raw >> 1) & 1,
            unknownRenderGate2=(raw >> 2) & 1,
            unknownRenderGate3=(raw >> 3) & 1,
            unknownLodFlag=(raw >> 4) & 1,
            shadowCastMode=(raw >> 5) & 3,
            beautyMaskEnabled=(raw >> 7) & 1,
            beautyMaskChannel=(raw >> 8) & 7,
            allowDensityCulling=(raw >> 11) & 1,
            densityCullingRangeOverride=(raw >> 12) & 1,
            densityCullingRangeMultiply=(raw >> 13) & 1,
            ignoreGlobalDensity=(raw >> 14) & 1,
            reserved15=(raw >> 15) & 1,
        )


@dataclass(slots=True)
class FolInstanceGroup:
    index: int
    instance_count: int = 0
    mesh_path: str = ""
    material_path: str = ""
    aabb: FolAabb = field(default_factory=FolAabb)
    properties: ExtendedFoliageUnitProperties = field(default_factory=ExtendedFoliageUnitProperties)
    compactPropertyBits: int = 0
    unknown1E: int = 0
    legacy_padding04: int = 0
    densityCullingNear: float = 0.0
    densityCullingFar: float = 0.0
    transforms: list[FolTransform] = field(default_factory=list)


@dataclass(slots=True)
class FolFile:
    version: int = 0
    aabb: FolAabb = field(default_factory=FolAabb)
    padding_0c: int = 0
    padding_24: int = 0
    uses_extended_group_layout: bool = False
    groups: list[FolInstanceGroup] = field(default_factory=list)

    def read(self, data: bytes | bytearray) -> bool:
        data = bytes(data)
        self.groups.clear()
        self.aabb = FolAabb()
        self.padding_0c = 0
        self.padding_24 = 0
        self.uses_extended_group_layout = False

        if len(data) < 12:
            return False

        magic, self.version, group_count = struct.unpack_from("<III", data)
        if magic != FOL_MAGIC:
            return False
        if group_count > MAX_FOL_GROUPS:
            raise ValueError(f"Invalid FOL group count: {group_count}")

        if self.version == 0:
            self._read_v0(data, group_count)
        else:
            self._read_modern(data, group_count)
        return True

    def _read_v0(self, data: bytes, group_count: int) -> None:
        if len(data) < V0_HEADER_SIZE:
            raise ValueError("FOL v0 header is truncated")

        self.padding_0c = struct.unpack_from("<I", data, 0x0C)[0]
        group_offset = struct.unpack_from("<Q", data, 0x10)[0] or V0_HEADER_SIZE
        self._require_range(data, group_offset, group_count * V0_GROUP_SIZE, "FOL v0 group table")

        for index in range(group_count):
            offset = group_offset + index * V0_GROUP_SIZE
            count, padding04 = struct.unpack_from("<II", data, offset)
            transform_offset, mesh_offset, material_offset = struct.unpack_from("<QQQ", data, offset + 8)
            self.groups.append(
                FolInstanceGroup(
                    index=index + 1,
                    instance_count=count,
                    mesh_path=self._read_wstring(data, mesh_offset),
                    material_path=self._read_wstring(data, material_offset),
                    legacy_padding04=padding04,
                    transforms=self._read_transforms(data, transform_offset, count),
                )
            )

    def _read_modern(self, data: bytes, group_count: int) -> None:
        if len(data) < MODERN_HEADER_SIZE:
            raise ValueError("FOL v3+ header is truncated")

        self.aabb = FolAabb(
            min=struct.unpack_from("<3f", data, 0x0C),
            max=struct.unpack_from("<3f", data, 0x18),
        )
        self.padding_24 = struct.unpack_from("<I", data, 0x24)[0]
        group_offset = struct.unpack_from("<Q", data, 0x28)[0] or MODERN_HEADER_SIZE
        self.uses_extended_group_layout = self._uses_extended_group_layout(data, group_count, group_offset)

        group_size = EXTENDED_GROUP_SIZE if self.uses_extended_group_layout else COMPACT_GROUP_SIZE
        self._require_range(data, group_offset, group_count * group_size, "FOL instance group table")

        for index in range(group_count):
            self.groups.append(self._read_modern_group(data, index + 1, group_offset + index * group_size))

    def _read_modern_group(self, data: bytes, index: int, offset: int) -> FolInstanceGroup:
        count = struct.unpack_from("<I", data, offset)[0]
        aabb = FolAabb(
            min=struct.unpack_from("<3f", data, offset + 4),
            max=struct.unpack_from("<3f", data, offset + 16),
        )

        compact_property_bits = 0
        if self.uses_extended_group_layout:
            property_bits, unknown1e = struct.unpack_from("<HH", data, offset + 0x1C)
            density_near, density_far = struct.unpack_from("<2f", data, offset + 0x20)
            ptr_offset = offset + 0x28
        else:
            property_bits = unknown1e = 0
            density_near = density_far = 0.0
            compact_property_bits = struct.unpack_from("<I", data, offset + 0x1C)[0]
            ptr_offset = offset + 0x20

        transform_offset, mesh_offset, material_offset = struct.unpack_from("<QQQ", data, ptr_offset)
        return FolInstanceGroup(
            index=index,
            instance_count=count,
            mesh_path=self._read_wstring(data, mesh_offset),
            material_path=self._read_wstring(data, material_offset),
            aabb=aabb,
            properties=ExtendedFoliageUnitProperties.from_word(property_bits),
            compactPropertyBits=0 if self.uses_extended_group_layout else compact_property_bits,
            unknown1E=unknown1e,
            densityCullingNear=density_near,
            densityCullingFar=density_far,
            transforms=self._read_transforms(data, transform_offset, count),
        )

    def _read_transforms(self, data: bytes, offset: int, count: int) -> list[FolTransform]:
        if count == 0 or offset == 0:
            return []

        self._require_range(data, offset, count * TRANSFORM_SIZE, "FOL transform table")
        return [
            FolTransform(
                position=struct.unpack_from("<3f", data, current),
                rotation=struct.unpack_from("<4f", data, current + 16),
                scale=struct.unpack_from("<3f", data, current + 32),
            )
            for current in range(offset, offset + count * TRANSFORM_SIZE, TRANSFORM_SIZE)
        ]

    def _uses_extended_group_layout(self, data: bytes, group_count: int, group_offset: int) -> bool:
        if self.version <= 3 or self.version == COMPACT_PROPERTY_VERSION:
            return False
        if self.version == MIXED_LAYOUT_VERSION:
            return self._first_group_looks_extended(data, group_count, group_offset)
        return True

    @staticmethod
    def _first_group_looks_extended(data: bytes, group_count: int, group_offset: int) -> bool:
        if group_count == 0:
            return True
        if group_offset + 0x28 > len(data):
            return True

        value20, value24 = struct.unpack_from("<II", data, group_offset + 0x20)
        return value20 == 0 or value24 != 0

    @staticmethod
    def _require_range(data: bytes, offset: int, size: int, label: str) -> None:
        if offset < 0 or size < 0 or offset + size > len(data):
            raise ValueError(f"{label} is truncated")

    @staticmethod
    def _read_wstring(data: bytes, offset: int) -> str:
        if offset == 0:
            return ""
        if offset < 0 or offset >= len(data) or offset % 2:
            raise ValueError(f"Invalid FOL string offset: 0x{offset:X}")

        end = offset
        while end + 1 < len(data):
            if data[end] == 0 and data[end + 1] == 0:
                return data[offset:end].decode("utf-16le")
            end += 2

        raise ValueError(f"Unterminated FOL string at 0x{offset:X}")
