"""Offline native FSM graph operations. Transactions publish only validated bytes."""
import copy
from contextlib import contextmanager

from file_handlers.rsz.rsz_file import RszInstanceInfo
from file_handlers.rsz.rsz_instance_operations import RszInstanceOperations
from file_handlers.rsz.graph_copy import copy_graph
from file_handlers.rsz.utils.rsz_field_utils import create_field_value_from_definition, coerce_field_value
from .motfsm_file import MotfsmFile, ChildNode, Action, State, Transition, AllState, IndexList
from .rsz_adapter import BLOCK_NAMES


def object_bindings(document):
    """Yield (block namespace, owner, attribute/index) for BHVT object slots."""
    for node in document.bhvt.nodes:
        yield 'selectors', node, 'selector_id'
        yield 'selectors', node, 'selector_caller_condition_id'
        for i in range(len(node.selector_callers)):
            yield 'selector_callers', node.selector_callers, i
        for child in node.children:
            yield 'conditions', child, 'condition_id'
        for state in node.states:
            yield 'conditions', state, 'TransitionConditions'
            for i in range(len(state.mStates.values)):
                yield 'transition_events', state.mStates.values, i
        for transition in node.transitions:
            yield 'conditions', transition, 'mStartStateTransition'
            if document.layout.transition_event_lists is not False:
                for i in range(len(transition.mStartTransitionEvent.values)):
                    yield 'transition_events', transition.mStartTransitionEvent.values, i
        for state in node.all_states:
            yield 'conditions', state, 'mAllTransition'


def get_value(owner, key):
    return owner[key] if isinstance(key, int) else getattr(owner, key)


def set_value(owner, key, value):
    if isinstance(key, int):
        owner[key] = value
    else:
        setattr(owner, key, value)


