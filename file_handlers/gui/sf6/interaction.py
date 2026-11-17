"""See docs/internals/gui-editor.md, Stage (SF6 lists)."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication

from file_handlers.native_math import f32

from ..dmc5.interaction import _LIST, _SCROLL, Dmc5GuiInteraction, _Scroll
from .grid import Sf6GridMixin

lazy from ..scene import GuiSceneNode

translate = QCoreApplication.translate


class Sf6GuiInteraction(Sf6GridMixin, Dmc5GuiInteraction):
    decides = False

    def _bind(self) -> None:
        pass

    def _direction(self, held: dict[str, object]) -> str:
        direction = super()._direction(held)
        return direction if not held["FlipBarSizeDir"] else "Vertical" if direction == "Horizontal" else "Horizontal"

    def _ranged(self, held: dict[str, object], count: int) -> bool:
        begin, end = int(held["CursorRangeBegin"]), int(held["CursorRangeEnd"])
        return self.profile.enum_label(held, "CursorType") != "Fix" and 0 <= begin < min(end, count - 1) \
            and 0 < end < count

    def _range(self, held: dict[str, object], count: int) -> tuple[int, int]:
        return (int(held["CursorRangeBegin"]), int(held["CursorRangeEnd"])) if self._ranged(held, count) \
            else (0, count - 1)

    def _scroll(self, owner: GuiSceneNode) -> None:
        held, template, bar = self._held(owner), self._template(owner), self._bar(owner)
        count, most = max(1, int(held["ItemCount"])), max(1, int(held["ItemMax"]))
        self._put(owner, SelectedIndex=0, CursorIndex=self._range(held, count)[0])
        if template is None:
            return
        scene, playback = self.stage.scene, self.stage.playback
        if owner.path in self.scrolls:
            self._put(template, Position=list(self.scrolls[owner.path].base))
        base = list(self._held(template)["Position"])
        self._put(owner, ItemCount=count, ItemMax=most)
        if bar is not None:
            begin, end = self._range(held, count)
            self._put(bar, CursorType=held["CursorType"], ItemCount=end - begin + 1, ItemMax=most)
            self._layout(bar)
            if bar.path in self.bars:
                self._place(bar)
        self._put(template, Enabled=True, CanSelect=True, CanDecide=True, Visible=True)
        self._choose(template, False)
        items = [template]
        for _ in range(count):
            number, names = len(items), {child.object.name for child in owner.children}
            while f"item{number}" in names:
                number += 1
            items.append(scene.clone(template, f"item{number}"))
            playback.spawn(items[-1])
        self._put(items[-1], Visible=False)
        self.items[owner.path] = tuple(items)
        self.scrolls[owner.path] = _Scroll(count, most, base, len(items) - 1, bar=bar)
        self._refresh(owner, False)
        self._lay(owner, True)
        if bar is not None:
            self._scrolled(bar, self._first(owner))
        if held["UseFocus"] and self.profile.enum_label(held, "CursorType") != "NoCursor":
            cursor = int(self._held(owner)["CursorIndex"])
            for place, item in enumerate(items):
                if place == cursor:
                    self._focus(item, True)
                else:
                    self._choose(item, False)

    def _lay(self, owner: GuiSceneNode, forward: bool) -> None:
        record, offset = self.scrolls[owner.path], [f32(part) for part in self._held(owner)["ItemOffset"]]
        start = [f32((0.0 if forward else -part) + base) for part, base in zip(offset, record.base)]
        for place, item in enumerate(self.items[owner.path]):
            self._put(item, Position=[f32(f32(place * part) + at) for part, at in zip(offset, start)])

    def _first(self, owner: GuiSceneNode) -> int:
        record = self.scrolls[owner.path]
        place = self._range(self._held(owner), record.count)[0] + (record.hidden == 0)
        return int(self._held(self.items[owner.path][place])["ListIndex"])

    def _current(self, owner: GuiSceneNode) -> GuiSceneNode | None:
        items, cursor = self.items[owner.path], int(self._held(owner)["CursorIndex"])
        return items[cursor + (self.scrolls[owner.path].hidden == 0)] if 0 <= cursor < len(items) - 1 else None

    def _step(self, owner: GuiSceneNode, step: int) -> None:
        held, record, items = self._held(owner), self.scrolls[owner.path], self.items[owner.path]
        kind, loop, one = self.profile.enum_label(held, "CursorType"), held["Loop"], held["ScrollOneLineEnabled"]
        count, most, old, saved = record.count, record.most, int(held["SelectedIndex"]), int(held["CursorIndex"])
        begin, end = self._range(held, count)
        size = end - begin + 1
        last = (most - size if kind == "NoCursor" else most - 1) if step > 0 else 0
        if record.state or (most < 2 and count < 2 if loop else old == last) \
                or kind == "NoCursor" and (most < count or most == size):
            return
        before, limit = self._current(owner), most - size + 1 if kind == "NoCursor" else most
        chosen, cursor, glide, skipped, found = old, saved, False, False, True
        while True:
            chosen += step
            if not 0 <= chosen < limit:
                if not loop:
                    chosen, cursor, found = old, saved, False
                    break
                chosen = 0 if step > 0 else limit - 1
                cursor = (begin if step > 0 else end) if kind == "Move" else cursor
                self._put(owner, SelectedIndex=chosen, CursorIndex=cursor)
                self._refresh(owner, False)
                cursor -= step if kind == "Move" else 0
            if kind == "Move":
                cursor += step
                if cursor > end if step > 0 else cursor < begin:
                    glide, cursor = True, end if step > 0 else begin
                    self._put(owner, SelectedIndex=chosen, CursorIndex=cursor)
                    self._refresh(owner, False)
            elif kind == "Fix":
                glide = True
                if chosen > 0 if step > 0 else chosen < limit - 1:
                    self._put(owner, SelectedIndex=chosen)
                    self._refresh(owner, False)
            else:
                glide = chosen > 0 if step > 0 else chosen < limit - 1
                break
            if chosen == old:
                cursor, found = saved, False
                break
            self._put(owner, SelectedIndex=chosen, CursorIndex=cursor)
            item = self._current(owner)
            if item is not None and (one or self._held(item)["CanSelect"]):
                break
            skipped = True
        self._put(owner, SelectedIndex=chosen, CursorIndex=cursor)
        focus = held["UseFocus"] and kind != "NoCursor"
        if glide and not skipped:
            self._put(items[record.hidden], Visible=True)
            record.hidden = 0 if step > 0 else len(items) - 1
            self._refresh(owner, True)
            self._lay(owner, step > 0)
            if record.bar is not None and self._held(record.bar)["AutoScroll"]:
                self._scrolled(record.bar, self._first(owner))
            if focus:
                self._refocus(owner, before, old, one)
            record.elapsed, record.state = 0.0, 2 if step > 0 else 1
        else:
            self._refresh(owner, False)
            if record.bar is not None:
                self._scrolled(record.bar, self._first(owner))
            if found and focus:
                self._refocus(owner, before, old, one)
        self.stage.playback.play_state(owner.path, "PLUS_INPUT" if step > 0 else "MINUS_INPUT")
        self.stage.layout()

    def _refocus(self, owner: GuiSceneNode, before: GuiSceneNode | None, old: int, one: bool) -> None:
        now, there = self._current(owner), self._at(owner, old)
        if before is not None:
            self._choose(before, False)
        for item, chosen in ((there, False), (now, True)):
            if item is not None and one and not self._held(item)["CanSelect"]:
                self._choose(item, False)
            elif item is not None:
                self._focus(item, not chosen)
                self._focus(item, chosen)

    def _select(self, owner: GuiSceneNode, index: int) -> bool:
        items, previous = self.items[owner.path], self.selection.get(owner.path, -1)
        if previous == index or not 0 <= index < len(items):
            return False
        if self._held(owner)["UseFocus"]:
            if 0 <= previous < len(items):
                self._focus(items[previous], False)
            self._focus(items[index], True)
        self.selection[owner.path] = index
        self._put(owner, SelectedIndex=index)
        self.stage.layout()
        return True

    def _pick(self, owner: GuiSceneNode, item: GuiSceneNode) -> None:
        index = int(self._held(item)["ListIndex"])
        if owner.path in self.grids:
            self._grid_pick(owner, item)
            return
        if owner.object.type_name == _LIST:
            self._select(owner, index)
            return
        held, record, items = self._held(owner), self.scrolls[owner.path], self.items[owner.path]
        if self.profile.enum_label(held, "CursorType") != "Move" or record.state \
                or index == int(held["SelectedIndex"]) or item is items[record.hidden] \
                or not self._held(item)["CanSelect"]:
            return
        if held["UseFocus"] and (now := self._current(owner)) is not None:
            self._focus(now, False)
        self._put(owner, SelectedIndex=index, CursorIndex=items.index(item) - (record.hidden == 0))
        if held["UseFocus"] and (now := self._current(owner)) is not None:
            self._focus(now, True)
        self.stage.layout()

    def press(self, x: float, y: float) -> None:
        item, owner = self._pointed(x, y)
        if owner is not None and self._mode(owner) == "LeftClick":
            self._pick(owner, item)

    def _keys(self, owner: GuiSceneNode) -> dict:
        return self.profile.list_keys.get(
            self.profile.enum_label(owner.render_properties, "InputType", "InputListType"), {})

    def _input(self, key: int, pressed: bool) -> bool:
        handled = False
        for owner in self._lists():
            if owner.path in self.grids:
                if key in self.profile.grid_keys and self.profile.enum_label(
                        owner.render_properties, "InputType", "InputGridType") != "None":
                    self._grid_input(owner, self.profile.grid_keys.index(key) + 1)
                    handled = True
                continue
            keys = self._keys(owner)
            step = 1 if key == keys.get("next") else -1 if key == keys.get("prev") else 0
            if step:
                (self._step if owner.object.type_name == _SCROLL else self._move)(owner, step)
                handled = True
        return handled

    def wheel(self, delta: float) -> bool:
        owners = [owner for owner in self._lists()
                  if delta and owner.path not in self.grids and self._keys(owner).get("wheel")]
        for owner in owners:
            (self._step if owner.object.type_name == _SCROLL else self._move)(owner, 1 if delta < 0 else -1)
        return bool(owners)

    def gaps(self, live: bool) -> list[tuple[str, str]]:
        game = self.profile.game
        found = [(path, translate("GuiStage", "the items show what the item they are copied from holds: the game's "
                                              "script fills them")) for path in self.scrolls]
        found += [(path, translate("GuiStage", "REasy has not read {0}'s dynamic-size item layout: the items are "
                                               "placed by ItemOffset").format(game))
                  for path in self.scrolls if self._held(self.items[path][0].parent)["DynamicSizeItemEnabled"]]
        if not live:
            return found
        lists = self._lists()
        found += [(owner.path, translate("GuiStage", "this list takes no keyboard keys (InputType {0})").format(
            self.profile.enum_label(owner.render_properties, "InputType",
                                    "InputGridType" if owner.path in self.grids else "InputListType")))
            for owner in lists if not (self.profile.enum_label(owner.render_properties, "InputType", "InputGridType")
                                       != "None" if owner.path in self.grids else self._keys(owner))]
        found += [(owner.path, translate("GuiStage", "this list takes the pointer as {0} here; the file says {1}")
                   .format(self.stage.pointer, self.profile.enum_label(owner.render_properties, "MouseSelectType")))
                  for owner in lists if self.stage.pointer and self.stage.pointer != self.profile.enum_label(
                      owner.render_properties, "MouseSelectType")]
        found += [(owner.path, translate("GuiStage", "this list takes no pointer (MouseSelectType None)"))
                  for owner in lists if self._mode(owner) == "None"]
        return [*found, *self.missed.items()]
