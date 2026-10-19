from dataclasses import replace

from .material_function import MaterialFunctionPin, material_function_node_properties


def combine_hull_behaviors(control, patch, contract, name):
    from .vanilla_function import _Builder, _Value, VanillaFunctionError
    builder = _Builder(contract)
    outputs, executions = [], []
    for phase, behavior, fields in (('control',control,contract.outputs), ('patch',patch,contract.patch_constants)):
        if behavior.clip_output or behavior.patch_output_count or behavior.patch_execution_output:
            raise VanillaFunctionError('A hull phase must have a single invocation and no pixel clipping.')
        if len(behavior.function.outputs) != len(fields) + bool(behavior.execution_output):
            raise VanillaFunctionError('Hull phase results disagree with their semantic signature.')
        instance = builder._node('material_function',properties=material_function_node_properties(behavior.function))
        inputs = {pin.pin_id:pin for pin in behavior.function.inputs}
        for pin_id,key in behavior.dependencies:
            pin = inputs[pin_id]
            argument = builder.input(key,pin.name,pin.value_type)
            builder.graph.connect(argument.node,argument.pin,instance.node_id,pin_id)
        for pin,field in zip(behavior.function.outputs,fields):
            if pin.name != field.display_name or pin.value_type != field.value_type:
                raise VanillaFunctionError('A hull phase changed its output meaning or domain.')
            value = _Value(instance.node_id,pin.pin_id,pin.value_type)
            outputs.append((field,[builder.scalar(value,lane) for lane in range(field.mask.bit_count())]))
        if behavior.execution_output:
            executions.append((MaterialFunctionPin('out_'+phase+'_execution',phase.title()+' Execution','execution',False),
                               _Value(instance.node_id,behavior.execution_output,'execution')))
    result = builder.finish(name,outputs,extra_outputs=executions)
    execution_pins = {pin.pin_id for pin,_ in executions}
    return replace(result,patch_output_count=len(contract.patch_constants),
        execution_output='out_control_execution' if 'out_control_execution' in execution_pins else '',
        patch_execution_output='out_patch_execution' if 'out_patch_execution' in execution_pins else '')
