from .shader_graph import ShaderGraphError,ShaderGraphNodeExtension,ShaderPin,register_shader_graph_node_extension
lazy from .shader_stage_execution import patch_field_members,patch_struct
from PySide6.QtCore import QT_TRANSLATE_NOOP


def domain_coordinate_type(contract):
    if contract is None or contract.stage!='domain' or contract.primitive_execution is None:
        raise ShaderGraphError('Domain coordinates require a declared tessellation domain.')
    contract.primitive_execution.validate_stage('domain')
    return 'float3' if contract.primitive_execution.domain=='tri' else 'float2'


def patch_input_properties(contract,field):
    member = next((member for candidate,_,_,member in patch_field_members(contract.patch_constants) if candidate.key==field.key),None)
    if member is None:
        raise ShaderGraphError('The patch input has no declared semantic.')
    return dict(semantic=field.semantic_name,semantic_index=field.semantic_index,member=member)


def patch_input_pins(node,contract):
    if contract is None or contract.stage!='domain':
        raise ShaderGraphError('Patch constants are per-patch domain inputs.')
    key = str(node.properties.get('semantic','')).casefold(),node.properties.get('semantic_index',0)
    field = next((field for field in contract.patch_constants if field.key==key),None)
    if field is None or node.properties!=patch_input_properties(contract,field):
        raise ShaderGraphError('The patch input differs from its declared semantic interface.')
    return (ShaderPin('value',field.display_name,field.value_type,'output'),)


def domain_declarations(contract):
    domain = domain_coordinate_type(contract)
    execution = contract.primitive_execution
    return (patch_struct(contract.patch_constants),
            ['REasyPatchConstants patch',f'{domain} reasy_domain_coordinates : SV_DomainLocation',
             f'const OutputPatch<REasyStageInput,{execution.input_control_points}> input'],
            [f'[domain("{execution.domain}")]'])


def domain_templates(contract):
    if contract is None or contract.stage!='domain':
        return ()
    return (('domain_coordinates',QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Domain Coordinates'),{}),)+tuple(
        ('patch_input',field.display_name,patch_input_properties(contract,field)) for field in contract.patch_constants)


register_shader_graph_node_extension(ShaderGraphNodeExtension(kind='patch_input',title='Patch Constant',
    pins_provider=patch_input_pins,expression=lambda node,_pin,_incoming:'patch.'+node.properties['member'],
    description=QT_TRANSLATE_NOOP("ShaderGraphNodes", 'A per-patch value produced by the hull stage, not a per-vertex attribute.')))
register_shader_graph_node_extension(ShaderGraphNodeExtension(kind='domain_coordinates',title='Domain Coordinates',
    pins_provider=lambda node,contract:(ShaderPin('value','Coordinates',domain_coordinate_type(contract),'output'),),
    expression=lambda _node,_pin,_incoming:'reasy_domain_coordinates',
    description=QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Tessellator coordinates: three barycentric components for a triangle, two for a quad or isoline.')))
