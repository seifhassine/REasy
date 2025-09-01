from PySide6.QtWidgets import (
	QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
	QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView
)
from PySide6.QtCore import Qt, Signal
from .motbank_file import MotlistItem


class MotbankViewer(QWidget):
	modified_changed = Signal(bool)

	def __init__(self, handler):
		super().__init__()
		self.handler = handler
		self._modified = False
		self._setup_ui()
		self._populate()

	@property
	def modified(self):
		return self._modified

	@modified.setter
	def modified(self, value: bool):
		if self._modified != value:
			self._modified = value
			self.modified_changed.emit(value)

	def _setup_ui(self):
		layout = QVBoxLayout(self)

		row = QHBoxLayout()
		row.addWidget(QLabel("Uvar Path:"))
		self.uvar_edit = QLineEdit()
		self.uvar_edit.textChanged.connect(self._on_uvar_changed)
		row.addWidget(self.uvar_edit)
		layout.addLayout(row)

		row2 = QHBoxLayout()
		row2.addWidget(QLabel("Jmap Path:"))
		self.jmap_edit = QLineEdit()
		self.jmap_edit.textChanged.connect(self._on_jmap_changed)
		row2.addWidget(self.jmap_edit)
		layout.addLayout(row2)

		self.table = QTableWidget(0, 4)
		self.table.setHorizontalHeaderLabels(["Path", "BankID", "BankType", "MaskBits"])
		self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
		self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
		self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
		self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
		self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
		self.table.itemChanged.connect(self._on_item_changed)
		layout.addWidget(self.table)

		btns = QHBoxLayout()
		self.add_btn = QPushButton("Add")
		self.add_btn.clicked.connect(self._on_add)
		self.del_btn = QPushButton("Delete")
		self.del_btn.clicked.connect(self._on_delete)
		btns.addWidget(self.add_btn)
		btns.addWidget(self.del_btn)
		btns.addStretch()
		layout.addLayout(btns)

	def _populate(self):
		mb = self.handler.motbank
		if not mb:
			return
		self.uvar_edit.blockSignals(True)
		self.uvar_edit.setText(mb.uvar_path)
		self.uvar_edit.blockSignals(False)
		if hasattr(self, 'jmap_edit'):
			self.jmap_edit.blockSignals(True)
			self.jmap_edit.setText(getattr(mb, 'jmap_path', ""))
			self.jmap_edit.blockSignals(False)
		self.table.blockSignals(True)
		self.table.setRowCount(len(mb.items))
		for r, it in enumerate(mb.items):
			self.table.setItem(r, 0, QTableWidgetItem(it.path))
			self.table.setItem(r, 1, QTableWidgetItem(str(it.bank_id)))
			self.table.setItem(r, 2, QTableWidgetItem(str(it.bank_type)))
			self.table.setItem(r, 3, QTableWidgetItem(str(it.bank_type_mask_bits)))
		self.table.blockSignals(False)

	def _on_uvar_changed(self, text: str):
		if self.handler.motbank:
			self.handler.motbank.uvar_path = text
			self.modified = True

	def _on_jmap_changed(self, text: str):
		if self.handler.motbank:
			setattr(self.handler.motbank, 'jmap_path', text)
			self.modified = True

	def _on_item_changed(self, item):
		r = item.row()
		c = item.column()
		mb = self.handler.motbank
		if not mb or r >= len(mb.items):
			return
		it = mb.items[r]
		val = item.text()
		try:
			if c == 0:
				it.path = val
			elif c == 1:
				it.bank_id = int(val)
			elif c == 2:
				it.bank_type = int(val)
			elif c == 3:
				it.bank_type_mask_bits = int(val)
			self.modified = True
		except ValueError:
			pass

	def _on_add(self):
		mb = self.handler.motbank
		if not mb:
			return
		mb.items.append(MotlistItem())
		self.table.blockSignals(True)
		r = self.table.rowCount()
		self.table.insertRow(r)
		for c, txt in enumerate(["", "0", "0", "0"]):
			self.table.setItem(r, c, QTableWidgetItem(txt))
		self.table.blockSignals(False)
		self.modified = True

	def _on_delete(self):
		rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
		mb = self.handler.motbank
		if not mb:
			return
		for r in rows:
			if 0 <= r < len(mb.items):
				del mb.items[r]
				self.table.removeRow(r)
		if rows:
			self.modified = True

