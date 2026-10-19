from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QSizePolicy, QSpinBox, QToolButton, QVBoxLayout, QWidget,
)

from ui.scene.mesh_attributes import HEAT_COLORS
from .preview_controls import ComboStepper, make_searchable


class AttributeControls(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.view = None
        self._name = None
        self._preferences = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        row = QHBoxLayout()
        self.mode = QComboBox(self)
        self.mode.setAccessibleName(self.tr("Attribute display mode"))
        row.addWidget(self.mode)
        self.entry = QComboBox(self)
        make_searchable(self.entry, self.tr("Search bone or palette entry…"))
        self.entry.setMinimumContentsLength(20)
        self.entry.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.entry.setAccessibleName(self.tr("Weight influence"))
        self.entry.setToolTip(self.tr("Search a bone name or palette entry. Duplicate lanes are added together."))
        self.entry_steps = ComboStepper(self.entry, self.tr("influence"), self)
        self.entry_steps.setMaximumWidth(420)
        row.addWidget(self.entry_steps, 1)
        self.scale = QSpinBox(self)
        self.scale.setRange(1, 256)
        self.scale.setValue(8)
        self.scale.setSuffix(self.tr(" checks / UV tile"))
        self.scale.setAccessibleName(self.tr("UV checker density"))
        row.addWidget(self.scale)
        self.minimum = QLabel(self)
        self.ramp = QLabel(self)
        self.ramp.setFixedSize(120, 12)
        self.maximum = QLabel(self)
        for widget in (self.minimum, self.ramp, self.maximum):
            row.addWidget(widget)
        self.guide_button = QToolButton(self)
        self.guide_button.setCheckable(True)
        self.guide_button.setAutoRaise(True)
        self.guide_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.guide_button.setArrowType(Qt.RightArrow)
        self.guide_button.setToolTip(self.tr("Explain the colors, values, and limitations of this view."))
        self.guide_button.toggled.connect(self._toggle_guide)
        row.addWidget(self.guide_button)
        row.addStretch()
        layout.addLayout(row)
        self.legend = QLabel(self)
        self.legend.setTextFormat(Qt.PlainText)
        self.legend.setWordWrap(True)
        self.legend.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.legend)
        self.legend.hide()
        # What drawing the attribute on the graphics device tells (``AttributesPreview.status``).
        self.status = QLabel(self)
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.status)
        self.status.hide()
        self.mode.currentIndexChanged.connect(self._changed)
        self.entry.currentIndexChanged.connect(self._changed)
        self.scale.valueChanged.connect(self._changed)
        self.hide()

    def set_status(self, lines):
        self.status.setText("\n".join(lines))
        self.status.setVisible(bool(lines))

    def _toggle_guide(self, visible):
        self.legend.setVisible(visible)
        self.guide_button.setArrowType(Qt.DownArrow if visible else Qt.RightArrow)

    def set_view(self, view, mesh, name):
        if self.view is view and self._name == name:
            return
        self.view, self._name = view, name
        self.setVisible(name is not None)
        with QSignalBlocker(self.mode), QSignalBlocker(self.entry), QSignalBlocker(self.scale):
            self.mode.clear()
            self.entry.clear()
            if view is not None:
                mode, entry, scale = self._preferences.get(name, (view.mode, view.entry, view.uv_scale))
                for label, value in view.modes:
                    self.mode.addItem(self.tr(label), value)
                self.mode.setCurrentIndex(max(0, self.mode.findData(mode)))
                for value in view.entries:
                    self.entry.addItem(view.entry_label(mesh, value), value)
                selected = self.entry.findData(entry)
                self.entry.setCurrentIndex(selected if selected >= 0 else self.entry.findData(view.entry))
                self.scale.setValue(scale)
        self._changed()

    def _changed(self, *_args):
        view = self.view
        if view is not None:
            view.mode = self.mode.currentData()
            view.entry = self.entry.currentData()
            view.uv_scale = self.scale.value()
            self._preferences[self._name] = view.mode, view.entry, view.uv_scale
        self.mode.setVisible(view is not None and len(view.modes) > 1)
        self.entry_steps.setVisible(view is not None and view.is_weight and view.mode == "influence")
        self.entry_steps.refresh_enabled()
        self.scale.setVisible(view is not None and view.is_uv and view.mode == "checker")
        self._update_legend()
        self.changed.emit()

    def _update_legend(self):
        view = self.view
        colors, limits = None, ("0", "1+")
        if view is None:
            text = self.tr("Colors mark the vertices sampled by each bone-modifier effector; "
                           "gray = unsampled. These are sample locations, not skin weights. "
                           "Overlapping samples show the last effector's color.")
        elif view.is_weight:
            if view.mode == "dominant":
                text = self.tr("Hue = strongest palette entry; brightness = its weight. Gray = zero. "
                               "Choose Influence to identify an entry by bone name.")
            else:
                colors = HEAT_COLORS
                if view.mode == "influence":
                    count = view.entry_counts.get(view.entry, 0)
                    text = self.tr("Selected entry has positive weight on {count:,} vertices in this LOD stream.").format(count=count)
                elif view.mode == "count":
                    limits = "0", str(view.count_max)
                    text = self.tr("Number of distinct entries with positive weight; repeated lanes count once.")
                else:
                    valid = view.totals[~view.invalid]
                    text = self.tr("Sum in this stream: {low:.3g} to {high:.3g}.").format(
                        low=float(valid.min()) if valid.size else 0, high=float(valid.max()) if valid.size else 0)
            text += self.tr(" Decoded stream only; shader rules and other streams are not combined. "
                            "Partial streams need not sum to 1. Magenta = negative or non-finite weights.")
        elif view.is_uv:
            text = (self.tr("Squares are evaluated across the surface: stretching shows distortion. "
                            "Density is per UV tile; tiled and negative coordinates are supported.")
                    if view.mode == "checker" else
                    self.tr("Red = U, green = V within each repeated UV tile."))
            text += self.tr(" Magenta = invalid UV coordinates.")
        elif view.name == "colors":
            if view.mode == "rgb":
                text = self.tr("Stored RGB, shown opaque. Choose Alpha to inspect opacity separately.")
            else:
                colors, limits = ((0, 0, 0), (1, 1, 1)), ("0", "255")
                text = self.tr("Stored {channel} channel, shown opaque in grayscale.").format(
                    channel=self.mode.currentText().lower())
        elif view.name in {"normals", "tangents"}:
            if view.mode == "direction":
                text = self.tr("RGB = XYZ direction; each component maps −1 to 0 and +1 to 1.")
            elif view.mode == "length":
                colors, limits = HEAT_COLORS, ("0", "0.25+")
                text = self.tr("Absolute difference from unit length. This measures length, not direction correctness.")
            else:
                colors, limits = ((0, 0, 0), (1, 1, 1)), ("−1", "+1")
                text = self.tr("Selected direction component, in mesh coordinates.")
            text += (self.tr(" Normals follow the current pose.") if view.name == "normals" else
                     self.tr(" Stored tangents; not regenerated for morphs."))
            text += self.tr(" Magenta = non-finite values.")
        elif view.name == "tangent_handedness":
            text = self.tr("Blue = negative · gray = zero · orange = positive. Magenta = non-finite values.")
        elif view.name == "pivot_indices":
            text = self.tr("Each hue represents a local pivot selector. Enable Pivots to see their positions.")
        else:
            colors, limits = ((0, 0, 0), (1, 1, 1)), ("−1", "+1")
            text = self.tr("Stored signed Normal W; its meaning is unspecified for this mesh profile.")
        self.legend.setText(text)
        self.guide_button.setText(self.tr("Decoded weights") if view is not None and view.is_weight
                                  else self.tr("Color guide"))
        for widget in (self.minimum, self.ramp, self.maximum):
            widget.setVisible(colors is not None)
        if colors is not None:
            self.minimum.setText(limits[0])
            self.maximum.setText(limits[1])
            stops = ", ".join(f"stop:{i / (len(colors) - 1):.3f} rgb({int(r*255)}, {int(g*255)}, {int(b*255)})"
                              for i, (r, g, b) in enumerate(colors))
            self.ramp.setStyleSheet(f"background: qlineargradient(x1:0,y1:0,x2:1,y2:0,{stops}); border-radius:3px;")
