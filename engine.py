import bpy
import hashlib
import json
import math
import queue
import time
from . import transforms, transport, authored, interpolation
from .receiver import Receiver, TakeWriter

session = None
manifest = None
pairs = []
direction = 'blender'
live = False
status = 'Start the server, then connect from an Eclipse file.'
last_poll = 0.0
revision = 0
take = None
job = None
last_digest = None
next_digest = 0.0
receiver = Receiver()
last_pose = None
last_playback = None
playback_revision = 0
last_live_take = None
suspended = False
transfers = {}
writer = None


def curves(owner):
    animation = getattr(owner, 'animation_data', None)
    if not animation or not animation.action:
        return []
    action = animation.action
    if getattr(action, 'is_action_layered', False):
        slot = getattr(animation, 'action_slot', None)
        result = []
        for layer in action.layers:
            for strip in layer.strips:
                if hasattr(strip, 'channelbag') and slot:
                    bag = strip.channelbag(slot, ensure=False)
                    if bag:
                        result.extend(bag.fcurves)
        return result
    return list(getattr(action, 'fcurves', []))


def digest():
    value = []
    actions = {}
    objects = {}
    for pair in pairs:
        obj = bpy.data.objects.get(pair['object'])
        if obj:
            objects.update({o.name:o for o in authored.dependencies(obj)})
    for pair in pairs:
        for mapping in pair['tracks']:
            if mapping['source'].startswith('@'):
                obj = bpy.data.objects.get(mapping['source'][1:])
                if obj:
                    objects.update({o.name:o for o in authored.dependencies(obj)})
    for obj in objects.values():
        for owner in (obj, obj.data):
            animation = getattr(owner, 'animation_data', None)
            if not animation:
                continue
            if animation.action:
                actions[animation.action.name] = animation.action
            for track in animation.nla_tracks:
                for strip in track.strips:
                    if strip.action:
                        actions[strip.action.name] = strip.action
                        value.append((track.mute, strip.mute, strip.frame_start, strip.frame_end, strip.scale, strip.repeat, strip.use_reverse))
    scene = bpy.context.scene
    value.append((scene.render.fps, scene.render.fps_base, scene.render.resolution_x, scene.render.resolution_y, scene.render.pixel_aspect_x, scene.render.pixel_aspect_y))
    for action in sorted(actions.values(), key=lambda a: a.name):
        value.append((action.name, str(action.frame_range)))
        if getattr(action, 'is_action_layered', False):
            action_curves = [f for layer in action.layers for strip in layer.strips for bag in getattr(strip, 'channelbags', []) for f in bag.fcurves]
        else:
            action_curves = getattr(action, 'fcurves', [])
        for curve in action_curves:
            value.append((curve.data_path, curve.array_index, [(tuple(k.co), tuple(k.handle_left), tuple(k.handle_right), k.interpolation, k.easing, k.handle_left_type, k.handle_right_type, k.back, k.amplitude, k.period) for k in curve.keyframe_points]))
    return hashlib.sha256(repr(value).encode()).hexdigest()


def catalog():
    values = []
    for obj in bpy.context.scene.objects:
        if obj.type == 'ARMATURE':
            values.append({'name': obj.name, 'kind': 'rig', 'bones': [b.name for b in obj.data.bones], 'output': sum('transform' in b for b in obj.data.bones)})
        elif obj.type == 'CAMERA':
            values.append({'name': obj.name, 'kind': 'camera', 'bones': []})
    return values


def pose():
    scene = bpy.context.scene
    depsgraph = bpy.context.evaluated_depsgraph_get()
    fps = scene.render.fps / scene.render.fps_base
    playing = any(window.screen.is_animation_playing for window in bpy.context.window_manager.windows)
    return {'time': max(0, (scene.frame_current + scene.frame_subframe) / fps), 'playing': playing,
            'start': max(0, scene.frame_start / fps), 'finish': (scene.frame_end + 1) / fps,
            'rigs': [transforms.sample(pair, scene, depsgraph) for pair in pairs]}


