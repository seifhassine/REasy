from __future__ import annotations

import struct
from dataclasses import dataclass, field, fields

import numpy as np

STRANDS_MAGIC = b"STRD"
HEADER_SIZES = {20: 188, 31: 244, 33: 292}
SUPPORTED_VERSIONS = tuple(HEADER_SIZES)
SEGMENT_VERTEX_MASK = 0x0FFFFFFF
SEGMENT_FLAG_START = 0x10000000
SEGMENT_FLAG_END = 0x20000000
NO_GUIDE = 0xFFFF
Buffer = bytes | memoryview
# ``width`` is a UNORM16 radius in m, before component scaling. Web-solver files pack the last word differently.
VERTEX = np.dtype([("position", "<f4", 3), ("width", "<u2"), ("root_tip", "u1"), ("random", "u1")])
_UNUSED_SHARE = -1.0


@dataclass(slots=True)
class StrandsBounds:
    min: tuple[float, float, float] = (0.0, 0.0, 0.0)
    max: tuple[float, float, float] = (0.0, 0.0, 0.0)


@dataclass(slots=True)
class StrandsLodLevel:
    index: int
    vertex_count: int
    segment_count: int
    strand_count: int
    vertex_records: Buffer = b""
    segment_records: Buffer = b""
    strand_offsets: tuple[int, ...] = ()
    vertex_strands: Buffer = b""
    closest_guide_data: Buffer = b""
    planar_guides: bool = False

    def guide_rows(self) -> np.ndarray:
        """Six planes from version 31 on, else row after row."""
        words = np.frombuffer(self.closest_guide_data, "<u4")
        return words.reshape(6, -1).T if self.planar_guides else words.reshape(-1, 6)

    def _validate(self):
        roots = np.asarray(self.strand_offsets, dtype=np.int64)
        lengths = np.diff(np.append(roots, self.vertex_count))
        if roots[0] != 0 or np.any(lengths <= 0):
            raise ValueError(f"LOD {self.index}: invalid strand vertex ranges")
        mapping = np.frombuffer(self.vertex_strands, dtype="<u4")
        if not np.array_equal(mapping, np.repeat(np.arange(self.strand_count), lengths)):
            raise ValueError(f"LOD {self.index}: vertex-to-strand mapping disagrees with roots")
        segments = np.frombuffer(self.segment_records, dtype="<u4")
        starts = segments & SEGMENT_VERTEX_MASK
        if np.any(starts + 1 >= self.vertex_count):
            raise ValueError(f"LOD {self.index}: segment vertex index out of range")
        if np.any(mapping[starts] != mapping[starts + 1]):
            raise ValueError(f"LOD {self.index}: segment crosses a strand boundary")
        seen = np.zeros(self.vertex_count, bool)
        seen[starts] = True
        if np.count_nonzero(seen) != self.segment_count:
            raise ValueError(f"LOD {self.index}: duplicate segments")
        expected = ((starts == roots[mapping[starts]]) * SEGMENT_FLAG_START
                    | (starts + 2 == (roots + lengths)[mapping[starts]]) * SEGMENT_FLAG_END)
        if np.any((segments & (SEGMENT_FLAG_START | SEGMENT_FLAG_END)) != expected):
            raise ValueError(f"LOD {self.index}: invalid segment boundary flags")
        vertices = np.frombuffer(self.vertex_records, dtype="<f4").reshape(-1, 4)
        if not np.isfinite(vertices[:, :3]).all():
            raise ValueError(f"LOD {self.index}: non-finite vertex position")
        if not self.closest_guide_data:
            return
        for guide, offset, weight, lerp in zip(*(slots.T for slots in unpacked(self.guide_rows()))):
            active = guide != NO_GUIDE
            if np.any(guide[active] >= self.strand_count):
                raise ValueError(f"LOD {self.index}: guide strand index out of range")
            # Retail RE4 also uses 0xFFFF offsets for short guides with nonzero weights.
            bound = active & (offset != NO_GUIDE)
            if np.any(offset[bound] >= lengths[guide[bound]]):
                raise ValueError(f"LOD {self.index}: guide vertex offset out of range")
            if not np.isfinite(weight[active]).all() or not np.isfinite(lerp[bound]).all():
                raise ValueError(f"LOD {self.index}: non-finite guide weight")


