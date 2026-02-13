import struct
from pathlib import Path

# PCM-only codec options.
POPULAR_WEM_CODECS: list[tuple[str, int]] = [
    ("PCM S16LE (sample/default)", 0xFFFE),
    ("PCM S16LE (classic PCM tag)", 0x0001),
]


def get_codec_profile(codec_tag: int) -> tuple[str, int]:
    wanted = codec_tag & 0xFFFF
    for p in POPULAR_WEM_CODECS:
        if p[1] == wanted:
            return p
    return POPULAR_WEM_CODECS[0]


_WEM_JUNK = bytes([
    0x06, 0x00, 0x00, 0x00, 0x01, 0x41, 0x00, 0x00,
    0x4A, 0x55, 0x4E, 0x4B, 0x04, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
])


def _riff_chunks(blob: bytes) -> list[tuple[bytes, bytes]]:
    if len(blob) < 12 or blob[0:4] != b"RIFF" or blob[8:12] != b"WAVE":
        raise ValueError("Input is not a RIFF/WAVE file.")
    out: list[tuple[bytes, bytes]] = []
    i, n = 12, len(blob)
    while i + 8 <= n:
        cid = blob[i:i + 4]
        sz = int.from_bytes(blob[i + 4:i + 8], "little")
        i += 8
        if i + sz > n:
            break
        out.append((cid, blob[i:i + sz]))
        i += sz + (sz & 1)
    return out


def _find_chunk(chunks: list[tuple[bytes, bytes]], cid: bytes) -> bytes | None:
    for k, v in chunks:
        if k == cid:
            return v
    return None


def _wav_to_wem_bytes(wav_data: bytes, *, codec_tag: int) -> bytes:
    chunks = _riff_chunks(wav_data)
    fmt = _find_chunk(chunks, b"fmt ")
    pcm = _find_chunk(chunks, b"data")
    if fmt is None or pcm is None or len(fmt) < 16:
        raise ValueError("WAV must contain valid fmt and data chunks.")

    audio_format, bits = struct.unpack_from("<H12xH", fmt, 0)
    if audio_format != 0x0001 or bits != 16:
        raise ValueError("Only PCM s16le WAV input is supported.")

    wem_fmt = bytearray(fmt)
    wem_fmt[0:2] = struct.pack("<H", codec_tag & 0xFFFF)

    wem = bytearray(b"RIFF\x00\x00\x00\x00WAVE")
    wem += b"fmt " + struct.pack("<I", len(wem_fmt) + 8) + bytes(wem_fmt) + _WEM_JUNK
    wem += b"data" + struct.pack("<I", len(pcm)) + pcm
    if len(pcm) & 1:
        wem += b"\x00"
    wem[4:8] = struct.pack("<I", len(wem) - 8)
    return bytes(wem)


def convert_wem_to_wav_bytes(wem_data: bytes) -> bytes:
    chunks = _riff_chunks(wem_data)
    fmt = _find_chunk(chunks, b"fmt ")
    pcm = _find_chunk(chunks, b"data")
    if fmt is None or pcm is None or len(fmt) < 16:
        raise ValueError("WEM must contain valid fmt and data chunks.")

    channels, sample_rate, byte_rate, block_align, bits = struct.unpack_from("<HIIHH", fmt, 2)
    wav_fmt_payload = struct.pack("<HHIIHH", 0x0001, channels, sample_rate, byte_rate, block_align, bits)

    wav = bytearray(b"RIFF\x00\x00\x00\x00WAVE")
    wav += b"fmt " + struct.pack("<I", 16) + wav_fmt_payload
    wav += b"data" + struct.pack("<I", len(pcm)) + pcm
    if len(pcm) & 1:
        wav += b"\x00"
    wav[4:8] = struct.pack("<I", len(wav) - 8)
    return bytes(wav)


def convert_wem_file_to_wav_bytes(wem_path: str | Path) -> bytes:
    with open(wem_path, "rb") as f:
        return convert_wem_to_wav_bytes(f.read())


def convert_wav_to_wem(wav_path: str | Path, *, codec_tag: int = 0xFFFE) -> bytes:
    _, codec_tag = get_codec_profile(codec_tag)
    with open(wav_path, "rb") as f:
        return _wav_to_wem_bytes(f.read(), codec_tag=codec_tag)


def convert_file_to_wem(src_path: str | Path, *, parent_window=None, auto_download=True, codec_tag: int = 0xFFFE) -> bytes:
    src_path = Path(src_path)
    if src_path.suffix.lower() != ".wav":
        raise ValueError("Only WAV imports are supported in PCM-only mode.")
    return convert_wav_to_wem(src_path, codec_tag=codec_tag)