def updates():
    global last_pose
    packet = pose()
    encoded = json.dumps(packet, separators=(',', ':'))
    result = {'pose': packet} if packet['playing'] or encoded != last_pose else {}
    last_pose = encoded
    return result


def persist():
    bpy.context.scene['eclipse_pairs'] = json.dumps(pairs)


def queue_bake(manual=False):
    global job, status
    if not pairs:
        raise ValueError('pair at least one rig or camera first')
    if not session or not manifest:
        raise ValueError('Connect from an Eclipse file first')
    manual = manual or bool(job and job.get('manual'))
    scene = bpy.context.scene
    fps = manifest['fps']
    blender_fps = scene.render.fps / scene.render.fps_base
    at = sorted({math.floor(f / blender_fps * fps + .5) for pair in pairs for f in authored.frames(pair)})
    keys = {p['rig']: at for p in pairs}
    samples, jumps = interpolation.sample_positions(at)
    if samples[-1] > 20000 or len(samples) * sum(max(1, len(p['tracks'])) for p in pairs) > 1000000:
        raise ValueError('shorten the animation range before syncing this take')
    job = {'session': session, 'frames': [], 'at': 0, 'samples': samples, 'keys': keys,
           'positions': at, 'jumps': jumps, 'fps': fps, 'source_fps': blender_fps,
           'pairs': json.loads(json.dumps(pairs)), 'manual': manual}
    status = 'Sampling animation…'


def step_bake():
    global job, take, revision, status
    if not job:
        return
    current = job
    if current['session'] != session:
        job = None
        return
    scene = bpy.context.scene
    before = scene.frame_current + scene.frame_subframe
    started = time.monotonic()
    try:
        while current['at'] < len(current['samples']):
            index = current['samples'][current['at']]
            at = index / current['fps'] * current['source_fps']
            scene.frame_set(math.floor(at), subframe=at % 1)
            depsgraph = bpy.context.evaluated_depsgraph_get()
            current['frames'].append({'frame': index, 'boundary': index in current['jumps'], 'rigs': [transforms.sample(p, scene, depsgraph) for p in current['pairs']]})
            current['at'] += 1
            if time.monotonic() - started > .012:
                break
        if current['at'] == len(current['samples']):
            if 'refine' not in current:
                current['refine'] = [(a, b, 0) for a, b in zip(current['frames'], current['frames'][1:]) if not b['boundary']]
            while current['refine']:
                left, right, depth = current['refine'].pop()
                index = (left['frame'] + right['frame']) / 2
                at = index / current['fps'] * current['source_fps']
                scene.frame_set(math.floor(at), subframe=at % 1)
                depsgraph = bpy.context.evaluated_depsgraph_get()
                middle = {'frame': index, 'rigs': [transforms.sample(p, scene, depsgraph) for p in current['pairs']]}
                if interpolation.needs_split(left, middle, right):
                    current['frames'].append(middle)
                    if depth < 10:
                        current['refine'].extend([(left, middle, depth + 1), (middle, right, depth + 1)])
                if len(current['frames']) * sum(max(1, len(p['tracks'])) for p in current['pairs']) > 1000000:
                    raise ValueError('shorten the animation range before syncing this take')
                if time.monotonic() - started > .012:
                    return
            current['frames'].sort(key=lambda f: f['frame'])
            current['curves'] = interpolation.collect(current['pairs'], current['positions'], [f['frame'] for f in current['frames']], current['fps'] / current['source_fps'])
            revision += 1
            take = {'revision': revision, 'fps': current['fps'], 'frames': current['frames'], 'keys': current['keys'], 'session': session, 'baked': False, 'curves': current['curves'], 'manual': current['manual']}
            job = None
            status = 'Animation ready'
    except Exception:
        job = None
        raise
    finally:
        scene.frame_set(math.floor(before), subframe=before % 1)


