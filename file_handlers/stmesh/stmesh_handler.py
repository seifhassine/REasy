from __future__ import annotations

from typing import Optional

from file_handlers.mesh.mesh_file import MeshFile, MeshLoadMode
from file_handlers.mesh.mesh_handler import MeshHandler
from file_handlers.mesh.profiles import mesh_profile_for_stmesh_version
lazy from file_handlers.mmtr.mesh_draw import SubmeshRange
lazy from file_handlers.mmtr.meshlets import MeshletBuffer
lazy from utils.resource_file_utils import resource_version_from_path

lazy from .lit import StmeshDraw, meshlet_buffer, stmesh_draws
lazy from .mesh_view import stmesh_mesh
lazy from .stmesh_file import FLAG_MESHLETS, HEADER, MAGIC, StmeshFile, read_stmesh
lazy from .stmesh_writer import StmeshWriteResult, write_stmesh



class StmeshHandler(MeshHandler):
    def __init__(self):
        super().__init__()
        self.stmesh: Optional[StmeshFile] = None
        self._clusters: dict = {}
        self._draws: dict[int, list[StmeshDraw]] = {}
        self._meshlets: MeshletBuffer | None = None

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        return data[:4] == MAGIC

    def supports_editing(self) -> bool:
        return False

    @property
    def stmesh_version(self) -> int:
        version = resource_version_from_path(self.filepath, "stmesh")
        if version is None:
            raise ValueError(f"A SpeedTree mesh's filename needs its numeric version suffix: {self.filepath}")
        return version

    def _view(self, stmesh: StmeshFile) -> MeshFile:
        profile = mesh_profile_for_stmesh_version(self.stmesh_version)
        if profile is None:
            raise ValueError(f"No game ships SpeedTree meshes of version {self.stmesh_version}")
        view, self._clusters = stmesh_mesh(stmesh, profile)
        self._draws = {}
        self._meshlets = None
        view.sdf_path = stmesh.sdf_texture_path or ""
        return view

    def cluster_draw(self, lod: int, submesh) -> StmeshDraw:
        if not isinstance(self._clusters.get(submesh), int):
            raise ValueError("a meshlet SpeedTree mesh's submesh is drawn as chunks (meshlet_draw)")
        if lod not in self._draws:
            self._draws[lod] = stmesh_draws(self.stmesh, lod)
        return self._draws[lod][self._clusters[submesh]]

    def meshlet_draw(self, submesh) -> tuple[MeshletBuffer, SubmeshRange]:
        if not isinstance(self._clusters.get(submesh), tuple):
            raise ValueError("a vertex-stream SpeedTree mesh's submesh is drawn as a cluster (cluster_draw)")
        if self._meshlets is None:
            self._meshlets = meshlet_buffer(self.stmesh, {
                SubmeshRange.from_submesh(item): chunks for item, chunks in self._clusters.items()
                if isinstance(chunks, tuple)})
        return self._meshlets, SubmeshRange.from_submesh(submesh)

    def _parse(self, data: bytes, load_mode: MeshLoadMode) -> MeshFile:
        streaming = self._load_streaming_mesh_data() if data[HEADER + 0x40] & FLAG_MESHLETS else None
        self.stmesh = read_stmesh(data, streaming)
        return self._view(self.stmesh)

    def _canonical_write_result(self) -> StmeshWriteResult:
        if self.stmesh is None:
            raise ValueError("no SpeedTree mesh is loaded")
        if self._pending_write_result is None:
            self._pending_write_result = write_stmesh(self.stmesh)
        return self._pending_write_result

    def conversion_source_skinning_contract(self, *, explicit_mdf_path=""):
        return None


__all__ = ["StmeshHandler"]
