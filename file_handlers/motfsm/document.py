"""FSM editor access, scoped queries and undoable native document commands."""
from dataclasses import dataclass, fields, is_dataclass
import os
import struct
import copy
import json
from uuid import uuid4

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QUndoCommand, QUndoStack

from .fields import SCALAR_FORMATS
from .motfsm_file import Action, MotfsmFile
from .rsz_adapter import BLOCK_NAMES


@dataclass(frozen=True)
class ObjectKey:
    file: str
    block: str
    instance: int


@dataclass(frozen=True)
class ActionRecord:
    key: ObjectKey
    id_hash: int
    ex_id: int
    class_name: str


@dataclass(frozen=True)
class NodeReference:
    node_index: int
    position: int


@dataclass(frozen=True)
class DocumentChange:
    address: tuple | None = None
    node_index: int | None = None

    @property
    def references_changed(self):
        if self.address is None:
            return True
        return (self.address[0] == 'rsz' and self.address[-1] == 'v1_ID'
                or self.address[0] == 'bhvt' and (
                    self.address[1] in ('action_ex_ids', 'static_action_ex_ids')
                    or 'actions' in self.address))

    @property
    def topology_changed(self):
        if self.address is None or self.address[0] != 'bhvt':
            return False
        return (any(part in self.address for part in ('children', 'states', 'transitions', 'all_states'))
                or self.address[-1] in ('id_hash', 'ex_id', 'parent', 'parent_ex')
                and 'actions' not in self.address)


class _FieldCommand(QUndoCommand):
    def __init__(self, access, address, before, after, label):
        super().__init__(f'Edit {label}')
        self.access, self.address = access, address
        self.before, self.after = before, after

    def redo(self):
        self.access._apply_field(self.address, self.after)

    def undo(self):
        self.access._apply_field(self.address, self.before)


class _DocumentCommand(QUndoCommand):
    def __init__(self, access, node_index, before, after, prepared, label):
        super().__init__(label)
        self.access, self.node_index = access, node_index
        self.before, self.after, self.prepared = before, after, prepared

    def redo(self):
        self.access._apply_document(self.after, self.node_index, self.prepared)
        self.prepared = None

    def undo(self):
        self.access._apply_document(self.before, self.node_index)


