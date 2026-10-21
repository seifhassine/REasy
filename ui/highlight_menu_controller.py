lazy from PySide6.QtWidgets import QColorDialog
lazy from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QPainter, QPen, QPixmap
lazy from PySide6.QtCore import QCoreApplication, QModelIndex, Qt

lazy from ui.highlight_utils import model_index_row_path
lazy from ui.icons import line_icon
lazy from ui.styles import current_colors


def _swatch_icon(color):
    colors = current_colors()
    icon = QIcon()
    for state, ring, width in (
        (QIcon.Off, colors["border_strong"], 1),
        (QIcon.On, colors["text_bright"], 2),
    ):
        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.fillRect(2, 2, 12, 12, color)
        pen = QPen(QColor(ring), width)
        pen.setJoinStyle(Qt.MiterJoin)
        painter.setPen(pen)
        inset = 2 if width == 1 else 1
        painter.drawRect(inset, inset, 15 - 2 * inset, 15 - 2 * inset)
        painter.end()
        icon.addPixmap(pixmap, QIcon.Normal, state)
        icon.addPixmap(pixmap, QIcon.Active, state)
    return icon


class HighlightMenuController:
    def __init__(self, main_window):
        self.main_window = main_window
        self.highlight_menu = None
        self.highlight_toggle_action = None
        self._color_actions = []

    def create_menu(self, menubar):
        self.highlight_menu = menubar.addMenu(
            QCoreApplication.translate("HighlightMenuController", "Highlight")
        )

        self.highlight_toggle_action = QAction(
            QCoreApplication.translate("HighlightMenuController", "Enable Highlight Mode"),
            self.main_window,
        )
        self.highlight_toggle_action.setCheckable(True)
        self.highlight_toggle_action.setToolTip(QCoreApplication.translate(
            "HighlightMenuController",
            "Toggle tree node text highlighting (click nodes to highlight)",
        ))
        self.highlight_toggle_action.triggered.connect(self._on_highlight_toggle)
        self.highlight_menu.addAction(self.highlight_toggle_action)

        self.highlight_menu.addSeparator()

        color_menu = self.highlight_menu.addMenu(
            QCoreApplication.translate("HighlightMenuController", "Select Color")
        )
        color_menu.setIcon(line_icon("palette"))

        colors = [
            (QCoreApplication.translate("HighlightMenuController", "Orange"), QColor(255, 165, 0)),
            (QCoreApplication.translate("HighlightMenuController", "Red"), QColor(255, 0, 0)),
            (QCoreApplication.translate("HighlightMenuController", "Green"), QColor(0, 255, 0)),
            (QCoreApplication.translate("HighlightMenuController", "Blue"), QColor(0, 150, 255)),
            (QCoreApplication.translate("HighlightMenuController", "Yellow"), QColor(255, 255, 0)),
            (QCoreApplication.translate("HighlightMenuController", "Magenta"), QColor(255, 0, 255)),
            (QCoreApplication.translate("HighlightMenuController", "Cyan"), QColor(0, 255, 255)),
        ]

        color_group = QActionGroup(self.main_window)
        color_group.setExclusionPolicy(QActionGroup.ExclusionPolicy.ExclusiveOptional)
        for color_name, color in colors:
            color_action = QAction(_swatch_icon(color), color_name, self.main_window)
            color_action.setCheckable(True)
            color_action.setActionGroup(color_group)
            color_action.triggered.connect(lambda checked, c=color: self._set_highlight_color(c))
            color_menu.addAction(color_action)
            self._color_actions.append((color, color_action))

        color_menu.addSeparator()

        custom_color_action = QAction(
            QCoreApplication.translate("HighlightMenuController", "Custom Color..."),
            self.main_window,
        )
        custom_color_action.triggered.connect(self._choose_custom_highlight_color)
        color_menu.addAction(custom_color_action)

        self.highlight_menu.menuAction().setVisible(False)

    def _sync_color_checks(self, current_color):
        for color, action in self._color_actions:
            action.setChecked(color == current_color)

    def _on_highlight_toggle(self):
        enabled = self.highlight_toggle_action.isChecked()
        current_tab = self.main_window.get_active_tab()

        if current_tab and hasattr(current_tab, 'highlight_manager'):
            current_tab.highlight_manager.set_enabled(enabled)

            if hasattr(current_tab, 'tree') and current_tab.tree:
                current_tab.tree.viewport().update()
            if hasattr(current_tab, 'viewer') and current_tab.viewer and hasattr(current_tab.viewer, 'tree'):
                current_tab.viewer.tree.viewport().update()

    def _set_highlight_color(self, color):
        current_tab = self.main_window.get_active_tab()
        if current_tab and hasattr(current_tab, 'highlight_manager'):
            current_tab.highlight_manager.set_highlight_color(color)
            self._sync_color_checks(current_tab.highlight_manager.highlight_color)

            if hasattr(current_tab, 'viewer') and current_tab.viewer and hasattr(current_tab.viewer, 'tree'):
                self._refresh_all_highlights(current_tab.viewer.tree, current_tab.highlight_manager)
            elif hasattr(current_tab, 'tree') and current_tab.tree:
                self._refresh_all_highlights(current_tab.tree, current_tab.highlight_manager)

    def _choose_custom_highlight_color(self):
        current_tab = self.main_window.get_active_tab()
        if not current_tab or not hasattr(current_tab, 'highlight_manager'):
            return

        current_color = current_tab.highlight_manager.highlight_color
        color = QColorDialog.getColor(
            current_color,
            self.main_window,
            QCoreApplication.translate("HighlightMenuController", "Choose Highlight Color"),
        )
        if color.isValid():
            self._set_highlight_color(color)

    def update_menu_visibility(self, is_rsz):
        if self.highlight_menu:
            self.highlight_menu.menuAction().setVisible(is_rsz)

        current_tab = self.main_window.get_active_tab()
        if current_tab and hasattr(current_tab, 'highlight_manager') and is_rsz:
            self.highlight_toggle_action.setChecked(current_tab.highlight_manager.enabled)
            self._sync_color_checks(current_tab.highlight_manager.highlight_color)

    def _refresh_all_highlights(self, tree, highlight_manager):
        if not tree or not highlight_manager:
            return

        highlighted_items = list(highlight_manager._highlighted_items)

        if hasattr(tree, 'indexWidget'):
            model = tree.model()
            if model:
                self._refresh_tree_recursive(tree, model, QModelIndex(), highlighted_items)

        tree.viewport().update()

    def _refresh_tree_recursive(self, tree, model, parent_index, highlighted_items):
        rows = model.rowCount(parent_index)
        for row in range(rows):
            index = model.index(row, 0, parent_index)
            if not index.isValid():
                continue

            item_id = model_index_row_path(index)

            if item_id in highlighted_items:
                if hasattr(tree, '_update_widget_highlight'):
                    tree._update_widget_highlight(index, True)

            if model.hasChildren(index):
                self._refresh_tree_recursive(tree, model, index, highlighted_items)
