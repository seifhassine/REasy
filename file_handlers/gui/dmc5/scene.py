"""Recovered DMC5 GUI scene layout, draw order, and native projection."""

from __future__ import annotations

import math
from functools import lru_cache

from ..errors import GuiSceneError
from ..native_math import f32 as _f32
from ..native_math import fadd as _fadd
from ..native_math import fdiv as _fdiv
from ..native_math import fmul as _fmul
from ..native_math import fsub as _fsub
from ..scene import GuiResource, GuiScene, GuiSceneNode, GuiWorkspace
from .adapter import DMC5_DEFAULT_SAFE_AREA_RATIO


_ORDER_FIELDS = frozenset({"Priority", "MaskType", "MaskMode"})


class Dmc5GuiScene(GuiScene):
    def __init__(
        self,
        workspace: GuiWorkspace,
        resource: GuiResource,
        root: GuiSceneNode,
    ) -> None:
        super().__init__(workspace, resource, root)
        self.draw_nodes = list(self.nodes)
        self.display_size = (1920.0, 1080.0)
        self.viewport_size = (1920.0, 1080.0)
        self._order_dirty = True
        self._order_override_signature: tuple[
            tuple[str, tuple[tuple[str, str], ...]], ...
        ] = ()

    @property
    def screen_size(self) -> tuple[float, float]:
        value = self.root.properties.get("ScreenSize", (1920.0, 1080.0))
        width, height = _pair(value, (1920.0, 1080.0))
        # View's native setter clamps each ordinary component to at least one.
        return max(1.0, width), max(1.0, height)

    def _invalidate_runtime(self) -> None:
        self._order_dirty = True

    def native_children(
        self,
        node: GuiSceneNode,
        properties_by_path: dict[str, dict[str, object]] | None = None,
    ) -> tuple[GuiSceneNode, ...]:
        """Return children in DMC5's intrusive-list traversal order."""

        overrides = properties_by_path or {}
        decorated = []
        for insertion_order, child in enumerate(node.children, start=1):
            properties = dict(child.properties)
            properties.update(overrides.get(child.path, {}))
            decorated.append((_child_sort_key(properties, insertion_order), child))
        decorated.sort(key=lambda item: item[0])
        return tuple(child for _key, child in decorated)

    def update_preview(
        self,
        overrides: dict[str, dict[str, object]] | None = None,
        *,
        output_size: tuple[float, float] | None = None,
        safe_area_ratio: float = DMC5_DEFAULT_SAFE_AREA_RATIO,
        transient: tuple[GuiSceneNode, float, float] | None = None,
    ) -> None:
        """Lay out a runtime projection without changing authored properties."""

        self._preview_overrides = overrides or {}
        root_override = self._preview_overrides.get(self.root.path, {})
        component_output_size = _pair(
            root_override.get("ScreenSize", self.screen_size),
            self.screen_size,
        )
        component_output_size = tuple(
            max(_f32(1.0), _f32(value)) for value in component_output_size
        )
        self.display_size = output_size or component_output_size
        viewport_size = _logical_viewport_size(
            self.display_size,
            component_output_size,
        )
        self.viewport_size = viewport_size
        transient_node = transient[0] if transient else None

        def visit(
            item: GuiSceneNode,
            parent_world: Matrix4,
            parent_raw: Matrix4,
            parent_visible: bool,
            parent_scale: tuple[float, float, float, float],
            parent_offset: tuple[float, float, float],
            parent_saturation: float,
            safe_area: bool,
        ) -> None:
            properties = dict(item.properties)
            properties.update(self._preview_overrides.get(item.path, {}))
            if item is transient_node and transient is not None:
                position = list(_triple(properties.get("Position"), (0.0, 0.0, 0.0)))
                position[:2] = transient[1:]
                properties["Position"] = position
            item.render_properties = properties
            position = _triple(properties.get("Position"), (0.0, 0.0, 0.0))
            rotation = _triple(properties.get("Rotation"), (0.0, 0.0, 0.0))
            scale = _triple(properties.get("Scale"), (1.0, 1.0, 1.0))
            if item.object.type_name == "via.gui.TextureSet":
                scale = (1.0, 1.0, 1.0)
            raw_local = _local_matrix(position, rotation, scale)
            raw_world = _multiply4(raw_local, parent_raw)
            safe_area = safe_area or bool(properties.get("SafeAreaAdjust", False))
            if bool(properties.get("ResolutionAdjust", False)):
                world = _resolution_world(
                    position,
                    rotation,
                    scale,
                    parent_raw,
                    component_output_size,
                    self.display_size,
                    properties,
                    safe_area,
                    safe_area_ratio,
                )
            elif parent_world is parent_raw:
                world = raw_world
            else:
                world = _multiply4(raw_local, parent_world)
            item.local_position = position[:2]
            item.world_matrix = world
            item.world_transform = _matrix2(world)
            item.world_position = world[3][:2]
            item.effective_visible = parent_visible and bool(properties.get("Visible", True))

            local_scale = tuple(
                _f32(value)
                for value in _quad(
                    properties.get("ColorScale"), (1.0, 1.0, 1.0, 1.0)
                )
            )
            local_offset = tuple(
                _f32(value)
                for value in _triple(
                    properties.get("ColorOffset"), (0.0, 0.0, 0.0)
                )
            )
            try:
                local_saturation = _f32(properties.get("Saturation", 1.0))
            except (TypeError, ValueError):
                local_saturation = 1.0
            if not math.isnan(local_saturation) and local_saturation < 0.0:
                local_saturation = 0.0
            item.color_scale = tuple(
                _fmul(parent_scale[index], local_scale[index])
                for index in range(4)
            )
            item.color_offset = tuple(
                _fadd(
                    _fmul(parent_scale[index], local_offset[index]),
                    _fmul(local_scale[index], parent_offset[index]),
                )
                for index in range(3)
            )
            item.saturation = _fmul(parent_saturation, local_saturation)

            for candidate in ("Size", "RegionSize", "BarFullSize", "ScreenSize"):
                size = _pair(properties.get(candidate), (0.0, 0.0))
                if size[0] > 0 and size[1] > 0:
                    item.size = size
                    break
            else:
                # Containers have no native local draw/hit quad. Every DMC5
                # drawable in the corpus supplies Size or RegionSize, so a
                # fabricated editor rectangle would only corrupt hit routing.
                item.size = (0.0, 0.0)
            item.anchor = _anchor(properties.get("ControlPoint"), item.size)
            for child in item.children:
                visit(
                    child,
                    world,
                    raw_world,
                    item.effective_visible,
                    item.color_scale,
                    item.color_offset,
                    item.saturation,
                    safe_area,
                )

        visit(
            self.root,
            _IDENTITY4,
            _IDENTITY4,
            True,
            (1.0, 1.0, 1.0, 1.0),
            (0.0, 0.0, 0.0),
            1.0,
            False,
        )
        order_override_signature = tuple(
            (
                path,
                tuple(
                    (name, repr(values[name]))
                    for name in sorted(_ORDER_FIELDS.intersection(values))
                ),
            )
            for path, values in sorted(self._preview_overrides.items())
            if _ORDER_FIELDS.intersection(values)
        )
        if (
            self._order_dirty
            or order_override_signature != self._order_override_signature
        ):
            self.draw_nodes = []

            def order(item: GuiSceneNode) -> None:
                self.draw_nodes.append(item)
                children = sorted(
                    enumerate(item.children, start=1),
                    key=lambda pair: _child_sort_key(
                        pair[1].render_properties, pair[0]
                    ),
                )
                for _index, child in children:
                    order(child)

            order(self.root)
            self._order_dirty = False
            self._order_override_signature = order_override_signature

    @staticmethod
    def local_position_for_scene_point(
        node: GuiSceneNode,
        x: float,
        y: float,
    ) -> tuple[float, float]:
        parent = node.parent.world_transform if node.parent else _IDENTITY
        a, b, c, d, tx, ty = parent
        determinant = a * d - b * c
        if abs(determinant) < 1e-8:
            raise GuiSceneError("cannot move a node below a singular parent transform")
        x, y = x - tx, y - ty
        return (d * x - c * y) / determinant, (-b * x + a * y) / determinant

