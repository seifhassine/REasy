"""See docs/internals/gui-rendering.md, World views."""
from __future__ import annotations

import numpy as np

from file_handlers.native_math import fdiv, fmul, fsub
lazy from file_handlers.engine_math import crt, transform_product
lazy from file_handlers.mmtr.engine_buffers import Camera, scene_values


def placements(scene, world, overrides=None) -> tuple:
    profile, view = scene.workspace.profile, scene.runtime_properties(scene.root, overrides)
    return world.places(scene.workspace.root_key, bool(
        profile.enum_label(view, "ViewType") != "Screen" and view["UseGUICamera"]),
        any("mesh" in profile.runtime_type(node.object.type_name).facts for node in scene.nodes))


def placement(scene, world, overrides, chosen: int):
    found = placements(scene, world, overrides)
    return found[chosen] if found else None


def lens_camera(lens, view: np.ndarray, pixels: tuple[int, int]) -> Camera:
    scale = fdiv(1.0, crt("tanf")(fmul(fmul(fmul(lens["fov"], lens["degree"]), lens["pi"]), lens["half"])))
    ratio = fdiv(lens["far"], fsub(lens["far"], lens["near"]))
    depth, far = fsub(ratio, 1.0), fmul(ratio, lens["near"]) / fsub(ratio, 1.0)
    return Camera(view, (scale, fmul(scale, fdiv(*map(float, pixels)))), depth * far / (1.0 + depth), far, pixels)


def gui_camera(catalog, lens, pixels: tuple[int, int]) -> Camera:
    return lens_camera({**catalog.camera, "fov": lens.fov, "near": lens.near, "far": lens.far},
                       np.asarray(lens.view, np.float64).reshape(4, 4).T[:3], pixels)


def gui_view(catalog, screen: tuple[float, float], world) -> np.ndarray:
    shift = np.eye(4, dtype=np.float32)
    shift[3, :2] = -np.float32(screen[0]) / 2, -np.float32(screen[1]) / 2
    axes = np.diag(np.float32([*(axis * catalog.world_view["pixel_scale"] for axis in catalog.world_view["axes"]), 1]))
    return transform_product(transform_product(shift, axes), np.float32(world).reshape(4, 4))


def projection(catalog, placed, screen: tuple[float, float], pixels: tuple[float, float],
               world: np.ndarray | None = None) -> np.ndarray:
    width, height = pixels
    corner = np.array([[width / 2, 0, 0, 0], [0, -height / 2, 0, 0], [0, 0, 1, 0], [width / 2, height / 2, 0, 1]])
    camera = gui_camera(catalog, placed["lens"], tuple(round(side) for side in pixels))
    return (gui_view(catalog, screen, placed["world"]) if world is None else world) @ scene_values(camera)[
        "viewProjMat"] @ corner
