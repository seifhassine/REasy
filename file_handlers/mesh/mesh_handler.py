import struct
from pathlib import Path
from typing import Optional

from file_handlers.base_handler import BaseFileHandler
from .mesh_file import MeshFile, MESH_MAGIC, MPLY_MAGIC
from utils.resource_file_utils import (
    ResourceResolutionContext,
    resolve_handler_resource_data,
    resource_context_for_app,
    resource_version_from_path,
)


class MeshHandler(BaseFileHandler):
    def __init__(self):
        super().__init__()
        self.mesh: Optional[MeshFile] = None
        self.filepath: str = ""
        self.game_version: str = ""
        self._streaming_data_cache: dict[str, bytes | None] = {}
        self._material_skinning_cache: dict[
            tuple[str, str],
            dict[str, int],
        ] = {}
        self._mmtr_skinning_cache: dict[tuple[str, str], int] = {}

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        if len(data) < 4:
            return False
        magic = struct.unpack_from('<I', data, 0)[0]
        return magic in (MESH_MAGIC, MPLY_MAGIC)

    @classmethod
    def from_bytes(
        cls,
        filepath: str,
        data: bytes,
        *,
        app=None,
        resource_context: ResourceResolutionContext | None = None,
        game_version: str = "",
    ) -> "MeshHandler":
        handler = cls()
        handler.filepath = filepath
        handler.app = app
        handler.resource_context = resource_context or resource_context_for_app(
            app,
            game=game_version,
        )
        handler.game_version = str(
            game_version
            or getattr(handler.resource_context, "game", "")
            or ""
        )
        handler.read(data)
        return handler

    def supports_editing(self) -> bool:
        return False

    @staticmethod
    def _file_version_from_path(filepath: str) -> int:
        version = resource_version_from_path(filepath, "mesh")
        if version is None:
            version = resource_version_from_path(filepath, "mply")
        if version is None:
            raise ValueError(f"Mesh filename needs a numeric version suffix: {filepath}")
        return version

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
        resolved = resolve_handler_resource_data(
            self,
            resource_path,
            allow_selection_dialog=False,
        )
        if resolved:
            self._streaming_data_cache[self.filepath] = resolved[1]
            return resolved[1]
        self._streaming_data_cache[self.filepath] = None
        return None

    def read(self, data: bytes):
        self._material_skinning_cache.clear()
        self._mmtr_skinning_cache.clear()
        file_version = self._file_version_from_path(self.filepath)

        mf = MeshFile()
        stream_data = self._load_streaming_mesh_data()
        mf.read(data, file_version=file_version, streaming_data=stream_data)
        self.mesh = mf
        self.modified = False

    def create_viewer(self):
        from .mesh_viewer import MeshViewer
        v = MeshViewer(self)
        v.modified_changed.connect(self.modified_changed.emit)
        return v
