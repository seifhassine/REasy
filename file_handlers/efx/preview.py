"""Deterministic authoring preview; not a reimplementation of RE Engine shaders."""
from dataclasses import dataclass
import math
import random

import numpy as np
from PySide6.QtCore import Qt, QRectF, QTimer, QElapsedTimer
from PySide6.QtGui import QImage, QPainter, QColor, QPen
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QSlider, QComboBox, QSpinBox, QDoubleSpinBox, QLabel


def load_entry_frames(handler, entry, parent=None):
    from utils.resource_file_utils import resolve_handler_resource_data, resource_version_from_path
    from file_handlers.uvs.uvs_file import UvsFile
    from file_handlers.tex.tex_file import TexFile
    from file_handlers.tex.texture_decoder import decode_tex_mip
    uv = next((a.body for a in entry.attributes if a.name == 'UVSequence'), None)
    if uv is None or not uv['UVSPath']:
        return [], 60.
    found = resolve_handler_resource_data(handler, uv['UVSPath'], parent)
    if not found:
        raise ValueError(f"UVS resource is unavailable: {uv['UVSPath']}")
    uvs = UvsFile(); uvs.read(found[1], version=resource_version_from_path(found[0], 'uvs') or 7)
    index = uv['SequenceNo'][0]
    if not 0 <= index < len(uvs.sequences):
        raise ValueError(f'UVS sequence {index} is outside the sequence table')
    textures, frames = {}, []
    for pattern in uvs.sequences[index].patterns:
        if pattern.texture_index not in textures:
            texture_path = uvs.textures[pattern.texture_index].path
            found = resolve_handler_resource_data(handler, texture_path, parent)
            if not found:
                raise ValueError(f'Texture resource is unavailable: {texture_path}')
            tex = TexFile()
            if not tex.read(found[1], file_version=resource_version_from_path(found[0], 'tex') or 28):
                raise ValueError(f'Invalid texture: {texture_path}')
            decoded = decode_tex_mip(tex)
            textures[pattern.texture_index] = np.frombuffer(decoded.rgba, np.uint8).reshape(decoded.height, decoded.width, 4)
        image = textures[pattern.texture_index]; h, w = image.shape[:2]
        x1, y1, x2, y2 = round(pattern.left*w), round(pattern.top*h), round(pattern.right*w), round(pattern.bottom*h)
        if not 0 <= x1 < x2 <= w or not 0 <= y1 < y2 <= h:
            raise ValueError('Preview requires non-repeating UV rectangles inside the texture')
        frames.append(image[y1:y2, x1:x2].copy())
    return frames, uv['PlaySpeed'][0] * 60.


@dataclass(frozen=True)
class PreviewParticle:
    identity: int
    position: tuple
    size: tuple
    alpha: float
    age: float


