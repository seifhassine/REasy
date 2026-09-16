"""Action tables, direct properties and reverse Node references for one FSM."""
from collections import Counter
import os

from PySide6.QtCore import Qt, QAbstractTableModel, QModelIndex, QSignalBlocker, QSortFilterProxyModel, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QTableView, QHeaderView, QAbstractItemView, QFormLayout,
    QScrollArea, QComboBox, QPushButton, QToolButton, QMessageBox, QTabWidget,
    QListView, QStyledItemDelegate,
)


def value_text(value):
    return str(value)


class ActionTableModel(QAbstractTableModel):
    edit_failed = Signal(str)

    def __init__(self, access, parent=None):
        super().__init__(parent)
        self.access = access
        self.rows = []
        self.columns = [('type', 'Type'), ('id', 'Action ID'), ('ex', 'Ex'), ('users', 'References')]

    def set_rows(self, rows, class_name, field_filter='', changed_key=None):
        columns = ([] if class_name else [('type', 'Type')]) + [
            ('id', 'Action ID'), ('ex', 'Ex'), ('users', 'References')]
        if class_name:
            representative = next((a for a in self.access.actions() if a.class_name == class_name), None)
            if representative is not None:
                columns += [('field', f.name) for f in self.access.instance(representative.key).fields
                            if f.name != 'v1_ID' and field_filter.casefold() in f.name.casefold()]
        if columns == self.columns and [a.key for a in rows] == [a.key for a in self.rows]:
            self.rows = rows
            if rows:
                if changed_key is None:
                    self.dataChanged.emit(self.index(0, 0), self.index(len(rows)-1, len(columns)-1))
                else:
                    row = next((i for i, action in enumerate(rows) if action.key == changed_key), None)
                    if row is not None:
                        self.dataChanged.emit(self.index(row, 0), self.index(row, len(columns)-1))
            return False
        self.beginResetModel()
        self.rows, self.columns = rows, columns
        self.endResetModel()
        return True

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole:
            return self.columns[section][1] if orientation == Qt.Horizontal else section+1

    def field(self, index):
        if index.isValid() and self.columns[index.column()][0] == 'field':
            return self.access.fields(self.rows[index.row()].key)[self.columns[index.column()][1]]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        action = self.rows[index.row()]
        kind, name = self.columns[index.column()]
        field = self.field(index)
        if role == Qt.UserRole:
            return action.key
        if role == Qt.ToolTipRole:
            if field is not None:
                return f'{field.name} · {field.type_name}'
            return f'{action.key.file}\n{action.key.block}[{action.key.instance}]\n0x{action.id_hash:08X}, ex={action.ex_id}'
        if role in (Qt.DisplayRole, Qt.EditRole):
            if field is not None:
                return value_text(field.value)
            return {'type': action.class_name, 'id': str(action.id_hash), 'ex': str(action.ex_id),
                    'users': str(len(self.access.users(action.key)))}[kind]

    def flags(self, index):
        flags = super().flags(index)
        field = self.field(index)
        if field is not None and field.binding is not None:
            flags |= Qt.ItemIsEditable
        return flags

    def setData(self, index, value, role=Qt.EditRole):
        field = self.field(index)
        if role != Qt.EditRole or field is None or field.binding is None:
            return False
        try:
            self.access.edit_field(field.binding, value)
        except ValueError as exc:
            self.edit_failed.emit(str(exc))
            return False
        return True


class ActionValueDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        field = index.model().field(index)
        if field is not None and field.binding is not None and field.binding.type_name == 'bool':
            editor = QComboBox(parent)
            editor.addItems(['True', 'False'])
            editor.activated.connect(lambda _: self.commitData.emit(editor))
            return editor
        return super().createEditor(parent, option, index)

    def setEditorData(self, editor, index):
        if isinstance(editor, QComboBox):
            editor.setCurrentText(index.data(Qt.EditRole))
        else:
            super().setEditorData(editor, index)

    def setModelData(self, editor, model, index):
        if isinstance(editor, QComboBox):
            model.setData(index, editor.currentText())
        else:
            super().setModelData(editor, model, index)


