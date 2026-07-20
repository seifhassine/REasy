import os
import queue
import threading
import mmap
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
    QInputDialog,
    QFileDialog,
    QApplication,
    QLineEdit,
    QCheckBox,
    QComboBox,
    QDialogButtonBox,
    QPushButton,
)

from utils.binary_search import create_binary_matcher, create_search_patterns

PAK_SEARCH_TITLE = QT_TRANSLATE_NOOP("DirectorySearch", "PAK Search")


def _search_type_label(search_type: str) -> str:
    return {
        "text": QCoreApplication.translate("DirectorySearch", "Text"),
        "guid": QCoreApplication.translate("DirectorySearch", "GUID"),
        "number": QCoreApplication.translate("DirectorySearch", "Number"),
        "hex": QCoreApplication.translate("DirectorySearch", "Hex"),
    }.get(search_type, search_type)

def ask_max_size_bytes(parent):
    """Get maximum file size from user"""
    val, ok = QInputDialog.getDouble(
        parent,
        QCoreApplication.translate("DirectorySearch", "Max File Size"),
        QCoreApplication.translate(
            "DirectorySearch", "Enter maximum file size in MB (0 for no limit):"
        ),
        0.0,
        0.0,
        10000.0,
        2,
    )
    if not ok:
        return False
    return val * 1024 * 1024 if val > 0 else None

def create_hex_search_dialog(parent):
    """Custom dialog for hex search with byte order option"""
    dialog = QDialog(parent)
    dialog.setWindowTitle(QCoreApplication.translate("DirectorySearch", "Hex Search"))
    layout = QVBoxLayout(dialog)

    label = QLabel(QCoreApplication.translate(
        "DirectorySearch",
        "Enter hexadecimal bytes (e.g., FF A9 00 3D or FFA9003D):",
    ))
    layout.addWidget(label)
    
    hex_input = QLineEdit()
    layout.addWidget(hex_input)

    byte_order_check = QCheckBox(
        QCoreApplication.translate("DirectorySearch", "Reverse Byte Order")
    )
    layout.addWidget(byte_order_check)
    
    button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    button_box.accepted.connect(dialog.accept)
    button_box.rejected.connect(dialog.reject)
    layout.addWidget(button_box)
    
    result = dialog.exec()
    
    if result == QDialog.Accepted:
        hex_text = hex_input.text()
        reverse_bytes = byte_order_check.isChecked()
        return (hex_text, reverse_bytes), True
    
    return None, False


def create_integer_search_dialog(parent):
    dialog = QDialog(parent)
    dialog.setWindowTitle(QCoreApplication.translate("DirectorySearch", "Integer Search"))
    layout = QVBoxLayout(dialog)

    integer_types = (
        ("int32", -(2**31), 2**31 - 1),
        ("uint32", 0, 2**32 - 1),
        ("int64", -(2**63), 2**63 - 1),
        ("uint64", 0, 2**64 - 1),
    )
    layout.addWidget(QLabel(
        QCoreApplication.translate("DirectorySearch", "Select integer type:")
    ))
    type_combo = QComboBox()
    type_combo.addItems(
        (
            QCoreApplication.translate("DirectorySearch", "int32 (signed 32-bit)"),
            QCoreApplication.translate("DirectorySearch", "uint32 (unsigned 32-bit)"),
            QCoreApplication.translate("DirectorySearch", "int64 (signed 64-bit)"),
            QCoreApplication.translate("DirectorySearch", "uint64 (unsigned 64-bit)"),
        )
    )
    layout.addWidget(type_combo)

    value_label = QLabel()
    value_input = QLineEdit()
    layout.addWidget(value_label)
    layout.addWidget(value_input)

    def update_limits():
        _, minimum, maximum = integer_types[type_combo.currentIndex()]
        value_label.setText(QCoreApplication.translate(
            "DirectorySearch", "Enter value ({minimum} to {maximum}):"
        ).format(minimum=minimum, maximum=maximum))

    type_combo.currentIndexChanged.connect(update_limits)
    update_limits()

    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    if dialog.exec() != QDialog.Accepted:
        return None, False

    integer_type, minimum, maximum = integer_types[type_combo.currentIndex()]
    try:
        value = int(value_input.text())
        if not minimum <= value <= maximum:
            raise ValueError(QCoreApplication.translate(
                "DirectorySearch", "Value out of range for {type}"
            ).format(type=integer_type))
    except ValueError as exc:
        QMessageBox.critical(
            parent,
            QCoreApplication.translate("DirectorySearch", "Invalid Input"),
            str(exc),
        )
        return None, False
    return (integer_type, value), True


