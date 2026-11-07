from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .profiles import (
    TexCookProfile,
    tex_profile,
    texture_linear_types,
    texture_type_name,
    texture_type_names,
)

SWITCH, NUMBER, CHOICE = "switch", "number", "choice"

RESIDENT = 1 << 0
EV_SHIFT = 1
EV_MASK = 0x3F
TYPE_SHIFT = 7
TYPE_MASK = 0x1F
FIXED_MIP_NUM = 1 << 12
STREAMING_MIPS_SHIFT = 13
# The widest streaming mip field a loader reads (bits 13-17; docs/internals/tex-flags.md).
STREAMING_MIPS_WIDEST = 0x1F
RANGE_COMPRESSION_EXP_SHIFT = 18
RANGE_COMPRESSION_EXP_MASK = 0x3F
REFILTERED_NORMAL = 1 << 24
HIGH_PRIORITY_LOAD_STREAMING = 1 << 25

RESERVED_HIGH_BITS = 0x3F << 26


def streaming_mips_mask(profile: TexCookProfile) -> int:
    return (1 << profile.streaming_mip_bits) - 1


@dataclass(frozen=True, slots=True)
class TexSetting:
    field: str
    kind: str
    value: int
    maximum: int = 0
    choices: tuple[str, ...] = ()


def type_choices(profile: TexCookProfile) -> tuple[str, ...]:
    if not profile.enum_game:
        return ()
    return tuple(texture_type_names(profile)[profile.type_enum_offset:])


@dataclass(frozen=True, slots=True)
class TexFlags:
    raw: int
    resident: bool
    texture_type: int
    type_name: Optional[str]
    linear_type: bool
    fixed_mip_num: bool
    streaming_mips: int
    refiltered_normal: bool
    ev: int
    range_compression_exp: int
    high_priority_load_streaming: bool
    # Bits no loader of the cook reads (past its streaming mip field, and 26-31).
    reserved_bits_set: int

    @classmethod
    def decode(cls, raw: int, version: int) -> "TexFlags":
        cook = tex_profile(version)
        index = (raw >> TYPE_SHIFT) & TYPE_MASK
        name = texture_type_name(cook, index)
        mips = streaming_mips_mask(cook)
        return cls(
            raw=raw,
            resident=bool(raw & RESIDENT),
            texture_type=index,
            type_name=name,
            linear_type=name is not None and name in texture_linear_types(cook),
            fixed_mip_num=bool(raw & FIXED_MIP_NUM),
            streaming_mips=(raw >> STREAMING_MIPS_SHIFT) & mips,
            refiltered_normal=bool(raw & REFILTERED_NORMAL),
            ev=(raw >> EV_SHIFT) & EV_MASK,
            range_compression_exp=(raw >> RANGE_COMPRESSION_EXP_SHIFT) & RANGE_COMPRESSION_EXP_MASK,
            high_priority_load_streaming=bool(raw & HIGH_PRIORITY_LOAD_STREAMING),
            reserved_bits_set=raw & ((STREAMING_MIPS_WIDEST & ~mips) << STREAMING_MIPS_SHIFT | RESERVED_HIGH_BITS),
        )

    @property
    def type_label(self) -> str:
        return self.type_name if self.type_name is not None else f"type index {self.texture_type}"

    @property
    def bit_12_matches_type(self) -> bool:
        return self.fixed_mip_num == self.linear_type

    def settings(self, profile: TexCookProfile) -> list[TexSetting]:
        settings = [
            TexSetting("resident", SWITCH, int(self.resident)),
            TexSetting("ev", NUMBER, self.ev, EV_MASK),
        ]
        if profile.type_field_used:
            settings.append(
                TexSetting("texture_type", CHOICE, self.texture_type, TYPE_MASK, type_choices(profile))
            )
        if profile.streaming_mips:
            settings.append(
                TexSetting("streaming_mips", NUMBER, self.streaming_mips, streaming_mips_mask(profile)))
        if profile.refiltered_normal:
            settings.append(TexSetting("refiltered_normal", SWITCH, int(self.refiltered_normal)))
        if profile.range_compression_exp:
            settings.append(TexSetting("range_compression_exp", NUMBER, self.range_compression_exp,
                                       RANGE_COMPRESSION_EXP_MASK))
        if profile.high_priority_load_streaming:
            settings.append(TexSetting("high_priority_load_streaming", SWITCH, int(self.high_priority_load_streaming)))
        return settings

    def encode(self, profile: TexCookProfile, **changes: int) -> int:
        resident = changes.get("resident", self.resident)
        ev = changes.get("ev", self.ev)
        texture_type = changes.get("texture_type", self.texture_type)
        streaming_mips = changes.get("streaming_mips", self.streaming_mips)
        refiltered_normal = changes.get("refiltered_normal", self.refiltered_normal)
        range_compression_exp = changes.get("range_compression_exp", self.range_compression_exp)
        high_priority_load_streaming = changes.get("high_priority_load_streaming", self.high_priority_load_streaming)
        word = (
            (1 if resident else 0)
            | (ev << EV_SHIFT)
            | (texture_type << TYPE_SHIFT)
            | ((streaming_mips & streaming_mips_mask(profile)) << STREAMING_MIPS_SHIFT)
            | (range_compression_exp << RANGE_COMPRESSION_EXP_SHIFT)
        )
        name = texture_type_name(profile, texture_type) if profile.type_field_used else None
        if name is not None and name in texture_linear_types(profile):
            word |= FIXED_MIP_NUM
        if refiltered_normal:
            word |= REFILTERED_NORMAL
        if high_priority_load_streaming:
            word |= HIGH_PRIORITY_LOAD_STREAMING
        return word

