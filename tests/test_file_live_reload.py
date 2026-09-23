import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import tempfile
import subprocess
import sys
import textwrap
import threading
import time
from unittest import TestCase, main
from unittest.mock import patch

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QTabWidget, QWidget

from services.file_change_monitor import FileChangeMonitor
from services.motion_document_loader import MotionDocumentLoader, _ParseJob


APP = QApplication.instance() or QApplication([])


def wait_for(predicate, timeout=10000):
    deadline = time.monotonic() + timeout / 1000
    while not predicate() and time.monotonic() < deadline:
        APP.processEvents()
        QTest.qWait(10)
    if not predicate():
        raise AssertionError('Timed out waiting for Qt work')


class FileMonitorTests(TestCase):
    def test_atomic_replacements_recreation_and_own_save(self):
        with tempfile.TemporaryDirectory() as folder:
            path, temporary = Path(folder) / 'source.bin', Path(folder) / 'new.bin'
            path.write_bytes(b'one')
            monitor = FileChangeMonitor(interval=15)
            changes = []
            monitor.changed.connect(changes.append)
            monitor.accept(str(path), b'one')
            try:
                for content in (b'two', b'three'):
                    temporary.write_bytes(content)
                    os.replace(temporary, path)
                    wait_for(lambda: changes and changes[-1] == content)
                    self.assertIn(str(path), monitor._watcher.files())
                self.assertEqual(changes, [b'two', b'three'])
                path.unlink()
                QTest.qWait(80)
                path.write_bytes(b'recreated')
                wait_for(lambda: changes[-1] == b'recreated')
                path.write_bytes(b'own-save')
                monitor.accept(str(path), b'own-save')
                QTest.qWait(100)
                self.assertEqual(changes, [b'two', b'three', b'recreated'])
                self.assertFalse(monitor.differs_from_disk())
            finally:
                monitor.close()

    def test_coalesces_writes_and_detects_a_newer_version_during_parse(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'source.bin'
            path.write_bytes(b'original')
            monitor = FileChangeMonitor(interval=25)
            changes = []
            monitor.changed.connect(changes.append)
            monitor.accept(str(path), b'original')
            try:
                for data in (b'a', b'ab', b'complete'):
                    path.write_bytes(data)
                    QTest.qWait(5)
                wait_for(lambda: changes == [b'complete'])
                path.write_bytes(b'newer')
                monitor.accept(str(path), b'complete')
                wait_for(lambda: changes[-1] == b'newer')
                self.assertTrue(monitor.differs_from_disk())
            finally:
                monitor.close()


class DocumentLoaderTests(TestCase):
    def test_background_parse_delivers_only_latest_request_on_ui_thread(self):
        owner = QWidget()
        started, release = threading.Event(), threading.Event()
        calls, results, failures, ticks = [], [], [], []
        def parser(data, label):
            self.assertIsNot(QThread.currentThread(), APP.thread())
            calls.append(data)
            if data == b'first':
                started.set()
                release.wait(5)
            return data
        def loaded(doc, data, context):
            self.assertIs(QThread.currentThread(), APP.thread())
            results.append((doc, context))
        loader = MotionDocumentLoader(owner, loaded, lambda error, _: failures.append(error), parser=parser)
        loader.submit(b'first', 'one', 1)
        try:
            wait_for(started.is_set)
            QTimer.singleShot(0, lambda: ticks.append(True))
            wait_for(lambda: ticks)
            loader.submit(b'second', 'two', 2)
            loader.submit(b'latest', 'three', 3)
            release.set()
            wait_for(lambda: results)
            self.assertEqual(calls, [b'first', b'latest'])
            self.assertEqual(results, [(b'latest', 3)])
            self.assertFalse(failures)
        finally:
            release.set()
            loader.close()
            wait_for(lambda: not _ParseJob._running)
            owner.deleteLater()

    def test_close_does_not_wait_for_worker_or_deliver_results(self):
        owner = QWidget()
        started, release = threading.Event(), threading.Event()
        results = []
        def parser(data, label):
            started.set()
            release.wait(5)
            return data
        loader = MotionDocumentLoader(owner, lambda *args: results.append(args), lambda *args: results.append(args), parser=parser)
        loader.submit(b'data', '', None)
        wait_for(started.is_set)
        before = time.monotonic()
        loader.close()
        self.assertLess(time.monotonic() - before, .1)
        owner.deleteLater()
        APP.processEvents()
        release.set()
        wait_for(lambda: not _ParseJob._running)
        self.assertEqual(results, [])


class _Viewer(QWidget):
    modified_changed = Signal(bool)
    def __init__(self, handler):
        super().__init__()
        self.handler = handler
        self.reloads = 0
    def reload_document(self, document, data):
        self.handler.adopt_document(document, data)
        self.reloads += 1


class FileTabReloadTests(TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).parent / 'TESTFILE/natives/STM/player/mot/plw_GunLance_100.motlist.528'
        if not path.is_file():
            from unittest import SkipTest
            raise SkipTest('MHR motion corpus unavailable')
        cls.raw = path.read_bytes()

    def version(self, number):
        import struct
        data = bytearray(self.raw)
        struct.pack_into('<Q', data, 8, number)
        return bytes(data)

    def test_initial_load_is_async_and_atomic_reload_retains_viewer(self):
        from ui.file_tab import FileTab
        from file_handlers.motion.motlist_handler import MotListHandler
        with tempfile.TemporaryDirectory() as folder, patch.object(MotListHandler, 'create_viewer', lambda handler: _Viewer(handler)):
            path = Path(folder) / 'test.motlist.528'
            path.write_bytes(self.raw)
            notebook = QTabWidget()
            tab = FileTab(notebook, str(path), self.raw)
            notebook.addTab(tab.notebook_widget, 'test')
            self.assertTrue(tab.loading)
            self.assertFalse(tab.initial_load_complete)
            try:
                wait_for(lambda: not tab.loading)
                viewer, handler = tab.viewer, tab.handler
                temporary = path.with_name('candidate')
                temporary.write_bytes(self.version(2))
                os.replace(temporary, path)
                wait_for(lambda: viewer.reloads == 1)
                self.assertIs(tab.viewer, viewer)
                self.assertIs(tab.handler, handler)
                self.assertEqual(handler.raw_data, self.version(2))
                with patch('ui.file_tab.QMessageBox.critical') as error:
                    bad = b'\x10\x02\x00\x00mlst' + bytes(32)
                    path.write_bytes(bad)
                    wait_for(lambda: error.called)
                    self.assertIs(tab.viewer, viewer)
                    self.assertEqual(handler.raw_data, self.version(2))
                    self.assertTrue(tab.notebook_widget.isEnabled())
            finally:
                tab.cleanup()
                wait_for(lambda: not _ParseJob._running)
                notebook.deleteLater()

    def test_dirty_conflict_and_save_never_silently_overwrite_disk(self):
        from ui.file_tab import FileTab
        from file_handlers.motion.motlist_handler import MotListHandler
        with tempfile.TemporaryDirectory() as folder, patch.object(MotListHandler, 'create_viewer', lambda handler: _Viewer(handler)):
            path = Path(folder) / 'test.motlist.528'
            path.write_bytes(self.raw)
            notebook = QTabWidget()
            tab = FileTab(notebook, str(path), self.raw)
            notebook.addTab(tab.notebook_widget, 'test')
            try:
                wait_for(lambda: not tab.loading)
                tab.modified = tab.handler.modified = True
                with patch('ui.file_tab.QMessageBox.question', return_value=QMessageBox.No) as question:
                    path.write_bytes(self.version(7))
                    wait_for(lambda: question.called)
                    self.assertTrue(tab.modified)
                    self.assertEqual(tab.handler.raw_data, self.raw)
                    self.assertFalse(tab.handle_file_save(str(path)))
                    self.assertEqual(path.read_bytes(), self.version(7))
                with patch('ui.file_tab.QMessageBox.question', return_value=QMessageBox.Yes), patch.object(tab, 'on_save', return_value=False):
                    tab.reload_file()
                    self.assertFalse(tab.loading)
                    self.assertTrue(tab.modified)
            finally:
                tab.cleanup()
                notebook.deleteLater()

    def test_retired_viewer_keeps_qt_parent_until_deferred_destruction(self):
        from ui.file_tab import FileTab
        from PySide6.QtCore import QCoreApplication, QEvent
        from shiboken6 import isValid
        notebook = QTabWidget()
        tab = FileTab(notebook)
        viewer = QWidget(tab.notebook_widget)
        tab.notebook_widget.layout().addWidget(viewer)
        tab.viewer = viewer
        try:
            tab._cleanup_viewer()
            self.assertIs(viewer.parent(), tab.notebook_widget)
            self.assertEqual(tab.notebook_widget.layout().indexOf(viewer), -1)
            self.assertTrue(viewer.isHidden())
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            self.assertFalse(isValid(viewer))
        finally:
            tab.cleanup()
            notebook.deleteLater()

    def test_repeated_fsm_atomic_reload_survives_native_viewer_destruction(self):
        root = Path(__file__).resolve().parents[1]
        fixtures = list((root / 'tests/TESTFILE').rglob('GunLance.motfsm2.43'))
        if not fixtures:
            self.skipTest('GunLance FSM corpus unavailable')
        script = textwrap.dedent('''
            import os, sys, time, tempfile
            from pathlib import Path
            from unittest.mock import patch
            from PySide6.QtWidgets import QApplication
            from PySide6.QtTest import QTest
            from ui.main_window import REasyEditorApp
            from settings import normalize_settings
            app = QApplication([])
            config = normalize_settings({'save_workspace_on_close': False,
                'rcol_json_path': str(Path('resources/data/dumps/rszmhrise.json').resolve())})
            raw = Path(sys.argv[1]).read_bytes()
            with tempfile.TemporaryDirectory() as folder, patch('ui.main_window.load_settings', return_value=config), patch('ui.main_window.save_settings'):
                path = Path(folder) / 'GunLance.motfsm2.43'
                path.write_bytes(raw)
                window = REasyEditorApp()
                window.show()
                tab = window.add_tab(str(path), raw)
                for index in range(10):
                    data = raw + bytes(index + 1)
                    temporary = path.with_name('candidate')
                    temporary.write_bytes(data)
                    os.replace(temporary, path)
                    previous = tab.handler
                    deadline = time.monotonic() + 30
                    while tab.handler is previous and time.monotonic() < deadline:
                        app.processEvents()
                        QTest.qWait(10)
                    assert tab.handler is not previous, 'No automatic reload'
                    assert tab.handler._saved_source == data
                    app.processEvents()
                window.close()
                app.processEvents()
            print('FSM reloads completed', flush=True)
        ''')
        result = subprocess.run([sys.executable, '-X', 'faulthandler', '-c', script, str(fixtures[0])],
                                cwd=root, capture_output=True, text=True, timeout=180,
                                env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('FSM reloads completed', result.stdout)


if __name__ == '__main__':
    main()
