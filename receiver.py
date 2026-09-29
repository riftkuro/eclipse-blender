import bpy
import math
import time
from mathutils import Matrix
from .transforms import unpack, restored, correction, set_camera_fov


class Receiver:
    def __init__(self):
        self.targets = {}
        self.constraints = []
        self.cameras = {}
        self.controls = {}
        self.hidden = {}
        self.drivers = []
        self.muted = []
        self.actions = {}
        self.poses = {}
        self.playback = None

    def follow(self, pairs, packet):
        at = packet.get('time')
        if not isinstance(at, (int, float)) or not math.isfinite(at) or at < 0:
            raise ValueError('invalid Eclipse playhead time')
        scene = bpy.context.scene
        frame = at * scene.render.fps / scene.render.fps_base
        scene.frame_set(math.floor(frame), subframe=frame % 1)
        self.apply(pairs, packet)
        self.playback = None

    def advance(self):
        return

    def action(self, owner):
        if owner in self.actions:
            return
        animation = owner.animation_data_create()
        action = bpy.data.actions.new('Eclipse Live • ' + owner.name)
        action.use_fake_user = True
        self.actions[owner] = (animation.action, animation.use_nla, action)
        animation.action, animation.use_nla = action, False

    def remember(self, subject):
        if subject not in self.poses:
            self.poses[subject] = (subject.matrix_basis.copy(), subject.rotation_mode)

    def record(self, pairs, take):
        frames, fps = take.get('frames', []), take.get('fps', 0)
        if not frames or len(frames) > 20001 or not 1 <= fps <= 1000:
            raise ValueError('invalid Eclipse animation')
        last = -1
        for sample in frames:
            frame = sample.get('frame', -1)
            if not isinstance(frame, (int, float)) or not math.isfinite(frame) or not last < frame <= 20000:
                raise ValueError('invalid Eclipse keyframe order')
            last = frame
            for row in sample.get('rigs', []):
                if 'camera' in row:
                    unpack(row['camera'])
                    if not 1 <= row.get('fov', 0) <= 120:
                        raise ValueError('invalid Eclipse camera FOV')
                for track in row.get('tracks', []):
                    unpack(track['world'])
        scene = bpy.context.scene
        before = scene.frame_current + scene.frame_subframe
        from .authored import action_curves
        for _, _, action in self.actions.values():
            for curve in action_curves(action):
                curve.keyframe_points.clear()
        for target in self.targets.values():
            if target.animation_data and target.animation_data.action:
                for curve in action_curves(target.animation_data.action):
                    curve.keyframe_points.clear()
        try:
            for sample in frames:
                at = sample['frame'] / fps * scene.render.fps / scene.render.fps_base
                scene.frame_set(math.floor(at), subframe=at % 1)
                self.apply(pairs, sample)
                for target in self.targets.values():
                    target.rotation_mode = 'QUATERNION'
                    for path in ('location', 'rotation_quaternion', 'scale'):
                        target.keyframe_insert(path, frame=at)
                for pair in pairs:
                    obj = bpy.data.objects.get(pair['object'])
                    if not obj:
                        continue
                    keys = None if take.get('baked') else take.get('trackKeys', {}).get(pair['rig'])
                    authored = lambda name: keys is None or sample['frame'] in keys.get(name, [])
                    if pair['kind'] == 'camera':
                        if authored('camera'):
                            self.action(obj)
                            self.remember(obj)
                            obj.rotation_mode = 'QUATERNION'
                            obj.matrix_world = self.target(obj).matrix_world
                            for path in ('location', 'rotation_quaternion'):
                                obj.keyframe_insert(path, frame=at)
                        if authored('fov'):
                            self.action(obj.data)
                            obj.data.keyframe_insert('lens', frame=at)
                        continue
                    ordered = sorted(pair['tracks'], key=lambda m: 0 if m['source'].startswith('@') else len(obj.pose.bones[m['source']].parent_recursive))
                    for mapping in ordered:
                        if not authored(mapping['id']):
                            continue
                        source = mapping['source']
                        owner = bpy.data.objects.get(source[1:]) if source.startswith('@') else obj
                        if not owner:
                            continue
                        self.action(owner)
                        bone = None if source.startswith('@') else source
                        subject = owner.pose.bones[bone] if bone else owner
                        self.remember(subject)
                        subject.rotation_mode = 'QUATERNION'
                        if bone:
                            subject.matrix = owner.matrix_world.inverted() @ self.target(owner, bone).matrix_world
                            bpy.context.view_layer.update()
                        else:
                            subject.matrix_world = self.target(owner).matrix_world
                        for path in ('location', 'rotation_quaternion', 'scale'):
                            subject.keyframe_insert(path, frame=at)
            for owner in list(self.targets.values()) + list(self.actions):
                animation = owner.animation_data
                for curve in action_curves(animation.action) if animation and animation.action else []:
                    for key in curve.keyframe_points:
                        key.interpolation = 'LINEAR'
            scene.frame_end = max(scene.frame_end, math.ceil(last / fps * scene.render.fps / scene.render.fps_base))
        finally:
            scene.frame_set(math.floor(before), subframe=before % 1)

    def prepare_controls(self, pair, obj):
        if obj.name in self.controls:
            return
        from .authored import dependencies
        for controller in dependencies(obj):
            if controller != obj and controller.type == 'ARMATURE':
                self.hidden[controller] = controller.hide_get()
                controller.hide_set(True)
        self.controls[obj.name] = True

    def target(self, obj, bone=None):
        key = (obj.name, bone)
        if key in self.targets:
            return self.targets[key]
        empty = bpy.data.objects.new('Eclipse Live Target', None)
        bpy.context.scene.collection.objects.link(empty)
        empty.hide_render = True
        empty.hide_set(True)
        owner = obj.pose.bones[bone] if bone else obj
        self.remember(owner)
        for existing in owner.constraints:
            self.muted.append((existing, existing.mute))
            existing.mute = True
        animation = obj.animation_data
        for driver in animation.drivers if animation else []:
            path = owner.path_from_id() + '.' if bone else ''
            if (bone and driver.data_path.startswith(path)) or (not bone and not driver.data_path.startswith('pose.bones[')):
                if all(saved != driver for saved, _ in self.drivers):
                    self.drivers.append((driver, driver.mute))
                    driver.mute = True
        constraint = owner.constraints.new('COPY_TRANSFORMS')
        constraint.name = 'Eclipse Live Preview'
        constraint.target = empty
        constraint.owner_space = constraint.target_space = 'WORLD'
        self.constraints.append((owner, constraint))
        self.targets[key] = empty
        return empty

    def camera(self, obj, fov):
        if obj.name not in self.cameras:
            drivers = [(f, f.mute) for f in (obj.data.animation_data.drivers if obj.data.animation_data else []) if f.data_path in {'lens', 'sensor_fit', 'sensor_height'}]
            self.cameras[obj.name] = (obj, obj.data.lens, obj.data.sensor_fit, drivers)
            for driver, _ in drivers:
                driver.mute = True
        set_camera_fov(obj, fov)

    def apply(self, pairs, packet):
        values = {v['id']: v for v in packet.get('rigs', [])}
        for pair in pairs:
            row = values.get(pair['rig'])
            obj = bpy.data.objects.get(pair['object'])
            if not row or not obj:
                continue
            if pair['kind'] == 'camera':
                self.target(obj).matrix_world = restored(unpack(row['camera']), pair['units'])
                self.camera(obj, row['fov'])
                continue
            tracks = {v['id']: v for v in row['tracks']}
            self.prepare_controls(pair, obj)
            destinations = {}
            alignment = unpack(pair['alignment']).inverted()
            for mapping in pair['tracks']:
                value = tracks.get(mapping['id'])
                if not value:
                    continue
                world = restored(alignment @ unpack(value['world']) @ unpack(mapping['offset']).inverted(), pair['units'])
                scale = value.get('scale', [1, 1, 1])
                base = mapping.get('rest_scale', [1, 1, 1])
                world = world @ Matrix.Diagonal(tuple(scale[i] * base[i] for i in range(3)) + (1,))
                source = mapping['source']
                if source.startswith('@'):
                    target_obj = bpy.data.objects.get(source[1:])
                    if target_obj:
                        self.target(target_obj).matrix_world = world
                else:
                    destinations[source] = world @ correction(obj.data.bones[source]).inverted()
                    self.target(obj, source).matrix_world = destinations[source]
        bpy.context.view_layer.update()

    def clear(self):
        self.playback = None
        for owner, constraint in self.constraints:
            try:
                owner.constraints.remove(constraint)
            except ReferenceError:
                pass
        for constraint, muted in self.muted:
            try:
                constraint.mute = muted
            except ReferenceError:
                pass
        for obj, lens, fit, drivers in self.cameras.values():
            try:
                obj.data.lens, obj.data.sensor_fit = lens, fit
                for driver, muted in drivers:
                    driver.mute = muted
            except ReferenceError:
                pass
        for empty in self.targets.values():
            bpy.data.objects.remove(empty, do_unlink=True)
        self.targets.clear()
        self.constraints.clear()
        self.cameras.clear()
        self.controls.clear()
        for obj, hidden in self.hidden.items():
            try:
                obj.hide_set(hidden)
            except ReferenceError:
                pass
        self.hidden.clear()
        for driver, muted in self.drivers:
            try:
                driver.mute = muted
            except ReferenceError:
                pass
        self.drivers.clear()
        self.muted.clear()
        for owner, (original, nla, _) in self.actions.items():
            try:
                owner.animation_data.action, owner.animation_data.use_nla = original, nla
            except ReferenceError:
                pass
        self.actions.clear()
        for subject, (basis, mode) in self.poses.items():
            try:
                subject.rotation_mode, subject.matrix_basis = mode, basis
            except ReferenceError:
                pass
        self.poses.clear()


