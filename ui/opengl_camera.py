from __future__ import annotations

import time

from PySide6.QtCore import QCoreApplication, Qt


class OrbitCameraMixin:
    def _init_orbit_camera(self, *, rot_x: float, rot_y: float, distance: float) -> None:
        self.rot_x = rot_x
        self.rot_y = rot_y
        self.distance = distance
        self.last_pos = None
        self.fps = 0.0
        self._frame_count = 0
        self._last_time = time.time()

    def _record_frame(self) -> None:
        now = time.time()
        self._frame_count += 1
        elapsed = now - self._last_time
        if elapsed >= 1.0:
            self.fps = self._frame_count / elapsed
            self._frame_count = 0
            self._last_time = now
            self.fps_label.setText(
                QCoreApplication.translate("OrbitCameraMixin", "{fps:.1f} FPS").format(
                    fps=self.fps
                )
            )

    def _update_after_camera_change(self) -> None:
        self.update()

    def mousePressEvent(self, event):
        self.last_pos = event.position()

    def mouseMoveEvent(self, event):
        if self.last_pos is None:
            return
        dx = event.position().x() - self.last_pos.x()
        dy = event.position().y() - self.last_pos.y()
        if event.buttons() & Qt.LeftButton:
            self.rot_x += dy * 0.5
            self.rot_y += dx * 0.5
            self._update_after_camera_change()
        self.last_pos = event.position()

    def wheelEvent(self, event):
        self.distance *= 0.9 ** (event.angleDelta().y() / 120.0)
        self._update_after_camera_change()
