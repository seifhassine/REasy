from PySide6.QtWidgets import QHBoxLayout, QPlainTextEdit, QToolButton, QWidget
from PySide6.QtCore import QSize, Qt, Signal, QObject

lazy from ui.editor_widgets import styled_label, tool_button
lazy from ui.icons import line_icon
lazy from ui.styles import current_colors, set_style_role


class ConsoleRedirector(QObject):
    """Redirects stdout/stderr to a ConsoleWidget"""
    text_written = Signal(str)

    def __init__(self, console_widget, original_stream=None):
        super().__init__()
        self.console = console_widget
        self.original_stream = original_stream
        self.text_written.connect(self.console.write)

    def write(self, text):
        """Write to both console and original stream"""
        self.text_written.emit(text)
        if self.original_stream:
            self.original_stream.write(text)

    def flush(self):
        """Required for file-like behavior"""
        if self.original_stream:
            self.original_stream.flush()


class ConsoleWidget(QPlainTextEdit):
    """A QPlainTextEdit widget that acts as a console output window"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("outputConsole")
        self.setReadOnly(True)
        self.setMaximumBlockCount(5000)
        self.document().setDocumentMargin(4)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setContentsMargins(0, 0, 0, 0)
        self.setProperty("mono", True)
        colors = current_colors()
        self.setStyleSheet(
            f"QPlainTextEdit#outputConsole {{ background-color: {colors['input_bg']}; color: {colors['text']};"
            f" border: none; border-radius: 0px; padding: 0px; }}"
        )

    def write(self, text):
        """Write text to the console, ensuring each message ends with a newline"""
        self.insertPlainText(text)
        self.ensureCursorVisible()

    def flush(self):
        """Required for file-like behavior"""
        pass

    def set_wrapped(self, wrapped: bool):
        self.setLineWrapMode(QPlainTextEdit.WidgetWidth if wrapped else QPlainTextEdit.NoWrap)


class OutputTitleBar(QWidget):
    """Unhandled mouse events go to the dock, which drags and floats it."""

    def __init__(self, dock, console: ConsoleWidget):
        super().__init__(dock)
        self.setAttribute(Qt.WA_StyledBackground, True)
        set_style_role(self, surface="toolbar")
        title = styled_label(dock.windowTitle(), self, heading="panel")

        wrap = self._button("wrap", self.tr("Wrap long lines"))
        wrap.setCheckable(True)
        wrap.setChecked(console.lineWrapMode() != QPlainTextEdit.NoWrap)
        wrap.toggled.connect(console.set_wrapped)
        clear = self._button("delete", self.tr("Clear output"))
        clear.clicked.connect(console.clear)
        self.float_button = self._button("float", self.tr("Float"))
        self.float_button.clicked.connect(self._toggle_floating)
        close = self._button("close", self.tr("Close"))
        close.clicked.connect(dock.close)
        dock.topLevelChanged.connect(self._sync_floating)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 3, 2)
        layout.setSpacing(2)
        layout.addWidget(title, 1)
        layout.addWidget(wrap)
        layout.addWidget(clear)
        layout.addSpacing(10)
        layout.addWidget(self.float_button)
        layout.addWidget(close)

    def _button(self, icon: str, tooltip: str) -> QToolButton:
        button = tool_button(icon, tooltip, parent=self)
        button.setIconSize(QSize(14, 14))
        return button

    def _toggle_floating(self):
        dock = self.parentWidget()  # not stored: the dock holds this bar
        dock.setFloating(not dock.isFloating())

    def _sync_floating(self, floating: bool):
        self.float_button.setIcon(line_icon("dock" if floating else "float"))
        tooltip = self.tr("Dock") if floating else self.tr("Float")
        self.float_button.setToolTip(tooltip)
        self.float_button.setAccessibleName(tooltip)
