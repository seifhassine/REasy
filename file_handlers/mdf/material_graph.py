from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from PySide6.QtCore import QT_TRANSLATE_NOOP, QCoreApplication, QPointF, QRectF, Qt, QTimer, Signal
lazy from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QDoubleSpinBox,
    QFrame,
    QGraphicsPathItem,
    QGraphicsProxyWidget,
    QGraphicsScene,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

lazy from file_handlers.material_names import is_private_material_parameter, resolve_material_name
lazy from file_handlers.mmtr.material_usage import InputUsage, MaterialInputUsage, material_input_usage
lazy from file_handlers.sdf.models import humanize_identifier
lazy from file_handlers.sdf.shader_graph_editor import _ShaderGraphView, connection_path
lazy from ui.editor_widgets import editor_strip, strip_button, styled_label
lazy from ui.styles import current_colors, set_style_role
lazy from .material_layout import MaterialDescriptorLayout
lazy from .mdf_file import MATERIAL_FLAG_NAMES

_READABLE_SCALE = 0.75
_BEHAVIOR_FLAGS = (
    (QT_TRANSLATE_NOOP("MdfMaterialGraph", "Casts shadows"), True, "ShadowCastDisable"),
    (QT_TRANSLATE_NOOP("MdfMaterialGraph", "Emission feature"), False, "EmissiveUsed"),
    (QT_TRANSLATE_NOOP("MdfMaterialGraph", "Tessellation"), False, "TessellationEnable"),
    (QT_TRANSLATE_NOOP("MdfMaterialGraph", "Ignore scene depth"), False, "EnableIgnoreDepth"),
    (QT_TRANSLATE_NOOP("MdfMaterialGraph", "Alpha mask"), False, "AlphaMaskUsed"),
    (QT_TRANSLATE_NOOP("MdfMaterialGraph", "Base two-sided route"), False, "BaseTwoSideEnable"),
    (QT_TRANSLATE_NOOP("MdfMaterialGraph", "Base alpha-test route"), False, "BaseAlphaTestEnable"),
    (QT_TRANSLATE_NOOP("MdfMaterialGraph", "Force two-sided"), False, "ForcedTwoSideEnable"),
    (QT_TRANSLATE_NOOP("MdfMaterialGraph", "Two-sided output"), False, "TwoSideEnable"),
)

_FRIENDLY_NAMES = {
    "sidecolor": "Edge Tint",
    "fake_blendcolor": "Ice Reflection Tint",
    "basecolor": "Base Color",
    "emissive_intensity": "Emission Strength",
    "detoneemissive_intensity": "Detone Emission Strength",
    "detoneemissive_rate": "Detone Emission Mix",
    "sidecolorrate": "Edge Tint Strength",
    "fake_contrast": "Reflection Contrast",
    "fake_luminosity": "Reflection Brightness",
    "fake_refractivity": "Refraction Strength",
    "uvtiliing": "UV Tiling",
    "usesecondaryuvformask": "Use Secondary UV for Mask",
    "metallic": "Metalness",
    "roughness": "Roughness",
    "translucency": "Translucency",
    "occlucion": "Ambient Occlusion",
    "alphavalue": "Alpha Multiplier",
    "dissolveoffset": "Dissolve Threshold",
    "basemetalmap": "Base / Metal Texture",
    "normalroughnessmap": "Normal / Roughness Texture",
    "fakecubemap": "Ice Reflection Cubemap",
}


def friendly_material_name(name: str) -> str:
    source = str(name or "")
    return _FRIENDLY_NAMES.get(source.casefold()) or humanize_identifier(source) or "Unnamed input"


def parameter_group(name: str) -> str:
    lowered = str(name or "").casefold()
    if "emiss" in lowered or "glow" in lowered:
        return "Emission"
    if any(token in lowered for token in ("fake", "refract", "reflect", "luminos")):
        return "Ice and reflection"
    if any(token in lowered for token in ("dissolve", "alpha", "opacity", "transluc")):
        return "Transparency"
    if any(token in lowered for token in ("uv", "tiling", "scroll", "mapping")):
        return "Texture mapping"
    if any(token in lowered for token in ("rough", "metal", "specular", "gloss", "occl")):
        return "Surface response"
    if any(token in lowered for token in ("color", "tint", "albedo", "diffuse")):
        return "Surface color"
    return "Additional controls"