def _pair(value, default: tuple[float, float]) -> tuple[float, float]:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return float(value[0]), float(value[1])
        except (TypeError, ValueError):
            pass
    return default


def _triple(value, default: tuple[float, float, float]) -> tuple[float, float, float]:
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        try:
            return float(value[0]), float(value[1]), float(value[2])
        except (TypeError, ValueError):
            pass
    return default


def _quad(
    value,
    default: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    if isinstance(value, (list, tuple)) and len(value) >= 4:
        try:
            return tuple(float(item) for item in value[:4])
        except (TypeError, ValueError):
            pass
    return default


def _logical_viewport_size(
    display_size: tuple[float, float],
    component_output_size: tuple[float, float],
) -> tuple[float, float]:
    """Convert physical pixels to the logical GUI viewport used by RE Engine."""

    display_width, display_height = _pair(display_size, component_output_size)
    output_height = component_output_size[1]
    display_width = max(1.0, display_width)
    display_height = max(1.0, display_height)
    output_height = max(1.0, output_height)
    return display_width * output_height / display_height, output_height


Matrix4 = tuple[
    tuple[float, float, float, float],
    tuple[float, float, float, float],
    tuple[float, float, float, float],
    tuple[float, float, float, float],
]
_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_IDENTITY4: Matrix4 = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)


