"""See docs/internals/mesh-preview.md, Device resources."""
from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable, Sequence
from threading import Lock, Thread

lazy import fastmatpreview
lazy from PySide6.QtWidgets import QApplication
lazy from file_handlers.tex.gdeflate import library_path
lazy from tools.github_downloader import show_download_failure
lazy from tools.shader_tools import AGILITY_SDK, PAGES, ShaderToolDownloadCancelled, prompt_shader_tool_download
lazy from utils.app_paths import downloads_directory

from .material_preview import RANGE_CBV, RANGE_SRV, VIEW_DIMENSIONS, Following, TexelRows, same_texels, texel_sample

# D3D12_RESOURCE_FLAGS bits.
RENDER_TARGET, DEPTH_STENCIL, UNORDERED_ACCESS = 1, 2, 4
BUFFER, TEXTURE_2D, TEXTURE_3D = (VIEW_DIMENSIONS[name] for name in ("buffer", "texture2d", "texture3d"))

_warming = Lock()
_warm_up: Thread | None = None
_warm_up_failure: Exception | None = None
_software_reason = ""
_inflating = Lock()
_inflates = False
_runtime = Lock()
_runtime_ready = _runtime_offered = False


def _inflate() -> None:
    """Lets the native device inflate GDeflate streams, once."""
    global _inflates
    with _inflating:
        if not _inflates:
            fastmatpreview.gdeflate(str(library_path()))
            _inflates = True


def prepare_runtime() -> None:
    global _runtime_ready
    with _runtime:
        if not _runtime_ready and not fastmatpreview.heap_indexing():
            folder = downloads_directory("d3d12")
            if not (folder / "D3D12Core.dll").is_file():
                raise RuntimeError(f"Windows' Direct3D 12 runtime lacks Shader Model 6.6: get {PAGES['d3d12']} into "
                                   f"{folder}.")
            fastmatpreview.runtime(str(folder), int(AGILITY_SDK.split(".")[1]))
        _runtime_ready = True


def warm_up() -> None:
    """Makes the process's hardware device on a thread of its own, once; the first hardware ``Resident`` waits for it
    and raises what it raised."""
    global _warm_up, _runtime_offered
    if not (_runtime_offered or fastmatpreview.heap_indexing()
            or (downloads_directory("d3d12") / "D3D12Core.dll").is_file()):
        _runtime_offered = True
        try:
            prompt_shader_tool_download("d3d12")
        except ShaderToolDownloadCancelled:
            pass
        except RuntimeError as exc:
            show_download_failure(QApplication.activeWindow(), exc)
    with _warming:
        if _warm_up is None:
            _warm_up = Thread(target=_make_device, name="Device warm-up", daemon=True)
            _warm_up.start()


def _make_device() -> None:
    global _warm_up_failure
    try:
        prepare_runtime()
        # The device and the scene's queue stay with the process when the scene goes.
        fastmatpreview.Scene(True)
    except Exception as exc:
        _warm_up_failure = exc


def _joined_warm_up() -> None:
    global _warm_up_failure
    with _warming:
        thread = _warm_up
    if thread is None:
        return
    thread.join()
    with _warming:
        failure, _warm_up_failure = _warm_up_failure, None
    if failure is not None:
        raise failure


def software_reason() -> str:
    """Why hardware-requested drawing runs on WARP instead: no adapter made a Direct3D 12 device. Empty otherwise."""
    return _software_reason


def _scene(hardware: bool):
    global _software_reason
    if hardware and not _software_reason:
        try:
            _joined_warm_up()
            prepare_runtime()
            return fastmatpreview.Scene(True)
        except RuntimeError as exc:
            # Virtual machines and old GPUs have no Direct3D 12 adapter; every Windows 10+ has WARP.
            if not str(exc).startswith(("EnumAdapterByGpuPreference", "D3D12CreateDevice")):
                raise
            _software_reason = str(exc)
            print(f"No graphics adapter makes a Direct3D 12 device ({exc}); "
                  "viewports draw with Windows' software renderer (WARP).")
    prepare_runtime()
    return fastmatpreview.Scene(False)


class _Texels:
    """Texels, packed bytes or rows, as a key: hashed by a sample of their bytes packed tightly, equal by all those."""
    __slots__ = ("texels", "sample")

    def __init__(self, texels: bytes | TexelRows):
        self.texels, self.sample = texels, texel_sample(texels)

    def __hash__(self) -> int:
        return hash(self.sample)

    def __eq__(self, other) -> bool:
        if not isinstance(other, _Texels):
            return NotImplemented
        return self.texels is other.texels or self.sample == other.sample and same_texels(self.texels, other.texels)


