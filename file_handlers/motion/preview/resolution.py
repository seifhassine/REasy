from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from enum import Enum
import re
from typing import Callable, Protocol

from ..mot.model import Motion
from ..mot_list.model import MotList, MotionSlotType
from ..mot_tree.model import MotTree, TreeParameter, TreeParameterType


class PreviewMotionOrigin(Enum):
    EMBEDDED = "embedded"
    INHERITED = "inherited"


@dataclass(frozen=True, slots=True)
class MotionListDocument:
    path: str
    model: MotList


@dataclass(frozen=True, slots=True)
class TreeMotionReference:
    tree_slot_index: int
    tree_motion_id: int
    tree_name: str
    node_name: str
    bank_id: int
    motion_id: int


@dataclass(slots=True)
class DeferredMotion:
    """Preview-only lazy access to one fully validated embedded MOT."""

    name: str
    loader: Callable[[], Motion]
    _value: Motion | None = None
    _error: Exception | None = None

    @property
    def loaded(self) -> bool:
        return self._value is not None

    def resolve(self) -> Motion:
        if self._error is not None:
            raise self._error
        if self._value is None:
            try:
                value = self.loader()
                if value.name != self.name:
                    raise ValueError(
                        f"deferred MOT name changed from {self.name!r} to "
                        f"{value.name!r}"
                    )
                self._value = value
            except Exception as exc:
                self._error = exc
                raise
        return self._value


@dataclass(frozen=True, slots=True)
class PreviewMotionEntry:
    motion_id: int
    motion: Motion | DeferredMotion
    origin: PreviewMotionOrigin
    source_path: str
    source_list_name: str
    slot_index: int
    inheritance_chain: tuple[str, ...] = ()
    bank_id: int | None = None

    @property
    def name(self) -> str:
        return self.motion.name

    def resolve_motion(self) -> Motion:
        return (
            self.motion.resolve()
            if isinstance(self.motion, DeferredMotion)
            else self.motion
        )

    @property
    def loaded_motion(self) -> Motion | None:
        if isinstance(self.motion, DeferredMotion):
            return self.motion._value
        return self.motion


@dataclass(frozen=True, slots=True)
class MotionResolutionDiagnostic:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class MotionPreviewResolution:
    entries: tuple[PreviewMotionEntry, ...]
    tree_references: tuple[TreeMotionReference, ...]
    unresolved_bank_ids: tuple[int, ...]
    diagnostics: tuple[MotionResolutionDiagnostic, ...]


BaseDocumentLoader = Callable[[str], MotionListDocument | None]
_MOTION_PARAMETER = re.compile(r"^(?:(No\d{2})_)?MotionID$")


@dataclass(frozen=True, slots=True)
class TreeMotionTarget:
    node_name: str
    bank_id: int
    motion_id: int


class TreeMotionReferenceStrategy(Protocol):
    def extract(
        self,
        tree: MotTree,
    ) -> tuple[tuple[TreeMotionTarget, ...], tuple[MotionResolutionDiagnostic, ...]]: ...


@dataclass(frozen=True, slots=True)
class Dmc5TreeMotionReferenceStrategy:
    """DMC5 v4 tree leaves use paired U32 BankID/MotionID parameters."""

    def extract(
        self,
        tree: MotTree,
    ) -> tuple[tuple[TreeMotionTarget, ...], tuple[MotionResolutionDiagnostic, ...]]:
        references: list[TreeMotionTarget] = []
        diagnostics: list[MotionResolutionDiagnostic] = []
        for node in tree.nodes:
            by_name: dict[str, list[TreeParameter]] = {}
            for parameter in node.parameters:
                by_name.setdefault(parameter.name, []).append(parameter)
            for name, parameters in by_name.items():
                match = _MOTION_PARAMETER.fullmatch(name)
                if match is None:
                    continue
                prefix = f"{match.group(1)}_" if match.group(1) else ""
                bank_name = f"{prefix}BankID"
                bank_parameters = by_name.get(bank_name, [])
                if len(parameters) != 1 or len(bank_parameters) != 1:
                    diagnostics.append(MotionResolutionDiagnostic(
                        "ambiguous_tree_reference",
                        f"MotTree {tree.name!r} node {node.class_name!r} has an ambiguous {name}/{bank_name} pair",
                    ))
                    continue
                motion_parameter = parameters[0]
                bank_parameter = bank_parameters[0]
                if (
                    motion_parameter.parameter_type != TreeParameterType.U32
                    or bank_parameter.parameter_type != TreeParameterType.U32
                    or type(motion_parameter.value) is not int
                    or type(bank_parameter.value) is not int
                ):
                    diagnostics.append(MotionResolutionDiagnostic(
                        "unsupported_tree_reference",
                        f"MotTree {tree.name!r} node {node.class_name!r} has non-U32 BankID/MotionID parameters",
                    ))
                    continue
                pair = bank_parameter.value, motion_parameter.value
                if 0xFFFFFFFF in pair:
                    continue
                references.append(TreeMotionTarget(
                    node.name or node.class_name,
                    pair[0],
                    pair[1],
                ))

        parameter_pairs = Counter((item.bank_id, item.motion_id) for item in references)
        remap_pairs = Counter((item.target, item.source) for item in tree.motion_id_remaps)
        if remap_pairs and parameter_pairs != remap_pairs:
            diagnostics.append(MotionResolutionDiagnostic(
                "tree_remap_mismatch",
                f"MotTree {tree.name!r} MotionID parameters do not match its remap table",
            ))
        return tuple(references), tuple(diagnostics)


