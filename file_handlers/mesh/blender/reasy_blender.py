"""Runs inside Blender's bundled Python: only its standard library and NumPy, never REasy modules."""

import json
import sys
from pathlib import Path

import bpy
import numpy as np
from mathutils import Matrix

SOURCE_MAIN = "reasy_source_main"
SOURCE_STREAMING = "reasy_source_streaming"
MAX_NAME_BYTES = 63
# Must match package.FORMAT and package.FORMAT_VERSION; REasy modules cannot be imported here.
FORMAT = "reasy-blend"
FORMAT_VERSION = 1


def _load(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    with np.load(directory / "arrays.npz", allow_pickle=False) as archive:
        arrays = {name: archive[name] for name in archive.files}
    return manifest, arrays


def _save(directory, manifest, arrays):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = dict(manifest, format=FORMAT, format_version=FORMAT_VERSION)
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    np.savez(directory / "arrays.npz", **arrays)


def _fit_name(name, used):
    data = name.encode("utf-8")
    base = data[:MAX_NAME_BYTES].decode("utf-8", "ignore")
    candidate, number = base, 1
    while candidate in used:
        number += 1
        suffix = f"~{number}"
        candidate = data[:MAX_NAME_BYTES - len(suffix)].decode("utf-8", "ignore") + suffix
    used.add(candidate)
    return candidate


def _orthonormal(matrix):
    values = np.array(matrix, dtype=np.float64).reshape(4, 4)
    u, _s, vt = np.linalg.svd(values[:3, :3])
    rotation = u @ vt
    if np.linalg.det(rotation) < 0:
        rotation[:, 0] = -rotation[:, 0]
    values[:3, :3] = rotation
    values[3] = (0, 0, 0, 1)
    return Matrix(values.tolist())


def _build_armature(manifest, arrays, root):
    skeleton = manifest["skeleton"]
    data = bpy.data.armatures.new(manifest["title"] + " skeleton")
    data.display_type = "STICK"
    armature = bpy.data.objects.new(manifest["title"] + " skeleton", data)
    root.objects.link(armature)
    armature.show_in_front = True
    bpy.context.view_layer.objects.active = armature
    bpy.ops.object.mode_set(mode="EDIT")
    used, names = set(), []
    matrices = arrays["bones.matrix"].reshape(-1, 4, 4)
    lengths = arrays["bones.length"]
    bones = []
    for joint, name in enumerate(skeleton["names"]):
        bone = data.edit_bones.new(_fit_name(name, used))
        bone.head = (0.0, 0.0, 0.0)
        bone.tail = (0.0, float(lengths[joint]), 0.0)
        bone.matrix = _orthonormal(matrices[joint])
        bones.append(bone)
        names.append(bone.name)
    for joint, parent in enumerate(skeleton["parents"]):
        if 0 <= parent < len(bones) and parent != joint:
            bones[joint].parent = bones[parent]
    bpy.ops.object.mode_set(mode="OBJECT")
    for joint, name in enumerate(names):
        bone = data.bones[name]
        bone["reasy_joint"] = joint
        bone["reasy_rest"] = [value for row in bone.matrix_local for value in row]
        bone["reasy_exported_name"] = bone.name
    palette = set(skeleton["palette"])
    for joint, name in enumerate(names):
        data.bones[name].use_deform = joint in palette
    return armature, names


def _build_mesh(entry, prefix, arrays, name):
    mesh = bpy.data.meshes.new(name)
    positions = arrays[prefix + "co"]
    corner_vert = arrays[prefix + "corner_vert"]
    sizes = arrays[prefix + "face_size"]
    mesh.vertices.add(len(positions))
    mesh.vertices.foreach_set("co", positions.astype(np.float32).ravel())
    mesh.loops.add(len(corner_vert))
    mesh.loops.foreach_set("vertex_index", corner_vert.astype(np.int32))
    mesh.polygons.add(len(sizes))
    starts = np.concatenate(([0], np.cumsum(sizes)[:-1])).astype(np.int32) if len(sizes) else np.zeros(0, np.int32)
    mesh.polygons.foreach_set("loop_start", starts)
    mesh.update(calc_edges=True)
    if prefix + "normals" in arrays:
        attribute = mesh.attributes.new("custom_normal", "FLOAT_VECTOR", entry["normal_domain"])
        attribute.data.foreach_set("vector", arrays[prefix + "normals"].astype(np.float32).ravel())
    for layer_name, array_name in entry["uvs"]:
        layer = mesh.uv_layers.new(name=layer_name, do_init=False)
        layer.uv.foreach_set("vector", arrays[prefix + array_name].astype(np.float32).ravel())
    if mesh.uv_layers:
        mesh.uv_layers.active_index = 0
    if entry["colors"]:
        colors = mesh.color_attributes.new("Color", "BYTE_COLOR", "POINT")
        colors.data.foreach_set("color_srgb", (arrays[prefix + "color"].astype(np.float32) / 255.0).ravel())
        mesh.color_attributes.active_color = colors
    for attribute_name, array_name, domain in (("reasy_vertex", "vid", "POINT"), ("reasy_corner", "cid", "CORNER"),
                                               ("reasy_pivot", "pivot", "POINT")):
        if prefix + array_name in arrays:
            attribute = mesh.attributes.new(attribute_name, "INT", domain)
            attribute.data.foreach_set("value", arrays[prefix + array_name].astype(np.int32))
    for field in ("hidden_cid", "hidden_size", "hidden_face"):
        if prefix + field in arrays:
            mesh["reasy_" + field] = arrays[prefix + field].astype("<i4").tobytes()
    mesh["reasy_mesh"] = entry["key"]
    return mesh


def _add_weights(obj, entry, prefix, arrays, bone_names):
    for label, group_prefix in entry["weights"]:
        vertices = arrays[prefix + f"w.{label}.v"]
        joints = arrays[prefix + f"w.{label}.j"]
        weights = arrays[prefix + f"w.{label}.w"]
        groups = {}
        for joint in np.unique(joints):
            name = group_prefix + bone_names[int(joint)] if int(joint) < len(bone_names) else f"{group_prefix}joint_{joint}"
            group = obj.vertex_groups.get(name) or obj.vertex_groups.new(name=name)
            groups[int(joint)] = group
            rows = joints == joint
            values = weights[rows]
            members = vertices[rows]
            for value in np.unique(values):
                group.add(members[values == value].tolist(), float(value), "REPLACE")


def _add_shape_keys(obj, entry, prefix, arrays):
    if not entry["shape_keys"]:
        return
    obj.shape_key_add(name="Basis", from_mix=False)
    for number, name in enumerate(entry["shape_keys"]):
        block = obj.shape_key_add(name=name, from_mix=False)
        block.data.foreach_set("co", arrays[prefix + f"sk{number}"].astype(np.float32).ravel())
    obj.data.shape_keys.use_relative = True
    obj.data.shape_keys["reasy_mesh"] = entry["key"]


def export(package, output):
    manifest, arrays = _load(package)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    root = bpy.data.collections.new(manifest["title"])
    scene.collection.children.link(root)
    package = Path(package)
    root[SOURCE_MAIN] = (package / "source_main.bin").read_bytes()
    if manifest["has_streaming"]:
        root[SOURCE_STREAMING] = (package / "source_streaming.bin").read_bytes()
    root["reasy_file_version"] = str(manifest["file_version"])
    root["reasy_game"] = manifest["game"]
    root["reasy_format"] = 1
    root["reasy_merge_seams"] = bool(manifest["merge_seams"])

    materials = []
    for index, name in enumerate(manifest["materials"]):
        material = bpy.data.materials.new(name)
        material["reasy_material"] = index
        material["reasy_exported_name"] = material.name
        materials.append(material)

    armature, bone_names = (None, [])
    if manifest["skeleton"]:
        armature, bone_names = _build_armature(manifest, arrays, root)

    collections = {}
    for item in manifest["collections"]:
        collection = bpy.data.collections.new(item["name"])
        collection["reasy_layer"] = item["layer"]
        collection["reasy_lod"] = item["lod"]
        collection["reasy_mesh_index"] = item["mesh_index"]
        root.children.link(collection)
        collections[item["name"]] = (collection, item["hidden"])

    built = {}
    for item in manifest["objects"]:
        number = item["mesh"]
        entry = manifest["meshes"][number]
        prefix = f"m{number}."
        mesh = built.get(number)
        first = mesh is None
        if first:
            mesh = built[number] = _build_mesh(entry, prefix, arrays, item["name"])
            if 0 <= item["material"] < len(materials):
                mesh.materials.append(materials[item["material"]])
        obj = bpy.data.objects.new(item["name"], mesh)
        for field in ("key", "layer", "lod", "part", "group", "draw"):
            obj["reasy_" + ("object" if field == "key" else field)] = item[field]
        collections[item["collection"]][0].objects.link(obj)
        if first:
            _add_weights(obj, entry, prefix, arrays, bone_names)
            _add_shape_keys(obj, entry, prefix, arrays)
        if armature is not None and item["layer"] != "occluder":
            obj.parent = armature
            modifier = obj.modifiers.new("Armature", "ARMATURE")
            modifier.object = armature

    for collection, hidden in collections.values():
        layer = _find_layer_collection(bpy.context.view_layer.layer_collection, collection)
        if layer is not None and hidden:
            layer.hide_viewport = True
    bpy.ops.wm.save_as_mainfile(filepath=str(output), compress=True)


def _find_layer_collection(layer, collection):
    if layer.collection == collection:
        return layer
    for child in layer.children:
        found = _find_layer_collection(child, collection)
        if found is not None:
            return found
    return None


def _roots(name):
    roots = [c for c in bpy.data.collections if SOURCE_MAIN in c and not c.library]
    if name:
        roots = [c for c in roots if c.name == name]
    return roots


def _collection_of(obj, root):
    for collection in [root, *root.children_recursive]:
        if obj.name in collection.objects and "reasy_layer" in collection:
            return collection
    return None


def _matrix(values):
    return [value for row in values for value in row]


def _dump_mesh(mesh, prefix, arrays):
    entry = {"name": mesh.name, "key": mesh.get("reasy_mesh", ""), "uvs": [], "colors": [], "attributes": []}
    count = len(mesh.vertices)
    positions = np.zeros(count * 3, np.float32)
    mesh.vertices.foreach_get("co", positions)
    arrays[prefix + "co"] = positions.reshape(-1, 3)
    corner_vert = np.zeros(len(mesh.loops), np.int32)
    mesh.loops.foreach_get("vertex_index", corner_vert)
    arrays[prefix + "corner_vert"] = corner_vert
    sizes = np.zeros(len(mesh.polygons), np.int32)
    mesh.polygons.foreach_get("loop_total", sizes)
    starts = np.zeros(len(mesh.polygons), np.int32)
    mesh.polygons.foreach_get("loop_start", starts)
    arrays[prefix + "face_size"] = sizes
    arrays[prefix + "face_start"] = starts
    materials = np.zeros(len(mesh.polygons), np.int32)
    mesh.polygons.foreach_get("material_index", materials)
    arrays[prefix + "face_material"] = materials
    entry["materials"] = [[m.name, int(m.get("reasy_material", -1)), str(m.get("reasy_exported_name", m.name))]
                          if m else ["", -1, ""] for m in mesh.materials]
    normals = np.zeros(len(mesh.loops) * 3, np.float32)
    mesh.corner_normals.foreach_get("vector", normals) if len(mesh.loops) else None
    arrays[prefix + "corner_normals"] = normals.reshape(-1, 3)
    custom = mesh.attributes.get("custom_normal")
    if custom is not None and custom.data_type == "FLOAT_VECTOR" and custom.domain in ("POINT", "CORNER"):
        values = np.zeros(len(custom.data) * 3, np.float32)
        custom.data.foreach_get("vector", values)
        arrays[prefix + "custom_normal"] = values.reshape(-1, 3)
        entry["custom_normal"] = custom.domain
    else:
        entry["custom_normal"] = None if custom is None else "ENCODED"
    vertex_normals = np.zeros(count * 3, np.float32)
    mesh.vertices.foreach_get("normal", vertex_normals)
    arrays[prefix + "vertex_normals"] = vertex_normals.reshape(-1, 3)
    for number, layer in enumerate(mesh.uv_layers):
        values = np.zeros(len(mesh.loops) * 2, np.float32)
        layer.uv.foreach_get("vector", values)
        arrays[prefix + f"uv{number}"] = values.reshape(-1, 2)
        entry["uvs"].append(layer.name)
    for number, attribute in enumerate(mesh.color_attributes):
        values = np.zeros(len(attribute.data) * 4, np.float32)
        attribute.data.foreach_get("color_srgb", values)
        arrays[prefix + f"color{number}"] = values.reshape(-1, 4)
        entry["colors"].append([attribute.name, attribute.domain, attribute.data_type])
    entry["active_color"] = mesh.color_attributes.active_color.name if mesh.color_attributes.active_color else ""
    for name in ("reasy_vertex", "reasy_corner", "reasy_pivot"):
        attribute = mesh.attributes.get(name)
        if attribute is not None and attribute.data_type == "INT":
            values = np.zeros(len(attribute.data), np.int32)
            attribute.data.foreach_get("value", values)
            arrays[prefix + name] = values
            entry["attributes"].append([name, attribute.domain])
    for field in ("hidden_cid", "hidden_size", "hidden_face"):
        value = mesh.get("reasy_" + field)
        if value is not None:
            arrays[prefix + field] = np.frombuffer(bytes(value), dtype="<i4").copy()
    mesh.calc_loop_triangles()
    triangles = np.zeros(len(mesh.loop_triangles) * 3, np.int32)
    mesh.loop_triangles.foreach_get("loops", triangles)
    arrays[prefix + "tri_loops"] = triangles.reshape(-1, 3)
    polygon = np.zeros(len(mesh.loop_triangles), np.int32)
    mesh.loop_triangles.foreach_get("polygon_index", polygon)
    arrays[prefix + "tri_face"] = polygon
    entry["shape_keys"] = []
    if mesh.shape_keys is not None:
        blocks = mesh.shape_keys.key_blocks
        entry["reference"] = mesh.shape_keys.reference_key.name
        entry["relative"] = bool(mesh.shape_keys.use_relative)
        for number, block in enumerate(blocks):
            values = np.zeros(count * 3, np.float32)
            block.data.foreach_get("co", values)
            arrays[prefix + f"sk{number}"] = values.reshape(-1, 3)
            entry["shape_keys"].append({"name": block.name, "relative_to": block.relative_key.name,
                                        "mute": bool(block.mute), "vertex_group": block.vertex_group,
                                        "slider_max": block.slider_max})
    return entry


def _evaluated(obj):
    """Armature modifiers stay off so the rest pose imports."""
    active = [m for m in obj.modifiers if m.type != "ARMATURE" and m.show_viewport]
    if not active:
        return None, [], False
    armatures = [(m, m.show_viewport) for m in obj.modifiers if m.type == "ARMATURE"]
    keys = obj.data.shape_keys
    saved = (obj.show_only_shape_key, obj.active_shape_key_index)
    lost = False
    try:
        for modifier, _shown in armatures:
            modifier.show_viewport = False
        if keys is not None:
            obj.show_only_shape_key = True
            obj.active_shape_key_index = list(keys.key_blocks).index(keys.reference_key)
        bpy.context.view_layer.update()
        depsgraph = bpy.context.evaluated_depsgraph_get()
        result = bpy.data.meshes.new_from_object(obj.evaluated_get(depsgraph), preserve_all_data_layers=True,
                                                 depsgraph=depsgraph)
        evaluated_keys = []
        if keys is not None:
            for index, block in enumerate(keys.key_blocks):
                obj.active_shape_key_index = index
                bpy.context.view_layer.update()
                depsgraph = bpy.context.evaluated_depsgraph_get()
                evaluated = obj.evaluated_get(depsgraph)
                temporary = evaluated.to_mesh()
                values = np.zeros(len(temporary.vertices) * 3, np.float32)
                temporary.vertices.foreach_get("co", values)
                evaluated.to_mesh_clear()
                if len(values) != len(result.vertices) * 3:
                    lost = True
                    break
                evaluated_keys.append((block, values))
        if evaluated_keys and not lost:
            holder = bpy.data.objects.new("_reasy_evaluated", result)
            reference = keys.reference_key.name
            for block, values in evaluated_keys:
                added = holder.shape_key_add(name=block.name, from_mix=False)
                added.data.foreach_set("co", values)
                added.mute = block.mute
            for added in result.shape_keys.key_blocks:
                added.relative_key = result.shape_keys.key_blocks[reference]
            bpy.data.objects.remove(holder)
    finally:
        obj.show_only_shape_key, obj.active_shape_key_index = saved
        for modifier, shown in armatures:
            modifier.show_viewport = shown
    for key in ("reasy_mesh", "reasy_hidden_cid", "reasy_hidden_size", "reasy_hidden_face"):
        if key in obj.data:
            result[key] = obj.data[key]
    return result, [m.name for m in active], lost


def _dump_weights(obj, mesh, prefix, arrays):
    names = [group.name for group in obj.vertex_groups]
    vertices, groups, weights = [], [], []
    for vertex in mesh.vertices:
        for element in vertex.groups:
            vertices.append(vertex.index)
            groups.append(element.group)
            weights.append(element.weight)
    arrays[prefix + "wv"] = np.array(vertices, np.int32)
    arrays[prefix + "wg"] = np.array(groups, np.int32)
    arrays[prefix + "ww"] = np.array(weights, np.float32)
    return names


def _dump_armature(obj):
    bones = []
    for bone in obj.data.bones:
        bones.append({"name": bone.name, "parent": bone.parent.name if bone.parent else "",
                      "matrix": _matrix(bone.matrix_local), "joint": int(bone.get("reasy_joint", -1)),
                      "rest": list(bone.get("reasy_rest", [])), "deform": bool(bone.use_deform),
                      "exported": str(bone.get("reasy_exported_name", bone.name))})
    return {"name": obj.name, "matrix": _matrix(obj.matrix_world), "bones": bones}


def _dump_object(obj, collection, manifest, arrays, meshes, apply_modifiers):
    mesh = obj.data
    applied, lost = [], False
    if apply_modifiers:
        evaluated, applied, lost = _evaluated(obj)
        if evaluated is not None:
            mesh = evaluated
    number = meshes.get(mesh.name)
    if number is None:
        number = meshes[mesh.name] = len(manifest["meshes"])
        manifest["meshes"].append(_dump_mesh(mesh, f"m{number}.", arrays))
    item = {"name": obj.name, "mesh": number, "collection": collection,
            "matrix": _matrix(obj.matrix_world),
            "parent": obj.parent.name if obj.parent else "",
            "modifiers": [[m.name, m.type, getattr(getattr(m, "object", None), "name", "")] for m in obj.modifiers],
            "props": {key: obj[key] for key in obj.keys() if key.startswith("reasy_")
                      and isinstance(obj[key], (int, float, str))},
            "hidden": bool(obj.hide_get() or obj.hide_viewport),
            "applied": applied, "lost_shape_keys": lost}
    item["groups"] = _dump_weights(obj, mesh, f"o{len(manifest['objects'])}.", arrays)
    manifest["objects"].append(item)


def _new_manifest(**fields):
    manifest = {"root": "", "file_version": "", "game": "", "merge_seams": True, "has_streaming": False,
                "foreign": False, "objects": [], "meshes": [], "armatures": [], "collections": [], "outside": [],
                "hidden": []}
    manifest.update(fields)
    return manifest


def _dump_reasy(root, package, apply_modifiers):
    (package / "source_main.bin").write_bytes(bytes(root[SOURCE_MAIN]))
    if SOURCE_STREAMING in root:
        (package / "source_streaming.bin").write_bytes(bytes(root[SOURCE_STREAMING]))
    manifest = _new_manifest(root=root.name, file_version=str(root.get("reasy_file_version", "")),
                             game=root.get("reasy_game", ""), merge_seams=bool(root.get("reasy_merge_seams", True)),
                             has_streaming=SOURCE_STREAMING in root)
    arrays = {}
    for collection in root.children_recursive:
        if "reasy_layer" in collection:
            manifest["collections"].append({"name": collection.name, "layer": collection["reasy_layer"],
                                             "lod": int(collection.get("reasy_lod", 0)),
                                             "mesh_index": int(collection.get("reasy_mesh_index", 0))})
    meshes = {}
    in_root = {obj.name for obj in root.all_objects}
    for obj in bpy.data.objects:
        if obj.name not in in_root:
            if obj.type == "MESH" and obj.data is not None and "reasy_mesh" in obj.data:
                manifest["outside"].append(obj.name)
        elif obj.type == "ARMATURE":
            manifest["armatures"].append(_dump_armature(obj))
        elif obj.type == "MESH":
            collection = _collection_of(obj, root)
            _dump_object(obj, collection.name if collection else "", manifest, arrays, meshes, apply_modifiers)
    return manifest, arrays


def _dump_foreign(apply_modifiers):
    """A mesh object's collection may name a LOD."""
    manifest, arrays, meshes = _new_manifest(foreign=True), {}, {}
    master = bpy.context.scene.collection
    for obj in bpy.context.scene.objects:
        if obj.type == "ARMATURE":
            manifest["armatures"].append(_dump_armature(obj))
        elif obj.type == "MESH":
            if obj.visible_get():
                collection = next((c.name for c in obj.users_collection if c != master), "")
                _dump_object(obj, collection, manifest, arrays, meshes, apply_modifiers)
            else:
                manifest["hidden"].append(obj.name)
    return manifest, arrays


def dump(blend, package, root_name="", apply_modifiers=True):
    bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False)
    roots = _roots(root_name)
    if len(roots) > 1:
        raise SystemExit("REASY_ERROR: several REasy mesh collections: " + json.dumps([r.name for r in roots]))
    if not roots and root_name:
        raise SystemExit(f"REASY_ERROR: no REasy mesh collection named {root_name!r} in this .blend")
    package = Path(package)
    package.mkdir(parents=True, exist_ok=True)
    if roots:
        manifest, arrays = _dump_reasy(roots[0], package, apply_modifiers)
    else:
        manifest, arrays = _dump_foreign(apply_modifiers)
    _save(package, manifest, arrays)


def main():
    argv = sys.argv[sys.argv.index("--") + 1:]
    mode = argv[0]
    if mode == "export":
        export(argv[1], argv[2])
    elif mode == "dump":
        flags = [value for value in argv[3:] if value.startswith("--")]
        positional = [value for value in argv[3:] if not value.startswith("--")]
        dump(argv[1], argv[2], positional[0] if positional else "", "--keep-modifiers" not in flags)
    else:
        raise SystemExit(f"REASY_ERROR: unknown mode {mode}")
    print("REASY_OK")


if __name__ == "__main__":
    main()
