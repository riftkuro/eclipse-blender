import math
import bpy
from . import authored


def mix(a, b, t):
    return [x + (y - x) * t for x, y in zip(a, b)]


def split(points, t):
    a, b, c = [mix(points[i], points[i + 1], t) for i in range(3)]
    d, e = mix(a, b, t), mix(b, c, t)
    f = mix(d, e, t)
    return [points[0], a, d, f], [f, e, c, points[3]]


def parameter(points, x):
    lo, hi = 0.0, 1.0
    for _ in range(40):
        t = (lo + hi) / 2
        value = split(points, t)[0][-1][0]
        if value < x:
            lo = t
        else:
            hi = t
    return (lo + hi) / 2


def segment(curve, start, finish, ratio):
    keys = list(curve.keyframe_points)
    a = next((k for k in reversed(keys) if k.co.x <= start + 1e-6), None)
    b = next((k for k in keys if k.co.x > start + 1e-6), None)
    if a is None or b is None:
        if curve.extrapolation != 'CONSTANT':
            return None
        value = float(curve.evaluate(start))
        return dict(style='CONSTANT', left=[start * ratio, value], right=[finish * ratio, value], start=value, finish=value, leftType='VECTOR', rightType='VECTOR')
    if finish > b.co.x + 1e-5:
        return None
    style = a.interpolation
    result = dict(style=style, easing=a.easing, back=a.back, amplitude=a.amplitude, period=a.period,
                  start=float(curve.evaluate(start)), finish=float(curve.evaluate(finish)),
                  leftType=a.handle_right_type, rightType=b.handle_left_type)
    if style == 'BEZIER':
        points = [list(a.co), list(a.handle_right), list(b.handle_left), list(b.co)]
        length = b.co.x - a.co.x
        lengths = [abs(points[1][0] - points[0][0]), abs(points[3][0] - points[2][0])]
        # Blender limits each handle to its adjacent key's time, independently.
        # Crossing handle projections are valid and must not be shortened together.
        if lengths[0] > length:
            points[1] = mix(points[0], points[1], length / lengths[0])
        if lengths[1] > length:
            points[2] = mix(points[3], points[2], length / lengths[1])
        if finish < b.co.x - 1e-6:
            points = split(points, parameter(points, finish))[0]
            result['rightType'] = 'FREE'
        if start > a.co.x + 1e-6:
            points = split(points, parameter(points, start))[1]
            result['leftType'] = 'FREE'
        result['left'], result['right'] = [[p[0] * ratio, p[1]] for p in points[1:3]]
    return result


def collect(pairs, positions, samples, ratio):
    result = {}
    for pair in pairs:
        obj = bpy.data.objects[pair['object']]
        objects = {o.name: o for o in authored.dependencies(obj)}
        for mapping in pair['tracks']:
            if mapping['source'].startswith('@'):
                objects.update({o.name: o for o in authored.dependencies(bpy.data.objects[mapping['source'][1:]])})
        records = []
        seen = set()
        for item in objects.values():
            for owner in (item, item.data):
                animation = getattr(owner, 'animation_data', None)
                if not animation:
                    continue
                nla = animation.use_nla and any(not t.mute and any(not s.mute for s in t.strips) for t in animation.nla_tracks)
                actions = [(animation.action, not nla, None, getattr(animation, 'action_slot', None))] if animation.action else []
                for track in animation.nla_tracks if animation.use_nla else []:
                    for strip in track.strips if not track.mute else []:
                        if strip.action and not strip.mute:
                            actions.append((strip.action, False, dict(start=strip.frame_start, end=strip.frame_end, actionStart=strip.action_frame_start, actionEnd=strip.action_frame_end, scale=strip.scale, repeat=strip.repeat, reverse=strip.use_reverse), getattr(strip, 'action_slot', None)))
                for action, exact, timing, slot in actions:
                    for curve in authored.action_curves(action, slot):
                        identity = (owner.name, action.name, curve.data_path, curve.array_index, str(timing))
                        if identity in seen or curve.mute or not curve.keyframe_points:
                            continue
                        seen.add(identity)
                        keys = [dict(frame=float(k.co.x) * ratio, value=float(k.co.y), left=[float(k.handle_left.x) * ratio, float(k.handle_left.y)], right=[float(k.handle_right.x) * ratio, float(k.handle_right.y)], leftType=k.handle_left_type, rightType=k.handle_right_type, interpolation=k.interpolation, easing=k.easing, back=k.back, amplitude=k.amplitude, period=k.period) for k in curve.keyframe_points]
                        record = dict(owner=owner.name, path=curve.data_path, index=curve.array_index, keys=keys, timing=timing)
                        if exact and not any(not m.mute for m in curve.modifiers):
                            record['values'] = [float(curve.evaluate(f / ratio)) for f in samples]
                            record['segments'] = [segment(curve, a / ratio, b / ratio, ratio) for a, b in zip(positions, positions[1:])]
                        records.append(record)
        result[pair['rig']] = records
    return result


def sample_positions(positions):
    if not positions:
        return [0], set()
    samples = set(positions) | {f / 4 for f in range(math.ceil(positions[0] * 4), math.floor(positions[-1] * 4) + 1)}
    jumps = set()
    for previous, frame in zip(positions, positions[1:]):
        epsilon = max(.0001, 2 ** (math.floor(math.log2(max(1, frame))) - 22))
        samples.add(round(frame - min(epsilon, (frame - previous) / 4), 6))
        jumps.add(frame)
    return sorted(samples), jumps


def needs_split(left, middle, right):
    from .transforms import unpack
    def differs(a, m, b):
        a, m, b = unpack(a), unpack(m), unpack(b)
        translation = a.translation.lerp(b.translation, .5)
        rotation = a.to_quaternion().slerp(b.to_quaternion(), .5)
        target = m.to_quaternion()
        distance = min(sum((x - y) ** 2 for x, y in zip(rotation, target)), sum((x + y) ** 2 for x, y in zip(rotation, target)))
        return (translation - m.translation).length > .0004 or distance > (.00015 / 2) ** 2
    for a, m, b in zip(left['rigs'], middle['rigs'], right['rigs']):
        if 'camera' in a:
            if differs(a['camera'], m['camera'], b['camera']) or abs((a['fov'] + b['fov']) / 2 - m['fov']) > .0004:
                return True
        else:
            for x, y, z in zip(a['tracks'], m['tracks'], b['tracks']):
                if differs(x['world'], y['world'], z['world']):
                    return True
                if any(abs((u + w) / 2 - v) > .0001 for u, v, w in zip(x['scale'], y['scale'], z['scale'])):
                    return True
    return False
