from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QLabel, QLineEdit, QTextEdit, QPushButton, QComboBox,
    QWidget, QSplitter, QMessageBox, QInputDialog,
    QMenu, QFormLayout
)

lazy from file_handlers.rsz.rsz_template_manager import RszTemplateManager
lazy from ui.community_templates_dialog import CommunityTemplatesDialog
lazy from ui.editor_widgets import editor_strip, search_field, styled_label
lazy from ui.icons import line_icon

class TemplateManagerDialog(QDialog):
    template_imported = Signal(dict)
    
    def __init__(self, parent=None, viewer=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("GameObject Template Manager"))
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
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(8, 8, 4, 8)
        left_layout.setSpacing(6)

        self.search_input = search_field(QLineEdit())
        self.search_input.setPlaceholderText(self.tr("Search templates..."))
        left_layout.addWidget(self.search_input)

        filter_layout = QHBoxLayout()
        filter_layout.setSpacing(6)

        self.registry_combo = QComboBox()
        self.registry_combo.addItem(self.tr("All Registries"), None)
        self.registry_combo.setToolTip(self.tr("Registry:"))
        filter_layout.addWidget(self.registry_combo, 1)

        self.tag_combo = QComboBox()
        self.tag_combo.addItem(self.tr("All Tags"), None)
        self.tag_combo.setToolTip(self.tr("Tag:"))
        filter_layout.addWidget(self.tag_combo, 1)

        left_layout.addLayout(filter_layout)

        self.template_list = QListWidget()
        self.template_list.setContextMenuPolicy(Qt.CustomContextMenu)
        left_layout.addWidget(self.template_list, 1)
        self.empty_list_label = styled_label(self.tr("No templates found"), emptyState=True)
        self.empty_list_label.setWordWrap(True)
        self.empty_list_label.hide()
        left_layout.addWidget(self.empty_list_label, 1)

        self.community_button = QPushButton(line_icon("globe"), self.tr("Browse Community Templates"))
        self.community_button.clicked.connect(self._open_community_templates)
        left_layout.addWidget(self.community_button)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self.template_details = QWidget()
        details_layout = QVBoxLayout(self.template_details)
        details_layout.setContentsMargins(4, 8, 8, 8)
        details_layout.setSpacing(6)

        details_form = QFormLayout()
        details_form.setHorizontalSpacing(8)
        details_form.setVerticalSpacing(6)
        self.name_edit = QLineEdit()
        details_form.addRow(self.tr("Name:"), self.name_edit)

        self.registry_label = QLabel()
        self.registry_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        details_form.addRow(self.tr("Registry:"), self.registry_label)

        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText(self.tr("Enter tags separated by commas"))
        details_form.addRow(self.tr("Tags:"), self.tags_edit)

        self.description_edit = QTextEdit()
        self.description_edit.setAcceptRichText(False)
        details_form.addRow(self.tr("Description:"), self.description_edit)
        details_layout.addLayout(details_form, 1)

        self.created_label = styled_label(tone="subtle")
        self.modified_label = styled_label(tone="subtle")

        dates_layout = QHBoxLayout()
        dates_layout.setSpacing(16)
        dates_layout.addWidget(self.created_label)
        dates_layout.addWidget(self.modified_label)
        dates_layout.addStretch()
        details_layout.addLayout(dates_layout)

        details_button_layout = QHBoxLayout()
        details_button_layout.setSpacing(6)
        details_button_layout.addStretch()

        self.update_button = QPushButton(line_icon("save"), self.tr("Update Template"))
        details_button_layout.addWidget(self.update_button)

        self.delete_button = QPushButton(self.tr("Delete Template"))
        self.delete_button.setProperty("variant", "danger")
        details_button_layout.addWidget(self.delete_button)

        details_layout.addLayout(details_button_layout)

        right_layout.addWidget(self.template_details, 1)
        self.empty_details_label = styled_label(self.tr("Select a template to see its details"), tone="subtle")
        self.empty_details_label.setAlignment(Qt.AlignCenter)
        right_layout.addWidget(self.empty_details_label, 1)

        import_bar, import_layout = editor_strip("footer", (8, 6, 8, 6), 6)

        self.parent_combo = QComboBox()
        self.parent_combo.addItem(line_icon("home"), self.tr("Create at Root"), -1)
        import_layout.addWidget(QLabel(self.tr("Parent:")))
        import_layout.addWidget(self.parent_combo, 1)

        self.import_name_edit = QLineEdit()
        self.import_name_edit.setPlaceholderText(self.tr("Use template name"))
        import_layout.addWidget(QLabel(self.tr("Name:")))
        import_layout.addWidget(self.import_name_edit, 1)

        self.import_button = QPushButton(self.tr("Import Template"))
        self.import_button.setProperty("variant", "primary")
        self.import_button.setEnabled(False)
        import_layout.addWidget(self.import_button)

        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 500])

        main_layout.addWidget(splitter, 1)
        main_layout.addWidget(import_bar)

        self._show_details(False)

    def _show_details(self, visible):
        self.template_details.setVisible(visible)
        self.empty_details_label.setVisible(not visible)
        self.import_button.setEnabled(visible and self.viewer is not None)
    
    def _connect_signals(self):
        self.tag_combo.currentIndexChanged.connect(self._on_filter_changed)
        self.search_input.textChanged.connect(self._on_search_text_changed)
        self.template_list.itemSelectionChanged.connect(self._on_template_selected)
        self.template_list.customContextMenuRequested.connect(self._show_template_context_menu)
        
        self.update_button.clicked.connect(self._update_template)
        self.delete_button.clicked.connect(self._delete_template)
        self.import_button.clicked.connect(self._import_template)
        
        self.registry_combo.currentIndexChanged.connect(self._on_filter_changed)
    
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
            
            item = QListWidgetItem(template.get("name", self.tr("Unknown")))
            item.setData(Qt.UserRole, template.get("id"))
            
            registry = template.get("registry", "default")
            tags = ", ".join(template.get("tags", []))
            tooltip = self.tr("Registry: {registry}").format(registry=registry)
            if tags:
                tooltip += "\n" + self.tr("Tags: {tags}").format(tags=tags)
            if template.get("description"):
                tooltip += f"\n{template.get('description')}"
            
            item.setToolTip(tooltip)
            self.template_list.addItem(item)

        has_templates = self.template_list.count() > 0
        self.template_list.setVisible(has_templates)
        self.empty_list_label.setVisible(not has_templates)

    def _load_filters(self):
        """Load filter options for registries and tags"""
        current_registry = self.registry_combo.currentData()
        current_tag = self.tag_combo.currentData()
        
        self.registry_combo.clear()
        self.registry_combo.addItem(self.tr("All Registries"), None)
        
        for registry in RszTemplateManager.get_all_registries():
            self.registry_combo.addItem(registry, registry)
            
        if current_registry is not None:
            index = self.registry_combo.findData(current_registry)
            if index >= 0:
                self.registry_combo.setCurrentIndex(index)
        
        self.tag_combo.clear()
        self.tag_combo.addItem(self.tr("All Tags"), None)
        
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
        self.parent_combo.addItem(line_icon("home"), self.tr("Create at Root"), -1)

        if not self.viewer or not hasattr(self.viewer, "scn"):
            return
        gameobject_icon = line_icon("gameobject")

        def add_gameobject(go_id, name, level=0):
            display_name = "    " * level + name
            self.parent_combo.addItem(gameobject_icon, display_name, go_id)
            
            for child_go in self.viewer.scn.gameobjects:
                if child_go.parent_id == go_id:
                    child_instance_id = self.viewer.scn.object_table[child_go.id]
                    child_name = self.viewer.name_helper.get_instance_first_field_name(child_instance_id)
                    if not child_name:
                        child_name = self.tr("GameObject {id}").format(id=child_go.id)
                    add_gameobject(child_go.id, child_name, level + 1)
        
        for go in self.viewer.scn.gameobjects:
            if go.parent_id < 0: 
                go_instance_id = self.viewer.scn.object_table[go.id]
                name = self.viewer.name_helper.get_instance_first_field_name(go_instance_id)
                if not name:
                    name = self.tr("GameObject {id}").format(id=go.id)
                add_gameobject(go.id, name)
                
        for folder in self.viewer.scn.folder_infos:
            if folder.id < len(self.viewer.scn.object_table):
                folder_instance_id = self.viewer.scn.object_table[folder.id]
                folder_name = self.viewer.name_helper.get_instance_first_field_name(folder_instance_id)
                if not folder_name:
                    folder_name = self.tr("Folder {id}").format(id=folder.id)
                self.parent_combo.addItem(
                    line_icon("folder"),
                    self.tr("Folder: {folder_name}").format(folder_name=folder_name), folder.id
                )
    
    def _on_template_selected(self):
        """Handle template selection"""
        selected_items = self.template_list.selectedItems()
        if not selected_items:
            self._show_details(False)
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
            except Exception as e:
                print(f"Error formatting timestamp: {e}")

            self.created_label.setText(self.tr("Created: {date}").format(date=created))
            self.modified_label.setText(self.tr("Modified: {date}").format(date=modified))
            
            if not self.import_name_edit.text():
                self.import_name_edit.setText(template_info.get("name", ""))
            
            self._show_details(True)
        else:
            self._show_details(False)
    
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
            QMessageBox.warning(
                self, self.tr("Error"), self.tr("Template name cannot be empty")
            )
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
            QMessageBox.information(
                self, self.tr("Success"), self.tr("Template updated successfully")
            )
            self._load_templates()
            self._load_filters()
        else:
            QMessageBox.warning(
                self, self.tr("Error"), self.tr("Failed to update template")
            )
    
    def _delete_template(self):
        """Delete the selected template"""
        if not self.current_template_id:
            return

        self._delete_template_by_id(self.current_template_id, self.name_edit.text())

    def _delete_template_by_id(self, template_id, name):
        """Delete a template after confirmation and refresh the dialog state."""
        confirm = QMessageBox.question(
            self,
            self.tr("Confirm Deletion"),
            self.tr("Are you sure you want to delete the template '{name}'?").format(
                name=name
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if confirm != QMessageBox.Yes:
            return
            
        success = RszTemplateManager.delete_template(template_id)
        
        if success:
            QMessageBox.information(
                self, self.tr("Success"), self.tr("Template deleted successfully")
            )
            if template_id == self.current_template_id:
                self.current_template_id = None
                self._show_details(False)
            self._load_templates()
            self._load_filters()
        else:
            QMessageBox.warning(
                self, self.tr("Error"), self.tr("Failed to delete template")
            )
    
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
            QMessageBox.information(self, self.tr("Success"), result["message"])
            self.template_imported.emit(result["gameobject_data"])
            self.accept() 
        else:
            QMessageBox.warning(self, self.tr("Error"), result["message"])
    
    def _show_template_context_menu(self, position):
        """Show context menu for template list items"""
        selected_items = self.template_list.selectedItems()
        if not selected_items:
            return
            
        item = selected_items[0]
        template_id = item.data(Qt.UserRole)
        
        menu = QMenu(self)
        
        import_action = menu.addAction(self.tr("Import Template"))
        import_action.triggered.connect(self._import_template)
        
        rename_action = menu.addAction(self.tr("Rename Template"))
        rename_action.triggered.connect(lambda: self._rename_template(template_id))
        
        delete_action = menu.addAction(self.tr("Delete Template"))
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
            self.tr("Rename Template"),
            self.tr("Enter new name:"),
            text=current_name
        )
        
        if ok and new_name:
            success = RszTemplateManager.update_template_metadata(
                template_id,
                name=new_name
            )
            
            if success:
                QMessageBox.information(
                    self, self.tr("Success"), self.tr("Template renamed successfully")
                )
                self._load_templates()
                self._load_filters()
                
                if template_id == self.current_template_id:
                    self.name_edit.setText(new_name)
            else:
                QMessageBox.warning(
                    self, self.tr("Error"), self.tr("Failed to rename template")
                )
    
    def _delete_template_from_context_menu(self, template_id):
        """Delete template from context menu"""
        metadata = RszTemplateManager.load_metadata()
        if "templates" not in metadata or template_id not in metadata["templates"]:
            return
            
        template_info = metadata["templates"][template_id]
        name = template_info.get("name", self.tr("Unknown"))

        self._delete_template_by_id(template_id, name)
    
    def _open_community_templates(self):
        """Open the community templates browser dialog"""
        dialog = CommunityTemplatesDialog(self)
        dialog.template_downloaded.connect(self._on_community_template_downloaded)
        dialog.exec_()
    
    def _on_community_template_downloaded(self, template_id):
        """Handle a template downloaded from the community"""
        self._load_templates()
        self._load_filters()
        
        for i in range(self.template_list.count()):
            item = self.template_list.item(i)
            if item.data(Qt.UserRole) == template_id:
                self.template_list.setCurrentItem(item)
                break
