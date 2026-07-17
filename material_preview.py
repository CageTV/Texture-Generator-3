"""
material_preview.py
PBR material preview — GPU-accelerated (ModernGL rasterizer) when available,
falls back to a pure-NumPy Cook-Torrance software raytracer otherwise.
Renders Sphere / Cube / Cylinder / Plane, normal mapping, parallax, AO,
and a full parameter control window. Same public API regardless of backend.
"""

import numpy as np
from PIL import Image, ImageTk
import tkinter as tk
from tkinter import ttk, colorchooser
import threading
import time
import os
import sys
import traceback
from pathlib import Path

try:
    from gpu_preview import GPUPBRRenderer as _GPURenderer, gpu_available as _gpu_available
    _GPU_PREVIEW_AVAILABLE = _gpu_available()   # probes for a real GL context, not just the import
except Exception:
    _GPU_PREVIEW_AVAILABLE = False

try:
    from ibl_preview import IBLPBRRenderer as _IBLRenderer
    _IBL_PREVIEW_AVAILABLE = _GPU_PREVIEW_AVAILABLE   # same GL requirement, no separate probe needed
except Exception:
    _IBL_PREVIEW_AVAILABLE = False

import queue


# ─── Math helpers ─────────────────────────────────────────────────────────────

def _norm(v):
    return v / (np.linalg.norm(v, axis=-1, keepdims=True) + 1e-8)

def _dot(a, b):
    return np.einsum('...i,...i->...', a, b)

def _rot_y(az):
    c, s = np.cos(az), np.sin(az)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], np.float32)

def _rot_x(el):
    c, s = np.cos(el), np.sin(el)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], np.float32)


# ─── Texture sampling ──────────────────────────────────────────────────────────

def _sample(tex, u, v):
    """Bilinear texture sample. tex: (H,W,C) float32."""
    h, w = tex.shape[:2]
    u = np.nan_to_num(u, nan=0.0, posinf=0.0, neginf=0.0)
    v = np.nan_to_num(v, nan=0.0, posinf=0.0, neginf=0.0)
    u = np.clip(u % 1.0, 0.0, 0.9999)
    v = np.clip(v % 1.0, 0.0, 0.9999)
    x = u * (w - 1);  y = v * (h - 1)
    x0 = np.floor(x).astype(np.int64); x1 = np.minimum(x0 + 1, w - 1)
    y0 = np.floor(y).astype(np.int64); y1 = np.minimum(y0 + 1, h - 1)
    x0 = np.clip(x0, 0, w-1); x1 = np.clip(x1, 0, w-1)
    y0 = np.clip(y0, 0, h-1); y1 = np.clip(y1, 0, h-1)
    fx = (x - np.floor(x))[..., None];  fy = (y - np.floor(y))[..., None]
    return (tex[y0, x0] * (1-fx) * (1-fy) + tex[y0, x1] * fx * (1-fy) +
            tex[y1, x0] * (1-fx) *    fy  + tex[y1, x1] * fx *    fy)


# ─── Ray / shape intersections ────────────────────────────────────────────────

def _isect_sphere(ro, rd):
    b = _dot(ro, rd)
    c = _dot(ro, ro) - 1.0
    h = b * b - c
    t = np.where(h >= 0, -b - np.sqrt(np.maximum(h, 0)), np.inf)
    mask = (h >= 0) & (t > 0.001)
    return np.where(mask, t, np.inf), mask

def _sphere_surface(P):
    N = _norm(P)
    u = (0.5 + np.arctan2(P[..., 2], P[..., 0]) / (2*np.pi)) % 1.0
    v = (0.5 - np.arcsin(np.clip(P[..., 1], -1, 1)) / np.pi) % 1.0
    phi = u * 2 * np.pi
    T = _norm(np.stack([-np.sin(phi), np.zeros_like(phi),  np.cos(phi)], -1))
    B = _norm(np.cross(N, T))
    return u, v, T, B, N


def _isect_box(ro, rd):
    mn, mx = np.float32(-0.92), np.float32(0.92)
    inv = 1.0 / (rd + 1e-12)
    t1 = (mn - ro) * inv;  t2 = (mx - ro) * inv
    tmin = np.minimum(t1, t2).max(-1)
    tmax = np.maximum(t1, t2).min(-1)
    mask = tmax >= tmin
    t = np.where(mask & (tmin > 0.001), tmin,
        np.where(mask & (tmax > 0.001), tmax, np.inf))
    return t, (t < np.inf) & (t > 0.001)

def _box_surface(P):
    aP = np.abs(P)
    ax = np.argmax(aP, axis=-1)
    N  = np.zeros_like(P)
    for i in range(3):
        m = ax == i
        N[m, i] = np.sign(P[m, i])
    # UV: project onto face
    ux = np.select([ax==0, ax==1, ax==2],
                   [(P[...,2]+1)/2, (P[...,0]+1)/2, (P[...,0]+1)/2])
    uy = np.select([ax==0, ax==1, ax==2],
                   [(P[...,1]+1)/2, (P[...,2]+1)/2, (P[...,1]+1)/2])
    # Tangent per face
    T0 = np.zeros_like(P); T0[..., 2] = 1.0   # ax=0 face
    T1 = np.zeros_like(P); T1[..., 0] = 1.0   # ax=1 face
    T2 = np.zeros_like(P); T2[..., 0] = 1.0   # ax=2 face
    T  = np.select([ax[...,None]==0, ax[...,None]==1, ax[...,None]==2], [T0, T1, T2])
    T  = _norm(T - _dot(T, N)[..., None] * N)
    B  = _norm(np.cross(T, N))
    return ux, uy, T, B, N


