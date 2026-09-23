import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from file_handlers.motion.motlist_file import MotListFile
from file_handlers.motion.motlist_handler import MotListHandler
from file_handlers.motion.preview.mhr_assets import MhrPreviewAssetCache, MhrPreviewAssets
from utils.resource_file_utils import ResourceResolutionContext


APP = QApplication.instance() or QApplication([])


class PreviewCacheTests(unittest.TestCase):
    def test_cache_reuses_resources_and_invalidates_changed_loose_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'natives/stm/asset.bin'
            path.parent.mkdir(parents=True)
            path.write_bytes(b'original')
            context = ResourceResolutionContext(project_dir=folder, game='MHRise')
            handler = SimpleNamespace(resource_context=context, app=SimpleNamespace(settings={}))
            cache = MhrPreviewAssetCache()
            with patch('file_handlers.motion.preview.mhr_assets.preview_context', return_value=context) as prepare:
                first = cache.get(handler)
                self.assertEqual(first.resource('asset.bin')[1], b'original')
                self.assertIs(cache.get(handler), first)
                self.assertEqual(prepare.call_count, 1)
                path.write_bytes(b'changed resource')
                second = cache.get(handler)
                self.assertIsNot(second, first)
                self.assertEqual(second.resource('asset.bin')[1], b'changed resource')
                self.assertEqual(prepare.call_count, 2)

    def test_projects_and_explicit_resource_directories_have_separate_caches(self):
        cache = MhrPreviewAssetCache()
        app = SimpleNamespace(settings={})
        left = SimpleNamespace(resource_context=ResourceResolutionContext(project_dir='left'), app=app)
        right = SimpleNamespace(resource_context=ResourceResolutionContext(project_dir='right'), app=app)
        with patch('file_handlers.motion.preview.mhr_assets.preview_context', side_effect=lambda h, _: h.resource_context):
            self.assertIs(cache.get(left), cache.get(left))
            self.assertIsNot(cache.get(left), cache.get(right))
            self.assertIsNot(cache.get(left, 'one'), cache.get(left, 'two'))

    def test_new_project_override_invalidates_a_cached_pak_resource(self):
        with tempfile.TemporaryDirectory() as folder:
            context = ResourceResolutionContext(project_dir=folder, game='MHRise')
            assets = MhrPreviewAssets(context)
            with patch.object(ResourceResolutionContext, 'resolve', return_value=('natives/stm/asset.bin', b'pak')):
                assets.resource('asset.bin')
            self.assertTrue(assets.is_current())
            path = Path(folder) / 'natives/stm/asset.bin'
            path.parent.mkdir(parents=True)
            path.write_bytes(b'override')
            self.assertFalse(assets.is_current())


class PreviewReloadTests(unittest.TestCase):
    def test_reload_retains_motion_frame_filter_camera_and_editor(self):
        from file_handlers.motion.preview.mhr_editor import MhrMotListEditor
        path = Path(__file__).parent / 'TESTFILE/natives/STM/player/mot/plw_GunLance_100.motlist.528'
        if not path.is_file():
            self.skipTest('MHR motion corpus unavailable')
        handler = MotListHandler()
        handler.filepath = str(path)
        handler.read(path.read_bytes())
        with patch.object(MhrMotListEditor, '_load_default_target') as load_assets:
            editor = MhrMotListEditor(handler)
            try:
                APP.processEvents()
                index = next(i for i, e in enumerate(editor.preview._motions)
                             if i > 5 and e.resolve_motion().end_frame > 20)
                browser = editor.preview.motion_browser
                browser.animation_list.setCurrentRow(index)
                selected = editor.preview.current_entry.motion_id
                browser.filter_edit.setText(str(selected))
                editor.preview.playback._on_frame_changed(10)
                document = MotListFile()
                document.read(path.read_bytes())
                motion = next(s.payload.value for s in document.model.slots if s.motion_id == selected)
                motion.end_frame += 5
                data = document.write()
                document.read(data)
                assets_calls = load_assets.call_count
                with patch.object(editor.preview.viewport, 'set_scene', wraps=editor.preview.viewport.set_scene) as scene:
                    editor.reload_document(document, data)
                    self.assertTrue(scene.called)
                    self.assertTrue(all(c.kwargs.get('reset_camera') is False for c in scene.call_args_list))
                self.assertEqual(editor.preview.current_entry.motion_id, selected)
                self.assertEqual(editor.preview.controller.current_frame, 10)
                self.assertEqual(browser.filter_edit.text(), str(selected))
                self.assertIs(handler.model, document.model)
                self.assertEqual(load_assets.call_count, assets_calls)
                self.assertFalse(handler.modified)
            finally:
                editor.cleanup()
                editor.deleteLater()
                APP.processEvents()


if __name__ == '__main__':
    unittest.main()