class Resident:
    """``hardware`` False runs on Microsoft's software device (WARP)."""

    def __init__(self, hardware: bool):
        self.scene = _scene(hardware)
        self.pending: list[int] = []
        self._buffers: dict[bytes, int] = {}
        self._read: dict[tuple, int] = {}
        # Every TexelRows ``read`` was given, by identity: once released they equal no other texels.
        self._rows: dict[tuple, tuple[TexelRows, int]] = {}
        self._following: dict[Hashable, tuple[int, Callable[[], bytes], bytes]] = {}
        self._zeros: dict[tuple, int] = {}
        self._copies: dict[tuple, int] = {}

    def buffer(self, data: bytes, packing: int = 0) -> int:
        """Equal ``data`` share the buffer, so no step may write it; ``packing`` is ``fastmatpreview``'s."""
        data = bytes(data)
        key = (data, packing) if packing else data
        found = self._buffers.get(key)
        if found is None:
            found = self._buffers[key] = self.scene.buffer(data, 0, packing)
        return found

    def following(self, key: Hashable, make: Callable[[], bytes], packing: int = 0) -> int:
        found = self._following.get(key)
        if found is None:
            data = make()
            found = self._following[key] = (self.scene.buffer(data, 0, packing), make, data)
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
        """``entry``'s texels are ``TexelRows`` or bytes; the texture lives as long as the scene."""
        index, form, *shape, texels = entry
        rows = (dimension, form, *shape, id(texels)) if isinstance(texels, TexelRows) else None
        if rows in self._rows:
            return index, form, self._rows[rows][1]
        key = (dimension, form, *shape, _Texels(texels))
        if key not in self._read:
            if isinstance(texels, TexelRows) and texels.packed:
                _inflate()
            self._read[key] = self.scene.texture(form, *shape, dimension, 0,
                                                 texels.rows if isinstance(texels, TexelRows) else texels)
        if rows is not None:
            self._rows[rows] = (texels, self._read[key])
        return index, form, self._read[key]

    def refill(self, texture: int, dimension: int, entry: tuple) -> None:
        """Makes ``texture``, one ``read`` made of TexelRows, anew of ``entry``: (format, *shape, TexelRows)."""
        form, *shape, texels = entry
        if texels.packed:
            _inflate()
        self.scene.refill(texture, form, *shape, dimension, texels.rows)

    def release_texels(self) -> set[int]:
        """Lets go of every TexelRows ``read`` was given; their ids. Each is still the texture it was."""
        released = set()
        for texels, _texture in self._rows.values():
            texels.release()
            released.add(id(texels))
        return released

    def bound(self, job: dict) -> dict:
        def kept(data, packing: int):
            if isinstance(data, Following):
                return self.following(*data, packing)
            return self.buffer(data, packing) if isinstance(data, (bytes, bytearray)) else data

        def kept_entry(item: tuple, packing: int) -> tuple:
            held = kept(item[-1], packing)
            return item if held is item[-1] else (*item[:-1], held)

        def entries(kind: int, dimension: int, items: Sequence[tuple]) -> tuple:
            if kind == RANGE_CBV:
                return tuple(kept_entry(item, 1) for item in items)
            if kind == RANGE_SRV and dimension == BUFFER:
                # A buffer view's elements are its stride apart, or 4 bytes for a raw view.
                return tuple(kept_entry(item, item[2] or 4) for item in items)
            if kind == RANGE_SRV:
                return tuple(self.read(dimension, item) if len(item) == 7 else item for item in items)
            return tuple(items)

        def heap_entry(held: tuple) -> tuple:
            # Entries bound as they are stay the ones jobs share.
            kind, dimension, entry = held
            bound, = entries(kind, dimension, (entry,))
            return held if bound is entry else (kind, dimension, bound)
        return job | {
            "tables": [(*table[:-1], entries(table[0], table[4], table[-1])) for table in job["tables"]],
            "heap": [heap_entry(held) for held in job["heap"]],
        }

    def held(self, job: dict) -> dict:
        return self.bound(job) | {
            "vertex_buffers": [(self.buffer(item[0], 1), *item[1:]) if isinstance(item[0], (bytes, bytearray))
                               else item for item in job["vertex_buffers"]],
            "indices": self.buffer(job["indices"], 1),
        }

    def close(self) -> None:
        self.scene = None
        self.pending.clear()
        self._buffers.clear()
        self._read.clear()
        self._rows.clear()
        self._following.clear()
        self._zeros.clear()
        self._copies.clear()

    def release(self, resources: Iterable[int | None]) -> None:
        """No step using ``resources`` may run again."""
        gone = {resource for resource in resources if resource is not None}
        if not gone:
            return
        if not gone.isdisjoint(self._read.values()):
            raise ValueError("a texture read lives as long as its scene")
        self.scene.release(sorted(gone))
        self._buffers = {data: buffer for data, buffer in self._buffers.items() if buffer not in gone}
        self._following = {key: held for key, held in self._following.items() if held[0] not in gone}
        self._zeros = {key: texture for key, texture in self._zeros.items() if texture not in gone}
        self._copies = {key: step for key, step in self._copies.items() if not gone & set(key[:2])}

    def flush(self, read: Sequence[int] = ()) -> list[bytes]:
        steps = list(self.pending)
        self.pending.clear()
        return self.scene.run(steps, list(read))


__all__ = ["DEPTH_STENCIL", "RENDER_TARGET", "Resident", "TEXTURE_2D", "TEXTURE_3D", "UNORDERED_ACCESS", "warm_up"]
