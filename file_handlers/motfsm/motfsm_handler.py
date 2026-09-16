"""
MOTFSM file handler for REasy.
Handles parsing and display of RE Engine FSM (Finite State Machine) files.
"""
from file_handlers.base_handler import BaseFileHandler
from file_handlers.motfsm.motfsm_file import MotfsmFile
from PySide6.QtCore import Signal


class MotfsmHandler(BaseFileHandler):
    """Handler for MOTFSM files (.motfsm2)"""
    document_changed = Signal()
    field_changed = Signal(object)

    def __init__(self):
        super().__init__()
        self.motfsm = MotfsmFile()
        self._saved_source = b""
        self.editor_document = None

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        """Check if data is a valid MOTFSM file"""
        return MotfsmFile.can_handle(data)

    def supports_editing(self) -> bool:
        """Support scalar edits and node Action reference changes."""
        return True

    def read(self, data: bytes):
        """Parse MOTFSM file data"""
        self.motfsm = MotfsmFile()
        # Set RSZ type info path from app settings if available
        if hasattr(self, 'app') and self.app and hasattr(self.app, 'settings'):
            rsz_json_path = self.app.settings.get('rcol_json_path', '')
            if rsz_json_path:
                self.motfsm.set_rsz_type_info_path(rsz_json_path)

        self.motfsm.read(data)
        self._saved_source = bytes(data)
        self.modified = False
        from .document import FsmDocument
        if self.editor_document is not None:
            self.editor_document.deleteLater()
        self.editor_document = FsmDocument(self)

    def rebuild(self) -> bytes:
        """Rebuild MOTFSM file with modifications"""
        return self.motfsm.rebuild()

    def create_viewer(self):
        """Create and return a viewer for MOTFSM files"""
        from file_handlers.motfsm.graph_workspace import MotfsmGraphWorkspace
        return MotfsmGraphWorkspace(self)

    def edit_field(self, binding, text):
        self.editor_document.edit_field(binding, text)

    def _replace_actions(self, node_index, actions):
        self.editor_document.replace_actions(node_index, actions)

    def add_action_reference(self, node_index, id_hash, ex_id):
        self.editor_document.add_reference(node_index, id_hash, ex_id)

    def remove_action_reference(self, node_index, action_index):
        self.editor_document.remove_reference(node_index, action_index)

    def mark_saved(self):
        self.motfsm.accept_changes()
        self._saved_source = self.motfsm.source
        self.editor_document.mark_saved()
