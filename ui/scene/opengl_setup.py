"""Shared setup for OpenGL-backed Qt widgets."""

from PySide6.QtGui import QSurfaceFormat
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QWidget


def mesh_surface_format() -> QSurfaceFormat:
    """Return the compatibility format used by the mesh and scene renderers."""
    fmt = QSurfaceFormat()
    fmt.setDepthBufferSize(24)
    fmt.setAlphaBufferSize(0)
    fmt.setSwapInterval(0)
    fmt.setVersion(2, 1)
    fmt.setProfile(QSurfaceFormat.CompatibilityProfile)
    fmt.setRenderableType(QSurfaceFormat.OpenGL)
    return fmt


def configure_default_surface_format() -> None:
    """Configure Qt before any OpenGL widget or context is created."""
    QSurfaceFormat.setDefaultFormat(mesh_surface_format())


def create_surface_anchor(parent: QWidget) -> QOpenGLWidget:
    """Keep a top-level window OpenGL-composited from its first show.

    Qt 6.4+ may recreate an already visible native window when its first
    QOpenGLWidget is added. A hidden child created before the window is shown
    selects OpenGLSurface without initializing its own context or framebuffer.
    """
    anchor = QOpenGLWidget(parent)
    anchor.setObjectName("openGLSurfaceAnchor")
    anchor.hide()
    return anchor
