from utils.id_manager import IdManager
from file_handlers.rsz.rsz_file import RszFile
from file_handlers.rsz.rsz_handler import RszHandler
from file_handlers.rsz.rsz_gameobject_clipboard import RszGameObjectClipboard
from file_handlers.rsz.rsz_component_clipboard import RszComponentClipboard


class WccHandler(RszHandler):
    """Handler for .wcc files containing only a headless RSZ section."""

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        # WCC is selected by extension in the factory to avoid collisions.
        return False

    def read(self, data: bytes):
        self.id_manager = IdManager.instance()
        self.init_type_registry()
        self.rsz_file = RszFile()
        self.rsz_file.type_registry = self.type_registry
        self.rsz_file.game_version = self._game_version
        self.rsz_file.filepath = self.filepath
        self.rsz_file.read_headless(data)
        self.rsz_file.auto_resource_management = self.auto_resource_management
        self.gameobject_clipboard = RszGameObjectClipboard()
        self.component_clipboard = RszComponentClipboard()

    def rebuild(self) -> bytes:
        if not self.rsz_file:
            raise ValueError("No WCC file loaded")
        return self.rsz_file.build_headless()