def _isect_cyl(ro, rd):
    R = 0.85; H = 1.7
    a = np.maximum(rd[...,0]**2 + rd[...,2]**2, 1e-8)
    b = 2*(ro[...,0]*rd[...,0] + ro[...,2]*rd[...,2])
    c = ro[...,0]**2 + ro[...,2]**2 - R**2
    disc = b*b - 4*a*c
    ts  = np.where(disc >= 0, (-b - np.sqrt(np.maximum(disc,0)))/(2*a), np.inf)
    Psy = ro[...,1] + ts * rd[...,1]
    ts  = np.where((ts>0.001) & (np.abs(Psy) < H), ts, np.inf)
    # Caps
    def cap(y0):
        tc = (y0 - ro[...,1]) / (rd[...,1] + 1e-8)
        Pc = ro + tc[...,None]*rd
        return np.where((tc>0.001)&(Pc[...,0]**2+Pc[...,2]**2 < R**2), tc, np.inf)
    tt = np.minimum(cap(H), cap(-H))
    t  = np.minimum(ts, tt)
    return t, t < np.inf

def _cyl_surface(P, ro, rd, t):
    R = 0.85; H = 1.7
    Pr = np.sqrt(P[...,0]**2 + P[...,2]**2)
    side = Pr > R * 0.99
    sign_y = np.sign(P[...,1])
    N_side = _norm(np.stack([P[...,0], np.zeros_like(P[...,0]), P[...,2]], -1))
    N_cap  = np.stack([np.zeros_like(sign_y), sign_y, np.zeros_like(sign_y)], -1)
    N = np.where(side[...,None], N_side, N_cap)
    phi   = np.arctan2(P[...,2], P[...,0])
    u_s   = (phi/(2*np.pi)+0.5)%1; v_s = (P[...,1]+H)/(2*H)
    u_c   = P[...,0]/R*0.5+0.5;     v_c = P[...,2]/R*0.5+0.5
    u = np.where(side, u_s, u_c); v = np.where(side, v_s, v_c)
    T_s = _norm(np.stack([-np.sin(phi), np.zeros_like(phi), np.cos(phi)], -1))
    T_c = np.zeros_like(P); T_c[...,0] = 1.0
    T = np.where(side[...,None], T_s, T_c)
    T = _norm(T - _dot(T, N)[...,None] * N)
    B = _norm(np.cross(T, N))
    return u, v, T, B, N


def _isect_plane(ro, rd):
    t = (-1.0 - ro[...,1]) / (rd[...,1] + 1e-8)
    mask = t > 0.001
    return np.where(mask, t, np.inf), mask

def _plane_surface(P):
    u = (P[...,0] + 2) / 4.0; v = (P[...,2] + 2) / 4.0
    N = np.zeros_like(P); N[...,1] = 1.0
    T = np.zeros_like(P); T[...,0] = 1.0
    B = np.zeros_like(P); B[...,2] = 1.0
    return u % 1.0, v % 1.0, T, B, N


# ─── Parallax mapping ─────────────────────────────────────────────────────────

def _parallax(h_tex, u, v, V_tang, depth, steps=10):
    if h_tex is None or depth < 0.0005:
        return u, v
    du = -V_tang[...,0] * depth / steps
    dv = -V_tang[...,2] * depth / steps
    cu, cv = u.copy(), v.copy()
    layer = 0.0
    for _ in range(steps):
        ht = _sample(h_tex, cu % 1, cv % 1)[...,0]
        done = layer >= ht
        layer += 1.0 / steps
        cu = np.where(done, cu, cu + du)
        cv = np.where(done, cv, cv + dv)
    return cu % 1, cv % 1


# ─── Cook-Torrance PBR ────────────────────────────────────────────────────────

def _ggx_d(NdH, r):
    a = np.maximum(r*r, 0.002)
    d = NdH**2 * (a*a - 1) + 1
    return a*a / (np.pi * d*d + 1e-7)

def _smith_g(NdV, NdL, r):
    k = (r+1)**2 / 8
    gv = NdV / (NdV*(1-k)+k+1e-7)
    gl = NdL / (NdL*(1-k)+k+1e-7)
    return gv * gl

def _fresnel(cos_t, F0):
    return F0 + (1 - F0) * np.power(np.clip(1 - cos_t[..., None], 0, 1), 5)

