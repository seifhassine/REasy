from __future__ import annotations

from collections import Counter
import math
from types import SimpleNamespace

from PySide6.QtCore import QPointF, QRectF, QT_TRANSLATE_NOOP, Qt, Signal
lazy from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView, QSizePolicy, QWidget

lazy from .models import RESOURCE_KINDS, STAGE_KEYS, SdfProgram
lazy from .sdf_file import write_channels


_STAGE_TITLES = {
    "vertex": QT_TRANSLATE_NOOP("SdfPipelineGraph", "Vertex shader"),
    "hull": QT_TRANSLATE_NOOP("SdfPipelineGraph", "Hull shader"),
    "domain": QT_TRANSLATE_NOOP("SdfPipelineGraph", "Domain shader"),
    "geometry": QT_TRANSLATE_NOOP("SdfPipelineGraph", "Geometry shader"),
}
_BLEND_SOURCE = (0.95, 0.24, 0.12, 0.55)
_BLEND_DESTINATION = (0.08, 0.28, 0.72, 1.0)


def _resource_summary(counts: Counter[str]) -> str:
    return " · ".join(f"{count} {kind.replace('_', ' ')}" for kind, count in counts.items())


class SdfPipelineGraph(QGraphicsView):
    node_selected = Signal(str, object)
    _WIDTH = 154
    _HEIGHT = 72
    _GAP = 38
    _COLORS = {
        "input": ("#315e7d", "#6fb6df"),
        "shader": ("#553b83", "#ac8de5"),
        "state": ("#674d22", "#d7ad58"),
        "output": ("#276148", "#6bc69c"),
        "resource": ("#34434d", "#8599a6"),
        "compute": ("#6b3d61", "#d283c1"),
        "absent": ("#292d32", "#515861"),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setBackgroundBrush(QBrush(QColor("#1d2024")))
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setMinimumHeight(310)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._nodes: dict[str, object] = {}
        self.clear_message(self.tr("Select a program to see its executable pipeline."))

    def clear_message(self, message: str) -> None:
        scene = self.scene()
        scene.clear()
        item = scene.addText(message)
        item.setDefaultTextColor(QColor("#c8cdd4"))
        item.setPos(18, 18)
        scene.setSceneRect(0, 0, 720, 200)
        self._nodes.clear()

    @staticmethod
    def _shader_detail(shader) -> str:
        if not shader or not shader.present:
            return "not bound"
        reflection = shader.reflection
        model = (
            f"{reflection.stage}_{reflection.model_major}_{reflection.model_minor}"
            if reflection is not None
            else "reflection deferred"
        )
        return model.replace("_", " ").title()

    def _node(self, key: str, title: str, detail: str, x: float, y: float, tone: str, payload=None):
        scene = self.scene()
        fill, outline = self._COLORS[tone]
        node_path = QPainterPath()
        node_path.addRoundedRect(QRectF(x, y, self._WIDTH, self._HEIGHT), 8, 8)
        rect = scene.addPath(
            node_path,
            QPen(QColor(outline), 1.5),
            QBrush(QColor(fill)),
        )
        rect.setData(0, (key, payload))
        rect.setToolTip(detail)
        title_font = QFont(self.font())
        title_font.setBold(True)
        label = scene.addText(title, title_font)
        label.setDefaultTextColor(QColor("white"))
        label.setTextWidth(self._WIDTH - 14)
        label.setPos(x + 7, y + 5)
        label.setData(0, (key, payload))
        sub = scene.addText(detail)
        sub.setDefaultTextColor(QColor("#d5d9de"))
        sub.setTextWidth(self._WIDTH - 14)
        sub.setPos(x + 7, y + 32)
        sub.setData(0, (key, payload))
        self._nodes[key] = rect
        return rect

    def _edge(self, source: str, target: str, *, color="#77818b") -> None:
        left = self._nodes.get(source)
        right = self._nodes.get(target)
        if left is None or right is None:
            return
        a, b = left.sceneBoundingRect(), right.sceneBoundingRect()
        vertically_aligned = abs(a.center().x() - b.center().x()) < self._WIDTH * 0.5
        if vertically_aligned:
            if a.center().y() < b.center().y():
                start, end = QPointF(a.center().x(), a.bottom()), QPointF(b.center().x(), b.top())
            else:
                start, end = QPointF(a.center().x(), a.top()), QPointF(b.center().x(), b.bottom())
            bend = max(18.0, abs(end.y() - start.y()) * 0.42)
            direction = 1 if end.y() >= start.y() else -1
            path = QPainterPath(start)
            path.cubicTo(
                start.x(),
                start.y() + bend * direction,
                end.x(),
                end.y() - bend * direction,
                end.x(),
                end.y(),
            )
        else:
            start = QPointF(a.right(), a.center().y())
            end = QPointF(b.left(), b.center().y())
            bend = max(22.0, (end.x() - start.x()) * 0.42)
            path = QPainterPath(start)
            path.cubicTo(start.x() + bend, start.y(), end.x() - bend, end.y(), end.x(), end.y())
        item = self.scene().addPath(path, QPen(QColor(color), 1.6))
        item.setZValue(-2)

        angle = math.atan2(end.y() - start.y(), end.x() - start.x())
        arrow = QPolygonF(
            [
                end,
                end - QPointF(math.cos(angle - 0.55) * 9, math.sin(angle - 0.55) * 9),
                end - QPointF(math.cos(angle + 0.55) * 9, math.sin(angle + 0.55) * 9),
            ]
        )
        arrow_item = self.scene().addPolygon(arrow, QPen(QColor(color)), QBrush(QColor(color)))
        arrow_item.setZValue(-1)

    def _compute_chain(self, program: SdfProgram, compute, counts, x: int, y: int, x_step: int) -> None:
        library = program.raytracing_library
        detail = _resource_summary(counts) or "no declared resources"
        resource_title = self.tr("Ray-tracing resources") if library else self.tr("Compute resources")
        self._node("resources-compute", resource_title, detail, x, y, "resource", ("resources", "compute"))
        compute_title = self.tr("DXR library") if library else self.tr("Compute shader")
        self._node("compute", compute_title, self._shader_detail(compute), x + x_step, y, "compute", "compute")
        dispatch_title = self.tr("DispatchRays") if library else self.tr("Dispatch")
        dispatch_detail = (self.tr("Runtime ray dimensions") if library
                           else " × ".join(map(str, program.compute_group_size)) + " threads/group")
        self._node("dispatch", dispatch_title, dispatch_detail, x + 2 * x_step, y, "output", "dispatch")
        self._edge("resources-compute", "compute", color="#c47db5")
        self._edge("compute", "dispatch", color="#c47db5")

    def set_program(self, program: SdfProgram) -> None:
        self.scene().clear()
        self._nodes.clear()
        x_step = self._WIDTH + self._GAP
        y = 145
        compute = program.shader("compute")
        bindings_by_stage: dict[str, Counter[str]] = {stage: Counter() for stage in STAGE_KEYS}
        for kind, bindings in zip(RESOURCE_KINDS, program.bindings):
            for binding in bindings:
                for stage in binding.active_stages:
                    bindings_by_stage[stage][kind] += 1
        graphics_present = any(
            shader.present for shader in program.shaders if shader.role != "compute"
        )
        if compute and compute.present and not graphics_present:
            self._compute_chain(program, compute, bindings_by_stage["compute"], 24, y, x_step)
            self.scene().setSceneRect(0, 0, 24 + 3 * x_step + self._WIDTH, 300)
            self.fit_graph()
            return
        layout_count = program.input_layout.element_count if program.input_layout else 0
        input_detail = f"{layout_count} elements · {program.primitive_topology}"
        self._node(
            "input",
            self.tr("Mesh input"),
            input_detail,
            24,
            y,
            "input",
            "input_layout",
        )
        graphics_stages = tuple(
            role
            for role in ("vertex", "hull", "domain", "geometry")
            if (program.shader(role) is not None and program.shader(role).present)
        )
        previous = "input"
        column = 0
        stage_x: dict[str, float] = {}
        for role in graphics_stages:
            column += 1
            shader = program.shader(role)
            stage_x[role] = 24 + column * x_step
            self._node(
                role,
                self.tr(_STAGE_TITLES[role]),
                self._shader_detail(shader),
                stage_x[role],
                y,
                "shader",
                role,
            )
            self._edge(previous, role)
            previous = role

        column += 1
        raster_x = 24 + column * x_step
        raster = program.rasterizer_state
        raster_detail = (
            f"{raster.fill_mode} · cull {raster.cull_mode}"
            if raster is not None
            else "not bound"
        )
        self._node(
            "rasterizer",
            self.tr("Geometry raster"),
            raster_detail,
            raster_x,
            y,
            "state",
            "rasterizer",
        )
        self._edge(previous, "rasterizer")

        pixel = program.shader("pixel")
        previous = "rasterizer"
        if pixel and pixel.present:
            column += 1
            stage_x["pixel"] = 24 + column * x_step
            self._node(
                "pixel",
                self.tr("Pixel shader"),
                self._shader_detail(pixel),
                stage_x["pixel"],
                y,
                "shader",
                "pixel",
            )
            self._edge("rasterizer", "pixel")
            previous = "pixel"
        depth = program.depth_stencil_state
        blend = program.blend_state
        om_detail = ""
        if depth:
            om_detail += f"depth {depth.depth_function}/{depth.depth_write_mask}"
        if blend:
            enabled = sum(target.blend_enable for target in blend.render_targets)
            om_detail += (" · " if om_detail else "") + f"{enabled} blended RT"
        column += 1
        self._node(
            "output",
            self.tr("Final output"),
            om_detail or "default output",
            24 + column * x_step,
            y,
            "output",
            "output_merger",
        )
        self._edge(previous, "output")

        stage_x["compute"] = 24 + max(1, column // 2) * x_step
        for stage in (*graphics_stages, *(('pixel',) if pixel and pixel.present else ())):
            counts = bindings_by_stage[stage]
            if not counts:
                continue
            detail = _resource_summary(counts)
            key = f"resources-{stage}"
            self._node(
                key,
                self.tr("Bound resources"),
                detail,
                stage_x[stage],
                30,
                "resource",
                ("resources", stage),
            )
            self._edge(key, stage, color="#68a8c4")

        if compute and compute.present:
            self._compute_chain(
                program, compute, bindings_by_stage["compute"], stage_x["compute"] - x_step, 310, x_step
            )

        width = 48 + column * x_step + self._WIDTH
        height = 420 if compute and compute.present else 270
        self.scene().setSceneRect(0, 0, width, height)
        self.fit_graph()

    def fit_graph(self) -> None:
        if not self._nodes:
            return
        self.resetTransform()
        scale = min(
            1.0,
            (self.viewport().width() - 16) / max(1.0, self.scene().width()),
            (self.viewport().height() - 16) / max(1.0, self.scene().height()),
        )
        self.scale(max(0.35, scale), max(0.35, scale))
        self.horizontalScrollBar().setValue(0)
        self.verticalScrollBar().setValue(0)

    def wheelEvent(self, event):
        delta = event.angleDelta().y() or event.pixelDelta().y()
        if delta:
            current = self.transform().m11()
            target = min(3.0, max(0.2, current * (1.16 if delta > 0 else 1 / 1.16)))
            self.scale(target / current, target / current)
            event.accept()
            return
        super().wheelEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(event.position().toPoint())
            while item is not None:
                payload = item.data(0)
                if isinstance(payload, tuple) and len(payload) == 2:
                    self.node_selected.emit(str(payload[0]), payload[1])
                    break
                item = item.parentItem()
        super().mouseReleaseEvent(event)


def _channel_factor(name: str, source, destination, constant):
    alpha = source[3]
    destination_alpha = destination[3]
    factors = {
        "zero": (0.0, 0.0, 0.0, 0.0),
        "one": (1.0, 1.0, 1.0, 1.0),
        "source-color": source,
        "inverse-source-color": tuple(1.0 - value for value in source),
        "source-alpha": (alpha,) * 4,
        "inverse-source-alpha": (1.0 - alpha,) * 4,
        "destination-alpha": (destination_alpha,) * 4,
        "inverse-destination-alpha": (1.0 - destination_alpha,) * 4,
        "destination-color": destination,
        "inverse-destination-color": tuple(1.0 - value for value in destination),
        "source-alpha-saturate": (min(alpha, 1.0 - destination_alpha),) * 3 + (1.0,),
        "blend-factor": constant,
        "inverse-blend-factor": tuple(1.0 - value for value in constant),
        "source1-color": source,
        "inverse-source1-color": tuple(1.0 - value for value in source),
        "source1-alpha": (alpha,) * 4,
        "inverse-source1-alpha": (1.0 - alpha,) * 4,
    }
    return factors.get(name, (1.0, 1.0, 1.0, 1.0))


def _operate(name: str, source: float, destination: float) -> float:
    if name == "subtract":
        return source - destination
    if name == "reverse-subtract":
        return destination - source
    if name == "minimum":
        return min(source, destination)
    if name == "maximum":
        return max(source, destination)
    return source + destination


def preview_blend(target, source, destination):
    if target is None or not target.blend_enable:
        result = source
    else:
        constant = (0.5, 0.5, 0.5, 0.5)
        source_factor = _channel_factor(target.source_blend, source, destination, constant)
        destination_factor = _channel_factor(target.destination_blend, source, destination, constant)
        source_alpha_factor = _channel_factor(target.source_alpha_blend, source, destination, constant)
        destination_alpha_factor = _channel_factor(target.destination_alpha_blend, source, destination, constant)
        rgb = tuple(
            _operate(
                target.blend_operation,
                source[channel] * source_factor[channel],
                destination[channel] * destination_factor[channel],
            )
            for channel in range(3)
        )
        alpha = _operate(
            target.alpha_blend_operation,
            source[3] * source_alpha_factor[3],
            destination[3] * destination_alpha_factor[3],
        )
        result = rgb + (alpha,)
    mask = target.write_mask if target is not None else 0xF
    return tuple(
        min(1.0, max(0.0, result[index] if mask & (1 << index) else destination[index]))
        for index in range(4)
    )


def _depth_accept(function: str, source: float, destination: float) -> bool:
    epsilon = 1e-6
    return {
        "never": False,
        "less": source < destination,
        "equal": abs(source - destination) <= epsilon,
        "less-equal": source <= destination,
        "greater": source > destination,
        "not-equal": abs(source - destination) > epsilon,
        "greater-equal": source >= destination,
        "always": True,
    }.get(function, True)


class PipelineStatePreview(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._program: SdfProgram | None = None
        self._render_target_index = 0
        self._state_overrides: dict[str, dict[str, object]] = {}
        self.setMinimumSize(620, 260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_program(self, program: SdfProgram | None) -> None:
        self._program = program
        self._state_overrides.clear()
        self.update()

    def set_state_override(self, kind: str, values) -> None:
        if kind == "reset":
            self._state_overrides.clear()
        elif isinstance(values, dict):
            self._state_overrides[str(kind)] = dict(values)
        self.update()

    def set_render_target(self, index: int) -> None:
        self._render_target_index = max(0, min(7, int(index)))
        self.update()

    def _panel(self, painter: QPainter, rect: QRectF, title: str) -> QRectF:
        painter.setPen(QPen(QColor("#4f5863"), 1))
        painter.setBrush(QColor("#252a30"))
        painter.drawRoundedRect(rect, 7, 7)
        painter.setPen(QColor("#e5e8ec"))
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect.adjusted(12, 7, -8, -4), Qt.AlignmentFlag.AlignTop, title)
        font.setBold(False)
        painter.setFont(font)
        return rect.adjusted(12, 34, -12, -10)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#1b1e22"))
        if self._program is None:
            painter.setPen(QColor("#c8cdd4"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.tr("Select a program."))
            return
        margin, gap = 10.0, 10.0
        width = (self.width() - margin * 2 - gap * 2) / 3
        height = self.height() - margin * 2
        raster_rect = QRectF(margin, margin, width, height)
        depth_rect = QRectF(margin + width + gap, margin, width, height)
        blend_rect = QRectF(margin + (width + gap) * 2, margin, width, height)
        self._paint_raster(painter, self._panel(painter, raster_rect, self.tr("Rasterizer behavior")))
        self._paint_depth(painter, self._panel(painter, depth_rect, self.tr("Depth / stencil behavior")))
        self._paint_blend(
            painter,
            self._panel(
                painter,
                blend_rect,
                self.tr("Render-target {index} blend").format(index=self._render_target_index),
            ),
        )
        if self._state_overrides:
            badge = QRectF(self.width() - 155, 16, 137, 25)
            painter.setPen(QPen(QColor("#d7ad58"), 1))
            painter.setBrush(QColor("#4e3a19"))
            painter.drawRoundedRect(badge, 12, 12)
            painter.setPen(QColor("#f2dcac"))
            painter.drawText(
                badge,
                Qt.AlignmentFlag.AlignCenter,
                self.tr("LIVE · NOT APPLIED"),
            )

    def _paint_raster(self, painter: QPainter, rect: QRectF) -> None:
        state = self._program.rasterizer_state
        if state is None:
            painter.setPen(QColor("#aeb4bb"))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.tr("No rasterizer state"))
            return
        override = self._state_overrides.get("rasterizer", {})
        value = lambda name: override.get(name, getattr(state, name))
        clip = rect.adjusted(4, 4, -4, -50)
        if value("scissor_enable"):
            scissor = clip.adjusted(clip.width() * 0.15, clip.height() * 0.12, -clip.width() * 0.1, -clip.height() * 0.18)
            painter.save()
            painter.setClipRect(scissor)
        bias = max(-16.0, min(16.0, float(value("depth_bias")) * 0.5))
        triangles = (
            (QColor("#58a6d6"), QPolygonF([QPointF(clip.left() + 8 + bias, clip.bottom() - 10), QPointF(clip.center().x() + bias, clip.top() + 9), QPointF(clip.right() - 8 + bias, clip.bottom() - 10)]), "front"),
            (
                QColor("#d88467"),
                QPolygonF(
                    [
                        QPointF(clip.left() + 22 - bias, clip.top() + 24),
                        QPointF(clip.center().x() - bias, clip.bottom() - 2),
                        QPointF(clip.right() - 18 - bias, clip.top() + 34),
                    ]
                ),
                "back",
            ),
        )
        front_name = "front" if not value("front_counter_clockwise") else "back"
        for color, polygon, winding in triangles:
            logical = front_name if winding == "front" else ("back" if front_name == "front" else "front")
            culled = value("cull_mode") == logical
            if culled:
                painter.setPen(QPen(QColor("#717780"), 1, Qt.PenStyle.DashLine))
                painter.setBrush(Qt.BrushStyle.NoBrush)
            else:
                painter.setPen(QPen(color.lighter(135), 2 if value("multisample_enable") else 1))
                painter.setBrush(Qt.BrushStyle.NoBrush if value("fill_mode") == "wireframe" else color)
            painter.drawPolygon(polygon)
        if value("scissor_enable"):
            painter.restore()
            painter.setPen(QPen(QColor("#e6c45c"), 1, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(scissor)
        flags = [str(value("fill_mode")), f"cull {value('cull_mode')}"]
        if value("depth_clip_enable"):
            flags.append("depth clip")
        if value("scissor_enable"):
            flags.append("scissor")
        if value("multisample_enable"):
            flags.append("MSAA")
        if value("antialiased_line_enable"):
            flags.append("AA lines")
        painter.setPen(QColor("#c9ced5"))
        painter.drawText(rect.adjusted(0, rect.height() - 42, 0, 0), Qt.TextFlag.TextWordWrap, " · ".join(flags))

    def _paint_depth(self, painter: QPainter, rect: QRectF) -> None:
        state = self._program.depth_stencil_state
        if state is None:
            painter.setPen(QColor("#aeb4bb"))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.tr("No depth/stencil state"))
            return
        override = self._state_overrides.get("depth", {})
        value = lambda name: override.get(name, getattr(state, name))
        source_depth, stored_depth = 0.28, 0.62
        accepted = not value("depth_enable") or _depth_accept(str(value("depth_function")), source_depth, stored_depth)
        bar = QRectF(rect.left() + 5, rect.top() + 12, rect.width() - 10, 34)
        painter.setBrush(QColor("#314157"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(bar, 5, 5)
        marker_x = bar.left() + stored_depth * bar.width()
        source_x = bar.left() + source_depth * bar.width()
        painter.setPen(QPen(QColor("#8fb2dd"), 3))
        painter.drawLine(QPointF(marker_x, bar.top()), QPointF(marker_x, bar.bottom()))
        painter.setPen(QPen(QColor("#efb45d"), 3))
        painter.drawLine(QPointF(source_x, bar.top()), QPointF(source_x, bar.bottom()))
        painter.setPen(QColor("#d8dde3"))
        verdict = self.tr("fragment passes") if accepted else self.tr("fragment rejected")
        painter.drawText(
            QRectF(rect.left(), bar.bottom() + 9, rect.width(), 24),
            Qt.AlignmentFlag.AlignHCenter,
            f"0.28 {value('depth_function')} 0.62 → {verdict}",
        )
        status = [f"write {value('depth_write_mask')}"]
        if value("stencil_enable"):
            status.append(
                f"stencil read {int(value('stencil_read_mask')):02X} / write {int(value('stencil_write_mask')):02X}"
            )
        else:
            status.append("stencil disabled")
        if value("depth_bounds_test_enable"):
            status.append("depth bounds")
        painter.setPen(QColor("#c9ced5"))
        painter.drawText(rect.adjusted(0, 96, 0, 0), Qt.TextFlag.TextWordWrap, "\n".join(status))

    def _paint_blend(self, painter: QPainter, rect: QRectF) -> None:
        target = (
            self._program.blend_state.render_targets[self._render_target_index]
            if self._program.blend_state
            else None
        )
        override = self._state_overrides.get("blend")
        if override and int(override.get("target_index", -1)) == self._render_target_index:
            write_mask = int(override.get("write_mask", 0xF))
            target = SimpleNamespace(
                blend_enable=bool(override.get("blend_enable", False)),
                source_blend=str(override.get("source_blend", "one")),
                destination_blend=str(override.get("destination_blend", "zero")),
                blend_operation=str(override.get("blend_operation", "add")),
                source_alpha_blend=str(override.get("source_alpha_blend", "one")),
                destination_alpha_blend=str(override.get("destination_alpha_blend", "zero")),
                alpha_blend_operation=str(override.get("alpha_blend_operation", "add")),
                write_mask=write_mask,
                write_channels=write_channels(write_mask),
            )
        result = preview_blend(target, _BLEND_SOURCE, _BLEND_DESTINATION)
        swatch_width = max(18.0, (rect.width() - 20) / 3)
        labels = (("Src", _BLEND_SOURCE), ("Dst", _BLEND_DESTINATION), ("Out", result))
        for index, (label, color) in enumerate(labels):
            x = rect.left() + index * (swatch_width + 7)
            swatch = QRectF(x, rect.top() + 10, swatch_width, 55)
            painter.setPen(QColor("#707984"))
            painter.setBrush(QColor.fromRgbF(*color))
            painter.drawRoundedRect(swatch, 4, 4)
            painter.setPen(QColor("#d8dde3"))
            painter.drawText(QRectF(x, swatch.bottom() + 3, swatch_width, 22), Qt.AlignmentFlag.AlignCenter, label)
        if target is None:
            detail = "no blend state"
        elif not target.blend_enable:
            detail = f"blend disabled · write {target.write_channels}"
        else:
            detail = (
                f"RGB: {target.source_blend} {target.blend_operation} {target.destination_blend}\n"
                f"A: {target.source_alpha_blend} {target.alpha_blend_operation} {target.destination_alpha_blend}\n"
                f"write {target.write_channels}"
            )
        painter.setPen(QColor("#c9ced5"))
        painter.drawText(rect.adjusted(0, 100, 0, 0), Qt.TextFlag.TextWordWrap, detail)


__all__ = ["PipelineStatePreview", "SdfPipelineGraph", "preview_blend"]
