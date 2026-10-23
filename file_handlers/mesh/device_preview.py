from __future__ import annotations

import math
import weakref
from collections.abc import Callable, Iterable, Mapping

from PySide6.QtCore import QT_TRANSLATE_NOOP, QElapsedTimer, QObject, Qt, QTimer, Signal
lazy from PySide6.QtWidgets import QApplication

lazy from file_handlers.mmtr.material_preview import (
    GpuBufferLocation, MaterialPreviewError, resident_key, streamed_key,
)
lazy from file_handlers.mmtr.mmtrs_file import MmtrsFormatError, resolve_mmtr_resource
lazy from file_handlers.mmtr.pretransform import ChainLevel
lazy from file_handlers.sdf.dxc import DxcNotFound, find_dxcompiler
lazy from file_handlers.strands.chain_scene import (ChainScene, gather as gather_strands, handler_files, shader_paths,
                                               with_files)
lazy from tools.github_downloader import show_download_failure
lazy from tools.shader_tools import ShaderToolDownloadCancelled, prompt_shader_tool_download
from ui.scene.device_frames import LIMIT_MOST, RenderThread, preview_image
lazy from utils.resource_file_utils import ReadOnUse, ResourceLocation

lazy from .material_resolver import MeshMaterialResolver


_MASTER_NOT_FOUND = QT_TRANSLATE_NOOP("MaterialPreview", "Master material {path} was not found.")

_COMPILER_OFFERED = weakref.WeakSet()


def _compiler_found() -> bool:
    try:
        find_dxcompiler()
    except DxcNotFound:
        return False
    return True


def offer_compiler(handler, reasons: Iterable) -> bool:
    if (handler is None or handler in _COMPILER_OFFERED
            or not any(isinstance(reason, DxcNotFound) for reason in reasons) or _compiler_found()):
        return False
    _COMPILER_OFFERED.add(handler)
    try:
        return prompt_shader_tool_download("dxc")
    except ShaderToolDownloadCancelled:
        return False
    except RuntimeError as exc:
        show_download_failure(QApplication.activeWindow(), exc)
        return False


def gather_master(mmtr_path: str, resolve: Callable[[str], tuple[str, bytes] | None],
                  masters: dict[str, tuple[str, bytes]]) -> str:
    missing = []

    def record(path: str) -> tuple[str, bytes] | None:
        found = resolve(path)
        if found is None:
            missing.append(path)
        else:
            masters[path] = found
        return found

    try:
        resolve_mmtr_resource(mmtr_path, record)
    except MmtrsFormatError as exc:
        return str(exc)
    except FileNotFoundError:
        if not missing:
            raise
    return str(MaterialPreviewError(_MASTER_NOT_FOUND, {"path": missing[0]})) if missing else ""


