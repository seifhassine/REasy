import json
import struct
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTreeWidget, QTreeWidgetItem,
    QStyledItemDelegate, QMessageBox, QHBoxLayout, QPushButton, QInputDialog, QMenu, QTreeWidgetItemIterator, QApplication, QFileDialog, QDialog, QDialogButtonBox, QListWidget,
)

from .efx_file import EfxFile, EfxRecord, EfxEntry, EfxAttribute, PRIMITIVES, schema_for_version
from .editing import EfxEditor, data, documents, locate, structural_fields, tokens, path_text

PATH_ROLE = int(Qt.ItemDataRole.UserRole) + 1


class ValueDelegate(QStyledItemDelegate):
    def __init__(self, viewer):
        super().__init__(viewer.tree); self.viewer = viewer

    def createEditor(self, parent, option, index):
        if index.column() == 1:
            from .value_widgets import ValueWidget
            binding = index.data(Qt.ItemDataRole.UserRole)
            if binding:
                field, _, kind = binding
                path = index.siblingAtColumn(0).data(PATH_ROLE)
                loc = locate(self.viewer.editor.document, path)
                return ValueWidget(kind, loc.value, field.definition if field else {}, loc.document.version, parent)
        return None

    def setEditorData(self, editor, index):
        pass  # ValueWidget is initialized with the native value, not the display string.

    def setModelData(self, editor, model, index):
        try:
            model.setData(index, str(editor.value()), Qt.ItemDataRole.EditRole)
        except (ValueError, OverflowError, struct.error) as error:
            QMessageBox.warning(self.viewer, 'Invalid value', str(error))


