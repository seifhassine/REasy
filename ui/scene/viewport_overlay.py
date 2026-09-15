from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QEvent, QTimer, Qt
from PySide6.QtWidgets import QLabel, QWidget


class ViewportOverlayManager:
    _blocked_drag_widgets = (
        "QAbstractButton",
        "QAbstractItemView",
        "QAbstractSlider",
        "QAbstractSpinBox",
        "QComboBox",
        "QLineEdit",
        "QTextEdit",
    )
    _text_input_widgets = (
        "QAbstractSpinBox",
        "QComboBox",
        "QLineEdit",
        "QTextEdit",
    )

    def __init__(self, view: QWidget, hover_key: Qt.Key | None = None):
        self.view = view
        self.hover_key = hover_key
        self.drag_overlay = self.drag_offset = self.resize_overlay = None
        self._widgets: list[QWidget] = []

    def setup(
        self,
        widget: QWidget,
        body: QWidget | None = None,
        fold_button=None,
    ) -> None:
        if widget not in self._widgets:
            self._widgets.append(widget)
        if body is not None:
            widget._viewport_body = body
        if fold_button is not None:
            widget._viewport_fold_button = fold_button
            fold_button.clicked.connect(lambda: self.toggle_fold(widget))
        grip = QLabel("◢", widget)
        grip.setObjectName("overlayResizeGrip")
        grip.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        grip.setCursor(Qt.SizeFDiagCursor)
        grip.setFixedHeight(12)
        grip.setToolTip(QCoreApplication.translate("ViewportOverlay", "Resize panel"))
        grip._viewport_resize_overlay = widget
        if widget.layout() is not None:
            widget.layout().addWidget(grip)
        for child in (widget, *widget.findChildren(QWidget)):
            child._viewport_drag_overlay = widget
            child.installEventFilter(self.view)
        QTimer.singleShot(0, self.view, self.place)

    def event_filter(self, obj, event):
        kind = event.type()
        overlay = getattr(obj, "_viewport_drag_overlay", None)
        if not overlay:
            return None
        if self._forward_hover_key_event(obj, kind, event):
            return True
        active = self._active_mode(overlay)
        if active and kind == QEvent.Type.MouseMove and event.buttons() & Qt.LeftButton:
            self._continue_drag(active, overlay, event.globalPosition().toPoint())
            return True
        if active and kind == QEvent.Type.MouseButtonRelease:
            overlay.releaseMouse()
            self.resize_overlay = self.drag_overlay = self.drag_offset = None
            return True
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.LeftButton:
            if self._begin_drag(obj, overlay, event.globalPosition().toPoint()):
                return True
        return None

    def _forward_hover_key_event(self, obj, kind, event) -> bool:
        if (
            kind not in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease)
            or self.hover_key is None
            or getattr(self.view, "_controls", "mesh") == "mesh"
            or event.key() != self.hover_key
            or any(obj.inherits(name) for name in self._text_input_widgets)
        ):
            return False
        handler = self.view.keyPressEvent if kind == QEvent.Type.KeyPress else self.view.keyReleaseEvent
        handler(event)
        return True

    def _active_mode(self, overlay) -> str:
        if self.resize_overlay is overlay:
            return "resize"
        if self.drag_overlay is overlay:
            return "drag"
        return ""

    def _continue_drag(self, active: str, overlay, global_pos) -> None:
        if active == "resize":
            self.resize_to(overlay, global_pos)
        else:
            self.move(overlay, self.view.mapFromGlobal(global_pos) - self.drag_offset)

    def _begin_drag(self, obj, overlay, global_pos) -> bool:
        if getattr(obj, "_viewport_resize_overlay", None) is overlay:
            self.resize_overlay = overlay
        elif self.can_drag_from(obj):
            self.drag_overlay = overlay
            self.drag_offset = overlay.mapFromGlobal(global_pos)
        else:
            return False
        overlay.viewport_anchor = "manual"
        overlay.raise_()
        overlay.grabMouse()
        return True

    def can_drag_from(self, widget) -> bool:
        while widget is not None:
            if any(widget.inherits(name) for name in self._blocked_drag_widgets):
                return False
            if getattr(widget, "_viewport_drag_overlay", None) is widget:
                return True
            widget = widget.parentWidget()
        return True

    def move(self, overlay: QWidget, pos) -> None:
        overlay.viewport_anchor = "manual"
        self.place_at(overlay, pos.x(), pos.y(), 4)

    def resize_to(self, overlay: QWidget, global_pos) -> None:
        margin = 4
        local = overlay.mapFromGlobal(global_pos)
        position = overlay.pos()
        max_w = min(overlay.maximumWidth(), self.view.width() - position.x() - margin)
        max_h = min(overlay.maximumHeight(), self.view.height() - position.y() - margin)
        overlay.resize(
            max(overlay.minimumWidth(), min(local.x(), max_w)),
            max(overlay.minimumHeight(), min(local.y(), max_h)),
        )

    def toggle_fold(self, overlay: QWidget) -> None:
        body = getattr(overlay, "_viewport_body", None)
        if body is None:
            return
        folding = not body.isHidden()
        if folding:
            overlay._viewport_expanded_size = overlay.size()
            overlay._viewport_expanded_minimum_size = overlay.minimumSize()
            overlay.setMinimumSize(0, 0)
        body.setVisible(not folding)
        if button := getattr(overlay, "_viewport_fold_button", None):
            button.setText("▸" if folding else "▾")
            button.setToolTip(
                QCoreApplication.translate(
                    "ViewportOverlay",
                    "Expand panel" if folding else "Fold panel",
                )
            )
        if folding:
            overlay.adjustSize()
        else:
            minimum = getattr(overlay, "_viewport_expanded_minimum_size", None)
            if minimum is not None:
                overlay.setMinimumSize(minimum)
            expanded = getattr(overlay, "_viewport_expanded_size", overlay.sizeHint())
            overlay.resize(expanded.expandedTo(overlay.sizeHint()))
        self.place()

    def set_folded(self, overlay: QWidget, folded: bool) -> None:
        body = getattr(overlay, "_viewport_body", None)
        if body is not None and body.isHidden() != bool(folded):
            self.toggle_fold(overlay)

    def place(self) -> None:
        margin = 12
        for widget in tuple(self._widgets):
            anchor = getattr(widget, "viewport_anchor", "")
            if widget is self.view.overlay:
                widget.adjustSize()
            if anchor == "manual":
                self.place_at(widget, widget.x(), widget.y(), margin)
            elif anchor in ("left", "right"):
                body = getattr(widget, "_viewport_body", None)
                preferred = getattr(widget, "viewport_preferred_size", None)
                target_width, target_height = (
                    preferred
                    if preferred is not None and (body is None or body.isVisible())
                    else (widget.width(), widget.height())
                )
                width = min(
                    max(target_width, widget.minimumWidth()),
                    widget.maximumWidth(),
                    self.view.width() - margin * 2,
                )
                height = min(
                    max(target_height, widget.minimumHeight()),
                    widget.maximumHeight(),
                    self.view.height() - margin * 2,
                )
                x = margin if anchor == "left" else max(margin, self.view.width() - width - margin)
                widget.setGeometry(x, margin, width, height)
                widget.raise_()
            elif anchor == "top":
                self.place_at(widget, (self.view.width() - widget.width()) // 2, margin, margin)
            elif widget is self.view.overlay:
                widget.move(margin, margin)
                widget.raise_()

    def place_at(self, widget: QWidget, x: int, y: int, margin: int) -> None:
        widget.move(
            max(margin, min(x, max(margin, self.view.width() - widget.width() - margin))),
            max(margin, min(y, max(margin, self.view.height() - widget.height() - margin))),
        )
        widget.raise_()

    def cleanup(self) -> None:
        self._widgets.clear()
