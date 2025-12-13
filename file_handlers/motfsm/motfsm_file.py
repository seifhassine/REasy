"""
MOTFSM file parser for RE Engine.
Based on RE_RSZ.bt 010 Editor template.
"""
import struct
import json
import os
from dataclasses import dataclass, field
from typing import List, Dict, Optional

from utils.binary_handler import BinaryHandler
from file_handlers.motfsm.rsz_parser import RSZBlockCollection, RSZBlock, RSZInstance


MOTFSM_MAGIC = 0x3273666D  # "mfs2"
BHVT_MAGIC = 0x54564842    # "BHVT"


@dataclass
class BHVTId:
    """Variable-length ID list (Count + Count*4 bytes)"""
    count: int = 0
    values: List[int] = field(default_factory=list)

    def read(self, handler: BinaryHandler):
        self.count = handler.read_int32()
        self.values = []
        for _ in range(self.count):
            self.values.append(handler.read_int32())

    def write(self, handler: BinaryHandler):
        """Write BHVTId to binary"""
        handler.write_int32(self.count)
        for value in self.values:
            handler.write_int32(value)

    @property
    def size(self) -> int:
        return 4 + self.count * 4


@dataclass
class BHVTHash:
    """Fixed 4-byte hash value"""
    value: int = 0

    def read(self, handler: BinaryHandler):
        self.value = handler.read_uint32()


@dataclass
class State:
    """State element in BHVTNode"""
    index: int = 0
    mStates: BHVTId = field(default_factory=BHVTId)
    mTransitions: int = 0
    TransitionConditions: int = 0
    TransitionMaps: int = 0
    mTransitionAttributes: int = 0
    mStatesEx: int = 0


@dataclass
class Transition:
    """Transition element in BHVTNode"""
    index: int = 0
    mStartTransitionEvent: BHVTId = field(default_factory=BHVTId)
    mStartState: int = 0
    mStartStateTransition: int = 0
    mStartStateEx: int = 0


@dataclass
class ChildNode:
    """Child node reference"""
    id_hash: int = 0
    ex_id: int = 0
    index: int = 0


@dataclass
class Action:
    """Action reference"""
    id_hash: int = 0
    index: int = 0


