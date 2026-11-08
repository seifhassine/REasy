from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from PySide6.QtCore import QT_TRANSLATE_NOOP, QCoreApplication, QPointF, QRectF, QSignalBlocker, Qt, QTimer, Signal
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
    def __init__(self, accent: str, width: int):
        super().__init__()
        self.setObjectName("materialGraphNode")
        self.setMinimumWidth(width)
        self.setMaximumWidth(width)
        self.setStyleSheet(_node_stylesheet(accent))
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.content = QVBoxLayout()
        self.content.setContentsMargins(8, 6, 8, 8)
        self.content.setSpacing(6)
        root.addLayout(self.content)

    def set_header(self, title: str, subtitle: str) -> None:
        title_label = QLabel(title, self)
        title_label.setObjectName("materialGraphNodeTitle")
        self.layout().insertWidget(0, title_label)
        if subtitle:
            subtitle_label = QLabel(subtitle, self)
            subtitle_label.setObjectName("materialGraphNodeSubtitle")
            subtitle_label.setWordWrap(True)
            self.content.addWidget(subtitle_label)

    def add_control(self, label: str, control: QWidget, tooltip: str) -> None:
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        name = QLabel(label, row)
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
            f"{title}\n" + "\n".join(f"• {name}" for name in names), self
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
        self._document = None
        self._structure = None
        self._flag_fields = {}
        self._texture_preview_provider = None
        self._texture_previews: dict[str, tuple[QLabel, str]] = {}
        self._columns = ((), (), ())
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
        flag_fields=(),
        texture_preview_provider=None,
    ) -> None:
        fields = {field.name: field for field in flag_fields}
        structure = None if material is None else (
            material.header.mmtr_path,
            tuple((p.name, p.component_count, p.param_rel_offset) for p in material.parameters),
            tuple(t.tex_type for t in material.textures),
        )
        self._material, self._texture_preview_provider = material, texture_preview_provider
        if material is not None and document is self._document and (fields, structure) == (self._flag_fields, self._structure):
            if self._sync_values():
                self._layout_nodes()
            return
        self._document, self._flag_fields, self._structure = document, fields, structure
        self._match = match_material_contract(material, document) if material is not None else MaterialContractMatch()
        try:
            layout = MaterialDescriptorLayout.from_material(material) if material is not None else None
            self._input_usage = material_input_usage(document, descriptor_layout=layout)
        except ValueError:
            self._input_usage = MaterialInputUsage()
        self.rebuild()

    def show_loading(self) -> None:
        self.set_material(None)
        self.status.setText(self.tr("Loading master material…"))

    def rebuild(self) -> None:
        self.scene.clear()
        self._edges.clear()
        self.parameter_editors.clear()
        self.parameter_runtime_editors.clear()
        self.texture_editors.clear()
        self.texture_runtime_editors.clear()
        self.behavior_editors.clear()
        self._texture_previews.clear()
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

        texture_nodes = [self._place(self._texture_node(index, texture))
                         for index, texture in enumerate(material.textures)]
        group_order = (
            "Surface color",
            "Surface response",
            "Ice and reflection",
            "Transparency",
            "Emission",
            "Texture mapping",
            "Additional controls",
        )
        behavior = self._behavior_node(material)
        control_nodes = [self._place(behavior)] if behavior is not None else []
        control_nodes += [self._place(self._parameter_node(group, parameter_groups[group]))
                          for group in group_order if parameter_groups.get(group)]
        master_proxy = self._place(self._master_node(material))
        output_proxy = self._place(self._node(("outputs", "renderer"), self.tr("Renderer"), "", "#83d1a7", 250))
        self._columns = (texture_nodes, control_nodes, (master_proxy, output_proxy))
        self._layout_nodes()

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
        self._fit_timer.start()

    def _layout_nodes(self) -> None:
        texture_nodes, control_nodes, ends = self._columns
        bottoms = []
        for x, column in ((20.0, texture_nodes), (390.0, control_nodes)):
            y = 20.0
            for proxy in column:
                proxy.setPos(x, y)
                y += proxy.boundingRect().height() + 22.0
            bottoms.append(y)
        middle = max(*bottoms, 620.0) * 0.5
        for x, proxy in zip((790.0, 1190.0), ends):
            proxy.setPos(x, max(20.0, middle - proxy.boundingRect().height() * 0.5))
        self.scene.setSceneRect(self.scene.itemsBoundingRect().adjusted(-55, -55, 55, 55))

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

    def _node(self, tag, title: str, subtitle: str, accent: str, width: int) -> MaterialGraphNodeFrame:
        # Filled after entering the scene: with accessibility active, each change to a widget outside a shown window
        # walks every widget in the app.
        node = MaterialGraphNodeFrame(accent, width)
        proxy = MovableMaterialGraphNode(self._update_edges)
        proxy.setWidget(node)
        proxy.hide()
        proxy.setZValue(2)
        proxy.setData(0, tag)
        self.scene.addItem(proxy)
        node.set_header(title, subtitle)
        return node

    @staticmethod
    def _place(node: MaterialGraphNodeFrame) -> QGraphicsProxyWidget:
        node.adjustSize()
        proxy = node.graphicsProxyWidget()
        proxy.show()
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
        label = QLabel(usage.label, layout.parentWidget())
        label.setObjectName("materialGraphNote")
        label.setWordWrap(True)
        label.setToolTip(usage.explanation)
        layout.addWidget(label)

    def _parameter_node(self, group: str, members) -> MaterialGraphNodeFrame:
        node = self._node(("parameter_group", group), group, "", "#c6a0e8", 355)
        for index, parameter in members:
            host = QWidget(node)
            layout = QVBoxLayout(host)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(3)
            control, editors = self._parameter_control(index, parameter, host)
            self.parameter_editors[parameter.name] = tuple(editors)
            layout.addWidget(control)
            self._add_usage_label(layout, self._input_usage.parameter(
                parameter.param_rel_offset, parameter.component_count * 4))
            runtime_editable = QCheckBox(self.tr("Allow runtime changes"), host)
            runtime_editable.setChecked(not bool(parameter.component_locked))
            runtime_editable.setToolTip(
                self.tr("Prevent the game from overriding this saved value.")
            )
            runtime_editable.toggled.connect(
                lambda enabled, i=index: self._set_parameter_runtime_editable(i, enabled)
            )
            self.parameter_runtime_editors[parameter.name] = runtime_editable
            layout.addWidget(runtime_editable)
            source = self.tr("MDF parameter: {name}").format(name=parameter.name)
            node.add_control(friendly_material_name(parameter.name), host, source)
        return node

    def _behavior_node(self, material) -> MaterialGraphNodeFrame | None:
        shown = [(label, inverted, self._flag_fields[name]) for label, inverted, name in _BEHAVIOR_FLAGS
                 if name in self._flag_fields]
        if not shown:
            return None
        node = self._node(("behavior", "material_flags"), self.tr("Rendering behavior"), "", "#d99173", 355)
        flags = int(getattr(material.header, "material_flags", 0))
        host = QWidget(node)
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(5)
        for flag_index, (label, inverted, field) in enumerate(shown):
            checkbox = QCheckBox(self.tr(label), host)
            checkbox.setChecked(bool(field.get(flags)) != inverted)
            checkbox.toggled.connect(
                lambda enabled, f=field, inv=inverted: self._set_behavior_flag(f, enabled, inverted=inv))
            grid.addWidget(checkbox, flag_index // 2, flag_index % 2)
            self.behavior_editors[field.name] = checkbox
        node.content.addWidget(host)
        return node

    def _set_behavior_flag(self, field, enabled: bool, *, inverted: bool) -> None:
        if self._material is None:
            return
        self._material.header.material_flags = field.set(self._material.header.material_flags, bool(enabled) != inverted)
        self.material_changed.emit("header", field.bit)

    def _parameter_control(self, index: int, parameter, parent: QWidget):
        count = max(1, min(4, int(parameter.component_count)))
        values = list(parameter.parameter[:count])
        lowered = str(parameter.name).casefold()
        if is_color_parameter(parameter):
            button = QPushButton(parent)
            self._refresh_color_button(button, values)
            button.clicked.connect(
                lambda _checked=False, i=index, b=button: self._choose_parameter_color(i, b)
            )
            return button, (button,)
        if count == 1 and lowered.startswith(("use", "enable")):
            checkbox = QCheckBox(self.tr("Enabled"), parent)
            checkbox.setChecked(bool(values[0]))
            checkbox.toggled.connect(
                lambda checked, i=index: self._set_parameter_component(i, 0, 1.0 if checked else 0.0)
            )
            return checkbox, (checkbox,)
        if count == 1:
            editor = self._number_editor(parameter.name, values[0], parent)
            editor.valueChanged.connect(
                lambda value, i=index: self._set_parameter_component(i, 0, value)
            )
            return editor, (editor,)

        host = QWidget(parent)
        layout = QHBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        editors = []
        for component, value in enumerate(values):
            editor = self._number_editor(parameter.name, value, host, compact=True)
            editor.valueChanged.connect(
                lambda changed, c=component, i=index: self._set_parameter_component(i, c, changed)
            )
            layout.addWidget(editor)
            editors.append(editor)
        return host, tuple(editors)

    @staticmethod
    def _number_editor(name: str, value: float, parent: QWidget, *, compact: bool = False) -> QDoubleSpinBox:
        editor = QDoubleSpinBox(parent)
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

    def _set_parameter_component(self, index: int, component: int, value: float) -> None:
        parameter = self._material.parameters[index]
        current = list(parameter.parameter)
        while len(current) < 4:
            current.append(0.0)
        current[int(component)] = float(value)
        parameter.parameter = tuple(current[:4])
        self.material_changed.emit("parameter", int(index))

    def _set_parameter_runtime_editable(self, index: int, enabled: bool) -> None:
        self._material.parameters[index].component_locked = 0 if enabled else 1
        self.material_changed.emit("parameter", int(index))

    @staticmethod
    def _refresh_color_button(button: QPushButton, values) -> None:
        channels = (list(values) + [1.0] * 4)[:4]
        color = _material_color(channels)
        button.setText("  ".join(f"{float(value):.2f}" for value in channels))
        text_color = "#11161a" if color.lightnessF() > 0.58 else "#f5f7f8"
        style = (
            "QPushButton {"
            f"background:{color.name(QColor.NameFormat.HexArgb)}; color:{text_color};"
            f"border:1px solid {current_colors()['border_strong']}; border-radius:3px; padding:3px 6px;"
            "}"
        )
        if button.styleSheet() != style:
            button.setStyleSheet(style)

    def _choose_parameter_color(self, index: int, button: QPushButton) -> None:
        if not pick_parameter_color(self, self._material, index):
            return
        parameter = self._material.parameters[index]
        self._refresh_color_button(button, parameter.parameter[:parameter.component_count])
        self.material_changed.emit("parameter", int(index))

    def _sync_values(self) -> bool:
        material, resized = self._material, False
        for parameter in material.parameters:
            editors = self.parameter_editors.get(parameter.name)
            if editors is None:
                continue
            values = parameter.parameter[:max(1, min(4, int(parameter.component_count)))]
            if isinstance(editors[0], QPushButton):
                self._refresh_color_button(editors[0], values)
            else:
                for editor, value in zip(editors, values):
                    with QSignalBlocker(editor):
                        if isinstance(editor, QCheckBox):
                            editor.setChecked(bool(value))
                        else:
                            editor.setValue(float(value))
            with QSignalBlocker(runtime := self.parameter_runtime_editors[parameter.name]):
                runtime.setChecked(not parameter.component_locked)
        for texture in material.textures:
            if (path := self.texture_editors[texture.tex_type]).text() != texture.tex_path:
                path.setText(texture.tex_path)
            with QSignalBlocker(runtime := self.texture_runtime_editors[texture.tex_type]):
                runtime.setChecked(not texture.locked)
            preview, shown = self._texture_previews[texture.tex_type]
            if shown != texture.tex_path:
                self._show_preview(preview, texture)
                preview.parentWidget().adjustSize()
                resized = True
        flags = int(material.header.material_flags)
        for _label, inverted, name in _BEHAVIOR_FLAGS:
            if (checkbox := self.behavior_editors.get(name)) is not None:
                with QSignalBlocker(checkbox):
                    checkbox.setChecked(bool(self._flag_fields[name].get(flags)) != inverted)
        return resized

    def _show_preview(self, preview: QLabel, texture) -> None:
        pixmap = self._texture_preview_provider(texture) if callable(self._texture_preview_provider) else None
        preview.setToolTip(pixmap if isinstance(pixmap, str) else "")
        if pixmap is not None and not isinstance(pixmap, str) and not pixmap.isNull():
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
        self._texture_previews[texture.tex_type] = preview, texture.tex_path

    def _texture_node(self, index: int, texture) -> MaterialGraphNodeFrame:
        node = self._node(("texture", texture.tex_type), friendly_material_name(texture.tex_type), texture.tex_type,
                          "#78b9d7", 330)
        self._add_usage_label(node.content, self._input_usage.texture(texture.tex_type))
        preview = QLabel(node)
        preview.setObjectName("materialGraphPreview")
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview.setMinimumHeight(88)
        self._show_preview(preview, texture)
        node.content.addWidget(preview)
        path = QLineEdit(texture.tex_path, node)
        path.setClearButtonEnabled(True)
        path.editingFinished.connect(
            lambda i=index, editor=path: self._set_texture_path(i, editor.text())
        )
        node.content.addWidget(path)
        runtime_editable = QCheckBox(self.tr("Allow runtime changes"), node)
        runtime_editable.setChecked(not bool(texture.locked))
        runtime_editable.setToolTip(
            self.tr("Prevent the game from overriding this saved texture.")
        )
        runtime_editable.toggled.connect(
            lambda enabled, i=index: self._set_texture_runtime_editable(i, enabled)
        )
        self.texture_runtime_editors[texture.tex_type] = runtime_editable
        node.content.addWidget(runtime_editable)
        self.texture_editors[texture.tex_type] = path
        return node

    def _set_texture_path(self, index: int, value: str) -> None:
        texture = self._material.textures[index]
        changed = str(value) != str(texture.tex_path)
        texture.tex_path = str(value)
        if changed:
            self.material_changed.emit("texture", int(index))

    def _set_texture_runtime_editable(self, index: int, enabled: bool) -> None:
        self._material.textures[index].locked = 0 if enabled else 1
        self.material_changed.emit("texture", int(index))

    def _master_node(self, material) -> MaterialGraphNodeFrame:
        path = str(material.header.mmtr_path or "")
        filename = PurePosixPath(path.replace("\\", "/")).name
        title = filename.rsplit(".", 1)[0] if filename else self.tr("Master Material")
        node = self._node(("master", material.header.mmtr_path), title, "", "#d7b870", 370)
        for label, names in (
            (self.tr("Parameters missing from MDF"), self._match.missing_parameters),
            (self.tr("Parameters not in MMTR"), self._match.unmatched_parameters),
            (self.tr("Parameter layouts differ"), self._match.layout_mismatches),
            (self.tr("Textures missing from MDF"), self._match.missing_textures),
            (self.tr("Textures not in MMTR"), self._match.unmatched_textures),
        ):
            node.add_mismatch(label, names)
        return node

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
