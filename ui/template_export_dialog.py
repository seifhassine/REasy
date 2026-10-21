from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLineEdit,
    QTextEdit, QDialogButtonBox
)

class TemplateExportDialog(QDialog):
    def __init__(self, parent=None, gameobject_name="GameObject"):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Export GameObject Template"))
        self.resize(440, 300)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(6)

        self.name_edit = QLineEdit(gameobject_name)
        form.addRow(self.tr("Template Name:"), self.name_edit)

        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText(self.tr("Enter tags separated by commas"))
        form.addRow(self.tr("Tags:"), self.tags_edit)

        self.description_edit = QTextEdit()
        self.description_edit.setAcceptRichText(False)
        form.addRow(self.tr("Description:"), self.description_edit)
        main_layout.addLayout(form, 1)

        button_box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        main_layout.addWidget(button_box)
    
    def get_template_info(self):
        """Get the entered template information"""
        name = self.name_edit.text()
        
        tags_text = self.tags_edit.text()
        tags = [tag.strip() for tag in tags_text.split(",") if tag.strip()]
        
        description = self.description_edit.toPlainText()
        
        return {
            "name": name,
            "tags": tags,
            "description": description
        }
