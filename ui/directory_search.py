import os
import queue
import threading
import mmap
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import (
    Qt,
    QCoreApplication,
    QT_TRANSLATE_NOOP,
)
from PySide6.QtGui import (
    QKeyEvent,
    QKeySequence
)
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressDialog,
    QMessageBox,
    QFileDialog,
    QApplication,
    QLineEdit,
    QCheckBox,
    QComboBox,
    QDialogButtonBox,
    QPushButton,
    QFormLayout,
    QDoubleSpinBox,
)

from file_handlers.msg.msg_handler import MsgHandler
from app_config import GAMES
from utils.binary_search import create_binary_matcher, create_search_patterns

PAK_SEARCH_TITLE = QT_TRANSLATE_NOOP("DirectorySearch", "PAK Search")


def _prepare_msg_search_data(data):
    if MsgHandler.can_handle(data):
        return MsgHandler.decrypt_for_search(data)
    return data


def _search_type_label(search_type: str) -> str:
    return {
        "text": QCoreApplication.translate("DirectorySearch", "Text"),
        "guid": QCoreApplication.translate("DirectorySearch", "GUID"),
        "number": QCoreApplication.translate("DirectorySearch", "Number"),
        "hex": QCoreApplication.translate("DirectorySearch", "Hex"),
    }.get(search_type, search_type)


@dataclass(frozen=True)
class SearchRequest:
    directory: str
    source: str
    search_type: str
    value: object
    max_bytes: int | None
    game: str
    ignore_mod_paks: bool


class ProjectSearchDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr('Project Search'))
        self.setMinimumWidth(540)
        self.request = None
        root = QVBoxLayout(self)
        form = QFormLayout()
        root.addLayout(form)
        directory_row = QHBoxLayout()
        project = getattr(parent, 'proj_dock', None)
        self.directory = QLineEdit(str(getattr(project, 'project_dir', '') or ''))
        browse = QPushButton(self.tr('Browse…'))
        browse.clicked.connect(self._browse)
        directory_row.addWidget(self.directory, 1)
        directory_row.addWidget(browse)
        form.addRow(self.tr('Directory'), directory_row)
        self.source = QComboBox()
        self.source.addItem(self.tr('Directory files'), 'directory')
        self.source.addItem(self.tr('PAK files in directory'), 'pak')
        form.addRow(self.tr('Search in'), self.source)
        self.search_type = QComboBox()
        for kind in ('text', 'guid', 'number', 'hex'):
            self.search_type.addItem(_search_type_label(kind), kind)
        form.addRow(self.tr('Type'), self.search_type)
        self.value = QLineEdit()
        form.addRow(self.tr('Value'), self.value)
        self.integer_type = QComboBox()
        self.integer_type.addItems(['int32', 'uint32', 'int64', 'uint64'])
        self.integer_label = QLabel(self.tr('Integer type'))
        form.addRow(self.integer_label, self.integer_type)
        self.reverse = QCheckBox(self.tr('Reverse byte order'))
        form.addRow(self.reverse)
        self.max_size = QDoubleSpinBox()
        self.max_size.setRange(0, 10000)
        self.max_size.setSuffix(' MB')
        self.max_size.setSpecialValueText(self.tr('Unlimited'))
        self.max_size_label = QLabel(self.tr('Maximum file size'))
        form.addRow(self.max_size_label, self.max_size)
        self.game = QComboBox()
        self.game.addItems(list(GAMES))
        settings = getattr(parent, 'settings', {})
        initial = getattr(parent, 'current_game', None) or settings.get('game_version', '')
        if initial in GAMES:
            self.game.setCurrentText(initial)
        self.game_label = QLabel(self.tr('Game'))
        form.addRow(self.game_label, self.game)
        self.ignore_mod_paks = QCheckBox(self.tr('Ignore mod PAKs'))
        form.addRow(self.ignore_mod_paks)
        self.error = QLabel()
        self.error.setWordWrap(True)
        root.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText(self.tr('Search'))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.search_type.currentIndexChanged.connect(self._update_options)
        self.source.currentIndexChanged.connect(self._update_options)
        self._update_options()

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, self.tr('Search directory'), self.directory.text())
        if path:
            self.directory.setText(path)

    def _update_options(self):
        kind = self.search_type.currentData()
        pak = self.source.currentData() == 'pak'
        self.integer_type.setVisible(kind == 'number')
        self.integer_label.setVisible(kind == 'number')
        self.reverse.setVisible(kind == 'hex')
        self.max_size.setVisible(not pak)
        self.max_size_label.setVisible(not pak)
        self.game.setVisible(pak)
        self.game_label.setVisible(pak)
        self.ignore_mod_paks.setVisible(pak)
        self.error.clear()

    def search_request(self):
        directory = self.directory.text().strip()
        if not os.path.isdir(directory):
            raise ValueError(self.tr('Select an existing directory.'))
        kind = self.search_type.currentData()
        value = self.value.text()
        if not value:
            raise ValueError(self.tr('Enter a search value.'))
        if kind == 'number':
            value = (self.integer_type.currentText(), int(value))
        elif kind == 'hex':
            value = (value, self.reverse.isChecked())
        create_search_patterns(kind, value)
        max_bytes = int(self.max_size.value()*1000000) or None
        return SearchRequest(directory, self.source.currentData(), kind, value, max_bytes,
                             self.game.currentText(), self.ignore_mod_paks.isChecked())

    def accept(self):
        try:
            self.request = self.search_request()
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        super().accept()

