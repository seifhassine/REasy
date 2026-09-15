from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GlyphResolution:
    requested_codepoint: int
    resolved_codepoint: int | None
    glyph_id: int
    face_index: int | None
    fallback_index: int | None
    vertical_glyph_id: int | None

    @property
    def missing(self) -> bool:
        return self.face_index is None or self.glyph_id == 0


@dataclass(frozen=True, slots=True)
class GlyphFallbackPolicy:
    """Ordered fallback behavior used by one GUI/runtime generation."""

    fallback_codepoints: tuple[int, ...]
    tab_advance_scale: float = 2.0
    space_advance_scale: float = 0.5

    def resolve(
        self,
        codepoint: int,
        face_cmaps: Sequence[Mapping[int, int]],
        *,
        vertical_substitutions: Sequence[Mapping[int, int]] | None = None,
    ) -> GlyphResolution:
        if vertical_substitutions is not None and len(vertical_substitutions) != len(
            face_cmaps
        ):
            raise ValueError("vertical substitutions must match the face count")

        for fallback_index, candidate in enumerate(
            (int(codepoint), *self.fallback_codepoints)
        ):
            for face_index, cmap in enumerate(face_cmaps):
                glyph_id = int(cmap.get(candidate, 0))
                if not glyph_id:
                    continue
                vertical_id = glyph_id
                if vertical_substitutions is not None:
                    vertical_id = int(
                        vertical_substitutions[face_index].get(glyph_id, glyph_id)
                    )
                return GlyphResolution(
                    int(codepoint),
                    candidate,
                    glyph_id,
                    face_index,
                    fallback_index,
                    vertical_id,
                )
        return GlyphResolution(int(codepoint), None, 0, None, None, None)


# Recovered from DMC5's GUI text path. The policy is explicit so a newer game
# can register a different chain without changing the SFNT or catalog code.
DMC5_GUI_GLYPH_POLICY = GlyphFallbackPolicy((0x303C, 0x25A1, 0x002A))
