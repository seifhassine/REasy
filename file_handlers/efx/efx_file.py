"""EFXR container and versioned attributes, adapted from REE-Lib.

See resources/data/efx/LICENSE for upstream attribution. Layout definitions are
generated from REE-Lib; parsing/writing runs entirely in Python.
"""
from __future__ import annotations

import copy
import json
import operator
import struct
from dataclasses import dataclass, field
from functools import lru_cache

from utils.app_paths import resource_path
from utils.hash_util import murmur3_hash, murmur3_hash_utf16le

EFX_MAGIC = b"efxr"
SCHEMA_ROOT = resource_path("resources/data/efx")
PRIMITIVES = {
    "F32": "f", "S32": "i", "U32": "I", "ukn_type": "I",
    "S16": "h", "U16": "H", "S8": "b", "U8": "B", "Bool": "?",
    "Color": "I", "Vec2": "2f", "Vec3": "3f", "Vec4": "4f",
    "Int2": "2i", "Int3": "3i", "Int4": "4i", "Range": "2f", "RangeI": "2i",
}


class EfxParseError(ValueError):
    pass


def signature(value):
    if isinstance(value, float):
        return value.hex()
    if isinstance(value, (tuple, list)):
        return tuple(signature(item) for item in value)
    return value


@lru_cache(maxsize=None)
def schema_for_version(version: int):
    path = SCHEMA_ROOT / f"{version}.json"
    if not path.is_file():
        raise EfxParseError(f"Unsupported EFX version {version}")
    schema = json.loads(path.read_text(encoding="utf-8"))
    from .sunbreak_schema import extend_schema
    extend_schema(schema, version)
    schema["by_id"] = {body["TypeID"]: body for body in schema["AttributeTypes"].values()}
    return schema


class Reader:
    def __init__(self, data: bytes, version: int):
        self.data = data
        self.pos = 0
        self.version = version
        self.schema = schema_for_version(version)

    def take(self, count: int) -> bytes:
        if count < 0 or count > len(self.data) - self.pos:
            raise EfxParseError(f"EFX read outside file at 0x{self.pos:X}: {count} bytes")
        start = self.pos
        self.pos += count
        return self.data[start:self.pos]

    def unpack(self, fmt: str):
        values = struct.unpack("<" + fmt, self.take(struct.calcsize("<" + fmt)))
        return values[0] if len(values) == 1 else values

    def count(self, value: int) -> int:
        if value < 0 or value > len(self.data) - self.pos:
            raise EfxParseError(f"Invalid EFX count {value} at 0x{self.pos:X}")
        return value

    def cstring(self, encoding="utf-8") -> bytes:
        start = self.pos
        step = 2 if encoding == "utf-16le" else 1
        while self.take(step) != b"\0" * step:
            pass
        return self.data[start:self.pos]


@dataclass
class EfxField:
    name: str
    kind: str
    value: object
    definition: dict = field(default_factory=dict, repr=False)
    raw: bytes = field(default=b"", repr=False)
    original: object = field(default=None, repr=False)
    offset: int = 0
    terminators: int = 1
    elements: list[EfxField] | None = field(default=None, repr=False)

    def unchanged(self):
        return signature(self.value) == self.original

    def string_data(self) -> bytes:
        encoding = self.definition.get("StringEncoding", "utf-16le")
        if self.unchanged():
            return self.raw
        return str(self.value).encode(encoding) + b"\0" * (self.terminators * (2 if encoding == "utf-16le" else 1))


@dataclass
class EfxRecord:
    classname: str
    fields: dict[str, EfxField]
    definition: dict = field(repr=False)
    version: int
    name: str | None = None
    original_name: str | None = None

    def __getitem__(self, name):
        return self.fields[name].value

    def __setitem__(self, name, value):
        self.fields[name].value = value


