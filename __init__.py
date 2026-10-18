# SPDX-License-Identifier: GPL-3.0-or-later
"""
Lens FX — real-world lens character for Blender cameras.

Each camera stores its own lens settings (Camera Properties > Lens FX, or the
3D View sidebar > Lens FX tab). A "Lens FX" node group is inserted at the end of
the scene compositor and its inputs are driven by the *active scene camera*, so
switching cameras (including via timeline camera markers) switches the look, and
every setting can be keyframed.

Effects, in physical order:
  Lens:   bloom, veiling glare, lens flare (diffraction starburst computed from
          the camera's real aperture, source glow, stray-light veil), ghosts,
          streaks, lens dirt, edge softness, distortion, chromatic aberration,
          vignette (optical cos^4 falloff + shaped mechanical vignette)
  Film:   halation, grain
  Optics: real depth of field (f-stop, aperture blades, anamorphic bokeh ratio)

Flare sources are bright pixels in the render and/or scene light objects
(including Sun lamps, which never appear in the image). Light objects are
projected through the active camera and occlusion-tested against the depth
pass, so a flare dims as its source slips behind geometry.
"""

import math

import bpy
from bpy.app.handlers import persistent
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
)

from . import psf

GROUP_NAME = "Lens FX"
NODE_NAME = "Lens FX"
DIRT_IMAGE_NODE = "Lens FX Dirt Image"
KERNEL_IMAGE_NODE = "Lens FX Starburst Kernel"
OCCLUDER_NODE = "Lens FX Occluders"
GROUP_VERSION = 10

F = "NodeSocketFloat"
I = "NodeSocketInt"
B = "NodeSocketBool"
C = "NodeSocketColor"

# Node group interface: (name, socket type, default, min, max)
INPUTS = [
    ("Image", C, (0.0, 0.0, 0.0, 1.0), None, None),
    ("Mix", F, 1.0, 0.0, 1.0),
    ("Bloom Strength", F, 0.0, 0.0, 10.0),
    ("Bloom Threshold", F, 1.0, 0.0, 100.0),
    ("Bloom Size", F, 0.5, 0.0, 1.0),
    ("Bloom Tint", C, (1.0, 1.0, 1.0, 1.0), None, None),
    ("Veiling Glare", F, 0.0, 0.0, 1.0),
    # Camera
    ("F-Stop", F, 2.8, 0.1, 128.0),
    ("Lens Ratio", F, 50.0 / 36.0, 0.0, 1000.0),
    ("Shift X", F, 0.0, -10.0, 10.0),
    ("Shift Y", F, 0.0, -10.0, 10.0),
    ("Lens Tangent", F, 0.36, 0.0, 100.0),
    # Flare source
    ("Flare From Image", F, 1.0, 0.0, 1.0),
    ("Flare Threshold", F, 2.0, 0.0, 1000.0),
    ("Source Size", F, 0.004, 0.0, 1.0),
    ("Show Source", F, 1.0, 0.0, 1.0),
    ("Flare X", F, 0.3, -10.0, 10.0),
    ("Flare Y", F, 0.7, -10.0, 10.0),
    ("Flare Intensity", F, 0.0, 0.0, 1e6),
    ("Flare Color", C, (1.0, 1.0, 1.0, 1.0), None, None),
    ("Follow", F, 0.0, 0.0, 1.0),
    ("Follow X", F, 0.0, -1e9, 1e9),
    ("Follow Y", F, 0.0, -1e9, 1e9),
    ("Follow Z", F, 1.0, -1e9, 1e9),
    ("Visibility", F, 1.0, 0.0, 1.0),
    # Flare elements
    ("Flare Tint", C, (1.0, 1.0, 1.0, 1.0), None, None),
    ("Starburst Strength", F, 0.0, 0.0, 100.0),
    ("Starburst Size", F, 0.5, 0.0, 10.0),
    ("Starburst Kernel", C, (0.0, 0.0, 0.0, 1.0), None, None),
    ("Glow Strength", F, 0.0, 0.0, 100.0),
    ("Glow Size", F, 0.3, 0.0, 10.0),
    ("Glow Falloff", F, 1.0, 0.1, 10.0),
    ("Source Veil", F, 0.0, 0.0, 10.0),
    ("Ana Streak Strength", F, 0.0, 0.0, 100.0),
    ("Ana Streak Length", F, 1.0, 0.0, 10.0),
    ("Ana Streak Thickness", F, 0.3, 0.0, 10.0),
    ("Ana Streak Angle", F, 0.0, -math.pi, math.pi),
    ("Ana Streak Color", C, (0.35, 0.6, 1.0, 1.0), None, None),
    # Ghosts and hoop
    ("Ghost Strength", F, 0.0, 0.0, 100.0),
    ("Ghost Size", F, 0.3, 0.0, 10.0),
    ("Ghost Spread", F, 1.0, 0.0, 5.0),
    ("Ghost Aspect", F, 1.0, 0.2, 5.0),
    ("Ghost Blades", I, 6, 3, 24),
    ("Ghost Roundness", F, 0.2, 0.0, 1.0),
    ("Ghost Rotation", F, 0.2, -10.0, 10.0),
    ("Ghost Softness", F, 0.15, 0.001, 1.0),
    ("Ghost Distance Fade", F, 0.0, 0.0, 1.0),
    ("Ghost Texture", F, 0.4, 0.0, 5.0),
    ("Bead Strength", F, 0.0, 0.0, 100.0),
    ("Bead Count", F, 14.0, 1.0, 200.0),
    ("Bead Size", F, 1.0, 0.0, 20.0),
    ("Bead Seed", F, 0.0, -10000.0, 10000.0),
    ("Chroma", F, 0.3, 0.0, 5.0),
    ("Halo Strength", F, 0.0, 0.0, 100.0),
    ("Halo Radius", F, 0.35, 0.0, 5.0),
    ("Halo Thickness", F, 0.02, 0.0005, 1.0),
    ("Halo Position", F, 1.0, -5.0, 5.0),
    ("Halo Directional", F, 0.6, 0.0, 1.0),
    # Streaks
    ("Streak Strength", F, 0.0, 0.0, 10.0),
    ("Streak Threshold", F, 1.0, 0.0, 100.0),
    ("Streak Count", I, 2, 1, 16),
    ("Streak Angle", F, 0.0, -math.pi, math.pi),
    ("Streak Length", F, 0.9, 0.5, 1.0),
    ("Streak Tint", C, (1.0, 1.0, 1.0, 1.0), None, None),
    # Dirt
    ("Dirt Strength", F, 0.0, 0.0, 10.0),
    ("Dirt Density", F, 0.5, 0.0, 1.0),
    ("Dirt Size", F, 0.5, 0.0, 1.0),
    ("Dirt Variation", F, 0.7, 0.0, 1.0),
    ("Dirt Smudges", F, 0.3, 0.0, 1.0),
    ("Dirt Scale", F, 6.0, 0.1, 100.0),
    ("Dirt Seed", F, 0.0, -10000.0, 10000.0),
    ("Dirt Threshold", F, 0.8, 0.0, 100.0),
    ("Dirt Glow Size", F, 1.0, 0.0, 1.0),
    ("Dirt Texture", C, (0.0, 0.0, 0.0, 1.0), None, None),
    ("Dirt Texture Mix", F, 0.0, 0.0, 1.0),
    # Lens
    ("Edge Softness", F, 0.0, 0.0, 1.0),
    ("Distortion", F, 0.0, -0.999, 1.0),
    ("Dispersion", F, 0.0, 0.0, 1.0),
    ("Fit", B, True, None, None),
    ("Vignette", F, 0.0, 0.0, 1.0),
    ("Vignette Radius", F, 0.8, 0.0, 2.0),
    ("Vignette Softness", F, 0.8, 0.001, 3.0),
    ("Vignette Roundness", F, 0.5, 0.0, 1.0),
    ("Vignette Tint", C, (0.0, 0.0, 0.0, 1.0), None, None),
    ("Natural Falloff", F, 0.0, 0.0, 3.0),
    # Film
    ("Halation Strength", F, 0.0, 0.0, 10.0),
    ("Halation Threshold", F, 0.8, 0.0, 100.0),
    ("Halation Size", F, 0.4, 0.0, 1.0),
    ("Halation Tint", C, (1.0, 0.3, 0.1, 1.0), None, None),
    ("Grain Amount", F, 0.0, 0.0, 2.0),
    ("Grain Size", F, 1.0, 0.0, 10.0),
    ("Grain Chroma", F, 0.3, 0.0, 1.0),
    ("Grain Seed", F, 0.0, -100000.0, 100000.0),
]
INPUT_INDEX = {spec[0]: i for i, spec in enumerate(INPUTS)}


def _follow_drivers(cam_obj, target):
    """Camera-space position of the flare's follow target.

    Reads the camera and target objects directly so Blender tracks their
    transforms. Sun lamps use their +Z axis (direction toward the sun) pushed
    very far away; other objects use their world location.
    """
    if cam_obj is None or target is None:
        return []
    # RNA paths index matrices column first: matrix_world[col][row].
    rel = {"s": "fx:flare_follow_is_sun"}
    for name, path in (("lx", "[3][0]"), ("ly", "[3][1]"), ("lz", "[3][2]"),
                       ("zx", "[2][0]"), ("zy", "[2][1]"), ("zz", "[2][2]")):
        rel[name] = (target, f"matrix_world{path}")
    for name, path in (("cx", "[3][0]"), ("cy", "[3][1]"), ("cz", "[3][2]")):
        rel[name] = (cam_obj, f"matrix_world{path}")
    out = []
    for axis, col in (("X", 0), ("Y", 1), ("Z", 2)):
        vars_ = dict(rel)
        vars_.update({f"a{c}": (cam_obj, f"matrix_world[{col}][{row}]")
                      for row, c in enumerate("xyz")})
        expr = ("((1-s)*(lx-cx)+s*zx*10000)*ax + ((1-s)*(ly-cy)+s*zy*10000)*ay"
                " + ((1-s)*(lz-cz)+s*zz*10000)*az")
        out.append((f"Follow {axis}", expr, vars_))
    return out


# Group input -> driver: (input name, expression, {var: source}).
# Source: a lens_fx property name; "fx:<path>" a path under lens_fx;
# "cam.<prop>" a Camera data property; "scene:<path>" a Scene path;
# None for the current frame.
DRIVERS = [
    ("Mix", "v", {"v": "enabled"}),
    ("Bloom Strength", "v*e", {"v": "bloom_strength", "e": "bloom_enable"}),
    ("Bloom Threshold", "v", {"v": "bloom_threshold"}),
    ("Bloom Size", "v", {"v": "bloom_size"}),
    ("Bloom Tint", "v", {"v": "bloom_tint"}),
    ("Veiling Glare", "v*e", {"v": "veil_strength", "e": "veil_enable"}),
    ("F-Stop", "v", {"v": "cam.dof.aperture_fstop"}),
    ("Lens Ratio", "f/w", {"f": "cam.lens", "w": "cam.sensor_width"}),
    ("Shift X", "v", {"v": "cam.shift_x"}),
    ("Shift Y", "v", {"v": "cam.shift_y"}),
    ("Lens Tangent", "w/(2*f)", {"w": "cam.sensor_width", "f": "cam.lens"}),
    ("Flare From Image", "v*max(e,g)",
     {"v": "flare_use_image", "e": "flare_enable", "g": "ghost_enable"}),
    ("Flare Threshold", "v", {"v": "flare_threshold"}),
    ("Source Size", "v", {"v": "flare_source_size"}),
    ("Show Source", "v*e", {"v": "flare_show_source", "e": "flare_enable"}),
    ("Flare X", "v", {"v": "flare_position[0]"}),
    ("Flare Y", "v", {"v": "flare_position[1]"}),
    # The source feeds both the flare and the ghosts, so either one enables it.
    ("Flare Intensity", "v*max(e,g)",
     {"v": "flare_intensity", "e": "flare_enable", "g": "ghost_enable"}),
    ("Flare Color", "v", {"v": "flare_color"}),
    ("Follow", "v", {"v": "flare_follow_active"}),
    ("Flare Tint", "v", {"v": "flare_tint"}),
    ("Starburst Strength", "v*e", {"v": "starburst_strength", "e": "flare_enable"}),
    ("Starburst Size", "v", {"v": "starburst_size"}),
    ("Glow Strength", "v*e", {"v": "glow_strength", "e": "flare_enable"}),
    ("Glow Size", "v", {"v": "glow_size"}),
    ("Glow Falloff", "v", {"v": "glow_falloff"}),
    ("Source Veil", "v*e", {"v": "source_veil", "e": "flare_enable"}),
    ("Ana Streak Strength", "v*e", {"v": "ana_streak_strength", "e": "flare_enable"}),
    ("Ana Streak Length", "v", {"v": "ana_streak_length"}),
    ("Ana Streak Thickness", "v", {"v": "ana_streak_thickness"}),
    ("Ana Streak Angle", "v", {"v": "ana_streak_angle"}),
    ("Ana Streak Color", "v", {"v": "ana_streak_color"}),
    ("Visibility", "v", {"v": "flare_visibility"}),
    ("Ghost Strength", "v*e", {"v": "ghost_strength", "e": "ghost_enable"}),
    ("Ghost Size", "v", {"v": "ghost_size"}),
    ("Ghost Spread", "v", {"v": "ghost_spread"}),
    ("Ghost Aspect", "v", {"v": "ghost_aspect"}),
    ("Ghost Blades", "m*max(b,3) + (1-m)*g",
     {"m": "ghost_match_aperture", "b": "cam.dof.aperture_blades", "g": "ghost_blades"}),
    ("Ghost Roundness", "m*(b<3) + (1-m)*r",
     {"m": "ghost_match_aperture", "b": "cam.dof.aperture_blades", "r": "ghost_roundness"}),
    ("Ghost Rotation", "m*a + (1-m)*0.2",
     {"m": "ghost_match_aperture", "a": "cam.dof.aperture_rotation"}),
    ("Ghost Softness", "v", {"v": "ghost_softness"}),
    ("Ghost Distance Fade", "v", {"v": "ghost_distance_fade"}),
    ("Ghost Texture", "v", {"v": "ghost_texture"}),
    ("Bead Strength", "v", {"v": "bead_strength"}),
    ("Bead Count", "v", {"v": "bead_count"}),
    ("Bead Size", "v", {"v": "bead_size"}),
    ("Bead Seed", "v", {"v": "bead_seed"}),
    ("Chroma", "v", {"v": "ghost_chroma"}),
    ("Halo Strength", "v*e", {"v": "halo_strength", "e": "ghost_enable"}),
    ("Halo Radius", "v", {"v": "halo_radius"}),
    ("Halo Thickness", "v", {"v": "halo_thickness"}),
    ("Halo Position", "v", {"v": "halo_position"}),
    ("Halo Directional", "v", {"v": "halo_directional"}),
    ("Streak Strength", "v*e", {"v": "streak_strength", "e": "streak_enable"}),
    ("Streak Threshold", "v", {"v": "streak_threshold"}),
    ("Streak Count", "v", {"v": "streak_count"}),
    ("Streak Angle", "v", {"v": "streak_angle"}),
    ("Streak Length", "v", {"v": "streak_length"}),
    ("Streak Tint", "v", {"v": "streak_tint"}),
    ("Dirt Strength", "v*e", {"v": "dirt_strength", "e": "dirt_enable"}),
    ("Dirt Density", "v", {"v": "dirt_density"}),
    ("Dirt Size", "v", {"v": "dirt_size"}),
    ("Dirt Variation", "v", {"v": "dirt_variation"}),
    ("Dirt Smudges", "v", {"v": "dirt_smudges"}),
    ("Dirt Scale", "v", {"v": "dirt_scale"}),
    ("Dirt Seed", "v", {"v": "dirt_seed"}),
    ("Dirt Threshold", "v", {"v": "dirt_threshold"}),
    ("Dirt Glow Size", "v", {"v": "dirt_glow_size"}),
    ("Dirt Texture Mix", "v*u", {"v": "dirt_image_mix", "u": "dirt_use_image"}),
    ("Edge Softness", "v*e", {"v": "softness", "e": "soft_enable"}),
    ("Distortion", "v*e", {"v": "distortion", "e": "distort_enable"}),
    ("Dispersion", "v*e", {"v": "dispersion", "e": "distort_enable"}),
    ("Fit", "v", {"v": "fit"}),
    ("Vignette", "v*e", {"v": "vig_amount", "e": "vig_enable"}),
    ("Vignette Radius", "v", {"v": "vig_radius"}),
    ("Vignette Softness", "v", {"v": "vig_softness"}),
    ("Vignette Roundness", "v", {"v": "vig_roundness"}),
    ("Vignette Tint", "v", {"v": "vig_tint"}),
    ("Natural Falloff", "v*e", {"v": "vig_natural", "e": "vig_enable"}),
    ("Halation Strength", "v*e", {"v": "halation_strength", "e": "halation_enable"}),
    ("Halation Threshold", "v", {"v": "halation_threshold"}),
    ("Halation Size", "v", {"v": "halation_size"}),
    ("Halation Tint", "v", {"v": "halation_tint"}),
    ("Grain Amount", "v*e", {"v": "grain_amount", "e": "grain_enable"}),
    ("Grain Size", "v", {"v": "grain_size"}),
    ("Grain Chroma", "v", {"v": "grain_chroma"}),
    ("Grain Seed", "f*a + s", {"a": "grain_animated", "s": "grain_seed", "f": None}),
]