class TakeWriter:
    def __init__(self, pairs, take):
        frames, fps = take.get('frames', []), take.get('fps', 0)
        if not frames or len(frames) > 20001 or not isinstance(fps, (int, float)) or not 1 <= fps <= 1000:
            raise ValueError('invalid animation length')
        scene = bpy.context.scene
        self.ratio = scene.render.fps / scene.render.fps_base / fps
        self.frames = frames
        self.at = 0
        self.channels = {}
        self.plans = []
        self.last = 0.0
        for pair in pairs:
            obj = bpy.data.objects.get(pair['object'])
            if obj is None:
                raise ValueError('paired object was removed: ' + pair['object'])
            plan = {'pair': pair, 'object': obj}
            if pair['kind'] == 'rig':
                plan['alignment'] = unpack(pair['alignment']).inverted()
                plan['inverse'] = obj.matrix_world.inverted()
                plan['bones'] = sorted(obj.pose.bones, key=lambda b: len(b.parent_recursive))
                plan['by_source'] = {m['source']: m for m in pair['tracks']}
                plan['offsets'] = {m['id']: unpack(m['offset']).inverted() for m in pair['tracks']}
                plan['corrections'] = {b.name: correction(b.bone).inverted() for b in obj.pose.bones}
                plan['props'] = []
                for mapping in pair['tracks']:
                    if mapping['source'].startswith('@'):
                        prop = bpy.data.objects.get(mapping['source'][1:])
                        if prop is None:
                            raise ValueError('mapped object was removed: ' + mapping['source'][1:])
                        plan['props'].append((mapping, prop))
            self.plans.append(plan)

    def names(self):
        return [plan['object'].name for plan in self.plans]

    def channel(self, owner, path, index, group, frame, value):
        key = (owner.as_pointer(), path, index)
        entry = self.channels.get(key)
        if entry is None:
            entry = self.channels[key] = {'owner': owner, 'path': path, 'index': index, 'group': group, 'keys': []}
        entry['keys'].append((frame, value))

    def rotation(self, owner, group, frame, quat, previous):
        mode = owner.rotation_mode
        if mode in {'QUATERNION', 'AXIS_ANGLE'}:
            if previous is not None and quat.dot(previous) < 0:
                quat.negate()
            if mode == 'AXIS_ANGLE':
                axis, angle = quat.to_axis_angle()
                for i, v in enumerate((angle, axis.x, axis.y, axis.z)):
                    self.channel(owner, 'rotation_axis_angle', i, group, frame, v)
            else:
                for i, v in enumerate(quat):
                    self.channel(owner, 'rotation_quaternion', i, group, frame, v)
            return quat
        euler = quat.to_euler(mode, previous) if previous is not None else quat.to_euler(mode)
        for i, v in enumerate(euler):
            self.channel(owner, 'rotation_euler', i, group, frame, v)
        return euler

    def transform(self, owner, group, frame, basis, state):
        loc, rot, scale = basis.decompose()
        for i, v in enumerate(loc):
            self.channel(owner, 'location', i, group, frame, v)
        for i, v in enumerate(scale):
            self.channel(owner, 'scale', i, group, frame, v)
        state[owner.as_pointer()] = self.rotation(owner, group, frame, rot, state.get(owner.as_pointer()))

    def object_basis(self, obj, world):
        if obj.parent is None:
            return world
        return (obj.parent.matrix_world @ obj.matrix_parent_inverse).inverted() @ world

    def step(self, budget):
        started = time.monotonic()
        state = getattr(self, 'state', None) or {}
        self.state = state
        while self.at < len(self.frames):
            sample = self.frames[self.at]
            self.at += 1
            number = sample.get('frame')
            if not isinstance(number, (int, float)) or not math.isfinite(number):
                raise ValueError('invalid Eclipse keyframe order')
            frame = number * self.ratio
            self.last = max(self.last, frame)
            values = {v['id']: v for v in sample.get('rigs', [])}
            for plan in self.plans:
                pair, obj = plan['pair'], plan['object']
                row = values.get(pair['rig'])
                if not row:
                    continue
                if pair['kind'] == 'camera':
                    world = restored(unpack(row['camera']), pair['units'])
                    self.transform(obj, 'Object Transforms', frame, self.object_basis(obj, world), state)
                    fov = row.get('fov')
                    if isinstance(fov, (int, float)) and 1 <= fov <= 120:
                        self.channel(obj.data, 'lens', 0, 'Camera', frame, obj.data.sensor_height / (2 * math.tan(math.radians(fov) / 2)))
                    continue
                tracks = {v['id']: v for v in row.get('tracks', [])}
                def world_of(mapping):
                    value = tracks[mapping['id']]
                    world = restored(plan['alignment'] @ unpack(value['world']) @ plan['offsets'][mapping['id']], pair['units'])
                    scale, base = value.get('scale', [1, 1, 1]), mapping.get('rest_scale', [1, 1, 1])
                    return world @ Matrix.Diagonal(tuple(scale[i] * base[i] for i in range(3)) + (1,))
                for mapping, prop in plan['props']:
                    if mapping['id'] in tracks:
                        self.transform(prop, 'Object Transforms', frame, self.object_basis(prop, world_of(mapping)), state)
                poses = {}
                for bone in plan['bones']:
                    rest = bone.bone
                    parent = bone.parent
                    extra = {} if parent is None else {'parent_matrix': poses[parent.name], 'parent_matrix_local': parent.bone.matrix_local}
                    mapping = plan['by_source'].get(bone.name)
                    if mapping and mapping['id'] in tracks:
                        pose = plan['inverse'] @ world_of(mapping) @ plan['corrections'][bone.name]
                        basis = rest.convert_local_to_pose(pose, rest.matrix_local, invert=True, **extra)
                        poses[bone.name] = pose
                        self.transform(bone, bone.name, frame, basis, state)
                    else:
                        poses[bone.name] = rest.convert_local_to_pose(bone.matrix_basis, rest.matrix_local, **extra)
            if time.monotonic() - started > budget:
                return False
        return True

    def finish(self):
        owners = {}
        for entry in self.channels.values():
            owner = entry['owner']
            holder = owner.id_data
            owners.setdefault(holder.as_pointer(), (holder, []))[1].append(entry)
        muted = {'constraints': 0, 'drivers': 0}
        for holder, entries in owners.values():
            animation = holder.animation_data_create()
            previous = animation.action
            if previous is not None and not previous.get('eclipse_take'):
                previous.use_fake_user = True
            action = bpy.data.actions.new(holder.name + ' • Eclipse Take')
            action['eclipse_take'] = True
            animation.action = action
            layered = hasattr(action, 'fcurve_ensure_for_datablock')
            for entry in entries:
                owner = entry['owner']
                path = owner.path_from_id(entry['path']) if owner != holder else entry['path']
                if layered:
                    try:
                        curve = action.fcurve_ensure_for_datablock(holder, path, index=entry['index'], group_name=entry['group'])
                    except TypeError:
                        curve = action.fcurve_ensure_for_datablock(holder, path, index=entry['index'])
                else:
                    curve = action.fcurves.new(path, index=entry['index'], action_group=entry['group'])
                keys = entry['keys']
                points = curve.keyframe_points
                points.add(len(keys))
                points.foreach_set('co', [v for key in keys for v in key])
                points.foreach_set('interpolation', [bpy.types.Keyframe.bl_rna.properties['interpolation'].enum_items['LINEAR'].value] * len(keys))
                curve.update()
            if isinstance(holder, bpy.types.Camera):
                holder.sensor_fit = 'VERTICAL'
            if isinstance(holder, bpy.types.Object) and holder.type == 'ARMATURE':
                keyed = {entry['owner'].name for entry in entries if isinstance(entry['owner'], bpy.types.PoseBone)}
                for name in keyed:
                    for constraint in holder.pose.bones[name].constraints:
                        if not constraint.mute:
                            constraint.mute = True
                            muted['constraints'] += 1
                for driver in holder.animation_data.drivers:
                    for name in keyed:
                        if driver.data_path.startswith('pose.bones["%s"].' % name) and driver.data_path.rsplit('.', 1)[-1] in {'location', 'rotation_quaternion', 'rotation_euler', 'rotation_axis_angle', 'scale'} and not driver.mute:
                            driver.mute = True
                            muted['drivers'] += 1
        scene = bpy.context.scene
        scene.frame_end = max(scene.frame_end, math.ceil(self.last))
        bpy.context.view_layer.update()
        return muted