def evaluate(arguments, scope):
    """Evaluate REE-Lib's bounded field-size annotation grammar, not Python code."""
    def atom(value):
        return scope[value] if isinstance(value, str) else value

    if len(arguments) == 1:
        return atom(arguments[0])
    comparisons = {"==": operator.eq, "!=": operator.ne, "<": operator.lt,
                   "<=": operator.le, ">": operator.gt, ">=": operator.ge}
    if len(arguments) == 7 and arguments[3] == "?" and arguments[5] == ":":
        selected = arguments[4] if comparisons[arguments[1]](atom(arguments[0]), atom(arguments[2])) else arguments[6]
        return atom(selected)
    if len(arguments) == 3 and arguments[1] in ("/", "*", "+", "-"):
        return {"/": operator.floordiv, "*": operator.mul, "+": operator.add, "-": operator.sub}[arguments[1]](atom(arguments[0]), atom(arguments[2]))
    if len(arguments) == 2 and isinstance(arguments[1], int):
        return atom(arguments[0]) + arguments[1]
    raise EfxParseError(f"Unsupported EFX length expression {arguments!r}")


def field_class(definition, scope):
    choices = definition.get("Switch", [])
    if not choices:
        return definition["Classname"]
    comparisons = {"==": operator.eq, ">=": operator.ge, "<": operator.lt}
    for index in range(0, len(choices) - 1, 4):
        name, operation, value, classname = choices[index:index + 4]
        if comparisons[operation](scope[name], value):
            return classname
    if len(choices) % 4 == 1:
        return choices[-1]
    raise EfxParseError(f"No EFX variant for {choices!r}")


def read_string(reader, name, definition, scope):
    encoding = definition.get("StringEncoding", "utf-16le")
    unit = 2 if encoding == "utf-16le" else 1
    arguments = definition.get("StringLength", [])
    if arguments == [-1]:
        raw = reader.cstring(encoding)
    else:
        length = evaluate(arguments, scope) if arguments else reader.unpack("i")
        byte_count = length if definition.get("StringLengthIsBytes") else length * unit
        if byte_count % unit:
            raise EfxParseError(f"Unaligned {encoding} string size {byte_count}")
        raw = reader.take(byte_count)
    text = raw.decode(encoding)
    value = text.rstrip("\0")
    return EfxField(name, "String", value, definition, raw, signature(value),
                    reader.pos - len(raw), len(text) - len(value))


def read_record(reader: Reader, classname: str, context=None, definition=None):
    scope = dict(context or {})
    scope["Version"] = reader.version
    definition = definition or reader.schema["Structs"][classname]
    simple_name = classname.rsplit(".", 1)[-1]
    if simple_name in ("EFXExpressionList", "EFXMaterialExpressionList"):
        size = reader.unpack("I")
        indices = reader.unpack("i") if simple_name == "EFXMaterialExpressionList" else None
        end = reader.pos + size
        reader.count(size)
        expressions = []
        target = "ReeLib.Efx.Structs.Common." + ("EFXMaterialExpression" if indices is not None else "EFXExpressionObject")
        while reader.pos < end:
            expressions.append(read_record(reader, target))
        if reader.pos != end:
            raise EfxParseError("EFX expression solver exceeds its declared byte size")
        fields = {"expressions": EfxField("expressions", "Array", expressions,
                                         {"FieldType": "Object", "Classname": target, "IsArray": True})}
        if indices is not None:
            fields["indices"] = EfxField("indices", "Array", [reader.unpack("I") for _ in range(reader.count(indices))], {"FieldType": "U32"})
        return EfxRecord(classname, fields, definition, reader.version)
    if simple_name in ("PtBehaviorVariableDataPrefabPath", "PtBehaviorVariableEnum"):
        base = "PtBehaviorVariableDataWString" if simple_name.endswith("PrefabPath") else "PtBehaviorVariableInteger"
        definition = reader.schema["Structs"]["ReeLib.Efx.Structs.Pt." + base]
    fields = {}
    # Particle variable records include their base class's unknown/size words.
    if simple_name.startswith("PtBehaviorVariable") and simple_name not in ("PtBehaviorVariable", "PtBehaviorVariableDataBase"):
        for name in ("unkn", "size"):
            start = reader.pos
            value = reader.unpack("i")
            fields[name] = EfxField(name, "S32", value, raw=reader.data[start:reader.pos], original=signature(value), offset=start)
            scope[name] = value
    if simple_name == "EfxMaterialClipData":
        # The generated field list omits the implicit pre-RE4 material count.
        scope["mdfPropertyCount"] = None
    lengths = {f["FlagTarget"]: f["Name"] for f in definition.get("Fields", [])
               if f.get("Flag") == "StructSize" and "ByteCountOf" not in f
               and not f["Name"].lower().endswith("size")}
    for descriptor in definition.get("Fields", []):
        name = descriptor["Name"]
        start = reader.pos
        kind = descriptor["FieldType"]
        if descriptor.get("IsArray") and descriptor.get("Flag") != "EmbeddedEFX":
            arguments = descriptor.get("ArrayLength", [])
            if "FixedLength" in descriptor:
                count = descriptor["FixedLength"]
            elif arguments:
                count = evaluate(arguments, scope)
            elif name in lengths:
                count = scope[lengths[name]]
            else:
                raise EfxParseError(f"Missing array length for {classname}.{name}")
            if count is None and name == "mdfProperties":
                count = scope["clipCount"]
            elements = [read_value(reader, dict(descriptor, IsArray=False), scope) for _ in range(reader.count(count))]
            item = EfxField(name, "Array", [element.value for element in elements], descriptor,
                            offset=start, elements=elements)
        else:
            item = read_value(reader, descriptor, scope)
        fields[name] = item
        scope[name] = item.value
    return EfxRecord(classname, fields, definition, reader.version)


