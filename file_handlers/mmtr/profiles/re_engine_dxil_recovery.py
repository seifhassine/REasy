from __future__ import annotations

from dataclasses import dataclass
import json
import re

from file_handlers.sdf.dxc import DxcError, disassemble_dxil
from file_handlers.sdf.shader_graph import ShaderGraphError
lazy from file_handlers.sdf.storage_codec import pack_ir_storage, unpack_ir_storage


IR_SCHEMA = "reasy.dxil-semantic-ir"
IR_VERSION = 1
MAX_IR_JSON_BYTES = 16 * 1024 * 1024
MAX_IR_STORAGE_BYTES = 2 * 1024 * 1024
MAX_IR_LINES = 250_000

LLVM_NAME = r'(?:"(?:[^"\\]|\\.)*"|[-$._A-Za-z0-9]+)'

_ASSIGNMENT = re.compile(r"^(?P<result>%[^=\s]+)\s*=\s*(?P<body>.+)$")
_UNNAMED_LABEL = re.compile(r"^\s*;\s*<label>:(\d+)\b")
_LABEL = re.compile(r"^(?:[A-Za-z$._][A-Za-z0-9$._-]*|\d+):$")
_METADATA = re.compile(r"^![A-Za-z0-9$._-]+\s*=|^!\d+\s*=")
_TYPE = re.compile(r'^(?P<name>%(?:"(?:[^"\\]|\\.)*"|[^\s=]+))\s*=\s*type\b')
_GLOBAL = re.compile(r'^(?P<name>@(?:"(?:[^"\\]|\\.)*"|[^\s=]+))\s*=')
_DEFINE = re.compile(r"^define\s+.+\{\s*$")
_DECLARE = re.compile(r"^declare\s+")
_ATTRIBUTE = re.compile(r"^attributes\s+#\d+\s*=")

_EXECUTABLE_OPCODES = frozenset(
    {
        "add",
        "alloca",
        "and",
        "ashr",
        "bitcast",
        "br",
        "call",
        "extractelement",
        "extractvalue",
        "fadd",
        "fcmp",
        "fdiv",
        "fmul",
        "fpext",
        "fptosi",
        "fptoui",
        "fptrunc",
        "frem",
        "fsub",
        "getelementptr",
        "icmp",
        "insertelement",
        "insertvalue",
        "inttoptr",
        "load",
        "lshr",
        "mul",
        "or",
        "phi",
        "ptrtoint",
        "ret",
        "sdiv",
        "select",
        "sext",
        "shl",
        "sitofp",
        "srem",
        "store",
        "sub",
        "switch",
        "trunc",
        "udiv",
        "uitofp",
        "unreachable",
        "urem",
        "xor",
        "zext",
    }
)
_CALL_PREFIXES = frozenset(("tail", "musttail", "notail"))
_ARITHMETIC_FLAGS = frozenset(
    ("fast", "nnan", "ninf", "nsz", "arcp", "contract", "afn", "reassoc", "nuw", "nsw", "exact")
)


def _strip_comment(line: str) -> str:
    if '"' not in line:
        return line.partition(";")[0].rstrip()
    quoted = False
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quoted:
            escaped = True
            continue
        if character == '"':
            quoted = not quoted
            continue
        if character == ";" and not quoted:
            return line[:index].rstrip()
    return line.rstrip()


def _bracket_delta(text: str) -> int:
    if '"' not in text:
        return text.count("[") - text.count("]")
    quoted = escaped = False
    result = 0
    for character in text:
        if escaped:
            escaped = False
            continue
        if character == "\\" and quoted:
            escaped = True
        elif character == '"':
            quoted = not quoted
        elif not quoted and character == "[":
            result += 1
        elif not quoted and character == "]":
            result -= 1
    return result


def _opcode(body: str) -> str:
    tokens = str(body).split(None, 1)
    if not tokens:
        raise ShaderGraphError("Recovered DXIL instruction is empty.")
    if tokens[0] in _CALL_PREFIXES:
        tokens = tokens[1].split(None, 1) if len(tokens) > 1 else []
    while tokens and tokens[0] in _ARITHMETIC_FLAGS:
        tokens = tokens[1].split(None, 1) if len(tokens) > 1 else []
    if not tokens:
        raise ShaderGraphError("Recovered DXIL instruction has no opcode.")
    return tokens[0].casefold()


