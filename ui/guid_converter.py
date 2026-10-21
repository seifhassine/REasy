
import uuid
lazy from PySide6.QtCore import QCoreApplication
lazy from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
)
lazy from ui.editor_widgets import set_invalid, styled_label
lazy from ui.icons import line_icon
lazy from ui.styles import set_style_role

def create_guid_converter_dialog(parent):
    """Creates and shows a GUID converter dialog"""
    dialog = QDialog(parent)
    dialog.setWindowTitle(QCoreApplication.translate("GuidConverter", "GUID Converter"))
    dialog.setMinimumWidth(440)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(12, 12, 12, 12)
    layout.setSpacing(4)

    mem_label = QLabel(QCoreApplication.translate("GuidConverter", "GUID Memory (in memory order)"))
    layout.addWidget(mem_label)
    mem_entry = QLineEdit()
    mem_entry.setPlaceholderText("00000000-0000-0000-0000-000000000000")
    set_style_role(mem_entry, mono=True)
    layout.addWidget(mem_entry)

    btn_layout = QHBoxLayout()
    btn_layout.setContentsMargins(0, 6, 0, 6)
    btn_layout.setSpacing(6)
    mem_to_std_btn = QPushButton(
        line_icon("arrow_down"), QCoreApplication.translate("GuidConverter", "Memory -> Standard")
    )
    std_to_mem_btn = QPushButton(
        line_icon("arrow_up"), QCoreApplication.translate("GuidConverter", "Standard -> Memory")
    )
    btn_layout.addWidget(mem_to_std_btn)
    btn_layout.addWidget(std_to_mem_btn)
    btn_layout.addStretch()
    layout.addLayout(btn_layout)

    std_label = QLabel(QCoreApplication.translate("GuidConverter", "GUID Standard (hyphenated)"))
    layout.addWidget(std_label)
    std_entry = QLineEdit()
    std_entry.setPlaceholderText("00000000-0000-0000-0000-000000000000")
    set_style_role(std_entry, mono=True)
    layout.addWidget(std_entry)

    error_label = styled_label(tone="danger")
    error_label.setWordWrap(True)
    error_label.hide()
    layout.addSpacing(4)
    layout.addWidget(error_label)

    def show_error(entry, error):
        set_invalid(entry, True)
        error_label.setText(
            QCoreApplication.translate("GuidConverter", "Conversion error: {error}").format(error=error)
        )
        error_label.show()

    def clear_error():
        for entry in (mem_entry, std_entry):
            set_invalid(entry, False)
        error_label.hide()

    def mem_to_std():
        ms = (
            mem_entry.text()
            .strip()
            .replace("-", "")
            .replace("{", "")
            .replace("}", "")
            .replace(" ", "")
        )
        try:
            if len(ms) != 32:
                raise ValueError(QCoreApplication.translate("GuidConverter", "Must be 32 hex digits."))
            mb = bytes.fromhex(ms)
            std = str(uuid.UUID(bytes_le=mb))
            std_entry.setText(std)
            clear_error()
        except Exception as e:
            show_error(mem_entry, e)

    def std_to_mem():
        try:
            g = uuid.UUID(std_entry.text().strip())
            hex_mem = g.bytes_le.hex()
            # Reinsert dashes similarly to the standard format
            dashed = f"{hex_mem[0:8]}-{hex_mem[8:12]}-{hex_mem[12:16]}-{hex_mem[16:20]}-{hex_mem[20:32]}"
            mem_entry.setText(dashed)
            clear_error()
        except Exception as e:
            show_error(std_entry, e)

    mem_to_std_btn.clicked.connect(mem_to_std)
    std_to_mem_btn.clicked.connect(std_to_mem)
    mem_entry.textEdited.connect(clear_error)
    std_entry.textEdited.connect(clear_error)

    return dialog
