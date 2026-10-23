from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import (
	QAbstractListModel, QCoreApplication, QModelIndex, QObject, QRectF, QRunnable, Qt, QThreadPool, Signal,
)
lazy from PySide6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPainterPath, QPixmap

lazy from file_handlers.mesh.mesh_handler import MeshHandler
lazy from file_handlers.mesh.material_session import MeshMaterialSession
lazy from file_handlers.mesh.lit_preview import thumbnail
lazy from file_handlers.tex.qt_image_utils import decode_tex_bytes_to_qimage
lazy from ui.icons import line_pixmap
lazy from ui.styles import current_colors
lazy from utils.app_paths import application_root
lazy from utils.resource_file_utils import ResourceResolutionContext


THUMBNAIL_SIZE = 160
MESH_BACKGROUND = (26, 26, 26)
SOUND_EXTENSIONS = {"spck", "sbnk", "pck", "bnk"}
THUMBNAIL_EXTENSIONS = SOUND_EXTENSIONS | {"mesh", "tex"}
_TILE_FAMILIES = {
	"mesh": ("cube", "#3fb6d6"),
	"tex": ("image", "#d6604f"),
	"material": ("palette", "#5fbf5f"),
	"scene": ("layers", "#e09a3a"),
	"prefab": ("gameobject", "#e09a3a"),
	"data": ("variable", "#b07fd6"),
	"sound": ("sound", "#5f8ad9"),
	"text": ("text", "#a0a3aa"),
	"file": ("file", None),
}
_TILE_FAMILY_BY_EXTENSION = {
	"mesh": "mesh", "tex": "tex", "mdf2": "material", "scn": "scene", "pfb": "prefab",
	"user": "data", "msg": "text", **{extension: "sound" for extension in SOUND_EXTENSIONS},
}


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


def _tile_family(path: str) -> str:
	return _TILE_FAMILY_BY_EXTENSION.get(resource_extension(path), "file")


