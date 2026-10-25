from functools import lru_cache
import re

_NOT_KEY = re.compile(r"[^A-Z0-9]")


def game_key(value: object) -> str:
    return _text_key(value) if type(value) is str else _NOT_KEY.sub("", str(value or "").upper())


@lru_cache(maxsize=64)
def _text_key(value: str) -> str:
    return _NOT_KEY.sub("", value.upper())
