"""See docs/internals/mottree-runtime.md, Playing a tree."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy

lazy import numpy as np

lazy from file_handlers.uvar.uvar_file import UVarFile

from .model import Motion, MotionTree, TreeLink, TreeNode, TreeParam
from .pose import Rig, bound
from .tree_pose import TreePose
from .tree_runtime import F, TreeContext, TreeRuntime, motion_choices, node_params

_STEP = 1.0


def motion_tree(bank: int, motion_id: int) -> MotionTree:
    node = TreeNode("MotionNode", params=[TreeParam(6, 0, bank, "BankID"), TreeParam(6, 0, motion_id, "MotionID")])
    return MotionTree(0, "", [node], root=node)


def change_tree(changes, tree_of: Callable[[int, int], MotionTree | None] = lambda _bank, _motion: None) -> MotionTree:
    node = TreeNode("LayerChangeNode", params=[TreeParam(6, 0, tuple(
        (frame, frames, mode, curve, *wrap) for frame, _bank, _motion, frames, mode, curve, *wrap in changes),
        "Changes")])
    tree = MotionTree(0, "", [node], root=node)
    for pin, (_frame, bank, motion, *_rest) in enumerate(changes):
        if (row := tree_of(bank, motion)) is not None:
            row = deepcopy(row)
            tree.version, tree.nodes, tree.links = row.version, tree.nodes + row.nodes, tree.links + row.links
        else:
            row = MotionTree(0, "", [TreeNode("MotionNode", params=[
                TreeParam(6, 0, bank, "BankID"), TreeParam(6, 0, motion, "MotionID")])])
            row.root = row.nodes[0]
            tree.nodes.append(row.root)
        tree.links.insert(pin, TreeLink(node, pin, row.root, 0, 1))
    return tree


def uvar_variables(data: bytes | None) -> dict[bytes, tuple[str, object]]:
    files, found = [] if data is None else [UVarFile(data)], {}
    while files:
        held = files.pop()
        files.extend(held.embedded_uvars)
        for variable in held.variables:
            if variable.guid is not None:
                value = variable.value
                found.setdefault(variable.guid.bytes, (variable.name, (
                    value[0] if isinstance(value, list) else value.x) if variable.is_vec3 else value))
    return found


def tree_variables(read: Callable[[str], bytes | None], tree: MotionTree) -> list[tuple[bytes, str, object]]:
    found: dict[bytes, tuple[str, object]] = {}
    for node in tree.nodes:
        path = node_params(node).get("Asset") if node.class_name == "VariablesNode" else None
        found = uvar_variables(read(path) if path else None) | found
    read_by = dict.fromkeys(link.guid for link in tree.links
                            if link.type == 2 and link.child.class_name == "VariablesNode" and link.guid)
    return [(guid, *found[guid]) for guid in read_by
            if guid in found and isinstance(found[guid][1], (bool, int, float))]


class TreeSampler:
    motion = None

    def __init__(self, tree: MotionTree, motion_of: Callable[[int, int], Motion | None], variables: dict,
                 joints_of: Callable[[Motion], frozenset] | None = None,
                 mirror_of: Callable[[int, int], bool] | None = None,
                 reverse_of: Callable[[int, int], bool] | None = None,
                 mask_of: Callable[[int, int], tuple[int, str]] | None = None,
                 values_of: Callable[[float], dict] | None = None):
        self.tree, self.variables, self._joints_of, self._values_of = tree, variables, joints_of, values_of
        self._motion_of, self._mirror_of, self._reverse_of, self._mask_of = motion_of, mirror_of, reverse_of, mask_of
        self._bases: dict = {}
        self.motions = self._played(tree)
        self._start()
        self._slotted()

    def _slotted(self) -> None:
        self.hashes = tuple(self.context.slots)
        self.weights = np.ones(len(self.hashes), np.float32)
        self.properties = {value for motion in self.motions for value in self.context.sampler(motion)[0].properties}

    def _played(self, tree: MotionTree) -> list[Motion]:
        found: dict[int, Motion] = {}
        for node in tree.nodes:
            params = node_params(node)
            if node.class_name in ("MotionNode", "SelectMotionNode"):
                for bank, motion_id in [(params.get("BankID", 0), params.get("MotionID", 0)), *motion_choices(node)]:
                    if (motion := self._motion_of(bank, motion_id)) is not None:
                        found.setdefault(id(motion), motion)
        return list(found.values())

    def _start(self, rig: Rig | None = None) -> None:
        held = getattr(self, "context", None)
        self.context = TreeContext(self._motion_of, self.variables)
        self.context.joints_of = self._joints_of
        if self._mirror_of is not None:
            self.context.mirror_of = self._mirror_of
        if self._reverse_of is not None:
            self.context.reverse_of = self._reverse_of
        if self._mask_of is not None:
            self.context.mask_of = self._mask_of
        if held is not None:
            self.context.slots = dict(held.slots)
            for name in ("rest_of", "root_hash", "rig", "mirror_table", "parent_of", "parent_rotations", "mask",
                         "groups"):
                setattr(self.context, name, getattr(held, name))
        for motion in self.motions:
            self.context.sampler(motion)
        if rig is not None:
            self.context.rest_of = {value: tuple(part[row] for part in rig.base)
                                    for row, value in enumerate(rig.hashes)}
            self.context.root_hash, self.context.rig = rig.hashes[rig.root] if rig.root >= 0 else None, rig
            self.context.parent_of = {value: rig.hashes[parent] for value, parent in zip(rig.hashes, rig.parents)
                                      if parent >= 0}
        self.runtime = TreeRuntime(self.tree, self.context)
        if rig is not None:  # a mirror swaps a slotted joint with its symmetric one
            slots = self.context.slots
            for partner in [rig.hashes[other] for value, other in zip(rig.hashes, rig.symmetry)
                            if other >= 0 and value in slots]:
                slots.setdefault(partner, len(slots))

    def set_rig(self, rig: Rig, mask: dict | None = None, groups: dict | None = None,
                mirror_table: tuple | None = None) -> None:
        self.context.mask, self.context.groups, self.context.mirror_table = mask, groups or {}, mirror_table
        self._start(rig)
        self._slotted()

    def set_joints(self, joints_of: Callable[[Motion], frozenset]) -> None:
        self._joints_of = joints_of
        self._start()
        self._slotted()

    def refresh(self) -> None:
        self.runtime.step(0.0)

    def _to(self, frame: float) -> None:
        target = F(frame)
        if target < self.runtime.time:
            self._start()
        while self.runtime.time + F(_STEP) <= target:
            self._step(F(_STEP))
        if self.runtime.time < target:
            self._step(target - self.runtime.time)

    def _step(self, delta) -> None:
        if self._values_of is not None:
            self.context.variables = self.variables | self._values_of(float(self.runtime.time + delta))
        self.runtime.step(delta)

    def sample(self, frame: float, base: tuple | None = None) -> tuple[list[np.ndarray], list[np.ndarray]]:
        self._to(frame)
        self.context.base = None if base is None else self._based(*base)
        pose = self.runtime.pose()
        count = len(self.hashes)
        return [values[:count] for values in pose.values], [pose.weights[:count, component] for component in range(3)]

    def _based(self, hashes: tuple[int, ...], pose: TreePose) -> TreePose:
        count = len(self.context.slots)
        if (key := (hashes, count)) not in self._bases:
            self._bases = {key: bound(hashes, tuple(self.context.slots))}
        rows, slots = self._bases[key]
        based = TreePose(count)
        for mine, rest, theirs in zip(based.values, self.context.rest, pose.values):
            mine[:] = rest
            mine[slots] = theirs[rows]
        based.weights[slots] = pose.weights[rows]
        return based

    def property_values(self, frame: float) -> dict[int, float]:
        self._to(frame)
        lead = self.runtime.leading()
        if lead is None:
            return {}
        return self.context.sampler(lead.motion)[0].property_values(float(lead.data_frame()))

    @property
    def end_frame(self) -> float:
        return float(self.runtime.root.end())
