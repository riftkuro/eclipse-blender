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


def receive_take(pairs, take):
    frames = take.get('frames', [])
    if not frames or len(frames) > 20001:
        raise ValueError('invalid animation length')
    collection = bpy.data.collections.new('Eclipse Take')
    bpy.context.scene.collection.children.link(collection)
    created, outputs, mapped = [], [], []
    try:
        for pair in pairs:
            source = bpy.data.objects.get(pair['object'])
            if source is None:
                raise ValueError('paired object was removed')
            obj = source.copy()
            obj.data = source.data.copy()
            obj.name = source.name + ' • Eclipse Take'
            obj.animation_data_clear()
            obj.data.animation_data_clear()
            obj.parent = None
            obj.matrix_world = source.matrix_world.copy()
            obj.constraints.clear()
            if obj.type == 'ARMATURE':
                for bone in obj.pose.bones:
                    for constraint in list(bone.constraints):
                        bone.constraints.remove(constraint)
            collection.objects.link(obj)
            created.append(obj)
            outputs.append(obj)
            if obj.type == 'ARMATURE':
                copies = {source: obj}
                remaining = list(bpy.context.scene.objects)
                progress = True
                while progress:
                    progress = False
                    for original in remaining[:]:
                        if original in copies or original in created or original.type not in {'MESH', 'EMPTY'}:
                            continue
                        linked = original.parent in copies or any(getattr(m, 'object', None) in copies for m in original.modifiers)
                        linked = linked or any(getattr(c, 'target', None) in copies for c in original.constraints)
                        if not linked:
                            continue
                        mesh = original.copy()
                        mesh.name = original.name + ' - Eclipse Take'
                        collection.objects.link(mesh)
                        mesh.hide_set(not original.visible_get())
                        copies[original] = mesh
                        created.append(mesh)
                        remaining.remove(original)
                        progress = True
                for original, mesh in copies.items():
                    if original == source:
                        continue
                    if mesh.parent in copies:
                        mesh.parent = copies[mesh.parent]
                    for modifier in mesh.modifiers:
                        if getattr(modifier, 'object', None) in copies:
                            modifier.object = copies[modifier.object]
                    for constraint in mesh.constraints:
                        if getattr(constraint, 'target', None) in copies:
                            constraint.target = copies[constraint.target]
                    mesh.matrix_parent_inverse = original.matrix_parent_inverse.copy()
                    mesh.matrix_basis = original.matrix_basis.copy()
            new_pair = dict(pair, object=obj.name)
            new_pair['tracks'] = [dict(m) for m in pair['tracks']]
            for mapping in new_pair['tracks']:
                if mapping['source'].startswith('@'):
                    original = bpy.data.objects.get(mapping['source'][1:])
                    if original is None:
                        raise ValueError('mapped object was removed')
                    prop = original.copy()
                    prop.animation_data_clear()
                    prop.constraints.clear()
                    prop.parent = None
                    prop.name = original.name + ' • Eclipse Take'
                    collection.objects.link(prop)
                    created.append(prop)
                    mapping['source'] = '@' + prop.name
            mapped.append(new_pair)
        scene = bpy.context.scene
        source_fps = take['fps']
        target_fps = scene.render.fps / scene.render.fps_base
        for sample in frames:
            frame = sample['frame'] / source_fps * target_fps
            values = {v['id']: v for v in sample['rigs']}
            for pair, obj in zip(mapped, outputs):
                row = values.get(pair['rig'])
                if not row:
                    continue
                if pair['kind'] == 'camera':
                    obj.matrix_world = restored(unpack(row['camera']), pair['units'])
                    obj.rotation_mode = 'QUATERNION'
                    obj.keyframe_insert('location', frame=frame)
                    obj.keyframe_insert('rotation_quaternion', frame=frame)
                    set_camera_fov(obj, row['fov'])
                    obj.data.keyframe_insert('lens', frame=frame)
                    continue
                tracks = {v['id']: v for v in row['tracks']}
                alignment = unpack(pair['alignment']).inverted()
                by_source = {m['source']: m for m in pair['tracks']}
                for mapping in pair['tracks']:
                    if not mapping['source'].startswith('@') or mapping['id'] not in tracks:
                        continue
                    prop = bpy.data.objects[mapping['source'][1:]]
                    value = tracks[mapping['id']]
                    world = restored(alignment @ unpack(value['world']) @ unpack(mapping['offset']).inverted(), pair['units'])
                    scale, base = value.get('scale', [1, 1, 1]), mapping.get('rest_scale', [1, 1, 1])
                    prop.matrix_world = world @ Matrix.Diagonal(tuple(scale[i] * base[i] for i in range(3)) + (1,))
                    prop.rotation_mode = 'QUATERNION'
                    for channel in ('location', 'rotation_quaternion', 'scale'):
                        prop.keyframe_insert(channel, frame=frame)
                bones = sorted(obj.pose.bones, key=lambda b: len(b.parent_recursive))
                for bone in bones:
                    mapping = by_source.get(bone.name)
                    if not mapping or mapping['id'] not in tracks:
                        continue
                    world = restored(alignment @ unpack(tracks[mapping['id']]['world']) @ unpack(mapping['offset']).inverted(), pair['units'])
                    scale = tracks[mapping['id']].get('scale', [1, 1, 1])
                    base = mapping.get('rest_scale', [1, 1, 1])
                    world = world @ Matrix.Diagonal(tuple(scale[i] * base[i] for i in range(3)) + (1,))
                    bone.matrix = obj.matrix_world.inverted() @ world @ correction(bone.bone).inverted()
                    bone.rotation_mode = 'QUATERNION'
                    bone.keyframe_insert('location', frame=frame)
                    bone.keyframe_insert('rotation_quaternion', frame=frame)
                    bone.keyframe_insert('scale', frame=frame)
                    bpy.context.view_layer.update()
        from .engine import curves
        for obj in created:
            for owner in (obj, obj.data):
                for curve in curves(owner):
                    for key in curve.keyframe_points:
                        key.interpolation = 'LINEAR'
        return [o.name for o in created]
    except Exception:
        for obj in created:
            bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.collections.remove(collection)
        raise
