from __future__ import annotations

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QElapsedTimer,
    QEvent,
    QModelIndex,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QTimer,
    Qt,
    QVariantAnimation,
)
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QAbstractItemView, QTabWidget, QWidget

from settings import DEFAULT_SETTINGS


_RIPPLE_DURATION_MS = 520
_MAX_ACTIVE_EFFECTS = 48
_EDITING_GLOW_CYCLE_MS = 1_250
_EDITING_GLOW_MINIMUM_MS = 700
_EDITING_GLOW_WIDTH = 18


class _AiActionRipple(QWidget):

    def __init__(
        self,
        host: QWidget,
        target: QRect,
        accent: QColor,
        center: QPoint | None = None,
    ):
        super().__init__(host)
        bounds = target.adjusted(-48, -48, 48, 48).intersected(
            host.rect()
        )
        origin = bounds.topLeft()
        self.setGeometry(bounds)
        self._target = QRectF(target.translated(-origin))
        self._center = (
            QPointF(center - origin)
            if center is not None
            else self._target.center()
        )
        self._accent = QColor(accent)
        self._progress = 0.0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._animation = QVariantAnimation(self)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setDuration(_RIPPLE_DURATION_MS)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._set_progress)
        self._animation.finished.connect(self.deleteLater)

    def start(self):
        self.show()
        self.raise_()
        self._animation.start()

    def finish(self):
        self._animation.stop()
        self.deleteLater()

    def _set_progress(self, value):
        self._progress = min(1.0, max(0.0, float(value)))
        self.update()

    def paintEvent(self, event):
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        remaining = 1.0 - self._progress
        target = self._target.adjusted(-2.0, -2.0, 2.0, 2.0)
        wash = QColor(self._accent)
        wash.setAlpha(round(38 * remaining * remaining))
        border = QColor(self._accent)
        border.setAlpha(round(118 * remaining))
        painter.setBrush(wash)
        painter.setPen(QPen(border, 1.2))
        painter.drawRoundedRect(target, 6.0, 6.0)

        center = self._center
        radius_limit = min(
            42.0,
            max(22.0, min(self._target.width(), self._target.height()) * 0.9),
        )
        radius = 5.0 + (radius_limit - 5.0) * self._progress
        ring = QColor(self._accent)
        ring.setAlpha(round(215 * remaining * remaining))
        ring_pen = QPen(ring, 1.4 + remaining)
        ring_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(ring_pen)
        painter.drawEllipse(
            QRectF(
                center.x() - radius,
                center.y() - radius,
                radius * 2.0,
                radius * 2.0,
            )
        )

        core = QColor(self._accent)
        core.setAlpha(round(180 * remaining))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(core)
        core_radius = max(1.5, 3.2 * remaining)
        painter.drawEllipse(
            QRectF(
                center.x() - core_radius,
                center.y() - core_radius,
                core_radius * 2.0,
                core_radius * 2.0,
            )
        )


class _AiEditingGlow(QWidget):

    def __init__(self, host: QWidget, accent: QColor):
        super().__init__(host)
        self._host = host
        self._accent = QColor(accent)
        self._intensity = 0.62
        self._requested_active = False
        self._visible_since = QElapsedTimer()
        self.setObjectName("aiEditingGlow")
        self.setGeometry(host.rect())
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.hide()

        self._animation = QVariantAnimation(self)
        self._animation.setStartValue(0.62)
        self._animation.setEndValue(1.0)
        self._animation.setDuration(_EDITING_GLOW_CYCLE_MS)
        self._animation.setLoopCount(-1)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._animation.valueChanged.connect(self._set_intensity)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._hide_if_idle)
        host.installEventFilter(self)

    @property
    def requested_active(self) -> bool:
        return self._requested_active

    def set_active(
        self,
        active: bool,
        accent: QColor,
        *,
        immediate: bool = False,
    ):
        self._accent = QColor(accent)
        self._requested_active = bool(active)
        self._hide_timer.stop()
        if active:
            self.setGeometry(self._host.rect())
            if (
                self._animation.state()
                == QAbstractAnimation.State.Stopped
            ):
                self._animation.start()
            if not self._visible_since.isValid():
                self._visible_since.start()
            self.show()
            self.raise_()
            self.update()
            return

        if immediate:
            self._hide_now()
            return
        elapsed = (
            self._visible_since.elapsed()
            if self._visible_since.isValid()
            else _EDITING_GLOW_MINIMUM_MS
        )
        remaining = max(0, _EDITING_GLOW_MINIMUM_MS - elapsed)
        if remaining:
            self._hide_timer.start(remaining)
        else:
            self._hide_now()

    def eventFilter(self, watched, event):
        if watched is self._host:
            if event.type() == QEvent.Type.Resize:
                self.setGeometry(self._host.rect())
            elif (
                event.type() == QEvent.Type.Show
                and self._requested_active
            ):
                self.show()
                self.raise_()
        return super().eventFilter(watched, event)

    def _hide_if_idle(self):
        if not self._requested_active:
            self._hide_now()

    def _hide_now(self):
        self._hide_timer.stop()
        self._animation.stop()
        self._visible_since.invalidate()
        self.hide()

    def _set_intensity(self, value):
        self._intensity = min(1.0, max(0.0, float(value)))
        self.update()

    @staticmethod
    def _gradient(
        start: QPointF,
        end: QPointF,
        accent: QColor,
        intensity: float,
    ) -> QLinearGradient:
        gradient = QLinearGradient(start, end)
        outer = QColor(accent)
        outer.setAlpha(round(205 * intensity))
        middle = QColor(accent)
        middle.setAlpha(round(105 * intensity))
        clear = QColor(accent)
        clear.setAlpha(0)
        gradient.setColorAt(0.0, outer)
        gradient.setColorAt(0.32, middle)
        gradient.setColorAt(1.0, clear)
        return gradient

    def paintEvent(self, event):
        del event
        width = float(
            min(
                _EDITING_GLOW_WIDTH,
                max(8, min(self.width(), self.height()) // 8),
            )
        )
        if width <= 0:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = QRectF(self.rect())
        accent = QColor(self._accent).lighter(108)

        painter.fillRect(
            QRectF(0.0, 0.0, bounds.width(), width),
            self._gradient(
                QPointF(0.0, 0.0),
                QPointF(0.0, width),
                accent,
                self._intensity,
            ),
        )
        painter.fillRect(
            QRectF(
                0.0,
                bounds.height() - width,
                bounds.width(),
                width,
            ),
            self._gradient(
                QPointF(0.0, bounds.height()),
                QPointF(0.0, bounds.height() - width),
                accent,
                self._intensity,
            ),
        )
        painter.fillRect(
            QRectF(0.0, 0.0, width, bounds.height()),
            self._gradient(
                QPointF(0.0, 0.0),
                QPointF(width, 0.0),
                accent,
                self._intensity,
            ),
        )
        painter.fillRect(
            QRectF(
                bounds.width() - width,
                0.0,
                width,
                bounds.height(),
            ),
            self._gradient(
                QPointF(bounds.width(), 0.0),
                QPointF(bounds.width() - width, 0.0),
                accent,
                self._intensity,
            ),
        )

        outline = QColor(accent)
        outline.setAlpha(round(225 * self._intensity))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(outline, 1.35))
        painter.drawRect(bounds.adjusted(0.7, 0.7, -0.7, -0.7))


