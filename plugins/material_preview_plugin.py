"""
Material Preview (Live GL) - a real-time viewport (Sphere/Cube/Cylinder/
Plane) with Cook-Torrance shading, rendered directly into the shell's own
live GL context and displayed with imgui.image() every frame.

This is the clearest before/after of the whole rewrite: the Tkinter version
(material_preview.py + gpu_preview.py) had to spin up a fresh standalone GL
context, render one still frame to a PIL image, hand it back to the Tk
thread, and repeat that whole cycle after every drag - so orbiting felt like
"drag, release, wait, see result". Here the shader source and mesh
generators are reused UNCHANGED from gpu_preview.py (imported, not
copy-pasted), but the render target is a persistent FBO in the app's shared
context, so orbiting is a genuine real-time drag.
"""
import math
import os

import numpy as np
import moderngl
from PIL import Image
from imgui_bundle import imgui

from app.plugin_base import TG3Plugin
from app.file_dialogs import pick_file

import gpu_preview as gp  # reuse VERT_SRC / FRAG_SRC / mesh generators as-is

SHAPES = ["sphere", "cube", "cylinder", "plane"]
MAP_SLOTS = [("albedo", "Diffuse / Albedo"), ("normal", "Normal"), ("rmaos", "RMAOS")]


class MaterialPreviewPlugin(TG3Plugin):
    name = "Material Preview (Live GL)"
    icon = "\u25c9"
    order = 80

    def __init__(self, app):
        super().__init__(app)
        self.shape = "sphere"
        self.az, self.el = 0.4, 0.2
        self.zoom = 1.0

        self.metallic_mult = 1.0
        self.smooth_mult = 1.0
        self.ao_power = 1.0
        self.tiling = 1.0
        self.light_intensity = 2.0
        self.light_az = 0.5
        self.light_el = 0.8

        self.map_paths = {"albedo": "", "normal": "", "rmaos": ""}
        self.gl_textures = {}   # slot -> moderngl.Texture
        self.prog = None
        self.vaos = {}          # shape -> moderngl.VertexArray
        self.fbo = None
        self.color_tex = None
        self.depth_rb = None
        self.fbo_size = (0, 0)
        self.dirty = True
        self.status = ""

    # ── lazy GL setup (needs a live context, so can't happen in __init__) ──
    def _ensure_gl(self):
        ctx = self.app.gl
        if self.prog is None:
            self.prog = ctx.program(vertex_shader=gp.VERT_SRC, fragment_shader=gp.FRAG_SRC)
        if not self.vaos:
            gens = {"sphere": gp._gen_sphere, "cube": gp._gen_cube,
                    "cylinder": gp._gen_cylinder, "plane": gp._gen_plane}
            for shape, gen in gens.items():
                verts, idx = gen()
                vbo = ctx.buffer(verts.tobytes())
                ibo = ctx.buffer(idx.tobytes())
                self.vaos[shape] = ctx.vertex_array(
                    self.prog, [(vbo, "3f 3f 3f 2f", "in_position", "in_normal",
                                 "in_tangent", "in_uv")], index_buffer=ibo)

    def _ensure_fbo(self, w, h):
        ctx = self.app.gl
        if self.fbo is not None and self.fbo_size == (w, h):
            return
        for obj in (self.fbo, self.color_tex, self.depth_rb):
            if obj is not None:
                obj.release()
        self.color_tex = ctx.texture((w, h), 4)
        self.depth_rb = ctx.depth_renderbuffer((w, h))
        self.fbo = ctx.framebuffer(color_attachments=[self.color_tex],
                                    depth_attachment=self.depth_rb)
        self.fbo_size = (w, h)

    def _load_map(self, slot):
        f = pick_file(self.app.input_dir,
                       [("Images", "*.png *.jpg *.jpeg *.bmp *.tga"), ("All files", "*.*")])
        if not f:
            return
        try:
            ctx = self.app.gl
            channels = 4 if slot == "rmaos" else 3
            img = Image.open(f).convert("RGBA" if channels == 4 else "RGB")
            img = img.transpose(Image.FLIP_TOP_BOTTOM)

            old = self.gl_textures.get(slot)
            if old is not None:
                old.release()

            tex = ctx.texture(img.size, channels, img.tobytes())
            tex.filter = (moderngl.LINEAR, moderngl.LINEAR_MIPMAP_LINEAR)
            tex.repeat_x = tex.repeat_y = True
            tex.build_mipmaps()

            self.gl_textures[slot] = tex
            self.map_paths[slot] = f
            self.dirty = True
            self.status = f"Loaded {slot}: {os.path.basename(f)}"
        except Exception as e:
            self.status = f"Load failed: {e}"

    # ── render ───────────────────────────────────────────────────────────
    def _render(self, w, h):
        self._ensure_gl()
        self._ensure_fbo(w, h)
        ctx = self.app.gl

        rot = gp._rot_x(self.el) @ gp._rot_y(self.az)
        rot3 = rot.astype(np.float32)
        model = np.eye(4, dtype=np.float32)
        model[:3, :3] = rot
        cam_dist = 2.8 / max(self.zoom, 0.05)
        view = gp._translate(-cam_dist)
        proj = gp._perspective(35.0, w / max(h, 1), 0.1, 10.0)
        mvp = proj @ view @ model

        lx = math.cos(self.light_el) * math.sin(self.light_az)
        ly = math.sin(self.light_el)
        lz = math.cos(self.light_el) * math.cos(self.light_az)

        self.fbo.use()
        ctx.enable(moderngl.DEPTH_TEST)
        ctx.viewport = (0, 0, w, h)
        self.fbo.clear(0.07, 0.07, 0.08, 1.0)

        prog = self.prog
        prog["mvp"].write(mvp.T.tobytes())
        prog["rot"].write(rot3.T.tobytes())
        prog["camPos"].value = (0.0, 0.0, cam_dist)
        prog["lightDir"].value = (float(lx), float(ly), float(lz))
        prog["lightColor"].value = (1.0, 1.0, 1.0)
        prog["ambientColor"].value = (0.04, 0.04, 0.05)
        prog["metallicMult"].value = self.metallic_mult
        prog["smoothnessMult"].value = self.smooth_mult
        prog["aoPower"].value = self.ao_power
        prog["tiling"].value = self.tiling
        prog["offsetU"].value = 0.0
        prog["offsetV"].value = 0.0
        prog["lightIntensity"].value = self.light_intensity

        for i, slot in enumerate(("albedo", "normal", "rmaos")):
            has = slot in self.gl_textures
            prog[f"has{slot.capitalize()}"].value = 1 if has else 0
            if has:
                self.gl_textures[slot].use(i)
                prog[f"{slot}Tex"].value = i

        self.vaos[self.shape].render(moderngl.TRIANGLES)
        ctx.screen.use()
        self.dirty = False

    # ── ui ───────────────────────────────────────────────────────────────
    def gui(self):
        for slot, label in MAP_SLOTS:
            if imgui.button(f"Load {label}##{slot}"):
                self._load_map(slot)
            imgui.same_line()
            imgui.text_disabled(os.path.basename(self.map_paths[slot]) or "(none)")

        imgui.separator()
        for shape in SHAPES:
            clicked = imgui.radio_button(shape.capitalize(), self.shape == shape)
            imgui.same_line()
            if clicked:
                self.shape = shape
                self.dirty = True
        imgui.new_line()

        changed = False
        c, self.metallic_mult = imgui.slider_float("Metallic x", self.metallic_mult, 0.0, 2.0); changed |= c
        c, self.smooth_mult = imgui.slider_float("Smoothness x", self.smooth_mult, 0.0, 2.0); changed |= c
        c, self.ao_power = imgui.slider_float("AO Power", self.ao_power, 0.1, 4.0); changed |= c
        c, self.tiling = imgui.slider_float("Tiling", self.tiling, 0.25, 8.0); changed |= c
        c, self.light_intensity = imgui.slider_float("Light Intensity", self.light_intensity, 0.0, 6.0); changed |= c
        c, self.light_az = imgui.slider_float("Light Azimuth", self.light_az, -math.pi, math.pi); changed |= c
        c, self.light_el = imgui.slider_float("Light Elevation", self.light_el, -1.5, 1.5); changed |= c
        c, self.zoom = imgui.slider_float("Zoom", self.zoom, 0.1, 8.0); changed |= c
        if changed:
            self.dirty = True

        imgui.separator()
        avail = imgui.get_content_region_avail()
        w = max(int(avail.x - 8), 64)
        h = max(int(avail.y - 30), 64)

        imgui.begin_child("##viewport", imgui.ImVec2(w, h), True)
        io = imgui.get_io()
        if imgui.is_window_hovered() and imgui.is_mouse_dragging(0):
            dx, dy = io.mouse_delta.x, io.mouse_delta.y
            if dx or dy:
                self.az += dx * 0.008
                self.el = float(np.clip(self.el - dy * 0.008, -1.4, 1.4))
                self.dirty = True
        if imgui.is_window_hovered() and io.mouse_wheel != 0:
            factor = 1.1 if io.mouse_wheel > 0 else 1 / 1.1
            self.zoom = float(np.clip(self.zoom * factor, 0.1, 8.0))
            self.dirty = True

        if self.dirty or self.fbo_size != (w, h):
            try:
                self._render(w, h)
            except Exception as e:
                self.status = f"Render error: {e}"

        if self.color_tex is not None:
            imgui.image(self.color_tex.glo, imgui.ImVec2(w - 8, h - 8),
                        uv0=imgui.ImVec2(0, 1), uv1=imgui.ImVec2(1, 0))
        imgui.end_child()

        imgui.text_disabled(self.status or "Drag to orbit, scroll to zoom - both in real time.")

    def on_shutdown(self):
        for tex in self.gl_textures.values():
            tex.release()
        for vao in self.vaos.values():
            vao.release()
        for obj in (self.fbo, self.color_tex, self.depth_rb, self.prog):
            if obj is not None:
                obj.release()
