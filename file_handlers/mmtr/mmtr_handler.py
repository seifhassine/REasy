from __future__ import annotations

from utils.resource_file_utils import resource_version_from_path

from file_handlers.sdf.sdf_handler import SdfHandler

from .mmtr_file import MmtrFile
from .profiles import MmtrGameProfile


class MmtrHandler(SdfHandler):
    def __init__(self) -> None:
        super().__init__()
        self.mmtr: MmtrFile | None = None
        self._requested_mmtr_profile: MmtrGameProfile | None = None
        self._related_outputs: dict[str, bytes] = {}
        self.material_instance = None
        self._family_loader = None

    @classmethod
    def from_bytes(
        cls,
        filepath: str,
        data: bytes,
        *,
        app=None,
        game_version: str = "",
        mmtr_profile: MmtrGameProfile | None = None,
    ) -> "MmtrHandler":
        handler = cls()
        handler.filepath = filepath
        handler.app = app
        handler.game_version = str(game_version or "")
        handler._requested_mmtr_profile = mmtr_profile
        handler.read(data)
        return handler

    def _resource_version(self) -> int | None:
        return resource_version_from_path(self.filepath or "", "mmtr")

    def read(self, data: bytes) -> None:
        self.mmtr = MmtrFile.from_bytes(
            data,
            resource_version=self._resource_version(),
            game=self._effective_game_version(),
            profile=self._requested_mmtr_profile,
            label=self.filepath or "MMTR",
        )
        self.sdf = self.mmtr.sdf
        if self.material_instance is not None:
            try:
                self._bind(self.material_instance)
            except ValueError:
                self.material_instance = None
        self._related_outputs.clear()
        self.modified = False

    def _bind(self, material) -> None:
        from .material_context import bind_material_instance
        from .material_family import load_material_family

        if not str(material.header.mmtr_path or "").casefold().endswith(".mmtrs"):
            self.mmtr.document = bind_material_instance(self.mmtr.require_document(), material)
        elif self._family_loader is None:
            raise ValueError("A shader-LOD material needs a way to find the MMTR files of its family.")
        else:
            self.mmtr.document = load_material_family(material, self._family_loader,
                                                      game=self._effective_game_version())

    def bind_material_instance(self, material, family_loader=None) -> None:
        self._family_loader = family_loader
        self._bind(material)
        self.material_instance = material

    def rebuild(self) -> bytes:
        if self.mmtr is None:
            raise ValueError("no MMTR file is loaded")
        data = self.mmtr.write()
        self.modified = False
        return data

    def queue_material_project(self, path: str, data: bytes) -> None:
        self._related_outputs = {str(path): bytes(data)}

    def related_output_targets(self, primary_path: str) -> dict[str, bytes]:
        # ``primary_path`` is unused: the outputs carry the paths they were made for.
        return dict(self._related_outputs)

    def pending_related_outputs(self) -> dict[str, bytes]:
        return dict(self._related_outputs)

    def mark_related_outputs_saved(self) -> None:
        self._related_outputs.clear()

    def discard_related_outputs(self) -> None:
        self._related_outputs.clear()

    def create_viewer(self):
        from .mmtr_viewer import MmtrViewer

        return MmtrViewer(self)


__all__ = ["MmtrHandler"]
