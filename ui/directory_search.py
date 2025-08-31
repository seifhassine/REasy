import os
import queue
import threading
import concurrent.futures
import mmap
import re

from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QKeyEvent,
    QKeySequence
)
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QLabel,
    QListWidget,
    QProgressDialog,
    QMessageBox,
    QInputDialog,
    QFileDialog,
    QApplication,
    QLineEdit,
    QCheckBox,
    QDialogButtonBox,
)

def ask_max_size_bytes(parent):
    """Get maximum file size from user"""
    val, ok = QInputDialog.getDouble(
        parent,
        "Max File Size",
        "Enter maximum file size in MB (0 for no limit):",
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
    dialog.setWindowTitle("Hex Search")
    layout = QVBoxLayout(dialog)
    
    label = QLabel("Enter hexadecimal bytes (e.g., FF A9 00 3D or FFA9003D):")
    layout.addWidget(label)
    
    hex_input = QLineEdit()
    layout.addWidget(hex_input)
    
    byte_order_check = QCheckBox("Reverse Byte Order")
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

def search_directory_common(parent, dpath, patterns, ptitle, rtext, max_bytes):
    """Common search implementation using QProgressDialog"""
    flist = [os.path.join(r, f) for r, _, fs in os.walk(dpath) for f in fs]
    total = len(flist)
    if total == 0:
        QMessageBox.information(parent, "Search", "No files found.")
        return

    progress = QProgressDialog(rtext, "Cancel", 0, total, parent)
    progress.setWindowTitle(ptitle)
    progress.setMinimumDuration(0)
    progress.setWindowModality(Qt.WindowModal)
    progress.show()

    results_dialog = QDialog(parent, Qt.Window)
    results_dialog.setWindowTitle("Search Results")
    results_dialog.resize(600, 400)
    layout = QVBoxLayout(results_dialog)

    result_label = QLabel(rtext)
    layout.addWidget(result_label)

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
        file_path = item.text()
        try:
            with open(file_path, 'rb') as f:
                data = f.read()
            parent.add_tab(file_path, data)
        except Exception as e:
            pass
    
    result_list.itemDoubleClicked.connect(handle_double_click)
    layout.addWidget(result_list)

    # Thread-safe queue for results
    result_queue = queue.Queue()
    cancel_event = threading.Event()

    def process_file(fp):
        if cancel_event.is_set():
            return None
        try:
            if max_bytes is not None and os.path.getsize(fp) > max_bytes:
                return None
            with open(fp, "rb") as f:
                data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
                found = any(data.find(p) != -1 for p in patterns)
                data.close()
                if found:
                    return fp
        except IOError:
            pass
        return None

    def search_worker():
        count = 0
        cpu_count = os.cpu_count() or 4
        max_workers = max(1, int(cpu_count * 0.6))

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(process_file, fp): fp for fp in flist}
            for future in concurrent.futures.as_completed(futures):
                if cancel_event.is_set():
                    return
                count += 1
                result_queue.put(("progress", count))
                try:
                    res = future.result()
                    if res is not None:
                        result_queue.put(("result", res))
                except Exception as e:
                    print(f"Error processing {futures[future]}: {e}")

    # Start search thread
    search_thread = threading.Thread(target=search_worker, daemon=True)
    search_thread.start()

    # Event processing loop
    while search_thread.is_alive() or not result_queue.empty():
        QApplication.processEvents()

        try:
            msg_type, data = result_queue.get_nowait()
            if msg_type == "progress":
                progress.setValue(data)
            elif msg_type == "result":
                result_list.addItem(data)
        except queue.Empty:
            continue

        if progress.wasCanceled():
            cancel_event.set()
            break

    progress.close()
    if not cancel_event.is_set():
        results_dialog.show()

def search_directory_for_type(parent, search_type, create_search_dialog_fn, create_search_patterns_fn):
    """Unified directory search method"""
    directory = QFileDialog.getExistingDirectory(parent, f"Select Directory for {search_type.title()} Search")
    if not directory:
        return

    if search_type == 'hex':
        value, ok = create_hex_search_dialog(parent)
    else:
        value, ok = create_search_dialog_fn(parent, search_type)
        
    if not ok or value is None:
        return

    max_bytes = ask_max_size_bytes(parent)
    if max_bytes is False:  # Only return if user clicked Cancel
        return

    try:
        patterns = create_search_patterns_fn(search_type, value)
        
        if search_type == 'hex':
            hex_text, reverse_bytes = value
            byte_order_text = "reversed byte order" if reverse_bytes else "normal byte order"
            rtext = f"Files containing hex {hex_text} ({byte_order_text}):"
            
            for i, pattern in enumerate(patterns):
                print(f"Search pattern {i+1}: {pattern.hex().upper()}")
        elif search_type == 'number' and isinstance(value, tuple):
            int_type, actual_value = value
            rtext = f"Files containing {int_type} value {actual_value}:"
            
            for pattern in patterns:
                print(f"Search pattern: {pattern.hex().upper()}")
        else:
            rtext = f"Files containing {search_type} {value}:"
            
        search_directory_common(parent, directory, patterns, f"{search_type.title()} Search Progress", rtext, max_bytes)
    except Exception as e:
        QMessageBox.critical(parent, "Error", str(e))
