from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem, 
    QLabel, QLineEdit, QTextEdit, QPushButton, QComboBox, 
    QWidget, QSplitter, QMessageBox, QInputDialog,
    QMenu
)

from file_handlers.rsz.rsz_template_manager import RszTemplateManager

class TemplateManagerDialog(QDialog):
    template_imported = Signal(dict)
    
    def __init__(self, parent=None, viewer=None):
        super().__init__(parent)
        self.setWindowTitle("GameObject Template Manager")
        self.resize(800, 600)
        self.setMinimumSize(600, 400)
        
        self.viewer = viewer
        self.current_template_id = None
        self.current_registry_filter = None
        self.current_tag_filter = None
        
        self._create_ui()
        self._connect_signals()
        self._load_templates()
        self._load_filters()
    
    def _create_ui(self):
        main_layout = QVBoxLayout(self)
        
        splitter = QSplitter(Qt.Horizontal)
        
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        
        filter_layout = QHBoxLayout()
        
        self.registry_combo = QComboBox()
        self.registry_combo.addItem("All Registries", None)
        filter_layout.addWidget(QLabel("Registry:"))
        filter_layout.addWidget(self.registry_combo)
        
        self.tag_combo = QComboBox()
        self.tag_combo.addItem("All Tags", None)
        filter_layout.addWidget(QLabel("Tag:"))
        filter_layout.addWidget(self.tag_combo)
        
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search templates...")
        filter_layout.addWidget(self.search_input)
        
        left_layout.addLayout(filter_layout)
        
        self.template_list = QListWidget()
        self.template_list.setContextMenuPolicy(Qt.CustomContextMenu)
        left_layout.addWidget(self.template_list, 1)
        
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        
        self.template_details = QWidget()
        details_layout = QVBoxLayout(self.template_details)
        
        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("Name:"))
        self.name_edit = QLineEdit()
        name_layout.addWidget(self.name_edit, 1)
        details_layout.addLayout(name_layout)
        
        registry_layout = QHBoxLayout()
        registry_layout.addWidget(QLabel("Registry:"))
        self.registry_label = QLabel()
        registry_layout.addWidget(self.registry_label, 1)
        details_layout.addLayout(registry_layout)
        
        tags_layout = QHBoxLayout()
        tags_layout.addWidget(QLabel("Tags:"))
        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("Enter tags separated by commas")
        tags_layout.addWidget(self.tags_edit, 1)
        details_layout.addLayout(tags_layout)
        
        details_layout.addWidget(QLabel("Description:"))
        self.description_edit = QTextEdit()
        details_layout.addWidget(self.description_edit)
        
        self.created_label = QLabel()
        details_layout.addWidget(self.created_label)
        
        self.modified_label = QLabel()
        details_layout.addWidget(self.modified_label)
        
        details_button_layout = QHBoxLayout()
        
        self.update_button = QPushButton("Update Template")
        details_button_layout.addWidget(self.update_button)
        
        self.delete_button = QPushButton("Delete Template")
        details_button_layout.addWidget(self.delete_button)
        
        details_layout.addLayout(details_button_layout)
        
        right_layout.addWidget(self.template_details)
        
        import_layout = QHBoxLayout()
        
        self.parent_combo = QComboBox()
        self.parent_combo.addItem("Create at Root", -1)
        import_layout.addWidget(QLabel("Parent:"))
        import_layout.addWidget(self.parent_combo, 1)
        
        self.import_name_edit = QLineEdit()
        self.import_name_edit.setPlaceholderText("Use template name")
        import_layout.addWidget(QLabel("Name:"))
        import_layout.addWidget(self.import_name_edit, 1)
        
        self.import_button = QPushButton("Import Template")
        import_layout.addWidget(self.import_button)
        
        right_layout.addLayout(import_layout)
        
        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setSizes([300, 500])
        
        main_layout.addWidget(splitter)
        
        self.template_details.setVisible(False)
    
    def _connect_signals(self):
        self.template_list.itemSelectionChanged.connect(self._on_template_selected)
        self.template_list.customContextMenuRequested.connect(self._show_template_context_menu)
        
        self.update_button.clicked.connect(self._update_template)
        self.delete_button.clicked.connect(self._delete_template)
        self.import_button.clicked.connect(self._import_template)
        
        self.registry_combo.currentIndexChanged.connect(self._on_filter_changed)
        self.tag_combo.currentIndexChanged.connect(self._on_filter_changed)
        self.search_input.textChanged.connect(self._on_search_text_changed)
    
    def _load_templates(self):
        """Load templates into the list widget"""
        self.template_list.clear()
        
        templates = RszTemplateManager.get_template_list(
            registry_filter=self.current_registry_filter,
            tag_filter=self.current_tag_filter
        )
        
        search_text = self.search_input.text().lower()
        
        for template in templates:
            if search_text:
                name = template.get("name", "").lower()
                desc = template.get("description", "").lower()
                tags = ",".join(template.get("tags", [])).lower()
                
                if (search_text not in name and 
                    search_text not in desc and 
                    search_text not in tags):
                    continue
            
            item = QListWidgetItem(template.get("name", "Unknown"))
            item.setData(Qt.UserRole, template.get("id"))
            
            registry = template.get("registry", "default")
            tags = ", ".join(template.get("tags", []))
            tooltip = f"Registry: {registry}"
            if tags:
                tooltip += f"\nTags: {tags}"
            if template.get("description"):
                tooltip += f"\n{template.get('description')}"
            
            item.setToolTip(tooltip)
            self.template_list.addItem(item)
    
    def _load_filters(self):
        """Load filter options for registries and tags"""
        current_registry = self.registry_combo.currentData()
        current_tag = self.tag_combo.currentData()
        
        self.registry_combo.clear()
        self.registry_combo.addItem("All Registries", None)
        
        for registry in RszTemplateManager.get_all_registries():
            self.registry_combo.addItem(registry, registry)
            
        if current_registry is not None:
            index = self.registry_combo.findData(current_registry)
            if index >= 0:
                self.registry_combo.setCurrentIndex(index)
        
        self.tag_combo.clear()
        self.tag_combo.addItem("All Tags", None)
        
        for tag in RszTemplateManager.get_all_tags():
            self.tag_combo.addItem(tag, tag)
            
        if current_tag is not None:
            index = self.tag_combo.findData(current_tag)
            if index >= 0:
                self.tag_combo.setCurrentIndex(index)
                
        if self.viewer and hasattr(self.viewer, "scn"):
            self._populate_parent_combo()
    
    def _populate_parent_combo(self):
        """Populate the parent selection combo with available GameObjects and folders"""
        self.parent_combo.clear()
        self.parent_combo.addItem("Create at Root", -1)
        
        if not self.viewer or not hasattr(self.viewer, "scn"):
            return
            
        def add_gameobject(go_id, name, level=0):
            indent = "  " * level
            display_name = f"{indent}└─ {name}"
            self.parent_combo.addItem(display_name, go_id)
            
            for child_go in self.viewer.scn.gameobjects:
                if child_go.parent_id == go_id:
                    child_instance_id = self.viewer.scn.object_table[child_go.id]
                    child_name = self.viewer.name_helper.get_instance_first_field_name(child_instance_id)
                    if not child_name:
                        child_name = f"GameObject {child_go.id}"
                    add_gameobject(child_go.id, child_name, level + 1)
        
        for go in self.viewer.scn.gameobjects:
            if go.parent_id < 0: 
                go_instance_id = self.viewer.scn.object_table[go.id]
                name = self.viewer.name_helper.get_instance_first_field_name(go_instance_id)
                if not name:
                    name = f"GameObject {go.id}"
                add_gameobject(go.id, name)
                
        for folder in self.viewer.scn.folder_infos:
            if folder.id < len(self.viewer.scn.object_table):
                folder_instance_id = self.viewer.scn.object_table[folder.id]
                folder_name = self.viewer.name_helper.get_instance_first_field_name(folder_instance_id)
                if not folder_name:
                    folder_name = f"Folder {folder.id}"
                self.parent_combo.addItem(f"Folder: {folder_name}", folder.id)
    
    def _on_template_selected(self):
        """Handle template selection"""
        selected_items = self.template_list.selectedItems()
        if not selected_items:
            self.template_details.setVisible(False)
            self.current_template_id = None
            return
            
        item = selected_items[0]
        template_id = item.data(Qt.UserRole)
        self.current_template_id = template_id
        
        metadata = RszTemplateManager.load_metadata()
        if "templates" in metadata and template_id in metadata["templates"]:
            template_info = metadata["templates"][template_id]
            
            self.name_edit.setText(template_info.get("name", ""))
            self.registry_label.setText(template_info.get("registry", "default"))
            self.tags_edit.setText(", ".join(template_info.get("tags", [])))
            self.description_edit.setPlainText(template_info.get("description", ""))
            
            created = template_info.get("created", "")
            modified = template_info.get("modified", "")
            
            try:
                from datetime import datetime
                if created:
                    dt = datetime.fromisoformat(created)
                    created = dt.strftime("%Y-%m-%d %H:%M:%S")
                if modified:
                    dt = datetime.fromisoformat(modified)
                    modified = dt.strftime("%Y-%m-%d %H:%M:%S")
            except:
                pass
                
            self.created_label.setText(f"Created: {created}")
            self.modified_label.setText(f"Modified: {modified}")
            
            if not self.import_name_edit.text():
                self.import_name_edit.setText(template_info.get("name", ""))
            
            self.template_details.setVisible(True)
        else:
            self.template_details.setVisible(False)
    
    def _on_filter_changed(self):
        """Handle filter selection changes"""
        self.current_registry_filter = self.registry_combo.currentData()
        self.current_tag_filter = self.tag_combo.currentData()
        self._load_templates()
    
    def _on_search_text_changed(self):
        """Handle search text changes"""
        self._load_templates()
    
    def _update_template(self):
        """Update the selected template's metadata"""
        if not self.current_template_id:
            return
            
        name = self.name_edit.text()
        if not name:
            QMessageBox.warning(self, "Error", "Template name cannot be empty")
            return
            
        tags_text = self.tags_edit.text()
        tags = [tag.strip() for tag in tags_text.split(",") if tag.strip()]
        
        description = self.description_edit.toPlainText()
        
        success = RszTemplateManager.update_template_metadata(
            self.current_template_id,
            name=name,
            tags=tags,
            description=description
        )
        
        if success:
            QMessageBox.information(self, "Success", "Template updated successfully")
            self._load_templates()
            self._load_filters()
        else:
            QMessageBox.warning(self, "Error", "Failed to update template")
    
    def _delete_template(self):
        """Delete the selected template"""
        if not self.current_template_id:
            return
            
        confirm = QMessageBox.question(
            self,
            "Confirm Deletion",
            f"Are you sure you want to delete the template '{self.name_edit.text()}'?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if confirm != QMessageBox.Yes:
            return
            
        success = RszTemplateManager.delete_template(self.current_template_id)
        
        if success:
            QMessageBox.information(self, "Success", "Template deleted successfully")
            self.current_template_id = None
            self.template_details.setVisible(False)
            self._load_templates()
            self._load_filters()
        else:
            QMessageBox.warning(self, "Error", "Failed to delete template")
    
    def _import_template(self):
        """Import the selected template"""
        if not self.current_template_id or not self.viewer:
            return
            
        parent_id = self.parent_combo.currentData()
        
        new_name = None
        
        metadata = RszTemplateManager.load_metadata()
        if "templates" in metadata and self.current_template_id in metadata["templates"]:
            template_info = metadata["templates"][self.current_template_id]
            template_name = template_info.get("name", "")
            
            if self.import_name_edit.text() and self.import_name_edit.text() != template_name:
                new_name = self.import_name_edit.text()
        else:
            new_name = self.import_name_edit.text() if self.import_name_edit.text() else None
        
        result = RszTemplateManager.import_template(
            self.viewer,
            self.current_template_id,
            parent_id,
            new_name
        )
        
        if result["success"]:
            QMessageBox.information(self, "Success", result["message"])
            self.template_imported.emit(result["gameobject_data"])
            self.accept() 
        else:
            QMessageBox.warning(self, "Error", result["message"])
    
    def _show_template_context_menu(self, position):
        """Show context menu for template list items"""
        selected_items = self.template_list.selectedItems()
        if not selected_items:
            return
            
        item = selected_items[0]
        template_id = item.data(Qt.UserRole)
        
        menu = QMenu(self)
        
        import_action = menu.addAction("Import Template")
        import_action.triggered.connect(self._import_template)
        
        rename_action = menu.addAction("Rename Template")
        rename_action.triggered.connect(lambda: self._rename_template(template_id))
        
        delete_action = menu.addAction("Delete Template")
        delete_action.triggered.connect(lambda: self._delete_template_from_context_menu(template_id))
        
        menu.exec_(self.template_list.mapToGlobal(position))
    
    def _rename_template(self, template_id):
        """Rename template from context menu"""
        metadata = RszTemplateManager.load_metadata()
        if "templates" not in metadata or template_id not in metadata["templates"]:
            return
            
        template_info = metadata["templates"][template_id]
        current_name = template_info.get("name", "")
        
        new_name, ok = QInputDialog.getText(
            self, 
            "Rename Template", 
            "Enter new name:", 
            text=current_name
        )
        
        if ok and new_name:
            success = RszTemplateManager.update_template_metadata(
                template_id,
                name=new_name
            )
            
            if success:
                QMessageBox.information(self, "Success", "Template renamed successfully")
                self._load_templates()
                self._load_filters()
                
                if template_id == self.current_template_id:
                    self.name_edit.setText(new_name)
            else:
                QMessageBox.warning(self, "Error", "Failed to rename template")
    
    def _delete_template_from_context_menu(self, template_id):
        """Delete template from context menu"""
        metadata = RszTemplateManager.load_metadata()
        if "templates" not in metadata or template_id not in metadata["templates"]:
            return
            
        template_info = metadata["templates"][template_id]
        name = template_info.get("name", "Unknown")
        
        confirm = QMessageBox.question(
            self,
            "Confirm Deletion",
            f"Are you sure you want to delete the template '{name}'?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if confirm != QMessageBox.Yes:
            return
            
        success = RszTemplateManager.delete_template(template_id)
        
        if success:
            QMessageBox.information(self, "Success", "Template deleted successfully")
            
            if template_id == self.current_template_id:
                self.current_template_id = None
                self.template_details.setVisible(False)
                
            self._load_templates()
            self._load_filters()
        else:
            QMessageBox.warning(self, "Error", "Failed to delete template")
