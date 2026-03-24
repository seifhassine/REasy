import struct
from pathlib import Path
from typing import Optional, Dict, Any

from file_handlers.base_handler import BaseFileHandler
from .mesh_file import MeshFile, MESH_MAGIC, MPLY_MAGIC
from utils.resource_file_utils import get_path_prefix_for_game, resolve_resource_data


class MeshHandler(BaseFileHandler):
    def __init__(self):
        super().__init__()
        self.mesh: Optional[MeshFile] = None
        self.raw_data: bytes | bytearray = b""
        self.filepath: str = ""
        self._streaming_data_cache: dict[str, bytes | None] = {}

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        if len(data) < 4:
            return False
        magic = struct.unpack_from('<I', data, 0)[0]
        return magic in (MESH_MAGIC, MPLY_MAGIC)

    def supports_editing(self) -> bool:
        return False

    def _find_streaming_mesh_path(self) -> Optional[Path]:
        if not self.filepath:
            return None
        path = Path(self.filepath)
        sibling_stream = path.parent.parent / "streaming" / path.parent.name / path.name
        if sibling_stream.is_file():
            return sibling_stream
        parts = path.parts
        try:
            natives_idx = parts.index("natives")
        except ValueError:
            return None
        if natives_idx + 1 >= len(parts):
            return None
        if "streaming" in parts[natives_idx + 2:]:
            return None
        root = Path(*parts[:natives_idx + 2])
        rel = Path(*parts[natives_idx + 2:])
        candidate = root / "streaming" / rel
        return candidate if candidate.is_file() else None

    def _load_streaming_mesh_data(self) -> Optional[bytes]:
        if self.filepath in self._streaming_data_cache:
            return self._streaming_data_cache[self.filepath]

        stream_path = self._find_streaming_mesh_path()
        if stream_path:
            data = stream_path.read_bytes()
            self._streaming_data_cache[self.filepath] = data
            return data

        if not self.filepath:
            return None

        path = Path(self.filepath)
        parts = path.parts
        try:
            natives_idx = parts.index("natives")
        except ValueError:
            return None
        if natives_idx + 1 >= len(parts) or "streaming" in parts[natives_idx + 2:]:
            return None

        resource_path = "/".join((*parts[natives_idx : natives_idx + 2], "streaming", *parts[natives_idx + 2 :]))
        app = getattr(self, "app", None)
        proj = getattr(app, "proj_dock", None) if app is not None else None
        proj_mgr = getattr(app, "project_manager", None) if app is not None else None
        game = str(getattr(proj_mgr, "current_game", "") or "")
        path_prefix = get_path_prefix_for_game(game)
        resolved = resolve_resource_data(
            resource_path,
            getattr(proj, "project_dir", None),
            getattr(proj, "unpacked_dir", None),
            path_prefix,
            getattr(proj, "_pak_cached_reader", None),
            getattr(proj, "_pak_selected_paks", None),
        ) if proj is not None else None
        if resolved:
            self._streaming_data_cache[self.filepath] = resolved[1]
            return resolved[1]
        self._streaming_data_cache[self.filepath] = None
        return None

    def read(self, data: bytes):
        self.raw_data = data

        file_version = 0
        if self.filepath and ".mesh." in self.filepath:
            try:
                _, _, version = self.filepath.rpartition(".mesh.")
                file_version = int(version)
            except (ValueError, IndexError):
                pass

        mf = MeshFile()
        stream_data = self._load_streaming_mesh_data()
        mf.read(data, file_version=file_version, streaming_data=stream_data)
        self.mesh = mf
        self.modified = False

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
        from .mesh_viewer import MeshViewer
        v = MeshViewer(self)
        v.modified_changed.connect(self.modified_changed.emit)
        return v
