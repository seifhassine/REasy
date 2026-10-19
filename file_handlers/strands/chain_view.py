from __future__ import annotations

import numpy as np

from file_handlers.mmtr.light_passes import LightPasses
from file_handlers.mmtr.mesh_render import MeshScene
from file_handlers.mmtr.pretransform_passes import SystemPrograms

from .chain_frame import SetPlace, StrandChain
from .chain_scene import ChainScene
from .lod import bone_union
from .placement import PrefabPlacement
from .set_lod import BoundMesh
from .spring_chain import SpringChain


class StrandView:
    def __init__(self, scene: ChainScene, lit: LightPasses, programs: SystemPrograms, material_target: int,
                 owner: int = 0):
        self.scene, self.owner = scene, owner
        self.spring = scene.catalog.generation == "spring"
        chain = SpringChain if self.spring else StrandChain
        self.chain = chain(lit, scene.catalog, programs, material_target)
        self.chain.add(scene.source)
        self.placement = PrefabPlacement(scene.document, scene.character, scene.holder, scene.catalog, scene.viewed,
                                         scene.meshes.get, scene.skeletons.get)
        self.own = self.placement.objects[scene.owner]

    def image(self, mesh: MeshScene, rate: float, world: np.ndarray | None = None, locals_: np.ndarray | None = None,
              wind: np.ndarray | None = None, turn: np.ndarray | None = None) -> None:
        placement, scene, source = self.placement, self.scene, self.scene.source
        placement.pose(placement.viewed.local if world is None else world, locals_)
        viewed, arguments = placement.viewed, scene.catalog.constants.arguments
        joints = [viewed.joint_world(index) for index in range(len(viewed.joints.names))]
        bound = source.binding is not None
        if self.spring:
            # The spring system's origin is the set's own transform; its binding reads the pre-transform positions.
            own = self.own.world()
            self.chain.image([SetPlace(own, own, self.own.named, None, (), None,
                                       mesh.deformed(self.owner) if bound else None, None, wind, turn)], rate)
            return
        # Only path A reads the renderer's positions; path B skins the LOD's own vertex buffer.
        morphed = bound and source.binding.positions is None
        self.chain.image([SetPlace(
            placement.origin(source.dynamics), self.own.world(), self.own.named,
            placement.anchor(source.dynamics[arguments["constraint_joint"]]), placement.colliders(scene.colliders),
            BoundMesh(scene.mesh_fields, bone_union(scene.boxes, scene.remap, joints)),
            mesh.morphed(self.owner) if morphed else None, np.array(joints) if bound else None, wind,
            turn)], rate)


__all__ = ["StrandView"]