DMC5_TREE_MOTION_REFERENCES = Dmc5TreeMotionReferenceStrategy()


class MotionPreviewResolver:
    """Resolve only MOT payload relationships serialized by a MOTLIST."""

    def __init__(
        self,
        load_base: BaseDocumentLoader,
        tree_reference_strategy: TreeMotionReferenceStrategy,
    ):
        self._load_base = load_base
        self._tree_reference_strategy = tree_reference_strategy
        self._diagnostics: list[MotionResolutionDiagnostic] = []
        self._effective_cache: dict[int, dict[int, PreviewMotionEntry]] = {}

    def resolve(
        self,
        root: MotionListDocument,
    ) -> MotionPreviewResolution:
        self._diagnostics.clear()
        self._effective_cache.clear()
        root_motions = self._effective_motions(root, ())
        references = self._tree_references(root.model)
        return MotionPreviewResolution(
            tuple(root_motions.values()),
            tuple(references),
            tuple(sorted({item.bank_id for item in references})),
            tuple(self._diagnostics),
        )

    def _effective_motions(
        self,
        document: MotionListDocument,
        active_models: tuple[int, ...],
    ) -> dict[int, PreviewMotionEntry]:
        identity = id(document.model)
        cached = self._effective_cache.get(identity)
        if cached is not None:
            return cached
        if identity in active_models:
            self._diagnostics.append(MotionResolutionDiagnostic(
                "base_list_cycle",
                f"base MOTLIST cycle reaches {document.path!r}",
            ))
            return {}
        base_motions: dict[int, PreviewMotionEntry] = {}
        base_path = document.model.base_motion_list_path
        if base_path:
            base_document = self._load_base(base_path)
            if base_document is None:
                self._diagnostics.append(MotionResolutionDiagnostic(
                    "missing_base_list",
                    f"base MOTLIST {base_path!r} could not be loaded for {document.path!r}",
                ))
            else:
                base_motions = self._effective_motions(
                    base_document,
                    (*active_models, identity),
                )

        result: dict[int, PreviewMotionEntry] = {}
        for slot_index, slot in enumerate(document.model.slots):
            value = slot.payload.value if slot.payload is not None else None
            if slot.slot_type == MotionSlotType.MOT and isinstance(value, Motion):
                result[slot.motion_id] = PreviewMotionEntry(
                    slot.motion_id,
                    value,
                    PreviewMotionOrigin.EMBEDDED,
                    document.path,
                    document.model.name,
                    slot_index,
                    (document.path,),
                )
                continue
            if slot.slot_type != MotionSlotType.MOT or slot.payload is not None:
                continue
            inherited = base_motions.get(slot.motion_id)
            if inherited is None:
                self._diagnostics.append(MotionResolutionDiagnostic(
                    "missing_inherited_motion",
                    f"slot ID {slot.motion_id} in {document.path!r} has no matching base MOT",
                ))
                continue
            result[slot.motion_id] = replace(
                inherited,
                motion_id=slot.motion_id,
                origin=PreviewMotionOrigin.INHERITED,
                slot_index=slot_index,
                inheritance_chain=(document.path, *inherited.inheritance_chain),
            )
        self._effective_cache[identity] = result
        return result

    def _tree_references(self, motlist: MotList) -> list[TreeMotionReference]:
        result: list[TreeMotionReference] = []
        for slot_index, slot in enumerate(motlist.slots):
            tree = slot.payload.value if slot.payload is not None else None
            if not isinstance(tree, MotTree):
                continue
            targets, diagnostics = self._tree_reference_strategy.extract(tree)
            self._diagnostics.extend(diagnostics)
            for target in targets:
                result.append(TreeMotionReference(
                    slot_index,
                    slot.motion_id,
                    tree.name,
                    target.node_name,
                    target.bank_id,
                    target.motion_id,
                ))
        return result
