from PySide6.QtGui import QColor

from settings import DEFAULT_SETTINGS


def get_color_scheme(accent_color: str | None = None) -> dict:
    """Return REasy's semantic dark-theme tokens with compatibility aliases."""
    accent = QColor(accent_color or DEFAULT_SETTINGS["tree_highlight_color"])
    if not accent.isValid():
        accent = QColor(DEFAULT_SETTINGS["tree_highlight_color"])
    selection = f"rgba({accent.red()}, {accent.green()}, {accent.blue()}, 0.38)"
    legacy_highlight = f"rgba({accent.red()}, {accent.green()}, {accent.blue()}, 0.5)"
    colors = {
        "accent": accent.name(),
        "focus": accent.name(),
        "window_bg": "#1e1f22",
        "editor_bg": "#25262a",
        "sidebar_bg": "#232427",
        "surface": "#2b2d31",
        "surface_alt": "#313338",
        "surface_hover": "#383a40",
        "surface_active": "#404249",
        "input_bg": "#292b2f",
        "tab_bar_bg": "#202125",
        "tab_bg": "#28292d",
        "tab_active_bg": "#25262a",
        "text": "#f1f3f5",
        "text_muted": "#b5bac1",
        "text_subtle": "#858b94",
        "text_disabled": "#666b73",
        "border": "#46484f",
        "border_subtle": "#35373c",
        "selection": selection,
        "selection_inactive": "rgba(120, 124, 132, 0.28)",
        "danger": "#f14c4c",
        "warning": "#cca700",
        "success": "#4ec9b0",
        "info": "#75beff",
    }
    colors.update({
        "bg": colors["editor_bg"],
        "tree_bg": colors["sidebar_bg"],
        "fg": colors["text"],
        "highlight": legacy_highlight,
        "disabled_bg": colors["surface_active"],
    })
    return colors

