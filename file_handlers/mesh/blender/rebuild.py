"""See docs/mesh-conversion.md, Blender round trip."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field, replace

import numpy as np

from ..canonical_writer import serialize_mesh
from ..mesh_file import MPLY_MAGIC, JointNode, MeshFile
from ..models import MeshAABB
from .export import joint_names
from .geometry import DrawPlan, apply_geometry
from .morphs import lod_channel_names
from .package import from_blender, matrix_from_blender
from .materials import normalize_mply
from .scene import UV_PLANES, build_scene, layer_lods

REST_TOLERANCE = 1e-5


class BlendImportError(ValueError):
    pass


@dataclass
class BlendImportResult:
    mesh: MeshFile
    output: object
    file_version: int
    changes: list
    losses: list
    unchanged: bool


@dataclass
class _Report:
    changes: list = field(default_factory=list)
    losses: list = field(default_factory=list)

    def change(self, message):
        if message not in self.changes:
            self.changes.append(message)

    def loss(self, message):
        if message not in self.losses:
            self.losses.append(message)


def _objects(names):
    names = list(dict.fromkeys(names))  # reference imports copy an object into every LOD
    if len(names) <= 2:
        return " and ".join(names)
    return f"{len(names)} objects ({', '.join(names[:3])}{', …' if len(names) > 3 else ''})"


def parse_source(main, streaming, file_version):
    mesh = MeshFile()
    mesh.read(main, file_version=int(file_version), streaming_data=streaming)
    return mesh


class _BlenderObject:
    """In engine coordinates."""

    def __init__(self, number, item, manifest, arrays):
        entry = manifest["meshes"][item["mesh"]]
        prefix, own = f"m{item['mesh']}.", f"o{number}."
        self.item, self.entry, self.name = item, entry, item["name"]
        self.props = item.get("props", {})
        self.key = str(self.props.get("reasy_object", ""))
        self.mesh_key = entry.get("key", "")
        self.collection = item.get("collection", "")
        matrix = np.asarray(item["matrix"], dtype=np.float64).reshape(4, 4)
        self.transformed = not np.array_equal(matrix, np.eye(4))
        linear = matrix[:3, :3]
        # A mirroring transform (negative scale) turns faces inside out unless their corner order is reversed.
        self.mirrored = bool(np.linalg.det(linear) < 0)
        blocks = entry.get("shape_keys", [])
        names = [block["name"] for block in blocks]
        reference = names.index(entry["reference"]) if blocks and entry.get("reference") in names else None
        co = arrays[prefix + ("co" if reference is None else f"sk{reference}")].astype(np.float64)
        if self.transformed:
            co = co @ linear.T + matrix[:3, 3]
        self.positions = from_blender(co)
        self.corner_vert = arrays[prefix + "corner_vert"].astype(np.int64)
        self.face_size = arrays[prefix + "face_size"].astype(np.int64)
        self.face_start = arrays[prefix + "face_start"].astype(np.int64)
        self.face_material = arrays[prefix + "face_material"].astype(np.int64)
        self.materials = entry.get("materials", [])

        def vectors(values):
            values = np.asarray(values, dtype=np.float64)
            if self.transformed:
                normal_matrix = np.linalg.inv(linear).T
                values = values @ normal_matrix.T
                length = np.linalg.norm(values, axis=1, keepdims=True)
                values = values / np.where(length > 0, length, 1)
            return from_blender(values)

        domain = entry.get("custom_normal")
        vertex_normals = arrays[prefix + "vertex_normals"]
        if domain == "POINT":
            custom = arrays[prefix + "custom_normal"]
            self.corner_normals, self.point_normals = vectors(custom[self.corner_vert]), vectors(custom)
        elif domain == "CORNER":
            self.corner_normals = vectors(arrays[prefix + "custom_normal"])
            self.point_normals = vectors(vertex_normals)
        else:
            self.corner_normals = vectors(arrays[prefix + "corner_normals"])
            self.point_normals = vectors(vertex_normals)
        self.encoded_normals = domain == "ENCODED"
        self.point_normals_known = domain == "POINT"

        uv_names = entry.get("uvs", [])
        self.uvs = {}
        for plane_index, plane in enumerate(UV_PLANES):
            name = f"UV{plane_index}"
            layer = uv_names.index(name) if name in uv_names else None
            if layer is None and not any(re.fullmatch(r"UV\d", n) for n in uv_names) and plane_index < len(uv_names):
                layer = plane_index
            if layer is not None:
                values = arrays[prefix + f"uv{layer}"].astype(np.float32).copy()
                values[:, 1] = np.float32(1.0) - values[:, 1]
                self.uvs[plane] = values
        self.color_domain, self.colors = None, None
        colors = entry.get("colors", [])
        if colors:
            color_names = [c[0] for c in colors]
            chosen = color_names.index("Color") if "Color" in color_names else (
                color_names.index(entry["active_color"]) if entry.get("active_color") in color_names else 0)
            self.color_domain = colors[chosen][1]
            self.colors = np.clip(np.rint(arrays[prefix + f"color{chosen}"] * 255.0), 0, 255).astype(np.uint8)
        attributes = dict(entry.get("attributes", []))
        self.vertex_ids = (arrays[prefix + "reasy_vertex"].astype(np.int64)
                           if attributes.get("reasy_vertex") == "POINT" else None)
        self.corner_ids = (arrays[prefix + "reasy_corner"].astype(np.int64)
                           if attributes.get("reasy_corner") == "CORNER" else None)
        self.pivots = (arrays[prefix + "reasy_pivot"].astype(np.int64)
                       if attributes.get("reasy_pivot") == "POINT" else None)
        self.hidden = None
        if prefix + "hidden_cid" in arrays:
            self.hidden = (arrays[prefix + "hidden_cid"].astype(np.int64), arrays[prefix + "hidden_size"].astype(np.int64),
                           arrays[prefix + "hidden_face"].astype(np.int64))
        self.tri_loops = arrays[prefix + "tri_loops"].astype(np.int64)
        self.tri_face = arrays[prefix + "tri_face"].astype(np.int64)
        self.group_names = item.get("groups", [])
        self.weight_vertex = arrays.get(own + "wv", np.zeros(0, np.int32)).astype(np.int64)
        self.weight_group = arrays.get(own + "wg", np.zeros(0, np.int32)).astype(np.int64)
        self.weight_value = arrays.get(own + "ww", np.zeros(0, np.float32)).astype(np.float64)
        self.shape_keys = {}
        for number_key, block in enumerate(blocks):
            if number_key == reference:
                continue
            relative = names.index(block["relative_to"]) if block["relative_to"] in names else reference
            delta = (arrays[prefix + f"sk{number_key}"].astype(np.float64)
                     - arrays[prefix + f"sk{relative}"].astype(np.float64))
            if self.transformed:
                delta = delta @ linear.T
            self.shape_keys[block["name"]] = from_blender(delta)
        self.muted_keys = [b["name"] for b in blocks if b.get("mute")]
        self.applied = list(item.get("applied", []))
        self.modifiers = [m for m in item.get("modifiers", []) if m[1] != "ARMATURE" and m[0] not in self.applied]
        self.lost_shape_keys = bool(item.get("lost_shape_keys", False))

    @property
    def vertex_count(self):
        return len(self.positions)


class _Import:
    def __init__(self, manifest, arrays, base, report, reference=False):
        self.manifest, self.arrays, self.mesh, self.report = manifest, arrays, base, report
        self.reference = reference
        self.scene = build_scene(base, merge_seams=bool(manifest.get("merge_seams", True)))
        self.domains = self.scene.domains
        self.objects = [_BlenderObject(number, item, manifest, arrays)
                        for number, item in enumerate(manifest["objects"])]
        self.collections = {c["name"]: c for c in manifest.get("collections", [])}
        self.original_counts = [len(payload.positions) // 3 for payload in self.domains.payloads]
        self.scene_objects = {item.key: item for item in self.scene.objects}
        self._morph_index = None
        self.new_palette = []
        self.original_materials = list(base.material_names)
        self.unassigned = []
        self.group_joints = {}
        self.unknown_groups = set()
        self.added_channels = {}  # LOD -> shape key name -> the morph channel added for it (morphs)

    def skeleton(self):
        mesh = self.mesh
        names = joint_names(mesh)
        self.group_joints = {}
        armatures = [a for a in self.manifest.get("armatures", []) if any(b["joint"] >= 0 for b in a["bones"])]
        if not mesh.bones:
            if self.manifest.get("armatures") and self.reference:
                self.report.loss("The reference has no skeleton: the new mesh is static, and the scene's "
                                 "armature and weights were ignored")
            elif self.manifest.get("armatures"):
                self.report.loss("The source mesh has no skeleton; ignored the Blender armature")
            return
        if not armatures:
            self.report.loss("The Blender skeleton is missing; kept the original skeleton")
            for joint, name in enumerate(names):
                self.group_joints[name] = joint
            return
        if len(armatures) > 1:
            self.report.loss("Several armatures carry REasy joints; used the first one")
        armature = armatures[0]
        world = np.asarray(armature["matrix"], dtype=np.float64).reshape(4, 4)
        joint_of = {}
        for bone in armature["bones"]:
            if 0 <= bone["joint"] < len(mesh.bones) and bone["joint"] not in joint_of.values():
                joint_of[bone["name"]] = bone["joint"]
        missing = sorted(set(range(len(mesh.bones))) - set(joint_of.values()))
        if missing:
            self.report.loss(f"Kept {len(missing)} joints deleted in Blender; joints cannot be removed safely")
        changed = []
        for bone in armature["bones"]:
            joint = joint_of.get(bone["name"])
            if joint is None:
                continue
            rest = world @ np.asarray(bone["matrix"], dtype=np.float64).reshape(4, 4)
            stored = np.asarray(bone["rest"], dtype=np.float64)
            if stored.size == 16 and np.allclose(rest, stored.reshape(4, 4), atol=REST_TOLERANCE, rtol=0):
                continue
            changed.append((joint, rest))
        new_bones = [bone for bone in armature["bones"] if bone["name"] not in joint_of]
        # Parents first, so a new joint can attach to another new joint.
        pending = list(new_bones)
        while pending:
            progress = False
            for bone in list(pending):
                parent = bone["parent"]
                if parent and parent not in joint_of and any(b["name"] == parent for b in pending):
                    continue
                joint_of[bone["name"]] = self._append_joint(bone, joint_of.get(parent, -1),
                                                            world @ np.asarray(bone["matrix"]).reshape(4, 4))
                pending.remove(bone)
                progress = True
            if not progress:
                raise BlendImportError("New bones have a parent cycle")
        if new_bones:
            listed = ", ".join(b["name"] for b in new_bones[:8]) + ("…" if len(new_bones) > 8 else "")
            self.report.change(f"Added {len(new_bones)} joint{'s' if len(new_bones) > 1 else ''}: {listed}")
        if changed:
            self._set_rest(dict(changed))
        exported = {bone["name"]: bone["exported"] for bone in armature["bones"]}
        renamed = [(joint, name) for name, joint in joint_of.items()
                   if joint < len(names) and name != exported[name]]
        for joint, name in renamed:
            self._rename(mesh.bone_indices, joint, name)
        if renamed:
            self.report.change(f"Renamed {len(renamed)} joints: " +
                               ", ".join(f"{names[j]} → {n}" for j, n in renamed[:6]) +
                               ("…" if len(renamed) > 6 else ""))
        self.group_joints = dict(joint_of)
        # Vertex groups may still use the original joint names if a bone was renamed in Blender.
        for joint, name in enumerate(names):
            self.group_joints.setdefault(name, joint)

    def _rename(self, indices, position, name):
        mesh = self.mesh
        index = indices[position]
        users = sum(value == index for table in (mesh.material_indices, mesh.bone_indices, mesh.blend_shape_indices)
                    for value in table)
        if users == 1 and 0 <= index < len(mesh.names):
            mesh.names[index] = name
        else:
            indices[position] = len(mesh.names)
            mesh.names.append(name)

    def _append_joint(self, bone, parent, rest):
        mesh = self.mesh
        joint = len(mesh.bones)
        node = JointNode(index=joint, parent_index=parent, sibling_index=-1, child_index=-1,
                         symmetry_index=joint, segment_scale=1)
        if parent >= 0:
            siblings = [b for b in mesh.bones if b.parent_index == parent]
            if not siblings:
                mesh.bones[parent] = replace(mesh.bones[parent], child_index=joint)
            else:
                last = siblings[-1]
                while last.sibling_index >= 0:
                    last = mesh.bones[last.sibling_index]
                mesh.bones[last.index] = replace(last, sibling_index=joint)
        mesh.bones.append(node)
        world = _row_major(rest)
        parent_world = (np.asarray(mesh.world_matrices[parent], dtype=np.float64).reshape(4, 4)
                        if parent >= 0 else np.eye(4))
        local = world @ np.linalg.inv(parent_world)
        mesh.world_matrices.append(list(map(float, world.ravel())))
        mesh.local_matrices.append(list(map(float, local.ravel())))
        mesh.inverse_bind_matrices.append(list(map(float, np.linalg.inv(world).ravel())))
        mesh.bone_indices.append(len(mesh.names))
        mesh.names.append(bone["name"])
        mesh.joint_count = len(mesh.bones)
        return joint

    def _set_rest(self, changed):
        mesh = self.mesh
        world = [np.asarray(m, dtype=np.float64).reshape(4, 4) for m in mesh.world_matrices]
        for joint, rest in changed.items():
            original = world[joint]
            scale = np.linalg.norm(original[:3, :3], axis=1)
            matrix = _row_major(rest)
            matrix[:3, :3] *= scale[:, None]
            world[joint] = matrix
        for joint, node in enumerate(mesh.bones):
            parent = int(node.parent_index)
            parent_world = world[parent] if 0 <= parent < len(world) else np.eye(4)
            local = world[joint] @ np.linalg.inv(parent_world)
            if joint in changed or parent in changed:
                mesh.local_matrices[joint] = list(map(float, local.ravel()))
            if joint in changed:
                mesh.world_matrices[joint] = list(map(float, world[joint].ravel()))
                mesh.inverse_bind_matrices[joint] = list(map(float, np.linalg.inv(world[joint]).ravel()))
        self.report.change(f"Changed the rest pose of {len(changed)} joints")
        self.report.loss("Edited joint rest poses change the bind pose; animations authored for the original "
                         "skeleton may deform differently")

    def material_index(self, slot):
        name, index = slot[0], slot[1]
        mesh = self.mesh
        if 0 <= index < len(mesh.material_names):
            original = self.original_materials[index]
            exported = slot[2] if len(slot) > 2 else original
            if name and name != exported and mesh.material_names[index] == original:
                mesh.material_names[index] = name
                if mesh.header.magic != MPLY_MAGIC:
                    self._rename(mesh.material_indices, index, name)
                self.report.change(f"Renamed material {original!r} → {name!r}")
            return index
        stem = re.sub(r"\.\d{3}$", "", name)
        for candidate in (name, stem):
            if candidate in mesh.material_names:
                return mesh.material_names.index(candidate)
        if not stem:
            return 0
        if mesh.header.magic != MPLY_MAGIC:
            mesh.material_indices.append(len(mesh.names))
            mesh.names.append(stem)
        mesh.material_names.append(stem)
        for data in mesh.meshes:
            data.material_count = len(mesh.material_names)
        if mesh.shadow_mesh is not None:
            mesh.shadow_mesh.material_count = len(mesh.material_names)
        self.report.change(f"Added material {stem!r}")
        return len(mesh.material_names) - 1

    def plan(self):
        scene_objects = self.scene_objects
        claimed, extra, plans = {}, {}, []
        unapplied, applied = {}, {}
        for obj in self.objects:
            for name, kind, _target in obj.modifiers:
                unapplied.setdefault(f"{name!r} ({kind})", []).append(obj.name)
            for name in obj.applied:
                applied.setdefault(repr(name), []).append(obj.name)
        for modifier, names in unapplied.items():
            self.report.loss(f"Modifier {modifier} was not applied on {_objects(names)}")
        for modifier, names in applied.items():
            self.report.change(f"Applied modifier {modifier} on {_objects(names)}")
        report = (("loss", lambda o: o.lost_shape_keys, "their modifiers change the vertex count per shape key; "
                   "their shape keys were dropped"),
                  ("change", lambda o: o.transformed and not o.mirrored, "applied the object transform to the geometry"),
                  ("change", lambda o: o.mirrored, "applied the mirroring object transform to the geometry and reversed "
                   "the faces' winding"),
                  ("change", lambda o: o.muted_keys, "muted shape keys still import their deltas"))
        for kind, test, message in report:
            names = [obj.name for obj in self.objects if test(obj)]
            if names:
                getattr(self.report, kind)(f"{_objects(names)}: {message}")
        for obj in self.objects:
            target = scene_objects.get(obj.key)
            if target is not None and target.alias_of:
                owner = scene_objects[target.alias_of]
                if obj.mesh_key != owner.mesh_key:
                    self.report.loss(f"{obj.name} shares {owner.name}'s native geometry; edit that object instead")
                continue
            collection = self.collections.get(obj.collection)
            moved = target is not None and collection is not None and (
                collection["layer"], int(collection["lod"]), int(collection.get("mesh_index", 0))) != (
                target.layer, target.lod, target.mesh_index)
            if moved:
                self.report.change(f"{obj.name} moved to {collection['layer']} LOD{collection['lod']}")
                target = None
            if target is not None:
                if target.key in claimed:
                    extra.setdefault(target.key, []).append(obj)
                else:
                    claimed[target.key] = obj
                continue
            if collection is None:
                if obj.key:
                    self.report.loss(f"{obj.name} is outside the LOD collections; ignored it")
                else:
                    self.report.loss(f"{obj.name} is not in a LOD collection; ignored it")
                continue
            plans.extend(self._new_plans(obj, collection, target))
        if self.reference:
            # Faces sharing a part and material form one draw, as in shipped meshes; a part holds at most 255 draws.
            merged = {}
            for plan in plans:
                key = (plan.layer, plan.mesh_index, plan.lod, plan.part_id, plan.material, plan.is_quad)
                if key in merged:
                    merged[key].sources.extend(plan.sources)
                else:
                    merged[key] = plan
            plans = list(merged.values())
            lods = len({(p.layer, p.lod, p.mesh_index) for p in plans})
            objects = len({obj.name for obj in self.objects})  # each scene object is copied into every LOD it fills
            self.report.change(f"Built {len(plans)} draw{'s' if len(plans) != 1 else ''} in {lods} LOD{'s' if lods != 1 else ''} "
                               f"from {objects} object{'s' if objects != 1 else ''}")
        if self.unassigned:
            self.report.loss(f"{_objects(self.unassigned)}: faces without a material use "
                             f"{self.mesh.material_names[0]!r}; name a material slot to choose one")
        for name in self.manifest.get("outside", []):
            self.report.loss(f"{name} is outside the REasy collection; ignored it")
        owned = []
        for item in self.scene.objects:
            if item.alias_of:
                continue
            obj = claimed.get(item.key)
            if obj is None:
                owned.append(DrawPlan(item.key, item.layer, item.mesh_index, item.lod, item.part_id,
                                      item.material_index, item.is_quad, [], submesh=item.submeshes,
                                      original_material=item.material_index))
                continue
            by_material = self._faces_by_material(obj)
            home = by_material.pop(item.material_index, None)
            if home is None and len(by_material) == 1 and not self.scene.meshlets:
                (material, home), = by_material.items()
                self.report.change(f"{obj.name}: material changed to {self.mesh.material_names[material]!r}")
                item_material = material
            else:
                item_material = item.material_index
            plan = DrawPlan(item.key, item.layer, item.mesh_index, item.lod, item.part_id, item_material,
                            item.is_quad, [(obj, home if home is not None else np.zeros(0, np.int64))],
                            submesh=item.submeshes, original_material=item.material_index)
            plan.hidden = obj.hidden
            owned.append(plan)
            pieces = [(obj, by_material)]
            for other in extra.get(item.key, ()):
                other_faces = self._faces_by_material(other)
                faces = other_faces.pop(item_material, None)
                if faces is not None:
                    plan.sources.append((other, faces))
                    self.report.change(f"{other.name} stays in {item.name}'s draw")
                pieces.append((other, other_faces))
            for piece, remaining in pieces:
                for material, faces in remaining.items():
                    plans.append(DrawPlan("", item.layer, item.mesh_index, item.lod, item.part_id, material,
                                          item.is_quad, [(piece, faces)]))
        for plan in owned:
            if not plan.sources:
                self._adopt(plan, plans)
        self.plans = owned + [plan for plan in plans if plan.sources]
        draws = Counter((p.layer, p.mesh_index, p.lod, p.part_id) for p in self.plans)
        (layer, _index, lod, part), count = draws.most_common(1)[0] if draws else ((None,) * 4, 0)
        if count > 0xFF:
            raise BlendImportError(f"{layer} LOD{lod} part {part} would need {count} draws; the format holds 255 per "
                                   "part. Use fewer materials, or join objects that share one")

    def _adopt(self, plan, plans):
        item = self.scene_objects[plan.key]
        source = self.scene.meshes.get(item.mesh_key)
        for candidate in plans:
            if (candidate.key or not candidate.sources or len(candidate.sources) != 1
                    or (candidate.layer, candidate.mesh_index, candidate.lod, candidate.material, candidate.is_quad)
                    != (item.layer, item.mesh_index, item.lod, item.material_index, item.is_quad)):
                continue
            (obj, faces), = candidate.sources
            if source is None or obj.vertex_ids is None or not len(faces):
                continue
            corners = np.concatenate([np.arange(obj.face_start[f], obj.face_start[f] + obj.face_size[f])
                                      for f in faces[:64]])
            if not np.isin(obj.vertex_ids[obj.corner_vert[corners]], source.gpu).any():
                continue
            plan.sources = candidate.sources
            candidate.sources = []
            self.report.change(f"{item.name} was joined into {obj.name}; its draw keeps its place")
            return
        if not self.reference:
            self.report.change(f"Removed {item.name}")

    def _faces_by_material(self, obj):
        result = {}
        slots = [self.material_index(slot) for slot in obj.materials] or [0]
        slot_of_face = np.clip(obj.face_material, 0, len(slots) - 1)
        named = [bool(slot[0]) for slot in obj.materials] or [False]
        if not all(named[int(slot)] for slot in np.unique(slot_of_face)):
            self.unassigned.append(obj.name)
        for slot in np.unique(slot_of_face):
            material = slots[int(slot)]
            faces = np.flatnonzero(slot_of_face == slot)
            result[material] = np.concatenate((result[material], faces)) if material in result else faces
        return {material: np.sort(faces) for material, faces in result.items()}

    def _new_plans(self, obj, collection, template):
        layer, lod, mesh_index = collection["layer"], int(collection["lod"]), int(collection.get("mesh_index", 0))
        part = obj.props.get("reasy_part")
        if part is None and not self.reference:
            match = re.search(r"(?:^|_)P(\d+)(?:_|$)", obj.name)
            part = int(match.group(1)) if match else None
        if part is None:
            lods = [l for m, i, l in layer_lods(self.mesh, layer) if i == lod and m == mesh_index]
            part = int(lods[0].mesh_groups[0].group_id) if lods and lods[0].mesh_groups else 0
        quad = bool(template.is_quad) if template is not None else False
        if not self.reference:
            self.report.change(f"Added {obj.name} to {layer} LOD{lod} part {part}")
        return [DrawPlan("", layer, mesh_index, lod, int(part), material, quad, [(obj, faces)])
                for material, faces in self._faces_by_material(obj).items()]

    def source(self, name, ids, width, dtype):
        result = np.zeros((len(ids), width), dtype=dtype)
        if not len(ids):
            return result
        domain, local = self.domains.locate(ids)
        for number in np.unique(domain):
            values = getattr(self.domains.payloads[number], name)
            if len(values):
                rows = domain == number
                result[rows] = np.asarray(values, dtype=dtype).reshape(-1, width)[local[rows]]
        return result

    def scene_object(self, key):
        return self.scene_objects[key]

    def palette_index(self, joint):
        palette = self.mesh.bone_remap_indices
        if joint in palette:
            return palette.index(joint)
        if len(palette) >= self.mesh.profile.max_deform_bones:
            raise BlendImportError(f"Joint {joint} needs a deform-palette slot, but the palette is full "
                                   f"({self.mesh.profile.max_deform_bones})")
        palette.append(joint)
        self.mesh.bone_remap_count = len(palette)
        boxes = self.mesh.bounding_boxes
        if boxes is not None:
            boxes.boxes.append(MeshAABB((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)))
        self.new_palette.append(len(palette) - 1)
        return len(palette) - 1

    def report_palette(self):
        if not self.new_palette:
            return
        names = joint_names(self.mesh)
        added = [names[self.mesh.bone_remap_indices[entry]] for entry in self.new_palette]
        listed = ", ".join(added[:6]) + ("…" if len(added) > 6 else "")
        self.report.change(f"Added {len(added)} joint{'s' if len(added) > 1 else ''} to the deform palette: {listed}")

    def channel_names_for(self, plan, scene_mesh):
        names = lod_channel_names(self.mesh, plan.lod)
        if scene_mesh is not None:
            names.update({name: (body, position) for body, position, name, _d, _n in scene_mesh.channels})
        names.update(self.added_channels.get(plan.lod, {}))
        return names

    def original_morph(self, channel, ids):
        if self._morph_index is None:
            index = {}
            for scene_mesh in self.scene.meshes.values():
                for body, position, _name, delta, normals in scene_mesh.channels:
                    index.setdefault((body, position), []).append((scene_mesh.gpu, delta, normals))
            self._morph_index = {}
            for key, parts in index.items():
                gpu = np.concatenate([part[0] for part in parts])
                order = np.argsort(gpu, kind="stable")
                deltas = np.concatenate([part[1] for part in parts])[order]
                normals = None
                if any(part[2] is not None for part in parts):
                    normals = np.concatenate([part[2] if part[2] is not None else np.zeros_like(part[1])
                                              for part in parts])[order]
                self._morph_index[key] = (gpu[order], deltas, normals)
        gpu, deltas, normals = self._morph_index.get(channel, (np.zeros(0, np.int64), np.zeros((0, 3)), None))
        result = np.zeros((len(ids), 3), np.float64)
        result_normals = None
        if normals is not None or self._channel_has_normals(channel):
            result_normals = self.source("normals", ids, 3, np.float32).astype(np.float64)
        if len(gpu):
            slot = np.clip(np.searchsorted(gpu, ids), 0, len(gpu) - 1)
            found = gpu[slot] == ids
            result[found] = deltas[slot[found]]
            if normals is not None:
                result_normals[found] = normals[slot[found]]
        return result, result_normals

    def _channel_has_normals(self, channel):
        blend = self.mesh.blend_shape_data
        body, position = channel
        segments = blend.bodies[body].channels[position].segments
        return any(segment.target_normals is not None for segment in segments)

    def finish_materials(self):
        """See docs/mesh-conversion-formats.md, Material linkage."""
        mesh = self.mesh
        if mesh.header.magic == MPLY_MAGIC:
            dropped = normalize_mply(mesh, self.original_materials)
            if dropped and not self.reference:
                self.report.change("Removed unused materials: " + ", ".join(map(repr, dropped)))
            return
        names, original = list(mesh.material_names), self.original_materials
        added = [name for name in names if name not in original]
        gone = [name for name in original if name not in names]
        if added or gone:
            needs = ", ".join(filter(None, (
                "add " + ", ".join(map(repr, added)) if added else "",
                "remove " + ", ".join(map(repr, gone)) if gone else "")))
            self.report.loss(f"The mesh now has {len(names)} materials ({len(original)} before). Its .mdf2 must list "
                             f"exactly these, by name: {needs}. The game draws an ordinary mesh with its default "
                             "material when the counts differ")

    def bounds(self):
        """Native bounds may be deliberately conservative, so bounds only grow."""
        mesh = self.mesh
        if mesh.header.magic == MPLY_MAGIC:
            return
        points, skinned = [], []
        for plan in self.plans:
            build = plan.build
            if build is None or not build.count or plan.layer != "main" or plan.referencing:
                continue
            rows = getattr(build, "moved", np.zeros(build.count, bool)) | (build.identity < 0)
            rows = rows | getattr(build, "reweighted", np.zeros(build.count, bool))
            morphed = getattr(build, "morph_changed", np.zeros(build.count, bool))
            positions = build.planes["positions"].astype(np.float64)
            selected = [positions[rows | morphed]]
            for delta, _normals in build.morphs.values():
                selected.append(positions[rows | morphed] + delta[rows | morphed])
            chosen = np.concatenate(selected)
            if not len(chosen):
                continue
            points.append(chosen)
            if "primary" in build.weights:
                indices, weights = build.weights["primary"]
                mask = rows | morphed
                repeat = len(selected)
                skinned.append((chosen, np.tile(indices[mask], (repeat, 1)), np.tile(weights[mask], (repeat, 1))))
        if not points:
            return
        points = np.concatenate(points)
        low, high = points.min(axis=0), points.max(axis=0)
        grown = False
        for data in mesh.meshes:
            box = [np.asarray(corner[:3], np.float64) for corner in data.bounding_box]
            center, radius = np.asarray(data.bounding_sphere[:3], np.float64), float(data.bounding_sphere[3])
            if self.reference:  # a reference's bounds belong to the geometry it no longer has
                box, center, radius = [low, high], (low + high) / 2, 0.0
            new_box = [np.minimum(box[0], low), np.maximum(box[1], high)]
            new_radius = max(radius, float(np.linalg.norm(points - center, axis=1).max()))
            grown |= new_radius > radius or not all(map(np.array_equal, new_box, box))
            data.bounding_box = tuple(tuple(map(float, corner)) + tuple(old[3:])
                                      for corner, old in zip(new_box, data.bounding_box))
            data.bounding_sphere = (*map(float, center), new_radius)
        if grown and not self.reference:
            self.report.change("Grew the mesh bounds to cover the edited geometry")
        self._bone_bounds(skinned)

    def _bone_bounds(self, skinned):
        mesh = self.mesh
        boxes = mesh.bounding_boxes
        if boxes is None or not mesh.bones or not skinned:
            return
        inverse = np.asarray(mesh.inverse_bind_matrices, np.float64).reshape(-1, 4, 4)
        lows = [np.full(3, np.inf) for _ in boxes.boxes]
        highs = [np.full(3, -np.inf) for _ in boxes.boxes]
        for points, indices, weights in skinned:
            homogeneous = np.c_[points, np.ones(len(points))]
            for entry in np.unique(indices[weights > 0]):
                if not 0 <= entry < len(boxes.boxes):
                    continue
                rows = np.any((indices == entry) & (weights > 0), axis=1)
                local = homogeneous[rows] @ inverse[mesh.bone_remap_indices[entry]]
                lows[entry] = np.minimum(lows[entry], local[:, :3].min(axis=0))
                highs[entry] = np.maximum(highs[entry], local[:, :3].max(axis=0))
        grown = 0
        for entry, box in enumerate(boxes.boxes):
            if not np.isfinite(lows[entry]).all():
                continue
            if entry in self.new_palette or self.reference:
                low, high = lows[entry], highs[entry]
            else:
                low = np.minimum(np.asarray(box.minimum, np.float64), lows[entry])
                high = np.maximum(np.asarray(box.maximum, np.float64), highs[entry])
                if np.array_equal(low, box.minimum) and np.array_equal(high, box.maximum):
                    continue
            boxes.boxes[entry] = replace(box, minimum=tuple(map(float, low)), maximum=tuple(map(float, high)))
            grown += 1
        if grown and self.reference:
            self.report.change(f"Computed the bounding boxes of {grown} joints")
        elif grown:
            self.report.change(f"Grew {grown} joint bounding boxes to cover the edited geometry")


def rebuild_from_dump(manifest, arrays, source_main, source_streaming, *, recompute_normals=False, reference=False,
                      skin_unweighted=False):
    file_version = int(manifest["file_version"])
    report = _Report()
    base = parse_source(source_main, source_streaming, file_version)
    context = _Import(manifest, arrays, base, report, reference=reference)
    context.recompute_normals = bool(recompute_normals)
    context.skin_unweighted = bool(skin_unweighted)
    try:
        context.skeleton()
        context.plan()
        apply_geometry(context)
        context.report_palette()
        context.finish_materials()
        context.bounds()
        output = serialize_mesh(base)
    except BlendImportError:
        raise
    except (ValueError, IndexError, KeyError) as exc:
        raise BlendImportError(str(exc)) from exc
    unchanged = False
    if not reference:
        original = serialize_mesh(parse_source(source_main, source_streaming, file_version))
        unchanged = (output.main == original.main and output.streaming == original.streaming)
        if unchanged and report.changes:
            report.change("The edits re-encode to the original bytes")
    result_mesh = output.verified or parse_source(output.main, output.streaming, file_version)
    return BlendImportResult(result_mesh, output, file_version, report.changes, report.losses, unchanged)


def _row_major(column):
    return matrix_from_blender(column).reshape(4, 4)