def search_items_with_progress(
    parent,
    items,
    ptitle,
    rtext,
    search_fn: Callable,
    open_fn: Callable,
    result_actions=None,
):
    """Shared progress/results UI for any binary search source."""
    total = len(items)
    if total == 0:
        QMessageBox.information(
            parent,
            QCoreApplication.translate("DirectorySearch", "Search"),
            QCoreApplication.translate("DirectorySearch", "No files found."),
        )
        return

    progress = QProgressDialog(
        rtext,
        QCoreApplication.translate("DirectorySearch", "Cancel"),
        0,
        total,
        parent,
    )
    progress.setWindowTitle(ptitle)
    progress.setMinimumDuration(0)
    progress.setWindowModality(Qt.WindowModal)
    progress.show()

    results_dialog = QDialog(parent, Qt.Window)
    results_dialog.setWindowTitle(
        QCoreApplication.translate("DirectorySearch", "Search Results")
    )
    results_dialog.resize(600, 400)
    layout = QVBoxLayout(results_dialog)
    layout.addWidget(QLabel(rtext))

    def handle_copy(self, event: QKeyEvent):
        if event.matches(QKeySequence.Copy):
            data = "\n".join([i.text() for i in self.selectedItems()])
            QApplication.clipboard().setText(data)
        else:
            QListWidget.keyPressEvent(self, event)

    result_list = QListWidget()
    result_list.setSelectionMode(QListWidget.ExtendedSelection)
    result_list.keyPressEvent = handle_copy.__get__(result_list, QListWidget)
    
    def handle_double_click(item):
        entry_data = item.data(Qt.UserRole)
        try:
            file_path, data = open_fn(entry_data)
            parent.add_tab(file_path, data)
        except Exception:
            pass

    result_list.itemDoubleClicked.connect(handle_double_click)
    layout.addWidget(result_list)

    if result_actions:
        actions_row = QHBoxLayout()
        actions_row.addStretch(1)
        for label, action_fn in result_actions:
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, fn=action_fn: fn(result_list))
            actions_row.addWidget(btn)
        layout.addLayout(actions_row)

    result_queue = queue.Queue()
    cancel_event = threading.Event()

    def search_worker():
        for idx, item in enumerate(items, start=1):
            if cancel_event.is_set():
                return
            result_queue.put(("progress", idx))
            try:
                label, entry_data = item
                if search_fn(entry_data):
                    result_queue.put(("result", label, entry_data))
            except Exception as e:
                print(f"Error processing {item}: {e}")

    search_thread = threading.Thread(target=search_worker, daemon=True)
    search_thread.start()

    while search_thread.is_alive() or not result_queue.empty():
        QApplication.processEvents()
        try:
            msg = result_queue.get_nowait()
            if msg[0] == "progress":
                progress.setValue(msg[1])
            elif msg[0] == "result":
                _, label, entry_data = msg
                result_item = QListWidgetItem(label)
                result_item.setData(Qt.UserRole, entry_data)
                result_list.addItem(result_item)
        except queue.Empty:
            continue

        if progress.wasCanceled():
            cancel_event.set()
            break

    progress.close()
    if not cancel_event.is_set():
        results_dialog.show()