class AiActionFeedback:

    def __init__(self, app_window):
        self._app_window = app_window
        self._effects: list[_AiActionRipple] = []
        self._editing_glow = (
            _AiEditingGlow(app_window, self._accent())
            if isinstance(app_window, QWidget)
            else None
        )

    def _accent(self) -> QColor:
        settings = getattr(self._app_window, "settings", {})
        value = (
            settings.get("tree_highlight_color")
            if isinstance(settings, dict)
            else None
        )
        accent = QColor(value or DEFAULT_SETTINGS["tree_highlight_color"])
        if not accent.isValid():
            accent = QColor(DEFAULT_SETTINGS["tree_highlight_color"])
        return accent

    def pulse_widget(
        self,
        widget,
        rect: QRect | None = None,
        *,
        center: QPoint | None = None,
    ) -> bool:
        if not isinstance(widget, QWidget):
            return False
        try:
            host = widget.window()
            if not widget.isVisibleTo(host):
                return False
            local_rect = QRect(rect or widget.rect()).normalized()
            if not local_rect.isValid() or local_rect.isEmpty():
                return False
            top_left = widget.mapTo(host, local_rect.topLeft())
            target = QRect(top_left, local_rect.size()).intersected(host.rect())
            if not target.isValid() or target.isEmpty():
                return False
            ripple_center = (
                widget.mapTo(host, center)
                if center is not None
                else None
            )
            effect = _AiActionRipple(
                host,
                target,
                self._accent(),
                ripple_center,
            )
        except RuntimeError:
            return False

        self._effects.append(effect)
        effect.destroyed.connect(
            lambda _object=None, current=effect: self._discard(current)
        )
        while len(self._effects) > _MAX_ACTIVE_EFFECTS:
            oldest = self._effects.pop(0)
            try:
                oldest.finish()
            except RuntimeError:
                pass
        effect.start()
        return True

    @property
    def editing_active(self) -> bool:
        return bool(
            self._editing_glow
            and self._editing_glow.requested_active
        )

    def set_editing_active(
        self,
        active: bool,
        *,
        immediate: bool = False,
    ) -> bool:
        if self._editing_glow is None:
            return False
        self._editing_glow.set_active(
            bool(active),
            self._accent(),
            immediate=immediate,
        )
        return True

    def pulse_index(
        self,
        view,
        index: QModelIndex,
        *,
        full_row: bool = True,
        ensure_visible: bool = True,
    ) -> bool:
        if not isinstance(view, QAbstractItemView) or not index.isValid():
            return False
        try:
            if ensure_visible:
                view.scrollTo(
                    index,
                    QAbstractItemView.ScrollHint.EnsureVisible,
                )
            rect = view.visualRect(index)
            center = rect.center()
            if full_row:
                rect.setLeft(0)
                rect.setRight(max(0, view.viewport().width() - 1))
            return self.pulse_widget(
                view.viewport(),
                rect,
                center=center,
            )
        except RuntimeError:
            return False

    def pulse_table_row(self, table, row: int) -> bool:
        try:
            model = table.model()
            index = model.index(int(row), 0)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return False
        return self.pulse_index(table, index)

    def pulse_tab(self, tabs, index: int) -> bool:
        if not isinstance(tabs, QTabWidget):
            return False
        try:
            bar = tabs.tabBar()
            if not 0 <= int(index) < bar.count():
                return False
            return self.pulse_widget(bar, bar.tabRect(int(index)))
        except (RuntimeError, TypeError, ValueError):
            return False

    def _discard(self, effect: _AiActionRipple):
        if effect in self._effects:
            self._effects.remove(effect)
