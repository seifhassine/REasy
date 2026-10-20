import re

lazy from file_handlers.sdf.shader_graph import ShaderGraphError
lazy from .profiles.re_engine_dxil_recovery import DxilSemanticRecord
lazy from .native_loop_flow import LABEL


def prepare_loop_exports(blocks, members, dominators, fresh):
    definitions = {row.result:(label,row) for label,rows in blocks.items() for row in rows if row.result}
    moved, after = set(), {}
    for label,rows in tuple(blocks.items()):
        if label in members:
            continue
        for row in rows:
            match = re.fullmatch(r'%' + LABEL + r' = extractvalue %' + LABEL + r' (%' + LABEL + r'), (\d+)',row.text)
            if match is None or match[1] not in definitions:
                continue
            owner, definition = definitions[match[1]]
            if owner not in members:
                continue
            if owner not in dominators[label]:
                raise ShaderGraphError('A loop aggregate does not dominate its projected exit value.')
            if definition.opcode not in {'call','insertvalue'}:
                continue
            after.setdefault(definition.result,[]).append(row)
            moved.add(row.result)
    if moved:
        for label,rows in tuple(blocks.items()):
            blocks[label] = [part for row in rows if row.result not in moved for part in (row,*after.get(row.result,()))]

    handles = {name:row for name,(label,row) in definitions.items() if label in members and
               (re.match(r'call %dx\.types\.Handle @dx\.op\.(?:annotateHandle|createHandle|createHandleFromBinding|createHandleFromHeap)\(',row.text.partition('=')[2].strip())
                or re.fullmatch(r'bitcast .+ to i8\*',row.text.partition('=')[2].strip()))}
    if not handles:
        return
    for label,rows in tuple(blocks.items()):
        if label in members:
            continue
        copies, rewritten, active = {}, [], set()
        def materialize(name, recurse):
            if name not in handles:
                return name
            if name in copies:
                return copies[name]
            if name in active:
                raise ShaderGraphError('A loop resource recipe contains a cyclic handle dependency.')
            active.add(name)
            body = re.sub(r'%'+LABEL,lambda match:recurse(match[0], recurse),handles[name].text.partition('=')[2].strip())
            result = '%'+fresh('reasy_resource')
            rewritten.append(DxilSemanticRecord('instruction',f'{result} = {body}',handles[name].opcode,result))
            copies[name] = result
            active.remove(name)
            return result
        for row in rows:
            body = row.text.partition('=')[2] if row.result else row.text
            if row.opcode=='phi' and any(name in handles for name in re.findall(r'%'+LABEL,body)):
                raise ShaderGraphError('A resource-valued exit phi needs explicit binding selection.')
            text = re.sub(r'%'+LABEL,lambda match:materialize(match[0], materialize),row.text)
            rewritten.append(DxilSemanticRecord(row.kind,text,row.opcode,row.result))
        blocks[label] = rewritten