@dataclass(frozen=True, slots=True)
class DxilSemanticRecord:
    kind: str
    text: str
    opcode: str = ""
    result: str = ""

    def __post_init__(self) -> None:
        _validate_record(self)

    def to_dict(self) -> dict[str, str]:
        value = {"kind": self.kind, "text": self.text}
        if self.opcode:
            value["opcode"] = self.opcode
        if self.result:
            value["result"] = self.result
        return value

    @classmethod
    def from_dict(cls, value: object) -> "DxilSemanticRecord":
        if not isinstance(value, dict):
            raise ShaderGraphError("Recovered DXIL record is invalid.")
        return cls(
            str(value.get("kind", "")),
            str(value.get("text", "")),
            str(value.get("opcode", "")),
            str(value.get("result", "")),
        )


def _validate_record(record: DxilSemanticRecord, *, _parsed: bool = False) -> None:
    if (
        record.kind not in {"module", "function", "label", "instruction"}
        or not record.text
        or len(record.text) > 2 * 1024 * 1024
        or "\0" in record.text
        or "\r" in record.text
        or "\n" in record.text
    ):
        raise ShaderGraphError("Recovered DXIL record is invalid.")
    if record.kind == "instruction":
        if record.opcode not in _EXECUTABLE_OPCODES:
            raise ShaderGraphError(
                f"Recovered DXIL opcode {record.opcode!r} is not understood."
            )
        if not _parsed:
            match = _ASSIGNMENT.match(record.text)
            expected_result = match.group("result") if match else ""
            body = match.group("body") if match else record.text
            if record.result != expected_result or record.opcode != _opcode(body):
                raise ShaderGraphError("Recovered DXIL instruction record is inconsistent.")
    elif record.opcode or record.result:
        raise ShaderGraphError("Recovered DXIL non-instruction has executable fields.")


@dataclass(frozen=True, slots=True)
class ReEngineDxilSemanticIr:
    records: tuple[DxilSemanticRecord, ...]

    def __post_init__(self) -> None:
        _validate_module_structure(self.records)

    def to_assembly(self) -> str:
        lines = []
        in_function = False
        for record in self.records:
            if record.kind == "function":
                if record.text == "}":
                    in_function = False
                    lines.append("}")
                else:
                    in_function = True
                    lines.append(record.text)
            elif record.kind == "instruction":
                lines.append("  " + record.text)
            elif record.kind == "label":
                # DXC allocates unnamed block slots implicitly; a bare `4:` is a named label, so emit the unnamed-block spelling.
                label = record.text.removesuffix(":")
                lines.append("; <label>:" + label if label.isdigit() else record.text)
            else:
                if in_function:
                    raise ShaderGraphError("Recovered DXIL module record is inside a function.")
                lines.append(record.text)
        return "\n".join(lines) + "\n"

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema": IR_SCHEMA,
                "version": IR_VERSION,
                "records": [item.to_dict() for item in self.records],
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    def to_storage(self) -> str:
        raw = self.to_json().encode("utf-8")
        if len(raw) > MAX_IR_JSON_BYTES:
            raise ShaderGraphError("Recovered DXIL semantics are too large.")
        # Level 6 is much faster than 9; use 9 only when 6 exceeds the size limit.
        stored = pack_ir_storage(raw, level=6)
        if len(stored) > MAX_IR_STORAGE_BYTES:
            compact = pack_ir_storage(raw, level=9)
            if len(compact) < len(stored):
                stored = compact
        return stored

    @classmethod
    def from_json(cls, text: str) -> "ReEngineDxilSemanticIr":
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_IR_JSON_BYTES:
            raise ShaderGraphError("Recovered DXIL semantics are too large.")
        try:
            value = json.loads(text)
            version = int(value.get("version", -1)) if isinstance(value, dict) else -1
        except (TypeError, ValueError, OverflowError) as exc:
            raise ShaderGraphError("Recovered DXIL semantics are invalid.") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema") != IR_SCHEMA
            or version != IR_VERSION
        ):
            raise ShaderGraphError("Recovered DXIL semantics are invalid.")
        rows = value.get("records", [])
        if not isinstance(rows, list) or not rows or len(rows) > MAX_IR_LINES:
            raise ShaderGraphError("Recovered DXIL semantics are invalid or too large.")
        return cls(tuple(DxilSemanticRecord.from_dict(item) for item in rows))

    @classmethod
    def from_storage(cls, text: object) -> "ReEngineDxilSemanticIr":
        return cls.from_json(_unpack_storage(text))


def _unpack_storage(text: object) -> str:
    return unpack_ir_storage(text, MAX_IR_JSON_BYTES, "Recovered DXIL semantics")


def compact_recovered_dxil_storage(text: str) -> str:
    decoded = _unpack_storage(text)
    ReEngineDxilSemanticIr.from_json(decoded)
    compact = pack_ir_storage(decoded.encode("utf-8"), level=9)
    return compact if len(compact) < len(text) else text


