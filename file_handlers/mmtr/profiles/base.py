from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from file_handlers.sdf.profiles import SdfGameProfile
from file_handlers.sdf.profiles.base import ProfileRegistry
lazy from file_handlers.material_names import logical_material_parameter_name
lazy from file_handlers.sdf.models import SdfProgram

lazy from ..models import MmtrNameSemantics, MmtrProgramContext

if TYPE_CHECKING:
    from .re_engine_shader_graph import ReEngineShaderGraphProfile


def surface_prefix(name: str, roots: tuple[str, ...]) -> tuple[str, bool, bool]:
    for prefix, alpha_test, two_sided in (
        ("ATS", True, True),
        ("TS", False, True),
        ("A", True, False),
    ):
        core = name[len(prefix) :]
        if name.startswith(prefix) and core.startswith(roots):
            return core, alpha_test, two_sided
    return name, False, False


def bound_texture_names(document) -> frozenset[str]:
    layouts = document.material_interface.descriptor_layouts
    return frozenset(name.casefold() for name in layouts[0].textures) if len(layouts) == 1 else frozenset()


@dataclass(frozen=True, slots=True)
class MmtrGameProfile:
    game: str
    name: str
    display_name: str
    aliases: tuple[str, ...]
    resource_versions: frozenset[int]
    sdf_profile: SdfGameProfile
    program_classifier: Callable[[str], MmtrNameSemantics]
    document_classifier: Callable[[tuple[MmtrNameSemantics, ...]], str]
    shader_graph_profile: ReEngineShaderGraphProfile
    program_refiner: Callable[
        [MmtrNameSemantics, MmtrProgramContext],
        MmtrNameSemantics,
    ] | None = None
    default_for_version: bool = False
    index_semantic: str = "INDEX"
    weight_semantic: str = "WEIGHT"
    user_material_buffer_names: tuple[str, ...] = ("UserMaterial",)
    reflected_material_variable_prefixes: tuple[str, ...] = ()
    current_resource_version: int | None = None

    @property
    def build_resource_version(self) -> int | None:
        if self.current_resource_version is not None:
            return self.current_resource_version
        if len(self.resource_versions) == 1:
            return next(iter(self.resource_versions))
        return None

    def accepts(self, resource_version: int | None) -> bool:
        return resource_version is None or int(resource_version) in self.resource_versions

    def material_parameter_name(self, reflected_name: str) -> str:
        return logical_material_parameter_name(
            reflected_name, buffer_names=self.user_material_buffer_names,
            prefixes=self.reflected_material_variable_prefixes,
        )

    def classify_program(self, name: str, *, program: SdfProgram, program_count: int) -> MmtrNameSemantics:
        descriptor = self.program_classifier(str(name))
        if self.program_refiner is not None:
            depth = program.depth_stencil_state
            descriptor = self.program_refiner(descriptor, MmtrProgramContext(
                program=program,
                program_count=int(program_count),
                depth_writes=None if depth is None else depth.depth_write_mask != "zero",
            ))
        return descriptor


_REGISTRY = ProfileRegistry("MMTR", lambda profile: profile.sdf_profile.variant_of)


def register_mmtr_profile(profile: MmtrGameProfile) -> MmtrGameProfile:
    key = _REGISTRY.checked_key(profile)
    if profile.current_resource_version is not None and profile.current_resource_version not in profile.resource_versions:
        raise ValueError(f"MMTR profile {profile.game!r} selects an unsupported build version")
    if not profile.resource_versions.issubset(profile.sdf_profile.resource_versions):
        raise ValueError(
            f"MMTR profile {profile.game!r} is incompatible with its SDF profile"
        )
    return _REGISTRY.add(profile, key)


def mmtr_profile_for_game(game: object) -> MmtrGameProfile | None:
    return _REGISTRY.for_game(game)


def same_mmtr_profile(first: object, second: object) -> bool:
    left, right = mmtr_profile_for_game(first), mmtr_profile_for_game(second)
    if left is None or right is None:
        return str(first).casefold() == str(second).casefold()
    return left is right


def mmtr_profiles() -> tuple[MmtrGameProfile, ...]:
    return _REGISTRY.all()


def mmtr_profile_for_resource(resource_version: int | None, *, game: object = "") -> MmtrGameProfile:
    return _REGISTRY.for_resource(resource_version, game=game)


__all__ = [
    "MmtrGameProfile",
    "mmtr_profile_for_game",
    "mmtr_profile_for_resource",
    "same_mmtr_profile",
    "mmtr_profiles",
    "register_mmtr_profile",
]