class ParticlePreview:
    """Analytical point/box/sphere/cylinder emission, linear velocity and fade.

    Ranges are sampled using a deterministic editor convention. Native random,
    interval, gravity, shader, curve and attachment semantics are not emulated.
    """
    def __init__(self, entry, seed=0):
        self.attributes = {a.name: a.body for a in entry.attributes}
        self.seed = seed
        self.limit = 2048

    def get(self, attr, field, default):
        body = self.attributes.get(attr)
        return body[field] if body and field in body.fields else default

    @property
    def shape(self):
        return self.get('EmitterShape3D', 'ShapeType', 0)

    @property
    def extent(self):
        return tuple(abs(self.get('EmitterShape3D', 'Range' + axis, (0., 0.))[0]) for axis in 'XYZ')

    @property
    def tint(self):
        return self.get('TypeBillboard3D', 'Color', 0xffffffff)

    def coverage(self):
        approximate = {'Spawn', 'Life', 'Transform3D', 'EmitterShape3D', 'Velocity3D', 'TypeBillboard3D', 'UVSequence'}
        return {'mode': 'Basic particle approximation', 'approximate': sorted(set(self.attributes) & approximate),
                'unrendered': sorted(set(self.attributes) - approximate), 'particle_limit': self.limit,
                'shader_fidelity': False, 'native_random_fidelity': False}

    def sample(self, frame):
        if not math.isfinite(frame) or frame < 0:
            raise ValueError('Preview time must be finite and nonnegative')
        mean = lambda pair: sum(pair) / 2
        interval = max(1., mean(self.get('Spawn', 'IntervalFrame', (1, 1))))
        count = max(0, int(mean(self.get('Spawn', 'SpawnNum', (1, 1)))))
        delay = max(0., mean(self.get('Spawn', 'EmitterDelayFrame', (0, 0))))
        appear, keep, vanish = (max(0., mean(self.get('Life', name, default))) for name, default in
                                 [('AppearFrame', (0, 0)), ('KeepFrame', (60, 60)), ('VanishFrame', (0, 0))])
        life = appear + keep + vanish
        if life <= 0 or not count or frame < delay:
            return []
        maximum = min(self.limit, self.get('Spawn', 'MaxParticles', self.limit))
        if maximum <= 0:
            return []
        last = int((frame-delay) // interval)
        first = max(0, int(math.floor((frame-delay-life) / interval)) + 1)
        first = max(first, last - math.ceil(maximum / count) + 1)
        origin = self.get('Transform3D', 'LocalPosition', (0., 0., 0.))
        scale = self.get('Transform3D', 'LocalScale', (1., 1., 1.))
        result = []
        for cycle in range(first, last+1):
            for member in range(max(0, count - maximum), count):
                identity = cycle * count + member
                rng = random.Random((self.seed << 32) ^ identity)
                age = frame - delay - cycle * interval
                position = [rng.uniform(-1, 1) for _ in range(3)]
                if self.shape == 1:
                    z, angle = rng.uniform(-1, 1), rng.uniform(0, math.tau)
                    radius = rng.random() ** (1/3); xy = math.sqrt(1-z*z)
                    position = [xy * math.cos(angle) * radius, z * radius, xy * math.sin(angle) * radius]
                elif self.shape == 2:
                    radius, angle = math.sqrt(rng.random()), rng.uniform(0, math.tau)
                    position = [radius * math.cos(angle), position[1], radius * math.sin(angle)]
                direction = [self.get('Velocity3D', 'DirectionVector'+axis, (0., 0.))[0] for axis in 'XYZ']
                norm = math.sqrt(sum(v*v for v in direction))
                speed = self.get('Velocity3D', 'Speed', (0., 0.))[0]
                offset = self.get('Velocity3D', 'Offset', (0., 0., 0.))
                position = tuple(origin[i] + position[i]*self.extent[i]*scale[i] + offset[i] + (direction[i]/norm*speed*age if norm else 0.) for i in range(3))
                scalar = self.get('TypeBillboard3D', 'SizeScalar', (1., 0.))[0]
                size = tuple(abs(scalar * self.get('TypeBillboard3D', 'Size'+axis, (1., 0.))[0] * scale[i]) for i, axis in enumerate('XY'))
                alpha = min(1., age/appear) if appear else 1.
                if vanish and age > appear+keep:
                    alpha *= max(0., (life-age)/vanish)
                result.append(PreviewParticle(identity, position, size, alpha, age))
        return result[-maximum:]


class PreviewCanvas(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent); self.setMinimumSize(400, 260)
        self.frames = []; self.frame = 0.; self.fps = 60.; self.model = None; self.particles = False; self.zoom = 50.

    def set_frames(self, frames):
        self.frames = [QImage(frame.data, frame.shape[1], frame.shape[0], frame.strides[0], QImage.Format_RGBA8888).copy() for frame in frames]
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self); painter.fillRect(self.rect(), QColor('#20262c'))
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.setPen(QColor('#36414b'))
        for x in range(0, self.width(), 32):
            painter.drawLine(x, 0, x, self.height())
        for y in range(0, self.height(), 32):
            painter.drawLine(0, y, self.width(), y)
        if not self.particles:
            if self.frames:
                image = self.frames[int(self.frame*self.fps/60) % len(self.frames)]
                size = image.size().scaled(self.size()*0.9, Qt.KeepAspectRatio)
                painter.drawImage(QRectF((self.width()-size.width())/2, (self.height()-size.height())/2, size.width(), size.height()), image)
            return
        if self.model is None:
            return
        origin = self.model.get('Transform3D', 'LocalPosition', (0., 0., 0.))
        project = lambda p: (self.width()/2+(p[0]-origin[0]+.3*(p[2]-origin[2]))*self.zoom,
                             self.height()*.65-(p[1]-origin[1]+.15*(p[2]-origin[2]))*self.zoom)
        cx, cy = project(origin); ex, ey, _ = self.model.extent
        painter.setPen(QPen(QColor('#59b8a7'), 1, Qt.DashLine)); painter.setBrush(Qt.NoBrush)
        region = QRectF(cx-ex*self.zoom, cy-ey*self.zoom, ex*2*self.zoom, ey*2*self.zoom)
        painter.drawEllipse(region) if self.model.shape else painter.drawRect(region)
        for particle in self.model.sample(self.frame):
            x, y = project(particle.position); w, h = (max(1., n*self.zoom) for n in particle.size)
            painter.setOpacity(particle.alpha)
            if self.frames:
                image = self.frames[int(particle.age*self.fps/60) % len(self.frames)]
                painter.drawImage(QRectF(x-w/2, y-h/2, w, h), image)
            else:
                color = QColor(*int(self.model.tint).to_bytes(4, 'little')); painter.setBrush(color); painter.setPen(Qt.NoPen)
                painter.drawEllipse(QRectF(x-w/2, y-h/2, w, h))


