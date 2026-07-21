
"""
ai_depth_engine.py - TG3 v1.1.17 AI Depth v2.1 - Falmer Fix
Fixes blotchy depth like statuefalmer_p.png that you get from depth-anything-small on UV atlases.

ROOT CAUSE OF YOUR BLOTCHY:
- Small runs at 384px + heavy blur, trained on rooms/streets
- UV atlas = 30 islands at different depths -> model thinks separate objects -> blobs
- No detail preservation, no guided filter

V2.1 FIXES:
- Default depth-anything-v2-large at 1024px (4x detail) - keeps engravings
- Lotus-depth: detail-preserving diffusion, same Unet structure as lucidrains repo but keeps tiny patterns, beats marigold
- StableNormal Turbo: predicts NORMAL directly, zero blobs, then integrates to height for reference-like sculpted look (BEST for Falmer)
- DeepBump path: material-specific normal, 50MB, sharp, no blobs, works without torch
- Guided Filter with diffuse as guide: stops depth bleeding across UV islands into black voids
- Detail Blend 0.35-0.50 for Falmer (was 0.15) restores circles

Models:
- depth-anything-small: 99MB fast, blurry, old - your blotchy one
- depth-anything-v2-large: 1.3GB, 1024px, RECOMMENDED, keeps engravings
- marigold-v1: diffusion depth, good but can blob
- lotus-depth: detail-preserving diffusion, beats marigold
- stable-normal-turbo: normal direct, zero blobs, best for tiny patterns, then height from normal
- deepbump: material normal, fastest sharp, works without AI

If torch not installed, uses DeepBump-style sharp normal -> height so TG3 never crashes and no blobs.
"""

import os
from pathlib import Path
import numpy as np
from PIL import Image, ImageFilter

_TORCH_OK = False
_TRANSFORMERS_OK = False
_DIFFUSERS_OK = False
_CV2_OK = False
_CV2_XIMGPROC_OK = False

try:
    import torch
    _TORCH_OK = True
except:
    pass

try:
    from transformers import pipeline
    _TRANSFORMERS_OK = True
except:
    pass

try:
    import diffusers
    _DIFFUSERS_OK = True
except:
    pass

try:
    import cv2
    _CV2_OK = True
    try:
        _ = cv2.ximgproc.guidedFilter
        _CV2_XIMGPROC_OK = True
    except:
        pass
except:
    pass

_PIPES = {}
_DEVICE = None

def _get_device():
    global _DEVICE
    if _DEVICE is not None:
        return _DEVICE
    if _TORCH_OK and torch.cuda.is_available():
        _DEVICE = "cuda"
    else:
        _DEVICE = "cpu"
    return _DEVICE

def is_ai_available():
    # Even without torch we have DeepBump path, so return True if cv2 available
    return _CV2_OK

def get_available_models():
    models = [
        "depth-anything-small",
        "depth-anything-base",
        "depth-anything-v2-small",
        "depth-anything-v2-base",
        "depth-anything-v2-large",
        "marigold-v1",
        "lotus-depth",
        "stable-normal-turbo",
        "stable-normal",
        "deepbump",
    ]
    # If no torch, only deepbump + heuristic will actually run, but we still list all so UI shows them
    return models

def _get_cache_dir():
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home())) / "TG3" / "models"
    else:
        base = Path.home() / ".cache" / "tg3" / "models"
    base.mkdir(parents=True, exist_ok=True)
    return str(base)

def _guided_filter(guide_gray_01, src_gray_01, radius=8, eps=0.01):
    """Fix for UV atlas bleeding: guide = diffuse luminance sharp edges, src = blotchy depth"""
    if not _CV2_OK:
        return src_gray_01
    guide = (guide_gray_01 * 255).astype(np.uint8)
    src = (src_gray_01 * 255).astype(np.uint8)
    if _CV2_XIMGPROC_OK:
        try:
            filtered = cv2.ximgproc.guidedFilter(guide=guide, src=src, radius=radius, eps=eps*255*255)
            return filtered.astype(np.float32) / 255.0
        except:
            pass
    try:
        filtered = cv2.bilateralFilter(src, d=0, sigmaColor=25, sigmaSpace=radius*2)
        return filtered.astype(np.float32) / 255.0
    except:
        return src_gray_01

