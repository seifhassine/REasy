"""Independent native UVAR snapshot used when verifying BHVT relocation."""
import struct
from file_handlers.uvar.base_model import FileHandler
from file_handlers.uvar.uvar_file import UVarFile


def variable_snapshot(document):
    source, base = document.source, document.tree_data_offset
    table = document.bhvt.offsets['base_variables']
    count = struct.unpack_from('<I', source, table)[0]
    roots = [document.bhvt.offsets['variables']]
    roots += [base + struct.unpack_from('<Q', source, table + 4 + i * 8)[0] for i in range(count)]
    result = []
    for root in roots:
        reader = FileHandler(source, offset=base)
        reader.seek(root)
        uvar = UVarFile()
        uvar.do_read(reader)
        variables = []
        for variable in uvar.variables:
            expression = variable.expression
            nodes = [] if expression is None else [
                (n.name, n.node_id, [(p.name_hash, p.raw_type_code, p.value) for p in n.parameters])
                for n in expression.nodes]
            relations = [] if expression is None else [vars(r) for r in expression.relations]
            variables.append((variable.guid, variable.name, variable.type, variable.flags,
                              variable.name_hash, variable.value, nodes, relations))
        result.append((uvar.header.version, uvar.header.name, uvar.header.uvar_hash,
                       variables, uvar.hash_data.guids, uvar.hash_data.guid_map,
                       uvar.hash_data.name_hashes, uvar.hash_data.name_hash_map))
    return result
