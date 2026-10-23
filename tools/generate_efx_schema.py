"""Export versioned REE-Lib EFX definitions for REasy's Python codec.

Developer tool only: requires pythonnet and a built REE-Lib assembly. The editor
loads the generated JSON and has no .NET/runtime dependency.
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

REVISION = "ceb0d2fbe9d76f29c552e4c56f4fd8cd87a165ef"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("assembly", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "resources/data/efx")
    args = parser.parse_args()
    from pythonnet import load
    load("coreclr")
    import clr
    assembly = clr.AddReference(str(args.assembly.resolve()))
    from System.Reflection import BindingFlags
    from System.Text.Json import JsonSerializer
    from ReeLib.Efx import EfxTools, EfxVersion
    from ReeLib import EfxStructCache

    def argument(value):
        value = value.Value
        if value is None:
            return None
        if isinstance(value, (str, bool, int, float)):
            return value
        if hasattr(value, "FullName"):
            return str(value.FullName)
        if hasattr(value, "GetEnumerator"):
            return [argument(item) for item in value]
        return str(value)

    def flattened(attribute):
        values = [argument(item) for item in attribute.ConstructorArguments]
        return values[0] if len(values) == 1 and isinstance(values[0], list) else values

    args.output.mkdir(parents=True, exist_ok=True)
    for version, name in ((2621987, "MHRise"), (2818730, "MHRiseSB")):
        with tempfile.TemporaryDirectory(prefix="reasy-efx-schema-") as temporary:
            source = Path(temporary) / "schema.json"
            cache = EfxTools.GenerateEFXStructsJson(getattr(EfxVersion, name), None)
            options = assembly.GetType("ReeLib.Efx.EfxTools").GetMethod("GetJsonOptions", BindingFlags.Static | BindingFlags.NonPublic).Invoke(None, None)
            source.write_text(str(JsonSerializer.Serialize[EfxStructCache](cache, options)), encoding="utf-8")
            schema = json.loads(source.read_text(encoding="utf-8"))
        for group in ("AttributeTypes", "Structs"):
            for body in schema[group].values():
                target = assembly.GetType(body["Classname"])
                if target is None:
                    raise ValueError(f"Missing REE-Lib class {body['Classname']}")
                body["BoneRelation"] = any(str(interface.Name) == "IBoneRelationAttribute" for interface in target.GetInterfaces())
                for field in body.get("Fields", []):
                    info = target.GetField(field["Name"], BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
                    if info is None:  # Generated length fields precede their arrays.
                        continue
                    for attribute in info.GetCustomAttributesData():
                        kind = str(attribute.AttributeType.Name)
                        values = flattened(attribute)
                        named = {str(item.MemberName): argument(item.TypedValue) for item in attribute.NamedArguments}
                        if kind in ("RszInlineWStringAttribute", "RszInlineStringAttribute", "RszInlineAsciiStringAttribute"):
                            field["StringEncoding"] = "utf-16le" if kind == "RszInlineWStringAttribute" else "utf-8"
                            field["StringLength"] = values
                            if field.get("IsArray") and not values:
                                field["StringLength"] = [-1]
                            field["StringLengthIsBytes"] = named.get("ByteSize", False)
                        elif kind in ("RszListAttribute", "RszFixedSizeArrayAttribute"):
                            field["ArrayLength"] = values
                        elif kind == "RszConditionalAttribute":
                            field["Conditional"] = values
                        elif kind == "RszSwitchAttribute":
                            field["Switch"] = values
                        elif kind == "RszByteSizeFieldAttribute":
                            field["ByteCountOf"] = values[0]
                        elif kind == "RszArraySizeFieldAttribute":
                            field["CountOf"] = values[0]
                            field["CountMultiplier"] = 2 if named.get("DoubleSize") else 1
        # Rise's effect shader is two CRC words, an unknown word and a byte-sized
        # UTF-16 path. REE-Lib currently selects its later DD2 layout for this ID.
        schema["AttributeTypes"]["EffectShader"]["Fields"] = [
            {"Name": "shaderCRCHash0", "FieldType": "U32"},
            {"Name": "shaderCRCHash1", "FieldType": "U32"},
            {"Name": "unknown", "FieldType": "U32"},
            {"Name": "shaderPath", "FieldType": "String", "StringEncoding": "utf-16le", "StringLength": [], "StringLengthIsBytes": True},
        ]
        if version == 2818730:
            # Explicit Sunbreak layouts verified against native resources. Do
            # not compare unrelated engine branches by their numeric version.
            no_draw = schema["AttributeTypes"]["TypeNoDraw"]["Fields"]
            no_draw[4:] = [
                {"Name": f"{group}{axis}{suffix}", "FieldType": "F32"}
                for group in ("Rotation", "Size") for axis in "XYZ" for suffix in ("", "Random")
            ]
            clip = schema["AttributeTypes"]["TypeMeshClip"]["Fields"]
            clip.insert(1, {"Name": "clipHeader", "FieldType": "U32"})
            ribbon = schema["AttributeTypes"]["TypeRibbonLength"]["Fields"]
            ribbon[:] = [field for field in ribbon if field["Name"] != "Flags"]
            chain = schema["AttributeTypes"]["TypeRibbonChain"]["Fields"]
            chain[:] = [field for field in chain if field["Name"] != "unkn2_32"]
            index = next(i for i, field in enumerate(chain) if field["Name"] == "rert_unkn0") + 1
            chain[index:index] = [{"Name": "sb_unkn1", "FieldType": "F32"}, {"Name": "sb_unkn2", "FieldType": "F32"}]
            polygon = schema["AttributeTypes"]["TypeGpuPolygon"]["Fields"]
            index = next(i for i, field in enumerate(polygon) if field["Name"] == "BlendFlags") + 1
            polygon[index:index] = [{"Name": "unkn1", "FieldType": "U32"}, {"Name": "unkn2", "FieldType": "U32"}]
            emitter = schema["AttributeTypes"]["MeshEmitter"]["Fields"]
            newer = {"EmissionVertexColorThreshold", "EmissionVertexColorChannel", "EmissionMaskMapColorChannel", "ParticleNum", "LodIndex", "TriangleFilter"}
            emitter[:] = [field for field in emitter if field["Name"] not in newer]
            for field in schema["AttributeTypes"]["VanishArea3D"]["Fields"]:
                if field["Name"] == "JointName":
                    field["StringLengthIsBytes"] = True
            for name in ("PlayEfx", "RenderTarget"):
                for field in schema["AttributeTypes"][name]["Fields"]:
                    if field["FieldType"] == "String":
                        field["StringLengthIsBytes"] = True
            path_translate = schema["AttributeTypes"]["PtPathTranslate"]["Fields"]
            path_translate[:] = [field for field in path_translate if field["Name"] != "unkn3"]
            schema["Structs"]["ReeLib.Efx.Structs.Pt.PtBehaviorVariableDataWString"]["Fields"][0]["StringLengthIsBytes"] = True
            fields = schema["AttributeTypes"]["Velocity3DExpression"]["Fields"]
            fields[:] = [field for field in fields if field["Name"] not in ("unkn13", "unkn14")]
            index = next(i for i, field in enumerate(fields) if field["Name"] == "expressions")
            fields[index:index] = [{"Name": f"unkn{number}", "FieldType": "S32"} for number in (13, 14, 15)]
            follow = schema["AttributeTypes"]["TypeRibbonFollowExpression"]["Fields"]
            index = next(i for i, field in enumerate(follow) if field["Name"] == "expressions")
            follow[index:index] = [{"Name": "unkn8", "FieldType": "S32"}, {"Name": "unkn9", "FieldType": "S32"}]
        schema["Source"] = {"url": "https://github.com/kagenocookie/RE-Engine-Lib", "revision": REVISION, "version": version}
        destination = args.output / f"{version}.json"
        destination.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(destination)


if __name__ == "__main__":
    main()
