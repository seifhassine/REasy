from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

lazy from utils.enum_manager import EnumDefinitionError, game_enum_definitions

LAYOUT_LEGACY = 1  # 32 B header: count_a = mip levels, count_b = images
LAYOUT_INLINE = 2  # 40 B header: u16 of 12-bit image count, 4-bit mip count
LAYOUT_STORED = 3  # LAYOUT_INLINE plus a per-mip stored-chunk table of GDeflate chunks

TYPE_ENUM = "via.render.TextureType"
FORMAT_ENUM = "via.render.TextureFormat"

#: ``TextureType`` sentinel: the last member of every cook's enum.
TYPE_SENTINEL = "Max"

#: Member prefix of the types the ``flags`` word's bit 12 accompanies.
LINEAR_TYPE_PREFIX = "ImageLinear"


class TexFormatError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TexCookProfile:
    version: int
    layout: int
    games: tuple[str, ...]
    enum_game: str = ""
    type_enum_offset: int = 0
    type_field_used: bool = True
    #: bits 13-17 (``streamigMipNum``).
    streaming_mips: bool = False
    #: bit 24.
    refiltered_normal: bool = False
    # bits 18-23 and bit 25.
    unnamed_fields: bool = False

    @property
    def display_name(self) -> str:
        return ", ".join(self.games) if self.games else f"version {self.version}"


_PROFILES: tuple[TexCookProfile, ...] = (
    TexCookProfile(8, LAYOUT_LEGACY, ("RE7",)),
    TexCookProfile(10, LAYOUT_LEGACY, ("RE2",), type_field_used=False),
    TexCookProfile(11, LAYOUT_LEGACY, ("DMC5",), enum_game="DMC5"),
    TexCookProfile(28, LAYOUT_INLINE, ("MHRise",), enum_game="MHRise"),
    TexCookProfile(30, LAYOUT_INLINE, ("RE8",), enum_game="RE8"),
    TexCookProfile(34, LAYOUT_INLINE, ("RE2RT", "RE3RT"), enum_game="RE3RT"),
    TexCookProfile(35, LAYOUT_INLINE, ("RE7",), enum_game="RE7RT"),
    TexCookProfile(143221013, LAYOUT_INLINE, ("RE4",), enum_game="RE4"),
    TexCookProfile(143230113, LAYOUT_INLINE, ("SF6",), enum_game="SF6"),
    TexCookProfile(190820018, LAYOUT_LEGACY, ("RE3", "REResistance"), enum_game="RE3", type_enum_offset=1),
    TexCookProfile(231106777, LAYOUT_INLINE, ("KunitsuGami",), enum_game="KunitsuGami"),
    TexCookProfile(240606151, LAYOUT_INLINE, ("DRDR",), enum_game="DRDR"),
    TexCookProfile(240701001, LAYOUT_INLINE, ("O2",), enum_game="O2"),
    TexCookProfile(
        241101895, LAYOUT_INLINE, ("SF6",), enum_game="SF6",
        streaming_mips=True, refiltered_normal=True,
    ),
    TexCookProfile(241106027, LAYOUT_STORED, ("MHWilds",), enum_game="MHWilds", streaming_mips=True),
    TexCookProfile(
        250813143, LAYOUT_STORED, ("RE9",), enum_game="RE9",
        streaming_mips=True, refiltered_normal=True, unnamed_fields=True,
    ),
    TexCookProfile(
        251111100, LAYOUT_STORED, ("MHST3", "OnimushaWOTS", "Pragmata"),
        enum_game="OnimushaWOTS", streaming_mips=True, refiltered_normal=True,
        unnamed_fields=True,
    ),
    TexCookProfile(251211553, LAYOUT_INLINE, ("DD2",), enum_game="DD2", streaming_mips=True),
    TexCookProfile(760230703, LAYOUT_INLINE, ("REVerse",)),
)

_BY_VERSION = {profile.version: profile for profile in _PROFILES}


def tex_profile(version: int) -> TexCookProfile:
    try:
        return _BY_VERSION[int(version)]
    except (KeyError, TypeError, ValueError):
        raise TexFormatError(f"unsupported .tex resource version: {version!r}") from None


def tex_profiles() -> tuple[TexCookProfile, ...]:
    return _PROFILES


def tex_version_for_game(game: str) -> int | None:
    wanted = (game or "").strip().lower()
    if not wanted:
        return None
    return max(
        (p.version for p in _PROFILES if any(name.lower() == wanted for name in p.games)),
        default=None,
    )


def _enum_members(profile: TexCookProfile, name: str) -> list[dict]:
    game = profile.enum_game
    if not game:
        raise EnumDefinitionError(
            f"{profile.display_name} declares no engine enum JSON, so {name} cannot be resolved"
        )
    members = game_enum_definitions(game, namespace="via.render.").get(name)
    if not isinstance(members, list) or not members:
        raise EnumDefinitionError(f"{game} enum JSON: {name} is missing or empty")
    return members


@lru_cache(maxsize=None)
def texture_type_names(profile: TexCookProfile) -> tuple[str, ...]:
    names: list[str] = []
    for member in _enum_members(profile, TYPE_ENUM):
        value = member.get("value")
        if type(value) is not int or value != len(names):
            raise EnumDefinitionError(f"{profile.display_name} {TYPE_ENUM} is not a 0-based, gapless list")
        if member["name"] != TYPE_SENTINEL:
            names.append(member["name"])
    if not names:
        raise EnumDefinitionError(f"{profile.display_name} {TYPE_ENUM} has no usable members")
    return tuple(names)


@lru_cache(maxsize=None)
def texture_format_ids(profile: TexCookProfile) -> frozenset[int]:
    if not profile.enum_game:
        return frozenset()
    ids = set()
    for member in _enum_members(profile, FORMAT_ENUM):
        value = member.get("value")
        if type(value) is not int:
            raise EnumDefinitionError(f"{profile.display_name} {FORMAT_ENUM} has a non-integer member {member!r}")
        if member["name"] != TYPE_SENTINEL:
            ids.add(value)
    return frozenset(ids)


def texture_type_name(profile: TexCookProfile, stored_index: int) -> str | None:
    if not profile.type_field_used:
        if stored_index:
            raise TexFormatError(
                f"{profile.display_name} never fills the texture type field, but this file stores {stored_index}"
            )
        return None
    if not profile.enum_game:
        return None
    names = texture_type_names(profile)
    enum_index = stored_index + profile.type_enum_offset
    if not 0 <= enum_index < len(names):
        raise TexFormatError(
            f"{profile.display_name} stores texture type {stored_index}, outside its {len(names)}-member enum"
        )
    return names[enum_index]


@lru_cache(maxsize=None)
def texture_linear_types(profile: TexCookProfile) -> frozenset[str]:
    if not profile.enum_game:
        return frozenset()
    return frozenset(
        name for name in texture_type_names(profile) if name.startswith(LINEAR_TYPE_PREFIX)
    )
