import bpy
import sys
from contextlib import contextmanager
import json
import math
import re
from mathutils import Matrix, Vector
from bpy_extras.io_utils import ExportHelper

DESELECT_SHADE = 0.35
WORLD_AXES = Matrix(((1, 0, 0, 0), (0, 0, 1, 0), (0, -1, 0, 0), (0, 0, 0, 1)))


def matrix_values(matrix, scale, world=False):
    """Rows of an affine transform. World axes change; bone-local axes do not."""
    m = WORLD_AXES @ matrix if world else matrix.copy()
    m.translation *= scale
    return [round(float(m[row][column]), 9) for row in range(4) for column in range(4)]


def bone_id(obj, name):
    return obj.name + "::" + name


def json_value(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return round(value, 9) if math.isfinite(value) else None
    if isinstance(value, bpy.types.ID):
        return {"object": value.name, "id_type": value.bl_rna.identifier}
    if hasattr(value, "items"):
        return {str(k): json_value(v) for k, v in value.items()}
    try:
        return [json_value(v) for v in value]
    except TypeError:
        return None


def custom_properties(owner):
    return {str(k): json_value(owner[k]) for k in owner.keys() if k != "_RNA_UI"}


def rna_properties(owner):
    result = {}
    for prop in owner.bl_rna.properties:
        if prop.identifier == "rna_type" or prop.type == 'COLLECTION':
            continue
        try:
            result[prop.identifier] = json_value(getattr(owner, prop.identifier))
        except (AttributeError, TypeError, ValueError):
            continue
    return result


def object_dependencies(obj):
    result = set()
    if obj.parent:
        result.add(obj.parent)
    owners = [obj]
    if obj.type == 'ARMATURE':
        owners.extend(obj.pose.bones)
    for owner in owners:
        for con in owner.constraints:
            for target in getattr(con, "targets", ()):
                if isinstance(target.target, bpy.types.Object):
                    result.add(target.target)
            for prop in con.bl_rna.properties:
                if prop.type == 'POINTER':
                    value = getattr(con, prop.identifier, None)
                    if isinstance(value, bpy.types.Object):
                        result.add(value)
    for owner in (obj, obj.data, getattr(obj.data, "shape_keys", None)):
        anim = getattr(owner, "animation_data", None)
        if anim:
            for curve in anim.drivers:
                for variable in curve.driver.variables:
                    for target in variable.targets:
                        if isinstance(target.id, bpy.types.Object):
                            result.add(target.id)
    for mod in obj.modifiers:
        target = getattr(mod, "object", None)
        if isinstance(target, bpy.types.Object):
            result.add(target)
    return result


def connected_objects(context, arm_obj):
    """Include consumers as well as targets: an output armature can copy controls."""
    objects = list(context.scene.objects)
    dependencies = {obj: object_dependencies(obj) for obj in objects}
    connected = {arm_obj}
    changed = True
    while changed:
        changed = False
        for obj, refs in dependencies.items():
            
            if obj in connected or (obj.type in {'MESH', 'ARMATURE'} and refs & connected):
                for entry in {obj} | refs:
                    if entry not in connected:
                        connected.add(entry)
                        changed = True
    return sorted(connected, key=lambda obj: (obj != arm_obj, obj.name))


def serialize_constraint(con, scale):
    result = rna_properties(con)
    if con.type == 'ARMATURE':
        result["targets"] = []
        for target in con.targets:
            item = rna_properties(target)
            obj, name = target.target, target.subtarget
            item["target_id"] = bone_id(obj, name) if obj and obj.type == 'ARMATURE' and name else None
            result["targets"].append(item)
    for prop, subtarget, output in (("target", "subtarget", "target_id"), ("pole_target", "pole_subtarget", "pole_target_id"), ("space_object", "space_subtarget", "space_target_id")):
        obj, name = getattr(con, prop, None), getattr(con, subtarget, "")
        if obj is not None:
            result[output] = bone_id(obj, name) if name and obj.type == 'ARMATURE' else obj.name
    if hasattr(con, "inverse_matrix"):
        result["inverse_matrix"] = matrix_values(con.inverse_matrix, scale)
    
    
    return result


def serialize_drivers(owner, scope):
    result = []
    anim = getattr(owner, "animation_data", None)
    if anim is None:
        return result
    for curve in anim.drivers:
        driver = curve.driver
        item = {"scope": scope, "data_path": curve.data_path, "array_index": curve.array_index,
                "type": driver.type, "expression": driver.expression, "use_self": driver.use_self,
                "mute": curve.mute, "valid": driver.is_valid, "variables": [],
                "curve": {"extrapolation": curve.extrapolation,
                    "keyframes": [rna_properties(key) for key in curve.keyframe_points],
                    "samples": [rna_properties(sample) for sample in curve.sampled_points],
                    "modifiers": []}}
        for modifier in curve.modifiers:
            settings = rna_properties(modifier)
            if hasattr(modifier, "control_points"):
                settings["control_points"] = [rna_properties(point) for point in modifier.control_points]
            item["curve"]["modifiers"].append(settings)
        try:
            value = owner.path_resolve(curve.data_path)
            if curve.array_index and not isinstance(value, (int, float, bool)):
                value = value[curve.array_index]
            item["evaluated_value"] = json_value(value)
        except (ValueError, IndexError, TypeError):
            pass
        for variable in driver.variables:
            v = {"name": variable.name, "type": variable.type, "targets": []}
            for target in variable.targets:
                t = rna_properties(target)
                if target.id is not None:
                    t["object"] = target.id.name
                    if getattr(target, "bone_target", "") and isinstance(target.id, bpy.types.Object) and target.id.type == 'ARMATURE':
                        t["target_id"] = bone_id(target.id, target.bone_target)
                    try:
                        t["evaluated_value"] = json_value(target.id.path_resolve(target.data_path))
                    except (ValueError, TypeError):
                        pass
                v["targets"].append(t)
            item["variables"].append(v)
        result.append(item)
    return result


def to_roblox(v, scale):
    return [round(v.x * scale, 9), round(v.z * scale, 9), round(-v.y * scale, 9)]


def to_roblox_dir(v):
    return [round(v.x, 6), round(v.z, 6), round(-v.y, 6)]


def color_to_list(c):
    return [round(c[0], 4), round(c[1], 4), round(c[2], 4)]


def export_bone_collections(arm_obj):
    """Keep names, hierarchy and overlapping membership in the portable JSON."""
    all_collections = getattr(arm_obj.data, "collections_all", None)
    if all_collections is None:
        all_collections = arm_obj.data.collections
    return [{"id": collection.name, "name": collection.name,
             "parent": collection.parent.name if getattr(collection, "parent", None) else None,
             "visible": bool(collection.is_visible_effectively),
             "visible_self": bool(collection.is_visible),
             "solo": bool(getattr(collection, "is_solo", False))}
            for collection in all_collections]


def bone_collection_hidden(pbone):
    cols = pbone.bone.collections
    return bool(cols) and not any(c.is_visible_effectively for c in cols)


def bone_hidden(arm_obj, pbone):
    return bool(pbone.bone.hide) or bone_collection_hidden(pbone)


def visible_collection_objects(context):
    """Viewport collection masks are separate from animated object.hide_viewport."""
    visible = set()
    def visit(layer, ancestors_visible):
        enabled = ancestors_visible and not layer.exclude and not layer.hide_viewport and not layer.collection.hide_viewport
        if enabled:
            visible.update(obj.name for obj in layer.collection.objects)
        for child in layer.children:
            visit(child, enabled)
    visit(context.view_layer.layer_collection, True)
    return visible


def resolve_bone_colors(context, pbone):
    theme = context.preferences.themes[0]
    v3d = theme.view_3d

    props = None
    if pbone.color.palette != 'DEFAULT':
        props = pbone.color
    elif pbone.bone.color.palette != 'DEFAULT':
        props = pbone.bone.color

    if props is not None:
        if props.palette == 'CUSTOM':
            cs = props.custom
            return color_to_list(cs.normal), color_to_list(cs.select), color_to_list(cs.active)
        try:
            idx = int(props.palette[-2:]) - 1
        except ValueError:
            idx = -1
        sets = theme.bone_color_sets
        if 0 <= idx < len(sets):
            cs = sets[idx]
            return color_to_list(cs.normal), color_to_list(cs.select), color_to_list(cs.active)

    pose = tuple(v3d.bone_pose)[:3]
    active = tuple(v3d.bone_pose_active)[:3]
    normal = tuple(c * DESELECT_SHADE for c in pose)
    return color_to_list(normal), color_to_list(pose), color_to_list(active)


def octahedron_edges(world_mat, length):
    r = 0.1 * length
    pts = [
        Vector((0, 0, 0)),
        Vector((r, r, r)), Vector((r, r, -r)), Vector((-r, r, -r)), Vector((-r, r, r)),
        Vector((0, length, 0)),
    ]
    idx_edges = [
        (0, 1), (0, 2), (0, 3), (0, 4),
        (1, 2), (2, 3), (3, 4), (4, 1),
        (1, 5), (2, 5), (3, 5), (4, 5),
    ]
    w = [world_mat @ p for p in pts]
    return [(w[a], w[b]) for a, b in idx_edges]


def custom_shape_world_matrix(arm_obj, pbone):
    override = pbone.custom_shape_transform or pbone
    bone_mat = arm_obj.matrix_world @ override.matrix
    size = pbone.bone.length if pbone.use_custom_shape_bone_size else 1.0
    sx, sy, sz = pbone.custom_shape_scale_xyz
    local = (
        Matrix.Translation(pbone.custom_shape_translation)
        @ pbone.custom_shape_rotation_euler.to_matrix().to_4x4()
        @ Matrix.Diagonal((sx * size, sy * size, sz * size, 1.0))
    )
    return bone_mat @ local


def planar_widget(vertices):
    """Flat custom widgets are outlines in Blender even with show_wire disabled."""
    if len(vertices) < 4:
        return True
    origin = vertices[0]
    offsets = [v - origin for v in vertices]
    axis = max(offsets, key=lambda v: v.length_squared)
    extent = axis.length
    if extent == 0:
        return True
    normal = max((axis.cross(v) for v in offsets), key=lambda v: v.length_squared)
    if normal.length <= extent * extent * 1e-7:
        return True
    normal.normalize()
    tolerance = max(extent * 1e-6, max(abs(c) for v in vertices for c in v) * 2**-22)
    return all(abs(normal.dot(v)) <= tolerance for v in offsets)


def shape_edges(world, mesh):
    verts = [world @ v.co for v in mesh.vertices]
    edge_keys = [tuple(e.vertices) for e in mesh.edges]
    if not edge_keys and mesh.polygons:
        seen = set()
        for poly in mesh.polygons:
            for k in poly.edge_keys:
                seen.add(k)
        edge_keys = list(seen)
    return [(verts[a], verts[b]) for a, b in edge_keys]


def shape_tris(world, mesh):
    mesh.calc_loop_triangles()
    verts = [world @ v.co for v in mesh.vertices]
    return [(verts[t.vertices[0]], verts[t.vertices[1]], verts[t.vertices[2]]) for t in mesh.loop_triangles]


def widget_names():
    names = set()
    for a in bpy.data.objects:
        if a.type == 'ARMATURE':
            for pb in a.pose.bones:
                if pb.custom_shape:
                    names.add(pb.custom_shape.name)
    return names


def normal_topology(mesh):
    """Keep polygon fans, sharp boundaries, and Blender's authored normal encoding."""
    polygons = [list(p.vertices) for p in mesh.polygons]
    corners, per_vertex, edge_users = {}, {}, {}
    for polygon in mesh.polygons:
        loops = list(polygon.loop_indices)
        for offset, index in enumerate(loops):
            loop = mesh.loops[index]
            prev, after = mesh.loops[loops[offset - 1]], mesh.loops[loops[(offset + 1) % len(loops)]]
            corner = {"index": index, "vertex": loop.vertex_index, "face": polygon.index,
                "previous": prev.vertex_index, "next": after.vertex_index,
                "before_edge": prev.edge_index, "after_edge": loop.edge_index, "smooth": polygon.use_smooth}
            corners[index] = corner
            per_vertex.setdefault(loop.vertex_index, []).append(index)
            edge_users.setdefault(loop.edge_index, []).append(index)
    attribute = mesh.attributes.get("custom_normal")
    encoded = attribute is not None and attribute.data_type == 'INT16_2D'
    groups, loop_groups = [], [0] * len(mesh.loops)
    for vertex, indices in per_vertex.items():
        neighbors = {}
        for index in indices:
            corner = corners[index]
            for direction, edge in (("before", corner["before_edge"]), ("after", corner["after_edge"])):
                users = edge_users[edge]
                if mesh.edges[edge].use_edge_sharp or not corner["smooth"] or len(users) != 2:
                    continue
                others = [other for other in indices if other != index and edge in (corners[other]["before_edge"], corners[other]["after_edge"])]
                if len(others) == 1:
                    other = corners[others[0]]
                    if other["smooth"] and edge == other["before_edge" if direction == "after" else "after_edge"]:
                        neighbors[(index, direction)] = other["index"]
        remaining = set(indices)
        while remaining:
            start = min(remaining)
            chain, current = [start], start
            while (current, "after") in neighbors:
                current = neighbors[(current, "after")]
                if current == start: break
                chain.append(current)
            cyclic = current == start and len(chain) > 1
            chain.reverse()
            if cyclic:
                first = chain.index(min(chain)); chain = chain[first:] + chain[:first]
            else:
                current = start
                while (current, "before") in neighbors:
                    current = neighbors[(current, "before")]; chain.append(current)
            remaining.difference_update(chain)
            group = {"vertex": vertex, "corners": chain,
                "faces": [corners[i]["face"] for i in chain],
                "previous": [corners[i]["previous"] for i in chain],
                "next": [corners[i]["next"] for i in chain]}
            if encoded:
                group["custom"] = [int(sum(attribute.data[i].value[axis] for i in chain) / len(chain)) for axis in (0, 1)]
            elif attribute is not None and attribute.data_type == 'FLOAT_VECTOR':
                group["custom_vector"] = [float(v) for v in mesh.corner_normals[chain[0]].vector]
            for index in chain: loop_groups[index] = len(groups)
            groups.append(group)
    return {"polygons": polygons, "groups": groups, "loop_groups": loop_groups}


def collect_reference(context, widgets, scale):
    mn = None
    mx = None
    for o in context.scene.objects:
        if o.type == 'MESH' and o.name not in widgets and o.visible_get():
            d = o.dimensions
            if min(abs(d.x), abs(d.y), abs(d.z)) < 1e-3:
                continue
            for corner in o.bound_box:
                w = o.matrix_world @ Vector(corner)
                if mn is None:
                    mn = w.copy()
                    mx = w.copy()
                for i in range(3):
                    mn[i] = min(mn[i], w[i])
                    mx[i] = max(mx[i], w[i])
    if mn is None:
        return None
    size = mx - mn
    center = (mn + mx) * 0.5
    return {
        "size": [round(abs(size.x) * scale, 4), round(abs(size.z) * scale, 4), round(abs(size.y) * scale, 4)],
        "center": to_roblox(center, scale),
    }


def bezier_weights(t):
    u = 1 - t
    return (u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t)


def curve_path(obj, deps, scale):
    splines = obj.data.splines
    if not splines:
        return None
    spline = splines[0]
    controls, points, radius, weights = [], [], [], []
    cyclic = bool(spline.use_cyclic_u)
    if spline.type == 'BEZIER':
        knots = spline.bezier_points
        for knot in knots:
            controls.extend([list(knot.handle_left * scale), list(knot.co * scale), list(knot.handle_right * scale)])
        count = len(knots)
        steps = max(1, spline.resolution_u)
        order = [count - 1] + list(range(count - 1)) if cyclic else list(range(count - 1))
        for k in order:
            n = (k + 1) % count
            for i in range(steps):
                t = i / steps
                w = bezier_weights(t)
                indices = (3 * k + 1, 3 * k + 2, 3 * n, 3 * n + 1)
                weights.append([[indices[j], round(w[j], 9)] for j in range(4) if w[j] != 0])
                radius.append(knots[k].radius + (knots[n].radius - knots[k].radius) * t)
        if not cyclic and count:
            weights.append([[3 * (count - 1) + 1, 1.0]])
            radius.append(knots[count - 1].radius)
    elif spline.type == 'POLY':
        for index, point in enumerate(spline.points):
            controls.append(list(point.co.xyz * scale))
            weights.append([[index, 1.0]])
            radius.append(point.radius)
    else:
        ev = obj.evaluated_get(deps)
        mesh = ev.to_mesh()
        try:
            for index, vertex in enumerate(mesh.vertices):
                controls.append(list(vertex.co * scale))
                weights.append([[index, 1.0]])
                radius.append(1.0)
        finally:
            ev.to_mesh_clear()
    for control in controls:
        control[:] = [round(v, 9) for v in control]
    hooks = []
    if spline.type in {'BEZIER', 'POLY'}:
        for modifier in obj.modifiers:
            if modifier.type != 'HOOK' or not modifier.show_viewport or modifier.object is None:
                continue
            target = modifier.object
            hooks.append({"object": target.name,
                          "bone": bone_id(target, modifier.subtarget) if modifier.subtarget and target.type == 'ARMATURE' else None,
                          "indices": list(modifier.vertex_indices), "strength": modifier.strength,
                          "falloff_type": modifier.falloff_type, "falloff_radius": modifier.falloff_radius * scale,
                          "center": [round(v * scale, 9) for v in modifier.center],
                          "matrix_inverse": matrix_values(modifier.matrix_inverse, scale)})
    return {"type": spline.type, "cyclic": cyclic, "controls": controls, "weights": weights,
            "radius": [round(r, 9) for r in radius], "hooks": hooks}


def relative_shape_keys(obj, scale):
    keys = obj.data.shape_keys
    if not keys:
        return []
    if not keys.use_relative:
        raise ValueError(f"Absolute shape keys on {obj.name} require conversion to relative keys")
    driven = {f.data_path for f in keys.animation_data.drivers} if keys.animation_data else set()
    result = []
    for key in keys.key_blocks:
        if key == keys.reference_key or (key.value == 0 and key.path_from_id("value") not in driven):
            continue
        group = obj.vertex_groups.get(key.vertex_group) if key.vertex_group else None
        deltas = []
        for index, (vertex, relative) in enumerate(zip(key.data, key.relative_key.data)):
            weight = 1.0
            if key.vertex_group:
                weight = next((assignment.weight for assignment in obj.data.vertices[index].groups
                               if group and assignment.group == group.index), 0.0)
            delta = (vertex.co - relative.co) * (scale * weight)
            if delta.length_squared > 1e-20:
                deltas.append([index] + [round(v, 9) for v in delta])
        result.append({"name": key.name, "value": float(key.value),
            "min": float(key.slider_min), "max": float(key.slider_max),
            "mute": bool(key.mute), "deltas": deltas})
    return result


def rigid_geometry(obj, deps, scale, binding):
    ev = obj.evaluated_get(deps)
    mesh = ev.to_mesh(preserve_all_data_layers=True, depsgraph=deps)
    try:
        mesh.calc_loop_triangles()
        uv = mesh.uv_layers.active
        faces = []
        for triangle in mesh.loop_triangles:
            face = {"vertices": list(triangle.vertices), "material_index": triangle.material_index,
                    "normals": [[round(v, 9) for v in mesh.corner_normals[i].vector] for i in triangle.loops]}
            if uv:
                face["uvs"] = [[round(v, 9) for v in uv.data[i].uv] for i in triangle.loops]
            faces.append(face)
        return {"object": obj.name, "bone": binding["bone"],
                "matrix": matrix_values(ev.matrix_world, scale, True),
                "vertices": [[round(v * scale, 9) for v in vertex.co] for vertex in mesh.vertices],
                "faces": faces,
                "materials": [{"name": slot.material.name, "color": list(slot.material.diffuse_color)}
                              if slot.material else None for slot in obj.material_slots]}
    finally:
        ev.to_mesh_clear()




def schedule_from_dot(text):
    stack=[];nodes={};edges=[]
    def quoted(line,key):
        match=re.search(r'\b'+key+r'="((?:\\.|[^"\\])*)"',line)
        return json.loads('"'+match.group(1)+'"') if match else ''
    for line in text.splitlines():
        if line.startswith('subgraph '):
            stack.append(dict(stack[-1]) if stack else {})
        elif line=='}' and stack:
            stack.pop()
        elif line.startswith('graph [') and stack:
            label=quoted(line,'label')
            if label.startswith('ID_REF : OB'):
                stack[-1]['object']=label[11:].split(' (orig: ',1)[0]
            elif label.startswith('[Bone Component] '):
                stack[-1]['bone']=re.match(r"\[Bone Component\] '(.*)' :",label)[1]
        elif line.startswith('"'):
            edge=re.match(r'"(\d+)" -> "(\d+)"',line)
            if edge:
                edges.append((edge[1],edge[2],quoted(line,'color')=='red4',quoted(line,'id')))
            else:
                node=re.match(r'"(\d+)"',line);label=quoted(line,'label')
                if node and stack and 'bone' in stack[-1] and label.startswith('BONE_'):
                    context=stack[-1];nodes[node[1]]={'bone':context['object']+'::'+context['bone'],'op':label.split('(')[0]}
    breaks=[]
    for a,b,cyclic,label in edges:
        if cyclic and a in nodes and b in nodes:
            breaks.append({'from':nodes[a],'to':nodes[b],'relation':label})
    return {'version':1,'cycle_breaks':breaks}


def evaluation_schedule(deps, bone_ids):
    import os
    import tempfile
    handle,path=tempfile.mkstemp(prefix="eclipse-evaluation-",suffix=".dot")
    os.close(handle)
    try:
        deps.debug_relations_graphviz(filepath=path)
        with open(path,encoding="utf-8") as stream:
            result=schedule_from_dot(stream.read())
        result["cycle_breaks"]=[edge for edge in result["cycle_breaks"] if edge["from"]["bone"] in bone_ids and edge["to"]["bone"] in bone_ids]
        return result
    finally:
        os.remove(path)

def collect_armature(context, arm_obj, scale):
    context.view_layer.update()
    deps = context.evaluated_depsgraph_get()
    objects = connected_objects(context, arm_obj)
    armatures = [obj for obj in objects if obj.type == 'ARMATURE']
    result = {"format": "EclipseArmature", "version": 4, "name": arm_obj.name, "scale": scale,
              "matrix_layout": "row-major-4x4", "world_axes": "+X,+Z,-Y", "bone_axes": "BLENDER_LOCAL_XYZ",
              "primary_armature": arm_obj.name, "source_blender_version": bpy.app.version_string,
              "source_eclipse_version": "1.5.11", "features": ["relative_shape_keys"],
              "frame": context.scene.frame_current, "fps": context.scene.render.fps / context.scene.render.fps_base, "bones": [], "armatures": [], "objects": [],
              "drivers": [], "mesh_bindings": [], "skin_meshes": [], "rigid_meshes": [], "features": {"constraints": [], "driver_types": [], "inherit_scale": []}}
    constraints, driver_types, inherit = set(), set(), set()
    geometry_bindings = {}
    skinning = set()
    for obj in objects:
        if obj.type != 'MESH':
            continue
        bindings = []
        weighted_groups = {}
        for vertex in obj.data.vertices:
            for group in vertex.groups:
                if group.weight > 0:
                    weighted_groups[group.group] = weighted_groups.get(group.group, 0) + 1
        for modifier in obj.modifiers:
            target = getattr(modifier, "object", None)
            if modifier.type == 'ARMATURE' and target in armatures:
                evaluated_arm = target.evaluated_get(deps)
                mesh_to_arm = target.matrix_world.inverted_safe() @ obj.matrix_world
                groups = {group.index: group for group in obj.vertex_groups}
                mesh = obj.data
                mesh.calc_loop_triangles()
                vertices = []
                for vertex in mesh.vertices:
                    weights = []
                    for assignment in vertex.groups:
                        group = groups.get(assignment.group)
                        pb = target.pose.bones.get(group.name) if group else None
                        if assignment.weight <= 0 or pb is None or not pb.bone.use_deform:
                            continue
                        weight = {"bone": bone_id(target, pb.name), "weight": float(assignment.weight)}
                        if pb.bone.bbone_segments > 1:
                            segment, blend = evaluated_arm.pose.bones[pb.name].bbone_segment_index(mesh_to_arm @ vertex.co)
                            weight["segment"], weight["blend"] = segment, round(blend, 9)
                        weights.append(weight)
                    vertices.append({"position": [round(v * scale, 9) for v in vertex.co], "weights": weights})
                uv_layer = mesh.uv_layers.active
                faces = []
                for triangle in mesh.loop_triangles:
                    face = {"vertices": list(triangle.vertices), "loops": list(triangle.loops), "material_index": triangle.material_index}
                    if uv_layer:
                        face["uvs"] = [[round(value, 9) for value in uv_layer.data[index].uv] for index in triangle.loops]
                    face["normals"] = [[round(value, 9) for value in mesh.corner_normals[index].vector] for index in triangle.loops]
                    faces.append(face)
                result["skin_meshes"].append({"object": obj.name, "armature": target.name,
                    "matrix": matrix_values(obj.matrix_world, scale, True),
                    "modifier": rna_properties(modifier), "vertices": vertices, "faces": faces,
                    "normal_topology": normal_topology(mesh),
                    "shape_keys": relative_shape_keys(obj, scale),
                    "materials": [slot.material.name if slot.material else None for slot in obj.material_slots]})
                for group in obj.vertex_groups:
                    if group.index in weighted_groups and group.name in target.pose.bones:
                        key = bone_id(target, group.name)
                        bindings.append({"bone": key, "kind": "SKIN", "weighted_vertices": weighted_groups[group.index]})
                        skinning.add(key)
        if obj.parent in armatures and obj.parent_type == 'BONE' and obj.parent_bone:
            bindings.append({"bone": bone_id(obj.parent, obj.parent_bone), "kind": "BONE_PARENT"})
        
        
        for con in obj.constraints:
            target = getattr(con, "target", None)
            subtarget = getattr(con, "subtarget", "")
            if target in armatures and subtarget and con.type in {'CHILD_OF', 'COPY_TRANSFORMS'}:
                bindings.append({"bone": bone_id(target, subtarget), "kind": con.type,
                    "constraint": con.name, "influence": con.influence, "mute": con.mute})
        if bindings and not any(b["kind"] == "SKIN" for b in bindings):
            active = [b for b in bindings if not b.get("mute") and b.get("influence", 1.0) >= 1.0 - 1e-6]
            if len(active) == 1:
                result["rigid_meshes"].append(rigid_geometry(obj, deps, scale, active[0]))
        if bindings:
            result["mesh_bindings"].append({"object": obj.name, "visible": obj.visible_get(), "bindings": bindings})
            for binding in bindings:
                geometry_bindings.setdefault(binding["bone"], []).append(obj.name)
    visible_objects = visible_collection_objects(context)
    for obj in objects:
        ev = obj.evaluated_get(deps)
        result["objects"].append({"name": obj.name, "type": obj.type, "rotation_mode": obj.rotation_mode,
            "viewport_visible": bool(obj.visible_get()), "view_layer_hidden": bool(obj.hide_get()),
            "collection_hidden": obj.name not in visible_objects,
            "hide_viewport": bool(obj.hide_viewport), "hide_render": bool(obj.hide_render), "hide_select": bool(obj.hide_select),
            "matrix": matrix_values(ev.matrix_world, scale, True),
            "matrix_local": matrix_values(obj.matrix_local, scale),
            "matrix_basis": matrix_values(obj.matrix_basis, scale),
            "parent_inverse": matrix_values(obj.matrix_parent_inverse, scale),
            "parent": obj.parent.name if obj.parent else None, "parent_type": obj.parent_type,
            "parent_bone": bone_id(obj.parent, obj.parent_bone) if obj.parent and obj.parent_bone else None,
            "properties": custom_properties(obj),
            "constraints": [serialize_constraint(c, scale) for c in obj.constraints]})
        if obj.type == 'CURVE':
            result["objects"][-1]["curve"] = curve_path(obj, deps, scale)
        result["drivers"].extend(serialize_drivers(obj, {"object": obj.name, "type": "OBJECT"}))
        if obj.data:
            result["drivers"].extend(serialize_drivers(obj.data, {"object": obj.name, "type": "DATA"}))
            keys = getattr(obj.data, "shape_keys", None)
            if keys:
                paths = {key.path_from_id("value"): key.name for key in keys.key_blocks}
                for driver in serialize_drivers(keys, {"object": obj.name, "type": "SHAPE_KEYS"}):
                    driver["shape_key"] = paths.get(driver["data_path"])
                    result["drivers"].append(driver)

    for obj in armatures:
        ev = obj.evaluated_get(deps)
        result["armatures"].append({"name": obj.name, "matrix": matrix_values(ev.matrix_world, scale, True),
            "pose_position": obj.data.pose_position, "display_type": obj.data.display_type,
            "bone_collections": export_bone_collections(obj),
            "show_in_front": obj.show_in_front, "properties": custom_properties(obj.data)})
        for pbone in obj.pose.bones:
            pb = ev.pose.bones[pbone.name]
            bone = pbone.bone
            normal, selected, active = resolve_bone_colors(context, pbone)
            entry = {"id": bone_id(obj, pbone.name), "armature": obj.name, "name": pbone.name,
                "parent": bone_id(obj, pbone.parent.name) if pbone.parent else None,
                "head": to_roblox(ev.matrix_world @ pb.head, scale),
                "tail": to_roblox(ev.matrix_world @ pb.tail, scale),
                "rest_head": to_roblox(ev.matrix_world @ bone.head_local, scale),
                "rest_tail": to_roblox(ev.matrix_world @ bone.tail_local, scale),
                "rest_matrix": matrix_values(ev.matrix_world @ bone.matrix_local, scale, True),
                "pose_matrix": matrix_values(ev.matrix_world @ pb.matrix, scale, True),
                "basis_matrix": matrix_values(pbone.matrix_basis, scale),
                "rest_local_matrix": matrix_values(bone.parent.matrix_local.inverted_safe() @ bone.matrix_local if bone.parent else bone.matrix_local, scale),
                "length": round(bone.length * scale, 9), "connected": bone.use_connect,
                "inherit_rotation": bone.use_inherit_rotation, "inherit_scale": bone.inherit_scale,
                "local_location": bone.use_local_location, "rotation_mode": pbone.rotation_mode,
                "lock_location": list(pbone.lock_location), "lock_rotation": list(pbone.lock_rotation),
                "lock_rotation_w": pbone.lock_rotation_w, "lock_rotations_4d": pbone.lock_rotations_4d,
                "lock_scale": list(pbone.lock_scale), "color": normal, "select": selected, "active": active,
                "solid": False, "hidden": bone_hidden(obj, pbone), "hide_select": bool(bone.hide_select), "deform": bone.use_deform,
                "visibility": {"bone_hidden": bool(bone.hide), "collection_hidden": bone_collection_hidden(pbone)},
                "collections": [collection.name for collection in bone.collections],
                "skinning": bone_id(obj, pbone.name) in skinning,
                "geometry_binding": bone_id(obj, pbone.name) in geometry_bindings,
                "bound_meshes": geometry_bindings.get(bone_id(obj, pbone.name), []),
                "bbone_segments": bone.bbone_segments,
                "properties": custom_properties(pbone), "constraints": [serialize_constraint(c, scale) for c in pb.constraints],
                "ik_settings": {key: json_value(getattr(pbone, key)) for key in (
                    "lock_ik_x", "lock_ik_y", "lock_ik_z", "use_ik_limit_x", "use_ik_limit_y", "use_ik_limit_z",
                    "ik_min_x", "ik_max_x", "ik_min_y", "ik_max_y", "ik_min_z", "ik_max_z",
                    "ik_stiffness_x", "ik_stiffness_y", "ik_stiffness_z", "ik_stretch")},
                "visual_owner": bone_id(obj, pbone.custom_shape_transform.name if pbone.custom_shape_transform else pbone.name)}
            entry["display"] = {
                "geometry_scale": scale,
                "custom_shape_scale_xyz": list(pb.custom_shape_scale_xyz),
                "custom_shape_translation": [float(v) * scale for v in pb.custom_shape_translation],
                "custom_shape_rotation_euler": list(pb.custom_shape_rotation_euler),
                "use_custom_shape_bone_size": pb.use_custom_shape_bone_size,
                "length": float(bone.length) * scale,
            }
            if bone.bbone_segments > 1:
                entry["bbone"] = {"bone_properties": {key: value for key, value in rna_properties(bone).items() if key.startswith("bbone_")},
                    "pose_properties": {key: value for key, value in rna_properties(pb).items() if key.startswith("bbone_")},
                    "rest_segments": [matrix_values(pb.bbone_segment_matrix(index, rest=True), scale) for index in range(bone.bbone_segments + 1)],
                    "pose_segments": [matrix_values(pb.bbone_segment_matrix(index, rest=False), scale) for index in range(bone.bbone_segments + 1)]}
                for end in ("start", "end"):
                    handle = getattr(bone, "bbone_custom_handle_" + end)
                    if handle:
                        entry["bbone"]["custom_handle_" + end] = bone_id(obj, handle.name)
            
            
            handled = False
            if pbone.custom_shape:
                shape = pbone.custom_shape.evaluated_get(deps)
                mesh = None
                try:
                    mesh = shape.to_mesh()
                    if mesh is not None:
                        
                        
                        entry["display"]["vertices"] = [[float(v) for v in vertex.co] for vertex in mesh.vertices]
                        entry["display"]["edges"] = [list(edge.vertices) for edge in mesh.edges]
                        mesh.calc_loop_triangles()
                        entry["display"]["triangles"] = [list(triangle.vertices) for triangle in mesh.loop_triangles]
                        world = custom_shape_world_matrix(ev, pb)
                        planar = planar_widget([vertex.co for vertex in mesh.vertices])
                        if planar:
                            entry["display"]["triangles"] = []
                        if mesh.polygons and not bone.show_wire and not planar:
                            entry["solid"] = True
                            entry["tris"] = [[to_roblox(a, scale), to_roblox(b, scale), to_roblox(c, scale)] for a, b, c in shape_tris(world, mesh)]
                        entry["edges"] = [[to_roblox(a, scale), to_roblox(b, scale)] for a, b in shape_edges(world, mesh)]
                        handled = True
                except RuntimeError:
                    pass
                finally:
                    if mesh is not None:
                        shape.to_mesh_clear()
            if not handled:
                world = ev.matrix_world @ pb.matrix
                entry["edges"] = [[to_roblox(a, scale), to_roblox(b, scale)] for a, b in octahedron_edges(world, bone.length)]
                if obj.data.display_type == 'OCTAHEDRAL':
                    length = bone.length
                    radius = length * 0.1
                    vertices = [Vector((0, 0, 0)), Vector((radius, radius, radius)), Vector((radius, radius, -radius)),
                        Vector((-radius, radius, -radius)), Vector((-radius, radius, radius)), Vector((0, length, 0))]
                    vertices = [world @ vertex for vertex in vertices]
                    faces = [(0, 2, 1), (0, 3, 2), (0, 4, 3), (0, 1, 4), (5, 1, 2), (5, 2, 3), (5, 3, 4), (5, 4, 1)]
                    entry["solid"] = True
                    entry["tris"] = [[to_roblox(vertices[index], scale) for index in face] for face in faces]
            constraints.update(c.type for c in pb.constraints)
            inherit.add(bone.inherit_scale)
            result["bones"].append(entry)
    result["evaluation_schedule"]=evaluation_schedule(deps,{b["id"] for b in result["bones"]})
    driver_types.update(d["type"] for d in result["drivers"])
    result["features"] = {"constraints": sorted(constraints), "driver_types": sorted(driver_types), "inherit_scale": sorted(inherit),
        "bendy_bones": any(b["bbone_segments"] > 1 for b in result["bones"]),
        "skinned_bendy_bones": [b["id"] for b in result["bones"] if b["skinning"] and b["bbone_segments"] > 1]}
    reference = collect_reference(context, widget_names(), scale)
    if reference:
        result["reference"] = reference
    return result


@contextmanager
def rest_pose(context):
    saved = []
    for obj in context.scene.objects:
        if obj.type != 'ARMATURE':
            continue
        entry = {"obj": obj, "bones": []}
        ad = obj.animation_data
        if ad and not getattr(ad, "use_tweak_mode", False):
            action, slot, nla = ad.action, getattr(ad, "action_slot", None), ad.use_nla
            try:
                ad.action = None
                ad.use_nla = False
                entry["action"], entry["slot"], entry["nla"] = action, slot, nla
            except (AttributeError, RuntimeError, TypeError):
                try:
                    if ad.action is None and action is not None:
                        ad.action = action
                    ad.use_nla = nla
                except (AttributeError, RuntimeError, TypeError):
                    pass
        for pb in obj.pose.bones:
            entry["bones"].append((pb, pb.location.copy(), pb.rotation_quaternion.copy(), pb.rotation_euler.copy(),
                                   tuple(pb.rotation_axis_angle), pb.scale.copy()))
            pb.location = (0.0, 0.0, 0.0)
            pb.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
            pb.rotation_euler = (0.0, 0.0, 0.0)
            pb.rotation_axis_angle = (0.0, 0.0, 1.0, 0.0)
            pb.scale = (1.0, 1.0, 1.0)
        saved.append(entry)
    context.view_layer.update()
    try:
        yield
    finally:
        for entry in saved:
            obj = entry["obj"]
            ad = obj.animation_data
            if ad and "action" in entry:
                try:
                    ad.use_nla = entry["nla"]
                    ad.action = entry["action"]
                except (AttributeError, RuntimeError, TypeError):
                    pass
                if entry["slot"] is not None and hasattr(ad, "action_slot"):
                    try:
                        ad.action_slot = entry["slot"]
                    except Exception:
                        pass
        context.view_layer.update()
        for entry in saved:
            for pb, loc, quat, eul, axis, scl in entry["bones"]:
                pb.location = loc
                pb.rotation_quaternion = quat
                pb.rotation_euler = eul
                pb.rotation_axis_angle = axis
                pb.scale = scl
        context.view_layer.update()


class ECLIPSE_OT_export_armature(bpy.types.Operator, ExportHelper):
    bl_idname = "eclipse.export_armature"
    bl_label = "Export Armature (.json)"
    bl_description = "Export this armature, its control dependencies, bind transforms and bone visuals"

    filename_ext = ".json"
    filter_glob: bpy.props.StringProperty(default="*.json", options={'HIDDEN'})

    scale: bpy.props.FloatProperty(name="Studs per Unit", default=1.0, min=0.001, max=1000.0)

    def find_armature(self, context):
        obj = context.active_object
        if obj and obj.type == 'ARMATURE':
            return obj
        best = None
        best_shapes = -1
        for o in context.selected_objects:
            if o.type == 'ARMATURE':
                n = sum(1 for pb in o.pose.bones if pb.custom_shape)
                if n > best_shapes:
                    best_shapes = n
                    best = o
        return best

    def invoke(self, context, event):
        arm = self.find_armature(context)
        if arm is None:
            self.report({'ERROR'}, "Select an armature first")
            return {'CANCELLED'}
        self.scale = context.scene.eclipse_export_scale
        if not self.filepath:
            self.filepath = bpy.path.clean_name(arm.name) + ".json"
        return ExportHelper.invoke(self, context, event)

    def execute(self, context):
        arm = self.find_armature(context)
        if arm is None:
            self.report({'ERROR'}, "Select an armature first")
            return {'CANCELLED'}
        with rest_pose(context):
            data = collect_armature(context, arm, self.scale)
        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        nhidden = sum(1 for b in data["bones"] if b.get("hidden"))
        nshapes = sum(len(mesh.get("shape_keys", [])) for mesh in data.get("skin_meshes", []))
        self.report({'INFO'}, f"Exported {len(data['bones'])} bones ({nhidden} hidden), {nshapes} shape keys")
        return {'FINISHED'}


class ECLIPSE_PT_export_panel(bpy.types.Panel):
    bl_label = "Eclipse Armature"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Eclipse"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        col = layout.column()
        if obj and obj.type == 'ARMATURE':
            col.label(text=f"Armature: {obj.name}", icon='ARMATURE_DATA')
        else:
            col.label(text="Select an armature", icon='ERROR')
        col.prop(context.scene, "eclipse_export_scale")
        col.operator("eclipse.export_armature", icon='EXPORT')


classes = (ECLIPSE_OT_export_armature, ECLIPSE_PT_export_panel)


_registered = False


def register():
    global _registered
    if _registered:
        return
    legacy = sys.modules.get("eclipse_armature_export")
    if legacy is not None and getattr(legacy, "_registered", False):
        legacy.unregister()
    if hasattr(bpy.types, "ECLIPSE_PT_export_panel"):
        raise RuntimeError("Another Eclipse armature exporter is registered. Disable the standalone exporter and enable Eclipse again.")
    if not hasattr(bpy.types.Scene, "eclipse_export_scale"):
        bpy.types.Scene.eclipse_export_scale = bpy.props.FloatProperty(name="Studs per Unit", default=1.0, min=0.001, max=1000.0)
    for cls in classes:
        bpy.utils.register_class(cls)
    _registered = True


def unregister():
    global _registered
    if not _registered:
        return
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    if hasattr(bpy.types.Scene, "eclipse_export_scale"):
        del bpy.types.Scene.eclipse_export_scale
    _registered = False
