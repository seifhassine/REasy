from abc import ABC, abstractmethod
from utils.type_registry import TypeRegistry
from PySide6.QtCore import QObject, Signal

class BaseFileHandler(QObject):
    """Base class for file handlers with common functionality"""
    modified_changed = Signal(bool)  # Add signal for modification state
    
    def __init__(self):
        super().__init__()
        self.app = None
        self.refresh_tree_callback = None
        self.type_registry = None
        self.dark_mode = False
        self.show_advanced = False
        self._modified = False
        
    @property
    def modified(self):
        return self._modified

    @modified.setter
    def modified(self, value):
        if self._modified != value:
            self._modified = value
            self.modified_changed.emit(value)

    def init_type_registry(self, json_path=""):
        """Initialize type registry from settings"""
        if not json_path and self.app and hasattr(self.app, 'settings'):
            json_path = self.app.settings.get('rcol_json_path', '')
        self.type_registry = TypeRegistry(json_path)

    def supports_editing(self) -> bool:
        """Default to supporting editing"""
        return True

    def create_viewer(self):
        """Create and return a viewer instance - override in subclasses"""
        raise NotImplementedError()

    def read(self, data: bytes):
        """Read file data - override in subclasses"""
        raise NotImplementedError()

    def rebuild(self) -> bytes:
        """Rebuild file data - override in subclasses"""
        raise NotImplementedError()

# Change FileHandler to inherit only from BaseFileHandler
class FileHandler(BaseFileHandler):
    @classmethod
    @abstractmethod
    def can_handle(cls, data: bytes) -> bool:
        """Return True if this handler can parse the given data."""
        pass

    @abstractmethod
    def read(self, data: bytes):
        """Parse the raw data into internal structures."""
        pass

    @abstractmethod
    def rebuild(self) -> bytes:
        """Rebuild the file and return its raw data."""
        pass

    @abstractmethod
    def populate_treeview(self, tree, parent_item, metadata_map: dict):
        """Populate the tree with a representation of this file."""
        pass

    @abstractmethod
    def get_context_menu(self, tree, item, meta: dict):
        """Return a context menu for the given tree row (or None)."""
        pass

    @abstractmethod
    def handle_edit(self, meta: dict, new_val, old_val, item):
        """Handle an edit for a given tree node."""
        pass

    @abstractmethod
    def add_variables(self, target, prefix: str, count: int):
        """Add new variables to the given target file object."""
        pass

    @abstractmethod
    def update_strings(self):
        """Update the internal string data."""
        pass