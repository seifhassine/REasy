from __future__ import annotations

import ctypes
import json
import os
import re
import struct
from ctypes import wintypes
from functools import cache
from itertools import count
from pathlib import Path

lazy import pefile
lazy from utils.app_paths import resource_path

_PATCH = "re_chunk_000.pak.patch_{:03d}.pak"
_kernel32 = ctypes.WinDLL("kernel32")
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD))
_kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_user32 = ctypes.WinDLL("user32")
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.argtypes = (_WNDENUMPROC, wintypes.LPARAM)
_user32.IsWindowVisible.argtypes = (wintypes.HWND,)
_user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))


@cache
def play_games() -> dict[str, dict]:
    return json.loads(resource_path("resources/data/play.json", required=True).read_text("utf-8"))


def patch_slot(game_dir: Path) -> Path:
    return next(path for number in count(1) if not (path := game_dir / _PATCH.format(number)).is_file()
                or not path.stat().st_size)


def chains_later_patch(pak: Path) -> bool:
    number = re.fullmatch(r"re_chunk_000\.pak\.patch_(\d{3})\.pak", pak.name)
    later = number and pak.with_name(_PATCH.format(int(number[1]) + 1))
    return bool(later and later.is_file() and later.stat().st_size)


def reframework_commits(dll: Path) -> int | None:
    data = dll.read_bytes()
    if (at := data.find(b"\0Commits: %i\0")) < 0:
        return None
    pe = pefile.PE(data=data, fast_load=True)
    target = pe.get_rva_from_offset(at + 1)
    for code in (section for section in pe.sections if section.IMAGE_SCN_MEM_EXECUTE):
        # mov edx, commits; lea rcx, ["Commits: %i"]
        for match in re.finditer(rb"\xBA(.{4})\x48\x8D\x0D(.{4})", code.get_data(), re.S):
            commits, displacement = struct.unpack("<Ii", match.group(1) + match.group(2))
            if code.VirtualAddress + match.end() + displacement == target:
                return commits
    return None


def pak_mods_enabled(game_dir: Path) -> bool:
    config, off = game_dir / "re2_fw_config.txt", "IntegrityCheckBypass_LoadPakDirectory=false"
    return not config.is_file() or off not in config.read_text("utf-8", "replace")


def steam_app_id(game_dir: Path) -> str | None:
    for manifest in game_dir.parent.parent.glob("appmanifest_*.acf"):
        installed = re.search(r'"installdir"\s+"([^"]+)"', manifest.read_text("utf-8", "replace"))
        if installed and installed[1].casefold() == game_dir.name.casefold():
            return manifest.stem.removeprefix("appmanifest_")
    return None


def game_processes(exe: Path) -> list[int]:
    pids, size = (wintypes.DWORD * 8192)(), wintypes.DWORD()
    _kernel32.K32EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(size))
    target, image, found = os.path.normcase(exe), ctypes.create_unicode_buffer(32768), []
    for pid in pids[:size.value // ctypes.sizeof(wintypes.DWORD)]:
        if handle := _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid):
            length = wintypes.DWORD(len(image))
            named = _kernel32.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length))
            _kernel32.CloseHandle(handle)
            if named and os.path.normcase(image.value) == target:
                found.append(pid)
    return found


def window_owners() -> set[int]:
    owners = set()

    def visit(hwnd, _):
        if _user32.IsWindowVisible(hwnd):
            owner = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            owners.add(owner.value)
        return True

    _user32.EnumWindows(_WNDENUMPROC(visit), 0)
    return owners
