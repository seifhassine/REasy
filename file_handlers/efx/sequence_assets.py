"""PNG/atlas to TEX + UVS + donor EFX authoring without game deployment."""
from __future__ import annotations

import copy
import json
import math
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import numpy as np
from PySide6.QtGui import QImage

from file_handlers.tex.tex_encoder import encode_tex28
from file_handlers.uvs.uvs_file import UvsFile, UvsTexture, UvsSequence, UvsPattern
from .efx_file import EfxFile
from .editing import EfxEditor, locate
from .graph import records, scopes


def natural_order(paths):
    return sorted(paths, key=lambda p: [int(v) if v.isdecimal() else v.casefold() for v in re.split(r'(\d+)', str(p))])


def load_rgba(path):
    image = QImage(str(path))
    if image.isNull():
        raise ValueError(f'Cannot load image: {path}')
    image = image.convertToFormat(QImage.Format_RGBA8888)
    return np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.bytesPerLine())[:, :image.width() * 4].reshape(image.height(), image.width(), 4).copy()


def slice_atlas(path, rectangles):
    image = load_rgba(path)
    frames = []
    for rectangle in rectangles:
        if len(rectangle) != 4 or any(type(v) is not int for v in rectangle):
            raise ValueError('Atlas rectangles require integer x, y, width, height')
        x, y, w, h = rectangle
        if min(x, y) < 0 or min(w, h) <= 0 or x + w > image.shape[1] or y + h > image.shape[0]:
            raise ValueError(f'Atlas rectangle outside image: {rectangle}')
        frames.append(image[y:y + h, x:x + w].copy())
    return frames


