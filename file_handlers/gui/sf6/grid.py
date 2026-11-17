"""See docs/internals/gui-editor.md, Stage (SF6 grids)."""

from __future__ import annotations

from dataclasses import dataclass

from file_handlers.engine_math import ease
from file_handlers.native_math import f32

lazy from ..scene import GuiSceneNode

_BAR = "via.gui.ScrollBar"
_U32 = 0xFFFFFFFF


@dataclass
class _Sf6Grid:
    wide: int
    tall: int
    base: list[float]
    bars: dict[bool, GuiSceneNode]
    cursor: int = 0
    window: int = 0
    state: int = 0
    elapsed: float = 0.0
    eased: tuple = (0.0, 0.0, 0.0)
    crossed: tuple = (0.0, 0.0)
    edge: bool = False


@dataclass
class _Shape:
    cx: int
    cy: int
    mx: int
    my: int
    bx: int
    by: int
    ex: int
    ey: int
    move: bool
    fixed: bool
    force: bool
    skip_focus: bool
    skip_unfocus: bool
    column: str
    row: str
    offset: tuple
    cross: tuple

    def column_range(self) -> bool:
        if self.bx < self.ex and self.bx < self.cx - 1 and self.ex != 0 and self.ex < self.cx \
                and (self.bx != 0 or self.ex != self.cx - 1):
            return True
        return self.bx == self.ex and self.by != self.ey

    def row_range(self) -> bool:
        if self.by < self.ey and self.by < self.cy - 1 and self.ey != 0 and self.ey < self.cy \
                and (self.by != 0 or self.ey != self.cy - 1):
            return True
        return self.by == self.ey and self.bx != self.ex

    def column_span(self) -> bool:
        return self.cx <= self.mx if not self.move else self.mx > self.cx or self.column_range()

    def row_span(self) -> bool:
        return self.cy <= self.my if not self.move else self.my > self.cy or self.row_range()


