from dataclasses import dataclass, replace
import re

from file_handlers.sdf.shader_graph import ShaderGraphError, ShaderSignatureField


_READ = re.compile(r"\bv(\d+)\b(?:\[([^\]]+)\])?")


@dataclass(frozen=True)
class StageInputRead:
    carrier: ShaderSignatureField
    native: ShaderSignatureField
    element: int | None


def stage_input_library(contract, texts, array_extents):
    texts = tuple(texts)
    fields = {}
    for field in contract.inputs:
        fields.setdefault(field.register, []).append(field)
    requests = {}
    for text in texts:
        for match in _READ.finditer(text):
            register, element = int(match[1]), match[2]
            if register not in fields or register == 0xFFFFFFFF:
                raise ShaderGraphError("Graph requests an absent ordinary stage input")
            extent = array_extents.get(register)
            if extent is not None:
                if element is None:
                    raise ShaderGraphError("Geometry/tessellation input needs an explicit control-point/primitive index")
                if not element.isdecimal() or not 0 <= int(element) < extent:
                    raise ShaderGraphError("Stage input element index is outside its native declared extent")
                element = int(element)
            elif element is not None:
                raise ShaderGraphError("A scalar stage interface has no element axis")
            requests[(register, element)] = 0
    requests = {key: index for index, key in enumerate(sorted(requests))}
    reads = []
    for (register, element), temporary in requests.items():
        for field in fields[register]:
            carrier = replace(field, semantic_name="REasyIndexedInput", semantic_index=len(reads),
                              system_value="undefined", register=temporary, array_length=1, interpolation="")
            reads.append(StageInputRead(carrier, field, element))

    def rewrite(text):
        def substitute(match):
            key = int(match[1]), int(match[2]) if match[2] is not None else None
            return f"v{requests[key]}"
        return _READ.sub(substitute, text)

    return tuple(reads), tuple(rewrite(text) for text in texts)
