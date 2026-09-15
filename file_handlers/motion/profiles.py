from __future__ import annotations

from dataclasses import dataclass

from utils.hash_util import murmur3_hash_utf16le

from .strategies import MotTreeParameterTableStrategy, SkeletonTopologyIndexStrategy


@dataclass(frozen=True, slots=True)
class MotListLayout:
    version: int
    header_size: int
    row_size: int
    main_alignment: int
    override_table_alignment: int
    sequence_alignment: int


@dataclass(frozen=True, slots=True)
class MotLayout:
    version: int
    header_size: int
    sequence_wrapper_size: int
    sequence_clip_offset: int
    tracks_data_size: int
    sequence_categories: frozenset[int]
    skeleton_topology_index: SkeletonTopologyIndexStrategy


@dataclass(frozen=True, slots=True)
class CompactClipLayout:
    version: int
    header_size: int
    node_size: int
    property_size: int
    key_size: int


@dataclass(frozen=True, slots=True)
class MotTreeLayout:
    version: int
    parameter_tables: MotTreeParameterTableStrategy


@dataclass(frozen=True, slots=True)
class MotionSemanticNames:
    append_classes: tuple[tuple[int, str], ...] = ()
    append_properties: tuple[tuple[int, str], ...] = ()
    motion_properties: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True, slots=True)
class MotionFormatProfile:
    name: str
    motlist: MotListLayout
    mot: MotLayout
    mot_clip: CompactClipLayout
    mot_tree: MotTreeLayout
    override_categories: frozenset[int]
    semantic_names: MotionSemanticNames = MotionSemanticNames()

    def require_versions(
        self,
        *,
        motlist: int | None = None,
        mot: int | None = None,
        mot_clip: int | None = None,
        mot_tree: int | None = None,
    ) -> None:
        for name, expected, actual in (
            ("MOTLIST", motlist, self.motlist.version),
            ("MOT", mot, self.mot.version),
            ("MotClip", mot_clip, self.mot_clip.version),
            ("MotTree", mot_tree, self.mot_tree.version),
        ):
            if expected is not None and actual != expected:
                raise ValueError(
                    f"{name} v{expected} codec cannot use {self.name} "
                    f"{name} v{actual} profile"
                )


def _prefixed_names(prefix: str, *names: str) -> tuple[str, ...]:
    return tuple(prefix + name for name in names)


DMC5_FULL_WRINKLE_NAMES = (
    *_prefixed_names(
        "head_cm1_color.head_wm1_",
        "blink_L",
        "blink_R",
        "browsRaiseInner_L",
        "browsRaiseInner_R",
        "browsRaiseOuter_L",
        "browsRaiseOuter_R",
        "chinRaise_L",
        "chinRaise_R",
        "jawOpen",
        "lipsNarrow_DL",
        "lipsNarrow_DR",
        "lipsNarrow_UL",
        "lipsNarrow_UR",
        "purse_DL",
        "purse_DR",
        "purse_UL",
        "purse_UR",
        "squintInner_L",
        "squintInner_R",
    ),
    *_prefixed_names(
        "head_cm2_color.head_wm2_",
        "browsDown_L",
        "browsDown_R",
        "browsLateral_L",
        "browsLateral_R",
        "mouthStretch_L",
        "mouthStretch_R",
        "neckStretch_L",
        "neckStretch_R",
        "noseWrinkler_L",
        "noseWrinkler_R",
    ),
    *_prefixed_names(
        "head_cm3_color.head_wm3_",
        "cheekRaiseInner_L",
        "cheekRaiseInner_R",
        "cheekRaiseOuter_L",
        "cheekRaiseOuter_R",
        "cheekRaiseUpper_L",
        "cheekRaiseUpper_R",
        "lipsStretch_DL",
        "lipsStretch_DR",
        "lipsStretch_UL",
        "lipsStretch_UR",
        "smile_L",
        "smile_R",
    ),
)


DMC5_PROFILE = MotionFormatProfile(
    name="Devil May Cry 5",
    motlist=MotListLayout(
        version=85,
        header_size=0x34,
        row_size=0x18,
        main_alignment=0x10,
        override_table_alignment=0x08,
        sequence_alignment=0x10,
    ),
    mot=MotLayout(
        version=65,
        header_size=0x74,
        sequence_wrapper_size=0x28,
        sequence_clip_offset=0x40,
        tracks_data_size=0x10,
        sequence_categories=frozenset(range(6)),
        # In the DMC5 v65 corpus the redundant topology index exists exactly
        # when a non-root joint of extra type 0 needs child/sibling navigation.
        skeleton_topology_index=SkeletonTopologyIndexStrategy(
            indexed_parented_joint_types=frozenset({0}),
        ),
    ),
    mot_clip=CompactClipLayout(
        version=27,
        header_size=0x88,
        node_size=0x60,
        property_size=0x70,
        key_size=0x28,
    ),
    mot_tree=MotTreeLayout(
        version=4,
        # This v4 node class owns a parameter table even when its semantic
        # parameter list is empty; other empty classes omit the table.
        parameter_tables=MotTreeParameterTableStrategy(
            materialized_empty_classes=frozenset({"ParentLocalConstraintsNode"}),
        ),
    ),
    override_categories=frozenset(range(3)),
    semantic_names=MotionSemanticNames(
        append_classes=(
            (0x4BCE080F, "via.motion.script.MotionConnectionTable"),
            (0xCD325D08, "via.motion.script.MotionExtraData"),
        ),
        append_properties=(
            (0x138292D0, "RightFootSlideList"),
            (0x1FE82BEE, "ContactStatusList"),
            (0x718ADA1A, "RootSpeedAndMoveDir"),
            (0x73BF48E9, "ID"),
            (0xA93393F0, "SimilarityValues"),
            (0xB704E95A, "SimilarityStartFrame"),
            (0xD618C61A, "LeftFootSlideList"),
            (0xE15FB3A5, "Version"),
            (0xE357827B, "ConnectionTarget"),
            (0xE958F08E, "FootStatusList"),
            (0x4640C6A1, "ContactStatusList"),
            (0x8701DF47, "LeftFootSlideList"),
            (0x9956B972, "RootSpeedAndMoveDir"),
            (0xA9FFF3A0, "Version"),
            (0xAB4208FE, "RightFootSlideList"),
            (0xBAA798B9, "ID"),
            (0xD14E7F15, "SimilarityValues"),
            (0xD4CDD324, "ConnectionTarget"),
            (0xD77450A0, "SimilarityStartFrame"),
            (0xF48A0397, "FootStatusList"),
        ),
        motion_properties=tuple(
            (murmur3_hash_utf16le(name), name)
            for name in DMC5_FULL_WRINKLE_NAMES
        ),
    ),
)
