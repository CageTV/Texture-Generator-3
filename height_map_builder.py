# height_map_builder.py
import os
from pathlib import Path
import numpy as np
from PIL import Image, ImageFilter
import subprocess

import platform_tools as _pt

STRENGTH = 6.0
BLUR_RADIUS = 2.5
GRAD_MULTIPLIER = 0.25
NORMALIZE_HEIGHT = False
SKIP_SUFFIXES = ("_n","_g","_m","_p","_r","_ao","_h","_s","_e","_orm","_rmaos","_emissive")

try:
    from gpu_engine import gpu_normal_from_height
    _GPU_OK = True
except Exception as e:
    print(f"[GPU] disabled: {e}")
    _GPU_OK = False

def get_height_field(image, blur=BLUR_RADIUS):
    gray = image.convert("L")
    if blur > 0:
        gray = gray.filter(ImageFilter.GaussianBlur(blur))
    h = np.array(gray, dtype=np.float32) / 255.0
    if NORMALIZE_HEIGHT:
        mn, mx = h.min(), h.max()
        if mx > mn:
            h = (h - mn) / (mx - mn)
    return h

def generate_normal_from_height(h, strength=STRENGTH):
    if _GPU_OK:
        height_img = Image.fromarray((np.clip(h,0,1)*255).astype(np.uint8), mode="L")
        return gpu_normal_from_height(height_img, strength)
    sx = np.array([[-1,0,1],[-2,0,2],[-1,0,1]], np.float32)
    sy = np.array([[-1,-2,-1],[0,0,0],[1,2,1]], np.float32)
    dx = np.zeros_like(h); dy = np.zeros_like(h)
    for y in range(1, h.shape[0]-1):
        for x in range(1, h.shape[1]-1):
            r = h[y-1:y+2, x-1:x+2]
            dx[y,x] = np.sum(r*sx) * GRAD_MULTIPLIER
            dy[y,x] = np.sum(r*sy) * GRAD_MULTIPLIER
    dz = np.ones_like(dx) / max(strength, 0.01)
    n = np.stack((dx,dy,dz), axis=2)
    n /= np.clip(np.linalg.norm(n, axis=2, keepdims=True), 1e-8, None)
    return Image.fromarray(((n+1)*0.5*255).astype(np.uint8), mode="RGB")

def generate_height_map(h):
    return Image.fromarray((np.clip(h,0,1)*255).astype(np.uint8), mode="L")

def should_skip(name):
    stem = Path(name).stem.lower()
    return any(stem.endswith(s) for s in SKIP_SUFFIXES)

def save_with_formats(img, base_path, formats, texconv_path=None):
    base_path = Path(base_path)
    base_path.parent.mkdir(parents=True, exist_ok=True)
    for fmt in formats:
        fmt = fmt.lower()
        out = base_path.with_suffix(f".{fmt}")
        if fmt in ("png","bmp","tga","jpg","jpeg"):
            save_fmt = "JPEG" if fmt in ("jpg","jpeg") else fmt.upper()
            img.save(out, save_fmt)
        elif fmt == "dds":
            bc = "BC5_UNORM" if base_path.name.endswith("_n") else "BC4_UNORM"
            tmp = base_path.with_suffix(".png")
            img.save(tmp, "PNG")
            _pt.dds_encode(tmp, out, bc)
            try: tmp.unlink()
            except: pass

def process_folder(in_dir, out_dir, formats=("png",), strength=STRENGTH,
                   blur=BLUR_RADIUS, texconv_path=None, progress=None):
    in_dir = Path(in_dir); out_dir = Path(out_dir)
    if not in_dir.is_dir(): return 0
    exts = {".png",".bmp",".tga",".jpg",".jpeg",".tif",".tiff"}
    files = [p for p in in_dir.rglob("*") if p.suffix.lower() in exts]
    total = len(files); processed = 0
    for idx, src in enumerate(files, 1):
        if progress:
            try: progress(idx, total)
            except: pass
        if should_skip(src.name): continue
        try:
            img = Image.open(src).convert("RGB")
            h = get_height_field(img, blur)
            normal = generate_normal_from_height(h, strength)
            height = generate_height_map(h)
            rel = src.relative_to(in_dir).with_suffix("")
            save_with_formats(normal, out_dir / f"{rel}_n", formats, texconv_path)
            save_with_formats(height, out_dir / f"{rel}_p", formats, texconv_path)
            processed += 1
        except Exception as e:
            print(f"[HMB] Error {src}: {e}")
    return processed