def read_value(reader, definition, scope):
    name, kind = definition["Name"], definition["FieldType"]
    start = reader.pos
    if kind == "String":
        return read_string(reader, name, definition, scope)
    if definition.get("Flag") == "EmbeddedEFX":
        size = scope["efxrSize"]
        value = EfxFile.from_bytes(reader.take(size), version=reader.version, parent_bones=reader.bones)
    elif kind in ("Struct", "Object"):
        value = read_record(reader, field_class(definition, scope), scope)
    elif kind in PRIMITIVES:
        value = reader.unpack(PRIMITIVES[kind])
    else:
        raise EfxParseError(f"Unsupported EFX field type {kind} ({name})")
    return EfxField(name, kind, value, definition, reader.data[start:reader.pos], signature(value), start)


def write_value(item: EfxField, scope) -> bytes:
    if item.kind == "Array":
        descriptor = dict(item.definition, IsArray=False)
        encoded = []
        for index, value in enumerate(item.value):
            element = copy.copy(item.elements[index]) if item.elements is not None and index < len(item.elements) else EfxField(item.name, descriptor["FieldType"], value, descriptor)
            element.value = value
            encoded.append(write_value(element, scope))
        return b"".join(encoded)
    if item.kind in ("Struct", "Object"):
        return write_record(item.value, scope)
    if item.definition.get("Flag") == "EmbeddedEFX":
        return item.value.write()
    if item.kind == "String":
        raw = item.string_data()
        arguments = item.definition.get("StringLength", [])
        if arguments:
            return raw
        unit = 2 if item.definition.get("StringEncoding", "utf-16le") == "utf-16le" else 1
        length = len(raw) if item.definition.get("StringLengthIsBytes") else len(raw) // unit
        return struct.pack("<i", length) + raw
    if item.unchanged() and item.raw:
        return item.raw
    value = item.value if isinstance(item.value, (tuple, list)) else (item.value,)
    if len(value) > 1 and item.raw:
        component_format = PRIMITIVES[item.kind][-1]
        size = struct.calcsize("<" + component_format)
        return b"".join(item.raw[index * size:(index + 1) * size]
                        if signature(component) == item.original[index]
                        else struct.pack("<" + component_format, component)
                        for index, component in enumerate(value))
    return struct.pack("<" + PRIMITIVES[item.kind], *value)


