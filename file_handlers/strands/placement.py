"""See docs/internals/strands-rendering.md, Placement."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from functools import partial

import numpy as np
from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.clsp.clsp_file import CollisionShape
from file_handlers.clsp.placement import place, transform_scale
from file_handlers.mmtr.material_preview import MaterialPreviewError
from file_handlers.rsz.plain_values import plain_fields
from file_handlers.rsz.scene_joints import F, JointSet, Placed, placed_objects

from .chain_catalog import StrandCatalog
from .chain_constants import rest_position


class PrefabPlacement:
    def __init__(self, document, character, holder: int, catalog: StrandCatalog, viewed: JointSet,
                 mesh: Callable[[str], object | None], skeleton: Callable[[str], bytes | None]):
        self.catalog, self.rest = catalog, viewed
        self.viewed = Placed(np.eye(4, dtype=F), joints=viewed)
        self._mesh, self._skeleton = mesh, skeleton
        self.characters = placed_objects(character, None, partial(self._joints, character), {holder: self.viewed})
        self.objects = placed_objects(document, self.viewed, partial(self._joints, document))

    def _joints(self, document, item) -> JointSet | None:
        """Refused where the engines' rule is not read."""
        held = item.transform
        if held is not None and (held.absolute_scaling or held.joint_segment_scale):
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The object {name} scales its joints in a way REasy does not place."),
                {"name": item.name})
        placement, found = self.catalog.placement, []
        for key in item.components:
            component = document.components[key]
            if component.type_name not in (placement.mesh_component, placement.skeleton_component):
                continue
            fields = plain_fields(document, component)
            if component.type_name == placement.mesh_component:
                if fields[placement.mesh["shared_skeleton"]]:
                    raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                        "MaterialPreview", "The object {name} shares another's skeleton, which REasy does not "
                                           "place."), {"name": item.name})
                path, read, joints_of = fields[placement.mesh["resource"]], self._mesh, JointSet.of_mesh
            else:
                path, read, joints_of = fields[placement.skeleton["resource"]], self._skeleton, JointSet.of_skeleton
            held = read(path) if path else None
            if path and held is None:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The file {path} giving the object {name} its joints was not found."),
                    {"path": path, "name": item.name})
            joints = None if held is None else joints_of(held)
            if joints is not None:
                found.append(joints)
        if len(found) > 1:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "Two components give the object {name} its joints; which the game takes last is "
                                   "not known."), {"name": item.name})
        return found[0] if found else None

    def pose(self, world: np.ndarray, locals_: np.ndarray | None = None) -> None:
        """``world`` is for row vectors."""
        self.viewed.local = np.asarray(world, F)
        self.viewed.joints = self.rest if locals_ is None else self.rest.posed(locals_)
        for item in {*self.characters.values(), *self.objects.values()}:
            item.forget()

    def origin(self, dynamics: Mapping[str, object]) -> np.ndarray:
        arguments, viewed = self.catalog.constants.arguments, self.viewed
        if dynamics[arguments["specify_origin"]]:
            found = viewed.named(dynamics[arguments["origin_joint"]])
            return viewed.world() if found is None else found
        if not viewed.joints.names:
            return viewed.world()
        root = 0
        while viewed.joints.parents[root] >= 0:
            root = viewed.joints.parents[root]
        return viewed.joint_world(root)

    def anchor(self, name: str) -> tuple[np.ndarray, np.ndarray] | None:
        index = self.rest.index(name)
        if index is None:
            return None
        return self.viewed.joint_world(index)[3, :3], rest_position(self.rest.locals, self.rest.parents, index)

    def hung(self, owner: int) -> tuple[Callable[[int], np.ndarray], np.ndarray]:
        held = self.characters[owner]

        def matrix(key: int) -> np.ndarray:
            at = held
            while at is not None:
                index = None if at.joints is None else at.joints.hashed(key)
                if index is not None:
                    return at.joint_world(index)
                at = at.parent
            return held.world()
        return matrix, transform_scale(held.world())

    def colliders(self, shapes: Sequence[tuple[int, CollisionShape]]) -> list[CollisionShape]:
        """``shapes`` pairs each shape with its owner's local id."""
        return [place(shape, *self.hung(owner)) for owner, shape in shapes]


__all__ = ["PrefabPlacement"]
