"""Rise-specific layouts absent from the imported REE-Lib schema.

Layout reference: https://github.com/NSACloud/MHR-EFX-Template/blob/master/RISE_EFX.bt
Names marked unknown retain the template's unresolved semantics.
"""


def extend_schema(schema, version):
    if version != 2818730:
        return
    definitions = {
        # Nine native instances share this 124-byte layout; field meanings are unresolved.
        "Transform2DModifier": (7, [{"Name": "unknown0", "FieldType": "U32"}] + [
            {"Name": f"unknown{index}", "FieldType": "F32"} for index in range(1, 31)
        ]),
        "EmitMask": (213, [{"Name": "mask", "FieldType": "U32"}]),
        "RgbCommon": (208, [{"Name": "Color", "FieldType": "Color"}] + [
            {"Name": f"unknown{index}", "FieldType": "Color" if index == 3 else "F32" if index < 7 else "U32"}
            for index in range(22)
        ]),
    }
    for name, (type_id, fields) in definitions.items():
        schema["AttributeTypes"][name] = {
            "TypeID": type_id, "Name": name, "Classname": f"REasy.Efx.Sunbreak.{name}",
            "Fields": fields, "BoneRelation": False,
        }
    lightning = schema["AttributeTypes"]["TypeGpuLightning3D"]
    lightning["BoneRelation"] = True
    lightning["BoneFields"] = ["str1", "str2", "str3"]
    for descriptor in lightning["Fields"]:
        if descriptor["Name"] in ("str1Len", "str2Len", "str3Len"):
            descriptor.update(Flag="StringLength", FlagTarget=descriptor["Name"][:-3])
    materials = [(schema['Structs']['ReeLib.Efx.Structs.Common.EfxMaterialStructV1'], 2),
                 (schema['Structs']['ReeLib.Efx.Structs.Common.EfxMaterialStructV2'], 1),
                 (schema['AttributeTypes']['TypeMesh'], 1)]
    for definition, divisor in materials:
        for descriptor in definition['Fields']:
            if descriptor['Name'] in ('texBlockSize', 'texPathBlockLength'):
                descriptor.update(ByteCountOf='texPaths', ByteCountDivisor=divisor)
    for descriptor in schema['AttributeTypes']['TypeMeshClip']['Fields']:
        if descriptor['Name'] == 'clipHeader':
            descriptor.update(CountOf='clipData', CountMultiplier=2, CountLowMask=1)
    # Rise has one flags word and 29 assignment slots (RISE_EFX.bt), not DD2's two words.
    expression = schema['AttributeTypes']['TypeLightning3DExpression']['Fields']
    expression[0].update(FixedLength=1, FlagTarget='29')
    expression.insert(-1, {'Name': 'unkn29', 'FieldType': 'S32', 'Classname': 'ReeLib.Efx.Structs.Common.ExpressionAssignType'})
    for descriptor in schema['AttributeTypes']['TypeMeshExpression']['Fields']:
        if descriptor['Name'] == 'indicesCount':
            descriptor.update(CountOf='materialIndices')
    for descriptor in schema['AttributeTypes']['PtPathTranslate']['Fields']:
        if descriptor['Name'] == 'dataSize':
            descriptor.update(CountOf='substruct2')