def guide_data(rows: np.ndarray | None, planar: bool) -> bytes:
    """``rows`` is n x 6 words."""
    return b"" if rows is None else (rows.T if planar else rows).tobytes()


def unpacked(words: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    low, high = (words & 0xFFFF).astype(np.int64), (words >> 16).astype(np.int64)
    guides = np.stack((low[:, 0], high[:, 0], low[:, 1]), 1)
    offsets = np.stack((high[:, 1], low[:, 2], high[:, 2]), 1)
    halves = np.stack((low[:, 3], high[:, 3], low[:, 4], high[:, 4], low[:, 5], high[:, 5]), 1)
    halves = halves.astype(np.uint16).view(np.float16).astype(np.float64)
    return guides, offsets, halves[:, :3], halves[:, 3:]


def packed(guides: np.ndarray, offsets: np.ndarray, weights: np.ndarray, shares: np.ndarray) -> np.ndarray:
    unused = guides == NO_GUIDE
    offsets = np.where(unused, NO_GUIDE, offsets).astype(np.uint32)
    weights, shares = np.where(unused, 0, weights), np.where(unused, _UNUSED_SHARE, shares)
    guides = guides.astype(np.uint32)

    def half(values):
        return np.asarray(values, np.float16).view(np.uint16).astype(np.uint32)
    weights, shares = half(weights), half(shares)
    return np.stack((guides[:, 0] | guides[:, 1] << 16, guides[:, 2] | offsets[:, 0] << 16,
                     offsets[:, 1] | offsets[:, 2] << 16, weights[:, 0] | weights[:, 1] << 16,
                     weights[:, 2] | shares[:, 0] << 16, shares[:, 1] | shares[:, 2] << 16), 1)


@dataclass(slots=True)
class StrandsGuideExtras:
    vertex_joint_indices: Buffer = b""
    vertex_joint_weights: Buffer = b""
    joint_names: tuple[str, ...] = ()
    # A joint matrix, 16 floats, translation (12-14) in cm.
    joint_matrix_records: Buffer = b""
    joint_bounds: tuple[StrandsBounds, ...] = ()
    # As the file holds it, padding included; empty writes it from ``joint_names``.
    joint_name_records: Buffer = b""


class _Reader:
    def __init__(self, data):
        self.data = memoryview(data)
        self.offset = 0

    def take(self, size):
        end = self.offset + size
        if end > len(self.data):
            raise ValueError(f"Truncated .strands section at {self.offset:#x} ({size} bytes)")
        data = self.data[self.offset:end]
        self.offset = end
        return data

    def unpack(self, fmt):
        return struct.unpack("<" + fmt, self.take(struct.calcsize("<" + fmt)))

    def bounds(self, *, max_first=False):
        first, second = self.unpack("3f"), self.unpack("3f")
        bounds = StrandsBounds(second, first) if max_first else StrandsBounds(first, second)
        if not np.isfinite((first, second)).all() or any(a > b for a, b in zip(bounds.min, bounds.max)):
            raise ValueError("Invalid strands bounds")
        return bounds


@dataclass(slots=True)
class StrandsFile:
    version: int = 0
    strand_count: int = 0
    guide_count: int = 0
    secondary_guide_count: int = 0
    lod_guide_vertex_counts: tuple[int, ...] = (0, 0, 0, 0)
    lod_secondary_guide_vertex_counts: tuple[int, ...] = (0, 0, 0, 0)
    bounds: StrandsBounds = field(default_factory=StrandsBounds)
    movement_bounds: StrandsBounds | None = None
    strand_width_min: float = 0.0
    strand_width_max: float = 0.0
    strand_width_ave: float = 0.0
    preset_name_hash: int = 0
    lods: list[StrandsLodLevel] = field(default_factory=list)
    root_uvs: tuple[tuple[float, float], ...] = ()
    groom_groups: tuple[int, ...] = ()
    guide_extras: StrandsGuideExtras | None = None
    # Byte sizes of the joint-skinning sections.
    extra_sizes: tuple[int, ...] = ()
    animation_guide_size: int = 0
    animation_guide_vertex_index_size: int = 0
    animation_guide_vertex_weight_size: int = 0
    # RE9 web solver: unique vertices are centroids; connections are directed.
    lod_web_unique_vertex_counts: tuple[int, ...] = (0, 0, 0, 0)
    lod_web_anchor_vertex_counts: tuple[int, ...] = (0, 0, 0, 0)
    lod_web_connection_counts: tuple[int, ...] = (0, 0, 0, 0)

    def read(self, data: bytes) -> None:
        """The file holds no version field; its version is the resource version of its path."""
        data = bytes(data)
        if data[:4] != STRANDS_MAGIC:
            raise ValueError("Invalid .strands magic")
        matches = []
        errors = []
        for candidate in SUPPORTED_VERSIONS:
            try:
                matches.append(self._parse(data, candidate))
            except ValueError as exc:
                errors.append(f"v{candidate}: {exc}")
        if not matches:
            raise ValueError("Invalid .strands layout: " + "; ".join(errors))
        if len(matches) != 1:
            raise ValueError("Ambiguous .strands layout")
        for f in fields(self):
            setattr(self, f.name, getattr(matches[0], f.name))

    @staticmethod
    def _parse(data: bytes, version: int) -> StrandsFile:
        reader = _Reader(data)
        reader.take(4)
        counts, vertex_sizes, index_sizes, root_sizes, mapping_sizes, guide_counts = (
            reader.unpack("4I") for _ in range(6)
        )
        out = StrandsFile(version=version, lod_guide_vertex_counts=guide_counts)
        modern = version >= 31
        if modern:
            out.lod_secondary_guide_vertex_counts = reader.unpack("4I")
        root_uv_size, out.guide_count = reader.unpack("2I")
        if modern:
            out.secondary_guide_count, = reader.unpack("I")
        out.bounds = reader.bounds(max_first=True)
        closest_sizes = reader.unpack("4I")
        out.extra_sizes = extra_sizes = reader.unpack("5I" if modern else "4I")
        # Legacy byte sizes: the v20/v31/v33 loaders read no matching payloads.
        (out.animation_guide_size, out.animation_guide_vertex_index_size,
         out.animation_guide_vertex_weight_size) = reader.unpack("3I")
        out.strand_width_min, out.strand_width_max, out.strand_width_ave = reader.unpack("3f")
        widths = (out.strand_width_min, out.strand_width_max, out.strand_width_ave)
        if not np.isfinite(widths).all() or min(widths) < 0:
            raise ValueError("Invalid strand widths")
        groom_size = 0
        if modern:
            groom_size, = reader.unpack("I")
            out.movement_bounds = reader.bounds()
            if version == 33:
                out.lod_web_unique_vertex_counts = reader.unpack("4I")
                # Exported anchor counts are retained; the native solver rebuilds its anchors.
                out.lod_web_anchor_vertex_counts = reader.unpack("4I")
                out.lod_web_connection_counts = reader.unpack("4I")
            out.preset_name_hash, = reader.unpack("I")
        expected_size = (HEADER_SIZES[version] + sum(vertex_sizes) + sum(index_sizes)
                         + sum(root_sizes) + sum(mapping_sizes)
                         + sum(closest_sizes) * (6 if modern else 1)
                         + root_uv_size + sum(extra_sizes) + groom_size)
        if expected_size != len(data):
            raise ValueError("File size does not match the declared sections")
        if root_uv_size % 8 or groom_size not in (0, root_uv_size // 2):
            raise ValueError("Invalid root UV or groom-group size")
        out.strand_count = root_uv_size // 8
        for i in range(4):
            vertices, remainder = divmod(vertex_sizes[i], 16)
            strands, root_remainder = divmod(root_sizes[i], 4)
            guide_size = closest_sizes[i] * (6 if modern else 1)
            if (remainder or root_remainder or index_sizes[i] != counts[i] * 4
                    or mapping_sizes[i] != vertices * 4
                    or guide_size not in (0, vertices * 24)):
                raise ValueError(f"LOD {i}: invalid buffer sizes")
            if not vertices:
                if strands or counts[i] or guide_size:
                    raise ValueError(f"LOD {i}: nonempty tables without vertices")
                continue
            if not 0 < strands <= out.strand_count or counts[i] != vertices - strands:
                raise ValueError(f"LOD {i}: inconsistent strand/vertex/segment counts")
            level = StrandsLodLevel(
                i, vertices, counts[i], strands,
                vertex_records=reader.take(vertex_sizes[i]),
                segment_records=reader.take(index_sizes[i]),
                strand_offsets=reader.unpack(f"{strands}I"),
                vertex_strands=reader.take(mapping_sizes[i]),
                closest_guide_data=reader.take(guide_size),
                planar_guides=modern,
            )
            level._validate()
            for guides, guide_vertices in (
                (out.guide_count, out.lod_guide_vertex_counts[i]),
                (out.secondary_guide_count, out.lod_secondary_guide_vertex_counts[i]),
            ):
                if (guides > strands or guide_vertices != (
                        level.strand_offsets[guides] if guides < strands else vertices)):
                    raise ValueError(f"LOD {i}: guide count disagrees with strand vertex ranges")
            out.lods.append(level)
        out.root_uvs = tuple(struct.iter_unpack("<2f", reader.take(root_uv_size)))
        if any(extra_sizes):
            out.guide_extras = _read_guide_extras(reader, extra_sizes, out.lods)
        out.groom_groups = reader.unpack(f"{groom_size // 4}I")
        return out

    def to_bytes(self) -> bytes:
        if self.version not in HEADER_SIZES:
            raise ValueError(f"Unsupported .strands version: {self.version}")
        modern = self.version >= 31
        levels = {level.index: level for level in self.lods}
        if len(levels) != len(self.lods) or any(index not in range(4) for index in levels):
            raise ValueError("A .strands file holds at most one level of each LOD 0 to 3")
        if len(self.root_uvs) != self.strand_count or (self.groom_groups
                                                        and len(self.groom_groups) != self.strand_count):
            raise ValueError("The root UVs and groom groups hold one entry a strand")
        empty = StrandsLodLevel(0, 0, 0, 0)
        held = [levels.get(i, empty) for i in range(4)]
        for level in held:
            if level.vertex_count:
                level._validate()
        planes = 6 if modern else 1
        header = [
            *(level.segment_count for level in held), *(len(level.vertex_records) for level in held),
            *(len(level.segment_records) for level in held), *(4 * len(level.strand_offsets) for level in held),
            *(len(level.vertex_strands) for level in held), *self.lod_guide_vertex_counts,
        ]
        out = bytearray(STRANDS_MAGIC + struct.pack("<24I", *header))
        if modern:
            out += struct.pack("<4I", *self.lod_secondary_guide_vertex_counts)
        out += struct.pack("<2I", 8 * self.strand_count, self.guide_count)
        if modern:
            out += struct.pack("<I", self.secondary_guide_count)
        out += struct.pack("<6f", *self.bounds.max, *self.bounds.min)
        out += struct.pack("<4I", *(len(level.closest_guide_data) // planes for level in held))
        extras = self.guide_extras
        names = b""
        if extras is not None:
            names = bytes(extras.joint_name_records) or "".join(f"{name}," for name in extras.joint_names).encode()
            sizes = [len(extras.vertex_joint_indices), len(extras.vertex_joint_weights), len(names),
                     len(extras.joint_matrix_records)]
            if modern:
                sizes.append(24 * len(extras.joint_bounds))
        else:
            sizes = [0] * (5 if modern else 4)
        out += struct.pack(f"<{len(sizes)}I", *sizes)
        out += struct.pack("<3I3f", self.animation_guide_size, self.animation_guide_vertex_index_size,
                           self.animation_guide_vertex_weight_size, self.strand_width_min, self.strand_width_max,
                           self.strand_width_ave)
        if modern:
            movement = self.movement_bounds or StrandsBounds()
            out += struct.pack("<I6f", 4 * len(self.groom_groups), *movement.min, *movement.max)
            if self.version == 33:
                out += struct.pack("<12I", *self.lod_web_unique_vertex_counts, *self.lod_web_anchor_vertex_counts,
                                   *self.lod_web_connection_counts)
            out += struct.pack("<I", self.preset_name_hash)
        if len(out) != HEADER_SIZES[self.version]:
            raise AssertionError("strands header size")
        for level in held:
            out += level.vertex_records
            out += level.segment_records
            out += struct.pack(f"<{len(level.strand_offsets)}I", *level.strand_offsets)
            out += level.vertex_strands
            out += level.closest_guide_data
        out += struct.pack(f"<{2 * self.strand_count}f", *(value for uv in self.root_uvs for value in uv))
        if extras is not None:
            out += extras.vertex_joint_indices
            out += extras.vertex_joint_weights
            out += names
            out += extras.joint_matrix_records
            if modern:
                for box in extras.joint_bounds:
                    out += struct.pack("<6f", *box.min, *box.max)
        out += struct.pack(f"<{len(self.groom_groups)}I", *self.groom_groups)
        return bytes(out)


def _read_guide_extras(reader, sizes, lods):
    indices_size, weights_size, names_size, matrices_size = sizes[:4]
    lod0 = next((level for level in lods if level.index == 0), None)
    if (indices_size % 8 or weights_size != indices_size * 2 or matrices_size % 64
            or (indices_size and (lod0 is None or indices_size != lod0.vertex_count * 8))):
        raise ValueError("Invalid guide skinning buffer sizes")
    out = StrandsGuideExtras(
        vertex_joint_indices=reader.take(indices_size),
        vertex_joint_weights=reader.take(weights_size),
    )
    out.joint_name_records = reader.take(names_size)
    try:
        names = bytes(out.joint_name_records).rstrip(b"\0").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Invalid UTF-8 joint-name map") from exc
    out.joint_names = tuple(names.rstrip(",").split(",")) if names else ()
    if any(not name or "\0" in name for name in out.joint_names):
        raise ValueError("Invalid empty or embedded-NUL joint name")
    joint_count = len(out.joint_names)
    if matrices_size != joint_count * 64:
        raise ValueError("Joint-name and matrix counts disagree")
    out.joint_matrix_records = reader.take(matrices_size)
    if not np.isfinite(np.frombuffer(out.joint_matrix_records, dtype="<f4")).all():
        raise ValueError("Non-finite joint matrix")
    if len(sizes) == 5:
        if sizes[4] not in (0, joint_count * 24):
            raise ValueError("Joint-name and bounds counts disagree")
        out.joint_bounds = tuple(reader.bounds() for _ in range(sizes[4] // 24))
    indices = np.frombuffer(out.vertex_joint_indices, dtype="<u2")
    weights = np.frombuffer(out.vertex_joint_weights, dtype="<f4")
    if np.any((indices != NO_GUIDE) & (indices >= joint_count)):
        raise ValueError("Guide joint index out of range")
    if not np.isfinite(weights).all() or np.any(weights < 0):
        raise ValueError("Invalid guide skinning weight")
    return out
