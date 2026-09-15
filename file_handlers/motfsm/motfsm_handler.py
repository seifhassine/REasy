"""
MOTFSM file handler for REasy.
Handles parsing and display of RE Engine FSM (Finite State Machine) files.
"""
from file_handlers.base_handler import BaseFileHandler
from file_handlers.motfsm.motfsm_file import MotfsmFile


class MotfsmHandler(BaseFileHandler):
    """Handler for MOTFSM files (.motfsm2)"""

    def __init__(self):
        super().__init__()
        self.motfsm = MotfsmFile()

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        """Check if data is a valid MOTFSM file"""
        return MotfsmFile.can_handle(data)

    def supports_editing(self) -> bool:
        """MOTFSM files support editing (in-place modification)"""
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
        self.modified = self.motfsm.bindings.modified

    def mark_saved(self):
        self.motfsm.accept_changes()
        self.modified = False
