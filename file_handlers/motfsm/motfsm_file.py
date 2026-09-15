"""MOTFSM containers with structural BHVT layout detection."""
from dataclasses import dataclass, field, replace
import struct

from .fields import FieldBindings, SCALAR_FORMATS
from .rsz_adapter import RSZBlocks, BLOCK_NAMES
from .references import References
from .registry_detection import detect_registry

MOTFSM_MAGIC = 0x3273666D
BHVT_MAGIC = 0x54564842


@dataclass(frozen=True)
class NodeLayout:
    transition_event_lists: bool | None
    transition_state_ex: bool


@dataclass
class IndexList:
    values: list = field(default_factory=list)


@dataclass
class ChildNode:
    id_hash: int = 0
    ex_id: int = 0
    condition_id: int = -1


@dataclass
class Action:
    id_hash: int = 0
    ex_id: int = 0


@dataclass
class State:
    mStates: IndexList = field(default_factory=IndexList)
    mTransitions: int = 0
    TransitionConditions: int = -1
    TransitionMaps: int = 0
    mTransitionAttributes: int = 0
    mStatesEx: int = 0


@dataclass
class Transition:
    mStartTransitionEvent: IndexList = field(default_factory=IndexList)
    mStartState: int = 0
    mStartStateTransition: int = -1
    mStartStateEx: int = 0


@dataclass
class AllState:
    mAllState: int = 0
    mAllTransition: int = -1
    mAllTransitionID: int = 0
    mAllStateEx: int = 0
    mAllTransitionAttributes: int = 0


@dataclass
class BHVTNode:
    id_hash: int = 0
    ex_id: int = 0
    name_index: int = 0
    name: str = ""
    parent: int = 0
    parent_ex: int = 0
    children: list = field(default_factory=list)
    selector_id: int = -1
    selector_callers: list = field(default_factory=list)
    selector_caller_condition_id: int = -1
    actions: list = field(default_factory=list)
    priority: int = 0
    node_attribute: int = 0
    work_flags: int = 0
    name_hash: int = 0
    fullname_hash: int = 0
    tags: list = field(default_factory=list)
    is_branch: int = 0
    is_end: int = 0
    states: list = field(default_factory=list)
    transitions: list = field(default_factory=list)
    all_states: list = field(default_factory=list)
    reference_tree_index: int = -1

    @property
    def is_fsm(self):
        return bool(self.node_attribute & 0x20)

    @property
    def has_reference_tree(self):
        return bool(self.node_attribute & 4)


@dataclass
class BHVT:
    base_offset: int
    offsets: dict
    nodes: list = field(default_factory=list)
    action_ex_ids: list = field(default_factory=list)
    static_action_ex_ids: list = field(default_factory=list)


class _Reader:
    def __init__(self, document, start, end):
        if not 0 <= start <= end <= len(document.source):
            raise ValueError(f"Invalid MOTFSM section: 0x{start:X}..0x{end:X}")
        self.document = document
        self.pos = start
        self.end = end

    def read(self, fmt):
        size = struct.calcsize("<" + fmt)
        if self.pos + size > self.end:
            raise ValueError(f"MOTFSM read exceeds section at 0x{self.pos:X}")
        values = struct.unpack_from("<" + fmt, self.document.source, self.pos)
        self.pos += size
        return values[0] if len(values) == 1 else values

    def count(self, minimum_size=4):
        count = self.read("I")
        if count > (self.end - self.pos) // minimum_size:
            raise ValueError(f"Invalid MOTFSM count {count} at 0x{self.pos - 4:X}")
        return count

    def scalar(self, owner, name, kind, *, layout_mask=0):
        offset = self.pos
        value = self.read(SCALAR_FORMATS[kind])
        if isinstance(name, int):
            owner[name] = value
        else:
            setattr(owner, name, value)
        self.document.bindings.bind(owner, name, offset, kind, layout_mask=layout_mask)

    def list(self, kind):
        values = [0] * self.count()
        for index in range(len(values)):
            self.scalar(values, index, kind)
        return values

    def columns(self, objects, columns):
        for name, kind in columns:
            for obj in objects:
                self.scalar(obj, name, kind)


