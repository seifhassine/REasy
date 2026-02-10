from .bnk_parser import (
    BnkParseResult, BnkTrack, WemMetadata,
    extract_embedded_wem, parse_soundbank, parse_wem_metadata, rewrite_soundbank,
)

__all__ = [
    "BnkParseResult", "BnkTrack", "WemMetadata",
    "extract_embedded_wem", "parse_soundbank", "parse_wem_metadata", "rewrite_soundbank",
]

def __getattr__(name: str):
    if name in ("SoundHandler", "SOUND_MAGICS"):
        from .sound_handler import SoundHandler, SOUND_MAGICS
        globals().update(SoundHandler=SoundHandler, SOUND_MAGICS=SOUND_MAGICS)
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