def _half(image, srgb):
    h, w = image.shape[:2]
    data = image.astype(np.float32) / 255
    rgb, alpha = data[..., :3], data[..., 3:4]
    if srgb:
        rgb = np.where(rgb <= .04045, rgb / 12.92, ((rgb + .055) / 1.055) ** 2.4)
    combined = np.concatenate((rgb * alpha, alpha), axis=-1)
    combined = combined.reshape(max(1, h // 2), min(h, 2), max(1, w // 2), min(w, 2), 4).mean(axis=(1, 3))
    alpha = combined[..., 3:4]
    rgb = np.divide(combined[..., :3], alpha, out=np.zeros_like(combined[..., :3]), where=alpha > 0)
    if srgb:
        rgb = np.where(rgb <= .0031308, rgb * 12.92, 1.055 * rgb ** (1 / 2.4) - .055)
    return np.rint(np.clip(np.concatenate((rgb, alpha), axis=-1), 0, 1) * 255).astype(np.uint8)


def make_atlas(frames, *, padding=4, columns=None, srgb=True, mipmaps=True, compression='bc7'):
    if not frames or type(padding) is not int or padding < 0:
        raise ValueError('At least one frame and nonnegative integer padding are required')
    if any(f.dtype != np.uint8 or f.ndim != 3 or f.shape[2] != 4 or min(f.shape[:2]) < 1 for f in frames):
        raise ValueError('Frames must be nonempty RGBA8 images')
    pow2 = lambda n: 1 << (n - 1).bit_length()
    width, height = max(f.shape[1] for f in frames), max(f.shape[0] for f in frames)
    cw, ch = pow2(width + 2 * padding), pow2(height + 2 * padding)
    if columns is None:
        columns = pow2(math.ceil(math.sqrt(len(frames) * ch / cw)))
    if type(columns) is not int or columns < 1 or columns & (columns - 1):
        raise ValueError('Atlas columns must be a positive power of two')
    rows = pow2(math.ceil(len(frames) / columns))
    if cw * columns > 8192 or ch * rows > 8192:
        raise ValueError('Atlas exceeds 8192 pixels; reduce frame size or adjust columns')
    cells = np.zeros((rows * columns, ch, cw, 4), dtype=np.uint8)
    rectangles = []
    for i, frame in enumerate(frames):
        y, x = padding + (height - frame.shape[0]) // 2, padding + (width - frame.shape[1]) // 2
        cells[i, y:y + frame.shape[0], x:x + frame.shape[1]] = frame
        rectangles.append((i % columns * cw + padding, i // columns * ch + padding, width, height))
    mips = []
    while True:
        level = np.concatenate([np.concatenate(cells[r * columns:(r + 1) * columns], axis=1) for r in range(rows)], axis=0)
        mips.append(level)
        # Stop before filtering across cells (and before BC7 shares a block across frames).
        minimum = 4 if compression == 'bc7' else 1
        if not mipmaps or min(cells.shape[1:3]) <= minimum:
            break
        cells = np.stack([_half(cell, srgb) for cell in cells])
    return mips, rectangles


def resource_base(value):
    path = PurePosixPath(value.replace('\\', '/'))
    if path.is_absolute() or not path.parts or any(p in ('..', '.') or ':' in p for p in path.parts) or '\0' in value:
        raise ValueError('Resource base must be a relative virtual path without parent traversal')
    if path.parts[0].casefold() == 'natives' or path.suffix:
        raise ValueError('Use a resource base without natives/STM or a file extension')
    return str(path)


def resource_references(document):
    found = set()
    extensions = re.compile(r'\.(?:tex|uvs|mesh|mdf2|mmtr|efx|vsdf|vfxshader|user|pfb|vf|ies|motlist)$', re.I)
    for _, doc, _ in scopes(document):
        for attr in doc.all_attributes():
            for _, record in records(attr.body):
                for item in record.fields.values():
                    values = item.value if item.kind == 'Array' else [item.value]
                    for value in values:
                        if isinstance(value, str) and extensions.search(value):
                            found.add(value.replace('\\', '/'))
        for record in (*doc.field_parameters, *doc.uvar_groups):
            for item in record.fields.values():
                if isinstance(item.value, str) and extensions.search(item.value):
                    found.add(item.value.replace('\\', '/'))
    return sorted(found)


@dataclass
class SequenceBundle:
    files: dict[str, bytes]
    document: EfxFile
    uvs: UvsFile
    atlas: np.ndarray
    manifest: dict

    def include_dependencies(self, resolver):
        """Copy resolved references, recursively following EFX and UVS containers."""
        queue = list(self.manifest['external_dependencies']); visited, copied, unresolved = set(), [], []
        while queue:
            reference = queue.pop(0).replace('\\', '/')
            key = reference.casefold()
            if key in visited:
                continue
            visited.add(key)
            normalized = reference[1:] if reference.startswith('@') else reference
            path = PurePosixPath(normalized)
            if path.is_absolute() or any(p == '..' or ':' in p for p in path.parts):
                raise ValueError(f'Invalid resource reference: {reference}')
            result = resolver(reference)
            if result is None:
                unresolved.append(reference); continue
            source, raw = result
            suffix = re.search(r'(\.\d+(?:\.(?:x64|stm))?)$', str(source), re.I)
            if suffix is None:
                raise ValueError(f'Resolved resource needs a versioned filename: {source}')
            target = (normalized if normalized.casefold().startswith('natives/') else 'natives/STM/' + normalized) + suffix[1]
            existing = next((p for p in self.files if p.casefold() == target.casefold()), None)
            if existing:
                if self.files[existing] != raw:
                    raise ValueError(f'Resource dependency conflicts with generated output: {reference}')
                continue
            self.files[target] = raw; copied.append(target)
            if key.endswith('.uvs'):
                uvs = UvsFile(); uvs.read(raw, version=int(suffix[1].split('.')[1]))
                queue.extend(p for t in uvs.textures for p in (t.path, t.normal_path, t.specular_path, t.alpha_path) if p)
            elif key.endswith('.efx'):
                effect = EfxFile.from_bytes(raw, version=int(suffix[1].split('.')[1]))
                queue.extend(resource_references(effect))
        self.manifest['copied_dependencies'] = copied
        self.manifest['unresolved_dependencies'] = unresolved

    def publish(self, destination):
        """Publish a complete new bundle; never replace an existing tree."""
        destination = Path(destination).resolve()
        if destination.exists():
            raise ValueError('Bundle destination already exists; select a new directory')
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix='.efx-bundle-', dir=destination.parent))
        try:
            for name, data in self.files.items():
                target = temporary / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            (temporary / 'manifest.json').write_text(json.dumps(self.manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            temporary.rename(destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return destination


def build_sequence_bundle(document, entry_path, frames, base, *, fps=60., padding=4, columns=None, compression='bc7', srgb=True, mipmaps=True):
    if document.version != 2818730:
        raise ValueError('Sequence authoring currently targets Rise/Sunbreak EFX 2818730')
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError('Frame rate must be positive and finite')
    base = resource_base(base)
    editor = EfxEditor(copy.deepcopy(document))
    entry = locate(editor.document, entry_path).value
    from .efx_file import EfxEntry
    if not isinstance(entry, EfxEntry):
        raise ValueError('Select an emitter entry')
    uv = next((a for a in entry.attributes if a.name == 'UVSequence'), None)
    if uv is None:
        raise ValueError('The donor emitter must contain a UVSequence attribute')
    mips, rectangles = make_atlas(frames, padding=padding, columns=columns, srgb=srgb, mipmaps=mipmaps, compression=compression)
    uvs = UvsFile()
    uvs.version, uvs.header.attributes = 7, 1
    uvs.textures = [UvsTexture(path=base + '.tex')]
    height, width = mips[0].shape[:2]
    uvs.sequences = [UvsSequence(patterns=[UvsPattern(left=x / width, top=y / height, right=(x + w) / width, bottom=(y + h) / height) for x, y, w, h in rectangles])]
    ai = entry.attributes.index(uv)
    for key, value in {'UVSPath': base + '.uvs', 'SequenceNo': [0, 1], 'PatternNo': [0, 1], 'PlaySpeed': [fps / 60., fps / 60.]}.items():
        editor.apply({'op': 'set', 'path': f'{entry_path}/attributes/{ai}/fields/{key}', 'value': value})
    tex_data, uvs_data, efx_data = encode_tex28(mips, compression=compression, srgb=srgb), uvs.write(), editor.document.build_validated()
    check = UvsFile()
    if not check.read(uvs_data, version=7) or check.write() != uvs_data:
        raise ValueError('Generated UVS failed roundtrip validation')
    files = {f'natives/STM/{base}.tex.28': tex_data, f'natives/STM/{base}.uvs.7': uvs_data, f'natives/STM/{base}.efx.2818730': efx_data}
    manifest = {'format': 'REasy.EFX.SequenceBundle', 'version': 1, 'entry': entry_path, 'resource': base,
                'frames': len(frames), 'fps': fps, 'compression': compression, 'srgb': srgb,
                'atlas_size': [width, height], 'mips': len(mips), 'rectangles': rectangles,
                'generated': list(files), 'external_dependencies': [p for p in resource_references(editor.document) if p != base + '.uvs'],
                'preview': 'Atlas playback; native shader and runtime behavior require in-game validation'}
    return SequenceBundle(files, editor.document, uvs, mips[0], manifest)
