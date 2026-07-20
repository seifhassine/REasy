from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QStyle

from file_handlers.mesh.mesh_handler import MeshHandler
from file_handlers.mesh.material_resolver import MeshMaterialResolver
from file_handlers.mesh.mesh_viewer import MeshThumbnailRenderer
from file_handlers.tex.qt_image_utils import build_tex_preview_upload, decode_tex_bytes_to_qimage, parse_tex_bytes
from file_handlers.tex.texture_quality import choose_texture_mip, texture_quality_profile
from utils.app_paths import application_root


THUMBNAIL_SIZE = 160
SOUND_EXTENSIONS = {"spck", "sbnk", "pck", "bnk"}
THUMBNAIL_EXTENSIONS = SOUND_EXTENSIONS | {"mesh", "tex"}


def thumbnail_cache_directory() -> Path:
	return application_root() / ".cache" / "pak_thumbnails"


def resource_extension(path: str) -> str:
	"""Return the meaningful extension from version-suffixed RE files."""
	parts = path.rsplit("/", 1)[-1].lower().split(".")
	for extension in reversed(parts[1:]):
		if extension in THUMBNAIL_EXTENSIONS:
			return extension
	if len(parts) > 2 and parts[-1].isdigit():
		return parts[-2]
	return parts[-1] if len(parts) > 1 else ""


def is_streaming_mesh(path: str) -> bool:
	parts = path.replace("\\", "/").lower().strip("/").split("/")
	return "streaming" in parts and resource_extension(path) == "mesh"


def _streaming_mesh_icon() -> QIcon:
	pixmap = QPixmap(THUMBNAIL_SIZE, THUMBNAIL_SIZE)
	pixmap.fill(Qt.transparent)
	painter = QPainter(pixmap)
	painter.setRenderHint(QPainter.Antialiasing)
	painter.setBrush(QColor(38, 44, 52))
	painter.setPen(QPen(QColor(74, 144, 226), 3))
	painter.drawRoundedRect(8, 8, THUMBNAIL_SIZE - 16, THUMBNAIL_SIZE - 16, 10, 10)
	font = painter.font()
	font.setBold(True)
	font.setPixelSize(15)
	painter.setFont(font)
	painter.setPen(QColor(220, 232, 245))
	painter.drawText(pixmap.rect(), Qt.AlignCenter, "STREAMING\nMESH")
	painter.end()
	return QIcon(pixmap)


@dataclass(frozen=True, slots=True)
class PakIconEntry:
	label: str
	path: str
	is_dir: bool = False


@dataclass(slots=True)
class MeshThumbnailScene:
	mesh: object
	profiles: dict[str, object]
	images: dict[str, tuple[str, object]]


class PakIconModel(QAbstractListModel):
	PathRole = Qt.UserRole + 32
	IsDirRole = Qt.UserRole + 33
	_BATCH_SIZE = 500

	def __init__(self, thumbnails, parent=None):
		super().__init__(parent)
		self._entries: list[PakIconEntry] = []
		self._visible_count = 0
		self._rows: dict[str, int] = {}
		self._thumbnails = thumbnails
		thumbnails.ready.connect(self._thumbnail_ready)

	def set_entries(self, entries: list[PakIconEntry]):
		self.beginResetModel()
		self._entries = entries
		self._visible_count = min(len(entries), self._BATCH_SIZE)
		self._rows = {
			entry.path: row for row, entry in enumerate(entries[:self._visible_count])
		}
		self.endResetModel()

	def rowCount(self, parent=QModelIndex()):
		return 0 if parent.isValid() else self._visible_count

	def canFetchMore(self, parent=QModelIndex()):
		return not parent.isValid() and self._visible_count < len(self._entries)

	def fetchMore(self, parent=QModelIndex()):
		if parent.isValid() or not self.canFetchMore(parent):
			return
		start = self._visible_count
		end = min(len(self._entries), start + self._BATCH_SIZE)
		self.beginInsertRows(QModelIndex(), start, end - 1)
		self._visible_count = end
		self._rows.update(
			(self._entries[row].path, row) for row in range(start, end)
		)
		self.endInsertRows()

	def data(self, index, role=Qt.DisplayRole):
		if not index.isValid() or not 0 <= index.row() < len(self._entries):
			return None
		entry = self._entries[index.row()]
		if role == Qt.DisplayRole:
			return entry.label
		if role == Qt.ToolTipRole:
			return entry.path
		if role == Qt.DecorationRole:
			return self._thumbnails.icon(entry.path, entry.is_dir)
		if role == self.PathRole:
			return entry.path
		if role == self.IsDirRole:
			return entry.is_dir
		return None

	def _thumbnail_ready(self, path: str):
		row = self._rows.get(path)
		if row is not None:
			idx = self.index(row)
			self.dataChanged.emit(idx, idx)


