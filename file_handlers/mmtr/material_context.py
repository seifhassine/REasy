from dataclasses import dataclass, replace

from file_handlers.material_names import is_private_material_parameter, resolve_material_name
from file_handlers.mdf.material_layout import MaterialDescriptorLayout
from file_handlers.sdf.models import program_tables

from .models import MmtrMaterialInterface, MmtrMaterialParameter
from .material_variant import mmtr_resource_path_from_file


@dataclass(frozen=True, slots=True)
class MaterialInstanceContext:
    layout: MaterialDescriptorLayout
    parameters: tuple[MmtrMaterialParameter, ...]

    @classmethod
    def from_material(cls, material):
        layout = MaterialDescriptorLayout.from_material(material)
        parameters = []
        names, words = set(), set()
        for field in material.parameters:
            if is_private_material_parameter(field.name):
                continue
            name, offset = str(field.name), int(field.param_rel_offset)
            width = max(1, int(field.component_count))
            occupied = set(range(offset, offset + width * 4, 4))
            if (not name or name in names or offset < 0 or offset % 4
                    or width > 4 or offset + width * 4 > layout.parameter_size or words & occupied):
                raise ValueError(f"MDF parameter {name!r} has an invalid or overlapping layout.")
            names.add(name)
            words.update(occupied)
            parameters.append(MmtrMaterialParameter(name, offset, width * 4,
                                                    ("float" if width == 1 else f"float{width}",), 0))
        return cls(layout, tuple(parameters))

    def bind_interface(self, native: MmtrMaterialInterface, *, named_sizes=(), shader_lod=None):
        parameters = list(self.parameters)
        fields = {p.name: p for p in parameters}
        words = {word for p in parameters for word in range(p.byte_offset, p.byte_offset + p.byte_size, 4)}
        for parameter in native.parameters:
            if is_private_material_parameter(parameter.name) or not parameter.program_count:
                continue
            supplied = fields.get(resolve_material_name(parameter.name, fields))
            if supplied is not None and (supplied.byte_offset, supplied.byte_size) != (parameter.byte_offset, parameter.byte_size):
                raise ValueError(f"MDF parameter {parameter.name!r} disagrees with the native MMTR declaration.")
            if supplied is None:
                occupied = set(range(parameter.byte_offset, parameter.byte_offset + parameter.byte_size, 4))
                if words & occupied:
                    raise ValueError(f"MDF parameters overlap the native MMTR declaration {parameter.name!r}.")
                parameters.append(parameter)
                words.update(occupied)
        if named_sizes and set(named_sizes) != {self.layout.parameter_size}:
            raise ValueError(
                f"The selected MDF allocates {self.layout.parameter_size} bytes, but the native material buffer "
                f"declares {', '.join(map(str, sorted(named_sizes)))}. Check the MMTR resource revision."
            )
        return replace(native, parameters=tuple(sorted(parameters, key=lambda p: p.byte_offset)),
                       user_buffer_sizes=(self.layout.parameter_size,), descriptor_layouts=(self.layout,), shader_lod=shader_lod)


def native_material_buffer_sizes(document):
    return tuple(sorted({int(buffer.byte_size) for target in document.sdf.targets
                         for program in program_tables(target) for buffer in program.constant_buffers
                         if buffer.name in document.profile.user_material_buffer_names}))


def bind_material_instance(document, material):
    return bind_material_context(document, MaterialInstanceContext.from_material(material), material.header.mmtr_path)


def bind_material_context(document, context: MaterialInstanceContext, mmtr_path: str, *, family=None,
                          shader_lod: int | None = None):
    expected = mmtr_resource_path_from_file(document.source_label).casefold()
    actual = mmtr_resource_path_from_file(
        family.path_for_lod(shader_lod) if family is not None and shader_lod is not None else mmtr_path
    ).casefold()
    if not expected or actual != expected:
        raise ValueError("The selected MDF material does not reference this MMTR.")
    layout = context.layout
    if layout.shader_lod_redirects and (family is None or shader_lod is None):
        raise ValueError("A shader-LOD material must be edited with its complete MMTRS family.")
    if family is not None and len(layout.shader_lod_redirects) != len(family.material_paths):
        raise ValueError("MDF shader-LOD redirects do not match the MMTRS member count.")
    native = document.material_interface
    named_sizes = native_material_buffer_sizes(document)
    profile = document.profile.shader_graph_profile
    instance_resources = {*profile.material_instance_resource_names, profile.instanced_material_buffer} - {""}
    if not named_sizes and not native.parameters and not any(r.name in instance_resources for r in native.resources):
        # E.g. renderer-only post effects, whose programs read no MDF record.
        interface = replace(native, descriptor_layouts=(layout,), shader_lod=shader_lod)
    else:
        interface = context.bind_interface(native, named_sizes=named_sizes, shader_lod=shader_lod)
        storage = native.parameter_storage
        if any(r.name in profile.material_instance_resource_names for r in native.resources):
            storage = "mixed-material-constant-buffer" if named_sizes else "bindless-material-constant-buffer"
        elif not native.parameters:
            storage = "bindless-material-constant-buffer"
        interface = replace(interface, parameter_storage=storage)
    return replace(document, material_interface=interface,
                   _material_contract_cache={}, _material_program_routes_cache={},
                   _material_texture_contracts_cache={}, _material_input_usage_cache={})
