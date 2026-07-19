# TG3 (Texture Generator) — Build & Usage Guide

## File Layout (this folder)

```
texture_generator/
├── texture_generator.py      ← Main app (all tabs built in here) — RUN THIS
├── pbr_engine.py               procedural PBR generation + Skyking PBR JSON tool
├── material_engine.py           (luminance/saturation-based heuristics)
├── material_preview.py        ← Standalone "Material Preview" window (3D preview)
├── gpu_preview.py              \  Studio (GPU) lighting mode
├── gpu_engine.py                } GPU-accelerated normal-map generation
├── ibl_preview.py               / IBL Sky (GPU) lighting mode — sky cubemap,
│                                  irradiance/prefilter convolution, split-sum
│                                  BRDF LUT, clearcoat + fuzz/sheen lobes
├── height_map_builder.py       ← CPU/GPU height→normal batch pipeline
├── shaders/normal.comp        ← Compute shader used by gpu_engine.py
├── background.png / icon.ico  ← UI assets
├── texconv.exe / BSArch.exe   ← Bundled DDS/BSA-BA2 tools
├── TG3.spec                    ← PyInstaller build config (canonical)
└── BUILD_README.md            ← This file
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
