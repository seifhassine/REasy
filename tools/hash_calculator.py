from PySide6.QtWidgets import (
    QApplication, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QComboBox, QWidget, QMessageBox
)
from utils.hash_util import compute_namehash, murmur3_hash


class HashCalculator(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Hash Calculator")
        self.setMinimumWidth(400)

        main_layout = QVBoxLayout(self)
        input_layout = QHBoxLayout()
        result_layout = QVBoxLayout()

        self.input_label = QLabel("Input String:")
        self.input_field = QLineEdit()
        input_layout.addWidget(self.input_label)
        input_layout.addWidget(self.input_field)

        self.hash_type_label = QLabel("Hash Type:")
        self.hash_type_combo = QComboBox()
        self.hash_type_combo.addItems(["Murmur3"])
        input_layout.addWidget(self.hash_type_label)
        input_layout.addWidget(self.hash_type_combo)

        self.calculate_button = QPushButton("Calculate Hash")
        self.calculate_button.clicked.connect(self.calculate_hash)

        utf8_group_layout = QVBoxLayout()
        utf8_label = QLabel("UTF-8 Results:")
        utf8_label.setStyleSheet("font-weight: bold")
        utf8_group_layout.addWidget(utf8_label)

        self.result_label_hex_utf8 = QLabel("Hex:")
        self.result_field_hex_utf8 = QLineEdit()
        self.result_field_hex_utf8.setReadOnly(True)

        self.result_label_num_utf8 = QLabel("Number:")
        self.result_field_num_utf8 = QLineEdit()
        self.result_field_num_utf8.setReadOnly(True)

        utf8_group_layout.addWidget(self.result_label_hex_utf8)
        utf8_group_layout.addWidget(self.result_field_hex_utf8)
        utf8_group_layout.addWidget(self.result_label_num_utf8)
        utf8_group_layout.addWidget(self.result_field_num_utf8)
        
        utf16_group_layout = QVBoxLayout()
        utf16_label = QLabel("UTF-16 Results:")
        utf16_label.setStyleSheet("font-weight: bold")
        utf16_group_layout.addWidget(utf16_label)

        self.result_label_hex_utf16 = QLabel("Hex:")
        self.result_field_hex_utf16 = QLineEdit()
        self.result_field_hex_utf16.setReadOnly(True)

        self.result_label_num_utf16 = QLabel("Number:")
        self.result_field_num_utf16 = QLineEdit()
        self.result_field_num_utf16.setReadOnly(True)

        utf16_group_layout.addWidget(self.result_label_hex_utf16)
        utf16_group_layout.addWidget(self.result_field_hex_utf16)
        utf16_group_layout.addWidget(self.result_label_num_utf16)
        utf16_group_layout.addWidget(self.result_field_num_utf16)

        result_layout.addLayout(utf8_group_layout)
        result_layout.addLayout(utf16_group_layout)

        main_layout.addLayout(input_layout)
        main_layout.addWidget(self.calculate_button)
        main_layout.addLayout(result_layout)

    def calculate_hash(self):
        input_text = self.input_field.text()

        if not input_text:
            QMessageBox.warning(self, "Input Error", "Please enter a string to hash.")
            return

        try:
            result_utf8 = murmur3_hash(input_text.encode("utf-8"))
            result_utf16 = murmur3_hash(input_text.encode("utf-16le"))
            
            self.result_field_hex_utf8.setText(f"0x{result_utf8:08X}")
            self.result_field_num_utf8.setText(f"{result_utf8}")
            
            self.result_field_hex_utf16.setText(f"0x{result_utf16:08X}")
            self.result_field_num_utf16.setText(f"{result_utf16}")

        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to calculate hash: {e}")


if __name__ == "__main__":
    app = QApplication([])
    window = HashCalculator()
    window.show()
    app.exec()
