"""See docs/internals/material-variations.md."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, QSize, Qt, Signal
lazy from PySide6.QtGui import QColor
lazy from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget,
)

lazy from file_handlers.mesh.profiles import mesh_profile_for_game
lazy from file_handlers.pyside.value_widgets import rgba_dialog_result
lazy from file_handlers.rsz.campaign_state import CampaignChoice
lazy from file_handlers.rsz.material_variations import VariationEntry
lazy from ui.editor_widgets import styled_label, tool_button
lazy from ui.scene.viewport_overlay import add_overlay_header, overlay_panel_stylesheet
lazy from ui.styles import set_style_role
lazy from ui.widgets_utils import ColorPreviewButton


class PartPickTool(QObject):
    picked = Signal(str, str)
    ended = Signal()

    def __init__(self, viewport, parent=None):
        super().__init__(parent)
        self.viewport = viewport

    def press(self, event, pos) -> bool:
        if event.button() != Qt.LeftButton:
            return False
        key, material = self.viewport.pick_at(pos)
        if key:
            self.picked.emit(key, material)
        return True

    def move(self, event, pos) -> bool:
        if event.buttons() & (Qt.RightButton | Qt.MiddleButton):
            return False
        self.viewport.setCursor(Qt.CrossCursor)
        self.viewport.set_hover_key(*self.viewport.pick_at(pos))
        return True

    def release(self, event, pos) -> bool:
        return False

    def wheel(self, event) -> bool:
        return False

    def key_press(self, event) -> bool:
        if not self.takes_key(event):
            return False
        self.ended.emit()
        return True

    def takes_key(self, event) -> bool:
        return event.key() == Qt.Key_Escape

    def leave(self) -> None:
        self.viewport.set_hover_key("")

    def marks(self) -> tuple:
        return ()

    def gizmo(self):
        return None


def panel_button(icon: str, tip: str, slot=None, parent: QWidget | None = None) -> QToolButton:
    button = tool_button(icon, tip, slot, parent=parent)
    button.setIconSize(QSize(16, 16))
    button.setFixedSize(24, 24)
    button.setFocusPolicy(Qt.NoFocus)
    return button


def text_button(text: str, slot=None, parent: QWidget | None = None) -> QToolButton:
    button = QToolButton(parent)
    button.setText(text)
    button.setToolButtonStyle(Qt.ToolButtonTextOnly)
    button.setFocusPolicy(Qt.NoFocus)
    if slot:
        button.clicked.connect(slot)
    return button


def note_label(text: str, parent: QWidget) -> QLabel:
    label = styled_label(text, parent, tone="muted")
    label.setWordWrap(True)
    return label


def clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().hide()
            item.widget().deleteLater()
        elif item.layout():
            clear_layout(item.layout())


class ScnSceneFeatures:
    def _build_features_panel(self) -> None:
        panel = QFrame(self.preview.preview)
        panel.setObjectName("sceneFeatures")
        panel.viewport_anchor = "top"
        panel.setMinimumWidth(320)
        panel.setStyleSheet(overlay_panel_stylesheet("sceneFeatures"))
        frame = QVBoxLayout(panel)
        frame.setContentsMargins(0, 0, 0, 0)
        frame.setSpacing(0)
        header = add_overlay_header(panel, "sceneFeatures")
        fold = panel_button("chevron_down", QCoreApplication.translate("ScnSceneWorkspace", "Fold panel"),
                            parent=panel)
        self.features_title = QLabel(panel)
        self.features_title.setObjectName("sceneFeaturesTitle")
        self.features_reload = panel_button(
            "refresh", QCoreApplication.translate("ScnSceneWorkspace", "Reload the current choices from their files"),
            self._reload_features, parent=panel)
        self.features_reset = panel_button(
            "reset", QCoreApplication.translate("ScnSceneWorkspace", "Back to the files' states"),
            self._reset_features, parent=panel)
        header.addWidget(fold)
        header.addWidget(self.features_title, 1)
        header.addWidget(self.features_reload)
        header.addWidget(self.features_reset)
        body = QWidget(panel)
        body.setObjectName("sceneFeaturesBody")
        self.features_rows = QVBoxLayout(body)
        self.features_rows.setContentsMargins(8, 6, 8, 6)
        self.features_rows.setSpacing(4)
        frame.addWidget(body)
        self.preview.preview.setup_viewport_overlay(panel, body, fold)
        panel.hide()
        self.features_panel = panel
        self.features_part: QFormLayout | None = None
        self._picked: tuple[str, str] | None = None
        self._pick_tool = PartPickTool(self.preview.preview, panel)
        self._pick_tool.picked.connect(self._pick_part)
        self._pick_tool.ended.connect(lambda: self._set_picking(False))
        self.preview.user_edits_changed.connect(self._user_edits_changed)

    def _show_features(self) -> None:
        clear_layout(self.features_rows)
        self.features_panel.hide()
        self.features_part = None
        campaign, variations = self.preview.campaign_choices(), self.preview.material_variations()
        spawns = self.preview.spawner_choices()
        if not variations:
            self._set_picking(False)
        if campaign is None and not variations and not spawns:
            return
        graph = self.preview.graphs[0]
        profile = mesh_profile_for_game(graph.documents[graph.root_document_id].rsz_file.game_version)
        self.features_title.setText("" if profile is None else profile.display_name)
        content = QWidget(self.features_panel)
        self.features_rows.addWidget(content)
        form = self._features_form(content)
        if campaign is not None:
            self._show_campaign(form, campaign)
        if spawns:
            caption = styled_label(QCoreApplication.translate("ScnSceneWorkspace", "Spawned entries"), content,
                                   heading="caption")
            caption.setToolTip(QCoreApplication.translate(
                "ScnSceneWorkspace", "The entry of its list each spawner spawns: a costume, preset or outfit."))
            form.addRow(caption)
        for choice in spawns:
            name = QLabel(choice.label, content)
            name.setObjectName("overlayFieldLabel")
            box = QComboBox(content)
            box.addItems(list(choice.choices))
            box.setCurrentIndex(choice.applied)
            box.currentIndexChanged.connect(lambda index, key=choice.key: self.preview.set_spawn_choices(
                {**self.preview.spawn_choices, key: index}))
            form.addRow(name, box)
        for label in dict.fromkeys(variation.label for variation in variations):
            form.addRow(styled_label(label, content, heading="caption"))
            for variation in (item for item in variations if item.label == label):
                name = QLabel(Path(variation.data).stem, content)
                name.setObjectName("overlayFieldLabel")
                name.setToolTip(variation.data)
                box = QComboBox(content)
                for value, text in variation.choices:
                    box.addItem(text, value)
                box.setCurrentIndex(box.findData(variation.applied))
                box.setEnabled(not variation.fixed)
                if variation.fixed:
                    box.setToolTip(QCoreApplication.translate("ScnSceneWorkspace",
                                                              "Its controller holds a fixed choice."))
                box.currentIndexChanged.connect(
                    lambda index, key=variation.key, values=tuple(value for value, _text in variation.choices):
                    self._pick_feature(key, values[index]))
                form.addRow(name, box)
        if variations:
            part = QWidget(self.features_panel)
            self.features_rows.addWidget(part)
            self.features_part = self._features_form(part)
            self._show_part()
        self.features_reload.setVisible(bool(variations))
        self.features_reset.setEnabled(bool(self.preview.variations or self.preview.spawn_choices)
                                       or self.preview.campaign is not None)
        self._fit_features()

    @staticmethod
    def _features_form(parent: QWidget) -> QFormLayout:
        form = QFormLayout(parent)
        form.setContentsMargins(0, 0, 0, 0)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        return form

    def _fit_features(self) -> None:
        # New rows reach the panel's size hint through layout events; activating the layouts carries them now.
        for layout in (self.features_part, self.features_rows):
            if layout is not None:
                layout.activate()
        self.features_panel.adjustSize()
        self.features_panel.show()
        self.preview.preview.place_viewport_overlays()

    def _show_campaign(self, form: QFormLayout, choices) -> None:
        panel = form.parentWidget()
        caption = styled_label(QCoreApplication.translate("ScnSceneWorkspace", "Campaign state"), panel,
                               heading="caption")
        caption.setToolTip(QCoreApplication.translate(
            "ScnSceneWorkspace", "Start-up rules read the story flags, chapter and difficulty the game starts the "
            "campaign with from an initial setting. Without one they read the files' states."))
        form.addRow(caption)
        current = self.preview.campaign
        boxes = []
        for label, items, value in (
            (QCoreApplication.translate("ScnSceneWorkspace", "Initial setting"), choices[0], current and current.key),
            (QCoreApplication.translate("ScnSceneWorkspace", "Difficulty"), choices[1], current and current.difficulty),
        ):
            name = QLabel(label, panel)
            name.setObjectName("overlayFieldLabel")
            box = QComboBox(panel)
            for item_value, text in items:
                box.addItem(text, item_value)
            if value is not None and (index := box.findData(value)) >= 0:
                box.setCurrentIndex(index)
            form.addRow(name, box)
            boxes.append(box)
        clear_data = QCheckBox(QCoreApplication.translate("ScnSceneWorkspace", "From clear data"), panel)
        clear_data.setToolTip(QCoreApplication.translate(
            "ScnSceneWorkspace", "A new game started from a clear save: played-through rules hold."))
        clear_data.setChecked(bool(current and current.clear_data))
        clear_data.setEnabled(boxes[0].currentData() in choices[2])
        boxes[0].currentIndexChanged.connect(lambda _index: clear_data.setEnabled(boxes[0].currentData() in choices[2]))
        apply = text_button(
            QCoreApplication.translate("ScnSceneWorkspace", "Apply"),
            lambda _checked=False: self.preview.set_campaign(CampaignChoice(
                boxes[0].currentData(), boxes[1].currentData(), clear_data.isEnabled() and clear_data.isChecked())),
            parent=panel)
        set_style_role(apply, variant="primary")
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(apply)
        form.addRow(clear_data, row)

    def _show_part(self) -> None:
        form = self.features_part
        panel = form.parentWidget()
        clear_layout(form)
        picking, edited = self.preview.preview.edit_tool is self._pick_tool, bool(self.preview.loader.user_documents)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        if edited:
            buttons.addWidget(panel_button(
                "reset", QCoreApplication.translate("ScnSceneWorkspace", "Discard the unsaved color edits"),
                self._discard_colours, parent=panel))
        pick = tool_button("target", QCoreApplication.translate(
            "ScnSceneWorkspace", "Pick a part in the viewport without selecting it (Esc ends)"),
            text=QCoreApplication.translate("ScnSceneWorkspace", "Pick"), parent=panel)
        pick.setFocusPolicy(Qt.NoFocus)
        pick.setCheckable(True)
        pick.setChecked(picking)
        pick.toggled.connect(self._set_picking)
        buttons.addWidget(pick)
        form.addRow(styled_label(QCoreApplication.translate("ScnSceneWorkspace", "Part colors"), panel,
                                 heading="caption"), buttons)
        if edited:
            form.addRow(note_label(QCoreApplication.translate(
                "ScnSceneWorkspace", "Saving writes the edited color files."), panel))
        colours = None if self._picked is None else self.preview.variation_colours(self._picked[0])
        if colours is None:
            note = (QCoreApplication.translate("ScnSceneWorkspace", "The object picked has no color entries.")
                    if self._picked else
                    QCoreApplication.translate("ScnSceneWorkspace", "Click a part in the viewport. Esc ends picking.")
                    if picking else
                    QCoreApplication.translate("ScnSceneWorkspace", "Turn on Pick, then click a part in the viewport."))
            form.addRow(note_label(note, panel))
            return
        name = QLabel(QCoreApplication.translate("ScnSceneWorkspace", "Part"), panel)
        name.setObjectName("overlayFieldLabel")
        part = QLabel(colours.part, panel)
        part.setToolTip(colours.mesh)
        form.addRow(name, part)
        keys = tuple(colours.materials)
        current = self._picked[1] if self._picked[1] in colours.materials else next(iter(keys), "")
        name = QLabel(QCoreApplication.translate("ScnSceneWorkspace", "Material"), panel)
        name.setObjectName("overlayFieldLabel")
        box = QComboBox(panel)
        box.addItems([material for material, _rows in colours.materials.values()])
        box.setCurrentIndex(keys.index(current) if current else -1)
        box.currentIndexChanged.connect(lambda index, key=self._picked[0]: self._pick_part(key, keys[index]))
        form.addRow(name, box)
        _material, rows = colours.materials.get(current, ("", ()))
        # A base entry a choice's entry overrides changes nothing drawn under this choice.
        rows = [(entry, drawn) for entry, drawn in rows if drawn or not entry.base]
        if not rows:
            form.addRow(note_label(QCoreApplication.translate("ScnSceneWorkspace", "No color entries."), panel))
        variation = colours.variation
        chosen = dict(variation.choices).get(variation.applied, str(variation.applied))
        for entry, drawn in rows:
            name = QLabel(entry.parameter, panel)
            name.setObjectName("overlayFieldLabel")
            place = f"{Path(entry.file).name} › " + "".join(
                f"[{step}]" if isinstance(step, int) else f".{step}" for step in entry.path)[1:]
            swatch = ColorPreviewButton(panel)
            swatch.setFixedSize(24, 24)
            swatch.setColor(*entry.raw[0][:3])
            swatch.setFocusPolicy(Qt.NoFocus)
            swatch.setToolTip(QCoreApplication.translate("ScnSceneWorkspace", "Choose the color of {place}").format(
                place=place))
            swatch.clicked.connect(lambda _checked=False, entry=entry: self._choose_colour(entry))
            source = variation.base if entry.base else chosen
            if not entry.applied:
                source = QCoreApplication.translate("ScnSceneWorkspace", "{source}, off").format(source=source)
            elif not drawn:
                source = QCoreApplication.translate("ScnSceneWorkspace", "{source}, not drawn").format(source=source)
            text = QLabel(source, panel)
            text.setToolTip(place)
            field = QHBoxLayout()
            field.addWidget(swatch)
            field.addWidget(text, 1)
            form.addRow(name, field)

    def _pick_part(self, key: str, material: str) -> None:
        self._picked = (key, material) if key else None
        self._refresh_part()

    def _refresh_part(self) -> None:
        if self.features_part is not None:
            self.features_panel.hide()
            self._show_part()
            self._fit_features()

    def _set_picking(self, on: bool) -> None:
        viewport = self.preview.preview
        if on == (viewport.edit_tool is self._pick_tool):
            return
        viewport.set_edit_tool(self._pick_tool if on else None)
        if not on:
            viewport.unsetCursor()
            viewport.set_hover_key("")
        self._refresh_part()

    def _choose_colour(self, entry: VariationEntry) -> None:
        key, held = self._picked[0], self.preview.user_edited(entry.file)
        chosen = rgba_dialog_result(self.features_panel, QColor(*entry.raw[0]),
                                    lambda rgba: self.preview.set_variation_entry(key, entry, (rgba,), True))
        if chosen is not None:
            self.preview.set_variation_entry(key, entry, (chosen,), True)
        elif held:
            self.preview.set_variation_entry(key, entry, entry.raw, entry.applied)
        else:
            self.preview.discard_user_edits(entry.file)
        self._refresh_part()

    def _discard_colours(self) -> None:
        self.preview.discard_user_edits()

    def _user_edits_changed(self) -> None:
        # Saved or discarded: live colour changes keep files held and leave the panel to _choose_colour.
        if not self.preview.loader.user_documents:
            self._refresh_part()

    def _pick_feature(self, key: str, value: int) -> None:
        self.preview.set_variations({**self.preview.variations, key: value})
        self.features_reset.setEnabled(True)
        self._refresh_part()

    def _reload_features(self) -> None:
        self.preview.set_variations(dict(self.preview.variations), reload=True)
        self._show_features()

    def _reset_features(self) -> None:
        self.preview.set_variations({})
        self.preview.set_campaign(None)
        self.preview.set_spawn_choices({})
        self._show_features()
