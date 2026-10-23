import struct
from pathlib import Path
from typing import Callable, Optional
from dataclasses import replace
import hashlib
import json

from file_handlers.base_handler import BaseFileHandler
lazy from file_handlers.sdftex.mesh_resource import MeshDistanceField
from .canonical_writer import CanonicalMeshWriteResult, serialize_mesh
from .mesh_file import MeshFile, MeshLoadMode, MESH_MAGIC, MPLY_MAGIC
lazy from .profiles import MeshGameProfile, mesh_profile_for_game
lazy from utils.resource_file_utils import (
    ResourceResolutionContext,
    natives_split,
    resolve_handler_resource_data,
    resource_context_for_app,
    resource_version_from_path,
)
lazy from .conversion import convert_mesh
lazy from . import skinning_contract
lazy from .conversion_geometry import mesh_payloads
lazy from .conversion_skin import (
    SourceSkinningAmbiguity,
    source_has_material_skinning,
    source_skinning_contract,
    equivalent_source_skinning,
    validate_source_skinning,
)
lazy from .material_resolver import MdfSkinningConflict, MeshMaterialResolver
lazy from .mesh_viewer import MeshViewer


class MeshHandler(BaseFileHandler):
    def __init__(self):
        super().__init__()
        self.mesh: Optional[MeshFile] = None
        self.filepath: str = ""
        # Format dispatch uses the mesh versions, not this resource lookup hint.
        self.game_version: str = ""
        self.load_mode = MeshLoadMode.FULL
        self.reread: Optional[Callable[[], Optional[bytes]]] = None
        self._streaming_data_cache: dict[str, bytes | None] = {}
        self._taken_streaming: tuple[bytes | None] | None = None
        self._resolved_mdf_cache: dict[tuple[object, ...], object] = {}
        self._pending_write_result: CanonicalMeshWriteResult | None = None
        self._write_result_staged = False
        self.sdftex = MeshDistanceField(self)
        self._pending_sdftex_outputs: dict[str, bytes] = {}

    @property
    def mesh_profile(self) -> MeshGameProfile | None:
        return self.mesh.profile if self.mesh is not None else None

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
        load_mode: MeshLoadMode | str = MeshLoadMode.FULL,
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
        handler.load_mode = MeshLoadMode.coerce(load_mode)
        handler.read(data)
        return handler

    def supports_editing(self) -> bool:
        return bool(
            self.mesh is not None
            and self.mesh.load_mode is MeshLoadMode.FULL
            and self.mesh.profile is not None
            and any(
                internal == self.mesh.header.version
                for internal, _file in self.mesh.profile.canonical_version_pairs
            )
        )

    def _canonical_write_result(self) -> CanonicalMeshWriteResult:
        if self.mesh is None:
            raise ValueError("no MESH file is loaded")
        if self._pending_write_result is None:
            self._pending_write_result = serialize_mesh(self.mesh)
        return self._pending_write_result

    def rebuild(self) -> bytes:
        if not self._write_result_staged:
            self._pending_write_result = None
        self._write_result_staged = False
        result = self._canonical_write_result()
        self.modified = False
        return result.main

    def convert_to(self, target, **options):
        source_mdf_path = options.pop("source_mdf_path", "")
        if source_mdf_path and "source_skinning_contract" in options:
            raise ValueError("Choose a source MDF or an explicit source shader, not both")
        if "source_skinning_contract" not in options:
            options["source_skinning_contract"] = self.conversion_source_skinning_contract(
                explicit_mdf_path=source_mdf_path)
        return convert_mesh(self.mesh, target, **options)

    def conversion_source_skinning_contract(self, *, explicit_mdf_path=""):
        if self.mesh is None or self.mesh.header.magic == MPLY_MAGIC:
            return None

        def material_contract(counts):
            return replace(source_skinning_contract(self.mesh, material_counts=counts),
                           lod0_compute_influence_count=skinning_contract.normal_compute_influence_count(self.mesh))
        flags = self.mesh.header.decoded_content_flags
        if flags is not None and not flags.is_skinning:
            return None
        if not any(payload.skin_weights is not None for payload in mesh_payloads(self.mesh)):
            return None
        ambiguity = None
        if not explicit_mdf_path:
            try:
                validate_source_skinning(self.mesh)
                return None
            except SourceSkinningAmbiguity as exc:
                ambiguity = exc
        try:
            profile = self.mesh.profile
            if source_has_material_skinning(profile) and not profile.has_material_skinning_contract:
                game = mesh_profile_for_game(self.game_version)
                if (game is None or not game.has_material_skinning_contract
                        or not game.version_pairs.intersection(profile.version_pairs)):
                    return None
                counts = MeshMaterialResolver.resolve_material_skinning_influences(
                    self, game, explicit_mdf_path=explicit_mdf_path)
                return material_contract(counts)
            return skinning_contract.resolve_mesh_skinning_contract(self.mesh, self, explicit_mdf_path=explicit_mdf_path)
        except ValueError as exc:
            if explicit_mdf_path:
                raise
            conflict = exc if isinstance(exc, MdfSkinningConflict) else exc.__cause__
            if isinstance(conflict, MdfSkinningConflict):
                modes = {tuple(sorted(counts.items())) for _path, counts in conflict.variants}
                contracts = [material_contract(dict(counts)) for counts in sorted(modes)]
                if equivalent_source_skinning(self.mesh, contracts):
                    return contracts[0]
            raise SourceSkinningAmbiguity(f"{ambiguity}. Material lookup: {exc}") from exc

    def conversion_output_targets(self, result, primary_path):
        path = Path(primary_path)
        if not path.name.lower().endswith(f".mesh.{result.file_version}"):
            raise ValueError(f"Converted filename must end in .mesh.{result.file_version}")
        outputs = {}
        for part, part_path in zip(result.parts, result.part_paths(path)):
            outputs[str(part_path)] = part.output.main
            if part.output.streaming is not None:
                outputs[str(self._streaming_output_path(part_path))] = part.output.streaming
        if len(result.parts) > 1:
            manifest = dict(schema="reasy.mesh-assembly", version=1, game=result.profile.game,
                mesh_version=result.file_version, source=Path(self.filepath).name if self.filepath else None,
                requirements=result.requirements, losses=result.losses,
                parts=[dict(file=p.name, sha256=hashlib.sha256(part.output.main).hexdigest(),
                            skeleton_joints=part.palette,
                            normal_support_triangles=sum(g.support_triangles for g in part.geometry))
                       for p, part in zip(result.part_paths(path), result.parts)])
            outputs[str(path.with_name(path.name.rsplit(".mesh", 1)[0] + ".assembly.json"))] = (
                json.dumps(manifest, indent=2) + "\n").encode("utf-8")
        source_paths = {Path(self.filepath).resolve()} if self.filepath else set()
        if self.filepath:
            try:
                source_paths.add(self._streaming_output_path().resolve())
            except ValueError:
                pass
        if any(Path(target).resolve() in source_paths for target in outputs):
            raise ValueError("Choose a new filename to keep the source mesh and companion intact")
        return outputs

    def _streaming_output_path(self, primary_path: str | Path | None = None) -> Path:
        return self._streaming_paths(primary_path)[0]

    def _streaming_paths(
        self, primary_path: str | Path | None = None,
    ) -> tuple[Path, str]:
        split = natives_split(primary_path or self.filepath)
        if split is None:
            raise ValueError(
                "A streaming MESH companion requires a path below natives/<platform>."
            )
        root, resource_parts = split
        if any(part.casefold() == "streaming" for part in resource_parts):
            raise ValueError("The primary MESH path is already below streaming.")
        resource_path = "/".join((*root.parts[-2:], "streaming", *resource_parts))
        return root / "streaming" / Path(*resource_parts), resource_path

    def related_output_targets(self, primary_path: str | Path) -> dict[str, bytes]:
        # A save asks for its companions before the main file (FileTab.handle_file_save).
        self._pending_write_result = None
        result = self._canonical_write_result()
        self._write_result_staged = True
        outputs = {}
        if result.streaming is not None:
            outputs[str(self._streaming_output_path(primary_path))] = result.streaming
        self._pending_sdftex_outputs = self.sdftex.output(primary_path)
        outputs.update(self._pending_sdftex_outputs)
        return outputs

    def pending_related_outputs(self) -> dict[str, bytes]:
        outputs = dict(self._pending_sdftex_outputs)
        if self._pending_write_result is not None and self._pending_write_result.streaming is not None:
            outputs[str(self._streaming_output_path())] = self._pending_write_result.streaming
        return outputs

    def mark_related_outputs_saved(self) -> None:
        self._pending_write_result = None
        if self._pending_sdftex_outputs:
            self.sdftex.resolved_path = next(iter(self._pending_sdftex_outputs))
            self.sdftex.mark_saved()
        self._pending_sdftex_outputs.clear()

    def discard_related_outputs(self) -> None:
        self._pending_write_result = None
        self._pending_sdftex_outputs.clear()
        self.sdftex.discard()
        self.sdftex = MeshDistanceField(self)

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
        try:
            candidate = self._streaming_output_path()
        except ValueError:
            if any(part.casefold() == "natives" for part in path.parts):
                return None
            candidate = path.parent.parent / "streaming" / path.parent.name / path.name
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

        try:
            _stream_path, resource_path = self._streaming_paths()
        except ValueError:
            self._streaming_data_cache[self.filepath] = None
            return None

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

    def _parse(self, data: bytes, load_mode: MeshLoadMode) -> MeshFile:
        mesh = MeshFile()
        mesh.read(data, file_version=self._file_version_from_path(self.filepath), load_mode=load_mode,
                  streaming_data_loader=self._load_streaming_mesh_data)
        return mesh

    def take_streaming(self, data: bytes | None) -> None:
        self._taken_streaming = (data,)

    def read(self, data: bytes):
        self._streaming_data_cache.clear()
        if self._taken_streaming is not None:
            self._streaming_data_cache[self.filepath], = self._taken_streaming
            self._taken_streaming = None
        self._resolved_mdf_cache.clear()
        self._pending_write_result = None
        self._pending_sdftex_outputs.clear()
        self.sdftex = MeshDistanceField(self)
        self.mesh = self._parse(data, self.load_mode)
        if self.load_mode is not MeshLoadMode.FULL:
            self._streaming_data_cache.clear()
        self.modified = False

    def whole_mesh(self) -> Optional[MeshFile]:
        """Raises the ValueError a FULL read of the file raises."""
        if self.mesh is not None and self.mesh.load_mode is MeshLoadMode.SCENE:
            held, failure = self.mesh.scene_contract()
            if not held:
                raise ValueError(failure)
            return self.mesh
        if self.mesh is not None and self.mesh.is_full:
            return self.mesh
        data = None if self.reread is None else self.reread()
        if data is None:
            return None
        return self._parse(data, MeshLoadMode.FULL)

    def create_viewer(self):
        v = MeshViewer(self)
        v.modified_changed.connect(self.modified_changed.emit)
        return v