def write_record(record: EfxRecord, context=None) -> bytes:
    from .material import synchronize_textures
    synchronize_textures(record)
    simple_name = record.classname.rsplit(".", 1)[-1]
    if simple_name in ("EFXExpressionList", "EFXMaterialExpressionList"):
        raw = b"".join(write_record(item) for item in record["expressions"])
        if simple_name == "EFXMaterialExpressionList":
            indices = record["indices"]
            return struct.pack("<Ii", len(raw), len(indices)) + raw + struct.pack(f"<{len(indices)}I", *indices)
        return struct.pack("<I", len(raw)) + raw
    scope = dict(context or {})
    scope.update({name: value.value for name, value in record.fields.items()})
    scope["Version"] = record.version
    for name, item in record.fields.items():
        if item.definition.get("CountOf") in record.fields:
            target = record.fields[item.definition["CountOf"]].value
            count = len(target["clips"]) if isinstance(target, EfxRecord) else len(target)
            item.value = count * item.definition.get("CountMultiplier", 1) | (item.value & item.definition.get('CountLowMask', 0))
            scope[name] = item.value
        target = item.definition.get("FlagTarget")
        if target in record.fields and item.definition.get("Flag") == "StructSize" and "ByteCountOf" not in item.definition and "CountOf" not in item.definition:
            if not name.lower().endswith("size"):
                item.value = len(record.fields[target].value)
                scope[name] = item.value
        if target in record.fields and item.definition.get("Flag") == "StringLength":
            string = record.fields[target]
            unit = 2 if string.definition.get("StringEncoding", "utf-16le") == "utf-16le" else 1
            item.value = len(string.string_data()) if string.definition.get("StringLengthIsBytes") else len(string.string_data()) // unit
            scope[name] = item.value
    encoded = {name: write_value(item, scope) for name, item in record.fields.items()}
    if simple_name.startswith("PtBehaviorVariable") and simple_name not in ("PtBehaviorVariable", "PtBehaviorVariableDataBase"):
        record["size"] = sum(len(raw) for name, raw in encoded.items() if name not in ("unkn", "size"))
        encoded["size"] = write_value(record.fields["size"], scope)
    if simple_name == "PtBehaviorVariable":
        record["varSize"] = sum(len(raw) for name, raw in encoded.items() if name != "varSize")
        encoded["varSize"] = write_value(record.fields["varSize"], scope)
    for name, item in record.fields.items():
        target = item.definition.get("ByteCountOf")
        if item.definition.get("Flag") == "StructSize":
            candidate = item.definition.get("FlagTarget")
            if candidate in record.fields and record.fields[candidate].definition.get("Flag") == "EmbeddedEFX":
                target = candidate
        if target in encoded:
            item.value = len(encoded[target]) // item.definition.get('ByteCountDivisor', 1)
            scope[name] = item.value
            encoded[name] = write_value(item, scope)
    return b"".join(encoded.values())


@dataclass
class EfxBone:
    name: str
    value: int = 0
    name_hash: int = 0
    original_name: str = ""


@dataclass
class EfxAttribute:
    type_id: int
    name: str
    unique_id: int
    body: EfxRecord
    parent_bone: EfxBone | None = None
    binding_name: str | None = field(default=None, repr=False)
    extra_bones: list[EfxBone | None] = field(default_factory=list, repr=False)
    extra_binding_names: list[str | None] = field(default_factory=list, repr=False)

    @property
    def bone_fields(self):
        if not self.body.definition.get("BoneRelation"):
            return ()
        return tuple(self.body.definition.get("BoneFields", [name for name in ("BoneName", "boneName", "JointName") if name in self.body.fields]))

    @property
    def bone_bindings(self):
        return (self.parent_bone, *self.extra_bones) if self.bone_fields else ()

    def set_bone_reference(self, slot, bone, original_name):
        if slot == 0:
            self.parent_bone, self.binding_name = bone, original_name
        else:
            while len(self.extra_bones) < len(self.bone_fields) - 1:
                self.extra_bones.append(None)
                self.extra_binding_names.append(None)
            self.extra_bones[slot - 1] = bone
            self.extra_binding_names[slot - 1] = original_name

    def bind_bone(self, bone: EfxBone | None, slot=0):
        names = self.bone_fields
        if not names:
            return
        name = names[slot]
        self.set_bone_reference(slot, bone, self.binding_name if slot == 0 else self.extra_binding_names[slot - 1] if slot <= len(self.extra_binding_names) else None)
        self.body[name] = bone.name if bone else ""


@dataclass
class EfxEntry:
    index: int
    name: str
    assignment: int
    name_hash: int
    attributes: list[EfxAttribute]
    original_name: str = ""


