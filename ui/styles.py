
def get_color_scheme(dark_mode: bool) -> dict:
    """Get color scheme based on dark/light mode"""
    if dark_mode:
        return {
            'bg': '#2b2b2b',
            'tree_bg': '#2b2b2b',
            'fg': 'white',
            'highlight': 'rgba(255, 133, 51, 0.5)',
            'input_bg': '#3b3b3b',
            'disabled_bg': '#404040',
            'border': '#555555'
        }
    else:
        return {
            'bg': '#ffffff',
            'tree_bg': '#ffffff',
            'fg': '#000000',
            'highlight': '#ff851b',
            'input_bg': '#ffffff',
            'disabled_bg': '#f0f0f0',
            'border': '#cccccc'
        }

def get_main_stylesheet(colors: dict) -> str:
    """Generate main application stylesheet"""
    return f"""
        QMainWindow, QDialog, QWidget {{ 
            background-color: {colors['bg']}; 
            color: {colors['fg']}; 
        }}
        QTreeWidget {{
            background-color: {colors['tree_bg']};
            color: {colors['fg']};
            border: 1px solid {colors['border']};
        }}
        QTreeWidget::item:selected {{
            background-color: {colors['highlight']};
        }}
        QLineEdit, QPlainTextEdit {{
            background-color: {colors['input_bg']};
            color: {colors['fg']};
            border: 1px solid {colors['border']};
            padding: 2px;
        }}
        QPushButton {{
            background-color: {colors['input_bg']};
            color: {colors['fg']};
            border: 1px solid {colors['border']};
            padding: 5px;
            min-width: 80px;
        }}
        QPushButton:disabled {{
            background-color: {colors['disabled_bg']};
        }}
        QLabel, QCheckBox {{
            color: {colors['fg']};
        }}
        QCheckBox::indicator {{
            width: 15px;
            height: 15px;
            background-color: {colors['input_bg']};
            border: 1px solid {colors['border']};
            border-radius: 2px;
        }}
        QCheckBox::indicator:checked {{
            background-color: {colors['highlight']};
            border-color: {colors['highlight']};
        }}
        QMenuBar, QMenu, QTabWidget::pane, QStatusBar, QProgressDialog, QListWidget {{
            background-color: {colors['bg']};
            color: {colors['fg']};
            border: 1px solid {colors['border']};
        }}
        QMenuBar::item:selected, QMenu::item:selected, QTabBar::tab:selected, QListWidget::item:selected {{
            background-color: {colors['highlight']};
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

def get_notebook_stylesheet(colors: dict) -> str:
    """Generate notebook stylesheet"""
    return f"""
        QTabWidget {{
            background-color: {colors['bg']};
        }}
        QTabWidget::pane {{ 
            border: none;
            margin: 0px;
            padding: 0px;
        }}
        QTabWidget::tab-bar {{
            left: 0px;
        }}
    """

def get_status_bar_stylesheet() -> str:
    """Generate status bar stylesheet"""
    return """
        QStatusBar {
            padding: 0;
            margin: 0;
            border: none;
            min-height: 1px;
            max-height: 1px;
        }
    """
