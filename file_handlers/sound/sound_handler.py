from __future__ import annotations

from pathlib import Path

from file_handlers.base_handler import FileHandler
from .bnk_parser import rewrite_soundbank

SOUND_MAGICS = frozenset({b"BKHD", b"AKPK", b"SBNK", b"SPCK"})


class SoundHandler(FileHandler):
    def __init__(self):
        super().__init__()
        self.raw_data: bytes = b""
        self.filename: str = ""
        self.extension: str = ""
        self._replacements: dict[int, bytes] = {}

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        return data[:4] in SOUND_MAGICS

    def supports_editing(self) -> bool:
        return True

    def read(self, data: bytes):
        self.raw_data = data
        self.extension = Path(self.filename).suffix.lower()
        self._replacements.clear()
        self.modified = False

    def rebuild(self) -> bytes:
        self.raw_data = rewrite_soundbank(self.raw_data, self._replacements)
        self._replacements.clear()
        self.modified = False
        return self.raw_data

    def replace_track_data(self, source_id: int, wem_data: bytes):
        self._replacements[int(source_id)] = bytes(wem_data)
        self.modified = True

    def populate_treeview(self, tree, parent_item, metadata_map: dict): pass
    def get_context_menu(self, tree, item, meta: dict): pass
    def handle_edit(self, meta: dict, new_val, old_val, item): pass
    def add_variables(self, target, prefix: str, count: int): pass
    def update_strings(self): pass

    def create_viewer(self):
        try:
            from .sound_viewer import SoundViewer
        except Exception:
            return None
        viewer = SoundViewer(self)
        viewer.modified_changed.connect(self.modified_changed.emit)
        return viewer
