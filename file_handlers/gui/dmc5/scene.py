"""Recovered DMC5 GUI scene layout, draw order, and native projection."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace
from functools import lru_cache

import numpy as np

lazy from ..errors import GuiSceneError
lazy from file_handlers.engine_math import crt
lazy from file_handlers.native_math import f32 as _f32
lazy from file_handlers.native_math import fadd as _fadd
lazy from file_handlers.native_math import fdiv as _fdiv
lazy from file_handlers.native_math import fmul as _fmul
lazy from file_handlers.native_math import fsub as _fsub
from ..scene import GuiResource, GuiScene, GuiSceneNode, GuiWorkspace


_ORDER_FIELDS = frozenset({"Priority", "MaskType", "MaskMode", "Segment"})


class Dmc5GuiScene(GuiScene):
    def __init__(self, workspace: GuiWorkspace, resource: GuiResource, root: GuiSceneNode) -> None:
        super().__init__(workspace, resource, root)
        self.draw_nodes = self.tree_nodes = list(self.nodes)
        self.display_size = self.viewport_size = self.screen_size
        self.withheld: set[str] = set()
        self.held_segment: Callable[[], int | None] = lambda: None
        self._order_dirty = True
        self._order_override_signature: tuple[
            tuple[str, tuple[tuple[str, str], ...]], ...
        ] = ()

    @property
    def screen_size(self) -> tuple[float, float]:
        width, height = self.runtime_properties(self.root)["ScreenSize"]
        # View's native setter clamps each ordinary component to at least one.
        return max(1.0, width), max(1.0, height)

    def _invalidate_runtime(self) -> None:
        self._order_dirty = True

    def clone(self, source: GuiSceneNode, name: str) -> GuiSceneNode:
        owner = source.parent
        held, base = {child.path for child in owner.children}, f"{owner.path.rstrip('/')}/{name}"
        path, suffix = base, 2
        while path in held:
            path, suffix = f"{base}#{suffix}", suffix + 1

        def build(node: GuiSceneNode, parent: GuiSceneNode, path: str, obj) -> GuiSceneNode:
            made = GuiSceneNode(path, node.resource, obj, node.prototype, node.runtime, parent,
                                properties=dict(node.properties), source=node)
            made.children = [build(child, made, f"{path}/{child.path.rsplit('/', 1)[1]}", child.object)
                             for child in node.children]
            return made

        made = build(source, owner, path, replace(source.object, name=name))
        owner.children.append(made)
        self._order_dirty = True
        return made

    def drop_clones(self, under: GuiSceneNode | None = None) -> list[GuiSceneNode]:
        dropped = []
        for node in self.nodes if under is None else [item for item in under.walk() if item.source is None]:
            dropped += [child for child in node.children if child.source is not None]
            node.children = [child for child in node.children if child.source is None]
        self._order_dirty = True
        return dropped

    def runtime_properties(self, item: GuiSceneNode, overrides: dict[str, object] | None = None) -> dict[str, object]:
        return self.type_properties(item.object.type_name, {**item.properties, **(overrides or {})})

    def type_properties(self, type_name: str, authored: dict[str, object]) -> dict[str, object]:
        kind = self.workspace.profile.runtime_type(type_name)
        return {**kind.defaults, **{name: value for name, value in authored.items() if name in kind.declared}}

    def native_children(
        self,
        node: GuiSceneNode,
        properties_by_path: dict[str, dict[str, object]] | None = None,
    ) -> tuple[GuiSceneNode, ...]:
        """Return children in DMC5's intrusive-list traversal order."""

        overrides = properties_by_path or {}
        decorated = []
        for insertion_order, child in enumerate(node.children, start=1):
            properties = self.runtime_properties(child, overrides.get(child.path))
            decorated.append((_child_sort_key(properties, insertion_order, self.workspace.profile), child))
        decorated.sort(key=lambda item: item[0])
        return tuple(child for _key, child in decorated)

    def update_preview(
        self,
        overrides: dict[str, dict[str, object]] | None = None,
        *,
        output_size: tuple[float, float] | None = None,
        safe_area_ratio: float | None = None,
        transient: tuple | None = None,
        projection: Callable | None = None,
    ) -> None:
        """Lay out a runtime projection without changing authored properties."""

        profile = self.workspace.profile
        if safe_area_ratio is None:
            safe_area_ratio = profile.safe_area_ratio
        self._preview_overrides = overrides or {}
        root_properties = self.runtime_properties(self.root, self._preview_overrides.get(self.root.path))
        component_output_size = tuple(max(_f32(1.0), _f32(value)) for value in root_properties["ScreenSize"])
        self.display_size = output_size or component_output_size
        viewport_size = _logical_viewport_size(self.display_size, component_output_size)
        self.viewport_size = viewport_size if projection is None else self.display_size
        placed = None if projection is None else projection(component_output_size, self.display_size)
        transient_node = transient[0] if transient else None

        def visit(item: GuiSceneNode, parent_world: Matrix4, parent_raw: Matrix4, parent_visible: bool,
                  parent_scale: tuple[float, float, float, float], parent_offset: tuple[float, float, float],
                  parent_saturation: float, safe_area: bool) -> None:
            facts = profile.runtime_type(item.object.type_name).facts
            properties = self.runtime_properties(item, self._preview_overrides.get(item.path))
            if item is transient_node and transient is not None:
                properties["Position"] = (*transient[1:3], properties["Position"][2])
                if len(transient) > 3:
                    properties[facts["size"]] = transient[3]
            item.render_properties = properties
            position = tuple(properties["Position"])
            rotation = tuple(properties["Rotation"])
            scale = tuple(properties[facts["scale"]]) if "scale" in facts else (1.0, 1.0, 1.0)
            raw_local = _local_matrix(position, rotation, scale, profile.degree_factors)
            raw_world = _multiply4(raw_local, parent_raw)
            safe_area = safe_area or bool(properties.get("SafeAreaAdjust"))
            if properties.get("ResolutionAdjust"):
                world = _resolution_world(position, rotation, scale, parent_raw, component_output_size,
                                          self.display_size, properties, safe_area, safe_area_ratio, profile)
            elif parent_world is parent_raw:
                world = raw_world
            else:
                world = _multiply4(raw_local, parent_world)
            item.local_position = position[:2]
            item.world_matrix = world
            if placed is None:
                _show(item, world)
            item.effective_visible = parent_visible and bool(properties["Visible"]) and item.path not in self.withheld

            if "ColorScale" in properties:
                local_scale = tuple(_f32(value) for value in properties["ColorScale"])
                offset = properties["ColorOffset"]
                local_offset = tuple(_f32(offset[0] if facts.get("color_offset_broadcast") else value)
                                     for value in offset)
                local_saturation = _f32(properties["Saturation"])
                if not math.isnan(local_saturation) and local_saturation < 0.0:
                    local_saturation = 0.0
                item.color_scale = tuple(_fmul(parent_scale[index], local_scale[index]) for index in range(4))
                item.color_offset = tuple(_fadd(_fmul(parent_scale[index], local_offset[index]),
                                                _fmul(local_scale[index], parent_offset[index])) for index in range(3))
                item.saturation = _fmul(parent_saturation, local_saturation)
            else:
                item.color_scale, item.color_offset, item.saturation = parent_scale, parent_offset, parent_saturation

            size = tuple(properties[facts["size"]]) if "size" in facts else (0.0, 0.0)
            item.size = size if size[0] > 0 and size[1] > 0 else (0.0, 0.0)
            item.anchor = _anchor(profile.enum_value(properties, "ControlPoint"), item.size)
            for child in item.children:
                visit(child, world, raw_world, item.effective_visible, item.color_scale, item.color_offset,
                      item.saturation, safe_area)

        visit(self.root, _IDENTITY4, _IDENTITY4, True, (1.0, 1.0, 1.0, 1.0), (0.0, 0.0, 0.0), 1.0, False)
        if placed is not None:
            nodes = list(self.root.walk())
            for item, shown in zip(nodes, (np.array([item.world_matrix for item in nodes]) @ placed).tolist()):
                _show(item, shown)
        order_override_signature = tuple(
            (path, tuple((name, repr(values[name])) for name in sorted(_ORDER_FIELDS.intersection(values))))
            for path, values in sorted(self._preview_overrides.items()) if _ORDER_FIELDS.intersection(values))
        if self._order_dirty or order_override_signature != self._order_override_signature:
            self.tree_nodes, keep = [], profile.enum("Segment")["Keep"]

            def order(item: GuiSceneNode, held: int | None) -> None:
                own = profile.enum_value(item.render_properties, "Segment")
                item.segment = held = held if own == keep else own
                self.tree_nodes.append(item)
                children = sorted(enumerate(item.children, start=1), key=lambda pair: _child_sort_key(
                    pair[1].render_properties, pair[0], profile))
                for _index, child in children:
                    order(child, held)

            order(self.root, None if all(profile.enum_value(item.render_properties, "Segment") == keep
                                         for item in self.root.walk()) else self.held_segment())
            self.draw_nodes = self.tree_nodes if self.root.segment is None else sorted(
                self.tree_nodes, key=lambda item: item.segment)
            self._order_dirty = False
            self._order_override_signature = order_override_signature

    @staticmethod
    def local_position_for_scene_point(
        node: GuiSceneNode,
        x: float,
        y: float,
    ) -> tuple[float, float]:
        (a, b, _, p), (c, d, _, q), (e, f, _, r), (tx, ty, _, s) = node.parent.scene_matrix if node.parent else (
            _IDENTITY4)
        z = node.render_properties["Position"][2]
        a, b, c, d, w = a - x * p, b - y * p, c - x * q, d - y * q, z * r + s
        determinant = a * d - b * c
        if abs(determinant) < 1e-8:
            raise GuiSceneError(f"{node.parent.object.name} is scaled to nothing: an object inside it cannot be "
                                "placed with the pointer")
        x, y = x * w - (z * e + tx), y * w - (z * f + ty)
        return (d * x - c * y) / determinant, (-b * x + a * y) / determinant

