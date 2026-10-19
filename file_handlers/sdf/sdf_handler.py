from __future__ import annotations

from file_handlers.base_handler import BaseFileHandler
from settings import app_settings
from utils.resource_file_utils import resource_version_from_path

from .profiles import SdfGameProfile
from .sdf_file import SdfFile
from .editing import SdfBinaryEditor


class SdfHandler(BaseFileHandler):
    def __init__(self) -> None:
        super().__init__()
        self.sdf: SdfFile | None = None
        self.game_version = ""
        self._requested_profile: SdfGameProfile | None = None

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        return SdfFile.can_handle(data)

    @property
    def sdf_profile(self) -> SdfGameProfile | None:
        if self._requested_profile is not None:
            return self._requested_profile
        if self.sdf is not None and self.sdf.document is not None:
            return self.sdf.document.profile
        return None

    @sdf_profile.setter
    def sdf_profile(self, value: SdfGameProfile | None) -> None:
        self._requested_profile = value

    @classmethod
    def from_bytes(
        cls,
        filepath: str,
        data: bytes,
        *,
        app=None,
        game_version: str = "",
        sdf_profile: SdfGameProfile | None = None,
    ) -> "SdfHandler":
        handler = cls()
        handler.filepath = filepath
        handler.app = app
        handler.game_version = str(game_version or "")
        handler.sdf_profile = sdf_profile
        handler.read(data)
        return handler

    def supports_editing(self) -> bool:
        profile = self.sdf_profile
        return bool(profile and profile.binary_editing)

    def apply_edit(self, operation) -> bool:
        if self.sdf is None:
            raise ValueError("no SDF file is loaded")
        document = self.sdf.require_document()
        editor = SdfBinaryEditor(document)
        operation(editor)
        data = editor.data
        if data == document.original_data:
            return False
        self.read(data)
        self.modified = True
        return True

    def _resource_version(self) -> int | None:
        return resource_version_from_path(self.filepath or "", "sdf")

    def _effective_game_version(self) -> str:
        if self.game_version:
            return self.game_version
        return str((app_settings(self.app) or {}).get("game_version", "") or "")

    def read(self, data: bytes) -> None:
        self.sdf = SdfFile.from_bytes(
            data,
            resource_version=self._resource_version(),
            game=self._effective_game_version(),
            profile=self._requested_profile,
            label=self.filepath or "SDF",
        )
        self.modified = False

    def rebuild(self) -> bytes:
        if self.sdf is None:
            raise ValueError("no SDF file is loaded")
        self.modified = False
        return self.sdf.write()

    def create_viewer(self):
        from .sdf_viewer import SdfViewer

        return SdfViewer(self)


__all__ = ["SdfHandler"]