class _ThumbnailSignals(QObject):
	ready = Signal(str, int, object)


class _CachePruneTask(QRunnable):
	def __init__(self, directory: Path, max_bytes=512 * 1024 * 1024):
		super().__init__()
		self.directory, self.max_bytes = directory, max_bytes

	def run(self):
		try:
			files = []
			for path in self.directory.rglob("*.png"):
				stat = path.stat()
				files.append((stat.st_mtime, stat.st_size, path))
			total = sum(size for _mtime, size, _path in files)
			if total <= self.max_bytes:
				return
			for _mtime, size, path in sorted(files):
				if total <= self.max_bytes * .8:
					break
				path.unlink(missing_ok=True)
				total -= size
		except OSError as exc:
			print(f"PAK thumbnail cache cleanup failed: {exc}")


class _ThumbnailTask(QRunnable):
	def __init__(
		self, path, reader, cache_path, generation, texture_quality,
		resource_cache, upload_cache, signals,
	):
		super().__init__()
		self.path, self.reader, self.cache_path = path, reader, cache_path
		self.generation, self.texture_quality, self.signals = generation, texture_quality, signals
		self.resource_cache, self.upload_cache = resource_cache, upload_cache

	def run(self):
		result = QImage()
		try:
			if getattr(self.reader, "_cache", None) is None:
				self.reader.cache_entries(assign_paths=True)
			stream = self.reader.get_file(self.path)
			data = stream.read() if stream else b""
			ext = resource_extension(self.path)
			if ext == "tex":
				decoded = decode_tex_bytes_to_qimage(data)
				result = decoded if decoded is not None else QImage()
			elif ext == "mesh":
				result = _prepare_mesh_scene(
					data, self.path, self.reader, self.texture_quality,
					self.resource_cache, self.upload_cache,
				)
			if isinstance(result, QImage) and not result.isNull():
				result = result.scaled(
					THUMBNAIL_SIZE, THUMBNAIL_SIZE,
					Qt.KeepAspectRatio, Qt.SmoothTransformation,
				)
				self.cache_path.parent.mkdir(parents=True, exist_ok=True)
				result.save(str(self.cache_path), "PNG")
		except Exception as exc:
			print(f"PAK thumbnail failed for {self.path}: {exc}")
		self.signals.ready.emit(self.path, self.generation, result)


