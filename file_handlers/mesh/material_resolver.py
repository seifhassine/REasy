from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from file_handlers.mdf.mdf_file import MatData, MdfFile, TexHeader
from file_handlers.mmtr import (
    DMC5_MMTR_PROFILE,
    MmtrContractError,
    parse_mmtr_skinning_contract,
)
from utils.resource_file_utils import resolve_handler_resource_data


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
class MdfSurfaceProfile:
    material_name: str
    game_version: str = ""
    tint: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    two_sided: bool = False
    mmtr_path: str = ""
    texture_type: str = ""
    texture_path: str = ""
    textures: tuple[MdfTextureProfile, ...] = ()
    parameter_names: tuple[str, ...] = ()


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


def _extract_surface_profiles(
    mdf_data: bytes,
    actual_path: str,
    game_version: str = "",
) -> dict[str, MdfSurfaceProfile]:
    mdf = MdfFile()
    mdf.read(mdf_data, actual_path)
    resolved: dict[str, MdfSurfaceProfile] = {}
    for material in mdf.materials:
        texture = MeshMaterialResolver.pick_primary_texture(material)
        resolved[material.header.mat_name] = MdfSurfaceProfile(
            material_name=material.header.mat_name,
            game_version=game_version,
            tint=MeshMaterialResolver.base_color(material),
            two_sided=bool(material.header.material_flags & _TWO_SIDED_FLAGS),
            mmtr_path=material.header.mmtr_path,
            texture_type=texture.tex_type if texture else "",
            texture_path=texture.tex_path if texture else "",
            textures=tuple(
                MdfTextureProfile(item.tex_type, item.tex_path)
                for item in material.textures
                if item.tex_type and item.tex_path
            ),
            parameter_names=tuple(
                parameter.name
                for parameter in material.parameters
                if parameter.name
            ),
        )
    return resolved


