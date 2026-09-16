"""FSM editor access, scoped queries and undoable native document commands."""
from dataclasses import dataclass, fields, is_dataclass
import os
import struct
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


class _ReferencesCommand(QUndoCommand):
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
        self._edited = set()
        self._dirty = set()
        self._references_dirty = False
        self._saved = self._read(handler._saved_source)
        self._saved_actions = self._action_lists(self.document)

    @property
    def document(self):
        return self.handler.motfsm

    def _read(self, source):
        document = MotfsmFile()
        document.set_rsz_type_info_path(self.document._registry_path)
        document.read(source)
        return document

    @staticmethod
    def _action_lists(document):
        return tuple(tuple((a.id_hash, a.ex_id) for a in n.actions) for n in document.bhvt.nodes)

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
                        if field.binding is not None:
                            self._addresses[id(field.binding)] = ('rsz', block.name, index, field.name)
        return self._addresses[id(binding)]

    def binding(self, address, document=None):
        document = document or self.document
        if address[0] == 'rsz':
            _, block, index, name = address
            instance = document.rsz_blocks.get_block(block).get_instance(index)
            return next(field.binding for field in instance.fields if field.name == name)
        owner = document.bhvt
        for part in address[1:-1]:
            owner = owner[part] if isinstance(part, int) else getattr(owner, part)
        return document.bindings.get(owner, address[-1])

    def edit_field(self, binding, text):
        encoded = binding.encode(binding.parse(text))
        if encoded == binding.encode(binding.value):
            return
        address = self.address(binding)
        value = struct.unpack('<' + SCALAR_FORMATS[binding.type_name], encoded)[0]
        self.undo_stack.push(_FieldCommand(self, address, binding.value, value, binding.name))

    def _track_field(self, address):
        binding, saved = self.binding(address), self.binding(address, self._saved)
        if binding.encode(binding.value) == saved.encode(saved.value):
            self._dirty.discard(address)
        else:
            self._dirty.add(address)

    def _apply_field(self, address, value):
        binding = self.binding(address)
        self.document.edit_field(binding, value)
        self._edited.add(address)
        # Newly inserted reference slots have no field in the saved document.
        if 'actions' in address and address[0] == 'bhvt':
            self._references_dirty = self._action_lists(self.document) != self._saved_actions
        else:
            self._track_field(address)
        self.handler.modified = bool(self._dirty) or self._references_dirty
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
        self.undo_stack.push(_ReferencesCommand(self, node_index, before, after, candidate, label))

    def _apply_document(self, source, node_index, prepared=None):
        self.handler.motfsm = prepared if prepared is not None else self._read(source)
        self._addresses.clear()
        self._actions = self._users = None
        for address in self._edited:
            if not (address[0] == 'bhvt' and 'actions' in address):
                self._track_field(address)
        self._references_dirty = self._action_lists(self.document) != self._saved_actions
        self.handler.modified = bool(self._dirty) or self._references_dirty
        self.handler.document_changed.emit()
        self.changed.emit(DocumentChange(node_index=node_index))

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
        self._saved = self._read(self.handler._saved_source)
        self._saved_actions = self._action_lists(self.document)
        self._edited.clear()
        self._dirty.clear()
        self._references_dirty = False
        self.undo_stack.setClean()
        self.handler.modified = False
