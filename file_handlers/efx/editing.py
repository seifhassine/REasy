"""Transactional EFX edits shared by the viewer and resource CLI.

Paths address list positions, never native entry IDs. Attribute values are under
``entries/0/attributes/0/fields/...``; embedded files use the same vocabulary.
"""
from __future__ import annotations

import copy
import struct
from dataclasses import dataclass

from .efx_file import (
    EFX_MAGIC, PRIMITIVES, EfxAttribute, EfxBone, EfxEntry, EfxField,
    EfxFile, EfxRecord, schema_for_version, field_class,
)
from .graph import scopes, references, rename_expression_parameter, DependencyImporter, unique_name
from .metadata import create_metadata, set_parameter_value, parameter_value, preserve_unchanged


FILE_LISTS = ("entries", "actions", "bones", "groups", "expression_parameters", "field_parameters")


def empty_file(version: int, dimension: int = 1) -> EfxFile:
    schema_for_version(version)
    if dimension not in (0, 1):
        raise ValueError("Dimension must be 0 or 1")
    return EfxFile.from_bytes(struct.pack("<4s11i2i", EFX_MAGIC, dimension, *([0] * 12)), version=version)


def tokens(path):
    if isinstance(path, (tuple, list)):
        return tuple(str(p) for p in path)
    if not isinstance(path, str):
        raise ValueError("An EFX path must be a string")
    return tuple(p.replace("~1", "/").replace("~0", "~") for p in path.strip("/").split("/")) if path.strip("/") else ()


def path_text(path):
    return "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in path)


@dataclass
class Location:
    value: object
    parent: object
    key: object
    document: EfxFile
    field: EfxField | None = None
    record: EfxRecord | None = None
    components: tuple = ()


def locate(root, path) -> Location:
    value, parent, key, document, field, record = root, None, None, root, None, None
    components = ()
    for part in tokens(path):
        if isinstance(value, EfxFile):
            document = value
            if part not in (*FILE_LISTS, "uvar_groups", 'dimension', 'header_flags'):
                raise ValueError(f"Unknown EFX collection: {part}")
            parent, key, value = value, part, getattr(value, part)
            field = record = None
        elif isinstance(value, EfxEntry):
            if part not in ("attributes", "name", "index", "assignment"):
                raise ValueError(f"Unknown entry property: {part}")
            parent, key, value = value, part, getattr(value, part)
        elif isinstance(value, EfxAttribute):
            if part == 'unique_id':
                parent, key, value = value, part, value.unique_id
            elif part == 'fields':
                parent, key, value = value, "body", value.body
            else:
                raise ValueError("Use fields to address an attribute's values")
        elif isinstance(value, EfxBone):
            if part not in ("name", "value"):
                raise ValueError(f"Unknown bone property: {part}")
            parent, key, value = value, part, getattr(value, part)
        elif isinstance(value, EfxRecord):
            record = value
            if part == "name" and value.name is not None:
                parent, key, value = value, "name", value.name
                field = None
            elif part == "fields":
                continue
            else:
                if part not in value.fields:
                    raise ValueError(f"Unknown field {part} in {value.classname}")
                field = value.fields[part]
                parent, key, value = field, "value", field.value
                components = ()
        elif isinstance(value, (list, tuple)):
            if not part.isdecimal() or not 0 <= int(part) < len(value):
                raise ValueError(f"Invalid list position: {part}")
            parent, key, value = value, int(part), value[int(part)]
            components += (int(part),)
        else:
            raise ValueError(f"Cannot descend through {part}")
    return Location(value, parent, key, value if isinstance(value, EfxFile) else document, field, record, components)


