from dataclasses import dataclass
import math
import re

from file_handlers.sdf.dxil_metadata import metadata_operands
from file_handlers.sdf.shader_graph import ShaderGraphError
from file_handlers.sdf.shader_graph_indexed_value import MAX_INDEXED_VALUES
from .profiles.re_engine_dxil_recovery import LLVM_NAME


_LIFETIME = re.compile(r'call void @llvm\.lifetime\.(start|end)(?:\.p0i8)?\(i64 (-?\d+), i8\* (.+)\)(?: #\d+)?(?:, !.*)?')
_SYMBOL = '[%@]' + LLVM_NAME


def split(text):
    return tuple(metadata_operands('!{' + text + '}'))


@dataclass(frozen=True)
class ArrayType:
    scalar: str
    dimensions: tuple[int, ...] = ()

    @classmethod
    def parse(cls, text):
        dimensions = []
        while (array := re.fullmatch(r'\[(\d+) x (.+)\]', text)) is not None:
            dimensions.append(int(array[1]))
            text = array[2]
        if text not in {'float', 'half', 'i32', 'i16', 'i8', 'i1'} or any(n <= 0 for n in dimensions) or math.prod(dimensions) > MAX_INDEXED_VALUES:
            raise ShaderGraphError('This invocation-local aggregate needs an additional semantic type adapter.')
        return cls(text, tuple(dimensions))

    @property
    def count(self):
        return math.prod(self.dimensions)

    @property
    def text(self):
        text = self.scalar
        for n in reversed(self.dimensions):
            text = f'[{n} x {text}]'
        return text

    def literals(self, text):
        if not text.startswith(self.text + ' '):
            raise ShaderGraphError('The constant aggregate disagrees with its declared type.')
        body = text[len(self.text)+1:]
        if body == 'undef':
            return ['undef'] * self.count
        if body == 'zeroinitializer':
            return ['false' if self.scalar == 'i1' else '0xH0000' if self.scalar == 'half' else '0'] * self.count
        if not self.dimensions:
            return [body]
        if not body.startswith('[') or not body.endswith(']'):
            raise ShaderGraphError('The constant array needs an explicit aggregate initializer.')
        members = split(body[1:-1])
        if len(members) != self.dimensions[0]:
            raise ShaderGraphError('A constant array has the wrong declared element count.')
        child = ArrayType(self.scalar, self.dimensions[1:])
        return [value for member in members for value in child.literals(member)]


@dataclass(frozen=True)
class Pointer:
    allocation: str
    element: ArrayType
    index: object


@dataclass(frozen=True)
class LifetimePointer:
    allocation: str
    first_cell: int