@dataclass
class BHVTNode:
    """Behavior Tree Node - variable length structure"""
    # Basic fields
    id_hash: int = 0
    ex_id: int = 0
    name_index: int = 0
    name: str = ""
    parent: int = 0
    parent_ex: int = 0

    # Child nodes (interleaved: all IDs, all exIDs, all indices)
    children: List[ChildNode] = field(default_factory=list)

    # Selector
    selector_id: int = 0
    selector_callers: List[int] = field(default_factory=list)
    selector_caller_condition_id: int = 0

    # Actions (interleaved: all IDs, all indices)
    actions: List[Action] = field(default_factory=list)

    # Priority and attributes
    priority: int = 0
    node_attribute: int = 0
    work_flags: int = 0

    # FSM-specific fields (only if IsFSM flag set)
    name_hash: int = 0
    fullname_hash: int = 0
    tags: List[int] = field(default_factory=list)
    is_branch: int = 0
    is_end: int = 0

    # States (interleaved arrays)
    states: List[State] = field(default_factory=list)

    # Transitions (interleaved arrays)
    transitions: List[Transition] = field(default_factory=list)

    # AllStates (only if not HasReferenceTree)
    all_states: List[dict] = field(default_factory=list)

    # Reference tree index
    reference_tree_index: int = 0

    @property
    def is_fsm(self) -> bool:
        return (self.node_attribute & 0x20) != 0

    @property
    def has_reference_tree(self) -> bool:
        return (self.node_attribute & 0x04) != 0

    def read(self, handler: BinaryHandler, string_pool_offset: int):
        start_pos = handler.tell

        # Store field offsets for later modification
        self._id_hash_offset = start_pos
        self._ex_id_offset = start_pos + 4
        self._name_index_offset = start_pos + 8
        self._parent_offset = start_pos + 12
        self._parent_ex_offset = start_pos + 16

        # Basic fields
        self.id_hash = handler.read_uint32()
        self.ex_id = handler.read_uint32()
        self.name_index = handler.read_uint32()
        self.parent = handler.read_int32()
        self.parent_ex = handler.read_uint32()

        # Read name from string pool
        if string_pool_offset > 0:
            self.name = self._read_string_at_index(handler, string_pool_offset, self.name_index)

        # Child nodes (interleaved storage)
        child_count = handler.read_int32()
        if child_count > 0:
            self.children = []
            # Record offsets for child node arrays
            child_ids_offset = handler.tell
            # All IDs
            ids = [handler.read_uint32() for _ in range(child_count)]
            # Record offset for ex_ids
            child_ex_ids_offset = handler.tell
            # All exIDs
            ex_ids = [handler.read_uint32() for _ in range(child_count)]
            # Record offset for indices
            child_indices_offset = handler.tell
            # All indices
            indices = [handler.read_int32() for _ in range(child_count)]
            for i in range(child_count):
                child = ChildNode(id_hash=ids[i], ex_id=ex_ids[i], index=indices[i])
                # Store offsets for this child's fields
                child._id_hash_offset = child_ids_offset + (i * 4)
                child._ex_id_offset = child_ex_ids_offset + (i * 4)
                child._index_offset = child_indices_offset + (i * 4)
                self.children.append(child)

        # Selector
        self._selector_id_offset = handler.tell
        self.selector_id = handler.read_int32()

        # Selector callers
        selector_callers_count = handler.read_int32()
        if selector_callers_count > 0:
            self.selector_callers = [handler.read_int32() for _ in range(selector_callers_count)]

        self._selector_caller_condition_id_offset = handler.tell
        self.selector_caller_condition_id = handler.read_int32()

        # Actions (interleaved storage)
        actions_count = handler.read_int32()
        if actions_count > 0:
            self.actions = []
            # Record offset for action IDs and indices
            action_ids_offset = handler.tell
            # All IDs
            action_ids = [handler.read_uint32() for _ in range(actions_count)]
            # Record offset for action indices
            action_indices_offset = handler.tell
            # All indices
            action_indices = [handler.read_int32() for _ in range(actions_count)]
            for i in range(actions_count):
                action = Action(id_hash=action_ids[i], index=action_indices[i])
                # Store offsets for this action's fields
                action._id_hash_offset = action_ids_offset + (i * 4)
                action._index_offset = action_indices_offset + (i * 4)
                self.actions.append(action)

        # Store priority offset before reading
        self._priority_offset = handler.tell
        self.priority = handler.read_int32()

        self.node_attribute = handler.read_uint16()
        self.work_flags = handler.read_uint16()

        # FSM-specific fields
        if self.is_fsm:
            self.name_hash = handler.read_uint32()
            self.fullname_hash = handler.read_uint32()

            # Tags
            tags_count = handler.read_int32()
            if tags_count > 0:
                self.tags = [handler.read_uint32() for _ in range(tags_count)]

            self.is_branch = handler.read_uint8()
            self.is_end = handler.read_uint8()

        # States (interleaved arrays)
        states_count = handler.read_int32()
        if states_count > 0:
            self.states = self._read_states_interleaved(handler, states_count)

        # Transitions (interleaved arrays)
        transitions_count = handler.read_int32()
        if transitions_count > 0:
            self.transitions = self._read_transitions_interleaved(handler, transitions_count)

        # AllStates (only if not HasReferenceTree)
        if not self.has_reference_tree:
            all_states_count = handler.read_int32()
            if all_states_count > 0:
                self.all_states = self._read_all_states_interleaved(handler, all_states_count)

        # Reference tree index
        self.reference_tree_index = handler.read_int32()

        return handler.tell - start_pos

    def _read_string_at_index(self, handler: BinaryHandler, pool_offset: int, char_index: int) -> str:
        """Read UTF-16LE string from string pool at character index"""
        current_pos = handler.tell
        try:
            # String pool: 4 bytes size + string data
            byte_offset = pool_offset + 4 + (char_index * 2)
            handler.seek(byte_offset)

            chars = []
            while True:
                c = handler.read_uint16()
                if c == 0:
                    break
                chars.append(chr(c))
            return ''.join(chars)
        except:
            return f"<error:{char_index}>"
        finally:
            handler.seek(current_pos)

    def _read_states_interleaved(self, handler: BinaryHandler, count: int) -> List[State]:
        """
        Read States using interleaved array storage.
        Layout:
          - All mStates[0..n-1] arrays (variable length: Count + Count*4 each)
          - All mTransitions[0..n-1] (4 bytes each)
          - All TransitionConditions[0..n-1] (4 bytes each)
          - All TransitionMaps[0..n-1] (4 bytes each)
          - All mTransitionAttributes[0..n-1] (4 bytes each)
          - All mStatesEx[0..n-1] (4 bytes each)
        """
        # First: all mStates arrays (variable length)
        mstates_list = []
        for i in range(count):
            bhvt_id = BHVTId()
            bhvt_id.read(handler)
            mstates_list.append(bhvt_id)

        # Record offsets for all state field arrays
        mTransitions_offset_base = handler.tell
        transitions = [handler.read_uint32() for _ in range(count)]

        TransitionConditions_offset_base = handler.tell
        trans_conds = [handler.read_int32() for _ in range(count)]

        TransitionMaps_offset_base = handler.tell
        trans_maps = [handler.read_uint32() for _ in range(count)]

        mTransitionAttributes_offset_base = handler.tell
        trans_attrs = [handler.read_uint32() for _ in range(count)]

        mStatesEx_offset_base = handler.tell
        states_ex = [handler.read_uint32() for _ in range(count)]

        # Combine into State objects with offset tracking
        states = []
        for i in range(count):
            state = State(
                index=i,
                mStates=mstates_list[i],
                mTransitions=transitions[i],
                TransitionConditions=trans_conds[i],
                TransitionMaps=trans_maps[i],
                mTransitionAttributes=trans_attrs[i],
                mStatesEx=states_ex[i],
            )
            # Store offsets for all editable fields
            state._mTransitions_offset = mTransitions_offset_base + (i * 4)
            state._TransitionConditions_offset = TransitionConditions_offset_base + (i * 4)
            state._TransitionMaps_offset = TransitionMaps_offset_base + (i * 4)
            state._mTransitionAttributes_offset = mTransitionAttributes_offset_base + (i * 4)
            state._mStatesEx_offset = mStatesEx_offset_base + (i * 4)
            states.append(state)
        return states

    def _read_transitions_interleaved(self, handler: BinaryHandler, count: int) -> List[Transition]:
        """
        Read Transitions using interleaved array storage.
        Layout:
          - All mStartTransitionEvent[0..n-1] (variable length)
          - All mStartState[0..n-1] (4 bytes each)
          - All mStartStateTransition[0..n-1] (4 bytes each)
          - All mStartStateEx[0..n-1] (4 bytes each)
        """
        # First: all mStartTransitionEvent (variable length)
        trans_events = []
        for i in range(count):
            bhvt_id = BHVTId()
            bhvt_id.read(handler)
            trans_events.append(bhvt_id)

        # Record offsets for all transition field arrays
        mStartState_offset_base = handler.tell
        start_states = [handler.read_uint32() for _ in range(count)]

        mStartStateTransition_offset_base = handler.tell
        start_state_trans = [handler.read_int32() for _ in range(count)]

        mStartStateEx_offset_base = handler.tell
        start_state_ex = [handler.read_uint32() for _ in range(count)]

        # Combine into Transition objects with offset tracking
        transitions = []
        for i in range(count):
            trans = Transition(
                index=i,
                mStartTransitionEvent=trans_events[i],
                mStartState=start_states[i],
                mStartStateTransition=start_state_trans[i],
                mStartStateEx=start_state_ex[i],
            )
            # Store offsets for all editable fields
            trans._mStartState_offset = mStartState_offset_base + (i * 4)
            trans._mStartStateTransition_offset = mStartStateTransition_offset_base + (i * 4)
            trans._mStartStateEx_offset = mStartStateEx_offset_base + (i * 4)
            transitions.append(trans)
        return transitions

    def _read_all_states_interleaved(self, handler: BinaryHandler, count: int) -> List[dict]:
        """
        Read AllStates using interleaved array storage.
        5 fields per AllState element.
        """
        # Read all 5 arrays
        field1 = [handler.read_uint32() for _ in range(count)]
        field2 = [handler.read_uint32() for _ in range(count)]
        field3 = [handler.read_int32() for _ in range(count)]
        field4 = [handler.read_uint32() for _ in range(count)]
        field5 = [handler.read_uint32() for _ in range(count)]

        # Combine
        all_states = []
        for i in range(count):
            all_states.append({
                'field1': field1[i],
                'field2': field2[i],
                'field3': field3[i],
                'field4': field4[i],
                'field5': field5[i],
            })
        return all_states

    def write(self, handler: BinaryHandler):
        """Write BHVTNode to binary (must match read format exactly)"""
        # Basic fields
        handler.write_uint32(self.id_hash)
        handler.write_uint32(self.ex_id)
        handler.write_uint32(self.name_index)
        handler.write_int32(self.parent)
        handler.write_uint32(self.parent_ex)

        # Child nodes (interleaved)
        handler.write_int32(len(self.children))
        if self.children:
            # All IDs
            for child in self.children:
                handler.write_uint32(child.id_hash)
            # All exIDs
            for child in self.children:
                handler.write_uint32(child.ex_id)
            # All indices
            for child in self.children:
                handler.write_int32(child.index)

        # Selector
        handler.write_int32(self.selector_id)

        # Selector callers
        handler.write_int32(len(self.selector_callers))
        for caller in self.selector_callers:
            handler.write_int32(caller)

        handler.write_int32(self.selector_caller_condition_id)

        # Actions (interleaved)
        handler.write_int32(len(self.actions))
        if self.actions:
            # All IDs
            for action in self.actions:
                handler.write_uint32(action.id_hash)
            # All indices
            for action in self.actions:
                handler.write_int32(action.index)

        # Priority and attributes
        handler.write_int32(self.priority)
        handler.write_uint16(self.node_attribute)
        handler.write_uint16(self.work_flags)

        # FSM-specific fields
        if self.is_fsm:
            handler.write_uint32(self.name_hash)
            handler.write_uint32(self.fullname_hash)

            # Tags
            handler.write_int32(len(self.tags))
            for tag in self.tags:
                handler.write_uint32(tag)

            handler.write_uint8(self.is_branch)
            handler.write_uint8(self.is_end)

        # States (interleaved)
        handler.write_int32(len(self.states))
        if self.states:
            self._write_states_interleaved(handler, self.states)

        # Transitions (interleaved)
        handler.write_int32(len(self.transitions))
        if self.transitions:
            self._write_transitions_interleaved(handler, self.transitions)

        # AllStates (only if not HasReferenceTree)
        if not self.has_reference_tree:
            handler.write_int32(len(self.all_states))
            if self.all_states:
                self._write_all_states_interleaved(handler, self.all_states)

        # Reference tree index
        handler.write_int32(self.reference_tree_index)

    def _write_states_interleaved(self, handler: BinaryHandler, states: List[State]):
        """Write States using interleaved array storage"""
        # First: all mStates arrays
        for state in states:
            state.mStates.write(handler)

        # Second: all mTransitions
        for state in states:
            handler.write_uint32(state.mTransitions)

        # Third: all TransitionConditions
        for state in states:
            handler.write_int32(state.TransitionConditions)

        # Fourth: all TransitionMaps
        for state in states:
            handler.write_uint32(state.TransitionMaps)

        # Fifth: all mTransitionAttributes
        for state in states:
            handler.write_uint32(state.mTransitionAttributes)

        # Sixth: all mStatesEx
        for state in states:
            handler.write_uint32(state.mStatesEx)

    def _write_transitions_interleaved(self, handler: BinaryHandler, transitions: List[Transition]):
        """Write Transitions using interleaved array storage"""
        # First: all mStartTransitionEvent
        for trans in transitions:
            trans.mStartTransitionEvent.write(handler)

        # Second: all mStartState
        for trans in transitions:
            handler.write_uint32(trans.mStartState)

        # Third: all mStartStateTransition
        for trans in transitions:
            handler.write_int32(trans.mStartStateTransition)

        # Fourth: all mStartStateEx
        for trans in transitions:
            handler.write_uint32(trans.mStartStateEx)

    def _write_all_states_interleaved(self, handler: BinaryHandler, all_states: List[dict]):
        """Write AllStates using interleaved array storage"""
        # Write all 5 arrays
        for state in all_states:
            handler.write_uint32(state['field1'])
        for state in all_states:
            handler.write_uint32(state['field2'])
        for state in all_states:
            handler.write_int32(state['field3'])
        for state in all_states:
            handler.write_uint32(state['field4'])
        for state in all_states:
            handler.write_uint32(state['field5'])


