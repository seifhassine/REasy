from PySide6.QtWidgets import QPlainTextEdit
from PySide6.QtCore import Qt, Signal, QObject


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
        self.setReadOnly(True)
        self.setMaximumBlockCount(5000)
        self.document().setDocumentMargin(0) 
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)  
        self.setContentsMargins(0, 0, 0, 0)  
        self.setStyleSheet("""
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
            }
        """)

    def write(self, text):
        """Write text to the console, ensuring each message ends with a newline"""
        self.insertPlainText(text)
        self.ensureCursorVisible()

    def flush(self):
        """Required for file-like behavior"""
        pass