class Sf6GridMixin:
    def _shape(self, owner: GuiSceneNode) -> _Shape:
        held, label = self._held(owner), self.profile.enum_label
        pairs = [int(value) & _U32 for name in ("ItemCount", "ItemMax", "CursorRangeBegin", "CursorRangeEnd")
                 for value in held[name]]
        return _Shape(*pairs, label(held, "CursorType", "GridCursorType") == "Move",
                      bool(held["CursorFixedInMoveType"]), bool(held["ForceSelectionOfUnselectableItem"]),
                      bool(held["SkipFocusAtIndexMover"]), bool(held["SkipUnfocusAtIndexMover"]),
                      label(held, "LoopTypeColumn", "GridLoopType"), label(held, "LoopTypeRow", "GridLoopType"),
                      tuple(f32(value) for value in held["ItemOffset"]),
                      tuple(f32(value) for value in held["ColumnRowOffset"]))

    def _grid(self, owner: GuiSceneNode) -> None:
        template, shape = self._template(owner), self._shape(owner)
        bars = {}
        for bar in (child for child in owner.children
                    if _BAR in self.profile.runtime_type(child.object.type_name).classes):
            direction = self.profile.enum_label(self._held(bar), "Direction", "BarDirection")
            if direction in ("Horizontal", "Vertical"):
                bars[direction == "Vertical"] = bar
        self._put(owner, SelectedIndex=0)
        if template is None:
            return
        s = shape
        wide, tall = s.cx + (s.cx < s.mx), s.cy + (s.cy < s.my)
        wide += s.mx == s.cx and (s.column_range() or not s.move)
        tall += s.my == s.cy and (s.row_range() or not s.move)
        if (s.cy <= s.my or s.cx <= s.mx) if not s.move else \
                s.my > s.cy or s.row_range() or s.cx < s.mx or s.column_range():
            self._put(owner, **{name: "Loop" for name, loop in (("LoopTypeColumn", s.column), ("LoopTypeRow", s.row))
                                if loop == "Next"})
        self._put(template, Enabled=True, CanSelect=True, CanDecide=True, Visible=True)
        self._choose(template, False)
        items = [template]
        for number in range(1, wide * tall):
            items.append(self.stage.scene.clone(template, f"item{number}"))
            self.stage.playback.spawn(items[-1])
        self.items[owner.path] = tuple(items)
        record = self.grids[owner.path] = _Sf6Grid(wide, tall, list(self._held(template)["Position"]), bars)
        record.cursor = (s.bx if s.column_range() else 0) + (s.by * wide if s.row_range() else 0)
        self._grid_place(owner, [f32(0.0)] * 3)
        self._grid_hide(owner, True, True)
        self._grid_number(owner)
        self._grid_cull(owner)
        for axis, bar in bars.items():
            ranged = s.row_range() if axis else s.column_range()
            self._put(bar, ItemCount=(s.ey - s.by + 1 if axis else s.ex - s.bx + 1) if ranged else (
                s.cy if axis else s.cx), ItemMax=s.my if axis else s.mx, CursorType=self._held(owner)["CursorType"])
            self._layout(bar)
        self._grid_bars(owner, 0)
        if self._held(owner)["UseFocus"]:
            for item in items:
                self._choose(item, False)
            self._choose(items[record.cursor], True)

    def assigned(self, node: GuiSceneNode, name: str) -> None:
        super().assigned(node, "ItemCount" if name == "CursorType" and node.path in self.grids else name)

    def _grid_offset(self, owner: GuiSceneNode, column: bool, row: bool) -> list[float]:
        s = self._shape(owner)
        (ox, oy, oz), (crx, cry) = s.offset, s.cross
        x = y = z = f32(0.0)
        if column:
            x, y, z = f32(x - ox), f32(y - cry), f32(z - oz)
        if row:
            x, y, z = f32(x - crx), f32(y - oy), f32(z - oz)
        return [x, y, z]

    def _grid_extra(self, owner: GuiSceneNode, direction: int = 0, glide: bool = False) -> list[float]:
        record = self.grids[owner.path]
        column, row = record.window % record.wide == 1, record.window // record.wide == 1
        if direction:
            column = {1: column, 2: not glide, 3: column, 4: glide}[direction]
            row = {1: glide, 2: row, 3: not glide, 4: row}[direction]
        return self._grid_offset(owner, column, row)

    def _grid_place(self, owner: GuiSceneNode, extra: list[float]) -> None:
        record, s = self.grids[owner.path], self._shape(owner)
        (ox, oy, oz), (crx, cry), (bx, by, bz) = s.offset, s.cross, record.base
        items = self.items[owner.path]
        for row in range(record.tall):
            for column in range(record.wide):
                r, c = f32(row), f32(column)
                self._put(items[row * record.wide + column], Visible=True, Position=[
                    f32(f32(f32(f32(r * crx) + f32(c * ox)) + extra[0]) + f32(bx)),
                    f32(f32(f32(f32(r * oy) + f32(c * cry)) + f32(by)) + extra[1]),
                    f32(f32(f32(bz) + extra[2]) + f32(f32(column + row) * oz))])

    def _grid_hide(self, owner: GuiSceneNode, columns: bool, rows: bool) -> None:
        record, s, items = self.grids[owner.path], self._shape(owner), self.items[owner.path]
        wide, tall = record.wide, record.tall
        if columns and wide > s.cx:
            column = wide - 1 if record.window % wide == 0 else 0
            for row in range(tall):
                self._put(items[row * wide + column], Visible=False)
        if rows and tall > s.cy:
            row = tall - 1 if record.window // wide == 0 else 0
            for column in range(wide):
                self._put(items[row * wide + column], Visible=False)

    def _grid_number(self, owner: GuiSceneNode) -> None:
        record, s, items = self.grids[owner.path], self._shape(owner), self.items[owner.path]
        wide, chosen = record.wide, int(self._held(owner)["SelectedIndex"])
        if s.move:
            first = chosen - s.mx * (record.cursor // wide) - record.cursor % wide
            for place, item in enumerate(items):
                self._put(item, ListIndex=s.mx * (place // wide) + place % wide + first)
            return
        column, total = record.cursor % wide, s.mx * s.my
        start = chosen - s.mx * (record.cursor // wide) - column + (s.mx if chosen % s.mx < column else 0)
        start += total if start < 0 else 0
        for row in range(record.tall):
            wrapped = False
            for place in range(wide):
                value = s.mx * row + place + start - (s.mx if wrapped else 0)
                wrapped = wrapped or value % s.mx == s.mx - 1
                self._put(items[row * wide + place], ListIndex=value - total if value >= total else value)

    def _grid_cull(self, owner: GuiSceneNode) -> None:
        record, s, items = self.grids[owner.path], self._shape(owner), self.items[owner.path]
        columns, rows = s.column_range(), s.row_range()
        if not s.move or not (columns or rows):
            return
        first = int(self._held(owner)["SelectedIndex"]) // s.mx - record.cursor // record.wide
        for place, item in enumerate(items):
            index, row = int(self._held(item)["ListIndex"]), first + place // record.wide
            if rows and not 0 <= index < s.my * s.mx or columns and not row * s.mx <= index <= row * s.mx + s.mx - 1:
                self._put(item, Visible=False)

    def _grid_scroll(self, owner: GuiSceneNode, vertical: bool) -> int:
        record, s = self.grids[owner.path], self._shape(owner)
        wide, chosen = record.wide, int(self._held(owner)["SelectedIndex"])
        if not s.move:
            index = int(self._held(self.items[owner.path][record.window])["ListIndex"])
            return index // s.mx if vertical else index % s.mx
        if vertical:
            return record.window // wide + chosen // s.mx - record.cursor // wide + (s.by if s.row_range() else 0)
        return record.window % wide + chosen % s.mx - record.cursor % wide + (s.bx if s.column_range() else 0)

    def _grid_bars(self, owner: GuiSceneNode, direction: int) -> None:
        record = self.grids[owner.path]
        for vertical in ((True, False) if direction == 0 else (direction in (1, 3),)):
            bar = record.bars.get(vertical)
            if bar is not None and (direction == 0 or self._held(bar)["AutoScroll"]):
                self._scrolled(bar, self._grid_scroll(owner, vertical))

    def _grid_input(self, owner: GuiSceneNode, direction: int) -> None:
        record, held = self.grids.get(owner.path), self._held(owner)
        if record is not None:
            self._grid_move(owner, direction, False, (int(held["SelectedIndex"]), record.cursor, record.window))
        self.stage.playback.play_state(owner.path, "PLUS_INPUT" if direction in (2, 3) else "MINUS_INPUT")
        self.stage.layout()

    def _grid_move(self, owner: GuiSceneNode, direction: int, forced: bool, saved: tuple[int, int, int]) -> None:
        record, s = self.grids[owner.path], self._shape(owner)
        if record.state or s.column not in ("Clamp", "Loop", "Next") or s.row not in ("Clamp", "Loop", "Next"):
            return
        record.edge, wrapped = False, False
        if s.move:
            wrapped = self._grid_step(owner, direction, saved)
            if not s.fixed:
                (self._grid_wrap if wrapped else self._grid_shift)(owner, direction)
        else:
            self._grid_fix_step(owner, direction)
            self._grid_fix_place(owner, direction)
        items, chosen = self.items[owner.path], int(self._held(owner)["SelectedIndex"])
        if not (s.fixed and s.move):
            if chosen == saved[0]:
                record.state, record.elapsed = 0, f32(self._held(owner)["ScrollTime"]) if record.state else \
                    record.elapsed
                record.cursor, record.window = saved[1], saved[2]
                self._grid_place(owner, self._grid_extra(owner))
                self._grid_hide(owner, True, True)
                self._grid_number(owner)
                self._grid_cull(owner)
                self._grid_bars(owner, 0)
            else:
                item = items[record.cursor] if 0 <= record.cursor < len(items) else None
                if item is None or not self._held(item)["CanSelect"]:
                    if record.state:
                        record.state, record.elapsed = 0, f32(self._held(owner)["ScrollTime"])
                    self._grid_move(owner, direction, True, saved)
                    return
                if record.state and not forced:
                    for place in items:
                        self._put(place, Visible=True)
                    self._grid_hide(owner, record.state in (1, 3), record.state in (2, 4))
                    self._grid_cull(owner)
                elif record.state:
                    record.elapsed = f32(self._held(owner)["ScrollTime"])
                    self._grid_glide(owner, 0.0)
        if self._held(owner)["UseFocus"] and int(self._held(owner)["SelectedIndex"]) != saved[0]:
            self._grid_focus(owner, direction, saved)

    def _grid_focus(self, owner: GuiSceneNode, direction: int, saved: tuple[int, int, int]) -> None:
        record, s, items = self.grids[owner.path], self._shape(owner), self.items[owner.path]
        old = items[saved[1]]
        was = bool(self._held(old)["Selected"])
        self._choose(old, False)
        if not s.skip_unfocus and was:
            shown = self._grid_shown(owner, direction, saved[0]) if not s.move else self._grid_holder(owner, saved[0])
            if shown is not None:
                self._focus(shown, True)
                self._focus(shown, False)
        if 0 <= record.cursor < len(items) and self._held(items[record.cursor])["CanSelect"]:
            (self._choose if s.skip_focus else self._focus)(items[record.cursor], True)

    def _grid_shown(self, owner: GuiSceneNode, direction: int, index: int) -> GuiSceneNode | None:
        record, items = self.grids[owner.path], self.items[owner.path]
        wide, tall, column, row = record.wide, record.tall, record.cursor % record.wide, record.cursor // record.wide
        places = {1: [wide * at + column for at in reversed(range(tall))], 2: [row * wide + at for at in range(wide)],
                  3: [wide * at + column for at in range(tall)],
                  4: [row * wide + at for at in reversed(range(wide))]}[direction]
        return next((items[place] for place in places if int(self._held(items[place])["ListIndex"]) == index
                     and self._held(items[place])["Visible"]), None)

    def _grid_holder(self, owner: GuiSceneNode, index: int) -> GuiSceneNode | None:
        record, s = self.grids[owner.path], self._shape(owner)
        chosen, wide = int(self._held(owner)["SelectedIndex"]), record.wide
        across, down = chosen % s.mx - record.cursor % wide, chosen // s.mx - record.cursor // wide
        row, column = index // s.mx, index % s.mx
        if across <= column <= wide - 1 + across and down <= row <= record.tall - 1 + down:
            place = (row - down) * wide - across + column
            if place != record.cursor:
                return self.items[owner.path][place]
        return None

    def _grid_fix_step(self, owner: GuiSceneNode, direction: int) -> None:
        s, chosen = self._shape(owner), int(self._held(owner)["SelectedIndex"])
        if direction == 1:
            chosen = chosen - s.mx if chosen >= s.mx else (s.my - 1) * s.mx + chosen
        elif direction == 2:
            chosen = chosen + 1 - (s.mx if chosen % s.mx == s.mx - 1 else 0)
        elif direction == 3:
            chosen = chosen + s.mx - (s.mx * s.my if chosen + s.mx >= s.mx * s.my else 0)
        else:
            chosen = chosen - 1 + (s.mx if chosen % s.mx == 0 else 0)
        self._put(owner, SelectedIndex=chosen)

    def _grid_fix_place(self, owner: GuiSceneNode, direction: int) -> None:
        record = self.grids[owner.path]
        wide, shift = record.wide, {1: -record.wide, 2: 1, 3: record.wide, 4: -1}[direction]
        edge = {1: record.window // wide == 1, 2: record.window % wide == 0, 3: record.window // wide == 0,
                4: record.window % wide == 1}[direction]
        if edge:
            record.cursor, record.window = record.cursor + shift, record.window + shift
        record.state, record.elapsed = direction, 0.0
        self._grid_place(owner, self._grid_glide_extra(owner, direction))
        self._grid_hide(owner, True, True)
        self._grid_number(owner)
        self._grid_bars(owner, direction)

    def _grid_step(self, owner: GuiSceneNode, direction: int, saved: tuple[int, int, int]) -> bool:
        s = self._shape(owner)
        if s.fixed:
            return self._grid_fixed(owner, direction)
        old = int(self._held(owner)["SelectedIndex"])
        vertical = direction in (1, 3)
        moved = (old - s.mx, old + 1, old + s.mx, old - 1)[direction - 1]
        if (moved >= 0 if direction == 1 else moved < s.mx * s.my if direction == 3
                else (moved & _U32) // s.mx == old // s.mx):
            self._put(owner, SelectedIndex=moved)
            return False
        loop = s.row if vertical else s.column
        if loop == "Loop":
            self._put(owner, SelectedIndex=((s.my - 1) * s.mx + old, old - s.mx + 1, old % s.mx, old - 1 + s.mx)[
                direction - 1])
        elif loop == "Next":
            self._grid_next(owner, direction, saved)
            if not self.grids[owner.path].edge:
                now = int(self._held(owner)["SelectedIndex"])
                self._put(owner, SelectedIndex=(now + (s.my - 1) * s.mx, now + 1 - s.mx, now % s.mx,
                                                now + s.mx - 1)[direction - 1])
        else:
            self.grids[owner.path].edge = True
            self._put(owner, SelectedIndex=saved[0])
        return True

    def _grid_next(self, owner: GuiSceneNode, direction: int, saved: tuple[int, int, int]) -> None:
        held = self._held(owner)
        loops = held["LoopTypeColumn"], held["LoopTypeRow"]
        self._put(owner, **{name: "Loop" for name in ("LoopTypeColumn", "LoopTypeRow")
                            if self.profile.enum_label(held, name, "GridLoopType") == "Next"})
        across = 5 - direction
        (self._grid_wrap if self._grid_step(owner, across, saved) else self._grid_shift)(owner, across)
        self._put(owner, LoopTypeColumn=loops[0], LoopTypeRow=loops[1])

    def _grid_shift(self, owner: GuiSceneNode, direction: int) -> None:
        record, s = self.grids[owner.path], self._shape(owner)
        if s.fixed:
            return
        wide, tall, glide = record.wide, record.tall, False
        if direction in (1, 3):
            span, ranged, limit = s.row_span(), s.row_range(), (s.by, s.ey)
            step, at, last, edge = wide, lambda value: value // wide, tall - 1, record.window // wide
        else:
            span, ranged, limit = s.column_span(), s.column_range(), (s.bx, s.ex)
            step, at, last, edge = 1, lambda value: value % wide, wide - 1, record.window % wide
        sign, end = (-1, 0) if direction in (1, 4) else (1, last)
        if span and at(record.cursor) == end:
            glide = True
        else:
            record.cursor += sign * step
            now = at(record.cursor)
            if span and not ranged:
                if now == end and edge == (1 if sign < 0 else 0):
                    record.window += sign * step
                    glide = True
            elif span and ((((now - edge) & _U32) < limit[0] if sign < 0 else limit[1] < ((now - edge) & _U32))
                           or now == end and edge == (1 if sign < 0 else 0)):
                glide = True
                if edge == (1 if sign < 0 else 0):
                    record.window += sign * step
                else:
                    record.cursor -= sign * step
        if glide:
            record.state, record.elapsed = direction, 0.0
            extra = self._grid_glide_extra(owner, direction)
        else:
            extra = self._grid_extra(owner)
        self._grid_place(owner, extra)
        self._grid_hide(owner, True, True)
        if record.state:
            self._grid_number(owner)
            self._grid_bars(owner, direction)
        self._grid_cull(owner)

    def _grid_glide_extra(self, owner: GuiSceneNode, direction: int) -> list[float]:
        record = self.grids[owner.path]
        column, row = record.window % record.wide == 1, record.window // record.wide == 1
        return self._grid_offset(owner, {1: column, 2: False, 3: column, 4: True}[direction],
                                 {1: True, 2: row, 3: False, 4: row}[direction])

    def _grid_wrap(self, owner: GuiSceneNode, direction: int) -> None:
        record, s = self.grids[owner.path], self._shape(owner)
        if s.fixed or record.edge:
            return
        wide = record.wide
        if direction == 1:
            record.cursor += ((s.ey - s.by + 1) if s.row_range() else record.tall - 1) * wide
            if s.row_span() and record.window // wide == 0:
                record.window += wide
        elif direction == 2:
            record.cursor += (s.bx - s.ex - 1) if s.column_range() else 1 - wide
            if s.column_span() and record.window % wide == 1:
                record.window -= 1
        elif direction == 3:
            record.cursor = record.cursor - (s.ey - s.by + 1) * wide if s.row_range() else record.cursor % wide
            if s.row_span() and record.window // wide == 1:
                record.window -= wide
        else:
            record.cursor += (s.ex - s.bx + 1) if s.column_range() else wide - 1
            if s.column_span() and record.window % wide == 0:
                record.window += 1
        self._grid_place(owner, self._grid_extra(owner))
        self._grid_hide(owner, True, True)
        self._grid_number(owner)
        self._grid_cull(owner)
        self._grid_bars(owner, direction)

    def _grid_fixed(self, owner: GuiSceneNode, direction: int) -> bool:
        record, s, items = self.grids[owner.path], self._shape(owner), self.items[owner.path]
        wide, start, total = record.wide, int(self._held(owner)["SelectedIndex"]), s.mx * s.my
        vertical, back = direction in (1, 3), direction in (1, 4)
        (crow, ccol), (wrow, wcol) = divmod(record.cursor, wide), divmod(record.window, wide)
        here, edge = (crow, wrow) if vertical else (ccol, wcol)
        low = (s.by if s.row_range() else 0) if vertical else (s.bx if s.column_range() else 0)
        high = (s.ey if s.row_range() else s.cy - 1) if vertical else (s.ex if s.column_range() else s.cx - 1)
        free = s.row_span() if vertical else s.column_span()
        clamp = s.column == "Clamp"
        if not free:
            record.edge = record.edge or clamp
            return True
        step, size = (s.mx, s.my) if vertical else (1, s.mx)
        line = (lambda value: value // s.mx) if vertical else (lambda value: value % s.mx)
        room = line if back else (lambda value: size - line(value) - 1)
        reach = here - low - edge if back else high - here + edge
        selectable = lambda index: 0 <= index < total and bool(self._held(items[record.cursor])["CanSelect"])
        chosen, glide, wrapped = start, False, not reach < room(start)
        if not wrapped and s.force:
            chosen, glide = start + (-step if back else step), True
        elif not wrapped:
            count = 0
            while line(chosen) != (0 if back else size - 1):
                chosen, count = chosen + (-step if back else step), count + 1
                if selectable(chosen):
                    glide = count == 1
                    break
                if not reach < room(chosen):
                    wrapped = True
                    break
        if wrapped:
            record.edge = record.edge or clamp
            if record.edge:
                return True
            row, column = start // s.mx, start % s.mx
            target = {1: ((crow - 1) + (s.my - high - wrow - row)) * s.mx + start,
                      3: start - ((low - crow) + wrow + row) * s.mx, 2: start - low - wcol - column + ccol,
                      4: (ccol - 1) + (start - high - wcol - column + s.mx)}[direction]
            while target != start and not (s.force or selectable(target)):
                target += step if direction in (2, 3) else -step
            if target == start:
                return True
            self._put(owner, SelectedIndex=target)
            shift = (wide if vertical else 1) * (1 if back else -1)
            if edge == (0 if back else 1):
                record.window, record.cursor = record.window + shift, record.cursor + shift
            self._grid_place(owner, self._grid_extra(owner))
            self._grid_number(owner)
            self._grid_cull(owner)
            self._grid_hide(owner, True, True)
            self._grid_bars(owner, direction)
            return True
        self._put(owner, SelectedIndex=chosen)
        shift = (wide if vertical else 1) * (-1 if back else 1)
        if edge == (1 if back else 0):
            record.window, record.cursor = record.window + shift, record.cursor + shift
        extra = self._grid_extra(owner, direction, glide)
        if glide:
            record.state, record.elapsed = direction, 0.0
        self._grid_place(owner, extra)
        self._grid_number(owner)
        self._grid_cull(owner)
        self._grid_hide(owner, not glide or vertical, not glide or not vertical)
        self._grid_bars(owner, direction)
        return False

    def _grid_glide(self, owner: GuiSceneNode, frames: float) -> None:
        record, held = self.grids[owner.path], self._held(owner)
        if not record.state:
            return
        s, time = self._shape(owner), f32(held["ScrollTime"])
        record.elapsed = min(f32(f32(frames) + record.elapsed), time)
        at = f32(record.elapsed / time) if 0.0 < time else f32(1.0)
        kind = held["ScrollEase"]
        eased = tuple(f32(ease(kind, 0.0, part, at)) for part in s.offset)
        crossed = tuple(f32(ease(kind, 0.0, part, at)) for part in s.cross)
        x, y, z = (f32(now - before) for now, before in zip(eased, record.eased))
        cx, cy = (f32(now - before) for now, before in zip(crossed, record.crossed))
        x, y, z = ((cx, y, z), (-x, -cy, -z), (-cx, -y, -z), (x, cy, z))[record.state - 1]
        for item in self.items[owner.path]:
            self._put(item, Position=[f32(part + now) for part, now in zip((x, y, z), self._held(item)["Position"])])
        record.eased, record.crossed = eased, crossed
        if time <= record.elapsed:
            self._grid_hide(owner, True, True)
            record.state, record.eased, record.crossed = 0, (0.0, 0.0, 0.0), (0.0, 0.0)

    def _grid_pick(self, owner: GuiSceneNode, item: GuiSceneNode) -> None:
        record, items = self.grids.get(owner.path), self.items.get(owner.path, ())
        if record is None or item not in items or record.state or not self._held(item)["CanSelect"]:
            return
        s, index, old = self._shape(owner), int(self._held(item)["ListIndex"]), int(self._held(owner)["SelectedIndex"])
        if not 0 <= index < s.mx * s.my or index == old:
            return
        self._put(owner, SelectedIndex=index)
        before = record.cursor
        moved = not s.move or self._grid_reach(owner, old, index)
        if moved:
            self._grid_place(owner, self._grid_extra(owner))
            self._grid_hide(owner, True, True)
            self._grid_number(owner)
            self._grid_cull(owner)
            self._grid_bars(owner, 0)
        if self._held(owner)["UseFocus"]:
            if not s.move:
                if self._held(items[record.cursor])["Selected"]:
                    shown = next((place for place in items if int(self._held(place)["ListIndex"]) == old
                                  and self._held(place)["Visible"]), None)
                    if shown is not None:
                        self._focus(shown, True)
                        self._focus(shown, False)
                self._focus(items[record.cursor], False)
            elif not moved:
                self._focus(items[before], False)
            else:
                was = bool(self._held(items[before])["Selected"])
                self._choose(items[before], False)
                shown = self._grid_holder(owner, old) if was else None
                if shown is not None:
                    self._focus(shown, True)
                    self._focus(shown, False)
            self._focus(items[record.cursor], True)
        self.stage.layout()

    def _grid_reach(self, owner: GuiSceneNode, old: int, index: int) -> bool:
        record, s = self.grids[owner.path], self._shape(owner)
        wide, (column, row) = record.wide, (index % s.mx, index // s.mx)
        across, down = old % s.mx - record.cursor % wide, old // s.mx - record.cursor // wide
        left, top = record.window % wide + across, record.window // wide + down
        right, bottom = s.cx - 1 + left, s.cy - 1 + top
        sx = column - right if right < column else column - left if column < left else 0
        sy = row - bottom if bottom < row else row - top if row < top else 0
        moved = bool(sx or sy)
        if moved:
            left, top = left + sx, top + sy
            limit_x, limit_y = wide - s.cx + across, record.tall - s.cy + down
            ex = left - limit_x if limit_x < left else left - across if left < across else 0
            ey = top - limit_y if limit_y < top else top - down if top < down else 0
            record.window = (top - (ey + down)) * wide - (ex + across) + left
            record.cursor = (row - (ey + down)) * wide - (ex + across) + column
        else:
            record.cursor = (row - down) * wide - across + column
        if s.column_range():
            gap = (record.cursor % wide - record.window % wide) & _U32
            if gap < s.bx or s.ex < gap:
                record.cursor, moved = record.cursor + (s.bx - gap if gap < s.bx else s.ex - gap), True
                if record.window % wide == (1 if gap < s.bx else 0):
                    shift = -1 if gap < s.bx else 1
                    record.cursor, record.window = record.cursor + shift, record.window + shift
        if s.row_range():
            gap = (record.cursor // wide - record.window // wide) & _U32
            if gap < s.by or s.ey < gap:
                record.cursor, moved = record.cursor + ((s.by - gap) if gap < s.by else s.ey - gap) * wide, True
                if record.window // wide == (1 if gap < s.by else 0):
                    shift = -wide if gap < s.by else wide
                    record.cursor, record.window = record.cursor + shift, record.window + shift
        return moved