class LocalArrays:
    def __init__(self, ir, builder, value, half_type):
        self.builder, self.value = builder, value
        self.domains = {'float':'float', 'half':half_type, 'i32':'uint', 'i16':'uint', 'i8':'uint', 'i1':'bool'}
        self.allocations, self.globals, self.pointers, self.initial_cells = {}, {}, {}, {}
        for record in ir.records:
            if record.kind != 'module' or not record.text.startswith('@'):
                continue
            match = re.fullmatch(r'(' + _SYMBOL + r') = (?:(internal|private) )?(?:unnamed_addr )?(constant|global) (.+)', record.text)
            if match is None:
                continue  # Other address spaces are not invocation-local data.
            if match[3]=='global' and match[2] is None:
                continue
            initializer = split(match[4])[0]
            depth, end = 0, None
            for index, char in enumerate(initializer):
                depth += (char == '[') - (char == ']')
                if char == ' ' and depth == 0:
                    end = index
                    break
            if end is None:
                continue
            element = ArrayType.parse(initializer[:end])
            self.allocations[match[1]] = element
            cells = tuple(value(literal, self.domains[element.scalar]) for literal in element.literals(initializer))
            if match[3]=='constant':
                self.globals[match[1]] = cells
            else:
                # AS0 internal globals are thread-private; the initializer runs once per invocation.
                self.initial_cells.update(zip(self.cell_keys(match[1]),cells))

    @staticmethod
    def is_cell(key):
        return isinstance(key, str) and key.startswith('local:')

    def cell_keys(self, allocation):
        return tuple(f'local:{allocation}:{i}' for i in range(self.allocations[allocation].count))

    def address(self, text):
        inline = re.fullmatch(r'(.+)\* getelementptr(?: inbounds)? \((.+)\)',text)
        if inline:
            parts = split(inline[2])
            if any(not re.fullmatch(r'i32 -?\d+',index) for index in parts[2:]):
                raise ShaderGraphError('A constant pointer expression requires constant indices.')
            pointer = self.element_address(parts)
            if pointer.element != ArrayType.parse(inline[1]):
                raise ShaderGraphError('The constant address result differs from its declared pointee.')
            return pointer
        match = re.fullmatch(r'(.+)\* (' + _SYMBOL + r')', text)
        if match is None:
            raise ShaderGraphError('This memory access requires an address-space/pointer adapter.')
        element, name = ArrayType.parse(match[1]), match[2]
        pointer = self.pointers.get(name)
        if pointer is None and name in self.allocations:
            pointer = Pointer(name, self.allocations[name], self.builder.integer(0))
        if not isinstance(pointer, Pointer) or pointer.element != element:
            raise ShaderGraphError('The local pointer has no matching declared pointee.')
        return pointer

    def element_address(self, parts):
        element, pointer = ArrayType.parse(parts[0]), self.address(parts[1])
        if element != pointer.element or not 1 <= len(parts[2:]) <= 1 + len(element.dimensions):
            raise ShaderGraphError('The element address disagrees with the declared array dimensions.')
        builder, offset = self.builder, pointer.index
        for axis, argument in enumerate(parts[2:]):
            if not argument.startswith('i32 '):
                raise ShaderGraphError('This pointer index needs an additional integer-width adapter.')
            index = self.value(argument[4:], 'uint')
            scale = math.prod(element.dimensions[axis:])
            node = builder.graph.node(index.node)
            if node.kind == 'constant' and node.properties['values'] == [0]:
                continue
            if node.kind == 'constant':
                index = builder.integer(int(node.properties['values'][0]) * scale)
            elif scale != 1:
                index = builder.operation('multiply', a=index, b=builder.integer(scale))
            base = builder.graph.node(offset.node)
            index_node = builder.graph.node(index.node)
            if base.kind == index_node.kind == 'constant':
                offset = builder.integer(int(base.properties['values'][0]) + int(index_node.properties['values'][0]))
            else:
                offset = index if base.kind == 'constant' and base.properties['values'] == [0] else builder.operation('add', a=offset, b=index)
        remaining = element.dimensions[len(parts)-3:]
        return Pointer(pointer.allocation, ArrayType(element.scalar, remaining), offset)

    def apply(self, record, body, values):
        builder = self.builder
        if record.opcode == 'bitcast' and (cast := re.fullmatch(r'bitcast (.+) to (float|i32)\*',body)):
            pointer = self.address(cast[1])
            if pointer.element.dimensions or pointer.element.scalar not in {'float','i32'}:
                raise ShaderGraphError('A local pointer reinterpretation must retain its 32-bit scalar extent.')
            self.pointers[record.result] = Pointer(pointer.allocation,ArrayType(cast[2]),pointer.index)
            return True
        if record.opcode == 'bitcast' and (cast := re.fullmatch(r'bitcast (.+) to i8\*', body)):
            pointer = self.address(cast[1])
            index = builder.graph.node(pointer.index.node)
            if index.kind != 'constant':
                raise ShaderGraphError('An allocation-lifetime address must identify a declared subobject.')
            self.pointers[record.result] = LifetimePointer(pointer.allocation, int(index.properties['values'][0]))
            return True
        lifetime = _LIFETIME.fullmatch(body)
        if lifetime:
            pointer = self.pointers.get(lifetime[3])
            if cast := re.fullmatch(r'bitcast \((.+) to i8\*\)', lifetime[3]):
                original = self.address(cast[1])
                index = builder.graph.node(original.index.node)
                if index.kind != 'constant':
                    raise ShaderGraphError('An allocation-lifetime address must identify a declared subobject.')
                pointer = LifetimePointer(original.allocation, int(index.properties['values'][0]))
            if not isinstance(pointer, LifetimePointer):
                raise ShaderGraphError('The lifetime marker has no invocation-local allocation address.')
            element = self.allocations[pointer.allocation]
            width = {'float':4, 'half':2, 'i32':4, 'i16':2, 'i8':1, 'i1':1}[element.scalar]
            size = int(lifetime[2])
            if size == -1:
                size = (element.count - pointer.first_cell) * width
            if size < 0 or size % width or not 0 <= pointer.first_cell <= element.count or size // width > element.count-pointer.first_cell:
                raise ShaderGraphError('The lifetime marker exceeds its allocation or splits a scalar cell.')
            if pointer.allocation in self.globals:
                # DXC HoistConstantArray replaces initialized allocas with an immutable global.
                return True
            for key in self.cell_keys(pointer.allocation)[pointer.first_cell:pointer.first_cell+size//width]:
                if lifetime[1]=='start':
                    values[key] = builder.unassigned(self.domains[element.scalar])
                else:
                    values.pop(key, None)
            return True
        if record.opcode == 'alloca':
            parts = split(body.removeprefix('alloca '))
            if len(parts) != 2 or not re.fullmatch(r'align \d+', parts[1]):
                raise ShaderGraphError('This allocation needs a dynamic extent/alignment adapter.')
            self.allocations[record.result] = ArrayType.parse(parts[0])
            for key in self.cell_keys(record.result):
                # An alloca is undef, not zero.
                values[key] = builder.unassigned(self.domains[self.allocations[record.result].scalar])
            return True
        if record.opcode == 'getelementptr':
            parts = split(body.removeprefix('getelementptr ').removeprefix('inbounds '))
            self.pointers[record.result] = self.element_address(parts)
            return True
        if record.opcode not in {'load', 'store'}:
            return False
        load = record.opcode == 'load'
        parts = split(body.removeprefix(record.opcode + ' '))
        if len(parts) < 3 or not re.fullmatch(r'align \d+', parts[2]) or any(not p.startswith('!') for p in parts[3:]):
            raise ShaderGraphError('This memory operation needs volatile/atomic/address-space semantics.')
        pointer = self.address(parts[1])
        scalar = parts[0] if load else parts[0].split(' ', 1)[0]
        if pointer.element != ArrayType(scalar):
            raise ShaderGraphError('A local load/store requires its declared scalar element.')
        keys = self.cell_keys(pointer.allocation)
        index = builder.graph.node(pointer.index.node)
        fixed = int(index.properties['values'][0]) if index.kind == 'constant' else None
        if fixed is not None and not 0 <= fixed < len(keys):
            raise ShaderGraphError('A constant local array index is outside its declared extent.')
        if load:
            cells = self.globals.get(pointer.allocation) or tuple(values.get(key) for key in keys)
            selected = [cells[fixed]] if fixed is not None else cells
            if any(cell is None for cell in selected):
                raise ShaderGraphError('An invocation-local read can reach a cell outside its active lifetime.')
            result = selected[0] if fixed is not None else builder.array_element(pointer.index, selected)
            values[record.result] = builder.convert(result,self.domains[scalar],bits=True)
        else:
            if pointer.allocation in self.globals:
                raise ShaderGraphError('A constant array cannot be written.')
            incoming = self.value(parts[0][len(scalar)+1:], self.domains[scalar])
            incoming = builder.convert(incoming,self.domains[self.allocations[pointer.allocation].scalar],bits=True)
            if fixed is not None:
                values[keys[fixed]] = incoming
            else:
                if any(key not in values for key in keys):
                    raise ShaderGraphError('A dynamic local write can reach a cell outside its active lifetime.')
                for i,key in enumerate(keys):
                    condition = builder.compare('equal', pointer.index, builder.integer(i))
                    values[key] = builder.select(condition, incoming, values[key])
        return True


class LocalValueState(dict):
    def __init__(self, values=(), **kwargs):
        fresh = not hasattr(self, '_local')
        dict.__init__(self, values, **kwargs)
        self._local = (dict.fromkeys(values._local)
                       if fresh and type(values) is LocalValueState and not kwargs else
                       {key: None for key in self if LocalArrays.is_cell(key)})

    def __setitem__(self, key, value):
        dict.__setitem__(self, key, value)
        if LocalArrays.is_cell(key):
            self._local[key] = None

    def __delitem__(self, key):
        dict.__delitem__(self, key)
        self._local.pop(key, None)

    def copy(self):
        return type(self)(self)

    def update(self, values=(), **kwargs):
        source = (values.items() if isinstance(values, dict) else
                  ((key, values[key]) for key in values.keys()) if hasattr(values, 'keys') else values)
        for key, value in source:
            self[key] = value
        for key, value in kwargs.items():
            self[key] = value

    def pop(self, key, *default):
        result = dict.pop(self, key, *default)
        self._local.pop(key, None)
        return result

    def popitem(self):
        key, value = dict.popitem(self)
        self._local.pop(key, None)
        return key, value

    def clear(self):
        dict.clear(self)
        self._local.clear()

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def __ior__(self, other):
        self.update(other)
        return self

    def local_items(self):
        return ((key, self[key]) for key in self._local)

    def clear_local_cells(self):
        for key in self._local:
            dict.__delitem__(self, key)
        self._local.clear()

    def nonlocal_copy(self):
        result = self.copy()
        result.clear_local_cells()
        return result
