from pathlib import Path
import bpy


def camera_output(obj):
    if obj.type == 'CAMERA':
        return obj
    if obj.type != 'ARMATURE':
        raise ValueError('Choose a Blender camera or its camera control armature.')
    from .authored import dependencies
    cameras = [candidate for candidate in bpy.context.scene.objects
               if candidate.type == 'CAMERA' and obj in dependencies(candidate)]
    if not cameras:
        raise ValueError('This armature has no camera output. Choose the camera object used for rendering.')
    if len(cameras) != 1:
        raise ValueError('This armature drives more than one camera. Choose the camera object to import.')
    return cameras[0]


def append_camera_rig():
    scene = bpy.context.scene
    existing = next((obj for obj in scene.objects if obj.type == 'CAMERA' and obj.get('eclipse_camera_reference') == 'Camera_Rig.blend'), None)
    if existing:
        return existing
    path = Path(__file__).parent / 'Camera_Rig.blend'
    collection = bpy.data.collections.new('Eclipse Camera Rig')
    loaded = []
    try:
        with bpy.data.libraries.load(str(path), link=False) as (source, target):
            target.objects = source.objects
        loaded = [obj for obj in target.objects if obj]
        for obj in loaded:
            collection.objects.link(obj)
        camera = next(obj for obj in loaded if obj.type == 'CAMERA')
        camera['eclipse_camera_reference'] = 'Camera_Rig.blend'
        for obj in loaded:
            if obj.type == 'ARMATURE' and obj.animation_data:
                for driver in obj.animation_data.drivers:
                    if driver.data_path.startswith('pose.bones["FovPart"]') and 'FovPart' not in obj.pose.bones:
                        driver.mute = True
        scene.collection.children.link(collection)
        bpy.context.view_layer.update()
        for obj in loaded:
            if obj.name.startswith(('WGT-', '__CamMeta')):
                obj.hide_render = True
                obj.hide_set(True)
        if scene.camera is None:
            scene.camera = camera
        return camera
    except Exception:
        for obj in loaded:
            bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.collections.remove(collection)
        raise