def _is_texture_contract(resource) -> bool:
    if getattr(resource, "resource_kind", "") not in {
        "shader_resource",
        "optional_resource",
    }:
        return False
    return any(
        "texture" in str(type_name).casefold()
        for type_name in getattr(resource, "type_names", ())
    )


@dataclass(frozen=True, slots=True)
class MaterialContractMatch:
    missing_parameters: tuple[str, ...] = ()
    unmatched_parameters: tuple[str, ...] = ()
    missing_textures: tuple[str, ...] = ()
    unmatched_textures: tuple[str, ...] = ()
    layout_mismatches: tuple[str, ...] = ()
    resolved: bool = False

    @property
    def verified(self) -> bool:
        return self.resolved and not (self.missing_parameters or self.unmatched_parameters
                                      or self.layout_mismatches or self.missing_textures or self.unmatched_textures)


def match_material_contract(material, document) -> MaterialContractMatch:
    if document is None:
        return MaterialContractMatch()
    parameters = tuple(
        parameter for parameter in material.parameters if not is_private_material_parameter(parameter.name)
    )
    textures = tuple(material.textures)

    interface = document.material_interface
    usage = material_input_usage(document)
    master_parameters = tuple(
        parameter
        for parameter in interface.parameters
        if not is_private_material_parameter(parameter.name)
    )
    by_parameter_name: dict[str, list[object]] = {}
    for parameter in master_parameters:
        by_parameter_name.setdefault(parameter.name.casefold(), []).append(parameter)

    unmatched_parameters = []
    layout_mismatches = []
    present_master_names = set()
    for parameter in parameters:
        candidates = by_parameter_name.get(str(parameter.name).casefold(), ())
        if not candidates:
            unmatched_parameters.append(parameter.name)
            continue
        present_master_names.update(candidate.name.casefold() for candidate in candidates)
        expected_size = max(1, int(parameter.component_count)) * 4
        exact = next(
            (
                candidate
                for candidate in candidates
                if int(candidate.byte_offset) == int(parameter.param_rel_offset)
                and int(candidate.byte_size) == expected_size
            ),
            None,
        )
        if exact is None:
            layout_mismatches.append(parameter.name)

    master_resources = tuple(document.profile.shader_graph_profile.editable_textures(document))
    resource_names = {
        resource.name for resource in interface.resources if _is_texture_contract(resource)
    } | {resource.name for resource in master_resources}
    present_texture_names = set()
    unmatched_textures = []
    for texture in textures:
        resolved = resolve_material_name(str(texture.tex_type), resource_names)
        if resolved is not None:
            present_texture_names.add(resolved)
        else:
            unmatched_textures.append(texture.tex_type)

    return MaterialContractMatch(
        missing_parameters=tuple(
            parameter.name
            for parameter in master_parameters
            if parameter.name.casefold() not in present_master_names
            and usage.parameter(parameter.byte_offset, parameter.byte_size) is not InputUsage.UNUSED
        ),
        unmatched_parameters=tuple(unmatched_parameters),
        missing_textures=tuple(
            resource.name
            for resource in master_resources
            if resource.name not in present_texture_names
            and usage.texture(resource.name) is not InputUsage.UNUSED
        ),
        unmatched_textures=tuple(unmatched_textures),
        layout_mismatches=tuple(layout_mismatches),
        resolved=True,
    )


def unit_value(value) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except Exception:
        return 0.0


def _material_color(values) -> QColor:
    return QColor.fromRgbF(*(unit_value(value) for value in (list(values) + [1.0] * 4)[:4]))


def is_color_parameter(parameter) -> bool:
    return "color" in (parameter.name or "").lower() and parameter.component_count in (3, 4)


