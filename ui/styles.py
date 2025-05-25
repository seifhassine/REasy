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
        QTreeView {{
            background-color: {colors['tree_bg']};
            color: {colors['fg']};
            border: 1px solid {colors['border']};
            selection-background-color: {colors['highlight']};
        }}
        QTreeView::item {{
            padding: 4px;
        }}
        QTreeView::item:selected {{
            background-color: {colors['highlight']};
        }}
        QTreeView::item:alternate {{
            background-color: {_get_alternate_color(colors)};
        }}
        QLineEdit, QPlainTextEdit, QTextEdit {{
            background-color: {colors['input_bg']};
            color: {colors['fg']};
            border: 1px solid {colors['border']};
            padding: 6px;
            border-radius: 4px;
        }}
        QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{
            border-color: {colors['highlight']};
        }}
        QComboBox {{
            background-color: {colors['input_bg']};
            color: {colors['fg']};
            border: 1px solid {colors['border']};
            padding: 6px;
            border-radius: 4px;
        }}
        QComboBox:focus {{
            border-color: {colors['highlight']};
        }}
        QComboBox::drop-down {{
            border: none;
        }}
        QComboBox::down-arrow {{
            width: 12px;
            height: 12px;
        }}
        QPushButton {{
            background-color: {colors['input_bg']};
            color: {colors['fg']};
            border: 1px solid {colors['border']};
            padding: 6px 12px;
            border-radius: 4px;
            min-width: 80px;
        }}
        QPushButton:hover {{
            background-color: {_get_hover_color(colors)};
            border-color: {colors['highlight']};
        }}
        QPushButton:pressed {{
            background-color: {_get_pressed_color(colors)};
        }}
        QPushButton:disabled {{
            background-color: {colors['disabled_bg']};
            color: {_get_disabled_text_color(colors)};
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
        QGroupBox {{
            font-weight: bold;
            border: 2px solid {colors['border']};
            border-radius: 6px;
            margin-top: 6px;
            padding-top: 6px;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 8px 0 8px;
            background-color: {colors['bg']};
        }}
        QFrame {{
            border: 1px solid {colors['border']};
            border-radius: 4px;
        }}
        QSplitter::handle {{
            background-color: {colors['border']};
        }}
        QSplitter::handle:horizontal {{
            width: 2px;
        }}
        QSplitter::handle:vertical {{
            height: 2px;
        }}
        QSpinBox {{
            background-color: {colors['input_bg']};
            color: {colors['fg']};
            border: 1px solid {colors['border']};
            padding: 4px;
            border-radius: 4px;
        }}
        QSpinBox:focus {{
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
        QStatusBar {{
            border-top: 1px solid {colors['border']};
            padding: 2px;
        }}
    """

def _get_alternate_color(colors: dict) -> str:
    """Get alternating row color"""
    if colors['bg'] == '#2b2b2b':  # Dark mode
        return '#353535'
    else:  # Light mode
        return '#f8f9fa'

def _get_hover_color(colors: dict) -> str:
    """Get button hover color"""
    if colors['bg'] == '#2b2b2b':  # Dark mode
        return '#4a4a4a'
    else:  # Light mode
        return '#e9ecef'

def _get_pressed_color(colors: dict) -> str:
    """Get button pressed color"""
    if colors['bg'] == '#2b2b2b':  # Dark mode
        return '#5a5a5a'
    else:  # Light mode
        return '#dee2e6'

def _get_disabled_text_color(colors: dict) -> str:
    """Get disabled text color"""
    if colors['bg'] == '#2b2b2b':  # Dark mode
        return '#666666'
    else:  # Light mode
        return '#6c757d'

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