def get_main_stylesheet(colors: dict) -> str:
    """Generate the main application stylesheet."""
    return f"""
        QMainWindow, QDialog, QWidget {{
            background-color: {colors['window_bg']}; color: {colors['text']};
        }}
        QTreeView, QTreeWidget, QTableView, QListWidget {{
            background-color: {colors['sidebar_bg']}; color: {colors['text']};
            border: 1px solid {colors['border_subtle']};
            outline: none;
        }}
        QTreeView::item:hover, QTreeWidget::item:hover, QTableView::item:hover,
        QListWidget::item:hover {{ background-color: {colors['surface_hover']}; }}
        QTreeView::item:selected, QTreeWidget::item:selected,
        QTableView::item:selected, QListWidget::item:selected {{
            background-color: {colors['selection']};
        }}
        QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox {{
            background-color: {colors['input_bg']}; color: {colors['text']};
            border: 1px solid {colors['border']}; padding: 3px 5px;
            selection-background-color: {colors['selection']};
        }}
        QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus {{
            border-color: {colors['focus']};
        }}
        QPushButton {{
            background-color: {colors['surface_alt']}; color: {colors['text']};
            border: 1px solid {colors['border']}; padding: 5px; min-width: 80px;
        }}
        QPushButton:hover {{ background-color: {colors['surface_hover']}; }}
        QPushButton:pressed {{ background-color: {colors['surface_active']}; }}
        QPushButton:disabled {{ background-color: {colors['disabled_bg']}; }}
        QPushButton[compact="true"] {{
            padding: 3px 8px; min-width: 0px;
        }}
        QLabel, QCheckBox {{ color: {colors['text']}; }}
        QLabel#rcolPath {{ color: {colors['text_muted']}; }}
        QWidget#meshViewer,
        QWidget#msgViewer,
        QWidget#cfilViewer,
        QWidget#texViewer,
        QWidget#uvsViewer,
        QWidget#motbankViewer,
        QWidget#mdfViewer,
        QWidget#rszViewer {{
            background-color: {colors['window_bg']};
        }}
        QWidget#mdfViewer QTabWidget,
        QWidget#mdfViewer QTabWidget::pane,
        QWidget#mdfViewer QStackedWidget {{
            background-color: {colors['window_bg']};
        }}
        QWidget#rszSceneBar {{
            background-color: {colors['window_bg']};
        }}
        QWidget#soundViewer,
        QWidget#soundViewer QLabel,
        QWidget#soundViewer QGroupBox,
        QWidget#soundViewer QSplitter,
        QWidget#soundViewer QWidget[reasySoundSurface="true"],
        QWidget#soundViewer QTabWidget#soundEditorTabs,
        QWidget#soundViewer QTabWidget#soundEditorTabs::pane,
        QWidget#soundViewer QTabWidget#soundEditorTabs QStackedWidget,
        QWidget#soundViewer QTabWidget#soundEditorTabs > QTabBar,
        QWidget#soundViewer QTreeView,
        QWidget#soundViewer QTableView,
        QWidget#soundViewer QListWidget {{
            background-color: {colors['window_bg']};
        }}
        QCheckBox::indicator {{
            width: 15px; height: 15px; background-color: {colors['input_bg']};
            border: 1px solid {colors['border']}; border-radius: 2px;
        }}
        QCheckBox::indicator:checked {{
            background-color: {colors['highlight']}; border-color: {colors['highlight']};
        }}
        QMenuBar, QMenu, QStatusBar, QProgressDialog {{
            background-color: {colors['window_bg']}; color: {colors['text']};
            border-color: {colors['border_subtle']};
        }}
        QMenu::item {{ padding: 5px 28px 5px 24px; }}
        QMenu::separator {{ height: 1px; background: {colors['border_subtle']}; margin: 4px 8px; }}
        QToolTip {{
            background-color: {colors['surface_active']}; color: {colors['text']};
            border: 1px solid {colors['border']}; padding: 4px;
        }}
        QDockWidget {{ color: {colors['text_muted']}; }}
        QDockWidget::title {{
            background: {colors['sidebar_bg']}; border-bottom: 1px solid {colors['border_subtle']};
            padding: 5px 8px; text-align: left;
        }}
        QSplitter::handle {{ background: {colors['border_subtle']}; }}
        QSplitter::handle:hover {{ background: {colors['accent']}; }}
        QStatusBar {{
            background: {colors['sidebar_bg']}; border-top: 1px solid {colors['border_subtle']};
        }}
        QWidget#projectBrowserTitleBar {{
            background-color: {colors['sidebar_bg']};
            border: 1px solid {colors['border']};
        }}
        QWidget#projectBrowserTitleBar QLabel,
        QWidget#projectBrowserTitleBar QToolButton {{
            background: transparent;
        }}
        QWidget#projectBrowserBody {{
            border-left: 1px solid {colors['border']};
            border-right: 1px solid {colors['border']};
            border-bottom: 1px solid {colors['border']};
        }}
        QMenuBar::item:selected, QMenu::item:selected {{
            background-color: {colors['selection']};
        }}
        QTabWidget#documentNotebook {{
            background-color: {colors['editor_bg']};
        }}
        QTabWidget#documentNotebook::pane {{
            background-color: {colors['editor_bg']};
            border: none;
            border-top: 1px solid {colors['border_subtle']};
            margin: 0px;
            padding: 0px;
        }}
        QTabWidget#documentNotebook::tab-bar {{
            left: 4px;
        }}
        QTabBar#documentTabBar {{
            background-color: {colors['tab_bar_bg']};
            border: none;
        }}
        QTabBar#documentTabBar::tab {{
            background-color: {colors['tab_bg']};
            color: {colors['text_muted']};
            border: 1px solid transparent;
            border-bottom: none;
            border-top-left-radius: 7px;
            border-top-right-radius: 7px;
            min-width: 92px;
            max-width: 230px;
            min-height: 18px;
            margin: 3px 1px 0px 0px;
            padding: 5px 8px;
        }}
        QTabBar#documentTabBar::tab:first {{
            margin-left: 4px;
        }}
        QTabBar#documentTabBar::tab:hover:!selected {{
            background-color: {colors['surface_hover']};
            color: {colors['text']};
        }}
        QTabBar#documentTabBar::tab:selected {{
            background-color: {colors['tab_active_bg']};
            color: {colors['text']};
            border-color: {colors['border']};
            border-top-color: {colors['accent']};
            border-bottom-color: {colors['tab_active_bg']};
            margin-top: 1px;
        }}
        QTabBar#documentTabBar QToolButton {{
            background-color: transparent;
            color: {colors['text_muted']};
            border: none;
            border-radius: 6px;
            padding: 1px;
        }}
        QTabBar#documentTabBar QToolButton:hover {{
            background-color: {colors['surface_active']};
            color: {colors['text']};
        }}
        QFrame#editorBreadcrumbs {{
            background: {colors['sidebar_bg']};
            border-bottom: 1px solid {colors['border_subtle']};
        }}
        QToolButton#breadcrumbButton {{
            background: transparent; color: {colors['text_muted']}; border: none;
            border-radius: 4px; padding: 2px 5px; min-width: 0px;
        }}
        QToolButton#breadcrumbButton:hover {{
            background: {colors['surface_hover']}; color: {colors['text']};
        }}
        QLabel#breadcrumbSeparator, QLabel#breadcrumbDivider {{
            background: transparent; color: {colors['text_subtle']};
        }}
        QHeaderView::section {{
            background: {colors['sidebar_bg']}; color: {colors['text_muted']};
            border: none; border-right: 1px solid {colors['border_subtle']};
            border-bottom: 1px solid {colors['border_subtle']}; padding: 4px 6px;
        }}
    """

def get_tree_stylesheet(colors: dict) -> str:
    """Generate the shared tree view/widget stylesheet."""
    return f"""
        QTreeView, QTreeWidget {{
            background-color: {colors['tree_bg']};
            color: {colors['fg']};
            padding-top: 0px;
            margin-top: 0px;
            padding-right: 0px;
            margin-right: 0px;
            border: none;
        }}
        QTreeView::item, QTreeWidget::item {{
            background-color: transparent;
            color: {colors['fg']};
            padding: 2px;
            padding-right: 0px;
        }}
        QTreeView::item:hover, QTreeWidget::item:hover {{
            background-color: {colors['surface_hover']};
        }}
        QTreeView::item:selected, QTreeWidget::item:selected {{
            background-color: {colors['highlight']} !important;
        }}
        QTreeView::branch, QTreeWidget::branch {{
            padding-right: 0px;
        }}
        QTreeView QLabel, QTreeWidget QLabel,
        QTreeView QCheckBox, QTreeWidget QCheckBox {{
            background-color: transparent;
        }}
    """

