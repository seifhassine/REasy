"""Transactional UVS edits with references in a loaded EFX document."""
import copy
import struct

from file_handlers.uvs.uvs_file import UvsFile, UvsSequence, UvsPattern, UvsTexture
from .efx_file import EfxFile
from .editing import EfxEditor


def _key(path):
    return path.replace('\\', '/').casefold()


def selection(pair, count):
    low, high = pair
    if low < 0 or high < low or low >= count or high > count:
        raise ValueError(f'UVS selection {pair} is outside 0..{count}')
    return [low] if low == high else list(range(low, high))


def _remap_range(pair, order):
    mapping = {old: new for new, old in enumerate(order) if old is not None}
    low, high = pair
    selected = [low] if low == high else list(range(low, high))
    if any(index not in mapping for index in selected):
        raise ValueError('UVS slot is still referenced; retarget its EFX selection first')
    mapped = sorted(mapping[index] for index in selected)
    if not mapped or mapped != list(range(mapped[0], mapped[-1]+1)):
        raise ValueError('Reordering would make an EFX random selection non-contiguous')
    return (mapped[0], mapped[0] if low == high else mapped[-1]+1)


def pattern_from_data(value):
    allowed = {'flags', 'left', 'top', 'right', 'bottom', 'texture_index', 'cutout_uvs'}
    if set(value) - allowed:
        raise ValueError('Unknown UVS pattern fields')
    for name in ('left', 'top', 'right', 'bottom', 'texture_index'):
        if name not in value:
            raise ValueError(f'Missing pattern {name}')
    return UvsPattern(**value)