def data(value):
    """Editable, version-independent JSON values; binary metadata stays in the model."""
    if isinstance(value, EfxField):
        return data(value.value)
    if isinstance(value, EfxAttribute):
        return {"type": value.name, "fields": data(value.body)}
    if isinstance(value, EfxEntry):
        return {"name": value.name, "index": value.index, "assignment": value.assignment, "attributes": [data(a) for a in value.attributes]}
    if isinstance(value, EfxRecord):
        result = {name: data(item) for name, item in value.fields.items()}
        if value.classname == "ExpressionParameter" and value["type"] in (0, 1, 2, 3):
            return {"name": value.name, "fields": result, "type": value["type"], "value": parameter_value(value)}
        return {"name": value.name, "fields": result} if value.name is not None else result
    if isinstance(value, EfxBone):
        return {"name": value.name, "value": value.value}
    if isinstance(value, EfxFile):
        return {"version": value.version, "dimension": value.header[1], "header_flags": value.header[11] & 0xffffffff,
                **{key: data(getattr(value, key)) for key in (*FILE_LISTS, "uvar_groups")}}
    if isinstance(value, (list, tuple)):
        return [data(item) for item in value]
    return value


def structural_fields(record):
    result = set()
    for item in record.fields.values():
        d = item.definition
        if d.get("Flag") in ("StructSize", "StringLength", "BitSet") or "CountOf" in d or "ByteCountOf" in d:
            result.add(item.name)
        for kind in ("ArrayLength", "StringLength", "Switch"):
            result.update(x for x in d.get(kind, []) if isinstance(x, str) and x in record.fields)
    if record.classname.rsplit(".", 1)[-1] == "PtBehaviorVariable":
        result.add("varSize")
    if record.classname.rsplit(".", 1)[-1].startswith("PtBehaviorVariable"):
        result.add("size")
    if record.classname in ("EffectGroup", "ExpressionParameter"):
        result.update(("utf8Hash", "utf16Hash"))
    if record.classname == "FieldParameter":
        result.update(("nameHash", "type"))
    if record.classname == "UserVariableGroup":
        result.add("type")
    return result


def typed_value(kind, value, definition):
    if kind == "String":
        if not isinstance(value, str) or "\0" in value:
            raise ValueError("Expected a string without null characters")
        value.encode(definition.get("StringEncoding", "utf-16le"))
        return value
    if kind not in PRIMITIVES:
        raise ValueError(f"Cannot assign {kind} as a scalar")
    fmt = PRIMITIVES[kind]
    vector = len(fmt) > 1
    values = value if vector else [value]
    if vector and (not isinstance(values, (tuple, list)) or len(values) != int(fmt[:-1])):
        raise ValueError(f"{kind} requires {fmt[:-1]} components")
    if fmt[-1] == "?":
        if not all(isinstance(v, bool) for v in values):
            raise ValueError("Expected a boolean")
    elif fmt[-1] == "f":
        if not all(type(v) in (int, float) for v in values):
            raise ValueError("Expected a number")
    elif not all(type(v) is int for v in values):
        raise ValueError("Expected an integer")
    result = struct.unpack("<" + fmt, struct.pack("<" + fmt, *values))
    if fmt[-1] == "f":
        result = tuple(float(v) for v in values)
    return result if vector else result[0]


