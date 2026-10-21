from PySide6.QtWidgets import (
	QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLineEdit,
	QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView
)
from PySide6.QtCore import Qt, Signal
lazy from ui.editor_widgets import styled_label, tool_button
from .motbank_file import MotlistItem


class MotbankViewer(QWidget):
	modified_changed = Signal(bool)

	def __init__(self, handler):
		super().__init__()
		self.setObjectName("motbankViewer")
		self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
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
		layout.setContentsMargins(8, 8, 8, 8)
		layout.setSpacing(8)

		paths = QGridLayout()
		paths.setHorizontalSpacing(8)
		paths.setVerticalSpacing(6)
		paths.setColumnStretch(1, 1)
		for row, text in enumerate(("Uvar Path:", "Jmap Path:")):
			label = styled_label(text, tone="muted")
			label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
			paths.addWidget(label, row, 0)
		self.uvar_edit = QLineEdit()
		self.uvar_edit.textChanged.connect(self._on_uvar_changed)
		paths.addWidget(self.uvar_edit, 0, 1)
		self.jmap_edit = QLineEdit()
		self.jmap_edit.textChanged.connect(self._on_jmap_changed)
		paths.addWidget(self.jmap_edit, 1, 1)
		layout.addLayout(paths)

		btns = QHBoxLayout()
		btns.setSpacing(2)
		self.add_btn = tool_button("add", slot=self._on_add, text=self.tr("Add"))
		self.del_btn = tool_button("delete", slot=self._on_delete, text=self.tr("Delete"))
		btns.addWidget(self.add_btn)
		btns.addWidget(self.del_btn)
		btns.addStretch()
		layout.addLayout(btns)

		self.table = QTableWidget(0, 4)
		self.table.setHorizontalHeaderLabels(["Path", "BankID", "BankType", "MaskBits"])
		self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
		self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
		self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
		self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
		self.table.verticalHeader().setDefaultSectionSize(24)
		self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
		self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
		self.table.itemChanged.connect(self._on_item_changed)
		self.table.itemSelectionChanged.connect(self._update_delete_button)
		layout.addWidget(self.table, 1)
		self._update_delete_button()

	def _update_delete_button(self):
		self.del_btn.setEnabled(bool(self.table.selectedIndexes()))

	@staticmethod
	def _table_item(text: str, column: int) -> QTableWidgetItem:
		item = QTableWidgetItem(text)
		if column:
			item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
		return item

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
			self.table.setItem(r, 0, self._table_item(it.path, 0))
			self.table.setItem(r, 1, self._table_item(str(it.bank_id), 1))
			self.table.setItem(r, 2, self._table_item(str(it.bank_type), 2))
			self.table.setItem(r, 3, self._table_item(str(it.bank_type_mask_bits), 3))
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
			self.table.setItem(r, c, self._table_item(txt, c))
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

