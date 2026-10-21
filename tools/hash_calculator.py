from PySide6.QtWidgets import (
    QApplication, QGridLayout, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QComboBox, QWidget,
    QMessageBox
)
lazy from ui.editor_widgets import styled_label
lazy from ui.styles import set_style_role
lazy from utils.hash_util import murmur3_hash


class HashCalculator(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(self.tr("Hash Calculator"))
        self.setMinimumWidth(440)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(8)
        input_layout = QHBoxLayout()
        input_layout.setSpacing(6)
        result_layout = QGridLayout()
        result_layout.setHorizontalSpacing(10)
        result_layout.setVerticalSpacing(6)
        result_layout.setColumnStretch(1, 1)

        self.input_label = QLabel(self.tr("Input String:"))
        self.input_field = QLineEdit()
        self.input_field.returnPressed.connect(self.calculate_hash)
        input_layout.addWidget(self.input_label)
        input_layout.addWidget(self.input_field, 1)

        self.hash_type_label = QLabel(self.tr("Hash Type:"))
        self.hash_type_combo = QComboBox()
        self.hash_type_combo.addItems(["Murmur3"])
        input_layout.addWidget(self.hash_type_label)
        input_layout.addWidget(self.hash_type_combo)

        self.calculate_button = QPushButton(self.tr("Calculate Hash"))
        set_style_role(self.calculate_button, variant="primary")
        self.calculate_button.clicked.connect(self.calculate_hash)
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        button_layout.addWidget(self.calculate_button)

        utf8_label = styled_label(self.tr("UTF-8 Results:"), heading="section")
        self.result_label_hex_utf8 = QLabel(self.tr("Hex:"))
        self.result_field_hex_utf8 = self._result_field()
        self.result_label_num_utf8 = QLabel(self.tr("Number:"))
        self.result_field_num_utf8 = self._result_field()

        utf16_label = styled_label(self.tr("UTF-16 Results:"), heading="section")
        self.result_label_hex_utf16 = QLabel(self.tr("Hex:"))
        self.result_field_hex_utf16 = self._result_field()
        self.result_label_num_utf16 = QLabel(self.tr("Number:"))
        self.result_field_num_utf16 = self._result_field()

        rows = (
            (utf8_label, None),
            (self.result_label_hex_utf8, self.result_field_hex_utf8),
            (self.result_label_num_utf8, self.result_field_num_utf8),
            (utf16_label, None),
            (self.result_label_hex_utf16, self.result_field_hex_utf16),
            (self.result_label_num_utf16, self.result_field_num_utf16),
        )
        for row, (label, field) in enumerate(rows):
            if field is None:
                if row:
                    label.setContentsMargins(0, 8, 0, 0)
                result_layout.addWidget(label, row, 0, 1, 2)
            else:
                set_style_role(label, tone="muted")
                result_layout.addWidget(label, row, 0)
                result_layout.addWidget(field, row, 1)

        main_layout.addLayout(input_layout)
        main_layout.addLayout(button_layout)
        main_layout.addLayout(result_layout)
        main_layout.addStretch()

    @staticmethod
    def _result_field():
        field = QLineEdit()
        field.setReadOnly(True)
        set_style_role(field, mono=True)
        return field

    def calculate_hash(self):
        input_text = self.input_field.text()

        if not input_text:
            QMessageBox.warning(self, self.tr("Input Error"), self.tr("Please enter a string to hash."))
            return

        try:
            result_utf8 = murmur3_hash(input_text.encode("utf-8"))
            result_utf16 = murmur3_hash(input_text.encode("utf-16le"))

            self.result_field_hex_utf8.setText(f"0x{result_utf8:08X}")
            self.result_field_num_utf8.setText(f"{result_utf8}")

            self.result_field_hex_utf16.setText(f"0x{result_utf16:08X}")
            self.result_field_num_utf16.setText(f"{result_utf16}")

        except Exception as e:
            QMessageBox.critical(
                self, self.tr("Error"), self.tr("Failed to calculate hash: {error}").format(error=e)
            )


if __name__ == "__main__":
    app = QApplication([])
    window = HashCalculator()
    window.show()
    app.exec()