# Ghosts: (step t along the axis from the source (0) through the center (1)
# to the mirrored position (2), relative size, opacity, coating tint, rim
# (0 = rim-lit disc, 1 = thin ring), softness multiplier). Small warm ghosts
# near the source, pink rings through the middle, large violet and blue
# hazes past the center, as in a multi-element prime.
GHOSTS = [
    (0.20, 0.22, 0.55, (1.00, 0.60, 0.30), 0.2, 1.0),
    (0.45, 0.70, 0.16, (1.00, 0.45, 0.65), 0.7, 0.8),
    (0.62, 0.28, 0.45, (1.00, 0.50, 0.85), 0.1, 1.0),
    (0.90, 1.10, 0.16, (0.85, 0.45, 1.00), 0.3, 2.5),
    (1.25, 0.45, 0.32, (0.65, 0.50, 1.00), 0.8, 0.6),
    (1.60, 1.90, 0.15, (0.35, 0.45, 1.00), 0.1, 4.0),
    (1.85, 0.60, 0.22, (0.50, 0.80, 1.00), 0.5, 1.0),
    (2.15, 1.20, 0.12, (0.70, 0.40, 1.00), 0.4, 1.6),
]

# Peak brightness of the flare source at Intensity 1 (scene-linear). Real light
# sources are thousands of times brighter than diffuse white.
SOURCE_PEAK = 2000.0


# -----------------------------------------------------------------------------
# Node group construction
# -----------------------------------------------------------------------------

class _Builder:
    def __init__(self, tree):
        self.tree = tree
        self.nodes = tree.nodes
        self.links = tree.links

    def node(self, idname, loc, label=None, **props):
        n = self.nodes.new(idname)
        n.location = loc
        if label:
            n.label = label
        for k, v in props.items():
            setattr(n, k, v)
        return n

    def link(self, out_socket, in_socket):
        self.links.new(out_socket, in_socket)

    def set(self, socket, value):
        if isinstance(value, bpy.types.NodeSocket):
            self.link(value, socket)
        else:
            socket.default_value = value

    def math(self, op, loc, a=None, b=None, c=None):
        n = self.node("ShaderNodeMath", loc, operation=op)
        for i, v in enumerate((a, b, c)):
            if v is not None:
                self.set(n.inputs[i], v)
        return n.outputs[0]

    def mix_color(self, blend, loc, fac, a, b, label=None):
        n = self.node("ShaderNodeMix", loc, label=label, data_type="RGBA",
                      blend_type=blend, clamp_result=False)
        self.set(n.inputs[0], fac)
        self.set(n.inputs[6], a)
        self.set(n.inputs[7], b)
        return n.outputs[2]

    def mix_float(self, loc, fac, a, b):
        n = self.node("ShaderNodeMix", loc, data_type="FLOAT", clamp_result=False)
        self.set(n.inputs[0], fac)
        self.set(n.inputs[2], a)
        self.set(n.inputs[3], b)
        return n.outputs[0]

    def combine(self, loc, x, y, z=0.0):
        n = self.node("ShaderNodeCombineXYZ", loc)
        self.set(n.inputs[0], x)
        self.set(n.inputs[1], y)
        self.set(n.inputs[2], z)
        return n.outputs[0]

    def scale(self, loc, image, x, y, label=None):
        n = self.node("CompositorNodeScale", loc, label=label)
        self.link(image, n.inputs["Image"])
        n.inputs["Type"].default_value = "Relative"
        self.set(n.inputs["X"], x)
        self.set(n.inputs["Y"], y)
        return n.outputs[0]

    def blur(self, loc, image, size, label=None, extend=False):
        n = self.node("CompositorNodeBlur", loc, label=label)
        self.link(image, n.inputs["Image"])
        self.link(self.combine((loc[0] - 150, loc[1] - 150), size, size), n.inputs["Size"])
        n.inputs["Type"].default_value = "Gaussian"
        n.inputs["Extend Bounds"].default_value = extend
        return n.outputs[0]

    def remap(self, loc, image, u, v, label=None):
        """Resample `image` onto the frame's own grid at normalized (u, v).

        Unlike Scale/Rotate nodes this never changes the output's size, so the
        result always lines up with the camera frame.
        """
        n = self.node("CompositorNodeMapUV", loc, label=label)
        self.link(image, n.inputs["Image"])
        self.link(self.combine((loc[0] - 150, loc[1] - 100), u, v, 1.0), n.inputs["UV"])
        return n.outputs[0]

    def sample(self, loc, image, u, v):
        """Sample `image` at normalized (u, v); the result is a single value."""
        n = self.node("CompositorNodeMapUV", loc)
        self.link(image, n.inputs["Image"])
        # MapUV treats the third UV component as a mask, so it must be 1.
        self.link(self.combine((loc[0] - 150, loc[1] - 100), u, v, 1.0), n.inputs["UV"])
        n.inputs["Extension X"].default_value = "Extend"
        n.inputs["Extension Y"].default_value = "Extend"
        n.inputs["Interpolation"].default_value = "Nearest"
        sep = self.node("CompositorNodeSeparateColor", (loc[0] + 150, loc[1]))
        self.link(n.outputs[0], sep.inputs[0])
        return sep.outputs[0]

    def glare(self, glare_type, loc, label, image, output="Glare", **inputs):
        n = self.node("CompositorNodeGlare", loc, label=label)
        self.link(image, n.inputs["Image"])
        n.inputs["Type"].default_value = glare_type
        n.inputs["Quality"].default_value = "Medium"
        for name, v in inputs.items():
            self.set(n.inputs[name.replace("_", " ")], v)
        return n.outputs[output]


