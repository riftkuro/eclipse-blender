import bpy
from .transforms import normalized


def dependencies(obj):
    found = {}
    pending = [obj]
    while pending:
        current = pending.pop()
        if current is None or current.name in found:
            continue
        found[current.name] = current
        pending.append(current.parent)
        owners = [current, current.data] if current.data else [current]
        if current.type == 'ARMATURE':
            owners.extend(current.pose.bones)
        for owner in owners:
            for constraint in getattr(owner, 'constraints', []):
                pending.append(getattr(constraint, 'target', None))
                pending.append(getattr(constraint, 'pole_target', None))
            animation = getattr(owner, 'animation_data', None)
            for curve in animation.drivers if animation else []:
                for variable in curve.driver.variables:
                    for target in variable.targets:
                        if isinstance(target.id, bpy.types.Object):
                            pending.append(target.id)
    return list(found.values())


def action_curves(action, slot=None):
    if getattr(action, 'is_action_layered', False):
        return [f for layer in action.layers for strip in layer.strips for bag in getattr(strip, 'channelbags', []) if slot is None or bag.slot_handle == slot.handle for f in bag.fcurves]
    return list(getattr(action, 'fcurves', []))


def frames(pair, accept=None):
    obj = bpy.data.objects[pair['object']]
    objects = dependencies(obj)
    for mapping in pair['tracks']:
        if mapping['source'].startswith('@'):
            objects.extend(dependencies(bpy.data.objects[mapping['source'][1:]]))
    points = set()
    for obj in objects:
        for owner in (obj, obj.data):
            animation = getattr(owner, 'animation_data', None)
            if not animation:
                continue
            if animation.action and not animation.use_nla or animation.action and not any(not t.mute and t.strips for t in animation.nla_tracks):
                for curve in action_curves(animation.action, getattr(animation, 'action_slot', None)):
                    if not curve.mute and (accept is None or accept(obj, owner, curve)):
                        points.update(float(k.co.x) for k in curve.keyframe_points)
            else:
                if animation.action and animation.action_influence:
                    for curve in action_curves(animation.action, getattr(animation, 'action_slot', None)):
                        if not curve.mute and (accept is None or accept(obj, owner, curve)):
                            points.update(float(k.co.x) for k in curve.keyframe_points)
                for track in animation.nla_tracks:
                    if track.mute:
                        continue
                    for strip in track.strips:
                        if strip.mute or not strip.action:
                            continue
                        for curve in action_curves(strip.action, getattr(strip, 'action_slot', None)):
                            if curve.mute or accept is not None and not accept(obj, owner, curve):
                                continue
                            for key in curve.keyframe_points:
                                at = float(key.co.x)
                                if not strip.action_frame_start <= at <= strip.action_frame_end:
                                    continue
                                duration = strip.action_frame_end - strip.action_frame_start
                                for repeat in range(max(1, int(__import__('math').ceil(strip.repeat)))):
                                    local = strip.action_frame_end - at if strip.use_reverse else at - strip.action_frame_start
                                    frame = strip.frame_start + (local + repeat * duration) * strip.scale
                                    if frame <= strip.frame_end + 1e-5:
                                        points.add(frame)
    return sorted(p for p in points if p >= 0)


