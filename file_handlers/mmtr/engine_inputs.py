"""See docs/internals/engine-input-catalog.md."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from file_handlers.sdf.shader_contract_storage import variable_to_dict
from file_handlers.sdf.shader_graph import ShaderContractBuffer, ShaderContractResource, ShaderContractVariable
from file_handlers.build_catalog import catalog_file_name, catalog_reader

CATALOG_FORMAT = 3
RESOURCE_TABLES = {"shader_resource": "t", "sampler": "s"}
CATALOG_DIRECTORY = "resources/data/engine_inputs"


class EngineInputCatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EngineBufferLayout:
    backend: str
    buffer: ShaderContractBuffer
    table_byte_size: int
    table_variables: tuple[tuple[str, int, int], ...]
    type_code: int
    reserved: int

    @property
    def name(self) -> str:
        return self.buffer.name


@dataclass(frozen=True, slots=True)
class EngineResourceLayout:
    """``resource`` has bind point 0, not the game's slot; structured buffers are typed by stride."""
    backend: str
    table: str
    resource: ShaderContractResource
    type_code: int
    reserved: int

    @property
    def name(self) -> str:
        return self.resource.name


PassSets = Mapping[tuple[str, str, str], Mapping[str, tuple[int, ...]]]


@dataclass(frozen=True, slots=True)
class EngineInputCatalog:
    game: str
    resource_version: int
    layouts: tuple[EngineBufferLayout, ...]
    passes: PassSets
    resources: tuple[EngineResourceLayout, ...] = ()
    resource_passes: PassSets = field(default_factory=dict)
    materials: int = 0
    buffer_materials: Mapping[str, int] = field(default_factory=dict)
    resource_materials: Mapping[str, int] = field(default_factory=dict)

    def pass_buffers(self, backend: str, program: str, stage: str) -> Mapping[str, tuple[int, ...]]:
        return self.passes.get((backend, program, stage), {})

    def pass_layout(self, backend: str, program: str, stage: str, buffer: str) -> EngineBufferLayout | None:
        found = self.pass_buffers(backend, program, stage).get(buffer, ())
        return self.layouts[found[0]] if len(found) == 1 else None

    def pass_resources(self, backend: str, program: str, stage: str) -> Mapping[str, tuple[int, ...]]:
        return self.resource_passes.get((backend, program, stage), {})


def _variable(data: Mapping) -> ShaderContractVariable:
    return ShaderContractVariable(
        str(data["name"]), int(data["byte_offset"]), int(data["byte_size"]),
        data["hlsl_declaration"], data["graph_type"], int(data["array_length"]), bool(data["matrix"]),
        str(data["struct_name"]), tuple(_variable(member) for member in data["members"]),
    )


def resource_to_dict(resource: ShaderContractResource) -> dict:
    return {"name": resource.name, "input_type": resource.input_type, "return_type": resource.return_type,
            "dimension": resource.dimension, "sample_count": resource.sample_count, "bind_count": resource.bind_count,
            "flags": resource.flags, "register_space": resource.register_space}


def resource_from_dict(data: Mapping) -> ShaderContractResource:
    return ShaderContractResource(str(data["name"]), str(data["input_type"]), str(data["return_type"]),
                                  str(data["dimension"]), int(data["sample_count"]), 0, int(data["bind_count"]),
                                  int(data["flags"]), int(data["register_space"]))


def _passes_from_dict(entries: Mapping, sets: list[tuple[int, ...]], items, kind: str) -> dict:
    for indices in sets:
        if any(not 0 <= index < len(items) for index in indices):
            raise EngineInputCatalogError(f"engine input catalog {kind} pass set points outside its entries")
    passes = {}
    for backend, programs in entries.items():
        for program, stages in programs.items():
            for stage, set_index in stages.items():
                grouped: dict[str, list[int]] = {}
                for index in sets[int(set_index)]:
                    if items[index].backend != backend:
                        raise EngineInputCatalogError(f"{program} {stage} {backend} lists a {items[index].backend} {kind}")
                    grouped.setdefault(items[index].name, []).append(index)
                passes[(str(backend), str(program), str(stage))] = {name: tuple(found) for name, found in grouped.items()}
    return passes


