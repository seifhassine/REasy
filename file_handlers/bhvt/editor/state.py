"""See docs/internals/bhvt-editor.md, Workspace."""

from __future__ import annotations

from collections import Counter

from PySide6.QtCore import QObject, QTimer, Signal

lazy from file_handlers.rsz.rsz_data_types import ArrayData, StructData, _DataValue

from ..document import BhvtDocument
from ..errors import BhvtFormatError
from ..model import Hfsm, MotionFsmV1, Record, TimelineFsm, Tree
from ..references import uses
from ..structure import Structure, Transition
from ..summaries import Summaries
from ..validation import graphs, problems


class EditorState(QObject):
    changed = Signal(bool)
    scope_changed = Signal()
    selection_changed = Signal()
    copied = Signal()
    refused = Signal(str)

    def __init__(self, document: BhvtDocument, parent: QObject | None = None):
        super().__init__(parent)
        self.document = document
        self.graphs = graphs(document.resource)
        self.model: Tree | Hfsm = self.graphs[0]
        self.selection: list = []
        self._tokens: dict[Transition, object] = {}
        self._cache: dict = {}
        self._reshaped = False
        self.scope = self.structure.root
        self._links = {"attribute", "default_start_child_id", *(
            spec[role] for spec in self.structure.kinds.values() for role in ("source", "target") if role in spec)}
        self._trail, self._at = [(self.model, [self.scope])], 0
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.timeout.connect(self._settled)
        document.changed.connect(self._invalidate)

    def _cached(self, name, make):
        if name not in self._cache:
            self._cache[name] = make()
        return self._cache[name]

    @property
    def structure(self) -> Structure:
        return self.structure_of(self.model)

    def structure_of(self, model: Tree | Hfsm) -> Structure:
        return self._cached(("structure", id(model)), lambda: Structure(model))

    def model_of(self, node) -> Tree | Hfsm | None:
        return next((model for model in self.graphs if node is (structure := self.structure_of(model)).root
                     or id(node) in structure.parent), None)

    @property
    def summaries(self) -> Summaries:
        return self._cached("summaries", lambda: Summaries(self.model, self.document.owner_variables.values(),
                                                           self.timelines, self.holds))

    @property
    def layered(self) -> bool:
        return len(self.graphs) > 1 or isinstance(self.document.resource.model, TimelineFsm)

    def layer(self, model: Tree | Hfsm):
        container = self.document.resource.model
        return next((item for item in container.trees if item.tree is model), None) \
            if isinstance(container, TimelineFsm) else None

    @property
    def holds(self) -> dict[bytes, str]:
        container = self.document.resource.model
        return {hold.hold_point_id: self.tr("Hold point {0}").format(number)
                for number, hold in enumerate(container.hold_points, 1)} if isinstance(container, TimelineFsm) else {}

    @property
    def timelines(self) -> dict[bytes, str]:
        container = self.document.resource.model
        return {entry.timeline_id: entry.name for entry in container.timelines} \
            if isinstance(container, TimelineFsm) else {}

    def uses(self, rsz) -> Counter:
        return self._cached(("uses", id(rsz.parsed_elements)), lambda: uses(rsz))

    @property
    def problems(self) -> list:
        return self._cached("problems", lambda: problems(self.document.resource))

    @property
    def current(self):
        return self.selection[-1] if self.selection else None

    @property
    def settling(self) -> bool:
        return self._settle.isActive()

    def title(self, model: Tree | Hfsm) -> str:
        container = self.document.resource.model
        if (layer := self.layer(model)) is not None:
            return layer.name
        if isinstance(container, MotionFsmV1):
            return self.tr("Layer {0}").format(next(entry.info.layer_no for entry in container.fsms
                                                    if entry.hfsm is model))
        return (model.root if isinstance(model, Hfsm) else model.nodes[0]).name

    def nodes(self) -> list:
        return [item for item in self.selection if isinstance(item, Record)]

    def transitions(self) -> list[Transition]:
        return [item for item in self.selection if isinstance(item, Transition)]

    def refresh(self) -> None:
        self._cache.clear()
        self._reshaped = True
        self._settle.start()

    def attempt(self, edit, *arguments):
        try:
            return edit(*arguments)
        except BhvtFormatError as error:
            self.refused.emit(str(error))

    def shows(self, node) -> bool:
        structure, path = self.structure, self.structure.path(node)
        at = next((index for index, item in enumerate(path) if item is self.scope), None)
        if at is None:
            return False
        between = path[at:-1]
        return len(between) == 1 if structure.is_fsm(self.scope) else not any(map(structure.is_fsm, between))

    def home(self, node):
        structure = self.structure
        scope = structure.parent.get(id(node), node)
        while not structure.is_fsm(scope) and (above := structure.parent.get(id(scope))) is not None \
                and not structure.is_fsm(above):
            scope = above
        return scope

    def _switch(self, model: Tree | Hfsm | None) -> None:
        if model is not None and model is not self.model:
            self.model, self.selection, self._tokens = model, [], {}
            self._cache.clear()
            self.selection_changed.emit()

    def show(self, scope=None, model: Tree | Hfsm | None = None) -> None:
        self._switch(model)
        scope = scope or self.structure.root
        if scope is not self.scope:
            self.scope = scope
            del self._trail[self._at + 1:]
            self._trail.append((self.model, self.structure.path(scope)))
            self._at += 1
            self.scope_changed.emit()

    def reveal(self, item, model: Tree | Hfsm | None = None) -> None:
        self._switch(model)
        if isinstance(item, Transition):
            self.show(self.structure.container(item))
        elif isinstance(item, Record) and not self.shows(item):
            self.show(self.home(item))
        self.select([item])

    def select(self, items: list) -> None:
        items = list(dict.fromkeys(items))
        if items != self.selection:
            self.selection = items
            self._tokens = {item: self.structure.token(item) for item in items if isinstance(item, Transition)}
            self.selection_changed.emit()

    def can_step(self, by: int) -> bool:
        return 0 <= self._at + by < len(self._trail)

    def step(self, by: int) -> None:
        if not self.can_step(by):
            return
        self._at += by
        model, path = self._trail[self._at]
        self._switch(model)
        self.scope = self._live(path)
        self.scope_changed.emit()

    def _live(self, path: list):
        structure = self.structure
        return next((node for node in reversed(path) if node is structure.root or id(node) in structure.parent),
                    structure.root)

    def _invalidate(self, change) -> None:
        target = change.target
        if change.structural or isinstance(target, (ArrayData, StructData)) or not isinstance(target, _DataValue) and (
                change.field in self._links or not isinstance(getattr(target, change.field), (int, float, str, bytes))):
            self._reshaped = True
        self.graphs = graphs(self.document.resource)
        self._cache.clear()
        self._settle.start()

    def _prune(self) -> None:
        kept = [(at, entry) for at, entry in enumerate(self._trail) if entry[0] in self.graphs]
        if len(kept) < len(self._trail):
            self._at = max(sum(at <= self._at for at, _entry in kept) - 1, 0)
            self._trail = [entry for _at, entry in kept] or [
                (self.graphs[0], [self.structure_of(self.graphs[0]).root])]
        if self.model not in self.graphs:
            self.model, self.selection, self._tokens = self._trail[self._at][0], [], {}
            self.scope = self._live(self._trail[self._at][1])
            self.selection_changed.emit()
            self.scope_changed.emit()

    def _settled(self) -> None:
        self._prune()
        structure, live, tokens = self.structure, [], {}
        for item in self.selection:
            if isinstance(item, Transition):
                token = self._tokens[item]
                if (item := structure.found(item, token)) is not None:
                    tokens[item] = token
            elif isinstance(item, Record):
                item = item if item is structure.root or id(item) in structure.parent else None
            elif not any(item is found[1] for found in self.summaries.found.values()):
                item = None
            if item is not None:
                live.append(item)
        scope = self._live(self._trail[self._at][1])
        moved = scope is not self.scope
        self.scope = scope
        if live != self.selection:
            self.selection, self._tokens = live, tokens
            self.selection_changed.emit()
        if moved:
            self.scope_changed.emit()
        reshaped, self._reshaped = self._reshaped, False
        self.changed.emit(reshaped)