def search_directory_common(parent, dpath, matcher, ptitle, rtext, max_bytes):
    """Search plain files in a directory."""
    flist = [os.path.join(r, f) for r, _, fs in os.walk(dpath) for f in fs]

    def process_file(fp):
        try:
            if max_bytes is not None and os.path.getsize(fp) > max_bytes:
                return False
            with open(fp, "rb") as f:
                data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
                try:
                    return matcher(_prepare_msg_search_data(data))
                finally:
                    data.close()
        except Exception:
            pass
        return False

    def search_fn(fp):
        return process_file(fp)

    def open_fn(fp):
        with open(fp, "rb") as f:
            return fp, f.read()

    search_items_with_progress(
        parent,
        [(fp, fp) for fp in flist],
        ptitle,
        rtext,
        search_fn,
        open_fn,
    )


def search_pak_common(
    parent,
    directory,
    matcher,
    ptitle,
    rtext,
    ignore_mod_paks=False,
    game=None,
):
    """Search files contained in all detected PAKs in a directory."""
    from file_handlers.pak import scan_pak_files
    from file_handlers.pak.reader import CachedPakReader

    paks = scan_pak_files(directory, ignore_mod_paks=ignore_mod_paks)
    if not paks:
        QMessageBox.information(
            parent,
            QCoreApplication.translate("DirectorySearch", PAK_SEARCH_TITLE),
            QCoreApplication.translate("DirectorySearch", "No .pak files found."),
        )
        return

    reader = CachedPakReader.from_paks(paks, game=game)
    reader.cache_entries(assign_paths=False)
    entry_paths = sorted(reader.cached_paths(include_unknown=True))

    def entry_data_for(path):
        if path.startswith("__Unknown/"):
            # Keep UserRole payload as strings to avoid QVariant int64 overflow on large hashes.
            return {"kind": "hash", "value": path.split("/", 1)[1]}
        return {"kind": "path", "value": path}

    def resolve_entry_ref(entry_data):
        kind = entry_data.get("kind")
        value = entry_data.get("value")
        if kind == "hash":
            return int(value, 16)
        return value

    def search_fn(entry_data):
        entry_ref = resolve_entry_ref(entry_data)
        buf = reader.get_file(entry_ref)
        if not buf:
            return False
        data = buf.getvalue()
        return matcher(_prepare_msg_search_data(data))

    def resolved_label(entry_data):
        if entry_data['kind'] == 'path':
            return entry_data['value']
        h = int(entry_data['value'], 16)
        path = reader.cached_path_for_hash(h)
        if path:
            entry_data['kind'] = 'path'
            entry_data['value'] = path
            return entry_data['value']
        return f"__Unknown/{entry_data['value']}"

    def open_fn(entry_data):
        entry_ref = resolve_entry_ref(entry_data)
        buf = reader.get_file(entry_ref)
        if not buf:
            raise RuntimeError("Could not open PAK entry")
        display = f"pak://{resolved_label(entry_data)}"
        return display, buf.getvalue()

    def load_list_action(result_list):
        list_path, _ = QFileDialog.getOpenFileName(
            parent,
            QCoreApplication.translate("DirectorySearch", "Load Path List"),
            filter="List/Text files (*.list *.txt);;All files (*)",
        )
        if not list_path:
            return
        try:
            from ui.project_manager.pak_file_lists import read_pak_list_file

            paths = read_pak_list_file(list_path)
            updated = reader.assign_paths(paths)
            for i in range(result_list.count()):
                item = result_list.item(i)
                entry_data = item.data(Qt.UserRole)
                item.setText(resolved_label(entry_data))
                item.setData(Qt.UserRole, entry_data)
            QMessageBox.information(
                parent,
                QCoreApplication.translate("DirectorySearch", PAK_SEARCH_TITLE),
                QCoreApplication.translate(
                    "DirectorySearch", "Loaded list and resolved {count} cached entries."
                ).format(count=updated),
            )
        except Exception as e:
            QMessageBox.critical(
                parent,
                QCoreApplication.translate("DirectorySearch", PAK_SEARCH_TITLE),
                str(e),
            )

    items = [(path, entry_data_for(path)) for path in entry_paths]
    search_items_with_progress(
        parent,
        items,
        ptitle,
        rtext,
        search_fn,
        open_fn,
        result_actions=[(
            QCoreApplication.translate("DirectorySearch", "Load List"),
            load_list_action,
        )],
    )


