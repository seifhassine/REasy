from dataclasses import dataclass

from .spirv import SpirvError


@dataclass(frozen=True)
class VulkanBinding:
    name: str
    register_class: str
    slot: int
    space: int
    descriptor_set: int
    binding: int

    def __post_init__(self):
        if (not isinstance(self.name, str) or not 1 <= len(self.name) <= 256
                or self.register_class not in {'b', 's', 't', 'u'}
                or any(type(v) is not int or not 0 <= v <= 0xffffffff
                       for v in (self.slot, self.space, self.descriptor_set, self.binding))):
            raise SpirvError('Invalid canonical Vulkan descriptor binding.')


@dataclass(frozen=True)
class ShaderVulkanExecution:
    version: int
    bindings: tuple[VulkanBinding, ...]
    early_fragment_tests: bool = False
    depth_mode: str = 'none'

    def __post_init__(self):
        object.__setattr__(self, 'bindings', tuple(VulkanBinding(**b) if isinstance(b, dict) else b for b in self.bindings))
        if (self.version not in {0x10000, 0x10300} or type(self.early_fragment_tests) is not bool
                or self.depth_mode not in {'none', 'any', 'greater', 'less', 'unchanged'}
                or len(self.bindings) > 2048 or any(not isinstance(b, VulkanBinding) for b in self.bindings)):
            raise SpirvError('Unsupported canonical Vulkan execution declaration.')
        for keys in ([b.name for b in self.bindings], [(b.descriptor_set, b.binding) for b in self.bindings],
                     [(b.register_class, b.slot, b.space) for b in self.bindings]):
            if len(set(keys)) != len(keys):
                raise SpirvError('Canonical Vulkan descriptor identities are duplicated.')


_TESSELLATION_SPACING = {1: 'integer', 2: 'fractional_even', 3: 'fractional_odd'}
_TESSELLATION_DOMAIN = {22: 'tri', 24: 'quad', 25: 'isoline'}
_TESSELLATION_WINDING = {4: 'triangle_cw', 5: 'triangle_ccw'}
_TESSELLATION_MODES = frozenset(_TESSELLATION_SPACING) | frozenset(_TESSELLATION_WINDING) \
    | frozenset(_TESSELLATION_DOMAIN) | {11, 26}  # PointMode, OutputVertices


def _sole(table, modes, message):
    declared = [key for key in table if key in modes]
    if len(declared) != 1:
        raise SpirvError(message)
    return table[declared[0]]


def _patch_control_points(native):
    lengths = set()
    for item in native.inputs:
        if item.built_in is not None:
            continue
        _head, separator, tail = item.value_type.partition('[')
        if not separator:
            continue
        lengths.add(int(tail.split(']', 1)[0]))
    if len(lengths) != 1:
        raise SpirvError('Vulkan tessellation needs one declared patch size on its input arrays.')
    return lengths.pop()


def _tessellation_execution(native):
    from .shader_stage_execution import ShaderPrimitiveExecution
    modes = dict(native.execution_modes)
    if set(modes) - _TESSELLATION_MODES:
        raise SpirvError('This Vulkan tessellation mode needs an explicit semantic adapter.')
    domain = _sole(_TESSELLATION_DOMAIN, modes, 'Vulkan tessellation needs exactly one declared patch domain.')
    point_mode = 11 in modes
    if point_mode and domain == 'tri':
        raise SpirvError('Vulkan point mode has no patch domain to subdivide.')
    input_control_points = _patch_control_points(native)
    if native.stage == 'domain':
        execution = ShaderPrimitiveExecution(domain=domain, input_control_points=input_control_points)
    else:
        vertices = modes.get(26)
        if vertices is None or len(vertices) != 1:
            raise SpirvError('Vulkan hull execution needs one declared output control-point count.')
        execution = ShaderPrimitiveExecution(domain=domain, input_control_points=input_control_points,
            output_control_points=vertices[0],
            partitioning=_sole(_TESSELLATION_SPACING, modes,
                'Vulkan hull execution needs exactly one declared partitioning policy.'),
            tessellation_primitive=_tessellation_output_primitive(domain, point_mode, modes))
    execution.validate_stage(native.stage)
    return execution