@dataclass
class BHVT:
    """Behavior Tree structure"""
    magic: int = BHVT_MAGIC
    _unknown: int = 0

    # Offset fields (relative to BHVT start)
    node_offset: int = 0
    action_offset: int = 0
    selector_offset: int = 0
    selector_caller_offset: int = 0
    conditions_offset: int = 0
    transition_event_offset: int = 0
    expression_tree_conditions_offset: int = 0
    static_action_offset: int = 0
    static_selector_caller_offset: int = 0
    static_conditions_offset: int = 0
    static_transition_event_offset: int = 0
    static_expression_tree_conditions_offset: int = 0
    string_offset: int = 0
    resource_paths_offset: int = 0
    userdata_paths_offset: int = 0
    variable_offset: int = 0
    base_variable_offset: int = 0
    reference_prefab_game_objects_offset: int = 0

    # Parsed data
    nodes: List[BHVTNode] = field(default_factory=list)

    # Base offset for calculating absolute positions
    base_offset: int = 0

    def read(self, handler: BinaryHandler):
        self.base_offset = handler.tell

        self.magic = handler.read_uint32()
        if self.magic != BHVT_MAGIC:
            raise ValueError(f"Invalid BHVT magic: 0x{self.magic:08X}, expected 0x{BHVT_MAGIC:08X}")

        self._unknown = handler.read_uint32()

        # Read all offset fields
        self.node_offset = handler.read_uint64()
        self.action_offset = handler.read_uint64()
        self.selector_offset = handler.read_uint64()
        self.selector_caller_offset = handler.read_uint64()
        self.conditions_offset = handler.read_uint64()
        self.transition_event_offset = handler.read_uint64()
        self.expression_tree_conditions_offset = handler.read_uint64()
        self.static_action_offset = handler.read_uint64()
        self.static_selector_caller_offset = handler.read_uint64()
        self.static_conditions_offset = handler.read_uint64()
        self.static_transition_event_offset = handler.read_uint64()
        self.static_expression_tree_conditions_offset = handler.read_uint64()
        self.string_offset = handler.read_uint64()
        self.resource_paths_offset = handler.read_uint64()
        self.userdata_paths_offset = handler.read_uint64()
        self.variable_offset = handler.read_uint64()
        self.base_variable_offset = handler.read_uint64()
        self.reference_prefab_game_objects_offset = handler.read_uint64()

        # Read nodes
        self._read_nodes(handler)

    def _read_nodes(self, handler: BinaryHandler):
        """Read all BHVTNode structures"""
        node_data_abs = self.base_offset + self.node_offset
        string_pool_abs = self.base_offset + self.string_offset

        handler.seek(node_data_abs)
        node_count = handler.read_uint32()

        self.nodes = []
        for i in range(node_count):
            node = BHVTNode()
            node.read(handler, string_pool_abs)
            self.nodes.append(node)

    def get_absolute_offset(self, relative_offset: int) -> int:
        """Convert relative offset to absolute file offset"""
        return self.base_offset + relative_offset


