from __future__ import annotations

import time
from collections import deque
from collections.abc import MutableMapping
from dataclasses import dataclass
from typing import Callable, Protocol

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QImage

from file_handlers.tex.qt_image_utils import (
    TexPreviewUpload,
    build_tex_preview_upload,
    decode_parsed_tex_to_qimage_with_buffer,
    parse_tex_bytes,
)
from file_handlers.tex.texture_quality import (
    DEFAULT_TEXTURE_QUALITY,
    choose_texture_mip,
    normalize_texture_quality,
    texture_quality_profile,
)

from .material_effects import (
    material_texture_key,
    riglogic_material_effect,
    surface_texture_paths,
)
from .material_resolver import MeshMaterialBinding, MeshMaterialResolver, ResolvedMdf


def scoped_material_key(scope: str, material_name: str) -> str:
    return f"{scope}:{material_name or '<default>'}" if scope else material_name


@dataclass(slots=True)
class _TextureRequest:
    texture_path: str
    image_key: str
    source_binding: MeshMaterialBinding | None = None
    material_key: str = ""
    required_for_effect: bool = False
    resolved_path: str = ""
    resolved_data: bytes | None = None

    def update_binding(self, status: str) -> None:
        if self.source_binding is None:
            return
        self.source_binding.resolved_texture_path = self.resolved_path
        self.source_binding.resolved_texture_data = self.resolved_data
        self.source_binding.status = status


class MaterialViewport(Protocol):
    texture_quality: str

    def set_material_profiles(self, profiles: dict[str, object]) -> None: ...

    def set_material_images(
        self,
        images: dict[str, tuple[str, TexPreviewUpload]],
    ) -> None: ...

    def update_material_images(
        self,
        images: dict[str, tuple[str, TexPreviewUpload]],
    ) -> None: ...

    def set_material_parameters(
        self,
        parameters: dict[str, dict[str, float]],
    ) -> None: ...

    def set_material_failures(self, failures: dict[str, str]) -> None: ...


