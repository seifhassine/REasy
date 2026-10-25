from dataclasses import dataclass
from enum import Enum

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP

lazy from file_handlers.material_names import resolve_material_name
lazy from file_handlers.sdf.spirv_ir import SpirvSemanticIr
lazy from .profiles.re_engine_spirv_authoring import spirv_uniform_reads, spirv_resource_reads
lazy from .worker_pool import parallel_map


class InputUsage(Enum):
    USED = QT_TRANSLATE_NOOP("MaterialInputUsage", "Consumed by shaders")
    DESCRIPTOR = QT_TRANSLATE_NOOP("MaterialInputUsage", "Descriptor used; no image access identified")
    UNUSED = QT_TRANSLATE_NOOP("MaterialInputUsage", "Not consumed by shaders")
    UNKNOWN = QT_TRANSLATE_NOOP("MaterialInputUsage", "Shader usage not established")

    @property
    def label(self):
        return QCoreApplication.translate("MaterialInputUsage", self.value)

    @property
    def explanation(self):
        return QCoreApplication.translate("MaterialInputUsage", {
            self.USED: QT_TRANSLATE_NOOP("MaterialInputUsage", "Referenced by at least one loaded compiled shader. It may be unused in other passes or variants."),
            self.DESCRIPTOR: QT_TRANSLATE_NOOP("MaterialInputUsage", "Compiled shaders read this texture's descriptor, but no image-access type was identified. This includes feedback/reporting uses. The slot is an editable material input, not unused data."),
            self.UNUSED: QT_TRANSLATE_NOOP("MaterialInputUsage", "No loaded compiled material shader consumes this input. It remains editable; connecting it in a modified graph can introduce a consumer."),
            self.UNKNOWN: QT_TRANSLATE_NOOP("MaterialInputUsage", "Shader usage could not be fully established. This does not mean the input is unused."),
        }[self])


@dataclass(frozen=True, slots=True)
class MaterialInputUsage:
    parameter_ranges: tuple[tuple[int, int], ...] = ()
    textures: frozenset[str] = frozenset()
    parameters_complete: bool = False
    textures_complete: bool = False
    descriptor_textures: frozenset[str] = frozenset()
    declared_textures: frozenset[str] = frozenset()

    def parameter(self, offset, size):
        if offset is None or size <= 0:
            return InputUsage.UNKNOWN
        if any(start < offset + size and offset < end for start, end in self.parameter_ranges):
            return InputUsage.USED
        return InputUsage.UNUSED if self.parameters_complete else InputUsage.UNKNOWN

    def texture(self, name):
        key = resolve_material_name(str(name), self.textures | self.descriptor_textures | self.declared_textures)
        if key in self.textures:
            return InputUsage.USED
        if key in self.descriptor_textures:
            return InputUsage.DESCRIPTOR if self.textures_complete else InputUsage.UNKNOWN
        return InputUsage.UNUSED if self.textures_complete else InputUsage.UNKNOWN


@dataclass(frozen=True, slots=True)
class NativeInputUsage:
    inputs: MaterialInputUsage
    indirect_textures: tuple[tuple[int, str], ...] = ()
    bindless: bool = False
    record_words: frozenset[int] = frozenset()


def _record_reads(job):
    """The material record words and indirect textures a bindless DXIL shader reads; None when it cannot be read."""
    from file_handlers.sdf.dxc import disassemble_dxil
    from .profiles.re_engine_bindless_material import material_input_accesses
    payload, contract, prefix = job
    try:
        accesses, texture_accesses = material_input_accesses(disassemble_dxil(payload).splitlines(), contract)
    except ValueError:
        return None
    return ({word.byte_offset - prefix for access in accesses for word in access.words if word.byte_offset >= prefix},
            {(access.byte_offset - prefix, access.dimension) for access in texture_accesses
             if access.byte_offset >= prefix})


