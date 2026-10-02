import math
from mathutils import Matrix, Vector

AXES = Matrix(((1, 0, 0, 0), (0, 0, 1, 0), (0, -1, 0, 0), (0, 0, 0, 1)))


def unpack(values):
    if not isinstance(values, list) or len(values) != 12 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
        raise ValueError('invalid camera or bone transform')
    return Matrix(((values[3], values[4], values[5], values[0]), (values[6], values[7], values[8], values[1]), (values[9], values[10], values[11], values[2]), (0, 0, 0, 1)))


def pack(matrix):
    loc, rot, scale = matrix.decompose()
    rigid = rot.to_matrix()
    return [round(float(v), 6) for v in loc] + [round(float(rigid[r][c]), 6) for r in range(3) for c in range(3)]


def converted(matrix, units=1.0):
    result = AXES @ matrix
    result.translation *= units
    return result


def restored(matrix, units=1.0):
    result = matrix.copy()
    result.translation /= units
    return AXES.inverted() @ result


def camera_fov(obj, scene):
    data = obj.data
    if data.type != 'PERSP':
        raise ValueError('Eclipse cameras require perspective projection')
    if abs(data.shift_x) > 1e-7 or abs(data.shift_y) > 1e-7:
        raise ValueError('camera lens shift cannot be represented by a Studio camera')
    corners = data.view_frame(scene=scene)
    ratios = [point.y / -point.z for point in corners]
    fov = math.degrees(math.atan(max(ratios)) - math.atan(min(ratios)))
    if not 1 <= fov <= 120:
        raise ValueError('camera vertical FOV must be between 1 and 120 degrees for Studio')
    return fov


def set_camera_fov(obj, value):
    if not isinstance(value, (int, float)) or not math.isfinite(value) or not 1 <= value <= 120:
        raise ValueError('invalid camera FOV')
    obj.data.sensor_fit = 'VERTICAL'
    obj.data.lens = obj.data.sensor_height / (2 * math.tan(math.radians(value) / 2))


def normalized(name):
    return ''.join(c for c in name.casefold() if c.isalnum())


def correction(bone):
    value = bone.get('nicetransform')
    return Matrix(value).inverted() if value is not None else Matrix.Identity(4)


def source_frame(obj, source, depsgraph=None, rest=False):
    import bpy
    if source.startswith('@'):
        item = bpy.data.objects.get(source[1:])
        if item is None:
            raise ValueError('mapped object is missing: ' + source[1:])
        return (item.evaluated_get(depsgraph) if depsgraph else item).matrix_world.copy()
    bone = obj.data.bones.get(source)
    if bone is None:
        raise ValueError('mapped bone is missing: ' + source)
    evaluated = obj.evaluated_get(depsgraph) if depsgraph and not rest else obj
    sample = bone.matrix_local if rest else evaluated.pose.bones[source].matrix
    return evaluated.matrix_world @ sample @ correction(bone)


def bind(obj, rig, supplied=None, units=1.0):
    supplied = supplied or {}
    if rig['kind'] == 'camera':
        from .camera_rig import camera_output
        obj = camera_output(obj)
        return {'rig': rig['id'], 'object': obj.name, 'kind': 'camera', 'units': units, 'tracks': []}
    if obj.type != 'ARMATURE':
        raise ValueError('pair rig tracks with the output armature')
    names = {}
    identifiers = {bone.get('eclipse_track_id'): bone.name for bone in obj.data.bones if bone.get('eclipse_track_id')}
    for bone in obj.data.bones:
        names.setdefault(normalized(bone.name), []).append(bone.name)
    mappings, missing = [], []
    for track in rig['tracks']:
        source = supplied.get(track['id']) or identifiers.get(track['id'])
        if not source:
            candidates = names.get(normalized(track['name']), [])
            source = candidates[0] if len(candidates) == 1 else None
        if not source:
            missing.append(track['name'])
            continue
        rest = converted(source_frame(obj, source, rest=True), units)
        mappings.append({'id': track['id'], 'source': source, 'parent': track.get('parent'), 'rest': pack(rest), 'target': track['rest'], 'rest_scale': list(rest.to_scale())})
    if missing:
        raise ValueError('map these output bones first: ' + ', '.join(missing))
    if not mappings:
        raise ValueError('the rig has no mapped animation tracks')
    alignment, fit = align(obj, rig, mappings)
    for mapping in mappings:
        offset = unpack(mapping['rest']).inverted() @ alignment.inverted() @ unpack(mapping['target'])
        mapping['offset'] = pack(offset)
    return {'rig': rig['id'], 'object': obj.name, 'kind': 'rig', 'units': units, 'alignment': pack(alignment), 'tracks': mappings, 'fit': fit}


