"""Native EFX reference scopes and dependency-preserving copies."""
from __future__ import annotations

import copy

from .efx_file import EfxFile, EfxRecord, EfxEntry, EfxAttribute, EfxBone, write_record
from utils.hash_util import murmur3_hash, murmur3_hash_utf16le


def pointer(parts):
    return '/'.join(str(p).replace('~', '~0').replace('/', '~1') for p in parts)


def records(value, path=()):
    if isinstance(value, EfxRecord):
        yield path, value
        for name, item in value.fields.items():
            yield from records(item.value, path + (name,))
    elif isinstance(value, (tuple, list)):
        for i, item in enumerate(value):
            yield from records(item, path + (i,))


def scopes(root, prefix=(), parent=None):
    yield prefix, root, parent
    for category in ('entries', 'actions'):
        for ei, entry in enumerate(getattr(root, category)):
            for ai, attr in enumerate(entry.attributes):
                for record_path, record in records(attr.body):
                    for name, item in record.fields.items():
                        if isinstance(item.value, EfxFile):
                            yield from scopes(item.value, prefix + (category, ei, 'attributes', ai, 'fields') + record_path + (name,), root)


def expressions(doc):
    for category in ('entries', 'actions'):
        for ei, entry in enumerate(getattr(doc, category)):
            for ai, attr in enumerate(entry.attributes):
                for path, record in records(attr.body):
                    if record.classname.rsplit('.', 1)[-1] in ('EFXExpressionObject', 'EFXMaterialExpression'):
                        yield (category, ei, 'attributes', ai, 'fields') + path, record


def expression_owner(root, doc):
    return next(parent or candidate for _, candidate, parent in scopes(root) if candidate is doc)


def rewrite_expression_hash(expression, old, new):
    for item in expression['parameters']:
        if item['source'] == 0 and item['parameterNameHash'] == old:
            item['parameterNameHash'] = new
    for component in expression['components']:
        if component['type'] == 4 and component['data']['parameterHash'] == old:
            component['data']['parameterHash'] = new


def rename_expression_parameter(root, owner, parameter, name):
    old = parameter['utf8Hash']
    new = murmur3_hash(name.encode('utf-8'))
    if any(p is not parameter and (p.name == name or p['utf8Hash'] == new) for p in owner.expression_parameters):
        raise ValueError('Expression parameter name/hash is already in use')
    for _, doc, parent in scopes(root):
        if (parent or doc) is owner:
            for _, expression in expressions(doc):
                if any(p['source'] == 0 and p['parameterNameHash'] == old for p in expression['parameters']):
                    rewrite_expression_hash(expression, old, new)
    parameter.name = name
    parameter['utf8Hash'], parameter['utf16Hash'] = new, murmur3_hash_utf16le(name)


def references(root):
    rows = []
    scope_rows = list(scopes(root))
    paths = {id(doc): prefix for prefix, doc, _ in scope_rows}
    bones = {id(b): pointer(prefix + ('bones', i)) for prefix, doc, _ in scope_rows for i, b in enumerate(doc.bones)}
    for prefix, doc, parent in scope_rows:
        for gi, group in enumerate(doc.groups):
            for i, index in enumerate(group['indices']):
                rows.append({'kind': 'entry', 'source': pointer(prefix + ('groups', gi, 'fields', 'indices', i)),
                             'target': pointer(prefix + ('entries', index)), 'valid': 0 <= index < len(doc.entries)})
        for category in ('entries', 'actions'):
            for ei, entry in enumerate(getattr(doc, category)):
                for ai, attr in enumerate(entry.attributes):
                    base = prefix + (category, ei, 'attributes', ai, 'fields')
                    for material_path, material in records(attr.body):
                        if {'properties', 'texPaths'} <= material.fields.keys():
                            from .material import texture_index
                            for pi, prop in enumerate(material['properties']):
                                if prop['parameterType'] == 3:
                                    index = texture_index(prop)
                                    rows.append({'kind': 'texture', 'source': pointer(base + material_path + ('properties', pi, 'rawValue')),
                                                 'target': pointer(base + material_path + ('texPaths', index)),
                                                 'valid': 0 <= index < len(material['texPaths'])})
                    for name, bone in zip(attr.bone_fields, attr.bone_bindings):
                        if bone is not None:
                            rows.append({'kind': 'bone', 'source': pointer(base + (name,)), 'target': bones.get(id(bone)), 'valid': id(bone) in bones})
                    if attr.name == 'PtLife':
                        index = attr.body['ActionIndex']
                        rows.append({'kind': 'action', 'source': pointer(base + ('ActionIndex',)),
                                     'target': pointer(prefix + ('actions', index)), 'valid': 0 <= index < len(doc.actions)})
                    if attr.name == 'VectorFieldParameter':
                        index = attr.body['FieldIndex']
                        if index >= 0:
                            rows.append({'kind': 'field', 'source': pointer(base + ('FieldIndex',)),
                                         'target': pointer(prefix + ('field_parameters', index)), 'valid': index < len(doc.field_parameters)})
        owner = parent or doc
        parameters = {p['utf8Hash']: i for i, p in enumerate(owner.expression_parameters)}
        for path, expression in expressions(doc):
            for i, parameter in enumerate(expression['parameters']):
                if parameter['source'] == 0:
                    index = parameters.get(parameter['parameterNameHash'])
                    rows.append({'kind': 'expression', 'source': pointer(prefix + path + ('parameters', i, 'parameterNameHash')),
                                 'target': pointer(paths[id(owner)] + ('expression_parameters', index)) if index is not None else None,
                                 'valid': index is not None})
    return rows


