from __future__ import annotations

from dataclasses import dataclass
import math

from PySide6.QtCore import Qt, QRectF, QPointF, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (QAbstractScrollArea, QWidget, QVBoxLayout, QHBoxLayout,
                               QLineEdit, QComboBox, QSlider, QLabel, QMessageBox)

from ..mot_clip.model import ClipProperty, ClipKey


@dataclass(slots=True)
class ClipLane:
    category: str
    name: str
    prop: ClipProperty
    ancestors: tuple[ClipProperty, ...]


@dataclass(frozen=True, slots=True)
class ClipEditDrag:
    lane: ClipLane
    key: ClipKey | None
    mode: str
    anchor_frame: float


def sequence_lanes(sequences, *, prefix=''):
    result = []
    def visit_property(prop, category, path, ancestors):
        name = path+' / '+prop.name
        result.append(ClipLane(category, name, prop, ancestors))
        for child in prop.children:
            visit_property(child, category, name, (*ancestors, prop))
    def visit_node(node, category):
        for prop in node.properties:
            visit_property(prop, category, node.name.rsplit('.', 1)[-1], ())
        for child in node.children:
            visit_node(child, category)
    for sequence in sequences:
        visit_node(sequence.clip.root, prefix+sequence.category.name)
    return result


def property_members(prop):
    yield prop
    for child in prop.children:
        yield from property_members(child)


def move_timeline_item(lane, key, mode, delta, end_frame):
    """Edit native frame fields; return a rollback snapshot for one transaction."""
    props = list(property_members(lane.prop))
    saved = [(p, 'start_frame', p.start_frame) for p in (*props, *lane.ancestors)]
    saved += [(p, 'end_frame', p.end_frame) for p in (*props, *lane.ancestors)]
    saved += [(k, 'frame', k.frame) for p in props for k in p.keys]
    if key is not None:
        keys = lane.prop.keys
        index = next(i for i, item in enumerate(keys) if item is key)
        lower = keys[index-1].frame if index else 0.0
        upper = keys[index+1].frame if index+1 < len(keys) else end_frame
        key.frame = min(upper, max(lower, key.frame+delta))
        start = end = key.frame
    elif mode == 'move':
        minimum = min([p.start_frame for p in props if p.start_frame >= 0]+[k.frame for p in props for k in p.keys], default=0)
        maximum = max([p.end_frame for p in props]+[k.frame for p in props for k in p.keys], default=0)
        delta = min(end_frame-maximum, max(-minimum, delta))
        for prop in props:
            if prop.start_frame >= 0:
                prop.start_frame += delta
            prop.end_frame += delta
            for item in prop.keys:
                item.frame += delta
        start, end = lane.prop.start_frame, lane.prop.end_frame
    elif mode == 'start':
        lane.prop.start_frame = min(lane.prop.end_frame, max(0, lane.prop.start_frame+delta))
        start, end = lane.prop.start_frame, lane.prop.end_frame
    else:
        lane.prop.end_frame = max(lane.prop.start_frame, min(end_frame, lane.prop.end_frame+delta))
        start, end = lane.prop.start_frame, lane.prop.end_frame
    for prop in (lane.prop, *lane.ancestors):
        if prop.start_frame < 0 or start < prop.start_frame:
            prop.start_frame = start
        prop.end_frame = max(prop.end_frame, end)
    return saved