def _logical_viewport_size(
    display_size: tuple[float, float],
    component_output_size: tuple[float, float],
) -> tuple[float, float]:
    """Convert physical pixels to the logical GUI viewport used by RE Engine."""

    display_width, display_height = display_size
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
_IDENTITY4: Matrix4 = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)


@lru_cache(maxsize=16_384)
def _local_matrix(position, rotation, scale, degree_factors) -> Matrix4:
    px, py, pz = (_f32(value) for value in position)
    rx, ry, rz = (_f32(value) for value in rotation)
    scale_x, scale_y, scale_z = (_f32(value) for value in scale)

    def to_radians(value: float) -> float:
        return _fmul(_fmul(value, degree_factors[0]), degree_factors[1])

    rx, ry, rz = (to_radians(value) for value in (rx, ry, rz))
    sx, sy, sz = (_f32(crt("sinf")(value)) for value in (rx, ry, rz))
    cx, cy, cz = (_f32(crt("cosf")(value)) for value in (rx, ry, rz))
    row0 = (_fmul(cy, cz), _fmul(cy, sz), _f32(-sy), 0.0)
    row1 = (
        _fsub(_fmul(_fmul(sy, cz), sx), _fmul(cx, sz)),
        _fadd(_fmul(_fmul(sy, sz), sx), _fmul(cx, cz)),
        _fmul(sx, cy),
        0.0,
    )
    row2 = (
        _fadd(_fmul(_fmul(sy, cz), cx), _fmul(sx, sz)),
        _fsub(_fmul(_fmul(sy, sz), cx), _fmul(sx, cz)),
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


def _show(item: GuiSceneNode, matrix) -> None:
    item.scene_matrix = matrix
    item.world_transform = (matrix[0][0], matrix[0][1], matrix[0][3], matrix[1][0], matrix[1][1], matrix[1][3],
                            matrix[3][0], matrix[3][1], matrix[3][3])
    item.world_position = (matrix[3][0] / matrix[3][3], matrix[3][1] / matrix[3][3]) if matrix[3][3] else (0.0, 0.0)


def project_gui_point(point, matrix: Matrix4) -> tuple[float, float, float]:
    x, y, z = (_f32(value) for value in point)
    result = tuple(
        _fadd(
            _fadd(_fmul(x, matrix[0][column]), _fmul(y, matrix[1][column])),
            _fadd(_fmul(z, matrix[2][column]), matrix[3][column]),
        )
        for column in range(4)
    )
    if not result[3]:
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
    inverse = 0.0 if not result[3] else _fdiv(1.0, result[3])
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
    runtime,
) -> Matrix4:
    ratio = _f32(safe_area_ratio)
    divisor = _f32(1.0) if safe_area else ratio
    source = (
        _fdiv(screen_size[0], _fmul(output_size[0], divisor)),
        _fdiv(screen_size[1], _fmul(output_size[1], divisor)),
    )
    condition = runtime.enum_label(properties, "ResAdjustCondition", "ResolutionAdjustCondition")
    policy = runtime.enum_label(properties, "ResAdjustScale", "ResolutionAdjustScale")
    selected = _resolution_scale(source, policy, condition)
    column, row = divmod(runtime.enum_value(properties, "ResAdjustAnchor", "ResolutionAdjustAnchor"), 3)
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
        runtime.degree_factors,
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


