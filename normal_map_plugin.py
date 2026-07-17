"""
Normal Map Generator - GL-shell port of texture_generator.py's
'Normal Map Generator' tab.

This is the proof-of-concept plugin: it drives the SAME gpu_engine.py
GPU-compute-shader path (with the same CPU NumPy fallback math) unchanged,
and displays both the source and result images as live GL textures instead
of the Tkinter version's PIL -> ImageTk.PhotoImage round trip.
"""
import os
import numpy as np
from PIL import Image
from imgui_bundle import imgui

from app.plugin_base import TG3Plugin
from app.gl_utils import pil_to_texture, texture_imgui_id
from app.file_dialogs import pick_file, save_file

try:
    import gpu_engine as _ge
    _GPU_OK = _ge.gpu_available()
except Exception:
    _ge = None
    _GPU_OK = False


def _generate_normal_map_cpu(img, scale=10.0, use_x=True, use_y=True,
                              flip_x=False, flip_y=False, full_z=False):
    """Same math as texture_generator.generate_normal_map(), duplicated
    here (rather than imported) so this plugin has no hard dependency on
    the Tkinter app module - plugins should be able to run with just the
    engine files, not the old GUI."""
    arr = np.array(img.convert('L')).astype('float32') / 255.0
    dx = np.gradient(arr, axis=1) * scale if use_x else np.zeros_like(arr)
    dy = np.gradient(arr, axis=0) * scale if use_y else np.zeros_like(arr)
    if flip_x: dx = -dx
    if flip_y: dy = -dy
    dz = np.ones_like(arr)
    length = np.sqrt(dx ** 2 + dy ** 2 + dz ** 2)
    nx = ((dx / length) + 1.0) * 127.5
    ny = ((dy / length) + 1.0) * 127.5
    nz = (dz / length) * 255.0 if full_z else ((dz / length) + 1.0) * 127.5
    return Image.fromarray(np.stack([nx, ny, nz], axis=-1).clip(0, 255).astype('uint8'))


class NormalMapPlugin(TG3Plugin):
    name = "Normal Map Generator"
    icon = "\u25c8"
    order = 40

    def __init__(self, app):
        super().__init__(app)
        self.source_path = ""
        self.source_img = None
        self.result_img = None
        self.source_tex = None
        self.result_tex = None

        self.scale = 10.0
        self.use_x = True
        self.use_y = True
        self.flip_x = False
        self.flip_y = False
        self.full_z = False
        self.use_gpu = _GPU_OK

        self.status = ""
        self.last_backend = ""

    # ── actions ──────────────────────────────────────────────────────────
    def _load_source(self):
        f = pick_file(self.app.input_dir,
                       [("Images", "*.png *.jpg *.jpeg *.bmp *.tga"), ("All files", "*.*")])
        if not f:
            return
        try:
            self.source_path = f
            self.source_img = Image.open(f).convert("RGB")
            self.source_tex = pil_to_texture(self.app.gl, self.source_img, self.source_tex)
            self.status = f"Loaded {os.path.basename(f)}"
        except Exception as e:
            self.status = f"Load failed: {e}"

    def _generate(self):
        if self.source_img is None:
            self.status = "Load a source image first."
            return
        try:
            if self.use_gpu and _GPU_OK:
                self.result_img = _ge.gpu_normal_from_image(
                    self.source_img, self.scale, self.use_x, self.use_y,
                    self.flip_x, self.flip_y, self.full_z)
                self.last_backend = "gpu"
            else:
                self.result_img = _generate_normal_map_cpu(
                    self.source_img, self.scale, self.use_x, self.use_y,
                    self.flip_x, self.flip_y, self.full_z)
                self.last_backend = "cpu"
            self.result_tex = pil_to_texture(self.app.gl, self.result_img, self.result_tex)
            self.status = f"Generated  [{self.last_backend}]"
        except Exception as e:
            self.status = f"Generate failed: {e}"

    def _save(self):
        if self.result_img is None:
            self.status = "Nothing to save yet."
            return
        f = save_file(self.app.output_dir, ".png",
                      [("PNG", "*.png"), ("TGA", "*.tga"), ("BMP", "*.bmp")])
        if not f:
            return
        self.result_img.save(f)
        self.status = f"Saved {f}"

    # ── ui ───────────────────────────────────────────────────────────────
    def gui(self):
        if imgui.button("Load Source Image"):
            self._load_source()
        imgui.same_line()
        imgui.text_disabled(self.source_path or "(none loaded)")

        imgui.separator()
        _, self.scale = imgui.slider_float("Strength", self.scale, 0.5, 30.0)
        _, self.use_x = imgui.checkbox("Use X gradient", self.use_x)
        imgui.same_line()
        _, self.use_y = imgui.checkbox("Use Y gradient", self.use_y)
        _, self.flip_x = imgui.checkbox("Flip X", self.flip_x)
        imgui.same_line()
        _, self.flip_y = imgui.checkbox("Flip Y", self.flip_y)
        _, self.full_z = imgui.checkbox("Full Z (strict tangent-space)", self.full_z)

        gpu_label = "Use GPU compute shader"
        if not _GPU_OK:
            gpu_label += "  (unavailable on this machine - CPU fallback in use)"
            self.use_gpu = False
        _, self.use_gpu = imgui.checkbox(gpu_label, self.use_gpu if _GPU_OK else False)

        imgui.separator()
        if imgui.button("Generate"):
            self._generate()
        imgui.same_line()
        if imgui.button("Save Result..."):
            self._save()
        imgui.same_line()
        imgui.text(self.status)

        imgui.separator()
        avail = imgui.get_content_region_avail()
        col_w = max((avail.x - 20) / 2, 64)
        preview_h = max(min(avail.y - 30, col_w), 64)

        imgui.begin_child("##src_col", imgui.ImVec2(col_w, 0), True)
        imgui.text("Source")
        if self.source_tex:
            imgui.image(texture_imgui_id(self.source_tex), imgui.ImVec2(col_w - 16, preview_h))
        imgui.end_child()

        imgui.same_line()
        imgui.begin_child("##res_col", imgui.ImVec2(col_w, 0), True)
        imgui.text("Normal Map")
        if self.result_tex:
            imgui.image(texture_imgui_id(self.result_tex), imgui.ImVec2(col_w - 16, preview_h))
        imgui.end_child()

    def on_shutdown(self):
        for tex in (self.source_tex, self.result_tex):
            if tex is not None:
                tex.release()