class MeshMaterialResolver:
    @staticmethod
    def _normalized_resource_path(path: str) -> str:
        return (path or "").replace("\\", "/").lstrip("@/").rstrip("\x00")

    @staticmethod
    def _handler_game_version(handler) -> str:
        app = getattr(handler, "app", None)
        project = getattr(app, "proj_dock", None) if app is not None else None
        manager = (
            getattr(app, "project_manager", None)
            if app is not None
            else None
        )
        settings = getattr(app, "settings", {}) if app is not None else {}
        return str(
            getattr(handler, "game_version", "")
            or getattr(getattr(handler, "resource_context", None), "game", "")
            or getattr(manager, "current_game", "")
            or getattr(project, "current_game", "")
            or getattr(app, "current_game", "")
            or (
                settings.get("game_version", "")
                if isinstance(settings, dict)
                else ""
            )
            or ""
        )

    @staticmethod
    def is_render_texture_path(path: str) -> bool:
        normalized = (path or "").replace("\\", "/").lower()
        return ".rtex" in normalized

    @classmethod
    def _material_binding(
        cls,
        handler,
        resolved_mdf: ResolvedMdf,
        mesh_name: str,
        *,
        prefer_streaming: bool,
        resolve_textures: bool,
        resource_cache: dict[tuple[bool, str], tuple[str, bytes] | None] | None,
    ) -> MeshMaterialBinding:
        surface = resolved_mdf.surfaces.get(mesh_name)
        if surface is None:
            return MeshMaterialBinding(mesh_name, status="Missing MDF material")
        if not surface.texture_path:
            return MeshMaterialBinding(
                mesh_material_name=mesh_name,
                mdf_material_name=mesh_name,
                surface=surface,
                status="No usable texture",
            )

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
        return MeshMaterialBinding(
            mesh_material_name=mesh_name,
            mdf_material_name=mesh_name,
            texture_type=surface.texture_type,
            texture_path=surface.texture_path,
            resolved_texture_path=resolved_tex[0] if resolved_tex else "",
            resolved_texture_data=resolved_tex[1] if resolved_tex else None,
            surface=surface,
            status=status,
        )

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
            bindings.append(
                cls._material_binding(
                    handler,
                    resolved_mdf,
                    mesh_name,
                    prefer_streaming=prefer_streaming,
                    resolve_textures=resolve_textures,
                    resource_cache=resource_cache,
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
        normalized = cls._normalized_resource_path(mdf_path)
        if not normalized:
            return None
        return cls._read_mdf(
            cls._resolve_resource(handler, normalized),
            parse_in_subprocess=parse_in_subprocess,
            game_version=cls._handler_game_version(handler),
        )

    @classmethod
    def resolve_dmc5_skinning_influences(
        cls,
        handler,
        *,
        explicit_mdf_path: str = "",
    ) -> dict[str, int]:
        """Resolve each mesh material's 4/8-weight vertex-shader contract."""
        cache_key = (
            DMC5_MMTR_PROFILE.name,
            cls._normalized_resource_path(explicit_mdf_path).lower(),
        )
        material_cache = getattr(
            handler,
            "_material_skinning_cache",
            None,
        )
        if material_cache is None:
            material_cache = {}
            handler._material_skinning_cache = material_cache
        if cache_key in material_cache:
            return material_cache[cache_key]

        resolved_mdf = (
            cls.resolve_mdf_path_for_handler(handler, explicit_mdf_path)
            if explicit_mdf_path
            else cls.resolve_mdf_for_handler(handler)
        )
        if resolved_mdf is None:
            raise ValueError("DMC5 material skinning requires a resolvable MDF")

        shader_cache = getattr(handler, "_mmtr_skinning_cache", None)
        if shader_cache is None:
            shader_cache = {}
            handler._mmtr_skinning_cache = shader_cache

        mesh = getattr(handler, "mesh", None)
        material_names = list(getattr(mesh, "material_names", ()) or ())
        influences: dict[str, int] = {}
        for material_name in material_names:
            surface = resolved_mdf.surfaces.get(material_name)
            if surface is None:
                raise ValueError(
                    f"DMC5 MDF has no material named {material_name!r}"
                )
            mmtr_path = cls._normalized_resource_path(surface.mmtr_path)
            if not mmtr_path:
                raise ValueError(
                    f"DMC5 material {material_name!r} has no MMTR path"
                )
            mmtr_key = (DMC5_MMTR_PROFILE.name, mmtr_path.lower())
            influence_count = shader_cache.get(mmtr_key)
            if influence_count is None:
                resolved_mmtr = cls._resolve_resource(handler, mmtr_path)
                if resolved_mmtr is None:
                    raise ValueError(
                        f"DMC5 material {material_name!r} MMTR was not found: "
                        f"{mmtr_path}"
                    )
                actual_path, data = resolved_mmtr
                try:
                    contract = parse_mmtr_skinning_contract(
                        data,
                        DMC5_MMTR_PROFILE,
                        label=actual_path,
                    )
                except MmtrContractError as exc:
                    raise ValueError(
                        f"DMC5 material {material_name!r} MMTR contract is "
                        f"unsupported: {exc}"
                    ) from exc
                influence_count = contract.influence_count
                shader_cache[mmtr_key] = influence_count
            influences[material_name] = influence_count

        material_cache[cache_key] = influences
        return influences

    @classmethod
    def _read_mdf(
        cls,
        resolved,
        *,
        parse_in_subprocess: bool = False,
        game_version: str = "",
    ) -> ResolvedMdf | None:
        if resolved is None:
            return None
        actual_path, data = resolved
        try:
            surfaces = cls._parse_mdf_surfaces(
                data,
                actual_path,
                parse_in_subprocess=parse_in_subprocess,
                game_version=game_version,
            )
        except Exception as exc:
            raise ValueError(
                f"Failed to parse MDF resource {actual_path!r}: {exc}"
            ) from exc
        if not surfaces:
            raise ValueError(
                f"MDF resource {actual_path!r} contains no materials"
            )
        return ResolvedMdf(path=actual_path, surfaces=surfaces)

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
                game_version=cls._handler_game_version(handler),
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
        game_version: str = "",
    ) -> dict[str, MdfSurfaceProfile]:
        if not parse_in_subprocess:
            return _extract_surface_profiles(data, actual_path, game_version)
        pool = cls._mdf_parse_pool()
        future = pool.submit(
            _extract_surface_profiles,
            data,
            actual_path,
            game_version,
        )
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
        hit = resolve_handler_resource_data(
            handler,
            resource_path,
            allow_selection_dialog=False,
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
