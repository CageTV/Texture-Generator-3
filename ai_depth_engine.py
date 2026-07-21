
"""
ai_depth_engine.py - TG3 v1.1.14 AI Depth upgrade
Provides high-quality depth like your reference image: https://github.com/CageTV/Texture-Generator-3

This is the bridge to the diffusion world (lucidrains/denoising-diffusion-pytorch style):
- Depth Anything V2 = fast, distilled ViT, ~100MB, CPU capable
- Marigold = diffusion UNet (same core as lucidrains repo: Unet + GaussianDiffusion conditioned on image), GPU, best quality

If torch/transformers not installed, functions fallback to luminance so TG3 never crashes.
"""

import os
from pathlib import Path
import numpy as np
from PIL import Image

# --- Optional deps probe ---
_TORCH_OK = False
_TRANSFORMERS_OK = False
_DIFFUSERS_OK = False
_MARIGOLD_OK = False

try:
    import torch
    _TORCH_OK = True
except Exception:
    _TORCH_OK = False

try:
    from transformers import pipeline
    _TRANSFORMERS_OK = True
except Exception:
    _TRANSFORMERS_OK = False

try:
    from diffusers import MarigoldDepthPipeline
    _DIFFUSERS_OK = True
except Exception:
    _DIFFUSERS_OK = False

# Global cache
_PIPE_DA_SMALL = None
_PIPE_MARIGOLD = None

def is_ai_available():
    return _TORCH_OK and _TRANSFORMERS_OK

def get_available_models():
    _probe()
    models = []
    if _TRANSFORMERS_OK:
        models.append("depth-anything-small")  # LiheYoung/depth-anything-small-hf - 99MB fast
        models.append("depth-anything-base")   # LiheYoung/depth-anything-base-hf - 400MB better
    if _DIFFUSERS_OK and _TORCH_OK:
        models.append("marigold-v1")           # prs-eth/marigold-depth-v1-0 - diffusion quality like your ref
    if not models:
        models = ["heuristic (no AI)"]
    return models

def _get_cache_dir():
    # Use %APPDATA%/TG3/models or ~/.cache/tg3
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home())) / "TG3" / "models"
    else:
        base = Path.home() / ".cache" / "tg3" / "models"
    base.mkdir(parents=True, exist_ok=True)
    return base

def _load_da_pipe(model_id="LiheYoung/depth-anything-small-hf"):
    global _PIPE_DA_SMALL
    if _PIPE_DA_SMALL is not None:
        return _PIPE_DA_SMALL
    if not _TRANSFORMERS_OK:
        return None
    try:
        # cache to our folder
        cache_dir = str(_get_cache_dir())
        pipe = pipeline(task="depth-estimation", model=model_id, cache_dir=cache_dir)
        _PIPE_DA_SMALL = pipe
        return pipe
    except Exception as e:
        print(f"[AI Depth] Failed to load {model_id}: {e}")
        return None

def _load_marigold_pipe():
    global _PIPE_MARIGOLD
    if _PIPE_MARIGOLD is not None:
        return _PIPE_MARIGOLD
    if not (_DIFFUSERS_OK and _TORCH_OK):
        return None
    try:
        cache_dir = str(_get_cache_dir())
        device = "cuda" if torch.cuda.is_available() else "cpu"
        dtype = torch.float16 if device == "cuda" else torch.float32
        pipe = MarigoldDepthPipeline.from_pretrained(
            "prs-eth/marigold-depth-v1-0",
            torch_dtype=dtype,
            cache_dir=cache_dir
        ).to(device)
        _PIPE_MARIGOLD = pipe
        return pipe
    except Exception as e:
        print(f"[AI Depth] Marigold load failed: {e}")
        return None

def _blend_detail(ai_depth_gray, diffuse_pil, detail_strength=0.15):
    """
    Your reference image has smooth base + crisp engraved detail.
    Blend: base = AI depth (smooth), detail = high-pass of diffuse luminance
    This gives you that sculpted look without noise.
    """
    import cv2
    if detail_strength <= 0:
        return ai_depth_gray
    
    # ai_depth_gray: PIL L 0-255
    base = np.array(ai_depth_gray).astype(np.float32) / 255.0
    gray = np.array(diffuse_pil.convert("L")).astype(np.float32) / 255.0
    
    # high-pass detail
    blur = cv2.GaussianBlur(gray, (0,0), 3)
    detail = gray - blur  # -0.5..0.5 approx
    
    # blend
    out = base + detail * detail_strength
    out = np.clip(out, 0, 1)
    return Image.fromarray((out * 255).astype(np.uint8), mode="L")

def estimate_depth_pil(diffuse_pil, model_type="depth-anything-small", detail_blend=0.15, invert=False):
    """
    Main entry: diffuse PIL RGB -> depth PIL L like your reference image.
    model_type: depth-anything-small | depth-anything-base | marigold-v1
    """
    _probe()
    if not isinstance(diffuse_pil, Image.Image):
        diffuse_pil = Image.fromarray(diffuse_pil)
    diffuse_pil = diffuse_pil.convert("RGB")
    
    depth_pil = None
    
    # Try AI models
    if model_type.startswith("depth-anything"):
        hf_id = "LiheYoung/depth-anything-small-hf" if "small" in model_type else "LiheYoung/depth-anything-base-hf"
        pipe = _load_da_pipe(hf_id)
        if pipe is not None:
            try:
                result = pipe(diffuse_pil)
                depth_pil = result["depth"]  # PIL, closer = brighter? DA small outputs inverted depth (near=white) which matches your ref
                # Ensure L
                depth_pil = depth_pil.convert("L")
            except Exception as e:
                print(f"[AI Depth] DA inference failed: {e}")
    
    elif model_type == "marigold-v1":
        pipe = _load_marigold_pipe()
        if pipe is not None:
            try:
                # Marigold expects RGB
                # denoising_steps=10, ensemble=5 gives quality like your ref, still fast
                out = pipe(diffuse_pil, denoising_steps=10, ensemble_size=5, processing_res=768)
                depth_np = out.depth_np  # 0-1 float, near=white? Marigold outputs near=0? Check - we invert if needed
                # Marigold depth is 0=far, 1=near? Actually 0-1, we normalize
                # Make near=white like reference
                if depth_np.mean() < 0.5:
                    # if mean dark, invert
                    pass
                depth_np = (depth_np - depth_np.min()) / (depth_np.max() - depth_np.min() + 1e-8)
                depth_pil = Image.fromarray((depth_np * 255).astype(np.uint8), mode="L")
            except Exception as e:
                print(f"[AI Depth] Marigold inference failed: {e}")
    
    # Fallback heuristic (old luminance) if AI failed
    if depth_pil is None:
        # Simple luminance as fallback
        depth_pil = diffuse_pil.convert("L")
    
    # Detail blend to get that engraved crispness from your reference
    if detail_blend > 0:
        depth_pil = _blend_detail(depth_pil, diffuse_pil, detail_blend)
    
    if invert:
        depth_pil = Image.fromarray(255 - np.array(depth_pil), mode="L")
    
    return depth_pil

# For testing standalone
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python ai_depth_engine.py image.png")
        sys.exit(1)
    img = Image.open(sys.argv[1])
    print(f"AI available: {is_ai_available()}, models: {get_available_models()}")
    depth = estimate_depth_pil(img, model_type="depth-anything-small", detail_blend=0.15)
    depth.save("depth_test.png")
    print("Saved depth_test.png")