def build_group():
    """Create (or rebuild in place) the Lens FX compositor node group."""
    ng = bpy.data.node_groups.get(GROUP_NAME)
    if ng is not None and ng.bl_idname == "CompositorNodeTree" \
            and ng.get("lens_fx_version") == GROUP_VERSION:
        return ng
    if ng is None or ng.bl_idname != "CompositorNodeTree":
        ng = bpy.data.node_groups.new(GROUP_NAME, "CompositorNodeTree")
    ng.nodes.clear()
    ng.interface.clear()

    for name, stype, default, lo, hi in INPUTS:
        item = ng.interface.new_socket(name=name, in_out="INPUT", socket_type=stype)
        item.default_value = default
        if lo is not None:
            item.min_value = lo
            item.max_value = hi
        if name == "Streak Angle":
            item.subtype = "ANGLE"
    ng.interface.new_socket(name="Image", in_out="OUTPUT", socket_type=C)

    b = _Builder(ng)
    gi = b.node("NodeGroupInput", (-4200, 0))
    go = b.node("NodeGroupOutput", (3600, 0))
    inp = gi.outputs
    img = inp["Image"]

    # --- Frame geometry ------------------------------------------------------
    coords = b.node("CompositorNodeImageCoordinates", (-3900, -2400))
    b.link(img, coords.inputs["Image"])
    info = b.node("CompositorNodeImageInfo", (-3900, -2650))
    b.link(img, info.inputs["Image"])
    dims = b.node("ShaderNodeSeparateXYZ", (-3700, -2650))
    b.link(info.outputs["Dimensions"], dims.inputs[0])
    width, height = dims.outputs["X"], dims.outputs["Y"]
    aspect = b.math("DIVIDE", (-3500, -2650), width, height)
    fit_dim = b.math("MAXIMUM", (-3500, -2800), width, height)

    pix = b.node("ShaderNodeSeparateXYZ", (-3700, -2300))
    b.link(coords.outputs["Pixel"], pix.inputs[0])
    uv = b.node("ShaderNodeSeparateXYZ", (-3700, -2450))
    b.link(coords.outputs["Normalized"], uv.inputs[0])
    cx = b.math("MULTIPLY_ADD", (-3500, -2400), uv.outputs["X"], 2.0, -1.0)
    cy = b.math("MULTIPLY_ADD", (-3500, -2500), uv.outputs["Y"], 2.0, -1.0)
    # Physical (circular) coordinates: x scaled by aspect, height half = 1.
    px = b.math("MULTIPLY", (-3300, -2400), cx, aspect)
    d2 = b.math("ADD", (-3100, -2450),
                b.math("MULTIPLY", (-3300, -2300), px, px),
                b.math("MULTIPLY", (-3300, -2500), cy, cy))
    dist = b.math("ADD", (-2900, -2450), b.math("SQRT", (-3000, -2550), d2), 1e-4)
    dir_x = b.math("DIVIDE", (-2700, -2400), px, dist)
    dir_y = b.math("DIVIDE", (-2700, -2500), cy, dist)

    # Shaped radius for vignette/softness: roundness 0 follows the frame, 1 is a
    # circle. Normalized so the frame corners are always r == 1.
    asp_m1 = b.math("SUBTRACT", (-3300, -2700), aspect, 1.0)
    s = b.math("MULTIPLY_ADD", (-3100, -2700), asp_m1, inp["Vignette Roundness"], 1.0)
    sx = b.math("MULTIPLY", (-2900, -2700), cx, s)
    r2_num = b.math("ADD", (-2700, -2700),
                    b.math("MULTIPLY", (-2800, -2650), sx, sx),
                    b.math("MULTIPLY", (-2800, -2800), cy, cy))
    r2 = b.math("DIVIDE", (-2500, -2700), r2_num,
                b.math("MULTIPLY_ADD", (-2700, -2850), s, s, 1.0))
    r2_round = b.math("DIVIDE", (-2500, -2550), d2,
                      b.math("MULTIPLY_ADD", (-2700, -2600), aspect, aspect, 1.0))

    # --- Scene glare: bloom, veiling glare ---------------------------------------
    bloom = b.glare("Bloom", (-3600, 1300), "Bloom", img,
                    Threshold=inp["Bloom Threshold"], Strength=inp["Bloom Strength"],
                    Size=inp["Bloom Size"], Tint=inp["Bloom Tint"])
    veil = b.glare("Fog Glow", (-3600, 1000), "Veiling Glare", img,
                   Threshold=0.0, Smoothness=1.0, Strength=inp["Veiling Glare"], Size=1.0)

    # --- Flare (overlay light source) ------------------------------------------
    # Every element is computed analytically on the frame's own pixel grid, in
    # "height units": origin at the frame center, frame height = 1. All
    # elements line up on the optical axis from the source L through the
    # center C; an element at step t sits at L + t * (C - L).
    x0, y0 = -3500, 700
    pxc = b.math("DIVIDE", (x0 + 150, y0 + 900),
                 b.math("SUBTRACT", (x0, y0 + 900), pix.outputs["X"],
                        b.math("MULTIPLY", (x0 - 150, y0 + 900), width, 0.5)), height)
    pyc = b.math("DIVIDE", (x0 + 150, y0 + 800),
                 b.math("SUBTRACT", (x0, y0 + 800), pix.outputs["Y"],
                        b.math("MULTIPLY", (x0 - 150, y0 + 800), height, 0.5)), height)

    # Source position in normalized frame coordinates (u, v): placed by hand, or
    # following an object projected through the camera (camera looks down -Z).
    proj = b.math("MULTIPLY", (x0, y0 + 600), inp["Lens Ratio"], fit_dim)
    shift_u = b.math("DIVIDE", (x0 + 150, y0 + 500),
                     b.math("MULTIPLY", (x0, y0 + 500), inp["Shift X"], fit_dim), width)
    shift_v = b.math("DIVIDE", (x0 + 150, y0 + 400),
                     b.math("MULTIPLY", (x0, y0 + 400), inp["Shift Y"], fit_dim), height)
    fx_, fy_, fz_ = inp["Follow X"], inp["Follow Y"], inp["Follow Z"]
    front = b.math("LESS_THAN", (x0, y0), fz_, -1e-4)
    depth = b.math("MULTIPLY", (x0, y0 - 100),
                   b.math("MINIMUM", (x0 - 100, y0 - 100), fz_, -1e-4), -1.0)
    inv_depth = b.math("DIVIDE", (x0 + 150, y0 - 100), 1.0, depth)
    fu = b.math("ADD", (x0 + 450, y0),
                b.math("MULTIPLY_ADD", (x0 + 300, y0),
                       b.math("MULTIPLY", (x0 + 150, y0), fx_, inv_depth),
                       b.math("DIVIDE", (x0 + 150, y0 + 100), proj, width), 0.5),
                shift_u)
    fv = b.math("ADD", (x0 + 450, y0 - 200),
                b.math("MULTIPLY_ADD", (x0 + 300, y0 - 200),
                       b.math("MULTIPLY", (x0 + 150, y0 - 200), fy_, inv_depth),
                       b.math("DIVIDE", (x0 + 150, y0 - 300), proj, height), 0.5),
                shift_v)
    u = b.mix_float((x0 + 600, y0), inp["Follow"], inp["Flare X"], fu)
    v = b.mix_float((x0 + 600, y0 - 200), inp["Follow"], inp["Flare Y"], fv)
    lx = b.math("MULTIPLY", (x0 + 750, y0), b.math("SUBTRACT", (x0 + 700, y0 + 60), u, 0.5), aspect)
    ly = b.math("SUBTRACT", (x0 + 750, y0 - 200), v, 0.5)

    # Occlusion: rays traced from the camera across the source disc against
    # the occluder objects (see update_visibility) give a 0-1 visibility.
    intensity = b.math("MULTIPLY", (x0 + 1600, y0 - 300),
                       b.math("MULTIPLY", (x0 + 1450, y0 - 300), inp["Flare Intensity"],
                              b.mix_float((x0 + 1300, y0 - 300), inp["Follow"], 1.0, front)),
                       inp["Visibility"])

    def sq(loc, a):
        return b.math("MULTIPLY", loc, a, a)

    dx = b.math("SUBTRACT", (x0 + 900, y0 + 900), pxc, lx)
    dy = b.math("SUBTRACT", (x0 + 900, y0 + 800), pyc, ly)
    d2 = b.math("ADD", (x0 + 1150, y0 + 850), sq((x0 + 1050, y0 + 900), dx),
                sq((x0 + 1050, y0 + 800), dy))

    # Source disc (the light itself).
    rs = b.math("MAXIMUM", (x0 + 1000, y0 + 1100),
                b.math("MULTIPLY", (x0 + 850, y0 + 1100), inp["Source Size"], aspect),
                b.math("DIVIDE", (x0 + 850, y0 + 1000), 0.75, height))
    disc = b.math("EXPONENT", (x0 + 1450, y0 + 1050),
                  b.math("DIVIDE", (x0 + 1300, y0 + 1050),
                         b.math("MULTIPLY", (x0 + 1150, y0 + 1050), d2, -1.0),
                         sq((x0 + 1150, y0 + 1150), rs)))
    disc = b.math("MULTIPLY", (x0 + 1600, y0 + 1050), disc,
                  b.math("MULTIPLY", (x0 + 1450, y0 + 1150), intensity, SOURCE_PEAK))
    dots = b.mix_color("MULTIPLY", (x0 + 1750, y0 + 1050), 1.0, inp["Flare Color"], disc,
                       "Source Disc")

    # Glow: radial falloff around the source, (1 + d²/r²)^-falloff.
    rg = b.math("MAXIMUM", (x0 + 1000, y0 + 1350),
                b.math("MULTIPLY", (x0 + 850, y0 + 1350), inp["Glow Size"], 0.5), 1e-4)
    glow = b.math("POWER", (x0 + 1450, y0 + 1300),
                  b.math("ADD", (x0 + 1300, y0 + 1300), 1.0,
                         b.math("DIVIDE", (x0 + 1150, y0 + 1300), d2, sq((x0 + 1150, y0 + 1400), rg))),
                  b.math("MULTIPLY", (x0 + 1300, y0 + 1400), inp["Glow Falloff"], -1.0))
    glow = b.math("MULTIPLY", (x0 + 1600, y0 + 1300), glow, inp["Glow Strength"])

    # Stray light: broad wash around the source, reaching in from off-screen.
    vs = b.math("MULTIPLY", (x0 + 850, y0 + 1550),
                b.math("MAXIMUM", (x0 + 700, y0 + 1550), aspect, 1.0), 0.45)
    wash = b.math("EXPONENT", (x0 + 1450, y0 + 1550),
                  b.math("DIVIDE", (x0 + 1300, y0 + 1550),
                         b.math("MULTIPLY", (x0 + 1150, y0 + 1550), d2, -1.0),
                         sq((x0 + 1150, y0 + 1650), vs)))
    wash = b.math("MULTIPLY", (x0 + 1600, y0 + 1550), wash, inp["Source Veil"])
    soft_light = b.math("MULTIPLY", (x0 + 1800, y0 + 1400),
                        b.math("ADD", (x0 + 1700, y0 + 1400), glow, wash), intensity)
    soft_light = b.mix_color("MULTIPLY", (x0 + 1950, y0 + 1400), 1.0, inp["Flare Color"],
                             soft_light, "Glow + Stray Light")

    # Anamorphic streak: the source stretched into a thin line (exponential
    # falloff along it, Gaussian across it) plus a softer, wider body.
    ca = b.math("COSINE", (x0 + 900, y0 + 1900), inp["Ana Streak Angle"])
    sa = b.math("SINE", (x0 + 900, y0 + 1800), inp["Ana Streak Angle"])
    along = b.math("ABSOLUTE", (x0 + 1150, y0 + 1900),
                   b.math("ADD", (x0 + 1100, y0 + 1950), b.math("MULTIPLY", (x0 + 1000, y0 + 2000), dx, ca),
                          b.math("MULTIPLY", (x0 + 1000, y0 + 1950), dy, sa)))
    across = b.math("SUBTRACT", (x0 + 1100, y0 + 1800), b.math("MULTIPLY", (x0 + 1000, y0 + 1850), dy, ca),
                    b.math("MULTIPLY", (x0 + 1000, y0 + 1800), dx, sa))
    th = b.math("MAXIMUM", (x0 + 1000, y0 + 1700),
                b.math("MULTIPLY", (x0 + 900, y0 + 1700), inp["Ana Streak Thickness"], 0.01), 1e-4)
    ln = b.math("MAXIMUM", (x0 + 1000, y0 + 1650),
                b.math("MULTIPLY", (x0 + 900, y0 + 1650),
                       b.math("MULTIPLY", (x0 + 800, y0 + 1650), inp["Ana Streak Length"], aspect), 0.5),
                1e-4)

    def streak_term(loc, th_mul, ln_mul, weight):
        xx, yy = loc
        a_ = b.math("EXPONENT", (xx + 300, yy),
                    b.math("MULTIPLY", (xx + 150, yy),
                           sq((xx, yy), b.math("DIVIDE", (xx - 100, yy),
                                                 across, b.math("MULTIPLY", (xx - 200, yy), th, th_mul))),
                           -1.0))
        b_ = b.math("EXPONENT", (xx + 300, yy - 100),
                    b.math("DIVIDE", (xx + 150, yy - 100), b.math("MULTIPLY", (xx, yy - 100), along, -1.0),
                           b.math("MULTIPLY", (xx - 100, yy - 100), ln, ln_mul)))
        return b.math("MULTIPLY", (xx + 450, yy), b.math("MULTIPLY", (xx + 400, yy - 50), a_, b_), weight)

    streak = b.math("ADD", (x0 + 1950, y0 + 1800),
                    streak_term((x0 + 1300, y0 + 1900), 1.0, 1.0, 1.0),
                    streak_term((x0 + 1300, y0 + 1700), 6.0, 0.5, 0.25))
    streak = b.math("MULTIPLY", (x0 + 2100, y0 + 1800), streak,
                    b.math("MULTIPLY", (x0 + 2000, y0 + 1700), intensity, inp["Ana Streak Strength"]))
    streak = b.mix_color("MULTIPLY", (x0 + 2250, y0 + 1800), 1.0, inp["Ana Streak Color"], streak,
                         "Anamorphic Streak")

    # Starburst: physically based diffraction. The kernel is the spectral
    # diffraction pattern of the camera's aperture (see psf.py); its on-screen
    # size and brightness scale with the f-number, as in a real lens.
    highlights = b.glare("Bloom", (-1400, 1100), "Image Highlights", img, output="Highlights",
                         Threshold=inp["Flare Threshold"], Strength=0.0, Size=0.0)
    src = b.mix_color("ADD", (-1100, 900), inp["Flare From Image"], dots, highlights,
                      "Starburst Sources")
    n8 = b.math("DIVIDE", (-1100, 600), inp["F-Stop"], 8.0)
    k_scale = b.math("MAXIMUM", (-800, 600),
                     b.math("DIVIDE", (-950, 600),
                            b.math("MULTIPLY", (-1100, 500),
                                   b.math("MULTIPLY", (-1250, 500), inp["Starburst Size"], width),
                                   n8),
                            float(psf.KERNEL_RES)), 0.01)
    kernel = b.scale((-650, 600), inp["Starburst Kernel"], k_scale, k_scale, "Kernel Size")
    burst = b.node("CompositorNodeGlare", (-450, 900), label="Diffraction Starburst")
    b.link(src, burst.inputs["Image"])
    burst.inputs["Type"].default_value = "Kernel"
    burst.inputs["Kernel Data Type"].default_value = "Color"
    burst.inputs["Threshold"].default_value = 0.0
    burst.inputs["Smoothness"].default_value = 0.0
    b.link(kernel, [s for s in burst.inputs if s.name == "Kernel" and s.type == "RGBA"][0])
    b.link(b.math("MULTIPLY", (-650, 450), inp["Starburst Strength"], n8), burst.inputs["Strength"])
    # Bright pixels also get a glow of their own.
    img_glow = b.glare("Fog Glow", (-450, 600), "Bright Pixel Glow", highlights,
                       Threshold=0.0, Smoothness=0.0,
                       Strength=b.math("MULTIPLY", (-650, 300), inp["Glow Strength"],
                                       b.math("MULTIPLY", (-800, 300), inp["Flare From Image"], 0.05)),
                       Size=0.6)
    flare = b.mix_color("ADD", (-200, 800), 1.0, burst.outputs["Glare"], img_glow)
    flare = b.mix_color("ADD", (-50, 800), 1.0, flare, soft_light)
    flare = b.mix_color("ADD", (100, 800), 1.0, flare, streak)
    flare = b.mix_color("MULTIPLY", (250, 800), 1.0, flare, inp["Flare Tint"], "Flare Tint")
    flare = b.mix_color("ADD", (400, 800), inp["Show Source"], flare, dots, "+ Source Disc")

    # --- Ghosts and hoop ---------------------------------------------------------
    # Ghosts are defocused images of the aperture: polygons with the camera's
    # blade count, shrinking as you stop down. Chromatic aberration scales red
    # outward and blue inward from the center, splitting each element's fringe.
    gx0, gy0 = -2300, -300
    stop_scale = b.math("MINIMUM", (gx0 - 300, gy0),
                        b.math("MAXIMUM", (gx0 - 450, gy0),
                               b.math("DIVIDE", (gx0 - 600, gy0), 4.0, inp["F-Stop"]), 0.3), 2.0)
    blades = inp["Ghost Blades"]
    sector = b.math("DIVIDE", (gx0 - 450, gy0 - 150), 2.0 * math.pi, blades)
    half_sector = b.math("DIVIDE", (gx0 - 450, gy0 - 250), math.pi, blades)
    cos_half = b.math("COSINE", (gx0 - 300, gy0 - 250), half_sector)
    l_len = b.math("SQRT", (gx0 - 450, gy0 - 400),
                   b.math("ADD", (gx0 - 600, gy0 - 400), sq((gx0 - 750, gy0 - 350), lx),
                          sq((gx0 - 750, gy0 - 450), ly)))
    fade = b.math("SUBTRACT", (gx0 - 150, gy0 - 400), 1.0,
                  b.math("MULTIPLY", (gx0 - 300, gy0 - 400), inp["Ghost Distance Fade"],
                         b.math("MINIMUM", (gx0 - 300, gy0 - 500),
                                b.math("DIVIDE", (gx0 - 450, gy0 - 500), l_len, 0.7), 1.0)))
    # Gain chosen so Strength 1 reads clearly over a daylight sky.
    ghost_gain = b.math("MULTIPLY", (gx0, gy0 - 400),
                        b.math("MULTIPLY", (gx0 - 150, gy0 - 300), intensity,
                               b.math("MULTIPLY", (gx0 - 300, gy0 - 300), inp["Ghost Strength"], 5.0)),
                        fade)
    inv_ga = b.math("DIVIDE", (gx0 - 450, gy0 - 600), 1.0, inp["Ghost Aspect"])
    chroma_pos = b.math("MULTIPLY", (gx0 - 450, gy0 - 700), inp["Chroma"], 0.06)
    chroma_rad = b.math("MULTIPLY", (gx0 - 450, gy0 - 800), inp["Chroma"], 0.04)
    soft_lo = b.math("SUBTRACT", (gx0 - 450, gy0 - 900), 1.0, inp["Ghost Softness"])

    def element_center(loc, t, spread=True):
        """Center of an element at step t along the axis: (1 - t) * L."""
        xx, yy = loc
        k = b.math("SUBTRACT", (xx, yy), 1.0, t) if isinstance(t, bpy.types.NodeSocket) else 1.0 - t
        if spread:
            k = b.math("MULTIPLY", (xx + 100, yy), k, inp["Ghost Spread"])
        return (b.math("MULTIPLY", (xx + 200, yy), lx, k),
                b.math("MULTIPLY", (xx + 200, yy - 60), ly, k))

    ghosts = None
    for i, (t, size, opacity, tint, rim, soft_mul) in enumerate(GHOSTS):
        yy = gy0 - 1100 - i * 1000
        xx = gx0
        cxg, cyg = element_center((xx, yy), t)
        radius = b.math("MULTIPLY", (xx + 300, yy - 150),
                        b.math("MULTIPLY", (xx + 150, yy - 150), inp["Ghost Size"], size * 0.5),
                        stop_scale)
        g_soft_lo = b.math("SUBTRACT", (xx + 300, yy - 250), 1.0,
                           b.math("MINIMUM", (xx + 150, yy - 250),
                                  b.math("MULTIPLY", (xx, yy - 250), inp["Ghost Softness"], soft_mul),
                                  0.999))
        # Polygon edge factor from the angle around the ghost center (shared by
        # all channels): cos(pi/n) / cos(a), blended to 1 by roundness.
        gdx = b.math("MULTIPLY", (xx + 450, yy), b.math("SUBTRACT", (xx + 350, yy), pxc, cxg), inv_ga)
        gdy = b.math("SUBTRACT", (xx + 450, yy - 60), pyc, cyg)
        theta = b.math("SUBTRACT", (xx + 650, yy),
                       b.math("ARCTAN2", (xx + 550, yy), gdy, gdx), inp["Ghost Rotation"])
        a_ = b.math("SUBTRACT", (xx + 850, yy),
                    b.math("FLOORED_MODULO", (xx + 750, yy), theta, sector), half_sector)
        edge = b.math("DIVIDE", (xx + 1000, yy), cos_half, b.math("COSINE", (xx + 900, yy - 60), a_))
        edge = b.math("ADD", (xx + 1150, yy), edge,
                      b.math("MULTIPLY", (xx + 1100, yy - 80),
                             b.math("SUBTRACT", (xx + 1000, yy - 80), 1.0, edge), inp["Ghost Roundness"]))
        # Refraction texture: uneven coatings and dust inside the element,
        # pinned to the ghost so it travels with it.
        inv_rad = b.math("DIVIDE", (xx + 450, yy + 150), 2.5,
                         b.math("MAXIMUM", (xx + 350, yy + 150), radius, 1e-4))
        noise = b.node("ShaderNodeTexNoise", (xx + 700, yy + 200), noise_dimensions="3D")
        b.link(b.combine((xx + 550, yy + 200), b.math("MULTIPLY", (xx + 500, yy + 260), gdx, inv_rad),
                         b.math("MULTIPLY", (xx + 500, yy + 160), gdy, inv_rad), float(i) * 7.31),
               noise.inputs["Vector"])
        noise.inputs["Scale"].default_value = 1.6
        noise.inputs["Detail"].default_value = 5.0
        noise.inputs["Roughness"].default_value = 0.6
        texture = b.math("MAXIMUM", (xx + 1050, yy + 200),
                         b.math("MULTIPLY_ADD", (xx + 950, yy + 200),
                                b.math("SUBTRACT", (xx + 850, yy + 250), noise.outputs["Factor"], 0.5),
                                b.math("MULTIPLY", (xx + 850, yy + 150), inp["Ghost Texture"], 2.5), 1.0),
                         0.0)
        chans = []
        for ci, kc in enumerate((1.0, 0.0, -1.0)):
            cy_ = yy - 300 - ci * 220
            if kc == 0.0:
                ccx, ccy, rad = cxg, cyg, radius
            else:
                pos_k = b.math("MULTIPLY_ADD", (xx + 250, cy_), chroma_pos, kc, 1.0)
                rad_k = b.math("MULTIPLY_ADD", (xx + 250, cy_ - 60), chroma_rad, kc, 1.0)
                ccx = b.math("MULTIPLY", (xx + 400, cy_), cxg, pos_k)
                ccy = b.math("MULTIPLY", (xx + 400, cy_ - 60), cyg, pos_k)
                rad = b.math("MULTIPLY", (xx + 400, cy_ - 120), radius, rad_k)
            ddx = b.math("MULTIPLY", (xx + 650, cy_), b.math("SUBTRACT", (xx + 550, cy_), pxc, ccx), inv_ga)
            ddy = b.math("SUBTRACT", (xx + 650, cy_ - 60), pyc, ccy)
            r_ = b.math("SQRT", (xx + 900, cy_),
                        b.math("ADD", (xx + 800, cy_), sq((xx + 750, cy_), ddx), sq((xx + 750, cy_ - 60), ddy)))
            q = b.math("DIVIDE", (xx + 1100, cy_), r_,
                       b.math("MAXIMUM", (xx + 1000, cy_ - 60), b.math("MULTIPLY", (xx + 950, cy_ - 100), rad, edge), 1e-5))
            mr = b.node("ShaderNodeMapRange", (xx + 1250, cy_), interpolation_type="SMOOTHSTEP")
            b.link(q, mr.inputs[0])
            b.link(g_soft_lo, mr.inputs[1])
            mr.inputs[2].default_value = 1.0
            mr.inputs[3].default_value = 1.0
            mr.inputs[4].default_value = 0.0
            # Profile: a gently rim-lit disc (rim 0) through to a thin ring (rim 1).
            qc = b.math("MINIMUM", (xx + 1250, cy_ - 120), q, 1.0)
            profile = b.mix_float((xx + 1400, cy_ - 100), rim,
                                  b.math("MULTIPLY_ADD", (xx + 1300, cy_ - 160), qc, 0.45, 0.55),
                                  b.math("POWER", (xx + 1300, cy_ - 220), qc, 6.0))
            shape = b.math("MULTIPLY", (xx + 1550, cy_),
                           b.math("MULTIPLY", (xx + 1450, cy_), mr.outputs[0], profile), texture)
            chans.append(shape)
        rgb = b.node("CompositorNodeCombineColor", (xx + 1700, yy - 300))
        for ci in range(3):
            b.link(chans[ci], rgb.inputs[ci])
        g = b.mix_color("MULTIPLY", (xx + 1850, yy - 300), 1.0, rgb.outputs[0],
                        (*(c * opacity for c in tint), 1.0), f"Ghost {i + 1}")
        ghosts = g if ghosts is None else b.mix_color("ADD", (xx + 2000, yy - 300), 1.0, ghosts, g)

    # Bead chain: many tiny bright reflections strung along the axis. One
    # pattern covers any number of beads: the axis is split into cells, and a
    # random value per cell decides whether it holds a bead and how big it is.
    bx0, by0 = gx0, gy0 - 1100 - len(GHOSTS) * 1000
    ax_len = b.math("MAXIMUM", (bx0, by0), l_len, 1e-3)
    adx = b.math("DIVIDE", (bx0 + 150, by0), b.math("MULTIPLY", (bx0, by0 + 60), lx, -1.0), ax_len)
    ady = b.math("DIVIDE", (bx0 + 150, by0 - 60), b.math("MULTIPLY", (bx0, by0 - 60), ly, -1.0), ax_len)
    t_axis = b.math("DIVIDE", (bx0 + 450, by0),
                    b.math("ADD", (bx0 + 350, by0), b.math("MULTIPLY", (bx0 + 250, by0 + 40), dx, adx),
                           b.math("MULTIPLY", (bx0 + 250, by0 - 40), dy, ady)), ax_len)
    perp = b.math("SUBTRACT", (bx0 + 350, by0 - 150), b.math("MULTIPLY", (bx0 + 250, by0 - 110), dx, ady),
                  b.math("MULTIPLY", (bx0 + 250, by0 - 190), dy, adx))
    cells = b.math("MULTIPLY", (bx0 + 600, by0), t_axis, inp["Bead Count"])
    cell = b.math("FLOOR", (bx0 + 750, by0), cells)
    rnd = b.node("ShaderNodeTexWhiteNoise", (bx0 + 900, by0), noise_dimensions="2D")
    b.link(b.combine((bx0 + 800, by0 - 100), cell, inp["Bead Seed"]), rnd.inputs["Vector"])
    rsep = b.node("CompositorNodeSeparateColor", (bx0 + 1050, by0 - 100))
    b.link(rnd.outputs["Color"], rsep.inputs[0])
    # Bead center jitters within its cell; distance measured in frame units.
    along = b.math("MULTIPLY", (bx0 + 1300, by0),
                   b.math("SUBTRACT", (bx0 + 1150, by0),
                          b.math("FRACT", (bx0 + 900, by0 + 100), cells),
                          b.math("MULTIPLY_ADD", (bx0 + 1050, by0 + 100), rsep.outputs[1], 0.6, 0.2)),
                   b.math("DIVIDE", (bx0 + 1150, by0 + 150), ax_len, inp["Bead Count"]))
    bead_r = b.math("MULTIPLY", (bx0 + 1300, by0 - 300),
                    b.math("MULTIPLY", (bx0 + 1150, by0 - 300), inp["Bead Size"], 0.012),
                    b.math("MULTIPLY_ADD", (bx0 + 1150, by0 - 400), rsep.outputs[2], 1.2, 0.3))
    bead = b.math("EXPONENT", (bx0 + 1700, by0),
                  b.math("DIVIDE", (bx0 + 1550, by0),
                         b.math("MULTIPLY", (bx0 + 1450, by0),
                                b.math("ADD", (bx0 + 1400, by0 + 60), sq((bx0 + 1350, by0 + 120), along),
                                       sq((bx0 + 1350, by0 - 60), perp)), -1.0),
                         sq((bx0 + 1450, by0 - 300), bead_r)))
    alive = b.math("LESS_THAN", (bx0 + 1200, by0 - 200), rsep.outputs[0], 0.55)
    span = b.math("MULTIPLY", (bx0 + 1700, by0 - 200),
                  b.math("SMOOTH_MIN", (bx0 + 1550, by0 - 200),
                         b.math("MULTIPLY", (bx0 + 1400, by0 - 160),
                                b.math("SUBTRACT", (bx0 + 1300, by0 - 160), t_axis, 0.12), 8.0),
                         b.math("MULTIPLY", (bx0 + 1400, by0 - 240),
                                b.math("SUBTRACT", (bx0 + 1300, by0 - 240), 2.3, t_axis), 3.0), 0.5),
                  1.0)
    span = b.math("MINIMUM", (bx0 + 1850, by0 - 200), b.math("MAXIMUM", (bx0 + 1800, by0 - 260), span, 0.0), 1.0)
    bead = b.math("MULTIPLY", (bx0 + 2000, by0), b.math("MULTIPLY", (bx0 + 1850, by0), bead, alive), span)
    bead = b.math("MULTIPLY", (bx0 + 2150, by0), bead,
                  b.math("MULTIPLY_ADD", (bx0 + 2000, by0 - 100), rsep.outputs[2], 0.8, 0.4))
    # Coating colors shift along the axis: warm near the source, pink through
    # the middle, violet and blue past the center.
    tc = b.math("MINIMUM", (bx0 + 1800, by0 - 400), b.math("MAXIMUM", (bx0 + 1700, by0 - 400),
                                                           b.math("MULTIPLY", (bx0 + 1600, by0 - 400), t_axis, 0.5), 0.0), 1.0)
    warm = b.mix_color("MIX", (bx0 + 1950, by0 - 400), b.math("MULTIPLY", (bx0 + 1850, by0 - 500), tc, 2.0),
                       (1.0, 0.65, 0.35, 1.0), (1.0, 0.45, 0.85, 1.0))
    bead_col = b.mix_color("MIX", (bx0 + 2100, by0 - 400),
                           b.math("MULTIPLY_ADD", (bx0 + 1950, by0 - 550), tc, 2.0, -1.0),
                           warm, (0.45, 0.55, 1.0, 1.0))
    beads = b.mix_color("MULTIPLY", (bx0 + 2300, by0 - 300), 1.0, bead_col,
                        b.math("MULTIPLY", (bx0 + 2200, by0 - 150), bead,
                               b.math("MULTIPLY", (bx0 + 2100, by0 - 200), inp["Bead Strength"], 0.6)),
                        "Bead Chain")
    ghosts = b.mix_color("ADD", (gx0 + 2100, gy0 - 650), 1.0, ghosts, beads)
    ghosts = b.mix_color("MULTIPLY", (gx0 + 2100, gy0 - 700), 1.0, ghosts, ghost_gain)

    # Hoop / halo: a thin ring around a point on the axis, brightest on the side
    # facing the light, with a chromatic radius split.
    hx0, hy0 = gx0, gy0 - 1100 - len(GHOSTS) * 900
    hcx, hcy = element_center((hx0, hy0), inp["Halo Position"], spread=False)
    vx = b.math("SUBTRACT", (hx0 + 400, hy0), pxc, hcx)
    vy = b.math("SUBTRACT", (hx0 + 400, hy0 - 60), pyc, hcy)
    hr = b.math("SQRT", (hx0 + 600, hy0),
                b.math("ADD", (hx0 + 500, hy0), sq((hx0 + 450, hy0 + 60), vx), sq((hx0 + 450, hy0 - 120), vy)))
    wx = b.math("SUBTRACT", (hx0 + 400, hy0 - 200), lx, hcx)
    wy = b.math("SUBTRACT", (hx0 + 400, hy0 - 260), ly, hcy)
    wl = b.math("SQRT", (hx0 + 600, hy0 - 200),
                b.math("ADD", (hx0 + 500, hy0 - 200), sq((hx0 + 450, hy0 - 300), wx), sq((hx0 + 450, hy0 - 360), wy)))
    cosang = b.math("DIVIDE", (hx0 + 850, hy0 - 100),
                    b.math("ADD", (hx0 + 700, hy0 - 100), b.math("MULTIPLY", (hx0 + 600, hy0 - 50), vx, wx),
                           b.math("MULTIPLY", (hx0 + 600, hy0 - 120), vy, wy)),
                    b.math("ADD", (hx0 + 750, hy0 - 200), b.math("MULTIPLY", (hx0 + 700, hy0 - 250), hr, wl), 1e-5))
    facing = b.math("POWER", (hx0 + 1100, hy0 - 100),
                    b.math("MULTIPLY_ADD", (hx0 + 1000, hy0 - 100), cosang, 0.5, 0.5), 3.0)
    facing = b.mix_float((hx0 + 1250, hy0 - 100), inp["Halo Directional"], 1.0, facing)
    hchans = []
    for ci, kc in enumerate((1.0, 0.0, -1.0)):
        cy_ = hy0 - 450 - ci * 150
        rad = b.math("MULTIPLY", (hx0 + 700, cy_), inp["Halo Radius"],
                     b.math("MULTIPLY_ADD", (hx0 + 550, cy_), chroma_rad, kc * 1.25, 1.0))
        ring = b.math("EXPONENT", (hx0 + 1150, cy_),
                      b.math("MULTIPLY", (hx0 + 1050, cy_),
                             sq((hx0 + 950, cy_), b.math("DIVIDE", (hx0 + 850, cy_),
                                                          b.math("SUBTRACT", (hx0 + 750, cy_ - 60), hr, rad),
                                                          inp["Halo Thickness"])), -1.0))
        hchans.append(ring)
    hrgb = b.node("CompositorNodeCombineColor", (hx0 + 1300, hy0 - 450))
    for ci in range(3):
        b.link(hchans[ci], hrgb.inputs[ci])
    halo_gain = b.math("MULTIPLY", (hx0 + 1300, hy0 - 700),
                       b.math("MULTIPLY", (hx0 + 1150, hy0 - 700), intensity, inp["Halo Strength"]),
                       b.math("MULTIPLY", (hx0 + 1150, hy0 - 800), facing, 0.25))
    halo = b.mix_color("MULTIPLY", (hx0 + 1450, hy0 - 450), 1.0, hrgb.outputs[0], halo_gain, "Hoop")
    ghosts = b.mix_color("ADD", (gx0 + 2250, gy0 - 700), 1.0, ghosts, halo, "+ Hoop")
    ghosts = b.mix_color("MULTIPLY", (gx0 + 2400, gy0 - 700), 1.0, ghosts, inp["Flare Color"])
    ghosts = b.mix_color("MULTIPLY", (gx0 + 2550, gy0 - 700), 1.0, ghosts, inp["Flare Tint"], "Ghosts")

    # --- Streaks (anamorphic line / star filter) — includes the flare source ---
    streak_src = b.mix_color("ADD", (-1100, 1300), 1.0, img, dots)
    streaks = b.glare("Streaks", (-900, 1300), "Streaks", streak_src,
                      Threshold=inp["Streak Threshold"], Strength=inp["Streak Strength"],
                      Streaks=inp["Streak Count"], Streaks_Angle=inp["Streak Angle"],
                      Fade=inp["Streak Length"], Tint=inp["Streak Tint"], Iterations=5,
                      Color_Modulation=0.1)

    # --- Lens dirt -------------------------------------------------------------
    # Three procedural layers (fine dust, large out-of-focus specks, smudges and
    # wipe marks) with per-speck random size/brightness, or a custom texture,
    # lit by the scene highlights and light sources.
    dvec_x = b.math("MULTIPLY", (-3400, -3100), uv.outputs["X"], aspect)

    def speck_layer(loc, scale_mul, density_mul, size_mul, z_offset, edge_pow):
        x0, y0 = loc
        vec = b.combine((x0, y0), dvec_x, uv.outputs["Y"],
                        b.math("ADD", (x0 - 150, y0 - 100), inp["Dirt Seed"], z_offset))
        vor = b.node("ShaderNodeTexVoronoi", (x0 + 200, y0), voronoi_dimensions="3D")
        b.link(vec, vor.inputs["Vector"])
        # Less than full randomness keeps neighbors apart so specks never clip.
        vor.inputs["Randomness"].default_value = 0.8
        b.link(b.math("MULTIPLY", (x0, y0 - 200), inp["Dirt Scale"], scale_mul),
               vor.inputs["Scale"])
        rnd = b.node("CompositorNodeSeparateColor", (x0 + 400, y0 - 150))
        b.link(vor.outputs["Color"], rnd.inputs[0])
        alive = b.math("LESS_THAN", (x0 + 600, y0 - 100), rnd.outputs[0],
                       b.math("MULTIPLY", (x0 + 400, y0 - 300), inp["Dirt Density"], density_mul))
        radius = b.math("MULTIPLY", (x0 + 800, y0 - 200),
                        b.math("MULTIPLY", (x0 + 600, y0 - 250), inp["Dirt Size"], size_mul),
                        b.math("SUBTRACT", (x0 + 600, y0 - 350), 1.0,
                               b.math("MULTIPLY", (x0 + 600, y0 - 400),
                                      inp["Dirt Variation"], rnd.outputs[1])))
        radius = b.math("ADD", (x0 + 950, y0 - 200), radius, 1e-3)
        shape = b.math("SUBTRACT", (x0 + 1100, y0), 1.0,
                       b.math("DIVIDE", (x0 + 950, y0), vor.outputs["Distance"], radius))
        shape = b.math("POWER", (x0 + 1250, y0),
                       b.math("MAXIMUM", (x0 + 1150, y0 + 100), shape, 0.0), edge_pow)
        bright = b.math("SUBTRACT", (x0 + 1100, y0 - 300), 1.0,
                        b.math("MULTIPLY", (x0 + 950, y0 - 350),
                               inp["Dirt Variation"], b.math("MULTIPLY", (x0 + 800, y0 - 400),
                                                             rnd.outputs[2], 0.85)))
        return b.math("MULTIPLY", (x0 + 1400, y0), b.math("MULTIPLY", (x0 + 1250, y0 - 200),
                                                          alive, bright), shape)

    fine = speck_layer((-3400, -3300), 2.5, 1.0, 0.4, 0.0, 1.6)
    large = speck_layer((-3400, -3900), 0.6, 0.55, 0.42, 37.0, 0.7)
    large = b.math("MULTIPLY", (-1850, -3900), large, 0.45)

    svec = b.combine((-3400, -4400), dvec_x, uv.outputs["Y"], inp["Dirt Seed"])
    smudge_tex = b.node("ShaderNodeTexNoise", (-3200, -4400), noise_dimensions="3D")
    b.link(svec, smudge_tex.inputs["Vector"])
    smudge_tex.inputs["Scale"].default_value = 1.8
    smudge_tex.inputs["Detail"].default_value = 8.0
    smudge_tex.inputs["Roughness"].default_value = 0.6
    smudge_tex.inputs["Distortion"].default_value = 1.2
    smudge = b.math("MAXIMUM", (-2800, -4400),
                    b.math("MULTIPLY_ADD", (-3000, -4400), smudge_tex.outputs["Factor"], 3.0, -1.35),
                    0.0)
    # Wipe marks: noise stretched along one axis reads as finger/cloth streaks.
    wvec = b.combine((-3400, -4700), b.math("MULTIPLY", (-3550, -4650), dvec_x, 0.35),
                     b.math("MULTIPLY", (-3550, -4750), uv.outputs["Y"], 5.0),
                     b.math("ADD", (-3550, -4850), inp["Dirt Seed"], 11.0))
    wipe_tex = b.node("ShaderNodeTexNoise", (-3200, -4700), noise_dimensions="3D")
    b.link(wvec, wipe_tex.inputs["Vector"])
    wipe_tex.inputs["Scale"].default_value = 2.0
    wipe_tex.inputs["Detail"].default_value = 4.0
    wipe = b.math("MAXIMUM", (-2800, -4700),
                  b.math("MULTIPLY_ADD", (-3000, -4700), wipe_tex.outputs["Factor"], 4.0, -2.2), 0.0)
    smudges = b.math("MULTIPLY", (-2500, -4500),
                     b.math("ADD", (-2650, -4500), smudge,
                            b.math("MULTIPLY", (-2650, -4700), wipe, 0.6)),
                     inp["Dirt Smudges"])
    procedural = b.math("ADD", (-1700, -3700), b.math("ADD", (-1700, -3500), fine, large), smudges)

    # Custom dirt texture: scaled to cover the frame (like "Crop"), centered.
    tinfo = b.node("CompositorNodeImageInfo", (-3400, -5100))
    b.link(inp["Dirt Texture"], tinfo.inputs["Image"])
    tdims = b.node("ShaderNodeSeparateXYZ", (-3200, -5100))
    b.link(tinfo.outputs["Dimensions"], tdims.inputs[0])
    cover = b.math("MAXIMUM", (-2800, -5100),
                   b.math("DIVIDE", (-3000, -5050), width,
                          b.math("MAXIMUM", (-3100, -5000), tdims.outputs["X"], 1.0)),
                   b.math("DIVIDE", (-3000, -5150), height,
                          b.math("MAXIMUM", (-3100, -5200), tdims.outputs["Y"], 1.0)))
    tu = b.math("MULTIPLY_ADD", (-2600, -5000),
                b.math("SUBTRACT", (-2750, -4950), uv.outputs["X"], 0.5),
                b.math("DIVIDE", (-2750, -5050),
                       b.math("DIVIDE", (-2900, -5050), width,
                              b.math("MAXIMUM", (-3050, -5000), tdims.outputs["X"], 1.0)), cover), 0.5)
    tv = b.math("MULTIPLY_ADD", (-2600, -5200),
                b.math("SUBTRACT", (-2750, -5150), uv.outputs["Y"], 0.5),
                b.math("DIVIDE", (-2750, -5250),
                       b.math("DIVIDE", (-2900, -5250), height,
                              b.math("MAXIMUM", (-3050, -5200), tdims.outputs["Y"], 1.0)), cover), 0.5)
    tex = b.remap((-2400, -5100), inp["Dirt Texture"], tu, tv, "Fit Dirt Texture")
    dirt_mask = b.mix_color("MIX", (-1500, -3700), inp["Dirt Texture Mix"], procedural, tex,
                            "Dirt Mask")

    # Light on the dirt: a local glow around each source plus a very wide,
    # low-res blur so a bright light reveals dirt across the whole front element.
    dirt_src = b.mix_color("ADD", (-2300, -2900), 1.0, img, dots)
    dirt_glow = b.glare("Fog Glow", (-2100, -2900), "Dirt Glow (Local)", dirt_src,
                        Threshold=inp["Dirt Threshold"], Strength=1.0, Size=0.8)
    dhl = b.glare("Bloom", (-2100, -3200), "Dirt Highlights", dirt_src, output="Highlights",
                  Threshold=inp["Dirt Threshold"], Strength=0.0, Size=0.0)
    dsmall = b.scale((-1900, -3200), dhl, 1.0 / 16.0, 1.0 / 16.0, "1/16 Res")
    wide_px = b.math("MULTIPLY", (-1900, -3400),
                     b.math("MULTIPLY", (-2050, -3400), inp["Dirt Glow Size"], width), 0.35 / 16.0)
    dwide = b.blur((-1750, -3200), dsmall, wide_px, "Dirt Glow (Wide)", extend=True)
    dwide = b.remap((-1600, -3200), dwide, uv.outputs["X"], uv.outputs["Y"], "Back to Full Res")
    dirt_light = b.mix_color("ADD", (-1450, -3100), 25.0, dirt_glow, dwide, "Dirt Light")
    dirt = b.mix_color("MULTIPLY", (-1300, -3400), 1.0, dirt_light, dirt_mask, "Dirt")

    acc = b.mix_color("ADD", (500, 1300), 1.0, img, bloom, "+ Bloom")
    acc = b.mix_color("ADD", (650, 1150), 1.0, acc, veil, "+ Veiling")
    acc = b.mix_color("ADD", (800, 1000), 1.0, acc, flare, "+ Lens Flare")
    acc = b.mix_color("ADD", (950, 850), 1.0, acc, ghosts, "+ Ghosts")
    acc = b.mix_color("ADD", (1100, 700), 1.0, acc, streaks, "+ Streaks")
    acc = b.mix_color("ADD", (1250, 550), inp["Dirt Strength"], acc, dirt, "+ Dirt")

    # --- Edge softness (field curvature / corner MTF falloff) ----------------
    soft_px = b.math("MULTIPLY", (1250, -200),
                     b.math("MULTIPLY", (1100, -200), inp["Edge Softness"], width), 0.008)
    blurred = b.blur((1400, 300), acc, soft_px, "Edge Blur")
    edge_mask = b.math("POWER", (1400, -400), b.math("MINIMUM", (1250, -500), r2, 1.0), 1.5)
    acc = b.mix_color("MIX", (1550, 450), edge_mask, acc, blurred, "Edge Softness")

    # --- Distortion + lateral chromatic aberration ---------------------------
    lens = b.node("CompositorNodeLensdist", (1700, 450), label="Distortion / CA")
    b.link(acc, lens.inputs["Image"])
    lens.inputs["Type"].default_value = "Radial"
    b.link(inp["Distortion"], lens.inputs["Distortion"])
    b.link(inp["Dispersion"], lens.inputs["Dispersion"])
    b.link(inp["Fit"], lens.inputs["Fit"])
    acc = lens.outputs[0]

    # --- Vignette ------------------------------------------------------------
    # Mechanical: a shaped edge with explicit radius and softness (sharpness).
    r = b.math("SQRT", (1600, -900), r2)
    half_soft = b.math("MULTIPLY", (1600, -1050), inp["Vignette Softness"], 0.5)
    mr = b.node("ShaderNodeMapRange", (1800, -900), interpolation_type="SMOOTHSTEP")
    b.link(r, mr.inputs[0])
    b.link(b.math("SUBTRACT", (1700, -1150), inp["Vignette Radius"], half_soft), mr.inputs[1])
    b.link(b.math("ADD", (1700, -1250), inp["Vignette Radius"], half_soft), mr.inputs[2])
    mech = b.math("MULTIPLY", (2000, -900), mr.outputs[0], inp["Vignette"])
    mech = b.mix_color("MIX", (2150, -900), mech, (1.0, 1.0, 1.0, 1.0), inp["Vignette Tint"],
                       "Mechanical Vignette")
    # Optical: cos^4 law from the real field of view, v = (1 + (r*tan)^2)^-2,
    # where tan is the corner half-angle tangent from sensor width / focal length.
    inv_a = b.math("DIVIDE", (1600, -1400), 1.0, aspect)
    k = b.math("MULTIPLY", (1800, -1400), inp["Lens Tangent"],
               b.math("SQRT", (1700, -1500), b.math("MULTIPLY_ADD", (1600, -1550), inv_a, inv_a, 1.0)))
    nat = b.math("POWER", (2000, -1300),
                 b.math("MULTIPLY_ADD", (1900, -1300), r2_round,
                        b.math("MULTIPLY", (1900, -1450), k, k), 1.0), -2.0)
    nat = b.math("POWER", (2150, -1300), nat, inp["Natural Falloff"])
    acc = b.mix_color("MULTIPLY", (2300, 450), 1.0, acc, mech, "Vignette")
    acc = b.mix_color("MULTIPLY", (2450, 450), 1.0, acc, nat, "Natural Falloff")

    # --- Film halation ---------------------------------------------------------
    hal = b.glare("Bloom", (2450, 800), "Halation", acc,
                  Threshold=inp["Halation Threshold"], Strength=inp["Halation Strength"],
                  Size=inp["Halation Size"], Tint=inp["Halation Tint"], Saturation=1.0)
    acc = b.mix_color("ADD", (2700, 450), 1.0, acc, hal, "+ Halation")

    # --- Film grain ------------------------------------------------------------
    wn = b.node("ShaderNodeTexWhiteNoise", (2450, -300), noise_dimensions="4D")
    b.link(coords.outputs["Pixel"], wn.inputs["Vector"])
    b.link(inp["Grain Seed"], wn.inputs["W"])
    g = b.mix_color("MIX", (2650, -300), inp["Grain Chroma"],
                    wn.outputs["Value"], wn.outputs["Color"], "Mono / Color")
    gblur = b.blur((2850, -300), g, inp["Grain Size"], "Grain Size")
    # Blurring lowers noise contrast; compensate so Amount stays perceptually stable.
    gamp = b.math("MULTIPLY", (2850, -550), inp["Grain Amount"],
                  b.math("MULTIPLY_ADD", (2650, -700), inp["Grain Size"], 1.6, 1.0))
    gmul = b.mix_color("LINEAR_LIGHT", (3050, -300), gamp, (1.0, 1.0, 1.0, 1.0),
                       gblur, "Grain Factor")
    acc = b.mix_color("MULTIPLY", (3200, 450), 1.0, acc, gmul, "Grain")

    # --- Master mix ------------------------------------------------------------
    out = b.mix_color("MIX", (3400, 0), inp["Mix"], img, acc, "Lens FX Mix")
    b.link(out, go.inputs[0])

    ng["lens_fx_version"] = GROUP_VERSION
    return ng


