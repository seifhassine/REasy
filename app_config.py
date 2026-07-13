"""Application metadata shared by the UI and project tooling."""

CURRENT_VERSION = "0.7.2"

GAMES = (
    "RE4",
    "RE2",
    "RE2RT",
    "RE8",
    "RE3",
    "RE3RT",
    "REResistance",
    "RE9",
    "RE7",
    "RE7RT",
    "MHWilds",
    "MHRise",
    "MHST3",
    "DMC5",
    "SF6",
    "O2",
    "OnimushaWOTS",
    "DD2",
    "Pragmata",
    "KunitsuGami",
)

_X64_GAMES = frozenset({"RE2", "RE7", "DMC5"})
GAME_NATIVE_PATHS = {
    game: ("natives", "x64") if game in _X64_GAMES else ("natives", "stm")
    for game in GAMES
}
