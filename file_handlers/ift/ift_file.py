from __future__ import annotations

from typing import Callable

from .codec import decode_ift, encode_ift
from .model import IconGlyph, IftAtlasValidation, IftData, IftEntry
from .profiles import IFT_MAGIC, ift_profile

lazy from file_handlers.uvs.uvs_file import UvsFile, UvsPattern


class IftFile:
    def __init__(self) -> None:
        self.model: IftData | None = None

    @staticmethod
    def can_handle(data: bytes) -> bool:
        return len(data) >= 8 and data[4:8] == IFT_MAGIC

    @classmethod
    def from_bytes(cls, data: bytes) -> "IftFile":
        result = cls()
        result.read(data)
        return result

    def read(self, data: bytes) -> bool:
        self.model = decode_ift(bytes(data))
        return True

    def write(self) -> bytes:
        return encode_ift(self.require_model())

    def require_model(self) -> IftData:
        if self.model is None:
            raise ValueError("no IFT data is loaded")
        return self.model

    def find(self, name: str) -> IftEntry | None:
        if not name:
            return None
        return next(
            (entry for entry in self.require_model().all_entries if entry.name == name),
            None,
        )

    def _uv_pattern(self, entry: IftEntry, uvs: "UvsFile", aux: "UvsFile | None"
                    ) -> tuple["UvsFile", int, "UvsPattern"] | None:
        table, sequence_no = 1, entry.uv_sequence_no
        if ift_profile(self.require_model().version).packed_sequence:
            table, sequence_no = sequence_no & 0xFFFF, sequence_no >> 16
        uvs = aux if table == 2 else uvs
        if uvs is None or not 0 <= sequence_no < len(uvs.sequences):
            return None
        sequence = uvs.sequences[sequence_no]
        if not 0 <= entry.uv_pattern_no < len(sequence.patterns):
            return None
        return uvs, sequence_no, sequence.patterns[entry.uv_pattern_no]

    def resolve(self, name: str, uvs: "UvsFile" | None = None, aux: "UvsFile | None" = None) -> IconGlyph | None:
        entry = self.find(name)
        if entry is None:
            return None
        if uvs is None:
            return IconGlyph(
                entry.name,
                entry.uv_sequence_no,
                entry.uv_pattern_no,
                entry.width,
                entry.height,
            )
        found = self._uv_pattern(entry, uvs, aux)
        if found is None:
            return None
        uvs, _, pattern = found
        texture_path = None
        if 0 <= pattern.texture_index < len(uvs.textures):
            texture_path = uvs.textures[pattern.texture_index].path
        return IconGlyph(
            entry.name,
            entry.uv_sequence_no,
            entry.uv_pattern_no,
            entry.width,
            entry.height,
            (pattern.left, pattern.top, pattern.right, pattern.bottom),
            pattern.texture_index,
            texture_path,
            pattern.flags,
        )

    def resolver(
        self, uvs: "UvsFile" | None = None, aux: "UvsFile | None" = None
    ) -> Callable[[str], IconGlyph | None]:
        return lambda name: self.resolve(name, uvs, aux)

    def validate_uvs(self, uvs: "UvsFile", aux: "UvsFile | None" = None) -> IftAtlasValidation:
        entries = self.require_model().all_entries
        invalid: list[str] = []
        used: set[tuple[int, int]] = set()
        for entry in entries:
            found = self._uv_pattern(entry, uvs, aux)
            if found is None:
                invalid.append(entry.name)
            elif found[0] is uvs:
                used.add((found[1], entry.uv_pattern_no))
        all_patterns = {
            (sequence_no, pattern_no)
            for sequence_no, sequence in enumerate(uvs.sequences)
            for pattern_no in range(len(sequence.patterns))
        }
        return IftAtlasValidation(
            entry_count=len(entries),
            resolved_count=len(entries) - len(invalid),
            invalid_entries=tuple(invalid),
            unused_patterns=tuple(sorted(all_patterns - used)),
        )