# -----------------------------------------------------------------------------
# Scene compositor wiring + drivers
# -----------------------------------------------------------------------------

def _ensure_compositor_tree(scene):
    tree = scene.compositing_node_group
    if tree is None:
        tree = bpy.data.node_groups.new(f"{scene.name} Compositor", "CompositorNodeTree")
        scene.compositing_node_group = tree
    if not any(i.in_out == "OUTPUT" for i in tree.interface.items_tree
               if i.item_type == "SOCKET"):
        tree.interface.new_socket(name="Image", in_out="OUTPUT", socket_type=C)
    out = next((n for n in tree.nodes if n.bl_idname == "NodeGroupOutput"), None)
    if out is None:
        out = tree.nodes.new("NodeGroupOutput")
        out.location = (400, 0)
    if not out.inputs[0].is_linked:
        rl = next((n for n in tree.nodes if n.bl_idname == "CompositorNodeRLayers"), None)
        if rl is None:
            rl = tree.nodes.new("CompositorNodeRLayers")
            rl.location = (-400, 0)
        tree.links.new(rl.outputs["Image"], out.inputs[0])
    return tree, out


def _driver_target(scene, source, channel):
    """Return (id_type, id, data_path) for a driver variable source."""
    if isinstance(source, tuple):
        return "OBJECT", source[0], source[1]
    if source is None:
        return "SCENE", scene, "frame_current"
    if source.startswith("scene:"):
        return "SCENE", scene, source[6:]
    if source.startswith("cam."):
        return "SCENE", scene, f"camera.data.{source[4:]}"
    if source.startswith("fx:"):
        return "SCENE", scene, f"camera.data.lens_fx.{source[3:]}"
    if channel is not None and "[" not in source:
        return "SCENE", scene, f"camera.data.lens_fx.{source}[{channel}]"
    return "SCENE", scene, f"camera.data.lens_fx.{source}"


