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

def get_main_status_bar_stylesheet() -> str:
    """Generate main status bar stylesheet with borders"""
    return """
        QStatusBar {
            margin: 0;
            padding: 0;
            border-top: 1px solid #cccccc;
        }
        QStatusBar::item {
            border: none;
        }
    """

def get_console_stylesheet() -> str:
    """Generate console logger stylesheet"""
    return """
        QPlainTextEdit {
            background-color: #000000;
            color: #00FF00;
            font-family: 'Consolas', 'Courier New', monospace;
            font-size: 10pt;
            margin: 0;
            padding: 0;
            border: none;
        }
        QPlainTextEdit QScrollBar:vertical {
            width: 12px;
            margin: 0;
            background-color: #333333;
            border: none;
        }
        QPlainTextEdit QScrollBar::handle:vertical {
            background-color: #666666;
            border: none;
            border-radius: 3px;
        }
        QPlainTextEdit QScrollBar::handle:vertical:hover {
            background-color: #888888;
        }
    """

def get_color_input_stylesheet(colors: dict = None) -> str:
    """Generate color input QLineEdit stylesheet"""
    if colors is None:
        # Default colors for color input styling
        border_color = "#888888"
        focus_color = "#aaaaaa"
        bg_color = "white"
        fg_color = "black"
    else:
        border_color = colors['border']
        focus_color = colors['highlight']
        bg_color = colors['input_bg']
        fg_color = colors['fg']
    
    return f"""
        QLineEdit {{
            margin: 0;
            padding: 1px 2px;
            border: 1px solid {border_color};
            background-color: {bg_color};
            color: {fg_color};
        }}
        QLineEdit:focus {{
            border: 1px solid {focus_color};
        }}
        QLineEdit[invalid="true"] {{
            border: 1px solid red;
        }}
    """

def get_star_rating_stylesheet(filled: bool = True, readonly: bool = False) -> str:
    """Generate star rating button stylesheet"""
    if filled:
        base_style = "QPushButton { border: none; color: #ffd700; }"
        if readonly:
            return base_style
        else:
            return base_style + "QPushButton:hover { color: #ffcc00; border: 1px solid #ffcc00; }"
    else:
        base_style = "QPushButton { border: none; color: #ccc; }"
        if readonly:
            return base_style
        else:
            return base_style + "QPushButton:hover { color: #ffcc00; border: 1px solid #ffcc00; }"

def get_resource_indicator_stylesheet() -> str:
    """Generate resource indicator stylesheet"""
    return "color: yellow; padding: 2px; border-radius: 2px;"

def get_bold_label_stylesheet() -> str:
    """Generate bold label stylesheet"""
    return "font-weight: bold;"

def get_title_label_stylesheet() -> str:
    """Generate title label stylesheet"""
    return "font-size: 16pt; font-weight: bold;text-align: center;"

def get_header_label_stylesheet() -> str:
    """Generate header label stylesheet"""
    return "font-size: 24px; font-weight: bold;"

def get_error_label_stylesheet() -> str:
    """Generate error label stylesheet"""
    return "color: red;"

def get_info_label_stylesheet() -> str:
    """Generate info label stylesheet"""
    return "color: blue;"

def get_success_label_stylesheet() -> str:
    """Generate success label stylesheet"""
    return "color: green;"

def get_muted_label_stylesheet() -> str:
    """Generate muted label stylesheet"""
    return "color: #888; font-size: 14px;"

def get_comment_frame_stylesheet() -> str:
    """Generate comment frame stylesheet"""
    return "border-radius: 5px;"

def get_info_frame_stylesheet() -> str:
    """Generate info frame stylesheet"""
    return "border-radius: 5px; padding: 10px;"

def get_validation_error_stylesheet() -> str:
    """Generate validation error stylesheet"""
    return "border: 1px solid red;"

def get_text_label_padding_stylesheet() -> str:
    """Generate text label padding stylesheet"""
    return "padding-left: 2px;"

def get_label_padding_stylesheet() -> str:
    """Generate label padding stylesheet"""
    return "padding-right: 2px;"

def get_container_margin_stylesheet() -> str:
    """Generate container margin stylesheet"""
    return "margin-right: 6px;"

def get_radius_input_stylesheet() -> str:
    """Generate radius input stylesheet"""
    return "margin-left: 6px;"

def get_color_button_stylesheet(r: int, g: int, b: int, a: float = 1.0, border_color: str = "#888888") -> str:
    """Generate color button stylesheet with RGBA values"""
    return f"background-color: rgba({r}, {g}, {b}, {a}); border: 1px solid {border_color};"

def get_color_button_rgb_stylesheet(r: int, g: int, b: int, border_color: str = "#888888") -> str:
    """Generate color button stylesheet with RGB values"""
    return f"background-color: rgb({r}, {g}, {b}); border: 1px solid {border_color};"

def get_checkbox_widget_stylesheet() -> str:
    """Generate checkbox widget-specific stylesheet"""
    return """
        QCheckBox {
            padding: 2px;
        }
        QCheckBox::indicator {
            width: 16px;
            height: 16px;
        }
    """

def get_margin_left_2px_stylesheet() -> str:
    """Generate margin-left: 2px stylesheet"""
    return "margin-left: 2px;"

def get_margin_left_6px_stylesheet() -> str:
    """Generate margin-left: 6px stylesheet"""
    return "margin-left: 6px;"

def get_colorinput_label_stylesheet() -> str:
    """Generate color input label stylesheet"""
    return """
        QLabel {
            margin: 0;
            padding: 0;
            border: none;
        }
    """

def get_muted_text_stylesheet() -> str:
    """Generate muted text stylesheet"""
    return "color: #888;"

def get_filetab_tree_stylesheet() -> str:
    """Generate file tab tree stylesheet"""
    return """
        QTreeView {
            border: none;
            margin: 0px;
            padding: 0px;
        }
    """

def get_filetab_tree_color_stylesheet(colors: dict) -> str:
    """Generate file tab tree color-specific stylesheet"""
    return f"""
        QTreeView {{
            background-color: {colors['tree_bg']};
            color: {colors['fg']};
        }}
        QTreeView::item {{
            background-color: {colors['tree_bg']};
            color: {colors['fg']};
            padding: 2px;
        }}
    """
