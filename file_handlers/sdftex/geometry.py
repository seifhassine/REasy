from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable

lazy import numpy as np

lazy from utils.native_build import ensure_fastsdftex

lazy from .sdftex_file import Bc4Blocks, DistanceLevel, SdfTexFile, StorageFormat, bc4_palette

MAX_VOXELS = 4_194_304
MAX_TRIANGLES = 1_000_000


class BakeCancelled(Exception):
    pass


@dataclass(frozen=True)
class Grid:
    center: tuple[float, float, float]
    dimensions: tuple[int, int, int]  # mesh XYZ, not texture XYZ
    voxel_size: float

    def __post_init__(self):
        if len(self.center) != 3 or not np.isfinite(self.center).all():
            raise ValueError("The mesh must have finite declared bounds")
        if len(self.dimensions) != 3 or any(int(n) != n or n < 1 for n in self.dimensions):
            raise ValueError("Distance grid dimensions must be positive integers")
        if not math.isfinite(self.voxel_size) or self.voxel_size <= 0:
            raise ValueError("Voxel size must be positive and finite")
        with np.errstate(over='ignore', under='ignore'):
            spacing = float(np.float32(self.voxel_size))
        if not math.isfinite(spacing) or spacing <= 0:
            raise ValueError("Voxel size must be representable as a positive float32")
        object.__setattr__(self, 'voxel_size', spacing)

    @property
    def first_sample(self):
        # OBB half extent = dimensions * voxel_size / 2; samples are at texel centers.
        return np.asarray(self.center) - (np.asarray(self.dimensions) - 1) * self.voxel_size / 2

    @property
    def count(self):
        return math.prod(self.dimensions)

    @classmethod
    def for_level(cls, level: DistanceLevel, center):
        return cls(tuple(center), level.logical_dimensions, level.voxel_size)


def mesh_bounds(mesh):
    layout = getattr(getattr(mesh, 'mesh_buffer', None), 'meshlet_layout', None)
    if layout is not None:
        bounds = layout.header.min_aabb, layout.header.max_aabb
    elif mesh.meshes:
        bounds = mesh.meshes[0].bounding_box
    else:
        raise ValueError("This mesh has no declared primary bounds")
    low, high = (np.asarray(v[:3], dtype=np.float64) for v in bounds)
    if not np.isfinite([low, high]).all() or np.any(high < low):
        raise ValueError("This mesh has invalid declared primary bounds")
    return low, high


def geometry_bounds_at_native_center(bounds, vertices):
    low, high = bounds
    center = (low+high)/2
    vertices = np.asarray(vertices)
    if not len(vertices) or not np.isfinite(vertices).all():
        raise ValueError("No finite source geometry to bake")
    extent = np.maximum((high-low)/2, np.max(np.abs(vertices-center), axis=0))
    return center-extent, center+extent


def covering_grid(bounds, voxel_size: float, fmt: StorageFormat, rotation=0, padding=2):
    low, high = (np.asarray(v, dtype=np.float64) for v in bounds)
    if (not math.isfinite(voxel_size) or voxel_size <= 0 or rotation not in (0, 1, 2)
            or not np.isfinite([low, high]).all() or np.any(high < low) or padding < 1):
        raise ValueError("Invalid bake bounds, voxel size, rotation or padding")
    spacing = Grid(tuple((low+high)/2), (1,1,1), voxel_size).voxel_size
    dimensions = np.maximum(2, np.ceil((high-low) / spacing) + 2*padding)
    if not np.isfinite(dimensions).all() or np.any(dimensions > MAX_VOXELS):
        raise ValueError("Requested grid exceeds the bake memory limit; use larger voxels")
    dims = dimensions.astype(np.int64)
    texture_axes = ((0, 1, 2), (0, 2, 1), (1, 2, 0))[rotation]
    if fmt == StorageFormat.BC4_UNORM:
        for axis in texture_axes[:2]:
            dims[axis] = (dims[axis]+3)//4*4
    return Grid(tuple((low+high)/2), tuple(map(int, dims)), float(voxel_size))


