from PySide6.QtCore import QDir
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QProxyStyle, QStyle, QStyleFactory

lazy from settings import DEFAULT_SETTINGS
lazy from ui.icons import line_icon
lazy from utils.app_paths import resource_path

MONO_FONT = "Consolas"
_current_colors: dict | None = None

_STANDARD_ICONS = {
    QStyle.SP_DirIcon: "folder",
    QStyle.SP_DirClosedIcon: "folder",
    QStyle.SP_DirLinkIcon: "folder",
    QStyle.SP_DirOpenIcon: "folder_open",
    QStyle.SP_DialogOpenButton: "folder_open",
    QStyle.SP_FileIcon: "file",
    QStyle.SP_FileLinkIcon: "link",
    QStyle.SP_TrashIcon: "delete",
    QStyle.SP_ArrowBack: "arrow_left",
    QStyle.SP_ArrowLeft: "arrow_left",
    QStyle.SP_FileDialogBack: "arrow_left",
    QStyle.SP_ArrowForward: "arrow_right",
    QStyle.SP_ArrowRight: "arrow_right",
    QStyle.SP_ArrowUp: "arrow_up",
    QStyle.SP_FileDialogToParent: "arrow_up",
    QStyle.SP_ArrowDown: "arrow_down",
    QStyle.SP_DialogResetButton: "reset",
    QStyle.SP_RestoreDefaultsButton: "reset",
    QStyle.SP_BrowserReload: "refresh",
    QStyle.SP_BrowserStop: "stop",
    QStyle.SP_MediaPlay: "play",
    QStyle.SP_MediaPause: "pause",
    QStyle.SP_MediaStop: "stop",
    QStyle.SP_MediaSkipBackward: "skip_back",
    QStyle.SP_MediaSkipForward: "skip_forward",
    QStyle.SP_MediaVolume: "sound",
    QStyle.SP_MediaVolumeMuted: "mute",
    QStyle.SP_FileDialogNewFolder: "new_folder",
    QStyle.SP_DialogSaveButton: "save",
    QStyle.SP_FileDialogDetailedView: "details",
    QStyle.SP_FileDialogContentsView: "details",
    QStyle.SP_FileDialogListView: "list",
    QStyle.SP_FileDialogInfoView: "info",
    QStyle.SP_DesktopIcon: "home",
    QStyle.SP_DialogApplyButton: "check",
    QStyle.SP_DialogOkButton: "check",
    QStyle.SP_DialogYesButton: "check",
    QStyle.SP_DialogNoButton: "close",
    QStyle.SP_DialogCancelButton: "close",
    QStyle.SP_DialogCloseButton: "close",
    QStyle.SP_DialogDiscardButton: "close",
    QStyle.SP_TitleBarCloseButton: "close",
    QStyle.SP_DockWidgetCloseButton: "close",
    QStyle.SP_LineEditClearButton: "close",
    QStyle.SP_TitleBarNormalButton: "float",
    QStyle.SP_DialogHelpButton: "question",
    QStyle.SP_MessageBoxInformation: "info",
    QStyle.SP_MessageBoxQuestion: "question",
    QStyle.SP_MessageBoxWarning: "warning",
    QStyle.SP_MessageBoxCritical: "error",
}
_STANDARD_ICON_TONES = {
    QStyle.SP_MessageBoxInformation: "info",
    QStyle.SP_MessageBoxQuestion: "info",
    QStyle.SP_MessageBoxWarning: "warning",
    QStyle.SP_MessageBoxCritical: "danger",
}


class ReasyStyle(QProxyStyle):
    """Fusion with REasy's line icons in place of the standard pixmaps."""

    def __init__(self):
        super().__init__(QStyleFactory.create("Fusion"))

    def standardIcon(self, standard_icon, option=None, widget=None):
        name = _STANDARD_ICONS.get(standard_icon)
        if name is None:
            return super().standardIcon(standard_icon, option, widget)
        tone = _STANDARD_ICON_TONES.get(standard_icon)
        return line_icon(name, current_colors()[tone] if tone else None)


def _rgba(color: QColor, alpha: float) -> str:
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {alpha})"


def _mix(a: str, b: QColor, amount: float) -> str:
    base = QColor(a)
    return QColor(
        round(base.red() + (b.red() - base.red()) * amount),
        round(base.green() + (b.green() - base.green()) * amount),
        round(base.blue() + (b.blue() - base.blue()) * amount),
    ).name()


def _luminance(color: QColor) -> float:
    def channel(value: float) -> float:
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(color.redF()) + 0.7152 * channel(color.greenF()) + 0.0722 * channel(color.blueF())


def _readable_on(color: QColor, background: str) -> QColor:
    """`color` lightened until it reaches 4.5:1 contrast on `background`."""
    back = _luminance(QColor(background)) + 0.05
    for _ in range(12):
        if (_luminance(color) + 0.05) / back >= 4.5:
            break
        color = QColor.fromHslF(color.hslHueF(), color.hslSaturationF(), min(1.0, color.lightnessF() + 0.05))
    return color


