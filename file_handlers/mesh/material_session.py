from __future__ import annotations

from collections.abc import MutableMapping
from typing import Protocol

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from file_handlers.bounded_cache import BoundedCache
from file_handlers.tex.qt_image_utils import decode_parsed_tex_to_qimage_with_buffer, parse_tex_bytes
from file_handlers.tex.texture_quality import (
    DEFAULT_TEXTURE_QUALITY,
    choose_texture_mip,
    normalize_texture_quality,
    texture_quality_profile,
)

from .material_resolver import MeshMaterialBinding, MeshMaterialResolver, ResolvedMdf


_MISSING = object()
_RESOURCE_CACHE_ENTRIES = 512
_RESOURCE_CACHE_BYTES = 64 * 1024 * 1024


class _ScopedMaterialCache(MutableMapping):
    def __init__(self, cache: BoundedCache, scope: tuple):
        self._cache, self._scope = cache, scope

    def __getitem__(self, key):
        value = self._cache.get((self._scope, key), _MISSING)
        if value is _MISSING:
            raise KeyError(key)
        return value

    def __setitem__(self, key, value):
        self._cache.put((self._scope, key), value, len(value[1]) if value is not None else 0,
                        _RESOURCE_CACHE_ENTRIES, _RESOURCE_CACHE_BYTES)

    def __delitem__(self, key):
        if self._cache.pop((self._scope, key), _MISSING) is _MISSING:
            raise KeyError(key)

    def __iter__(self):
        return (key for scope, key in self._cache.entries if scope == self._scope)

    def __len__(self):
        return sum(1 for _key in self)


def scoped_material_key(scope: str, material_name: str) -> str:
    return f"{scope}:{material_name or '<default>'}" if scope else material_name


class MaterialViewport(Protocol):
    texture_quality: str

    def materials_changed(self) -> None: ...


class MeshMaterialSession(QObject):
    reset = Signal()

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

        self._resolved_texture_cache = (
            resource_cache if resource_cache is not None else {}
        )
        self._owns_resource_cache = resource_cache is None
        self._collection: MeshMaterialCollection | None = None
        self._cache_scope = None
        self._resolved_once = False
        self._previews: dict[str, tuple[QImage, bytes] | None] = {}

    def material_key(self, material_name: str) -> str:
        return scoped_material_key(self.material_scope, material_name)

    def reload(self) -> None:
        if (
            self._resolved_once
            and self._collection is not None
            and not self._collection._batching
        ):
            self._collection._clear_caches()
        if self._owns_resource_cache and self._collection is None:
            self._resolved_texture_cache.clear()
        self._previews.clear()
        self._refresh_cache_scope()
        self._resolved_once = True
        self.resolved_mdf, self.bindings = MeshMaterialResolver.resolve_for_handler(
            self.handler,
            explicit_mdf_path=self.explicit_mdf_path,
            parse_in_subprocess=self.parse_in_subprocess,
        )
        self.profiles = {
            self.material_key(binding.mesh_material_name): binding.surface
            for binding in self.bindings
            if binding.surface is not None
        }
        self.reset.emit()

    def set_texture_quality(self, quality: str) -> None:
        quality = normalize_texture_quality(quality)
        if quality == self.texture_quality:
            return
        self.texture_quality = quality
        self.reload()

    def resolve_texture(self, binding: MeshMaterialBinding) -> None:
        if self._refresh_cache_scope():
            for each in self.bindings:
                each.resolved_texture_path, each.resolved_texture_data = "", None
        if binding.resolved_texture_path or not binding.texture_path:
            return
        resolved = MeshMaterialResolver.resolve_texture_path(
            self.handler,
            binding.texture_path,
            prefer_streaming=texture_quality_profile(self.texture_quality).prefer_streaming,
            resource_cache=self._resolved_texture_cache,
        )
        binding.resolved_texture_path, binding.resolved_texture_data = resolved or ("", None)
        binding.status = "Resolved" if resolved else "Texture not found"

    def preview_image(self, binding: MeshMaterialBinding) -> QImage | None:
        self.resolve_texture(binding)
        path = binding.resolved_texture_path
        if not path:
            return None
        if path not in self._previews:
            texture = parse_tex_bytes(binding.resolved_texture_data) if binding.resolved_texture_data else None
            self._previews[path] = None if texture is None else decode_parsed_tex_to_qimage_with_buffer(
                texture, mip_selector=lambda each: choose_texture_mip(each, self.texture_quality))
        preview = self._previews[path]
        return None if preview is None else preview[0]

    def _refresh_cache_scope(self) -> bool:
        if self._collection is None:
            return False
        scope = MeshMaterialResolver.resource_cache_scope(self.handler)
        scope = (scope, self.resource_scope) if scope is not None else (id(self),)
        token = self._collection._cache_generation, scope
        if token == self._cache_scope:
            return False
        self._cache_scope = token
        if self._owns_resource_cache:
            self._resolved_texture_cache = _ScopedMaterialCache(
                self._collection._resource_cache, scope
            )
        self._previews.clear()
        return True


class MeshMaterialCollection(QObject):
    """Combine collision-safe material sessions for one shared viewport."""

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
        self._reset_callbacks: dict[str, object] = {}
        self._batching = False
        self._profile_owners: dict[str, str] = {}
        self._resource_cache = BoundedCache()
        self._cache_generation = 0

    @property
    def sessions(self) -> tuple[MeshMaterialSession, ...]:
        return tuple(self._sessions.values())

    def __contains__(self, key: object) -> bool:
        return key in self._sessions

    @staticmethod
    def prefetch(handler, explicit_mdf_path: str = "") -> None:
        MeshMaterialResolver.prefetch_mdf(handler, explicit_mdf_path=explicit_mdf_path)

    def owner(self, material: str) -> MeshMaterialSession | None:
        key = self._profile_owners.get(material)
        return None if key is None else self._sessions[key]

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

        def reset_callback(item=key):
            self._on_reset(item)

        self._reset_callbacks[key] = reset_callback
        session._collection = self
        session.reset.connect(reset_callback)
        try:
            if start:
                session.reload()
            else:
                self._on_reset(key)
        except Exception:
            self.remove(key)
            raise

    def remove(self, key: str) -> None:
        session = self._sessions.pop(key, None)
        if session is None:
            return
        self._enabled.discard(key)
        session._collection = None
        session.reset.disconnect(self._reset_callbacks.pop(key))
        session.deleteLater()
        self.refresh()

    def clear(self) -> None:
        self._batching = True
        try:
            for key in tuple(self._sessions):
                self.remove(key)
        finally:
            self._batching = False
        self._clear_caches()
        self.refresh()

    def _clear_caches(self) -> None:
        self._resource_cache.clear()
        self._cache_generation += 1

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
        self._clear_caches()
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
        owners = {}
        for key, session in self._sessions.items():
            if key in self._enabled:
                for name in session.profiles:
                    if owners.setdefault(name, key) != key:
                        raise ValueError(f"duplicate viewport material key {name!r}")
        self._profile_owners = owners
        if self.viewport is not None:
            self.viewport.materials_changed()

    def _on_reset(self, key: str) -> None:
        if key in self._enabled:
            self.refresh()
