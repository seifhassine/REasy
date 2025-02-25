
import os
import queue
import threading
import concurrent.futures
import mmap

from PySide6.QtCore import Qt
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

    results_dialog = QDialog(parent)
    results_dialog.setWindowTitle("Search Results")
    results_dialog.resize(600, 400)
    layout = QVBoxLayout(results_dialog)

    result_label = QLabel(rtext)
    layout.addWidget(result_label)

    result_list = QListWidget()
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
        results_dialog.exec()

def search_directory_for_type(parent, search_type, create_search_dialog_fn, create_search_patterns_fn):
    """Unified directory search method"""
    directory = QFileDialog.getExistingDirectory(parent, f"Select Directory for {search_type.title()} Search")
    if not directory:
        return

    value, ok = create_search_dialog_fn(parent, search_type)
    if not ok or value is None:
        return

    max_bytes = ask_max_size_bytes(parent)
    if max_bytes is False:  # Only return if user clicked Cancel
        return

    try:
        patterns = create_search_patterns_fn(search_type, value)
        rtext = f"Files containing {search_type} {value}:"
        search_directory_common(parent, directory, patterns, f"{search_type.title()} Search Progress", rtext, max_bytes)
    except Exception as e:
        QMessageBox.critical(parent, "Error", str(e))