class PakThumbnailProvider(QObject):
	ready = Signal(str)

	def __init__(self, cache_dir: Path, settings=None, parent=None):
		super().__init__(parent)
		self._cache_dir = cache_dir
		self._settings = settings if isinstance(settings, dict) else {}
		self._reader = None
		self._source_reader = None
		self._signature = ""
		self._generation = 0
		self._icons: dict[str, QIcon] = {}
		self._pending: set[str] = set()
		self._failed: set[str] = set()
		self._resource_cache = OrderedDict()
		self._upload_cache = OrderedDict()
		self._signals = _ThumbnailSignals()
		self._signals.ready.connect(self._on_ready)
		self._pool = QThreadPool(self)
		self._pool.setMaxThreadCount(1)  # CachedPakReader and mesh dependencies share one index.
		QThreadPool.globalInstance().start(_CachePruneTask(cache_dir))
		self._mesh_renderer = MeshThumbnailRenderer(
			self._settings, THUMBNAIL_SIZE, None, self
		)
		self._mesh_renderer.rendered.connect(self._on_mesh_rendered)
		style = QApplication.style()
		self._folder = style.standardIcon(QStyle.SP_DirIcon)
		self._file = style.standardIcon(QStyle.SP_FileIcon)
		self._sound = style.standardIcon(QStyle.SP_MediaVolume)
		self._streaming_mesh = _streaming_mesh_icon()
		placeholder = QPixmap(THUMBNAIL_SIZE, THUMBNAIL_SIZE)
		placeholder.fill(Qt.transparent)
		self._preview_placeholder = QIcon(placeholder)

	def set_source(self, reader, pak_paths: list[str], known_paths=()):
		digest = hashlib.sha256()
		for name in pak_paths:
			try:
				stat = Path(name).stat()
				digest.update(f"{Path(name).resolve()}:{stat.st_size}:{stat.st_mtime_ns}".encode())
			except OSError:
				digest.update(name.encode())
		keys = (
			"renderer_texture_quality", "mesh_viewer_use_vertex_colors",
			"mesh_viewer_wireframe_mode", "mesh_viewer_lighting_mode",
			"mesh_viewer_ambient", "mesh_viewer_diffuse",
		)
		digest.update(repr(tuple(self._settings.get(key) for key in keys)).encode())
		signature = digest.hexdigest()
		if reader is self._source_reader and signature == self._signature:
			return
		thumbnail_reader = type(reader)() if reader is not None else None
		if thumbnail_reader is not None:
			thumbnail_reader.pak_file_priority = list(pak_paths)
			thumbnail_reader.add_files(*known_paths)
		self._source_reader, self._reader, self._signature = reader, thumbnail_reader, signature
		self._generation += 1
		self._pool.clear()
		self._mesh_renderer.cancel()
		self._icons.clear()
		self._pending.clear()
		self._failed.clear()
		self._resource_cache = OrderedDict()
		self._upload_cache = OrderedDict()

	def icon(self, path: str, is_dir: bool) -> QIcon:
		if is_dir:
			return self._folder
		ext = resource_extension(path)
		if is_streaming_mesh(path):
			return self._streaming_mesh
		if ext in SOUND_EXTENSIONS:
			return self._sound
		fallback = self._preview_placeholder if ext in {"mesh", "tex"} else self._file
		return self._icons.get(path, fallback)

	def request(self, path: str):
		if is_streaming_mesh(path):
			return
		if resource_extension(path) not in {"mesh", "tex"} or self._reader is None:
			return
		if path in self._icons or path in self._pending or path in self._failed:
			return
		cache_path = self._cache_path(path)
		if cache_path.is_file():
			self._icons[path] = QIcon(str(cache_path))
			self.ready.emit(path)
			return
		self._pending.add(path)
		self._pool.start(
			_ThumbnailTask(
				path, self._reader, cache_path, self._generation,
				self._settings.get("renderer_texture_quality", "balanced"),
				self._resource_cache, self._upload_cache, self._signals,
			)
		)

	def cancel_pending(self):
		self._generation += 1
		self._pool.clear()
		self._mesh_renderer.cancel()
		self._pending.clear()

	def close(self):
		self.cancel_pending()
		self._mesh_renderer.close()

	def _cache_path(self, path: str) -> Path:
		key = hashlib.sha256(f"gl-v3:{self._signature}:{path.lower()}".encode()).hexdigest()
		return self._cache_dir / key[:2] / f"{key}.png"

	def _on_ready(self, path: str, generation: int, result):
		if generation != self._generation:
			return
		if isinstance(result, MeshThumbnailScene):
			self._mesh_renderer.enqueue(
				(path, generation), result.mesh, result.profiles, result.images
			)
			return
		self._finish(path, result)

	def _on_mesh_rendered(self, token, image: QImage):
		path, generation = token
		if generation != self._generation:
			return
		if not image.isNull():
			cache_path = self._cache_path(path)
			cache_path.parent.mkdir(parents=True, exist_ok=True)
			image.save(str(cache_path), "PNG")
		self._finish(path, image)

	def _finish(self, path: str, image: QImage):
		self._pending.discard(path)
		if not image.isNull():
			self._icons[path] = QIcon(QPixmap.fromImage(image))
		else:
			self._failed.add(path)
		self.ready.emit(path)

def _add_thumbnail_upload(images, binding, quality, profile, upload_cache):
	if not binding.resolved_texture_data or not binding.resolved_texture_path:
		return
	try:
		key = quality, binding.resolved_texture_path
		upload = upload_cache.get(key) if upload_cache is not None else None
		if upload is None:
			tex = parse_tex_bytes(binding.resolved_texture_data, raise_errors=True)
			upload = build_tex_preview_upload(
				tex, mip_selector=lambda parsed: choose_texture_mip(parsed, profile)
			)
			if upload_cache is not None:
				upload_cache[key] = upload
		images[binding.mesh_material_name] = (binding.resolved_texture_path, upload)
	except Exception as exc:
		print(f"PAK thumbnail texture failed for {binding.resolved_texture_path}: {exc}")


def _prepare_mesh_scene(
	data: bytes, path: str, reader, quality: str,
	resource_cache=None, upload_cache=None,
) -> MeshThumbnailScene:
	handler = MeshHandler()
	handler.filepath = path
	handler._resource_context = (None, None, "natives/stm", reader)
	handler.read(data)
	profile = texture_quality_profile(quality)
	_mdf, bindings = MeshMaterialResolver.resolve_for_handler(
		handler, prefer_streaming=profile.prefer_streaming, resolve_textures=True,
		resource_cache=resource_cache,
	)
	profiles = {
		binding.mesh_material_name: binding.surface
		for binding in bindings if binding.surface is not None
	}
	images = {}
	for binding in bindings:
		_add_thumbnail_upload(images, binding, quality, profile, upload_cache)
	for cache in (resource_cache, upload_cache):
		while cache is not None and len(cache) > 32:
			cache.popitem(last=False)
	return MeshThumbnailScene(handler.mesh, profiles, images)
