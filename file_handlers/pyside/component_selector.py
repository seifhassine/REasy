from PySide6.QtWidgets import (QDialog, QVBoxLayout, QLineEdit, QListWidget, 
                              QDialogButtonBox, QLabel)

class ComponentSelectorDialog(QDialog):
    """Dialog for selecting a component type with filtering and autocomplete"""
    
    def __init__(self, parent=None, type_registry=None, required_parent_name=None, include_parent=False):
        super().__init__(parent)
        self.type_registry = type_registry
        self.selected_component = None
        self.required_parent_name = required_parent_name
        self.include_parent = include_parent
        
        self.setWindowTitle(self.tr("Add Component"))
        self.resize(500, 400)
        
        layout = QVBoxLayout(self)
        
        self.status_label = QLabel(self.tr("Loading components..."))
        layout.addWidget(self.status_label)
        
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(
            self.tr("Type component name (e.g. 'chainsaw.GmOptionSleep')")
        )
        layout.addWidget(self.search_input)
        
        self.component_list = QListWidget()
        layout.addWidget(self.component_list)
        
        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)
        
        self.all_component_types = []
        self._load_component_types()
        
        self.search_input.textChanged.connect(self.on_text_changed)
        self.component_list.itemDoubleClicked.connect(self.accept)
        
        self.search_input.setFocus()

    def _should_include_component(self, type_info):
        if not isinstance(type_info, dict) or "name" not in type_info:
            return False

        type_name = type_info["name"]
        if not isinstance(type_name, str) or not type_name:
            return False
        if (
            type_name.startswith("System.")
            or ".Collections." in type_name
            or "[]" in type_name
        ):
            return False

        if not self.required_parent_name:
            return bool(type_info["fields"])
        if type_name == self.required_parent_name:
            return self.include_parent

        parents = self.type_registry.getTypeParents(type_name)
        return self.required_parent_name in parents
        
    def _load_component_types(self):
        """Extract all component types from the type registry"""
        self.all_component_types = [
            type_info["name"]
            for type_info in self.type_registry.registry.values()
            if self._should_include_component(type_info)
        ]
        
        self.all_component_types.sort()
        
        self.status_label.setText(
            self.tr("Found {count} component types").format(
                count=len(self.all_component_types)
            )
        )
        self.populate_component_list("")
        
    def on_text_changed(self, text):
        """Filter component list as user types"""
        self.populate_component_list(text)
        
    def populate_component_list(self, search_text):
        """Populate list with component types that match the search text"""
        self.component_list.clear()
        
        search_lower = search_text.lower()
        matching_types = []
        
        for component_type in self.all_component_types:
            if not search_text or search_lower in component_type.lower():
                matching_types.append(component_type)
                if not self.required_parent_name and len(matching_types) >= 600:
                    break
        
        self.component_list.addItems(matching_types)
        
        if self.component_list.count() > 0:
            self.component_list.setCurrentRow(0)
            
        if self.required_parent_name:
            self.status_label.setText(
                self.tr("Showing {matches} matches out of {total} components").format(
                    matches=len(matching_types),
                    total=len(self.all_component_types),
                )
            )
        else:
            self.status_label.setText(
                self.tr("Showing first {matches} matches out of {total} components").format(
                    matches=len(matching_types),
                    total=len(self.all_component_types),
                )
            )
        
    def get_selected_component(self):
        """Return the selected component type name"""
        if self.result() == QDialog.Accepted:
            current_item = self.component_list.currentItem()
            if current_item:
                return current_item.text()
            else:
                return self.search_input.text() if self.search_input.text() else None
        return None