def layer_color_channel(name: str) -> tuple[bool, int | None]:
    lowered = (name or "").lower()
    if not lowered.startswith("layercolor_"):
        return False, None
    if "red" in lowered:
        return True, 0
    if "green" in lowered:
        return True, 1
    if "blue" in lowered:
        return True, 2
    return True, None


def _picked_color(parent: QWidget, initial: QColor, alpha: bool) -> QColor | None:
    color = QColorDialog.getColor(
        initial,
        parent,
        QCoreApplication.translate("MdfMaterialGraph", "Choose material color"),
        QColorDialog.ColorDialogOption.ShowAlphaChannel if alpha else QColorDialog.ColorDialogOption(0),
    )
    return color if color.isValid() else None


def pick_parameter_color(parent: QWidget, material, index: int) -> bool:
    parameters = material.parameters
    parameter = parameters[index]
    if layer_color_channel(parameter.name)[0]:
        start = end = index
        while start > 0 and layer_color_channel(parameters[start - 1].name)[0]:
            start -= 1
        while end + 1 < len(parameters) and layer_color_channel(parameters[end + 1].name)[0]:
            end += 1
        run = [(layer, channel) for layer in parameters[start:end + 1]
               if (channel := layer_color_channel(layer.name)[1]) is not None]
        channels = [1.0, 1.0, 1.0]
        for layer, channel in run:
            channels[channel] = float(layer.parameter[0])
        picked = _picked_color(parent, QColor.fromRgbF(*(unit_value(value) for value in channels)), False)
        if picked is None:
            return False
        rgb = (picked.redF(), picked.greenF(), picked.blueF())
        for layer, channel in run:
            layer.parameter = (rgb[channel], 0.0, 0.0, 0.0)
        return True
    if not is_color_parameter(parameter):
        return False
    alpha = parameter.component_count == 4
    red, green, blue, opacity = parameter.parameter
    picked = _picked_color(parent, QColor.fromRgbF(
        unit_value(red), unit_value(green), unit_value(blue), unit_value(opacity if alpha else 1.0)), alpha)
    if picked is None:
        return False
    parameter.parameter = (picked.redF(), picked.greenF(), picked.blueF(), picked.alphaF() if alpha else 0.0)
    return True