def _add_drivers(tree, scene, only=None, specs=None):
    for input_name, expr, var_map in (specs if specs is not None else DRIVERS):
        if only is not None and input_name not in only:
            continue
        idx = INPUT_INDEX[input_name]
        path = f'nodes["{NODE_NAME}"].inputs[{idx}].default_value'
        is_color = INPUTS[idx][1] == C
        for ch in (range(3) if is_color else (None,)):
            if ch is not None:
                tree.driver_remove(path, ch)
                fc = tree.driver_add(path, ch)
            else:
                tree.driver_remove(path)
                fc = tree.driver_add(path)
            drv = fc.driver
            drv.type = "SCRIPTED"
            for var_name, source in var_map.items():
                var = drv.variables.new()
                var.name = var_name
                var.type = "SINGLE_PROP"
                id_type, id_, data_path = _driver_target(scene, source, ch)
                t = var.targets[0]
                t.id_type = id_type
                t.id = id_
                t.data_path = data_path
            drv.expression = expr


def _add_follow_drivers(scene):
    """(Re)build the follow-target drivers for the active camera."""
    tree = scene.compositing_node_group
    cam_obj = scene.camera if scene.camera and scene.camera.type == "CAMERA" else None
    target = cam_obj.data.lens_fx.flare_follow if cam_obj else None
    specs = _follow_drivers(cam_obj, target)
    if not specs:
        for axis, value in (("X", 0.0), ("Y", 0.0), ("Z", 1.0)):
            idx = INPUT_INDEX[f"Follow {axis}"]
            tree.driver_remove(f'nodes["{NODE_NAME}"].inputs[{idx}].default_value')
            tree.nodes[NODE_NAME].inputs[idx].default_value = value
    else:
        _add_drivers(tree, scene, specs=specs)
    tree["lens_fx_follow"] = _follow_key(scene)


def _follow_key(scene):
    cam = scene.camera
    if cam is None or cam.type != "CAMERA":
        return ""
    target = cam.data.lens_fx.flare_follow
    return f"{cam.name}|{target.name if target else ''}"


def _remove_drivers(tree):
    ad = tree.animation_data
    if not ad:
        return
    prefix = f'nodes["{NODE_NAME}"]'
    for fc in list(ad.drivers):
        if fc.data_path.startswith(prefix):
            ad.drivers.remove(fc)


def _image_node(tree, name, label, location):
    node = tree.nodes.get(name)
    if node is None:
        node = tree.nodes.new("CompositorNodeImage")
        node.name = name
        node.label = label
        node.location = location
        node.hide = True
    return node


def _render_layers_node(tree):
    return next((n for n in tree.nodes if n.bl_idname == "CompositorNodeRLayers"), None)


_OCCLUDER_TYPES = {"MESH", "CURVE", "SURFACE", "META", "FONT"}
# Ray pattern across the source disc: center plus two rings (unit-disc offsets).
_RAY_TAPS = [(0.0, 0.0)] + [
    (r * math.cos(a), r * math.sin(a))
    for r, n in ((0.5, 6), (1.0, 10))
    for a in (2.0 * math.pi * i / n + r for i in range(n))
]


def _flare_target(scene, cam, fx, depsgraph):
    """Return (u, v, distance) of the flare source as seen by the camera."""
    from bpy_extras.object_utils import world_to_camera_view
    target = fx.flare_follow
    if target is None:
        return fx.flare_position[0], fx.flare_position[1], float("inf")
    ob = target.evaluated_get(depsgraph)
    cam_loc = cam.matrix_world.translation
    if ob.type == "LIGHT" and ob.data.type == "SUN":
        point = cam_loc + ob.matrix_world.to_3x3().col[2].normalized() * 1e5
        dist = float("inf")
    else:
        point = ob.matrix_world.translation
        dist = (point - cam_loc).length
    co = world_to_camera_view(scene, cam, point)
    if co.z <= 0.0:
        return None
    return co.x, co.y, dist


def compute_visibility(scene, depsgraph):
    """Fraction of the flare source not hidden behind the occluder objects.

    Rays go from the camera through points spread across the source disc, so
    the flare fades gradually as the source slides behind an edge.
    """
    cam = scene.camera
    if cam is None or cam.type != "CAMERA":
        return 1.0
    fx = cam.data.lens_fx
    coll = fx.flare_occluders
    if coll is None or not (fx.flare_enable or fx.ghost_enable):
        return 1.0
    occluders = [o.evaluated_get(depsgraph) for o in coll.all_objects
                 if o.type in _OCCLUDER_TYPES and not o.hide_render]
    if not occluders:
        return 1.0
    cam_eval = cam.evaluated_get(depsgraph)
    target = _flare_target(scene, cam_eval, fx, depsgraph)
    if target is None:
        return 1.0
    u, v, dist = target
    tr, br, bl, tl = cam_eval.data.view_frame(scene=scene)
    mw = cam_eval.matrix_world
    origin = mw.translation
    rot = mw.to_3x3()
    render = scene.render
    aspect = (render.resolution_x * render.pixel_aspect_x) / \
        max(render.resolution_y * render.pixel_aspect_y, 1e-6)
    radius = max(fx.flare_occlusion_softness, 1e-4)
    inverses = [(o, o.matrix_world.inverted_safe()) for o in occluders]
    blocked = 0
    for du, dv in _RAY_TAPS:
        su = u + du * radius
        sv = v + dv * radius * aspect
        local = bl + (br - bl) * su + (tl - bl) * sv
        direction = (rot @ local).normalized()
        for ob, inv in inverses:
            lo = inv @ origin
            ld = inv.to_3x3() @ direction
            hit, loc, _n, _i = ob.ray_cast(lo, ld, depsgraph=depsgraph)
            if hit and ((ob.matrix_world @ loc) - origin).length < dist:
                blocked += 1
                break
    return 1.0 - blocked / len(_RAY_TAPS)


def update_visibility(scene, depsgraph=None):
    cam = scene.camera
    if cam is None or cam.type != "CAMERA":
        return
    if depsgraph is None:
        depsgraph = bpy.context.evaluated_depsgraph_get()
    try:
        vis = compute_visibility(scene, depsgraph)
    except (ReferenceError, RuntimeError, AttributeError):
        return
    fx = cam.data.lens_fx
    # Only write on change: the write itself triggers another depsgraph update.
    if abs(fx.flare_visibility - vis) > 1e-4:
        fx.flare_visibility = vis
        # Writing from Python doesn't tag the camera on its own; without this
        # the compositor keeps using the previous visibility.
        cam.data.update_tag()


def install(scene):
    tree, out = _ensure_compositor_tree(scene)
    node = tree.nodes.get(NODE_NAME)
    # Remember the wiring before a rebuild clears the group's sockets.
    src = targets = None
    if node is not None:
        if node.inputs and node.inputs[0].is_linked:
            src = node.inputs[0].links[0].from_socket
        if node.outputs:
            targets = [l.to_socket for l in node.outputs[0].links]
    group = build_group()
    if node is None:
        node = tree.nodes.new("CompositorNodeGroup")
        node.name = NODE_NAME
        node.label = "Lens FX"
        node.node_tree = group
        src = out.inputs[0].links[0].from_socket
        targets = [out.inputs[0]]
        node.location = (out.location.x, out.location.y)
        out.location.x += 250
    else:
        node.node_tree = group
    if src is not None and not node.inputs[0].is_linked:
        tree.links.new(src, node.inputs[0])
    for t in targets or ():
        tree.links.new(node.outputs[0], t)
    node.width = 200

    dirt_node = _image_node(tree, DIRT_IMAGE_NODE, "Lens Dirt Texture",
                            (node.location.x - 300, node.location.y - 250))
    tree.links.new(dirt_node.outputs["Image"], node.inputs["Dirt Texture"])
    kernel_node = _image_node(tree, KERNEL_IMAGE_NODE, "Starburst Kernel",
                              (node.location.x - 300, node.location.y - 300))
    tree.links.new(kernel_node.outputs["Image"], node.inputs["Starburst Kernel"])

    legacy = tree.nodes.get(OCCLUDER_NODE)
    if legacy is not None:
        tree.nodes.remove(legacy)

    _remove_drivers(tree)
    _add_drivers(tree, scene)
    _add_follow_drivers(scene)
    scene.render.use_compositing = True
    sync_images(scene, force=True)
    return node


def uninstall(scene):
    tree = scene.compositing_node_group
    if tree is None:
        return
    node = tree.nodes.get(NODE_NAME)
    if node is None:
        return
    _remove_drivers(tree)
    src = node.inputs[0].links[0].from_socket if node.inputs[0].is_linked else None
    targets = [l.to_socket for l in node.outputs[0].links]
    tree.nodes.remove(node)
    for name in (DIRT_IMAGE_NODE, KERNEL_IMAGE_NODE, OCCLUDER_NODE):
        n = tree.nodes.get(name)
        if n is not None:
            tree.nodes.remove(n)
    if src is not None:
        for t in targets:
            tree.links.new(src, t)


