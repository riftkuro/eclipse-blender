import bpy
import json
import math
import re
from mathutils import Matrix, Vector, kdtree
from .transforms import unpack, restored, set_camera_fov
from .rig_appearance import restore_appearance


def import_rig(filepath):
    before = set(bpy.data.objects)
    selected = list(bpy.context.selected_objects)
    active = bpy.context.view_layer.objects.active
    old_mode = bpy.context.object.mode if bpy.context.object else 'OBJECT'
    if old_mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    created = []
    try:
        bpy.ops.wm.obj_import(filepath=filepath, use_split_objects=True, use_split_groups=True,
                              forward_axis='NEGATIVE_Z', up_axis='Y')
        imported = [obj for obj in bpy.data.objects if obj not in before]
        created.extend(imported)
        chunks, markers = {}, []
        for obj in imported:
            match = re.search(r'^eclipse(\d+)q1([a-f0-9]+)q1', obj.name, re.I)
            if match:
                chunks[int(match[1])] = match[2]
                markers.append(obj)
        if not chunks or sorted(chunks) != list(range(1, len(chunks) + 1)):
            raise ValueError('This OBJ needs Eclipse rig metadata. Use Export Rig in Eclipse.')
        metadata = json.loads(bytes.fromhex(''.join(chunks[i] for i in sorted(chunks))).decode('utf-8'))
        if metadata.get('format') != 'EclipseRig' or metadata.get('version') != 1:
            raise ValueError('Unsupported Eclipse rig file')
        for obj in markers:
            created.remove(obj)
            imported.remove(obj)
        bpy.data.batch_remove(markers)
        outputs = []
        for rig in metadata['rigs']:
            if rig['kind'] == 'camera':
                data = bpy.data.cameras.new(rig['name'])
                obj = bpy.data.objects.new(rig['name'], data)
                bpy.context.collection.objects.link(obj)
                created.append(obj)
                obj.matrix_world = restored(unpack(rig.get('camera', rig['origin'])))
                set_camera_fov(obj, rig.get('fov', 70))
                outputs.append(obj)
                continue
            matches = [o for o in bpy.context.scene.objects if o.type == 'ARMATURE' and o.get('eclipse_rig_id') == rig['id']] if metadata.get('accessory') else []
            if len(matches) > 1:
                raise ValueError('More than one imported rig matches this accessory. Keep one target rig in the scene.')
            existing = matches[0] if matches else None
            if existing:
                obj = existing
                names = {b.get('eclipse_track_id'): b.name for b in obj.data.bones if b.get('eclipse_track_id')}
                if any(p.get('track') and p['track'] not in names for p in rig['parts']):
                    raise ValueError('The accessory references a new bone. Export/import the updated rig first.')
            else:
                data = bpy.data.armatures.new(rig['name'])
                obj = bpy.data.objects.new(rig['name'], data)
                bpy.context.collection.objects.link(obj)
                created.append(obj)
                obj['eclipse_rig_id'] = rig['id']
                obj.matrix_world = restored(unpack(rig['origin']))
                bpy.ops.object.select_all(action='DESELECT')
                obj.select_set(True)
                bpy.context.view_layer.objects.active = obj
                bpy.ops.object.mode_set(mode='EDIT')
                names = {}
                pivots = {}
                for track in rig['tracks']:
                    bone = data.edit_bones.new(track['name'])
                    rest = restored(unpack(track['rest']))
                    frame, length = rest, .25
                    if track.get('joint'):
                        joint = restored(unpack(track['joint'])).translation
                        along = rest.to_3x3().inverted() @ (rest.translation - joint)
                        if along.y < -1e-6:
                            frame = rest @ Matrix.Rotation(math.pi, 4, 'X')
                        length = max(.25, abs(along.y))
                        frame = frame.copy()
                        frame.translation = joint
                        pivots[track['id']] = 'joint'
                    bone.head, bone.tail = (0, 0, 0), (0, length, 0)
                    bone.matrix = obj.matrix_world.inverted() @ frame
                    names[track['id']] = bone.name
                for track in rig['tracks']:
                    if track.get('parent') in names:
                        data.edit_bones[names[track['id']]].parent = data.edit_bones[names[track['parent']]]
                bpy.ops.object.mode_set(mode='OBJECT')
                for key, name in names.items():
                    data.bones[name]['eclipse_track_id'] = key
                    if key in pivots:
                        data.bones[name]['eclipse_pivot'] = pivots[key]
            face_atlas = None
            if rig.get('meshGroup'):
                for candidate in imported:
                    if not re.match(re.escape(rig['meshGroup']) + r'(?=\d|\.|$)', candidate.name, re.I):
                        continue
                    for mat in candidate.data.materials:
                        if not mat or not mat.use_nodes:
                            continue
                        bsdf = mat.node_tree.nodes.get('Principled BSDF')
                        for link in bsdf.inputs['Base Color'].links if bsdf else []:
                            node = link.from_node
                            if node.type == 'TEX_IMAGE' and node.image and tuple(node.image.size) == (1024, 512):
                                face_atlas = node.image
            assigned = set()
            for part in rig['parts']:
                meshes = [mesh for mesh in imported if re.match(re.escape(part['token']), mesh.name, re.I)]
                if part.get('meshGroup'):
                    # Roblox combines humanoid groups under the model name. Match each
                    # rendered body mesh by its authored rest-position bounds, once only.
                    candidates = [m for m in imported if m not in assigned and re.match(re.escape(part['meshGroup']) + r'(?=\d|\.|$)', m.name, re.I)]
                    expected = restored(unpack(part['matrix'])).translation
                    def score(m):
                        points = [m.matrix_world @ Vector(c) for c in m.bound_box]
                        center = (Vector(tuple(min(v[i] for v in points) for i in range(3))) + Vector(tuple(max(v[i] for v in points) for i in range(3)))) * .5
                        return (center - expected).length_squared
                    if not candidates:
                        raise ValueError('Missing rendered character geometry: ' + part['name'])
                    meshes = [min(candidates, key=score)]
                if not meshes:
                    raise ValueError('Missing exported geometry: ' + part['name'] + '. Re-export the fully loaded rig from Eclipse.')
                for mesh in meshes:
                    assigned.add(mesh)
                    restore_appearance(mesh, part, filepath, face_atlas)
                    mesh.name = part['name']
                    if existing:
                        mesh.matrix_world = obj.matrix_world @ restored(unpack(rig['origin'])).inverted() @ mesh.matrix_world
                    if part.get('skin'):
                        tree = kdtree.KDTree(len(part['skin']))
                        for index, vertex in enumerate(part['skin']):
                            world = restored(Matrix.Translation(vertex['position'])).translation
                            tree.insert(world, index)
                        tree.balance()
                        for key, name in names.items():
                            mesh.vertex_groups.new(name=name)
                        for vertex in mesh.data.vertices:
                            _, index, distance = tree.find(mesh.matrix_world @ vertex.co)
                            if distance > .02:
                                raise ValueError('OBJ skin geometry does not match its rig metadata: ' + mesh.name)
                            for key, weight in part['skin'][index]['weights']:
                                if isinstance(key, int):
                                    key = rig['tracks'][key - 1]['id']
                                mesh.vertex_groups[names[key]].add([vertex.index], weight, 'REPLACE')
                        modifier = mesh.modifiers.new('Eclipse Rig', 'ARMATURE')
                        modifier.object = obj
                    elif part.get('track') in names:
                        bone = names[part['track']]
                        world = mesh.matrix_world.copy()
                        constraint = mesh.constraints.new('CHILD_OF')
                        constraint.name = 'Eclipse Rig'
                        constraint.target, constraint.subtarget = obj, bone
                        constraint.inverse_matrix = (obj.matrix_world @ obj.pose.bones[bone].matrix).inverted()
                        mesh.matrix_basis = world
                    else:
                        world = mesh.matrix_world.copy()
                        mesh.parent = obj
                        mesh.matrix_world = world
            obj.show_in_front = True
            outputs.append(obj)
        bpy.context.view_layer.update()
        bpy.ops.object.select_all(action='DESELECT')
        for obj in outputs:
            obj.select_set(True)
        if outputs:
            bpy.context.view_layer.objects.active = outputs[0]
        # Imported materials/textures are invisible in Blender's default solid view.
        if bpy.context.area and bpy.context.area.type == 'VIEW_3D':
            bpy.context.area.spaces.active.shading.type = 'MATERIAL'
        return outputs
    except Exception:
        if bpy.context.object and bpy.context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        bpy.data.batch_remove([obj for obj in created if obj.name in bpy.data.objects])
        for obj in selected:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = active
        if active and old_mode != 'OBJECT':
            bpy.ops.object.mode_set(mode=old_mode)
        raise
