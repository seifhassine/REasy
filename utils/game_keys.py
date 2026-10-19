import re


def game_key(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