def nodes(obj, bone=None, found=None):
    if obj.type != 'ARMATURE':
        bone = None
    found = {} if found is None else found
    key = (obj.name, bone)
    if key in found:
        return found
    found[key] = obj
    owner = obj.pose.bones.get(bone) if bone and obj.type == 'ARMATURE' else obj
    if owner is None:
        return found
    if bone:
        if owner.parent:
            nodes(obj, owner.parent.name, found)
        nodes(obj, None, found)
        for other in obj.pose.bones:
            for constraint in other.constraints:
                if constraint.type == 'IK' and not constraint.mute:
                    chain = [other] + list(other.parent_recursive)
                    if owner in chain[:constraint.chain_count or len(chain)]:
                        nodes(obj, other.name, found)
    elif obj.parent:
        nodes(obj.parent, obj.parent_bone if obj.parent_type == 'BONE' else None, found)
    for constraint in owner.constraints:
        if constraint.mute:
            continue
        for attr, subtarget in [('target', 'subtarget'), ('pole_target', 'pole_subtarget')]:
            target = getattr(constraint, attr, None)
            if target:
                nodes(target, getattr(constraint, subtarget, '') or None, found)
    for data in (obj, obj.data):
        animation = getattr(data, 'animation_data', None)
        for curve in animation.drivers if animation else []:
            if not bone and curve.data_path.startswith('pose.bones['):
                continue
            if bone and not curve.data_path.startswith(owner.path_from_id()):
                continue
            for variable in curve.driver.variables:
                for target in variable.targets:
                    if isinstance(target.id, bpy.types.Object):
                        path = target.data_path
                        target_bone = target.bone_target or None
                        if not target_bone and path.startswith('pose.bones['):
                            for candidate in target.id.pose.bones:
                                if path.startswith(candidate.path_from_id()):
                                    target_bone = candidate.name
                                    break
                        nodes(target.id, target_bone, found)
    return found


def track_frames(pair):
    obj = bpy.data.objects[pair['object']]
    result = {}
    mappings = pair['tracks'] if pair['kind'] != 'camera' else [{'id': 'camera', 'source': None}, {'id': 'fov', 'source': None}]
    by_id = {m['id']: m for m in mappings}
    for mapping in mappings:
        source = mapping['source']
        target = bpy.data.objects[source[1:]] if source and source.startswith('@') else obj
        bone = source if source and not source.startswith('@') else None
        related = nodes(target, bone)
        parent = by_id.get(mapping.get('parent'))
        if parent and bone and obj.pose.bones[bone].parent and parent['source'] == obj.pose.bones[bone].parent.name:
            inherited = nodes(obj, parent['source'])
            related = {key: value for key, value in related.items() if key not in inherited or key == (obj.name, bone)}
        def accept(item, owner, curve):
            path = curve.data_path
            if pair['kind'] == 'camera' and item == obj:
                if mapping['id'] == 'camera' and owner == obj.data:
                    return False
                if mapping['id'] == 'fov' and owner == obj and not path.startswith('data.'):
                    return False
            if path.startswith('pose.bones['):
                return any(name == item.name and member and item.pose.bones.get(member) and path.startswith(item.pose.bones[member].path_from_id()) for name, member in related)
            return (item.name, None) in related
        result[mapping['id']] = frames(pair, accept)
    return result


def automatic(rig, objects, occupied=()):
    candidates = []
    for obj in objects:
        if obj.name in occupied:
            continue
        if obj.get('eclipse_rig_id') == rig['id']:
            candidates.append((10000, obj))
            continue
        if rig['kind'] == 'camera':
            if obj.type == 'CAMERA':
                score = 1000 if obj.get('eclipse_camera_reference') == 'Camera_Rig.blend' else 100 + 10 * (normalized(obj.name) == normalized(rig['name']))
                candidates.append((score, obj))
            continue
        if obj.type != 'ARMATURE':
            continue
        names = {}
        for bone in obj.data.bones:
            names.setdefault(normalized(bone.name), []).append(bone)
        if all(len(names.get(normalized(t['name']), [])) == 1 for t in rig['tracks']):
            score = 100 + sum('transform' in b for b in obj.data.bones) + 10 * (normalized(obj.name) == normalized(rig['name']))
            candidates.append((score, obj))
    candidates.sort(key=lambda row: row[0], reverse=True)
    if not candidates:
        raise ValueError('No matching output rig found. Choose a Blender object and check its bone names.')
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        raise ValueError('More than one rig matches. Choose the Blender object, then click Pair.')
    return candidates[0][1]
