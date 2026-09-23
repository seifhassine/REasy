"""RCOL structural editing through the native viewer and shared RSZ APIs."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication, QMessageBox, QLineEdit
from file_handlers.rcol.rcol_handler import RcolHandler
from file_handlers.rcol.rcol_viewer import NavPayload
from file_handlers.rcol.shape_types import ShapeType
from file_handlers.rsz.rsz_data_types import ArrayData, ObjectData, UserDataData

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / 'tests/TESTFILE/natives/STM/player/hit'
REGISTRY = ROOT / 'resources/data/dumps/rszmhrise.json'


def read_rcol(path, data=None):
    handler = RcolHandler()
    handler.filepath = str(path)
    handler.init_type_registry(str(REGISTRY))
    handler.read(path.read_bytes() if data is None else data)
    handler.rcol.rsz.validate_type_registry_state()
    return handler


def request_values(rcol):
    """Resolve userdata graphs so comparisons do not depend on shifted indices."""
    rsz = rcol.rsz
    cache = {}
    def instance(index):
        if index == 0:
            return None
        if index not in cache:
            fields = rsz.parsed_elements[index]
            cache[index] = (rsz.instance_infos[index].type_id,
                            tuple((name, value(field)) for name, field in fields.items()))
        return cache[index]
    def value(field):
        if isinstance(field, (ObjectData, UserDataData)):
            return instance(field.value)
        if isinstance(field, ArrayData):
            return tuple(value(v) for v in field.values)
        if hasattr(field, 'value'):
            return field.value
        if hasattr(field, 'raw_bytes'):
            return field.raw_bytes
        return tuple(vars(field).items())

    result = {}
    for index, request in enumerate(rcol.request_sets):
        group = rcol.groups[request.info.group_index]
        shape_values = []
        for shape in group.shapes:
            slot = shape.info.user_data_index + request.info.shape_offset
            shape_values.append((shape.info.guid, instance(rsz.object_table[slot])))
        result[request.info.id] = {
            'name': request.info.name,
            'group': group.info.guid,
            'root': instance(rsz.object_table[index]),
            'shapes': shape_values,
        }
    return result


@unittest.skipUnless(CORPUS.is_dir(), 'MHRise RCOL corpus not installed')
class RcolEditingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @contextlib.contextmanager
    def viewer(self, path):
        with contextlib.redirect_stdout(io.StringIO()):
            handler = read_rcol(path)
            viewer = handler.create_viewer()
        self.assertIsNotNone(viewer)
        # Replace only user dialog selections, never editing or serialization.
        viewer._prompt_shape_userdata_type = lambda: 'snow.hit.userdata.DummyHitAttackShapeData'
        viewer._prompt_request_set_type = lambda: 'snow.hit.userdata.PlHitAttackRSData'
        try:
            with patch.object(QMessageBox, 'warning', side_effect=lambda *args: self.fail(str(args[-1]))):
                with contextlib.redirect_stdout(io.StringIO()):
                    yield handler, viewer
        finally:
            viewer.close()
            viewer.deleteLater()
            self.app.processEvents()

    def roundtrip(self, handler, path):
        with tempfile.TemporaryDirectory() as folder:
            saved = Path(folder) / path.name
            saved.write_bytes(handler.rebuild())
            reopened = read_rcol(path, saved.read_bytes())
            self.assertEqual(reopened.rebuild(), saved.read_bytes())
            return reopened.rcol

    def test_request_panel_edits_its_own_rsz_fields_and_roundtrips(self):
        path = CORPUS / 'LongSword.rcol.20'
        with self.viewer(path) as (handler, viewer):
            viewer.resize(1400, 900)
            viewer.show()
            before = request_values(handler.rcol)
            index = next(i for i, request in enumerate(handler.rcol.request_sets)
                         if request.instance != i and '_Power' in handler.rcol.rsz.parsed_elements.get(request.instance, {}))
            request = handler.rcol.request_sets[index]
            viewer._select_and_render(NavPayload(kind='request_set', request_index=index))
            self.app.processEvents()
            scoped = viewer._request_userdata_viewer
            self.assertIs(scoped.scn, handler.rcol.rsz)
            self.assertEqual(scoped.instance_roots[0], request.instance)
            self.assertIn(request.info.name, viewer.path_label.text())
            self.assertGreater(viewer._detail_scroll.height(), 60)
            self.assertLess(viewer._detail_scroll.height(), 200)
            field = handler.rcol.rsz.parsed_elements[request.instance]['_Power']
            editors = scoped.tree._refresh_widgets_by_data[id(field)]
            editor = next(e for e in editors if e.findChildren(QLineEdit))
            line = editor.findChildren(QLineEdit)[0]
            value = field.value + 7
            line.setText(str(value))
            line.editingFinished.emit()
            self.assertEqual(field.value, value)
            self.assertTrue(handler.modified)
            self.assertTrue(viewer.modified)
            reopened = self.roundtrip(handler, path)
            self.assertEqual(reopened.rsz.parsed_elements[reopened.request_sets[index].instance]['_Power'].value, value)
            after = request_values(reopened)
            for key, original in before.items():
                if key != request.info.id:
                    self.assertEqual(after[key], original)
            # A save clears the outer dirty flag; the next edit must set it again.
            viewer.modified = handler.modified = False
            line.setText(str(value + 1))
            line.editingFinished.emit()
            self.assertTrue(viewer.modified)
            self.assertTrue(handler.modified)
            self.assertEqual(field.value, value + 1)

    def test_request_selection_tracks_roots_and_full_rsz_changes(self):
        path = CORPUS / 'LongSword.rcol.20'
        with self.viewer(path) as (handler, viewer):
            viewer.resize(1400, 900)
            viewer.show()
            for index in (0, 1, 26, 2, 26):
                viewer._select_and_render(NavPayload(kind='request_set', request_index=index))
                self.app.processEvents()
                scoped = viewer._request_userdata_viewer
                request = handler.rcol.request_sets[index]
                self.assertEqual(scoped.instance_roots[0], request.instance)
                self.assertEqual(scoped.tree.model().rowCount(), len(scoped.instance_roots))
                self.assertIn(request.info.name, viewer.path_label.text())
            field = handler.rcol.rsz.parsed_elements[request.instance]['_Power']
            viewer._preview_tabs.setCurrentIndex(1)
            self.app.processEvents()
            full = viewer._embedded_headless_viewer
            self.assertIsNone(full.instance_roots)
            field.value += 3
            full.mark_modified(field)
            scoped = viewer._request_userdata_viewer
            editor = next(e for e in scoped.tree._refresh_widgets_by_data[id(field)] if e.findChildren(QLineEdit))
            self.assertEqual(editor.findChildren(QLineEdit)[0].text(), str(field.value))
            viewer._preview_tabs.setCurrentIndex(0)
            self.app.processEvents()
            self.assertIs(viewer._request_userdata_viewer.scn, handler.rcol.rsz)

    def test_add_shape_and_request_preserve_all_existing_userdata(self):
        for path in sorted(CORPUS.glob('*.rcol.20')):
            source = path.read_bytes()
            with self.subTest(file=path.name), self.viewer(path) as (handler, viewer):
                rcol = handler.rcol
                before = request_values(rcol)
                group_index = max(range(len(rcol.groups)), key=lambda i:
                    sum(rs.info.group_index == i for rs in rcol.request_sets))
                old_shapes = len(rcol.groups[group_index].shapes)
                old_requests = len(rcol.request_sets)
                old_object_count = len(rcol.rsz.object_table)
                affected = {rs.info.id for rs in rcol.request_sets if rs.info.group_index == group_index}
                viewer._add_shape(group_index, False)
                self.assertEqual(len(rcol.groups[group_index].shapes), old_shapes + 1)
                self.assertEqual(len(rcol.rsz.object_table), old_object_count + len(affected))
                payload = NavPayload(kind='shape', group_index=group_index, shape_index=old_shapes)
                viewer._set_shape_type(payload, int(ShapeType.Box))
                rcol.groups[group_index].shapes[-1].shape.extent = [1.25, 2.5, 3.75]
                reopened = self.roundtrip(handler, path)
                self.assertEqual(reopened.groups[group_index].shapes[-1].shape.extent, [1.25, 2.5, 3.75])
                after_shape = request_values(reopened)
                for key, original in before.items():
                    actual = after_shape[key].copy()
                    if key in affected:
                        self.assertEqual(len(actual['shapes']), old_shapes + 1)
                        actual['shapes'] = actual['shapes'][:-1]
                    self.assertEqual(actual, original, (path.name, key))
                viewer._prompt_request_set_group_index = lambda: group_index
                viewer._add_request_set()
                self.assertEqual(len(rcol.request_sets), old_requests + 1)
                self.assertEqual(rcol.request_sets[-1].info.id, max(before) + 1)
                reopened = self.roundtrip(handler, path)
                after_request = request_values(reopened)
                for key, original in after_shape.items():
                    self.assertEqual(after_request[key], original, (path.name, key))
                self.assertEqual(len(after_request[max(before) + 1]['shapes']), old_shapes + 1)
                viewer._remove_request_set(old_requests)
                viewer._remove_shape(payload)
                reopened = self.roundtrip(handler, path)
                self.assertEqual(request_values(reopened), before)
                self.assertEqual(len(reopened.rsz.object_table), old_object_count)
            self.assertEqual(path.read_bytes(), source)
            print(path.name, 'add/remove shape and request: preserved existing userdata', flush=True)

    def test_remove_primary_and_middle_requests_preserve_surviving_windows(self):
        path = CORPUS / 'LongSword.rcol.20'
        for request_index in (0, 1):
            with self.subTest(request=request_index), self.viewer(path) as (handler, viewer):
                before = request_values(handler.rcol)
                removed_id = handler.rcol.request_sets[request_index].info.id
                viewer._remove_request_set(request_index)
                del before[removed_id]
                self.assertEqual(request_values(self.roundtrip(handler, path)), before)

    def test_remove_first_and_middle_shapes_preserve_remaining_userdata(self):
        path = CORPUS / 'LongSword.rcol.20'
        for shape_index in (0, 1):
            with self.subTest(shape=shape_index), self.viewer(path) as (handler, viewer):
                before = request_values(handler.rcol)
                affected = {rs.info.id for rs in handler.rcol.request_sets if rs.info.group_index == 4}
                viewer._remove_shape(NavPayload(kind='shape', group_index=4, shape_index=shape_index))
                for key in affected:
                    del before[key]['shapes'][shape_index]
                self.assertEqual(request_values(self.roundtrip(handler, path)), before)

    def test_first_request_for_an_empty_group_and_append_at_table_end(self):
        path = CORPUS / 'LongSword.rcol.20'
        for group_index in (0, 7):
            with self.subTest(group=group_index), self.viewer(path) as (handler, viewer):
                before = request_values(handler.rcol)
                if group_index == 0:
                    viewer._add_shape(group_index, False)
                viewer._prompt_request_set_group_index = lambda: group_index
                viewer._add_request_set()
                viewer._add_request_set()
                reopened = self.roundtrip(handler, path)
                after = request_values(reopened)
                for key, original in before.items():
                    self.assertEqual(after[key], original)
                count = len(reopened.groups[group_index].shapes)
                self.assertEqual(len(after[72]['shapes']), count)
                self.assertEqual(len(after[73]['shapes']), count)
                viewer._remove_request_set(73)
                viewer._remove_request_set(72)
                if group_index == 0:
                    viewer._remove_shape(NavPayload(kind='shape', group_index=0, shape_index=0))
                self.assertEqual(request_values(self.roundtrip(handler, path)), before)


if __name__ == '__main__':
    unittest.main()