class ActionEditor(QWidget):
    action_selected = Signal(object)
    node_selected = Signal(int)
    node_activated = Signal(int)
    preview_requested = Signal()
    add_reference_requested = Signal(int)

    def __init__(self, handler, node_model, parent=None):
        super().__init__(parent)
        self.handler, self.access = handler, handler.editor_document
        self.selected_key = None
        self.node_index = None
        self.class_name = ''
        self.editors = {}
        self._property_key = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.splitter = QSplitter(Qt.Horizontal, self)
        layout.addWidget(self.splitter)

        navigation = QWidget(self)
        navigation.setMinimumWidth(185)
        nav = QVBoxLayout(navigation)
        nav.setContentsMargins(8, 8, 8, 8)
        resource = QLabel(os.path.basename(handler.filepath) or self.tr('FSM resource'), self)
        resource.setToolTip(handler.filepath)
        nav.addWidget(resource)
        self.navigation_tabs = QTabWidget(self)
        nav.addWidget(self.navigation_tabs)
        types_page = QWidget(self)
        types_layout = QVBoxLayout(types_page)
        types_layout.setContentsMargins(0, 6, 0, 0)
        self.type_search = QLineEdit(self)
        self.type_search.setPlaceholderText(self.tr('Find type…'))
        self.type_search.setClearButtonEnabled(True)
        types_layout.addWidget(self.type_search)
        self.types = QListWidget(self)
        types_layout.addWidget(self.types)
        self.navigation_tabs.addTab(types_page, self.tr('Types'))
        nodes_page = QWidget(self)
        nodes_layout = QVBoxLayout(nodes_page)
        nodes_layout.setContentsMargins(0, 6, 0, 0)
        self.node_search = QLineEdit(self)
        self.node_search.setPlaceholderText(self.tr('Find node or ID…'))
        self.node_search.setClearButtonEnabled(True)
        nodes_layout.addWidget(self.node_search)
        self.node_proxy = QSortFilterProxyModel(self)
        self.node_proxy.setSourceModel(node_model)
        self.node_proxy.setFilterRole(Qt.UserRole+1)
        self.node_proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.nodes = QListView(self)
        self.nodes.setModel(self.node_proxy)
        self.nodes.setUniformItemSizes(True)
        self.nodes.setEditTriggers(QAbstractItemView.NoEditTriggers)
        nodes_layout.addWidget(self.nodes)
        self.navigation_tabs.addTab(nodes_page, self.tr('Nodes'))
        self.splitter.addWidget(navigation)

        center = QWidget(self)
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(8, 8, 8, 8)
        scope_row = QHBoxLayout()
        self.scope_label = QLabel(self.tr('All nodes'), self)
        scope_row.addWidget(self.scope_label, 1)
        self.all_nodes = QPushButton(self.tr('All nodes'), self)
        self.all_nodes.setEnabled(False)
        scope_row.addWidget(self.all_nodes)
        self.add_reference = QPushButton(self.tr('Reference existing Action…'), self)
        self.add_reference.setEnabled(False)
        scope_row.addWidget(self.add_reference)
        center_layout.addLayout(scope_row)
        filters = QHBoxLayout()
        self.identity_filter = QLineEdit(self)
        self.identity_filter.setPlaceholderText(self.tr('Type / Action ID / RSZ identity…'))
        self.field_filter = QLineEdit(self)
        self.field_filter.setPlaceholderText(self.tr('Field name…'))
        self.value_filter = QLineEdit(self)
        self.value_filter.setPlaceholderText(self.tr('Field value…'))
        for entry in (self.identity_filter, self.field_filter, self.value_filter):
            entry.setClearButtonEnabled(True)
            filters.addWidget(entry)
            entry.textChanged.connect(self._refresh_rows)
        center_layout.addLayout(filters)
        self.table = QTableView(self)
        self.table_model = ActionTableModel(self.access, self)
        self.table.setModel(self.table_model)
        self.table.setItemDelegate(ActionValueDelegate(self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked | QAbstractItemView.EditKeyPressed)
        self.table.setAlternatingRowColors(False)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setDefaultSectionSize(130)
        center_layout.addWidget(self.table, 1)
        self.result_count = QLabel(self)
        center_layout.addWidget(self.result_count)
        self.splitter.addWidget(center)

        details = QSplitter(Qt.Vertical, self)
        details.setMinimumWidth(285)
        properties = QWidget(self)
        prop_layout = QVBoxLayout(properties)
        prop_layout.setContentsMargins(8, 8, 8, 8)
        self.title = QLabel(self.tr('Select an Action'), self)
        self.title.setWordWrap(True)
        self.title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        prop_layout.addWidget(self.title)
        self.sharing = QLabel(self)
        self.sharing.setWordWrap(True)
        prop_layout.addWidget(self.sharing)
        self.property_search = QLineEdit(self)
        self.property_search.setPlaceholderText(self.tr('Find property…'))
        self.property_search.setClearButtonEnabled(True)
        self.property_search.textChanged.connect(self._filter_properties)
        prop_layout.addWidget(self.property_search)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.property_container = QWidget(self)
        self.form = QFormLayout(self.property_container)
        self.form.setContentsMargins(0, 6, 4, 6)
        self.form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.scroll.setWidget(self.property_container)
        prop_layout.addWidget(self.scroll, 1)
        self.advanced_button = QToolButton(self)
        self.advanced_button.setText(self.tr('Advanced'))
        self.advanced_button.setCheckable(True)
        self.advanced_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.advanced_button.setArrowType(Qt.RightArrow)
        prop_layout.addWidget(self.advanced_button)
        self.advanced = QLabel(self)
        self.advanced.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.advanced.setWordWrap(True)
        self.advanced.hide()
        self.advanced_button.toggled.connect(self._toggle_advanced)
        prop_layout.addWidget(self.advanced)
        details.addWidget(properties)

        references = QWidget(self)
        refs_layout = QVBoxLayout(references)
        refs_layout.setContentsMargins(8, 8, 8, 8)
        refs_layout.addWidget(QLabel(self.tr('Node references'), self))
        self.references = QListWidget(self)
        refs_layout.addWidget(self.references, 1)
        buttons = QHBoxLayout()
        self.jump_button = QPushButton(self.tr('Go to node'), self)
        self.remove_button = QPushButton(self.tr('Remove reference'), self)
        self.preview_button = QPushButton(self.tr('Preview'), self)
        for button in (self.jump_button, self.remove_button, self.preview_button):
            buttons.addWidget(button)
        refs_layout.addLayout(buttons)
        details.addWidget(references)
        details.setSizes([550, 210])
        self.splitter.addWidget(details)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([230, 740, 360])

        self.type_search.textChanged.connect(self._filter_types)
        self.types.currentItemChanged.connect(self._type_changed)
        self.node_search.textChanged.connect(self.node_proxy.setFilterFixedString)
        self.nodes.selectionModel().currentChanged.connect(self._node_changed)
        self.all_nodes.clicked.connect(lambda: self.set_node(None))
        self.add_reference.clicked.connect(lambda: self.add_reference_requested.emit(self.node_index))
        self.table.selectionModel().currentRowChanged.connect(self._row_changed)
        self.table_model.edit_failed.connect(self._edit_failed)
        self.references.currentRowChanged.connect(self._reference_selection_changed)
        self.references.itemDoubleClicked.connect(lambda _: self._jump())
        self.jump_button.clicked.connect(self._jump)
        self.remove_button.clicked.connect(self._remove_reference)
        self.preview_button.clicked.connect(self.preview_requested)
        self.access.changed.connect(self._document_changed)
        self._refresh_types()
        self._refresh_rows()

    def _filter_types(self, text):
        for i in range(self.types.count()):
            item = self.types.item(i)
            item.setHidden(bool(item.data(Qt.UserRole)) and text.casefold() not in item.data(Qt.UserRole).casefold())

    def _refresh_types(self):
        counts = Counter(a.class_name for a in self.access.actions())
        with QSignalBlocker(self.types):
            self.types.clear()
            for name, count in [('', sum(counts.values())), *sorted(counts.items())]:
                item = QListWidgetItem(f'{name.rsplit(".", 1)[-1] if name else self.tr("All types")}  ({count})')
                item.setData(Qt.UserRole, name)
                item.setToolTip(name)
                self.types.addItem(item)
                if name == self.class_name:
                    self.types.setCurrentItem(item)
        self._filter_types(self.type_search.text())

    def _type_changed(self, current, previous):
        self.class_name = current.data(Qt.UserRole) if current is not None else ''
        self._refresh_rows()

    def _node_changed(self, current, previous):
        if current.isValid():
            index = current.data(Qt.UserRole)
            self.set_node(index)
            self.node_selected.emit(index)

    def set_node(self, index):
        self.node_index = index
        self.scope_label.setText(self.tr('All nodes') if index is None else
                                 f'[{index}] {self.access.document.get_node_by_index(index).name}')
        self.all_nodes.setEnabled(index is not None)
        self.add_reference.setEnabled(index is not None)
        with QSignalBlocker(self.nodes.selectionModel()):
            source = self.node_proxy.sourceModel().index(index, 0) if index is not None else QModelIndex()
            mapped = self.node_proxy.mapFromSource(source)
            self.nodes.setCurrentIndex(mapped)
            if mapped.isValid():
                self.nodes.scrollTo(mapped)
        self._refresh_rows()

    def _refresh_rows(self, *_, changed_key=None):
        key = self.selected_key
        rows = self.access.query(class_name=self.class_name, identity=self.identity_filter.text(),
                                 field_name=self.field_filter.text(), value=self.value_filter.text(),
                                 node_index=self.node_index)
        with QSignalBlocker(self.table.selectionModel()):
            reset = self.table_model.set_rows(rows, self.class_name, self.field_filter.text(), changed_key)
            row = next((i for i, action in enumerate(rows) if action.key == key), 0 if rows else -1)
            if reset or self.table.currentIndex().row() != row:
                self.table.setCurrentIndex(self.table_model.index(row, 0))
        if reset:
            self.table.setColumnWidth(0, 245 if not self.class_name else 120)
        self.result_count.setText(self.tr('{shown} / {total} Actions').format(shown=len(rows), total=len(self.access.actions())))
        self._select(rows[row].key if row >= 0 else None)

    def _row_changed(self, current, previous):
        self._select(current.data(Qt.UserRole) if current.isValid() else None)

    def select_action(self, key):
        previous = self.selected_key
        action = self.access.action(key)
        self.class_name = action.class_name
        self.selected_key = key
        with QSignalBlocker(self.identity_filter), QSignalBlocker(self.field_filter), QSignalBlocker(self.value_filter):
            self.identity_filter.clear()
            self.field_filter.clear()
            self.value_filter.clear()
        self._refresh_types()
        if self.node_index is not None and not any(ref.node_index == self.node_index for ref in self.access.users(key)):
            self.set_node(None)
        else:
            self._refresh_rows()
        self.table.doItemsLayout()
        self.table.scrollTo(self.table.currentIndex())
        if previous != self.selected_key:
            self.action_selected.emit(self.selected_key)

    def _select(self, key):
        changed = key != self.selected_key
        self.selected_key = key
        if self._property_key != key:
            self._build_properties()
        self._refresh_properties()
        self._refresh_references()
        if changed:
            self.action_selected.emit(key)

    def _build_properties(self):
        self._property_key = self.selected_key
        self.editors.clear()
        while self.form.rowCount():
            self.form.removeRow(0)
        if self.selected_key is None:
            return
        for field in self.access.instance(self.selected_key).fields:
            if field.name == 'v1_ID':
                continue
            if field.binding is None:
                editor = QLabel(value_text(field.value), self)
                editor.setTextInteractionFlags(Qt.TextSelectableByMouse)
                editor.setWordWrap(True)
            elif field.binding.type_name == 'bool':
                editor = QComboBox(self)
                editor.addItems(['True', 'False'])
                editor.activated.connect(lambda _, name=field.name: self._commit_property(name))
            else:
                editor = QLineEdit(self)
                editor.editingFinished.connect(lambda name=field.name: self._commit_property(name))
            editor.setObjectName('fsm_' + field.name)
            editor.setToolTip(field.type_name)
            self.editors[field.name] = editor
            self.form.addRow(field.name, editor)
        self._filter_properties(self.property_search.text())

    def _refresh_properties(self):
        key = self.selected_key
        self.preview_button.setEnabled(key is not None and self.access.action(key).class_name == 'snow.PlayerPlayMotion2')
        if key is None:
            self.title.setText(self.tr('Select an Action'))
            self.sharing.clear()
            self.advanced.clear()
            return
        action, instance = self.access.action(key), self.access.instance(key)
        self.title.setText(f'{action.class_name}\nAction {action.id_hash}:{action.ex_id}')
        users = self.access.users(key)
        count = len({ref.node_index for ref in users})
        self.sharing.setText((self.tr('Shared instance · ') if len(users) > 1 else '') +
                             self.tr('{nodes} nodes · {refs} references').format(nodes=count, refs=len(users)))
        self.advanced.setText(f'{key.block}[{key.instance}] · Type 0x{instance.type_id:08X}\n'
                              f'ID 0x{action.id_hash:08X} · Ex {action.ex_id}\n'
                              f'Offset {hex(instance.start_offset) if instance.start_offset is not None else "—"} · Size {instance.size}')
        self.advanced.setToolTip(key.file)
        for name, field in self.access.fields(key).items():
            if name not in self.editors:
                continue
            editor = self.editors[name]
            with QSignalBlocker(editor):
                if isinstance(editor, QComboBox):
                    editor.setCurrentText(value_text(field.value))
                elif not isinstance(editor, QLineEdit) or editor.text() != value_text(field.value):
                    editor.setText(value_text(field.value))

    def _commit_property(self, name):
        if self.selected_key is None:
            return
        editor = self.editors[name]
        value = editor.currentText() if isinstance(editor, QComboBox) else editor.text()
        try:
            self.access.edit_field(self.access.fields(self.selected_key)[name].binding, value)
        except ValueError as exc:
            self._edit_failed(str(exc))
        self._refresh_properties()

    def _filter_properties(self, text):
        for name, editor in self.editors.items():
            self.form.setRowVisible(editor, text.casefold() in name.casefold())

    def _toggle_advanced(self, checked):
        self.advanced.setVisible(checked)
        self.advanced_button.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)

    def _refresh_references(self):
        selected = self.references.currentItem()
        previous = selected.data(Qt.UserRole) if selected else None
        with QSignalBlocker(self.references):
            self.references.clear()
            if self.selected_key is not None:
                for ref in self.access.users(self.selected_key):
                    node = self.access.document.get_node_by_index(ref.node_index)
                    item = QListWidgetItem(f'[{ref.node_index}] {node.name} · Action slot {ref.position}')
                    item.setData(Qt.UserRole, ref)
                    item.setToolTip(f'Node 0x{node.id_hash:08X}, ex={node.ex_id}')
                    self.references.addItem(item)
                    if ref == previous:
                        self.references.setCurrentItem(item)
            if self.references.currentRow() < 0 and self.references.count():
                self.references.setCurrentRow(0)
        self._reference_selection_changed()

    def _reference_selection_changed(self, *_):
        selected = self.references.currentItem() is not None
        self.jump_button.setEnabled(selected)
        self.remove_button.setEnabled(selected)

    def _jump(self):
        item = self.references.currentItem()
        if item is not None:
            self.node_activated.emit(item.data(Qt.UserRole).node_index)

    def _remove_reference(self):
        item = self.references.currentItem()
        if item is not None:
            ref = item.data(Qt.UserRole)
            try:
                self.access.remove_reference(ref.node_index, ref.position)
            except (ValueError, IndexError) as exc:
                self._edit_failed(str(exc))

    def _document_changed(self, change):
        if change.references_changed:
            self._refresh_types()
        key = None
        if change.address is not None and change.address[0] == 'rsz':
            from .document import ObjectKey
            key = ObjectKey(self.access.scope, change.address[1], change.address[2])
        self._refresh_rows(changed_key=key)

    def _edit_failed(self, message):
        QMessageBox.warning(self, self.tr('Invalid value'), message)
