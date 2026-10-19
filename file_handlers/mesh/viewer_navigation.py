from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QMenu, QToolButton

from ui.styles import get_color_scheme
from .preview_controls import find_combo_data


class MeshNavigation(QObject):
    def __init__(self, viewer):
        super().__init__(viewer)
        self.viewer = viewer
        self.viewport = viewer.viewport
        self.inspector = viewer.inspector
        self.last_attribute = viewer.attribute_combo.currentData() or viewer.attribute_combo.itemData(1)
        self.menu = QMenu(viewer.navigation_button)
        self.context_menu = QMenu(self.inspector.tree)
        disabled = get_color_scheme()["text_disabled"]
        for menu in (self.menu, self.context_menu):
            menu.setStyleSheet(f"QMenu::item:disabled {{ color: {disabled}; }}")
        self.actions, self.bindings = {}, {}
        specs = (
            ("search", self.tr("Find material / submesh"), "Ctrl+F", self.focus_search, False),
            ("frame", self.tr("Frame selection / visible"), "F", viewer._frame_inspection, False),
            ("fit", self.tr("Fit visible"), "Home", self.viewport.fit_geometry, True),
            ("isolate", self.tr("Isolate / restore selection"), "I", self.inspector.isolate_selection, False),
            ("hide", self.tr("Hide selection"), "H", self.inspector.hide_selection, False),
            ("show_all", self.tr("Show all"), "Alt+H", self.inspector.show_all, False),
            ("back", self.tr("Previous visibility"), "Alt+Left", self.inspector.restore_visibility, False),
            ("surface", self.tr("Surface / last attribute"), "V", self.toggle_surface, True),
            ("previous_lod", self.tr("Previous LOD"), "PgUp", lambda: self.step_lod(-1), True),
            ("next_lod", self.tr("Next LOD"), "PgDown", lambda: self.step_lod(1), True),
            ("previous_morph", self.tr("Previous morph"), "Left", lambda: viewer.morph_controls.step(-1), True),
            ("next_morph", self.tr("Next morph"), "Right", lambda: viewer.morph_controls.step(1), True),
            ("play", self.tr("Play / pause morph"), "Space", viewer.morph_controls.play_button.click, True),
            ("previous_influence", self.tr("Previous influence"), "Ctrl+Left", lambda: self.step_influence(-1), True),
            ("next_influence", self.tr("Next influence"), "Ctrl+Right", lambda: self.step_influence(1), True),
        )
        self.callbacks = {}
        for name, label, shortcut, callback, viewport_only in specs:
            if name in {"surface", "previous_lod", "previous_morph", "previous_influence"}:
                self.menu.addSeparator()
            action = self.menu.addAction(label + "\t" + shortcut)
            action.triggered.connect(lambda _checked=False, key=name: self.activate(key))
            self.actions[name], self.callbacks[name] = action, callback
            key = QKeySequence(shortcut)[0]
            self.bindings[(key.keyboardModifiers(), key.key())] = name, viewport_only
            if not viewport_only and name != "search":
                self.context_menu.addAction(action)
        camera = self.menu.addMenu(self.tr("Camera"))
        for label, name, shortcut in (
            (self.tr("3D view"), "orbit", ""), (self.tr("Front"), "front", "1"), (self.tr("Back"), "back", "Ctrl+1"),
            (self.tr("Right"), "right", "3"), (self.tr("Left"), "left", "Ctrl+3"),
            (self.tr("Top"), "top", "7"), (self.tr("Bottom"), "bottom", "Ctrl+7"),
        ):
            camera.addAction(label + ("\t" + shortcut if shortcut else ""),
                             lambda _checked=False, value=name: self.viewport.set_camera_view(value))
        camera.addAction(self.tr("Toggle orthographic") + "\t5",
                         lambda: self.viewport.set_orthographic(not self.viewport.orthographic))
        help_action = self.menu.addAction(self.tr("Drag: orbit · Middle/right drag: pan · Wheel: zoom"))
        help_action.setEnabled(False)
        viewer.navigation_button.setMenu(self.menu)
        viewer.navigation_button.setPopupMode(QToolButton.InstantPopup)
        viewer.attribute_toggle.clicked.connect(self.toggle_surface)
        viewer.attribute_combo.currentIndexChanged.connect(self._attribute_changed)
        viewer.geometry_combo.currentIndexChanged.connect(self.refresh_actions)
        self.menu.aboutToShow.connect(self.refresh_actions)
        self.context_menu.aboutToShow.connect(self.refresh_actions)
        self.inspector.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.inspector.tree.customContextMenuRequested.connect(self._context_menu)
        self.inspector.filter_edit.setToolTip(self.tr(
            "Filter the list without hiding geometry. Down enters results; Enter shows and frames the first result. "
            "Ctrl+Enter isolates it. Escape clears the search."))
        for widget in (self.viewport, self.inspector.tree, self.inspector.filter_edit):
            widget.installEventFilter(self)
        for buttons, keys in ((viewer.morph_controls.steps, ("Left", "Right")),
                              (viewer.attribute_controls.entry_steps, ("Ctrl+Left", "Ctrl+Right"))):
            for button, key in zip((buttons.previous, buttons.next), keys):
                button.setToolTip(button.toolTip() + self.tr(" ({key} in viewport)").format(key=key))
        self.refresh_actions()

    def enabled(self, name):
        viewer = self.viewer
        ready = self.inspector.isEnabled() and self.inspector.inspection is not None
        if name in {"previous_lod", "next_lod"}:
            direction = -1 if name == "previous_lod" else 1
            return self.lod_target(direction) != viewer.geometry_combo.currentIndex()
        if not ready:
            return False
        if name in {"isolate", "hide"}:
            return bool(self.inspector.selected_parts())
        if name == "back":
            return bool(self.inspector._visibility_history)
        if name == "surface":
            return viewer.attribute_combo.currentData() is not None or (
                self.last_attribute is not None and viewer.attribute_combo.findData(self.last_attribute) > 0)
        if name == "play":
            return viewer.morph_controls.play_button.isEnabled()
        if name.endswith("morph"):
            steps = viewer.morph_controls.steps
            direction = -1 if name.startswith("previous") else 1
            return steps.can_step(direction)
        if name.endswith("influence"):
            controls = viewer.attribute_controls
            direction = -1 if name.startswith("previous") else 1
            return (controls.view is not None and controls.view.is_weight
                    and controls.entry_steps.can_step(direction))
        return True

    def refresh_actions(self, *_args):
        for name, action in self.actions.items():
            action.setEnabled(self.enabled(name))
        self.viewer.attribute_toggle.setEnabled(self.enabled("surface"))

    def activate(self, name):
        if not self.viewer._closed and self.enabled(name):
            self.callbacks[name]()

    def _attribute_changed(self, *_args):
        if (name := self.viewer.attribute_combo.currentData()) is not None:
            self.last_attribute = name
        self.refresh_actions()

    def toggle_surface(self):
        combo = self.viewer.attribute_combo
        target = 0 if combo.currentData() is not None else combo.findData(self.last_attribute)
        if target >= 0:
            combo.setCurrentIndex(target)

    def lod_target(self, direction):
        combo = self.viewer.geometry_combo
        layer, lod = self.viewer._selected_geometry()
        return find_combo_data(combo, (layer, lod + direction), combo.currentIndex())

    def step_lod(self, direction):
        self.viewer.geometry_combo.setCurrentIndex(self.lod_target(direction))

    def step_influence(self, direction):
        controls = self.viewer.attribute_controls
        if controls.view is not None and controls.view.is_weight:
            controls.mode.setCurrentIndex(controls.mode.findData("influence"))
            controls.entry_steps.step(direction)

    def focus_search(self):
        if self.viewport._fullscreen_restore is not None:
            self.viewport._toggle_view_fullscreen()
        self.viewer.inspect_button.setChecked(True)
        self.inspector.filter_edit.setFocus()
        self.inspector.filter_edit.selectAll()

    def _first_result(self):
        tree = self.inspector.tree
        query = self.inspector.filter_edit.text().strip().casefold()
        for index in range(tree.topLevelItemCount()):
            parent = tree.topLevelItem(index)
            if parent.isHidden():
                continue
            item = parent if query in parent.text(0).casefold() else next(
                (parent.child(i) for i in range(parent.childCount()) if not parent.child(i).isHidden()), None)
            if item is not None:
                tree.clearSelection()
                tree.setCurrentItem(item)
                item.setSelected(True)
                tree.scrollToItem(item)
                tree.setFocus()
                return True
        return False

    def _context_menu(self, position):
        tree = self.inspector.tree
        item = tree.itemAt(position)
        if item is not None and not item.isSelected():
            tree.setCurrentItem(item)
        self.context_menu.popup(tree.viewport().mapToGlobal(position))

    def eventFilter(self, watched, event):
        if self.viewer._closed or event.type() not in (QEvent.KeyPress, QEvent.ShortcutOverride):
            return False
        key = event.key()
        modifiers = event.modifiers() & ~Qt.KeypadModifier
        searching = watched is self.inspector.filter_edit
        enter = (key in (Qt.Key_Return, Qt.Key_Enter)
                 and modifiers in (Qt.NoModifier, Qt.ControlModifier)
                 and watched in (self.inspector.tree, self.inspector.filter_edit))
        search_key = searching and modifiers == Qt.NoModifier and key in (Qt.Key_Escape, Qt.Key_Down)
        binding = self.bindings.get((modifiers, key))
        if binding is not None:
            name, viewport_only = binding
            if (searching and name != "search") or (viewport_only and watched is not self.viewport):
                binding = None
        if not (binding or search_key or enter):
            return False
        event.accept()
        if event.type() == QEvent.ShortcutOverride:
            return True
        if binding:
            if not event.isAutoRepeat() or binding[0] not in {"play", "isolate", "surface"}:
                self.activate(binding[0])
        elif key == Qt.Key_Escape:
            if self.inspector.filter_edit.text():
                self.inspector.filter_edit.clear()
            else:
                self.viewport.setFocus()
        elif not searching or self._first_result():
            if enter:
                self.inspector.frame_selection(isolate=modifiers == Qt.ControlModifier)
        return True

    def cleanup(self):
        self.menu.close()
        self.context_menu.close()
        for widget in (self.viewport, self.inspector.tree, self.inspector.filter_edit):
            widget.removeEventFilter(self)