class ClipTimelineView(QAbstractScrollArea):
    frame_requested = Signal(float)
    selection_changed = Signal(object, object)

    LABEL_WIDTH = 280
    RULER_HEIGHT = 28
    ROW_HEIGHT = 27
    COLORS = {'SOUND': '#48b8dc', 'VFX': '#e9a04b', 'GAME': '#a38deb',
              'PHYSICS': '#71b986', 'MOTION_SYNC': '#619fde'}

    def __init__(self, commit, parent=None):
        super().__init__(parent)
        self.commit = commit
        self.read_only = False
        self.lanes = []
        self.end_frame = 1.0
        self.playhead = 0.0
        self.zoom = 1.0
        self.selected = None
        self.drag: ClipEditDrag | None = None
        self.seeking = False
        self.ghost_delta = 0.0
        self.viewport().setMouseTracking(True)
        self.horizontalScrollBar().valueChanged.connect(self.viewport().update)
        self.verticalScrollBar().valueChanged.connect(self.viewport().update)

    @property
    def scale(self):
        return max(0.1, (self.viewport().width()-self.LABEL_WIDTH-20)/max(1, self.end_frame)*self.zoom)

    def set_lanes(self, lanes, end_frame):
        self.drag, self.seeking, self.ghost_delta = None, False, 0.0
        self.lanes = lanes
        self.end_frame = max(1.0, end_frame)
        self.selected = None
        self._scroll_ranges()
        self.viewport().update()

    def set_zoom(self, value):
        self.zoom = 2**(value/25)
        self._scroll_ranges()
        self.viewport().update()

    def set_playhead(self, value):
        self.playhead = value
        self.viewport().update()

    def _scroll_ranges(self):
        self.verticalScrollBar().setRange(0, max(0, len(self.lanes)*self.ROW_HEIGHT+self.RULER_HEIGHT-self.viewport().height()))
        self.verticalScrollBar().setPageStep(self.viewport().height())
        self.horizontalScrollBar().setRange(0, max(0, round(self.end_frame*self.scale+self.LABEL_WIDTH-self.viewport().width()+20)))
        self.horizontalScrollBar().setPageStep(max(1, self.viewport().width()-self.LABEL_WIDTH))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._scroll_ranges()

    def x_at(self, frame):
        return self.LABEL_WIDTH+frame*self.scale-self.horizontalScrollBar().value()

    def frame_at(self, x):
        return min(self.end_frame, max(0.0, (x-self.LABEL_WIDTH+self.horizontalScrollBar().value())/self.scale))

    def paintEvent(self, event):
        painter = QPainter(self.viewport())
        painter.fillRect(self.viewport().rect(), QColor('#232427'))
        width, height = self.viewport().width(), self.viewport().height()
        first = self.verticalScrollBar().value()//self.ROW_HEIGHT
        count = (height-self.RULER_HEIGHT)//self.ROW_HEIGHT+2
        for i in range(first, min(len(self.lanes), first+count)):
            lane = self.lanes[i]
            y = self.RULER_HEIGHT+i*self.ROW_HEIGHT-self.verticalScrollBar().value()
            selected = self.selected is not None and self.selected[0] is lane
            painter.fillRect(0, y, width, self.ROW_HEIGHT, QColor('#343b45' if selected else '#292b2f' if i%2 else '#232427'))
            category = lane.category.removeprefix('Override ')
            color = QColor(self.COLORS.get(category, '#bab064'))
            painter.fillRect(4, y+5, 3, self.ROW_HEIGHT-10, color)
            painter.setPen(QColor('#d5d9df'))
            text = f'{lane.category} · {lane.name}'
            text = painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, self.LABEL_WIDTH-16)
            painter.drawText(QRectF(12, y, self.LABEL_WIDTH-18, self.ROW_HEIGHT), Qt.AlignmentFlag.AlignVCenter, text)
            painter.save()
            painter.setClipRect(self.LABEL_WIDTH, self.RULER_HEIGHT, width-self.LABEL_WIDTH, height-self.RULER_HEIGHT)
            offset = self.ghost_delta if self.drag is not None and self.drag.lane is lane and self.drag.mode == 'move' else 0
            start, end = lane.prop.start_frame+offset, lane.prop.end_frame+offset
            if self.drag is not None and self.drag.lane is lane and self.drag.mode in ('start', 'end'):
                if self.drag.mode == 'start': start += self.ghost_delta
                else: end += self.ghost_delta
            if start >= 0 and end > start:
                bar = QRectF(self.x_at(start), y+8, max(2, (end-start)*self.scale), 11)
                fill = QColor(color);fill.setAlpha(95)
                painter.setBrush(fill);painter.setPen(QPen(color, 1))
                painter.drawRoundedRect(bar, 2, 2)
            for key in lane.prop.keys:
                frame = key.frame+offset
                if self.drag is not None and self.drag.key is key: frame += self.ghost_delta
                x = self.x_at(frame)
                if x < self.LABEL_WIDTH-8 or x > width+8: continue
                painter.setBrush(QColor('#ffffff') if self.selected is not None and self.selected[0] is lane and self.selected[1] is key else color)
                painter.setPen(QPen(QColor('#141517'), 1))
                painter.drawPolygon(QPolygonF([QPointF(x, y+6), QPointF(x+6, y+13), QPointF(x, y+20), QPointF(x-6, y+13)]))
            painter.restore()
        painter.fillRect(0, 0, width, self.RULER_HEIGHT, QColor('#303238'))
        painter.setPen(QColor('#bcc1ca'))
        step = 10**math.floor(math.log10(max(1, 65/self.scale)))
        for multiplier in (1, 2, 5, 10):
            if step*multiplier*self.scale >= 55:
                step *= multiplier;break
        first_frame = max(0, math.floor(self.frame_at(self.LABEL_WIDTH)/step)*step)
        for frame in range(int(first_frame), int(self.end_frame)+1, max(1, int(step))):
            x = self.x_at(frame)
            if x > width: break
            if x < self.LABEL_WIDTH: continue
            painter.drawLine(QPointF(x, 20), QPointF(x, 28))
            painter.drawText(QRectF(x+3, 0, 55, 22), Qt.AlignmentFlag.AlignVCenter, str(frame))
        painter.drawText(QRectF(12, 0, 200, 28), Qt.AlignmentFlag.AlignVCenter, 'CLIP')
        x = self.x_at(self.playhead)
        if self.LABEL_WIDTH <= x <= width:
            painter.setPen(QPen(QColor('#f46b70'), 1.5))
            painter.drawLine(QPointF(x, 0), QPointF(x, height))

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton: return
        self.drag, self.seeking, self.ghost_delta = None, False, 0.0
        x, y = event.position().x(), event.position().y()
        if y < self.RULER_HEIGHT:
            if x >= self.LABEL_WIDTH:
                self.seeking = True
                self.frame_requested.emit(self.frame_at(x))
            return
        row = int((y-self.RULER_HEIGHT+self.verticalScrollBar().value())//self.ROW_HEIGHT)
        if not 0 <= row < len(self.lanes): return
        lane = self.lanes[row]
        key = next((k for k in lane.prop.keys if abs(self.x_at(k.frame)-x) <= 7), None) if x >= self.LABEL_WIDTH else None
        self.selected = (lane, key)
        self.selection_changed.emit(lane, key)
        if x >= self.LABEL_WIDTH:
            mode = 'key' if key else 'start' if abs(x-self.x_at(lane.prop.start_frame)) <= 6 else 'end' if abs(x-self.x_at(lane.prop.end_frame)) <= 6 else 'move'
            if not self.read_only and (key is not None or self.x_at(lane.prop.start_frame)-6 <= x <= self.x_at(lane.prop.end_frame)+6):
                self.drag = ClipEditDrag(lane, key, mode, self.frame_at(x))
            else:
                self.seeking = True
                self.frame_requested.emit(self.frame_at(x))
        self.viewport().update()

    def mouseMoveEvent(self, event):
        if self.seeking:
            self.frame_requested.emit(self.frame_at(event.position().x()))
            return
        if self.drag is None:
            row = int((event.position().y()-self.RULER_HEIGHT+self.verticalScrollBar().value())//self.ROW_HEIGHT)
            if 0 <= row < len(self.lanes):
                lane = self.lanes[row]
                key = next((key for key in lane.prop.keys if abs(self.x_at(key.frame)-event.position().x()) <= 7), None)
                detail = f'\n{key.frame:g}: {key.value}' if key is not None else ''
                self.viewport().setToolTip(lane.category+' · '+lane.name+detail)
            return
        frame = self.frame_at(event.position().x())
        delta = frame-self.drag.anchor_frame
        self.ghost_delta = delta if event.modifiers() & Qt.KeyboardModifier.AltModifier else round(delta)
        self.viewport().update()

    def mouseReleaseEvent(self, event):
        drag, delta = self.drag, self.ghost_delta
        self.drag, self.ghost_delta, self.seeking = None, 0.0, False
        if drag is not None and delta:
            lane, key, mode = drag.lane, drag.key, drag.mode
            saved = move_timeline_item(lane, key, mode, delta, self.end_frame)
            try:
                self.commit()
            except (ValueError, OverflowError) as exc:
                for item, attr, value in saved: setattr(item, attr, value)
                QMessageBox.warning(self, 'CLIP edit', str(exc))
            self.selection_changed.emit(lane, key)
        self.viewport().update()


class ClipTimeline(QWidget):
    selection_changed = Signal(object, object)
    frame_requested = Signal(float)

    def __init__(self, commit, parent=None):
        super().__init__(parent)
        self.all_lanes = []
        self.motion = None
        root = QVBoxLayout(self);root.setContentsMargins(0, 0, 0, 0);root.setSpacing(3)
        toolbar = QHBoxLayout()
        self.search = QLineEdit();self.search.setPlaceholderText(self.tr('Search CLIP tracks…'))
        self.category = QComboBox();self.category.addItem(self.tr('All categories'), '')
        self.zoom = QSlider(Qt.Orientation.Horizontal);self.zoom.setRange(0, 100);self.zoom.setMaximumWidth(160)
        toolbar.addWidget(self.search, 1);toolbar.addWidget(self.category);toolbar.addWidget(QLabel(self.tr('Zoom')));toolbar.addWidget(self.zoom)
        root.addLayout(toolbar)
        self.view = ClipTimelineView(commit, self);root.addWidget(self.view, 1)
        self.view.selection_changed.connect(self.selection_changed.emit)
        self.view.frame_requested.connect(self.frame_requested.emit)
        self.search.textChanged.connect(self._filter)
        self.category.currentIndexChanged.connect(self._filter)
        self.zoom.valueChanged.connect(self.view.set_zoom)

    def set_motion(self, motion, overrides=()):
        same = self.motion is motion
        selected = self.view.selected if same else None
        self.motion = motion
        self.all_lanes = sequence_lanes(motion.sequences) if motion else []
        self.all_lanes += sequence_lanes(overrides, prefix='Override ')
        category = self.category.currentData() if same else ''
        self.category.blockSignals(True);self.category.clear();self.category.addItem(self.tr('All categories'), '')
        for value in dict.fromkeys(lane.category for lane in self.all_lanes): self.category.addItem(value, value)
        self.category.setCurrentIndex(max(0, self.category.findData(category)))
        self.category.blockSignals(False)
        self._filter()
        if selected is not None:
            lane = next((lane for lane in self.view.lanes if lane.prop is selected[0].prop), None)
            if lane is not None:
                self.view.selected = lane, selected[1]
                self.selection_changed.emit(lane, selected[1])

    def _filter(self):
        text, category = self.search.text().casefold(), self.category.currentData()
        lanes = [lane for lane in self.all_lanes if (not category or lane.category == category)
                 and text in (lane.name+' '+lane.category).casefold()]
        self.view.set_lanes(lanes, self.motion.end_frame if self.motion else 1)