class FsmDocument(QObject):
    changed = Signal(object)

    def __init__(self, handler):
        super().__init__(handler)
        self.handler = handler
        self.undo_stack = QUndoStack(self)
        self.scope = os.path.normcase(os.path.abspath(handler.filepath)) if handler.filepath else f'untitled:{uuid4()}'
        self._addresses = {}
        self._actions = None
        self._users = None
        self._transitions = None

    @property
    def document(self):
        return self.handler.motfsm

    def _read(self, source):
        document = MotfsmFile()
        document.set_rsz_type_info_path(self.document._registry_path)
        document.read(source)
        return document

    def _index(self):
        if self._actions is None:
            self._actions = {}
            for (id_hash, ex_id), (block, index) in self.document.references.action_identities().items():
                instance = self.document.rsz_blocks.get_block(block).get_instance(index)
                key = ObjectKey(self.scope, block, index)
                self._actions[key] = ActionRecord(key, id_hash, ex_id, instance.class_name)
        if self._users is None:
            by_identity = {(a.id_hash, a.ex_id): a.key for a in self._actions.values()}
            self._users = {key: [] for key in self._actions}
            for node_index, node in enumerate(self.document.bhvt.nodes):
                for position, action in enumerate(node.actions):
                    key = by_identity.get((action.id_hash, action.ex_id))
                    if key is not None:
                        self._users[key].append(NodeReference(node_index, position))

    def actions(self):
        self._index()
        return tuple(self._actions.values())

    def action(self, key):
        self._index()
        return self._actions[key]

    def instance(self, key):
        if key.file != self.scope:
            raise ValueError('Object belongs to a different FSM document')
        return self.document.rsz_blocks.get_block(key.block).get_instance(key.instance)

    def users(self, key):
        self._index()
        return tuple(self._users[key])

    def fields(self, key):
        return {field.name: field for field in self.instance(key).fields}

    def query(self, *, class_name='', identity='', field_name='', value='', node_index=None):
        identity, field_name, value = identity.casefold(), field_name.casefold(), value.casefold()
        result = []
        for action in self.actions():
            if class_name and action.class_name != class_name:
                continue
            if node_index is not None and not any(ref.node_index == node_index for ref in self.users(action.key)):
                continue
            if identity and identity not in (
                f'{action.class_name} {action.id_hash} 0x{action.id_hash:08x} {action.ex_id} '
                f'{action.key.block}[{action.key.instance}]').casefold():
                continue
            if field_name or value:
                candidates = [f for f in self.instance(action.key).fields if field_name in f.name.casefold()]
                if not candidates or value and not any(value in str(f.value).casefold() for f in candidates):
                    continue
            result.append(action)
        return result

    def _map_bhvt(self, value, path=('bhvt',)):
        if is_dataclass(value):
            members = [(field.name, getattr(value, field.name)) for field in fields(value)
                       if not field.name.startswith('_')]
        elif isinstance(value, list):
            members = list(enumerate(value))
        else:
            return
        for name, child in members:
            binding = self.document.bindings.get(value, name)
            if binding is not None:
                self._addresses[id(binding)] = (*path, name)
            self._map_bhvt(child, (*path, name))

    def address(self, binding):
        if self.document.bindings.get(binding.owner, binding.attribute) is not binding:
            raise ValueError('Field does not belong to this document')
        if id(binding) not in self._addresses:
            self._map_bhvt(self.document.bhvt)
            for name in BLOCK_NAMES:
                block = self.document.rsz_blocks.get_block(name)
                for index, instance in block._instances.items():
                    for field in instance.fields:
                        self._map_rsz(field, block.name, index)
        return self._addresses[id(binding)]

    def _map_rsz(self, field, block, index):
        if field.binding is not None:
            self._addresses[id(field.binding)] = ('rsz', block, index, *field.path)
        for child in field.children:
            self._map_rsz(child, block, index)

    @staticmethod
    def field_address(field):
        return ('rsz', field.block.name, field.instance_index, *field.path)

    def rsz_field(self, address, document=None):
        document = document or self.document
        _, block, index, *path = address
        children = document.rsz_blocks.get_block(block).get_instance(index).fields
        for position, part in enumerate(path):
            field = next(f for f in children if f.path[-1] == part)
            if position != len(path)-1:
                children = field.children
        return field

    def binding(self, address, document=None):
        document = document or self.document
        if address[0] == 'rsz':
            return self.rsz_field(address, document).binding
        owner = document.bhvt
        for part in address[1:-1]:
            owner = owner[part] if isinstance(part, int) else getattr(owner, part)
        return document.bindings.get(owner, address[-1])

    def edit_field(self, binding, text):
        address = self.address(binding)
        if address[0] == 'rsz':
            field = self.rsz_field(address)
            member = next((m for m in field.enum_values if m['name'] == str(text)), None)
            if member is not None:
                text = member['value']
        encoded = binding.encode(binding.parse(text))
        if encoded == binding.encode(binding.value):
            return
        value = struct.unpack('<' + SCALAR_FORMATS[binding.type_name], encoded)[0]
        if address[0] == 'rsz':
            field = self.rsz_field(address)
            if field.reference:
                from file_handlers.rsz.utils.rsz_field_utils import validate_reference_type
                validate_reference_type(self.document.type_registry, field.block.file.instance_infos,
                                        field.data.orig_type, value)
        self.undo_stack.push(_FieldCommand(self, address, binding.value, value, binding.name))

    def _apply_field(self, address, value):
        binding = self.binding(address)
        self.document.edit_field(binding, value)
        self.handler.modified = self.document.rebuild() != self.handler._saved_source
        change = DocumentChange(address)
        if change.references_changed:
            self._actions = self._users = None
        self.handler.field_changed.emit(binding)
        self.changed.emit(change)

    def replace_actions(self, node_index, actions, label='Change Action references'):
        node = self.document.get_node_by_index(node_index)
        if node.actions == actions:
            return
        before = self.document.rebuild()
        original = node.actions
        node.actions = actions
        try:
            after = self.document.rebuild()
            candidate = self._read(after)
        finally:
            node.actions = original
        self.undo_stack.push(_DocumentCommand(self, node_index, before, after, candidate, label))

    def _apply_document(self, source, node_index, prepared=None):
        self.handler.motfsm = prepared if prepared is not None else self._read(source)
        self._addresses.clear()
        self._actions = self._users = None
        self._transitions = None
        self.handler.modified = self.document.rebuild() != self.handler._saved_source
        self.handler.document_changed.emit()
        self.changed.emit(DocumentChange(node_index=node_index))

    def _change_rsz(self, field, mutate, label):
        from .serialization import splice_document
        from .validation import variable_snapshot
        address = self.field_address(field)
        before = self.document.rebuild()
        document = self._read(before)
        target = self.rsz_field(address, document)
        block = target.block
        baseline = block.file.build_validated()
        padding = block.end-block.offset-len(baseline)
        if padding < 0 or document.source[block.offset:block.end] != baseline+bytes(padding):
            raise ValueError('RSZ writer does not reproduce the original block')
        mutate(document, target)
        data = block.file.build_validated()
        after = bytes(splice_document(document, [(block.offset, block.end, data+bytes((-len(data)) % 16))]))
        if after == before:
            return
        prepared = self._read(after)
        if prepared.rebuild() != after:
            raise ValueError('RSZ candidate does not rebuild identically')
        if prepared.bhvt.nodes != document.bhvt.nodes:
            raise ValueError('RSZ edit changed the BHVT node graph')
        if variable_snapshot(prepared) != variable_snapshot(document):
            raise ValueError('RSZ edit changed UVAR data')
        if (before[document.transition_map_tbl_offset:document.transition_map_tbl_offset+document.transition_map_count*8]
                != after[prepared.transition_map_tbl_offset:prepared.transition_map_tbl_offset+prepared.transition_map_count*8]
                or before[document.transition_data_tbl_offset:] != after[prepared.transition_data_tbl_offset:]):
            raise ValueError('RSZ edit changed transition tables')
        for name in BLOCK_NAMES:
            previous, current = document.rsz_blocks.get_block(name), prepared.rsz_blocks.get_block(name)
            if previous is block:
                if current.file.build_validated() != data:
                    raise ValueError('RSZ block differs after reopening')
            elif before[previous.offset:previous.end] != after[current.offset:current.end]:
                raise ValueError(f'RSZ edit changed unrelated {name} data')
        self.undo_stack.push(_DocumentCommand(self, None, before, after, prepared, label))

    def edit_rsz_field(self, field, text):
        if field.binding:
            return self.edit_field(field.binding, text)
        from file_handlers.rsz.utils.rsz_field_utils import coerce_field_value
        value = text if isinstance(field.value, str) else json.loads(text)
        if value == field.value:
            return
        def mutate(document, target):
            native = target.block.file
            replacement = coerce_field_value(target.data, value, instance_infos=native.instance_infos,
                userdata_strings={i: native._rsz_userdata_str_map[v] for i, v in native._rsz_userdata_dict.items()},
                registry=document.type_registry, label=field.name, allow_references=True)
            owner = native.parsed_elements[target.instance_index]
            for part in target.path[:-1]:
                owner = owner[part] if isinstance(owner, dict) else owner.values[part]
            if isinstance(owner, dict):
                owner[target.path[-1]] = replacement
            else:
                owner.values[target.path[-1]] = replacement
        self._change_rsz(field, mutate, f'Edit {field.name}')

    def transition_fields(self, address):
        from .transitions import TransitionTables
        if self._transitions is None:
            self._transitions = TransitionTables(self.document)
        tables = self._transitions
        index = tables.resolve(self.binding(address).value)
        return index, tables.fields(index) if index is not None else []

    def edit_transition(self, address, name, value):
        from .transitions import edit_state_transition, TransitionTables, patch_record
        from .validation import variable_snapshot
        before = self.document.rebuild()
        document = self._read(before)
        binding = self.binding(address, document)
        tables = TransitionTables(document)
        index = tables.resolve(binding.value)
        if index is None:
            raise ValueError('This state has no transition-data record')
        expected = patch_record(tables, index, name, value)
        after = edit_state_transition(document, binding, name, value)
        if after == before:
            return
        prepared = self._read(after)
        saved = TransitionTables(prepared)
        saved_index = saved.resolve(self.binding(address, prepared).value)
        if saved.record(saved_index)[4:] != expected[4:] or prepared.rebuild() != after:
            raise ValueError('Transition settings did not survive serialization')
        # All original data records except a privately owned edited record stay exact.
        for i in range(document.transition_data_count):
            if i != saved_index and saved.record(i) != tables.record(i):
                raise ValueError('Transition edit changed an unrelated data record')
        if variable_snapshot(prepared) != variable_snapshot(document):
            raise ValueError('Transition edit changed UVAR data')
        self.undo_stack.push(_DocumentCommand(self, address[2], before, after, prepared, f'Edit {name}'))

    def change_array(self, field, operation, position=None):
        from file_handlers.rsz.rsz_data_types import ArrayData
        from file_handlers.rsz.utils.rsz_field_utils import create_default_field_value, create_field_value_from_definition
        def mutate(document, target):
            if not target.is_array:
                raise ValueError('Selected field is not an array')
            values = list(target.data.values)
            if operation in ('duplicate', 'remove'):
                if position is None or not 0 <= position < len(values):
                    raise ValueError('Array element index is out of range')
                if operation == 'duplicate':
                    values.insert(position+1, copy.deepcopy(values[position]))
                else:
                    del values[position]
            elif operation == 'add':
                if values:
                    values.append(copy.deepcopy(values[-1]))
                elif isinstance(target.data, ArrayData):
                    values.append(create_default_field_value(target.data.element_class, target.data.orig_type,
                                                            field_size=target.definition['size']))
                else:
                    definition, _ = document.type_registry.find_type_by_name(target.data.orig_type)
                    if definition is None:
                        raise ValueError(f'Unknown struct type {target.data.orig_type}')
                    values.append({fd['name']: create_field_value_from_definition(fd)[1] for fd in definition['fields']})
            else:
                raise ValueError(f'Unknown array operation: {operation}')
            target.data.values = values
        self._change_rsz(field, mutate, f'{operation.title()} {field.name} element')

    def add_reference(self, node_index, id_hash, ex_id):
        if self.document.references.action(id_hash, ex_id) is None:
            raise ValueError('Select an existing Action')
        node = self.document.get_node_by_index(node_index)
        self.replace_actions(node_index, [*node.actions, Action(id_hash, ex_id)], 'Add Action reference')

    def remove_reference(self, node_index, position):
        actions = list(self.document.get_node_by_index(node_index).actions)
        if not 0 <= position < len(actions):
            raise IndexError(f'Invalid Action reference index: {position}')
        del actions[position]
        self.replace_actions(node_index, actions, 'Remove Action reference')

    def mark_saved(self):
        self.undo_stack.setClean()
        self.handler.modified = False
