import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
from types import SimpleNamespace

import numpy as np
from PySide6.QtWidgets import QApplication, QTreeWidgetItemIterator

from file_handlers.efx.efx_handler import EfxHandler
from file_handlers.efx.editing import EfxEditor, data, record_from_data
from file_handlers.efx.efx_file import EfxFile
from file_handlers.efx.efx_viewer import EfxViewer, PATH_ROLE
from file_handlers.efx.value_widgets import ValueWidget, ParameterDialog
from file_handlers.efx.channel_dialogs import ChannelDialog
from file_handlers.efx.preview import ParticlePreview, PreviewPanel
from file_handlers.efx.sequence_dialog import SequenceDialog
from tests.test_efx import golden
from tests.test_efx_graph import simple_values
from tests.test_efx_sequence import donor


class AuthoringUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_value_controls_enum_color_flags_and_vector(self):
        color = ValueWidget('Color', 0xff804020)
        self.assertEqual(color.control.text(), '#204080FF')
        self.assertEqual(color.value(), 0xff804020)
        boolean = ValueWidget('Bool', False); boolean.control.setChecked(True); self.assertTrue(boolean.value())
        enum = ValueWidget('S32', 0, {'Classname': 'ReeLib.Efx.Enums.Shape3DType'})
        enum.control.setCurrentIndex(1); self.assertEqual(enum.value(), 1)
        enum.control.setEditText('17'); self.assertEqual(enum.value(), 17)
        vector = ValueWidget('Vec3', (1., 2., 3.)); vector.control[1].setText('-4.5')
        self.assertEqual(vector.value(), (1., -4.5, 3.))

    def test_parameter_dialog_commits_as_one_undo_and_toolbar_selection(self):
        handler = EfxHandler(); handler.filepath = 'effect.efx.2818730'; handler.read(donor().write())
        viewer = EfxViewer(handler)
        iterator = QTreeWidgetItemIterator(viewer.tree)
        while iterator.value():
            if iterator.value().data(0, PATH_ROLE) == ('entries', '0'):
                viewer.tree.setCurrentItem(iterator.value()); break
            iterator += 1
        self.assertTrue(viewer.buttons['Parameters'].isEnabled())
        self.assertTrue(viewer.buttons['Sequence'].isEnabled())
        dialog = ParameterDialog(handler.efx.entries[0], ('entries','0'), handler.efx.version)
        for path, widget in dialog.controls:
            if path[-1] == 'LocalPosition':
                widget.control[0].setText('7')
        dialog._accept()
        before = viewer.editor.document.write()
        viewer._run({'op': 'transaction', 'path': 'entries/0', 'operations': dialog.operations})
        self.assertEqual(len(viewer.editor._undo), 1)
        self.assertEqual(viewer.editor.document.entries[0].attributes[1].body['LocalPosition'][0], 7)
        viewer._undo(); self.assertEqual(viewer.editor.document.write(), before)
        viewer.close(); dialog.close()

    def test_curve_dialog_and_expression_dialog_apply_drafts(self):
        editor = EfxEditor(EfxFile.from_bytes(golden(), version=2818730))
        path = editor.apply({'op':'insert','path':'entries/0/attributes','value':{'type':'Transform3DClip','fields':{
            'clipBits':[0],'unkn1':0,'clipData':{'loopType':2,'clipDuration':0,'clips':[],'frames':[],'interpolationData':[]}}}})
        dialog = ChannelDialog(editor, path, 'curve')
        dialog.table.item(0,1).setText('10'); dialog._accept()
        self.assertEqual(len(dialog.operations), 1)
        self.assertEqual(dialog.operations[0]['value']['keys'][0]['value'], 10)
        editor.apply({'op':'transaction','operations':dialog.operations}); dialog.close()
        path = editor.apply({'op':'insert','path':'entries/0/attributes','value':{'type':'Transform3DExpression','fields':simple_values('Transform3DExpression')}})
        dialog = ChannelDialog(editor, path, 'expression')
        dialog.formula.setPlainText('2 * (3 + 4)'); dialog._accept()
        editor.apply({'op':'transaction','operations':dialog.operations}); dialog.close()

    def test_preview_seed_time_seek_and_frame_order(self):
        entry = donor().entries[0]
        model = ParticlePreview(entry, seed=13)
        model.extent  # Model is valid without optional emitter attributes.
        a = model.sample(25.5)
        model.sample(50); model.sample(10)
        self.assertEqual(model.sample(25.5), a)
        self.assertFalse(model.coverage()['shader_fidelity'])
        panel = PreviewPanel(entry, [np.full((8,8,4), [255,80,0,128], np.uint8)])
        panel.resize(600,400); panel.show(); self.app.processEvents()
        panel.timeline.setValue(30); self.assertEqual(panel.canvas.frame, 30)
        self.assertFalse(panel.grab().isNull())
        panel.mode.setCurrentIndex(1); self.app.processEvents(); self.assertFalse(panel.grab().isNull()); panel.close()
        dialog = SequenceDialog(donor(), 'entries/0')
        dialog.frames = [np.full((4,4,4), [i,0,0,255], np.uint8) for i in range(3)]
        dialog.frame_list.addItems(['0','1','2']); dialog.frame_list.setCurrentRow(2); dialog.move(-1)
        self.assertEqual([int(f[0,0,0]) for f in dialog.frames], [0,2,1]); dialog.remove()
        self.assertEqual([int(f[0,0,0]) for f in dialog.frames], [0,1]); dialog.close()


if __name__ == '__main__':
    unittest.main()
