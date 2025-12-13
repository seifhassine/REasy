"""
MOTFSM file handler for REasy.
Handles parsing and display of RE Engine FSM (Finite State Machine) files.
"""
import logging
from typing import Optional

from file_handlers.base_handler import BaseFileHandler
from file_handlers.motfsm.motfsm_file import MotfsmFile, MOTFSM_MAGIC

logger = logging.getLogger(__name__)


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
        """MOTFSM files are read-only for now"""
        return False

    def read(self, data: bytes):
        """Parse MOTFSM file data"""
        # Set RSZ type info path from app settings if available
        if hasattr(self, 'app') and self.app and hasattr(self.app, 'settings'):
            rsz_json_path = self.app.settings.get('rcol_json_path', '')
            if rsz_json_path:
                self.motfsm.set_rsz_type_info_path(rsz_json_path)

        self.motfsm.read(data)

    def rebuild(self) -> bytes:
        """Rebuild is not supported for MOTFSM files"""
        raise NotImplementedError("MOTFSM rebuild not yet supported")

    def create_viewer(self):
        """Create and return a viewer for MOTFSM files"""
        try:
            from file_handlers.motfsm.motfsm_viewer import MotfsmViewer
            return MotfsmViewer(self)
        except Exception as exc:
            logger.error("Failed to create MOTFSM viewer: %s", exc)
            return None
