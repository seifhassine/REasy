from __future__ import annotations

from dataclasses import dataclass, field

lazy import numpy as np

lazy from file_handlers.mmtr.engine_buffers import Camera
lazy from file_handlers.mmtr.textures_view import ProbeLight, VertexLight, ViewLight

lazy from .scene_model import SceneDrawBatch



@dataclass(frozen=True, slots=True, eq=False)
class SceneShape:
    """Unposed; ``positions`` and ``normals`` are n x 3, ``colours`` n x 4."""
    positions: np.ndarray
    normals: np.ndarray
    colours: np.ndarray | None


@dataclass(frozen=True, slots=True, eq=False)
class SceneGeometry:
    """``vertices`` is the index of the first shown mesh with the same vertices, whose shape, UVs and influences it
    shares. ``colour`` is white for meshes with vertex colours. ``solid`` meshes fill in every wireframe mode.
    ``edge_excluded``, ``decal_receive_mode`` and ``transparent_bias`` come from the ``via.render.Mesh`` component."""
    key: str
    vertices: int
    uvs: np.ndarray | None
    colour: tuple[float, float, float, float]
    bones: np.ndarray | None
    weights: np.ndarray | None
    batches: tuple[SceneDrawBatch, ...]
    solid: bool = field(default=False, kw_only=True)
    edge_excluded: bool = field(default=False, kw_only=True)
    decal_receive_mode: int = field(default=0, kw_only=True)
    transparent_bias: float = field(default=0.0, kw_only=True)
    speedtree_placement: str | None = field(default=None, kw_only=True)


@dataclass(frozen=True, slots=True, eq=False)
class ScenePose:
    """``bones`` are 3 x 4 each in remap order, as the game's programs take them; ``joints`` are local matrices, None
    for the file's."""
    bones: np.ndarray | None
    morphs: tuple[tuple[str, float], ...] = ()
    joints: np.ndarray | None = None


@dataclass(frozen=True, slots=True, eq=False)
class ScenePlacement:
    """``shape`` is shared by meshes with the same vertices; ``world`` is 3 x 4; ``pose`` None is the rest pose."""
    shape: SceneShape
    world: np.ndarray
    bones: np.ndarray | None
    pose: ScenePose | None = None


@dataclass(frozen=True, slots=True)
class SceneView:
    """``light`` None is unlit. ``content`` and ``motion`` count changes to the meshes and placements. ``game`` is lit
    by the camera light; ``strands_only`` applies only with ``game``. ``antialiased`` is temporal anti-aliasing."""
    eye: tuple[float, ...]
    scale: tuple[float, float]
    near: float
    far: float
    size: tuple[int, int]
    light: ViewLight | VertexLight | ProbeLight | None
    background: tuple[int, int, int]
    content: int
    motion: int
    hidden: frozenset[str]
    hidden_parts: frozenset[tuple[str, int]]
    game: bool = field(default=False, kw_only=True)
    orthographic: bool = field(default=False, kw_only=True)
    strands_only: bool = field(default=False, kw_only=True)
    antialiased: bool = field(default=False, kw_only=True)

    @classmethod
    def through(cls, camera: Camera, *rest, **named) -> SceneView:
        return cls(tuple(map(float, np.asarray(camera.view, np.float64).reshape(-1))), tuple(map(float, camera.scale)),
                   float(camera.near), float(camera.far), tuple(camera.size), *rest,
                   orthographic=bool(camera.orthographic), **named)

    @property
    def geometry(self) -> int:
        return self.content

    def camera(self) -> Camera:
        return Camera(np.reshape(self.eye, (3, 4)), self.scale, self.near, self.far, self.size,
                      orthographic=self.orthographic)


__all__ = ["SceneGeometry", "ScenePlacement", "ScenePose", "SceneShape", "SceneView"]