def _pbr(albedo, N, V, L, rough, metal, ao, lc, amb):
    H    = _norm(V + L)
    NdV  = np.clip(_dot(N, V), 0.001, 1.0)
    NdL  = np.clip(_dot(N, L), 0.0,   1.0)
    NdH  = np.clip(_dot(N, H), 0.0,   1.0)
    VdH  = np.clip(_dot(V, H), 0.0,   1.0)
    F0   = np.where(metal[...,None] > 0.5, albedo,
                    np.full_like(albedo, 0.04))
    F    = _fresnel(VdH, F0)
    D    = _ggx_d(NdH, rough)[...,None]
    G    = _smith_g(NdV, NdL, rough)[...,None]
    spec = D * G * F / (4 * NdV[...,None] * NdL[...,None] + 1e-7)
    kD   = (1 - F) * (1 - metal[...,None])
    diff = kD * albedo / np.pi
    Lo   = (diff + spec) * lc * NdL[...,None]
    return Lo + amb * albedo * ao[...,None]


def _tonemap(c):
    """ACES filmic + gamma."""
    c = (c*(2.51*c+0.03)) / (c*(2.43*c+0.59)+0.14)
    return np.power(np.clip(c, 0, 1), 1/2.2)


# ─── Renderer ─────────────────────────────────────────────────────────────────

class SoftwarePBRRenderer:
    def __init__(self):
        self.textures = {}
        self.az = 0.4
        self.el = 0.2
        self.dist = 2.8

    def load_texture(self, name, source):
        if isinstance(source, np.ndarray):
            arr = source.astype(np.float32) / 255.0
        elif isinstance(source, Image.Image):
            arr = np.array(source).astype(np.float32) / 255.0
        elif source and os.path.isfile(str(source)):
            arr = np.array(Image.open(str(source))).astype(np.float32) / 255.0
        else:
            return
        if arr.ndim == 2:
            arr = arr[..., None]
        self.textures[name] = arr

    def clear(self):
        self.textures.clear()

    def render(self, W=420, H=420, shape='sphere', p=None):
        if p is None:
            p = {}
        mm   = float(p.get('metallic_mult',   1.0))
        sm   = float(p.get('smooth_mult',      1.0))
        aop  = float(p.get('ao_power',         1.0))
        pd   = float(p.get('parallax_depth',   0.0))
        til  = float(p.get('tiling',           1.0))
        ou   = float(p.get('offset_u',         0.0))
        ov   = float(p.get('offset_v',         0.0))
        li   = float(p.get('light_intensity',  2.0))
        laz  = float(p.get('light_az',         0.5))
        lel  = float(p.get('light_el',         0.8))
        lc   = np.array(p.get('light_color',   [1.0,1.0,1.0]), np.float32) * li
        amb  = np.array([0.04, 0.04, 0.05], np.float32)
        BG   = np.array([0.07, 0.07, 0.08], np.float32)

        R    = _rot_x(self.el) @ _rot_y(self.az)
        Ri   = R.T
        cam  = np.array([0, 0, getattr(self, 'dist', 2.8)], np.float32)
        fov  = np.tan(np.radians(35))
        asp  = W / H

        xi   = np.linspace(-asp*fov, asp*fov, W, dtype=np.float32)
        yi   = np.linspace( fov,    -fov,     H, dtype=np.float32)
        xx, yy = np.meshgrid(xi, yi)
        rd_c = _norm(np.stack([xx, yy, -np.ones((H,W), np.float32)], -1))

        # Transform rays to object space
        rd   = np.einsum('ij,...j->...i', Ri, rd_c)
        ro_pt = (Ri @ cam)
        ro   = np.broadcast_to(ro_pt[None,None,:], (H,W,3)).copy()

        # Intersect
        dispatch = {'sphere':   _isect_sphere,
                    'cube':     _isect_box,
                    'cylinder': _isect_cyl,
                    'plane':    _isect_plane}
        t, mask = dispatch.get(shape, _isect_sphere)(ro, rd)

        out = np.broadcast_to(BG, (H,W,3)).copy().astype(np.float32)
        if not mask.any():
            return Image.fromarray((_tonemap(out)*255).astype(np.uint8))

        # Hit points
        P  = ro + t[...,None] * rd

        # Surface geometry
        surf_fn = {'sphere':   _sphere_surface,
                   'cube':     _box_surface,
                   'cylinder': lambda P: _cyl_surface(P, ro, rd, t),
                   'plane':    _plane_surface}
        u, v, T, B, N = surf_fn.get(shape, _sphere_surface)(P)

        # Tiling + offset
        ut = (u * til + ou) % 1.0
        vt = (v * til + ov) % 1.0

        # Parallax
        V_obj  = _norm(-rd)
        V_tang = np.stack([_dot(V_obj,T), _dot(V_obj,B), _dot(V_obj,N)], -1)
        ht     = self.textures.get('height')
        ut, vt = _parallax(ht, ut, vt, V_tang, pd)

        # Sample textures
        ta = self.textures.get('albedo')
        tn = self.textures.get('normal')
        tr = self.textures.get('rmaos')

        albedo = _sample(ta, ut, vt)[...,:3] if ta is not None \
                 else np.full((H,W,3), 0.6, np.float32)

        if tn is not None:
            ns = _sample(tn, ut, vt)[...,:3] * 2.0 - 1.0
            N_s = _norm(ns[...,0:1]*T + ns[...,1:2]*B + ns[...,2:3]*N)
        else:
            N_s = N

        if tr is not None:
            rs = _sample(tr, ut, vt)
            rough = np.clip(rs[...,0] * max(2.0 - sm, 0.0), 0.02, 1.0)
            metal = np.clip(rs[...,1] * mm,                  0.0,  1.0)
            ao    = np.clip(rs[...,2] ** aop,                 0.0,  1.0)
        else:
            rough = np.full((H,W), 0.5, np.float32)
            metal = np.full((H,W), 0.0, np.float32)
            ao    = np.full((H,W), 1.0, np.float32)

        # Light direction in object space
        L_w = np.array([np.cos(lel)*np.sin(laz),
                        np.sin(lel),
                        np.cos(lel)*np.cos(laz)], np.float32)
        L_o = Ri @ L_w
        L   = _norm(np.broadcast_to(L_o, (H,W,3)).copy())

        # Shade masked pixels
        m = mask
        color = np.zeros((H,W,3), np.float32)
        color[m] = _pbr(albedo[m], N_s[m], V_obj[m], L[m],
                        rough[m], metal[m], ao[m], lc, amb)
        out[m] = color[m]

        return Image.fromarray((_tonemap(out)*255).astype(np.uint8))