def rotation_between(a, b):
    if a.length < 1e-9 or b.length < 1e-9:
        return Matrix.Identity(3)
    return a.normalized().rotation_difference(b.normalized()).to_matrix()


def rigid_fit(sources, targets, prior):
    import numpy
    count = len(sources)
    centre_s = sum(sources, Vector()) / count
    centre_t = sum(targets, Vector()) / count
    spread_s = sum((p - centre_s).length_squared for p in sources)
    spread_t = sum((p - centre_t).length_squared for p in targets)
    scale = math.sqrt(spread_t / spread_s) if spread_s > 1e-12 and spread_t > 1e-12 else 1.0
    rotation = None
    if count >= 3:
        cross = numpy.zeros((3, 3))
        for s, t in zip(sources, targets):
            cross += numpy.outer(numpy.array(s - centre_s), numpy.array(t - centre_t))
        u, singular, vt = numpy.linalg.svd(cross)
        if singular[1] > 1e-6 * max(singular[0], 1e-9):
            flip = numpy.diag([1, 1, numpy.sign(numpy.linalg.det(vt.T @ u.T)) or 1])
            rotation = Matrix((vt.T @ flip @ u.T).tolist())
    if rotation is None:
        rotation = prior.copy()
        if count >= 2:
            axis_s = max((p - centre_s for p in sources), key=lambda v: v.length)
            axis_t = max((p - centre_t for p in targets), key=lambda v: v.length)
            rotation = rotation_between(rotation @ axis_s, axis_t) @ rotation
    residual = math.sqrt(sum((rotation @ (s - centre_s) - (t - centre_t)).length_squared for s, t in zip(sources, targets)) / count)
    result = rotation.to_4x4()
    result.translation = centre_t - rotation @ centre_s
    return result, residual, scale


# bones follow their part rigidly, so the alignment has to be the true frame between both rigs:
# any error in it moves every pivot. fit mapped heads onto the studio joints (or part centres) instead of trusting one root bone
def align(obj, rig, mappings):
    tracks = {t['id']: t for t in rig['tracks']}
    depth = {}
    for mapping in mappings:
        count, current = 0, tracks[mapping['id']]
        while current and current.get('parent') in tracks:
            count += 1
            current = tracks[current['parent']]
        depth[mapping['id']] = count
    top = min(mappings, key=lambda m: depth[m['id']])
    prior = (unpack(top['target']) @ unpack(top['rest']).inverted()).to_3x3()
    heads = [unpack(m['rest']).translation for m in mappings]
    centres = [unpack(m['target']).translation for m in mappings]
    joints = [unpack(tracks[m['id']]['joint']).translation if tracks[m['id']].get('joint') else unpack(m['target']).translation for m in mappings]
    if len(mappings) >= 3:
        candidates = [rigid_fit(heads, points, prior) for points in (joints, centres)]
        alignment, residual, scale = min(candidates, key=lambda c: c[1])
    else:
        pivot = 'centre' if obj.get('eclipse_rig_id') and not any(obj.data.bones[m['source']].get('eclipse_pivot') == 'joint' for m in mappings if not m['source'].startswith('@') and m['source'] in obj.data.bones) else 'joint'
        alignment, residual, scale = rigid_fit(heads, joints if pivot == 'joint' else centres, prior)
    return alignment, {'residual': residual, 'scale': scale}


def sample(pair, scene, depsgraph):
    import bpy
    obj = bpy.data.objects.get(pair['object'])
    if obj is None:
        raise ValueError('paired object is missing: ' + pair['object'])
    if pair['kind'] == 'camera':
        evaluated = obj.evaluated_get(depsgraph)
        return {'id': pair['rig'], 'camera': pack(converted(evaluated.matrix_world, pair['units'])), 'fov': camera_fov(evaluated, scene)}
    alignment = unpack(pair['alignment'])
    tracks = []
    for mapping in pair['tracks']:
        source = converted(source_frame(obj, mapping['source'], depsgraph), pair['units'])
        world = alignment @ unpack(pack(source)) @ unpack(mapping['offset'])
        scale = source.to_scale()
        rest_scale = mapping.get('rest_scale', [1, 1, 1])
        scale = [scale[i] / max(abs(rest_scale[i]), 1e-8) for i in range(3)]
        tracks.append({'id': mapping['id'], 'world': pack(world), 'scale': scale})
    return {'id': pair['rig'], 'tracks': tracks}