class DevicePreview(QObject):
    view_type: type
    active_changed = Signal(bool)
    frame_changed = Signal(object)
    status_changed = Signal()
    image_read = Signal(object)
    _finished = Signal(object, object, object)
    _read = Signal(object, object)

    def __init__(self, handler, session, inspection: Callable[[], object], parent=None):
        from file_handlers.mmtr.resident import warm_up

        super().__init__(parent)
        warm_up()
        self._handler, self._session, self._inspection = handler, session, inspection
        self._drawer = None
        self._thread: RenderThread | None = None
        self._scene = None
        self._active = False
        self._window: int | None = None
        self._frames_shown = True
        self._view = None
        self._overlays = None
        self._frame = None
        self._reason = ""
        self._failures: dict[int, str] = {}
        self._told_failures: list[tuple[Mapping | None, dict[int, str]]] = []
        self._error = ""
        self._finished.connect(self._on_finished)
        self._read.connect(self._on_read)
        self._image_of = None
        self._rate = LIMIT_MOST
        self._clock = QElapsedTimer()
        self._clock.start()
        self._due: float | None = None
        self._asked_at = 0.0
        self.step_rate: float = self._rate
        self._stepped = 0.0
        self._drawing = False
        self._offering = False
        self._ticker = QTimer(self)
        self._ticker.setSingleShot(True)
        self._ticker.setTimerType(Qt.TimerType.PreciseTimer)
        self._ticker.timeout.connect(self._tick)

    def _gather(self):
        raise NotImplementedError

    def _make_drawer(self):
        raise NotImplementedError

    def _request(self) -> tuple:
        raise NotImplementedError

    def _frame_lines(self, _frame) -> list[str]:
        return []

    def _stepping(self) -> bool:
        return False

    def _gather_strands(self, handler, mesh, game: str, profile, shown: tuple[str, str, str], lod: int,
                        level: ChainLevel | None, resolve: Callable[[str], tuple[str, bytes] | None],
                        masters: dict[str, tuple[str, bytes]], files: dict[str, bytes],
                        edited=None) -> tuple[ChainScene | None, str]:
        try:
            strands = gather_strands(with_files(handler_files(handler, game), edited or {}), game, profile, mesh,
                                     str(handler.filepath), *shown, lod, level)
            for path, versioned in shader_paths(strands.catalog).items():
                found = resolve(versioned)
                if found is None:
                    raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                        "MaterialPreview", "The game's shader file {path} was not found."), {"path": path})
                files[path] = found[1]
        except MaterialPreviewError as exc:
            return None, str(exc)
        master = strands.source.master
        refused = "" if master is None else gather_master(master, resolve, masters)
        return (None, refused) if refused else (strands, "")

    def _gather_material(self, handler, surface, masters: dict[str, tuple[str, bytes]] | None,
                         files: dict[str, bytes | ResourceLocation], streaming: bool, cache: dict) -> str:
        path = surface.texture_path
        if path:
            found = MeshMaterialResolver.locate_texture_path(handler, path, prefer_streaming=streaming)
            if found is None:
                return self.tr("Texture {path} was not found.").format(path=path)
            files[path] = found
        if masters is None:
            return ""
        return gather_master(surface.mmtr_path, self._resolver(handler, cache), masters)

    @staticmethod
    def _resolver(handler, cache: dict) -> Callable[[str], tuple[str, bytes] | None]:
        def resolve(path: str) -> tuple[str, bytes] | None:
            if path not in cache:
                cache[path] = MeshMaterialResolver._resolve_resource(handler, path)
            return cache[path]
        return resolve

    def _gather_drawn(self, handler, surface, resolve: Callable[[str], tuple[str, bytes] | None],
                      masters: dict[str, tuple[str, bytes]], files: dict[str, bytes | ResourceLocation],
                      streaming: bool, cache: dict) -> str:
        reason = gather_master(surface.mmtr_path, resolve, masters)
        if reason:
            return reason
        for texture in surface.textures:
            found = self._located(handler, texture.texture_path, streaming, cache)
            if found is not None:
                files[texture.texture_path] = found
                if "/streaming/" in f"/{found.path}".replace("\\", "/").lower():
                    files[streamed_key(texture.texture_path)] = b""
                    resident = self._located(handler, texture.texture_path, False, cache)
                    if resident is not None:
                        files[resident_key(texture.texture_path)] = ReadOnUse(resident)
        for buffer in surface.gpu_buffers:
            found = self._located(handler, buffer.path, False, cache)
            if found is not None:
                try:
                    files[buffer.path] = GpuBufferLocation.of(found)
                except MaterialPreviewError as exc:
                    return str(exc)
        return ""

    @staticmethod
    def _located(handler, path: str, streaming: bool, cache: dict) -> ResourceLocation | None:
        """Where a file is holds for one gather, whose ``cache`` this is."""
        key = "located", path, streaming
        if key not in cache:
            cache[key] = MeshMaterialResolver.locate_texture_path(handler, path, prefer_streaming=streaming)
        return cache[key]

    def set_active(self, active: bool) -> None:
        if active == self._active:
            return
        self._active = active
        self.active_changed.emit(active)
        if active:
            self.scene_changed()
        else:
            self._stop()
            self._scene = None
            self.status_changed.emit()

    def set_window(self, window: int) -> None:
        """Stops showing before it returns, so the window can be destroyed after."""
        self._window = window or None
        if self._drawer is not None:
            self._drawer.place(self._placed())
        if self._active:
            self._ask()

    def set_shown(self, shown: bool) -> None:
        """A hidden preview releases the window at once: a window takes one swap chain at a time."""
        if shown != self._frames_shown:
            self._frames_shown = shown
            if self._drawer is not None:
                self._drawer.place(self._placed())

    def _placed(self) -> int | None:
        return self._window if self._frames_shown else None

    def read_image(self) -> None:
        frame = self._frame
        if self._thread is None or self._placed() is None or frame is None or not frame.drawn \
                or frame is self._image_of:
            return
        self._image_of = frame
        self._thread.read_back()

    def geometry_changed(self, _preview=None) -> None:
        inspection = self._inspection()
        if self._active and (self._scene is None or inspection is None or self._scene.geometry != inspection.geometry):
            self.scene_changed()

    def scene_changed(self) -> None:
        if not self._active:
            return
        self._failures.clear()
        self._error = ""
        try:
            self._scene = self._gather()
        except MaterialPreviewError as exc:
            self._scene, self._error = None, str(exc)
            self._forget_frame()
        self._told_failures = []
        self._ask()
        self.status_changed.emit()

    def view_changed(self, view, overlays=None) -> None:
        if not isinstance(view, str) and type(view) is not self.view_type:
            view = ""
        reason = view if isinstance(view, str) else ""
        self._view, self._overlays = (None, None) if isinstance(view, str) else (view, overlays)
        if self._active:
            self._ask()
            if reason != self._reason:
                self._reason = reason
                self.status_changed.emit()
        else:
            self._reason = reason

    def status(self) -> list[str]:
        if not self._active:
            return []
        lines = [text for text in (self._error, self._reason) if text]
        if self._frame is None and self._drawable():
            lines.append(self.tr("Drawing…"))
        if self._frame is not None:
            lines += self._frame_lines(self._frame)
        return lines

    def nothing_drawn(self) -> list[str]:
        """Why the preview, on, shows no image: the reasons of the parts shown; none while its first is drawn."""
        if not self._active or self._frame is not None and self._frame.drawn:
            return []
        told = [text for text in (self._error, self._reason) if text]
        if told or self._frame is None:
            return told
        shown = self._frame.view.visible
        return list(dict.fromkeys(text for index, text in self.failures().items() if index in shown))

    def failures(self) -> Mapping[int, str]:
        """Not to be changed: made again only when the frame or the gather changes."""
        if not self._active:
            return {}
        shown = None if self._frame is None else self._frame.failures
        for held, failures in self._told_failures:
            if held is shown or held == shown:
                return failures
        failures = dict(self._failures)
        texts: dict[int, str] = {}
        for index, reason in (shown or {}).items():
            text = texts.get(id(reason))
            if text is None:
                text = texts[id(reason)] = str(reason)
            failures[index] = text
        self._told_failures = [(shown, failures), *self._told_failures[:1]]
        return failures

    def failure_lines(self) -> list[str]:
        return self._failure_lines(self.failures())

    def _failure_lines(self, failures: dict[int, str]) -> list[str]:
        inspection = self._inspection()
        lines = []
        for index, reason in sorted(failures.items()):
            if inspection is not None and index < len(inspection.parts):
                lines.append(self.tr("{part}: not drawn. {reason}").format(
                    part=inspection.parts[index].name or self.tr("Part {index}").format(index=index), reason=reason))
        return lines

    def set_limit(self, limit: int) -> None:
        rate = limit or LIMIT_MOST
        if rate != self._rate:
            self._rate = rate
            if self._due is not None:
                self._due = None
                self._ticker.stop()
                self._tick()

    def stepped_time(self) -> float | None:
        """In seconds."""
        return None if self._due is None else self._stepped

    def _ask(self) -> None:
        if not self._stepping():
            self._ticker.stop()
            self._due = None
            self._draw()
        elif self._due is None:
            self._tick()

    def _tick(self, answered: bool = False) -> None:
        """A late step is taken at the rate actually reached, as the game measures its rate."""
        if not self._stepping():
            self._due = None
            return
        if self._drawing:
            return
        now = self._clock.nsecsElapsed() / 1e6
        period = 1000.0 / self._rate
        if self._due is None:
            rate, due = self._rate, now
        elif now < self._due:
            self._ticker.start(max(1, math.ceil(self._due - now)))
            return
        elif answered or now >= self._due + period:
            rate, due = min(self._rate, 1000.0 / max(now - self._asked_at, 1e-3)), now
        else:
            rate, due = self._rate, self._due
        self._due, self._asked_at, self.step_rate = due + period, now, rate
        self._stepped += 1 / rate
        self._drawing = self._draw()
        if not self._drawing:
            self._ticker.start(max(1, math.ceil(self._due - now)))

    def _draw(self) -> bool:
        if not self._drawable() or self._window is None:
            return False
        if self._thread is None:
            self._drawer = self._make_drawer()
            self._drawer.place(self._placed())
            thread = RenderThread(self._drawer, lambda request, result: self._finished.emit(thread, request, result),
                                  lambda image: self._read.emit(thread, preview_image(image)))
            self._thread = thread
            thread.start()
        self._thread.ask(self._request())
        return True

    def _forget_frame(self) -> None:
        if self._frame is not None:
            self._frame = None
            self.frame_changed.emit(None)

    def _stop(self) -> None:
        self._ticker.stop()
        self._due, self._drawing = None, False
        if self._thread is not None:
            self._drawer.place(None)
            self._thread.stop()
            self._thread = self._drawer = None
        self._forget_frame()

    def _drawable(self) -> bool:
        view, scene = self._view, self._scene
        return view is not None and scene is not None and view.geometry == scene.geometry

    def _on_read(self, thread: RenderThread, image) -> None:
        if thread is self._thread:
            self.image_read.emit(image)

    def _on_finished(self, thread: RenderThread, _request: tuple, result) -> None:
        if thread is not self._thread or self._offering:
            return
        self._offering = True
        try:
            downloaded = offer_compiler(self._handler, (result,) if isinstance(result, Exception)
                                        else result.failures.values())
        finally:
            self._offering = False
        if downloaded:
            self._stop()
            self.scene_changed()
            return
        stepped, self._drawing = self._drawing, False
        if isinstance(result, Exception):
            self._frame, self._error = None, str(result)
            self.frame_changed.emit(None)
        else:
            self._frame, self._error = result, ""
            self.frame_changed.emit(result if result.drawn else None)
            # Anti-aliasing needs the next frames at once; steps request them while stepping.
            if getattr(result, "settling", False) and self._active and not self._stepping():
                self._draw()
        self.status_changed.emit()
        if stepped:
            self._tick(answered=True)


__all__ = ["DevicePreview", "gather_master"]