def encode_bc4(samples: np.ndarray) -> Bc4Blocks:
    depth, height, width = samples.shape
    if height % 4 or width % 4:
        raise ValueError("BC4 width and height must be multiples of four")
    blocks = samples.reshape(depth, height//4, 4, width//4, 4).transpose(0, 1, 3, 2, 4).reshape(-1, 16)
    endpoints = np.empty((len(blocks), 2), dtype=np.uint8)
    selectors = np.empty((len(blocks), 16), dtype=np.uint8)
    for start in range(0, len(blocks), 4096):
        values = blocks[start:start+4096]
        low, high = values.min(axis=1), values.max(axis=1)
        best_error = np.full(len(values), np.inf)
        for short in (False, True):
            # Short palettes encode exact 0 and 1; fit the other six to interior samples.
            a, b = high, low
            if short:
                a = np.where(values > 0, values, np.inf).min(axis=1)
                b = np.where(values < 1, values, -np.inf).max(axis=1)
                a, b = np.where(np.isfinite(a), a, low), np.where(np.isfinite(b), b, high)
                a, b = np.minimum(a, b), np.maximum(a, b)
            ep = np.rint(np.stack((a, b), axis=-1)*255).clip(0, 255).astype(np.uint8)
            errors = (values[:, :, None]-bc4_palette(ep)[:, None, :])**2
            indices = errors.argmin(axis=2).astype(np.uint8)
            error = np.take_along_axis(errors, indices[:, :, None], axis=2).sum(axis=(1, 2))
            better = error < best_error
            endpoints[start:start+len(values)][better] = ep[better]
            selectors[start:start+len(values)][better] = indices[better]
            best_error[better] = error[better]
    shape = (depth, height//4, width//4)
    return Bc4Blocks(endpoints.reshape(*shape, 2), selectors.reshape(*shape, 16))


def encode_distances(distances, voxel_size, fmt, rotation=0) -> DistanceLevel:
    values = np.asarray(distances, dtype=np.float32)
    if values.ndim != 3 or not values.size or not np.isfinite(values).all():
        raise ValueError("Distance samples must be a nonempty finite ZYX array")
    if rotation not in (0, 1, 2):
        raise ValueError("Unsupported SDFTEX rotation")
    # Inverse of DistanceLevel.distances(mesh_axes=True).
    texture = np.ascontiguousarray(values.transpose(((0, 1, 2), (1, 0, 2), (2, 0, 1))[rotation]))
    low, high = float(texture.min()), float(texture.max())
    if fmt == StorageFormat.R16_FLOAT:
        if max(abs(low), abs(high)) > np.finfo(np.float16).max:
            raise ValueError("Distances exceed R16_FLOAT range; no bake was applied")
        encoded = texture.astype(np.float16)
        low = high = 0.0
    else:
        if high-low > np.finfo(np.float32).max:
            raise ValueError("Distance range exceeds the runtime's float32 correction scale")
        normalized = (texture-low)/(high-low) if high > low else np.zeros_like(texture)
        if fmt == StorageFormat.R8_UNORM:
            encoded = np.rint(normalized*255).clip(0, 255).astype(np.uint8)
        elif fmt == StorageFormat.BC4_UNORM:
            encoded = encode_bc4(normalized)
        else:
            raise ValueError(f"Unsupported SDFTEX storage format {fmt}")
    return DistanceLevel(float(voxel_size), tuple(map(int, texture.shape[::-1])), fmt,
                         encoded, low, high, rotation)


def triangle_geometry(vertices, indices, *, solid: bool):
    points = np.asarray(vertices, dtype=np.float64)
    faces = np.asarray(indices)
    if (points.ndim != 2 or points.shape[1] != 3 or not np.isfinite(points).all()
            or faces.dtype.kind not in 'ui' or faces.size % 3 or not faces.size
            or np.any(faces < 0) or np.any(faces >= len(points))):
        raise ValueError("Baking requires finite vertices and valid triangle indices")
    faces = faces.reshape(-1, 3)
    if len(faces) > MAX_TRIANGLES:
        raise ValueError(f"Bake exceeds the {MAX_TRIANGLES:,}-triangle memory limit")
    points, inverse = np.unique(points, axis=0, return_inverse=True)
    faces = inverse[faces]
    tri = points[faces]
    keep = np.linalg.norm(np.cross(tri[:, 1]-tri[:, 0], tri[:, 2]-tri[:, 0]), axis=1) > 0
    faces = faces[keep]
    _, unique = np.unique(np.sort(faces, axis=1), axis=0, return_index=True)
    faces = faces[np.sort(unique)]
    if not len(faces):
        raise ValueError("No nondegenerate triangles to bake")
    if solid:
        edges = np.concatenate((faces[:, (0, 1)], faces[:, (1, 2)], faces[:, (2, 0)]))
        unordered, inverse, counts = np.unique(np.sort(edges, axis=1), axis=0, return_inverse=True, return_counts=True)
        direction = np.bincount(inverse, weights=np.where(edges[:, 0] < edges[:, 1], 1, -1), minlength=len(unordered))
        if np.any(counts != 2) or np.any(direction != 0):
            raise ValueError("Solid baking needs a closed, consistently oriented mesh. "
                             "Repair the topology or explicitly choose a shell thickness.")
    return np.ascontiguousarray(points[faces])


def bake_levels(vertices, indices, specs, *, center, resource_version, shadow_offset_scale=1.0,
                shell_thickness=0.0, progress: Callable[[int, int], None] | None = None,
                cancelled: Callable[[], bool] | None = None) -> tuple[SdfTexFile, list[float]]:
    fastsdftex = ensure_fastsdftex()

    if not math.isfinite(shell_thickness) or shell_thickness < 0:
        raise ValueError("Shell thickness must be finite and nonnegative")
    specs = list(specs)
    if not specs or sum(grid.count for grid, _, _ in specs) > MAX_VOXELS:
        raise ValueError(f"Bake exceeds the {MAX_VOXELS:,}-voxel memory limit (all quality levels combined)")
    if any(not np.allclose(grid.center, center, rtol=0, atol=1e-8) for grid, _, _ in specs):
        raise ValueError("All levels must be centered on the mesh's declared bounds")

    def check_cancel():
        if cancelled and cancelled():
            raise BakeCancelled()

    check_cancel()
    triangles = triangle_geometry(vertices, indices, solid=shell_thickness == 0)
    check_cancel()
    tree = fastsdftex.build(triangles)
    del triangles
    total = sum(grid.count for grid, _, _ in specs)
    completed = 0
    output = SdfTexFile(resource_version, shadow_offset_scale=shadow_offset_scale)
    errors = []
    for grid, fmt, rotation in specs:
        samples = np.empty(grid.count, dtype=np.float32)
        width, height, depth = grid.dimensions
        for start in range(0, grid.count, 8192):
            check_cancel()
            offsets = np.arange(start, min(start+8192, grid.count), dtype=np.int64)
            points = np.column_stack((offsets % width, offsets//width % height, offsets//(width*height)))
            points = np.ascontiguousarray(grid.first_sample+points*grid.voxel_size)
            values = np.frombuffer(fastsdftex.sample(tree, points, shell_thickness == 0), dtype=np.float32)
            samples[start:start+len(values)] = values-shell_thickness/2
            completed += len(values)
            if progress:
                progress(completed, total)
        check_cancel()
        samples = samples.reshape(depth, height, width)
        level = encode_distances(samples, grid.voxel_size, fmt, rotation)
        errors.append(float(np.max(np.abs(level.distances()-samples))))
        output.levels.append(level)
    output.validate()
    check_cancel()
    return output, errors


def surface_mesh(level: DistanceLevel, center, *, stride=1, triangle_limit=500_000, cancelled=None):
    grid = Grid.for_level(level, center)
    if not isinstance(stride, int) or stride < 1:
        raise ValueError("Preview stride must be a positive integer")
    values = level.distances()[::stride, ::stride, ::stride]
    if any(n < 2 for n in values.shape):
        return np.empty((0, 3), np.float32), np.empty((0, 3), np.uint32)
    corners = np.array(((0,0,0),(1,0,0),(0,1,0),(1,1,0),(0,0,1),(1,0,1),(0,1,1),(1,1,1)))
    tetrahedra = ((0,1,3,7),(0,3,2,7),(0,2,6,7),(0,6,4,7),(0,4,5,7),(0,5,1,7))
    pieces, count = [], 0
    depth, height, width = values.shape
    for z in range(depth-1):
        if cancelled and cancelled():
            raise BakeCancelled()
        yz, xz = np.mgrid[:height-1, :width-1]
        cells = np.column_stack((xz.ravel(), yz.ravel(), np.full(xz.size, z)))
        cube = np.stack([values[z+dz, dy:dy+height-1, dx:dx+width-1].ravel() for dx,dy,dz in corners], axis=1)
        crossing = (cube.min(axis=1) < 0) & (cube.max(axis=1) >= 0)
        cube, cells = cube[crossing], cells[crossing]
        for tetra in tetrahedra:
            samples = cube[:, tetra]
            masks = ((samples < 0) * np.array((1,2,4,8))).sum(axis=1)
            for mask in range(1,15):
                use = masks == mask
                if not use.any():
                    continue
                negative = [i for i in range(4) if mask & (1 << i)]
                positive = [i for i in range(4) if not mask & (1 << i)]
                pairs = [(a,b) for a in negative for b in positive]
                points = []
                for a,b in pairs:
                    t = samples[use,a]/(samples[use,a]-samples[use,b])
                    ca,cb = corners[tetra[a]], corners[tetra[b]]
                    points.append(cells[use]+ca+t[:,None]*(cb-ca))
                points = np.stack(points,axis=1)
                order = ((0,1,2),(1,3,2)) if len(pairs)==4 else ((0,1,2),)
                direction = corners[np.array(tetra)[positive]].mean(axis=0)-corners[np.array(tetra)[negative]].mean(axis=0)
                for face in order:
                    triangles = points[:,face,:].copy()
                    normal = np.cross(triangles[:,1]-triangles[:,0],triangles[:,2]-triangles[:,0])
                    flip = normal @ direction < 0
                    triangles[flip] = triangles[flip][:,(0,2,1),:]
                    count += len(triangles)
                    if count > triangle_limit:
                        raise ValueError("Surface preview exceeds its triangle budget; increase preview stride")
                    pieces.append((grid.first_sample+triangles*grid.voxel_size*stride).astype(np.float32))
    vertices = np.concatenate(pieces).reshape(-1,3) if pieces else np.empty((0,3),np.float32)
    return vertices, np.arange(len(vertices),dtype=np.uint32).reshape(-1,3)
