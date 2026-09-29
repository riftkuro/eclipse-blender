"""Restore Roblox appearance omitted by Studio's legacy OBJ exporter."""
import json
from pathlib import Path
import bpy
from mathutils import Vector
from .transforms import restored, unpack

ASSETS = Path(__file__).with_name('assets')


def solid_material(name, color, opacity=1):
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = (*color, opacity)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get('Principled BSDF')
    bsdf.inputs['Base Color'].default_value = (*color, 1)
    bsdf.inputs['Alpha'].default_value = opacity
    bsdf.inputs['Roughness'].default_value = .7
    return mat


def restore_appearance(obj, part, filepath, face_atlas=None):
    appearance = part.get('appearance')
    if not appearance:
        return
    if appearance.get('rendered'):
        # Current exports contain Studio's actual rendered surface, custom meshes,
        # avatar clothing and decals. Never replace them with a template head or
        # a solid-color material. The paths/UVs are authored by the native exporter.
        for mat in obj.data.materials:
            if mat and mat.use_nodes:
                for node in mat.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image:
                        if not node.image.has_data:
                            raise ValueError('Missing exported texture: ' + node.image.filepath)
                        if not node.image.packed_file:
                            node.image.pack()
        return
    color = [max(0, min(1, c)) ** 2.2 for c in appearance.get('color', [.64, .64, .64])]
    obj.color = (*color, appearance.get('opacity', 1))
    special = appearance.get('special', {})
    if appearance.get('nativeCharacter') and special.get('type') != 'Head':
        for mat in obj.data.materials:
            if mat and mat.use_nodes:
                for node in mat.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image and not node.image.packed_file:
                        node.image.pack()
        return
    if special.get('type') != 'Head':
        # Keep OBJ image/UV materials for textured geometry. Plain parts carry
        # their own color instead of inheriting the first metadata marker's MTL.
        if not appearance.get('decals') and not special.get('texture'):
            if appearance.get('class') == 'Part' and not special:
                obj.data.materials.clear()
                obj.data.materials.append(solid_material(part['name'], color, appearance.get('opacity', 1)))
        return
    source = json.loads((ASSETS / 'classic-head.json').read_text(encoding='utf8'))
    size = Vector(appearance['size'])
    factor = Vector(special['scale'])
    # Head MeshType uses the smaller X/Z span and a 1.25 conversion factor.
    # Applying raw SpecialMesh.Scale to the file mesh made heads 25% too large.
    radial = min(size.x * factor.x, size.z * factor.z) / 1.25
    factor = Vector((radial, size.y * factor.y / 1.25, radial))
    offset = Vector(special['offset'])
    vertices = [tuple(Vector(p) * factor + offset) for p in source['positions']]
    data = bpy.data.meshes.new(part['name'] + ' Surface')
    data.from_pydata(vertices, [], source['faces'])
    data.update()
    uv = data.uv_layers.new(name='UVMap')
    for polygon, values in zip(data.polygons, source['uvs']):
        polygon.use_smooth = True
        for loop, value in zip(polygon.loop_indices, values):
            uv.data[loop].uv = value
    normals = [tuple(Vector((n[0] / factor.x, n[1] / factor.y, n[2] / factor.z)).normalized())
               for face in source['normals'] for n in face]
    data.normals_split_custom_set(normals)
    old = obj.data
    obj.data = data
    if old.users == 0:
        bpy.data.meshes.remove(old)
    obj.matrix_world = restored(unpack(part['matrix']))
    mat = solid_material(part['name'], color, appearance.get('opacity', 1))
    data.materials.append(mat)
    if appearance.get('nativeCharacter'):
        if face_atlas is None:
            raise ValueError('The exported character texture atlas is missing. Keep the OBJ, MTL and textures together.')
        # Studio has already composited the actual face, including its color and
        # transparency, into the R6 atlas. Never substitute a bundled smile.
        face_atlas.pack()
        face_mat = solid_material(part['name'] + ' Exported Face', color, appearance.get('opacity', 1))
        nodes, links = face_mat.node_tree.nodes, face_mat.node_tree.links
        tex = nodes.new('ShaderNodeTexImage');tex.image = face_atlas
        coord = nodes.new('ShaderNodeUVMap');coord.uv_map = 'EclipseExportedFace'
        links.new(coord.outputs['UV'], tex.inputs['Vector'])
        mix = nodes.new('ShaderNodeMixRGB');mix.blend_type = 'MIX'
        mix.inputs[1].default_value = (*color, 1)
        links.new(tex.outputs['Alpha'], mix.inputs[0])
        links.new(tex.outputs['Color'], mix.inputs[2])
        links.new(mix.outputs[0], nodes.get('Principled BSDF').inputs['Base Color'])
        data.materials.append(face_mat)
        projected = data.uv_layers.new(name=coord.uv_map)
        for polygon, values in zip(data.polygons, source['uvs']):
            if polygon.normal.z < -.05 and any(u or v for u, v in values):
                polygon.material_index = 1
            for loop, (u, v) in zip(polygon.loop_indices, values):
                # Native head UVs are square; the old Part.Size projection
                # stretched the face horizontally on its 2x1 collision box.
                projected.data[loop].uv = ((896.5 + (1-u)*127)/1024, (384.5 + (1-v)*127)/512)
        obj['eclipse_face_source'] = face_atlas.name
        return
    # Roblox's default smile is a front-projected Decal, not the mesh's UV texture.
    face = next((d for d in appearance.get('decals', [])
                 if d['face'] == 'Front' and d['texture'].lower() == 'rbxasset://textures/face.png'), None)
    if face:
        projected = data.uv_layers.new(name='EclipseFrontDecal')
        for polygon, values in zip(data.polygons, source['uvs']):
            for loop, (u, v) in zip(polygon.loop_indices, values):
                projected.data[loop].uv = (1-u, 1-v) if polygon.normal.z < -.05 else (-2, -2)
        nodes, links = mat.node_tree.nodes, mat.node_tree.links
        tex = nodes.new('ShaderNodeTexImage')
        tex.image = bpy.data.images.load(str(ASSETS / 'classic-face.png'), check_existing=True)
        tex.image.pack()
        tex.extension = 'CLIP'
        coord = nodes.new('ShaderNodeUVMap');coord.uv_map = projected.name
        links.new(coord.outputs['UV'], tex.inputs['Vector'])
        mix = nodes.new('ShaderNodeMixRGB');mix.blend_type = 'MIX'
        mix.inputs[1].default_value = (*color, 1)
        links.new(tex.outputs['Alpha'], mix.inputs[0])
        links.new(tex.outputs['Color'], mix.inputs[2])
        links.new(mix.outputs[0], nodes.get('Principled BSDF').inputs['Base Color'])
