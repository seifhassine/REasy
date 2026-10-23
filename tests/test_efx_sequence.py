import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import numpy as np
from PySide6.QtGui import QImage

from file_handlers.efx.editing import EfxEditor
from file_handlers.efx.efx_file import EfxFile
from file_handlers.efx.sequence_assets import build_sequence_bundle, make_atlas, _half, slice_atlas
from file_handlers.tex.tex_file import TexFile
from file_handlers.tex.texture_decoder import decode_tex_mip
from file_handlers.uvs.uvs_file import UvsFile, UvsTexture
from tests.test_efx import golden
from tools.cli.main import main


def donor():
    editor = EfxEditor(EfxFile.from_bytes(golden(), version=2818730))
    editor.apply({'op': 'insert', 'path': 'entries/0/attributes', 'value': {'type': 'UVSequence', 'fields': {
        'SequenceNo': [0, 1], 'PatternNo': [0, 1], 'PlaySpeed': [1, 1], 'Flags': 3, 'UVSPath': 'old.uvs'}}})
    return editor.document


class SequenceTests(unittest.TestCase):
    def test_explicit_invalid_column_counts_are_not_replaced_by_defaults(self):
        frame = np.full((4, 4, 4), 255, np.uint8)
        for columns in (0, -1, 3, True, 2.0):
            with self.subTest(columns=columns), self.assertRaisesRegex(ValueError, 'positive power of two'):
                make_atlas([frame], columns=columns)
        self.assertTrue(make_atlas([frame], columns=None)[0])

    def test_32_and_64_frame_bundle_decodes_and_preserves_donor(self):
        for count, compression in ((32, 'rgba8'), (64, 'bc7')):
            with self.subTest(count=count):
                doc = donor()
                original = doc.build_validated()
                frames = [np.full((12, 16, 4), [i * 3, 128, 255 - i * 3, 192], np.uint8) for i in range(count)]
                bundle = build_sequence_bundle(doc, 'entries/0', frames, 'vfx/custom/fire', fps=30, compression=compression)
                self.assertEqual(original, doc.build_validated())
                tex = TexFile(); tex.read(bundle.files['natives/STM/vfx/custom/fire.tex.28'], 28)
                decoded = decode_tex_mip(tex)
                image = np.frombuffer(decoded.rgba, np.uint8).reshape(tex.header.height, tex.header.width, 4)
                for frame, (x, y, w, h) in zip(frames, bundle.manifest['rectangles']):
                    self.assertLess(np.abs(image[y:y+h, x:x+w].astype(float) - frame).mean(), 2.)
                self.assertEqual(len(bundle.uvs.sequences[0].patterns), count)
                uv = next(a.body for a in bundle.document.entries[0].attributes if a.name == 'UVSequence')
                self.assertEqual(uv['PlaySpeed'], (.5, .5))
                self.assertEqual(uv['UVSPath'], 'vfx/custom/fire.uvs')
                self.assertEqual(bundle.document.entries[0].attributes[0].body['shaderPath'], doc.entries[0].attributes[0].body['shaderPath'])
                with tempfile.TemporaryDirectory() as folder:
                    output = Path(folder) / 'candidate'
                    bundle.publish(output)
                    self.assertEqual(set(p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file()), {*bundle.files, 'manifest.json'})
                    with self.assertRaises(ValueError):
                        bundle.publish(output)

    def test_linear_light_alpha_weighted_mips_and_frame_isolation(self):
        pixels = np.array([[[255, 0, 0, 255], [0, 255, 0, 0]], [[0, 0, 255, 0], [0, 0, 0, 0]]], np.uint8)
        self.assertEqual(_half(pixels, True)[0, 0].tolist(), [255, 0, 0, 64])
        pixels[..., 3] = 255
        self.assertEqual(_half(pixels, True)[0, 0].tolist(), [137, 137, 137, 255])
        frames = [np.full((16, 16, 4), color, np.uint8) for color in ([255, 0, 0, 255], [0, 0, 255, 255])]
        mips, _ = make_atlas(frames, padding=0, columns=2)
        for level in mips:
            split = level.shape[1] // 2
            self.assertTrue(np.all(level[:, :split, 2] == 0))
            self.assertTrue(np.all(level[:, split:, 0] == 0))

    def test_atlas_crop_unicode_offsets_and_cli_dry_run(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            pixels = np.full((8, 16, 4), [60, 80, 100, 120], np.uint8)
            image = QImage(pixels.data, 16, 8, 64, QImage.Format_RGBA8888).copy()
            image.save(str(folder / 'atlas.png'))
            self.assertEqual(slice_atlas(folder / 'atlas.png', [[8, 0, 8, 8]])[0].shape, (8, 8, 4))
            source = folder / 'donor.efx.2818730'; source.write_bytes(donor().build_validated())
            for dry in (True, False):
                stream = io.StringIO()
                with redirect_stdout(stream):
                    status = main(['efx', 'sequence', str(source), '--entry', 'entries/0', '--atlas', str(folder / 'atlas.png'),
                                   '--rectangles', '[[0,0,8,8],[8,0,8,8]]', '--resource', 'vfx/custom/test', '-o', str(folder / 'result'), '--json',
                                   *(['--dry-run'] if dry else [])])
                self.assertEqual(status, 0, stream.getvalue())
                self.assertEqual((folder / 'result').exists(), not dry)
                self.assertEqual(json.loads(stream.getvalue())['details']['frames'], 2)
        uvs = UvsFile(); uvs.version = 7; uvs.textures = [UvsTexture(path='emoji_🔥.tex', normal_path='normal.tex')]
        other = UvsFile(); other.read(uvs.write(), 7)
        self.assertEqual(other.textures[0].normal_path, 'normal.tex')


if __name__ == '__main__':
    unittest.main()