class MeshMaterialSession(QObject):
    """Resolve and prepare one mesh's materials independently of any viewer."""

    reset = Signal()
    images_updated = Signal(object)
    status_changed = Signal()

    def __init__(
        self,
        handler,
        *,
        explicit_mdf_path: str = "",
        material_scope: str = "",
        resource_scope: str | None = None,
        texture_quality: str = DEFAULT_TEXTURE_QUALITY,
        parse_in_subprocess: bool = True,
        resource_cache: MutableMapping[
            tuple[bool, str], tuple[str, bytes] | None
        ] | None = None,
        upload_cache: MutableMapping[
            tuple[str, str], TexPreviewUpload | None
        ] | None = None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.handler = handler
        self.explicit_mdf_path = explicit_mdf_path
        self.material_scope = material_scope
        self.resource_scope = (
            str(getattr(handler, "filepath", "") or "")
            if resource_scope is None
            else resource_scope
        )
        self.texture_quality = normalize_texture_quality(texture_quality)
        self.parse_in_subprocess = parse_in_subprocess
        self.resolved_mdf: ResolvedMdf | None = None
        self.bindings: list[MeshMaterialBinding] = []
        self.profiles: dict[str, object] = {}
        self.images: dict[str, tuple[str, TexPreviewUpload]] = {}
        self.errors: dict[str, str] = {}

        self._resolved_texture_cache = (
            resource_cache if resource_cache is not None else {}
        )
        self._shared_upload_cache = upload_cache
        self._texture_cache: dict[str, TexPreviewUpload | None] = {}
        self._preview_cache: dict[str, QImage | None] = {}
        self._preview_buffers: dict[str, bytes] = {}
        self._parsed_texture_cache: dict[str, object | None] = {}
        self._queue: deque[_TextureRequest] = deque()
        self._primary_requests: dict[int, _TextureRequest] = {}
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._warm_step)

    @property
    def loading(self) -> bool:
        return bool(self._queue) or self._timer.isActive()

    def material_key(self, material_name: str) -> str:
        return scoped_material_key(self.material_scope, material_name)

    def start(self) -> None:
        self.reload()

    def reload(self) -> None:
        self._resolve()
        if self._queue:
            self._timer.start(0)

    def prepare_all(self) -> None:
        """Resolve and prepare every material synchronously without a Qt event loop."""
        self._resolve()
        loaded = self._prepare_pending(None)
        if loaded:
            self.images.update(loaded)
            self.images_updated.emit(loaded)
        self.status_changed.emit()

    def _resolve(self) -> None:
        self._timer.stop()
        self._queue.clear()
        self._texture_cache.clear()
        self._preview_cache.clear()
        self._preview_buffers.clear()
        self._parsed_texture_cache.clear()
        self.images.clear()
        self.errors.clear()
        self._primary_requests.clear()

        quality = texture_quality_profile(self.texture_quality)
        self.resolved_mdf, self.bindings = MeshMaterialResolver.resolve_for_handler(
            self.handler,
            explicit_mdf_path=self.explicit_mdf_path,
            prefer_streaming=quality.prefer_streaming,
            resolve_textures=False,
            parse_in_subprocess=self.parse_in_subprocess,
            resource_cache=self._resolved_texture_cache,
        )
        self.profiles = {
            self.material_key(binding.mesh_material_name): binding.surface
            for binding in self.bindings
            if binding.surface is not None
        }
        for binding in self.bindings:
            material_key = self.material_key(binding.mesh_material_name)
            try:
                effect = riglogic_material_effect(binding.surface)
            except ValueError as exc:
                self.errors[material_key] = f"{material_key}: {exc}"
                continue
            if effect is None:
                if not binding.texture_path:
                    continue
                primary = _TextureRequest(
                    binding.texture_path,
                    material_key,
                    source_binding=binding,
                    resolved_path=binding.resolved_texture_path,
                    resolved_data=binding.resolved_texture_data,
                )
                self._primary_requests[id(binding)] = primary
                self._queue.append(primary)
                continue

            texture_paths = surface_texture_paths(binding.surface)
            for texture_type in effect.texture_types:
                image_key = material_texture_key(material_key, texture_type)
                request = _TextureRequest(
                    texture_paths[texture_type],
                    image_key,
                    source_binding=(
                        binding if image_key == material_key else None
                    ),
                    material_key=material_key,
                    required_for_effect=True,
                )
                if request.source_binding is not None:
                    self._primary_requests[id(binding)] = request
                self._queue.append(request)
        self.reset.emit()
        self.status_changed.emit()

    def set_texture_quality(self, quality: str) -> None:
        quality = normalize_texture_quality(quality)
        if quality == self.texture_quality:
            return
        self.texture_quality = quality
        self.reload()

    def close(self) -> None:
        self._timer.stop()
        self._queue.clear()

    def ensure_texture(
        self,
        binding: MeshMaterialBinding,
    ) -> TexPreviewUpload | None:
        request = self._primary_requests.get(id(binding))
        if request is None:
            return None
        texture = self._load_texture(request)
        if texture is not None and request.resolved_path:
            value = (self._texture_source(request.resolved_path), texture)
            current = self.images.get(request.image_key)
            if (
                current is None
                or current[0] != value[0]
                or current[1] is not texture
            ):
                self.images[request.image_key] = value
                self.images_updated.emit({request.image_key: value})
        self.status_changed.emit()
        return texture

    def preview_image(self, binding: MeshMaterialBinding) -> QImage | None:
        self.ensure_texture(binding)
        request = self._primary_requests.get(id(binding))
        if request is None or not request.resolved_path:
            return None
        path = request.resolved_path
        if path in self._preview_cache:
            return self._preview_cache[path]
        parsed = self._parse_texture(path, request.resolved_data)
        decoded = (
            decode_parsed_tex_to_qimage_with_buffer(
                parsed,
                mip_selector=self._choose_mip,
            )
            if parsed is not None
            else None
        )
        if decoded is None:
            image = None
        else:
            image, buffer = decoded
            self._preview_buffers[path] = buffer
        self._preview_cache[path] = image
        return image

    def _warm_step(self) -> None:
        deadline = time.perf_counter() + 0.02
        loaded = self._prepare_pending(deadline)
        if loaded:
            self.images.update(loaded)
            self.images_updated.emit(loaded)
        self.status_changed.emit()
        if self._queue:
            self._timer.start(0)

    def _prepare_pending(
        self,
        deadline: float | None,
    ) -> dict[str, tuple[str, TexPreviewUpload]]:
        loaded: dict[str, tuple[str, TexPreviewUpload]] = {}
        processed = 0
        while self._queue and (
            deadline is None
            or processed == 0
            or time.perf_counter() < deadline
        ):
            processed += 1
            request = self._queue.popleft()
            texture = self._load_texture(request)
            if texture is None or not request.resolved_path:
                continue
            loaded[request.image_key] = (
                self._texture_source(request.resolved_path),
                texture,
            )
        return loaded

    def _load_texture(
        self,
        request: _TextureRequest,
    ) -> TexPreviewUpload | None:
        quality = texture_quality_profile(self.texture_quality)
        if not request.resolved_path:
            resolved = MeshMaterialResolver.resolve_texture_path(
                self.handler,
                request.texture_path,
                prefer_streaming=quality.prefer_streaming,
                resource_cache=self._resolved_texture_cache,
            )
            if resolved is None:
                request.update_binding("Texture not found")
                self._record_required_texture_error(
                    request,
                    "was not found",
                )
                return None
            request.resolved_path, request.resolved_data = resolved
            request.update_binding("Resolved")

        path = request.resolved_path
        if path in self._texture_cache:
            return self._texture_cache[path]
        shared_key = (self.texture_quality, self._texture_source(path))
        if (
            self._shared_upload_cache is not None
            and shared_key in self._shared_upload_cache
        ):
            upload = self._shared_upload_cache[shared_key]
            self._texture_cache[path] = upload
            if upload is None:
                self._record_required_texture_error(
                    request,
                    "could not be prepared",
                )
            return upload
        try:
            texture = self._parse_texture(
                path,
                request.resolved_data,
                raise_errors=True,
            )
            upload = build_tex_preview_upload(texture, mip_selector=self._choose_mip)
        except Exception as exc:
            self._record_required_texture_error(
                request,
                f"could not be prepared: {exc}",
            )
            if not request.required_for_effect:
                print(f"Texture preparation failed: path={path!r}: {exc}")
            upload = None
        else:
            if upload is None:
                self._record_required_texture_error(
                    request,
                    "could not be prepared",
                )
        self._texture_cache[path] = upload
        if self._shared_upload_cache is not None:
            self._shared_upload_cache[shared_key] = upload
        return upload

    def _record_required_texture_error(
        self,
        request: _TextureRequest,
        detail: str,
    ) -> None:
        if not request.required_for_effect:
            return
        self.errors[request.material_key] = (
            f"{request.material_key}: required RigLogic texture "
            f"{request.texture_path!r} {detail}"
        )

    def _parse_texture(
        self,
        path: str,
        data: bytes | None,
        *,
        raise_errors: bool = False,
    ):
        if path not in self._parsed_texture_cache:
            self._parsed_texture_cache[path] = (
                parse_tex_bytes(data, raise_errors=raise_errors) if data else None
            )
        return self._parsed_texture_cache[path]

    def _choose_mip(self, texture) -> int:
        return choose_texture_mip(texture, self.texture_quality)

    def _texture_source(self, path: str) -> str:
        return f"{self.resource_scope}|{path}" if self.resource_scope else path


