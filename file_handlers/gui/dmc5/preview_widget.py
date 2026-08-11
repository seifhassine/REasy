"""Fast, editable, runtime-evaluated GUI preview."""

from __future__ import annotations

import math
import struct
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import lru_cache

import numpy as np
from PySide6.QtCore import QByteArray, QElapsedTimer, QObject, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QGradient,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QRadialGradient,
    QTransform,
)
from PySide6.QtWidgets import QWidget

from file_handlers.tex.qt_image_utils import decode_parsed_tex_to_qimage_with_buffer

from ..dependencies import GuiDependencyCatalog
from ..errors import GuiSceneError
from ..model import GuiAnimation, GuiNineSlice, GuiSymbol, GuiTextureSet
from ..native_math import f32 as _preview_f32
from ..native_math import fadd as _pfadd
from ..native_math import fdiv as _pfdiv
from ..native_math import fmul as _pfmul
from ..native_math import fsub as _pfsub
from ..scene import GuiSceneNode
from .adapter import DMC5_DEFAULT_LANGUAGE, DMC5_DEFAULT_SAFE_AREA_RATIO
from .controllers import Dmc5GuiControllerContext
from .icon_tags import (
    DMC5_MENU_DECIDE_VK,
    Dmc5IconInputContext,
    resolve_dmc5_icon_tag,
)
from .playback import DMC5_SEGMENT_VALUES, Dmc5GuiPlayback
from .scene import Dmc5GuiScene, project_gui_point
from .text import (
    TextOpcode,
    compile_dmc5_markup,
    find_dmc5_wrap_break,
    ruby_layout_plan,
)
from .text_tags import resolve_dmc5_text_tag


_CONTAINERS = {
    "via.gui.View", "via.gui.Panel", "via.gui.SelectItem", "via.gui.SimpleList",
    "via.gui.ScrollBar", "via.gui.ScrollList", "via.gui.ScrollGrid", "via.gui.Window",
}
_DMC5_SELECT_BAR_MMTR = "ui/mastermaterial/guimesh_selectbar.mmtr"
_FLOAT32_EPSILON = float(np.finfo(np.float32).eps)
# DMC5 action indices consumed by the three SimpleList InputType listeners.
# The physical virtual keys remain external and come from keyboard_bindings.
_DMC5_LIST_INPUT_ACTIONS = {
    1: (8, 9),   # UpDown: ACT_DirU / ACT_DirD
    2: (10, 11), # LeftRight: ACT_DirL / ACT_DirR
    3: (16, 4),  # LBRB: ACT_LB / ACT_RB
}
_TEXTURE_ASSET_TYPES = {"UVSequence": 0, "Texture": 1}
_SAMPLER_TYPES = {
    "PointWrap": 0, "PointClamp": 1, "BilinearWrap": 4, "BilinearClamp": 5,
}
_BLUR_TYPES = {"Instant": 0, "System": 1}
_REGION_FIT_TYPES = {"None": 0, "Horizontal": 1, "Vertical": 2, "Both": 3}
_PAGE_ALIGNMENTS = {
    "LeftTop": 0, "CenterTop": 1, "RightTop": 2,
    "LeftCenter": 4, "CenterCenter": 5, "RightCenter": 6,
    "LeftBottom": 8, "CenterBottom": 9, "RightBottom": 10,
}
_FONT_SLOTS = {f"Slot{index}": index for index in range(10)}
_ICON_COLOR_TYPES = {"None": 0, "AlphaOnly": 1, "RGBA": 2}
_HIT_AREA_SHAPES = {"Triangle": 0, "Rect": 1, "Hexagon": 2, "Octagon": 3}
_COLOR_TYPES = {"Fill": 0, "Vertical": 1, "Horizontal": 2, "EachVertex": 3}
_CIRCLE_COLOR_TYPES = {"Fill": 0, "InOut": 1}
_MASK_MODES = {
    "Keep": 0, "Default": 1, "Reverse": 2, "Disable": 3, "ApplyToParent": 4,
}
_VIEW_TYPES = {"Screen": 0, "World": 1}
_REPROJECTION_TYPES = {"Default": 0, "WithOverlay": 1}
_SEGMENTS = DMC5_SEGMENT_VALUES
_SELECT_BAR_PARAMETERS = frozenset({
    "Alpha", "AspectUV_U", "AspectUV_V", "BarScale", "BaseColor",
    "BreakIntensity", "BreakLightIntensity", "BreakPos", "BreakSmooth",
    "BreakVector", "ColorIntensity", "CutAngle", "CutSmooth", "FixingLight",
    "LightColor", "LightColorBreak", "LightDepth", "LightDirect",
    "LightIntensity", "LightSize", "LightSmooth", "MaskMax", "MaskMin",
    "MaskSmooth", "MeshScale", "NormalBlend", "Offset", "TwistIntensity",
})


@dataclass(frozen=True, slots=True)
class _RubyPart:
    base: str
    reading: str
    ratio: float


class _PreviewAssets(QObject):
    """Load one dependency per event-loop turn and retain decoded GPU-ready data."""

    changed = Signal()

    def __init__(self, catalog: GuiDependencyCatalog | None) -> None:
        super().__init__()
        self.catalog = catalog
        self.errors: set[str] = set()
        self._uv: dict[tuple, QPixmap | None] = {}
        self._direct: dict[tuple, QPixmap | None] = {}
        self._images = {}
        self._textures: dict[str, QImage | None] = {}
        self._materials = {}
        self._meshes = {}
        self._icons: dict[str, tuple[QPixmap, float, float] | None] = {}
        self._icon_glyphs = {}
        self._fonts: dict[tuple[int, int], tuple[tuple[str, ...], float] | None] = {}
        self._messages_requested = False
        self._jobs: deque[Callable[[], None]] = deque()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._run_job)

    def _schedule(self, job: Callable[[], None]) -> None:
        self._jobs.append(job)
        if not self._timer.isActive():
            self._timer.start(0)

    def _run_job(self) -> None:
        if not self._jobs:
            return
        try:
            self._jobs.popleft()()
        except Exception as exc:  # Dependency errors belong in the preview, not a modal.
            self.errors.add(str(exc))
        if self.catalog is not None:
            self.errors.update(self.catalog.errors)
        self.changed.emit()
        if self._jobs:
            self._timer.start(0)

    def _image(self, reference: str):
        key = reference.casefold()
        if key not in self._images:
            if self.catalog is None:
                raise FileNotFoundError("GUI project/PAK resource loader is unavailable")
            _source, texture = self.catalog.assets.load_tex(reference)
            decoded = decode_parsed_tex_to_qimage_with_buffer(texture)
            if decoded is None:
                raise ValueError(f"could not decode {reference}")
            self._images[key] = decoded[0].copy()
        return self._images[key]

    def uv(self, reference: str, sequence: int, pattern: int) -> QPixmap | None:
        key = reference.casefold(), int(sequence), int(pattern)
        if key not in self._uv:
            self._uv[key] = None

            def load() -> None:
                if self.catalog is None:
                    raise FileNotFoundError(f"unresolved UV sequence {reference}")
                resolved = self.catalog.assets.resolve_uv(reference, sequence, pattern)
                image = self._image(resolved.texture_reference)
                left, top, right, bottom = resolved.uv_bounds
                x0, x1 = sorted((round(left * image.width()), round(right * image.width())))
                y0, y1 = sorted((round(top * image.height()), round(bottom * image.height())))
                rect = QRect(x0, y0, x1 - x0, y1 - y0).intersected(image.rect())
                if rect.isEmpty():
                    raise ValueError(f"{reference} [{sequence}:{pattern}] has empty UV bounds")
                cropped = image.copy(rect)
                if resolved.mirrored_x or resolved.mirrored_y:
                    cropped = cropped.mirrored(resolved.mirrored_x, resolved.mirrored_y)
                self._uv[key] = QPixmap.fromImage(cropped)

            self._schedule(load)
        return self._uv[key]

    def direct(self, reference: str, source: tuple[int, int, int, int]) -> QPixmap | None:
        key = reference.casefold(), source
        if key not in self._direct:
            self._direct[key] = None

            def load() -> None:
                image = self._image(reference)
                u0, v0, u1, v1 = source
                left, right = sorted((u0, u1))
                top, bottom = sorted((v0, v1))
                rect = QRect(left, top, right - left, bottom - top).intersected(image.rect())
                if rect.isEmpty():
                    raise ValueError(f"{reference} has an empty direct-texture source rectangle")
                cropped = image.copy(rect)
                cropped = cropped.mirrored(u1 < u0, v1 < v0)
                self._direct[key] = QPixmap.fromImage(cropped)

            self._schedule(load)
        return self._direct[key]

    def texture(self, reference: str) -> QImage | None:
        """Load a complete material texture without blocking a paint event."""

        key = reference.casefold()
        if key not in self._textures:
            self._textures[key] = None

            def load() -> None:
                self._textures[key] = self._image(reference)

            self._schedule(load)
        return self._textures[key]

    def material(self, reference: str):
        key = reference.casefold()
        if key not in self._materials:
            self._materials[key] = None

            def load() -> None:
                if self.catalog is None:
                    raise FileNotFoundError(f"unresolved MDF2 resource {reference}")
                _source, material = self.catalog.assets.load_mdf(reference)
                self._materials[key] = material

            self._schedule(load)
        return self._materials[key]

    def mesh(self, reference: str):
        key = reference.casefold()
        if key not in self._meshes:
            self._meshes[key] = None

            def load() -> None:
                if self.catalog is None:
                    raise FileNotFoundError(f"unresolved MESH resource {reference}")
                _source, mesh = self.catalog.assets.load_mesh(reference)
                self._meshes[key] = mesh

            self._schedule(load)
        return self._meshes[key]

    def icon(self, name: str) -> tuple[QPixmap, float, float] | None:
        if name not in self._icons:
            self._icons[name] = None

            def load() -> None:
                catalog = self.catalog.icon_catalog if self.catalog is not None else None
                if catalog is None:
                    raise FileNotFoundError("configured GUI icon font is unavailable")
                glyph = catalog.resolve(name)
                if glyph is None or glyph.uv_rect is None or not glyph.texture_path:
                    raise FileNotFoundError(f"icon font has no glyph named {name!r}")
                self._icon_glyphs[name] = glyph
                image = self._image(glyph.texture_path)
                left, top, right, bottom = glyph.uv_rect
                rect = QRect(
                    round(left * image.width()),
                    round(top * image.height()),
                    round((right - left) * image.width()),
                    round((bottom - top) * image.height()),
                ).intersected(image.rect())
                if rect.isEmpty():
                    raise ValueError(f"icon {name!r} has empty UV bounds")
                self._icons[name] = (
                    QPixmap.fromImage(image.copy(rect)),
                    float(glyph.width),
                    float(glyph.height),
                )

            self._schedule(load)
        return self._icons[name]

    def icon_glyph(self, name: str):
        self.icon(name)
        return self._icon_glyphs.get(name)

    def message(self, message_id: object, language: int) -> str:
        if self.catalog is None or not message_id:
            return ""
        if not self.catalog.messages_loaded and not self._messages_requested:
            self._messages_requested = True
            self._schedule(self.catalog.load_messages)
        return self.catalog.cached_message(str(message_id), language)

    def named_message(self, name: str, language: int) -> str:
        if self.catalog is None:
            return ""
        if not self.catalog.messages_loaded and not self._messages_requested:
            self._messages_requested = True
            self._schedule(self.catalog.load_messages)
            return ""
        return self.catalog.cached_named_message(name, language)

    def font(self, slot: int, language: int, pixel_size: float) -> QFont | None:
        slot_index = int(slot)
        key = int(language), slot_index
        if key not in self._fonts:
            self._fonts[key] = None

            def load() -> None:
                fonts = self.catalog.font_catalog if self.catalog is not None else None
                if fonts is None:
                    raise FileNotFoundError("configured GUI fonts are unavailable")
                faces = fonts.slot_faces(*key).faces
                if not faces:
                    raise FileNotFoundError(f"GUI font slot {slot} has no face")
                families = []
                for face in faces:
                    font_id = QFontDatabase.addApplicationFontFromData(
                        QByteArray(face.font.data)
                    )
                    loaded = QFontDatabase.applicationFontFamilies(font_id)
                    family = loaded[0] if loaded else face.font.family_name
                    if family and family not in families:
                        families.append(family)
                self._fonts[key] = tuple(families), faces[0].adjustment_scale

            self._schedule(load)
        loaded = self._fonts[key]
        if loaded is None:
            return None
        families, adjustment = loaded
        font = QFont(families[0])
        font.setFamilies(list(families))
        font.setPixelSize(max(1, round(pixel_size * adjustment)))
        return font


