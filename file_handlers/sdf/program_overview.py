from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
lazy from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

lazy from .models import RESOURCE_KINDS, SdfProgram, humanize_identifier, stage_display_name


def _human(value: str) -> str:
    return humanize_identifier(value).replace("G Buffer", "G-buffer").replace("G buffer", "G-buffer")


def _joined(values, *, limit: int = 10) -> str:
    unique = tuple(dict.fromkeys(str(value) for value in values if str(value)))
    shown = unique[:limit]
    suffix = f" and {len(unique) - limit} more" if len(unique) > limit else ""
    return ", ".join(shown) + suffix


class PassBehaviorCanvas(QWidget):
    action_requested = Signal(str)

    _TONES = {
        "input": ("#17384b", "#67b8e3"),
        "shader": ("#322758", "#b59af0"),
        "state": ("#493719", "#e0b65f"),
        "output": ("#173d31", "#6bd0a4"),
        "resource": ("#27333b", "#91a7b5"),
        "compute": ("#4b2946", "#dc86c9"),
        "neutral": ("#2b3036", "#68727c"),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._model: dict[str, object] = {}
        self._regions: list[tuple[QRectF, str]] = []
        self._hover_action = ""
        self.setMouseTracking(True)
        self.setMinimumSize(500, 410)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    @property
    def model(self) -> dict[str, object]:
        return self._model

    def set_model(self, model: dict[str, object]) -> None:
        self._model = dict(model)
        self.setToolTip(
            self.tr("Click a visual section to open its authoring controls.")
            if model
            else ""
        )
        self.update()

    def _remember(self, rect: QRectF, action: str) -> None:
        if action:
            self._regions.append((QRectF(rect), action))

    @staticmethod
    def _rounded_path(rect: QRectF, radius: float = 9.0) -> QPainterPath:
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        return path

    def _draw_panel(
        self,
        painter: QPainter,
        rect: QRectF,
        title: str,
        detail: str,
        tone: str,
        action: str,
    ) -> None:
        fill, outline = self._TONES.get(tone, self._TONES["neutral"])
        hovered = bool(action and action == self._hover_action)
        painter.setPen(QPen(QColor(outline), 2.4 if hovered else 1.25))
        panel_fill = QColor(fill)
        if hovered:
            panel_fill = panel_fill.lighter(118)
        painter.setBrush(panel_fill)
        painter.drawPath(self._rounded_path(rect))
        painter.setPen(QColor("#f2f4f7"))
        font = painter.font()
        font.setBold(True)
        font.setPointSize(max(8, font.pointSize()))
        painter.setFont(font)
        painter.drawText(
            rect.adjusted(11, 9, -11, -8),
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
            title.upper(),
        )
        font.setBold(False)
        font.setPointSize(max(8, font.pointSize() - 1))
        painter.setFont(font)
        painter.setPen(QColor("#dce2e8"))
        painter.drawText(
            rect.adjusted(11, 35, -11, -8),
            Qt.TextFlag.TextWordWrap,
            detail,
        )
        self._remember(rect, action)

    @staticmethod
    def _draw_arrow(painter: QPainter, left: QRectF, right: QRectF) -> None:
        start = QPointF(left.right() + 3, left.center().y())
        end = QPointF(right.left() - 4, right.center().y())
        painter.setPen(QPen(QColor("#73808c"), 1.7))
        painter.drawLine(start, end)
        arrow = QPolygonF(
            [
                end,
                QPointF(end.x() - 8, end.y() - 5),
                QPointF(end.x() - 8, end.y() + 5),
            ]
        )
        painter.setBrush(QColor("#73808c"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(arrow)

    def _draw_resource_strip(
        self,
        painter: QPainter,
        rect: QRectF,
        resources: tuple[tuple[str, int], ...],
    ) -> None:
        hovered = self._hover_action == "resources"
        painter.setPen(QPen(QColor("#91a7b5"), 2.2 if hovered else 1.0))
        painter.setBrush(QColor("#222a30").lighter(112 if hovered else 100))
        painter.drawRoundedRect(rect, 8, 8)
        painter.setPen(QColor("#dce2e8"))
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(
            rect.adjusted(10, 7, -10, -5),
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
            self.tr("BOUND INPUTS"),
        )
        font.setBold(False)
        painter.setFont(font)
        if not resources:
            painter.setPen(QColor("#9da7b1"))
            painter.drawText(
                rect.adjusted(10, 30, -10, -7),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                self.tr("No declared buffers, textures, samplers, or writable resources"),
            )
        else:
            count = len(resources)
            gap = 7.0
            width = max(62.0, (rect.width() - 20 - gap * (count - 1)) / count)
            for index, (label, value) in enumerate(resources):
                chip = QRectF(
                    rect.left() + 10 + index * (width + gap),
                    rect.top() + 31,
                    width,
                    rect.height() - 40,
                )
                painter.setPen(QPen(QColor("#53616c"), 1))
                painter.setBrush(QColor("#303a42"))
                painter.drawRoundedRect(chip, 6, 6)
                painter.setPen(QColor("#e8edf1"))
                bold = painter.font()
                bold.setBold(True)
                painter.setFont(bold)
                painter.drawText(
                    QRectF(chip.left(), chip.top() + 4, chip.width(), 20),
                    Qt.AlignmentFlag.AlignCenter,
                    str(value),
                )
                bold.setBold(False)
                painter.setFont(bold)
                painter.setPen(QColor("#b9c2ca"))
                painter.drawText(
                    QRectF(chip.left() + 3, chip.top() + 24, chip.width() - 6, chip.height() - 26),
                    Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap,
                    label,
                )
        self._remember(rect, "resources")

    def _draw_feature_chips(
        self,
        painter: QPainter,
        rect: QRectF,
        features: tuple[str, ...],
        action: str,
    ) -> None:
        painter.setPen(QColor("#aeb8c2"))
        painter.drawText(
            QRectF(rect.left(), rect.top(), rect.width(), 21),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self.tr("ACTIVE BEHAVIOR"),
        )
        x = rect.left()
        y = rect.top() + 27
        font_metrics = painter.fontMetrics()
        for feature in features or (self.tr("Default behavior"),):
            width = min(rect.width(), max(72.0, font_metrics.horizontalAdvance(feature) + 22.0))
            if x + width > rect.right() and x > rect.left():
                x = rect.left()
                y += 31
            chip = QRectF(x, y, width, 25)
            hovered = action == self._hover_action
            painter.setPen(QPen(QColor("#667582"), 1.5 if hovered else 1.0))
            painter.setBrush(QColor("#30373e").lighter(112 if hovered else 100))
            painter.drawRoundedRect(chip, 12, 12)
            painter.setPen(QColor("#dce2e8"))
            painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, feature)
            x += width + 7
        self._remember(rect, action)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#181c20"))
        self._regions.clear()
        if not self._model:
            painter.setPen(QColor("#c5ccd3"))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                self.tr("Select a pass to visualize its behavior."),
            )
            return

        margin = 14.0
        purpose_rect = QRectF(margin, margin, self.width() - margin * 2, 62)
        hovered = self._hover_action == "program"
        painter.setPen(QPen(QColor("#56636f"), 1.7 if hovered else 1.0))
        painter.setBrush(QColor("#232a30").lighter(112 if hovered else 100))
        painter.drawRoundedRect(purpose_rect, 9, 9)
        painter.setPen(QColor("#eef1f4"))
        font = painter.font()
        font.setBold(True)
        font.setPointSize(max(9, font.pointSize() + 1))
        painter.setFont(font)
        painter.drawText(
            purpose_rect.adjusted(12, 8, -12, -8),
            Qt.TextFlag.TextWordWrap,
            str(self._model.get("purpose", "")),
        )
        self._remember(purpose_rect, "program")

        nodes = tuple(self._model.get("nodes", ()))
        node_gap = 16.0
        nodes_top = purpose_rect.bottom() + 18
        node_height = 108.0
        node_width = (
            self.width() - margin * 2 - node_gap * max(0, len(nodes) - 1)
        ) / max(1, len(nodes))
        node_rects = []
        for index, node in enumerate(nodes):
            rect = QRectF(
                margin + index * (node_width + node_gap),
                nodes_top,
                node_width,
                node_height,
            )
            node_rects.append(rect)
            self._draw_panel(
                painter,
                rect,
                str(node.get("title", "")),
                str(node.get("detail", "")),
                str(node.get("tone", "neutral")),
                str(node.get("action", "")),
            )
        for left, right in zip(node_rects, node_rects[1:]):
            self._draw_arrow(painter, left, right)

        resources_top = nodes_top + node_height + 18
        resource_rect = QRectF(margin, resources_top, self.width() - margin * 2, 83)
        self._draw_resource_strip(
            painter,
            resource_rect,
            tuple(self._model.get("resources", ())),
        )
        feature_rect = QRectF(
            margin,
            resource_rect.bottom() + 12,
            self.width() - margin * 2,
            max(58.0, self.height() - resource_rect.bottom() - margin - 12),
        )
        self._draw_feature_chips(
            painter,
            feature_rect,
            tuple(self._model.get("features", ())),
            str(self._model.get("feature_action", "state")),
        )

    def _action_at(self, point: QPointF) -> str:
        for rect, action in reversed(self._regions):
            if rect.contains(point):
                return action
        return ""

    def mouseMoveEvent(self, event) -> None:
        action = self._action_at(event.position())
        if action != self._hover_action:
            self._hover_action = action
            self.setCursor(
                Qt.CursorShape.PointingHandCursor
                if action
                else Qt.CursorShape.ArrowCursor
            )
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        if self._hover_action:
            self._hover_action = ""
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            action = self._action_at(event.position())
            if action:
                self.action_requested.emit(action)
                event.accept()
                return
        super().mouseReleaseEvent(event)


class SdfProgramOverview(QScrollArea):
    navigate_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._document = None
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        self.setWidget(content)
        root = QVBoxLayout(content)

        self.title = QLabel(self.tr("Select a program"))
        title_font = QFont(self.title.font())
        title_font.setBold(True)
        title_font.setPointSize(max(title_font.pointSize() + 4, 14))
        self.title.setFont(title_font)
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.title)

        self.subtitle = QLabel()
        self.subtitle.setWordWrap(True)
        root.addWidget(self.subtitle)
        self.status = QLabel()
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self.canvas = PassBehaviorCanvas()
        self.canvas.action_requested.connect(self.navigate_requested)
        root.addWidget(self.canvas, 1)
        hint = QLabel(
            self.tr(
                "Select any visual section to open the matching authoring controls. "
                "Exact reflection and assembly remain under Advanced."
            )
        )
        hint.setWordWrap(True)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(hint)
        root.addStretch(1)

    def set_document(self, document) -> None:
        self._document = document

    def show_file_summary(self, document) -> None:
        self._document = document
        self.title.setText(
            self.tr("{profile} shader library").format(profile=document.profile.display_name)
        )
        self.subtitle.setText(
            self.tr("{kind} · {passes:,} target-specific passes").format(
                kind=self.tr("Shader definition"),
                passes=document.header.record_count,
            )
        )
        note_count = len(document.notes)
        self.status.setText(
            self.tr("Choose a pass to start authoring.")
            if not note_count
            else self.tr("{count} compatibility notes need review under Advanced.").format(
                count=note_count
            )
        )
        self.canvas.set_model(
            {
                "purpose": self.tr(
                    "Choose a pass to visualize how geometry, shaders, resources, and outputs work together."
                ),
                "nodes": (
                    {
                        "title": self.tr("Pass library"),
                        "detail": self.tr("{count} logical passes").format(
                            count=len({p.name for target in document.targets for p in target.programs})
                        ),
                        "tone": "shader",
                        "action": "",
                    },
                    {
                        "title": self.tr("GPU variants"),
                        "detail": self.tr("{count} compiled hardware variants").format(
                            count=document.header.target_count
                        ),
                        "tone": "input",
                        "action": "",
                    },
                ),
                "resources": (),
                "features": (self.tr("Select a pass on the left"),),
                "feature_action": "",
            }
        )

    def set_program(self, program: SdfProgram) -> None:
        document = self._document
        present_stages = tuple(
            shader.role for shader in program.shaders if shader.present
        )
        compute_only = "compute" in present_stages and not any(
            stage != "compute" for stage in present_stages
        )
        library = program.raytracing_library
        kind = self.tr("Ray-tracing library") if library else self.tr("Compute pass") if compute_only else self.tr("Graphics pass")
        target_name = next(
            (
                target.display_name
                for target in document.targets
                if target.index == program.target_index
            ),
            self.tr("Target {index}").format(index=program.target_index),
        )
        self.title.setText(program.name)
        self.subtitle.setText(f"{kind} · {_human(program.primitive_topology)} · {target_name}")

        note_count = len(document.notes)
        if note_count:
            self.status.setText(
                self.tr("Needs review · {count} compatibility notes under Advanced").format(
                    count=note_count
                )
            )
        else:
            self.status.setText(
                self.tr("Ready to author · ray-tracing graphs and compiled shader replacement") if library else
                self.tr("Ready to author · all stored pass behavior is decoded")
            )

        if library:
            purpose = self.tr("Traces rays through the library's named exports using DispatchRays.")
        elif compute_only:
            purpose = self.tr(
                "Processes data in {groups} thread groups without drawing geometry."
            ).format(groups=" × ".join(map(str, program.compute_group_size)))
        elif "pixel" not in present_stages:
            purpose = self.tr(
                "Processes geometry without a pixel-shading stage, so it does not produce a normal shaded color pass."
            )
        else:
            purpose = self.tr(
                "Draws {topology} geometry through the active graphics stages."
            ).format(topology=_human(program.primitive_topology))

        if library:
            inputs = self.tr("Ray-generation export: {name}. Dispatch dimensions are supplied at runtime.").format(name=library.raygen)
        elif compute_only:
            inputs = self.tr("Dispatch size: {groups} threads per group.").format(
                groups=" × ".join(map(str, program.compute_group_size))
            )
        elif program.input_layout is not None:
            elements = []
            for element in program.input_layout.elements:
                suffix = str(element.semantic_index) if element.semantic_index else ""
                elements.append(f"{element.semantic}{suffix}")
            inputs = self.tr("{count} vertex inputs: {names}.\nPrimitive: {topology}.").format(
                count=len(program.input_layout.elements),
                names=_joined(elements) or self.tr("none"),
                topology=_human(program.primitive_topology),
            )
            if any(element.per_instance for element in program.input_layout.elements):
                inputs += self.tr(" Instance data is consumed per draw instance.")
        else:
            inputs = self.tr("This pass does not store a vertex input layout.")
        stage_names = [stage_display_name(stage) for stage in present_stages]

        counts = tuple(program.distinct_resource_counts)
        resource_names = []
        resource_names.extend(buffer.name for buffer in program.constant_buffers)
        for group in program.resource_metadata[1:]:
            resource_names.extend(resource.name for resource in group)
        features = []
        if not compute_only:
            features.append(_human(program.primitive_topology))
            if program.rasterizer_state is not None:
                features.append(
                    self.tr("{mode} culling").format(
                        mode=_human(program.rasterizer_state.cull_mode)
                    )
                )
            if program.depth_stencil_state is not None:
                depth = program.depth_stencil_state
                features.append(
                    self.tr("depth writes")
                    if depth.depth_enable and depth.depth_write_mask == "all"
                    else self.tr("depth read-only")
                    if depth.depth_enable
                    else self.tr("no depth test")
                )
            if program.blend_state is not None and any(
                target.blend_enable for target in program.blend_state.render_targets
            ):
                features.append(self.tr("color blending"))

        resource_visuals = []
        visual_labels = {
            "constant_buffer": self.tr("Data buffers"),
            "sampler": self.tr("Samplers"),
            "shader_resource": self.tr("Textures / reads"),
            "unordered_access": self.tr("Writable"),
            "optional_resource": self.tr("Optional"),
        }
        for kind, count in zip(RESOURCE_KINDS, counts):
            if count:
                resource_visuals.append((visual_labels[kind], count))

        if compute_only:
            nodes = (
                {
                    "title": self.tr("Data"),
                    "detail": _joined(resource_names, limit=4) or self.tr("Declared runtime inputs"),
                    "tone": "resource",
                    "action": "resources",
                },
                {
                    "title": self.tr("DXR library") if library else self.tr("Compute"),
                    "detail": library.raygen if library else _joined(stage_names) or self.tr("No active shader"),
                    "tone": "compute",
                    "action": "shaders",
                },
                {
                    "title": self.tr("DispatchRays") if library else self.tr("Dispatch"),
                    "detail": self.tr("Runtime ray dimensions") if library else self.tr("{groups} threads per group").format(
                        groups=" × ".join(map(str, program.compute_group_size))
                    ),
                    "tone": "state",
                    "action": "program",
                },
                {
                    "title": self.tr("Result"),
                    "detail": self.tr("Ray-tracing output") if library else self.tr("Compute output"),
                    "tone": "output",
                    "action": "resources",
                },
            )
        else:
            raster = program.rasterizer_state
            depth = program.depth_stencil_state
            blend = program.blend_state
            blend_count = (
                sum(target.blend_enable for target in blend.render_targets)
                if blend is not None
                else 0
            )
            output_detail = self.tr("Color and depth targets")
            if "pixel" not in present_stages:
                output_detail = self.tr("Geometry / depth only")
            elif blend_count:
                output_detail += self.tr(" · {count} blended targets").format(
                    count=blend_count
                )
            nodes = (
                {
                    "title": self.tr("Geometry"),
                    "detail": inputs,
                    "tone": "input",
                    "action": "input",
                },
                {
                    "title": self.tr("Shaders"),
                    "detail": " → ".join(stage_names) or self.tr("No active stages"),
                    "tone": "shader",
                    "action": "shaders",
                },
                {
                    "title": self.tr("Raster"),
                    "detail": (
                        self.tr("{fill} · {cull} culling").format(
                            fill=_human(raster.fill_mode),
                            cull=_human(raster.cull_mode),
                        )
                        if raster is not None
                        else self.tr("No rasterizer preset")
                    ),
                    "tone": "state",
                    "action": "state",
                },
                {
                    "title": self.tr("Depth"),
                    "detail": (
                        self.tr("{test} · {write}").format(
                            test=_human(depth.depth_function) if depth.depth_enable else self.tr("disabled"),
                            write=self.tr("write") if depth.depth_write_mask == "all" else self.tr("read only"),
                        )
                        if depth is not None
                        else self.tr("No depth preset")
                    ),
                    "tone": "state",
                    "action": "state",
                },
                {
                    "title": self.tr("Output"),
                    "detail": output_detail,
                    "tone": "output",
                    "action": "state",
                },
            )

        self.canvas.set_model(
            {
                "purpose": purpose,
                "nodes": nodes,
                "resources": tuple(resource_visuals),
                "features": tuple(dict.fromkeys(features)),
                "feature_action": "state",
            }
        )


__all__ = ["SdfProgramOverview"]
