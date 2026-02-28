
import uuid
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QMessageBox,
)

def create_guid_converter_dialog(parent):
    """Creates and shows a GUID converter dialog"""
    dialog = QDialog(parent)
    dialog.setWindowTitle("GUID Converter")
    dialog.resize(400, 200)
    layout = QVBoxLayout(dialog)
    layout.setSpacing(10)

    # Input fields section
    mem_label = QLabel("GUID Memory (in memory order)")
    layout.addWidget(mem_label)
    mem_entry = QLineEdit()
    layout.addWidget(mem_entry)

    layout.addSpacing(20)

    std_label = QLabel("GUID Standard (hyphenated)")
    layout.addWidget(std_label)
    std_entry = QLineEdit()
    layout.addWidget(std_entry)

    layout.addSpacing(30)

    # Buttons section
    btn_layout = QHBoxLayout()
    mem_to_std_btn = QPushButton("Memory -> Standard")
    std_to_mem_btn = QPushButton("Standard -> Memory")
    btn_layout.addWidget(mem_to_std_btn)
    btn_layout.addWidget(std_to_mem_btn)
    layout.addLayout(btn_layout)

    layout.addSpacing(15)

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
                raise ValueError("Must be 32 hex digits.")
            mb = bytes.fromhex(ms)
            std = str(uuid.UUID(bytes_le=mb))
            std_entry.setText(std)
        except Exception as e:
            QMessageBox.critical(dialog, "Error", f"Conversion error: {e}")

    def std_to_mem():
        try:
            g = uuid.UUID(std_entry.text().strip())
            hex_mem = g.bytes_le.hex()
            # Reinsert dashes similarly to the standard format
            dashed = f"{hex_mem[0:8]}-{hex_mem[8:12]}-{hex_mem[12:16]}-{hex_mem[16:20]}-{hex_mem[20:32]}"
            mem_entry.setText(dashed)
        except Exception as e:
            QMessageBox.critical(dialog, "Error", f"Conversion error: {e}")

    mem_to_std_btn.clicked.connect(mem_to_std)
    std_to_mem_btn.clicked.connect(std_to_mem)

    return dialog