@lru_cache(maxsize=16_384)
def _local_matrix(position, rotation, scale) -> Matrix4:
    px, py, pz = (_f32(value) for value in position)
    rx, ry, rz = (_f32(value) for value in rotation)
    scale_x, scale_y, scale_z = (_f32(value) for value in scale)

    def to_radians(value: float) -> float:
        return _fmul(_fmul(value, 0.0055555556900799274), 3.1415927410125732)

    rx, ry, rz = (to_radians(value) for value in (rx, ry, rz))
    sx, sy, sz = (_f32(math.sin(value)) for value in (rx, ry, rz))
    cx, cy, cz = (_f32(math.cos(value)) for value in (rx, ry, rz))
    row0 = (_fmul(cy, cz), _fmul(cy, sz), _f32(-sy), 0.0)
    row1 = (
        _fsub(_fmul(_fmul(sx, sy), cz), _fmul(cx, sz)),
        _fadd(_fmul(_fmul(sx, sy), sz), _fmul(cx, cz)),
        _fmul(sx, cy),
        0.0,
    )
    row2 = (
        _fadd(_fmul(_fmul(cx, sy), cz), _fmul(sx, sz)),
        _fsub(_fmul(_fmul(cx, sy), sz), _fmul(sx, cz)),
        _fmul(cx, cy),
        0.0,
    )
    return (
        tuple(_fmul(scale_x, value) for value in row0),
        tuple(_fmul(scale_y, value) for value in row1),
        tuple(_fmul(scale_z, value) for value in row2),
        (px, py, pz, 1.0),
    )


@lru_cache(maxsize=65_536)
def _multiply4(left: Matrix4, right: Matrix4) -> Matrix4:
    rows = []
    for row in left:
        values = []
        for column in range(4):
            x = _fmul(row[0], right[0][column])
            y = _fmul(row[1], right[1][column])
            z = _fmul(row[2], right[2][column])
            w = _fmul(row[3], right[3][column])
            values.append(_fadd(_fadd(w, z), _fadd(y, x)))
        rows.append(tuple(values))
    return tuple(rows)


def _matrix2(matrix: Matrix4) -> tuple[float, float, float, float, float, float]:
    return (
        matrix[0][0], matrix[0][1], matrix[1][0], matrix[1][1],
        matrix[3][0], matrix[3][1],
    )


def project_gui_point(point, matrix: Matrix4) -> tuple[float, float, float]:
    x, y, z = (_f32(value) for value in point)
    result = tuple(
        _fadd(
            _fadd(_fmul(x, matrix[0][column]), _fmul(y, matrix[1][column])),
            _fadd(_fmul(z, matrix[2][column]), matrix[3][column]),
        )
        for column in range(4)
    )
    if result[3] == 0.0:
        return 0.0, 0.0, 0.0
    inverse = _fdiv(1.0, result[3])
    return tuple(_fmul(value, inverse) for value in result[:3])


_project = project_gui_point


def _resolution_project(point, matrix: Matrix4) -> tuple[float, float, float]:
    """Projection with the scalar ADDSS grouping used by the adjust path."""

    x, y, z = (_f32(value) for value in point)
    result = []
    for column in range(4):
        result.append(
            _fadd(
                _fadd(
                    _fadd(
                        _fmul(x, matrix[0][column]),
                        _fmul(y, matrix[1][column]),
                    ),
                    _fmul(z, matrix[2][column]),
                ),
                matrix[3][column],
            )
        )
    inverse = 0.0 if result[3] == 0.0 else _fdiv(1.0, result[3])
    return tuple(_fmul(value, inverse) for value in result[:3])