def search_project(parent):
    dialog = ProjectSearchDialog(parent)
    if dialog.exec() != QDialog.Accepted:
        dialog.deleteLater()
        return
    request = dialog.request
    dialog.deleteLater()
    run_search(parent, request)


def run_search(parent, request):
    search_type, value = request.search_type, request.value
    try:
        patterns = create_search_patterns(search_type, value)
        
        if search_type == 'hex':
            hex_text, reverse_bytes = value
            byte_order_text = (
                QCoreApplication.translate("DirectorySearch", "reversed byte order")
                if reverse_bytes
                else QCoreApplication.translate("DirectorySearch", "normal byte order")
            )
            rtext = QCoreApplication.translate(
                "DirectorySearch", "Files containing hex {hex} ({byte_order}):"
            ).format(hex=hex_text, byte_order=byte_order_text)
            
            for i, pattern in enumerate(patterns):
                print(f"Search pattern {i+1}: {pattern.hex().upper()}")
        elif search_type == 'number' and isinstance(value, tuple):
            int_type, actual_value = value
            rtext = QCoreApplication.translate(
                "DirectorySearch", "Files containing {type} value {value}:"
            ).format(type=int_type, value=actual_value)
            
            for pattern in patterns:
                print(f"Search pattern: {pattern.hex().upper()}")
        else:
            rtext = QCoreApplication.translate(
                "DirectorySearch", "Files containing {search_type} {value}:"
            ).format(search_type=_search_type_label(search_type), value=value)

        matcher = create_binary_matcher(patterns, case_insensitive=(search_type == "text"))
        if request.source == "pak":
            progress_title = QCoreApplication.translate(
                "DirectorySearch", "PAK {search_type} Search Progress"
            ).format(search_type=_search_type_label(search_type))
            search_pak_common(
                parent,
                request.directory,
                matcher,
                progress_title,
                rtext,
                ignore_mod_paks=request.ignore_mod_paks,
                game=request.game,
            )
        else:
            progress_title = QCoreApplication.translate(
                "DirectorySearch", "{search_type} Search Progress"
            ).format(search_type=_search_type_label(search_type))
            search_directory_common(
                parent, request.directory, matcher, progress_title, rtext, request.max_bytes
            )
    except Exception as e:
        QMessageBox.critical(
            parent,
            QCoreApplication.translate("DirectorySearch", "Error"),
            str(e),
        )