def is_installed(scene):
    tree = scene.compositing_node_group
    return tree is not None and NODE_NAME in tree.nodes


def needs_upgrade(scene):
    if not is_installed(scene):
        return False
    group = scene.compositing_node_group.nodes[NODE_NAME].node_tree
    return group is None or group.get("lens_fx_version") != GROUP_VERSION


def _kernel_key(cam_data):
    dof = cam_data.dof
    fx = cam_data.lens_fx
    return (f"{dof.aperture_blades}|{round(dof.aperture_rotation, 4)}|"
            f"{round(fx.starburst_imperfection, 3)}|{fx.starburst_seed}")


def _kernel_image(cam_data, force=False):
    name = f"Lens FX Starburst ({cam_data.name})"
    img = bpy.data.images.get(name)
    if img is None:
        img = bpy.data.images.new(name, psf.KERNEL_RES, psf.KERNEL_RES,
                                  alpha=True, float_buffer=True)
        img.colorspace_settings.is_data = True
        force = True
    key = _kernel_key(cam_data)
    if force or img.get("lens_fx_key") != key:
        dof = cam_data.dof
        kernel = psf.compute_kernel(dof.aperture_blades, dof.aperture_rotation,
                                    cam_data.lens_fx.starburst_imperfection,
                                    cam_data.lens_fx.starburst_seed)
        img.pixels.foreach_set(kernel.ravel())
        img.update()
        img["lens_fx_key"] = key
    return img


_kernels_loaded = set()


def sync_images(scene, force=False):
    """Images can't be driven, so dirt texture and starburst kernel follow the
    active camera through handlers."""
    tree = scene.compositing_node_group
    if tree is None or NODE_NAME not in tree.nodes:
        return
    if not bpy.app.is_job_running("RENDER"):
        if tree.get("lens_fx_follow") != _follow_key(scene):
            _add_follow_drivers(scene)

    cam = scene.camera
    cam_data = cam.data if cam and cam.type == "CAMERA" else None
    dirt_node = tree.nodes.get(DIRT_IMAGE_NODE)
    if dirt_node is not None:
        image = cam_data.lens_fx.dirt_image if cam_data else None
        if dirt_node.image != image:
            dirt_node.image = image
    kernel_node = tree.nodes.get(KERNEL_IMAGE_NODE)
    if kernel_node is not None and cam_data is not None:
        if bpy.app.is_job_running("RENDER") and kernel_node.image is not None:
            return
        # Generated pixels aren't saved with the file; rebuild once per session.
        first = cam_data.name not in _kernels_loaded
        img = _kernel_image(cam_data, force=force or first)
        _kernels_loaded.add(cam_data.name)
        if kernel_node.image != img:
            kernel_node.image = img


def upgrade_all():
    for scene in bpy.data.scenes:
        if needs_upgrade(scene):
            install(scene)


@persistent
def _on_load(_):
    _kernels_loaded.clear()
    upgrade_all()
    for scene in bpy.data.scenes:
        sync_images(scene)


@persistent
def _on_change(scene, depsgraph=None):
    sync_images(scene)
    if is_installed(scene):
        update_visibility(scene, depsgraph)


def _tag_redraw(self, context):
    # Drivers carry the values; this just nudges the viewport compositor.
    scene = context.scene
    tree = scene.compositing_node_group if scene else None
    if tree is not None:
        tree.update_tag()
    for window in getattr(context.window_manager, "windows", []):
        for area in window.screen.areas:
            if area.type in {"VIEW_3D", "NODE_EDITOR", "IMAGE_EDITOR"}:
                area.tag_redraw()


def _images_update(self, context):
    if context.scene:
        sync_images(context.scene)
    _tag_redraw(self, context)


def _follow_update(self, context):
    # Driver relations to the target are resolved when drivers are built, so
    # rebuild them whenever the follow target changes.
    scene = context.scene
    if scene and is_installed(scene):
        _add_follow_drivers(scene)
    _tag_redraw(self, context)


# -----------------------------------------------------------------------------
# Per-camera settings
# -----------------------------------------------------------------------------

def _f(name, default, lo, hi, desc, soft_hi=None, soft_lo=None, update=_tag_redraw, **kw):
    return FloatProperty(name=name, default=default, min=lo, max=hi,
                         soft_max=soft_hi if soft_hi is not None else hi,
                         soft_min=soft_lo if soft_lo is not None else lo,
                         description=desc, update=update, options={"ANIMATABLE"}, **kw)


def _b(name, default, desc):
    return BoolProperty(name=name, default=default, description=desc, update=_tag_redraw)


def _col(name, default, desc):
    return FloatVectorProperty(name=name, default=default, size=3, min=0.0, max=1.0,
                               subtype="COLOR", description=desc, update=_tag_redraw)


def _follow_is_sun(self):
    ob = self.flare_follow
    return ob is not None and ob.type == "LIGHT" and ob.data.type == "SUN"


class LensFXSettings(bpy.types.PropertyGroup):
    enabled: _b("Enable Lens FX", True, "Apply this camera's lens character")

    bloom_enable: _b("Bloom", False, "Soft glow spreading from bright highlights")
    bloom_strength: _f("Strength", 0.25, 0.0, 10.0, "Bloom intensity", soft_hi=2.0)
    bloom_threshold: _f("Threshold", 1.0, 0.0, 100.0,
                        "Brightness (scene linear) above which highlights bloom. "
                        "Low values (0.1-0.4) give a Pro-Mist diffusion filter look", soft_hi=5.0)
    bloom_size: _f("Size", 0.5, 0.0, 1.0, "Spread of the bloom relative to the frame")
    bloom_tint: _col("Tint", (1.0, 1.0, 1.0), "Color of the bloom")

    veil_enable: _b("Veiling Glare", False,
                    "Stray light scattering inside the lens, lifting blacks and lowering contrast")
    veil_strength: _f("Strength", 0.04, 0.0, 1.0, "Veiling glare amount", soft_hi=0.3)

    flare_enable: _b("Lens Flare", False,
                     "A light source you place in frame, rendered through the lens: diffraction "
                     "starburst from the camera's aperture, glow and stray light")
    flare_position: FloatVectorProperty(
        name="Position", size=2, default=(0.3, 0.7), soft_min=-0.5, soft_max=1.5,
        min=-10.0, max=10.0, subtype="XYZ", update=_tag_redraw, options={"ANIMATABLE"},
        description="Where the light source sits in the frame (0,0 = bottom left, "
                    "1,1 = top right). Values outside 0-1 put it off-screen")
    flare_intensity: _f("Intensity", 1.0, 0.0, 1000.0,
                        "Brightness of the light source. Starburst, glow, ghosts and stray "
                        "light all scale with it", soft_hi=5.0)
    flare_color: _col("Color", (1.0, 0.95, 0.88), "Color of the light source")
    flare_source_size: _f("Source Size", 0.004, 0.0, 1.0,
                          "Apparent size of the light (fraction of frame width). "
                          "The sun is about 0.5° across", soft_hi=0.03, precision=4)
    flare_show_source: _b("Show Source", True,
                          "Draw the light itself as a bright disc (off for an invisible source)")
    flare_follow: PointerProperty(
        name="Follow Object", type=bpy.types.Object, update=_follow_update,
        description="Pin the flare to an object as seen through the camera. Sun lamps use "
                    "their direction. Leave empty to place the flare by Position")
    flare_follow_active: BoolProperty(get=lambda self: self.flare_follow is not None)
    flare_follow_is_sun: BoolProperty(get=_follow_is_sun)
    flare_occluders: PointerProperty(
        name="Occluders", type=bpy.types.Collection, update=_images_update,
        description="Objects in this collection hide the light source: the flare fades as the "
                    "source moves behind them, in the viewport and in renders")
    flare_occlusion_softness: _f("Occlusion Softness", 0.01, 0.0, 0.2,
                                 "How gradually the flare fades as the source slips behind an "
                                 "occluder edge (fraction of frame width)", precision=3,
                                 update=_images_update)
    flare_visibility: FloatProperty(
        name="Visibility", default=1.0, min=0.0, max=1.0, options=set(),
        description="How much of the source is visible past the occluders (computed)")
    flare_use_image: _b("Also Flare Bright Pixels", False,
                        "Anything in the render brighter than the threshold also gets a "
                        "starburst and glow")
    flare_threshold: _f("Threshold", 5.0, 0.5, 1000.0,
                        "Pixels brighter than this (scene linear) also act as flare sources. "
                        "Keep it well above 1, or ordinary surfaces start flaring",
                        soft_hi=50.0, soft_lo=1.0)
    flare_tint: _col("Tint", (1.0, 1.0, 1.0), "Color of all flare elements (lens coating)")
    starburst_strength: _f("Starburst", 1.0, 0.0, 100.0,
                           "Diffraction spikes from the aperture blades. Length and brightness "
                           "follow the camera's f-stop: stop down for longer stars", soft_hi=10.0)
    starburst_size: _f("Starburst Scale", 0.5, 0.0, 10.0,
                       "Spike length at f/8, as a fraction of frame width", soft_hi=2.0)
    starburst_imperfection: _f("Glass Imperfections", 0.3, 0.0, 1.0,
                               "Dust and uneven blade edges: adds fine texture to the spikes",
                               update=_images_update)
    starburst_seed: IntProperty(name="Seed", default=0, min=0, max=9999, update=_images_update,
                                description="Variation of the glass imperfections")
    glow_strength: _f("Glow", 0.5, 0.0, 100.0, "Brightness of the glow around the source",
                      soft_hi=3.0)
    glow_size: _f("Glow Size", 0.15, 0.0, 10.0, "Radius of the glow (fraction of frame height)",
                  soft_hi=1.0)
    glow_falloff: _f("Glow Falloff", 1.0, 0.1, 10.0,
                     "How fast the glow fades: 1 = soft inverse-square, higher = tighter core",
                     soft_hi=4.0)
    source_veil: _f("Stray Light", 0.02, 0.0, 10.0,
                    "Broad wash that lowers contrast near the source, even from just outside "
                    "the frame", soft_hi=0.2, precision=3)
    ana_streak_strength: _f("Anamorphic Streak", 0.0, 0.0, 100.0,
                            "Horizontal line through the source, like a cylindrical "
                            "anamorphic front element", soft_hi=3.0)
    ana_streak_length: _f("Streak Length", 1.0, 0.0, 10.0,
                          "How far the streak reaches (1 = about half the frame width)",
                          soft_hi=3.0)
    ana_streak_thickness: _f("Streak Thickness", 0.3, 0.0, 10.0, "Thickness of the streak line",
                             soft_hi=2.0)
    ana_streak_angle: _f("Streak Angle", 0.0, -math.pi, math.pi, "Streak rotation",
                         subtype="ANGLE")
    ana_streak_color: _col("Streak Color", (0.35, 0.6, 1.0), "Streak color (classic is blue)")

    ghost_enable: _b("Ghosts", False,
                     "Internal reflections: aperture-shaped ghosts along the line from the "
                     "light through the frame center, plus a hoop ring")
    ghost_strength: _f("Strength", 0.8, 0.0, 100.0, "Ghost brightness", soft_hi=3.0)
    ghost_size: _f("Size", 0.3, 0.0, 10.0,
                   "Size of the ghost shapes. Ghosts also shrink as you stop down", soft_hi=2.0)
    ghost_spread: _f("Spread", 1.0, 0.0, 5.0,
                     "Stretch the ghost chain along the axis (1 = physical spacing)", soft_hi=2.0)
    ghost_aspect: _f("Aspect", 1.0, 0.2, 5.0,
                     "Ghost shape ratio. 1 = round, below 1 = tall ovals (anamorphic), "
                     "above 1 = wide", soft_hi=3.0)
    ghost_softness: _f("Edge Softness", 0.15, 0.001, 1.0, "0 = crisp ghost edges, 1 = very soft")
    ghost_distance_fade: _f("Distance Fade", 0.3, 0.0, 1.0,
                            "Ghosts fade as the light moves away from the frame center")
    ghost_texture: _f("Refraction Texture", 0.4, 0.0, 5.0,
                      "Uneven coatings and dust inside each ghost, like light refracting "
                      "through real lens elements", soft_hi=1.5)
    bead_strength: _f("Bead Chain", 1.0, 0.0, 100.0,
                      "String of tiny bright reflections along the flare axis", soft_hi=3.0)
    bead_count: _f("Bead Count", 14.0, 1.0, 200.0, "How densely beads are packed along the axis",
                   soft_hi=60.0)
    bead_size: _f("Bead Size", 1.0, 0.0, 20.0, "Size of the beads", soft_hi=4.0)
    bead_seed: _f("Bead Seed", 0.0, -10000.0, 10000.0, "Random arrangement of the beads",
                  soft_hi=100.0, soft_lo=-100.0)
    ghost_match_aperture: _b("Match Camera Aperture", True,
                             "Shape ghosts with the camera's aperture blades and rotation")
    ghost_blades: IntProperty(name="Blades", default=6, min=3, max=24, update=_tag_redraw,
                              description="Aperture blade count shaping each ghost")
    ghost_roundness: _f("Roundness", 0.2, 0.0, 1.0, "0 = sharp polygon ghosts, 1 = circular")
    ghost_chroma: _f("Chromatic Aberration", 0.3, 0.0, 5.0,
                     "Splits red outward and blue inward from the frame center on every ghost "
                     "and the hoop", soft_hi=2.0)
    halo_strength: _f("Hoop", 0.0, 0.0, 100.0,
                      "Thin ring on the optical axis, brightest on the side facing the light",
                      soft_hi=3.0)
    halo_radius: _f("Hoop Radius", 0.35, 0.0, 5.0, "Ring radius (fraction of frame height)",
                    soft_hi=1.0)
    halo_thickness: _f("Hoop Thickness", 0.02, 0.0005, 1.0, "Ring thickness", soft_hi=0.1,
                       precision=3)
    halo_position: _f("Hoop Position", 1.0, -5.0, 5.0,
                      "Where the ring sits on the axis: 0 = at the light, 1 = frame center, "
                      "2 = mirrored", soft_lo=0.0, soft_hi=2.0)
    halo_directional: _f("Hoop Directional", 0.6, 0.0, 1.0,
                         "Brighten the side of the ring facing the light")

    streak_enable: _b("Streaks", False, "Anamorphic flare line / star-filter light streaks")
    streak_strength: _f("Strength", 0.5, 0.0, 10.0, "Streak intensity", soft_hi=3.0)
    streak_threshold: _f("Threshold", 1.5, 0.0, 100.0, "Highlight brightness that produces streaks",
                         soft_hi=10.0)
    streak_count: IntProperty(name="Count", default=2, min=1, max=16, update=_tag_redraw,
                              description="Number of streak arms. 2 = horizontal anamorphic flare, "
                                          "4-8 = star filter")
    streak_angle: _f("Angle", 0.0, -math.pi, math.pi, "Streak rotation", subtype="ANGLE")
    streak_length: _f("Length", 0.93, 0.5, 1.0, "How far streaks travel across the frame")
    streak_tint: _col("Tint", (0.35, 0.6, 1.0), "Streak color (classic anamorphic is blue)")

    dirt_enable: _b("Lens Dirt", False, "Dust, specks and smudges on the front element, "
                                         "revealed when bright light hits the lens")
    dirt_strength: _f("Strength", 0.6, 0.0, 10.0, "Lens dirt intensity", soft_hi=3.0)
    dirt_density: _f("Density", 0.5, 0.0, 1.0, "How many dust specks cover the lens")
    dirt_size: _f("Speck Size", 0.5, 0.0, 1.0, "Size of the dust specks")
    dirt_variation: _f("Variation", 0.7, 0.0, 1.0,
                       "Randomness of speck size and brightness")
    dirt_smudges: _f("Smudges", 0.3, 0.0, 1.0, "Fingerprint smudges and wipe marks")
    dirt_scale: _f("Scale", 6.0, 0.1, 100.0, "Overall scale of the dirt pattern", soft_hi=30.0)
    dirt_seed: _f("Seed", 0.0, -10000.0, 10000.0, "Random variation of the dirt pattern",
                  soft_hi=100.0, soft_lo=-100.0)
    dirt_threshold: _f("Light Threshold", 0.8, 0.0, 100.0,
                       "Brightness of light that reveals the dirt", soft_hi=5.0)
    dirt_glow_size: _f("Light Spread", 1.0, 0.0, 1.0,
                       "How widely light spreads across the dirty glass")
    dirt_use_image: _b("Use Texture", False, "Use a custom lens dirt image (light dirt on black)")
    dirt_image: PointerProperty(name="Dirt Texture", type=bpy.types.Image,
                                description="Lens dirt image: bright dirt on a black background",
                                update=_images_update)
    dirt_image_mix: _f("Texture Mix", 1.0, 0.0, 1.0,
                       "Blend from the procedural dirt (0) to the texture (1)")

    soft_enable: _b("Edge Softness", False, "Corners lose sharpness, like real glass off-axis")
    softness: _f("Amount", 0.3, 0.0, 1.0, "Corner blur amount")

    distort_enable: _b("Distortion & Aberration", False,
                       "Geometric distortion and lateral chromatic aberration")
    distortion: _f("Distortion", 0.0, -0.999, 1.0,
                   "Radial distortion. Positive = barrel (wide angle, straight lines bow outward), "
                   "negative = pincushion (telephoto)", soft_hi=0.3, soft_lo=-0.3)
    dispersion: _f("Chromatic Aberration", 0.02, 0.0, 1.0,
                   "Color fringing toward the frame edges", soft_hi=0.15)
    fit: _b("Fit", True, "Scale the image so distortion never reveals empty edges")

    vig_enable: _b("Vignette", True, "Lens vignetting: optical falloff plus a shaped edge")
    vig_amount: _f("Amount", 0.35, 0.0, 1.0, "Darkness of the vignette edge")
    vig_radius: _f("Radius", 0.8, 0.0, 2.0,
                   "Where the vignette edge sits. 1 = frame corners, lower = closer to center")
    vig_softness: _f("Softness", 0.8, 0.001, 3.0,
                     "Width of the edge transition. Low = hard edge (matte box, lens hood), "
                     "high = gentle gradient", soft_hi=2.0)
    vig_roundness: _f("Roundness", 0.6, 0.0, 1.0, "0 = follows the frame shape, 1 = perfect circle")
    vig_tint: _col("Tint", (0.0, 0.0, 0.0), "Color the edge fades toward")
    vig_natural: _f("Natural Falloff", 0.5, 0.0, 3.0,
                    "Optical cos⁴ light falloff computed from the camera's real field of view. "
                    "Wider lenses darken the corners more", soft_hi=2.0)

    halation_enable: _b("Halation", False,
                        "Film halation: red-orange glow where light reflects off the film base")
    halation_strength: _f("Strength", 0.3, 0.0, 10.0, "Halation intensity", soft_hi=2.0)
    halation_threshold: _f("Threshold", 0.7, 0.0, 100.0, "Brightness that starts to halate",
                           soft_hi=5.0)
    halation_size: _f("Size", 0.35, 0.0, 1.0, "Halation spread")
    halation_tint: _col("Tint", (1.0, 0.28, 0.08), "Halation color")

    grain_enable: _b("Film Grain", False, "Organic film/sensor grain")
    grain_amount: _f("Amount", 0.06, 0.0, 2.0, "Grain intensity", soft_hi=0.4)
    grain_size: _f("Size", 1.0, 0.0, 10.0, "Grain clump size in pixels", soft_hi=4.0)
    grain_chroma: _f("Color", 0.3, 0.0, 1.0, "0 = monochrome grain, 1 = fully colored grain")
    grain_animated: _b("Animated", True, "New grain pattern every frame")
    grain_seed: _f("Seed", 0.0, -100000.0, 100000.0, "Grain pattern variation", soft_hi=1000.0)


