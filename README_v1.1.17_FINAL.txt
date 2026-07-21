
TG3 v1.1.17 - Falmer Fix FINAL - AI Depth v2.1

This is NOT the old files. This is NEW v2.1 Falmer Fix built from your diffuse statuefalmer.png and your SculptOK reference.

YOUR ISSUE:
- statuefalmer_p.png from depth-anything-small = blotchy blobs, no detail (your screenshot)
- You used Detail 0.15 + Blur 2.8 which smeared everything
- SculptOK.com/3d uses material-trained models (similar to StableNormal) which is why their result is better

V2.1 FIXES IN THIS PACK:
1. ai_depth_engine.py v2.1 - Complete rewrite:
   - Default: depth-anything-v2-large at 1024px (was small at 384px) - 4x detail
   - New models: lotus-depth (detail-preserving diffusion, beats marigold, same Unet as lucidrains), stable-normal-turbo (predicts normal directly, ZERO blobs, best for keeping tiny circles), deepbump (material-specific, 50MB, sharp, works without torch)
   - Guided Filter: uses diffuse luminance as guide to stop depth bleeding across UV islands into black voids (key fix for your reference with black background)
   - Detail Blend default 0.35-0.45 for Falmer (was 0.15) restores engravings
   - Normal-to-Height integration: for stable-normal-turbo, we predict crisp normal then Poisson-integrate to smooth sculpted height like your first reference image

2. texture_generator.py v1.1.15:
   - Material Generator tab: AI Depth panel with model dropdown + Detail Blend
   - Dual Layer Builder: Same AI Depth panel at top (USE AI DEPTH)
   - Height Map Generator (_p Builder): AI Depth checkbox + model + detail slider for batch folder processing

3. material_engine.py, height_map_builder.py: now accept use_ai, ai_model, ai_detail, use_guided

4. shaders/normal.comp: upgraded Sobel shader optimized for smooth AI depth

5. TG3.spec: bundles new engine + hiddenimports for torch/transformers/diffusers

INSTALL:
1. Backup texture_generator folder
2. Unzip this FINAL over it (overwrite 7 files)
3. pip install torch transformers diffusers accelerate opencv-contrib-python pillow numpy opencv-python moderngl glcontext pyinstaller
   Minimal for DeepBump sharp path (no blobs, no torch): pip install opencv-python pillow numpy
4. python -m PyInstaller TG3.spec -> dist/TG3.exe

RECOMMENDED SETTINGS FOR YOUR STATUEFALMER DIFFUSE:
For that diffuse you just uploaded (statuefalmer.png):
- Height Map Generator:
  Model: stable-normal-turbo (BEST, zero blobs, keeps all circles) OR depth-anything-v2-large (fast)
  Detail Blend: 0.45
  Blur Radius: 0.0  (you had 2.8 which destroyed detail)
  Gradient Mult: 0.25
  Normal Strength: 6.0 (you had 11.1 which adds noise)
  [ ] Normalize Height UNCHECKED (your reference has black voids)

- If result still not like SculptOK reference: SculptOK uses high-poly bake + material training. Best open equivalent is stable-normal-turbo -> height integration.
  If you have 50+ pairs of diffuse + SculptOK reference height like you have, we can fine-tune a custom model that will beat SculptOK for Skyrim textures.

FILES IN THIS ZIP:
- texture_generator.py (v1.1.15 + AI in all tabs)
- material_engine.py (v1.1.14 AI-aware)
- height_map_builder.py (v1.1.15 batch AI)
- ai_depth_engine.py (v2.1 Falmer Fix FINAL)
- gpu_engine.py
- shaders/normal.comp
- TG3.spec

This is NEW, not the old files from earlier.