class PreviewPanel(QWidget):
    def __init__(self, entry=None, frames=(), fps=60., parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self); layout.setContentsMargins(0, 0, 0, 0)
        self.canvas = PreviewCanvas(); self.canvas.set_frames(frames); self.canvas.fps = fps
        if entry is not None:
            self.canvas.model = ParticlePreview(entry)
        layout.addWidget(self.canvas)
        controls = QHBoxLayout(); self.play = QPushButton('Play'); self.play.setCheckable(True); controls.addWidget(self.play)
        self.mode = QComboBox(); self.mode.addItems(['Sequence', 'Basic particle approximation']); controls.addWidget(self.mode)
        self.seed = QSpinBox(); self.seed.setRange(0, 1000000); self.seed.setPrefix('Seed '); controls.addWidget(self.seed)
        self.zoom = QDoubleSpinBox(); self.zoom.setRange(.1, 10000); self.zoom.setValue(50); self.zoom.setPrefix('Zoom '); controls.addWidget(self.zoom)
        layout.addLayout(controls)
        self.timeline = QSlider(Qt.Horizontal); self.timeline.setRange(0, 6000); layout.addWidget(self.timeline)
        self.clock = QElapsedTimer(); self.start_frame = 0.
        self.timer = QTimer(self); self.timer.setInterval(16); self.timer.timeout.connect(self._tick)
        self.play.toggled.connect(self._play); self.timeline.valueChanged.connect(self._seek)
        self.mode.currentIndexChanged.connect(self._mode); self.seed.valueChanged.connect(self._seed)
        self.zoom.valueChanged.connect(self._zoom)
        self.time_label = QLabel('0.00 s'); layout.addWidget(self.time_label)
        if entry is not None:
            self.coverage = QLabel(', '.join(self.canvas.model.coverage()['unrendered'])); self.coverage.setWordWrap(True)
            self.coverage.setToolTip('Attributes outside preview coverage'); layout.addWidget(self.coverage)
        if entry is not None and not frames:
            self.mode.setCurrentIndex(1)

    def _play(self, enabled):
        self.play.setText('Pause' if enabled else 'Play')
        self.start_frame = self.canvas.frame; self.clock.start()
        self.timer.start() if enabled else self.timer.stop()

    def _tick(self):
        self.canvas.frame = (self.start_frame + self.clock.elapsed() * .06) % self.timeline.maximum()
        self.timeline.blockSignals(True); self.timeline.setValue(int(self.canvas.frame)); self.timeline.blockSignals(False)
        self.time_label.setText(f'{self.canvas.frame/60:.2f} s'); self.canvas.update()

    def _seek(self, value):
        self.canvas.frame = float(value); self.start_frame = float(value); self.clock.restart()
        self.time_label.setText(f'{value/60:.2f} s'); self.canvas.update()

    def _mode(self, index):
        self.canvas.particles = index == 1; self.canvas.update()

    def _seed(self, value):
        if self.canvas.model:
            self.canvas.model.seed = value; self.canvas.update()

    def _zoom(self, value):
        self.canvas.zoom = value; self.canvas.update()
