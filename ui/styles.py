from PySide6.QtGui import QColor

from settings import DEFAULT_SETTINGS


def get_color_scheme(accent_color: str | None = None) -> dict:
    """Return the shared dark color scheme with the requested accent."""
    accent = QColor(accent_color or DEFAULT_SETTINGS["tree_highlight_color"])
    if not accent.isValid():
        accent = QColor(DEFAULT_SETTINGS["tree_highlight_color"])
    highlight = f"rgba({accent.red()}, {accent.green()}, {accent.blue()}, 0.5)"
    return {
        'accent': accent.name(),
        'bg': '#2b2b2b',
        'tree_bg': '#2b2b2b',
        'fg': 'white',
        'highlight': highlight,
        'input_bg': '#3b3b3b',
        'disabled_bg': '#404040',
        'border': '#555555'
    }

def get_main_stylesheet(colors: dict) -> str:
    """Generate the main application stylesheet."""
    return f"""
        QMainWindow, QDialog, QWidget {{
            background-color: {colors['bg']}; color: {colors['fg']};
        }}
        QTreeView {{
            background-color: {colors['tree_bg']}; color: {colors['fg']};
            border: 1px solid {colors['border']};
        }}
        QTreeView::item:selected {{ background-color: {colors['highlight']}; }}
        QLineEdit, QPlainTextEdit {{
            background-color: {colors['input_bg']}; color: {colors['fg']};
            border: 1px solid {colors['border']}; padding: 2px;
        }}
        QPushButton {{
            background-color: {colors['input_bg']}; color: {colors['fg']};
            border: 1px solid {colors['border']}; padding: 5px; min-width: 80px;
        }}
        QPushButton:disabled {{ background-color: {colors['disabled_bg']}; }}
        QLabel, QCheckBox {{ color: {colors['fg']}; }}
        QCheckBox::indicator {{
            width: 15px; height: 15px; background-color: {colors['input_bg']};
            border: 1px solid {colors['border']}; border-radius: 2px;
        }}
        QCheckBox::indicator:checked {{
            background-color: {colors['highlight']}; border-color: {colors['highlight']};
        }}
        QMenuBar, QMenu, QTabWidget::pane, QStatusBar, QProgressDialog, QListWidget {{
            background-color: {colors['bg']}; color: {colors['fg']};
            border: 1px solid {colors['border']};
        }}
        QMenuBar::item:selected, QMenu::item:selected, QTabBar::tab:selected, QListWidget::item:selected {{
            background-color: {colors['highlight']};
        }}
        QTabWidget#documentNotebook {{
            background-color: {colors['bg']};
        }}
        QTabWidget#documentNotebook::pane {{
            background-color: {colors['bg']};
            border: none;
            border-top: 1px solid #3b3c40;
            margin: 0px;
            padding: 0px;
        }}
        QTabWidget#documentNotebook::tab-bar {{
            left: 4px;
        }}
        QTabBar#documentTabBar {{
            background-color: #232427;
            border: none;
        }}
        QTabBar#documentTabBar::tab {{
            background-color: #28292d;
            color: #b9bbc0;
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
            background-color: #323338;
            color: #eef0f3;
        }}
        QTabBar#documentTabBar::tab:selected {{
            background-color: {colors['bg']};
            color: #f4f5f7;
            border-color: #45464b;
            border-top-color: {colors['accent']};
            border-bottom-color: {colors['bg']};
            margin-top: 1px;
        }}
        QTabBar#documentTabBar QToolButton {{
            background-color: transparent;
            color: #b9bbc0;
            border: none;
            border-radius: 6px;
            padding: 1px;
        }}
        QTabBar#documentTabBar QToolButton:hover {{
            background-color: #3d3e43;
            color: #f4f5f7;
        }}
    """

def get_tree_stylesheet(colors: dict) -> str:
    """Generate tree widget stylesheet"""
    return f"""
        QTreeWidget {{
            background-color: {colors['tree_bg']};
            color: {colors['fg']};
            padding-top: 0px;
            margin-top: 0px;
            padding-right: 0px;
            margin-right: 0px;
            border: none;
        }}
        QTreeWidget::item {{
            background-color: {colors['tree_bg']};
            color: {colors['fg']};
            padding: 2px;
            padding-right: 0px;
        }}
        QTreeWidget::item:selected {{ 
            background-color: {colors['highlight']} !important;
        }}
        QTreeWidget::branch {{
            padding-right: 0px;
        }}
    """

