import os
import tempfile

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

from file_handlers.base_handler import BaseFileHandler


class MovHandler(BaseFileHandler):
    """RE Engine movie files (`name.mov.<n>[.x64|.stm]`).

    The payload is a QuickTime MOV that REasy cannot edit, so these files are
    handed to the OS default player as plain `.mov` files.
    """

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        return False

    def open_externally(self, filename, data=None, pak_source_path=None) -> bool:
        if not filename:
            return False
        try:
            if data is None:
                if pak_source_path or not os.path.isfile(filename):
                    return False
                with open(filename, "rb") as f:
                    data = f.read()
            fd, tmp_path = tempfile.mkstemp(
                prefix=os.path.splitext(os.path.basename(filename))[0] + "-",
                suffix=".mov",
            )
            os.close(fd)
            with open(tmp_path, "wb") as f:
                f.write(data)
            QDesktopServices.openUrl(QUrl.fromLocalFile(tmp_path))
            return True
        except Exception as e:
            print(f"Could not open {os.path.basename(filename)} externally: {e}")
            return False
