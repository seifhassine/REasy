import os
import shlex
import struct
import subprocess
import sys
from pathlib import Path
from typing import Optional, Dict, Any, List

from file_handlers.base_handler import BaseFileHandler

from .tex_file import TexFile, TEX_MAGIC
from .dds import build_dds_dx10, convert_dds_for_pil_compatibility


_HELPER_CMD_CACHE: list[str] | None = None


def _resolve_helper_command() -> list[str]:
    global _HELPER_CMD_CACHE

    if _HELPER_CMD_CACHE:
        return _HELPER_CMD_CACHE

    env_cmd = os.environ.get("REASY_TEX_GDEFLATE_HELPER_CMD", "").strip()
    if env_cmd:
        _HELPER_CMD_CACHE = shlex.split(env_cmd)
        return _HELPER_CMD_CACHE

    if getattr(sys, "frozen", False):
        helper_path = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)).resolve() / "tools" / "reasy_tex_gdeflate_helper.exe"
    else:
        helper_root = Path(__file__).resolve().parents[2] / "tools" / "reasy_tex_gdeflate_helper" / "bin" / "Release"
        candidates = list(helper_root.glob("**/reasy_tex_gdeflate_helper.exe"))
        if not candidates:
            raise FileNotFoundError(f"Could not find reasy_tex_gdeflate_helper.exe in expected location: {helper_root}")
        helper_path = candidates[0]

    _HELPER_CMD_CACHE = [str(helper_path)]
    return _HELPER_CMD_CACHE


def _run_external_unpacker(data: bytes) -> tuple[bytes | None, str]:
    cmd = _resolve_helper_command() + ["--stdin", "--stdout", "--mode", "decompress-tex"]
    helper_path = Path(cmd[0])
    helper_dir = helper_path.parent

    if not helper_path.exists():
        return None, f"missing:{helper_path}"

    env = os.environ.copy()
    env["PATH"] = f"{helper_dir}{os.pathsep}{env.get('PATH', '')}"

    kwargs = {
        "input": data,
        "capture_output": True,
        "check": False,
        "timeout": 30,
        "cwd": str(helper_dir),
        "env": env,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        proc = subprocess.run(cmd, **kwargs)
    except (FileNotFoundError, PermissionError, subprocess.SubprocessError) as ex:
        return None, f"exec-failed:{helper_path}:{type(ex).__name__}:{ex}"

    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip().replace("\n", " ")
        return None, f"ret={proc.returncode}:{helper_path}:{err[:240]}"

    if len(proc.stdout) < 4:
        return None, f"short-output:{helper_path}:{len(proc.stdout)}"

    if struct.unpack_from('<I', proc.stdout, 0)[0] != TEX_MAGIC:
        return None, f"bad-magic:{helper_path}"

    return proc.stdout, ""


class TexHandler(BaseFileHandler):

    def __init__(self):
        super().__init__()
        self.tex: Optional[TexFile] = None
        self.raw_data: bytes | bytearray = b""

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        if len(data) < 4:
            return False
        magic = struct.unpack_from('<I', data, 0)[0]
        return magic == TEX_MAGIC

    def supports_editing(self) -> bool:
        return False

    def read(self, data: bytes):
        self.raw_data = data
        tex = TexFile()

        try:
            ok = tex.read(data)
        except RuntimeError as ex:
            if "gdeflate" not in str(ex).lower():
                raise
            unpacked, helper_diag = _run_external_unpacker(data)
            if unpacked is None:
                raise RuntimeError(
                    "TEX file contains gdeflate-compressed mip data and helper execution failed. "
                    f"Helper diagnostics: {helper_diag}"
                ) from ex
            ok = tex.read(unpacked)
            self.raw_data = unpacked

        if not ok:
            raise ValueError("Failed to parse TEX file")
        self.tex = tex
        self.modified = False

    def rebuild(self) -> bytes:
        return bytes(self.raw_data)

    def populate_treeview(self, tree, parent_item, metadata_map: dict):
        return

    def get_context_menu(self, tree, item, meta: dict):
        return None

    def handle_edit(self, meta: Dict[str, Any], new_val, old_val, item):
        pass

    def add_variables(self, target, prefix: str, count: int):
        pass

    def update_strings(self):
        pass

    def create_viewer(self):
        try:
            from .tex_viewer import TexViewer
            v = TexViewer(self)
            v.modified_changed.connect(self.modified_changed.emit)
            return v
        except Exception:
            return None

    def build_dds_bytes(self, image_index: int = 0) -> bytes:
        if not self.tex:
            return b""
        header = self.tex.header
        mip_bytes: List[bytes] = []

        if header.format_is_block_compressed() and not self.tex.header_is_power_of_two():
            for level in range(header.mip_count):
                data, _, _ = self.tex.read_non_pot_level(level, image_index)
                mip_bytes.append(data)
        else:
            for level in range(header.mip_count):
                m = self.tex.get_mip_map_data(level, image_index)
                mip_bytes.append(m.data)

        dds_header = build_dds_dx10(
            width=header.width,
            height=header.height,
            mip_count=header.mip_count,
            dxgi_format=header.format,
            array_size=max(1, getattr(header, 'image_count', 1)),
        )
        return dds_header + b"".join(mip_bytes)

    def build_dds_bytes_for_viewing(self, image_index: int = 0) -> bytes:
        dds_data = self.build_dds_bytes(image_index)
        return convert_dds_for_pil_compatibility(dds_data)