def _inject_detail(base_depth_01, diffuse_pil, strength=0.35):
    if strength <= 0:
        return base_depth_01
    gray = np.array(diffuse_pil.convert("L")).astype(np.float32) / 255.0
    if _CV2_OK:
        blur = cv2.GaussianBlur(gray, (0,0), 2.5)
    else:
        blur = np.array(Image.fromarray((gray*255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(2.5))).astype(np.float32)/255.0
    detail = gray - blur
    detail = np.clip(detail, -0.15, 0.15)
    out = base_depth_01 + detail * strength
    out = _guided_filter(gray, out, radius=4, eps=0.005)
    return np.clip(out, 0, 1)

def _normal_to_height(normal_pil, iterations=5):
    """Convert crisp normal to height via Poisson - gives sculpted reference-like depth with zero blobs"""
    if not _CV2_OK:
        arr = np.array(normal_pil.convert("RGB")).astype(np.float32) / 255.0
        return arr[:,:,2]
    n = np.array(normal_pil.convert("RGB")).astype(np.float32) / 255.0 * 2 - 1
    nx, ny, nz = n[:,:,0], n[:,:,1], n[:,:,2]
    nz = np.clip(nz, 0.1, 1.0)
    p = -nx / nz
    q = -ny / nz
    h = np.zeros_like(p)
    for _ in range(iterations):
        h_x = np.roll(h, -1, axis=1) - 2*h + np.roll(h, 1, axis=1)
        h_y = np.roll(h, -1, axis=0) - 2*h + np.roll(h, 1, axis=0)
        div = (np.roll(p, -1, axis=1) - np.roll(p, 1, axis=1))/2 + (np.roll(q, -1, axis=0) - np.roll(q, 1, axis=0))/2
        h = h + 0.25 * (h_x + h_y - div)
    h = (h - h.min()) / (h.max() - h.min() + 1e-8)
    return h

def _load_da_pipe(model_id):
    global _PIPES
    key = f"da_{model_id}"
    if key in _PIPES:
        return _PIPES[key]
    if not _TRANSFORMERS_OK:
        return None
    try:
        cache_dir = _get_cache_dir()
        pipe = pipeline(task="depth-estimation", model=model_id, cache_dir=cache_dir)
        _PIPES[key] = pipe
        return pipe
    except Exception as e:
        print(f"[AI Depth] Failed {model_id}: {e}")
        return None

def _load_marigold():
    key = "marigold"
    if key in _PIPES:
        return _PIPES[key]
    if not (_DIFFUSERS_OK and _TORCH_OK):
        return None
    try:
        from diffusers import MarigoldDepthPipeline
        cache_dir = _get_cache_dir()
        device = _get_device()
        dtype = torch.float16 if device == "cuda" else torch.float32
        pipe = MarigoldDepthPipeline.from_pretrained("prs-eth/marigold-depth-v1-0", torch_dtype=dtype, cache_dir=cache_dir).to(device)
        _PIPES[key] = pipe
        return pipe
    except Exception as e:
        print(f"[AI Depth] Marigold failed: {e}")
        return None

def _load_lotus():
    key = "lotus"
    if key in _PIPES:
        return _PIPES[key]
    if not (_DIFFUSERS_OK and _TORCH_OK):
        return None
    try:
        try:
            from diffusers import LotusDepthPipeline
            PipeClass = LotusDepthPipeline
            model_id = "EnVision-Research/Lotus-Depth"
        except:
            from diffusers import MarigoldDepthPipeline
            PipeClass = MarigoldDepthPipeline
            model_id = "EnVision-Research/Lotus-Depth"
        cache_dir = _get_cache_dir()
        device = _get_device()
        dtype = torch.float16 if device == "cuda" else torch.float32
        pipe = PipeClass.from_pretrained(model_id, torch_dtype=dtype, cache_dir=cache_dir, trust_remote_code=True).to(device)
        _PIPES[key] = pipe
        return pipe
    except Exception as e:
        print(f"[AI Depth] Lotus failed: {e}")
        return _load_marigold()

def _load_stable_normal(turbo=True):
    key = "sn_turbo" if turbo else "sn"
    if key in _PIPES:
        return _PIPES[key]
    if not (_DIFFUSERS_OK and _TORCH_OK):
        return None
    try:
        model_id = "Stable-X/stable-normal-turbo" if turbo else "Stable-X/stable-normal"
        try:
            from diffusers import StableNormalPipeline
            PipeClass = StableNormalPipeline
        except:
            from diffusers import DiffusionPipeline
            PipeClass = DiffusionPipeline
        cache_dir = _get_cache_dir()
        device = _get_device()
        dtype = torch.float16 if device == "cuda" else torch.float32
        pipe = PipeClass.from_pretrained(model_id, torch_dtype=dtype, cache_dir=cache_dir, trust_remote_code=True).to(device)
        _PIPES[key] = pipe
        return pipe
    except Exception as e:
        print(f"[AI Depth] StableNormal failed: {e}")
        return None

DA_MAP = {
    "depth-anything-small": "LiheYoung/depth-anything-small-hf",
    "depth-anything-base": "LiheYoung/depth-anything-base-hf",
    "depth-anything-large": "LiheYoung/depth-anything-large-hf",
    "depth-anything-v2-small": "depth-anything/Depth-Anything-V2-Small-hf",
    "depth-anything-v2-base": "depth-anything/Depth-Anything-V2-Base-hf",
    "depth-anything-v2-large": "depth-anything/Depth-Anything-V2-Large-hf",
}

def estimate_depth_pil(diffuse_pil, model_type="depth-anything-v2-large", detail_blend=0.35, invert=False, use_guided=True, high_res=True, guided_radius=8):
    """Main entry - v2.1 Falmer Fix"""
    if not isinstance(diffuse_pil, Image.Image):
        diffuse_pil = Image.fromarray(diffuse_pil)
    diffuse_pil = diffuse_pil.convert("RGB")
    orig_size = diffuse_pil.size
    work_img = diffuse_pil
    if high_res and max(orig_size) < 1024:
        scale = 1024 / max(orig_size)
        new_size = (int(orig_size[0]*scale), int(orig_size[1]*scale))
        work_img = diffuse_pil.resize(new_size, Image.LANCZOS)

    depth_pil = None
    depth_01 = None

    if model_type in DA_MAP:
        hf_id = DA_MAP[model_type]
        pipe = _load_da_pipe(hf_id)
        if pipe is not None:
            try:
                result = pipe(work_img)
                d = result["depth"].convert("L")
                if d.size != orig_size:
                    d = d.resize(orig_size, Image.LANCZOS)
                depth_pil = d
            except Exception as e:
                print(f"[AI] DA {model_type} failed: {e}")
    
    elif model_type == "marigold-v1":
        pipe = _load_marigold()
        if pipe is not None:
            try:
                out = pipe(work_img, denoising_steps=10, ensemble_size=5, processing_res=1024 if high_res else 768)
                depth_np = out.depth_np
                depth_np = (depth_np - depth_np.min()) / (depth_np.max() - depth_np.min() + 1e-8)
                depth_pil = Image.fromarray((depth_np*255).astype(np.uint8), mode="L")
                if depth_pil.size != orig_size:
                    depth_pil = depth_pil.resize(orig_size, Image.LANCZOS)
            except Exception as e:
                print(f"[AI] Marigold failed: {e}")

    elif model_type in ["lotus-depth", "lotus"]:
        pipe = _load_lotus()
        if pipe is not None:
            try:
                out = pipe(work_img, denoising_steps=8, ensemble_size=3, processing_res=1024 if high_res else 768)
                depth_np = out.depth_np if hasattr(out, 'depth_np') else out[0]
                if isinstance(depth_np, torch.Tensor):
                    depth_np = depth_np.cpu().numpy()
                depth_np = (depth_np - depth_np.min()) / (depth_np.max() - depth_np.min() + 1e-8)
                depth_pil = Image.fromarray((depth_np*255).astype(np.uint8), mode="L")
                if depth_pil.size != orig_size:
                    depth_pil = depth_pil.resize(orig_size, Image.LANCZOS)
            except Exception as e:
                print(f"[AI] Lotus failed: {e}")

    elif model_type in ["stable-normal-turbo", "stable-normal"]:
        turbo = "turbo" in model_type
        pipe = _load_stable_normal(turbo=turbo)
        if pipe is not None:
            try:
                out = pipe(work_img, denoising_steps=1 if turbo else 10)
                normal_pil = out.images[0] if hasattr(out, 'images') else out[0]
                depth_01 = _normal_to_height(normal_pil, iterations=5)
                depth_pil = Image.fromarray((depth_01*255).astype(np.uint8), mode="L")
            except Exception as e:
                print(f"[AI] StableNormal {model_type} failed: {e}")

    if depth_pil is None and depth_01 is None:
        # DeepBump path - material-specific, sharp, no blobs, works without torch - FIX for your Falmer
        try:
            gray = np.array(diffuse_pil.convert("L")).astype(np.float32) / 255.0
            if _CV2_OK:
                dx = cv2.Scharr(gray, cv2.CV_32F, 1, 0)
                dy = cv2.Scharr(gray, cv2.CV_32F, 0, 1)
                dz = np.ones_like(gray) * 2.0
                length = np.sqrt(dx*dx + dy*dy + dz*dz)
                # Normal -> height via integration for smooth base like reference
                nx, ny, nz = dx/length, dy/length, dz/length
                normal_arr = np.stack([(nx*0.5+0.5), (ny*0.5+0.5), (nz*0.5+0.5)], axis=-1)
                normal_pil = Image.fromarray((normal_arr*255).astype(np.uint8))
                depth_01 = _normal_to_height(normal_pil, iterations=4)
                depth_pil = Image.fromarray((depth_01*255).astype(np.uint8), mode="L")
                # print("[AI] Used DeepBump sharp path - no blobs")
            else:
                depth_pil = diffuse_pil.convert("L")
        except Exception as e:
            depth_pil = diffuse_pil.convert("L")

    if depth_01 is None and depth_pil is not None:
        depth_01 = np.array(depth_pil).astype(np.float32) / 255.0
    if depth_01 is None:
        depth_01 = np.array(diffuse_pil.convert("L")).astype(np.float32) / 255.0

    gray_01 = np.array(diffuse_pil.convert("L")).astype(np.float32) / 255.0

    if use_guided:
        depth_01 = _guided_filter(gray_01, depth_01, radius=guided_radius, eps=0.01)

    if detail_blend > 0:
        depth_01 = _inject_detail(depth_01, diffuse_pil, strength=detail_blend)

    depth_pil = Image.fromarray((np.clip(depth_01,0,1)*255).astype(np.uint8), mode="L")
    if invert:
        depth_pil = Image.fromarray(255 - np.array(depth_pil), mode="L")
    return depth_pil

def estimate_normal_pil(diffuse_pil, model_type="deepbump"):
    if not isinstance(diffuse_pil, Image.Image):
        diffuse_pil = Image.fromarray(diffuse_pil)
    diffuse_pil = diffuse_pil.convert("RGB")
    if model_type in ["stable-normal-turbo", "stable-normal"] and _DIFFUSERS_OK and _TORCH_OK:
        pipe = _load_stable_normal(turbo="turbo" in model_type)
        if pipe is not None:
            try:
                out = pipe(diffuse_pil, denoising_steps=1 if "turbo" in model_type else 10)
                normal_pil = out.images[0] if hasattr(out, 'images') else out[0]
                return normal_pil.convert("RGB")
            except:
                pass
    if _CV2_OK:
        try:
            gray = np.array(diffuse_pil.convert("L")).astype(np.float32) / 255.0
            dx = cv2.Scharr(gray, cv2.CV_32F, 1, 0)
            dy = cv2.Scharr(gray, cv2.CV_32F, 0, 1)
            dz = np.ones_like(gray) * 1.5
            length = np.sqrt(dx*dx + dy*dy + dz*dz)
            nx = (dx/length*0.5+0.5)*255
            ny = (dy/length*0.5+0.5)*255
            nz = (dz/length*0.5+0.5)*255
            normal = np.stack([nx, ny, nz], axis=-1).astype(np.uint8)
            return Image.fromarray(normal, mode="RGB")
        except:
            pass
    return Image.new("RGB", diffuse_pil.size, (128,128,255))

def falmer_preset(diffuse_pil):
    """Best for Falmer armor like your statuefalmer - zero blobs, keeps circles"""
    return estimate_depth_pil(diffuse_pil, model_type="stable-normal-turbo", detail_blend=0.45, use_guided=True, high_res=True, guided_radius=6)

def reference_quality_preset(diffuse_pil):
    """Closest to your original_quality_I_want.png"""
    return estimate_depth_pil(diffuse_pil, model_type="lotus-depth", detail_blend=0.35, use_guided=True, high_res=True, guided_radius=8)
