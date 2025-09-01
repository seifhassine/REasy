import struct
from typing import Optional, Dict, Any

from file_handlers.base_handler import BaseFileHandler
from .motbank_file import MotbankFile, MOTBANK_MAGIC


class MotbankHandler(BaseFileHandler):
	def __init__(self):
		super().__init__()
		self.motbank: Optional[MotbankFile] = None

	@classmethod
	def can_handle(cls, data: bytes) -> bool:
		if len(data) < 8:
			return False
		magic = struct.unpack_from('<I', data, 4)[0]
		return magic == MOTBANK_MAGIC

	def supports_editing(self) -> bool:
		return True

	def read(self, data: bytes):
		f = MotbankFile()
		if not f.read(data):
			raise ValueError("Failed to parse Motbank file")
		self.motbank = f
		self.modified = False

	def rebuild(self) -> bytes:
		if not self.motbank:
			return b""
		result = self.motbank.write()
		self.modified = False
		return result

	def populate_treeview(self, tree, parent_item, metadata_map: dict):
		return

	def get_context_menu(self, tree, item, meta: dict):
		return None

	def handle_edit(self, meta: Dict[str, Any], new_val, old_val, item):
		pass

	def add_variables(self, target, prefix: str, count: int):
		pass

	def update_strings(self):
		pass

	def create_viewer(self):
		try:
			from .motbank_viewer import MotbankViewer
			v = MotbankViewer(self)
			v.modified_changed.connect(self.modified_changed.emit)
			return v
		except Exception:
			return None