def record_from_data(version, classname, values, definition=None, context=None):
    """Construct from explicit values. Never invent unknown game defaults."""
    if not isinstance(values, dict):
        raise ValueError(f"{classname} requires a field object")
    schema = schema_for_version(version)
    definition = definition or schema["Structs"][classname]
    simple = classname.rsplit(".", 1)[-1]
    if simple in ("EFXExpressionList", "EFXMaterialExpressionList"):
        target = "ReeLib.Efx.Structs.Common." + ("EFXMaterialExpression" if simple == "EFXMaterialExpressionList" else "EFXExpressionObject")
        desc = {"Name": "expressions", "FieldType": "Object", "Classname": target, "IsArray": True}
        fields = {"expressions": EfxField("expressions", "Array", [record_from_data(version, target, v) for v in values["expressions"]], desc)}
        if simple == "EFXMaterialExpressionList":
            fields["indices"] = EfxField("indices", "Array", [typed_value("U32", v, {}) for v in values["indices"]], {"FieldType": "U32", "IsArray": True})
        return EfxRecord(classname, fields, definition, version)
    if simple in ("PtBehaviorVariableDataPrefabPath", "PtBehaviorVariableEnum"):
        target = "PtBehaviorVariableDataWString" if simple.endswith("PrefabPath") else "PtBehaviorVariableInteger"
        definition = schema["Structs"]["ReeLib.Efx.Structs.Pt." + target]
    descriptors = list(definition.get("Fields", []))
    if simple.startswith("PtBehaviorVariable") and simple not in ("PtBehaviorVariable", "PtBehaviorVariableDataBase"):
        descriptors = [{"Name": "unkn", "FieldType": "S32"}, {"Name": "size", "FieldType": "S32"}] + descriptors
    extra = set(values) - {d["Name"] for d in descriptors}
    if extra:
        raise ValueError(f"Unknown fields in {classname}: {sorted(extra)}")
    scope = dict(context or {}, Version=version)
    fields = {}
    for descriptor in descriptors:
        name, kind = descriptor["Name"], descriptor["FieldType"]
        computed = descriptor.get("Flag") in ("StructSize", "StringLength") or "CountOf" in descriptor or "ByteCountOf" in descriptor
        computed |= simple == "PtBehaviorVariable" and name == "varSize"
        computed |= simple.startswith("PtBehaviorVariable") and name == "size"
        if name not in values and not computed:
            raise ValueError(f"Missing value: {classname}.{name}")
        value = values.get(name, 0)
        if descriptor.get("Flag") == "EmbeddedEFX":
            from .construction import document_from_data
            value = document_from_data(version, value, record_from_data, typed_value)
        elif descriptor.get("IsArray"):
            if not isinstance(value, list):
                raise ValueError(f"{name} requires an array")
            if "FixedLength" in descriptor and len(value) != descriptor["FixedLength"]:
                raise ValueError(f"{name} requires {descriptor['FixedLength']} elements")
            value = [value_from_data(version, dict(descriptor, IsArray=False), v, scope) for v in value]
            kind = "Array"
        else:
            value = value_from_data(version, descriptor, value, scope)
        fields[name] = EfxField(name, kind, value, descriptor)
        scope[name] = value
    return EfxRecord(classname, fields, definition, version)


def value_from_data(version, descriptor, value, context):
    if descriptor["FieldType"] in ("Struct", "Object"):
        return record_from_data(version, field_class(descriptor, context), value, context=context)
    return typed_value(descriptor["FieldType"], value, descriptor)


def documents(root, prefix=()):
    for path, doc, _ in scopes(root, prefix):
        yield path, doc


