from __future__ import annotations

import math

from utils.hash_util import murmur3_hash

from ..binary import ReadContext
from ..errors import MotionParseError
from ..transform_channels import TransformChannels
from .joint_map import (
    ExtraJointFlags,
    JointMapDefinition,
    JointMapAttributes,
    JointMapExtraJoint,
    JointMapJoint,
    JointMaskEntry,
    JointMaskGroup,
)


JMAP_MAGIC = 0x70616D6A


def parse_dmc5_joint_map(data: bytes, *, label: str = "DMC5 JMAP") -> JointMapDefinition:
    """Parse the bounded v10 mask index used by runtime TreeLayer slots."""

    context = ReadContext.from_bytes(data, label)
    context.require(0, 0x40, "JMAP header")
    version = context.u32(0, "JMAP version")
    magic = context.u32(4, "JMAP magic")
    if version != 10 or magic != JMAP_MAGIC:
        raise MotionParseError(
            f"{label}: expected JMAP v10, got v{version}/0x{magic:08X}"
        )
    bone_table = context.u64(0x10, "bone table pointer")
    after_bone_header = context.u64(0x18, "post-bone header pointer")
    mask_table = context.u64(0x20, "mask-group table pointer")
    expression_table = context.u64(0x28, "expression table pointer")
    extra_table = context.u64(0x30, "extra-joint table pointer")
    bone_count = context.u16(0x38, "bone count")
    group_count = context.u16(0x3A, "mask-group count")
    raw_attributes = context.u32(0x3C, "joint-map attributes")
    if raw_attributes & ~int(JointMapAttributes.DEFORM):
        raise MotionParseError(
            f"{label}: unsupported joint-map attributes 0x{raw_attributes:X}"
        )
    attributes = JointMapAttributes(raw_attributes)
    if group_count and not mask_table:
        raise MotionParseError(f"{label}: nonempty mask groups have a null table")
    if bone_count and not bone_table:
        raise MotionParseError(f"{label}: nonempty bones have a null table")
    if not after_bone_header:
        raise MotionParseError(f"{label}: null v10 post-bone header pointer")
    context.require(bone_table, bone_count * 0x30, "bone table")
    context.require(mask_table, group_count * 0x18, "mask-group table")
    context.require(after_bone_header, 0x30, "post-bone header")
    after_bone_data = context.u64(
        after_bone_header,
        "post-bone data pointer",
    )
    if not after_bone_data:
        raise MotionParseError(f"{label}: null v10 post-bone data pointer")
    context.require_zero(
        after_bone_header + 8,
        after_bone_header + 0x30,
        "post-bone header reserved bytes",
    )
    context.require_zero(
        after_bone_data,
        after_bone_data + 60 * 4,
        "unsupported v10 post-bone data",
    )

    joints: list[JointMapJoint] = []
    for index in range(bone_count):
        record = bone_table + index * 0x30
        translation = tuple(
            context.f32(record + axis * 4, f"joint {index} translation")
            for axis in range(3)
        )
        rotation = tuple(
            context.f32(record + 0x10 + axis * 4, f"joint {index} rotation")
            for axis in range(4)
        )
        raw_parent = context.u16(record + 0x20, f"joint {index} parent")
        parent_index = None if raw_parent == 0xFFFF else raw_parent
        if parent_index is not None and parent_index >= bone_count:
            raise MotionParseError(f"{label}: joint {index} has an invalid parent")
        if not all(math.isfinite(value) for value in (*translation, *rotation)):
            raise MotionParseError(f"{label}: joint {index} has a nonfinite bind pose")
        if sum(value * value for value in rotation) <= 1e-16:
            raise MotionParseError(f"{label}: joint {index} has a zero rotation")
        context.require_zero(
            record + 0xC,
            record + 0x10,
            f"joint {index} position padding",
        )
        context.require_zero(
            record + 0x22,
            record + 0x24,
            f"joint {index} parent padding",
        )
        context.require_zero(
            record + 0x28,
            record + 0x30,
            f"joint {index} record padding",
        )
        joints.append(
            JointMapJoint(
                translation,
                rotation,
                parent_index,
                context.u32(record + 0x24, f"joint {index} name hash"),
            )
        )

    if expression_table:
        context.require(expression_table, 0x20, "joint-expression header")
        expression_count = context.i32(
            expression_table + 0x18,
            "joint-expression count",
        )
        if expression_count < 0:
            raise MotionParseError(f"{label}: negative joint-expression count")
        if expression_count:
            raise MotionParseError(
                f"{label}: JMAP v10 joint expressions are unsupported "
                f"({expression_count} records)"
            )
        context.require_zero(
            expression_table + 0x1C,
            expression_table + 0x20,
            "joint-expression header padding",
        )

    groups: list[JointMaskGroup] = []
    for index in range(group_count):
        record = mask_table + index * 0x18
        hash_table = context.u64(record, f"mask group {index} hash pointer")
        value_table = context.u64(record + 8, f"mask group {index} value pointer")
        group_id = context.u32(record + 0x10, f"mask group {index} id")
        joint_count = context.u16(record + 0x14, f"mask group {index} joint count")
        context.require(hash_table, joint_count * 4, f"mask group {index} hashes")
        context.require(value_table, joint_count, f"mask group {index} values")
        entries = []
        for item in range(joint_count):
            raw_channels = context.u8(
                value_table + item,
                f"mask group {index} transform channels {item}",
            )
            if raw_channels & ~int(TransformChannels.ALL):
                raise MotionParseError(
                    f"{label}: mask group {group_id} joint {item} has "
                    f"unsupported transform channels 0x{raw_channels:02X}"
                )
            entries.append(
                JointMaskEntry(
                    context.u32(
                        hash_table + item * 4,
                        f"mask group {index} joint hash {item}",
                    ),
                    TransformChannels(raw_channels),
                )
            )
        groups.append(
            JointMaskGroup(
                group_id,
                tuple(entries),
            )
        )

    extra_joints: list[JointMapExtraJoint] = []
    if extra_table:
        context.require(extra_table, 0x10, "extra-joint header")
        records = context.u64(extra_table, "extra-joint record pointer")
        count = context.i32(extra_table + 8, "extra-joint count")
        if count < 0 or context.u32(extra_table + 0xC, "extra-joint reserved"):
            raise MotionParseError(f"{label}: invalid extra-joint header")
        if count and not records:
            raise MotionParseError(
                f"{label}: nonempty extra-joint block has a null record pointer"
            )
        context.require(records, count * 0x20, "extra-joint records")
        for index in range(count):
            record = records + index * 0x20
            parent_pointer = context.u64(
                record,
                f"extra joint {index} parent-name pointer",
            )
            joint_pointer = context.u64(
                record + 8,
                f"extra joint {index} name pointer",
            )
            if not parent_pointer or not joint_pointer:
                raise MotionParseError(
                    f"{label}: extra joint {index} has a null name pointer"
                )
            parent_name, _ = context.utf16_z(
                parent_pointer,
                f"extra joint {index} parent name",
            )
            joint_name, _ = context.utf16_z(
                joint_pointer,
                f"extra joint {index} name",
            )
            parent_hash = context.u32(
                record + 0x10,
                f"extra joint {index} parent hash",
            )
            joint_hash = context.u32(
                record + 0x14,
                f"extra joint {index} hash",
            )
            if parent_hash != murmur3_hash(parent_name.encode("utf-16le")):
                raise MotionParseError(
                    f"{label}: extra joint {index} parent hash mismatch"
                )
            if joint_hash != murmur3_hash(joint_name.encode("utf-16le")):
                raise MotionParseError(
                    f"{label}: extra joint {index} name hash mismatch"
                )
            raw_flags = context.u32(
                record + 0x1C,
                f"extra joint {index} flags",
            )
            if raw_flags & ~int(ExtraJointFlags.DISABLE_EMPTY_INTERPOLATION):
                raise MotionParseError(
                    f"{label}: extra joint {index} has unsupported flags "
                    f"0x{raw_flags:X}"
                )
            extra_joints.append(
                JointMapExtraJoint(
                    parent_name,
                    joint_name,
                    parent_hash,
                    joint_hash,
                    context.u32(
                        record + 0x18,
                        f"extra joint {index} symmetry hash",
                    ),
                    ExtraJointFlags(raw_flags),
                )
            )
    return JointMapDefinition(
        version,
        tuple(joints),
        tuple(groups),
        attributes,
        tuple(extra_joints),
    )
