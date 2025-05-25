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
        self.hash_type_combo.addItems(["NameHash", "Murmur3"])
        input_layout.addWidget(self.hash_type_label)
        input_layout.addWidget(self.hash_type_combo)

        self.calculate_button = QPushButton("Calculate Hash")
        self.calculate_button.clicked.connect(self.calculate_hash)

        self.result_label_hex = QLabel("Hash Result (Hex):")
        self.result_field_hex = QLineEdit()
        self.result_field_hex.setReadOnly(True)

        self.result_label_num = QLabel("Hash Result (Number):")
        self.result_field_num = QLineEdit()
        self.result_field_num.setReadOnly(True)

        result_layout.addWidget(self.result_label_hex)
        result_layout.addWidget(self.result_field_hex)
        result_layout.addWidget(self.result_label_num)
        result_layout.addWidget(self.result_field_num)

        main_layout.addLayout(input_layout)
        main_layout.addWidget(self.calculate_button)
        main_layout.addLayout(result_layout)

    def calculate_hash(self):
        input_text = self.input_field.text()
        hash_type = self.hash_type_combo.currentText()

        if not input_text:
            QMessageBox.warning(self, "Input Error", "Please enter a string to hash.")
            return

        try:
            if hash_type == "NameHash":
                result = compute_namehash(input_text)
            elif hash_type == "Murmur3":
                result = murmur3_hash(input_text.encode("utf-16le"))
            else:
                raise ValueError("Unsupported hash type.")

            self.result_field_hex.setText(f"0x{result:08X}")
            self.result_field_num.setText(str(result))
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to calculate hash: {e}")


if __name__ == "__main__":
    app = QApplication([])
    window = HashCalculator()
    window.show()
    app.exec()