# -----------------------------------------------------------------------------
# Lens presets — characters of well-known real-world lens families
# -----------------------------------------------------------------------------

_OFF = {k: False for k in ("bloom_enable", "veil_enable", "streak_enable", "flare_enable",
                            "ghost_enable", "dirt_enable", "soft_enable", "distort_enable",
                            "vig_enable", "halation_enable", "grain_enable")}
# Settings a preset never touches: the user's own placement and textures.
_KEEP = {"rna_type", "name", "enabled", "dirt_image", "dirt_use_image", "dirt_image_mix",
         "flare_position", "flare_follow"}

PRESETS = {
    "NEUTRAL": ("Neutral (All Off)", "Disable every effect", {}, {}),
    "MODERN_PRIME": (
        "Modern Cinema Prime",
        "Clinical, high-contrast modern glass with an 11-blade iris (Master Prime class)",
        dict(vig_enable=True, vig_amount=0.1, vig_radius=1.0, vig_softness=1.0,
             vig_roundness=0.7, vig_natural=0.4,
             veil_enable=True, veil_strength=0.01,
             bloom_enable=True, bloom_strength=0.08, bloom_threshold=2.0, bloom_size=0.4,
             flare_enable=True, starburst_strength=0.6, starburst_imperfection=0.1,
             glow_strength=0.15, source_veil=0.01,
             ghost_enable=True, ghost_strength=0.12, ghost_size=0.2, ghost_chroma=0.15,
             distort_enable=True, distortion=0.0, dispersion=0.006),
        dict(aperture_fstop=2.8, aperture_blades=11, aperture_ratio=1.0)),
    "WARM_CINE": (
        "Warm Cine Prime",
        "Gentle, warm, flattering roll-off (Cooke-style 'look')",
        dict(vig_enable=True, vig_amount=0.25, vig_radius=0.85, vig_softness=1.2,
             vig_roundness=0.6, vig_natural=0.6,
             veil_enable=True, veil_strength=0.03,
             bloom_enable=True, bloom_strength=0.18, bloom_threshold=0.9, bloom_size=0.55,
             bloom_tint=(1.0, 0.88, 0.72),
             flare_enable=True, starburst_strength=0.8, glow_strength=0.3,
             source_veil=0.03, flare_tint=(1.0, 0.9, 0.75),
             ghost_enable=True, ghost_strength=0.3, ghost_size=0.3, ghost_chroma=0.25,
             halo_strength=0.2,
             soft_enable=True, softness=0.15,
             distort_enable=True, distortion=0.01, dispersion=0.012,
             grain_enable=True, grain_amount=0.03, grain_size=0.8, grain_chroma=0.2),
        dict(aperture_fstop=2.0, aperture_blades=8, aperture_rotation=0.0, aperture_ratio=1.0)),
    "VINTAGE_58": (
        "Vintage 58mm f/2 (Helios-style)",
        "Soviet-era double-gauss: glowy, low contrast, strong falloff, 6-blade stars and ghosts",
        dict(vig_enable=True, vig_amount=0.45, vig_radius=0.7, vig_softness=1.1,
             vig_roundness=0.85, vig_natural=1.0,
             veil_enable=True, veil_strength=0.08,
             bloom_enable=True, bloom_strength=0.35, bloom_threshold=0.7, bloom_size=0.6,
             bloom_tint=(1.0, 0.92, 0.8),
             flare_enable=True, starburst_strength=1.5, starburst_imperfection=0.7,
             glow_strength=0.8, glow_size=0.25, source_veil=0.06,
             flare_tint=(1.0, 0.85, 0.65),
             ghost_enable=True, ghost_strength=0.7, ghost_size=0.4, ghost_spread=1.1,
             ghost_chroma=0.6, halo_strength=0.5, halo_radius=0.3,
             soft_enable=True, softness=0.45,
             distort_enable=True, distortion=0.03, dispersion=0.04,
             grain_enable=True, grain_amount=0.04, grain_size=1.0, grain_chroma=0.3),
        dict(aperture_fstop=2.0, aperture_blades=6, aperture_rotation=math.radians(15),
             aperture_ratio=1.0)),
    "ANAMORPHIC": (
        "Anamorphic 2x Scope",
        "Blue horizontal flare line, tall oval ghosts and bokeh, barrel distortion",
        dict(streak_enable=True, streak_strength=0.6, streak_threshold=1.2, streak_count=2,
             streak_angle=0.0, streak_length=0.96, streak_tint=(0.35, 0.6, 1.0),
             flare_enable=True, starburst_strength=0.3, glow_strength=0.5, glow_size=0.15,
             ana_streak_strength=1.0, ana_streak_length=1.6, ana_streak_thickness=0.25,
             source_veil=0.04, flare_tint=(0.75, 0.85, 1.0),
             ghost_enable=True, ghost_strength=0.6, ghost_size=0.35, ghost_aspect=0.5,
             ghost_match_aperture=False, ghost_blades=9, ghost_roundness=0.8,
             ghost_chroma=0.35, halo_strength=0.4, halo_radius=0.4,
             bloom_enable=True, bloom_strength=0.15, bloom_threshold=1.0, bloom_size=0.5,
             bloom_tint=(0.85, 0.92, 1.0),
             veil_enable=True, veil_strength=0.03,
             distort_enable=True, distortion=0.06, dispersion=0.03, fit=True,
             vig_enable=True, vig_amount=0.35, vig_radius=0.85, vig_softness=0.9,
             vig_roundness=0.15, vig_natural=0.6,
             soft_enable=True, softness=0.3),
        dict(aperture_fstop=2.8, aperture_blades=0, aperture_ratio=2.0)),
    "SUN_FLARE": (
        "Cinematic Sun Flare",
        "Warm sun with a chain of pink and violet element reflections, beads and blue haze",
        dict(flare_enable=True, flare_intensity=1.5, flare_color=(1.0, 0.9, 0.75),
             flare_source_size=0.008, glow_strength=1.2, glow_size=0.35, glow_falloff=1.2,
             source_veil=0.08, flare_tint=(1.0, 0.82, 0.65),
             starburst_strength=1.0, starburst_size=0.4,
             ghost_enable=True, ghost_strength=1.0, ghost_size=0.45, ghost_spread=1.0,
             ghost_softness=0.25, ghost_texture=0.6, ghost_distance_fade=0.2, ghost_chroma=0.8,
             ghost_match_aperture=False, ghost_blades=8, ghost_roundness=0.85,
             bead_strength=1.2, bead_count=18.0, bead_size=1.2,
             halo_strength=0.5, halo_radius=0.45, halo_thickness=0.03, halo_directional=0.7,
             bloom_enable=True, bloom_strength=0.15, bloom_threshold=1.0, bloom_size=0.5,
             veil_enable=True, veil_strength=0.03,
             vig_enable=True, vig_amount=0.2, vig_natural=0.6),
        dict(aperture_fstop=5.6, aperture_blades=8, aperture_ratio=1.0)),
    "STAR_FILTER": (
        "Star Filter (6-point)",
        "Cross-screen filter turning point lights into stars",
        dict(streak_enable=True, streak_strength=0.8, streak_threshold=2.0, streak_count=6,
             streak_angle=math.radians(15), streak_length=0.92, streak_tint=(1.0, 1.0, 1.0),
             bloom_enable=True, bloom_strength=0.1, bloom_threshold=1.5,
             vig_enable=True, vig_amount=0.15, vig_natural=0.4),
        {}),
    "PROMIST": (
        "Black Pro-Mist 1/4 Filter",
        "Diffusion filter: halos around everything bright, softened highlights",
        dict(bloom_enable=True, bloom_strength=0.45, bloom_threshold=0.25, bloom_size=0.7,
             bloom_tint=(1.0, 1.0, 1.0),
             veil_enable=True, veil_strength=0.05,
             flare_enable=True, starburst_strength=0.0, glow_strength=1.0, glow_size=0.35,
             source_veil=0.05,
             vig_enable=True, vig_amount=0.15, vig_natural=0.5),
        {}),
    "FILM_16MM": (
        "16mm Film",
        "Visible grain, halation, softer edges, strong vignette",
        dict(grain_enable=True, grain_amount=0.12, grain_size=1.6, grain_chroma=0.4,
             grain_animated=True,
             halation_enable=True, halation_strength=0.35, halation_threshold=0.6,
             halation_size=0.35, halation_tint=(1.0, 0.25, 0.05),
             vig_enable=True, vig_amount=0.4, vig_radius=0.75, vig_softness=1.0,
             vig_roundness=0.5, vig_natural=1.0,
             soft_enable=True, softness=0.4,
             bloom_enable=True, bloom_strength=0.2, bloom_threshold=0.9, bloom_size=0.5,
             veil_enable=True, veil_strength=0.05,
             flare_enable=True, starburst_strength=0.8, glow_strength=0.4, source_veil=0.04,
             distort_enable=True, distortion=0.0, dispersion=0.02),
        dict(aperture_fstop=2.8, aperture_blades=6, aperture_ratio=1.0)),
    "SUPER8": (
        "Super 8 Home Movie",
        "Heavy grain, blooming halation, hard dark corners, cheap-zoom distortion",
        dict(grain_enable=True, grain_amount=0.2, grain_size=2.2, grain_chroma=0.5,
             grain_animated=True,
             halation_enable=True, halation_strength=0.5, halation_threshold=0.5,
             halation_size=0.45, halation_tint=(1.0, 0.3, 0.06),
             vig_enable=True, vig_amount=0.85, vig_radius=0.9, vig_softness=0.45,
             vig_roundness=0.4, vig_natural=1.2,
             soft_enable=True, softness=0.7,
             bloom_enable=True, bloom_strength=0.3, bloom_threshold=0.7, bloom_size=0.6,
             veil_enable=True, veil_strength=0.1,
             flare_enable=True, starburst_strength=0.6, starburst_imperfection=0.8,
             glow_strength=0.7, source_veil=0.08,
             distort_enable=True, distortion=0.06, dispersion=0.06, fit=True),
        dict(aperture_fstop=1.9, aperture_blades=5, aperture_ratio=1.0)),
    "SMARTPHONE": (
        "Smartphone Main Camera",
        "Tiny sensor: crisp center, chroma noise, mild fringing, small green ghosts",
        dict(vig_enable=True, vig_amount=0.08, vig_radius=1.0, vig_softness=1.5,
             vig_roundness=1.0, vig_natural=0.6,
             distort_enable=True, distortion=0.0, dispersion=0.015,
             grain_enable=True, grain_amount=0.03, grain_size=0.4, grain_chroma=1.0,
             flare_enable=True, starburst_strength=0.4, glow_strength=0.2, source_veil=0.03,
             ghost_enable=True, ghost_strength=0.5, ghost_size=0.12, ghost_spread=1.0,
             ghost_match_aperture=False, ghost_blades=6, ghost_roundness=0.9,
             ghost_chroma=0.1, flare_tint=(0.85, 1.0, 0.85),
             bloom_enable=True, bloom_strength=0.06, bloom_threshold=2.0, bloom_size=0.3),
        dict(aperture_fstop=11.0, aperture_blades=0, aperture_ratio=1.0)),
    "MUSIC_VIDEO": (
        "Lens Dirt + Flare",
        "Hazy, flared look with dust and smudges lit up by backlights",
        dict(dirt_enable=True, dirt_strength=1.5, dirt_density=0.6, dirt_size=0.5,
             dirt_variation=0.8, dirt_smudges=0.5, dirt_scale=6.0, dirt_threshold=0.7,
             bloom_enable=True, bloom_strength=0.3, bloom_threshold=0.9, bloom_size=0.6,
             flare_enable=True, starburst_strength=1.2, glow_strength=0.6, source_veil=0.08,
             flare_tint=(1.0, 0.85, 0.7),
             ghost_enable=True, ghost_strength=0.8, ghost_size=0.35, ghost_chroma=0.5,
             halo_strength=0.6,
             veil_enable=True, veil_strength=0.08,
             streak_enable=True, streak_strength=0.25, streak_threshold=2.0, streak_count=2,
             streak_tint=(1.0, 0.75, 0.5), streak_length=0.9,
             vig_enable=True, vig_amount=0.3, vig_natural=0.6,
             distort_enable=True, dispersion=0.03),
        dict(aperture_fstop=5.6, aperture_blades=7, aperture_ratio=1.0)),
}


