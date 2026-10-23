from __future__ import annotations

import os
lazy import subprocess
import tempfile
from pathlib import Path

from utils.app_paths import downloads_directory

SCRIPT = Path(__file__).with_name("reasy_blender.py")
MINIMUM_VERSION = (5, 0)
DOWNLOAD_VERSION = "5.2.2"
DOWNLOAD_URL = "https://download.blender.org/release/Blender5.2/blender-5.2.2-windows-x64.zip"
DOWNLOAD_SHA256 = "3849d17a682cba006075aaa3f3597ecb5c9c30ec31035b2e092c53e40679b535"
DOWNLOAD_PAGE = "https://www.blender.org/download/"


class BlenderError(RuntimeError):
    pass


def _candidates():
    yield from sorted(downloads_directory("blender").glob("*/blender.exe"), reverse=True)
    for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        base = os.environ.get(variable)
        if base:
            yield from sorted(Path(base, "Blender Foundation").glob("Blender*/blender.exe"), reverse=True)
    for steam in (Path(os.environ.get("ProgramFiles(x86)", "")) / "Steam", Path("C:/SteamLibrary"), Path("D:/SteamLibrary")):
        yield steam / "steamapps/common/Blender/blender.exe"
    yield Path("/Applications/Blender.app/Contents/MacOS/Blender")
    yield Path("/usr/bin/blender")


def find_blender(configured: str | os.PathLike | None = "") -> Path | None:
    if configured:
        path = Path(configured)
        if path.is_dir():
            path = path / ("blender.exe" if os.name == "nt" else "blender")
        return path if path.is_file() else None
    for path in _candidates():
        if path.is_file():
            return path
    return None


def blender_version(executable) -> tuple[int, ...]:
    result = subprocess.run([str(executable), "--version"], capture_output=True, text=True, timeout=60,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for line in result.stdout.splitlines():
        if line.startswith("Blender "):
            return tuple(int(part) for part in line.split()[1].split(".")[:2] if part.isdigit())
    raise BlenderError("Could not read the Blender version")


def run_script(executable, *arguments, timeout=1800):
    if executable is None:
        raise BlenderError("Blender was not found. Choose blender.exe in Settings.")
    # --disable-autoexec: scenes from elsewhere may embed scripts and Python drivers; they never run here.
    command = [str(executable), "--background", "--factory-startup", "--disable-autoexec", "--python-exit-code", "3",
               "--python", str(SCRIPT), "--", *map(str, arguments)]
    env = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
        env.pop(name, None)
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=timeout, env=env,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise BlenderError("Blender did not finish in time") from exc
    except OSError as exc:
        raise BlenderError(f"Could not start Blender: {exc}") from exc
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode == 0 and "REASY_OK" in output:
        return output
    for line in output.splitlines():
        if "REASY_ERROR:" in line:
            raise BlenderError(line.split("REASY_ERROR:", 1)[1].strip())
    tail = "\n".join(line for line in output.splitlines()[-25:] if line.strip())
    raise BlenderError(f"Blender failed (exit code {result.returncode}):\n{tail}")


def temporary_directory():
    return tempfile.TemporaryDirectory(prefix="reasy_blend_")
