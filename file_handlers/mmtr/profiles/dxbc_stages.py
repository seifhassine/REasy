from dataclasses import replace

from file_handlers.sdf.shader_graph import ShaderGraphError, mask_components


_DECLARATIONS = {
    "dcl_input_control_point_count": ("input_control_points", int),
    "dcl_output_control_point_count": ("output_control_points", int),
    "dcl_tessellator_domain": ("domain", lambda s: s.removeprefix("domain_")),
    "dcl_tessellator_partitioning": ("partitioning", lambda s: s.removeprefix("partitioning_")),
    "dcl_tessellator_output_primitive": ("tessellation_primitive", lambda s: s.removeprefix("output_")),
    "dcl_hs_max_tessfactor": ("max_tessellation_factor", lambda s: float(s.removeprefix("l(").removesuffix(")"))),
    "dcl_inputprimitive": ("input_primitive", str),
    "dcl_outputtopology": ("output_topology", str),
    "dcl_maxout": ("max_output_vertices", int),
    "dcl_gsinstancecount": ("geometry_instances", int),
    "dcl_gsinstances": ("geometry_instances", int),
}


def stage_declaration(line):
    name, _, operand = line.partition(" ")
    field = _DECLARATIONS.get(name.casefold())
    return (field[0], field[1](operand.strip())) if field else None


def pixel_signature_interpolation(fields, declarations):
    modes = {}
    for field in fields:
        lanes = set(mask_components(field.mask))
        values = {mode for register, mask, mode in declarations
                  if register == field.register and set(mask) & lanes}
        if len(values) > 1:
            raise ShaderGraphError("Paired pixel field has conflicting interpolation modes")
        if values:
            modes[field.key] = next(iter(values))
    return _signature_packing_modes(fields, modes, unused_only=True)


def _signature_packing_modes(fields, modes, *, unused_only):
    modes = dict(modes)
    groups = {}
    for field in fields:
        group = groups.setdefault((field.stream, field.register, field.component_type), [])
        group.append(field)
    occupied = {}
    for key, group in groups.items():
        occupied[key] = 0
        for field in group:
            occupied[key] |= field.mask
    assigned = {}
    for key, group in groups.items():
        live = {modes[f.key] for f in group if f.key in modes}
        if len(live) == 1:
            assigned[key] = next(iter(live))
    choices = ("linear", "constant", "linear noperspective", "linear centroid",
               "linear noperspective centroid", "linear sample", "linear noperspective sample")
    for key, group in groups.items():
        missing = [f for f in group if f.key not in modes and (not unused_only or not f.read_write_mask)
                   and not f.semantic_name.casefold().startswith("sv_")]
        if not missing:
            continue
        if key not in assigned:
            forbidden = {mode for other, mode in assigned.items()
                         if other != key and (other[0], other[2]) == (key[0], key[2])
                         and occupied[key].bit_count() + occupied[other].bit_count() <= 4}
            available = next((mode for mode in choices if mode not in forbidden), None)
            if available is None:
                raise ShaderGraphError("Shader signature packing needs an explicit aggregate declaration")
            assigned[key] = available
        for field in missing:
            modes[field.key] = assigned[key]
    return modes


def signature_packing_modes(fields):
    return _signature_packing_modes(
        fields, {f.key: f.interpolation for f in fields if f.interpolation}, unused_only=False,
    )


def with_pixel_interpolation(contract, pixel_contract, declarations):
    modes = pixel_signature_interpolation(pixel_contract.inputs, declarations)
    return replace(contract, outputs=tuple(replace(f, interpolation=modes.get(f.key, "") if f.stream == 0 else "") for f in contract.outputs))
