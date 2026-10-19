"""See docs/internals/mesh-preview.md, Device resources."""
from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable, Sequence

from .material_preview import RANGE_CBV, RANGE_SRV, VIEW_DIMENSIONS, Following

# D3D12_RESOURCE_FLAGS bits.
RENDER_TARGET, DEPTH_STENCIL, UNORDERED_ACCESS = 1, 2, 4
BUFFER, TEXTURE_2D, TEXTURE_3D = (VIEW_DIMENSIONS[name] for name in ("buffer", "texture2d", "texture3d"))


class Resident:
    """``hardware`` False runs on Microsoft's software device (WARP)."""

    def __init__(self, hardware: bool):
        import fastmatpreview

        self.scene = fastmatpreview.Scene(hardware)
        self.pending: list[int] = []
        self._buffers: dict[bytes, int] = {}
        self._read: dict[tuple, int] = {}
        self._following: dict[Hashable, tuple[int, Callable[[], bytes], bytes]] = {}
        self._zeros: dict[tuple, int] = {}
        self._copies: dict[tuple, int] = {}

    def buffer(self, data: bytes) -> int:
        """Equal ``data`` share the buffer, so no step may write it."""
        data = bytes(data)
        found = self._buffers.get(data)
        if found is None:
            found = self._buffers[data] = self.scene.buffer(data, 0)
        return found

    def following(self, key: Hashable, make: Callable[[], bytes]) -> int:
        found = self._following.get(key)
        if found is None:
            data = make()
            found = self._following[key] = (self.scene.buffer(data, 0), make, data)
        return found[0]

    def refresh(self) -> None:
        for key, (buffer, make, held) in self._following.items():
            data = make()
            if data != held:
                self.scene.write(buffer, data)
                self._following[key] = (buffer, make, data)

    def texture(self, form: int, size: Sequence[int], flags: int, texels: bytes | None = None) -> int:
        """``texels`` None for a texture a step writes first."""
        width, height, depth = (*size, 1)[:3]
        return self.scene.texture(form, width, height, depth, 1, TEXTURE_3D if depth > 1 else TEXTURE_2D, flags,
                                  texels)

    def zeros(self, form: int, size: Sequence[int], flags: int, held: int) -> int:
        """Shared by equal arguments, so no step may write it."""
        key = (form, *size, flags)
        if key not in self._zeros:
            self._zeros[key] = self.texture(form, size, flags, bytes(held))
        return self._zeros[key]

    def copy(self, to: int, source: int, *subresources: int) -> int:
        key = (to, source, *subresources)
        if key not in self._copies:
            self._copies[key] = self.scene.copy(*key)
        return self._copies[key]

    def read(self, dimension: int, entry: tuple) -> tuple:
        index, form, *shape, texels = entry
        key = (dimension, form, *shape, texels)
        if key not in self._read:
            self._read[key] = self.scene.texture(form, *shape, dimension, 0, texels)
        return index, form, self._read[key]

    def bound(self, job: dict) -> dict:
        def kept(data):
            if isinstance(data, Following):
                return self.following(*data)
            return self.buffer(data) if isinstance(data, (bytes, bytearray)) else data

        def entries(kind: int, dimension: int, items: Sequence[tuple]) -> tuple:
            if kind == RANGE_CBV or kind == RANGE_SRV and dimension == BUFFER:
                return tuple((*item[:-1], kept(item[-1])) for item in items)
            if kind == RANGE_SRV:
                return tuple(self.read(dimension, item) if len(item) == 7 else item for item in items)
            return tuple(items)
        return job | {
            "tables": [(*table[:-1], entries(table[0], table[4], table[-1])) for table in job["tables"]],
            "heap": [(kind, dimension, *entries(kind, dimension, (entry,)))
                     for kind, dimension, entry in job["heap"]],
        }

    def held(self, job: dict) -> dict:
        return self.bound(job) | {
            "vertex_buffers": [(self.buffer(item[0]), *item[1:]) if isinstance(item[0], (bytes, bytearray)) else item
                               for item in job["vertex_buffers"]],
            "indices": self.buffer(job["indices"]),
        }

    def close(self) -> None:
        self.scene = None
        self.pending.clear()
        self._buffers.clear()
        self._read.clear()
        self._following.clear()
        self._zeros.clear()
        self._copies.clear()

    def release(self, resources: Iterable[int | None]) -> None:
        """No step using ``resources`` may run again."""
        gone = {resource for resource in resources if resource is not None}
        if not gone:
            return
        self.scene.release(sorted(gone))
        self._buffers = {data: buffer for data, buffer in self._buffers.items() if buffer not in gone}
        self._zeros = {key: texture for key, texture in self._zeros.items() if texture not in gone}
        self._copies = {key: step for key, step in self._copies.items() if not gone & set(key[:2])}

    def flush(self, read: Sequence[int] = ()) -> list[bytes]:
        steps = list(self.pending)
        self.pending.clear()
        return self.scene.run(steps, list(read))


__all__ = ["DEPTH_STENCIL", "RENDER_TARGET", "Resident", "TEXTURE_2D", "TEXTURE_3D", "UNORDERED_ACCESS"]