class MovableMaterialGraphNode(QGraphicsProxyWidget):
    def __init__(self, moved_callback):
        super().__init__()
        self._moved_callback = moved_callback
        self.setFlag(self.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(self.GraphicsItemFlag.ItemSendsGeometryChanges, True)

    def itemChange(self, change, value):
        result = super().itemChange(change, value)
        if change == self.GraphicsItemChange.ItemPositionHasChanged:
            self._moved_callback()
        return result


class MaterialGraphEdge(QGraphicsPathItem):
    def __init__(
        self,
        source: QGraphicsProxyWidget,
        target: QGraphicsProxyWidget,
        color: QColor,
        *,
        target_fraction: float = 0.5,
        width: float = 2.1,
    ):
        super().__init__()
        self.source = source
        self.target = target
        self.target_fraction = float(target_fraction)
        self.setPen(QPen(color, width))
        self.update_path()

    def update_path(self) -> None:
        source = self.source.sceneBoundingRect()
        target = self.target.sceneBoundingRect()
        start = QPointF(
            source.right(),
            source.top() + source.height() * 0.5,
        )
        end = QPointF(
            target.left(),
            target.top() + target.height() * self.target_fraction,
        )
        self.setPath(connection_path(start, end, 75.0))


def _node_stylesheet(accent: str) -> str:
    c = current_colors()
    return (
        f"QFrame#materialGraphNode {{ background:{c['node_bg']}; border:1px solid {c['node_border']};"
        f" border-top:3px solid {accent}; border-radius:4px; }}"
        " .QWidget { background:transparent; }"
        f" QLabel#materialGraphNodeTitle {{ background:{c['node_header']}; color:{c['text_bright']};"
        f" border:none; border-bottom:1px solid {c['node_border']}; padding:5px 8px; font-weight:600; }}"
        f" QLabel#materialGraphNodeSubtitle {{ color:{c['text_subtle']}; }}"
        f" QLabel#materialGraphNote {{ color:{c['warning']}; }}"
        f" QLabel#materialGraphPreview {{ background:{c['canvas_bg']}; border:1px solid {c['border_subtle']};"
        f" border-radius:3px; color:{c['text_subtle']}; }}"
    )


class MaterialGraphNodeFrame(QFrame):
    def __init__(self, title: str, subtitle: str, accent: str, width: int):
        super().__init__()
        self.setObjectName("materialGraphNode")
        self.setMinimumWidth(width)
        self.setMaximumWidth(width)
        self.setStyleSheet(_node_stylesheet(accent))
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        title_label = QLabel(title)
        title_label.setObjectName("materialGraphNodeTitle")
        root.addWidget(title_label)
        self.content = QVBoxLayout()
        self.content.setContentsMargins(8, 6, 8, 8)
        self.content.setSpacing(6)
        root.addLayout(self.content)
        if subtitle:
            subtitle_label = QLabel(subtitle)
            subtitle_label.setObjectName("materialGraphNodeSubtitle")
            subtitle_label.setWordWrap(True)
            self.content.addWidget(subtitle_label)

    def add_control(self, label: str, control: QWidget, tooltip: str) -> None:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        name = QLabel(label)
        name.setMinimumWidth(126)
        name.setWordWrap(True)
        name.setToolTip(tooltip)
        control.setToolTip(tooltip)
        layout.addWidget(name, 1)
        layout.addWidget(control)
        self.content.addWidget(row)

    def add_mismatch(self, title: str, names: tuple[str, ...]) -> None:
        if not names:
            return
        warning = QLabel(
            f"{title}\n" + "\n".join(f"• {name}" for name in names)
        )
        warning.setObjectName("materialContractMismatch")
        warning.setWordWrap(True)
        warning.setStyleSheet(
            "QLabel#materialContractMismatch {"
            f" color:{current_colors()['danger']}; font-weight:600; }}"
        )
        self.content.addWidget(warning)

class MdfMaterialGraph(QWidget):
    material_changed = Signal(str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._material = None
        self._texture_preview_provider = None
        self._match = MaterialContractMatch()
        self._input_usage = MaterialInputUsage()
        self.parameter_editors: dict[str, tuple[QWidget, ...]] = {}
        self.parameter_runtime_editors: dict[str, QCheckBox] = {}
        self.texture_editors: dict[str, QLineEdit] = {}
        self.texture_runtime_editors: dict[str, QCheckBox] = {}
        self.behavior_editors: dict[str, QCheckBox] = {}
        self._edges: list[QGraphicsPathItem] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        header, header_row = editor_strip()
        title = styled_label(self.tr("Material Graph"), heading="panel")
        header_row.addWidget(title)
        header_row.addSpacing(6)
        self.status = styled_label(badge="neutral")
        header_row.addWidget(self.status)
        header_row.addStretch(1)
        fit = strip_button(QPushButton(self.tr("Fit graph")), "fit")
        zoom_out = strip_button(QPushButton(), "zoom_out", tooltip=self.tr("Zoom out"))
        zoom_in = strip_button(QPushButton(), "zoom_in", tooltip=self.tr("Zoom in"))
        fit.clicked.connect(self.fit_graph)
        zoom_out.clicked.connect(lambda: self.view.scale(0.85, 0.85))
        zoom_in.clicked.connect(lambda: self.view.scale(1.18, 1.18))
        header_row.addWidget(fit)
        header_row.addWidget(zoom_out)
        header_row.addWidget(zoom_in)
        root.addWidget(header)

        self.scene = QGraphicsScene(self)
        self.view = _ShaderGraphView()
        self.view.setMinimumSize(0, 240)
        self.view.setScene(self.scene)
        root.addWidget(self.view, 1)
        # A child timer dies with the graph; ``QTimer.singleShot`` would run on a deleted graph.
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.setInterval(0)
        self._fit_timer.timeout.connect(self._show_initial_view)

    def set_material(
        self,
        material,
        document=None,
        *,
        texture_preview_provider=None,
    ) -> None:
        self._material = material
        self._texture_preview_provider = texture_preview_provider
        self._match = match_material_contract(material, document) if material is not None else MaterialContractMatch()
        try:
            layout = MaterialDescriptorLayout.from_material(material) if material is not None else None
            self._input_usage = material_input_usage(document, descriptor_layout=layout)
        except ValueError:
            self._input_usage = MaterialInputUsage()
        self.rebuild()

    def rebuild(self) -> None:
        self.scene.clear()
        self._edges.clear()
        self.parameter_editors.clear()
        self.parameter_runtime_editors.clear()
        self.texture_editors.clear()
        self.texture_runtime_editors.clear()
        self.behavior_editors.clear()
        material = self._material
        if material is None:
            self.status.show()
            self.status.setText(self.tr("No material selected"))
            set_style_role(self.status, badge="neutral")
            self.status.setToolTip("")
            self.scene.setSceneRect(QRectF(0, 0, 900, 500))
            return

        self._update_status()
        parameter_groups: dict[str, list[tuple[int, object]]] = {}
        for index, parameter in enumerate(material.parameters):
            if is_private_material_parameter(parameter.name):
                continue
            parameter_groups.setdefault(parameter_group(parameter.name), []).append(
                (index, parameter)
            )

        texture_nodes = []
        texture_y = 20.0
        for index, texture in enumerate(material.textures):
            node = self._texture_node(index, texture)
            proxy = self._add_node(node, 20.0, texture_y, ("texture", texture.tex_type))
            texture_nodes.append(proxy)
            texture_y += proxy.boundingRect().height() + 22.0

        group_order = (
            "Surface color",
            "Surface response",
            "Ice and reflection",
            "Transparency",
            "Emission",
            "Texture mapping",
            "Additional controls",
        )
        control_nodes = []
        control_y = 20.0
        behavior = self._behavior_node(material)
        behavior_proxy = self._add_node(
            behavior,
            390.0,
            control_y,
            ("behavior", "material_flags"),
        )
        control_nodes.append(behavior_proxy)
        control_y += behavior_proxy.boundingRect().height() + 22.0
        for group in group_order:
            members = parameter_groups.get(group)
            if not members:
                continue
            node = self._parameter_node(group, members)
            proxy = self._add_node(node, 390.0, control_y, ("parameter_group", group))
            control_nodes.append(proxy)
            control_y += proxy.boundingRect().height() + 22.0

        content_height = max(texture_y, control_y, 620.0)
        master = self._master_node(material)
        master_proxy = self._add_node(
            master,
            790.0,
            max(20.0, content_height * 0.5 - master.sizeHint().height() * 0.5),
            ("master", material.header.mmtr_path),
        )
        output = self._output_node()
        output_proxy = self._add_node(
            output,
            1190.0,
            max(20.0, content_height * 0.5 - output.sizeHint().height() * 0.5),
            ("outputs", "renderer"),
        )

        sources = texture_nodes + control_nodes
        for index, source in enumerate(sources):
            fraction = (index + 1) / (len(sources) + 1)
            self._connect(
                source,
                master_proxy,
                QColor("#5fa7c8") if source in texture_nodes else QColor("#a980cb"),
                target_fraction=fraction,
            )
        self._connect(
            master_proxy,
            output_proxy,
            QColor("#75c69a"),
            width=3.0,
        )

        bounds = self.scene.itemsBoundingRect().adjusted(-55, -55, 55, 55)
        self.scene.setSceneRect(bounds)
        self._fit_timer.start()

    def _update_status(self) -> None:
        match = self._match
        if not match.resolved:
            text = self.tr("Master material not found")
            tone = "warning"
        elif match.verified:
            self.status.clear()
            self.status.setToolTip("")
            self.status.hide()
            return
        else:
            text = self.tr("Material inputs do not match the master material")
            tone = "danger"
        self.status.show()
        self.status.setText(text)
        set_style_role(self.status, badge=tone)
        self.status.setToolTip("")

    def _add_node(self, widget: QWidget, x: float, y: float, tag) -> QGraphicsProxyWidget:
        widget.adjustSize()
        proxy = MovableMaterialGraphNode(self._update_edges)
        proxy.setWidget(widget)
        self.scene.addItem(proxy)
        proxy.setPos(x, y)
        proxy.setZValue(2)
        proxy.setData(0, tag)
        return proxy

    def _connect(self, source: QGraphicsProxyWidget, target: QGraphicsProxyWidget, color: QColor, **options) -> None:
        edge = MaterialGraphEdge(source, target, color, **options)
        self.scene.addItem(edge)
        self._edges.append(edge)

    def _update_edges(self) -> None:
        for edge in self._edges:
            edge.update_path()

    def _add_usage_label(self, layout, usage: InputUsage) -> None:
        if usage is InputUsage.USED:
            return
        label = QLabel(usage.label)
        label.setObjectName("materialGraphNote")
        label.setWordWrap(True)
        label.setToolTip(usage.explanation)
        layout.addWidget(label)

    def _parameter_node(self, group: str, members) -> MaterialGraphNodeFrame:
        node = MaterialGraphNodeFrame(
            group,
            "",
            "#c6a0e8",
            355,
        )
        for index, parameter in members:
            control, editors = self._parameter_control(index, parameter)
            self.parameter_editors[parameter.name] = tuple(editors)
            host = QWidget()
            layout = QVBoxLayout(host)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(3)
            layout.addWidget(control)
            self._add_usage_label(layout, self._input_usage.parameter(
                parameter.param_rel_offset, parameter.component_count * 4))
            runtime_editable = QCheckBox(self.tr("Allow runtime changes"))
            runtime_editable.setChecked(not bool(parameter.component_locked))
            runtime_editable.setToolTip(
                self.tr("Prevent the game from overriding this saved value.")
            )
            runtime_editable.toggled.connect(
                lambda enabled, i=index, p=parameter: self._set_parameter_runtime_editable(
                    i,
                    p,
                    enabled,
                )
            )
            self.parameter_runtime_editors[parameter.name] = runtime_editable
            layout.addWidget(runtime_editable)
            source = self.tr("MDF parameter: {name}").format(name=parameter.name)
            node.add_control(friendly_material_name(parameter.name), host, source)
        return node

    def _behavior_node(self, material) -> MaterialGraphNodeFrame:
        node = MaterialGraphNodeFrame(
            self.tr("Rendering behavior"),
            "",
            "#d99173",
            355,
        )
        flags = int(getattr(material.header, "material_flags", 0))
        host = QWidget()
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(5)
        for flag_index, (label, inverted, source_name) in enumerate(_BEHAVIOR_FLAGS):
            bit = MATERIAL_FLAG_NAMES.index(source_name)
            checked = bool(flags & (1 << bit))
            if inverted:
                checked = not checked
            checkbox = QCheckBox(self.tr(label))
            checkbox.setChecked(checked)
            checkbox.toggled.connect(
                lambda enabled, b=bit, inv=inverted: self._set_behavior_flag(
                    b,
                    enabled,
                    inverted=inv,
                )
            )
            grid.addWidget(checkbox, flag_index // 2, flag_index % 2)
            self.behavior_editors[source_name] = checkbox
        node.content.addWidget(host)
        return node

    def _set_behavior_flag(self, bit: int, enabled: bool, *, inverted: bool) -> None:
        if self._material is None:
            return
        stored = not bool(enabled) if inverted else bool(enabled)
        mask = 1 << int(bit)
        flags = int(self._material.header.material_flags)
        self._material.header.material_flags = (
            flags | mask if stored else flags & ~mask
        )
        self.material_changed.emit("header", int(bit))

    def _parameter_control(self, index: int, parameter):
        count = max(1, min(4, int(parameter.component_count)))
        values = list(parameter.parameter[:count])
        lowered = str(parameter.name).casefold()
        if is_color_parameter(parameter):
            button = QPushButton()
            self._refresh_color_button(button, values)
            button.clicked.connect(
                lambda _checked=False, i=index, p=parameter, b=button: self._choose_parameter_color(i, p, b)
            )
            return button, (button,)
        if count == 1 and lowered.startswith(("use", "enable")):
            checkbox = QCheckBox(self.tr("Enabled"))
            checkbox.setChecked(bool(values[0]))
            checkbox.toggled.connect(
                lambda checked, i=index, p=parameter: self._set_parameter_component(i, p, 0, 1.0 if checked else 0.0)
            )
            return checkbox, (checkbox,)
        if count == 1:
            editor = self._number_editor(parameter.name, values[0])
            editor.valueChanged.connect(
                lambda value, i=index, p=parameter: self._set_parameter_component(i, p, 0, value)
            )
            return editor, (editor,)

        host = QWidget()
        layout = QHBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        editors = []
        for component, value in enumerate(values):
            editor = self._number_editor(parameter.name, value, compact=True)
            editor.valueChanged.connect(
                lambda changed, c=component, i=index, p=parameter: self._set_parameter_component(i, p, c, changed)
            )
            layout.addWidget(editor)
            editors.append(editor)
        return host, tuple(editors)

    @staticmethod
    def _number_editor(name: str, value: float, *, compact: bool = False) -> QDoubleSpinBox:
        editor = QDoubleSpinBox()
        editor.setDecimals(4)
        editor.setKeyboardTracking(False)
        lowered = str(name).casefold()
        if any(token in lowered for token in ("rough", "metal", "occl", "transluc", "rate")):
            editor.setRange(0.0, 1.0)
            editor.setSingleStep(0.02)
        elif any(token in lowered for token in ("til", "intensity", "luminos", "contrast", "refract", "alpha")):
            editor.setRange(0.0, 100.0)
            editor.setSingleStep(0.05)
        else:
            editor.setRange(-100000.0, 100000.0)
            editor.setSingleStep(0.05)
        editor.setValue(float(value))
        editor.setMinimumWidth(72 if compact else 92)
        return editor

    def _set_parameter_component(self, index: int, parameter, component: int, value: float) -> None:
        current = list(parameter.parameter)
        while len(current) < 4:
            current.append(0.0)
        current[int(component)] = float(value)
        parameter.parameter = tuple(current[:4])
        self.material_changed.emit("parameter", int(index))

    def _set_parameter_runtime_editable(
        self,
        index: int,
        parameter,
        enabled: bool,
    ) -> None:
        parameter.component_locked = 0 if enabled else 1
        self.material_changed.emit("parameter", int(index))

    @staticmethod
    def _refresh_color_button(button: QPushButton, values) -> None:
        channels = (list(values) + [1.0] * 4)[:4]
        color = _material_color(channels)
        button.setText("  ".join(f"{float(value):.2f}" for value in channels))
        text_color = "#11161a" if color.lightnessF() > 0.58 else "#f5f7f8"
        button.setStyleSheet(
            "QPushButton {"
            f"background:{color.name(QColor.NameFormat.HexArgb)}; color:{text_color};"
            f"border:1px solid {current_colors()['border_strong']}; border-radius:3px; padding:3px 6px;"
            "}"
        )

    def _choose_parameter_color(self, index: int, parameter, button: QPushButton) -> None:
        if not pick_parameter_color(self, self._material, index):
            return
        self._refresh_color_button(button, parameter.parameter[:parameter.component_count])
        self.material_changed.emit("parameter", int(index))

    def _texture_node(self, index: int, texture) -> MaterialGraphNodeFrame:
        node = MaterialGraphNodeFrame(
            friendly_material_name(texture.tex_type),
            texture.tex_type,
            "#78b9d7",
            330,
        )
        self._add_usage_label(node.content, self._input_usage.texture(texture.tex_type))
        preview = QLabel()
        preview.setObjectName("materialGraphPreview")
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview.setMinimumHeight(88)
        pixmap = self._texture_preview_provider(texture) if callable(self._texture_preview_provider) else None
        if isinstance(pixmap, str):
            preview.setToolTip(pixmap)
            pixmap = None
        if pixmap is not None and not pixmap.isNull():
            preview.setPixmap(
                pixmap.scaled(
                    290,
                    110,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            preview.setText(self.tr("No preview"))
        node.content.addWidget(preview)
        path = QLineEdit(texture.tex_path)
        path.setClearButtonEnabled(True)
        path.editingFinished.connect(
            lambda i=index, t=texture, editor=path: self._set_texture_path(i, t, editor.text())
        )
        node.content.addWidget(path)
        runtime_editable = QCheckBox(self.tr("Allow runtime changes"))
        runtime_editable.setChecked(not bool(texture.locked))
        runtime_editable.setToolTip(
            self.tr("Prevent the game from overriding this saved texture.")
        )
        runtime_editable.toggled.connect(
            lambda enabled, i=index, t=texture: self._set_texture_runtime_editable(
                i,
                t,
                enabled,
            )
        )
        self.texture_runtime_editors[texture.tex_type] = runtime_editable
        node.content.addWidget(runtime_editable)
        self.texture_editors[texture.tex_type] = path
        return node

    def _set_texture_path(self, index: int, texture, value: str) -> None:
        changed = str(value) != str(texture.tex_path)
        texture.tex_path = str(value)
        if changed:
            self.material_changed.emit("texture", int(index))

    def _set_texture_runtime_editable(
        self,
        index: int,
        texture,
        enabled: bool,
    ) -> None:
        texture.locked = 0 if enabled else 1
        self.material_changed.emit("texture", int(index))

    def _master_node(self, material) -> MaterialGraphNodeFrame:
        path = str(material.header.mmtr_path or "")
        filename = PurePosixPath(path.replace("\\", "/")).name
        title = filename.rsplit(".", 1)[0] if filename else self.tr("Master Material")
        node = MaterialGraphNodeFrame(title, "", "#d7b870", 370)
        for label, names in (
            (self.tr("Parameters missing from MDF"), self._match.missing_parameters),
            (self.tr("Parameters not in MMTR"), self._match.unmatched_parameters),
            (self.tr("Parameter layouts differ"), self._match.layout_mismatches),
            (self.tr("Textures missing from MDF"), self._match.missing_textures),
            (self.tr("Textures not in MMTR"), self._match.unmatched_textures),
        ):
            node.add_mismatch(label, names)
        return node

    def _output_node(self) -> MaterialGraphNodeFrame:
        return MaterialGraphNodeFrame(
            self.tr("Renderer"),
            "",
            "#83d1a7",
            250,
        )

    def fit_graph(self) -> None:
        if not self.scene.items():
            return
        self.view.resetTransform()
        self.view.fitInView(
            self.scene.sceneRect(),
            Qt.AspectRatioMode.KeepAspectRatio,
        )
        if self.view.transform().m11() > 1.0:
            self.view.resetTransform()

    def _show_initial_view(self) -> None:
        self.fit_graph()
        if self.scene.items() and self.view.transform().m11() < _READABLE_SCALE:
            # Fitting a tall graph makes nodes unreadable; open readable at its top-left instead.
            self.view.resetTransform()
            self.view.scale(_READABLE_SCALE, _READABLE_SCALE)
            for bar in (self.view.horizontalScrollBar(), self.view.verticalScrollBar()):
                bar.setValue(bar.minimum())


__all__ = [
    "MaterialGraphEdge",
    "MaterialGraphNodeFrame",
    "MaterialContractMatch",
    "MdfMaterialGraph",
    "MovableMaterialGraphNode",
    "friendly_material_name",
    "match_material_contract",
    "parameter_group",
]
