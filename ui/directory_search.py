import os
import queue
import threading
import mmap
import re
from typing import Callable

from PySide6.QtCore import (
    Qt,
    QObject
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
    QDialogButtonBox,
    QPushButton,
)

def ask_max_size_bytes(parent):
    """Get maximum file size from user"""
    val, ok = QInputDialog.getDouble(
        parent,
        QObject.tr("Max File Size"),
        QObject.tr("Enter maximum file size in MB (0 for no limit):"),
        0.0,
        0.0,
        10000.0,
        2,
    )
    if not ok:
        return False
    return val * 1024 * 1024 if val > 0 else None

def validate_hex_string(hex_str):
    """Validate and normalize a hexadecimal string"""
    hex_str = re.sub(r'\s+', '', hex_str)
    
    if not re.match(r'^[0-9a-fA-F]+$', hex_str):
        raise ValueError("Invalid hexadecimal string. Use only 0-9, A-F characters.")
    
    if len(hex_str) % 2 != 0:
        raise ValueError("Hexadecimal string must contain an even number of characters.")
    
    return hex_str

def hex_string_to_bytes(hex_str, reverse_bytes=False):
    """Convert a validated hexadecimal string to bytes
    
    Args:
        hex_str: Validated hex string without spaces
        reverse_bytes: If True, reverse the byte order
    """
    byte_data = bytes.fromhex(hex_str)
    
    if reverse_bytes and len(byte_data) > 1:
        byte_data = byte_data[::-1]
        print(f"Original hex: {hex_str}")
        print(f"Reversed bytes: {byte_data.hex().upper()}")
    
    return byte_data

def create_hex_search_dialog(parent):
    """Custom dialog for hex search with byte order option"""
    dialog = QDialog(parent)
    dialog.setWindowTitle(QObject.tr("Hex Search"))
    layout = QVBoxLayout(dialog)

    label = QLabel(QObject.tr("Enter hexadecimal bytes (e.g., FF A9 00 3D or FFA9003D):"))
    layout.addWidget(label)
    
    hex_input = QLineEdit()
    layout.addWidget(hex_input)

    byte_order_check = QCheckBox(QObject.tr("Reverse Byte Order"))
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


def choose_search_source(parent, search_type):
    options = ["Directory Files", "PAK Files in Directory"]
    selected, ok = QInputDialog.getItem(
        parent,
        f"{search_type.title()} Search Scope",
        "Search in:",
        options,
        0,
        False,
    )
    if not ok:
        return None
    return "pak" if selected == options[1] else "directory"


def ask_ignore_mod_paks(parent):
    choices = ["Include mod PAKs", "Ignore mod PAKs"]
    selected, ok = QInputDialog.getItem(
        parent,
        "PAK Search Options",
        "PAK scan mode:",
        choices,
        0,
        False,
    )
    if not ok:
        return None
    return selected == "Ignore mod PAKs"


def create_binary_matcher(patterns, case_insensitive=False):
    if case_insensitive:
        folded_patterns = [p.lower() for p in patterns]
        
        def contains_case_insensitive(data: bytes, folded_pattern: bytes, chunk_size=4 * 1024 * 1024) -> bool:
            plen = len(folded_pattern)
            if plen == 0:
                return True

            overlap = max(0, plen - 1)
            pos = 0
            total = len(data)
            while pos < total:
                end = min(total, pos + chunk_size + overlap)
                chunk = data[pos:end]
                if chunk.lower().find(folded_pattern) != -1:
                    return True
                pos += chunk_size
            return False

        def matcher(data: bytes) -> bool:
            return any(contains_case_insensitive(data, p) for p in folded_patterns)

        return matcher

    def matcher(data: bytes) -> bool:
        return any(data.find(p) != -1 for p in patterns)

    return matcher


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
        QMessageBox.information(parent, "Search", "No files found.")
        return

    progress = QProgressDialog(rtext, "Cancel", 0, total, parent)
    progress.setWindowTitle(ptitle)
    progress.setMinimumDuration(0)
    progress.setWindowModality(Qt.WindowModal)
    progress.show()

    results_dialog = QDialog(parent, Qt.Window)
    results_dialog.setWindowTitle(QObject.tr("Search Results"))
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
        QMessageBox.information(parent, "PAK Search", "No .pak files found.")
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
        list_path, _ = QFileDialog.getOpenFileName(parent, "Load Path List", filter="List/Text files (*.list *.txt);;All files (*)")
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
            QMessageBox.information(parent, "PAK Search", f"Loaded list and resolved {updated} cached entries.")
        except Exception as e:
            QMessageBox.critical(parent, "PAK Search", str(e))

    items = [(path, entry_data_for(path)) for path in entry_paths]
    search_items_with_progress(
        parent,
        items,
        ptitle,
        rtext,
        search_fn,
        open_fn,
        result_actions=[("Load List", load_list_action)],
    )


def search_directory_for_type(parent, search_type, create_search_dialog_fn, create_search_patterns_fn, source_mode=None):
    """Unified directory search method"""
    source = source_mode or choose_search_source(parent, search_type)
    if not source:
        return

    directory = QFileDialog.getExistingDirectory(parent, QObject.tr("Select Directory for {search_type} Search").format(search_type=search_type.title()))
    if not directory:
        return

    if search_type == 'hex':
        value, ok = create_hex_search_dialog(parent)
    else:
        value, ok = create_search_dialog_fn(parent, search_type)
        
    if not ok or value is None:
        return

    max_bytes = None
    if source == "directory":
        max_bytes = ask_max_size_bytes(parent)
        if max_bytes is False:  # Only return if user clicked Cancel
            return

    try:
        patterns = create_search_patterns_fn(search_type, value)
        
        if search_type == 'hex':
            hex_text, reverse_bytes = value
            byte_order_text = "reversed byte order" if reverse_bytes else "normal byte order"
            rtext = QObject.tr("Files containing hex {} ({}):").format(hex_text, byte_order_text)
            
            for i, pattern in enumerate(patterns):
                print(f"Search pattern {i+1}: {pattern.hex().upper()}")
        elif search_type == 'number' and isinstance(value, tuple):
            int_type, actual_value = value
            rtext = QObject.tr("Files containing {} value {}:").format(int_type, actual_value)
            
            for pattern in patterns:
                print(f"Search pattern: {pattern.hex().upper()}")
        else:
            rtext = QObject.tr("Files containing {} {}:").format(search_type, value)

        matcher = create_binary_matcher(patterns, case_insensitive=(search_type == "text"))
        title_prefix = "PAK " if source == "pak" else ""
        if source == "pak":
            ignore_mod_paks = ask_ignore_mod_paks(parent)
            if ignore_mod_paks is None:
                return
            search_pak_common(
                parent,
                directory,
                matcher,
                f"{title_prefix}{search_type.title()} Search Progress",
                rtext,
                ignore_mod_paks=ignore_mod_paks,
            )
        else:
            search_directory_common(parent, directory, matcher, f"{title_prefix}{search_type.title()} Search Progress", rtext, max_bytes)
    except Exception as e:
        QMessageBox.critical(parent, "Error", str(e))