def _resolution_world(
    position,
    rotation,
    scale,
    raw_parent: Matrix4,
    screen_size,
    output_size,
    properties,
    safe_area: bool,
    safe_area_ratio: float,
) -> Matrix4:
    ratio = _f32(safe_area_ratio)
    divisor = _f32(1.0) if safe_area else ratio
    source = (
        _fdiv(screen_size[0], _fmul(output_size[0], divisor)),
        _fdiv(screen_size[1], _fmul(output_size[1], divisor)),
    )
    condition = _enum_index(
        properties.get("ResAdjustCondition"),
        {"Always": 0, "Expanding": 1, "Shrinking": 2},
        0,
    )
    policy = _enum_index(
        properties.get("ResAdjustScale"),
        {"None": 0, "Stretch": 1, "FitSmallRatioAxis": 2, "FitLargeRatioAxis": 3},
        1,
    )
    selected = _resolution_scale(source, policy, condition)
    anchor_index = _enum_index(
        properties.get("ResAdjustAnchor"),
        {
            "LeftTop": 0, "LeftCenter": 1, "LeftBottom": 2,
            "CenterTop": 3, "CenterCenter": 4, "CenterBottom": 5,
            "RightTop": 6, "RightCenter": 7, "RightBottom": 8,
        },
        0,
    )
    column, row = divmod(anchor_index, 3)
    anchor = (
        _fmul(screen_size[0], _fmul(float(column), 0.5)),
        _fmul(screen_size[1], _fmul(float(row), 0.5)),
    )
    raw_position = _resolution_project(position, raw_parent)
    adjusted = (
        _fmul(_fsub(raw_position[0], anchor[0]), selected[0]),
        _fmul(_fsub(raw_position[1], anchor[1]), selected[1]),
        raw_position[2],
    )
    local = _local_matrix(
        adjusted,
        rotation,
        (_fmul(selected[0], scale[0]), _fmul(selected[1], scale[1]), scale[2]),
    )
    anchored_parent: Matrix4 = (
        raw_parent[0], raw_parent[1], raw_parent[2], (*anchor, 0.0, 1.0)
    )
    world = _multiply4(local, anchored_parent)
    if safe_area:
        half_margin = _fmul(_fsub(1.0, ratio), 0.5)
        margin = _fmul(screen_size[0], half_margin), _fmul(screen_size[1], half_margin)
        safe_matrix: Matrix4 = (
            (ratio, 0.0, 0.0, 0.0),
            (0.0, ratio, 0.0, 0.0),
            (0.0, 0.0, 1.0, 0.0),
            (*margin, 0.0, 1.0),
        )
        world = _multiply4(world, safe_matrix)
    return world


def _resolution_scale(source, policy: int, condition: int) -> tuple[float, float]:
    x_ratio, y_ratio = (_f32(value) for value in source)
    if policy == 0:
        return x_ratio, y_ratio
    if policy == 1:
        selected = (_f32(1.0), _f32(1.0))
        if condition == 1:
            selected = max(1.0, x_ratio), max(1.0, y_ratio)
        elif condition == 2:
            selected = min(1.0, x_ratio), min(1.0, y_ratio)
        return selected
    axis = max(x_ratio, y_ratio) if policy == 2 else min(x_ratio, y_ratio)
    fitted = _fdiv(x_ratio, axis), _fdiv(y_ratio, axis)
    if condition == 0:
        return fitted
    expands = axis > 1.0
    return (source if expands == (condition == 1) else fitted)


def _enum_index(value, labels: dict[str, int], default: int) -> int:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return labels[value]
        except KeyError as exc:
            raise GuiSceneError(f"unknown GUI enum label {value!r}") from exc
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise GuiSceneError(f"invalid GUI enum value {value!r}") from exc
    if result not in labels.values():
        raise GuiSceneError(f"unknown GUI enum value {result}")
    return result


def _child_sort_key(properties: dict[str, object], insertion_order: int) -> int:
    mask_type = _enum_index(
        properties.get("MaskType"),
        {"Target": 0, "NonTarget": 1, "Mask": 2, "MaskTop": 3, "MaskTopMost": 4},
        0,
    )
    mask_mode = _enum_index(
        properties.get("MaskMode"),
        {"Keep": 0, "Default": 1, "Reverse": 2, "Disable": 3, "ApplyToParent": 4},
        0,
    )
    mask_class = (0, 0, 0x40000000, 0x80000000, 0xC0000000)[
        min(4, max(0, mask_type))
    ]
    category = (0xC0000000 - (mask_class | (0x80000000 if mask_mode & 4 else 0)))
    priority = int(properties.get("Priority", 0)) & 0xFFFF
    return (category | (priority << 17) | (0x7FFF - insertion_order)) & 0xFFFFFFFF


def _anchor(value, size: tuple[float, float]) -> tuple[float, float]:
    control = _enum_index(
        value,
        {
            "LeftTop": 0, "CenterTop": 1, "RightTop": 2,
            "LeftCenter": 4, "CenterCenter": 5, "RightCenter": 6,
            "LeftBottom": 8, "CenterBottom": 9, "RightBottom": 10,
        },
        0,
    )
    horizontal = (0.0, 0.5, 1.0)[control & 3]
    vertical = (0.0, 0.5, 1.0)[control >> 2]
    return size[0] * horizontal, size[1] * vertical
