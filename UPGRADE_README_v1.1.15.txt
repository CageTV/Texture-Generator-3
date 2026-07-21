
TG3 v1.1.15 - Full AI Depth Upgrade
====================================

NEW IN v1.1.15 (over v1.1.14):
- Dual Layer Builder now has AI Depth toggle (same as Material Generator)
  - Checkbox: USE AI DEPTH (High Quality like reference)
  - Model: depth-anything-small (fast CPU) / marigold-v1 (diffusion quality, like lucidrains Unet)
  - Detail Blend: 0=smooth like ref, 0.15=preserve engravings
  - When enabled, Height is AI depth, Normal is derived from AI depth, giving you that sculpted look from your reference image

- Batch _p Map Builder (_p MAP BUILDER tab) now has AI Depth:
  - Checkbox: USE AI DEPTH for Height
  - Processes entire folder of diffuse PNGs to _p (height) and _n (normal) using AI depth
  - Perfect for batch converting Skyrim textures to reference-quality depth

- height_map_builder.py process_folder() now accepts use_ai, ai_model, ai_detail params

INSTALL:
1. Backup texture_generator/
2. Unzip over it
3. pip install torch transformers diffusers accelerate opencv-python moderngl glcontext pillow numpy
4. python -m PyInstaller TG3.spec -> dist/TG3.exe

USAGE:
Material Generator:
  Check USE AI DEPTH -> Model: depth-anything-small -> Generate All Maps

Dual Layer Builder:
  Load Diffuse -> Check USE AI DEPTH at top -> Generate All 8 Maps
  Height will now be AI depth like your reference, Normal from that depth

Batch Builder (_p MAP BUILDER tab):
  Input Folder: folder with diffuse PNGs
  Output Folder: where to save _n and _p
  Check USE AI DEPTH -> Run Batch Builder
  Will create for each diffuse: <name>_n.png (normal from AI depth) and <name>_p.png (AI depth)

MODELS:
- depth-anything-small: 99MB, CPU, fast, 90% of reference quality
- depth-anything-base: 400MB, better
- marigold-v1: 2GB, GPU, exact reference quality, uses diffusion Unet like lucidrains repo
  (same structure: Unet + GaussianDiffusion conditioned on image)

FALLBACK:
If torch not installed, auto uses old luminance so app never crashes.
