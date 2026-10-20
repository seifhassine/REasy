from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
import json
from functools import reduce
from operator import or_
import struct

from .spirv import SPIRV_MAGIC, SpirvError, _spirv_module
lazy from .storage_codec import pack_ir_storage, unpack_ir_storage
from tools.shader_tools import prompt_shader_tool_download, shader_tool_directory


MAX_STORAGE = 32 * 1024 * 1024


@lru_cache(maxsize=1)
def grammar():
    root = shader_tool_directory("spirv_tools")
    files = [root / name for name in ("spirv.core.grammar.json", "extinst.glsl.std.450.grammar.json")]
    if not all(path.is_file() for path in files) and not prompt_shader_tool_download("spirv_tools"):
        raise SpirvError(f"SPIR-V grammars are required in {root}. "
                         "Open the file in REasy to download them, or install the pinned "
                         "SPIRV-Headers grammars described in tools/shader_tools.md.")
    core, extension = (json.loads(path.read_text(encoding="utf-8")) for path in files)
    return (
        {row["opcode"]: row for row in core["instructions"]},
        {row["kind"]: row for row in core["operand_kinds"]},
        {row["opcode"]: row for row in extension["instructions"]},
    )


@dataclass(frozen=True, slots=True)
class Operand:
    kind: str
    value: int | str
    width: int = 32

    @property
    def is_id(self):
        return self.kind.startswith("Id")

    def words(self):
        if self.kind == "LiteralString":
            raw = str(self.value).encode("utf-8") + b"\0"
            raw += b"\0" * (-len(raw) % 4)
            return struct.unpack(f"<{len(raw) // 4}I", raw)
        number = int(self.value)
        if self.width not in {32, 64} or not 0 <= number < 1 << self.width:
            raise SpirvError(f"Invalid {self.kind} literal width/value")
        return tuple((number >> bit) & 0xFFFFFFFF for bit in range(0, self.width, 32))


@dataclass(frozen=True, slots=True)
class Instruction:
    opcode: int
    operands: tuple[Operand, ...]

    @property
    def name(self):
        return grammar()[0][self.opcode]["opname"]

    @property
    def values(self):
        return tuple(item.value for item in self.operands)

    @property
    def result(self):
        return next((int(v.value) for v in self.operands if v.kind == "IdResult"), None)

    @property
    def type_id(self):
        return next((int(v.value) for v in self.operands if v.kind == "IdResultType"), None)

    def remap(self, ids):
        return replace(self, operands=tuple(replace(v, value=ids.get(v.value, v.value)) if v.is_id else v for v in self.operands))

    def words(self):
        words = tuple(word for operand in self.operands for word in operand.words())
        if len(words) >= 0xFFFF:
            raise SpirvError("SPIR-V instruction exceeds its word-count field")
        return ((len(words) + 1) << 16 | self.opcode, *words)


class _Decoder:
    def __init__(self):
        self.definitions = {}
        self.result_types = {}
        self.extensions = {}

    def instruction(self, opcode, words):
        opcodes, kinds, glsl = grammar()
        schema = opcodes.get(opcode)
        if schema is None:
            raise SpirvError(f"Unknown SPIR-V opcode {opcode}")
        cursor = 0
        decoded = []
        literal_width = 32
        if opcode in {43, 50}:
            type_row = self.definitions.get(words[0])
            if type_row is None or type_row.opcode not in {21, 22}:
                raise SpirvError("SPIR-V scalar constant has no integer/float type")
            literal_width = max(32, int(type_row.values[1]))
        elif opcode == 251:  # OpSwitch: case literals have the selector's integer type.
            type_row = self.definitions.get(self.result_types.get(words[0]))
            if type_row is None or type_row.opcode != 21:
                raise SpirvError("SPIR-V switch selector has no integer type")
            literal_width = max(32, int(type_row.values[1]))

        def scalar(kind, width=32):
            nonlocal cursor
            count = width // 32
            if width not in {32, 64} or cursor + count > len(words):
                raise SpirvError(f"Truncated {schema['opname']} operand {kind}")
            value = sum(words[cursor + i] << (32 * i) for i in range(count))
            cursor += count
            decoded.append(Operand(kind, value, width))
            return value

        def operand(kind):
            nonlocal cursor
            if kind == "LiteralString":
                raw = bytearray()
                while cursor < len(words):
                    block = struct.pack("<I", words[cursor]); cursor += 1
                    if b"\0" in block:
                        end = block.index(0)
                        if any(block[end:]):
                            raise SpirvError("SPIR-V literal string has nonzero padding")
                        raw.extend(block[:end])
                        try:
                            decoded.append(Operand(kind, raw.decode("utf-8")))
                        except UnicodeDecodeError as exc:
                            raise SpirvError("SPIR-V literal is not UTF-8") from exc
                        return
                    raw.extend(block)
                raise SpirvError("Unterminated SPIR-V literal string")
            description = kinds.get(kind)
            if description is None:
                raise SpirvError(f"Unknown SPIR-V operand kind {kind}")
            category = description["category"]
            if category == "Composite":
                for child in description["bases"]:
                    if kind == "PairLiteralIntegerIdRef" and opcode == 251 and child == "LiteralInteger":
                        scalar(child, literal_width)
                    else:
                        operand(child)
                return
            width = literal_width if kind == "LiteralContextDependentNumber" else 32
            value = scalar(kind, width)
            if category == "Id" and value == 0:
                raise SpirvError(f"{schema['opname']} uses the reserved ID zero")
            if category not in {"BitEnum", "ValueEnum"}:
                return
            enums = {}
            for row in description["enumerants"]:
                number = int(row["value"], 0) if isinstance(row["value"], str) else row["value"]
                enums.setdefault(number, row)
            selected = [value] if category == "ValueEnum" else [bit for bit in sorted(enums) if bit and value & bit]
            if category == "ValueEnum" and value not in enums or category == "BitEnum" and value & ~reduce(or_, enums, 0):
                raise SpirvError(f"Unknown {kind} value {value}")
            for number in selected:
                sequence(enums[number].get("parameters", ()))

        def sequence(specs):
            for spec in specs:
                quantifier = spec.get("quantifier", "")
                if quantifier == "*":
                    while cursor < len(words):
                        operand(spec["kind"])
                elif quantifier != "?" or cursor < len(words):
                    operand(spec["kind"])

        specs = schema.get("operands", ())
        if opcode == 12:
            if len(words) < 4 or self.extensions.get(words[2]) != "GLSL.std.450" or words[3] not in glsl:
                raise SpirvError("SPIR-V extended instruction set/operation is not supported")
            specs = (*specs[:4], *glsl[words[3]].get("operands", ()))
        elif opcode == 52:  # OpSpecConstantOp has the embedded operation's operands.
            operation = opcodes.get(words[2]) if len(words) >= 3 else None
            if operation is None or [v["kind"] for v in operation.get("operands", ())[:2]] != ["IdResultType", "IdResult"]:
                raise SpirvError("SPIR-V specialization operation has no typed result")
            specs = (*specs[:3], *operation["operands"][2:])
        sequence(specs)
        if cursor != len(words):
            raise SpirvError(f"{schema['opname']} has excess operands")
        row = Instruction(opcode, tuple(decoded))
        if row.result is not None:
            if row.result in self.definitions:
                raise SpirvError(f"SPIR-V ID {row.result} is defined twice")
            self.definitions[row.result] = row
            if row.type_id is not None:
                self.result_types[row.result] = row.type_id
        if opcode == 11:
            self.extensions[row.values[0]] = row.values[1]
        return row