def _resolution_scale(source, policy: str, condition: str) -> tuple[float, float]:
    x_ratio, y_ratio = (_f32(value) for value in source)
    if policy == "None":
        return x_ratio, y_ratio
    if policy == "Stretch":
        selected = (_f32(1.0), _f32(1.0))
        if condition == "Expanding":
            selected = max(1.0, x_ratio), max(1.0, y_ratio)
        elif condition == "Shrinking":
            selected = min(1.0, x_ratio), min(1.0, y_ratio)
        return selected
    axis = max(x_ratio, y_ratio) if policy == "FitSmallRatioAxis" else min(x_ratio, y_ratio)
    fitted = _fdiv(x_ratio, axis), _fdiv(y_ratio, axis)
    if condition == "Always":
        return fitted
    expands = axis > 1.0
    return (source if expands == (condition == "Expanding") else fitted)


def _child_sort_key(properties: dict[str, object], insertion_order: int, runtime) -> int:
    draw_order, classes = runtime.draw_order, runtime.draw_order["mask_classes"]
    mask_class = classes[min(len(classes) - 1, max(0, runtime.enum_value(properties, "MaskType")))]
    if runtime.enum_label(properties, "MaskMode") == "ApplyToParent":
        mask_class |= draw_order["apply_to_parent"]
    priority = int(properties["Priority"]) & draw_order["priority_mask"]
    return (
        (draw_order["base"] - mask_class)
        | (priority << draw_order["priority_shift"])
        | (draw_order["order_base"] - insertion_order)
    ) & 0xFFFFFFFF


def _anchor(control: int, size: tuple[float, float]) -> tuple[float, float]:
    horizontal = (0.0, 0.5, 1.0)[control & 3]
    vertical = (0.0, 0.5, 1.0)[control >> 2]
    return size[0] * horizontal, size[1] * vertical
