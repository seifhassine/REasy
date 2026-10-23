from file_handlers.base_handler import BaseFileHandler
from .efx_file import EFX_MAGIC
from .mhw_efx_file import MHW_EFX_MAGIC, MhwEfxFile
from .format_registry import read_efx


class EfxHandler(BaseFileHandler):
    def __init__(self):
        super().__init__()
        self.efx = None

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        return data[:4] in (EFX_MAGIC, MHW_EFX_MAGIC)

    def supports_editing(self) -> bool:
        return not isinstance(self.efx, MhwEfxFile)

    def read(self, data: bytes):
        self.efx = read_efx(data, self.filepath)
        self.modified = False

    def rebuild(self) -> bytes:
        return self.efx.build_validated()

    def create_viewer(self):
        if isinstance(self.efx, MhwEfxFile):
            from .mhw_efx_viewer import MhwEfxViewer
            return MhwEfxViewer(self)
        from .efx_viewer import EfxViewer
        return EfxViewer(self)
