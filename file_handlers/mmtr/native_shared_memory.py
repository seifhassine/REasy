from dataclasses import dataclass
import hashlib
import re

lazy from file_handlers.sdf.shader_graph import ShaderGraphError
lazy from file_handlers.sdf.shader_storage import native_storage_type
from .native_local_memory import split
lazy from .profiles.re_engine_dxil_recovery import LLVM_NAME


@dataclass(frozen=True)
class SharedPointer:
    storage: dict
    element: object
    path: tuple = ()
    indices: tuple = ()

    @property
    def properties(self):
        return {'storage': self.storage, 'path': list(self.path)}


class SharedMemory:
    def __init__(self, ir, metadata, builder, value):
        self.metadata, self.builder, self.value = metadata, builder, value
        self.pointers = {}
        namespace = builder.contract.source_sha256[:20] or hashlib.sha256(ir.to_assembly().encode()).hexdigest()[:20]
        for record in ir.records:
            match = re.fullmatch(r'(@'+LLVM_NAME+r') = (?:external )?addrspace\(3\) global (.+)',record.text)
            if match is None:
                continue
            declaration = split(match[2])[0].removesuffix(' undef')
            layout = native_storage_type(declaration,metadata.storage_types)
            storage = dict(id=f'group_{namespace}_{len(self.pointers)}', scope='group', layout=layout.to_data())
            self.pointers[match[1]] = SharedPointer(storage,layout)

    def address(self, text):
        text = re.sub(r'\* (?:nonnull |nocapture |readonly |writeonly )*', '* ', text)
        match = re.fullmatch(r'(.+?)(?: addrspace\(([036])\))?\* ([%@]'+LLVM_NAME+r')',text)
        if match is None or match[3] not in self.pointers:
            return None
        pointer = self.pointers[match[3]]
        if native_storage_type(match[1],self.metadata.storage_types) != pointer.element:
            raise ShaderGraphError('A shared address disagrees with its declared pointee.')
        if pointer.storage['scope']=='group' and match[2]!='3' or pointer.storage['scope']=='payload' and match[2] not in {None,'0','6'}:
            raise ShaderGraphError('A shared address changed its storage ownership.')
        return pointer

    def apply(self, record, body, values):
        if match := re.fullmatch(r'call (%[-\w.$]+)\* @dx\.op\.getMeshPayload\.[^(]+\(i32 170\)(?: #\d+)?',body):
            layout = native_storage_type(match[1],self.metadata.storage_types)
            execution = self.builder.contract.mesh_execution
            if self.metadata.stage != 'mesh' or execution is None or execution.payload_layout != layout:
                raise ShaderGraphError('Incoming mesh payload differs from its declared stage linkage.')
            self.pointers[record.result] = SharedPointer(dict(id='payload',scope='payload',layout=layout.to_data()),layout)
            return True
        if match := re.fullmatch(r'call void @dx\.op\.dispatchMesh\.[^(]+\((.+)\)(?: #\d+)?',body):
            parts = split(match[1])
            if len(parts)!=5 or parts[0]!='i32 173' or any(not part.startswith('i32 ') for part in parts[1:4]):
                raise ShaderGraphError('Invalid amplification dispatch arguments.')
            pointer = self.address(parts[4])
            if pointer is None or pointer.path or pointer.storage['scope']!='group':
                raise ShaderGraphError('Amplification dispatch needs the complete typed shared payload.')
            self.builder.execute('dispatch_mesh',properties={'storage':pointer.storage},
                                 **{pin:self.value(part[4:],'uint') for pin,part in zip(('x','y','z'),parts[1:4])})
            return True
        if record.opcode == 'getelementptr':
            parts = split(re.sub(r'^getelementptr(?: inbounds)? ', '', body))
            pointer = self.address(parts[1]) if len(parts)>2 else None
            if pointer is None:
                return False
            if native_storage_type(parts[0],self.metadata.storage_types)!=pointer.element or parts[2]!='i32 0':
                raise ShaderGraphError('Shared addressing must stay within its declared root allocation.')
            element, path, indices = pointer.element, list(pointer.path), list(pointer.indices)
            for index in parts[3:]:
                if element.kind == 'struct':
                    if not re.fullmatch(r'i32 \d+',index) or not 0<=int(index[4:])<len(element.members):
                        raise ShaderGraphError('Shared structure addressing needs a declared member index.')
                    path.append(int(index[4:]))
                    element = element.members[path[-1]]
                elif element.kind == 'array' and index.startswith('i32 '):
                    path.append(None)
                    indices.append(self.value(index[4:],'uint'))
                    element = element.element
                else:
                    raise ShaderGraphError('Shared addressing does not match its declared aggregate axes.')
            self.pointers[record.result] = SharedPointer(pointer.storage,element,tuple(path),tuple(indices))
            return True
        if record.opcode not in {'load','store'}:
            return False
        parts = split(body[len(record.opcode)+1:])
        pointer = self.address(parts[1]) if len(parts)>1 else None
        if pointer is None:
            return False
        if pointer.element.kind not in {'uint','float','bool'}:
            raise ShaderGraphError('A shared access must identify a scalar member.')
        operands = {f'index{i}':index for i,index in enumerate(pointer.indices)}
        native = {'uint':'i32','float':'float','bool':'i1'}[pointer.element.kind]
        if record.opcode == 'load':
            if parts[0]!=native:
                raise ShaderGraphError('Shared read differs from its typed scalar member.')
            values[record.result] = self.builder.execute('group_read',properties=pointer.properties,**operands)['value']
        else:
            if not parts[0].startswith(native+' '):
                raise ShaderGraphError('Shared write differs from its typed scalar member.')
            self.builder.execute('group_write',properties=pointer.properties,
                value=self.value(parts[0][len(native)+1:],pointer.element.kind),**operands)
        return True