@dataclass
class EfxFile:
    version: int
    header: list[int]
    names: dict[str, list[EfxField]]
    expression_parameters: list[EfxRecord]
    bones: list[EfxBone]
    field_parameters: list[EfxRecord]
    actions: list[EfxEntry]
    entries: list[EfxEntry]
    groups: list[EfxRecord]
    uvar_groups: list[EfxRecord]
    parent_bones: list[EfxBone] = field(default_factory=list, repr=False)
    inherits_bones: bool = False

    @property
    def effective_bones(self):
        return self.parent_bones if self.inherits_bones and not self.bones else self.bones

    @property
    def dimension(self):
        return self.header[1]

    @dimension.setter
    def dimension(self, value):
        self.header[1] = value

    @property
    def header_flags(self):
        return self.header[11] & 0xffffffff

    @header_flags.setter
    def header_flags(self, value):
        self.header[11] = value if value < 0x80000000 else value - 0x100000000

    @classmethod
    def can_handle(cls, data: bytes):
        return data[:4] == EFX_MAGIC

    @classmethod
    def from_bytes(cls, data: bytes, *, version: int, parent_bones=None):
        reader = Reader(data, version)
        if reader.take(4) != EFX_MAGIC:
            raise EfxParseError("Invalid EFX magic")
        header = [int.from_bytes(EFX_MAGIC, "little")] + list(reader.unpack("11i"))
        dimension, entry_count, string_size, action_count, field_count, expression_count, group_count, group_size, bone_count, relation_count, binding_count = header[1:]
        if dimension not in (0, 1):
            raise EfxParseError(f"Invalid EFX dimension {dimension}")
        string_start = reader.pos
        names = {}
        for key, count, paired in (("expressions", expression_count, True), ("bones", bone_count, True),
                                   ("actions", action_count, False), ("fields", field_count, False),
                                   ("entries", entry_count, False), ("groups", group_count, False)):
            values = []
            for _ in range(reader.count(count)):
                ascii_bytes = reader.cstring() if paired else None
                definition = {"StringEncoding": "utf-16le" if paired else "utf-8", "StringLength": [-1]}
                item = read_string(reader, key, definition, {})
                if paired:
                    item.definition["PairedAscii"] = ascii_bytes
                values.append(item)
            names[key] = values
        if reader.pos != string_start + string_size:
            raise EfxParseError(f"EFX string table size mismatch: declared {string_size}, consumed {reader.pos - string_start}")
        def simple_record(classname, descriptors):
            return read_record(reader, classname, definition={"Fields": descriptors})
        expression_definition = [{"Name": name, "FieldType": kind} for name, kind in
                                 (("utf16Hash", "U32"), ("utf8Hash", "U32"), ("type", "S32"),
                                  ("value1", "F32"), ("value2", "F32"), ("value3", "F32"))]
        expressions = [simple_record("ExpressionParameter", expression_definition) for _ in range(reader.count(expression_count))]
        for item, name in zip(expressions, names["expressions"]):
            item.name = item.original_name = name.value
        bones = []
        for name in names["bones"]:
            hash_value, value = reader.unpack("II")
            bones.append(EfxBone(name.value, value, hash_value, name.value))
        effective_bones = bones if bones else (parent_bones if parent_bones is not None else [])
        reader.bones = effective_bones
        relations = [reader.unpack("h") for _ in range(reader.count(relation_count))]
        def read_entry(name, action=False):
            index, hash_value = reader.unpack("iI")
            assignment = 0 if action else reader.unpack("i")
            count = reader.count(reader.unpack("i"))
            attributes = []
            for _ in range(count):
                type_id, unique = reader.unpack("ii")
                definition = reader.schema["by_id"].get(type_id)
                if definition is None:
                    previous = attributes[-1].name if attributes else 'entry header'
                    raise EfxParseError(f"Unsupported EFX attribute ID {type_id} at 0x{reader.pos - 8:X}, after {previous}")
                body_start = reader.pos
                try:
                    body = read_record(reader, definition["Classname"], definition=definition)
                except (EfxParseError, KeyError, struct.error, UnicodeError) as exc:
                    raise EfxParseError(f"{definition['Name']} (start 0x{body_start:X}) at 0x{reader.pos:X}: {exc}") from exc
                attributes.append(EfxAttribute(type_id, definition["Name"], unique, body))
            return EfxEntry(index, name.value, assignment, hash_value, attributes, name.value)
        actions = [read_entry(name, True) for name in names["actions"]]
        fields = []
        for _ in range(reader.count(field_count)):
            descriptors = [{"Name": name, "FieldType": kind} for name, kind in
                           (("unknown0", "U32"), ("nameHash", "U32"), ("unknown2", "U32"),
                            ("type", "U32"), ("unknown4", "U32"), ("value1", "S32"))]
            item = simple_record("FieldParameter", descriptors)
            if item["type"] == 196:
                item.fields["filePath"] = read_string(reader, "filePath", {"StringEncoding": "utf-16le", "StringLength": ["value1"]}, {"value1": item["value1"]})
            else:
                for name, kind in (("value2", "U32"), ("value3", "U32"), ("value4", "F32"), ("value5", "F32"), ("value6", "F32")):
                    item.fields[name] = read_value(reader, {"Name": name, "FieldType": kind}, {})
                if item["type"] in (110, 144, 183, 184, 202, 194, 215, 217):
                    item.fields["filePath"] = read_string(reader, "filePath", {"StringEncoding": "utf-16le"}, {})
            fields.append(item)
        for item, name in zip(fields, names["fields"]):
            item.name = item.original_name = name.value
        entries = [read_entry(name) for name in names["entries"]]
        groups_start = reader.pos
        group_definition = [{"Name": "utf16Hash", "FieldType": "U32"}, {"Name": "utf8Hash", "FieldType": "U32"},
                            {"Name": "count", "FieldType": "S32", "Flag": "StructSize", "FlagTarget": "indices"},
                            {"Name": "indices", "FieldType": "S32", "IsArray": True, "ArrayLength": ["count"]}]
        groups = [simple_record("EffectGroup", group_definition) for _ in range(reader.count(group_count))]
        for item, name in zip(groups, names["groups"]):
            item.name = item.original_name = name.value
        if reader.pos - groups_start != group_size:
            raise EfxParseError("EFX group byte size mismatch")
        uvars = []
        for kind in reader.unpack("2i"):
            if kind not in (0, 1, 2):
                raise EfxParseError(f"Invalid EFX user variable group type {kind}")
            item = EfxRecord("UserVariableGroup", {"type": EfxField("type", "S32", kind)}, {}, version)
            if kind == 2:
                for name in ("path", "group"):
                    item.fields[name] = read_string(reader, name, {"StringEncoding": "utf-16le", "StringLengthIsBytes": True}, {})
            uvars.append(item)
        if reader.pos != len(data):
            raise EfxParseError(f"EFX has {len(data) - reader.pos} unconsumed bytes at 0x{reader.pos:X}")
        bound = [(a, slot, name) for entry in entries for a in entry.attributes for slot, name in enumerate(a.bone_fields)]
        if len(bound) != len(relations):
            raise EfxParseError(f"EFX bone relation count {len(relations)} does not match {len(bound)} attributes")
        for (attribute, slot, name), index in zip(bound, relations):
            if not -1 <= index < len(effective_bones):
                raise EfxParseError(f"Invalid EFX bone reference {index}")
            attribute.set_bone_reference(slot, None if index == -1 else effective_bones[index], attribute.body[name])
        return cls(version, header, names, expressions, bones, fields, actions, entries, groups, uvars,
                   parent_bones if parent_bones is not None else [], parent_bones is not None)

    def all_attributes(self, *, recursive=False):
        result = []
        for entry in self.entries + self.actions:
            for attribute in entry.attributes:
                result.append(attribute)
                if recursive:
                    for item in attribute.body.fields.values():
                        if isinstance(item.value, EfxFile):
                            result.extend(item.value.all_attributes(recursive=True))
        return result

    def write(self) -> bytes:
        schema = schema_for_version(self.version)
        self._prepare_bindings()
        effective_bones = self.effective_bones
        categories = dict(self.names)
        return self._write(schema, effective_bones, categories)

    def _prepare_bindings(self):
        effective_bones = self.effective_bones
        # Preserve native string/index pairs until the binding string is edited.
        for attribute in self.all_attributes():
            for slot, field_name in enumerate(attribute.bone_fields):
                if field_name in attribute.body.fields:
                    item = attribute.body.fields[field_name]
                    previous_name = attribute.binding_name if slot == 0 else attribute.extra_binding_names[slot - 1] if slot <= len(attribute.extra_binding_names) else None
                    if item.value == previous_name:
                        continue
                    name = str(item.value)
                    bone = next((bone for bone in effective_bones if bone.name == name), None)
                    if name and bone is None:
                        bone = EfxBone(name, 0, murmur3_hash_utf16le(name), name)
                        effective_bones.append(bone)
                    attribute.set_bone_reference(slot, bone, name)
            for item in attribute.body.fields.values():
                if isinstance(item.value, EfxFile):
                    item.value._prepare_bindings()

    def _write(self, schema, effective_bones, categories):
        for key, values in (("bones", self.bones), ("entries", self.entries), ("actions", self.actions),
                            ("expressions", self.expression_parameters), ("fields", self.field_parameters),
                            ("groups", self.groups)):
            original = self.names[key]
            updated = []
            for index, value in enumerate(values):
                if index < len(original):
                    item = copy.copy(original[index])
                    item.definition = dict(item.definition)
                    item.value = value.name
                else:
                    item = EfxField(key, "String", value.name,
                                    {"StringEncoding": "utf-16le" if key in ("bones", "expressions") else "utf-8", "StringLength": [-1]}, terminators=1)
                updated.append(item)
            categories[key] = updated
        strings = bytearray()
        for key in ("expressions", "bones", "actions", "fields", "entries", "groups"):
            for item in categories[key]:
                if key in ("expressions", "bones"):
                    ascii_raw = item.definition.get("PairedAscii") if item.unchanged() else None
                    strings.extend(ascii_raw if ascii_raw is not None else str(item.value).encode("ascii", "replace") + b"\0")
                strings.extend(item.string_data())
        body = bytearray()
        for record in self.expression_parameters:
            if record.name != record.original_name:
                record["utf16Hash"] = murmur3_hash_utf16le(record.name)
                record["utf8Hash"] = murmur3_hash(record.name.encode("utf-8"))
            body.extend(write_record(record))
        for bone in self.bones:
            hash_value = bone.name_hash if bone.name == bone.original_name else murmur3_hash_utf16le(bone.name)
            body.extend(struct.pack("<II", hash_value, bone.value))
        relations = []
        for entry in self.entries:
            for attribute in entry.attributes:
                for binding in attribute.bone_bindings:
                    relations.append(-1 if binding is None else next(i for i, bone in enumerate(effective_bones) if bone is binding))
        body.extend(struct.pack(f"<{len(relations)}h", *relations))
        def write_entry(entry, action=False):
            name_hash = entry.name_hash if entry.name == entry.original_name else murmur3_hash(entry.name.encode("utf-8"))
            result = bytearray(struct.pack("<iI", entry.index, name_hash))
            result.extend(struct.pack("<i" if action else "<ii", *([len(entry.attributes)] if action else [entry.assignment, len(entry.attributes)])))
            for attribute in entry.attributes:
                if attribute.type_id not in schema["by_id"]:
                    raise EfxParseError(f"Unsupported EFX attribute ID {attribute.type_id}")
                result.extend(struct.pack("<ii", attribute.type_id, attribute.unique_id))
                result.extend(write_record(attribute.body))
            return result
        for action in self.actions:
            body.extend(write_entry(action, True))
        for record in self.field_parameters:
            if record.name != record.original_name:
                record["nameHash"] = murmur3_hash(record.name.encode("utf-8"))
            if record["type"] == 196 and not record.fields["filePath"].unchanged():
                record["value1"] = len(record.fields["filePath"].string_data()) // 2
            body.extend(write_record(record))
        for entry in self.entries:
            body.extend(write_entry(entry))
        for record in self.groups:
            if record.name != record.original_name:
                record["utf16Hash"] = murmur3_hash_utf16le(record.name)
                record["utf8Hash"] = murmur3_hash(record.name.encode("utf-8"))
        groups = b"".join(write_record(record) for record in self.groups)
        body.extend(groups)
        body.extend(struct.pack("<2i", *(record["type"] for record in self.uvar_groups)))
        for record in self.uvar_groups:
            for name, item in record.fields.items():
                if name != "type":
                    body.extend(write_value(item, {}))
        header = list(self.header)
        header[2:8] = [len(self.entries), len(strings), len(self.actions), len(self.field_parameters), len(self.expression_parameters), len(self.groups)]
        header[8:11] = [len(groups), len(self.bones), len(relations)]
        return struct.pack("<12i", *header) + strings + body

    def build_validated(self) -> bytes:
        result = self.write()
        reopened = EfxFile.from_bytes(result, version=self.version, parent_bones=self.parent_bones if self.inherits_bones else None)
        if reopened.write() != result:
            raise EfxParseError("EFX serialization is not stable after reopening")
        return result
