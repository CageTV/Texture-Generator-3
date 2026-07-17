# TG3 (Texture Generator) — Build & Usage Guide
**Current Version: v1.1.13** (from v1.1.12)

## File Layout (this folder)

```
texture_generator/
├── texture_generator.py      <- Main app (all tabs built in here) — RUN THIS
├── pbr_engine.py               procedural PBR generation + Skyking PBR JSON tool
├── material_engine.py          luminance/saturation-based heuristics (Material Generator backend)
├── material_preview.py        <- Standalone "Material Preview" window (3D preview)
├── gpu_preview.py              \  Studio (GPU) lighting mode
├── gpu_engine.py                } GPU-accelerated normal-map generation
├── ibl_preview.py               / IBL Sky (GPU) lighting mode — sky cubemap,
│                                  irradiance/prefilter convolution, split-sum
│                                  BRDF LUT, clearcoat + fuzz/sheen lobes
├── height_map_builder.py       <- CPU/GPU height→normal batch pipeline
├── shaders/normal.comp        <- Compute shader used by gpu_engine.py
├── background.png / icon.ico  <- UI assets
├── texconv.exe / BSArch.exe   <- Bundled DDS/BSA-BA2 tools
├── TG3.spec                    <- PyInstaller build config (canonical)
├── BUILD_README.md            <- This file
└── CHANGELOG_v1.1.13.txt      <- Changelog 1.1.12 -> 1.1.13
```

## How to Build (Windows)

```
pip install pillow numpy opencv-python moderngl glcontext pyinstaller
python -m PyInstaller TG3.spec
```
Output: `dist\TG3.exe` — a single portable file with everything bundled in
(texconv.exe, BSArch.exe, shaders, icon, background).

If building from a network share (`\\server\share\...`), use `BUILD_TG3.bat`
instead of running PyInstaller directly — `cd` can't set a UNC path as the
working directory and silently falls back to `C:\Windows`, which breaks the
build. The `.bat` uses `pushd` to work around that.

## How to Build (Linux)

```
./build_tg3_linux.sh
```
or manually:
```
pip install pillow numpy opencv-python moderngl glcontext pyinstaller
python3 -m PyInstaller TG3_linux.spec
```
Output: `dist/TG3` — run with `./dist/TG3`.

**System packages** (apt): `python3-tk` is required. `imagemagick` and
`nvidia-texture-tools` are only needed for DDS conversion; `wine` is only
needed for BSA/BA2 packing. Everything else (texture conversion, normal
maps, PBR generation, the IBL Sky material preview) works with no extra
system packages.

**What's different from Windows, and why:**
- **DDS conversion** goes through `nvcompress` (encode: PNG/TGA/etc. → DDS)
  and `imagemagick`'s `convert`/`magick` (decode: DDS → PNG/etc.), instead of
  `texconv.exe`. This is an approximation, not a byte-identical port —
  `nvcompress`'s BC-format flags are the closest match to texconv's DXGI
  format strings, but compression quality/output size can differ slightly.
  If output looks wrong, run `nvcompress --help` on the target machine and
  check the flag mapping in `platform_tools.py` (`_NVCOMPRESS_FLAGS`), since
  flags have changed across `nvidia-texture-tools` releases.
- **BSA/BA2 packing** still uses the bundled `BSArch.exe`, run through Wine
  (`wine BSArch.exe ...`) rather than a native Linux tool, since no native
  Linux BSArch build is known to exist.
- **"Reveal in file manager"** uses `xdg-open` instead of `os.startfile`.
- All of this lives behind one abstraction, `platform_tools.py` — the rest
  of the app calls `texconv_available()`/`dds_encode()`/`dds_decode()`/
  `bsarch_available()`/`bsarch_command()`/`open_in_file_manager()` and never
  touches an exe path or `os.startfile` directly.
- No embedded `.ico` — Linux doesn't support that. Use `tg3.desktop`
  (included) for a proper launcher: edit the `Exec=`/`Icon=` paths inside it,
  supply your own square PNG for the icon (the bundled `background.png` is
  a portrait poster, not icon-shaped), then copy it to
  `~/.local/share/applications/`.