def get_color_scheme(accent_color: str | None = None) -> dict:
    """Return REasy's semantic dark-theme tokens with compatibility aliases."""
    accent = QColor(accent_color or DEFAULT_SETTINGS["tree_highlight_color"])
    if not accent.isValid():
        accent = QColor(DEFAULT_SETTINGS["tree_highlight_color"])
    # Dark text when it contrasts more than white (WCAG relative luminance).
    luminance = _luminance(accent)
    light_accent = (luminance + 0.05) / 0.05 > 1.05 / (luminance + 0.05)
    colors = {
        "accent": accent.name(),
        "accent_hover": accent.lighter(115).name(),
        "accent_pressed": accent.darker(120).name(),
        "accent_text": "#0c0d0f" if light_accent else "#ffffff",
        "accent_fg": _readable_on(QColor(accent), "#1f2023").name(),
        "accent_soft": _rgba(accent, 0.18),
        "accent_border": _rgba(accent, 0.65),
        "focus": accent.name(),
        "gutter": "#101113",
        "chrome_bg": "#17181b",
        "window_bg": "#1f2023",
        "editor_bg": "#1f2023",
        "panel": "#1f2023",
        "sidebar_bg": "#1a1b1e",
        "view_bg": "#1a1b1e",
        "view_alt": "#1d1e21",
        "header_bg": "#25262a",
        "menu_bg": "#212226",
        "surface": "#25262a",
        "surface_alt": "#2d2e33",
        "surface_hover": "#36383d",
        "surface_active": "#424449",
        "button_bg": "#2d2e33",
        "button_border": "#3b3d43",
        "button_hover": "#36383d",
        "button_pressed": "#232427",
        "input_bg": "#111215",
        "input_border": "#2f3136",
        "readonly_bg": "#18191c",
        "tab_bar_bg": "#17181b",
        "tab_bg": "#17181b",
        "tab_active_bg": "#1f2023",
        "row_hover": "rgba(255, 255, 255, 0.045)",
        "scroll_handle": "#3a3c42",
        "scroll_handle_hover": "#50535a",
        "text": "#d5d7db",
        "text_bright": "#f3f4f6",
        "text_muted": "#a2a5ac",
        "text_subtle": "#777a82",
        "text_disabled": "#595c63",
        "border": "#393b41",
        "border_subtle": "#2a2c30",
        "border_strong": "#4e5158",
        "selection": _rgba(accent, 0.38),
        "selection_solid": _mix("#1a1b1e", accent, 0.42),
        "selection_inactive": "rgba(140, 144, 152, 0.22)",
        "danger": "#e5534b",
        "warning": "#e0a43a",
        "success": "#4cc38a",
        "info": "#58a6ff",
        "canvas_bg": "#151619",
        "canvas_grid_minor": "#1c1d21",
        "canvas_grid_major": "#25272c",
        "node_bg": "#232428",
        "node_header": "#2c2d32",
        "node_border": "#3a3c42",
        "wire": "#7c808a",
        "mono_font": MONO_FONT,
        "check_icon": "check_dark.svg" if light_accent else "check_light.svg",
        "dash_icon": "dash_dark.svg" if light_accent else "dash_light.svg",
    }
    colors.update({
        "bg": colors["editor_bg"],
        "tree_bg": colors["sidebar_bg"],
        "fg": colors["text"],
        "highlight": _rgba(accent, 0.5),
        "disabled_bg": colors["surface_active"],
    })
    for tone in ("danger", "warning", "success", "info"):
        tone_color = QColor(colors[tone])
        colors[f"{tone}_soft"] = _rgba(tone_color, 0.13)
        colors[f"{tone}_border"] = _rgba(tone_color, 0.55)
    return colors


def current_colors() -> dict:
    """Tokens of the theme the main window applied last."""
    return _current_colors or get_color_scheme()


def qcolor(token: str) -> QColor:
    """QColor for a token value, including the rgba(r, g, b, alpha 0-1) form."""
    if token.startswith("rgba("):
        red, green, blue, alpha = (part.strip() for part in token[5:-1].split(","))
        color = QColor(int(red), int(green), int(blue))
        color.setAlphaF(float(alpha))
        return color
    return QColor(token)


