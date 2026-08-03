from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..binary import ReadContext
from ..errors import MotionParseError
from ..mot_list.model import MotionSlotFlags, MotionSlotType
from ..mot_list.parser import MotListV85Parser
from ..profiles import MotionFormatProfile
from .resolution import DeferredMotion


@dataclass(frozen=True, slots=True)
class CatalogMotionSlot:
    motion_id: int
    slot_index: int
    slot_type: MotionSlotType
    motion: DeferredMotion | None


@dataclass(frozen=True, slots=True)
class MotionListCatalogDocument:
    path: str
    name: str
    base_motion_list_path: str | None
    slots: tuple[CatalogMotionSlot, ...]


class MotionListCatalogReader(Protocol):
    def parse(
        self,
        path: str,
        data: bytes | bytearray | memoryview,
    ) -> MotionListCatalogDocument: ...


class Dmc5MotionListCatalogReader:
    """Read v85 list identity while deferring complete v65 MOT decoding."""

    _FLAGS = frozenset(
        {
            MotionSlotFlags.NONE,
            MotionSlotFlags.MIRROR,
            MotionSlotFlags.LOCAL_TREE,
            MotionSlotFlags.SEQUENCE_ONLY,
        }
    )

    def __init__(self, profile: MotionFormatProfile):
        profile.require_versions(motlist=85, mot=65, mot_tree=4)
        self.profile = profile
        self._parser = MotListV85Parser(profile)

    def parse(
        self,
        path: str,
        data: bytes | bytearray | memoryview,
    ) -> MotionListCatalogDocument:
        scanned = self._parser.scan_layout(data, label=path)
        c = scanned.context
        ids = [row.motion_id for row in scanned.rows]
        if len(ids) != len(set(ids)):
            raise MotionParseError(f"{path}: v85 motion IDs must be sorted and unique")

        for row in scanned.rows:
            if row.flags not in self._FLAGS or row.physics or row.joint_mask:
                raise MotionParseError(f"{path}: unsupported v85 slot metadata")
            if row.flags & MotionSlotFlags.LOCAL_TREE and row.slot_type != MotionSlotType.MOT_TREE:
                raise MotionParseError(f"{path}: LocalTree flag requires a MotTree slot")
            if not row.pointer:
                if scanned.base_path is None or row.slot_type != MotionSlotType.MOT:
                    raise MotionParseError(f"{path}: invalid null v85 payload")
                if row.flags not in (MotionSlotFlags.NONE, MotionSlotFlags.SEQUENCE_ONLY):
                    raise MotionParseError(f"{path}: invalid inherited v85 slot flags")
            elif row.flags & MotionSlotFlags.SEQUENCE_ONLY:
                raise MotionParseError(f"{path}: SeqOnly slot owns an embedded payload")

        motions: dict[int, DeferredMotion] = {}
        for payload in scanned.payloads:
            if payload.slot_type == MotionSlotType.MOT:
                motion_name = self._motion_name(
                    c,
                    payload.offset,
                    payload.physical_end,
                )
                motions[payload.offset] = DeferredMotion(
                    motion_name,
                    lambda payload=payload: self._parser.mot_parser.parse(
                        c, payload.offset, payload.physical_end
                    ),
                )
            else:
                self._require_tree_header(
                    c,
                    payload.offset,
                    payload.physical_end,
                )

        return MotionListCatalogDocument(
            path,
            scanned.name,
            scanned.base_path,
            tuple(
                CatalogMotionSlot(
                    row.motion_id,
                    index,
                    row.slot_type,
                    motions.get(row.pointer),
                )
                for index, row in enumerate(scanned.rows)
            ),
        )

    def _motion_name(
        self,
        context: ReadContext,
        offset: int,
        physical_end: int,
    ) -> str:
        c = context.subcontext(
            offset,
            physical_end,
            label=f"MOT@0x{offset:X}",
            object_base=offset,
        )
        header = self.profile.mot.header_size
        c.require(offset, header, "MOT header")
        if c.u32(offset) != self.profile.mot.version or c.bytes(offset + 4, 4) != b"mot ":
            raise MotionParseError(f"{c.label}: expected embedded MOT v{self.profile.mot.version}")
        if c.u32(offset + 8) or c.u32(offset + 0xC):
            raise MotionParseError(f"{c.label}: unsupported MOT error/master state")
        if c.u64(offset + 0x50) != header:
            raise MotionParseError(f"{c.label}: v65 MOT name must follow the header")
        return c.utf16_z(offset + header, "MOT name")[0]

    def _require_tree_header(
        self,
        context: ReadContext,
        offset: int,
        physical_end: int,
    ) -> None:
        c = context.subcontext(
            offset,
            physical_end,
            label=f"MotTree@0x{offset:X}",
            object_base=offset,
        )
        c.require(offset, 0x50, "MotTree header")
        if (
            c.u32(offset) != self.profile.mot_tree.version
            or c.bytes(offset + 4, 4) != b"mtre"
        ):
            raise MotionParseError(
                f"{c.label}: expected embedded MotTree v{self.profile.mot_tree.version}"
            )
