from utils.id_manager import IdManager
from file_handlers.rsz.rsz_file import RszFile
from file_handlers.rsz.rsz_handler import RszHandler
from file_handlers.rsz.rsz_gameobject_clipboard import RszGameObjectClipboard
from file_handlers.rsz.rsz_component_clipboard import RszComponentClipboard


class WccHandler(RszHandler):
    """Handler for headless RSZ files (.wcc and the other kinds the factory routes by extension)."""

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        # WCC is selected by extension in the factory to avoid collisions.
        return False

    def read(self, data: bytes, validate_type_registry: bool = False):
        self.init_type_registry()
        rsz_file = RszFile()
        rsz_file.type_registry = self.type_registry
        rsz_file.game_version = self._game_version
        rsz_file.filepath = self.filepath
        rsz_file.read_headless(data, validate_type_registry=validate_type_registry)
        self.attach(rsz_file)

    def attach(self, rsz_file: RszFile) -> None:
        self.id_manager = IdManager.instance()
        self.rsz_file = rsz_file
        self.rsz_file.auto_resource_management = self.auto_resource_management
        self.gameobject_clipboard = RszGameObjectClipboard()
        self.component_clipboard = RszComponentClipboard()

    def rebuild(self) -> bytes:
        if not self.rsz_file:
            raise ValueError("No WCC file loaded")
        return self.rsz_file.build_headless()