def _asset_tile(family: str, image: QImage | None = None, caption: str = "") -> QIcon:
	colors = current_colors()
	icon_name, strip = _TILE_FAMILIES[family]
	size = THUMBNAIL_SIZE
	pixmap = QPixmap(size, size)
	pixmap.fill(Qt.transparent)
	painter = QPainter(pixmap)
	painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
	card = QRectF(6, 6, size - 12, size - 12)
	shape = QPainterPath()
	shape.addRoundedRect(card, 7, 7)
	painter.fillPath(shape, QColor(colors["surface"]))
	painter.setClipPath(shape)
	if image is not None and not image.isNull():
		fitted = image.scaled(card.size().toSize(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
		painter.drawImage(
			QRectF(card.center().x() - fitted.width() / 2, card.center().y() - fitted.height() / 2,
				fitted.width(), fitted.height()),
			fitted,
		)
	else:
		glyph = 56
		top = card.center().y() - glyph / 2 - (10 if caption else 4)
		painter.drawPixmap(
			int(card.center().x() - glyph / 2), int(top), line_pixmap(icon_name, colors["text_subtle"], glyph)
		)
		if caption:
			font = QFont(painter.font())
			font.setPixelSize(17)
			font.setWeight(QFont.DemiBold)
			painter.setFont(font)
			painter.setPen(QColor(colors["text_muted"]))
			painter.drawText(
				QRectF(card.left(), top + glyph + 6, card.width(), 26), Qt.AlignHCenter | Qt.AlignTop, caption
			)
	if strip:
		painter.fillRect(QRectF(card.left(), card.bottom() - 7, card.width(), 7), QColor(strip))
	painter.end()
	return QIcon(pixmap)


def _folder_tile() -> QIcon:
	colors = current_colors()
	pixmap = QPixmap(THUMBNAIL_SIZE, THUMBNAIL_SIZE)
	pixmap.fill(Qt.transparent)
	painter = QPainter(pixmap)
	painter.setRenderHint(QPainter.Antialiasing)
	back = QPainterPath()
	back.addRoundedRect(QRectF(20, 36, 120, 94), 7, 7)
	tab = QPainterPath()
	tab.addRoundedRect(QRectF(20, 28, 54, 20), 6, 6)
	painter.fillPath(back.united(tab), QColor(colors["surface_active"]))
	front = QPainterPath()
	front.addRoundedRect(QRectF(20, 52, 120, 78), 7, 7)
	painter.fillPath(front, QColor(colors["text_subtle"]))
	painter.end()
	return QIcon(pixmap)


@dataclass(frozen=True, slots=True)
class PakIconEntry:
	label: str
	path: str
	is_dir: bool = False


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
		resource_context, resource_cache, passes, signals,
	):
		super().__init__()
		self.path, self.reader, self.cache_path = path, reader, cache_path
		self.generation, self.texture_quality, self.signals = generation, texture_quality, signals
		self.resource_context = resource_context
		self.resource_cache, self.passes = resource_cache, passes

	def run(self):
		result = QImage()
		try:
			if not self.reader.cache_ready:
				self.reader.cache_entries(assign_paths=True)
			stream = self.reader.get_file(self.path)
			data = stream.read() if stream else b""
			ext = resource_extension(self.path)
			if ext == "tex":
				decoded = decode_tex_bytes_to_qimage(data)
				result = decoded if decoded is not None else QImage()
			elif ext == "mesh":
				result = _mesh_thumbnail(
					data, self.path, self.texture_quality,
					self.resource_context, self.resource_cache, self.passes,
				)
			if not result.isNull():
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
		self._resource_context = None
		self._signature = ""
		self._generation = 0
		self._icons: dict[str, QIcon] = {}
		self._pending: set[str] = set()
		self._failed: set[str] = set()
		self._resource_cache = OrderedDict()
		# The game's light pass files the thumbnails read, by what they draw.
		self._passes: dict = {}
		self._signals = _ThumbnailSignals()
		self._signals.ready.connect(self._on_ready)
		self._pool = QThreadPool(self)
		self._pool.setMaxThreadCount(1)  # CachedPakReader and mesh dependencies share one index.
		QThreadPool.globalInstance().start(_CachePruneTask(cache_dir))
		self._folder = _folder_tile()
		self._type_tiles: dict[str, QIcon] = {}
		self._streaming_mesh = _asset_tile(
			"mesh", caption=QCoreApplication.translate("PakThumbnailProvider", "Streaming")
		)

	def set_source(
		self,
		reader,
		pak_paths: list[str],
		known_paths=(),
		resource_context: ResourceResolutionContext | None = None,
	):
		digest = hashlib.sha256()
		for name in pak_paths:
			try:
				stat = Path(name).stat()
				digest.update(f"{Path(name).resolve()}:{stat.st_size}:{stat.st_mtime_ns}".encode())
			except OSError:
				digest.update(name.encode())
		if resource_context is not None:
			digest.update(repr((
				resource_context.project_dir,
				resource_context.unpacked_dir,
				resource_context.path_prefix,
				resource_context.game,
			)).encode())
		digest.update(repr((self._settings.get("renderer_texture_quality"),)).encode())
		signature = digest.hexdigest()
		if reader is self._source_reader and signature == self._signature:
			return
		# A ready index is shared rather than built again; its names are only ever added to.
		thumbnail_reader = None if reader is None else reader.snapshot() if reader.cache_ready else reader.fork()
		if thumbnail_reader is not None:
			thumbnail_reader.add_files(*known_paths)
		resource_context = resource_context or ResourceResolutionContext()
		self._resource_context = resource_context.with_pak_reader(thumbnail_reader)
		self._source_reader, self._reader, self._signature = reader, thumbnail_reader, signature
		self._generation += 1
		self._pool.clear()
		self._icons.clear()
		self._pending.clear()
		self._failed.clear()
		self._resource_cache = OrderedDict()
		self._passes = {}

	def icon(self, path: str, is_dir: bool) -> QIcon:
		if is_dir:
			return self._folder
		if is_streaming_mesh(path):
			return self._streaming_mesh
		icon = self._icons.get(path)
		if icon is not None:
			return icon
		family = _tile_family(path)
		tile = self._type_tiles.get(family)
		if tile is None:
			tile = self._type_tiles[family] = _asset_tile(family)
		return tile

	def request(self, path: str):
		if is_streaming_mesh(path):
			return
		if resource_extension(path) not in {"mesh", "tex"} or self._reader is None:
			return
		if path in self._icons or path in self._pending or path in self._failed:
			return
		cache_path = self._cache_path(path)
		if cache_path.is_file():
			self._icons[path] = _asset_tile(_tile_family(path), QImage(str(cache_path)))
			self.ready.emit(path)
			return
		self._pending.add(path)
		self._pool.start(
			_ThumbnailTask(
				path, self._reader, cache_path, self._generation,
				self._settings.get("renderer_texture_quality", "balanced"), self._resource_context,
				self._resource_cache, self._passes, self._signals,
			)
		)

	def cancel_pending(self):
		self._generation += 1
		self._pool.clear()
		self._pending.clear()

	def _cache_path(self, path: str) -> Path:
		key = hashlib.sha256(f"device-v1:{self._signature}:{path.lower()}".encode()).hexdigest()
		return self._cache_dir / key[:2] / f"{key}.png"

	def _on_ready(self, path: str, generation: int, result):
		if generation != self._generation:
			return
		self._pending.discard(path)
		if not result.isNull():
			self._icons[path] = _asset_tile(_tile_family(path), result)
		else:
			self._failed.add(path)
		self.ready.emit(path)


def _mesh_thumbnail(
	data: bytes, path: str, quality: str,
	resource_context: ResourceResolutionContext, resource_cache, passes: dict,
) -> QImage:
	handler = MeshHandler.from_bytes(
		path,
		data,
		resource_context=resource_context,
	)
	materials = MeshMaterialSession(
		handler,
		resource_scope="",
		texture_quality=quality,
		parse_in_subprocess=False,
		resource_cache=resource_cache,
	)
	materials.reload()
	while len(resource_cache) > 32:
		resource_cache.popitem(last=False)
	pixels = thumbnail(handler, materials, THUMBNAIL_SIZE, MESH_BACKGROUND, passes)
	if pixels is None:
		return QImage()
	return QImage(
		pixels.tobytes(), THUMBNAIL_SIZE, THUMBNAIL_SIZE, 4 * THUMBNAIL_SIZE, QImage.Format.Format_RGBA8888,
	).copy()
