from __future__ import annotations

import weakref
from pathlib import Path, PurePosixPath

from utils.resource_file_utils import natives_split, resolve_handler_resource_data

from .sdftex_file import SdfTexFile


def reference_path(value: str) -> str:
    value = str(value).replace('\\', '/').strip()
    if value.startswith('@'):
        value = value[1:]
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or '..' in path.parts
            or any(char in value for char in ':*?"<>|\x00')
            or not value.casefold().endswith('.sdftex')):
        raise ValueError("SDFTEX reference must be a relative .sdftex resource path without a version")
    if value.casefold().startswith(('natives/stm/', 'natives/x64/')):
        value = '/'.join(path.parts[2:])
    if value.casefold().startswith('streaming/'):
        raise ValueError("SDFTEX is not a streaming MESH companion")
    return value


def native_root(primary_path):
    split = natives_split(primary_path)
    if split is None:
        raise ValueError("Saving a baked distance field needs a mesh path below natives/<platform>")
    return split


class MeshDistanceField:
    # A strong cycle through the handler (a Qt object) would be left to the GC on an arbitrary thread.
    def __init__(self, handler):
        self._handler = weakref.ref(handler)
        self.document: SdfTexFile | None = None
        self.resolved_path = ''
        self.authored = False
        self._original_reference: str | None = None

    @property
    def handler(self):
        return self._handler()

    @property
    def version(self):
        return getattr(self.handler.mesh_profile, 'sdf_texture_resource_version', None)

    @property
    def reference(self):
        return str(getattr(self.handler.mesh, 'sdf_path', '') or '')

    def proposed_reference(self):
        if self.reference:
            return reference_path(self.reference)
        _, parts = native_root(self.handler.filepath)
        name = parts[-1]
        stem = name[:name.casefold().rfind('.mesh.')]
        if not stem or '.mesh.' not in name.casefold():
            raise ValueError("Cannot derive a SDFTEX resource name from this mesh filename")
        return reference_path('/'.join((*parts[:-1], stem+'.sdftex')))

    def load(self):
        if self.document is not None:
            return self.document
        if not self.reference:
            return None
        if self.version is None:
            raise ValueError("This mesh build has no verified SDFTEX resource profile")
        path = f'{reference_path(self.reference)}.{self.version}'
        resolved = resolve_handler_resource_data(self.handler, path, allow_selection_dialog=False)
        own_tree = natives_split(self.handler.filepath) if Path(self.handler.filepath).is_absolute() else None
        if resolved is None and own_tree is not None and (own_tree[0] / path).is_file():
            resolved = str(own_tree[0] / path), (own_tree[0] / path).read_bytes()
        if resolved is None:
            raise FileNotFoundError(f'SDFTEX not found: {path}')
        source, data = resolved
        document = SdfTexFile.from_bytes(data, resource_version=self.version)
        self.resolved_path, self.document = str(source), document
        return document

    def stage(self, document: SdfTexFile, reference: str):
        reference = reference_path(reference)
        if not self.handler.supports_editing() or document.resource_version != self.version:
            raise ValueError("Baked SDFTEX does not match this writable mesh build")
        document.validate()
        if self._original_reference is None:
            self._original_reference = self.reference
        self.document = document
        self.authored = True
        self.resolved_path = ''
        self.handler.mesh.sdf_path = reference
        self.handler._pending_write_result = None
        self.handler.modified = True

    def mark_saved(self):
        self._original_reference = self.reference

    def discard(self):
        if self._original_reference is not None:
            self.handler.mesh.sdf_path = self._original_reference

    def output(self, primary_path):
        if not self.authored:
            return {}
        root, _ = native_root(primary_path)
        path = root / f'{reference_path(self.reference)}.{self.version}'
        # A second containment check also catches existing symlinks/junctions.
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("SDFTEX output escapes the mesh's native resource root")
        return {str(path): self.document.to_bytes()}