class LENSFX_OT_apply_preset(bpy.types.Operator):
    bl_idname = "lens_fx.apply_preset"
    bl_label = "Apply Lens Preset"
    bl_description = "Load a real-world lens character onto this camera"
    bl_options = {"REGISTER", "UNDO"}

    preset: EnumProperty(
        name="Preset",
        items=[(k, v[0], v[1]) for k, v in PRESETS.items()],
    )
    set_optics: BoolProperty(
        name="Apply Optics",
        description="Also set f-stop, aperture blades and bokeh ratio, and enable depth of field",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return _camera_data(context) is not None

    def execute(self, context):
        cam = _camera_data(context)
        fx = cam.lens_fx
        _, _, values, optics = PRESETS[self.preset]
        # Reset to defaults first so presets don't inherit leftovers.
        for prop in fx.bl_rna.properties:
            if prop.identifier not in _KEEP and not prop.is_readonly:
                fx.property_unset(prop.identifier)
        for k, v in {**_OFF, **values}.items():
            setattr(fx, k, v)
        fx.enabled = True
        if self.set_optics and optics:
            cam.dof.use_dof = optics.get("aperture_fstop", 0) < 8.0
            for k, v in optics.items():
                setattr(cam.dof, k, v)
        if not is_installed(context.scene) or needs_upgrade(context.scene):
            install(context.scene)
        sync_images(context.scene)
        _tag_redraw(None, context)
        self.report({"INFO"}, f"Lens FX: {PRESETS[self.preset][0]}")
        return {"FINISHED"}


class LENSFX_OT_install(bpy.types.Operator):
    bl_idname = "lens_fx.install"
    bl_label = "Set Up Lens FX"
    bl_description = ("Add the Lens FX node group to the end of this scene's compositor, "
                      "driven by the active camera")
    bl_options = {"REGISTER", "UNDO"}

    rebuild: BoolProperty(name="Rebuild Node Group", default=False,
                          description="Regenerate the Lens FX node group from scratch")

    def execute(self, context):
        if self.rebuild:
            ng = bpy.data.node_groups.get(GROUP_NAME)
            if ng is not None:
                ng["lens_fx_version"] = -1
        install(context.scene)
        self.report({"INFO"}, "Lens FX added to the compositor")
        return {"FINISHED"}


class LENSFX_OT_uninstall(bpy.types.Operator):
    bl_idname = "lens_fx.uninstall"
    bl_label = "Remove Lens FX"
    bl_description = "Remove the Lens FX node from this scene's compositor (settings are kept)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        uninstall(context.scene)
        return {"FINISHED"}


class LENSFX_OT_viewport_preview(bpy.types.Operator):
    bl_idname = "lens_fx.viewport_preview"
    bl_label = "Viewport Preview"
    bl_description = ("Toggle the real-time viewport compositor in every 3D View "
                      "(Material Preview / Rendered shading, camera view)")

    def execute(self, context):
        spaces = [a.spaces.active for a in context.screen.areas if a.type == "VIEW_3D"]
        turn_on = any(s.shading.use_compositor == "DISABLED" for s in spaces)
        for s in spaces:
            s.shading.use_compositor = "CAMERA" if turn_on else "DISABLED"
            if turn_on and s.shading.type in {"WIREFRAME", "SOLID"}:
                s.shading.type = "MATERIAL"
        self.report({"INFO"}, "Viewport compositor " + ("on" if turn_on else "off"))
        return {"FINISHED"}


class LENSFX_OT_focus_pick(bpy.types.Operator):
    bl_idname = "lens_fx.focus_selected"
    bl_label = "Focus on Selected"
    bl_description = "Use the active object as the camera's focus target and enable depth of field"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        cam_obj = _camera_object(context)
        return cam_obj is not None and context.active_object not in {None, cam_obj}

    def execute(self, context):
        cam = _camera_object(context).data
        cam.dof.use_dof = True
        cam.dof.focus_object = context.active_object
        return {"FINISHED"}


class LENSFX_OT_place_flare(bpy.types.Operator):
    bl_idname = "lens_fx.place_flare"
    bl_label = "Place in Camera View"
    bl_description = ("Click in the camera view to position the flare source. "
                      "Move the mouse to preview, click to confirm, right-click/Esc to cancel")
    bl_options = {"REGISTER", "UNDO", "BLOCKING"}

    @classmethod
    def poll(cls, context):
        return _camera_data(context) is not None

    @staticmethod
    def _camera_view_area(context):
        areas = [context.area] if context.area and context.area.type == "VIEW_3D" else []
        areas += [a for a in context.screen.areas if a.type == "VIEW_3D"]
        for area in areas:
            rv3d = area.spaces.active.region_3d
            if rv3d.view_perspective == "CAMERA":
                region = next(r for r in area.regions if r.type == "WINDOW")
                return area, region, rv3d
        return None

    def _frame_coords(self, context, event):
        from bpy_extras.view3d_utils import location_3d_to_region_2d
        scene = context.scene
        cam = scene.camera
        corners = [cam.matrix_world @ v for v in cam.data.view_frame(scene=scene)]
        pts = [location_3d_to_region_2d(self.region, self.rv3d, p) for p in corners]
        if any(p is None for p in pts):
            return None
        xs = [p.x for p in pts]
        ys = [p.y for p in pts]
        mx = event.mouse_x - self.region.x
        my = event.mouse_y - self.region.y
        return ((mx - min(xs)) / max(max(xs) - min(xs), 1e-6),
                (my - min(ys)) / max(max(ys) - min(ys), 1e-6))

    def invoke(self, context, event):
        found = self._camera_view_area(context)
        if found is None:
            self.report({"WARNING"}, "Look through the camera in a 3D View first (Numpad 0)")
            return {"CANCELLED"}
        self.area, self.region, self.rv3d = found
        self.fx = context.scene.camera.data.lens_fx
        self.original = tuple(self.fx.flare_position)
        self.fx.flare_enable = True
        context.window.cursor_modal_set("CROSSHAIR")
        context.workspace.status_text_set("Click to place the flare · Right-click / Esc to cancel")
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def _finish(self, context):
        context.window.cursor_modal_restore()
        context.workspace.status_text_set(None)

    def modal(self, context, event):
        if event.type in {"RIGHTMOUSE", "ESC"}:
            self.fx.flare_position = self.original
            self._finish(context)
            return {"CANCELLED"}
        if event.type in {"MOUSEMOVE", "LEFTMOUSE"}:
            uv = self._frame_coords(context, event)
            if uv is not None:
                self.fx.flare_position = uv
                _tag_redraw(None, context)
            if event.type == "LEFTMOUSE" and event.value == "PRESS":
                self._finish(context)
                return {"FINISHED"}
        return {"RUNNING_MODAL"}


# -----------------------------------------------------------------------------
# UI
# -----------------------------------------------------------------------------

def _camera_object(context):
    if context.space_data and context.space_data.type == "PROPERTIES":
        ob = context.object
        return ob if ob and ob.type == "CAMERA" else None
    cam = context.scene.camera if context.scene else None
    return cam if cam and cam.type == "CAMERA" else None


def _camera_data(context):
    ob = _camera_object(context)
    return ob.data if ob else None


class _Base:
    @classmethod
    def poll(cls, context):
        return _camera_data(context) is not None


def _draw_main(self, context):
    layout = self.layout
    cam_obj = _camera_object(context)
    scene = context.scene

    if not is_installed(scene):
        box = layout.box()
        box.label(text="Compositor not set up", icon="INFO")
        box.operator("lens_fx.install", icon="NODE_COMPOSITING")
    elif needs_upgrade(scene):
        box = layout.box()
        box.label(text="Node group is from an older version", icon="INFO")
        box.operator("lens_fx.install", text="Update Lens FX", icon="FILE_REFRESH")
    elif scene.camera != cam_obj:
        layout.label(text="Not the active scene camera", icon="ERROR")

    row = layout.row(align=True)
    row.operator_menu_enum("lens_fx.apply_preset", "preset", text="Lens Preset", icon="PRESET")
    row = layout.row(align=True)
    row.operator("lens_fx.viewport_preview", icon="RESTRICT_VIEW_OFF")
    if is_installed(scene):
        row.operator("lens_fx.uninstall", text="", icon="X")


def _draw_optics(self, context):
    layout = self.layout
    layout.use_property_split = True
    layout.use_property_decorate = True
    cam = _camera_data(context)
    layout.prop(cam, "lens", text="Focal Length")
    layout.prop(cam.dof, "use_dof", text="Depth of Field")
    col = layout.column()
    col.active = cam.dof.use_dof
    row = col.row(align=True)
    row.prop(cam.dof, "focus_object", text="Focus On")
    row.operator("lens_fx.focus_selected", text="", icon="EYEDROPPER")
    sub = col.column()
    sub.active = cam.dof.focus_object is None
    sub.prop(cam.dof, "focus_distance", text="Distance")
    layout.prop(cam.dof, "aperture_fstop", text="F-Stop")
    layout.prop(cam.dof, "aperture_blades", text="Blades")
    layout.prop(cam.dof, "aperture_rotation", text="Blade Rotation")
    col = layout.column()
    col.active = cam.dof.use_dof
    col.prop(cam.dof, "aperture_ratio", text="Anamorphic Ratio")


def _draw_flare(layout, fx, cam):
    col = layout.column()
    col.label(text="Light Source")
    row = col.row(align=True)
    row.active = fx.flare_follow is None
    row.prop(fx, "flare_position")
    row = col.row()
    row.active = fx.flare_follow is None
    row.operator("lens_fx.place_flare", icon="RESTRICT_SELECT_OFF")
    col.prop(fx, "flare_follow")
    col.prop(fx, "flare_intensity")
    col.prop(fx, "flare_color")
    col.prop(fx, "flare_source_size")
    col.prop(fx, "flare_show_source")
    col.prop(fx, "flare_occluders")
    sub = col.column()
    sub.active = fx.flare_occluders is not None
    sub.prop(fx, "flare_occlusion_softness")

    col.separator()
    col.label(text="Glow")
    col.prop(fx, "glow_strength")
    col.prop(fx, "glow_size")
    col.prop(fx, "glow_falloff")
    col.prop(fx, "source_veil")

    col.separator()
    col.label(text="Starburst")
    blades = cam.dof.aperture_blades
    if blades < 3:
        spikes = "Circular iris: soft rings, no spikes"
    else:
        spikes = f"{blades} blades → {blades if blades % 2 == 0 else blades * 2} spikes"
    info = col.box().column(align=True)
    info.label(text=f"{spikes}, f/{cam.dof.aperture_fstop:g}", icon="LIGHT_SUN")
    if blades < 3:
        info.label(text="Set Blades in Optics for a starburst")
    col.prop(fx, "starburst_strength")
    col.prop(fx, "starburst_size")
    col.prop(fx, "starburst_imperfection")

    col.separator()
    col.label(text="Anamorphic Streak")
    col.prop(fx, "ana_streak_strength", text="Strength")
    sub = col.column()
    sub.active = fx.ana_streak_strength > 0.0
    sub.prop(fx, "ana_streak_length", text="Length")
    sub.prop(fx, "ana_streak_thickness", text="Thickness")
    sub.prop(fx, "ana_streak_angle", text="Angle")
    sub.prop(fx, "ana_streak_color", text="Color")

    col.separator()
    col.prop(fx, "flare_tint")
    col.prop(fx, "flare_use_image")
    sub = col.column()
    sub.active = fx.flare_use_image
    sub.prop(fx, "flare_threshold")


def _draw_ghosts(layout, fx, cam):
    col = layout.column()
    if not fx.flare_enable:
        col.label(text="Ghosts follow the Lens Flare source", icon="INFO")
    for p in ("ghost_strength", "ghost_size", "ghost_spread", "ghost_aspect",
              "ghost_softness", "ghost_texture", "ghost_distance_fade", "ghost_chroma"):
        col.prop(fx, p)
    col.separator()
    col.prop(fx, "bead_strength")
    sub = col.column()
    sub.active = fx.bead_strength > 0.0
    sub.prop(fx, "bead_count")
    sub.prop(fx, "bead_size")
    sub.prop(fx, "bead_seed")
    col.separator()
    col.prop(fx, "ghost_match_aperture")
    sub = col.column()
    sub.active = not fx.ghost_match_aperture
    sub.prop(fx, "ghost_blades")
    sub.prop(fx, "ghost_roundness")
    col.separator()
    col.prop(fx, "halo_strength")
    sub = col.column()
    sub.active = fx.halo_strength > 0.0
    sub.prop(fx, "halo_radius")
    sub.prop(fx, "halo_thickness")
    sub.prop(fx, "halo_position")
    sub.prop(fx, "halo_directional")


def _draw_dirt(layout, fx, cam):
    col = layout.column()
    col.prop(fx, "dirt_strength")
    col.prop(fx, "dirt_threshold")
    col.prop(fx, "dirt_glow_size")
    col.separator()
    sub = col.column()
    sub.active = not (fx.dirt_use_image and fx.dirt_image_mix >= 1.0)
    for p in ("dirt_density", "dirt_size", "dirt_variation", "dirt_smudges",
              "dirt_scale", "dirt_seed"):
        sub.prop(fx, p)
    col.separator()
    col.prop(fx, "dirt_use_image")
    sub = col.column()
    sub.active = fx.dirt_use_image
    sub.template_ID(fx, "dirt_image", open="image.open")
    sub.prop(fx, "dirt_image_mix")


# (id suffix, label, enable prop, [props] or custom draw function)
SECTIONS = [
    ("bloom", "Bloom", "bloom_enable",
     ["bloom_strength", "bloom_threshold", "bloom_size", "bloom_tint"]),
    ("veil", "Veiling Glare", "veil_enable", ["veil_strength"]),
    ("flare", "Lens Flare", "flare_enable", _draw_flare),
    ("ghost", "Ghosts", "ghost_enable", _draw_ghosts),
    ("streak", "Streaks", "streak_enable",
     ["streak_strength", "streak_threshold", "streak_count", "streak_angle",
      "streak_length", "streak_tint"]),
    ("dirt", "Lens Dirt", "dirt_enable", _draw_dirt),
    ("soft", "Edge Softness", "soft_enable", ["softness"]),
    ("distort", "Distortion & Aberration", "distort_enable",
     ["distortion", "dispersion", "fit"]),
    ("vig", "Vignette", "vig_enable",
     ["vig_amount", "vig_radius", "vig_softness", "vig_roundness", "vig_tint", None,
      "vig_natural"]),
    ("halation", "Halation", "halation_enable",
     ["halation_strength", "halation_threshold", "halation_size", "halation_tint"]),
    ("grain", "Film Grain", "grain_enable",
     ["grain_amount", "grain_size", "grain_chroma", "grain_animated", "grain_seed"]),
]


def _section_draw_funcs(enable, props):
    def draw_header(self, context):
        self.layout.prop(_camera_data(context).lens_fx, enable, text="")

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = True
        cam = _camera_data(context)
        fx = cam.lens_fx
        col = layout.column()
        col.active = getattr(fx, enable) and fx.enabled
        if callable(props):
            props(col, fx, cam)
            return
        for p in props:
            if p is None:
                col.separator()
            else:
                col.prop(fx, p)

    return draw_header, draw


def _make_panels():
    spaces = {
        "PROP": dict(bl_space_type="PROPERTIES", bl_region_type="WINDOW", bl_context="data"),
        "VIEW": dict(bl_space_type="VIEW_3D", bl_region_type="UI", bl_category="Lens FX"),
    }
    classes = []
    for key, space in spaces.items():
        parent_id = f"LENSFX_PT_{key}_main"

        def draw_header(self, context):
            self.layout.prop(_camera_data(context).lens_fx, "enabled", text="")

        main = type(parent_id, (_Base, bpy.types.Panel), dict(
            bl_idname=parent_id, bl_label="Lens FX", draw=_draw_main,
            draw_header=draw_header, **space))
        classes.append(main)

        optics_id = f"LENSFX_PT_{key}_optics"
        classes.append(type(optics_id, (_Base, bpy.types.Panel), dict(
            bl_idname=optics_id, bl_label="Optics", bl_parent_id=parent_id,
            bl_options={"DEFAULT_CLOSED"}, draw=_draw_optics, **space)))

        for suffix, label, enable, props in SECTIONS:
            draw_header, draw = _section_draw_funcs(enable, props)
            pid = f"LENSFX_PT_{key}_{suffix}"
            classes.append(type(pid, (_Base, bpy.types.Panel), dict(
                bl_idname=pid, bl_label=label, bl_parent_id=parent_id,
                bl_options={"DEFAULT_CLOSED"}, draw=draw, draw_header=draw_header, **space)))
    return classes


_classes = [
    LensFXSettings,
    LENSFX_OT_apply_preset,
    LENSFX_OT_install,
    LENSFX_OT_uninstall,
    LENSFX_OT_viewport_preview,
    LENSFX_OT_focus_pick,
    LENSFX_OT_place_flare,
]
_panels = []
_handlers = [
    (bpy.app.handlers.load_post, _on_load),
    (bpy.app.handlers.depsgraph_update_post, _on_change),
    (bpy.app.handlers.frame_change_post, _on_change),
]


def _upgrade_after_register():
    try:
        upgrade_all()
        for scene in bpy.data.scenes:
            sync_images(scene)
    except Exception as e:  # never block enabling the add-on
        print("Lens FX: upgrade skipped:", e)
    return None


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Camera.lens_fx = PointerProperty(type=LensFXSettings)
    _panels[:] = _make_panels()
    for cls in _panels:
        bpy.utils.register_class(cls)
    for handlers, fn in _handlers:
        if fn not in handlers:
            handlers.append(fn)
    bpy.app.timers.register(_upgrade_after_register, first_interval=0.1)


def unregister():
    for handlers, fn in _handlers:
        if fn in handlers:
            handlers.remove(fn)
    for cls in reversed(_panels):
        bpy.utils.unregister_class(cls)
    _panels.clear()
    del bpy.types.Camera.lens_fx
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
