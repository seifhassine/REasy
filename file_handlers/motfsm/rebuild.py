"""Rebuild node Action columns and relocate typed BHVT pointers.

RSZ offsets are block-relative. Moving the entire tail by a multiple of 16
keeps both their pointers and their absolute alignment valid.
"""
import struct

from file_handlers.uvar.uvar_types import TypeKind
from file_handlers.uvar.node_parameter import RAW_TYPE_TO_NODE_VALUE


class BhvtPointers:
    """Collect pointer slots through section tables, never by scanning values."""

    def __init__(self, document):
        self.doc = document
        self.source = document.source
        self.base = document.tree_data_offset
        self.end = self.base + document.tree_data_size
        self.slots = set()
        self._uvars = set()
        self._expressions = set()

    def read(self, fmt, offset):
        size = struct.calcsize('<' + fmt)
        if not self.base <= offset <= self.end - size:
            raise ValueError(f'BHVT relocation field outside tree at 0x{offset:X}')
        values = struct.unpack_from('<' + fmt, self.source, offset)
        return values[0] if len(values) == 1 else values

    def pointer(self, slot):
        relative = self.read('Q', slot)
        self.slots.add(slot)
        if not relative:
            return 0
        target = self.base + relative
        if not self.base <= target <= self.end:
            raise ValueError(f'BHVT pointer at 0x{slot:X} targets outside tree: 0x{target:X}')
        return target

    def array(self, offset, count, size):
        if count < 0 or (count and (not offset or offset + count * size > self.end)):
            raise ValueError(f'Invalid BHVT relocation array at 0x{offset:X}, count={count}')
        return range(offset, offset + count * size, size)

    def expression(self, offset):
        if offset in self._expressions:
            return
        self._expressions.add(offset)
        nodes = self.pointer(offset)
        self.pointer(offset + 8)  # Relation entries contain indices, not pointers.
        count = self.read('h', offset + 16)
        for node in self.array(nodes, count, 32):
            self.pointer(node)
            parameters = self.pointer(node + 8)
            unknown = self.pointer(node + 16)
            parameter_count, unknown_count = self.read('hI', node + 26)
            if unknown or unknown_count:
                raise ValueError(f'Unsupported UVAR node extension at 0x{node:X}')
            self.array(parameters, parameter_count, 16)
            for _ in range(parameter_count):
                kind = self.read('i', parameters + 4)
                if kind not in RAW_TYPE_TO_NODE_VALUE:
                    raise ValueError(f'Unsupported UVAR parameter type {kind} at 0x{parameters:X}')
                if kind in (18, 20):
                    self.pointer(parameters + 8)
                # Native parameter records end on the next BHVT-relative boundary.
                parameters = self.base + ((parameters - self.base + 16 + 15) & ~15)

    def uvar(self, offset):
        if offset in self._uvars:
            return
        self._uvars.add(offset)
        version, magic = self.read('II', offset)
        if magic != 0x72617675:
            raise ValueError(f'Invalid UVAR magic at 0x{offset:X}')
        self.pointer(offset + 8)
        variables = self.pointer(offset + 16)
        self.pointer(offset + 24)
        hashes = self.pointer(offset + 32)
        count_offset = offset + (52 if version < 3 else 44)
        count, embed_count = self.read('HH', count_offset)
        if embed_count:
            raise ValueError(f'Embedded UVAR relocation is not implemented at 0x{offset:X}')
        for variable in self.array(variables, count, 48):
            self.pointer(variable + 16)
            value = self.pointer(variable + 24)
            expression = self.pointer(variable + 32)
            raw_kind = self.read('I', variable + 40) & 0xFFFFFF
            try:
                kind = TypeKind(raw_kind)
            except ValueError as exc:
                raise ValueError(f'Unsupported UVAR value type {raw_kind} at 0x{variable:X}') from exc
            if kind in (TypeKind.Unknown, TypeKind.Num):
                raise ValueError(f'Unsupported UVAR value type {raw_kind} at 0x{variable:X}')
            if kind in (TypeKind.C8, TypeKind.C16) and value:
                self.pointer(value)
            if expression:
                self.expression(expression)
        if hashes:
            for slot in self.array(hashes, 4, 8):
                self.pointer(slot)

    def collect(self):
        for i, name in enumerate(self.doc._offset_names):
            self.pointer(self.base + 8 + 8 * i)
            if name.startswith('section_') and name in self.doc.bhvt.offsets:
                raise ValueError(f'Relocation of BHVT {name} is not implemented')
        offsets = self.doc.bhvt.offsets
        prefab = offsets.get('reference_prefab_game_objects')
        if prefab and self.read('I', prefab):
            raise ValueError(f'Nonempty reference prefab table relocation is not implemented at 0x{prefab:X}')
        if offsets.get('variables'):
            self.uvar(offsets['variables'])
        base_variables = offsets.get('base_variables')
        if base_variables:
            count = self.read('I', base_variables)
            for slot in self.array(base_variables + 4, count, 8):
                target = self.pointer(slot)
                if target:
                    self.uvar(target)
        return self.slots


def rebuild_actions(document):
    patched = document.bindings.rebuild()
    changes = []
    for node in document.bhvt.nodes:
        start, end = node._action_span
        values = [action.id_hash for action in node.actions] + [action.ex_id for action in node.actions]
        try:
            encoded = struct.pack(f'<{1 + len(values)}I', len(node.actions), *values)
        except struct.error as exc:
            raise ValueError(f'Invalid Action reference in node {node.name}: {exc}') from exc
        if encoded != patched[start:end]:
            changes.append((start, end, encoded))
    if not changes:
        return patched

    growth = sum(len(data) - (end - start) for start, end, data in changes)
    node_end = document.bhvt.node_data_end
    tail = min(offset for offset in document.bhvt.offsets.values() if offset >= node_end)
    old_padding = tail - node_end
    new_padding = (old_padding - growth) % 16
    delta = growth + new_padding - old_padding
    # Preserve padding bytes where possible, including unchanged nonzero padding.
    padding = patched[node_end:tail]
    padding = (bytes(max(0, new_padding - len(padding))) + padding)[-new_padding:] if new_padding else b''
    changes.append((node_end, tail, padding))
    output = bytearray()
    cursor = 0
    for start, end, data in changes:
        output.extend(patched[cursor:start])
        output.extend(data)
        cursor = end
    output.extend(patched[cursor:])
    if delta:
        pointers = BhvtPointers(document).collect()
        for slot in pointers:
            value = struct.unpack_from('<Q', patched, slot)[0]
            target = document.tree_data_offset + value
            if value and target >= tail:
                value += delta
            new_slot = slot + delta if slot >= tail else slot
            struct.pack_into('<Q', output, new_slot, value)
        for slot in (16, 24, 32, 40):
            value = struct.unpack_from('<Q', patched, slot)[0]
            struct.pack_into('<Q', output, slot, value + delta if value >= tail else value)
        info = document.tree_info_ptr
        struct.pack_into('<I', output, info + delta if info >= tail else info,
                         document.tree_data_size + delta)
    return bytes(output)
