from __future__ import annotations

from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass
from hashlib import blake2b
import os
from pathlib import Path
import sys
from threading import RLock
from typing import Iterable

from file_handlers.bounded_cache import BoundedCache
from file_handlers.mdf.mdf_file import BASE_TWO_SIDE_ENABLE, FORCED_TWO_SIDE_ENABLE, MatData, MdfFile, TexHeader
from file_handlers.mmtr.mmtr_file import (
    _parse_preview_document, _supports_preview_without_native_reflection, parse_drawn_master,
)
from file_handlers.mmtr.material_context import MaterialInstanceContext
from file_handlers.mmtr.parsed_files import Lease
from file_handlers.mmtr.profiles import MmtrGameProfile, mmtr_profile_for_game, mmtr_profile_for_resource
from utils.resource_file_utils import (
    ResourceLocation,
    ResourceResolutionContext,
    locate_handler_resource,
    locate_local_resource,
    resource_context_for_handler,
    resource_version_from_path,
)

from .profiles import MeshGameProfile, mesh_profile_for_game


PREFERRED_ALBEDO_TEXTURE_TYPES: tuple[str, ...] = (
    "BaseDielectricMap",
    "ALBD",
    "ALBDmap",
    "BackMap",
    "BaseMetalMap",
    "BaseDielectricMapBase",
    "BaseAlphaMap",
    "BaseShiftMap",
)


@dataclass(slots=True)
class MdfTextureProfile:
    texture_type: str
    texture_path: str


@dataclass(slots=True)
class MdfParameterProfile:
    name: str
    values: tuple[float, ...]


@dataclass(slots=True)
class MdfGpuBufferProfile:
    name: str
    path: str


@dataclass(slots=True)
class MdfSurfaceProfile:
    material_name: str
    tint: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    material_flags: int = 0
    mmtr_path: str = ""
    texture_type: str = ""
    texture_path: str = ""
    textures: tuple[MdfTextureProfile, ...] = ()
    parameters: tuple[MdfParameterProfile, ...] = ()
    gpu_buffers: tuple[MdfGpuBufferProfile, ...] = ()
    instance_context: MaterialInstanceContext | None = None

    @property
    def two_sided(self) -> bool:
        return bool(self.material_flags & _TWO_SIDED_FLAGS)


@dataclass(slots=True)
class MeshMaterialBinding:
    mesh_material_name: str
    mdf_material_name: str = ""
    texture_type: str = ""
    texture_path: str = ""
    resolved_texture_path: str = ""
    resolved_texture_data: bytes | None = None
    surface: MdfSurfaceProfile | None = None
    status: str = "Missing MDF material"


@dataclass(slots=True)
class ResolvedMdf:
    path: str
    surfaces: dict[str, MdfSurfaceProfile]


class MdfSkinningConflict(ValueError):
    def __init__(self, game, variants):
        self.variants = tuple(variants)
        first, counts = self.variants[0]
        other = next(path for path, candidate in self.variants if candidate != counts)
        super().__init__(f"{game} has conflicting source MDF skinning variants: "
                         f"{first} and {other}; select the source MDF explicitly")


def _drawn_document(data: bytes, profile: MmtrGameProfile, label: str):
    if mmtr_profile_for_game(profile.game) is not profile:
        return _parse_preview_document(data, profile, label)
    lease = Lease()
    try:
        return lease.parsed(label, data, parse_drawn_master, profile.game, label)
    finally:
        lease.close()


_MDF_PARSE_POOL: ProcessPoolExecutor | None = None
_MDF_PARSES: dict[tuple[str, int, bytes], Future] = {}
_MDF_PARSES_LIMIT = 1024
_MDF_PARSES_LOCK = RLock()
_TWO_SIDED_FLAGS = BASE_TWO_SIDE_ENABLE | FORCED_TWO_SIDE_ENABLE
_SKINNING_CONTRACTS = BoundedCache()
_SKINNING_CONTRACTS_LIMIT = 256


