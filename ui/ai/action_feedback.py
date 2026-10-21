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
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QAbstractItemView, QTabWidget, QWidget

lazy from ui.styles import current_colors


_RIPPLE_DURATION_MS = 480
# Share of the flash spent widening from the changed point to the full target.
_RIPPLE_REVEAL = 0.3
_MAX_ACTIVE_EFFECTS = 48
_EDITING_GLOW_CYCLE_MS = 2_400
_EDITING_GLOW_MINIMUM_MS = 700
_EDITING_GLOW_WIDTH = 2


class _AiActionRipple(QWidget):

    def __init__(
        self,
        host: QWidget,
        target: QRect,
        accent: QColor,
        center: QPoint | None = None,
    ):
        super().__init__(host)
        bounds = target.adjusted(-2, -2, 2, 2).intersected(host.rect())
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
        reveal = 1.0 - (1.0 - min(1.0, self._progress / _RIPPLE_REVEAL)) ** 2
        target = self._target
        center_x = min(max(self._center.x(), target.left()), target.right())
        left = center_x - (center_x - target.left()) * reveal
        right = center_x + (target.right() - center_x) * reveal
        flash = QRectF(left, target.top(), max(1.0, right - left), target.height())

        wash = QColor(self._accent)
        wash.setAlpha(round(48 * remaining))
        outline = QColor(self._accent)
        outline.setAlpha(round(220 * remaining))
        painter.setPen(QPen(outline, 1.0))
        painter.setBrush(wash)
        painter.drawRoundedRect(flash.adjusted(0.5, 0.5, -0.5, -0.5), 3.0, 3.0)


class _AiEditingGlow(QWidget):

    def __init__(self, host: QWidget, accent: QColor):
        super().__init__(host)
        self._host = host
        self._accent = QColor(accent)
        self._intensity = 1.0
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
        self._animation.setStartValue(1.0)
        self._animation.setKeyValueAt(0.5, 0.7)
        self._animation.setEndValue(1.0)
        self._animation.setDuration(_EDITING_GLOW_CYCLE_MS)
        self._animation.setLoopCount(-1)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._animation.valueChanged.connect(self._set_intensity)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._hide_if_idle)
        host.installEventFilter(self)

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

    def paintEvent(self, event):
        del event
        edge = _EDITING_GLOW_WIDTH
        width, height = self.width(), self.height()
        if width <= 2 * edge or height <= 2 * edge:
            return
        # Opaque, so the frame looks the same over native viewport windows.
        base = QColor(current_colors()["gutter"])
        accent = self._accent
        strength = self._intensity
        color = QColor(
            round(base.red() + (accent.red() - base.red()) * strength),
            round(base.green() + (accent.green() - base.green()) * strength),
            round(base.blue() + (accent.blue() - base.blue()) * strength),
        )
        painter = QPainter(self)
        painter.fillRect(0, 0, width, edge, color)
        painter.fillRect(0, height - edge, width, edge, color)
        painter.fillRect(0, edge, edge, height - 2 * edge, color)
        painter.fillRect(width - edge, edge, edge, height - 2 * edge, color)


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
        return QColor(current_colors()["accent"])

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

    @property
    def editing_active(self) -> bool:
        glow = self._editing_glow
        return bool(glow._requested_active) if glow is not None else False

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
