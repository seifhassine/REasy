from copy import deepcopy
from dataclasses import dataclass, replace
import logging
from pathlib import Path

from utils.resource_file_utils import resource_version_from_path
from .material_context import MaterialInstanceContext, bind_material_context
from .mmtr_file import MmtrFile
from .mmtrs_file import MmtrsFile, family_member, is_mmtrs_resource, mmtrs_family


@dataclass(frozen=True, slots=True)
class MmtrMaterialFamily:
    source_path: str
    resource: MmtrsFile
    members: tuple


def family_member_filename(target, member, lod):
    stem = Path(target).name.rsplit(".mmtrs", 1)[0]
    version = resource_version_from_path(member.source_label, "mmtr")
    if version is None:
        raise ValueError("A family member needs a verified MMTR resource version.")
    return f"{stem}_lod{lod}.mmtr.{version}"


def distinct_members(family) -> tuple[tuple[tuple[int, ...], object], ...]:
    lods: dict[str, list[int]] = {}
    for lod, path in enumerate(family.resource.material_paths):
        lods.setdefault(path.replace("\\", "/").casefold(), []).append(lod)
    return tuple((tuple(indices), family.members[indices[0]]) for indices in lods.values())


def parsed_master(data: bytes, game: str, resource_version: int | None, label: str):
    return MmtrFile.from_bytes(data, game=game, resource_version=resource_version, label=label).require_document()


def _family(mmtr_path: str, loader) -> tuple[str, MmtrsFile]:
    hit = loader(mmtr_path)
    if hit is None:
        raise FileNotFoundError(mmtr_path)
    return hit[0], mmtrs_family(hit)


def bind_first_member(context: MaterialInstanceContext, mmtr_path: str, loader, masters):
    _family_path, family = _family(mmtr_path, loader)
    return bind_material_context(masters(*family_member(family, 0, loader)), context, mmtr_path,
                                 family=family, shader_lod=0)


def bind_drawn_master(context: MaterialInstanceContext, mmtr_path: str, loader, masters):
    hit = loader(mmtr_path)
    if hit is None:
        raise FileNotFoundError(mmtr_path)
    if is_mmtrs_resource(mmtr_path, hit[0]):
        return bind_first_member(context, mmtr_path, loader, masters)
    return bind_material_context(masters(*hit), context, mmtr_path)


def load_material_family(material, loader, *, game: str):
    context, mmtr_path = MaterialInstanceContext.from_material(material), material.header.mmtr_path
    family_path, family = _family(mmtr_path, loader)
    documents, members = {}, []
    for lod, path in enumerate(family.material_paths):
        key = path.replace("\\", "/").casefold()
        if key not in documents:
            found, data = family_member(family, lod, loader)
            documents[key] = parsed_master(data, game, resource_version_from_path(found, "mmtr"), found)
        members.append(bind_material_context(documents[key], context, mmtr_path, family=family, shader_lod=lod))
    root = members[0]
    resources = {(r.resource_kind, r.name.casefold()): r for r in root.material_interface.resources}
    texture_names = {name.casefold() for name in root.material_interface.descriptor_layouts[0].textures}
    for member in members:
        for resource in member.material_interface.resources:
            if resource.resource_kind == "shader_resource" and resource.name.casefold() in texture_names:
                key = (resource.resource_kind, resource.name.casefold())
                existing = resources.setdefault(key, resource)
                if existing.type_names != resource.type_names:
                    raise ValueError(f"MMTRS texture {resource.name} has inconsistent native dimensions.")
    return replace(root, material_interface=replace(root.material_interface, resources=tuple(resources.values())),
                   _material_input_usage_cache={}, _material_texture_contracts_cache={},
                   material_family=MmtrMaterialFamily(family_path, family, tuple(members)))


def seed_material_family(document, project, *, defer_shader_recovery):
    from .profiles.re_engine_shader_graph import _master, _bundle, SEMANTIC_BUNDLE_VERSION
    family = document.material_family

    def prepare(destination):
        boundary = _master(destination)
        recipes, indices = [], [0] * len(family.members)
        for index, (lods, member) in enumerate(distinct_members(family)):
            programs, routes = _bundle(member, member.profile.shader_graph_profile)
            recipes.append({"semantic_programs": programs, "program_routes": routes,
                            "parameter_storage": member.material_interface.parameter_storage,
                            "parameter_buffer_sizes": list(member.material_interface.user_buffer_sizes)})
            for lod in lods:
                indices[lod] = index
        boundary.properties.update(recipes[0])
        recipes[0] = {"root": True}
        boundary.properties.update(family_recipes=recipes, family_recipe_indices=indices, semantic_bundle_version=SEMANTIC_BUNDLE_VERSION)
    project._prepare_semantics = prepare
    if not defer_shader_recovery:
        project.prepare_semantics()


def family_member_project(project, lod):
    from .pipeline_settings import member_states
    from .profiles.re_engine_shader_graph import _master
    project.prepare_semantics()
    master = _master(project)
    indices, recipes = master.properties.get("family_recipe_indices", ()), master.properties.get("family_recipes", ())
    if not 0 <= lod < len(indices) or not 0 <= indices[lod] < len(recipes):
        raise ValueError("Material project has no recipe for this shader LOD.")
    memo = {id(value): value for value in recipes}
    result = deepcopy(project, memo)
    boundary = _master(result)
    recipe = recipes[indices[lod]]
    if not recipe.get("root"):
        boundary.properties.update(deepcopy(recipe))
    boundary.properties["shader_lod"] = lod
    boundary.properties.pop("family_recipes", None)
    boundary.properties.pop("family_recipe_indices", None)
    result.pipeline_states = member_states(project.pipeline_states, lod)
    result._prepare_semantics = None
    return result


def build_material_family(project, document):
    from .material_project import _build_material_project, MaterialBuildError
    family = document.material_family
    project.prepare_semantics()
    builds = []
    for lod, member in enumerate(family.members):
        try:
            logging.getLogger(__name__).info("Compiling MMTRS shader LOD %d of %d", lod + 1, len(family.members))
            builds.append(_build_material_project(family_member_project(project, lod), member))
        except (ValueError, MaterialBuildError) as exc:
            detail = getattr(exc, "technical_details", "")
            raise MaterialBuildError(f"MMTRS LOD {lod}: {exc}" + (f"\n{detail}" if detail else ""), technical_details=detail) from exc
    first = builds[0]
    if any((b.buffer_layout, b.texture_layout, b.descriptor_layout) !=
           (first.buffer_layout, first.texture_layout, first.descriptor_layout) for b in builds):
        raise MaterialBuildError("MMTRS members do not compile to one shared MDF interface.")
    return replace(builds[0], family_builds=tuple(builds))
