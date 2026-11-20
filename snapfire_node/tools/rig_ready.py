"""Blender, headless: make a generated character ready for an auto-rigger.

  blender -b --factory-startup --python-exit-code 1 -P rig_ready.py -- in.glb outdir height pose

Joins the parts, stands the model on the floor (feet at z=0) centered and facing
front (-Y in Blender, +Z in glTF), scales it to `height` meters, measures the
pose, and writes outdir/character.glb, character.fbx, front.png, side.png,
hand.png and report.json.

This file ships with the node. A task only gives it a file and two values.
"""

import json
import math
import os
import sys

import bpy
from mathutils import Matrix, Vector
from mathutils.kdtree import KDTree

argv = sys.argv[sys.argv.index("--") + 1:]
SRC, OUT, HEIGHT, POSE = argv[0], argv[1], float(argv[2]), argv[3]
# TRELLIS.2 models face +Z in glTF, which the importer turns into -Y: Blender's front.
FACING_TURN = float(os.environ.get("FACING_TURN", "0"))  # degrees about Z, for other generators

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=SRC)
scene = bpy.context.scene

meshes = [o for o in scene.objects if o.type == "MESH"]
if not meshes:
    raise SystemExit("no mesh in the file")
for o in meshes:  # keep world placement, drop the empties above them
    m = o.matrix_world.copy()
    o.parent = None
    o.matrix_world = m
for o in [o for o in scene.objects if o.type != "MESH"]:
    bpy.data.objects.remove(o, do_unlink=True)
bpy.ops.object.select_all(action="DESELECT")
for o in meshes:
    o.select_set(True)
bpy.context.view_layer.objects.active = meshes[0]
if len(meshes) > 1:
    bpy.ops.object.join()
obj = bpy.context.view_layer.objects.active
obj.name = obj.data.name = "Character"
if FACING_TURN:
    obj.rotation_euler.z += math.radians(FACING_TURN)
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)


def bounds(o):
    vs = [v.co for v in o.data.vertices]
    lo = Vector((min(v.x for v in vs), min(v.y for v in vs), min(v.z for v in vs)))
    hi = Vector((max(v.x for v in vs), max(v.y for v in vs), max(v.z for v in vs)))
    return lo, hi


lo, hi = bounds(obj)
size = hi - lo
turned = False
if size.y > size.x * 1.15:  # arms run front-to-back: the model is side-on, turn it a quarter
    obj.rotation_euler.z += math.radians(90)
    bpy.ops.object.transform_apply(rotation=True)
    lo, hi = bounds(obj)
    size, turned = hi - lo, True

s = HEIGHT / size.z
obj.scale = (s, s, s)
bpy.ops.object.transform_apply(scale=True)
lo, hi = bounds(obj)
center = (lo + hi) / 2
obj.data.transform(Matrix.Translation((-center.x, -center.y, -lo.z)))
obj.data.update()
lo, hi = bounds(obj)
size = hi - lo

# ---- measure the pose -------------------------------------------------------
verts = [v.co.copy() for v in obj.data.vertices]
span = size.x
half = span / 2


def hand_height(sign):
    """Mean height of the outermost 4% of the span on one side: where the hand is."""
    tip = [v.z for v in verts if sign * v.x > half * 0.92]
    return sum(tip) / len(tip) / HEIGHT if tip else 0.0


def arm_slope_deg():
    """Rough angle of the arms below level, from the arm's center line out past the head and
    torso (55% to 88% of the half-span), both sides together. A coat or hips under the arm
    pull it steeper, so it only sorts poses: under 20 is T, under 75 is A."""
    xs, zs = [], []
    for sign in (-1, 1):
        for k in range(8):
            f = 0.55 + k * 0.33 / 7
            col = [v.z for v in verts if abs(sign * v.x - half * f) < half * 0.02]
            if col:
                xs.append(half * f)
                zs.append((min(col) + max(col)) / 2)
    if len(xs) < 4:
        return 90.0
    mx, mz = sum(xs) / len(xs), sum(zs) / len(zs)
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (z - mz) for x, z in zip(xs, zs)) / sxx if sxx else 0.0
    return math.degrees(math.atan(-slope))


