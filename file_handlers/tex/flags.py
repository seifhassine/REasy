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
STREAMING_MIPS_MASK = 0x7
FIELD_18_23_SHIFT = 18
FIELD_18_23_MASK = 0x3F
REFILTERED_NORMAL = 1 << 24
OPAQUE_FLAG = 1 << 25

RESERVED_BITS = (1 << 16) | (1 << 17) | (0x3F << 26)


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
    field_18_23: int
    opaque_flag: bool

    @classmethod
    def decode(cls, raw: int, version: int) -> "TexFlags":
        cook = tex_profile(version)
        index = (raw >> TYPE_SHIFT) & TYPE_MASK
        name = texture_type_name(cook, index)
        return cls(
            raw=raw,
            resident=bool(raw & RESIDENT),
            texture_type=index,
            type_name=name,
            linear_type=name is not None and name in texture_linear_types(cook),
            fixed_mip_num=bool(raw & FIXED_MIP_NUM),
            streaming_mips=(raw >> STREAMING_MIPS_SHIFT) & STREAMING_MIPS_MASK,
            refiltered_normal=bool(raw & REFILTERED_NORMAL),
            ev=(raw >> EV_SHIFT) & EV_MASK,
            field_18_23=(raw >> FIELD_18_23_SHIFT) & FIELD_18_23_MASK,
            opaque_flag=bool(raw & OPAQUE_FLAG),
        )

    @property
    def type_label(self) -> str:
        return self.type_name if self.type_name is not None else f"type index {self.texture_type}"

    @property
    def reserved_bits_set(self) -> int:
        return self.raw & RESERVED_BITS

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
            settings.append(TexSetting("streaming_mips", NUMBER, self.streaming_mips, STREAMING_MIPS_MASK))
        if profile.refiltered_normal:
            settings.append(TexSetting("refiltered_normal", SWITCH, int(self.refiltered_normal)))
        if profile.unnamed_fields:
            settings.append(TexSetting("field_18_23", NUMBER, self.field_18_23, FIELD_18_23_MASK))
            settings.append(TexSetting("opaque_flag", SWITCH, int(self.opaque_flag)))
        return settings

    def encode(self, profile: TexCookProfile, **changes: int) -> int:
        resident = changes.get("resident", self.resident)
        ev = changes.get("ev", self.ev)
        texture_type = changes.get("texture_type", self.texture_type)
        streaming_mips = changes.get("streaming_mips", self.streaming_mips)
        refiltered_normal = changes.get("refiltered_normal", self.refiltered_normal)
        field_18_23 = changes.get("field_18_23", self.field_18_23)
        opaque_flag = changes.get("opaque_flag", self.opaque_flag)
        word = (
            (1 if resident else 0)
            | (ev << EV_SHIFT)
            | (texture_type << TYPE_SHIFT)
            | ((streaming_mips & STREAMING_MIPS_MASK) << STREAMING_MIPS_SHIFT)
            | (field_18_23 << FIELD_18_23_SHIFT)
        )
        name = texture_type_name(profile, texture_type) if profile.type_field_used else None
        if name is not None and name in texture_linear_types(profile):
            word |= FIXED_MIP_NUM
        if refiltered_normal:
            word |= REFILTERED_NORMAL
        if opaque_flag:
            word |= OPAQUE_FLAG
        return word