class MotfsmFile:
    EXTENSION = ".motfsm2"

    def __init__(self):
        self.source = b""
        self.version = 0
        self.bhvt = None
        self.rsz_blocks = None
        self.bindings = FieldBindings(b"")
        self._registry_path = ""
        self._type_registry = None
        self._registry_detected = False
        self.layout = None
        self.references = References(self)

    @staticmethod
    def can_handle(data):
        return len(data) >= 64 and struct.unpack_from("<I", data, 4)[0] == MOTFSM_MAGIC

    def set_rsz_type_info_path(self, path):
        self._registry_path = str(path)
        self._type_registry = None
        self._registry_detected = False

    @property
    def type_registry(self):
        if not self._registry_detected:
            self._type_registry = detect_registry(self.source, self.rsz_blocks, self._registry_path)
            self._registry_detected = True
        return self._type_registry

    def read(self, data):
        if not self.can_handle(data):
            raise ValueError("Invalid MOTFSM header")
        self.source = bytes(data)
        self.bindings = FieldBindings(self.source)
        self._type_registry = None
        self._registry_detected = False
        header = _Reader(self, 0, len(data))
        self.version, magic = header.read("II")
        header.read("Q")
        (self.tree_data_offset, self.transition_map_tbl_offset,
         self.transition_data_tbl_offset, self.tree_info_ptr) = header.read("4Q")
        (self.transition_map_count, self.transition_data_count,
         self.start_transition_data_index) = header.read("3I")
        self.tree_data_size = _Reader(self, self.tree_info_ptr, len(data)).read("I")
        tree_end = self.tree_data_offset + self.tree_data_size
        reader = _Reader(self, self.tree_data_offset, tree_end)
        if reader.read("I") != BHVT_MAGIC:
            raise ValueError("Invalid BHVT magic")
        reader.read("I")
        node_offset = struct.unpack_from('<Q', self.source, reader.pos)[0]
        if node_offset % 8 or node_offset < 8 + 17 * 8 or node_offset > self.tree_data_size:
            raise ValueError("Invalid BHVT header extent")
        offset_count = (node_offset - 8) // 8
        names = ("nodes",) + BLOCK_NAMES + (
            "strings", "resource_paths", "userdata_paths", "variables",
            "base_variables",
        )
        if offset_count >= 18:
            names += ("reference_prefab_game_objects",)
        names += tuple(f"section_{i}" for i in range(18, offset_count))
        relative_offsets = {name: reader.read("Q") for name in names}
        offsets = {name: self.tree_data_offset + offset for name, offset in relative_offsets.items() if offset}
        if any(name not in offsets for name in ("nodes", "strings") + BLOCK_NAMES):
            raise ValueError("Missing BHVT node, string or RSZ section")
        if any(not reader.pos <= offset < tree_end for offset in offsets.values()):
            raise ValueError("BHVT section offset outside tree")
        self.bhvt = BHVT(self.tree_data_offset, offsets)
        section_starts = sorted(set(offsets.values()) | {tree_end})
        ends = {offset: section_starts[index + 1] for index, offset in enumerate(section_starts[:-1])}
        self.rsz_blocks = RSZBlocks(self, [offsets[name] for name in BLOCK_NAMES], ends)
        candidates, failures = [], []
        for event_lists in (True, False):
            self.bindings = FieldBindings(self.source)
            self.bhvt = BHVT(self.tree_data_offset, offsets)
            layout = NodeLayout(event_lists, offset_count >= 18)
            try:
                self._read_nodes(_Reader(self, offsets["nodes"], ends[offsets["nodes"]]), layout)
                candidates.append((layout, self.bhvt, self.bindings))
            except (ValueError, struct.error) as exc:
                failures.append(f"{'list' if event_lists else 'scalar'} events: {exc}")
        if not candidates:
            raise ValueError("Cannot parse BHVT node layout: " + "; ".join(failures))
        if len(candidates) > 1:
            # Zero event slots encode both an empty list and a null scalar hash.
            # Accept this only when every other parsed value agrees; preserve the
            # empty slots without claiming to know their unobservable encoding.
            scalar_nodes = [replace(node, transitions=[replace(transition,
                mStartTransitionEvent=IndexList([] if transition.mStartTransitionEvent.values == [0]
                    else transition.mStartTransitionEvent.values)) for transition in node.transitions])
                for node in candidates[1][1].nodes]
            if candidates[0][1].nodes != scalar_nodes:
                raise ValueError("Ambiguous BHVT node layout")
            layout, tree, bindings = candidates[0]
            candidates = [(replace(layout, transition_event_lists=None), tree, bindings)]
        self.layout, self.bhvt, self.bindings = candidates[0]
        self.references.invalidate()
        return True

    def _name(self, index):
        start = self.bhvt.offsets["strings"]
        pool = _Reader(self, start, self.tree_data_offset + self.tree_data_size)
        count = pool.read("I")
        end = start + 4 + count * 2
        if end > pool.end or index >= count:
            raise ValueError(f"Invalid BHVT name index: {index}")
        begin = start + 4 + index * 2
        string = _Reader(self, begin, end)
        while string.read("H"):
            pass
        return self.source[begin:string.pos - 2].decode("utf-16le")

    def _read_nodes(self, reader, layout):
        for _ in range(reader.count(48)):
            node = BHVTNode()
            reader.columns([node], (("id_hash", "u32"), ("ex_id", "u32")))
            node.name_index = reader.read("I")
            node.name = self._name(node.name_index)
            reader.columns([node], (("parent", "u32"), ("parent_ex", "u32")))
            node.children = [ChildNode() for _ in range(reader.count(12))]
            reader.columns(node.children, (("id_hash", "u32"), ("ex_id", "u32"), ("condition_id", "s32")))
            reader.scalar(node, "selector_id", "s32")
            node.selector_callers = reader.list("s32")
            reader.scalar(node, "selector_caller_condition_id", "s32")
            node.actions = [Action() for _ in range(reader.count(8))]
            reader.columns(node.actions, (("id_hash", "u32"), ("ex_id", "u32")))
            reader.scalar(node, "priority", "s32")
            reader.scalar(node, "node_attribute", "u16", layout_mask=0x24)
            reader.scalar(node, "work_flags", "u16")
            if node.is_fsm:
                reader.columns([node], (("name_hash", "u32"), ("fullname_hash", "u32")))
                node.tags = reader.list("u32")
                reader.columns([node], (("is_branch", "u8"), ("is_end", "u8")))
            node.states = [State() for _ in range(reader.count(24))]
            for state in node.states:
                state.mStates.values = reader.list("s32")
            reader.columns(node.states, (("mTransitions", "u32"), ("TransitionConditions", "s32"),
                ("TransitionMaps", "u32"), ("mTransitionAttributes", "u32"), ("mStatesEx", "u32")))
            node.transitions = [Transition() for _ in range(reader.count(12))]
            for transition in node.transitions:
                if layout.transition_event_lists:
                    transition.mStartTransitionEvent.values = reader.list("s32")
                else:
                    transition.mStartTransitionEvent.values = [0]
                    reader.scalar(transition.mStartTransitionEvent.values, 0, "u32")
            reader.columns(node.transitions, (("mStartState", "u32"), ("mStartStateTransition", "s32")))
            if layout.transition_state_ex:
                reader.columns(node.transitions, (("mStartStateEx", "u32"),))
            if not node.has_reference_tree:
                node.all_states = [AllState() for _ in range(reader.count(20))]
                reader.columns(node.all_states, (("mAllState", "u32"), ("mAllTransition", "s32"),
                    ("mAllTransitionID", "u32"), ("mAllStateEx", "u32"), ("mAllTransitionAttributes", "s32")))
            reader.scalar(node, "reference_tree_index", "s32")
            self.bhvt.nodes.append(node)
        self.bhvt.action_ex_ids = reader.list("u32")
        self.bhvt.static_action_ex_ids = reader.list("u32")
        for name, values in (("actions", self.bhvt.action_ex_ids), ("static_actions", self.bhvt.static_action_ex_ids)):
            block = self.rsz_blocks.get_block(name)
            count = _Reader(self, block.offset + 8, block.end).read("I")
            if count != len(values):
                raise ValueError(f"{name} extension table count does not match RSZ object table")
        if reader.end - reader.pos >= 16:
            raise ValueError(f"Unconsumed BHVT node data at 0x{reader.pos:X}")

    @property
    def node_count(self):
        return len(self.bhvt.nodes) if self.bhvt else 0

    def get_node_by_index(self, index):
        if not 0 <= index < self.node_count:
            raise IndexError(f"Invalid BHVT node index: {index}")
        return self.bhvt.nodes[index]

    def edit_field(self, binding, value):
        if self.bindings.get(binding.owner, binding.attribute) is not binding:
            raise ValueError("Field does not belong to this document")
        binding.set_value(value)
        self.references.invalidate()

    def rebuild(self):
        if not self.source:
            raise ValueError("No MOTFSM data loaded")
        return self.bindings.rebuild()

    def accept_changes(self):
        self.source = self.rebuild()
        self.bindings.accept_changes(self.source)
