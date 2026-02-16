import struct
import subprocess
import tempfile
from shutil import which

from tools.ffmpeg_downloader import ensure_ffmpeg
from pathlib import Path

# PCM-only codec options.
POPULAR_WEM_CODECS: list[tuple[str, int]] = [
    ("PCM S16LE (sample/default)", 0xFFFE),
]


def get_codec_profile(codec_tag: int) -> tuple[str, int]:
    wanted = codec_tag & 0xFFFF
    for p in POPULAR_WEM_CODECS:
        if p[1] == wanted:
            return p
    return POPULAR_WEM_CODECS[0]


_WEM_JUNK_CHUNK = bytes([
    0x4A, 0x55, 0x4E, 0x4B, 0x04, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
])
_PCM_SUBFORMAT_GUID = bytes([
    0x01, 0x00, 0x00, 0x00,
    0x00, 0x00,
    0x10, 0x00,
    0x80, 0x00, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71,
])


def _default_channel_mask(channels: int) -> int:
    return {
        1: 0x00000004,
        2: 0x00000003,
        3: 0x00000007,
        4: 0x00000033,
        5: 0x00000037,
        6: 0x0000003F,
        7: 0x00000013F,
        8: 0x0000063F,
    }.get(channels, 0)


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

    audio_format, channels, sample_rate, byte_rate, block_align, bits = struct.unpack_from("<HHIIHH", fmt, 0)
    if bits != 16:
        raise ValueError("Only PCM s16le WAV input is supported.")
    if audio_format == 0xFFFE:
        if len(fmt) < 40:
            raise ValueError("Extensible WAV input is missing required fields.")
        source_channel_mask = struct.unpack_from("<I", fmt, 20)[0]
        subformat = fmt[24:40]
        if subformat != _PCM_SUBFORMAT_GUID:
            raise ValueError("Only PCM s16le WAV input is supported.")
    elif audio_format == 0x0001:
        source_channel_mask = _default_channel_mask(channels)
    else:
        raise ValueError("Only PCM s16le WAV input is supported.")

    wem_fmt = struct.pack(
        "<HHIIHHHHI16s",
        codec_tag & 0xFFFF,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits,
        22,
        bits,
        source_channel_mask,
        _PCM_SUBFORMAT_GUID,
    )

    wem = bytearray(b"RIFF\x00\x00\x00\x00WAVE")
    wem += b"fmt " + struct.pack("<I", len(wem_fmt)) + wem_fmt + _WEM_JUNK_CHUNK
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


def _resolve_ffmpeg_executable(*, parent_window=None) -> str | None:
    try:
        exe = ensure_ffmpeg(auto_download=False, parent_window=parent_window)
        if exe.exists() and exe.is_file():
            return str(exe)
    except Exception:
        pass

    path_ffmpeg = which("ffmpeg")
    if path_ffmpeg:
        return path_ffmpeg

    return None


def _prompt_and_download_ffmpeg(parent_window=None) -> str | None:
    if parent_window is None:
        return None

    from PySide6.QtWidgets import QMessageBox

    answer = QMessageBox.question(
        parent_window,
        "FFmpeg Required",
        "FFmpeg is required to import this audio format.\n\nDownload FFmpeg now?",
        QMessageBox.Yes | QMessageBox.No,
    )
    if answer != QMessageBox.Yes:
        return None

    exe = ensure_ffmpeg(auto_download=True, parent_window=parent_window)
    if exe.exists() and exe.is_file():
        return str(exe)
    return None


def _transcode_to_pcm16_wav(src_path: str | Path, *, parent_window=None, auto_download: bool = True) -> bytes:
    ffmpeg = _resolve_ffmpeg_executable(parent_window=parent_window)
    if not ffmpeg and auto_download:
        try:
            ffmpeg = _prompt_and_download_ffmpeg(parent_window=parent_window)
        except Exception as e:
            raise ValueError(f"ffmpeg download failed: {e}") from e
    if not ffmpeg:
        raise ValueError("ffmpeg is required to import non-PCM WAV or other audio formats. Download FFmpeg from Settings or add ffmpeg to PATH.")

    src_path = Path(src_path)
    with tempfile.TemporaryDirectory(prefix="reasy_ffmpeg_") as td:
        out_wav = Path(td) / "input_pcm16.wav"
        proc = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-v",
                "error",
                "-i",
                str(src_path),
                "-vn",
                "-acodec",
                "pcm_s16le",
                str(out_wav),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0 or not out_wav.exists():
            msg = (proc.stderr or proc.stdout or "Unknown ffmpeg error").strip()
            raise ValueError(f"ffmpeg conversion failed: {msg}")
        return out_wav.read_bytes()


def convert_file_to_wem(src_path: str | Path, *, parent_window=None, auto_download=True, codec_tag: int = 0xFFFE) -> bytes:
    src_path = Path(src_path)
    _, codec_tag = get_codec_profile(codec_tag)
    if src_path.suffix.lower() == ".wav":
        try:
            with open(src_path, "rb") as f:
                return _wav_to_wem_bytes(f.read(), codec_tag=codec_tag)
        except ValueError:
            pass

    wav_data = _transcode_to_pcm16_wav(src_path, parent_window=parent_window, auto_download=auto_download)
    return _wav_to_wem_bytes(wav_data, codec_tag=codec_tag)