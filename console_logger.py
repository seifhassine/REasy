import logging
import tkinter as tk
from tkinter.scrolledtext import ScrolledText


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
    logger.setLevel(logging.INFO)  # Only INFO and above will be logged now
    for handler in logger.handlers[:]:
        if isinstance(handler, TextHandler):
            logger.removeHandler(handler)
    handler = TextHandler(text_widget)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)


class ConsoleRedirector:
    def __init__(self, text_widget, original_stream):
        self.text_widget = text_widget
        self.original_stream = original_stream

    def write(self, s):
        if s.strip():

            def append():
                self.text_widget.configure(state="normal")
                self.text_widget.insert(tk.END, s)
                self.text_widget.configure(state="disabled")
                self.text_widget.yview(tk.END)

            self.text_widget.after(0, append)
        self.original_stream.write(s)

    def flush(self):
        self.original_stream.flush()


class StdoutRedirector:
    def __init__(self, text_widget):
        self.text_widget = text_widget

    def write(self, message):
        # Ensure each write ends with a newline.
        if message and not message.endswith("\n"):
            message += "\n"
        if message.strip() == "":
            return
        self.text_widget.configure(state="normal")
        self.text_widget.insert(tk.END, message)
        self.text_widget.configure(state="disabled")
        self.text_widget.see(tk.END)

    def flush(self):
        pass