class GraphEditor:
    """Edit an existing native container, keeping opaque sections and string indices.

    Use transaction() for atomic publication or FsmDocument.change_graph for undo.
    Existing names stay in the pool when nodes are deleted; byte-exact undo uses
    the original checkpoint. Shared object-table roots and external userdata stay
    shared when cloning, while owned ObjectData descendants are copied/remapped.
    Scalar-event layouts store UID hashes; list layouts store typed object slots.
    """
    def __init__(self, document):
        self.document = document

    @contextmanager
    def transaction(self):
        """Edit a detached native document; failure leaves the original intact."""
        candidate = MotfsmFile()
        candidate.set_rsz_type_info_path(self.document._registry_path)
        candidate.read(self.document.build_validated())
        editor = GraphEditor(candidate)
        yield editor
        output = candidate.build_validated()
        self.document.read(output)

    def _invalidate(self, block=None):
        self.document.references.invalidate()
        if block is not None:
            block._instances.clear()

    def intern_name(self, name):
        if not isinstance(name, str) or '\0' in name:
            raise ValueError('Node names must be strings without embedded NULs')
        encoded = (name + '\0').encode('utf-16le')
        pool = self.document.bhvt.string_pool
        for i in range(0, len(pool), 2):
            if (i == 0 or pool[i-2:i] == b'\0\0') and pool[i:i+len(encoded)] == encoded:
                return i // 2
        self.document.bhvt.string_pool += encoded
        return len(pool) // 2

    def rename_node(self, index, name):
        node = self.document.get_node_by_index(index)
        node.name_index = self.intern_name(name)
        node.name = name
        # Hashes are independent native identities; the caller sets them explicitly.

    def add_node(self, node, parent_index=None, condition=-1):
        if node.id_hash in (0, 0xFFFFFFFF):
            raise ValueError('New nodes require a non-sentinel identity hash')
        if any((n.id_hash, n.ex_id) == (node.id_hash, node.ex_id) for n in self.document.bhvt.nodes):
            raise ValueError('Duplicate BHVT node identity')
        if node.children:
            raise ValueError('Add nodes before connecting their children')
        self.document.references.object_instance('conditions', condition)
        parent = self.document.get_node_by_index(parent_index) if parent_index is not None else None
        node.name_index = self.intern_name(node.name)
        node.parent, node.parent_ex = (parent.id_hash, parent.ex_id) if parent else (0xFFFFFFFF, 0)
        self.document.bhvt.nodes.append(node)
        if parent:
            parent.children.append(ChildNode(node.id_hash, node.ex_id, condition))
        self._invalidate()
        return self.document.node_count - 1

    def reparent_node(self, index, parent_index=None, condition=-1):
        node = self.document.get_node_by_index(index)
        parent = self.document.get_node_by_index(parent_index) if parent_index is not None else None
        self.document.references.object_instance('conditions', condition)
        current, seen = parent, set()
        self._invalidate()
        while current is not None:
            if current is node or id(current) in seen:
                raise ValueError('Cyclic BHVT parent relationship')
            seen.add(id(current))
            ancestor = self.document.references.parent_index(current)
            current = self.document.bhvt.nodes[ancestor] if ancestor is not None else None
        old = self.document.references.parent_index(node)
        if old is not None:
            children = self.document.bhvt.nodes[old].children
            children[:] = [c for c in children if (c.id_hash, c.ex_id) != (node.id_hash, node.ex_id)]
        node.parent, node.parent_ex = (parent.id_hash, parent.ex_id) if parent else (0xFFFFFFFF, 0)
        if parent:
            parent.children.append(ChildNode(node.id_hash, node.ex_id, condition))
        self._invalidate()

    def remove_node(self, index):
        node = self.document.get_node_by_index(index)
        self._invalidate()
        if node.children:
            raise ValueError('Remove or reparent children before deleting a node')
        for i, other in enumerate(self.document.bhvt.nodes):
            if other is node:
                continue
            targets = [self.document.references.state_target(s.mTransitions, s.mTransitionAttributes) for s in other.states]
            targets += [self.document.references.node_index(t.mStartState, t.mStartStateEx) for t in other.transitions]
            targets += [self.document.references.node_index(s.mAllState, s.mAllStateEx) for s in other.all_states]
            if index in targets:
                raise ValueError(f'Node {i} {other.name} still references the deleted node')
        parent = self.document.references.parent_index(node)
        if parent is not None:
            children = self.document.bhvt.nodes[parent].children
            children[:] = [c for c in children if (c.id_hash, c.ex_id) != (node.id_hash, node.ex_id)]
        del self.document.bhvt.nodes[index]
        self._invalidate()

    def connect_state(self, index, target_index, *, condition=-1, transition_map=0,
                      events=(), metadata=0):
        node, target = self.document.get_node_by_index(index), self.document.get_node_by_index(target_index)
        self._invalidate()
        self.document.references.state_target(target.id_hash, target.ex_id)
        self.document.references.object_instance('conditions', condition)
        for raw in events:
            self.document.references.object_instance('transition_events', raw)
        from .transitions import TransitionTables
        if self.document.transition_map_count or transition_map:
            TransitionTables(self.document).resolve(transition_map)
        state = State(IndexList(list(events)), target.id_hash, condition, transition_map, target.ex_id, metadata)
        node.states.append(state)
        return state

    def connect_start(self, index, target_index, *, condition=-1, events=()):
        node, target = self.document.get_node_by_index(index), self.document.get_node_by_index(target_index)
        if not self.document.layout.transition_state_ex and target.ex_id:
            raise ValueError('This layout cannot encode the target extension ID')
        self.document.references.object_instance('conditions', condition)
        transition = Transition(IndexList(list(events)), target.id_hash, condition, target.ex_id)
        node.transitions.append(transition)
        return transition

    def connect_all(self, index, target_index, *, condition=-1, transition_map=0, attributes=0):
        node, target = self.document.get_node_by_index(index), self.document.get_node_by_index(target_index)
        if node.has_reference_tree:
            raise ValueError('Reference-tree nodes cannot store AllStates')
        self.document.references.object_instance('conditions', condition)
        state = AllState(target.id_hash, condition, transition_map, target.ex_id, attributes)
        node.all_states.append(state)
        return state

    def _block(self, name):
        if name not in BLOCK_NAMES:
            raise ValueError(f'Unknown RSZ block {name}')
        block = self.document.rsz_blocks.get_block(name)
        aliases = [n for n in BLOCK_NAMES if self.document.rsz_blocks.get_block(n) is block]
        if len(aliases) != 1:
            raise ValueError(f'Editing aliased RSZ sections is not supported: {aliases}')
        return block

    def _extensions(self, name):
        if name == 'actions':
            return self.document.bhvt.action_ex_ids
        if name == 'static_actions':
            return self.document.bhvt.static_action_ex_ids
        return None

    @staticmethod
    def _slot(name, raw):
        if not isinstance(raw, int) or not 0 <= raw <= 0x4000FFFF or raw & 0xBFFF0000:
            raise ValueError(f'Invalid {name} object slot: {raw}')
        if raw & 0x40000000 and not name.startswith('static_'):
            raise ValueError(f'Static object reference used with {name}')
        return raw & 0xFFFF

    def _identity(self, name, fields, identity, extension):
        if identity is not None and (not isinstance(identity, int) or not 0 <= identity <= 0xFFFFFFFF):
            raise ValueError('Object identity must be an unsigned 32-bit integer')
        if not isinstance(extension, int) or not 0 <= extension <= 0xFFFFFFFF:
            raise ValueError('Action extension ID must be an unsigned 32-bit integer')
        if name in ('actions', 'static_actions'):
            if identity is None or identity in (0, 0xFFFFFFFF):
                raise ValueError('New Actions require a non-sentinel identity hash')
            self._invalidate()
            if (identity, extension) in self.document.references.action_identities():
                raise ValueError('Duplicate Action identity')
            if 'v1_ID' not in fields:
                raise ValueError('Action objects require the native v1_ID field')
            fields['v1_ID'].value = identity
        elif identity is not None:
            key = 'v0_UID' if name.endswith('transition_events') else 'v0_ID'
            if key not in fields:
                raise ValueError(f'{name} objects do not have an {key} identity field')
            fields[key].value = identity

    def create_instance(self, name, class_name, values=None):
        block = self._block(name)
        definition, type_id = self.document.type_registry.find_type_by_name(class_name)
        if not definition:
            raise ValueError(f'Unknown RSZ class {class_name}')
        fields = {fd['name']: create_field_value_from_definition(fd)[1] for fd in definition['fields']}
        self._set_fields(block, fields, values or {})
        info = RszInstanceInfo()
        info.type_id = type_id
        info.crc = int(definition['crc'], 16) if isinstance(definition['crc'], str) else definition['crc']
        index = len(block.file.instance_infos)
        block.file.instance_infos.append(info)
        block.file.parsed_elements[index] = fields
        self._invalidate(block)
        return index

    def _set_fields(self, block, fields, values):
        native = block.file
        for key, value in values.items():
            if key not in fields:
                raise ValueError(f'Unknown RSZ field {key}')
            fields[key] = coerce_field_value(fields[key], value, instance_infos=native.instance_infos,
                userdata_strings={i: native.get_rsz_userdata_string(u) for i, u in native._rsz_userdata_dict.items()},
                registry=self.document.type_registry, label=key, allow_references=True)

    def set_object_fields(self, name, raw, values):
        block = self._block(name)
        index = self._slot(name, raw)
        fields = copy.deepcopy(block.file.parsed_elements[block.get_object(index).index])
        self._set_fields(block, fields, values)
        block.file.parsed_elements[block.object_table[index]] = fields
        self._invalidate(block)

    def create_object(self, name, class_name, *, identity=None, extension=0, values=None):
        block = self._block(name)
        if len(block.object_table) > 0xFFFF:
            raise ValueError('BHVT object table exceeds its 16-bit reference range')
        definition, _ = self.document.type_registry.find_type_by_name(class_name)
        if not definition:
            raise ValueError(f'Unknown RSZ class {class_name}')
        fields = {fd['name']: create_field_value_from_definition(fd)[1] for fd in definition['fields']}
        self._set_fields(block, fields, values or {})
        self._identity(name, fields, identity, extension)
        # Create the same schema-backed instance through the common operation.
        index = self.create_instance(name, class_name)
        block.file.parsed_elements[index] = fields
        slot = len(block.object_table)
        block.object_table.append(index)
        extensions = self._extensions(name)
        if extensions is not None:
            extensions.append(extension)
        self._invalidate(block)
        return slot | (0x40000000 if name.startswith('static_') else 0)

    def clone_object(self, name, raw, *, identity=None, extension=0):
        """Clone the owned Object graph; keep shared roots and external userdata."""
        return self.import_object(name, raw, identity=identity, extension=extension,
                                  owned_only=True)

    def import_object(self, name, raw, *, source=None, source_name=None,
                      identity=None, extension=0, owned_only=False, shared=None):
        """Import a graph across blocks/documents with explicit native identities."""
        block = self._block(name)
        native = block.file
        origin = source or self.document
        source_name = source_name or name
        source_block = origin.rsz_blocks.get_block(source_name)
        root = source_block.get_object(self._slot(source_name, raw)).index
        fields = copy.deepcopy(source_block.file.parsed_elements[root])
        self._identity(name, fields, identity, extension)
        slot = len(native.object_table)
        if slot > 0xFFFF:
            raise ValueError('BHVT object table exceeds its 16-bit reference range')
        copied = copy_graph(source_block.file, native, [root], owned_only=owned_only, shared=shared)
        index = copied.roots[0]
        self._identity(name, native.parsed_elements[index], identity, extension)
        native.object_table.append(index)
        extensions = self._extensions(name)
        if extensions is not None:
            extensions.append(extension)
        self._invalidate(block)
        return slot | (0x40000000 if name.startswith('static_') else 0)

    def action_reference(self, name, raw):
        extensions = self._extensions(name)
        if extensions is None:
            raise ValueError('Action reference requires an Action block')
        slot = self._slot(name, raw)
        block = self.document.rsz_blocks.get_block(name)
        instance = block.get_object(slot).index
        return Action(block.file.parsed_elements[instance]['v1_ID'].value, extensions[slot])

    def remove_object(self, name, raw):
        """Delete an unreferenced object/owned graph, then compact both namespaces."""
        block = self._block(name)
        slot = self._slot(name, raw)
        root = block.get_object(slot).index
        native = block.file
        adjustments = []
        extensions = self._extensions(name)
        if extensions is not None:
            action = self.action_reference(name, raw)
            if any(action in n.actions for n in self.document.bhvt.nodes):
                raise ValueError('Action is still referenced by a node')
        for namespace, owner, key in object_bindings(self.document):
            value = get_value(owner, key) & 0xFFFFFFFF
            if value == 0xFFFFFFFF:
                continue
            resolved = ('static_' if value >> 24 == 0x40 else '') + namespace
            if resolved != name:
                continue
            position = value & 0xFFFF
            if position == slot:
                raise ValueError(f'{name} object {slot} is still referenced by a node')
            if position > slot:
                adjustments.append((owner, key, value - 1))
        owned = RszInstanceOperations.collect_owned_instances(native.parsed_elements, root,
                    object_table=native.object_table, valid_instance_ids=range(len(native.instance_infos)))
        deleted = {root, *owned}
        if any(i in deleted for j, i in enumerate(native.object_table) if j != slot):
            raise ValueError('RSZ object graph is still present in another object slot')
        for source, field, ref, _ in RszInstanceOperations.iter_instance_references(native.parsed_elements, deleted):
            if ref.value in deleted:
                raise ValueError(f'RSZ instance {source}.{field} still references the deleted graph')
        mapping = RszInstanceOperations.build_deletion_id_adjustments(len(native.instance_infos), deleted)
        native.instance_infos = [info for i, info in enumerate(native.instance_infos) if i not in deleted]
        native.parsed_elements = RszInstanceOperations.remap_instance_fields(native.parsed_elements, mapping, deleted)
        native.object_table[:] = [mapping.get(i, i) for j, i in enumerate(native.object_table) if j != slot]
        native.instance_hierarchy = RszInstanceOperations.remap_hierarchy(native.instance_hierarchy, mapping, deleted)
        for u in native.rsz_userdata_infos:
            u.instance_id = mapping.get(u.instance_id, u.instance_id)
        native._rsz_userdata_dict = {u.instance_id: u for u in native.rsz_userdata_infos}
        native._rsz_userdata_set = set(native._rsz_userdata_dict)
        if extensions is not None:
            del extensions[slot]
        for owner, key, value in adjustments:
            set_value(owner, key, value)
        self._invalidate(block)