def _tessellation_output_primitive(domain, point_mode, modes):
    if point_mode:
        return 'point'
    if domain == 'isoline':
        return 'line'
    return _sole(_TESSELLATION_WINDING, modes,
        'Vulkan triangle tessellation needs exactly one declared vertex order.')


def vulkan_primitive_execution(native, streams=(0,)):
    if native.stage in {'hull', 'domain'}:
        return _tessellation_execution(native)
    if native.stage != 'geometry':
        return None
    from .shader_stage_execution import ShaderPrimitiveExecution
    modes = dict(native.execution_modes)
    inputs = {19:'point',20:'line',21:'lineadj',22:'triangle',23:'triangleadj'}
    outputs = {27:'pointlist',28:'linestrip',29:'trianglestrip'}
    declared_input, declared_output = set(modes) & inputs.keys(), set(modes) & outputs.keys()
    if len(declared_input) != 1 or len(declared_output) != 1 or len(modes.get(26, ())) != 1 or len(modes.get(0, (1,))) != 1:
        raise SpirvError('Vulkan geometry needs explicit input/output primitive and vertex-count declarations.')
    execution = ShaderPrimitiveExecution(input_primitive=inputs[next(iter(declared_input))],
        output_topology=outputs[next(iter(declared_output))], max_output_vertices=modes[26][0],
        geometry_instances=modes.get(0,(1,))[0], output_streams=streams)
    execution.validate_stage('geometry')
    return execution


def vulkan_execution(native, contract):
    if native.stage not in {'vertex', 'pixel', 'compute', 'geometry', 'hull', 'domain'}:
        raise SpirvError('This Vulkan stage needs its primitive execution declaration.')
    modes = dict(native.execution_modes)
    allowed = {'vertex': set(), 'pixel': {7, 9, 12, 14, 15, 16}, 'compute': {17},
               'geometry': {0, 19, 20, 21, 22, 23, 26, 27, 28, 29},
               'hull': _TESSELLATION_MODES, 'domain': _TESSELLATION_MODES - {26}}[native.stage]
    literals = {0,26} if native.stage=='geometry' else {26} if native.stage=='hull' else {17}
    if set(modes) - allowed or any(values for mode, values in modes.items() if mode not in literals):
        raise SpirvError('This Vulkan execution mode needs an explicit semantic adapter.')
    if native.stage == 'pixel' and 7 not in modes or native.stage == 'compute' and 17 not in modes:
        raise SpirvError('Vulkan execution lacks its required origin/thread declaration.')
    if native.stage in {'geometry', 'hull', 'domain'}:
        streams=tuple(sorted({field.stream for field in contract.outputs})) or (0,)
        declared = vulkan_primitive_execution(native,streams)
        if contract.primitive_execution != declared:
            raise SpirvError(f'Vulkan {native.stage} differs from its declared primitive execution.')
    restrictions = set(modes) & {14, 15, 16}
    if len(restrictions) > 1 or restrictions and 12 not in modes:
        raise SpirvError('Vulkan depth restrictions have no consistent DepthReplacing declaration.')
    depth = next(({14: 'greater', 15: 'less', 16: 'unchanged'}[m] for m in restrictions), 'any' if 12 in modes else 'none')
    bindings = []
    for descriptor in native.descriptors:
        resource = contract.resource(descriptor.name)
        if resource is None or resource.register_class != descriptor.register_class:
            raise SpirvError('A Vulkan descriptor has no matching logical renderer resource.')
        bindings.append(VulkanBinding(resource.name, resource.register_class, resource.bind_point,
                                      resource.register_space, descriptor.descriptor_set, descriptor.binding))
    return ShaderVulkanExecution(native.version, tuple(bindings), 9 in modes, depth)
