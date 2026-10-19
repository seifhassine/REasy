from dataclasses import dataclass
import hashlib
import json
import re

lazy from .dxil_metadata import metadata_operands
lazy from .shader_graph import ShaderGraphError


@dataclass(frozen=True, slots=True)
class ShaderStorageType:
    kind: str
    count: int = 0
    element: 'ShaderStorageType | None' = None
    members: tuple['ShaderStorageType', ...] = ()

    @classmethod
    def from_data(cls, data, depth=0):
        if depth > 16 or not isinstance(data, dict) or set(data) != {'kind','count','element','members'}:
            raise ShaderGraphError('Invalid typed shader storage declaration.')
        members = data['members']
        if not isinstance(members, (tuple,list)) or len(members) > 128:
            raise ShaderGraphError('Invalid shader storage member list.')
        return cls(data['kind'], data['count'], cls.from_data(data['element'],depth+1) if data['element'] is not None else None,
                   tuple(cls.from_data(member,depth+1) for member in members))

    def __post_init__(self):
        if not isinstance(self.kind,str) or type(self.count) is not int or not isinstance(self.members,tuple):
            raise ShaderGraphError('Invalid typed shader storage declaration.')
        valid = (self.kind in {'uint','float','bool'} and self.count == 0 and self.element is None and not self.members
                 or self.kind == 'array' and 1 <= self.count <= 8192 and isinstance(self.element,ShaderStorageType) and not self.members
                 or self.kind == 'struct' and self.count == 0 and self.element is None and 1 <= len(self.members) <= 128
                 and all(isinstance(member,ShaderStorageType) for member in self.members))
        if not valid or self.byte_size > 32768:
            raise ShaderGraphError('Unsupported group/payload scalar type, member layout or extent.')

    @property
    def byte_size(self):
        return self.count * self.element.byte_size if self.kind == 'array' else sum(member.byte_size for member in self.members) if self.kind == 'struct' else 4

    def to_data(self):
        return dict(kind=self.kind, count=self.count, element=self.element.to_data() if self.element else None,
                    members=[member.to_data() for member in self.members])

    @property
    def type_name(self):
        return 'REasyStorage_' + hashlib.sha256(json.dumps(self.to_data(), sort_keys=True).encode()).hexdigest()[:20]

    def declaration(self, name):
        if self.kind == 'array':
            return self.element.declaration(f'{name}[{self.count}]')
        return f'{self.type_name if self.kind == "struct" else self.kind} {name}'

    def declarations(self):
        result = []
        if self.kind == 'array':
            return self.element.declarations()
        if self.kind == 'struct':
            for member in self.members:
                result.extend(member.declarations())
            result.append(f'struct {self.type_name} {{\n' + '\n'.join('    '+member.declaration(f'field{i}')+';' for i,member in enumerate(self.members)) + '\n};')
        return tuple(dict.fromkeys(result))


def native_storage_type(text, definitions, trail=()):
    if len(trail) > 16 or text in trail:
        raise ShaderGraphError('Recursive or excessively nested shader storage type.')
    if text in {'i32','float','i1'}:
        return ShaderStorageType({'i32':'uint','float':'float','i1':'bool'}[text])
    if match := re.fullmatch(r'\[(\d+) x (.+)\]',text):
        return ShaderStorageType('array', int(match[1]), native_storage_type(match[2], definitions, (*trail,text)))
    if text.startswith('%') and text in definitions:
        return native_storage_type(definitions[text],definitions,(*trail,text))
    if text.startswith('{ ') and text.endswith(' }'):
        return ShaderStorageType('struct', members=tuple(native_storage_type(member,definitions,(*trail,text))
            for member in metadata_operands('!{'+text[2:-2]+'}')))
    raise ShaderGraphError('This group/payload element needs an additional semantic type adapter.')
