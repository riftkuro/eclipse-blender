bl_info = {
    'name': 'Eclipse',
    'author': 'riftkuro',
    'version': (1, 5, 0),
    'blender': (4, 2, 0),
    'location': 'View3D > Sidebar > Eclipse',
    'description': 'Sync rigs, custom output bones and cameras with an Eclipse animation file',
    'category': 'Import-Export',
}

import bpy
from bpy.app.handlers import persistent
from bpy_extras.io_utils import ImportHelper
from . import engine


class ECLIPSE_OT_server(bpy.types.Operator):
    bl_idname = 'eclipse.server'
    bl_label = 'Start Server'
    bl_description = 'Connect from Blender Import inside an open Eclipse file'

    def execute(self, context):
        try:
            if engine.transport.server:
                engine.stop()
            else:
                engine.start()
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        return {'FINISHED'}


class ECLIPSE_OT_bake(bpy.types.Operator):
    bl_idname = 'eclipse.bake'
    bl_label = 'Send Animation to Eclipse'
    bl_description = 'Sample paired output rigs and cameras at the Eclipse file FPS'

    def execute(self, context):
        try:
            engine.queue_bake(manual=True)
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        return {'FINISHED'}


class ECLIPSE_OT_import_rig(bpy.types.Operator, ImportHelper):
    bl_idname = 'eclipse.import_rig'
    bl_label = 'Eclipse Rig (.obj)'
    bl_description = 'Import geometry, bone hierarchy, attachments and OBJ materials exported by Eclipse'
    filename_ext = '.obj'
    filter_glob: bpy.props.StringProperty(default='*.obj', options={'HIDDEN'})

    def execute(self, context):
        try:
            from .obj_rig import import_rig
            outputs = import_rig(self.filepath)
            engine.status = 'Imported ' + ', '.join(obj.name for obj in outputs)
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        return {'FINISHED'}


class ECLIPSE_OT_camera_rig(bpy.types.Operator):
    bl_idname = 'eclipse.camera_rig'
    bl_label = 'Add Camera Rig'
    bl_description = 'Append the supplied camera rig, including its scale-driven FOV, to this scene'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            from .camera_rig import append_camera_rig
            camera = append_camera_rig()
            engine.status = 'Camera rig ready. Pair the Eclipse camera with ' + camera.name
        except Exception as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        return {'FINISHED'}


class ECLIPSE_PT_main(bpy.types.Panel):
    bl_label = 'Eclipse'
    bl_idname = 'ECLIPSE_PT_main'
    bl_category = 'Eclipse'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'

    def draw(self, context):
        layout = self.layout
        layout.operator('eclipse.server', text='Stop Server' if engine.transport.server else 'Start Server', icon='LINKED' if engine.transport.server else 'UNLINKED')
        box = layout.box()
        box.label(text=engine.status[:80])
        if engine.session:
            box.label(text=('Blender → Eclipse' if engine.direction == 'blender' else 'Eclipse → Blender') + (' • Live' if engine.live else ' • Paused'))
            for pair in engine.pairs:
                box.label(text=pair['object'], icon='CAMERA_DATA' if pair['kind'] == 'camera' else 'ARMATURE_DATA')
            box.operator('eclipse.bake', icon='EXPORT')
        else:
            box.label(text='Open a file in Eclipse, then Blender Import.')
        layout.operator('eclipse.import_rig', text='Import Rig (.obj)', icon='IMPORT')
        layout.operator('eclipse.camera_rig', icon='CAMERA_DATA')
        layout.label(text='Pair existing custom output rigs in Eclipse.')


@persistent
def before_load(_):
    engine.disconnect()


_classes = (ECLIPSE_OT_server, ECLIPSE_OT_bake, ECLIPSE_OT_import_rig, ECLIPSE_OT_camera_rig, ECLIPSE_PT_main)
_registered = False


def register():
    global _registered
    if _registered:
        return
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.app.handlers.load_pre.append(before_load)
    _registered = True


def unregister():
    global _registered
    if not _registered:
        return
    engine.stop()
    if before_load in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(before_load)
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
    _registered = False
