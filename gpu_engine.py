# gpu_engine.py
# Minimal ModernGL compute wrapper for TG3
# pip install moderngl glcontext numpy pillow

import moderngl
import numpy as np
from PIL import Image
from pathlib import Path
import sys, os

def resource_path(rel):
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)

_ctx = None
_progs = {}
_gpu_probe_result = None


def gpu_available():
    """One-time cached probe: can we create a standalone GL context here at all?
    Cached because context creation is the expensive/failure-prone step
    (no GPU, missing/old drivers, headless RDP session, etc.)."""
    global _gpu_probe_result
    if _gpu_probe_result is None:
        try:
            _ctx_get()
            _gpu_probe_result = True
        except Exception:
            _gpu_probe_result = False
    return _gpu_probe_result


def _ctx_get():
    global _ctx
    if _ctx is None:
        _ctx = moderngl.create_standalone_context(require=430)
    return _ctx

def _prog(name):
    if name in _progs:
        return _progs[name]
    ctx = _ctx_get()
    shader_path = resource_path(f"shaders/{name}.comp")
    with open(shader_path, 'r') as f:
        src = f.read()
    prog = ctx.compute_shader(src)
    _progs[name] = prog
    return prog


def _run_normal_shader(arr, strength, gradMul, use_x, use_y, flip_x, flip_y, full_z, z_mode):
    """arr: HxW float32 array in [0,1] (the height/luminance field). Returns HxW x3 uint8 RGB array."""
    ctx = _ctx_get()
    h, w = arr.shape

    src_tex = ctx.texture((w, h), 1, np.ascontiguousarray(arr, dtype=np.float32).tobytes(), dtype='f4')
    src_tex.bind_to_image(0, read=True, write=False, format=moderngl.R32F)

    dst_tex = ctx.texture((w, h), 4, dtype='f4')
    dst_tex.bind_to_image(1, write=True, format=moderngl.RGBA32F)

    prog = _prog("normal")
    prog["strength"].value = float(strength)
    prog["gradMul"].value = float(gradMul)
    prog["useX"].value = 1 if use_x else 0
    prog["useY"].value = 1 if use_y else 0
    prog["flipX"].value = -1.0 if flip_x else 1.0
    prog["flipY"].value = -1.0 if flip_y else 1.0
    prog["fullZ"].value = 1 if full_z else 0
    prog["zMode"].value = int(z_mode)

    gx = (w + 15) // 16
    gy = (h + 15) // 16
    prog.run(gx, gy, 1)

    data = dst_tex.read()
    out = np.frombuffer(data, dtype=np.float32).reshape(h, w, 4)
    rgb = (np.clip(out[:, :, :3], 0, 1) * 255).astype(np.uint8)

    src_tex.release()
    dst_tex.release()
    return rgb


def gpu_normal_from_height(height_img: Image.Image, strength: float = 6.0) -> Image.Image:
    """Original TG3 height-field -> normal map path, used by height_map_builder.py.
    Behavior/formula unchanged from the original single-purpose shader."""
    gray = height_img.convert("L")
    arr = np.array(gray, dtype=np.float32) / 255.0
    rgb = _run_normal_shader(arr, strength=strength, gradMul=0.25,
                              use_x=True, use_y=True, flip_x=False, flip_y=False,
                              full_z=False, z_mode=0)
    return Image.fromarray(rgb, mode="RGB")


def gpu_normal_from_image(img: Image.Image, scale: float = 10.0, use_x: bool = True,
                           use_y: bool = True, flip_x: bool = False, flip_y: bool = False,
                           full_z: bool = False) -> Image.Image:
    """GPU-accelerated equivalent of texture_generator.generate_normal_map().
    Same signature/parameter semantics as the CPU function (drop-in for the
    Normal Map Generator tab), just computed with a Sobel compute shader
    instead of a NumPy central-difference gradient."""
    gray = img.convert("L")
    arr = np.array(gray, dtype=np.float32) / 255.0
    rgb = _run_normal_shader(arr, strength=scale, gradMul=0.25,
                              use_x=use_x, use_y=use_y, flip_x=flip_x, flip_y=flip_y,
                              full_z=full_z, z_mode=1)
    return Image.fromarray(rgb, mode="RGB")