def _passes_to_dict(passes: PassSets) -> tuple[list[list[int]], dict]:
    pass_sets: dict[tuple[int, ...], int] = {}
    entries: dict[str, dict[str, dict[str, int]]] = {}
    for (backend, program, stage), found in sorted(passes.items()):
        key = tuple(sorted(index for indices in found.values() for index in indices))
        entries.setdefault(backend, {}).setdefault(program, {})[stage] = pass_sets.setdefault(key, len(pass_sets))
    return [list(key) for key in sorted(pass_sets, key=pass_sets.get)], entries


def _sets(data) -> list[tuple[int, ...]]:
    return [tuple(int(index) for index in item) for item in data]


def catalog_from_dict(data: Mapping) -> EngineInputCatalog:
    if data.get("format") != CATALOG_FORMAT:
        raise EngineInputCatalogError(f"unsupported engine input catalog format {data.get('format')!r}")
    layouts = tuple(
        EngineBufferLayout(
            str(item["backend"]),
            ShaderContractBuffer(str(item["buffer"]), int(item["byte_size"]), 0, 1,
                                 tuple(_variable(variable) for variable in item["variables"]),
                                 int(item["register_space"])),
            int(item["table_byte_size"]),
            tuple((str(name), int(offset), int(size)) for name, offset, size in item["table_variables"]),
            int(item["type_code"]), int(item["reserved"]),
        )
        for item in data["layouts"]
    )
    passes = _passes_from_dict(data["passes"], _sets(data["pass_sets"]), layouts, "layout")
    resources = []
    for item in data["resources"]:
        resource = resource_from_dict(item["resource"])
        if RESOURCE_TABLES.get(item["table"]) != resource.register_class:
            raise EngineInputCatalogError(f"engine resource {resource.name!r} does not belong to table {item['table']!r}")
        resources.append(EngineResourceLayout(str(item["backend"]), str(item["table"]), resource,
                                              int(item["type_code"]), int(item["reserved"])))
    resources = tuple(resources)
    resource_passes = _passes_from_dict(data["resource_passes"], _sets(data["resource_pass_sets"]), resources, "resource")
    counts = data["materials"]
    return EngineInputCatalog(
        str(data["game"]), int(data["resource_version"]), layouts, passes, resources, resource_passes,
        int(counts["total"]), {str(name): int(count) for name, count in counts["buffers"].items()},
        {str(name): int(count) for name, count in counts["resources"].items()},
    )


def catalog_to_dict(catalog: EngineInputCatalog, source: Mapping) -> dict:
    pass_sets, passes = _passes_to_dict(catalog.passes)
    resource_pass_sets, resource_passes = _passes_to_dict(catalog.resource_passes)
    return {
        "format": CATALOG_FORMAT,
        "game": catalog.game,
        "resource_version": catalog.resource_version,
        "source": dict(source),
        "layouts": [
            {"backend": layout.backend, "buffer": layout.name, "byte_size": layout.buffer.byte_size,
             "register_space": layout.buffer.register_space, "table_byte_size": layout.table_byte_size,
             "type_code": layout.type_code,
             "reserved": layout.reserved, "variables": [variable_to_dict(item) for item in layout.buffer.variables],
             "table_variables": [list(item) for item in layout.table_variables]}
            for layout in catalog.layouts
        ],
        "pass_sets": pass_sets,
        "passes": passes,
        "resources": [
            {"backend": item.backend, "table": item.table, "resource": resource_to_dict(item.resource),
             "type_code": item.type_code, "reserved": item.reserved}
            for item in catalog.resources
        ],
        "resource_pass_sets": resource_pass_sets,
        "resource_passes": resource_passes,
        "materials": {"total": catalog.materials, "buffers": dict(sorted(catalog.buffer_materials.items())),
                      "resources": dict(sorted(catalog.resource_materials.items()))},
    }


engine_input_catalog_for = catalog_reader(CATALOG_DIRECTORY, catalog_from_dict, EngineInputCatalogError,
                                          "engine input")


__all__ = [
    "CATALOG_DIRECTORY", "RESOURCE_TABLES", "EngineBufferLayout", "EngineInputCatalog", "EngineInputCatalogError",
    "EngineResourceLayout", "catalog_file_name", "catalog_from_dict", "catalog_to_dict",
    "engine_input_catalog_for", "resource_from_dict", "resource_to_dict",
]
