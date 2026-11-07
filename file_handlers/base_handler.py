from abc import abstractmethod
lazy from utils.type_registry import TypeRegistry
from PySide6.QtCore import QObject, Signal

class BaseFileHandler(QObject):
    modified_changed = Signal(bool)
    
    def __init__(self):
        super().__init__()
        self.app = None
        self.resource_context = None
        self.filepath = ""
        self.refresh_tree_callback = None
        self.type_registry = None
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
        if not json_path and self.app and hasattr(self.app, 'settings'):
            json_path = self.app.settings.get('rcol_json_path', '')
        self.type_registry = TypeRegistry(json_path)

    def supports_editing(self) -> bool:
        return True

    def supports_tree_editing(self) -> bool:
        """Return whether the generic tree editor may call validate_edit/handle_edit."""
        return False

    def open_externally(self, filename=None, data=None, pak_source_path=None) -> bool:
        return False

    def create_viewer(self):
        raise NotImplementedError()

    def read(self, data: bytes):
        raise NotImplementedError()

    def rebuild(self) -> bytes:
        raise NotImplementedError()


class DocumentHandler(BaseFileHandler):
    def rebuild(self) -> bytes:
        data = self.document.write()
        self.document.mark_saved()
        self.modified = False
        return data

    def _set_modified(self, modified: bool) -> None:
        self.modified = modified


class FileHandler(BaseFileHandler):
    @classmethod
    @abstractmethod
    def can_handle(cls, data: bytes) -> bool:
        pass

    @abstractmethod
    def read(self, data: bytes):
        pass

    @abstractmethod
    def rebuild(self) -> bytes:
        pass