def create_search_dialog(parent, search_type):
    if search_type == "number":
        return create_integer_search_dialog(parent)
    prompts = {
        "text": (
            QCoreApplication.translate("DirectorySearch", "Text Search"),
            QCoreApplication.translate("DirectorySearch", "Enter text to search (UTF-16LE):"),
        ),
        "guid": (
            QCoreApplication.translate("DirectorySearch", "GUID Search"),
            QCoreApplication.translate("DirectorySearch", "Enter GUID (standard format):"),
        ),
    }
    title, prompt = prompts[search_type]
    value, accepted = QInputDialog.getText(parent, title, prompt)
    return (value, accepted) if accepted else (None, False)


def choose_search_source(parent, search_type):
    options = [
        QCoreApplication.translate("DirectorySearch", "Directory Files"),
        QCoreApplication.translate("DirectorySearch", "PAK Files in Directory"),
    ]
    selected, ok = QInputDialog.getItem(
        parent,
        QCoreApplication.translate(
            "DirectorySearch", "{search_type} Search Scope"
        ).format(search_type=_search_type_label(search_type)),
        QCoreApplication.translate("DirectorySearch", "Search in:"),
        options,
        0,
        False,
    )
    if not ok:
        return None
    return "pak" if selected == options[1] else "directory"


def ask_ignore_mod_paks(parent):
    choices = [
        QCoreApplication.translate("DirectorySearch", "Include mod PAKs"),
        QCoreApplication.translate("DirectorySearch", "Ignore mod PAKs"),
    ]
    selected, ok = QInputDialog.getItem(
        parent,
        QCoreApplication.translate("DirectorySearch", "PAK Search Options"),
        QCoreApplication.translate("DirectorySearch", "PAK scan mode:"),
        choices,
        0,
        False,
    )
    if not ok:
        return None
    return selected == choices[1]


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
                found = matcher(data)
                data.close()
                return found
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


def search_pak_common(parent, directory, matcher, ptitle, rtext, ignore_mod_paks=False):
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

    reader = CachedPakReader()
    reader.pak_file_priority = list(paks)
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
        return matcher(data)

    def resolved_label(entry_data):
        if entry_data['kind'] == 'path':
            return entry_data['value']
        h = int(entry_data['value'], 16)
        hit = reader._cache.get(h) if reader._cache else None
        if hit and hit[1].path:
            entry_data['kind'] = 'path'
            entry_data['value'] = hit[1].path
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
            with open(list_path, 'r', encoding='utf-8', errors='ignore') as f:
                paths = [ln.strip().replace('\\', '/') for ln in f if ln.strip()]
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


def search_directory_for_type(parent, search_type, source_mode=None):
    """Unified directory search method"""
    source = source_mode or choose_search_source(parent, search_type)
    if not source:
        return

    directory = QFileDialog.getExistingDirectory(
        parent,
        QCoreApplication.translate(
            "DirectorySearch", "Select Directory for {search_type} Search"
        ).format(search_type=_search_type_label(search_type)),
    )
    if not directory:
        return

    if search_type == 'hex':
        value, ok = create_hex_search_dialog(parent)
    else:
        value, ok = create_search_dialog(parent, search_type)
        
    if not ok or value is None:
        return

    max_bytes = None
    if source == "directory":
        max_bytes = ask_max_size_bytes(parent)
        if max_bytes is False:  # Only return if user clicked Cancel
            return

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
        if source == "pak":
            progress_title = QCoreApplication.translate(
                "DirectorySearch", "PAK {search_type} Search Progress"
            ).format(search_type=_search_type_label(search_type))
            ignore_mod_paks = ask_ignore_mod_paks(parent)
            if ignore_mod_paks is None:
                return
            search_pak_common(
                parent,
                directory,
                matcher,
                progress_title,
                rtext,
                ignore_mod_paks=ignore_mod_paks,
            )
        else:
            progress_title = QCoreApplication.translate(
                "DirectorySearch", "{search_type} Search Progress"
            ).format(search_type=_search_type_label(search_type))
            search_directory_common(
                parent, directory, matcher, progress_title, rtext, max_bytes
            )
    except Exception as e:
        QMessageBox.critical(
            parent,
            QCoreApplication.translate("DirectorySearch", "Error"),
            str(e),
        )
