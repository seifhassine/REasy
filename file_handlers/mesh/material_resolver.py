from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from file_handlers.mdf.mdf_file import MatData, MdfFile, TexHeader
from utils.resource_file_utils import get_path_prefix_for_game, resolve_resource_data


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
class MdfSurfaceProfile:
    material_name: str
    tint: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    two_sided: bool = False
    texture_type: str = ""
    texture_path: str = ""


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


_MDF_PARSE_POOL: ProcessPoolExecutor | None = None
_TWO_SIDED_FLAGS = (1 << 0) | (1 << 8)


def _extract_surface_profiles(mdf_data: bytes, actual_path: str) -> dict[str, MdfSurfaceProfile]:
    mdf = MdfFile()
    if not mdf.read(mdf_data, actual_path):
        return {}
    resolved: dict[str, MdfSurfaceProfile] = {}
    for material in mdf.materials:
        texture = MeshMaterialResolver.pick_primary_texture(material)
        resolved[material.header.mat_name] = MdfSurfaceProfile(
            material_name=material.header.mat_name,
            tint=MeshMaterialResolver.base_color(material),
            two_sided=bool(material.header.material_flags & _TWO_SIDED_FLAGS),
            texture_type=texture.tex_type if texture else "",
            texture_path=texture.tex_path if texture else "",
        )
    return resolved


class MeshMaterialResolver:
    @staticmethod
    def _project_resolution_context(handler):
        app = getattr(handler, "app", None)
        proj = getattr(app, "proj_dock", None) if app is not None else None
        proj_mgr = getattr(app, "project_manager", None) if app is not None else None
        game = str(getattr(proj_mgr, "current_game", "") or "")
        return proj, get_path_prefix_for_game(game)

    @staticmethod
    def is_render_texture_path(path: str) -> bool:
        normalized = (path or "").replace("\\", "/").lower()
        return ".rtex" in normalized

    @classmethod
    def resolve_for_handler(
        cls,
        handler,
        *,
        explicit_mdf_path: str = "",
        prefer_streaming: bool = False,
        resolve_textures: bool = True,
        parse_in_subprocess: bool = False,
        resource_cache: dict[tuple[bool, str], tuple[str, bytes] | None] | None = None,
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

        bindings: list[MeshMaterialBinding] = []
        for mesh_name in material_names:
            surface = resolved_mdf.surfaces.get(mesh_name)
            if surface is None:
                bindings.append(MeshMaterialBinding(mesh_name, status="Missing MDF material"))
                continue

            if not surface.texture_path:
                bindings.append(
                    MeshMaterialBinding(
                        mesh_material_name=mesh_name,
                        mdf_material_name=mesh_name,
                        surface=surface,
                        status="No usable texture",
                    )
                )
                continue

            resolved_tex = None
            status = "Resolved MDF"
            if resolve_textures:
                resolved_tex = cls.resolve_texture_path(
                    handler,
                    surface.texture_path,
                    prefer_streaming=prefer_streaming,
                    resource_cache=resource_cache,
                )
                status = "Resolved" if resolved_tex else "Texture not found"
            bindings.append(
                MeshMaterialBinding(
                    mesh_material_name=mesh_name,
                    mdf_material_name=mesh_name,
                    texture_type=surface.texture_type,
                    texture_path=surface.texture_path,
                    resolved_texture_path=resolved_tex[0] if resolved_tex else "",
                    resolved_texture_data=resolved_tex[1] if resolved_tex else None,
                    surface=surface,
                    status=status,
                )
            )
        return resolved_mdf, bindings

    @classmethod
    def resolve_mdf_path_for_handler(
        cls,
        handler,
        mdf_path: str,
        *,
        parse_in_subprocess: bool = False,
    ) -> ResolvedMdf | None:
        normalized = (mdf_path or "").replace("\\", "/").lstrip("@/").rstrip("\x00")
        if not normalized:
            return None
        return cls._read_mdf(cls._resolve_resource(handler, normalized), parse_in_subprocess=parse_in_subprocess)

    @classmethod
    def _read_mdf(cls, resolved, *, parse_in_subprocess: bool = False) -> ResolvedMdf | None:
        if resolved is None:
            return None
        actual_path, data = resolved
        try:
            surfaces = cls._parse_mdf_surfaces(data, actual_path, parse_in_subprocess=parse_in_subprocess)
        except Exception:
            return None
        return ResolvedMdf(path=actual_path, surfaces=surfaces) if surfaces else None

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
            if resolved := cls._read_mdf(cls._resolve_resource(handler, candidate), parse_in_subprocess=parse_in_subprocess):
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
        pool = cls._mdf_parse_pool()
        future = pool.submit(_extract_surface_profiles, data, actual_path)
        return future.result(timeout=5.0)

    @staticmethod
    def _mdf_parse_pool() -> ProcessPoolExecutor:
        global _MDF_PARSE_POOL
        if _MDF_PARSE_POOL is None:
            _MDF_PARSE_POOL = ProcessPoolExecutor(max_workers=1)
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
        normalized = (texture_path or "").replace("\\", "/").lstrip("@/")
        if not normalized:
            return None
        cache_key = (prefer_streaming, normalized)
        if resource_cache is not None and cache_key in resource_cache:
            return resource_cache[cache_key]

        candidates: list[str] = []
        if prefer_streaming and not normalized.startswith("streaming/"):
            candidates.append(f"streaming/{normalized}")
        candidates.append(normalized)

        for candidate in candidates:
            resolved = cls._resolve_resource(handler, candidate)
            if resolved is not None:
                if resource_cache is not None:
                    resource_cache[cache_key] = resolved
                return resolved
        if resource_cache is not None:
            resource_cache[cache_key] = None
        return None

    @staticmethod
    def iter_mdf_candidates(mesh_filepath: str) -> Iterable[str]:
        if not mesh_filepath:
            return
        normalized = mesh_filepath.replace("\\", "/")
        idx = normalized.lower().rfind(".mesh")
        if idx == -1:
            return
        base = normalized[:idx]
        yield f"{base}.mdf2"
        yield f"{base}_Mat.mdf2"
        yield f"{base}_00.mdf2"

    @classmethod
    def _resolve_resource(cls, handler, resource_path: str):
        context = getattr(handler, "_resource_context", None)
        if context:
            hit = resolve_resource_data(resource_path, *context)
            if hit is not None:
                return hit

        proj, path_prefix = cls._project_resolution_context(handler)

        if proj is not None:
            hit = resolve_resource_data(
                resource_path,
                getattr(proj, "project_dir", None),
                getattr(proj, "unpacked_dir", None),
                path_prefix,
                getattr(proj, "_pak_cached_reader", None),
            )
            if hit is not None:
                return hit

        direct = cls._find_local_resource(Path(resource_path))
        if direct is not None:
            return direct

        source_path = Path(str(getattr(handler, "filepath", "") or ""))
        if source_path.is_file():
            fallback = cls._find_local_resource(source_path.parent / Path(resource_path).name)
            if fallback is not None:
                return fallback
        return None

    @staticmethod
    def _find_local_resource(path: Path):
        if path.is_file():
            return str(path), path.read_bytes()

        parent = path.parent if str(path.parent) not in ("", ".") else Path.cwd()
        if not parent.exists():
            return None

        for candidate in parent.glob(path.name + ".*"):
            if candidate.is_file():
                return str(candidate), candidate.read_bytes()
        return None