class MotfsmFile:
    """MOTFSM file parser with lazy RSZ block loading"""
    EXTENSION = ".motfsm2"

    # Default RSZ type info path
    DEFAULT_RSZ_JSON = "resources/data/dumps/rszmhrise.json"

    def __init__(self):
        # Header fields
        self.version: int = 0
        self.magic: int = MOTFSM_MAGIC
        self.tree_data_offset: int = 0
        self.transition_map_tbl_offset: int = 0
        self.transition_data_tbl_offset: int = 0
        self.tree_info_ptr: int = 0
        self.transition_map_count: int = 0
        self.transition_data_count: int = 0
        self.start_transition_data_index: int = 0

        # Tree data size (at treeInfoPtr)
        self.tree_data_size: int = 0

        # BHVT structure
        self.bhvt: Optional[BHVT] = None

        # Raw data for lazy loading
        self._data: bytes = b''

        # RSZ type info (loaded lazily)
        self._rsz_type_info: Optional[Dict] = None
        self._rsz_type_info_path: str = ""

        # RSZ block collection (lazy loaded)
        self._rsz_blocks: Optional[RSZBlockCollection] = None

    @staticmethod
    def can_handle(data: bytes) -> bool:
        """Check if data is a valid MOTFSM file"""
        if len(data) < 0x3C:
            return False
        try:
            magic = struct.unpack_from('<I', data, 4)[0]
            return magic == MOTFSM_MAGIC
        except Exception:
            return False

    def set_rsz_type_info_path(self, path: str):
        """Set path to RSZ type info JSON file"""
        self._rsz_type_info_path = path
        self._rsz_type_info = None  # Reset to reload
        self._rsz_blocks = None  # Reset RSZ blocks

    def _load_rsz_type_info(self) -> Dict:
        """Load RSZ type info from JSON file (lazy)"""
        if self._rsz_type_info is not None:
            return self._rsz_type_info

        # Try configured path first
        paths_to_try = []
        if self._rsz_type_info_path:
            paths_to_try.append(self._rsz_type_info_path)

        # Try default paths
        paths_to_try.extend([
            self.DEFAULT_RSZ_JSON,
            os.path.join(os.path.dirname(__file__), "..", "..", self.DEFAULT_RSZ_JSON),
            "G:/REasy/resources/data/dumps/rszmhrise.json",  # Absolute fallback
        ])

        for path in paths_to_try:
            try:
                if os.path.exists(path):
                    with open(path, 'r', encoding='utf-8') as f:
                        self._rsz_type_info = json.load(f)
                        return self._rsz_type_info
            except Exception as e:
                continue

        # Return empty dict if not found
        self._rsz_type_info = {}
        return self._rsz_type_info

    def read(self, data: bytes) -> bool:
        """Read and parse MOTFSM file"""
        self._data = data
        handler = BinaryHandler(data)

        # Read header
        self.version = handler.read_uint32()
        self.magic = handler.read_uint32()

        if self.magic != MOTFSM_MAGIC:
            raise ValueError(f"Invalid MOTFSM magic: 0x{self.magic:08X}")

        handler.skip(8)  # 8 bytes padding

        self.tree_data_offset = handler.read_uint64()
        self.transition_map_tbl_offset = handler.read_uint64()
        self.transition_data_tbl_offset = handler.read_uint64()
        self.tree_info_ptr = handler.read_uint64()
        self.transition_map_count = handler.read_uint32()
        self.transition_data_count = handler.read_uint32()
        self.start_transition_data_index = handler.read_uint32()

        # Read tree data size
        handler.seek(self.tree_info_ptr)
        self.tree_data_size = handler.read_uint32()

        # Read BHVT structure
        handler.seek(self.tree_data_offset)
        self.bhvt = BHVT()
        self.bhvt.read(handler)

        return True

    @property
    def rsz_blocks(self) -> Optional[RSZBlockCollection]:
        """Get RSZ block collection (lazy loaded)"""
        if self._rsz_blocks is not None:
            return self._rsz_blocks

        if not self._data or not self.bhvt:
            return None

        # Build offset dictionary from BHVT
        offsets = {
            "action_offset": self.bhvt.action_offset,
            "selector_offset": self.bhvt.selector_offset,
            "selector_caller_offset": self.bhvt.selector_caller_offset,
            "conditions_offset": self.bhvt.conditions_offset,
            "transition_event_offset": self.bhvt.transition_event_offset,
            "expression_tree_conditions_offset": self.bhvt.expression_tree_conditions_offset,
            "static_action_offset": self.bhvt.static_action_offset,
            "static_selector_caller_offset": self.bhvt.static_selector_caller_offset,
            "static_conditions_offset": self.bhvt.static_conditions_offset,
            "static_transition_event_offset": self.bhvt.static_transition_event_offset,
            "static_expression_tree_conditions_offset": self.bhvt.static_expression_tree_conditions_offset,
        }

        rsz_info = self._load_rsz_type_info()
        self._rsz_blocks = RSZBlockCollection(
            self._data,
            self.bhvt.base_offset,
            offsets,
            rsz_info
        )
        return self._rsz_blocks

    def get_action_instance(self, index: int) -> Optional[RSZInstance]:
        """Get Action RSZ instance by index (from node.actions[i].index)"""
        blocks = self.rsz_blocks
        if blocks and blocks.actions:
            return blocks.actions.get_instance(index)
        return None

    def get_condition_instance(self, index: int) -> Optional[RSZInstance]:
        """Get Condition RSZ instance by index"""
        blocks = self.rsz_blocks
        if blocks and blocks.conditions:
            return blocks.conditions.get_instance(index)
        return None

    def get_selector_instance(self, index: int) -> Optional[RSZInstance]:
        """Get Selector RSZ instance by index"""
        blocks = self.rsz_blocks
        if blocks and blocks.selectors:
            return blocks.selectors.get_instance(index)
        return None

    def get_static_condition_instance(self, index: int) -> Optional[RSZInstance]:
        """Get StaticCondition RSZ instance by index"""
        blocks = self.rsz_blocks
        if blocks and blocks.static_conditions:
            return blocks.static_conditions.get_instance(index)
        return None

    def get_node_by_index(self, index: int) -> Optional[BHVTNode]:
        """Get node by index"""
        if self.bhvt and 0 <= index < len(self.bhvt.nodes):
            return self.bhvt.nodes[index]
        return None

    def get_node_by_name(self, name: str) -> Optional[BHVTNode]:
        """Get node by name"""
        if self.bhvt:
            for node in self.bhvt.nodes:
                if node.name == name:
                    return node
        return None

    @property
    def node_count(self) -> int:
        """Get total node count"""
        return len(self.bhvt.nodes) if self.bhvt else 0

    def rebuild(self) -> bytes:
        """
        Rebuild MOTFSM file with modifications.
        Uses in-place modification strategy - only modifies field values, keeps file structure intact.
        """
        if not self._data:
            raise ValueError("No original data to rebuild from")

        # Create a mutable copy of the original data
        output_data = bytearray(self._data)
        handler = BinaryHandler(output_data)

        # Write back modified Node fields
        if self.bhvt:
            for node in self.bhvt.nodes:
                # Write basic node fields if they have offset tracking
                if hasattr(node, '_id_hash_offset'):
                    handler.seek(node._id_hash_offset)
                    handler.write_uint32(node.id_hash)

                if hasattr(node, '_ex_id_offset'):
                    handler.seek(node._ex_id_offset)
                    handler.write_uint32(node.ex_id)

                if hasattr(node, '_parent_offset'):
                    handler.seek(node._parent_offset)
                    handler.write_int32(node.parent)

                if hasattr(node, '_priority_offset'):
                    handler.seek(node._priority_offset)
                    handler.write_int32(node.priority)

                # Write back Children fields
                for child in node.children:
                    if hasattr(child, '_id_hash_offset'):
                        handler.seek(child._id_hash_offset)
                        handler.write_uint32(child.id_hash)
                    if hasattr(child, '_ex_id_offset'):
                        handler.seek(child._ex_id_offset)
                        handler.write_uint32(child.ex_id)
                    if hasattr(child, '_index_offset'):
                        handler.seek(child._index_offset)
                        handler.write_int32(child.index)

                # Write back Selector fields
                if hasattr(node, '_selector_id_offset'):
                    handler.seek(node._selector_id_offset)
                    handler.write_int32(node.selector_id)

                if hasattr(node, '_selector_caller_condition_id_offset'):
                    handler.seek(node._selector_caller_condition_id_offset)
                    handler.write_int32(node.selector_caller_condition_id)

                # Write back Action fields
                for action in node.actions:
                    if hasattr(action, '_id_hash_offset'):
                        handler.seek(action._id_hash_offset)
                        handler.write_uint32(action.id_hash)
                    if hasattr(action, '_index_offset'):
                        handler.seek(action._index_offset)
                        handler.write_int32(action.index)

                # Write back modified State fields
                for state in node.states:
                    if hasattr(state, '_mTransitions_offset'):
                        handler.seek(state._mTransitions_offset)
                        handler.write_uint32(state.mTransitions)
                    if hasattr(state, '_TransitionConditions_offset'):
                        handler.seek(state._TransitionConditions_offset)
                        handler.write_int32(state.TransitionConditions)
                    if hasattr(state, '_TransitionMaps_offset'):
                        handler.seek(state._TransitionMaps_offset)
                        handler.write_uint32(state.TransitionMaps)
                    if hasattr(state, '_mTransitionAttributes_offset'):
                        handler.seek(state._mTransitionAttributes_offset)
                        handler.write_uint32(state.mTransitionAttributes)
                    if hasattr(state, '_mStatesEx_offset'):
                        handler.seek(state._mStatesEx_offset)
                        handler.write_uint32(state.mStatesEx)

                # Write back modified Transition fields
                for trans in node.transitions:
                    if hasattr(trans, '_mStartState_offset'):
                        handler.seek(trans._mStartState_offset)
                        handler.write_uint32(trans.mStartState)
                    if hasattr(trans, '_mStartStateTransition_offset'):
                        handler.seek(trans._mStartStateTransition_offset)
                        handler.write_int32(trans.mStartStateTransition)
                    if hasattr(trans, '_mStartStateEx_offset'):
                        handler.seek(trans._mStartStateEx_offset)
                        handler.write_uint32(trans.mStartStateEx)

        # Write back modified RSZ field values (ONLY those marked as modified)
        blocks = self.rsz_blocks
        if blocks:
            # Iterate through all blocks
            for block_name in ['actions', 'selectors', 'conditions', 'static_conditions',
                              'static_actions', 'transition_events', 'static_transition_events']:
                block = getattr(blocks, block_name, None)
                if block:
                    # Iterate through all instances
                    for i in range(block.instance_count):
                        instance = block.get_instance(i)
                        if instance and instance.fields:
                            # Write each field (write_value_to_buffer checks _modified flag)
                            for field in instance.fields:
                                field.write_value_to_buffer(handler)

        return bytes(output_data)
