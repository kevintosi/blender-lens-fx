# Lens FX

Real-world lens character for Blender cameras: physically based lens flares, ghosts, bloom, chromatic aberration, vignetting, lens dirt, halation and film grain. Everything runs in the compositor, so it works in Cycles and EEVEE, in final renders and in the live viewport.

Requires **Blender 5.0 or newer**.

## Features

- **Lens Flare.** A light source you place in frame, or pin to an object or Sun lamp. It includes:
  - a diffraction starburst computed from the camera's real aperture: the blade count sets the number of spikes, and the f-stop sets their length and brightness;
  - glow, stray light and an anamorphic streak.
- **Ghosts.** Reflections from the lens elements, lined up from the light through the frame center:
  - aperture-shaped ghosts with refraction texture and chromatic aberration;
  - a bead chain of tiny reflections and a hoop ring.
- **Occlusion.** Pick a collection of objects. The flare fades as its source moves behind any of them, in the viewport and in renders.
- **Lens effects.** Bloom, veiling glare, star or anamorphic streaks, edge softness, barrel and pincushion distortion, and a vignette with shaped edges and optical cos⁴ falloff.
- **Lens dirt.** Procedural dust, specks and smudges, or your own dirt texture, lit by bright light.
- **Film.** Halation and grain.
- **Per-camera settings.** Each camera keeps its own lens look. Switching cameras, including with timeline markers, switches the look. Every setting can be keyframed.
- **Presets.** Modern Cinema Prime, Warm Cine Prime, Vintage 58mm, Anamorphic 2x, Cinematic Sun Flare, Star Filter, Black Pro-Mist, 16mm Film, Super 8, Smartphone, and Lens Dirt + Flare.

## Install

1. Download this repository as a zip (**Code → Download ZIP**), or build one with `blender --command extension build --source-dir lens_fx`.
2. In Blender, open **Edit → Preferences → Get Extensions**, then choose **⌄ → Install from Disk…** and pick the zip.

## Use

1. Select a camera, open **Camera Properties → Lens FX**, and click **Set Up Lens FX**. The same controls are in the 3D View sidebar under the **Lens FX** tab.
2. Choose a **Lens Preset**, or enable effects one by one.
3. Click **Viewport Preview** and look through the camera (Numpad 0) to see the result live.
4. For a flare, enable **Lens Flare** and click **Place in Camera View** to position it. For spikes, set **Blades** under **Optics**.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
