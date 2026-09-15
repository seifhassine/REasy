from __future__ import annotations

from dataclasses import dataclass
from typing import Hashable, Protocol

from ..mot.model import AnimationNode, Joint, Motion
from .model import (
    BoundJoint,
    DiagnosticSeverity,
    EvaluationDiagnostic,
    MotionRigBinding,
    Rig,
    RigJoint,
)


class JointBindingStrategy(Protocol):
    def motion_name_key(self, name: str) -> Hashable: ...

    def motion_key(self, joint: Joint) -> Hashable: ...

    def rig_key(self, joint: RigJoint) -> Hashable: ...


@dataclass(frozen=True, slots=True)
class ExactNameBindingStrategy:
    case_sensitive: bool = True

    def motion_name_key(self, name: str) -> str:
        return name if self.case_sensitive else name.casefold()

    def motion_key(self, joint: Joint) -> str:
        return self.motion_name_key(joint.name)

    def rig_key(self, joint: RigJoint) -> str:
        return self.motion_name_key(joint.name)


def bind_motion(motion: Motion, rig: Rig, strategy: JointBindingStrategy) -> MotionRigBinding:
    diagnostics: list[EvaluationDiagnostic] = []
    source_joints = tuple(motion.skeleton.joints) if motion.skeleton else ()
    nodes_by_joint: dict[int, AnimationNode] = {}
    for node in motion.animation_nodes:
        identity = id(node.joint)
        if identity in nodes_by_joint:
            diagnostics.append(EvaluationDiagnostic(
                DiagnosticSeverity.ERROR,
                "duplicate_animation_node",
                f"motion joint {node.joint.name!r} owns more than one animation node",
            ))
        else:
            nodes_by_joint[identity] = node

    source_by_key: dict[Hashable, list[Joint]] = {}
    for joint in source_joints:
        source_by_key.setdefault(strategy.motion_key(joint), []).append(joint)
    target_by_key: dict[Hashable, list[int]] = {}
    for index, joint in enumerate(rig.joints):
        target_by_key.setdefault(strategy.rig_key(joint), []).append(index)

    bound_by_target: dict[int, Joint] = {}
    bound_source_ids: set[int] = set()
    ignored_source_ids: set[int] = set()
    for key, joints in source_by_key.items():
        targets = target_by_key.get(key, [])
        if len(joints) > 1:
            diagnostics.append(EvaluationDiagnostic(
                DiagnosticSeverity.ERROR,
                "ambiguous_motion_joint",
                f"{len(joints)} motion joints share binding key {key!r}",
            ))
            continue
        source = joints[0]
        if not targets:
            ignored_source_ids.add(id(source))
            diagnostics.append(EvaluationDiagnostic(
                DiagnosticSeverity.INFO,
                "unmatched_motion_joint",
                f"motion joint {source.name!r} has no target-rig match and is ignored",
            ))
            continue
        if len(targets) > 1:
            diagnostics.append(EvaluationDiagnostic(
                DiagnosticSeverity.ERROR,
                "ambiguous_rig_joint",
                f"motion joint {source.name!r} matches {len(targets)} target-rig joints",
            ))
            continue
        target = targets[0]
        bound_by_target[target] = source
        bound_source_ids.add(id(source))

    for node in motion.animation_nodes:
        source_id = id(node.joint)
        if source_id not in bound_source_ids and source_id not in ignored_source_ids:
            diagnostics.append(EvaluationDiagnostic(
                DiagnosticSeverity.ERROR,
                "unbound_animation_node",
                f"animation node {node.joint.name!r} cannot be evaluated on the target rig",
            ))

    joints = tuple(
        BoundJoint(
            index,
            source := bound_by_target.get(index),
            nodes_by_joint.get(id(source)) if source is not None else None,
        )
        for index in range(len(rig.joints))
    )
    return MotionRigBinding(rig, motion, joints, tuple(diagnostics))


def binding_reaches_dominant_branch(binding: MotionRigBinding) -> bool:
    """Reject bindings that overlap only through shared placement roots.

    A multi-object MOTLIST may contain motions for unrelated rigs which still
    share ``root``-style controls.  The largest animated branch directly below
    a source root identifies the motion's principal skeletal target without
    relying on game-specific joint names or an arbitrary coverage threshold.
    """

    motion = binding.motion
    animated = {id(node.joint) for node in motion.animation_nodes}
    if not animated:
        return True
    source_joints = tuple(motion.skeleton.joints) if motion.skeleton else ()
    if not source_joints:
        return False

    source_ids = {id(joint) for joint in source_joints}
    descendant_counts = dict.fromkeys(source_ids, 0)
    for node in motion.animation_nodes:
        joint = node.joint
        seen: set[int] = set()
        while joint is not None and id(joint) in source_ids and id(joint) not in seen:
            identity = id(joint)
            descendant_counts[identity] += 1
            seen.add(identity)
            joint = joint.parent

    branches: list[Joint] = []
    for root in (joint for joint in source_joints if joint.parent is None):
        children = [
            joint
            for joint in source_joints
            if joint.parent is root and descendant_counts[id(joint)]
        ]
        branches.extend(children or ([root] if id(root) in animated else []))
    if not branches:
        return False

    largest = max(descendant_counts[id(joint)] for joint in branches)
    bound_source_ids = {
        id(joint.motion_joint)
        for joint in binding.joints
        if joint.motion_joint is not None
    }
    return any(
        descendant_counts[id(joint)] == largest
        and id(joint) in bound_source_ids
        for joint in branches
    )