def _content_fingerprint(data: bytes) -> tuple[int, bytes]:
    return len(data), blake2b(data, digest_size=16).digest()


def _extract_surface_profiles(mdf_data: bytes, actual_path: str) -> dict[str, MdfSurfaceProfile]:
    mdf = MdfFile()
    mdf.read(mdf_data, actual_path)
    resolved: dict[str, MdfSurfaceProfile] = {}
    for material in mdf.materials:
        texture = MeshMaterialResolver.pick_primary_texture(material)
        resolved[material.header.mat_name] = MdfSurfaceProfile(
            material_name=material.header.mat_name,
            tint=MeshMaterialResolver.base_color(material),
            material_flags=int(material.header.material_flags),
            mmtr_path=material.header.mmtr_path,
            texture_type=texture.tex_type if texture else "",
            texture_path=texture.tex_path if texture else "",
            textures=tuple(
                MdfTextureProfile(item.tex_type, item.tex_path)
                for item in material.textures
                if item.tex_type and item.tex_path
            ),
            parameters=tuple(
                MdfParameterProfile(
                    parameter.name,
                    tuple(
                        float(value)
                        for value in parameter.parameter[
                            : max(0, min(4, int(parameter.component_count)))
                        ]
                    ),
                )
                for parameter in material.parameters
                if parameter.name
            ),
            gpu_buffers=tuple(
                MdfGpuBufferProfile(name.name, data.name)
                for name, data in material.gpu_buffers
            ),
            instance_context=MaterialInstanceContext.from_material(material),
        )
    return resolved


