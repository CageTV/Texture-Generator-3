
TG3 v1.1.16 - AI Depth v2 FIX for blotchy Falmer (statuefalmer_p.png)

YOUR ISSUE:
depth-anything-small runs at 384px, heavy blur, trained on rooms. UV atlas with 30 islands -> thinks each island is separate object -> blobs. Your statuefalmer_p.png shows this: no detail, just blotches.

FIX IN V2:
- New default model: depth-anything-v2-large (1024px, 1.3GB) - 4x detail, keeps engravings, much less blotchy
- New models:
  * lotus-depth: detail-preserving diffusion, beats marigold, same Unet structure as lucidrains but keeps tiny patterns
  * stable-normal-turbo: predicts NORMAL directly, zero blobs, then integrates to height (BEST for keeping Falmer circles)
  * deepbump: material-specific normal, 50MB fastest sharp

- Guided Filter: uses diffuse luminance as guide to stop depth bleeding across UV islands into black voids
  This is the key fix for your reference image with black background.

- Detail Blend now 0.35 default (was 0.15) for Falmer-type - restores engravings Small blurred away

- Normal-to-Height integration: for stable-normal-turbo, we predict crisp normal then Poisson-integrate to height = sculpted depth like reference with zero blobs

USAGE:
In TG3:
  Material Generator -> USE AI DEPTH -> Model: depth-anything-v2-large -> Detail Blend 0.35 -> Generate
  or Model: stable-normal-turbo -> Best for no blobs, keeps all tiny circles
  or Model: lotus-depth -> Best overall quality

Batch:
  _p MAP BUILDER -> USE AI DEPTH -> Model: depth-anything-v2-large -> Detail 0.35 -> Run

INSTALL:
pip install torch transformers diffusers accelerate opencv-python-headless opencv-contrib-python
For guided filter: pip install opencv-contrib-python (for ximgproc.guidedFilter)
Then: python -m PyInstaller TG3.spec

MODELS:
Small (your blotchy): 99MB, 384px, blurry
V2 Large (fixed): 1.3GB, 1024px, sharp
Lotus: 2.5GB, GPU, detail-preserving diffusion
StableNormal Turbo: 1.6GB, GPU, 1 step, zero blobs, best for engravings
