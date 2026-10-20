import json
import math
import bpy
from .transforms import camera_fov


def selected_camera(context):
    cameras = [obj for obj in context.selected_objects if obj.type == 'CAMERA']
    if len(cameras) == 1:
        return cameras[0]
    if len(cameras) > 1:
        raise ValueError('Select one camera to export its FOV.')
    cameras = [obj for obj in context.scene.objects if obj.type == 'CAMERA']
    if len(cameras) != 1:
        raise ValueError('Select a camera to export its FOV.')
    return cameras[0]


def export_fov(context, camera):
    scene = context.scene
    fps = scene.render.fps / scene.render.fps_base
    start, end = scene.frame_start, scene.frame_end
    if not math.isfinite(fps) or not 1 <= fps <= 240 or end < start or end - start > 144000:
        raise ValueError('Use 1–240 FPS and a range of at most 144,000 frames.')
    previous = scene.frame_current, scene.frame_subframe
    samples = {}
    kept = {0}

    def sample(frame):
        if frame not in samples:
            at = start + frame
            scene.frame_set(math.floor(at), subframe=at - math.floor(at))
            evaluated = camera.evaluated_get(context.evaluated_depsgraph_get())
            samples[frame] = camera_fov(evaluated, scene)
        return samples[frame]

    def refine(a, b, depth=0):
        va, vb = sample(a), sample(b)
        checks = [(a + (b - a) * t, t) for t in (.25, .5, .75)]
        errors = [abs(sample(frame) - (va + (vb - va) * t)) for frame, t in checks]
        if max(errors) > .0004:
            if depth >= 8:
                raise ValueError('FOV changes too sharply to export accurately. Smooth the lens animation.')
            middle = (a + b) / 2
            refine(a, middle, depth + 1)
            refine(middle, b, depth + 1)
        else:
            kept.update((a, b))

    try:
        sample(0)
        for frame in range(end - start):
            refine(frame, frame + 1)
        return json.dumps({
            'format': 'EclipseCameraFOV',
            'version': 1,
            'camera': camera.name,
            'fps': fps,
            'startFrame': start,
            'endFrame': end,
            'units': 'VerticalDegrees',
            'samples': [[frame, value] for frame, value in sorted(samples.items()) if frame in kept],
        }, separators=(',', ':'), allow_nan=False)
    finally:
        scene.frame_set(previous[0], subframe=previous[1])


class ECLIPSE_OT_copy_camera_fov(bpy.types.Operator):
    bl_idname = 'eclipse.copy_camera_fov'
    bl_label = 'Copy Camera FOV'
    bl_description = 'Copy evaluated camera FOV for Add Blender Camera FOV in Eclipse'

    def execute(self, context):
        try:
            text = export_fov(context, selected_camera(context))
            context.window_manager.clipboard = text
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        self.report({'INFO'}, 'Camera FOV copied. Paste it into Add Blender Camera FOV in Eclipse.')
        return {'FINISHED'}