def _module_record(line: str) -> DxilSemanticRecord:
    if (
        line.startswith(("target datalayout =", "target triple =", "source_filename ="))
        or _TYPE.match(line)
        or _GLOBAL.match(line)
        or _DECLARE.match(line)
        or _ATTRIBUTE.match(line)
        or _METADATA.match(line)
    ):
        return DxilSemanticRecord("module", line)
    raise ShaderGraphError(f"Recovered DXIL module statement is not understood: {line[:120]}")


def _validate_module_structure(records: tuple[DxilSemanticRecord, ...]) -> None:
    depth = 0
    functions = 0
    for record in records:
        if record.kind == "function":
            if record.text == "}":
                if depth != 1:
                    raise ShaderGraphError("Recovered DXIL function braces are unbalanced.")
                depth = 0
            else:
                if depth or not _DEFINE.match(record.text):
                    raise ShaderGraphError("Recovered DXIL function header is invalid.")
                depth = 1
                functions += 1
        elif record.kind in {"instruction", "label"}:
            if depth != 1:
                raise ShaderGraphError("Recovered DXIL executable record is outside a function.")
        elif depth:
            raise ShaderGraphError("Recovered DXIL module record is inside a function.")
    if depth or functions == 0:
        raise ShaderGraphError(
            "Recovered material DXIL must contain complete function definitions."
        )


def parse_re_engine_dxil_assembly(assembly: str) -> ReEngineDxilSemanticIr:
    if not isinstance(assembly, str) or "\0" in assembly:
        raise ShaderGraphError("Recovered DXIL assembly is invalid.")
    records: list[DxilSemanticRecord] = []
    in_function = False
    pending_instruction = ""
    pending_brackets = 0

    def instruction(line: str) -> None:
        match = _ASSIGNMENT.match(line)
        body = match.group("body") if match else line
        record = object.__new__(DxilSemanticRecord)
        object.__setattr__(record, "kind", "instruction")
        object.__setattr__(record, "text", line)
        object.__setattr__(record, "opcode", _opcode(body))
        object.__setattr__(record, "result", match.group("result") if match else "")
        _validate_record(record, _parsed=True)
        records.append(record)

    for source_line in assembly.splitlines():
        # LLVM prints unnamed block declarations as comments; keep them as block identities.
        unnamed_label = (
            _UNNAMED_LABEL.match(source_line)
            if in_function and not pending_instruction and source_line.lstrip().startswith(";")
            else None
        )
        if unnamed_label:
            records.append(DxilSemanticRecord("label", unnamed_label[1] + ":"))
            continue
        line = _strip_comment(source_line).strip()
        if not line:
            continue
        if pending_instruction:
            pending_instruction += " " + line
            pending_brackets += _bracket_delta(line)
            if pending_brackets < 0:
                raise ShaderGraphError("Recovered DXIL instruction brackets are unbalanced.")
            if pending_brackets == 0:
                instruction(pending_instruction)
                pending_instruction = ""
            continue
        if line.startswith("define") and _DEFINE.match(line):
            if in_function:
                raise ShaderGraphError("Recovered DXIL contains nested functions.")
            records.append(DxilSemanticRecord("function", line))
            in_function = True
            continue
        if in_function and line == "}":
            records.append(DxilSemanticRecord("function", line))
            in_function = False
            continue
        if in_function:
            if line.endswith(":") and _LABEL.match(line):
                records.append(DxilSemanticRecord("label", line))
                continue
            pending_brackets = _bracket_delta(line)
            if pending_brackets < 0:
                raise ShaderGraphError("Recovered DXIL instruction brackets are unbalanced.")
            if pending_brackets:
                pending_instruction = line
            else:
                instruction(line)
            continue
        records.append(_module_record(line))
    if pending_instruction:
        raise ShaderGraphError("Recovered DXIL instruction is incomplete.")
    return ReEngineDxilSemanticIr(tuple(records))


def recover_re_engine_dxil_ir(bytecode: bytes) -> ReEngineDxilSemanticIr:
    try:
        assembly = disassemble_dxil(bytecode)
    except DxcError as exc:
        raise ShaderGraphError(f"DXC could not disassemble the material shader: {exc}") from exc
    return parse_re_engine_dxil_assembly(assembly)



__all__ = [
    "DxilSemanticRecord",
    "ReEngineDxilSemanticIr",
    "compact_recovered_dxil_storage",
    "parse_re_engine_dxil_assembly",
    "recover_re_engine_dxil_ir",
]
