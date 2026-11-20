"""Blender, headless: a turntable video of a 3D model.

  blender -b --factory-startup --python-exit-code 1 -P turntable.py -- in.glb outdir seconds background

Centers the model, puts a camera on a slow full circle around it, and renders PNG frames with
Workbench (textures, studio light, soft shadow and cavity: no GPU context needed headless).
The node turns the frames into outdir/turntable.mp4 with ffmpeg and keeps one frame as a poster.

This file ships with the node. A task only gives it a file and two values.
"""

import math
import os
import sys

import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:]
SRC, OUT, SECONDS, BG = argv[0], argv[1], float(argv[2]), argv[3]
FPS, SIZE = 24, 720

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=SRC)
scene = bpy.context.scene
meshes = [o for o in scene.objects if o.type == "MESH"]
if not meshes:
    raise SystemExit("no mesh in the file")

# bounds of everything, in world space
lo, hi = Vector((1e9, 1e9, 1e9)), Vector((-1e9, -1e9, -1e9))
for o in meshes:
    for c in o.bound_box:
        w = o.matrix_world @ Vector(c)
        lo, hi = Vector(map(min, lo, w)), Vector(map(max, hi, w))
center, size = (lo + hi) / 2, hi - lo
radius = max(size.x, size.y, size.z) / 2 or 1.0

pivot = bpy.data.objects.new("pivot", None)
scene.collection.objects.link(pivot)
pivot.location = center
cam_data = bpy.data.cameras.new("cam")
cam_data.lens = 50
cam = bpy.data.objects.new("cam", cam_data)
scene.collection.objects.link(cam)
cam.parent = pivot
dist = radius / math.tan(cam_data.angle / 2) * 1.25
elev = math.radians(12)
cam.location = (0, -dist * math.cos(elev), dist * math.sin(elev))
track = cam.constraints.new("TRACK_TO")
track.target, track.track_axis, track.up_axis = pivot, "TRACK_NEGATIVE_Z", "UP_Y"
scene.camera = cam

frames = max(24, int(SECONDS * FPS))
scene.frame_start, scene.frame_end = 1, frames
pivot.rotation_euler = (0, 0, 0)
pivot.keyframe_insert("rotation_euler", frame=1)
pivot.rotation_euler = (0, 0, math.radians(360) * frames / (frames + 1))  # the last frame stops one step short: a seamless loop
pivot.keyframe_insert("rotation_euler", frame=frames)
for fc in (pivot.animation_data.action.fcurves if hasattr(pivot.animation_data.action, "fcurves") else []):
    for k in fc.keyframe_points:
        k.interpolation = "LINEAR"
try:  # Blender 4.4+: layered actions
    for layer in pivot.animation_data.action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                for fc in bag.fcurves:
                    for k in fc.keyframe_points:
                        k.interpolation = "LINEAR"
except AttributeError:
    pass

scene.render.engine = "BLENDER_WORKBENCH"
shading = scene.display.shading
shading.light, shading.color_type = "STUDIO", "TEXTURE"
shading.show_shadows, shading.show_cavity = True, True
shading.cavity_type = "BOTH"
scene.display.shadow_focus = 0.6
try:
    scene.view_settings.view_transform = "Standard"
except TypeError:
    pass
world = bpy.data.worlds.new("w")
scene.world = world
world.color = (0.012, 0.013, 0.018) if BG == "dark" else (0.9, 0.9, 0.9)  # a final render takes the world's color
scene.render.resolution_x = scene.render.resolution_y = SIZE
scene.render.fps = FPS
scene.render.image_settings.file_format = "PNG"
os.makedirs(os.path.join(OUT, "frames"), exist_ok=True)
scene.render.filepath = os.path.join(OUT, "frames", "f_")
bpy.ops.render.render(animation=True)