class SpirvBuilder:
    def __init__(self, ir):
        self.decoder = _Decoder()
        for row in ir.instructions:
            self.decoder.instruction(row.opcode, row.words()[1:])
        self.next_id = max(self.decoder.definitions, default=0) + 1

    def id(self):
        result = self.next_id
        self.next_id += 1
        return result

    def instruction(self, opcode, *values):
        words = tuple(word for value in values for word in (
            Operand("LiteralString", value).words() if isinstance(value, str) else (int(value),)
        ))
        return self.decoder.instruction(opcode, words)


@dataclass(frozen=True, slots=True)
class SpirvSemanticIr:
    version: int
    instructions: tuple[Instruction, ...]

    @classmethod
    def from_bytes(cls, raw):
        if len(raw) > MAX_STORAGE:
            raise SpirvError("Invalid SPIR-V module size")
        raw, (_magic, version, _generator, bound, _schema) = _spirv_module(raw)
        if version not in range(0x10000, 0x10700, 0x100):
            raise SpirvError("Invalid SPIR-V module header")
        words = struct.unpack(f"<{len(raw) // 4}I", raw)
        decoder = _Decoder()
        rows, cursor = [], 5
        while cursor < len(words):
            count, opcode = words[cursor] >> 16, words[cursor] & 0xFFFF
            if not count or cursor + count > len(words):
                raise SpirvError("Truncated SPIR-V instruction")
            rows.append(decoder.instruction(opcode, words[cursor + 1:cursor + count]))
            cursor += count
        references = {v.value for row in rows for v in row.operands if v.is_id}
        if any(value >= bound or value not in decoder.definitions for value in references):
            raise SpirvError("SPIR-V ID is undefined or outside the module bound")
        return cls(version, tuple(rows))

    def write(self):
        ids = [int(v.value) for row in self.instructions for v in row.operands if v.is_id]
        words = (SPIRV_MAGIC, self.version, 0, max(ids, default=0) + 1, 0,
                 *(word for row in self.instructions for word in row.words()))
        return struct.pack(f"<{len(words)}I", *words)

    def to_json(self):
        text = json.dumps({"schema": "reasy.spirv-semantic-ir", "version": self.version,
                          "instructions": [[row.opcode, [[v.kind, v.value, v.width] for v in row.operands]] for row in self.instructions]},
                         separators=(",", ":"))
        if len(text.encode('utf-8')) > MAX_STORAGE:
            raise SpirvError("SPIR-V semantic project is too large")
        return text

    def to_storage(self):
        return pack_ir_storage(self.to_json().encode('utf-8'), level=9)

    @classmethod
    def from_storage(cls, text):
        if not isinstance(text, str) or len(text) > MAX_STORAGE:
            raise SpirvError("Invalid SPIR-V semantic project")
        try:
            value = json.loads(unpack_ir_storage(text, MAX_STORAGE, "SPIR-V semantic project data"))
            if value["schema"] != "reasy.spirv-semantic-ir":
                raise SpirvError("Invalid SPIR-V project schema")
            result = cls(int(value["version"]), tuple(Instruction(int(row[0]), tuple(Operand(*v) for v in row[1])) for row in value["instructions"]))
            checked = cls.from_bytes(result.write())
            if checked != result:
                raise SpirvError("SPIR-V project operand roles disagree with the specification")
            return result
        except (ValueError, KeyError, TypeError, IndexError, struct.error) as exc:
            raise SpirvError(f"Invalid SPIR-V semantic project: {exc}") from exc
