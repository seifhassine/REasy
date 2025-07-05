from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, 
    QTextEdit, QDialogButtonBox
)

class TemplateExportDialog(QDialog):
    def __init__(self, parent=None, gameobject_name="GameObject"):
        super().__init__(parent)
        self.setWindowTitle("Export GameObject Template")
        self.resize(400, 300)
        
        main_layout = QVBoxLayout(self)
        
        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("Template Name:"))
        self.name_edit = QLineEdit(gameobject_name)
        name_layout.addWidget(self.name_edit)
        main_layout.addLayout(name_layout)
        
        tags_layout = QHBoxLayout()
        tags_layout.addWidget(QLabel("Tags:"))
        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("Enter tags separated by commas")
        tags_layout.addWidget(self.tags_edit)
        main_layout.addLayout(tags_layout)
        
        main_layout.addWidget(QLabel("Description:"))
        self.description_edit = QTextEdit()
        main_layout.addWidget(self.description_edit)
        
        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
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
