"""See docs/internals/gui-editor.md, Workspace."""

from __future__ import annotations

from collections import Counter

from PySide6.QtCore import QElapsedTimer, QObject, QRectF, QTimer, Signal

lazy from file_handlers.mmtr.profiles import mmtr_profile_for_game

from .. import edits, state_edits
from ..dependencies import GuiDependencyCatalog
from ..document import GuiEditDocument
from ..errors import GuiEditError
from ..model import GuiAnimation, GuiBinding, GuiDocument, GuiObject, GuiProperty, GuiSymbol
from ..profiles import gui_runtime
from ..authoring import GuiField
from ..scene import GuiResource, GuiSceneNode, GuiWorkspace
from ..validation import Problem, problems

_TREE = {GuiSymbol: ("objects", "name"), GuiDocument: ("symbols", "objects", "root_object"),
         GuiObject: ("name", "prototype_guid")}


class EditorState(QObject):
    changed = Signal(bool)
    selection_changed = Signal()
    scope_changed = Signal()
    shown_changed = Signal()
    frame_changed = Signal()
    played = Signal(bool)
    refused = Signal(str)

    def __init__(self, document: GuiEditDocument, source: str, loader, locate, lister, game: str,
                 parent: QObject | None = None):
        super().__init__(parent)
        self.document, self.source = document, source or document.gui.source
        profile = gui_runtime(document.gui.version)
        self.dependencies, self._lister, self._listed = GuiDependencyCatalog(loader, profile, locate if (
            mmtr_profile_for_game(game or profile.game).game == profile.game) else lambda path: None), lister, {}
        self.loader = self.dependencies.resource_data_loader
        self.stage = profile.adapter.create_stage(self.dependencies, game)
        self.scope: GuiSymbol | None = None
        self.selection: list[GuiSceneNode] = []
        self.context: GuiSceneNode | None = None
        self.shown: GuiAnimation | None = None
        self.frame = 0.0
        self._origin = (0.0, 0.0)
        self._keys: dict[int, tuple] = {}
        self._nodes: dict[tuple, GuiSceneNode] = {}
        self.script: dict[tuple, dict[str, object]] = {}
        self._wanted: list[tuple] | None = None
        self._tree = self._lists = self._reload = False
        self._problems: list[Problem] | None = None
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.timeout.connect(self._settled)
        self._ticker = QTimer(self)
        self._ticker.setInterval(16)
        self._ticker.timeout.connect(self._tick)
        self._clock = QElapsedTimer()
        document.changed.connect(self._invalidate)
        self._load()

    def _load(self) -> bool:
        self.workspace = self.document.workspace = GuiWorkspace.from_document(
            self.source, self.document.gui, self.loader)
        return self._build()

    def _build(self) -> bool:
        keys = self._keys
        selection = self._wanted if self._wanted is not None else [keys[id(node)] for node in self.selection]
        context, self._wanted = keys[id(self.context)] if self.context is not None else None, None
        self.workspace.invalidate()
        self.scene = scene = self.workspace.instantiate(self.scope)
        if self.scope is not None:
            held = scene.root.children[0].object
            field = self.document.authoring.field(held.type_name, self.stage.position_field)
            held.properties = [GuiProperty(field.name, field.type, [*self._origin, *field.default[2:]])]
            scene.refresh()
        self._keys, found = {}, {}
        for node in scene.nodes:
            mine = id(self.scope) if self.stand(node) else id(node.object)
            key = self._keys[id(node)] = (*self._keys[id(node.parent)], mine) if node.parent is not None else (mine,)
            found[key] = node
        self._placed, self._nodes = Counter(id(node.object) for node in scene.nodes), found
        self.selection = [found[key] for key in selection if key in found]
        self.context = found.get(context)
        return self._present()

    def _center(self) -> None:
        stage, bounds = self.stage, QRectF()
        for node in self.scene.root.children[0].walk():
            bounds |= stage.outline(node).boundingRect()
        if not bounds.isEmpty():
            width, height = stage.size
            self._origin = (float(round(width / 2 - bounds.center().x())),
                            float(round(height / 2 - bounds.center().y())))
            self._build()

    def _present(self) -> bool:
        scene, was = self.scene, self.shown
        if self.context is None:
            self.context = scene.root.children[0] if self.scope is not None else scene.root
        self.stage.set_scene(scene)
        for key, values in self.script.items():
            for name, value in values.items() if key in self._nodes else ():
                self.stage.script(self._nodes[key], name, value)
        held = self.context.prototype.symbol.animations if self.context.prototype is not None else ()
        if was is None or not any(item is was for item in held):
            self.shown, self.frame = self.stage.state(self.context), 0.0
        self.stage.show_state(self.context, self.shown)
        if self.shown is not None:
            self.stage.set_frame(self.frame)
        return self.shown is not was

    def _invalidate(self, change) -> None:
        target, field = change.target, change.field
        self._problems = None
        if field in _TREE.get(type(target), ()):
            self._tree = True
        elif field == "imported_gui_paths":
            self._reload = True
        elif change.structural:
            self._lists = True
        self._settle.start()

    def settle(self) -> None:
        if self._settle.isActive():
            self._settle.stop()
            self._settled()

    def _settled(self) -> None:
        tree, reload, lists = self._tree or self._reload, self._reload, self._lists
        self._tree = self._lists = self._reload = False
        if reload:
            dropped = self._load()
        elif tree:
            dropped = self._build()
        else:
            if lists:
                self.scene.rebind([] if self.scope is not None else self.document.gui.bindings)
            else:
                self.scene.refresh()
                self.scene.apply_bindings()
            dropped = self._present()
        if tree:
            self.selection_changed.emit()
        if tree or dropped:
            self.shown_changed.emit()
        self.changed.emit(tree)

    @property
    def settling(self) -> bool:
        return self._settle.isActive()

    @property
    def problems(self) -> list[Problem]:
        if self._problems is None:
            self._problems = problems(self.document)
        return self._problems

    def listed(self, kind: str) -> tuple[str, ...]:
        if kind not in self._listed:
            self._listed[kind] = self._lister(kind)
        return self._listed[kind]

    @property
    def current(self) -> GuiSceneNode | None:
        return self.selection[-1] if self.selection else None

    def key(self, node: GuiSceneNode) -> tuple:
        return self._keys[id(node)]

    def stand(self, node: GuiSceneNode) -> bool:
        return self.scope is not None and node.parent is not None and node.parent.parent is None

    def local(self, node: GuiSceneNode) -> bool:
        return node.resource is self.workspace.root and not self.stand(node)

    def placed(self, node: GuiSceneNode) -> int:
        return self._placed[id(node.object)]

    def home(self, node: GuiSceneNode) -> GuiSymbol | None:
        held = node.prototype
        return held.symbol if held is not None and held.resource is self.workspace.root else None

    def mode(self, node: GuiSceneNode) -> str:
        if self.local(node) and self.placed(node) == 1:
            return "definition"
        return "override" if self.scope is None and not self.stand(node) else "locked"

    def binding(self, node: GuiSceneNode, name: str) -> GuiBinding | None:
        return next(iter(self.scene.bindings_for(node, name)), None)

    def stored(self, node: GuiSceneNode, name: str):
        if (binding := self.binding(node, name)) is not None:
            return binding.property.value
        held = node.object.property(name)
        return held.value if held is not None else self.document.authoring.field(node.object.type_name, name).default

    def animates(self, node: GuiSceneNode) -> bool:
        return node.parent is self.context and self.shown is not None and self.mode(node) == "definition"

    def keyed(self, node: GuiSceneNode, name: str):
        return state_edits.track(self.document, self.shown, node.object, name) if self.animates(node) else None

    def can_key(self, node: GuiSceneNode, name: str) -> bool:
        document, obj = self.document, node.object
        return self.animates(node) and state_edits.can_animate(document, obj, name) \
            and state_edits.node_of(document, self.shown, obj) is not None

    def on_key(self, track) -> bool:
        return any(key.frame == self.frame for leaf in track.children or [track] for key in leaf.keys)

    def add_key(self, node: GuiSceneNode, name: str) -> None:
        document, track = self.document, self.keyed(node, name)
        if track is None:
            state_edits.add_track(document, self.shown, node.object, name)
        else:
            state_edits.add_keys(document, self.shown, track.children or [track], self.frame)

    def value(self, node: GuiSceneNode, name: str):
        now = self.stage.value(node, name) if self.keyed(node, name) is not None else None
        return self.stored(node, name) if now is None else now

    def set_value(self, node: GuiSceneNode, name: str, value) -> None:
        if self.mode(node) == "locked":
            raise GuiEditError(self.locked(node))
        track, stored = self.keyed(node, name), self.stored(node, name)
        if track is None:
            self._store(node, name, value)
            return
        with self.document.macro(self.tr("Set {0}").format(name)):
            kept = state_edits.key_value(self.document, self.shown, track, self.frame, self.value(node, name), value,
                                         stored)
            if kept != stored:
                self._store(node, name, kept)

    def shown_value(self, node: GuiSceneNode, name: str):
        now = self.stage.value(node, name)
        return self.value(node, name) if now is None else now

    def scripted(self, node: GuiSceneNode, name: str) -> bool:
        return name in self.script.get(self.key(node), ())

    def set_script(self, node: GuiSceneNode, name: str, value) -> None:
        held = node.object.property(name)
        kind = held.type if held is not None else self.document.authoring.field(node.object.type_name, name).type
        self.script.setdefault(self.key(node), {})[name] = edits.checked(self.document, kind, value, name)
        self._replay()

    def clear_script(self, node: GuiSceneNode | None = None, name: str = "") -> None:
        if node is None:
            self.script.clear()
        else:
            del self.script[self.key(node)][name]
        self._replay()

    def _replay(self) -> None:
        self._present()
        self.changed.emit(False)

    def _store(self, node: GuiSceneNode, name: str, value) -> None:
        document, obj, mode = self.document, node.object, self.mode(node)
        if mode == "definition" and self.binding(node, name) is None:
            edits.set_value(document, obj, name, value)
            return
        if "#" in node.path:
            raise GuiEditError(self.tr("Two objects share this name: rename one before giving it a value of its "
                                       "own."))
        held = obj.property(name)
        kind = held.type if held is not None else document.authoring.field(obj.type_name, name).type
        edits.set_override(document, node.path, obj.type_name, name, kind, value)

    def resize(self, node: GuiSceneNode, size: tuple[float, float], position: tuple[float, float]) -> None:
        stage, held = self.stage, self.value(node, self.stage.position_field)
        with self.document.macro(self.tr("Resize {0}").format(node.object.name)):
            for name, value in ((stage.size_field(node), list(size)), (stage.position_field, [*position, *held[2:]])):
                if value != list(self.value(node, name)):
                    self.set_value(node, name, value)

    def locked(self, node: GuiSceneNode) -> str:
        if self.stand(node):
            return self.tr("This is the symbol itself: place it in a file to give it values.")
        if self.local(node):
            return self.tr("{0} is placed {1} times in this symbol: open the symbol that holds it, or edit one "
                           "placement from the file's own view.").format(node.object.name, self.placed(node))
        return self.tr("{0} belongs to {1}: open that file to change it, or edit this placement from the file's own "
                       "view.").format(node.object.name, node.resource.key)

    def fixed(self, node: GuiSceneNode) -> str:
        if node.parent is None or self.stand(node):
            return self.tr("The root object stays where it is.")
        if not self.local(node):
            return self.tr("{0} belongs to {1}: open that file to change it.").format(node.object.name,
                                                                                    node.resource.key)
        if self.placed(node) > 1:
            return self.tr("{0} is part of a symbol placed {1} times: open the symbol alone to change every "
                           "placement.").format(node.object.name, self.placed(node))
        return ""

    def inside(self, node: GuiSceneNode) -> GuiSymbol:
        symbol = self.home(node)
        if symbol is None:
            raise GuiEditError(self.tr("{0} places no symbol of this file: nothing can be put in it.")
                               .format(node.object.name))
        if sum(item.prototype is not None and item.prototype.symbol is symbol for item in self.scene.nodes) > 1:
            raise GuiEditError(self.tr("{0} is placed several times: open the symbol alone to change every "
                                       "placement.").format(symbol.name))
        return symbol

    def container(self, into: bool = True) -> tuple[GuiSceneNode, GuiSymbol, int | None]:
        node = self.current or self.context
        if node.parent is None or self.stand(node) or into and self.home(node) is not None:
            return node, self.inside(node), None
        symbol = self.inside(node.parent)
        return node.parent, symbol, symbol.objects.index(node.object) + 1

    def labels(self, resource: GuiResource, obj: GuiObject, field: GuiField | None) -> tuple[str, ...]:
        if field is None:
            return ()
        if field.enum or field.labels:
            return self.document.authoring.labels(field)
        held = self.workspace.resolve_symbol(resource.key, obj.prototype_guid)
        if field.name != self.workspace.profile.control_fields["state"] or held is None:
            return ()
        return ("", *dict.fromkeys(item.name for item in held.symbol.animations))

    def attempt(self, edit, *arguments):
        try:
            return edit(*arguments)
        except GuiEditError as error:
            self.refused.emit(str(error))

    def select(self, nodes: list[GuiSceneNode]) -> None:
        nodes = list(dict.fromkeys(nodes))
        if nodes == self.selection:
            return
        self.selection = nodes
        node = self.current
        context = node if node is None or node.parent is None or self.stand(node) else node.parent
        if context is not None and context is not self.context:
            self._show(context, None)
        self.selection_changed.emit()

    def select_new(self, parent: GuiSceneNode, objects: list[GuiObject]) -> None:
        self._wanted = [(*self._keys[id(parent)], id(obj)) for obj in objects]
        self._tree = True
        self._settle.start()

    def show(self, context: GuiSceneNode, animation: GuiAnimation | None) -> None:
        if context is not self.context or animation is not None and animation is not self.shown:
            self._show(context, animation)

    def _show(self, context: GuiSceneNode, animation: GuiAnimation | None) -> None:
        self.context, self.frame = context, 0.0
        self.shown = animation if animation is not None else self.stage.state(context)
        self.stage.show_state(context, self.shown)
        self.shown_changed.emit()
        self.frame_changed.emit()

    def set_frame(self, frame: float) -> None:
        if self.shown is not None and frame != self.frame:
            self.frame = frame
            self.stage.set_frame(frame)
            self.frame_changed.emit()

    @property
    def playing(self) -> bool:
        return self._ticker.isActive()

    def play(self, on: bool) -> None:
        if on != self.playing:
            self._clock.start()
            if on:
                self._ticker.start()
            else:
                self._ticker.stop()
            self.played.emit(on)

    def reset(self) -> None:
        self.shown = None
        self._present()
        self.shown_changed.emit()
        self.frame_changed.emit()
        self.changed.emit(False)

    def _tick(self) -> None:
        self.advance(self._clock.restart() / 1000.0)

    def advance(self, seconds: float) -> None:
        self.stage.advance(seconds)
        if (now := self.stage.state(self.context)) is not self.shown:
            self.shown = now
            self.shown_changed.emit()
        self.frame = self.stage.frame
        self.frame_changed.emit()

    def open_symbol(self, symbol: GuiSymbol | None) -> None:
        if symbol is not self.scope:
            self.scope, self.selection, self.context, self.shown, self.frame = symbol, [], None, None, 0.0
            self._origin = (0.0, 0.0)
            self._build()
            if symbol is not None:
                self._center()
            self.scope_changed.emit()
            self.selection_changed.emit()
            self.shown_changed.emit()
            self.changed.emit(True)

    def reveal(self, target) -> None:
        gui, structure = self.document.gui, self.document.structure
        if isinstance(target, GuiBinding):
            self.open_symbol(None)
            if (node := self.scene.binding_target(target.target_path)) is not None:
                self.select([node])
            return
        animation = target if isinstance(target, GuiAnimation) else next(
            (item for item in gui.animations if self.document.clip(item).parent(target) is not None), None)
        obj = target if isinstance(target, GuiObject) else None
        symbol = structure.state_symbol(animation) if animation is not None else structure.owner.get(id(obj))
        holds = (lambda node: self.home(node) is symbol) if animation is not None else (lambda node: node.object is obj)
        if symbol is not None and not any(map(holds, self.scene.nodes)):
            self.open_symbol(None if symbol is gui.symbols[0] or structure.uses.get(symbol.guid) else symbol)
        node = next(filter(holds, self.scene.nodes), None)
        if node is not None and animation is not None:
            self.show(node, animation)
        elif node is not None:
            self.select([node])
