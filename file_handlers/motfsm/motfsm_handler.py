"""
MOTFSM file handler for REasy.
Handles parsing and display of RE Engine FSM (Finite State Machine) files.
"""
from file_handlers.base_handler import BaseFileHandler
from file_handlers.motfsm.motfsm_file import MotfsmFile, Action
from PySide6.QtCore import Signal


class MotfsmHandler(BaseFileHandler):
    """Handler for MOTFSM files (.motfsm2)"""
    document_changed = Signal()

    def __init__(self):
        super().__init__()
        self.motfsm = MotfsmFile()
        self._saved_source = b""

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

    def rebuild(self) -> bytes:
        """Rebuild MOTFSM file with modifications"""
        return self.motfsm.rebuild()

    def create_viewer(self):
        """Create and return a viewer for MOTFSM files"""
        from file_handlers.motfsm.motfsm_viewer import MotfsmViewer
        return MotfsmViewer(self)

    def edit_field(self, binding, text):
        self.motfsm.edit_field(binding, binding.parse(text))
        self.modified = self.motfsm.rebuild() != self._saved_source

    def _replace_actions(self, node_index, actions):
        node = self.motfsm.get_node_by_index(node_index)
        original = node.actions
        node.actions = actions
        try:
            output = self.motfsm.rebuild()
            document = MotfsmFile()
            document.set_rsz_type_info_path(self.motfsm._registry_path)
            document.read(output)
        finally:
            node.actions = original
        self.motfsm = document
        self.modified = output != self._saved_source
        self.document_changed.emit()

    def add_action_reference(self, node_index, id_hash, ex_id):
        if self.motfsm.references.action(id_hash, ex_id) is None:
            raise ValueError('Select an existing Action')
        node = self.motfsm.get_node_by_index(node_index)
        self._replace_actions(node_index, [*node.actions, Action(id_hash, ex_id)])

    def remove_action_reference(self, node_index, action_index):
        actions = list(self.motfsm.get_node_by_index(node_index).actions)
        if not 0 <= action_index < len(actions):
            raise IndexError(f'Invalid Action reference index: {action_index}')
        del actions[action_index]
        self._replace_actions(node_index, actions)

    def mark_saved(self):
        self.motfsm.accept_changes()
        self._saved_source = self.motfsm.source
        self.modified = False
