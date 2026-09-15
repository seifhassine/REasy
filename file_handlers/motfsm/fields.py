"""Fixed-size edits against the byte spans produced by the readers."""
from dataclasses import dataclass, field
import math
import struct


SCALAR_FORMATS = {
    "u8": "B", "s8": "b", "u16": "H", "s16": "h",
    "u32": "I", "s32": "i", "u64": "Q", "s64": "q",
    "f32": "f", "f64": "d", "bool": "?",
}


@dataclass
class FieldBinding:
    name: str
    type_name: str
    owner: object
    attribute: str | int
    offset: int
    original_bytes: bytes
    layout_mask: int = 0
    original_value: object = field(init=False)

    def __post_init__(self):
        self.original_value = self.value
        if self.type_name not in SCALAR_FORMATS:
            raise ValueError(f"Unsupported fixed-size field type: {self.type_name}")
        if len(self.original_bytes) != struct.calcsize("<" + SCALAR_FORMATS[self.type_name]):
            raise ValueError(f"Invalid byte span for {self.name}")

    @property
    def size(self):
        return len(self.original_bytes)

    @property
    def value(self):
        if isinstance(self.attribute, int):
            return self.owner[self.attribute]
        return getattr(self.owner, self.attribute)

    def parse(self, text):
        text = str(text).strip()
        if self.type_name == "bool":
            if text.lower() not in ("true", "false"):
                raise ValueError("Expected True or False")
            return text.lower() == "true"
        if self.type_name in ("f32", "f64"):
            return float(text)
        return int(text, 16 if text.lower().startswith(("0x", "-0x")) else 10)

    def encode(self, value):
        if value == self.original_value or (
            isinstance(value, float) and isinstance(self.original_value, float)
            and math.isnan(value) and math.isnan(self.original_value)
        ):
            return self.original_bytes
        try:
            if self.layout_mask and (value ^ self.original_value) & self.layout_mask:
                raise ValueError("Changing FSM/reference-tree layout requires rebuilding the node")
            return struct.pack("<" + SCALAR_FORMATS[self.type_name], value)
        except (struct.error, OverflowError, TypeError) as exc:
            raise ValueError(f"Invalid {self.type_name} value for {self.name}: {value}") from exc

    def set_value(self, value):
        encoded = self.encode(value)  # Validate before changing the document.
        value = struct.unpack("<" + SCALAR_FORMATS[self.type_name], encoded)[0]
        if isinstance(self.attribute, int):
            self.owner[self.attribute] = value
        else:
            setattr(self.owner, self.attribute, value)

    @property
    def modified(self):
        return self.encode(self.value) != self.original_bytes


class FieldBindings:
    def __init__(self, source):
        self.source = source
        self._fields = {}

    def bind(self, owner, attribute, offset, type_name, *, name=None, layout_mask=0):
        size = struct.calcsize("<" + SCALAR_FORMATS[type_name])
        if offset < 0 or offset + size > len(self.source):
            raise ValueError(f"Field outside file: {name or attribute} at 0x{offset:X}")
        key = (id(owner), attribute)
        if key in self._fields:
            binding = self._fields[key]
            if binding.offset != offset or binding.type_name != type_name:
                raise ValueError(f"Conflicting byte spans for {name or attribute}")
            return binding
        binding = FieldBinding(
            name or str(attribute), type_name, owner, attribute, offset,
            self.source[offset:offset + size], layout_mask,
        )
        self._fields[key] = binding
        return binding

    def get(self, owner, attribute):
        return self._fields.get((id(owner), attribute))

    @property
    def modified(self):
        return any(binding.modified for binding in self._fields.values())

    def rebuild(self):
        output = bytearray(self.source)
        for binding in self._fields.values():
            encoded = binding.encode(binding.value)
            if encoded != binding.original_bytes:
                output[binding.offset:binding.offset + binding.size] = encoded
        return bytes(output)

    def accept_changes(self, source):
        self.source = source
        for binding in self._fields.values():
            binding.original_value = binding.value
            binding.original_bytes = source[binding.offset:binding.offset + binding.size]