# ─── Preview Window ────────────────────────────────────────────────────────────

C_DARK = {
    'bg':'#1e1e1e','surface':'#252526','panel':'#2d2d2d','panel2':'#323232',
    'input':'#3c3c3c','text':'#cccccc','text_dim':'#858585','text_bright':'#e8e8e8',
    'accent':'#0078d4','accent_hi':'#1a86d9','success':'#4ec9b0',
    'warn':'#ce9178','error':'#f44747',
}

class MaterialPreviewWindow:
    RENDER_W = 440
    RENDER_H = 440

    def __init__(self, parent_app):
        self.app      = parent_app
        # Preferred backend: IBL > flat GPU > CPU, based on what's actually usable here.
        if _IBL_PREVIEW_AVAILABLE:
            self.desired_backend = 'ibl'
        elif _GPU_PREVIEW_AVAILABLE:
            self.desired_backend = 'gpu'
        else:
            self.desired_backend = 'cpu'
        self.backend  = None      # set once the worker thread actually constructs a renderer
        self.renderer = None
        self.win      = None
        self._photo   = None
        self._params  = {}
        self._drag0   = None
        self._cam_az, self._cam_el = 0.4, 0.2   # camera/object orbit state, independent of renderer identity
        self._cam_dist = 2.8                     # camera distance (zoom), same idea
        self._maps_dirty = True   # nothing loaded into a renderer yet

        # Persistent single worker thread. Required for IBL mode, since its GL
        # context may only ever be touched from the thread that created it —
        # unlike the old "spawn a fresh thread per click" pattern, this thread
        # (and whatever renderer it owns) lives for the life of this window.
        self._render_q = queue.Queue(maxsize=1)
        self._render_requested = False
        self._render_thread = threading.Thread(target=self._render_loop, daemon=True)
        self._render_thread.start()

    def _render_loop(self):
        while True:
            self._render_q.get()
            while True:
                self._render_requested = False
                self._do_render()
                if not self._render_requested:
                    break

    def _ensure_renderer(self):
        """Runs on the worker thread. (Re)constructs self.renderer if the
        desired backend has changed since the last render."""
        if self.renderer is not None and self.backend == self.desired_backend:
            return
        target = self.desired_backend
        if target == 'ibl':
            self.renderer = _IBLRenderer()
        elif target == 'gpu':
            self.renderer = _GPURenderer()
        else:
            self.renderer = SoftwarePBRRenderer()
        self.backend = target
        self._reload_maps_now()
        self._maps_dirty = False
        if hasattr(self, '_backend_lbl') and self._backend_lbl.winfo_exists():
            self.win.after(0, self._update_backend_label)

    def _downgrade_backend(self, reason=''):
        """Runs on the worker thread after a render/construction failure.
        Steps one tier down: ibl -> gpu -> cpu."""
        if self.desired_backend == 'ibl':
            self.desired_backend = 'gpu' if _GPU_PREVIEW_AVAILABLE else 'cpu'
        elif self.desired_backend == 'gpu':
            self.desired_backend = 'cpu'
        else:
            return  # already on CPU, nothing lower to fall back to
        self.renderer = None
        self.backend = None
        self._ensure_renderer()

    def _update_backend_label(self):
        txt = {'ibl': 'GPU — IBL Sky (ModernGL)',
               'gpu': 'GPU — Studio (ModernGL)',
               'cpu': 'CPU (software)'}.get(self.backend, '?')
        fg = C_DARK['success'] if self.backend in ('ibl', 'gpu') else C_DARK['warn']
        self._backend_lbl.config(text=txt, fg=fg)

    # ── Public ────────────────────────────────────────────────────────────────
    def open(self):
        if self.win and self.win.winfo_exists():
            self.win.lift(); return
        self._build_window()
        self._reload_maps()

    # ── Window builder ────────────────────────────────────────────────────────
    def _build_window(self):
        C = C_DARK
        self.win = tk.Toplevel(self.app.root)
        self.win.title('Material Preview')
        self.win.geometry('1000x660')
        self.win.configure(bg=C['bg'])
        self.win.resizable(True, True)

        # Header
        hdr = tk.Frame(self.win, bg='#131313', height=40)
        hdr.pack(fill='x'); hdr.pack_propagate(False)
        tk.Label(hdr, text='⬡  MATERIAL PREVIEW  — drag to rotate',
                 bg='#131313', fg=C['text_bright'],
                 font=('Segoe UI', 10, 'bold')).pack(side='left', padx=14, pady=9)
        self._backend_lbl = tk.Label(hdr, text='Initializing…', bg='#131313',
                                      fg=C['text_dim'], font=('Segoe UI', 9, 'bold'))
        self._backend_lbl.pack(side='right', padx=14, pady=9)


        main = tk.Frame(self.win, bg=C['bg']); main.pack(fill='both', expand=True)

        # ── Left: render canvas ───────────────────────────────────────────────
        lf = tk.Frame(main, bg=C['surface'])
        lf.pack(side='left', fill='both', expand=True)

        W, H = self.RENDER_W, self.RENDER_H
        self._render_w, self._render_h = W, H
        self.canvas = tk.Canvas(lf, width=W, height=H, bg='#111',
                                 highlightthickness=0, cursor='fleur')
        self.canvas.pack(fill='both', expand=True, padx=12, pady=12)
        self.canvas.create_text(W//2, H//2, text='Loading…',
                                fill=C['text_dim'], font=('Segoe UI', 14), tags='msg')
        self.canvas.bind('<ButtonPress-1>',   self._on_drag_start)
        self.canvas.bind('<B1-Motion>',       self._on_drag_move)
        self.canvas.bind('<ButtonRelease-1>', self._on_drag_end)
        self.canvas.bind('<Configure>',       self._on_canvas_resize)
        self.canvas.bind('<MouseWheel>',      self._on_zoom)     # Windows / macOS
        self.canvas.bind('<Button-4>',        self._on_zoom)     # Linux scroll up
        self.canvas.bind('<Button-5>',        self._on_zoom)     # Linux scroll down
        self._resize_after_id = None

        # Mesh selector
        mf = tk.Frame(lf, bg=C['surface']); mf.pack(fill='x', padx=12, pady=(0,6))
        tk.Label(mf, text='Mesh:', bg=C['surface'], fg=C['text_dim'],
                 font=('Segoe UI', 8)).pack(side='left', padx=(0,8))
        self._shape = tk.StringVar(value='sphere')
        for val, lbl in [('sphere','● Sphere'),('cube','■ Cube'),
                          ('cylinder','⬤ Cylinder'),('plane','▬ Plane')]:
            ttk.Radiobutton(mf, text=lbl, variable=self._shape, value=val,
                            command=self._queue_render).pack(side='left', padx=(0,10))

        # Status + progress
        sf_bot = tk.Frame(lf, bg=C['surface']); sf_bot.pack(fill='x', padx=12, pady=(0,10))
        self._prog = tk.DoubleVar(value=0)
        ttk.Progressbar(sf_bot, variable=self._prog, maximum=100, length=220
                        ).pack(side='left', padx=(0,10))
        self._status = tk.Label(sf_bot, text='Ready', bg=C['surface'],
                                 fg=C['text_dim'], font=('Segoe UI', 8))
        self._status.pack(side='left')

        # ── Right: control panel (scrollable) ────────────────────────────────
        rf = tk.Frame(main, bg=C['panel'], width=430)
        rf.pack(side='right', fill='y'); rf.pack_propagate(False)

        rc = tk.Canvas(rf, bg=C['panel'], highlightthickness=0)
        rsb = ttk.Scrollbar(rf, orient='vertical', command=rc.yview)
        ctl = tk.Frame(rc, bg=C['panel'])
        ctl.bind('<Configure>', lambda e: rc.configure(scrollregion=rc.bbox('all')))
        wid = rc.create_window((0,0), window=ctl, anchor='nw')
        rc.configure(yscrollcommand=rsb.set)
        rc.bind('<Configure>', lambda e: rc.itemconfig(wid, width=e.width))
        rc.pack(side='left', fill='both', expand=True)
        rsb.pack(side='right', fill='y')

        self._build_controls(ctl, C)

    def _sec(self, p, txt):
        C = C_DARK
        tk.Label(p, text=txt, bg=C['panel'], fg=C['accent'],
                 font=('Segoe UI', 9, 'bold')).pack(anchor='w', padx=12, pady=(12,2))

    def _slider(self, p, label, lo, hi, res, default, key):
        C = C_DARK
        var = tk.DoubleVar(value=default)
        self._params[key] = var
        row = tk.Frame(p, bg=C['panel']); row.pack(fill='x', padx=12, pady=2)
        tk.Label(row, text=label, bg=C['panel'], fg=C['text'],
                 font=('Segoe UI', 8), width=22, anchor='w').pack(side='left')
        lbl = tk.Label(row, text=f'{default:.2f}', bg=C['panel'],
                       fg=C['text_bright'], font=('Consolas', 8), width=6)
        lbl.pack(side='right')
        def _upd(v, l=lbl): l.config(text=f'{float(v):.2f}')
        tk.Scale(row, from_=lo, to=hi, resolution=res, orient='horizontal',
                 variable=var, bg=C['panel'], fg=C['text'],
                 troughcolor=C['input'], highlightthickness=0,
                 showvalue=False, sliderlength=14,
                 command=_upd
                 ).pack(fill='x', expand=True)
        return var

    def _build_controls(self, ctl, C):
        # MATERIAL
        self._sec(ctl, 'MATERIAL')
        self._slider(ctl, 'Metallic Multiplier',   0.0, 2.0,  0.05, 1.0, 'metallic_mult')
        self._slider(ctl, 'Smoothness Multiplier', 0.0, 2.0,  0.05, 1.0, 'smooth_mult')
        self._slider(ctl, 'AO Power',              0.5, 4.0,  0.05, 1.0, 'ao_power')
        self._slider(ctl, 'Parallax Depth',        0.0, 0.15, 0.005,0.0, 'parallax_depth')

        ttk.Separator(ctl).pack(fill='x', padx=12, pady=8)

        # LIGHTING
        self._sec(ctl, 'LIGHTING')
        self._slider(ctl, 'Light Intensity',  0.1, 6.0,  0.1,  2.0, 'light_intensity')
        self._slider(ctl, 'Light Azimuth',   -3.14,3.14, 0.05, 0.5, 'light_az')
        self._slider(ctl, 'Light Elevation',  0.0, 1.57, 0.05, 0.8, 'light_el')

        lc_row = tk.Frame(ctl, bg=C['panel']); lc_row.pack(fill='x', padx=12, pady=4)
        tk.Label(lc_row, text='Light Color', bg=C['panel'], fg=C['text'],
                 font=('Segoe UI', 8), width=22, anchor='w').pack(side='left')
        self._lc = [1.0, 1.0, 1.0]
        self._lc_btn = tk.Button(lc_row, text='  ■  White  ',
                                  bg='#ffffff', fg='#000000',
                                  relief='flat', cursor='hand2', font=('Segoe UI', 8),
                                  command=self._pick_light_color)
        self._lc_btn.pack(side='right')

        ttk.Separator(ctl).pack(fill='x', padx=12, pady=8)

        # LIGHTING MODE
        self._sec(ctl, 'LIGHTING MODE')
        mode_row = tk.Frame(ctl, bg=C['panel']); mode_row.pack(fill='x', padx=12, pady=2)
        self._mode_var = tk.StringVar(value=self.desired_backend)
        modes = [('ibl', 'IBL Sky (GPU)'), ('gpu', 'Studio (GPU)'), ('cpu', 'Studio (CPU)')]
        for val, lbl in modes:
            available = (val == 'ibl' and _IBL_PREVIEW_AVAILABLE) or \
                        (val == 'gpu' and _GPU_PREVIEW_AVAILABLE) or (val == 'cpu')
            rb = ttk.Radiobutton(mode_row, text=lbl, variable=self._mode_var, value=val,
                                  command=self._on_mode_change, style='Dark.TCheckbutton')
            rb.pack(anchor='w', pady=1)
            if not available:
                rb.state(['disabled'])
        tk.Label(ctl, text='IBL Sky uses the Light Azimuth/Elevation above as the sun direction.',
                 bg=C['panel'], fg=C['text_dim'], font=('Segoe UI', 7),
                 wraplength=380, justify='left').pack(anchor='w', padx=12, pady=(2,0))

        ttk.Separator(ctl).pack(fill='x', padx=12, pady=8)

        # IBL / ADVANCED SHADING (only affect IBL mode; harmless elsewhere)
        self._sec(ctl, 'IBL / ADVANCED SHADING')
        self._slider(ctl, 'IBL Intensity',      0.0, 3.0,  0.05, 1.0,  'ibl_intensity')
        self._slider(ctl, 'Clearcoat Weight',   0.0, 1.0,  0.02, 0.0,  'coat_weight')
        self._slider(ctl, 'Clearcoat Roughness',0.02,1.0,  0.02, 0.08, 'coat_roughness')
        self._slider(ctl, 'Fuzz / Sheen Weight',0.0, 1.0,  0.02, 0.0,  'fuzz_weight')

        ttk.Separator(ctl).pack(fill='x', padx=12, pady=8)

        # TEXTURE
        self._sec(ctl, 'TEXTURE')
        self._slider(ctl, 'Tiling',    0.1, 8.0,  0.1,  1.0, 'tiling')
        self._slider(ctl, 'Offset U', -1.0, 1.0,  0.05, 0.0, 'offset_u')
        self._slider(ctl, 'Offset V', -1.0, 1.0,  0.05, 0.0, 'offset_v')

        ttk.Separator(ctl).pack(fill='x', padx=12, pady=8)

        # ACTIONS
        self._sec(ctl, 'ACTIONS')
        act = tk.Frame(ctl, bg=C['panel']); act.pack(fill='x', padx=12, pady=4)

        def _mkbtn(txt, cmd, bg=None, fg='white', **kw):
            return tk.Button(act, text=txt, command=cmd,
                             bg=bg or C['accent'], fg=fg,
                             activebackground=C['accent_hi'], activeforeground='white',
                             relief='flat', cursor='hand2', bd=0,
                             font=('Segoe UI', 9, 'bold'), padx=10, pady=7, **kw)

        _mkbtn('▶  Render', self._queue_render).pack(fill='x', pady=(0,4))
        _mkbtn('↺  Reload Maps', self._reload_and_render,
               bg=C['panel2'], fg=C['text']).pack(fill='x', pady=(0,4))

        ttk.Separator(ctl).pack(fill='x', padx=12, pady=8)

        # APPLY TO RMAOS
        self._sec(ctl, 'APPLY MULTIPLIERS → RMAOS')
        tk.Label(ctl,
                 text='Bake current Metallic, Smoothness and AO Power\n'
                      'multipliers into the RMAOS texture and update the\n'
                      'Material Generator preview cell.',
                 bg=C['panel'], fg=C['text_dim'],
                 font=('Segoe UI', 8), justify='left').pack(anchor='w', padx=12, pady=(0,6))

        tk.Button(ctl, text='🗜  Apply & Save New RMAOS',
                  command=self._apply_to_rmaos,
                  bg='#1a3a1a', fg=C['success'],
                  activebackground='#2a5a2a', activeforeground=C['success'],
                  relief='flat', cursor='hand2', font=('Segoe UI', 9, 'bold'),
                  padx=12, pady=8, bd=0
                  ).pack(fill='x', padx=12, pady=4)

        self._apply_lbl = tk.Label(ctl, text='', bg=C['panel'],
                                    fg=C['success'], font=('Segoe UI', 8))
        self._apply_lbl.pack(anchor='w', padx=12, pady=(0,16))

    # ── Controls ──────────────────────────────────────────────────────────────
    def _get_params(self):
        p = {k: v.get() for k, v in self._params.items() if isinstance(v, tk.Variable)}
        p['light_color'] = list(self._lc)
        return p

    def _pick_light_color(self):
        result = colorchooser.askcolor(title='Light Color',
                                        color=self._lc_btn.cget('bg'),
                                        parent=self.win)
        if result and result[0]:
            r, g, b = [x/255.0 for x in result[0]]
            self._lc = [r, g, b]
            hex_c = result[1]
            lum = r*0.299 + g*0.587 + b*0.114
            self._lc_btn.config(bg=hex_c, fg='white' if lum < 0.5 else '#111')
            self._lc_btn.config(text=f'  ■  {hex_c}  ')

    # ── Drag-to-rotate ────────────────────────────────────────────────────────
    def _on_drag_start(self, e):
        self._drag0 = (e.x, e.y)
        self._az0, self._el0 = self._cam_az, self._cam_el

    def _on_drag_move(self, e):
        if not self._drag0: return
        dx, dy = e.x - self._drag0[0], e.y - self._drag0[1]
        self._cam_az = self._az0 + dx * 0.012
        self._cam_el = float(np.clip(self._el0 - dy * 0.012, -1.4, 1.4))
        self.canvas.delete('hint')
        self._queue_render()

    def _on_drag_end(self, e):
        self._drag0 = None
        self.canvas.delete('hint')
        self._queue_render()

    def _on_canvas_resize(self, e):
        """Canvas now actually expands with the window (it didn't before --
        that's why the render used to look 'stuck small' no matter how big
        you made the window). Debounced so dragging the window edge doesn't
        trigger a render on every single pixel of resize."""
        new_w, new_h = max(int(e.width), 64), max(int(e.height), 64)
        if (new_w, new_h) == (self._render_w, self._render_h):
            return
        self._render_w, self._render_h = new_w, new_h
        if self._resize_after_id:
            try: self.win.after_cancel(self._resize_after_id)
            except Exception: pass
        self._resize_after_id = self.win.after(120, self._queue_render)

    def _on_zoom(self, e):
        """Mouse wheel = dolly the camera in/out. Works with both the
        Windows/macOS <MouseWheel> delta convention and Linux's X11
        Button-4/Button-5 scroll events."""
        if getattr(e, 'delta', 0) > 0 or getattr(e, 'num', None) == 4:
            factor = 0.9   # scroll up / away = zoom in
        else:
            factor = 1.1   # scroll down / toward = zoom out
        self._cam_dist = float(np.clip(self._cam_dist * factor, 1.2, 9.0))
        self._queue_render()

    def _on_mode_change(self):
        self.desired_backend = self._mode_var.get()
        self._queue_render()

    # ── Rendering ─────────────────────────────────────────────────────────────
    def _queue_render(self, *_):
        if not (self.win and self.win.winfo_exists()):
            return
        self._render_requested = True
        try:
            self._render_q.put_nowait(True)
        except queue.Full:
            pass

    def _do_render(self):
        self.win.after(0, lambda: (self._status.config(text='Rendering…'),
                                    self._prog.set(5)))
        t0 = time.time()
        try:
            self._ensure_renderer()
            if self._maps_dirty:
                self._reload_maps_now()
                self._maps_dirty = False
            self.renderer.az, self.renderer.el = self._cam_az, self._cam_el
            self.renderer.dist = self._cam_dist
            params = self._get_params()
            if self.backend == 'ibl':
                self.renderer.set_environment(
                    sun_az=params.get('light_az', 0.5),
                    sun_el=params.get('light_el', 0.8),
                    sun_intensity=params.get('light_intensity', 2.0) * 3.0)
            shape = self._shape.get()
            img   = self.renderer.render(self._render_w, self._render_h,
                                          shape=shape, p=params)
            self.win.after(0, lambda i=img: self._show(i))
            elapsed = time.time() - t0
            backend = self.backend.upper()
            self.win.after(0, lambda s=elapsed, b=backend:
                self._status.config(text=f'Done  {s*1000:.0f}ms  [{b}]'))
        except Exception as e:
            failed_tier = self.desired_backend
            if failed_tier in ('ibl', 'gpu'):
                # GPU/driver/shader issue on this machine — drop a tier and retry once.
                self._log_render_failure(failed_tier, e)
                self.win.after(0, lambda p=failed_tier: self._status.config(
                    text=f'{p.upper()} render failed, falling back… (see tg3_preview_errors.log)'))
                self._downgrade_backend()
                self._render_requested = True
            else:
                self._log_render_failure(failed_tier, e)
                self.win.after(0, lambda err=str(e):
                    self._status.config(text=f'Error: {err}'))
        self.win.after(0, lambda: self._prog.set(100))

    def _log_render_failure(self, tier, exc):
        """Best-effort write of the real exception + traceback to a log file
        next to the exe, since a --windowed PyInstaller build has no console
        to print to and this failure was previously silently discarded."""
        try:
            if getattr(sys, 'frozen', False):
                base_dir = os.path.dirname(os.path.abspath(sys.executable))
            else:
                base_dir = os.path.dirname(os.path.abspath(__file__))
            log_path = Path(base_dir) / 'tg3_preview_errors.log'
            with log_path.open('a', encoding='utf-8') as f:
                f.write(f'\n[{time.strftime("%Y-%m-%d %H:%M:%S")}] backend={tier} failed:\n')
                f.write(''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        except Exception:
            pass  # logging must never itself crash the render loop

    def _show(self, img):
        self._photo = ImageTk.PhotoImage(img)
        W, H = self._render_w, self._render_h
        self.canvas.delete('all')
        self.canvas.create_image(W//2, H//2, anchor='center', image=self._photo)

    # ── Map loading ───────────────────────────────────────────────────────────
    def _reload_maps(self):
        """Safe to call from the Tkinter thread: just flags textures dirty and
        queues a render. The actual PIL loads happen on the worker thread
        (self.renderer may not exist yet, or may be mid-use on that thread)."""
        self._maps_dirty = True
        self._queue_render()

    def _reload_maps_now(self):
        """Worker-thread only. Actually populates self.renderer's textures."""
        self.renderer.clear()
        MAP = {'diffuse': 'albedo', 'normal': 'normal',
               'height': 'height', 'rmaos': 'rmaos'}
        for mat_key, tex_key in MAP.items():
            path = getattr(self.app, '_mat_paths', {}).get(mat_key, '')
            if path and os.path.isfile(path):
                self.renderer.load_texture(tex_key, path)

    def _reload_and_render(self):
        self._reload_maps()

    # ── Apply to RMAOS ────────────────────────────────────────────────────────
    def _apply_to_rmaos(self):
        rp = getattr(self.app, '_mat_paths', {}).get('rmaos', '')
        if not rp or not os.path.isfile(rp):
            self._apply_lbl.config(
                text='No RMAOS found — generate maps first.', fg=C_DARK['warn'])
            return

        p   = self._get_params()
        mm  = float(p.get('metallic_mult',  1.0))
        sm  = float(p.get('smooth_mult',    1.0))
        aop = float(p.get('ao_power',       1.0))

        try:
            orig = np.array(Image.open(rp)).astype(np.float32) / 255.0

            # RMAOS channels: R=rough, G=metal, B=AO, A=spec
            new_r = np.clip(orig[..., 0] * max(2.0 - sm, 0.0), 0, 1)  # roughness ↓ when smooth↑
            new_g = np.clip(orig[..., 1] * mm,                  0, 1)  # metalness
            new_b = np.clip(orig[..., 2] ** aop,                0, 1)  # AO
            new_a = np.clip(1.0 - new_r,                        0, 1)  # specular = 1 - rough

            result = (np.stack([new_r, new_g, new_b, new_a], -1) * 255).astype(np.uint8)
            Image.fromarray(result).save(rp)

            # Refresh preview cell
            self.app.root.after(0, lambda: self.app._mat_update_cell('rmaos', rp))
            # Reload into renderer (routed via the worker thread — safe even
            # mid-render, and works even before any render has happened yet)
            self._reload_maps()

            self._apply_lbl.config(
                text=f'✓  metal×{mm:.2f}  smooth×{sm:.2f}  AO^{aop:.2f}',
                fg=C_DARK['success'])

        except Exception as e:
            self._apply_lbl.config(text=f'Error: {e}', fg=C_DARK['error'])