class MeshMaterialCollection(QObject):
    """Combine collision-safe material sessions for one shared viewport."""

    changed = Signal()

    def __init__(
        self,
        viewport: MaterialViewport | None,
        *,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.viewport = viewport
        self.texture_quality = normalize_texture_quality(
            viewport.texture_quality
            if viewport is not None
            else DEFAULT_TEXTURE_QUALITY
        )
        self._sessions: dict[str, MeshMaterialSession] = {}
        self._enabled: set[str] = set()
        self._image_callbacks: dict[str, Callable[[object], None]] = {}
        self._status_callbacks: dict[str, Callable[[], None]] = {}
        self._parameter_values: dict[str, dict[str, float]] = {}
        self._batching = False

    @property
    def sessions(self) -> tuple[MeshMaterialSession, ...]:
        return tuple(self._sessions.values())

    def __contains__(self, key: object) -> bool:
        return key in self._sessions

    @property
    def loading(self) -> bool:
        return any(
            session.loading
            for key, session in self._sessions.items()
            if key in self._enabled
        )

    def add(
        self,
        key: str,
        session: MeshMaterialSession,
        *,
        enabled: bool = True,
        start: bool = True,
    ) -> None:
        if key in self._sessions:
            raise ValueError(f"duplicate mesh material session {key!r}")
        self._sessions[key] = session
        if enabled:
            self._enabled.add(key)

        def image_callback(images, item=key):
            self._on_images(item, images)

        def status_callback():
            self._sync_failures()
            self.changed.emit()

        self._image_callbacks[key] = image_callback
        self._status_callbacks[key] = status_callback
        session.reset.connect(self.refresh)
        session.images_updated.connect(image_callback)
        session.status_changed.connect(status_callback)
        if start:
            session.start()
        else:
            self.refresh()

    def remove(self, key: str) -> None:
        session = self._sessions.pop(key, None)
        if session is None:
            return
        self._enabled.discard(key)
        session.close()
        session.reset.disconnect(self.refresh)
        callback = self._image_callbacks.pop(key)
        session.images_updated.disconnect(callback)
        status_callback = self._status_callbacks.pop(key)
        session.status_changed.disconnect(status_callback)
        session.deleteLater()
        self.refresh()

    def clear(self) -> None:
        self._batching = True
        try:
            for key in tuple(self._sessions):
                self.remove(key)
        finally:
            self._batching = False
        self.set_parameter_values({})
        self.refresh()

    def set_parameter_values(
        self,
        values: dict[str, dict[str, float]],
    ) -> None:
        normalized = {
            material: dict(parameters)
            for material, parameters in values.items()
            if parameters
        }
        if normalized == self._parameter_values:
            return
        self._parameter_values = normalized
        if self.viewport is not None:
            self.viewport.set_material_parameters(normalized)

    def set_enabled(self, key: str, enabled: bool) -> None:
        if key not in self._sessions:
            raise KeyError(key)
        before = key in self._enabled
        if enabled:
            self._enabled.add(key)
        else:
            self._enabled.discard(key)
        if before != enabled:
            self.refresh()

    def set_texture_quality(self, quality: str) -> None:
        quality = normalize_texture_quality(quality)
        if quality == self.texture_quality:
            return
        self.texture_quality = quality
        self._batching = True
        try:
            for session in self._sessions.values():
                session.set_texture_quality(quality)
        finally:
            self._batching = False
        self.refresh()

    def refresh(self) -> None:
        if self._batching:
            return
        profiles = self._merged("profiles")
        images = self._merged("images")
        failures = self._merged("errors")
        if self.viewport is not None:
            self.viewport.set_material_profiles(profiles)
            self.viewport.set_material_images(images)
            self.viewport.set_material_failures(failures)
            self.viewport.set_material_parameters(self._parameter_values)
        self.changed.emit()

    def _on_images(
        self,
        key: str,
        images: dict[str, tuple[str, TexPreviewUpload]],
    ) -> None:
        if self._batching or key not in self._enabled:
            return
        if self.viewport is not None:
            self.viewport.update_material_images(images)
        self.changed.emit()

    def _sync_failures(self) -> None:
        if self.viewport is not None and not self._batching:
            self.viewport.set_material_failures(self._merged("errors"))

    def _merged(self, attribute: str) -> dict:
        merged = {}
        for key, session in self._sessions.items():
            if key not in self._enabled:
                continue
            values = getattr(session, attribute)
            overlap = merged.keys() & values.keys()
            if overlap:
                duplicate = next(iter(overlap))
                raise ValueError(f"duplicate viewport material key {duplicate!r}")
            merged.update(values)
        return merged
