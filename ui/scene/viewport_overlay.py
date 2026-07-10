from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QLabel, QWidget


class ViewportOverlayManager:
    _blocked_drag_widgets = ("QAbstractButton", "QAbstractSpinBox", "QComboBox", "QAbstractItemView", "QTextEdit", "QScrollBar")

    def __init__(self, view: QWidget, hover_key: Qt.Key | None = None):
        self.view = view
        self.hover_key = hover_key
        self.drag_overlay = self.drag_offset = self.resize_overlay = None

    def setup(self, widget: QWidget, body: QWidget | None = None, fold_button=None) -> None:
        if body is not None:
            widget._viewport_body = body
        if fold_button is not None:
            widget._viewport_fold_button = fold_button
            fold_button.clicked.connect(lambda: self.toggle_fold(widget))
        grip = QLabel("///", widget)
        grip.setObjectName("overlayResizeGrip")
        grip.setAlignment(Qt.AlignRight)
        grip.setCursor(Qt.SizeFDiagCursor)
        grip.setFixedHeight(20)
        grip.setToolTip("Resize panel")
        grip._viewport_resize_overlay = widget
        if widget.layout() is not None:
            widget.layout().addWidget(grip)
        for child in (widget, *widget.findChildren(QWidget)):
            child._viewport_drag_overlay = widget
            child.installEventFilter(self.view)

    def event_filter(self, obj, event):
        overlay = getattr(obj, "_viewport_drag_overlay", None)
        if not overlay:
            return None
        kind = event.type()
        if kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease) and self.hover_key is not None and getattr(self.view, "_controls", "mesh") != "mesh" and event.key() == self.hover_key:
            (self.view.keyPressEvent if kind == QEvent.Type.KeyPress else self.view.keyReleaseEvent)(event)
            return True
        active = "resize" if self.resize_overlay is overlay else "drag" if self.drag_overlay is overlay else ""
        if active and kind == QEvent.Type.MouseMove and event.buttons() & Qt.LeftButton:
            self.resize_to(overlay, event.globalPosition().toPoint()) if active == "resize" else self.move(overlay, self.view.mapFromGlobal(event.globalPosition().toPoint()) - self.drag_offset)
            return True
        if active and kind == QEvent.Type.MouseButtonRelease:
            overlay.releaseMouse()
            self.resize_overlay = self.drag_overlay = self.drag_offset = None
            return True
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.LeftButton:
            if getattr(obj, "_viewport_resize_overlay", None) is overlay:
                self.resize_overlay = overlay
            elif self.can_drag_from(obj):
                self.drag_overlay = overlay
                self.drag_offset = overlay.mapFromGlobal(event.globalPosition().toPoint())
            else:
                return None
            overlay.viewport_anchor = "manual"
            overlay.raise_()
            overlay.grabMouse()
            return True
        return None

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
        max_w = min(overlay.maximumWidth(), self.view.width() - overlay.x() - margin)
        max_h = min(overlay.maximumHeight(), self.view.height() - overlay.y() - margin)
        overlay.resize(max(overlay.minimumWidth(), min(local.x(), max_w)), max(overlay.minimumHeight(), min(local.y(), max_h)))

    def toggle_fold(self, overlay: QWidget) -> None:
        body = getattr(overlay, "_viewport_body", None)
        if body is None:
            return
        body.setVisible(not body.isVisible())
        if button := getattr(overlay, "_viewport_fold_button", None):
            button.setText(">" if not body.isVisible() else "v")
        overlay.resize(max(overlay.width(), overlay.sizeHint().width()), max(overlay.height(), overlay.sizeHint().height())) if body.isVisible() else overlay.adjustSize()
        self.place()

    def place(self) -> None:
        margin = 12
        for widget in (self.view.overlay, *(child for child in self.view.children() if child is not self.view.overlay)):
            if not isinstance(widget, QWidget):
                continue
            anchor = getattr(widget, "viewport_anchor", "")
            if widget is self.view.overlay:
                widget.adjustSize()
            if anchor == "manual":
                self.place_at(widget, widget.x(), widget.y(), margin)
            elif anchor == "right":
                width = min(max(widget.width(), widget.minimumWidth()), widget.maximumWidth(), self.view.width() - margin * 2)
                height = min(max(widget.height(), widget.minimumHeight()), widget.maximumHeight(), self.view.height() - margin * 2)
                widget.setGeometry(max(margin, self.view.width() - width - margin), margin, width, height)
                widget.raise_()
            elif widget is self.view.overlay:
                widget.move(margin, margin)
                widget.raise_()

    def place_at(self, widget: QWidget, x: int, y: int, margin: int) -> None:
        widget.move(
            max(margin, min(x, max(margin, self.view.width() - widget.width() - margin))),
            max(margin, min(y, max(margin, self.view.height() - widget.height() - margin))),
        )
        widget.raise_()