def set_style_role(widget, **properties) -> None:
    """Set dynamic style properties (variant, tone, heading, banner, surface, mono) and repolish."""
    for name, value in properties.items():
        widget.setProperty(name, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


def build_palette(colors: dict) -> QPalette:
    palette = QPalette()
    roles = {
        QPalette.Window: colors["panel"],
        QPalette.WindowText: colors["text"],
        QPalette.Base: colors["view_bg"],
        QPalette.AlternateBase: colors["view_alt"],
        QPalette.ToolTipBase: colors["menu_bg"],
        QPalette.ToolTipText: colors["text_bright"],
        QPalette.PlaceholderText: colors["text_subtle"],
        QPalette.Text: colors["text"],
        QPalette.Button: colors["button_bg"],
        QPalette.ButtonText: colors["text"],
        QPalette.BrightText: colors["text_bright"],
        QPalette.Light: colors["surface_active"],
        QPalette.Midlight: colors["surface_hover"],
        QPalette.Mid: colors["border"],
        QPalette.Dark: colors["gutter"],
        QPalette.Shadow: "#000000",
        QPalette.Highlight: colors["selection_solid"],
        QPalette.HighlightedText: colors["text_bright"],
        QPalette.Link: colors["accent_fg"],
        QPalette.LinkVisited: colors["accent_pressed"],
        QPalette.Accent: colors["accent"],
    }
    for role, value in roles.items():
        palette.setColor(role, QColor(value))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        palette.setColor(QPalette.Disabled, role, QColor(colors["text_disabled"]))
    palette.setColor(QPalette.Disabled, QPalette.Base, QColor(colors["panel"]))
    palette.setColor(QPalette.Disabled, QPalette.Button, QColor(colors["panel"]))
    palette.setColor(QPalette.Inactive, QPalette.Highlight, QColor(colors["selection_solid"]))
    return palette


def apply_theme(app, colors: dict) -> None:
    """Install the palette and stylesheet app-wide so dialogs and floating windows share them."""
    global _current_colors
    QDir.setSearchPaths("theme", [str(resource_path("resources/icons/theme"))])
    _current_colors = colors
    app.setPalette(build_palette(colors))
    app.setStyleSheet(get_main_stylesheet(colors))


def get_main_stylesheet(colors: dict) -> str:
    c = colors
    tones = ("info", "success", "warning", "danger")
    badge_rules = "".join(
        f"""
        QLabel[badge="{tone}"] {{
            background: {c[f'{tone}_soft']}; color: {c[tone]}; border: 1px solid {c[f'{tone}_border']};
        }}"""
        for tone in tones
    )
    tone_rules = "".join(f"""
        QLabel[tone="{tone}"] {{ color: {c[tone]}; }}""" for tone in tones)
    banner_rules = "".join(
        f"""
        QFrame[banner="{tone}"], QLabel[banner="{tone}"] {{
            background-color: {c[f'{tone}_soft']}; border: none; border-left: 3px solid {c[tone]};
            border-radius: 2px; padding: 4px 8px; color: {c['text']};
        }}"""
        for tone in tones
    )
    return f"""
        QWidget {{
            background-color: {c['panel']}; color: {c['text']};
            selection-background-color: {c['selection']}; selection-color: {c['text_bright']};
        }}
        QMainWindow, QDialog {{ background-color: {c['panel']}; }}
        QMainWindow::separator {{ background: {c['gutter']}; width: 4px; height: 4px; }}
        QMainWindow::separator:hover {{ background: {c['accent_border']}; }}
        QLabel, QCheckBox, QRadioButton {{ background: transparent; color: {c['text']}; }}
        QLabel:disabled, QCheckBox:disabled, QRadioButton:disabled {{ color: {c['text_disabled']}; }}
        QLabel#rcolPath {{ color: {c['text_muted']}; }}
        QScrollArea {{ border: none; }}
        QFrame[frameShape="4"] {{ border: none; background: {c['border_subtle']}; max-height: 1px; }}
        QFrame[frameShape="5"] {{ border: none; background: {c['border_subtle']}; max-width: 1px; }}

        /* Item views */
        QAbstractItemView {{
            background-color: {c['view_bg']}; alternate-background-color: {c['view_alt']};
            color: {c['text']}; border: 1px solid {c['border_subtle']}; outline: none;
        }}
        QAbstractItemView::item:hover {{ background-color: {c['row_hover']}; }}
        QAbstractItemView::item:selected {{
            background-color: {c['selection_solid']}; color: {c['text_bright']};
        }}
        QTreeView {{ show-decoration-selected: 1; }}
        QTreeView::branch:selected {{ background-color: {c['selection_solid']}; }}
        QTreeView::branch:has-children:!has-siblings:closed,
        QTreeView::branch:closed:has-children:has-siblings {{
            border-image: none; image: url(theme:chevron_right.svg);
        }}
        QTreeView::branch:open:has-children:!has-siblings,
        QTreeView::branch:open:has-children:has-siblings {{
            border-image: none; image: url(theme:chevron_down.svg);
        }}
        QTableView {{ gridline-color: {c['border_subtle']}; }}
        QHeaderView {{ background-color: {c['header_bg']}; border: none; }}
        QHeaderView::section {{
            background-color: {c['header_bg']}; color: {c['text_muted']};
            border: none; border-right: 1px solid {c['border_subtle']};
            border-bottom: 1px solid {c['border_subtle']}; padding: 4px 8px;
            font-weight: 600;
        }}
        QHeaderView::section:hover {{ background-color: {c['surface_hover']}; color: {c['text']}; }}
        QHeaderView::up-arrow {{ image: url(theme:chevron_up.svg); width: 10px; height: 10px; }}
        QHeaderView::down-arrow {{ image: url(theme:chevron_down.svg); width: 10px; height: 10px; }}
        QTableCornerButton::section {{
            background-color: {c['header_bg']}; border: none;
            border-right: 1px solid {c['border_subtle']}; border-bottom: 1px solid {c['border_subtle']};
        }}

        /* Inputs */
        QLineEdit, QPlainTextEdit, QTextEdit, QAbstractSpinBox, QComboBox {{
            background-color: {c['input_bg']}; color: {c['text']};
            border: 1px solid {c['input_border']}; border-radius: 3px; padding: 3px 6px;
            selection-background-color: {c['selection_solid']};
        }}
        QPlainTextEdit, QTextEdit {{ padding: 2px; }}
        QLineEdit:hover, QPlainTextEdit:hover, QTextEdit:hover,
        QAbstractSpinBox:hover, QComboBox:hover {{ border-color: {c['border_strong']}; }}
        QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
        QAbstractSpinBox:focus, QComboBox:focus, QComboBox:on {{ border-color: {c['focus']}; }}
        QLineEdit:read-only, QPlainTextEdit:read-only, QTextEdit:read-only,
        QPlainTextEdit[readOnly="true"], QTextEdit[readOnly="true"] {{
            background-color: {c['readonly_bg']};
        }}
        QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled,
        QAbstractSpinBox:disabled, QComboBox:disabled {{
            background-color: {c['panel']}; color: {c['text_disabled']}; border-color: {c['border_subtle']};
        }}
        QLineEdit[mono="true"], QPlainTextEdit[mono="true"], QTextEdit[mono="true"],
        QLabel[mono="true"], QAbstractItemView[mono="true"] {{ font-family: "{c['mono_font']}"; }}
        QComboBox {{ padding-right: 22px; }}
        QComboBox::drop-down {{
            subcontrol-origin: padding; subcontrol-position: center right;
            width: 20px; border: none; background: transparent;
        }}
        QComboBox::down-arrow {{ image: url(theme:chevron_down.svg); width: 12px; height: 12px; }}
        QComboBox::down-arrow:disabled {{ image: url(theme:chevron_down_disabled.svg); }}
        QComboBox QAbstractItemView {{
            background-color: {c['menu_bg']}; border: 1px solid {c['border']};
            selection-background-color: {c['selection']}; outline: none; padding: 2px;
        }}
        QAbstractSpinBox {{ padding-right: 18px; }}
        QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
            subcontrol-origin: border; width: 16px; border: none;
            border-left: 1px solid {c['input_border']}; background: transparent;
        }}
        QAbstractSpinBox::up-button {{ subcontrol-position: top right; border-top-right-radius: 3px; }}
        QAbstractSpinBox::down-button {{ subcontrol-position: bottom right; border-bottom-right-radius: 3px; }}
        QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{
            background: {c['surface_hover']};
        }}
        QAbstractSpinBox::up-arrow {{ image: url(theme:chevron_up.svg); width: 10px; height: 10px; }}
        QAbstractSpinBox::down-arrow {{ image: url(theme:chevron_down.svg); width: 10px; height: 10px; }}
        QAbstractSpinBox::up-arrow:disabled, QAbstractSpinBox::up-arrow:off {{
            image: url(theme:chevron_up_disabled.svg);
        }}
        QAbstractSpinBox::down-arrow:disabled, QAbstractSpinBox::down-arrow:off {{
            image: url(theme:chevron_down_disabled.svg);
        }}
        QAbstractSpinBox[buttonSymbols="NoButtons"] {{ padding-right: 6px; }}
        QAbstractSpinBox[buttonSymbols="NoButtons"]::up-button,
        QAbstractSpinBox[buttonSymbols="NoButtons"]::down-button {{
            subcontrol-origin: border; subcontrol-position: left; width: 0px; border: none;
        }}

        /* Buttons */
        QPushButton {{
            background-color: {c['button_bg']}; color: {c['text']};
            border: 1px solid {c['button_border']}; border-radius: 3px;
            padding: 4px 12px; min-height: 16px;
        }}
        QPushButton:hover {{
            background-color: {c['button_hover']}; border-color: {c['border_strong']}; color: {c['text_bright']};
        }}
        QPushButton:pressed {{ background-color: {c['button_pressed']}; }}
        QPushButton:checked {{
            background-color: {c['accent_soft']}; border-color: {c['accent_border']}; color: {c['text_bright']};
        }}
        QPushButton:disabled {{
            background-color: {c['panel']}; color: {c['text_disabled']}; border-color: {c['border_subtle']};
        }}
        QPushButton:flat {{ background: transparent; border-color: transparent; }}
        QPushButton:flat:hover {{ background: {c['surface_hover']}; }}
        QPushButton[compact="true"] {{ padding: 2px 8px; }}
        QDialogButtonBox QPushButton {{ min-width: 64px; }}
        QDialogButtonBox QPushButton:default, QPushButton[variant="primary"] {{
            background-color: {c['accent']}; border-color: {c['accent']}; color: {c['accent_text']};
            font-weight: 600;
        }}
        QDialogButtonBox QPushButton:default:hover, QPushButton[variant="primary"]:hover {{
            background-color: {c['accent_hover']}; border-color: {c['accent_hover']};
        }}
        QDialogButtonBox QPushButton:default:pressed, QPushButton[variant="primary"]:pressed {{
            background-color: {c['accent_pressed']}; border-color: {c['accent_pressed']};
        }}
        QPushButton[variant="danger"] {{ color: {c['danger']}; }}
        QPushButton[variant="danger"]:hover {{
            background-color: {c['danger_soft']}; border-color: {c['danger_border']};
        }}
        QPushButton[variant="primary"]:disabled, QPushButton[variant="danger"]:disabled,
        QDialogButtonBox QPushButton:default:disabled {{
            background-color: {c['panel']}; color: {c['text_disabled']}; border-color: {c['border_subtle']};
        }}
        QPushButton[variant="ghost"] {{ background: transparent; border-color: transparent; }}
        QPushButton[variant="ghost"]:hover {{ background: {c['surface_hover']}; }}
        QPushButton::menu-indicator {{
            image: url(theme:chevron_down.svg); subcontrol-origin: padding;
            subcontrol-position: center right; width: 10px; height: 10px; right: -6px;
        }}
        QToolButton {{
            background-color: {c['button_bg']}; color: {c['text']};
            border: 1px solid {c['button_border']}; border-radius: 3px; padding: 3px;
        }}
        QToolButton[autoRaise="true"] {{ background: transparent; border-color: transparent; }}
        QToolButton:hover {{ background-color: {c['button_hover']}; border-color: {c['border_strong']}; }}
        QToolButton[autoRaise="true"]:hover {{ background-color: {c['surface_hover']}; border-color: transparent; }}
        QToolButton:pressed {{ background-color: {c['button_pressed']}; }}
        QToolButton:checked {{
            background-color: {c['accent_soft']}; border-color: {c['accent_border']}; color: {c['text_bright']};
        }}
        QToolButton:disabled {{
            color: {c['text_disabled']}; background-color: {c['panel']}; border-color: {c['border_subtle']};
        }}
        QToolButton[autoRaise="true"]:disabled {{ background: transparent; border-color: transparent; }}
        QToolButton[popupMode="1"] {{ padding-right: 14px; }}
        QToolButton::menu-button {{ border: none; width: 14px; }}
        QToolButton::menu-indicator, QToolButton::menu-arrow {{
            image: url(theme:chevron_down.svg); width: 10px; height: 10px;
        }}
        QToolButton::menu-indicator {{ subcontrol-origin: padding; subcontrol-position: bottom right; }}
        QToolButton::menu-indicator:disabled, QToolButton::menu-arrow:disabled,
        QPushButton::menu-indicator:disabled {{ image: url(theme:chevron_down_disabled.svg); }}
        QToolButton[popupMode="2"] {{ padding-right: 18px; }}
        QToolButton[popupMode="2"]::menu-indicator {{ subcontrol-position: center right; right: 5px; }}
        QGraphicsView {{ border: none; }}

        /* Check boxes and radio buttons */
        QCheckBox, QRadioButton {{ spacing: 6px; }}
        QCheckBox::indicator, QGroupBox::indicator, QAbstractItemView::indicator {{
            width: 14px; height: 14px; border-radius: 3px;
            border: 1px solid {c['border_strong']}; background-color: {c['input_bg']};
        }}
        QCheckBox::indicator:hover, QGroupBox::indicator:hover, QAbstractItemView::indicator:hover {{
            border-color: {c['accent']};
        }}
        QCheckBox::indicator:checked, QGroupBox::indicator:checked, QAbstractItemView::indicator:checked {{
            background-color: {c['accent']}; border-color: {c['accent']};
            image: url(theme:{c['check_icon']});
        }}
        QCheckBox::indicator:indeterminate, QAbstractItemView::indicator:indeterminate {{
            background-color: {c['accent']}; border-color: {c['accent']};
            image: url(theme:{c['dash_icon']});
        }}
        QCheckBox::indicator:disabled, QGroupBox::indicator:disabled, QAbstractItemView::indicator:disabled {{
            background-color: {c['panel']}; border-color: {c['border_subtle']};
        }}
        QCheckBox::indicator:checked:disabled, QAbstractItemView::indicator:checked:disabled {{
            background-color: {c['surface_active']}; border-color: {c['surface_active']};
            image: url(theme:check_light.svg);
        }}
        QRadioButton::indicator {{
            width: 14px; height: 14px; border-radius: 8px;
            border: 1px solid {c['border_strong']}; background-color: {c['input_bg']};
        }}
        QRadioButton::indicator:hover {{ border-color: {c['accent']}; }}
        QRadioButton::indicator:checked {{
            background-color: {c['accent_text']}; border: 4px solid {c['accent']}; width: 8px; height: 8px;
        }}
        QRadioButton::indicator:disabled {{ background-color: {c['panel']}; border-color: {c['border_subtle']}; }}

        /* Group boxes */
        QGroupBox {{
            background: transparent; border: 1px solid {c['border_subtle']}; border-radius: 4px;
            margin-top: 16px; padding: 8px 6px 6px 6px;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin; subcontrol-position: top left; left: 8px;
            padding: 0px 4px; color: {c['text_muted']}; font-weight: 600; background: transparent;
        }}

        /* Menus */
        QMenuBar {{
            background-color: {c['chrome_bg']}; color: {c['text']};
            border-bottom: 1px solid {c['gutter']}; padding: 2px 2px;
        }}
        QMenuBar::item {{ background: transparent; padding: 4px 9px; border-radius: 3px; }}
        QMenuBar::item:selected {{ background-color: {c['surface_hover']}; color: {c['text_bright']}; }}
        QMenuBar::item:pressed {{ background-color: {c['surface_active']}; }}
        QMenu {{
            background-color: {c['menu_bg']}; color: {c['text']};
            border: 1px solid {c['border']}; padding: 4px;
        }}
        QMenu::item {{ background: transparent; padding: 5px 28px 5px 26px; border-radius: 3px; }}
        QMenu::item:selected {{ background-color: {c['selection']}; color: {c['text_bright']}; }}
        QMenu::item:disabled {{ color: {c['text_disabled']}; }}
        QMenu::separator {{ height: 1px; background: {c['border_subtle']}; margin: 4px 6px; }}
        QMenu::right-arrow {{ image: url(theme:chevron_right.svg); width: 12px; height: 12px; }}
        QMenu::indicator {{ width: 14px; height: 14px; left: 6px; }}
        QMenu::indicator:non-exclusive:checked, QMenu::indicator:exclusive:checked {{
            image: url(theme:check_light.svg);
        }}
        QToolTip {{
            background-color: {c['menu_bg']}; color: {c['text_bright']};
            border: 1px solid {c['border']}; padding: 4px 6px;
        }}

        /* Tabs */
        QTabWidget::pane {{
            background-color: {c['panel']}; border: none;
            border-top: 1px solid {c['border_subtle']}; top: -1px;
        }}
        QTabBar {{ background: transparent; qproperty-drawBase: 0; }}
        QTabBar::tab {{
            background: transparent; color: {c['text_muted']}; border: none;
            padding: 5px 12px; min-width: 32px;
        }}
        QTabBar::tab:top {{ border-bottom: 2px solid transparent; }}
        QTabBar::tab:bottom {{ border-top: 2px solid transparent; }}
        QTabBar::tab:left {{ border-right: 2px solid transparent; }}
        QTabBar::tab:right {{ border-left: 2px solid transparent; }}
        QTabBar::tab:hover {{ color: {c['text']}; background-color: {c['row_hover']}; }}
        QTabBar::tab:selected {{ color: {c['text_bright']}; }}
        QTabBar::tab:top:selected {{ border-bottom-color: {c['accent']}; }}
        QTabBar::tab:bottom:selected {{ border-top-color: {c['accent']}; }}
        QTabBar::tab:left:selected {{ border-right-color: {c['accent']}; }}
        QTabBar::tab:right:selected {{ border-left-color: {c['accent']}; }}
        QTabBar::tab:disabled {{ color: {c['text_disabled']}; }}
        QTabBar::close-button {{ image: url(theme:close.svg); subcontrol-position: right; border-radius: 3px; }}
        QTabBar::close-button:hover {{ image: url(theme:close_hover.svg); background: {c['surface_hover']}; }}
        QTabBar QToolButton {{ background-color: {c['chrome_bg']}; border: none; border-radius: 0px; }}

        QTabWidget#documentNotebook, QTabWidget#documentNotebook::pane {{
            background-color: {c['editor_bg']};
        }}
        QTabWidget#documentNotebook::pane {{
            border: none; border-top: 1px solid {c['border_subtle']}; margin: 0px; padding: 0px;
        }}
        QTabWidget#documentNotebook::tab-bar {{ left: 0px; }}
        QTabBar#documentTabBar {{ background-color: {c['tab_bar_bg']}; border: none; }}
        QTabBar#documentTabBar::tab {{
            background-color: transparent; color: {c['text_muted']};
            border: none; border-top: 2px solid transparent; border-right: 1px solid {c['gutter']};
            border-top-left-radius: 4px; border-top-right-radius: 4px;
            min-width: 92px; max-width: 230px; min-height: 18px;
            margin: 3px 0px 0px 0px; padding: 4px 8px 5px 10px;
        }}
        QTabBar#documentTabBar::tab:first {{ margin-left: 4px; }}
        QTabBar#documentTabBar::tab:hover:!selected {{
            background-color: {c['surface']}; color: {c['text']};
        }}
        QTabBar#documentTabBar::tab:selected {{
            background-color: {c['tab_active_bg']}; color: {c['text_bright']};
            border-top-color: {c['accent']}; border-right-color: transparent;
        }}
        QTabBar#documentTabBar QToolButton {{
            background-color: transparent; color: {c['text_muted']};
            border: none; border-radius: 3px; padding: 1px;
        }}
        QTabBar#documentTabBar QToolButton:hover {{
            background-color: {c['surface_active']}; color: {c['text']};
        }}

        /* Docks, splitters, status bar */
        QDockWidget {{
            color: {c['text_muted']};
            titlebar-close-icon: url(theme:close.svg); titlebar-normal-icon: url(theme:float.svg);
        }}
        QDockWidget::title {{
            background: {c['header_bg']}; border-bottom: 1px solid {c['gutter']};
            padding: 5px 8px; text-align: left;
        }}
        QDockWidget::close-button, QDockWidget::float-button {{
            border: none; background: transparent; border-radius: 3px; padding: 1px;
        }}
        QDockWidget::close-button:hover, QDockWidget::float-button:hover {{
            background: {c['surface_hover']};
        }}
        QSplitter::handle {{ background: {c['gutter']}; }}
        QSplitter::handle:hover {{ background: {c['accent_border']}; }}
        QStatusBar {{
            background: {c['chrome_bg']}; color: {c['text_muted']};
            border-top: 1px solid {c['gutter']};
        }}
        QStatusBar::item {{ border: none; }}
        QStatusBar QLabel {{ color: {c['text_muted']}; }}
        QProgressDialog {{ background-color: {c['panel']}; }}

        /* Scroll bars, progress bars, sliders */
        QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0px; border: none; }}
        QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0px; border: none; }}
        QScrollBar::handle:vertical {{
            background: {c['scroll_handle']}; min-height: 28px; border-radius: 3px; margin: 2px;
        }}
        QScrollBar::handle:horizontal {{
            background: {c['scroll_handle']}; min-width: 28px; border-radius: 3px; margin: 2px;
        }}
        QScrollBar::handle:hover {{ background: {c['scroll_handle_hover']}; }}
        QScrollBar::handle:pressed {{ background: {c['text_subtle']}; }}
        QScrollBar::add-line, QScrollBar::sub-line {{
            width: 0px; height: 0px; border: none; background: none;
        }}
        QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
        QAbstractScrollArea::corner {{ background: transparent; border: none; }}
        QWidget#qt_scrollarea_vcontainer, QWidget#qt_scrollarea_hcontainer {{ background: transparent; }}
        QProgressBar {{
            background-color: {c['input_bg']}; color: {c['text']};
            border: 1px solid {c['border_subtle']}; border-radius: 3px;
            text-align: center; min-height: 14px;
        }}
        QProgressBar::chunk {{ background-color: {c['accent']}; border-radius: 2px; }}
        QSlider {{ background: transparent; }}
        QSlider::groove:horizontal {{ height: 4px; background: {c['input_bg']}; border-radius: 2px; }}
        QSlider::groove:vertical {{ width: 4px; background: {c['input_bg']}; border-radius: 2px; }}
        QSlider::sub-page:horizontal, QSlider::add-page:vertical {{
            background: {c['accent']}; border-radius: 2px;
        }}
        QSlider::handle:horizontal {{
            width: 12px; height: 12px; margin: -4px 0px; border-radius: 6px;
            background: {c['text']}; border: 1px solid {c['gutter']};
        }}
        QSlider::handle:vertical {{
            width: 12px; height: 12px; margin: 0px -4px; border-radius: 6px;
            background: {c['text']}; border: 1px solid {c['gutter']};
        }}
        QSlider::handle:hover {{ background: {c['text_bright']}; }}
        QSlider::sub-page:horizontal:disabled, QSlider::add-page:vertical:disabled {{
            background: {c['surface_active']};
        }}

        /* Semantic roles (set_style_role) */
        QLineEdit[invalid="true"], QAbstractSpinBox[invalid="true"], QComboBox[invalid="true"],
        QPlainTextEdit[invalid="true"], QTextEdit[invalid="true"] {{ border-color: {c['danger']}; }}
        QPushButton[variant="link"], QToolButton[variant="link"] {{
            color: {c['accent_fg']}; background: transparent; border: none;
            padding: 2px 4px; min-height: 0px;
        }}
        QPushButton[variant="link"]:hover, QToolButton[variant="link"]:hover {{ color: {c['accent_hover']}; }}
        QPushButton[variant="link"]:disabled, QToolButton[variant="link"]:disabled {{ color: {c['text_disabled']}; }}
        QToolButton[variant="primary"] {{
            background-color: {c['accent']}; border-color: {c['accent']}; color: {c['accent_text']};
        }}
        QToolButton[variant="primary"]:hover {{ background-color: {c['accent_hover']}; }}
        QLabel[badge] {{
            border-radius: 3px; padding: 1px 6px; font-size: 11px; font-weight: 600;
        }}
        QLabel[badge="neutral"] {{
            background: {c['surface_alt']}; color: {c['text_muted']}; border: 1px solid {c['border_subtle']};
        }}
        QLabel[badge="accent"] {{
            background: {c['accent_soft']}; color: {c['text_bright']}; border: 1px solid {c['accent_border']};
        }}
{badge_rules}
        QLabel[dropZone="true"], QFrame[dropZone="true"] {{
            background: {c['input_bg']}; color: {c['text_muted']};
            border: 1px dashed {c['border_strong']}; border-radius: 4px; padding: 12px;
        }}
        QLabel[dropZone="true"][dragActive="true"], QFrame[dropZone="true"][dragActive="true"] {{
            background: {c['accent_soft']}; color: {c['text_bright']}; border-color: {c['accent']};
        }}
        QWidget[surface="header"] {{
            background-color: {c['header_bg']}; border-bottom: 1px solid {c['border_subtle']};
        }}
        QLabel[heading="panel"] {{ color: {c['text']}; font-weight: 600; }}
        QLabel[heading="title"] {{ color: {c['text_bright']}; font-size: 15px; font-weight: 600; }}
        QLabel[heading="section"] {{ color: {c['text_bright']}; font-weight: 600; }}
        QLabel[heading="caption"] {{
            color: {c['text_subtle']}; font-size: 11px; font-weight: 700; letter-spacing: 0.6px;
        }}
        QLabel[tone="muted"] {{ color: {c['text_muted']}; }}
        QLabel[tone="subtle"] {{ color: {c['text_subtle']}; }}
        QLabel[tone="accent"] {{ color: {c['accent_fg']}; }}
{tone_rules}
        QLabel[emptyState="true"] {{
            background: {c['view_bg']}; color: {c['text_muted']}; border: 1px solid {c['border_subtle']};
            padding: 16px; qproperty-alignment: AlignCenter;
        }}
{banner_rules}
        QWidget[surface="toolbar"] {{
            background-color: {c['header_bg']}; border-bottom: 1px solid {c['gutter']};
        }}
        QWidget[surface="chrome"] {{ background-color: {c['chrome_bg']}; }}
        QWidget[surface="view"] {{ background-color: {c['view_bg']}; }}
        QWidget[surface="card"] {{
            background-color: {c['surface']}; border: 1px solid {c['border_subtle']}; border-radius: 4px;
        }}
        QWidget[surface="recessed"] {{
            background-color: {c['input_bg']}; border: 1px solid {c['border_subtle']}; border-radius: 3px;
        }}
        QWidget[surface="footer"] {{
            background-color: {c['chrome_bg']}; border-top: 1px solid {c['gutter']};
        }}
        QWidget[surface="section"] {{
            background-color: {c['panel']}; border: none; border-bottom: 1px solid {c['gutter']};
        }}
        QWidget[surface="toolbar"] QFrame[frameShape="5"], QWidget[surface="header"] QFrame[frameShape="5"] {{
            background: {c['border']};
        }}
        QToolButton[variant="disclosure"], QPushButton[variant="disclosure"] {{
            background: transparent; border: none; color: {c['text_muted']};
            padding: 2px 6px 2px 2px; font-weight: 600;
        }}
        QToolButton[variant="disclosure"]:hover, QPushButton[variant="disclosure"]:hover {{
            background: {c['surface_hover']}; color: {c['text']};
        }}
        QLineEdit[compact="true"] {{ padding: 1px 4px; }}
        QAbstractSpinBox[scrub="true"] {{ padding-right: 6px; }}
        QAbstractSpinBox[scrub="true"]::up-button, QAbstractSpinBox[scrub="true"]::down-button {{
            width: 0px; border: none;
        }}
        QAbstractSpinBox[scrub="true"]::up-arrow, QAbstractSpinBox[scrub="true"]::down-arrow {{
            image: none; width: 0px; height: 0px;
        }}
        QProgressBar[thin="true"] {{
            min-height: 0px; max-height: 3px; border: none; border-radius: 1px; background: {c['input_bg']};
        }}
        QProgressBar[thin="true"]::chunk {{ border-radius: 1px; }}

        /* Viewers */
        QWidget#meshViewer,
        QWidget#msgViewer,
        QWidget#cfilViewer,
        QWidget#texViewer,
        QWidget#uvsViewer,
        QWidget#motbankViewer,
        QWidget#mcambankViewer,
        QWidget#welViewer,
        QWidget#uvarViewer,
        QWidget#mdfViewer,
        QWidget#mmtrViewer,
        QWidget#sdfViewer,
        QWidget#rszViewer {{
            background-color: {c['editor_bg']};
        }}
        QWidget#mdfViewer QTabWidget,
        QWidget#mdfViewer QTabWidget::pane,
        QWidget#mdfViewer QStackedWidget {{
            background-color: {c['editor_bg']};
        }}
        QWidget#soundViewer,
        QWidget#soundViewer QSplitter,
        QWidget#soundViewer QWidget[reasySoundSurface="true"],
        QWidget#soundViewer QTabWidget#soundEditorTabs,
        QWidget#soundViewer QTabWidget#soundEditorTabs::pane,
        QWidget#soundViewer QTabWidget#soundEditorTabs QStackedWidget {{
            background-color: {c['editor_bg']};
        }}

        /* Project browser */
        QWidget#projectBrowserTitleBar {{
            background-color: {c['header_bg']}; border: none; border-bottom: 1px solid {c['gutter']};
        }}
        QWidget#projectBrowserTitleBar QLabel,
        QWidget#projectBrowserTitleBar QToolButton {{
            background: transparent;
        }}
        QWidget#projectBrowserBody {{ border: none; }}

        /* Breadcrumbs */
        QFrame#editorBreadcrumbs {{
            background: {c['editor_bg']}; border-bottom: 1px solid {c['border_subtle']};
        }}
        QToolButton#breadcrumbButton {{
            background: transparent; color: {c['text_muted']}; border: none;
            border-radius: 3px; padding: 2px 5px;
        }}
        QToolButton#breadcrumbButton:hover {{
            background: {c['surface_hover']}; color: {c['text']};
        }}
    """


def get_tree_stylesheet(colors: dict) -> str:
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
            background-color: {colors['row_hover']};
        }}
        QTreeView::item:selected, QTreeWidget::item:selected {{
            background-color: {colors['selection_solid']} !important;
        }}
        QTreeView::branch:has-children:!has-siblings:closed,
        QTreeView::branch:closed:has-children:has-siblings {{
            border-image: none; image: url(theme:chevron_right.svg); padding-right: 0px;
        }}
        QTreeView::branch:open:has-children:!has-siblings,
        QTreeView::branch:open:has-children:has-siblings {{
            border-image: none; image: url(theme:chevron_down.svg); padding-right: 0px;
        }}
        QTreeView QLabel, QTreeWidget QLabel,
        QTreeView QCheckBox, QTreeWidget QCheckBox {{
            background-color: transparent;
        }}
    """
