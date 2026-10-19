from file_handlers.mesh.normal_recalc import PreparedNormalRecalcPlan, mesh_normal_recalc_plan
from .mesh_attributes import MeshAttributeView
from .mesh_scene import build_mesh_scene, deform_mesh_vertices, mesh_scene_payloads


class MeshPreviewScene:
    def __init__(self, mesh, *, layer="main", lod_index=0, **options):
        self.mesh, self.layer, self.lod_index = mesh, layer, lod_index
        self._records = mesh_scene_payloads(mesh, layer=layer, lod_index=lod_index)
        name = options.get("visualize_attribute")
        self.attribute = (MeshAttributeView(self._records, name)
                          if name is not None and name != "bone_modifier_effectors" else None)
        self.scene = build_mesh_scene(mesh, layer=layer, lod_index=lod_index,
                                      normal_recalculation_enabled=False, attribute_view=self.attribute, **options)
        self._selection = None
        self._normal_colors = name == "normals"
        self._normal_plan = None
        if not self.scene:
            return
        draw = self.scene[0]
        self._bind_vertices, self._bind_normals = draw.vertices, draw.normals
        if layer == "main" and mesh.normal_recalc_data is not None:
            plan = mesh_normal_recalc_plan(mesh, draw.indices, lod_index=lod_index)
            if plan is not None:
                self._normal_plan = PreparedNormalRecalcPlan(plan, len(draw.vertices))
                self._rest_normals = self._normal_plan.recalculate(self._bind_vertices)
        self.update(None, 0.0)

    def update(self, channel, weight):
        selection = (channel, float(weight))
        if selection == self._selection or not self.scene:
            return False
        vertices, normals = self._bind_vertices, self._bind_normals
        if self.layer == "main":
            vertices, normals = deform_mesh_vertices(
                self.mesh, vertices, normals, self._records, lod_index=self.lod_index,
                blend_shape_channel=channel, blend_shape_weight=weight,
            )
        if self._normal_plan:
            normals = (self._rest_normals if vertices is self._bind_vertices
                       else self._normal_plan.recalculate(vertices))
        draw = self.scene[0]
        draw.vertices, draw.normals = vertices, normals
        if self._normal_colors:
            self.refresh_attribute_colors()
        self._selection = selection
        return True

    def refresh_attribute_colors(self):
        if self.attribute is not None:
            for draw in self.scene:
                draw.colors = self.attribute.colors(normals=draw.normals, tangents=draw.tangents)

    def upload(self, viewport):
        for draw in self.scene:
            viewport.update_mesh_geometry(draw.key, draw.vertices, draw.normals,
                                          colors=draw.colors if self._normal_colors else None,
                                          recompute_bounds=False)