class EfxViewer(QWidget):
    modified_changed = Signal(bool)

    def __init__(self, handler, parent=None):
        super().__init__(parent)
        self.handler = handler
        self.editor = EfxEditor(handler.efx)
        self._bone_rows = {}
        self._binding_rows = {}
        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels(["Field", "Value", "Type"])
        self.tree.setItemDelegate(ValueDelegate(self))
        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        self.buttons = {}
        for label, callback in (("New entry", self._new_entry), ("Add", self._add),
                                ("Import", self._import_file), ("Copy", self._copy), ("Paste", self._paste),
                                ("Duplicate", self._duplicate), ("Delete", self._delete),
                                ("Up", lambda: self._move(-1)), ("Down", lambda: self._move(1)),
                                ("Undo", self._undo), ("Redo", self._redo)):
            button = QPushButton(self.tr(label))
            button.clicked.connect(callback)
            toolbar.addWidget(button)
            self.buttons[label] = button
        layout.addLayout(toolbar)
        authoring = QHBoxLayout()
        for label, callback in [('Parameters', self._parameters), ('Curves', lambda: self._channels('curve')),
                                ('Expressions', lambda: self._channels('expression')), ('Sequence', self._sequence), ('Linked UVS', self._linked_uvs), ('Preview', self._preview)]:
            button = QPushButton(self.tr(label)); button.clicked.connect(callback); authoring.addWidget(button); self.buttons[label] = button
        authoring.addStretch(); layout.addLayout(authoring)
        layout.addWidget(self.tree)
        self._file(self.tree.invisibleRootItem(), handler.efx)
        self.tree.expandToDepth(1)
        self.tree.setColumnWidth(0, 340)
        self.tree.setColumnWidth(1, 380)
        self.tree.itemChanged.connect(self._edit)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.currentItemChanged.connect(self._update_buttons)
        QShortcut(QKeySequence.StandardKey.Undo, self, activated=self._undo)
        QShortcut(QKeySequence.StandardKey.Redo, self, activated=self._redo)
        self.handler.modified_changed.connect(self._saved_state_changed)
        self._update_buttons()

    def _row(self, parent, name, value="", kind="", binding=None, path=None):
        row = QTreeWidgetItem(parent, [str(name), str(value), kind])
        if path is not None:
            row.setData(0, PATH_ROLE, tokens(path))
        if binding is not None:
            row.setData(1, Qt.ItemDataRole.UserRole, binding)
            row.setFlags(row.flags() | Qt.ItemFlag.ItemIsEditable)
        return row

    def _file(self, parent, document, prefix=()):
        self._row(parent, "Version", document.version, "EFX")
        self._row(parent, "Dimension", document.dimension, "S32", binding=(None, (), 'S32'), path=prefix + ('dimension',))
        self._row(parent, 'Header word 11', document.header_flags, 'U32', binding=(None, (), 'U32'), path=prefix + ('header_flags',))
        bones = self._row(parent, "Bones", len(document.bones), path=prefix + ("bones",))
        self._bone_rows[id(document)] = (document, bones)
        for index, bone in enumerate(document.bones):
            base = prefix + ("bones", index)
            node = self._row(bones, index, bone.name, "Bone", path=base)
            self._row(node, "Name", bone.name, "String", binding=(None, (), "String"), path=base + ("name",))
            self._row(node, 'Value', bone.value, 'U32', binding=(None, (), 'U32'), path=base + ('value',))
        for title, entries, key in (("Entries", document.entries, "entries"), ("Actions", document.actions, "actions")):
            group = self._row(parent, title, len(entries), path=prefix + (key,))
            for ei, entry in enumerate(entries):
                base = prefix + (key, ei)
                node = self._row(group, f"{entry.index}: {entry.name}", entry.assignment, "Entry", path=base)
                self._row(node, "Name", entry.name, "String", binding=(None, (), "String"), path=base + ("name",))
                self._row(node, 'Index', entry.index, 'S32', binding=(None, (), 'S32'), path=base + ('index',))
                if key == "entries":
                    self._row(node, "Assignment", entry.assignment, "S32", binding=(None, (), "S32"), path=base + ("assignment",))
                attrs = self._row(node, "Attributes", len(entry.attributes), path=base + ("attributes",))
                for ai, attribute in enumerate(entry.attributes):
                    attr_path = base + ("attributes", ai)
                    row = self._row(attrs, attribute.name, attribute.unique_id, f"ID {attribute.type_id}", path=attr_path)
                    self._row(row, 'Unique ID', attribute.unique_id, 'S32', binding=(None, (), 'S32'), path=attr_path + ('unique_id',))
                    for slot, name in enumerate(attribute.bone_fields):
                        bone = attribute.bone_bindings[slot] if slot < len(attribute.bone_bindings) else None
                        label = "ParentBone" if len(attribute.bone_fields) == 1 else f"ParentBone[{name}]"
                        bone_row = self._row(row, label, bone.name if bone else "", "Bone")
                        self._binding_rows[id(attribute.body.fields[name])] = (attribute, slot, bone_row)
                    self._record(row, attribute.body, prefix=attr_path + ("fields",))
        for title, records, key in (("ExpressionParameters", document.expression_parameters, "expression_parameters"),
                                    ("FieldParameters", document.field_parameters, "field_parameters"),
                                    ("Groups", document.groups, "groups"), ("UserVariables", document.uvar_groups, "uvar_groups")):
            group = self._row(parent, title, len(records), path=prefix + (key,))
            for index, record in enumerate(records):
                base = prefix + (key, index)
                row = self._row(group, index, record.name or "", record.classname, path=base)
                if record.name is not None:
                    self._row(row, "Name", record.name, "String", binding=(None, (), "String"), path=base + ("name",))
                self._record(row, record, prefix=base + ("fields",))

    def _record(self, parent, record, *, metadata=False, prefix=()):
        # Lengths and variant selectors describe the layout; the writer owns them.
        structural = structural_fields(record)
        for name, item in record.fields.items():
            readonly = metadata or name in structural or item.definition.get("Flag") in ("StructSize", "StringLength") or "CountOf" in item.definition or "ByteCountOf" in item.definition
            self._value(parent, name, item.value, item.kind, item, readonly=readonly, prefix=prefix + (name,))

    def _value(self, parent, name, value, kind, field, *, readonly=False, path=(), prefix=()):
        if isinstance(value, EfxFile):
            self._file(self._row(parent, name, "", "EFX", path=prefix), value, prefix)
        elif isinstance(value, EfxRecord):
            self._record(self._row(parent, name, "", value.classname, path=prefix), value, metadata=readonly, prefix=prefix)
        elif isinstance(value, (tuple, list)):
            row = self._row(parent, name, len(value), kind, path=prefix)
            for component, element in enumerate(value):
                element_kind = field.definition.get("FieldType", "") if kind == "Array" else ("F32" if "f" in PRIMITIVES[kind] else "S32")
                self._value(row, component, element, element_kind, field,
                            readonly=readonly, path=path + (component,), prefix=prefix + (component,))
        else:
            binding = None if readonly else (field, path, kind)
            self._row(parent, name, value, kind, binding, path=prefix)

    def _edit(self, row, column):
        binding = row.data(1, Qt.ItemDataRole.UserRole)
        if column != 1 or binding is None:
            return
        field, path, kind = binding
        target = row.data(0, PATH_ROLE)
        previous = locate(self.editor.document, target).value
        text = row.text(1)
        try:
            if isinstance(previous, bool):
                if text.casefold() not in ("true", "false", "0", "1"):
                    raise ValueError("Expected true, false, 0 or 1")
                value = text.casefold() in ("true", "1")
            elif isinstance(previous, float):
                value = float(text)
                struct.pack("<f", value)
            elif isinstance(previous, int):
                value = int(text, 16 if text.lower().startswith(("0x", "-0x")) else 10)
                struct.pack("<" + PRIMITIVES[kind], value)
            else:
                value = text
                value.encode(field.definition.get("StringEncoding", "utf-16le") if field else "utf-16le")
                if "\0" in value:
                    raise ValueError("String cannot contain a null character")
        except (ValueError, OverflowError, struct.error, UnicodeError) as error:
            self.tree.blockSignals(True)
            row.setText(1, str(previous))
            self.tree.blockSignals(False)
            QMessageBox.warning(self, "Invalid value", str(error))
            return
        if previous == value:
            return
        try:
            self.editor.apply({"op": "set", "path": target, "value": value})
        except (ValueError, KeyError, IndexError, TypeError, OverflowError, struct.error) as error:
            self.tree.blockSignals(True)
            row.setText(1, str(previous))
            self.tree.blockSignals(False)
            self._sync(refresh=True)
            QMessageBox.warning(self, self.tr("Invalid value"), str(error))
            return
        if id(field) in self._binding_rows:
            self.handler.efx._prepare_bindings()
            attribute, slot, bone_row = self._binding_rows[id(field)]
            self.tree.blockSignals(True)
            bone = attribute.bone_bindings[slot]
            bone_row.setText(1, bone.name if bone else "")
            for document, bones in self._bone_rows.values():
                bones.takeChildren()
                bones.setText(1, str(len(document.bones)))
                prefix = next(p for p, d in documents(self.editor.document) if d is document)
                for index, bone in enumerate(document.bones):
                    base = prefix + ("bones", index)
                    node = self._row(bones, index, bone.name, "Bone", path=base)
                    self._row(node, "Name", bone.name, "String", binding=(None, (), "String"), path=base + ("name",))
                    self._row(node, 'Value', bone.value, 'U32', binding=(None, (), 'U32'), path=base + ('value',))
            self.tree.blockSignals(False)
        self._sync(refresh=field is None)

    def _saved_state_changed(self, modified):
        if not modified:
            self.editor.mark_clean()

    def _sync(self, *, refresh=True, selection=None):
        self.handler.efx = self.editor.document
        # Undo reaching the clean state must not redefine the saved state.
        self.handler.modified_changed.disconnect(self._saved_state_changed)
        try:
            self.handler.modified = self.editor.modified
        finally:
            self.handler.modified_changed.connect(self._saved_state_changed)
        self.modified_changed.emit(self.handler.modified)
        if refresh:
            selection = selection if selection is not None else self._selected_path()
            self.tree.blockSignals(True)
            self.tree.clear()
            self._bone_rows.clear()
            self._binding_rows.clear()
            self._file(self.tree.invisibleRootItem(), self.editor.document)
            self.tree.expandToDepth(1)
            if selection:
                iterator = QTreeWidgetItemIterator(self.tree)
                while iterator.value():
                    item = iterator.value()
                    if item.data(0, PATH_ROLE) == tuple(selection):
                        self.tree.setCurrentItem(item)
                        parent = item.parent()
                        while parent:
                            parent.setExpanded(True)
                            parent = parent.parent()
                        break
                    iterator += 1
            self.tree.blockSignals(False)
        self._update_buttons()

    def _selected_path(self):
        row = self.tree.currentItem()
        return row.data(0, PATH_ROLE) if row else None

    def _run(self, operation, *, donor=None):
        try:
            result = self.editor.apply(operation, donor=donor)
        except (ValueError, KeyError, IndexError, TypeError, OverflowError, struct.error) as error:
            self._sync()
            QMessageBox.warning(self, self.tr("EFX edit failed"), str(error))
            return
        self._sync(selection=tokens(result))
        return result

    def _document_prefix(self):
        path = self._selected_path()
        doc = locate(self.editor.document, path).document if path else self.editor.document
        return next(prefix for prefix, candidate in documents(self.editor.document) if candidate is doc)

    def _new_entry(self):
        name, accepted = QInputDialog.getText(self, self.tr("New entry"), self.tr("Name"))
        if accepted and name:
            selected = self._selected_path()
            prefix = self._document_prefix()
            relative = selected[len(prefix):] if selected else ()
            category = "actions" if relative and relative[0] == "actions" else "entries"
            self._run({"op": "insert", "path": prefix + (category,), "value": {"name": name}})

    def _add(self):
        path = self._selected_path()
        if path is None:
            return
        loc = locate(self.editor.document, path)
        if isinstance(loc.value, EfxEntry):
            path += ("attributes",)
            loc = locate(self.editor.document, path)
        if not isinstance(loc.value, list):
            return
        if isinstance(loc.parent, EfxEntry):
            types = sorted(set(schema_for_version(loc.document.version)["AttributeTypes"]) - {a.name for a in loc.value})
            name, ok = QInputDialog.getItem(self, self.tr("Add attribute"), self.tr("Type"), types, editable=False)
            if not ok:
                return
            prefix = next(p for p, d in documents(self.editor.document) if d is loc.document)
            templates = []
            for category in ("entries", "actions"):
                for ei, entry in enumerate(getattr(loc.document, category)):
                    for ai, attr in enumerate(entry.attributes):
                        if attr.name == name:
                            templates.append((f"{category}/{ei}: {entry.name}", prefix + (category, ei, "attributes", ai)))
            if templates:
                chosen, ok = QInputDialog.getItem(self, self.tr("Attribute template"), self.tr("Source"), [label for label, _ in templates], editable=False)
                if ok:
                    self._run({"op": "copy", "source": dict(templates)[chosen], "path": path})
                return
            definition = schema_for_version(loc.document.version)["AttributeTypes"][name]
            initial = {"type": name, "fields": {f["Name"]: None for f in definition["Fields"]
                       if f.get("Flag") not in ("StructSize", "StringLength") and "CountOf" not in f and "ByteCountOf" not in f}}
        elif loc.key in ("entries", "actions"):
            self._new_entry()
            return
        elif loc.key == "groups":
            initial = {"name": "Group", "indices": []}
        elif loc.key == "bones":
            initial = {"name": "Bone", "value": 0}
        elif loc.key == "expression_parameters":
            initial = {"name": "Parameter", "type": 0, "value": 0.0}
        elif loc.key == "field_parameters":
            initial = data(loc.value[-1]) if loc.value else {"name": "Field", "fields": {
                "unknown0": None, "unknown2": None, "type": None, "unknown4": None,
                "value1": None, "value2": None, "value3": None, "value4": None, "value5": None, "value6": None}}
            initial["name"] += "_copy" if loc.value else ""
        elif loc.field:
            initial = data(loc.value[-1]) if loc.value else None
        else:
            return
        value, ok = QInputDialog.getMultiLineText(self, self.tr("Add"), self.tr("JSON value"), json.dumps(initial, ensure_ascii=False, indent=2))
        if ok:
            try:
                payload = json.loads(value)
            except ValueError as error:
                QMessageBox.warning(self, self.tr("Invalid value"), str(error))
                return
            self._run({"op": "insert", "path": path, "value": payload})

    def _duplicate(self):
        path = self._selected_path()
        if path and isinstance(locate(self.editor.document, path).parent, list):
            self._run({"op": "copy", "source": path, "path": path[:-1], "index": int(path[-1]) + 1})

    def _copy_to(self):
        path = self._selected_path()
        if not path or not isinstance(locate(self.editor.document, path).value, EfxAttribute):
            return
        loc = locate(self.editor.document, path)
        prefix = self._document_prefix()
        choices = {f"{category}/{i}: {entry.name}": prefix + (category, i, "attributes")
                   for category in ("entries", "actions") for i, entry in enumerate(getattr(loc.document, category))
                   if all(a.name != loc.value.name for a in entry.attributes)}
        if not choices:
            return
        name, ok = QInputDialog.getItem(self, self.tr("Copy attribute"), self.tr("Destination"), list(choices), editable=False)
        if ok:
            self._run({"op": "copy", "source": path, "path": choices[name]})

    def _delete(self):
        path = self._selected_path()
        if path:
            self._run({"op": "delete", "path": path})

    def _move(self, delta):
        path = self._selected_path()
        if path and isinstance(locate(self.editor.document, path).parent, list):
            self._run({"op": "move", "path": path, "index": int(path[-1]) + delta})

    def _undo(self):
        if self.editor.undo():
            self._sync()

    def _redo(self):
        if self.editor.redo():
            self._sync()

    def _copy(self):
        from .templates import export_template
        path = self._selected_path()
        if path:
            QApplication.clipboard().setText(export_template(self.editor.document, path))

    def _destination(self, source_node, source_path):
        path = self._selected_path()
        if path:
            loc = locate(self.editor.document, path)
            if isinstance(loc.value, list):
                return path
            if isinstance(source_node, EfxAttribute) and isinstance(loc.value, EfxEntry):
                return path + ("attributes",)
            if isinstance(loc.parent, list):
                return path[:-1]
        if isinstance(source_node, EfxAttribute):
            raise ValueError("Select a destination entry or attribute list")
        return self._document_prefix() + (tokens(source_path)[-2],)

    def _paste(self):
        from .templates import import_template
        try:
            donor, source = import_template(QApplication.clipboard().text())
            path = self._destination(locate(donor, source).value, source)
            self._run({"op": "import", "source": source, "path": path}, donor=donor)
        except (ValueError, KeyError, TypeError) as error:
            QMessageBox.warning(self, self.tr("Paste failed"), str(error))

    def _import_file(self):
        from .format_registry import read_efx
        from .templates import import_template
        filename, _ = QFileDialog.getOpenFileName(self, self.tr("Import EFX or preset"), "", "EFX / Preset (*.efx.* *.json);;All files (*)")
        if not filename:
            return
        try:
            if filename.lower().endswith('.json'):
                donor, source = import_template(Path(filename).read_text(encoding='utf-8-sig'))
            else:
                donor = read_efx(Path(filename).read_bytes(), filename)
                if not isinstance(donor, EfxFile):
                    raise ValueError("Select a compatible RE Engine EFX")
                choices = {}
                for prefix, doc in documents(donor):
                    for category in ("entries", "actions"):
                        for i, entry in enumerate(getattr(doc, category)):
                            path = prefix + (category, i)
                            choices[f"{path_text(path)}: {entry.name}"] = path
                            for ai, attribute in enumerate(entry.attributes):
                                attr_path = path + ("attributes", ai)
                                choices[f"{path_text(attr_path)}: {attribute.name}"] = attr_path
                    for category in ("bones", "groups", "expression_parameters", "field_parameters"):
                        for i, item in enumerate(getattr(doc, category)):
                            path = prefix + (category, i)
                            choices[f"{path_text(path)}: {item.name}"] = path
                chosen, accepted = QInputDialog.getItem(self, self.tr("Import entry"), self.tr("Source"), list(choices), editable=False)
                if not accepted:
                    return
                source = choices[chosen]
            target = self._destination(locate(donor, source).value, source)
            self._run({"op": "import", "source": source, "path": target}, donor=donor)
        except (OSError, ValueError, KeyError, TypeError) as error:
            QMessageBox.warning(self, self.tr("Import failed"), str(error))

    def _save_preset(self):
        from .templates import export_template
        path = self._selected_path()
        if not path:
            return
        filename, _ = QFileDialog.getSaveFileName(self, self.tr("Save EFX preset"), "effect.efxpreset.json", "EFX preset (*.json)")
        if filename:
            try:
                Path(filename).write_text(export_template(self.editor.document, path), encoding='utf-8')
            except (OSError, ValueError) as error:
                QMessageBox.warning(self, self.tr("Save failed"), str(error))

    def _replace_record(self):
        path = self._selected_path()
        if not path:
            return
        record = locate(self.editor.document, path).value
        payload = data(record)
        text, accepted = QInputDialog.getMultiLineText(self, self.tr("Edit structure"), self.tr("JSON value"), json.dumps(payload, ensure_ascii=False, indent=2))
        if accepted:
            try:
                payload = json.loads(text)
            except ValueError as error:
                QMessageBox.warning(self, self.tr("Invalid value"), str(error))
                return
            self._run({"op": "replace", "path": path, "value": payload})

    def _show_references(self):
        from .graph import references
        path = path_text(self._selected_path() or ())
        rows = [r for r in references(self.editor.document) if not path or r['source'].startswith(path + '/') or r['target'] == path or (r['target'] or '').startswith(path + '/')]
        dialog = QDialog(self); dialog.setWindowTitle('References'); dialog.resize(780, 380)
        layout = QVBoxLayout(dialog); listing = QListWidget(); layout.addWidget(listing)
        for row in rows:
            listing.addItem(f"{row['source']}  →  {row['target']}")
        buttons = QDialogButtonBox(QDialogButtonBox.Open | QDialogButtonBox.Close); layout.addWidget(buttons)
        buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
        listing.itemDoubleClicked.connect(lambda _: dialog.accept())
        if dialog.exec() and listing.currentRow() >= 0:
            self._sync(selection=tokens(rows[listing.currentRow()]['target']))

    def _entry_path(self):
        path = self._selected_path() or ()
        for length in range(len(path), 0, -1):
            candidate = path[:length]
            if isinstance(locate(self.editor.document, candidate).value, EfxEntry):
                return candidate
        return None

    def _parameters(self):
        from .value_widgets import ParameterDialog
        path = self._entry_path()
        if path:
            loc = locate(self.editor.document, path)
            dialog = ParameterDialog(loc.value, path, loc.document.version, self)
            if dialog.exec():
                self._run({'op': 'transaction', 'path': path, 'operations': dialog.operations})

    def _channels(self, mode):
        from .channel_dialogs import ChannelDialog
        path = self._selected_path() or ()
        chosen = None
        for length in range(len(path), 0, -1):
            node = locate(self.editor.document, path[:length]).value
            if isinstance(node, EfxAttribute):
                chosen = path[:length]; break
        entry_path = self._entry_path()
        if not entry_path:
            return
        entry = locate(self.editor.document, entry_path).value
        keys = ('clipData',) if mode == 'curve' else ('expressions', 'materialExpressions', 'materialExpressionsList')
        choices = {attr.name: entry_path + ('attributes', str(ai)) for ai, attr in enumerate(entry.attributes) if any(key in attr.body.fields for key in keys)}
        if not choices:
            return
        if chosen not in choices.values():
            name, ok = QInputDialog.getItem(self, mode.title(), 'Attribute', list(choices), editable=False)
            if not ok:
                return
            chosen = choices[name]
        try:
            dialog = ChannelDialog(self.editor, chosen, mode, self)
            if dialog.exec() and dialog.operations:
                self._run({'op': 'transaction', 'path': chosen, 'operations': dialog.operations})
        except (ValueError, KeyError) as error:
            QMessageBox.warning(self, 'Channel editor', str(error))

    def _sequence(self):
        from .sequence_dialog import SequenceDialog
        path = self._entry_path()
        if path:
            from utils.resource_file_utils import resolve_handler_resource_data
            dialog = SequenceDialog(self.editor.document, path_text(path), self, resolver=lambda path: resolve_handler_resource_data(self.handler, path, self))
            if dialog.exec():
                QMessageBox.information(self, 'Sequence bundle saved', str(dialog.output_path))

    def _preview(self):
        from .preview import PreviewPanel, load_entry_frames
        path = self._entry_path()
        if not path:
            return
        entry = locate(self.editor.document, path).value
        try:
            frames, fps = load_entry_frames(self.handler, entry, self)
        except (ValueError, OSError, IndexError) as error:
            QMessageBox.warning(self, 'Preview resource', str(error)); return
        dialog = QDialog(self); dialog.setWindowTitle('EFX preview'); dialog.resize(900, 700)
        layout = QVBoxLayout(dialog); layout.addWidget(PreviewPanel(entry, frames, fps, dialog)); dialog.exec()

    def _linked_uvs(self):
        from .linked_uvs_dialog import LinkedUvsDialog
        from file_handlers.uvs.uvs_file import UvsFile
        from utils.resource_file_utils import resolve_handler_resource_data
        path = self._entry_path()
        if not path:
            return
        entry = locate(self.editor.document, path).value
        resource = next(a.body['UVSPath'] for a in entry.attributes if a.name == 'UVSequence')
        resolver = lambda p: resolve_handler_resource_data(self.handler, p, self)
        try:
            found = resolver(resource)
            if not found:
                raise ValueError(f'UVS resource is unavailable: {resource}')
            uvs = UvsFile(); uvs.read(found[1], version=7)
            dialog = LinkedUvsDialog(self.editor.document, uvs, resource, resolver, self)
            if dialog.exec():
                QMessageBox.information(self, 'Linked bundle saved', str(dialog.output_path))
        except (ValueError, OSError, KeyError) as error:
            QMessageBox.warning(self, 'Linked UVS', str(error))

    def _edit_value(self):
        from .value_widgets import ValueWidget
        from .metadata import parameter_value
        from .material import material_value
        path = self._selected_path()
        loc = locate(self.editor.document, path)
        kind, value, operation = None, loc.value, 'set'
        if isinstance(value, EfxRecord) and value.classname == 'ExpressionParameter':
            operation = 'parameter-value'; kind = {0: 'F32', 1: 'Color', 2: 'Vec3', 3: 'Vec2'}.get(value['type']); value = parameter_value(value)
        elif isinstance(value, EfxRecord) and value.classname.endswith('.MdfProperty'):
            operation = 'material-value'; kind = 'S32' if value['parameterType'] == 3 else 'Vec4' if value['parameterType'] in (1, 2) else None; value = material_value(value)
        elif loc.field and loc.field.kind != 'Array':
            kind = loc.field.kind if not loc.components else 'F32' if isinstance(value, float) else 'S32'
            if loc.field.name in structural_fields(loc.record):
                return
        if kind is None or kind not in (*PRIMITIVES, 'String'):
            return
        dialog = QDialog(self); dialog.setWindowTitle('Edit value'); layout = QVBoxLayout(dialog)
        widget = ValueWidget(kind, value, loc.field.definition if loc.field else {}, loc.document.version); layout.addWidget(widget)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel); layout.addWidget(buttons)
        buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
        if dialog.exec():
            try:
                self._run({'op': operation, 'path': path, 'value': widget.value()})
            except (ValueError, OverflowError, struct.error) as error:
                QMessageBox.warning(self, 'Invalid value', str(error))

    def _update_buttons(self, *_):
        self.buttons["Undo"].setEnabled(bool(self.editor._undo))
        self.buttons["Redo"].setEnabled(bool(self.editor._redo))
        path = self._selected_path()
        loc = locate(self.editor.document, path) if path else None
        entry_path = self._entry_path()
        entry = locate(self.editor.document, entry_path).value if entry_path else None
        self.buttons['Parameters'].setEnabled(entry is not None)
        self.buttons['Preview'].setEnabled(entry is not None)
        self.buttons['Sequence'].setEnabled(entry is not None and self.editor.document.version == 2818730 and any(a.name == 'UVSequence' for a in entry.attributes))
        self.buttons['Linked UVS'].setEnabled(self.buttons['Sequence'].isEnabled())
        self.buttons['Curves'].setEnabled(entry is not None and any('clipData' in a.body.fields for a in entry.attributes))
        self.buttons['Expressions'].setEnabled(entry is not None and any(any(k in a.body.fields for k in ('expressions', 'materialExpressions', 'materialExpressionsList')) for a in entry.attributes))
        element = loc is not None and isinstance(loc.parent, list)
        from .templates import supported_node
        self.buttons["Copy"].setEnabled(loc is not None and supported_node(loc.value))
        mutable = False
        if loc and (isinstance(loc.value, list) or element):
            collection = loc if isinstance(loc.value, list) else locate(self.editor.document, path[:-1])
            try:
                self.editor._mutable_collection(collection)
                mutable = True
            except ValueError:
                pass
        self.buttons["Add"].setEnabled(loc is not None and (isinstance(loc.value, EfxEntry) or isinstance(loc.value, list) and mutable))
        self.buttons["Duplicate"].setEnabled(element and mutable and not isinstance(loc.value, EfxAttribute))
        self.buttons["Delete"].setEnabled(element and mutable)
        movable = element and mutable and not isinstance(loc.value, EfxAttribute)
        self.buttons["Up"].setEnabled(movable and loc.key > 0)
        self.buttons["Down"].setEnabled(movable and loc.key + 1 < len(loc.parent))

    def _context_menu(self, position):
        row = self.tree.itemAt(position)
        if row is None:
            return
        self.tree.setCurrentItem(row)
        menu = QMenu(self)
        for label in ("Add", "Duplicate", "Delete", "Up", "Down"):
            if self.buttons[label].isEnabled():
                menu.addAction(self.tr(label), self.buttons[label].click)
        path = self._selected_path()
        if path and isinstance(locate(self.editor.document, path).value, EfxAttribute):
            menu.addAction(self.tr("Copy to…"), self._copy_to)
        if self.buttons["Copy"].isEnabled():
            menu.addAction(self.tr("Copy"), self._copy)
            menu.addAction(self.tr("Save preset…"), self._save_preset)
        if path and isinstance(locate(self.editor.document, path).value, EfxRecord):
            menu.addAction(self.tr("Edit structure…"), self._replace_record)
        if path:
            menu.addAction(self.tr('Edit value…'), self._edit_value)
        menu.addAction(self.tr("References…"), self._show_references)
        menu.exec(self.tree.viewport().mapToGlobal(position))