def finger_runs(sign):
    """Most separate pieces a vertical cut through the hand crosses (fingers seen from the front).
    Spread fingers: 3 to 5. Fingers stuck together: 1 or 2."""
    gap = HEIGHT * 0.006
    best = 0
    for k in range(12):
        f = 0.86 + k * 0.012
        col = sorted(v.z for v in verts if abs(sign * v.x - half * f) < half * 0.003)
        if not col:
            continue
        runs = 1 + sum(1 for a, b in zip(col, col[1:]) if b - a > gap)
        best = max(best, runs)
    return min(best, 5)


left, right = hand_height(1), hand_height(-1)  # facing -Y, the character's right hand is at -X
angle = max(0.0, arm_slope_deg())
fingers = {"left": finger_runs(1), "right": finger_runs(-1)}

tree = KDTree(len(verts))
for i, v in enumerate(verts):
    tree.insert(v, i)
tree.balance()
step = max(1, len(verts) // 3000)
mirror = [tree.find(Vector((-v.x, v.y, v.z)))[2] for v in verts[::step]]
symmetry = 1 - min(1.0, (sum(mirror) / len(mirror)) / (HEIGHT * 0.02))

looks = "T" if angle < 20 else ("A" if angle < 75 else "arms down")
report = {
    "height_m": round(size.z, 3), "width_m": round(size.x, 3), "depth_m": round(size.y, 3),
    "span_to_height": round(span / size.z, 3), "arm_angle_deg": round(angle, 1),
    "hand_height": {"left": round(left, 3), "right": round(right, 3)},
    "symmetry": round(symmetry, 3), "looks_like": looks, "asked_for": POSE,
    "pose_ok": looks == POSE and abs(left - right) < 0.06,
    "finger_gaps": fingers, "fingers_apart": min(fingers.values()) >= 3,
    "faces": len(obj.data.polygons), "vertices": len(verts), "turned_quarter": turned,
}

# ---- export ------------------------------------------------------------------
os.makedirs(OUT, exist_ok=True)
bpy.ops.object.select_all(action="DESELECT")
obj.select_set(True)
bpy.ops.export_scene.gltf(filepath=os.path.join(OUT, "character.glb"), export_format="GLB", use_selection=True)
for img in bpy.data.images:  # FBX embeds textures from files, so give packed ones a path
    if img.packed_file and img.source == "FILE":
        img.filepath_raw = os.path.join(OUT, "tex", bpy.path.clean_name(img.name) + ".png")
        img.file_format = "PNG"
        os.makedirs(os.path.dirname(img.filepath_raw), exist_ok=True)
        img.save()
bpy.ops.export_scene.fbx(filepath=os.path.join(OUT, "character.fbx"), use_selection=True, path_mode="COPY",
                         embed_textures=True, apply_scale_options="FBX_SCALE_UNITS", add_leaf_bones=False)

# ---- renders -----------------------------------------------------------------
scene.render.resolution_x = scene.render.resolution_y = 768
scene.render.film_transparent = False
scene.view_settings.view_transform = "Standard"  # true texture colors, white stays white
world = bpy.data.worlds.new("w")
world.color = (1, 1, 1)
scene.world = world
try:
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.display.shading.light = "STUDIO"
    scene.display.shading.color_type = "TEXTURE"
    scene.display.shading.background_type = "WORLD"
except Exception:
    pass


def shot(name, loc, rot, scale):
    cam = bpy.data.cameras.new(name)
    cam.type = "ORTHO"
    cam.ortho_scale = scale
    o = bpy.data.objects.new(name, cam)
    scene.collection.objects.link(o)
    o.location, o.rotation_euler = loc, [math.radians(a) for a in rot]
    scene.camera = o
    scene.render.filepath = os.path.join(OUT, name + ".png")
    bpy.ops.render.render(write_still=True)


big = max(size.x, size.z) * 1.08
mid = size.z / 2
shot("front", (0, -10, mid), (90, 0, 0), big)
shot("side", (10, 0, mid), (90, 0, 90), max(size.y, size.z) * 1.08)
hx = -half * 0.86  # the character's right hand is on screen left in the front view
hz = right * HEIGHT
shot("hand", (hx, -10, hz), (90, 0, 0), max(0.12, span * 0.2))

with open(os.path.join(OUT, "report.json"), "w") as f:
    json.dump(report, f, indent=1)
print("RIG_REPORT " + json.dumps(report))