class Dmc5GuiCanvas(QWidget):
    node_selected = Signal(object)
    node_moved = Signal(object, float, float)
    frame_changed = Signal(float, float)
    diagnostics_changed = Signal(str)

    def __init__(
        self,
        dependencies: GuiDependencyCatalog | None = None,
        controller_context: Dmc5GuiControllerContext | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setMinimumSize(520, 320)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._scene: Dmc5GuiScene | None = None
        self.playback: Dmc5GuiPlayback | None = None
        self.assets = _PreviewAssets(dependencies)
        self._controller_context = (
            controller_context or Dmc5GuiControllerContext()
        )
        self.assets.changed.connect(self._assets_changed)
        self._preview = False
        self._interaction_enabled = False
        self._guides = False
        self._playing = False
        self._interaction_frames = 0.0
        self._selected: GuiSceneNode | None = None
        self._output_size: tuple[float, float] | None = None
        self._safe_area_ratio = DMC5_DEFAULT_SAFE_AREA_RATIO
        self._language = DMC5_DEFAULT_LANGUAGE
        self._icon_context = Dmc5IconInputContext(keyboard_mode=True)
        self._runtime_overrides: dict[str, dict[str, object]] = {}
        self._runtime_active_by_path: dict[str, bool] = {}
        self._zoom = 1.0
        self._pan = QPointF()
        self._drag_node: GuiSceneNode | None = None
        self._drag_start = QPointF()
        self._drag_origin = (0.0, 0.0)
        self._transient: tuple[GuiSceneNode, float, float] | None = None
        self._panning = False
        self._pan_start = QPointF()
        self._interaction_ready = False
        self._active_list: GuiSceneNode | None = None
        self._list_selection: dict[str, int] = {}
        self._list_items_by_owner: dict[str, tuple[GuiSceneNode, ...]] = {}
        self._vertex_gradients: dict[tuple, QPixmap] = {}
        self._colored_pixmaps: OrderedDict[tuple, QPixmap] = OrderedDict()
        self._material_defaults: dict[tuple[int, int], dict[str, object]] = {}
        self._break_geometries: dict[tuple[int, str], tuple] = {}
        self._texture_arrays: dict[int, np.ndarray] = {}
        self._break_samples: dict[tuple, tuple[np.ndarray, np.ndarray]] = {}
        self._break_static_pixmaps: OrderedDict[tuple, tuple[tuple[float, float], QPixmap]] = OrderedDict()
        self._break_pixmaps: OrderedDict[tuple, tuple[tuple[float, float], QPixmap]] = OrderedDict()
        self._static_boundaries: set[tuple[str, str]] = set()
        self._last_frame_boundaries: set[tuple[str, str]] = set()
        self._frame_boundaries: set[tuple[str, str]] | None = None
        self._diagnostic_pending = False
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self._clock = QElapsedTimer()

    @property
    def semantic_scene(self) -> Dmc5GuiScene:
        assert self._scene is not None
        return self._scene

    def set_document(self, scene: Dmc5GuiScene) -> None:
        self._scene = scene
        self.playback = Dmc5GuiPlayback(scene)
        self._selected = None
        self._output_size = scene.screen_size
        self._interaction_ready = False
        self._active_list = None
        self._list_selection.clear()
        self._list_items_by_owner.clear()
        self._runtime_overrides.clear()
        self._runtime_active_by_path.clear()
        self._static_boundaries.clear()
        self._last_frame_boundaries.clear()
        self._interaction_frames = 0.0
        self._apply_layout()
        self.fit_document()

    def set_controller_context(
        self,
        context: Dmc5GuiControllerContext | None,
    ) -> None:
        self._controller_context = context or Dmc5GuiControllerContext()
        self._emit_diagnostics()
        self.update()

    def set_runtime_properties(
        self,
        values_by_path: dict[str, dict[str, object]] | None,
    ) -> None:
        """Install host/controller values that do not belong to GUIR.

        The mapping is deliberately explicit.  Current save data, menu-controller
        labels, and other game state must not be guessed from the opened file.
        """

        self._runtime_overrides = {
            str(path): dict(values)
            for path, values in (values_by_path or {}).items()
        }
        self._apply_layout()

    def set_runtime_active(
        self,
        values_by_path: dict[str, bool] | None,
    ) -> None:
        """Install native runtime-active flags for hit/input traversal."""

        self._runtime_active_by_path = {
            str(path): bool(value)
            for path, value in (values_by_path or {}).items()
        }
        self.update()

    def set_runtime_playback(
        self,
        values_by_path: dict[str, dict[str, object]] | None,
    ) -> None:
        """Restore captured Control states without modifying authored clips."""

        if self.playback is None:
            return
        self.playback.reset_runtime()
        self.playback.apply_runtime_snapshot({
            str(path): dict(values)
            for path, values in (values_by_path or {}).items()
        })
        self._interaction_frames = 0.0
        self._apply_layout()

    def set_input_context(self, context: Dmc5IconInputContext | None) -> None:
        """Install the runtime input state used by DMC5 ``<ICON>`` aliases."""

        self._icon_context = context or Dmc5IconInputContext()
        self.update()

    def set_preview_enabled(self, enabled: bool) -> None:
        self._preview = bool(enabled)
        if not self._preview:
            self._interaction_enabled = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
        if self._preview and not self._interaction_ready:
            self._initialize_interaction()
        self._sync_timer()
        self._apply_layout()

    def set_interaction_enabled(self, enabled: bool) -> None:
        self._interaction_enabled = bool(enabled) and self._preview
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if self._interaction_enabled:
            self.setFocus(Qt.FocusReason.OtherFocusReason)

    def set_playing(self, enabled: bool) -> None:
        self._playing = bool(enabled)
        self._sync_timer()

    def set_guides(self, enabled: bool) -> None:
        self._guides = bool(enabled)
        self.update()

    def set_output_size(self, width: int, height: int) -> None:
        self._output_size = max(1, int(width)), max(1, int(height))
        self._apply_layout()

    def set_safe_area_ratio(self, ratio: float) -> None:
        """Set the external ``GUISystem.SafeAreaRatio`` preview input."""

        self._safe_area_ratio = min(1.0, max(0.01, float(ratio)))
        self._apply_layout()

    def set_language(self, language: int) -> None:
        self._language = int(language)
        self.update()

    def set_input_device(self, device: int) -> None:
        if device not in (0, 1):
            raise ValueError(f"unknown DMC5 GUI input device index {device}")
        self._icon_context = replace(
            self._icon_context,
            keyboard_mode=device == 0,
        )
        self.update()

    def select_animation(
        self,
        symbol: GuiSymbol | None,
        animation: GuiAnimation | None,
        path: str | None = None,
    ) -> None:
        if self.playback is None:
            return
        self.playback.select_animation(symbol, animation, path)
        self._apply_layout()
        self.frame_changed.emit(self.playback.frame, self.playback.duration)

    def set_frame(self, frame: float) -> None:
        if self.playback is None:
            return
        self.playback.set_selected_frame(frame)
        self._apply_layout()
        self.frame_changed.emit(self.playback.frame, self.playback.duration)

    def restart_animation(self) -> None:
        if self.playback is None:
            return
        self.playback.restart_selected()
        self._apply_layout()
        self.frame_changed.emit(self.playback.frame, self.playback.duration)

    def document_changed(self) -> None:
        if self.playback is not None:
            self.playback.rebuild()
        self._static_boundaries.clear()
        self._last_frame_boundaries.clear()
        self._emit_diagnostics()
        self._apply_layout()

    def update_branch(self, _node: GuiSceneNode) -> None:
        self.document_changed()

    def fit_document(self) -> None:
        self._zoom = 1.0
        self._pan = QPointF()
        self.update()

    def select_node(self, node: GuiSceneNode) -> None:
        self._selected = node
        self.update()

    def _sync_timer(self) -> None:
        active = self._preview and (self._playing or self._interaction_frames > 0.0)
        if active and not self._timer.isActive():
            self._clock.restart()
            self._timer.start()
        elif not active:
            self._timer.stop()

    def _tick(self) -> None:
        if self.playback is None or self._scene is None:
            return
        elapsed = self._clock.restart() / 1000.0
        fps = max(1.0, float(self._scene.root.render_properties.get("BaseFps", 60.0)))
        delta = min(elapsed * fps, fps * 0.25)
        self.playback.advance(delta)
        if self._interaction_frames > 0.0:
            self._interaction_frames = max(0.0, self._interaction_frames - delta)
        self._apply_layout()
        self.frame_changed.emit(self.playback.frame, self.playback.duration)
        self._sync_timer()

    def _apply_layout(self) -> None:
        if self._scene is None:
            return
        overrides = None
        if self._preview:
            overrides = {
                path: dict(values)
                for path, values in self._runtime_overrides.items()
            }
            if self.playback is not None:
                for path, values in self.playback.overrides.items():
                    overrides.setdefault(path, {}).update(values)
        self._scene.update_preview(
            overrides,
            output_size=self._output_size,
            safe_area_ratio=self._safe_area_ratio,
            transient=self._transient,
        )
        self.update()

    def _assets_changed(self) -> None:
        self._emit_diagnostics()
        self.update()

    def _boundary(self, node: GuiSceneNode, reason: str) -> None:
        boundary = node.path, reason
        target = (
            self._frame_boundaries
            if self._frame_boundaries is not None
            else self._static_boundaries
        )
        if boundary in target:
            return
        target.add(boundary)
        if self._frame_boundaries is None and not self._diagnostic_pending:
            self._diagnostic_pending = True
            QTimer.singleShot(0, self._emit_diagnostics)

    def _emit_diagnostics(self) -> None:
        self._diagnostic_pending = False
        reasons: dict[str, int] = {}
        for _path, reason in self._static_boundaries | self._last_frame_boundaries:
            reasons[reason] = reasons.get(reason, 0) + 1
        parts = [
            f"{count}× {reason}"
            for reason, count in sorted(reasons.items())
        ]
        if self.assets.errors:
            parts.append(
                f"{len(self.assets.errors)} unavailable asset dependencies"
            )
        parts.extend(self._controller_context.errors)
        parts.extend(self._controller_context.diagnostics)
        list_path = self._controller_context.active_list_path
        if list_path is not None and self._scene is not None:
            target = self._scene.nodes_by_path.get(list_path)
            if target is None or not self._controller_context.accepts_list(target):
                behavior = self._controller_context.runtime_list_behavior
                target_type = (
                    behavior.list_type.rsplit(".", 1)[-1]
                    if behavior is not None
                    else "runtime"
                )
                parts.append(
                    f"selected preview {target_type} target is unresolved"
                )
        self.diagnostics_changed.emit("; ".join(parts))

    def _stage(self) -> tuple[QRectF, float]:
        width, height = self._scene.viewport_size if self._scene else (1920.0, 1080.0)
        available = QRectF(10.0, 10.0, max(1.0, self.width() - 20.0), max(1.0, self.height() - 20.0))
        scale = min(available.width() / width, available.height() / height) * self._zoom
        size = QPointF(width * scale, height * scale)
        origin = QPointF(
            available.center().x() - size.x() * 0.5,
            available.center().y() - size.y() * 0.5,
        ) + self._pan
        return QRectF(origin, origin + size), scale

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.fillRect(self.rect(), QColor(24, 26, 30))
        stage, scale = self._stage()
        painter.fillRect(stage, QColor(2, 4, 6))
        painter.setPen(QPen(QColor(68, 75, 82), 1.0))
        painter.drawRect(stage)
        if self._scene is None:
            return
        painter.save()
        painter.setClipRect(stage)
        painter.translate(stage.left(), stage.top())
        painter.scale(scale, scale)
        if self._preview:
            self._draw_preview(painter)
        else:
            self._draw_layout(painter)
        self._draw_guides(painter)
        painter.restore()

    def _draw_preview(self, painter: QPainter) -> None:
        current: set[tuple[str, str]] = set()
        self._frame_boundaries = current
        try:
            self._draw_preview_nodes(painter)
        finally:
            self._frame_boundaries = None
            if current != self._last_frame_boundaries:
                self._last_frame_boundaries = current
                if not self._diagnostic_pending:
                    self._diagnostic_pending = True
                    QTimer.singleShot(0, self._emit_diagnostics)

    def _draw_preview_nodes(self, painter: QPainter) -> None:
        assert self._scene is not None
        self._register_scene_boundaries()
        masks: dict[str, dict[int, list[GuiSceneNode]]] = {}
        for node in self._scene.draw_nodes:
            if node is self._scene.root or not self._render_branch_enabled(node):
                continue
            mask_type = _enum(
                node.render_properties.get("MaskType"),
                {"Target": 0, "NonTarget": 1, "Mask": 2, "MaskTop": 3, "MaskTopMost": 4},
                0,
            )
            if mask_type >= 2:
                scope = self._mask_scope(node)
                masks.setdefault(scope, {}).setdefault(mask_type - 2, []).append(node)

        for node in self._scene.draw_nodes:
            if node is self._scene.root or not self._render_branch_enabled(node):
                continue
            if node.object.type_name in _CONTAINERS or node.object.type_name == "via.gui.HitArea":
                continue
            mask_type = _enum(
                node.render_properties.get("MaskType"),
                {"Target": 0, "NonTarget": 1, "Mask": 2, "MaskTop": 3, "MaskTopMost": 4},
                0,
            )
            if mask_type >= 2:
                continue
            painter.save()
            blend = _enum(
                node.render_properties.get("BlendType", "Alpha"),
                {"Alpha": 0, "Add": 1, "Disable": 2},
                0,
            )
            painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_Plus
                if blend == 1
                else QPainter.CompositionMode.CompositionMode_Source
                if blend == 2
                else QPainter.CompositionMode.CompositionMode_SourceOver
            )
            if bool(node.render_properties.get("LinearAlphaBlend", False)):
                self._boundary(node, "native LinearAlphaBlend GPU path")
            applicable = self._applicable_masks(node, masks) if mask_type == 0 else []
            mode = self._mask_mode(node)
            if mode == 3:
                applicable = []
            elif applicable:
                # Hierarchy/type/mode selection is native.  The remaining
                # boundary is Qt's software alpha surface versus DMC5's three
                # full-resolution A8 GPU surfaces.
                self._boundary(node, "native A8 mask-surface raster precision")
            alpha_masks: list[list[GuiSceneNode]] = []
            for producers in applicable:
                vector = QPainterPath()
                alpha = []
                for producer in producers:
                    if producer.object.type_name in {
                        "via.gui.Texture",
                        "via.gui.TextureSet",
                        "via.gui.Scale9Grid",
                        "via.gui.BlurFilter",
                    }:
                        alpha.append(producer)
                    else:
                        vector = vector.united(self._world_path(producer))
                if not vector.isEmpty():
                    if mode == 2:
                        outside = QPainterPath()
                        width, height = self._scene.viewport_size
                        outside.addRect(QRectF(0.0, 0.0, width, height))
                        vector = outside.subtracted(vector)
                    painter.setClipPath(vector, Qt.ClipOperation.IntersectClip)
                if alpha:
                    alpha_masks.append(alpha)
            if alpha_masks:
                self._draw_alpha_masked(painter, node, alpha_masks, reverse=mode == 2)
            else:
                painter.setWorldTransform(QTransform(*node.world_transform), combine=True)
                self._draw_node(painter, node)
            painter.restore()

    def _register_scene_boundaries(self) -> None:
        """Expose engine-owned render context instead of silently guessing it."""

        assert self._scene is not None
        root = self._scene.root
        view = root.render_properties
        view_type = _enum(view.get("ViewType"), _VIEW_TYPES, 0)
        reprojection = _enum(
            view.get("ReprojectionType"), _REPROJECTION_TYPES, 0
        )
        if any(
            node is not root
            and node.object.type_name not in _CONTAINERS
            and node.object.type_name != "via.gui.HitArea"
            for node in self._scene.draw_nodes
        ):
            self._boundary(root, "native D3D11 GUI raster/filter precision")
        if (
            view_type != 0
            or bool(view.get("UseGUICamera", False))
        ):
            self._boundary(root, "external world/GUI camera matrices")
        if (
            not bool(view.get("Overlay", True))
            or bool(view.get("DepthTest", False))
            or bool(view.get("StencilZeroFill", False))
        ):
            self._boundary(root, "native depth/stencil/compositor integration")
        if bool(view.get("Detonemap", False)):
            self._boundary(root, "native detonemap render pass")
        if reprojection != 0:
            self._boundary(root, "native GUI reprojection pass")

        for node in self._scene.nodes:
            properties = node.render_properties
            if _enum(properties.get("Segment"), _SEGMENTS, -1) != -1:
                self._boundary(node, "native multi-segment render routing")
            if bool(properties.get("UseColorScaleSrgb", False)):
                self._boundary(node, "inactive/unknown DMC5 UseColorScaleSrgb branch")
        self._register_interaction_boundaries()

    def _register_interaction_boundaries(self) -> None:
        if not self._interaction_enabled or self._scene is None:
            return
        if not self._icon_context.keyboard_mode:
            self._boundary(self._scene.root, "external gamepad input-event source")
            return
        bindings = self._icon_context.keyboard_bindings
        for owner in self._interactive_lists():
            input_type = _enum(
                owner.render_properties.get("InputType", "None"),
                {"None": 0, "UpDown": 1, "LeftRight": 2, "LBRB": 3},
                0,
            )
            actions = _DMC5_LIST_INPUT_ACTIONS.get(input_type)
            if actions is not None and any(action not in bindings for action in actions):
                self._boundary(owner, "external keyboard bindings for SimpleList input")

    @staticmethod
    def _mask_scope(node: GuiSceneNode) -> str:
        """Return the native target scope, including ApplyToParent transfer."""

        scope = node.parent
        while scope is not None and _enum(
            scope.render_properties.get("MaskMode", "Keep"),
            _MASK_MODES,
            0,
        ) == 4:
            scope = scope.parent
        return scope.path if scope is not None else "/"

    @staticmethod
    def _applicable_masks(
        node: GuiSceneNode,
        masks: dict[str, dict[int, list[GuiSceneNode]]],
    ) -> list[list[GuiSceneNode]]:
        result = []
        for scope, levels in masks.items():
            prefix = "/" if scope == "/" else f"{scope}/"
            if not node.path.startswith(prefix):
                continue
            for level in sorted(levels):
                result.append(levels[level])
        return result

    def _draw_alpha_masked(
        self,
        painter: QPainter,
        node: GuiSceneNode,
        mask_groups: list[list[GuiSceneNode]],
        *,
        reverse: bool,
    ) -> None:
        assert self._scene is not None
        viewport = QRectF(0.0, 0.0, *self._scene.viewport_size)
        bounds = self._world_path(node).boundingRect().adjusted(-12.0, -12.0, 12.0, 12.0)
        bounds = bounds.intersected(viewport)
        if bounds.isEmpty():
            return
        left, top = math.floor(bounds.left()), math.floor(bounds.top())
        right, bottom = math.ceil(bounds.right()), math.ceil(bounds.bottom())
        logical_width, logical_height = max(1, right - left), max(1, bottom - top)
        _stage, stage_scale = self._stage()
        raster_scale = min(1.0, max(0.25, stage_scale))
        width = max(1, math.ceil(logical_width * raster_scale))
        height = max(1, math.ceil(logical_height * raster_scale))
        layer = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        layer.fill(Qt.GlobalColor.transparent)
        target = QPainter(layer)
        target.setRenderHints(painter.renderHints())
        target.scale(raster_scale, raster_scale)
        target.translate(-left, -top)
        target.setWorldTransform(QTransform(*node.world_transform), combine=True)
        self._draw_node(target, node)
        target.end()

        for producers in mask_groups:
            mask = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
            mask.fill(Qt.GlobalColor.transparent)
            mask_painter = QPainter(mask)
            mask_painter.setRenderHints(painter.renderHints())
            mask_painter.scale(raster_scale, raster_scale)
            mask_painter.translate(-left, -top)
            for producer in producers:
                self._draw_mask_node(mask_painter, producer)
            mask_painter.end()
            compositor = QPainter(layer)
            compositor.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_DestinationOut
                if reverse
                else QPainter.CompositionMode.CompositionMode_DestinationIn
            )
            compositor.drawImage(0, 0, mask)
            compositor.end()
        painter.drawImage(
            QRectF(left, top, logical_width, logical_height),
            layer,
            QRectF(layer.rect()),
        )

    def _draw_mask_node(self, painter: QPainter, node: GuiSceneNode) -> None:
        painter.save()
        painter.setWorldTransform(QTransform(*node.world_transform), combine=True)
        if node.object.type_name == "via.gui.Texture":
            properties = node.render_properties
            pixmap = None
            asset_type = _enum(
                properties.get("AssetType"), _TEXTURE_ASSET_TYPES, 0
            )
            if asset_type == 1:
                reference = str(properties.get("Texture", ""))
                if _is_runtime_texture(reference):
                    self._boundary(node, "external runtime/render-target texture")
                    painter.restore()
                    return
                source = tuple(
                    int(properties.get(name, 0)) for name in ("U0", "V0", "U1", "V1")
                )
                if reference:
                    pixmap = self.assets.direct(reference, source)
            else:
                reference = str(properties.get("UVSequence", ""))
                if reference:
                    pixmap = self.assets.uv(
                        reference,
                        int(properties.get("UVSequenceNo", 0)),
                        int(properties.get("UVPatternNo", 0)),
                    )
            if pixmap is not None and not pixmap.isNull():
                pixmap, opacity = self._colored_pixmap(
                    pixmap,
                    node,
                    mirror_x=False,
                    mirror_y=False,
                )
                painter.setOpacity(opacity)
                painter.drawPixmap(self._bounds(node), pixmap, QRectF(pixmap.rect()))
        else:
            self._draw_node(painter, node)
        painter.restore()

    def _draw_node(self, painter: QPainter, node: GuiSceneNode) -> None:
        kind = node.object.type_name
        if kind in _CONTAINERS or kind == "via.gui.HitArea":
            return
        if kind == "via.gui.Rect":
            painter.fillPath(self._local_path(node), self._brush(node))
        elif kind == "via.gui.Circle":
            painter.fillPath(self._circle_path(node), self._circle_brush(node))
        elif kind == "via.gui.Texture":
            self._draw_texture(painter, node, self._bounds(node))
        elif kind == "via.gui.TextureSet":
            self._draw_texture_set(painter, node)
        elif kind == "via.gui.Scale9Grid":
            self._draw_nine_slice(painter, node)
        elif kind == "via.gui.BlurFilter":
            self._draw_blur(painter, node)
        elif kind in {"via.gui.Text", "via.gui.MaterialText"}:
            self._draw_text(painter, node)
        elif kind == "via.gui.Mesh":
            self._draw_mesh(painter, node)
        elif kind == "via.gui.Effect":
            self._draw_effect(painter, node)
        elif kind == "via.gui.Material":
            self._boundary(node, "external MDF/MMTR material shader")
        else:
            self._boundary(node, f"unsupported GUI renderer type {kind}")

    def _draw_texture(self, painter: QPainter, node: GuiSceneNode, bounds: QRectF) -> None:
        properties = node.render_properties
        pixmap = None
        asset_type = _enum(properties.get("AssetType"), _TEXTURE_ASSET_TYPES, 0)
        if asset_type == 1:
            reference = str(properties.get("Texture", ""))
            if _is_runtime_texture(reference):
                self._boundary(node, "external runtime/render-target texture")
                return
            source = tuple(int(properties.get(name, 0)) for name in ("U0", "V0", "U1", "V1"))
            if reference:
                pixmap = self.assets.direct(reference, source)
        else:
            reference = str(properties.get("UVSequence", ""))
            if reference:
                pixmap = self.assets.uv(
                    reference,
                    int(properties.get("UVSequenceNo", 0)),
                    int(properties.get("UVPatternNo", 0)),
                )
        self._draw_pixmap(painter, node, bounds, pixmap)

    def _draw_texture_set(self, painter: QPainter, node: GuiSceneNode) -> None:
        special = node.object.special_data
        if not isinstance(special, GuiTextureSet):
            return
        reference = str(node.render_properties.get("UVSequence", ""))
        scale = _pair(node.render_properties.get("Scale"), (1.0, 1.0))
        for entry in special.entries:
            left, top, right, bottom = entry.bounds
            bounds = QRectF(
                left * scale[0],
                top * scale[1],
                (right - left) * scale[0],
                (bottom - top) * scale[1],
            )
            pixmap = (
                self.assets.uv(reference, entry.sequence, entry.pattern)
                if reference
                else None
            )
            self._draw_pixmap(
                painter,
                node,
                bounds.normalized(),
                pixmap,
                mirror_x=right < left,
                mirror_y=bottom < top,
            )

    def _draw_nine_slice(self, painter: QPainter, node: GuiSceneNode) -> None:
        special = node.object.special_data
        if not isinstance(special, GuiNineSlice):
            return
        outer = self._bounds(node)
        left, top, right, bottom = special.borders
        left, right = min(left, outer.width() * 0.5), min(right, outer.width() * 0.5)
        top, bottom = min(top, outer.height() * 0.5), min(bottom, outer.height() * 0.5)
        xs = (outer.left(), _pfadd(outer.left(), left), _pfsub(outer.right(), right), outer.right())
        ys = (outer.top(), _pfadd(outer.top(), top), _pfsub(outer.bottom(), bottom), outer.bottom())
        reference = str(node.render_properties.get("UVSequence", ""))
        for index, cell in enumerate(special.cells[:9]):
            row, column = divmod(index, 3)
            bounds = QRectF(xs[column], ys[row], xs[column + 1] - xs[column], ys[row + 1] - ys[row])
            pixmap = self.assets.uv(reference, cell.sequence, cell.pattern) if reference else None
            self._draw_pixmap(painter, node, bounds, pixmap)

    def _draw_blur(self, painter: QPainter, node: GuiSceneNode) -> None:
        blur_type = _enum(node.render_properties.get("BlurType"), _BLUR_TYPES, 0)
        reference = str(node.render_properties.get("UVSequence", ""))
        if blur_type == 1:
            self._boundary(node, "native System Blur backbuffer/GPU passes")
        elif reference:
            self._boundary(node, "native Instant Blur UV-sequence shader")
        else:
            self._boundary(node, "native Instant Blur GUI-backbuffer shader")

    def _draw_pixmap(
        self,
        painter: QPainter,
        node: GuiSceneNode,
        bounds: QRectF,
        pixmap: QPixmap | None,
        *,
        mirror_x: bool = False,
        mirror_y: bool = False,
    ) -> None:
        tiling = _pair(node.render_properties.get("TilingSize"), (0.0, 0.0))
        if tiling != (0.0, 0.0):
            self._boundary(node, "native nonzero texture tiling shader")
            return
        if pixmap is not None and not pixmap.isNull():
            pixmap, opacity = self._colored_pixmap(
                pixmap,
                node,
                mirror_x=mirror_x,
                mirror_y=mirror_y,
            )
            painter.save()
            painter.setOpacity(opacity)
            sampler = _enum(
                node.render_properties.get("SamplerType"), _SAMPLER_TYPES, None
            )
            smooth = sampler in {4, 5}
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, smooth)
            painter.drawPixmap(bounds, pixmap, QRectF(pixmap.rect()))
            painter.restore()
            return
        # Loading/missing assets are represented by diagnostics, not invented
        # cyan geometry that could be mistaken for game output.

    def _colored_pixmap(
        self,
        pixmap: QPixmap,
        node: GuiSceneNode,
        *,
        mirror_x: bool,
        mirror_y: bool,
    ) -> tuple[QPixmap, float]:
        colors = self._vertex_colors(node, apply_saturation=False)
        alphas = tuple(color.alpha() for color in colors)
        opacity = alphas[0] / 255.0 if len(set(alphas)) == 1 else 1.0
        if opacity != 1.0:
            colors = tuple(
                QColor(color.red(), color.green(), color.blue(), 255)
                for color in colors
            )
        rgba = tuple(channel for color in colors for channel in color.getRgb())
        saturation = max(0.0, float(node.saturation))
        key = (
            int(pixmap.cacheKey()),
            bool(mirror_x),
            bool(mirror_y),
            rgba,
            round(saturation, 6),
        )
        cached = self._colored_pixmaps.get(key)
        if cached is not None:
            self._colored_pixmaps.move_to_end(key)
            return cached, opacity

        identity = all(color.getRgb() == (255, 255, 255, 255) for color in colors)
        if identity and saturation == 1.0 and not mirror_x and not mirror_y:
            return pixmap, opacity

        image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        if mirror_x or mirror_y:
            image = image.mirrored(mirror_x, mirror_y)
        width, height = image.width(), image.height()
        stride = image.bytesPerLine()
        storage = np.frombuffer(image.bits(), dtype=np.uint8, count=image.sizeInBytes())
        pixels = storage.reshape(height, stride)[:, : width * 4].reshape(height, width, 4)
        corners = np.asarray([color.getRgb() for color in colors], dtype=np.uint8)
        if np.all(corners == corners[0]):
            tint = corners[0]
        else:
            corners = corners.astype(np.float32)
            x = np.linspace(0.0, 1.0, width, dtype=np.float32)[None, :, None]
            y = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None, None]
            top = corners[0] * (1.0 - x) + corners[1] * x
            bottom = corners[2] * (1.0 - x) + corners[3] * x
            tint = top * (1.0 - y) + bottom * y

        # RE Engine samples sRGB textures into linear space and submits its
        # transformed vertex colors as linear RGBA8.  QImage/QPainter do not,
        # so perform the shader-side color math explicitly before encoding the
        # result back to display sRGB.
        if saturation == 1.0:
            table = _srgb_multiply_table()
            tint_bytes = np.asarray(tint[..., :3], dtype=np.uint8)
            if tint_bytes.ndim == 1:
                for channel in range(3):
                    value = int(tint_bytes[channel])
                    if value != 255:
                        pixels[..., channel] = table[pixels[..., channel], value]
            else:
                pixels[..., :3] = table[pixels[..., :3], tint_bytes]
        else:
            source_rgb = _srgb_to_linear_array(
                pixels[..., :3].astype(np.float32) / 255.0
            )
            tint_rgb = _srgb_to_linear_array(
                np.asarray(tint[..., :3], dtype=np.float32) / 255.0
            )
            rgb = source_rgb * tint_rgb
            luminance = (
                rgb[..., 0:1] * 0.2126
                + rgb[..., 1:2] * 0.7152
                + rgb[..., 2:3] * 0.0722
            )
            rgb = luminance + (rgb - luminance) * saturation
            encoded = _linear_to_srgb_array(np.clip(rgb, 0.0, 1.0)) * 255.0
            pixels[..., :3] = np.clip(np.rint(encoded), 0.0, 255.0).astype(np.uint8)
        alpha_tint = np.asarray(tint[..., 3])
        if alpha_tint.ndim == 0:
            value = int(alpha_tint)
            if value != 255:
                product = pixels[..., 3].astype(np.uint16) * value
                pixels[..., 3] = ((product + 127) // 255).astype(np.uint8)
        else:
            alpha = (
                pixels[..., 3].astype(np.float32)
                * alpha_tint.astype(np.float32)
                / 255.0
            )
            pixels[..., 3] = np.clip(np.rint(alpha), 0.0, 255.0).astype(np.uint8)

        result = QPixmap.fromImage(image)
        self._colored_pixmaps[key] = result
        if len(self._colored_pixmaps) > 256:
            self._colored_pixmaps.popitem(last=False)
        return result, opacity

    def _draw_text(self, painter: QPainter, node: GuiSceneNode) -> None:
        properties = node.render_properties
        text = str(properties.get("Message", "") or "")
        if not text:
            text = self.assets.message(properties.get("MessageId"), self._language)
        if not text:
            return
        size = _pair(properties.get("FontSize"), (64.0, 64.0))
        font_slot = _enum(properties.get("FontSlot"), _FONT_SLOTS, 0)
        parts, direction = self._compiled_text_parts(node, text, size, font_slot)
        plain = "".join(
            part
            if isinstance(part, str)
            else part.base
            if isinstance(part, _RubyPart)
            else ""
            for part in parts
        )
        if not plain and not any(not isinstance(part, str) for part in parts):
            return
        bounds = self._bounds(node)
        font = self.assets.font(font_slot, self._language, size[1])
        if font is None:
            return
        font.setStretch(max(1, min(400, round(100.0 * size[0] / max(1.0, size[1])))))
        font.setKerning(bool(properties.get("Kerning", False)))
        font.setStyleStrategy(font.styleStrategy() | QFont.StyleStrategy.NoFontMerging)
        self._boundary(node, "native GUI glyph rasterization/metrics")
        if direction.effective_rtl:
            self._boundary(node, "native RTL shaping and glyph ordering")
        if direction.effective_vertical_layout:
            self._boundary(node, "native vertical glyph substitution/layout")
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, float(properties.get("LetterSpace", 0.0)))
        if direction.effective_vertical_layout:
            plain = "\n".join(plain)
            parts = [plain]
        fit = _enum(properties.get("AutoRegionFit"), _REGION_FIT_TYPES, 0)
        if fit:
            metrics = QFontMetricsF(font)
            lines = plain.splitlines() or [""]
            measured_width = max((metrics.horizontalAdvance(line) for line in lines), default=0.0)
            measured_height = len(lines) * metrics.height() + max(0, len(lines) - 1) * float(
                properties.get("LineSpace", 0.0)
            )
            scales = [1.0]
            if fit & 1 and measured_width > 0.0 and bounds.width() > 0.0:
                scales.append(bounds.width() / measured_width)
            if fit & 2 and measured_height > 0.0 and bounds.height() > 0.0:
                scales.append(bounds.height() / measured_height)
            scale = min(scales)
            if scale < 1.0:
                font.setPixelSize(max(1, round(font.pixelSize() * scale)))
        painter.setFont(font)
        alignment = _enum(properties.get("PageAlignment"), _PAGE_ALIGNMENTS, 0)
        if direction.auto_rtl_alignment_flip:
            alignment = (alignment & ~3) | {0: 2, 1: 1, 2: 0}[alignment & 3]
        flags = _alignment(alignment)
        if bool(properties.get("AutoWrap", False)):
            if any(not isinstance(part, str) for part in parts):
                self._boundary(node, "native rich-text wrapping/layout")
            else:
                plain = _wrap_dmc5_text(
                    plain,
                    QFontMetricsF(font),
                    max(0.0, bounds.width()),
                )
        scissor = properties.get("ScissorRect")
        if (
            isinstance(scissor, (list, tuple))
            and len(scissor) >= 4
            and float(scissor[2]) >= 0.0
            and float(scissor[3]) >= 0.0
        ):
            painter.setClipRect(
                QRectF(*(float(value) for value in scissor[:4])),
                Qt.ClipOperation.IntersectClip,
            )
        if any(not isinstance(part, str) for part in parts):
            if bool(properties.get("ShadowEnable", False)) or bool(
                properties.get("GlowEnable", False)
            ):
                self._boundary(node, "native rich-text glow/shadow passes")
            self._draw_icon_text(
                painter,
                node,
                parts,
                bounds,
                font,
                alignment,
            )
            return
        color = self._color(node)
        if bool(properties.get("ShadowEnable", False)):
            self._boundary(node, "native text shadow blur shader")
            distance = float(properties.get("ShadowDistance", 10.0))
            angle = math.radians(float(properties.get("ShadowRotation", 30.0)))
            painter.setPen(
                self._color(
                    node,
                    _qcolor(properties.get("ShadowColor"), QColor(0, 0, 0, 255)),
                )
            )
            painter.drawText(bounds.translated(math.sin(angle) * distance, math.cos(angle) * distance), flags, plain)
        if bool(properties.get("GlowEnable", False)):
            self._boundary(node, "native text glow blur shader")
            painter.setPen(
                self._color(node, _qcolor(properties.get("GlowColor"), color))
            )
            radius = max(1.0, float(properties.get("GlowStrength", 1.0)))
            for x, y in ((-radius, 0.0), (radius, 0.0), (0.0, -radius), (0.0, radius)):
                painter.drawText(bounds.translated(x, y), flags, plain)
        brush = self._text_brush(node)
        painter.setPen(QPen(QBrush(brush), 1.0))
        painter.drawText(bounds, flags, plain)

    def _compiled_text_parts(
        self,
        node: GuiSceneNode,
        text: str,
        size: tuple[float, float],
        font_slot: int,
    ) -> tuple[list[str | tuple[str, str] | _RubyPart], object]:
        properties = node.render_properties
        missing_runtime_tag = False

        def resolve_icon(name: str):
            resolved = resolve_dmc5_icon_tag(name, self._icon_context)
            if name in {"DECIDE", "CANCEL"} and resolved == name:
                self._boundary(node, f"external input binding for <ICON {name}>")
            return self.assets.icon_glyph(resolved)

        def resolve_runtime_tag(name: str, parameter: str):
            nonlocal missing_runtime_tag
            resolved = resolve_dmc5_text_tag(
                name,
                parameter,
                self._language,
                self.assets.named_message,
                runtime_target=(
                    self.assets.catalog.profile.runtime_target
                    if self.assets.catalog
                    else ""
                ),
            )
            catalog = self.assets.catalog
            if resolved is None and (
                catalog is None or catalog.messages_loaded
            ):
                missing_runtime_tag = True
            return resolved

        program = compile_dmc5_markup(
            text,
            language_index=self._language,
            auto_rtl=bool(properties.get("AutoRTL", True)),
            vertical_layout=bool(properties.get("VerticalLayout", False)),
            font_slot=font_slot,
            size=size,
            ruby_size_ratio=float(properties.get("RubySizeRatio", 0.375)),
            tag_resolver=resolve_runtime_tag,
            icon_resolver=resolve_icon,
        )
        if program.diagnostics:
            self._boundary(node, "malformed or incomplete native text markup")
        if missing_runtime_tag or any(
            item.recursion_suppressed for item in program.unresolved_tags
        ):
            self._boundary(node, "external runtime text-tag resolver")
        # PAGE, line breaks, icons and ruby are consumed below.  These sourced
        # opcodes alter individual glyph runs and need the native span layout;
        # never pretend the node-wide Qt font/color is equivalent.
        run_opcodes = {
            TextOpcode.SIZE,
            TextOpcode.FONT,
            TextOpcode.COLOR,
            TextOpcode.WRAP,
            TextOpcode.CENTER,
            TextOpcode.LEFT,
            TextOpcode.RIGHT,
            TextOpcode.TOP,
            TextOpcode.BOTTOM,
        }
        if any(
            instruction.source_start >= 0 and instruction.opcode in run_opcodes
            for instruction in program.instructions
        ):
            self._boundary(node, "native per-glyph text span layout")
        if bool(properties.get("UseTyping", False)) or bool(
            properties.get("AnimationEnable", False)
        ):
            self._boundary(node, "external text typing/animation runtime")

        wanted_page = max(0, int(properties.get("PageIndex", 0)))
        page = -1
        initial_line = False
        parts: list[str | tuple[str, str] | _RubyPart] = []
        ruby_base: list[str] | None = None
        ruby_reading: list[str] | None = None
        ruby_target: list[str] | None = None
        ruby_ratio = float(properties.get("RubySizeRatio", 0.375))

        def append_text(value: str) -> None:
            if ruby_target is not None:
                ruby_target.append(value)
            elif parts and isinstance(parts[-1], str):
                parts[-1] += value
            else:
                parts.append(value)

        for instruction in program.instructions:
            if instruction.opcode == TextOpcode.PAGE:
                page += 1
                initial_line = True
                continue
            if page != wanted_page:
                continue
            if instruction.opcode == TextOpcode.LINE_BREAK:
                if initial_line:
                    initial_line = False
                else:
                    append_text("\n")
            elif instruction.opcode == TextOpcode.RUBY:
                if not instruction.closing:
                    ruby_base, ruby_reading, ruby_target = [], [], None
                    ruby_ratio = instruction.ruby_ratio or ruby_ratio
                elif ruby_base is not None and ruby_reading is not None:
                    parts.append(
                        _RubyPart("".join(ruby_base), "".join(ruby_reading), ruby_ratio)
                    )
                    ruby_base = ruby_reading = ruby_target = None
            elif instruction.opcode == TextOpcode.RUBY_BASE:
                ruby_target = None if instruction.closing else ruby_base
            elif instruction.opcode == TextOpcode.RUBY_TEXT:
                ruby_target = None if instruction.closing else ruby_reading
            elif instruction.opcode == TextOpcode.GLYPH:
                if instruction.icon_glyph is not None:
                    if ruby_target is not None:
                        ruby_target.append("\uFFFC")
                    else:
                        parts.append(("icon", instruction.icon_glyph.name))
                elif instruction.codepoint is not None:
                    append_text(chr(instruction.codepoint))
        return parts, program.direction

    def _draw_icon_text(
        self,
        painter: QPainter,
        node: GuiSceneNode,
        parts: list[str | tuple[str, str] | _RubyPart],
        bounds: QRectF,
        font: QFont,
        alignment: int,
    ) -> None:
        """Lay out text and IFT glyphs on one baseline without rasterizing text."""

        properties = node.render_properties
        metrics = QFontMetricsF(font)
        icon_height = float(properties.get("IconFontSize", 0) or font.pixelSize())
        lines = _part_lines(parts)
        line_space = float(properties.get("LineSpace", 0.0))
        ruby_ratio = float(properties.get("RubySizeRatio", 0.375))
        ruby_line_space = float(properties.get("RubyLineSpace", 0.0))
        ruby_always = bool(properties.get("RubyHeightAlways", False))
        ruby_font = QFont(font)
        ruby_font.setPixelSize(max(1, round(font.pixelSize() * ruby_ratio)))
        ruby_metrics = QFontMetricsF(ruby_font)
        line_heights = [
            max(metrics.height(), icon_height)
            + (
                ruby_metrics.height() + ruby_line_space
                if ruby_always or any(isinstance(part, _RubyPart) for part in line)
                else 0.0
            )
            for line in lines
        ]
        total_height = sum(line_heights) + max(0, len(lines) - 1) * line_space
        y = bounds.top()
        if alignment >> 2 == 2:
            y = bounds.bottom() - total_height
        elif alignment >> 2 == 1:
            y = bounds.center().y() - total_height * 0.5

        painter.setFont(font)
        painter.setPen(QPen(QBrush(self._text_brush(node)), 1.0))
        for line, line_height in zip(lines, line_heights):
            measured = []
            for part in line:
                if isinstance(part, str):
                    measured.append(metrics.horizontalAdvance(part))
                    continue
                if isinstance(part, _RubyPart):
                    measured.append(metrics.horizontalAdvance(part.base))
                else:
                    final_name = resolve_dmc5_icon_tag(
                        part[1],
                        self._icon_context,
                    )
                    loaded = self.assets.icon(final_name)
                    aspect = loaded[1] / loaded[2] if loaded and loaded[2] else 0.0
                    measured.append(icon_height * aspect)
            width = sum(measured)
            x = bounds.left()
            if alignment & 3 == 2:
                x = bounds.right() - width
            elif alignment & 3 == 1:
                x = bounds.center().x() - width * 0.5
            ruby_height = (
                ruby_metrics.height() + ruby_line_space
                if ruby_always or any(isinstance(part, _RubyPart) for part in line)
                else 0.0
            )
            content_height = line_height - ruby_height
            baseline = y + ruby_height + (content_height - metrics.height()) * 0.5 + metrics.ascent()
            for part, advance in zip(line, measured):
                if isinstance(part, str):
                    painter.drawText(QPointF(x, baseline), part)
                elif isinstance(part, _RubyPart):
                    painter.setFont(font)
                    painter.drawText(QPointF(x, baseline), part.base)
                    advances = [
                        ruby_metrics.horizontalAdvance(character)
                        for character in part.reading
                    ]
                    plan = ruby_layout_plan(advance, advances)
                    ruby_x = x + plan.marker_offset
                    ruby_baseline = (
                        baseline
                        - metrics.ascent()
                        - ruby_line_space
                        - ruby_metrics.descent()
                    )
                    painter.setFont(ruby_font)
                    for character, assigned in zip(
                        part.reading,
                        plan.assigned_advances,
                    ):
                        painter.drawText(QPointF(ruby_x, ruby_baseline), character)
                        ruby_x += assigned
                    painter.setFont(font)
                else:
                    final_name = resolve_dmc5_icon_tag(
                        part[1],
                        self._icon_context,
                    )
                    loaded = self.assets.icon(final_name)
                    if loaded is None:
                        self._boundary(node, f"unresolved IFT icon {final_name!r}")
                    else:
                        pixmap = loaded[0]
                        opacity = 1.0
                        icon_color = _enum(
                            properties.get("IconColorType"), _ICON_COLOR_TYPES, 1
                        )
                        if icon_color == 0:
                            self._boundary(node, "native IconColorType=None branch")
                        elif icon_color == 1:
                            pixmap, opacity = self._colored_pixmap(
                                pixmap,
                                node,
                                mirror_x=False,
                                mirror_y=False,
                            )
                        painter.save()
                        painter.setOpacity(opacity)
                        painter.drawPixmap(
                            QRectF(x, y + (line_height - icon_height) * 0.5, advance, icon_height),
                            pixmap,
                            QRectF(pixmap.rect()),
                        )
                        painter.restore()
                x += advance
            y += line_height + line_space

    def _text_brush(self, node: GuiSceneNode):
        if node.object.type_name != "via.gui.MaterialText":
            return self._brush(node)
        self._boundary(node, "CPU approximation of external MaterialText shader")
        reference = str(node.render_properties.get("Material", ""))
        material = self.assets.material(reference) if reference else None
        parameters = self._material_parameters(node, material)
        inside = parameters.get("Gradient_Color_In")
        outside = parameters.get("Gradient_Color_Out")
        if not (_is_vector(inside, 4) and _is_vector(outside, 4)):
            return self._brush(node)
        intensity = max(0.0, float(parameters.get("Gradient_Intensity", 1.0)))
        inner = _float_color(inside, intensity)
        outer = _float_color(outside, intensity)
        inner = self._color(node, inner)
        outer = self._color(node, outer)
        gradient = QRadialGradient(QPointF(0.5, 0.5), 0.75)
        gradient.setCoordinateMode(QGradient.CoordinateMode.ObjectBoundingMode)
        gradient.setColorAt(0.0, inner)
        gradient.setColorAt(1.0, outer)
        return gradient

    def _material_parameters(
        self,
        node: GuiSceneNode,
        material,
        material_index: int = 0,
    ) -> dict[str, object]:
        if material is None or not 0 <= material_index < len(material.materials):
            return {}
        key = id(material), material_index
        defaults = self._material_defaults.get(key)
        if defaults is None:
            defaults = {
                parameter.name: (
                    parameter.parameter[0]
                    if parameter.component_count == 1
                    else list(parameter.parameter)
                )
                for parameter in material.materials[material_index].parameters
            }
            self._material_defaults[key] = defaults
        values = dict(defaults)
        properties = node.render_properties
        value_fields = {
            "Float": "VariableFloat",
            "Float4": "VariableVec",
            "Color": "VariableColor",
            "Texture": "VariableTexture",
        }
        for index in range(8):
            name = str(properties.get(f"VariableName{index}", ""))
            kind = str(properties.get(f"VariableType{index}", "Unknown"))
            field = value_fields.get(kind)
            target = str(properties.get(f"MaterialName{index}", ""))
            material_name = str(material.materials[material_index].header.mat_name)
            if (
                name
                and field
                and f"{field}{index}" in properties
                and (not target or target == material_name)
            ):
                values[name] = properties[f"{field}{index}"]
        return values

    def _draw_mesh(self, painter: QPainter, node: GuiSceneNode) -> None:
        properties = node.render_properties
        mesh_reference = str(properties.get("Mesh", ""))
        material_reference = str(properties.get("Material", ""))
        mesh = self.assets.mesh(mesh_reference) if mesh_reference else None
        material = self.assets.material(material_reference) if material_reference else None
        if material is None or mesh is None:
            return
        material_names = {
            name for index in range(8)
            if (name := str(properties.get(f"MaterialName{index}", "")))
        }
        materials = [
            (index, item)
            for index, item in enumerate(material.materials)
            if not material_names or item.header.mat_name in material_names
        ]
        if not materials:
            materials = list(enumerate(material.materials))
        if materials and all(
            str(item.header.mmtr_path).replace("\\", "/").casefold()
            == _DMC5_SELECT_BAR_MMTR
            and int(item.header.shader_type) == 9
            for _index, item in materials
        ):
            if self._draw_break_mesh(painter, node, mesh, material, materials):
                self._boundary(
                    node,
                    "CPU emulation of native GUIMesh raster/filter precision",
                )
            return
        shader_names = sorted(
            {
                str(item.header.mmtr_path) or "<missing MMTR>"
                for _index, item in materials
            }
        )
        self._boundary(
            node,
            "external mesh shader " + ", ".join(shader_names[:2]),
        )

    def _break_mesh_geometry(self, mesh, material_name: str):
        """Return the authored triangles and both UV channels for one material layer."""

        key = id(mesh), material_name.casefold()
        if key in self._break_geometries:
            return self._break_geometries[key]
        try:
            material_index = next(
                index for index, name in enumerate(mesh.material_names)
                if str(name).casefold() == key[1]
            )
            buffer = mesh.mesh_buffer
            positions = np.asarray(buffer.positions, dtype=np.float32).reshape(-1, 3)
            uv0 = np.asarray(buffer.uv0, dtype=np.float32).reshape(-1, 2)
            uv1 = np.asarray(buffer.uv1, dtype=np.float32).reshape(-1, 2)
            mesh_data = next(item for item in mesh.meshes if item.lods)
            groups = mesh_data.lods[0].mesh_groups
        except (AttributeError, StopIteration, TypeError, ValueError):
            self._break_geometries[key] = None
            return None

        triangles = []
        seen = set()
        for group in groups:
            for submesh in group.submeshes:
                if submesh.material_index != material_index or submesh.buffer_index != 0:
                    continue
                start = submesh.faces_index_offset
                strip = buffer.faces[start:start + submesh.indices_count]
                base = submesh.verts_index_offset
                for offset in range(0, max(0, len(strip) - 2), 3):
                    indices = tuple(base + int(strip[offset + part]) for part in range(3))
                    identity = tuple(sorted(indices))
                    if len(set(indices)) != 3 or identity in seen:
                        continue
                    if min(indices) < 0 or max(indices) >= len(positions):
                        continue
                    points = positions[list(indices), :2]
                    first = points[1] - points[0]
                    second = points[2] - points[0]
                    if abs(float(first[0] * second[1] - first[1] * second[0])) < 1e-7:
                        continue
                    seen.add(identity)
                    triangles.append(indices)
        result = (
            positions,
            uv0,
            uv1,
            np.asarray(triangles, dtype=np.int32).reshape(-1, 3),
        ) if triangles else None
        self._break_geometries[key] = result
        return result

    @staticmethod
    def _material_texture_reference(node, item, texture_type: str) -> str:
        reference = next(
            (
                str(texture.tex_path) for texture in item.textures
                if str(texture.tex_type).casefold() == texture_type.casefold()
            ),
            "",
        )
        properties = node.render_properties
        for index in range(8):
            if (
                str(properties.get(f"VariableType{index}", "")) == "Texture"
                and str(properties.get(f"VariableName{index}", "")).casefold()
                == texture_type.casefold()
                and str(properties.get(f"MaterialName{index}", item.header.mat_name))
                == str(item.header.mat_name)
            ):
                reference = str(properties.get(f"VariableTexture{index}", reference))
        return reference

    def _texture_array(self, image: QImage) -> np.ndarray:
        key = int(image.cacheKey())
        cached = self._texture_arrays.get(key)
        if cached is not None:
            return cached
        rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
        storage = np.frombuffer(rgba.bits(), dtype=np.uint8, count=rgba.sizeInBytes())
        result = storage.reshape(rgba.height(), rgba.bytesPerLine())[
            :, : rgba.width() * 4
        ].reshape(rgba.height(), rgba.width(), 4).copy()
        self._texture_arrays[key] = result
        return result

    def _break_material_samples(
        self,
        geometry,
        alp_image: QImage,
        normal_image: QImage,
    ) -> tuple[np.ndarray, np.ndarray]:
        key = id(geometry), int(alp_image.cacheKey()), int(normal_image.cacheKey())
        cached = self._break_samples.get(key)
        if cached is not None:
            return cached

        def filtered_pixels(image: QImage) -> np.ndarray:
            longest = max(image.width(), image.height())
            if longest > 512:
                scale = 512.0 / longest
                image = image.scaled(
                    max(1, round(image.width() * scale)),
                    max(1, round(image.height() * scale)),
                    Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            return self._texture_array(image)[..., :3].astype(np.float32) / 255.0

        # The game samples lower mips for this roughly 400-pixel-wide mesh.
        # Keep a prefiltered lookup so animated frames only need one indexed
        # fetch per material texture, with no large temporary float images.
        result = filtered_pixels(alp_image), filtered_pixels(normal_image)
        self._break_samples[key] = result
        return result

    @staticmethod
    def _sample_wrapped_texture(pixels: np.ndarray, uv: np.ndarray) -> np.ndarray:
        """Bilinear-sample a prefiltered material texture with GPU wrap addressing."""

        height, width = pixels.shape[:2]
        coordinates = uv % 1.0
        x = coordinates[:, 0] * width - 0.5
        y = coordinates[:, 1] * height - 0.5
        x0 = np.floor(x).astype(np.int32)
        y0 = np.floor(y).astype(np.int32)
        tx = (x - x0)[:, None]
        ty = (y - y0)[:, None]
        x0 %= width
        y0 %= height
        x1 = (x0 + 1) % width
        y1 = (y0 + 1) % height
        top = pixels[y0, x0] * (1.0 - tx) + pixels[y0, x1] * tx
        bottom = pixels[y1, x0] * (1.0 - tx) + pixels[y1, x1] * tx
        return top * (1.0 - ty) + bottom * ty

    @staticmethod
    def _rasterize_triangle_uvs(
        transformed: np.ndarray,
        uv0: np.ndarray,
        triangles: np.ndarray,
        left: float,
        top: float,
        width: int,
        height: int,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Affine-interpolate authored UVs over the deformed triangle list."""

        coverage = np.zeros((height, width), dtype=bool)
        coordinates = np.zeros((height, width, 2), dtype=np.float32)
        local = transformed - np.asarray((left, top), dtype=np.float32)
        for indices in triangles:
            points = local[indices]
            x0 = max(0, int(math.floor(float(points[:, 0].min()))))
            y0 = max(0, int(math.floor(float(points[:, 1].min()))))
            x1 = min(width, int(math.ceil(float(points[:, 0].max()))))
            y1 = min(height, int(math.ceil(float(points[:, 1].max()))))
            if x0 >= x1 or y0 >= y1:
                continue
            a, b, c = points
            denominator = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
            if abs(float(denominator)) < 1e-7:
                continue
            ys, xs = np.mgrid[y0:y1, x0:x1]
            xs = xs.astype(np.float32) + 0.5
            ys = ys.astype(np.float32) + 0.5
            first = ((b[1] - c[1]) * (xs - c[0]) + (c[0] - b[0]) * (ys - c[1])) / denominator
            second = ((c[1] - a[1]) * (xs - c[0]) + (a[0] - c[0]) * (ys - c[1])) / denominator
            third = 1.0 - first - second
            inside = (first >= -1e-5) & (second >= -1e-5) & (third >= -1e-5)
            if not np.any(inside):
                continue
            interpolated = (
                first[..., None] * uv0[indices[0]]
                + second[..., None] * uv0[indices[1]]
                + third[..., None] * uv0[indices[2]]
            )
            region = coordinates[y0:y1, x0:x1]
            region[inside] = interpolated[inside]
            coverage[y0:y1, x0:x1] |= inside
        return coverage, coordinates

    def _render_break_layer(
        self,
        node: GuiSceneNode,
        geometry,
        parameters: dict[str, object],
        samples: tuple[np.ndarray, np.ndarray],
        pixels_per_unit: float,
        noise_image: QImage | None,
        *,
        _static_only: bool = False,
    ) -> tuple[tuple[float, float], QPixmap] | None:
        positions, uv0, uv1, triangles = geometry
        try:
            bar_scale = np.asarray(parameters["BarScale"][:3], dtype=np.float32)
            offset = np.asarray(parameters["Offset"][:3], dtype=np.float32)
            break_vector = np.asarray(parameters["BreakVector"][:3], dtype=np.float32)
            twist = np.radians(np.asarray(parameters["TwistIntensity"][:3], dtype=np.float32))
            mesh_scale = float(parameters["MeshScale"])
            break_position = float(parameters["BreakPos"])
            break_smooth = float(parameters["BreakSmooth"])
            break_intensity = max(0.0, float(parameters["BreakIntensity"]))
            color_intensity = max(0.0, float(parameters["ColorIntensity"]))
            light_intensity = max(0.0, float(parameters["LightIntensity"]))
            alpha = max(0.0, float(parameters["Alpha"]))
            cut_angle = abs(float(parameters["CutAngle"]))
        except (IndexError, TypeError, ValueError):
            return None
        if min(abs(float(bar_scale[0])), abs(float(bar_scale[1]))) < 1e-6:
            return None

        base = positions.copy()
        base += np.asarray((0.5 - offset[0], offset[1] - 0.5, offset[2]), dtype=np.float32) * mesh_scale * 0.1
        base *= np.asarray((bar_scale[0], bar_scale[1], max(1.0, bar_scale[2])), dtype=np.float32)

        both_sides = float(
            parameters["BreakBothSide"]
            if "BreakBothSide" in parameters
            else parameters["BreakBothSides"]
        )
        center = break_position * (1.0 + 2.0 * break_smooth) - break_smooth
        half_width = (both_sides - 0.5) * break_smooth
        edge0, edge1 = sorted((center + 2.0 * half_width, center - 2.0 * half_width))
        coordinate = uv1[:, 0]
        symmetric = 1.0 - np.minimum(1.0, 2.0 * np.abs(coordinate - 0.5))
        coordinate = coordinate + (symmetric - coordinate) * both_sides
        if abs(edge1 - edge0) < 1e-6:
            amount = (coordinate >= edge1).astype(np.float32)
        else:
            amount = np.clip((coordinate - edge0) / (edge1 - edge0), 0.0, 1.0)
            amount = amount * amount * (3.0 - 2.0 * amount)
        factor = amount * break_intensity

        candidate = base + break_vector
        if noise_image is not None and np.any(np.abs(break_vector[1:]) > 1e-6):
            noise = self._texture_array(noise_image).astype(np.float32) / 255.0
            fps = max(1.0, float(self._scene.root.render_properties.get("BaseFps", 60.0)))
            milliseconds = (self.playback.frame if self.playback is not None else 0.0) * 1000.0 / fps
            period_a = max(1e-3, float(parameters["VertexNoiseAnimations_A"]) * 1000.0)
            period_b = max(1e-3, float(parameters["VertexNoiseAnimations_B"]) * 1000.0)
            noise_uv = np.column_stack((uv1[:, 0] + milliseconds / period_a, uv1[:, 0] + milliseconds / period_b)) % 1.0
            values = np.maximum(
                self._sample_wrapped_texture(noise[..., :2], noise_uv),
                1e-6,
            ) ** max(
                0.0, float(parameters["NoiseMapContrast"])
            )
            values = values * 2.0 - 1.0
            candidate[:, 1] += values[:, 0] * break_vector[1]
            candidate[:, 2] += values[:, 1] * break_vector[2]

        sx, sy, sz = np.sin(twist)
        cx, cy, cz = np.cos(twist)
        rotation = np.asarray(
            (
                (cy * cz, cz * sx * sy - cx * sz, sx * sz + cx * cz * sy),
                (cy * sz, cx * cz + sx * sy * sz, cx * sy * sz - cz * sx),
                (-sy, cy * sx, cx * cy),
            ),
            dtype=np.float32,
        )
        candidate = candidate @ rotation.T
        transformed = (base + factor[:, None] * (candidate - base))[:, :2] * pixels_per_unit
        width_units = max(1e-6, float(np.ptp(base[:, 0])))
        height_units = max(1e-6, float(np.ptp(base[:, 1])))
        slant = (
            min(1.0, height_units * math.tan(math.radians(cut_angle)) / width_units)
            if 0.0 < cut_angle < 90.0 else 0.0
        )
        bounds = transformed[np.unique(triangles)]
        left, top = np.floor(bounds.min(axis=0) - 2.0)
        right, bottom = np.ceil(bounds.max(axis=0) + 2.0)
        width, height = int(right - left), int(bottom - top)
        if not 0 < width <= 4096 or not 0 < height <= 4096:
            return None

        coverage = np.zeros((height, width), dtype=bool)
        interpolated_uv = np.zeros((height, width, 2), dtype=np.float32)
        dynamic_triangles = triangles
        static_layer = None
        dynamic_left = float(uv0[:, 0].min())
        if both_sides < 1e-6:
            triangle_uv = uv0[triangles][..., 0]
            margin = max(
                0.02,
                float(np.max(np.abs(uv0[:, 0] - uv1[:, 0])))
                + float(np.max(np.ptp(triangle_uv, axis=1))),
            )
            dynamic_left, dynamic_right = edge0 - margin, edge1 + margin

            if not _static_only:
                static_key = (
                    id(geometry),
                    id(samples[0]),
                    round(pixels_per_unit, 4),
                    tuple(
                        (name, _hashable(value))
                        for name, value in sorted(parameters.items())
                        if name != "BreakPos"
                    ),
                    tuple(round(float(value), 5) for value in (*node.color_scale, *node.color_offset)),
                    round(float(node.saturation), 5),
                )
                static_layer = self._break_static_pixmaps.get(static_key)
                if static_layer is None:
                    static_parameters = dict(parameters)
                    static_parameters["BreakPos"] = 2.0
                    static_layer = self._render_break_layer(
                        node,
                        geometry,
                        static_parameters,
                        samples,
                        pixels_per_unit,
                        noise_image,
                        _static_only=True,
                    )
                    if static_layer is not None:
                        self._break_static_pixmaps[static_key] = static_layer
                        if len(self._break_static_pixmaps) > 16:
                            self._break_static_pixmaps.popitem(last=False)
                else:
                    self._break_static_pixmaps.move_to_end(static_key)

            # Position and UV0 are affine on the undisturbed selection-bar
            # plane. Build it once; ordinary frames only rasterize the narrow
            # moving fracture band and composite the cached intact prefix.
            if _static_only and dynamic_left > float(uv0[:, 0].min()):
                base_pixels = base[:, :2] * pixels_per_unit
                x0 = max(0, int(math.floor(float(base_pixels[:, 0].min() - left))))
                y0 = max(0, int(math.floor(float(base_pixels[:, 1].min() - top))))
                x1 = min(width, int(math.ceil(float(base_pixels[:, 0].max() - left))))
                y1 = min(height, int(math.ceil(float(base_pixels[:, 1].max() - top))))
                if x0 < x1 and y0 < y1:
                    affine = np.linalg.lstsq(
                        np.column_stack((base[:, :2], np.ones(len(base)))), uv0, rcond=None
                    )[0]
                    ys, xs = np.mgrid[y0:y1, x0:x1]
                    points = np.column_stack(
                        (
                            (xs.ravel() + left + 0.5) / pixels_per_unit,
                            (ys.ravel() + top + 0.5) / pixels_per_unit,
                            np.ones(xs.size),
                        )
                    )
                    static_uv = (points @ affine).reshape(y1 - y0, x1 - x0, 2)
                    uv_low, uv_high = uv0.min(axis=0), uv0.max(axis=0)
                    static = (
                        (static_uv[..., 0] >= uv_low[0] - 1e-4)
                        & (static_uv[..., 0] <= min(dynamic_left, uv_high[0]) + 1e-4)
                        & (static_uv[..., 1] >= uv_low[1] - 1e-4)
                        & (static_uv[..., 1] <= uv_high[1] + 1e-4)
                    )
                    interpolated_uv[y0:y1, x0:x1][static] = static_uv[static]
                    coverage[y0:y1, x0:x1] |= static
            dynamic_triangles = triangles[
                (triangle_uv.max(axis=1) >= dynamic_left)
                & (triangle_uv.min(axis=1) <= dynamic_right)
            ]
            if (
                not _static_only
                and static_layer is not None
                and dynamic_left >= float(uv0[:, 0].max())
                and not len(dynamic_triangles)
            ):
                return static_layer
        if len(dynamic_triangles):
            dynamic_coverage, dynamic_uv = self._rasterize_triangle_uvs(
                transformed, uv0, dynamic_triangles, left, top, width, height
            )
            interpolated_uv[dynamic_coverage] = dynamic_uv[dynamic_coverage]
            coverage |= dynamic_coverage
        rows, columns = np.nonzero(coverage)
        if not len(rows):
            return None
        pixel_uv = interpolated_uv[rows, columns]
        uv_min = uv0.min(axis=0)
        uv_span = np.maximum(uv0.max(axis=0) - uv_min, 1e-6)
        normalized_uv = (pixel_uv - uv_min) / uv_span

        # The vertex shader uses UV1 for displacement, while the pixel shader
        # deliberately recomputes the moving break boundary from UV0.
        pixel_coordinate = pixel_uv[:, 0]
        pixel_symmetric = 1.0 - np.minimum(1.0, 2.0 * np.abs(pixel_coordinate - 0.5))
        pixel_coordinate += (pixel_symmetric - pixel_coordinate) * both_sides
        if abs(edge1 - edge0) < 1e-6:
            pixel_break = (pixel_coordinate >= edge1).astype(np.float32)
        else:
            pixel_break = np.clip((pixel_coordinate - edge0) / (edge1 - edge0), 0.0, 1.0)
            pixel_break *= pixel_break * (3.0 - 2.0 * pixel_break)

        alp_pixels, normal_pixels = samples
        alp = self._sample_wrapped_texture(alp_pixels, pixel_uv)[:, 0]
        normals = self._sample_wrapped_texture(normal_pixels, pixel_uv)
        mask_min = float(parameters["MaskMin"])
        mask_max = float(parameters["MaskMax"])
        mask_smooth = max(1e-6, abs(float(parameters["MaskSmooth"])))
        mask_threshold = mask_max + pixel_break * (mask_min - mask_max)
        mask_amount = np.clip(
            (alp - (mask_threshold - mask_smooth)) / (2.0 * mask_smooth), 0.0, 1.0
        )
        mask_amount *= mask_amount * (3.0 - 2.0 * mask_amount)
        # The authored UV strip runs bottom-to-top. Preserve the shader's
        # forward slash: the upper edge reaches farther right than the lower.
        cut_distance = 1.0 - slant * (1.0 - normalized_uv[:, 1]) - normalized_uv[:, 0]
        cut_softness = max(1e-4, abs(float(parameters["CutSmooth"])))
        cut_amount = np.clip(cut_distance / cut_softness + 0.5, 0.0, 1.0)
        opacities = np.clip(
            alpha * node.color_scale[3] * mask_amount * cut_amount, 0.0, 1.0
        )
        visible = opacities > 1.0 / 255.0
        if not np.any(visible):
            return None

        light_direction = np.asarray(
            parameters["LightDirect"][:3], dtype=np.float32
        )
        normal_blend = np.clip(
            pixel_break + float(parameters["NormalBlend"]), 0.0, 1.0
        )
        # This GUIMesh shader consumes Select_NRM's UNORM channels directly;
        # unlike a conventional normal map it does not remap them to -1..1.
        tangent_normals = normals.copy()
        tangent_normals[:, 2] -= 1.0
        tangent_normals *= normal_blend[:, None]
        tangent_normals[:, 2] += 1.0
        tangent_normals /= np.maximum(
            np.linalg.norm(tangent_normals, axis=1, keepdims=True), 1e-6
        )

        # DMC5's fixed GUI light is spatial, not a painted horizontal ramp.
        # These operations mirror the captured ui2120 pixel shader. The mesh
        # is a GUI plane whose captured world-space TBN is (-X, +Y, -Z).
        aspect = np.asarray(
            (
                float(parameters["AspectUV_U"]),
                float(parameters["AspectUV_V"]),
            ),
            dtype=np.float32,
        )
        aspect[np.abs(aspect) < 1e-6] = 1.0
        fixing_light = float(parameters["FixingLight"])
        # The orthographic GUI plane is in front of the camera. For the fixed
        # light used by this material, the shader cancels the view vector and
        # retains only this front/back sign.
        fixed_scale = 1.0 + fixing_light * (bar_scale - 1.0)
        normal_bias = float(parameters["NormalBlend"])
        light_x = fixing_light * (
            (-(1.0 - aspect[0]) * 0.5 - pixel_uv[:, 0])
            * bar_scale[0] / aspect[0]
        ) + fixed_scale[0] * (-light_direction[0] - normal_bias)
        light_y = fixing_light * (
            (-(1.0 - aspect[1]) * 0.5 - pixel_uv[:, 0])
            * bar_scale[1] / aspect[1]
        ) + fixed_scale[1] * (light_direction[1] - normal_bias)
        light_z = (
            1.0
            + fixing_light * (abs(float(light_direction[2])) - 1.0)
            + fixed_scale[2] * light_direction[2]
        )
        inverse_light_length = 1.0 / np.maximum(
            np.sqrt(light_x * light_x + light_y * light_y + light_z * light_z),
            1e-6,
        )
        # Dot against the captured plane basis: tangent=-X, bitangent=+Y,
        # geometric normal=-Z.
        facing = np.maximum(
            (
                tangent_normals[:, 0] * light_x
                - tangent_normals[:, 1] * light_y
                + tangent_normals[:, 2] * light_z
            ) * inverse_light_length,
            1e-6,
        )
        facing = np.minimum(
            np.power(facing, max(0.0, float(parameters["LightDepth"]))),
            1.0,
        )
        light_size = float(parameters["LightSize"])
        light_smooth = max(1e-6, abs(float(parameters["LightSmooth"])))
        darkness = np.clip(
            (facing - (light_size - light_smooth)) / (2.0 * light_smooth),
            0.0,
            1.0,
        )
        darkness *= darkness * (3.0 - 2.0 * darkness)
        shade = 1.0 - darkness
        dark = (
            np.asarray(parameters["LightColor"][:3], dtype=np.float32)
            * light_intensity
        )
        base_color = np.asarray(parameters["BaseColor"][:3], dtype=np.float32)
        highlight = np.asarray(parameters["LightColorBreak"][:3], dtype=np.float32)
        break_light = max(0.0, float(parameters["BreakLightIntensity"]))
        material_color = base_color + (
            highlight * break_light - base_color
        ) * pixel_break[:, None]
        colors = dark + (material_color - dark) * shade[:, None]
        colors *= color_intensity
        # DMC5 raises this linear material result to 2.2 before its final
        # display transform. Qt paints directly into the display image, so the
        # two transfer steps cancel here rather than leaving the preview dark.
        colors = colors * np.asarray(node.color_scale[:3]) + np.asarray(node.color_offset) / 255.0
        if node.saturation != 1.0:
            luminance = colors @ np.asarray((0.2126, 0.7152, 0.0722))
            colors = luminance[:, None] + (colors - luminance[:, None]) * node.saturation
        colors = np.clip(colors, 0.0, 1.0)

        rgba = np.zeros((height, width, 4), dtype=np.uint8)
        rgba[rows, columns, :3] = np.rint(colors * 255.0).astype(np.uint8)
        rgba[rows, columns, 3] = np.rint(opacities * 255.0).astype(np.uint8)
        dynamic_image = QImage(
            rgba.data, width, height, rgba.strides[0], QImage.Format.Format_RGBA8888
        ).copy()
        if static_layer is None or dynamic_left <= float(uv0[:, 0].min()):
            return (float(left), float(top)), QPixmap.fromImage(dynamic_image)

        image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        layer = QPainter(image)
        base_pixels = base[:, :2] * pixels_per_unit
        fraction = np.clip(
            (dynamic_left - float(uv0[:, 0].min())) / max(float(np.ptp(uv0[:, 0])), 1e-6),
            0.0,
            1.0,
        )
        cutoff = float(base_pixels[:, 0].min()) + fraction * float(np.ptp(base_pixels[:, 0]))
        layer.setClipRect(QRectF(0.0, 0.0, max(0.0, cutoff - left), float(height)))
        static_origin, static_pixmap = static_layer
        layer.drawPixmap(
            QPointF(float(static_origin[0] - left), float(static_origin[1] - top)),
            static_pixmap,
        )
        layer.setClipping(False)
        layer.drawImage(0, 0, dynamic_image)
        layer.end()
        return (float(left), float(top)), QPixmap.fromImage(image)

    def _draw_break_mesh(self, painter, node, mesh, material, materials) -> bool:
        """Evaluate the authored selection-bar mesh, UVs, textures and break parameters."""

        pixels_per_unit = (
            self.assets.catalog.profile.mesh_pixels_per_unit
            if self.assets.catalog is not None else 100.0
        )
        rendered = False
        for index, item in materials:
            parameters = self._material_parameters(node, material, index)
            if (
                not _SELECT_BAR_PARAMETERS.issubset(parameters)
                or not ({"BreakBothSide", "BreakBothSides"} & parameters.keys())
            ):
                self._boundary(node, "incomplete GUIMesh_SelectBar material parameters")
                continue
            geometry = self._break_mesh_geometry(mesh, str(item.header.mat_name))
            if geometry is None:
                continue
            references = {
                name: self._material_texture_reference(node, item, name)
                for name in ("ALPMap", "NormalRoughnessMap", "BreakNoiseMap")
            }
            if not references["ALPMap"] or not references["NormalRoughnessMap"]:
                continue
            alp_image = self.assets.texture(references["ALPMap"])
            normal_image = self.assets.texture(references["NormalRoughnessMap"])
            noise_image = self.assets.texture(references["BreakNoiseMap"]) if references["BreakNoiseMap"] else None
            if references["BreakNoiseMap"] and not {
                "NoiseMapContrast", "VertexNoiseAnimations_A",
                "VertexNoiseAnimations_B",
            }.issubset(parameters):
                self._boundary(node, "incomplete animated select-bar noise parameters")
                continue
            if alp_image is None or normal_image is None:
                continue
            samples = self._break_material_samples(geometry, alp_image, normal_image)
            key = (
                id(geometry),
                int(alp_image.cacheKey()),
                int(normal_image.cacheKey()),
                int(noise_image.cacheKey()) if noise_image is not None else 0,
                round(pixels_per_unit, 4),
                tuple(
                    _hashable(parameters.get(name)) for name in (
                        "Alpha", "BarScale", "BaseColor", "BreakBothSide",
                        "BreakBothSides", "BreakIntensity", "BreakLightIntensity",
                        "BreakPos", "BreakSmooth", "BreakVector", "ColorIntensity",
                        "AspectUV_U", "AspectUV_V", "CutAngle", "FixingLight",
                        "LightColor", "LightColorBreak", "LightDepth", "LightDirect",
                        "LightIntensity", "LightSize", "LightSmooth", "MaskMax", "MaskMin",
                        "MaskSmooth", "MeshScale", "NormalBlend",
                        "NoiseMapContrast", "Offset", "TwistIntensity",
                        "VertexNoiseAnimations_A", "VertexNoiseAnimations_B",
                    )
                ),
                tuple(round(float(value), 5) for value in (*node.color_scale, *node.color_offset)),
                round(float(node.saturation), 5),
                round(float(self.playback.frame), 3)
                if noise_image is not None
                and _vector_has_nonzero_tail(parameters.get("BreakVector"))
                and self.playback is not None
                else 0.0,
            )
            cached = self._break_pixmaps.get(key)
            if cached is None:
                cached = self._render_break_layer(
                    node, geometry, parameters, samples, pixels_per_unit, noise_image
                )
                if cached is None:
                    continue
                self._break_pixmaps[key] = cached
                if len(self._break_pixmaps) > 64:
                    self._break_pixmaps.popitem(last=False)
            else:
                self._break_pixmaps.move_to_end(key)
            origin, pixmap = cached
            painter.drawPixmap(QPointF(*origin), pixmap)
            rendered = True
        return rendered

    def _draw_effect(self, painter: QPainter, node: GuiSceneNode) -> None:
        self._boundary(node, "external EFX player/runtime")

    def _draw_layout(self, painter: QPainter) -> None:
        assert self._scene is not None
        for node in self._scene.draw_nodes:
            if node is self._scene.root:
                continue
            path = self._world_path(node)
            fill = self._color(node)
            fill.setAlpha(35 if node.effective_visible else 10)
            painter.fillPath(path, fill)
            painter.setPen(QPen(QColor(80, 190, 255, 150), 0.0))
            painter.drawPath(path)

    def _draw_guides(self, painter: QPainter) -> None:
        if self._scene is None:
            return
        for node in self._scene.draw_nodes:
            if node is self._scene.root or (not self._guides and node is not self._selected):
                continue
            pen = QPen(
                QColor(255, 211, 105, 235) if node is self._selected else QColor(80, 195, 205, 90),
                0.0,
            )
            if not node.effective_visible:
                pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(self._world_path(node))

    def _bounds(self, node: GuiSceneNode) -> QRectF:
        half_width = _native_gui_half(_pfmul(node.size[0], 0.5))
        half_height = _native_gui_half(_pfmul(node.size[1], 0.5))
        control = _enum(
            node.render_properties.get("ControlPoint", "LeftTop"),
            {
                "LeftTop": 0, "CenterTop": 1, "RightTop": 2,
                "LeftCenter": 4, "CenterCenter": 5, "RightCenter": 6,
                "LeftBottom": 8, "CenterBottom": 9, "RightBottom": 10,
            },
            0,
        )

        def axis(half: float, alignment: int) -> tuple[float, float]:
            if alignment == 0:
                return 0.0, half + half
            if alignment == 1:
                return -half, half
            return -(half + half), 0.0

        left, right = axis(half_width, control & 3)
        top, bottom = axis(half_height, control >> 2)
        return QRectF(left, top, right - left, bottom - top)

    def _local_path(self, node: GuiSceneNode) -> QPainterPath:
        path = QPainterPath()
        if node.object.type_name == "via.gui.TextureSet" and isinstance(
            node.object.special_data, GuiTextureSet
        ):
            scale = _pair(node.render_properties.get("Scale"), (1.0, 1.0))
            for entry in node.object.special_data.entries:
                left, top, right, bottom = entry.bounds
                path.addRect(
                    QRectF(
                        left * scale[0],
                        top * scale[1],
                        (right - left) * scale[0],
                        (bottom - top) * scale[1],
                    ).normalized()
                )
        elif node.object.type_name == "via.gui.HitArea":
            bounds = self._bounds(node)
            shape = _enum(
                node.render_properties.get("Shape"), _HIT_AREA_SHAPES, 1
            )
            if shape == 0:
                path.addPolygon(
                    QPolygonF(
                        [
                            bounds.bottomLeft(),
                            QPointF(bounds.center().x(), bounds.top()),
                            bounds.bottomRight(),
                        ]
                    )
                )
            elif shape in {2, 3}:
                count = 6 if shape == 2 else 8
                center = bounds.center()
                angle_bias = 0.0 if count == 6 else 0.5
                points = [
                    QPointF(
                        center.x() + math.cos(2 * math.pi * (index + angle_bias) / count) * bounds.width() * 0.5,
                        center.y() + math.sin(2 * math.pi * (index + angle_bias) / count) * bounds.height() * 0.5,
                    )
                    for index in range(count)
                ]
                path.addPolygon(QPolygonF(points))
            else:
                path.addRect(bounds)
        elif node.object.type_name == "via.gui.Circle":
            return self._circle_path(node)
        elif node.object.type_name in _CONTAINERS | {"via.gui.Mesh", "via.gui.Effect"}:
            # These classes have no file-authored local hit/draw rectangle.
            # An arbitrary editor-sized quad would change native hit routing.
            return path
        else:
            path.addRect(self._bounds(node))
        return path

    def _circle_path(
        self,
        node: GuiSceneNode,
        *,
        hit_test: bool = False,
    ) -> QPainterPath:
        path = QPainterPath()
        points = self._circle_points(node, hit_test=hit_test)
        if points is None:
            return path
        outer, inner = points
        path.addPolygon(QPolygonF([*outer, *reversed(inner)]))
        return path

    def _circle_points(
        self,
        node: GuiSceneNode,
        *,
        hit_test: bool,
    ) -> tuple[list[QPointF], list[QPointF]] | None:
        bounds = self._bounds(node)
        arc = _pair(node.render_properties.get("ArcAngle"), (0.0, 360.0))
        arc_min, arc_max = sorted(
            min(_preview_f32(360.0), max(_preview_f32(0.0), _preview_f32(value)))
            for value in arc
        )
        arc_start = _preview_f32(node.render_properties.get("ArcStart", 0.0))
        subdivisions = int(node.render_properties.get("Subdiv", 32))
        if subdivisions <= 0:
            self._boundary(node, "invalid authored Circle subdivision count")
            return None
        if subdivisions > 65_536:
            self._boundary(node, "Circle subdivision count exceeds preview safety limit")
            return None
        step = _pfdiv(360.0, float(subdivisions))
        first_step = _pfsub(step, _preview_f32(math.fmod(arc_min, step)))
        count = subdivisions - math.trunc(_pfdiv(_pfsub(360.0, arc_max), step))
        count -= math.trunc(_pfdiv(arc_min, step))
        if count < 0:
            self._boundary(node, "invalid authored Circle arc/subdivision combination")
            return None
        angles = []
        current = _pfadd(arc_min, arc_start)
        increment = first_step
        for index in range(count + 1):
            angles.append(_pfadd(arc_max, arc_start) if index == count else current)
            current = _pfadd(current, increment)
            increment = step
        ratio = _preview_f32(node.render_properties.get("InnerRatio", 0.0))
        draw_radius_x = _pfmul(node.size[0], 0.5)
        draw_radius_y = _pfmul(node.size[1], 0.5)
        center = QPointF(
            _pfadd(bounds.left(), draw_radius_x),
            _pfadd(bounds.top(), draw_radius_y),
        )
        radius_x, radius_y = draw_radius_x, draw_radius_y
        if hit_test:
            # Circle's native hit-test virtual deliberately swaps the two
            # truncating-half radii; rendering does not.
            radius_x = _native_gui_half(draw_radius_y)
            radius_y = _native_gui_half(draw_radius_x)

        def point(angle: float, radius: float) -> QPointF:
            value = _pfmul(
                _pfmul(angle, 0.0055555556900799274),
                3.1415927410125732,
            )
            cosine = _preview_f32(math.cos(value))
            sine = _preview_f32(math.sin(value))
            return QPointF(
                _pfadd(center.x(), _pfmul(radius, _pfmul(cosine, radius_x))),
                _pfadd(center.y(), _pfmul(radius, _pfmul(sine, radius_y))),
            )

        outer = [point(angle, 1.0) for angle in angles]
        inner = [point(angle, ratio) for angle in angles]
        return outer, inner

    def _world_path(
        self,
        node: GuiSceneNode,
        *,
        hit_test: bool = False,
    ) -> QPainterPath:
        local = (
            self._circle_path(node, hit_test=True)
            if hit_test and node.object.type_name == "via.gui.Circle"
            else self._local_path(node)
        )
        return QTransform(*node.world_transform).map(local)

    def _native_contains(self, node: GuiSceneNode, point: QPointF) -> bool:
        """Apply the recovered leaf hit-test geometry and strict edge rule."""

        matrix = node.world_matrix
        if matrix is None:
            return False

        def project(value: QPointF) -> tuple[float, float, float]:
            return project_gui_point((value.x(), value.y(), 0.0), matrix)

        def quad_contains(bounds: QRectF) -> bool:
            quad = tuple(
                project(value)
                for value in (
                    bounds.topLeft(), bounds.topRight(),
                    bounds.bottomLeft(), bounds.bottomRight(),
                )
            )
            if all(value[2] > 1.0 for value in quad):
                return False
            target = point.x(), point.y()
            return _strict_triangle_contains(target, quad[0], quad[2], quad[1]) or _strict_triangle_contains(
                target, quad[2], quad[1], quad[3]
            )

        bounds = self._bounds(node)
        kind = node.object.type_name
        if kind == "via.gui.Circle":
            if not quad_contains(bounds):
                return False
            points = self._circle_points(node, hit_test=True)
            if points is None:
                return False
            outer, inner = points
            target = point.x(), point.y()
            for first in range(len(outer) - 1):
                vertices = tuple(
                    project(value)
                    for value in (
                        outer[first], inner[first],
                        outer[first + 1], inner[first + 1],
                    )
                )
                if all(value[2] > 1.0 for value in vertices):
                    continue
                if _strict_triangle_contains(
                    target,
                    vertices[0],
                    vertices[1],
                    vertices[2],
                ) or _strict_triangle_contains(
                    target,
                    vertices[1],
                    vertices[2],
                    vertices[3],
                ):
                    return True
            return False

        if kind == "via.gui.HitArea":
            shape = _enum(
                node.render_properties.get("Shape", "Rect"),
                _HIT_AREA_SHAPES,
                1,
            )
            center = bounds.center()
            target = point.x(), point.y()
            if shape == 0:
                vertices = tuple(
                    project(value)
                    for value in (
                        QPointF(center.x(), bounds.top()),
                        bounds.bottomRight(),
                        bounds.bottomLeft(),
                    )
                )
                return _strict_triangle_contains(target, *vertices)
            if not quad_contains(bounds):
                return False
            if shape == 1:
                return True
            count = 6 if shape == 2 else 8
            bias = 0.0 if count == 6 else 0.5
            polygon = tuple(
                project(
                    QPointF(
                        center.x() + bounds.width() * 0.5 * math.cos(math.tau * (index + bias) / count),
                        center.y() + bounds.height() * 0.5 * math.sin(math.tau * (index + bias) / count),
                    )
                )
                for index in range(count)
            )
            world_center = project(center)
            return any(
                _strict_triangle_contains(
                    target,
                    polygon[index],
                    polygon[(index + 1) % count],
                    world_center,
                )
                for index in range(count)
            )

        if kind == "via.gui.TextureSet" and isinstance(
            node.object.special_data, GuiTextureSet
        ):
            scale = _pair(node.render_properties.get("Scale"), (1.0, 1.0))
            return any(
                quad_contains(
                    QRectF(
                        entry.bounds[0] * scale[0],
                        entry.bounds[1] * scale[1],
                        (entry.bounds[2] - entry.bounds[0]) * scale[0],
                        (entry.bounds[3] - entry.bounds[1]) * scale[1],
                    ).normalized()
                )
                for entry in node.object.special_data.entries
            )
        return not bounds.isEmpty() and quad_contains(bounds)

    def _circle_brush(self, node: GuiSceneNode):
        properties = node.render_properties
        color_type = _enum(
            properties.get("ColorType"), _CIRCLE_COLOR_TYPES, 0
        )
        if color_type == 0:
            return self._brush(node)
        self._boundary(node, "CPU emulation of native Circle vertex-color raster precision")
        fallback = _qcolor(properties.get("Color"), QColor(255, 255, 255, 255))
        inner = self._color(node, _qcolor(properties.get("InnerColor"), fallback))
        outer = self._color(node, _qcolor(properties.get("OuterColor"), fallback))
        ratio = min(1.0, max(0.0, float(properties.get("InnerRatio", 0.0))))
        key = ("circle", *inner.getRgb(), *outer.getRgb(), round(ratio, 6))
        pixmap = self._vertex_gradients.get(key)
        side = 64
        if pixmap is None:
            image = QImage(side, side, QImage.Format.Format_RGBA8888)
            for y in range(side):
                dy = ((y + 0.5) / side - 0.5) * 2.0
                for x in range(side):
                    dx = ((x + 0.5) / side - 0.5) * 2.0
                    radius = math.hypot(dx, dy)
                    amount = (
                        1.0
                        if ratio >= 1.0
                        else min(1.0, max(0.0, (radius - ratio) / (1.0 - ratio)))
                    )
                    image.setPixelColor(x, y, _mix_color(inner, outer, amount))
            pixmap = QPixmap.fromImage(image)
            self._vertex_gradients[key] = pixmap
        bounds = self._bounds(node)
        brush = QBrush(pixmap)
        transform = QTransform()
        transform.translate(bounds.left(), bounds.top())
        transform.scale(bounds.width() / side, bounds.height() / side)
        brush.setTransform(transform)
        return brush

    def _brush(self, node: GuiSceneNode, fallback: QColor | None = None):
        colors = self._vertex_colors(node, fallback=fallback)
        left_top, right_top, left_bottom, right_bottom = colors
        color_type = _enum(
            node.render_properties.get("ColorType"), _COLOR_TYPES, 0
        )
        bounds = self._bounds(node)
        if color_type in {1, 2, 3}:
            # Qt gradients interpolate encoded sRGB. DMC5 interpolates the
            # packed linear vertex colors, so use the cached linear raster.
            return self._vertex_brush(bounds, colors)
        return left_top

    def _vertex_colors(
        self,
        node: GuiSceneNode,
        fallback: QColor | None = None,
        *,
        apply_saturation: bool = True,
    ) -> tuple[QColor, QColor, QColor, QColor]:
        properties = node.render_properties
        raw_base = _qcolor(
            properties.get("Color"),
            fallback or QColor(255, 255, 255, 255),
        )
        color_type = _enum(properties.get("ColorType"), _COLOR_TYPES, 0)
        if color_type == 1:
            raw = (
                _qcolor(properties.get("ColorTop"), raw_base),
                _qcolor(properties.get("ColorTop"), raw_base),
                _qcolor(properties.get("ColorBottom"), raw_base),
                _qcolor(properties.get("ColorBottom"), raw_base),
            )
        elif color_type == 2:
            raw = (
                _qcolor(properties.get("ColorLeft"), raw_base),
                _qcolor(properties.get("ColorRight"), raw_base),
                _qcolor(properties.get("ColorLeft"), raw_base),
                _qcolor(properties.get("ColorRight"), raw_base),
            )
        elif color_type == 3:
            raw = tuple(
                _qcolor(properties.get(name), raw_base)
                for name in (
                    "ColorLeftTop",
                    "ColorRightTop",
                    "ColorLeftBottom",
                    "ColorRightBottom",
                )
            )
        else:
            raw = (raw_base,) * 4
        return tuple(
            self._color(node, color, apply_saturation=apply_saturation)
            for color in raw
        )

    def _vertex_brush(
        self,
        bounds: QRectF,
        colors: tuple[QColor, QColor, QColor, QColor],
    ) -> QBrush:
        key = tuple(channel for color in colors for channel in color.getRgb())
        pixmap = self._vertex_gradients.get(key)
        side = 16
        if pixmap is None:
            image = QImage(side, side, QImage.Format.Format_RGBA8888)
            for y in range(side):
                vertical = y / (side - 1)
                for x in range(side):
                    horizontal = x / (side - 1)
                    top = _mix_color(colors[0], colors[1], horizontal)
                    bottom = _mix_color(colors[2], colors[3], horizontal)
                    image.setPixelColor(x, y, _mix_color(top, bottom, vertical))
            pixmap = QPixmap.fromImage(image)
            if len(self._vertex_gradients) >= 128:
                self._vertex_gradients.clear()
            self._vertex_gradients[key] = pixmap
        brush = QBrush(pixmap)
        transform = QTransform()
        transform.translate(bounds.left(), bounds.top())
        transform.scale(bounds.width() / side, bounds.height() / side)
        brush.setTransform(transform)
        return brush

    def _color(
        self,
        node: GuiSceneNode,
        source: QColor | None = None,
        *,
        apply_saturation: bool = True,
    ) -> QColor:
        if source is None:
            source = _qcolor(
                node.render_properties.get("Color"),
                QColor(255, 255, 255, 255),
            )
        return QColor.fromRgb(
            *_cached_display_rgba(
                source.getRgb(),
                tuple(node.color_scale),
                tuple(node.color_offset),
                float(node.saturation),
                bool(apply_saturation),
            )
        )

    def _mask_mode(self, node: GuiSceneNode) -> int:
        mode = 1
        chain = []
        current: GuiSceneNode | None = node.parent
        while current is not None:
            chain.append(current)
            current = current.parent
        for ancestor in reversed(chain):
            candidate = _enum(
                ancestor.render_properties.get("MaskMode"), _MASK_MODES, 0
            )
            if candidate not in {0, 4}:
                mode = candidate
        return mode

    def _initialize_interaction(self) -> None:
        self._interaction_ready = True
        if self._scene is None or self.playback is None:
            return
        self._list_items_by_owner.clear()
        for node in self._scene.nodes:
            if node.object.type_name in {"via.gui.ScrollList", "via.gui.ScrollGrid"}:
                self._boundary(
                    node,
                    "native recycled list/grid clones require external item data",
                )
        for node in self._scene.nodes:
            if node.object.type_name != "via.gui.SimpleList":
                continue
            self._list_items_by_owner[node.path] = tuple(
                child
                for child in node.children
                if child.object.type_name == "via.gui.SelectItem"
            )
            items = self._list_items(node)
            if not items:
                continue
            selected = int(
                node.render_properties.get(
                    "SelectedIndex",
                    node.render_properties.get("CursorIndex", 0),
                )
            )
            self._list_selection[node.path] = selected
            for index, item in enumerate(items):
                self.playback.overrides.setdefault(item.path, {})["ListIndex"] = index
                self.playback.overrides[item.path]["Selected"] = index == selected
                # SelectItem constructor starts unselected. SimpleList.awake
                # invokes the reflected Selected setter for every item, which
                # only changes state for the selected one.
                if (
                    index == selected
                    and bool(node.render_properties.get("UseFocus", True))
                ):
                    self._start_state(item, self._select_state(item, True))
        self._sync_timer()

    def _list_items(self, node: GuiSceneNode) -> list[GuiSceneNode]:
        cached = self._list_items_by_owner.get(node.path)
        if cached is None:
            cached = tuple(
                child
                for child in node.children
                if child.object.type_name == "via.gui.SelectItem"
            )
            self._list_items_by_owner[node.path] = cached
        return list(cached)

    @staticmethod
    def _select_state(node: GuiSceneNode, selected: bool) -> str:
        enabled = bool(node.render_properties.get("Enabled", True))
        if enabled:
            return "FOCUS" if selected else "UNFOCUS"
        return "DISABLE_FOCUS" if selected else "DISABLE_UNFOCUS"

    def _start_state(self, node: GuiSceneNode, state: str) -> bool:
        if self.playback is None or not self.playback.play_state(node.path, state):
            return False
        self._interaction_frames = max(
            self._interaction_frames,
            self.playback.state_remaining(node.path),
        )
        self._sync_timer()
        return True

    def _set_list_selection(self, owner: GuiSceneNode, index: int) -> bool:
        if self.playback is None or not bool(owner.render_properties.get("UseFocus", True)):
            return False
        items = self._list_items(owner)
        if not 0 <= index < len(items):
            return False
        target = items[index]
        if not bool(target.render_properties.get("CanSelect", True)):
            return False
        previous = self._list_selection.get(owner.path, -1)
        if previous == index:
            self._active_list = owner
            return False
        if 0 <= previous < len(items):
            self._start_state(items[previous], self._select_state(items[previous], False))
            self.playback.overrides.setdefault(items[previous].path, {})["Selected"] = False
        self._start_state(target, self._select_state(target, True))
        self.playback.overrides.setdefault(target.path, {})["Selected"] = True
        self._list_selection[owner.path] = index
        self.playback.overrides.setdefault(owner.path, {})["SelectedIndex"] = index
        self._active_list = owner
        self._apply_layout()
        return True

    def _move_list_selection(self, owner: GuiSceneNode, step: int) -> bool:
        items = self._list_items(owner)
        if not items:
            return False
        previous = self._list_selection.get(owner.path, 0)
        candidate = previous
        for _ in range(len(items)):
            candidate += step
            if candidate < 0 or candidate >= len(items):
                if not bool(owner.render_properties.get("Loop", False)):
                    break
                candidate %= len(items)
            if candidate == previous:
                break
            if bool(items[candidate].render_properties.get("CanSelect", True)):
                moved = self._set_list_selection(owner, candidate)
                self._start_state(owner, "PLUS_INPUT" if step > 0 else "MINUS_INPUT")
                return moved
        self._start_state(owner, "PLUS_INPUT" if step > 0 else "MINUS_INPUT")
        return False

    def _hit_node(self, point: QPointF) -> GuiSceneNode | None:
        if self._scene is None:
            return None
        return next(
            (
                node for node in reversed(self._scene.draw_nodes)
                if node is not self._scene.root
                and node.effective_visible
                and self._world_path(node, hit_test=True).contains(point)
            ),
            None,
        )

    def _runtime_branch_enabled(
        self,
        node: GuiSceneNode,
        *,
        require_hit_visible: bool,
    ) -> bool:
        """Apply native local visibility, alpha, active, and optional hit gates."""

        current: GuiSceneNode | None = node
        while current is not None:
            properties = current.render_properties
            scale = properties.get("ColorScale", (1.0, 1.0, 1.0, 1.0))
            alpha = (
                float(scale[3])
                if isinstance(scale, (list, tuple)) and len(scale) == 4
                else 1.0
            )
            if (
                not bool(properties.get("Visible", True))
                or alpha < _FLOAT32_EPSILON
                or not self._controller_context.runtime_active(current)
                or not self._runtime_active_by_path.get(current.path, True)
                or (
                    require_hit_visible
                    and not bool(properties.get("HitVisible", True))
                )
            ):
                return False
            current = current.parent
        return True

    def _render_branch_enabled(self, node: GuiSceneNode) -> bool:
        """Apply authored visibility, inherited alpha, and external Active state."""

        if not node.effective_visible or node.color_scale[3] < _FLOAT32_EPSILON:
            return False
        current: GuiSceneNode | None = node
        while current is not None:
            if (
                not self._controller_context.runtime_active(current)
                or not self._runtime_active_by_path.get(current.path, True)
            ):
                return False
            current = current.parent
        return True

    def _input_branch_enabled(self, node: GuiSceneNode) -> bool:
        """Apply native file-authored visibility, alpha, and HitVisible gates."""

        return self._runtime_branch_enabled(node, require_hit_visible=True)

    def _input_hit_node(self, point: QPointF) -> GuiSceneNode | None:
        """Return the frontmost leaf admitted by native Control hit traversal."""

        if self._scene is None:
            return None
        return next(
            (
                node for node in reversed(self._scene.draw_nodes)
                if node is not self._scene.root
                and node.object.type_name not in _CONTAINERS
                and self._input_branch_enabled(node)
                and self._native_contains(node, point)
            ),
            None,
        )

    @staticmethod
    def _select_item_ancestor(node: GuiSceneNode | None) -> GuiSceneNode | None:
        while node is not None and node.object.type_name != "via.gui.SelectItem":
            node = node.parent
        return node

    @staticmethod
    def _list_owner(item: GuiSceneNode | None) -> GuiSceneNode | None:
        owner = item.parent if item is not None else None
        return (
            owner
            if owner is not None and owner.object.type_name == "via.gui.SimpleList"
            else None
        )

    def _list_interactive(self, owner: GuiSceneNode) -> bool:
        current: GuiSceneNode | None = owner
        while current is not None:
            if current.object.type_name in _CONTAINERS:
                authored = bool(current.render_properties.get("Interactive", True))
                if not self._controller_context.interactive(current, authored):
                    return False
            current = current.parent
        return True

    def _mouse_select(
        self,
        point: QPointF,
        required_mode: int,
    ) -> GuiSceneNode | None:
        item = self._select_item_ancestor(self._input_hit_node(point))
        owner = self._list_owner(item)
        if item is None or owner is None:
            return item
        self._active_list = owner
        authored_mode = _enum(
            owner.render_properties.get("MouseSelectType", "None"),
            {"None": 0, "MouseOver": 1, "LeftClick": 2},
            0,
        )
        mode = self._controller_context.mouse_select_type(owner, authored_mode)
        if (
            self._runtime_branch_enabled(owner, require_hit_visible=False)
            and self._list_interactive(owner)
            and mode == required_mode
        ):
            self._set_list_selection(owner, self._list_items(owner).index(item))
        return item

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if not self._preview or not self._interaction_enabled or self._scene is None:
            return super().keyPressEvent(event)
        if not self._icon_context.keyboard_mode:
            return super().keyPressEvent(event)
        key = _event_virtual_key(event)
        if key == DMC5_MENU_DECIDE_VK:
            handled = False
            for owner in self._interactive_lists():
                items = self._list_items(owner)
                selected = self._list_selection.get(owner.path, -1)
                if 0 <= selected < len(items):
                    handled |= self._start_state(items[selected], "DECIDE")
            if handled:
                self._apply_layout()
                event.accept()
                return

        bindings = self._icon_context.keyboard_bindings
        handled = False
        for input_type, actions in _DMC5_LIST_INPUT_ACTIONS.items():
            step = (
                -1 if bindings.get(actions[0]) == key
                else 1 if bindings.get(actions[1]) == key
                else 0
            )
            if not step:
                continue
            for owner in self._interactive_lists(input_type):
                self._move_list_selection(owner, step)
                handled = True
        if handled:
            event.accept()
            return
        super().keyPressEvent(event)

    def _interactive_lists(self, input_type: int | None = None) -> list[GuiSceneNode]:
        """Lists registered for the same global input event in native code."""

        if self._scene is None:
            return []
        return [
            node
            for node in self._scene.nodes
            if node.object.type_name == "via.gui.SimpleList"
            and self._runtime_branch_enabled(node, require_hit_visible=False)
            and self._list_interactive(node)
            and (
                input_type is None
                or _enum(
                    node.render_properties.get("InputType", "None"),
                    {"None": 0, "UpDown": 1, "LeftRight": 2, "LBRB": 3},
                    0,
                ) == input_type
            )
        ]

    def _virtual_point(self, position: QPointF) -> QPointF | None:
        stage, scale = self._stage()
        if scale <= 0.0 or not stage.contains(position):
            return None
        return QPointF((position.x() - stage.left()) / scale, (position.y() - stage.top()) / scale)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = True
            self._pan_start = event.position()
            return
        if event.button() != Qt.MouseButton.LeftButton or self._scene is None:
            return super().mousePressEvent(event)
        point = self._virtual_point(event.position())
        if point is None:
            return
        if self._preview and self._interaction_enabled:
            item = self._mouse_select(point, 2)
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            event.accept()
            return
        node = self._hit_node(point)
        if node is None:
            return
        self.select_node(node)
        self.node_selected.emit(node)
        if self._scene.can_move(node):
            self._drag_node = node
            self._drag_start = point
            self._drag_origin = node.world_position

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._panning:
            delta = event.position() - self._pan_start
            self._pan += delta
            self._pan_start = event.position()
            self.update()
            return
        if self._preview and self._interaction_enabled and self._scene is not None:
            point = self._virtual_point(event.position())
            item = None
            if point is not None:
                item = self._mouse_select(point, 1)
            owner = self._list_owner(item)
            clickable = (
                item is not None
                and owner is not None
                and bool(item.render_properties.get("CanSelect", True))
                and self._list_interactive(owner)
                and self._controller_context.mouse_select_type(
                    owner,
                    _enum(
                        owner.render_properties.get("MouseSelectType", "None"),
                        {"None": 0, "MouseOver": 1, "LeftClick": 2},
                        0,
                    ),
                ) in {1, 2}
            )
            self.setCursor(
                Qt.CursorShape.PointingHandCursor
                if clickable
                else Qt.CursorShape.ArrowCursor
            )
            event.accept()
            return
        if self._drag_node is None or self._scene is None:
            return super().mouseMoveEvent(event)
        point = self._virtual_point(event.position())
        if point is None:
            return
        desired = (
            self._drag_origin[0] + point.x() - self._drag_start.x(),
            self._drag_origin[1] + point.y() - self._drag_start.y(),
        )
        x, y = self._scene.local_position_for_scene_point(self._drag_node, *desired)
        self._transient = self._drag_node, x, y
        self._apply_layout()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = False
            return
        if event.button() == Qt.MouseButton.LeftButton and self._drag_node is not None:
            node = self._drag_node
            transient = self._transient
            self._drag_node = None
            self._transient = None
            if transient is not None:
                self.node_moved.emit(node, transient[1], transient[2])
            self._apply_layout()

    def wheelEvent(self, event) -> None:  # noqa: N802
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._zoom = min(12.0, max(0.1, self._zoom * (1.15 if event.angleDelta().y() > 0 else 1 / 1.15)))
            self.update()
            event.accept()
            return
        # Mouse-wheel-to-list movement is not a DMC5 GUI behavior. A game
        # controller may map it to an input action, but GUIR alone does not.
        super().wheelEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._interaction_enabled:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        super().leaveEvent(event)


def _event_virtual_key(event) -> int:
    """Translate a Qt key event to the Windows virtual-key domain used by DMC5."""

    native = int(event.nativeVirtualKey())
    if native:
        return native
    key = event.key()
    translated = {
        Qt.Key.Key_Backspace: 8,
        Qt.Key.Key_Tab: 9,
        Qt.Key.Key_Return: 13,
        Qt.Key.Key_Enter: 146,
        Qt.Key.Key_Escape: 27,
        Qt.Key.Key_Space: 32,
        Qt.Key.Key_PageUp: 33,
        Qt.Key.Key_PageDown: 34,
        Qt.Key.Key_End: 35,
        Qt.Key.Key_Home: 36,
        Qt.Key.Key_Left: 37,
        Qt.Key.Key_Up: 38,
        Qt.Key.Key_Right: 39,
        Qt.Key.Key_Down: 40,
        Qt.Key.Key_Insert: 45,
        Qt.Key.Key_Delete: 46,
    }
    return translated.get(key, int(key) if 0 <= int(key) <= 0xFF else 0)


def _qcolor(value: object, fallback: QColor) -> QColor:
    if isinstance(value, (list, tuple)) and len(value) >= 4:
        try:
            return QColor(*(int(item) for item in value[:4]))
        except (TypeError, ValueError):
            pass
    return QColor(fallback)


def _is_runtime_texture(reference: str) -> bool:
    """Runtime render targets are engine-owned, not serialized TEX assets."""

    normalized = reference.replace("\\", "/").lstrip("@/").casefold()
    return normalized.endswith(".rtex")


def _strict_triangle_contains(
    point: tuple[float, float],
    first: tuple[float, float, float],
    second: tuple[float, float, float],
    third: tuple[float, float, float],
) -> bool:
    """DMC5 requires all three triangle edge signs to be strictly equal."""

    x, y = point

    def cross(left, right) -> float:
        return (right[0] - left[0]) * (y - left[1]) - (right[1] - left[1]) * (x - left[0])

    signs = cross(first, second), cross(second, third), cross(third, first)
    return all(value > 0.0 for value in signs) or all(value < 0.0 for value in signs)


def _is_vector(value: object, length: int) -> bool:
    return isinstance(value, (list, tuple)) and len(value) >= length


def _vector_has_nonzero_tail(value: object) -> bool:
    return _is_vector(value, 3) and any(abs(float(item)) > 1e-6 for item in value[1:3])


def _float_color(value: object, intensity: float = 1.0) -> QColor:
    red, green, blue, alpha = (float(item) for item in value[:4])
    return QColor.fromRgbF(
        *(min(1.0, max(0.0, channel * intensity)) for channel in (red, green, blue)),
        min(1.0, max(0.0, alpha)),
    )


def _mix_color(left: QColor, right: QColor, amount: float) -> QColor:
    inverse = 1.0 - amount
    left_rgba = left.getRgbF()
    right_rgba = right.getRgbF()
    linear = [
        _srgb_to_linear(left_rgba[index]) * inverse
        + _srgb_to_linear(right_rgba[index]) * amount
        for index in range(3)
    ]
    return QColor.fromRgbF(
        *(_linear_to_srgb(value) for value in linear),
        left_rgba[3] * inverse + right_rgba[3] * amount,
    )


def _native_gui_half(value: float) -> float:
    """Round-trip through DMC5's truncating binary32-to-binary16 helper."""

    bits = struct.unpack("<I", struct.pack("<f", _preview_f32(value)))[0]
    sign = (bits >> 16) & 0x8000
    mantissa = bits & 0x7FFFFF
    exponent = ((bits >> 23) & 0xFF) - 0x70
    if exponent <= 0:
        payload = 0 if exponent < -10 else ((mantissa | 0x800000) >> (1 - exponent)) >> 13
        half = sign | payload
    elif exponent == 0x8F:
        payload = mantissa >> 13
        half = sign | 0x7C00 | (payload or (1 if mantissa else 0))
    elif exponent > 0x1E:
        half = sign | 0x7C00
    else:
        half = sign | (exponent << 10) | (mantissa >> 13)
    return struct.unpack("<e", struct.pack("<H", half))[0]


@lru_cache(maxsize=256)
def _native_srgb8_to_linear8(value: int) -> int:
    channel = _preview_f32(_preview_f32(float(value & 0xFF)) / _preview_f32(255.0))
    if channel <= _preview_f32(0.04045):
        linear = _preview_f32(channel / _preview_f32(12.92))
    else:
        base = _preview_f32(
            _preview_f32(channel + _preview_f32(0.055)) / _preview_f32(1.055)
        )
        linear = _preview_f32(math.pow(base, _preview_f32(2.4)))
    return int(math.floor(_preview_f32(_preview_f32(linear * 255.0) + 0.5)))


def _native_color_channel(value: int, scale: float, offset: float) -> int:
    transformed = _preview_f32(
        _preview_f32(_preview_f32(float(value)) * _preview_f32(scale))
        + _preview_f32(offset)
    )
    transformed = _preview_f32(transformed + _preview_f32(0.5))
    if math.isnan(transformed):
        return 0
    return int(min(255.0, max(0.0, transformed)))


def _native_linear_rgba(node: GuiSceneNode, source: QColor) -> tuple[int, int, int, int]:
    rgba = source.getRgb()
    rgb = tuple(
        _native_color_channel(
            _native_srgb8_to_linear8(rgba[index]),
            node.color_scale[index],
            node.color_offset[index],
        )
        for index in range(3)
    )
    alpha = _native_color_channel(rgba[3], node.color_scale[3], 0.0)
    return rgb + (alpha,)


@lru_cache(maxsize=16_384)
def _cached_display_rgba(
    rgba: tuple[int, int, int, int],
    scale: tuple[float, float, float, float],
    offset: tuple[float, float, float],
    saturation: float,
    apply_saturation: bool,
) -> tuple[int, int, int, int]:
    rgb = tuple(
        _native_color_channel(
            _native_srgb8_to_linear8(rgba[index]),
            scale[index],
            offset[index],
        )
        for index in range(3)
    )
    alpha = _native_color_channel(rgba[3], scale[3], 0.0)
    channels = [value / 255.0 for value in rgb]
    if apply_saturation:
        luminance = 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]
        channels = [
            luminance + (value - luminance) * saturation
            for value in channels
        ]
    return (
        *(round(_linear_to_srgb(value) * 255.0) for value in channels),
        alpha,
    )


def _srgb_to_linear(value: float) -> float:
    value = min(1.0, max(0.0, float(value)))
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(value: float) -> float:
    value = min(1.0, max(0.0, float(value)))
    return value * 12.92 if value <= 0.0031308 else 1.055 * value ** (1.0 / 2.4) - 0.055


def _srgb_to_linear_array(values: np.ndarray) -> np.ndarray:
    values = np.clip(values, 0.0, 1.0)
    return np.where(
        values <= 0.04045,
        values / 12.92,
        np.power((values + 0.055) / 1.055, 2.4),
    )


def _linear_to_srgb_array(values: np.ndarray) -> np.ndarray:
    values = np.clip(values, 0.0, 1.0)
    return np.where(
        values <= 0.0031308,
        values * 12.92,
        1.055 * np.power(values, 1.0 / 2.4) - 0.055,
    )


@lru_cache(maxsize=1)
def _srgb_multiply_table() -> np.ndarray:
    """Map two encoded bytes through DMC5's linear texture/tint multiply."""

    values = np.arange(256, dtype=np.float32) / 255.0
    linear = _srgb_to_linear_array(values)
    product = linear[:, None] * linear[None, :]
    return np.clip(
        np.rint(_linear_to_srgb_array(product) * 255.0),
        0.0,
        255.0,
    ).astype(np.uint8)


def _pair(value: object, fallback: tuple[float, float]) -> tuple[float, float]:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return float(value[0]), float(value[1])
        except (TypeError, ValueError):
            pass
    return fallback


def _hashable(value: object):
    if isinstance(value, (list, tuple)):
        return tuple(_hashable(item) for item in value)
    if isinstance(value, float):
        return round(value, 6)
    return value


def _enum(value: object, labels: dict[str, int], default: int | None) -> int:
    if value is None:
        if default is None:
            raise GuiSceneError("missing GUI enum with no recovered default")
        return default
    if isinstance(value, str):
        try:
            return labels[value]
        except KeyError as exc:
            raise GuiSceneError(f"unknown GUI enum label {value!r}") from exc
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise GuiSceneError(f"invalid GUI enum value {value!r}") from exc
    if result not in labels.values():
        raise GuiSceneError(f"unknown GUI enum value {result}")
    return result


def _part_lines(
    parts: list[str | tuple[str, str] | _RubyPart],
) -> list[list[str | tuple[str, str] | _RubyPart]]:
    lines: list[list[str | tuple[str, str] | _RubyPart]] = [[]]
    for part in parts:
        if not isinstance(part, str):
            lines[-1].append(part)
            continue
        chunks = part.split("\n")
        for index, chunk in enumerate(chunks):
            if chunk:
                lines[-1].append(chunk)
            if index + 1 < len(chunks):
                lines.append([])
    return lines


def _wrap_dmc5_text(value: str, metrics: QFontMetricsF, width: float) -> str:
    """Insert line breaks using DMC5's recovered CJK/punctuation policy."""

    if width <= 0.0:
        return value
    result: list[str] = []
    for authored_line in value.split("\n"):
        remaining = authored_line
        while remaining and metrics.horizontalAdvance(remaining) > width:
            codepoints = [ord(character) for character in remaining]
            advances = [metrics.horizontalAdvance(character) for character in remaining]
            split = find_dmc5_wrap_break(codepoints, advances, width)
            if split is None or split < 0 or split + 1 >= len(remaining):
                break
            result.append(remaining[: split + 1])
            remaining = remaining[split + 1 :]
        result.append(remaining)
    return "\n".join(result)


def _alignment(value: int) -> int:
    horizontal = {
        0: Qt.AlignmentFlag.AlignLeft,
        1: Qt.AlignmentFlag.AlignHCenter,
        2: Qt.AlignmentFlag.AlignRight,
    }[value & 3]
    vertical = {
        0: Qt.AlignmentFlag.AlignTop,
        1: Qt.AlignmentFlag.AlignVCenter,
        2: Qt.AlignmentFlag.AlignBottom,
    }[value >> 2]
    return int(horizontal | vertical | Qt.TextFlag.TextDontClip)
