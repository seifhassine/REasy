from __future__ import annotations

import time

import numpy as np
from PySide6.QtCore import Qt


class FreecamController:
    MOD_KEYS = {Qt.Key_Shift, Qt.Key_Control}
    SCANCODES = {17: "forward", 31: "back", 30: "left", 32: "right", 16: "down", 18: "up"}
    TEXT_FALLBACK = {"w": "forward", "s": "back", "a": "left", "d": "right", "q": "down", "e": "up"}

    def __init__(self):
        self.pos = np.zeros(3, dtype=np.float32)
        self.yaw = 0.0
        self.pitch = -10.0
        self.base_speed = self.speed = 1.0
        self.keys: set[str] = set()
        self.mods: set[int] = set()
        self.last_time = time.perf_counter()

    def reset(self, center: np.ndarray, extent: float, speed_scale: float) -> None:
        self.yaw, self.pitch = 0.0, -10.0
        self.base_speed = max(float(extent) * 0.6, 1.0)
        self.speed = self.base_speed * speed_scale
        self.pos = center + np.array((0.0, extent * 0.2, extent * 1.4), dtype=np.float32)
        self.last_time = time.perf_counter()

    def focus(self, center: np.ndarray, extent: float) -> None:
        forward = self.rotation() @ np.array((0.0, 0.0, -1.0), dtype=np.float32)
        forward /= np.linalg.norm(forward) or 1.0
        self.pos = center - forward * max(float(extent) * 1.8, 1.0)
        self.last_time = time.perf_counter()

    def rotation(self) -> np.ndarray:
        yaw, pitch = np.deg2rad(self.yaw), np.deg2rad(self.pitch)
        cy, sy, cp, sp = np.cos(yaw), np.sin(yaw), np.cos(pitch), np.sin(pitch)
        return (
            np.array(((cy, 0.0, sy), (0.0, 1.0, 0.0), (-sy, 0.0, cy)), dtype=np.float32)
            @ np.array(((1.0, 0.0, 0.0), (0.0, cp, -sp), (0.0, sp, cp)), dtype=np.float32)
        )

    def move_local(self, local_delta: np.ndarray) -> None:
        self.pos += self.rotation() @ local_delta

    def look(self, dx: float, dy: float, sensitivity: float) -> None:
        self.yaw -= dx * sensitivity
        self.pitch = max(-80.0, min(80.0, self.pitch - dy * sensitivity))

    def key_action(self, event) -> str:
        try:
            return self.SCANCODES.get(int(event.nativeScanCode()), "") or self.TEXT_FALLBACK.get((event.text() or "").lower(), "")
        except Exception:
            return self.TEXT_FALLBACK.get((event.text() or "").lower(), "")

    def step(self, boost: float, slow: float) -> bool:
        now = time.perf_counter()
        dt, self.last_time = min(now - self.last_time, 0.05), now
        if not self.keys:
            return False
        keys = self.keys
        move = np.array((("right" in keys) - ("left" in keys), ("up" in keys) - ("down" in keys), ("back" in keys) - ("forward" in keys)), dtype=np.float32)
        length = float(np.linalg.norm(move))
        if length <= 1e-6:
            return False
        speed = self.speed * (boost if Qt.Key_Shift in self.mods else 1.0) * (slow if Qt.Key_Control in self.mods else 1.0)
        self.move_local(move / length * speed * dt)
        return True

    def update_speed(self, scale: float) -> None:
        self.speed = self.base_speed * scale

    def clear_input(self) -> None:
        self.keys.clear()
        self.mods.clear()
