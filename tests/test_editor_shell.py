"""Search workflow and editor shell behavior after menu consolidation."""
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication, QWidget, QListWidget
from settings import normalize_settings
from ui.directory_search import ProjectSearchDialog, run_search


class ProjectSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_all_search_types_find_real_file_bytes(self):
        identity = uuid.UUID('9f3ae802-65e1-4c7a-a045-293c0bb26e17')
        cases = [('text', 'Example', 'example'.encode('utf-16le')),
                 ('guid', str(identity), identity.bytes_le),
                 ('number', str(2**63+5), struct.pack('>Q', 2**63+5)),
                 ('hex', '12 AB 34', bytes.fromhex('34 AB 12'))]
        for kind, text, payload in cases:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder:
                hit = Path(folder)/'match.bin'
                hit.write_bytes(b'prefix'+payload+b'suffix')
                (Path(folder)/'miss.bin').write_bytes(b'not present')
                parent = QWidget()
                dialog = ProjectSearchDialog(parent)
                try:
                    dialog.directory.setText(folder)
                    dialog.search_type.setCurrentIndex(dialog.search_type.findData(kind))
                    dialog.value.setText(text)
                    dialog.integer_type.setCurrentText('uint64')
                    dialog.reverse.setChecked(True)
                    dialog.accept()
                    self.assertTrue(dialog.request, dialog.error.text())
                    run_search(parent, dialog.request)
                    results = parent.findChildren(QListWidget)
                    self.assertEqual(len(results), 1)
                    self.assertEqual([results[0].item(i).text() for i in range(results[0].count())], [str(hit)])
                finally:
                    for child in parent.findChildren(QWidget):
                        if child.isWindow():
                            child.close()
                    parent.close(); parent.deleteLater()

    def test_invalid_input_stays_in_dialog_and_pak_options_route_to_reader(self):
        with tempfile.TemporaryDirectory() as folder:
            dialog = ProjectSearchDialog()
            try:
                dialog.directory.setText(folder)
                dialog.search_type.setCurrentIndex(dialog.search_type.findData('number'))
                dialog.integer_type.setCurrentText('uint32')
                dialog.value.setText('-1')
                dialog.accept()
                self.assertIsNone(dialog.request)
                self.assertTrue(dialog.error.text())
                dialog.value.setText('100')
                dialog.source.setCurrentIndex(dialog.source.findData('pak'))
                dialog.game.setCurrentText('MHRise')
                dialog.ignore_mod_paks.setChecked(True)
                dialog.accept()
                with patch('ui.directory_search.search_pak_common') as scan:
                    run_search(None, dialog.request)
                args, kwargs = scan.call_args
                self.assertEqual(args[1], folder)
                self.assertTrue(args[2](struct.pack('<I', 100)))
                self.assertEqual(kwargs, {'game': 'MHRise', 'ignore_mod_paks': True})
            finally:
                dialog.close()


class EditorShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_hidden_output_records_logs_and_closing_restores_streams(self):
        from ui.main_window import REasyEditorApp
        config = normalize_settings({'save_workspace_on_close': False})
        before_stdout, before_stderr = sys.stdout, sys.stderr
        with patch('ui.main_window.load_settings', return_value=config), patch('ui.main_window.save_settings'):
            window = REasyEditorApp()
            try:
                window.show(); self.app.processEvents()
                self.assertFalse(window.output_dock.isVisible())
                print('hidden output remains available')
                sys.stderr.write('hidden error remains available\n')
                self.app.processEvents()
                self.assertIn('hidden error remains available', window.console_widget.toPlainText())
                window.toggle_debug_console(True)
                self.assertTrue(window.output_dock.isVisible())
                self.assertIn('hidden output remains available', window.console_widget.toPlainText())
                window.toggle_debug_console(False)
                self.assertFalse(window.output_dock.isVisible())
                self.assertFalse(window.scene_menu.menuAction().isVisible())
            finally:
                window.close(); window.deleteLater()
        self.assertIs(sys.stdout, before_stdout)
        self.assertIs(sys.stderr, before_stderr)

    def test_explicit_output_preference_survives_while_obsolete_shortcuts_are_removed(self):
        config = normalize_settings({'show_debug_console': True, 'keyboard_shortcuts': {
            'find_search_text': 'Alt+T', 'file_save': 'Alt+S'}})
        self.assertTrue(config['show_debug_console'])
        self.assertNotIn('find_search_text', config['keyboard_shortcuts'])
        self.assertEqual(config['keyboard_shortcuts']['file_save'], 'Alt+S')
        self.assertEqual(config['keyboard_shortcuts']['find_project_search'], 'Ctrl+Shift+F')


if __name__ == '__main__':
    unittest.main()
