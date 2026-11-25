"""Application metadata shared by the UI and project tooling."""

CURRENT_VERSION = "0.8.0"

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
    "DRDR",
)
GAME_TITLES = {
    "RE4": "Resident Evil 4", "RE2": "Resident Evil 2", "RE2RT": "Resident Evil 2", "RE8": "Resident Evil Village",
    "RE3": "Resident Evil 3", "RE3RT": "Resident Evil 3", "REResistance": "Resident Evil Resistance",
    "RE9": "Resident Evil Requiem", "RE7": "Resident Evil 7 biohazard", "RE7RT": "Resident Evil 7 biohazard",
    "MHWilds": "Monster Hunter Wilds", "MHRise": "Monster Hunter Rise",
    "MHST3": "Monster Hunter Stories 3: Twisted Reflection", "DMC5": "Devil May Cry 5", "SF6": "Street Fighter 6",
    "O2": "Onimusha 2: Samurai's Destiny", "OnimushaWOTS": "Onimusha: Way of the Sword", "DD2": "Dragon's Dogma 2",
    "Pragmata": "PRAGMATA", "KunitsuGami": "Kunitsu-Gami: Path of the Goddess", "DRDR": "Dead Rising Deluxe Remaster",
}

_X64_GAMES = frozenset({"RE2", "RE7", "DMC5"})
GAME_NATIVE_PATHS = {
    game: ("natives", "x64") if game in _X64_GAMES else ("natives", "stm")
    for game in GAMES
}