def unique_name(name, collection):
    names = {item.name for item in collection}
    if name not in names:
        return name
    number = 2
    while f'{name}_{number}' in names:
        number += 1
    return f'{name}_{number}'


class DependencyImporter:
    def __init__(self, target_root, donor_root, *, conflicts='rename'):
        if target_root.version != donor_root.version:
            raise ValueError('EFX versions differ; a format conversion is required')
        if conflicts not in ('rename', 'error'):
            raise ValueError('Conflict policy must be rename or error')
        self.root, self.donor, self.conflicts = target_root, donor_root, conflicts
        self.action_map, self.field_map, self.parameter_map = {}, {}, {}
        self.next_id = max((a.unique_id for a in target_root.all_attributes(recursive=True)), default=0) + 1
        self.imported = {'actions': 0, 'field_parameters': 0, 'expression_parameters': 0, 'bones': 0}

    def _parameter(self, source_owner, target_owner, hash_value):
        key = (id(source_owner), id(target_owner), hash_value)
        if key in self.parameter_map:
            return self.parameter_map[key]
        source = next((p for p in source_owner.expression_parameters if p['utf8Hash'] == hash_value), None)
        if source is None:
            raise ValueError(f'Unresolved expression parameter 0x{hash_value:08X}')
        match = next((p for p in target_owner.expression_parameters if p.name == source.name), None)
        if match and write_record(copy.deepcopy(match)) == write_record(copy.deepcopy(source)):
            new_hash = match['utf8Hash']
        else:
            if match and self.conflicts == 'error':
                raise ValueError(f'Expression parameter conflict: {source.name}')
            clone = copy.deepcopy(source)
            clone.name = unique_name(source.name, target_owner.expression_parameters)
            clone['utf8Hash'] = new_hash = murmur3_hash(clone.name.encode('utf-8'))
            clone['utf16Hash'] = murmur3_hash_utf16le(clone.name)
            target_owner.expression_parameters.append(clone)
            self.imported['expression_parameters'] += 1
        self.parameter_map[key] = new_hash
        return new_hash

    def _field(self, source_doc, target_doc, index):
        if source_doc is target_doc:
            return index
        key = (id(source_doc), id(target_doc), index)
        if key not in self.field_map:
            if not 0 <= index < len(source_doc.field_parameters):
                raise ValueError(f'Unresolved field parameter {index}')
            source = source_doc.field_parameters[index]
            match = next((i for i, item in enumerate(target_doc.field_parameters)
                          if item.name == source.name and write_record(copy.deepcopy(item)) == write_record(copy.deepcopy(source))), None)
            if match is not None:
                self.field_map[key] = match
            else:
                if self.conflicts == 'error' and any(item.name == source.name for item in target_doc.field_parameters):
                    raise ValueError(f'Field parameter conflict: {source.name}')
                clone = copy.deepcopy(source)
                clone.name = unique_name(source.name, target_doc.field_parameters)
                clone['nameHash'] = murmur3_hash(clone.name.encode('utf-8'))
                self.field_map[key] = len(target_doc.field_parameters)
                target_doc.field_parameters.append(clone)
                self.imported['field_parameters'] += 1
        return self.field_map[key]

    def _action(self, source_doc, target_doc, index, source_owner, target_owner):
        if source_doc is target_doc:
            return index
        key = (id(source_doc), id(target_doc), index)
        if key not in self.action_map:
            if not 0 <= index < len(source_doc.actions):
                raise ValueError(f'Unresolved action {index}')
            source = source_doc.actions[index]
            clone = self._clone(source, source_doc)
            clone.name = unique_name(source.name, target_doc.actions)
            self.action_map[key] = len(target_doc.actions)
            target_doc.actions.append(clone)  # Reserve before recursion to preserve cycles.
            self.imported['actions'] += 1
            self._entry(source, clone, source_doc, target_doc, source_owner, target_owner)
        return self.action_map[key]

    def _clone(self, node, source_doc):
        return copy.deepcopy(node, {id(b): b for b in source_doc.effective_bones})

    def _bone(self, bone, target_doc):
        if bone is None:
            return None
        table = target_doc.effective_bones
        match = next((b for b in table if b.name == bone.name), None)
        if match is not None:
            if match.value != bone.value:
                raise ValueError(f'Bone metadata conflict: {bone.name}')
            return match
        clone = copy.deepcopy(bone)
        # An embedded scope with an empty inherited table must be attached first.
        table.append(clone)
        self.imported['bones'] += 1
        return clone

    def _attribute(self, source, clone, source_doc, target_doc, source_owner, target_owner):
        clone.unique_id = self.next_id
        self.next_id += 1
        for slot, name in enumerate(source.bone_fields):
            bone = source.bone_bindings[slot] if slot < len(source.bone_bindings) else None
            clone.set_bone_reference(slot, self._bone(bone, target_doc), source.body[name])
        if source.name == 'PtLife':
            clone.body['ActionIndex'] = self._action(source_doc, target_doc, source.body['ActionIndex'], source_owner, target_owner)
        if source.name == 'VectorFieldParameter' and source.body['FieldIndex'] >= 0:
            clone.body['FieldIndex'] = self._field(source_doc, target_doc, source.body['FieldIndex'])
        source_records = dict(records(source.body))
        for path, record in records(clone.body):
            original = source_records[path]
            if record.classname.rsplit('.', 1)[-1] in ('EFXExpressionObject', 'EFXMaterialExpression'):
                if any(p['source'] == 2 for p in original['parameters']):
                    for i, binding in enumerate(source_owner.uvar_groups):
                        if binding['type'] == 0:
                            continue
                        current = target_owner.uvar_groups[i]
                        if current['type'] != 0 and write_record(copy.deepcopy(current)) != write_record(copy.deepcopy(binding)):
                            raise ValueError(f'External UVAR binding conflict in slot {i}')
                        target_owner.uvar_groups[i] = copy.deepcopy(binding)
                hashes = {p['parameterNameHash'] for p in original['parameters'] if p['source'] == 0}
                mapping = {h: self._parameter(source_owner, target_owner, h) for h in hashes}
                for p in record['parameters']:
                    if p['source'] == 0:
                        p['parameterNameHash'] = mapping[p['parameterNameHash']]
                for component in record['components']:
                    if component['type'] == 4 and component['data']['parameterHash'] in mapping:
                        component['data']['parameterHash'] = mapping[component['data']['parameterHash']]
            for name, item in record.fields.items():
                if isinstance(item.value, EfxFile):
                    child_source = original[name]
                    child = item.value
                    child.parent_bones = target_doc.effective_bones
                    child.inherits_bones = True
                    # Entire child tables were cloned; references within them retain their positions.
                    for i in range(len(child.actions)):
                        self.action_map[(id(child_source), id(child), i)] = i
                    for i in range(len(child.field_parameters)):
                        self.field_map[(id(child_source), id(child), i)] = i
                    for category in ('entries', 'actions'):
                        for old_entry, new_entry in zip(getattr(child_source, category), getattr(child, category)):
                            self._entry(old_entry, new_entry, child_source, child, source_doc, target_doc)

    def _entry(self, source, clone, source_doc, target_doc, source_owner, target_owner):
        for old_attr, new_attr in zip(source.attributes, clone.attributes):
            self._attribute(old_attr, new_attr, source_doc, target_doc, source_owner, target_owner)

    def import_node(self, node, source_doc, target_doc):
        source_owner = expression_owner(self.donor, source_doc)
        target_owner = expression_owner(self.root, target_doc)
        clone = self._clone(node, source_doc)
        if isinstance(clone, EfxEntry):
            self._entry(node, clone, source_doc, target_doc, source_owner, target_owner)
            clone.index = max((e.index for e in target_doc.entries), default=0) + 1
        elif isinstance(clone, EfxAttribute):
            self._attribute(node, clone, source_doc, target_doc, source_owner, target_owner)
        else:
            raise ValueError('Dependency import accepts an entry or attribute')
        return clone

    def import_action(self, index, source_doc, target_doc):
        return self._action(source_doc, target_doc, index, expression_owner(self.donor, source_doc), expression_owner(self.root, target_doc))

    def attach_groups(self, source, source_doc, target_doc, target_index):
        source_index = next(i for i, entry in enumerate(source_doc.entries) if entry is source)
        for group in source_doc.groups:
            if source_index not in group['indices']:
                continue
            target = next((g for g in target_doc.groups if g.name == group.name), None)
            if target is None:
                target = copy.deepcopy(group)
                target['indices'] = []
                target_doc.groups.append(target)
            if target_index not in target['indices']:
                target['indices'].append(target_index)
                target.fields['indices'].elements = None

    def import_group(self, source, source_doc, target_doc):
        clone = copy.deepcopy(source)
        clone.name = unique_name(source.name, target_doc.groups)
        mapped = {}
        for index in source['indices']:
            if index not in mapped:
                entry = self.import_node(source_doc.entries[index], source_doc, target_doc)
                entry.name = unique_name(entry.name, target_doc.entries)
                mapped[index] = len(target_doc.entries)
                target_doc.entries.append(entry)
        clone['indices'] = [mapped[index] for index in source['indices']]
        clone.fields['indices'].elements = None
        return clone
