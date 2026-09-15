from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Callable

from .codec import decode_ift, encode_ift
from .model import IconGlyph, IftAtlasValidation, IftData, IftEntry
from .profiles import IFT_MAGIC

if TYPE_CHECKING:
    from file_handlers.uvs.uvs_file import UvsFile, UvsPattern


class IftFile:
    def __init__(self) -> None:
        self.model: IftData | None = None
        self.raw_data = b""
        self._original: IftData | None = None

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
        self._original = deepcopy(self.model)
        self.raw_data = bytes(data)
        return True

    def write(self) -> bytes:
        model = self.require_model()
        if self._original is not None and model == self._original:
            return self.raw_data
        return encode_ift(model)

    def require_model(self) -> IftData:
        if self.model is None:
            raise ValueError("no IFT data is loaded")
        return self.model

    def find(self, name: str) -> IftEntry | None:
        if not name:
            return None
        return next(
            (entry for entry in self.require_model().entries if entry.name == name),
            None,
        )

    @staticmethod
    def _uv_pattern(uvs: "UvsFile", entry: IftEntry) -> "UvsPattern" | None:
        if not 0 <= entry.uv_sequence_no < len(uvs.sequences):
            return None
        sequence = uvs.sequences[entry.uv_sequence_no]
        if not 0 <= entry.uv_pattern_no < len(sequence.patterns):
            return None
        return sequence.patterns[entry.uv_pattern_no]

    def resolve(self, name: str, uvs: "UvsFile" | None = None) -> IconGlyph | None:
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
        pattern = self._uv_pattern(uvs, entry)
        if pattern is None:
            return None
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
        self, uvs: "UvsFile" | None = None
    ) -> Callable[[str], IconGlyph | None]:
        return lambda name: self.resolve(name, uvs)

    def validate_uvs(self, uvs: "UvsFile") -> IftAtlasValidation:
        entries = self.require_model().entries
        invalid: list[str] = []
        used: set[tuple[int, int]] = set()
        for entry in entries:
            if self._uv_pattern(uvs, entry) is None:
                invalid.append(entry.name)
            else:
                used.add((entry.uv_sequence_no, entry.uv_pattern_no))
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