# studio posts long takes as frame chunks; the take is only handed on once every part is here
def assemble(data):
    take = data.get('take')
    if not take:
        return None
    parts, part, key = data.get('parts'), data.get('part'), data.get('transfer')
    if parts is None:
        return take
    if not isinstance(parts, int) or not isinstance(part, int) or not 1 <= part <= parts or not isinstance(key, str):
        raise ValueError('invalid animation transfer')
    entry = transfers.get(key)
    if entry is None or entry['parts'] != parts:
        transfers.clear()
        entry = transfers[key] = {'parts': parts, 'frames': {}}
    entry['frames'][part] = take.get('frames', [])
    if len(entry['frames']) < parts:
        return None
    del transfers[key]
    return dict(take, frames=[frame for index in range(1, parts + 1) for frame in entry['frames'][index]])


def step_writer():
    global writer, status
    if not writer:
        return
    current = writer
    try:
        if not current.step(.012):
            return
        writer = None
        muted = current.finish()
        status = 'Take applied to ' + ', '.join(current.names()) + '.'
        parts = ['%d %s' % (count, kind if count != 1 else kind[:-1]) for kind, count in muted.items() if count]
        if parts:
            status += ' Muted ' + ' and '.join(parts) + ' on synced bones so it plays.'
    except Exception:
        writer = None
        raise


def disconnect():
    global session, manifest, live, job, take, status, last_pose, last_playback, last_live_take, suspended, writer
    writer = None
    receiver.clear()
    transfers.clear()
    session = manifest = job = take = None
    live = False
    last_pose = last_playback = last_live_take = None
    suspended = False
    status = 'Disconnected'


def handle(path, data):
    global session, manifest, pairs, direction, live, last_poll, take, status, last_digest, job, last_pose, last_playback, last_live_take, suspended, writer
    if path == '/connect':
        disconnect()
        candidate = data.get('manifest')
        if not isinstance(candidate, dict) or not candidate.get('rigs') or not 1 <= candidate.get('fps', 0) <= 1000:
            raise ValueError('open an Eclipse file containing a rig or camera')
        session, manifest = data['session'], candidate
        allowed = {r['id'] for r in manifest['rigs']}
        saved = json.loads(bpy.context.scene.get('eclipse_pairs', '[]'))
        pairs = []
        for previous in saved:
            rig = next((r for r in manifest['rigs'] if r['id'] == previous.get('rig')), None)
            obj = bpy.data.objects.get(previous.get('object', ''))
            if not rig or not obj:
                continue
            try:
                pairs.append(transforms.bind(obj, rig, {m['id']: m['source'] for m in previous.get('tracks', [])}, previous.get('units', 1)))
            except ValueError:
                continue
        persist()
        last_digest = digest()
        last_poll = time.monotonic()
        status = 'Connected to ' + manifest.get('name', 'Eclipse')
        return {'session': session, 'objects': catalog(), 'pairs': pairs, 'version': bpy.app.version_string}
    if session is None or data.get('session') != session:
        raise ValueError('Eclipse file changed; reconnect')
    resumed = suspended
    if suspended:
        last_pose = last_playback = last_live_take = None
    last_poll = time.monotonic()
    suspended = False
    if path == '/disconnect':
        disconnect()
        return {'ok': True}
    if path == '/pair':
        rig = next((r for r in manifest['rigs'] if r['id'] == data.get('rig')), None)
        if not rig:
            raise ValueError('Choose an Eclipse rig first.')
        obj = authored.automatic(rig, bpy.context.scene.objects, [p['object'] for p in pairs if p['rig'] != rig['id']]) if data.get('auto') else bpy.data.objects.get(data.get('object', ''))
        if not obj:
            raise ValueError('Choose a Blender object first, or click Auto.')
        units = data.get('units', 1)
        if not rig or not obj or not isinstance(units, (int, float)) or not math.isfinite(units) or not .0001 <= units <= 10000:
            raise ValueError('choose a valid project rig and Blender object')
        receiver.clear()
        pair = transforms.bind(obj, rig, data.get('mapping'), units)
        pairs = [p for p in pairs if p['rig'] != rig['id']]
        pairs.append(pair)
        persist()
        take = job = None
        status = 'Paired ' + obj.name
        scale = pair.get('fit', {}).get('scale', 1)
        if pair['kind'] == 'rig' and abs(scale - 1) > .15:
            status += ' · rig looks %.2gx the Eclipse size, check Studs/unit' % scale
        return {'pairs': pairs, 'objects': catalog()}
    if path == '/bones':
        from . import structure
        pair = next((p for p in pairs if p['rig'] == data.get('rig')), None)
        if pair is None:
            raise ValueError('Pair this rig before syncing bones.')
        return structure.bones(pair)
    if path == '/refresh':
        from . import structure
        receiver.clear()
        candidate = data['manifest']
        updated = structure.refresh(pairs, candidate, data.get('additions', {}))
        manifest, pairs = candidate, updated
        live = False
        job = take = None
        persist()
        return {'pairs': pairs, 'objects': catalog()}
    if path == '/unpair':
        receiver.clear()
        pairs = [p for p in pairs if p['rig'] != data['rig']]
        persist()
        take = job = None
        status = 'Pair removed'
        return {'pairs': pairs}
    if path == '/mode':
        if data.get('direction') not in {'blender', 'eclipse'}:
            raise ValueError('choose a sync direction')
        receiver.clear()
        direction, live = data['direction'], data.get('live') is True
        last_pose = last_playback = last_live_take = None
        take = job = None
        if live and direction == 'blender':
            queue_bake()
        else:
            status = 'Eclipse → Blender · Live' if live else 'Live sync paused'
        return {'ok': True}
    if path == '/bake':
        queue_bake(manual=True)
        return {'ok': True}
    if path == '/take':
        full = assemble(data)
        if full is None:
            return {'objects': [], 'part': data.get('part')}
        receiver.clear()
        writer = TakeWriter(pairs, full)
        status = 'Applying Eclipse take…'
        return {'objects': writer.names(), 'queued': True}
    if path == '/poll':
        wanted = data.get('mode')
        if wanted and (wanted.get('direction'), wanted.get('live')) != (direction, live):
            handle('/mode', dict(wanted, session=session))
        result = {'session': session, 'pairs': pairs, 'status': status, 'sampling': job is not None, 'resumed': resumed}
        if live and direction == 'blender':
            pass
        elif live and direction == 'eclipse':
            if data.get('take') and data.get('takeRevision') != last_live_take:
                full = assemble(data)
                result['takePart'] = data.get('part')
                if full is not None:
                    receiver.record(pairs, full)
                    last_live_take = data['takeRevision']
                    status = 'Eclipse keys updated'
            if data.get('pose'):
                receiver.follow(pairs, data['pose'])
            result['takeAck'] = last_live_take
        if take and data.get('ack', 0) < take['revision']:
            result['take'] = take
        return result
    raise ValueError('unknown bridge operation')