def native_input_usage(document):
    cache = getattr(document, "_native_input_usage_cache", None)
    if cache is None:
        return NativeInputUsage(MaterialInputUsage())
    if "usage" in cache:
        return cache["usage"]

    from file_handlers.sdf.dxc import enrich_dxil_shader

    profile = document.profile.shader_graph_profile
    user_names = {name.casefold() for name in document.profile.user_material_buffer_names}
    arena_names = set(profile.material_instance_resource_names)
    ranges, textures, indirect, seen, record_words = set(), set(), set(), set(), set()
    record_jobs = []
    parameters_complete = textures_complete = True
    bindless = False
    for target in document.sdf.targets:
        for program in target.programs:
            for shader in program.shaders:
                if not shader.present:
                    continue
                key = (shader.binary_format, shader.payload, getattr(getattr(shader, "spirv", None), "entry_point", None))
                if key in seen:
                    continue
                seen.add(key)
                if shader.binary_format == "spirv-module":
                    try:
                        contract = profile.shader_contract(document, program, shader.role)
                        ir = None
                        for buffer in contract.constant_buffers:
                            if buffer.name.casefold() not in user_names:
                                continue
                            if ir is None:
                                ir = SpirvSemanticIr.from_bytes(shader.spirv.module)
                            words = spirv_uniform_reads(ir, buffer.name)
                            if words is None:
                                parameters_complete = False
                            else:
                                ranges.update((offset, offset + 4) for offset in words)
                        native_textures = {r.name for r in contract.resources if r.input_type == "texture"}
                        if native_textures:
                            if ir is None:
                                ir = SpirvSemanticIr.from_bytes(shader.spirv.module)
                            reads = spirv_resource_reads(ir)
                            if reads is None:
                                textures_complete = False
                            else:
                                textures.update(native_textures & reads)
                        if any(r.name in arena_names for r in contract.resources):
                            bindless = True
                            parameters_complete = textures_complete = False
                    except ValueError:
                        parameters_complete = textures_complete = False
                    continue
                if shader.binary_format != "dxbc-container" or shader.reflection is None:
                    parameters_complete = textures_complete = False
                    continue
                try:
                    dxbc = shader.reflection
                    if dxbc.reflection is None:
                        dxbc = enrich_dxil_shader(dxbc, shader.payload)
                    reflection = dxbc.reflection
                    if reflection is None:
                        parameters_complete = textures_complete = False
                        continue
                    for buffer in reflection.constant_buffers:
                        if buffer.name.casefold() in user_names:
                            ranges.update((v.start_offset, v.start_offset + v.size)
                                          for v in buffer.variables if v.flags & 2)  # D3D_SVF_USED
                    textures.update(r.name for r in reflection.resources if r.input_type == "texture")
                    if not any(r.name in arena_names for r in reflection.resources):
                        continue
                    bindless = True
                    if dxbc.model_major < 6:
                        parameters_complete = textures_complete = False
                        continue
                    record_jobs.append((shader.payload, profile.shader_contract(document, program, shader.role),
                                  profile.material_record_prefix_size))
                except ValueError:
                    parameters_complete = textures_complete = False
    for found in parallel_map(_record_reads, record_jobs) or map(_record_reads, record_jobs):
        if found is None:
            parameters_complete = textures_complete = False
            continue
        words, texture_reads = found
        record_words.update(words)
        ranges.update((offset, offset + 4) for offset in words)
        indirect.update(texture_reads)
    inputs = MaterialInputUsage(tuple(sorted(ranges)), frozenset(textures),
                                parameters_complete and bool(seen), textures_complete and bool(seen))
    result = NativeInputUsage(inputs, tuple(sorted(indirect)), bindless, frozenset(record_words))
    cache["usage"] = result
    return result


def material_input_usage(document, *, descriptor_layout=None):
    if document is None or not hasattr(document, "_material_input_usage_cache"):
        return MaterialInputUsage()
    cache = document._material_input_usage_cache
    if descriptor_layout in cache:
        return cache[descriptor_layout]
    family = document.material_family
    if family is not None:
        members = tuple(material_input_usage(member, descriptor_layout=descriptor_layout) for member in family.members)
        result = MaterialInputUsage(
            tuple(sorted({*member.parameter_ranges for member in members})),
            frozenset(name for member in members for name in member.textures),
            bool(members) and all(member.parameters_complete for member in members),
            bool(members) and all(member.textures_complete for member in members),
            frozenset(name for member in members for name in member.descriptor_textures),
            frozenset(name for member in members for name in member.declared_textures),
        )
    else:
        native = native_input_usage(document)
        inputs = native.inputs
        names = set(inputs.textures)
        descriptors = set()
        declared = set()
        complete = inputs.textures_complete
        if native.bindless:
            interface = document.material_interface
            layouts = (descriptor_layout,) if descriptor_layout is not None else interface.descriptor_layouts
            declared.update(name for layout in layouts for name in layout.textures)
            if len(layouts) != 1 or (layouts[0].shader_lod_redirects and interface.shader_lod is None):
                complete = False
            else:
                reads = {offset for offset, _dimension in native.indirect_textures}
                for (kind, name), offset in layouts[0].word_offsets(interface.shader_lod).items():
                    if kind == "texture":
                        if offset in reads:
                            names.add(name)
                        if offset in native.record_words:
                            descriptors.add(name)
        result = MaterialInputUsage(inputs.parameter_ranges, frozenset(names),
                                    inputs.parameters_complete, complete, frozenset(descriptors), frozenset(declared))
    cache[descriptor_layout] = result
    return result
