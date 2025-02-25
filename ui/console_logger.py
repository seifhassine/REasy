import logging
import tkinter as tk
from PySide6.QtWidgets import QPlainTextEdit
from PySide6.QtCore import Qt, Signal, QObject


class TextHandler(logging.Handler):
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget

    def emit(self, record):
        msg = self.format(record) + "\n"

        def append():
            self.text_widget.configure(state="normal")
            self.text_widget.insert(tk.END, msg)
            self.text_widget.configure(state="disabled")
            self.text_widget.yview(tk.END)

        self.text_widget.after(0, append)


def setup_console_logging(text_widget):
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)  
    for handler in logger.handlers[:]:
        if isinstance(handler, TextHandler):
            logger.removeHandler(handler)
    handler = TextHandler(text_widget)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)


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


class StdoutRedirector:
    def __init__(self, text_widget):
        self.text_widget = text_widget

    def write(self, message):
        if message.strip() == "":
            return
        self.text_widget.configure(state="normal")
        self.text_widget.insert(tk.END, message)
        self.text_widget.configure(state="disabled")
        self.text_widget.see(tk.END)

    def flush(self):
        pass


class ConsoleWidget(QPlainTextEdit):
    """A QPlainTextEdit widget that acts as a console output window"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(5000)
        self.setStyleSheet("""
            QPlainTextEdit {
                background-color: #000000;
                color: #00FF00;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 10pt;
            }
        """)

    def write(self, text):
        """Write text to the console, ensuring each message ends with a newline"""
        self.insertPlainText(text)
        self.ensureCursorVisible()

    def flush(self):
        """Required for file-like behavior"""
        pass
