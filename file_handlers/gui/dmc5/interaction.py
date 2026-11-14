"""See docs/internals/gui-editor.md, Stage."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from PySide6.QtCore import QCoreApplication, QPointF, QRectF, Qt

from file_handlers.engine_math import ease
from file_handlers.native_math import f32

lazy from ..model import GuiTextureSet
lazy from ..scene import GuiSceneNode
lazy from .scene import project_gui_point
lazy from .text_layout import TextLayoutGap
lazy from .vertices import bounds, circle_points, hit_polygon

_EPSILON = float(np.finfo(np.float32).eps)
_CONTROL, _LIST, _ITEM = "via.gui.Control", "via.gui.SimpleList", "via.gui.SelectItem"
_SCROLL, _GRID = _FILLED = ("via.gui.ScrollList", "via.gui.ScrollGrid")
_BAR = "via.gui.ScrollBar"
_KEYS = {Qt.Key.Key_Backspace: 8, Qt.Key.Key_Tab: 9, Qt.Key.Key_Return: 13, Qt.Key.Key_Enter: 146,
         Qt.Key.Key_Escape: 27, Qt.Key.Key_Space: 32, Qt.Key.Key_PageUp: 33, Qt.Key.Key_PageDown: 34,
         Qt.Key.Key_End: 35, Qt.Key.Key_Home: 36, Qt.Key.Key_Left: 37, Qt.Key.Key_Up: 38, Qt.Key.Key_Right: 39,
         Qt.Key.Key_Down: 40, Qt.Key.Key_Insert: 45, Qt.Key.Key_Delete: 46}


@dataclass
class _Scroll:
    count: int
    most: int
    base: list[float]
    hidden: int
    state: int = 0
    elapsed: float = 0.0
    eased: tuple[float, ...] = (0.0, 0.0, 0.0)
    bar: GuiSceneNode | None = None


@dataclass
class _Grid:
    wide: int
    tall: int
    base: list[float]
    bars: dict[bool, GuiSceneNode]
    cursor: int = 0
    window: int = 0
    state: int = 0
    elapsed: float = 0.0
    eased: tuple = (0.0, 0.0, 0.0)
    loops: list[str] = field(default_factory=list)


@dataclass
class _Bar:
    thumb: GuiSceneNode
    base: list[float]
    step: list[float]


translate = QCoreApplication.translate


def virtual_key(event) -> int:
    key = event.key()
    return int(event.nativeVirtualKey()) or _KEYS.get(key, int(key) if 0 <= int(key) <= 0xFF else 0)


def _inside(point: tuple[float, float], first, second, third) -> bool:
    x, y = f32(point[0]), f32(point[1])

    def edge(start, end) -> float:
        return f32(f32(f32(y - end[1]) * f32(end[0] - start[0])) - f32(f32(x - end[0]) * f32(end[1] - start[1])))

    signs = edge(first, second), edge(second, third), edge(third, first)
    return all(value > 0.0 for value in signs) or all(value < 0.0 for value in signs)


class Dmc5GuiInteraction:
    def __init__(self, stage) -> None:
        self.stage, self.profile = stage, stage.scene.workspace.profile
        self.selection: dict[str, int] = {}
        self.missed: dict[str, str] = {}
        self.down: dict[int, tuple[float | None, bool]] = {}
        self.clock = f32(0.0)
        icons = self.profile.icons
        self.keys = {action: icons["device"][icons["pad"][str(button)]][0]
                     for action, button in icons["actions"].values()
                     if icons["pad"].get(str(button)) in icons["device"]}
        self.actions = {button: action for action, button in icons["actions"].values()}
        self.items: dict[str, tuple[GuiSceneNode, ...]] = {}
        self.scrolls: dict[str, _Scroll] = {}
        self.grids: dict[str, _Grid] = {}
        self.bars: dict[str, _Bar] = {}
        stage.scene.withheld.clear()
        self._awake(stage.scene.root)

    def _awake(self, node: GuiSceneNode) -> None:
        build = {_LIST: self._simple, _SCROLL: self._scroll, _GRID: self._grid, _BAR: self._layout}.get(
            node.object.type_name)
        if build is not None:
            build(node)
        for child in list(node.children):
            self._awake(child)

    def assigned(self, node: GuiSceneNode, name: str) -> None:
        if node.object.type_name in _FILLED and name in ("ItemCount", "ItemMax"):
            for item in self.stage.scene.drop_clones(node):
                self.stage.playback.forget(item)
            self._awake(node)
        elif _BAR in self.profile.runtime_type(node.object.type_name).classes and name in ("Direction",
                                                                                           "BarVisibility"):
            if name == "Direction" or node.path not in self.bars:
                self._layout(node)
            if node.path in self.bars:
                self._place(node)

    def _bar(self, owner: GuiSceneNode) -> GuiSceneNode | None:
        return next((child for child in owner.children
                     if _BAR in self.profile.runtime_type(child.object.type_name).classes), None)

    def _layout(self, bar: GuiSceneNode) -> None:
        fact, held = self.profile.runtime_type(bar.object.type_name).facts["bar"], self._held(bar)
        if bar.path not in self.bars:
            thumb = next((child for child in bar.children if child.object.name == fact["child"] and set(
                fact["types"]) & set(self.profile.runtime_type(child.object.type_name).classes)), None)
            if thumb is None:
                return
            self.bars[bar.path] = _Bar(thumb, list(self._held(thumb)["Position"]), [0.0, 0.0, 0.0])
        record, (width, height) = self.bars[bar.path], held["BarFullSize"]
        count, most, rest, share = int(held["ItemCount"]), int(held["ItemMax"]), 1.0, 1.0
        if self.profile.enum_label(held, "CursorType") == "Fix":
            if most > 1:
                rest, share = f32(1.0 / (most - 1)), f32(1.0 / most)
        elif count < most:
            rest, share = f32(1.0 / (most - count)), f32(count / most)
        record.step = [0.0, 0.0, 0.0]
        direction = self.profile.enum_label(held, "Direction", "BarDirection")
        if direction == "Horizontal":
            record.step[0], width = f32(f32(width - f32(width * share)) * rest), f32(width * share)
        elif direction == "Vertical":
            record.step[1], height = f32(f32(height - f32(height * share)) * rest), f32(height * share)
        size = self.profile.runtime_type(record.thumb.object.type_name).facts["size"]
        self._put(record.thumb, **{size: [width, height]})

    def _last(self, bar: GuiSceneNode) -> int:
        held = self._held(bar)
        return int(held["ItemMax"]) - (1 if self.profile.enum_label(held, "CursorType") == "Fix" else int(
            held["ItemCount"]))

    def _place(self, bar: GuiSceneNode) -> None:
        held, record = self._held(bar), self.bars[bar.path]
        at = f32(int(held["ScrollIndex"]))
        self._put(record.thumb, Position=[f32(f32(at * step) + base) for step, base in zip(record.step, record.base)])
        needed, mode = int(held["ItemCount"]) < int(held["ItemMax"]), self.profile.enum_label(held, "BarVisibility")
        withheld, thumb = self.stage.scene.withheld, record.thumb.path
        for path, shown in ((bar.path, mode != "AutoHideAll" or needed), (thumb, mode == "Always" or needed)):
            (withheld.discard if shown else withheld.add)(path)

    def _scrolled(self, bar: GuiSceneNode, index: int) -> None:
        if int(self._held(bar)["ScrollIndex"]) == index:
            return
        self._put(bar, ScrollIndex=min(max(index, 0), self._last(bar)))
        if bar.path not in self.bars:
            self._layout(bar)
        if bar.path in self.bars:
            self._place(bar)

    def _put(self, node: GuiSceneNode, **values) -> None:
        self.stage.playback.overrides[node.path].update(values)

    def _choose(self, item: GuiSceneNode, chosen: bool) -> None:
        name = ("SELECT" if chosen else "DEFAULT") if self._held(item)["Enabled"] else \
            "DISABLE_SELECT" if chosen else "DISABLE"
        self.stage.playback.play_state(item.path, name)
        self._put(item, Selected=chosen)

    def _template(self, owner: GuiSceneNode) -> GuiSceneNode | None:
        return next((child for child in owner.children if child.object.type_name == _ITEM), None)

    def _simple(self, owner: GuiSceneNode) -> None:
        items = self.items[owner.path] = tuple(child for child in owner.children if child.object.type_name == _ITEM)
        if not items:
            return
        held = self._held(owner)
        chosen = self.selection[owner.path] = int(held["SelectedIndex"])
        for index, item in enumerate(items):
            self._put(item, ListIndex=index, Selected=index == chosen)
            if index == chosen and held["UseFocus"]:
                self.stage.playback.play_state(item.path, self._state(item, True))

    def _scroll(self, owner: GuiSceneNode) -> None:
        held, template, bar = self._held(owner), self._template(owner), self._bar(owner)
        scene, playback = self.stage.scene, self.stage.playback
        count, most = max(1, int(held["ItemCount"])), max(1, int(held["ItemMax"]))
        ranged = self._ranged(held, count)
        if bar is not None:
            self._put(bar, ItemCount=int(held["CursorRangeEnd"]) - int(held["CursorRangeBegin"]) + 1 if ranged
                      else count, ItemMax=most, CursorType=held["CursorType"])
            self._layout(bar)
            if bar.path in self.bars:
                self._place(bar)
        if template is None:
            return
        cursor = int(held["CursorRangeBegin"]) if ranged else 0
        self._put(template, Visible=True, Enabled=True)
        self._choose(template, False)
        items = [template]
        for _ in range(count):
            number, names = len(items), {child.object.name for child in owner.children}
            while f"item{number}" in names:
                number += 1
            items.append(scene.clone(template, f"item{number}"))
            playback.spawn(items[-1])
            self._put(items[-1], Position=[f32(at + step) for at, step in zip(
                self._held(items[-2])["Position"], held["ItemOffset"])])
        self._put(items[-1], Visible=False)
        self._put(owner, CursorIndex=cursor)
        self.items[owner.path] = tuple(items)
        self.scrolls[owner.path] = _Scroll(count, most, list(self._held(template)["Position"]), len(items) - 1,
                                           bar=bar)
        if held["UseFocus"] and self.profile.enum_label(held, "CursorType") != "NoCursor":
            for place, item in enumerate(items):
                if place == cursor:
                    self._focus(item, True)
                else:
                    self._choose(item, False)
        self._refresh(owner, False)
        if bar is not None:
            self._scrolled(bar, int(held["SelectedIndex"]) - cursor)

    def _ranged(self, held: dict[str, object], count: int) -> bool:
        begin, end = int(held["CursorRangeBegin"]), int(held["CursorRangeEnd"])
        return self.profile.enum_label(held, "CursorType") == "Move" and 0 <= begin < min(end, count - 1) \
            and 0 < end < count

    def _refresh(self, owner: GuiSceneNode, moving: bool) -> None:
        held, record = self._held(owner), self.scrolls[owner.path]
        first = int(held["SelectedIndex"]) - int(held["CursorIndex"]) - (record.state == 2 or record.hidden == 0)
        for place, item in enumerate(self.items[owner.path]):
            index, most = first + place, record.most
            shown = 0 <= index < most and (moving or place != record.hidden)
            self._put(item, ListIndex=index + most if index < 0 else index - most if index >= most else index)
            if self._ranged(held, record.count) or not held["Loop"]:
                self._put(item, Visible=shown)
                if not shown:
                    self._choose(item, False)

    def _at(self, owner: GuiSceneNode, index: int) -> GuiSceneNode | None:
        ranged = self._ranged(self._held(owner), self.scrolls[owner.path].count)
        return next((item for item in self.items[owner.path] for held in [self._held(item)]
                     if held["ListIndex"] == index and (not ranged or held["Visible"])), None)

    def _focus(self, item: GuiSceneNode, chosen: bool) -> None:
        if self._held(item)["Selected"] != chosen:
            self.stage.playback.play_state(item.path, self._state(item, chosen))
            self._put(item, Selected=chosen)

    def _step(self, owner: GuiSceneNode, step: int) -> None:
        held, record, items = self._held(owner), self.scrolls[owner.path], self.items[owner.path]
        kind, count, most, loop = self.profile.enum_label(held, "CursorType"), record.count, record.most, held["Loop"]
        old = chosen = int(held["SelectedIndex"])
        saved = cursor = int(held["CursorIndex"])
        ranged = self._ranged(held, count)
        low, top = (int(held["CursorRangeBegin"]), int(held["CursorRangeEnd"])) if ranged else (0, count - 1)
        span, moving = most - count + 1 if kind == "NoCursor" else most, kind == "Move"
        last = (most - count if kind == "NoCursor" else most - 1) if step > 0 else 0
        if record.state or (most < 2 and count < 2 if loop else old == last) or kind == "NoCursor" and most <= count:
            return
        if kind == "Fix" and cursor == (count - 1 if step > 0 else 0):
            record.hidden = len(items) - 1 if step > 0 else 0
            self._refresh(owner, False)
        old_item, moved, skipped, found = self._at(owner, old), False, False, True
        while True:
            chosen += step
            if not 0 <= chosen < span:
                if not loop:
                    chosen, cursor, found = old, saved, False
                    break
                chosen = 0 if step > 0 else span - 1
                cursor = (low if step > 0 else top) if moving else cursor
                self._put(owner, SelectedIndex=chosen, CursorIndex=cursor)
                self._refresh(owner, False)
                cursor -= step if moving else 0
            if moving:
                cursor += step
                if cursor > top if step > 0 else cursor < low:
                    cursor, moved = (top if step > 0 else low), True
                    self._put(owner, SelectedIndex=chosen, CursorIndex=cursor)
                    self._refresh(owner, False)
            self._put(owner, SelectedIndex=chosen, CursorIndex=cursor)
            if kind == "NoCursor":
                break
            item = self._at(owner, chosen)
            if item is not None and self._held(item)["CanSelect"]:
                break
            skipped = True
            if chosen == old:
                found = False
                break
        self._put(owner, SelectedIndex=chosen, CursorIndex=cursor)
        if kind == "NoCursor":
            glide = found and (0 < chosen if step > 0 else chosen < span - 1)
        else:
            glide = moved and not skipped or kind == "Fix"
        if glide:
            self._glide(owner, step, old, old_item, found)
        else:
            self._settle(owner, step, old, old_item, found, skipped)
        self.stage.playback.play_state(owner.path, "PLUS_INPUT" if step > 0 else "MINUS_INPUT")
        self.stage.layout()

    def _settle(self, owner: GuiSceneNode, step: int, old: int, old_item: GuiSceneNode | None, found: bool,
                skipped: bool) -> None:
        held, record, items = self._held(owner), self.scrolls[owner.path], self.items[owner.path]
        chosen, kind = int(held["SelectedIndex"]), self.profile.enum_label(held, "CursorType")
        if step < 0 and skipped and chosen < record.count:
            self._put(owner, CursorIndex=chosen)
        self._refresh(owner, False)
        if held["UseFocus"] and found:
            if record.count < record.most and kind != "NoCursor":
                self._choose(items[-1] if step > 0 else items[0], False)
            new_item = self._at(owner, chosen)
            if old_item is not None and old_item is not new_item and old_item is not self._at(owner, old):
                self._choose(old_item, False)
            if old_item is not None and new_item is not None and old_item is not new_item and kind != "NoCursor":
                self._focus(old_item, False)
                self._focus(new_item, True)
        if record.bar is not None:
            self._scrolled(record.bar, chosen - int(self._held(owner)["CursorIndex"]))

    def _glide(self, owner: GuiSceneNode, step: int, old: int, old_item: GuiSceneNode | None, found: bool) -> None:
        held, record, items = self._held(owner), self.scrolls[owner.path], self.items[owner.path]
        offset = held["ItemOffset"]
        self._put(items[record.hidden], Visible=True)
        if step > 0:
            for place, item in enumerate(items):
                self._put(item, Position=[f32(f32(place * part) + at) for part, at in zip(offset, record.base)])
            record.hidden = 0
        else:
            at = [f32(base - part) for base, part in zip(record.base, offset)]
            for item in items:
                self._put(item, Position=at)
                at = [f32(part + now) for part, now in zip(offset, at)]
            record.hidden = len(items) - 1
        self._refresh(owner, True)
        if held["UseFocus"] and found and self.profile.enum_label(held, "CursorType") != "NoCursor":
            new_item, now = self._at(owner, int(held["SelectedIndex"])), self._at(owner, old)
            if old_item is not None and old_item is not new_item and old_item is not now:
                self._choose(old_item, False)
            if now is not None:
                if not self._held(now)["Selected"]:
                    self._choose(now, True)
                self._focus(now, False)
            if self._held(new_item)["Selected"]:
                self._focus(new_item, False)
            self._focus(new_item, True)
        record.elapsed, record.state = 0.0, 2 if step > 0 else 1
        if record.bar is not None and self._held(record.bar)["AutoScroll"]:
            self._scrolled(record.bar, int(held["SelectedIndex"]) - int(held["CursorIndex"]))

    def advance(self, frames: float, seconds: float) -> None:
        self.clock, (start, again) = f32(self.clock + f32(seconds * 1000.0)), self.profile.key_repeat
        for key, (stamp, taken) in list(self.down.items()):
            if stamp is None:
                self.down[key] = (self.clock, taken)
            elif start <= f32(self.clock - stamp):
                self.down[key] = (f32(f32(self.clock + again) - start), taken)
                self._input(key, False)
        for path in self.grids:
            self._grid_glide(self.items[path][0].parent, frames)
        for path, record in self.scrolls.items():
            if not record.state:
                continue
            items = self.items[path]
            held = self._held(items[0].parent)
            time = f32(held["ScrollTime"])
            record.elapsed = min(f32(f32(frames) + record.elapsed), time)
            at = 1.0 if time < _EPSILON else f32(record.elapsed / time)
            eased = tuple(ease(held["ScrollEase"], 0.0, -part if record.state == 2 else part, at)
                          for part in held["ItemOffset"])
            for item in items:
                self._put(item, Position=[f32(f32(now - before) + part) for now, before, part in zip(
                    eased, record.eased, self._held(item)["Position"])])
            record.eased = eased
            if record.elapsed == time:
                self._put(items[record.hidden], Visible=False)
                record.state, record.eased = 0, (0.0, 0.0, 0.0)

    def _grid(self, owner: GuiSceneNode) -> None:
        held, template, bars = self._held(owner), self._template(owner), {}
        scene, playback = self.stage.scene, self.stage.playback
        for bar in (child for child in owner.children
                    if _BAR in self.profile.runtime_type(child.object.type_name).classes):
            direction = self.profile.enum_label(self._held(bar), "Direction", "BarDirection")
            if direction in ("Horizontal", "Vertical"):
                axis = direction == "Vertical"
                self._put(bar, ItemCount=int(held["ItemCount"][axis]), ItemMax=int(held["ItemMax"][axis]))
                bars[axis] = bar
        if template is None or "ItemOffset" not in held:
            return
        count, most, offset = held["ItemCount"], held["ItemMax"], held["ItemOffset"]
        wide, tall = (int(count[at]) + (count[at] < most[at]) for at in range(2))
        base = self._held(template)["Position"]
        self._put(template, Visible=True)
        self._choose(template, False)
        items = [template]
        for number in range(1, wide * tall):
            items.append(scene.clone(template, f"item{number}"))
            playback.spawn(items[-1])
            self._choose(items[-1], False)
        for row in range(tall):
            for column in range(wide):
                self._put(items[row * wide + column], ListIndex=int(most[0]) * row + column, Position=[
                    f32(base[0] + f32(column * offset[0])), f32(base[1] + f32(row * offset[1])),
                    f32(base[2] + f32((column + row) * offset[2]))])
        for row in range(tall) if count[0] < most[0] else ():
            self._put(items[row * wide + wide - 1], Visible=False)
        for column in range(wide) if count[1] < most[1] else ():
            self._put(items[(tall - 1) * wide + column], Visible=False)
        self._put(owner, SelectedIndex=0)
        if len(items) == 1 or held["UseFocus"]:
            self._choose(items[0], True)
        self.items[owner.path] = tuple(items)
        if wide * tall:
            self.grids[owner.path] = _Grid(wide, tall, list(base), bars)

    def _sizes(self, owner: GuiSceneNode) -> tuple[int, int, int, int]:
        held = self._held(owner)
        return (*(int(value) for value in held["ItemCount"]), *(int(value) for value in held["ItemMax"]))

    def _grid_input(self, owner: GuiSceneNode, direction: int) -> None:
        held, record = self._held(owner), self.grids[owner.path]
        record.loops = [self.profile.enum_label(held, name, "GridLoopType") for name in ("LoopTypeColumn",
                                                                                          "LoopTypeRow")]
        self._grid_move(owner, direction, False, (int(held["SelectedIndex"]), record.cursor, record.window))
        self.stage.playback.play_state(owner.path, "PLUS_INPUT" if direction in (2, 3) else "MINUS_INPUT")
        self.stage.layout()

    def _grid_move(self, owner: GuiSceneNode, direction: int, forced: bool, saved: tuple[int, int, int]) -> None:
        record, items, held = self.grids[owner.path], self.items[owner.path], self._held(owner)
        if record.state:
            return
        loops, cursor, chosen = record.loops, record.cursor, int(held["SelectedIndex"])
        if forced:
            record.loops = ["Loop" if loop == "Next" else loop for loop in loops]
        if self._grid_next(owner, direction, saved):
            self._grid_wrap(owner, direction)
            self._grid_bar(owner, direction)
            self._grid_number(owner)
        else:
            self._grid_step(owner, direction)
        if forced:
            record.loops = loops
            return
        now, skipped = int(self._held(owner)["SelectedIndex"]), False
        if now not in (chosen, saved[0]):
            self._grid_number(owner)
            item = self._numbered(owner, now)
            if item is not None and not self._held(item)["CanSelect"]:
                record.state, skipped = 0, True
                self._grid_move(owner, direction, False, saved)
        if int(self._held(owner)["SelectedIndex"]) == saved[0]:
            record.window = saved[2]
            self._grid_place(owner, self._grid_rest(owner))
        if record.state:
            for item in items:
                self._put(item, Visible=True)
            self._grid_hide(owner, direction in (1, 3), direction in (2, 4))
            self._grid_bar(owner, direction)
            self._grid_number(owner)
            if skipped:
                record.elapsed = f32(held["ScrollTime"])
                self._grid_glide(owner, 0.0)
                self._grid_bar(owner, 0)
        if held["UseFocus"] and (record.cursor != cursor or int(self._held(owner)["SelectedIndex"]) != chosen):
            for at, focused in ((cursor, False), (record.cursor, True)):
                if 0 <= at < len(items):
                    self._focus(items[at], focused)

    def _grid_next(self, owner: GuiSceneNode, direction: int, saved: tuple[int, int, int]) -> bool:
        record, (columns, rows) = self.grids[owner.path], self._sizes(owner)[2:]
        old = int(self._held(owner)["SelectedIndex"])
        new = old + (-columns, 1, columns, -1)[direction - 1]
        if new // columns == old // columns if direction in (2, 4) else 0 <= new < columns * rows:
            self._put(owner, SelectedIndex=new)
            return False
        loop = record.loops[direction in (1, 3)]
        if loop == "Clamp":
            self._put(owner, SelectedIndex=saved[0])
            record.cursor = saved[1]
            return True
        if loop == "Next":
            self._grid_move(owner, 5 - direction, True, saved)
        now = int(self._held(owner)["SelectedIndex"])
        self._put(owner, SelectedIndex=(now + (rows - 1) * columns, now + 1 - columns, now % columns,
                                        now + columns - 1)[direction - 1])
        return True

    def _grid_step(self, owner: GuiSceneNode, direction: int) -> None:
        record, (shown, rows_shown, columns, rows) = self.grids[owner.path], self._sizes(owner)
        wide, tall, across, down = record.wide, record.tall, columns > shown, rows > rows_shown
        if direction == 1 and (not down or record.cursor // wide):
            record.cursor -= wide
            if not down or record.cursor // wide or record.window // wide != 1:
                return
            record.window -= wide
        elif direction == 2 and not (across and record.cursor % wide == wide - 1):
            record.cursor += 1
            if not across or record.cursor % wide != wide - 1 or record.window % wide:
                return
            record.window += 1
        elif direction == 3 and (not down or record.cursor // wide != tall - 1):
            record.cursor += wide
            if not down or record.cursor // wide != tall - 1 or record.window // wide:
                return
            record.window += wide
        elif direction == 4 and (not across or record.cursor % wide):
            record.cursor -= 1
            if not across or record.cursor % wide or record.window % wide != 1:
                return
            record.window -= 1
        x_offset, y_offset, z_offset = (f32(part) for part in self._held(owner)["ItemOffset"])
        x = y = z = f32(0.0)
        row, column = divmod(record.window, wide)
        horizontal = direction in (2, 4)
        if column == (0 if horizontal else 1):
            x, z = -x_offset, -z_offset
        if row == (1 if horizontal else 0):
            y = -y_offset
            if not z == -z_offset:
                z = -z_offset
            elif direction == (4 if horizontal else 1):
                z = f32(z - z_offset)
        self._grid_place(owner, [x, y, z])
        record.elapsed, record.state = 0.0, direction

    def _grid_wrap(self, owner: GuiSceneNode, direction: int) -> None:
        record, (shown, rows_shown, columns, rows) = self.grids[owner.path], self._sizes(owner)
        wide, (column, row) = record.wide, record.loops
        if direction == 1:
            if row == "Loop" or column == "Next":
                record.cursor += (record.tall - 1) * wide
            if rows > rows_shown and record.window // wide == 0:
                record.window += wide
        elif direction == 2:
            if column != "Clamp":
                record.cursor += 1 - wide
            if columns > shown and record.window % wide == 1:
                record.window -= 1
        elif direction == 3:
            if row != "Clamp":
                record.cursor %= wide
            if rows > rows_shown and record.window // wide == 1:
                record.window -= wide
        else:
            if column != "Clamp":
                record.cursor += wide - 1
            if columns > shown and record.window % wide == 0:
                record.window += 1
        self._grid_place(owner, self._grid_rest(owner))

    def _grid_rest(self, owner: GuiSceneNode) -> list:
        record, offset = self.grids[owner.path], [f32(part) for part in self._held(owner)["ItemOffset"]]
        x = y = z = f32(0.0)
        if record.window % record.wide == 1:
            x, z = -offset[0], f32(z - offset[2])
        if record.window // record.wide == 1:
            y, z = -offset[1], f32(z - offset[2])
        return [x, y, z]

    def _grid_place(self, owner: GuiSceneNode, extra: list) -> None:
        record, items = self.grids[owner.path], self.items[owner.path]
        offset, base = [f32(part) for part in self._held(owner)["ItemOffset"]], record.base
        x, y, z = f32(extra[0] + base[0]), f32(base[1] + extra[1]), f32(base[2] + extra[2])
        for row in range(record.tall):
            for column in range(record.wide):
                self._put(items[row * record.wide + column], Visible=True, Position=[
                    f32(f32(column * offset[0]) + x), f32(y + f32(row * offset[1])),
                    f32(z + f32((column + row) * offset[2]))])
        self._grid_hide(owner, True, True)

    def _grid_hide(self, owner: GuiSceneNode, columns: bool, rows: bool) -> None:
        record, items = self.grids[owner.path], self.items[owner.path]
        (shown, rows_shown, most, rows_most), wide, tall = self._sizes(owner), record.wide, record.tall
        if columns and shown < most:
            column = wide - 1 if record.window % wide == 0 else 0
            for row in range(tall):
                self._put(items[row * wide + column], Visible=False)
        if rows and rows_shown < rows_most:
            row = tall - 1 if record.window // wide == 0 else 0
            for column in range(wide):
                self._put(items[row * wide + column], Visible=False)

    def _grid_number(self, owner: GuiSceneNode) -> None:
        record, columns = self.grids[owner.path], self._sizes(owner)[2]
        wide = record.wide
        first = int(self._held(owner)["SelectedIndex"]) - columns * (record.cursor // wide) - record.cursor % wide
        for place, item in enumerate(self.items[owner.path]):
            self._put(item, ListIndex=columns * (place // wide) + place % wide + first)

    def _numbered(self, owner: GuiSceneNode, index: int) -> GuiSceneNode | None:
        return next((item for item in self.items[owner.path] if self._held(item)["ListIndex"] == index), None)

    def _grid_bar(self, owner: GuiSceneNode, direction: int) -> None:
        record, columns = self.grids[owner.path], self._sizes(owner)[2]
        vertical = direction in (1, 3) or not direction and True in record.bars
        bar = record.bars.get(vertical)
        if bar is None or direction and not self._held(bar)["AutoScroll"]:
            return
        chosen, wide = int(self._held(owner)["SelectedIndex"]), record.wide
        self._scrolled(bar, chosen // columns - record.cursor // wide + record.window // wide if vertical
                       else chosen % columns - record.cursor % wide + record.window % wide)

    def _grid_glide(self, owner: GuiSceneNode, frames: float) -> None:
        record, held = self.grids[owner.path], self._held(owner)
        if not record.state:
            return
        time = f32(held["ScrollTime"])
        record.elapsed = min(f32(f32(frames) + record.elapsed), time)
        at = f32(record.elapsed / time) if time else np.nan
        eased = tuple(ease(held["ScrollEase"], 0.0, part, at) for part in held["ItemOffset"])
        x, y, z = (f32(now - before) for now, before in zip(eased, record.eased))
        x, y, z = ((f32(0.0), y, z), (-x, f32(-0.0), -z), (f32(-0.0), -y, -z), (x, f32(0.0), z))[record.state - 1]
        for item in self.items[owner.path]:
            self._put(item, Position=[f32(part + now) for part, now in zip((x, y, z), self._held(item)["Position"])])
        record.eased = eased
        if time <= record.elapsed:
            record.state, record.eased = 0, (0.0, 0.0, 0.0)
            self._grid_hide(owner, True, True)

    def _grid_pick(self, owner: GuiSceneNode, item: GuiSceneNode) -> None:
        held, record, items = self._held(owner), self.grids[owner.path], self.items[owner.path]
        chosen, index = int(held["SelectedIndex"]), int(self._held(item)["ListIndex"])
        if record.state or chosen == index or not self._held(item)["CanSelect"]:
            return
        columns, rows = self._sizes(owner)[2:]
        new = index if 0 <= index < columns * rows else columns * rows - 1
        self._put(owner, SelectedIndex=new)
        if 0 <= record.cursor < len(items):
            self._focus(items[record.cursor], False)
        place = new - (chosen - columns * (record.cursor // record.wide) - record.cursor % record.wide)
        record.cursor = place // columns * record.wide + place % columns
        if held["UseFocus"] and 0 <= record.cursor < len(items):
            self._focus(items[record.cursor], True)
        self.stage.layout()

    def gaps(self, live: bool) -> list[tuple[str, str]]:
        scene, context = self.stage.scene, self.stage.icon_context
        filled = [node for node in scene.nodes if node.object.type_name in _FILLED]
        found = [(node.path, translate("GuiStage", "the items show what the item they are copied from holds: the "
                                                   "game's script fills them"))
                 for node in filled if node.path in self.items]
        found += [(node.path, translate("GuiStage", "its items are not made: the game leaves a grid's ItemOffset "
                                                    "unset when it stores none"))
                  for node in filled if node.path not in self.items and self._template(node)]
        if not live:
            return found
        if not context.keyboard_mode:
            return [*found, (scene.root.path,
                             translate("GuiStage", "lists are moved by the gamepad, which the preview does not read"))]
        for owner in self._lists():
            commands = self.profile.grid_input if owner.path in self.grids else [
                command for side, command in self._rule(owner).items() if side in ("prev", "next")]
            if any(self._key(command) is None for command in commands):
                found.append((owner.path,
                              translate("GuiStage", "the keys the player bound to this list's input are not known")))
            if self.stage.pointer and self.stage.pointer != self.profile.enum_label(
                    owner.render_properties, "MouseSelectType"):
                found.append((owner.path, translate(
                    "GuiStage", "this list takes the pointer as {0} here; the file says {1}").format(
                    self.stage.pointer, self.profile.enum_label(owner.render_properties, "MouseSelectType"))))
        return [*found, *self.missed.items()]

    def action(self, command: str | None) -> int | None:
        return next((self.actions[button] for button in self.stage.commands.get(command, ())
                     if button in self.actions), None)

    def _key(self, command: str | None) -> int | None:
        action = self.action(command)
        return self.stage.icon_context.keyboard_bindings.get(action, self.keys.get(action))

    def _mode(self, owner: GuiSceneNode) -> str:
        return self.stage.pointer or self.profile.enum_label(owner.render_properties, "MouseSelectType")

    def _held(self, node: GuiSceneNode) -> dict[str, object]:
        return self.stage.scene.runtime_properties(node, self.stage.playback.overrides.get(node.path))

    def _state(self, node: GuiSceneNode, selected: bool) -> str:
        if self._held(node)["Enabled"]:
            return "FOCUS" if selected else "UNFOCUS"
        return "DISABLE_FOCUS" if selected else "DISABLE_UNFOCUS"

    def _control(self, node: GuiSceneNode) -> bool:
        return _CONTROL in self.profile.runtime_type(node.object.type_name).classes

    def _enabled(self, node: GuiSceneNode | None, hit: bool) -> bool:
        while node is not None:
            held = node.render_properties
            alpha = float(held["ColorScale"][3]) if "ColorScale" in held else 1.0
            if not held["Visible"] or alpha < _EPSILON or hit and not held["HitVisible"]:
                return False
            node = node.parent
        return True

    def _interactive(self, node: GuiSceneNode | None) -> bool:
        while node is not None:
            if self._control(node) and not node.render_properties["Interactive"]:
                return False
            node = node.parent
        return True

    def _rule(self, owner: GuiSceneNode) -> dict:
        profile = self.profile
        rule = profile.list_input[profile.list_handlers.get(self.stage.scene.workspace.root_key, {}).get(
            owner.origin().path, profile.list_default)]
        return {**rule, **rule["InputType"].get(profile.enum_label(
            owner.render_properties, "InputType", "InputListType"), {})} if "InputType" in rule else rule

    def _lists(self) -> list[GuiSceneNode]:
        return [node for node in self.stage.scene.root.walk()
                if (node.path in self.grids or node.object.type_name in (_LIST, _SCROLL) and node.path in self.items)
                and self._enabled(node, False) and self._interactive(node)]

    def _chosen(self, owner: GuiSceneNode) -> GuiSceneNode | None:
        if owner.path in self.grids:
            return self._numbered(owner, int(self._held(owner)["SelectedIndex"]))
        if owner.object.type_name == _SCROLL:
            return self._at(owner, int(self._held(owner)["SelectedIndex"]))
        items, chosen = self.items[owner.path], self.selection.get(owner.path, -1)
        return items[chosen] if 0 <= chosen < len(items) else None

    def _pick(self, owner: GuiSceneNode, item: GuiSceneNode) -> None:
        if owner.path in self.grids:
            self._grid_pick(owner, item)
            return
        if owner.object.type_name == _LIST:
            self._select(owner, self.items[owner.path].index(item))
            return
        held, record = self._held(owner), self.scrolls[owner.path]
        chosen, index = int(held["SelectedIndex"]), int(self._held(item)["ListIndex"])
        if self.profile.enum_label(held, "CursorType") != "Move" or record.state or chosen == index \
                or item is self.items[owner.path][record.hidden] or not self._held(item)["CanSelect"]:
            return
        if held["UseFocus"] and (old := self._at(owner, chosen)) is not None:
            self._focus(old, False)
        self._put(owner, CursorIndex=int(held["CursorIndex"]) - chosen + index, SelectedIndex=index)
        if held["UseFocus"] and (new := self._at(owner, index)) is not None:
            self._focus(new, True)
        self.stage.layout()

    def _select(self, owner: GuiSceneNode, index: int) -> bool:
        items, playback = self.items[owner.path], self.stage.playback
        if not owner.render_properties["UseFocus"] or not 0 <= index < len(items) \
                or not items[index].render_properties["CanSelect"]:
            return False
        previous = self.selection.get(owner.path, -1)
        if previous == index:
            return False
        if 0 <= previous < len(items):
            playback.play_state(items[previous].path, self._state(items[previous], False))
            playback.overrides.setdefault(items[previous].path, {})["Selected"] = False
        playback.play_state(items[index].path, self._state(items[index], True))
        playback.overrides.setdefault(items[index].path, {})["Selected"] = True
        self.selection[owner.path] = index
        playback.overrides.setdefault(owner.path, {})["SelectedIndex"] = index
        self.stage.layout()
        return True

    def _move(self, owner: GuiSceneNode, step: int) -> None:
        items = self.items[owner.path]
        previous = candidate = self.selection.get(owner.path, 0)
        for _ in items:
            candidate += step
            if not 0 <= candidate < len(items):
                if not owner.render_properties["Loop"]:
                    break
                candidate %= len(items)
            if candidate == previous:
                break
            if items[candidate].render_properties["CanSelect"]:
                self._select(owner, candidate)
                break
        if items:
            self.stage.playback.play_state(owner.path, "PLUS_INPUT" if step > 0 else "MINUS_INPUT")

    def contains(self, node: GuiSceneNode, x: float, y: float) -> bool:
        matrix, target = node.scene_matrix, (x, y)

        def project(point: QPointF) -> tuple[float, float, float]:
            return project_gui_point((point.x(), point.y(), 0.0), matrix)

        def quad(rect: QRectF) -> bool:
            corners = [project(point) for point in (rect.topLeft(), rect.topRight(), rect.bottomLeft(),
                                                    rect.bottomRight())]
            if all(corner[2] > 1.0 for corner in corners):
                return False
            return _inside(target, corners[0], corners[2], corners[1]) or \
                _inside(target, corners[2], corners[1], corners[3])

        def box(size) -> QRectF:
            left, top, right, bottom = bounds(node, size)
            return QRectF(left, top, right - left, bottom - top)

        rect, kind = box(node.size), node.object.type_name
        if kind == "via.gui.Circle":
            rings = circle_points(node, node.size, hit_test=True) if quad(rect) else None
            if rings is None:
                return False
            outer, inner = ([QPointF(*point) for point in ring] for ring in rings)
            for at in range(len(outer) - 1):
                corners = [project(point) for point in (outer[at], inner[at], outer[at + 1], inner[at + 1])]
                if not all(corner[2] > 1.0 for corner in corners) and (
                        _inside(target, *corners[:3]) or _inside(target, *corners[1:])):
                    return True
            return False
        if kind == "via.gui.HitArea":
            shape = self.profile.enum_label(node.render_properties, "Shape", "HitAreaShape")
            if shape == "Triangle":
                return _inside(target, project(QPointF(rect.center().x(), rect.top())), project(rect.bottomLeft()),
                               project(rect.bottomRight()))
            if not quad(rect):
                return False
            if shape == "Rect":
                return True
            polygon = [project(QPointF(*point)) for point in hit_polygon(node, node.size)]
            middle = project(rect.center())
            return any(_inside(target, polygon[at], polygon[(at + 1) % len(polygon)], middle)
                       for at in range(len(polygon)))
        if "hit_layout_size" in self.profile.runtime_type(kind).facts:
            if not rect.isEmpty() and quad(rect):
                return True
            try:
                if self.stage.device is None:
                    raise TextLayoutGap(translate("GuiStage", "no frame is drawn"))
                shown = self.stage.device.display_size(node)
            except TextLayoutGap as gap:
                self.missed[node.path] = translate(
                    "GuiStage", "its text size is not known, so the pointer misses it ({0})").format(gap)
                return False
            return min(shown) > 0.0 and quad(box(shown))
        if kind == "via.gui.TextureSet" and isinstance(node.object.special_data, GuiTextureSet):
            scale = node.render_properties["Scale"]
            return any(quad(QRectF(left * scale[0], top * scale[1], (right - left) * scale[0],
                                   (bottom - top) * scale[1]).normalized())
                       for left, top, right, bottom in (entry.bounds for entry in node.object.special_data.entries))
        return not rect.isEmpty() and quad(rect)

    def hit(self, x: float, y: float) -> GuiSceneNode | None:
        scene = self.stage.scene
        return next((node for node in reversed(scene.tree_nodes)
                     if node is not scene.root and not self._control(node) and self._enabled(node, True)
                     and self.contains(node, x, y)), None)

    def _pointed(self, x: float, y: float) -> tuple[GuiSceneNode | None, GuiSceneNode | None]:
        item = self.hit(x, y)
        while item is not None and item.object.type_name != _ITEM:
            item = item.parent
        owner = item.parent if item is not None else None
        listed = owner is not None and owner in self._lists() and item in self.items[owner.path]
        return (item, owner) if listed and self._mode(owner) in ("MouseOver", "LeftClick") else (None, None)

    def _decide(self, owners: list[GuiSceneNode]) -> bool:
        handled = False
        for item in filter(None, map(self._chosen, owners)):
            handled |= self.stage.playback.play_state(item.path, "DECIDE")
        if handled:
            self.stage.layout()
        return handled

    def press(self, x: float, y: float) -> None:
        item, owner = self._pointed(x, y)
        if owner is None:
            return
        if item is self._chosen(owner) and owner.render_properties["UseFocus"]:
            self._decide([owner])
        elif self._mode(owner) == "LeftClick":
            self._pick(owner, item)

    def hover(self, x: float, y: float) -> bool:
        item, owner = self._pointed(x, y)
        if owner is not None and self._mode(owner) == "MouseOver":
            self._pick(owner, item)
        return owner is not None and bool(item.render_properties["CanSelect"])

    def key(self, event) -> bool:
        key = virtual_key(event)
        if event.isAutoRepeat():
            return self.down.get(key, (None, False))[1]
        if not self.stage.icon_context.keyboard_mode:
            return False
        self.down[key] = (None, self._input(key, True))
        return self.down[key][1]

    def release(self, event=None) -> None:
        if event is None:
            self.down.clear()
        elif not event.isAutoRepeat():
            self.down.pop(virtual_key(event), None)

    def _input(self, key: int, pressed: bool) -> bool:
        if pressed and key == self.profile.icons["menu"]["DECIDE"][0] and self._decide(self._lists()):
            return True
        handled = False
        direction = next((direction for direction in (0, 2, 3, 1)
                          if self._key(self.profile.grid_input[direction]) == key), None)
        for owner in self._lists():
            if owner.path in self.grids:
                if direction is not None:
                    self._grid_input(owner, direction + 1)
                    handled = True
                continue
            rule, held = self._rule(owner), self._held(owner)
            step = 1 if self._key(rule.get("next")) == key else -1 if self._key(rule.get("prev")) == key else 0
            if not step or rule.get("press") and not pressed or step > 0 and rule.get("more") and int(
                    held["ItemMax"]) <= int(held["ItemCount"]) + int(held["SelectedIndex"]):
                continue
            (self._step if owner.object.type_name == _SCROLL else self._move)(owner, step)
            handled = True
        return handled