class MeshMaterialResolver:
    @classmethod
    def resource_cache_scope(cls, handler) -> tuple | None:
        context, sibling = cls.resource_scope(handler)
        if type(context) is not ResourceResolutionContext:
            return None
        reader = context.pak_cached_reader
        revision = None
        if reader is not None:
            from file_handlers.pak.reader import CachedPakReader
            if type(reader) is not CachedPakReader:
                return None
            revision = reader.path_lookup_revision
        return (
            str(context.project_dir), str(context.unpacked_dir), context.path_prefix,
            context.game, reader, revision, os.getcwd(), None if sibling is None else str(sibling.resolve()),
        )

    @staticmethod
    def _normalized_resource_path(path: str) -> str:
        return (path or "").replace("\\", "/").lstrip("@/").rstrip("\x00")

    @staticmethod
    def material_profile(handler) -> MeshGameProfile | None:
        profile = getattr(handler, "mesh_profile", None)
        opened = mesh_profile_for_game(getattr(handler, "game_version", ""))
        if profile is not None and opened is not None and opened.version_pairs & profile.version_pairs:
            return opened
        return profile

    @staticmethod
    def is_render_texture_path(path: str) -> bool:
        normalized = (path or "").replace("\\", "/").lower()
        return ".rtex" in normalized

    @staticmethod
    def _material_binding(resolved_mdf: ResolvedMdf, mesh_name: str) -> MeshMaterialBinding:
        surface = resolved_mdf.surfaces.get(mesh_name)
        if surface is None:
            return MeshMaterialBinding(mesh_name, status="Missing MDF material")
        return MeshMaterialBinding(
            mesh_material_name=mesh_name,
            mdf_material_name=mesh_name,
            texture_type=surface.texture_type,
            texture_path=surface.texture_path,
            surface=surface,
            status="Resolved MDF",
        )

    @classmethod
    def resolve_for_handler(
        cls,
        handler,
        *,
        explicit_mdf_path: str = "",
        parse_in_subprocess: bool = False,
    ) -> tuple[ResolvedMdf | None, list[MeshMaterialBinding]]:
        mesh = getattr(handler, "mesh", None)
        material_names = list(getattr(mesh, "material_names", []) or [])
        if not material_names:
            return None, []

        if explicit_mdf_path:
            resolved_mdf = cls.resolve_mdf_path_for_handler(
                handler,
                explicit_mdf_path,
                parse_in_subprocess=parse_in_subprocess,
            )
        else:
            resolved_mdf = cls.resolve_mdf_for_handler(handler, parse_in_subprocess=parse_in_subprocess)
        if resolved_mdf is None:
            return None, [MeshMaterialBinding(name, status="MDF not found") for name in material_names]

        return resolved_mdf, [cls._material_binding(resolved_mdf, name) for name in material_names]

    @classmethod
    def resolve_mdf_path_for_handler(
        cls,
        handler,
        mdf_path: str,
        *,
        parse_in_subprocess: bool = False,
    ) -> ResolvedMdf | None:
        normalized = cls._normalized_resource_path(mdf_path)
        if not normalized:
            return None
        return cls._read_mdf(
            cls._resolve_resource(handler, normalized),
            parse_in_subprocess=parse_in_subprocess,
            parsed_cache=cls._handler_mdf_cache(handler),
        )

    @staticmethod
    def _skinning_contracts(
        data: bytes,
        *,
        actual_path: str,
        game: str,
        resource_version: int | None,
    ) -> tuple[int, ...]:
        profile = mmtr_profile_for_resource(resource_version, game=game)
        if not _supports_preview_without_native_reflection(profile):
            return _drawn_document(data, profile, actual_path).skinning_contracts
        key = (id(profile), resource_version, actual_path, *_content_fingerprint(data))
        contracts = _SKINNING_CONTRACTS.get(key)
        if contracts is None:
            contracts = tuple(_drawn_document(data, profile, actual_path).skinning_contracts)
            _SKINNING_CONTRACTS.put(key, contracts, 0, _SKINNING_CONTRACTS_LIMIT, sys.maxsize)
        return contracts

    @classmethod
    def resolve_material_skinning_influences(
        cls,
        handler,
        mesh_profile: MeshGameProfile,
        *,
        explicit_mdf_path: str = "",
    ) -> dict[str, int]:
        if not mesh_profile.has_material_skinning_contract:
            raise ValueError(
                f"{mesh_profile.display_name} has no verified material skinning ABI"
            )
        resolved_mdf = (
            cls.resolve_mdf_path_for_handler(handler, explicit_mdf_path)
            if explicit_mdf_path
            else cls.resolve_mdf_for_handler(handler)
        )
        mesh = getattr(handler, "mesh", None)
        material_names = list(getattr(mesh, "material_names", ()) or ())
        if explicit_mdf_path or (resolved_mdf is not None
                                 and set(material_names) <= resolved_mdf.surfaces.keys()):
            return cls._material_skinning_influences(handler, mesh_profile, resolved_mdf, material_names)

        matched = []
        for path in cls.iter_nearby_mdf_paths(handler):
            candidate = cls.resolve_mdf_path_for_handler(handler, path)
            if candidate is not None and set(material_names) <= candidate.surfaces.keys():
                counts = cls._material_skinning_influences(handler, mesh_profile, candidate, material_names)
                matched.append((candidate.path, counts))
        if matched:
            if any(counts != matched[0][1] for _path, counts in matched[1:]):
                raise MdfSkinningConflict(mesh_profile.display_name, matched)
            return matched[0][1]
        return cls._material_skinning_influences(handler, mesh_profile, resolved_mdf, material_names)

    @classmethod
    def _material_skinning_influences(cls, handler, mesh_profile, resolved_mdf, material_names):
        if resolved_mdf is None:
            raise ValueError(
                f"{mesh_profile.display_name} material skinning requires an MDF with matching "
                "material names; none was found beside the mesh or in the configured game resources"
            )
        influences: dict[str, int] = {}
        resolved_influences: dict[str, int] = {}
        for material_name in material_names:
            surface = resolved_mdf.surfaces.get(material_name)
            if surface is None:
                raise ValueError(
                    f"{mesh_profile.display_name} MDF has no material named "
                    f"{material_name!r}"
                )
            mmtr_path = cls._normalized_resource_path(surface.mmtr_path)
            if not mmtr_path:
                raise ValueError(
                    f"{mesh_profile.display_name} material {material_name!r} "
                    "has no MMTR path"
                )
            mmtr_key = mmtr_path.casefold()
            influence_count = resolved_influences.get(mmtr_key)
            if influence_count is None:
                resolved_mmtr = cls._resolve_mmtr_resource(handler, mmtr_path)
                if resolved_mmtr is None:
                    raise ValueError(
                        f"{mesh_profile.display_name} material "
                        f"{material_name!r} MMTR was not found: "
                        f"{mmtr_path}"
                    )
                actual_path, data = resolved_mmtr
                actual_version = resource_version_from_path(actual_path, "mmtr")
                if actual_version is None:
                    actual_version = mesh_profile.material_shader_resource_version
                try:
                    contracts = cls._skinning_contracts(
                        data,
                        actual_path=actual_path,
                        game=mesh_profile.game,
                        resource_version=actual_version,
                    )
                    if not contracts:
                        raise ValueError("no skinned vertex shader was found")
                    if len(contracts) != 1:
                        raise ValueError(f"mixed skinning contracts {list(contracts)}")
                    influence_count = contracts[0]
                except Exception as exc:
                    raise ValueError(
                        f"{mesh_profile.display_name} material "
                        f"{material_name!r} MMTR contract is unsupported: {exc}"
                    ) from exc
                resolved_influences[mmtr_key] = influence_count
            influences[material_name] = influence_count

        return influences

    @classmethod
    def iter_nearby_mdf_paths(cls, handler) -> Iterable[str]:
        filepath = str(getattr(handler, "filepath", "") or "")
        if not filepath:
            return
        path = Path(filepath)
        candidates = set()
        if path.is_file():
            candidates.update(str(item) for item in path.parent.glob("*.mdf2.*") if item.is_file())
            # A common asset layout separates mesh/ and material/ directories.
            candidates.update(str(item) for item in path.parent.parent.glob("*/*.mdf2.*") if item.is_file())
        context = resource_context_for_handler(handler)
        reader = context.pak_cached_reader if context is not None else None
        known_paths = getattr(reader, "cached_known_paths", None)
        if callable(known_paths):
            native = filepath.replace("\\", "/").casefold()
            start = native.rfind("natives/")
            if start >= 0:
                scope = native[start:].rsplit("/", 2)[0] + "/"
                candidates.update(item for item in known_paths()
                                  if item.casefold().startswith(scope) and ".mdf2." in item.casefold())
        yield from sorted(candidates, key=str.casefold)

    @classmethod
    def _read_mdf(
        cls,
        resolved,
        *,
        parse_in_subprocess: bool = False,
        parsed_cache: dict[tuple[str, int, bytes], ResolvedMdf] | None = None,
    ) -> ResolvedMdf | None:
        if resolved is None:
            return None
        actual_path, data = resolved
        cache_key = cls._mdf_key(actual_path, data)
        if parsed_cache is not None:
            cached = parsed_cache.get(cache_key)
            if cached is not None:
                return cached
        try:
            surfaces = cls._parse_mdf_surfaces(
                data,
                actual_path,
                parse_in_subprocess=parse_in_subprocess,
            )
        except Exception as exc:
            raise ValueError(
                f"Failed to parse MDF resource {actual_path!r}: {exc}"
            ) from exc
        if not surfaces:
            raise ValueError(
                f"MDF resource {actual_path!r} contains no materials"
            )
        result = ResolvedMdf(path=actual_path, surfaces=surfaces)
        if parsed_cache is not None:
            parsed_cache[cache_key] = result
        return result

    @staticmethod
    def _handler_mdf_cache(handler) -> dict[tuple[str, int, bytes], ResolvedMdf]:
        cache = getattr(handler, "_resolved_mdf_cache", None)
        if cache is None:
            cache = {}
            handler._resolved_mdf_cache = cache
        return cache

    @staticmethod
    def base_color(material: MatData) -> tuple[float, float, float, float]:
        for param in material.parameters:
            if param.name == "BaseColor" and param.component_count == 4:
                return tuple(float(value) for value in param.parameter)
        return (1.0, 1.0, 1.0, 1.0)

    @staticmethod
    def pick_primary_texture(material: MatData) -> TexHeader | None:
        first_preferred_non_null: TexHeader | None = None
        first_preferred: TexHeader | None = None
        for tex in material.textures:
            tex_path = (tex.tex_path or "").strip()
            if not tex_path:
                continue
            if tex.tex_type in PREFERRED_ALBEDO_TEXTURE_TYPES:
                if first_preferred is None:
                    first_preferred = tex
                if "null" not in tex_path.lower():
                    first_preferred_non_null = tex
                    break
        chosen = first_preferred_non_null or first_preferred
        if chosen is None:
            return None
        return None if MeshMaterialResolver.is_render_texture_path(chosen.tex_path) else chosen

    @classmethod
    def resolve_mdf_for_handler(cls, handler, *, parse_in_subprocess: bool = False) -> ResolvedMdf | None:
        filepath = str(getattr(handler, "filepath", "") or "")
        for candidate in cls.iter_mdf_candidates(filepath):
            if resolved := cls._read_mdf(
                cls._resolve_resource(handler, candidate),
                parse_in_subprocess=parse_in_subprocess,
                parsed_cache=cls._handler_mdf_cache(handler),
            ):
                return resolved
        return None

    @classmethod
    def _parse_mdf_surfaces(
        cls,
        data: bytes,
        actual_path: str,
        *,
        parse_in_subprocess: bool = False,
    ) -> dict[str, MdfSurfaceProfile]:
        if not parse_in_subprocess:
            return _extract_surface_profiles(data, actual_path)
        key = cls._mdf_key(actual_path, data)
        with _MDF_PARSES_LOCK:
            future = _MDF_PARSES.pop(key, None)
        if future is None:
            future = cls._mdf_parse_pool().submit(_extract_surface_profiles, data, actual_path)
        return future.result(timeout=5.0)

    @classmethod
    def _mdf_key(cls, actual_path: str, data: bytes) -> tuple[str, int, bytes]:
        return cls._normalized_resource_path(actual_path).casefold(), *_content_fingerprint(data)

    @classmethod
    def prefetch_mdf(cls, handler, *, explicit_mdf_path: str = "") -> None:
        if not list(getattr(getattr(handler, "mesh", None), "material_names", []) or []):
            return
        candidates = ([cls._normalized_resource_path(explicit_mdf_path)] if explicit_mdf_path
                      else cls.iter_mdf_candidates(str(getattr(handler, "filepath", "") or "")))
        try:
            for candidate in candidates:
                resolved = cls._resolve_resource(handler, candidate) if candidate else None
                if resolved is None:
                    continue
                actual_path, data = resolved
                key = cls._mdf_key(actual_path, data)
                with _MDF_PARSES_LOCK:
                    if (key not in _MDF_PARSES and len(_MDF_PARSES) < _MDF_PARSES_LIMIT
                            and key not in cls._handler_mdf_cache(handler)):
                        _MDF_PARSES[key] = cls._mdf_parse_pool().submit(_extract_surface_profiles, data, actual_path)
                return
        except (OSError, ValueError):
            return

    @staticmethod
    def _mdf_parse_pool() -> ProcessPoolExecutor:
        global _MDF_PARSE_POOL
        if _MDF_PARSE_POOL is None:
            _MDF_PARSE_POOL = ProcessPoolExecutor(max_workers=min(4, os.cpu_count() or 1))
        return _MDF_PARSE_POOL

    @classmethod
    def resolve_texture_path(
        cls,
        handler,
        texture_path: str,
        *,
        prefer_streaming: bool = False,
        resource_cache: dict[tuple[bool, str], tuple[str, bytes] | None] | None = None,
    ) -> tuple[str, bytes] | None:
        normalized = cls._normalized_resource_path(texture_path)
        if not normalized:
            return None
        cache_key = (prefer_streaming, normalized)
        if resource_cache is not None and cache_key in resource_cache:
            return resource_cache[cache_key]

        for candidate in cls._texture_candidates(normalized, prefer_streaming):
            resolved = cls._resolve_resource(handler, candidate)
            if resolved is not None:
                if resource_cache is not None:
                    resource_cache[cache_key] = resolved
                return resolved
        if resource_cache is not None:
            resource_cache[cache_key] = None
        return None

    @classmethod
    def locate_texture_path(cls, handler, texture_path: str, *,
                            prefer_streaming: bool = False) -> ResourceLocation | None:
        normalized = cls._normalized_resource_path(texture_path)
        for candidate in cls._texture_candidates(normalized, prefer_streaming) if normalized else ():
            found = cls._locate_resource(handler, candidate)
            if found is not None:
                return found
        return None

    @staticmethod
    def _texture_candidates(normalized: str, prefer_streaming: bool) -> tuple[str, ...]:
        if prefer_streaming and not normalized.startswith("streaming/"):
            return f"streaming/{normalized}", normalized
        return (normalized,)

    @staticmethod
    def iter_mdf_candidates(mesh_filepath: str) -> Iterable[str]:
        if not mesh_filepath:
            return
        normalized = mesh_filepath.replace("\\", "/")
        lower = normalized.lower()
        idx = lower.rfind(".stmesh")
        if idx == -1:
            idx = lower.rfind(".mesh")
        if idx == -1:
            return
        base = normalized[:idx]
        bases = [base]

        lower_base = base.lower()
        for marker in ("/natives/stm/", "/natives/x64/"):
            marker_index = lower_base.rfind(marker)
            if marker_index >= 0:
                resource_base = base[marker_index + 1 :]
                if resource_base not in bases:
                    bases.append(resource_base)
                break

        for candidate_base in bases:
            yield f"{candidate_base}.mdf2"
            yield f"{candidate_base}_Mat.mdf2"
            yield f"{candidate_base}_00.mdf2"
            yield f"{candidate_base}_v00.mdf2"
            # The first variant, ``_a``: most ONIWOTS meshes and SF6's and MHWS's SpeedTree meshes.
            yield f"{candidate_base}_a.mdf2"

    @classmethod
    def _resolve_mmtr_resource(cls, handler, resource_path: str):
        from file_handlers.mmtr.mmtrs_file import resolve_mmtr_resource
        return resolve_mmtr_resource(resource_path, lambda path: cls._resolve_resource(handler, path))

    @classmethod
    def _resolve_resource(cls, handler, resource_path: str):
        found = cls._locate_resource(handler, resource_path)
        return None if found is None else found.read()

    @staticmethod
    def _locate_resource(handler, resource_path: str) -> ResourceLocation | None:
        found = locate_handler_resource(handler, resource_path)
        if found is not None:
            return found
        source_path = Path(str(getattr(handler, "filepath", "") or ""))
        for path in (Path(resource_path), *((source_path.parent / Path(resource_path).name,)
                                            if source_path.is_file() else ())):
            local = locate_local_resource(path)
            if local is not None:
                return ResourceLocation(local)
        return None

    @staticmethod
    def resource_scope(handler) -> tuple:
        source_path = Path(str(getattr(handler, "filepath", "") or ""))
        return resource_context_for_handler(handler), source_path.parent if source_path.is_file() else None