class SequenceProject:
    def __init__(self, document, uvs, resource):
        if document.version != 2818730 or uvs.version != 7:
            raise ValueError('Linked UVS editing targets Rise/Sunbreak EFX 2818730 and UVS 7')
        self.document, self.uvs, self.resource = copy.deepcopy(document), copy.deepcopy(uvs), resource
        self._undo, self._redo = [], []
        self.validate()

    def bindings(self):
        return [a.body for a in self.document.all_attributes(recursive=True) if a.name == 'UVSequence' and _key(a.body['UVSPath']) == _key(self.resource)]

    def snapshot(self):
        return self.document.build_validated(), self.uvs.write()

    def restore(self, snapshot):
        self.document = EfxFile.from_bytes(snapshot[0], version=self.document.version)
        uvs = UvsFile(); uvs.read(snapshot[1], version=self.uvs.version); self.uvs = uvs

    def undo(self):
        if self._undo:
            self._redo.append(self.snapshot()); self.restore(self._undo.pop()); return True
        return False

    def redo(self):
        if self._redo:
            self._undo.append(self.snapshot()); self.restore(self._redo.pop()); return True
        return False

    def bundle(self, effect_resource):
        import numpy as np
        from .sequence_assets import SequenceBundle, resource_base, resource_references
        base = resource_base(effect_resource)
        if not self.resource.lower().endswith('.uvs'):
            raise ValueError('Linked resource must end in .uvs')
        uvbase = resource_base(self.resource[:-4])
        efx, uv = self.snapshot()
        files = {f'natives/STM/{base}.efx.2818730': efx, f'natives/STM/{uvbase}.uvs.7': uv}
        dependencies = {p for t in self.uvs.textures for p in (t.path, t.normal_path, t.specular_path, t.alpha_path) if p}
        dependencies.update(p for p in resource_references(self.document) if _key(p) != _key(self.resource))
        manifest = {'format': 'REasy.EFX.LinkedUvsBundle', 'version': 1, 'resource': base, 'uvs': self.resource,
                    'generated': list(files), 'external_dependencies': sorted(dependencies)}
        return SequenceBundle(files, self.document, self.uvs, np.empty((0, 0, 4), np.uint8), manifest)

    def apply(self, operation):
        before = self.snapshot()
        try:
            self._apply(operation); self.validate(); after = self.snapshot()
            check = UvsFile(); check.read(after[1], self.uvs.version)
            if check.write() != after[1]:
                raise ValueError('UVS serialization is not stable')
        except (ValueError, KeyError, TypeError, IndexError, OverflowError, struct.error):
            self.restore(before); raise
        if before != after:
            self._undo.append(before); self._redo.clear()

    def validate(self):
        import math
        for texture in self.uvs.textures:
            for path in (texture.path, texture.normal_path, texture.specular_path, texture.alpha_path):
                if not isinstance(path, str) or '\0' in path:
                    raise ValueError('Texture paths must be strings without null characters')
        for seq in self.uvs.sequences:
            if not seq.patterns:
                raise ValueError('UVS sequences must contain at least one frame')
            for pattern in seq.patterns:
                if type(pattern.texture_index) is not int or not 0 <= pattern.texture_index < len(self.uvs.textures):
                    raise ValueError('UVS texture index is outside the texture table')
                if any(not math.isfinite(v) for v in (pattern.left, pattern.top, pattern.right, pattern.bottom)):
                    raise ValueError('UV coordinates must be finite')
                if any(len(point) != 2 or any(not math.isfinite(v) for v in point) for point in pattern.cutout_uvs):
                    raise ValueError('Cutout UVs must contain finite coordinate pairs')
        for binding in self.bindings():
            for index in selection(binding['SequenceNo'], len(self.uvs.sequences)):
                selection(binding['PatternNo'], len(self.uvs.sequences[index].patterns))

    def _apply(self, operation):
        op = operation['op']
        if op == 'efx':
            editor = EfxEditor(self.document); editor.apply(operation['operation']); self.document = editor.document; return
        category, action = op.split('-', 1)
        sequence = operation.get('sequence')
        collection = self.uvs.sequences if category == 'sequence' else self.uvs.textures if category == 'texture' else self.uvs.sequences[sequence].patterns if category == 'frame' else None
        if collection is None:
            raise ValueError('UVS operations address sequence, frame or texture')
        index = operation.get('index', len(collection))
        if type(index) is not int or not 0 <= index <= len(collection) or action != 'insert' and index == len(collection):
            raise ValueError('UVS index is outside the collection')
        order = list(range(len(collection)))
        if action in ('insert', 'set'):
            payload = operation['value']
            value = UvsSequence(patterns=[pattern_from_data(p) for p in payload['patterns']]) if category == 'sequence' else UvsTexture(**payload) if category == 'texture' else pattern_from_data(payload)
            if action == 'set':
                if category == 'sequence' and len(value.patterns) != len(collection[index].patterns):
                    raise ValueError('Use frame operations to change a sequence frame count')
                collection[index] = value; return
            collection.insert(index, value); order.insert(index, None)
        elif action == 'copy':
            target = operation['to']
            if type(target) is not int or not 0 <= target <= len(collection):
                raise ValueError('Copy destination is outside the collection')
            collection.insert(target, copy.deepcopy(collection[index])); order.insert(target, None)
        elif action == 'delete':
            collection.pop(index); order.pop(index)
        elif action == 'move':
            target = operation['to']
            if type(target) is not int or not 0 <= target < len(collection):
                raise ValueError('Move destination is outside the collection')
            collection.insert(target, collection.pop(index)); order.insert(target, order.pop(index))
        else:
            raise ValueError(f'Unknown UVS operation: {op}')
        if category == 'sequence':
            for binding in self.bindings():
                binding['SequenceNo'] = _remap_range(binding['SequenceNo'], order)
        elif category == 'frame':
            for binding in self.bindings():
                selected = selection(binding['SequenceNo'], len(self.uvs.sequences))
                if sequence in selected:
                    remapped = _remap_range(binding['PatternNo'], order)
                    if len(selected) > 1 and remapped != binding['PatternNo']:
                        raise ValueError('This EFX binding shares its frame range across multiple sequences; select one sequence first')
                    binding['PatternNo'] = remapped
        else:
            mapping = {old: new for new, old in enumerate(order) if old is not None}
            for seq in self.uvs.sequences:
                for pattern in seq.patterns:
                    if pattern.texture_index not in mapping:
                        raise ValueError('Texture is still referenced by a UVS frame')
                    pattern.texture_index = mapping[pattern.texture_index]
