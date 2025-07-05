from PySide6.QtWidgets import (QDialog, QVBoxLayout, QLineEdit, QListWidget, 
                              QDialogButtonBox, QLabel)

class ComponentSelectorDialog(QDialog):
    """Dialog for selecting a component type with filtering and autocomplete"""
    
    def __init__(self, parent=None, type_registry=None):
        super().__init__(parent)
        self.type_registry = type_registry
        self.selected_component = None
        
        self.setWindowTitle("Add Component")
        self.resize(500, 400)
        
        layout = QVBoxLayout(self)
        
        self.status_label = QLabel("Loading components...")
        layout.addWidget(self.status_label)
        
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Type component name (e.g. 'chainsaw.GmOptionSleep')")
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
        
    def _load_component_types(self):
        """Extract all component types from the type registry"""
        self.all_component_types = []
        
        if not self.type_registry:
            self.status_label.setText("Error: Type registry not available")
            return
            
        registry_type = type(self.type_registry).__name__
        has_registry_attr = hasattr(self.type_registry, "registry")
        
        if hasattr(self.type_registry, "registry"):
            registry_dict = self.type_registry.registry
        else:
            self.status_label.setText(f"Registry type: {registry_type}, has 'registry' attr: {has_registry_attr}")
            return
            
        if not registry_dict:
            self.status_label.setText("Registry dictionary is empty")
            return
            
        count = 0
        for type_key, type_info in registry_dict.items():
            if isinstance(type_info, dict) and "name" in type_info:
                type_name = type_info["name"]
                if not isinstance(type_name, str) or not type_name:
                    continue
                    
                # Skip system and collection types
                if (type_name.startswith("System.") or 
                    ".Collections." in type_name or 
                    "[]" in type_name):
                    continue
                    
                # Only add types with fields (likely to be valid components)
                if "fields" in type_info and type_info["fields"]:
                    self.all_component_types.append(type_name)
                    count += 1
        
        self.all_component_types.sort()
        
        self.status_label.setText(f"Found {count} component types")
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
                if len(matching_types) >= 600:
                    break
        
        self.component_list.addItems(matching_types)
        
        if self.component_list.count() > 0:
            self.component_list.setCurrentRow(0)
            
        self.status_label.setText(f"Showing first {len(matching_types)} matches out of {len(self.all_component_types)} components")
        
    def get_selected_component(self):
        """Return the selected component type name"""
        if self.result() == QDialog.Accepted:
            current_item = self.component_list.currentItem()
            if current_item:
                return current_item.text()
            else:
                return self.search_input.text() if self.search_input.text() else None
        return None
