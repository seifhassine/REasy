from __future__ import annotations
from contextlib import contextmanager
from math import ceil
from typing import Any

from PySide6.QtCore import QPoint, QRectF, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QMenu,
    QPushButton,
    QScrollArea,
    QSlider,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .enums import PROPERTY_TYPES_WITH_CHILDREN, PropertyType, property_type_or_unknown
from .graph_operations import ClipGraphOperations
from .metadata import (
    AUX_KEY_TABLE_NAMES,
    COMPONENT_LABELS,
    CONTAINER_CHILD_TYPES,
    EXTRA_KEY_FLAG_NAMES,
    EXTRA_PROPERTY_MASK_NAMES,
    INTERPOLATION_BY_NAME,
    INTERPOLATION_DEFAULT_REFS,
    INTERPOLATION_NAMES,
    NODE_TYPE_BADGES,
    NODE_TYPE_COLORS,
    NODE_TYPE_NAMES,
    enum_text,
    flags_text,
)
from .structures import (
    ActionKey,
    BoolKey,
    ClipInfo,
    Key,
    NoHermiteKey,
    Node,
    Property,
    SpeedPoint,
    Track,
    UserDataAssetInfo,
)
from .value_adapters import apply_key_payload_text, key_payload_editable, key_payload_text


KEY_OBJECT_TYPES = (Key, BoolKey, ActionKey, NoHermiteKey, SpeedPoint)
KEY_REF_ROLES = (
    ("Last", "last_key_ref"),
    ("Last", "extra_key_last_ref"),
    ("Extra 1", "extra_key1_ref"),
    ("Extra 2", "extra_key2_ref"),
    ("Extra 3", "extra_key3_ref"),
)


def _node_type_value(node: Node) -> int:
    return int(getattr(node, "node_type", 0))


def _node_type_badge(node: Node) -> str:
    value = _node_type_value(node)
    return NODE_TYPE_BADGES.get(value, f"0x{value:X}")


def _node_type_name(node: Node) -> str:
    return enum_text(_node_type_value(node), NODE_TYPE_NAMES)


def _node_type_color(node: Node) -> QColor:
    return QColor(NODE_TYPE_COLORS.get(_node_type_value(node), NODE_TYPE_COLORS[0]))


def _clip_root_node_ids(parsed) -> set[int]:
    return {id(node) for clip_info in parsed.clip_infos for node in clip_info.root_nodes} if parsed else set()


def _visible_track_child_nodes(parsed, track: Track) -> list[Node]:
    clip_owned = _clip_root_node_ids(parsed)
    return [node for node in track.child_nodes if id(node) not in clip_owned]


def _identity_index(items, item) -> int:
    if items is None:
        return -1
    return next((index for index, current in enumerate(items) if current is item), -1)


def _prop_type(prop: Property) -> PropertyType:
    return property_type_or_unknown(prop.property_type)


def _prop_type_name(prop: Property) -> str:
    ptype = _prop_type(prop)
    return ptype.name if ptype is not PropertyType.UNKNOWN else f"0x{int(prop.property_type):X}"


def _is_property_container(prop: Property) -> bool:
    return _prop_type(prop) in PROPERTY_TYPES_WITH_CHILDREN


def _guid_text(raw: bytes) -> str:
    if not raw:
        return ""
    data = bytes(raw)
    if len(data) != 16:
        return data.hex()
    hexed = data.hex()
    return f"{hexed[:8]}-{hexed[8:12]}-{hexed[12:16]}-{hexed[16:20]}-{hexed[20:]}"


def _parse_guid_text(text: str) -> bytes:
    cleaned = text.strip().replace("-", "").replace("{", "").replace("}", "")
    if len(cleaned) != 32:
        raise ValueError("GUID must be 16 bytes / 32 hex digits")
    return bytes.fromhex(cleaned)


def _node_display_name(node: Node) -> str:
    return node.name or node.node_tag or _guid_text(node.root_node_guid) or "Node"


def _property_path_to(parsed, target: Property) -> tuple[Node, list[Property]] | None:
    def walk(prop: Property, path: list[Property]):
        if prop is target:
            return [*path, prop]
        for child in prop.child_properties:
            found = walk(child, [*path, prop])
            if found:
                return found
        return None

    for node in parsed.nodes:
        for prop in node.properties:
            found = walk(prop, [])
            if found:
                return node, found
    return None