---

## v1.1.13 Changelog — From v1.1.12

### 1. Material Generator — Major Expansion (Main Feature)

Comparison: v1.1.12 (original) had minimal controls, v1.1.13 is fully expanded.

**HEIGHT:**
- Before: Blur, Contrast, Brightness (3 sliders)
- Now: Blur (2.00), Contrast (1.00), Brightness (0.00) + **Invert checkbox**
- Better heightmap control for parallax

**NORMAL:**
- Before: Scale (8.00) only
- Now: Scale (8.00) + Diffuse Wgt (0.00) + **Full Z / Flip Y / Flip X** checkboxes
- Allows DirectX vs OpenGL normal flipping and diffuse influence

**AO (Ambient Occlusion) — Complete Rewrite:**
- Before: Radius, Intensity, Power
- Now: Pixel Spread (4.00), Pixel Depth (4.00), Blend N/D (1.00), Power (1.00), Bias (0.00)
- Much more accurate cavity AO with spread/depth control

**ROUGHNESS:**
- Before: Base, Lum Infl. (2 sliders)
- Now: Base (0.65), Lum Infl. (0.48), **Sat Infl. (0.25), Blur (1.50) + Smooth checkbox**
- Considers luminance + saturation + blur

**METALNESS:**
- Before: Threshold, Sharpness
- Now: Threshold (0.55), Sharpness (6.00), **Blur (2.00)**
- Softens metalness mask, less noise

**EDGE (Curvature):**
- Before: Blur, Low Thr, High Thr
- Now: Blur (1.00), Low Thr (40.00), High Thr (130.00), **Dilate (1.00), Soften (0.50) + Invert**
- Full edge wear pipeline

**EMISSIVE:**
- Same: Threshold (0.80), Falloff (0.12), Bloom (2.00) — improved algorithm behind

**RMAOS CHANNEL PACKER:**
- Now auto-packed after generation with preview at bottom
- Format: R=Roughness, G=Metalness, B=AO, A=Specular
- New checkbox: RMAOS auto-packed after generation

**UI:**
- Left controls now taller, scrollable, more sliders visible
- Each section has X toggle to enable/disable map
- Right grid: 4x2 + RMAOS (packed) at bottom, Save As buttons

### 2. Dual Layer Builder — Complete Rework

**v1.1.12 had:** 6 basic sliders, flat metallic, inverted luminance roughness, luminance-only height, flat coat, no real-time, no scroll, tiny 170px previews leaving 60% empty black space in fullscreen.

**v1.1.13 Now:**

**Base Controls Upgrade:**
- BASE COLOR: Brightness (-0.5 to 0.5), Contrast (0.5-2.0), Saturation (0-2.0)
- NORMAL: Blur 0-10, Z Strength 0.1-5.0, Detail 0-1, Flip X/Y, Mode Sobel/Scharr
- ROUGHNESS: Contrast, Min/Max clamp 0-1, Gamma 0.1-3.0, Blur 0-10, AO Mix 0-1, Invert toggle
- METALLIC: Modes Flat/Threshold/Color Detect + Thresh, Toler, Contrast, Blur
- HEIGHT: Sources Luminance/Red/Average + Contrast, Brightness, Blur, Midlevel 0-1, Parallax Scale 0-0.1, Invert
- CLEAR COAT: Opacity, Roughness, Normal Strength 0-2, Metallic, Parallax, Color picker, Use blurred normal toggle
- AO/CURVATURE: Strength 0-2, Radius 0-10 from height

**Live Preview (v1.3):**
- New checkbox: Live Preview (on by default) at top
- All sliders use trace_add -> debounced 150ms auto-generate
- Checkboxes and dropdowns also trigger live
- No need to hit Generate All 8 Maps anymore

