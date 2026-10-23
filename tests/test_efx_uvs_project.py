import copy
import tempfile
import unittest
from pathlib import Path

from file_handlers.uvs.uvs_file import UvsFile, UvsTexture, UvsSequence, UvsPattern
from file_handlers.efx.sequence_project import SequenceProject
from file_handlers.efx.sequence_assets import build_sequence_bundle
from tests.test_efx_sequence import donor
import numpy as np


def project():
    doc = donor()
    uvs = UvsFile(); uvs.version = 7
    uvs.textures = [UvsTexture(path='a.tex'), UvsTexture(path='b.tex')]
    uvs.sequences = [UvsSequence(patterns=[UvsPattern(right=1, bottom=1, texture_index=i) for _ in range(3)]) for i in range(2)]
    return SequenceProject(doc, uvs, 'old.uvs')


class LinkedUvsTests(unittest.TestCase):
    def test_sequence_frame_and_texture_reorders_preserve_bindings(self):
        p = project(); before = p.snapshot()
        p.apply({'op':'sequence-move','index':0,'to':1})
        self.assertEqual(p.bindings()[0]['SequenceNo'], (1,2))
        p.apply({'op':'frame-move','sequence':1,'index':0,'to':2})
        self.assertEqual(p.bindings()[0]['PatternNo'], (2,3))
        p.apply({'op':'texture-move','index':0,'to':1})
        self.assertEqual(p.uvs.sequences[1].patterns[2].texture_index, 1)
        for _ in range(3):self.assertTrue(p.undo())
        self.assertEqual(p.snapshot(), before)
        for _ in range(3):self.assertTrue(p.redo())
        self.assertEqual(p.bindings()[0]['PatternNo'], (2,3))

    def test_delete_referenced_slots_and_non_contiguous_range_roll_back(self):
        p = project(); before = p.snapshot()
        for op in ({'op':'sequence-delete','index':0}, {'op':'frame-delete','sequence':0,'index':0}, {'op':'texture-delete','index':0}):
            with self.subTest(op=op), self.assertRaises(ValueError):p.apply(op)
            self.assertEqual(p.snapshot(), before)
        p.bindings()[0]['PatternNo'] = (0,2)
        before = p.snapshot()
        with self.assertRaisesRegex(ValueError,'non-contiguous'):
            p.apply({'op':'frame-move','sequence':0,'index':1,'to':2})
        self.assertEqual(p.snapshot(), before)

    def test_sequence_frame_crud_and_dependency_bundle(self):
        p = project()
        p.apply({'op':'sequence-copy','index':0,'to':2})
        p.apply({'op':'sequence-delete','index':2})
        p.apply({'op':'frame-insert','sequence':0,'index':0,'value':{'left':0,'top':0,'right':.5,'bottom':1,'texture_index':0}})
        self.assertEqual(p.bindings()[0]['PatternNo'], (1,2))
        p.apply({'op':'frame-set','sequence':0,'index':0,'value':{'left':.5,'top':0,'right':1,'bottom':1,'texture_index':1}})
        p.apply({'op':'frame-delete','sequence':0,'index':0})
        bundle = p.bundle('vfx/custom/linked')
        fake_textures = {'a.tex': ('a.tex.28', b'a'), 'b.tex': ('b.tex.28', b'b')}
        bundle.include_dependencies(fake_textures.get)
        self.assertEqual(bundle.files['natives/STM/a.tex.28'], b'a')
        self.assertEqual(bundle.manifest['unresolved_dependencies'], ['vfx/火焰.vsdf'])
        with tempfile.TemporaryDirectory() as folder:
            bundle.publish(Path(folder)/'result')
            self.assertTrue((Path(folder)/'result/natives/STM/old.uvs.7').is_file())

    def test_transitive_uvs_dependencies_are_copied_and_missing_assets_recorded(self):
        frames = [np.zeros((8,8,4), np.uint8)]
        bundle = build_sequence_bundle(donor(), 'entries/0', frames, 'vfx/custom/flipbook', compression='rgba8')
        old = UvsFile(); old.version = 7; old.textures = [UvsTexture(path='texture.tex')]
        bundle.manifest['external_dependencies'] += ['other.uvs']
        resources = {'other.uvs': ('other.uvs.7', old.write()), 'texture.tex': ('texture.tex.28', b'tex')}
        bundle.include_dependencies(resources.get)
        self.assertIn('natives/STM/texture.tex.28', bundle.files)
        self.assertIn('vfx/火焰.vsdf', bundle.manifest['unresolved_dependencies'])


if __name__ == '__main__':unittest.main()
