from dataclasses import dataclass
import re


def structured_buffer_type(stride, *, writable=False, word_bits=32):
    # D3D structured elements have a maximum 2048-byte stride.
    if type(word_bits) is not int or word_bits not in (32, 64):
        raise ValueError('Structured word storage requires 32- or 64-bit words.')
    if type(stride) is not int or not word_bits // 8 <= stride <= 2048 or stride % (word_bits // 8):
        raise ValueError('A structured stride must be word-aligned and at most 2048 bytes.')
    return f"{'rw_' if writable else ''}structured_buffer<{stride}{',64' if word_bits == 64 else ''}>"


def structured_buffer_stride(value_type):
    match = re.fullmatch(r"(?:rw_)?structured_buffer<(\d+)(,64)?>", str(value_type))
    if match is None:
        return None
    stride = int(match[1])
    if structured_buffer_type(stride, writable=value_type.startswith('rw_'), word_bits=64 if match[2] else 32) != value_type:
        raise ValueError("Invalid structured-buffer object type.")
    return stride


def structured_word_bits(value_type):
    if structured_buffer_stride(value_type) is None:
        raise ValueError('The resource is not a structured word buffer.')
    return 64 if value_type.endswith(',64>') else 32


def structured_layout(stride, *, word_bits=32):
    words = stride // (word_bits // 8)
    return words // 4, words % 4


def structured_word_member(stride, word, *, word_bits=32):
    full, tail = structured_layout(stride, word_bits=word_bits)
    slot, component = divmod(int(word), 4)
    if slot < full:
        return f"REasySlots[{slot}].{'xyzw'[component]}"
    return 'REasyTail' + (f".{'xyzw'[component]}" if tail > 1 else '')


def structured_slot_member(stride, slot):
    full, _tail = structured_layout(stride)
    return f'REasySlots[{int(slot)}]' if slot < full else 'REasyTail'


def structured_dynamic_word(stride, word):
    full, tail = structured_layout(stride)
    return f'REasySlots[({word}) >> 2][({word}) & 3u]' if full and not tail else None


def structured_storage(stride, *, word_bits=32):
    structured_buffer_type(stride, word_bits=word_bits)
    name = f"REasyStructuredWords{stride}" + ('_64' if word_bits == 64 else '')
    full, tail = structured_layout(stride, word_bits=word_bits)
    scalar = 'uint' if word_bits == 32 else 'uint64_t'

    def vector(width):
        if width == 1:
            return scalar
        return f'uint{width}' if word_bits == 32 else f'vector<uint64_t,{width}>'

    fields = []
    if full:
        fields.append(f"    {vector(4)} REasySlots[{full}];")
    if tail:
        fields.append(f"    {vector(tail)} REasyTail;")
    return name, "\n".join((f"#ifndef {name}_DEFINED", f"#define {name}_DEFINED", f"struct {name}", "{", *fields, "};", "#endif"))


@dataclass(frozen=True)
class BufferRead:
    stride: int = 0
    byte_offset: int = 0
    dynamic_offset: bool = False
    words: int = 1

    def __post_init__(self):
        if type(self.stride) is not int or type(self.byte_offset) is not int:
            raise ValueError("Buffer addresses and strides must be integers.")
        if type(self.words) is not int or not 1 <= self.words <= 4:
            raise ValueError('A multi-word read loads two to four consecutive words.')
        if self.words > 1 and self.stride and (self.dynamic_offset or self.byte_offset % 16 or self.words != min(
                4, (self.stride - self.byte_offset) // 4)):
            raise ValueError('A multi-word record read loads one whole slot of the record.')
        if type(self.dynamic_offset) is not bool or self.dynamic_offset and (not self.stride or self.byte_offset):
            raise ValueError('A dynamic record offset needs a structured buffer and an explicit offset input, not a fixed field.')
        if self.stride:
            structured_buffer_type(self.stride)
            if self.byte_offset < 0 or self.byte_offset + 4 > self.stride or self.byte_offset % 4:
                raise ValueError("The record word is outside its declared stride or is unaligned.")
        elif self.byte_offset:
            raise ValueError("Byte-address buffer offsets come from the explicit address input.")

    @classmethod
    def from_properties(cls, properties):
        return cls(properties.get("stride", 0), properties.get("byte_offset", 0), properties.get('dynamic_offset', False),
                   properties.get('words', 1))

    @property
    def value_type(self):
        return 'uint' if self.words == 1 else f'uint{self.words}'

    @property
    def object_type(self):
        return structured_buffer_type(self.stride) if self.stride else "byte_address_buffer"

    @property
    def inputs(self):
        return (("buffer", self.object_type), ("index" if self.stride else "byte_offset", "uint"),
                *((('record_byte_offset', 'uint'),) if self.dynamic_offset else ()))

    @property
    def helper_name(self):
        return f'REasyReadStructuredWord{self.stride}'

    def helpers(self):
        if not self.dynamic_offset:
            return ''
        element, declaration = structured_storage(self.stride)
        member = structured_dynamic_word(self.stride, 'word')
        lines = [f'#ifndef {self.helper_name}_DEFINED', f'#define {self.helper_name}_DEFINED', declaration,
                 f'uint {self.helper_name}(StructuredBuffer<{element}> resource, uint index, uint byteOffset)', '{',
                 '    uint word = byteOffset >> 2;']
        if member is not None:
            lines.append(f'    return resource[index].{member};')
        else:
            full, tail = structured_layout(self.stride)
            start = full * 4
            value = 'resource[index].REasyTail' + (f'[word - {start}u]' if tail > 1 else '')
            if full:
                value = f'word < {start}u ? resource[index].REasySlots[word >> 2][word & 3u] : {value}'
            lines.append(f'    return word < {start + tail}u ? {value} : 0u;')
        lines.extend(('}', '#endif'))
        return '\n'.join(lines)

    def expression(self, incoming):
        resource = incoming("buffer", self.object_type)
        if not self.stride:
            return f"{resource}.Load{self.words if self.words > 1 else ''}({incoming('byte_offset', 'uint')})"
        if self.dynamic_offset:
            return f"{self.helper_name}({resource}, {incoming('index', 'uint')}, {incoming('record_byte_offset', 'uint')})"
        if self.words > 1:
            return f"{resource}[{incoming('index', 'uint')}].{structured_slot_member(self.stride, self.byte_offset // 16)}"
        return f"{resource}[{incoming('index', 'uint')}].{structured_word_member(self.stride, self.byte_offset // 4)}"