**Layout Flip (v1.4):**
- Before: Left 380px controls + Right 170px thumbs = huge empty space
- Now: TOP = 8 big previews in 4x2 grid, responsive 200-450px (starts 260px), in 1080p fullscreen ~360-400px
- BOTTOM = controls in 4 columns, 420px tall, scrollable:
  Col 0: Base Color + Normal
  Col 1: Roughness + Metallic
  Col 2: Height/Parallax + AO
  Col 3: Clear Coat + Generate/Save/Preview
- Previews scale with window resize and re-render

### 3. PBR JSON Builder — Responsive Fix

**Before (v1.1.12):**
- left_half/right_half pack side=left, fixed width 440 + pack_propagate(False)
- When not fullscreen, left log cut off, no resize

**Now (v1.1.13):**
- Uses PanedWindow orient=horizontal, sashwidth=6, draggable divider, minsize 380 stretch always
- User can drag divider to give more space to left or right tool
- _pbr_layout rewritten as responsive grid: controls weight 1 minsize 360 scrollable, log weight 2 gets 2x space
- Fixed crash: TclError cannot use geometry manager grid inside which already has slaves managed by pack
- Both Python keyword-driven and PowerShell scaffold+fill tools now resize with window

### 4. Bug Fixes
- Fixed IndentationError at _build_dual_layer_tab line 3060 (def at column 0)
- Fixed IndentationError at _dl_update_cell line 3285
- Fixed TclError pack/grid mix in _pbr_layout (launch crash at _build_pbr_tab)
- Fixed Dual Layer scrollregion not expanding
- Fixed requiring Generate All 8 Maps button for preview

### 5. Performance
- Dual Layer: luminance cached, single cv2 blur passes
- Live preview debounced 150ms to avoid spamming on 4K
- Toggle Live Preview off for huge images

---

## Material Preview — Lighting Modes

`material_preview.py` opens a standalone 3D preview window with three
interchangeable renderers, picked at runtime:
- **IBL Sky (GPU)** — `ibl_preview.py`. Full image-based lighting: procedural
  sky → irradiance convolution → GGX-prefiltered specular → split-sum BRDF LUT.
  Plus simplified clearcoat and fuzz/sheen lobes.
- **Studio (GPU)** — `gpu_preview.py`. Single directional light, Cook-Torrance,
  no environment lighting.
- **Studio (CPU)** — `material_preview.py`'s own NumPy raytracer. Always
  available as a fallback if no GPU/OpenGL context can be created.

**Parallax mapping fix:** the Parallax Depth slider previously only worked in
Studio (CPU) — neither GPU shader read a height texture at all, so the slider
was a silent no-op in IBL Sky and Studio (GPU) modes. Both shaders now sample
a `heightTex` and do the same tangent-space parallax offset as the CPU path
before sampling albedo/normal/rmaos, so the slider works identically in all
three modes.

## Two divergent PBR-JSON generators — worth reconciling

Two separate implementations of the same "scan a mod folder, build a
PBRNIFPatcher JSON" tool exist side by side:
- `generate.py` + `config.json` — a standalone CLI script, config-file driven,
  requires only diffuse+normal minimum.
- `pbr_engine.py`'s `run_generate_json()` (labeled "TOOL 3 ... (generate.py)"
  in a comment) — the version actually wired into the app's Parallax Generator
  tab, with hardcoded defaults baked into the .py file (different specular
  level, an extra `glint` section, no `subsurface_foliage`), and a *stricter*
  requirement of diffuse+normal+height+rmaos all present.

They'll disagree on which texture sets qualify and what settings they get.
`generate.py`/`config.json` aren't currently called from anywhere in the GUI —
they're along for the ride but inert. Flagging this rather than silently
picking one, since reconciling the schemas is a real decision, not a bug fix.

## Not Included

**AI-driven PBR generation** — only one `.pt` checkpoint ever survived upload
(filename collisions overwrote the other 6), and no model-loading/inference
code was ever provided. Nothing in this build references `torch`.

`Texture-builder-app-prototype.py` is an earlier, truncated draft of
`gpu_preview.py` — safe to ignore, kept only for reference.