def tick():
    global status, last_digest, next_digest, suspended
    if not transport.server:
        return None
    for _ in range(4):
        try:
            path, data, event, result = transport.pending.get_nowait()
        except queue.Empty:
            break
        if result.get('cancelled'):
            continue
        try:
            result.update(handle(path, data))
        except Exception as error:
            result['error'] = str(error)
            status = str(error)
        finally:
            event.set()
    if session and time.monotonic() - last_poll > 15 and not suspended:
        receiver.clear()
        transfers.clear()
        suspended = True
        status = 'Waiting for Eclipse; connection will resume automatically'
    try:
        if session and live and direction == 'blender' and time.monotonic() >= next_digest:
            next_digest = time.monotonic() + .5
            current = digest()
            if current != last_digest:
                last_digest = current
                queue_bake()
        step_bake()
        step_writer()
        if session and live and direction == 'eclipse' and not suspended:
            receiver.advance()
    except Exception as error:
        status = str(error)
    return .02


def start():
    global status
    if not transport.server:
        transport.start()
        status = 'Server ready - open Blender Import in Eclipse.'
    if not bpy.app.timers.is_registered(tick):
        bpy.app.timers.register(tick, first_interval=.02, persistent=True)


def stop():
    disconnect()
    if bpy.app.timers.is_registered(tick):
        bpy.app.timers.unregister(tick)
    transport.stop()
