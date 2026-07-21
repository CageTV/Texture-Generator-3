
TG3 v1.1.14 - AI Depth Upgrade (for reference-quality depth like your screenshot)

WHAT'S NEW:
- New module ai_depth_engine.py: uses Depth Anything V2 (fast CPU) or Marigold (diffusion Unet, same structure as lucidrains/denoising-diffusion-pytorch) to generate depth like your reference image
- Upgraded material_engine.py: height_from_diffuse() now accepts use_ai, ai_model, ai_detail, ai_strength
- Upgraded height_map_builder.py: get_height_field() now accepts use_ai
- Upgraded shaders/normal.comp: Sobel-based, optimized for smooth AI depth (preserves engravings)
- Upgraded texture_generator.py: New UI panel "AI DEPTH - HIGH QUALITY" in Material Generator tab
- Upgraded TG3.spec: bundles ai_depth_engine and new shader, hiddenimports for torch/transformers/diffusers

HOW TO INSTALL:
1. Backup your texture_generator folder
2. Unzip this over it (overwrite)
3. pip install torch transformers diffusers accelerate --upgrade
   For fast CPU only: pip install transformers torch pillow numpy opencv-python
4. python -m PyInstaller TG3.spec
   Output: dist/TG3.exe

HOW TO USE:
- Open TG3 -> Material Generator tab
- At top, check "USE AI DEPTH"
- Model: depth-anything-small = fast (100MB), marigold-v1 = quality like your reference (2GB GPU)
- Detail Blend: 0.0 = super smooth like ref, 0.15 = keeps small engravings (forehead pattern)
- Generate All Maps -> Height will now be AI depth, Normal will be derived from that depth (not luminance) -> gives you that sculpted look

If torch not installed, it auto falls back to old luminance so app never crashes.