class EfxEditor:
    def __init__(self, document):
        if not isinstance(document, EfxFile):
            raise ValueError("Structural editing requires an RE Engine EFX file")
        self.document = document
        self._undo = []
        self._redo = []
        self.clean = document.build_validated()

    @property
    def modified(self):
        return self.document.build_validated() != self.clean

    def mark_clean(self):
        self.clean = self.document.build_validated()

    def undo(self):
        if not self._undo:
            return False
        self._redo.append(self.document.build_validated())
        self.document = EfxFile.from_bytes(self._undo.pop(), version=self.document.version)
        return True

    def redo(self):
        if not self._redo:
            return False
        self._undo.append(self.document.build_validated())
        self.document = EfxFile.from_bytes(self._redo.pop(), version=self.document.version)
        return True

    def apply(self, operation, *, donor=None):
        before = self.document.build_validated()
        try:
            result = self._apply(operation, donor=donor)
            self._attach_embedded_bones(self.document)
            from .channels import validate_channels
            validate_channels(self.document)
            for reference in references(self.document):
                if not reference["valid"]:
                    raise ValueError(f"Dangling {reference['kind']} reference at {reference['source']}")
            after = self.document.build_validated()
        except (ValueError, KeyError, IndexError, TypeError, OverflowError, struct.error, StopIteration):
            self.document = EfxFile.from_bytes(before, version=self.document.version)
            raise
        if before != after:
            self._undo.append(before)
            self._redo.clear()
        return result

    def _attach_embedded_bones(self, document):
        for attribute in document.all_attributes():
            for item in attribute.body.fields.values():
                if isinstance(item.value, EfxFile):
                    item.value.parent_bones = document.effective_bones
                    item.value.inherits_bones = True
                    self._attach_embedded_bones(item.value)

    def _apply(self, op, *, donor=None):
        action = op["op"]
        if action == 'transaction':
            for operation in op['operations']:
                self._apply(operation)
            return op.get('path', '')
        path = tokens(op.get("path", ""))
        loc = locate(self.document, path)
        if action in ('curve-set', 'curve-delete', 'expression-set', 'expression-delete'):
            from .channels import edit_curve, edit_expression
            from .graph import expression_owner
            if action.startswith('curve-'):
                edit_curve(loc.value, op, record_from_data)
            else:
                edit_expression(loc.value, op, record_from_data, expression_owner(self.document, loc.document).expression_parameters)
            return path_text(path)
        if action == "set":
            self._set(loc, op["value"])
            return path_text(path)
        if action == "parameter-value":
            if not isinstance(loc.value, EfxRecord) or loc.value.classname != "ExpressionParameter":
                raise ValueError("Select an expression parameter")
            set_parameter_value(loc.value, op["value"], typed_value)
            return path_text(path)
        if action == "material-value":
            from .material import set_material_value
            set_material_value(loc.value, op['value'])
            return path_text(path)
        if action == "replace":
            self._replace(loc, op["value"])
            return path_text(path)
        if action == "insert":
            value = self._create(loc, op["value"])
            return self._insert(loc, path, value, op.get("index", len(loc.value)))
        if action in ("copy", "import"):
            source_root = donor if action == "import" else self.document
            if source_root is None:
                raise ValueError("Import requires a donor EFX document")
            source = locate(source_root, op["source"])
            if source_root is not self.document or source.document is not loc.document:
                importer = DependencyImporter(self.document, source_root, conflicts=op.get("conflicts", "rename"))
                if isinstance(source.value, (EfxRecord, EfxBone)):
                    category = "bones" if isinstance(source.value, EfxBone) else {
                        "ExpressionParameter": "expression_parameters", "FieldParameter": "field_parameters", "EffectGroup": "groups"}.get(source.value.classname)
                    if category != loc.key:
                        raise ValueError("Metadata source and target collections differ")
                    value = importer.import_group(source.value, source.document, loc.document) if category == "groups" else copy.deepcopy(source.value)
                    value.name = typed_value("String", op.get("name", unique_name(value.name, loc.value)), {"StringEncoding": "utf-8"})
                    if category == "expression_parameters":
                        from utils.hash_util import murmur3_hash, murmur3_hash_utf16le
                        value["utf8Hash"] = murmur3_hash(value.name.encode("utf-8"))
                        value["utf16Hash"] = murmur3_hash_utf16le(value.name)
                    return self._insert(loc, path, value, op.get("index", len(loc.value)))
                if isinstance(source.value, EfxEntry):
                    category = "actions" if source.parent is source.document.actions else "entries"
                    if category != loc.key:
                        raise ValueError("Entry and Action collection types differ")
                    if category == "actions":
                        index = importer.import_action(source.key, source.document, loc.document)
                        if "name" in op:
                            loc.document.actions[index].name = typed_value("String", op["name"], {"StringEncoding": "utf-8"})
                        if "index" in op:
                            return self._apply({"op": "move", "path": path + (index,), "index": op["index"]})
                        return path_text(path + (index,))
                value = importer.import_node(source.value, source.document, loc.document)
                if isinstance(value, EfxEntry):
                    value.name = typed_value('String', op.get('name', value.name), {'StringEncoding': 'utf-8'})
                result = self._insert(loc, path, value, op.get("index", len(loc.value)))
                if isinstance(value, EfxEntry):
                    importer.attach_groups(source.value, source.document, loc.document, int(tokens(result)[-1]))
                return result
            value = copy.deepcopy(source.value, {id(b): b for b in loc.document.effective_bones})
            if isinstance(value, EfxEntry):
                value.name = typed_value('String', op.get('name', value.name + '_copy'), {'StringEncoding': 'utf-8'})
                if loc.key == "entries":
                    value.index = max((e.index for e in loc.document.entries), default=0) + 1
                self._allocate_ids(value)
            elif isinstance(value, EfxAttribute):
                self._allocate_ids(value)
            elif isinstance(value, (EfxBone, EfxRecord)) and getattr(value, "name", None) is not None:
                value.name = typed_value('String', op.get('name', value.name + '_copy'), {'StringEncoding': 'utf-8'})
                if isinstance(value, EfxRecord) and value.classname == "ExpressionParameter":
                    from utils.hash_util import murmur3_hash, murmur3_hash_utf16le
                    value["utf8Hash"] = murmur3_hash(value.name.encode("utf-8"))
                    value["utf16Hash"] = murmur3_hash_utf16le(value.name)
            return self._insert(loc, path, value, op.get("index", len(loc.value)), source=source)
        if action in ("delete", "move"):
            if not isinstance(loc.parent, list):
                raise ValueError("Only list elements can be deleted or moved")
            collection = locate(self.document, path[:-1])
            self._mutable_collection(collection)
            if action == "move" and isinstance(loc.value, EfxAttribute):
                raise ValueError("Attributes retain their version-specific type order")
            old = list(loc.parent)
            order = list(range(len(old)))
            if action == "delete":
                self._check_delete(loc, path)
                loc.parent.pop(loc.key)
                order.pop(loc.key)
                if collection.field and collection.field.elements is not None:
                    collection.field.elements.pop(loc.key)
            else:
                index = op["index"]
                if type(index) is not int or not 0 <= index < len(old):
                    raise ValueError("Move position is outside the list")
                loc.parent.insert(index, loc.parent.pop(loc.key))
                order.insert(index, order.pop(loc.key))
                if collection.field and collection.field.elements is not None:
                    collection.field.elements.insert(index, collection.field.elements.pop(loc.key))
            self._remap(collection, old, order)
            return path_text(path[:-1] + (op["index"],)) if action == "move" else path_text(path)
        raise ValueError(f"Unknown EFX operation: {action}")

    def _allocate_ids(self, value):
        next_id = max((a.unique_id for a in self.document.all_attributes(recursive=True)), default=0) + 1
        attrs = value.attributes if isinstance(value, EfxEntry) else [value]
        for attr in attrs:
            attr.unique_id = next_id
            next_id += 1
            for item in attr.body.fields.values():
                if isinstance(item.value, EfxFile):
                    for child in item.value.all_attributes(recursive=True):
                        child.unique_id = next_id
                        next_id += 1

    def _set(self, loc, value):
        if isinstance(loc.parent, EfxFile) and loc.key in ('dimension', 'header_flags'):
            value = typed_value('U32', value, {})
            if loc.key == 'dimension' and value not in (0, 1):
                raise ValueError('EFX dimension must be 0 or 1')
            setattr(loc.parent, loc.key, value)
        elif isinstance(loc.parent, EfxAttribute) and loc.key == 'unique_id':
            value = typed_value('S32', value, {})
            if any(a is not loc.parent and a.unique_id == value for a in self.document.all_attributes(recursive=True)):
                raise ValueError('Attribute unique ID is already in use')
            loc.parent.unique_id = value
        elif isinstance(loc.parent, EfxField):
            if loc.record and loc.parent.name in structural_fields(loc.record):
                raise ValueError("Edit the corresponding structure instead of its count or variant selector")
            loc.parent.value = typed_value(loc.parent.kind, value, loc.parent.definition)
        elif isinstance(loc.parent, (list, tuple)) and loc.field:
            if loc.record and loc.field.name in structural_fields(loc.record):
                raise ValueError("This field describes the binary layout")
            kind = loc.field.definition.get("FieldType") if loc.field.kind == "Array" else loc.field.kind
            if isinstance(loc.parent, tuple):
                kind = "F32" if "f" in PRIMITIVES[kind] else "S32"
            value = typed_value(kind, value, loc.field.definition)
            def replace(current, path):
                if not path:
                    return value
                values = list(current)
                values[path[0]] = replace(values[path[0]], path[1:])
                return tuple(values) if isinstance(current, tuple) else values
            loc.field.value = replace(loc.field.value, loc.components)
        elif isinstance(loc.parent, EfxEntry) and loc.key in ("name", "assignment", "index"):
            if loc.key == "assignment" and any(entry is loc.parent for entry in loc.document.actions):
                raise ValueError("Actions do not have an assignment field")
            value = typed_value("String" if loc.key == "name" else "S32", value, {"StringEncoding": "utf-8"})
            if loc.key == "index" and any(e is not loc.parent and e.index == value for e in loc.document.entries):
                raise ValueError("Entry index is already in use")
            setattr(loc.parent, loc.key, value)
        elif isinstance(loc.parent, EfxRecord) and loc.key == "name":
            value = typed_value("String", value, {"StringEncoding": "utf-8"})
            if loc.parent.classname == "ExpressionParameter":
                rename_expression_parameter(self.document, loc.document, loc.parent, value)
            else:
                table = loc.document.field_parameters if loc.parent.classname == "FieldParameter" else loc.document.groups
                if any(item is not loc.parent and item.name == value for item in table):
                    raise ValueError("Name is already in use")
                loc.parent.name = value
        elif isinstance(loc.parent, EfxBone):
            if loc.key == "value":
                loc.parent.value = typed_value("U32", value, {})
            else:
                value = typed_value("String", value, {})
                if any(b is not loc.parent and b.name == value for b in loc.document.bones):
                    raise ValueError("Bone name is already in use")
                loc.parent.name = value
                for attr in self.document.all_attributes(recursive=True):
                    for slot, bone in enumerate(attr.bone_bindings):
                        if bone is loc.parent:
                            attr.bind_bone(loc.parent, slot)
                            attr.set_bone_reference(slot, bone, value)
        else:
            raise ValueError("Select an editable scalar or vector field")

    def _replace(self, loc, value):
        if not isinstance(loc.value, EfxRecord):
            raise ValueError("Replace targets a record; use scalar set or collection insert for other objects")
        category = {"ExpressionParameter": "expression_parameters", "FieldParameter": "field_parameters", "UserVariableGroup": "uvar_groups", "EffectGroup": "groups"}.get(loc.value.classname)
        if category:
            replacement = self._create(Location(loc.document.groups, loc.document, 'groups', loc.document), value) if category == 'groups' else create_metadata(category, loc.document.version, value, record_from_data, typed_value)
            if replacement.name is not None and any(item is not loc.value and item.name == replacement.name for item in getattr(loc.document, category)):
                raise ValueError('Name is already in use')
            if category == "expression_parameters" and replacement.name != loc.value.name:
                rename_expression_parameter(self.document, loc.document, loc.value, replacement.name)
        else:
            context = {k: f.value for k, f in loc.record.fields.items()} if loc.record is not None else {}
            replacement = record_from_data(loc.document.version, loc.value.classname, value, loc.value.definition, context)
        from .graph import records
        next_id = max((a.unique_id for a in self.document.all_attributes(recursive=True)), default=0) + 1
        for _, record in records(replacement):
            for item in record.fields.values():
                if isinstance(item.value, EfxFile):
                    for attribute in item.value.all_attributes(recursive=True):
                        attribute.unique_id = next_id
                        next_id += 1
        preserve_unchanged(loc.value, replacement)
        if isinstance(loc.parent, EfxField):
            loc.parent.value = replacement
        elif isinstance(loc.parent, EfxAttribute):
            loc.parent.body = replacement
        elif isinstance(loc.parent, list):
            loc.parent[loc.key] = replacement
        else:
            raise ValueError("Cannot replace this record")

    def _mutable_collection(self, loc):
        if not isinstance(loc.value, list):
            raise ValueError("Target must be a list")
        if loc.field:
            if loc.record.classname.rsplit(".", 1)[-1] in ("EFXExpressionList", "EFXMaterialExpressionList", "EfxClipData", "EfxMaterialClipData"):
                raise ValueError("Channel edits require updating their curve/expression bit sets together")
            if "FixedLength" in loc.field.definition:
                raise ValueError("Fixed-length arrays cannot change shape")
            if loc.field.definition.get("Flag") == "BitSet":
                raise ValueError("Edit curve/expression channels together with their bit set")
        elif isinstance(loc.parent, EfxFile) and loc.key not in FILE_LISTS:
            raise ValueError("This file collection has a fixed shape")
        elif not isinstance(loc.parent, (EfxFile, EfxEntry)):
            raise ValueError("Unsupported collection")

    def _create(self, loc, value):
        self._mutable_collection(loc)
        doc = loc.document
        if loc.field:
            scope = {k: v.value for k, v in loc.record.fields.items()}
            scope["Version"] = doc.version
            return value_from_data(doc.version, dict(loc.field.definition, IsArray=False), value, scope)
        if not isinstance(value, dict):
            raise ValueError("An object is required")
        if isinstance(loc.parent, EfxEntry):
            definition = schema_for_version(doc.version)["AttributeTypes"].get(value["type"])
            if definition is None:
                raise ValueError(f"Unsupported attribute: {value['type']}")
            body = record_from_data(doc.version, definition["Classname"], value["fields"], definition)
            result = EfxAttribute(definition["TypeID"], definition["Name"], 0, body)
            self._allocate_ids(result)
            return result
        if loc.key in ("entries", "actions"):
            name = typed_value("String", value["name"], {"StringEncoding": "utf-8"})
            index = value.get("index", max((e.index for e in doc.entries), default=0) + 1 if loc.key == "entries" else 0)
            index = typed_value("S32", index, {})
            if loc.key == "entries" and any(e.index == index for e in doc.entries):
                raise ValueError("Entry index is already in use")
            assignment = typed_value("S32", value.get("assignment", 2), {}) if loc.key == "entries" else 0
            result = EfxEntry(index, name, assignment, 0, [])
            for attr in value.get("attributes", []):
                result.attributes.append(self._create(Location(result.attributes, result, "attributes", doc), attr))
            self._allocate_ids(result)
            return result
        if loc.key == "bones":
            return EfxBone(typed_value("String", value["name"], {}), typed_value("U32", value.get("value", 0), {}))
        if loc.key in ("expression_parameters", "field_parameters"):
            return create_metadata(loc.key, doc.version, value, record_from_data, typed_value)
        if loc.key == "groups":
            name = typed_value("String", value["name"], {"StringEncoding": "utf-8"})
            members = value.get("indices", value.get("fields", {}).get("indices", []))
            if not all(type(i) is int and 0 <= i < len(doc.entries) for i in members):
                raise ValueError("Group members must be entry positions")
            return EfxRecord("EffectGroup", {
                "utf16Hash": EfxField("utf16Hash", "U32", 0),
                "utf8Hash": EfxField("utf8Hash", "U32", 0),
                "count": EfxField("count", "S32", len(members), {"Flag": "StructSize", "FlagTarget": "indices"}),
                "indices": EfxField("indices", "Array", members, {"FieldType": "S32", "IsArray": True, "ArrayLength": ["count"]}),
            }, {}, doc.version, name=name)
        raise ValueError("Parameter creation requires expression/field reference mapping")

    def _insert(self, loc, path, value, index, source=None):
        self._mutable_collection(loc)
        if type(index) is not int or not 0 <= index <= len(loc.value):
            raise ValueError("Insert position is outside the list")
        expected = EfxEntry if loc.key in ("entries", "actions") else EfxBone if loc.key == "bones" else EfxAttribute if isinstance(loc.parent, EfxEntry) else EfxRecord
        if not loc.field and not isinstance(value, expected):
            raise ValueError("Source and destination types differ")
        if loc.field:
            descriptor = loc.field.definition
            if descriptor["FieldType"] in ("Struct", "Object"):
                scope = {k: v.value for k, v in loc.record.fields.items()}
                scope["Version"] = loc.document.version
                if not isinstance(value, EfxRecord) or value.classname != field_class(descriptor, scope):
                    raise ValueError("Source and destination record types differ")
            else:
                typed_value(descriptor["FieldType"], value, descriptor)
        if isinstance(value, EfxAttribute):
            if any(a.type_id == value.type_id for a in loc.value):
                raise ValueError(f"{value.name} already exists in this entry")
            # The native attribute list is ordered by its version-specific type ID.
            index = next((i for i, a in enumerate(loc.value) if a.type_id > value.type_id), len(loc.value))
        if isinstance(value, EfxBone) and any(b.name == value.name for b in loc.value):
            raise ValueError("Bone name is already in use")
        if isinstance(value, EfxRecord) and value.name is not None and any(item.name == value.name for item in loc.value):
            raise ValueError("Name is already in use")
        old = list(loc.value)
        loc.value.insert(index, value)
        if loc.field and loc.field.elements is not None:
            if source and source.field and source.field.elements is not None and len(source.components) == 1:
                element = copy.deepcopy(source.field.elements[source.key])
                element.value = value
            else:
                element = EfxField(loc.field.name, loc.field.definition["FieldType"], value, dict(loc.field.definition, IsArray=False))
            loc.field.elements.insert(index, element)
        order = list(range(len(old)))
        order.insert(index, None)
        self._remap(loc, old, order)
        if source and loc.key == "entries":
            source_index = next(i for i, entry in enumerate(loc.value) if entry is source.value)
            for group in loc.document.groups:
                if source_index in group["indices"]:
                    position = group["indices"].index(source_index) + 1
                    group["indices"].insert(position, index)
                    group.fields["indices"].elements = None
        elif source and loc.key == "actions" and isinstance(value, EfxEntry):
            source_index = next(i for i, entry in enumerate(loc.value) if entry is source.value)
            for attr in value.attributes:
                if attr.name == "PtLife" and attr.body["ActionIndex"] == source_index:
                    attr.body["ActionIndex"] = index
        return path_text(path + (index,))

    def _check_delete(self, loc, path):
        for row in references(self.document):
            if row["target"] == path_text(path):
                if row["source"].startswith(path_text(path) + "/"):
                    continue
                if row["kind"] == "entry":
                    continue  # Group membership is removed along with the entry.
                raise ValueError(f"Object is referenced by {row['source']}; retarget that field first")
        if isinstance(loc.value, EfxBone):
            if any(b is loc.value for a in self.document.all_attributes(recursive=True) for b in a.bone_bindings):
                raise ValueError("Bone is still referenced; change the bindings first")

    def _remap(self, collection, old, order):
        if collection.field and collection.field.name == 'texPaths' and 'properties' in collection.record.fields:
            from .material import remap_textures
            remap_textures(collection.record, order)
        if not isinstance(collection.parent, EfxFile):
            return
        mapping = {i: next((j for j, item in enumerate(collection.value) if item is source), None) for i, source in enumerate(old)}
        doc = collection.document
        if collection.key == "entries":
            for group in doc.groups:
                group["indices"] = [mapping[i] for i in group["indices"] if mapping.get(i) is not None]
                group.fields["indices"].elements = None
        elif collection.key == "actions":
            for attr in doc.all_attributes():
                if attr.name == "PtLife":
                    index = attr.body["ActionIndex"]
                    if index in mapping and mapping[index] is not None:
                        attr.body["ActionIndex"] = mapping[index]
        elif collection.key == "field_parameters":
            for attr in doc.all_attributes():
                if attr.name == "VectorFieldParameter":
                    index = attr.body["FieldIndex"]
                    if index in mapping and mapping[index] is not None:
                        attr.body["FieldIndex"] = mapping[index]
