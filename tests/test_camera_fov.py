import bpy, importlib.util, sys, pathlib, json, bisect, math
root=pathlib.Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("eclipse_test",root/"__init__.py",submodule_search_locations=[str(root)])
addon=importlib.util.module_from_spec(spec);sys.modules[spec.name]=addon;spec.loader.exec_module(addon)
from eclipse_test.camera_fov import export_fov, selected_camera, ECLIPSE_OT_copy_camera_fov
from eclipse_test.transforms import camera_fov
addon.register()
assert addon.bl_info["version"]==(1,5,16)
scene=bpy.context.scene
cam=scene.camera
scene.frame_start=12;scene.frame_end=36;scene.render.fps=60
cam.data.lens=40;cam.data.keyframe_insert("lens",frame=12)
cam.data.lens=85;cam.data.keyframe_insert("lens",frame=24)
cam.data.lens=30;cam.data.keyframe_insert("lens",frame=36)
scene.frame_set(18,subframe=.375)
outputs=[];max_error=0
for fit,res,aspect in [("AUTO",(1920,1080),(1,1)),("VERTICAL",(1080,1920),(1,1)),("HORIZONTAL",(2048,1080),(2,1))]:
 cam.data.sensor_fit=fit
 scene.render.resolution_x,scene.render.resolution_y=res
 scene.render.pixel_aspect_x,scene.render.pixel_aspect_y=aspect
 data=json.loads(export_fov(bpy.context,cam))
 assert (scene.frame_current,scene.frame_subframe)==(18,.375)
 rows=data["samples"];frames=[p[0] for p in rows]
 for i in range(481):
  frame=i/20
  scene.frame_set(12+int(frame),subframe=frame-int(frame))
  value=camera_fov(cam.evaluated_get(bpy.context.evaluated_depsgraph_get()),scene)
  index=min(len(rows)-2,max(0,bisect.bisect_right(frames,frame)-1))
  a,b=rows[index:index+2]
  got=a[1]+(b[1]-a[1])*(frame-a[0])/(b[0]-a[0])
  max_error=max(max_error,abs(got-value))
  assert abs(got-value)<.001,(fit,frame,got,value)
 outputs.append(data)
 scene.frame_set(18,subframe=.375)
cam.data.type="ORTHO"
try:
 export_fov(bpy.context,cam)
 raise AssertionError("Orthographic accepted")
except ValueError:
 pass
assert (scene.frame_current,scene.frame_subframe)==(18,.375)
cam.data.type="PERSP"
cam.data.shift_x=.2
try:
 export_fov(bpy.context,cam)
 raise AssertionError("Lens shift accepted")
except ValueError:
 pass
cam.data.shift_x=0
from types import SimpleNamespace
clipboard=SimpleNamespace(clipboard='unchanged')
context=SimpleNamespace(
 scene=scene, selected_objects=bpy.context.selected_objects,
 evaluated_depsgraph_get=bpy.context.evaluated_depsgraph_get,
 window_manager=clipboard)
operator=SimpleNamespace(report=lambda *args:None)
assert ECLIPSE_OT_copy_camera_fov.execute(operator,context)=={'FINISHED'}
assert json.loads(clipboard.clipboard)['format']=='EclipseCameraFOV'
cam.data.type='ORTHO'
before_clipboard=clipboard.clipboard
assert ECLIPSE_OT_copy_camera_fov.execute(operator,context)=={'CANCELLED'}
assert clipboard.clipboard==before_clipboard
cam.data.type='PERSP'
second=bpy.data.objects.new('Second Camera',bpy.data.cameras.new('Second Camera'))
scene.collection.objects.link(second)
for obj in bpy.context.selected_objects:
 obj.select_set(False)
try:
 selected_camera(bpy.context)
 raise AssertionError("Ambiguous camera accepted")
except ValueError:
 pass
second.select_set(True)
assert selected_camera(bpy.context)==second
cam.select_set(True)
try:
 selected_camera(bpy.context)
 raise AssertionError("Two selected cameras accepted")
except ValueError:
 pass
addon.unregister()
print('FOV_TEST_PASSED', max_error, bpy.app.version_string)