class ClipTimelineCanvas(QWidget):
    selection_changed = Signal(dict)
    graph_changed = Signal()
    BASE_LABEL_W = 360
    MAX_LABEL_W = 560
    MAX_LABEL_FRACTION = 0.46
    MAX_LABEL_TEXT_HINT = 320
    MIN_VISIBLE_LABEL_TEXT = 112

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parsed = None
        self.rows: list[dict[str, Any]] = []
        self.items: list[dict[str, Any]] = []
        self.selected: dict[str, Any] | None = None
        self.hover_item: dict[str, Any] | None = None
        self.hover_row: dict[str, Any] | None = None
        self.expanded: set[int] = set()
        self.collapsed: set[int] = set()
        self.drag: dict[str, Any] | None = None
        self.filter_text = ""
        self.view_mode = "focused"
        self.zoom_factor = 1.0
        self.snap_to_frames = True
        self.label_w = self.BASE_LABEL_W
        self.row_h = 32
        self.header_h = 28
        self.margin = 10
        self.setMouseTracking(True)
        self.setMinimumHeight(300)

    def set_clip(self, parsed):
        self.parsed = parsed
        self.rebuild_rows()

    def set_filter_text(self, text: str):
        self.filter_text = text.strip().lower()
        self.rebuild_rows()

    def set_view_mode(self, mode: str):
        self.view_mode = mode
        self.rebuild_rows()

    def set_zoom(self, value: float):
        self.zoom_factor = max(0.25, float(value))
        self._sync_rows()

    def set_snap(self, enabled: bool):
        self.snap_to_frames = bool(enabled)

    def rebuild_rows(self):
        self.rows = []
        if not self.parsed:
            return
        for index, track in enumerate(self.parsed.tracks):
            self._add_track_rows(index, track)
        referenced = {
            id(node)
            for track in self.parsed.tracks
            for node in _visible_track_child_nodes(self.parsed, track)
        } | _clip_root_node_ids(self.parsed)
        loose_roots = [node for node in self.parsed.root_nodes if id(node) not in referenced]
        if loose_roots:
            self.rows.append(self._row("section", None, 0, "Root Nodes", meta=f"{len(loose_roots)} roots"))
            for node in loose_roots:
                self._add_node_rows(node, 1)
        self._filter_rows()
        self._sync_rows()

    def _add_track_rows(self, index: int, track: Track):
        label = track.group_name or track.type_unicode or track.type_ascii or f"Track {index}"
        self.rows.append(self._row(
            "track",
            track,
            0,
            label,
            badge="ON" if track.enable else "OFF",
            badge_color=QColor("#4f8f5b" if track.enable else "#7a4f4f"),
            meta=self._track_meta(track),
        ))
        if not self._is_collapsed(track):
            for clip_info in track.clip_infos:
                self._add_clip_info_row(clip_info, track, 1)
            for node in _visible_track_child_nodes(self.parsed, track):
                self._add_node_rows(node, 1, owner_track=track)

    def _add_clip_info_row(self, clip_info: ClipInfo, track: Track, depth: int):
        self.rows.append(self._row(
            "clip_info",
            clip_info,
            depth,
            clip_info.unicode_name or "Clip",
            owner_track=track,
            badge="Clip",
            badge_color=QColor("#4f83c2"),
            meta=f"{clip_info.frame_in:g}-{clip_info.frame_out:g}",
        ))
        if not self._is_collapsed(clip_info) and self._show_clip_nodes(clip_info):
            for node in clip_info.root_nodes:
                self._add_node_rows(node, depth + 1, owner_clip=clip_info)

    def _add_node_rows(
        self,
        node: Node,
        depth: int,
        owner_track: Track | None = None,
        owner_clip: ClipInfo | None = None,
        owner_node: Node | None = None,
    ):
        name = _node_display_name(node)
        meta = f"{node.begin_frame:g}-{node.end_frame:g}"
        counts = []
        if node.properties:
            counts.append(f"{len(node.properties)} props")
        if node.child_nodes:
            counts.append(f"{len(node.child_nodes)} children")
        if counts:
            meta = f"{meta} | {', '.join(counts)}"
        self.rows.append(self._row(
            "node",
            node,
            depth,
            name,
            badge=_node_type_name(node),
            badge_color=_node_type_color(node),
            owner_badge="Child Node" if owner_node else "Clip Root" if owner_clip else "Track Root" if owner_track else "Root Node",
            meta=meta,
            owner_track=owner_track,
            owner_clip=owner_clip,
            owner_node=owner_node,
        ))
        if not self._is_collapsed(node):
            if self._show_properties_for(node):
                for prop in node.properties:
                    self._add_property_rows(prop, depth + 1, node)
            for child in node.child_nodes:
                self._add_node_rows(child, depth + 1, owner_node=node)

    def _add_property_rows(self, prop: Property, depth: int, owner_node: Node | None = None, owner_prop: Property | None = None):
        self.rows.append(self._row(
            "property",
            prop,
            depth,
            self._property_label(prop),
            owner_node=owner_node,
            owner_prop=owner_prop,
            badge=_prop_type_name(prop),
            badge_color=QColor("#806dc0"),
            owner_badge="Child Prop" if owner_prop else "Node Prop" if owner_node else "",
            meta=self._property_meta(prop),
        ))
        if not self._is_collapsed(prop) and ClipGraphOperations.is_property_container(prop) and self._show_property_children(prop):
            for child in prop.child_properties:
                self._add_property_rows(child, depth + 1, owner_node, prop)

    def _row(self, kind: str, obj, depth: int, label: str, **extra):
        search = f"{kind} {label} {extra.get('badge', '')} {extra.get('meta', '')}"
        if isinstance(obj, Node):
            search += f" {_node_type_name(obj)} {_node_type_badge(obj)}"
        elif isinstance(obj, Property):
            search += f" {_prop_type_name(obj)}"
        return {"kind": kind, "obj": obj, "depth": depth, "label": label, "search": search.lower(), **extra}

    def _filter_rows(self):
        if not self.filter_text:
            return
        keep: set[int] = set()
        for index, row in enumerate(self.rows):
            if self.filter_text not in row.get("search", ""):
                continue
            keep.add(index)
            depth = row.get("depth", 0)
            for parent in range(index - 1, -1, -1):
                parent_depth = self.rows[parent].get("depth", 0)
                if parent_depth < depth:
                    keep.add(parent)
                    depth = parent_depth
                    if depth == 0:
                        break
        self.rows = [row for index, row in enumerate(self.rows) if index in keep]
        if not self.rows:
            self.rows = [self._row("section", None, 0, "No matches")]

    def _property_label(self, prop: Property):
        return prop.name or "Property"

    def _property_meta(self, prop: Property):
        count = len(prop.child_properties) if ClipGraphOperations.is_property_container(prop) else len(prop.keys)
        suffix = f"{count} child" if ClipGraphOperations.is_property_container(prop) else f"{count} key"
        if count != 1:
            suffix += "s"
        if prop.speed_points_ref:
            suffix += f", {len(prop.speed_points_ref)} speed"
        return suffix

    def _track_meta(self, track: Track):
        visible_roots = len(_visible_track_child_nodes(self.parsed, track))
        parts = [f"{len(track.clip_infos)} clips"]
        if visible_roots:
            parts.append(f"{visible_roots} roots")
        return ", ".join(parts)

    def _show_clip_nodes(self, clip_info: ClipInfo) -> bool:
        if self.view_mode == "details" or self.filter_text:
            return True
        if self.view_mode == "overview":
            return False
        return id(clip_info) in self.expanded or bool(self.selected and self.selected.get("obj") is clip_info)

    def _show_properties_for(self, node: Node) -> bool:
        if self._is_collapsed(node):
            return False
        if self.view_mode == "details" or self.filter_text:
            return True
        if self.view_mode == "overview":
            return False
        if id(node) in self.expanded:
            return True
        selected = self.selected.get("obj") if self.selected else None
        if selected is node:
            return True
        if isinstance(selected, Property):
            return any(self._property_contains(prop, selected) for prop in node.properties)
        if isinstance(selected, KEY_OBJECT_TYPES):
            owner = self.selected.get("owner_prop") if self.selected else None
            return any(self._property_contains(prop, owner) for prop in node.properties)
        return False

    def _show_property_children(self, prop: Property) -> bool:
        if self._is_collapsed(prop):
            return False
        if self.view_mode == "details" or self.filter_text:
            return True
        if id(prop) in self.expanded:
            return True
        selected = self.selected.get("obj") if self.selected else None
        return selected is prop or self._property_contains(prop, selected)

    def _property_contains(self, root: Property | None, target) -> bool:
        if root is None or target is None:
            return False
        if root is target:
            return True
        return any(self._property_contains(child, target) for child in root.child_properties)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), self.palette().base())
        if not self.parsed:
            return
        self.items = []
        self._sync_label_width()
        self._paint_header(painter)
        for row_index, row in enumerate(self.rows):
            self._paint_row(painter, row_index, row)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.rows:
            self._sync_label_width()
            self.setMinimumWidth(self.label_w + self._timeline_width() + self.margin)

    def _paint_header(self, painter: QPainter):
        y = 0
        painter.fillRect(0, y, self.width(), self.header_h, QColor("#2f343a"))
        painter.setPen(QColor("#f2f2f2"))
        painter.drawText(10, 0, self.label_w - 16, self.header_h, Qt.AlignVCenter, "Timeline")
        painter.setPen(QColor("#525b64"))
        painter.drawLine(self.label_w - 1, 0, self.label_w - 1, self.height())
        duration = self._duration()
        for tick in self._ticks(duration):
            x = self._frame_to_x(tick)
            painter.setPen(QColor("#7f8790"))
            painter.drawLine(x, self.header_h - 6, x, self.height())
            painter.setPen(QColor("#f2f2f2"))
            painter.drawText(x + 3, 0, 70, self.header_h, Qt.AlignVCenter, f"{tick:g}")

    def _paint_row(self, painter: QPainter, row_index: int, row: dict[str, Any]):
        y = self.header_h + row_index * self.row_h
        selected = self.selected and row.get("obj") is not None and row.get("obj") is self.selected.get("obj")
        hovered = self.hover_row is row
        bg = QColor("#303b48") if selected else QColor("#2d333a") if hovered else QColor("#25282c") if row_index % 2 else QColor("#202327")
        painter.fillRect(0, y, self.width(), self.row_h, bg)
        painter.setPen(QColor("#4b5158"))
        painter.drawLine(0, y + self.row_h - 1, self.width(), y + self.row_h - 1)
        indent = row.get("depth", 0) * 16
        if row["kind"] == "section":
            painter.setPen(QColor("#9ea7b1"))
            painter.drawText(10 + indent, y, self.label_w - 18 - indent, self.row_h, Qt.AlignVCenter, row["label"])
            return
        x = 10 + indent
        if self._row_has_children(row):
            rect = self._toggle_rect(row_index, row)
            painter.setPen(QColor("#b8c0c8"))
            if not self._row_is_expanded(row):
                points = [
                    QPoint(int(rect.left()) + 4, int(rect.top()) + 4),
                    QPoint(int(rect.left()) + 4, int(rect.bottom()) - 4),
                    QPoint(int(rect.right()) - 4, int(rect.center().y())),
                ]
            else:
                points = [
                    QPoint(int(rect.left()) + 3, int(rect.top()) + 5),
                    QPoint(int(rect.right()) - 3, int(rect.top()) + 5),
                    QPoint(int(rect.center().x()), int(rect.bottom()) - 4),
                ]
            painter.setBrush(QColor("#b8c0c8"))
            painter.drawPolygon(points)
            x += 16
        else:
            x += 16
        label_x = x
        text_w = max(20, self.label_w - label_x - 10)
        text = painter.fontMetrics().elidedText(row["label"], Qt.ElideRight, text_w)
        painter.setPen(QColor("#ffffff") if selected else QColor("#e1e5ea"))
        painter.drawText(label_x, y + 2, text_w, 16, Qt.AlignVCenter, text)
        subtitle = self._row_subtitle(row)
        if subtitle:
            font = painter.font()
            small_font = painter.font()
            point_size = small_font.pointSizeF()
            if point_size > 0:
                small_font.setPointSizeF(max(7.0, point_size - 1.5))
            painter.setFont(small_font)
            painter.setPen(QColor("#9aa3ad"))
            painter.drawText(
                label_x,
                y + 17,
                text_w,
                13,
                Qt.AlignVCenter,
                painter.fontMetrics().elidedText(subtitle, Qt.ElideRight, text_w),
            )
            painter.setFont(font)
        obj = row.get("obj")
        if isinstance(obj, ClipInfo):
            self._paint_range(
                painter,
                row_index,
                obj,
                obj.frame_in,
                obj.frame_out,
                QColor("#4f83c2"),
                "clip_info",
                owner_track=row.get("owner_track"),
                owner_list=self._owner_list_for_row(row),
            )
        elif isinstance(obj, Node):
            self._paint_range(
                painter,
                row_index,
                obj,
                obj.begin_frame,
                obj.end_frame,
                _node_type_color(obj),
                "node",
                owner_track=row.get("owner_track"),
                owner_clip=row.get("owner_clip"),
                owner_node=row.get("owner_node"),
                owner_list=self._owner_list_for_row(row),
            )
        elif isinstance(obj, Property):
            owner_prop = row.get("owner_prop")
            owner_list = owner_prop.child_properties if owner_prop else row.get("owner_node").properties if row.get("owner_node") else None
            self._paint_range(
                painter,
                row_index,
                obj,
                obj.begin_frame,
                obj.end_frame,
                QColor("#8e7bd4"),
                "property",
                owner_node=row.get("owner_node"),
                owner_prop=owner_prop,
                owner_list=owner_list,
            )
            for key in obj.keys:
                self._paint_key(painter, row_index, key, obj)
            for sp in obj.speed_points_ref:
                self._paint_key(painter, row_index, sp, obj, color=QColor("#e4c15d"), kind="speed_point")
    @staticmethod

    def _row_subtitle(row: dict[str, Any]) -> str:
        parts = [str(value) for value in (row.get("badge"), row.get("owner_badge"), row.get("meta")) if value]
        return " | ".join(parts)

    def _paint_range(self, painter: QPainter, row_index: int, obj, start: float, end: float, color: QColor, kind: str, **extra):
        if end < start:
            start, end = end, start
        y = self.header_h + row_index * self.row_h + 8
        x1 = self._frame_to_x(start)
        x2 = self._frame_to_x(end)
        rect = QRectF(x1, y, max(4, x2 - x1), 14)
        painter.setPen(QPen(QColor("#151719"), 1))
        painter.setBrush(color)
        painter.drawRoundedRect(rect, 3, 3)
        hovered = self.hover_item and self.hover_item.get("obj") is obj
        if hovered:
            painter.setPen(QPen(QColor("#d7dde5"), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect.adjusted(-2, -2, 2, 2), 4, 4)
        if self.selected and self.selected.get("obj") is obj:
            painter.setPen(QPen(QColor("#ffffff"), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), 4, 4)
        self.items.append({"kind": kind, "obj": obj, "rect": rect, "row": row_index, **extra})

    def _paint_key(self, painter: QPainter, row_index: int, obj, owner_prop: Property, color=QColor("#e08d4f"), kind="key"):
        frame = getattr(obj, "frame", 0.0)
        x = self._frame_to_x(frame)
        y = self.header_h + row_index * self.row_h + 15
        points = [QPoint(x, y - 7), QPoint(x + 6, y), QPoint(x, y + 7), QPoint(x - 6, y)]
        painter.setPen(QPen(QColor("#151719"), 1))
        painter.setBrush(color)
        painter.drawPolygon(points)
        hovered = self.hover_item and self.hover_item.get("obj") is obj
        if hovered:
            painter.setPen(QPen(QColor("#d7dde5"), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(x - 9, y - 9, 18, 18)
        if self.selected and self.selected.get("obj") is obj:
            painter.setPen(QPen(QColor("#ffffff"), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(x - 8, y - 8, 16, 16)
        if self.selected and (self.selected.get("obj") is owner_prop or self.selected.get("owner_prop") is owner_prop):
            text = painter.fontMetrics().elidedText(key_payload_text(owner_prop, obj), Qt.ElideRight, 120)
            painter.setPen(QColor("#dce3ea"))
            painter.drawText(x + 10, y - 12, 124, 16, Qt.AlignVCenter, text)
        owner_list = owner_prop.speed_points_ref if isinstance(obj, SpeedPoint) else owner_prop.keys if obj in owner_prop.keys else None
        self.items.append({
            "kind": kind,
            "obj": obj,
            "owner_prop": owner_prop,
            "owner_list": owner_list,
            "rect": QRectF(x - 8, y - 8, 16, 16),
            "row": row_index,
        })

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        row = self._row_at(event.position().y())
        row_index = self._row_index(row) if row else -1
        if row_index >= 0 and row and self._row_has_children(row) and self._toggle_rect(row_index, row).contains(event.position()):
            self.toggle_row(row)
            return
        item = self._item_at(event.position())
        if not item:
            if row:
                item = self._meta_from_row(row)
        if not item or item.get("obj") is None:
            return
        self.selected = item
        self.selection_changed.emit(item)
        self.update()
        if "rect" in item and item["kind"] in {"clip_info", "node", "property", "key", "speed_point"}:
            rect = item["rect"]
            mode = "move"
            if item["kind"] in {"clip_info", "node", "property"}:
                if abs(event.position().x() - rect.left()) <= 6:
                    mode = "start"
                elif abs(event.position().x() - rect.right()) <= 6:
                    mode = "end"
            self.drag = {
                "item": item,
                "mode": mode,
                "start_frame": self._x_to_frame(event.position().x()),
                "orig": self._range_values(item),
            }

    def _owner_list_for_row(self, row):
        if row["kind"] == "track":
            return self.parsed.tracks if self.parsed else None
        if row["kind"] == "clip_info":
            owner = row.get("owner_track")
            return owner.clip_infos if owner else None
        if row["kind"] == "node":
            if row.get("owner_node"):
                return row["owner_node"].child_nodes
            if row.get("owner_clip"):
                return row["owner_clip"].root_nodes
            if row.get("owner_track"):
                return row["owner_track"].child_nodes
        if row["kind"] == "property":
            owner_prop = row.get("owner_prop")
            return owner_prop.child_properties if owner_prop else row.get("owner_node").properties if row.get("owner_node") else None
        return None

    def mouseMoveEvent(self, event):
        if not self.drag:
            self._update_hover(event.position())
            return
        item = self.drag["item"]
        frame = self._x_to_frame(event.position().x())
        if self.snap_to_frames:
            frame = round(frame)
        start_frame = round(self.drag["start_frame"]) if self.snap_to_frames else self.drag["start_frame"]
        delta = frame - start_frame
        obj = item["obj"]
        mode = self.drag["mode"]
        if item["kind"] in {"key", "speed_point"}:
            obj.frame = max(0.0, self.drag["orig"][0] + delta)
            if self.snap_to_frames:
                obj.frame = round(obj.frame)
        else:
            start, end = self.drag["orig"]
            if mode == "start":
                start = min(end, max(0.0, start + delta))
            elif mode == "end":
                end = max(start, end + delta)
            else:
                width = end - start
                start = max(0.0, start + delta)
                end = start + width
            if self.snap_to_frames:
                start, end = round(start), round(end)
            self._set_range_values(item, start, end)
        self.graph_changed.emit()
        self.update()

    def mouseReleaseEvent(self, event):
        if self.drag:
            self.drag = None
            self.graph_changed.emit()

    def leaveEvent(self, event):
        self.hover_item = None
        self.hover_row = None
        self.setToolTip("")
        self.update()

    def _update_hover(self, pos):
        item = self._item_at(pos)
        row = self._row_at(pos.y())
        if item is self.hover_item and row is self.hover_row:
            return
        self.hover_item = item
        self.hover_row = row
        self.setToolTip(self._tooltip_for(item or row))
        self.update()

    def _tooltip_for(self, meta):
        if not meta or meta.get("obj") is None:
            return ""
        obj = meta["obj"]
        if isinstance(obj, Track):
            return f"Track: {obj.group_name or obj.type_unicode or obj.type_ascii or 'Track'}"
        if isinstance(obj, ClipInfo):
            return f"Clip: {obj.unicode_name or 'Clip'}\nFrames {obj.frame_in:g}-{obj.frame_out:g}"
        if isinstance(obj, Node):
            return f"{_node_type_name(obj)}: {_node_display_name(obj)}\nFrames {obj.begin_frame:g}-{obj.end_frame:g}"
        if isinstance(obj, Property):
            return f"{_prop_type_name(obj)}: {obj.name or 'Property'}\nFrames {obj.begin_frame:g}-{obj.end_frame:g}"
        if isinstance(obj, KEY_OBJECT_TYPES):
            owner = meta.get("owner_prop")
            value = key_payload_text(owner, obj) if isinstance(owner, Property) else ""
            return f"{meta.get('kind', 'Key')}: frame {getattr(obj, 'frame', 0.0):g}\n{value}"
        return str(obj)

    def _item_at(self, pos):
        for item in reversed(self.items):
            if item["rect"].contains(pos):
                return item
        return None

    def _row_at(self, y: float):
        index = int((y - self.header_h) // self.row_h)
        if 0 <= index < len(self.rows):
            return self.rows[index]
        return None

    def _toggle_rect(self, row_index: int, row: dict[str, Any]):
        x = 10 + row.get("depth", 0) * 16
        y = self.header_h + row_index * self.row_h
        return QRectF(x, y + 8, 12, self.row_h - 16)

    def _row_has_children(self, row: dict[str, Any]) -> bool:
        return bool(self._direct_child_objects(row))

    def _row_is_expanded(self, row: dict[str, Any]) -> bool:
        if self._is_collapsed(row.get("obj")):
            return False
        index = self._row_index(row)
        if index < 0 or self._subtree_end(index) == index + 1:
            return False
        visible = {
            id(child.get("obj"))
            for child in self.rows[index + 1:self._subtree_end(index)]
            if child.get("depth") == row.get("depth", 0) + 1
        }
        return all(id(obj) in visible for obj in self._direct_child_objects(row))

    def _direct_child_objects(self, row: dict[str, Any]):
        obj = row.get("obj")
        if isinstance(obj, Track):
            return [*obj.clip_infos, *_visible_track_child_nodes(self.parsed, obj)]
        if isinstance(obj, ClipInfo):
            return list(obj.root_nodes)
        if isinstance(obj, Node):
            return [*obj.properties, *obj.child_nodes]
        if isinstance(obj, Property):
            return list(obj.child_properties)
        return []

    def _is_collapsed(self, obj) -> bool:
        return obj is not None and id(obj) in self.collapsed

    def toggle_row(self, row: dict[str, Any]):
        obj = row.get("obj")
        if obj is None:
            return
        if self._row_is_expanded(row):
            self.collapsed.add(id(obj))
            self.expanded.discard(id(obj))
            self._remove_child_rows(row)
        else:
            self.collapsed.discard(id(obj))
            self.expanded.add(id(obj))
            self._insert_child_rows(row)
        self.update()

    def _insert_child_rows(self, row: dict[str, Any]):
        index = self._row_index(row)
        if index < 0:
            return
        end = self._subtree_end(index)
        if end > index + 1:
            del self.rows[index + 1:end]
        depth = row.get("depth", 0) + 1
        obj = row.get("obj")
        def add():
            if isinstance(obj, Track):
                for clip_info in obj.clip_infos:
                    self._add_clip_info_row(clip_info, obj, depth)
                for node in _visible_track_child_nodes(self.parsed, obj):
                    self._add_node_rows(node, depth, owner_track=obj)
            elif isinstance(obj, ClipInfo):
                for node in obj.root_nodes:
                    self._add_node_rows(node, depth, owner_clip=obj)
            elif isinstance(obj, Node):
                for prop in obj.properties:
                    self._add_property_rows(prop, depth, obj)
                for child in obj.child_nodes:
                    self._add_node_rows(child, depth, owner_node=obj)
            elif isinstance(obj, Property):
                owner_node = row.get("owner_node")
                for child in obj.child_properties:
                    self._add_property_rows(child, depth, owner_node, obj)
        rows = self._captured_rows(add)
        if rows:
            self.rows[index + 1:index + 1] = rows
            self._sync_rows()

    def _remove_child_rows(self, row: dict[str, Any]):
        index = self._row_index(row)
        if index < 0:
            return
        end = self._subtree_end(index)
        removed = self.rows[index + 1:end]
        if removed and self.selected and any(child.get("obj") is self.selected.get("obj") for child in removed):
            self.selected = self._meta_from_row(row)
            self.selection_changed.emit(self.selected)
        del self.rows[index + 1:end]
        self._sync_rows()

    def _meta_from_row(self, row: dict[str, Any]):
        owner_prop = row.get("owner_prop")
        return {
            "kind": row["kind"],
            "obj": row.get("obj"),
            "owner_track": row.get("owner_track"),
            "owner_clip": row.get("owner_clip"),
            "owner_node": row.get("owner_node"),
            "owner_prop": owner_prop,
            "owner_list": self._owner_list_for_row(row),
        }

    def _row_index(self, row: dict[str, Any] | None):
        return next((index for index, current in enumerate(self.rows) if current is row), -1)

    def row_index_for(self, obj) -> int:
        return next((index for index, row in enumerate(self.rows) if row.get("obj") is obj), -1)

    def ensure_focused_context(self, meta: dict | None = None):
        if self.view_mode != "focused" or self.filter_text or not self.parsed:
            self.update()
            return
        meta = meta or self.selected or {}
        obj = meta.get("obj")
        if isinstance(obj, ClipInfo):
            if not self._is_collapsed(obj):
                self.expanded.add(id(obj))
                self._ensure_clip_info_nodes(obj)
        elif isinstance(obj, Node):
            if not self._is_collapsed(obj):
                self.expanded.add(id(obj))
                self._ensure_node_properties(obj)
        elif isinstance(obj, Property):
            if not self._is_collapsed(obj):
                self.expanded.add(id(obj))
                self._ensure_property_context(obj, meta.get("owner_node"), meta.get("owner_prop"))
        elif isinstance(obj, KEY_OBJECT_TYPES):
            owner = meta.get("owner_prop")
            if isinstance(owner, Property) and not self._is_collapsed(owner):
                self.expanded.add(id(owner))
                self._ensure_property_context(owner, meta.get("owner_node"), meta.get("owner_prop"))
        self.update()

    def insert_track_row(self, track: Track) -> bool:
        if self.row_index_for(track) >= 0 or not self.parsed:
            return False
        index = _identity_index(self.parsed.tracks, track)
        insert_at = len(self.rows)
        if index > 0:
            previous = self.parsed.tracks[index - 1]
            previous_index = self.row_index_for(previous)
            if previous_index >= 0:
                insert_at = self._subtree_end(previous_index)
        rows = self._captured_rows(lambda: self._add_track_rows(index, track))
        if not rows:
            return False
        self.rows[insert_at:insert_at] = rows
        self._sync_rows()
        return True

    def insert_clip_info_row(self, clip_info: ClipInfo, track: Track | None) -> bool:
        if track is None or self.row_index_for(clip_info) >= 0:
            return False
        return self._insert_child_row(
            clip_info,
            track,
            track.clip_infos,
            lambda depth: self._add_clip_info_row(clip_info, track, depth),
        )

    def insert_property_row(self, prop: Property, owner_node: Node | None = None, owner_prop: Property | None = None) -> bool:
        parent = owner_prop or owner_node
        parent_index = self.row_index_for(parent)
        if owner_node is None and parent_index >= 0:
            owner_node = self.rows[parent_index].get("owner_node")
        siblings = owner_prop.child_properties if owner_prop else owner_node.properties if owner_node else []
        return self._insert_child_row(
            prop,
            parent,
            siblings,
            lambda depth: self._add_property_rows(prop, depth, owner_node, owner_prop),
        )

    def insert_node_row(
        self,
        node: Node,
        owner_track: Track | None = None,
        owner_clip: ClipInfo | None = None,
        owner_node: Node | None = None,
    ) -> bool:
        parent = owner_node or owner_clip or owner_track
        siblings = (
            owner_node.child_nodes if owner_node else
            owner_clip.root_nodes if owner_clip else
            owner_track.child_nodes if owner_track else []
        )
        return self._insert_child_row(
            node,
            parent,
            siblings,
            lambda depth: self._add_node_rows(
                node,
                depth,
                owner_track=owner_track,
                owner_clip=owner_clip,
                owner_node=owner_node,
            ),
        )

    def insert_root_node_row(self, node: Node) -> bool:
        if self.row_index_for(node) >= 0:
            return False
        section_index = next((i for i, row in enumerate(self.rows) if row["kind"] == "section" and row["label"] == "Root Nodes"), -1)
        if section_index < 0:
            section_index = len(self.rows)
            self.rows.append(self._row("section", None, 0, "Root Nodes", meta="1 roots"))
        elif self.parsed:
            self.rows[section_index]["meta"] = f"{len(self.parsed.root_nodes)} roots"
        rows = self._captured_rows(lambda: self._add_node_rows(node, self.rows[section_index].get("depth", 0) + 1))
        insert_at = self._subtree_end(section_index)
        self.rows[insert_at:insert_at] = rows
        self._sync_rows()
        return bool(rows)

    def _insert_child_row(self, obj, parent, siblings, add_row) -> bool:
        if self.row_index_for(obj) >= 0:
            return False
        parent_index = self.row_index_for(parent)
        if parent_index < 0:
            return False
        parent_row = self.rows[parent_index]
        if self._is_collapsed(parent):
            self.collapsed.discard(id(parent))
            self.expanded.add(id(parent))
            self._insert_child_rows(parent_row)
            return self.row_index_for(obj) >= 0
        insert_at = parent_index + 1
        index = _identity_index(siblings, obj)
        if index > 0:
            for sibling in reversed(siblings[:index]):
                sibling_index = self.row_index_for(sibling)
                if sibling_index >= 0:
                    insert_at = self._subtree_end(sibling_index)
                    break
        depth = self.rows[parent_index].get("depth", 0) + 1
        rows = self._captured_rows(lambda: add_row(depth))
        if not rows:
            return False
        self.rows[insert_at:insert_at] = rows
        self._sync_rows()
        return True

    def refresh_object_row(self, obj):
        index = self.row_index_for(obj)
        if index < 0:
            self.update()
            return
        row = self.rows[index]
        if isinstance(obj, Track):
            row.update(
                label=obj.group_name or obj.type_unicode or obj.type_ascii or "Track",
                badge="ON" if obj.enable else "OFF",
                badge_color=QColor("#4f8f5b" if obj.enable else "#7a4f4f"),
                meta=self._track_meta(obj),
            )
        elif isinstance(obj, ClipInfo):
            row.update(label=obj.unicode_name or "Clip", meta=f"{obj.frame_in:g}-{obj.frame_out:g}")
        elif isinstance(obj, Node):
            meta = f"{obj.begin_frame:g}-{obj.end_frame:g}"
            counts = []
            if obj.properties:
                counts.append(f"{len(obj.properties)} props")
            if obj.child_nodes:
                counts.append(f"{len(obj.child_nodes)} children")
            if counts:
                meta = f"{meta} | {', '.join(counts)}"
            row.update(label=_node_display_name(obj), badge=_node_type_name(obj), badge_color=_node_type_color(obj), meta=meta)
        elif isinstance(obj, Property):
            row.update(label=self._property_label(obj), badge=_prop_type_name(obj), meta=self._property_meta(obj))
        row["search"] = self._row(row["kind"], row.get("obj"), row.get("depth", 0), row["label"], **{
            key: value for key, value in row.items()
            if key not in {"kind", "obj", "depth", "label", "search"}
        })["search"]
        self.update()

    def remove_object_row(self, obj):
        index = self.row_index_for(obj)
        if index < 0:
            self.update()
            return
        depth = self.rows[index].get("depth", 0)
        end = index + 1
        while end < len(self.rows) and self.rows[end].get("depth", 0) > depth:
            end += 1
        del self.rows[index:end]
        self._sync_rows()

    def relocate_object_row(self, obj, owner_list):
        index = self.row_index_for(obj)
        if index < 0 or owner_list is None:
            self.update()
            return
        row = self.rows[index]
        block = self.rows[index:self._subtree_end(index)]
        del self.rows[index:index + len(block)]
        owner_index = _identity_index(owner_list, obj)
        if owner_index > 0:
            previous_index = self.row_index_for(owner_list[owner_index - 1])
            insert_at = self._subtree_end(previous_index) if previous_index >= 0 else index
        elif row["kind"] == "track":
            insert_at = 0
        else:
            parent = row.get("owner_prop") or row.get("owner_node") or row.get("owner_clip") or row.get("owner_track")
            parent_index = self.row_index_for(parent)
            insert_at = parent_index + 1 if parent_index >= 0 else index
        self.rows[insert_at:insert_at] = block
        self._sync_rows()

    def _ensure_clip_info_nodes(self, clip_info: ClipInfo):
        index = self.row_index_for(clip_info)
        subtree = self.rows[index + 1:self._subtree_end(index)] if index >= 0 else []
        if index < 0 or all(any(row.get("obj") is node for row in subtree) for node in clip_info.root_nodes):
            return
        depth = self.rows[index].get("depth", 0) + 1
        rows = self._captured_rows(lambda: [
            self._add_node_rows(node, depth, owner_clip=clip_info)
            for node in clip_info.root_nodes
        ])
        if rows:
            self.rows[index + 1:index + 1] = rows
            self._sync_rows()

    def _ensure_node_properties(self, node: Node):
        if self.row_index_for(node) < 0:
            return
        for prop in node.properties:
            self._ensure_property_context(prop, owner_node=node)

    def _ensure_property_context(self, prop: Property, owner_node: Node | None = None, owner_prop: Property | None = None):
        if self.row_index_for(prop) < 0:
            if owner_prop or owner_node:
                self.insert_property_row(prop, owner_node, owner_prop)
            else:
                path = _property_path_to(self.parsed, prop)
                if path:
                    owner_node, properties = path
                    parent = None
                    for item in properties:
                        self.insert_property_row(item, owner_node if parent is None else None, parent)
                        parent = item
        row_index = self.row_index_for(prop)
        if row_index < 0 or not ClipGraphOperations.is_property_container(prop) or not self._show_property_children(prop):
            return
        row = self.rows[row_index]
        for child in prop.child_properties:
            self._ensure_property_context(child, row.get("owner_node"), prop)

    def _captured_rows(self, add_rows):
        current_rows = self.rows
        self.rows = []
        try:
            add_rows()
            return self.rows
        finally:
            self.rows = current_rows

    def _subtree_end(self, index: int) -> int:
        depth = self.rows[index].get("depth", 0)
        end = index + 1
        while end < len(self.rows) and self.rows[end].get("depth", 0) > depth:
            end += 1
        return end

    def _sync_rows(self):
        self._sync_label_width()
        height = self.header_h + max(1, len(self.rows)) * self.row_h + self.margin
        width = self.label_w + self._timeline_width() + self.margin
        self.setMinimumWidth(width)
        self.setMinimumHeight(height)
        self.updateGeometry()
        self.update()

    def _sync_label_width(self):
        viewport = self.parentWidget().width() if self.parentWidget() else self.width()
        label_cap = self.BASE_LABEL_W
        if viewport > 0:
            label_cap = max(self.BASE_LABEL_W, min(self.MAX_LABEL_W, int(viewport * self.MAX_LABEL_FRACTION)))
        desired = self.BASE_LABEL_W
        metrics = self.fontMetrics()
        for row in self.rows:
            desired = max(desired, self._label_width_hint(metrics, row))
            if desired >= label_cap:
                break
        new_width = min(label_cap, desired)
        changed = new_width != self.label_w
        self.label_w = new_width
        return changed

    def _label_width_hint(self, metrics, row: dict[str, Any]) -> int:
        if row["kind"] == "section":
            return self.BASE_LABEL_W
        x = 10 + row.get("depth", 0) * 16 + 16
        label_text_w = metrics.horizontalAdvance(str(row.get("label", ""))) + 12
        label_text_w = min(self.MAX_LABEL_TEXT_HINT, max(self.MIN_VISIBLE_LABEL_TEXT, label_text_w))
        badge = row.get("badge")
        badge_w = min(92, max(34, metrics.horizontalAdvance(str(badge)) + 14)) + 8 if badge else 0
        return x + label_text_w + badge_w + 16

    def _range_values(self, item):
        obj = item["obj"]
        if isinstance(obj, ClipInfo):
            return obj.frame_in, obj.frame_out
        if isinstance(obj, (Node, Property)):
            return obj.begin_frame, obj.end_frame
        return (getattr(obj, "frame", 0.0),)

    def _set_range_values(self, item, start: float, end: float):
        obj = item["obj"]
        if isinstance(obj, ClipInfo):
            obj.frame_in, obj.frame_out = start, end
        elif isinstance(obj, (Node, Property)):
            obj.begin_frame, obj.end_frame = start, end

    def _duration(self):
        if not self.parsed:
            return 1.0
        values = [float(self.parsed.header.total_frame or 0.0)]
        values += [n.end_frame for n in self.parsed.nodes]
        values += [p.end_frame for p in self.parsed.properties]
        values += [ci.frame_out for ci in self.parsed.clip_infos]
        return max(1.0, max(values))

    def _frame_to_x(self, frame: float):
        width = self._timeline_width()
        return int(self.label_w + (float(frame) / self._duration()) * width)

    def _x_to_frame(self, x: float):
        width = self._timeline_width()
        ratio = max(0.0, min(1.0, (float(x) - self.label_w) / width))
        return ratio * self._duration()

    def _timeline_width(self):
        viewport = self.parentWidget().width() if self.parentWidget() else self.width()
        fit_width = max(1, viewport - self.label_w - self.margin)
        return max(1, ceil(fit_width * self.zoom_factor))

    def _ticks(self, duration: float):
        target = 8
        raw = max(1.0, duration / target)
        step = 1.0
        while step < raw:
            step *= 2.0 if step < 10 else 2.5
        ticks = []
        current = 0.0
        while current <= duration + 0.001:
            ticks.append(current)
            current += step
        return ticks
    @staticmethod
    def _short_guid(raw: bytes):
        return raw.hex()[:8] if raw else "node"

    _prop_type_name = staticmethod(_prop_type_name)


class ClipViewer(QWidget):
    modified_changed = Signal(bool)

    def __init__(self, handler, parent=None):
        super().__init__(parent)
        self.handler = handler
        self.parsed = handler.parsed
        self.ops = ClipGraphOperations(self.parsed)
        self.modified = False
        self.current: dict[str, Any] | None = None
        self._key_table_internal = False
        self._build_ui()
        self._refresh()

    def rebuild(self) -> bytes:
        data = self.handler.rebuild()
        self._reload_model()
        self.modified = False
        self._refresh()
        return data

    def _zoom_changed(self, value: int):
        zoom = value / 100.0
        self.zoom_label.setText(f"{zoom:.2f}x")
        self.timeline.set_zoom(zoom)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        bar = QHBoxLayout()
        self.status = QLabel()
        bar.addWidget(self.status)
        bar.addWidget(QLabel("Frames"))
        self.duration_spin = QDoubleSpinBox()
        self.duration_spin.setRange(0.0, 1_000_000.0)
        self.duration_spin.setDecimals(3)
        self.duration_spin.setSingleStep(1.0)
        bar.addWidget(self.duration_spin)
        self.timeline_filter = QLineEdit()
        self.timeline_filter.setPlaceholderText("Filter")
        self.timeline_filter.setMaximumWidth(220)
        bar.addWidget(self.timeline_filter)
        self.timeline_view = QComboBox()
        for label, mode in (("Focused", "focused"), ("Overview", "overview"), ("Details", "details")):
            self.timeline_view.addItem(label, mode)
        self.timeline_view.setMaximumWidth(120)
        bar.addWidget(self.timeline_view)
        bar.addWidget(QLabel("Zoom"))
        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setRange(25, 2000)
        self.zoom_slider.setSingleStep(5)
        self.zoom_slider.setPageStep(25)
        self.zoom_slider.setValue(100)
        self.zoom_slider.setMaximumWidth(180)
        bar.addWidget(self.zoom_slider)
        self.zoom_label = QLabel("1.00x")
        self.zoom_label.setMinimumWidth(44)
        bar.addWidget(self.zoom_label)
        self.snap_check = QCheckBox("Snap")
        self.snap_check.setChecked(True)
        bar.addWidget(self.snap_check)
        bar.addStretch(1)
        self.add_btn = QPushButton("Add")
        self.add_menu = QMenu(self.add_btn)
        self.add_btn.setMenu(self.add_menu)
        self.add_actions = {
            "track": self.add_menu.addAction("Track"),
            "clip": self.add_menu.addAction("Clip"),
            "node": self.add_menu.addAction("Node"),
            "property": self.add_menu.addAction("Property"),
            "child": self.add_menu.addAction("Child Property"),
            "key": self.add_menu.addAction("Key"),
            "speed": self.add_menu.addAction("Speed Point"),
        }
        self.dup_btn = QPushButton("Duplicate")
        self.del_btn = QPushButton("Delete")
        for button in (self.add_btn, self.dup_btn, self.del_btn):
            bar.addWidget(button)
        layout.addLayout(bar)
        splitter = QSplitter(Qt.Horizontal)
        self.timeline = ClipTimelineCanvas()
        self.timeline.selection_changed.connect(self._select)
        self.timeline.graph_changed.connect(self._timeline_graph_changed)
        self.timeline_filter.textChanged.connect(self.timeline.set_filter_text)
        self.timeline_view.currentIndexChanged.connect(
            lambda index: self.timeline.set_view_mode(self.timeline_view.itemData(index))
        )
        self.zoom_slider.valueChanged.connect(self._zoom_changed)
        self.snap_check.stateChanged.connect(lambda _state: self.timeline.set_snap(self.snap_check.isChecked()))
        self.timeline_scroll = QScrollArea()
        self.timeline_scroll.setWidgetResizable(True)
        self.timeline_scroll.setWidget(self.timeline)
        splitter.addWidget(self.timeline_scroll)
        inspector = QWidget()
        right = QVBoxLayout(inspector)
        right.setContentsMargins(4, 0, 0, 0)
        inspector_header = QHBoxLayout()
        inspector_header.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel("No selection")
        self.title.setStyleSheet("font-weight: bold;")
        inspector_header.addWidget(self.title, 1)
        self.advanced = QCheckBox("Advanced")
        inspector_header.addWidget(self.advanced)
        right.addLayout(inspector_header)
        self.path_label = QLabel()
        self.path_label.setStyleSheet("color: #aeb7c2;")
        self.path_label.setWordWrap(True)
        right.addWidget(self.path_label)
        self.validation_label = QLabel()
        self.validation_label.setStyleSheet("color: #ffcf66;")
        self.validation_label.setWordWrap(True)
        right.addWidget(self.validation_label)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.form_host = QWidget()
        self.form = QFormLayout(self.form_host)
        self.scroll.setWidget(self.form_host)
        right.addWidget(self.scroll, 1)
        self.related = QListWidget()
        self.related.itemClicked.connect(self._related_clicked)
        self.contents_label = QLabel("Contents")
        right.addWidget(self.contents_label)
        right.addWidget(self.related, 1)
        relation_bar = QHBoxLayout()
        self.move_up_btn = QPushButton("Move Up")
        self.move_down_btn = QPushButton("Move Down")
        for button in (self.move_up_btn, self.move_down_btn):
            relation_bar.addWidget(button)
        right.addLayout(relation_bar)
        self.key_header = QWidget()
        key_bar = QHBoxLayout(self.key_header)
        key_bar.setContentsMargins(0, 0, 0, 0)
        self.key_label = QLabel("Keys / Speed Points")
        key_bar.addWidget(self.key_label)
        key_bar.addStretch(1)
        right.addWidget(self.key_header)
        self.keys = QTableWidget(0, 3)
        self.keys.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.keys.setSelectionMode(QAbstractItemView.SingleSelection)
        self._configure_key_table()
        self.keys.itemChanged.connect(self._key_table_changed)
        self.keys.itemSelectionChanged.connect(self._key_table_selection_changed)
        right.addWidget(self.keys, 1)
        splitter.addWidget(inspector)
        splitter.setSizes([900, 360])
        layout.addWidget(splitter, 1)
        for action, slot in (
            (self.add_actions["track"], self._add_track),
            (self.add_actions["clip"], self._add_clip),
            (self.add_actions["node"], self._add_node),
            (self.add_actions["property"], self._add_property),
            (self.add_actions["child"], self._add_child_property),
            (self.add_actions["key"], self._add_key),
            (self.add_actions["speed"], self._add_speed_point),
        ):
            action.triggered.connect(slot)
        for button, slot in (
            (self.dup_btn, self._duplicate),
            (self.del_btn, self._delete),
            (self.move_up_btn, lambda: self._move_current(-1)),
            (self.move_down_btn, lambda: self._move_current(1)),
        ):
            button.clicked.connect(slot)
        self.duration_spin.valueChanged.connect(self._duration_changed)
        self.advanced.stateChanged.connect(lambda _: self._select(self.current or {}))

    def _reload_model(self):
        self.parsed = self.handler.parsed
        self.ops = ClipGraphOperations(self.parsed)

    def _refresh(self):
        self._reload_model()
        self.timeline.set_clip(self.parsed)
        self._update_status()
        self._update_duration_spin()
        self._select(self.current or {})

    def _update_status(self):
        h = self.parsed.header
        state = "Modified" if self.modified else "Ready"
        self.status.setText(
            f"{state} | v{h.version} | {h.total_frame:g} frames | "
            f"{len(self.parsed.tracks)} tracks, {len(self.parsed.nodes)} nodes, "
            f"{len(self.parsed.properties)} properties"
        )

    def _select(self, meta: dict):
        self.current = meta if meta and meta.get("obj") is not None else None
        self._clear_form()
        self.related.clear()
        self._reset_key_table()
        obj = self.current.get("obj") if self.current else None
        if self.timeline.selected != self.current:
            self.timeline.selected = self.current
        self.timeline.ensure_focused_context(self.current)
        self.title.setText(self._title(obj))
        self.path_label.setText(self._selection_path(obj))
        self.path_label.setVisible(obj is not None)
        self._show_validation(obj)
        self._show_object(obj)
        self._update_section_visibility(obj)
        self._update_buttons(obj)
        QTimer.singleShot(0, self._scroll_timeline_to_current)

    def _show_object(self, obj):
        if self.current and self.current.get("kind") == "oword":
            self._show_oword(self.current["index"])
        elif obj is None:
            self._show_clip_overview()
        elif isinstance(obj, Track):
            roots = _visible_track_child_nodes(self.parsed, obj)
            self._bool_check(obj, "enable", "Enabled")
            self._lines(obj, (("group_name", "Name"), ("type_unicode", "Type")))
            self._readonlys((("ClipInfo refs", len(obj.clip_infos)), ("Root nodes", len(roots))))
            self._related(obj.clip_infos, "clip_info", owner_track=obj, owner_list=obj.clip_infos)
            self._related(roots, "node", owner_track=obj, owner_list=obj.child_nodes)
        elif isinstance(obj, ClipInfo):
            self._lines(obj, (
                ("unicode_name", "Name"),
                ("frame_in", "Frame In"),
                ("frame_out", "Frame Out"),
                ("source_in", "Source In"),
                ("source_out", "Source Out"),
            ))
            self._related(obj.root_nodes, "node", owner_clip=obj, owner_list=obj.root_nodes)
        elif isinstance(obj, Node):
            self._lines(obj, (("name", "Name"), ("node_tag", "Tag")))
            self._guid_line(obj, "root_node_guid", "GUID")
            self._lines(obj, (("begin_frame", "Begin"), ("end_frame", "End")))
            self._enum_combo(obj, "node_type", "Node Type", NODE_TYPE_NAMES)
            if self.advanced.isChecked():
                self._lines(obj, (
                    ("unique_id", "Unique ID"),
                    ("unique32_id", "Unique32 ID"),
                    ("dev32_id", "Dev32 ID"),
                ))
                if self.parsed.header.version <= 43:
                    self._guid_line(obj, "ex_id", "Ex ID")
                if self.parsed.header.version >= 86:
                    self._flag_checks(obj, "extra_property_pass_mask", "Extra Property Mask", EXTRA_PROPERTY_MASK_NAMES)
            self._readonlys((("Properties", len(obj.properties)), ("Children", len(obj.child_nodes))))
            self._related(obj.properties, "property", owner_node=obj, owner_list=obj.properties)
            self._related(obj.child_nodes, "node", owner_node=obj, owner_list=obj.child_nodes)
        elif isinstance(obj, Property):
            self._lines(obj, (("name", "Name"),))
            self._property_type(obj)
            self._lines(obj, (("begin_frame", "Begin"), ("end_frame", "End")))
            if self.advanced.isChecked():
                self._lines(obj, (("array_index", "Array Index"),))
                self._bool_checks(obj, (
                    ("is_enum_closed", "Enum Closed"),
                    ("set_after_end_frame", "Set After End"),
                    ("is_set_delegate_enable", "Delegate Enable"),
                    ("is_prev_diff_frame_set", "Prev Diff Frame"),
                    ("is_next_diff_frame_set", "Next Diff Frame"),
                    ("is_prev_key_value_set", "Prev Key Value"),
                    ("is_delayed_execution_or_array_count_set", "Delayed / Array Count"),
                    ("has_set_property_delegate", "Set Delegate Callback"),
                ))
                self._readonlys((
                    ("Extra Keys", flags_text(obj.extra_key_flags, EXTRA_KEY_FLAG_NAMES)),
                    ("Key Table", enum_text(obj.aux_key_flags, AUX_KEY_TABLE_NAMES)),
                ))
            if _is_property_container(obj):
                self._readonly("Children", self._child_count_text(obj))
                self._related(obj.child_properties, "property", owner_prop=obj, owner_list=obj.child_properties)
            else:
                self._fill_keys(obj)
                if obj.speed_points_ref:
                    self._readonly("Speed Points", len(obj.speed_points_ref))
        elif isinstance(obj, UserDataAssetInfo):
            self._lines(obj, (("type_ascii", "Type"), ("path_unicode", "Path")))
        elif isinstance(obj, KEY_OBJECT_TYPES):
            self._line(obj, "frame", "Frame")
            owner_prop = self.current.get("owner_prop") if self.current else self._property_for_key(obj)
            self._key_payload_line(owner_prop, obj)
            if isinstance(obj, (Key, SpeedPoint)) and hasattr(obj, "rate"):
                self._line(obj, "rate", "Rate")
            self._interpolation_controls(obj)
            for attr, label in self._advanced_key_detail_fields(obj):
                self._line(obj, attr, label)
            if isinstance(owner_prop, Property):
                self._fill_keys(owner_prop, selected_key=obj)

    def _update_section_visibility(self, obj):
        has_contents = self.related.count() > 0
        has_key_context = (
            isinstance(obj, Property) and not _is_property_container(obj)
        ) or (
            isinstance(obj, KEY_OBJECT_TYPES)
            and isinstance(self.current.get("owner_prop") if self.current else None, Property)
        )
        self.contents_label.setVisible(has_contents)
        self.related.setVisible(has_contents)
        self.key_header.setVisible(has_key_context)
        self.key_label.setVisible(has_key_context)
        self.keys.setVisible(has_key_context)

    def _selection_path(self, obj) -> str:
        if obj is None:
            return ""
        if self.current and self.current.get("kind") == "oword":
            return "OWords > " + self._title(obj)
        path = self._object_path(obj)
        return " > ".join(path or [self._title(obj)])

    def _object_path(self, obj):
        if isinstance(obj, Track):
            return [self._title(obj)]
        if isinstance(obj, ClipInfo):
            track = self.current.get("owner_track") if self.current else self._track_for_clip(obj)
            return ([self._title(track)] if track else []) + [self._title(obj)]
        if isinstance(obj, Node):
            return self._node_path_to(obj)
        if isinstance(obj, Property):
            found = _property_path_to(self.parsed, obj)
            if not found:
                return [self._title(obj)]
            node, props = found
            return [*self._node_path_to(node), *[self._title(prop) for prop in props]]
        if isinstance(obj, KEY_OBJECT_TYPES):
            owner = self.current.get("owner_prop") if self.current else self._property_for_key(obj)
            return ([*self._object_path(owner)] if owner else []) + [self._title(obj)]
        if isinstance(obj, UserDataAssetInfo):
            return ["User Data Assets", self._title(obj)]
        return [self._title(obj)]

    def _node_path_to(self, target: Node):
        def walk(node: Node, prefix):
            path = [*prefix, self._title(node)]
            if node is target:
                return path
            for child in node.child_nodes:
                found = walk(child, path)
                if found:
                    return found
            return None
        for clip in self.parsed.clip_infos:
            for node in clip.root_nodes:
                found = walk(node, [self._title(clip)])
                if found:
                    return found
        clip_owned = _clip_root_node_ids(self.parsed)
        for track in self.parsed.tracks:
            for node in track.child_nodes:
                if id(node) in clip_owned:
                    continue
                found = walk(node, [self._title(track)])
                if found:
                    return found
        for node in self.parsed.root_nodes:
            found = walk(node, ["Root Nodes"])
            if found:
                return found
        return [self._title(target)]

    def _show_validation(self, obj):
        messages = self._validation_messages(obj)
        self.validation_label.setText(" | ".join(messages))
        self.validation_label.setVisible(bool(messages))

    def _validation_messages(self, obj):
        messages = []
        if isinstance(obj, ClipInfo) and obj.frame_out < obj.frame_in:
            messages.append("End frame is before begin frame")
        elif isinstance(obj, (Node, Property)) and getattr(obj, "end_frame", 0.0) < getattr(obj, "begin_frame", 0.0):
            messages.append("End frame is before begin frame")
        if isinstance(obj, Property):
            expected = self._container_expected_child_count(obj) if _is_property_container(obj) else None
            if expected is not None and len(obj.child_properties) != expected:
                messages.append(f"Expected {expected} children, found {len(obj.child_properties)}")
            if not _property_path_to(self.parsed, obj):
                messages.append("Property has no graph owner")
        if isinstance(obj, KEY_OBJECT_TYPES):
            owner = self.current.get("owner_prop") if self.current else self._property_for_key(obj)
            if not isinstance(owner, Property):
                messages.append("Key has no owning property")
            elif hasattr(obj, "frame") and not (owner.begin_frame <= obj.frame <= owner.end_frame):
                messages.append("Key frame is outside property range")
        return messages

    def _scroll_timeline_to_current(self):
        if not self.current:
            return
        row = self.timeline.row_index_for(self.current.get("obj"))
        if row < 0:
            return
        y = self.timeline.header_h + row * self.timeline.row_h
        scrollbar = self.timeline_scroll.verticalScrollBar()
        top = scrollbar.value()
        bottom = top + self.timeline_scroll.viewport().height()
        if y < top:
            scrollbar.setValue(max(0, y - self.timeline.row_h))
        elif y + self.timeline.row_h > bottom:
            scrollbar.setValue(min(scrollbar.maximum(), y - self.timeline_scroll.viewport().height() + self.timeline.row_h * 2))

    def _line(self, obj, attr: str, label: str):
        edit = QLineEdit(str(getattr(obj, attr, "")))
        edit.setProperty("clip_obj_id", id(obj))
        edit.setProperty("clip_attr", attr)
        def commit():
            old = getattr(obj, attr)
            try:
                value = float(edit.text()) if isinstance(old, float) else int(edit.text(), 0) if isinstance(old, int) and not isinstance(old, bool) else edit.text()
                setattr(obj, attr, value)
                edit.setStyleSheet("")
                self._object_changed(obj)
            except Exception:
                edit.setStyleSheet("border: 1px solid #cc3333;")
        edit.editingFinished.connect(commit)
        self.form.addRow(label, edit)

    def _guid_line(self, obj, attr: str, label: str):
        edit = QLineEdit(_guid_text(getattr(obj, attr, b"")))
        edit.setProperty("clip_obj_id", id(obj))
        edit.setProperty("clip_attr", attr)
        def commit():
            try:
                setattr(obj, attr, _parse_guid_text(edit.text()))
                edit.setText(_guid_text(getattr(obj, attr)))
                edit.setStyleSheet("")
                self._object_changed(obj)
            except Exception:
                edit.setStyleSheet("border: 1px solid #cc3333;")
        edit.editingFinished.connect(commit)
        self.form.addRow(label, edit)

    def _lines(self, obj, fields):
        for attr, label in fields:
            self._line(obj, attr, label)

    def _readonly(self, label: str, value):
        edit = QLineEdit(str(value))
        edit.setReadOnly(True)
        self.form.addRow(label, edit)

    def _readonlys(self, fields):
        for label, value in fields:
            self._readonly(label, value)

    def _bool_check(self, obj, attr: str, label: str):
        check = QCheckBox()
        check.setChecked(bool(getattr(obj, attr, 0)))
        def changed(_state):
            setattr(obj, attr, 1 if check.isChecked() else 0)
            self._object_changed(obj)
        check.stateChanged.connect(changed)
        self.form.addRow(label, check)

    def _bool_checks(self, obj, fields):
        for attr, label in fields:
            self._bool_check(obj, attr, label)

    def _combo(self, label: str, items, current, changed, unknown_fmt="0x{:X}"):
        combo = QComboBox()
        for text, value in items:
            combo.addItem(text, value)
        idx = combo.findData(current)
        if idx < 0 and current is not None:
            text = unknown_fmt.format(current) if isinstance(current, int) else str(current)
            combo.addItem(text, current)
            idx = combo.count() - 1
        combo.setCurrentIndex(max(0, idx))
        combo.currentIndexChanged.connect(lambda index: changed(combo.itemData(index)))
        self.form.addRow(label, combo)
        return combo

    def _flag_checks(self, obj, attr: str, label: str, names: dict[int, str]):
        host = QWidget()
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        value = int(getattr(obj, attr, 0))
        checks = []
        for bit, name in names.items():
            check = QCheckBox(name)
            check.setChecked(bool(value & bit))
            row.addWidget(check)
            checks.append((bit, check))
        row.addStretch(1)
        def changed(_state):
            new_value = 0
            for bit, check in checks:
                if check.isChecked():
                    new_value |= bit
            setattr(obj, attr, new_value)
            self._object_changed(obj)
        for _bit, check in checks:
            check.stateChanged.connect(changed)
        self.form.addRow(label, host)

    def _show_clip_overview(self):
        h = self.parsed.header
        self._readonlys((
            ("Version", h.version),
            ("Frames", f"{h.total_frame:g}"),
            ("Tracks", len(self.parsed.tracks)),
            ("Clip Infos", len(self.parsed.clip_infos)),
            ("Nodes", len(self.parsed.nodes)),
            ("Properties", len(self.parsed.properties)),
            ("User Data Assets", len(self.parsed.user_data_assets)),
            ("OWords", len(self.parsed.owords)),
        ))
        self._related_section("Tracks")
        self._related(self.parsed.tracks, "track", owner_list=self.parsed.tracks)
        if self.parsed.clip_infos:
            self._related_section("Clip Infos")
            self._related(self.parsed.clip_infos, "clip_info", owner_list=self.parsed.clip_infos)
        if self.parsed.user_data_assets:
            self._related_section("User Data Assets")
            self._related(self.parsed.user_data_assets, "user_data_asset", owner_list=self.parsed.user_data_assets)
        if self.parsed.owords:
            self._related_section("OWords")
            self._related_owords()

    def _show_oword(self, index: int):
        values = self.parsed.owords[index]
        edit = QLineEdit(", ".join(f"{value:g}" for value in values))
        def commit():
            try:
                new_values = tuple(float(part.strip()) for part in edit.text().split(","))
                if len(new_values) != 4:
                    raise ValueError("Expected four values")
                old_values = self.parsed.owords[index]
                self.parsed.owords[index] = new_values
                for prop in self.parsed.properties:
                    if _prop_type(prop) != PropertyType.PATH_POINT3D:
                        continue
                    for key in ClipGraphOperations.iter_property_payload_keys(prop):
                        if getattr(key, "oword_ref", None) is old_values or (
                            getattr(key, "raw0", -1) == index and getattr(key, "oword_ref", None) == old_values
                        ):
                            key.oword_ref = new_values
                edit.setStyleSheet("")
                self._mark_modified()
                self.timeline.update()
            except Exception:
                edit.setStyleSheet("border: 1px solid #cc3333;")
        edit.editingFinished.connect(commit)
        self.form.addRow("Path Point", edit)

    def _property_type(self, prop: Property):
        def changed(value):
            was_container = _is_property_container(prop)
            prop.property_type = int(value)
            self._normalize_property_after_type_change(prop, was_container)
            if self.current and self.current.get("obj") is prop:
                self._mark_modified()
                self.timeline.refresh_object_row(prop)
                self._select(self.current)
            else:
                self._update_buttons(prop)
                self._object_changed(prop)
        self._combo("Type", ((ptype.name, int(ptype)) for ptype in PropertyType), int(prop.property_type), changed)

    def _normalize_property_after_type_change(self, prop: Property, was_container: bool):
        if _is_property_container(prop):
            prop.keys = []
            prop.speed_points_ref = []
            prop.last_key_ref = None
            prop.extra_key_last_ref = None
            prop.extra_key1_ref = None
            prop.extra_key2_ref = None
            prop.extra_key3_ref = None
            prop.extra_key_flags = 0
            prop.aux_key_flags = 0
            ptype = _prop_type(prop)
            child_types = CONTAINER_CHILD_TYPES.get(ptype)
            if child_types and ptype not in {PropertyType.NATIVE_ARRAY, PropertyType.NATIVE_CLASS}:
                prop.child_properties = []
                self._populate_fixed_child_properties(prop)
            elif not was_container:
                prop.child_properties = []
            prop.children = []
            return

        if was_container:
            prop.child_properties = []
            prop.children = []
            prop.keys = [self._default_key_for_property(prop)]
            prop.speed_points_ref = []
            prop.last_key_ref = None
            prop.extra_key_last_ref = None
            prop.extra_key1_ref = None
            prop.extra_key2_ref = None
            prop.extra_key3_ref = None
            prop.extra_key_flags = 0
            prop.aux_key_flags = 0

    def _enum_combo(self, obj, attr: str, label: str, names: dict[int, str]):
        def changed(value):
            setattr(obj, attr, int(value))
            self._object_changed(obj)
        self._combo(label, ((name, value) for value, name in names.items()), int(getattr(obj, attr)), changed)

    def _related(self, objects: list, kind: str, **extra):
        for obj in objects:
            item = QListWidgetItem(self._title(obj))
            item.setData(Qt.UserRole, {"kind": kind, "obj": obj, **extra})
            self.related.addItem(item)

    def _related_section(self, label: str):
        item = QListWidgetItem(label)
        item.setFlags(item.flags() & ~Qt.ItemIsSelectable & ~Qt.ItemIsEnabled)
        self.related.addItem(item)

    def _related_owords(self):
        for index, values in enumerate(self.parsed.owords):
            text = ", ".join(f"{value:g}" for value in values)
            item = QListWidgetItem(f"{index}: {text}")
            item.setData(Qt.UserRole, {"kind": "oword", "obj": values, "index": index})
            self.related.addItem(item)

    def _related_clicked(self, item: QListWidgetItem):
        if item.data(Qt.UserRole) is None:
            return
        self.timeline.selected = item.data(Qt.UserRole)
        self._select(item.data(Qt.UserRole))

    def _configure_key_table(self):
        headers = ["Frame", "Role", "Interpolation", "Value"]
        if self.advanced.isChecked():
            headers.append("Payload Words")
        self.keys.setColumnCount(len(headers))
        self.keys.setHorizontalHeaderLabels(headers)
    @contextmanager

    def _blocked_key_table(self):
        self._key_table_internal = True
        blocker = QSignalBlocker(self.keys)
        try:
            yield
        finally:
            del blocker
            self._key_table_internal = False

    def _reset_key_table(self):
        with self._blocked_key_table():
            self._configure_key_table()
            self.keys.setRowCount(0)
    @staticmethod

    def _key_item(text: str, editable: bool = True):
        item = QTableWidgetItem(text)
        if not editable:
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        return item

    def _fill_keys(self, prop: Property, selected_key=None):
        rows = self._key_rows(prop)
        with self._blocked_key_table():
            self._configure_key_table()
            self.keys.clearSelection()
            self.keys.setRowCount(len(rows))
            selected_row = -1
            for row, (role, key) in enumerate(rows):
                self._set_key_table_row(row, prop, role, key)
                if key is selected_key:
                    selected_row = row
            if selected_row >= 0:
                self.keys.selectRow(selected_row)

    def _remove_key_table_row(self, key):
        with self._blocked_key_table():
            for row in range(self.keys.rowCount()):
                item = self.keys.item(row, 0)
                if item and item.data(Qt.UserRole) and item.data(Qt.UserRole).get("key") is key:
                    self.keys.removeRow(row)
                    return

    def _set_key_table_row(self, row: int, prop: Property, role: str, key):
        self.keys.setItem(row, 0, self._key_item(str(getattr(key, "frame", ""))))
        self.keys.setItem(row, 1, self._key_item(f"{role} ({self._key_kind_text(key)})", editable=False))
        self.keys.setItem(row, 2, self._key_item(self._interpolation_text(key)))
        self.keys.setItem(row, 3, self._key_item(key_payload_text(prop, key), editable=key_payload_editable(prop, key)))
        if self.advanced.isChecked():
            self.keys.setItem(row, 4, self._key_item(self._key_raw_text(key), editable=hasattr(key, "raw0")))
        self.keys.item(row, 0).setData(Qt.UserRole, {"key": key, "prop": prop, "role": role})

    def _key_table_selection_changed(self):
        if self._key_table_internal:
            return
        meta = self._key_meta_for_table_row(self.keys.currentRow())
        if not meta or (self.current and self.current.get("obj") is meta.get("obj")):
            return
        self._select(meta)

    def _key_meta_for_table_row(self, row: int):
        if row < 0:
            return None
        item = self.keys.item(row, 0)
        data = item.data(Qt.UserRole) if item else None
        if not data:
            return None
        key = data["key"]
        prop = data["prop"]
        kind = "speed_point" if isinstance(key, SpeedPoint) else "key"
        owner_list = prop.speed_points_ref if isinstance(key, SpeedPoint) else prop.keys if key in prop.keys else None
        return {"kind": kind, "obj": key, "owner_prop": prop, "owner_list": owner_list}

    def _key_table_changed(self, item: QTableWidgetItem):
        if self._key_table_internal:
            return
        meta = self.keys.item(item.row(), 0).data(Qt.UserRole)
        key = meta["key"]
        prop = meta["prop"]
        changed = False
        try:
            if item.column() == 0 and hasattr(key, "frame"):
                key.frame = float(item.text())
                changed = True
            elif item.column() == 2:
                self._set_interpolation(key, self._parse_interpolation(item.text()))
                self.keys.blockSignals(True)
                item.setText(self._interpolation_text(key))
                self.keys.blockSignals(False)
                changed = True
            elif item.column() == 3:
                changed = apply_key_payload_text(prop, key, item.text())
            elif item.column() == 4 and self.advanced.isChecked() and hasattr(key, "raw0"):
                raw0, raw1 = [int(part.strip(), 0) for part in item.text().split(",", 1)]
                key.raw0, key.raw1 = raw0, raw1
                changed = True
            if changed:
                item.setBackground(QBrush())
                self._mark_modified()
                self.timeline.refresh_object_row(prop)
        except Exception:
            item.setBackground(QColor("#663333"))

    def _key_rows(self, prop: Property):
        rows: list[tuple[str, object]] = []
        seen: set[int] = set()
        def add(role: str, key):
            if key is None or id(key) in seen:
                return
            seen.add(id(key))
            rows.append((role, key))
        for role, keys in (
            ("Value", prop.keys),
            *[(role, (getattr(prop, attr),)) for role, attr in KEY_REF_ROLES],
            ("Speed", prop.speed_points_ref),
        ):
            for key in keys:
                add(role, key)
        return rows

    def _key_payload_line(self, prop: Property | None, key):
        if isinstance(key, BoolKey):
            self._bool_check(key, "bool_value", "Value")
            return
        asset = getattr(key, "user_data_asset_ref", None)
        if asset is not None and self.parsed.user_data_assets:
            def changed(value):
                key.user_data_asset_ref = value
                key.user_data_asset_index = -1
                self._object_changed(key)
            self._combo(
                "Asset",
                ((row_asset.path_unicode or row_asset.type_ascii or "UserDataAsset", row_asset) for row_asset in self.parsed.user_data_assets),
                asset,
                changed,
            )
            return
        text = key_payload_text(prop, key)
        edit = QLineEdit(text)
        edit.setReadOnly(not key_payload_editable(prop, key))
        def commit():
            if edit.isReadOnly():
                return
            try:
                if apply_key_payload_text(prop, key, edit.text()):
                    edit.setStyleSheet("")
                    self._object_changed(key)
            except Exception:
                edit.setStyleSheet("border: 1px solid #cc3333;")
        edit.editingFinished.connect(commit)
        self.form.addRow("Value", edit)

    def _interpolation_controls(self, key):
        attr = self._interpolation_attr(key)
        if attr:
            self._interpolation_combo(key, attr, "Interpolation")
        if hasattr(key, "offset_frame_flag"):
            self._bool_check(key, "offset_frame_flag", "Offset Frame")
        if hasattr(key, "frame_span"):
            self._line(key, "frame_span", "Frame Span")
        if hasattr(key, "range_v2_frame_span"):
            self._line(key, "range_v2_frame_span", "RangeV2 Span")
        if attr == "interpolation_type" and (
            getattr(key, "interpolation_ref", None) is not None
            or getattr(key, attr, None) in INTERPOLATION_DEFAULT_REFS
        ):
            self._interpolation_ref_line(key)

    def _interpolation_combo(self, key, attr: str, label: str):
        def changed(value):
            self._set_interpolation(key, int(value))
            self._object_changed(key)
        self._combo(label, ((name, value) for value, name in INTERPOLATION_NAMES.items()), int(getattr(key, attr)), changed)

    def _interpolation_ref_line(self, key):
        value = getattr(key, "interpolation_ref", None)
        if value is None:
            value = INTERPOLATION_DEFAULT_REFS.get(getattr(key, "interpolation_type", 0))
            key.interpolation_ref = value
        edit = QLineEdit(", ".join(f"{part:g}" for part in value))
        def commit():
            try:
                values = tuple(float(part.strip()) for part in edit.text().split(","))
                expected = len(key.interpolation_ref or ())
                if len(values) != expected:
                    raise ValueError("Wrong tuple length")
                key.interpolation_ref = values
                edit.setStyleSheet("")
                self._object_changed(key)
            except Exception:
                edit.setStyleSheet("border: 1px solid #cc3333;")
        edit.editingFinished.connect(commit)
        self.form.addRow("Curve Controls", edit)

    def _advanced_key_detail_fields(self, key):
        if not self.advanced.isChecked():
            return ()
        return tuple(
            (attr, label)
            for attr, label in (
                ("raw0", "Payload Word 0"),
                ("raw1", "Payload Word 1"),
                ("reserved", "Reserved"),
                ("reserved2", "Reserved 2"),
            )
            if hasattr(key, attr)
        )
    @staticmethod

    def _interpolation_attr(key):
        if hasattr(key, "interpolation_type"):
            return "interpolation_type"
        if hasattr(key, "interpolation_type_to_next"):
            return "interpolation_type_to_next"
        return None

    def _interpolation_text(self, key) -> str:
        attr = self._interpolation_attr(key)
        if not attr:
            return ""
        value = int(getattr(key, attr))
        return INTERPOLATION_NAMES.get(value, f"0x{value:X}")
    @staticmethod

    def _parse_interpolation(text: str) -> int:
        cleaned = text.strip()
        if "(" in cleaned:
            cleaned = cleaned.split("(", 1)[0].strip()
        lowered = cleaned.lower().removeprefix("interpolationtype_")
        if lowered in INTERPOLATION_BY_NAME:
            return INTERPOLATION_BY_NAME[lowered]
        return int(cleaned, 0)

    def _set_interpolation(self, key, value: int):
        attr = self._interpolation_attr(key)
        if not attr:
            return
        setattr(key, attr, value)
        if attr != "interpolation_type":
            return
        if value in INTERPOLATION_DEFAULT_REFS:
            current = getattr(key, "interpolation_ref", None)
            expected = len(INTERPOLATION_DEFAULT_REFS[value])
            if current is None or len(current) != expected:
                key.interpolation_ref = INTERPOLATION_DEFAULT_REFS[value]
        else:
            key.interpolation_ref = None
    @staticmethod

    def _key_kind_text(key):
        for cls, label in ((BoolKey, "Bool"), (ActionKey, "Action"), (NoHermiteKey, "No Hermite"), (SpeedPoint, "Speed Point")):
            if isinstance(key, cls):
                return label
        return "Key"

    def _add_track(self):
        def mutate():
            track = self.ops.add_track(ClipGraphOperations.create_track())
            if self.parsed.header.version >= 40:
                clip_info = self.ops.add_clip_info(ClipGraphOperations.create_clip_info(self.parsed.header.total_frame))
                self.ops.add_track_clip(track, clip_info)
            self.timeline.insert_track_row(track)
            return self._meta("track", track, owner_list=self.parsed.tracks)
        self._structural(mutate)

    def _add_clip(self):
        obj = self.current.get("obj") if self.current else None
        def mutate():
            clip_info = self.ops.add_clip_info(ClipGraphOperations.create_clip_info(self.parsed.header.total_frame))
            track = obj if isinstance(obj, Track) else self._track_for_clip(obj) if isinstance(obj, ClipInfo) else None
            if track is None and self.parsed.tracks:
                track = self.parsed.tracks[0]
            if track is not None:
                self.ops.add_track_clip(track, clip_info)
                self.timeline.insert_clip_info_row(clip_info, track)
            return self._meta("clip_info", clip_info, owner_track=track, owner_list=track.clip_infos if track else None)
        self._structural(mutate)

    def _add_node(self):
        obj = self.current.get("obj") if self.current else None
        def mutate():
            node = ClipGraphOperations.create_node(self.parsed.header.total_frame)
            if isinstance(obj, Node):
                self.ops.add_node_child(obj, node)
                self.timeline.insert_node_row(node, owner_node=obj)
                return self._meta("node", node, owner_node=obj, owner_list=obj.child_nodes)
            elif isinstance(obj, ClipInfo):
                meta = self._attach_timeline_node(node, clip_info=obj)
            else:
                meta = self._attach_timeline_node(node, track=obj if isinstance(obj, Track) else None)
            return meta
        self._structural(mutate)

    def _add_property(self):
        obj = self.current.get("obj") if self.current else None
        if not isinstance(obj, Node):
            return
        def mutate():
            prop = ClipGraphOperations.create_property(self.parsed.header.total_frame)
            self.ops.add_node_property(obj, prop)
            self.timeline.insert_property_row(prop, owner_node=obj)
            self.timeline.refresh_object_row(obj)
            return self._meta("property", prop, owner_node=obj, owner_list=obj.properties)
        self._structural(mutate)

    def _add_child_property(self):
        obj = self.current.get("obj") if self.current else None
        if not isinstance(obj, Property) or not _is_property_container(obj) or not self._can_add_child(obj):
            return
        def mutate():
            index = len(obj.child_properties)
            child = self._create_child_property(obj, index)
            self.ops.add_property_child(obj, child)
            self.timeline.insert_property_row(child, owner_prop=obj)
            self.timeline.refresh_object_row(obj)
            return self._meta("property", child, owner_node=self.current.get("owner_node") if self.current else None, owner_prop=obj, owner_list=obj.child_properties)
        self._structural(mutate)

    def _add_key(self):
        obj = self.current.get("obj") if self.current else None
        if not isinstance(obj, Property) or _is_property_container(obj):
            return
        def mutate():
            key = self._default_key_for_property(obj)
            self.ops.add_property_key(obj, key)
            self.timeline.refresh_object_row(obj)
            return self._meta("key", key, owner_prop=obj, owner_list=obj.keys)
        self._structural(mutate)

    def _add_speed_point(self):
        obj = self.current.get("obj") if self.current else None
        if not isinstance(obj, Property):
            return
        def mutate():
            point = ClipGraphOperations.create_speed_point()
            obj.speed_points_ref.append(point)
            self.timeline.refresh_object_row(obj)
            return self._meta("speed_point", point, owner_prop=obj, owner_list=obj.speed_points_ref)
        self._structural(mutate)

    def _duplicate(self):
        obj = self.current.get("obj") if self.current else None
        def mutate():
            if isinstance(obj, Track):
                duplicate = ClipGraphOperations.duplicate_track(obj)
                self.ops.add_track(duplicate)
                self.timeline.insert_track_row(duplicate)
                return self._meta("track", duplicate, owner_list=self.parsed.tracks)
            elif isinstance(obj, ClipInfo):
                duplicate = ClipGraphOperations.duplicate_clip_info(obj)
                self.ops.add_clip_info(duplicate)
                owner = self.current.get("owner_track") or self._track_for_clip(obj)
                if owner:
                    self.ops.add_track_clip(owner, duplicate)
                    self.timeline.insert_clip_info_row(duplicate, owner)
                return self._meta("clip_info", duplicate, owner_track=owner, owner_list=owner.clip_infos if owner else None)
            elif isinstance(obj, Node):
                duplicate = ClipGraphOperations.duplicate_node(obj)
                owner_node = self.current.get("owner_node")
                owner_clip = self.current.get("owner_clip")
                owner_track = self.current.get("owner_track")
                if owner_node:
                    self.ops.add_node_child(owner_node, duplicate)
                    self.timeline.insert_node_row(duplicate, owner_node=owner_node)
                    self.timeline.refresh_object_row(owner_node)
                    return self._meta("node", duplicate, owner_node=owner_node, owner_list=owner_node.child_nodes)
                elif owner_clip:
                    self.ops.add_clip_root_node(owner_clip, duplicate)
                    self.timeline.insert_node_row(duplicate, owner_clip=owner_clip)
                    track = self.current.get("owner_track") or self._track_for_clip(owner_clip)
                    if track:
                        self.ops.add_track_child_node(track, duplicate)
                    return self._meta("node", duplicate, owner_clip=owner_clip, owner_list=owner_clip.root_nodes)
                elif owner_track:
                    self.ops.add_root_node(duplicate)
                    self.ops.add_track_child_node(owner_track, duplicate)
                    self.timeline.insert_node_row(duplicate, owner_track=owner_track)
                    return self._meta("node", duplicate, owner_track=owner_track, owner_list=owner_track.child_nodes)
                else:
                    return self._attach_timeline_node(duplicate)
            elif isinstance(obj, Property):
                owner = self.current.get("owner_node")
                parent_prop = self.current.get("owner_prop")
                duplicate = ClipGraphOperations.duplicate_property(obj)
                if parent_prop:
                    self.ops.add_property_child(parent_prop, duplicate)
                    self.timeline.insert_property_row(duplicate, owner_prop=parent_prop)
                    self.timeline.refresh_object_row(parent_prop)
                    return self._meta("property", duplicate, owner_node=self.current.get("owner_node"), owner_prop=parent_prop, owner_list=parent_prop.child_properties)
                elif owner:
                    self.ops.add_node_property(owner, duplicate)
                    self.timeline.insert_property_row(duplicate, owner_node=owner)
                    self.timeline.refresh_object_row(owner)
                    return self._meta("property", duplicate, owner_node=owner, owner_list=owner.properties)
            elif isinstance(obj, KEY_OBJECT_TYPES):
                owner = self.current.get("owner_prop")
                if isinstance(owner, Property):
                    duplicate = ClipGraphOperations.duplicate_key(obj)
                    if isinstance(obj, SpeedPoint):
                        owner.speed_points_ref.append(duplicate)
                        meta = self._meta("speed_point", duplicate, owner_prop=owner, owner_list=owner.speed_points_ref)
                    else:
                        self.ops.add_property_key(owner, duplicate)
                        meta = self._meta("key", duplicate, owner_prop=owner, owner_list=owner.keys)
                    self.timeline.refresh_object_row(owner)
                    return meta
            return None
        self._structural(mutate)

    def _delete(self):
        obj = self.current.get("obj") if self.current else None
        if isinstance(obj, KEY_OBJECT_TYPES):
            owner = self.current.get("owner_prop") if self.current else None
            def mutate_key():
                if isinstance(owner, Property):
                    if isinstance(obj, SpeedPoint):
                        owner.speed_points_ref = [point for point in owner.speed_points_ref if point is not obj]
                    else:
                        self.ops.remove_property_key(owner, obj)
                    self._remove_key_table_row(obj)
                    self.timeline.refresh_object_row(owner)
            self._structural(mutate_key)
            if isinstance(owner, Property):
                self._select({"kind": "property", "obj": owner})
            else:
                self._clear_selection_view()
            return
        promoted_track = self.current.get("owner_track") if isinstance(obj, ClipInfo) and self.current else None
        promoted_track = promoted_track or (self._track_for_clip(obj) if isinstance(obj, ClipInfo) else None)
        promoted_nodes = [
            node for node in (obj.root_nodes if isinstance(obj, ClipInfo) else [])
            if promoted_track and any(child is node for child in promoted_track.child_nodes)
        ]
        def mutate():
            if isinstance(obj, Track):
                self.ops.remove_track(obj)
            elif isinstance(obj, ClipInfo):
                self.ops.remove_clip_info(obj)
            elif isinstance(obj, Node):
                self.ops.delete_node_everywhere(obj)
            elif isinstance(obj, Property):
                self.ops.delete_property_everywhere(obj)
            elif isinstance(obj, UserDataAssetInfo):
                self.ops.delete_user_data_asset(obj)
            self.timeline.remove_object_row(obj)
            if isinstance(obj, ClipInfo) and promoted_track:
                for node in promoted_nodes:
                    self.timeline.insert_node_row(node, owner_track=promoted_track)
                self.timeline.refresh_object_row(promoted_track)
        self._structural(mutate, clear_selection=True)

    def _move_current(self, delta: int):
        obj = self.current.get("obj") if self.current else None
        owner_list = self.current.get("owner_list") if self.current else None
        if obj is None or owner_list is None:
            return
        def mutate():
            ClipGraphOperations.reorder(owner_list, obj, delta)
            self.timeline.relocate_object_row(obj, owner_list)
            return self.current
        self._structural(mutate)

    def _default_key_for_property(self, prop: Property):
        ptype = _prop_type(prop)
        if self.parsed.header.version >= 85:
            if ptype == PropertyType.BOOL:
                return ClipGraphOperations.create_bool_key()
            if ptype == PropertyType.ACTION:
                return ClipGraphOperations.create_action_key()
            if prop.aux_key_flags == 3 or (prop.keys and all(isinstance(key, NoHermiteKey) for key in prop.keys)):
                return ClipGraphOperations.create_no_hermite_key()
        return ClipGraphOperations.create_key()

    def _create_child_property(self, parent: Property, index: int) -> Property:
        child_type = self._default_child_property_type(parent, index)
        child = ClipGraphOperations.create_property(self.parsed.header.total_frame, int(child_type))
        child.begin_frame = parent.begin_frame
        child.end_frame = parent.end_frame
        child.name = self._default_child_name(parent, index)
        self._populate_fixed_child_properties(child)
        return child

    def _populate_fixed_child_properties(self, prop: Property):
        if not _is_property_container(prop):
            return
        child_types = CONTAINER_CHILD_TYPES.get(_prop_type(prop))
        if not child_types or _prop_type(prop) in {PropertyType.NATIVE_ARRAY, PropertyType.NATIVE_CLASS}:
            return
        for index, child_type in enumerate(child_types):
            child = ClipGraphOperations.create_property(self.parsed.header.total_frame, int(child_type))
            child.begin_frame = prop.begin_frame
            child.end_frame = prop.end_frame
            child.name = self._default_child_name(prop, index)
            self._populate_fixed_child_properties(child)
            prop.child_properties.append(child)

    def _default_child_property_type(self, parent: Property, index: int) -> PropertyType:
        child_types = CONTAINER_CHILD_TYPES.get(_prop_type(parent))
        if child_types and index < len(child_types):
            return child_types[index]
        return PropertyType.F32

    def _default_child_name(self, parent: Property, index: int) -> str:
        labels = COMPONENT_LABELS.get(_prop_type(parent), ())
        return labels[index] if index < len(labels) else f"Value {index + 1}"

    def _container_expected_child_count(self, prop: Property) -> int | None:
        ptype = _prop_type(prop)
        if ptype in {PropertyType.NATIVE_ARRAY, PropertyType.NATIVE_CLASS}:
            return None
        child_types = CONTAINER_CHILD_TYPES.get(ptype)
        return len(child_types) if child_types else None

    def _child_count_text(self, prop: Property):
        expected = self._container_expected_child_count(prop)
        if expected is not None:
            return f"{len(prop.child_properties)} / {expected}"
        return len(prop.child_properties)

    def _can_add_child(self, prop: Property) -> bool:
        expected = self._container_expected_child_count(prop)
        return expected is None or len(prop.child_properties) < expected

    def _structural(self, mutate, clear_selection: bool = False):
        try:
            result = mutate()
        except Exception as exc:
            QMessageBox.warning(self, "Invalid CLIP edit", str(exc))
            return
        if clear_selection:
            self._clear_selection_view()
            result = None
        self._mark_modified()
        if isinstance(result, dict) and result.get("obj") is not None:
            self._select(result)
        else:
            self.timeline.update()
            self._update_buttons(self.current.get("obj") if self.current else None)
    @staticmethod

    def _meta(kind: str, obj, **extra):
        return {"kind": kind, "obj": obj, **extra}

    def _clear_selection_view(self):
        self.current = None
        self.timeline.selected = None
        self._clear_form()
        self.related.clear()
        self._reset_key_table()
        self.title.setText("CLIP Overview")
        self.path_label.clear()
        self.path_label.hide()
        self.validation_label.clear()
        self.validation_label.hide()
        self._update_section_visibility(None)

    def _mark_modified(self):
        self.modified = True
        self.modified_changed.emit(True)
        self._update_status()

    def _object_changed(self, obj):
        self._mark_modified()
        self.timeline.refresh_object_row(obj)
        if isinstance(obj, KEY_OBJECT_TYPES):
            self._sync_key_table_row(obj)
            owner = self.current.get("owner_prop") if self.current else self._property_for_key(obj)
            if isinstance(owner, Property):
                self.timeline.refresh_object_row(owner)
        if self.current and self.current.get("obj") is obj:
            self.title.setText(self._title(obj))
            self.path_label.setText(self._selection_path(obj))
            self._show_validation(obj)

    def _timeline_graph_changed(self):
        self._mark_modified()
        self._sync_visible_details()

    def _sync_visible_details(self):
        obj = self.current.get("obj") if self.current else None
        if obj is None:
            return
        self.timeline.refresh_object_row(obj)
        owner = self.current.get("owner_prop") if self.current else None
        if isinstance(owner, Property):
            self.timeline.refresh_object_row(owner)
        for row in range(self.form.rowCount()):
            field_item = self.form.itemAt(row, QFormLayout.FieldRole)
            field = field_item.widget() if field_item else None
            attr = field.property("clip_attr") if isinstance(field, QLineEdit) else None
            if attr and field.property("clip_obj_id") == id(obj) and hasattr(obj, attr):
                value = getattr(obj, attr, "")
                field.setText(_guid_text(value) if isinstance(value, (bytes, bytearray)) else str(value))
        self._sync_key_table_row(obj)
        self._show_validation(obj)

    def _sync_key_table_row(self, obj):
        if not isinstance(obj, KEY_OBJECT_TYPES):
            return
        with self._blocked_key_table():
            for row in range(self.keys.rowCount()):
                item = self.keys.item(row, 0)
                data = item.data(Qt.UserRole) if item else None
                if data and data.get("key") is obj:
                    item.setText(str(getattr(obj, "frame", "")))
                    return

    def _update_buttons(self, obj):
        owner_list = self.current.get("owner_list") if self.current else None
        owner_index = _identity_index(owner_list, obj)
        can_add_track = obj is None
        can_add_clip = self.parsed.header.version >= 40 and isinstance(obj, Track)
        can_add_node = isinstance(obj, (Track, ClipInfo, Node)) or obj is None
        can_add_prop = isinstance(obj, Node)
        can_add_child = isinstance(obj, Property) and _is_property_container(obj) and self._can_add_child(obj)
        can_add_key = isinstance(obj, Property) and not _is_property_container(obj)
        can_add_speed = isinstance(obj, Property) and (
            _prop_type(obj) == PropertyType.PATH_POINT3D or bool(obj.speed_points_ref)
        )
        can_duplicate = isinstance(obj, (Track, ClipInfo, Node, Property, *KEY_OBJECT_TYPES))
        can_delete = isinstance(obj, (Track, ClipInfo, Node, Property, UserDataAssetInfo, *KEY_OBJECT_TYPES))
        add_visibility = {
            "track": can_add_track,
            "clip": can_add_clip,
            "node": can_add_node,
            "property": can_add_prop,
            "child": can_add_child,
            "key": can_add_key,
            "speed": can_add_speed,
        }
        for name, visible in add_visibility.items():
            self._action(self.add_actions[name], visible)
        self._action(self.add_btn, any(add_visibility.values()))
        self._action(self.dup_btn, can_duplicate)
        self._action(self.del_btn, can_delete)
        self._action(self.move_up_btn, owner_index > 0)
        self._action(self.move_down_btn, owner_list is not None and 0 <= owner_index < len(owner_list) - 1)
    @staticmethod

    def _action(action, visible: bool):
        action.setVisible(visible)
        action.setEnabled(visible)

    def _clear_form(self):
        while self.form.rowCount():
            self.form.removeRow(0)

    def _attach_timeline_node(self, node: Node, track: Track | None = None, clip_info: ClipInfo | None = None):
        if clip_info is None and self.parsed.header.version >= 85 and self.parsed.clip_infos:
            clip_info = self.parsed.clip_infos[0]
        if clip_info is not None:
            self.ops.add_clip_root_node(clip_info, node)
            self.timeline.insert_node_row(node, owner_clip=clip_info)
            owner_meta = self._meta("node", node, owner_clip=clip_info, owner_list=clip_info.root_nodes)
        else:
            self.ops.add_root_node(node)
            owner_meta = self._meta("node", node, owner_list=self.parsed.root_nodes)
        track = track or (self.parsed.tracks[0] if self.parsed.tracks else None)
        if track is not None:
            self.ops.add_track_child_node(track, node)
            if clip_info is None:
                self.timeline.insert_node_row(node, owner_track=track)
                owner_meta = self._meta("node", node, owner_track=track, owner_list=track.child_nodes)
        elif clip_info is None:
            self.timeline.insert_root_node_row(node)
        return owner_meta

    def _track_for_clip(self, clip_info: ClipInfo):
        return next((track for track in self.parsed.tracks if any(ci is clip_info for ci in track.clip_infos)), None)

    def _iter_graph_properties(self):
        seen: set[int] = set()
        def visit(prop: Property):
            if id(prop) in seen:
                return
            seen.add(id(prop))
            yield prop
            for child in prop.child_properties:
                yield from visit(child)
        for node in self.parsed.nodes:
            for prop in node.properties:
                yield from visit(prop)
        for prop in self.parsed.properties:
            yield from visit(prop)

    def _property_for_key(self, key_obj):
        for prop in self._iter_graph_properties():
            for candidate in self._key_rows(prop):
                if candidate[1] is key_obj:
                    return prop
        return None

    def _update_duration_spin(self):
        self.duration_spin.blockSignals(True)
        self.duration_spin.setValue(float(self.parsed.header.total_frame or 0.0))
        self.duration_spin.blockSignals(False)

    def _duration_changed(self, value: float):
        self.parsed.header.total_frame = float(value)
        self._mark_modified()
        self.timeline.update()

    def _title(self, obj):
        if self.current and self.current.get("kind") == "oword":
            return f"OWord {self.current.get('index', 0)}"
        if isinstance(obj, Track):
            return obj.group_name or obj.type_unicode or obj.type_ascii or "Track"
        if isinstance(obj, ClipInfo):
            return obj.unicode_name or "ClipInfo"
        if isinstance(obj, Node):
            name = _node_display_name(obj)
            return f"{_node_type_name(obj)}: {name}"
        if isinstance(obj, Property):
            return f"{obj.name or 'Property'} ({_prop_type_name(obj)})"
        if isinstance(obj, SpeedPoint):
            return f"SpeedPoint @ {obj.frame:g}"
        if isinstance(obj, (Key, BoolKey, ActionKey, NoHermiteKey)):
            return f"{type(obj).__name__} @ {obj.frame:g}"
        if isinstance(obj, UserDataAssetInfo):
            return obj.path_unicode or obj.type_ascii or "UserDataAsset"
        return "CLIP Overview"
    @staticmethod

    def _key_raw_text(key):
        if hasattr(key, "raw0"):
            return f"{key.raw0}, {key.raw1}"
        return ""
    _is_container = staticmethod(_is_property_container)

