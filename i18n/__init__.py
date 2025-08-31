from PySide6.QtCore import QCoreApplication


def tr(context: str, text: str, disambiguation: str | None = None, n: int = -1) -> str:
    return QCoreApplication.translate(context, text, disambiguation, n)

